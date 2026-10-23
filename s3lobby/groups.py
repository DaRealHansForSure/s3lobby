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
    ALL_LANGUAGES,
    BLUE_BYTE_CLAN_NAME,
    FIRST_PRINTABLE_CHARACTER,
    GAME_ROW_BLUE,
    GAME_ROW_GREEN,
    GROUP_COUNTERS_FULL,
    JOINED_GROUP_PARENT_ID,
    MAX_GROUP_DESCRIPTION_BYTES,
    MAX_GROUP_TEXT_LENGTH,
    UNLIMITED_PLAYERS,
    GroupFlag,
    LadderType,
    MessageType,
)
from s3lobby.messages import GameBlock, GameDescription, GameSettings, GroupAnnouncement, GroupCounters, GroupUpdate
from s3lobby.wire import Address, ProtocolError

# What a host's game reports as its own address before the server fills in the real one: the
# client binds its socket to INADDR_ANY and so only knows itself as 0.0.0.0.
UNKNOWN_HOST = "0.0.0.0"


class GroupKind(Enum):
    """The three kinds of group the server tells apart: they differ in who opens them and when they close."""

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
    # Players already recorded as having joined this game (each is recorded once).
    recorded_player_ids: set[int] = field(default_factory=set)
    # When the last member of a chat group left (time.monotonic()); None while it has members.
    empty_since_seconds: float | None = None
    # Players of a ladder game who were told that this server keeps no ladder (each hears it once).
    ladder_notice_player_ids: set[int] = field(default_factory=set)
    # The game installations of the players who joined, by serial number: each serial gets a number
    # the first time a player with it joins, so the ladder can tell two copies of one installation
    # apart without the serial numbers ever being stored.
    installation_by_serial: dict[str, int] = field(default_factory=dict)
    # A chat theme for the games of one language is shown only to them; ALL_LANGUAGES for every game.
    language: str = ALL_LANGUAGES

    def installation_of(self, serial_number: str) -> int:
        """The number of the game installation with this serial number within this game."""
        if serial_number not in self.installation_by_serial:
            self.installation_by_serial[serial_number] = len(self.installation_by_serial) + 1
        return self.installation_by_serial[serial_number]

    @property
    def group_id(self) -> int:
        """The group's id, as its announcement carries it."""
        return self.announcement.group_id

    @property
    def is_room(self) -> bool:
        """True for a group the server keeps for as long as it runs (tabs, zones, themes)."""
        return self.kind == GroupKind.ROOM

    @property
    def is_full(self) -> bool:
        """True when the group has a player limit and reached it; a limit of 0 or UNLIMITED_PLAYERS means
        there is none."""
        max_players = self.announcement.max_players
        # Rooms are announced with 0 and chat groups with UNLIMITED_PLAYERS; neither is ever full.
        return max_players != UNLIMITED_PLAYERS and max_players > 0 and len(self.member_ids) >= max_players

    def counters(self) -> GroupCounters:
        """The GroupCounters that show viewers in a parent how many players the group has and whether
        it is full."""
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


def ladder_type_of(announcement: GroupAnnouncement) -> int | None:
    """The ladder a game is now (LadderType INDIVIDUAL or CLANS), None for a normal game.

    The description says whether the game is a ladder game now (the host rewrites it on every
    change, 0x454db0); only the game block says which ladder, and it keeps the choice made when the
    game was opened (docs/protocol.md). A game opened as a normal game and switched to a ladder
    game later has a block naming none; it counts as Liga (inferred: Liga is the ladder the zone
    "League 1 vs 1" is for). A host switching between Liga and Clanliga after opening cannot be
    seen; the Clanliga rules then void a game that was not one."""
    if not is_ladder_game(announcement):
        return None
    settings = game_settings_of(announcement)
    if settings is None or not settings.is_ladder_game:
        return LadderType.INDIVIDUAL
    return settings.ladder_type


def room_announcement(group_id: int, name: str, flags: int = 0, description_text: str = "") -> GroupAnnouncement:
    """The announcement of a room the server offers. description_text fills the second text, which
    chat theme lists show in their Description column."""
    # The parent is only a placeholder: every send puts the room under the right parent with
    # under_parent. A maximum of 0 means no player limit (see Group.is_full).
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


def game_row_colour(is_highlighted: bool, clan_name: str) -> str:
    """The colour the game draws a member's row in: none without the HIGHLIGHTED flag, blue for
    the clan BB, green otherwise (0x449da1, 0x449de2). Empty for the usual colour."""
    if not is_highlighted:
        return ""
    if clan_name == BLUE_BYTE_CLAN_NAME:
        return GAME_ROW_BLUE
    return GAME_ROW_GREEN


def stored_game_announcement(announcement: GroupAnnouncement) -> bytes:
    """A game as the database keeps it for the Games page: the announcement's body with the host
    address zeroed, since the page has no use for players' IP addresses. The password is never in
    a stored announcement (the third text is always empty there)."""
    if announcement.game_block is not None:
        anonymous_block = replace(announcement.game_block, host_address=(UNKNOWN_HOST, 0))
        announcement = replace(announcement, game_block=anonymous_block)
    return announcement.encode().body


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
    # The description is the host's encoded GameDescription, so it is limited in bytes, not characters.
    if len(group.description) > MAX_GROUP_DESCRIPTION_BYTES:
        raise ProtocolError(f"group description of {len(group.description)} bytes")


def check_client_text(label: str, text: str) -> None:
    """Raise ProtocolError for a text that is longer than the server passes on or holds control
    characters; label names the text in the error."""
    if len(text) > MAX_GROUP_TEXT_LENGTH:
        raise ProtocolError(f"{label} of {len(text)} characters")
    # Compares code points: every character below the space is a control character.
    has_control_character = any(character < FIRST_PRINTABLE_CHARACTER for character in text)
    if has_control_character:
        raise ProtocolError(f"{label} contains control characters")
