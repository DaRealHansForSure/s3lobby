"""Abuse cases: what a hostile or broken sender tries, and what the server must do about it.

Each test names the attack in its docstring. The server must never stop, never hold unbounded
state for one sender, and never write a secret into its log.
"""

from __future__ import annotations

import logging
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from s3lobby.connection import ReliableConnection
from s3lobby.constants import (
    CLIENT_BLOCK_ID_WINDOW,
    MAX_UNACKNOWLEDGED_SERVER_BLOCKS,
    UNCONFIRMED_CONNECTION_TIMEOUT_MS,
    GroupUpdateMask,
    MessageType,
)
from s3lobby.database import LobbyDatabase
from s3lobby.guards import RateLimiter, ThrottledLogger
from s3lobby.messages import (
    AddPlayerToGroup,
    CheckFileVersion,
    GameBlock,
    GetImageList,
    GetLobbyServerList,
    GetTextMessage,
    GroupUpdate,
    Logon,
    MessageId,
    MinMissingAcknowledge,
    OpenConnection,
    Result,
    describe_for_log,
)
from s3lobby.wire import ProtocolError, RawMessage, encode_block
from tests.support import BackgroundServer, FakeGameClient

CLIENT_ADDRESS = ("10.0.0.2", 3343)
START_MS = 1_000_000


class GuardsTest(unittest.TestCase):
    """The rate limiter and the throttled logger the server uses against floods."""

    def test_rate_limiter_allows_a_burst_then_the_steady_rate(self) -> None:
        """A sender may use the burst at once, is then refused, and earns one more event per 1/rate seconds.
        """
        limiter = RateLimiter(events_per_second=10, burst=5)
        allowed = [limiter.allows("sender", now_seconds=0.0) for _ in range(8)]
        self.assertEqual(allowed, [True] * 5 + [False] * 3)
        self.assertTrue(limiter.allows("sender", now_seconds=0.1))
        self.assertFalse(limiter.allows("sender", now_seconds=0.1))

    def test_rate_limiter_forgets_senders_that_went_quiet(self) -> None:
        """Senders that went quiet are forgotten, so a flood from many forged addresses cannot make the
        limiter's memory grow without end."""
        limiter = RateLimiter(events_per_second=10, burst=5)
        for sender_number in range(100):
            limiter.allows(("forged", sender_number), now_seconds=0.0)
        limiter.forget_quiet_keys(now_seconds=60.0)
        self.assertEqual(limiter.tracked_key_count, 0)

    def test_throttled_logger_holds_back_repeats_and_reports_them(self) -> None:
        """A sender repeating the same fault gets one warning per interval and later a count of the ones held
        back, so a flood cannot fill the log or the disk."""
        logger = logging.getLogger("tests.throttled")
        throttled = ThrottledLogger(logger, interval_seconds=10)
        with self.assertLogs(logger, logging.WARNING) as captured:
            for second in range(5):
                throttled.warning("sender", float(second), "bad datagram")
            throttled.forget_quiet_keys(now_seconds=20.0)
        self.assertEqual(len(captured.records), 2)
        self.assertIn("4 more similar messages held back", captured.records[1].getMessage())


