"""The administration commands, run through the same entry point as the s3lobby command."""

from __future__ import annotations

import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from s3lobby.cli import EXIT_FAILURE, EXIT_SUCCESS, configure_logging, main
from s3lobby.database import ACCESS_CODE_LENGTH, LobbyDatabase, Setting


class CommandLineTest(unittest.TestCase):
    """The administration subcommands against a fresh SQLite file per test."""

    def setUp(self) -> None:
        """A temporary folder for the database; the first command creates it."""
        self.folder = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.folder.name) / "lobby.sqlite3")

    def tearDown(self) -> None:
        """Remove the temporary folder with the database."""
        self.folder.cleanup()

    def run_command(self, *arguments: str) -> tuple[int, str, str]:
        """Run the command line with these arguments against the test's database; returns the exit code, the
        standard output and the error output."""
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = main([*arguments, "--database", self.database_path])
        return exit_code, output.getvalue(), errors.getvalue()

    def open_database(self) -> LobbyDatabase:
        """Open the test's database directly, closed again when the test ends."""
        database = LobbyDatabase(self.database_path)
        self.addCleanup(database.close)
        return database

    def test_account_add_prints_a_working_access_code_once(self) -> None:
        """A new account's access code is shown only when it is created, so the printed code must be complete
        and must let the player log on."""
        exit_code, output, _ = self.run_command("account", "add", "Alice", "--label", "home PC")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        code_line = next(line for line in output.splitlines() if line.startswith("Access code"))
        access_code = code_line.rsplit(": ", 1)[1]
        self.assertEqual(len(access_code), ACCESS_CODE_LENGTH)

        database = self.open_database()
        alice = database.find_account("Alice")
        self.assertTrue(database.check_access_code(alice.account_id, access_code))

    def test_invalid_player_name_is_refused_with_the_game_rule(self) -> None:
        """A name the game would not accept is refused when the account is made, with the rule in the message,
        rather than leaving an account nobody can log on with."""
        exit_code, _, errors = self.run_command("account", "add", "Alice2")
        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertIn("only letters A to Z", errors)

    def test_clan_membership_is_managed_by_name(self) -> None:
        """Clans and their members are handled by name on the command line, and the listing shows each clan
        with its members."""
        self.run_command("account", "add", "Alice")
        self.run_command("clan", "add", "Romans")
        exit_code, _, _ = self.run_command("clan", "join", "Romans", "Alice")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        _, listing, _ = self.run_command("clan", "list")
        self.assertIn("Romans: Alice", listing)

    def test_database_status_shows_pending_migrations_without_applying_them(self) -> None:
        """database status only reports; an admin checking before a backup must not find the schema already
        changed."""
        exit_code, output, _ = self.run_command("database", "status")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertIn("pending  0001 initial", output)
        _, output_after, _ = self.run_command("database", "status")
        self.assertIn("pending  0001 initial", output_after, "status alone must not migrate")

    def test_database_copy_moves_the_lobby_to_another_location(self) -> None:
        """database copy carries the data over to a new location and reports how many rows of each kind it
        copied."""
        self.run_command("account", "add", "Alice")
        target_path = str(Path(self.folder.name) / "copy.sqlite3")
        exit_code, output, _ = self.run_command("database", "copy", "--to", target_path)
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertIn("accounts: 1", output)
        target = LobbyDatabase(target_path)
        self.addCleanup(target.close)
        self.assertIsNotNone(target.find_account("Alice"))

    def test_development_mode_refuses_to_serve_the_internet(self) -> None:
        """A password everybody knows must never meet a server that is open to the internet."""
        for option in (["--public-host", "lobby.example.org"], ["--upnp"]):
            with self.subTest(option=option):
                exit_code, _, _ = self.run_command("serve", "--dev", *option)
                self.assertEqual(exit_code, EXIT_FAILURE)

    def test_clan_highlight_is_switched_and_listed(self) -> None:
        """Highlighting a clan is switched on and off from the command line, and the listing says which clans
        the game shows in green."""
        self.run_command("clan", "add", "Romans")
        exit_code, output, _ = self.run_command("clan", "highlight", "Romans")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertTrue(self.open_database().find_clan("Romans").is_highlighted)
        _, listing, _ = self.run_command("clan", "list")
        self.assertIn("Romans (green in the game)", listing)
        self.run_command("clan", "unhighlight", "Romans")
        self.assertFalse(self.open_database().find_clan("Romans").is_highlighted)

    def test_web_development_mode_refuses_a_reverse_proxy(self) -> None:
        """Behind a reverse proxy the web interface is open to the internet."""
        exit_code, _, _ = self.run_command("web", "--dev", "--behind-proxy")
        self.assertEqual(exit_code, EXIT_FAILURE)

    def test_unknown_account_is_an_error_not_a_traceback(self) -> None:
        """A command naming an account that does not exist fails with a plain message instead of a Python
        traceback."""
        exit_code, _, errors = self.run_command("account", "ban", "Nobody")
        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertIn("no account named 'Nobody'", errors)

    def test_news_and_settings_are_stored(self) -> None:
        """News and settings set on the command line land in the database the server reads them from."""
        self.run_command("news", "set", "--audience", "german", "--title", "Willkommen", "--text", "Hallo")
        self.run_command("setting", "set", "max_players", "40")
        database = self.open_database()
        self.assertEqual(database.find_news("german").title, "Willkommen")
        self.assertEqual(database.get_integer_setting(Setting.MAX_PLAYERS), 40)

    def test_unknown_setting_lists_the_known_ones(self) -> None:
        """A mistyped setting fails and the message lists the settings there are."""
        exit_code, _, errors = self.run_command("setting", "set", "colour", "blue")
        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertIn("max_players", errors)

    def test_board_theme_and_message(self) -> None:
        """A board theme is created and a message posted to it from the command line, and the board listing
        shows the message."""
        self.run_command("board", "add-theme", "English", "Tournaments")
        theme_id = self.open_database().list_board_themes()[0].theme_id
        exit_code, _, _ = self.run_command("board", "post", str(theme_id), "--title", "Cup", "--text", "Saturday")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        _, listing, _ = self.run_command("board", "list")
        self.assertIn("Cup", listing)

    def test_board_order_and_hiding_work_without_the_web(self) -> None:
        """Every admin action of the web interface exists as a command too: moving and hiding board
        themes and messages."""
        self.run_command("board", "add-theme", "English", "Tournaments")
        self.run_command("board", "add-theme", "English", "Trading")
        database = self.open_database()
        tournaments, trading = database.list_board_themes()
        self.assertEqual(self.run_command("board", "move-theme", str(trading.theme_id), "up")[0], EXIT_SUCCESS)
        self.run_command("board", "hide-theme", str(tournaments.theme_id))
        database = self.open_database()
        themes = database.list_board_themes()
        self.assertEqual([theme.name for theme in themes], ["Trading", "Tournaments"])
        self.assertTrue(themes[1].is_hidden)
        _, listing, _ = self.run_command("board", "list")
        self.assertIn("Tournaments (hidden)", listing)
        self.assertNotEqual(self.run_command("board", "move-message", "999", "down")[0], EXIT_SUCCESS)


