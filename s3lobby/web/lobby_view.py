"""The live lobby: who is online, which games and chat groups are open, who is in which room.

Everyone may look; admins also see each player's address. The page reloads its live part every
few seconds (static/app.js) from /live/state, which renders the same part on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from flask import Blueprint, render_template

from s3lobby.constants import LIVE_STATE_STALE_SECONDS, LOBBY_TAB_GROUP_NAMES, UNLIMITED_PLAYERS
from s3lobby.database import utc_now
from s3lobby.groups import GroupKind
from s3lobby.live import LiveGroup, LiveLobbyState, StoredLiveState
from s3lobby.web.storage import get_database

blueprint = Blueprint("lobby_view", __name__)

# A maximum player count of 0 or UNLIMITED_PLAYERS means the group takes any number of players.
NO_PLAYER_LIMITS = frozenset({0, UNLIMITED_PLAYERS})
# A game description names a random map with a backslash in front of the random map's name
# (0x4616da..0x461796).
RANDOM_MAP_PREFIX = "\\"


@dataclass(frozen=True)
class PlayerRow:
    """A player, with the lobby tab it has open and the games and chat groups it is in."""

    name: str
    clan: str
    is_admin: bool
    address: str
    logged_on_at: str
    tab_names: tuple[str, ...]
    group_names: tuple[str, ...]
    reachability: str
    # The colour of the player's row in the game's member lists (groups.game_row_colour).
    row_colour: str = ""


@dataclass(frozen=True)
class GameRow:
    """An open game. A game listed in no zone has started: the host takes it out of the zones'
    lists when the match begins."""

    name: str
    member_names: tuple[str, ...]
    player_count_text: str
    zone_names: tuple[str, ...]
    is_full: bool
    has_started: bool
    map_text: str
    team_count: int
    is_ladder_game: bool


@dataclass(frozen=True)
class ChatGroupRow:
    """A chat group: the themes it is listed in, its members, and whether it needs a password."""

    name: str
    theme_names: tuple[str, ...]
    member_names: tuple[str, ...]
    has_password: bool


@dataclass(frozen=True)
class RoomRow:
    """A room of the lobby (GroupKind.ROOM) with the players in it; the page leaves out empty rooms."""

    name: str
    member_names: tuple[str, ...]


@dataclass(frozen=True)
class LiveLobbyPage:
    """What the live lobby page shows. is_running is False when no server wrote a state lately."""

    is_running: bool
    server_name: str = ""
    written_at: datetime | None = None
    players: tuple[PlayerRow, ...] = ()
    games: tuple[GameRow, ...] = ()
    chat_groups: tuple[ChatGroupRow, ...] = ()
    rooms: tuple[RoomRow, ...] = ()


@blueprint.get("/")
def live_lobby() -> str:
    """The public live lobby page: who is online, the open games and the chat groups."""
    return render_template("live.html", page=read_live_lobby_page())


@blueprint.get("/live/state")
def live_state_part() -> str:
    """Only the live part of the page, for the page's script to swap in."""
    return render_template("_live_state.html", page=read_live_lobby_page())


def read_live_lobby_page() -> LiveLobbyPage:
    """The live lobby as the server last wrote it to the database, ready for the page."""
    return describe_live_lobby(get_database().read_live_state(), utc_now())


def online_player_names() -> set[str]:
    """Names of the players in the running lobby; empty when the server is not running."""
    return {player.name for player in read_live_lobby_page().players}


def describe_live_lobby(stored: StoredLiveState | None, now: datetime) -> LiveLobbyPage:
    """Turn the state the server wrote into the page's tables."""
    if stored is None:
        return LiveLobbyPage(is_running=False)
    # A running server rewrites the state at least every LIVE_STATE_HEARTBEAT_SECONDS; an older one
    # was left by a server that stopped without clearing it, for example after a crash.
    age_seconds = (now - stored.written_at).total_seconds()
    if age_seconds > LIVE_STATE_STALE_SECONDS:
        return LiveLobbyPage(is_running=False, server_name=stored.state.server_name, written_at=stored.written_at)

    state = stored.state
    names_by_id = state.group_names_by_id()
    return LiveLobbyPage(
        is_running=True,
        server_name=state.server_name,
        written_at=stored.written_at,
        players=describe_players(state, names_by_id),
        games=tuple(describe_game(group, names_by_id) for group in state.groups_of_kind(GroupKind.GAME.value)),
        chat_groups=tuple(
            describe_chat_group(group, names_by_id) for group in state.groups_of_kind(GroupKind.CHAT_GROUP.value)
        ),
        rooms=tuple(
            RoomRow(name=group.name, member_names=group.member_names)
            for group in state.groups_of_kind(GroupKind.ROOM.value)
            if group.member_names
        ),
    )


def describe_players(state: LiveLobbyState, names_by_id: dict[int, str]) -> tuple[PlayerRow, ...]:
    """Rooms other than the four tabs are left out: a player on the GameZone tab is in all nine
    zones at once, which says nothing about where it is."""
    game_and_chat_group_ids = {
        group.group_id
        for group in state.groups
        if group.kind in (GroupKind.GAME.value, GroupKind.CHAT_GROUP.value)
    }
    rows = []
    for player in state.players:
        tab_names = tuple(
            LOBBY_TAB_GROUP_NAMES[group_id] for group_id in player.group_ids if group_id in LOBBY_TAB_GROUP_NAMES
        )
        group_names = tuple(
            names_by_id[group_id] for group_id in player.group_ids if group_id in game_and_chat_group_ids
        )
        rows.append(
            PlayerRow(
                name=player.name,
                clan=player.clan,
                is_admin=player.is_admin,
                address=player.address,
                logged_on_at=player.logged_on_at,
                tab_names=tab_names,
                group_names=group_names,
                reachability=player.reachability,
                row_colour=player.row_colour,
            )
        )
    return tuple(rows)


def describe_game(game: LiveGroup, names_by_id: dict[int, str]) -> GameRow:
    """An open game from the live state as the page lists it."""
    return GameRow(
        name=game.name,
        member_names=game.member_names,
        player_count_text=player_count_text(game),
        zone_names=listed_in_names(game, names_by_id),
        is_full=game.is_full,
        has_started=not game.listed_in_ids,
        map_text=map_text(game.map_name),
        team_count=game.team_count,
        is_ladder_game=game.is_ladder_game,
    )


def describe_chat_group(chat_group: LiveGroup, names_by_id: dict[int, str]) -> ChatGroupRow:
    """A chat group from the live state as the page lists it."""
    return ChatGroupRow(
        name=chat_group.name,
        theme_names=listed_in_names(chat_group, names_by_id),
        member_names=chat_group.member_names,
        has_password=chat_group.has_password,
    )


def listed_in_names(group: LiveGroup, names_by_id: dict[int, str]) -> tuple[str, ...]:
    """The names of the groups this group is listed in (a game's zones, a chat group's themes)."""
    return tuple(names_by_id[parent_id] for parent_id in group.listed_in_ids if parent_id in names_by_id)


def map_text(map_name: str) -> str:
    """The map as the page shows it: "Random (random map)" for a random map."""
    if map_name.startswith(RANDOM_MAP_PREFIX):
        return f"{map_name[len(RANDOM_MAP_PREFIX):]} (random map)"
    return map_name


def player_count_text(group: LiveGroup) -> str:
    """"2 / 4" for a game with a limit, "3" for one without."""
    player_count = len(group.member_names)
    if group.max_players in NO_PLAYER_LIMITS:
        return str(player_count)
    return f"{player_count} / {group.max_players}"
