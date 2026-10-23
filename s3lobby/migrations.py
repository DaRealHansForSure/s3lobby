"""Schema migrations: numbered SQL files, applied in order, each exactly once.

Every dialect has a folder in s3lobby/schema (sqlite, mysql) with files named NNNN_description.sql.
The numbers run from 0001 without gaps and mean the same change in every folder, so databases of
different dialects at the same version hold the same tables and can be copied into each other.

A database records what it has applied in the table schema_migrations: version, name, a SHA-256
checksum of the file and when. Opening a database applies whatever is missing. It refuses to open
when a file was changed after it was applied, or when the database is newer than this s3lobby:
both would leave the code and the tables disagreeing about the schema.

Rules for a new migration: add the next number to every dialect folder; never edit or renumber a
file that is released; keep each statement free of semicolons, which separate the statements, in
string literals and comments; write full-line comments starting with "--". SQLite runs the whole
migration in one transaction. MySQL commits every schema statement on its own, so a migration that
fails there halfway must be finished or undone by hand before the next start.

Why this module and not a library: Alembic builds on SQLAlchemy, and yoyo-migrations, the closest
fit (plain SQL files over DB-API, SQLite and MySQL), brings dependencies of its own. The lobby
server keeps to the standard library, and the parts it needs (ordered files, a ledger, checksums, a
lock) fit in this one file.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

from s3lobby.backends import DatabaseBackend

SCHEMA_PACKAGE = "s3lobby.schema"
MIGRATION_FILE_PATTERN = re.compile(r"(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql")
MIGRATIONS_TABLE = "schema_migrations"
# Written in the SQL both dialects understand, since it must exist before any migration runs.
CREATE_MIGRATIONS_TABLE = f"""
    CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
        version INTEGER PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        checksum VARCHAR(64) NOT NULL,
        applied_at VARCHAR(32) NOT NULL
    )
"""
STATEMENT_SEPARATOR = ";"
COMMENT_PREFIX = "--"


class MigrationError(Exception):
    """The database's schema and the migrations of this s3lobby do not fit together."""


@dataclass(frozen=True)
class Migration:
    """One migration file: its number, the name after the number, and its SQL text."""

    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        """The SHA-256 of the file in hex, as recorded in schema_migrations when it was applied."""
        # Line endings are normalised so that a checkout with Windows line endings (Git's autocrlf)
        # does not count as a changed file.
        normalised_sql = self.sql.replace("\r\n", "\n")
        return hashlib.sha256(normalised_sql.encode("utf-8")).hexdigest()

    @property
    def statements(self) -> list[str]:
        """The statements of the file, without the full-line comments, split at the semicolons."""
        lines = [line for line in self.sql.splitlines() if not line.strip().startswith(COMMENT_PREFIX)]
        statements = "\n".join(lines).split(STATEMENT_SEPARATOR)
        return [statement.strip() for statement in statements if statement.strip()]


@dataclass(frozen=True)
class AppliedMigration:
    """A row of schema_migrations: a migration the database has applied, and when."""

    version: int
    name: str
    checksum: str
    applied_at: str


@dataclass(frozen=True)
class MigrationStatus:
    """What read_status reports: the migrations a database has applied, and those still to apply."""

    applied: list[AppliedMigration]
    pending: list[Migration]


@cache
def load_migrations(dialect: str) -> tuple[Migration, ...]:
    """The migrations of a dialect in order. Read once per process: they ship with the code."""
    folder = resources.files(SCHEMA_PACKAGE).joinpath(dialect)
    if not folder.is_dir():
        raise MigrationError(f"there are no migrations for the {dialect} dialect")
    migrations = []
    for entry in folder.iterdir():
        match = MIGRATION_FILE_PATTERN.fullmatch(entry.name)
        if match is None:
            continue
        migration = Migration(int(match["version"]), match["name"], entry.read_text(encoding="utf-8"))
        migrations.append(migration)
    migrations.sort(key=lambda migration: migration.version)
    for expected_version, migration in enumerate(migrations, start=1):
        if migration.version != expected_version:
            raise MigrationError(f"{dialect} migration {expected_version} is missing or doubled")
    return tuple(migrations)


def latest_version(dialect: str) -> int:
    """The number of the newest migration this s3lobby has for the dialect."""
    return len(load_migrations(dialect))


