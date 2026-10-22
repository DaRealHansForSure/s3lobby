"""The game's reliable layer over UDP, seen from the server's side of one client.

Rules taken from S3.EXE's protocol.cpp (docs/s3-lobby-protocol.md, "Connection layer"):
- Each side numbers its blocks 1, 2, 3, ... and puts MessageId(n) first in the block.
- Block 1 must carry the sender's OpenConnection; the client only counts the
  connection as open once it receives the server's OpenConnection in block 1.
- The receiver acknowledges every numbered block it got (AcknowledgeMessages, a
  list of ids) until the sender confirms it has those acks with
  MinMissingAcknowledge(v): "I hold your acks for all my blocks below v".
- Blocks are retried after 750, 1500, 3000, 5000 ms; a peer silent for 60 s is gone.
- Blocks may arrive out of order and twice; duplicates are acked again but not
  delivered again.
"""

import random

from wire import (
    ACKNOWLEDGE_MESSAGES,
    ADJUST_CONNECTION_TIMEOUT,
    CLOSE_CONNECTION,
    MESSAGE_ID,
    MIN_MISSING_ACKNOWLEDGE,
    OPEN_CONNECTION,
    BodyBuilder,
    Message,
    encode_block,
)

RETRY_INTERVALS_MS = (750, 1500, 3000, 5000)
DEFAULT_SILENCE_TIMEOUT_MS = 60_000
KEEPALIVE_INTERVAL_MS = 3_000
CLOSING_LINGER_MS = 10_000
CONNECTION_LAYER_TYPES = (
    ACKNOWLEDGE_MESSAGES,
    MIN_MISSING_ACKNOWLEDGE,
    MESSAGE_ID,
    CLOSE_CONNECTION,
    ADJUST_CONNECTION_TIMEOUT,
    OPEN_CONNECTION,
)
ACK_ONLY_TYPES = (ACKNOWLEDGE_MESSAGES, MIN_MISSING_ACKNOWLEDGE)


class SentBlock:
    """A numbered block of ours that the client has not acknowledged yet."""

    def __init__(self, block_id, leading_messages, lobby_messages, sent_at_ms):
        self.block_id = block_id
        self.leading_messages = leading_messages
        self.lobby_messages = lobby_messages
        self.sent_at_ms = sent_at_ms
        self.retry_count = 0

    def is_due(self, now_ms):
        interval = RETRY_INTERVALS_MS[min(self.retry_count, len(RETRY_INTERVALS_MS) - 1)]
        return now_ms - self.sent_at_ms >= interval


class ReceivedBlock:
    """What one incoming datagram meant for the connection."""

    def __init__(self):
        self.lobby_messages = []
        self.peer_nonce = None
        self.is_close = False


