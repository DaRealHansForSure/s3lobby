"""Games played through the lobby, and the match reports the game sends during a match.

A game is stored with the announcement the lobby last showed for it (an AddGroupToGroup2 body:
the game block and the host's description, the host address zeroed) and the players who joined
it. Reports are stored as the bytes the game sent: a GameStatus body (docs/protocol.md, 0x1f
GameStatus). The page decodes both: the setup the host chose, who played, and every field of every
report. The report bytes stay downloadable as stored. Anything that does not decode is listed
without its details rather than taking the page down.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus

from flask import Blueprint, Response, abort, render_template

from s3lobby.constants import (
    UNLIMITED_PLAYERS,
    GameStatusFlag,
    LadderType,
    MatchPlayerFlag,
    MessageType,
    Race,
)
from s3lobby.database import GameRecord, GameReportRecord
from s3lobby.groups import game_description_of, game_settings_of
from s3lobby.messages import GameStatus, GroupAnnouncement, MatchReportPlayer
from s3lobby.web.auth import redirect_visitors_to_logon
from s3lobby.web.storage import get_database
from s3lobby.wire import ProtocolError, RawMessage, readable_text

blueprint = Blueprint("games", __name__)
blueprint.before_request(redirect_visitors_to_logon)

RECENT_GAME_COUNT = 100
RECENT_REPORT_COUNT = 100
REPORT_CONTENT_TYPE = "application/octet-stream"
RACE_NAMES = {
    Race.ROMANS: "Romans",
    Race.EGYPTIANS: "Egyptians",
    Race.ASIANS: "Asians",
    Race.AMAZONS: "Amazons",
}
# The host's choice in the game setup, as the game list names it (0x44b843, 0x44b97a).
LADDER_TYPE_NAMES = {
    LadderType.NONE: "Normal game",
    LadderType.INDIVIDUAL: "Ladder 1 on 1 (Liga)",
    LadderType.CLANS: "Ladder with clans (Clanliga)",
}
# The game's version number counts hundredths: 160 is version 1.60 (global 0x6970f0).
VERSION_DIVISOR = 100
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class GameSetup:
    """What the host chose for a game, decoded from its stored announcement."""

    map_name: str = ""
    team_count: int | None = None
    computer_opponent_count: int | None = None
    max_players: str = ""
    game_type: str = ""
    allows_amazons: bool | None = None
    version: str = ""
    program_checksum: str = ""
    session_guid: str = ""


@dataclass(frozen=True)
class ReportPlayerRow:
    """One slot of a match. name is the account's name, "Computer" for a computer opponent, or the
    lobby player id when no account has it (any more). notes names the slot's flags."""

    name: str
    slot: int
    team: int
    race: str
    lobby_player_id: int
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportRow:
    report_id: int
    received_at: datetime
    sender_name: str
    group_id: int
    size: int
    is_readable: bool
    game_id: int | None = None
    game_name: str = ""
    map_name: str = ""
    outcome_text: str = ""
    game_time: int = 0
    flag_notes: tuple[str, ...] = ()
    players: tuple[ReportPlayerRow, ...] = ()


@dataclass(frozen=True)
class GameRow:
    game_id: int
    name: str
    host_name: str
    group_id: int | None
    opened_at: datetime
    closed_at: datetime | None
    open_for: str
    player_names: tuple[str, ...]
    setup: GameSetup | None
    match_started_at: datetime | None = None
    outcome_text: str = ""
    reports: tuple[ReportRow, ...] = field(default=())


@blueprint.get("/games")
def list_games() -> str:
    database = get_database()
    account_names = {account.account_id: account.name for account in database.list_accounts()}
    games = database.list_recent_games(RECENT_GAME_COUNT)
    game_names = {game.game_id: game.name for game in games}
    reports = [
        describe_report(report, account_names, game_names)
        for report in database.list_recent_game_reports(RECENT_REPORT_COUNT)
    ]
    return render_template(
        "games.html",
        games=[describe_game(game, reports) for game in games],
        reports=reports,
    )


