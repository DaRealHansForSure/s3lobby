"""The lobby: players, groups, and the answers the game expects to every request.

The server plays both roles the game knows (docs/protocol.md, "Lobby layer"):
- index server: the patch check and the list of lobby servers, asked first;
- lobby server: logon, the lobby tabs and game zones, and the games and chat groups players open.

In this protocol rooms, zones, chat groups and games are all groups (see groups.py). A client joins
a group with AddPlayerToGroup; the server announces the group to it with parent 0 ("you are in"),
lists its members, and shows it the groups listed inside. The match itself runs over DirectPlay
between the players, never through this server.

The server mirrors what each client sees. A client drops the connection when it is shown a group
it already sees in the same parent (0x5db9ad), or told to remove one it does not see (0x5dbcc3), or
told that a player left a group it is not in (0x5d6af0). So every player has a set of the groups it
sees, as (parent id, group id) pairs, and every announcement and removal goes through it.

Several more rules exist because the real client breaks without them. Each one is explained where
it is applied, with the address in S3.EXE that shows why.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

from s3lobby.constants import (
    BANNER_URL_PREFIX,
    CHAT_THEME_GROUP_ID_BASE,
    CHAT_ZONE_GROUP_ID,
    DEFAULT_CHAT_THEME_DESCRIPTION,
    DEFAULT_CHAT_THEME_NAME,
    FIRST_GAME_GROUP_ID,
    GAME_ZONE_NAMES,
    HOME_GROUP_ID,
    JOINED_GROUP_PARENT_ID,
    LOBBY_TAB_GROUP_NAMES,
    LOGON_PROTOCOL_VERSION,
    MAX_CHAT_COMMAND_LENGTH,
    MAX_OPEN_GROUPS_PER_PLAYER,
    MAX_TEXT_MESSAGE_BYTES,
    NEWS_TEXT_ID_BY_AUDIENCE,
    PRIVATE_CHAT_GROUP_ID,
    SERVER_ASSIGNS_GROUP_ID,
    TEXT_ENCODING,
    UNKNOWN_TEXT_TITLE,
    WARNING_INTERVAL_SECONDS,
    GroupFlag,
    MessageType,
    ResultCode,
)
from s3lobby.database import Clan, LobbyDatabase, Setting
from s3lobby.groups import (
    Group,
    GroupKind,
    check_client_group_texts,
    game_block_with_real_host,
    kind_of_new_group,
    room_announcement,
    with_real_host_address,
)
from s3lobby.guards import ThrottledLogger
from s3lobby.logon import AcceptedLogon, LobbyOccupancy, LogonCheck, RefusedLogon
from s3lobby.messages import (
    AddPlayerToGroup,
    AddPlayerToGroupWithClan,
    ChatCommand,
    ChatCommandAnswer,
    ChatMessage,
    CheckFileVersion,
    EncodableMessage,
    GameStatus,
    GetImageList,
    GetTextMessage,
    GroupAnnouncement,
    GroupUpdate,
    ImageEntry,
    ImageList,
    LobbyServerEntry,
    LobbyServerList,
    Logon,
    OwnPlayer,
    OwnPlayerWithClan,
    RemoveGroupFromGroup,
    RemovePlayerFromGroup,
    Result,
    ServerStatistics,
    TextIdList,
    TextMessage,
)
from s3lobby.wire import Address, ProtocolError, RawMessage

log = logging.getLogger(__name__)

# A group shown inside a parent, as the client keeps it: (parent id, group id).
VisibleGroup = tuple[int, int]


class LobbyOutbox(Protocol):
    """What the lobby needs from the network side: a way to reach a client, and the address under
    which that client reaches this server."""

    def send(self, client_address: Address, message: EncodableMessage) -> None: ...

    def server_address_text_for(self, client_address: Address) -> str: ...


@dataclass
class Player:
    """A logged on player.

    The player id is the account id, so it stays the same across logons. The address is where the
    player's datagrams come from. shown_clan is set when the player logged on with the clan box
    ticked; the other players then see the clan next to the name. group_ids are the groups the
    player is in; visible_groups the groups its client shows inside one of those.
    """

    player_id: int
    name: str
    address: Address
    serial_number: str
    is_admin: bool
    session_id: int
    shown_clan: Clan | None = None
    group_ids: set[int] = field(default_factory=set)
    visible_groups: set[VisibleGroup] = field(default_factory=set)


class Lobby:
    """Keeps the lobby's state and answers every lobby message a client sends."""

    def __init__(self, server_name: str, outbox: LobbyOutbox, database: LobbyDatabase) -> None:
        self._server_name = server_name
        self._outbox = outbox
        self._database = database
        self._logon_check = LogonCheck(database)
        self._players_by_address: dict[Address, Player] = {}
        self._players_by_id: dict[int, Player] = {}
        self._groups: dict[int, Group] = {}
        self._next_player_group_id = FIRST_GAME_GROUP_ID
        # Warnings about a client's messages are throttled per client, so a client that sends a
        # stream of bad messages within its rate limit cannot flood the log.
        self._warnings = ThrottledLogger(log, WARNING_INTERVAL_SECONDS)
        self._handlers: dict[int, Callable[[Address, RawMessage], None]] = {
            MessageType.CHECK_FILE_VERSION: self._answer_patch_check,
            MessageType.GET_LOBBY_SERVER_LIST: self._answer_lobby_server_list,
            MessageType.LOGON: self._log_on_player,
            MessageType.LOGOUT: self._log_out_player,
            MessageType.ADD_PLAYER_TO_GROUP: self._join_group_on_request,
            MessageType.REMOVE_PLAYER_FROM_GROUP: self._leave_group_on_request,
            MessageType.ADD_GROUP_TO_GROUP: self._open_or_list_group_on_request,
            MessageType.ADD_GROUP_TO_GROUP_2: self._open_or_list_group_on_request,
            MessageType.UPDATE_GROUP: self._update_group_on_request,
            MessageType.UPDATE_GROUP_2: self._update_group_on_request,
            MessageType.REMOVE_GROUP_FROM_GROUP: self._unlist_group_on_request,
            MessageType.CHAT_MESSAGE: self._log_chat_message,
            MessageType.CHAT_MESSAGE_2: self._log_chat_message,
            MessageType.CHAT_COMMAND: self._answer_chat_command,
            MessageType.GET_IMAGE_LIST: self._answer_image_list,
            MessageType.GET_TEXT_MESSAGE: self._answer_text_message,
            MessageType.GAME_STATUS: self._log_game_status,
        }
        self._open_built_in_rooms()

    def handle_message(self, client_address: Address, message: RawMessage) -> None:
        """Act on one lobby message. A malformed message is logged and skipped; the rest of the
        client's block is still handled, as the game handles each message on its own."""
        handler = self._handlers.get(message.message_type)
        if handler is None:
            self._warn(client_address, "%s: no handler for %s (%d bytes)", client_address, message.type_name, len(message.body))
            return
        try:
            handler(client_address, message)
        except ProtocolError as error:
            self._warn(client_address, "%s: refused %s: %s", client_address, message.type_name, error)
        except Exception:
            # A bug, not a broken message: the next message of the block still gets its turn.
            self._warnings.error_with_traceback(
                client_address, time.monotonic(), "%s: internal error while handling %s", client_address, message.type_name
            )

    def forget_client(self, client_address: Address) -> None:
        """The client's connection is gone: take its player out of every group. Nobody can tell the
        player anything any more, so only the remaining members hear about it."""
        player = self._players_by_address.pop(client_address, None)
        if player is None:
            return
        del self._players_by_id[player.player_id]
        self._database.record_logout(player.session_id)
        log.info("player %d %r left the lobby", player.player_id, player.name)
        for group_id in list(player.group_ids):
            self._remove_player_from_group(player, group_id, is_player_told=False)
        self._send_statistics_to_home()

    def forget_quiet_keys(self, now_seconds: float) -> None:
        """Let the per client warning and logon limits forget clients that went quiet."""
        self._warnings.forget_quiet_keys(now_seconds)
        self._logon_check.forget_quiet_keys(now_seconds)

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
        logon = Logon.decode(message)
        if logon.protocol_version != LOGON_PROTOCOL_VERSION:
            self._warn(
                client_address,
                "%s: logon protocol version %d, this server knows %d",
                client_address,
                logon.protocol_version,
                LOGON_PROTOCOL_VERSION,
            )
        # A logon on an address that already has a player starts that player over.
        self.forget_client(client_address)
        decision = self._logon_check.decide(logon, client_address[0], self._occupancy())
        if isinstance(decision, RefusedLogon):
            log.info("%s: logon of %r refused: %s", client_address, logon.player_name, decision.reason)
            self._outbox.send(client_address, Result(decision.result_code))
            return
        player = self._admit_player(client_address, logon, decision)
        log.info(
            "%s: %r logged on as player %d%s",
            client_address,
            player.name,
            player.player_id,
            f" of clan {player.shown_clan.name!r}" if player.shown_clan else "",
        )
        # Order matters: the Result makes this server the client's lobby server, and the OwnPlayer
        # handler asserts exactly that (handler 0x5d7300). Joining a group later uses the own player
        # without a check (0x5ca46b), so OwnPlayer is not optional either.
        self._outbox.send(client_address, Result(ResultCode.LOGON_ACCEPTED))
        self._outbox.send(client_address, own_player_message(player))
        self._send_statistics_to_home()

    def _admit_player(self, client_address: Address, logon: Logon, decision: AcceptedLogon) -> Player:
        account = decision.account
        player = Player(
            player_id=account.account_id,
            name=account.name,
            address=client_address,
            serial_number=logon.serial_number,
            is_admin=account.is_admin,
            session_id=self._database.record_logon(account.account_id),
            shown_clan=decision.shown_clan,
        )
        self._players_by_address[client_address] = player
        self._players_by_id[player.player_id] = player
        return player

    def _occupancy(self) -> LobbyOccupancy:
        players = list(self._players_by_id.values())
        return LobbyOccupancy(
            player_count=len(players),
            casefolded_player_names=frozenset(player.name.casefold() for player in players),
            serial_numbers=frozenset(player.serial_number for player in players),
        )

    def _log_out_player(self, client_address: Address, message: RawMessage) -> None:
        self.forget_client(client_address)

    # Lobby server role: joining and leaving groups.

    def _join_group_on_request(self, client_address: Address, message: RawMessage) -> None:
        # Right after the logon the client joins its lobby tab (1 to 4) and one zone per bit of its
        # GameZoneSelection setting (30 to 38 with the default 511), all in one block, without
        # asking for a list of groups first. A joiner of a game or chat group knows its id from the
        # zone or theme it saw it in; for a chat group the extra text carries the password.
        request = AddPlayerToGroup.decode(message)
        player = self._find_logged_on_player(client_address)
        if player is None or request.group_id == PRIVATE_CHAT_GROUP_ID:
            return
        group = self._groups.get(request.group_id)
        if group is None:
            self._refuse_join(player, request.group_id, ResultCode.GROUP_CHANGE_FAILED, "no such group")
            return
        if player.player_id in group.member_ids:
            return
        if group.password and request.extra_text != group.password:
            self._refuse_join(player, group.group_id, ResultCode.WRONG_GROUP_PASSWORD, "wrong password")
            return
        if group.is_full:
            self._refuse_join(player, group.group_id, ResultCode.GROUP_CHANGE_FAILED, "group is full")
            return
        if group.group_id in LOBBY_TAB_GROUP_NAMES:
            # Back from a match, the client joins its lobby tab and zones again but never says it
            # left its game; the tab comes first in that block (seen in a run). Zone joins alone do
            # not count: a host still setting up its game may list it in further zones.
            self._take_player_out_of_games(player)
        self._add_player_to_group(player, group)

    def _leave_group_on_request(self, client_address: Address, message: RawMessage) -> None:
        request = RemovePlayerFromGroup.decode(message)
        player = self._find_logged_on_player(client_address)
        if player is not None:
            self._remove_player_from_group(player, request.group_id, is_player_told=True)

    def _refuse_join(self, player: Player, group_id: int, result_code: ResultCode, reason: str) -> None:
        log.info("player %d %r cannot join group %d: %s", player.player_id, player.name, group_id, reason)
        self._outbox.send(player.address, Result(result_code, value=group_id))

    # Lobby server role: games and chat groups players open.

    def _open_or_list_group_on_request(self, client_address: Address, message: RawMessage) -> None:
        """AddGroupToGroup from a client: open a new game or chat group under a parent the player is
        in, or, for a game the player is in, list it in one more zone ("Show game in GameZone":
        AddGroupToGroup with the game's id, flags 0 and empty texts, 0x45637f)."""
        request = GroupAnnouncement.decode(message)
        player = self._find_logged_on_player(client_address)
        if player is None:
            return
        parent = self._groups.get(request.parent_id)
        if parent is None:
            raise ProtocolError(f"group {request.group_id} under {request.parent_id}, which does not exist")
        existing_group = self._groups.get(request.group_id)
        if existing_group is not None:
            self._list_existing_game(player, existing_group, parent)
            return
        if player.player_id not in parent.member_ids:
            raise ProtocolError(f"new group under {request.parent_id}, a group the player is not in")
        self._open_player_group(player, request, parent)

    def _open_player_group(self, player: Player, request: GroupAnnouncement, parent: Group) -> None:
        if request.group_id not in (SERVER_ASSIGNS_GROUP_ID, PRIVATE_CHAT_GROUP_ID):
            raise ProtocolError(f"new group with id {request.group_id}; only the server picks ids")
        if request.group_id == PRIVATE_CHAT_GROUP_ID:
            # A private chat invitation goes from client to client; one sent here is a mistake.
            return
        check_client_group_texts(request)
        open_group_count = sum(1 for group in self._groups.values() if group.creator_player_id == player.player_id)
        if open_group_count >= MAX_OPEN_GROUPS_PER_PLAYER:
            self._refuse_join(player, request.group_id, ResultCode.GROUP_CHANGE_FAILED, "too many open groups")
            return

        group_id = self._next_player_group_id
        self._next_player_group_id += 1
        announcement = with_real_host_address(request, player.address)
        # The third text is the password of a chat group: kept here, never shown to anyone.
        announcement = replace(announcement, group_id=group_id, third_text="")
        group = Group(
            announcement=announcement,
            kind=kind_of_new_group(request),
            password=request.third_text,
            creator_player_id=player.player_id,
        )
        if group.kind == GroupKind.GAME:
            group.game_record_id = self._database.record_game_opened(announcement.name, player.player_id)
            self._send_statistics_to_home()
        self._groups[group_id] = group
        log.info(
            "player %d %r opens %s %d %r in group %d",
            player.player_id,
            player.name,
            group.kind.value,
            group_id,
            announcement.name,
            parent.group_id,
        )
        # The creator's client does not count itself in the new group (CreateGame 0x5ca7d0): it is
        # in once the server announces the group to it with parent 0, like after any other join.
        self._add_player_to_group(player, group)
        self._list_group_in(group, parent)

    def _list_existing_game(self, player: Player, group: Group, zone: Group) -> None:
        if player.player_id not in group.member_ids or group.kind != GroupKind.GAME:
            raise ProtocolError(f"listing group {group.group_id}, which is no game of this player")
        if not zone.is_room:
            raise ProtocolError(f"listing game {group.group_id} in group {zone.group_id}, which is no zone")
        if zone.group_id in group.listed_in_parent_ids:
            return
        log.info("player %d %r lists game %d in group %d", player.player_id, player.name, group.group_id, zone.group_id)
        self._list_group_in(group, zone)

    def _update_group_on_request(self, client_address: Address, message: RawMessage) -> None:
        """UpdateGroup from a member, usually the host setting up its game (mask 0x50: player count
        and description, 0x45506c). The server keeps the new state, so players who see the group
        later see it current, and passes the update on to everyone who sees the group inside a
        parent. Members of the group do not take updates for it (0x5d8de6), so they are skipped."""
        update = GroupUpdate.decode(message)
        player = self._find_logged_on_player(client_address)
        group = self._groups.get(update.group_id)
        if player is None or group is None or player.player_id not in group.member_ids:
            raise ProtocolError(f"update of group {update.group_id} by a player not in it")
        check_client_group_texts(update)
        update = replace(update, game_block=game_block_with_real_host(update.game_block, player.address))
        updated = update.applied_to(group.announcement)
        if updated.third_text:
            group.password = updated.third_text
        group.announcement = replace(updated, third_text="")
        public_update = replace(update, third_text="")
        for viewer in self._viewers_of(group):
            self._outbox.send(viewer.address, public_update)
        self._send_counters_to_viewers(group)

    def _unlist_group_on_request(self, client_address: Address, message: RawMessage) -> None:
        """RemoveGroupFromGroup from a member: at the start of the match the host takes its game
        out of every zone it was listed in (0x454c20). The game itself goes on for its players; it
        only disappears from the zone lists, and only for players who see it there (0x5d5828,
        0x5dbcc3: anything else drops their connection)."""
        request = RemoveGroupFromGroup.decode(message)
        player = self._find_logged_on_player(client_address)
        group = self._groups.get(request.group_id)
        if player is None or group is None or player.player_id not in group.member_ids:
            raise ProtocolError(f"unlisting group {request.group_id} by a player not in it")
        log.info(
            "player %d %r unlists group %d from group %d", player.player_id, player.name, group.group_id, request.parent_id
        )
        self._unlist_group_from(group, request.parent_id)

    # Chat and other requests.

    def _log_chat_message(self, client_address: Address, message: RawMessage) -> None:
        # Clients chat with each other directly; the server only sees lines meant for it.
        chat = ChatMessage.decode(message)
        log.info("%s: chat to group %d: %r", client_address, chat.group_id, chat.text)

    def _answer_chat_command(self, client_address: Address, message: RawMessage) -> None:
        command = ChatCommand.decode(message)
        if len(command.command) > MAX_CHAT_COMMAND_LENGTH:
            raise ProtocolError(f"chat command of {len(command.command)} characters")
        answer = ChatCommandAnswer(group_id=command.group_id, lines=(f"Unknown command: {command.command}",))
        self._outbox.send(client_address, answer)

    def _answer_image_list(self, client_address: Address, message: RawMessage) -> None:
        # Only the Home tab shows an image: the banner, when one is configured.
        request = GetImageList.decode(message)
        images = ()
        banner = self._configured_banner()
        if request.group_id == HOME_GROUP_ID and banner is not None:
            images = (banner,)
        self._outbox.send(client_address, ImageList(group_id=request.group_id, images=images))

    def _answer_text_message(self, client_address: Address, message: RawMessage) -> None:
        request = GetTextMessage.decode(message)
        self._outbox.send(client_address, self._text_message_for(request.text_id))

    def _text_message_for(self, text_id: int) -> TextMessage:
        """The text with this id: the news for one audience, or a placeholder for unknown ids,
        always with a title, since a text without one leaves the client waiting for good."""
        for audience, news_text_id in NEWS_TEXT_ID_BY_AUDIENCE.items():
            news = self._database.find_news(audience)
            if text_id == news_text_id and news is not None:
                return text_message(text_id, news.title, news.body)
        return TextMessage(text_id=text_id, title=UNKNOWN_TEXT_TITLE)

    def _log_game_status(self, client_address: Address, message: RawMessage) -> None:
        status = GameStatus.decode(message)
        log.info("%s: game status for group %d: %s", client_address, status.group_id, status.rest_of_body.hex())

    # Membership.

    def _add_player_to_group(self, player: Player, group: Group) -> None:
        """Put a player into a group and tell everyone concerned: the player gets the group, its
        members and what is listed in it; the members get the newcomer; whoever sees the group in
        a parent gets its new counters."""
        log.info("player %d %r joins group %d", player.player_id, player.name, group.group_id)
        if group.group_id == HOME_GROUP_ID:
            group.announcement = replace(group.announcement, flags=self._home_flags())
        if group.group_id == CHAT_ZONE_GROUP_ID:
            self._sync_chat_themes()
        self._outbox.send(player.address, group.announcement.under_parent(JOINED_GROUP_PARENT_ID))
        for member in self._members_of(group):
            self._outbox.send(player.address, member_entry(group.group_id, member))
            self._outbox.send(member.address, member_entry(group.group_id, player))
        self._outbox.send(player.address, member_entry(group.group_id, player))
        group.member_ids.append(player.player_id)
        player.group_ids.add(group.group_id)
        for child in self._groups_listed_in(group.group_id):
            self._show_group(player, child, group.group_id)
        self._send_counters_to_viewers(group)
        if group.group_id == HOME_GROUP_ID:
            self._send_home_page(player)

    # The Home tab.

    def _send_home_page(self, player: Player) -> None:
        """What the Home tab shows: the statistics table, and the ids of the news texts there are.
        The client then picks the id for its language and asks for that text."""
        self._outbox.send(player.address, self._statistics())
        news_text_ids = []
        for audience, text_id in NEWS_TEXT_ID_BY_AUDIENCE.items():
            if self._database.find_news(audience) is not None:
                news_text_ids.append(text_id)
        self._outbox.send(player.address, TextIdList(group_id=HOME_GROUP_ID, text_ids=tuple(news_text_ids)))

    def _send_statistics_to_home(self) -> None:
        home = self._groups[HOME_GROUP_ID]
        members = self._members_of(home)
        if not members:
            return
        statistics = self._statistics()
        for member in members:
            self._outbox.send(member.address, statistics)

    def _statistics(self) -> ServerStatistics:
        numbers = self._database.collect_statistics()
        return ServerStatistics(
            players_online=numbers.players_online,
            players_last_hour=numbers.players_last_hour,
            players_last_day=numbers.players_last_day,
            games_open=numbers.games_open,
            games_last_hour=numbers.games_last_hour,
            games_last_day=numbers.games_last_day,
        )

    def _configured_banner(self) -> ImageEntry | None:
        url = self._database.get_setting(Setting.BANNER_URL)
        if not url.startswith(BANNER_URL_PREFIX):
            return None
        return ImageEntry(
            banner_id=self._database.get_integer_setting(Setting.BANNER_ID),
            banner_sub_id=self._database.get_integer_setting(Setting.BANNER_SUB_ID),
            url=url,
        )

    def _home_flags(self) -> int:
        """Home receives the statistics, and asks for its image list when a banner is configured.
        Worked out on every join, so a banner set while the server runs shows on the next visit."""
        flags = GroupFlag.RECEIVES_STATISTICS
        if self._configured_banner() is not None:
            flags |= GroupFlag.REQUESTS_IMAGES
        return flags

    def _remove_player_from_group(self, player: Player, group_id: int, is_player_told: bool) -> None:
        """Take a player out of a group and tell the remaining members.

        is_player_told sends the confirmation to the player as well. A client that asked to leave
        needs it: it keeps the group on its joined list until the server confirms (handler
        0x5d6af0, own player check at 0x5d721e), and joining that group again would then fail
        (assert "! vJoinedGroups.contains") and drop the connection. With the group, the client
        also drops what it saw listed inside it.
        """
        group = self._groups.get(group_id)
        player.group_ids.discard(group_id)
        player.visible_groups = {visible for visible in player.visible_groups if visible[0] != group_id}
        if group is None or player.player_id not in group.member_ids:
            return
        group.member_ids.remove(player.player_id)
        log.info("player %d %r leaves group %d", player.player_id, player.name, group_id)
        departure = RemovePlayerFromGroup(group_id=group_id, player_id=player.player_id)
        for member in self._members_of(group):
            self._outbox.send(member.address, departure)
        if is_player_told:
            self._outbox.send(player.address, departure)
        if not group.member_ids and not group.is_room:
            self._close_player_group(group)
            return
        self._send_counters_to_viewers(group)

    def _take_player_out_of_games(self, player: Player) -> None:
        """Take a player who is back from a match out of its game, without telling the player.

        After a played match the client may still list the game, but after "Zurück" in the game
        setup it has already dropped it, and a confirmation for a group it does not list makes its
        handler throw a sync error (0x5d6af0): the game hangs. Saying nothing is safe in both cases.
        """
        for group_id in list(player.group_ids):
            group = self._groups.get(group_id)
            if group is not None and group.kind == GroupKind.GAME:
                self._remove_player_from_group(player, group_id, is_player_told=False)

    # Listing groups inside parents, mirrored per player.

    def _list_group_in(self, group: Group, parent: Group) -> None:
        """Show a group in a parent to the parent's members who are not in the group themselves."""
        group.listed_in_parent_ids.add(parent.group_id)
        for member in self._members_of(parent):
            if member.player_id not in group.member_ids:
                self._show_group(member, group, parent.group_id)

    def _unlist_group_from(self, group: Group, parent_id: int) -> None:
        group.listed_in_parent_ids.discard(parent_id)
        for player in list(self._players_by_id.values()):
            self._hide_group(player, group.group_id, parent_id)

    def _close_player_group(self, group: Group) -> None:
        for parent_id in list(group.listed_in_parent_ids):
            self._unlist_group_from(group, parent_id)
        self._groups.pop(group.group_id, None)
        if group.game_record_id is not None:
            self._database.record_game_closed(group.game_record_id)
            self._send_statistics_to_home()
        log.info("%s %d %r closed", group.kind.value, group.group_id, group.announcement.name)

    def _show_group(self, player: Player, group: Group, parent_id: int) -> None:
        """Show a group inside a parent to one player, unless the player already sees it there."""
        visible = (parent_id, group.group_id)
        if visible in player.visible_groups:
            return
        player.visible_groups.add(visible)
        self._outbox.send(player.address, group.announcement.under_parent(parent_id))
        if not group.is_room:
            self._outbox.send(player.address, group.counters())

    def _hide_group(self, player: Player, group_id: int, parent_id: int) -> None:
        """Remove a group from a parent for one player, only if the player sees it there."""
        visible = (parent_id, group_id)
        if visible not in player.visible_groups:
            return
        player.visible_groups.discard(visible)
        self._outbox.send(player.address, RemoveGroupFromGroup(parent_id=parent_id, group_id=group_id))

    def _send_counters_to_viewers(self, group: Group) -> None:
        if group.is_room:
            return
        counters = group.counters()
        for viewer in self._viewers_of(group):
            self._outbox.send(viewer.address, counters)

    def _viewers_of(self, group: Group) -> list[Player]:
        """Players who see the group inside a parent; members of the group do not count."""
        viewers = []
        for player in self._players_by_id.values():
            sees_group = any(visible[1] == group.group_id for visible in player.visible_groups)
            if sees_group:
                viewers.append(player)
        return viewers

    def _groups_listed_in(self, parent_id: int) -> list[Group]:
        return [group for group in self._groups.values() if parent_id in group.listed_in_parent_ids]

    # The ChatZone tab.

    def _sync_chat_themes(self) -> None:
        """Bring the chat theme groups listed in ChatZone in line with the database: themes added
        while the server runs appear, removed ones disappear once nobody is in them. With no theme
        configured a default one is offered, since players open chat groups inside a theme."""
        chat_zone = self._groups[CHAT_ZONE_GROUP_ID]
        wanted = {}
        for theme in self._database.list_chat_themes():
            wanted[CHAT_THEME_GROUP_ID_BASE + theme.theme_id] = (theme.name, theme.description)
        if not wanted:
            wanted[CHAT_THEME_GROUP_ID_BASE] = (DEFAULT_CHAT_THEME_NAME, DEFAULT_CHAT_THEME_DESCRIPTION)

        for group in self._groups_listed_in(CHAT_ZONE_GROUP_ID):
            is_removed = group.group_id not in wanted
            if is_removed and not group.member_ids:
                self._unlist_group_from(group, CHAT_ZONE_GROUP_ID)
                self._groups.pop(group.group_id, None)
        for group_id, (name, description) in wanted.items():
            if group_id in self._groups:
                continue
            theme = Group(announcement=room_announcement(group_id, name, description_text=description), kind=GroupKind.ROOM)
            self._groups[group_id] = theme
            self._list_group_in(theme, chat_zone)

    # Built-in groups and lookups.

    def _open_built_in_rooms(self) -> None:
        """The lobby tabs and the game zones exist from the start, with the names the client's UI
        gives them. The client does not show these names, but they make logs and lists readable."""
        for group_id, name in {**LOBBY_TAB_GROUP_NAMES, **GAME_ZONE_NAMES}.items():
            self._groups[group_id] = Group(announcement=room_announcement(group_id, name), kind=GroupKind.ROOM)

    def _members_of(self, group: Group) -> list[Player]:
        return [self._players_by_id[member_id] for member_id in group.member_ids if member_id in self._players_by_id]

    def _find_logged_on_player(self, client_address: Address) -> Player | None:
        player = self._players_by_address.get(client_address)
        if player is None:
            self._warn(client_address, "%s: lobby message from a client that has not logged on", client_address)
        return player

    def _warn(self, client_address: Address, message: str, *arguments: object) -> None:
        self._warnings.warning(client_address, time.monotonic(), message, *arguments)


