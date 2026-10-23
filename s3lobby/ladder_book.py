"""The ladder's records: games turned into results, and results turned into standings.

s3lobby/ladder.py holds the rules as pure functions; this module feeds them from the database and
keeps what they decide. The server evaluates waiting ladder games in its housekeeping, the
command line and the web interface read standings and let admins decide or void results. All of
them go through LadderBook, so none of them holds ladder rules of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from s3lobby.constants import LadderState, LadderType, MessageType
from s3lobby.database import DELETED_ACCOUNT_NAME, GameRecord, LadderResultRecord, LobbyDatabase
from s3lobby.ladder import (
    INFINITE_SEASON,
    Evaluation,
    GameFacts,
    GamePlayerFacts,
    LadderSettings,
    Ratings,
    ReportFacts,
    ResultPlayer,
    Season,
    Standing,
    clan_strength,
    compute_ratings,
    evaluate_game,
    exceeds_pair_limit,
    ladder_minutes,
    pair_key,
    standings,
)
from s3lobby.messages import GameStatus
from s3lobby.wire import ProtocolError, RawMessage

log = logging.getLogger(__name__)

# The names the ladders go by, for options, JSON and the web: "liga" ranks players, "clanliga" clans.
LIGA = "liga"
CLANLIGA = "clanliga"
LADDER_NAMES = {LadderType.INDIVIDUAL: LIGA, LadderType.CLANS: CLANLIGA}
# Seasons are looked up by name; these name the infinite season.
INFINITE_SEASON_NAMES = ("", "infinite", "infinite season")
# A ladder result's clan that was deleted since.
DELETED_CLAN_NAME = "(deleted clan)"


@dataclass(frozen=True)
class StandingRow:
    """One line of a ranking with names, as every view shows it: ratings rounded to whole points."""

    place: int
    side_id: int
    name: str
    clan_name: str
    rating: int
    last_change: int
    results: int
    won: int
    lost: int
    left: int
    is_provisional: bool
    last_played_at: datetime | None


@dataclass(frozen=True)
class ClanStrengthRow:
    """A clan's strength from Liga, None while it has too few established members, and who counts."""

    clan_id: int
    name: str
    strength: int | None
    members: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ResultRow:
    """One ladder result with names: who won, who lost, and what became of it."""

    game_id: int
    game_name: str
    ladder: str
    state: LadderState
    reason: str
    played_at: datetime | None
    minutes: float
    winners: tuple[str, ...]
    losers: tuple[str, ...]
    left: tuple[str, ...]
    decided_by: str


@dataclass(frozen=True)
class StoredEvaluation:
    """A result the book has just stored, with the game's name for the log and the players' lines."""

    evaluation: Evaluation
    game_name: str


