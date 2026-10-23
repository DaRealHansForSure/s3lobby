"""The ladder on the web: the public standings, and the admins' ladder games and seasons.

Skipped where Flask is not installed, like tests/test_web.py.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from s3lobby.constants import LadderState, LadderType, SeasonCounts
from s3lobby.ladder import Evaluation, ResultPlayer
from tests.test_web import IS_FLASK_INSTALLED, PLAYER_NAME, WebInterfaceTestCase

PLAYED_AT = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)


@unittest.skipUnless(IS_FLASK_INSTALLED, "the web interface needs Flask: pip install .[web]")
class LadderPagesTest(WebInterfaceTestCase):
    """The admin (Boss) beat the player in a counted Liga game; another game is disputed."""

    def setUp(self) -> None:
        """Store the two results."""
        super().setUp()
        self.counted_game = self.store(LadderState.COUNTED, "First")
        self.disputed_game = self.store(LadderState.DISPUTED, "Revenge")

    def store(self, state: LadderState, name: str) -> int:
        """A Liga game the admin won (in the reports of a disputed one, at least)."""
        game_id = self.database.record_game_opened(name, self.admin.account_id, ladder_type=LadderType.INDIVIDUAL)
        self.database.store_ladder_result(
            Evaluation(
                game_id,
                LadderType.INDIVIDUAL,
                state,
                winning_team=0,
                game_ticks=30_000,
                played_at=PLAYED_AT,
                players=(
                    ResultPlayer(self.admin.account_id, None, 0, 0, 0, True, False),
                    ResultPlayer(self.player.account_id, None, 1, 1, 1, False, False),
                ),
            )
        )
        return game_id

    def test_the_standings_are_public(self) -> None:
        """Visitors see the ladder like the live lobby, without logging on."""
        response = self.client.get("/ladder")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn(PLAYER_NAME, page)
        self.assertIn("Rating over time", page)

    def test_every_view_and_an_unknown_season_render(self) -> None:
        """Clanliga, clan strength, a season that does not exist (falls back to the infinite one)."""
        for path in ("/ladder?view=clanliga", "/ladder?view=clans", "/ladder?season=Nope", "/ladder?view=bogus"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_the_admin_page_needs_a_logon(self) -> None:
        """Visitors are sent to the logon page."""
        self.assertEqual(self.client.get("/admin/ladder/games").status_code, 302)

    def test_an_admin_decides_a_disputed_game(self) -> None:
        """The player's team wins by the admin's decision; the decision names the admin."""
        self.log_on()
        page = self.client.get("/admin/ladder/games").get_data(as_text=True)
        self.assertIn("need a decision", page)
        self.post(f"/admin/ladder/games/{self.disputed_game}/decide", winner="1")
        result = self.database.find_ladder_result(self.disputed_game)
        self.assertEqual(result.evaluation.state, LadderState.COUNTED)
        self.assertEqual(result.decided_by_name, self.admin.name)

    def test_an_admin_voids_a_result(self) -> None:
        """Voiding keeps the reason given."""
        self.log_on()
        self.post(f"/admin/ladder/games/{self.counted_game}/void", reason="played on a broken map")
        result = self.database.find_ladder_result(self.counted_game)
        expected = (LadderState.VOID, "played on a broken map")
        self.assertEqual((result.evaluation.state, result.evaluation.reason), expected)

    def test_a_decision_without_the_form_token_is_refused(self) -> None:
        """Every POST carries the CSRF token; without it nothing changes."""
        self.log_on()
        self.client.post(f"/admin/ladder/games/{self.counted_game}/void", data={"reason": "x"})
        self.assertEqual(self.database.find_ladder_result(self.counted_game).evaluation.state, LadderState.COUNTED)

    def test_seasons_are_created_edited_ended_and_deleted(self) -> None:
        """The season forms reach the database; the last day typed is included."""
        self.log_on()
        self.post("/admin/ladder/seasons", name="Autumn Cup", start="2026-10-01", end="2026-10-31", counts="liga")
        season = self.database.find_ladder_season("Autumn Cup")
        self.assertEqual(season.ends_at, datetime(2026, 11, 1, tzinfo=timezone.utc))
        season_path = f"/admin/ladder/seasons/{season.season_id}"
        self.post(season_path, name="Herbst Cup", start="2026-10-01", end="", counts="both")
        changed = self.database.find_ladder_season("Herbst Cup")
        self.assertEqual((changed.ends_at, changed.counts), (None, SeasonCounts.BOTH))
        self.post(f"/admin/ladder/seasons/{season.season_id}/end")
        self.assertIsNotNone(self.database.find_ladder_season("Herbst Cup").ends_at)
        self.post(f"/admin/ladder/seasons/{season.season_id}/delete")
        self.assertEqual(self.database.list_ladder_seasons(), [])

    def test_a_game_page_shows_its_ladder_result(self) -> None:
        """The game page says whether and how the game counted."""
        self.log_on()
        page = self.client.get(f"/admin/games/{self.disputed_game}").get_data(as_text=True)
        self.assertIn("<h2>Ladder</h2>", page)
        self.assertIn("disputed", page)

    def test_a_bad_season_form_is_a_message(self) -> None:
        """A malformed date or a duplicate name comes back as a message, not an error page."""
        self.log_on()
        response = self.post("/admin/ladder/seasons", name="Cup", start="tomorrow", end="", counts="liga")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database.list_ladder_seasons(), [])


    def test_a_player_page_shows_place_record_and_games(self) -> None:
        """Players see their own and others' ladder place and games without an admin's logon."""
        page = self.client.get(f"/players/{PLAYER_NAME.lower()}").get_data(as_text=True)
        self.assertIn(f"<h1>{PLAYER_NAME}</h1>", page)
        self.assertIn("Place 2 of 2", page)
        self.assertIn("0 won, 1 lost", page)

    def test_a_player_page_shows_no_admin_content(self) -> None:
        """Public: no access codes, no bans, no addresses."""
        page = self.client.get(f"/players/{PLAYER_NAME}").get_data(as_text=True)
        for hidden in ("Access codes", "Ban", "Revoke"):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, page)

    def test_an_unknown_player_is_not_found(self) -> None:
        """A name without an account gets a 404."""
        self.assertEqual(self.client.get("/players/Nobody").status_code, 404)

    def test_the_standings_link_to_the_player_pages(self) -> None:
        """Every name in the Liga standings leads to that player's page."""
        page = self.client.get("/ladder").get_data(as_text=True)
        self.assertIn(f'href="/players/{PLAYER_NAME}"', page)

if __name__ == "__main__":
    unittest.main()
