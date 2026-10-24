"""The ladder: when a ladder game counts, and the ratings, standings and clan strength that follow.

The game keeps no ladder of its own (docs/ladder.md). It only reports each match twice, at the
start and when one team is left (GameStatus 0x1f). Everything here turns those reports into
results and results into ratings. It is pure: no database, no network, no clock of its own, so the
server, the command line, the web interface and anyone's own tools reach the same answers from the
same facts.

Ratings are never stored. compute_ratings replays the counted results in order, so an admin who
voids or decides a result, or changes a setting, changes every rating at once and consistently.

Players take part only in the ladders they joined (LadderMembership): Liga, Clanliga and each
custom season separately. A match counts in a ladder only when every player in it was a member of
that ladder when it was played, and a player who leaves and joins again starts at the start rating.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from functools import cached_property
from datetime import datetime, timedelta

from s3lobby.constants import (
    CLAN_STRENGTH_MEMBER_COUNT,
    ELO_BASE,
    ELO_SCALE,
    GAME_TICK_MS,
    LADDER_TEAM_COUNT,
    LIGA_PLAYER_COUNT,
    MAX_CLANLIGA_TEAM_SIZE,
    MILLISECONDS_PER_SECOND,
    SECONDS_PER_MINUTE,
    GameStatusFlag,
    LadderState,
    LadderType,
    MatchPlayerFlag,
    SeasonCounts,
)
from s3lobby.messages import GameStatus, MatchReportPlayer
from s3lobby.wire import readable_text


@dataclass(frozen=True)
class LadderSettings:
    """The ladder's numbers, all of them settings an admin can change (docs/ladder.md, section 4)."""

    minimum_minutes: int = 10
    report_timeout_minutes: int = 10
    pair_daily_limit: int = 3
    start_rating: int = 1000
    k: int = 16
    k_provisional: int = 32
    provisional_games: int = 10
    active_days: int = 30
    clan_member_days: int = 3
    inactive_days: int = 60


@dataclass(frozen=True)
class GamePlayerFacts:
    """A player who joined a ladder game in the lobby, as the server recorded it then: the clan the
    player showed (None for none), since when the player was in that clan, and the number of the
    game installation (players with the same number share a serial number)."""

    account_id: int
    clan_id: int | None = None
    clan_joined_at: datetime | None = None
    installation: int | None = None


@dataclass(frozen=True)
class ReportFacts:
    """One match report: who sent it, when, and what it said."""

    sender_account_id: int | None
    received_at: datetime
    status: GameStatus


@dataclass(frozen=True)
class GameFacts:
    """Everything the ladder needs to know about one game.

    ladder_type is the ladder the host chose (LadderType), None for a game that is not a ladder
    game. is_open says whether the running server still has the game; a game that is neither open
    nor closed was cut off by a server restart.
    """

    game_id: int
    ladder_type: int | None
    opened_at: datetime
    closed_at: datetime | None
    is_open: bool
    players: tuple[GamePlayerFacts, ...] = ()
    reports: tuple[ReportFacts, ...] = ()


@dataclass(frozen=True)
class ResultPlayer:
    """One slot of a ladder result."""

    account_id: int
    clan_id: int | None
    team: int
    race: int
    slot: int
    has_won: bool
    has_left: bool


@dataclass(frozen=True)
class Evaluation:
    """What the ladder made of a game: its state, why, and for a counted or disputed game what
    the reports said. played_at is when the first end report arrived, the moment the match was
    decided."""

    game_id: int
    ladder_type: int
    state: LadderState
    reason: str = ""
    winning_team: int | None = None
    game_ticks: int = 0
    played_at: datetime | None = None
    players: tuple[ResultPlayer, ...] = ()
    # The map the match was played on, as the reports name it (a random map starts with a backslash).
    map_name: str = ""


def ladder_minutes(game_ticks: int) -> float:
    """The game's own clock in minutes: one tick every GAME_TICK_MS (0x53d761)."""
    return game_ticks * GAME_TICK_MS / MILLISECONDS_PER_SECOND / SECONDS_PER_MINUTE


