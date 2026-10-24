"""The admin area's start page and its view of the live lobby.

Everything for admins lives under /admin, apart from the public pages (the live lobby, the
ladder), which show the same to everyone, logged on or not. The start page gathers what needs an
admin's eye: disputed ladder games, whether the server runs, and the latest games; the admins'
lobby view adds each player's address and whether others can reach them.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from s3lobby.constants import MAX_GROUP_TEXT_LENGTH, LadderState
from s3lobby.database import GameRecord, Setting
from s3lobby.ladder_book import LadderBook
from s3lobby.server_info import collect_server_info
from s3lobby.web.auth import redirect_visitors_to_logon
from s3lobby.web.lobby_view import LiveLobbyPage, read_live_lobby_page, search_text
from s3lobby.web.storage import apply_change, get_database
from s3lobby.web.texts import form_text

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


@dataclass(frozen=True)
class WebModes:
    """How this web interface was started, which only the web process knows."""

    is_development_mode: bool
    has_secure_cookies: bool
    is_behind_proxy: bool


@blueprint.get("/info")
def show_info() -> str:
    """The installation at a glance: version, database and schema, whether the server runs, the
    lobby's numbers, the ladder's members, the settings worth knowing, and this web interface's
    modes. `s3lobby info` prints the same without the web modes."""
    database = get_database()
    modes = WebModes(
        is_development_mode=current_app.config["S3LOBBY_DEVELOPMENT_MODE"],
        has_secure_cookies=current_app.config["S3LOBBY_HTTPS_ONLY"],
        is_behind_proxy=current_app.config.get("S3LOBBY_BEHIND_PROXY", False),
    )
    return render_template("info.html", info=collect_server_info(database), modes=modes)


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
    them, who is an admin, and Message, Kick and Ban for each; ?q= narrows the players down, ?to=
    fills in the message form's recipient."""
    search = search_text()
    message_to = request.args.get("to", "").strip()[:MAX_GROUP_TEXT_LENGTH]
    return render_template(
        "admin_lobby.html",
        page=read_live_lobby_page(search),
        search=search,
        message_to=message_to,
        show_admin_columns=True,
    )


@blueprint.get("/lobby/state")
def admin_lobby_state() -> str:
    """Only the live part of the admins' lobby page, for the page's script to swap in."""
    search = search_text()
    return render_template(
        "_live_state.html", page=read_live_lobby_page(search), search=search, show_admin_columns=True
    )


@blueprint.post("/lobby/message")
def message_player() -> Response:
    """Queue a server line for one player; the running server sends it within a second if the
    player is online, and the Announcements page shows whether it arrived."""
    name = form_text("name")
    text = form_text("text")
    database = get_database()
    account = database.find_account(name)
    if account is None:
        flash(f"There is no account named {name}.", "bad")
        return redirect(url_for("admin_home.admin_lobby", to=name))
    is_queued = apply_change(
        lambda: database.queue_announcement(text, account_id=account.account_id),
        f"Message for {account.name} queued; it arrives within a second if {account.name} is online.",
    )
    if not is_queued:
        return redirect(url_for("admin_home.admin_lobby", to=account.name))
    return redirect(url_for("admin_home.admin_lobby"))
