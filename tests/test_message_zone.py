"""The MessageZone tab: language groups, board themes, and their messages."""

from __future__ import annotations

import unittest

from s3lobby.constants import (
    BOARD_LANGUAGE_GROUP_ID_BASE,
    BOARD_LANGUAGES,
    BOARD_MESSAGE_TEXT_ID_BASE,
    BOARD_THEME_ID_SPAN,
    FIRST_GAME_GROUP_ID,
    MAX_BOARD_THEMES_PER_LANGUAGE,
    MESSAGE_ZONE_GROUP_ID,
    MessageType,
)
from s3lobby.lobby import board_theme_group_id
from s3lobby.messages import GetTextMessage, GroupAnnouncement, RemovePlayerFromGroup, TextIdList, TextMessage
from s3lobby.wire import RawMessage
from tests.support import FakeGameClient, LobbyTestCase, join_group

ENGLISH_GROUP_ID = BOARD_LANGUAGE_GROUP_ID_BASE + BOARD_LANGUAGES.index("English")


def receive_listed(client: FakeGameClient, parent_id: int, count: int) -> list[GroupAnnouncement]:
    """The next groups shown inside one parent."""

    def is_listed_in_parent(message: RawMessage) -> bool:
        """Whether the message lists a group inside parent_id."""
        if message.message_type != MessageType.ADD_GROUP_TO_GROUP:
            return False
        return GroupAnnouncement.decode(message).parent_id == parent_id

    return [GroupAnnouncement.decode(client.receive_matching(is_listed_in_parent)) for _ in range(count)]


def receive_listed_names(client: FakeGameClient, parent_id: int, count: int) -> list[str]:
    """The names of the next groups shown inside one parent."""

    def is_listed_in_parent(message: RawMessage) -> bool:
        """Whether the message lists a group inside parent_id."""
        if message.message_type != MessageType.ADD_GROUP_TO_GROUP:
            return False
        return GroupAnnouncement.decode(message).parent_id == parent_id

    return [GroupAnnouncement.decode(client.receive_matching(is_listed_in_parent)).name for _ in range(count)]


