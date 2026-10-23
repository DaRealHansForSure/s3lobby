"""The database servers the lobby can keep its data in, behind one small interface.

LobbyDatabase (database.py) writes each query once, with ? placeholders, in the part of SQL that
SQLite and MySQL share. A backend runs those queries on its server and hides what differs:
connecting, the placeholder style, transactions, rows (always dicts keyed by column name), upserts,
and the error a unique or foreign key raises.

Where the data lives is one string, the database location:

    s3lobby.sqlite3                         a SQLite file (any text that is not a URL below)
    mysql://user:password@host:3306/name    a MySQL or MariaDB database

The SQLite backend needs nothing beyond the standard library. The MySQL backend needs PyMySQL, the
optional "mysql" extra, and imports it only when a mysql:// location is opened.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

IN_MEMORY_DATABASE = ":memory:"
# The file holds access code hashes and player data: readable and writable by its owner only.
DATABASE_FILE_MODE = 0o600

# How long a SQLite write waits for another process's lock before it fails.
SQLITE_BUSY_TIMEOUT_SECONDS = 2.0

MYSQL_SCHEME = "mysql"
MYSQL_LOCATION_PREFIX = f"{MYSQL_SCHEME}://"
DEFAULT_MYSQL_PORT = 3306
# The password may stay out of the location, which shows up in process lists and shell history.
MYSQL_PASSWORD_VARIABLE = "S3LOBBY_DATABASE_PASSWORD"
MYSQL_CHARACTER_SET = "utf8mb4"
# A MySQL server drops connections that stay idle too long (wait_timeout, 8 hours by default), and
# a lobby without players may not query for that long. A connection idle for longer than this is
# checked, and opened again if the server dropped it, before the next query.
MYSQL_IDLE_SECONDS_BEFORE_PING = 60.0
# Only one process at a time may migrate the schema; the others wait this long for their turn.
MYSQL_MIGRATION_LOCK_NAME = "s3lobby_schema_migration"
MYSQL_MIGRATION_LOCK_TIMEOUT_SECONDS = 60
MARIADB_SERVER_MARKER = "MariaDB"

Row = dict[str, Any]


class DatabaseError(Exception):
    """A request the stored data does not allow, such as adding an account name twice, or a
    database that failed. Callers catch this one type; the database's own errors never leak out."""


class BackendError(DatabaseError):
    """The database cannot be reached, opened or used: a bad location, a missing driver, a refused
    logon, a lock that did not clear in time."""


class ConstraintViolation(DatabaseError):
    """A write that a unique key or a foreign key of the schema refused."""


@dataclass(frozen=True)
class WriteResult:
    """What a write statement did: the id an INSERT created, and how many rows it touched."""

    last_row_id: int | None
    row_count: int


@dataclass(frozen=True)
class MysqlLocation:
    host: str
    port: int
    user: str
    password: str
    database_name: str

    def describe(self) -> str:
        """The location without its password, for logs and error messages."""
        return f"{MYSQL_LOCATION_PREFIX}{self.user}@{self.host}:{self.port}/{self.database_name}"


