"""The numbers the Settlers III lobby protocol is built on, shared by every module.

Every value here was read from the game's own code: S3.EXE of the GOG "Ultimate Collection"
(3,842,048 bytes, SHA-256 00a925cf...804d950b, image base 0x400000). Where a comment names a hex
address, that is the virtual address in S3.EXE where the value can be checked. docs/protocol.md
explains how the pieces fit together.

Values the game does not dictate, but this server chooses, say so in their comment.
"""

from enum import IntEnum, IntFlag

# Network addresses.

# The address the game falls back to when no lobby server is configured: Blue Byte's
# siedler3svr.bluebyte.de:3344 (string at 0x6972dc, fallback at 0x40f44a). Serving on the same port
# keeps the registry entry that points a game at this server short and familiar.
DEFAULT_SERVER_PORT = 3344

# The game binds its own UDP socket to this port, or the next free one (0x5c4b55, 0x5c4bc8). The
# server never needs it: it always answers to the address a datagram came from.
GAME_CLIENT_FIRST_PORT = 3343

# Byte layout.

# Every message starts with [type u8][always 0 u8][size u16], and the size counts these 4 bytes too.
MESSAGE_HEADER_SIZE = 4

# A datagram holds one block of messages; the game asserts a block fits a u16 length (0x601550).
MAX_DATAGRAM_SIZE = 0xFFFF

U32_SIZE = 4

# struct sockaddr_in as Windows lays it out: family u16, port u16, IPv4 address, 8 zero bytes.
SOCKET_ADDRESS_SIZE = 16
ADDRESS_FAMILY_IPV4 = 2

# The game is a Windows program of 1998 and writes names and chat in the ANSI code page. The
# German, English and other western releases all use Windows-1252.
TEXT_ENCODING = "cp1252"


class MessageType(IntEnum):
    """The type byte of every message the game knows.

    Each message class in S3.EXE passes its type to the shared base constructor 0x601e80. The
    connection layer types (protocol.cpp) carry the reliability machinery; the others (Lobby.cpp)
    are the lobby itself. The receive dispatch for lobby types is 0x5eec50.
    """

    # Connection layer.
    ACKNOWLEDGE_MESSAGES = 0x00
    MIN_MISSING_ACKNOWLEDGE = 0x01
    MESSAGE_ID = 0x02
    CLOSE_CONNECTION = 0x03
    ADJUST_CONNECTION_TIMEOUT = 0xFE
    OPEN_CONNECTION = 0xFF

    # Lobby layer.
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
    PATCH_INFORMATION = 0x12
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
    OWN_PLAYER_WITH_CLAN = 0x24


# Messages the reliable layer consumes itself; everything else is handed to the lobby.
CONNECTION_LAYER_MESSAGE_TYPES = frozenset(
    {
        MessageType.ACKNOWLEDGE_MESSAGES,
        MessageType.MIN_MISSING_ACKNOWLEDGE,
        MessageType.MESSAGE_ID,
        MessageType.CLOSE_CONNECTION,
        MessageType.ADJUST_CONNECTION_TIMEOUT,
        MessageType.OPEN_CONNECTION,
    }
)

# A block that holds only these carries no news: it must not be acknowledged or delivered.
ACKNOWLEDGEMENT_MESSAGE_TYPES = frozenset(
    {
        MessageType.ACKNOWLEDGE_MESSAGES,
        MessageType.MIN_MISSING_ACKNOWLEDGE,
    }
)

# The two group announcement variants. They differ only in the size of the game block.
GROUP_ANNOUNCEMENT_MESSAGE_TYPES = frozenset(
    {
        MessageType.ADD_GROUP_TO_GROUP,
        MessageType.ADD_GROUP_TO_GROUP_2,
    }
)
GROUP_UPDATE_MESSAGE_TYPES = frozenset(
    {
        MessageType.UPDATE_GROUP,
        MessageType.UPDATE_GROUP_2,
    }
)


