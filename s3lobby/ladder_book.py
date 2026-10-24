"""The ladder's records: games turned into results, and results turned into standings.

Only players who joined a ladder take part in it (docs/ladder.md): a game whose players were not
all members of at least one ladder it could count in is marked as skipped and nothing is stored.

s3lobby/ladder.py holds the rules as pure functions; this module feeds them from the database and
keeps what they decide. The server evaluates waiting ladder games in its housekeeping, the
command line and the web interface read standings and let admins decide or void results. All of
them go through LadderBook, so none of them holds ladder rules of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone

from s3lobby.constants import LadderState, LadderType, MessageType
from s3lobby.database import DELETED_ACCOUNT_NAME, GameRecord, LadderResultRecord, LobbyDatabase
from s3lobby.ladder import (
    CLANLIGA_KEY,
    INFINITE_SEASON,
    LIGA_KEY,
    SEASON_LADDER,
    Evaluation,
    GameFacts,
    GamePlayerFacts,
    LadderKey,
    LadderRoll,
    LadderSettings,
    Ratings,
    ReportFacts,
    ResultPlayer,
    Season,
    Standing,
    clan_strength,
    compute_ratings,
    counting_ladders,
    evaluate_game,
    exceeds_pair_limit,
    keep_current_memberships,
    ladder_minutes,
    ladder_key,
    pair_key,
    results_in_ladder,
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
# A ladder result has two sides: the winning one and the losing one.
LADDER_SIDE_COUNT = 2
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


@dataclass(frozen=True)
class SkippedGame:
    """A ladder game stored nowhere because not every player had joined a ladder it could count
    in: its players, and the names of those not in its Liga or Clanliga, for the players' line."""

    game_id: int
    game_name: str
    ladder_type: int
    account_ids: tuple[int, ...]
    names_not_joined: tuple[str, ...]


@dataclass(frozen=True)
class EvaluatedGames:
    """What one round of evaluation did: the results it stored and the games it skipped."""

    stored: list[StoredEvaluation]
    skipped: list[SkippedGame]


@dataclass(frozen=True)
class OnlineMember:
    """A Liga member who is online now, with their rating (the start rating before a result)."""

    account_id: int
    name: str
    rating: int


@dataclass(frozen=True)
class OwnResult:
    """One of a player's counted Liga results, seen from their side."""

    played_at: datetime
    opponent: str
    has_won: bool
    # How far the result moved the player's rating, in whole points.
    change: int


@dataclass(frozen=True)
class HeadToHead:
    """A player's counted Liga record against one opponent."""

    won: int
    lost: int
    last_played_at: datetime | None
    has_won_last: bool


@dataclass(frozen=True)
class PendingGame:
    """A ladder game of the player that has no final result yet: waiting for reports, or disputed."""

    game_id: int
    game_name: str
    state: str


@dataclass(frozen=True)
class MembershipRow:
    """One current membership with names, for the admins' list: who, which ladder, since when."""

    account_id: int
    name: str
    ladder: str
    joined_at: datetime