def evaluate_game(facts: GameFacts, settings: LadderSettings, now: datetime) -> Evaluation | None:
    """Decide what a ladder game is worth; None for a game that is not a ladder game.

    The rules are those of docs/ladder.md, section 1, checked in an order that gives the most
    useful reason first. A player's report only counts when the player joined the game in the
    lobby: any logged on player can send a report naming any game id.
    """
    if facts.ladder_type is None or facts.ladder_type == LadderType.NONE:
        return None
    ladder_type = facts.ladder_type

    def void(reason: str) -> Evaluation:
        """The game, void for reason."""
        return Evaluation(facts.game_id, ladder_type, LadderState.VOID, reason)

    if ladder_type not in (LadderType.INDIVIDUAL, LadderType.CLANS):
        return void(f"unknown ladder type {ladder_type}")
    # The game is still running on this server: nothing to decide yet.
    if facts.closed_at is None and facts.is_open:
        return Evaluation(facts.game_id, ladder_type, LadderState.WAITING, "the game is still open")
    # Neither open nor closed: the server stopped while the game ran, and the reports that came
    # after the restart could not be tied to it (group ids start again at every start).
    if facts.closed_at is None:
        return void("the server restarted during the game")

    players_by_account = {player.account_id: player for player in facts.players}
    reports = [report for report in facts.reports if report.sender_account_id in players_by_account]
    end_reports = sorted(
        (report for report in reports if report.status.has_winner),
        key=lambda report: report.received_at,
    )
    timeout = timedelta(minutes=settings.report_timeout_minutes)
    has_timed_out = now - facts.closed_at >= timeout

    for report in reports:
        # The game marks a ladder report it considers invalid itself (0x5405ec): a ladder slot
        # without a lobby player id, or the lobby manager's own mark.
        if report.status.flags & GameStatusFlag.NOT_VALID_FOR_LADDER:
            return void("the game marked the match as not valid for the ladder")
        if not report.status.flags & GameStatusFlag.LADDER_GAME:
            return void("a report says the match was not a ladder game")

    if not end_reports:
        if has_timed_out:
            return void("no end report arrived")
        return Evaluation(facts.game_id, ladder_type, LadderState.WAITING, "no end report yet")

    reference = end_reports[0].status
    slots = tuple(sorted(reference.players, key=lambda slot: slot.slot))
    for report in end_reports[1:]:
        if report.status.winning_team != reference.winning_team:
            return Evaluation(
                facts.game_id, ladder_type, LadderState.DISPUTED, "the reports name different winners",
                game_ticks=reference.game_time, played_at=end_reports[0].received_at,
                players=result_players(slots, reference.winning_team, players_by_account),
                map_name=readable_text(reference.map_name),
            )
        if slot_teams(report.status.players) != slot_teams(slots):
            return Evaluation(
                facts.game_id, ladder_type, LadderState.DISPUTED, "the reports name different players or teams",
                game_ticks=reference.game_time, played_at=end_reports[0].received_at,
                players=result_players(slots, reference.winning_team, players_by_account),
                map_name=readable_text(reference.map_name),
            )

    # Every human slot either reported or is marked as having left; until then, and before the
    # timeout, more reports may still come.
    senders = {report.sender_account_id for report in end_reports}
    is_complete = all(
        slot.lobby_player_id in senders or slot.flags & MatchPlayerFlag.SLOT_STATE_3 for slot in slots
    )
    if not is_complete and not has_timed_out:
        return Evaluation(facts.game_id, ladder_type, LadderState.WAITING, "waiting for the other end reports")

    reason = slot_rule_violation(slots, ladder_type, players_by_account, end_reports[0].received_at, settings)
    if reason:
        return void(reason)
    if ladder_minutes(reference.game_time) < settings.minimum_minutes:
        return void(f"shorter than {settings.minimum_minutes} minutes")
    return Evaluation(
        facts.game_id,
        ladder_type,
        LadderState.COUNTED,
        "",
        winning_team=reference.winning_team,
        game_ticks=reference.game_time,
        played_at=end_reports[0].received_at,
        players=result_players(slots, reference.winning_team, players_by_account),
        map_name=readable_text(reference.map_name),
    )


