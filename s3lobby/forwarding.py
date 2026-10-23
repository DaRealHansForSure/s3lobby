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
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from s3lobby.constants import (
    UPNP_FIRST_PLAYER_PORT,
    UPNP_LAST_PLAYER_PORT,
    UPNP_LEASE_SECONDS,
)
from s3lobby.upnp import PortMapper, UpnpError, discover_gateway
from s3lobby.wire import Address

log = logging.getLogger(__name__)

UDP = "UDP"
# Error 718 ConflictInMappingEntry: the outside port is forwarded elsewhere already.
CONFLICT_IN_MAPPING_ERROR = 718
LOOPBACK_PREFIX = "127."


class RouterForwarding:
    """The mappings for the server's port and for the players in its network."""

    def __init__(self, server_port: int, find_mapper: Callable[[], PortMapper] | None = None) -> None:
        self._server_port = server_port
        self._find_mapper = find_mapper or default_mapper
        self._mapper: PortMapper | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="upnp")
        # Player address -> (outside port, the machine and port it is forwarded to).
        self._player_mappings: dict[Address, tuple[int, Address]] = {}

    @property
    def is_active(self) -> bool:
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
        if self._mapper is None or player_address in self._player_mappings:
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
            for player_address in list(self._player_mappings):
                await self.release_player(player_address)
            try:
                await self._run(self._mapper.delete, self._server_port, UDP)
            except UpnpError as error:
                log.debug("UPnP: removing the server's forwarding: %s", error)
        self._executor.shutdown(wait=False)

    async def _run(self, function: Callable, *arguments: object):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, function, *arguments)


def default_mapper() -> PortMapper:
    return PortMapper(discover_gateway())
