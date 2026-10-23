"""Bytes on the wire: message headers, blocks of messages, and the three field types.

One UDP datagram carries one block: messages placed back to back, each starting with a 4-byte
header [type u8][always 0 u8][size u16]. There is no other framing (send 0x5cbad0, block parser
0x601810).

The game writes every field with one of three helpers, so readers and writers here offer exactly
those three:
- u32: four bytes, little endian (AppendU32 0x6038e0)
- string: ANSI text followed by a NUL byte (AppendString 0x602830)
- raw bytes, used for struct sockaddr_in and GUIDs (AppendBytes 0x6027f0)

The higher level message classes in messages.py are built from these.
"""

from __future__ import annotations

import socket
import struct
from collections.abc import Iterable
from dataclasses import dataclass

from s3lobby.constants import (
    ADDRESS_FAMILY_IPV4,
    MAX_DATAGRAM_SIZE,
    MESSAGE_HEADER_SIZE,
    SOCKET_ADDRESS_SIZE,
    TEXT_ENCODING,
    U32_SIZE,
    MessageType,
)

# An IPv4 address and UDP port, in the shape asyncio and the socket module use: ("10.0.0.5", 3343).
Address = tuple[str, int]

# The header's middle byte is always written as 0 (base constructor 0x601e80).
HEADER_FORMAT = "<BBH"
HEADER_PADDING_BYTE = 0
U32_FORMAT = "<I"
U32_MASK = 0xFFFFFFFF
STRING_TERMINATOR = b"\0"

# Inside struct sockaddr_in: the family is little endian like everything else the game writes,
# but the port and the address are in network order (big endian), as Winsock expects.
SOCKET_ADDRESS_FAMILY_FORMAT = "<H"
SOCKET_ADDRESS_PORT_FORMAT = ">H"
SOCKET_ADDRESS_PORT_OFFSET = 2
SOCKET_ADDRESS_HOST_OFFSET = 4
SOCKET_ADDRESS_PADDING_OFFSET = 8

# Text is decoded so that every byte survives the round trip back to the wire. Windows-1252 leaves
# five byte values undefined; "surrogateescape" carries them through as placeholder characters
# instead of replacing them, so a game name relayed to other players stays byte for byte the same.
TEXT_ERROR_HANDLING = "surrogateescape"
# Where such text leaves the wire (the database, the live lobby, web pages), a placeholder would
# break: UTF-8 cannot encode it. readable_text puts the replacement character there instead.
READABLE_TEXT_ERROR_HANDLING = "replace"


class ProtocolError(Exception):
    """A datagram or a field that does not follow the layout the game writes."""


@dataclass(frozen=True)
class RawMessage:
    """One message as it travels: its type byte and its body (everything after the header).

    The type stays a plain int rather than a MessageType: the game knows types this server has no
    name for, and those must still pass through the block parser and the logs.
    """

    message_type: int
    body: bytes = b""

    @property
    def type_name(self) -> str:
        """The message type's name for logs, or its hex value when the server has no name for it."""
        try:
            return MessageType(self.message_type).name
        except ValueError:
            return f"{self.message_type:#04x}"

    def encode(self) -> bytes:
        """The message as it goes into a datagram, header included."""
        size = MESSAGE_HEADER_SIZE + len(self.body)
        if size > MAX_DATAGRAM_SIZE:
            raise ProtocolError(f"message {self.type_name} is {size} bytes, more than one datagram holds")
        header = struct.pack(HEADER_FORMAT, self.message_type, HEADER_PADDING_BYTE, size)
        return header + self.body

    def reader(self) -> FieldReader:
        """A reader positioned at the first byte of the body."""
        return FieldReader(self.body)


def decode_block(datagram: bytes, max_messages: int | None = None) -> list[RawMessage]:
    """Split one datagram into its messages, in the order they were packed.

    Raises ProtocolError when a header is cut off or a size points outside the datagram; the game
    treats such a datagram as broken as a whole, and so does the server. With max_messages it also
    refuses a datagram holding more, as soon as it gets there: a datagram of empty messages would
    otherwise make some sixteen thousand objects before its count could be checked.
    """
    messages = []
    position = 0
    while position < len(datagram):
        if max_messages is not None and len(messages) == max_messages:
            raise ProtocolError(f"more than {max_messages} messages in one datagram")
        if len(datagram) - position < MESSAGE_HEADER_SIZE:
            raise ProtocolError(f"message header cut off at byte {position}")
        message_type, _, size = struct.unpack_from(HEADER_FORMAT, datagram, position)
        is_size_valid = MESSAGE_HEADER_SIZE <= size <= len(datagram) - position
        if not is_size_valid:
            raise ProtocolError(f"message {message_type:#04x} at byte {position} claims {size} bytes")
        body = datagram[position + MESSAGE_HEADER_SIZE:position + size]
        messages.append(RawMessage(message_type, body))
        position += size
    return messages


