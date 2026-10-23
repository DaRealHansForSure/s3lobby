"""The ladder's rules (s3lobby/ladder.py): when a ladder game counts, and the ratings that follow."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from s3lobby.constants import (
    GAME_TICK_MS,
    MILLISECONDS_PER_SECOND,
    NO_WINNING_TEAM,
    SECONDS_PER_MINUTE,
    GameStatusFlag,
    LadderState,
    LadderType,
    MatchPlayerFlag,
    SeasonCounts,
)
from s3lobby.ladder import (
    INFINITE_SEASON,
    Evaluation,
    GameFacts,
    GamePlayerFacts,
    LadderSettings,
    ReportFacts,
    ResultPlayer,
    Season,
    clan_strength,
    compute_ratings,
    evaluate_game,
    exceeds_pair_limit,
    expected_score,
    recent_form,
    report_can_count,
    standings,
)
from s3lobby.messages import GameStatus, MatchReportPlayer

ALICE, BOB, CAROL, DAVE = 1, 2, 3, 4
ROMANS, AMAZONS = 10, 20
OPENED = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
SETTINGS = LadderSettings()
# Before the report timeout of a game closed 32 minutes after it opened.
SOON = OPENED + timedelta(minutes=33)


def ticks(minutes: float) -> int:
    """The game's tick count for a match of this many minutes."""
    return round(minutes * SECONDS_PER_MINUTE * MILLISECONDS_PER_SECOND / GAME_TICK_MS)


def slot(account_id: int, slot_number: int, team: int, flags: int = 0) -> MatchReportPlayer:
    """One slot of a report, Romans."""
    return MatchReportPlayer(flags, account_id, 0, 0, slot_number, team)


def report(
    sender: int,
    slots: tuple[MatchReportPlayer, ...],
    winning_team: int = 0,
    minutes: float = 30,
    flags: int = GameStatusFlag.LADDER_GAME,
    at_minutes: float = 31,
) -> ReportFacts:
    """An end report from sender, received at_minutes after the game opened."""
    status = GameStatus(1, flags, winning_team, ticks(minutes), "\\Random", slots)
    return ReportFacts(sender, OPENED + timedelta(minutes=at_minutes), status)


LIGA_SLOTS = (slot(ALICE, 0, 0), slot(BOB, 1, 1))


def liga_game(*reports: ReportFacts, closed_minutes: float = 32, players: tuple | None = None) -> GameFacts:
    """A closed Liga game between Alice and Bob with the given reports."""
    if players is None:
        players = (GamePlayerFacts(ALICE, installation=1), GamePlayerFacts(BOB, installation=2))
    return GameFacts(
        game_id=7,
        ladder_type=LadderType.INDIVIDUAL,
        opened_at=OPENED,
        closed_at=OPENED + timedelta(minutes=closed_minutes),
        is_open=False,
        players=players,
        reports=reports,
    )


def soon_after_close() -> datetime:
    """A moment before the report timeout ran out."""
    return OPENED + timedelta(minutes=33)


def long_after_close() -> datetime:
    """A moment after the report timeout ran out."""
    return OPENED + timedelta(hours=2)


