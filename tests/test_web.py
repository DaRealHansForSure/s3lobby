"""The web interface: the public live lobby, the admin logon and its protections, and the admin
pages writing to the same database the server reads.

Skipped where Flask is not installed: the lobby server itself does not need it.
"""

from __future__ import annotations

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

from s3lobby.constants import FIRST_GAME_GROUP_ID, LOGON_FAILURE_BURST, NO_WINNING_TEAM, GameStatusFlag, MatchPlayerFlag, Race
from s3lobby.database import LobbyDatabase, Setting
from s3lobby.groups import GroupKind
from s3lobby.live import LiveGroup, LiveLobbyState, LivePlayer
from s3lobby.messages import GameStatus, MatchReportPlayer

IS_FLASK_INSTALLED = importlib.util.find_spec("flask") is not None
if IS_FLASK_INSTALLED:
    from s3lobby.web.app import create_app
    from s3lobby.web.auth import safe_next_path

ADMIN_NAME = "Boss"
ADMIN_CODE = "ADMINCODE234"
PLAYER_NAME = "Alice"
PLAYER_CODE = "PLAYERCODE23"
CSRF_TOKEN = "test-csrf-token"
HTTP_OK = 200
HTTP_FOUND = 302
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_TOO_MANY_REQUESTS = 429
NEW_ACCESS_CODE_PATTERN = re.compile(r'<div class="access-code">([A-Z0-9]{12})</div>')

LIVE_STATE = LiveLobbyState(
    server_name="Test Lobby",
    players=[
        LivePlayer(
            player_id=2,
            name=PLAYER_NAME,
            clan="Romans",
            is_admin=False,
            address="192.0.2.7:2300",
            logged_on_at="2026-10-23T12:00:00+00:00",
            group_ids=[3, 30, FIRST_GAME_GROUP_ID],
        )
    ],
    groups=[
        LiveGroup(group_id=3, kind=GroupKind.ROOM.value, name="GameZone", max_players=0, is_full=False,
                  has_password=False, member_names=[PLAYER_NAME]),
        LiveGroup(group_id=30, kind=GroupKind.ROOM.value, name="League 1 vs 1", max_players=0, is_full=False,
                  has_password=False, member_names=[PLAYER_NAME]),
        LiveGroup(group_id=FIRST_GAME_GROUP_ID, kind=GroupKind.GAME.value, name="<b>Alice's game</b>",
                  max_players=4, is_full=False, has_password=False, member_names=[PLAYER_NAME],
                  listed_in_ids=[30], map_name="\\Random", team_count=2, is_ladder_game=True),
    ],
)


@unittest.skipUnless(IS_FLASK_INSTALLED, "the web interface needs Flask: pip install .[web]")
class WebInterfaceTestCase(unittest.TestCase):
    """A web application on a fresh database with one admin and one player account."""

    def setUp(self) -> None:
        self.database_folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.database_folder.cleanup)
        database_path = str(Path(self.database_folder.name) / "lobby.sqlite3")
        self.database = LobbyDatabase(database_path)
        self.addCleanup(self.database.close)
        self.admin = self.database.create_account(ADMIN_NAME, is_admin=True)
        self.admin_code_id = self.database.add_access_code(self.admin.account_id, ADMIN_CODE)
        self.player = self.database.create_account(PLAYER_NAME)
        self.database.add_access_code(self.player.account_id, PLAYER_CODE)

        self.app = create_app(database_path, secret_key="test secret")
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf_token"] = CSRF_TOKEN

    def post(self, path: str, **form: str):
        return self.client.post(path, data={"csrf_token": CSRF_TOKEN, **form})

    def log_on(self, name: str = ADMIN_NAME, access_code: str = ADMIN_CODE):
        response = self.post("/logon", name=name, access_code=access_code)
        # A logon starts a fresh session; put the test's form token back into it.
        with self.client.session_transaction() as session:
            session["csrf_token"] = CSRF_TOKEN
        return response


