"""The address one client is shown for another, so the client can reach it.

Clients send chat straight to each other (loop 0x5c661e) and connect DirectPlay to a game's host
(JoinLobbyGame 0x467e00), at the addresses the server gave them. The server knows each client by
the address its datagrams come from. Passed on as it is, that address only works for clients in
the same network:

- A client on the server's own machine comes from 127.0.0.1. Any other client must get an address
  of this machine instead: the one that faces it, or the public host for a client on the internet.
- A client in the server's LAN comes from a private address, which a client on the internet cannot
  reach. With a public host configured, internet clients get the public address instead; the
  router must then forward the player's UDP port (the game's 3343, or the next free one) to that
  machine, and a host also DirectPlay's ports (TCP 47624, TCP and UDP 2300 to 2400).

Everything else passes unchanged: clients in the same network see each other's real addresses,
and an internet client's address is reachable for everyone. LobbyServer.send applies this to every
message, so the lobby keeps one address per player and never needs to know who is looking.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import replace

from s3lobby.messages import (
    AddPlayerToGroup,
    AddPlayerToGroupWithClan,
    AddPlayerToGroupWithFlags,
    EncodableMessage,
    GroupAnnouncement,
    GroupUpdate,
)
from s3lobby.wire import Address


class ViewerAddresses:
    """Turns a client's address as the server sees it into the address a given viewer can reach.

    local_ip_facing names the IP of this machine that a viewer reaches it at; public_ip returns the
    public host's current IP, or None when none is configured or it could not be resolved;
    forwarded_port returns the outside port the router forwards to a player in this network (UPnP),
    or None when it forwards none.
    """

    def __init__(
        self,
        local_ip_facing: Callable[[Address], str],
        public_ip: Callable[[], str | None],
        forwarded_port: Callable[[Address], int | None] = lambda subject: None,
    ) -> None:
        self._local_ip_facing = local_ip_facing
        self._public_ip = public_ip
        self._forwarded_port = forwarded_port

    def address_for_viewer(self, subject: Address, viewer: Address) -> Address:
        """The address the viewer should use for the subject."""
        subject_host, subject_port = subject
        try:
            subject_ip = ipaddress.ip_address(subject_host)
            viewer_ip = ipaddress.ip_address(viewer[0])
        except ValueError:
            return subject
        # 0.0.0.0 is a host's own placeholder, not an address: nothing to translate.
        if subject_ip.is_unspecified or subject_ip == viewer_ip:
            return subject
        is_viewer_on_the_internet = viewer_ip.is_global
        if subject_ip.is_loopback:
            this_machine = self._this_machine_for(viewer, is_viewer_on_the_internet)
            if this_machine is None:
                return subject
            return (this_machine, self._outside_port(subject, is_viewer_on_the_internet))
        is_subject_hidden_from_viewer = not subject_ip.is_global and is_viewer_on_the_internet
        if not is_subject_hidden_from_viewer:
            return subject
        public_ip = self._public_ip()
        if public_ip is None:
            return subject
        return (public_ip, self._outside_port(subject, is_viewer_on_the_internet))

    def message_for_viewer(self, message: EncodableMessage, viewer: Address) -> EncodableMessage:
        """The message with every address in it as the viewer can reach it."""
        if isinstance(message, (AddPlayerToGroup, AddPlayerToGroupWithClan, AddPlayerToGroupWithFlags)):
            return replace(message, player_address=self.address_for_viewer(message.player_address, viewer))
        if isinstance(message, (GroupAnnouncement, GroupUpdate)) and message.game_block is not None:
            host_address = self.address_for_viewer(message.game_block.host_address, viewer)
            return replace(message, game_block=replace(message.game_block, host_address=host_address))
        return message

    def _outside_port(self, subject: Address, is_viewer_on_the_internet: bool) -> int:
        """The port an internet viewer reaches the subject at: its own forwarded port when the
        router forwards one, else the game's port, which the router may forward by hand."""
        subject_port = subject[1]
        if not is_viewer_on_the_internet:
            return subject_port
        forwarded_port = self._forwarded_port(subject)
        if forwarded_port is None:
            return subject_port
        return forwarded_port

    def _this_machine_for(self, viewer: Address, is_viewer_on_the_internet: bool) -> str | None:
        """This machine's IP as the viewer reaches it; None when there is no route to the viewer."""
        public_ip = self._public_ip()
        if is_viewer_on_the_internet and public_ip is not None:
            return public_ip
        try:
            return self._local_ip_facing(viewer)
        except OSError:
            return None
