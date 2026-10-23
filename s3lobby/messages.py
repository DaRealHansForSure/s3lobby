"""Every message the server reads or writes, as one small class each.

A class knows its type byte, its fields in wire order, and how to turn itself into a RawMessage
(encode) and back (decode). Field orders and sizes come from the game's message constructors and
accessors in S3.EXE; the class docstrings name them. The game asserts many exact sizes when it
reads a message and drops the connection when one fails, so the sizes noted here are hard rules.

Fields whose meaning is not known are still decoded and carried through unchanged, under names
that say they are unknown, so relaying a message never loses information.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import ClassVar, Protocol

from s3lobby.constants import (
    AMAZONS_ALLOWED_BIT,
    GAME_BLOCK_SIZE_BY_MESSAGE_TYPE,
    GAME_DESCRIPTION_MESSAGE_TYPE,
    GAME_LANGUAGES,
    GROUP_ANNOUNCEMENT_MESSAGE_TYPES,
    GROUP_UPDATE_MESSAGE_TYPES,
    SESSION_GUID_SIZE,
    SOCKET_ADDRESS_SIZE,
    U32_SIZE,
    NO_WINNING_TEAM,
    GameStatusFlag,
    GroupFlag,
    GroupUpdateMask,
    LadderType,
    MatchPlayerFlag,
    MessageType,
)
from s3lobby.wire import Address, FieldReader, FieldWriter, ProtocolError, RawMessage, decode_block


class EncodableMessage(Protocol):
    """Anything the server can queue for a client: it only has to turn itself into bytes."""

    def encode(self) -> RawMessage: ...


def open_message_body(message: RawMessage, accepted_types: Iterable[int]) -> FieldReader:
    """A reader for the body, after checking the message is one of the types a class decodes."""
    if message.message_type not in accepted_types:
        raise ProtocolError(f"cannot decode {message.type_name} here")
    return message.reader()


# Connection layer (protocol.cpp). The reliable layer in connection.py builds and reads these.


@dataclass(frozen=True)
class MessageId:
    """0x02: the number of the block it starts. Only counts as the first message of a block
    (0x601d70). Size exactly 8."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.MESSAGE_ID
    block_id: int

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.block_id).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> MessageId:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(block_id=reader.read_u32())


