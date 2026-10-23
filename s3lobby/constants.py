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
    ADD_PLAYER_TO_GROUP_WITH_CLAN = 0x25
    ADD_PLAYER_TO_GROUP_WITH_FLAGS = 0x26
    GROUP_COUNTERS = 0x1A
    TEXT_ID_LIST = 0x14
    SERVER_STATISTICS = 0x1B


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

    The client's Result handler 0x5cd820 switches on the code through the table at 0x5d0618. A
    logon refusal shows a dialog with a fixed text from the game's text archive; the text ids are
    noted here (id 0x900nn is text nn of GFX/siedler3_09.*.dat). Codes 0x64 and 0x65 answer a
    private chat invitation and travel between two clients, never from the server.
    """

    # Answer to CheckFileVersion: the client is up to date, no patch download (branch 0x5cffe1).
    NO_PATCH_NEEDED = 9

    # Answer to Logon: the player is in (branch 0x5cdaaa, LogonRetval 0 at 0x5cde49).
    LOGON_ACCEPTED = 0x66

    # Logon refusals.
    # 0x90069 "The server refuses your combination of username and password."
    UNKNOWN_NAME_OR_ACCESS_CODE = 3
    # 0x9009d "Another player with this name or serial number is already logged in ..."
    ALREADY_LOGGED_ON = 6
    # 0x9009e "The chosen player name is invalid. Only letters from A to Z ... and spaces"
    INVALID_PLAYER_NAME = 0x67
    # 0x900ee "The maximum number of users for this server has been reached."
    SERVER_FULL = 0x68
    # 0x900f3 "On account of unacceptable behavior, you have been barred from the Lobby ..."
    BANNED = 0x69
    # 0x90139 "You're no clan member. ..." (the player ticked the clan box without being in one)
    NOT_A_CLAN_MEMBER = 0x6D

    # Group answers.
    # Dialog "Der Wechsel in die Gruppe hat nicht geklappt" (joining a group failed).
    GROUP_CHANGE_FAILED = 0x0B
    # Dialog "Das Passwort stimmte nicht" (wrong group password); value = the group id.
    WRONG_GROUP_PASSWORD = 0x62

    # Answer to GameStatus: 0x90148 "Game status stored at the Lobby server." (0x540a3e). The game
    # shows answers to GameStatus only during a ladder game (0x540a18) and ignores them otherwise.
    GAME_STATUS_STORED = 0x6C
    # Answer to GameStatus of a ladder game: 0x90149 "Game status inconsistencies. Game not valid for
    # ladder." (Result handler 0x5d056c, shown in the match at 0x540b89). This server keeps no
    # ladder, so it gives this answer to every ladder report.
    GAME_NOT_VALID_FOR_LADDER = 0x6A


class GameStatusFlag(IntFlag):
    """Bits of GameStatus's flags (Logic.SendGameStatus 0x540500)."""

    # The match is a ladder game (game settings +0x2a, 0x540770..0x54077e).
    LADDER_GAME = 0x1
    # Not valid for the ladder: a ladder slot has no lobby player id (0x5405ec, 0x5407b1), or the
    # lobby manager marked the game (its byte +0x675, 0x54078a).
    NOT_VALID_FOR_LADDER = 0x2


class PlayerFlag(IntFlag):
    """Bits of the player flags in AddPlayerToGroup 0x26 (handler 0x5d6520, read by 0x5e7900)."""

    # The room member list draws the player's row in another colour (0x449e09..0x449eb5); with the
    # clan name "BB" in yet another (inferred: Blue Byte staff). s3lobby uses it to mark admins.
    HIGHLIGHTED = 0x1


class MatchPlayerFlag(IntFlag):
    """Bits of a GameStatus player record's flags (0x540625..0x540680)."""

    # The slot is a computer opponent (slot state 2; the setup names the states at 0x48fd26..0x490309:
    # 0 free, 1 human, 2 computer opponent, 4 closed).
    COMPUTER = 0x1
    # The slot is in state 3, which the setup never shows (inferred: a human player who left).
    SLOT_STATE_3 = 0x2
    # The slot has no player number in the match (its number is 0xff, 0x540631).
    NO_PLAYER_NUMBER = 0x4


