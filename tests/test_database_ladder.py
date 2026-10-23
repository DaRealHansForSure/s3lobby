"""The ladder's part of the database: results, custom seasons, events, and what a game's players
had when they joined it.

Runs on SQLite, or on MySQL when S3LOBBY_TEST_DATABASE names one (tests/databases.py).
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from s3lobby.constants import LadderState, LadderType, SeasonCounts
from s3lobby.database import DELETED_ACCOUNT_ID, DatabaseError, LobbyDatabase, Setting
from s3lobby.ladder import Evaluation, ResultPlayer
from tests.databases import database_location_for_test
from tests.test_database import START, SteppingClock


class LadderDatabaseTest(unittest.TestCase):
    """LobbyDatabase's ladder methods on a fresh database per test."""

    def setUp(self) -> None:
        """A fresh database with two accounts in a Liga game."""
        self.clock = SteppingClock()
        self.database = LobbyDatabase(database_location_for_test(":memory:"), clock=self.clock)
        self.alice = self.database.create_account("Alice").account_id
        self.bob = self.database.create_account("Bob").account_id
        self.game_id = self.database.record_game_opened("Liga", self.alice, ladder_type=LadderType.INDIVIDUAL)

    def tearDown(self) -> None:
        """Close the database."""
        self.database.close()

    def evaluation(self, state: LadderState = LadderState.COUNTED, reason: str = "") -> Evaluation:
        """Alice beat Bob in the test's game."""
        return Evaluation(
            self.game_id,
            LadderType.INDIVIDUAL,
            state,
            reason,
            winning_team=0,
            game_ticks=30000,
            played_at=START,
            players=(
                ResultPlayer(self.alice, None, 0, 0, 0, True, False),
                ResultPlayer(self.bob, None, 1, 1, 1, False, True),
            ),
        )

    def test_a_result_reads_back_with_its_players(self) -> None:
        """What is stored is what the ratings are computed from."""
        self.database.store_ladder_result(self.evaluation())
        [record] = self.database.list_ladder_results()
        self.assertEqual(record.evaluation, self.evaluation())
        self.assertEqual(record.game_name, "Liga")
        self.assertEqual(record.decided_by_name, "")

    def test_a_decision_replaces_the_server_s_result(self) -> None:
        """An admin decides a disputed game: one result per game, naming the admin."""
        self.database.store_ladder_result(self.evaluation(LadderState.DISPUTED, "the reports name different winners"))
        self.database.store_ladder_result(self.evaluation(), decided_by_account_id=self.alice)
        [record] = self.database.list_ladder_results()
        self.assertEqual((record.evaluation.state, record.decided_by_name), (LadderState.COUNTED, "Alice"))

    def test_a_waiting_game_is_no_result(self) -> None:
        """WAITING only exists while a game is evaluated; it is never stored."""
        with self.assertRaises(ValueError):
            self.database.store_ladder_result(self.evaluation(LadderState.WAITING))

    def test_unresolved_ladder_games_are_those_without_a_result(self) -> None:
        """Normal games never show up; a ladder game leaves the list once it has a result."""
        self.database.record_game_opened("Normal", self.alice)
        self.assertEqual([game.game_id for game in self.database.list_unresolved_ladder_games()], [self.game_id])
        self.database.store_ladder_result(self.evaluation())
        self.assertEqual(self.database.list_unresolved_ladder_games(), [])

    def test_a_host_switching_to_a_normal_game_leaves_the_ladder(self) -> None:
        """The ladder type follows the game's latest setup."""
        self.database.record_game_announcement(self.game_id, b"setup", ladder_type=None)
        self.assertEqual(self.database.list_unresolved_ladder_games(), [])

    def test_a_deleted_account_keeps_its_results_for_the_others(self) -> None:
        """Bob's account goes; Alice's win over him stays, Bob shows as account 0."""
        self.database.store_ladder_result(self.evaluation())
        self.database.delete_account(self.bob)
        [record] = self.database.list_ladder_results()
        self.assertEqual([player.account_id for player in record.evaluation.players], [self.alice, DELETED_ACCOUNT_ID])

    def test_a_game_player_keeps_the_clan_and_since_when(self) -> None:
        """The clan a player showed and the date they joined it are kept with the game."""
        clan = self.database.create_clan("Romans")
        self.database.set_clan_of_account(self.alice, clan.clan_id)
        self.clock.advance(days=5)
        self.database.record_game_player(self.game_id, self.alice, clan_id=clan.clan_id, installation=1)
        [player] = self.database.list_game_players(self.game_id)
        self.assertEqual((player.clan_id, player.clan_joined_at, player.installation), (clan.clan_id, START, 1))

    def test_putting_a_member_into_its_own_clan_keeps_the_date(self) -> None:
        """An admin saving the clan again must not restart Clanliga's membership days."""
        clan = self.database.create_clan("Romans")
        self.database.set_clan_of_account(self.alice, clan.clan_id)
        self.clock.advance(days=5)
        self.database.set_clan_of_account(self.alice, clan.clan_id)
        self.database.record_game_player(self.game_id, self.alice, clan_id=clan.clan_id)
        [player] = self.database.list_game_players(self.game_id)
        self.assertEqual(player.clan_joined_at, START)

    def test_seasons_are_created_listed_changed_and_deleted(self) -> None:
        """A custom season round trip, found by name in any case."""
        season = self.database.add_ladder_season("Autumn Cup", START, START + timedelta(days=30), SeasonCounts.LIGA)
        self.assertEqual(self.database.find_ladder_season("autumn cup"), season)
        self.database.update_ladder_season(season.season_id, "Autumn Cup", START, None, SeasonCounts.BOTH)
        self.assertIsNone(self.database.find_ladder_season("Autumn Cup").ends_at)
        self.database.delete_ladder_season(season.season_id)
        self.assertEqual(self.database.list_ladder_seasons(), [])

    def test_a_season_needs_a_showable_name_and_ordered_dates(self) -> None:
        """Season names go into chat answers and board texts; an end before the start is no season."""
        bad_seasons = (
            ("", START, None),
            ("Cup\x01", START, None),
            ("Cup " + "x" * 64, START, None),
            ("Backwards", START, START - timedelta(days=1)),
        )
        for name, starts_at, ends_at in bad_seasons:
            with self.subTest(name=name):
                with self.assertRaises(DatabaseError):
                    self.database.add_ladder_season(name, starts_at, ends_at, SeasonCounts.BOTH)

    def test_season_names_are_unique_and_their_number_bounded(self) -> None:
        """No two seasons share a name, and an admin cannot create them without limit."""
        self.database.add_ladder_season("Cup", START, None, SeasonCounts.BOTH)
        with self.assertRaises(DatabaseError):
            self.database.add_ladder_season("cup", START, None, SeasonCounts.BOTH)
        self.database.set_setting(Setting.LADDER_MAX_CUSTOM_SEASONS, "1")
        with self.assertRaises(DatabaseError):
            self.database.add_ladder_season("Other", START, None, SeasonCounts.BOTH)

    def test_an_event_is_recorded_once(self) -> None:
        """The second time the server asks, the text was written already."""
        self.assertTrue(self.database.record_ladder_event("season-end:1"))
        self.assertFalse(self.database.record_ladder_event("season-end:1"))

    def test_ladder_settings_are_bounded(self) -> None:
        """A huge number of days would not fit a date; the ceiling is far below it."""
        with self.assertRaises(DatabaseError):
            self.database.set_setting(Setting.LADDER_ACTIVE_DAYS, "4000000000")
        self.database.set_setting(Setting.LADDER_MINIMUM_MINUTES, "15")
        self.assertEqual(self.database.get_ladder_settings().minimum_minutes, 15)


if __name__ == "__main__":
    unittest.main()
