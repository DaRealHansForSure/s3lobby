"""The SQLite database: accounts and access codes, clans, news, themes, board, statistics."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from s3lobby.database import (
    ACCESS_CODE_ALPHABET,
    ACCESS_CODE_LENGTH,
    SCHEMA_VERSION,
    DatabaseError,
    LobbyDatabase,
    Setting,
    is_valid_player_name,
)

START = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


class SteppingClock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **duration: float) -> None:
        self.now += timedelta(**duration)


class LobbyDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = SteppingClock()
        self.database = LobbyDatabase(":memory:", clock=self.clock)

    def tearDown(self) -> None:
        self.database.close()

    def test_player_names_follow_the_game_rule(self) -> None:
        self.assertTrue(is_valid_player_name("Player"))
        self.assertTrue(is_valid_player_name("Roman Legion"))
        for invalid_name in ("", " Player", "Player ", "Two  Spaces", "Player1", "Spieler_X", "Müller"):
            with self.subTest(name=invalid_name):
                self.assertFalse(is_valid_player_name(invalid_name))
        with self.assertRaises(DatabaseError):
            self.database.create_account("Player1")

    def test_account_names_are_unique_regardless_of_case(self) -> None:
        self.database.create_account("Alice")
        with self.assertRaises(DatabaseError):
            self.database.create_account("ALICE")
        self.assertEqual(self.database.find_account("alice").name, "Alice")

    def test_issued_access_code_is_checked_by_hash_and_can_be_revoked(self) -> None:
        account = self.database.create_account("Alice")
        access_code = self.database.issue_access_code(account.account_id, label="home PC")
        self.assertEqual(len(access_code), ACCESS_CODE_LENGTH)
        self.assertTrue(set(access_code) <= set(ACCESS_CODE_ALPHABET))

        self.assertTrue(self.database.check_access_code(account.account_id, access_code))
        self.assertFalse(self.database.check_access_code(account.account_id, "WRONGCODE123"))

        self.assertEqual(self.database.revoke_access_codes(account.account_id), 1)
        self.assertFalse(self.database.check_access_code(account.account_id, access_code))

    def test_access_code_is_stored_only_as_a_salted_hash(self) -> None:
        account = self.database.create_account("Alice")
        self.database.add_access_code(account.account_id, "SAMECODE2345")
        self.database.add_access_code(account.account_id, "SAMECODE2345")
        stored = [row[0] for row in self.database._connection.execute("SELECT code_hash FROM access_codes")]
        self.assertTrue(all(value.startswith("scrypt$") for value in stored))
        self.assertTrue(all("SAMECODE2345" not in value for value in stored))
        self.assertNotEqual(stored[0], stored[1], "each code gets its own salt")

    def test_ban_lasts_until_its_end(self) -> None:
        account = self.database.create_account("Alice")
        self.database.ban_account(account.account_id, until=START + timedelta(hours=2), reason="spam")
        banned = self.database.find_account("Alice")
        self.assertTrue(banned.is_banned_at(START))
        self.assertFalse(banned.is_banned_at(START + timedelta(hours=3)))
        self.database.unban_account(account.account_id)
        self.assertFalse(self.database.find_account("Alice").is_banned_at(START))

    def test_an_account_belongs_to_at_most_one_clan(self) -> None:
        alice = self.database.create_account("Alice")
        romans = self.database.create_clan("Romans")
        vikings = self.database.create_clan("Vikings")
        self.database.set_clan_of_account(alice.account_id, romans.clan_id)
        self.database.set_clan_of_account(alice.account_id, vikings.clan_id)
        self.assertEqual(self.database.find_clan_of_account(alice.account_id), vikings)
        self.assertEqual(self.database.list_clan_member_names(romans.clan_id), [])
        self.database.set_clan_of_account(alice.account_id, None)
        self.assertIsNone(self.database.find_clan_of_account(alice.account_id))

    def test_news_per_audience_is_replaced_not_duplicated(self) -> None:
        self.database.set_news("german", "Willkommen", "Erste Fassung")
        self.database.set_news("german", "Willkommen", "Zweite Fassung")
        self.assertEqual(self.database.find_news("german").body, "Zweite Fassung")
        self.assertIsNone(self.database.find_news("other"))

    def test_board_messages_belong_to_their_theme(self) -> None:
        theme = self.database.add_board_theme("English", "Tournaments", "Upcoming events")
        message = self.database.post_board_message(theme.theme_id, "Cup", "Saturday 8 pm", author="Alice")
        self.assertEqual(self.database.list_board_messages(theme.theme_id), [message])
        self.database.delete_board_theme(theme.theme_id)
        self.assertIsNone(self.database.find_board_message(message.message_id))

    def test_settings_fall_back_to_their_defaults(self) -> None:
        self.assertEqual(self.database.get_integer_setting(Setting.MAX_PLAYERS), 0)
        self.assertFalse(self.database.get_boolean_setting(Setting.UNIQUE_SERIAL_NUMBERS))
        self.database.set_setting(Setting.MAX_PLAYERS, "50")
        self.assertEqual(self.database.get_integer_setting(Setting.MAX_PLAYERS), 50)

    def test_statistics_count_players_and_games_per_period(self) -> None:
        alice = self.database.create_account("Alice")
        bob = self.database.create_account("Bob")
        old_session = self.database.record_logon(bob.account_id)
        self.clock.advance(minutes=10)
        self.database.record_logout(old_session)
        old_game = self.database.record_game_opened("Old game", bob.account_id)
        self.database.record_game_closed(old_game)

        self.clock.advance(hours=3)
        self.database.record_logon(alice.account_id)
        self.database.record_game_opened("New game", alice.account_id)

        statistics = self.database.collect_statistics()
        self.assertEqual(statistics.players_online, 1)
        self.assertEqual(statistics.players_last_hour, 1)
        self.assertEqual(statistics.players_last_day, 2)
        self.assertEqual(statistics.games_open, 1)
        self.assertEqual(statistics.games_last_hour, 1)
        self.assertEqual(statistics.games_last_day, 2)

    def test_restart_closes_what_the_previous_run_left_open(self) -> None:
        alice = self.database.create_account("Alice")
        self.database.record_logon(alice.account_id)
        self.database.record_game_opened("Game", alice.account_id)
        self.database.close_open_sessions_and_games()
        statistics = self.database.collect_statistics()
        self.assertEqual((statistics.players_online, statistics.games_open), (0, 0))

    def test_schema_version_is_stored_and_reopening_keeps_the_data(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lobby.sqlite3"
            first = LobbyDatabase(path)
            first.create_account("Alice")
            first.close()
            reopened = LobbyDatabase(path)
            self.assertIsNotNone(reopened.find_account("Alice"))
            self.assertEqual(reopened.schema_version, SCHEMA_VERSION)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