class LadderBook:
    """Reads and writes the ladder through the database, on the database's clock."""

    def __init__(self, database: LobbyDatabase) -> None:
        """A book on this database."""
        self._database = database
        self._clock = database.now

    def settings(self) -> LadderSettings:
        """The ladder's numbers as the admins set them."""
        return self._database.get_ladder_settings()

    def evaluate_waiting_games(self, open_game_ids: Iterable[int]) -> list[StoredEvaluation]:
        """Evaluate every ladder game without a result and store those that are decided; return
        them. open_game_ids are the game records the running server still has open: a game neither
        open nor closed was cut off by a restart."""
        open_ids = set(open_game_ids)
        settings = self.settings()
        now = self._clock()
        stored = []
        earlier_results = [record.evaluation for record in self._database.list_ladder_results()]
        for game in self._database.list_unresolved_ladder_games():
            evaluation = evaluate_game(self.game_facts(game, game.game_id in open_ids), settings, now)
            if evaluation is None or evaluation.state == LadderState.WAITING:
                continue
            evaluation = exceeds_pair_limit(evaluation, count_same_day_results(evaluation, earlier_results), settings)
            self._database.store_ladder_result(evaluation)
            earlier_results.append(evaluation)
            stored.append(StoredEvaluation(evaluation, game.name))
            log.info(
                "ladder: game %d %r is %s%s",
                game.game_id,
                game.name,
                evaluation.state.value,
                f" ({evaluation.reason})" if evaluation.reason else "",
            )
        return stored

    def game_facts(self, game: GameRecord, is_open: bool) -> GameFacts:
        """What the rules need to know about one stored game: its players as they joined, and its
        reports as the clients sent them. A report that does not decode is left out, as it was
        never trusted to be well formed."""
        players = tuple(
            GamePlayerFacts(
                account_id=player.account_id,
                clan_id=player.clan_id,
                clan_joined_at=player.clan_joined_at,
                installation=player.installation,
            )
            for player in self._database.list_game_players(game.game_id)
            if player.account_id is not None
        )
        reports = []
        for report in self._database.list_reports_of_game(game.game_id):
            try:
                status = GameStatus.decode(RawMessage(MessageType.GAME_STATUS, report.report))
            except ProtocolError:
                continue
            reports.append(ReportFacts(report.sender_account_id, report.received_at, status))
        return GameFacts(
            game_id=game.game_id,
            ladder_type=game.ladder_type,
            opened_at=game.opened_at,
            closed_at=game.closed_at,
            is_open=is_open,
            players=players,
            reports=tuple(reports),
        )

    def results(self) -> list[LadderResultRecord]:
        """Every stored result, in the order the matches were played."""
        return self._database.list_ladder_results()

    def ratings(self, season: Season = INFINITE_SEASON) -> Ratings:
        """The ratings of a season, computed from the counted results."""
        return compute_ratings((record.evaluation for record in self.results()), self.settings(), season)

    def decide(self, game_id: int, winning_team: int, admin_account_id: int | None) -> Evaluation:
        """An admin names the winner of a game the reports disagree on (or overrules any other
        result that has players): the result counts for that team. Raises LookupError for a game
        without a result, ValueError for a team that did not play."""
        record = self._database.find_ladder_result(game_id)
        if record is None:
            raise LookupError(f"game {game_id} has no ladder result")
        teams = {player.team for player in record.evaluation.players}
        if winning_team not in teams:
            raise ValueError(f"team {winning_team} did not play in game {game_id}; the teams were {sorted(teams)}")
        players = tuple(replace(player, has_won=player.team == winning_team) for player in record.evaluation.players)
        decided = replace(
            record.evaluation,
            state=LadderState.COUNTED,
            reason="decided by an admin",
            winning_team=winning_team,
            players=players,
        )
        self._database.store_ladder_result(decided, decided_by_account_id=admin_account_id)
        return decided

    def void(self, game_id: int, reason: str, admin_account_id: int | None) -> Evaluation:
        """An admin takes a result out of every rating; the reports stay. Raises LookupError for a
        game without a result."""
        record = self._database.find_ladder_result(game_id)
        if record is None:
            raise LookupError(f"game {game_id} has no ladder result")
        voided = replace(record.evaluation, state=LadderState.VOID, reason=reason or "voided by an admin")
        self._database.store_ladder_result(voided, decided_by_account_id=admin_account_id)
        return voided

    # Views with names, for the command line, the web interface and the chat commands.

    def find_season(self, name: str | None) -> Season:
        """The season with this name; the infinite season for no name or "infinite". Raises
        LookupError naming the seasons there are."""
        if name is None or name.strip().lower() in INFINITE_SEASON_NAMES:
            return INFINITE_SEASON
        season = self._database.find_ladder_season(name.strip())
        if season is None:
            known = ", ".join(season.name for season in self._database.list_ladder_seasons()) or "none"
            raise LookupError(f"there is no season named {name!r}; custom seasons: {known}")
        return season

    def standing_rows(self, season: Season = INFINITE_SEASON, ladder: str = LIGA) -> list[StandingRow]:
        """The ranking of a season's Liga (players) or Clanliga (clans), best first. Banned and
        deleted accounts are left out; the infinite season also leaves out whoever has not played
        for the inactive days."""
        settings = self.settings()
        now = self._clock()
        ratings = self.ratings(season)
        hides_inactive = season.season_id is None
        if ladder == CLANLIGA:
            clan_names = {clan.clan_id: clan.name for clan in self._database.list_clans()}
            ranked = standings(ratings.clanliga, now, settings, hidden_ids=(), hides_inactive=hides_inactive)
            # A deleted clan's results stay for its opponents, but the clan leaves the list.
            clan_rows = [
                standing_row(standing, clan_names[standing.side_id], "", settings)
                for standing in ranked
                if standing.side_id in clan_names
            ]
            return [replace(row, place=place) for place, row in enumerate(clan_rows, start=1)]
        accounts = {account.account_id: account for account in self._database.list_accounts()}
        hidden = [account_id for account_id, account in accounts.items() if account.is_banned_at(now)]
        clan_of_account = self.clan_names_of_accounts()
        rows = []
        for standing in standings(ratings.liga, now, settings, hidden_ids=hidden, hides_inactive=hides_inactive):
            account = accounts.get(standing.side_id)
            # A deleted account's results stay for its opponents, but the account leaves the list.
            if account is None:
                continue
            rows.append(standing_row(standing, account.name, clan_of_account.get(account.account_id, ""), settings))
        # Places count again over what is shown, so a hidden player leaves no gap.
        return [replace(row, place=place) for place, row in enumerate(rows, start=1)]

    def clan_names_of_accounts(self) -> dict[int, str]:
        """Each account's clan name, for the accounts in a clan."""
        clan_names = {clan.clan_id: clan.name for clan in self._database.list_clans()}
        names = {}
        for clan_id, account_ids in self._database.list_clan_memberships().items():
            for account_id in account_ids:
                names[account_id] = clan_names.get(clan_id, "")
        return names

    def clan_strength_rows(self) -> list[ClanStrengthRow]:
        """Every clan's strength from Liga in the infinite season, strongest first, clans without
        enough established members last."""
        settings = self.settings()
        now = self._clock()
        liga = self.ratings().liga
        account_names = {account.account_id: account.name for account in self._database.list_accounts()}
        memberships = self._database.list_clan_memberships()
        rows = []
        for clan in self._database.list_clans():
            strength = clan_strength(clan.clan_id, memberships.get(clan.clan_id, []), liga, now, settings)
            members = tuple(
                (account_names.get(account_id, DELETED_ACCOUNT_NAME), round(rating))
                for account_id, rating in strength.counted_members
            )
            value = None if strength.strength is None else round(strength.strength)
            rows.append(ClanStrengthRow(clan.clan_id, clan.name, value, members))
        rows.sort(key=lambda row: (row.strength is None, -(row.strength or 0), row.name.lower()))
        return rows

    def result_rows(
        self, season: Season = INFINITE_SEASON, states: Iterable[LadderState] | None = None, limit: int | None = None
    ) -> list[ResultRow]:
        """The results of a season, newest first, with names; states narrows them down (all by
        default: an admin wants to see the void and disputed ones too)."""
        wanted_states = set(states) if states is not None else None
        account_names = {account.account_id: account.name for account in self._database.list_accounts()}
        clan_names = {clan.clan_id: clan.name for clan in self._database.list_clans()}
        rows = []
        for record in reversed(self.results()):
            evaluation = record.evaluation
            if wanted_states is not None and evaluation.state not in wanted_states:
                continue
            # A void result may have no players; it still belongs to the season by its date.
            if evaluation.played_at is not None and not season_contains_date(season, evaluation):
                continue
            rows.append(result_row(record, account_names, clan_names))
            if limit is not None and len(rows) >= limit:
                break
        return rows


