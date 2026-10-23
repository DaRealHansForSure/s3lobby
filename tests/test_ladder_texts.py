"""The ladder's lobby texts (s3lobby/ladder_texts.py): board messages, the news block, the lines."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from s3lobby.constants import MAX_TEXT_MESSAGE_BYTES, LadderState, LadderType, SeasonCounts
from s3lobby.database import DatabaseError, LobbyDatabase, Setting, check_game_text
from s3lobby.ladder import Evaluation, ResultPlayer
from s3lobby.ladder_book import LadderBook
from s3lobby.ladder_texts import (
    BOARD_BYLINE_RESERVE_BYTES,
    ENGLISH,
    GERMAN,
    LadderTexts,
    LadderWriter,
    fit_board_text,
    text_size,
    week_start_of,
)

# A Wednesday; its week started on Monday 19 October 2026.
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
LAST_WEEK = datetime(2026, 10, 12, tzinfo=timezone.utc)


class SettableClock:
    """A clock the test sets."""

    def __init__(self) -> None:
        """Start at NOW."""
        self.now = NOW

    def __call__(self) -> datetime:
        """The time the test set."""
        return self.now


class LadderTextsTest(unittest.TestCase):
    """Alice beat Bob three times last week, Bob beat Alice once."""

    def setUp(self) -> None:
        """A database with last week's results and a writer on it."""
        self.clock = SettableClock()
        self.database = LobbyDatabase(":memory:", clock=self.clock)
        self.alice = self.database.create_account("Alice").account_id
        self.bob = self.database.create_account("Bob").account_id
        for day, winner, loser in ((12, self.alice, self.bob), (13, self.alice, self.bob), (14, self.alice, self.bob),
                                   (15, self.bob, self.alice)):
            self.store(day, winner, loser)
        self.book = LadderBook(self.database)
        self.texts = LadderTexts(self.database, self.book)
        self.writer = LadderWriter(self.database, self.book)

    def tearDown(self) -> None:
        """Close the database."""
        self.database.close()

    def store(self, day: int, winner: int, loser: int) -> int:
        """A counted Liga result on a day of October."""
        game_id = self.database.record_game_opened("Duel", winner, ladder_type=LadderType.INDIVIDUAL)
        self.database.store_ladder_result(
            Evaluation(
                game_id,
                LadderType.INDIVIDUAL,
                LadderState.COUNTED,
                winning_team=0,
                played_at=datetime(2026, 10, day, 20, 0, tzinfo=timezone.utc),
                players=(
                    ResultPlayer(winner, None, 0, 0, 0, True, False),
                    ResultPlayer(loser, None, 1, 0, 1, False, False),
                ),
            )
        )
        return game_id

    def board_titles(self, language: str) -> list[str]:
        """The titles in the server's theme of a language, oldest first."""
        theme = self.writer.server_themes()[language]
        return [message.title for message in self.database.list_board_messages(theme.theme_id)]

    def test_the_weekly_summary_names_the_week_s_facts(self) -> None:
        """Top places, the climber, the most matches, the longest run; in both languages."""
        english = self.texts.weekly_summary(LAST_WEEK, ENGLISH)
        self.assertEqual(english.title, "Week of 12 Oct 2026 to 18 Oct 2026")
        self.assertIn("1. Alice 1024", english.lines)
        self.assertIn("Longest winning run: Alice (3)", english.lines)
        self.assertIn("Most matches: Alice (4)", english.lines)
        german = self.texts.weekly_summary(LAST_WEEK, GERMAN)
        self.assertEqual(german.title, "Wochenbericht 12. Okt 2026 bis 18. Okt 2026")
        self.assertIn("Längste Siegesserie: Alice (3)", german.lines)

    def test_a_quiet_week_has_no_summary(self) -> None:
        """Nothing to write about a week without a counted result."""
        self.assertIsNone(self.texts.weekly_summary(LAST_WEEK - timedelta(days=7), ENGLISH))

    def test_every_text_is_one_the_game_can_show(self) -> None:
        """The database's own check passes for each text the writer could post."""
        season = self.database.add_ladder_season("Autumn Cup", LAST_WEEK, NOW, SeasonCounts.LIGA)
        texts = [
            self.texts.weekly_summary(LAST_WEEK, ENGLISH),
            self.texts.weekly_summary(LAST_WEEK, GERMAN),
            self.texts.season_start(season, GERMAN),
            self.texts.season_end(season, ENGLISH),
        ]
        for text in texts:
            with self.subTest(title=text.title):
                check_game_text("text", text.body, MAX_TEXT_MESSAGE_BYTES, allows_line_breaks=True)

    def test_a_long_text_loses_whole_lines_from_its_end(self) -> None:
        """The text and the byline fit the game's limit, and no line is cut in the middle."""
        lines = [f"line {number} " + "x" * 80 for number in range(40)]
        fitted = fit_board_text("Title", lines)
        self.assertLessEqual(text_size(fitted.title, fitted.lines), MAX_TEXT_MESSAGE_BYTES - BOARD_BYLINE_RESERVE_BYTES)
        self.assertEqual(list(fitted.lines), lines[: len(fitted.lines)])

    def test_the_writer_posts_last_week_once(self) -> None:
        """The summary of the last full week goes on the board in both languages, and only once."""
        self.writer.run()
        self.writer.run()
        self.assertEqual(self.board_titles(ENGLISH), ["Week of 12 Oct 2026 to 18 Oct 2026"])
        self.assertEqual(self.board_titles(GERMAN), ["Wochenbericht 12. Okt 2026 bis 18. Okt 2026"])

    def test_a_season_start_and_end_are_written_with_an_announcement(self) -> None:
        """A season that started posts its start; once it ended, its end and one announcement."""
        season_start = NOW - timedelta(days=1)
        season = self.database.add_ladder_season("Autumn Cup", season_start, NOW + timedelta(days=1), SeasonCounts.LIGA)
        self.store(20, self.alice, self.bob)
        self.assertEqual(self.writer.run().announcements, ())
        self.assertIn("Autumn Cup begins", self.board_titles(ENGLISH))
        self.clock.now = NOW + timedelta(days=2)
        written = self.writer.run()
        self.assertEqual(written.announcements, ("Autumn Cup is over: Alice 1016 win. Final standings on the board.",))
        self.assertIn("Autumn Cup is over", self.board_titles(ENGLISH))
        self.assertEqual(self.writer.run().announcements, ())
        self.assertEqual(self.database.find_ladder_season("Autumn Cup"), season)

    def test_an_old_season_end_is_noted_but_not_written(self) -> None:
        """A season created afterwards for past dates must not flood the board."""
        old_start = NOW - timedelta(days=60)
        self.database.add_ladder_season("Old Cup", old_start, NOW - timedelta(days=30), SeasonCounts.BOTH)
        self.assertEqual(self.writer.run().announcements, ())
        self.assertNotIn("Old Cup is over", self.board_titles(ENGLISH))

    def test_the_board_keeps_the_newest_messages(self) -> None:
        """Older messages go, the setting's number stay."""
        self.database.set_setting(Setting.LADDER_BOARD_MESSAGES, "1")
        self.database.add_ladder_season("Autumn Cup", LAST_WEEK, NOW + timedelta(days=1), SeasonCounts.LIGA)
        self.writer.run()
        self.assertEqual(len(self.board_titles(ENGLISH)), 1)

    def test_nobody_else_can_post_into_the_server_s_theme(self) -> None:
        """The theme the writer creates is the server's."""
        theme = self.writer.server_themes()[ENGLISH]
        with self.assertRaises(DatabaseError):
            self.database.post_board_message(theme.theme_id, "Spam", "spam")

    def test_the_news_block_and_the_lines(self) -> None:
        """The top five under the news, a player's place at logon, an admin's decision."""
        self.assertTrue(self.texts.news_lines(ENGLISH)[0].startswith("Ladder (infinite season): 1. Alice "))
        self.assertTrue(self.texts.news_lines(GERMAN)[0].startswith("Liga (unendliche Saison): 1. Alice "))
        self.assertEqual(self.texts.logon_line(self.bob), "Liga: 976, place 2 of 2.")
        decided = self.book.results()[0].evaluation
        self.assertEqual(self.texts.decision_line(decided, "Duel"), "Ladder: an admin decided Duel: Alice won.")

    def test_a_week_starts_on_monday(self) -> None:
        """Weeks run Monday to Sunday, UTC."""
        self.assertEqual(week_start_of(NOW), datetime(2026, 10, 19, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
