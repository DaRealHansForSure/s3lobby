"""Port forwarding on the router through UPnP (Internet Gateway Device), with the standard library.

With --upnp the server asks the router in front of it to forward its own UDP port, and gives every
player in its network an outside port of their own, forwarded to that player's game. Players on
the internet are then shown each of those players at the public address and that player's outside
port (addresses.py), so their chat reaches every LAN player, not only one: a hand-made forwarding of
the game's port 3343 can point at a single machine only.

The protocol, from the UPnP Forum's Internet Gateway Device specification:

1. Discovery: an SSDP M-SEARCH to the multicast group 239.255.255.250:1900; routers answer with
   the URL of their device description in a LOCATION header.
2. The description (XML) lists services; the WANIPConnection or WANPPPConnection service has the
   control URL the actions are posted to.
3. Actions are SOAP requests: GetExternalIPAddress, AddPortMapping, DeletePortMapping.

Every call here blocks on the network; the server runs them on a worker thread.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from xml.etree import ElementTree

log = logging.getLogger(__name__)

SSDP_GROUP = ("239.255.255.250", 1900)
SSDP_SEARCH_TARGET = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
SSDP_WAIT_SECONDS = 2
SSDP_DISCOVERY_SECONDS = 3.0
SSDP_ANSWER_BYTES = 2048
# The connection services that carry the port mapping actions, newest first.
WAN_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
DEVICE_NAMESPACE = "urn:schemas-upnp-org:device-1-0"
SOAP_ENVELOPE_NAMESPACE = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_ENCODING = "http://schemas.xmlsoap.org/soap/encoding/"
CONTROL_NAMESPACE = "urn:schemas-upnp-org:control-1-0"
HTTP_TIMEOUT_SECONDS = 5.0
# Descriptions and answers are a few kilobytes; anything far larger is not a router's.
MAX_RESPONSE_BYTES = 256 * 1024
# Error 725 OnlyPermanentLeasesSupported: older routers accept no lease time but 0 (permanent).
ONLY_PERMANENT_LEASES_ERROR = 725
PERMANENT_LEASE = 0
MAPPING_DESCRIPTION = "s3lobby"


class UpnpError(Exception):
    """The router could not be found, or refused or failed an action."""

    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class Gateway:
    """The router's port mapping service. local_ip is this machine's address on the router's side."""

    control_url: str
    service_type: str
    local_ip: str


