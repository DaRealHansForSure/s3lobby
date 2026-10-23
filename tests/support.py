"""Test helpers: a server running in the background, and a fake game client.

The fake client follows the rules read from the game's code rather than this server's code: it
numbers its blocks, opens with OpenConnection in block 1, acknowledges the server's blocks, and
checks every size the real client asserts. A test passing against it shows the server keeps the
protocol as documented; only a run with the real game shows the documentation is right.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path

from s3lobby.constants import (
    ACKNOWLEDGEMENT_MESSAGE_TYPES,
    JOINED_GROUP_PARENT_ID,
    LOGON_PROTOCOL_VERSION,
    MESSAGE_HEADER_SIZE,
    SERVER_ASSIGNS_GROUP_ID,
    GroupFlag,
    MessageType,
)
from s3lobby.database import LobbyDatabase
from s3lobby.messages import (
    AcknowledgeMessages,
    AddPlayerToGroup,
    CheckFileVersion,
    CloseConnection,
    EncodableMessage,
    GameBlock,
    GetLobbyServerList,
    GroupAnnouncement,
    LobbyServerList,
    Logon,
    MessageId,
    MinMissingAcknowledge,
    OpenConnection,
    OwnPlayer,
    Result,
)
from s3lobby.server import LobbyServer, ServerSettings, start_server
from s3lobby.wire import Address, RawMessage, decode_block, encode_block

from tests.databases import database_location_for_test

LOOPBACK = "127.0.0.1"
ANY_FREE_PORT = 0
RECEIVE_TIMEOUT_SECONDS = 2.0
# Long enough for a server answer on the loopback interface, short enough to keep tests quick.
SILENCE_CHECK_SECONDS = 0.5
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
    """Runs a LobbyServer on its own event loop in a thread, for tests that talk to it over UDP.

    database_location names a database the test may also open itself, for example to create
    accounts; the server opens its own connection to it.
    """

    def __init__(self, database_location: str, server_name: str = "Test Lobby") -> None:
        self._settings = ServerSettings(
            bind_host=LOOPBACK,
            port=ANY_FREE_PORT,
            server_name=server_name,
            database_location=database_location,
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._server: LobbyServer | None = None

    def __enter__(self) -> BackgroundServer:
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(start_server(self._settings), self._loop)
        self._server = future.result(timeout=SERVER_START_TIMEOUT_SECONDS)
        return self

    def __exit__(self, *exception_details: object) -> None:
        self.stop()

    def stop(self) -> None:
        """Close the server and end its thread; a second call does nothing."""
        if self._loop.is_closed():
            return
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

    def assert_nothing_arrives(self, message_type: MessageType) -> None:
        """Wait a short while and fail if a message of this type arrives in that time."""
        self._socket.settimeout(SILENCE_CHECK_SECONDS)
        try:
            message = self.receive_next(message_type)
        except socket.timeout:
            return
        finally:
            self._socket.settimeout(RECEIVE_TIMEOUT_SECONDS)
        raise AssertionError(f"unexpected {message}")

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


# Whole sessions: the steps a real client takes, and a test case with a server and accounts.

GAME_VERSION = 160
ACCESS_CODE = "TESTCODE2345"
SERIAL_NUMBER = "0497-2584-2194-4356-2336"
EDITION_MISSION_AND_GOLD = 4
# Index of German in the game's text archive: the GOG copy used for the runs is German.
GERMAN_LANGUAGE_INDEX = 0
# The version check the GOG game sends first (seen in a run).
GOG_VERSION_CHECK = CheckFileVersion(
    game_version=GAME_VERSION,
    edition=EDITION_MISSION_AND_GOLD,
    language_index=GERMAN_LANGUAGE_INDEX,
)
LOBBY_ROOM_ID = 3
GAME_ZONE_ID = 30
UNKNOWN_HOST_ADDRESS = ("0.0.0.0", 3343)
# AddGroupToGroup2's game block after the host address: a 16-byte session id and 5 u32 values.
GAME_BLOCK_REMAINDER_SIZE = 16 + 5 * 4


def go_to_lobby_server(client: FakeGameClient) -> None:
    """The patch check and the server list, then a new connection for the logon, like the game."""
    client.send(GOG_VERSION_CHECK)
    Result.decode(client.receive_next(MessageType.RESULT))
    client.send(GetLobbyServerList())
    LobbyServerList.decode(client.receive_next(MessageType.LOBBY_SERVER_LIST))
    client.close_connection()


def send_logon(client: FakeGameClient, access_code: str = ACCESS_CODE, flags: int = 0, name: str | None = None) -> Result:
    logon = Logon(
        protocol_version=LOGON_PROTOCOL_VERSION,
        player_name=client.player_name if name is None else name,
        password=access_code,
        flags=flags,
        serial_number=SERIAL_NUMBER,
    )
    client.send(logon)
    return Result.decode(client.receive_next(MessageType.RESULT))


def log_on(client: FakeGameClient) -> int:
    """Run the whole way into the lobby like the game; returns the player id."""
    go_to_lobby_server(client)
    send_logon(client)
    return OwnPlayer.decode(client.receive_next(MessageType.OWN_PLAYER)).player_id


def join_group(client: FakeGameClient, group_id: int, player_id: int) -> None:
    """Join a group the way the game asks: its own address unknown, its name left empty. Waits for
    the group to arrive with parent 0; other announcements stay unread."""
    client.send(AddPlayerToGroup(group_id, player_id, player_address=("0.0.0.0", 0), player_name=""))

    def is_joined_announcement(message: RawMessage) -> bool:
        if message.message_type != MessageType.ADD_GROUP_TO_GROUP:
            return False
        announcement = GroupAnnouncement.decode(message)
        return (announcement.parent_id, announcement.group_id) == (JOINED_GROUP_PARENT_ID, group_id)

    client.receive_matching(is_joined_announcement)


def receive_member_entry(client: FakeGameClient, group_id: int, player_id: int | None = None) -> AddPlayerToGroup:
    """The next member entry for one group, and for one player when player_id is given; other
    entries stay unread."""

    def is_wanted_entry(message: RawMessage) -> bool:
        if message.message_type != MessageType.ADD_PLAYER_TO_GROUP:
            return False
        entry = AddPlayerToGroup.decode(message)
        is_wanted_player = player_id is None or entry.player_id == player_id
        return entry.group_id == group_id and is_wanted_player

    return AddPlayerToGroup.decode(client.receive_matching(is_wanted_entry))


def open_game(client: FakeGameClient, zone_id: int) -> GroupAnnouncement:
    game_block = GameBlock(host_address=UNKNOWN_HOST_ADDRESS, remainder=bytes(GAME_BLOCK_REMAINDER_SIZE))
    request = GroupAnnouncement(
        message_type=MessageType.ADD_GROUP_TO_GROUP_2,
        parent_id=zone_id,
        group_id=SERVER_ASSIGNS_GROUP_ID,
        flags=GroupFlag.HAS_GAME_BLOCK,
        max_players=2,
        name=f"{client.player_name}'s game",
        second_text="",
        third_text="",
        game_block=game_block,
    )
    client.send(request)
    return request


class LobbyTestCase(unittest.TestCase):
    """A running server with a fresh database holding the accounts in ACCOUNT_NAMES, all with the
    access code ACCESS_CODE, and fake game clients that are closed after each test."""

    ACCOUNT_NAMES = ("Alice", "Bob", "Carol")

    def setUp(self) -> None:
        self.database_folder = tempfile.TemporaryDirectory()
        self.database_location = database_location_for_test(str(Path(self.database_folder.name) / "lobby.sqlite3"))
        self.database = LobbyDatabase(self.database_location)
        for name in self.ACCOUNT_NAMES:
            account = self.database.create_account(name)
            self.database.add_access_code(account.account_id, ACCESS_CODE)
        self.server = BackgroundServer(self.database_location)
        self.server.__enter__()
        self.clients: list[FakeGameClient] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close_socket()
        self.server.__exit__(None, None, None)
        self.database.close()
        self.database_folder.cleanup()

    def new_client(self, player_name: str, nonce: int) -> FakeGameClient:
        client = FakeGameClient(self.server.address, player_name, nonce)
        self.clients.append(client)
        return client

    def player_in_room(self, player_name: str, nonce: int, room_id: int) -> tuple[FakeGameClient, int]:
        """A client logged on and in one room; returns the client and its player id."""
        client = self.new_client(player_name, nonce)
        player_id = log_on(client)
        join_group(client, room_id, player_id)
        return client, player_id
