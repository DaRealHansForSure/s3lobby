"""The ladder on the web: the public standings, and the admins' ladder games and seasons.

Everything here only shows what LadderBook (s3lobby/ladder_book.py) computes and passes admins'
decisions to it; the ladder's rules live outside the web interface, so the lobby works without
it and `s3lobby ladder` offers the same actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from s3lobby.constants import (
    LADDER_TOP_COUNT,
    MAX_GROUP_TEXT_LENGTH,
    RACE_NAMES,
    LadderState,
    LadderType,
    SeasonCounts,
)
from s3lobby.database import Account, Clan, DatabaseError, LobbyDatabase, Setting
from s3lobby.ladder import (
    FORM_LEFT,
    FORM_LOST,
    FORM_WON,
    INFINITE_SEASON,
    MapStatistic,
    RaceStatistic,
    Rating,
    Season,
    map_statistics,
    race_statistics,
    recent_form,
)
from s3lobby.ladder_book import (
    CLANLIGA,
    LIGA,
    ClanStrengthRow,
    LadderBook,
    LadderMembershipError,
    ResultRow,
    StandingRow,
    join_ladder,
    last_season_day,
    leave_ladder,
    membership_rows,
    parse_season_day,
    parse_season_end,
)
from s3lobby.web.accounts import account_row_colour
from s3lobby.web.auth import current_admin, redirect_visitors_to_logon
from s3lobby.web.lobby_view import map_text
from s3lobby.web.paging import pagination_from_request
from s3lobby.web.storage import get_database, sentence_from_error
from s3lobby.web.texts import form_text

# The standings page is public, like the live lobby; the ladder games and seasons are for admins.
public_blueprint = Blueprint("ladder_public", __name__)
admin_blueprint = Blueprint("ladder_admin", __name__)
admin_blueprint.before_request(redirect_visitors_to_logon)

# The tabs of the public page: players in Liga, clans in Clanliga, clans' strength from Liga.
CLANS_VIEW = "clans"
STATISTICS_VIEW = "statistics"
VIEWS = (LIGA, CLANLIGA, CLANS_VIEW, STATISTICS_VIEW)
VIEW_LABELS = {LIGA: "Liga", CLANLIGA: "Clanliga", CLANS_VIEW: "Clan strength", STATISTICS_VIEW: "Races and maps"}
RECENT_RESULT_COUNT = 10
# The admins' state tabs; "waiting" are games without a result yet.
WAITING_TAB = "waiting"
ALL_TAB = "all"
STATE_TABS = (LadderState.DISPUTED.value, WAITING_TAB, LadderState.COUNTED.value, LadderState.VOID.value, ALL_TAB)
STATE_TAB_LABELS = {
    LadderState.DISPUTED.value: "Needs a decision",
    WAITING_TAB: "Waiting",
    LadderState.COUNTED.value: "Counted",
    LadderState.VOID.value: "Void",
    ALL_TAB: "All",
}
FORM_CLASSES = {FORM_WON: ("won", "won"), FORM_LOST: ("lost", "lost"), FORM_LEFT: ("left", "lost by leaving")}
# How many players the rating chart draws: the podium.
CHART_SERIES_COUNT = 3

# The rating chart's drawing area inside its 700 by 200 viewBox: room on the left for the rating
# labels, below for the dates. CHART_RATING_STEP is the distance between two grid lines.
CHART_LEFT = 56
CHART_RIGHT = 676
CHART_TOP = 12
CHART_BOTTOM = 172
CHART_LABEL_GAP = 8
CHART_DATE_LABEL_OFFSET = 20
CHART_RATING_STEP = 50
PERCENT = 100
# A stand-in account for colouring a clan's name: no admin, so only the clan decides the colour.
NO_ACCOUNT = Account(account_id=0, name="", is_admin=False, banned_until=None, ban_reason="")


@dataclass(frozen=True)
class FormMark:
    """One square of a side's recent form: its CSS class and what it means."""

    css_class: str
    title: str


@dataclass(frozen=True)
class StandingView:
    """A ranking line as the page shows it: the row, the name's in-game colour, the form, the win rate."""

    row: StandingRow
    colour: str
    form: tuple[FormMark, ...]
    win_rate: int


@dataclass(frozen=True)
class RaceRow:
    """A race in the statistics: its name, how often it was played and won, the win rate."""

    name: str
    played: int
    won: int
    win_rate: int


@dataclass(frozen=True)
class MapRow:
    """A map in the statistics: its name as the page shows it, how often it was played, and the
    average match in whole minutes."""

    name: str
    played: int
    average_minutes: int