class DatabaseBackend(ABC):
    """One open connection to one database. Not thread-safe: one instance per thread."""

    # The name of the SQL dialect, which also names the folder of its migrations (s3lobby/schema).
    dialect: str

    @abstractmethod
    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> WriteResult:
        """Run one statement that returns no rows. Raises ConstraintViolation when a key refuses it."""

    @abstractmethod
    def execute_many(self, sql: str, parameter_rows: Sequence[Sequence[Any]]) -> None:
        """Run one statement once per parameter row, in as few round trips as the driver allows."""

    @abstractmethod
    def fetch_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[Row]:
        """Run one query and return all its rows."""

    def fetch_one(self, sql: str, parameters: Sequence[Any] = ()) -> Row | None:
        """Run one query and return its first row, or None when it found none."""
        rows = self.fetch_all(sql, parameters)
        if not rows:
            return None
        return rows[0]

    @abstractmethod
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit the statements run inside the block when it succeeds, roll them back when it
        raises. Every block is a write: it takes the write lock at its start."""

    def upsert(self, table: str, key_columns: Sequence[str], values: dict[str, Any]) -> None:
        """Insert a row, or update the row that has the same key. values holds every column to
        write, the key columns included. Table and column names come from this program only."""
        columns = list(values)
        updated_columns = [column for column in columns if column not in key_columns]
        sql = self._upsert_sql(table, key_columns, columns, updated_columns)
        self.execute(sql, [values[column] for column in columns])

    @abstractmethod
    def _upsert_sql(
        self, table: str, key_columns: Sequence[str], columns: Sequence[str], updated_columns: Sequence[str]
    ) -> str:
        """The dialect's insert-or-update statement with one placeholder per column."""

    @abstractmethod
    def table_names(self) -> set[str]:
        """The names of the tables in the database."""

    @abstractmethod
    @contextmanager
    def migration_lock(self) -> Iterator[None]:
        """Keep every other process from migrating the schema while the block runs."""

    def legacy_schema_version(self) -> int:
        """The schema version a database stored before it had the schema_migrations table; 0 when
        the dialect never had another way to store it."""
        return 0

    def note_schema_version(self, version: int) -> None:
        """Called after each migration, for a dialect that also keeps the version elsewhere."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection; the backend cannot be used afterwards."""


def open_backend(location: str) -> DatabaseBackend:
    """Connect to the database at location (see the module docstring)."""
    if location.startswith(MYSQL_LOCATION_PREFIX):
        return MysqlBackend(parse_mysql_location(location))
    return SqliteBackend(location)


def describe_location(location: str) -> str:
    """The location as it may be shown: a MySQL password is left out."""
    if not location.startswith(MYSQL_LOCATION_PREFIX):
        return location
    try:
        return parse_mysql_location(location).describe()
    except BackendError:
        return MYSQL_LOCATION_PREFIX


def parse_mysql_location(location: str) -> MysqlLocation:
    """Read mysql://user:password@host:port/name. The password may be left out of the location
    and given in S3LOBBY_DATABASE_PASSWORD instead; special characters in it are percent-encoded."""
    parts = urlsplit(location)
    try:
        port = parts.port or DEFAULT_MYSQL_PORT
    except ValueError as error:
        raise BackendError(f"the port in the MySQL location is not a number: {error}") from error
    database_name = unquote(parts.path.lstrip("/"))
    if not parts.hostname or not parts.username or not database_name:
        raise BackendError("a MySQL location needs a user, a host and a database: mysql://user@host/name")
    password = parts.password
    if password is None:
        password = os.environ.get(MYSQL_PASSWORD_VARIABLE, "")
    return MysqlLocation(
        host=parts.hostname,
        port=port,
        user=unquote(parts.username),
        password=unquote(password),
        database_name=database_name,
    )


