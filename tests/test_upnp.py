"""UPnP port forwarding: talking to the router, and the mappings the server keeps with --upnp.

Without the forwarding, internet players could reach at most one player in the server's network
(a hand-made forwarding of the game's port points at one machine), and the server's own port had
to be forwarded by hand.
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree

from s3lobby.addresses import ViewerAddresses
from s3lobby.constants import UPNP_FIRST_PLAYER_PORT
from s3lobby.forwarding import CONFLICT_IN_MAPPING_ERROR, RouterForwarding
from s3lobby.upnp import (
    ONLY_PERMANENT_LEASES_ERROR,
    Gateway,
    PortMapper,
    UpnpError,
    gateway_from_description,
    is_on_same_device,
    is_on_this_network,
    location_from_ssdp_answer,
)

LOOPBACK = "127.0.0.1"
ANY_FREE_PORT = 0
SERVICE_TYPE = "urn:schemas-upnp-org:service:WANIPConnection:1"
# A public address: documentation ranges such as 203.0.113.0/24 do not count as public.
EXTERNAL_IP = "84.75.20.9"
SERVER_PORT = 3344
SERVER_LAN_IP = "192.168.1.10"
DESCRIPTION = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <deviceList><device><deviceList><device>
      <serviceList>
        <service>
          <serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>
          <controlURL>/ctl/CmnIfCfg</controlURL>
        </service>
        <service>
          <serviceType>{SERVICE_TYPE}</serviceType>
          <controlURL>/ctl/IPConn</controlURL>
        </service>
      </serviceList>
    </device></deviceList></device></deviceList>
  </device>
</root>"""


def soap_answer(action: str, arguments: dict[str, str]) -> bytes:
    """A router's SOAP answer to action with these output arguments."""
    body = "".join(f"<{name}>{value}</{name}>" for name, value in arguments.items())
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<u:{action}Response xmlns:u="{SERVICE_TYPE}">{body}</u:{action}Response></s:Body></s:Envelope>'
    ).encode()


def soap_fault(error_code: int) -> bytes:
    """A router's SOAP fault carrying a UPnP error code, as a router refuses an action."""
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        "<s:Fault><detail><UPnPError xmlns=\"urn:schemas-upnp-org:control-1-0\">"
        f"<errorCode>{error_code}</errorCode><errorDescription>refused</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    ).encode()


