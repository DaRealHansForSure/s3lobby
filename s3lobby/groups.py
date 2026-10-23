"""Groups: what they are, and the checks a group from a client must pass before others see it.

In this protocol rooms, zones, chat groups and games are all groups. Three kinds matter to the
server:

- rooms: the four lobby tabs and the nine game zones (ids the client chooses on its own), and the
  groups the server offers inside tabs, such as chat themes. They exist as long as the server runs.
- games: opened by a host with a game block (the host's address and DirectPlay session), listed in
  one or more zones, closed when the last player left.
- chat groups: opened by a player inside a chat theme, optionally with a password, closed when the
  last player left.

A group is "listed" in a parent when players in that parent see it in their list. Every group a
client sends to be shown to others goes through check_client_group_texts first: the other players
run a game client from 1998, and nothing oversized or unexpected reaches them through this server.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from s3lobby.constants import (
    GROUP_COUNTERS_FULL,
    JOINED_GROUP_PARENT_ID,
    MAX_GROUP_DESCRIPTION_BYTES,
    MAX_GROUP_TEXT_LENGTH,
    UNLIMITED_PLAYERS,
    GroupFlag,
    MessageType,
)
from s3lobby.messages import GameBlock, GameDescription, GameSettings, GroupAnnouncement, GroupCounters, GroupUpdate
from s3lobby.wire import Address, ProtocolError

# What a host's game reports as its own address before the server fills in the real one: the
# client binds its socket to INADDR_ANY and so only knows itself as 0.0.0.0.
UNKNOWN_HOST = "0.0.0.0"
FIRST_PRINTABLE_CHARACTER = " "


class GroupKind(Enum):
    ROOM = "room"
    GAME = "game"
    CHAT_GROUP = "chat group"


@dataclass
class Group:
    """One group and who is in it.

    The announcement is what other clients are shown, always with an empty third text: for a chat
    group that text is the password, which the server keeps separately and never passes on.
    """

    announcement: GroupAnnouncement
    kind: GroupKind
    member_ids: list[int] = field(default_factory=list)
    listed_in_parent_ids: set[int] = field(default_factory=set)
    password: str = ""
    creator_player_id: int | None = None
    game_record_id: int | None = None
    # Players of a ladder game who were told that this server keeps no ladder (each hears it once).
    ladder_notice_player_ids: set[int] = field(default_factory=set)

    @property
    def group_id(self) -> int:
        return self.announcement.group_id

    @property
    def is_room(self) -> bool:
        return self.kind == GroupKind.ROOM

    @property
    def is_full(self) -> bool:
        max_players = self.announcement.max_players
        return max_players != UNLIMITED_PLAYERS and max_players > 0 and len(self.member_ids) >= max_players

    def counters(self) -> GroupCounters:
        status_flags = GROUP_COUNTERS_FULL if self.is_full else 0
        return GroupCounters(
            group_id=self.group_id,
            player_count=len(self.member_ids),
            max_players=self.announcement.max_players,
            status_flags=status_flags,
        )


def game_settings_of(announcement: GroupAnnouncement) -> GameSettings | None:
    """The settings in a group's game block; None for a group without one, or with one that does
    not decode (a client's bytes are passed on as they are, not trusted to be well formed)."""
    if announcement.game_block is None:
        return None
    try:
        return GameSettings.from_game_block(announcement.game_block)
    except ProtocolError:
        return None


def game_description_of(announcement: GroupAnnouncement) -> GameDescription | None:
    """The game description a host keeps in its game; None when there is none or it does not
    decode."""
    if not announcement.description:
        return None
    try:
        return GameDescription.decode(announcement.description)
    except ProtocolError:
        return None


def is_ladder_game(announcement: GroupAnnouncement) -> bool:
    """True when the host set the game up as a ladder game. The description is current: the host
    rewrites it whenever the setup changes (UpdateGameSettings 0x454db0); the game block only holds
    the choice made when the game was opened."""
    description = game_description_of(announcement)
    if description is not None:
        return description.is_ladder_game
    settings = game_settings_of(announcement)
    return settings is not None and settings.is_ladder_game


def room_announcement(group_id: int, name: str, flags: int = 0, description_text: str = "") -> GroupAnnouncement:
    """The announcement of a room the server offers. description_text fills the second text, which
    chat theme lists show in their Description column."""
    return GroupAnnouncement(
        message_type=MessageType.ADD_GROUP_TO_GROUP,
        parent_id=JOINED_GROUP_PARENT_ID,
        group_id=group_id,
        flags=flags,
        max_players=0,
        name=name,
        second_text=description_text,
        third_text="",
    )


def kind_of_new_group(announcement: GroupAnnouncement) -> GroupKind:
    """A group a player opens is a game when it carries a game block, else a chat group."""
    if announcement.flags & GroupFlag.HAS_GAME_BLOCK:
        return GroupKind.GAME
    return GroupKind.CHAT_GROUP


def with_real_host_address(announcement: GroupAnnouncement, host_client_address: Address) -> GroupAnnouncement:
    """The announcement with the host's real IP in its game block, if it has one."""
    if announcement.game_block is None:
        return announcement
    return replace(announcement, game_block=game_block_with_real_host(announcement.game_block, host_client_address))


def game_block_with_real_host(game_block: GameBlock, host_client_address: Address) -> GameBlock:
    """A host announces its game with its own address, which reads 0.0.0.0 on the host. Put in the
    IP the server sees the host at, so other players can connect DirectPlay to it, and keep the
    host's port."""
    host, port = game_block.host_address
    if host != UNKNOWN_HOST:
        return game_block
    real_host_ip = host_client_address[0]
    return replace(game_block, host_address=(real_host_ip, port))


def check_client_group_texts(group: GroupAnnouncement | GroupUpdate) -> None:
    """Raise ProtocolError when a group from a client carries text the server will not pass on."""
    for label, text in (("name", group.name), ("second text", group.second_text), ("third text", group.third_text)):
        check_client_text(label, text)
    if len(group.description) > MAX_GROUP_DESCRIPTION_BYTES:
        raise ProtocolError(f"group description of {len(group.description)} bytes")


def check_client_text(label: str, text: str) -> None:
    if len(text) > MAX_GROUP_TEXT_LENGTH:
        raise ProtocolError(f"{label} of {len(text)} characters")
    has_control_character = any(character < FIRST_PRINTABLE_CHARACTER for character in text)
    if has_control_character:
        raise ProtocolError(f"{label} contains control characters")