@dataclass(frozen=True)
class ChartLine:
    """One player's rating over time in the chart: the series number picks the colour."""

    name: str
    series: int
    points: str
    rating: int


@dataclass(frozen=True)
class ChartLabel:
    """A label of the chart's axes, placed in viewBox units."""

    x: float
    y: float
    text: str
    is_start_line: bool = False


@dataclass(frozen=True)
class RatingChart:
    """The rating chart: lines, horizontal grid lines with their ratings, and dates below."""

    lines: tuple[ChartLine, ...]
    grid: tuple[ChartLabel, ...]
    dates: tuple[ChartLabel, ...]


@public_blueprint.get("/ladder")
def show_ladder() -> str:
    """The public standings: Liga, Clanliga or clan strength, for a season, with a podium, the
    rating chart and the last results."""
    database = get_database()
    book = LadderBook(database)
    view = request.args.get("view", LIGA)
    if view not in VIEWS:
        view = LIGA
    try:
        season = book.find_season(request.args.get("season"))
    except LookupError:
        season = INFINITE_SEASON
    search = request.args.get("q", "").strip()
    seasons = database.list_ladder_seasons()
    results = book.results()
    # Only results that count among the ladder's members, so form and statistics match the ratings.
    evaluations = book.evaluations_in_ladder(season)

    standings: list[StandingView] = []
    strength_rows: list[ClanStrengthRow] = []
    race_rows: list[RaceRow] = []
    map_rows: list[MapRow] = []
    chart = None
    if view == CLANS_VIEW:
        strength_rows = book.clan_strength_rows()
    elif view == STATISTICS_VIEW:
        race_rows = [race_row(statistic) for statistic in race_statistics(evaluations, season)]
        map_rows = [map_row(statistic) for statistic in map_statistics(evaluations, season)]
    else:
        ladder_type = ladder_type_of_view(view)
        colours = name_colours(database, view)
        for row in book.standing_rows(season, view):
            form = recent_form(evaluations, row.side_id, ladder_type, season)
            standings.append(StandingView(row, colours.get(row.side_id, ""), form_marks(form), win_rate(row)))
        if view == LIGA:
            ratings = book.ratings(season).liga
            chart = rating_chart(standings[:CHART_SERIES_COUNT], ratings, settings_start(database), database.now())
    podium = standings[:CHART_SERIES_COUNT]
    shown = [standing for standing in standings if not search or search.lower() in standing.row.name.lower()]
    recent = book.result_rows(season, [LadderState.COUNTED], RECENT_RESULT_COUNT)
    counted_count = sum(1 for record in results if record.evaluation.state == LadderState.COUNTED)
    running_count = sum(1 for custom in seasons if custom.is_running(database.now()))
    return render_template(
        "ladder.html",
        view=view,
        views=VIEWS,
        view_labels=VIEW_LABELS,
        season=season,
        seasons=seasons,
        search=search,
        podium=podium,
        standings=shown,
        ranked_count=len(standings),
        strength_rows=strength_rows,
        race_rows=race_rows,
        map_rows=map_rows,
        chart=chart,
        recent=recent,
        counted_count=counted_count,
        running_count=running_count,
        start_rating=settings_start(database),
        top_count=LADDER_TOP_COUNT,
    )


@admin_blueprint.get("/ladder/games")
def list_ladder_games() -> str:
    """Every evaluated ladder game, by state, with decide and void; the seasons and their forms."""
    database = get_database()
    book = LadderBook(database)
    tab = request.args.get("state", ALL_TAB)
    if tab not in STATE_TABS:
        tab = ALL_TAB
    search = request.args.get("q", "").strip()[:MAX_GROUP_TEXT_LENGTH]
    all_rows = book.result_rows()
    waiting_games = database.list_unresolved_ladder_games()
    counts = {state.value: 0 for state in (LadderState.COUNTED, LadderState.DISPUTED, LadderState.VOID)}
    for row in all_rows:
        counts[row.state.value] += 1
    counts[WAITING_TAB] = len(waiting_games)
    counts[ALL_TAB] = len(all_rows) + len(waiting_games)

    matching = []
    if tab != WAITING_TAB:
        matching = [row for row in all_rows if tab == ALL_TAB or row.state.value == tab]
        if search:
            matching = [row for row in matching if search.lower() in searchable_text(row)]
    # Every result is loaded for the counts anyway, so the page is a slice of them.
    pagination = pagination_from_request(len(matching))
    rows = pagination.slice(matching)
    show_waiting = tab in (WAITING_TAB, ALL_TAB) and not pagination.has_previous
    page_params = {"state": tab}
    if search:
        page_params["q"] = search
    return render_template(
        "ladder_games.html",
        tab=tab,
        tabs=STATE_TABS,
        tab_labels=STATE_TAB_LABELS,
        counts=counts,
        search=search,
        rows=rows,
        waiting_games=waiting_games if show_waiting else [],
        teams_by_game=disputed_teams(database, rows),
        pagination=pagination,
        page_params=page_params,
    )


