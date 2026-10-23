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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from http import HTTPStatus

from flask import Blueprint, Response, abort, render_template

from s3lobby.constants import (
    GAME_TICK_MS,
    RACE_NAMES,
    GameStatusFlag,
    LadderType,
    MatchPlayerFlag,
    MessageType,
)
from s3lobby.database import GameRecord, GameReportRecord, LobbyDatabase
from s3lobby.groups import game_description_of, game_settings_of, ladder_type_of
from s3lobby.ladder_book import LadderBook, ResultRow
from s3lobby.messages import GameStatus, GroupAnnouncement, MatchReportPlayer
from s3lobby.web.auth import redirect_visitors_to_logon
from s3lobby.web.lobby_view import NO_PLAYER_LIMITS, map_text
from s3lobby.web.paging import pagination_from_request
from s3lobby.web.storage import get_database
from s3lobby.wire import ProtocolError, RawMessage, readable_text

blueprint = Blueprint("games", __name__)
blueprint.before_request(redirect_visitors_to_logon)

# Games listed on an account's page, newest first.
ACCOUNT_GAME_COUNT = 50
RECENT_REPORT_COUNT = 100
REPORT_CONTENT_TYPE = "application/octet-stream"
# The host's choice in the game setup, as the game list names it (0x44b843, 0x44b97a).
# The game type in a table, where the long names would wrap every row.
SHORT_LADDER_TYPE_NAMES = {LadderType.NONE: "Normal", LadderType.INDIVIDUAL: "Liga", LadderType.CLANS: "Clanliga"}
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
    # The account of the slot's player, when it still exists (the lobby id is the account id).
    account_id: int | None = None


@dataclass(frozen=True)
class ReportRow:
    """One stored match report as the games pages show it. Only report_id, received_at,
    sender_name, group_id, size and the game are set when the report does not decode (is_readable
    False). game_time counts game ticks; winning_team is None while no winner is reported."""

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
    # The game time as minutes and seconds of play (GAME_TICK_MS per tick).
    played_text: str = ""
    winning_team: int | None = None
    flag_notes: tuple[str, ...] = ()
    players: tuple[ReportPlayerRow, ...] = ()


@dataclass(frozen=True)
class GameRow:
    """One stored game as the games pages show it: the setup decoded from its announcement (None
    when that is missing or does not decode), who joined, and its reports, oldest first."""

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
    # Who won, by name ("Greta won"), for the list; empty while the match is undecided.
    winner_text: str = ""
    # The game type in one word for the list: Normal, Liga or Clanliga.
    type_text: str = ""


@blueprint.get("/games")
def list_games() -> str:
    """The games page: the recent games and the recent match reports, each decoded as far as it goes."""
    database = get_database()
    account_names = {account.account_id: account.name for account in database.list_accounts()}
    pagination = pagination_from_request(database.count_games())
    games = database.list_recent_games(pagination.size, pagination.offset)
    game_names = {game.game_id: game.name for game in games}
    # Each game on this page is described from its own reports; describe_game sorts them oldest first.
    own_reports = [
        describe_report(report, account_names, game_names)
        for report in database.list_reports_of_games([game.game_id for game in games])
    ]
    # The newest reports of the whole lobby, newest first, on the first page only.
    recent_reports = []
    if not pagination.has_previous:
        recent_reports = [
            describe_report(report, account_names, game_names)
            for report in database.list_recent_game_reports(RECENT_REPORT_COUNT)
        ]
    return render_template(
        "games.html",
        games=[describe_game(game, own_reports) for game in games],
        reports=recent_reports,
        pagination=pagination,
    )


@dataclass(frozen=True)
class TeamRow:
    """One team of a match as the last report saw it."""

    team: int
    players: tuple[ReportPlayerRow, ...]
    has_won: bool


@dataclass(frozen=True)
class AccountGameRow:
    """A game one account joined, as its account page lists it. team, race and result come from
    the match's last report and are empty for a game whose match was not played."""

    game_id: int
    name: str
    host_name: str
    opened_at: datetime
    team: int | None = None
    race: str = ""
    result: str = ""
    # The other side's players by name, the game type in one word, and how long the match ran.
    opponents: tuple[str, ...] = ()
    type_text: str = ""
    played_text: str = ""


def games_of_account(account_id: int) -> list[AccountGameRow]:
    """The games an account joined, with its own team, race and result in each."""
    database = get_database()
    account_names = {account.account_id: account.name for account in database.list_accounts()}
    games = database.list_games_of_account(account_id, ACCOUNT_GAME_COUNT)
    reports = [
        describe_report(report, account_names, {})
        for report in database.list_reports_of_games([game.game_id for game in games])
    ]
    rows = []
    for game in games:
        own_reports = [report for report in reports if report.game_id == game.game_id and report.is_readable]
        rows.append(account_game_row(game, own_reports, account_id))
    return rows


