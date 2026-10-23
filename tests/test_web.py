"""The web interface: the public live lobby, the admin logon and its protections, and the admin
pages writing to the same database the server reads.

Skipped where Flask is not installed: the lobby server itself does not need it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

from s3lobby.constants import (
    DEVELOPMENT_PASSWORD,
    FIRST_GAME_GROUP_ID,
    LOGON_FAILURE_BURST,
    NO_WINNING_TEAM,
    GameStatusFlag,
    GroupFlag,
    LadderType,
    MatchPlayerFlag,
    MessageType,
    Race,
)
from s3lobby.database import LobbyDatabase, Setting
from s3lobby.groups import GroupKind
from s3lobby.live import LiveGroup, LiveLobbyState, LivePlayer
from s3lobby.messages import GameBlock, GameDescription, GameStatus, GroupAnnouncement, MatchReportPlayer
from tests.support import game_block_remainder

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
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
NEW_ACCESS_CODE_PATTERN = re.compile(r'<div class="access-code">([A-Z0-9]{12})</div>')

LIVE_STATE = LiveLobbyState(
    server_name="Test Lobby",
    players=(
        LivePlayer(
            player_id=2,
            name=PLAYER_NAME,
            clan="Romans",
            is_admin=False,
            address="192.0.2.7:2300",
            logged_on_at="2026-10-23T12:00:00+00:00",
            group_ids=(3, 30, FIRST_GAME_GROUP_ID),
            reachability="blocked",
        ),
    ),
    groups=(
        LiveGroup(group_id=3, kind=GroupKind.ROOM.value, name="GameZone", max_players=0, is_full=False,
                  has_password=False, member_names=(PLAYER_NAME,)),
        LiveGroup(group_id=30, kind=GroupKind.ROOM.value, name="League 1 vs 1", max_players=0, is_full=False,
                  has_password=False, member_names=(PLAYER_NAME,)),
        LiveGroup(group_id=FIRST_GAME_GROUP_ID, kind=GroupKind.GAME.value, name="<b>Alice's game</b>",
                  max_players=4, is_full=False, has_password=False, member_names=(PLAYER_NAME,),
                  listed_in_ids=(30,), map_name="\\Random", team_count=2, is_ladder_game=True),
    ),
)


@unittest.skipUnless(IS_FLASK_INSTALLED, "the web interface needs Flask: pip install .[web]")
class WebInterfaceTestCase(unittest.TestCase):
    """A web application on a fresh database with one admin and one player account."""

    def setUp(self) -> None:
        """Create the database with the admin and the player, the application on it, and a test client whose
        session holds the form token."""
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
        """Post a form to path with the session's form token added."""
        return self.client.post(path, data={"csrf_token": CSRF_TOKEN, **form})

    def log_on(self, name: str = ADMIN_NAME, access_code: str = ADMIN_CODE):
        """Log on through the logon form, by default as the admin; returns the response."""
        response = self.post("/logon", name=name, access_code=access_code)
        # A logon starts a fresh session; put the test's form token back into it.
        with self.client.session_transaction() as session:
            session["csrf_token"] = CSRF_TOKEN
        return response


