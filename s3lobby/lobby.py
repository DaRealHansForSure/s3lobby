"""The lobby: players, rooms, games, and the answers the game expects to every request.

The server plays both roles the game knows (docs/protocol.md, "Lobby layer"):
- index server: the patch check and the list of lobby servers, asked first;
- lobby server: logon, rooms and zones, and the games players open in them.

In this protocol rooms, zones and games are all "groups". A client joins a group with
AddPlayerToGroup, the server announces the group to it with parent 0 ("you are in") and lists its
members, and everyone who can see a group hears when players come and go. Games are groups under
a zone; the match itself runs over DirectPlay between the players, never through this server.

Several rules here exist because the real client breaks without them. Each one is explained where
it is applied, with the address in S3.EXE that shows why.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

from s3lobby.constants import (
    FIRST_GAME_GROUP_ID,
    JOINED_GROUP_PARENT_ID,
    LOGON_PROTOCOL_VERSION,
    PRIVATE_CHAT_GROUP_ID,
    SERVER_ASSIGNS_GROUP_ID,
    MessageType,
    ResultCode,
)
from s3lobby.messages import (
    AddPlayerToGroup,
    ChatCommand,
    ChatCommandAnswer,
    ChatMessage,
    CheckFileVersion,
    EmptyImageList,
    EncodableMessage,
    GameStatus,
    GetImageList,
    GetTextMessage,
    GroupAnnouncement,
    GroupUpdate,
    LobbyServerEntry,
    LobbyServerList,
    Logon,
    OwnPlayer,
    RemoveGroupFromGroup,
    RemovePlayerFromGroup,
    Result,
    TextMessage,
)
from s3lobby.wire import Address, ProtocolError, RawMessage

log = logging.getLogger(__name__)

# What a host's game reports as its own address before the server fills in the real one: the
# client binds its socket to INADDR_ANY and so only knows itself as 0.0.0.0.
UNKNOWN_HOST = "0.0.0.0"

FIRST_PLAYER_ID = 1


class LobbyOutbox(Protocol):
    """What the lobby needs from the network side: a way to reach a client, and the address under
    which that client reaches this server."""

    def send(self, client_address: Address, message: EncodableMessage) -> None: ...

    def server_address_text_for(self, client_address: Address) -> str: ...


@dataclass
class Player:
    """A logged on player. The address is where the player's datagrams come from."""

    player_id: int
    name: str
    address: Address
    group_ids: set[int] = field(default_factory=set)


@dataclass
class Group:
    """A room, a zone or a game.

    Rooms and zones are made the first time a client joins them and stay for as long as the
    server runs. Games are opened by a host and closed when their last member leaves.
    """

    announcement: GroupAnnouncement
    is_room: bool
    member_ids: list[int] = field(default_factory=list)

    @property
    def group_id(self) -> int:
        return self.announcement.group_id

    @property
    def parent_id(self) -> int:
        return self.announcement.parent_id


