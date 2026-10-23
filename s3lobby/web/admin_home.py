"""The admin area's start page and its view of the live lobby.

Everything for admins lives under /admin, apart from the public pages (the live lobby, the
ladder), which show the same to everyone, logged on or not. The start page gathers what needs an
admin's eye: disputed ladder games, whether the server runs, and the latest games; the admins'
lobby view adds each player's address and whether others can reach them.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import Blueprint, render_template

from s3lobby.constants import LadderState
from s3lobby.database import GameRecord, Setting
from s3lobby.ladder_book import LadderBook
from s3lobby.web.auth import redirect_visitors_to_logon
from s3lobby.web.lobby_view import LiveLobbyPage, read_live_lobby_page
from s3lobby.web.storage import get_database

blueprint = Blueprint("admin_home", __name__)
blueprint.before_request(redirect_visitors_to_logon)

RECENT_GAME_COUNT = 5


@dataclass(frozen=True)
class Dashboard:
    """What the admins' start page shows."""

    lobby: LiveLobbyPage
    account_count: int
    banned_count: int
    disputed_count: int
    waiting_count: int
    custom_season_count: int
    recent_games: tuple[GameRecord, ...]
    is_ladder_enabled: bool


@blueprint.get("/")
def show_dashboard() -> str:
    """The admins' start page: the lobby at a glance and what waits for an admin."""
    database = get_database()
    now = database.now()
    accounts = database.list_accounts()
    results = LadderBook(database).results()
    dashboard = Dashboard(
        lobby=read_live_lobby_page(),
        account_count=len(accounts),
        banned_count=sum(1 for account in accounts if account.is_banned_at(now)),
        disputed_count=sum(1 for record in results if record.evaluation.state == LadderState.DISPUTED),
        waiting_count=len(database.list_unresolved_ladder_games()),
        custom_season_count=sum(1 for season in database.list_ladder_seasons() if season.is_running(now)),
        recent_games=tuple(database.list_recent_games(limit=RECENT_GAME_COUNT)),
        is_ladder_enabled=database.get_boolean_setting(Setting.LADDER_ENABLED),
    )
    return render_template("admin_home.html", dashboard=dashboard)


@blueprint.get("/lobby")
def admin_lobby() -> str:
    """The live lobby with what only admins see: each player's address, whether others can reach
    them, and who is an admin."""
    return render_template("admin_lobby.html", page=read_live_lobby_page(), show_admin_columns=True)


@blueprint.get("/lobby/state")
def admin_lobby_state() -> str:
    """Only the live part of the admins' lobby page, for the page's script to swap in."""
    return render_template("_live_state.html", page=read_live_lobby_page(), show_admin_columns=True)
