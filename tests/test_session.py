"""Whole lobby sessions against a running server, played by fake game clients.

Each test covers one behaviour the real game depends on; the docstrings say what breaks in the
game when the server gets it wrong.
"""

from __future__ import annotations

import unittest
from unittest import mock

from s3lobby.constants import (
    CHAT_ZONE_GROUP_ID,
    FIRST_GAME_GROUP_ID,
    JOINED_GROUP_PARENT_ID,
    LOGON_PROTOCOL_VERSION,
    NEWS_AUDIENCE_GERMAN,
    NEWS_TEXT_ID_BY_AUDIENCE,
    GameStatusFlag,
    LadderState,
    LadderType,
    LogonFlag,
    MessageType,
    PlayerFlag,
    ResultCode,
)
from s3lobby.database import DatabaseError, LobbyDatabase, Setting
from s3lobby.messages import (
    AddPlayerToGroup,
    AddPlayerToGroupWithClan,
    AddPlayerToGroupWithFlags,
    ChatCommandAnswer,
    CheckFileVersion,
    GameStatus,
    GetLobbyServerList,
    GetTextMessage,
    GroupAnnouncement,
    LobbyServerList,
    Logon,
    Logout,
    MatchReportPlayer,
    MessageId,
    OpenConnection,
    OwnPlayerWithClan,
    RemoveGroupFromGroup,
    RemovePlayerFromGroup,
    Result,
    TextMessage,
)
from s3lobby.ladder import Evaluation, ResultPlayer
from tests.test_groups import game_description, receive_announcement
from tests.support import (
    ACCESS_CODE,
    EDITION_MISSION_AND_GOLD,
    GAME_VERSION,
    GAME_ZONE_ID,
    GERMAN_LANGUAGE_INDEX,
    GOG_VERSION_CHECK,
    LOBBY_ROOM_ID,
    SERIAL_NUMBER,
    UNKNOWN_HOST_ADDRESS,
    LobbyTestCase,
    go_to_lobby_server,
    join_group,
    log_on,
    open_game,
    receive_member_entry,
    send_logon,
)


