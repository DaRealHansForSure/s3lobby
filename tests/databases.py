"""Where the database tests keep their data: SQLite by default, or a MySQL or MariaDB server.

Set S3LOBBY_TEST_DATABASE to a mysql:// location to run the database tests and the session tests
against that server (needs PyMySQL):

    S3LOBBY_TEST_DATABASE=mysql://s3lobby:secret@127.0.0.1:3306/s3lobby_test .venv/bin/python -m unittest

Every table of that database is dropped before each test, so never point it at one that matters.
"""

from __future__ import annotations

import os

from s3lobby.backends import MysqlBackend, open_backend

TEST_DATABASE_VARIABLE = "S3LOBBY_TEST_DATABASE"


def configured_test_database() -> str | None:
    """The mysql:// location to test against, or None to test with SQLite."""
    return os.environ.get(TEST_DATABASE_VARIABLE) or None


def database_location_for_test(sqlite_location: str) -> str:
    """The location a test should open: the configured MySQL database, emptied, or else
    sqlite_location (":memory:" or a file in the test's temporary folder)."""
    location = configured_test_database()
    if location is None:
        return sqlite_location
    drop_all_tables(location)
    return location


def drop_all_tables(location: str) -> None:
    backend = open_backend(location)
    try:
        if not isinstance(backend, MysqlBackend):
            raise ValueError(f"{TEST_DATABASE_VARIABLE} must name a mysql:// database")
        # The tables reference each other; dropping them in any order needs the checks off.
        backend.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in backend.table_names():
            backend.execute(f"DROP TABLE {table}")
        backend.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        backend.close()