class MessageZoneTest(LobbyTestCase):
    """The message board: language groups, the themes inside them, and the messages of a theme."""

    def test_language_groups_carry_the_names_the_client_looks_for(self) -> None:
        """The client picks "MessageThemes" + its language and falls back to English (0x453831)."""
        self.database.add_board_theme("German", "Turniere")
        alice, _ = self.player_in_room("Alice", 17, MESSAGE_ZONE_GROUP_ID)
        names = receive_listed_names(alice, MESSAGE_ZONE_GROUP_ID, 2)
        self.assertEqual(sorted(names), ["MessageThemesEnglish", "MessageThemesGerman"])

    def test_theme_lists_its_messages_and_each_message_has_title_and_byline(self) -> None:
        """A theme is listed in its language group, joining it brings the ids of its messages, and each
        message comes with its title, a first line naming author and date, and an empty line before the
        text itself."""
        theme = self.database.add_board_theme("English", "Tournaments", "Upcoming events")
        message = self.database.post_board_message(theme.theme_id, "Saturday cup", "1 vs 1\nfrom 8 pm", author="Alice")
        alice, alice_id = self.player_in_room("Alice", 17, MESSAGE_ZONE_GROUP_ID)
        join_group(alice, ENGLISH_GROUP_ID, alice_id)
        [listed_theme] = receive_listed(alice, ENGLISH_GROUP_ID, 1)
        self.assertEqual(listed_theme.name, "Tournaments")

        theme_group_id = listed_theme.group_id
        join_group(alice, theme_group_id, alice_id)
        listed = TextIdList.decode(alice.receive_next(MessageType.TEXT_ID_LIST))
        text_id = BOARD_MESSAGE_TEXT_ID_BASE + message.message_id
        self.assertEqual((listed.group_id, listed.text_ids), (theme_group_id, (text_id,)))

        alice.send(GetTextMessage(text_id))
        text = TextMessage.decode(alice.receive_next(MessageType.TEXT_MESSAGE))
        self.assertEqual(text.title, "Saturday cup")
        self.assertTrue(text.lines[0].startswith("Alice, "))
        self.assertEqual(text.lines[2:], ("1 vs 1", "from 8 pm"))

    def test_themes_come_in_the_board_s_order_and_hidden_ones_stay_away(self) -> None:
        """Admins put the ladder's theme or an important one on top. The game lists a parent's
        groups by ascending group id (0x5db570), not in the order they arrive, so the group ids
        must follow the board's order; a hidden theme is not sent at all."""
        cups = self.database.add_board_theme("English", "Cups")
        rules = self.database.add_board_theme("English", "Rules")
        hidden = self.database.add_board_theme("English", "Old news")
        self.database.set_board_theme_order([rules.theme_id, cups.theme_id, hidden.theme_id])
        self.database.set_board_theme_hidden(hidden.theme_id, True)
        alice, alice_id = self.player_in_room("Alice", 17, MESSAGE_ZONE_GROUP_ID)
        join_group(alice, ENGLISH_GROUP_ID, alice_id)
        listed = sorted(receive_listed(alice, ENGLISH_GROUP_ID, 2), key=lambda theme: theme.group_id)
        self.assertEqual([theme.name for theme in listed], ["Rules", "Cups"])
        alice.assert_nothing_matching(lambda message: b"Old news" in message.body)

    def test_a_new_order_reaches_players_who_open_the_board_again(self) -> None:
        """Moving a theme up while the server runs gives it a lower group id, so the game shows it
        higher the next time a player opens the language's themes."""
        cups = self.database.add_board_theme("English", "Cups")
        rules = self.database.add_board_theme("English", "Rules")
        alice, alice_id = self.player_in_room("Alice", 17, MESSAGE_ZONE_GROUP_ID)
        join_group(alice, ENGLISH_GROUP_ID, alice_id)
        before = sorted(receive_listed(alice, ENGLISH_GROUP_ID, 2), key=lambda theme: theme.group_id)
        self.assertEqual([theme.name for theme in before], ["Cups", "Rules"])
        alice.send(RemovePlayerFromGroup(ENGLISH_GROUP_ID, alice_id))
        self.database.set_board_theme_order([rules.theme_id, cups.theme_id])
        join_group(alice, ENGLISH_GROUP_ID, alice_id)
        after = sorted(receive_listed(alice, ENGLISH_GROUP_ID, 2), key=lambda theme: theme.group_id)
        self.assertEqual([theme.name for theme in after], ["Rules", "Cups"])

    def test_every_theme_place_fits_below_the_game_ids(self) -> None:
        """The last place of the last language must not reach the ids games and chat groups use."""
        last = board_theme_group_id(len(BOARD_LANGUAGES) - 1, MAX_BOARD_THEMES_PER_LANGUAGE - 1, BOARD_THEME_ID_SPAN - 1)
        self.assertLess(last, FIRST_GAME_GROUP_ID)

    def test_a_hidden_message_is_not_listed_or_sent(self) -> None:
        """A hidden message stays stored for admins, but the game neither lists it nor gets its text."""
        theme = self.database.add_board_theme("English", "Tournaments")
        shown = self.database.post_board_message(theme.theme_id, "Shown", "text")
        hidden = self.database.post_board_message(theme.theme_id, "Hidden", "text")
        self.database.set_board_message_hidden(hidden.message_id, True)
        alice, alice_id = self.player_in_room("Alice", 17, MESSAGE_ZONE_GROUP_ID)
        join_group(alice, ENGLISH_GROUP_ID, alice_id)
        [listed_theme] = receive_listed(alice, ENGLISH_GROUP_ID, 1)
        join_group(alice, listed_theme.group_id, alice_id)
        listed = TextIdList.decode(alice.receive_next(MessageType.TEXT_ID_LIST))
        self.assertEqual(listed.text_ids, (BOARD_MESSAGE_TEXT_ID_BASE + shown.message_id,))
        alice.send(GetTextMessage(BOARD_MESSAGE_TEXT_ID_BASE + hidden.message_id))
        text = TextMessage.decode(alice.receive_next(MessageType.TEXT_MESSAGE))
        self.assertNotEqual(text.title, "Hidden")


if __name__ == "__main__":
    unittest.main()