# GameStatus's winning team while more than one team is still in the match (0x540747..0x540768).
NO_WINNING_TEAM = 0xFFFFFFFF


class Race(IntEnum):
    """A slot's race in a match report (MatchReportPlayer.race). Inferred from the setup's race
    choices (0x48c2a3..0x48c2df) and the per-race tables the game indexes with the value (0x4dd2fc)."""

    ROMANS = 0
    EGYPTIANS = 1
    ASIANS = 2
    AMAZONS = 3


class LogonFlag(IntFlag):
    """Bits of the Logon message's flags (built in CLobbyClient.Logon 0x5dd0b0..0x5dd0e4)."""

    # The player pressed "create player" in the logon dialog (dialog flag +0xf0, set at 0x43a35c).
    # This server never creates accounts from the game: they are made on the server side.
    CREATE_PLAYER = 0x1
    # Set when the game's edition setting is 1; meaning unknown.
    EDITION_ONE = 0x2
    # The "Clan" box in the logon dialog (registry ClanFlag): show the player as a clan member.
    SHOW_AS_CLAN_MEMBER = 0x4


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

# The groups the client joins on its own, with the names its UI uses for them. The four lobby tabs
# (labels 0x90097..0x9009a, buttons 0x43eb09..0x43ebb1) and the nine game zones on the GameZone tab,
# one per bit of the registry value GameZoneSelection (0x457030; labels set at 0x441313).
HOME_GROUP_ID = 1
CHAT_ZONE_GROUP_ID = 2
GAME_ZONE_GROUP_ID = 3
MESSAGE_ZONE_GROUP_ID = 4
LOBBY_TAB_GROUP_NAMES = {
    HOME_GROUP_ID: "Home",
    CHAT_ZONE_GROUP_ID: "ChatZone",
    GAME_ZONE_GROUP_ID: "GameZone",
    MESSAGE_ZONE_GROUP_ID: "MessageZone",
}
GAME_ZONE_NAMES = {
    30: "League 1 vs 1",
    31: "Free-for-all",
    32: "1 vs 1",
    33: "2 vs 2",
    34: "3-4 vs 3-4",
    35: "5+ vs 5+",
    36: "3 or more teams",
    37: "Human vs Computer",
    38: "Other games",
}

# A host opens its game with this group id and leaves the choice of the real id to the server
# (seen in a run: CreateGame 0x5ca7d0 sends 0xffffffff).
SERVER_ASSIGNS_GROUP_ID = 0xFFFFFFFF

# This server's choice of id ranges for the groups it makes, far above the ids the client uses on
# its own (1 to 4 for the tabs, 30 to 38 for the game zones), so nothing ever collides:
# chat themes and MessageZone themes add their database id to their base, MessageZone language
# groups their position in BOARD_LANGUAGES, and games and chat groups players open count up from
# FIRST_GAME_GROUP_ID.
CHAT_THEME_GROUP_ID_BASE = 0x10000
BOARD_LANGUAGE_GROUP_ID_BASE = 0x20000
BOARD_THEME_GROUP_ID_BASE = 0x30000
FIRST_GAME_GROUP_ID = 0x100000

# Text ids of MessageZone messages: this base plus the message's database id, far from the news ids.
BOARD_MESSAGE_TEXT_ID_BASE = 0x10000

# Offered on the ChatZone tab when no chat theme is configured: without any theme players could not
# open chat groups at all.
DEFAULT_CHAT_THEME_NAME = "General"
DEFAULT_CHAT_THEME_DESCRIPTION = "Talk about anything"


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

    # The group receives the Home tab statistics (0x1b) (0x5d997d).
    RECEIVES_STATISTICS = 0x80

    # Joining asks for a password (0x442d1a); also set on a game not yet listed in a zone (0x455319).
    HAS_PASSWORD = 0x1000


class GroupUpdateMask(IntFlag):
    """Which fields of an UpdateGroup the receiver takes over (0x5dbf80)."""

    NAME = 0x1
    SECOND_TEXT = 0x2
    THIRD_TEXT = 0x4
    FLAGS = 0x8
    MAX_PLAYERS = 0x10
    GAME_BLOCK = 0x20
    DESCRIPTION = 0x40


