"""Everything the lobby keeps between restarts, in one database: a SQLite file, or a MySQL or
MariaDB database (see backends.py for the locations, migrations.py for the schema).

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
    BANNER_URL_PREFIX,
    BOARD_LANGUAGES,
    MAX_ANNOUNCEMENT_LENGTH,
    MAX_GROUP_TEXT_LENGTH,
    MAX_TEXT_MESSAGE_BYTES,
    NEWS_AUDIENCES,
    TEXT_ENCODING,
)
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
    "settings",
    "pending_announcements",
    "live_state",
    "pending_kicks",
    "web_sessions",
)

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
    # Shown in the chat as [Server] right after a player enters the lobby; empty for none.
    WELCOME_MESSAGE = "welcome_message"
    # The advert banner on the Home tab: the game downloads banner_url by anonymous FTP whenever the
    # two ids differ from the ones it saw last. Empty url for no banner.
    BANNER_URL = "banner_url"
    BANNER_ID = "banner_id"
    BANNER_SUB_ID = "banner_sub_id"
    # Admins' rows in the room member lists are drawn in another colour (AddPlayerToGroup 0x26).
    # Off by default: no run with the real game has shown this message yet.
    MARK_ADMINS = "mark_admins"
    # After each logon from the internet, probe whether other players can reach the player
    # directly (reachability.py). Off by default: no run with the real game has seen the probe.
    CHECK_REACHABILITY = "check_reachability"

    @property
    def key(self) -> str:
        return self.value

    @property
    def default(self) -> str:
        return SETTING_DEFAULTS[self]


SETTING_DEFAULTS = {
    Setting.MAX_PLAYERS: "0",
    Setting.UNIQUE_SERIAL_NUMBERS: "false",
    Setting.WELCOME_MESSAGE: "",
    Setting.BANNER_URL: "",
    Setting.BANNER_ID: "0",
    Setting.BANNER_SUB_ID: "0",
    Setting.MARK_ADMINS: "false",
    Setting.CHECK_REACHABILITY: "false",
}

# What a player is told when their account is deleted while they are online.
ACCOUNT_REMOVED_KICK_REASON = "Your account was removed."

# live_state holds one row, the running server's lobby.
LIVE_STATE_ROW_ID = 1

TRUE_SETTING_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_SETTING_VALUES = frozenset({"0", "false", "no", "off"})
# The numeric settings travel to the game as u32 fields.
INTEGER_SETTINGS = frozenset({Setting.MAX_PLAYERS, Setting.BANNER_ID, Setting.BANNER_SUB_ID})
BOOLEAN_SETTINGS = frozenset({Setting.UNIQUE_SERIAL_NUMBERS, Setting.MARK_ADMINS, Setting.CHECK_REACHABILITY})
MAX_INTEGER_SETTING = 0xFFFFFFFF

LINE_BREAK = "\n"
FIRST_PRINTABLE_CHARACTER = " "


@dataclass(frozen=True)
class Account:
    account_id: int
    name: str
    is_admin: bool
    banned_until: datetime | None
    ban_reason: str
    created_at: datetime | None = None
    last_logon_at: datetime | None = None

    def is_banned_at(self, moment: datetime) -> bool:
        return self.banned_until is not None and self.banned_until > moment


@dataclass(frozen=True)
class Clan:
    clan_id: int
    name: str


@dataclass(frozen=True)
class News:
    title: str
    body: str


@dataclass(frozen=True)
class ChatTheme:
    theme_id: int
    name: str
    description: str


@dataclass(frozen=True)
class BoardTheme:
    theme_id: int
    language: str
    name: str
    description: str


@dataclass(frozen=True)
class BoardMessage:
    message_id: int
    theme_id: int
    title: str
    body: str
    author: str
    posted_at: datetime


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
        return self.revoked_at is None


@dataclass(frozen=True)
class GameRecord:
    """A game as the statistics keep it; host_name is empty when the host's account was removed."""

    game_id: int
    name: str
    host_name: str
    opened_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True)
class GameReportRecord:
    """A stored match report with the bytes the game sent (a GameStatus body, a few hundred
    bytes at most)."""

    report_id: int
    sender_name: str
    group_id: int
    size: int
    received_at: datetime
    report: bytes = b""


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
    text: str
    created_at: datetime
    sent_at: datetime | None