def slot_teams(slots: Iterable[MatchReportPlayer]) -> tuple[tuple[int, int, int], ...]:
    """The slots as (slot, player id, team), in slot order, to compare two reports."""
    return tuple(sorted((slot.slot, slot.lobby_player_id, slot.team) for slot in slots))


def slot_rule_violation(
    slots: tuple[MatchReportPlayer, ...],
    ladder_type: int,
    players_by_account: dict[int, GamePlayerFacts],
    played_at: datetime,
    settings: LadderSettings,
) -> str:
    """Why the slots of a match cannot count for the ladder, or an empty string when they can."""
    if any(slot.is_computer for slot in slots):
        return "computer opponents took part"
    account_ids = [slot.lobby_player_id for slot in slots]
    if any(account_id not in players_by_account for account_id in account_ids):
        return "a player in the match did not join the game in the lobby"
    if len(set(account_ids)) != len(account_ids):
        return "one account played more than one slot"
    teams: dict[int, list[MatchReportPlayer]] = {}
    for slot in slots:
        teams.setdefault(slot.team, []).append(slot)
    if len(teams) != LADDER_TEAM_COUNT:
        return f"the match had {len(teams)} teams, a ladder match needs {LADDER_TEAM_COUNT}"
    if ladder_type == LadderType.INDIVIDUAL and len(slots) != LIGA_PLAYER_COUNT:
        return f"Liga is 1 on 1, the match had {len(slots)} players"
    if ladder_type == LadderType.CLANS:
        reason = clan_rule_violation(teams, players_by_account, played_at, settings)
        if reason:
            return reason
    # Two copies of one installation (one serial number) on opposite sides are most likely one
    # person playing against themselves.
    installations_by_team = []
    for team_slots in teams.values():
        installations = {players_by_account[slot.lobby_player_id].installation for slot in team_slots}
        installations_by_team.append(installations - {None})
    if installations_by_team[0] & installations_by_team[1]:
        return "players on both sides share one game installation"
    return ""


def clan_rule_violation(
    teams: dict[int, list[MatchReportPlayer]],
    players_by_account: dict[int, GamePlayerFacts],
    played_at: datetime,
    settings: LadderSettings,
) -> str:
    """Why a Clanliga match cannot count, or an empty string: two teams of equal size, each one clan,
    two different clans, every player in the clan long enough."""
    team_sizes = {len(team_slots) for team_slots in teams.values()}
    if len(team_sizes) != 1:
        return "the teams differ in size"
    if team_sizes.pop() > MAX_CLANLIGA_TEAM_SIZE:
        return f"Clanliga teams have at most {MAX_CLANLIGA_TEAM_SIZE} players"
    clan_of_team = []
    membership = timedelta(days=settings.clan_member_days)
    for team_slots in teams.values():
        facts = [players_by_account[slot.lobby_player_id] for slot in team_slots]
        clans = {player.clan_id for player in facts}
        if None in clans:
            return "a player showed no clan"
        if len(clans) != 1:
            return "a team mixes clans"
        for player in facts:
            is_new_member = player.clan_joined_at is None or played_at - player.clan_joined_at < membership
            if is_new_member:
                return f"a player joined the clan less than {settings.clan_member_days} days before the match"
        clan_of_team.append(clans.pop())
    if clan_of_team[0] == clan_of_team[1]:
        return "both teams are the same clan"
    return ""


def result_players(
    slots: tuple[MatchReportPlayer, ...], winning_team: int, players_by_account: dict[int, GamePlayerFacts]
) -> tuple[ResultPlayer, ...]:
    """The slots as result players, with the clan each showed when joining the game."""
    players = []
    for slot in slots:
        facts = players_by_account.get(slot.lobby_player_id)
        clan_id = facts.clan_id if facts is not None else None
        players.append(
            ResultPlayer(
                account_id=slot.lobby_player_id,
                clan_id=clan_id,
                team=slot.team,
                race=slot.race,
                slot=slot.slot,
                has_won=slot.team == winning_team,
                has_left=bool(slot.flags & MatchPlayerFlag.SLOT_STATE_3),
            )
        )
    return tuple(players)


