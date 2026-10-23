"""A SQLite file, with nothing beyond the standard library."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from s3lobby.backends.base import (
    BackendError,
    ConstraintViolation,
    DatabaseBackend,
    Row,
    WriteResult,
    placeholders,
)

log = logging.getLogger(__name__)

IN_MEMORY_DATABASE = ":memory:"
# The file holds access code hashes and player data: readable and writable by its owner only.
DATABASE_FILE_MODE = 0o600
# How long a SQLite write waits for another process's lock before it fails.
SQLITE_BUSY_TIMEOUT_SECONDS = 2.0


class SqliteBackend(DatabaseBackend):
    """A SQLite file, or ":memory:" for a database that lives only as long as the backend."""

    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        """Open or create the file at path, or a database in memory for ":memory:". A new file
        is created readable by its owner only. Raises BackendError when SQLite cannot open it."""
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
        # SQLite ignores the schema's foreign keys unless each connection turns them on, so without
        # this a deleted account would leave its access codes, sessions and clan membership behind.
        self._connection.execute("PRAGMA foreign_keys = ON")
        if not is_in_memory:
            # Write-ahead logging lets the web interface read while the server writes, instead of
            # one waiting for the other. The setting is stored in the file.
            self._connection.execute("PRAGMA journal_mode = WAL")

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> WriteResult:
        """Run one statement that returns no rows; see DatabaseBackend.execute."""
        with sqlite_errors_translated():
            cursor = self._connection.execute(sql, parameters)
        return WriteResult(last_row_id=cursor.lastrowid, row_count=cursor.rowcount)

    def execute_many(self, sql: str, parameter_rows: Sequence[Sequence[Any]]) -> None:
        """Run one statement once per parameter row; see DatabaseBackend.execute_many."""
        with sqlite_errors_translated():
            self._connection.executemany(sql, parameter_rows)

    def fetch_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[Row]:
        """Run one query and return all its rows as dicts."""
        with sqlite_errors_translated():
            return self._connection.execute(sql, parameters).fetchall()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the block in one transaction that holds the write lock from its start; see
        DatabaseBackend.transaction."""
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
        """Roll back the open transaction, if there is one, without raising."""
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
        """INSERT ... ON CONFLICT (key) DO UPDATE, SQLite's insert-or-update."""
        assignments = ", ".join(f"{column} = excluded.{column}" for column in updated_columns)
        return (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders(len(columns))}) "
            f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET {assignments}"
        )

    def table_names(self) -> set[str]:
        """The names of the tables in the file, from sqlite_master."""
        rows = self.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row["name"] for row in rows}

    @contextmanager
    def migration_lock(self) -> Iterator[None]:
        """Hold the write lock while the block migrates the schema; see DatabaseBackend.migration_lock."""
        # SQLite changes its schema inside transactions, so one transaction around the whole
        # migration both locks out other processes and makes it all or nothing.
        with self.transaction():
            yield

    def legacy_schema_version(self) -> int:
        """The version in PRAGMA user_version, where s3lobby kept the schema version before the
        schema_migrations table."""
        return self.fetch_one("PRAGMA user_version")["user_version"]

    def note_schema_version(self, version: int) -> None:
        """Store the version in PRAGMA user_version as well."""
        # PRAGMA takes no ? placeholders, hence the formatted value; int() keeps it a plain number.
        # Kept up to date so that an s3lobby from before the migrations table, which only reads
        # user_version, refuses a database newer than it knows instead of breaking it.
        self._connection.execute(f"PRAGMA user_version = {int(version)}")

    def close(self) -> None:
        """Close the connection; the backend cannot be used afterwards."""
        self._connection.close()


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