class LiveLobbyTest(WebInterfaceTestCase):
    def test_without_a_server_the_lobby_says_so(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("The lobby server is not running", page)

    def test_visitors_see_players_and_games_but_no_addresses(self) -> None:
        self.database.write_live_state(LIVE_STATE)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Running", page)
        self.assertIn(PLAYER_NAME, page)
        self.assertIn("League 1 vs 1", page)
        self.assertNotIn("192.0.2.7", page)

    def test_games_show_their_map_teams_and_that_a_ladder_game_is_unranked(self) -> None:
        self.database.write_live_state(LIVE_STATE)
        page = self.client.get("/live/state").get_data(as_text=True)
        self.assertIn("Random (random map)", page)
        self.assertIn("ladder, unranked", page)

    def test_admins_also_see_addresses(self) -> None:
        self.database.write_live_state(LIVE_STATE)
        self.log_on()
        self.assertIn("192.0.2.7:2300", self.client.get("/live/state").get_data(as_text=True))

    def test_names_from_game_clients_are_escaped(self) -> None:
        """Group names come from players' game clients; they must never become markup."""
        self.database.write_live_state(LIVE_STATE)
        page = self.client.get("/live/state").get_data(as_text=True)
        self.assertIn("&lt;b&gt;Alice&#39;s game&lt;/b&gt;", page)
        self.assertNotIn("<b>Alice", page)

    def test_every_page_sends_the_security_headers(self) -> None:
        response = self.client.get("/")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")


class AdminLogonTest(WebInterfaceTestCase):
    ADMIN_PAGES = ("/accounts", "/clans", "/news", "/chat-themes", "/board", "/settings", "/announcements", "/games")

    def test_admin_pages_send_visitors_to_the_logon(self) -> None:
        for path in self.ADMIN_PAGES:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, HTTP_FOUND)
                self.assertIn(f"/logon?next={path}", response.headers["Location"])

    def test_admin_logs_on_with_name_and_code_in_any_case(self) -> None:
        response = self.log_on(name=ADMIN_NAME.lower(), access_code=ADMIN_CODE.lower())
        self.assertEqual(response.status_code, HTTP_FOUND)
        for path in self.ADMIN_PAGES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, HTTP_OK)

    def test_wrong_code_and_players_without_admin_rights_are_refused(self) -> None:
        self.assertEqual(self.log_on(access_code="WRONGCODE234").status_code, HTTP_UNAUTHORIZED)
        self.assertEqual(self.log_on(name=PLAYER_NAME, access_code=PLAYER_CODE).status_code, HTTP_UNAUTHORIZED)
        self.assertEqual(self.client.get("/accounts").status_code, HTTP_FOUND)

    def test_revoking_the_code_ends_the_session(self) -> None:
        self.log_on()
        self.database.revoke_access_code(self.admin.account_id, self.admin_code_id)
        self.assertEqual(self.client.get("/accounts").status_code, HTTP_FOUND)

    def test_losing_admin_rights_ends_the_session(self) -> None:
        self.log_on()
        self.database.set_admin(self.admin.account_id, False)
        self.assertEqual(self.client.get("/accounts").status_code, HTTP_FOUND)

    def test_repeated_failures_lock_out_even_the_right_code_for_a_while(self) -> None:
        for _ in range(LOGON_FAILURE_BURST):
            self.log_on(access_code="WRONGCODE234")
        self.assertEqual(self.log_on().status_code, HTTP_TOO_MANY_REQUESTS)

    def test_forms_without_the_session_token_are_refused(self) -> None:
        self.log_on()
        response = self.client.post("/clans", data={"name": "Romans"})
        self.assertEqual(response.status_code, HTTP_BAD_REQUEST)
        response = self.client.post("/clans", data={"name": "Romans", "csrf_token": "forged"})
        self.assertEqual(response.status_code, HTTP_BAD_REQUEST)
        self.assertIsNone(self.database.find_clan("Romans"))

    def test_logon_returns_only_to_pages_of_this_site(self) -> None:
        self.assertEqual(safe_next_path("/accounts/2"), "/accounts/2")
        for unsafe_path in ("//evil.example/", "https://evil.example/", "/\\evil.example", "accounts"):
            with self.subTest(path=unsafe_path):
                self.assertEqual(safe_next_path(unsafe_path), "")


