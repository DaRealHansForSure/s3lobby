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
    """Stands in for the route lookup: this machine faces every viewer at its LAN address."""
    return SERVER_LAN_IP


def no_route(viewer: Address) -> str:
    """Stands in for a route lookup that fails, as when the viewer's network cannot be reached."""
    raise OSError("network is unreachable")


class ViewerAddressesTest(unittest.TestCase):
    """Which address one player is shown at for each kind of viewer: on the server's machine, in its LAN, or
    on the internet."""

    def setUp(self) -> None:
        """Viewer addresses with a public host the test can take away again."""
        self.public_ip: str | None = PUBLIC_IP
        self.addresses = ViewerAddresses(local_ip_facing, lambda: self.public_ip)

    def test_player_on_the_server_machine_is_shown_at_this_machines_address(self) -> None:
        """A player on the server's machine comes from 127.0.0.1, which points at the viewer's own machine for
        everyone else: LAN viewers must get the LAN address, internet viewers the public one, or their chat
        and joins go nowhere. A viewer on the same machine keeps 127.0.0.1."""
        self.assertEqual(self.addresses.address_for_viewer(ON_THE_SERVER, IN_THE_LAN), (SERVER_LAN_IP, GAME_PORT))
        self.assertEqual(self.addresses.address_for_viewer(ON_THE_SERVER, ON_THE_INTERNET), (PUBLIC_IP, GAME_PORT))
        self.assertEqual(self.addresses.address_for_viewer(ON_THE_SERVER, ON_THE_SERVER), ON_THE_SERVER)

    def test_lan_player_is_shown_to_internet_players_at_the_public_address(self) -> None:
        """A private LAN address cannot be reached from the internet, so internet viewers get the public host
        instead; viewers in the same LAN keep the real address."""
        self.assertEqual(self.addresses.address_for_viewer(IN_THE_LAN, ON_THE_INTERNET), (PUBLIC_IP, GAME_PORT))
        self.assertEqual(self.addresses.address_for_viewer(IN_THE_LAN, OTHER_LAN_PLAYER), IN_THE_LAN)

    def test_without_a_public_host_lan_players_stay_as_they_are(self) -> None:
        """With no public host configured there is nothing better to show an internet viewer, so a LAN player
        stays as seen; a player on the server's machine still gets the LAN address."""
        self.public_ip = None
        self.assertEqual(self.addresses.address_for_viewer(IN_THE_LAN, ON_THE_INTERNET), IN_THE_LAN)
        self.assertEqual(self.addresses.address_for_viewer(ON_THE_SERVER, ON_THE_INTERNET), (SERVER_LAN_IP, GAME_PORT))

    def test_internet_players_and_placeholders_pass_unchanged(self) -> None:
        """An internet address is reachable for everyone, and 0.0.0.0 is a host's own placeholder rather than
        an address, so neither is rewritten."""
        for viewer in (ON_THE_SERVER, IN_THE_LAN, OTHER_INTERNET_PLAYER):
            with self.subTest(viewer=viewer):
                self.assertEqual(self.addresses.address_for_viewer(ON_THE_INTERNET, viewer), ON_THE_INTERNET)
        self.assertEqual(self.addresses.address_for_viewer(HOST_PLACEHOLDER, ON_THE_INTERNET), HOST_PLACEHOLDER)

    def test_players_behind_the_same_router_keep_their_address(self) -> None:
        """Both are seen at the router's public IP; nothing better is known about them."""
        same_router = (ON_THE_INTERNET[0], 40002)
        self.assertEqual(self.addresses.address_for_viewer(same_router, ON_THE_INTERNET), same_router)

    def test_no_route_to_the_viewer_keeps_the_address(self) -> None:
        """A failing route lookup must not stop the message; the address goes out as the server sees it."""
        addresses = ViewerAddresses(no_route, lambda: None)
        self.assertEqual(addresses.address_for_viewer(ON_THE_SERVER, IN_THE_LAN), ON_THE_SERVER)

    def test_member_entries_and_game_hosts_are_rewritten_per_viewer(self) -> None:
        """The addresses inside messages are rewritten too: a member entry's address, which chat goes to, and
        a game's host address, which joiners connect DirectPlay to. The rest of the game block passes byte
        for byte."""
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
