"""Everything the lobby keeps between restarts, in one database: a SQLite file, or a MySQL or
MariaDB database (see s3lobby/backends/ for the locations, migrations.py for the schema).

- accounts and their access codes: a player logs on with the account name as player name and an
  access code as password. Codes are random strings the server generates (with the command line or
  the web interface); only a salted scrypt hash of each is stored.
- clans and who belongs to them
- the news on the lobby's Home tab, and the message board of the MessageZone tab
- the chat themes listed on the ChatZone tab
- lobby sessions and games, for the statistics table on the Home tab
- the game reports clients send after a match
- settings, such as the player limit or the welcome message
- announcements queued for the running server, and the server's live lobby for the web interface

Accounts are only ever created on the server side (the command line or the web interface). The
game's own "create player" button is refused at logon.

All times are stored as UTC text in ISO 8601 format, which every backend compares correctly as text.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from s3lobby.backends import (
    BackendError,
    ConstraintViolation,
    DatabaseError,
    Row,
    describe_location,
    open_backend,
    placeholders,
)
from s3lobby.constants import (
    ALL_LANGUAGES,
    BANNER_URL_PREFIX,
    BOARD_LANGUAGE_GROUP_ID_BASE,
    BOARD_LANGUAGES,
    CHAT_THEME_GROUP_ID_BASE,
    DEFAULT_CHAT_THEME_DESCRIPTION,
    DEFAULT_CHAT_THEME_NAME,
    FIRST_PRINTABLE_CHARACTER,
    GAME_LANGUAGES,
    MAX_ANNOUNCEMENT_LENGTH,
    MAX_GROUP_TEXT_LENGTH,
    MAX_TEXT_MESSAGE_BYTES,
    NEWS_AUDIENCES,
    TEXT_ENCODING,
    LadderState,
    SeasonCounts,
)
from s3lobby.ladder import Evaluation, LadderKey, LadderMembership, LadderSettings, ResultPlayer, Season
from s3lobby.live import LiveLobbyState, StoredLiveState
from s3lobby.migrations import Migration, MigrationError, MigrationStatus, current_version, migrate, read_status

# Every table of the schema, each after the tables its foreign keys point to: the order to copy
# them in, and the reverse order to empty them in.
TABLES_IN_DEPENDENCY_ORDER = (
    "accounts",
    "access_codes",
    "clans",
    "clan_members",
    "news",
    "chat_themes",
    "board_themes",
    "board_messages",
    "lobby_sessions",
    "games",
    "game_reports",
    "game_players",
    "ladder_results",
    "ladder_result_players",
    "ladder_seasons",
    "ladder_memberships",
    "ladder_events",
    "settings",
    "pending_announcements",
    "live_state",
    "pending_kicks",
    "web_sessions",
)

# Tables a migration fills with starting rows in every new database (0014: the default chat
# themes). A copy replaces them in its target instead of refusing it as not empty.
TABLES_SEEDED_BY_MIGRATIONS = ("chat_themes",)

# Shown for a game's player whose account was deleted since.
DELETED_ACCOUNT_NAME = "(deleted account)"
# The ladder states a result is stored in; WAITING is not a result.
STORED_LADDER_STATES = frozenset({LadderState.COUNTED, LadderState.DISPUTED, LadderState.VOID})
# Lengths of the ladder's columns that hold text the server writes (VARCHAR in MySQL).
MAX_LADDER_REASON_LENGTH = 255
MAX_LADDER_EVENT_KEY_LENGTH = 191
# The longest map name a ladder result keeps (VARCHAR(255) in MySQL); the game's are far shorter.
MAX_MAP_NAME_LENGTH = 255

# The game's own rule for player names (refusal text 0x9009e): letters A to Z in either case, and
# single spaces between words.
PLAYER_NAME_PATTERN = re.compile(r"[A-Za-z]+( [A-Za-z]+)*")

# scrypt parameters for access code hashes: about 16 MB and a few tens of milliseconds per check,
# which only logons pay, and those are rate limited.
SCRYPT_PREFIX = "scrypt"
SCRYPT_COST = 2**14
SCRYPT_BLOCK_SIZE = 8
SCRYPT_PARALLELISM = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_DIGEST_BYTES = 32
SCRYPT_HASH_FIELD_COUNT = 6
HASH_FIELD_SEPARATOR = "$"

# Access codes: long enough to be safe from guessing, short enough to type into the game's password
# field. The alphabet leaves out characters that are easy to mix up (0/O, 1/I/L). It has capitals
# only, so comparing codes without regard to case (normalize_access_code) costs no strength.
ACCESS_CODE_LENGTH = 12

# Web interface sessions: a logon lasts at most WEB_SESSION_LIFETIME however busy it is, and ends
# earlier after WEB_SESSION_IDLE_TIMEOUT without a request. The live lobby page polls every few
# seconds, so last_seen_at is written at most every WEB_SESSION_TOUCH_INTERVAL. Rows are deleted
# once they are a few lifetimes old.
WEB_SESSION_TOKEN_BYTES = 32
WEB_SESSION_LIFETIME = timedelta(hours=12)
WEB_SESSION_IDLE_TIMEOUT = timedelta(hours=2)
WEB_SESSION_TOUCH_INTERVAL = timedelta(minutes=1)
WEB_SESSIONS_KEPT_LIFETIMES = 2
ACCESS_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class Setting(Enum):
    """Settings stored in the database, by the key they are stored under. default is the value used
    when none is stored.

    Each member's value is its key rather than its default: an Enum makes members with equal values
    aliases of the first one, and several settings share the default "0".
    """

    # 0 means no limit; above it, logons are refused with the game's "server full" message.
    MAX_PLAYERS = "max_players"
    # The original refused a second logon with the same serial number as an illegal copy. Off by
    # default: on a LAN, one household's copies often share a serial.
    UNIQUE_SERIAL_NUMBERS = "unique_serial_numbers"
    # The game's "Create Player" button makes an account with the name and password typed in the
    # game. Off by default: accounts are made by admins, who hand out access codes.
    ALLOW_CREATE_PLAYER = "allow_create_player"
    # Shown in the chat as [Server] right after a player enters the lobby; empty for none.
    WELCOME_MESSAGE = "welcome_message"
    # The advert banner on the Home tab: the game downloads banner_url by anonymous FTP whenever the
    # two ids differ from the ones it saw last. Empty url for no banner.
    BANNER_URL = "banner_url"
    BANNER_ID = "banner_id"
    BANNER_SUB_ID = "banner_sub_id"
    # Admins' rows in the room member lists are drawn in another colour (AddPlayerToGroup 0x26).
    # On by default since a run showed 0x26 working (a highlighted clan's member listed in colour);
    # it was off while no run had shown the message.
    MARK_ADMINS = "mark_admins"
    # After each logon from the internet, probe whether other players can reach the player
    # directly (reachability.py). Off by default: no run with the real game has seen the probe.
    CHECK_REACHABILITY = "check_reachability"
    # The ladder (s3lobby/ladder.py, docs/ladder.md). LADDER_ENABLED tells the game a valid result
    # was stored (Result 0x6c) instead of "not valid for ladder" (0x6a), and drops the "not ranked"
    # line; off until a run with the real game has shown the answer. The ladder evaluates every
    # ladder game either way.
    LADDER_ENABLED = "ladder_enabled"
    LADDER_MINIMUM_MINUTES = "ladder_minimum_minutes"
    LADDER_REPORT_TIMEOUT_MINUTES = "ladder_report_timeout_minutes"
    LADDER_PAIR_DAILY_LIMIT = "ladder_pair_daily_limit"
    LADDER_START_RATING = "ladder_start_rating"
    LADDER_K = "ladder_k"
    LADDER_K_PROVISIONAL = "ladder_k_provisional"
    LADDER_PROVISIONAL_GAMES = "ladder_provisional_games"
    LADDER_ACTIVE_DAYS = "ladder_active_days"
    LADDER_CLAN_MEMBER_DAYS = "ladder_clan_member_days"
    LADDER_INACTIVE_DAYS = "ladder_inactive_days"
    LADDER_MAX_CUSTOM_SEASONS = "ladder_max_custom_seasons"
    # The lobby texts the ladder writes while it is switched on: how many board messages its own
    # theme keeps, the standings under the news, and a line with the player's place at each logon
    # (off by default: noise for players who do not play the ladder).
    LADDER_BOARD_MESSAGES = "ladder_board_messages"
    LADDER_NEWS_STANDINGS = "ladder_news_standings"
    LADDER_LOGON_LINE = "ladder_logon_line"

    @property
    def key(self) -> str:
        """The name the setting is stored under in the settings table."""
        return self.value

    @property
    def default(self) -> str:
        """The value used while none is stored."""
        return SETTING_DEFAULTS[self]


SETTING_DEFAULTS = {
    Setting.MAX_PLAYERS: "0",
    Setting.UNIQUE_SERIAL_NUMBERS: "false",
    Setting.ALLOW_CREATE_PLAYER: "false",
    Setting.WELCOME_MESSAGE: "",
    Setting.BANNER_URL: "",
    Setting.BANNER_ID: "0",
    Setting.BANNER_SUB_ID: "0",
    Setting.MARK_ADMINS: "true",
    Setting.CHECK_REACHABILITY: "false",
    Setting.LADDER_ENABLED: "false",
    Setting.LADDER_MINIMUM_MINUTES: "10",
    Setting.LADDER_REPORT_TIMEOUT_MINUTES: "10",
    Setting.LADDER_PAIR_DAILY_LIMIT: "3",
    Setting.LADDER_START_RATING: "1000",
    Setting.LADDER_K: "16",
    Setting.LADDER_K_PROVISIONAL: "32",
    Setting.LADDER_PROVISIONAL_GAMES: "10",
    Setting.LADDER_ACTIVE_DAYS: "30",
    Setting.LADDER_CLAN_MEMBER_DAYS: "3",
    Setting.LADDER_INACTIVE_DAYS: "60",
    Setting.LADDER_MAX_CUSTOM_SEASONS: "50",
    Setting.LADDER_BOARD_MESSAGES: "20",
    Setting.LADDER_NEWS_STANDINGS: "true",
    Setting.LADDER_LOGON_LINE: "false",
}

# A label tells an account's access codes apart, such as "laptop"; the reason for a ban is shown to
# a player who is online when banned, and kept as a note for admins.
MAX_ACCESS_CODE_LABEL_LENGTH = 64
MAX_BAN_REASON_LENGTH = 200
# The label of the access code that holds a password chosen in the game's create dialog.
CHOSEN_IN_GAME_LABEL = "chosen in the game"
# How a kick or ban line names the admin's reason (with_reason).
REASON_LABEL = "Reason"
NO_REASON_TEXT = "No reason given."

# What a player is told when their account is deleted while they are online.
ACCOUNT_REMOVED_KICK_REASON = "Your account was removed."

# live_state holds one row, the running server's lobby.
LIVE_STATE_ROW_ID = 1

TRUE_SETTING_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_SETTING_VALUES = frozenset({"0", "false", "no", "off"})
# The numeric settings travel to the game as u32 fields.
INTEGER_SETTINGS = frozenset({Setting.MAX_PLAYERS, Setting.BANNER_ID, Setting.BANNER_SUB_ID})
BOOLEAN_SETTINGS = frozenset(
    {
        Setting.UNIQUE_SERIAL_NUMBERS,
        Setting.ALLOW_CREATE_PLAYER,
        Setting.MARK_ADMINS,
        Setting.CHECK_REACHABILITY,
        Setting.LADDER_ENABLED,
        Setting.LADDER_NEWS_STANDINGS,
        Setting.LADDER_LOGON_LINE,
    }
)
MAX_INTEGER_SETTING = 0xFFFFFFFF
# The ladder's numbers are days, minutes, counts and Elo points. They get a far lower ceiling than
# the protocol's u32 values: a span of four billion days does not fit a datetime and would stop
# the ladder.
LADDER_INTEGER_SETTINGS = frozenset(
    {
        Setting.LADDER_MINIMUM_MINUTES,
        Setting.LADDER_REPORT_TIMEOUT_MINUTES,
        Setting.LADDER_PAIR_DAILY_LIMIT,
        Setting.LADDER_START_RATING,
        Setting.LADDER_K,
        Setting.LADDER_K_PROVISIONAL,
        Setting.LADDER_PROVISIONAL_GAMES,
        Setting.LADDER_ACTIVE_DAYS,
        Setting.LADDER_CLAN_MEMBER_DAYS,
        Setting.LADDER_INACTIVE_DAYS,
        Setting.LADDER_MAX_CUSTOM_SEASONS,
        Setting.LADDER_BOARD_MESSAGES,
    }
)
MAX_LADDER_SETTING = 100_000

LINE_BREAK = "\n"


@dataclass(frozen=True)
class Account:
    """An account, the only way into the lobby: its name is the player name the game logs on with.
    banned_until is None or the moment logons are refused until; ban_reason is told to a player who is
    online when banned and kept as a note for admins."""

    account_id: int
    name: str
    is_admin: bool
    banned_until: datetime | None
    ban_reason: str
    created_at: datetime | None = None
    last_logon_at: datetime | None = None

    def is_banned_at(self, moment: datetime) -> bool:
        """True while a ban is in force at the given moment; a ban ends by itself when it runs out."""
        return self.banned_until is not None and self.banned_until > moment


@dataclass(frozen=True)
class Clan:
    """A clan. is_highlighted lists its members with a green row in the game (see
    PlayerFlag.HIGHLIGHTED), for those who show the clan."""

    clan_id: int
    name: str
    is_highlighted: bool = False


@dataclass(frozen=True)
class News:
    """The Home tab's news for one audience, a title and a text (see LobbyDatabase.set_news)."""

    title: str
    body: str


