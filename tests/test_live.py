"""The live lobby the server writes to the database for the web interface."""

from __future__ import annotations

import time
import unittest
from collections.abc import Callable

from s3lobby.constants import FIRST_GAME_GROUP_ID, LIVE_STATE_WRITE_INTERVAL_SECONDS, MessageType
from s3lobby.database import LobbyDatabase
from s3lobby.groups import GroupKind
from s3lobby.live import LiveGroup, LiveLobbyState, LivePlayer, StoredLiveState
from s3lobby.messages import GameDescription
from tests.support import GAME_ZONE_ID, LobbyTestCase, join_group, log_on, open_game

# The server checks once a second; three checks are plenty on a slow test machine.
LIVE_STATE_WAIT_SECONDS = 3 * LIVE_STATE_WRITE_INTERVAL_SECONDS
POLL_INTERVAL_SECONDS = 0.05

EXAMPLE_STATE = LiveLobbyState(
    server_name="Test Lobby",
    players=(
        LivePlayer(
            player_id=1,
            name="Alice",
            clan="Romans",
            is_admin=False,
            address="127.0.0.1:2300",
            logged_on_at="2026-10-23T12:00:00+00:00",
            group_ids=(3, 30),
        ),
    ),
    groups=(
        LiveGroup(
            group_id=30,
            kind=GroupKind.ROOM.value,
            name="League 1 vs 1",
            max_players=0,
            is_full=False,
            has_password=False,
            member_names=("Alice",),
        ),
    ),
)


def has_alices_game(state: LiveLobbyState) -> bool:
    """Whether the state lists the game Alice opened."""
    return any(group.group_id == FIRST_GAME_GROUP_ID for group in state.groups)

class LiveLobbyStateTest(unittest.TestCase):
    """The live lobby as JSON and its row in the database, without a server."""

    def test_json_round_trip_keeps_every_field(self) -> None:
        """The web interface reads exactly what the server wrote; a field lost in JSON would vanish from the
        live page."""
        self.assertEqual(LiveLobbyState.from_json(EXAMPLE_STATE.to_json()), EXAMPLE_STATE)

    def test_state_from_a_server_before_the_game_fields_still_reads(self) -> None:
        """The web interface may be newer than the running server; a game then simply shows no map."""
        old_text = (
            '{"server_name": "x", "players": [], "groups": [{"group_id": 32768, "kind": "game", "name": "g", '
            '"max_players": 2, "is_full": false, "has_password": false, "member_names": [], "listed_in_ids": []}]}'
        )
        [game] = LiveLobbyState.from_json(old_text).groups
        self.assertEqual((game.map_name, game.team_count, game.is_ladder_game), ("", 0, False))

    def test_text_that_is_not_a_state_is_refused(self) -> None:
        """Text that is not a whole live state raises ValueError, which the web interface can handle, rather
        than showing a half read lobby."""
        for text in ("", "[]", '{"server_name": "x"}', '{"server_name": "x", "players": [{}], "groups": []}'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    LiveLobbyState.from_json(text)

    def test_database_keeps_the_last_state_until_cleared(self) -> None:
        """The database holds one live state, replaced by each write, until the server clears it; before the
        first write there is none."""
        database = LobbyDatabase(":memory:")
        self.addCleanup(database.close)
        self.assertIsNone(database.read_live_state())
        database.write_live_state(EXAMPLE_STATE)
        database.write_live_state(EXAMPLE_STATE)
        self.assertEqual(database.read_live_state().state, EXAMPLE_STATE)
        database.clear_live_state()
        self.assertIsNone(database.read_live_state())


class ServerWritesLiveStateTest(LobbyTestCase):
    """What a running server writes as its live lobby, read back as the web interface reads it."""

    def wait_for_live_state(self, is_wanted: Callable[[LiveLobbyState], bool]) -> StoredLiveState:
        """The live state once the server has written one is_wanted accepts; fails after
        LIVE_STATE_WAIT_SECONDS."""
        deadline = time.monotonic() + LIVE_STATE_WAIT_SECONDS
        stored = None
        while time.monotonic() < deadline:
            stored = self.database.read_live_state()
            if stored is not None and is_wanted(stored.state):
                return stored
            time.sleep(POLL_INTERVAL_SECONDS)
        self.fail(f"the server did not write the expected live state; last read: {stored}")

    def test_running_server_writes_its_lobby_at_once(self) -> None:
        """A server writes its lobby right after it starts, with its name and its fixed rooms, so the live
        page is not empty until the first player comes."""
        stored = self.wait_for_live_state(lambda state: True)
        self.assertEqual((stored.state.server_name, stored.state.players), ("Test Lobby", ()))
        room_names = {group.name for group in stored.state.groups_of_kind(GroupKind.ROOM.value)}
        self.assertTrue({"Home", "GameZone", "League 1 vs 1"} <= room_names)

    def test_players_and_their_games_show_up(self) -> None:
        """A logged on player and the game they opened appear in the live state, with the player in the game,
        the game's members and the zone it is listed in."""
        alice = self.new_client("Alice", nonce=17)
        alice_id = log_on(alice)
        join_group(alice, GAME_ZONE_ID, alice_id)
        open_game(alice, GAME_ZONE_ID)
        alice.receive_next(MessageType.ADD_GROUP_TO_GROUP_2)

        state = self.wait_for_live_state(has_alices_game).state
        self.assertEqual([player.name for player in state.players], ["Alice"])
        self.assertIn(FIRST_GAME_GROUP_ID, state.players[0].group_ids)
        game = state.groups_of_kind(GroupKind.GAME.value)[0]
        self.assertEqual((game.name, game.member_names, game.listed_in_ids), ("Alice's game", ("Alice",), (GAME_ZONE_ID,)))

    def test_game_shows_what_its_description_says(self) -> None:
        """A game's map, team count and ladder flag are taken from the description the host sends, so the live
        page can show them."""
        alice = self.new_client("Alice", nonce=17)
        alice_id = log_on(alice)
        join_group(alice, GAME_ZONE_ID, alice_id)
        description = GameDescription(team_count=2, computer_opponent_count=1, is_ladder_game=True, map_name="\\Random")
        open_game(alice, GAME_ZONE_ID, description=description)
        alice.receive_next(MessageType.ADD_GROUP_TO_GROUP_2)

        [game] = self.wait_for_live_state(has_alices_game).state.groups_of_kind(GroupKind.GAME.value)
        self.assertEqual((game.map_name, game.team_count, game.is_ladder_game), ("\\Random", 2, True))

    def test_stopped_server_leaves_no_live_state(self) -> None:
        """A stopped server clears its live state, so the web interface does not show players of a lobby that
        is gone."""
        self.wait_for_live_state(lambda state: True)
        self.server.stop()
        self.assertIsNone(self.database.read_live_state())


if __name__ == "__main__":
    unittest.main()
