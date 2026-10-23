"""The Home tab: news, the statistics table and the banner."""

from __future__ import annotations

import unittest

from s3lobby.constants import (
    HOME_GROUP_ID,
    MAX_TEXT_MESSAGE_BYTES,
    NEWS_TEXT_ID_BY_AUDIENCE,
    UNKNOWN_TEXT_TITLE,
    GroupFlag,
    MessageType,
)
from s3lobby.database import Setting
from s3lobby.messages import (
    AddPlayerToGroup,
    GetImageList,
    GetTextMessage,
    GroupAnnouncement,
    ImageList,
    ServerStatistics,
    TextIdList,
    TextMessage,
)
from tests.support import FakeGameClient, LobbyTestCase, join_group, log_on

GERMAN_NEWS_ID = NEWS_TEXT_ID_BY_AUDIENCE["german"]
OTHER_NEWS_ID = NEWS_TEXT_ID_BY_AUDIENCE["other"]


class HomeTabTest(LobbyTestCase):
    def enter_home(self, player_name: str = "Alice", nonce: int = 17) -> FakeGameClient:
        client = self.new_client(player_name, nonce)
        player_id = log_on(client)
        join_group(client, HOME_GROUP_ID, player_id)
        return client

    def test_news_ids_lead_to_the_text_with_its_title(self) -> None:
        """The client picks 0x91 or 0x92 from the list and asks for it; a text without a title
        would leave the page at "Loading message" for good (0x5cb642)."""
        self.database.set_news("german", "Willkommen", "Erste Zeile\nZweite Zeile")
        alice = self.enter_home()
        offered = TextIdList.decode(alice.receive_next(MessageType.TEXT_ID_LIST))
        self.assertEqual((offered.group_id, offered.text_ids), (HOME_GROUP_ID, (GERMAN_NEWS_ID,)))

        alice.send(GetTextMessage(GERMAN_NEWS_ID))
        news = TextMessage.decode(alice.receive_next(MessageType.TEXT_MESSAGE))
        self.assertEqual((news.text_id, news.title, news.lines), (GERMAN_NEWS_ID, "Willkommen", ("Erste Zeile", "Zweite Zeile")))

    def test_without_news_the_list_is_empty_and_unknown_ids_still_get_a_title(self) -> None:
        alice = self.enter_home()
        self.assertEqual(TextIdList.decode(alice.receive_next(MessageType.TEXT_ID_LIST)).text_ids, ())
        alice.send(GetTextMessage(OTHER_NEWS_ID))
        self.assertEqual(TextMessage.decode(alice.receive_next(MessageType.TEXT_MESSAGE)).title, UNKNOWN_TEXT_TITLE)

    def test_long_news_is_cut_to_one_datagram(self) -> None:
        self.database.set_news("other", "Welcome", "\n".join(f"line {number} " + "x" * 60 for number in range(100)))
        alice = self.enter_home()
        alice.send(GetTextMessage(OTHER_NEWS_ID))
        news = alice.receive_next(MessageType.TEXT_MESSAGE)
        self.assertLessEqual(len(news.body), MAX_TEXT_MESSAGE_BYTES + 16)

    def test_home_receives_statistics_and_they_follow_logons(self) -> None:
        alice = self.enter_home()
        first = ServerStatistics.decode(alice.receive_next(MessageType.SERVER_STATISTICS))
        self.assertEqual(first.players_online, 1)
        log_on(self.new_client("Bob", 42))
        second = ServerStatistics.decode(alice.receive_next(MessageType.SERVER_STATISTICS))
        self.assertEqual(second.players_online, 2)

    def test_banner_turns_on_the_image_request_and_is_offered(self) -> None:
        self.database.set_setting(Setting.BANNER_URL, "ftp://192.168.1.10/banner/lobby.png")
        self.database.set_setting(Setting.BANNER_ID, "7")
        client = self.new_client("Alice", 17)
        player_id = log_on(client)
        client.send(AddPlayerToGroup(HOME_GROUP_ID, player_id, ("0.0.0.0", 0), ""))
        home = GroupAnnouncement.decode(client.receive_next(MessageType.ADD_GROUP_TO_GROUP))
        self.assertTrue(home.flags & GroupFlag.RECEIVES_STATISTICS)
        self.assertTrue(home.flags & GroupFlag.REQUESTS_IMAGES)
        client.send(GetImageList(HOME_GROUP_ID))
        images = ImageList.decode(client.receive_next(MessageType.IMAGE_LIST))
        self.assertEqual((images.images[0].banner_id, images.images[0].url), (7, "ftp://192.168.1.10/banner/lobby.png"))


if __name__ == "__main__":
    unittest.main()
