"""Can other players reach a player directly? A probe from a second port of the server tells.

Chat goes straight between the players, and many home routers drop data from anyone the player has
not written to yet. Right after the logon, the server sends the player's game an OpenConnection from
another UDP port, as another player's game would. The game accepts connections from unknown peers
(the session's open callbacks only return true, 0x5eec30, 0x5eec40) and answers with its own
OpenConnection (*inferred* from PP.OpenIncoming 0x6045c0). An answer means the player's router let
the probe in; silence means it dropped it. The prober then closes the connection with an
acknowledgement and CloseConnection; for an address that holds no group, the game's ConnectionClosed
does nothing (0x5cc9fb..0x5cca39).

The probe comes from the server's own IP. A router that drops data from ports it has not sent to
(the usual Linux behaviour) is caught; one that only checks the IP would pass the probe and still
drop other players. So an open result is a good sign, not a proof, and a closed one is certain.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from enum import Enum
from typing import cast

from s3lobby.constants import (
    CONNECTION_NONCE_MAX,
    CONNECTION_NONCE_MIN,
    REACHABILITY_PROBE_ATTEMPTS,
    REACHABILITY_PROBE_INTERVAL_SECONDS,
)
from s3lobby.messages import AcknowledgeMessages, CloseConnection, MessageId, MinMissingAcknowledge, OpenConnection
from s3lobby.wire import Address, ProtocolError, decode_block, encode_block

log = logging.getLogger(__name__)

PROBE_BLOCK_ID = 1
CLOSING_BLOCK_ID = 2


class Reachability(Enum):
    """What the probe found out about a player, as the web interface and /net show it."""

    NOT_CHECKED = "not checked"
    CHECKING = "checking"
    OPEN = "open"
    BLOCKED = "blocked"


class ReachabilityProber(asyncio.DatagramProtocol):
    """Probes players from its own UDP socket and reports each result to on_result."""

    def __init__(self, on_result: Callable[[Address, Reachability], None]) -> None:
        self._on_result = on_result
        self._transport: asyncio.DatagramTransport | None = None
        self._pending: dict[Address, asyncio.TimerHandle] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # A datagram endpoint always hands over a DatagramTransport.
        self._transport = cast(asyncio.DatagramTransport, transport)

    def probe(self, address: Address) -> None:
        """Send the probe, and retry it a few times: a single datagram may simply get lost."""
        if self._transport is None or address in self._pending:
            return
        nonce = random.randint(CONNECTION_NONCE_MIN, CONNECTION_NONCE_MAX)
        probe_block = encode_block([MessageId(PROBE_BLOCK_ID).encode(), OpenConnection(nonce).encode()])
        self._send_probe(address, probe_block, REACHABILITY_PROBE_ATTEMPTS)

    def forget(self, address: Address) -> None:
        """The player left: stop probing them."""
        timer = self._pending.pop(address, None)
        if timer is not None:
            timer.cancel()

    def close(self) -> None:
        for timer in self._pending.values():
            timer.cancel()
        self._pending.clear()
        if self._transport is not None:
            self._transport.close()

    def datagram_received(self, data: bytes, address: Address) -> None:
        # Anything from a probed address is its answer; datagrams from anyone else are ignored.
        timer = self._pending.pop(address, None)
        if timer is None:
            return
        timer.cancel()
        self._close_probe_connection(data, address)
        log.info("%s: reachable from other ports", address)
        self._on_result(address, Reachability.OPEN)

    def error_received(self, error: OSError) -> None:
        log.debug("reachability probe socket: %s", error)

    def _send_probe(self, address: Address, probe_block: bytes, attempts_left: int) -> None:
        if attempts_left == 0:
            self._pending.pop(address, None)
            log.info("%s: not reachable from other ports", address)
            self._on_result(address, Reachability.BLOCKED)
            return
        if self._transport is None:
            return
        self._transport.sendto(probe_block, address)
        loop = asyncio.get_running_loop()
        self._pending[address] = loop.call_later(
            REACHABILITY_PROBE_INTERVAL_SECONDS, self._send_probe, address, probe_block, attempts_left - 1
        )

    def _close_probe_connection(self, data: bytes, address: Address) -> None:
        """Acknowledge the game's answer and close, so it does not keep the connection for its
        minute of timeout. Its block has to be acknowledged in the same block as the close: the
        game checks it is in sync before it closes (PP.CheckInSync 0x606310)."""
        try:
            messages = decode_block(data)
        except ProtocolError:
            return
        if not messages or self._transport is None:
            return
        try:
            answer_block_id = MessageId.decode(messages[0]).block_id
        except ProtocolError:
            return
        closing_block = encode_block(
            [
                MessageId(CLOSING_BLOCK_ID).encode(),
                AcknowledgeMessages((answer_block_id,)).encode(),
                MinMissingAcknowledge(CLOSING_BLOCK_ID).encode(),
                CloseConnection().encode(),
            ]
        )
        self._transport.sendto(closing_block, address)
