"""Chat commands players type into the lobby chat, answered by the server.

The client handles a few slash commands itself (/MSG, /WHISPER, /IGNORE, /UNIGNORE, /CHATLOG,
parser 0x5c67a0) and sends every other one to the server as a ChatCommand (0x22, from 0x5c859c).
The answer, a ChatCommandAnswer (0x23), shows in the chat as "[Server]: line" (0x44a43c).

Commands:
    /help               what this server offers
    /who                who is online
    /clan               your clan and which of its members are online
    /announce TEXT      (admins) a line for every player in the lobby
    /kick NAME [REASON]          (admins) remove a player from the lobby; they may log on again
    /ban NAME HOURS [REASON]     (admins) bar an account, online or not; an online player is removed

Player names may hold spaces ("Roman Legion"), so the name is the longest run of words at the
start of the argument that names a player.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

from s3lobby.database import LobbyDatabase, utc_now

COMMAND_PREFIX = "/"
# Answers stay short: the chat shows each line on its own, and a long answer would push the
# conversation off the screen.
MAX_ANSWER_LINE_LENGTH = 100
MAX_ANSWER_LINES = 12
# A ban from the chat lasts from one hour up to ten years, the same range the web interface allows.
MIN_BAN_HOURS = 1
MAX_BAN_HOURS = 10 * 365 * 24
KICK_REASON_PREFIX = "An admin removed you from the lobby"


class CommandSender(Protocol):
    player_id: int
    name: str
    is_admin: bool


class ChatCommands:
    """Answers chat commands. online_player_names lists who is in the lobby right now; announce
    sends a line to every player and returns how many it reached; kick removes the player with
    that id from the lobby, telling them the reason, and returns False when they are not online."""

    def __init__(
        self,
        database: LobbyDatabase,
        online_player_names: Callable[[], list[str]],
        announce: Callable[[str], int],
        kick: Callable[[int, str], bool],
    ) -> None:
        self._database = database
        self._online_player_names = online_player_names
        self._announce = announce
        self._kick = kick

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
            case "announce":
                lines = self._announce_for(sender, argument)
            case "kick":
                lines = self._kick_for(sender, argument)
            case "ban":
                lines = self._ban_for(sender, argument)
            case _:
                lines = [f"Unknown command {COMMAND_PREFIX}{command_name}. Type {COMMAND_PREFIX}help for the list."]
        return fit_answer(lines)

    def _help(self, sender: CommandSender) -> list[str]:
        lines = [
            "/who: who is online",
            "/clan: your clan and its members online",
        ]
        if sender.is_admin:
            lines.append("/announce TEXT: a line for every player in the lobby")
            lines.append("/kick NAME [REASON]: remove a player from the lobby")
            lines.append("/ban NAME HOURS [REASON]: bar an account, online or not")
        return lines

    def _who(self) -> list[str]:
        names = sorted(self._online_player_names(), key=str.casefold)
        return [f"{len(names)} player(s) online: {', '.join(names)}"]

    def _clan(self, sender: CommandSender) -> list[str]:
        clan = self._database.find_clan_of_account(sender.player_id)
        if clan is None:
            return ["You are in no clan."]
        online = {name.casefold() for name in self._online_player_names()}
        members = self._database.list_clan_member_names(clan.clan_id)
        online_members = [name for name in members if name.casefold() in online]
        return [f"Clan {clan.name}: {len(members)} member(s), online: {', '.join(online_members) or 'nobody'}"]

    def _announce_for(self, sender: CommandSender, text: str) -> list[str]:
        if not sender.is_admin:
            return ["Only admins may announce."]
        if not text:
            return [f"Usage: {COMMAND_PREFIX}announce TEXT"]
        reached_count = self._announce(text)
        return [f"Announced to {reached_count} player(s)."]

    def _kick_for(self, sender: CommandSender, argument: str) -> list[str]:
        if not sender.is_admin:
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
        if not self._kick(account.account_id, kick_reason(reason)):
            return [f"{account.name} is not online."]
        return [f"Removed {account.name} from the lobby."]

    def _ban_for(self, sender: CommandSender, argument: str) -> list[str]:
        """The ban is stored like one from the command line; the running server then removes the
        player within a second if they are online, through the kick queue the ban fills."""
        if not sender.is_admin:
            return ["Only admins may ban."]
        usage = [f"Usage: {COMMAND_PREFIX}ban NAME HOURS [REASON], HOURS from {MIN_BAN_HOURS} to {MAX_BAN_HOURS}"]
        name, rest = split_leading_name(argument, lambda candidate: self._database.find_account(candidate) is not None)
        hours_text, _, reason = rest.partition(" ")
        if not name or not hours_text.isdigit():
            return usage
        hours = int(hours_text)
        if hours < MIN_BAN_HOURS or hours > MAX_BAN_HOURS:
            return usage
        account = self._database.find_account(name)
        if account.account_id == sender.player_id:
            return ["You cannot ban yourself."]
        until = utc_now() + timedelta(hours=hours)
        self._database.ban_account(account.account_id, until, reason.strip())
        return [f"{account.name} is banned for {hours} hour(s)."]


def split_command(command_text: str) -> tuple[str, str]:
    """("announce", "Hello all") for "/announce Hello all"; the slash is optional."""
    stripped = command_text.strip().removeprefix(COMMAND_PREFIX)
    command_name, _, argument = stripped.partition(" ")
    return command_name.lower(), argument.strip()


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