@dataclass(frozen=True)
class OpenConnection:
    """0xff: opens a connection. Each side sends one in its block number 1; the client only counts
    its connection as open once it receives the server's (0x605ec8). The nonce tells a restarted
    peer from a retransmission: a new nonce means a new connection (0x605eec). Size exactly 8."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.OPEN_CONNECTION
    nonce: int

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.nonce).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> OpenConnection:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(nonce=reader.read_u32())


@dataclass(frozen=True)
class CloseConnection:
    """0x03: ends a connection. No body (constructor 0x603b40)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.CLOSE_CONNECTION

    def encode(self) -> RawMessage:
        return RawMessage(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> CloseConnection:
        open_message_body(message, {cls.MESSAGE_TYPE})
        return cls()


@dataclass(frozen=True)
class AcknowledgeMessages:
    """0x00: the ids of the peer's blocks that arrived. The list may be empty; the reader asserts
    the body is a whole number of u32 values (0x603580). Acknowledging an id the peer never sent
    makes the client drop the connection (0x6051d0)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.ACKNOWLEDGE_MESSAGES
    block_ids: tuple[int, ...] = ()

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        for block_id in self.block_ids:
            writer.write_u32(block_id)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> AcknowledgeMessages:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        block_ids = []
        while reader.remaining_size > 0:
            block_ids.append(reader.read_u32())
        return cls(block_ids=tuple(block_ids))


@dataclass(frozen=True)
class MinMissingAcknowledge:
    """0x01: "I hold your acknowledgements for all my blocks below this id", so the peer can stop
    repeating them (0x605d90). Size exactly 8. The value must never be 0: the game's constructor
    then leaves the body out (0x603880) and the reader's size assert fails (0x6038f0)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.MIN_MISSING_ACKNOWLEDGE
    lowest_unconfirmed_block_id: int

    def encode(self) -> RawMessage:
        if self.lowest_unconfirmed_block_id == 0:
            raise ValueError("MinMissingAcknowledge must not carry 0, the client cannot read it")
        return FieldWriter().write_u32(self.lowest_unconfirmed_block_id).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> MinMissingAcknowledge:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        lowest_unconfirmed_block_id = reader.read_u32()
        if lowest_unconfirmed_block_id == 0:
            raise ProtocolError("MinMissingAcknowledge with 0, which the game never sends")
        return cls(lowest_unconfirmed_block_id=lowest_unconfirmed_block_id)


@dataclass(frozen=True)
class AdjustConnectionTimeout:
    """0xfe: the sender changes how long it waits for silence before giving up, and tells the
    peer. The client sends 600000 (ten minutes) around a running game and 60000 afterwards
    (0x454d6f, 0x452072). Size exactly 8."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.ADJUST_CONNECTION_TIMEOUT
    timeout_ms: int

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.timeout_ms).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> AdjustConnectionTimeout:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(timeout_ms=reader.read_u32())


# Index server role: the patch check and the list of lobby servers.


@dataclass(frozen=True)
class CheckFileVersion:
    """0x11: the client's first question to every configured server (constructor 0x5e8380).

    game_version is 160 in the last release (global 0x6970f0). edition says which add-ons are
    installed: 2 Mission CD, 3 Gold/Plus, 4 both (global 0x7a7ff8). language_index is the game's
    language, an index into GAME_LANGUAGES (configuration offset 0x28, getter 0x4120a0, passed at
    0x43af70; a German game sends 0, seen in a run).
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.CHECK_FILE_VERSION
    game_version: int
    edition: int
    language_index: int

    @property
    def language_name(self) -> str:
        """The language's name, or "language N" for an index the game's text archive does not have."""
        if 0 <= self.language_index < len(GAME_LANGUAGES):
            return GAME_LANGUAGES[self.language_index]
        return f"language {self.language_index}"

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        writer.write_u32(self.game_version).write_u32(self.edition).write_u32(self.language_index)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> CheckFileVersion:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(
            game_version=reader.read_u32(),
            edition=reader.read_u32(),
            language_index=reader.read_u32(),
        )


@dataclass(frozen=True)
class GetLobbyServerList:
    """0x05: the client asks the index server that answered its patch check for the lobby
    servers. No body (constructor 0x5e16f0)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.GET_LOBBY_SERVER_LIST

    def encode(self) -> RawMessage:
        return RawMessage(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> GetLobbyServerList:
        open_message_body(message, {cls.MESSAGE_TYPE})
        return cls()


@dataclass(frozen=True)
class LobbyServerEntry:
    """One lobby server in the list: where to reach it ("host:port") and the name shown."""

    address_text: str
    name: str


@dataclass(frozen=True)
class LobbyServerList:
    """0x06: the lobby servers, as pairs of strings up to the end of the message, with no count in
    front (parser 0x5e1710). An empty list makes the client give up with error 0x1e."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.LOBBY_SERVER_LIST
    servers: tuple[LobbyServerEntry, ...]

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        for server in self.servers:
            writer.write_string(server.address_text).write_string(server.name)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> LobbyServerList:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        servers = []
        while reader.remaining_size > 0:
            servers.append(LobbyServerEntry(address_text=reader.read_string(), name=reader.read_string()))
        return cls(servers=tuple(servers))


@dataclass(frozen=True)
class Result:
    """0x09: an answer code, see constants.ResultCode, plus one value whose use depends on the code
    (a player id for private chat answers, else 0). Size exactly 12 (0x5e1e30, 0x5e2080)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.RESULT
    code: int
    value: int = 0

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.code).write_u32(self.value).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> Result:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(code=reader.read_u32(), value=reader.read_u32())


# Lobby server role: logon.


@dataclass(frozen=True)
class Logon:
    """0x07: a player logs on (constructor 0x5e1d50).

    The game sends name, password and serial number in clear text. It checks nothing in the
    server's answer except the Result code. Of the flags, bits 0 and 2 come from the logon dialog
    and bit 1 is set for edition 1; their meaning is not known.
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.LOGON
    protocol_version: int
    player_name: str
    password: str
    flags: int
    serial_number: str

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        writer.write_u32(self.protocol_version).write_string(self.player_name).write_string(self.password)
        writer.write_u32(self.flags).write_string(self.serial_number)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> Logon:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(
            protocol_version=reader.read_u32(),
            player_name=reader.read_string(),
            password=reader.read_string(),
            flags=reader.read_u32(),
            serial_number=reader.read_string(),
        )

    def __repr__(self) -> str:
        # Keep the password out of logs and tracebacks.
        return (
            f"Logon(protocol_version={self.protocol_version}, player_name={self.player_name!r}, "
            f"flags={self.flags:#x}, serial_number={self.serial_number!r})"
        )


@dataclass(frozen=True)
class Logout:
    """0x0a: the player leaves the lobby. No body; the client closes without waiting (0x5c5e80)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.LOGOUT

    def encode(self) -> RawMessage:
        return RawMessage(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> Logout:
        open_message_body(message, {cls.MESSAGE_TYPE})
        return cls()


@dataclass(frozen=True)
class OwnPlayer:
    """0x0e: tells a client its own player id after the logon (handler 0x5d7300). Size exactly 8.

    The handler asserts the client has no own player yet, so it is sent once per logon. It also
    asserts the sender is the client's server, which only holds after Result LOGON_ACCEPTED: the
    Result has to come first.
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.OWN_PLAYER
    player_id: int

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.player_id).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> OwnPlayer:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(player_id=reader.read_u32())