class LobbySessionTest(LobbyTestCase):
    """Whole sessions with fake game clients against a running server, each step as the real game takes it."""

    def test_patch_check_says_no_patch_and_the_list_names_this_server(self) -> None:
        """Without Result NO_PATCH_NEEDED the client stops with "patch check failed"; an empty
        server list stops it with error 0x1e."""
        client = self.new_client("Alice", nonce=17)
        client.send(GOG_VERSION_CHECK)
        self.assertEqual(Result.decode(client.receive_next(MessageType.RESULT)).code, ResultCode.NO_PATCH_NEEDED)

        client.send(GetLobbyServerList())
        server_list = LobbyServerList.decode(client.receive_next(MessageType.LOBBY_SERVER_LIST))
        self.assertEqual(len(server_list.servers), 1)
        self.assertEqual(server_list.servers[0].address_text, f"127.0.0.1:{self.server.address[1]}")
        self.assertEqual(server_list.servers[0].name, "Test Lobby")

    def test_version_check_never_offers_a_patch(self) -> None:
        """The server never tells a game that a patch is needed: there is no patch to offer, and
        a game told to fetch one stops before the logon. Whatever version, edition and language a
        game reports, the answer is NO_PATCH_NEEDED."""
        client = self.new_client("Alice", nonce=17)
        unusual_checks = (
            CheckFileVersion(game_version=150, edition=0, language_index=GERMAN_LANGUAGE_INDEX),
            CheckFileVersion(game_version=0xFFFFFFFF, edition=0xFFFFFFFF, language_index=0xFFFFFFFF),
            CheckFileVersion(game_version=GAME_VERSION, edition=EDITION_MISSION_AND_GOLD, language_index=7),
        )
        for version_check in unusual_checks:
            with self.subTest(version_check=version_check):
                client.send(version_check)
                answer = Result.decode(client.receive_next(MessageType.RESULT))
                self.assertEqual(answer.code, ResultCode.NO_PATCH_NEEDED)
        client.assert_nothing_arrives(MessageType.PATCH_INFORMATION)

    def test_logon_answers_result_before_own_player(self) -> None:
        """OwnPlayer's handler asserts the sender is the client's server, which only the Result
        LOGON_ACCEPTED establishes: the other order drops the connection."""
        client = self.new_client("Alice", nonce=17)
        log_on_messages = {MessageType.RESULT, MessageType.OWN_PLAYER}
        client.send(GOG_VERSION_CHECK)
        client.receive_next(MessageType.RESULT)
        client.close_connection()

        client.send(Logon(LOGON_PROTOCOL_VERSION, "Alice", ACCESS_CODE, 0, SERIAL_NUMBER))
        first = client.receive_matching(lambda message: message.message_type in log_on_messages)
        second = client.receive_matching(lambda message: message.message_type in log_on_messages)
        self.assertEqual(Result.decode(first).code, ResultCode.LOGON_ACCEPTED)
        self.assertEqual(second.message_type, MessageType.OWN_PLAYER)

    def test_refused_logon_gets_only_the_result_code(self) -> None:
        """The game shows the dialog text for the code; nothing else may follow (no OwnPlayer)."""
        client = self.new_client("Alice", nonce=17)
        go_to_lobby_server(client)
        result = send_logon(client, access_code="WRONGCODE")
        self.assertEqual(result.code, ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE)
        self.assertEqual(client.unread_messages_of_type(MessageType.OWN_PLAYER), [])

    def test_clan_member_is_shown_with_the_clan_to_others(self) -> None:
        """A player who ticked the clan box gets 0x24 instead of 0x0e, and the others see the
        clan name in the member list through 0x25."""
        romans = self.database.create_clan("Romans")
        self.database.set_clan_of_account(self.database.find_account("Bob").account_id, romans.clan_id)
        alice = self.new_client("Alice", nonce=17)
        bob = self.new_client("Bob", nonce=42)
        alice_id = log_on(alice)
        join_group(alice, LOBBY_ROOM_ID, alice_id)

        go_to_lobby_server(bob)
        send_logon(bob, flags=LogonFlag.SHOW_AS_CLAN_MEMBER)
        own_player = OwnPlayerWithClan.decode(bob.receive_next(MessageType.OWN_PLAYER_WITH_CLAN))
        self.assertEqual((own_player.clan_id, own_player.clan_name), (romans.clan_id, "Romans"))
        join_group(bob, LOBBY_ROOM_ID, own_player.player_id)

        entry = AddPlayerToGroupWithClan.decode(alice.receive_next(MessageType.ADD_PLAYER_TO_GROUP_WITH_CLAN))
        self.assertEqual((entry.player_name, entry.clan_name), ("Bob", "Romans"))

    def test_members_of_a_highlighted_clan_who_show_it_are_listed_green(self) -> None:
        """A highlighted clan's member who ticked the clan box comes as 0x26 with HIGHLIGHTED, so
        the member list draws the row green (0x449da1); one who did not tick it is not marked,
        as the clan itself is not shown either."""
        romans = self.database.create_clan("Romans")
        self.database.set_clan_highlighted(romans.clan_id, True)
        for name in ("Bob", "Carol"):
            self.database.set_clan_of_account(self.database.find_account(name).account_id, romans.clan_id)
        alice = self.new_client("Alice", nonce=17)
        join_group(alice, LOBBY_ROOM_ID, log_on(alice))

        bob = self.new_client("Bob", nonce=42)
        go_to_lobby_server(bob)
        send_logon(bob, flags=LogonFlag.SHOW_AS_CLAN_MEMBER)
        bob_id = OwnPlayerWithClan.decode(bob.receive_next(MessageType.OWN_PLAYER_WITH_CLAN)).player_id
        join_group(bob, LOBBY_ROOM_ID, bob_id)
        marked = AddPlayerToGroupWithFlags.decode(alice.receive_next(MessageType.ADD_PLAYER_TO_GROUP_WITH_FLAGS))
        self.assertEqual((marked.player_name, marked.clan_name, marked.player_flags), ("Bob", "Romans", PlayerFlag.HIGHLIGHTED))

        carol = self.new_client("Carol", nonce=77)
        join_group(carol, LOBBY_ROOM_ID, log_on(carol))
        alice.assert_nothing_arrives(MessageType.ADD_PLAYER_TO_GROUP_WITH_FLAGS)

    def test_admins_are_marked_in_the_member_list_only_when_set(self) -> None:
        """With mark_admins (the default), an admin arrives as 0x26 with the HIGHLIGHTED flag and
        the member list colours the row (0x449da1); turned off, admins look like everyone else.
        Other players are never marked."""
        bob_account = self.database.find_account("Bob")
        self.database.set_admin(bob_account.account_id, True)
        self.database.set_setting(Setting.MARK_ADMINS, "false")
        alice = self.new_client("Alice", nonce=17)
        alice_id = log_on(alice)
        join_group(alice, LOBBY_ROOM_ID, alice_id)
        bob = self.new_client("Bob", nonce=42)
        join_group(bob, LOBBY_ROOM_ID, log_on(bob))
        unmarked = AddPlayerToGroup.decode(alice.receive_next(MessageType.ADD_PLAYER_TO_GROUP))
        self.assertEqual(unmarked.player_name, "Alice")
        unmarked_admin = AddPlayerToGroup.decode(alice.receive_next(MessageType.ADD_PLAYER_TO_GROUP))
        self.assertEqual(unmarked_admin.player_name, "Bob")

        self.database.set_setting(Setting.MARK_ADMINS, "true")
        carol = self.new_client("Carol", nonce=77)
        join_group(carol, LOBBY_ROOM_ID, log_on(carol))
        marked = AddPlayerToGroupWithFlags.decode(carol.receive_next(MessageType.ADD_PLAYER_TO_GROUP_WITH_FLAGS))
        self.assertEqual((marked.player_name, marked.player_flags), ("Bob", PlayerFlag.HIGHLIGHTED))
        self.assertEqual((marked.clan_id, marked.clan_name), (0, ""))
        carol.assert_nothing_arrives(MessageType.ADD_PLAYER_TO_GROUP_WITH_FLAGS)

    def test_player_leaving_is_seen_by_the_others_even_when_the_database_fails(self) -> None:
        """The database only keeps statistics; if writing the logout fails, the others must still
        see the player leave, or their clients keep a player who is gone."""
        alice = self.new_client("Alice", nonce=17)
        join_group(alice, LOBBY_ROOM_ID, log_on(alice))
        bob = self.new_client("Bob", nonce=42)
        bob_id = log_on(bob)
        join_group(bob, LOBBY_ROOM_ID, bob_id)
        with mock.patch.object(LobbyDatabase, "record_logout", side_effect=DatabaseError("database is locked")):
            bob.send(Logout())
            departure = RemovePlayerFromGroup.decode(alice.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP))
        self.assertEqual(departure.player_id, bob_id)

    def test_logon_succeeds_even_when_the_database_cannot_record_it(self) -> None:
        """The session row only feeds the statistics; if writing it fails, the game must still get its
        Result and OwnPlayer, or it waits on the logon screen for an answer that never comes."""
        alice = self.new_client("Alice", nonce=17)
        with mock.patch.object(LobbyDatabase, "record_logon", side_effect=DatabaseError("database is locked")):
            alice_id = log_on(alice)
        join_group(alice, LOBBY_ROOM_ID, alice_id)
        bob = self.new_client("Bob", nonce=42)
        join_group(bob, LOBBY_ROOM_ID, log_on(bob))
        receive_member_entry(bob, LOBBY_ROOM_ID, alice_id)
        # Without a session row there is nothing to close at the logout, and the logout still reaches Bob.
        alice.send(Logout())
        departure = RemovePlayerFromGroup.decode(bob.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP))
        self.assertEqual(departure.player_id, alice_id)

    def test_member_list_carries_the_address_the_server_saw(self) -> None:
        """Clients chat straight to each other's addresses and only know themselves as 0.0.0.0."""
        alice = self.new_client("Alice", nonce=17)
        bob = self.new_client("Bob", nonce=42)
        alice_id = log_on(alice)
        join_group(alice, LOBBY_ROOM_ID, alice_id)
        receive_member_entry(alice, LOBBY_ROOM_ID)

        bob_id = log_on(bob)
        join_group(bob, LOBBY_ROOM_ID, bob_id)
        arrival = receive_member_entry(alice, LOBBY_ROOM_ID)
        self.assertEqual((arrival.group_id, arrival.player_id), (LOBBY_ROOM_ID, bob_id))
        self.assertEqual(arrival.player_address, bob.address)
        self.assertEqual(arrival.player_name, "Bob")

    def test_host_is_put_in_its_game_under_a_server_assigned_id(self) -> None:
        """The host asks for an id with 0xffffffff and only counts itself in the game once the
        server sends the game back with parent 0; the zone sees it with the host's real IP."""
        alice = self.new_client("Alice", nonce=17)
        bob = self.new_client("Bob", nonce=42)
        alice_id = log_on(alice)
        join_group(alice, GAME_ZONE_ID, alice_id)
        bob_id = log_on(bob)
        join_group(bob, GAME_ZONE_ID, bob_id)

        open_game(bob, GAME_ZONE_ID)
        game_for_host = GroupAnnouncement.decode(bob.receive_next(MessageType.ADD_GROUP_TO_GROUP_2))
        self.assertEqual(game_for_host.parent_id, JOINED_GROUP_PARENT_ID)
        self.assertEqual(game_for_host.group_id, FIRST_GAME_GROUP_ID)
        host_entry = receive_member_entry(bob, FIRST_GAME_GROUP_ID)
        self.assertEqual((host_entry.group_id, host_entry.player_id), (FIRST_GAME_GROUP_ID, bob_id))

        game_in_zone = GroupAnnouncement.decode(alice.receive_next(MessageType.ADD_GROUP_TO_GROUP_2))
        self.assertEqual((game_in_zone.parent_id, game_in_zone.group_id), (GAME_ZONE_ID, FIRST_GAME_GROUP_ID))
        self.assertEqual(game_in_zone.game_block.host_address, ("127.0.0.1", UNKNOWN_HOST_ADDRESS[1]))

    def test_leaving_is_confirmed_so_the_group_can_be_joined_again(self) -> None:
        """The client keeps a group it left on its joined list until the server confirms; joining it
        again before that makes the client drop the connection."""
        alice = self.new_client("Alice", nonce=17)
        alice_id = log_on(alice)
        join_group(alice, LOBBY_ROOM_ID, alice_id)

        alice.send(RemovePlayerFromGroup(LOBBY_ROOM_ID, alice_id))
        confirmation = RemovePlayerFromGroup.decode(alice.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP))
        self.assertEqual((confirmation.group_id, confirmation.player_id), (LOBBY_ROOM_ID, alice_id))
        join_group(alice, LOBBY_ROOM_ID, alice_id)

    def test_host_back_from_a_match_is_taken_out_of_its_game_silently(self) -> None:
        """Back from a match the client only rejoins its rooms. The server must close the game for
        the zone but must not confirm the departure to the host: after "Zurück" in the game setup
        the client no longer lists the game, and such a confirmation hangs the game."""
        alice = self.new_client("Alice", nonce=17)
        bob = self.new_client("Bob", nonce=42)
        alice_id = log_on(alice)
        join_group(alice, GAME_ZONE_ID, alice_id)
        bob_id = log_on(bob)
        join_group(bob, GAME_ZONE_ID, bob_id)
        open_game(bob, GAME_ZONE_ID)
        bob.receive_next(MessageType.ADD_GROUP_TO_GROUP_2)
        alice.receive_next(MessageType.ADD_GROUP_TO_GROUP_2)

        # The host leaves the zone while it plays, then joins it again afterwards.
        bob.send(RemovePlayerFromGroup(GAME_ZONE_ID, bob_id))
        bob.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP)
        join_group(bob, LOBBY_ROOM_ID, bob_id)

        removal = RemoveGroupFromGroup.decode(alice.receive_next(MessageType.REMOVE_GROUP_FROM_GROUP))
        self.assertEqual((removal.parent_id, removal.group_id), (GAME_ZONE_ID, FIRST_GAME_GROUP_ID))
        # The confirmation would have come in the same block as the room, which join_group read.
        self.assertEqual(bob.unread_messages_of_type(MessageType.REMOVE_PLAYER_FROM_GROUP), [])


