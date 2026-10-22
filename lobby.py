"""Lobby state and the answers the game expects (docs/s3-lobby-protocol.md, "Lobby layer").

The server plays both roles the game knows: index server (patch check and the
list of lobby servers) and lobby server (logon, rooms, games).
"""

import logging

from wire import (
    ADD_GROUP_TO_GROUP,
    ADD_GROUP_TO_GROUP_2,
    ADD_PLAYER_TO_GROUP,
    CHAT_COMMAND,
    CHAT_COMMAND_ANSWER,
    CHAT_MESSAGE,
    CHAT_MESSAGE_2,
    CHECK_FILE_VERSION,
    GAME_STATUS,
    GET_IMAGE_LIST,
    GET_LOBBY_SERVER_LIST,
    GET_TEXT_MESSAGE,
    IMAGE_LIST,
    LOBBY_SERVER_LIST,
    LOGON,
    LOGOUT,
    MESSAGE_NAMES,
    OWN_PLAYER,
    REMOVE_GROUP_FROM_GROUP,
    REMOVE_PLAYER_FROM_GROUP,
    RESULT,
    SOCKADDR_SIZE,
    TEXT_MESSAGE,
    UPDATE_GROUP,
    UPDATE_GROUP_2,
    BodyBuilder,
    Message,
    ProtocolError,
    decode_sockaddr,
    encode_sockaddr,
)

log = logging.getLogger("s3lobby")

# Result codes the client's handler switches on (lobbyclient.cpp 0x5cd820).
RESULT_NO_PATCH = 9
RESULT_LOGON_OK = 0x66

# Rooms are made when a client first joins them. Seen from the real client: after logon it
# joins group 3 and groups 30..38, one per bit of the registry value GameZoneSelection (511).
TOP_LEVEL_PARENT = 0
# A host opens its game with group id 0xffffffff and leaves the id to the server.
SERVER_ASSIGNS_GROUP_ID = 0xFFFFFFFF
FIRST_GAME_ID = 0x10000

# Group flags that append optional parts to AddGroupToGroup (Lobby.cpp 0x5e2d60, 0x5e3b40).
GROUP_FLAG_GAME_BLOCK = 0x400
GROUP_FLAG_DESCRIPTION = 0x40000
GAME_BLOCK_SIZES = {ADD_GROUP_TO_GROUP: 0x2C, ADD_GROUP_TO_GROUP_2: 0x34}
LOGON_PROTOCOL_VERSION = 14


class Player:
    def __init__(self, player_id, name, address):
        self.player_id = player_id
        self.name = name
        self.address = address
        self.group_ids = set()


class Group:
    """A room or a game. Games keep the client's own AddGroupToGroup body to relay it unchanged."""

    def __init__(self, group_id, parent_id, announce_type, announce_body, is_room=False):
        self.group_id = group_id
        self.is_room = is_room
        self.parent_id = parent_id
        self.announce_type = announce_type
        self.announce_body = announce_body
        self.member_ids = []

    def announcement(self, parent_id):
        """The group's AddGroupToGroup as seen under `parent_id` (0 tells the receiver it joined)."""
        return Message(self.announce_type, replace_u32(self.announce_body, 0, parent_id))


