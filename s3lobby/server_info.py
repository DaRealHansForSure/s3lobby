"""What an admin wants to know about this installation at a glance: the software and its version,
the database and its schema, whether the server runs, and the lobby's numbers.

The web interface's Info page and `s3lobby info` both show what collect_server_info gathers, so
the lobby tells the same with and without the web interface. Everything here only reads.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import datetime

from s3lobby import __version__
from s3lobby.constants import LIVE_STATE_STALE_SECONDS, LadderState
from s3lobby.database import BUILT_IN_CHAT_THEMES, LobbyDatabase, Setting
from s3lobby.ladder_book import LadderBook, LadderMembers, ladder_members

# The settings whose state an admin most often needs to know: the ones that are off until a run
# with the game confirms them, and the ones that open the lobby to more people.
WATCHED_SETTINGS = (
    Setting.LADDER_ENABLED,
    Setting.PERMANENT_CHAT_GROUPS,
    Setting.ALLOW_CREATE_PLAYER,
    Setting.CHECK_REACHABILITY,
    Setting.MARK_ADMINS,
    Setting.UNIQUE_SERIAL_NUMBERS,
)


@dataclass(frozen=True)
class AppliedMigrationRow:
    """One migration the database has applied, and when."""

    version: int
    name: str
    applied_at: str


@dataclass(frozen=True)
class ServerInfo:
    """Everything the Info page and `s3lobby info` show."""

    version: str
    python_version: str
    system: str
    database: str
    dialect: str
    schema_version: int
    applied_migrations: tuple[AppliedMigrationRow, ...]
    pending_migration_count: int
    is_running: bool
    server_name: str
    live_state_written_at: datetime | None
    players_online: int
    players_last_hour: int
    players_last_day: int
    games_open: int
    games_last_hour: int
    games_last_day: int
    account_count: int
    admin_count: int
    banned_count: int
    clan_count: int
    chat_theme_count: int
    permanent_group_count: int
    board_theme_count: int
    board_message_count: int
    game_count: int
    report_count: int
    counted_result_count: int
    disputed_result_count: int
    void_result_count: int
    ladder_members: tuple[LadderMembers, ...]
    settings: tuple[tuple[str, bool], ...]


def collect_server_info(database: LobbyDatabase) -> ServerInfo:
    """Gather the installation's facts from the database and the running Python."""
    now = database.now()
    # The schema: its version, and each migration with the time it was applied.
    status = database.read_migration_status()
    applied = tuple(
        AppliedMigrationRow(migration.version, migration.name, migration.applied_at) for migration in status.applied
    )
    # Whether the server runs: it rewrites the live state at least every few seconds.
    stored = database.read_live_state()
    is_running = stored is not None and (now - stored.written_at).total_seconds() <= LIVE_STATE_STALE_SECONDS
    server_name = stored.state.server_name if stored is not None else ""
    # Players and games now, in the last hour and the last day, as the Home tab counts them.
    statistics = database.collect_statistics()
    # What the lobby holds: accounts, clans, content, games and the ladder's results.
    accounts = database.list_accounts()
    board_themes = database.list_board_themes()
    results = LadderBook(database).results()
    states = [record.evaluation.state for record in results]
    return ServerInfo(
        version=__version__,
        python_version=platform.python_version(),
        system=platform.platform(terse=True),
        database=database.description,
        dialect=database.dialect,
        schema_version=database.schema_version,
        applied_migrations=applied,
        pending_migration_count=len(status.pending),
        is_running=is_running,
        server_name=server_name,
        live_state_written_at=stored.written_at if stored is not None else None,
        players_online=statistics.players_online,
        players_last_hour=statistics.players_last_hour,
        players_last_day=statistics.players_last_day,
        games_open=statistics.games_open,
        games_last_hour=statistics.games_last_hour,
        games_last_day=statistics.games_last_day,
        account_count=len(accounts),
        admin_count=sum(1 for account in accounts if account.is_admin),
        banned_count=sum(1 for account in accounts if account.is_banned_at(now)),
        clan_count=len(database.list_clans()),
        chat_theme_count=len(BUILT_IN_CHAT_THEMES) + len(database.list_chat_themes()),
        permanent_group_count=len(database.list_permanent_chat_groups()),
        board_theme_count=len(board_themes),
        board_message_count=sum(len(database.list_board_messages(theme.theme_id)) for theme in board_themes),
        game_count=database.count_games(),
        report_count=database.count_game_reports(),
        counted_result_count=states.count(LadderState.COUNTED),
        disputed_result_count=states.count(LadderState.DISPUTED),
        void_result_count=states.count(LadderState.VOID),
        ladder_members=tuple(ladder_members(database)),
        settings=tuple((setting.key, database.get_boolean_setting(setting)) for setting in WATCHED_SETTINGS),
    )
