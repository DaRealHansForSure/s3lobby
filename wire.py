"""Byte layout of the Settlers 3 lobby protocol (see docs/s3-lobby-protocol.md).

One UDP datagram carries one block: messages back to back, each starting with a
4-byte header `[type u8][0 u8][size u16]` whose size counts the whole message.
Numbers are little endian u32, strings are ANSI and end with a NUL byte.
"""

import socket
import struct

HEADER_SIZE = 4
MAX_DATAGRAM_SIZE = 0xFFFF
# The game is a German Windows program: names and chat travel in the ANSI code page.
TEXT_ENCODING = "cp1252"
SOCKADDR_SIZE = 16
AF_INET = 2

# Connection layer (protocol.cpp).
ACKNOWLEDGE_MESSAGES = 0x00
MIN_MISSING_ACKNOWLEDGE = 0x01
MESSAGE_ID = 0x02
CLOSE_CONNECTION = 0x03
ADJUST_CONNECTION_TIMEOUT = 0xFE
OPEN_CONNECTION = 0xFF

# Lobby layer (Lobby.cpp).
GET_LOBBY_SERVER_LIST = 0x05
LOBBY_SERVER_LIST = 0x06
LOGON = 0x07
RESULT = 0x09
LOGOUT = 0x0A
CHAT_MESSAGE = 0x0B
ADD_PLAYER_TO_GROUP = 0x0C
REMOVE_PLAYER_FROM_GROUP = 0x0D
OWN_PLAYER = 0x0E
ADD_GROUP_TO_GROUP = 0x0F
REMOVE_GROUP_FROM_GROUP = 0x10
CHECK_FILE_VERSION = 0x11
TEXT_MESSAGE = 0x15
GET_TEXT_MESSAGE = 0x16
UPDATE_GROUP = 0x19
IMAGE_LIST = 0x1C
GET_IMAGE_LIST = 0x1D
CHAT_MESSAGE_2 = 0x1E
GAME_STATUS = 0x1F
ADD_GROUP_TO_GROUP_2 = 0x20
UPDATE_GROUP_2 = 0x21
CHAT_COMMAND = 0x22
CHAT_COMMAND_ANSWER = 0x23

MESSAGE_NAMES = {
    value: name
    for name, value in globals().items()
    if name.isupper() and isinstance(value, int) and name not in ("HEADER_SIZE", "MAX_DATAGRAM_SIZE", "SOCKADDR_SIZE", "AF_INET")
}


class ProtocolError(Exception):
    """A datagram or message that does not follow the layout the game uses."""


class Message:
    """One lobby message: its type and its body (everything after the header)."""

    def __init__(self, message_type, body=b""):
        self.message_type = message_type
        self.body = bytes(body)

    def encode(self):
        size = HEADER_SIZE + len(self.body)
        if size > MAX_DATAGRAM_SIZE:
            raise ProtocolError(f"message {self.message_type:#x} too large: {size} bytes")
        return struct.pack("<BBH", self.message_type, 0, size) + self.body

    def reader(self):
        return BodyReader(self.body)

    def __repr__(self):
        name = MESSAGE_NAMES.get(self.message_type, "unknown")
        return f"Message({self.message_type:#04x} {name}, {len(self.body)} bytes)"


def decode_block(datagram):
    """Split one datagram into its messages, in the order they were packed."""
    messages = []
    position = 0
    while position < len(datagram):
        if len(datagram) - position < HEADER_SIZE:
            raise ProtocolError(f"truncated header at byte {position}")
        message_type, _, size = struct.unpack_from("<BBH", datagram, position)
        if size < HEADER_SIZE or position + size > len(datagram):
            raise ProtocolError(f"bad size {size} for message {message_type:#x} at byte {position}")
        messages.append(Message(message_type, datagram[position + HEADER_SIZE: position + size]))
        position += size
    return messages


def encode_block(messages):
    return b"".join(message.encode() for message in messages)


class BodyBuilder:
    """Appends fields in the order the game's message constructors write them."""

    def __init__(self):
        self.parts = []

    def u32(self, value):
        self.parts.append(struct.pack("<I", value & 0xFFFFFFFF))
        return self

    def string(self, text):
        self.parts.append(text.encode(TEXT_ENCODING, errors="replace") + b"\0")
        return self

    def raw(self, data):
        self.parts.append(bytes(data))
        return self

    def sockaddr(self, address):
        self.parts.append(encode_sockaddr(address))
        return self

    def build(self, message_type):
        return Message(message_type, b"".join(self.parts))


class BodyReader:
    """Reads fields from a message body front to back."""

    def __init__(self, body):
        self.body = body
        self.position = 0

    def remaining(self):
        return len(self.body) - self.position

    def u32(self):
        if self.remaining() < 4:
            raise ProtocolError("u32 past end of message")
        value = struct.unpack_from("<I", self.body, self.position)[0]
        self.position += 4
        return value

    def string(self):
        end = self.body.find(b"\0", self.position)
        if end < 0:
            raise ProtocolError("string without NUL")
        text = self.body[self.position:end].decode(TEXT_ENCODING, errors="replace")
        self.position = end + 1
        return text

    def raw(self, length):
        if self.remaining() < length:
            raise ProtocolError(f"{length} bytes past end of message")
        data = self.body[self.position:self.position + length]
        self.position += length
        return data

    def rest(self):
        return self.raw(self.remaining())


def encode_sockaddr(address):
    """struct sockaddr_in as Windows lays it out: family u16 LE, port and IP in network order, 8 zero bytes."""
    host, port = address
    return struct.pack("<H", AF_INET) + struct.pack(">H", port) + socket.inet_aton(host) + bytes(8)


def decode_sockaddr(data):
    port = struct.unpack_from(">H", data, 2)[0]
    host = socket.inet_ntoa(data[4:8])
    return host, port