@admin_blueprint.post("/ladder/games/<int:game_id>/decide")
def decide_game(game_id: int) -> Response:
    """Count a game for the team the admin picked."""
    winner_text = form_text("winner")
    if not winner_text.isdigit():
        abort(400)
    admin = current_admin()
    try:
        LadderBook(get_database()).decide(game_id, int(winner_text), admin.account_id if admin else None)
    except (LookupError, ValueError) as error:
        flash(f"{error}.", "bad")
        return redirect(url_for("ladder_admin.list_ladder_games"))
    flash(f"Game {game_id} counts for team {winner_text} now; every rating follows.", "good")
    return redirect(url_for("ladder_admin.list_ladder_games"))


@admin_blueprint.post("/ladder/games/<int:game_id>/void")
def void_game(game_id: int) -> Response:
    """Take a game's result out of every rating; its reports stay."""
    admin = current_admin()
    try:
        LadderBook(get_database()).void(game_id, form_text("reason"), admin.account_id if admin else None)
    except LookupError as error:
        flash(f"{error}.", "bad")
        return redirect(url_for("ladder_admin.list_ladder_games"))
    flash(f"Game {game_id} is void now; every rating follows.", "good")
    return redirect(url_for("ladder_admin.list_ladder_games"))


@admin_blueprint.get("/ladder/seasons")
def list_seasons() -> str:
    """The custom seasons as cards, with editing, ending and deleting, and the form for a new one."""
    database = get_database()
    now = database.now()
    return render_template(
        "ladder_seasons.html",
        seasons=[season_card(season, now) for season in database.list_ladder_seasons()],
        counts_choices=[counts_choice.value for counts_choice in SeasonCounts],
    )


@admin_blueprint.post("/ladder/seasons")
def create_season() -> Response:
    """Create a custom season from the form."""
    database = get_database()
    admin = current_admin()
    try:
        starts_at = parse_season_day(form_text("start"))
        ends_at = parse_season_end(form_text("end"))
        counts = SeasonCounts(form_text("counts"))
        season = database.add_ladder_season(
            form_text("name"), starts_at, ends_at, counts, admin.account_id if admin else None
        )
    except DatabaseError as error:
        flash(sentence_from_error(error), "bad")
        return redirect(url_for("ladder_admin.list_seasons"))
    except ValueError as error:
        flash(f"{error}.", "bad")
        return redirect(url_for("ladder_admin.list_seasons"))
    flash(f"Season {season.name} created.", "good")
    return redirect(url_for("ladder_admin.list_seasons"))


@admin_blueprint.post("/ladder/seasons/<int:season_id>")
def edit_season(season_id: int) -> Response:
    """Change a custom season's name, dates or ladders; its standings follow at once."""
    database = get_database()
    require_season(database, season_id)
    try:
        starts_at = parse_season_day(form_text("start"))
        ends_at = parse_season_end(form_text("end"))
        counts = SeasonCounts(form_text("counts"))
        database.update_ladder_season(season_id, form_text("name"), starts_at, ends_at, counts)
    except DatabaseError as error:
        flash(sentence_from_error(error), "bad")
        return redirect(url_for("ladder_admin.list_seasons"))
    except ValueError as error:
        flash(f"{error}.", "bad")
        return redirect(url_for("ladder_admin.list_seasons"))
    flash("Season saved; its standings follow at once.", "good")
    return redirect(url_for("ladder_admin.list_seasons"))


@admin_blueprint.post("/ladder/seasons/<int:season_id>/end")
def end_season(season_id: int) -> Response:
    """End a custom season now."""
    database = get_database()
    season = require_season(database, season_id)
    try:
        database.update_ladder_season(season_id, season.name, season.starts_at, database.now(), season.counts)
    except DatabaseError as error:
        flash(sentence_from_error(error), "bad")
        return redirect(url_for("ladder_admin.list_seasons"))
    flash(f"Season {season.name} ended.", "good")
    return redirect(url_for("ladder_admin.list_seasons"))


