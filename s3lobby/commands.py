"""Chat commands players type into the lobby chat, answered by the server.

The client handles a few slash commands itself (/MSG, /WHISPER, /IGNORE, /UNIGNORE, /CHATLOG,
parser 0x5c67a0) and sends every other one to the server as a ChatCommand (0x22, from 0x5c859c).
The answer, a ChatCommandAnswer (0x23), shows in the chat as "[Server]: line" (0x44a43c).

Commands:
    /help               what this server offers
    /who                who is online
    /clan               your clan and which of its members are online
    /net                the address the server sees you at, and what to forward on your router
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
    MAX_BAN_HOURS,
    MIN_BAN_HOURS,
)
from s3lobby.database import DatabaseError, LobbyDatabase, is_whole_number_text, utc_now
from s3lobby.reachability import Reachability
from s3lobby.wire import Address

COMMAND_PREFIX = "/"
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

    def answer(self, sender: CommandSender, command_text: str) -> tuple[str, ...]:
        """The lines that answer one command."""
        command_name, argument = split_command(command_text)
        match command_name:
            case "help":
                lines = self._help(sender)
            case "who":
                lines = self._who()
            case "clan":
                lines = self._clan(sender)
            case "net":
                lines = describe_network(sender.address, self._reachability_of(sender.address))
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
        lines = [
            "/who: who is online",
            "/clan: your clan and its members online",
            "/net: your address as the server sees it, and what to forward on your router",
        ]
        if self._is_admin_now(sender):
            lines.append("/announce TEXT: a line for every player in the lobby")
            lines.append("/kick NAME [REASON]: remove a player from the lobby")
            lines.append("/ban NAME HOURS [REASON]: bar an account, online or not")
            lines.append("/confirm CODE: carry out the command you just gave")
        return lines

    def _ask_for_confirmation(self, sender: CommandSender, description: str, action: Callable[[], list[str]]) -> list[str]:
        """Hold an admin command until the admin confirms it with a random code. A datagram forged
        from an admin's address is sent blind: its sender never sees the code, and a wrong one
        cancels the command, so guessing does not work either."""
        code = f"{secrets.randbelow(10 ** CONFIRMATION_CODE_DIGITS):0{CONFIRMATION_CODE_DIGITS}d}"
        expires_at = self._clock_seconds() + CONFIRMATION_SECONDS
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
        hours_text, _, reason = rest.partition(" ")
        if not name or not is_whole_number_text(hours_text):
            return usage
        hours = int(hours_text)
        if hours < MIN_BAN_HOURS or hours > MAX_BAN_HOURS:
            return usage
        account = self._database.find_account(name)
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
    if not ipaddress.ip_address(host).is_global:
        lines.append("You are in the server's own network: the server shows you to others at an address they can reach.")
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
    if reason:
        return f"{KICK_REASON_PREFIX}: {reason}"
    return f"{KICK_REASON_PREFIX}."


def split_leading_name(argument: str, is_name: Callable[[str], bool]) -> tuple[str, str]:
    """("Roman Legion", "spamming") for "Roman Legion spamming" when is_name accepts "Roman
    Legion": the longest run of leading words that is_name accepts, and the rest. ("", argument)
    when no run is a name."""
    words = argument.split()
    for word_count in range(len(words), 0, -1):
        candidate = " ".join(words[:word_count])
        if is_name(candidate):
            return candidate, " ".join(words[word_count:])
    return "", argument


def fit_answer(lines: list[str]) -> tuple[str, ...]:
    """Wrap long lines and cap the answer's length."""
    wrapped = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, MAX_ANSWER_LINE_LENGTH) or [""])
    return tuple(wrapped[:MAX_ANSWER_LINES])
