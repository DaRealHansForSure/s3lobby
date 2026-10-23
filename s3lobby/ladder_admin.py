"""The ladder from the command line: standings, results, admins' decisions and custom seasons.

The lobby works with and without the web interface, so everything an admin can do with the ladder
on the web can be done here too, and standings and results come out as text for people, or as
JSON and CSV for other tools. The JSON carries a "format" number (LADDER_JSON_FORMAT) that changes
whenever a change would break a reader.

Each function takes the open database and the parsed options, like the ones in admin.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from typing import Any

from s3lobby.admin import AdminError, require_account
from s3lobby.constants import LADDER_JSON_FORMAT, LadderState, SeasonCounts
from s3lobby.database import STORED_LADDER_STATES, LobbyDatabase
from s3lobby.ladder import Season
from s3lobby.ladder_book import CLANLIGA, LIGA, LadderBook, ResultRow, StandingRow

TEXT_FORMAT = "text"
JSON_FORMAT = "json"
CSV_FORMAT = "csv"
OUTPUT_FORMATS = (TEXT_FORMAT, JSON_FORMAT, CSV_FORMAT)
# What `ladder standings --ladder` can show: players' Liga, clans' Clanliga, clans' strength from Liga.
CLANS_VIEW = "clans"
STANDINGS_VIEWS = (LIGA, CLANLIGA, CLANS_VIEW)
# The states `ladder results --state` can filter on, in a fixed order for the help text.
STORED_STATES = sorted(STORED_LADDER_STATES, key=lambda state: state.value)


def show_standings(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print a season's ranking of players (Liga), clans (Clanliga) or the clans' strength from Liga."""
    book = LadderBook(database)
    season = find_season(book, options.season)
    if options.ladder == CLANS_VIEW:
        rows = [asdict(row) for row in book.clan_strength_rows()]
        print_rows(options.format, rows, ("name", "strength", "members"), season, CLANS_VIEW, "clans")
        return
    rows = [standing_dict(row) for row in book.standing_rows(season, options.ladder)]
    columns = ("place", "name", "clan_name", "rating", "last_change", "won", "lost", "left", "is_provisional")
    if options.ladder == CLANLIGA:
        columns = ("place", "name", "rating", "last_change", "won", "lost", "left")
    print_rows(options.format, rows, columns, season, options.ladder, "standings")