class Lobby:
    def __init__(self, server_name, send, server_address_for):
        """`send(address, message)` queues a message to a client; `server_address_for(address)` is
        the "host:port" under which that client reaches this server."""
        self.server_name = server_name
        self.send = send
        self.server_address_for = server_address_for
        self.players_by_address = {}
        self.next_player_id = 1
        self.groups = {}
        self.next_game_id = FIRST_GAME_ID

    def handle(self, address, message):
        handler = self.HANDLERS.get(message.message_type)
        name = MESSAGE_NAMES.get(message.message_type, "unknown")
        if handler is None:
            log.warning("%s: no handler for %#04x %s (%s)", address, message.message_type, name, message.body.hex())
            return
        try:
            handler(self, address, message)
        except ProtocolError as error:
            log.warning("%s: bad %s: %s (%s)", address, name, error, message.body.hex())

    def disconnect(self, address):
        player = self.players_by_address.pop(address, None)
        if player is None:
            return
        log.info("player %d %r left", player.player_id, player.name)
        for group_id in list(player.group_ids):
            self._leave_group(player, group_id, is_player_told=False)

    # Index server role.

    def _on_check_file_version(self, address, message):
        reader = message.reader()
        version, edition, extra = reader.u32(), reader.u32(), reader.u32()
        log.info("%s: CheckFileVersion version=%d edition=%d extra=%d -> no patch", address, version, edition, extra)
        self.send(address, BodyBuilder().u32(RESULT_NO_PATCH).u32(0).build(RESULT))

    def _on_get_lobby_server_list(self, address, message):
        server_address = self.server_address_for(address)
        log.info("%s: GetLobbyServerList -> %s %r", address, server_address, self.server_name)
        answer = BodyBuilder().string(server_address).string(self.server_name)
        self.send(address, answer.build(LOBBY_SERVER_LIST))

    # Lobby server role.

    def _on_logon(self, address, message):
        reader = message.reader()
        protocol_version = reader.u32()
        name = reader.string()
        reader.string()  # password: everyone is welcome on a LAN server
        flags = reader.u32()
        serial = reader.string()
        if protocol_version != LOGON_PROTOCOL_VERSION:
            log.warning("%s: logon protocol version %d, expected %d", address, protocol_version, LOGON_PROTOCOL_VERSION)
        self.disconnect(address)
        player = Player(self.next_player_id, name, address)
        self.next_player_id += 1
        self.players_by_address[address] = player
        log.info("%s: logon %r flags=%#x serial=%r -> player %d", address, name, flags, serial, player.player_id)
        # Result first: it sets the client's lpServer, which the own-player handler asserts.
        self.send(address, BodyBuilder().u32(RESULT_LOGON_OK).u32(0).build(RESULT))
        self.send(address, BodyBuilder().u32(player.player_id).build(OWN_PLAYER))

    def _on_logout(self, address, message):
        self.disconnect(address)

    def _on_add_player_to_group(self, address, message):
        player = self._player_at(address)
        reader = message.reader()
        group_id = reader.u32()
        reader.u32()  # the client repeats its own player id
        if player is None or group_id == 0:
            return
        group = self._group_or_new_room(group_id)
        if player.player_id in group.member_ids:
            return
        if group.is_room:
            # Back from a match the client just rejoins its rooms; it never says it left the game.
            self._leave_games(player)
        log.info("player %d %r joins group %d", player.player_id, player.name, group_id)
        self._send_group_contents(player, group)
        for member in self._members(group):
            self.send(member.address, add_player_message(group_id, player))
        group.member_ids.append(player.player_id)
        player.group_ids.add(group_id)

    def _on_remove_player_from_group(self, address, message):
        player = self._player_at(address)
        group_id = message.reader().u32()
        if player is not None:
            self._leave_group(player, group_id)

    def _on_add_group(self, address, message):
        player = self._player_at(address)
        reader = message.reader()
        parent_id = reader.u32()
        group_id = reader.u32()
        parent = self.groups.get(parent_id)
        if player is None or parent is None or group_id == 0 or group_id in self.groups:
            log.warning("%s: ignored new group %d under %d", address, group_id, parent_id)
            return
        body = fill_in_game_host(message, address)
        if group_id == SERVER_ASSIGNS_GROUP_ID:
            group_id = self.next_game_id
            self.next_game_id += 1
            body = replace_u32(body, 4, group_id)
        game = Group(group_id, parent_id, message.message_type, body)
        self.groups[group_id] = game
        log.info("player %d %r opens game %d in group %d", player.player_id, player.name, group_id, parent_id)
        # The host's client does not count itself in the game (CreateGame 0x5ca7d0); it joins
        # when the server announces the game to it with parent 0, like any other join.
        self._send_group_contents(player, game)
        game.member_ids.append(player.player_id)
        player.group_ids.add(group_id)
        for member in self._members(parent):
            if member is not player:
                self.send(member.address, game.announcement(parent_id))

    def _on_update_group(self, address, message):
        group_id = message.reader().u32()
        group = self.groups.get(group_id)
        if group is None:
            return
        self._relay_to_watchers(address, group, message)

    def _on_remove_group(self, address, message):
        reader = message.reader()
        reader.u32()
        group_id = reader.u32()
        group = self.groups.get(group_id)
        if group is None or group.is_room:
            return
        self._relay_to_watchers(address, group, message)
        self._forget_group(group)

    def _on_chat(self, address, message):
        # Room and private chat go from client to client; the server is not a member.
        reader = message.reader()
        group_id = reader.u32()
        reader.u32()  # sender
        if message.message_type == CHAT_MESSAGE_2:
            reader.u32()  # kind: 0 normal, 1 whisper
        log.info("%s: chat to group %d: %r", address, group_id, reader.string())

    def _on_chat_command(self, address, message):
        reader = message.reader()
        group_id = reader.u32()
        reader.u32()
        command = reader.string()
        answer = BodyBuilder().u32(group_id).u32(0).u32(1).string(f"Unknown command: {command}")
        self.send(address, answer.build(CHAT_COMMAND_ANSWER))

    def _on_get_image_list(self, address, message):
        group_id = message.reader().u32()
        self.send(address, BodyBuilder().u32(group_id).u32(0).build(IMAGE_LIST))

    def _on_get_text_message(self, address, message):
        text_id = message.reader().u32()
        self.send(address, BodyBuilder().u32(text_id).string("").u32(0).build(TEXT_MESSAGE))

    def _on_game_status(self, address, message):
        log.info("%s: GameStatus %s", address, message.body.hex())

    HANDLERS = {
        CHECK_FILE_VERSION: _on_check_file_version,
        GET_LOBBY_SERVER_LIST: _on_get_lobby_server_list,
        LOGON: _on_logon,
        LOGOUT: _on_logout,
        ADD_PLAYER_TO_GROUP: _on_add_player_to_group,
        REMOVE_PLAYER_FROM_GROUP: _on_remove_player_from_group,
        ADD_GROUP_TO_GROUP: _on_add_group,
        ADD_GROUP_TO_GROUP_2: _on_add_group,
        UPDATE_GROUP: _on_update_group,
        UPDATE_GROUP_2: _on_update_group,
        REMOVE_GROUP_FROM_GROUP: _on_remove_group,
        CHAT_MESSAGE: _on_chat,
        CHAT_MESSAGE_2: _on_chat,
        CHAT_COMMAND: _on_chat_command,
        GET_IMAGE_LIST: _on_get_image_list,
        GET_TEXT_MESSAGE: _on_get_text_message,
        GAME_STATUS: _on_game_status,
    }

    def _send_group_contents(self, player, group):
        """What a client needs on joining: the group (parent 0), every member including itself, and the games inside."""
        self.send(player.address, group.announcement(TOP_LEVEL_PARENT))
        for member in self._members(group):
            self.send(player.address, add_player_message(group.group_id, member))
        self.send(player.address, add_player_message(group.group_id, player))
        for child in self.groups.values():
            if child.parent_id == group.group_id:
                self.send(player.address, child.announcement(group.group_id))

    def _leave_games(self, player):
        """Take a player back from a match out of its game, without telling the player: after a
        match the client may still list the game, but after "Zurück" in the game setup it has
        already dropped it, and a RemovePlayerFromGroup for a group it does not list makes the
        handler throw a sync error (0x5d6af0) and the game hangs."""
        for group_id in list(player.group_ids):
            group = self.groups.get(group_id)
            if group is not None and not group.is_room:
                self._leave_group(player, group_id, is_player_told=False)

    def _group_or_new_room(self, group_id):
        group = self.groups.get(group_id)
        if group is None:
            group = make_room(group_id, f"Room {group_id}")
            self.groups[group_id] = group
            log.info("room %d opened", group_id)
        return group

    def _leave_group(self, player, group_id, is_player_told=True):
        """Take a player out of a group. The leaver is told as well: the client keeps a group on its
        joined list until the server confirms with RemovePlayerFromGroup for its own player id
        (handler 0x5d6af0, 0x5d721e), and rejoining a group still on that list drops the connection."""
        group = self.groups.get(group_id)
        player.group_ids.discard(group_id)
        if group is None or player.player_id not in group.member_ids:
            return
        group.member_ids.remove(player.player_id)
        log.info("player %d %r leaves group %d", player.player_id, player.name, group_id)
        leave = BodyBuilder().u32(group_id).u32(player.player_id).build(REMOVE_PLAYER_FROM_GROUP)
        for member in self._members(group):
            self.send(member.address, leave)
        if is_player_told:
            self.send(player.address, leave)
        is_empty_game = not group.member_ids and not group.is_room
        if is_empty_game:
            self._close_game(group)

    def _close_game(self, game):
        parent = self.groups.get(game.parent_id)
        if parent is not None:
            removal = BodyBuilder().u32(game.parent_id).u32(game.group_id).build(REMOVE_GROUP_FROM_GROUP)
            for member in self._members(parent):
                self.send(member.address, removal)
        self._forget_group(game)

    def _forget_group(self, group):
        self.groups.pop(group.group_id, None)
        for player in self.players_by_address.values():
            player.group_ids.discard(group.group_id)

    def _relay_to_watchers(self, sender_address, group, message):
        """Send a group change to everyone who sees the group: its members and the members of its room."""
        watchers = {}
        parent = self.groups.get(group.parent_id)
        for owner in (group, parent):
            if owner is None:
                continue
            for member in self._members(owner):
                watchers[member.address] = member
        for watcher_address in watchers:
            if watcher_address != sender_address:
                self.send(watcher_address, message)

    def _members(self, group):
        by_id = {player.player_id: player for player in self.players_by_address.values()}
        return [by_id[member_id] for member_id in group.member_ids if member_id in by_id]

    def _player_at(self, address):
        player = self.players_by_address.get(address)
        if player is None:
            log.warning("%s: message from a client that has not logged on", address)
        return player