class ConnectionAbuseTest(unittest.TestCase):
    """A ReliableConnection fed what a hostile or broken client sends, after a proper opening block."""

    def setUp(self) -> None:
        """A connection whose client sent a proper block 1 with OpenConnection."""
        self.connection = ReliableConnection(CLIENT_ADDRESS, client_nonce=7, now_ms=START_MS)
        opening = [MessageId(1).encode(), OpenConnection(7).encode()]
        self.connection.receive_block(opening, START_MS)

    def test_block_id_far_ahead_is_refused(self) -> None:
        """A client numbering its blocks wildly would make the duplicate tracking grow."""
        far_ahead = [MessageId(1 + CLIENT_BLOCK_ID_WINDOW + 5).encode(), GetLobbyServerList().encode()]
        with self.assertRaises(ProtocolError):
            self.connection.receive_block(far_ahead, START_MS)

    def test_lobby_messages_without_a_block_id_are_refused(self) -> None:
        """The game sends every lobby message in a numbered block (0x606ff0's unguaranteed flag is
        0 at every call); one without a number comes from someone else at the player's address."""
        unnumbered = [GetLobbyServerList().encode()]
        with self.assertRaises(ProtocolError):
            self.connection.receive_block(unnumbered, START_MS)

    def test_connection_messages_without_a_block_id_still_count(self) -> None:
        """The game sends its connection messages, such as acknowledgements, unnumbered; refusing those along
        with unnumbered lobby messages would break every real client."""
        received = self.connection.receive_block([MinMissingAcknowledge(1).encode()], START_MS)
        self.assertEqual(received.lobby_messages, [])

    def test_connection_that_never_acknowledges_ends_early(self) -> None:
        """An OpenConnection with a forged sender: the server must not keep sending there."""
        self.connection.take_datagrams_to_send(START_MS)
        self.assertFalse(self.connection.is_finished(START_MS + UNCONFIRMED_CONNECTION_TIMEOUT_MS))
        self.assertTrue(self.connection.is_finished(START_MS + UNCONFIRMED_CONNECTION_TIMEOUT_MS + 1))

    def test_client_that_stops_acknowledging_is_overloaded(self) -> None:
        """A client that never acknowledges must not make the server queue blocks forever."""
        for _ in range(MAX_UNACKNOWLEDGED_SERVER_BLOCKS + 1):
            self.connection.queue_lobby_message(Result(9))
            self.connection.take_datagrams_to_send(START_MS)
        self.assertTrue(self.connection.is_overloaded)


class SecretsInLogTest(unittest.TestCase):
    """The datagram descriptions --verbose writes to the log, which must never hold an access code or a chat
    group password."""

    def test_access_code_never_appears_in_the_debug_description(self) -> None:
        """The access code in a Logon is a password; neither as text nor as hex may it land in a log file that
        admins share for debugging."""
        logon = Logon(14, "Alice", "K7QXM2PAR9TB", 0, "0497-2584-2194-4356-2336").encode()
        description = describe_for_log(logon)
        self.assertNotIn("K7QXM2PAR9TB", description)
        self.assertNotIn(b"K7QXM2PAR9TB".hex(), description)

    def test_chat_group_password_is_removed(self) -> None:
        """The password a player gives to join a chat group is left out of the log."""
        join = AddPlayerToGroup(5, 1, ("0.0.0.0", 0), "", extra_text="hunter").encode()
        self.assertNotIn("hunter", describe_for_log(join))

    def test_chat_group_password_set_by_an_update_is_removed(self) -> None:
        """A chat group's password changed by an update is left out of the log too, as text and as hex."""
        update = GroupUpdate(
            message_type=MessageType.UPDATE_GROUP_2,
            group_id=0x8000,
            update_mask=GroupUpdateMask.THIRD_TEXT,
            flags=0,
            max_players=0,
            name="Romans only",
            second_text="",
            third_text="hunter",
            game_block=GameBlock(("0.0.0.0", 3343), bytes(36)),
            description=b"",
        ).encode()
        description = describe_for_log(update)
        self.assertNotIn("hunter", description)
        self.assertNotIn(b"hunter".hex(), description)

    def test_unreadable_secret_bearing_message_is_not_dumped(self) -> None:
        """A malformed message of a type that carries a secret is not logged as a hex dump either, since the
        secret could still be in its bytes."""
        broken_logon = RawMessage(MessageType.LOGON, b"\x0e\x00\x00\x00Alice\x00hunter")
        self.assertNotIn(b"hunter".hex(), describe_for_log(broken_logon))


