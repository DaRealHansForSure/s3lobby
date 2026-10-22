"""Plays the game's side of a lobby session against the server, without the game.

Two fake clients follow the rules read from S3.EXE (docs/s3-lobby-protocol.md):
patch check, lobby server list, logon, joining a room, a second player arriving,
a game being opened. The fake client also checks the size rules whose violation
makes the real client drop the connection. This proves the server keeps the
protocol as documented; only a run with the real game proves the document right.

Usage:
    python tools/s3lobby/selftest.py
"""

import asyncio
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import LobbyServer  # noqa: E402
from wire import (  # noqa: E402
    ACKNOWLEDGE_MESSAGES,
    ADD_GROUP_TO_GROUP,
    ADD_GROUP_TO_GROUP_2,
    ADD_PLAYER_TO_GROUP,
    CHECK_FILE_VERSION,
    CLOSE_CONNECTION,
    GET_LOBBY_SERVER_LIST,
    LOBBY_SERVER_LIST,
    LOGON,
    MESSAGE_ID,
    MIN_MISSING_ACKNOWLEDGE,
    OPEN_CONNECTION,
    OWN_PLAYER,
    REMOVE_PLAYER_FROM_GROUP,
    RESULT,
    BodyBuilder,
    decode_block,
    decode_sockaddr,
    encode_block,
)

TEST_PORT = 33440
RECEIVE_TIMEOUT_SECONDS = 2.0
# Sizes the real client asserts (whole message incl. header), see FINDINGS / the protocol doc.
EXACT_SIZES = {RESULT: 12, OWN_PLAYER: 8, MIN_MISSING_ACKNOWLEDGE: 8, MESSAGE_ID: 8, OPEN_CONNECTION: 8}


class FakeClient:
    """A minimal client: numbers its blocks, acks the server's, and collects lobby messages."""

    def __init__(self, name, nonce):
        self.name = name
        self.nonce = nonce
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(RECEIVE_TIMEOUT_SECONDS)
        self.server = ("127.0.0.1", TEST_PORT)
        self.backlog = []
        self.reset_connection()

    def reset_connection(self):
        self.next_block_id = 1
        self.is_open = False
        self.seen_server_blocks = set()

    def send(self, *lobby_messages):
        messages = [BodyBuilder().u32(self.next_block_id).build(MESSAGE_ID)]
        if self.next_block_id == 1:
            messages.append(BodyBuilder().u32(self.nonce).build(OPEN_CONNECTION))
        messages += lobby_messages
        self.next_block_id += 1
        self.socket.sendto(encode_block(messages), self.server)

    def close(self):
        self.send(BodyBuilder().build(CLOSE_CONNECTION))
        self.receive_until(lambda message: message.message_type == CLOSE_CONNECTION, connection_layer=True)
        self.reset_connection()
        self.nonce += 1

    def receive_until(self, is_wanted, connection_layer=False):
        """Read until a wanted message arrives; the rest of its block waits in the backlog."""
        while True:
            while self.backlog:
                message = self.backlog.pop(0)
                if is_wanted(message):
                    return message
            datagram, _ = self.socket.recvfrom(0x10000)
            self.backlog += self.read_block(datagram, connection_layer)

    def read_block(self, datagram, connection_layer):
        messages = decode_block(datagram)
        for message in messages:
            expected = EXACT_SIZES.get(message.message_type)
            if expected is not None:
                assert len(message.body) + 4 == expected, f"{message} breaks the size the game asserts"
        assert messages[0].message_type == MESSAGE_ID, "every server block starts with MessageId"
        block_id = messages[0].reader().u32()
        content = [m for m in messages[1:] if m.message_type not in (ACKNOWLEDGE_MESSAGES, MIN_MISSING_ACKNOWLEDGE)]
        if not content:
            return []
        if not self.is_open:
            assert block_id == 1 and content[0].message_type == OPEN_CONNECTION, "server must open with block 1"
            self.is_open = True
        self.acknowledge(block_id)
        if block_id in self.seen_server_blocks:
            return []
        self.seen_server_blocks.add(block_id)
        if connection_layer:
            return content
        return [m for m in content if m.message_type != OPEN_CONNECTION]

    def acknowledge(self, block_id):
        acknowledge = BodyBuilder().u32(block_id).build(ACKNOWLEDGE_MESSAGES)
        min_missing = BodyBuilder().u32(self.next_block_id).build(MIN_MISSING_ACKNOWLEDGE)
        id_message = BodyBuilder().u32(self.next_block_id - 1).build(MESSAGE_ID)
        self.socket.sendto(encode_block([id_message, acknowledge, min_missing]), self.server)


