"""Chat commands players type into the lobby chat, answered by the server.

The client handles a few slash commands itself (/MSG, /WHISPER, /IGNORE, /UNIGNORE, /CHATLOG,
parser 0x5c67a0) and sends every other one to the server as a ChatCommand (0x22, from 0x5c859c).
The answer, a ChatCommandAnswer (0x23), shows in the chat as "[Server]: line" (0x44a43c).

Commands:
    /help               what this server offers
    /who                who is online
    /clan               your clan and which of its members are online
    /net                the address the server sees you at, and what to forward on your router
    /rank [NAME]        a player's Liga rating and place (yours without a name)
    /top [SEASON]       the best five of the infinite season, or of a custom season
    /clanrank [CLAN]    a clan's Clanliga rating and its strength from Liga (yours without a name)
    /seasons            the custom seasons running now
    /season NAME        one custom season: dates, members, its top three and your place
    /opponents          Liga members online now, closest to your rating first
    /results            your last Liga results and how they moved your rating
    /vs NAME            your Liga record against one player
    /pending            your ladder games still waiting for reports or an admin's decision
    /ladder [join|leave [LADDER]]   the ladders you are in; join or leave Liga (the default),
                        Clanliga or a custom season by name. Only members' games count.
    /announce TEXT      (admins) a line for every player in the lobby
    /kick NAME [REASON]          (admins) remove a player from the lobby; they may log on again
    /ban NAME HOURS [REASON]     (admins) bar an account, online or not; an online player is removed
    /confirm CODE                (admins) carry out the last /announce, /kick or /ban

The admin commands wait for /confirm with a code shown only to the admin: on a network that lets
datagrams carry a forged sender address, a forger could send a command as an admin, but never sees
the code.

Player names may hold spaces ("Roman Legion"), so the name is the longest run of words at the
start of the argument that names a player.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from s3lobby.constants import (
    CONFIRMATION_CODE_DIGITS,
    CONFIRMATION_SECONDS,
    DIRECTPLAY_ENUMERATION_PORT,
    DIRECTPLAY_FIRST_GAME_PORT,
    DIRECTPLAY_LAST_GAME_PORT,
    GAME_CLIENT_FIRST_PORT,
    LADDER_COMMAND_BURST,
    LADDER_COMMANDS_PER_SECOND,
    LADDER_TOP_COUNT,
    MAX_BAN_HOURS,
    MAX_GROUP_TEXT_LENGTH,
    MIN_BAN_HOURS,
)
from s3lobby.database import DatabaseError, LobbyDatabase, is_whole_number_text, utc_now, with_reason
from s3lobby.guards import RateLimiter
from s3lobby.ladder import INFINITE_SEASON, Season
from s3lobby.ladder_book import (
    CLANLIGA,
    LIGA,
    LadderBook,
    LadderMembershipError,
    StandingRow,
    join_ladder,
    ladder_labels,
    ladder_members,
    leave_ladder,
)
from s3lobby.ladder_texts import ENGLISH, LadderTexts
from s3lobby.reachability import Reachability
from s3lobby.wire import Address

COMMAND_PREFIX = "/"
# The words after /ladder that join or leave a ladder.
LADDER_JOIN = "join"
LADDER_LEAVE = "leave"
LADDER_RULES = "rules"
# The commands that read or change the ladder; they share one limit per player.
LADDER_COMMAND_NAMES = frozenset(
    {"rank", "top", "clanrank", "seasons", "season", "ladder", "opponents", "results", "vs", "pending"}
)
# How many lines or names the ladder's answers give: a season's top, opponents online, own results,
# pending games. Each answer stays within a few chat lines.
SEASON_TOP_COUNT = 3
OPPONENT_COUNT = 8
OWN_RESULT_COUNT = 5
PENDING_GAME_COUNT = 5
# Answers stay short: the chat shows each line on its own, and a long answer would push the
# conversation off the screen.
MAX_ANSWER_LINE_LENGTH = 100
MAX_ANSWER_LINES = 12
KICK_REASON_PREFIX = "An admin removed you from the lobby"
REACHABILITY_ANSWERS = {
    Reachability.CHECKING: "The check whether others can reach you is still running; ask again in a few seconds.",
    Reachability.OPEN: "Check: a test from another port reached you, so others can probably write to you first.",
    Reachability.BLOCKED: (
        f"Check: your router blocked a test from another port. Forward UDP port {GAME_CLIENT_FIRST_PORT} "
        "to this PC, or others reach you only after you wrote to them."
    ),
}


class CommandSender(Protocol):
    """What the commands need to know about the player who typed one (lobby.Player fits)."""

    player_id: int
    name: str
    address: Address


@dataclass(frozen=True)
class PendingConfirmation:
    """An admin command waiting for /confirm: its code, what it does, and until when it waits."""

    code: str
    action: Callable[[], list[str]]
    expires_at: float


class ChatCommands:
    """Answers chat commands. online_player_names lists who is in the lobby right now; announce
    sends a line to every player and returns how many it reached; kick removes the player with
    that id from the lobby, telling them the reason, and returns False when they are not online;
    reachability_of tells what the reachability probe found for a client's address."""

    def __init__(
        self,
        database: LobbyDatabase,
        online_player_names: Callable[[], list[str]],
        announce: Callable[[str], int],
        kick: Callable[[int, str], bool],
        reachability_of: Callable[[Address], Reachability],
        clock_seconds: Callable[[], float] = time.monotonic,
    ) -> None:
        """See the class docstring for what each callable is expected to do."""
        self._database = database
        self._online_player_names = online_player_names
        self._announce = announce
        self._kick = kick
        self._reachability_of = reachability_of
        self._clock_seconds = clock_seconds
        # The admin command each admin still has to confirm, by player id.
        self._pending_confirmations: dict[int, PendingConfirmation] = {}
        self._ladder = LadderBook(database)
        self._ladder_questions = RateLimiter(LADDER_COMMANDS_PER_SECOND, LADDER_COMMAND_BURST)

    def answer(self, sender: CommandSender, command_text: str) -> tuple[str, ...]:
        """The lines that answer one command."""
        command_name, argument = split_command(command_text)
        # One handler per command: the players' own first, then the ladder's (limited together),
        # then the admins' (each checks admin rights itself).
        match command_name:
            case "help":
                lines = self._help(sender)
            case "who":
                lines = self._who()
            case "clan":
                lines = self._clan(sender)
            case "net":
                lines = describe_network(sender.address, self._reachability_of(sender.address))
            case _ if command_name in LADDER_COMMAND_NAMES:
                lines = self._answer_ladder_question(sender, command_name, argument)
            # Admin commands wait for /confirm with a code only the admin sees.
            case "announce":
                lines = self._announce_for(sender, argument)
            case "kick":
                lines = self._kick_for(sender, argument)
            case "ban":
                lines = self._ban_for(sender, argument)
            case "confirm":
                lines = self._confirm(sender, argument)
            case _:
                lines = [f"Unknown command {COMMAND_PREFIX}{command_name}. Type {COMMAND_PREFIX}help for the list."]
        return fit_answer(lines)

    def _help(self, sender: CommandSender) -> list[str]:
        """The commands this player may use; the admin ones only for a player who is an admin now."""
        # Every player's commands, one line each.
        lines = [
            "/who: who is online",
            "/clan: your clan and its members online",
            "/net: your address as the server sees it, and what to forward on your router",
            "/rank [NAME], /top [SEASON], /clanrank [CLAN], /seasons: the ladder",
            "/ladder, /ladder join [LADDER], /ladder leave [LADDER], /ladder rules: your ladders",
            "/opponents, /results, /vs NAME, /pending, /season NAME: find games, your record",
        ]
        # The admin commands only for an admin, by the rights the database has now.
        if self._is_admin_now(sender):
            lines.append("/announce TEXT: a line for every player in the lobby")
            lines.append("/kick NAME [REASON]: remove a player from the lobby")
            lines.append("/ban NAME HOURS [REASON]: bar an account, online or not")
            lines.append("/confirm CODE: carry out the command you just gave")
        return lines

    def _ask_for_confirmation(
        self, sender: CommandSender, description: str, action: Callable[[], list[str]]
    ) -> list[str]:
        """Hold an admin command until the admin confirms it with a random code. A datagram forged
        from an admin's address is sent blind: its sender never sees the code, and a wrong one
        cancels the command, so guessing does not work either."""
        # A random number padded with leading zeros, so every code has exactly
        # CONFIRMATION_CODE_DIGITS digits.
        code = f"{secrets.randbelow(10 ** CONFIRMATION_CODE_DIGITS):0{CONFIRMATION_CODE_DIGITS}d}"
        expires_at = self._clock_seconds() + CONFIRMATION_SECONDS
        # One pending command per admin: a new one replaces the command given before.
        self._pending_confirmations[sender.player_id] = PendingConfirmation(code, action, expires_at)
        return [f"To {description}, type {COMMAND_PREFIX}confirm {code} within {CONFIRMATION_SECONDS} seconds."]

    def _confirm(self, sender: CommandSender, code: str) -> list[str]:
        """Carry out the admin's pending command if the code matches and has not run out. The pending
        command is dropped in every case, and admin rights are checked again, as they may have been
        taken away since the command was given."""
        pending = self._pending_confirmations.pop(sender.player_id, None)
        if pending is None:
            return ["There is nothing to confirm."]
        if self._clock_seconds() > pending.expires_at:
            return ["That command ran out; give it again."]
        # Compared in constant time, so the time of the answer does not tell how much of a guess was right.
        if not hmac.compare_digest(code.encode("utf-8"), pending.code.encode("utf-8")):
            return ["Wrong code: the command was cancelled."]
        if not self._is_admin_now(sender):
            return ["You are no admin any more."]
        return pending.action()

    def _is_admin_now(self, sender: CommandSender) -> bool:
        """Admin rights as the database has them now: an admin whose rights were taken away while
        online must not keep using /kick, /ban and /announce until the next logon."""
        account = self._database.find_account_by_id(sender.player_id)
        return account is not None and account.is_admin

    def _who(self) -> list[str]:
        """One line with the number and the names of the players online, sorted without regard to case."""
        names = sorted(self._online_player_names(), key=str.casefold)
        return [f"{len(names)} player(s) online: {', '.join(names)}"]

    def _clan(self, sender: CommandSender) -> list[str]:
        """The sender's clan, its number of members and which of them are online."""
        clan = self._database.find_clan_of_account(sender.player_id)
        if clan is None:
            return ["You are in no clan."]
        online = {name.casefold() for name in self._online_player_names()}
        members = self._database.list_clan_member_names(clan.clan_id)
        online_members = [name for name in members if name.casefold() in online]
        return [f"Clan {clan.name}: {len(members)} member(s), online: {', '.join(online_members) or 'nobody'}"]

    def _answer_ladder_question(self, sender: CommandSender, command_name: str, argument: str) -> list[str]:
        """The ladder's chat commands, limited per player: they read every result, so a client
        sending them in a loop must not keep the server busy. Names are cut to the longest a name
        can be."""
        if not self._ladder_questions.allows(sender.player_id, self._clock_seconds()):
            return ["Too many ladder questions; ask again in a few seconds."]
        name = argument[:MAX_GROUP_TEXT_LENGTH].strip()
        match command_name:
            case "rank":
                return self._rank(sender, name)
            case "top":
                return self._top(name)
            case "season":
                return self._season(sender, name)
            case "opponents":
                return self._opponents(sender)
            case "results":
                return self._results(sender)
            case "vs":
                return self._versus(sender, name)
            case "pending":
                return self._pending(sender)
            case "clanrank":
                return self._clan_rank(sender, name)
            case "ladder":
                return self._ladder_membership(sender, argument)
        return self._seasons()

    def _ladder_membership(self, sender: CommandSender, argument: str) -> list[str]:
        """/ladder: the ladders the player is in; /ladder join [LADDER] and /ladder leave [LADDER]
        change that. Players take part only in ladders they joined (owner's decision), and the game
        has no other place to do it."""
        # The word after /ladder, and the ladder named after it, cut to the longest name there is.
        action, ladder_name = split_command(COMMAND_PREFIX + argument.strip())
        ladder_name = ladder_name[:MAX_GROUP_TEXT_LENGTH].strip()
        # /ladder rules: the board's info message, line by line.
        if action == LADDER_RULES:
            return [line for line in LadderTexts(self._database, self._ladder).info(ENGLISH).lines if line]
        # /ladder join and /ladder leave; a refusal (unknown ladder, joined already) is told as it
        # is.
        try:
            if action == LADDER_JOIN:
                return [join_ladder(self._ladder, self._database, sender.player_id, ladder_name)]
            if action == LADDER_LEAVE:
                return [leave_ladder(self._ladder, self._database, sender.player_id, ladder_name)]
        except LadderMembershipError as error:
            return [f"Ladder: {error}."]
        # Any other word: how to use the command.
        if action:
            return [
                f"Type {COMMAND_PREFIX}ladder join or {COMMAND_PREFIX}ladder leave, with liga, clanliga or a "
                f"season, or {COMMAND_PREFIX}ladder rules."
            ]
        # /ladder alone: the ladders the player is in now.
        labels = ladder_labels(self._database, sender.player_id)
        if not labels:
            return [f"You are in no ladder: your ladder games do not count. Type {COMMAND_PREFIX}ladder join."]
        return [f"Your ladders: {', '.join(labels)}. Games count where every player has joined."]

    def _rank(self, sender: CommandSender, name: str) -> list[str]:
        """/rank [NAME]: a player's Liga rating and place in the infinite season and in every
        running custom season that counts Liga, and the Clanliga record."""
        account = self._database.find_account(name) if name else self._database.find_account_by_id(sender.player_id)
        if account is None:
            return [f"There is no account named {name}."]
        lines = []
        rows = self._ladder.standing_rows(INFINITE_SEASON, LIGA)
        row = row_of(rows, account.account_id)
        if row is None:
            lines.append(f"{account.name} has no Liga place in the infinite season yet.")
        else:
            provisional = ", provisional" if row.is_provisional else ""
            lines.append(
                f"{account.name}: Liga {row.rating}, place {row.place} of {len(rows)} (infinite season{provisional})."
            )
            lines.append(f"Won {row.won}, lost {row.lost}, left {row.left}.")
        for season in self._running_custom_seasons():
            season_rows = self._ladder.standing_rows(season, LIGA)
            season_row = row_of(season_rows, account.account_id)
            if season_row is not None:
                lines.append(f"{season.name}: {season_row.rating}, place {season_row.place} of {len(season_rows)}.")
        clanliga_record = self._ladder.ratings().clanliga_players.get(account.account_id)
        if clanliga_record is not None:
            lines.append(f"Clanliga: won {clanliga_record.won}, lost {clanliga_record.lost} for the clan.")
        return lines

    def _top(self, argument: str) -> list[str]:
        """/top [SEASON] [clanliga]: the best of the infinite season's Liga, or of a custom season;
        with "clanliga" at the end, the best clans instead."""
        # A last word "clanliga" asks for the clans; the words before it name the season.
        ladder = LIGA
        season_name = argument
        words = argument.split()
        if words and words[-1].lower() == CLANLIGA:
            ladder = CLANLIGA
            season_name = " ".join(words[:-1])
        try:
            season = self._ladder.find_season(season_name)
        except LookupError:
            return [f"There is no season named {season_name}. {COMMAND_PREFIX}seasons lists the running ones."]
        # The first places, in one line.
        rows = self._ladder.standing_rows(season, ladder)[:LADDER_TOP_COUNT]
        # "Liga" or "Clanliga" for the infinite season; a custom season's name, with "Clanliga"
        # after it for its clans.
        label = "Liga"
        if season.season_id is not None:
            label = season.name
        if ladder == CLANLIGA and season.season_id is None:
            label = "Clanliga"
        elif ladder == CLANLIGA:
            label = f"{season.name} Clanliga"
        if not rows:
            return [f"{label}: nobody has a place yet."]
        return [f"{label}: " + ", ".join(f"{row.place} {row.name} {row.rating}" for row in rows) + "."]

    def _season(self, sender: CommandSender, name: str) -> list[str]:
        """/season NAME: a custom season's dates, what it counts, its members, its top three and
        the player's place."""
        # Only custom seasons: the infinite season has /top and /rank.
        if not name:
            return [f"Name a season: {COMMAND_PREFIX}season NAME. {COMMAND_PREFIX}seasons lists the running ones."]
        try:
            season = self._ladder.find_season(name)
        except LookupError:
            return [f"There is no season named {name}. {COMMAND_PREFIX}seasons lists the running ones."]
        if season.season_id is None:
            return [f"That is the infinite season: {COMMAND_PREFIX}top and {COMMAND_PREFIX}rank."]
        # The season's dates, what it counts and how many joined it.
        members = next((item for item in ladder_members(self._database) if item.ladder == season.name), None)
        member_count = len(members.names) if members is not None else 0
        lines = [f"{season.name}: {season_end_text(season)}, counts {season.counts.value}, {member_count} member(s)."]
        # Its top three, and the player's own place, or how to join it.
        rows = self._ladder.standing_rows(season, LIGA)
        if rows:
            lines.append("Top: " + ", ".join(f"{row.place} {row.name} {row.rating}" for row in rows[:SEASON_TOP_COUNT]) + ".")
        row = row_of(rows, sender.player_id)
        if row is not None:
            lines.append(f"You: place {row.place} of {len(rows)}, {row.rating}.")
        elif season.name not in ladder_labels(self._database, sender.player_id):
            lines.append(f"You have not joined it: {COMMAND_PREFIX}ladder join {season.name}.")
        return lines

    def _opponents(self, sender: CommandSender) -> list[str]:
        """/opponents: the Liga members online now, closest to the player's rating first, so a
        player finds someone for a ladder game."""
        # The Liga members online besides the player, nearest rating first.
        online_ids = self._online_account_ids()
        opponents = self._ladder.opponents(sender.player_id, online_ids)[:OPPONENT_COUNT]
        if not opponents:
            return ["No other Liga member is online right now."]
        # A player outside the Liga hears how to join, since their games with these would not
        # count.
        listed = ", ".join(f"{member.name} {member.rating}" for member in opponents)
        lines = [f"Liga members online: {listed}."]
        if "Liga" not in ladder_labels(self._database, sender.player_id):
            lines.append(f"You have not joined the Liga yet: {COMMAND_PREFIX}ladder join.")
        return lines

    def _results(self, sender: CommandSender) -> list[str]:
        """/results: the player's own last Liga results and how each moved their rating."""
        # Only the player's own results, newest first, each with its rating change.
        results = self._ladder.own_results(sender.player_id, OWN_RESULT_COUNT)
        if not results:
            return ["You have no counted Liga result yet."]
        # "won vs Bob +16 (02 Oct)" for each, in one line.
        parts = []
        for result in results:
            outcome = "won vs" if result.has_won else "lost vs"
            parts.append(f"{outcome} {result.opponent} {result.change:+d} ({result.played_at:%d %b})")
        return ["Your last results: " + ", ".join(parts) + "."]

    def _versus(self, sender: CommandSender, name: str) -> list[str]:
        """/vs NAME: the player's own counted Liga record against one opponent."""
        # The opponent by name, then the player's own record against them.
        if not name:
            return [f"Name your opponent: {COMMAND_PREFIX}vs NAME."]
        opponent = self._database.find_account(name)
        if opponent is None:
            return [f"There is no account named {name}."]
        # No counted match between them yet, or the record with the last match.
        record = self._ladder.head_to_head(sender.player_id, opponent.account_id)
        if record.last_played_at is None:
            return [f"You have no counted Liga result against {opponent.name} yet."]
        last = "won" if record.has_won_last else "lost"
        return [
            f"You against {opponent.name}: won {record.won}, lost {record.lost}. "
            f"Last match {record.last_played_at:%d %b %Y}: {last}."
        ]

    def _pending(self, sender: CommandSender) -> list[str]:
        """/pending: the player's ladder games without a final result yet, so nobody waits for a
        result line that cannot come yet."""
        # Games waiting for reports, and disputed ones waiting for an admin.
        games = self._ladder.pending_games(sender.player_id)
        if not games:
            return ["None of your ladder games is waiting for a result."]
        return [f"{game.game_name}: {game.state}." for game in games[:PENDING_GAME_COUNT]]

    def _online_account_ids(self) -> list[int]:
        """The account ids of the players online, from their names."""
        online = {name.casefold() for name in self._online_player_names()}
        return [account.account_id for account in self._database.list_accounts() if account.name.casefold() in online]

    def _clan_rank(self, sender: CommandSender, name: str) -> list[str]:
        """/clanrank [CLAN]: a clan's Clanliga rating and place, and its strength from Liga."""
        clan = self._database.find_clan(name) if name else self._database.find_clan_of_account(sender.player_id)
        if clan is None:
            if name:
                return [f"There is no clan named {name}."]
            return ["You are in no clan; name one: /clanrank CLAN."]
        lines = []
        rows = self._ladder.standing_rows(INFINITE_SEASON, CLANLIGA)
        row = row_of(rows, clan.clan_id)
        if row is None:
            lines.append(f"{clan.name} has no Clanliga result yet.")
        else:
            lines.append(f"{clan.name}: Clanliga {row.rating}, place {row.place} of {len(rows)}.")
            lines.append(f"Won {row.won}, lost {row.lost}.")
        strength = next((item for item in self._ladder.clan_strength_rows() if item.clan_id == clan.clan_id), None)
        if strength is None or strength.strength is None:
            lines.append("No strength from Liga yet: it needs three established members who played Liga lately.")
        else:
            members = ", ".join(member_name for member_name, _ in strength.members)
            lines.append(f"Strength from Liga {strength.strength} ({members}).")
        return lines

    def _seasons(self) -> list[str]:
        """/seasons: the custom seasons running now, and until when."""
        running = self._running_custom_seasons()
        if not running:
            return ["No custom season runs now; the infinite season always does."]
        return [f"Running: {season.name} ({season.counts.value}), {season_end_text(season)}." for season in running]

    def _running_custom_seasons(self) -> list[Season]:
        """The custom seasons running now, by their start."""
        now = self._database.now()
        seasons = [season for season in self._database.list_ladder_seasons() if season.is_running(now)]
        return sorted(seasons, key=lambda season: season.starts_at)

    def _announce_for(self, sender: CommandSender, text: str) -> list[str]:
        """/announce TEXT: after /confirm, send TEXT as a server line to every player in the lobby."""
        if not self._is_admin_now(sender):
            return ["Only admins may announce."]
        if not text:
            return [f"Usage: {COMMAND_PREFIX}announce TEXT"]

        def announce() -> list[str]:
            """Send the announcement and report how many players it reached."""
            reached_count = self._announce(text)
            return [f"Announced to {reached_count} player(s)."]

        return self._ask_for_confirmation(sender, "announce this to everyone", announce)

    def _kick_for(self, sender: CommandSender, argument: str) -> list[str]:
        """/kick NAME [REASON]: after /confirm, remove an online player from the lobby; they may log on
        again. An admin cannot kick themselves."""
        if not self._is_admin_now(sender):
            return ["Only admins may kick."]
        online = {name.casefold(): name for name in self._online_player_names()}
        name, reason = split_leading_name(argument, lambda candidate: candidate.casefold() in online)
        if not name:
            return [f"Usage: {COMMAND_PREFIX}kick NAME [REASON], NAME a player who is online"]
        account = self._database.find_account(name)
        if account is None:
            return [f"There is no account named {name}."]
        if account.account_id == sender.player_id:
            return ["You cannot kick yourself."]

        def kick() -> list[str]:
            """Remove the player, or say they went offline since the command was given."""
            if not self._kick(account.account_id, kick_reason(reason)):
                return [f"{account.name} is not online."]
            return [f"Removed {account.name} from the lobby."]

        return self._ask_for_confirmation(sender, f"remove {account.name} from the lobby", kick)

    def _ban_for(self, sender: CommandSender, argument: str) -> list[str]:
        """The ban is stored like one from the command line; the running server then removes the
        player within a second if they are online, through the kick queue the ban fills."""
        if not self._is_admin_now(sender):
            return ["Only admins may ban."]
        usage = [f"Usage: {COMMAND_PREFIX}ban NAME HOURS [REASON], HOURS from {MIN_BAN_HOURS} to {MAX_BAN_HOURS}"]
        name, rest = split_leading_name(argument, lambda candidate: self._database.find_account(candidate) is not None)
        # After the name come HOURS and then the optional reason, which may hold spaces.
        hours_text, _, reason = rest.partition(" ")
        if not name or not is_whole_number_text(hours_text):
            return usage
        hours = int(hours_text)
        if hours < MIN_BAN_HOURS or hours > MAX_BAN_HOURS:
            return usage
        # split_leading_name only accepted a name that has an account, but the account may have been
        # deleted since (by another admin or the command line).
        account = self._database.find_account(name)
        if account is None:
            return usage
        if account.account_id == sender.player_id:
            return ["You cannot ban yourself."]

        def ban() -> list[str]:
            """Store the ban, or report why the database refused it."""
            until = utc_now() + timedelta(hours=hours)
            try:
                self._database.ban_account(account.account_id, until, reason.strip())
            except DatabaseError as error:
                return [f"Not banned: {error}."]
            return [f"{account.name} is banned for {hours} hour(s)."]

        return self._ask_for_confirmation(sender, f"ban {account.name} for {hours} hour(s)", ban)


