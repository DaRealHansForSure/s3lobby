"""LAN lobby server for the original Settlers 3 (replaces siedler3svr.bluebyte.de:3344).

Point the game at it with a registry value whose NAME is "<this PC's IP>:<port>"
under HKLM\\SOFTWARE\\WOW6432Node\\BlueByte\\Siedler3\\1.0\\Index Server (type and
data do not matter). The protocol is described in docs/s3-lobby-protocol.md.

Usage:
    python tools/s3lobby/server.py                 (UDP port 3344 on all interfaces)
    python tools/s3lobby/server.py --port 3344 --name "Home LAN" --verbose --log-file lobby.log
"""

import argparse
import asyncio
import logging
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lobby import Lobby  # noqa: E402
from reliable import ReliableConnection  # noqa: E402
from wire import MESSAGE_NAMES, OPEN_CONNECTION, ProtocolError, decode_block  # noqa: E402

DEFAULT_PORT = 3344
DEFAULT_SERVER_NAME = "LAN Lobby"
# The game sends to one peer at most every 250 ms; ticking at the same pace keeps retries on time.
TICK_SECONDS = 0.25

log = logging.getLogger("s3lobby")


def now_ms():
    return int(time.monotonic() * 1000)


class LobbyServer(asyncio.DatagramProtocol):
    """Owns one reliable connection per client address and feeds their messages to the lobby."""

    def __init__(self, port, server_name):
        self.port = port
        self.connections = {}
        self.transport = None
        self.lobby = Lobby(server_name, self.send, self.server_address_for)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        try:
            messages = decode_block(data)
        except ProtocolError as error:
            log.warning("%s: unreadable datagram: %s (%s)", address, error, data.hex())
            return
        log.debug("%s -> %s", address, ", ".join(describe(m) for m in messages))

        connection = self.connections.get(address)
        opening_nonce = find_open_nonce(messages)
        is_new_connection = connection is None or (opening_nonce is not None and opening_nonce != connection.peer_nonce)
        if is_new_connection:
            if opening_nonce is None:
                # Leftovers of a connection we already dropped; the game copes with silence.
                log.debug("%s: ignored datagram without an open connection", address)
                return
            connection = ReliableConnection(address, now_ms())
            connection.peer_nonce = opening_nonce
            self.connections[address] = connection
            log.info("%s: connection opened", address)

        received = connection.receive(messages, now_ms())
        for message in received.lobby_messages:
            self.lobby.handle(address, message)
        if received.is_close and connection.closing_since_ms is None:
            log.info("%s: connection closed by the client", address)
            connection.send_close(now_ms())
        self.flush(connection)

    def send(self, address, message):
        connection = self.connections.get(address)
        if connection is None:
            log.warning("%s: no connection for %s", address, describe(message))
            return
        connection.send(message)

    def server_address_for(self, client_address):
        """The "host:port" under which a client reaches this server: the local IP routed towards it."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(client_address)
            local_host = probe.getsockname()[0]
        finally:
            probe.close()
        return f"{local_host}:{self.port}"

    def flush(self, connection):
        for datagram in connection.collect_datagrams(now_ms()):
            log.debug("%s <- %s", connection.address, ", ".join(describe(m) for m in decode_block(datagram)))
            self.transport.sendto(datagram, connection.address)

    async def tick_forever(self):
        while True:
            await asyncio.sleep(TICK_SECONDS)
            current_ms = now_ms()
            for address, connection in list(self.connections.items()):
                if connection.has_expired(current_ms):
                    log.info("%s: connection dropped", address)
                    del self.connections[address]
                    self.lobby.disconnect(address)
                    continue
                self.flush(connection)


def find_open_nonce(messages):
    for message in messages:
        if message.message_type == OPEN_CONNECTION:
            return message.reader().u32()
    return None


def describe(message):
    name = MESSAGE_NAMES.get(message.message_type, f"{message.message_type:#04x}")
    if not message.body:
        return name
    return f"{name}[{message.body.hex()}]"


async def run(port, server_name):
    loop = asyncio.get_running_loop()
    transport, server = await loop.create_datagram_endpoint(
        lambda: LobbyServer(port, server_name), local_addr=("0.0.0.0", port)
    )
    log.info("Settlers 3 lobby server %r on UDP port %d", server_name, port)
    try:
        await server.tick_forever()
    finally:
        transport.close()


def main():
    parser = argparse.ArgumentParser(description="LAN lobby server for the original Settlers 3")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="UDP port (default 3344)")
    parser.add_argument("--name", default=DEFAULT_SERVER_NAME, help="server name shown in the game")
    parser.add_argument("--verbose", action="store_true", help="log every datagram with its bytes")
    parser.add_argument("--log-file", help="also write the log to this file")
    arguments = parser.parse_args()
    handlers = [logging.StreamHandler()]
    if arguments.log_file:
        handlers.append(logging.FileHandler(arguments.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    try:
        asyncio.run(run(arguments.port, arguments.name))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
