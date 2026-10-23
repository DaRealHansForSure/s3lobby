"""Chat commands, server announcements, the welcome line and game reports."""

from __future__ import annotations

import re
import time
import unittest
from dataclasses import dataclass

from s3lobby.commands import MAX_ANSWER_LINE_LENGTH, MAX_ANSWER_LINES, ChatCommands, fit_answer, split_command
from s3lobby.constants import ANNOUNCEMENT_GROUP_ID, CONFIRMATION_SECONDS, GameStatusFlag, MessageType, ResultCode
from s3lobby.database import LobbyDatabase, Setting
from s3lobby.messages import ChatCommand, ChatCommandAnswer, GameStatus, Result
from s3lobby.reachability import Reachability
from tests.support import LobbyTestCase, log_on

GAME_ZONE = 30
# How long a test waits for the running server to take a queued line: it polls every second.
ANNOUNCEMENT_POLL_SECONDS = 0.1
MAX_ANNOUNCEMENT_POLLS = 50


@dataclass
class Sender:
    """The player a chat command comes from, as ChatCommands sees it: id, name and the address the server sees
    them at."""

    player_id: int
    name: str
    address: tuple[str, int] = ("192.168.1.20", 3343)


class ChatCommandsTest(unittest.TestCase):
    """ChatCommands on its own, with an in-memory database, a list of who is online, recorded announcements
    and kicks, and a clock the test controls."""

    def setUp(self) -> None:
        """Alice, a plain player, and Boss, an admin, with Alice and Bob online."""
        self.database = LobbyDatabase(":memory:")
        self.alice = self.database.create_account("Alice")
        self.boss = self.database.create_account("Boss", is_admin=True)
        self.online = ["Alice", "Bob"]
        self.announced: list[str] = []
        self.kicked: list[tuple[int, str]] = []
        self.reachability = Reachability.NOT_CHECKED
        self.clock_seconds = 1000.0
        self.commands = ChatCommands(
            self.database,
            lambda: self.online,
            self.record_announcement,
            self.record_kick,
            lambda address: self.reachability,
            clock_seconds=lambda: self.clock_seconds,
        )

    def tearDown(self) -> None:
        """Close the in-memory database."""
        self.database.close()

    def record_announcement(self, text: str) -> int:
        """Record an announcement instead of sending it; returns how many players it reached."""
        self.announced.append(text)
        return len(self.online)

    def confirm(self, sender: Sender, answer: tuple[str, ...]) -> tuple[str, ...]:
        """Confirm an admin command with the code its answer showed."""
        code = re.search(r"/confirm (\d+)", answer[0]).group(1)
        return self.commands.answer(sender, f"/confirm {code}")

    def record_kick(self, player_id: int, reason: str) -> bool:
        """Record a kick instead of carrying it out; the player always counts as online."""
        self.kicked.append((player_id, reason))
        return True

    def test_command_name_is_case_insensitive_and_the_slash_optional(self) -> None:
        """Players type commands in any case and may leave out the slash; the argument keeps its own spaces
        inside but loses them at the ends."""
        self.assertEqual(split_command("/ANNOUNCE  Server restart at 22:00 "), ("announce", "Server restart at 22:00"))
        self.assertEqual(split_command("who"), ("who", ""))

    def test_who_lists_the_players_online(self) -> None:
        """/who names everyone in the lobby."""
        self.assertEqual(self.commands.answer(Sender(1, "Alice"), "/who"), ("2 player(s) online: Alice, Bob",))

    def test_clan_names_the_members_online(self) -> None:
        """/clan says when the player is in no clan, and otherwise names the clan, its size and who of it is
        online."""
        self.assertEqual(self.commands.answer(Sender(self.alice.account_id, "Alice"), "/clan"), ("You are in no clan.",))
        romans = self.database.create_clan("Romans")
        self.database.set_clan_of_account(self.alice.account_id, romans.clan_id)
        answer = self.commands.answer(Sender(self.alice.account_id, "Alice"), "/clan")
        self.assertEqual(answer, ("Clan Romans: 1 member(s), online: Alice",))

    def test_only_admins_announce(self) -> None:
        """/announce is refused to plain players and nothing goes out; an admin's announcement goes out once
        confirmed."""
        self.assertEqual(self.commands.answer(Sender(1, "Alice"), "/announce hello"), ("Only admins may announce.",))
        self.assertEqual(self.announced, [])
        self.database.set_admin(self.alice.account_id, True)
        alice = Sender(self.alice.account_id, "Alice")
        answer = self.confirm(alice, self.commands.answer(alice, "/announce hello"))
        self.assertEqual((answer, self.announced), (("Announced to 2 player(s).",), ["hello"]))

    def test_only_admins_kick_and_ban(self) -> None:
        """/kick and /ban are refused to plain players, and neither removes nor bans anyone."""
        self.assertEqual(self.commands.answer(Sender(1, "Bob"), "/kick Alice"), ("Only admins may kick.",))
        self.assertEqual(self.commands.answer(Sender(1, "Bob"), "/ban Alice 24"), ("Only admins may ban.",))
        self.assertEqual(self.kicked, [])
        self.assertIsNone(self.database.find_account("Alice").banned_until)

    def test_admin_rights_count_as_they_are_now_not_as_at_the_logon(self) -> None:
        """An admin whose rights were taken away while online must lose /kick at once."""
        self.database.set_admin(self.boss.account_id, False)
        self.assertEqual(self.commands.answer(Sender(self.boss.account_id, "Boss"), "/kick Alice"), ("Only admins may kick.",))

    def test_kick_takes_a_name_with_spaces_and_a_reason(self) -> None:
        """Account names may hold spaces, so /kick matches the longest online name the text starts with, in
        any case, and the rest is the reason the player is told."""
        legion = self.database.create_account("Roman Legion")
        self.online.append("Roman Legion")
        boss = Sender(self.boss.account_id, "Boss")
        answer = self.confirm(boss, self.commands.answer(boss, "/kick roman legion spamming the chat"))
        self.assertEqual(answer, ("Removed Roman Legion from the lobby.",))
        self.assertEqual(self.kicked, [(legion.account_id, "An admin removed you from the lobby. Reason: spamming the chat")])

    def test_admin_command_waits_for_its_code(self) -> None:
        """A datagram forged from an admin's address is sent blind; without the code it does nothing."""
        boss = Sender(self.boss.account_id, "Boss")
        answer = self.commands.answer(boss, "/kick Alice")
        self.assertEqual(self.kicked, [], "nothing happens before the confirmation")
        code = re.search(r"/confirm (\d+)", answer[0]).group(1)
        wrong_digit = (int(code[-1]) + 1) % 10
        wrong_code = f"{code[:-1]}{wrong_digit}"
        self.assertEqual(self.commands.answer(boss, f"/confirm {wrong_code}"), ("Wrong code: the command was cancelled.",))
        self.assertEqual(self.commands.answer(boss, f"/confirm {code}"), ("There is nothing to confirm.",))
        self.assertEqual(self.kicked, [])

    def test_confirmation_runs_out(self) -> None:
        """A /confirm given after CONFIRMATION_SECONDS no longer runs the command, so an old code cannot be
        used later."""
        boss = Sender(self.boss.account_id, "Boss")
        answer = self.commands.answer(boss, "/kick Alice")
        self.clock_seconds += CONFIRMATION_SECONDS + 1
        self.assertEqual(self.confirm(boss, answer), ("That command ran out; give it again.",))
        self.assertEqual(self.kicked, [])

    def test_kick_needs_a_player_who_is_online(self) -> None:
        """Kicking someone who is not online shows the usage, and an admin cannot kick themselves out by
        mistake."""
        answer = self.commands.answer(Sender(self.boss.account_id, "Boss"), "/kick Carol")
        self.assertIn("Usage", answer[0])
        self.database.set_admin(self.alice.account_id, True)
        own_answer = self.commands.answer(Sender(self.alice.account_id, "Alice"), "/kick Alice")
        self.assertEqual(own_answer, ("You cannot kick yourself.",))

    def test_ban_bars_an_account_online_or_not_and_queues_its_removal(self) -> None:
        """/ban stores the ban and its reason for any account and queues a kick, which removes the player if
        they are online."""
        carol = self.database.create_account("Carol")
        boss = Sender(self.boss.account_id, "Boss")
        answer = self.confirm(boss, self.commands.answer(boss, "/ban Carol 48 cheating"))
        self.assertEqual(answer, ("Carol is banned for 48 hour(s).",))
        banned = self.database.find_account("Carol")
        self.assertEqual(banned.ban_reason, "cheating")
        [kick] = self.database.take_pending_kicks()
        self.assertEqual(kick.account_id, carol.account_id)
        self.assertIn("cheating", kick.reason)

    def test_ban_needs_hours_in_range(self) -> None:
        """A /ban without hours, with hours that are not a positive number, or for an unknown account only
        shows the usage and bans nobody."""
        for command in ("/ban Alice", "/ban Alice soon", "/ban Alice 0", "/ban Nobody 5"):
            with self.subTest(command=command):
                self.assertIn("Usage", self.commands.answer(Sender(self.boss.account_id, "Boss"), command)[0])
        self.assertIsNone(self.database.find_account("Alice").banned_until)

    def test_net_tells_an_internet_player_whether_the_router_changed_the_port(self) -> None:
        """Chat goes straight between players; a player whose router rewrote the game's port
        misses chat from players they have not written to, and needs to know what to forward."""
        changed = self.commands.answer(Sender(1, "Alice", address=("84.75.10.4", 61012)), "/net")
        self.assertEqual(changed[0], "The server sees you at 84.75.10.4:61012.")
        self.assertIn("changed the game's port 3343 to 61012", " ".join(changed))
        kept = self.commands.answer(Sender(1, "Alice", address=("84.75.10.4", 3343)), "/net")
        self.assertIn("kept port 3343", " ".join(kept))
        self.assertIn("TCP 47624", " ".join(kept))

    def test_net_adds_what_the_reachability_probe_found(self) -> None:
        """/net adds the result of the reachability probe when there is one: blocked, or probably open;
        without a probe it says nothing about it."""
        internet_player = Sender(1, "Alice", address=("84.75.10.4", 3343))
        self.reachability = Reachability.BLOCKED
        self.assertIn("router blocked a test", " ".join(self.commands.answer(internet_player, "/net")))
        self.reachability = Reachability.OPEN
        self.assertIn("others can probably write to you first", " ".join(self.commands.answer(internet_player, "/net")))
        self.reachability = Reachability.NOT_CHECKED
        self.assertNotIn("Check:", " ".join(self.commands.answer(internet_player, "/net")))

    def test_net_tells_a_player_in_the_servers_network_that_nothing_is_needed(self) -> None:
        """A player in the server's own network needs no port forwarding, and /net must not tell them to set
        one up."""
        answer = self.commands.answer(Sender(1, "Alice"), "/net")
        self.assertIn("server's own network", " ".join(answer))
        self.assertNotIn("Forward", " ".join(answer))

    def test_unknown_command_points_to_help(self) -> None:
        """An unknown command is answered with a pointer to /help rather than silence."""
        self.assertIn("/help", self.commands.answer(Sender(1, "Alice"), "/dance")[0])

    def test_answers_are_wrapped_and_capped(self) -> None:
        """A long answer is wrapped into lines of at most MAX_ANSWER_LINE_LENGTH and cut at MAX_ANSWER_LINES,
        so it cannot push the conversation off the chat."""
        lines = fit_answer(["word " * 500])
        self.assertEqual(len(lines), MAX_ANSWER_LINES)
        self.assertTrue(all(len(line) <= MAX_ANSWER_LINE_LENGTH for line in lines))


