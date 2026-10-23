"""The ChatZone tab: chat themes from the database, and the chat groups players open in them."""

from __future__ import annotations

import unittest

from s3lobby.constants import (
    CHAT_THEME_GROUP_ID_BASE,
    CHAT_ZONE_GROUP_ID,
    DEFAULT_CHAT_THEME_NAME,
    MessageType,
)
from s3lobby.messages import GroupAnnouncement
from s3lobby.wire import RawMessage
from tests.support import FakeGameClient, LobbyTestCase, join_group


def receive_theme(client: FakeGameClient) -> GroupAnnouncement:
    """The next chat theme shown inside the ChatZone tab."""

    def is_theme(message: RawMessage) -> bool:
        """Whether the message lists a group directly inside the ChatZone tab."""
        if message.message_type != MessageType.ADD_GROUP_TO_GROUP:
            return False
        return GroupAnnouncement.decode(message).parent_id == CHAT_ZONE_GROUP_ID

    return GroupAnnouncement.decode(client.receive_matching(is_theme))


class ChatZoneTest(LobbyTestCase):
    """The chat themes listed in the ChatZone tab, and joining them."""

    def test_default_theme_is_offered_when_none_is_configured(self) -> None:
        """Players open chat groups inside a theme; an empty ChatZone would leave them no way to."""
        alice, _ = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        theme = receive_theme(alice)
        self.assertEqual((theme.group_id, theme.name), (CHAT_THEME_GROUP_ID_BASE, DEFAULT_CHAT_THEME_NAME))

    def test_configured_themes_with_their_description_column(self) -> None:
        """A chat theme from the database is listed under its own group id with its description as the second
        text, the column the game shows next to the name."""
        tactics = self.database.add_chat_theme("Tactics", "Talk about strategies")
        alice, _ = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        theme = receive_theme(alice)
        self.assertEqual(theme.group_id, CHAT_THEME_GROUP_ID_BASE + tactics.theme_id)
        self.assertEqual((theme.name, theme.second_text), ("Tactics", "Talk about strategies"))

    def test_theme_added_while_running_reaches_players_already_in_the_chat_zone(self) -> None:
        """Themes an admin adds while the server runs must appear without a restart, also for players already
        on the tab, who would otherwise never see them."""
        self.database.add_chat_theme("Tactics")
        alice, _ = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        receive_theme(alice)
        self.database.add_chat_theme("Trading")
        self.player_in_room("Bob", 42, CHAT_ZONE_GROUP_ID)
        self.assertEqual(receive_theme(alice).name, "Trading")

    def test_theme_can_be_joined_like_a_room(self) -> None:
        """A chat theme is entered like a room; if the join were not answered with the group under parent 0,
        the player could not open chat groups in it."""
        alice, alice_id = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        theme = receive_theme(alice)
        join_group(alice, theme.group_id, alice_id)


if __name__ == "__main__":
    unittest.main()
