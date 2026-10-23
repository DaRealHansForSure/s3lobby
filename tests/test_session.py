"""Whole lobby sessions against a running server, played by fake game clients.

Each test covers one behaviour the real game depends on; the docstrings say what breaks in the
game when the server gets it wrong.
"""

from __future__ import annotations

import unittest

from s3lobby.constants import (
    FIRST_GAME_GROUP_ID,
    JOINED_GROUP_PARENT_ID,
    LOGON_PROTOCOL_VERSION,
    LogonFlag,
    MessageType,
    ResultCode,
)
from s3lobby.messages import (
    AddPlayerToGroupWithClan,
    CheckFileVersion,
    GetLobbyServerList,
    GroupAnnouncement,
    LobbyServerList,
    Logon,
    OwnPlayerWithClan,
    RemoveGroupFromGroup,
    RemovePlayerFromGroup,
    Result,
)
from tests.support import (
    ACCESS_CODE,
    EDITION_MISSION_AND_GOLD,
    GAME_VERSION,
    GAME_ZONE_ID,
    LOBBY_ROOM_ID,
    SERIAL_NUMBER,
    UNKNOWN_HOST_ADDRESS,
    LobbyTestCase,
    go_to_lobby_server,
    join_group,
    log_on,
    open_game,
    receive_member_entry,
    send_logon,
)