class LoggingLevelTest(unittest.TestCase):
    """The console stays at INFO while a log file takes the datagrams of --verbose."""

    def setUp(self) -> None:
        """Keep the root logger's handlers and level to restore them after the test, and give the test a log
        file in a temporary folder."""
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level

        def restore() -> None:
            """Close the handlers the test added and put the root logger back as it was."""
            for handler in root.handlers:
                handler.close()
            root.handlers = saved_handlers
            root.setLevel(saved_level)

        self.addCleanup(restore)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.log_file = str(Path(self.folder.name) / "lobby.log")

    def levels(self) -> tuple[int | None, int | None]:
        """The console's level and the file's (None without a file)."""
        console_level = None
        file_level = None
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.FileHandler):
                file_level = handler.level
            else:
                console_level = handler.level
        return console_level, file_level

    def test_verbose_with_a_file_keeps_the_console_at_info(self) -> None:
        """With --verbose and a log file, the datagrams go into the file only, so the console stays readable.
        """
        configure_logging(is_verbose=True, log_file=self.log_file)
        self.assertEqual(self.levels(), (logging.INFO, logging.DEBUG))

    def test_verbose_without_a_file_shows_the_detail_on_the_console(self) -> None:
        """With --verbose and no log file, the console is the only place the datagrams can go."""
        configure_logging(is_verbose=True, log_file=None)
        self.assertEqual(self.levels(), (logging.DEBUG, None))

    def test_without_verbose_everything_is_info(self) -> None:
        """Without --verbose neither the console nor the file gets the datagram detail."""
        configure_logging(is_verbose=False, log_file=self.log_file)
        self.assertEqual(self.levels(), (logging.INFO, logging.INFO))


if __name__ == "__main__":
    unittest.main()