def account_game_row(game: GameRecord, reports: list[ReportRow], account_id: int) -> AccountGameRow:
    """The game as an account's page lists it, with the account's team, race and result from the
    last readable report; without them when there is no report or the account had no slot in it."""
    row = AccountGameRow(
        game_id=game.game_id,
        name=game.name,
        host_name=game.host_name,
        opened_at=game.opened_at,
        type_text=short_type_text(game),
    )
    if not reports:
        return row
    # The database lists reports in the order they arrived, so the last one is the latest state.
    final_report = reports[-1]
    own_slot = None
    for player in final_report.players:
        if player.lobby_player_id == account_id:
            own_slot = player
            break
    if own_slot is None:
        return row
    result = "no winner reported"
    if final_report.winning_team is not None and own_slot.team == final_report.winning_team:
        result = "won"
    elif final_report.winning_team is not None:
        result = "lost"
    opponents = tuple(player.name for player in final_report.players if player.team != own_slot.team)
    played_text = final_report.played_text if final_report.winning_team is not None else ""
    return replace(
        row,
        team=own_slot.team,
        race=own_slot.race,
        result=result,
        opponents=opponents,
        played_text=played_text,
    )


@blueprint.get("/games/<int:game_id>")
def show_game(game_id: int) -> str:
    """Everything kept of one game: the setup, who joined when, how the match went and every
    report."""
    database = get_database()
    record = database.find_game(game_id)
    if record is None:
        abort(HTTPStatus.NOT_FOUND)
    account_names = {account.account_id: account.name for account in database.list_accounts()}
    reports = [
        describe_report(report, account_names, {record.game_id: record.name})
        for report in database.list_reports_of_game(game_id)
    ]
    game = describe_game(record, reports)
    readable_reports = [report for report in reports if report.is_readable]
    final_report = None
    if readable_reports:
        final_report = readable_reports[-1]
    decided_report = first_report_with_winner(readable_reports)
    return render_template(
        "game.html",
        game=game,
        players=database.list_game_players(game_id),
        reports=reports,
        final_report=final_report,
        decided_report=decided_report,
        teams=teams_of(final_report),
        winner_names=winner_names(final_report),
        ladder_result=ladder_result_of(database, record),
    )


def ladder_result_of(database: LobbyDatabase, record: GameRecord) -> ResultRow | None:
    """What the ladder made of a ladder game, None for a normal game or one without a result yet."""
    if not record.ladder_type:
        return None
    for row in LadderBook(database).result_rows():
        if row.game_id == record.game_id:
            return row
    return None


def first_report_with_winner(reports: list[ReportRow]) -> ReportRow | None:
    """The first report that names a winner after the start: when the match was decided. A
    game with one team names its winner already at the start (game time 0), which says nothing
    about when it ended, so a later report is preferred when there is one."""
    reports_with_winner = [report for report in reports if report.winning_team is not None]
    for report in reports_with_winner:
        if report.game_time > 0:
            return report
    if reports_with_winner:
        return reports_with_winner[0]
    return None


def teams_of(report: ReportRow | None) -> list[TeamRow]:
    """The slots of a report grouped into teams, in team order, each marked when it won."""
    if report is None:
        return []
    players_by_team: dict[int, list[ReportPlayerRow]] = {}
    for player in report.players:
        players_by_team.setdefault(player.team, []).append(player)
    return [
        TeamRow(team=team, players=tuple(players), has_won=team == report.winning_team)
        for team, players in sorted(players_by_team.items())
    ]


def winner_names(report: ReportRow | None) -> list[str]:
    """The names in the winning team of a report; empty while it names no winner."""
    if report is None or report.winning_team is None:
        return []
    return [player.name for player in report.players if player.team == report.winning_team]


@blueprint.get("/games/reports/<int:report_id>")
def download_report(report_id: int) -> Response:
    """The stored bytes of one match report as a file download, exactly as the game sent them."""
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
    readable_reports = [report for report in own_reports if report.is_readable]
    winners = winner_names(readable_reports[-1]) if readable_reports else []
    winner_text = f"{', '.join(winners)} won" if winners else ""
    type_text = short_type_text(game)
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
        winner_text=winner_text,
        type_text=type_text,
    )


