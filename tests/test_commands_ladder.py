"""The ladder's chat commands: /rank, /top, /clanrank and /seasons, the ladder as the game shows it."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from s3lobby.commands import MAX_ANSWER_LINE_LENGTH, ChatCommands
from s3lobby.constants import LADDER_COMMAND_BURST, LadderState, LadderType, SeasonCounts
from s3lobby.database import LobbyDatabase
from s3lobby.ladder import CLANLIGA_KEY, LIGA_KEY, Evaluation, ResultPlayer
from s3lobby.reachability import Reachability
from tests.support import join_ladder_since
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
        players = [self.alice, self.bob, self.carol]
        join_ladder_since(self.database, players, LIGA_KEY)
        join_ladder_since(self.database, players, CLANLIGA_KEY)
        for day in (1, 2):
            self.store(LadderType.INDIVIDUAL, day, (self.alice, None, True), (self.bob, None, False))
        self.store(LadderType.CLANS, 3, (self.alice, self.romans, True), (self.carol, self.amazons, False))
        self.time_seconds = 0.0
        self.online_names = ["Alice"]
        self.commands = ChatCommands(
            self.database,
            online_player_names=lambda: self.online_names,
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


    # /ladder, /ladder join and /ladder leave: players take part only in ladders they joined, and
    # the game has no other place to join one.

    def test_ladder_names_the_player_s_ladders(self) -> None:
        """Alice is in the Liga and the Clanliga."""
        self.assertEqual(self.ask("/ladder"), ("Your ladders: Liga, Clanliga. Games count where every player has joined.",))

    def test_leaving_hides_the_player_and_joining_again_starts_over(self) -> None:
        """After /ladder leave Alice has no Liga place; after /ladder join she is back without her
        old results, while they still count for Bob."""
        self.assertIn("Left Liga", self.ask("/ladder leave")[0])
        self.assertEqual(self.ask("/ladder leave"), ("Ladder: not in Liga.",))
        self.assertIn("has no Liga place", self.ask("/rank")[0])
        # Past the limit on ladder questions, which this test would otherwise run into.
        self.time_seconds += 60
        self.assertIn("Joined Liga", self.ask("/ladder join liga")[0])
        self.assertIn("has no Liga place", self.ask("/rank")[0])
        self.assertIn("Bob: Liga", self.ask("/rank Bob")[0])

    def test_a_season_is_joined_by_name_until_it_ends(self) -> None:
        """A custom season is joined by its name; one that is over cannot be joined."""
        self.database.add_ladder_season("Autumn Cup", NOW - timedelta(days=1), None, SeasonCounts.LIGA)
        self.database.add_ladder_season("Summer Cup", NOW - timedelta(days=90), NOW - timedelta(days=30), SeasonCounts.LIGA)
        self.assertIn("Joined Autumn Cup", self.ask("/ladder join autumn cup")[0])
        self.assertIn("Autumn Cup", self.ask("/ladder")[0])
        self.assertEqual(self.ask("/ladder join Summer Cup"), ("Ladder: Summer Cup is over.",))
        self.assertIn("there is no season named", self.ask("/ladder join Winter")[0])


    # The ladder's questions beyond the standings: finding opponents, one's own record, games
    # still waiting, one season, the rules.

    def test_opponents_are_the_liga_members_online_closest_first(self) -> None:
        """/opponents names who to ask for a ladder game; Carol is online but has no rating yet."""
        self.online_names = ["Alice", "Bob", "Carol"]
        [line] = self.ask("/opponents")
        self.assertTrue(line.startswith("Liga members online: "))
        self.assertNotIn("Alice", line)
        self.assertIn("Bob", line)
        self.assertIn("Carol 1000", line)

    def test_results_show_the_own_last_results_with_their_change(self) -> None:
        """/results: Alice's two wins over Bob, newest first, each with its rating change."""
        [line] = self.ask("/results")
        self.assertTrue(line.startswith("Your last results: won vs Bob +"))
        self.assertEqual(line.count("won vs Bob"), 2)

    def test_vs_gives_the_own_record_against_one_player(self) -> None:
        """/vs NAME: won and lost against that player, and the last match."""
        self.assertEqual(self.ask("/vs Bob"), ("You against Bob: won 2, lost 0. Last match 02 Oct 2026: won.",))
        self.assertEqual(self.ask("/vs Carol"), ("You have no counted Liga result against Carol yet.",))

    def test_pending_lists_waiting_and_disputed_games(self) -> None:
        """/pending: a ladder game Alice played that has no result yet."""
        game_id = self.database.record_game_opened("Rematch", self.alice, ladder_type=LadderType.INDIVIDUAL)
        self.database.record_game_player(game_id, self.alice, None, 1)
        self.assertEqual(self.ask("/pending"), ("Rematch: waiting for reports.",))

    def test_season_and_top_clanliga(self) -> None:
        """/season NAME tells a season's members and whether the player joined; /top clanliga
        ranks the clans."""
        self.database.add_ladder_season("Autumn Cup", NOW - timedelta(days=1), None, SeasonCounts.LIGA)
        answer = self.ask("/season autumn cup")
        self.assertIn("0 member(s)", answer[0])
        self.assertEqual(answer[-1], "You have not joined it: /ladder join Autumn Cup.")
        self.assertTrue(self.ask("/top clanliga")[0].startswith("Clanliga: 1 Romans "))

    def test_ladder_rules_explain_joining(self) -> None:
        """/ladder rules: the board's info message as chat lines."""
        answer = self.ask("/ladder rules")
        self.assertIn("/ladder join (Liga), /ladder join clanliga, /ladder join SEASON", answer)


if __name__ == "__main__":
    unittest.main()