class LadderBook:
    """Reads and writes the ladder through the database, on the database's clock."""

    def __init__(self, database: LobbyDatabase) -> None:
        """A book on this database."""
        self._database = database
        self._clock = database.now
        # Ratings by season and settings, valid while the results do not change (see
        # LobbyDatabase.ladder_results_version): a burst of /top in the chat replays nothing.
        self._cached_version: tuple[int, int] | None = None
        self._cached_results: list[LadderResultRecord] = []
        self._cached_ratings: dict[tuple[Season, LadderSettings, LadderRoll], Ratings] = {}

    def settings(self) -> LadderSettings:
        """The ladder's numbers as the admins set them."""
        return self._database.get_ladder_settings()

    def evaluate_waiting_games(self, open_game_ids: Iterable[int]) -> EvaluatedGames:
        """Evaluate every ladder game without a result and store those that are decided and count
        in at least one ladder; mark the others as skipped. open_game_ids are the game records the
        running server still has open: a game neither open nor closed was cut off by a restart."""
        # What every game is judged against: the settings, the clock, who is in which ladder, the
        # running seasons.
        open_ids = set(open_game_ids)
        settings = self.settings()
        now = self._clock()
        roll = self.roll()
        seasons = self._database.list_ladder_seasons()
        stored = []
        skipped = []
        # Results stored so far, for the daily limit per pair; each new one joins them, so a burst
        # of games in one round is limited too.
        earlier_results = [record.evaluation for record in self._database.list_ladder_results()]
        # Each ladder game without a result: evaluated, then skipped, stored, or left waiting for
        # more reports.
        for game in self._database.list_unresolved_ladder_games():
            # What the rules see of the game: its players as they joined, and its reports.
            facts = self.game_facts(game, game.game_id in open_ids)
            evaluation = evaluate_game(facts, settings, now)
            # Not a ladder game, or still waiting for reports: looked at again in the next round.
            if evaluation is None or evaluation.state == LadderState.WAITING:
                continue
            # Membership is judged for everyone who joined the game in the lobby, when the match
            # was decided (or, for a game without an end report, now): the result of players who
            # did not all join a ladder is not kept anywhere.
            account_ids = tuple(player.account_id for player in facts.players)
            moment = evaluation.played_at or now
            if not counting_ladders(account_ids, evaluation.ladder_type, moment, seasons, roll):
                # Counts nowhere: only the mark is stored, and the players' line names who had not
                # joined the game's own Liga or Clanliga.
                self._database.mark_ladder_game_skipped(game.game_id)
                base_key = ladder_key(INFINITE_SEASON, evaluation.ladder_type)
                skipped.append(
                    SkippedGame(
                        game.game_id,
                        game.name,
                        evaluation.ladder_type,
                        account_ids,
                        self.names_not_joined(account_ids, base_key, moment, roll),
                    )
                )
                log.info("ladder: game %d %r skipped, not every player joined a ladder", game.game_id, game.name)
                continue
            # Counts somewhere: the daily limit per pair may still void it, then it is stored.
            evaluation = exceeds_pair_limit(evaluation, count_same_day_results(evaluation, earlier_results), settings)
            self._database.store_ladder_result(evaluation)
            earlier_results.append(evaluation)
            # Kept for the players' line after the match, and logged with its state.
            stored.append(StoredEvaluation(evaluation, game.name))
            log.info(
                "ladder: game %d %r is %s%s",
                game.game_id,
                game.name,
                evaluation.state.value,
                f" ({evaluation.reason})" if evaluation.reason else "",
            )
        return EvaluatedGames(stored, skipped)

    def online_members(self, online_account_ids: Iterable[int]) -> list[OnlineMember]:
        """The Liga members among the players online, best rating first."""
        # Who is in the Liga now, and the ratings and names to show for them.
        now = self._clock()
        members = self.roll().current_members(LIGA_KEY, now)
        start_rating = self.settings().start_rating
        liga = self.ratings().liga
        names = {account.account_id: account.name for account in self._database.list_accounts()}
        # The online players who are members; one without a result yet shows the start rating.
        online = []
        for account_id in set(online_account_ids):
            # Not a Liga member, or an account deleted since the live state was written.
            if account_id not in members or account_id not in names:
                continue
            # A member without a result yet shows the start rating.
            rating = liga.get(account_id)
            value = round(rating.rating) if rating is not None else start_rating
            online.append(OnlineMember(account_id, names[account_id], value))
        # Best rating first, then by name, so the order is the same on every call.
        online.sort(key=lambda member: (-member.rating, member.name.casefold()))
        return online

    def opponents(self, account_id: int, online_account_ids: Iterable[int]) -> list[OnlineMember]:
        """The Liga members online besides the player, closest to the player's rating first: whom
        to ask for a ladder game."""
        # The player's own rating is the measure; a player outside the Liga or without one is taken
        # at the start rating.
        members = self.online_members(online_account_ids)
        mine = next((member.rating for member in members if member.account_id == account_id), None)
        if mine is None:
            mine = self.settings().start_rating
        # Everyone else, nearest rating first: the fairest games come first.
        others = [member for member in members if member.account_id != account_id]
        others.sort(key=lambda member: (abs(member.rating - mine), member.name.casefold()))
        return others

    def own_results(self, account_id: int, count: int) -> list[OwnResult]:
        """The player's last counted Liga results in their current membership, newest first, with
        how each moved their rating."""
        # Only the current membership counts, so a player without a current Liga rating has nothing
        # to show.
        rating = self.ratings().liga.get(account_id)
        if rating is None:
            return []
        # The player's counted Liga results since their membership began, in the order they were
        # played.
        names = {account.account_id: account.name for account in self._database.list_accounts()}
        mine = [
            result
            for result in self.evaluations_in_ladder()
            if result.state == LadderState.COUNTED
            and result.ladder_type == LadderType.INDIVIDUAL
            and result.played_at is not None
            and (rating.period_start is None or result.played_at >= rating.period_start)
            and any(player.account_id == account_id for player in result.players)
        ]
        # In the order they were played, the order the rating history has.
        mine.sort(key=lambda result: (result.played_at, result.game_id))
        # The rating's history holds the value after each of these results, in the same order.
        previous = float(self.settings().start_rating)
        rows = []
        for result, (_, after) in zip(mine, rating.history):
            # Who the player met, and how the result moved the rating.
            me = next(player for player in result.players if player.account_id == account_id)
            opponent = next((player for player in result.players if player.account_id != account_id), None)
            opponent_name = names.get(opponent.account_id, DELETED_ACCOUNT_NAME) if opponent else ""
            rows.append(OwnResult(result.played_at, opponent_name, me.has_won, round(after - previous)))
            previous = after
        # Newest first, as the chat shows them.
        rows.reverse()
        return rows[:count]

    def head_to_head(self, account_id: int, opponent_id: int) -> HeadToHead:
        """The player's counted Liga record against one opponent, over every membership."""
        # Every counted Liga result between the two, over every membership: the record is history,
        # not a rating.
        won = 0
        lost = 0
        last: Evaluation | None = None
        # Only counted Liga results that both played in.
        for result in self.evaluations_in_ladder():
            if result.state != LadderState.COUNTED or result.ladder_type != LadderType.INDIVIDUAL:
                continue
            sides = {player.account_id: player for player in result.players}
            if account_id not in sides or opponent_id not in sides:
                continue
            # Count it for the player's side, and remember the latest match.
            if sides[account_id].has_won:
                won += 1
            else:
                lost += 1
            # The latest match decides the "last match" line.
            if last is None or (result.played_at, result.game_id) > (last.played_at, last.game_id):
                last = result
        # No match between them yet.
        if last is None:
            return HeadToHead(won, lost, None, False)
        # Whether the player won that latest match.
        has_won_last = any(player.account_id == account_id and player.has_won for player in last.players)
        return HeadToHead(won, lost, last.played_at, has_won_last)

    def pending_games(self, account_id: int) -> list[PendingGame]:
        """The player's ladder games without a final result: still waiting for reports, or
        disputed until an admin decides."""
        # Games still waiting: the player joined them in the lobby, but no result is stored yet.
        pending = []
        # A game counts as the player's when they joined it in the lobby.
        for game in self._database.list_unresolved_ladder_games():
            players = self._database.list_game_players(game.game_id)
            if any(player.account_id == account_id for player in players):
                pending.append(PendingGame(game.game_id, game.name, "waiting for reports"))
        # Disputed results of the player's: stored, but counting for nobody until an admin decides.
        for record in self.results():
            is_mine = any(player.account_id == account_id for player in record.evaluation.players)
            if is_mine and record.evaluation.state == LadderState.DISPUTED:
                pending.append(PendingGame(record.evaluation.game_id, record.game_name, "disputed, an admin decides"))
        return pending

    def names_not_joined(
        self, account_ids: Iterable[int], key: LadderKey, moment: datetime, roll: LadderRoll
    ) -> tuple[str, ...]:
        """The names of the players who were not in the ladder at the moment, for the players' line."""
        # The names of the players the roll does not have in the ladder at that moment.
        names = {account.account_id: account.name for account in self._database.list_accounts()}
        return tuple(
            names.get(account_id, DELETED_ACCOUNT_NAME)
            for account_id in account_ids
            if not roll.is_member(account_id, key, moment)
        )

    def can_count(self, account_ids: Iterable[int], ladder_type: int) -> bool:
        """Whether a game of these players could count in some ladder right now: all of them in its
        Liga or Clanliga, or in one running custom season that counts it."""
        seasons = self._database.list_ladder_seasons()
        return bool(counting_ladders(account_ids, ladder_type, self._clock(), seasons, self.roll()))

    def roll(self) -> LadderRoll:
        """Every membership, read fresh: joining and leaving change who counts at once."""
        return LadderRoll(tuple(self._database.list_ladder_memberships()))

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
        """Every stored result, in the order the matches were played; read again only when the
        stored results changed."""
        version = self._database.ladder_results_version()
        if version != self._cached_version:
            self._cached_results = self._database.list_ladder_results()
            self._cached_ratings = {}
            self._cached_version = version
        return self._cached_results

    def ratings(self, season: Season = INFINITE_SEASON) -> Ratings:
        """The ratings of a season, computed from the counted results, once per season and
        settings while the results stay the same. Callers must not change what they get."""
        # The cache key: the season, the settings and the memberships; the results' own version is
        # checked by results().
        results = self.results()
        settings = self.settings()
        roll = self.roll()
        key = (season, settings, roll)
        # Replay once per key, then drop the ratings of players outside their current membership.
        if key not in self._cached_ratings:
            replayed = compute_ratings((record.evaluation for record in results), settings, season, roll)
            self._cached_ratings[key] = keep_current_memberships(replayed, roll, season)
        return self._cached_ratings[key]

    def evaluations_in_ladder(self, season: Season = INFINITE_SEASON) -> list[Evaluation]:
        """The stored results that count in a season's ladder by its members, for form strips and
        statistics, which must agree with the ratings."""
        evaluations = [record.evaluation for record in self.results()]
        return results_in_ladder(evaluations, season, self.roll())

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

    def match_lines(self, evaluation: Evaluation, game_name: str) -> list[str]:
        """What the players of a just decided game are told in the chat: who beat whom and how the
        ratings moved, or why the game does not count."""
        if evaluation.state == LadderState.DISPUTED:
            return [f"Ladder: the reports of {game_name} name different winners; an admin will decide."]
        if evaluation.state != LadderState.COUNTED:
            return [f"Ladder: {game_name} does not count ({evaluation.reason})."]
        ratings = self.ratings()
        if evaluation.ladder_type == LadderType.CLANS:
            clan_names = {clan.clan_id: clan.name for clan in self._database.list_clans()}
            sides = [player.clan_id for player in evaluation.players if player.has_won][:1]
            sides += [player.clan_id for player in evaluation.players if not player.has_won][:1]
            names = [clan_names.get(side, DELETED_CLAN_NAME) for side in sides]
            side_ratings = ratings.clanliga
            label = "Clanliga"
        else:
            account_names = {account.account_id: account.name for account in self._database.list_accounts()}
            sides = [player.account_id for player in evaluation.players if player.has_won][:1]
            sides += [player.account_id for player in evaluation.players if not player.has_won][:1]
            names = [account_names.get(side, DELETED_ACCOUNT_NAME) for side in sides]
            side_ratings = ratings.liga
            label = "Liga"
        if len(sides) != LADDER_SIDE_COUNT:
            return []
        moves = []
        for name, side in zip(names, sides):
            rating = side_ratings.get(side)
            if rating is not None:
                moves.append(f"{name} {round(rating.rating)} ({round(rating.last_change):+d})")
        return [f"{label}: {names[0]} beat {names[1]}. " + ", ".join(moves) + "."]

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
        # The season's ratings; only the infinite season hides players who have not played for a
        # while.
        settings = self.settings()
        now = self._clock()
        ratings = self.ratings(season)
        hides_inactive = season.season_id is None
        # Clanliga ranks clans, which do not join; only deleted clans leave the list.
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
        # Only players in the ladder now are ranked: one who left keeps their results for their
        # opponents but leaves the list, and comes back at the start rating when joining again.
        members = self.roll().current_members(ladder_key(season, LadderType.INDIVIDUAL), now)
        hidden = [
            account_id
            for account_id, account in accounts.items()
            if account.is_banned_at(now) or account_id not in members
        ]
        # Liga: one line per ranked player, with the clan they are in now.
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
        # The infinite season's Liga ratings, names, and who is in which clan.
        settings = self.settings()
        now = self._clock()
        liga = self.ratings().liga
        account_names = {account.account_id: account.name for account in self._database.list_accounts()}
        memberships = self._database.list_clan_memberships()
        # Only clan members who are in the Liga now lend the clan their rating.
        liga_members = self.roll().current_members(LIGA_KEY, now)
        # One line per clan, with the members that make up its strength.
        rows = []
        # The clan's members who are in the Liga, and what their ratings make of the clan.
        for clan in self._database.list_clans():
            member_ids = [account_id for account_id in memberships.get(clan.clan_id, []) if account_id in liga_members]
            strength = clan_strength(clan.clan_id, member_ids, liga, now, settings)
            members = tuple(
                (account_names.get(account_id, DELETED_ACCOUNT_NAME), round(rating))
                for account_id, rating in strength.counted_members
            )
            # None while the clan has too few established Liga members for a strength.
            value = None if strength.strength is None else round(strength.strength)
            rows.append(ClanStrengthRow(clan.clan_id, clan.name, value, members))
        # Strongest first, clans without a strength last, then by name.
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