def exceeds_pair_limit(evaluation: Evaluation, earlier_results_today: int, settings: LadderSettings) -> Evaluation:
    """A counted result past the daily limit for its pair becomes void (win trading between
    friends); earlier_results_today counts the pair's counted results earlier that day."""
    if evaluation.state != LadderState.COUNTED or earlier_results_today < settings.pair_daily_limit:
        return evaluation
    return replace(
        evaluation,
        state=LadderState.VOID,
        reason=f"more than {settings.pair_daily_limit} results between the same sides in one day",
    )


def pair_key(evaluation: Evaluation) -> frozenset[int]:
    """Who a result was between: the two accounts in Liga, the two clans in Clanliga."""
    if evaluation.ladder_type == LadderType.CLANS:
        return frozenset(player.clan_id for player in evaluation.players if player.clan_id is not None)
    return frozenset(player.account_id for player in evaluation.players)


# Seasons and ratings.

@dataclass(frozen=True)
class Season:
    """A custom season: the counted results between its dates, of the ladders it counts. ends_at
    None runs until an admin ends it. The infinite season is INFINITE_SEASON, not a row."""

    season_id: int | None
    name: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    counts: SeasonCounts = SeasonCounts.BOTH

    def includes(self, result: Evaluation) -> bool:
        """True when the result counts in this season."""
        if result.played_at is None:
            return False
        if self.starts_at is not None and result.played_at < self.starts_at:
            return False
        if self.ends_at is not None and result.played_at >= self.ends_at:
            return False
        if self.counts == SeasonCounts.LIGA:
            return result.ladder_type == LadderType.INDIVIDUAL
        if self.counts == SeasonCounts.CLANLIGA:
            return result.ladder_type == LadderType.CLANS
        return True

    def is_running(self, now: datetime) -> bool:
        """True from the start until the end; the infinite season always runs."""
        has_started = self.starts_at is None or self.starts_at <= now
        has_ended = self.ends_at is not None and self.ends_at <= now
        return has_started and not has_ended


INFINITE_SEASON = Season(season_id=None, name="Infinite season")


# Ladders players join: the infinite season's Liga and Clanliga, and each custom season.
LIGA_LADDER = "liga"
CLANLIGA_LADDER = "clanliga"
SEASON_LADDER = "season"


@dataclass(frozen=True)
class LadderKey:
    """One ladder a player can join: Liga or Clanliga of the infinite season, or a custom season
    (season_id set), which counts whatever ladders the season counts."""

    ladder: str
    season_id: int | None = None


LIGA_KEY = LadderKey(LIGA_LADDER)
CLANLIGA_KEY = LadderKey(CLANLIGA_LADDER)


def ladder_key(season: Season, ladder_type: int) -> LadderKey:
    """The ladder a result of this type counts in for a season: the infinite season's Liga or
    Clanliga, or the custom season itself."""
    if season.season_id is not None:
        return LadderKey(SEASON_LADDER, season.season_id)
    if ladder_type == LadderType.CLANS:
        return CLANLIGA_KEY
    return LIGA_KEY


@dataclass(frozen=True)
class LadderMembership:
    """A player's time in one ladder: from joined_at until left_at (None while still in it)."""

    account_id: int
    key: LadderKey
    joined_at: datetime
    left_at: datetime | None = None

    def covers(self, moment: datetime) -> bool:
        """True when the player was in the ladder at that moment."""
        if moment < self.joined_at:
            return False
        return self.left_at is None or moment < self.left_at


