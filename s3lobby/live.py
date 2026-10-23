"""A picture of the running lobby, for other processes to show: who is online, which games are open.

The server keeps its lobby in memory. Once a second it turns that state into a LiveLobbyState and,
when something changed, writes it to the database as JSON (see LobbyDatabase.write_live_state).
The web interface reads it from there, so the two processes share nothing but the database.

Only what an administrator needs to see goes in: names, rooms and games. Chat group passwords and
access codes never do.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields as dataclass_fields, replace
from datetime import datetime


@dataclass(frozen=True)
class LivePlayer:
    """A logged on player. address is "ip:port" as the server sees it; group_ids are the rooms,
    games and chat groups the player is in."""

    player_id: int
    name: str
    clan: str
    is_admin: bool
    address: str
    logged_on_at: str
    group_ids: tuple[int, ...] = ()
    # A Reachability value (reachability.py); the default keeps older states readable.
    reachability: str = "not checked"
    # The colour of the player's row in the game's member lists (groups.game_row_colour).
    row_colour: str = ""


@dataclass(frozen=True)
class LiveGroup:
    """A room, game or chat group. kind is a GroupKind value; listed_in_ids are the rooms that
    show the group in their list (the game zones of a game, the chat theme of a chat group).

    A game also has what its description says: the map (a backslash in front for a random map),
    the number of teams, and whether it is a ladder game. They have defaults, so a state written
    by a server from before they existed still reads.
    """

    group_id: int
    kind: str
    name: str
    max_players: int
    is_full: bool
    has_password: bool
    member_names: tuple[str, ...] = ()
    listed_in_ids: tuple[int, ...] = ()
    map_name: str = ""
    team_count: int = 0
    is_ladder_game: bool = False


@dataclass(frozen=True)
class LiveLobbyState:
    """Everything the server writes about its running lobby, sorted so that the same lobby always
    gives the same JSON text: comparing the text then tells whether anything changed."""

    server_name: str
    players: tuple[LivePlayer, ...] = ()
    groups: tuple[LiveGroup, ...] = ()

    def to_json(self) -> str:
        """The state as JSON text, keys sorted, so the same lobby always gives the same text."""
        # ensure_ascii=False stores names such as "Müller" as they are rather than as \u escapes.
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> LiveLobbyState:
        """Read what to_json wrote. Raises ValueError for text that is not such a state."""
        try:
            fields = json.loads(text)
            players = tuple(live_player_from_fields(player) for player in fields["players"])
            groups = tuple(live_group_from_fields(group) for group in fields["groups"])
            return cls(server_name=fields["server_name"], players=players, groups=groups)
        # KeyError for a missing part, TypeError for JSON of another shape and for a field the
        # dataclasses do not know (a state written by a newer server).
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"not a live lobby state: {error}") from error

    def group_names_by_id(self) -> dict[int, str]:
        """The name of every group by its id, for showing the rooms and games a player is in."""
        return {group.group_id: group.name for group in self.groups}

    def groups_of_kind(self, kind: str) -> list[LiveGroup]:
        """The groups of one GroupKind value, such as only the games."""
        return [group for group in self.groups if group.kind == kind]


def live_player_from_fields(fields: dict) -> LivePlayer:
    """JSON has lists where the state has tuples. Fields this version does not know (written by a
    newer server) are left out, so the web page still shows the lobby."""
    player = LivePlayer(**known_fields(LivePlayer, fields))
    return replace(player, group_ids=tuple(player.group_ids))


def known_fields(record_class: type, fields: dict) -> dict:
    """The fields of a stored record that record_class has; missing ones take their defaults."""
    names = {record_field.name for record_field in dataclass_fields(record_class)}
    return {name: value for name, value in fields.items() if name in names}


def live_group_from_fields(fields: dict) -> LiveGroup:
    """A LiveGroup from its JSON fields; JSON has lists where the state has tuples. Unknown fields
    are left out, as for players."""
    group = LiveGroup(**known_fields(LiveGroup, fields))
    return replace(group, member_names=tuple(group.member_names), listed_in_ids=tuple(group.listed_in_ids))


@dataclass(frozen=True)
class StoredLiveState:
    """The live state as read back from the database, with the moment the server wrote it. A
    running server rewrites it at least every LIVE_STATE_HEARTBEAT_SECONDS, so an old written_at
    means the server is not running."""

    state: LiveLobbyState
    written_at: datetime
