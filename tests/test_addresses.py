"""The address each client is shown for another: chat and joining games go straight to it, so it
must be one the receiving client can reach. Passed on as the server sees it, a player on the
server's machine would be 127.0.0.1 for everyone, and a LAN player a private address for players
on the internet: their chat and their joins would go nowhere."""

from __future__ import annotations

import unittest

from s3lobby.addresses import ViewerAddresses
from s3lobby.constants import GroupFlag, MessageType
from s3lobby.messages import AddPlayerToGroup, GameBlock, GroupAnnouncement
from s3lobby.wire import Address

GAME_PORT = 3343
SERVER_LAN_IP = "192.168.1.10"
PUBLIC_IP = "203.0.113.7"
ON_THE_SERVER = ("127.0.0.1", GAME_PORT)
IN_THE_LAN = ("192.168.1.20", GAME_PORT)
OTHER_LAN_PLAYER = ("192.168.1.21", 51000)
# Real public addresses: the documentation ranges (198.51.100.0/24 and the like) do not count as
# global, so they would not stand for players on the internet.
ON_THE_INTERNET = ("84.75.10.4", 40000)
OTHER_INTERNET_PLAYER = ("84.75.10.9", 40001)
HOST_PLACEHOLDER = ("0.0.0.0", GAME_PORT)


def local_ip_facing(viewer: Address) -> str:
    return SERVER_LAN_IP


def no_route(viewer: Address) -> str:
    raise OSError("network is unreachable")


class ViewerAddressesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.public_ip: str | None = PUBLIC_IP
        self.addresses = ViewerAddresses(local_ip_facing, lambda: self.public_ip)

    def test_player_on_the_server_machine_is_shown_at_this_machines_address(self) -> None:
        self.assertEqual(self.addresses.address_for_viewer(ON_THE_SERVER, IN_THE_LAN), (SERVER_LAN_IP, GAME_PORT))
        self.assertEqual(self.addresses.address_for_viewer(ON_THE_SERVER, ON_THE_INTERNET), (PUBLIC_IP, GAME_PORT))
        self.assertEqual(self.addresses.address_for_viewer(ON_THE_SERVER, ON_THE_SERVER), ON_THE_SERVER)

    def test_lan_player_is_shown_to_internet_players_at_the_public_address(self) -> None:
        self.assertEqual(self.addresses.address_for_viewer(IN_THE_LAN, ON_THE_INTERNET), (PUBLIC_IP, GAME_PORT))
        self.assertEqual(self.addresses.address_for_viewer(IN_THE_LAN, OTHER_LAN_PLAYER), IN_THE_LAN)

    def test_without_a_public_host_lan_players_stay_as_they_are(self) -> None:
        self.public_ip = None
        self.assertEqual(self.addresses.address_for_viewer(IN_THE_LAN, ON_THE_INTERNET), IN_THE_LAN)
        self.assertEqual(self.addresses.address_for_viewer(ON_THE_SERVER, ON_THE_INTERNET), (SERVER_LAN_IP, GAME_PORT))

    def test_internet_players_and_placeholders_pass_unchanged(self) -> None:
        for viewer in (ON_THE_SERVER, IN_THE_LAN, OTHER_INTERNET_PLAYER):
            with self.subTest(viewer=viewer):
                self.assertEqual(self.addresses.address_for_viewer(ON_THE_INTERNET, viewer), ON_THE_INTERNET)
        self.assertEqual(self.addresses.address_for_viewer(HOST_PLACEHOLDER, ON_THE_INTERNET), HOST_PLACEHOLDER)

    def test_players_behind_the_same_router_keep_their_address(self) -> None:
        """Both are seen at the router's public IP; nothing better is known about them."""
        same_router = (ON_THE_INTERNET[0], 40002)
        self.assertEqual(self.addresses.address_for_viewer(same_router, ON_THE_INTERNET), same_router)

    def test_no_route_to_the_viewer_keeps_the_address(self) -> None:
        addresses = ViewerAddresses(no_route, lambda: None)
        self.assertEqual(addresses.address_for_viewer(ON_THE_SERVER, IN_THE_LAN), ON_THE_SERVER)

    def test_member_entries_and_game_hosts_are_rewritten_per_viewer(self) -> None:
        member = AddPlayerToGroup(group_id=30, player_id=2, player_address=IN_THE_LAN, player_name="Alice")
        shown_member = self.addresses.message_for_viewer(member, ON_THE_INTERNET)
        self.assertEqual(shown_member.player_address, (PUBLIC_IP, GAME_PORT))
        self.assertEqual(self.addresses.message_for_viewer(member, OTHER_LAN_PLAYER), member)

        game = GroupAnnouncement(
            message_type=MessageType.ADD_GROUP_TO_GROUP_2,
            parent_id=30,
            group_id=0x8000,
            flags=GroupFlag.HAS_GAME_BLOCK,
            max_players=2,
            name="Alice's game",
            second_text="",
            third_text="",
            game_block=GameBlock(host_address=IN_THE_LAN, remainder=bytes(36)),
        )
        shown_game = self.addresses.message_for_viewer(game, ON_THE_INTERNET)
        self.assertEqual(shown_game.game_block.host_address, (PUBLIC_IP, GAME_PORT))
        self.assertEqual(shown_game.game_block.remainder, game.game_block.remainder)


if __name__ == "__main__":
    unittest.main()