class LadderMembershipError(ValueError):
    """A join or leave that cannot be done: an unknown or ended ladder, or nothing to change."""


def find_ladder_key(book: LadderBook, name: str | None) -> tuple[LadderKey, str]:
    """The ladder a player names for /ladder join and the like, with the name to show: "liga" (the
    default), "clanliga", or a custom season by name. Raises LadderMembershipError naming the
    choices."""
    # No name, or "liga", is the infinite season's Liga; "clanliga" its Clanliga.
    text = (name or "").strip()
    if not text or text.lower() == LIGA:
        return LIGA_KEY, "Liga"
    if text.lower() == CLANLIGA:
        return CLANLIGA_KEY, "Clanliga"
    # Anything else names a custom season; the error lists the seasons there are.
    try:
        season = book.find_season(text)
    except LookupError as error:
        raise LadderMembershipError(f"{error}; or liga, clanliga") from error
    # "infinite" names the infinite season, whose ladder to join is the Liga.
    if season.season_id is None:
        return LIGA_KEY, "Liga"
    return LadderKey(SEASON_LADDER, season.season_id), season.name


def join_ladder(book: LadderBook, database: LobbyDatabase, account_id: int, name: str | None) -> str:
    """Make a player a member of a ladder; returns the sentence to tell them. A season that is over
    cannot be joined. Raises LadderMembershipError when there is nothing to do."""
    # A season that is over cannot be joined: nothing of it would count any more.
    key, label = find_ladder_key(book, name)
    if key.season_id is not None:
        season = next(season for season in database.list_ladder_seasons() if season.season_id == key.season_id)
        if season.ends_at is not None and season.ends_at <= database.now():
            raise LadderMembershipError(f"{label} is over")
    # Joining twice is refused, so the player hears they are in already.
    if not database.join_ladder(account_id, key):
        raise LadderMembershipError(f"already in {label}")
    return f"Joined {label}. Your ladder games count there once every player in them has joined it too."