@blueprint.get("/games/reports/<int:report_id>")
def download_report(report_id: int) -> Response:
    report = get_database().find_game_report(report_id)
    if report is None:
        abort(HTTPStatus.NOT_FOUND)
    file_name = f"game-report-{report_id}.bin"
    return Response(
        report,
        mimetype=REPORT_CONTENT_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


# Games.


def describe_game(game: GameRecord, reports: list[ReportRow]) -> GameRow:
    """A stored game as the page shows it, with its reports, oldest first. The match started when
    the first report came: every player's game reports once at the start (0x53da69)."""
    own_reports = sorted(
        (report for report in reports if report.game_id == game.game_id),
        key=lambda report: report.received_at,
    )
    match_started_at = None
    if own_reports:
        match_started_at = own_reports[0].received_at
    outcome = ""
    for report in own_reports:
        if report.is_readable:
            outcome = report.outcome_text
    return GameRow(
        game_id=game.game_id,
        name=game.name,
        host_name=game.host_name,
        group_id=game.group_id,
        opened_at=game.opened_at,
        closed_at=game.closed_at,
        open_for=open_duration_text(game.opened_at, game.closed_at),
        player_names=game.player_names,
        setup=decode_setup(game.announcement),
        match_started_at=match_started_at,
        outcome_text=outcome,
        reports=tuple(own_reports),
    )


def decode_setup(announcement_body: bytes | None) -> GameSetup | None:
    """The host's setup from a stored announcement; None for games stored before the server kept
    it, or for bytes that do not decode. The description is the current setup (the host rewrites
    it on every change, 0x454db0); the game block holds the type chosen when the game was opened,
    the version and the session."""
    if announcement_body is None:
        return None
    try:
        announcement = GroupAnnouncement.decode(RawMessage(MessageType.ADD_GROUP_TO_GROUP_2, announcement_body))
    except (ProtocolError, ValueError):
        return None
    description = game_description_of(announcement)
    settings = game_settings_of(announcement)

    map_name = ""
    team_count = None
    computer_opponent_count = None
    game_type = ""
    if description is not None:
        map_name = readable_text(description.map_name)
        team_count = description.team_count
        computer_opponent_count = description.computer_opponent_count
        if description.is_ladder_game:
            game_type = "Ladder"
        else:
            game_type = "Normal game"
    if settings is None:
        return GameSetup(
            map_name=map_name,
            team_count=team_count,
            computer_opponent_count=computer_opponent_count,
            max_players=max_players_text(announcement.max_players),
            game_type=game_type,
        )
    # The description says whether the game is a ladder game now; the block can only name which
    # ladder, and is out of date when the host switched back to a normal game.
    is_ladder_now = description is None or description.is_ladder_game
    is_ladder_named = settings.ladder_type is not None and settings.ladder_type != LadderType.NONE
    if is_ladder_now and is_ladder_named:
        game_type = LADDER_TYPE_NAMES.get(settings.ladder_type, f"type {settings.ladder_type}")
    elif not game_type and settings.ladder_type is not None:
        game_type = LADDER_TYPE_NAMES.get(settings.ladder_type, f"type {settings.ladder_type}")
    return GameSetup(
        map_name=map_name,
        team_count=team_count,
        computer_opponent_count=computer_opponent_count,
        max_players=max_players_text(announcement.max_players),
        game_type=game_type,
        allows_amazons=settings.allows_amazons,
        version=version_text(settings.game_version),
        program_checksum=f"{settings.program_checksum:016x}",
        session_guid=str(uuid.UUID(bytes_le=settings.session_guid)),
    )


def max_players_text(max_players: int) -> str:
    if max_players in (0, UNLIMITED_PLAYERS):
        return "no limit"
    return str(max_players)


def version_text(game_version: int) -> str:
    whole, hundredths = divmod(game_version, VERSION_DIVISOR)
    return f"{whole}.{hundredths:02d}"


def open_duration_text(opened_at: datetime, closed_at: datetime | None) -> str:
    """How long a closed game was open, in hours, minutes and seconds as far as they matter."""
    if closed_at is None:
        return ""
    return duration_text(closed_at - opened_at)


def duration_text(duration: timedelta) -> str:
    total_seconds = max(int(duration.total_seconds()), 0)
    hours, rest = divmod(total_seconds, SECONDS_PER_HOUR)
    minutes, seconds = divmod(rest, SECONDS_PER_MINUTE)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} s"
    return f"{seconds} s"