class ReliableConnection:
    """One client's connection: numbering, acks, retries and timeouts."""

    def __init__(self, address, now_ms):
        self.address = address
        self.own_nonce = random.randint(1, 255)
        self.peer_nonce = None
        self.next_own_block_id = 1
        self.unacked_blocks = {}
        self.received_block_ids = set()
        self.ids_to_acknowledge = set()
        self.is_ack_due = False
        self.queued_leading = [BodyBuilder().u32(self.own_nonce).build(OPEN_CONNECTION)]
        self.queued_lobby = []
        self.last_heard_ms = now_ms
        self.last_sent_ms = now_ms
        self.silence_timeout_ms = DEFAULT_SILENCE_TIMEOUT_MS
        self.closing_since_ms = None

    def receive(self, messages, now_ms):
        """Apply one decoded datagram; returns the lobby messages to act on."""
        self.last_heard_ms = now_ms
        result = ReceivedBlock()
        block_id = 0
        if messages and messages[0].message_type == MESSAGE_ID:
            block_id = messages[0].reader().u32()
            messages = messages[1:]

        for message in messages:
            self._apply_connection_message(message, result)

        # The client's ack-only blocks repeat its last block id; they carry nothing new to ack.
        is_ack_only = all(message.message_type in ACK_ONLY_TYPES for message in messages)
        if block_id == 0 or is_ack_only:
            if block_id == 0:
                result.lobby_messages = [m for m in messages if m.message_type not in CONNECTION_LAYER_TYPES]
            return result

        self.ids_to_acknowledge.add(block_id)
        self.is_ack_due = True
        if block_id in self.received_block_ids:
            return result
        self.received_block_ids.add(block_id)
        result.lobby_messages = [m for m in messages if m.message_type not in CONNECTION_LAYER_TYPES]
        return result

    def send(self, message):
        self.queued_lobby.append(message)

    def send_close(self, now_ms):
        self.queued_leading.append(Message(CLOSE_CONNECTION))
        self.closing_since_ms = now_ms

    def has_expired(self, now_ms):
        if now_ms - self.last_heard_ms > self.silence_timeout_ms:
            return True
        if self.closing_since_ms is None:
            return False
        is_settled = not self.unacked_blocks
        return is_settled or now_ms - self.closing_since_ms > CLOSING_LINGER_MS

    def collect_datagrams(self, now_ms):
        """Everything that should go out now: a new block, retries, acks or a keepalive."""
        datagrams = []
        if self.queued_leading or self.queued_lobby:
            datagrams.append(self._send_new_block(now_ms))
        for block in self.unacked_blocks.values():
            if block.is_due(now_ms):
                block.retry_count += 1
                block.sent_at_ms = now_ms
                datagrams.append(self._encode_numbered(block))
        is_keepalive_due = now_ms - self.last_sent_ms >= KEEPALIVE_INTERVAL_MS
        if not datagrams and (self.is_ack_due or is_keepalive_due):
            datagrams.append(self._encode_ack_only())
        if datagrams:
            self.is_ack_due = False
            self.last_sent_ms = now_ms
        return datagrams

    def _apply_connection_message(self, message, result):
        reader = message.reader()
        if message.message_type == ACKNOWLEDGE_MESSAGES:
            while reader.remaining() >= 4:
                self.unacked_blocks.pop(reader.u32(), None)
        elif message.message_type == MIN_MISSING_ACKNOWLEDGE:
            lowest_unconfirmed = reader.u32()
            self.ids_to_acknowledge = {i for i in self.ids_to_acknowledge if i >= lowest_unconfirmed}
        elif message.message_type == OPEN_CONNECTION:
            result.peer_nonce = reader.u32()
        elif message.message_type == CLOSE_CONNECTION:
            result.is_close = True
        elif message.message_type == ADJUST_CONNECTION_TIMEOUT:
            # The client raises its timeout to 10 minutes around a running game; follow it.
            self.silence_timeout_ms = max(DEFAULT_SILENCE_TIMEOUT_MS, reader.u32())

    def _send_new_block(self, now_ms):
        block = SentBlock(self.next_own_block_id, self.queued_leading, self.queued_lobby, now_ms)
        self.next_own_block_id += 1
        self.queued_leading = []
        self.queued_lobby = []
        self.unacked_blocks[block.block_id] = block
        return self._encode_numbered(block)

    def _encode_numbered(self, block):
        # Same order as the game's minimal answer: id, OpenConnection/Close, acks, then the lobby messages.
        messages = [BodyBuilder().u32(block.block_id).build(MESSAGE_ID)]
        messages += block.leading_messages
        messages += self._acknowledgements()
        messages += block.lobby_messages
        return encode_block(messages)

    def _encode_ack_only(self):
        messages = []
        last_own_block_id = self.next_own_block_id - 1
        if last_own_block_id > 0:
            messages.append(BodyBuilder().u32(last_own_block_id).build(MESSAGE_ID))
        messages += self._acknowledgements()
        return encode_block(messages)

    def _acknowledgements(self):
        acknowledge = BodyBuilder()
        for block_id in sorted(self.ids_to_acknowledge):
            acknowledge.u32(block_id)
        # Never 0: the game's constructor drops the body for 0 and the reader then asserts.
        if self.unacked_blocks:
            lowest_unacked = min(self.unacked_blocks)
        else:
            lowest_unacked = self.next_own_block_id
        min_missing = BodyBuilder().u32(lowest_unacked).build(MIN_MISSING_ACKNOWLEDGE)
        return [acknowledge.build(ACKNOWLEDGE_MESSAGES), min_missing]
