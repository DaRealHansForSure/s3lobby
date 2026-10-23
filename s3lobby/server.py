"""The UDP server: one socket, one reliable connection per client, and the lobby behind them.

Datagrams come in through asyncio. Each is split into messages, the reliable layer of the
client's connection takes what is its own, and the lobby handles the rest. Answers queue up on the
connection and leave right away, together with the acknowledgements. A housekeeping task sends
retries and keepalives and drops connections that went silent.

Anyone can send datagrams to the server, with any sender address. Before a datagram is even parsed
its sender must be within its rate limit; a datagram must then fit the size and message count
limits, and a new connection the connection limits. Whatever a datagram does wrong ends with a
throttled warning and the datagram dropped, never with the server stopping.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass

from s3lobby.connection import ReliableConnection
from s3lobby.constants import (
    ANNOUNCEMENT_POLL_INTERVAL_SECONDS,
    DATAGRAM_BURST_PER_ADDRESS,
    DATAGRAMS_PER_SECOND_PER_ADDRESS,
    DEFAULT_SERVER_PORT,
    HOUSEKEEPING_INTERVAL_SECONDS,
    MAX_CONNECTIONS,
    MAX_CONNECTIONS_PER_IP,
    MAX_MESSAGES_PER_BLOCK,
    WARNING_INTERVAL_SECONDS,
    MessageType,
)
from s3lobby.database import LobbyDatabase
from s3lobby.guards import RateLimiter, ThrottledLogger
from s3lobby.lobby import Lobby
from s3lobby.messages import EncodableMessage, OpenConnection, describe_for_log
from s3lobby.wire import Address, ProtocolError, RawMessage, decode_block

log = logging.getLogger(__name__)

DEFAULT_SERVER_NAME = "LAN Lobby"
DEFAULT_DATABASE_PATH = "s3lobby.sqlite3"
ALL_INTERFACES = "0.0.0.0"


def monotonic_ms() -> int:
    """Milliseconds from a clock that never jumps, for timeouts and retries."""
    return int(time.monotonic() * 1000)


@dataclass(frozen=True)
class ServerSettings:
    """How the server is reached and how it presents itself.

    public_host is the host name or IP the lobby server list tells clients to log on to. Leave it
    empty on a LAN: the server then names the local IP that faces each client. Set it when clients
    come over the internet through a router, to the router's public address.
    """

    bind_host: str = ALL_INTERFACES
    port: int = DEFAULT_SERVER_PORT
    server_name: str = DEFAULT_SERVER_NAME
    public_host: str = ""
    database_path: str = DEFAULT_DATABASE_PATH


class LobbyServer(asyncio.DatagramProtocol):
    """Owns the UDP socket and one reliable connection per client address."""

    def __init__(self, settings: ServerSettings, database: LobbyDatabase) -> None:
        self._settings = settings
        self._database = database
        self._connections: dict[Address, ReliableConnection] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._housekeeping_task: asyncio.Task[None] | None = None
        self._lobby = Lobby(settings.server_name, outbox=self, database=database)
        self._datagram_rate = RateLimiter(DATAGRAMS_PER_SECOND_PER_ADDRESS, DATAGRAM_BURST_PER_ADDRESS)
        self._warnings = ThrottledLogger(log, WARNING_INTERVAL_SECONDS)

    @property
    def bound_address(self) -> Address:
        """The address the socket is bound to; with port 0 in the settings this has the real port."""
        if self._transport is None:
            raise RuntimeError("the server has not started yet")
        return self._transport.get_extra_info("sockname")

    # asyncio.DatagramProtocol

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, client_address: Address) -> None:
        now_seconds = time.monotonic()
        if not self._datagram_rate.allows(client_address, now_seconds):
            self._warnings.warning(client_address, now_seconds, "%s: over its rate limit, datagrams dropped", client_address)
            return
        try:
            self._receive_datagram(data, client_address)
        except ProtocolError as error:
            self._warnings.warning(client_address, now_seconds, "%s: datagram refused: %s", client_address, error)
        except Exception:
            # A bug, not a broken datagram: keep serving everyone else, and leave the details.
            self._warnings.error_with_traceback(
                client_address, now_seconds, "%s: internal error while handling a datagram", client_address
            )

    def error_received(self, error: OSError) -> None:
        # On some systems an ICMP "port unreachable" for an earlier datagram surfaces here, for
        # example after a client quit. It says nothing about the socket itself, so keep serving.
        log.debug("socket error: %s", error)

    # LobbyOutbox, used by the lobby to reach clients.

    def send(self, client_address: Address, message: EncodableMessage) -> None:
        connection = self._connections.get(client_address)
        if connection is None:
            log.warning("%s: no connection to send %s on", client_address, type(message).__name__)
            return
        connection.queue_lobby_message(message)

    def server_address_text_for(self, client_address: Address) -> str:
        """The "host:port" a client should log on to: the configured public host, or else the local
        IP the operating system would use to reach that client."""
        bound_host, port = self.bound_address
        if self._settings.public_host:
            return f"{self._settings.public_host}:{port}"
        try:
            return f"{local_ip_facing(client_address)}:{port}"
        except OSError as error:
            log.warning("%s: no route to the client (%s), naming %s instead", client_address, error, bound_host)
            return f"{bound_host}:{port}"

    # Lifecycle.

    def start_housekeeping(self) -> None:
        self._housekeeping_task = asyncio.get_running_loop().create_task(self._run_housekeeping())

    async def close(self) -> None:
        """Stop housekeeping and close the socket; open connections simply end."""
        if self._housekeeping_task is not None:
            self._housekeeping_task.cancel()
            try:
                await self._housekeeping_task
            except asyncio.CancelledError:
                pass
        if self._transport is not None:
            self._transport.close()
        self._database.close()

    async def _run_housekeeping(self) -> None:
        last_announcement_poll_seconds = time.monotonic()
        while True:
            await asyncio.sleep(HOUSEKEEPING_INTERVAL_SECONDS)
            if time.monotonic() - last_announcement_poll_seconds >= ANNOUNCEMENT_POLL_INTERVAL_SECONDS:
                last_announcement_poll_seconds = time.monotonic()
                self._lobby.send_pending_announcements()
            now_ms = monotonic_ms()
            for client_address, connection in list(self._connections.items()):
                if connection.is_finished(now_ms):
                    self._end_connection(client_address, "it went quiet or was closed")
                    continue
                self._send_due_datagrams(connection)
            now_seconds = time.monotonic()
            self._datagram_rate.forget_quiet_keys(now_seconds)
            self._warnings.forget_quiet_keys(now_seconds)
            self._lobby.forget_quiet_keys(now_seconds)

    def _receive_datagram(self, data: bytes, client_address: Address) -> None:
        """Parse and act on one datagram; raises ProtocolError when it breaks a rule."""
        messages = decode_block(data)
        if len(messages) > MAX_MESSAGES_PER_BLOCK:
            raise ProtocolError(f"{len(messages)} messages in one datagram")
        if log.isEnabledFor(logging.DEBUG):
            log.debug("%s -> %s", client_address, ", ".join(describe_for_log(m) for m in messages))

        connection = self._find_or_open_connection(client_address, messages)
        if connection is None:
            return
        received = connection.receive_block(messages, monotonic_ms())
        for message in received.lobby_messages:
            self._lobby.handle_message(client_address, message)
        if received.is_close_requested and not connection.is_closing:
            log.info("%s: connection closed by the client", client_address)
            connection.begin_closing(monotonic_ms())
        if connection.is_overloaded:
            self._end_connection(client_address, "it stopped acknowledging")
            return
        self._send_due_datagrams(connection)

    def _find_or_open_connection(self, client_address: Address, messages: list[RawMessage]) -> ReliableConnection | None:
        """The connection a datagram belongs to. An OpenConnection with a new nonce starts a new one
        (the client restarted or reconnects, 0x605eec); a datagram without one for an unknown
        address is a leftover of a connection that already ended and is ignored, as the game does."""
        connection = self._connections.get(client_address)
        open_connection = find_open_connection(messages)
        is_new_connection = open_connection is not None and (
            connection is None or open_connection.nonce != connection.client_nonce
        )
        if not is_new_connection:
            if connection is None:
                log.debug("%s: datagram for no open connection ignored", client_address)
            return connection

        if connection is not None:
            # The client started over; whatever it had in the lobby is gone on its side too.
            self._end_connection(client_address, "the client opened a new one")
        refusal = self._refusal_for_new_connection(client_address)
        if refusal:
            raise ProtocolError(f"new connection refused: {refusal}")
        connection = ReliableConnection(client_address, open_connection.nonce, monotonic_ms())
        self._connections[client_address] = connection
        log.info("%s: connection opened", client_address)
        return connection

    def _refusal_for_new_connection(self, client_address: Address) -> str:
        """Why a new connection from this address cannot be opened now, or "" when it can."""
        if len(self._connections) >= MAX_CONNECTIONS:
            return f"the server already has {MAX_CONNECTIONS} connections"
        client_ip = client_address[0]
        connections_from_ip = sum(1 for address in self._connections if address[0] == client_ip)
        if connections_from_ip >= MAX_CONNECTIONS_PER_IP:
            return f"{client_ip} already has {MAX_CONNECTIONS_PER_IP} connections"
        return ""

    def _end_connection(self, client_address: Address, reason: str) -> None:
        log.info("%s: connection ended, %s", client_address, reason)
        del self._connections[client_address]
        self._lobby.forget_client(client_address)

    def _send_due_datagrams(self, connection: ReliableConnection) -> None:
        for datagram in connection.take_datagrams_to_send(monotonic_ms()):
            if log.isEnabledFor(logging.DEBUG):
                described = ", ".join(describe_for_log(m) for m in decode_block(datagram))
                log.debug("%s <- %s", connection.client_address, described)
            self._transport.sendto(datagram, connection.client_address)


def find_open_connection(messages: list[RawMessage]) -> OpenConnection | None:
    for message in messages:
        if message.message_type == MessageType.OPEN_CONNECTION:
            return OpenConnection.decode(message)
    return None


def local_ip_facing(client_address: Address) -> str:
    """The local IP the operating system would send from to reach a client.

    Connecting a UDP socket sends nothing; it only makes the system pick a route, and with it the
    local address on that route. This finds the right address on a machine with several networks.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(client_address)
        return probe.getsockname()[0]


async def start_server(settings: ServerSettings) -> LobbyServer:
    """Open the database, bind the socket and start housekeeping; the server then runs until
    close() is awaited. The database is opened here, on the event loop's thread, because SQLite
    connections must stay on the thread that opened them."""
    database = LobbyDatabase(settings.database_path)
    # Nobody is connected at start: whatever a previous run left open has ended.
    database.close_open_sessions_and_games()
    loop = asyncio.get_running_loop()
    try:
        _, server = await loop.create_datagram_endpoint(
            lambda: LobbyServer(settings, database),
            local_addr=(settings.bind_host, settings.port),
        )
    except OSError:
        database.close()
        raise
    server.start_housekeeping()
    return server


async def serve_forever(settings: ServerSettings) -> None:
    server = await start_server(settings)
    host, port = server.bound_address
    log.info("Settlers III lobby server %r on UDP %s:%d", settings.server_name, host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()