@dataclass(frozen=True)
class ChatTheme:
    """A theme listed on the ChatZone tab; the chat groups players open there are listed under it."""

    theme_id: int
    name: str
    description: str
    # Shown only to games of this language (a name from GAME_LANGUAGES); ALL_LANGUAGES for every game.
    language: str = ALL_LANGUAGES


@dataclass(frozen=True)
class BuiltInChatTheme:
    """A ChatZone theme the server always lists, under a fixed group id; admins cannot change or
    remove it (constants.DEFAULT_CHAT_THEME_NAME says why)."""

    group_id: int
    name: str
    description: str


BUILT_IN_CHAT_THEMES = (
    BuiltInChatTheme(CHAT_THEME_GROUP_ID_BASE, DEFAULT_CHAT_THEME_NAME, DEFAULT_CHAT_THEME_DESCRIPTION),
)
# Admins' themes take CHAT_THEME_GROUP_ID_BASE plus their database id, so one with an id above this
# would leave the chat theme id range; it is not listed (a database would need tens of thousands
# of themes added to get there).
MAX_CHAT_THEME_ID = BOARD_LANGUAGE_GROUP_ID_BASE - CHAT_THEME_GROUP_ID_BASE - 1


def is_built_in_chat_theme_name(name: str) -> bool:
    """True for the name of a theme the server lists itself: a second "General" would be one too
    many in the game's list."""
    return any(theme.name.casefold() == name.strip().casefold() for theme in BUILT_IN_CHAT_THEMES)


@dataclass(frozen=True)
class BoardTheme:
    """A theme of the MessageZone board. language is one of BOARD_LANGUAGES: the game shows the
    themes of its own language, and the English ones when its language has none. A theme
    is_server_written when the server fills it itself (the ladder's texts): nobody posts into it."""

    theme_id: int
    language: str
    name: str
    description: str
    is_server_written: bool = False
    # Lowest first on the board; a hidden theme is kept but not sent to the game.
    position: int = 0
    is_hidden: bool = False


@dataclass(frozen=True)
class BoardMessage:
    """A message on the MessageZone board under one theme. author is empty for a message written
    on the server side; the lobby then shows the server's name as its byline."""

    message_id: int
    theme_id: int
    title: str
    body: str
    author: str
    posted_at: datetime
    # Lowest first in its theme; a hidden message is kept but not sent to the game.
    position: int = 0
    is_hidden: bool = False


@dataclass(frozen=True)
class AccessCodeRecord:
    """What is known about an access code without the code itself: only its hash is stored."""

    code_id: int
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def is_active(self) -> bool:
        """True until the code is revoked; a revoked code no longer logs on."""
        return self.revoked_at is None


@dataclass(frozen=True)
class GameRecord:
    """A game as the statistics keep it; host_name is empty when the host's account was removed.

    group_id and announcement are None for games stored before migration 0008. announcement is the
    game as the lobby last showed it, an AddGroupToGroup2 body (game block and description
    included, the host address zeroed); player_names are the accounts that joined it, host first.
    """

    game_id: int
    name: str
    host_name: str
    opened_at: datetime
    closed_at: datetime | None
    group_id: int | None = None
    announcement: bytes | None = None
    player_names: tuple[str, ...] = ()
    ladder_type: int | None = None


@dataclass(frozen=True)
class GamePlayerRecord:
    """A player who joined a game. account_id is None and name DELETED_ACCOUNT_NAME when the
    account was deleted since (the foreign key clears it). clan_id is the clan the player showed
    when joining, clan_joined_at since when the player was in it, and installation a number shared
    by the players of the game whose copies have one serial number (all None before migration 0010)."""

    name: str
    joined_at: datetime
    account_id: int | None = None
    clan_id: int | None = None
    clan_joined_at: datetime | None = None
    installation: int | None = None


@dataclass(frozen=True)
class GameReportRecord:
    """A stored match report with the bytes the game sent (a GameStatus body, a few hundred
    bytes at most). game_id is the game it belongs to, None for reports stored before migration
    0008 or for a game the server did not know."""

    report_id: int
    sender_name: str
    group_id: int
    size: int
    received_at: datetime
    report: bytes = b""
    game_id: int | None = None
    sender_account_id: int | None = None


@dataclass(frozen=True)
class LadderResultRecord:
    """A stored ladder result: what the ladder made of a game, with the game's name, who decided it
    (empty for the server) and when."""

    result_id: int
    evaluation: Evaluation
    game_name: str
    decided_at: datetime
    decided_by_name: str = ""


@dataclass(frozen=True)
class WebSession:
    """A live logon to the web interface: whose, and with which access code."""

    account_id: int
    access_code_id: int


@dataclass(frozen=True)
class PendingKick:
    """A player the running server is to remove from the lobby, with the reason it tells them."""

    account_id: int
    reason: str


@dataclass(frozen=True)
class AnnouncementRecord:
    """An announcement queued for the running server. sent_at is None until the server took it,
    or until a server start discarded it as stale. recipient_name is empty for every player;
    reached_count is how many players got it (None for announcements from before migration 0012)."""

    text: str
    created_at: datetime
    sent_at: datetime | None
    recipient_name: str = ""
    reached_count: int | None = None


@dataclass(frozen=True)
class PendingAnnouncement:
    """A server line the running server is to send: to every player, or to the account
    account_id only."""

    announcement_id: int
    text: str
    account_id: int | None = None


@dataclass(frozen=True)
class LobbyStatistics:
    """The six numbers of the Home tab's statistics table, in the order the game shows them."""

    players_online: int
    players_last_hour: int
    players_last_day: int
    games_open: int
    games_last_hour: int
    games_last_day: int


def is_whole_number_text(text: str) -> bool:
    """Digits 0 to 9 only. str.isdigit() also accepts characters such as "²", which int() refuses,
    so a check with it alone would let such input through to a server error."""
    return text.isascii() and text.isdigit()


def is_valid_player_name(name: str) -> bool:
    """True for a name the game itself accepts as player name: letters A to Z and single spaces."""
    return PLAYER_NAME_PATTERN.fullmatch(name) is not None


def check_game_text(label: str, text: str, max_length: int, allows_line_breaks: bool = False) -> None:
    """Raise DatabaseError unless the game can show the text: every character must exist in its
    code page, it must fit max_length characters, and it may hold no control characters (line
    breaks only where allows_line_breaks). Texts reach the game exactly as stored, so a text that
    passes here never makes the server fail while encoding it for a client."""
    # Encoding is the test: the game's code page (Windows-1252) has no place for most of Unicode, and
    # error.start points at the first character it could not encode.
    try:
        text.encode(TEXT_ENCODING)
    except UnicodeEncodeError as error:
        bad_character = text[error.start]
        raise DatabaseError(f"{label} contains {bad_character!r}, which the game cannot show") from error
    if len(text) > max_length:
        raise DatabaseError(f"{label} is {len(text)} characters long; the most is {max_length}")
    # Every character below the space is a control character (tab, line break, NUL and the like);
    # comparing strings compares their code points.
    for character in text:
        is_allowed_line_break = allows_line_breaks and character == LINE_BREAK
        if character < FIRST_PRINTABLE_CHARACTER and not is_allowed_line_break:
            raise DatabaseError(f"{label} contains the control character {character!r}")


def check_title(label: str, title: str) -> None:
    """A title or name: one line, and not empty, as the game shows nothing for an empty one."""
    if not title.strip():
        raise DatabaseError(f"{label} must not be empty")
    check_game_text(label, title, MAX_GROUP_TEXT_LENGTH)


def check_setting_value(setting: Setting, value: str) -> None:
    """Raise DatabaseError for a value the server could not use for this setting."""
    if setting in INTEGER_SETTINGS:
        if not is_whole_number_text(value) or int(value) > MAX_INTEGER_SETTING:
            raise DatabaseError(f"{setting.key} must be a whole number from 0 to {MAX_INTEGER_SETTING}")
        return
    if setting in LADDER_INTEGER_SETTINGS:
        if not is_whole_number_text(value) or int(value) > MAX_LADDER_SETTING:
            raise DatabaseError(f"{setting.key} must be a whole number from 0 to {MAX_LADDER_SETTING}")
        return
    if setting in BOOLEAN_SETTINGS:
        if value.strip().lower() not in TRUE_SETTING_VALUES | FALSE_SETTING_VALUES:
            raise DatabaseError(f"{setting.key} must be true or false")
        return
    if setting == Setting.BANNER_URL:
        if value:
            check_banner_url(value)
        check_game_text(setting.key, value, MAX_TEXT_MESSAGE_BYTES)
        return
    if setting == Setting.WELCOME_MESSAGE:
        check_game_text(setting.key, value, MAX_ANNOUNCEMENT_LENGTH)
        return
    # Reached only when a new Setting member was added without a check here: a programming error,
    # not bad input, so it is not a DatabaseError.
    raise AssertionError(f"no check for setting {setting.key}")


