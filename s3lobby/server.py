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
import ipaddress
import logging
import math
import signal
import socket
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import cast

from s3lobby.addresses import ViewerAddresses
from s3lobby.backends import describe_location
from s3lobby.connection import ReliableConnection
from s3lobby.constants import (
    ANNOUNCEMENT_POLL_INTERVAL_SECONDS,
    LIVE_STATE_HEARTBEAT_SECONDS,
    LIVE_STATE_WRITE_INTERVAL_SECONDS,
    DATAGRAM_BURST_PER_ADDRESS,
    DATAGRAMS_PER_SECOND_PER_ADDRESS,
    DEVELOPMENT_PASSWORD,
    DEFAULT_SERVER_PORT,
    HOUSEKEEPING_INTERVAL_SECONDS,
    LADDER_EVALUATION_INTERVAL_SECONDS,
    MAX_CONNECTIONS,
    MAX_CONNECTIONS_PER_IP,
    MAX_MESSAGES_PER_BLOCK,
    LOGON_DEADLINE_MS,
    PUBLIC_HOST_REFRESH_SECONDS,
    RECONNECT_AFTER_QUIET_MS,
    UPNP_RENEW_SECONDS,
    WARNING_INTERVAL_SECONDS,
    MessageType,
)
from s3lobby.database import LobbyDatabase, Setting
from s3lobby.forwarding import RouterForwarding
from s3lobby.guards import RateLimiter, ThrottledLogger
from s3lobby.lobby import Lobby
from s3lobby.messages import EncodableMessage, OpenConnection, describe_for_log
from s3lobby.reachability import Reachability, ReachabilityProber
from s3lobby.wire import Address, ProtocolError, RawMessage, decode_block

log = logging.getLogger(__name__)

DEFAULT_SERVER_NAME = "LAN Lobby"
DEFAULT_DATABASE_PATH = "s3lobby.sqlite3"
ALL_INTERFACES = "0.0.0.0"
ANY_FREE_PORT = 0


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
    database_location: str = DEFAULT_DATABASE_PATH
    use_upnp: bool = False
    # s3lobby serve --dev: every account also logs on with DEVELOPMENT_PASSWORD from the LAN.
    is_development_mode: bool = False