class SqliteBackend(DatabaseBackend):
    """A SQLite file, or ":memory:" for a database that lives only as long as the backend."""

    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        is_in_memory = path == IN_MEMORY_DATABASE
        if not is_in_memory and not Path(path).exists():
            # Create the empty file with its final mode first; SQLite would create it world-readable.
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, DATABASE_FILE_MODE))
        try:
            # isolation_level None: no implicit transactions; transaction() opens them explicitly.
            # The timeout bounds how long a write waits for another process's lock; the server's
            # event loop waits with it, so it is kept short.
            self._connection = sqlite3.connect(path, isolation_level=None, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        except sqlite3.Error as error:
            raise BackendError(str(error)) from error
        self._connection.row_factory = row_as_dict
        self._connection.execute("PRAGMA foreign_keys = ON")
        if not is_in_memory:
            # Write-ahead logging lets the web interface read while the server writes, instead of
            # one waiting for the other. The setting is stored in the file.
            self._connection.execute("PRAGMA journal_mode = WAL")

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> WriteResult:
        with sqlite_errors_translated():
            cursor = self._connection.execute(sql, parameters)
        return WriteResult(last_row_id=cursor.lastrowid, row_count=cursor.rowcount)

    def execute_many(self, sql: str, parameter_rows: Sequence[Sequence[Any]]) -> None:
        with sqlite_errors_translated():
            self._connection.executemany(sql, parameter_rows)

    def fetch_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[Row]:
        with sqlite_errors_translated():
            return self._connection.execute(sql, parameters).fetchall()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # IMMEDIATE takes the write lock at once. A deferred transaction that reads first would
        # have to upgrade its lock at the first write, and fails if another process wrote meanwhile.
        with sqlite_errors_translated():
            self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            with sqlite_errors_translated():
                self._connection.execute("COMMIT")
        except BaseException:
            # Also after a failed COMMIT: left open, the transaction would make every later BEGIN fail.
            self._roll_back()
            raise

    def _roll_back(self) -> None:
        if not self._connection.in_transaction:
            return
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error as error:
            # The error that led here matters more; this one is only logged.
            log.warning("ROLLBACK failed: %s", error)

    def _upsert_sql(
        self, table: str, key_columns: Sequence[str], columns: Sequence[str], updated_columns: Sequence[str]
    ) -> str:
        assignments = ", ".join(f"{column} = excluded.{column}" for column in updated_columns)
        return (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders(len(columns))}) "
            f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET {assignments}"
        )

    def table_names(self) -> set[str]:
        rows = self.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row["name"] for row in rows}

    @contextmanager
    def migration_lock(self) -> Iterator[None]:
        # SQLite changes its schema inside transactions, so one transaction around the whole
        # migration both locks out other processes and makes it all or nothing.
        with self.transaction():
            yield

    def legacy_schema_version(self) -> int:
        return self.fetch_one("PRAGMA user_version")["user_version"]

    def note_schema_version(self, version: int) -> None:
        # Kept up to date so that an s3lobby from before the migrations table, which only reads
        # user_version, refuses a database newer than it knows instead of breaking it.
        self._connection.execute(f"PRAGMA user_version = {int(version)}")

    def close(self) -> None:
        self._connection.close()