class ForgedDatagramTest(LobbyTestCase):
    """Someone who sends UDP datagrams with a logged on player's address, without seeing the
    answers. The server must not take those as the player's."""

    def test_unnumbered_lobby_message_from_a_player_address_is_ignored(self) -> None:
        """The game sends every lobby message in a numbered block (0x606ff0, flag 0 at every
        call). Without this, anyone could move a player between rooms or speak for them."""
        alice, alice_id = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        alice.send_raw(AddPlayerToGroup(CHAT_ZONE_GROUP_ID, alice_id, ("0.0.0.0", 0), ""))
        alice.assert_nothing_arrives(MessageType.ADD_GROUP_TO_GROUP)

    def test_new_connection_from_an_active_player_address_is_ignored(self) -> None:
        """A forged OpenConnection used to end the player's session: the game then lost its
        lobby. A client that is still sending keepalives has not started over."""
        alice, alice_id = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        bob, _ = self.player_in_room("Bob", 42, GAME_ZONE_ID)
        alice.send_raw(MessageId(1), OpenConnection(200))
        bob.assert_nothing_arrives(MessageType.REMOVE_PLAYER_FROM_GROUP)
        join_group(alice, CHAT_ZONE_GROUP_ID, alice_id)

    def test_new_connection_after_the_old_one_fell_silent_is_taken(self) -> None:
        """A game that crashed and starts again comes back from the same address with a new
        nonce; once its old connection is quiet, the server lets it in again."""
        alice, _ = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        bob, _ = self.player_in_room("Bob", 42, GAME_ZONE_ID)
        with mock.patch("s3lobby.server.RECONNECT_AFTER_QUIET_MS", 0):
            alice.send_raw(MessageId(1), OpenConnection(200))
            bob.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP)