class ResultCode(IntEnum):
    """Codes of the Result message (0x09) that this server sends.

    The client's Result handler 0x5cd820 switches on the code through the table at 0x5d0618. Other
    codes refuse a logon (1, 2, 3, 5, 6, 7, 0x67, 0x68, 0x69, 0x6d, each with its own dialog text)
    or answer a private chat invitation between two clients (0x64, 0x65).
    """

    # Answer to CheckFileVersion: the client is up to date, no patch download (branch 0x5cffe1).
    NO_PATCH_NEEDED = 9

    # Answer to Logon: the player is in (branch 0x5cdaaa, LogonRetval 0 at 0x5cde49).
    LOGON_ACCEPTED = 0x66


# The first field of every Logon. The game always writes 14 (constructor 0x5e1d50); most likely
# the version of the lobby protocol. A different value is logged, not refused.
LOGON_PROTOCOL_VERSION = 14

# Groups: rooms, zones and games are all groups in this protocol.

# Parent id 0 in a group announcement tells the receiver "you are now in this group" (handler
# 0x5d2790); any other parent shows the group inside that parent, which the receiver must be in.
JOINED_GROUP_PARENT_ID = 0

# Group id 0 in an AddGroupToGroup sent between two clients is a private chat invitation. It never
# names a real group on the server.
PRIVATE_CHAT_GROUP_ID = 0

# A host opens its game with this group id and leaves the choice of the real id to the server
# (seen in a run: CreateGame 0x5ca7d0 sends 0xffffffff).
SERVER_ASSIGNS_GROUP_ID = 0xFFFFFFFF

# This server's choice: game ids start here, far above the room and zone ids the client uses on
# its own (1 to 3 for the lobby rooms, 30 to 38 for the game zones), so the two never collide.
FIRST_GAME_GROUP_ID = 0x10000


class GroupFlag(IntFlag):
    """Bits of a group announcement's flags that add optional parts after its three texts.

    Read from the constructors 0x5e2d60 (AddGroupToGroup) and 0x5e3b40 (AddGroupToGroup2). A game
    opened by the client carries 0x840401; the bits not listed here are relayed untouched.
    """

    # A game block follows: the host's address, the DirectPlay session and a few numbers.
    HAS_GAME_BLOCK = 0x400

    # A description follows: u32 length, then that many bytes.
    HAS_DESCRIPTION = 0x40000

    # The receiver asks for the group's images with GetImageList.
    REQUESTS_IMAGES = 0x4000000


# Size of the game block, socket address included, per announcement type.
GAME_BLOCK_SIZE_BY_MESSAGE_TYPE = {
    MessageType.ADD_GROUP_TO_GROUP: 0x2C,
    MessageType.ADD_GROUP_TO_GROUP_2: 0x34,
}

# Timing of the reliable layer, in milliseconds.

# How long to wait for an acknowledgement before sending a block again, per attempt; the last
# value repeats (table at 0x65d498, read by 0x6017c0).
RETRY_INTERVALS_MS = (750, 1500, 3000, 5000)

# A peer that stays silent this long is gone (the client's own default, connection field +0x2a8).
DEFAULT_SILENCE_TIMEOUT_MS = 60_000

# This server's choice: say something to every client at least this often. The client sends its
# keepalive every 5 seconds (0x604da4); staying below that keeps both timers far from expiring.
KEEPALIVE_INTERVAL_MS = 3_000

# This server's choice: how long a connection closed by the client is kept for retransmissions of
# the server's own CloseConnection before it is forgotten.
CLOSING_LINGER_MS = 10_000

# The client sends to one peer at most every 250 ms (constructor argument 0xfa). Checking for due
# retries at the same pace keeps the server's retries as punctual as the game's.
HOUSEKEEPING_INTERVAL_SECONDS = 0.25

# The client picks its connection nonce as rand() % 255 + 1 (0x604404); the server does the same.
CONNECTION_NONCE_MIN = 1
CONNECTION_NONCE_MAX = 255