class MysqlBackend(DatabaseBackend):
    """A database on a MySQL (8.0.19 or newer) or MariaDB (10.5 or newer) server, through PyMySQL.

    The connection runs in autocommit mode, so every query outside transaction() sees the latest
    committed data. Without it a long-lived connection would keep reading the snapshot of its
    first query, and the server would never see an announcement the web interface queued.
    """

    dialect = "mysql"

    def __init__(self, location: MysqlLocation) -> None:
        try:
            import pymysql
            import pymysql.constants.CLIENT
            import pymysql.cursors
        except ImportError as error:
            raise BackendError("MySQL needs PyMySQL; install it with: pip install .[mysql]") from error
        self._driver = pymysql
        try:
            self._connection = pymysql.connect(
                host=location.host,
                port=location.port,
                user=location.user,
                password=location.password,
                database=location.database_name,
                charset=MYSQL_CHARACTER_SET,
                autocommit=True,
                # Report the rows an UPDATE matched, as SQLite does, not only those it changed.
                client_flag=pymysql.constants.CLIENT.FOUND_ROWS,
                cursorclass=pymysql.cursors.DictCursor,
            )
        except pymysql.err.Error as error:
            raise BackendError(f"cannot connect to {location.describe()}: {error}") from error
        self._is_mariadb = MARIADB_SERVER_MARKER in self._connection.get_server_info()
        self._is_in_transaction = False
        self._last_used_at = time.monotonic()

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> WriteResult:
        with self._errors_translated(), self._cursor() as cursor:
            cursor.execute(to_pymysql_placeholders(sql), tuple(parameters))
            return WriteResult(last_row_id=cursor.lastrowid, row_count=cursor.rowcount)

    def execute_many(self, sql: str, parameter_rows: Sequence[Sequence[Any]]) -> None:
        # PyMySQL turns an INSERT run for many rows into multi-row INSERT statements.
        with self._errors_translated(), self._cursor() as cursor:
            cursor.executemany(to_pymysql_placeholders(sql), [tuple(row) for row in parameter_rows])

    def fetch_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[Row]:
        with self._errors_translated(), self._cursor() as cursor:
            cursor.execute(to_pymysql_placeholders(sql), tuple(parameters))
            return list(cursor.fetchall())

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._errors_translated():
            self._reconnect_if_idle()
            self._connection.begin()
        self._is_in_transaction = True
        try:
            yield
            with self._errors_translated():
                self._connection.commit()
        except BaseException:
            self._roll_back()
            raise
        finally:
            self._is_in_transaction = False

    def _roll_back(self) -> None:
        try:
            self._connection.rollback()
        except self._driver.err.Error as error:
            # The error that led here matters more; this one is only logged.
            log.warning("ROLLBACK failed: %s", error)

    @contextmanager
    def _errors_translated(self) -> Iterator[None]:
        """PyMySQL's errors as the backend's, like sqlite_errors_translated."""
        try:
            yield
        except self._driver.err.IntegrityError as error:
            raise ConstraintViolation(str(error)) from error
        except self._driver.err.Error as error:
            raise BackendError(f"MySQL: {error}") from error

    def _upsert_sql(
        self, table: str, key_columns: Sequence[str], columns: Sequence[str], updated_columns: Sequence[str]
    ) -> str:
        # MySQL deprecates VALUES(column) in favour of a row alias; MariaDB knows only VALUES().
        # Both update on a clash with any unique key, so key_columns is implied by the schema.
        insert = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders(len(columns))})"
        if self._is_mariadb:
            assignments = ", ".join(f"{column} = VALUES({column})" for column in updated_columns)
            return f"{insert} ON DUPLICATE KEY UPDATE {assignments}"
        assignments = ", ".join(f"{column} = incoming.{column}" for column in updated_columns)
        return f"{insert} AS incoming ON DUPLICATE KEY UPDATE {assignments}"

    def table_names(self) -> set[str]:
        rows = self.fetch_all(
            "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = DATABASE()"
        )
        return {row["name"] for row in rows}

    @contextmanager
    def migration_lock(self) -> Iterator[None]:
        # MySQL commits every schema change on its own, so a transaction cannot hold the lock;
        # a named lock, held by this connection until it is released, does.
        row = self.fetch_one(
            "SELECT GET_LOCK(?, ?) AS is_locked", (MYSQL_MIGRATION_LOCK_NAME, MYSQL_MIGRATION_LOCK_TIMEOUT_SECONDS)
        )
        if row["is_locked"] != 1:
            raise BackendError("another process has been migrating the schema for too long")
        try:
            yield
        finally:
            self.execute("DO RELEASE_LOCK(?)", (MYSQL_MIGRATION_LOCK_NAME,))

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        if not self._is_in_transaction:
            self._reconnect_if_idle()
        with self._connection.cursor() as cursor:
            yield cursor
        self._last_used_at = time.monotonic()

    def _reconnect_if_idle(self) -> None:
        idle_seconds = time.monotonic() - self._last_used_at
        if idle_seconds < MYSQL_IDLE_SECONDS_BEFORE_PING:
            return
        try:
            self._connection.ping(reconnect=True)
        except self._driver.err.Error as error:
            raise BackendError(f"lost the connection to the MySQL server: {error}") from error
        self._last_used_at = time.monotonic()


@contextmanager
def sqlite_errors_translated() -> Iterator[None]:
    """SQLite's errors as the backend's: a refused key as ConstraintViolation, anything else (a
    lock that did not clear in time, a full disk) as BackendError."""
    try:
        yield
    except sqlite3.IntegrityError as error:
        raise ConstraintViolation(str(error)) from error
    except sqlite3.Error as error:
        raise BackendError(f"SQLite: {error}") from error


def row_as_dict(cursor: sqlite3.Cursor, values: tuple[Any, ...]) -> Row:
    """SQLite row factory: rows as dicts, the form PyMySQL's DictCursor returns as well."""
    column_names = [column[0] for column in cursor.description]
    return dict(zip(column_names, values))


def placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def to_pymysql_placeholders(sql: str) -> str:
    """PyMySQL takes %s where SQLite takes ?, and reads every other % as a format character. The
    queries of this program have no ? or % inside string literals, so plain replacing is safe."""
    return sql.replace("%", "%%").replace("?", "%s")