class EvaluateLigaTest(unittest.TestCase):
    """When a Liga game counts, waits, is disputed or is void."""

    def test_two_agreeing_end_reports_count(self) -> None:
        """Both players report the same winner after a long enough match: Alice won."""
        game = liga_game(report(ALICE, LIGA_SLOTS), report(BOB, LIGA_SLOTS))
        evaluation = evaluate_game(game, SETTINGS, soon_after_close())
        self.assertEqual(evaluation.state, LadderState.COUNTED)
        self.assertEqual(evaluation.winning_team, 0)
        winners = [player.account_id for player in evaluation.players if player.has_won]
        self.assertEqual(winners, [ALICE])

    def test_a_normal_game_is_not_evaluated(self) -> None:
        """Only ladder games become results."""
        game = GameFacts(1, LadderType.NONE, OPENED, OPENED, False)
        self.assertIsNone(evaluate_game(game, SETTINGS, soon_after_close()))

    def test_an_open_game_waits(self) -> None:
        """Nothing is decided while the game is still open on this server."""
        game = GameFacts(1, LadderType.INDIVIDUAL, OPENED, None, is_open=True)
        self.assertEqual(evaluate_game(game, SETTINGS, soon_after_close()).state, LadderState.WAITING)

    def test_a_game_cut_off_by_a_restart_is_void(self) -> None:
        """A game neither open nor closed lost its reports to a server restart."""
        game = GameFacts(1, LadderType.INDIVIDUAL, OPENED, None, is_open=False)
        evaluation = evaluate_game(game, SETTINGS, soon_after_close())
        expected = (LadderState.VOID, "the server restarted during the game")
        self.assertEqual((evaluation.state, evaluation.reason), expected)

    def test_one_end_report_waits_for_the_other_until_the_timeout(self) -> None:
        """Bob's report may still come; after the timeout Alice's report alone decides."""
        game = liga_game(report(ALICE, LIGA_SLOTS))
        self.assertEqual(evaluate_game(game, SETTINGS, soon_after_close()).state, LadderState.WAITING)
        self.assertEqual(evaluate_game(game, SETTINGS, long_after_close()).state, LadderState.COUNTED)

    def test_a_player_who_left_is_not_waited_for(self) -> None:
        """Bob left the match (slot state 3): Alice's report is complete at once and Bob lost by
        leaving."""
        slots = (slot(ALICE, 0, 0), slot(BOB, 1, 1, MatchPlayerFlag.SLOT_STATE_3))
        evaluation = evaluate_game(liga_game(report(ALICE, slots)), SETTINGS, soon_after_close())
        self.assertEqual(evaluation.state, LadderState.COUNTED)
        bob = next(player for player in evaluation.players if player.account_id == BOB)
        self.assertTrue(bob.has_left)
        self.assertFalse(bob.has_won)

    def test_no_end_report_is_void_after_the_timeout(self) -> None:
        """Only start reports: void once the timeout ran out."""
        start = report(ALICE, LIGA_SLOTS, winning_team=NO_WINNING_TEAM, minutes=0)
        evaluation = evaluate_game(liga_game(start), SETTINGS, long_after_close())
        self.assertEqual((evaluation.state, evaluation.reason), (LadderState.VOID, "no end report arrived"))

    def test_different_winners_are_disputed(self) -> None:
        """Each player claims the win: nobody gets it until an admin decides."""
        game = liga_game(report(ALICE, LIGA_SLOTS, winning_team=0), report(BOB, LIGA_SLOTS, winning_team=1))
        evaluation = evaluate_game(game, SETTINGS, soon_after_close())
        self.assertEqual(evaluation.state, LadderState.DISPUTED)

    def test_a_short_match_is_void(self) -> None:
        """A match decided before the minimum time does not count, for either side."""
        game = liga_game(report(ALICE, LIGA_SLOTS, minutes=4), report(BOB, LIGA_SLOTS, minutes=4))
        evaluation = evaluate_game(game, SETTINGS, soon_after_close())
        self.assertEqual((evaluation.state, evaluation.reason), (LadderState.VOID, "shorter than 10 minutes"))

    def test_a_report_from_someone_outside_the_game_is_ignored(self) -> None:
        """Carol never joined the game: her report, claiming Bob won, changes nothing."""
        game = liga_game(report(ALICE, LIGA_SLOTS), report(BOB, LIGA_SLOTS), report(CAROL, LIGA_SLOTS, winning_team=1))
        self.assertEqual(evaluate_game(game, SETTINGS, soon_after_close()).state, LadderState.COUNTED)

    def test_computer_opponents_are_void(self) -> None:
        """Liga is between people."""
        slots = (slot(ALICE, 0, 0), slot(0, 1, 1, MatchPlayerFlag.COMPUTER))
        evaluation = evaluate_game(liga_game(report(ALICE, slots)), SETTINGS, long_after_close())
        self.assertEqual((evaluation.state, evaluation.reason), (LadderState.VOID, "computer opponents took part"))

    def test_one_installation_on_both_sides_is_void(self) -> None:
        """Two copies with one serial number against each other are most likely one person."""
        players = (GamePlayerFacts(ALICE, installation=1), GamePlayerFacts(BOB, installation=1))
        game = liga_game(report(ALICE, LIGA_SLOTS), report(BOB, LIGA_SLOTS), players=players)
        evaluation = evaluate_game(game, SETTINGS, soon_after_close())
        self.assertEqual(evaluation.reason, "players on both sides share one game installation")

    def test_the_game_s_own_invalid_mark_is_void(self) -> None:
        """The game marks a ladder report invalid itself (0x5405ec)."""
        flags = GameStatusFlag.LADDER_GAME | GameStatusFlag.NOT_VALID_FOR_LADDER
        game = liga_game(report(ALICE, LIGA_SLOTS, flags=flags), report(BOB, LIGA_SLOTS, flags=flags))
        self.assertEqual(evaluate_game(game, SETTINGS, soon_after_close()).state, LadderState.VOID)

    def test_three_players_are_not_liga(self) -> None:
        """Liga is 1 on 1."""
        slots = (slot(ALICE, 0, 0), slot(BOB, 1, 1), slot(CAROL, 2, 1))
        players = tuple(GamePlayerFacts(account) for account in (ALICE, BOB, CAROL))
        game = liga_game(report(ALICE, slots), report(BOB, slots), report(CAROL, slots), players=players)
        evaluation = evaluate_game(game, SETTINGS, soon_after_close())
        self.assertEqual(evaluation.reason, "Liga is 1 on 1, the match had 3 players")

    def test_the_pair_limit_voids_further_results_of_a_day(self) -> None:
        """A fourth result between the same two players in one day does not count."""
        game = liga_game(report(ALICE, LIGA_SLOTS), report(BOB, LIGA_SLOTS))
        evaluation = evaluate_game(game, SETTINGS, soon_after_close())
        self.assertEqual(exceeds_pair_limit(evaluation, 2, SETTINGS).state, LadderState.COUNTED)
        self.assertEqual(exceeds_pair_limit(evaluation, 3, SETTINGS).state, LadderState.VOID)


