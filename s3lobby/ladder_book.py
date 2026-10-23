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

from s3lobby.constants import LadderState, MessageType
from s3lobby.database import GameRecord, LadderResultRecord, LobbyDatabase
from s3lobby.ladder import (
    INFINITE_SEASON,
    Evaluation,
    GameFacts,
    GamePlayerFacts,
    LadderSettings,
    Ratings,
    ReportFacts,
    Season,
    compute_ratings,
    evaluate_game,
    exceeds_pair_limit,
    pair_key,
)
from s3lobby.messages import GameStatus
from s3lobby.wire import ProtocolError, RawMessage

log = logging.getLogger(__name__)


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
