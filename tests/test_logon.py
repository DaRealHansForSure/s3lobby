"""Logon decisions: who gets in, and which of the game's refusals the others see."""

from __future__ import annotations

import unittest
from datetime import timedelta

from s3lobby.constants import (
    ACCESS_CODE_CHECK_BURST,
    ACCESS_CODE_CHECKS_PER_SECOND,
    DEVELOPMENT_PASSWORD,
    LOGON_FAILURE_BURST,
    LOGON_PROTOCOL_VERSION,
    LogonFlag,
    ResultCode,
)
from s3lobby.database import LobbyDatabase, Setting, utc_now
from s3lobby.logon import AcceptedLogon, LobbyOccupancy, LogonCheck, RefusedLogon
from s3lobby.messages import Logon

ACCESS_CODE = "TESTCODE2345"
SERIAL_NUMBER = "0497-2584-2194-4356-2336"
CLIENT_IP = "10.0.0.2"
EMPTY_LOBBY = LobbyOccupancy(player_count=0, casefolded_player_names=frozenset(), serial_numbers=frozenset())


def logon(name: str = "Alice", access_code: str = ACCESS_CODE, flags: int = 0) -> Logon:
    """A Logon as the game sends it, with the shared serial number."""
    return Logon(LOGON_PROTOCOL_VERSION, name, access_code, flags, SERIAL_NUMBER)


class FixedClock:
    """A clock for LogonCheck that only moves when the test sets now_seconds."""

    def __init__(self) -> None:
        """Start the clock at 0 seconds."""
        self.now_seconds = 0.0

    def __call__(self) -> float:
        """The time the test set, in seconds."""
        return self.now_seconds