def of_type(message_type):
    return lambda message: message.message_type == message_type


def log_on(client):
    client.send(BodyBuilder().u32(0xA0).u32(4).u32(0).build(CHECK_FILE_VERSION))
    patch = client.receive_until(of_type(RESULT)).reader()
    assert patch.u32() == 9, "patch check answers Result 9 (no patch)"

    client.send(BodyBuilder().build(GET_LOBBY_SERVER_LIST))
    servers = client.receive_until(of_type(LOBBY_SERVER_LIST)).reader()
    address, name = servers.string(), servers.string()
    assert address.endswith(f":{TEST_PORT}") and servers.remaining() == 0, address
    client.close()

    logon = BodyBuilder().u32(14).string(client.name).string("secret").u32(0).string("0000-0000-0000-0000")
    client.send(logon.build(LOGON))
    result = client.receive_until(of_type(RESULT)).reader()
    assert result.u32() == 0x66, "logon accepted"
    own_player_id = client.receive_until(of_type(OWN_PLAYER)).reader().u32()
    print(f"{client.name}: server {name!r} at {address}, player id {own_player_id}")
    return own_player_id


def join_room(client, room_id, player_id):
    join = BodyBuilder().u32(room_id).u32(player_id).raw(bytes(16)).string(client.name).string("")
    client.send(join.build(ADD_PLAYER_TO_GROUP))
    room = client.receive_until(of_type(ADD_GROUP_TO_GROUP)).reader()
    assert room.u32() == 0 and room.u32() == room_id, "joined group is announced with parent 0"
    member = client.receive_until(of_type(ADD_PLAYER_TO_GROUP)).reader()
    assert member.u32() == room_id
    return member


def run_session():
    first = FakeClient("Alice", nonce=17)
    second = FakeClient("Bob", nonce=42)
    first_id = log_on(first)
    join_room(first, 1, first_id)

    second_id = log_on(second)
    join_room(second, 1, second_id)
    arrival = first.receive_until(of_type(ADD_PLAYER_TO_GROUP)).reader()
    assert arrival.u32() == 1 and arrival.u32() == second_id
    host, port = decode_sockaddr(arrival.raw(16))
    assert (host, port) == second.socket.getsockname(), "members get the address the server saw"
    print(f"Alice sees Bob arrive from {host}:{port}")

    game = BodyBuilder().u32(1).u32(1000).u32(0x400).u32(0).string("Bob's game").string("").string("")
    game.raw(bytes(16)).raw(bytes(16)).u32(0).u32(0).u32(0).u32(0).u32(0)
    second.send(game.build(ADD_GROUP_TO_GROUP_2))
    confirmation = second.receive_until(of_type(ADD_GROUP_TO_GROUP_2)).reader()
    assert confirmation.u32() == 0 and confirmation.u32() == 1000, "the host is told it is in its game"
    host_member = second.receive_until(of_type(ADD_PLAYER_TO_GROUP)).reader()
    assert host_member.u32() == 1000 and host_member.u32() == second_id, "the host is the game's first member"
    announced = first.receive_until(of_type(ADD_GROUP_TO_GROUP_2)).reader()
    assert announced.u32() == 1 and announced.u32() == 1000
    announced.u32()
    announced.u32()
    title = announced.string()
    announced.string()
    announced.string()
    game_host = decode_sockaddr(announced.raw(16))
    assert game_host[0] == "127.0.0.1", "0.0.0.0 host address is replaced by the one the server saw"
    print(f"Alice sees {title!r} hosted at {game_host[0]}")

    first.send(BodyBuilder().u32(1).u32(first_id).build(REMOVE_PLAYER_FROM_GROUP))
    departure = first.receive_until(of_type(REMOVE_PLAYER_FROM_GROUP)).reader()
    assert (departure.u32(), departure.u32()) == (1, first_id), "the leaver gets the confirmation"
    join_room(first, 1, first_id)
    print("Alice leaves room 1 and joins it again")


def start_server():
    loop = asyncio.new_event_loop()
    server = LobbyServer(TEST_PORT, "Selftest")

    async def serve():
        await loop.create_datagram_endpoint(lambda: server, local_addr=("127.0.0.1", TEST_PORT))
        await server.tick_forever()

    thread = threading.Thread(target=loop.run_until_complete, args=(serve(),), daemon=True)
    thread.start()


def main():
    start_server()
    run_session()
    print("selftest passed")


if __name__ == "__main__":
    main()
