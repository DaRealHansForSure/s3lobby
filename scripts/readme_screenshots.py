"""Retake the README's screenshots of the web interface from a demo lobby with made-up players.

Usage, from the checkout, with Flask in .venv and Google Chrome installed:

    .venv/bin/python scripts/readme_screenshots.py

It fills build/readme-screenshots/web-demo.sqlite3 with made-up data (never the owner's
database), renders each page the README shows with Flask's test client in light and dark, takes
a screenshot of each with headless Chrome, crops the empty space below the page, and copies the
pictures into docs/images. Cropping needs Pillow; when the running Python has none, the script
crops with the system's python3 instead. Run it before every release and every merge into main
(CLAUDE.md).
"""

from __future__ import annotations

import importlib.util
import os
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# The checkout's root, so the script runs without PYTHONPATH.
REPOSITORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY))

from s3lobby.constants import (  # noqa: E402 (the import path is set up first)
    CHAT_THEME_GROUP_ID_BASE,
    CHAT_ZONE_GROUP_ID,
    FIRST_GAME_GROUP_ID,
    GAME_ZONE_GROUP_ID,
    NO_WINNING_TEAM,
    GameStatusFlag,
    GroupFlag,
    LadderState,
    LadderType,
    MatchPlayerFlag,
    MessageType,
    Race,
    SeasonCounts,
)
from s3lobby.database import LobbyDatabase, Setting, utc_now  # noqa: E402
from s3lobby.groups import GroupKind  # noqa: E402
from s3lobby.ladder import Evaluation, ResultPlayer  # noqa: E402
from s3lobby.live import LiveGroup, LiveLobbyState, LivePlayer  # noqa: E402
from s3lobby.messages import (  # noqa: E402
    GameBlock,
    GameDescription,
    GameStatus,
    GroupAnnouncement,
    MatchReportPlayer,
)
from tests.support import game_block_remainder, join_ladder_since  # noqa: E402

OUTPUT_FOLDER = REPOSITORY / "build" / "readme-screenshots"
IMAGES_FOLDER = REPOSITORY / "docs" / "images"
STATIC_FOLDER = REPOSITORY / "s3lobby" / "web" / "static"
DATABASE_NAME = "web-demo.sqlite3"
THEMES = ("light", "dark")
# The width the README's pictures have; the height is cut to the page afterwards.
SCREENSHOT_WIDTH = 1280
SCREENSHOT_HEIGHT = 4000
# Chrome waits this long for app.js (the bar of section links, the theme) before it takes the
# picture.
SCRIPT_TIME_BUDGET_MS = 2000
# Space left below a page's last line when cropping, and pages cut shorter: the Games page lists
# every report below its table, the README shows the table only.
BOTTOM_MARGIN = 48
HEIGHT_LIMITS = {"games": 1300}
# The demo: its players (the first is the admin who logs on), the random seed that picks who
# played whom, and how many days of ladder games it holds. The players carry the names of the
# people who made The Settlers III (Blue Byte, 1998), a small thank-you; only the last one, banned
# in the demo, is made up.
PLAYER_NAMES = (
    "VolkerW", "DirkR", "ThomasH", "TorstenH", "ThorstenW", "WolfgangW", "HaikoR", "Sanna",
)
DEMO_SEED = 3
DEMO_DAYS = 18
DEMO_START_DAYS_AGO = 20
LADDER_PLAYER_COUNT = 6
MATCH_MINUTES = 40
MATCH_TICKS = 40_000
LEAGUE_ZONE_ID = 30
MAP_NAME = "\\Random"
# The password every account has in development mode (s3lobby web --dev).
DEMO_PASSWORD = "1234"
SESSION_TOKEN = "demo"


@dataclass(frozen=True)
class Shot:
    """One README picture: its file name without theme and suffix, the page, and whether the
    admin takes it (the public pages are taken as a visitor)."""

    name: str
    path: str
    as_admin: bool


def main() -> None:
    """Fill the demo database, take every picture, crop them, and copy them into docs/images."""
    if "--crop" in sys.argv:
        crop_pictures(Path(sys.argv[-1]))
        return
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    database_path = OUTPUT_FOLDER / DATABASE_NAME
    accounts = fill_demo_database(database_path)
    take_pictures(database_path, readme_shots(accounts), accounts)
    crop_with_pillow(OUTPUT_FOLDER)
    # Only the pictures go into docs/images; the HTML and the database stay in build/.
    for picture in sorted(OUTPUT_FOLDER.glob("*.png")):
        shutil.copy2(picture, IMAGES_FOLDER / picture.name)
        print(IMAGES_FOLDER / picture.name)