class ServerLinesTest(LobbyTestCase):
    """Server lines through a running server: command answers, queued announcements, the welcome line and
    answers to game reports."""

    def test_chat_command_is_answered_in_the_chat(self) -> None:
        """A chat command sent from a room is answered with ChatCommandAnswer lines the game shows in its
        chat."""
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

    def test_a_line_for_one_player_reaches_only_them(self) -> None:
        """An admin's message from the web goes to one player as a server line; the others get
        nothing, and the database notes that it arrived (the Announcements page shows it)."""
        alice, alice_id = self.player_in_room("Alice", 17, GAME_ZONE)
        bob, _ = self.player_in_room("Bob", 42, GAME_ZONE)
        self.database.queue_announcement("Please rename your game", account_id=alice_id)
        line = ChatCommandAnswer.decode(alice.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertEqual(line.lines, ("Please rename your game",))
        bob.assert_nothing_arrives(MessageType.CHAT_COMMAND_ANSWER)
        [record] = self.database.list_recent_announcements(limit=1)
        self.assertEqual((record.recipient_name, record.reached_count), ("Alice", 1))

    def test_a_line_for_a_player_who_is_offline_is_noted_as_not_delivered(self) -> None:
        """The line is not kept for a later logon: an admin's remark is about now."""
        carol_id = self.database.find_account("Carol").account_id
        self.player_in_room("Alice", 17, GAME_ZONE)
        self.database.queue_announcement("Are you there?", account_id=carol_id)
        for _ in range(MAX_ANNOUNCEMENT_POLLS):
            [record] = self.database.list_recent_announcements(limit=1)
            if record.reached_count is not None:
                break
            time.sleep(ANNOUNCEMENT_POLL_SECONDS)
        self.assertEqual(record.reached_count, 0)

    def test_welcome_line_follows_the_logon(self) -> None:
        """The welcome message an admin set reaches each player right after the logon."""
        self.database.set_setting(Setting.WELCOME_MESSAGE, "Welcome to the LAN lobby!")
        alice = self.new_client("Alice", 17)
        log_on(alice)
        line = ChatCommandAnswer.decode(alice.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertEqual(line.lines, ("Welcome to the LAN lobby!",))

    def test_game_report_is_stored_and_confirmed(self) -> None:
        """The game shows "Game status stored at the Lobby server." for Result 0x6c."""
        alice, _ = self.player_in_room("Alice", 17, GAME_ZONE)
        alice.send(GameStatus(group_id=0x100000, flags=0, winning_team=0, game_time=40_000, map_name="Elbe"))
        self.assertEqual(Result.decode(alice.receive_next(MessageType.RESULT)).code, ResultCode.GAME_STATUS_STORED)
        self.assertEqual(self.database.count_game_reports(), 1)

    def test_ladder_report_is_stored_but_not_taken_for_a_ranked_result(self) -> None:
        """This server keeps no ladder. Confirming a ladder report as stored (0x6c) would let the
        game tell its players the result counts; 0x6a shows "Game not valid for ladder." instead."""
        alice, _ = self.player_in_room("Alice", 17, GAME_ZONE)
        report = GameStatus(
            group_id=0x100000, flags=GameStatusFlag.LADDER_GAME, winning_team=0, game_time=40_000, map_name="Elbe"
        )
        alice.send(report)
        self.assertEqual(Result.decode(alice.receive_next(MessageType.RESULT)).code, ResultCode.GAME_NOT_VALID_FOR_LADDER)
        self.assertEqual(self.database.count_game_reports(), 1)


if __name__ == "__main__":
    unittest.main()
