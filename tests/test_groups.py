"""Games and chat groups: opening, listing, joining, updating, unlisting and closing them.

Several of these rules exist because the real client drops its connection otherwise: being shown a
group it already sees, being told to remove one it does not see, or getting a departure for a group
it is not in. The docstrings name the address in S3.EXE for each.
"""

from __future__ import annotations

import unittest

from s3lobby.constants import (
    FIRST_GAME_GROUP_ID,
    GAME_BLOCK_SIZE_BY_MESSAGE_TYPE,
    GROUP_COUNTERS_FULL,
    JOINED_GROUP_PARENT_ID,
    MAX_GROUP_TEXT_LENGTH,
    SERVER_ASSIGNS_GROUP_ID,
    UNLIMITED_PLAYERS,
    GroupFlag,
    GroupUpdateMask,
    MessageType,
    ResultCode,
)
from s3lobby.messages import (
    AddPlayerToGroup,
    GameBlock,
    GroupAnnouncement,
    GroupCounters,
    GroupUpdate,
    RemoveGroupFromGroup,
    Result,
)
from tests.support import (
    GAME_ZONE_ID,
    UNKNOWN_HOST_ADDRESS,
    FakeGameClient,
    LobbyTestCase,
    join_group,
    open_game,
    receive_member_entry,
)

OTHER_ZONE_ID = 31
CHAT_THEME_PARENT_ID = 2
GAME_BLOCK_REMAINDER_SIZE = GAME_BLOCK_SIZE_BY_MESSAGE_TYPE[MessageType.UPDATE_GROUP_2] - 16


def receive_announcement(client: FakeGameClient) -> GroupAnnouncement:
    return GroupAnnouncement.decode(client.receive_next(MessageType.ADD_GROUP_TO_GROUP_2))


def open_chat_group(client: FakeGameClient, parent_id: int, name: str, password: str) -> None:
    flags = GroupFlag.HAS_PASSWORD if password else 0
    client.send(
        GroupAnnouncement(
            message_type=MessageType.ADD_GROUP_TO_GROUP_2,
            parent_id=parent_id,
            group_id=SERVER_ASSIGNS_GROUP_ID,
            flags=flags,
            max_players=UNLIMITED_PLAYERS,
            name=name,
            second_text="Come in",
            third_text=password,
        )
    )


def game_update(group_id: int, update_mask: int, name: str = "", max_players: int = 0) -> GroupUpdate:
    return GroupUpdate(
        message_type=MessageType.UPDATE_GROUP_2,
        group_id=group_id,
        update_mask=update_mask,
        flags=GroupFlag.HAS_GAME_BLOCK,
        max_players=max_players,
        name=name,
        second_text="",
        third_text="",
        game_block=GameBlock(UNKNOWN_HOST_ADDRESS, bytes(GAME_BLOCK_REMAINDER_SIZE)),
        description=b"",
    )