class LobbySessionTest(LobbyTestCase):
    def test_patch_check_says_no_patch_and_the_list_names_this_server(self) -> None:
        """Without Result NO_PATCH_NEEDED the client stops with "patch check failed"; an empty
        server list stops it with error 0x1e."""
        client = self.new_client("Alice", nonce=17)
        client.send(CheckFileVersion(game_version=GAME_VERSION, edition=EDITION_MISSION_AND_GOLD, unknown_setting=0))
        self.assertEqual(Result.decode(client.receive_next(MessageType.RESULT)).code, ResultCode.NO_PATCH_NEEDED)

        client.send(GetLobbyServerList())
        server_list = LobbyServerList.decode(client.receive_next(MessageType.LOBBY_SERVER_LIST))
        self.assertEqual(len(server_list.servers), 1)
        self.assertEqual(server_list.servers[0].address_text, f"127.0.0.1:{self.server.address[1]}")
        self.assertEqual(server_list.servers[0].name, "Test Lobby")

    def test_logon_answers_result_before_own_player(self) -> None:
        """OwnPlayer's handler asserts the sender is the client's server, which only the Result
        LOGON_ACCEPTED establishes: the other order drops the connection."""
        client = self.new_client("Alice", nonce=17)
        log_on_messages = {MessageType.RESULT, MessageType.OWN_PLAYER}
        client.send(CheckFileVersion(game_version=GAME_VERSION, edition=EDITION_MISSION_AND_GOLD, unknown_setting=0))
        client.receive_next(MessageType.RESULT)
        client.close_connection()

        client.send(Logon(LOGON_PROTOCOL_VERSION, "Alice", ACCESS_CODE, 0, SERIAL_NUMBER))
        first = client.receive_matching(lambda message: message.message_type in log_on_messages)
        second = client.receive_matching(lambda message: message.message_type in log_on_messages)
        self.assertEqual(Result.decode(first).code, ResultCode.LOGON_ACCEPTED)
        self.assertEqual(second.message_type, MessageType.OWN_PLAYER)

    def test_refused_logon_gets_only_the_result_code(self) -> None:
        """The game shows the dialog text for the code; nothing else may follow (no OwnPlayer)."""
        client = self.new_client("Alice", nonce=17)
        go_to_lobby_server(client)
        result = send_logon(client, access_code="WRONGCODE")
        self.assertEqual(result.code, ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE)
        self.assertEqual(client.unread_messages_of_type(MessageType.OWN_PLAYER), [])

    def test_clan_member_is_shown_with_the_clan_to_others(self) -> None:
        """A player who ticked the clan box gets 0x24 instead of 0x0e, and the others see the
        clan name in the member list through 0x25."""
        romans = self.database.create_clan("Romans")
        self.database.set_clan_of_account(self.database.find_account("Bob").account_id, romans.clan_id)
        alice = self.new_client("Alice", nonce=17)
        bob = self.new_client("Bob", nonce=42)
        alice_id = log_on(alice)
        join_group(alice, LOBBY_ROOM_ID, alice_id)

        go_to_lobby_server(bob)
        send_logon(bob, flags=LogonFlag.SHOW_AS_CLAN_MEMBER)
        own_player = OwnPlayerWithClan.decode(bob.receive_next(MessageType.OWN_PLAYER_WITH_CLAN))
        self.assertEqual((own_player.clan_id, own_player.clan_name), (romans.clan_id, "Romans"))
        join_group(bob, LOBBY_ROOM_ID, own_player.player_id)

        entry = AddPlayerToGroupWithClan.decode(alice.receive_next(MessageType.ADD_PLAYER_TO_GROUP_WITH_CLAN))
        self.assertEqual((entry.player_name, entry.clan_name), ("Bob", "Romans"))

    def test_member_list_carries_the_address_the_server_saw(self) -> None:
        """Clients chat straight to each other's addresses and only know themselves as 0.0.0.0."""
        alice = self.new_client("Alice", nonce=17)
        bob = self.new_client("Bob", nonce=42)
        alice_id = log_on(alice)
        join_group(alice, LOBBY_ROOM_ID, alice_id)
        receive_member_entry(alice, LOBBY_ROOM_ID)

        bob_id = log_on(bob)
        join_group(bob, LOBBY_ROOM_ID, bob_id)
        arrival = receive_member_entry(alice, LOBBY_ROOM_ID)
        self.assertEqual((arrival.group_id, arrival.player_id), (LOBBY_ROOM_ID, bob_id))
        self.assertEqual(arrival.player_address, bob.address)
        self.assertEqual(arrival.player_name, "Bob")

    def test_host_is_put_in_its_game_under_a_server_assigned_id(self) -> None:
        """The host asks for an id with 0xffffffff and only counts itself in the game once the
        server sends the game back with parent 0; the zone sees it with the host's real IP."""
        alice = self.new_client("Alice", nonce=17)
        bob = self.new_client("Bob", nonce=42)
        alice_id = log_on(alice)
        join_group(alice, GAME_ZONE_ID, alice_id)
        bob_id = log_on(bob)
        join_group(bob, GAME_ZONE_ID, bob_id)

        open_game(bob, GAME_ZONE_ID)
        game_for_host = GroupAnnouncement.decode(bob.receive_next(MessageType.ADD_GROUP_TO_GROUP_2))
        self.assertEqual(game_for_host.parent_id, JOINED_GROUP_PARENT_ID)
        self.assertEqual(game_for_host.group_id, FIRST_GAME_GROUP_ID)
        host_entry = receive_member_entry(bob, FIRST_GAME_GROUP_ID)
        self.assertEqual((host_entry.group_id, host_entry.player_id), (FIRST_GAME_GROUP_ID, bob_id))

        game_in_zone = GroupAnnouncement.decode(alice.receive_next(MessageType.ADD_GROUP_TO_GROUP_2))
        self.assertEqual((game_in_zone.parent_id, game_in_zone.group_id), (GAME_ZONE_ID, FIRST_GAME_GROUP_ID))
        self.assertEqual(game_in_zone.game_block.host_address, ("127.0.0.1", UNKNOWN_HOST_ADDRESS[1]))

    def test_leaving_is_confirmed_so_the_group_can_be_joined_again(self) -> None:
        """The client keeps a group it left on its joined list until the server confirms; joining it
        again before that makes the client drop the connection."""
        alice = self.new_client("Alice", nonce=17)
        alice_id = log_on(alice)
        join_group(alice, LOBBY_ROOM_ID, alice_id)

        alice.send(RemovePlayerFromGroup(LOBBY_ROOM_ID, alice_id))
        confirmation = RemovePlayerFromGroup.decode(alice.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP))
        self.assertEqual((confirmation.group_id, confirmation.player_id), (LOBBY_ROOM_ID, alice_id))
        join_group(alice, LOBBY_ROOM_ID, alice_id)

    def test_host_back_from_a_match_is_taken_out_of_its_game_silently(self) -> None:
        """Back from a match the client only rejoins its rooms. The server must close the game for
        the zone but must not confirm the departure to the host: after "Zurück" in the game setup
        the client no longer lists the game, and such a confirmation hangs the game."""
        alice = self.new_client("Alice", nonce=17)
        bob = self.new_client("Bob", nonce=42)
        alice_id = log_on(alice)
        join_group(alice, GAME_ZONE_ID, alice_id)
        bob_id = log_on(bob)
        join_group(bob, GAME_ZONE_ID, bob_id)
        open_game(bob, GAME_ZONE_ID)
        bob.receive_next(MessageType.ADD_GROUP_TO_GROUP_2)
        alice.receive_next(MessageType.ADD_GROUP_TO_GROUP_2)

        # The host leaves the zone while it plays, then joins it again afterwards.
        bob.send(RemovePlayerFromGroup(GAME_ZONE_ID, bob_id))
        bob.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP)
        join_group(bob, LOBBY_ROOM_ID, bob_id)

        removal = RemoveGroupFromGroup.decode(alice.receive_next(MessageType.REMOVE_GROUP_FROM_GROUP))
        self.assertEqual((removal.parent_id, removal.group_id), (GAME_ZONE_ID, FIRST_GAME_GROUP_ID))
        # The confirmation would have come in the same block as the room, which join_group read.
        self.assertEqual(bob.unread_messages_of_type(MessageType.REMOVE_PLAYER_FROM_GROUP), [])


if __name__ == "__main__":
    unittest.main()
