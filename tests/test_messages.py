"""Byte layouts: headers, blocks, socket addresses and every message class.

The expected bytes in these tests are the ones the real game sent or accepted in a recorded
session, so a failure here means the server would no longer talk to the game.
"""

from __future__ import annotations

import unittest

from s3lobby.constants import (
    NO_WINNING_TEAM,
    U32_SIZE,
    GameStatusFlag,
    GroupFlag,
    LadderType,
    MatchPlayerFlag,
    MessageType,
    PlayerFlag,
    ResultCode,
)
from s3lobby.messages import (
    MATCH_REPORT_PLAYER_SIZE,
    AcknowledgeMessages,
    AddPlayerToGroup,
    AddPlayerToGroupWithClan,
    AddPlayerToGroupWithFlags,
    ChatCommandAnswer,
    ChatMessage,
    CheckFileVersion,
    ImageList,
    GameBlock,
    GameDescription,
    GameSettings,
    GameStatus,
    GroupAnnouncement,
    LobbyServerEntry,
    LobbyServerList,
    Logon,
    MatchReportPlayer,
    MinMissingAcknowledge,
    OwnPlayer,
    RemoveGroupFromGroup,
    RemovePlayerFromGroup,
    Result,
    TextMessage,
)
from s3lobby.wire import (
    FieldReader,
    ProtocolError,
    RawMessage,
    decode_block,
    decode_socket_address,
    encode_block,
    encode_socket_address,
    readable_text,
)

# Recorded from the GOG game: a host opening a game in zone 30 (AddGroupToGroup2, flags 0x840401).
RECORDED_GAME_ANNOUNCEMENT = bytes.fromhex(
    "1e000000ffffffff0104840002000000506c6179657227732067616d6500000002000d0f000000000000000000000000"
    "a6ba4159c0b5c043b074ca816a1b944ea000000057fe443c219f433d010000000100000018000000000018000200000000000000"
    "010000005c52616e646f6d00"
)
# Recorded from the GOG game: a logon by "Player" with password "1234".
RECORDED_LOGON = bytes.fromhex(
    "0e000000506c6179657200313233340000000000303439372d323538342d323139342d343335362d3233333600"
)


class HeaderAndBlockTest(unittest.TestCase):
    """The message header and the block of messages one datagram carries."""

    def test_header_counts_itself_in_the_size(self) -> None:
        """The header is the type as 16 bits, then the size of the whole message, header included; a size
        without the header would cut every message short for the game."""
        encoded = RawMessage(MessageType.RESULT, bytes(8)).encode()
        self.assertEqual(encoded[:4], bytes([0x09, 0x00, 0x0C, 0x00]))

    def test_block_round_trip_keeps_order_and_unknown_types(self) -> None:
        """A block decodes into the same messages in the same order, and a type the server does not know
        passes through unchanged instead of failing the block."""
        messages = [RawMessage(MessageType.MESSAGE_ID, bytes(4)), RawMessage(0x77, b"abc"), RawMessage(0x0A)]
        self.assertEqual(decode_block(encode_block(messages)), messages)

    def test_size_pointing_past_the_datagram_is_rejected(self) -> None:
        """A header whose size runs past the end of the datagram raises ProtocolError rather than reading
        beyond the data."""
        with self.assertRaises(ProtocolError):
            decode_block(bytes([0x09, 0x00, 0x0C, 0x00, 0x01]))

    def test_cut_off_header_is_rejected(self) -> None:
        """Bytes left over after the last message that cannot hold a whole header raise ProtocolError."""
        valid = RawMessage(MessageType.LOGOUT).encode()
        with self.assertRaises(ProtocolError):
            decode_block(valid + b"\x09\x00")


class FieldTest(unittest.TestCase):
    """The field layouts inside messages: socket addresses and text."""

    def test_socket_address_has_the_windows_layout(self) -> None:
        """Addresses go on the wire as a Windows sockaddr_in, 16 bytes: the game connects to exactly what it
        reads there."""
        # Family 2 little endian, port 3343 and 127.0.0.1 in network order, 8 zero bytes.
        encoded = encode_socket_address(("127.0.0.1", 3343))
        self.assertEqual(encoded.hex(), "02000d0f7f0000010000000000000000")
        self.assertEqual(decode_socket_address(encoded), ("127.0.0.1", 3343))

    def test_text_keeps_bytes_undefined_in_windows_1252(self) -> None:
        """Text from one client is relayed to the others byte for byte, also bytes Windows-1252 leaves
        undefined, so a name reaches them as its owner wrote it."""
        # 0x81 has no character in Windows-1252; relaying a name must still give the same bytes.
        reader = FieldReader(b"A\x81B\0")
        text = reader.read_string()
        self.assertEqual(group_announcement_named(text).encode().body[16:20], b"A\x81B\0")

    def test_readable_text_replaces_bytes_the_code_page_leaves_undefined(self) -> None:
        """Such bytes ride the wire as placeholders, which UTF-8 cannot store: the database and the
        live lobby would fail on a game named with one."""
        relayed = FieldReader(b"Game\x81X\x00").read_string()
        self.assertEqual(readable_text(relayed), "Game\ufffdX")
        self.assertEqual(readable_text("Römer"), "Römer")

    def test_reading_past_the_end_raises_protocol_error(self) -> None:
        """A number cut short or a string without its terminating zero raises ProtocolError, which the server
        handles, instead of another exception."""
        with self.assertRaises(ProtocolError):
            FieldReader(b"\x01\x02").read_u32()
        with self.assertRaises(ProtocolError):
            FieldReader(b"no terminator").read_string()


