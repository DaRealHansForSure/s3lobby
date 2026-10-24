"""The `s3lobby ladder` commands: the ladder without the web interface, for admins and other tools."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from s3lobby.cli import EXIT_FAILURE, EXIT_SUCCESS, main
from s3lobby.constants import LADDER_JSON_FORMAT, LadderState, LadderType
from s3lobby.database import LobbyDatabase
from s3lobby.ladder import SEASON_LADDER, Evaluation, LadderKey, ResultPlayer
from tests.support import join_ladder_since

PLAYED_AT = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)


class LadderCommandLineTest(unittest.TestCase):
    """Standings and results as text, JSON and CSV, decisions and custom seasons, against a fresh
    SQLite file per test."""

    def setUp(self) -> None:
        """A database in which Alice beat Bob in a counted Liga game."""
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.database_path = str(Path(self.folder.name) / "lobby.sqlite3")
        database = LobbyDatabase(self.database_path)
        try:
            alice = database.create_account("Alice").account_id
            bob = database.create_account("Bob").account_id
            join_ladder_since(database, [alice, bob])
            self.game_id = database.record_game_opened("Revenge", alice, ladder_type=LadderType.INDIVIDUAL)
            database.store_ladder_result(
                Evaluation(
                    self.game_id,
                    LadderType.INDIVIDUAL,
                    LadderState.COUNTED,
                    winning_team=0,
                    game_ticks=30_000,
                    played_at=PLAYED_AT,
                    players=(
                        ResultPlayer(alice, None, 0, 0, 0, True, False),
                        ResultPlayer(bob, None, 1, 1, 1, False, False),
                    ),
                )
            )
        finally:
            database.close()

    def run_command(self, *arguments: str) -> tuple[int, str, str]:
        """Run the command line against the test's database; returns the exit code, the standard
        output and the error output."""
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = main([*arguments, "--database", self.database_path])
        return exit_code, output.getvalue(), errors.getvalue()

    def test_standings_as_json_carry_the_format_number(self) -> None:
        """A tool reads the standings without scraping a page; the format number guards it."""
        exit_code, output, _ = self.run_command("ladder", "standings", "--format", "json")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        document = json.loads(output)
        self.assertEqual(document["format"], LADDER_JSON_FORMAT)
        self.assertEqual([row["name"] for row in document["standings"]], ["Alice", "Bob"])
        self.assertEqual(document["standings"][0]["rating"], 1016)

    def test_results_as_csv(self) -> None:
        """One header line, one line per result."""
        _, output, _ = self.run_command("ladder", "results", "--format", "csv")
        lines = list(csv.reader(io.StringIO(output)))
        self.assertEqual(lines[0][:4], ["game_id", "played_at", "ladder", "state"])
        self.assertEqual(lines[1][2:6], ["liga", "counted", "Alice", "Bob"])

    def test_an_admin_voids_and_decides_from_the_command_line(self) -> None:
        """What the web offers works here too: void, then decide for Bob."""
        exit_code, _, _ = self.run_command("ladder", "void", str(self.game_id), "--by", "Alice")
        self.assertEqual(exit_code, EXIT_SUCCESS)
        _, output, _ = self.run_command("ladder", "standings")
        self.assertIn("(none)", output)
        self.run_command("ladder", "decide", str(self.game_id), "--winner", "1")
        _, output, _ = self.run_command("ladder", "show", str(self.game_id))
        self.assertIn("winners: Bob", output)

    def test_a_custom_season_counts_only_its_dates(self) -> None:
        """A season after the match is empty; one around it has Alice on top."""
        self.run_command("ladder", "season", "add", "Later", "--start", "2026-11-01")
        _, output, _ = self.run_command("ladder", "standings", "--season", "later")
        self.assertIn("(none)", output)
        exit_code, output, _ = self.run_command(
            "ladder", "season", "add", "Autumn", "--start", "2026-10-01", "--end", "2026-10-31", "--counts", "liga"
        )
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertIn("from 2026-10-01 to 2026-10-31", output)
        # Only the season's members are ranked in it; they joined before the match.
        database = LobbyDatabase(self.database_path)
        try:
            autumn = database.find_ladder_season("Autumn")
            players = [database.find_account(name).account_id for name in ("Alice", "Bob")]
            join_ladder_since(database, players, LadderKey(SEASON_LADDER, autumn.season_id))
        finally:
            database.close()
        _, output, _ = self.run_command("ladder", "standings", "--season", "Autumn", "--format", "json")
        self.assertEqual(json.loads(output)["standings"][0]["name"], "Alice")

    def test_an_unknown_season_or_game_is_a_message(self) -> None:
        """A typo gets an error naming what exists, not a traceback."""
        exit_code, _, errors = self.run_command("ladder", "standings", "--season", "Nope")
        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertIn("there is no season named 'Nope'", errors)
        exit_code, _, errors = self.run_command("ladder", "decide", "999", "--winner", "0")
        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertIn("has no ladder result", errors)


    def test_players_join_and_leave_ladders_from_the_command_line(self) -> None:
        """ladder join, leave and members do what /ladder does in the game, for admins."""
        self.assertEqual(self.run_command("ladder", "leave", "Alice")[0], EXIT_SUCCESS)
        self.assertEqual(self.run_command("ladder", "leave", "Alice")[0], EXIT_FAILURE)
        self.assertEqual(self.run_command("ladder", "join", "Alice", "--ladder", "clanliga")[0], EXIT_SUCCESS)
        _, output, _ = self.run_command("ladder", "members", "--format", "json")
        members = {(row["ladder"], row["name"]) for row in json.loads(output)["members"]}
        self.assertEqual(members, {("Clanliga", "Alice"), ("Liga", "Bob")})


if __name__ == "__main__":
    unittest.main()
