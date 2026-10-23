"""The SQLite database: accounts and access codes, clans, news, themes, board, statistics."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from s3lobby.constants import MAX_ANNOUNCEMENT_LENGTH, MAX_GROUP_TEXT_LENGTH
from s3lobby.database import (
    ACCESS_CODE_ALPHABET,
    ACCESS_CODE_LENGTH,
    SCHEMA_UPGRADES,
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

    def test_access_code_is_checked_without_regard_to_case(self) -> None:
        account = self.database.create_account("Alice")
        access_code = self.database.issue_access_code(account.account_id)
        for typed_code in (access_code, access_code.lower(), access_code.swapcase().capitalize()):
            with self.subTest(typed_code=typed_code):
                self.assertTrue(self.database.check_access_code(account.account_id, typed_code))
        self.database.add_access_code(account.account_id, "lowercode234")
        self.assertTrue(self.database.check_access_code(account.account_id, "LOWERCODE234"))

    def test_access_code_is_stored_only_as_a_salted_hash(self) -> None:
        account = self.database.create_account("Alice")
        self.database.add_access_code(account.account_id, "SAMECODE2345")
        self.database.add_access_code(account.account_id, "SAMECODE2345")
        stored = [row[0] for row in self.database._connection.execute("SELECT code_hash FROM access_codes")]
        self.assertTrue(all(value.startswith("scrypt$") for value in stored))
        self.assertTrue(all("SAMECODE2345" not in value for value in stored))
        self.assertNotEqual(stored[0], stored[1], "each code gets its own salt")

    def test_single_access_code_is_revoked_and_listed_without_the_code(self) -> None:
        alice = self.database.create_account("Alice")
        bob = self.database.create_account("Bob")
        self.database.add_access_code(alice.account_id, "FIRSTCODE234", label="laptop")
        second_code_id = self.database.add_access_code(alice.account_id, "SECONDCODE23")

        self.assertFalse(self.database.revoke_access_code(bob.account_id, second_code_id), "only the owner's codes")
        self.assertTrue(self.database.revoke_access_code(alice.account_id, second_code_id))
        self.assertFalse(self.database.revoke_access_code(alice.account_id, second_code_id), "already revoked")

        codes = self.database.list_access_codes(alice.account_id)
        self.assertEqual([(code.label, code.is_active) for code in codes], [("laptop", True), ("", False)])
        self.assertTrue(self.database.check_access_code(alice.account_id, "FIRSTCODE234"))
        self.assertFalse(self.database.check_access_code(alice.account_id, "SECONDCODE23"))
        self.assertEqual(self.database.list_access_codes(alice.account_id)[0].last_used_at, START)

    def test_recent_games_reports_and_announcements_come_newest_first(self) -> None:
        alice = self.database.create_account("Alice")
        self.database.record_game_opened("First", alice.account_id)
        second_game = self.database.record_game_opened("Second", None)
        self.database.record_game_closed(second_game)
        self.database.store_game_report(alice.account_id, 0x100000, b"\x01\x02\x03")
        self.database.queue_announcement("one")
        self.database.queue_announcement("two")
        self.database.take_pending_announcements()
        self.database.queue_announcement("three")

        games = self.database.list_recent_games(limit=10)
        self.assertEqual([(game.name, game.host_name, game.closed_at) for game in games], [("Second", "", START), ("First", "Alice", None)])
        self.assertEqual(len(self.database.list_recent_games(limit=1)), 1)

        [report] = self.database.list_recent_game_reports(limit=10)
        self.assertEqual((report.sender_name, report.group_id, report.size), ("Alice", 0x100000, 3))
        self.assertEqual(self.database.find_game_report(report.report_id), b"\x01\x02\x03")
        self.assertIsNone(self.database.find_game_report(report.report_id + 1))

        announcements = self.database.list_recent_announcements(limit=10)
        self.assertEqual([(entry.text, entry.sent_at) for entry in announcements], [("three", None), ("two", START), ("one", START)])

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

    def test_every_setting_is_stored_under_its_own_key(self) -> None:
        """Settings with equal defaults once were Enum aliases: banner_id wrote max_players."""
        self.assertEqual(len(list(Setting)), len({setting.key for setting in Setting}))
        self.database.set_setting(Setting.BANNER_ID, "7")
        self.database.set_setting(Setting.BANNER_URL, "ftp://192.168.1.10/banner.png")
        self.assertEqual(self.database.get_integer_setting(Setting.MAX_PLAYERS), 0)
        self.assertEqual(self.database.get_integer_setting(Setting.BANNER_ID), 7)
        self.assertEqual(self.database.get_setting(Setting.WELCOME_MESSAGE), "")

    def test_texts_the_game_cannot_show_are_refused(self) -> None:
        """The server encodes stored texts for the game as they are; one it cannot encode would
        fail every time a client asks for it."""
        refused_calls = {
            "character outside the game's code page": lambda: self.database.add_chat_theme("Party \U0001f389"),
            "control character in a name": lambda: self.database.add_chat_theme("Tab\there"),
            "line break in a title": lambda: self.database.set_news("german", "Two\nLines", "text"),
            "empty title": lambda: self.database.set_news("german", "  ", "text"),
            "name too long": lambda: self.database.create_clan("x" * (MAX_GROUP_TEXT_LENGTH + 1)),
            "unknown audience": lambda: self.database.set_news("french", "Bonjour", "text"),
            "unknown board language": lambda: self.database.add_board_theme("Latin", "Forum"),
            "announcement too long": lambda: self.database.queue_announcement("x" * (MAX_ANNOUNCEMENT_LENGTH + 1)),
        }
        for case, refused_call in refused_calls.items():
            with self.subTest(case=case):
                with self.assertRaises(DatabaseError):
                    refused_call()
        self.database.set_news("german", "Grüße", "Erste Zeile\nZweite Zeile mit ä, ö, ü und €")
        self.assertEqual(self.database.find_news("german").title, "Grüße")

    def test_setting_values_are_checked(self) -> None:
        refused_values = {
            Setting.MAX_PLAYERS: ("-1", "ten", str(2**32)),
            Setting.UNIQUE_SERIAL_NUMBERS: ("maybe",),
            Setting.BANNER_URL: ("http://example.org/banner.png",),
            Setting.WELCOME_MESSAGE: ("x" * (MAX_ANNOUNCEMENT_LENGTH + 1),),
        }
        for setting, values in refused_values.items():
            for value in values:
                with self.subTest(setting=setting.key, value=value):
                    with self.assertRaises(DatabaseError):
                        self.database.set_setting(setting, value)
        self.database.set_setting(Setting.BANNER_URL, "")
        self.database.set_setting(Setting.UNIQUE_SERIAL_NUMBERS, "Yes")
        self.assertTrue(self.database.get_boolean_setting(Setting.UNIQUE_SERIAL_NUMBERS))

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

    def test_database_of_an_older_version_is_upgraded_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.sqlite3"
            old_database = sqlite3.connect(path)
            old_database.executescript(SCHEMA_UPGRADES[1])
            old_database.execute("INSERT INTO accounts (name, created_at) VALUES ('Alice', '2026-10-01T00:00:00+00:00')")
            old_database.execute("PRAGMA user_version = 1")
            old_database.commit()
            old_database.close()

            upgraded = LobbyDatabase(path)
            self.assertEqual(upgraded.schema_version, SCHEMA_VERSION)
            self.assertIsNotNone(upgraded.find_account("Alice"))
            upgraded.queue_announcement("works")
            self.assertEqual(upgraded.take_pending_announcements(), ["works"])
            upgraded.close()

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