@dataclass(frozen=True)
class OwnPlayerWithClan:
    """0x24: like OwnPlayer, for a player who logged on as clan member (handler 0x5d7830).

    The client keeps the clan for the games it creates or joins and hands it to the other players
    over DirectPlay (0x470250, "CLAN-Pack"). Same rules as OwnPlayer: after the Result, once.
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.OWN_PLAYER_WITH_CLAN
    player_id: int
    clan_id: int
    clan_name: str

    def encode(self) -> RawMessage:
        writer = FieldWriter().write_u32(self.player_id).write_u32(self.clan_id).write_string(self.clan_name)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> OwnPlayerWithClan:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(player_id=reader.read_u32(), clan_id=reader.read_u32(), clan_name=reader.read_string())


# Groups: rooms, zones and games.


@dataclass(frozen=True)
class GameBlock:
    """The part of a group announcement that makes the group a joinable game (GroupFlag
    HAS_GAME_BLOCK).

    host_address is where the host's game runs; a joining client connects DirectPlay to its IP.
    The remainder is kept exactly as the host wrote it, so relaying a game never changes it;
    GameSettings.from_game_block reads what it holds.
    """

    host_address: Address
    remainder: bytes


@dataclass(frozen=True)
class GameSettings:
    """What a game block says after the host address (constructors 0x5e3b40 and 0x5ea320, values
    from CreateGame 0x454780).

    session_guid is the DirectPlay session a joining client opens (CGroup.GetSessionGuid 0x5db4c0,
    JoinLobbyGame 0x467e00); every recorded game had a new one. game_version is 160 for version
    1.60 (global 0x6970f0). program_checksum is a 64-bit checksum the game computes over its own
    program at start (0x40b417, 0x61b6e0); a client whose checksum differs shows the game with its
    version and refuses to join it (0x44b19d, 0x4436ac).

    allows_amazons and ladder_type exist only in the long block of AddGroupToGroup2 and
    UpdateGroup2 and are None for the short block. ladder_type is a LadderType value, kept as a
    number so that an unexpected value from a client still decodes.
    """

    session_guid: bytes
    game_version: int
    program_checksum: int
    allows_amazons: bool | None = None
    ladder_type: int | None = None

    @property
    def is_ladder_game(self) -> bool:
        return self.ladder_type is not None and self.ladder_type != LadderType.NONE

    @classmethod
    def from_game_block(cls, game_block: GameBlock) -> GameSettings:
        """Raises ProtocolError for a remainder that is neither the short nor the long block."""
        reader = FieldReader(game_block.remainder)
        session_guid = reader.read_bytes(SESSION_GUID_SIZE)
        game_version = reader.read_u32()
        checksum_low = reader.read_u32()
        checksum_high = reader.read_u32()
        settings = cls(session_guid, game_version, (checksum_high << 32) | checksum_low)
        if reader.remaining_size == 0:
            return settings
        amazons_value = reader.read_u32()
        ladder_type = reader.read_u32()
        if reader.remaining_size:
            raise ProtocolError(f"game block with {reader.remaining_size} unexpected bytes")
        return replace(settings, allows_amazons=bool(amazons_value & AMAZONS_ALLOWED_BIT), ladder_type=ladder_type)


@dataclass(frozen=True)
class GameDescription:
    """What a host keeps in its game's description (GroupFlag HAS_DESCRIPTION): a serialized
    CLMGameDescription message (constructor 0x4504e0), written by CreateGame (0x4547cc) and again by
    UpdateGameSettings (0x454df4) whenever the host changes the setup. The game list shows the map
    and the team count from it (0x44b14b..0x44b500).

    Layout: a message header of type GAME_DESCRIPTION_MESSAGE_TYPE and its size, then u32 number of
    teams (the size of the setup's team list, settings +0x50), u32 number of computer opponents
    (slots in state 2, 0x435ca0), u32 1 for a ladder game and 0 otherwise (0x4547ba), string map
    name: the map's name, or a backslash and the random map's name for a random map
    (0x4616da..0x461796). Seen in a run: 2, 0, 1, "\\Random".
    """

    team_count: int
    computer_opponent_count: int
    is_ladder_game: bool
    map_name: str

    def encode(self) -> bytes:
        writer = FieldWriter()
        writer.write_u32(self.team_count).write_u32(self.computer_opponent_count)
        writer.write_u32(int(self.is_ladder_game)).write_string(self.map_name)
        return writer.to_message(GAME_DESCRIPTION_MESSAGE_TYPE).encode()

    @classmethod
    def decode(cls, description: bytes) -> GameDescription:
        """Raises ProtocolError unless the bytes hold exactly one game description message."""
        messages = decode_block(description)
        is_one_description = len(messages) == 1 and messages[0].message_type == GAME_DESCRIPTION_MESSAGE_TYPE
        if not is_one_description:
            raise ProtocolError("the description is not one game description message")
        reader = messages[0].reader()
        team_count = reader.read_u32()
        computer_opponent_count = reader.read_u32()
        ladder_value = reader.read_u32()
        map_name = reader.read_string()
        if reader.remaining_size:
            raise ProtocolError(f"game description with {reader.remaining_size} unexpected bytes")
        return cls(team_count, computer_opponent_count, ladder_value != 0, map_name)


@dataclass(frozen=True)
class GroupAnnouncement:
    """0x0f AddGroupToGroup and 0x20 AddGroupToGroup2: a group is added under a parent.

    Constructors 0x5e2d60 and 0x5e3b40. Seen by the receiver (handlers 0x5d2790, 0x5d3b50):
    parent JOINED_GROUP_PARENT_ID means "you are now in this group"; any other parent shows the
    group inside that parent, which the receiver must be in. The same message from a client asks
    the server to open a game or chat group (with group id SERVER_ASSIGNS_GROUP_ID), or to list an
    existing game in a zone; between two clients it is a private chat invitation (group id
    PRIVATE_CHAT_GROUP_ID).

    Layout: u32 parent, u32 group id, u32 flags, u32 maximum player count, three strings (name,
    description column text, password when a player creates a chat group), then the game block
    when flags has HAS_GAME_BLOCK, then the description (u32 length, that many bytes) when flags
    has HAS_DESCRIPTION. Nothing may follow: unknown trailing bytes are refused rather than passed
    on to other players.
    """

    message_type: MessageType
    parent_id: int
    group_id: int
    flags: int
    max_players: int
    name: str
    second_text: str
    third_text: str
    game_block: GameBlock | None = None
    description: bytes = field(default=b"", repr=False)

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        writer.write_u32(self.parent_id).write_u32(self.group_id)
        writer.write_u32(self.flags).write_u32(self.max_players)
        writer.write_string(self.name).write_string(self.second_text).write_string(self.third_text)
        if self.flags & GroupFlag.HAS_GAME_BLOCK:
            if self.game_block is None:
                raise ValueError("flags announce a game block, but there is none")
            write_game_block(writer, self.game_block)
        if self.flags & GroupFlag.HAS_DESCRIPTION:
            write_description(writer, self.description)
        return writer.to_message(self.message_type)

    @classmethod
    def decode(cls, message: RawMessage) -> GroupAnnouncement:
        reader = open_message_body(message, GROUP_ANNOUNCEMENT_MESSAGE_TYPES)
        message_type = MessageType(message.message_type)
        parent_id = reader.read_u32()
        group_id = reader.read_u32()
        flags = reader.read_u32()
        max_players = reader.read_u32()
        name = reader.read_string()
        second_text = reader.read_string()
        third_text = reader.read_string()
        game_block = None
        if flags & GroupFlag.HAS_GAME_BLOCK:
            game_block = read_game_block(reader, message_type)
        description = b""
        if flags & GroupFlag.HAS_DESCRIPTION:
            description = read_description(reader)
        require_end_of_body(reader, message)
        return cls(
            message_type=message_type,
            parent_id=parent_id,
            group_id=group_id,
            flags=flags,
            max_players=max_players,
            name=name,
            second_text=second_text,
            third_text=third_text,
            game_block=game_block,
            description=description,
        )

    def under_parent(self, parent_id: int) -> GroupAnnouncement:
        """The same announcement placed under another parent."""
        return replace(self, parent_id=parent_id)


@dataclass(frozen=True)
class GroupUpdate:
    """0x19 UpdateGroup and 0x21 UpdateGroup2: some settings of a group changed.

    Built by the constructor 0x5ea320 in this order: u32 group id, u32 update mask (GroupUpdateMask:
    which of the following fields count), u32 flags, u32 maximum player count, three strings, the
    game block and the description, the last two always present. A host sends it while setting up
    a game (mask 0x50: player count and description, 0x45506c). Receivers apply it only to groups
    they see inside a parent, never to a group they are in (0x5d8de6).
    """

    message_type: MessageType
    group_id: int
    update_mask: int
    flags: int
    max_players: int
    name: str
    second_text: str
    third_text: str
    game_block: GameBlock
    description: bytes = field(repr=False)

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        writer.write_u32(self.group_id).write_u32(self.update_mask)
        writer.write_u32(self.flags).write_u32(self.max_players)
        writer.write_string(self.name).write_string(self.second_text).write_string(self.third_text)
        write_game_block(writer, self.game_block)
        write_description(writer, self.description)
        return writer.to_message(self.message_type)

    @classmethod
    def decode(cls, message: RawMessage) -> GroupUpdate:
        reader = open_message_body(message, GROUP_UPDATE_MESSAGE_TYPES)
        message_type = MessageType(message.message_type)
        update = cls(
            message_type=message_type,
            group_id=reader.read_u32(),
            update_mask=reader.read_u32(),
            flags=reader.read_u32(),
            max_players=reader.read_u32(),
            name=reader.read_string(),
            second_text=reader.read_string(),
            third_text=reader.read_string(),
            game_block=read_game_block(reader, message_type),
            description=read_description(reader),
        )
        require_end_of_body(reader, message)
        return update

    def applied_to(self, announcement: GroupAnnouncement) -> GroupAnnouncement:
        """The announcement with the fields this update's mask names taken over."""
        changes = {}
        if self.update_mask & GroupUpdateMask.NAME:
            changes["name"] = self.name
        if self.update_mask & GroupUpdateMask.SECOND_TEXT:
            changes["second_text"] = self.second_text
        if self.update_mask & GroupUpdateMask.THIRD_TEXT:
            changes["third_text"] = self.third_text
        if self.update_mask & GroupUpdateMask.FLAGS:
            changes["flags"] = self.flags
        if self.update_mask & GroupUpdateMask.MAX_PLAYERS:
            changes["max_players"] = self.max_players
        if self.update_mask & GroupUpdateMask.GAME_BLOCK:
            changes["game_block"] = self.game_block
        if self.update_mask & GroupUpdateMask.DESCRIPTION:
            changes["description"] = self.description
        return replace(announcement, **changes)


@dataclass(frozen=True)
class GroupCounters:
    """0x1a: the counters of a group shown inside a group the receiver is in (handler 0x5d8580):
    players in it, its maximum, and flags with bit 0 for full. The game list shows players/max
    and marks a full game (0x44b635); joining a full group is refused by the client (0x4437c1)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.GROUP_COUNTERS
    group_id: int
    player_count: int
    max_players: int
    status_flags: int

    def encode(self) -> RawMessage:
        writer = FieldWriter().write_u32(self.group_id).write_u32(self.player_count)
        writer.write_u32(self.max_players).write_u32(self.status_flags)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> GroupCounters:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(
            group_id=reader.read_u32(),
            player_count=reader.read_u32(),
            max_players=reader.read_u32(),
            status_flags=reader.read_u32(),
        )


def write_game_block(writer: FieldWriter, game_block: GameBlock) -> None:
    writer.write_socket_address(game_block.host_address).write_bytes(game_block.remainder)


def read_game_block(reader: FieldReader, message_type: MessageType) -> GameBlock:
    host_address = reader.read_socket_address()
    remainder_size = GAME_BLOCK_SIZE_BY_MESSAGE_TYPE[message_type] - SOCKET_ADDRESS_SIZE
    return GameBlock(host_address=host_address, remainder=reader.read_bytes(remainder_size))


def write_description(writer: FieldWriter, description: bytes) -> None:
    writer.write_u32(len(description)).write_bytes(description)


def read_description(reader: FieldReader) -> bytes:
    """u32 length, then that many bytes. A game's host stores a serialized GameDescription there;
    the server keeps the bytes and passes them on unchanged."""
    length = reader.read_u32()
    return reader.read_bytes(length)


def require_end_of_body(reader: FieldReader, message: RawMessage) -> None:
    if reader.remaining_size:
        raise ProtocolError(f"{reader.remaining_size} unexpected bytes after {message.type_name}")


@dataclass(frozen=True)
class RemoveGroupFromGroup:
    """0x10: a group under a parent is gone, for example a game that ended. The receiver removes
    it from the parent it is in (handler 0x5d5360). Size exactly 12."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.REMOVE_GROUP_FROM_GROUP
    parent_id: int
    group_id: int

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.parent_id).write_u32(self.group_id).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> RemoveGroupFromGroup:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(parent_id=reader.read_u32(), group_id=reader.read_u32())


