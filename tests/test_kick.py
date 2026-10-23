"""Removing players from the running lobby: kicks, bans of players who are online, and shutdown.

The server closes the connection with CloseConnection. The game's ConnectionClosed handler then
logs out and shows "The connection to the lobby server dropped." (0x5cc840, 0x5cc972, text
0x900a7). Without the close, a kicked or banned player would stay in the lobby until they left on
their own, and a stopped server would leave every player waiting a minute for a timeout.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from s3lobby.constants import ANNOUNCEMENT_GROUP_ID, MessageType
from s3lobby.database import utc_now
from s3lobby.messages import AddPlayerToGroup, ChatCommand, ChatCommandAnswer, RemovePlayerFromGroup
from s3lobby.wire import RawMessage
from tests.support import GAME_ZONE_ID, LobbyTestCase, log_on, logon_request


class KickTest(LobbyTestCase):
    def setUp(self) -> None:
        super().setUp()
        alice_account = self.database.find_account("Alice")
        self.database.set_admin(alice_account.account_id, True)
        self.alice, self.alice_id = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        self.bob, self.bob_id = self.player_in_room("Bob", 42, GAME_ZONE_ID)

    def test_admin_kicks_from_the_chat_and_the_others_see_the_player_leave(self) -> None:
        self.alice.send(ChatCommand(group_id=GAME_ZONE_ID, unknown_value=0, command="/kick Bob spamming"))
        reason = ChatCommandAnswer.decode(self.bob.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertEqual(reason.group_id, ANNOUNCEMENT_GROUP_ID)
        self.assertEqual(reason.lines, ("An admin removed you from the lobby: spamming",))
        self.bob.receive_next(MessageType.CLOSE_CONNECTION)

        departure = RemovePlayerFromGroup.decode(self.alice.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP))
        self.assertEqual((departure.group_id, departure.player_id), (GAME_ZONE_ID, self.bob_id))
        answer = ChatCommandAnswer.decode(self.alice.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertEqual(answer.lines, ("Removed Bob from the lobby.",))

    def test_ban_from_another_process_removes_a_player_who_is_online(self) -> None:
        """The command line and the web interface only write the database; the server takes the
        kick a ban queues within a second."""
        bob_account = self.database.find_account("Bob")
        self.database.ban_account(bob_account.account_id, utc_now() + timedelta(hours=1), "cheating")
        reason = ChatCommandAnswer.decode(self.bob.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertIn("cheating", " ".join(reason.lines))
        self.bob.receive_next(MessageType.CLOSE_CONNECTION)
        self.assertNotIn("Bob", [player.name for player in self.wait_for_players_online(["Alice"])])

    def test_kicked_client_cannot_act_before_it_notices(self) -> None:
        """The client may still send a request before it handles the CloseConnection; the server
        must not let that bring the player back."""

        def is_bob_joining(message: RawMessage) -> bool:
            if message.message_type != MessageType.ADD_PLAYER_TO_GROUP:
                return False
            return AddPlayerToGroup.decode(message).player_id == self.bob_id

        # Bob's first arrival in the zone, from setUp.
        self.alice.receive_matching(is_bob_joining)
        self.database.queue_kick(self.database.find_account("Bob").account_id, "test")
        self.bob.receive_next(MessageType.CLOSE_CONNECTION)
        self.bob.send(logon_request(self.bob), AddPlayerToGroup(GAME_ZONE_ID, self.bob_id, ("0.0.0.0", 0), ""))
        self.alice.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP)
        self.alice.assert_nothing_matching(is_bob_joining)

    def test_kick_for_a_player_who_is_offline_is_dropped(self) -> None:
        carol_account = self.database.find_account("Carol")
        self.database.queue_kick(carol_account.account_id, "not here")
        self.alice.assert_nothing_arrives(MessageType.REMOVE_PLAYER_FROM_GROUP)
        carol = self.new_client("Carol", 77)
        self.assertEqual(log_on(carol), carol_account.account_id, "a dropped kick does not hit a later logon")

    def test_stopping_the_server_closes_every_connection(self) -> None:
        self.server.stop()
        self.alice.receive_next(MessageType.CLOSE_CONNECTION)
        self.bob.receive_next(MessageType.CLOSE_CONNECTION)

    def wait_for_players_online(self, expected_names: list[str]) -> list:
        """The players the server's live state lists once it matches expected_names."""
        live_state = None
        for _ in range(60):
            stored = self.database.read_live_state()
            if stored is not None:
                live_state = stored.state
                if sorted(player.name for player in live_state.players) == sorted(expected_names):
                    return live_state.players
            self.alice.assert_nothing_arrives(MessageType.CLOSE_CONNECTION)
        self.fail(f"the live lobby never listed {expected_names}; last: {live_state}")


if __name__ == "__main__":
    unittest.main()