def row_of(rows: list[StandingRow], side_id: int) -> StandingRow | None:
    """The ranking line of one player or clan, None when it has no place."""
    for row in rows:
        if row.side_id == side_id:
            return row
    return None


def season_end_text(season: Season) -> str:
    """When a season ends, for a chat line: its last day, or that an admin ends it."""
    if season.ends_at is None:
        return "until an admin ends it"
    last_day = season.ends_at - timedelta(seconds=1)
    return f"until {last_day:%d %b %Y}"


def split_command(command_text: str) -> tuple[str, str]:
    """("announce", "Hello all") for "/announce Hello all"; the slash is optional."""
    stripped = command_text.strip().removeprefix(COMMAND_PREFIX)
    command_name, _, argument = stripped.partition(" ")
    return command_name.lower(), argument.strip()


def describe_network(address: Address, reachability: Reachability = Reachability.NOT_CHECKED) -> list[str]:
    """What the server can tell a player about their connection. Chat goes straight between the
    players, and most home routers drop data from someone the player has not written to yet: a
    player who forwards the game's port receives everyone's chat from the first line on.

    The game binds UDP 3343, or the next free port (0x5c4b55). A different port as the server sees
    it means a router rewrote it, so others cannot know the way in without a forwarding."""
    host, port = address
    lines = [f"The server sees you at {host}:{port}."]
    # A loopback or LAN player: router and port advice does not apply, as the server rewrites the
    # address others see (see addresses.py).
    if not ipaddress.ip_address(host).is_global:
        lines.append(
            "You are in the server's own network: the server shows you to others at an address they can reach."
        )
        return lines
    if port != GAME_CLIENT_FIRST_PORT:
        lines.append(
            f"Your router changed the game's port {GAME_CLIENT_FIRST_PORT} to {port}: chat from players you have "
            f"not written to may not reach you. Forward UDP port {GAME_CLIENT_FIRST_PORT} to this PC to fix it."
        )
    else:
        lines.append(
            f"Your router kept port {GAME_CLIENT_FIRST_PORT}. If chat from others does not arrive until you write "
            f"to them, forward UDP port {GAME_CLIENT_FIRST_PORT} to this PC."
        )
    lines.append(
        f"To host games, also forward TCP {DIRECTPLAY_ENUMERATION_PORT} and TCP and UDP "
        f"{DIRECTPLAY_FIRST_GAME_PORT} to {DIRECTPLAY_LAST_GAME_PORT}."
    )
    probe_result = REACHABILITY_ANSWERS.get(reachability)
    if probe_result is not None:
        lines.append(probe_result)
    return lines


def kick_reason(reason: str) -> str:
    """What a kicked player is told, with the admin's reason when there is one."""
    return with_reason(KICK_REASON_PREFIX, reason)


def split_leading_name(argument: str, is_name: Callable[[str], bool]) -> tuple[str, str]:
    """("Roman Legion", "spamming") for "Roman Legion spamming" when is_name accepts "Roman
    Legion": the longest run of leading words that is_name accepts, and the rest. ("", argument)
    when no run is a name."""
    words = argument.split()
    # From the most words down, so "Roman Legion" wins over a player named "Roman".
    for word_count in range(len(words), 0, -1):
        candidate = " ".join(words[:word_count])
        if is_name(candidate):
            return candidate, " ".join(words[word_count:])
    return "", argument


def fit_answer(lines: list[str]) -> tuple[str, ...]:
    """Wrap long lines and cap the answer's length."""
    wrapped = []
    for line in lines:
        # textwrap.wrap returns nothing for an empty line; the empty line is kept as a gap.
        wrapped.extend(textwrap.wrap(line, MAX_ANSWER_LINE_LENGTH) or [""])
    return tuple(wrapped[:MAX_ANSWER_LINES])