# Match reports.


def describe_report(record: GameReportRecord, account_names: dict[int, str], game_names: dict[int, str]) -> ReportRow:
    """A stored report as the page shows it. The lobby player id of a slot is the player's account
    id, since the server hands out account ids as player ids."""
    row = ReportRow(
        report_id=record.report_id,
        received_at=record.received_at,
        sender_name=record.sender_name,
        group_id=record.group_id,
        size=record.size,
        is_readable=False,
        game_id=record.game_id,
        game_name=game_names.get(record.game_id, "") if record.game_id is not None else "",
    )
    try:
        status = GameStatus.decode(RawMessage(MessageType.GAME_STATUS, record.report))
    except (ProtocolError, ValueError):
        # Reports are stored as players' games sent them; a malformed one must not take the page
        # down for every admin.
        return row
    players = [describe_report_player(player, account_names) for player in status.players]
    players.sort(key=lambda player: (player.team, player.slot))
    return ReportRow(
        report_id=row.report_id,
        received_at=row.received_at,
        sender_name=row.sender_name,
        group_id=row.group_id,
        size=row.size,
        is_readable=True,
        game_id=row.game_id,
        game_name=row.game_name,
        map_name=readable_text(status.map_name),
        outcome_text=outcome_text(status),
        game_time=status.game_time,
        flag_notes=report_flag_notes(status.flags),
        players=tuple(players),
    )


def describe_report_player(player: MatchReportPlayer, account_names: dict[int, str]) -> ReportPlayerRow:
    if player.is_computer:
        name = "Computer"
    else:
        name = account_names.get(player.lobby_player_id, f"player {player.lobby_player_id}")
    return ReportPlayerRow(
        name=name,
        slot=player.slot,
        team=player.team,
        race=RACE_NAMES.get(player.race, f"race {player.race}"),
        lobby_player_id=player.lobby_player_id,
        notes=player_flag_notes(player.flags),
    )


def outcome_text(status: GameStatus) -> str:
    """The game reports once at the start of the match and once when a single team is left. A game
    with one team has a single team left from the start, so its first report names a winner too."""
    if not status.has_winner:
        return "No winner yet"
    return f"Team {status.winning_team} won"


def report_flag_notes(flags: int) -> tuple[str, ...]:
    notes = []
    if flags & GameStatusFlag.LADDER_GAME:
        notes.append("ladder game, unranked here")
    if flags & GameStatusFlag.NOT_VALID_FOR_LADDER:
        notes.append("not valid for the ladder")
    unknown_flags = flags & ~(GameStatusFlag.LADDER_GAME | GameStatusFlag.NOT_VALID_FOR_LADDER)
    if unknown_flags:
        notes.append(f"flags {unknown_flags:#x}")
    return tuple(notes)


def player_flag_notes(flags: int) -> tuple[str, ...]:
    """The slot's flags in words (MatchPlayerFlag, 0x540625..0x540680)."""
    notes = []
    if flags & MatchPlayerFlag.COMPUTER:
        notes.append("computer")
    if flags & MatchPlayerFlag.SLOT_STATE_3:
        notes.append("left (inferred)")
    if flags & MatchPlayerFlag.NO_PLAYER_NUMBER:
        notes.append("no player number")
    known_flags = MatchPlayerFlag.COMPUTER | MatchPlayerFlag.SLOT_STATE_3 | MatchPlayerFlag.NO_PLAYER_NUMBER
    unknown_flags = flags & ~known_flags
    if unknown_flags:
        notes.append(f"flags {unknown_flags:#x}")
    return tuple(notes)
