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

base.py holds the interface and its errors, location.py reads locations, and sqlite.py and
mysql.py are the two backends. Everything other modules use is importable from here.
"""

from __future__ import annotations

from s3lobby.backends.base import (
    BackendError,
    ConstraintViolation,
    DatabaseBackend,
    DatabaseError,
    Row,
    WriteResult,
    placeholders,
)
from s3lobby.backends.location import (
    MYSQL_LOCATION_PREFIX,
    MYSQL_PASSWORD_VARIABLE,
    MysqlLocation,
    describe_location,
    parse_mysql_location,
)
from s3lobby.backends.mysql import MysqlBackend
from s3lobby.backends.sqlite import IN_MEMORY_DATABASE, SqliteBackend

__all__ = [
    "IN_MEMORY_DATABASE",
    "MYSQL_PASSWORD_VARIABLE",
    "BackendError",
    "ConstraintViolation",
    "DatabaseBackend",
    "DatabaseError",
    "MysqlBackend",
    "MysqlLocation",
    "Row",
    "SqliteBackend",
    "WriteResult",
    "describe_location",
    "open_backend",
    "parse_mysql_location",
    "placeholders",
]


def open_backend(location: str) -> DatabaseBackend:
    """Connect to the database at location (see the module docstring)."""
    if location.startswith(MYSQL_LOCATION_PREFIX):
        return MysqlBackend(parse_mysql_location(location))
    return SqliteBackend(location)
