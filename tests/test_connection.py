"""The reliable layer on its own: numbering, acknowledgements, duplicates, retries, timeouts.

ReliableConnection never touches the network, so these tests feed it blocks and read the datagrams
it wants to send, with a clock they control.
"""

from __future__ import annotations

import unittest

from s3lobby.constants import DEFAULT_SILENCE_TIMEOUT_MS, KEEPALIVE_INTERVAL_MS, RETRY_INTERVALS_MS, MessageType
from s3lobby.connection import ReliableConnection
from s3lobby.messages import (
    AcknowledgeMessages,
    AdjustConnectionTimeout,
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
    return [MessageId(block_id).encode(), *messages]


def messages_of_type(datagram: bytes, message_type: MessageType) -> list[RawMessage]:
    return [message for message in decode_block(datagram) if message.message_type == message_type]


def retries_of_block_one(datagrams: list[bytes]) -> list[bytes]:
    """The datagrams among these that send the server's block 1, recognisable by its OpenConnection."""
    return [datagram for datagram in datagrams if messages_of_type(datagram, MessageType.OPEN_CONNECTION)]


class ReliableConnectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = ReliableConnection(CLIENT_ADDRESS, CLIENT_NONCE, START_MS)

    def open_connection(self) -> None:
        """The client's block 1 and the server's answer, acknowledged by the client."""
        first_block = client_block(1, OpenConnection(CLIENT_NONCE).encode(), GetLobbyServerList().encode())
        self.connection.receive_block(first_block, START_MS)
        self.connection.queue_lobby_message(Result(9))
        self.connection.take_datagrams_to_send(START_MS)
        acknowledgement = client_block(1, AcknowledgeMessages((1,)).encode(), MinMissingAcknowledge(2).encode())
        self.connection.receive_block(acknowledgement, START_MS)

    def test_first_block_is_number_one_and_opens_the_connection(self) -> None:
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
        self.open_connection()
        block = client_block(2, GetLobbyServerList().encode())
        self.assertEqual(len(self.connection.receive_block(block, START_MS).lobby_messages), 1)
        self.assertEqual(self.connection.receive_block(block, START_MS).lobby_messages, [])
        [datagram] = self.connection.take_datagrams_to_send(START_MS)
        acknowledged = AcknowledgeMessages.decode(messages_of_type(datagram, MessageType.ACKNOWLEDGE_MESSAGES)[0])
        self.assertIn(2, acknowledged.block_ids)

    def test_acknowledgement_only_block_is_not_acknowledged(self) -> None:
        self.open_connection()
        self.connection.receive_block(client_block(1, MinMissingAcknowledge(2).encode()), START_MS)
        self.assertEqual(self.connection.take_datagrams_to_send(START_MS), [])

    def test_unacknowledged_block_is_retried_on_the_game_schedule(self) -> None:
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
        self.open_connection()
        self.connection.receive_block(client_block(2, GetLobbyServerList().encode()), START_MS)
        self.connection.receive_block(client_block(2, MinMissingAcknowledge(3).encode()), START_MS)
        [datagram] = self.connection.take_datagrams_to_send(START_MS + KEEPALIVE_INTERVAL_MS)
        acknowledged = AcknowledgeMessages.decode(messages_of_type(datagram, MessageType.ACKNOWLEDGE_MESSAGES)[0])
        self.assertEqual(acknowledged.block_ids, ())

    def test_keepalive_when_nothing_else_goes_out(self) -> None:
        self.open_connection()
        self.assertEqual(self.connection.take_datagrams_to_send(START_MS + KEEPALIVE_INTERVAL_MS - 1), [])
        self.assertEqual(len(self.connection.take_datagrams_to_send(START_MS + KEEPALIVE_INTERVAL_MS)), 1)

    def test_silent_client_ends_the_connection_unless_it_announced_a_longer_timeout(self) -> None:
        self.open_connection()
        self.assertTrue(self.connection.is_finished(START_MS + DEFAULT_SILENCE_TIMEOUT_MS + 1))
        self.connection.receive_block(client_block(2, AdjustConnectionTimeout(TEN_MINUTES_MS).encode()), START_MS)
        self.assertFalse(self.connection.is_finished(START_MS + DEFAULT_SILENCE_TIMEOUT_MS + 1))
        self.assertTrue(self.connection.is_finished(START_MS + TEN_MINUTES_MS + 1))


if __name__ == "__main__":
    unittest.main()
