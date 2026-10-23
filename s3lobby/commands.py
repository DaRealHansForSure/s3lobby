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

Player names may hold spaces ("Roman Legion"), so the name is the longest run of words at the
start of the argument that names a player.
"""

from __future__ import annotations

import ipaddress
import textwrap
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

from s3lobby.constants import (
    DIRECTPLAY_ENUMERATION_PORT,
    DIRECTPLAY_FIRST_GAME_PORT,
    DIRECTPLAY_LAST_GAME_PORT,
    GAME_CLIENT_FIRST_PORT,
)
from s3lobby.database import LobbyDatabase, utc_now
from s3lobby.reachability import Reachability
from s3lobby.wire import Address

COMMAND_PREFIX = "/"
# Answers stay short: the chat shows each line on its own, and a long answer would push the
# conversation off the screen.
MAX_ANSWER_LINE_LENGTH = 100
MAX_ANSWER_LINES = 12
# A ban from the chat lasts from one hour up to ten years, the same range the web interface allows.
MIN_BAN_HOURS = 1
MAX_BAN_HOURS = 10 * 365 * 24
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
    player_id: int
    name: str
    address: Address


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
    ) -> None:
        self._database = database
        self._online_player_names = online_player_names
        self._announce = announce
        self._kick = kick
        self._reachability_of = reachability_of

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
            case _:
                lines = [f"Unknown command {COMMAND_PREFIX}{command_name}. Type {COMMAND_PREFIX}help for the list."]
        return fit_answer(lines)

    def _help(self, sender: CommandSender) -> list[str]:
        lines = [
            "/who: who is online",
            "/clan: your clan and its members online",
            "/net: your address as the server sees it, and what to forward on your router",
        ]
        if self._is_admin_now(sender):
            lines.append("/announce TEXT: a line for every player in the lobby")
            lines.append("/kick NAME [REASON]: remove a player from the lobby")
            lines.append("/ban NAME HOURS [REASON]: bar an account, online or not")
        return lines

    def _is_admin_now(self, sender: CommandSender) -> bool:
        """Admin rights as the database has them now: an admin whose rights were taken away while
        online must not keep using /kick, /ban and /announce until the next logon."""
        account = self._database.find_account_by_id(sender.player_id)
        return account is not None and account.is_admin

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
        if not self._is_admin_now(sender):
            return ["Only admins may announce."]
        if not text:
            return [f"Usage: {COMMAND_PREFIX}announce TEXT"]
        reached_count = self._announce(text)
        return [f"Announced to {reached_count} player(s)."]

    def _kick_for(self, sender: CommandSender, argument: str) -> list[str]:
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
        if not self._kick(account.account_id, kick_reason(reason)):
            return [f"{account.name} is not online."]
        return [f"Removed {account.name} from the lobby."]

    def _ban_for(self, sender: CommandSender, argument: str) -> list[str]:
        """The ban is stored like one from the command line; the running server then removes the
        player within a second if they are online, through the kick queue the ban fills."""
        if not self._is_admin_now(sender):
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
