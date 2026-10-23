"""Chat commands players type into the lobby chat, answered by the server.

The client handles a few slash commands itself (/MSG, /WHISPER, /IGNORE, /UNIGNORE, /CHATLOG,
parser 0x5c67a0) and sends every other one to the server as a ChatCommand (0x22, from 0x5c859c).
The answer, a ChatCommandAnswer (0x23), shows in the chat as "[Server]: line" (0x44a43c).

Commands:
    /help               what this server offers
    /who                who is online
    /clan               your clan and which of its members are online
    /announce TEXT      (admins) a line for every player in the lobby
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from typing import Protocol

from s3lobby.database import LobbyDatabase

COMMAND_PREFIX = "/"
# Answers stay short: the chat shows each line on its own, and a long answer would push the
# conversation off the screen.
MAX_ANSWER_LINE_LENGTH = 100
MAX_ANSWER_LINES = 12


class CommandSender(Protocol):
    player_id: int
    name: str
    is_admin: bool


class ChatCommands:
    """Answers chat commands. online_player_names lists who is in the lobby right now; announce
    sends a line to every player and returns how many it reached."""

    def __init__(
        self,
        database: LobbyDatabase,
        online_player_names: Callable[[], list[str]],
        announce: Callable[[str], int],
    ) -> None:
        self._database = database
        self._online_player_names = online_player_names
        self._announce = announce

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


def split_command(command_text: str) -> tuple[str, str]:
    """("announce", "Hello all") for "/announce Hello all"; the slash is optional."""
    stripped = command_text.strip().removeprefix(COMMAND_PREFIX)
    command_name, _, argument = stripped.partition(" ")
    return command_name.lower(), argument.strip()


def fit_answer(lines: list[str]) -> tuple[str, ...]:
    """Wrap long lines and cap the answer's length."""
    wrapped = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, MAX_ANSWER_LINE_LENGTH) or [""])
    return tuple(wrapped[:MAX_ANSWER_LINES])
