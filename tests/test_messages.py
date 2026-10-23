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
    def test_header_counts_itself_in_the_size(self) -> None:
        encoded = RawMessage(MessageType.RESULT, bytes(8)).encode()
        self.assertEqual(encoded[:4], bytes([0x09, 0x00, 0x0C, 0x00]))

    def test_block_round_trip_keeps_order_and_unknown_types(self) -> None:
        messages = [RawMessage(MessageType.MESSAGE_ID, bytes(4)), RawMessage(0x77, b"abc"), RawMessage(0x0A)]
        self.assertEqual(decode_block(encode_block(messages)), messages)

    def test_size_pointing_past_the_datagram_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            decode_block(bytes([0x09, 0x00, 0x0C, 0x00, 0x01]))

    def test_cut_off_header_is_rejected(self) -> None:
        valid = RawMessage(MessageType.LOGOUT).encode()
        with self.assertRaises(ProtocolError):
            decode_block(valid + b"\x09\x00")


class FieldTest(unittest.TestCase):
    def test_socket_address_has_the_windows_layout(self) -> None:
        # Family 2 little endian, port 3343 and 127.0.0.1 in network order, 8 zero bytes.
        encoded = encode_socket_address(("127.0.0.1", 3343))
        self.assertEqual(encoded.hex(), "02000d0f7f0000010000000000000000")
        self.assertEqual(decode_socket_address(encoded), ("127.0.0.1", 3343))

    def test_text_keeps_bytes_undefined_in_windows_1252(self) -> None:
        # 0x81 has no character in Windows-1252; relaying a name must still give the same bytes.
        reader = FieldReader(b"A\x81B\0")
        text = reader.read_string()
        self.assertEqual(group_announcement_named(text).encode().body[16:20], b"A\x81B\0")

    def test_reading_past_the_end_raises_protocol_error(self) -> None:
        with self.assertRaises(ProtocolError):
            FieldReader(b"\x01\x02").read_u32()
        with self.assertRaises(ProtocolError):
            FieldReader(b"no terminator").read_string()


def group_announcement_named(name: str) -> GroupAnnouncement:
    return GroupAnnouncement(MessageType.ADD_GROUP_TO_GROUP, 0, 1, 0, 0, name, "", "")


class MessageLayoutTest(unittest.TestCase):
    def test_recorded_game_announcement_survives_a_round_trip_byte_for_byte(self) -> None:
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
        valid = GameDescription(4, 2, False, "Elbe").encode()
        other_type = RawMessage(MessageType.OWN_PLAYER, valid[4:]).encode()
        for description in (b"", bytes(3), valid + valid, other_type, valid[:-1], valid[:-1] + b"x\0" + b"\0"):
            with self.subTest(description=description):
                with self.assertRaises(ProtocolError):
                    GameDescription.decode(description)

    def test_match_report_lists_every_slot(self) -> None:
        human = MatchReportPlayer(flags=0, lobby_player_id=7, race=0, unused=0, slot=0, team=0)
        computer = MatchReportPlayer(flags=MatchPlayerFlag.COMPUTER, lobby_player_id=0, race=3, unused=0, slot=1, team=1)
        report = GameStatus(0x100000, GameStatusFlag.LADDER_GAME, NO_WINNING_TEAM, 12, "Elbe", (human, computer))
        decoded = GameStatus.decode(report.encode())
        self.assertEqual(decoded, report)
        self.assertTrue(decoded.is_ladder_game)
        self.assertFalse(decoded.has_winner)
        self.assertEqual([player.is_computer for player in decoded.players], [False, True])

    def test_match_report_whose_count_does_not_fit_is_refused(self) -> None:
        report = GameStatus(0x100000, 0, 0, 12, "Elbe", (MatchReportPlayer(0, 7, 0, 0, 0, 0),)).encode()
        player_count_offset = len(report.body) - MATCH_REPORT_PLAYER_SIZE - U32_SIZE
        for forged_count in (0, 2, 0xFFFFFFFF):
            body = bytearray(report.body)
            body[player_count_offset:player_count_offset + U32_SIZE] = forged_count.to_bytes(U32_SIZE, "little")
            with self.subTest(forged_count=forged_count):
                with self.assertRaises(ProtocolError):
                    GameStatus.decode(RawMessage(MessageType.GAME_STATUS, bytes(body)))

    def test_recorded_logon_decodes(self) -> None:
        logon = Logon.decode(RawMessage(MessageType.LOGON, RECORDED_LOGON))
        self.assertEqual(logon.protocol_version, 14)
        self.assertEqual(logon.player_name, "Player")
        self.assertEqual(logon.password, "1234")
        self.assertEqual(logon.serial_number, "0497-2584-2194-4356-2336")
        self.assertNotIn("1234", repr(logon))

    def test_version_check_names_the_language_of_the_game(self) -> None:
        self.assertEqual(CheckFileVersion(160, 4, 0).language_name, "German")
        self.assertEqual(CheckFileVersion(160, 4, 7).language_name, "Japanese")
        self.assertEqual(CheckFileVersion(160, 4, 8).language_name, "language 8")

    def test_sizes_the_client_asserts(self) -> None:
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
        with self.assertRaises(ValueError):
            MinMissingAcknowledge(0).encode()
        with self.assertRaises(ProtocolError):
            MinMissingAcknowledge.decode(RawMessage(MessageType.MIN_MISSING_ACKNOWLEDGE, bytes(4)))

    def test_every_message_class_round_trips(self) -> None:
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
        with self.assertRaises(ProtocolError):
            Result.decode(RawMessage(MessageType.OWN_PLAYER, bytes(4)))


if __name__ == "__main__":
    unittest.main()