class LiveLobbyTest(WebInterfaceTestCase):
    """The public live lobby page and what it shows to visitors and to admins."""

    def test_without_a_server_the_lobby_says_so(self) -> None:
        """Without a live state from a running server the page says the server is not running instead of
        showing an empty lobby."""
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("The lobby server is not running", page)

    def test_visitors_see_players_and_games_but_no_addresses(self) -> None:
        """Visitors see who is online and in which rooms, but never a player's address."""
        self.database.write_live_state(LIVE_STATE)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Running", page)
        self.assertIn(PLAYER_NAME, page)
        self.assertIn("League 1 vs 1", page)
        self.assertNotIn("192.0.2.7", page)

    def test_games_show_their_map_teams_and_that_a_ladder_game_is_a_ladder_game(self) -> None:
        """A game shows its map, with a random map named so, and a ladder game is marked as one."""
        self.database.write_live_state(LIVE_STATE)
        page = self.client.get("/live/state").get_data(as_text=True)
        self.assertIn("Random map", page)
        self.assertIn(">ladder</span>", page)

    def test_admins_see_addresses_in_the_admin_area_only(self) -> None:
        """Admins see each player's address and whether the reachability probe found them blocked, to help
        players with their network; on their own lobby page, while the public one stays the same for
        everyone, logged on or not."""
        self.database.write_live_state(LIVE_STATE)
        self.log_on()
        for path in ("/admin/lobby", "/admin/lobby/state"):
            with self.subTest(path=path):
                page = self.client.get(path).get_data(as_text=True)
                self.assertIn("192.0.2.7:2300", page)
                self.assertIn("blocked", page, "admins see whether players can be reached")
        public = self.client.get("/live/state").get_data(as_text=True)
        self.assertNotIn("192.0.2.7", public)

    def test_the_admin_area_needs_a_logon_and_opens_on_its_start_page(self) -> None:
        """Visitors are sent to the logon; an admin who logs on lands on the admin start page."""
        self.assertEqual(self.client.get("/admin/").status_code, HTTP_FOUND)
        response = self.log_on()
        self.assertTrue(response.headers["Location"].endswith("/admin/"))
        page = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("Players online", page)
        self.assertIn("Public site", page)

    def test_names_from_game_clients_are_escaped(self) -> None:
        """Group names come from players' game clients; they must never become markup."""
        self.database.write_live_state(LIVE_STATE)
        page = self.client.get("/live/state").get_data(as_text=True)
        self.assertIn("&lt;b&gt;Alice&#39;s game&lt;/b&gt;", page)
        self.assertNotIn("<b>Alice", page)

    def test_every_page_sends_the_security_headers(self) -> None:
        """Every response carries the Content-Security-Policy without inline script, nosniff, and a ban on
        framing."""
        response = self.client.get("/")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")