# Players in groups.


@dataclass(frozen=True)
class AddPlayerToGroup:
    """0x0c: a player is in a group (constructor 0x5e4e10). Size above 28.

    From a client it asks to join; from the server it lists a member. The address matters: clients
    send room chat straight to each member's address (loop 0x5c661e), and a client only knows its
    own address as 0.0.0.0, so the server fills in the one it sees datagrams come from. The client
    sends an empty name and never reads extra_text.
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.ADD_PLAYER_TO_GROUP
    group_id: int
    player_id: int
    player_address: Address
    player_name: str
    extra_text: str = ""

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        writer.write_u32(self.group_id).write_u32(self.player_id).write_socket_address(self.player_address)
        writer.write_string(self.player_name).write_string(self.extra_text)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> AddPlayerToGroup:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(
            group_id=reader.read_u32(),
            player_id=reader.read_u32(),
            player_address=reader.read_socket_address(),
            player_name=reader.read_string(),
            extra_text=reader.read_string(),
        )


@dataclass(frozen=True)
class AddPlayerToGroupWithClan:
    """0x25: AddPlayerToGroup for a clan member, with the clan after the two texts (handler
    0x5d5f60, readers 0x5e6550 and 0x5e6740). The member list shows the clan name in its second
    column (0x449d34). With clan id 0 and an empty name it behaves exactly like 0x0c (0x5d5ea5)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.ADD_PLAYER_TO_GROUP_WITH_CLAN
    group_id: int
    player_id: int
    player_address: Address
    player_name: str
    clan_id: int
    clan_name: str
    extra_text: str = ""

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        writer.write_u32(self.group_id).write_u32(self.player_id).write_socket_address(self.player_address)
        writer.write_string(self.player_name).write_string(self.extra_text)
        writer.write_u32(self.clan_id).write_string(self.clan_name)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> AddPlayerToGroupWithClan:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(
            group_id=reader.read_u32(),
            player_id=reader.read_u32(),
            player_address=reader.read_socket_address(),
            player_name=reader.read_string(),
            extra_text=reader.read_string(),
            clan_id=reader.read_u32(),
            clan_name=reader.read_string(),
        )