def encode_block(messages: Iterable[RawMessage]) -> bytes:
    """Pack messages back to back into one datagram."""
    datagram = b"".join(message.encode() for message in messages)
    if len(datagram) > MAX_DATAGRAM_SIZE:
        raise ProtocolError(f"block of {len(datagram)} bytes does not fit one datagram")
    return datagram


def readable_text(text: str) -> str:
    """Text from a client as it may be stored or shown: bytes Windows-1252 leaves undefined become
    the replacement character instead of the placeholders that carry them over the wire."""
    raw = text.encode(TEXT_ENCODING, errors=TEXT_ERROR_HANDLING)
    return raw.decode(TEXT_ENCODING, errors=READABLE_TEXT_ERROR_HANDLING)


class FieldWriter:
    """Builds a message body field by field, in the order the game's constructors write them.

    Every write returns the writer itself, so a body reads like the field list in the protocol
    document: FieldWriter().write_u32(group_id).write_string(name).to_message(MessageType.X)
    """

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def write_u32(self, value: int) -> FieldWriter:
        # Masking lets callers pass values like -1 the way the game's C++ code would.
        self._parts.append(struct.pack(U32_FORMAT, value & U32_MASK))
        return self

    def write_string(self, text: str) -> FieldWriter:
        encoded = text.encode(TEXT_ENCODING, errors=TEXT_ERROR_HANDLING)
        if STRING_TERMINATOR in encoded:
            raise ProtocolError(f"text {text!r} contains a NUL byte, which would end the string early")
        self._parts.append(encoded + STRING_TERMINATOR)
        return self

    def write_bytes(self, data: bytes) -> FieldWriter:
        self._parts.append(bytes(data))
        return self

    def write_socket_address(self, address: Address) -> FieldWriter:
        self._parts.append(encode_socket_address(address))
        return self

    def to_message(self, message_type: int) -> RawMessage:
        return RawMessage(message_type, b"".join(self._parts))


class FieldReader:
    """Reads a message body front to back.

    Every read checks that the field fits what is left of the body and raises ProtocolError when it
    does not, so a short or garbled message from a client never crashes the server.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._position = 0

    @property
    def position(self) -> int:
        """Offset of the next unread byte, counted from the start of the body."""
        return self._position

    @property
    def remaining_size(self) -> int:
        return len(self._body) - self._position

    def read_u32(self) -> int:
        data = self.read_bytes(U32_SIZE)
        return struct.unpack(U32_FORMAT, data)[0]

    def read_string(self) -> str:
        end = self._body.find(STRING_TERMINATOR, self._position)
        if end < 0:
            raise ProtocolError(f"string at body offset {self._position} has no terminating NUL")
        text = self._body[self._position:end].decode(TEXT_ENCODING, errors=TEXT_ERROR_HANDLING)
        self._position = end + len(STRING_TERMINATOR)
        return text

    def read_bytes(self, length: int) -> bytes:
        if length > self.remaining_size:
            raise ProtocolError(
                f"field of {length} bytes at body offset {self._position}, only {self.remaining_size} left"
            )
        data = self._body[self._position:self._position + length]
        self._position += length
        return data

    def read_socket_address(self) -> Address:
        return decode_socket_address(self.read_bytes(SOCKET_ADDRESS_SIZE))

    def read_remaining_bytes(self) -> bytes:
        return self.read_bytes(self.remaining_size)


def encode_socket_address(address: Address) -> bytes:
    """struct sockaddr_in as the game writes it: family u16 little endian, port u16 big endian,
    the IPv4 address in network order, then zero bytes up to 16."""
    host, port = address
    try:
        packed_host = socket.inet_aton(host)
    except OSError as error:
        raise ProtocolError(f"{host!r} is not an IPv4 address") from error
    family = struct.pack(SOCKET_ADDRESS_FAMILY_FORMAT, ADDRESS_FAMILY_IPV4)
    network_order_port = struct.pack(SOCKET_ADDRESS_PORT_FORMAT, port)
    zero_padding = bytes(SOCKET_ADDRESS_SIZE - SOCKET_ADDRESS_PADDING_OFFSET)
    return family + network_order_port + packed_host + zero_padding


def decode_socket_address(data: bytes) -> Address:
    """The (host, port) inside a struct sockaddr_in written by the game."""
    if len(data) != SOCKET_ADDRESS_SIZE:
        raise ProtocolError(f"socket address of {len(data)} bytes, expected {SOCKET_ADDRESS_SIZE}")
    port = struct.unpack_from(SOCKET_ADDRESS_PORT_FORMAT, data, SOCKET_ADDRESS_PORT_OFFSET)[0]
    host = socket.inet_ntoa(data[SOCKET_ADDRESS_HOST_OFFSET:SOCKET_ADDRESS_PADDING_OFFSET])
    return host, port
