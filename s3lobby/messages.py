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
    GAME_BLOCK_SIZE_BY_MESSAGE_TYPE,
    GROUP_ANNOUNCEMENT_MESSAGE_TYPES,
    GROUP_UPDATE_MESSAGE_TYPES,
    SOCKET_ADDRESS_SIZE,
    GroupFlag,
    MessageType,
)
from s3lobby.wire import Address, FieldReader, FieldWriter, ProtocolError, RawMessage


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
    installed: 2 Mission CD, 3 Gold/Plus, 4 both (global 0x7a7ff8). The third value comes from the
    game's configuration (offset 0x28) and its meaning is unknown.
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.CHECK_FILE_VERSION
    game_version: int
    edition: int
    unknown_setting: int

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        writer.write_u32(self.game_version).write_u32(self.edition).write_u32(self.unknown_setting)
        return writer.to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> CheckFileVersion:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(
            game_version=reader.read_u32(),
            edition=reader.read_u32(),
            unknown_setting=reader.read_u32(),
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


# Groups: rooms, zones and games.


@dataclass(frozen=True)
class GameBlock:
    """The part of a group announcement that makes the group a joinable game (GroupFlag
    HAS_GAME_BLOCK).

    host_address is where the host's game runs; a joining client connects DirectPlay to its IP.
    The remainder is kept exactly as the host wrote it: 16 bytes that are most likely the
    DirectPlay session GUID, then 3 (AddGroupToGroup) or 5 (AddGroupToGroup2) unknown u32 values.
    """

    host_address: Address
    remainder: bytes


@dataclass(frozen=True)
class GroupAnnouncement:
    """0x0f AddGroupToGroup and 0x20 AddGroupToGroup2: a group is added under a parent.

    Constructors 0x5e2d60 and 0x5e3b40. Seen by the receiver (handlers 0x5d2790, 0x5d3b50):
    parent JOINED_GROUP_PARENT_ID means "you are now in this group"; any other parent shows the
    group inside that parent, which the receiver must be in. The same message from a client asks
    the server to open a game (with group id SERVER_ASSIGNS_GROUP_ID), or between two clients it is
    a private chat invitation (group id PRIVATE_CHAT_GROUP_ID).

    Layout: u32 parent, u32 group id, u32 flags, u32 unknown, three strings (the first is the
    group's name), then the game block when the flags say so, then everything else (the
    description when the flags say so) kept as raw bytes.
    """

    message_type: MessageType
    parent_id: int
    group_id: int
    flags: int
    unknown_value: int
    name: str
    second_text: str
    third_text: str
    game_block: GameBlock | None = None
    trailing_bytes: bytes = field(default=b"", repr=False)

    def encode(self) -> RawMessage:
        writer = FieldWriter()
        writer.write_u32(self.parent_id).write_u32(self.group_id)
        writer.write_u32(self.flags).write_u32(self.unknown_value)
        writer.write_string(self.name).write_string(self.second_text).write_string(self.third_text)
        if self.game_block is not None:
            writer.write_socket_address(self.game_block.host_address)
            writer.write_bytes(self.game_block.remainder)
        writer.write_bytes(self.trailing_bytes)
        return writer.to_message(self.message_type)

    @classmethod
    def decode(cls, message: RawMessage) -> GroupAnnouncement:
        reader = open_message_body(message, GROUP_ANNOUNCEMENT_MESSAGE_TYPES)
        message_type = MessageType(message.message_type)
        parent_id = reader.read_u32()
        group_id = reader.read_u32()
        flags = reader.read_u32()
        unknown_value = reader.read_u32()
        name = reader.read_string()
        second_text = reader.read_string()
        third_text = reader.read_string()
        game_block = None
        if flags & GroupFlag.HAS_GAME_BLOCK:
            host_address = reader.read_socket_address()
            remainder_size = GAME_BLOCK_SIZE_BY_MESSAGE_TYPE[message_type] - SOCKET_ADDRESS_SIZE
            game_block = GameBlock(host_address=host_address, remainder=reader.read_bytes(remainder_size))
        return cls(
            message_type=message_type,
            parent_id=parent_id,
            group_id=group_id,
            flags=flags,
            unknown_value=unknown_value,
            name=name,
            second_text=second_text,
            third_text=third_text,
            game_block=game_block,
            trailing_bytes=reader.read_remaining_bytes(),
        )

    def under_parent(self, parent_id: int) -> GroupAnnouncement:
        """The same announcement placed under another parent."""
        return replace(self, parent_id=parent_id)


@dataclass(frozen=True)
class GroupUpdate:
    """0x19 UpdateGroup and 0x21 UpdateGroup2: a group's settings changed.

    Layout: u32 group id, u32 update mask, u32 flags, u32 unknown, three strings, the game block
    and the description, the last two always present (docs/protocol.md). The server only needs
    the group id to know whom to tell; the rest is relayed exactly as received.
    """

    message_type: MessageType
    group_id: int
    rest_of_body: bytes = field(repr=False)

    def encode(self) -> RawMessage:
        writer = FieldWriter().write_u32(self.group_id).write_bytes(self.rest_of_body)
        return writer.to_message(self.message_type)

    @classmethod
    def decode(cls, message: RawMessage) -> GroupUpdate:
        reader = open_message_body(message, GROUP_UPDATE_MESSAGE_TYPES)
        return cls(
            message_type=MessageType(message.message_type),
            group_id=reader.read_u32(),
            rest_of_body=reader.read_remaining_bytes(),
        )


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
class EmptyImageList:
    """0x1c: the answer to GetImageList without any images (handler 0x5d4e50).

    Layout of the full message: u32 group id, u32 count, then per image two u32 and a string whose
    meanings are unknown. This server has no images to offer, so it always sends a count of 0.
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.IMAGE_LIST
    group_id: int

    def encode(self) -> RawMessage:
        image_count = 0
        return FieldWriter().write_u32(self.group_id).write_u32(image_count).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> EmptyImageList:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        group_id = reader.read_u32()
        image_count = reader.read_u32()
        if image_count != 0:
            raise ProtocolError(f"image list with {image_count} images, only empty lists are supported")
        return cls(group_id=group_id)


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
    """0x15: the answer to GetTextMessage (handler 0x5d8270). The text id must match the request.
    Layout: u32 text id, string title, u32 line count, the lines."""

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
class GameStatus:
    """0x1f: a client reports how its game went (sent from 0x540500). No answer is expected.

    Layout: u32 group id, three u32 (flags with 1 = league game, winning team, unknown), a string,
    u32 player count, then six u32 per player. The server only logs it, so everything after the
    group id stays raw.
    """

    MESSAGE_TYPE: ClassVar[MessageType] = MessageType.GAME_STATUS
    group_id: int
    rest_of_body: bytes = field(repr=False)

    def encode(self) -> RawMessage:
        return FieldWriter().write_u32(self.group_id).write_bytes(self.rest_of_body).to_message(self.MESSAGE_TYPE)

    @classmethod
    def decode(cls, message: RawMessage) -> GameStatus:
        reader = open_message_body(message, {cls.MESSAGE_TYPE})
        return cls(group_id=reader.read_u32(), rest_of_body=reader.read_remaining_bytes())