def readme_shots(accounts: dict[str, int]) -> list[Shot]:
    """The pages the README shows, in its order."""
    return [
        Shot("live-lobby", "/", False),
        Shot("ladder", "/ladder", False),
        Shot("player", "/players/DirkR", False),
        Shot("admin-start", "/admin/", True),
        Shot("admin-lobby", "/admin/lobby", True),
        Shot("ladder-members", "/admin/ladder/members", True),
        Shot("board", "/admin/board", True),
        Shot("chat-themes", "/admin/chat-themes", True),
        Shot("games", "/admin/games", True),
        Shot("game-details", "/admin/games/1", True),
        Shot("account", f"/admin/accounts/{accounts['ThomasH']}", True),
        Shot("clans", "/admin/clans", True),
        Shot("info", "/admin/info", True),
    ]


def fill_demo_database(database_path: Path) -> dict[str, int]:
    """A fresh demo lobby: accounts, clans, content, three weeks of ladder games, a season and a
    running lobby. Returns each player's account id."""
    # A new file every run, so the pictures never show something left from an earlier one.
    for suffix in ("", "-wal", "-shm"):
        Path(str(database_path) + suffix).unlink(missing_ok=True)
    now = utc_now()
    clock = DemoClock(now - timedelta(days=DEMO_START_DAYS_AGO))
    database = LobbyDatabase(str(database_path), clock=clock)
    try:
        accounts = add_players_and_content(database, now)
        add_ladder_games(database, accounts, clock, now)
        # The season is created at the real time, as an admin would today.
        clock.now = now
        database.add_ladder_season("Autumn Cup", now - timedelta(days=10), now + timedelta(days=10), SeasonCounts.LIGA)
        database.write_live_state(demo_live_state(accounts, now))
    finally:
        database.close()
    return accounts


class DemoClock:
    """A clock the demo moves along, so games and results get believable times."""

    def __init__(self, start: datetime) -> None:
        """Start at a moment in the past."""
        self.now = start

    def __call__(self) -> datetime:
        """The moment the demo is at."""
        return self.now


def add_players_and_content(database: LobbyDatabase, now: datetime) -> dict[str, int]:
    """The accounts with their codes, ladders, clans and a ban, and the news, themes and board."""
    # The first player is the admin who logs on; every player has a code and joined the Liga long ago.
    accounts = {
        name: database.create_account(name, is_admin=name == PLAYER_NAMES[0]).account_id for name in PLAYER_NAMES
    }
    for account_id in accounts.values():
        database.issue_access_code(account_id, label="home PC")
    join_ladder_since(database, list(accounts.values()))
    # Clans: one plain, and BB highlighted, which the game draws in blue.
    romans = database.create_clan("Romans").clan_id
    blue_byte = database.create_clan("BB").clan_id
    database.set_clan_highlighted(blue_byte, True)
    database.set_clan_of_account(accounts["TorstenH"], romans)
    database.set_clan_of_account(accounts["ThomasH"], blue_byte)
    database.ban_account(accounts["Sanna"], now + timedelta(days=1), "spamming the chat")
    # What players read: news, a theme of the admins' own with the permanent groups on, the board.
    database.set_setting(Setting.PERMANENT_CHAT_GROUPS, "true")
    database.set_news("german", "Willkommen", "Willkommen in der LAN-Lobby!\nFreitag ab 20 Uhr: offener Abend.")
    database.set_news("other", "Welcome", "Welcome to the LAN lobby!\nFriday from 20:00: open evening.")
    database.add_chat_theme("Tactics", "Builds and openings")
    board = database.add_board_theme("English", "Tournaments", "Cups and dates")
    database.post_board_message(
        board.theme_id, "Autumn Cup", "Saturday from 14:00, sign up with VolkerW.", author="VolkerW"
    )
    server_theme = database.add_board_theme(
        "English", "Ladder", "Seasons and weekly summaries, written by the server", is_server_written=True
    )
    database.post_board_message(
        server_theme.theme_id,
        "Week of 5 Oct 2026 to 11 Oct 2026",
        "Standings (infinite season)\n1. DirkR 1097\n2. TorstenH 1076",
        is_by_server=True,
    )
    database.queue_announcement("Server restart at 22:00")
    return accounts