class AdminLogonTest(WebInterfaceTestCase):
    """The admin logon, the sessions it starts and the protections around both."""

    ADMIN_PAGES = ("/admin/accounts", "/admin/clans", "/admin/news", "/admin/chat-themes", "/admin/board", "/admin/settings", "/admin/announcements", "/admin/games")

    def test_admin_pages_send_visitors_to_the_logon(self) -> None:
        """Every admin page sends a visitor to the logon, with the page to come back to afterwards."""
        for path in self.ADMIN_PAGES:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, HTTP_FOUND)
                self.assertIn(f"/logon?next={path}", response.headers["Location"])

    def test_admin_logs_on_with_name_and_code_in_any_case(self) -> None:
        """An admin logs on with name and access code in any case, as in the game, and then reaches every
        admin page."""
        response = self.log_on(name=ADMIN_NAME.lower(), access_code=ADMIN_CODE.lower())
        self.assertEqual(response.status_code, HTTP_FOUND)
        for path in self.ADMIN_PAGES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, HTTP_OK)

    def test_wrong_code_and_players_without_admin_rights_are_refused(self) -> None:
        """A wrong code and a player without admin rights are both refused, and no session is left behind."""
        self.assertEqual(self.log_on(access_code="WRONGCODE234").status_code, HTTP_UNAUTHORIZED)
        self.assertEqual(self.log_on(name=PLAYER_NAME, access_code=PLAYER_CODE).status_code, HTTP_UNAUTHORIZED)
        self.assertEqual(self.client.get("/admin/accounts").status_code, HTTP_FOUND)

    def test_revoking_the_code_ends_the_session(self) -> None:
        """A session ends as soon as the access code it logged on with is revoked, so revoking a leaked code
        also throws out whoever used it."""
        self.log_on()
        self.database.revoke_access_code(self.admin.account_id, self.admin_code_id)
        self.assertEqual(self.client.get("/admin/accounts").status_code, HTTP_FOUND)

    def test_losing_admin_rights_ends_the_session(self) -> None:
        """A session ends as soon as its account loses admin rights, not only at its next logon."""
        self.log_on()
        self.database.set_admin(self.admin.account_id, False)
        self.assertEqual(self.client.get("/admin/accounts").status_code, HTTP_FOUND)

    def test_repeated_failures_lock_out_even_the_right_code_for_a_while(self) -> None:
        """After LOGON_FAILURE_BURST failures from one address even the right code is refused for a while, so
        access codes cannot be guessed through the web interface."""
        for _ in range(LOGON_FAILURE_BURST):
            self.log_on(access_code="WRONGCODE234")
        self.assertEqual(self.log_on().status_code, HTTP_TOO_MANY_REQUESTS)

    def test_forms_without_the_session_token_are_refused(self) -> None:
        """A form posted without the session's token, or with a forged one, is refused and changes nothing, so
        another site cannot make an admin's browser change the lobby."""
        self.log_on()
        response = self.client.post("/admin/clans", data={"name": "Romans"})
        self.assertEqual(response.status_code, HTTP_BAD_REQUEST)
        response = self.client.post("/admin/clans", data={"name": "Romans", "csrf_token": "forged"})
        self.assertEqual(response.status_code, HTTP_BAD_REQUEST)
        self.assertIsNone(self.database.find_clan("Romans"))

    def test_logon_returns_only_to_pages_of_this_site(self) -> None:
        """Browsers drop tabs and line breaks from a URL, so "/<tab>/evil.example" leads there."""
        self.assertEqual(safe_next_path("/admin/accounts/2"), "/admin/accounts/2")
        unsafe_paths = (
            "//evil.example/",
            "https://evil.example/",
            "/\\evil.example",
            "accounts",
            "/\t/evil.example",
            "/\r/evil.example",
            "/\n/evil.example",
            "/\x7f/evil.example",
        )
        for unsafe_path in unsafe_paths:
            with self.subTest(path=unsafe_path):
                self.assertEqual(safe_next_path(unsafe_path), "")

    def test_failures_from_strangers_do_not_lock_the_admin_out(self) -> None:
        """Admins' names are no secret; failing with one on purpose must not keep them out."""
        for attempt in range(LOGON_FAILURE_BURST + 1):
            self.client.post(
                "/logon",
                data={"csrf_token": CSRF_TOKEN, "name": ADMIN_NAME, "access_code": "WRONGCODE234"},
                environ_base={"REMOTE_ADDR": f"10.0.0.{attempt + 1}"},
            )
        self.assertEqual(self.log_on().status_code, HTTP_FOUND)

    def test_logout_ends_the_session_also_for_a_copied_cookie(self) -> None:
        """Logging out ends the session on the server side, so a copy of the cookie taken before no longer
        works."""
        self.log_on()
        with self.client.session_transaction() as session:
            copied_session = dict(session)
        self.post("/logout")
        with self.client.session_transaction() as session:
            session.update(copied_session)
        self.assertEqual(self.client.get("/admin/accounts").status_code, HTTP_FOUND, "back to the logon page")

    def test_session_ends_after_its_lifetime_however_busy_it_was(self) -> None:
        """A session ends once its lifetime from the logon is over, even while it is in use, so a stolen
        session cannot be kept alive forever."""
        self.log_on()
        self.assertEqual(self.client.get("/admin/accounts").status_code, HTTP_OK)
        self.database._backend.execute("UPDATE web_sessions SET created_at = '2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.client.get("/admin/accounts").status_code, HTTP_FOUND)

    def test_forged_token_with_other_characters_is_refused_not_a_server_error(self) -> None:
        """A form token with characters outside ASCII is refused like any wrong token, not answered with a
        server error."""
        response = self.client.post("/admin/clans", data={"name": "Romans", "csrf_token": "\u00e9t\u00e9"})
        self.assertEqual(response.status_code, HTTP_BAD_REQUEST)

    def test_pages_are_not_cached_and_send_the_isolation_headers(self) -> None:
        """Pages are not cached and isolated from other windows; Strict-Transport-Security is only sent over
        HTTPS, where it belongs."""
        response = self.client.get("/")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertNotIn("Strict-Transport-Security", response.headers, "only with HTTPS")


class DevelopmentLogonTest(WebInterfaceTestCase):
    """`s3lobby web --dev`: admins also log on with DEVELOPMENT_PASSWORD, for testing on one machine."""

    def setUp(self) -> None:
        """The same database, but the application in development mode."""
        super().setUp()
        database_path = str(Path(self.database_folder.name) / "lobby.sqlite3")
        self.app = create_app(database_path, secret_key="test secret", is_development_mode=True)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf_token"] = CSRF_TOKEN

    def log_on_from(self, address: str, name: str = ADMIN_NAME):
        """Log on with DEVELOPMENT_PASSWORD from the given address, by default as the admin; returns the
        response."""
        response = self.client.post(
            "/logon",
            data={"csrf_token": CSRF_TOKEN, "name": name, "access_code": DEVELOPMENT_PASSWORD},
            environ_base={"REMOTE_ADDR": address},
        )
        # A logon starts a fresh session; put the test's form token back into it.
        with self.client.session_transaction() as session:
            session["csrf_token"] = CSRF_TOKEN
        return response

    def test_admin_logs_on_with_the_development_password_from_this_machine_and_the_lan(self) -> None:
        """In development mode an admin gets in with DEVELOPMENT_PASSWORD from this machine and from the LAN.
        """
        for address in ("127.0.0.1", "192.168.1.20"):
            with self.subTest(address=address):
                self.assertEqual(self.log_on_from(address).status_code, HTTP_FOUND)

    def test_development_password_is_refused_from_the_internet_and_for_players(self) -> None:
        """A password everybody knows must never open the web interface to the internet, nor give a player
        admin pages."""
        self.assertEqual(self.log_on_from("8.8.8.8").status_code, HTTP_UNAUTHORIZED)
        self.assertEqual(self.log_on_from("127.0.0.1", name=PLAYER_NAME).status_code, HTTP_UNAUTHORIZED)

    def test_session_ends_when_the_code_it_stands_for_is_revoked(self) -> None:
        """A web session names the access code it logged on with; this one stands for the newest."""
        self.log_on_from("127.0.0.1")
        self.assertEqual(self.client.get("/admin/accounts", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code, HTTP_OK)
        self.database.revoke_access_code(self.admin.account_id, self.admin_code_id)
        self.assertEqual(self.client.get("/admin/accounts").status_code, HTTP_FOUND)

    def test_without_development_mode_the_password_is_just_wrong(self) -> None:
        """Outside development mode DEVELOPMENT_PASSWORD is just a wrong code."""
        database_path = str(Path(self.database_folder.name) / "lobby.sqlite3")
        self.client = create_app(database_path, secret_key="test secret").test_client()
        with self.client.session_transaction() as session:
            session["csrf_token"] = CSRF_TOKEN
        self.assertEqual(self.log_on_from("127.0.0.1").status_code, HTTP_UNAUTHORIZED)


class ClanHighlightPageTest(WebInterfaceTestCase):
    """Switching a clan's green highlight on the clans page."""

    def test_clan_is_switched_green_and_back(self) -> None:
        """The clans page switches a clan's highlight on and off and marks a highlighted clan as green."""
        self.log_on()
        romans = self.database.create_clan("Romans")
        self.post(f"/admin/clans/{romans.clan_id}/highlight", is_highlighted="on")
        self.assertTrue(self.database.find_clan("Romans").is_highlighted)
        self.assertIn('<span class="badge badge--good">green</span>', self.client.get("/admin/clans").get_data(as_text=True))
        self.post(f"/admin/clans/{romans.clan_id}/highlight")
        self.assertFalse(self.database.find_clan("Romans").is_highlighted)


class GameColoursTest(WebInterfaceTestCase):
    """Names show in the colour the game's member lists draw them in: green, or blue for the
    clan BB (0x449da1, 0x449de2)."""

    def test_live_lobby_shows_the_colour_the_server_reports(self) -> None:
        """The live lobby draws a player in the row colour the server reports, as the game shows them."""
        blue_player = dataclasses.replace(LIVE_STATE.players[0], row_colour="blue")
        self.database.write_live_state(dataclasses.replace(LIVE_STATE, players=(blue_player,)))
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('<span class="game-row game-row--blue"', page)

    def test_admin_is_green_and_a_marked_clan_named_bb_blue(self) -> None:
        """The admin pages show admins in green and a highlighted clan named BB in blue, matching the game's
        member lists."""
        self.log_on()
        accounts = self.client.get("/admin/accounts").get_data(as_text=True)
        self.assertIn(f'game-row--green" title="green in the game\'s member lists">{ADMIN_NAME}</span>', accounts)
        blue_byte = self.database.create_clan("BB")
        self.database.set_clan_highlighted(blue_byte.clan_id, True)
        clans = self.client.get("/admin/clans").get_data(as_text=True)
        self.assertIn('game-row--blue" title="blue in the game\'s member lists">BB</span>', clans)


class AccountPagesTest(WebInterfaceTestCase):
    """The account pages: new accounts, bans, clans, deletion and kicks."""

    def setUp(self) -> None:
        """Log on as the admin."""
        super().setUp()
        self.log_on()

    def test_new_account_shows_its_code_once_and_the_code_works(self) -> None:
        """A new account's access code is shown once on the page that creates it and works for a logon; the
        account page later shows its label but never the code."""
        page = self.post("/admin/accounts", name="Carol", label="laptop").get_data(as_text=True)
        access_code = NEW_ACCESS_CODE_PATTERN.search(page).group(1)
        carol = self.database.find_account("Carol")
        self.assertTrue(self.database.check_access_code(carol.account_id, access_code))
        account_page = self.client.get(f"/admin/accounts/{carol.account_id}").get_data(as_text=True)
        self.assertIn("laptop", account_page)
        self.assertNotIn(access_code, account_page)

    def test_invalid_name_is_refused_with_the_reason(self) -> None:
        """A name the game would not accept creates no account, and the page says why."""
        response = self.post("/admin/accounts", name="Player1")
        self.assertEqual(response.status_code, HTTP_FOUND)
        self.assertIsNone(self.database.find_account("Player1"))
        self.assertIn("not a valid player name", self.client.get("/admin/accounts").get_data(as_text=True))

    def test_admin_cannot_lock_themselves_out(self) -> None:
        """An admin cannot take away their own rights, ban or delete themselves, which could leave the lobby
        without an admin."""
        self.post(f"/admin/accounts/{self.admin.account_id}/admin")
        self.post(f"/admin/accounts/{self.admin.account_id}/ban", hours="24")
        self.post(f"/admin/accounts/{self.admin.account_id}/delete", confirm_name=ADMIN_NAME)
        admin = self.database.find_account(ADMIN_NAME)
        self.assertTrue(admin.is_admin)
        self.assertIsNone(admin.banned_until)

    def test_ban_clan_and_removal_of_another_account(self) -> None:
        """An admin sets another account's clan and ban, and deleting it needs its name typed again: a wrong
        name deletes nothing."""
        romans = self.database.create_clan("Romans")
        player_path = f"/admin/accounts/{self.player.account_id}"
        self.post(f"{player_path}/clan", clan_id=str(romans.clan_id))
        self.assertEqual(self.database.find_clan_of_account(self.player.account_id), romans)
        self.post(f"{player_path}/ban", hours="2", reason="spam")
        self.assertIsNotNone(self.database.find_account(PLAYER_NAME).banned_until)

        self.post(f"{player_path}/delete", confirm_name="wrong")
        self.assertIsNotNone(self.database.find_account(PLAYER_NAME))
        self.post(f"{player_path}/delete", confirm_name=PLAYER_NAME)
        self.assertIsNone(self.database.find_account(PLAYER_NAME))

    def test_player_is_removed_from_the_running_lobby_but_not_the_admin(self) -> None:
        """The web interface shares only the database with the server: it queues the kick."""
        self.post(f"/admin/accounts/{self.admin.account_id}/kick")
        self.assertEqual(self.database.take_pending_kicks(), [])
        self.post(f"/admin/accounts/{self.player.account_id}/kick")
        [kick] = self.database.take_pending_kicks()
        self.assertEqual(kick.account_id, self.player.account_id)


class ContentAndSettingsPagesTest(WebInterfaceTestCase):
    """The pages for news, chat themes, the board, settings, announcements and the games list."""

    def setUp(self) -> None:
        """Log on as the admin."""
        super().setUp()
        self.log_on()

    def test_news_keeps_line_breaks_without_carriage_returns(self) -> None:
        """Browsers send line breaks as CR LF; the news keeps the lines but stores plain line breaks, which
        the server splits into the game's text lines."""
        self.post("/admin/news/german", title="Willkommen", body="Erste Zeile\r\nZweite Zeile\r\n")
        self.assertEqual(self.database.find_news("german").body, "Erste Zeile\nZweite Zeile")

    def test_text_the_game_cannot_show_is_refused(self) -> None:
        """A chat theme with a character outside the game's code page is refused, and the page says why."""
        self.post("/admin/chat-themes", name="Party \U0001f389")
        self.assertEqual(self.database.list_chat_themes(), [])
        self.assertIn("which the game cannot show", self.client.get("/admin/chat-themes").get_data(as_text=True))

    def test_board_theme_and_message(self) -> None:
        """A board theme is created and a message posted to it from the web interface."""
        self.post("/admin/board/themes", language="English", name="Tournaments")
        [theme] = self.database.list_board_themes()
        self.post("/admin/board/messages", theme_id=str(theme.theme_id), title="Cup", body="Saturday", author=ADMIN_NAME)
        [message] = self.database.list_board_messages(theme.theme_id)
        self.assertEqual((message.title, message.author), ("Cup", ADMIN_NAME))

    def test_one_invalid_setting_saves_none(self) -> None:
        """A settings form with one invalid value saves none of its values; a valid form saves them all, and
        an unticked box saves as off."""
        self.post(
            "/admin/settings",
            max_players="20",
            welcome_message="Hello",
            banner_url="http://not-ftp.example/banner.png",
            banner_id="0",
            banner_sub_id="0",
        )
        self.assertEqual(self.database.get_setting(Setting.MAX_PLAYERS), "0")
        self.post("/admin/settings", max_players="20", welcome_message="Hello", banner_url="", banner_id="1", banner_sub_id="0")
        self.assertEqual(self.database.get_integer_setting(Setting.MAX_PLAYERS), 20)
        self.assertFalse(self.database.get_boolean_setting(Setting.UNIQUE_SERIAL_NUMBERS))

    def test_setting_set_to_yes_elsewhere_shows_as_ticked(self) -> None:
        """Otherwise the next save of the page would quietly turn it off."""
        self.database.set_setting(Setting.MARK_ADMINS, "yes")
        self.assertIn('name="mark_admins" checked', self.client.get("/admin/settings").get_data(as_text=True))

    def test_announcement_is_queued_for_the_running_server(self) -> None:
        """With a server running, an announcement is queued in the database, where the server picks it up."""
        self.database.write_live_state(LIVE_STATE)
        self.post("/admin/announcements", text="Server restart at 22:00")
        self.assertEqual(self.database.take_pending_announcements(), ["Server restart at 22:00"])

    def test_announcement_without_a_running_server_is_not_queued(self) -> None:
        """The server drops lines queued while it was stopped, so queueing one would only pretend."""
        self.post("/admin/announcements", text="Server restart at 22:00")
        self.assertEqual(self.database.take_pending_announcements(), [])

    def test_match_report_downloads_as_stored(self) -> None:
        """A game report downloads byte for byte as an attachment, and one that does not decode is listed as
        unreadable rather than breaking the games page."""
        self.database.store_game_report(self.player.account_id, FIRST_GAME_GROUP_ID, b"\x01\x02\x03")
        [report] = self.database.list_recent_game_reports(limit=1)
        self.assertIn(PLAYER_NAME, self.client.get("/admin/games").get_data(as_text=True))
        response = self.client.get(f"/admin/games/reports/{report.report_id}")
        self.assertEqual(response.data, b"\x01\x02\x03")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("unreadable", self.client.get("/admin/games").get_data(as_text=True))

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
        page = self.client.get("/admin/games").get_data(as_text=True)
        self.assertIn("Team 1 won", page)
        self.assertIn("No winner yet", page)
        self.assertIn(f"Team 1, slot 0: {PLAYER_NAME} (Egyptians)", page)
        self.assertIn("Team 2, slot 1: Computer (Romans)", page)
        self.assertIn("ladder game, unranked here", page)
        self.assertIn("42 min 40 s", page)

    def test_games_list_shows_type_map_players_and_result(self) -> None:
        """Everything the lobby kept of a game: the host's setup from its announcement, who joined,
        and the reports of its match, linked to it."""
        game_block = GameBlock(("0.0.0.0", 0), game_block_remainder(LadderType.CLANS))
        description = GameDescription(team_count=2, computer_opponent_count=1, is_ladder_game=True, map_name="\\Random")
        announcement = GroupAnnouncement(
            MessageType.ADD_GROUP_TO_GROUP_2,
            30,
            FIRST_GAME_GROUP_ID,
            GroupFlag.HAS_GAME_BLOCK | GroupFlag.HAS_DESCRIPTION,
            2,
            "Alice's game",
            "",
            "",
            game_block=game_block,
            description=description.encode(),
        )
        game_id = self.database.record_game_opened(
            "Alice's game", self.player.account_id, FIRST_GAME_GROUP_ID, announcement.encode().body
        )
        self.database.record_game_player(game_id, self.player.account_id)
        self.database.record_game_player(game_id, self.admin.account_id)
        players = (MatchReportPlayer(flags=0, lobby_player_id=self.player.account_id, race=Race.AMAZONS, unused=0, slot=0, team=0),)
        report = GameStatus(FIRST_GAME_GROUP_ID, GameStatusFlag.LADDER_GAME, 0, 68, "\\Random", players)
        self.database.store_game_report(self.player.account_id, FIRST_GAME_GROUP_ID, report.encode().body, game_id)
        self.database.record_game_closed(game_id)

        page = self.client.get("/admin/games").get_data(as_text=True)
        for expected in (
            "<td>Clanliga</td>",
            "<td>Random map</td>",
            f"<td>{PLAYER_NAME}, {ADMIN_NAME}</td>",
            "Team 0 won",
            f'href="/admin/games/{game_id}"',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, page)


class GameDetailPageTest(WebInterfaceTestCase):
    """/admin/games/ID: everything the lobby kept of one game."""

    def setUp(self) -> None:
        """Log on as the admin."""
        super().setUp()
        self.log_on()

    def record_two_player_match(self) -> int:
        """A match like the first one seen with two players: both raced at random, team 0 won."""
        game_block = GameBlock(("0.0.0.0", 0), game_block_remainder(LadderType.NONE))
        description = GameDescription(team_count=2, computer_opponent_count=0, is_ladder_game=False, map_name="\\Random")
        announcement = GroupAnnouncement(
            MessageType.ADD_GROUP_TO_GROUP_2,
            30,
            FIRST_GAME_GROUP_ID,
            GroupFlag.HAS_GAME_BLOCK | GroupFlag.HAS_DESCRIPTION,
            2,
            "Bob's game",
            "",
            "",
            game_block=game_block,
            description=description.encode(),
        )
        game_id = self.database.record_game_opened("Bob's game", self.admin.account_id, FIRST_GAME_GROUP_ID, announcement.encode().body)
        self.database.record_game_player(game_id, self.admin.account_id)
        self.database.record_game_player(game_id, self.player.account_id)
        slots = (
            MatchReportPlayer(MatchPlayerFlag.RANDOM_RACE, self.admin.account_id, Race.EGYPTIANS, 0, 0, 0),
            MatchReportPlayer(MatchPlayerFlag.RANDOM_RACE, self.player.account_id, Race.ROMANS, 0, 1, 1),
        )
        start = GameStatus(FIRST_GAME_GROUP_ID, 0, NO_WINNING_TEAM, 0, "\\Random", slots)
        end = GameStatus(FIRST_GAME_GROUP_ID, 0, 0, 107, "\\Random", slots)
        for status in (start, end):
            self.database.store_game_report(self.admin.account_id, FIRST_GAME_GROUP_ID, status.encode().body, game_id)
        self.database.record_game_closed(game_id)
        return game_id

    def test_game_page_shows_the_match_teams_setup_and_players(self) -> None:
        """The game page shows the winner, the winning team, the playing time, every player linked to their
        account with their id, the random race and the kind of game, and a download for each report."""
        game_id = self.record_two_player_match()
        page = self.client.get(f"/admin/games/{game_id}").get_data(as_text=True)
        for expected in (
            f"won by {ADMIN_NAME}",
            "Team 0 won",
            "after 6 s of play",
            f'<a href="/admin/accounts/{self.player.account_id}">{PLAYER_NAME}</a>',
            f'<td class="num">{self.player.account_id}</td>',
            "race drawn at random",
            "Normal game",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, page)
        self.assertEqual(page.count("Download"), 2)

    def test_deleted_account_keeps_its_place_without_a_link(self) -> None:
        """A game outlives its players' accounts; the page names the gap instead of a dead link."""
        game_id = self.record_two_player_match()
        self.database.delete_account(self.player.account_id)
        page = self.client.get(f"/admin/games/{game_id}").get_data(as_text=True)
        self.assertNotIn(f'href="/admin/accounts/{self.player.account_id}"', page)
        self.assertIn("(deleted account)", page)

    def test_account_page_lists_its_games_with_team_and_result(self) -> None:
        """An account page lists the games the account played in, with the race and whether it won or lost."""
        game_id = self.record_two_player_match()
        page = self.client.get(f"/admin/accounts/{self.player.account_id}").get_data(as_text=True)
        self.assertIn(f'href="/admin/games/{game_id}"', page)
        self.assertIn('<span class="badge badge--bad">lost</span>', page)
        self.assertIn("Romans", page)
        winner_page = self.client.get(f"/admin/accounts/{self.admin.account_id}").get_data(as_text=True)
        self.assertIn('<span class="badge badge--good">won</span>', winner_page)

    def test_unknown_game_is_not_found(self) -> None:
        """A game id that does not exist answers 404 instead of a server error."""
        self.assertEqual(self.client.get("/admin/games/999").status_code, HTTP_NOT_FOUND)

    def test_visitors_are_sent_to_the_logon(self) -> None:
        """Game pages are for admins only; a visitor is sent to the logon."""
        game_id = self.record_two_player_match()
        self.client.post("/logout", data={"csrf_token": CSRF_TOKEN})
        response = self.client.get(f"/admin/games/{game_id}")
        self.assertEqual(response.status_code, HTTP_FOUND)


if __name__ == "__main__":
    unittest.main()