class FakeRouter:
    """An Internet Gateway Device on loopback: serves its description and answers actions."""

    def __init__(self) -> None:
        """Start serving the description and the actions on a free loopback port, with no mappings yet."""
        self.mappings: dict[tuple[int, str], tuple[str, int, int]] = {}
        self.only_permanent_leases = False
        self.external_ip = EXTERNAL_IP
        router = self

        class Handler(BaseHTTPRequestHandler):
            """Answers the description on GET and the SOAP actions on POST."""

            def do_GET(self) -> None:
                """Serve the device description, whatever the path."""
                self._reply(200, DESCRIPTION.encode())

            def do_POST(self) -> None:
                """Read the SOAP action and its arguments and answer as the router decides."""
                action = self.headers["SOAPAction"].strip('"').rpartition("#")[2]
                body = self.rfile.read(int(self.headers["Content-Length"]))
                arguments = {element.tag: element.text or "" for element in ElementTree.fromstring(body).iter()}
                status, answer = router.handle(action, arguments)
                self._reply(status, answer)

            def _reply(self, status: int, body: bytes) -> None:
                """Send a status and a body with its length."""
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments: object) -> None:
                """Keep the test output free of the request log."""
                pass

        self._server = ThreadingHTTPServer((LOOPBACK, ANY_FREE_PORT), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def location(self) -> str:
        """The URL of the description, as an SSDP answer would name it."""
        host, port = self._server.server_address
        return f"http://{host}:{port}/rootDesc.xml"

    def handle(self, action: str, arguments: dict[str, str]) -> tuple[int, bytes]:
        """Carry out one SOAP action on the mappings; returns the HTTP status and the answer. Set to permanent
        leases only, it refuses any other lease with error 725 like an older router."""
        if action == "GetExternalIPAddress":
            return 200, soap_answer(action, {"NewExternalIPAddress": self.external_ip})
        key = (int(arguments["NewExternalPort"]), arguments["NewProtocol"])
        if action == "DeletePortMapping":
            self.mappings.pop(key, None)
            return 200, soap_answer(action, {})
        lease_seconds = int(arguments["NewLeaseDuration"])
        if self.only_permanent_leases and lease_seconds != 0:
            return 500, soap_fault(ONLY_PERMANENT_LEASES_ERROR)
        self.mappings[key] = (arguments["NewInternalClient"], int(arguments["NewInternalPort"]), lease_seconds)
        return 200, soap_answer(action, {})

    def stop(self) -> None:
        """Stop serving and close the socket."""
        self._server.shutdown()
        self._server.server_close()


class UpnpClientTest(unittest.TestCase):
    """PortMapper and the discovery helpers against a fake router over HTTP on loopback."""

    def setUp(self) -> None:
        """A fake router and a PortMapper for the gateway read from its description."""
        self.router = FakeRouter()
        self.addCleanup(self.router.stop)
        self.mapper = PortMapper(gateway_from_description(self.router.location))

    def test_description_yields_the_port_mapping_service(self) -> None:
        """The description is searched for the WANIPConnection service through the nested devices, its control
        URL is taken, and the local IP is the one that faces the router."""
        gateway = self.mapper.gateway
        self.assertEqual(gateway.service_type, SERVICE_TYPE)
        self.assertTrue(gateway.control_url.endswith("/ctl/IPConn"))
        self.assertEqual(gateway.local_ip, LOOPBACK)

    def test_mapping_is_added_read_and_removed(self) -> None:
        """The external address is read, and a mapping is added with its target and lease and deleted again.
        """
        self.assertEqual(self.mapper.external_ip(), EXTERNAL_IP)
        self.mapper.add(33430, "UDP", "192.168.1.20", 3343, lease_seconds=3600)
        self.assertEqual(self.router.mappings[(33430, "UDP")], ("192.168.1.20", 3343, 3600))
        self.mapper.delete(33430, "UDP")
        self.assertEqual(self.router.mappings, {})

    def test_private_external_address_is_refused(self) -> None:
        """Internet players are sent to this address: a router behind another router knows only
        a private one, which would send them nowhere."""
        self.router.external_ip = "10.0.0.1"
        with self.assertRaises(UpnpError):
            self.mapper.external_ip()

    def test_control_url_on_another_machine_is_refused(self) -> None:
        """A description could point the control URL at another machine or scheme, and the server would send
        its requests there; only a plain http URL on the host that served the description counts."""
        self.assertFalse(is_on_same_device("http://169.254.169.254/latest", "http://192.168.1.1:5000/desc.xml"))
        self.assertFalse(is_on_same_device("https://192.168.1.1/ctl", "http://192.168.1.1:5000/desc.xml"))
        self.assertTrue(is_on_same_device("http://192.168.1.1:5000/ctl/IPConn", "http://192.168.1.1:5000/desc.xml"))

    def test_router_with_permanent_leases_only_gets_one(self) -> None:
        """Older routers accept no lease time but 0 and refuse others with error 725; the mapping is then
        added again as permanent instead of failing."""
        self.router.only_permanent_leases = True
        self.mapper.add(33430, "UDP", "192.168.1.20", 3343, lease_seconds=3600)
        self.assertEqual(self.router.mappings[(33430, "UDP")][2], 0)

    def test_ssdp_answer_is_taken_only_from_the_device_on_this_network(self) -> None:
        """The description location from an SSDP answer is used only when it points at the private address
        that answered, so another machine or a file URL cannot make the server fetch anything else."""
        answer = b"HTTP/1.1 200 OK\r\nST: x\r\nLocation: http://192.168.1.1:5000/rootDesc.xml\r\n\r\n"
        location = location_from_ssdp_answer(answer)
        self.assertEqual(location, "http://192.168.1.1:5000/rootDesc.xml")
        self.assertTrue(is_on_this_network(location, "192.168.1.1"))
        self.assertFalse(is_on_this_network(location, "192.168.1.66"), "another machine's answer")
        self.assertFalse(is_on_this_network("http://8.8.8.8/desc.xml", "8.8.8.8"), "not a private address")
        self.assertFalse(is_on_this_network("file:///etc/passwd", "192.168.1.1"))


class FakeMapper:
    """A PortMapper stand-in that keeps the mappings in memory."""

    def __init__(self, taken_ports: set[int] = frozenset()) -> None:
        """A gateway for the server's LAN address, no mappings, and outside ports another forwarding already
        holds."""
        self.gateway = Gateway(control_url="http://192.168.1.1/ctl", service_type=SERVICE_TYPE, local_ip=SERVER_LAN_IP)
        self.mappings: dict[int, tuple[str, int]] = {}
        self._taken_ports = taken_ports

    def external_ip(self) -> str:
        """The router's public address."""
        return EXTERNAL_IP

    def add(self, external_port: int, protocol: str, internal_ip: str, internal_port: int, lease_seconds: int) -> None:
        """Keep the mapping, or fail with CONFLICT_IN_MAPPING_ERROR when the outside port is taken, as a
        router does."""
        if external_port in self._taken_ports:
            raise UpnpError("taken", CONFLICT_IN_MAPPING_ERROR)
        self.mappings[external_port] = (internal_ip, internal_port)

    def delete(self, external_port: int, protocol: str) -> None:
        """Forget the mapping of an outside port, if there is one."""
        self.mappings.pop(external_port, None)


class RouterForwardingTest(unittest.IsolatedAsyncioTestCase):
    """RouterForwarding, the mappings the server keeps with --upnp, on a fake mapper."""

    async def start(self, mapper: FakeMapper) -> RouterForwarding:
        """Start forwarding with mapper and check it reports the router's public address."""
        forwarding = RouterForwarding(SERVER_PORT, find_mapper=lambda: mapper)
        self.assertEqual(await forwarding.start(), EXTERNAL_IP)
        return forwarding

    async def test_server_port_and_one_port_per_lan_player_are_forwarded(self) -> None:
        """The server's port is forwarded at start, and each LAN player gets an outside port of its own from
        UPNP_FIRST_PLAYER_PORT on, so internet players can reach every one of them; a player leaving frees
        the port, and a clean stop leaves no mapping on the router."""
        mapper = FakeMapper()
        forwarding = await self.start(mapper)
        self.assertEqual(mapper.mappings[SERVER_PORT], (SERVER_LAN_IP, SERVER_PORT))
        await forwarding.forward_player(("192.168.1.20", 3343))
        await forwarding.forward_player(("192.168.1.21", 3343))
        self.assertEqual(forwarding.forwarded_port(("192.168.1.20", 3343)), UPNP_FIRST_PLAYER_PORT)
        self.assertEqual(forwarding.forwarded_port(("192.168.1.21", 3343)), UPNP_FIRST_PLAYER_PORT + 1)
        self.assertEqual(mapper.mappings[UPNP_FIRST_PLAYER_PORT + 1], ("192.168.1.21", 3343))

        await forwarding.release_player(("192.168.1.20", 3343))
        self.assertNotIn(UPNP_FIRST_PLAYER_PORT, mapper.mappings)
        await forwarding.stop()
        self.assertEqual(mapper.mappings, {}, "a clean stop leaves nothing on the router")

    async def test_player_on_the_server_machine_is_forwarded_to_its_lan_address(self) -> None:
        """A player on the server's machine comes from 127.0.0.1, which the router cannot forward to; the
        mapping must use the machine's LAN address."""
        mapper = FakeMapper()
        forwarding = await self.start(mapper)
        await forwarding.forward_player((LOOPBACK, 3343))
        self.assertEqual(mapper.mappings[UPNP_FIRST_PLAYER_PORT], (SERVER_LAN_IP, 3343))
        await forwarding.stop()

    async def test_outside_port_taken_by_another_forwarding_is_skipped(self) -> None:
        """An outside port the router already uses for something else is skipped for the next free one rather
        than giving up on the player."""
        mapper = FakeMapper(taken_ports={UPNP_FIRST_PLAYER_PORT})
        forwarding = await self.start(mapper)
        await forwarding.forward_player(("192.168.1.20", 3343))
        self.assertEqual(forwarding.forwarded_port(("192.168.1.20", 3343)), UPNP_FIRST_PLAYER_PORT + 1)
        await forwarding.stop()

    async def test_only_games_in_this_network_are_forwarded(self) -> None:
        """A forged or odd address must not open another machine's service to the internet."""
        mapper = FakeMapper()
        forwarding = await self.start(mapper)
        for address in (("192.168.1.20", 161), ("84.75.10.4", 3343), ("169.254.1.2", 3343)):
            with self.subTest(address=address):
                await forwarding.forward_player(address)
                self.assertIsNone(forwarding.forwarded_port(address))
        self.assertEqual(list(mapper.mappings), [SERVER_PORT])
        await forwarding.stop()

    async def test_player_leaving_while_being_forwarded_is_released_after_it(self) -> None:
        """Without the order, the release would find nothing and the late mapping would stay."""
        mapper = FakeMapper()
        forwarding = await self.start(mapper)
        player = ("192.168.1.20", 3343)
        forwarding_task = asyncio.ensure_future(forwarding.forward_player(player))
        release_task = asyncio.ensure_future(forwarding.release_player(player))
        await asyncio.gather(forwarding_task, release_task)
        self.assertEqual(list(mapper.mappings), [SERVER_PORT])
        await forwarding.stop()

    async def test_without_a_router_the_server_runs_without_forwarding(self) -> None:
        """Without a UPnP router the server starts all the same, with forwarding inactive, rather than
        failing."""
        def no_router() -> FakeMapper:
            """Stands in for a search that finds no router."""
            raise UpnpError("no UPnP router answered")

        forwarding = RouterForwarding(SERVER_PORT, find_mapper=no_router)
        self.assertIsNone(await forwarding.start())
        self.assertFalse(forwarding.is_active)
        await forwarding.stop()

    async def test_internet_players_see_lan_players_at_their_own_outside_port(self) -> None:
        """Internet players are shown a LAN player at the public address and the outside port forwarded for
        that player, while LAN players keep seeing the real address."""
        forwarding = await self.start(FakeMapper())
        await forwarding.forward_player(("192.168.1.20", 3343))
        addresses = ViewerAddresses(lambda viewer: SERVER_LAN_IP, lambda: EXTERNAL_IP, forwarding.forwarded_port)
        shown = addresses.address_for_viewer(("192.168.1.20", 3343), ("84.75.10.4", 40000))
        self.assertEqual(shown, (EXTERNAL_IP, UPNP_FIRST_PLAYER_PORT))
        self.assertEqual(addresses.address_for_viewer(("192.168.1.20", 3343), ("192.168.1.21", 3343)), ("192.168.1.20", 3343))
        await forwarding.stop()


if __name__ == "__main__":
    unittest.main()
