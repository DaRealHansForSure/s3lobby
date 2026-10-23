"""Schema migrations and database locations: what lets the lobby move between SQLite and MySQL and
keep its data across schema changes."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from s3lobby.backends import (
    MYSQL_PASSWORD_VARIABLE,
    BackendError,
    describe_location,
    parse_mysql_location,
)
from s3lobby.database import TABLES_IN_DEPENDENCY_ORDER, DatabaseError, LobbyDatabase, Setting
from s3lobby.migrations import MIGRATIONS_TABLE, Migration, latest_version, load_migrations

DIALECTS = ("sqlite", "mysql")
# Before migration 0004 the settings column was called key.
LEGACY_SETTINGS_VERSION = 3


class MigrationFilesTest(unittest.TestCase):
    """The migration files in s3lobby/schema and how they are read."""

    def test_every_dialect_has_the_same_numbered_migrations(self) -> None:
        """Copying between dialects relies on the same version meaning the same tables."""
        names_by_dialect = {}
        for dialect in DIALECTS:
            names_by_dialect[dialect] = [(migration.version, migration.name) for migration in load_migrations(dialect)]
        self.assertEqual(names_by_dialect["sqlite"], names_by_dialect["mysql"])

    def test_statements_are_split_at_semicolons_and_comment_lines_dropped(self) -> None:
        """A migration file runs as one statement per semicolon, and a semicolon inside a comment line does
        not split anything."""
        sql = "-- a note; with a semicolon\nCREATE TABLE a (x INTEGER);\n\nCREATE TABLE b (y INTEGER);\n"
        migration = Migration(1, "example", sql)
        self.assertEqual(migration.statements, ["CREATE TABLE a (x INTEGER)", "CREATE TABLE b (y INTEGER)"])

    def test_checksum_ignores_windows_line_endings(self) -> None:
        """A checkout with Windows line endings must not count as an edited migration and stop the server."""
        unix = Migration(1, "example", "CREATE TABLE a (x INTEGER);\n")
        windows = Migration(1, "example", "CREATE TABLE a (x INTEGER);\r\n")
        self.assertEqual(unix.checksum, windows.checksum)


class SqliteMigrationTest(unittest.TestCase):
    """Migrating SQLite files: new ones, old ones from before the migrations table, and ones that do not fit
    this s3lobby."""

    def setUp(self) -> None:
        """A temporary folder with the path of the test's database file."""
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "lobby.sqlite3"

    def open_database(self, **options: bool) -> LobbyDatabase:
        """Open the test's file as a LobbyDatabase, closed again when the test ends."""
        database = LobbyDatabase(self.path, **options)
        self.addCleanup(database.close)
        return database

    def raw_connection(self) -> sqlite3.Connection:
        """A plain sqlite3 connection to the test's file, for setting up and inspecting what LobbyDatabase
        does not show."""
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        return connection

    def test_new_database_gets_every_migration_and_records_it(self) -> None:
        """A new file gets every migration, each recorded in the migrations table, and user_version set to the
        newest, so an older s3lobby that only reads user_version refuses it instead of breaking it."""
        database = self.open_database()
        self.assertEqual(database.schema_version, latest_version("sqlite"))
        self.assertEqual(len(database.applied_migrations), latest_version("sqlite"))
        connection = self.raw_connection()
        recorded = connection.execute(f"SELECT COUNT(*) FROM {MIGRATIONS_TABLE}").fetchone()[0]
        self.assertEqual(recorded, latest_version("sqlite"))
        # An s3lobby from before the migrations table reads user_version and must see the newest.
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], latest_version("sqlite"))

    def test_reopening_applies_nothing(self) -> None:
        """A database that is up to date is opened without running any migration again."""
        self.open_database().close()
        self.assertEqual(self.open_database().applied_migrations, [])

    def test_database_from_before_the_migrations_table_is_adopted_and_keeps_its_settings(self) -> None:
        """Lobbies that ran an s3lobby with PRAGMA user_version only must upgrade in place."""
        connection = self.raw_connection()
        for migration in load_migrations("sqlite")[:LEGACY_SETTINGS_VERSION]:
            connection.executescript(migration.sql)
        connection.execute("INSERT INTO settings (key, value) VALUES ('welcome_message', 'Hallo')")
        connection.execute(f"PRAGMA user_version = {LEGACY_SETTINGS_VERSION}")
        connection.commit()
        connection.close()

        database = self.open_database()
        applied_versions = [migration.version for migration in database.applied_migrations]
        self.assertEqual(applied_versions, list(range(LEGACY_SETTINGS_VERSION + 1, latest_version("sqlite") + 1)))
        self.assertEqual(database.get_setting(Setting.WELCOME_MESSAGE), "Hallo")

    def test_migration_changed_after_it_was_applied_stops_the_start(self) -> None:
        """Released migration files are never edited; one whose checksum no longer matches the applied one
        stops the start, since the schema may then differ from what the code expects."""
        self.open_database().close()
        connection = self.raw_connection()
        connection.execute(f"UPDATE {MIGRATIONS_TABLE} SET checksum = 'edited' WHERE version = 1")
        connection.commit()
        with self.assertRaisesRegex(DatabaseError, "changed after it was applied"):
            LobbyDatabase(self.path)

    def test_database_of_a_newer_s3lobby_is_refused(self) -> None:
        """A database that a newer s3lobby migrated further is refused, since this version does not know its
        tables."""
        self.open_database().close()
        connection = self.raw_connection()
        connection.execute(
            f"INSERT INTO {MIGRATIONS_TABLE} (version, name, checksum, applied_at) VALUES (?, 'future', '', '')",
            (latest_version("sqlite") + 1,),
        )
        connection.commit()
        with self.assertRaisesRegex(DatabaseError, "newer s3lobby"):
            LobbyDatabase(self.path)

    def test_legacy_version_beyond_the_known_migrations_is_refused(self) -> None:
        """A file from before the migrations table whose user_version is beyond the known migrations is
        refused rather than adopted."""
        connection = self.raw_connection()
        connection.execute(f"PRAGMA user_version = {latest_version('sqlite') + 1}")
        connection.commit()
        with self.assertRaisesRegex(DatabaseError, "knows up to"):
            LobbyDatabase(self.path)

    def test_status_without_migrating_shows_the_pending_migrations(self) -> None:
        """Opened without applying migrations, the database lists them all as pending and changes nothing."""
        database = self.open_database(applies_migrations=False)
        status = database.read_migration_status()
        self.assertEqual(status.applied, [])
        self.assertEqual(len(status.pending), latest_version("sqlite"))
        self.assertEqual(database.schema_version, 0, "looking changed nothing")