@dataclass(frozen=True)
class LadderRoll:
    """Every membership there is, to look up who was in which ladder when. Frozen and hashable, so
    ratings can be cached per roll."""

    memberships: tuple[LadderMembership, ...] = ()

    @cached_property
    def _by_player_and_ladder(self) -> dict[tuple[int, LadderKey], list[LadderMembership]]:
        """The memberships per player and ladder, built once: replaying ratings asks for every
        player of every result."""
        index: dict[tuple[int, LadderKey], list[LadderMembership]] = {}
        for membership in self.memberships:
            index.setdefault((membership.account_id, membership.key), []).append(membership)
        return index

    def period_start(self, account_id: int, key: LadderKey, moment: datetime) -> datetime | None:
        """When the membership that covers the moment began, None when the player was not in the
        ladder then. Results of different memberships never share a rating: rejoining starts over."""
        for membership in self._by_player_and_ladder.get((account_id, key), ()):
            if membership.covers(moment):
                return membership.joined_at
        return None

    def open_period_start(self, account_id: int, key: LadderKey) -> datetime | None:
        """When the player's current membership of the ladder began, None when they are not in it."""
        for membership in self._by_player_and_ladder.get((account_id, key), ()):
            if membership.left_at is None:
                return membership.joined_at
        return None

    def current_members(self, key: LadderKey, now: datetime) -> frozenset[int]:
        """The players in the ladder now."""
        return frozenset(
            membership.account_id
            for membership in self.memberships
            if membership.key == key and membership.covers(now)
        )

    def is_member(self, account_id: int, key: LadderKey, moment: datetime) -> bool:
        """True when the player was in the ladder at that moment."""
        return self.period_start(account_id, key, moment) is not None

    def all_members(self, account_ids: Iterable[int], key: LadderKey, moment: datetime) -> bool:
        """True when every one of the players was in the ladder at that moment."""
        return all(self.is_member(account_id, key, moment) for account_id in account_ids)


def results_in_ladder(
    results: Iterable[Evaluation], season: Season, roll: LadderRoll | None
) -> list[Evaluation]:
    """The results that count in a season's ladder: all of them without a roll, else those whose
    players were all members of it when they played. For form strips and statistics, which must
    agree with the ratings."""
    if roll is None:
        return list(results)
    kept = []
    for result in results:
        if result.played_at is None:
            continue
        account_ids = [player.account_id for player in result.players]
        if roll.all_members(account_ids, ladder_key(season, result.ladder_type), result.played_at):
            kept.append(result)
    return kept


def counting_ladders(
    account_ids: Iterable[int], ladder_type: int, moment: datetime, seasons: Iterable[Season], roll: LadderRoll
) -> list[LadderKey]:
    """The ladders a match of these players counts in: the infinite season's Liga or Clanliga, and
    every custom season running at the moment that counts this ladder, each only when all the
    players were its members. Empty means the match is stored nowhere."""
    players = list(account_ids)
    if not players:
        return []
    candidates = [ladder_key(INFINITE_SEASON, ladder_type)]
    probe = Evaluation(0, ladder_type, LadderState.COUNTED, played_at=moment)
    for season in seasons:
        if season.season_id is not None and season.includes(probe):
            candidates.append(ladder_key(season, ladder_type))
    return [key for key in candidates if roll.all_members(players, key, moment)]


@dataclass
class Rating:
    """One side's rating in one ladder and season, with its record. history holds the rating after
    each result, oldest first, for charts."""

    rating: float
    results: int = 0
    won: int = 0
    lost: int = 0
    left: int = 0
    last_change: float = 0.0
    last_played_at: datetime | None = None
    history: list[tuple[datetime, float]] = field(default_factory=list)
    # When the membership this rating was earned in began (None without memberships): a rating
    # from an earlier membership is not the player's any more (keep_current_memberships).
    period_start: datetime | None = None

    def is_provisional(self, settings: LadderSettings) -> bool:
        """True while the rating rests on fewer results than settings.provisional_games."""
        return self.results < settings.provisional_games


@dataclass(frozen=True)
class Ratings:
    """The ratings of one season: Liga per account, Clanliga per clan, and each player's personal
    Clanliga record (no rating: one player's share of a team win cannot be read from a report)."""

    liga: dict[int, Rating]
    clanliga: dict[int, Rating]
    clanliga_players: dict[int, Rating]


def expected_score(rating: float, opponent_rating: float) -> float:
    """Elo's chance that a side with rating beats a side with opponent_rating."""
    return 1 / (1 + ELO_BASE ** ((opponent_rating - rating) / ELO_SCALE))