# A group's maximum player count of this value means no limit; chat groups are opened with it.
UNLIMITED_PLAYERS = 0xFFFFFFFF

# Bit 0 of the third GroupCounters value marks a full group (0x44b635, 0x442cf2).
GROUP_COUNTERS_FULL = 0x1


# Size of the game block, socket address included, per announcement or update type: the 2 variants
# carry two more u32 values (5 instead of 3). After the address come the DirectPlay session GUID,
# the game version and the checksum of the game's program (low half first); the long block adds the
# Amazons value and the ladder type (constructors 0x5e3b40, 0x5ea320).
GAME_BLOCK_SIZE_BY_MESSAGE_TYPE = {
    MessageType.ADD_GROUP_TO_GROUP: 0x2C,
    MessageType.ADD_GROUP_TO_GROUP_2: 0x34,
    MessageType.UPDATE_GROUP: 0x2C,
    MessageType.UPDATE_GROUP_2: 0x34,
}

SESSION_GUID_SIZE = 16

# Bit 0 of the game block's Amazons value: the host allowed the Amazons, so the game needs the
# "Quest of the Amazons" add-on; the game list marks such games (0x44b1e6..0x44b232, text 0x9016b).
AMAZONS_ALLOWED_BIT = 0x1


class LadderType(IntEnum):
    """The last value of a long game block: what the host picked under "Normal Game",
    "Ladder: 1 on 1" and "Ladder Game: Clans" in the game setup (radio buttons 0x4848b4..0x48493b,
    passed by CreateGame at 0x46180b..0x46181e). The game list shows "Liga" and "Clanliga" for 1 and
    2 (0x44b843, 0x44b97a). Ladder games were ranked by Blue Byte's server; this server keeps no
    ladder."""

    NONE = 0
    INDIVIDUAL = 1
    CLANS = 2


# A game's description is a serialized message of its own, typed 0 (CLMGameDescription, constructor
# 0x4504e0). It only ever travels inside a group's description, never as a lobby message.
GAME_DESCRIPTION_MESSAGE_TYPE = 0

# The languages of the game's text archive (GFX/siedler3_09.*.dat), in the archive's order. The game
# keeps the index of its language in its configuration (offset 0x28) and clamps it to the archive's
# language count at start (0x42e94b..0x42e97b); CheckFileVersion reports it as its third value.
GAME_LANGUAGES = ("German", "English", "Italian", "French", "Polish", "Spanish", "Korean", "Japanese")

# The languages the MessageZone tab knows. The client looks for a group named "MessageThemes"
# followed by its own language, and falls back to English (0x453831, 0x4539ce).
BOARD_LANGUAGES = ("German", "English", "Italian", "French", "Polish", "Spanish", "Korean")
BOARD_FALLBACK_LANGUAGE = "English"
BOARD_LANGUAGE_GROUP_PREFIX = "MessageThemes"

# The Home tab shows one of two news texts, picked by the language of the game (0x45351b..0x453606):
# it looks for text id 0x91 in a German game and 0x92 in any other, in the list of text ids the
# server sends for the Home group, and falls back to the list's first entry.
NEWS_AUDIENCE_GERMAN = "german"
NEWS_AUDIENCE_OTHER = "other"
NEWS_AUDIENCES = (NEWS_AUDIENCE_GERMAN, NEWS_AUDIENCE_OTHER)
NEWS_TEXT_ID_BY_AUDIENCE = {NEWS_AUDIENCE_GERMAN: 0x91, NEWS_AUDIENCE_OTHER: 0x92}

# A text message (0x15) counts as delivered only with a non-empty title (0x5cb642); without one the
# page stays at "Loading message" for good. This title answers ids the server does not know.
UNKNOWN_TEXT_TITLE = "Not available"

# Keeps a text message within one unfragmented datagram on any network; longer texts are cut.
MAX_TEXT_MESSAGE_BYTES = 1200

# The banner's url must start like this; the game skips the first six characters and splits the rest
# into host and path at the first slash (0x449938).
BANNER_URL_PREFIX = "ftp://"

# Logon attempts: a token bucket per IP and per account name, drawn on by failed attempts only, so
# a LAN party behind one router can log on together while guessing access codes stays hopeless.
LOGON_FAILURE_BURST = 10
LOGON_FAILURES_PER_SECOND = 10 / (15 * 60)

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