class CopyTest(unittest.TestCase):
    """Copying a whole lobby from one database to another, as database copy does."""

    def setUp(self) -> None:
        """An empty source and an empty target database in memory."""
        self.source = LobbyDatabase(":memory:")
        self.target = LobbyDatabase(":memory:")
        self.addCleanup(self.source.close)
        self.addCleanup(self.target.close)

    def test_copy_keeps_every_row_and_its_id(self) -> None:
        """A copy carries every table with its row ids, gaps included, since the rows refer to each other by
        id; new rows in the target then continue after the copied ids."""
        alice = self.source.create_account("Alice")
        self.source.delete_account(self.source.create_account("Bob").account_id)
        carol = self.source.create_account("Carol")
        code_id = self.source.add_access_code(carol.account_id, "SAMECODE2345")
        clan = self.source.create_clan("Romans")
        self.source.set_clan_of_account(carol.account_id, clan.clan_id)
        self.source.store_game_report(alice.account_id, 7, b"\x00\x01report")
        self.source.set_setting(Setting.WELCOME_MESSAGE, "Hallo")

        copied_rows = self.source.copy_to(self.target)

        self.assertEqual(set(copied_rows), set(TABLES_IN_DEPENDENCY_ORDER))
        self.assertEqual(copied_rows["accounts"], 2)
        self.assertEqual(self.target.find_account("Carol").account_id, carol.account_id, "ids are kept")
        self.assertEqual(self.target.match_access_code(carol.account_id, "SAMECODE2345"), code_id)
        self.assertEqual(self.target.find_clan_of_account(carol.account_id), clan)
        self.assertEqual(self.target.find_game_report(1), b"\x00\x01report")
        self.assertEqual(self.target.get_setting(Setting.WELCOME_MESSAGE), "Hallo")
        newest = self.target.create_account("Dave")
        self.assertGreater(newest.account_id, carol.account_id, "new ids continue after the copied ones")

    def test_copy_into_a_database_with_data_is_refused(self) -> None:
        """A copy into a database that already holds data is refused, so it can neither mix two lobbies nor
        overwrite one."""
        self.source.create_account("Alice")
        self.target.create_account("Bob")
        with self.assertRaisesRegex(DatabaseError, "not empty"):
            self.source.copy_to(self.target)


