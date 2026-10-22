"""Everything the lobby keeps between restarts, in one SQLite file.

- accounts and their access codes: a player logs on with the account name as player name and an
  access code as password. Codes are random strings the server generates (today with the command
  line, later through a web interface); only their SHA-256 hash is stored.
- clans and who belongs to them
- the news on the lobby's Home tab, and the message board of the MessageZone tab
- the chat themes listed on the ChatZone tab
- lobby sessions and games, for the statistics table on the Home tab
- the game reports clients send after a match
- settings, such as the player limit or the welcome message

Accounts are only ever created on the server side (the command line today, a web interface later).
The game's own "create player" button is refused at logon.

All times are stored as UTC text in ISO 8601 format, which SQLite compares correctly as text.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

# Bump together with a new entry in SCHEMA_UPGRADES whenever the schema changes.
SCHEMA_VERSION = 1

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
}

# The game's own rule for player names (refusal text 0x9009e): letters A to Z in either case, and
# single spaces between words.
PLAYER_NAME_PATTERN = re.compile(r"[A-Za-z]+( [A-Za-z]+)*")

# Access codes: long enough to be safe from guessing, short enough to type into the game's password
# field. The alphabet leaves out characters that are easy to mix up (0/O, 1/I/L).
ACCESS_CODE_LENGTH = 12
ACCESS_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class Setting(Enum):
    """Settings stored in the database, with the value used when none is stored."""

    # 0 means no limit; above it, logons are refused with the game's "server full" message.
    MAX_PLAYERS = "0"
    # The original refused a second logon with the same serial number as an illegal copy. Off by
    # default: on a LAN, one household's copies often share a serial.
    UNIQUE_SERIAL_NUMBERS = "false"
    # Shown in the chat as [Server] right after a player enters the lobby; empty for none.
    WELCOME_MESSAGE = ""
    # The advert banner on the Home tab: the game downloads banner_url by anonymous FTP whenever the
    # two ids differ from the ones it saw last. Empty url for no banner.
    BANNER_URL = ""
    BANNER_ID = "0"
    BANNER_SUB_ID = "0"

    @property
    def key(self) -> str:
        return self.name.lower()


TRUE_SETTING_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class Account:
    account_id: int
    name: str
    is_admin: bool
    banned_until: datetime | None
    ban_reason: str

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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_stored_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_stored_time(text: str | None) -> datetime | None:
    if text is None:
        return None
    return datetime.fromisoformat(text)


def hash_access_code(access_code: str) -> str:
    # Access codes are random and long, so one round of SHA-256 is enough: there is nothing to
    # guess from a leaked hash, unlike with passwords people choose themselves.
    return hashlib.sha256(access_code.encode("utf-8")).hexdigest()


def generate_access_code() -> str:
    return "".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(ACCESS_CODE_LENGTH))


class LobbyDatabase:
    """The lobby's SQLite file. One instance per process; all calls come from the event loop's
    thread, and each call is short, so they run synchronously."""

    def __init__(self, path: str | Path, clock: Callable[[], datetime] = utc_now) -> None:
        """path may be ":memory:" for a database that lives only as long as this object."""
        self._clock = clock
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
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
            return setting.value
        return row["value"]

    def get_integer_setting(self, setting: Setting) -> int:
        try:
            return int(self.get_setting(setting))
        except ValueError as error:
            raise DatabaseError(f"setting {setting.key} is not a number") from error

    def get_boolean_setting(self, setting: Setting) -> bool:
        return self.get_setting(setting).strip().lower() in TRUE_SETTING_VALUES

    def set_setting(self, setting: Setting, value: str) -> None:
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

    def check_access_code(self, account_id: int, access_code: str) -> bool:
        """True when the code matches one of the account's active codes; marks that code as used."""
        offered_hash = hash_access_code(access_code)
        rows = self._connection.execute(
            "SELECT id, code_hash FROM access_codes WHERE account_id = ? AND revoked_at IS NULL",
            (account_id,),
        ).fetchall()
        for row in rows:
            if hmac.compare_digest(row["code_hash"], offered_hash):
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