def count_same_day_results(evaluation: Evaluation, earlier: Iterable[Evaluation]) -> int:
    """How many counted results between the same sides were played earlier on the same day (UTC)."""
    if evaluation.played_at is None:
        return 0
    sides = pair_key(evaluation)
    day = evaluation.played_at.date()
    count = 0
    for other in earlier:
        is_same_day = other.played_at is not None and other.played_at.date() == day
        is_earlier = other.played_at is not None and other.played_at <= evaluation.played_at
        is_same_pair = other.ladder_type == evaluation.ladder_type and pair_key(other) == sides
        if other.state == LadderState.COUNTED and is_same_day and is_earlier and is_same_pair:
            count += 1
    return count


def standing_row(standing: Standing, name: str, clan_name: str, settings: LadderSettings) -> StandingRow:
    """A ranking line with names; ratings and changes are shown in whole points."""
    rating = standing.rating
    return StandingRow(
        place=standing.place,
        side_id=standing.side_id,
        name=name,
        clan_name=clan_name,
        rating=round(rating.rating),
        last_change=round(rating.last_change),
        results=rating.results,
        won=rating.won,
        lost=rating.lost,
        left=rating.left,
        is_provisional=rating.is_provisional(settings),
        last_played_at=rating.last_played_at,
    )


def season_contains_date(season: Season, evaluation: Evaluation) -> bool:
    """True when a result falls into the season's dates and ladders; unlike Season.includes it
    does not care whether the result counted."""
    return season.includes(replace(evaluation, state=LadderState.COUNTED))


def result_row(record: LadderResultRecord, account_names: dict[int, str], clan_names: dict[int, str]) -> ResultRow:
    """A stored result with the names of its sides: players for Liga, clans and players for Clanliga."""
    evaluation = record.evaluation

    def side_name(player: ResultPlayer) -> str:
        """A player's name, with the clan in Clanliga."""
        name = account_names.get(player.account_id, DELETED_ACCOUNT_NAME)
        if evaluation.ladder_type == LadderType.CLANS and player.clan_id is not None:
            return f"{name} ({clan_names.get(player.clan_id, DELETED_CLAN_NAME)})"
        return name

    winners = tuple(side_name(player) for player in evaluation.players if player.has_won)
    losers = tuple(side_name(player) for player in evaluation.players if not player.has_won)
    left = tuple(side_name(player) for player in evaluation.players if player.has_left)
    return ResultRow(
        game_id=evaluation.game_id,
        game_name=record.game_name,
        ladder=LADDER_NAMES.get(evaluation.ladder_type, str(evaluation.ladder_type)),
        state=evaluation.state,
        reason=evaluation.reason,
        played_at=evaluation.played_at,
        minutes=ladder_minutes(evaluation.game_ticks),
        winners=winners,
        losers=losers,
        left=left,
        decided_by=record.decided_by_name,
    )