def compute_ratings(
    results: Iterable[Evaluation],
    settings: LadderSettings,
    season: Season = INFINITE_SEASON,
    roll: LadderRoll | None = None,
) -> Ratings:
    """Replay the counted results of a season in the order they were played.

    Each side's K depends on its own number of results so far: a provisional side moves faster.
    Results that are not counted, or not in the season, are skipped. With a roll, so are results
    whose players were not all members of the season's ladder when they played, and a player's
    rating starts over with each new membership. Without one (tools of their own, older tests)
    every counted result counts.
    """
    liga: dict[int, Rating] = {}
    clanliga: dict[int, Rating] = {}
    clanliga_players: dict[int, Rating] = {}
    # The membership each player's current rating belongs to; a new one resets the rating.
    periods: dict[tuple[str, int], datetime] = {}
    counted = [result for result in results if result.state == LadderState.COUNTED and season.includes(result)]
    counted.sort(key=lambda result: (result.played_at, result.game_id))
    for result in counted:
        winners = [player for player in result.players if player.has_won]
        losers = [player for player in result.players if not player.has_won]
        if not winners or not losers:
            continue
        if roll is not None:
            key = ladder_key(season, result.ladder_type)
            account_ids = [player.account_id for player in result.players]
            if result.played_at is None or not roll.all_members(account_ids, key, result.played_at):
                continue
            for account_id in account_ids:
                start = roll.period_start(account_id, key, result.played_at)
                start_over_on_new_membership(liga, clanliga_players, periods, account_id, start)
        if result.ladder_type == LadderType.INDIVIDUAL:
            apply_result(liga, winners[0].account_id, losers[0].account_id, losers[0].has_left, result, settings)
            continue
        # Clanliga: the clans are rated against each other, once per match whatever its size.
        winning_clan = winners[0].clan_id
        losing_clan = losers[0].clan_id
        if winning_clan is None or losing_clan is None:
            continue
        has_losing_team_left = all(player.has_left for player in losers)
        apply_result(clanliga, winning_clan, losing_clan, has_losing_team_left, result, settings)
        for player in result.players:
            record = clanliga_players.setdefault(player.account_id, Rating(rating=settings.start_rating))
            count_result(record, player.has_won, player.has_left, result.played_at)
    # Each player's rating remembers the membership it was earned in.
    for player_ratings in (liga, clanliga_players):
        for account_id, rating in player_ratings.items():
            rating.period_start = periods.get(("player", account_id))
    return Ratings(liga, clanliga, clanliga_players)


def keep_current_memberships(ratings: Ratings, roll: LadderRoll, season: Season) -> Ratings:
    """The ratings without those of players who left the ladder or whose rating comes from an
    earlier membership: a player who leaves leaves the standings, and one who joins again starts at
    the start rating (owner's decision). Clans' ratings stay: clans do not join, players do."""
    liga_key = ladder_key(season, LadderType.INDIVIDUAL)
    clanliga_key = ladder_key(season, LadderType.CLANS)
    liga = {
        account_id: rating
        for account_id, rating in ratings.liga.items()
        if rating.period_start == roll.open_period_start(account_id, liga_key)
    }
    clanliga_players = {
        account_id: rating
        for account_id, rating in ratings.clanliga_players.items()
        if rating.period_start == roll.open_period_start(account_id, clanliga_key)
    }
    return Ratings(liga, ratings.clanliga, clanliga_players)


def start_over_on_new_membership(
    liga: dict[int, Rating],
    clanliga_players: dict[int, Rating],
    periods: dict[tuple[str, int], datetime],
    account_id: int,
    start: datetime | None,
) -> None:
    """Forget a player's Liga rating and personal Clanliga record when this result belongs to a
    later membership than the ones before: a player who left and joined again starts over (owner's
    decision). The results stay, so opponents' ratings are not touched."""
    if start is None:
        return
    period_key = ("player", account_id)
    previous = periods.get(period_key)
    periods[period_key] = start
    if previous is None or previous == start:
        return
    liga.pop(account_id, None)
    clanliga_players.pop(account_id, None)