class LobbyServer(asyncio.DatagramProtocol):
    """Owns the UDP socket and one reliable connection per client address."""

    def __init__(self, settings: ServerSettings, database: LobbyDatabase) -> None:
        """Set up the lobby and the per-client state; the socket arrives later in connection_made."""
        self._settings = settings
        self._database = database
        self._connections: dict[Address, ReliableConnection] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._housekeeping_task: asyncio.Task[None] | None = None
        self._lobby = Lobby(
            settings.server_name, outbox=self, database=database, is_development_mode=settings.is_development_mode
        )
        # The public host's IP, looked up at start and again every PUBLIC_HOST_REFRESH_SECONDS.
        self._public_ip: str | None = None
        # The router's port mappings with --upnp; None without, or when no router answered.
        self._forwarding: RouterForwarding | None = None
        self._addresses = ViewerAddresses(local_ip_facing, lambda: self._public_ip, self._forwarded_port)
        # Router calls run as tasks; keeping them here stops them being collected while they run.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Probes whether players on the internet can be reached directly, from a second socket.
        self._prober: ReachabilityProber | None = None
        self._reachability: dict[Address, Reachability] = {}
        self._probes_waiting_for_confirmation: set[Address] = set()
        self._datagram_rate = RateLimiter(DATAGRAMS_PER_SECOND_PER_ADDRESS, DATAGRAM_BURST_PER_ADDRESS)
        self._warnings = ThrottledLogger(log, WARNING_INTERVAL_SECONDS)
        # Nothing written yet: the first check writes at once.
        self._last_live_state_json = ""
        self._last_live_state_write_seconds = -math.inf

    @property
    def bound_address(self) -> Address:
        """The address the socket is bound to; with port 0 in the settings this has the real port."""
        if self._transport is None:
            raise RuntimeError("the server has not started yet")
        # An IPv6 socket name carries flow info and scope id after host and port; keep the pair.
        host, port = self._transport.get_extra_info("sockname")[:2]
        return (host, port)

    @property
    def schema_version(self) -> int:
        """The number of the newest migration the server's database has applied, for the start line."""
        return self._database.schema_version

    def evaluate_ladder_games(self) -> None:
        """Turn ladder games whose reports are in into results (s3lobby/ladder.py)."""
        self._lobby.evaluate_ladder_games()

    def close_abandoned_chat_groups(self, seconds_from_now: float = 0.0) -> None:
        """Close the chat groups that stayed empty long enough; tests look ahead with
        seconds_from_now instead of waiting."""
        self._lobby.close_abandoned_chat_groups(time.monotonic() + seconds_from_now)

    # asyncio.DatagramProtocol

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Keep the transport asyncio hands over once the socket is bound."""
        # A datagram endpoint always hands over a DatagramTransport.
        self._transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, client_address: Address) -> None:
        """Handle one datagram from anyone, after its sender passed the rate limit.

        A broken datagram or a bug while handling it is logged (throttled per sender) and the datagram
        dropped; it never stops the server for the other clients."""
        now_seconds = time.monotonic()
        if not self._datagram_rate.allows(client_address, now_seconds):
            self._warnings.warning(
                client_address, now_seconds, "%s: over its rate limit, datagrams dropped", client_address
            )
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
        """Log a socket error at debug level and keep serving."""
        # On some systems an ICMP "port unreachable" for an earlier datagram surfaces here, for
        # example after a client quit. It says nothing about the socket itself, so keep serving.
        log.debug("socket error: %s", error)

    # LobbyOutbox, used by the lobby to reach clients.

    def send(self, client_address: Address, message: EncodableMessage) -> None:
        """Queue a message for a client, with every player and host address in it as that client
        can reach it (addresses.py)."""
        connection = self._connections.get(client_address)
        if connection is None:
            log.warning("%s: no connection to send %s on", client_address, type(message).__name__)
            return
        connection.queue_lobby_message(self._addresses.message_for_viewer(message, client_address))

    def player_logged_on(self, client_address: Address) -> None:
        """A player from the internet may be probed (setting check_reachability); a player in
        this network gets an outside port of their own when the router forwards them (--upnp)."""
        is_on_the_internet = ipaddress.ip_address(client_address[0]).is_global
        if is_on_the_internet and self._database.get_boolean_setting(Setting.CHECK_REACHABILITY):
            self.check_reachability(client_address)
        if not is_on_the_internet and self._forwarding is not None:
            self._run_in_background(self._forwarding.forward_player(client_address))

    def check_reachability(self, client_address: Address) -> None:
        """Probe the player once their connection is confirmed: before that the address is only
        claimed, and a forged one must not make the server send to somebody else."""
        if self._prober is None:
            return
        self._reachability[client_address] = Reachability.CHECKING
        self._probes_waiting_for_confirmation.add(client_address)

    def _send_confirmed_probes(self) -> None:
        """Send the waiting reachability probes whose client has confirmed its connection.

        A probe for an address whose connection ended is dropped; one whose client has not confirmed yet
        keeps waiting (see check_reachability)."""
        for client_address in list(self._probes_waiting_for_confirmation):
            connection = self._connections.get(client_address)
            if connection is None:
                self._probes_waiting_for_confirmation.discard(client_address)
                continue
            if connection.is_confirmed_by_client and self._prober is not None:
                self._probes_waiting_for_confirmation.discard(client_address)
                self._prober.probe(client_address)

    def reachability_of(self, client_address: Address) -> Reachability:
        """What the probe found out about a player's address; NOT_CHECKED when it never ran."""
        return self._reachability.get(client_address, Reachability.NOT_CHECKED)

    def record_reachability(self, client_address: Address, reachability: Reachability) -> None:
        """Store a probe's result for a client, unless its connection ended meanwhile.

        The prober calls this when an answer arrives or a probe times out; a result for an address
        without a connection would only linger."""
        if client_address in self._connections:
            self._reachability[client_address] = reachability

    def attach_prober(self, prober: ReachabilityProber) -> None:
        """Use this prober for the reachability checks; without one, check_reachability does nothing."""
        self._prober = prober

    def attach_forwarding(self, forwarding: RouterForwarding, external_ip: str) -> None:
        """Use the router's forwarding; its external address is the public one unless
        --public-host names another."""
        self._forwarding = forwarding
        if not self._settings.public_host:
            self._public_ip = external_ip

    def _forwarded_port(self, player_address: Address) -> int | None:
        """The router's outside port for a player in this network, or None without --upnp or a mapping."""
        if self._forwarding is None:
            return None
        return self._forwarding.forwarded_port(player_address)

    def _run_in_background(self, coroutine: Coroutine[object, object, None]) -> None:
        """Run a coroutine as a task the server keeps a reference to until it ends.

        asyncio holds only weak references to tasks, so an unreferenced router call could be collected
        while it still runs; close() also cancels these tasks."""
        task = asyncio.get_running_loop().create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def close_connection(self, client_address: Address, is_server_stopping: bool = False) -> None:
        """End the connection from the server's side: whatever is queued for the client goes out
        first, then, once the client acknowledged all of that, CloseConnection in a block of its
        own, retried until the client acknowledges it. A server that is stopping cannot wait for
        acknowledgements and sends it at once. Closing an open connection moves the client to state 4
        and on to its logout (PP.ReceivedCloseConnection 0x606050, ConnectionClosed 0x5cc840)."""
        connection = self._connections.get(client_address)
        if connection is None or connection.is_closing:
            return
        # The answers already queued leave first as a numbered block of their own, so they count as
        # unacknowledged and the CloseConnection waits behind them instead of overtaking them.
        self._send_due_datagrams(connection)
        connection.begin_closing(monotonic_ms(), waits_for_acknowledgements=not is_server_stopping)
        self._send_due_datagrams(connection)
        log.info("%s: connection closed by the server", client_address)

    def close_connection_later(self, client_address: Address, delay_seconds: float) -> None:
        """Close the connection after delay_seconds, but only the connection there is now: a client
        that opened a new one meanwhile (logged on again) keeps it."""
        connection = self._connections.get(client_address)
        if connection is None:
            return
        asyncio.get_running_loop().call_later(
            delay_seconds, self._close_connection_if_unchanged, client_address, connection
        )

    def _close_connection_if_unchanged(self, client_address: Address, connection: ReliableConnection) -> None:
        """The delayed half of close_connection_later."""
        if self._connections.get(client_address) is connection:
            self.close_connection(client_address)

    def server_address_text_for(self, client_address: Address) -> str:
        """The "host:port" a client should log on to: the configured public host, or else the local
        IP the operating system would use to reach that client."""
        bound_host, port = self.bound_address
        if self._settings.public_host:
            return f"{self._settings.public_host}:{port}"
        # With --upnp alone the router's external address is the public one.
        is_client_on_the_internet = ipaddress.ip_address(client_address[0]).is_global
        if is_client_on_the_internet and self._public_ip is not None:
            return f"{self._public_ip}:{port}"
        try:
            return f"{local_ip_facing(client_address)}:{port}"
        except OSError as error:
            log.warning("%s: no route to the client (%s), naming %s instead", client_address, error, bound_host)
            return f"{bound_host}:{port}"

    # Lifecycle.

    async def look_up_public_host(self) -> None:
        """Resolve the configured public host to the IP internet clients are given for players in
        this network. A failed lookup keeps the last IP, and leaves the addresses unchanged when
        there is none yet."""
        if not self._settings.public_host:
            return
        loop = asyncio.get_running_loop()
        try:
            addresses = await loop.getaddrinfo(self._settings.public_host, None, family=socket.AF_INET)
        except OSError as error:
            log.warning("cannot look up the public host %r: %s", self._settings.public_host, error)
            return
        # Each entry is (family, type, proto, canonname, sockaddr); the IP is the sockaddr's first item.
        public_ip = addresses[0][4][0]
        if public_ip != self._public_ip:
            log.info("public host %r is at %s", self._settings.public_host, public_ip)
        self._public_ip = public_ip

    async def _renew_forwarding(self) -> None:
        """Renew the router's port mappings before they expire, and take its external IP as the
        public one when it changed and no --public-host names another."""
        external_ip = await self._forwarding.renew()
        if external_ip is not None and not self._settings.public_host:
            self._public_ip = external_ip

    def start_housekeeping(self) -> None:
        """Start the task that sends retries and keepalives and does the periodic work."""
        self._housekeeping_task = asyncio.get_running_loop().create_task(self._run_housekeeping())

    async def close(self) -> None:
        """Stop housekeeping, tell every client the connection ends, and close the socket. The
        game shows "The connection to the lobby server dropped." at once instead of noticing the
        silence a minute later. The CloseConnection goes out once, without waiting for answers:
        a client that misses it still times out as before.

        Housekeeping and the router tasks stop first, so nothing new starts while the rest shuts
        down, and the database is closed even when an earlier step fails."""
        try:
            self._close_every_connection()
            await self._stop_tasks()
            if self._prober is not None:
                self._prober.close()
            if self._forwarding is not None:
                await self._forwarding.stop()
            if self._transport is not None:
                self._transport.close()
            self._database.clear_live_state()
        finally:
            self._database.close()

    async def _stop_tasks(self) -> None:
        """Cancel the housekeeping and background tasks and wait until each has ended."""
        tasks = list(self._background_tasks)
        if self._housekeeping_task is not None:
            tasks.append(self._housekeeping_task)
        for task in tasks:
            task.cancel()
        # The tasks' own exceptions, cancellation included, are theirs: the stop goes on.
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_housekeeping(self) -> None:
        """Retries, timeouts and the periodic work, every HOUSEKEEPING_INTERVAL_SECONDS.

        Every step is guarded on its own: an error in one (a locked database, a failing router) is
        logged and the others still run. Unguarded, a single exception would end this task, and
        with it every retry, timeout, kick and announcement for every player."""
        # Every timer starts now: start_server already looked up the public host, set up the router
        # and wrote the live lobby once, so the first periodic run is one interval away.
        last_announcement_poll_seconds = time.monotonic()
        last_live_state_check_seconds = time.monotonic()
        last_public_host_lookup_seconds = time.monotonic()
        last_forwarding_renewal_seconds = time.monotonic()
        last_ladder_evaluation_seconds = time.monotonic()
        while True:
            await asyncio.sleep(HOUSEKEEPING_INTERVAL_SECONDS)
            is_renewal_due = time.monotonic() - last_forwarding_renewal_seconds >= UPNP_RENEW_SECONDS
            if self._forwarding is not None and is_renewal_due:
                last_forwarding_renewal_seconds = time.monotonic()
                self._run_in_background(self._renew_forwarding())
            if time.monotonic() - last_public_host_lookup_seconds >= PUBLIC_HOST_REFRESH_SECONDS:
                last_public_host_lookup_seconds = time.monotonic()
                self._run_in_background(self.look_up_public_host())
            if time.monotonic() - last_announcement_poll_seconds >= ANNOUNCEMENT_POLL_INTERVAL_SECONDS:
                last_announcement_poll_seconds = time.monotonic()
                self._run_guarded("sending announcements", self._lobby.send_pending_announcements)
                self._run_guarded("carrying out kicks", self._lobby.carry_out_pending_kicks)
                self._run_guarded("closing abandoned chat groups", self.close_abandoned_chat_groups)
            if time.monotonic() - last_ladder_evaluation_seconds >= LADDER_EVALUATION_INTERVAL_SECONDS:
                last_ladder_evaluation_seconds = time.monotonic()
                self._run_guarded("evaluating ladder games", self.evaluate_ladder_games)
                self._run_guarded("writing the ladder's texts", self._lobby.write_ladder_texts)
            if time.monotonic() - last_live_state_check_seconds >= LIVE_STATE_WRITE_INTERVAL_SECONDS:
                last_live_state_check_seconds = time.monotonic()
                self._run_guarded(
                    "writing the live lobby", lambda: self.write_live_state_when_due(last_live_state_check_seconds)
                )
            # These run every round: the connections time their retries and keepalives in
            # milliseconds themselves and only send what is due.
            self._run_guarded("serving connections", self._serve_connections)
            self._run_guarded("sending reachability probes", self._send_confirmed_probes)
            self._run_guarded("pruning limits", self._forget_quiet_keys)

    def _run_guarded(self, step: str, action: Callable[[], None]) -> None:
        """Run one housekeeping step, logging (throttled) instead of raising when it fails, so the
        other steps and the next rounds still run."""
        try:
            action()
        except Exception:
            self._warnings.error_with_traceback(step, time.monotonic(), "housekeeping: %s failed", step)

    def _serve_connections(self) -> None:
        """End the connections that are over and send what is due on the others. A connection
        whose work fails is ended on its own, so one broken client cannot stall the rest."""
        now_ms = monotonic_ms()
        for client_address, connection in list(self._connections.items()):
            try:
                if connection.is_finished(now_ms):
                    self._end_connection(client_address, "it went quiet or was closed")
                    continue
                # A closing connection is left to finish on its own, so the CloseConnection and
                # whatever was queued before it can still be delivered.
                is_past_logon_deadline = (
                    connection.age_ms(now_ms) > LOGON_DEADLINE_MS
                    and not connection.is_closing
                    and not self._lobby.is_logged_on(client_address)
                )
                if is_past_logon_deadline:
                    self._end_connection(client_address, "it never logged on")
                    continue
                self._send_due_datagrams(connection)
            except Exception:
                self._warnings.error_with_traceback(
                    client_address, time.monotonic(), "%s: internal error while serving the connection", client_address
                )
                if client_address in self._connections:
                    self._end_connection(client_address, "serving it failed")

    def _forget_quiet_keys(self) -> None:
        """Drop the entries of senders that have been quiet a while from the datagram rate limit,
        the warning throttle and the lobby's own limits, so forged sender addresses cannot grow
        them without end."""
        now_seconds = time.monotonic()
        self._datagram_rate.forget_quiet_keys(now_seconds)
        self._warnings.forget_quiet_keys(now_seconds)
        self._lobby.forget_quiet_keys(now_seconds)

    def write_live_state_when_due(self, now_seconds: float) -> None:
        """Write the lobby for the web interface when it changed, or as a sign of life when the
        last write is LIVE_STATE_HEARTBEAT_SECONDS old. Comparing the JSON text keeps an idle
        lobby from rewriting the database every second."""
        state = self._lobby.live_state()
        state_json = state.to_json()
        is_unchanged = state_json == self._last_live_state_json
        is_heartbeat_due = now_seconds - self._last_live_state_write_seconds >= LIVE_STATE_HEARTBEAT_SECONDS
        if is_unchanged and not is_heartbeat_due:
            return
        self._database.write_live_state(state)
        self._last_live_state_json = state_json
        self._last_live_state_write_seconds = now_seconds

    def _receive_datagram(self, data: bytes, client_address: Address) -> None:
        """Parse and act on one datagram; raises ProtocolError when it breaks a rule."""
        messages = decode_block(data, MAX_MESSAGES_PER_BLOCK)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("%s -> %s", client_address, ", ".join(describe_for_log(m) for m in messages))

        connection = self._find_or_open_connection(client_address, messages)
        if connection is None:
            return
        received = connection.receive_block(messages, monotonic_ms())
        # A connection the server closed has no player any more; what the client still sends
        # before it sees the CloseConnection (a logon, a join) must not bring one back.
        if not connection.is_closing:
            for message in received.lobby_messages:
                self._lobby.handle_message(client_address, message)
        if received.is_close_requested and not connection.is_closing:
            log.info("%s: connection closed by the client", client_address)
            connection.begin_closing(monotonic_ms())
        # Too many blocks wait for an acknowledgement: the client is gone or ignores the server, and
        # each further answer would only pile up in memory.
        if connection.is_overloaded:
            self._end_connection(client_address, "it stopped acknowledging")
            return
        self._send_due_datagrams(connection)

    def _find_or_open_connection(
        self, client_address: Address, messages: list[RawMessage]
    ) -> ReliableConnection | None:
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

        if connection is not None and self._is_active_player_connection(client_address, connection):
            # Only a client that ended starts over while its player is logged on, and that client
            # has stopped sending keepalives: this one is still talking, so the new connection
            # comes from someone else sending from the player's address.
            raise ProtocolError("new connection from the address of an active player ignored")
        if connection is not None:
            # The client started over; whatever it had in the lobby is gone on its side too.
            self._end_connection(client_address, "the client opened a new one")
        # Checked only after the old connection of this address ended, so a client that restarts
        # does not count against its own limit.
        refusal = self._refusal_for_new_connection(client_address)
        if refusal:
            raise ProtocolError(f"new connection refused: {refusal}")
        connection = ReliableConnection(client_address, open_connection.nonce, monotonic_ms())
        self._connections[client_address] = connection
        log.info("%s: connection opened", client_address)
        return connection

    def _is_active_player_connection(self, client_address: Address, connection: ReliableConnection) -> bool:
        """True while a player is logged on over this connection and its client still sends its
        keepalives (see RECONNECT_AFTER_QUIET_MS)."""
        if not self._lobby.is_logged_on(client_address):
            return False
        return connection.quiet_ms(monotonic_ms()) < RECONNECT_AFTER_QUIET_MS

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
        """Drop a connection at once, without telling the client, and everything kept for it: its
        reachability, its probe, its router mapping and its player in the lobby."""
        log.info("%s: connection ended, %s", client_address, reason)
        del self._connections[client_address]
        self._reachability.pop(client_address, None)
        if self._prober is not None:
            self._prober.forget(client_address)
        if self._forwarding is not None:
            self._run_in_background(self._forwarding.release_player(client_address))
        self._lobby.forget_client(client_address)

    def _close_every_connection(self) -> None:
        """Send CloseConnection to every client at once, as the server stops; nothing when it never started."""
        if self._transport is None:
            return
        for client_address in list(self._connections):
            self.close_connection(client_address, is_server_stopping=True)

    def _send_due_datagrams(self, connection: ReliableConnection) -> None:
        """Send whatever the connection has due now (answers, acknowledgements, retries, keepalives)."""
        if self._transport is None:
            return
        for datagram in connection.take_datagrams_to_send(monotonic_ms()):
            if log.isEnabledFor(logging.DEBUG):
                described = ", ".join(describe_for_log(m) for m in decode_block(datagram))
                log.debug("%s <- %s", connection.client_address, described)
            self._transport.sendto(datagram, connection.client_address)