def own_player_message(player: Player) -> EncodableMessage:
    """OwnPlayer, or its clan variant when the player logged on as clan member. Either one, once."""
    if player.shown_clan is None:
        return OwnPlayer(player.player_id)
    return OwnPlayerWithClan(player.player_id, player.shown_clan.clan_id, player.shown_clan.name)


def member_entry(group_id: int, player: Player) -> EncodableMessage:
    """A group member as other clients need it: with the address the server sees the player at,
    because they send chat straight to that address, and with the clan when the player shows one."""
    if player.shown_clan is None:
        return AddPlayerToGroup(
            group_id=group_id,
            player_id=player.player_id,
            player_address=player.address,
            player_name=player.name,
        )
    return AddPlayerToGroupWithClan(
        group_id=group_id,
        player_id=player.player_id,
        player_address=player.address,
        player_name=player.name,
        clan_id=player.shown_clan.clan_id,
        clan_name=player.shown_clan.name,
    )


def text_message(text_id: int, title: str, body: str) -> TextMessage:
    """A text message with the body split into lines, cut to fit one unfragmented datagram."""
    lines = []
    used_bytes = len(title) + 1
    for line in body.splitlines():
        line_bytes = len(line.encode(TEXT_ENCODING, errors="replace")) + 1
        if used_bytes + line_bytes > MAX_TEXT_MESSAGE_BYTES:
            log.warning("text %#x %r cut to %d bytes", text_id, title, MAX_TEXT_MESSAGE_BYTES)
            break
        lines.append(line)
        used_bytes += line_bytes
    return TextMessage(text_id=text_id, title=title, lines=tuple(lines))