@dataclass(frozen=True)
class RemovePlayerFromGroup:
    """0x0d: a player left a group. Size exactly 12.

    From a client it asks to leave (LeaveGroup 0x5ca5e0). The leaving client keeps the group on its
    joined list until the server sends this back with its own player id (handler 0x5d6af0 compares
    with its own player at 0x5d721e). Sent for a group the client does not list, it makes the
    handler throw a sync error and the game hangs.
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.REMOVE_PLAYER_FROM_GROUP
    group_id: int
    player_id: int

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.group_id).write_u32(self.player_id).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> RemovePlayerFromGroup:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(group_id=reader.read_u32(), player_id=reader.read_u32())


# Chat.


@dataclass(frozen=True)
class ChatMessage:
    """0x0b and 0x1e: one chat line. 0x1e adds a kind (0 normal, 1 whisper) before the text.

    Clients send room chat and private chat straight to each other, not through the server; the
    server only ever sees a line when it is in the list of receivers, and logs it. Group id
    PRIVATE_CHAT_GROUP_ID means a private line.
    """

    message_type: MessageType
    group_id: int
    sender_player_id: int
    text: str
    kind: int | None = None

    def encode(self) -> RawMessage:
        writer = FieldWriter().write_u32(self.group_id).write_u32(self.sender_player_id)
        if self.message_type == MessageType.CHAT_MESSAGE_2:
            normal_chat_kind = 0
            kind = normal_chat_kind if self.kind is None else self.kind
            writer.write_u32(kind)
        return writer.write_string(self.text).to_message(self.message_type)

    @classmethod
    def decode(cls, message: RawMessage) -> ChatMessage:
        reader = open_message_body(message, {MessageType.CHAT_MESSAGE, MessageType.CHAT_MESSAGE_2})
        message_type = MessageType(message.message_type)
        group_id = reader.read_u32()
        sender_player_id = reader.read_u32()
        kind = None
        if message_type == MessageType.CHAT_MESSAGE_2:
            kind = reader.read_u32()
        return cls(
            message_type=message_type,
            group_id=group_id,
            sender_player_id=sender_player_id,
            text=reader.read_string(),
            kind=kind,
        )


@dataclass(frozen=True)
class ChatCommand:
    """0x22: a slash command the client did not handle itself goes to the server."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.CHAT_COMMAND
    group_id: int
    unknown_value: int
    command: str

    def encode(self) -> RawMessage:
        writer = FieldWriter().write_u32(self.group_id).write_u32(self.unknown_value)
        return writer.write_string(self.command).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> ChatCommand:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(group_id=reader.read_u32(), unknown_value=reader.read_u32(), command=reader.read_string())


