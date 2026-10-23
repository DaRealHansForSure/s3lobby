"""The reachability probe: whether other players can write to a player first.

Without it a player whose router drops data from strangers never learns why chat from others does
not arrive until they write themselves; with it, /net and the lobby page say so, and what to do.
"""

from __future__ import annotations

import asyncio
import socket
import unittest
from unittest import mock

from s3lobby.constants import MessageType
from s3lobby.messages import AcknowledgeMessages, MessageId, MinMissingAcknowledge, OpenConnection
from s3lobby.reachability import Reachability, ReachabilityProber
from s3lobby.wire import Address, decode_block, encode_block

LOOPBACK = "127.0.0.1"
ANY_FREE_PORT = 0
GAME_NONCE = 77
SHORT_PROBE_INTERVAL_SECONDS = 0.05
RESULT_TIMEOUT_SECONDS = 2.0


class FakeGame(asyncio.DatagramProtocol):
    """A game's socket: answers a peer's OpenConnection with its own, as the game does, unless
    told to stay silent like a player behind a router that drops the probe."""

    def __init__(self, answers: bool) -> None:
        self.answers = answers
        self.received: list[list[int]] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, address: Address) -> None:
        messages = decode_block(data)
        self.received.append([message.message_type for message in messages])
        is_opening = any(message.message_type == MessageType.OPEN_CONNECTION for message in messages)
        if self.answers and is_opening:
            answer = [
                MessageId(1).encode(),
                OpenConnection(GAME_NONCE).encode(),
                AcknowledgeMessages((1,)).encode(),
                MinMissingAcknowledge(2).encode(),
            ]
            self.transport.sendto(encode_block(answer), address)


class ReachabilityProberTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.results: asyncio.Queue[tuple[Address, Reachability]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        _, self.prober = await loop.create_datagram_endpoint(
            lambda: ReachabilityProber(lambda address, result: self.results.put_nowait((address, result))),
            local_addr=(LOOPBACK, ANY_FREE_PORT),
        )
        self.addCleanup(self.prober.close)

    async def start_game(self, answers: bool) -> tuple[FakeGame, Address]:
        loop = asyncio.get_running_loop()
        transport, game = await loop.create_datagram_endpoint(
            lambda: FakeGame(answers), local_addr=(LOOPBACK, ANY_FREE_PORT)
        )
        self.addCleanup(transport.close)
        return game, transport.get_extra_info("sockname")

    async def next_result(self) -> tuple[Address, Reachability]:
        return await asyncio.wait_for(self.results.get(), RESULT_TIMEOUT_SECONDS)

    async def test_game_that_answers_is_open_and_its_connection_is_closed(self) -> None:
        game, address = await self.start_game(answers=True)
        self.prober.probe(address)
        self.assertEqual(await self.next_result(), (address, Reachability.OPEN))
        await asyncio.sleep(SHORT_PROBE_INTERVAL_SECONDS)
        probe, closing = game.received[0], game.received[-1]
        self.assertEqual(probe, [MessageType.MESSAGE_ID, MessageType.OPEN_CONNECTION])
        # The game's block is acknowledged in the same block as the close, which it requires.
        self.assertEqual(
            closing,
            [
                MessageType.MESSAGE_ID,
                MessageType.ACKNOWLEDGE_MESSAGES,
                MessageType.MIN_MISSING_ACKNOWLEDGE,
                MessageType.CLOSE_CONNECTION,
            ],
        )

    async def test_silent_game_is_blocked_after_every_attempt(self) -> None:
        game, address = await self.start_game(answers=False)
        with mock.patch("s3lobby.reachability.REACHABILITY_PROBE_INTERVAL_SECONDS", SHORT_PROBE_INTERVAL_SECONDS):
            self.prober.probe(address)
            self.assertEqual(await self.next_result(), (address, Reachability.BLOCKED))
        self.assertEqual(len(game.received), 3, "the probe is retried before it counts as blocked")

    async def test_datagrams_from_strangers_are_ignored(self) -> None:
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(stranger.close)
        stranger.bind((LOOPBACK, ANY_FREE_PORT))
        prober_address = self.prober._transport.get_extra_info("sockname")
        stranger.sendto(encode_block([MessageId(1).encode()]), prober_address)
        await asyncio.sleep(SHORT_PROBE_INTERVAL_SECONDS)
        self.assertTrue(self.results.empty())


if __name__ == "__main__":
    unittest.main()