@dataclass(frozen=True)
class LobbyStatistics:
    """The six numbers of the Home tab's statistics table, in the order the game shows them."""

    players_online: int
    players_last_hour: int
    players_last_day: int
    games_open: int
    games_last_hour: int
    games_last_day: int


def is_valid_player_name(name: str) -> bool:
    return PLAYER_NAME_PATTERN.fullmatch(name) is not None


def check_game_text(label: str, text: str, max_length: int, allows_line_breaks: bool = False) -> None:
    """Raise DatabaseError unless the game can show the text: every character must exist in its
    code page, it must fit max_length characters, and it may hold no control characters (line
    breaks only where allows_line_breaks). Texts reach the game exactly as stored, so a text that
    passes here never makes the server fail while encoding it for a client."""
    try:
        text.encode(TEXT_ENCODING)
    except UnicodeEncodeError as error:
        bad_character = text[error.start]
        raise DatabaseError(f"{label} contains {bad_character!r}, which the game cannot show") from error
    if len(text) > max_length:
        raise DatabaseError(f"{label} is {len(text)} characters long; the most is {max_length}")
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
        if not value.isdigit() or int(value) > MAX_INTEGER_SETTING:
            raise DatabaseError(f"{setting.key} must be a whole number from 0 to {MAX_INTEGER_SETTING}")
        return
    if setting in BOOLEAN_SETTINGS:
        if value.strip().lower() not in TRUE_SETTING_VALUES | FALSE_SETTING_VALUES:
            raise DatabaseError(f"{setting.key} must be true or false")
        return
    if setting == Setting.BANNER_URL:
        if value and not value.startswith(BANNER_URL_PREFIX):
            raise DatabaseError(f"{setting.key} must start with {BANNER_URL_PREFIX}, or be empty for no banner")
        check_game_text(setting.key, value, MAX_TEXT_MESSAGE_BYTES)
        return
    if setting == Setting.WELCOME_MESSAGE:
        check_game_text(setting.key, value, MAX_ANNOUNCEMENT_LENGTH)
        return
    raise AssertionError(f"no check for setting {setting.key}")


def ban_kick_reason(until: datetime, reason: str) -> str:
    """What a banned player who is online is told as they are removed."""
    text = f"You are banned until {until.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC"
    if reason:
        return f"{text}: {reason}"
    return f"{text}."


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_stored_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_stored_time(text: str | None) -> datetime | None:
    if text is None:
        return None
    return datetime.fromisoformat(text)


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
    # Normalizing here, where both storing and checking pass, keeps the two from ever disagreeing.
    return hashlib.scrypt(
        normalize_access_code(access_code).encode("utf-8"),
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
    return "".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(ACCESS_CODE_LENGTH))