def leave_ladder(book: LadderBook, database: LobbyDatabase, account_id: int, name: str | None) -> str:
    """End a player's membership of a ladder; returns the sentence to tell them. Raises
    LadderMembershipError when they were not in it."""
    key, label = find_ladder_key(book, name)
    if not database.leave_ladder(account_id, key):
        raise LadderMembershipError(f"not in {label}")
    return f"Left {label}. Your results stay for your opponents; joining again starts you at the start rating."


def ladder_labels(database: LobbyDatabase, account_id: int) -> list[str]:
    """The ladders a player is in now, by name: Liga, Clanliga and custom seasons."""
    # The player's memberships that run now, by the ladder's name.
    now = database.now()
    season_names = {season.season_id: season.name for season in database.list_ladder_seasons()}
    labels = []
    # Only this player's memberships, and only those running now.
    for membership in database.list_ladder_memberships():
        if membership.account_id != account_id or not membership.covers(now):
            continue
        labels.append(key_label(membership.key, season_names))
    return labels


def key_label(key: LadderKey, season_names: dict[int, str]) -> str:
    """A ladder's name for people."""
    # The infinite season's two ladders by their names, a custom season by its own.
    if key == LIGA_KEY:
        return "Liga"
    if key == CLANLIGA_KEY:
        return "Clanliga"
    return season_names.get(key.season_id, f"season {key.season_id}")