@dataclass(frozen=True)
class ChatCommandAnswer:
    """0x23: the server's answer to a chat command; the client shows each line as [Server] (handler
    0x5d2420). Layout: u32 group id, u32 unused, u32 line count, the lines."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.CHAT_COMMAND_ANSWER
    group_id: int
    lines: tuple[str, ...]

    def encode(self) -> RawMessage:
        unused_value = 0
        writer = FieldWriter().write_u32(self.group_id).write_u32(unused_value).write_u32(len(self.lines))
        for line in self.lines:
            writer.write_string(line)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> ChatCommandAnswer:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        group_id = reader.read_u32()
        reader.read_u32()
        line_count = reader.read_u32()
        lines = tuple(reader.read_string() for _ in range(line_count))
        return cls(group_id=group_id, lines=lines)


# Other requests the server answers so the client does not wait for them.


@dataclass(frozen=True)
class GetImageList:
    """0x1d: the client asks for a group's images (sent for groups with GroupFlag REQUESTS_IMAGES)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.GET_IMAGE_LIST
    group_id: int

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.group_id).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> GetImageList:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(group_id=reader.read_u32())


@dataclass(frozen=True)
class ImageEntry:
    """One entry of an image list: the game's lobby banner. The game compares the two ids with the
    ones it stored (registry values BannerId and BannerSubId) and, when they differ and banner_id is
    not 0, downloads url by anonymous FTP as Banner\\Banner.png (0x449938, 0x465a0f)."""

    banner_id: int
    banner_sub_id: int
    url: str