class LobbyDatabase:
    """The lobby's database. One instance per process and thread; all calls come from the event
    loop's thread, and each call is short, so they run synchronously."""

    def __init__(
        self, location: str | Path, clock: Callable[[], datetime] = utc_now, applies_migrations: bool = True
    ) -> None:
        """location is a SQLite file, ":memory:" for a SQLite database that lives only as long as
        this object, or mysql://user@host/name (backends.py). The migrations the database is
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
            self._backend.close()
            raise DatabaseError(str(error)) from error

    def close(self) -> None:
        self._backend.close()

    @property
    def dialect(self) -> str:
        return self._backend.dialect

    @property
    def description(self) -> str:
        """The location as it may be shown in logs: without a password."""
        return describe_location(self.location)

    @property
    def schema_version(self) -> int:
        return current_version(self._backend)

    def read_migration_status(self) -> MigrationStatus:
        try:
            return read_status(self._backend)
        except MigrationError as error:
            raise DatabaseError(str(error)) from error

    def copy_to(self, target: LobbyDatabase) -> dict[str, int]:
        """Copy every row into target, which must be empty and at the same schema version; ids
        are kept, so every reference stays intact. Returns the rows copied per table. This moves a
        lobby from SQLite to MySQL or back; no server may write to either database meanwhile."""
        if target.schema_version != self.schema_version:
            raise DatabaseError(
                f"the target has schema version {target.schema_version}, the source {self.schema_version}"
            )
        tables_with_rows = [table for table in TABLES_IN_DEPENDENCY_ORDER if target._has_rows(table)]
        if tables_with_rows:
            raise DatabaseError(f"the target is not empty: {', '.join(tables_with_rows)} already hold rows")
        copied_rows = {}
        with target._backend.transaction():
            for table in TABLES_IN_DEPENDENCY_ORDER:
                rows = self._backend.fetch_all(f"SELECT * FROM {table} ORDER BY 1")
                copied_rows[table] = len(rows)
                if not rows:
                    continue
                columns = list(rows[0])
                insert = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders(len(columns))})"
                parameter_rows = []
                for row in rows:
                    parameter_rows.append([row[column] for column in columns])
                target._backend.execute_many(insert, parameter_rows)
        return copied_rows

    def _has_rows(self, table: str) -> bool:
        # The table name comes from TABLES_IN_DEPENDENCY_ORDER only.
        return self._backend.fetch_one(f"SELECT 1 AS found FROM {table} LIMIT 1") is not None

    # Settings.

    def get_setting(self, setting: Setting) -> str:
        row = self._backend.fetch_one("SELECT value FROM settings WHERE name = ?", (setting.key,))
        if row is None:
            return setting.default
        return row["value"]

    def get_integer_setting(self, setting: Setting) -> int:
        try:
            return int(self.get_setting(setting))
        except ValueError as error:
            raise DatabaseError(f"setting {setting.key} is not a number") from error

    def get_boolean_setting(self, setting: Setting) -> bool:
        return self.get_setting(setting).strip().lower() in TRUE_SETTING_VALUES

    def set_setting(self, setting: Setting, value: str) -> None:
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
        if not is_valid_player_name(name):
            raise DatabaseError(f"{name!r} is not a valid player name: only letters A to Z and single spaces")
        try:
            with self._backend.transaction():
                result = self._backend.execute(
                    "INSERT INTO accounts (name, is_admin, created_at) VALUES (?, ?, ?)",
                    (name, int(is_admin), to_stored_time(self._clock())),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"an account named {name!r} already exists") from error
        return self.find_account_by_id(result.last_row_id)

    def find_account(self, name: str) -> Account | None:
        row = self._backend.fetch_one("SELECT * FROM accounts WHERE name = ?", (name,))
        return account_from_row(row)

    def find_account_by_id(self, account_id: int) -> Account | None:
        row = self._backend.fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        return account_from_row(row)

    def list_accounts(self) -> list[Account]:
        rows = self._backend.fetch_all("SELECT * FROM accounts ORDER BY name")
        return [account_from_row(row) for row in rows]

    def delete_account(self, account_id: int) -> None:
        """Delete the account; a running server also removes the player from the lobby."""
        with self._backend.transaction():
            self._backend.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            self._queue_kick(account_id, ACCOUNT_REMOVED_KICK_REASON)

    def set_admin(self, account_id: int, is_admin: bool) -> None:
        with self._backend.transaction():
            self._backend.execute("UPDATE accounts SET is_admin = ? WHERE id = ?", (int(is_admin), account_id))

    def ban_account(self, account_id: int, until: datetime, reason: str) -> None:
        """Bar the account from logging on until the given moment. A player who is online is also
        removed from the running lobby; one who is offline is refused at the next logon."""
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
        self._backend.execute(
            "INSERT INTO pending_kicks (account_id, reason, created_at) VALUES (?, ?, ?)",
            (account_id, reason, to_stored_time(self._clock())),
        )

    def take_pending_kicks(self) -> list[PendingKick]:
        """The kicks not carried out yet, oldest first, marked as taken in the same step."""
        with self._backend.transaction():
            rows = self._backend.fetch_all(
                "SELECT id, account_id, reason FROM pending_kicks WHERE taken_at IS NULL ORDER BY id"
            )
            taken_at = to_stored_time(self._clock())
            for row in rows:
                self._backend.execute("UPDATE pending_kicks SET taken_at = ? WHERE id = ?", (taken_at, row["id"]))
        return [PendingKick(account_id=row["account_id"], reason=row["reason"]) for row in rows]

    def unban_account(self, account_id: int) -> None:
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE accounts SET banned_until = NULL, ban_reason = '' WHERE id = ?", (account_id,)
            )

    def add_access_code(self, account_id: int, access_code: str, label: str = "") -> int:
        """Store an access code for an account and return its id. The code itself is not kept."""
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
        rows = self._backend.fetch_all(
            "SELECT id, label, created_at, last_used_at, revoked_at FROM access_codes WHERE account_id = ? ORDER BY id",
            (account_id,),
        )
        return [
            AccessCodeRecord(
                code_id=row["id"],
                label=row["label"],
                created_at=from_stored_time(row["created_at"]),
                last_used_at=from_stored_time(row["last_used_at"]),
                revoked_at=from_stored_time(row["revoked_at"]),
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
        if now - last_seen_at >= WEB_SESSION_TOUCH_INTERVAL:
            with self._backend.transaction():
                self._backend.execute(
                    "UPDATE web_sessions SET last_seen_at = ? WHERE id = ?", (to_stored_time(now), row["id"])
                )
        return WebSession(account_id=row["account_id"], access_code_id=row["access_code_id"])

    def end_web_session(self, token: str) -> None:
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
        row = self._backend.fetch_one("SELECT id, name FROM clans WHERE name = ?", (name,))
        if row is None:
            return None
        return Clan(clan_id=row["id"], name=row["name"])

    def list_clans(self) -> list[Clan]:
        rows = self._backend.fetch_all("SELECT id, name FROM clans ORDER BY name")
        return [Clan(clan_id=row["id"], name=row["name"]) for row in rows]

    def delete_clan(self, clan_id: int) -> None:
        with self._backend.transaction():
            self._backend.execute("DELETE FROM clans WHERE id = ?", (clan_id,))

    def set_clan_of_account(self, account_id: int, clan_id: int | None) -> None:
        """Put an account into a clan, or take it out with clan_id None. One clan per account."""
        with self._backend.transaction():
            self._backend.execute("DELETE FROM clan_members WHERE account_id = ?", (account_id,))
            if clan_id is not None:
                self._backend.execute(
                    "INSERT INTO clan_members (account_id, clan_id, joined_at) VALUES (?, ?, ?)",
                    (account_id, clan_id, to_stored_time(self._clock())),
                )

    def find_clan_of_account(self, account_id: int) -> Clan | None:
        row = self._backend.fetch_one(
            "SELECT clans.id, clans.name FROM clans JOIN clan_members ON clan_members.clan_id = clans.id "
            "WHERE clan_members.account_id = ?",
            (account_id,),
        )
        if row is None:
            return None
        return Clan(clan_id=row["id"], name=row["name"])

    def list_clan_member_names(self, clan_id: int) -> list[str]:
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
        row = self._backend.fetch_one("SELECT title, body FROM news WHERE audience = ?", (audience,))
        if row is None:
            return None
        return News(title=row["title"], body=row["body"])

    # ChatZone themes.

    def add_chat_theme(self, name: str, description: str = "") -> ChatTheme:
        check_title("chat theme name", name)
        check_game_text("chat theme description", description, MAX_GROUP_TEXT_LENGTH)
        try:
            with self._backend.transaction():
                result = self._backend.execute(
                    "INSERT INTO chat_themes (name, description, created_at) VALUES (?, ?, ?)",
                    (name, description, to_stored_time(self._clock())),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"a chat theme named {name!r} already exists") from error
        return ChatTheme(theme_id=result.last_row_id, name=name, description=description)

    def list_chat_themes(self) -> list[ChatTheme]:
        rows = self._backend.fetch_all("SELECT * FROM chat_themes ORDER BY id")
        return [ChatTheme(theme_id=row["id"], name=row["name"], description=row["description"]) for row in rows]

    def delete_chat_theme(self, theme_id: int) -> None:
        with self._backend.transaction():
            self._backend.execute("DELETE FROM chat_themes WHERE id = ?", (theme_id,))

    # MessageZone board.

    def add_board_theme(self, language: str, name: str, description: str = "") -> BoardTheme:
        if language not in BOARD_LANGUAGES:
            raise DatabaseError(f"unknown board language {language!r}; known: {', '.join(BOARD_LANGUAGES)}")
        check_title("board theme name", name)
        check_game_text("board theme description", description, MAX_GROUP_TEXT_LENGTH)
        try:
            with self._backend.transaction():
                result = self._backend.execute(
                    "INSERT INTO board_themes (language, name, description, created_at) VALUES (?, ?, ?, ?)",
                    (language, name, description, to_stored_time(self._clock())),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"a {language} board theme named {name!r} already exists") from error
        return BoardTheme(theme_id=result.last_row_id, language=language, name=name, description=description)

    def list_board_themes(self) -> list[BoardTheme]:
        rows = self._backend.fetch_all("SELECT * FROM board_themes ORDER BY language, id")
        return [board_theme_from_row(row) for row in rows]

    def delete_board_theme(self, theme_id: int) -> None:
        with self._backend.transaction():
            self._backend.execute("DELETE FROM board_themes WHERE id = ?", (theme_id,))

    def post_board_message(self, theme_id: int, title: str, body: str, author: str = "") -> BoardMessage:
        check_title("message title", title)
        check_game_text("message text", body, MAX_TEXT_MESSAGE_BYTES, allows_line_breaks=True)
        check_game_text("author", author, MAX_GROUP_TEXT_LENGTH)
        posted_at = self._clock()
        try:
            with self._backend.transaction():
                result = self._backend.execute(
                    "INSERT INTO board_messages (theme_id, title, body, author, posted_at) VALUES (?, ?, ?, ?, ?)",
                    (theme_id, title, body, author, to_stored_time(posted_at)),
                )
        except ConstraintViolation as error:
            raise DatabaseError(f"there is no board theme {theme_id}") from error
        return BoardMessage(result.last_row_id, theme_id, title, body, author, posted_at)

    def list_board_messages(self, theme_id: int) -> list[BoardMessage]:
        rows = self._backend.fetch_all("SELECT * FROM board_messages WHERE theme_id = ? ORDER BY id", (theme_id,))
        return [board_message_from_row(row) for row in rows]

    def find_board_message(self, message_id: int) -> BoardMessage | None:
        row = self._backend.fetch_one("SELECT * FROM board_messages WHERE id = ?", (message_id,))
        if row is None:
            return None
        return board_message_from_row(row)

    def delete_board_message(self, message_id: int) -> None:
        with self._backend.transaction():
            self._backend.execute("DELETE FROM board_messages WHERE id = ?", (message_id,))

    # Games and their reports.

    def record_game_opened(self, name: str, host_account_id: int | None) -> int:
        with self._backend.transaction():
            result = self._backend.execute(
                "INSERT INTO games (name, host_account_id, opened_at) VALUES (?, ?, ?)",
                (name, host_account_id, to_stored_time(self._clock())),
            )
        return result.last_row_id

    def record_game_closed(self, game_record_id: int) -> None:
        with self._backend.transaction():
            self._backend.execute(
                "UPDATE games SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                (to_stored_time(self._clock()), game_record_id),
            )

    def store_game_report(self, account_id: int | None, group_id: int, report: bytes) -> None:
        with self._backend.transaction():
            self._backend.execute(
                "INSERT INTO game_reports (account_id, group_id, report, received_at) VALUES (?, ?, ?, ?)",
                (account_id, group_id, report, to_stored_time(self._clock())),
            )

    def count_game_reports(self) -> int:
        return self._backend.fetch_one("SELECT COUNT(*) AS report_count FROM game_reports")["report_count"]

    def list_recent_games(self, limit: int) -> list[GameRecord]:
        """The newest games first, open and closed."""
        rows = self._backend.fetch_all(
            "SELECT games.id, games.name, accounts.name AS host_name, games.opened_at, games.closed_at "
            "FROM games LEFT JOIN accounts ON accounts.id = games.host_account_id "
            "ORDER BY games.id DESC LIMIT ?",
            (limit,),
        )
        return [
            GameRecord(
                game_id=row["id"],
                name=row["name"],
                host_name=row["host_name"] or "",
                opened_at=from_stored_time(row["opened_at"]),
                closed_at=from_stored_time(row["closed_at"]),
            )
            for row in rows
        ]

    def list_recent_game_reports(self, limit: int) -> list[GameReportRecord]:
        """The newest match reports first."""
        rows = self._backend.fetch_all(
            "SELECT game_reports.id, accounts.name AS sender_name, game_reports.group_id, "
            "LENGTH(game_reports.report) AS size, game_reports.received_at, game_reports.report "
            "FROM game_reports LEFT JOIN accounts ON accounts.id = game_reports.account_id "
            "ORDER BY game_reports.id DESC LIMIT ?",
            (limit,),
        )
        return [
            GameReportRecord(
                report_id=row["id"],
                sender_name=row["sender_name"] or "",
                group_id=row["group_id"],
                size=row["size"],
                received_at=from_stored_time(row["received_at"]),
                report=bytes(row["report"]),
            )
            for row in rows
        ]

    def find_game_report(self, report_id: int) -> bytes | None:
        row = self._backend.fetch_one("SELECT report FROM game_reports WHERE id = ?", (report_id,))
        if row is None:
            return None
        return bytes(row["report"])

    # Announcements from outside the server process.

    def queue_announcement(self, text: str) -> None:
        check_title("announcement", text)
        check_game_text("announcement", text, MAX_ANNOUNCEMENT_LENGTH)
        with self._backend.transaction():
            self._backend.execute(
                "INSERT INTO pending_announcements (text, created_at) VALUES (?, ?)",
                (text, to_stored_time(self._clock())),
            )

    def take_pending_announcements(self) -> list[str]:
        """The announcements not sent yet, oldest first, marked as sent in the same step."""
        with self._backend.transaction():
            rows = self._backend.fetch_all(
                "SELECT id, text FROM pending_announcements WHERE sent_at IS NULL ORDER BY id"
            )
            sent_at = to_stored_time(self._clock())
            for row in rows:
                self._backend.execute(
                    "UPDATE pending_announcements SET sent_at = ? WHERE id = ?", (sent_at, row["id"])
                )
        return [row["text"] for row in rows]

    def list_recent_announcements(self, limit: int) -> list[AnnouncementRecord]:
        """The newest announcements first; sent_at is None while the server has not taken one yet."""
        rows = self._backend.fetch_all(
            "SELECT text, created_at, sent_at FROM pending_announcements ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            AnnouncementRecord(
                text=row["text"],
                created_at=from_stored_time(row["created_at"]),
                sent_at=from_stored_time(row["sent_at"]),
            )
            for row in rows
        ]

    # The running server's lobby, for the web interface.

    def write_live_state(self, state: LiveLobbyState) -> None:
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
            players_last_hour=self._count_open_since("lobby_sessions", "started_at", "ended_at", hour_ago, "account_id"),
            players_last_day=self._count_open_since("lobby_sessions", "started_at", "ended_at", day_ago, "account_id"),
            games_open=self._count_open("games", "closed_at"),
            games_last_hour=self._count_open_since("games", "opened_at", "closed_at", hour_ago),
            games_last_day=self._count_open_since("games", "opened_at", "closed_at", day_ago),
        )

    def _count_open(self, table: str, end_column: str, count_distinct: str = "id") -> int:
        query = f"SELECT COUNT(DISTINCT {count_distinct}) AS counted FROM {table} WHERE {end_column} IS NULL"
        return self._backend.fetch_one(query)["counted"]

    def _count_open_since(
        self, table: str, start_column: str, end_column: str, since: str, count_distinct: str = "id"
    ) -> int:
        # Table and column names come from this class only, never from outside, so formatting
        # them into the query is safe; the time is passed as a parameter.
        query = (
            f"SELECT COUNT(DISTINCT {count_distinct}) AS counted FROM {table} "
            f"WHERE {end_column} IS NULL OR {end_column} >= ?"
        )
        return self._backend.fetch_one(query, (since,))["counted"]


def account_from_row(row: Row | None) -> Account | None:
    if row is None:
        return None
    return Account(
        account_id=row["id"],
        name=row["name"],
        is_admin=bool(row["is_admin"]),
        banned_until=from_stored_time(row["banned_until"]),
        ban_reason=row["ban_reason"],
        created_at=from_stored_time(row["created_at"]),
        last_logon_at=from_stored_time(row["last_logon_at"]),
    )


def board_theme_from_row(row: Row) -> BoardTheme:
    return BoardTheme(theme_id=row["id"], language=row["language"], name=row["name"], description=row["description"])


def board_message_from_row(row: Row) -> BoardMessage:
    return BoardMessage(
        message_id=row["id"],
        theme_id=row["theme_id"],
        title=row["title"],
        body=row["body"],
        author=row["author"],
        posted_at=from_stored_time(row["posted_at"]),
    )
