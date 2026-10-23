"""Games played through the lobby, and the match reports the game sends after a match.

The reports are stored as the bytes the game sent: a GameStatus body (docs/protocol.md, 0x1f
GameStatus). The page decodes them to show the map, the outcome and who played on which team with
which race; the bytes stay downloadable as stored. A report that does not decode is listed with
its size only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from flask import Blueprint, Response, abort, render_template

from s3lobby.constants import MessageType, Race
from s3lobby.database import GameReportRecord
from s3lobby.messages import GameStatus, MatchReportPlayer
from s3lobby.web.auth import redirect_visitors_to_logon
from s3lobby.web.storage import get_database
from s3lobby.wire import ProtocolError, RawMessage, readable_text

blueprint = Blueprint("games", __name__)
blueprint.before_request(redirect_visitors_to_logon)

RECENT_GAME_COUNT = 100
RECENT_REPORT_COUNT = 100
HTTP_NOT_FOUND = 404
REPORT_CONTENT_TYPE = "application/octet-stream"
RACE_NAMES = {
    Race.ROMANS: "Romans",
    Race.EGYPTIANS: "Egyptians",
    Race.ASIANS: "Asians",
    Race.AMAZONS: "Amazons",
}


@dataclass(frozen=True)
class ReportPlayerRow:
    """One slot of a match. name is the account's name, "Computer" for a computer opponent, or the
    lobby player id when no account has it (any more)."""

    name: str
    team: int
    race: str


@dataclass(frozen=True)
class ReportRow:
    report_id: int
    received_at: datetime
    sender_name: str
    group_id: int
    size: int
    is_readable: bool
    map_name: str = ""
    is_ladder_game: bool = False
    outcome_text: str = ""
    players: list[ReportPlayerRow] = field(default_factory=list)


@blueprint.get("/games")
def list_games() -> str:
    database = get_database()
    account_names = {account.account_id: account.name for account in database.list_accounts()}
    reports = database.list_recent_game_reports(RECENT_REPORT_COUNT)
    return render_template(
        "games.html",
        games=database.list_recent_games(RECENT_GAME_COUNT),
        reports=[describe_report(report, account_names) for report in reports],
    )


@blueprint.get("/games/reports/<int:report_id>")
def download_report(report_id: int) -> Response:
    report = get_database().find_game_report(report_id)
    if report is None:
        abort(HTTP_NOT_FOUND)
    file_name = f"game-report-{report_id}.bin"
    return Response(
        report,
        mimetype=REPORT_CONTENT_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


def describe_report(record: GameReportRecord, account_names: dict[int, str]) -> ReportRow:
    """A stored report as the page shows it. The lobby player id of a slot is the player's account
    id, since the server hands out account ids as player ids."""
    row = ReportRow(
        report_id=record.report_id,
        received_at=record.received_at,
        sender_name=record.sender_name,
        group_id=record.group_id,
        size=record.size,
        is_readable=False,
    )
    try:
        status = GameStatus.decode(RawMessage(MessageType.GAME_STATUS, record.report))
    except ProtocolError:
        return row
    players = [describe_report_player(player, account_names) for player in status.players]
    players.sort(key=lambda player: player.team)
    return ReportRow(
        report_id=row.report_id,
        received_at=row.received_at,
        sender_name=row.sender_name,
        group_id=row.group_id,
        size=row.size,
        is_readable=True,
        map_name=readable_text(status.map_name),
        is_ladder_game=status.is_ladder_game,
        outcome_text=outcome_text(status),
        players=players,
    )


def describe_report_player(player: MatchReportPlayer, account_names: dict[int, str]) -> ReportPlayerRow:
    if player.is_computer:
        name = "Computer"
    else:
        name = account_names.get(player.lobby_player_id, f"player {player.lobby_player_id}")
    race = RACE_NAMES.get(player.race, f"race {player.race}")
    return ReportPlayerRow(name=name, team=player.team, race=race)


def outcome_text(status: GameStatus) -> str:
    """Every client reports once at the start of the match, with no winner yet, and once when a
    single team is left."""
    if not status.has_winner:
        return "Match started"
    return f"Team {status.winning_team} won"