def add_ladder_games(database: LobbyDatabase, accounts: dict[str, int], clock: DemoClock, now: datetime) -> None:
    """One counted Liga match a day, between two of the first players, with both players' start
    and end reports, as the game sends them."""
    chooser = random.Random(DEMO_SEED)
    for day in range(DEMO_DAYS):
        # The game opens two hours before its day ends, and the match is decided after MATCH_MINUTES.
        clock.now = now - timedelta(days=DEMO_DAYS - day, hours=2)
        first, second = chooser.sample(PLAYER_NAMES[:LADDER_PLAYER_COUNT], 2)
        group_id = FIRST_GAME_GROUP_ID + day
        name = f"{first}'s game"
        game_id = database.record_game_opened(
            name, accounts[first], group_id, stored_announcement(group_id, name), LadderType.INDIVIDUAL
        )
        database.record_game_player(game_id, accounts[first], installation=1)
        database.record_game_player(game_id, accounts[second], installation=2)
        slots = (
            MatchReportPlayer(MatchPlayerFlag.RANDOM_RACE, accounts[first], Race.ROMANS, 0, 0, 0),
            MatchReportPlayer(0, accounts[second], Race.AMAZONS, 0, 1, 1),
        )
        # Both report the start, then, after the match, the first player's win.
        for reporter in (first, second):
            start = GameStatus(group_id, GameStatusFlag.LADDER_GAME, NO_WINNING_TEAM, 0, MAP_NAME, slots)
            database.store_game_report(accounts[reporter], group_id, start.encode().body, game_id)
        clock.now += timedelta(minutes=MATCH_MINUTES)
        for reporter in (first, second):
            end = GameStatus(group_id, GameStatusFlag.LADDER_GAME, 0, MATCH_TICKS, MAP_NAME, slots)
            database.store_game_report(accounts[reporter], group_id, end.encode().body, game_id)
        database.record_game_closed(game_id)
        # The result as the ladder stores it after evaluating those reports.
        database.store_ladder_result(
            Evaluation(
                game_id,
                LadderType.INDIVIDUAL,
                LadderState.COUNTED,
                winning_team=0,
                game_ticks=MATCH_TICKS,
                played_at=clock.now,
                players=(
                    ResultPlayer(accounts[first], None, 0, Race.ROMANS, 0, True, False),
                    ResultPlayer(accounts[second], None, 1, Race.AMAZONS, 1, False, False),
                ),
            )
        )


def stored_announcement(group_id: int, name: str) -> bytes:
    """A Liga game's announcement as the lobby stores it for the Games page."""
    description = GameDescription(2, 0, True, MAP_NAME)
    announcement = GroupAnnouncement(
        MessageType.ADD_GROUP_TO_GROUP_2,
        GAME_ZONE_GROUP_ID,
        group_id,
        GroupFlag.HAS_GAME_BLOCK | GroupFlag.HAS_DESCRIPTION,
        2,
        name,
        "",
        "",
        game_block=GameBlock(("0.0.0.0", 0), game_block_remainder(LadderType.INDIVIDUAL)),
        description=description.encode(),
    )
    return announcement.encode().body


def demo_live_state(accounts: dict[str, int], now: datetime) -> LiveLobbyState:
    """A running lobby as the server writes it: five players, an open Liga game in the League
    zone, a chat group in the Meeting Room, one player in the GameZone."""
    chat_group_id = FIRST_GAME_GROUP_ID + 100
    game_group_id = FIRST_GAME_GROUP_ID + 101
    # The Meeting Room is the first theme the database starts with.
    meeting_room = CHAT_THEME_GROUP_ID_BASE + 1
    online_since = now.isoformat()
    players = (
        LivePlayer(
            accounts["VolkerW"], "VolkerW", "", True, "192.168.1.20:3343", online_since,
            (game_group_id,), "open", "green",
        ),
        LivePlayer(accounts["DirkR"], "DirkR", "", False, "192.168.1.21:3343", online_since, (game_group_id,), "open", ""),
        LivePlayer(
            accounts["TorstenH"], "TorstenH", "Romans", False, "198.51.100.4:3343", online_since,
            (GAME_ZONE_GROUP_ID, LEAGUE_ZONE_ID), "open", "",
        ),
        LivePlayer(
            accounts["ThomasH"], "ThomasH", "BB", False, "203.0.113.9:3343", online_since,
            (chat_group_id,), "blocked", "blue",
        ),
        LivePlayer(accounts["ThorstenW"], "ThorstenW", "", False, "192.168.1.23:3343", online_since, (chat_group_id,), "open", ""),
    )
    groups = (
        LiveGroup(
            game_group_id, GroupKind.GAME.value, "VolkerW's game", 4, False, False, ("VolkerW", "DirkR"),
            (LEAGUE_ZONE_ID,), MAP_NAME, 2, True,
        ),
        LiveGroup(
            chat_group_id, GroupKind.CHAT_GROUP.value, "Tactics talk", 0, False, False,
            ("ThomasH", "ThorstenW"), (meeting_room,),
        ),
        LiveGroup(meeting_room, GroupKind.ROOM.value, "Meeting Room", 0, False, False, (), (CHAT_ZONE_GROUP_ID,)),
        LiveGroup(GAME_ZONE_GROUP_ID, GroupKind.ROOM.value, "GameZone", 0, False, False, ("TorstenH",)),
        LiveGroup(
            LEAGUE_ZONE_ID, GroupKind.ROOM.value, "League 1 vs 1", 0, False, False, ("TorstenH",), (GAME_ZONE_GROUP_ID,)
        ),
    )
    return LiveLobbyState(server_name="LAN Lobby", players=players, groups=groups)


