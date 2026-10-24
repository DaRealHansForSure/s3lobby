"""The ChatZone tab: chat themes from the database, and the chat groups players open in them."""

from __future__ import annotations

import unittest
from collections.abc import Callable

from s3lobby.constants import (
    CHAT_THEME_GROUP_ID_BASE,
    CHAT_ZONE_GROUP_ID,
    DEFAULT_CHAT_THEME_NAME,
    GAME_LANGUAGES,
    JOINED_GROUP_PARENT_ID,
    PERMANENT_CHAT_GROUP_FLAGS,
    MessageType,
)
from s3lobby.database import DatabaseError, Setting
from s3lobby.messages import AddPlayerToGroup, CheckFileVersion, GroupAnnouncement, OwnPlayer, RemovePlayerFromGroup
from s3lobby.wire import RawMessage
from tests.support import (
    EDITION_MISSION_AND_GOLD,
    GAME_VERSION,
    FakeGameClient,
    LobbyTestCase,
    join_group,
    send_logon,
)

ENGLISH_LANGUAGE_INDEX = GAME_LANGUAGES.index("English")
# The default themes migration 0014 adds, in their order.
DEFAULT_THEME_NAMES = ["Meeting Room", "Beginners", "Deutsch", "English", "Français", "Italiano", "Polski", "Español"]


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
    return GroupAnnouncement.decode(client.receive_matching(is_theme_named(name)))


def is_theme_named(name: str) -> Callable[[RawMessage], bool]:
    """A test for messages that list the named theme inside the ChatZone tab."""

    def is_named_theme(message: RawMessage) -> bool:
        """Whether the message lists the named group directly inside the ChatZone tab."""
        if message.message_type != MessageType.ADD_GROUP_TO_GROUP:
            return False
        announcement = GroupAnnouncement.decode(message)
        return announcement.parent_id == CHAT_ZONE_GROUP_ID and announcement.name == name

    return is_named_theme


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

    def test_a_lobby_starts_with_the_rooms_the_manual_describes(self) -> None:
        """A meeting room to find players, a topic room and one room per language, as the game's
        manual describes the ChatZone; they are ordinary themes an admin may remove."""
        self.assertEqual([theme.name for theme in self.database.list_chat_themes()], DEFAULT_THEME_NAMES)
        alice, _ = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        receive_theme_named(alice, "Meeting Room")

    def test_a_language_theme_is_shown_only_to_games_of_that_language(self) -> None:
        """The game reports its language in the patch check before the logon; a German game sees
        "Deutsch", an English one does not, and themes for every game reach both."""
        german, _ = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        receive_theme_named(german, "Deutsch")
        english = self.new_client("Bob", nonce=42)
        english.send(CheckFileVersion(GAME_VERSION, EDITION_MISSION_AND_GOLD, ENGLISH_LANGUAGE_INDEX))
        english.receive_next(MessageType.RESULT)
        english.close_connection()
        send_logon(english)
        english_id = OwnPlayer.decode(english.receive_next(MessageType.OWN_PLAYER)).player_id
        join_group(english, CHAT_ZONE_GROUP_ID, english_id)
        receive_theme_named(english, "English")
        receive_theme_named(english, "Meeting Room")
        english.assert_nothing_matching(is_theme_named("Deutsch"))

    def test_a_built_in_theme_name_cannot_be_added_again(self) -> None:
        """A second "General" would stand twice in the game's list."""
        with self.assertRaises(DatabaseError):
            self.database.add_chat_theme("general")

    def test_a_theme_for_an_unknown_language_is_refused(self) -> None:
        """Only the game's own language names can match what a game reports."""
        with self.assertRaises(DatabaseError):
            self.database.add_chat_theme("Klingon", language="Klingon")

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



class PermanentChatGroupTest(LobbyTestCase):
    """Chat groups the server keeps open in the themes (setting permanent_chat_groups)."""

    def setUp(self) -> None:
        """The test server with the setting on."""
        super().setUp()
        self.database.set_setting(Setting.PERMANENT_CHAT_GROUPS, "true")

    def test_a_theme_offers_its_permanent_group_to_join_and_chat_in(self) -> None:
        """Without it a player in an empty theme has nobody to talk to until someone opens a chat
        group; with it General lists "[Server] General", which a player joins like any chat group."""
        alice, alice_id = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        join_group(alice, CHAT_THEME_GROUP_ID_BASE, alice_id)
        group = receive_permanent_group(alice, CHAT_THEME_GROUP_ID_BASE)
        self.assertEqual((group.name, group.flags), ("[Server] General", PERMANENT_CHAT_GROUP_FLAGS))
        join_chat_group(alice, group.group_id, alice_id)

    def test_a_permanent_group_stays_open_when_empty(self) -> None:
        """The housekeeping that closes abandoned chat groups leaves it alone, so the next player
        still finds it."""
        alice, alice_id = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        join_group(alice, CHAT_THEME_GROUP_ID_BASE, alice_id)
        group = receive_permanent_group(alice, CHAT_THEME_GROUP_ID_BASE)
        join_chat_group(alice, group.group_id, alice_id)
        alice.send(RemovePlayerFromGroup(group.group_id, alice_id))
        alice.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP)
        # An hour later a player's empty chat group would be long closed.
        self.server.close_abandoned_chat_groups(seconds_from_now=3600)
        bob, bob_id = self.player_in_room("Bob", 42, CHAT_ZONE_GROUP_ID)
        join_group(bob, CHAT_THEME_GROUP_ID_BASE, bob_id)
        self.assertEqual(receive_permanent_group(bob, CHAT_THEME_GROUP_ID_BASE).group_id, group.group_id)

    def test_switched_off_no_group_is_kept_open(self) -> None:
        """Off by default until a run with the game confirms it: then General lists nothing."""
        self.database.set_setting(Setting.PERMANENT_CHAT_GROUPS, "false")
        alice, alice_id = self.player_in_room("Alice", 17, CHAT_ZONE_GROUP_ID)
        join_group(alice, CHAT_THEME_GROUP_ID_BASE, alice_id)
        alice.assert_nothing_matching(lambda message: message.message_type == MessageType.ADD_GROUP_TO_GROUP_2)


def join_chat_group(client: FakeGameClient, group_id: int, player_id: int) -> None:
    """Join a chat group like the game, and wait until the server answers with the group under
    parent 0, as an AddGroupToGroup2 like every chat group."""
    client.send(AddPlayerToGroup(group_id, player_id, player_address=("0.0.0.0", 0), player_name=""))

    def is_joined(message: RawMessage) -> bool:
        """Whether the message announces the joined chat group under parent 0."""
        if message.message_type != MessageType.ADD_GROUP_TO_GROUP_2:
            return False
        announcement = GroupAnnouncement.decode(message)
        return (announcement.parent_id, announcement.group_id) == (JOINED_GROUP_PARENT_ID, group_id)

    client.receive_matching(is_joined)


def receive_permanent_group(client: FakeGameClient, theme_group_id: int) -> GroupAnnouncement:
    """The next chat group the client is shown inside a theme."""

    def is_group_in_theme(message: RawMessage) -> bool:
        """Whether the message lists a group inside the theme."""
        if message.message_type != MessageType.ADD_GROUP_TO_GROUP_2:
            return False
        return GroupAnnouncement.decode(message).parent_id == theme_group_id

    return GroupAnnouncement.decode(client.receive_matching(is_group_in_theme))


if __name__ == "__main__":
    unittest.main()