class ReportAnswerTest(unittest.TestCase):
    """What a single report can be told during the match, before the other reports are in."""

    def test_a_good_report_can_count(self) -> None:
        """A ladder report with two human teams: the game may hear it was stored."""
        self.assertTrue(report_can_count(report(ALICE, LIGA_SLOTS).status))

    def test_reports_that_never_count(self) -> None:
        """Not a ladder game, the game's own invalid mark, computer opponents, one team only."""
        computer_slots = (slot(ALICE, 0, 0), slot(0, 1, 1, MatchPlayerFlag.COMPUTER))
        one_team = (slot(ALICE, 0, 0), slot(BOB, 1, 0))
        bad_reports = (
            report(ALICE, LIGA_SLOTS, flags=0),
            report(ALICE, LIGA_SLOTS, flags=GameStatusFlag.LADDER_GAME | GameStatusFlag.NOT_VALID_FOR_LADDER),
            report(ALICE, computer_slots),
            report(ALICE, one_team),
        )
        for bad_report in bad_reports:
            with self.subTest(report=bad_report):
                self.assertFalse(report_can_count(bad_report.status))

class EvaluateClanligaTest(unittest.TestCase):
    """Clanliga: one clan per team, two clans, members long enough."""

    SLOTS = (slot(ALICE, 0, 0), slot(BOB, 1, 0), slot(CAROL, 2, 1), slot(DAVE, 3, 1))

    def game(self, players: tuple[GamePlayerFacts, ...]) -> GameFacts:
        """A closed 2 on 2 Clanliga game with everyone's agreeing end report."""
        reports = tuple(report(account, self.SLOTS) for account in (ALICE, BOB, CAROL, DAVE))
        return GameFacts(8, LadderType.CLANS, OPENED, OPENED + timedelta(minutes=32), False, players, reports)

    def members(self, clans: tuple, joined_days_before: int = 30) -> tuple[GamePlayerFacts, ...]:
        """Alice, Bob, Carol and Dave with the given clans, each on their own installation."""
        joined = OPENED - timedelta(days=joined_days_before)
        accounts = (ALICE, BOB, CAROL, DAVE)
        return tuple(
            GamePlayerFacts(account, clan, joined, installation=account) for account, clan in zip(accounts, clans)
        )

    def test_two_clans_count(self) -> None:
        """Romans against Amazons: the Romans won."""
        evaluation = evaluate_game(self.game(self.members((ROMANS, ROMANS, AMAZONS, AMAZONS))), SETTINGS, SOON)
        self.assertEqual(evaluation.state, LadderState.COUNTED)

    def test_a_mixed_team_is_void(self) -> None:
        """A team of two clans is no clan team."""
        evaluation = evaluate_game(self.game(self.members((ROMANS, AMAZONS, AMAZONS, AMAZONS))), SETTINGS, SOON)
        self.assertEqual(evaluation.reason, "a team mixes clans")

    def test_a_new_member_is_void(self) -> None:
        """A player borrowed for the evening (in the clan for a day) does not count."""
        players = self.members((ROMANS, ROMANS, AMAZONS, AMAZONS), joined_days_before=1)
        evaluation = evaluate_game(self.game(players), SETTINGS, SOON)
        self.assertEqual(evaluation.reason, "a player joined the clan less than 3 days before the match")

    def test_a_player_without_a_clan_is_void(self) -> None:
        """Clanliga needs every player to show a clan."""
        evaluation = evaluate_game(self.game(self.members((ROMANS, ROMANS, None, AMAZONS))), SETTINGS, SOON)
        self.assertEqual(evaluation.reason, "a player showed no clan")


def counted(game_id: int, winner: int, loser: int, day: int, has_loser_left: bool = False) -> Evaluation:
    """A counted Liga result on the given day of October."""
    return Evaluation(
        game_id,
        LadderType.INDIVIDUAL,
        LadderState.COUNTED,
        winning_team=0,
        played_at=datetime(2026, 10, day, 20, 0, tzinfo=timezone.utc),
        players=(
            ResultPlayer(winner, None, 0, 0, 0, True, False),
            ResultPlayer(loser, None, 1, 0, 1, False, has_loser_left),
        ),
    )