# A 30 minute match on the game's clock (one tick every 64 ms), well over the ladder's minimum.
THIRTY_MINUTES_OF_TICKS = 28_125
OTHER_SERIAL_NUMBER = "1111-2222-3333-4444-5555"


class LadderEvaluationTest(LobbyTestCase):
    """The ladder turns a Liga match into a result once both players reported and left. Nothing
    breaks in the game without it, the game only reports; without it the server ranks nothing."""

    def play_liga_match(self, bob_serial: str) -> int:
        """Alice hosts a Liga game, Bob (on bob_serial) joins, both report Alice's win after 30
        minutes, both leave the game. Returns the game's record id."""
        alice, alice_id = self.player_in_room("Alice", 17, GAME_ZONE_ID)
        bob = self.new_client("Bob", 42, bob_serial)
        bob_id = log_on(bob)
        join_group(bob, GAME_ZONE_ID, bob_id)
        open_game(alice, GAME_ZONE_ID, LadderType.INDIVIDUAL, game_description(is_ladder_game=True))
        game = receive_announcement(alice)
        # The host is told its game's id first (Result 0xc), before any report is answered.
        alice.receive_next(MessageType.RESULT)
        bob.send(AddPlayerToGroup(game.group_id, bob_id, ("0.0.0.0", 0), ""))
        receive_announcement(bob)
        slots = (MatchReportPlayer(0, alice_id, 0, 0, 0, 0), MatchReportPlayer(0, bob_id, 1, 0, 1, 1))
        report = GameStatus(game.group_id, GameStatusFlag.LADDER_GAME, 0, THIRTY_MINUTES_OF_TICKS, "\\Random", slots)
        # Each reports, then leaves the game; the server confirms the leave, and the game closes
        # with its last player.
        self.report_codes = []
        for client, player_id in ((alice, alice_id), (bob, bob_id)):
            client.send(report)
            self.report_codes.append(Result.decode(client.receive_next(MessageType.RESULT)).code)
            client.send(RemovePlayerFromGroup(game.group_id, player_id))
            client.receive_next(MessageType.REMOVE_PLAYER_FROM_GROUP)
        self.alice, self.bob = alice, bob
        [record] = self.database.list_recent_games(limit=1)
        return record.game_id

    def test_a_reported_liga_match_counts_for_its_winner(self) -> None:
        """Both reports agree, the match was long enough, the players use different copies of the
        game: Alice's win counts."""
        game_id = self.play_liga_match(OTHER_SERIAL_NUMBER)
        self.server.evaluate_ladder_games()
        result = self.database.find_ladder_result(game_id)
        self.assertEqual(result.evaluation.state, LadderState.COUNTED)
        winners = [player.account_id for player in result.evaluation.players if player.has_won]
        self.assertEqual(winners, [self.database.find_account("Alice").account_id])

    def test_a_switched_off_ladder_tells_the_game_nothing_counts(self) -> None:
        """Until the admins switch the ladder on, the game shows "not valid for ladder" (0x540b89),
        as before the ladder existed, while the server still evaluates the match."""
        self.play_liga_match(OTHER_SERIAL_NUMBER)
        self.assertEqual(self.report_codes, [ResultCode.GAME_NOT_VALID_FOR_LADDER] * 2)

    def test_a_switched_on_ladder_stores_the_report_and_tells_the_players(self) -> None:
        """With the ladder on, the game shows "Game status stored at the Lobby server." during the
        match (0x540a3e), and after the evaluation both players hear how their ratings moved; the
        game itself has no place to show it."""
        self.database.set_setting(Setting.LADDER_ENABLED, "true")
        self.play_liga_match(OTHER_SERIAL_NUMBER)
        self.assertEqual(self.report_codes, [ResultCode.GAME_STATUS_STORED] * 2)
        self.server.evaluate_ladder_games()
        for client in (self.alice, self.bob):
            line = ChatCommandAnswer.decode(client.receive_next(MessageType.CHAT_COMMAND_ANSWER))
            self.assertEqual(line.lines[0], "Liga: Alice beat Bob. Alice 1016 (+16), Bob 984 (-16).")

    def store_alice_beats_bob(self, decided_by: int | None = None) -> None:
        """A counted Liga result in the database, Alice over Bob, decided by an admin when given."""
        alice_id = self.database.find_account("Alice").account_id
        bob_id = self.database.find_account("Bob").account_id
        game_id = self.database.record_game_opened("Duel", alice_id, ladder_type=LadderType.INDIVIDUAL)
        self.database.store_ladder_result(
            Evaluation(
                game_id,
                LadderType.INDIVIDUAL,
                LadderState.COUNTED,
                "decided by an admin" if decided_by else "",
                winning_team=0,
                played_at=self.database.now(),
                players=(
                    ResultPlayer(alice_id, None, 0, 0, 0, True, False),
                    ResultPlayer(bob_id, None, 1, 0, 1, False, False),
                ),
            ),
            decided_by_account_id=decided_by,
        )

    def test_the_news_carries_the_standings_while_the_ladder_is_on(self) -> None:
        """The game has no screen for standings; the Home tab's news is where every player looks
        after logging on, so the top five go under the admins' text there."""
        self.database.set_setting(Setting.LADDER_ENABLED, "true")
        self.database.set_news(NEWS_AUDIENCE_GERMAN, "Willkommen", "Viel Spass!")
        self.store_alice_beats_bob()
        alice, _ = self.player_in_room("Alice", 17, LOBBY_ROOM_ID)
        alice.send(GetTextMessage(NEWS_TEXT_ID_BY_AUDIENCE[NEWS_AUDIENCE_GERMAN]))
        news = TextMessage.decode(alice.receive_next(MessageType.TEXT_MESSAGE))
        self.assertEqual(news.lines[0], "Viel Spass!")
        self.assertIn("Liga (unendliche Saison): 1. Alice 1016, 2. Bob 984.", news.lines)

    def test_a_player_hears_their_place_and_a_decision_at_logon(self) -> None:
        """Without a line at logon, a player learns of an admin's decision on their disputed game
        only by asking; with ladder_logon_line on they also hear their place."""
        self.database.set_setting(Setting.LADDER_ENABLED, "true")
        self.database.set_setting(Setting.LADDER_LOGON_LINE, "true")
        admin_id = self.database.find_account("Carol").account_id
        self.store_alice_beats_bob(decided_by=admin_id)
        alice = self.new_client("Alice", 17)
        log_on(alice)
        answer = ChatCommandAnswer.decode(alice.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertEqual(answer.lines, ("Liga: 1016, place 1 of 2.", "Ladder: an admin decided Duel: Alice won."))
        # A second logon tells the place again, but the decision only once.
        alice.send(Logout())
        alice.close_connection()
        again = self.new_client("Alice", 18)
        log_on(again)
        answer = ChatCommandAnswer.decode(again.receive_next(MessageType.CHAT_COMMAND_ANSWER))
        self.assertEqual(answer.lines, ("Liga: 1016, place 1 of 2.",))

    def test_two_copies_of_one_installation_do_not_count(self) -> None:
        """Bob logs on with Alice's serial number: one installation against itself is void."""
        game_id = self.play_liga_match(SERIAL_NUMBER)
        self.server.evaluate_ladder_games()
        result = self.database.find_ladder_result(game_id)
        self.assertEqual(result.evaluation.reason, "players on both sides share one game installation")


if __name__ == "__main__":
    unittest.main()
