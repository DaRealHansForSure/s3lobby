"""The game's reliable layer over UDP, as the server keeps it for one client.

The rules, read from protocol.cpp in S3.EXE (docs/protocol.md, "Connection layer"):
- Each side numbers its blocks 1, 2, 3, ... and puts MessageId(n) first in a numbered block.
- Block 1 must carry the sender's OpenConnection. The client only counts the connection as open
  once it receives the server's OpenConnection in a block with id 1 (0x605ec8, 0x607f13).
- The receiver acknowledges every numbered block it got, duplicates included, with
  AcknowledgeMessages, and keeps repeating an id until the sender answers MinMissingAcknowledge(v):
  "I hold your acknowledgements for all my blocks below v" (0x605d90).
- A block nobody acknowledged is sent again after 750, 1500, 3000, 5000, 5000, ... ms.
- Blocks may arrive out of order and more than once. A duplicate is acknowledged again but not
  handed to the lobby again.
- A peer that stays silent for 60 seconds is gone.

Nothing here touches the network: the server feeds received messages in and sends the datagrams
this class asks it to send. That keeps the rules testable with plain function calls.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from s3lobby.constants import (
    ACKNOWLEDGEMENT_MESSAGE_TYPES,
    CLOSING_LINGER_MS,
    CONNECTION_LAYER_MESSAGE_TYPES,
    CONNECTION_NONCE_MAX,
    CONNECTION_NONCE_MIN,
    DEFAULT_SILENCE_TIMEOUT_MS,
    KEEPALIVE_INTERVAL_MS,
    RETRY_INTERVALS_MS,
    MessageType,
)
from s3lobby.messages import (
    AcknowledgeMessages,
    AdjustConnectionTimeout,
    CloseConnection,
    EncodableMessage,
    MessageId,
    MinMissingAcknowledge,
    OpenConnection,
)
from s3lobby.wire import Address, RawMessage, encode_block

# Messages that sit right after MessageId in a numbered block, ahead of the acknowledgements and
# the lobby messages. Same order as the game's own first answer: [MessageId 1][OpenConnection]...
LeadingMessages = list[RawMessage]


@dataclass
class UnacknowledgedBlock:
    """A numbered block the server sent and the client has not acknowledged yet, kept for retries.

    The messages are stored rather than the finished datagram, because every retry carries the
    acknowledgements that are current at that moment.
    """

    block_id: int
    leading_messages: LeadingMessages
    lobby_messages: list[RawMessage]
    last_sent_ms: int
    retry_count: int = 0

    def is_retry_due(self, now_ms: int) -> bool:
        interval_index = min(self.retry_count, len(RETRY_INTERVALS_MS) - 1)
        return now_ms - self.last_sent_ms >= RETRY_INTERVALS_MS[interval_index]


@dataclass
class ReceivedBlock:
    """What one datagram from the client means once the reliable layer has dealt with it."""

    # Lobby messages to act on now: empty for duplicates and for blocks that only acknowledge.
    lobby_messages: list[RawMessage] = field(default_factory=list)
    is_close_requested: bool = False


class ReliableConnection:
    """One client's connection: block numbering, acknowledgements, retries and timeouts."""

    def __init__(self, client_address: Address, client_nonce: int, now_ms: int) -> None:
        self.client_address = client_address
        # The nonce from the client's OpenConnection. A datagram with a different nonce from the
        # same address means the client started a new connection (0x605eec).
        self.client_nonce = client_nonce
        self.server_nonce = random.randint(CONNECTION_NONCE_MIN, CONNECTION_NONCE_MAX)

        self._next_server_block_id = 1
        self._unacknowledged_blocks: dict[int, UnacknowledgedBlock] = {}

        self._received_client_block_ids: set[int] = set()
        self._client_block_ids_to_acknowledge: set[int] = set()
        self._is_acknowledgement_due = False

        # The server's first block must open the connection from its side.
        self._queued_leading_messages: LeadingMessages = [OpenConnection(self.server_nonce).encode()]
        self._queued_lobby_messages: list[RawMessage] = []

        self._last_heard_from_client_ms = now_ms
        self._last_sent_to_client_ms = now_ms
        self._silence_timeout_ms = DEFAULT_SILENCE_TIMEOUT_MS
        self._closing_since_ms: int | None = None

    @property
    def is_closing(self) -> bool:
        return self._closing_since_ms is not None

    def receive_block(self, messages: list[RawMessage], now_ms: int) -> ReceivedBlock:
        """Apply one decoded datagram from the client.

        Raises ProtocolError when a connection layer message in it is malformed.
        """
        self._last_heard_from_client_ms = now_ms
        received = ReceivedBlock()

        client_block_id = None
        if messages and messages[0].message_type == MessageType.MESSAGE_ID:
            client_block_id = MessageId.decode(messages[0]).block_id
            messages = messages[1:]

        for message in messages:
            self._apply_connection_layer_message(message, received)
        lobby_messages = [m for m in messages if m.message_type not in CONNECTION_LAYER_MESSAGE_TYPES]

        if client_block_id is None:
            # Sent "unguaranteed": no number, no acknowledgement, handed over as it is.
            received.lobby_messages = lobby_messages
            return received

        # The client's acknowledgement-only blocks repeat the id of its last real block. They hold
        # nothing to acknowledge or deliver, and acknowledging them again would only cause noise.
        is_acknowledgement_only = all(m.message_type in ACKNOWLEDGEMENT_MESSAGE_TYPES for m in messages)
        if is_acknowledgement_only:
            return received

        self._client_block_ids_to_acknowledge.add(client_block_id)
        self._is_acknowledgement_due = True
        is_duplicate = client_block_id in self._received_client_block_ids
        if is_duplicate:
            return received
        self._received_client_block_ids.add(client_block_id)
        received.lobby_messages = lobby_messages
        return received

    def queue_lobby_message(self, message: EncodableMessage) -> None:
        """Queue a message; it leaves with the next numbered block."""
        self._queued_lobby_messages.append(message.encode())

    def begin_closing(self, now_ms: int) -> None:
        """Answer the client's CloseConnection with the server's own, then let the connection end."""
        self._queued_leading_messages.append(CloseConnection().encode())
        self._closing_since_ms = now_ms

    def is_finished(self, now_ms: int) -> bool:
        """True once the client is silent for too long, or a closed connection has nothing left to
        deliver (or has waited long enough for its last acknowledgement)."""
        silence_ms = now_ms - self._last_heard_from_client_ms
        if silence_ms > self._silence_timeout_ms:
            return True
        if self._closing_since_ms is None:
            return False
        has_delivered_everything = not self._unacknowledged_blocks
        has_waited_long_enough = now_ms - self._closing_since_ms > CLOSING_LINGER_MS
        return has_delivered_everything or has_waited_long_enough

    def take_datagrams_to_send(self, now_ms: int) -> list[bytes]:
        """Everything that should go out now: a new block, due retries, or acknowledgements and a
        keepalive when nothing else is going out."""
        datagrams = []
        has_queued_messages = bool(self._queued_leading_messages or self._queued_lobby_messages)
        if has_queued_messages:
            datagrams.append(self._send_queued_messages_as_new_block(now_ms))

        for block in self._unacknowledged_blocks.values():
            if block.is_retry_due(now_ms):
                block.retry_count += 1
                block.last_sent_ms = now_ms
                datagrams.append(self._encode_numbered_block(block))

        # Acknowledgements ride along in every numbered block; only when none is going out do they
        # need a block of their own. The same block doubles as the keepalive.
        is_keepalive_due = now_ms - self._last_sent_to_client_ms >= KEEPALIVE_INTERVAL_MS
        needs_acknowledgement_block = self._is_acknowledgement_due or is_keepalive_due
        if not datagrams and needs_acknowledgement_block:
            datagrams.append(self._encode_acknowledgement_block())

        if datagrams:
            self._is_acknowledgement_due = False
            self._last_sent_to_client_ms = now_ms
        return datagrams

    def _apply_connection_layer_message(self, message: RawMessage, received: ReceivedBlock) -> None:
        match message.message_type:
            case MessageType.ACKNOWLEDGE_MESSAGES:
                for block_id in AcknowledgeMessages.decode(message).block_ids:
                    self._unacknowledged_blocks.pop(block_id, None)
            case MessageType.MIN_MISSING_ACKNOWLEDGE:
                lowest_unconfirmed = MinMissingAcknowledge.decode(message).lowest_unconfirmed_block_id
                still_unconfirmed = {i for i in self._client_block_ids_to_acknowledge if i >= lowest_unconfirmed}
                self._client_block_ids_to_acknowledge = still_unconfirmed
            case MessageType.CLOSE_CONNECTION:
                received.is_close_requested = True
            case MessageType.ADJUST_CONNECTION_TIMEOUT:
                # The client raises its own timeout to ten minutes around a running game, while it
                # may not talk to the lobby at all; follow it, but never wait less than the default.
                requested_timeout_ms = AdjustConnectionTimeout.decode(message).timeout_ms
                self._silence_timeout_ms = max(DEFAULT_SILENCE_TIMEOUT_MS, requested_timeout_ms)
            case _:
                # MessageId further into a block means nothing (0x601d70); OpenConnection was
                # already read by the server to pick this connection; lobby messages are not ours.
                pass

    def _send_queued_messages_as_new_block(self, now_ms: int) -> bytes:
        block = UnacknowledgedBlock(
            block_id=self._next_server_block_id,
            leading_messages=self._queued_leading_messages,
            lobby_messages=self._queued_lobby_messages,
            last_sent_ms=now_ms,
        )
        self._next_server_block_id += 1
        self._queued_leading_messages = []
        self._queued_lobby_messages = []
        self._unacknowledged_blocks[block.block_id] = block
        return self._encode_numbered_block(block)

    def _encode_numbered_block(self, block: UnacknowledgedBlock) -> bytes:
        messages = [MessageId(block.block_id).encode()]
        messages += block.leading_messages
        messages += self._encode_acknowledgements()
        messages += block.lobby_messages
        return encode_block(messages)

    def _encode_acknowledgement_block(self) -> bytes:
        # Like the client, repeat the id of the last numbered block; the receiver ignores it.
        messages = []
        last_server_block_id = self._next_server_block_id - 1
        if last_server_block_id > 0:
            messages.append(MessageId(last_server_block_id).encode())
        messages += self._encode_acknowledgements()
        return encode_block(messages)

    def _encode_acknowledgements(self) -> list[RawMessage]:
        acknowledge = AcknowledgeMessages(tuple(sorted(self._client_block_ids_to_acknowledge)))
        if self._unacknowledged_blocks:
            lowest_unacknowledged = min(self._unacknowledged_blocks)
        else:
            lowest_unacknowledged = self._next_server_block_id
        min_missing = MinMissingAcknowledge(lowest_unacknowledged)
        return [acknowledge.encode(), min_missing.encode()]