class AccountPagesTest(WebInterfaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.log_on()

    def test_new_account_shows_its_code_once_and_the_code_works(self) -> None:
        page = self.post("/accounts", name="Carol", label="laptop").get_data(as_text=True)
        access_code = NEW_ACCESS_CODE_PATTERN.search(page).group(1)
        carol = self.database.find_account("Carol")
        self.assertTrue(self.database.check_access_code(carol.account_id, access_code))
        account_page = self.client.get(f"/accounts/{carol.account_id}").get_data(as_text=True)
        self.assertIn("laptop", account_page)
        self.assertNotIn(access_code, account_page)

    def test_invalid_name_is_refused_with_the_reason(self) -> None:
        response = self.post("/accounts", name="Player1")
        self.assertEqual(response.status_code, HTTP_FOUND)
        self.assertIsNone(self.database.find_account("Player1"))
        self.assertIn("not a valid player name", self.client.get("/accounts").get_data(as_text=True))

    def test_admin_cannot_lock_themselves_out(self) -> None:
        self.post(f"/accounts/{self.admin.account_id}/admin")
        self.post(f"/accounts/{self.admin.account_id}/ban", hours="24")
        self.post(f"/accounts/{self.admin.account_id}/delete", confirm_name=ADMIN_NAME)
        admin = self.database.find_account(ADMIN_NAME)
        self.assertTrue(admin.is_admin)
        self.assertIsNone(admin.banned_until)

    def test_ban_clan_and_removal_of_another_account(self) -> None:
        romans = self.database.create_clan("Romans")
        player_path = f"/accounts/{self.player.account_id}"
        self.post(f"{player_path}/clan", clan_id=str(romans.clan_id))
        self.assertEqual(self.database.find_clan_of_account(self.player.account_id), romans)
        self.post(f"{player_path}/ban", hours="2", reason="spam")
        self.assertIsNotNone(self.database.find_account(PLAYER_NAME).banned_until)

        self.post(f"{player_path}/delete", confirm_name="wrong")
        self.assertIsNotNone(self.database.find_account(PLAYER_NAME))
        self.post(f"{player_path}/delete", confirm_name=PLAYER_NAME)
        self.assertIsNone(self.database.find_account(PLAYER_NAME))


class ContentAndSettingsPagesTest(WebInterfaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.log_on()

    def test_news_keeps_line_breaks_without_carriage_returns(self) -> None:
        self.post("/news/german", title="Willkommen", body="Erste Zeile\r\nZweite Zeile\r\n")
        self.assertEqual(self.database.find_news("german").body, "Erste Zeile\nZweite Zeile")

    def test_text_the_game_cannot_show_is_refused(self) -> None:
        self.post("/chat-themes", name="Party \U0001f389")
        self.assertEqual(self.database.list_chat_themes(), [])
        self.assertIn("which the game cannot show", self.client.get("/chat-themes").get_data(as_text=True))

    def test_board_theme_and_message(self) -> None:
        self.post("/board/themes", language="English", name="Tournaments")
        [theme] = self.database.list_board_themes()
        self.post("/board/messages", theme_id=str(theme.theme_id), title="Cup", body="Saturday", author=ADMIN_NAME)
        [message] = self.database.list_board_messages(theme.theme_id)
        self.assertEqual((message.title, message.author), ("Cup", ADMIN_NAME))

    def test_one_invalid_setting_saves_none(self) -> None:
        self.post(
            "/settings",
            max_players="20",
            welcome_message="Hello",
            banner_url="http://not-ftp.example/banner.png",
            banner_id="0",
            banner_sub_id="0",
        )
        self.assertEqual(self.database.get_setting(Setting.MAX_PLAYERS), "0")
        self.post("/settings", max_players="20", welcome_message="Hello", banner_url="", banner_id="1", banner_sub_id="0")
        self.assertEqual(self.database.get_integer_setting(Setting.MAX_PLAYERS), 20)
        self.assertFalse(self.database.get_boolean_setting(Setting.UNIQUE_SERIAL_NUMBERS))

    def test_announcement_is_queued_for_the_server(self) -> None:
        self.post("/announcements", text="Server restart at 22:00")
        self.assertEqual(self.database.take_pending_announcements(), ["Server restart at 22:00"])

    def test_match_report_downloads_as_stored(self) -> None:
        self.database.store_game_report(self.player.account_id, FIRST_GAME_GROUP_ID, b"\x01\x02\x03")
        [report] = self.database.list_recent_game_reports(limit=1)
        self.assertIn(PLAYER_NAME, self.client.get("/games").get_data(as_text=True))
        response = self.client.get(f"/games/reports/{report.report_id}")
        self.assertEqual(response.data, b"\x01\x02\x03")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("unreadable", self.client.get("/games").get_data(as_text=True))

    def test_match_report_shows_map_outcome_and_who_played_what(self) -> None:
        """A slot's lobby player id is the player's account id; a computer opponent has none."""
        players = (
            MatchReportPlayer(flags=0, lobby_player_id=self.player.account_id, race=Race.EGYPTIANS, unused=0, slot=0, team=1),
            MatchReportPlayer(flags=MatchPlayerFlag.COMPUTER, lobby_player_id=0, race=Race.ROMANS, unused=0, slot=1, team=2),
        )
        report = GameStatus(FIRST_GAME_GROUP_ID, GameStatusFlag.LADDER_GAME, 1, 40_000, "Elbe", players)
        self.database.store_game_report(self.player.account_id, FIRST_GAME_GROUP_ID, report.encode().body)
        start = GameStatus(FIRST_GAME_GROUP_ID, 0, NO_WINNING_TEAM, 0, "Elbe", players)
        self.database.store_game_report(self.player.account_id, FIRST_GAME_GROUP_ID, start.encode().body)
        page = self.client.get("/games").get_data(as_text=True)
        self.assertIn("Team 1 won", page)
        self.assertIn("Match started", page)
        self.assertIn(f"Team 1: {PLAYER_NAME} (Egyptians)", page)
        self.assertIn("Team 2: Computer (Romans)", page)
        self.assertIn("ladder, unranked", page)


if __name__ == "__main__":
    unittest.main()
