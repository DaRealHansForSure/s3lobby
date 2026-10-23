"""The reliable layer on its own: numbering, acknowledgements, duplicates, retries, timeouts.

ReliableConnection never touches the network, so these tests feed it blocks and read the datagrams
it wants to send, with a clock they control.
"""

from __future__ import annotations

import unittest

from s3lobby.constants import (
    CLIENT_BLOCK_ID_WINDOW,
    DEFAULT_SILENCE_TIMEOUT_MS,
    KEEPALIVE_INTERVAL_MS,
    MAX_BLOCK_LOBBY_BYTES,
    RETRY_INTERVALS_MS,
    MessageType,
)
from s3lobby.connection import ReliableConnection
from s3lobby.messages import (
    AcknowledgeMessages,
    AdjustConnectionTimeout,
    ChatCommandAnswer,
    GetLobbyServerList,
    MessageId,
    MinMissingAcknowledge,
    OpenConnection,
    Result,
)
from s3lobby.wire import RawMessage, decode_block

CLIENT_ADDRESS = ("10.0.0.2", 3343)
CLIENT_NONCE = 27
START_MS = 1_000_000
TEN_MINUTES_MS = 600_000


def client_block(block_id: int, *messages: RawMessage) -> list[RawMessage]:
    """A numbered block from the client: MessageId(block_id) followed by the messages."""
    return [MessageId(block_id).encode(), *messages]


def messages_of_type(datagram: bytes, message_type: MessageType) -> list[RawMessage]:
    """The messages of one type in a datagram the server wants to send."""
    return [message for message in decode_block(datagram) if message.message_type == message_type]


def retries_of_block_one(datagrams: list[bytes]) -> list[bytes]:
    """The datagrams among these that send the server's block 1, recognisable by its OpenConnection."""
    return [datagram for datagram in datagrams if messages_of_type(datagram, MessageType.OPEN_CONNECTION)]