@dataclass(frozen=True)
class LadderMembers:
    """One ladder's current members by name, for the pages that show who takes part."""

    ladder: str
    names: tuple[str, ...]


def ladder_members(database: LobbyDatabase) -> list[LadderMembers]:
    """Every ladder that can be joined now, with its members: Liga, Clanliga, then the custom
    seasons that have not ended, newest first. Names sorted without regard to case."""
    # The ladders that can be joined: Liga, Clanliga, and the seasons that have not ended.
    now = database.now()
    rows = membership_rows(database)
    labels = ["Liga", "Clanliga"]
    # A season without an end, or one that ends later, can still be joined.
    for season in database.list_ladder_seasons():
        if season.ends_at is None or season.ends_at > now:
            labels.append(season.name)
    # Each ladder with the names of its current members.
    return [
        LadderMembers(label, tuple(sorted((row.name for row in rows if row.ladder == label), key=str.casefold)))
        for label in labels
    ]


def membership_rows(database: LobbyDatabase) -> list[MembershipRow]:
    """Every current membership with names, by ladder and then by player name."""
    # Names for the accounts and seasons the memberships point to.
    now = database.now()
    names = {account.account_id: account.name for account in database.list_accounts()}
    season_names = {season.season_id: season.name for season in database.list_ladder_seasons()}
    # The memberships that run now, one row each.
    rows = [
        MembershipRow(
            membership.account_id,
            names.get(membership.account_id, DELETED_ACCOUNT_NAME),
            key_label(membership.key, season_names),
            membership.joined_at,
        )
        for membership in database.list_ladder_memberships()
        if membership.covers(now)
    ]
    # By ladder, then by player, without regard to case.
    rows.sort(key=lambda row: (row.ladder.lower(), row.name.lower()))
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


def parse_season_day(text: str) -> datetime:
    """A season's first day as YYYY-MM-DD (the start of that day, UTC) or a full ISO 8601 time.
    Raises ValueError for anything else."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as error:
        raise ValueError(f"{text!r} is not a date like 2026-10-01") from error
    # A plain date or a time without a zone is taken as UTC, like every time the server shows.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def parse_season_end(text: str | None) -> datetime | None:
    """A season's end: None for none, a date means the season includes that whole day, so it ends
    at the start of the next one. Raises ValueError for anything else."""
    if text is None or not text.strip():
        return None
    moment = parse_season_day(text)
    is_plain_date = len(text.strip()) == len("YYYY-MM-DD")
    if is_plain_date:
        return datetime.combine(moment.date() + timedelta(days=1), time(), tzinfo=timezone.utc)
    return moment


def last_season_day(season: Season) -> datetime | None:
    """The last day a season includes, for forms and texts: the day before an end at midnight."""
    if season.ends_at is None:
        return None
    is_end_of_day = season.ends_at.time() == time() and season.ends_at.utcoffset() == timedelta(0)
    if is_end_of_day:
        return season.ends_at - timedelta(days=1)
    return season.ends_at