def show_results(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print a season's ladder results, newest first, optionally only those in one state."""
    book = LadderBook(database)
    season = find_season(book, options.season)
    states = [LadderState(options.state)] if options.state else None
    rows = [result_dict(row) for row in book.result_rows(season, states, options.limit)]
    columns = ("game_id", "played_at", "ladder", "state", "winners", "losers", "minutes", "reason")
    print_rows(options.format, rows, columns, season, "", "results")


def show_game_result(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print what the ladder made of one game."""
    book = LadderBook(database)
    rows = [row for row in book.result_rows() if row.game_id == options.game_id]
    if not rows:
        raise AdminError(f"game {options.game_id} has no ladder result (yet)")
    row = rows[0]
    print(f"Game {row.game_id} {row.game_name!r}, {row.ladder}: {row.state.value}")
    if row.reason:
        print(f"  why: {row.reason}")
    if row.played_at is not None:
        print(f"  played: {row.played_at:%Y-%m-%d %H:%M} UTC, {row.minutes:.0f} minutes")
    print(f"  winners: {', '.join(row.winners) or '-'}")
    print(f"  losers: {', '.join(row.losers) or '-'}")
    if row.left:
        print(f"  left the match: {', '.join(row.left)}")
    if row.decided_by:
        print(f"  decided by: {row.decided_by}")


def decide_game(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Name the winning team of a game, typically one the reports disagree on."""
    admin_id = admin_account_id(database, options.by)
    try:
        LadderBook(database).decide(options.game_id, options.winner, admin_id)
    except (LookupError, ValueError) as error:
        raise AdminError(str(error)) from error
    print(f"Game {options.game_id} counts for team {options.winner} now; every rating follows.")


def void_game(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Take a game's result out of every rating; its reports stay."""
    admin_id = admin_account_id(database, options.by)
    try:
        LadderBook(database).void(options.game_id, options.reason, admin_id)
    except LookupError as error:
        raise AdminError(str(error)) from error
    print(f"Game {options.game_id} is void now; every rating follows.")


def list_seasons(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Print the custom seasons, newest first; the infinite season always runs besides them."""
    now = database.now()
    rows = [season_dict(season, now) for season in database.list_ladder_seasons()]
    if options.format == TEXT_FORMAT:
        print("Infinite season: always running, every counted result.")
    print_rows(options.format, rows, ("name", "starts_at", "ends_at", "counts", "state"), None, "", "seasons")


def add_season(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Create a custom season."""
    starts_at = parse_day(options.start)
    ends_at = parse_end_day(options.end)
    created_by = admin_account_id(database, options.by)
    season = database.add_ladder_season(options.name, starts_at, ends_at, SeasonCounts(options.counts), created_by)
    print(f"Season {season.name!r} created: {describe_dates(season)}, counts {season.counts.value}.")


def edit_season(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Change a custom season's name, dates or ladders; what is not given stays."""
    season = require_season(database, options.name)
    starts_at = season.starts_at if options.start is None else parse_day(options.start)
    ends_at = season.ends_at
    if options.no_end:
        ends_at = None
    elif options.end is not None:
        ends_at = parse_end_day(options.end)
    counts = season.counts if options.counts is None else SeasonCounts(options.counts)
    new_name = options.new_name or season.name
    database.update_ladder_season(season.season_id, new_name, starts_at, ends_at, counts)
    print(f"Season {new_name!r} changed; its standings follow at once.")


def end_season(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """End a custom season now, or at the end of the given day."""
    season = require_season(database, options.name)
    ends_at = database.now() if options.at is None else parse_end_day(options.at)
    database.update_ladder_season(season.season_id, season.name, season.starts_at, ends_at, season.counts)
    print(f"Season {season.name!r} ends {ends_at:%Y-%m-%d %H:%M} UTC.")


def delete_season(database: LobbyDatabase, options: argparse.Namespace) -> None:
    """Delete a custom season; its results stay in the infinite season."""
    season = require_season(database, options.name)
    database.delete_ladder_season(season.season_id)
    print(f"Season {season.name!r} deleted; its results still count in the infinite season.")


# Helpers.


def find_season(book: LadderBook, name: str | None) -> Season:
    """The season named on the command line, the infinite season without a name."""
    try:
        return book.find_season(name)
    except LookupError as error:
        raise AdminError(str(error)) from error


def require_season(database: LobbyDatabase, name: str) -> Season:
    """The custom season with this name; raises AdminError when there is none."""
    season = database.find_ladder_season(name)
    if season is None:
        raise AdminError(f"there is no custom season named {name!r}")
    return season


def admin_account_id(database: LobbyDatabase, name: str | None) -> int | None:
    """The account of the admin named with --by, kept with the decision; None when not given."""
    if not name:
        return None
    return require_account(database, name).account_id


def parse_day(text: str) -> datetime:
    """A date as YYYY-MM-DD (the start of that day, UTC) or a full ISO 8601 time."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise AdminError(f"{text!r} is not a date like 2026-10-01") from error
    # A plain date or a time without a zone is taken as UTC, like every time the server shows.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def parse_end_day(text: str | None) -> datetime | None:
    """The end of a season: a date means the season includes that whole day, so it ends at the
    start of the next one."""
    if text is None:
        return None
    moment = parse_day(text)
    is_plain_date = len(text.strip()) == len("YYYY-MM-DD")
    if is_plain_date:
        return datetime.combine(moment.date() + timedelta(days=1), time(), tzinfo=timezone.utc)
    return moment


def describe_dates(season: Season) -> str:
    """A season's dates for a person: from the start, until the end or until an admin ends it."""
    start = f"{season.starts_at:%Y-%m-%d}" if season.starts_at else "the start"
    if season.ends_at is None:
        return f"from {start}, until an admin ends it"
    # An end at midnight came from a last day: name that day, as it was typed.
    is_end_of_day = season.ends_at.time() == time() and season.ends_at.utcoffset() == timedelta(0)
    if is_end_of_day:
        return f"from {start} to {season.ends_at - timedelta(days=1):%Y-%m-%d}"
    return f"from {start} until {season.ends_at:%Y-%m-%d %H:%M} UTC"


def season_dict(season: Season, now: datetime) -> dict[str, Any]:
    """A custom season as a row for the output."""
    if season.is_running(now):
        state = "running"
    elif season.starts_at is not None and season.starts_at > now:
        state = "planned"
    else:
        state = "ended"
    return {
        "name": season.name,
        "starts_at": season.starts_at,
        "ends_at": season.ends_at,
        "counts": season.counts.value,
        "state": state,
    }


def standing_dict(row: StandingRow) -> dict[str, Any]:
    """A ranking line as a row for the output."""
    return asdict(row)


def result_dict(row: ResultRow) -> dict[str, Any]:
    """A result as a row for the output; the state as its name, the minutes rounded."""
    values = asdict(row)
    values["state"] = row.state.value
    values["minutes"] = round(row.minutes, 1)
    return values


def print_rows(
    output_format: str,
    rows: list[dict[str, Any]],
    columns: tuple[str, ...],
    season: Season | None,
    ladder: str,
    key: str,
) -> None:
    """Print rows as a text table, as JSON with the season and format number, or as CSV."""
    if output_format == JSON_FORMAT:
        document: dict[str, Any] = {"format": LADDER_JSON_FORMAT}
        if season is not None:
            document["season"] = {"name": season.name, "starts_at": season.starts_at, "ends_at": season.ends_at}
        if ladder:
            document["ladder"] = ladder
        document[key] = rows
        print(json.dumps(document, default=json_value, indent=2, ensure_ascii=False))
        return
    if output_format == CSV_FORMAT:
        writer = csv.writer(sys.stdout)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(cell_text(row[column]) for column in columns)
        return
    if not rows:
        print("(none)")
        return
    table = [list(columns)] + [[cell_text(row[column]) for column in columns] for row in rows]
    widths = [max(len(line[index]) for line in table) for index in range(len(columns))]
    for line in table:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip())


def cell_text(value: Any) -> str:
    """A value as one table or CSV cell: times in UTC to the minute, lists joined, None as '-'."""
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return f"{value.astimezone(timezone.utc):%Y-%m-%d %H:%M}"
    if isinstance(value, (list, tuple)):
        return ", ".join(cell_text(item) for item in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def json_value(value: Any) -> Any:
    """What json cannot write itself: times as ISO 8601 in UTC."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    raise TypeError(f"{type(value).__name__} is not JSON")