class ReliableConnectionTest(unittest.TestCase):
    """The connection rules read from the game's protocol.cpp, checked on ReliableConnection with a clock the
    test controls."""

    def setUp(self) -> None:
        """A connection for one client, created as the server does when the client's OpenConnection arrives.
        """
        self.connection = ReliableConnection(CLIENT_ADDRESS, CLIENT_NONCE, START_MS)

    def open_connection(self) -> None:
        """The client's block 1 and the server's answer, acknowledged by the client."""
        first_block = client_block(1, OpenConnection(CLIENT_NONCE).encode(), GetLobbyServerList().encode())
        self.connection.receive_block(first_block, START_MS)
        self.connection.queue_lobby_message(Result(9))
        self.connection.take_datagrams_to_send(START_MS)
        acknowledgement = client_block(1, AcknowledgeMessages((1,)).encode(), MinMissingAcknowledge(2).encode())
        self.connection.receive_block(acknowledgement, START_MS)

    def test_close_started_by_the_server_waits_for_earlier_blocks(self) -> None:
        """An open client throws a sync error on a CloseConnection while an earlier block of the
        server's is missing (CheckInSync 0x606310): a kick's reason line lost on the way would
        turn the clean close into an error."""
        self.open_connection()
        self.connection.queue_lobby_message(ChatCommandAnswer(0, ("You were removed.",)))
        [reason_block] = self.connection.take_datagrams_to_send(START_MS)
        self.connection.begin_closing(START_MS, waits_for_acknowledgements=True)
        held_back = self.connection.take_datagrams_to_send(START_MS)
        self.assertFalse(any(messages_of_type(datagram, MessageType.CLOSE_CONNECTION) for datagram in held_back))
        reason_block_id = MessageId.decode(decode_block(reason_block)[0]).block_id
        acknowledgement = client_block(1, AcknowledgeMessages((reason_block_id,)).encode())
        self.connection.receive_block(acknowledgement, START_MS)
        released = self.connection.take_datagrams_to_send(START_MS)
        self.assertTrue(any(messages_of_type(datagram, MessageType.CLOSE_CONNECTION) for datagram in released))

    def test_answer_to_the_client_close_goes_out_at_once(self) -> None:
        """A client that sent CloseConnection is closing already and takes the answer as it is."""
        self.open_connection()
        self.connection.queue_lobby_message(Result(9))
        self.connection.take_datagrams_to_send(START_MS)
        self.connection.begin_closing(START_MS)
        datagrams = self.connection.take_datagrams_to_send(START_MS)
        self.assertTrue(any(messages_of_type(datagram, MessageType.CLOSE_CONNECTION) for datagram in datagrams))

    def test_first_block_is_number_one_and_opens_the_connection(self) -> None:
        """The server's first block must be block 1 and carry OpenConnection right after MessageId: the client
        only counts the connection as open on exactly that (0x605ec8, 0x607f13), and drops everything else.
        The client's lobby messages from its own block 1 reach the lobby, and the block is acknowledged."""
        received = self.connection.receive_block(
            client_block(1, OpenConnection(CLIENT_NONCE).encode(), GetLobbyServerList().encode()),
            START_MS,
        )
        self.assertEqual([m.message_type for m in received.lobby_messages], [MessageType.GET_LOBBY_SERVER_LIST])
        self.connection.queue_lobby_message(Result(9))
        [datagram] = self.connection.take_datagrams_to_send(START_MS)
        types = [message.message_type for message in decode_block(datagram)]
        self.assertEqual(
            types,
            [
                MessageType.MESSAGE_ID,
                MessageType.OPEN_CONNECTION,
                MessageType.ACKNOWLEDGE_MESSAGES,
                MessageType.MIN_MISSING_ACKNOWLEDGE,
                MessageType.RESULT,
            ],
        )
        self.assertEqual(MessageId.decode(decode_block(datagram)[0]).block_id, 1)
        acknowledged = AcknowledgeMessages.decode(messages_of_type(datagram, MessageType.ACKNOWLEDGE_MESSAGES)[0])
        self.assertEqual(acknowledged.block_ids, (1,))

    def test_duplicate_block_is_acknowledged_again_but_not_delivered_again(self) -> None:
        """A block may arrive twice when an acknowledgement got lost. It must be acknowledged again, or the
        client keeps sending it, but handed to the lobby only once, or a request would be carried out
        twice."""
        self.open_connection()
        block = client_block(2, GetLobbyServerList().encode())
        self.assertEqual(len(self.connection.receive_block(block, START_MS).lobby_messages), 1)
        self.assertEqual(self.connection.receive_block(block, START_MS).lobby_messages, [])
        [datagram] = self.connection.take_datagrams_to_send(START_MS)
        acknowledged = AcknowledgeMessages.decode(messages_of_type(datagram, MessageType.ACKNOWLEDGE_MESSAGES)[0])
        self.assertIn(2, acknowledged.block_ids)

    def test_acknowledgement_only_block_is_not_acknowledged(self) -> None:
        """The client's acknowledgement-only blocks repeat the id of its last real block; acknowledging them
        would answer every acknowledgement with another one."""
        self.open_connection()
        self.connection.receive_block(client_block(1, MinMissingAcknowledge(2).encode()), START_MS)
        self.assertEqual(self.connection.take_datagrams_to_send(START_MS), [])

    def test_unacknowledged_block_is_retried_on_the_game_schedule(self) -> None:
        """A block the client did not acknowledge is sent again after 750, 1500, 3000 and 5000 ms, the game's
        own retry table, and not earlier."""
        self.connection.receive_block(client_block(1, OpenConnection(CLIENT_NONCE).encode()), START_MS)
        self.connection.take_datagrams_to_send(START_MS)
        now_ms = START_MS
        for interval_ms in RETRY_INTERVALS_MS:
            # Keepalives may go out in between; a retry is a datagram that repeats block 1's content.
            too_early = self.connection.take_datagrams_to_send(now_ms + interval_ms - 1)
            self.assertEqual(retries_of_block_one(too_early), [])
            now_ms += interval_ms
            on_time = self.connection.take_datagrams_to_send(now_ms)
            self.assertEqual(len(retries_of_block_one(on_time)), 1)

    def test_min_missing_acknowledge_stops_repeating_acknowledgements(self) -> None:
        """Once the client says with MinMissingAcknowledge that it holds the acknowledgements for its blocks,
        the server stops repeating them (0x605d90)."""
        self.open_connection()
        self.connection.receive_block(client_block(2, GetLobbyServerList().encode()), START_MS)
        self.connection.receive_block(client_block(2, MinMissingAcknowledge(3).encode()), START_MS)
        [datagram] = self.connection.take_datagrams_to_send(START_MS + KEEPALIVE_INTERVAL_MS)
        acknowledged = AcknowledgeMessages.decode(messages_of_type(datagram, MessageType.ACKNOWLEDGE_MESSAGES)[0])
        self.assertEqual(acknowledged.block_ids, ())

    def test_keepalive_when_nothing_else_goes_out(self) -> None:
        """With nothing to send the server still sends a datagram every KEEPALIVE_INTERVAL_MS, well within the
        client's silence timeout, so an idle player is not dropped."""
        self.open_connection()
        self.assertEqual(self.connection.take_datagrams_to_send(START_MS + KEEPALIVE_INTERVAL_MS - 1), [])
        self.assertEqual(len(self.connection.take_datagrams_to_send(START_MS + KEEPALIVE_INTERVAL_MS)), 1)

    def test_silent_client_ends_the_connection_unless_it_announced_a_longer_timeout(self) -> None:
        """A client silent for a minute is gone, but around a running game the client raises its timeout to
        ten minutes and may not talk to the lobby at all; ending the connection then would throw the player
        out of the lobby while they play."""
        self.open_connection()
        self.assertTrue(self.connection.is_finished(START_MS + DEFAULT_SILENCE_TIMEOUT_MS + 1))
        self.connection.receive_block(client_block(2, AdjustConnectionTimeout(TEN_MINUTES_MS).encode()), START_MS)
        self.assertFalse(self.connection.is_finished(START_MS + DEFAULT_SILENCE_TIMEOUT_MS + 1))
        self.assertTrue(self.connection.is_finished(START_MS + TEN_MINUTES_MS + 1))

    def test_timeout_a_client_asks_for_is_capped_at_what_the_game_sends(self) -> None:
        """A forged sender asking for 49 days would hold a connection slot that long."""
        self.open_connection()
        self.connection.receive_block(client_block(2, AdjustConnectionTimeout(0xFFFFFFFF).encode()), START_MS)
        self.assertTrue(self.connection.is_finished(START_MS + TEN_MINUTES_MS + 1))

    def test_acknowledgements_stay_within_the_window_without_min_missing(self) -> None:
        """A sender that never confirms would otherwise make every reply carry every id it sent."""
        self.open_connection()
        for block_id in range(2, 2 + 3 * CLIENT_BLOCK_ID_WINDOW):
            self.connection.receive_block(client_block(block_id, GetLobbyServerList().encode()), START_MS)
        [datagram] = self.connection.take_datagrams_to_send(START_MS + KEEPALIVE_INTERVAL_MS)
        acknowledged = AcknowledgeMessages.decode(messages_of_type(datagram, MessageType.ACKNOWLEDGE_MESSAGES)[0])
        self.assertLessEqual(len(acknowledged.block_ids), CLIENT_BLOCK_ID_WINDOW + 1)

    def test_queue_too_large_for_one_block_goes_out_in_order(self) -> None:
        """One datagram cannot hold everything; the rest follows only once the client has the
        first part, since the game hands blocks on in the order they arrive."""
        self.open_connection()
        large_text = "x" * 1000
        for line_number in range(2 * MAX_BLOCK_LOBBY_BYTES // len(large_text)):
            self.connection.queue_lobby_message(ChatCommandAnswer(0, (f"{line_number} {large_text}",)))
        [first] = self.connection.take_datagrams_to_send(START_MS)
        self.assertLessEqual(len(first), MAX_BLOCK_LOBBY_BYTES + 1024)
        self.assertEqual(self.connection.take_datagrams_to_send(START_MS), [], "held back until acknowledged")
        first_block_id = MessageId.decode(decode_block(first)[0]).block_id
        acknowledgement = client_block(1, AcknowledgeMessages((first_block_id,)).encode())
        self.connection.receive_block(acknowledgement, START_MS)
        [second] = self.connection.take_datagrams_to_send(START_MS)
        self.assertEqual(MessageId.decode(decode_block(second)[0]).block_id, first_block_id + 1)


if __name__ == "__main__":
    unittest.main()