class LogonCheckTest(unittest.TestCase):
    """LogonCheck's decisions against an in-memory database holding Alice with her access code."""

    def setUp(self) -> None:
        """Alice's account with ACCESS_CODE, and a logon check on a clock the test controls."""
        self.database = LobbyDatabase(":memory:")
        self.alice = self.database.create_account("Alice")
        self.database.add_access_code(self.alice.account_id, ACCESS_CODE)
        self.clock = FixedClock()
        self.check = LogonCheck(self.database, clock_seconds=self.clock)

    def tearDown(self) -> None:
        """Close the in-memory database."""
        self.database.close()

    def assert_refused_with(self, decision: AcceptedLogon | RefusedLogon, result_code: ResultCode) -> None:
        """Fail unless the decision is a refusal with this result code; the refusal's reason goes into the
        failure message."""
        self.assertIsInstance(decision, RefusedLogon)
        self.assertEqual(decision.result_code, result_code, decision.reason)

    def test_account_with_its_access_code_gets_in(self) -> None:
        """The right name and access code get in, and without the clan box ticked no clan is shown."""
        decision = self.check.decide(logon(), CLIENT_IP, EMPTY_LOBBY)
        self.assertIsInstance(decision, AcceptedLogon)
        self.assertEqual(decision.account.name, "Alice")
        self.assertIsNone(decision.shown_clan)

    def test_name_is_matched_without_regard_to_case(self) -> None:
        """The database compares names without regard to case, so a name typed in another case finds the
        account under its stored spelling."""
        decision = self.check.decide(logon(name="alice"), CLIENT_IP, EMPTY_LOBBY)
        self.assertEqual(decision.account.name, "Alice")

    def test_access_code_is_matched_without_regard_to_case(self) -> None:
        """Access codes are compared in upper case, so a player typing one in lower case still gets in."""
        decision = self.check.decide(logon(access_code=ACCESS_CODE.lower()), CLIENT_IP, EMPTY_LOBBY)
        self.assertIsInstance(decision, AcceptedLogon)

    def test_unknown_account_and_wrong_code_get_the_same_refusal(self) -> None:
        """An unknown name and a wrong code get the same result code, so a guesser cannot learn which names
        have accounts."""
        self.assert_refused_with(
            self.check.decide(logon(name="Mallory"), CLIENT_IP, EMPTY_LOBBY), ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE
        )
        self.assert_refused_with(
            self.check.decide(logon(access_code="WRONG"), CLIENT_IP, EMPTY_LOBBY), ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE
        )

    def test_create_player_from_the_game_is_refused_even_for_a_free_name(self) -> None:
        """The game's "create player" button is not offered: accounts are made on the server side, so the
        request is refused and no account is created."""
        decision = self.check.decide(logon(name="Newcomer", flags=LogonFlag.CREATE_PLAYER), CLIENT_IP, EMPTY_LOBBY)
        self.assert_refused_with(decision, ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE)
        self.assertIsNone(self.database.find_account("Newcomer"))

    def test_name_breaking_the_game_rule_gets_its_own_text(self) -> None:
        """A name the game itself would not allow gets the game's own text for an invalid name instead of the
        generic refusal."""
        self.assert_refused_with(self.check.decide(logon(name="Alice2"), CLIENT_IP, EMPTY_LOBBY), ResultCode.INVALID_PLAYER_NAME)

    def test_repeated_failures_lock_out_even_the_right_code_for_a_while(self) -> None:
        """After LOGON_FAILURE_BURST failures from one address even the right code is refused until the budget
        refills after about 15 minutes, so guessing access codes stays hopeless."""
        for _ in range(LOGON_FAILURE_BURST):
            self.check.decide(logon(access_code="GUESS"), CLIENT_IP, EMPTY_LOBBY)
        self.assert_refused_with(self.check.decide(logon(), CLIENT_IP, EMPTY_LOBBY), ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE)
        self.clock.now_seconds += 15 * 60
        self.assertIsInstance(self.check.decide(logon(), CLIENT_IP, EMPTY_LOBBY), AcceptedLogon)

    def test_failures_from_other_addresses_do_not_lock_the_player_out(self) -> None:
        """Every player sees an admin's name in the lobby; failing with it on purpose from elsewhere
        must not keep the admin out."""
        for attempt in range(LOGON_FAILURE_BURST + 1):
            self.check.decide(logon(access_code="GUESS"), f"10.0.0.{attempt + 1}", EMPTY_LOBBY)
        self.assertIsInstance(self.check.decide(logon(), CLIENT_IP, EMPTY_LOBBY), AcceptedLogon)

    def test_access_code_checks_are_budgeted_for_all_clients_together(self) -> None:
        """Each check costs a scrypt hash on the event loop; a flood from many addresses would
        otherwise stall every player."""
        for attempt in range(ACCESS_CODE_CHECK_BURST):
            self.check.decide(logon(access_code="GUESS"), f"10.0.{attempt // 250}.{attempt % 250}", EMPTY_LOBBY)
        refusal = self.check.decide(logon(), CLIENT_IP, EMPTY_LOBBY)
        self.assert_refused_with(refusal, ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE)
        self.clock.now_seconds += ACCESS_CODE_CHECK_BURST / ACCESS_CODE_CHECKS_PER_SECOND
        self.assertIsInstance(self.check.decide(logon(), CLIENT_IP, EMPTY_LOBBY), AcceptedLogon)

    def test_development_mode_accepts_its_password_only_from_this_network(self) -> None:
        """For testing with the game without copying codes; a client from the internet must never
        get in with a password everybody knows, even if the mode is left on by mistake."""
        development_check = LogonCheck(self.database, clock_seconds=self.clock, is_development_mode=True)
        accepted = development_check.decide(logon(access_code=DEVELOPMENT_PASSWORD), CLIENT_IP, EMPTY_LOBBY)
        self.assertIsInstance(accepted, AcceptedLogon)
        from_the_internet = development_check.decide(logon(access_code=DEVELOPMENT_PASSWORD), "84.75.10.4", EMPTY_LOBBY)
        self.assert_refused_with(from_the_internet, ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE)
        unknown_name = development_check.decide(logon(name="Nobody", access_code=DEVELOPMENT_PASSWORD), CLIENT_IP, EMPTY_LOBBY)
        self.assert_refused_with(unknown_name, ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE)

    def test_development_password_means_nothing_without_the_mode(self) -> None:
        """Outside development mode DEVELOPMENT_PASSWORD is just a wrong code."""
        refusal = self.check.decide(logon(access_code=DEVELOPMENT_PASSWORD), CLIENT_IP, EMPTY_LOBBY)
        self.assert_refused_with(refusal, ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE)

    def test_banned_account_is_told_so_only_after_a_correct_code(self) -> None:
        """A wrong code gets the generic refusal even for a banned account, so a guesser cannot learn of the
        ban; only the right code gets the ban's result code."""
        self.database.ban_account(self.alice.account_id, utc_now() + timedelta(hours=1), "spam")
        self.assert_refused_with(
            self.check.decide(logon(access_code="WRONG"), CLIENT_IP, EMPTY_LOBBY), ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE
        )
        self.assert_refused_with(self.check.decide(logon(), CLIENT_IP, EMPTY_LOBBY), ResultCode.BANNED)

    def test_full_server_refuses_new_players(self) -> None:
        """With MAX_PLAYERS players online, a further logon is refused with the game's text for a full server.
        """
        self.database.set_setting(Setting.MAX_PLAYERS, "2")
        two_online = LobbyOccupancy(2, frozenset({"bob", "carol"}), frozenset())
        self.assert_refused_with(self.check.decide(logon(), CLIENT_IP, two_online), ResultCode.SERVER_FULL)

    def test_second_logon_of_an_account_is_refused(self) -> None:
        """An account already online cannot log on a second time, in any case of its name."""
        alice_online = LobbyOccupancy(1, frozenset({"alice"}), frozenset())
        self.assert_refused_with(self.check.decide(logon(), CLIENT_IP, alice_online), ResultCode.ALREADY_LOGGED_ON)

    def test_shared_serial_number_only_counts_when_the_setting_asks_for_it(self) -> None:
        """A serial number already online is allowed by default and refused only when the
        unique_serial_numbers setting is on."""
        serial_in_use = LobbyOccupancy(1, frozenset({"bob"}), frozenset({SERIAL_NUMBER}))
        self.assertIsInstance(self.check.decide(logon(), CLIENT_IP, serial_in_use), AcceptedLogon)
        self.database.set_setting(Setting.UNIQUE_SERIAL_NUMBERS, "true")
        self.assert_refused_with(self.check.decide(logon(), CLIENT_IP, serial_in_use), ResultCode.ALREADY_LOGGED_ON)

    def test_development_mode_lets_copies_of_one_installation_share_a_serial(self) -> None:
        """Two copies of the game on one machine send the same serial; testing with them needs both
        online at once, under different accounts."""
        self.database.set_setting(Setting.UNIQUE_SERIAL_NUMBERS, "true")
        serial_in_use = LobbyOccupancy(1, frozenset({"bob"}), frozenset({SERIAL_NUMBER}))
        development_check = LogonCheck(self.database, clock_seconds=self.clock, is_development_mode=True)
        self.assertIsInstance(development_check.decide(logon(), CLIENT_IP, serial_in_use), AcceptedLogon)

    def test_clan_box_needs_a_clan(self) -> None:
        """Ticking the clan box in the logon dialog without being in a clan is refused with the game's own
        text; a clan member gets their clan shown."""
        clan_logon = logon(flags=LogonFlag.SHOW_AS_CLAN_MEMBER)
        self.assert_refused_with(self.check.decide(clan_logon, CLIENT_IP, EMPTY_LOBBY), ResultCode.NOT_A_CLAN_MEMBER)
        romans = self.database.create_clan("Romans")
        self.database.set_clan_of_account(self.alice.account_id, romans.clan_id)
        self.assertEqual(self.check.decide(clan_logon, CLIENT_IP, EMPTY_LOBBY).shown_clan, romans)

    def test_clan_member_without_the_box_shows_no_clan(self) -> None:
        """A clan member who leaves the clan box unticked is shown without a clan, as they chose."""
        romans = self.database.create_clan("Romans")
        self.database.set_clan_of_account(self.alice.account_id, romans.clan_id)
        self.assertIsNone(self.check.decide(logon(), CLIENT_IP, EMPTY_LOBBY).shown_clan)


if __name__ == "__main__":
    unittest.main()