def check_banner_url(url: str) -> None:
    """Raise DatabaseError unless the game can fetch this url: ftp://HOST/PATH, the host without a
    port or a user, since the game takes it as it is and connects anonymously on port 21."""
    usage = f"{Setting.BANNER_URL.key} must look like {BANNER_URL_PREFIX}HOST/PATH, or be empty for no banner"
    if not url.startswith(BANNER_URL_PREFIX):
        raise DatabaseError(usage)
    host, slash, path = url[len(BANNER_URL_PREFIX):].partition("/")
    # partition returns an empty separator when there is no slash at all, so "ftp://host" fails
    # here as well as "ftp:///file" and "ftp://host/".
    if not host or not slash or not path:
        raise DatabaseError(usage)
    if ":" in host or "@" in host:
        raise DatabaseError(
            f"{Setting.BANNER_URL.key}: the game fetches from port 21 without a user, so the host must have neither"
        )


def ban_kick_reason(until: datetime, reason: str) -> str:
    """What a banned player who is online is told as they are removed."""
    return with_reason(f"You are banned until {until.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC", reason)


def with_reason(text: str, reason: str) -> str:
    """A kick or ban line with the admin's reason labelled as such, or a note that none was given:
    in one chat line, a reason after a bare colon was read as part of the date or the sentence."""
    if reason:
        return f"{text}. {REASON_LABEL}: {reason}"
    return f"{text}. {NO_REASON_TEXT}"


def moved_one_place(ids: list[int], item_id: int, is_up: bool) -> list[int]:
    """The ids with one of them a place up or down; unchanged at either end or when it is missing."""
    if item_id not in ids:
        return ids
    index = ids.index(item_id)
    if is_up:
        target = index - 1
    else:
        target = index + 1
    if target < 0 or target >= len(ids):
        return ids
    reordered = list(ids)
    reordered[index], reordered[target] = reordered[target], reordered[index]
    return reordered


def utc_now() -> datetime:
    """The current moment in UTC, the default clock of LobbyDatabase."""
    return datetime.now(timezone.utc)


def to_stored_time(moment: datetime) -> str:
    """A moment as stored: UTC in ISO 8601 to the second, so that comparing the text compares
    the times in every backend."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_stored_time(text: str) -> datetime:
    """A time from a column that always holds one."""
    return datetime.fromisoformat(text)


def like_pattern(search: str) -> str:
    """A LIKE pattern matching text that contains search, in lower case (compare with LOWER(column)).
    The wildcards % and _ and the escape character ! in search match only themselves (ESCAPE '!')."""
    escaped = search.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def from_optional_stored_time(text: str | None) -> datetime | None:
    """A time from a column that may be empty: a code never used, a game still open."""
    if text is None:
        return None
    return from_stored_time(text)


def to_optional_stored_time(moment: datetime | None) -> str | None:
    """A moment as stored, or None for a column left empty: a season that runs until it is ended."""
    if moment is None:
        return None
    return to_stored_time(moment)


def hash_access_code(access_code: str) -> str:
    """A salted scrypt hash, stored with its parameters: "scrypt$n$r$p$salt$hash" in hex.

    Access codes are random, but a slow, salted hash still makes a stolen database useless for
    guessing them, and keeping the parameters with each hash lets them be raised later.
    """
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = scrypt_digest(access_code, salt, SCRYPT_COST, SCRYPT_BLOCK_SIZE, SCRYPT_PARALLELISM)
    fields = (SCRYPT_PREFIX, SCRYPT_COST, SCRYPT_BLOCK_SIZE, SCRYPT_PARALLELISM, salt.hex(), digest.hex())
    return HASH_FIELD_SEPARATOR.join(str(field) for field in fields)


def is_matching_access_code(access_code: str, stored_hash: str) -> bool:
    """True when the access code hashes to the stored hash; compares in constant time."""
    fields = stored_hash.split(HASH_FIELD_SEPARATOR)
    if len(fields) != SCRYPT_HASH_FIELD_COUNT or fields[0] != SCRYPT_PREFIX:
        return False
    _, cost, block_size, parallelism, salt_hex, digest_hex = fields
    # The parameters are read from the stored hash, not from the SCRYPT_ constants, so hashes made
    # before the constants were raised still check correctly.
    try:
        expected_digest = bytes.fromhex(digest_hex)
        salt = bytes.fromhex(salt_hex)
        offered_digest = scrypt_digest(access_code, salt, int(cost), int(block_size), int(parallelism))
    except ValueError:
        # A damaged hash matches no code; it must not turn a logon into a server error.
        return False
    return hmac.compare_digest(offered_digest, expected_digest)


def normalize_access_code(access_code: str) -> str:
    """The form a code is hashed in: capitals, so a player may type it in any case."""
    return access_code.upper()


def scrypt_digest(access_code: str, salt: bytes, cost: int, block_size: int, parallelism: int) -> bytes:
    """The scrypt digest of a normalized access code; used both for storing a hash and for
    checking a code against one."""
    # Normalizing here, where both storing and checking pass, keeps the two from ever disagreeing.
    # Bytes Windows-1252 leaves undefined arrive from the game as surrogates (wire.read_string),
    # which plain UTF-8 refuses to encode; surrogateescape turns them back into their bytes, so
    # such a password hashes instead of raising.
    return hashlib.scrypt(
        normalize_access_code(access_code).encode("utf-8", errors="surrogateescape"),
        salt=salt,
        n=cost,
        r=block_size,
        p=parallelism,
        dklen=SCRYPT_DIGEST_BYTES,
    )


def hash_session_token(token: str) -> str:
    """Session tokens are long and random, so a plain SHA-256 suffices: there is nothing to guess."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_access_code() -> str:
    """A new random access code of ACCESS_CODE_LENGTH characters from ACCESS_CODE_ALPHABET."""
    return "".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(ACCESS_CODE_LENGTH))