def discover_gateway(timeout_seconds: float = SSDP_DISCOVERY_SECONDS) -> Gateway:
    """Find the router by SSDP and read its port mapping service from its description."""
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP[0]}:{SSDP_GROUP[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {SSDP_WAIT_SECONDS}\r\n"
        f"ST: {SSDP_SEARCH_TARGET}\r\n"
        "\r\n"
    ).encode("ascii")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as search:
        search.settimeout(timeout_seconds)
        search.sendto(request, SSDP_GROUP)
        while True:
            try:
                answer, sender = search.recvfrom(SSDP_ANSWER_BYTES)
            except socket.timeout as error:
                raise UpnpError("no UPnP router answered; is UPnP switched on in the router?") from error
            location = location_from_ssdp_answer(answer)
            if location is None or not is_on_this_network(location, sender[0]):
                continue
            try:
                return gateway_from_description(location)
            except UpnpError as error:
                log.debug("UPnP device at %s has no usable port mapping service: %s", location, error)


def location_from_ssdp_answer(answer: bytes) -> str | None:
    """The LOCATION header of an SSDP answer, or None when it has none."""
    for line in answer.decode("latin-1").split("\r\n"):
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "location":
            return value.strip()
    return None


def is_on_this_network(location: str, sender_ip: str) -> bool:
    """Accept only a plain http description served by the device that answered, on a private
    address: any other machine on the network could answer the multicast search."""
    parts = urllib.parse.urlsplit(location)
    if parts.scheme != "http" or parts.hostname != sender_ip:
        return False
    try:
        return ipaddress.ip_address(sender_ip).is_private
    except ValueError:
        return False


def gateway_from_description(location: str) -> Gateway:
    """Read the device description at location and pick its port mapping service."""
    description, local_ip = fetch(location)
    try:
        root = ElementTree.fromstring(description)
    except ElementTree.ParseError as error:
        raise UpnpError(f"unreadable device description: {error}") from error
    for service in root.iter(f"{{{DEVICE_NAMESPACE}}}service"):
        service_type = service.findtext(f"{{{DEVICE_NAMESPACE}}}serviceType", "").strip()
        control_path = service.findtext(f"{{{DEVICE_NAMESPACE}}}controlURL", "").strip()
        if service_type in WAN_SERVICE_TYPES and control_path:
            control_url = urllib.parse.urljoin(location, control_path)
            return Gateway(control_url=control_url, service_type=service_type, local_ip=local_ip)
    raise UpnpError("the device offers no WANIPConnection or WANPPPConnection service")


class PortMapper:
    """The port mapping actions of one router."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def external_ip(self) -> str:
        answer = self._call("GetExternalIPAddress", {})
        external_ip = answer.get("NewExternalIPAddress", "")
        try:
            ipaddress.ip_address(external_ip)
        except ValueError as error:
            raise UpnpError(f"the router gave no external address ({external_ip!r})") from error
        return external_ip

    def add(self, external_port: int, protocol: str, internal_ip: str, internal_port: int, lease_seconds: int) -> None:
        """Forward external_port to internal_ip:internal_port. A router that only takes permanent
        mappings gets one, which delete removes again."""
        arguments = {
            "NewRemoteHost": "",
            "NewExternalPort": str(external_port),
            "NewProtocol": protocol,
            "NewInternalPort": str(internal_port),
            "NewInternalClient": internal_ip,
            "NewEnabled": "1",
            "NewPortMappingDescription": MAPPING_DESCRIPTION,
            "NewLeaseDuration": str(lease_seconds),
        }
        try:
            self._call("AddPortMapping", arguments)
        except UpnpError as error:
            if error.error_code != ONLY_PERMANENT_LEASES_ERROR:
                raise
            arguments["NewLeaseDuration"] = str(PERMANENT_LEASE)
            self._call("AddPortMapping", arguments)

    def delete(self, external_port: int, protocol: str) -> None:
        arguments = {"NewRemoteHost": "", "NewExternalPort": str(external_port), "NewProtocol": protocol}
        self._call("DeletePortMapping", arguments)

    def _call(self, action: str, arguments: dict[str, str]) -> dict[str, str]:
        """Post one SOAP action; the answer's arguments by name. Raises UpnpError with the
        router's error code when it refuses."""
        service_type = self.gateway.service_type
        argument_xml = "".join(f"<{name}>{escape_xml(value)}</{name}>" for name, value in arguments.items())
        body = (
            '<?xml version="1.0"?>'
            f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NAMESPACE}" s:encodingStyle="{SOAP_ENCODING}">'
            f'<s:Body><u:{action} xmlns:u="{service_type}">{argument_xml}</u:{action}></s:Body>'
            "</s:Envelope>"
        ).encode("utf-8")
        headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service_type}#{action}"'}
        request = urllib.request.Request(self.gateway.control_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                answer = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as error:
            # The router's SOAP fault comes as an HTTP error; its body says why.
            with error:
                fault = error.read(MAX_RESPONSE_BYTES)
            raise upnp_error_from_fault(action, fault) from error
        except OSError as error:
            raise UpnpError(f"{action}: the router did not answer: {error}") from error
        return answer_arguments(answer, action)


def fetch(url: str) -> tuple[bytes, str]:
    """GET url; returns the body and the local address the connection came from."""
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES)
            local_ip = local_ip_towards(urllib.parse.urlsplit(url).hostname)
    except OSError as error:
        raise UpnpError(f"cannot read {url}: {error}") from error
    return body, local_ip


def local_ip_towards(host: str) -> str:
    """This machine's address on the way to host: the address the router forwards to."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, SSDP_GROUP[1]))
        return probe.getsockname()[0]


def answer_arguments(answer: bytes, action: str) -> dict[str, str]:
    """The out arguments of an action's answer, by their names without namespace."""
    try:
        root = ElementTree.fromstring(answer)
    except ElementTree.ParseError as error:
        raise UpnpError(f"{action}: unreadable answer: {error}") from error
    arguments = {}
    for element in root.iter():
        is_argument = len(element) == 0 and element.text is not None
        if is_argument:
            arguments[element.tag.rpartition("}")[2]] = element.text.strip()
    return arguments


def upnp_error_from_fault(action: str, fault: bytes) -> UpnpError:
    """The router's SOAP fault as an UpnpError with its UPnP error code, when it has one."""
    try:
        root = ElementTree.fromstring(fault)
    except ElementTree.ParseError:
        return UpnpError(f"{action} failed")
    code_text = root.findtext(f".//{{{CONTROL_NAMESPACE}}}errorCode", "")
    description = root.findtext(f".//{{{CONTROL_NAMESPACE}}}errorDescription", "")
    error_code = int(code_text) if code_text.isdigit() else None
    return UpnpError(f"{action} failed: {error_code} {description}".rstrip(), error_code)


def escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
