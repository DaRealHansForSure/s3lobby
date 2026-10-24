"""The database: accounts and access codes, clans, news, themes, board, statistics.

Runs on SQLite, or on MySQL when S3LOBBY_TEST_DATABASE names one (tests/databases.py).
"""

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
    MAX_PERMANENT_CHAT_GROUP_NAME_LENGTH,
    DatabaseError,
    LobbyDatabase,
    Setting,
    is_matching_access_code,
    is_valid_player_name,
    is_whole_number_text,
)
from s3lobby.migrations import latest_version, load_migrations
from tests.databases import database_location_for_test

START = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


class SteppingClock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        """Start at START."""
        self.now = START

    def __call__(self) -> datetime:
        """The time the test has moved the clock to."""
        return self.now

    def advance(self, **duration: float) -> None:
        """Move the clock forward by a timedelta given as keyword arguments, such as minutes=10."""
        self.now += timedelta(**duration)


class LobbyDatabaseTest(unittest.TestCase):
    """LobbyDatabase on a fresh database per test, SQLite in memory or the configured MySQL one, with a clock
    the test moves."""

    def setUp(self) -> None:
        """A fresh database on a clock that starts at START."""
        self.clock = SteppingClock()
        self.database = LobbyDatabase(database_location_for_test(":memory:"), clock=self.clock)

    def tearDown(self) -> None:
        """Close the database."""
        self.database.close()

    def test_every_new_theme_gets_a_permanent_chat_group(self) -> None:
        """A theme starts with one permanent group named after it; admins add more, and removing
        the theme takes its groups along."""
        tactics = self.database.add_chat_theme("Tactics")
        self.database.add_permanent_chat_group(tactics.theme_id, "Openings")
        names = [group.name for group in self.database.list_permanent_chat_groups() if group.chat_theme_id == tactics.theme_id]
        self.assertEqual(names, ["Tactics", "Openings"])
        self.database.delete_chat_theme(tactics.theme_id)
        self.assertNotIn(tactics.theme_id, [group.chat_theme_id for group in self.database.list_permanent_chat_groups()])

    def test_a_permanent_group_name_is_unique(self) -> None:
        """Group names stay unique: a second "General" in any case is refused, and a theme whose
        name a group has already gets no group of the same name."""
        with self.assertRaises(DatabaseError):
            self.database.add_permanent_chat_group(None, "general")
        self.database.add_permanent_chat_group(None, "Tactics")
        tactics = self.database.add_chat_theme("Tactics")
        names = [group.name.casefold() for group in self.database.list_permanent_chat_groups()]
        self.assertEqual(names.count("tactics"), 1)
        self.assertNotIn(tactics.theme_id, [group.chat_theme_id for group in self.database.list_permanent_chat_groups()])

    def test_a_permanent_group_name_leaves_room_for_the_server_mark(self) -> None:
        """The game shows "[Server] " in front, and the whole name must fit the game's limit."""
        with self.assertRaises(DatabaseError):
            self.database.add_permanent_chat_group(None, "X" * (MAX_PERMANENT_CHAT_GROUP_NAME_LENGTH + 1))

    def test_account_search_takes_wildcards_literally(self) -> None:
        """A search for "a_b" or "%" finds names containing exactly that, not everything."""
        self.database.create_account("Alice")
        self.database.create_account("Bob")
        self.assertEqual(self.database.count_accounts("li"), 1)
        self.assertEqual(self.database.count_accounts("%"), 0)
        self.assertEqual(self.database.count_accounts("_"), 0)
        self.assertEqual([account.name for account in self.database.list_accounts_page("", 1, 1)], ["Bob"])

    def test_player_names_follow_the_game_rule(self) -> None:
        """Only names the game accepts (letters A to Z, single spaces between words) are valid, and an account
        with another name is refused, since its player could never log on."""
        self.assertTrue(is_valid_player_name("Player"))
        self.assertTrue(is_valid_player_name("Roman Legion"))
        for invalid_name in ("", " Player", "Player ", "Two  Spaces", "Player1", "Spieler_X", "Müller"):
            with self.subTest(name=invalid_name):
                self.assertFalse(is_valid_player_name(invalid_name))
        with self.assertRaises(DatabaseError):
            self.database.create_account("Player1")

    def test_account_names_are_unique_regardless_of_case(self) -> None:
        """Names differing only in case are one name, as logons match them without regard to case; the stored
        spelling is kept."""
        self.database.create_account("Alice")
        with self.assertRaises(DatabaseError):
            self.database.create_account("ALICE")
        self.assertEqual(self.database.find_account("alice").name, "Alice")

    def test_issued_access_code_is_checked_by_hash_and_can_be_revoked(self) -> None:
        """An issued code has the expected length and alphabet, is accepted while active and refused once
        revoked; a wrong code is refused."""
        account = self.database.create_account("Alice")
        access_code = self.database.issue_access_code(account.account_id, label="home PC")
        self.assertEqual(len(access_code), ACCESS_CODE_LENGTH)
        self.assertTrue(set(access_code) <= set(ACCESS_CODE_ALPHABET))

        self.assertTrue(self.database.check_access_code(account.account_id, access_code))
        self.assertFalse(self.database.check_access_code(account.account_id, "WRONGCODE123"))

        self.assertEqual(self.database.revoke_access_codes(account.account_id), 1)
        self.assertFalse(self.database.check_access_code(account.account_id, access_code))

    def test_access_code_is_checked_without_regard_to_case(self) -> None:
        """A code is accepted in any case, whether issued or added in lower case, since players type it into
        the game's password field by hand."""
        account = self.database.create_account("Alice")
        access_code = self.database.issue_access_code(account.account_id)
        for typed_code in (access_code, access_code.lower(), access_code.swapcase().capitalize()):
            with self.subTest(typed_code=typed_code):
                self.assertTrue(self.database.check_access_code(account.account_id, typed_code))
        self.database.add_access_code(account.account_id, "lowercode234")
        self.assertTrue(self.database.check_access_code(account.account_id, "LOWERCODE234"))

    def test_matching_code_is_named_and_stays_active_until_revoked(self) -> None:
        """A web session keeps the id of the code it logged on with and ends when it is revoked."""
        alice = self.database.create_account("Alice")
        bob = self.database.create_account("Bob")
        self.database.add_access_code(alice.account_id, "FIRSTCODE234")
        second_code_id = self.database.add_access_code(alice.account_id, "SECONDCODE23")
        self.assertEqual(self.database.match_access_code(alice.account_id, "SECONDCODE23"), second_code_id)
        self.assertIsNone(self.database.match_access_code(alice.account_id, "WRONGCODE234"))
        self.assertTrue(self.database.is_access_code_active(alice.account_id, second_code_id))
        self.assertFalse(self.database.is_access_code_active(bob.account_id, second_code_id), "only the owner's")
        self.database.revoke_access_code(alice.account_id, second_code_id)
        self.assertFalse(self.database.is_access_code_active(alice.account_id, second_code_id))

    def test_access_code_is_stored_only_as_a_salted_hash(self) -> None:
        """Codes are stored only as scrypt hashes, each with its own salt, so a stolen database file shows
        neither the codes nor which accounts share one."""
        account = self.database.create_account("Alice")
        self.database.add_access_code(account.account_id, "SAMECODE2345")
        self.database.add_access_code(account.account_id, "SAMECODE2345")
        stored = [row["code_hash"] for row in self.database._backend.fetch_all("SELECT code_hash FROM access_codes")]
        self.assertTrue(all(value.startswith("scrypt$") for value in stored))
        self.assertTrue(all("SAMECODE2345" not in value for value in stored))
        self.assertNotEqual(stored[0], stored[1], "each code gets its own salt")

    def test_single_access_code_is_revoked_and_listed_without_the_code(self) -> None:
        """One code can be revoked on its own, only by its owner and only once; the listing shows label, state
        and last use but never the code, and the remaining code still works."""
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
        """The recent games, game reports and announcements the web interface shows come newest first, with
        their host, sender, close and send times, and limits are kept."""
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
        """A ban holds until its end and no longer, and lifting it ends it at once."""
        account = self.database.create_account("Alice")
        self.database.ban_account(account.account_id, until=START + timedelta(hours=2), reason="spam")
        banned = self.database.find_account("Alice")
        self.assertTrue(banned.is_banned_at(START))
        self.assertFalse(banned.is_banned_at(START + timedelta(hours=3)))
        self.database.unban_account(account.account_id)
        self.assertFalse(self.database.find_account("Alice").is_banned_at(START))

    def test_an_account_belongs_to_at_most_one_clan(self) -> None:
        """Joining another clan leaves the first, and an account can leave its clan altogether."""
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
        """Each audience has at most one news text, replaced on each change, since Home offers one text id per
        audience."""
        self.database.set_news("german", "Willkommen", "Erste Fassung")
        self.database.set_news("german", "Willkommen", "Zweite Fassung")
        self.assertEqual(self.database.find_news("german").body, "Zweite Fassung")
        self.assertIsNone(self.database.find_news("other"))

    def test_board_messages_belong_to_their_theme(self) -> None:
        """Deleting a board theme deletes its messages too, so no message is left without a theme to be listed
        in."""
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
        """Values the server could not use are refused when they are set: numbers out of range, booleans that
        are no yes or no, banner URLs the game cannot fetch, and welcome lines that are too long."""
        refused_values = {
            Setting.MAX_PLAYERS: ("-1", "ten", str(2**32)),
            Setting.UNIQUE_SERIAL_NUMBERS: ("maybe",),
            # The game connects to the host as it is, anonymously on port 21, and needs a path.
            Setting.BANNER_URL: (
                "http://example.org/banner.png",
                "ftp://example.org",
                "ftp://example.org/",
                "ftp://example.org:2121/banner.png",
                "ftp://anonymous@example.org/banner.png",
            ),
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
        """A setting never set reads as its default until a value is stored."""
        self.assertEqual(self.database.get_integer_setting(Setting.MAX_PLAYERS), 0)
        self.assertFalse(self.database.get_boolean_setting(Setting.UNIQUE_SERIAL_NUMBERS))
        self.database.set_setting(Setting.MAX_PLAYERS, "50")
        self.assertEqual(self.database.get_integer_setting(Setting.MAX_PLAYERS), 50)

    def test_statistics_count_players_and_games_per_period(self) -> None:
        """The statistics count who is online and what is open now, and the players and games of the last hour
        and the last day, for Home and the web interface."""
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

    def test_ban_and_deletion_queue_the_removal_from_the_running_lobby(self) -> None:
        """Another process cannot reach the server: it leaves the kick in the database."""
        alice = self.database.create_account("Alice")
        bob = self.database.create_account("Bob")
        self.database.ban_account(alice.account_id, START + timedelta(hours=2), "spam")
        self.database.delete_account(bob.account_id)
        self.database.queue_kick(alice.account_id, "by hand")
        kicks = self.database.take_pending_kicks()
        self.assertEqual([kick.account_id for kick in kicks], [alice.account_id, bob.account_id, alice.account_id])
        self.assertIn("spam", kicks[0].reason)
        self.assertEqual(self.database.take_pending_kicks(), [], "each kick is taken once")

    def test_damaged_stored_hash_matches_no_code_instead_of_failing(self) -> None:
        """A logon must get a refusal, not a server error, when a stored hash is damaged."""
        self.assertFalse(is_matching_access_code("ANYCODE23456", "scrypt$16384$8$1$zz$zz"))
        self.assertFalse(is_matching_access_code("ANYCODE23456", "scrypt$many$8$1$00$00"))

    def test_settings_change_together_or_not_at_all(self) -> None:
        """A form saving several settings with one bad value changes none of them, so the settings never end
        up half saved."""
        with self.assertRaises(DatabaseError):
            self.database.set_settings({Setting.WELCOME_MESSAGE: "Hello", Setting.MAX_PLAYERS: "lots"})
        self.assertEqual(self.database.get_setting(Setting.WELCOME_MESSAGE), "")
        self.database.set_settings({Setting.WELCOME_MESSAGE: "Hello", Setting.MAX_PLAYERS: "20"})
        self.assertEqual(self.database.get_integer_setting(Setting.MAX_PLAYERS), 20)

    def test_failing_database_raises_database_error_not_the_drivers(self) -> None:
        """The command line and the web interface catch DatabaseError; a driver's own error would
        reach the user as a traceback."""
        self.database.close()
        with self.assertRaises(DatabaseError):
            self.database.list_accounts()
        self.database = LobbyDatabase(":memory:", clock=self.clock)

    def test_only_plain_digits_count_as_a_number(self) -> None:
        """str.isdigit() accepts "\u00b2", which int() refuses: the check must not let it through."""
        self.assertTrue(is_whole_number_text("24"))
        for text in ("\u00b2", "\u0663", "-1", "1.5", ""):
            with self.subTest(text=text):
                self.assertFalse(is_whole_number_text(text))
        with self.assertRaises(DatabaseError):
            self.database.set_setting(Setting.MAX_PLAYERS, "\u00b2")

    def test_labels_and_ban_reasons_are_checked_like_game_texts(self) -> None:
        """Labels longer than the limit and ban reasons with a line break are refused; a ban reason is shown
        to the player in a single chat line."""
        alice = self.database.create_account("Alice")
        with self.assertRaises(DatabaseError):
            self.database.add_access_code(alice.account_id, "SAMECODE2345", label="x" * 65)
        with self.assertRaises(DatabaseError):
            self.database.ban_account(alice.account_id, START + timedelta(hours=1), "line\nbreak")

    def test_announcements_queued_while_stopped_are_dropped_at_start(self) -> None:
        """Announcements queued while the server was stopped are stale by the time it starts, so they are
        dropped instead of sent."""
        self.database.queue_announcement("Server restart at 22:00")
        self.assertEqual(self.database.discard_pending_announcements(), 1)
        self.assertEqual(self.database.take_pending_announcements(), [])

    def test_restart_closes_what_the_previous_run_left_open(self) -> None:
        """Sessions and games a crashed run left open are closed at the next start, where nobody is connected
        yet, so the statistics do not count them forever."""
        alice = self.database.create_account("Alice")
        self.database.record_logon(alice.account_id)
        self.database.record_game_opened("Game", alice.account_id)
        self.database.close_open_sessions_and_games()
        statistics = self.database.collect_statistics()
        self.assertEqual((statistics.players_online, statistics.games_open), (0, 0))

    def test_database_of_an_older_version_is_upgraded_in_place(self) -> None:
        """A file made by the first schema, with data in it, is migrated to the newest version on opening,
        keeps its data and works with the newer tables."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.sqlite3"
            old_database = sqlite3.connect(path)
            old_database.executescript(load_migrations("sqlite")[0].sql)
            old_database.execute("INSERT INTO accounts (name, created_at) VALUES ('Alice', '2026-10-01T00:00:00+00:00')")
            old_database.execute("PRAGMA user_version = 1")
            old_database.commit()
            old_database.close()

            upgraded = LobbyDatabase(path)
            self.assertEqual(upgraded.schema_version, latest_version("sqlite"))
            self.assertIsNotNone(upgraded.find_account("Alice"))
            upgraded.queue_announcement("works")
            self.assertEqual(upgraded.take_pending_announcements(), ["works"])
            upgraded.close()

    def test_schema_version_is_stored_and_reopening_keeps_the_data(self) -> None:
        """A database file reopened keeps its data and its schema version."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lobby.sqlite3"
            first = LobbyDatabase(path)
            first.create_account("Alice")
            first.close()
            reopened = LobbyDatabase(path)
            self.assertIsNotNone(reopened.find_account("Alice"))
            self.assertEqual(reopened.schema_version, latest_version("sqlite"))
            reopened.close()


if __name__ == "__main__":
    unittest.main()