def apply_result(
    ratings: dict[int, Rating],
    winner: int,
    loser: int,
    has_loser_left: bool,
    result: Evaluation,
    settings: LadderSettings,
) -> None:
    """Move two sides' ratings by one result."""
    winner_rating = ratings.setdefault(winner, Rating(rating=settings.start_rating))
    loser_rating = ratings.setdefault(loser, Rating(rating=settings.start_rating))
    surprise = 1 - expected_score(winner_rating.rating, loser_rating.rating)
    winner_gain = k_factor(winner_rating, settings) * surprise
    loser_loss = k_factor(loser_rating, settings) * surprise
    winner_rating.rating += winner_gain
    winner_rating.last_change = winner_gain
    loser_rating.rating -= loser_loss
    loser_rating.last_change = -loser_loss
    count_result(winner_rating, True, False, result.played_at)
    count_result(loser_rating, False, has_loser_left, result.played_at)


def k_factor(rating: Rating, settings: LadderSettings) -> int:
    """How far one result moves a rating: more while it is provisional."""
    if rating.is_provisional(settings):
        return settings.k_provisional
    return settings.k


def count_result(rating: Rating, has_won: bool, has_left: bool, played_at: datetime | None) -> None:
    """Add one result to a side's record and history."""
    rating.results += 1
    if has_won:
        rating.won += 1
    else:
        rating.lost += 1
    if has_left:
        rating.left += 1
    rating.last_played_at = played_at
    if played_at is not None:
        rating.history.append((played_at, rating.rating))


@dataclass(frozen=True)
class Standing:
    """One line of a ranking: place, who, and their rating."""

    place: int
    side_id: int
    rating: Rating


def standings(
    ratings: dict[int, Rating],
    now: datetime,
    settings: LadderSettings,
    hidden_ids: Iterable[int] = (),
    hides_inactive: bool = False,
) -> list[Standing]:
    """A ranking, best first. hidden_ids are left out (banned or deleted accounts); with
    hides_inactive, sides without a result for settings.inactive_days are left out too (the infinite
    season does that; a custom season shows everyone who played in it). Equal ratings share a place
    order by more results, then by id, so the order never jumps between two calls."""
    hidden = set(hidden_ids)
    inactive_before = now - timedelta(days=settings.inactive_days)
    shown = []
    for side_id, rating in ratings.items():
        if side_id in hidden:
            continue
        is_inactive = rating.last_played_at is not None and rating.last_played_at < inactive_before
        if hides_inactive and is_inactive:
            continue
        shown.append((side_id, rating))
    shown.sort(key=lambda item: (-item[1].rating, -item[1].results, item[0]))
    return [Standing(place, side_id, rating) for place, (side_id, rating) in enumerate(shown, start=1)]


@dataclass(frozen=True)
class ClanStrength:
    """A clan's strength from Liga: the average Liga rating of its best counted members, None when
    it has fewer than CLAN_STRENGTH_MEMBER_COUNT of them."""

    clan_id: int
    strength: float | None
    counted_members: tuple[tuple[int, float], ...]


def clan_strength(
    clan_id: int, member_ids: Iterable[int], liga: dict[int, Rating], now: datetime, settings: LadderSettings
) -> ClanStrength:
    """The average Liga rating of the clan's best members who are no longer provisional and played
    Liga within settings.active_days, so one strong member cannot carry a clan alone."""
    active_since = now - timedelta(days=settings.active_days)
    candidates = []
    for account_id in member_ids:
        rating = liga.get(account_id)
        if rating is None or rating.is_provisional(settings):
            continue
        if rating.last_played_at is None or rating.last_played_at < active_since:
            continue
        candidates.append((account_id, rating.rating))
    candidates.sort(key=lambda item: -item[1])
    best = tuple(candidates[:CLAN_STRENGTH_MEMBER_COUNT])
    if len(best) < CLAN_STRENGTH_MEMBER_COUNT:
        return ClanStrength(clan_id, None, best)
    return ClanStrength(clan_id, sum(rating for _, rating in best) / len(best), best)