class LobbyDatabase:
    """The lobby's database. One instance per process and thread; all calls come from the event
    loop's thread, and each call is short, so they run synchronously."""

    def __init__(
        self, location: str | Path, clock: Callable[[], datetime] = utc_now, applies_migrations: bool = True
    ) -> None:
        """location is a SQLite file, ":memory:" for a SQLite database that lives only as long as
        this object, or mysql://user@host/name (s3lobby/backends/). The migrations the database is
        missing are applied, and listed in applied_migrations; applies_migrations False opens it
        as it is, which only suits looking at its migration status."""
        self._clock = clock
        self.location = str(location)
        self.applied_migrations: list[Migration] = []
        try:
            self._backend = open_backend(self.location)
        except BackendError as error:
            raise DatabaseError(str(error)) from error
        if not applies_migrations:
            return
        try:
            self.applied_migrations = migrate(self._backend, to_stored_time(clock()))
        except (BackendError, MigrationError) as error:
            # The caller never gets this object, so nobody else could close the connection.
            self._backend.close()
            raise DatabaseError(str(error)) from error

    def close(self) -> None:
        """Close the connection to the database; the object cannot be used afterwards."""
        self._backend.close()

    @property
    def dialect(self) -> str:
        """The SQL dialect of the backend: "sqlite" or "mysql"."""
        return self._backend.dialect

    @property
    def description(self) -> str:
        """The location as it may be shown in logs: without a password."""
        return describe_location(self.location)

    @property
    def schema_version(self) -> int:
        """The number of the newest migration applied to the database."""
        return current_version(self._backend)

    def read_migration_status(self) -> MigrationStatus:
        """Which migrations are applied and which are still to apply, without changing anything.
        Raises DatabaseError when the database does not fit this s3lobby's migrations."""
        try:
            return read_status(self._backend)
        except MigrationError as error:
            raise DatabaseError(str(error)) from error

    def copy_to(self, target: LobbyDatabase) -> dict[str, int]:
        """Copy every row into target, which must be empty and at the same schema version; ids
        are kept, so every reference stays intact. Returns the rows copied per table. This moves a
        lobby from SQLite to MySQL or back; no server may write to either database meanwhile.
        The starting rows a migration puts into a new database (TABLES_SEEDED_BY_MIGRATIONS) do
        not count as content: the source's rows replace them."""
        if target.schema_version != self.schema_version:
            raise DatabaseError(
                f"the target has schema version {target.schema_version}, the source {self.schema_version}"
            )
        tables_with_rows = [
            table
            for table in TABLES_IN_DEPENDENCY_ORDER
            if table not in TABLES_SEEDED_BY_MIGRATIONS and target._has_rows(table)
        ]
        if tables_with_rows:
            raise DatabaseError(f"the target is not empty: {', '.join(tables_with_rows)} already hold rows")
        copied_rows = {}
        # One transaction on the target: a copy that fails halfway leaves it empty, ready for another try.
        with target._backend.transaction():
            # The target is otherwise empty, so these rows are only what its migrations put there;
            # the source has its own, kept or changed by its admins.
            for table in TABLES_SEEDED_BY_MIGRATIONS:
                target._backend.execute(f"DELETE FROM {table}")
            for table in TABLES_IN_DEPENDENCY_ORDER:
                # ORDER BY 1 sorts by the first column, the id in most tables, so rows arrive in the
                # order they were made and the copy is the same on every run.
                rows = self._backend.fetch_all(f"SELECT * FROM {table} ORDER BY 1")
                copied_rows[table] = len(rows)
                if not rows:
                    continue
                # The column names come from the source's rows; both databases are at the same
                # schema version, so the target has the same columns.
                columns = list(rows[0])
                insert = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders(len(columns))})"
                parameter_rows = []
                for row in rows:
                    parameter_rows.append([row[column] for column in columns])
                target._backend.execute_many(insert, parameter_rows)
        return copied_rows

    def _has_rows(self, table: str) -> bool:
        """True when the table holds at least one row."""
        # The table name comes from TABLES_IN_DEPENDENCY_ORDER only.
        return self._backend.fetch_one(f"SELECT 1 AS found FROM {table} LIMIT 1") is not None

    # Settings.

    def get_setting(self, setting: Setting) -> str:
        """The stored value of a setting, or its default when none is stored."""
        row = self._backend.fetch_one("SELECT value FROM settings WHERE name = ?", (setting.key,))
        if row is None:
            return setting.default
        return row["value"]

    def get_integer_setting(self, setting: Setting) -> int:
        """A numeric setting as a number. Raises DatabaseError when the stored value is not one,
        which only a value written past check_setting_value can be."""
        try:
            return int(self.get_setting(setting))
        except ValueError as error:
            raise DatabaseError(f"setting {setting.key} is not a number") from error

    def get_boolean_setting(self, setting: Setting) -> bool:
        """A switch setting: True for any of TRUE_SETTING_VALUES, False for anything else."""
        return self.get_setting(setting).strip().lower() in TRUE_SETTING_VALUES

    def set_setting(self, setting: Setting, value: str) -> None:
        """Check and store the value of one setting (see set_settings)."""
        self.set_settings({setting: value})

    def set_settings(self, values: dict[Setting, str]) -> None:
        """Check every value first, then store them all in one transaction: either all change or
        none does."""
        for setting, value in values.items():
            check_setting_value(setting, value)
        with self._backend.transaction():
            for setting, value in values.items():
                self._backend.upsert("settings", ("name",), {"name": setting.key, "value": value})

    # Accounts and access codes.

    def create_account(self, name: str, is_admin: bool = False) -> Account:
        """Create an account and return it. Raises DatabaseError for a name the game would refuse
        or one that is taken already. The account has no access code yet; issue_access_code makes one."""
        if not is_valid_player_name(name):
            raise DatabaseError(f"{name!r} is not a valid player name: only letters A to Z and single spaces")
        try:
            with self._backend.transaction():
                result = self._backend.execute(
                    "INSERT INTO accounts (name, is_admin, created_at) VALUES (?, ?, ?)",
                    (name, int(is_admin), to_stored_time(self._clock())),
                )
        except ConstraintViolation as error:
            # The only unique key a new account can break is the one on its name, which ignores case:
            # "Hans" is refused while "hans" exists.
            raise DatabaseError(f"an account named {name!r} already exists") from error
        account = self.find_account_by_id(result.last_row_id)
        if account is None:
            raise DatabaseError(f"the account {name!r} was not found right after it was made")
        return account

    def find_account(self, name: str) -> Account | None:
        """The account with this name, or None. The name is compared without regard to case, as the
        schema's collation of accounts.name does."""
        row = self._backend.fetch_one("SELECT * FROM accounts WHERE name = ?", (name,))
        return account_from_row(row)

    def find_account_by_id(self, account_id: int) -> Account | None:
        """The account with this id, or None."""
        row = self._backend.fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        return account_from_row(row)

    def list_accounts(self) -> list[Account]:
        """Every account, sorted by name."""
        rows = self._backend.fetch_all("SELECT * FROM accounts ORDER BY name")
        return [account_from_row(row) for row in rows]

    def list_accounts_page(self, search: str, limit: int, offset: int) -> list[Account]:
        """One page of the accounts whose name contains search (any case), sorted by name; every
        account for an empty search."""
        rows = self._backend.fetch_all(
            "SELECT * FROM accounts WHERE LOWER(name) LIKE ? ESCAPE '!' ORDER BY name LIMIT ? OFFSET ?",
            (like_pattern(search), limit, offset),
        )
        return [account_from_row(row) for row in rows]

    def count_accounts(self, search: str = "") -> int:
        """How many accounts have a name containing search (any case); all of them for an empty one."""
        row = self._backend.fetch_one(
            "SELECT COUNT(*) AS account_count FROM accounts WHERE LOWER(name) LIKE ? ESCAPE '!'",
            (like_pattern(search),),
        )
        return row["account_count"]

    def delete_account(self, account_id: int) -> None:
        """Delete the account; a running server also removes the player from the lobby."""
        with self._backend.transaction():
            # The foreign keys delete the account's codes, sessions and clan membership with it.
            # pending_kicks has no foreign key on purpose, so the kick queued next survives.
            self._backend.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            self._queue_kick(account_id, ACCOUNT_REMOVED_KICK_REASON)

    def set_admin(self, account_id: int, is_admin: bool) -> None:
        """Make the account an admin or take that right away. Chat commands read the account at each
        use, so they follow at once; an online player's row colour (mark_admins) follows at the next logon."""
        with self._backend.transaction():
            self._backend.execute("UPDATE accounts SET is_admin = ? WHERE id = ?", (int(is_admin), account_id))

    def ban_account(self, account_id: int, until: datetime, reason: str) -> None:
        """Bar the account from logging on until the given moment. A player who is online is also
        removed from the running lobby; one who is offline is refused at the next logon. The reason
        is checked like any text for the game: an online player is told it."""
        check_game_text("ban reason", reason, MAX_BAN_REASON_LENGTH)
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE accounts SET banned_until = ?, ban_reason = ? WHERE id = ?",
                (to_stored_time(until), reason, account_id),
            )
            self._queue_kick(account_id, ban_kick_reason(until, reason))

    def queue_kick(self, account_id: int, reason: str) -> None:
        """Ask the running server to remove the player from the lobby, if online. The player may
        log on again; a ban keeps them out."""
        with self._backend.transaction():
            self._queue_kick(account_id, reason)

    def _queue_kick(self, account_id: int, reason: str) -> None:
        """Queue a kick inside the caller's transaction, so that it is stored together with the
        change that causes it."""
        self._backend.execute(
            "INSERT INTO pending_kicks (account_id, reason, created_at) VALUES (?, ?, ?)",
            (account_id, reason, to_stored_time(self._clock())),
        )

    def take_pending_kicks(self) -> list[PendingKick]:
        """The kicks not carried out yet, oldest first, marked as taken in the same step."""
        # Reading and marking in one transaction: the marks are written all or none, so a failure
        # halfway leaves every kick to be taken again instead of some lost.
        with self._backend.transaction():
            rows = self._backend.fetch_all(
                "SELECT id, account_id, reason FROM pending_kicks WHERE taken_at IS NULL ORDER BY id"
            )
            taken_at = to_stored_time(self._clock())
            for row in rows:
                self._backend.execute("UPDATE pending_kicks SET taken_at = ? WHERE id = ?", (taken_at, row["id"]))
        return [PendingKick(account_id=row["account_id"], reason=row["reason"]) for row in rows]

    def unban_account(self, account_id: int) -> None:
        """Lift the account's ban and clear its reason; the player may log on again at once."""
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE accounts SET banned_until = NULL, ban_reason = '' WHERE id = ?", (account_id,)
            )

    def add_access_code(self, account_id: int, access_code: str, label: str = "") -> int:
        """Store an access code for an account and return its id. The code itself is not kept."""
        check_game_text("access code label", label, MAX_ACCESS_CODE_LABEL_LENGTH)
        with self._backend.transaction():
            result = self._backend.execute(
                "INSERT INTO access_codes (account_id, code_hash, label, created_at) VALUES (?, ?, ?, ?)",
                (account_id, hash_access_code(access_code), label, to_stored_time(self._clock())),
            )
        return result.last_row_id

    def issue_access_code(self, account_id: int, label: str = "") -> str:
        """Generate a new access code for an account, store its hash, and return the code: this is
        the only moment it can be shown to the player."""
        access_code = generate_access_code()
        self.add_access_code(account_id, access_code, label)
        return access_code

    def revoke_access_codes(self, account_id: int) -> int:
        """Revoke every active access code of an account; returns how many were revoked."""
        with self._backend.transaction():
            result = self._backend.execute(
                "UPDATE access_codes SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
                (to_stored_time(self._clock()), account_id),
            )
        return result.row_count

    def revoke_access_code(self, account_id: int, code_id: int) -> bool:
        """Revoke one access code of an account; False when it has no such active code."""
        with self._backend.transaction():
            result = self._backend.execute(
                "UPDATE access_codes SET revoked_at = ? WHERE id = ? AND account_id = ? AND revoked_at IS NULL",
                (to_stored_time(self._clock()), code_id, account_id),
            )
        return result.row_count == 1

    def list_access_codes(self, account_id: int) -> list[AccessCodeRecord]:
        """Every access code of an account, revoked ones included, oldest first. The codes
        themselves are not stored, only what describes them."""
        rows = self._backend.fetch_all(
            "SELECT id, label, created_at, last_used_at, revoked_at FROM access_codes WHERE account_id = ? ORDER BY id",
            (account_id,),
        )
        return [
            AccessCodeRecord(
                code_id=row["id"],
                label=row["label"],
                created_at=from_stored_time(row["created_at"]),
                last_used_at=from_optional_stored_time(row["last_used_at"]),
                revoked_at=from_optional_stored_time(row["revoked_at"]),
            )
            for row in rows
        ]

    def check_access_code(self, account_id: int, access_code: str) -> bool:
        """True when the code matches one of the account's active codes; marks that code as used."""
        return self.match_access_code(account_id, access_code) is not None

    def match_access_code(self, account_id: int, access_code: str) -> int | None:
        """The id of the account's active code that matches, marked as used; None for no match."""
        rows = self._backend.fetch_all(
            "SELECT id, code_hash FROM access_codes WHERE account_id = ? AND revoked_at IS NULL",
            (account_id,),
        )
        if not rows:
            # Costs one hash like a wrong code, so the time of the answer does not tell an account
            # without active codes from one with a code.
            hash_access_code(access_code)
            return None
        for row in rows:
            if is_matching_access_code(access_code, row["code_hash"]):
                # Only a successful logon writes: last_used_at tells an admin which codes are still in use.
                with self._backend.transaction():
                    self._backend.execute(
                        "UPDATE access_codes SET last_used_at = ? WHERE id = ?",
                        (to_stored_time(self._clock()), row["id"]),
                    )
                return row["id"]
        return None

    def is_access_code_active(self, account_id: int, code_id: int) -> bool:
        """True while the code exists, belongs to the account and is not revoked. A web session
        remembers the code it logged on with and ends when this turns False."""
        row = self._backend.fetch_one(
            "SELECT id FROM access_codes WHERE id = ? AND account_id = ? AND revoked_at IS NULL",
            (code_id, account_id),
        )
        return row is not None

    # Web interface sessions.

    def start_web_session(self, account_id: int, access_code_id: int) -> str:
        """Record a logon to the web interface and return its token for the browser's cookie. Only
        the token's hash is stored. Sessions long over are cleared out on the way."""
        token = secrets.token_urlsafe(WEB_SESSION_TOKEN_BYTES)
        now = self._clock()
        with self._backend.transaction():
            # Sessions this old are dead by WEB_SESSION_LIFETIME already. Clearing them on each new
            # logon keeps the table small without a timer of its own.
            self._backend.execute(
                "DELETE FROM web_sessions WHERE created_at < ?",
                (to_stored_time(now - WEB_SESSION_LIFETIME * WEB_SESSIONS_KEPT_LIFETIMES),),
            )
            self._backend.execute(
                "INSERT INTO web_sessions (token_hash, account_id, access_code_id, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (hash_session_token(token), account_id, access_code_id, to_stored_time(now), to_stored_time(now)),
            )
        return token

    def find_web_session(self, token: str) -> WebSession | None:
        """The live session with this token, or None when it is unknown, ended, older than
        WEB_SESSION_LIFETIME (however busy it was) or idle longer than WEB_SESSION_IDLE_TIMEOUT.
        Marks it as seen, at most once a minute so a page that polls does not write each time."""
        now = self._clock()
        row = self._backend.fetch_one(
            "SELECT id, account_id, access_code_id, last_seen_at FROM web_sessions "
            "WHERE token_hash = ? AND ended_at IS NULL AND created_at >= ? AND last_seen_at >= ?",
            (
                hash_session_token(token),
                to_stored_time(now - WEB_SESSION_LIFETIME),
                to_stored_time(now - WEB_SESSION_IDLE_TIMEOUT),
            ),
        )
        if row is None:
            return None
        last_seen_at = from_stored_time(row["last_seen_at"])
        # last_seen_at only needs minute precision for the idle timeout; skipping the write saves a
        # write lock on every poll of the live lobby page.
        if now - last_seen_at >= WEB_SESSION_TOUCH_INTERVAL:
            with self._backend.transaction():
                self._backend.execute(
                    "UPDATE web_sessions SET last_seen_at = ? WHERE id = ?", (to_stored_time(now), row["id"])
                )
        return WebSession(account_id=row["account_id"], access_code_id=row["access_code_id"])

    def end_web_session(self, token: str) -> None:
        """End the web session with this token (the admin logged off); an unknown or ended token
        changes nothing."""
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE web_sessions SET ended_at = ? WHERE token_hash = ? AND ended_at IS NULL",
                (to_stored_time(self._clock()), hash_session_token(token)),
            )

    def record_logon(self, account_id: int) -> int:
        """Note a logon for the statistics; returns the session id to close on logout."""
        now = to_stored_time(self._clock())
        with self._backend.transaction():
            self._backend.execute("UPDATE accounts SET last_logon_at = ? WHERE id = ?", (now, account_id))
            result = self._backend.execute(
                "INSERT INTO lobby_sessions (account_id, started_at) VALUES (?, ?)",
                (account_id, now),
            )
        return result.last_row_id

    def record_logout(self, session_id: int) -> None:
        """Note when a lobby session ended, for the statistics; a session already ended stays as it is."""
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE lobby_sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (to_stored_time(self._clock()), session_id),
            )

    def close_open_sessions_and_games(self) -> None:
        """At server start nobody is connected: end what a previous run left open."""
        now = to_stored_time(self._clock())
        with self._backend.transaction():
            self._backend.execute("UPDATE lobby_sessions SET ended_at = ? WHERE ended_at IS NULL", (now,))
            self._backend.execute("UPDATE games SET closed_at = ? WHERE closed_at IS NULL", (now,))

    # Clans.

    def create_clan(self, name: str) -> Clan:
        """Create a clan and return it. Raises DatabaseError for a name the game cannot show or
        one that is taken already."""
        check_title("clan name", name)
        try:
            with self._backend.transaction():
                result = self._backend.execute(
                    "INSERT INTO clans (name, created_at) VALUES (?, ?)",
                    (name, to_stored_time(self._clock())),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"a clan named {name!r} already exists") from error
        return Clan(clan_id=result.last_row_id, name=name)

    def find_clan(self, name: str) -> Clan | None:
        """The clan with this name, or None. The name is compared without regard to case, as the
        schema's collation of clans.name does."""
        row = self._backend.fetch_one("SELECT id, name, is_highlighted FROM clans WHERE name = ?", (name,))
        if row is None:
            return None
        return clan_from_row(row)

    def list_clans(self) -> list[Clan]:
        """Every clan, sorted by name."""
        rows = self._backend.fetch_all("SELECT id, name, is_highlighted FROM clans ORDER BY name")
        return [clan_from_row(row) for row in rows]

    def set_clan_highlighted(self, clan_id: int, is_highlighted: bool) -> None:
        """List the clan's members with a green row in the game, from their next logon on."""
        with self._backend.transaction():
            self._backend.execute("UPDATE clans SET is_highlighted = ? WHERE id = ?", (int(is_highlighted), clan_id))

    def delete_clan(self, clan_id: int) -> None:
        """Delete the clan; its members are left without a clan."""
        with self._backend.transaction():
            # The foreign key on clan_members deletes the memberships; the accounts themselves stay.
            self._backend.execute("DELETE FROM clans WHERE id = ?", (clan_id,))

    def set_clan_of_account(self, account_id: int, clan_id: int | None) -> None:
        """Put an account into a clan, or take it out with clan_id None. One clan per account."""
        with self._backend.transaction():
            # Putting a member into the clan it is in already keeps the date it joined: Clanliga
            # counts a player for a clan only after some days in it.
            current = self._backend.fetch_one("SELECT clan_id FROM clan_members WHERE account_id = ?", (account_id,))
            if current is not None and clan_id is not None and current["clan_id"] == clan_id:
                return
            # account_id is the key of clan_members, so the old membership goes first; both steps
            # share one transaction, so the account is never seen without a clan in between.
            self._backend.execute("DELETE FROM clan_members WHERE account_id = ?", (account_id,))
            if clan_id is not None:
                self._backend.execute(
                    "INSERT INTO clan_members (account_id, clan_id, joined_at) VALUES (?, ?, ?)",
                    (account_id, clan_id, to_stored_time(self._clock())),
                )

    def find_clan_of_account(self, account_id: int) -> Clan | None:
        """The clan the account belongs to, or None."""
        row = self._backend.fetch_one(
            "SELECT clans.id, clans.name, clans.is_highlighted FROM clans "
            "JOIN clan_members ON clan_members.clan_id = clans.id WHERE clan_members.account_id = ?",
            (account_id,),
        )
        if row is None:
            return None
        return clan_from_row(row)

    def list_clan_memberships(self) -> dict[int, list[int]]:
        """Every clan's member account ids, in one query (the ladder's clan strength needs them all)."""
        rows = self._backend.fetch_all("SELECT clan_id, account_id FROM clan_members ORDER BY account_id")
        members_by_clan: dict[int, list[int]] = {}
        for row in rows:
            members_by_clan.setdefault(row["clan_id"], []).append(row["account_id"])
        return members_by_clan

    def list_clan_member_names(self, clan_id: int) -> list[str]:
        """The names of the clan's members, sorted."""
        rows = self._backend.fetch_all(
            "SELECT accounts.name FROM accounts JOIN clan_members ON clan_members.account_id = accounts.id "
            "WHERE clan_members.clan_id = ? ORDER BY accounts.name",
            (clan_id,),
        )
        return [row["name"] for row in rows]

    # Home tab news.

    def set_news(self, audience: str, title: str, body: str) -> None:
        """audience "german" is shown to German games, "other" to every other language."""
        if audience not in NEWS_AUDIENCES:
            raise DatabaseError(f"unknown news audience {audience!r}; known: {', '.join(NEWS_AUDIENCES)}")
        check_title("news title", title)
        check_game_text("news text", body, MAX_TEXT_MESSAGE_BYTES, allows_line_breaks=True)
        values = {"audience": audience, "title": title, "body": body, "updated_at": to_stored_time(self._clock())}
        with self._backend.transaction():
            self._backend.upsert("news", ("audience",), values)

    def find_news(self, audience: str) -> News | None:
        """The news for one audience ("german" or "other"), or None when none was set."""
        row = self._backend.fetch_one("SELECT title, body FROM news WHERE audience = ?", (audience,))
        if row is None:
            return None
        return News(title=row["title"], body=row["body"])

    # ChatZone themes.

    def add_chat_theme(self, name: str, description: str = "", language: str = ALL_LANGUAGES) -> ChatTheme:
        """Add a theme to the ChatZone tab and return it; language limits it to the games of one
        language. Raises DatabaseError for a text the game cannot show, a name that is taken already,
        the name of a built-in theme, or an unknown language."""
        check_title("chat theme name", name)
        check_game_text("chat theme description", description, MAX_GROUP_TEXT_LENGTH)
        if is_built_in_chat_theme_name(name):
            raise DatabaseError(f"{name!r} is a chat theme the server always lists itself")
        if language != ALL_LANGUAGES and language not in GAME_LANGUAGES:
            raise DatabaseError(f"unknown game language {language!r}; known: {', '.join(GAME_LANGUAGES)}")
        try:
            with self._backend.transaction():
                result = self._backend.execute(
                    "INSERT INTO chat_themes (name, description, language, created_at) VALUES (?, ?, ?, ?)",
                    (name, description, language, to_stored_time(self._clock())),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"a chat theme named {name!r} already exists") from error
        return ChatTheme(theme_id=result.last_row_id, name=name, description=description, language=language)

    def list_chat_themes(self) -> list[ChatTheme]:
        """Every ChatZone theme, in the order they were added."""
        rows = self._backend.fetch_all("SELECT * FROM chat_themes ORDER BY id")
        return [
            ChatTheme(theme_id=row["id"], name=row["name"], description=row["description"], language=row["language"])
            for row in rows
        ]

    def delete_chat_theme(self, theme_id: int) -> None:
        """Remove a theme from the ChatZone tab."""
        with self._backend.transaction():
            self._backend.execute("DELETE FROM chat_themes WHERE id = ?", (theme_id,))

    # MessageZone board.

    def add_board_theme(
        self, language: str, name: str, description: str = "", is_server_written: bool = False
    ) -> BoardTheme:
        """Add a theme to the MessageZone board of one language and return it. Raises
        DatabaseError for an unknown language, a text the game cannot show, or a name that language has
        already."""
        if language not in BOARD_LANGUAGES:
            raise DatabaseError(f"unknown board language {language!r}; known: {', '.join(BOARD_LANGUAGES)}")
        check_title("board theme name", name)
        check_game_text("board theme description", description, MAX_GROUP_TEXT_LENGTH)
        try:
            with self._backend.transaction():
                # The server's own theme starts above every other one (the ladder's news comes first
                # unless an admin moves something above it); an admin's theme starts at the end.
                bounds = self._backend.fetch_one(
                    "SELECT MIN(position) AS first, MAX(position) AS last FROM board_themes WHERE language = ?",
                    (language,),
                )
                position = 0
                if bounds["first"] is not None and is_server_written:
                    position = bounds["first"] - 1
                elif bounds["last"] is not None:
                    position = bounds["last"] + 1
                result = self._backend.execute(
                    "INSERT INTO board_themes (language, name, description, created_at, is_server_written, position) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (language, name, description, to_stored_time(self._clock()), int(is_server_written), position),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"a {language} board theme named {name!r} already exists") from error
        return BoardTheme(result.last_row_id, language, name, description, is_server_written, position)

    def list_board_themes(self) -> list[BoardTheme]:
        """Every board theme, by language and then in the order they were added."""
        rows = self._backend.fetch_all("SELECT * FROM board_themes ORDER BY language, position, id")
        return [board_theme_from_row(row) for row in rows]

    def set_board_theme_order(self, theme_ids: list[int]) -> None:
        """Put themes in this order (the first at the top); themes not named keep their places."""
        with self._backend.transaction():
            for position, theme_id in enumerate(theme_ids):
                self._backend.execute("UPDATE board_themes SET position = ? WHERE id = ?", (position, theme_id))

    def move_board_theme(self, theme_id: int, is_up: bool) -> None:
        """Move a theme one place up or down among its language's themes, the way the web
        interface's Up and Down do; at either end it stays. Raises DatabaseError for an unknown theme."""
        themes = self.list_board_themes()
        theme = next((item for item in themes if item.theme_id == theme_id), None)
        if theme is None:
            raise DatabaseError(f"there is no board theme {theme_id}")
        ids = [item.theme_id for item in themes if item.language == theme.language]
        self.set_board_theme_order(moved_one_place(ids, theme_id, is_up))

    def set_board_theme_hidden(self, theme_id: int, is_hidden: bool) -> None:
        """Hide a theme from the game, or show it again; its messages stay as they are."""
        with self._backend.transaction():
            self._backend.execute("UPDATE board_themes SET is_hidden = ? WHERE id = ?", (int(is_hidden), theme_id))

    def delete_board_theme(self, theme_id: int) -> None:
        """Remove a theme from the MessageZone board. Raises DatabaseError for a theme the server
        writes itself: admins hide it instead, since the server would only create it again."""
        theme = self._backend.fetch_one("SELECT is_server_written FROM board_themes WHERE id = ?", (theme_id,))
        if theme is not None and theme["is_server_written"]:
            raise DatabaseError("the server writes this board theme itself; hide it instead")
        with self._backend.transaction():
            self._backend.execute("DELETE FROM board_themes WHERE id = ?", (theme_id,))

    def post_board_message(
        self, theme_id: int, title: str, body: str, author: str = "", is_by_server: bool = False
    ) -> BoardMessage:
        """Post a message under a board theme and return it. Raises DatabaseError for a text the
        game cannot show, a theme that does not exist, or a theme the server writes itself when
        is_by_server is not set: its texts are the server's alone."""
        if not is_by_server:
            theme = self._backend.fetch_one("SELECT is_server_written FROM board_themes WHERE id = ?", (theme_id,))
            if theme is not None and theme["is_server_written"]:
                raise DatabaseError("the server writes this board theme itself; nobody can post into it")
        check_title("message title", title)
        check_game_text("message text", body, MAX_TEXT_MESSAGE_BYTES, allows_line_breaks=True)
        check_game_text("author", author, MAX_GROUP_TEXT_LENGTH)
        posted_at = self._clock()
        try:
            with self._backend.transaction():
                # A new message goes to the top of its theme; admins move it from there.
                first = self._backend.fetch_one(
                    "SELECT MIN(position) AS first FROM board_messages WHERE theme_id = ?", (theme_id,)
                )["first"]
                position = 0 if first is None else first - 1
                result = self._backend.execute(
                    "INSERT INTO board_messages (theme_id, title, body, author, posted_at, position) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (theme_id, title, body, author, to_stored_time(posted_at), position),
                )
        except ConstraintViolation as error:
            # board_messages has no unique key, so only the foreign key to board_themes can refuse it.
            raise DatabaseError(f"there is no board theme {theme_id}") from error
        return BoardMessage(result.last_row_id, theme_id, title, body, author, posted_at, position)

    def list_board_messages(self, theme_id: int) -> list[BoardMessage]:
        """The messages under a board theme in the board's order, hidden ones included."""
        rows = self._backend.fetch_all(
            "SELECT * FROM board_messages WHERE theme_id = ? ORDER BY position, id", (theme_id,)
        )
        return [board_message_from_row(row) for row in rows]

    def set_board_message_order(self, theme_id: int, message_ids: list[int]) -> None:
        """Put a theme's messages in this order (the first at the top); ids of another theme are
        left alone."""
        with self._backend.transaction():
            for position, message_id in enumerate(message_ids):
                self._backend.execute(
                    "UPDATE board_messages SET position = ? WHERE id = ? AND theme_id = ?",
                    (position, message_id, theme_id),
                )

    def move_board_message(self, message_id: int, is_up: bool) -> None:
        """Move a message one place up or down in its theme; at either end it stays. Raises
        DatabaseError for an unknown message."""
        message = self.find_board_message(message_id)
        if message is None:
            raise DatabaseError(f"there is no board message {message_id}")
        ids = [item.message_id for item in self.list_board_messages(message.theme_id)]
        self.set_board_message_order(message.theme_id, moved_one_place(ids, message_id, is_up))

    def set_board_message_hidden(self, message_id: int, is_hidden: bool) -> None:
        """Hide a message from the game, or show it again."""
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE board_messages SET is_hidden = ? WHERE id = ?", (int(is_hidden), message_id)
            )

    def find_board_message(self, message_id: int) -> BoardMessage | None:
        """The board message with this id, or None."""
        row = self._backend.fetch_one("SELECT * FROM board_messages WHERE id = ?", (message_id,))
        if row is None:
            return None
        return board_message_from_row(row)

    def delete_board_messages_but_newest(self, theme_id: int, keep_count: int) -> int:
        """Delete a theme's messages but its newest keep_count; returns how many went. The server
        keeps its own themes short this way."""
        # Newest by when they were posted (the id), whatever order admins gave them on the board.
        messages = sorted(self.list_board_messages(theme_id), key=lambda message: message.message_id)
        surplus = messages[: max(0, len(messages) - keep_count)]
        with self._backend.transaction():
            for message in surplus:
                self._backend.execute("DELETE FROM board_messages WHERE id = ?", (message.message_id,))
        return len(surplus)

    def delete_board_message(self, message_id: int) -> None:
        """Remove a message from the board. Raises DatabaseError for a message in a theme the
        server writes itself: admins hide those instead."""
        theme = self._backend.fetch_one(
            "SELECT board_themes.is_server_written FROM board_messages "
            "JOIN board_themes ON board_themes.id = board_messages.theme_id WHERE board_messages.id = ?",
            (message_id,),
        )
        if theme is not None and theme["is_server_written"]:
            raise DatabaseError("the server writes this message itself; hide it instead")
        with self._backend.transaction():
            self._backend.execute("DELETE FROM board_messages WHERE id = ?", (message_id,))

    # Games and their reports.

    def record_game_opened(
        self,
        name: str,
        host_account_id: int | None,
        group_id: int | None = None,
        announcement: bytes | None = None,
        ladder_type: int | None = None,
    ) -> int:
        """Note a game the lobby opened, for the statistics and the web interface; returns its
        record id. group_id is the lobby's id for the game, announcement the game as the lobby shows it
        (see GameRecord), ladder_type the ladder the host chose (None for a normal game)."""
        with self._backend.transaction():
            result = self._backend.execute(
                "INSERT INTO games (name, host_account_id, opened_at, group_id, announcement, ladder_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, host_account_id, to_stored_time(self._clock()), group_id, announcement, ladder_type),
            )
        return result.last_row_id

    def record_game_announcement(
        self, game_record_id: int, announcement: bytes, ladder_type: int | None = None
    ) -> None:
        """Keep the game as the lobby shows it now, after the host changed its setup, and the ladder
        it is now (the host may switch between a normal and a ladder game until the match starts)."""
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE games SET announcement = ?, ladder_type = ? WHERE id = ?",
                (announcement, ladder_type, game_record_id),
            )

    def record_game_player(
        self, game_record_id: int, account_id: int, clan_id: int | None = None, installation: int | None = None
    ) -> None:
        """Note a player who joined a game; the lobby records each player once per game. clan_id is
        the clan the player shows, installation the number of the player's copy of the game within
        this game (see GamePlayerRecord). Since when the player is in the clan is copied from the
        membership as it is now, so a later change of clan does not rewrite it."""
        with self._backend.transaction():
            self._backend.execute(
                "INSERT INTO game_players (game_id, account_id, joined_at, clan_id, clan_joined_at, installation) "
                "VALUES (?, ?, ?, ?, "
                "(SELECT joined_at FROM clan_members WHERE account_id = ? AND clan_id = ?), ?)",
                (
                    game_record_id,
                    account_id,
                    to_stored_time(self._clock()),
                    clan_id,
                    account_id,
                    clan_id,
                    installation,
                ),
            )

    def record_game_closed(self, game_record_id: int) -> None:
        """Note when a game closed; a game already closed stays as it is."""
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE games SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                (to_stored_time(self._clock()), game_record_id),
            )

    def store_game_report(
        self, account_id: int | None, group_id: int, report: bytes, game_record_id: int | None = None
    ) -> None:
        """Keep a match report as the client sent it. account_id is the sender, group_id the
        game's id in the lobby, and game_record_id the stored game, None when the server did not know the
        game."""
        with self._backend.transaction():
            self._backend.execute(
                "INSERT INTO game_reports (account_id, group_id, report, received_at, game_id) VALUES (?, ?, ?, ?, ?)",
                (account_id, group_id, report, to_stored_time(self._clock()), game_record_id),
            )

    def count_games(self) -> int:
        """How many games are stored, open and closed."""
        return self._backend.fetch_one("SELECT COUNT(*) AS game_count FROM games")["game_count"]

    def count_game_reports(self) -> int:
        """How many match reports are stored."""
        return self._backend.fetch_one("SELECT COUNT(*) AS report_count FROM game_reports")["report_count"]

    GAME_COLUMNS = (
        "SELECT games.id, games.name, accounts.name AS host_name, games.opened_at, games.closed_at, "
        "games.group_id, games.announcement, games.ladder_type "
        "FROM games LEFT JOIN accounts ON accounts.id = games.host_account_id "
    )
    GAME_REPORT_COLUMNS = (
        # LENGTH of a binary column counts bytes in SQLite and in MySQL alike. The LEFT JOIN keeps a
        # report whose sender's account was deleted since, with a NULL name.
        "SELECT game_reports.id, accounts.name AS sender_name, game_reports.group_id, "
        "LENGTH(game_reports.report) AS size, game_reports.received_at, game_reports.report, "
        "game_reports.game_id, game_reports.account_id AS sender_account_id "
        "FROM game_reports LEFT JOIN accounts ON accounts.id = game_reports.account_id "
    )

    def list_recent_games(self, limit: int, offset: int = 0) -> list[GameRecord]:
        """The newest games first, open and closed; offset skips that many of the newest."""
        rows = self._backend.fetch_all(
            self.GAME_COLUMNS + "ORDER BY games.id DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        player_names = self._player_names_of_games([row["id"] for row in rows])
        return [game_record_from_row(row, player_names.get(row["id"], [])) for row in rows]

    def find_game(self, game_id: int) -> GameRecord | None:
        """The stored game with this record id and the names of its players, or None."""
        row = self._backend.fetch_one(self.GAME_COLUMNS + "WHERE games.id = ?", (game_id,))
        if row is None:
            return None
        player_names = self._player_names_of_games([game_id])
        return game_record_from_row(row, player_names.get(game_id, []))

    def list_games_of_account(self, account_id: int, limit: int) -> list[GameRecord]:
        """The games an account joined, newest first."""
        rows = self._backend.fetch_all(
            self.GAME_COLUMNS
            + "WHERE games.id IN (SELECT game_id FROM game_players WHERE account_id = ?) "
            "ORDER BY games.id DESC LIMIT ?",
            (account_id, limit),
        )
        player_names = self._player_names_of_games([row["id"] for row in rows])
        return [game_record_from_row(row, player_names.get(row["id"], [])) for row in rows]

    def list_game_players(self, game_id: int) -> list[GamePlayerRecord]:
        """Who joined a game and when, in the order they joined (the host first)."""
        rows = self._backend.fetch_all(
            "SELECT accounts.name, game_players.joined_at, game_players.account_id, game_players.clan_id, "
            "game_players.clan_joined_at, game_players.installation FROM game_players "
            "LEFT JOIN accounts ON accounts.id = game_players.account_id "
            "WHERE game_players.game_id = ? ORDER BY game_players.id",
            (game_id,),
        )
        return [
            GamePlayerRecord(
                name=row["name"] or DELETED_ACCOUNT_NAME,
                joined_at=from_stored_time(row["joined_at"]),
                account_id=row["account_id"],
                clan_id=row["clan_id"],
                clan_joined_at=from_optional_stored_time(row["clan_joined_at"]),
                installation=row["installation"],
            )
            for row in rows
        ]

    def _player_names_of_games(self, game_ids: list[int]) -> dict[int, list[str]]:
        """The accounts that joined each game, in the order they joined. One query for all games,
        not one per game; the ids are numbers from the database, joined into the IN list."""
        if not game_ids:
            return {}
        id_placeholders = placeholders(len(game_ids))
        rows = self._backend.fetch_all(
            "SELECT game_players.game_id, accounts.name FROM game_players "
            "LEFT JOIN accounts ON accounts.id = game_players.account_id "
            f"WHERE game_players.game_id IN ({id_placeholders}) ORDER BY game_players.id",
            tuple(game_ids),
        )
        names_by_game: dict[int, list[str]] = {}
        for row in rows:
            names_by_game.setdefault(row["game_id"], []).append(row["name"] or DELETED_ACCOUNT_NAME)
        return names_by_game

    def list_recent_game_reports(self, limit: int) -> list[GameReportRecord]:
        """The newest match reports first."""
        rows = self._backend.fetch_all(self.GAME_REPORT_COLUMNS + "ORDER BY game_reports.id DESC LIMIT ?", (limit,))
        return [game_report_record_from_row(row) for row in rows]

    def list_reports_of_games(self, game_ids: list[int]) -> list[GameReportRecord]:
        """The match reports of several games in the order they arrived, in one query; the ids
        are numbers from the database, joined into the IN list."""
        if not game_ids:
            return []
        id_placeholders = placeholders(len(game_ids))
        rows = self._backend.fetch_all(
            self.GAME_REPORT_COLUMNS + f"WHERE game_reports.game_id IN ({id_placeholders}) ORDER BY game_reports.id",
            tuple(game_ids),
        )
        return [game_report_record_from_row(row) for row in rows]

    def list_reports_of_game(self, game_id: int) -> list[GameReportRecord]:
        """A game's match reports in the order they arrived."""
        rows = self._backend.fetch_all(
            self.GAME_REPORT_COLUMNS + "WHERE game_reports.game_id = ? ORDER BY game_reports.id", (game_id,)
        )
        return [game_report_record_from_row(row) for row in rows]

    def find_game_report(self, report_id: int) -> bytes | None:
        """The bytes of a stored match report as the client sent them, or None."""
        row = self._backend.fetch_one("SELECT report FROM game_reports WHERE id = ?", (report_id,))
        if row is None:
            return None
        return bytes(row["report"])

    # The ladder (s3lobby/ladder.py): results, custom seasons, and the events the server wrote about.

    def now(self) -> datetime:
        """The time by the database's clock, the one every stored time comes from; tests move it."""
        return self._clock()

    def get_ladder_settings(self) -> LadderSettings:
        """The ladder's numbers as the admins set them."""
        return LadderSettings(
            minimum_minutes=self.get_integer_setting(Setting.LADDER_MINIMUM_MINUTES),
            report_timeout_minutes=self.get_integer_setting(Setting.LADDER_REPORT_TIMEOUT_MINUTES),
            pair_daily_limit=self.get_integer_setting(Setting.LADDER_PAIR_DAILY_LIMIT),
            start_rating=self.get_integer_setting(Setting.LADDER_START_RATING),
            k=self.get_integer_setting(Setting.LADDER_K),
            k_provisional=self.get_integer_setting(Setting.LADDER_K_PROVISIONAL),
            provisional_games=self.get_integer_setting(Setting.LADDER_PROVISIONAL_GAMES),
            active_days=self.get_integer_setting(Setting.LADDER_ACTIVE_DAYS),
            clan_member_days=self.get_integer_setting(Setting.LADDER_CLAN_MEMBER_DAYS),
            inactive_days=self.get_integer_setting(Setting.LADDER_INACTIVE_DAYS),
        )

    def list_unresolved_ladder_games(self) -> list[GameRecord]:
        """Ladder games without a result yet, oldest first: still open, waiting for reports, or cut
        off by a restart."""
        rows = self._backend.fetch_all(
            self.GAME_COLUMNS
            + "WHERE games.ladder_type IS NOT NULL AND games.ladder_type <> 0 AND games.is_ladder_skipped = 0 "
            "AND NOT EXISTS (SELECT 1 FROM ladder_results WHERE ladder_results.game_id = games.id) "
            "ORDER BY games.id",
        )
        return [game_record_from_row(row, []) for row in rows]

    def mark_ladder_game_skipped(self, game_id: int) -> None:
        """Note that a ladder game counts in no ladder because not every player had joined one: no
        result is stored for it, and the server does not look at it again."""
        with self._backend.transaction():
            self._backend.execute("UPDATE games SET is_ladder_skipped = 1 WHERE id = ?", (game_id,))

    def join_ladder(self, account_id: int, key: LadderKey) -> bool:
        """Make the player a member of a ladder from now on; False when they are one already."""
        now = self._clock()
        with self._backend.transaction():
            if self._active_ladder_membership_id(account_id, key) is not None:
                return False
            self._backend.execute(
                "INSERT INTO ladder_memberships (account_id, ladder, season_id, joined_at) VALUES (?, ?, ?, ?)",
                (account_id, key.ladder, key.season_id, to_stored_time(now)),
            )
        return True

    def leave_ladder(self, account_id: int, key: LadderKey) -> bool:
        """End the player's membership of a ladder now; False when they were not a member. Their
        results stay, so their opponents' ratings do not change."""
        with self._backend.transaction():
            membership_id = self._active_ladder_membership_id(account_id, key)
            if membership_id is None:
                return False
            self._backend.execute(
                "UPDATE ladder_memberships SET left_at = ? WHERE id = ?",
                (to_stored_time(self._clock()), membership_id),
            )
        return True

    def _active_ladder_membership_id(self, account_id: int, key: LadderKey) -> int | None:
        """The row id of the player's current membership of a ladder, or None."""
        # "season_id = ?" never matches NULL, hence the two forms.
        if key.season_id is None:
            row = self._backend.fetch_one(
                "SELECT id FROM ladder_memberships WHERE account_id = ? AND ladder = ? AND season_id IS NULL "
                "AND left_at IS NULL",
                (account_id, key.ladder),
            )
        else:
            row = self._backend.fetch_one(
                "SELECT id FROM ladder_memberships WHERE account_id = ? AND ladder = ? AND season_id = ? "
                "AND left_at IS NULL",
                (account_id, key.ladder, key.season_id),
            )
        if row is None:
            return None
        return row["id"]

    def list_ladder_memberships(self) -> list[LadderMembership]:
        """Every membership, past ones included, oldest first: who was in which ladder when."""
        rows = self._backend.fetch_all(
            "SELECT account_id, ladder, season_id, joined_at, left_at FROM ladder_memberships ORDER BY id"
        )
        return [
            LadderMembership(
                account_id=row["account_id"],
                key=LadderKey(row["ladder"], row["season_id"]),
                joined_at=from_stored_time(row["joined_at"]),
                left_at=from_optional_stored_time(row["left_at"]),
            )
            for row in rows
        ]

    def store_ladder_result(self, evaluation: Evaluation, decided_by_account_id: int | None = None) -> None:
        """Keep what the ladder made of a game, replacing an earlier result of the same game (an
        admin's decision replaces the server's). Raises ValueError for a game still waiting: that
        is not a result."""
        if evaluation.state not in STORED_LADDER_STATES:
            raise ValueError(f"a ladder result cannot be {evaluation.state.value}")
        played_at = evaluation.played_at or self._clock()
        with self._backend.transaction():
            self._backend.execute("DELETE FROM ladder_results WHERE game_id = ?", (evaluation.game_id,))
            result = self._backend.execute(
                "INSERT INTO ladder_results (game_id, ladder_type, state, reason, winning_team, game_ticks, "
                "played_at, decided_at, decided_by_account_id, map_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evaluation.game_id,
                    evaluation.ladder_type,
                    evaluation.state.value,
                    evaluation.reason[:MAX_LADDER_REASON_LENGTH],
                    evaluation.winning_team,
                    evaluation.game_ticks,
                    to_stored_time(played_at),
                    to_stored_time(self._clock()),
                    decided_by_account_id,
                    evaluation.map_name[:MAX_MAP_NAME_LENGTH],
                ),
            )
            for player in evaluation.players:
                self._backend.execute(
                    "INSERT INTO ladder_result_players (result_id, account_id, clan_id, team, race, slot, "
                    "has_won, has_left) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.last_row_id,
                        player.account_id,
                        player.clan_id,
                        player.team,
                        player.race,
                        player.slot,
                        int(player.has_won),
                        int(player.has_left),
                    ),
                )

    LADDER_RESULT_COLUMNS = (
        "SELECT ladder_results.id, ladder_results.game_id, ladder_results.ladder_type, ladder_results.state, "
        "ladder_results.reason, ladder_results.winning_team, ladder_results.game_ticks, ladder_results.played_at, "
        "ladder_results.decided_at, ladder_results.map_name, games.name AS game_name, "
        "accounts.name AS decided_by_name "
        "FROM ladder_results JOIN games ON games.id = ladder_results.game_id "
        "LEFT JOIN accounts ON accounts.id = ladder_results.decided_by_account_id "
    )

    def ladder_results_version(self) -> tuple[int, int]:
        """A cheap fingerprint of the stored results: their number and the newest row id. Every new,
        decided or voided result is a new row (store_ladder_result replaces), so the fingerprint
        changes with each and a cache of ratings knows when to compute them again."""
        row = self._backend.fetch_one("SELECT COUNT(*) AS result_count, MAX(id) AS newest_id FROM ladder_results")
        return row["result_count"], row["newest_id"] or 0

    def list_ladder_results(self) -> list[LadderResultRecord]:
        """Every ladder result in the order the matches were played, with its players. Two queries
        for all of them: a small lobby has a few thousand at most, and ratings need them all."""
        rows = self._backend.fetch_all(
            self.LADDER_RESULT_COLUMNS + "ORDER BY ladder_results.played_at, ladder_results.id"
        )
        return self._ladder_results_from_rows(rows)

    def find_ladder_result(self, game_id: int) -> LadderResultRecord | None:
        """The result of one game, or None while it has none."""
        rows = self._backend.fetch_all(self.LADDER_RESULT_COLUMNS + "WHERE ladder_results.game_id = ?", (game_id,))
        results = self._ladder_results_from_rows(rows)
        if not results:
            return None
        return results[0]

    def _ladder_results_from_rows(self, rows: list[Row]) -> list[LadderResultRecord]:
        """The results the rows describe, with their players fetched in one query."""
        if not rows:
            return []
        result_ids = [row["id"] for row in rows]
        id_placeholders = placeholders(len(result_ids))
        player_rows = self._backend.fetch_all(
            "SELECT result_id, account_id, clan_id, team, race, slot, has_won, has_left FROM ladder_result_players "
            f"WHERE result_id IN ({id_placeholders}) ORDER BY slot",
            tuple(result_ids),
        )
        players_by_result: dict[int, list[ResultPlayer]] = {}
        for row in player_rows:
            players_by_result.setdefault(row["result_id"], []).append(
                ResultPlayer(
                    account_id=row["account_id"],
                    clan_id=row["clan_id"],
                    team=row["team"],
                    race=row["race"],
                    slot=row["slot"],
                    has_won=bool(row["has_won"]),
                    has_left=bool(row["has_left"]),
                )
            )
        return [
            LadderResultRecord(
                result_id=row["id"],
                evaluation=Evaluation(
                    game_id=row["game_id"],
                    ladder_type=row["ladder_type"],
                    state=LadderState(row["state"]),
                    reason=row["reason"],
                    winning_team=row["winning_team"],
                    game_ticks=row["game_ticks"],
                    played_at=from_stored_time(row["played_at"]),
                    players=tuple(players_by_result.get(row["id"], [])),
                    map_name=row["map_name"],
                ),
                game_name=row["game_name"],
                decided_at=from_stored_time(row["decided_at"]),
                decided_by_name=row["decided_by_name"] or "",
            )
            for row in rows
        ]

    def add_ladder_season(
        self,
        name: str,
        starts_at: datetime,
        ends_at: datetime | None,
        counts: SeasonCounts,
        created_by_account_id: int | None = None,
    ) -> Season:
        """Create a custom season. Raises DatabaseError for a name the game cannot show, dates in the
        wrong order, a name in use, or more seasons than the setting allows."""
        check_season(name, starts_at, ends_at)
        limit = self.get_integer_setting(Setting.LADDER_MAX_CUSTOM_SEASONS)
        existing = self._backend.fetch_one("SELECT COUNT(*) AS season_count FROM ladder_seasons")["season_count"]
        if existing >= limit:
            raise DatabaseError(f"there are {existing} custom seasons already, the most is {limit}")
        try:
            with self._backend.transaction():
                result = self._backend.execute(
                    "INSERT INTO ladder_seasons (name, starts_at, ends_at, counts, created_at, created_by_account_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        to_stored_time(starts_at),
                        to_optional_stored_time(ends_at),
                        counts.value,
                        to_stored_time(self._clock()),
                        created_by_account_id,
                    ),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"a season named {name!r} exists already") from error
        return Season(result.last_row_id, name, starts_at, ends_at, counts)

    def update_ladder_season(
        self, season_id: int, name: str, starts_at: datetime, ends_at: datetime | None, counts: SeasonCounts
    ) -> None:
        """Change a custom season; ratings follow at once, since they are computed from the results.
        Raises DatabaseError as add_ladder_season does, or for a season that does not exist."""
        check_season(name, starts_at, ends_at)
        try:
            with self._backend.transaction():
                result = self._backend.execute(
                    "UPDATE ladder_seasons SET name = ?, starts_at = ?, ends_at = ?, counts = ? WHERE id = ?",
                    (name, to_stored_time(starts_at), to_optional_stored_time(ends_at), counts.value, season_id),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"a season named {name!r} exists already") from error
        if result.row_count == 0:
            raise DatabaseError(f"there is no season {season_id}")

    def delete_ladder_season(self, season_id: int) -> None:
        """Delete a custom season and its memberships. Its results stay: they count in the
        infinite season and in other seasons for the players who are members there."""
        with self._backend.transaction():
            self._backend.execute("DELETE FROM ladder_seasons WHERE id = ?", (season_id,))

    def list_ladder_seasons(self) -> list[Season]:
        """Every custom season, newest start first."""
        rows = self._backend.fetch_all(
            "SELECT id, name, starts_at, ends_at, counts FROM ladder_seasons ORDER BY starts_at DESC, id DESC"
        )
        return [season_from_row(row) for row in rows]

    def find_ladder_season(self, name: str) -> Season | None:
        """The custom season with this name, in any case, or None."""
        row = self._backend.fetch_one(
            "SELECT id, name, starts_at, ends_at, counts FROM ladder_seasons WHERE LOWER(name) = LOWER(?)", (name,)
        )
        if row is None:
            return None
        return season_from_row(row)

    def record_ladder_event(self, event_key: str) -> bool:
        """Note that the server wrote about an event (a season's end, a weekly summary); True the
        first time, False when it was noted already, so the text is never written twice."""
        try:
            with self._backend.transaction():
                self._backend.execute(
                    "INSERT INTO ladder_events (event_key, happened_at) VALUES (?, ?)",
                    (event_key[:MAX_LADDER_EVENT_KEY_LENGTH], to_stored_time(self._clock())),
                )
        except ConstraintViolation:
            return False
        return True

    # Announcements from outside the server process.

    def queue_announcement(self, text: str, account_id: int | None = None) -> None:
        """Queue a line for the running server to show in every player's chat, or with account_id
        in that player's chat only. Raises DatabaseError for an empty text, one the game cannot
        show, or an account that does not exist."""
        check_title("announcement", text)
        check_game_text("announcement", text, MAX_ANNOUNCEMENT_LENGTH)
        try:
            with self._backend.transaction():
                self._backend.execute(
                    "INSERT INTO pending_announcements (text, created_at, account_id) VALUES (?, ?, ?)",
                    (text, to_stored_time(self._clock()), account_id),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"there is no account {account_id}") from error

    def discard_pending_announcements(self) -> int:
        """Mark every queued announcement as done without sending it; returns how many. At server
        start nobody is online, and a line queued while the server was stopped is stale by now."""
        with self._backend.transaction():
            result = self._backend.execute(
                "UPDATE pending_announcements SET sent_at = ? WHERE sent_at IS NULL",
                (to_stored_time(self._clock()),),
            )
        return result.row_count

    def take_pending_lines(self) -> list[PendingAnnouncement]:
        """The announcements and messages to single players not sent yet, oldest first, marked as
        sent in the same step."""
        # One transaction, as in take_pending_kicks: a failure halfway marks none as sent.
        with self._backend.transaction():
            rows = self._backend.fetch_all(
                "SELECT id, text, account_id FROM pending_announcements WHERE sent_at IS NULL ORDER BY id"
            )
            sent_at = to_stored_time(self._clock())
            for row in rows:
                self._backend.execute(
                    "UPDATE pending_announcements SET sent_at = ? WHERE id = ?", (sent_at, row["id"])
                )
        return [PendingAnnouncement(row["id"], row["text"], row["account_id"]) for row in rows]

    def take_pending_announcements(self) -> list[str]:
        """The texts of the lines not sent yet, oldest first, marked as sent (see take_pending_lines)."""
        return [line.text for line in self.take_pending_lines()]

    def record_announcement_reach(self, announcement_id: int, reached_count: int) -> None:
        """Note how many players a sent line reached: 0 for a player who was not online."""
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE pending_announcements SET reached_count = ? WHERE id = ?", (reached_count, announcement_id)
            )

    def list_recent_announcements(self, limit: int) -> list[AnnouncementRecord]:
        """The newest announcements first; sent_at is None while the server has not taken one yet."""
        rows = self._backend.fetch_all(
            "SELECT pending_announcements.text, pending_announcements.created_at, pending_announcements.sent_at, "
            "pending_announcements.reached_count, accounts.name AS recipient_name FROM pending_announcements "
            "LEFT JOIN accounts ON accounts.id = pending_announcements.account_id "
            "ORDER BY pending_announcements.id DESC LIMIT ?",
            (limit,),
        )
        return [
            AnnouncementRecord(
                text=row["text"],
                created_at=from_stored_time(row["created_at"]),
                sent_at=from_optional_stored_time(row["sent_at"]),
                recipient_name=row["recipient_name"] or "",
                reached_count=row["reached_count"],
            )
            for row in rows
        ]

    # The running server's lobby, for the web interface.

    def write_live_state(self, state: LiveLobbyState) -> None:
        """Store the running server's lobby for the web interface, replacing the state written before."""
        values = {"id": LIVE_STATE_ROW_ID, "snapshot": state.to_json(), "written_at": to_stored_time(self._clock())}
        with self._backend.transaction():
            self._backend.upsert("live_state", ("id",), values)

    def read_live_state(self) -> StoredLiveState | None:
        """The state last written, or None when no server ever wrote one or its text is unreadable."""
        row = self._backend.fetch_one("SELECT snapshot, written_at FROM live_state WHERE id = ?", (LIVE_STATE_ROW_ID,))
        if row is None:
            return None
        try:
            state = LiveLobbyState.from_json(row["snapshot"])
        except ValueError:
            # Such as a snapshot written by another s3lobby version; the page then shows no lobby
            # instead of failing.
            return None
        return StoredLiveState(state=state, written_at=from_stored_time(row["written_at"]))

    def clear_live_state(self) -> None:
        """The server stops: nobody is online any more."""
        with self._backend.transaction():
            self._backend.execute("DELETE FROM live_state")

    # Statistics for the Home tab.

    def collect_statistics(self) -> LobbyStatistics:
        """Players and games now, within the last hour and within the last day. A session or game
        counts for a period when it was open at any moment of it."""
        now = self._clock()
        hour_ago = to_stored_time(now - timedelta(hours=1))
        day_ago = to_stored_time(now - timedelta(days=1))
        return LobbyStatistics(
            players_online=self._count_open("lobby_sessions", "ended_at", count_distinct="account_id"),
            players_last_hour=self._count_open_since("lobby_sessions", "ended_at", hour_ago, "account_id"),
            players_last_day=self._count_open_since("lobby_sessions", "ended_at", day_ago, "account_id"),
            games_open=self._count_open("games", "closed_at"),
            games_last_hour=self._count_open_since("games", "closed_at", hour_ago),
            games_last_day=self._count_open_since("games", "closed_at", day_ago),
        )

    def _count_open(self, table: str, end_column: str, count_distinct: str = "id") -> int:
        """How many rows of the table are still open (end_column empty), counting each value of
        count_distinct once, so that a player with two sessions counts once."""
        query = f"SELECT COUNT(DISTINCT {count_distinct}) AS counted FROM {table} WHERE {end_column} IS NULL"
        return self._backend.fetch_one(query)["counted"]

    def _count_open_since(self, table: str, end_column: str, since: str, count_distinct: str = "id") -> int:
        """How many rows of the table were open at any moment since the given stored time: still
        open, or ended since. Each value of count_distinct counts once."""
        # Table and column names come from this class only, never from outside, so formatting
        # them into the query is safe; the time is passed as a parameter.
        # Every stored row started before now, so a row overlaps the period exactly when it has not
        # ended before the period's start. Stored times compare correctly as text (to_stored_time).
        query = (
            f"SELECT COUNT(DISTINCT {count_distinct}) AS counted FROM {table} "
            f"WHERE {end_column} IS NULL OR {end_column} >= ?"
        )
        return self._backend.fetch_one(query, (since,))["counted"]


