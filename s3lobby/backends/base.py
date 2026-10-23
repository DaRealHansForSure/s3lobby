"""What every backend offers: the interface LobbyDatabase talks to, its errors and results."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

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
        # The key columns identify the row, so only the others are overwritten when it exists. At
        # least one column must be left over: an upsert of key columns only would build an empty SET.
        updated_columns = [column for column in columns if column not in key_columns]
        if not updated_columns:
            raise ValueError(f"upsert into {table} needs a column besides the key columns {list(key_columns)}")
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


def placeholders(count: int) -> str:
    """count comma-separated ? placeholders, for an INSERT or an IN list."""
    return ", ".join("?" for _ in range(count))