class LocationTest(unittest.TestCase):
    """Parsing and describing database locations."""

    def test_mysql_location_with_password_and_port(self) -> None:
        """A mysql:// location gives host, port, user, password and database, with the password URL decoded.
        """
        location = parse_mysql_location("mysql://lobby:p%40ss@db.example:3307/settlers")
        self.assertEqual(
            (location.host, location.port, location.user, location.password, location.database_name),
            ("db.example", 3307, "lobby", "p@ss", "settlers"),
        )

    def test_password_may_come_from_the_environment(self) -> None:
        """The password may come from S3LOBBY_DATABASE_PASSWORD instead of the location, which shows up in
        process lists and shell history; the port defaults to 3306."""
        with mock.patch.dict(os.environ, {MYSQL_PASSWORD_VARIABLE: "from environment"}):
            location = parse_mysql_location("mysql://lobby@db.example/settlers")
        self.assertEqual((location.port, location.password), (3306, "from environment"))

    def test_incomplete_mysql_location_is_refused(self) -> None:
        """A location without a user, without a database or with a port that is not a number is refused with
        BackendError."""
        incomplete_locations = ("mysql://db.example/settlers", "mysql://lobby@db.example", "mysql://lobby@db.example:port/x")
        for incomplete in incomplete_locations:
            with self.subTest(location=incomplete), self.assertRaises(BackendError):
                parse_mysql_location(incomplete)

    def test_tls_options_are_read_and_checked(self) -> None:
        """tls=verify checks the server's host name, tls=verify-ca takes its own authority file and needs one,
        no tls means no TLS context, and an unknown tls value is refused. The described location keeps the
        TLS option."""
        verified = parse_mysql_location("mysql://lobby@db.example/settlers?tls=verify")
        self.assertTrue(verified.tls_context().check_hostname)
        own_authority = parse_mysql_location("mysql://lobby@db.example/settlers?tls=verify-ca&ca=/srv/ca.pem")
        self.assertEqual((own_authority.tls, own_authority.ca_file), ("verify-ca", "/srv/ca.pem"))
        self.assertIsNone(parse_mysql_location("mysql://lobby@db.example/settlers").tls_context())
        for refused in ("mysql://lobby@db.example/settlers?tls=yes", "mysql://lobby@db.example/settlers?tls=verify-ca"):
            with self.subTest(location=refused), self.assertRaises(BackendError):
                parse_mysql_location(refused)
        self.assertEqual(describe_location("mysql://lobby:secret@db.example/settlers?tls=verify"),
                         "mysql://lobby@db.example:3306/settlers?tls=verify")

    def test_described_location_never_shows_the_password(self) -> None:
        """The location as shown in logs and output never contains the password; a SQLite path is shown as it
        is."""
        described = describe_location("mysql://lobby:secret@db.example/settlers")
        self.assertNotIn("secret", described)
        self.assertEqual(described, "mysql://lobby@db.example:3306/settlers")
        self.assertEqual(describe_location("lobby.sqlite3"), "lobby.sqlite3")


if __name__ == "__main__":
    unittest.main()
