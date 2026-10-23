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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol, TypeVar

from s3lobby.constants import (
    ANNOUNCEMENT_GROUP_ID,
    BANNER_URL_PREFIX,
    BOARD_CHAT_HINT_TEXT,
    BOARD_FALLBACK_LANGUAGE,
    BOARD_LANGUAGES,
    BOARD_LANGUAGE_GROUP_ID_BASE,
    BOARD_LANGUAGE_GROUP_PREFIX,
    BOARD_MESSAGE_TEXT_ID_BASE,
    BOARD_THEME_GROUP_ID_BASE,
    CHAT_THEME_GROUP_ID_BASE,
    CHAT_ZONE_GROUP_ID,
    DEFAULT_CHAT_THEME_DESCRIPTION,
    DEFAULT_CHAT_THEME_NAME,
    EMPTY_CHAT_GROUP_KEEP_SECONDS,
    FIRST_GAME_GROUP_ID,
    FIRST_PRINTABLE_CHARACTER,
    GAME_ZONE_NAMES,
    GROUP_UPDATES_PER_SECOND,
    GROUP_UPDATE_BURST,
    HOME_GROUP_ID,
    JOINED_GROUP_PARENT_ID,
    LADDER_NOT_SUPPORTED_TEXT,
    LOBBY_TAB_GROUP_NAMES,
    LOGON_PROTOCOL_VERSION,
    MAX_CHAT_COMMAND_LENGTH,
    MAX_OPEN_GROUPS_PER_PLAYER,
    MAX_TEXT_MESSAGE_BYTES,
    MESSAGE_ZONE_GROUP_ID,
    NEWS_AUDIENCE_GERMAN,
    NEWS_TEXT_ID_BY_AUDIENCE,
    NO_BANNER_ID,
    PASSWORD_CHAT_GROUP_PARENT_ID,
    PRIVATE_CHAT_GROUP_ID,
    SERVER_ASSIGNS_GROUP_ID,
    TEXT_ENCODING,
    UNKNOWN_TEXT_TITLE,
    WARNING_INTERVAL_SECONDS,
    WRONG_GROUP_PASSWORDS_PER_SECOND,
    WRONG_GROUP_PASSWORD_BURST,
    GroupFlag,
    MessageType,
    PlayerFlag,
    ResultCode,
)
from s3lobby.commands import ChatCommands, fit_answer
from s3lobby.database import (
    Clan,
    DatabaseError,
    LobbyDatabase,
    Setting,
    to_stored_time,
    utc_now,
)
from s3lobby.groups import (
    Group,
    GroupKind,
    check_client_group_texts,
    game_block_with_real_host,
    game_description_of,
    game_row_colour,
    is_ladder_game,
    kind_of_new_group,
    ladder_type_of,
    room_announcement,
    stored_game_announcement,
    with_real_host_address,
)
from s3lobby.guards import RateLimiter, ThrottledLogger
from s3lobby.ladder import report_can_count
from s3lobby.ladder_book import LadderBook
from s3lobby.ladder_texts import ENGLISH, GERMAN, LADDER_EVENT_MAX_AGE, LadderWriter, text_size
from s3lobby.live import LiveGroup, LiveLobbyState, LivePlayer
from s3lobby.logon import AcceptedLogon, LobbyOccupancy, LogonCheck, RefusedLogon
from s3lobby.messages import (
    AddPlayerToGroup,
    AddPlayerToGroupWithClan,
    AddPlayerToGroupWithFlags,
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
from s3lobby.reachability import Reachability
from s3lobby.wire import Address, ProtocolError, RawMessage, readable_text

log = logging.getLogger(__name__)

T = TypeVar("T")

# A group shown inside a parent, as the client keeps it: (parent id, group id).
VisibleGroup = tuple[int, int]


class LobbyOutbox(Protocol):
    """What the lobby needs from the network side: a way to reach a client, and the address under
    which that client reaches this server."""

    def send(self, client_address: Address, message: EncodableMessage) -> None:
        """Send one message to a client over its reliable connection."""

    def server_address_text_for(self, client_address: Address) -> str:
        """This server's address as the client should use it, as text for the lobby server list."""

    def close_connection(self, client_address: Address) -> None:
        """Close the connection to a client, as for a kick."""

    def player_logged_on(self, client_address: Address) -> None:
        """Tell the network side that the client at this address is now a logged on player."""

    def reachability_of(self, client_address: Address) -> Reachability:
        """What the reachability probe found out about the client at this address."""


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
    # The lobby_sessions row of this logon; None when the database failed to record it.
    session_id: int | None
    shown_clan: Clan | None = None
    logged_on_at: datetime = field(default_factory=utc_now)
    group_ids: set[int] = field(default_factory=set)
    visible_groups: set[VisibleGroup] = field(default_factory=set)
    # The chat theme the player was in last; a chat group with a password is listed there.
    last_chat_theme_id: int | None = None
    has_heard_board_chat_hint: bool = False


class Lobby:
    """Keeps the lobby's state and answers every lobby message a client sends."""

    def __init__(
        self, server_name: str, outbox: LobbyOutbox, database: LobbyDatabase, is_development_mode: bool = False
    ) -> None:
        """Set up the message handlers and the built-in rooms. is_development_mode is passed to the
        logon check (see LogonCheck)."""
        self._server_name = server_name
        self._outbox = outbox
        self._database = database
        self._logon_check = LogonCheck(database, is_development_mode=is_development_mode)
        self._ladder_book = LadderBook(database)
        self._ladder_writer = LadderWriter(database, self._ladder_book)
        self._chat_commands = ChatCommands(
            database, self._online_player_names, self.announce, self.kick_player, outbox.reachability_of
        )
        self._players_by_address: dict[Address, Player] = {}
        self._players_by_id: dict[int, Player] = {}
        self._groups: dict[int, Group] = {}
        self._next_player_group_id = FIRST_GAME_GROUP_ID
        self._wrong_group_passwords = RateLimiter(WRONG_GROUP_PASSWORDS_PER_SECOND, WRONG_GROUP_PASSWORD_BURST)
        self._group_updates = RateLimiter(GROUP_UPDATES_PER_SECOND, GROUP_UPDATE_BURST)
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
            MessageType.GAME_STATUS: self._store_game_status,
        }
        self._open_built_in_rooms()

    def handle_message(self, client_address: Address, message: RawMessage) -> None:
        """Act on one lobby message. A malformed message is logged and skipped; the rest of the
        client's block is still handled, as the game handles each message on its own."""
        handler = self._handlers.get(message.message_type)
        if handler is None:
            self._warn(
                client_address, "%s: no handler for %s (%d bytes)", client_address, message.type_name, len(message.body)
            )
            return
        try:
            handler(client_address, message)
        except ProtocolError as error:
            self._warn(client_address, "%s: refused %s: %s", client_address, message.type_name, error)
        except Exception:
            # A bug, not a broken message: the next message of the block still gets its turn.
            self._warnings.error_with_traceback(
                client_address,
                time.monotonic(),
                "%s: internal error while handling %s",
                client_address,
                message.type_name,
            )

    def forget_client(self, client_address: Address) -> None:
        """The client's connection is gone: take its player out of every group. Nobody can tell the
        player anything any more, so only the remaining members hear about it. The lobby and the
        other clients come first; the database only keeps the statistics, and a failure there
        must not leave the others seeing a player who is gone."""
        player = self._players_by_address.pop(client_address, None)
        if player is None:
            return
        del self._players_by_id[player.player_id]
        log.info("player %d %r left the lobby", player.player_id, player.name)
        # A copy, since every removal also takes the group out of player.group_ids.
        for group_id in list(player.group_ids):
            self._remove_player_from_group(player, group_id, is_player_told=False)
        session_id = player.session_id
        if session_id is not None:
            self._do_bookkeeping("recording the logout", lambda: self._database.record_logout(session_id))
        self._send_statistics_to_home()

    def _do_bookkeeping(self, what: str, action: Callable[[], T]) -> T | None:
        """Database work for the statistics, which must not stop the lobby when the database
        fails: the failure is logged, and None comes back instead of the action's result."""
        try:
            return action()
        except DatabaseError as error:
            log.warning("database: %s failed: %s", what, error)
            return None

    def announce(self, text: str) -> int:
        """Send a server line to every player in the lobby; returns how many it reached."""
        message = ChatCommandAnswer(group_id=ANNOUNCEMENT_GROUP_ID, lines=fit_answer([text]))
        players = list(self._players_by_id.values())
        for player in players:
            self._outbox.send(player.address, message)
        log.info("announced to %d player(s): %r", len(players), text)
        return len(players)

    def send_pending_announcements(self) -> None:
        """Send what another process (the command line, a web interface) queued in the database."""
        for text in self._database.take_pending_announcements():
            self.announce(text)

    def is_logged_on(self, client_address: Address) -> bool:
        """True when a player is logged on from this address."""
        return client_address in self._players_by_address

    def carry_out_pending_kicks(self) -> None:
        """Remove the players that another process, a ban or an account deletion asked to remove
        (LobbyDatabase.queue_kick). A kick for a player who is not online is simply dropped."""
        for kick in self._database.take_pending_kicks():
            self.kick_player(kick.account_id, kick.reason)

    def kick_player(self, player_id: int, reason: str) -> bool:
        """Tell the player why, take them out of every group and close their connection. The game
        then shows "The connection to the lobby server dropped." (0x900a7; during a match "Lobby
        connection lost!", 0x90146): its ConnectionClosed handler logs out and posts status 6
        (0x5cc840, 0x5cc972). False when the player is not online."""
        player = self._players_by_id.get(player_id)
        if player is None:
            return False
        line = ChatCommandAnswer(group_id=ANNOUNCEMENT_GROUP_ID, lines=fit_answer([printable_text(reason)]))
        self._outbox.send(player.address, line)
        log.info("player %d %r removed from the lobby: %s", player.player_id, player.name, reason)
        address = player.address
        self.forget_client(address)
        self._outbox.close_connection(address)
        return True

    def live_state(self) -> LiveLobbyState:
        """Who is online and which groups exist, for the web interface (see live.py)."""
        marks_admins = self._database.get_boolean_setting(Setting.MARK_ADMINS)
        players = tuple(
            live_player(player, self._outbox.reachability_of(player.address), marks_admins)
            for player in sorted(self._players_by_id.values(), key=lambda player: player.name)
        )
        groups = tuple(self._live_group(self._groups[group_id]) for group_id in sorted(self._groups))
        return LiveLobbyState(server_name=self._server_name, players=players, groups=groups)

    def _live_group(self, group: Group) -> LiveGroup:
        """One group for the live lobby state; for a game also whether it is a ladder game and, when its
        description decodes, its map and number of teams."""
        live_group = LiveGroup(
            group_id=group.group_id,
            kind=group.kind.value,
            name=readable_text(group.announcement.name),
            max_players=group.announcement.max_players,
            is_full=group.is_full,
            has_password=bool(group.password),
            member_names=tuple(player.name for player in self._members_of(group)),
            listed_in_ids=tuple(sorted(group.listed_in_parent_ids)),
        )
        if group.kind != GroupKind.GAME:
            return live_group
        live_group = replace(live_group, is_ladder_game=is_ladder_game(group.announcement))
        description = game_description_of(group.announcement)
        if description is None:
            return live_group
        return replace(live_group, map_name=readable_text(description.map_name), team_count=description.team_count)

    def forget_quiet_keys(self, now_seconds: float) -> None:
        """Let the per client warning, logon, password and update limits forget clients that went quiet."""
        self._warnings.forget_quiet_keys(now_seconds)
        self._logon_check.forget_quiet_keys(now_seconds)
        self._wrong_group_passwords.forget_quiet_keys(now_seconds)
        self._group_updates.forget_quiet_keys(now_seconds)

    # Index server role.

    def _answer_patch_check(self, client_address: Address, message: RawMessage) -> None:
        """CheckFileVersion: always answered with Result NO_PATCH_NEEDED."""
        # The server never says a patch is needed: there is none to offer, and a game told to fetch
        # one stops before the logon. Every version check, whatever version, edition and language
        # it reports, gets Result NO_PATCH_NEEDED: the client's handler marks the answer as received
        # and takes this server as its index server (0x5cff1c, 0x5cff27). PATCH_INFORMATION (0x12)
        # is never sent. Games of another version still cannot join each other's games: the game
        # block carries a checksum of the game's program that the joining client compares.
        request = CheckFileVersion.decode(message)
        log.info(
            "%s: patch check, game version %d, edition %d, %s: no patch needed",
            client_address,
            request.game_version,
            request.edition,
            request.language_name,
        )
        self._outbox.send(client_address, Result(ResultCode.NO_PATCH_NEEDED))

    def _answer_lobby_server_list(self, client_address: Address, message: RawMessage) -> None:
        """GetLobbyServerList: answered with a list that holds only this server."""
        # The list names this same server: the client then closes the index connection and opens
        # a new one here for the logon.
        server_address_text = self._outbox.server_address_text_for(client_address)
        log.info("%s: lobby server list: %s %r", client_address, server_address_text, self._server_name)
        this_server = LobbyServerEntry(address_text=server_address_text, name=self._server_name)
        self._outbox.send(client_address, LobbyServerList(servers=(this_server,)))

    # Lobby server role: logon.

    def _log_on_player(self, client_address: Address, message: RawMessage) -> None:
        """Logon: let the player in with Result LOGON_ACCEPTED and OwnPlayer, or refuse with the
        Result code the logon check chose."""
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
        # The player forgotten just above no longer counts in the occupancy, so logging on again
        # from the same address is not refused as "already logged on".
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
        welcome_message = self._database.get_setting(Setting.WELCOME_MESSAGE)
        if welcome_message:
            self._outbox.send(client_address, ChatCommandAnswer(ANNOUNCEMENT_GROUP_ID, fit_answer([welcome_message])))
        self._do_bookkeeping("telling a player about the ladder", lambda: self._tell_ladder_at_logon(player))
        # Only an accepted logon gets the network side's extra work (the reachability probe, a
        # forwarded port on the router); a refused one has returned above.
        self._outbox.player_logged_on(client_address)

    def _admit_player(self, client_address: Address, logon: Logon, decision: AcceptedLogon) -> Player:
        """Record the session and make the accepted account a player of the lobby."""
        account = decision.account
        # Only the statistics need the session row: a database failure must not stop the logon,
        # or the game would wait for an answer that never comes.
        session_id = self._do_bookkeeping(
            "recording the logon", lambda: self._database.record_logon(account.account_id)
        )
        player = Player(
            player_id=account.account_id,
            name=account.name,
            address=client_address,
            serial_number=logon.serial_number,
            is_admin=account.is_admin,
            session_id=session_id,
            shown_clan=decision.shown_clan,
        )
        self._players_by_address[client_address] = player
        self._players_by_id[player.player_id] = player
        return player

    def _occupancy(self) -> LobbyOccupancy:
        """Who is online now, as the logon check needs it."""
        players = list(self._players_by_id.values())
        return LobbyOccupancy(
            player_count=len(players),
            casefolded_player_names=frozenset(player.name.casefold() for player in players),
            serial_numbers=frozenset(player.serial_number for player in players),
        )

    def _log_out_player(self, client_address: Address, message: RawMessage) -> None:
        """Logout: the player leaves the lobby and every group in it."""
        self.forget_client(client_address)

    # Lobby server role: joining and leaving groups.

    def _join_group_on_request(self, client_address: Address, message: RawMessage) -> None:
        """AddPlayerToGroup from a client: join a room, game or chat group, or get the Result that
        says why not."""
        # Right after the logon the client joins its lobby tab (1 to 4) and one zone per bit of its
        # GameZoneSelection setting (30 to 38 with the default 511), all in one block, without
        # asking for a list of groups first. A joiner of a game or chat group knows its id from the
        # zone or theme it saw it in; for a chat group the extra text carries the password.
        request = AddPlayerToGroup.decode(message)
        player = self._find_logged_on_player(client_address)
        # Group id 0 is a private chat invitation between two clients, never a group on this server.
        if player is None or request.group_id == PRIVATE_CHAT_GROUP_ID:
            return
        group = self._groups.get(request.group_id)
        if group is None:
            self._refuse_join(player, request.group_id, ResultCode.GROUP_CHANGE_FAILED, "no such group")
            return
        # Already a member: announcing the group again would fail the client's assert
        # "! vJoinedGroups.contains" and drop the connection (see _remove_player_from_group).
        if player.player_id in group.member_ids:
            return
        if group.password:
            # Every join carries one guess, and a datagram can hold hundreds: wrong guesses are
            # limited per player, and once over the limit even the right password waits.
            now_seconds = time.monotonic()
            if self._wrong_group_passwords.is_exhausted(player.player_id, now_seconds):
                self._refuse_join(player, group.group_id, ResultCode.WRONG_GROUP_PASSWORD, "too many wrong passwords")
                return
            if request.extra_text != group.password:
                self._wrong_group_passwords.record_event(player.player_id, now_seconds)
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
        self._tell_about_ladder(group, [player])

    def _leave_group_on_request(self, client_address: Address, message: RawMessage) -> None:
        """RemovePlayerFromGroup from a client: leave the group, with the confirmation the client
        waits for."""
        request = RemovePlayerFromGroup.decode(message)
        player = self._find_logged_on_player(client_address)
        if player is not None:
            self._remove_player_from_group(player, request.group_id, is_player_told=True)

    def _refuse_join(self, player: Player, group_id: int, result_code: ResultCode, reason: str) -> None:
        """Tell a player that a join or the opening of a group failed: a Result with the group id as
        value, which the game shows as a message."""
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
        # An id this server already gave out: the host wants its game listed in one more zone. A
        # new group comes with SERVER_ASSIGNS_GROUP_ID instead, which never names an existing one.
        if existing_group is not None:
            self._list_existing_game(player, existing_group, parent)
            return
        if player.player_id not in parent.member_ids:
            raise ProtocolError(f"new group under {request.parent_id}, a group the player is not in")
        self._open_player_group(player, request, parent)

    def _open_player_group(self, player: Player, request: GroupAnnouncement, parent: Group) -> None:
        """Open a game or chat group a player asked for, under an id the server picks, and list it in
        the parent it was opened in."""
        if request.group_id not in (SERVER_ASSIGNS_GROUP_ID, PRIVATE_CHAT_GROUP_ID):
            raise ProtocolError(f"new group with id {request.group_id}; only the server picks ids")
        if request.group_id == PRIVATE_CHAT_GROUP_ID:
            # A private chat invitation goes from client to client; one sent here is a mistake.
            return
        check_client_group_texts(request)
        # Only groups this player opened that are still open count, so one client cannot fill the
        # zones with groups while a host who closed its games may open new ones.
        open_group_count = sum(1 for group in self._groups.values() if group.creator_player_id == player.player_id)
        if open_group_count >= MAX_OPEN_GROUPS_PER_PLAYER:
            self._refuse_join(player, request.group_id, ResultCode.GROUP_CHANGE_FAILED, "too many open groups")
            return

        group_id = self._next_player_group_id
        self._next_player_group_id += 1
        # The host only knows itself as 0.0.0.0; joiners need the address the server sees it at.
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
            group.game_record_id = self._do_bookkeeping(
                "recording the game's opening",
                lambda: self._database.record_game_opened(
                    readable_text(announcement.name),
                    player.player_id,
                    group_id,
                    stored_game_announcement(announcement),
                    ladder_type_of(announcement),
                ),
            )
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
        # The creator's lobby screen keeps the new group in an empty slot until this Result names
        # it (see GROUP_OPENED). Without it a chat group's creator had no name, no members and no
        # working chat, whisper or new game until leaving and joining again (seen in a run).
        self._outbox.send(player.address, Result(ResultCode.GROUP_OPENED, value=group_id))
        self._list_group_in(group, parent)
        # Group 5 is listed nowhere and nobody stays in it, so a chat group listed only there would
        # be invisible to everyone but its creator.
        if parent.group_id == PASSWORD_CHAT_GROUP_PARENT_ID and group.kind == GroupKind.CHAT_GROUP:
            self._list_in_creators_chat_theme(group, player)
        self._tell_about_ladder(group, [player])

    def _list_in_creators_chat_theme(self, group: Group, creator: Player) -> None:
        """List a chat group with a password, opened under the hidden group 5, in the chat theme
        its creator just left, or in the first theme when there is none, so others can find it."""
        theme = self._groups.get(creator.last_chat_theme_id) if creator.last_chat_theme_id is not None else None
        # Also None when the theme was removed from the database since the creator was in it.
        if theme is None:
            themes = self._groups_listed_in(CHAT_ZONE_GROUP_ID)
            if not themes:
                return
            theme = themes[0]
        self._list_group_in(group, theme)

    def _list_existing_game(self, player: Player, group: Group, zone: Group) -> None:
        """List a player's own game in one more zone ("Show game in GameZone"); listing it twice is
        ignored."""
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
        # Each update goes on to every viewer: a budget per player keeps one client from
        # multiplying its traffic onto everyone in a zone. A host sends a few while setting up.
        if not self._group_updates.allows(player.player_id, time.monotonic()):
            raise ProtocolError("more group updates than the budget allows")
        check_client_group_texts(update)
        # Every update carries a game block, and its host reads 0.0.0.0 just like at the opening.
        update = replace(update, game_block=game_block_with_real_host(update.game_block, player.address))
        updated = update.applied_to(group.announcement)
        # A third text in the update is a new password; an empty one keeps the password there was.
        if updated.third_text:
            group.password = updated.third_text
        group.announcement = replace(updated, third_text="")
        self._record_game_announcement(group)
        # The update passed on still carries the client's third text: viewers get it without.
        public_update = replace(update, third_text="")
        for viewer in self._viewers_of(group):
            self._outbox.send(viewer.address, public_update)
        self._send_counters_to_viewers(group)
        self._tell_about_ladder(group, self._members_of(group))

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
            "player %d %r unlists group %d from group %d",
            player.player_id,
            player.name,
            group.group_id,
            request.parent_id,
        )
        self._unlist_group_from(group, request.parent_id)

    # Chat and other requests.

    def _log_chat_message(self, client_address: Address, message: RawMessage) -> None:
        """ChatMessage sent to the server: logged only, as nobody else needs it from here."""
        # Clients chat with each other directly; the server only sees lines meant for it.
        chat = ChatMessage.decode(message)
        log.info("%s: chat to group %d: %r", client_address, chat.group_id, chat.text)

    def _answer_chat_command(self, client_address: Address, message: RawMessage) -> None:
        """ChatCommand: answer a slash command the client does not handle itself (see commands.py)."""
        command = ChatCommand.decode(message)
        if len(command.command) > MAX_CHAT_COMMAND_LENGTH:
            raise ProtocolError(f"chat command of {len(command.command)} characters")
        player = self._find_logged_on_player(client_address)
        if player is None:
            return
        # Read as stored text: a ban reason typed here goes into the database.
        command_text = readable_text(command.command)
        log.info("player %d %r: %r", player.player_id, player.name, command_text)
        lines = self._chat_commands.answer(player, command_text)
        self._outbox.send(client_address, ChatCommandAnswer(group_id=command.group_id, lines=lines))

    def _answer_image_list(self, client_address: Address, message: RawMessage) -> None:
        """GetImageList from a logged on member of the group: the banner for Home, an empty list for
        any other group."""
        # Only the Home tab shows an image: the banner, when one is configured. The game asks only
        # for groups it joined, so only after its logon; answering anyone else would let a forged
        # sender address turn a small request into a large answer to someone else.
        player = self._find_logged_on_player(client_address)
        if player is None:
            return
        request = GetImageList.decode(message)
        if request.group_id not in player.group_ids:
            # The game takes an image list only for a group it is in and drops the connection
            # otherwise (0x5d5258..0x5d5296); a request that arrives after the player left the
            # group gets no answer.
            return
        images = ()
        banner = self._configured_banner()
        if request.group_id == HOME_GROUP_ID and banner is not None:
            images = (banner,)
        self._outbox.send(client_address, ImageList(group_id=request.group_id, images=images))

    def _answer_text_message(self, client_address: Address, message: RawMessage) -> None:
        """GetTextMessage from a logged on player: a news text, a board message or a placeholder."""
        # Asked for from the Home tab and the board, so only after the logon (see _answer_image_list).
        if self._find_logged_on_player(client_address) is None:
            return
        request = GetTextMessage.decode(message)
        self._outbox.send(client_address, self._text_message_for(request.text_id))

    def _text_message_for(self, text_id: int) -> TextMessage:
        """The text with this id: the news for one audience, or a placeholder for unknown ids,
        always with a title, since a text without one leaves the client waiting for good."""
        for audience, news_text_id in NEWS_TEXT_ID_BY_AUDIENCE.items():
            news = self._database.find_news(audience)
            if text_id == news_text_id and news is not None:
                return text_message(text_id, news.title, self._news_with_ladder(news.title, news.body, audience))
        # Board texts are numbered from BOARD_MESSAGE_TEXT_ID_BASE up: the base plus the message's
        # database id (see _send_board_message_ids).
        if text_id >= BOARD_MESSAGE_TEXT_ID_BASE:
            board_message = self._database.find_board_message(text_id - BOARD_MESSAGE_TEXT_ID_BASE)
            if board_message is not None:
                byline = f"{board_message.author or self._server_name}, {board_message.posted_at:%Y-%m-%d}"
                return text_message(text_id, board_message.title, f"{byline}\n\n{board_message.body}")
        return TextMessage(text_id=text_id, title=UNKNOWN_TEXT_TITLE)

    def _news_with_ladder(self, title: str, body: str, audience: str) -> str:
        """The admins' news, and under it the ladder's standings while the ladder is on (setting
        ladder_news_standings). The admins' text is never cut: the ladder block loses lines from its
        end until both fit the game's limit."""
        is_wanted = self._is_ladder_enabled() and self._database.get_boolean_setting(Setting.LADDER_NEWS_STANDINGS)
        if not is_wanted:
            return body
        language = GERMAN if audience == NEWS_AUDIENCE_GERMAN else ENGLISH
        block = self._do_bookkeeping(
            "adding the ladder to the news", lambda: self._ladder_writer.texts.news_lines(language)
        )
        if not block:
            return body
        news_lines = body.splitlines()
        while block and text_size(title, [*news_lines, "", *block]) > MAX_TEXT_MESSAGE_BYTES:
            block.pop()
        if not block:
            return body
        return "\n".join([*news_lines, "", *block])

    def _store_game_status(self, client_address: Address, message: RawMessage) -> None:
        """A client reports on its match, once at the start and once when a single team is left
        (0x540500). The report is kept as the client wrote it. The client is told it was stored,
        or for a ladder game that it is not valid for the ladder; the game shows the answer only
        during a ladder game (0x540a18)."""
        status = GameStatus.decode(message)
        player = self._find_logged_on_player(client_address)
        if player is None:
            return
        # A report that arrives after every player left is still stored, only without a link to the
        # game's record, as the closed group is gone.
        game_record_id = None
        group = self._groups.get(status.group_id)
        if group is not None and group.kind == GroupKind.GAME:
            game_record_id = group.game_record_id
        # A report that cannot be stored is still answered like any other: the statistics are the
        # only thing that suffers (the game shows the answer during a ladder match, 0x540a18).
        self._do_bookkeeping(
            "storing a match report",
            lambda: self._database.store_game_report(player.player_id, status.group_id, message.body, game_record_id),
        )
        outcome = "no winner yet"
        if status.has_winner:
            outcome = f"team {status.winning_team} won"
        log.info(
            "player %d %r reported the match of group %d: map %r, %s, %d slot(s), game time %d",
            player.player_id,
            player.name,
            status.group_id,
            status.map_name,
            outcome,
            len(status.players),
            status.game_time,
        )
        result_code = ResultCode.GAME_STATUS_STORED
        if status.is_ladder_game and not self._is_ladder_enabled():
            # Stored and evaluated like any report, but until the admins switch the ladder on the
            # game must not take it for a ranked result: it shows "Game not valid for ladder."
            # during the match (0x540b89).
            result_code = ResultCode.GAME_NOT_VALID_FOR_LADDER
        elif status.is_ladder_game and not report_can_count(status):
            # A report that can never count (computer opponents, the game's own invalid mark) is
            # told so at once; whether a good one counts is decided once every report is in.
            result_code = ResultCode.GAME_NOT_VALID_FOR_LADDER
        self._outbox.send(client_address, Result(result_code))

    def _record_game_announcement(self, group: Group) -> None:
        """Keep a game's current setup in its database record, for the Games page."""
        game_record_id = group.game_record_id
        if game_record_id is None:
            return
        announcement = stored_game_announcement(group.announcement)
        ladder_type = ladder_type_of(group.announcement)
        self._do_bookkeeping(
            "recording the game's setup",
            lambda: self._database.record_game_announcement(game_record_id, announcement, ladder_type),
        )

    def _record_game_player(self, group: Group, player: Player) -> None:
        """Note in a game's database record that a player joined it, once per player."""
        game_record_id = group.game_record_id
        if game_record_id is None or player.player_id in group.recorded_player_ids:
            return
        group.recorded_player_ids.add(player.player_id)
        # The ladder needs the clan the player shows now (Clanliga) and which copy of the game it
        # plays (two copies of one installation never count against each other).
        clan_id = player.shown_clan.clan_id if player.shown_clan is not None else None
        installation = group.installation_of(player.serial_number)
        self._do_bookkeeping(
            "recording a game's player",
            lambda: self._database.record_game_player(game_record_id, player.player_id, clan_id, installation),
        )

    def evaluate_ladder_games(self) -> None:
        """Turn ladder games whose reports are in (or whose time ran out) into results. The games
        this server still has open are passed on, so a game left open by an earlier run of the
        server can be told apart from one still being played."""
        open_game_ids = [
            group.game_record_id
            for group in self._groups.values()
            if group.kind == GroupKind.GAME and group.game_record_id is not None
        ]
        stored = self._ladder_book.evaluate_waiting_games(open_game_ids)
        if not stored or not self._is_ladder_enabled():
            return
        # The players of each decided game who are online hear how it ended, as a server line: the
        # game itself has no place to show a ladder result after the match.
        for decided in stored:
            account_ids = {player.account_id for player in decided.evaluation.players}
            if not account_ids:
                account_ids = {
                    player.account_id
                    for player in self._database.list_game_players(decided.evaluation.game_id)
                    if player.account_id is not None
                }
            lines = self._ladder_book.match_lines(decided.evaluation, decided.game_name)
            if not lines:
                continue
            answer = ChatCommandAnswer(group_id=ANNOUNCEMENT_GROUP_ID, lines=fit_answer(lines))
            for account_id in account_ids:
                player = self._players_by_id.get(account_id)
                if player is not None:
                    self._outbox.send(player.address, answer)

    def write_ladder_texts(self) -> None:
        """Write the ladder's board texts that are due (season starts and ends, the weekly
        summary) and announce a season's end to everyone online. Only while the ladder is on:
        with it off the game is told that nothing is ranked."""
        if not self._is_ladder_enabled():
            return
        written = self._ladder_writer.run()
        for text in written.announcements:
            self.announce(text)

    def _tell_ladder_at_logon(self, player: Player) -> None:
        """At logon, while the ladder is on: the player's place (setting ladder_logon_line), and how
        an admin decided a disputed game of theirs since, once per decision."""
        if not self._is_ladder_enabled():
            return
        lines = []
        if self._database.get_boolean_setting(Setting.LADDER_LOGON_LINE):
            place_line = self._ladder_writer.texts.logon_line(player.player_id)
            if place_line is not None:
                lines.append(place_line)
        since = self._database.now() - LADDER_EVENT_MAX_AGE
        for record in self._ladder_book.results():
            # Only an admin's decisions, recent ones, and only for the player's own games.
            is_admin_decision = bool(record.decided_by_name) and record.decided_at >= since
            is_players_game = any(item.account_id == player.player_id for item in record.evaluation.players)
            if not is_admin_decision or not is_players_game:
                continue
            # The result's row id is new with every decision, so a changed decision is told again.
            if self._database.record_ladder_event(f"told-decision:{record.result_id}:{player.player_id}"):
                lines.append(self._ladder_writer.texts.decision_line(record.evaluation, record.game_name))
        if lines:
            self._outbox.send(player.address, ChatCommandAnswer(ANNOUNCEMENT_GROUP_ID, fit_answer(lines)))

    def _is_ladder_enabled(self) -> bool:
        """Whether the admins switched the ladder on (setting ladder_enabled): only then does the
        game hear that a ladder report was stored, and players how their match counted."""
        return self._database.get_boolean_setting(Setting.LADDER_ENABLED)

    def _online_player_names(self) -> list[str]:
        """The names of everyone logged on, for the chat commands."""
        return [player.name for player in self._players_by_id.values()]

    # Membership.

    def _add_player_to_group(self, player: Player, group: Group) -> None:
        """Put a player into a group and tell everyone concerned: the player gets the group, its
        members and what is listed in it; the members get the newcomer; whoever sees the group in
        a parent gets its new counters."""
        log.info("player %d %r joins group %d", player.player_id, player.name, group.group_id)
        # Brought in line with the database before the group goes out, so settings, themes and
        # languages changed while the server runs show on this join.
        if group.group_id == HOME_GROUP_ID:
            group.announcement = replace(group.announcement, flags=self._home_flags())
        if group.group_id == CHAT_ZONE_GROUP_ID:
            self._sync_chat_themes()
        if group.group_id == MESSAGE_ZONE_GROUP_ID:
            self._sync_board_languages()
        if is_board_language_group(group.group_id):
            self._sync_board_themes(language_of_board_group(group.group_id))
        self._outbox.send(player.address, group.announcement.under_parent(JOINED_GROUP_PARENT_ID))
        marks_admins = self._database.get_boolean_setting(Setting.MARK_ADMINS)
        # The newcomer is not in member_ids yet, so the loop only pairs it with the others; the
        # newcomer's entry for itself is sent once after the loop.
        for member in self._members_of(group):
            self._outbox.send(player.address, member_entry(group.group_id, member, marks_admins))
            self._outbox.send(member.address, member_entry(group.group_id, player, marks_admins))
        self._outbox.send(player.address, member_entry(group.group_id, player, marks_admins))
        group.member_ids.append(player.player_id)
        # A chat group with a member again no longer waits to be closed (close_abandoned_chat_groups).
        group.empty_since_seconds = None
        player.group_ids.add(group.group_id)
        if is_chat_theme_group(group.group_id):
            player.last_chat_theme_id = group.group_id
        self._record_game_player(group, player)
        for child in self._groups_listed_in(group.group_id):
            self._show_group(player, child, group.group_id)
        self._send_counters_to_viewers(group)
        if group.group_id == HOME_GROUP_ID:
            self._send_home_page(player)
        if group.group_id == MESSAGE_ZONE_GROUP_ID:
            self._tell_about_board_chat(player)
        if is_board_theme_group(group.group_id):
            self._send_board_message_ids(player, group.group_id - BOARD_THEME_GROUP_ID_BASE)

    def _tell_about_board_chat(self, player: Player) -> None:
        """Once per logon, when the setting asks for it: chat typed on the message board reaches
        nobody (see BOARD_CHAT_HINT_TEXT)."""
        if player.has_heard_board_chat_hint:
            return
        if not self._database.get_boolean_setting(Setting.BOARD_CHAT_HINT):
            return
        player.has_heard_board_chat_hint = True
        self._outbox.send(player.address, ChatCommandAnswer(ANNOUNCEMENT_GROUP_ID, fit_answer([BOARD_CHAT_HINT_TEXT])))

    def _tell_about_ladder(self, group: Group, players: Iterable[Player]) -> None:
        """Blue Byte's server ranked ladder games; this one keeps no ladder, so a ladder game
        counts for nothing beyond the match itself. Each player of a ladder game hears so once, as
        a server line: the host when opening it or when turning it into one during the setup, a
        joiner when joining."""
        is_ladder = group.kind == GroupKind.GAME and is_ladder_game(group.announcement)
        # With the ladder switched on, ladder games are ranked, so there is nothing to warn about.
        if not is_ladder or self._is_ladder_enabled():
            return
        line = ChatCommandAnswer(group_id=ANNOUNCEMENT_GROUP_ID, lines=fit_answer([LADDER_NOT_SUPPORTED_TEXT]))
        for player in players:
            if player.player_id in group.ladder_notice_player_ids:
                continue
            group.ladder_notice_player_ids.add(player.player_id)
            self._outbox.send(player.address, line)
            log.info(
                "player %d %r told that ladder game %d is not ranked", player.player_id, player.name, group.group_id
            )

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
        """Send the current statistics to everyone on the Home tab, whenever the numbers change
        (a logon, a logout, a game opened or closed). Nothing is sent when the database fails."""
        home = self._groups[HOME_GROUP_ID]
        members = self._members_of(home)
        if not members:
            return
        statistics = self._do_bookkeeping("reading the statistics", self._statistics)
        if statistics is None:
            return
        for member in members:
            self._outbox.send(member.address, statistics)

    def _statistics(self) -> ServerStatistics:
        """The statistics table of the Home tab, read from the database."""
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
        """The banner to offer, or None: without a url, or with banner id 0, which the game
        ignores (0x449894), Home does not ask for images at all."""
        url = self._database.get_setting(Setting.BANNER_URL)
        banner_id = self._database.get_integer_setting(Setting.BANNER_ID)
        if not url.startswith(BANNER_URL_PREFIX) or banner_id == NO_BANNER_ID:
            return None
        return ImageEntry(
            banner_id=banner_id,
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
        # The player's own view is cleaned up first, even when the group is already gone, so the
        # mirror of what the client sees never keeps a group it dropped.
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
        if not group.member_ids and group.kind == GroupKind.GAME:
            self._close_player_group(group)
            return
        if not group.member_ids and group.kind == GroupKind.CHAT_GROUP:
            # Kept for its players to come back to (see EMPTY_CHAT_GROUP_KEEP_SECONDS).
            group.empty_since_seconds = time.monotonic()
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
        """Stop listing a group in a parent, and remove it from that parent for every player who sees it
        there."""
        group.listed_in_parent_ids.discard(parent_id)
        for player in list(self._players_by_id.values()):
            self._hide_group(player, group.group_id, parent_id)

    def close_abandoned_chat_groups(self, now_seconds: float) -> None:
        """Close the chat groups that have been empty for EMPTY_CHAT_GROUP_KEEP_SECONDS and hold
        no open game. The time counts from the later of the last departure and the end of the last
        game opened in the group, so a host who played a long match still finds the group."""
        # A game keeps the parent it was opened in, so for a game opened from a chat group this
        # is the chat group's id.
        parent_ids_of_open_games = {
            group.announcement.parent_id for group in self._groups.values() if group.kind == GroupKind.GAME
        }
        # A copy, since closing a group takes it out of self._groups.
        for group in list(self._groups.values()):
            if group.kind != GroupKind.CHAT_GROUP or group.empty_since_seconds is None:
                continue
            if group.group_id in parent_ids_of_open_games:
                # The wait starts over on every pass while the game is open, so it counts from the
                # game's end once the game is closed.
                group.empty_since_seconds = now_seconds
                continue
            if now_seconds - group.empty_since_seconds >= EMPTY_CHAT_GROUP_KEEP_SECONDS:
                self._close_player_group(group)

    def _close_player_group(self, group: Group) -> None:
        """Remove a game or chat group: unlist it everywhere, forget it and, for a game, record its end."""
        # A copy, since every unlisting takes the parent out of listed_in_parent_ids.
        for parent_id in list(group.listed_in_parent_ids):
            self._unlist_group_from(group, parent_id)
        self._groups.pop(group.group_id, None)
        if group.game_record_id is not None:
            game_record_id = group.game_record_id
            self._do_bookkeeping("recording the game's end", lambda: self._database.record_game_closed(game_record_id))
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
        """Send a game's or chat group's counters to everyone who sees it in a parent; rooms have none
        the client shows."""
        if group.is_room:
            return
        counters = group.counters()
        for viewer in self._viewers_of(group):
            self._outbox.send(viewer.address, counters)

    def _viewers_of(self, group: Group) -> list[Player]:
        """Players who see the group inside a parent; members of the group do not count."""
        viewers = []
        for player in self._players_by_id.values():
            # A member takes no updates for its own group (0x5d8de6), even if it still saw the group
            # listed somewhere before it joined.
            if player.player_id in group.member_ids:
                continue
            sees_group = any(visible[1] == group.group_id for visible in player.visible_groups)
            if sees_group:
                viewers.append(player)
        return viewers

    def _groups_listed_in(self, parent_id: int) -> list[Group]:
        """The groups listed inside a parent."""
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
        # The default theme takes the base id itself: generated database ids start at 1, so a theme
        # added later never gets the id of a default theme that still has players in it.
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
            announcement = room_announcement(group_id, name, description_text=description)
            theme = Group(announcement=announcement, kind=GroupKind.ROOM)
            self._groups[group_id] = theme
            self._list_group_in(theme, chat_zone)

    # The MessageZone tab.

    def _sync_board_languages(self) -> None:
        """List one group per language that has board themes, named as the client looks for them
        ("MessageThemes" + language), and always the English one the client falls back to."""
        message_zone = self._groups[MESSAGE_ZONE_GROUP_ID]
        languages_with_themes = {theme.language for theme in self._database.list_board_themes()}
        languages_with_themes.add(BOARD_FALLBACK_LANGUAGE)
        # A language group's id is its position in BOARD_LANGUAGES, so it never depends on the
        # database. Language groups are only ever added, never unlisted.
        for index, language in enumerate(BOARD_LANGUAGES):
            group_id = BOARD_LANGUAGE_GROUP_ID_BASE + index
            if language in languages_with_themes and group_id not in self._groups:
                name = BOARD_LANGUAGE_GROUP_PREFIX + language
                language_group = Group(announcement=room_announcement(group_id, name), kind=GroupKind.ROOM)
                self._groups[group_id] = language_group
                self._list_group_in(language_group, message_zone)

    def _sync_board_themes(self, language: str) -> None:
        """List the board themes of one language in its language group; themes removed from the
        database disappear once nobody is in them."""
        language_group_id = BOARD_LANGUAGE_GROUP_ID_BASE + BOARD_LANGUAGES.index(language)
        language_group = self._groups[language_group_id]
        wanted = {}
        for theme in self._database.list_board_themes():
            if theme.language == language:
                wanted[BOARD_THEME_GROUP_ID_BASE + theme.theme_id] = theme
        for group in self._groups_listed_in(language_group_id):
            if group.group_id not in wanted and not group.member_ids:
                self._unlist_group_from(group, language_group_id)
                self._groups.pop(group.group_id, None)
        for group_id, theme in wanted.items():
            if group_id in self._groups:
                continue
            announcement = room_announcement(group_id, theme.name, description_text=theme.description)
            theme_group = Group(announcement=announcement, kind=GroupKind.ROOM)
            self._groups[group_id] = theme_group
            self._list_group_in(theme_group, language_group)

    def _send_board_message_ids(self, player: Player, theme_id: int) -> None:
        """The ids of a theme's messages; the client asks for each text when it shows it."""
        messages = self._database.list_board_messages(theme_id)
        text_ids = tuple(BOARD_MESSAGE_TEXT_ID_BASE + message.message_id for message in messages)
        group_id = BOARD_THEME_GROUP_ID_BASE + theme_id
        self._outbox.send(player.address, TextIdList(group_id=group_id, text_ids=text_ids))

    # Built-in groups and lookups.

    def _open_built_in_rooms(self) -> None:
        """The lobby tabs and the game zones exist from the start, with the names the client's UI
        gives them. The client does not show these names, but they make logs and lists readable."""
        for group_id, name in {**LOBBY_TAB_GROUP_NAMES, **GAME_ZONE_NAMES}.items():
            self._groups[group_id] = Group(announcement=room_announcement(group_id, name), kind=GroupKind.ROOM)
        # Listed nowhere, with empty texts like the client's own copy (see PASSWORD_CHAT_GROUP_PARENT_ID).
        self._groups[PASSWORD_CHAT_GROUP_PARENT_ID] = Group(
            announcement=room_announcement(PASSWORD_CHAT_GROUP_PARENT_ID, ""), kind=GroupKind.ROOM
        )

    def _members_of(self, group: Group) -> list[Player]:
        """The players in a group who are still logged on."""
        return [self._players_by_id[member_id] for member_id in group.member_ids if member_id in self._players_by_id]

    def _find_logged_on_player(self, client_address: Address) -> Player | None:
        """The player logged on from this address, or None with a warning for a client that has not
        logged on."""
        player = self._players_by_address.get(client_address)
        if player is None:
            self._warn(client_address, "%s: lobby message from a client that has not logged on", client_address)
        return player

    def _warn(self, client_address: Address, message: str, *arguments: object) -> None:
        """Log a warning about a client's message, throttled per client address."""
        self._warnings.warning(client_address, time.monotonic(), message, *arguments)


def printable_text(text: str) -> str:
    """The text without control characters, which a chat line cannot carry (a NUL would even end
    the string early); reasons for kicks and bans are typed freely and never checked before."""
    return "".join(character for character in text if character >= FIRST_PRINTABLE_CHARACTER)


def own_player_message(player: Player) -> EncodableMessage:
    """OwnPlayer, or its clan variant when the player logged on as clan member. Either one, once."""
    if player.shown_clan is None:
        return OwnPlayer(player.player_id)
    return OwnPlayerWithClan(player.player_id, player.shown_clan.clan_id, player.shown_clan.name)


def is_highlighted_member(player: Player, marks_admins: bool) -> bool:
    """Whether the game draws the player's row in colour: shown with a clan an admin marked, or
    an admin while the setting mark_admins is on."""
    is_highlighted_clan = player.shown_clan is not None and player.shown_clan.is_highlighted
    return is_highlighted_clan or (marks_admins and player.is_admin)


def live_player(player: Player, reachability: Reachability, marks_admins: bool = False) -> LivePlayer:
    """A player for the live lobby state, with the address, the reachability found and the colour the
    game draws the player's row in."""
    host, port = player.address
    clan_name = player.shown_clan.name if player.shown_clan is not None else ""
    return LivePlayer(
        player_id=player.player_id,
        name=player.name,
        clan=clan_name,
        is_admin=player.is_admin,
        address=f"{host}:{port}",
        logged_on_at=to_stored_time(player.logged_on_at),
        group_ids=tuple(sorted(player.group_ids)),
        reachability=reachability.value,
        row_colour=game_row_colour(is_highlighted_member(player, marks_admins), clan_name),
    )


def member_entry(group_id: int, player: Player, marks_admins: bool = False) -> EncodableMessage:
    """A group member as other clients need it: with the address the server sees the player at,
    because they send chat straight to that address, and with the clan when the player shows one.
    A player shown with a highlighted clan, and with marks_admins an admin, comes as 0x26 with the
    HIGHLIGHTED flag, so the member list colours their row green (0x449da1); the clan travels in it
    as well, with clan id 0 and no name for a player without one."""
    if is_highlighted_member(player, marks_admins):
        clan_id = 0
        clan_name = ""
        if player.shown_clan is not None:
            clan_id = player.shown_clan.clan_id
            clan_name = player.shown_clan.name
        return AddPlayerToGroupWithFlags(
            group_id=group_id,
            player_id=player.player_id,
            player_address=player.address,
            player_name=player.name,
            clan_id=clan_id,
            clan_name=clan_name,
            player_flags=PlayerFlag.HIGHLIGHTED,
        )
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
    # Each string travels with a terminating NUL, hence one byte more than its text.
    used_bytes = len(title) + 1
    for line in body.splitlines():
        line_bytes = len(line.encode(TEXT_ENCODING, errors="replace")) + 1
        if used_bytes + line_bytes > MAX_TEXT_MESSAGE_BYTES:
            log.warning("text %#x %r cut to %d bytes", text_id, title, MAX_TEXT_MESSAGE_BYTES)
            break
        lines.append(line)
        used_bytes += line_bytes
    return TextMessage(text_id=text_id, title=title, lines=tuple(lines))


def is_board_language_group(group_id: int) -> bool:
    """True for the id of one of the MessageZone language groups."""
    return BOARD_LANGUAGE_GROUP_ID_BASE <= group_id < BOARD_LANGUAGE_GROUP_ID_BASE + len(BOARD_LANGUAGES)


def language_of_board_group(group_id: int) -> str:
    """The board language a language group stands for."""
    return BOARD_LANGUAGES[group_id - BOARD_LANGUAGE_GROUP_ID_BASE]


def is_chat_theme_group(group_id: int) -> bool:
    """True for the id of a chat theme, the range below the board language groups."""
    return CHAT_THEME_GROUP_ID_BASE <= group_id < BOARD_LANGUAGE_GROUP_ID_BASE


def is_board_theme_group(group_id: int) -> bool:
    """True for the id of a board theme, the range below the first game id."""
    return BOARD_THEME_GROUP_ID_BASE <= group_id < FIRST_GAME_GROUP_ID