@dataclass(frozen=True)
class ImageList:
    """0x1c: the answer to GetImageList (handler 0x5d4e50): u32 group id, u32 count, then per entry
    u32 banner id, u32 banner sub id and the url. Only the Home tab uses it, and only its last
    entry; the client stores it on the group, which must be one it is in (0x5dbf00)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.IMAGE_LIST
    group_id: int
    images: tuple[ImageEntry, ...] = ()

    def encode(self) -> RawMessage:
        writer = FieldWriter().write_u32(self.group_id).write_u32(len(self.images))
        for image in self.images:
            writer.write_u32(image.banner_id).write_u32(image.banner_sub_id).write_string(image.url)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> ImageList:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        group_id = reader.read_u32()
        image_count = reader.read_u32()
        images = []
        for _ in range(image_count):
            images.append(ImageEntry(banner_id=reader.read_u32(), banner_sub_id=reader.read_u32(), url=reader.read_string()))
        return cls(group_id=group_id, images=tuple(images))


@dataclass(frozen=True)
class TextIdList:
    """0x14: the ids of the texts a group offers (handler 0x5d7d60); size exactly 12 + 4 per id. On
    the Home tab the client picks the news text from it, in the MessageZone a theme's messages. The
    group must be one the client is in (0x5d81a6)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.TEXT_ID_LIST
    group_id: int
    text_ids: tuple[int, ...]

    def encode(self) -> RawMessage:
        writer = FieldWriter().write_u32(self.group_id).write_u32(len(self.text_ids))
        for text_id in self.text_ids:
            writer.write_u32(text_id)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> TextIdList:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        group_id = reader.read_u32()
        text_count = reader.read_u32()
        text_ids = tuple(reader.read_u32() for _ in range(text_count))
        return cls(group_id=group_id, text_ids=text_ids)


@dataclass(frozen=True)
class ServerStatistics:
    """0x1b: the statistics table on the Home tab (0x448d20), stored on every joined group with
    GroupFlag RECEIVES_STATISTICS (0x5d997d). No group id; six u32 in the order shown: players
    online, players in the last hour, players in the last day, games open, games in the last hour,
    games in the last day (cells 0x448dfc..0x44921d). A first value of 0 shows "-"."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.SERVER_STATISTICS
    players_online: int
    players_last_hour: int
    players_last_day: int
    games_open: int
    games_last_hour: int
    games_last_day: int

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        for value in (
            self.players_online,
            self.players_last_hour,
            self.players_last_day,
            self.games_open,
            self.games_last_hour,
            self.games_last_day,
        ):
            writer.write_u32(value)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> ServerStatistics:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(
            players_online=reader.read_u32(),
            players_last_hour=reader.read_u32(),
            players_last_day=reader.read_u32(),
            games_open=reader.read_u32(),
            games_last_hour=reader.read_u32(),
            games_last_day=reader.read_u32(),
        )


@dataclass(frozen=True)
class GetTextMessage:
    """0x16: the client asks for a text (most likely a server message of the day) by its id."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.GET_TEXT_MESSAGE
    text_id: int

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.text_id).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> GetTextMessage:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(text_id=reader.read_u32())


@dataclass(frozen=True)
class TextMessage:
    """0x15: the answer to GetTextMessage (handler 0x5d8270): u32 text id, string title, u32 line
    count, the lines. The client takes it only for the id it is waiting for (0x5d8483) and only
    with a non-empty title (0x5cb642), and keeps it for the rest of its session (0x60bbe0). It shows
    the title, an empty line, then the lines (0x4497ff)."""

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.TEXT_MESSAGE
    text_id: int
    title: str = ""
    lines: tuple[str, ...] = ()

    def encode(self) -> RawMessage:
        writer = FieldWriter().write_u32(self.text_id).write_string(self.title).write_u32(len(self.lines))
        for line in self.lines:
            writer.write_string(line)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> TextMessage:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        text_id = reader.read_u32()
        title = reader.read_string()
        line_count = reader.read_u32()
        lines = tuple(reader.read_string() for _ in range(line_count))
        return cls(text_id=text_id, title=title, lines=lines)


