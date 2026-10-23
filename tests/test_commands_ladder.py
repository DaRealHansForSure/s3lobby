"""The ladder's chat commands: /rank, /top, /clanrank and /seasons, the ladder as the game shows it."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from s3lobby.commands import MAX_ANSWER_LINE_LENGTH, ChatCommands
from s3lobby.constants import LADDER_COMMAND_BURST, LadderState, LadderType, SeasonCounts
from s3lobby.database import LobbyDatabase
from s3lobby.ladder import Evaluation, ResultPlayer
from s3lobby.reachability import Reachability
from tests.test_commands import Sender

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


class LadderCommandsTest(unittest.TestCase):
    """Alice beat Bob twice in Liga; the Romans beat the Amazons once in Clanliga."""

    def setUp(self) -> None:
        """A database with the results, on a clock at NOW, and commands that hand out answers."""
        self.database = LobbyDatabase(":memory:", clock=lambda: NOW)
        self.alice = self.database.create_account("Alice").account_id
        self.bob = self.database.create_account("Bob").account_id
        self.carol = self.database.create_account("Carol").account_id
        self.romans = self.database.create_clan("Romans").clan_id
        self.amazons = self.database.create_clan("Amazons").clan_id
        self.database.set_clan_of_account(self.alice, self.romans)
        for day in (1, 2):
            self.store(LadderType.INDIVIDUAL, day, (self.alice, None, True), (self.bob, None, False))
        self.store(LadderType.CLANS, 3, (self.alice, self.romans, True), (self.carol, self.amazons, False))
        self.time_seconds = 0.0
        self.commands = ChatCommands(
            self.database,
            online_player_names=lambda: ["Alice"],
            announce=lambda text: 0,
            kick=lambda account_id, reason: False,
            reachability_of=lambda address: Reachability.NOT_CHECKED,
            clock_seconds=lambda: self.time_seconds,
        )
        self.sender = Sender(self.alice, "Alice", ("127.0.0.1", 3343))

    def tearDown(self) -> None:
        """Close the database."""
        self.database.close()

    def store(self, ladder_type: int, day: int, winner: tuple, loser: tuple) -> None:
        """A counted result on a day of October: winner and loser as (account, clan, has_won)."""
        game_id = self.database.record_game_opened("game", winner[0], ladder_type=ladder_type)
        players = tuple(
            ResultPlayer(account_id, clan_id, team, 0, team, has_won, False)
            for team, (account_id, clan_id, has_won) in enumerate((winner, loser))
        )
        played_at = datetime(2026, 10, day, 20, 0, tzinfo=timezone.utc)
        self.database.store_ladder_result(
            Evaluation(game_id, ladder_type, LadderState.COUNTED, winning_team=0, played_at=played_at, players=players)
        )

    def ask(self, command: str) -> tuple[str, ...]:
        """Alice's answer to a command."""
        return self.commands.answer(self.sender, command)

    def test_rank_shows_rating_place_and_record(self) -> None:
        """/rank without a name is the sender's own line."""
        answer = self.ask("/rank")
        self.assertIn("Alice: Liga", answer[0])
        self.assertIn("place 1 of 2", answer[0])
        self.assertIn("Won 2, lost 0, left 0.", answer)
        self.assertIn("Clanliga: won 1, lost 0 for the clan.", answer)

    def test_rank_of_another_player(self) -> None:
        """/rank NAME looks up anyone, in any case."""
        self.assertIn("place 2 of 2", self.ask("/rank bob")[0])
        self.assertEqual(self.ask("/rank Nobody"), ("There is no account named Nobody.",))

    def test_top_names_the_best(self) -> None:
        """/top: places, names and ratings in one line the chat shows whole."""
        [line] = self.ask("/top")
        self.assertTrue(line.startswith("Liga: 1 Alice "))
        self.assertLessEqual(len(line), MAX_ANSWER_LINE_LENGTH)

    def test_top_of_a_custom_season(self) -> None:
        """/top SEASON counts only that season's dates."""
        self.database.add_ladder_season("Late", NOW - timedelta(days=1), None, SeasonCounts.LIGA)
        self.assertEqual(self.ask("/top late"), ("Late: nobody has a place yet.",))
        self.assertIn("There is no season named Nope", self.ask("/top Nope")[0])

    def test_clanrank_shows_clanliga_and_strength(self) -> None:
        """/clanrank without a name is the sender's clan; strength needs three established members."""
        answer = self.ask("/clanrank")
        self.assertIn("Romans: Clanliga", answer[0])
        self.assertIn("No strength from Liga yet", answer[-1])

    def test_seasons_lists_the_running_ones(self) -> None:
        """/seasons says when each running season ends."""
        self.assertEqual(self.ask("/seasons"), ("No custom season runs now; the infinite season always does.",))
        self.database.add_ladder_season(
            "Autumn Cup", NOW - timedelta(days=5), datetime(2026, 12, 1, tzinfo=timezone.utc), SeasonCounts.LIGA
        )
        self.assertEqual(self.ask("/seasons"), ("Running: Autumn Cup (liga), until 30 Nov 2026.",))

    def test_ladder_questions_are_limited_per_player(self) -> None:
        """A client asking in a loop gets a refusal instead of keeping the server reading results."""
        for _ in range(LADDER_COMMAND_BURST):
            self.ask("/top")
        self.assertEqual(self.ask("/top"), ("Too many ladder questions; ask again in a few seconds.",))
        self.time_seconds += 10
        self.assertTrue(self.ask("/top")[0].startswith("Liga:"))


if __name__ == "__main__":
    unittest.main()
