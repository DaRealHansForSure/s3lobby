"""A picture of the running lobby, for other processes to show: who is online, which games are open.

The server keeps its lobby in memory. Once a second it turns that state into a LiveLobbyState and,
when something changed, writes it to the database as JSON (see LobbyDatabase.write_live_state).
The web interface reads it from there, so the two processes share nothing but the database.

Only what an administrator needs to see goes in: names, rooms and games. Chat group passwords and
access codes never do.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
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
    group_ids: list[int] = field(default_factory=list)


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
    member_names: list[str] = field(default_factory=list)
    listed_in_ids: list[int] = field(default_factory=list)
    map_name: str = ""
    team_count: int = 0
    is_ladder_game: bool = False


@dataclass(frozen=True)
class LiveLobbyState:
    """Everything the server writes about its running lobby, sorted so that the same lobby always
    gives the same JSON text: comparing the text then tells whether anything changed."""

    server_name: str
    players: list[LivePlayer] = field(default_factory=list)
    groups: list[LiveGroup] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> LiveLobbyState:
        """Read what to_json wrote. Raises ValueError for text that is not such a state."""
        try:
            fields = json.loads(text)
            return cls(
                server_name=fields["server_name"],
                players=[LivePlayer(**player) for player in fields["players"]],
                groups=[LiveGroup(**group) for group in fields["groups"]],
            )
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"not a live lobby state: {error}") from error

    def group_names_by_id(self) -> dict[int, str]:
        return {group.group_id: group.name for group in self.groups}

    def groups_of_kind(self, kind: str) -> list[LiveGroup]:
        return [group for group in self.groups if group.kind == kind]


@dataclass(frozen=True)
class StoredLiveState:
    """The live state as read back from the database, with the moment the server wrote it. A
    running server rewrites it at least every LIVE_STATE_HEARTBEAT_SECONDS, so an old written_at
    means the server is not running."""

    state: LiveLobbyState
    written_at: datetime