# How often the server looks for announcements queued in the database by another process.
ANNOUNCEMENT_POLL_INTERVAL_SECONDS = 1.0

# How often the server looks up its public host name again: a home router's public address changes
# now and then, and a dynamic DNS name follows it.
PUBLIC_HOST_REFRESH_SECONDS = 300.0

# How often the server writes its live lobby (s3lobby/live.py) to the database when it changed, and
# how often it writes it unchanged, as a sign of life. The web interface takes a state older than
# LIVE_STATE_STALE_SECONDS as a server that is not running.
LIVE_STATE_WRITE_INTERVAL_SECONDS = 1.0
LIVE_STATE_HEARTBEAT_SECONDS = 10.0
LIVE_STATE_STALE_SECONDS = 30.0

# Group id of a server line sent to every player: the line shows in whatever room the player is
# in; the id only names the group in the client's chat log file (0x5d2420).
ANNOUNCEMENT_GROUP_ID = 0

# Server line for the players of a ladder game: Blue Byte's server ranked ladder games, this one
# keeps no ladder.
LADDER_NOT_SUPPORTED_TEXT = "Ladder games are not supported on this server: you can play, but the result is not ranked."

# The client sends to one peer at most every 250 ms (constructor argument 0xfa). Checking for due
# retries at the same pace keeps the server's retries as punctual as the game's.
HOUSEKEEPING_INTERVAL_SECONDS = 0.25

# The client picks its connection nonce as rand() % 255 + 1 (0x604404); the server does the same.
CONNECTION_NONCE_MIN = 1
CONNECTION_NONCE_MAX = 255

# Limits against hostile or broken senders. All of them are this server's choice: the game stays
# far below each, and each keeps one sender from using up the server's memory or time, or from
# using the server to flood a third party whose address it forged.

# Connections in total, and from one IP. Several players behind one router share an IP, so the
# per IP limit leaves room for a LAN party.
MAX_CONNECTIONS = 1000
MAX_CONNECTIONS_PER_IP = 32

# Datagrams per second from one address, as a token bucket: the game sends at most one block per
# 250 ms plus acknowledgements, so a steady 50 per second with bursts of 200 is never reached.
DATAGRAMS_PER_SECOND_PER_ADDRESS = 50
DATAGRAM_BURST_PER_ADDRESS = 200

# Messages in one datagram. The largest block the game sends joins its rooms and nine zones at once,
# about a dozen messages.
MAX_MESSAGES_PER_BLOCK = 256

# How far ahead of the oldest missing block a client's block id may be. The game numbers its blocks
# one by one and waits for acknowledgements, so it is never more than a few ahead; the window keeps
# the memory for duplicate detection bounded.
CLIENT_BLOCK_ID_WINDOW = 256

# A client that leaves this many of the server's blocks unacknowledged is not reading them; the
# connection ends instead of queueing more.
MAX_UNACKNOWLEDGED_SERVER_BLOCKS = 256

# A connection whose first block from the server is not acknowledged within this time ends. A real
# game acknowledges within a second; an OpenConnection with a forged sender address never is, and
# without this limit the server would keep sending retries to the forged address for a minute.
UNCONFIRMED_CONNECTION_TIMEOUT_MS = 15_000

# A warning about one sender is logged at most this often, so a flood cannot flood the log too.
WARNING_INTERVAL_SECONDS = 10.0

# Text a client sends for other clients to see (group names, descriptions, the map name inside a
# game's description) is passed on only within these limits and without control characters. The
# game's own dialogs stay well below them; the limits keep a crafted client from feeding the other
# players' 1998 game clients oversized or unexpected input through this server.
MAX_GROUP_TEXT_LENGTH = 64
MAX_GROUP_DESCRIPTION_BYTES = 512
MAX_CHAT_COMMAND_LENGTH = 256
# This server's limit for an announcement or the welcome line. ChatCommandAnswer wraps it into
# lines the chat window shows; 256 characters stay within its line cap.
MAX_ANNOUNCEMENT_LENGTH = 256

# Groups one player may have open at a time (games and chat groups it created).
MAX_OPEN_GROUPS_PER_PLAYER = 4
