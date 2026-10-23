"""The ChatZone tab: chat themes from the database, and the chat groups players open in them."""

from __future__ import annotations

import unittest

from s3lobby.constants import (
    CHAT_THEME_GROUP_ID_BASE,
    CHAT_ZONE_GROUP_ID,
    DEFAULT_CHAT_THEME_NAME,
    MessageType,
)
from s3lobby.database import DatabaseError
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


def receive_theme_named(client: FakeGameClient, name: str) -> GroupAnnouncement:
    """The chat theme with this name, shown inside the ChatZone tab; other themes stay unread."""

    def is_named_theme(message: RawMessage) -> bool:
        """Whether the message lists the named group directly inside the ChatZone tab."""
        if message.message_type != MessageType.ADD_GROUP_TO_GROUP:
            return False
        announcement = GroupAnnouncement.decode(message)
        return announcement.parent_id == CHAT_ZONE_GROUP_ID and announcement.name == name

    return GroupAnnouncement.decode(client.receive_matching(is_named_theme))


class ChatZoneTest(LobbyTestCase):
    """The chat themes listed in the ChatZone tab, and joining them."""

    def test_general_stays_listed_beside_the_admins_themes(self) -> None:
        """The game joins the theme it was in last as soon as the ChatZone opens; when General
        disappeared after an admin added a theme, that join failed with "Der Wechsel in die Gruppe
        hat nicht geklappt" (seen in a run). General is always listed under its id."""
        self.database.add_chat_theme("Tactics")
        alice, alice_id = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        general = receive_theme_named(alice, DEFAULT_CHAT_THEME_NAME)
        self.assertEqual(general.group_id, CHAT_THEME_GROUP_ID_BASE)
        join_group(alice, CHAT_THEME_GROUP_ID_BASE, alice_id)

    def test_a_built_in_theme_name_cannot_be_added_again(self) -> None:
        """A second "General" would stand twice in the game's list."""
        with self.assertRaises(DatabaseError):
            self.database.add_chat_theme("general")

    def test_configured_themes_with_their_description_column(self) -> None:
        """A chat theme from the database is listed under its own group id with its description as the second
        text, the column the game shows next to the name."""
        tactics = self.database.add_chat_theme("Tactics", "Talk about strategies")
        alice, _ = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        theme = receive_theme_named(alice, "Tactics")
        self.assertEqual(theme.group_id, CHAT_THEME_GROUP_ID_BASE + tactics.theme_id)
        self.assertEqual((theme.name, theme.second_text), ("Tactics", "Talk about strategies"))

    def test_theme_added_while_running_reaches_players_already_in_the_chat_zone(self) -> None:
        """Themes an admin adds while the server runs must appear without a restart, also for players already
        on the tab, who would otherwise never see them."""
        self.database.add_chat_theme("Tactics")
        alice, _ = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        receive_theme_named(alice, "Tactics")
        self.database.add_chat_theme("Trading")
        self.player_in_room("Bob", 42, CHAT_ZONE_GROUP_ID)
        self.assertEqual(receive_theme_named(alice, "Trading").name, "Trading")

    def test_theme_can_be_joined_like_a_room(self) -> None:
        """A chat theme is entered like a room; if the join were not answered with the group under parent 0,
        the player could not open chat groups in it."""
        alice, alice_id = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        theme = receive_theme(alice)
        join_group(alice, theme.group_id, alice_id)


if __name__ == "__main__":
    unittest.main()
