"""Games played through the lobby, and the match reports the game sends after a match.

The reports are stored as the bytes the game sent. Their layout is only partly worked out (see
docs/protocol.md, 0x1f GameStatus), so they are offered for download rather than shown.
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, render_template

from s3lobby.web.auth import redirect_visitors_to_logon
from s3lobby.web.storage import get_database

blueprint = Blueprint("games", __name__)
blueprint.before_request(redirect_visitors_to_logon)

RECENT_GAME_COUNT = 100
RECENT_REPORT_COUNT = 100
HTTP_NOT_FOUND = 404
REPORT_CONTENT_TYPE = "application/octet-stream"


@blueprint.get("/games")
def list_games() -> str:
    database = get_database()
    return render_template(
        "games.html",
        games=database.list_recent_games(RECENT_GAME_COUNT),
        reports=database.list_recent_game_reports(RECENT_REPORT_COUNT),
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
