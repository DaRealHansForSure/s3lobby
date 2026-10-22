"""Test helpers: a server running in the background, and a fake game client.

The fake client follows the rules read from the game's code rather than this server's code: it
numbers its blocks, opens with OpenConnection in block 1, acknowledges the server's blocks, and
checks every size the real client asserts. A test passing against it shows the server keeps the
protocol as documented; only a run with the real game shows the documentation is right.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Callable

from s3lobby.constants import ACKNOWLEDGEMENT_MESSAGE_TYPES, MESSAGE_HEADER_SIZE, MessageType
from s3lobby.messages import (
    AcknowledgeMessages,
    CloseConnection,
    EncodableMessage,
    MessageId,
    MinMissingAcknowledge,
    OpenConnection,
)
from s3lobby.server import LobbyServer, ServerSettings, start_server
from s3lobby.wire import Address, RawMessage, decode_block, encode_block

LOOPBACK = "127.0.0.1"
ANY_FREE_PORT = 0
RECEIVE_TIMEOUT_SECONDS = 2.0
SERVER_START_TIMEOUT_SECONDS = 5.0
MAX_DATAGRAM_BUFFER = 0x10000

# Whole message sizes, header included, that the real client asserts when it reads a message.
# A server message of another size makes the game drop the connection.
SIZE_THE_CLIENT_ASSERTS = {
    MessageType.MESSAGE_ID: 8,
    MessageType.OPEN_CONNECTION: 8,
    MessageType.MIN_MISSING_ACKNOWLEDGE: 8,
    MessageType.RESULT: 12,
    MessageType.OWN_PLAYER: 8,
    MessageType.REMOVE_PLAYER_FROM_GROUP: 12,
    MessageType.REMOVE_GROUP_FROM_GROUP: 12,
}


class BackgroundServer:
    """Runs a LobbyServer on its own event loop in a thread, for tests that talk to it over UDP."""

    def __init__(self, server_name: str = "Test Lobby") -> None:
        self._settings = ServerSettings(bind_host=LOOPBACK, port=ANY_FREE_PORT, server_name=server_name)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._server: LobbyServer | None = None

    def __enter__(self) -> BackgroundServer:
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(start_server(self._settings), self._loop)
        self._server = future.result(timeout=SERVER_START_TIMEOUT_SECONDS)
        return self

    def __exit__(self, *exception_details: object) -> None:
        future = asyncio.run_coroutine_threadsafe(self._server.close(), self._loop)
        future.result(timeout=SERVER_START_TIMEOUT_SECONDS)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    @property
    def address(self) -> Address:
        return self._server.bound_address


class FakeGameClient:
    """Speaks the connection layer like the game does and collects the lobby messages it receives."""

    def __init__(self, server_address: Address, player_name: str, nonce: int) -> None:
        self.player_name = player_name
        self._server_address = server_address
        self._nonce = nonce
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((LOOPBACK, ANY_FREE_PORT))
        self._socket.settimeout(RECEIVE_TIMEOUT_SECONDS)
        self._unread_messages: list[RawMessage] = []
        self._start_new_connection()

    @property
    def address(self) -> Address:
        return self._socket.getsockname()

    def close_socket(self) -> None:
        self._socket.close()

    def send(self, *lobby_messages: EncodableMessage) -> None:
        """Send lobby messages in the next numbered block, opening the connection with block 1."""
        messages = [MessageId(self._next_block_id).encode()]
        if self._next_block_id == 1:
            messages.append(OpenConnection(self._nonce).encode())
        messages += [message.encode() for message in lobby_messages]
        self._next_block_id += 1
        self._socket.sendto(encode_block(messages), self._server_address)

    def close_connection(self) -> None:
        """End the connection like the game between index and lobby phase, then start afresh."""
        self.send(CloseConnection())
        self.receive_next(MessageType.CLOSE_CONNECTION)
        self._nonce += 1
        self._start_new_connection()

    def receive_next(self, message_type: MessageType) -> RawMessage:
        """The next message of this type from the server. Messages of other types that arrive
        first stay unread for later calls, so a test can read a block's messages in any order."""
        return self.receive_matching(lambda message: message.message_type == message_type)

    def unread_messages_of_type(self, message_type: MessageType) -> list[RawMessage]:
        """Messages of this type that already arrived but were not read yet."""
        return [message for message in self._unread_messages if message.message_type == message_type]

    def receive_matching(self, is_wanted: Callable[[RawMessage], bool]) -> RawMessage:
        while True:
            for index, message in enumerate(self._unread_messages):
                if is_wanted(message):
                    return self._unread_messages.pop(index)
            datagram, _ = self._socket.recvfrom(MAX_DATAGRAM_BUFFER)
            self._unread_messages += self._read_server_block(datagram)

    def _start_new_connection(self) -> None:
        self._next_block_id = 1
        self._is_open = False
        self._seen_server_block_ids: set[int] = set()

    def _read_server_block(self, datagram: bytes) -> list[RawMessage]:
        messages = decode_block(datagram)
        for message in messages:
            check_size_the_client_asserts(message)
        if not messages or messages[0].message_type != MessageType.MESSAGE_ID:
            raise AssertionError("every server block starts with MessageId")
        block_id = MessageId.decode(messages[0]).block_id
        content = [m for m in messages[1:] if m.message_type not in ACKNOWLEDGEMENT_MESSAGE_TYPES]
        if not content:
            return []
        if not self._is_open:
            # The client only opens on the server's OpenConnection, in block 1 (0x605ec8, 0x607f13).
            is_proper_opening = block_id == 1 and content[0].message_type == MessageType.OPEN_CONNECTION
            if not is_proper_opening:
                raise AssertionError("the server's first block must be block 1 with OpenConnection")
            self._is_open = True
        self._acknowledge(block_id)
        if block_id in self._seen_server_block_ids:
            return []
        self._seen_server_block_ids.add(block_id)
        return [m for m in content if m.message_type != MessageType.OPEN_CONNECTION]

    def _acknowledge(self, server_block_id: int) -> None:
        messages = [
            MessageId(self._next_block_id - 1).encode(),
            AcknowledgeMessages((server_block_id,)).encode(),
            MinMissingAcknowledge(self._next_block_id).encode(),
        ]
        self._socket.sendto(encode_block(messages), self._server_address)


def check_size_the_client_asserts(message: RawMessage) -> None:
    expected_size = SIZE_THE_CLIENT_ASSERTS.get(message.message_type)
    actual_size = MESSAGE_HEADER_SIZE + len(message.body)
    if expected_size is not None and actual_size != expected_size:
        raise AssertionError(f"{message.type_name} is {actual_size} bytes, the game asserts {expected_size}")