def migrate(backend: DatabaseBackend, applied_at: str) -> list[Migration]:
    """Apply the migrations the database is missing and return them; applied_at is the stored time
    to record. Raises MigrationError when the database does not fit this s3lobby's migrations."""
    migrations = load_migrations(backend.dialect)
    # The common case, a database that is up to date, needs no lock: the web interface opens the
    # database on every request.
    if MIGRATIONS_TABLE in backend.table_names():
        if not find_pending(backend, migrations):
            return []
    with backend.migration_lock():
        adopt_legacy_schema(backend, migrations, applied_at)
        # Read again under the lock: another process may have migrated while this one waited.
        pending = find_pending(backend, migrations)
        for migration in pending:
            apply_migration(backend, migration, applied_at)
    return pending


def read_status(backend: DatabaseBackend) -> MigrationStatus:
    """What is applied and what is still to apply, without changing anything."""
    migrations = load_migrations(backend.dialect)
    if MIGRATIONS_TABLE not in backend.table_names():
        legacy_version = backend.legacy_schema_version()
        return MigrationStatus(applied=[], pending=list(migrations[legacy_version:]))
    return MigrationStatus(applied=read_applied(backend), pending=find_pending(backend, migrations))


def current_version(backend: DatabaseBackend) -> int:
    """The number of the newest migration the database has applied; for a database without the
    schema_migrations table, the version it stored the old way (0 for a new one)."""
    if MIGRATIONS_TABLE not in backend.table_names():
        return backend.legacy_schema_version()
    row = backend.fetch_one(f"SELECT MAX(version) AS version FROM {MIGRATIONS_TABLE}")
    return row["version"] or 0


def adopt_legacy_schema(backend: DatabaseBackend, migrations: tuple[Migration, ...], applied_at: str) -> None:
    """Create the migrations table. A SQLite database from before it kept its version in
    PRAGMA user_version, whose upgrades are this folder's first migrations: record those as applied."""
    if MIGRATIONS_TABLE in backend.table_names():
        return
    backend.execute(CREATE_MIGRATIONS_TABLE)
    legacy_version = backend.legacy_schema_version()
    if legacy_version > len(migrations):
        raise MigrationError(
            f"the database has schema version {legacy_version}, this s3lobby knows up to {len(migrations)}"
        )
    for migration in migrations[:legacy_version]:
        record_migration(backend, migration, applied_at)


def find_pending(backend: DatabaseBackend, migrations: tuple[Migration, ...]) -> list[Migration]:
    """The migrations the database has not applied yet. Raises MigrationError when what it has
    applied does not match the files: a newer s3lobby migrated it, a number was skipped, or a file was
    changed after it was applied."""
    applied = read_applied(backend)
    if len(applied) > len(migrations):
        newest = applied[-1]
        raise MigrationError(
            f"the database has migration {newest.version} ({newest.name}), this s3lobby knows up to "
            f"{len(migrations)}: it was migrated by a newer s3lobby"
        )
    for record, migration in zip(applied, migrations):
        if record.version != migration.version:
            raise MigrationError(f"the database skipped migration {migration.version} ({migration.name})")
        if record.checksum != migration.checksum:
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) was changed after it was applied; "
                "restore the file and add the change as a new migration"
            )
    return list(migrations[len(applied):])


def read_applied(backend: DatabaseBackend) -> list[AppliedMigration]:
    """Every row of schema_migrations, in order of version."""
    rows = backend.fetch_all(f"SELECT version, name, checksum, applied_at FROM {MIGRATIONS_TABLE} ORDER BY version")
    return [AppliedMigration(row["version"], row["name"], row["checksum"], row["applied_at"]) for row in rows]


def apply_migration(backend: DatabaseBackend, migration: Migration, applied_at: str) -> None:
    """Run every statement of the migration and record it as applied."""
    for statement in migration.statements:
        backend.execute(statement)
    record_migration(backend, migration, applied_at)


def record_migration(backend: DatabaseBackend, migration: Migration, applied_at: str) -> None:
    """Record the migration as applied in schema_migrations, and tell the backend the new version
    for a dialect that also keeps it elsewhere."""
    backend.execute(
        f"INSERT INTO {MIGRATIONS_TABLE} (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
        (migration.version, migration.name, migration.checksum, applied_at),
    )
    backend.note_schema_version(migration.version)