def find_open_connection(messages: list[RawMessage]) -> OpenConnection | None:
    """The first OpenConnection among a datagram's messages, decoded, or None when it has none."""
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
    close() is awaited. The database is opened here, on the event loop's thread, because a
    database connection (SQLite or MySQL) must stay on the thread that opened it."""
    database = LobbyDatabase(settings.database_location)
    # Nobody is connected at start: whatever a previous run left open has ended, and lines queued
    # while the server was stopped are stale and would reach nobody.
    database.close_open_sessions_and_games()
    discarded_count = database.discard_pending_announcements()
    if discarded_count:
        log.info("%d announcement(s) queued while the server was stopped dropped", discarded_count)
    loop = asyncio.get_running_loop()
    try:
        _, server = await loop.create_datagram_endpoint(
            lambda: LobbyServer(settings, database),
            local_addr=(settings.bind_host, settings.port),
        )
    except OSError:
        database.close()
        raise
    try:
        await attach_helpers(server, settings)
        await server.look_up_public_host()
        server.start_housekeeping()
        server.write_live_state_when_due(time.monotonic())
    except BaseException:
        # Whatever was opened so far (socket, probe socket, router mappings, database) is closed.
        await server.close()
        raise
    return server


async def attach_helpers(server: LobbyServer, settings: ServerSettings) -> None:
    """The reachability probe's socket, and the router's forwarding with --upnp."""
    loop = asyncio.get_running_loop()
    # The probe socket only needs to be another port of this machine: any free one will do.
    _, prober = await loop.create_datagram_endpoint(
        lambda: ReachabilityProber(server.record_reachability), local_addr=(settings.bind_host, ANY_FREE_PORT)
    )
    server.attach_prober(prober)
    if not settings.use_upnp:
        return
    forwarding = RouterForwarding(server.bound_address[1])
    external_ip = await forwarding.start()
    if external_ip is None:
        await forwarding.stop()
        return
    server.attach_forwarding(forwarding, external_ip)


async def serve_forever(settings: ServerSettings) -> None:
    """Run the server until Ctrl+C or SIGTERM, then close it cleanly.

    Logs where it listens and which database it uses, and warns loudly in development mode, since
    that mode lets every account log on with a shared password."""
    server = await start_server(settings)
    host, port = server.bound_address
    log.info("Settlers III lobby server %r on UDP %s:%d", settings.server_name, host, port)
    log.info(
        "database: %s, schema version %d", describe_location(settings.database_location), server.schema_version
    )
    if settings.is_development_mode:
        log.warning(
            "DEVELOPMENT MODE: every account also logs on with the password %r from this machine and "
            "its LAN. Never run this on a server others can reach.",
            DEVELOPMENT_PASSWORD,
        )
    stop_requested = asyncio.Event()
    watch_for_stop_signal(stop_requested)
    try:
        await stop_requested.wait()
        log.info("stopping")
    finally:
        await server.close()


def watch_for_stop_signal(stop_requested: asyncio.Event) -> None:
    """SIGTERM, as systemctl stop or docker stop send it, stops the server the same clean way as
    Ctrl+C: players are told, router mappings removed. Windows has no such signal handlers."""
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop_requested.set)
    except (NotImplementedError, RuntimeError):
        log.debug("no SIGTERM handler on this platform")