@admin_blueprint.post("/ladder/seasons/<int:season_id>/delete")
def delete_season(season_id: int) -> Response:
    """Delete a custom season; its results stay in the infinite season."""
    database = get_database()
    season = require_season(database, season_id)
    database.delete_ladder_season(season_id)
    flash(f"Season {season.name} deleted with its members; its results stay for the infinite season.", "good")
    return redirect(url_for("ladder_admin.list_seasons"))


@admin_blueprint.get("/ladder/members")
def list_members() -> str:
    """Who is in which ladder now, with the form to add a player and a button to take one out.
    Players join themselves with /ladder join in the game; this is the admins' way to do it."""
    database = get_database()
    now = database.now()
    joinable_seasons = [
        season.name
        for season in database.list_ladder_seasons()
        if season.ends_at is None or season.ends_at > now
    ]
    return render_template(
        "ladder_members.html",
        rows=membership_rows(database),
        ladder_choices=["Liga", "Clanliga", *joinable_seasons],
    )


@admin_blueprint.post("/ladder/members")
def add_member() -> Response:
    """Make a player a member of the ladder the form names."""
    database = get_database()
    account = database.find_account(form_text("name"))
    if account is None:
        flash("There is no account with that name.", "bad")
        return redirect(url_for("ladder_admin.list_members"))
    ladder = form_text("ladder")
    try:
        join_ladder(LadderBook(database), database, account.account_id, ladder)
    except LadderMembershipError as error:
        flash(f"{account.name}: {error}.", "bad")
        return redirect(url_for("ladder_admin.list_members"))
    flash(f"{account.name} is in {ladder or 'Liga'} now.", "good")
    return redirect(url_for("ladder_admin.list_members"))


@admin_blueprint.post("/ladder/members/<int:account_id>/leave")
def remove_member(account_id: int) -> Response:
    """Take a player out of a ladder; their results stay for their opponents."""
    database = get_database()
    ladder = form_text("ladder")
    try:
        leave_ladder(LadderBook(database), database, account_id, ladder)
    except LadderMembershipError as error:
        flash(f"{error}.", "bad")
        return redirect(url_for("ladder_admin.list_members"))
    flash(f"Taken out of {ladder or 'Liga'}; joining again starts at the start rating.", "good")
    return redirect(url_for("ladder_admin.list_members"))


# Helpers.


@dataclass(frozen=True)
class SeasonCard:
    """A custom season as the admins' page shows it: its state, how far it has run, and its form values."""

    season: Season
    state: str
    progress: int | None
    start_text: str
    end_text: str


def season_card(season: Season, now: datetime) -> SeasonCard:
    """A season with its state (planned, running, ended) and, while running with an end, how much
    of it has passed."""
    if season.is_running(now):
        state = "running"
    elif season.starts_at is not None and season.starts_at > now:
        state = "planned"
    else:
        state = "ended"
    progress = None
    if state == "running" and season.starts_at is not None and season.ends_at is not None:
        progress = round(PERCENT * (now - season.starts_at) / (season.ends_at - season.starts_at))
    last_day = last_season_day(season)
    start_text = f"{season.starts_at:%Y-%m-%d}" if season.starts_at else ""
    end_text = f"{last_day:%Y-%m-%d}" if last_day else ""
    return SeasonCard(season, state, progress, start_text, end_text)


def require_season(database: LobbyDatabase, season_id: int) -> Season:
    """The custom season with this id, or a 404 page."""
    for season in database.list_ladder_seasons():
        if season.season_id == season_id:
            return season
    abort(404)


def ladder_type_of_view(view: str) -> int:
    """The LadderType a standings view ranks."""
    return LadderType.CLANS if view == CLANLIGA else LadderType.INDIVIDUAL


def name_colours(database: LobbyDatabase, view: str) -> dict[int, str]:
    """The colour the game's member lists draw each player (Liga) or each clan's members (Clanliga)
    in, so the ladder looks like the lobby."""
    clans = {clan.clan_id: clan for clan in database.list_clans()}
    if view == CLANLIGA:
        return {clan_id: account_row_colour(NO_ACCOUNT, clan, marks_admins=False) for clan_id, clan in clans.items()}
    marks_admins = database.get_boolean_setting(Setting.MARK_ADMINS)
    clan_of_account: dict[int, Clan] = {}
    for clan_id, account_ids in database.list_clan_memberships().items():
        for account_id in account_ids:
            clan_of_account[account_id] = clans[clan_id]
    return {
        account.account_id: account_row_colour(account, clan_of_account.get(account.account_id), marks_admins)
        for account in database.list_accounts()
    }


