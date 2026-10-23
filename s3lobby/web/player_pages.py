"""A public page per player: their ladder standing, their rating over time, and the games the lobby
saw them play, with opponents and results.

Public like the live lobby and the ladder: it shows only what those already show (names, clans,
ratings, results), never addresses, access codes or bans.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import Blueprint, abort, render_template

from s3lobby.constants import LadderType
from s3lobby.ladder import INFINITE_SEASON, Rating, Season, recent_form
from s3lobby.ladder_book import LIGA, LadderBook, StandingRow
from s3lobby.web.games import AccountGameRow, games_of_account
from s3lobby.web.ladder_pages import (
    FormMark,
    RatingChart,
    StandingView,
    form_marks,
    name_colours,
    rating_chart,
    win_rate,
)
from s3lobby.web.storage import get_database

blueprint = Blueprint("players", __name__)


@dataclass(frozen=True)
class SeasonPlace:
    """The player's place in one custom season that is running."""

    season: Season
    row: StandingRow
    ranked_count: int


@dataclass(frozen=True)
class PlayerPage:
    """Everything the player page shows."""

    name: str
    clan_name: str
    colour: str
    standing: StandingRow | None
    ranked_count: int
    win_rate: int
    form: tuple[FormMark, ...]
    chart: RatingChart | None
    season_places: tuple[SeasonPlace, ...]
    clanliga_record: Rating | None
    games: tuple[AccountGameRow, ...]


@blueprint.get("/players/<name>")
def show_player(name: str) -> str:
    """A player's public page, found by name in any case."""
    database = get_database()
    account = database.find_account(name)
    if account is None:
        abort(404)
    book = LadderBook(database)
    rows = book.standing_rows(INFINITE_SEASON, LIGA)
    standing = next((row for row in rows if row.side_id == account.account_id), None)
    evaluations = [record.evaluation for record in book.results()]
    form = form_marks(recent_form(evaluations, account.account_id, LadderType.INDIVIDUAL))
    colour = name_colours(database, LIGA).get(account.account_id, "")
    chart = None
    if standing is not None:
        view = StandingView(standing, colour, form, win_rate(standing))
        start_rating = database.get_ladder_settings().start_rating
        chart = rating_chart([view], book.ratings().liga, start_rating, database.now())
    season_places = []
    now = database.now()
    for season in database.list_ladder_seasons():
        if not season.is_running(now):
            continue
        season_rows = book.standing_rows(season, LIGA)
        season_row = next((row for row in season_rows if row.side_id == account.account_id), None)
        if season_row is not None:
            season_places.append(SeasonPlace(season, season_row, len(season_rows)))
    clan = database.find_clan_of_account(account.account_id)
    page = PlayerPage(
        name=account.name,
        clan_name=clan.name if clan is not None else "",
        colour=colour,
        standing=standing,
        ranked_count=len(rows),
        win_rate=win_rate(standing) if standing is not None else 0,
        form=form,
        chart=chart,
        season_places=tuple(season_places),
        clanliga_record=book.ratings().clanliga_players.get(account.account_id),
        games=tuple(games_of_account(account.account_id)),
    )
    return render_template("player.html", page=page)