@dataclass(frozen=True)
class MatchReportPlayer:
    """One slot of a match report, in wire order (constructor 0x5ec316..0x5ec505; the values are
    gathered for every slot of the match at 0x5405bc..0x5406b5).

    flags holds MatchPlayerFlag bits. lobby_player_id is the player id the lobby gave the slot's
    player (0 for a computer opponent, inferred); in a ladder game a slot without one makes the
    report not valid for the ladder (0x5405ec). race is 0 Romans, 1 Egyptians, 2 Asians, 3 Amazons
    (inferred from the setup's race choices 0x48c2a3..0x48c2df and the per-race tables the game
    indexes with it, 0x4dd2fc). unused is always 0 (0x54061d). slot is the slot number, 0 to 19;
    team the slot's team as the game numbers it (the winning team uses the same numbers).
    """

    flags: int
    lobby_player_id: int
    race: int
    unused: int
    slot: int
    team: int

    @property
    def is_computer(self) -> bool:
        return bool(self.flags & MatchPlayerFlag.COMPUTER)


MATCH_REPORT_PLAYER_FIELD_COUNT = 6
MATCH_REPORT_PLAYER_SIZE = MATCH_REPORT_PLAYER_FIELD_COUNT * U32_SIZE


@dataclass(frozen=True)
class GameStatus:
    """0x1f: a client's report on its match (Logic.SendGameStatus 0x540500, constructor 0x5ec280).

    Every client of a lobby game sends one when the match starts (sent unconditionally, 0x53da69,
    so there is no winner yet) and one when a single team is left (0x53ae66, once per match).

    Layout: u32 group id (the lobby game), u32 flags (GameStatusFlag), u32 winning team
    (NO_WINNING_TEAM while more than one team is left), u32 game time (the game's tick counter,
    global 0x7dfd48), string map (the map file of the game settings, +0x2c via 0x436a60), u32 n,
    then n MatchReportPlayer records of six u32.

    The game shows the server's answer only during a ladder game (0x540a18): Result
    GAME_STATUS_STORED as "Game status stored at the Lobby server." (0x540a3e), any other code of
    the same kind as "Game status inconsistencies. Game not valid for ladder." (0x540b89).
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.GAME_STATUS
    group_id: int
    flags: int
    winning_team: int
    game_time: int
    map_name: str
    players: tuple[MatchReportPlayer, ...] = ()

    @property
    def is_ladder_game(self) -> bool:
        return bool(self.flags & GameStatusFlag.LADDER_GAME)

    @property
    def has_winner(self) -> bool:
        return self.winning_team != NO_WINNING_TEAM

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        writer.write_u32(self.group_id).write_u32(self.flags)
        writer.write_u32(self.winning_team).write_u32(self.game_time)
        writer.write_string(self.map_name).write_u32(len(self.players))
        for player in self.players:
            writer.write_u32(player.flags).write_u32(player.lobby_player_id).write_u32(player.race)
            writer.write_u32(player.unused).write_u32(player.slot).write_u32(player.team)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> GameStatus:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        group_id = reader.read_u32()
        flags = reader.read_u32()
        winning_team = reader.read_u32()
        game_time = reader.read_u32()
        map_name = reader.read_string()
        player_count = reader.read_u32()
        # Checked before the loop, so a forged count cannot keep the server reading.
        if player_count * MATCH_REPORT_PLAYER_SIZE != reader.remaining_size:
            raise ProtocolError(f"match report claims {player_count} players in {reader.remaining_size} bytes")
        players = []
        for _ in range(player_count):
            fields = [reader.read_u32() for _ in range(MATCH_REPORT_PLAYER_FIELD_COUNT)]
            players.append(MatchReportPlayer(*fields))
        return cls(group_id, flags, winning_team, game_time, map_name, tuple(players))


# Logging.

# Messages that can carry a secret: the logon's access code, and the password of a chat group a
# player creates (third text of the announcement) or joins (extra text of AddPlayerToGroup).
SECRET_PLACEHOLDER = "<secret removed>"


def describe_for_log(message: RawMessage) -> str:
    """One message for a debug log, with any secret it carries removed.

    Messages without secrets show their body in hex, which is what is needed to compare a session
    with the game's code. Messages with secrets show their decoded fields with the secret replaced.
    """
    match message.message_type:
        case MessageType.LOGON:
            return describe_decoded(message, Logon)
        case MessageType.ADD_PLAYER_TO_GROUP:
            return describe_decoded(message, AddPlayerToGroup, secret_field="extra_text")
        case MessageType.ADD_GROUP_TO_GROUP | MessageType.ADD_GROUP_TO_GROUP_2:
            return describe_decoded(message, GroupAnnouncement, secret_field="third_text")
        case _:
            if not message.body:
                return message.type_name
            return f"{message.type_name}[{message.body.hex()}]"


def describe_decoded(message: RawMessage, message_class: type, secret_field: str | None = None) -> str:
    try:
        decoded = message_class.decode(message)
    except ProtocolError:
        return f"{message.type_name}[{len(message.body)} bytes, unreadable, not shown]"
    if secret_field is not None and getattr(decoded, secret_field):
        decoded = replace(decoded, **{secret_field: SECRET_PLACEHOLDER})
    return repr(decoded)