class ServerUnderAttackTest(unittest.TestCase):
    """A running server receiving datagrams no real game sends."""

    def setUp(self) -> None:
        """A server on a fresh database and a plain UDP socket to attack it from."""
        self.folder = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.folder.name) / "lobby.sqlite3")
        self.server = BackgroundServer(self.database_path)
        self.server.__enter__()
        self.attacker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def tearDown(self) -> None:
        """Close the attacking socket, stop the server and remove the database."""
        self.attacker.close()
        self.server.__exit__(None, None, None)
        self.folder.cleanup()

    def assert_server_still_answers(self) -> None:
        """Fail unless a new client still gets an answer to its version check."""
        client = FakeGameClient(self.server.address, "Alice", nonce=99)
        self.addCleanup(client.close_socket)
        client.send(CheckFileVersion(160, 4, 0))
        client.receive_next(MessageType.RESULT)

    def test_home_texts_are_not_sent_before_a_logon(self) -> None:
        """A request costs a few bytes, the answer a whole news text: sent before a logon, from a
        forged sender address, it would multiply traffic onto someone else."""
        client = FakeGameClient(self.server.address, "Alice", nonce=98)
        self.addCleanup(client.close_socket)
        client.send(GetTextMessage(0x92), GetImageList(1))
        client.assert_nothing_arrives(MessageType.TEXT_MESSAGE)
        client.assert_nothing_arrives(MessageType.IMAGE_LIST)

    def test_connection_that_never_logs_on_ends(self) -> None:
        """A forged sender could otherwise keep connection slots without ever logging on."""
        with mock.patch("s3lobby.server.LOGON_DEADLINE_MS", 0):
            client = FakeGameClient(self.server.address, "Alice", nonce=97)
            self.addCleanup(client.close_socket)
            client.send(CheckFileVersion(160, 4, 0))
            client.receive_next(MessageType.RESULT)
            # Waiting a moment gives the housekeeping time to end the connection.
            client.assert_nothing_arrives(MessageType.LOBBY_SERVER_LIST)
            client.send(GetLobbyServerList())
            client.assert_nothing_arrives(MessageType.LOBBY_SERVER_LIST)

    def test_garbage_datagrams_do_not_stop_the_server(self) -> None:
        """Empty, truncated, random and malformed datagrams are dropped one by one; none may stop the server
        for everyone else."""
        garbage = [
            b"",
            b"\x00",
            b"\xff\xff\xff\xff",
            os.urandom(1400),
            RawMessage(MessageType.OPEN_CONNECTION, b"\x01").encode(),
            encode_block([MessageId(1).encode(), RawMessage(MessageType.OPEN_CONNECTION, b"")]),
            encode_block([MessageId(1).encode(), OpenConnection(5).encode(), RawMessage(MessageType.LOGON, b"x")]),
        ]
        for datagram in garbage:
            self.attacker.sendto(datagram, self.server.address)
        self.assert_server_still_answers()

    def test_connections_from_one_ip_are_limited(self) -> None:
        """Opening connection after connection from one IP must hit a ceiling."""
        connection_limit = 3
        with mock.patch("s3lobby.server.MAX_CONNECTIONS_PER_IP", connection_limit):
            clients = [FakeGameClient(self.server.address, "Bob", nonce=10 + i) for i in range(connection_limit + 1)]
            for client in clients:
                self.addCleanup(client.close_socket)
                client.send(GetLobbyServerList())
            for client in clients[:connection_limit]:
                client.receive_next(MessageType.LOBBY_SERVER_LIST)
            with self.assertRaises(socket.timeout):
                clients[connection_limit].receive_next(MessageType.LOBBY_SERVER_LIST)

    def test_database_file_is_private_to_its_owner(self) -> None:
        """The database the server creates holds access code hashes and player data, so only its owner may
        read it."""
        mode = stat.S_IMODE(Path(self.database_path).stat().st_mode)
        self.assertEqual(mode, 0o600)


class DatabaseFileTest(unittest.TestCase):
    """The permissions of a database file the lobby creates."""

    def test_new_database_file_is_created_owner_only(self) -> None:
        """A new database file is created readable by its owner only, not tightened afterwards, so it never
        exists with wider permissions."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new.sqlite3"
            LobbyDatabase(path).close()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
