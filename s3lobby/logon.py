"""Deciding whether a logon may enter the lobby, and with which of the game's refusals if not.

Players log on with an account made on the server (command line now, web interface later): the
account name as player name and one of the account's access codes as password. The game's own
"create player" button is refused; nobody gets an account from inside the game.

Every refusal uses a Result code the game knows, so the player sees the matching dialog text from
the game's own text archive (constants.ResultCode lists them). Failed attempts are limited per IP
and per account name from that IP, so access codes cannot be guessed by trying; failures from
others never keep a player with the right code out, since anyone can see an admin's name.
All checks together are budgeted too: each costs a scrypt hash on the event loop.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from s3lobby.constants import (
    ACCESS_CODE_CHECK_BURST,
    ACCESS_CODE_CHECKS_PER_SECOND,
    DEVELOPMENT_PASSWORD,
    LOGON_FAILURE_BURST,
    LOGON_FAILURES_PER_SECOND,
    LogonFlag,
    ResultCode,
)
from s3lobby.database import Account, Clan, LobbyDatabase, Setting, hash_access_code, is_valid_player_name, utc_now
from s3lobby.guards import RateLimiter
from s3lobby.messages import Logon

log = logging.getLogger(__name__)

ALL_CHECKS_KEY = "all"


def is_development_client(client_ip: str) -> bool:
    """True for this machine and its LAN, the only places the development password works from."""
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


@dataclass(frozen=True)
class LobbyOccupancy:
    """Who is in the lobby right now, as far as a logon decision needs to know."""

    player_count: int
    # Player names in casefolded form, as the database compares names without regard to case.
    casefolded_player_names: frozenset[str]
    serial_numbers: frozenset[str]


@dataclass(frozen=True)
class AcceptedLogon:
    account: Account
    # The clan to show next to the player's name; None when the player did not tick the clan box.
    shown_clan: Clan | None


@dataclass(frozen=True)
class RefusedLogon:
    result_code: ResultCode
    # For the server log only; the player sees the game's own text for the result code.
    reason: str


class LogonCheck:
    """Checks logons against the database, and limits failed attempts."""

    def __init__(
        self,
        database: LobbyDatabase,
        clock_seconds: Callable[[], float] = time.monotonic,
        is_development_mode: bool = False,
    ) -> None:
        """is_development_mode also accepts DEVELOPMENT_PASSWORD for every account, from private and
        local addresses only (s3lobby serve --dev)."""
        self._database = database
        self._clock_seconds = clock_seconds
        self._is_development_mode = is_development_mode
        self._failures_by_ip = RateLimiter(LOGON_FAILURES_PER_SECOND, LOGON_FAILURE_BURST)
        self._failures_by_name_and_ip = RateLimiter(LOGON_FAILURES_PER_SECOND, LOGON_FAILURE_BURST)
        self._access_code_checks = RateLimiter(ACCESS_CODE_CHECKS_PER_SECOND, ACCESS_CODE_CHECK_BURST)

    def decide(self, logon: Logon, client_ip: str, occupancy: LobbyOccupancy) -> AcceptedLogon | RefusedLogon:
        """Run the checks in an order that tells a guesser nothing: the public rules first, then
        the access code, and only after a correct code anything about the account itself."""
        now_seconds = self._clock_seconds()
        name_and_ip_key = (logon.player_name.casefold(), client_ip)

        if logon.flags & LogonFlag.CREATE_PLAYER:
            return RefusedLogon(ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE, "the game's create player is not offered")
        is_limited = self._failures_by_ip.is_exhausted(client_ip, now_seconds) or (
            self._failures_by_name_and_ip.is_exhausted(name_and_ip_key, now_seconds)
        )
        if is_limited:
            return RefusedLogon(ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE, "too many failed attempts, try again later")
        if not is_valid_player_name(logon.player_name):
            return RefusedLogon(ResultCode.INVALID_PLAYER_NAME, "name breaks the game's name rule")
        if not self._access_code_checks.allows(ALL_CHECKS_KEY, now_seconds):
            return RefusedLogon(ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE, "too many logons at once, try again later")

        account = self._database.find_account(logon.player_name)
        if account is None:
            # One hash like for a wrong code, so the time of the answer does not tell which
            # names have accounts.
            hash_access_code(logon.password)
            is_known = False
        elif self._is_development_logon(logon, client_ip):
            log.warning("development mode: %r logged on with the development password", account.name)
            is_known = True
        else:
            is_known = self._database.check_access_code(account.account_id, logon.password)
        if not is_known:
            self._failures_by_ip.record_event(client_ip, now_seconds)
            self._failures_by_name_and_ip.record_event(name_and_ip_key, now_seconds)
            return RefusedLogon(ResultCode.UNKNOWN_NAME_OR_ACCESS_CODE, "unknown account or wrong access code")

        return self._decide_for_known_account(account, logon, occupancy)

    def _is_development_logon(self, logon: Logon, client_ip: str) -> bool:
        """The development password, in development mode, from this machine or its LAN: a client
        from the internet never gets in with it, even if the mode was left on by mistake."""
        if not self._is_development_mode or logon.password != DEVELOPMENT_PASSWORD:
            return False
        return is_development_client(client_ip)

    def forget_quiet_keys(self, now_seconds: float) -> None:
        self._failures_by_ip.forget_quiet_keys(now_seconds)
        self._failures_by_name_and_ip.forget_quiet_keys(now_seconds)
        self._access_code_checks.forget_quiet_keys(now_seconds)

    def _decide_for_known_account(
        self, account: Account, logon: Logon, occupancy: LobbyOccupancy
    ) -> AcceptedLogon | RefusedLogon:
        if account.is_banned_at(utc_now()):
            return RefusedLogon(ResultCode.BANNED, f"banned until {account.banned_until:%Y-%m-%d %H:%M} UTC")

        max_players = self._database.get_integer_setting(Setting.MAX_PLAYERS)
        is_full = max_players > 0 and occupancy.player_count >= max_players
        if is_full:
            return RefusedLogon(ResultCode.SERVER_FULL, f"{occupancy.player_count} of {max_players} players online")

        if account.name.casefold() in occupancy.casefolded_player_names:
            return RefusedLogon(ResultCode.ALREADY_LOGGED_ON, "already logged on from elsewhere")
        # Development mode lets several copies of one game installation, which all send the same
        # serial, log on with different accounts for testing on one machine.
        is_serial_in_use = (
            not self._is_development_mode
            and self._database.get_boolean_setting(Setting.UNIQUE_SERIAL_NUMBERS)
            and logon.serial_number in occupancy.serial_numbers
        )
        if is_serial_in_use:
            return RefusedLogon(ResultCode.ALREADY_LOGGED_ON, "serial number already in use")

        shown_clan = None
        if logon.flags & LogonFlag.SHOW_AS_CLAN_MEMBER:
            shown_clan = self._database.find_clan_of_account(account.account_id)
            if shown_clan is None:
                return RefusedLogon(ResultCode.NOT_A_CLAN_MEMBER, "ticked the clan box without being in a clan")
        return AcceptedLogon(account=account, shown_clan=shown_clan)