def take_pictures(database_path: Path, shots: list[Shot], accounts: dict[str, int]) -> None:
    """Render each page with Flask's test client and take a picture of it in each theme."""
    # Imported here: only this step needs Flask.
    from s3lobby.web.app import create_app

    # The Info page shows the database's location as the app was given it. Opened by its bare file
    # name from inside the output folder, the pictures show no path of the machine that took them.
    os.chdir(database_path.parent)
    app = create_app(database_path.name, secret_key=SESSION_TOKEN, is_development_mode=True)
    visitor = app.test_client()
    admin = app.test_client()
    # The admin logs on with the development password, as `s3lobby web --dev` allows.
    with admin.session_transaction() as session:
        session["csrf_token"] = SESSION_TOKEN
    admin.post("/logon", data={"csrf_token": SESSION_TOKEN, "name": PLAYER_NAMES[0], "access_code": DEMO_PASSWORD})
    for shot in shots:
        # The web interface takes a live state older than LIVE_STATE_STALE_SECONDS for a stopped
        # server, and taking all the pictures lasts longer than that: written again before each
        # page, the demo's server always runs and its players stay online.
        refresh_live_state(database_path, accounts)
        client = admin if shot.as_admin else visitor
        response = client.get(shot.path)
        if response.status_code != 200:
            raise SystemExit(f"{shot.path} answered {response.status_code}")
        page = response.get_data(as_text=True)
        # The pictures are published: a page that shows the home folder of whoever takes them
        # (and so their user name) must never become one.
        if str(Path.home()) in page:
            raise SystemExit(f"{shot.path} shows the home folder {Path.home()}; not taking its picture")
        # The page with its styles and scripts read from the checkout instead of the server.
        page = page.replace('href="/static/', f'href="file://{STATIC_FOLDER}/')
        page = page.replace('src="/static/', f'src="file://{STATIC_FOLDER}/')
        for theme in THEMES:
            html_file = OUTPUT_FOLDER / f"{shot.name}-{theme}.html"
            html_file.write_text(page.replace("<html", f'<html data-theme="{theme}"', 1))
            picture = OUTPUT_FOLDER / f"{shot.name}-{theme}.png"
            subprocess.run(
                [
                    "google-chrome",
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    f"--virtual-time-budget={SCRIPT_TIME_BUDGET_MS}",
                    f"--window-size={SCREENSHOT_WIDTH},{SCREENSHOT_HEIGHT}",
                    f"--screenshot={picture}",
                    f"file://{html_file}",
                ],
                check=True,
                capture_output=True,
            )


def refresh_live_state(database_path: Path, accounts: dict[str, int]) -> None:
    """Write the demo's running lobby again, as a running server rewrites it every few seconds."""
    database = LobbyDatabase(str(database_path))
    try:
        database.write_live_state(demo_live_state(accounts, utc_now()))
    finally:
        database.close()


def crop_with_pillow(folder: Path) -> None:
    """Crop the pictures here, or with the system's python3 when this Python has no Pillow (the
    project's .venv has Flask, not Pillow)."""
    if importlib.util.find_spec("PIL") is None:
        subprocess.run(["python3", __file__, "--crop", str(folder)], check=True)
        return
    crop_pictures(folder)


def crop_pictures(folder: Path) -> None:
    """Cut each picture below the page's last line, plus a margin."""
    from PIL import Image, ImageChops

    for picture in sorted(folder.glob("*.png")):
        name = picture.stem.rsplit("-", 1)[0]
        image = Image.open(picture).convert("RGB")
        width, height = image.size
        # The page ends where it no longer differs from the background in the bottom corner.
        background = Image.new("RGB", image.size, image.getpixel((width - 1, height - 1)))
        box = ImageChops.difference(image, background).getbbox()
        content_bottom = box[3] if box else height
        bottom = min(height, content_bottom + BOTTOM_MARGIN, HEIGHT_LIMITS.get(name, height))
        image.crop((0, 0, width, bottom)).save(picture, optimize=True)


if __name__ == "__main__":
    main()
