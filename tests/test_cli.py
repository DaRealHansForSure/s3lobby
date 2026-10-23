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
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.folder.name) / "lobby.sqlite3")

    def tearDown(self) -> None:
        self.folder.cleanup()

    def run_command(self, *arguments: str) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = main([*arguments, "--database", self.database_path])
        return exit_code, output.getvalue(), errors.getvalue()

    def open_database(self) -> LobbyDatabase:
        database = LobbyDatabase(self.database_path)
        self.addCleanup(database.close)
        return database

    def test_account_add_prints_a_working_access_code_once(self) -> None:
        exit_code, output, _ = self.run_command("account", "add", "Alice", "--label", "home PC")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        code_line = next(line for line in output.splitlines() if line.startswith("Access code"))
        access_code = code_line.rsplit(": ", 1)[1]
        self.assertEqual(len(access_code), ACCESS_CODE_LENGTH)

        database = self.open_database()
        alice = database.find_account("Alice")
        self.assertTrue(database.check_access_code(alice.account_id, access_code))

    def test_invalid_player_name_is_refused_with_the_game_rule(self) -> None:
        exit_code, _, errors = self.run_command("account", "add", "Alice2")
        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertIn("only letters A to Z", errors)

    def test_clan_membership_is_managed_by_name(self) -> None:
        self.run_command("account", "add", "Alice")
        self.run_command("clan", "add", "Romans")
        exit_code, _, _ = self.run_command("clan", "join", "Romans", "Alice")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        _, listing, _ = self.run_command("clan", "list")
        self.assertIn("Romans: Alice", listing)

    def test_database_status_shows_pending_migrations_without_applying_them(self) -> None:
        exit_code, output, _ = self.run_command("database", "status")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertIn("pending  0001 initial", output)
        _, output_after, _ = self.run_command("database", "status")
        self.assertIn("pending  0001 initial", output_after, "status alone must not migrate")

    def test_database_copy_moves_the_lobby_to_another_location(self) -> None:
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
        exit_code, _, errors = self.run_command("account", "ban", "Nobody")
        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertIn("no account named 'Nobody'", errors)

    def test_news_and_settings_are_stored(self) -> None:
        self.run_command("news", "set", "--audience", "german", "--title", "Willkommen", "--text", "Hallo")
        self.run_command("setting", "set", "max_players", "40")
        database = self.open_database()
        self.assertEqual(database.find_news("german").title, "Willkommen")
        self.assertEqual(database.get_integer_setting(Setting.MAX_PLAYERS), 40)

    def test_unknown_setting_lists_the_known_ones(self) -> None:
        exit_code, _, errors = self.run_command("setting", "set", "colour", "blue")
        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertIn("max_players", errors)

    def test_board_theme_and_message(self) -> None:
        self.run_command("board", "add-theme", "English", "Tournaments")
        theme_id = self.open_database().list_board_themes()[0].theme_id
        exit_code, _, _ = self.run_command("board", "post", str(theme_id), "--title", "Cup", "--text", "Saturday")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        _, listing, _ = self.run_command("board", "list")
        self.assertIn("Cup", listing)



class LoggingLevelTest(unittest.TestCase):
    """The console stays at INFO while a log file takes the datagrams of --verbose."""

    def setUp(self) -> None:
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level

        def restore() -> None:
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
        configure_logging(is_verbose=True, log_file=self.log_file)
        self.assertEqual(self.levels(), (logging.INFO, logging.DEBUG))

    def test_verbose_without_a_file_shows_the_detail_on_the_console(self) -> None:
        configure_logging(is_verbose=True, log_file=None)
        self.assertEqual(self.levels(), (logging.DEBUG, None))

    def test_without_verbose_everything_is_info(self) -> None:
        configure_logging(is_verbose=False, log_file=self.log_file)
        self.assertEqual(self.levels(), (logging.INFO, logging.INFO))


if __name__ == "__main__":
    unittest.main()
