"""Chat commands, server announcements, the welcome line and game reports."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from s3lobby.commands import MAX_ANSWER_LINE_LENGTH, MAX_ANSWER_LINES, ChatCommands, fit_answer, split_command
from s3lobby.constants import ANNOUNCEMENT_GROUP_ID, MessageType, ResultCode
from s3lobby.database import LobbyDatabase, Setting
from s3lobby.messages import ChatCommand, ChatCommandAnswer, GameStatus, Result
from tests.support import LobbyTestCase, log_on

GAME_ZONE = 30


@dataclass
class Sender:
    player_id: int
    name: str
    is_admin: bool = False


class ChatCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = LobbyDatabase(":memory:")
        self.alice = self.database.create_account("Alice")
        self.online = ["Alice", "Bob"]
        self.announced: list[str] = []
        self.commands = ChatCommands(self.database, lambda: self.online, self.record_announcement)

    def tearDown(self) -> None:
        self.database.close()

    def record_announcement(self, text: str) -> int:
        self.announced.append(text)
        return len(self.online)

    def test_command_name_is_case_insensitive_and_the_slash_optional(self) -> None:
        self.assertEqual(split_command("/ANNOUNCE  Server restart at 22:00 "), ("announce", "Server restart at 22:00"))
        self.assertEqual(split_command("who"), ("who", ""))

    def test_who_lists_the_players_online(self) -> None:
        self.assertEqual(self.commands.answer(Sender(1, "Alice"), "/who"), ("2 player(s) online: Alice, Bob",))

    def test_clan_names_the_members_online(self) -> None:
        self.assertEqual(self.commands.answer(Sender(self.alice.account_id, "Alice"), "/clan"), ("You are in no clan.",))
        romans = self.database.create_clan("Romans")
        self.database.set_clan_of_account(self.alice.account_id, romans.clan_id)
        answer = self.commands.answer(Sender(self.alice.account_id, "Alice"), "/clan")
        self.assertEqual(answer, ("Clan Romans: 1 member(s), online: Alice",))

    def test_only_admins_announce(self) -> None:
        self.assertEqual(self.commands.answer(Sender(1, "Alice"), "/announce hello"), ("Only admins may announce.",))
        self.assertEqual(self.announced, [])
        answer = self.commands.answer(Sender(1, "Alice", is_admin=True), "/announce hello")
        self.assertEqual((answer, self.announced), (("Announced to 2 player(s).",), ["hello"]))

    def test_unknown_command_points_to_help(self) -> None:
        self.assertIn("/help", self.commands.answer(Sender(1, "Alice"), "/dance")[0])

    def test_answers_are_wrapped_and_capped(self) -> None:
        lines = fit_answer(["word " * 500])
        self.assertEqual(len(lines), MAX_ANSWER_LINES)
        self.assertTrue(all(len(line) <= MAX_ANSWER_LINE_LENGTH for line in lines))


class ServerLinesTest(LobbyTestCase):
    def test_chat_command_is_answered_in_the_chat(self) -> None:
        alice, _ = self.player_in_room("Alice", 17, GAME_ZONE)
        alice.send(ChatCommand(group_id=GAME_ZONE, unknown_value=0, command="/who"))
        answer = ChatCommandAnswer.decode(alice.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertEqual(answer.lines, ("1 player(s) online: Alice",))

    def test_queued_announcement_reaches_every_player(self) -> None:
        """The command line queues it in the database; the running server sends it within a second."""
        alice, _ = self.player_in_room("Alice", 17, GAME_ZONE)
        bob, _ = self.player_in_room("Bob", 42, GAME_ZONE)
        self.database.queue_announcement("Server restart at 22:00")
        for client in (alice, bob):
            line = ChatCommandAnswer.decode(client.receive_next(MessageType.CHAT_COMMAND_ANSWER))
            self.assertEqual((line.group_id, line.lines), (ANNOUNCEMENT_GROUP_ID, ("Server restart at 22:00",)))

    def test_welcome_line_follows_the_logon(self) -> None:
        self.database.set_setting(Setting.WELCOME_MESSAGE, "Welcome to the LAN lobby!")
        alice = self.new_client("Alice", 17)
        log_on(alice)
        line = ChatCommandAnswer.decode(alice.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertEqual(line.lines, ("Welcome to the LAN lobby!",))

    def test_game_report_is_stored_and_confirmed(self) -> None:
        """The game shows "Game status stored at the Lobby server." for Result 0x6c."""
        alice, _ = self.player_in_room("Alice", 17, GAME_ZONE)
        alice.send(GameStatus(group_id=0x100000, rest_of_body=bytes(16)))
        self.assertEqual(Result.decode(alice.receive_next(MessageType.RESULT)).code, ResultCode.GAME_STATUS_STORED)
        self.assertEqual(self.database.count_game_reports(), 1)


if __name__ == "__main__":
    unittest.main()