class Lobby:
    """Keeps the lobby's state and answers every lobby message a client sends."""

    def __init__(self, server_name: str, outbox: LobbyOutbox) -> None:
        self._server_name = server_name
        self._outbox = outbox
        self._players_by_address: dict[Address, Player] = {}
        self._players_by_id: dict[int, Player] = {}
        self._groups: dict[int, Group] = {}
        self._next_player_id = FIRST_PLAYER_ID
        self._next_game_group_id = FIRST_GAME_GROUP_ID
        self._handlers: dict[int, Callable[[Address, RawMessage], None]] = {
            MessageType.CHECK_FILE_VERSION: self._answer_patch_check,
            MessageType.GET_LOBBY_SERVER_LIST: self._answer_lobby_server_list,
            MessageType.LOGON: self._log_on_player,
            MessageType.LOGOUT: self._log_out_player,
            MessageType.ADD_PLAYER_TO_GROUP: self._join_group_on_request,
            MessageType.REMOVE_PLAYER_FROM_GROUP: self._leave_group_on_request,
            MessageType.ADD_GROUP_TO_GROUP: self._open_game_on_request,
            MessageType.ADD_GROUP_TO_GROUP_2: self._open_game_on_request,
            MessageType.UPDATE_GROUP: self._relay_group_update,
            MessageType.UPDATE_GROUP_2: self._relay_group_update,
            MessageType.REMOVE_GROUP_FROM_GROUP: self._close_game_on_request,
            MessageType.CHAT_MESSAGE: self._log_chat_message,
            MessageType.CHAT_MESSAGE_2: self._log_chat_message,
            MessageType.CHAT_COMMAND: self._answer_chat_command,
            MessageType.GET_IMAGE_LIST: self._answer_image_list,
            MessageType.GET_TEXT_MESSAGE: self._answer_text_message,
            MessageType.GAME_STATUS: self._log_game_status,
        }

    def handle_message(self, client_address: Address, message: RawMessage) -> None:
        """Act on one lobby message. A malformed message is logged and skipped; the rest of the
        client's block is still handled, as the game handles each message on its own."""
        handler = self._handlers.get(message.message_type)
        if handler is None:
            log.warning("%s: no handler for %s (%s)", client_address, message.type_name, message.body.hex())
            return
        try:
            handler(client_address, message)
        except ProtocolError as error:
            log.warning("%s: unreadable %s: %s (%s)", client_address, message.type_name, error, message.body.hex())

    def forget_client(self, client_address: Address) -> None:
        """The client's connection is gone: take its player out of every group. Nobody can tell the
        player anything any more, so only the remaining members hear about it."""
        player = self._players_by_address.pop(client_address, None)
        if player is None:
            return
        del self._players_by_id[player.player_id]
        log.info("player %d %r left the lobby", player.player_id, player.name)
        for group_id in list(player.group_ids):
            self._remove_player_from_group(player, group_id, is_player_told=False)

    # Index server role.

    def _answer_patch_check(self, client_address: Address, message: RawMessage) -> None:
        # Result NO_PATCH_NEEDED is the simplest answer the client accepts: its handler marks the
        # answer as received and takes this server as its index server (0x5cff1c, 0x5cff27). The
        # alternative, a patch information message, is easy to get wrong (every accessor asserts
        # a minimum size) and only needed to offer an FTP patch.
        request = CheckFileVersion.decode(message)
        log.info(
            "%s: patch check, game version %d, edition %d: no patch needed",
            client_address,
            request.game_version,
            request.edition,
        )
        self._outbox.send(client_address, Result(ResultCode.NO_PATCH_NEEDED))

    def _answer_lobby_server_list(self, client_address: Address, message: RawMessage) -> None:
        # The list names this same server: the client then closes the index connection and opens
        # a new one here for the logon.
        server_address_text = self._outbox.server_address_text_for(client_address)
        log.info("%s: lobby server list: %s %r", client_address, server_address_text, self._server_name)
        this_server = LobbyServerEntry(address_text=server_address_text, name=self._server_name)
        self._outbox.send(client_address, LobbyServerList(servers=(this_server,)))

    # Lobby server role: logon.

    def _log_on_player(self, client_address: Address, message: RawMessage) -> None:
        # A LAN server welcomes everyone: name, password and serial number are taken as they come.
        logon = Logon.decode(message)
        if logon.protocol_version != LOGON_PROTOCOL_VERSION:
            log.warning(
                "%s: logon protocol version %d, this server knows %d",
                client_address,
                logon.protocol_version,
                LOGON_PROTOCOL_VERSION,
            )
        self.forget_client(client_address)
        player = Player(player_id=self._next_player_id, name=logon.player_name, address=client_address)
        self._next_player_id += 1
        self._players_by_address[client_address] = player
        self._players_by_id[player.player_id] = player
        log.info(
            "%s: logon of %r (flags %#x, serial %r) as player %d",
            client_address,
            logon.player_name,
            logon.flags,
            logon.serial_number,
            player.player_id,
        )
        # Order matters: the Result makes this server the client's lobby server, and the OwnPlayer
        # handler asserts exactly that (handler 0x5d7300). Joining a group later uses the own player
        # without a check (0x5ca46b), so OwnPlayer is not optional either.
        self._outbox.send(client_address, Result(ResultCode.LOGON_ACCEPTED))
        self._outbox.send(client_address, OwnPlayer(player.player_id))

    def _log_out_player(self, client_address: Address, message: RawMessage) -> None:
        self.forget_client(client_address)

    # Lobby server role: rooms, zones and games.

    def _join_group_on_request(self, client_address: Address, message: RawMessage) -> None:
        # Right after the logon the client joins its lobby room (1, 2 or 3) and one zone per bit of
        # its GameZoneSelection setting (30 to 38 with the default 511), all in one block. There is
        # no request for a list of groups first, so any group a client asks for is made on the spot.
        request = AddPlayerToGroup.decode(message)
        player = self._find_logged_on_player(client_address)
        is_real_group = request.group_id != PRIVATE_CHAT_GROUP_ID
        if player is None or not is_real_group:
            return
        group = self._find_or_open_room(request.group_id)
        if player.player_id in group.member_ids:
            return
        if group.is_room:
            # Back from a match, the client joins its rooms again but never says it left its game.
            self._take_player_out_of_games(player)
        log.info("player %d %r joins group %d", player.player_id, player.name, group.group_id)
        self._send_group_contents(player, group)
        for member in self._members_of(group):
            self._outbox.send(member.address, member_entry(group.group_id, player))
        group.member_ids.append(player.player_id)
        player.group_ids.add(group.group_id)

    def _leave_group_on_request(self, client_address: Address, message: RawMessage) -> None:
        request = RemovePlayerFromGroup.decode(message)
        player = self._find_logged_on_player(client_address)
        if player is not None:
            self._remove_player_from_group(player, request.group_id, is_player_told=True)

    def _open_game_on_request(self, client_address: Address, message: RawMessage) -> None:
        request = GroupAnnouncement.decode(message)
        player = self._find_logged_on_player(client_address)
        zone = self._groups.get(request.parent_id)
        is_acceptable = (
            player is not None
            and zone is not None
            and request.group_id != PRIVATE_CHAT_GROUP_ID
            and request.group_id not in self._groups
        )
        if not is_acceptable:
            log.warning("%s: ignored new group %d under %d", client_address, request.group_id, request.parent_id)
            return

        announcement = with_real_host_address(request, client_address)
        if announcement.group_id == SERVER_ASSIGNS_GROUP_ID:
            announcement = replace(announcement, group_id=self._next_game_group_id)
            self._next_game_group_id += 1
        game = Group(announcement=announcement, is_room=False)
        self._groups[game.group_id] = game
        log.info("player %d %r opens game %d in group %d", player.player_id, player.name, game.group_id, zone.group_id)

        # The host's client does not count itself in its own game (CreateGame 0x5ca7d0): it joins
        # when the server announces the game to it with parent 0, like any other join.
        self._send_group_contents(player, game)
        game.member_ids.append(player.player_id)
        player.group_ids.add(game.group_id)
        for member in self._members_of(zone):
            if member is not player:
                self._outbox.send(member.address, game.announcement)

    def _relay_group_update(self, client_address: Address, message: RawMessage) -> None:
        # TODO: apply the update to the stored announcement, so players who join the zone later see
        # the game's current settings. Needs the update mask decoded first (docs/protocol.md,
        # UpdateGroup); until then later joiners see the settings the game was opened with.
        update = GroupUpdate.decode(message)
        group = self._groups.get(update.group_id)
        if group is not None:
            self._relay_to_everyone_who_sees(group, update, sender_address=client_address)

    def _close_game_on_request(self, client_address: Address, message: RawMessage) -> None:
        request = RemoveGroupFromGroup.decode(message)
        group = self._groups.get(request.group_id)
        if group is None or group.is_room:
            return
        self._relay_to_everyone_who_sees(group, request, sender_address=client_address)
        self._forget_group(group)

    # Chat and other requests.

    def _log_chat_message(self, client_address: Address, message: RawMessage) -> None:
        # Clients chat with each other directly; the server only sees lines meant for it.
        chat = ChatMessage.decode(message)
        log.info("%s: chat to group %d: %r", client_address, chat.group_id, chat.text)

    def _answer_chat_command(self, client_address: Address, message: RawMessage) -> None:
        command = ChatCommand.decode(message)
        answer = ChatCommandAnswer(group_id=command.group_id, lines=(f"Unknown command: {command.command}",))
        self._outbox.send(client_address, answer)

    def _answer_image_list(self, client_address: Address, message: RawMessage) -> None:
        request = GetImageList.decode(message)
        self._outbox.send(client_address, EmptyImageList(group_id=request.group_id))

    def _answer_text_message(self, client_address: Address, message: RawMessage) -> None:
        request = GetTextMessage.decode(message)
        self._outbox.send(client_address, TextMessage(text_id=request.text_id))

    def _log_game_status(self, client_address: Address, message: RawMessage) -> None:
        status = GameStatus.decode(message)
        log.info("%s: game status for group %d: %s", client_address, status.group_id, status.rest_of_body.hex())

    # Group bookkeeping.

    def _send_group_contents(self, player: Player, group: Group) -> None:
        """What a client needs when it joins a group: the group itself with parent 0, every member
        with the address they can be reached at (the joining player last), and the games inside."""
        self._outbox.send(player.address, group.announcement.under_parent(JOINED_GROUP_PARENT_ID))
        for member in self._members_of(group):
            self._outbox.send(player.address, member_entry(group.group_id, member))
        self._outbox.send(player.address, member_entry(group.group_id, player))
        for child in self._groups.values():
            if child.parent_id == group.group_id:
                self._outbox.send(player.address, child.announcement)

    def _remove_player_from_group(self, player: Player, group_id: int, is_player_told: bool) -> None:
        """Take a player out of a group and tell the remaining members.

        is_player_told sends the confirmation to the player as well. A client that asked to leave
        needs it: it keeps the group on its joined list until the server confirms (handler
        0x5d6af0, own player check at 0x5d721e), and joining that group again would then fail
        (assert "! vJoinedGroups.contains") and drop the connection.
        """
        group = self._groups.get(group_id)
        player.group_ids.discard(group_id)
        if group is None or player.player_id not in group.member_ids:
            return
        group.member_ids.remove(player.player_id)
        log.info("player %d %r leaves group %d", player.player_id, player.name, group_id)
        departure = RemovePlayerFromGroup(group_id=group_id, player_id=player.player_id)
        for member in self._members_of(group):
            self._outbox.send(member.address, departure)
        if is_player_told:
            self._outbox.send(player.address, departure)
        is_empty_game = not group.member_ids and not group.is_room
        if is_empty_game:
            self._close_game(group)

    def _take_player_out_of_games(self, player: Player) -> None:
        """Take a player who is back from a match out of its game, without telling the player.

        After a played match the client may still list the game, but after "Zurück" in the game
        setup it has already dropped it, and a confirmation for a group it does not list makes its
        handler throw a sync error (0x5d6af0): the game hangs. Saying nothing is safe in both cases.
        """
        for group_id in list(player.group_ids):
            group = self._groups.get(group_id)
            if group is not None and not group.is_room:
                self._remove_player_from_group(player, group_id, is_player_told=False)

    def _close_game(self, game: Group) -> None:
        zone = self._groups.get(game.parent_id)
        if zone is not None:
            removal = RemoveGroupFromGroup(parent_id=zone.group_id, group_id=game.group_id)
            for member in self._members_of(zone):
                self._outbox.send(member.address, removal)
        self._forget_group(game)

    def _forget_group(self, group: Group) -> None:
        self._groups.pop(group.group_id, None)
        for player in self._players_by_id.values():
            player.group_ids.discard(group.group_id)

    def _find_or_open_room(self, group_id: int) -> Group:
        group = self._groups.get(group_id)
        if group is None:
            group = Group(announcement=room_announcement(group_id), is_room=True)
            self._groups[group_id] = group
            log.info("room %d opened", group_id)
        return group

    def _relay_to_everyone_who_sees(self, group: Group, message: EncodableMessage, sender_address: Address) -> None:
        """Send a change of a group to its members and to the members of the zone it is shown in,
        once each, except to the client the change came from."""
        receivers: dict[Address, Player] = {}
        zone = self._groups.get(group.parent_id)
        for watched_group in (group, zone):
            if watched_group is None:
                continue
            for member in self._members_of(watched_group):
                receivers[member.address] = member
        for receiver_address in receivers:
            if receiver_address != sender_address:
                self._outbox.send(receiver_address, message)

    def _members_of(self, group: Group) -> list[Player]:
        return [self._players_by_id[member_id] for member_id in group.member_ids if member_id in self._players_by_id]

    def _find_logged_on_player(self, client_address: Address) -> Player | None:
        player = self._players_by_address.get(client_address)
        if player is None:
            log.warning("%s: lobby message from a client that has not logged on", client_address)
        return player