def race_row(statistic: RaceStatistic) -> RaceRow:
    """A race statistic as the page shows it."""
    win_rate = round(PERCENT * statistic.won / statistic.played) if statistic.played else 0
    name = RACE_NAMES.get(statistic.race, f"race {statistic.race}")
    return RaceRow(name, statistic.played, statistic.won, win_rate)


def map_row(statistic: MapStatistic) -> MapRow:
    """A map statistic as the page shows it; a match without a map name shows as unknown."""
    name = map_text(statistic.map_name) if statistic.map_name else "(unknown map)"
    return MapRow(name, statistic.played, round(statistic.average_minutes))


def form_marks(form: str) -> tuple[FormMark, ...]:
    """The squares of a recent form."""
    return tuple(FormMark(*FORM_CLASSES[letter]) for letter in form)


def win_rate(row: StandingRow) -> int:
    """The share of won results in percent."""
    if row.results == 0:
        return 0
    return round(PERCENT * row.won / row.results)


def settings_start(database: LobbyDatabase) -> int:
    """The start rating, where the chart draws its dashed line."""
    return database.get_ladder_settings().start_rating


def rating_chart(
    podium: list[StandingView], ratings: dict[int, Rating], start_rating: int, now: datetime
) -> RatingChart | None:
    """The podium's ratings over time as lines in a 700 by 200 viewBox, None while nobody has
    played. Each line starts at the start rating on the player's first result."""
    histories = []
    for number, standing in enumerate(podium, start=1):
        rating = ratings.get(standing.row.side_id)
        if rating is None or not rating.history:
            continue
        histories.append((standing.row.name, number, rating.history))
    if not histories:
        return None
    first_moment = min(history[0][0] for _, _, history in histories)
    last_moment = max(now, first_moment + timedelta(seconds=1))
    values = [start_rating] + [value for _, _, history in histories for _, value in history]
    low = CHART_RATING_STEP * (int(min(values)) // CHART_RATING_STEP)
    high = CHART_RATING_STEP * (int(max(values)) // CHART_RATING_STEP + 1)

    def x_of(moment: datetime) -> float:
        """Where a moment lies on the time axis."""
        return CHART_LEFT + (CHART_RIGHT - CHART_LEFT) * (moment - first_moment) / (last_moment - first_moment)

    def y_of(value: float) -> float:
        """Where a rating lies on the rating axis."""
        return CHART_BOTTOM - (CHART_BOTTOM - CHART_TOP) * (value - low) / (high - low)

    lines = []
    for name, number, history in histories:
        points = [(x_of(history[0][0]), y_of(start_rating))]
        points += [(x_of(moment), y_of(value)) for moment, value in history]
        points.append((x_of(last_moment), y_of(history[-1][1])))
        point_text = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        lines.append(ChartLine(name, number, point_text, round(history[-1][1])))
    grid = tuple(
        ChartLabel(CHART_LEFT - CHART_LABEL_GAP, y_of(value), str(value), is_start_line=value == start_rating)
        for value in range(low, high + 1, CHART_RATING_STEP)
    )
    middle = first_moment + (last_moment - first_moment) / 2
    dates = tuple(
        ChartLabel(x_of(moment), CHART_BOTTOM + CHART_DATE_LABEL_OFFSET, f"{moment:%d %b}")
        for moment in (first_moment, middle, last_moment)
    )
    return RatingChart(tuple(lines), grid, dates)


def disputed_teams(database: LobbyDatabase, rows: list[ResultRow]) -> dict[int, list[tuple[int, str]]]:
    """The teams of every disputed game among the rows, by game."""
    return {row.game_id: teams_of(database, row.game_id) for row in rows if row.state == LadderState.DISPUTED}


def teams_of(database: LobbyDatabase, game_id: int) -> list[tuple[int, str]]:
    """The teams of a disputed game with their players' names, for the buttons that decide it."""
    record = database.find_ladder_result(game_id)
    if record is None:
        return []
    names = {account.account_id: account.name for account in database.list_accounts()}
    teams: dict[int, list[str]] = {}
    for player in record.evaluation.players:
        teams.setdefault(player.team, []).append(names.get(player.account_id, "?"))
    return [(team, ", ".join(members)) for team, members in sorted(teams.items())]


def searchable_text(row: ResultRow) -> str:
    """What the admins' search looks in: the game's name and every player."""
    return " ".join((row.game_name, *row.winners, *row.losers)).lower()
