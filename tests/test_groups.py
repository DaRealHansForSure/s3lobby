"""Games and chat groups: opening, listing, joining, updating, unlisting and closing them.

Several of these rules exist because the real client drops its connection otherwise: being shown a
group it already sees, being told to remove one it does not see, or getting a departure for a group
it is not in. The docstrings name the address in S3.EXE for each.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable

from s3lobby.commands import fit_answer
from s3lobby.constants import (
    EMPTY_CHAT_GROUP_KEEP_SECONDS,
    ANNOUNCEMENT_GROUP_ID,
    CHAT_THEME_GROUP_ID_BASE,
    CHAT_ZONE_GROUP_ID,
    FIRST_GAME_GROUP_ID,
    GAME_BLOCK_SIZE_BY_MESSAGE_TYPE,
    GROUP_COUNTERS_FULL,
    GROUP_UPDATE_BURST,
    JOINED_GROUP_PARENT_ID,
    LADDER_NOT_SUPPORTED_TEXT,
    MAX_GROUP_TEXT_LENGTH,
    SERVER_ASSIGNS_GROUP_ID,
    UNLIMITED_PLAYERS,
    WRONG_GROUP_PASSWORD_BURST,
    GroupFlag,
    GroupUpdateMask,
    LadderType,
    MessageType,
    ResultCode,
)
from s3lobby.messages import (
    AddPlayerToGroup,
    ChatCommandAnswer,
    GameBlock,
    GameDescription,
    GameStatus,
    GroupAnnouncement,
    GroupCounters,
    GroupUpdate,
    RemoveGroupFromGroup,
    RemovePlayerFromGroup,
    Result,
)
from s3lobby.wire import RawMessage
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
# The theme the ChatZone offers when none is configured; chat groups are opened inside a theme.
CHAT_THEME_PARENT_ID = CHAT_THEME_GROUP_ID_BASE
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


def game_update(
    group_id: int,
    update_mask: int,
    name: str = "",
    max_players: int = 0,
    description: GameDescription | None = None,
) -> GroupUpdate:
    """An UpdateGroup from a host; with a description, as the game sends it when the setup changes."""
    flags = GroupFlag.HAS_GAME_BLOCK
    description_bytes = b""
    if description is not None:
        flags |= GroupFlag.HAS_DESCRIPTION
        description_bytes = description.encode()
    return GroupUpdate(
        message_type=MessageType.UPDATE_GROUP_2,
        group_id=group_id,
        update_mask=update_mask,
        flags=flags,
        max_players=max_players,
        name=name,
        second_text="",
        third_text="",
        game_block=GameBlock(UNKNOWN_HOST_ADDRESS, bytes(GAME_BLOCK_REMAINDER_SIZE)),
        description=description_bytes,
    )


def game_description(is_ladder_game: bool) -> GameDescription:
    """A two-team game on a random map, as seen in a run."""
    return GameDescription(team_count=2, computer_opponent_count=0, is_ladder_game=is_ladder_game, map_name="\\Random")


class GameTest(LobbyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice, self.alice_id = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        self.bob, self.bob_id = self.player_in_room("Bob", 42, GAME_ZONE_ID)
        receive_member_entry(self.alice, GAME_ZONE_ID)
        open_game(self.bob, GAME_ZONE_ID)
        self.game_for_host = receive_announcement(self.bob)
        self.game_in_zone = receive_announcement(self.alice)

    def test_game_is_recorded_with_setup_players_and_reports(self) -> None:
        """The Games page shows what the lobby kept of a game: its setup without the host's address,
        everyone who joined, host first, and the reports of its match linked to it."""
        self.alice.send(AddPlayerToGroup(self.game_for_host.group_id, self.alice_id, ("0.0.0.0", 0), ""))
        receive_announcement(self.alice)
        self.bob.send(GameStatus(self.game_for_host.group_id, 0, 0, 68, "\\Random"))
        self.bob.receive_next(MessageType.RESULT)
        self.bob.receive_next(MessageType.RESULT)

        [game] = self.database.list_recent_games(limit=1)
        self.assertEqual(game.group_id, self.game_for_host.group_id)
        self.assertEqual(game.player_names, ("Bob", "Alice"))
        stored = GroupAnnouncement.decode(RawMessage(MessageType.ADD_GROUP_TO_GROUP_2, game.announcement))
        self.assertEqual(stored.name, "Bob's game")
        self.assertEqual(stored.game_block.host_address, ("0.0.0.0", 0))
        [report] = self.database.list_recent_game_reports(limit=1)
        self.assertEqual(report.game_id, game.game_id)

    def test_host_is_told_its_game_for_the_setup(self) -> None:
        """The game setup keeps the host's new game in an empty slot until Result 0xc names it
        (0x452b53); setup updates (0x45506c), "show game in GameZone" (0x456370) and the match
        start (0x454c20) all name that slot, so without it they name no game."""
        opened = Result.decode(self.bob.receive_next(MessageType.RESULT))
        self.assertEqual((opened.code, opened.value), (ResultCode.GROUP_OPENED, self.game_for_host.group_id))
        self.alice.assert_nothing_arrives(MessageType.RESULT)

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


class LadderGameTest(LobbyTestCase):
    """Blue Byte's server ranked ladder games ("Liga", "Clanliga"); this one keeps no ladder. The
    game itself says nothing about that before the match, so without the server line its players
    would play a ladder game believing it counts."""

    def setUp(self) -> None:
        super().setUp()
        self.alice, self.alice_id = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        self.bob, self.bob_id = self.player_in_room("Bob", 42, GAME_ZONE_ID)
        self.notice = fit_answer([LADDER_NOT_SUPPORTED_TEXT])

    def receive_notice(self, client: FakeGameClient) -> ChatCommandAnswer:
        return ChatCommandAnswer.decode(client.receive_next(MessageType.CHAT_COMMAND_ANSWER))

    def test_host_of_a_ladder_game_is_told_once(self) -> None:
        open_game(self.alice, GAME_ZONE_ID, ladder_type=LadderType.INDIVIDUAL)
        notice = self.receive_notice(self.alice)
        self.assertEqual((notice.group_id, notice.lines), (ANNOUNCEMENT_GROUP_ID, self.notice))
        self.alice.send(game_update(FIRST_GAME_GROUP_ID, GroupUpdateMask.MAX_PLAYERS, max_players=4))
        self.alice.assert_nothing_arrives(MessageType.CHAT_COMMAND_ANSWER)

    def test_joiner_of_a_ladder_game_is_told(self) -> None:
        open_game(self.alice, GAME_ZONE_ID, ladder_type=LadderType.CLANS)
        self.bob.send(AddPlayerToGroup(FIRST_GAME_GROUP_ID, self.bob_id, ("0.0.0.0", 0), ""))
        self.assertEqual(self.receive_notice(self.bob).lines, self.notice)

    def test_normal_game_brings_no_notice(self) -> None:
        open_game(self.alice, GAME_ZONE_ID, description=game_description(is_ladder_game=False))
        self.bob.send(AddPlayerToGroup(FIRST_GAME_GROUP_ID, self.bob_id, ("0.0.0.0", 0), ""))
        receive_announcement(self.bob)
        self.alice.assert_nothing_arrives(MessageType.CHAT_COMMAND_ANSWER)
        self.bob.assert_nothing_arrives(MessageType.CHAT_COMMAND_ANSWER)

    def test_turning_a_game_into_a_ladder_game_tells_its_players(self) -> None:
        """The host can switch to a ladder game during the setup; the description it then sends
        is current, while the game block still holds the choice made when opening."""
        open_game(self.alice, GAME_ZONE_ID, description=game_description(is_ladder_game=False))
        self.bob.send(AddPlayerToGroup(FIRST_GAME_GROUP_ID, self.bob_id, ("0.0.0.0", 0), ""))
        receive_announcement(self.bob)
        self.alice.send(
            game_update(
                FIRST_GAME_GROUP_ID,
                GroupUpdateMask.MAX_PLAYERS | GroupUpdateMask.DESCRIPTION,
                max_players=2,
                description=game_description(is_ladder_game=True),
            )
        )
        self.assertEqual(self.receive_notice(self.alice).lines, self.notice)
        self.assertEqual(self.receive_notice(self.bob).lines, self.notice)


class GroupRulesTest(LobbyTestCase):
    def player_in_chat_theme(self, player_name: str, nonce: int) -> tuple[FakeGameClient, int]:
        """A player on the ChatZone tab who chose the default chat theme."""
        client, player_id = self.player_in_room(player_name, nonce, CHAT_ZONE_GROUP_ID)
        join_group(client, CHAT_THEME_PARENT_ID, player_id)
        return client, player_id

    def test_chat_group_password_is_checked_and_never_shown(self) -> None:
        alice, _ = self.player_in_chat_theme("Alice", 17)
        bob, bob_id = self.player_in_chat_theme("Bob", 42)
        open_chat_group(alice, CHAT_THEME_PARENT_ID, "Romans only", password="hunter")
        shown_to_bob = receive_announcement(bob)
        self.assertEqual(shown_to_bob.third_text, "")
        self.assertTrue(shown_to_bob.flags & GroupFlag.HAS_PASSWORD)

        bob.send(AddPlayerToGroup(shown_to_bob.group_id, bob_id, ("0.0.0.0", 0), "", extra_text="guess"))
        refusal = Result.decode(bob.receive_next(MessageType.RESULT))
        self.assertEqual((refusal.code, refusal.value), (ResultCode.WRONG_GROUP_PASSWORD, shown_to_bob.group_id))
        bob.send(AddPlayerToGroup(shown_to_bob.group_id, bob_id, ("0.0.0.0", 0), "", extra_text="hunter"))
        self.assertEqual(receive_announcement(bob).parent_id, JOINED_GROUP_PARENT_ID)

    def test_chat_group_creator_is_told_its_channel(self) -> None:
        """After opening a chat group the game's lobby screen has no channel until Result 0xc
        names the group (0x452577). Without it the creator sat in the group without name and
        members, and chat, whisper and new games did nothing until leaving and joining it again
        (seen in a run)."""
        alice, _ = self.player_in_chat_theme("Alice", 17)
        open_chat_group(alice, CHAT_THEME_PARENT_ID, "test", password="")
        joined = receive_announcement(alice)
        self.assertEqual(joined.parent_id, JOINED_GROUP_PARENT_ID)
        channel = Result.decode(alice.receive_next(MessageType.RESULT))
        self.assertEqual((channel.code, channel.value), (ResultCode.GROUP_OPENED, joined.group_id))

    def test_host_finds_its_chat_group_again_after_the_game_setup(self) -> None:
        """A player who opens a game inside a chat group leaves the group in the same block and,
        back from the game setup, joins it again (seen in a run). Closed as empty meanwhile, the
        join got Result 0xb, whose dialog Wine did not show over the full screen game: the screen
        froze (seen in a run)."""
        alice, alice_id = self.player_in_chat_theme("Alice", 17)
        open_chat_group(alice, CHAT_THEME_PARENT_ID, "test", password="")
        chat_group_id = receive_announcement(alice).group_id
        open_game(alice, chat_group_id)
        alice.send(RemovePlayerFromGroup(chat_group_id, alice_id))
        game_id = self.receive_joined_group(alice, lambda group_id: group_id != chat_group_id)
        # A long game setup or match: the group waits while the game is open.
        self.server.close_abandoned_chat_groups(EMPTY_CHAT_GROUP_KEEP_SECONDS * 10)

        alice.send(
            RemovePlayerFromGroup(game_id, alice_id),
            AddPlayerToGroup(chat_group_id, alice_id, ("0.0.0.0", 0), ""),
        )
        self.assertEqual(self.receive_joined_group(alice, lambda group_id: True), chat_group_id)

    def test_game_opened_in_a_chat_group_can_be_warped_into_a_zone(self) -> None:
        """"Warp" in the game setup lists a game opened in a chat group in a GameZone (0x4552c0,
        0x45637f; seen in a run). Players in that zone see it there and can join it."""
        alice, alice_id = self.player_in_chat_theme("Alice", 17)
        bob, bob_id = self.player_in_room("Bob", 42, GAME_ZONE_ID)
        open_chat_group(alice, CHAT_THEME_PARENT_ID, "test", password="")
        chat_group_id = receive_announcement(alice).group_id
        open_game(alice, chat_group_id)
        alice.send(RemovePlayerFromGroup(chat_group_id, alice_id))
        game_id = self.receive_joined_group(alice, lambda group_id: group_id != chat_group_id)

        alice.send(GroupAnnouncement(MessageType.ADD_GROUP_TO_GROUP, GAME_ZONE_ID, game_id, 0, 0, "", "", ""))
        shown = receive_announcement(bob)
        self.assertEqual((shown.parent_id, shown.group_id, shown.name), (GAME_ZONE_ID, game_id, "Alice's game"))
        bob.send(AddPlayerToGroup(game_id, bob_id, ("0.0.0.0", 0), ""))
        self.assertEqual(self.receive_joined_group(bob, lambda group_id: group_id == game_id), game_id)

    def test_empty_chat_group_closes_after_a_while(self) -> None:
        alice, alice_id = self.player_in_chat_theme("Alice", 17)
        bob, _ = self.player_in_chat_theme("Bob", 42)
        open_chat_group(alice, CHAT_THEME_PARENT_ID, "test", password="")
        chat_group_id = receive_announcement(bob).group_id
        alice.send(RemovePlayerFromGroup(chat_group_id, alice_id))
        alice.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP)

        self.server.close_abandoned_chat_groups(EMPTY_CHAT_GROUP_KEEP_SECONDS / 2)
        bob.assert_nothing_arrives(MessageType.REMOVE_GROUP_FROM_GROUP)
        self.server.close_abandoned_chat_groups(EMPTY_CHAT_GROUP_KEEP_SECONDS)
        removal = RemoveGroupFromGroup.decode(bob.receive_next(MessageType.REMOVE_GROUP_FROM_GROUP))
        self.assertEqual((removal.parent_id, removal.group_id), (CHAT_THEME_PARENT_ID, chat_group_id))

    def receive_joined_group(self, client: FakeGameClient, is_wanted: Callable[[int], bool]) -> int:
        """The id of the next group announced to the client as joined (parent 0) that is wanted."""
        while True:
            announcement = receive_announcement(client)
            if announcement.parent_id == JOINED_GROUP_PARENT_ID and is_wanted(announcement.group_id):
                return announcement.group_id

    def test_wrong_group_passwords_are_limited_per_player(self) -> None:
        """Each join carries one guess and a datagram holds hundreds: without a limit a password
        falls to trying."""
        alice, _ = self.player_in_chat_theme("Alice", 17)
        bob, bob_id = self.player_in_chat_theme("Bob", 42)
        open_chat_group(alice, CHAT_THEME_PARENT_ID, "Romans only", password="hunter")
        group_id = receive_announcement(bob).group_id
        guesses = [
            AddPlayerToGroup(group_id, bob_id, ("0.0.0.0", 0), "", extra_text=f"guess{number}")
            for number in range(WRONG_GROUP_PASSWORD_BURST)
        ]
        bob.send(*guesses, AddPlayerToGroup(group_id, bob_id, ("0.0.0.0", 0), "", extra_text="hunter"))
        for _ in range(WRONG_GROUP_PASSWORD_BURST + 1):
            refusal = Result.decode(bob.receive_next(MessageType.RESULT))
            self.assertEqual(refusal.code, ResultCode.WRONG_GROUP_PASSWORD)

    def test_group_updates_are_limited_per_player(self) -> None:
        """Each update goes to every viewer; a flood of them would be multiplied onto them all."""
        alice, _ = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        bob, _ = self.player_in_room("Bob", 42, GAME_ZONE_ID)
        open_game(bob, GAME_ZONE_ID)
        receive_announcement(alice)
        update = game_update(FIRST_GAME_GROUP_ID, GroupUpdateMask.MAX_PLAYERS, max_players=2)
        updates = [update] * (GROUP_UPDATE_BURST + 10)
        bob.send(*updates)
        for _ in range(GROUP_UPDATE_BURST):
            alice.receive_next(MessageType.UPDATE_GROUP_2)
        alice.assert_nothing_arrives(MessageType.UPDATE_GROUP_2)

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
        alice, _ = self.player_in_chat_theme("Alice", 17)
        bob, _ = self.player_in_chat_theme("Bob", 42)
        open_chat_group(alice, CHAT_THEME_PARENT_ID, "X" * (MAX_GROUP_TEXT_LENGTH + 1), password="")
        bob.assert_nothing_arrives(MessageType.ADD_GROUP_TO_GROUP_2)


if __name__ == "__main__":
    unittest.main()