def short_type_text(game: GameRecord) -> str:
    """The game type in one word: the ladder kept with the game, or for a game stored before the
    ladder type was kept, the one its stored setup names; empty without either."""
    ladder_type = game.ladder_type
    if ladder_type is None and game.announcement is not None:
        try:
            announcement = GroupAnnouncement.decode(RawMessage(MessageType.ADD_GROUP_TO_GROUP_2, game.announcement))
        # The bytes come from a host's game; a malformed announcement just has no type.
        except (ProtocolError, ValueError):
            return ""
        ladder_type = ladder_type_of(announcement) or LadderType.NONE
    if ladder_type is None:
        return ""
    return SHORT_LADDER_TYPE_NAMES.get(ladder_type, f"type {ladder_type}")


def decode_setup(announcement_body: bytes | None) -> GameSetup | None:
    """The host's setup from a stored announcement; None for games stored before the server kept
    it, or for bytes that do not decode. The description is the current setup (the host rewrites
    it on every change, 0x454db0); the game block holds the type chosen when the game was opened,
    the version and the session."""
    if announcement_body is None:
        return None
    try:
        announcement = GroupAnnouncement.decode(RawMessage(MessageType.ADD_GROUP_TO_GROUP_2, announcement_body))
    # The bytes come from a host's game; a malformed announcement must not take the page down.
    except (ProtocolError, ValueError):
        return None
    description = game_description_of(announcement)
    settings = game_settings_of(announcement)

    map_name = ""
    team_count = None
    computer_opponent_count = None
    game_type = ""
    if description is not None:
        map_name = map_text(readable_text(description.map_name))
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
    # Without a readable description the block is the only source for the type, even if outdated.
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
        # A Windows GUID keeps its first three fields little endian, hence bytes_le.
        session_guid=str(uuid.UUID(bytes_le=settings.session_guid)),
    )


def max_players_text(max_players: int) -> str:
    """The player limit for the page; 0 and UNLIMITED_PLAYERS both mean no limit."""
    if max_players in NO_PLAYER_LIMITS:
        return "no limit"
    return str(max_players)


def version_text(game_version: int) -> str:
    """The game's version number as the game writes it: 160 becomes "1.60"."""
    whole, hundredths = divmod(game_version, VERSION_DIVISOR)
    return f"{whole}.{hundredths:02d}"


def open_duration_text(opened_at: datetime, closed_at: datetime | None) -> str:
    """How long a closed game was open, in hours, minutes and seconds as far as they matter."""
    if closed_at is None:
        return ""
    return duration_text(closed_at - opened_at)


def duration_text(duration: timedelta) -> str:
    """A duration in its two largest units that matter: "1 h 5 min", "3 min 20 s" or "45 s"."""
    total_seconds = max(int(duration.total_seconds()), 0)
    hours, rest = divmod(total_seconds, SECONDS_PER_HOUR)
    minutes, seconds = divmod(rest, SECONDS_PER_MINUTE)
    if hours:
        return f"{hours} h {minutes} min"
    # Whole minutes read "40 min", not "40 min 0 s".
    if minutes and seconds:
        return f"{minutes} min {seconds} s"
    if minutes:
        return f"{minutes} min"
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
    # Teammates next to each other, in slot order within a team, as the page lists them.
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
        played_text=duration_text(timedelta(milliseconds=status.game_time * GAME_TICK_MS)),
        winning_team=status.winning_team if status.has_winner else None,
        flag_notes=report_flag_notes(status.flags),
        players=tuple(players),
    )


def describe_report_player(player: MatchReportPlayer, account_names: dict[int, str]) -> ReportPlayerRow:
    """One slot of a report as the page lists it, named after its account when the lobby player id
    is one; a computer opponent is named "Computer"."""
    account_id = None
    if player.is_computer:
        name = "Computer"
    elif player.lobby_player_id in account_names:
        name = account_names[player.lobby_player_id]
        account_id = player.lobby_player_id
    else:
        name = f"player {player.lobby_player_id}"
    return ReportPlayerRow(
        account_id=account_id,
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
    """The report's flags in words (GameStatusFlag); bits without a name are shown as a number."""
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
        notes.append("left the match")
    if flags & MatchPlayerFlag.RANDOM_RACE:
        notes.append("race drawn at random")
    known_flags = MatchPlayerFlag.COMPUTER | MatchPlayerFlag.SLOT_STATE_3 | MatchPlayerFlag.RANDOM_RACE
    unknown_flags = flags & ~known_flags
    if unknown_flags:
        notes.append(f"flags {unknown_flags:#x}")
    return tuple(notes)
