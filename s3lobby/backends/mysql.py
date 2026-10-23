"""A MySQL or MariaDB database through PyMySQL, the optional "mysql" extra."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from s3lobby.backends.base import (
    BackendError,
    ConstraintViolation,
    DatabaseBackend,
    Row,
    WriteResult,
    placeholders,
)
from s3lobby.backends.location import MysqlLocation

log = logging.getLogger(__name__)

MYSQL_CHARACTER_SET = "utf8mb4"
# A MySQL server drops connections that stay idle too long (wait_timeout, 8 hours by default), and
# a lobby without players may not query for that long. A connection idle for longer than this is
# checked, and opened again if the server dropped it, before the next query.
MYSQL_IDLE_SECONDS_BEFORE_PING = 60.0
# Only one process at a time may migrate the schema; the others wait this long for their turn.
MYSQL_MIGRATION_LOCK_NAME = "s3lobby_schema_migration"
MYSQL_MIGRATION_LOCK_TIMEOUT_SECONDS = 60
MARIADB_SERVER_MARKER = "MariaDB"


class MysqlBackend(DatabaseBackend):
    """A database on a MySQL (8.0.19 or newer) or MariaDB (10.5 or newer) server, through PyMySQL.

    The connection runs in autocommit mode, so every query outside transaction() sees the latest
    committed data. Without it a long-lived connection would keep reading the snapshot of its
    first query, and the server would never see an announcement the web interface queued.
    """

    dialect = "mysql"

    def __init__(self, location: MysqlLocation) -> None:
        """Connect to the database at location. Raises BackendError when PyMySQL is not installed
        or the server refuses the connection. PyMySQL is imported here only, so SQLite needs nothing extra."""
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
                ssl=location.tls_context(),
            )
        except pymysql.err.Error as error:
            raise BackendError(f"cannot connect to {location.describe()}: {error}") from error
        # MariaDB reports a version such as "10.11.6-MariaDB"; MySQL's version names no product.
        # The two differ in the upsert syntax they accept (_upsert_sql).
        self._is_mariadb = MARIADB_SERVER_MARKER in self._connection.get_server_info()
        self._is_in_transaction = False
        self._last_used_at = time.monotonic()

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> WriteResult:
        """Run one statement that returns no rows; see DatabaseBackend.execute."""
        with self._errors_translated(), self._cursor() as cursor:
            cursor.execute(to_pymysql_placeholders(sql), tuple(parameters))
            return WriteResult(last_row_id=cursor.lastrowid, row_count=cursor.rowcount)

    def execute_many(self, sql: str, parameter_rows: Sequence[Sequence[Any]]) -> None:
        """Run one statement once per parameter row; see DatabaseBackend.execute_many."""
        # PyMySQL turns an INSERT run for many rows into multi-row INSERT statements.
        with self._errors_translated(), self._cursor() as cursor:
            cursor.executemany(to_pymysql_placeholders(sql), [tuple(row) for row in parameter_rows])

    def fetch_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[Row]:
        """Run one query and return all its rows as dicts."""
        with self._errors_translated(), self._cursor() as cursor:
            cursor.execute(to_pymysql_placeholders(sql), tuple(parameters))
            return list(cursor.fetchall())

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the block in one transaction; see DatabaseBackend.transaction. A connection idle
        for long is checked first, since a dropped one cannot be reopened in the middle of a transaction."""
        with self._errors_translated():
            self._reconnect_if_idle()
            # begin() suspends autocommit until commit() or rollback().
            self._connection.begin()
        # From here on _cursor must not reconnect: a new connection would not know this transaction.
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
        """Roll back the open transaction without raising."""
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
        """INSERT ... ON DUPLICATE KEY UPDATE, in the form the server (MySQL or MariaDB) prefers."""
        # MySQL deprecates VALUES(column) in favour of a row alias; MariaDB knows only VALUES().
        # Both update on a clash with any unique key, so key_columns is implied by the schema.
        insert = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders(len(columns))})"
        if self._is_mariadb:
            assignments = ", ".join(f"{column} = VALUES({column})" for column in updated_columns)
            return f"{insert} ON DUPLICATE KEY UPDATE {assignments}"
        assignments = ", ".join(f"{column} = incoming.{column}" for column in updated_columns)
        return f"{insert} AS incoming ON DUPLICATE KEY UPDATE {assignments}"

    def table_names(self) -> set[str]:
        """The names of the tables in the connection's database, from information_schema."""
        rows = self.fetch_all(
            "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = DATABASE()"
        )
        return {row["name"] for row in rows}

    @contextmanager
    def migration_lock(self) -> Iterator[None]:
        """Hold a named server lock while the block migrates the schema; raises BackendError when
        another process holds it longer than MYSQL_MIGRATION_LOCK_TIMEOUT_SECONDS."""
        # MySQL commits every schema change on its own, so a transaction cannot hold the lock;
        # a named lock, held by this connection until it is released, does.
        row = self.fetch_one(
            "SELECT GET_LOCK(?, ?) AS is_locked", (MYSQL_MIGRATION_LOCK_NAME, MYSQL_MIGRATION_LOCK_TIMEOUT_SECONDS)
        )
        # GET_LOCK returns 1 when it got the lock, 0 after the timeout, and NULL on an error.
        if row["is_locked"] != 1:
            raise BackendError("another process has been migrating the schema for too long")
        try:
            yield
        finally:
            self.execute("DO RELEASE_LOCK(?)", (MYSQL_MIGRATION_LOCK_NAME,))

    def close(self) -> None:
        """Close the connection; the backend cannot be used afterwards."""
        self._connection.close()

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        """A cursor for one statement. Outside a transaction an idle connection is checked first;
        inside one it is not, as reconnecting would silently lose the transaction."""
        if not self._is_in_transaction:
            self._reconnect_if_idle()
        with self._connection.cursor() as cursor:
            yield cursor
        # Only reached when the statement succeeded; a failed one leaves the idle clock as it was,
        # so the next statement may still ping.
        self._last_used_at = time.monotonic()

    def _reconnect_if_idle(self) -> None:
        """Ping the server after MYSQL_IDLE_SECONDS_BEFORE_PING without a query, and reconnect
        if it dropped the connection meanwhile. Raises BackendError when it cannot."""
        idle_seconds = time.monotonic() - self._last_used_at
        if idle_seconds < MYSQL_IDLE_SECONDS_BEFORE_PING:
            return
        try:
            self._connection.ping(reconnect=True)
        except self._driver.err.Error as error:
            raise BackendError(f"lost the connection to the MySQL server: {error}") from error
        self._last_used_at = time.monotonic()


def to_pymysql_placeholders(sql: str) -> str:
    """PyMySQL takes %s where SQLite takes ?, and reads every other % as a format character. The
    queries of this program have no ? or % inside string literals, so plain replacing is safe."""
    # The order matters: escaping % first keeps the %s placeholders made next from being escaped too.
    return sql.replace("%", "%%").replace("?", "%s")