def group_announcement_named(name: str) -> GroupAnnouncement:
    """A plain AddGroupToGroup whose only text is name."""
    return GroupAnnouncement(MessageType.ADD_GROUP_TO_GROUP, 0, 1, 0, 0, name, "", "")


class MessageLayoutTest(unittest.TestCase):
    """The byte layout of the message classes, checked against recorded datagrams where there are some."""

    def test_recorded_game_announcement_survives_a_round_trip_byte_for_byte(self) -> None:
        """The game's own AddGroupToGroup2 decodes into the expected fields and encodes back to the same
        bytes, so a game relayed by the server reaches the others exactly as its host sent it."""
        raw = RawMessage(MessageType.ADD_GROUP_TO_GROUP_2, RECORDED_GAME_ANNOUNCEMENT)
        announcement = GroupAnnouncement.decode(raw)
        self.assertEqual((announcement.parent_id, announcement.group_id), (30, 0xFFFFFFFF))
        self.assertTrue(announcement.flags & GroupFlag.HAS_GAME_BLOCK)
        self.assertTrue(announcement.flags & GroupFlag.HAS_DESCRIPTION)
        self.assertEqual(announcement.name, "Player's game")
        self.assertEqual(announcement.game_block.host_address, ("0.0.0.0", 3343))
        self.assertEqual(announcement.encode(), raw)

    def test_recorded_game_announcement_holds_a_ladder_game_on_a_random_map(self) -> None:
        """The recorded host had picked "Ladder: 1 on 1" on a random map with two teams."""
        raw = RawMessage(MessageType.ADD_GROUP_TO_GROUP_2, RECORDED_GAME_ANNOUNCEMENT)
        announcement = GroupAnnouncement.decode(raw)
        settings = GameSettings.from_game_block(announcement.game_block)
        self.assertEqual(settings.session_guid, bytes.fromhex("a6ba4159c0b5c043b074ca816a1b944e"))
        self.assertEqual(settings.game_version, 160)
        self.assertEqual(settings.program_checksum, 0x3D439F21_3C44FE57)
        self.assertTrue(settings.allows_amazons)
        self.assertEqual(settings.ladder_type, LadderType.INDIVIDUAL)
        self.assertTrue(settings.is_ladder_game)

        description = GameDescription.decode(announcement.description)
        self.assertEqual(description, GameDescription(2, 0, True, "\\Random"))
        self.assertEqual(description.encode(), announcement.description)

    def test_short_game_block_has_no_amazons_value_or_ladder_type(self) -> None:
        """AddGroupToGroup and UpdateGroup carry three values after the session id (0x5ea320)."""
        version_and_checksum = bytes.fromhex("a0000000" "01000000" "02000000")
        game_block = GameBlock(host_address=("10.0.0.2", 3343), remainder=bytes(16) + version_and_checksum)
        settings = GameSettings.from_game_block(game_block)
        self.assertEqual((settings.game_version, settings.program_checksum), (160, 0x2_00000001))
        self.assertIsNone(settings.allows_amazons)
        self.assertIsNone(settings.ladder_type)
        self.assertFalse(settings.is_ladder_game)

    def test_description_that_is_not_one_game_description_is_refused(self) -> None:
        """A description that is empty, too short, too long, of another message type, or with a map name that
        does not end where the message does is refused, since only well formed ones are relayed."""
        valid = GameDescription(4, 2, False, "Elbe").encode()
        other_type = RawMessage(MessageType.OWN_PLAYER, valid[4:]).encode()
        for description in (b"", bytes(3), valid + valid, other_type, valid[:-1], valid[:-1] + b"x\0" + b"\0"):
            with self.subTest(description=description):
                with self.assertRaises(ProtocolError):
                    GameDescription.decode(description)

    def test_match_report_lists_every_slot(self) -> None:
        """A GameStatus round trips with every slot of the match, and tells ladder games, a missing winner and
        computer players apart for the game reports."""
        human = MatchReportPlayer(flags=0, lobby_player_id=7, race=0, unused=0, slot=0, team=0)
        computer = MatchReportPlayer(flags=MatchPlayerFlag.COMPUTER, lobby_player_id=0, race=3, unused=0, slot=1, team=1)
        report = GameStatus(0x100000, GameStatusFlag.LADDER_GAME, NO_WINNING_TEAM, 12, "Elbe", (human, computer))
        decoded = GameStatus.decode(report.encode())
        self.assertEqual(decoded, report)
        self.assertTrue(decoded.is_ladder_game)
        self.assertFalse(decoded.has_winner)
        self.assertEqual([player.is_computer for player in decoded.players], [False, True])

    def test_match_report_whose_count_does_not_fit_is_refused(self) -> None:
        """A player count that does not match the slots the message holds is refused, so a forged count can
        neither make the server read past the data nor drop slots from the stored report."""
        report = GameStatus(0x100000, 0, 0, 12, "Elbe", (MatchReportPlayer(0, 7, 0, 0, 0, 0),)).encode()
        player_count_offset = len(report.body) - MATCH_REPORT_PLAYER_SIZE - U32_SIZE
        for forged_count in (0, 2, 0xFFFFFFFF):
            body = bytearray(report.body)
            body[player_count_offset:player_count_offset + U32_SIZE] = forged_count.to_bytes(U32_SIZE, "little")
            with self.subTest(forged_count=forged_count):
                with self.assertRaises(ProtocolError):
                    GameStatus.decode(RawMessage(MessageType.GAME_STATUS, bytes(body)))

    def test_recorded_logon_decodes(self) -> None:
        """The game's own Logon decodes into its fields, and the password stays out of the message's repr,
        which can end up in a log."""
        logon = Logon.decode(RawMessage(MessageType.LOGON, RECORDED_LOGON))
        self.assertEqual(logon.protocol_version, 14)
        self.assertEqual(logon.player_name, "Player")
        self.assertEqual(logon.password, "1234")
        self.assertEqual(logon.serial_number, "0497-2584-2194-4356-2336")
        self.assertNotIn("1234", repr(logon))

    def test_version_check_names_the_language_of_the_game(self) -> None:
        """The language index the game sends in its version check is named for the log, and an index without a
        known name still gets a readable one."""
        self.assertEqual(CheckFileVersion(160, 4, 0).language_name, "German")
        self.assertEqual(CheckFileVersion(160, 4, 7).language_name, "Japanese")
        self.assertEqual(CheckFileVersion(160, 4, 8).language_name, "language 8")

    def test_sizes_the_client_asserts(self) -> None:
        """These server messages have the exact sizes the client asserts when it reads them; another size
        makes the game drop the connection."""
        cases = {
            Result(ResultCode.LOGON_ACCEPTED): 12,
            OwnPlayer(1): 8,
            MinMissingAcknowledge(1): 8,
            RemovePlayerFromGroup(3, 1): 12,
            RemoveGroupFromGroup(30, 0x10000): 12,
        }
        for message, expected_size in cases.items():
            with self.subTest(message=message):
                self.assertEqual(len(message.encode().encode()), expected_size)

    def test_min_missing_acknowledge_never_carries_zero(self) -> None:
        """MinMissingAcknowledge with 0 is neither sent nor taken: for 0 the game leaves the body out and its
        own size assert then fails (0x603880, 0x6038f0)."""
        with self.assertRaises(ValueError):
            MinMissingAcknowledge(0).encode()
        with self.assertRaises(ProtocolError):
            MinMissingAcknowledge.decode(RawMessage(MessageType.MIN_MISSING_ACKNOWLEDGE, bytes(4)))

    def test_every_message_class_round_trips(self) -> None:
        """Every message class decodes what it encodes into an equal message."""
        game_block = GameBlock(host_address=("10.0.0.2", 3343), remainder=bytes(36))
        messages = [
            AcknowledgeMessages((1, 2, 5)),
            AcknowledgeMessages(()),
            CheckFileVersion(160, 4, 0),
            LobbyServerList((LobbyServerEntry("10.0.0.1:3344", "LAN Lobby"),)),
            Logon(14, "Player", "1234", 0, "0497-2584-2194-4356-2336"),
            AddPlayerToGroup(3, 1, ("10.0.0.2", 3343), "Player"),
            AddPlayerToGroupWithClan(3, 1, ("10.0.0.2", 3343), "Player", 4, "Romans"),
            AddPlayerToGroupWithFlags(3, 1, ("10.0.0.2", 3343), "Player", 0, "", PlayerFlag.HIGHLIGHTED),
            GroupAnnouncement(MessageType.ADD_GROUP_TO_GROUP_2, 30, 7, 0x400, 2, "Game", "", "", game_block),
            ChatMessage(MessageType.CHAT_MESSAGE_2, 3, 1, "hello", kind=0),
            ChatMessage(MessageType.CHAT_MESSAGE, 3, 1, "hello"),
            ChatCommandAnswer(3, ("first line", "second line")),
            ImageList(30),
            TextMessage(5, "Title", ("line",)),
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(type(message).decode(message.encode()), message)

    def test_decoding_the_wrong_type_is_refused(self) -> None:
        """A class refuses to decode a message of another type rather than reading its bytes with the wrong
        layout."""
        with self.assertRaises(ProtocolError):
            Result.decode(RawMessage(MessageType.OWN_PLAYER, bytes(4)))


if __name__ == "__main__":
    unittest.main()
