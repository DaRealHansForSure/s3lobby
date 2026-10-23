"""The router's port mappings while the server runs with --upnp (upnp.py does the talking).

At start the server's own UDP port is forwarded and the router's external address becomes the
public address, unless --public-host names one. Each player in the server's network gets an outside
UDP port of their own, forwarded to their game, for as long as they are logged on: internet players
are shown that player at the public address and that port (addresses.py). Mappings are leased and
renewed, so a server that dies without cleaning up leaves nothing behind for long; on a clean stop
they are removed at once.

The router answers slowly (tens of milliseconds per action), so every call runs on one worker
thread, one after the other, while the event loop keeps serving players.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from s3lobby.constants import (
    GAME_CLIENT_FIRST_PORT,
    GAME_CLIENT_PORT_COUNT,
    UPNP_FIRST_PLAYER_PORT,
    UPNP_LAST_PLAYER_PORT,
    UPNP_LEASE_SECONDS,
)
from s3lobby.upnp import PortMapper, UpnpError, discover_gateway
from s3lobby.wire import Address

log = logging.getLogger(__name__)

T = TypeVar("T")

UDP = "UDP"
# Error 718 ConflictInMappingEntry: the outside port is forwarded elsewhere already.
CONFLICT_IN_MAPPING_ERROR = 718
LOOPBACK_PREFIX = "127."


class RouterForwarding:
    """The mappings for the server's port and for the players in its network."""

    def __init__(self, server_port: int, find_mapper: Callable[[], PortMapper] | None = None) -> None:
        """Prepare the forwarding for the server's port; nothing talks to the router before start().

        find_mapper locates the router; tests pass a fake one instead of searching the network."""
        self._server_port = server_port
        self._find_mapper = find_mapper or default_mapper
        self._mapper: PortMapper | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="upnp")
        # Player address -> (outside port, the machine and port it is forwarded to).
        self._player_mappings: dict[Address, tuple[int, Address]] = {}
        # Changes to the mappings run one at a time, in the order they were asked for: a player who
        # leaves while their forwarding is still being made is released only after it exists.
        self._changes = asyncio.Lock()

    @property
    def is_active(self) -> bool:
        """True once start() found a router and forwarded the server's port."""
        return self._mapper is not None

    async def start(self) -> str | None:
        """Find the router and forward the server's port; the router's external address, or None
        when there is no router to talk to (the server then runs without forwarding)."""
        try:
            self._mapper = await self._run(self._find_mapper)
            local_ip = self._mapper.gateway.local_ip
            await self._run(self._mapper.add, self._server_port, UDP, local_ip, self._server_port, UPNP_LEASE_SECONDS)
            external_ip = await self._run(self._mapper.external_ip)
        except UpnpError as error:
            log.warning("UPnP: %s; running without port forwarding", error)
            self._mapper = None
            return None
        log.info("UPnP: router forwards UDP %d to %s, external address %s", self._server_port, local_ip, external_ip)
        return external_ip

    def forwarded_port(self, player_address: Address) -> int | None:
        """The outside port of a player in this network, or None when the player has none."""
        mapping = self._player_mappings.get(player_address)
        if mapping is None:
            return None
        return mapping[0]

    async def forward_player(self, player_address: Address) -> None:
        """Give a player in this network an outside port of their own. A player on this machine
        (127.0.0.1) is forwarded to this machine's address on the router's side."""
        async with self._changes:
            await self._forward_player(player_address)

    async def _forward_player(self, player_address: Address) -> None:
        """Forward the first free outside port in the player range to the player's game, skipping
        ports the router already forwards elsewhere (error 718). Callers hold the change lock."""
        if self._mapper is None or player_address in self._player_mappings:
            return
        if not is_game_in_this_network(player_address):
            log.warning("UPnP: %s:%d is not a game in this network, nothing forwarded", *player_address)
            return
        host, port = player_address
        if host.startswith(LOOPBACK_PREFIX):
            host = self._mapper.gateway.local_ip
        target = (host, port)
        used_ports = {mapping[0] for mapping in self._player_mappings.values()}
        for outside_port in range(UPNP_FIRST_PLAYER_PORT, UPNP_LAST_PLAYER_PORT + 1):
            if outside_port in used_ports:
                continue
            try:
                await self._run(self._mapper.add, outside_port, UDP, host, port, UPNP_LEASE_SECONDS)
            except UpnpError as error:
                if error.error_code == CONFLICT_IN_MAPPING_ERROR:
                    continue
                log.warning("UPnP: cannot forward a port to %s:%d: %s", host, port, error)
                return
            self._player_mappings[player_address] = (outside_port, target)
            log.info("UPnP: outside UDP %d forwarded to %s:%d", outside_port, host, port)
            return
        log.warning("UPnP: no free outside port left for %s:%d", host, port)

    async def release_player(self, player_address: Address) -> None:
        """Remove the outside port of a player who left. Waits for a forwarding still being made for
        them, so it is never left behind on the router."""
        async with self._changes:
            await self._release_player(player_address)

    async def _release_player(self, player_address: Address) -> None:
        """Delete a player's mapping on the router; a failure is only logged, as the lease ends it
        anyway. Callers hold the change lock."""
        mapping = self._player_mappings.pop(player_address, None)
        if mapping is None or self._mapper is None:
            return
        outside_port, _ = mapping
        try:
            await self._run(self._mapper.delete, outside_port, UDP)
        except UpnpError as error:
            log.debug("UPnP: removing outside port %d: %s", outside_port, error)

    async def renew(self) -> str | None:
        """Renew every lease before it runs out; the router's external address, which may have
        changed since the start, or None when the router cannot be reached."""
        if self._mapper is None:
            return None
        async with self._changes:
            return await self._renew()

    async def _renew(self) -> str | None:
        """Add every mapping again with a fresh lease and read the external address; None when the
        router fails. Callers hold the change lock."""
        try:
            local_ip = self._mapper.gateway.local_ip
            await self._run(self._mapper.add, self._server_port, UDP, local_ip, self._server_port, UPNP_LEASE_SECONDS)
            for outside_port, (host, port) in list(self._player_mappings.values()):
                await self._run(self._mapper.add, outside_port, UDP, host, port, UPNP_LEASE_SECONDS)
            return await self._run(self._mapper.external_ip)
        except UpnpError as error:
            log.warning("UPnP: renewing the forwardings failed: %s", error)
            return None

    async def stop(self) -> None:
        """Remove every mapping this server made."""
        if self._mapper is not None:
            async with self._changes:
                for player_address in list(self._player_mappings):
                    await self._release_player(player_address)
            try:
                await self._run(self._mapper.delete, self._server_port, UDP)
            except UpnpError as error:
                log.debug("UPnP: removing the server's forwarding: %s", error)
        self._executor.shutdown(wait=False)

    async def _run(self, function: Callable[..., T], *arguments: object) -> T:
        """Run one blocking router call on the worker thread and wait for its result."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, function, *arguments)


def is_game_in_this_network(player_address: Address) -> bool:
    """Only a game in this network is forwarded: a private or local address, and one of the ports
    the game binds (3343, or the next free one when several games run on one machine). A forged
    or odd address must not open some other machine's service to the internet."""
    host, port = player_address
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    is_local_address = ip.is_loopback or (ip.is_private and not ip.is_link_local)
    is_game_port = GAME_CLIENT_FIRST_PORT <= port < GAME_CLIENT_FIRST_PORT + GAME_CLIENT_PORT_COUNT
    return is_local_address and is_game_port


def default_mapper() -> PortMapper:
    """A PortMapper for the router SSDP finds; blocking, so it runs on the worker thread."""
    return PortMapper(discover_gateway())
