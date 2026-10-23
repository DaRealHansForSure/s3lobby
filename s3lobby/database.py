"""Everything the lobby keeps between restarts, in one SQLite file.

- accounts and their access codes: a player logs on with the account name as player name and an
  access code as password. Codes are random strings the server generates (today with the command
  line, later through a web interface); only a salted scrypt hash of each is stored.
- clans and who belongs to them
- the news on the lobby's Home tab, and the message board of the MessageZone tab
- the chat themes listed on the ChatZone tab
- lobby sessions and games, for the statistics table on the Home tab
- the game reports clients send after a match
- settings, such as the player limit or the welcome message
- announcements queued for the running server, and the server's live lobby for the web interface

Accounts are only ever created on the server side (the command line today, a web interface later).
The game's own "create player" button is refused at logon.

All times are stored as UTC text in ISO 8601 format, which SQLite compares correctly as text.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

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

IN_MEMORY_DATABASE = ":memory:"
# The file holds access code hashes and player data: readable and writable by its owner only.
DATABASE_FILE_MODE = 0o600

# Bump together with a new entry in SCHEMA_UPGRADES whenever the schema changes.
SCHEMA_VERSION = 3

SCHEMA_UPGRADES = {
    1: """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            is_admin INTEGER NOT NULL DEFAULT 0,
            banned_until TEXT,
            ban_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_logon_at TEXT
        );
        CREATE TABLE access_codes (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            code_hash TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT
        );
        CREATE INDEX access_codes_by_account ON access_codes(account_id);
        CREATE TABLE clans (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE clan_members (
            account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
            clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
            joined_at TEXT NOT NULL
        );
        CREATE TABLE news (
            audience TEXT PRIMARY KEY CHECK (audience IN ('german', 'other')),
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE chat_themes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE board_themes (
            id INTEGER PRIMARY KEY,
            language TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE (language, name)
        );
        CREATE TABLE board_messages (
            id INTEGER PRIMARY KEY,
            theme_id INTEGER NOT NULL REFERENCES board_themes(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            posted_at TEXT NOT NULL
        );
        CREATE INDEX board_messages_by_theme ON board_messages(theme_id);
        CREATE TABLE lobby_sessions (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        CREATE TABLE games (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            host_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT
        );
        CREATE TABLE game_reports (
            id INTEGER PRIMARY KEY,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            group_id INTEGER NOT NULL,
            report BLOB NOT NULL,
            received_at TEXT NOT NULL
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """,
    # Announcements written by another process (the command line, later a web interface) for the
    # running server to send to every player.
    2: """
        CREATE TABLE pending_announcements (
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT
        );
    """,
    # The running server's lobby as JSON (s3lobby/live.py), one row, for the web interface to show.
    3: """
        CREATE TABLE live_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            snapshot TEXT NOT NULL,
            written_at TEXT NOT NULL
        );
    """,
}

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
# field. The alphabet leaves out characters that are easy to mix up (0/O, 1/I/L).
ACCESS_CODE_LENGTH = 12
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
}

TRUE_SETTING_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_SETTING_VALUES = frozenset({"0", "false", "no", "off"})
# The numeric settings travel to the game as u32 fields.
INTEGER_SETTINGS = frozenset({Setting.MAX_PLAYERS, Setting.BANNER_ID, Setting.BANNER_SUB_ID})
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
    """A stored match report without its bytes; find_game_report returns those."""

    report_id: int
    sender_name: str
    group_id: int
    size: int
    received_at: datetime


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


class DatabaseError(Exception):
    """A request the stored data does not allow, such as adding an account name twice."""


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
    if setting == Setting.UNIQUE_SERIAL_NUMBERS:
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
    expected_digest = bytes.fromhex(digest_hex)
    offered_digest = scrypt_digest(access_code, bytes.fromhex(salt_hex), int(cost), int(block_size), int(parallelism))
    return hmac.compare_digest(offered_digest, expected_digest)


def scrypt_digest(access_code: str, salt: bytes, cost: int, block_size: int, parallelism: int) -> bytes:
    return hashlib.scrypt(
        access_code.encode("utf-8"),
        salt=salt,
        n=cost,
        r=block_size,
        p=parallelism,
        dklen=SCRYPT_DIGEST_BYTES,
    )


def generate_access_code() -> str:
    return "".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(ACCESS_CODE_LENGTH))


class LobbyDatabase:
    """The lobby's SQLite file. One instance per process; all calls come from the event loop's
    thread, and each call is short, so they run synchronously."""

    def __init__(self, path: str | Path, clock: Callable[[], datetime] = utc_now) -> None:
        """path may be ":memory:" for a database that lives only as long as this object."""
        self._clock = clock
        is_new_file = str(path) != IN_MEMORY_DATABASE and not Path(path).exists()
        if is_new_file:
            # Create the empty file with its final mode first; SQLite would create it world-readable.
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, DATABASE_FILE_MODE))
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if str(path) != IN_MEMORY_DATABASE:
            # Write-ahead logging lets the web interface read while the server writes, instead of
            # one waiting for the other. The setting is stored in the file.
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._upgrade_schema()

    def close(self) -> None:
        self._connection.close()

    @property
    def schema_version(self) -> int:
        return self._connection.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit when the block succeeds, roll back when it raises."""
        with self._connection:
            yield self._connection

    def _upgrade_schema(self) -> None:
        current_version = self.schema_version
        if current_version > SCHEMA_VERSION:
            raise DatabaseError(
                f"the database has schema version {current_version}, this s3lobby knows up to {SCHEMA_VERSION}"
            )
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            with self._transaction() as connection:
                connection.executescript(SCHEMA_UPGRADES[version])
                connection.execute(f"PRAGMA user_version = {version}")

    # Settings.

    def get_setting(self, setting: Setting) -> str:
        row = self._connection.execute("SELECT value FROM settings WHERE key = ?", (setting.key,)).fetchone()
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
        check_setting_value(setting, value)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (setting.key, value),
            )

    # Accounts and access codes.

    def create_account(self, name: str, is_admin: bool = False) -> Account:
        if not is_valid_player_name(name):
            raise DatabaseError(f"{name!r} is not a valid player name: only letters A to Z and single spaces")
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO accounts (name, is_admin, created_at) VALUES (?, ?, ?)",
                    (name, int(is_admin), to_stored_time(self._clock())),
                )
        except sqlite3.IntegrityError as error:
            raise DatabaseError(f"an account named {name!r} already exists") from error
        return self.find_account_by_id(cursor.lastrowid)

    def find_account(self, name: str) -> Account | None:
        row = self._connection.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
        return account_from_row(row)

    def find_account_by_id(self, account_id: int) -> Account | None:
        row = self._connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return account_from_row(row)

    def list_accounts(self) -> list[Account]:
        rows = self._connection.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        return [account_from_row(row) for row in rows]

    def delete_account(self, account_id: int) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def set_admin(self, account_id: int, is_admin: bool) -> None:
        with self._transaction() as connection:
            connection.execute("UPDATE accounts SET is_admin = ? WHERE id = ?", (int(is_admin), account_id))

    def ban_account(self, account_id: int, until: datetime, reason: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE accounts SET banned_until = ?, ban_reason = ? WHERE id = ?",
                (to_stored_time(until), reason, account_id),
            )

    def unban_account(self, account_id: int) -> None:
        with self._transaction() as connection:
            connection.execute("UPDATE accounts SET banned_until = NULL, ban_reason = '' WHERE id = ?", (account_id,))

    def add_access_code(self, account_id: int, access_code: str, label: str = "") -> int:
        """Store an access code for an account and return its id. The code itself is not kept."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO access_codes (account_id, code_hash, label, created_at) VALUES (?, ?, ?, ?)",
                (account_id, hash_access_code(access_code), label, to_stored_time(self._clock())),
            )
        return cursor.lastrowid

    def issue_access_code(self, account_id: int, label: str = "") -> str:
        """Generate a new access code for an account, store its hash, and return the code: this is
        the only moment it can be shown to the player."""
        access_code = generate_access_code()
        self.add_access_code(account_id, access_code, label)
        return access_code

    def revoke_access_codes(self, account_id: int) -> int:
        """Revoke every active access code of an account; returns how many were revoked."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE access_codes SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
                (to_stored_time(self._clock()), account_id),
            )
        return cursor.rowcount

    def revoke_access_code(self, account_id: int, code_id: int) -> bool:
        """Revoke one access code of an account; False when it has no such active code."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE access_codes SET revoked_at = ? WHERE id = ? AND account_id = ? AND revoked_at IS NULL",
                (to_stored_time(self._clock()), code_id, account_id),
            )
        return cursor.rowcount == 1

    def list_access_codes(self, account_id: int) -> list[AccessCodeRecord]:
        rows = self._connection.execute(
            "SELECT id, label, created_at, last_used_at, revoked_at FROM access_codes WHERE account_id = ? ORDER BY id",
            (account_id,),
        ).fetchall()
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
        rows = self._connection.execute(
            "SELECT id, code_hash FROM access_codes WHERE account_id = ? AND revoked_at IS NULL",
            (account_id,),
        ).fetchall()
        for row in rows:
            if is_matching_access_code(access_code, row["code_hash"]):
                with self._transaction() as connection:
                    connection.execute(
                        "UPDATE access_codes SET last_used_at = ? WHERE id = ?",
                        (to_stored_time(self._clock()), row["id"]),
                    )
                return True
        return False

    def record_logon(self, account_id: int) -> int:
        """Note a logon for the statistics; returns the session id to close on logout."""
        now = to_stored_time(self._clock())
        with self._transaction() as connection:
            connection.execute("UPDATE accounts SET last_logon_at = ? WHERE id = ?", (now, account_id))
            cursor = connection.execute(
                "INSERT INTO lobby_sessions (account_id, started_at) VALUES (?, ?)",
                (account_id, now),
            )
        return cursor.lastrowid

    def record_logout(self, session_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE lobby_sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (to_stored_time(self._clock()), session_id),
            )

    def close_open_sessions_and_games(self) -> None:
        """At server start nobody is connected: end what a previous run left open."""
        now = to_stored_time(self._clock())
        with self._transaction() as connection:
            connection.execute("UPDATE lobby_sessions SET ended_at = ? WHERE ended_at IS NULL", (now,))
            connection.execute("UPDATE games SET closed_at = ? WHERE closed_at IS NULL", (now,))

    # Clans.

    def create_clan(self, name: str) -> Clan:
        check_title("clan name", name)
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO clans (name, created_at) VALUES (?, ?)",
                    (name, to_stored_time(self._clock())),
                )
        except sqlite3.IntegrityError as error:
            raise DatabaseError(f"a clan named {name!r} already exists") from error
        return Clan(clan_id=cursor.lastrowid, name=name)

    def find_clan(self, name: str) -> Clan | None:
        row = self._connection.execute("SELECT id, name FROM clans WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        return Clan(clan_id=row["id"], name=row["name"])

    def list_clans(self) -> list[Clan]:
        rows = self._connection.execute("SELECT id, name FROM clans ORDER BY name").fetchall()
        return [Clan(clan_id=row["id"], name=row["name"]) for row in rows]

    def delete_clan(self, clan_id: int) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM clans WHERE id = ?", (clan_id,))

    def set_clan_of_account(self, account_id: int, clan_id: int | None) -> None:
        """Put an account into a clan, or take it out with clan_id None. One clan per account."""
        with self._transaction() as connection:
            connection.execute("DELETE FROM clan_members WHERE account_id = ?", (account_id,))
            if clan_id is not None:
                connection.execute(
                    "INSERT INTO clan_members (account_id, clan_id, joined_at) VALUES (?, ?, ?)",
                    (account_id, clan_id, to_stored_time(self._clock())),
                )

    def find_clan_of_account(self, account_id: int) -> Clan | None:
        row = self._connection.execute(
            "SELECT clans.id, clans.name FROM clans JOIN clan_members ON clan_members.clan_id = clans.id "
            "WHERE clan_members.account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        return Clan(clan_id=row["id"], name=row["name"])

    def list_clan_member_names(self, clan_id: int) -> list[str]:
        rows = self._connection.execute(
            "SELECT accounts.name FROM accounts JOIN clan_members ON clan_members.account_id = accounts.id "
            "WHERE clan_members.clan_id = ? ORDER BY accounts.name",
            (clan_id,),
        ).fetchall()
        return [row["name"] for row in rows]

    # Home tab news.

    def set_news(self, audience: str, title: str, body: str) -> None:
        """audience "german" is shown to German games, "other" to every other language."""
        if audience not in NEWS_AUDIENCES:
            raise DatabaseError(f"unknown news audience {audience!r}; known: {', '.join(NEWS_AUDIENCES)}")
        check_title("news title", title)
        check_game_text("news text", body, MAX_TEXT_MESSAGE_BYTES, allows_line_breaks=True)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO news (audience, title, body, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (audience) DO UPDATE SET title = excluded.title, body = excluded.body, "
                "updated_at = excluded.updated_at",
                (audience, title, body, to_stored_time(self._clock())),
            )

    def find_news(self, audience: str) -> News | None:
        row = self._connection.execute("SELECT title, body FROM news WHERE audience = ?", (audience,)).fetchone()
        if row is None:
            return None
        return News(title=row["title"], body=row["body"])

    # ChatZone themes.

    def add_chat_theme(self, name: str, description: str = "") -> ChatTheme:
        check_title("chat theme name", name)
        check_game_text("chat theme description", description, MAX_GROUP_TEXT_LENGTH)
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO chat_themes (name, description, created_at) VALUES (?, ?, ?)",
                    (name, description, to_stored_time(self._clock())),
                )
        except sqlite3.IntegrityError as error:
            raise DatabaseError(f"a chat theme named {name!r} already exists") from error
        return ChatTheme(theme_id=cursor.lastrowid, name=name, description=description)

    def list_chat_themes(self) -> list[ChatTheme]:
        rows = self._connection.execute("SELECT * FROM chat_themes ORDER BY id").fetchall()
        return [ChatTheme(theme_id=row["id"], name=row["name"], description=row["description"]) for row in rows]

    def delete_chat_theme(self, theme_id: int) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM chat_themes WHERE id = ?", (theme_id,))

    # MessageZone board.

    def add_board_theme(self, language: str, name: str, description: str = "") -> BoardTheme:
        if language not in BOARD_LANGUAGES:
            raise DatabaseError(f"unknown board language {language!r}; known: {', '.join(BOARD_LANGUAGES)}")
        check_title("board theme name", name)
        check_game_text("board theme description", description, MAX_GROUP_TEXT_LENGTH)
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO board_themes (language, name, description, created_at) VALUES (?, ?, ?, ?)",
                    (language, name, description, to_stored_time(self._clock())),
                )
        except sqlite3.IntegrityError as error:
            raise DatabaseError(f"a {language} board theme named {name!r} already exists") from error
        return BoardTheme(theme_id=cursor.lastrowid, language=language, name=name, description=description)

    def list_board_themes(self) -> list[BoardTheme]:
        rows = self._connection.execute("SELECT * FROM board_themes ORDER BY language, id").fetchall()
        return [board_theme_from_row(row) for row in rows]

    def delete_board_theme(self, theme_id: int) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM board_themes WHERE id = ?", (theme_id,))

    def post_board_message(self, theme_id: int, title: str, body: str, author: str = "") -> BoardMessage:
        check_title("message title", title)
        check_game_text("message text", body, MAX_TEXT_MESSAGE_BYTES, allows_line_breaks=True)
        check_game_text("author", author, MAX_GROUP_TEXT_LENGTH)
        posted_at = self._clock()
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO board_messages (theme_id, title, body, author, posted_at) VALUES (?, ?, ?, ?, ?)",
                    (theme_id, title, body, author, to_stored_time(posted_at)),
                )
        except sqlite3.IntegrityError as error:
            raise DatabaseError(f"there is no board theme {theme_id}") from error
        return BoardMessage(cursor.lastrowid, theme_id, title, body, author, posted_at)

    def list_board_messages(self, theme_id: int) -> list[BoardMessage]:
        rows = self._connection.execute(
            "SELECT * FROM board_messages WHERE theme_id = ? ORDER BY id", (theme_id,)
        ).fetchall()
        return [board_message_from_row(row) for row in rows]

    def find_board_message(self, message_id: int) -> BoardMessage | None:
        row = self._connection.execute("SELECT * FROM board_messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            return None
        return board_message_from_row(row)

    def delete_board_message(self, message_id: int) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM board_messages WHERE id = ?", (message_id,))

    # Games and their reports.

    def record_game_opened(self, name: str, host_account_id: int | None) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO games (name, host_account_id, opened_at) VALUES (?, ?, ?)",
                (name, host_account_id, to_stored_time(self._clock())),
            )
        return cursor.lastrowid

    def record_game_closed(self, game_record_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE games SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                (to_stored_time(self._clock()), game_record_id),
            )

    def store_game_report(self, account_id: int | None, group_id: int, report: bytes) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO game_reports (account_id, group_id, report, received_at) VALUES (?, ?, ?, ?)",
                (account_id, group_id, report, to_stored_time(self._clock())),
            )

    def count_game_reports(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM game_reports").fetchone()[0]

    def list_recent_games(self, limit: int) -> list[GameRecord]:
        """The newest games first, open and closed."""
        rows = self._connection.execute(
            "SELECT games.id, games.name, accounts.name AS host_name, games.opened_at, games.closed_at "
            "FROM games LEFT JOIN accounts ON accounts.id = games.host_account_id "
            "ORDER BY games.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
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
        """The newest match reports first, without their bytes."""
        rows = self._connection.execute(
            "SELECT game_reports.id, accounts.name AS sender_name, game_reports.group_id, "
            "LENGTH(game_reports.report) AS size, game_reports.received_at "
            "FROM game_reports LEFT JOIN accounts ON accounts.id = game_reports.account_id "
            "ORDER BY game_reports.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            GameReportRecord(
                report_id=row["id"],
                sender_name=row["sender_name"] or "",
                group_id=row["group_id"],
                size=row["size"],
                received_at=from_stored_time(row["received_at"]),
            )
            for row in rows
        ]

    def find_game_report(self, report_id: int) -> bytes | None:
        row = self._connection.execute("SELECT report FROM game_reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            return None
        return bytes(row["report"])

    # Announcements from outside the server process.

    def queue_announcement(self, text: str) -> None:
        check_title("announcement", text)
        check_game_text("announcement", text, MAX_ANNOUNCEMENT_LENGTH)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO pending_announcements (text, created_at) VALUES (?, ?)",
                (text, to_stored_time(self._clock())),
            )

    def take_pending_announcements(self) -> list[str]:
        """The announcements not sent yet, oldest first, marked as sent in the same step."""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, text FROM pending_announcements WHERE sent_at IS NULL ORDER BY id"
            ).fetchall()
            sent_at = to_stored_time(self._clock())
            for row in rows:
                connection.execute("UPDATE pending_announcements SET sent_at = ? WHERE id = ?", (sent_at, row["id"]))
        return [row["text"] for row in rows]

    def list_recent_announcements(self, limit: int) -> list[AnnouncementRecord]:
        """The newest announcements first; sent_at is None while the server has not taken one yet."""
        rows = self._connection.execute(
            "SELECT text, created_at, sent_at FROM pending_announcements ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
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
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO live_state (id, snapshot, written_at) VALUES (1, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET snapshot = excluded.snapshot, written_at = excluded.written_at",
                (state.to_json(), to_stored_time(self._clock())),
            )

    def read_live_state(self) -> StoredLiveState | None:
        """The state last written, or None when no server ever wrote one or its text is unreadable."""
        row = self._connection.execute("SELECT snapshot, written_at FROM live_state WHERE id = 1").fetchone()
        if row is None:
            return None
        try:
            state = LiveLobbyState.from_json(row["snapshot"])
        except ValueError:
            return None
        return StoredLiveState(state=state, written_at=from_stored_time(row["written_at"]))

    def clear_live_state(self) -> None:
        """The server stops: nobody is online any more."""
        with self._transaction() as connection:
            connection.execute("DELETE FROM live_state")

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
        query = f"SELECT COUNT(DISTINCT {count_distinct}) FROM {table} WHERE {end_column} IS NULL"
        return self._connection.execute(query).fetchone()[0]

    def _count_open_since(
        self, table: str, start_column: str, end_column: str, since: str, count_distinct: str = "id"
    ) -> int:
        # Table and column names come from this class only, never from outside, so formatting
        # them into the query is safe; the time is passed as a parameter.
        query = (
            f"SELECT COUNT(DISTINCT {count_distinct}) FROM {table} "
            f"WHERE {end_column} IS NULL OR {end_column} >= ?"
        )
        return self._connection.execute(query, (since,)).fetchone()[0]


def account_from_row(row: sqlite3.Row | None) -> Account | None:
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


def board_theme_from_row(row: sqlite3.Row) -> BoardTheme:
    return BoardTheme(theme_id=row["id"], language=row["language"], name=row["name"], description=row["description"])


def board_message_from_row(row: sqlite3.Row) -> BoardMessage:
    return BoardMessage(
        message_id=row["id"],
        theme_id=row["theme_id"],
        title=row["title"],
        body=row["body"],
        author=row["author"],
        posted_at=from_stored_time(row["posted_at"]),
    )