def room_announcement(group_id: int) -> GroupAnnouncement:
    """The announcement for a room or zone the server made on a client's first join."""
    no_flags = 0
    return GroupAnnouncement(
        message_type=MessageType.ADD_GROUP_TO_GROUP,
        parent_id=JOINED_GROUP_PARENT_ID,
        group_id=group_id,
        flags=no_flags,
        unknown_value=0,
        name=f"Room {group_id}",
        second_text="",
        third_text="",
    )


def member_entry(group_id: int, player: Player) -> AddPlayerToGroup:
    """A group member as other clients need it: with the address the server sees the player at,
    because they send chat straight to that address."""
    return AddPlayerToGroup(
        group_id=group_id,
        player_id=player.player_id,
        player_address=player.address,
        player_name=player.name,
    )


def with_real_host_address(announcement: GroupAnnouncement, host_client_address: Address) -> GroupAnnouncement:
    """A host announces its game with its own address, which reads 0.0.0.0 on the host. Put in the
    IP the server sees the host at, so other players can connect DirectPlay to it, and keep the
    host's port."""
    game_block = announcement.game_block
    if game_block is None:
        return announcement
    host, port = game_block.host_address
    if host != UNKNOWN_HOST:
        return announcement
    real_host_ip = host_client_address[0]
    return replace(announcement, game_block=replace(game_block, host_address=(real_host_ip, port)))