# The letters recent_form uses: won, lost, lost by leaving.
FORM_WON = "W"
FORM_LOST = "L"
FORM_LEFT = "X"


def recent_form(
    results: Iterable[Evaluation], side_id: int, ladder_type: int, season: Season = INFINITE_SEASON, count: int = 5
) -> str:
    """A side's last counted results in a season, oldest first, one letter each: FORM_WON,
    FORM_LOST, or FORM_LEFT for a loss by leaving. A side is an account in Liga and a clan in
    Clanliga (a clan loses by leaving only when its whole team left)."""
    counted = [
        result
        for result in results
        if result.state == LadderState.COUNTED and result.ladder_type == ladder_type and season.includes(result)
    ]
    counted.sort(key=lambda result: (result.played_at, result.game_id))
    letters = []
    for result in counted:
        if ladder_type == LadderType.CLANS:
            side = [player for player in result.players if player.clan_id == side_id]
        else:
            side = [player for player in result.players if player.account_id == side_id]
        if not side:
            continue
        if side[0].has_won:
            letters.append(FORM_WON)
        elif all(player.has_left for player in side):
            letters.append(FORM_LEFT)
        else:
            letters.append(FORM_LOST)
    return "".join(letters[-count:])


def report_can_count(status: GameStatus) -> bool:
    """Whether a single ladder report could be part of a counted result, judged on the report alone:
    a ladder game the game itself did not mark invalid, two teams, no computer opponents. The
    game shows the server's answer during the match (0x540a18), before the other reports are in,
    so the answer can only say the report was stored; whether the match counts is decided later."""
    if not status.flags & GameStatusFlag.LADDER_GAME:
        return False
    if status.flags & GameStatusFlag.NOT_VALID_FOR_LADDER:
        return False
    if any(slot.is_computer for slot in status.players):
        return False
    teams = {slot.team for slot in status.players}
    return len(teams) == LADDER_TEAM_COUNT



@dataclass(frozen=True)
class RaceStatistic:
    """How one race fared in a season's counted Liga results: how often it was played and won."""

    race: int
    played: int
    won: int


@dataclass(frozen=True)
class MapStatistic:
    """How often a map was played in a season's counted results, and how long a match took there."""

    map_name: str
    played: int
    average_minutes: float


def race_statistics(
    results: Iterable[Evaluation], season: Season = INFINITE_SEASON, account_id: int | None = None
) -> list[RaceStatistic]:
    """Each race's plays and wins in the counted Liga results of a season, most played first; with
    account_id, only that player's own slots."""
    played: dict[int, int] = {}
    won: dict[int, int] = {}
    for result in results:
        if result.state != LadderState.COUNTED or result.ladder_type != LadderType.INDIVIDUAL:
            continue
        if not season.includes(result):
            continue
        for player in result.players:
            if account_id is not None and player.account_id != account_id:
                continue
            played[player.race] = played.get(player.race, 0) + 1
            if player.has_won:
                won[player.race] = won.get(player.race, 0) + 1
    statistics = [RaceStatistic(race, count, won.get(race, 0)) for race, count in played.items()]
    statistics.sort(key=lambda statistic: (-statistic.played, statistic.race))
    return statistics


def map_statistics(results: Iterable[Evaluation], season: Season = INFINITE_SEASON) -> list[MapStatistic]:
    """Each map's counted results in a season, Liga and Clanliga, most played first, with the
    average length of a match on the game's clock."""
    counts: dict[str, int] = {}
    ticks: dict[str, int] = {}
    for result in results:
        if result.state != LadderState.COUNTED or not season.includes(result):
            continue
        counts[result.map_name] = counts.get(result.map_name, 0) + 1
        ticks[result.map_name] = ticks.get(result.map_name, 0) + result.game_ticks
    statistics = [
        MapStatistic(map_name, count, ladder_minutes(ticks[map_name]) / count) for map_name, count in counts.items()
    ]
    statistics.sort(key=lambda statistic: (-statistic.played, statistic.map_name))
    return statistics