class RatingTest(unittest.TestCase):
    """Elo from the counted results, seasons, standings and clan strength."""

    def test_equal_players_move_by_half_of_k(self) -> None:
        """Two new players: the winner gains K/2, the loser loses as much (both provisional, K 32)."""
        ratings = compute_ratings([counted(1, ALICE, BOB, 1)], SETTINGS).liga
        self.assertAlmostEqual(ratings[ALICE].rating, 1016)
        self.assertAlmostEqual(ratings[BOB].rating, 984)
        self.assertEqual((ratings[ALICE].won, ratings[BOB].lost), (1, 1))

    def test_beating_a_stronger_player_gains_more(self) -> None:
        """The expected score falls as the opponent's rating rises."""
        self.assertAlmostEqual(expected_score(1000, 1000), 0.5)
        self.assertLess(expected_score(1000, 1200), 0.5)

    def test_order_follows_the_time_of_play(self) -> None:
        """Results replay by time played, whatever order they come in."""
        results = [counted(2, BOB, ALICE, 2), counted(1, ALICE, BOB, 1)]
        ratings = compute_ratings(results, SETTINGS).liga
        self.assertEqual(ratings[ALICE].history[0][1], 1016)

    def test_void_results_do_not_count(self) -> None:
        """Only counted results move ratings."""
        void = Evaluation(1, LadderType.INDIVIDUAL, LadderState.VOID, "test")
        self.assertEqual(compute_ratings([void], SETTINGS).liga, {})

    def test_a_custom_season_starts_everyone_again(self) -> None:
        """Results before a season's start do not count in it."""
        season = Season(1, "Autumn Cup", starts_at=datetime(2026, 10, 2, tzinfo=timezone.utc), counts=SeasonCounts.LIGA)
        results = [counted(1, ALICE, BOB, 1), counted(2, BOB, ALICE, 3)]
        ratings = compute_ratings(results, SETTINGS, season).liga
        self.assertAlmostEqual(ratings[BOB].rating, 1016)
        self.assertEqual(compute_ratings(results, SETTINGS, INFINITE_SEASON).liga[BOB].results, 2)

    def test_leaving_is_counted(self) -> None:
        """A loss by leaving shows in the record."""
        ratings = compute_ratings([counted(1, ALICE, BOB, 1, has_loser_left=True)], SETTINGS).liga
        self.assertEqual(ratings[BOB].left, 1)

    def test_standings_rank_by_rating_and_hide_inactive(self) -> None:
        """Best first; a player without a result for too long leaves the infinite season's list."""
        results = [counted(1, ALICE, BOB, 1), counted(2, ALICE, CAROL, 20)]
        ratings = compute_ratings(results, SETTINGS).liga
        now = datetime(2026, 12, 15, tzinfo=timezone.utc)
        ranked = standings(ratings, now, SETTINGS, hides_inactive=True)
        self.assertEqual([standing.side_id for standing in ranked], [ALICE, CAROL])
        self.assertEqual([standing.side_id for standing in standings(ratings, now, SETTINGS)], [ALICE, CAROL, BOB])

    def test_clan_strength_needs_three_established_members(self) -> None:
        """The average of the three best members who are no longer provisional."""
        results = []
        for game in range(12):
            results.append(counted(game * 3 + 1, ALICE, DAVE, 1 + game))
            results.append(counted(game * 3 + 2, BOB, DAVE, 1 + game))
            results.append(counted(game * 3 + 3, CAROL, DAVE, 1 + game))
        liga = compute_ratings(results, SETTINGS).liga
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        strength = clan_strength(ROMANS, (ALICE, BOB, CAROL), liga, now, SETTINGS)
        self.assertIsNotNone(strength.strength)
        self.assertIsNone(clan_strength(AMAZONS, (ALICE, BOB), liga, now, SETTINGS).strength)


    def test_form_shows_the_last_results_oldest_first(self) -> None:
        """Won, lost by leaving, won: the last five at most, in the order they were played."""
        results = [counted(1, ALICE, BOB, 1), counted(2, BOB, ALICE, 2, has_loser_left=True), counted(3, ALICE, BOB, 3)]
        self.assertEqual(recent_form(results, ALICE, LadderType.INDIVIDUAL), "WXW")
        self.assertEqual(recent_form(results, BOB, LadderType.INDIVIDUAL, count=2), "WL")
        self.assertEqual(recent_form(results, CAROL, LadderType.INDIVIDUAL), "")

if __name__ == "__main__":
    unittest.main()
