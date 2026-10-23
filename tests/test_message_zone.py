"""The MessageZone tab: language groups, board themes, and their messages."""

from __future__ import annotations

import unittest

from s3lobby.constants import (
    BOARD_CHAT_HINT_TEXT,
    BOARD_LANGUAGE_GROUP_ID_BASE,
    BOARD_LANGUAGES,
    BOARD_MESSAGE_TEXT_ID_BASE,
    BOARD_THEME_GROUP_ID_BASE,
    MESSAGE_ZONE_GROUP_ID,
    MessageType,
)
from s3lobby.database import Setting
from s3lobby.messages import ChatCommandAnswer, GetTextMessage, GroupAnnouncement, RemovePlayerFromGroup, TextIdList, TextMessage
from s3lobby.wire import RawMessage
from tests.support import FakeGameClient, LobbyTestCase, join_group

ENGLISH_GROUP_ID = BOARD_LANGUAGE_GROUP_ID_BASE + BOARD_LANGUAGES.index("English")


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
        self.assertEqual(receive_listed_names(alice, ENGLISH_GROUP_ID, 1), ["Tournaments"])

        theme_group_id = BOARD_THEME_GROUP_ID_BASE + theme.theme_id
        join_group(alice, theme_group_id, alice_id)
        listed = TextIdList.decode(alice.receive_next(MessageType.TEXT_ID_LIST))
        text_id = BOARD_MESSAGE_TEXT_ID_BASE + message.message_id
        self.assertEqual((listed.group_id, listed.text_ids), (theme_group_id, (text_id,)))

        alice.send(GetTextMessage(text_id))
        text = TextMessage.decode(alice.receive_next(MessageType.TEXT_MESSAGE))
        self.assertEqual(text.title, "Saturday cup")
        self.assertTrue(text.lines[0].startswith("Alice, "))
        self.assertEqual(text.lines[2:], ("1 vs 1", "from 8 pm"))


class BoardChatHintTest(LobbyTestCase):
    """Chat typed on the message board reaches nobody, a bug of the game (0x452455). The optional
    server line tells the player so once per logon."""

    def test_hint_comes_once_per_logon_when_turned_on(self) -> None:
        """With the setting on, entering the MessageZone tab brings the hint, but only the first time per
        logon, so players moving between tabs are not told again and again."""
        self.database.set_setting(Setting.BOARD_CHAT_HINT, "true")
        alice, alice_id = self.player_in_room("Alice", 17, MESSAGE_ZONE_GROUP_ID)
        line = ChatCommandAnswer.decode(alice.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertEqual(line.lines, (BOARD_CHAT_HINT_TEXT,))
        alice.send(RemovePlayerFromGroup(MESSAGE_ZONE_GROUP_ID, alice_id))
        join_group(alice, MESSAGE_ZONE_GROUP_ID, alice_id)
        alice.assert_nothing_arrives(MessageType.CHAT_COMMAND_ANSWER)

    def test_no_hint_while_the_setting_is_off(self) -> None:
        """No run with the real game has seen a server line on that tab yet, so the hint stays off unless an
        admin turns it on."""
        alice, _ = self.player_in_room("Alice", 17, MESSAGE_ZONE_GROUP_ID)
        alice.assert_nothing_arrives(MessageType.CHAT_COMMAND_ANSWER)


if __name__ == "__main__":
    unittest.main()