class GameTest(LobbyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice, self.alice_id = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        self.bob, self.bob_id = self.player_in_room("Bob", 42, GAME_ZONE_ID)
        receive_member_entry(self.alice, GAME_ZONE_ID)
        open_game(self.bob, GAME_ZONE_ID)
        self.game_for_host = receive_announcement(self.bob)
        self.game_in_zone = receive_announcement(self.alice)

    def test_host_is_in_its_game_and_the_zone_sees_it_with_counters(self) -> None:
        self.assertEqual(self.game_for_host.parent_id, JOINED_GROUP_PARENT_ID)
        self.assertEqual(self.game_in_zone.parent_id, GAME_ZONE_ID)
        counters = GroupCounters.decode(self.alice.receive_next(MessageType.GROUP_COUNTERS))
        self.assertEqual((counters.group_id, counters.player_count, counters.max_players), (FIRST_GAME_GROUP_ID, 1, 2))

    def test_joiner_gets_the_game_and_its_members_and_the_zone_sees_it_full(self) -> None:
        """Joining a full game is refused by the client from the counters (0x4437c1), so they must
        follow every change; a joiner the server lets in anyway would overfill the game."""
        self.alice.send(AddPlayerToGroup(FIRST_GAME_GROUP_ID, self.alice_id, ("0.0.0.0", 0), ""))
        joined = receive_announcement(self.alice)
        self.assertEqual((joined.parent_id, joined.group_id), (JOINED_GROUP_PARENT_ID, FIRST_GAME_GROUP_ID))
        receive_member_entry(self.alice, FIRST_GAME_GROUP_ID, self.bob_id)
        receive_member_entry(self.bob, FIRST_GAME_GROUP_ID, self.alice_id)

        carol, carol_id = self.player_in_room("Carol", 77, GAME_ZONE_ID)
        receive_announcement(carol)
        counters = GroupCounters.decode(carol.receive_next(MessageType.GROUP_COUNTERS))
        self.assertEqual((counters.player_count, counters.status_flags), (2, GROUP_COUNTERS_FULL))
        carol.send(AddPlayerToGroup(FIRST_GAME_GROUP_ID, carol_id, ("0.0.0.0", 0), ""))
        self.assertEqual(Result.decode(carol.receive_next(MessageType.RESULT)).code, ResultCode.GROUP_CHANGE_FAILED)

    def test_listing_in_another_zone_reaches_that_zone_once(self) -> None:
        """"Show game in GameZone" (0x45637f); showing a group twice in one parent is fatal (0x5db9ad)."""
        carol, _ = self.player_in_room("Carol", 77, OTHER_ZONE_ID)
        listing = GroupAnnouncement(MessageType.ADD_GROUP_TO_GROUP, OTHER_ZONE_ID, FIRST_GAME_GROUP_ID, 0, 0, "", "", "")
        self.bob.send(listing)
        shown = receive_announcement(carol)
        self.assertEqual((shown.parent_id, shown.name), (OTHER_ZONE_ID, "Bob's game"))
        self.bob.send(listing)
        carol.assert_nothing_arrives(MessageType.ADD_GROUP_TO_GROUP_2)

    def test_match_start_unlists_the_game_but_keeps_it_for_its_players(self) -> None:
        """The host takes its game out of the zone (0x454c20). Only zone members who see it may be
        told (0x5d5828, 0x5dbcc3); a joiner who left the zone must not, or its connection drops."""
        self.alice.send(AddPlayerToGroup(FIRST_GAME_GROUP_ID, self.alice_id, ("0.0.0.0", 0), ""))
        receive_announcement(self.alice)
        carol, _ = self.player_in_room("Carol", 77, GAME_ZONE_ID)
        receive_announcement(carol)

        self.bob.send(RemoveGroupFromGroup(GAME_ZONE_ID, FIRST_GAME_GROUP_ID))
        removal = RemoveGroupFromGroup.decode(carol.receive_next(MessageType.REMOVE_GROUP_FROM_GROUP))
        self.assertEqual((removal.parent_id, removal.group_id), (GAME_ZONE_ID, FIRST_GAME_GROUP_ID))
        self.assertEqual(self.alice.unread_messages_of_type(MessageType.REMOVE_GROUP_FROM_GROUP), [])
        # The game goes on: a member leaving still reaches the other member.
        self.alice.send(AddPlayerToGroup(GAME_ZONE_ID, self.alice_id, ("0.0.0.0", 0), ""))
        carol.assert_nothing_arrives(MessageType.ADD_GROUP_TO_GROUP_2)

    def test_update_is_kept_and_passed_to_viewers_but_not_members(self) -> None:
        """Members do not take updates for a group they are in (0x5d8de6); the server keeps the new
        state so players who come later see it current."""
        self.bob.send(game_update(FIRST_GAME_GROUP_ID, GroupUpdateMask.NAME | GroupUpdateMask.MAX_PLAYERS, "Renamed", 4))
        update = GroupUpdate.decode(self.alice.receive_next(MessageType.UPDATE_GROUP_2))
        self.assertEqual((update.name, update.game_block.host_address[0]), ("Renamed", "127.0.0.1"))
        self.assertEqual(self.bob.unread_messages_of_type(MessageType.UPDATE_GROUP_2), [])

        carol, _ = self.player_in_room("Carol", 77, GAME_ZONE_ID)
        late_view = receive_announcement(carol)
        self.assertEqual((late_view.name, late_view.max_players), ("Renamed", 4))

    def test_game_counts_in_the_statistics_while_it_is_open(self) -> None:
        self.assertEqual(self.database.collect_statistics().games_open, 1)


class GroupRulesTest(LobbyTestCase):
    def test_chat_group_password_is_checked_and_never_shown(self) -> None:
        alice, _ = self.player_in_room("Alice", 17, CHAT_THEME_PARENT_ID)
        bob, bob_id = self.player_in_room("Bob", 42, CHAT_THEME_PARENT_ID)
        open_chat_group(alice, CHAT_THEME_PARENT_ID, "Romans only", password="hunter")
        shown_to_bob = receive_announcement(bob)
        self.assertEqual(shown_to_bob.third_text, "")
        self.assertTrue(shown_to_bob.flags & GroupFlag.HAS_PASSWORD)

        bob.send(AddPlayerToGroup(shown_to_bob.group_id, bob_id, ("0.0.0.0", 0), "", extra_text="guess"))
        refusal = Result.decode(bob.receive_next(MessageType.RESULT))
        self.assertEqual((refusal.code, refusal.value), (ResultCode.WRONG_GROUP_PASSWORD, shown_to_bob.group_id))
        bob.send(AddPlayerToGroup(shown_to_bob.group_id, bob_id, ("0.0.0.0", 0), "", extra_text="hunter"))
        self.assertEqual(receive_announcement(bob).parent_id, JOINED_GROUP_PARENT_ID)

    def test_unknown_group_cannot_be_joined_or_conjured(self) -> None:
        alice, alice_id = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        alice.send(AddPlayerToGroup(999, alice_id, ("0.0.0.0", 0), ""))
        self.assertEqual(Result.decode(alice.receive_next(MessageType.RESULT)).code, ResultCode.GROUP_CHANGE_FAILED)

    def test_group_cannot_be_opened_in_a_parent_the_player_is_not_in(self) -> None:
        alice, _ = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        bob, _ = self.player_in_room("Bob", 42, OTHER_ZONE_ID)
        open_game(alice, OTHER_ZONE_ID)
        bob.assert_nothing_arrives(MessageType.ADD_GROUP_TO_GROUP_2)

    def test_oversized_names_are_not_passed_on(self) -> None:
        alice, _ = self.player_in_room("Alice", 17, CHAT_THEME_PARENT_ID)
        bob, _ = self.player_in_room("Bob", 42, CHAT_THEME_PARENT_ID)
        open_chat_group(alice, CHAT_THEME_PARENT_ID, "X" * (MAX_GROUP_TEXT_LENGTH + 1), password="")
        bob.assert_nothing_arrives(MessageType.ADD_GROUP_TO_GROUP_2)


if __name__ == "__main__":
    unittest.main()