def account_from_row(row: Row | None) -> Account | None:
    """The account a row of the accounts table describes, or None for no row."""
    if row is None:
        return None
    return Account(
        account_id=row["id"],
        name=row["name"],
        is_admin=bool(row["is_admin"]),
        banned_until=from_optional_stored_time(row["banned_until"]),
        ban_reason=row["ban_reason"],
        created_at=from_stored_time(row["created_at"]),
        last_logon_at=from_optional_stored_time(row["last_logon_at"]),
    )


def board_theme_from_row(row: Row) -> BoardTheme:
    """The board theme a row of the board_themes table describes."""
    return BoardTheme(
        theme_id=row["id"],
        language=row["language"],
        name=row["name"],
        description=row["description"],
        is_server_written=bool(row["is_server_written"]),
        position=row["position"],
        is_hidden=bool(row["is_hidden"]),
    )


def board_message_from_row(row: Row) -> BoardMessage:
    """The board message a row of the board_messages table describes."""
    return BoardMessage(
        message_id=row["id"],
        theme_id=row["theme_id"],
        title=row["title"],
        body=row["body"],
        author=row["author"],
        posted_at=from_stored_time(row["posted_at"]),
        position=row["position"],
        is_hidden=bool(row["is_hidden"]),
    )


def clan_from_row(row: Row) -> Clan:
    """The clan a row with the columns id, name and is_highlighted describes."""
    return Clan(clan_id=row["id"], name=row["name"], is_highlighted=bool(row["is_highlighted"]))