def make_room(room_id, room_name):
    body = BodyBuilder().u32(TOP_LEVEL_PARENT).u32(room_id).u32(0).u32(0)
    body.string(room_name).string("").string("")
    return Group(room_id, TOP_LEVEL_PARENT, ADD_GROUP_TO_GROUP, body.build(ADD_GROUP_TO_GROUP).body, is_room=True)


def add_player_message(group_id, player):
    """AddPlayerToGroup with the address the server saw, so other clients can reach the player directly."""
    body = BodyBuilder().u32(group_id).u32(player.player_id).sockaddr(player.address)
    return body.string(player.name).string("").build(ADD_PLAYER_TO_GROUP)


def fill_in_game_host(message, sender_address):
    """The host announces its game with its own socket address, which reads 0.0.0.0 on the
    host itself; put in the address the server saw, keeping the host's port."""
    reader = message.reader()
    reader.u32()
    reader.u32()
    flags = reader.u32()
    reader.u32()
    for _ in range(3):
        reader.string()
    has_game_block = flags & GROUP_FLAG_GAME_BLOCK
    if not has_game_block:
        return message.body
    sockaddr_offset = reader.position
    host, port = decode_sockaddr(reader.raw(SOCKADDR_SIZE))
    if host != "0.0.0.0":
        return message.body
    patched = encode_sockaddr((sender_address[0], port))
    return message.body[:sockaddr_offset] + patched + message.body[sockaddr_offset + SOCKADDR_SIZE:]


def replace_u32(body, offset, value):
    return body[:offset] + value.to_bytes(4, "little") + body[offset + 4:]