def game_record_from_row(row: Row, player_names: list[str]) -> GameRecord:
    """The game a GAME_COLUMNS row describes, with the names of the accounts that joined it.
    The database driver may hand the announcement over as another bytes-like type; it is made bytes."""
    announcement = row["announcement"]
    if announcement is not None:
        announcement = bytes(announcement)
    return GameRecord(
        game_id=row["id"],
        name=row["name"],
        host_name=row["host_name"] or "",
        opened_at=from_stored_time(row["opened_at"]),
        closed_at=from_optional_stored_time(row["closed_at"]),
        group_id=row["group_id"],
        announcement=announcement,
        player_names=tuple(player_names),
        ladder_type=row["ladder_type"],
    )


def check_season(name: str, starts_at: datetime, ends_at: datetime | None) -> None:
    """Raise DatabaseError unless a custom season can be stored: a name the game can show (it is
    named in chat answers and on the board) and an end after the start."""
    check_title("season name", name)
    check_game_text("season name", name, MAX_GROUP_TEXT_LENGTH)
    if ends_at is not None and ends_at <= starts_at:
        raise DatabaseError("a season must end after it starts")


def season_from_row(row: Row) -> Season:
    """The custom season a ladder_seasons row describes."""
    return Season(
        season_id=row["id"],
        name=row["name"],
        starts_at=from_stored_time(row["starts_at"]),
        ends_at=from_optional_stored_time(row["ends_at"]),
        counts=SeasonCounts(row["counts"]),
    )


def game_report_record_from_row(row: Row) -> GameReportRecord:
    """The match report a GAME_REPORT_COLUMNS row describes; an empty sender name when the
    account was deleted since."""
    return GameReportRecord(
        report_id=row["id"],
        sender_name=row["sender_name"] or "",
        group_id=row["group_id"],
        size=row["size"],
        received_at=from_stored_time(row["received_at"]),
        report=bytes(row["report"]),
        game_id=row["game_id"],
        sender_account_id=row["sender_account_id"],
    )
