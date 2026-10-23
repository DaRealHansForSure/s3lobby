"""The texts the ladder writes into the lobby on its own (docs/ladder.md, "Texts the server writes
into the lobby").

The game has no screen for a ladder, so the server writes it into the places the game has:

- the MessageZone board, under a theme only the server writes ("Liga" for German games, "Ladder"
  for every other language, which falls back to English): one message when a custom season starts,
  one when it ends, and a weekly summary. Board texts are plain text; line breaks are the only
  formatting the board knows (0x420a40, 0x420cb0).
- a block under the Home tab's news with the top five and the running seasons;
- an announcement to everyone online when a custom season ends;
- a line at logon with the player's place, and a line about an admin's decision on the player's
  disputed game.

Every text is built only from text that was checked when it was stored (player, clan and season
names, which the game can show) and numbers, never from what a client typed into the chat. Each
event is written once: LobbyDatabase.record_ladder_event notes it before the text is posted, so a
restart never writes it twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from s3lobby.constants import (
    LADDER_TOP_COUNT,
    MAX_TEXT_MESSAGE_BYTES,
    TEXT_ENCODING,
    LadderState,
    LadderType,
    SeasonCounts,
)
from s3lobby.database import DELETED_ACCOUNT_NAME, BoardTheme, DatabaseError, LobbyDatabase, Setting
from s3lobby.ladder import INFINITE_SEASON, Evaluation, Rating, Season
from s3lobby.ladder_book import CLANLIGA, LIGA, LadderBook, StandingRow, last_season_day

log = logging.getLogger(__name__)

GERMAN = "German"
ENGLISH = "English"
# The server's own board theme per language: German games read the German one, every other language
# falls back to the English one (0x453831, 0x4539ce).
BOARD_THEME_NAMES = {GERMAN: "Liga", ENGLISH: "Ladder"}
BOARD_THEME_DESCRIPTIONS = {
    GERMAN: "Saisons und Wochenberichte, vom Server geschrieben",
    ENGLISH: "Seasons and weekly summaries, written by the server",
}
GERMAN_MONTHS = ("Jan", "Feb", "März", "Apr", "Mai", "Juni", "Juli", "Aug", "Sep", "Okt", "Nov", "Dez")
# Room the lobby needs in front of a board message for its byline ("Server name, 2026-10-23" and an
# empty line, see Lobby._text_message_for): a server name is a group text of at most 64 characters.
BOARD_BYLINE_RESERVE_BYTES = 96
# Events older than this are noted without writing about them: a season created afterwards for past
# dates, or a ladder switched on long after a season ended, must not flood the board.
EVENT_MAX_AGE = timedelta(days=7)
DAYS_PER_WEEK = 7
# Each line the game receives travels with a terminating NUL (Lobby's text_message counts the same).
LINE_TERMINATOR_BYTES = 1


@dataclass(frozen=True)
class BoardText:
    """A text for the board in one language: its title and its lines."""

    title: str
    lines: tuple[str, ...]

    @property
    def body(self) -> str:
        """The lines as one text with line breaks, as the board stores it."""
        return "\n".join(self.lines)


def text_size(title: str, lines: list[str] | tuple[str, ...]) -> int:
    """The bytes a text takes in a TextMessage: title and every line, each with its NUL."""
    size = len(title.encode(TEXT_ENCODING, errors="replace")) + LINE_TERMINATOR_BYTES
    for line in lines:
        size += len(line.encode(TEXT_ENCODING, errors="replace")) + LINE_TERMINATOR_BYTES
    return size


def fit_board_text(title: str, lines: list[str]) -> BoardText:
    """Drop whole lines from the end until the text and the byline fit the game's limit; a text is
    never cut inside a line."""
    kept = list(lines)
    limit = MAX_TEXT_MESSAGE_BYTES - BOARD_BYLINE_RESERVE_BYTES
    while kept and text_size(title, kept) > limit:
        kept.pop()
    return BoardText(title, tuple(kept))


def day_text(moment: datetime, language: str) -> str:
    """A day for a reader of that language: "19. Okt 2026" or "19 Oct 2026"."""
    if language == GERMAN:
        return f"{moment.day}. {GERMAN_MONTHS[moment.month - 1]} {moment.year}"
    return f"{moment.day} {moment:%b %Y}"


def counts_text(counts: SeasonCounts, language: str) -> str:
    """Which ladders a season counts, in words."""
    if counts == SeasonCounts.LIGA:
        return "nur Liga" if language == GERMAN else "Liga only"
    if counts == SeasonCounts.CLANLIGA:
        return "nur Clanliga" if language == GERMAN else "Clanliga only"
    return "Liga und Clanliga" if language == GERMAN else "Liga and Clanliga"


def ranking_lines(rows: list[StandingRow]) -> list[str]:
    """The top of a ranking, one place per line."""
    return [f"{row.place}. {row.name} {row.rating}" for row in rows[:LADDER_TOP_COUNT]]


def season_dates_text(season: Season, language: str) -> str:
    """A season's dates in words: its first and last day, or that an admin ends it."""
    start = day_text(season.starts_at, language) if season.starts_at else ""
    last_day = last_season_day(season)
    if last_day is None:
        if language == GERMAN:
            return f"ab {start}, bis ein Admin sie beendet"
        return f"from {start}, until an admin ends it"
    end = day_text(last_day, language)
    return f"{start} bis {end}" if language == GERMAN else f"{start} to {end}"


@dataclass(frozen=True)
class WeekFacts:
    """What happened in the ladder in one week: who climbed most, who played most, the longest
    winning run, and how many results counted."""

    counted: int
    climber: tuple[str, int] | None
    most_matches: tuple[str, int] | None
    longest_run: tuple[str, int] | None


class LadderTexts:
    """Builds the ladder's lobby texts from the book; writes nothing itself."""

    def __init__(self, database: LobbyDatabase, book: LadderBook) -> None:
        """Texts from this database's results."""
        self._database = database
        self._book = book

    def season_start(self, season: Season, language: str) -> BoardText:
        """The board message when a custom season starts."""
        rating = self._book.settings().start_rating
        if language == GERMAN:
            title = f"{season.name} beginnt"
            lines = [
                f"{season.name}: {season_dates_text(season, GERMAN)}, {counts_text(season.counts, GERMAN)}.",
                f"Alle beginnen bei {rating}; es zählen nur Spiele in diesen Tagen.",
                "",
                f"Rangliste im Spiel: /top {season.name}",
            ]
        else:
            title = f"{season.name} begins"
            lines = [
                f"{season.name}: {season_dates_text(season, ENGLISH)}, {counts_text(season.counts, ENGLISH)}.",
                f"Everyone starts at {rating}; only matches in these dates count.",
                "",
                f"Standings in the game: /top {season.name}",
            ]
        return fit_board_text(title, lines)

    def season_end(self, season: Season, language: str) -> BoardText:
        """The board message when a custom season ends: its champions and final top five."""
        is_german = language == GERMAN
        lines = [season_dates_text(season, language), ""]
        for ladder, label in ((LIGA, "Liga"), (CLANLIGA, "Clanliga")):
            if not season_counts_ladder(season, ladder):
                continue
            rows = self._book.standing_rows(season, ladder)
            if not rows:
                continue
            champion = rows[0]
            if is_german:
                lines.append(f"{label}-Sieger: {champion.name} {champion.rating}")
            else:
                lines.append(f"{label} champion: {champion.name} {champion.rating}")
        liga_rows = self._book.standing_rows(season, LIGA) if season_counts_ladder(season, LIGA) else []
        if liga_rows:
            lines += ["", "Schlussstand" if is_german else "Final standings"]
            lines += ranking_lines(liga_rows)
        counted = sum(
            1
            for record in self._book.results()
            if record.evaluation.state == LadderState.COUNTED and season.includes(record.evaluation)
        )
        lines += ["", f"{counted} Spiele gezählt." if is_german else f"{counted} matches counted."]
        title = f"{season.name} ist vorbei" if is_german else f"{season.name} is over"
        return fit_board_text(title, lines)

    def season_end_announcement(self, season: Season) -> str:
        """The line everyone online hears when a custom season ends."""
        winners = []
        for ladder in (LIGA, CLANLIGA):
            if not season_counts_ladder(season, ladder):
                continue
            rows = self._book.standing_rows(season, ladder)
            if rows:
                winners.append(f"{rows[0].name} {rows[0].rating}")
        if not winners:
            return f"{season.name} is over."
        return f"{season.name} is over: {' and '.join(winners)} win. Final standings on the board."

    def week_facts(self, week_start: datetime) -> WeekFacts:
        """The ladder's week from week_start (a Monday, UTC) on: Liga's best climber, who played most,
        the longest winning run within the week."""
        week_end = week_start + timedelta(days=DAYS_PER_WEEK)
        names = {account.account_id: account.name for account in self._database.list_accounts()}
        in_week = []
        for record in self._book.results():
            evaluation = record.evaluation
            if evaluation.state != LadderState.COUNTED or evaluation.played_at is None:
                continue
            if week_start <= evaluation.played_at < week_end:
                in_week.append(evaluation)
        in_week.sort(key=lambda evaluation: (evaluation.played_at, evaluation.game_id))
        matches: dict[int, int] = {}
        runs: dict[int, int] = {}
        best_runs: dict[int, int] = {}
        for evaluation in in_week:
            for player in evaluation.players:
                matches[player.account_id] = matches.get(player.account_id, 0) + 1
                if evaluation.ladder_type != LadderType.INDIVIDUAL:
                    continue
                # A run of wins grows with each win and starts again at a loss.
                if player.has_won:
                    runs[player.account_id] = runs.get(player.account_id, 0) + 1
                else:
                    runs[player.account_id] = 0
                best_runs[player.account_id] = max(best_runs.get(player.account_id, 0), runs[player.account_id])
        liga = self._book.ratings().liga
        start_rating = self._book.settings().start_rating
        climbs = {}
        for account_id in {player.account_id for evaluation in in_week for player in evaluation.players}:
            rating = liga.get(account_id)
            if rating is None:
                continue
            climbs[account_id] = rating_at(rating, week_end, start_rating) - rating_at(rating, week_start, start_rating)
        return WeekFacts(
            counted=len(in_week),
            climber=best_of(climbs, names),
            most_matches=best_of(matches, names),
            longest_run=best_of(best_runs, names),
        )

    def weekly_summary(self, week_start: datetime, language: str) -> BoardText | None:
        """The weekly summary for the board, None for a week without a counted result."""
        facts = self.week_facts(week_start)
        if facts.counted == 0:
            return None
        is_german = language == GERMAN
        last_day = week_start + timedelta(days=DAYS_PER_WEEK - 1)
        dates = f"{day_text(week_start, language)} bis {day_text(last_day, language)}"
        if not is_german:
            dates = f"{day_text(week_start, language)} to {day_text(last_day, language)}"
        lines = ["Rangliste (unendliche Saison)" if is_german else "Standings (infinite season)"]
        lines += ranking_lines(self._book.standing_rows(INFINITE_SEASON, LIGA))
        lines.append("")
        if facts.climber is not None and facts.climber[1] > 0:
            label = "Aufsteiger der Woche" if is_german else "Climber of the week"
            lines.append(f"{label}: {facts.climber[0]} {facts.climber[1]:+d}")
        if facts.most_matches is not None:
            label = "Meiste Spiele" if is_german else "Most matches"
            lines.append(f"{label}: {facts.most_matches[0]} ({facts.most_matches[1]})")
        if facts.longest_run is not None and facts.longest_run[1] > 1:
            label = "Längste Siegesserie" if is_german else "Longest winning run"
            lines.append(f"{label}: {facts.longest_run[0]} ({facts.longest_run[1]})")
        lines.append(f"{facts.counted} Spiele gezählt." if is_german else f"{facts.counted} matches counted.")
        clan_rows = self._book.standing_rows(INFINITE_SEASON, CLANLIGA)
        if clan_rows:
            lines += ["", "Clanliga: " + ", ".join(f"{row.place}. {row.name} {row.rating}" for row in clan_rows[:2])]
        running = self.running_seasons(week_start + timedelta(days=DAYS_PER_WEEK))
        for season in running:
            last = last_season_day(season)
            until = f" bis {day_text(last, language)}" if is_german else f" until {day_text(last, language)}"
            lines.append(("Läuft: " if is_german else "Running: ") + season.name + (until if last else ""))
        title = f"Wochenbericht {dates}" if is_german else f"Week of {dates}"
        return fit_board_text(title, lines)

    def news_lines(self, language: str) -> list[str]:
        """The ladder's block under the news: the top five and the running seasons; empty while
        nobody has a place."""
        rows = self._book.standing_rows(INFINITE_SEASON, LIGA)[:LADDER_TOP_COUNT]
        if not rows:
            return []
        places = ", ".join(f"{row.place}. {row.name} {row.rating}" for row in rows)
        is_german = language == GERMAN
        lines = [("Liga (unendliche Saison): " if is_german else "Ladder (infinite season): ") + places + "."]
        for season in self.running_seasons(self._database.now()):
            last = last_season_day(season)
            if is_german:
                until = f" bis {day_text(last, GERMAN)}" if last else ""
                lines.append(f"Läuft: {season.name} ({counts_text(season.counts, GERMAN)}){until}.")
            else:
                until = f" until {day_text(last, ENGLISH)}" if last else ""
                lines.append(f"Running: {season.name} ({counts_text(season.counts, ENGLISH)}){until}.")
        lines.append("Im Spiel: /top und /rank." if is_german else "In the game: /top and /rank.")
        return lines

    def logon_line(self, account_id: int) -> str | None:
        """A player's rating and place at logon, None for a player without a Liga place."""
        rows = self._book.standing_rows(INFINITE_SEASON, LIGA)
        row = next((row for row in rows if row.side_id == account_id), None)
        if row is None:
            return None
        parts = [f"Liga: {row.rating}, place {row.place} of {len(rows)}."]
        for season in self.running_seasons(self._database.now()):
            season_rows = self._book.standing_rows(season, LIGA)
            season_row = next((item for item in season_rows if item.side_id == account_id), None)
            if season_row is not None:
                parts.append(f"{season.name}: place {season_row.place} of {len(season_rows)}.")
        return " ".join(parts)

    def decision_line(self, evaluation: Evaluation, game_name: str) -> str:
        """What a player of a decided dispute hears at the next logon."""
        if evaluation.state != LadderState.COUNTED:
            return f"Ladder: an admin decided that {game_name} does not count."
        names = {account.account_id: account.name for account in self._database.list_accounts()}
        winners = ", ".join(
            names.get(player.account_id, DELETED_ACCOUNT_NAME) for player in evaluation.players if player.has_won
        )
        return f"Ladder: an admin decided {game_name}: {winners} won."

    def running_seasons(self, moment: datetime) -> list[Season]:
        """The custom seasons running at a moment, by their start."""
        seasons = [season for season in self._database.list_ladder_seasons() if season.is_running(moment)]
        return sorted(seasons, key=lambda season: season.starts_at)


def season_counts_ladder(season: Season, ladder: str) -> bool:
    """Whether a season counts this ladder (LIGA or CLANLIGA)."""
    if ladder == LIGA:
        return season.counts in (SeasonCounts.LIGA, SeasonCounts.BOTH)
    return season.counts in (SeasonCounts.CLANLIGA, SeasonCounts.BOTH)


def rating_at(rating: Rating, moment: datetime, start_rating: int) -> int:
    """A side's rating just before a moment: its last value before it, the start rating before its
    first result."""
    value = float(start_rating)
    for played_at, after in rating.history:
        if played_at >= moment:
            break
        value = after
    return round(value)


def best_of(values: dict[int, int], names: dict[int, str]) -> tuple[str, int] | None:
    """The account with the highest value and its value; ties go to the lower id, so the choice
    never changes between two runs."""
    if not values:
        return None
    account_id = min(values, key=lambda key: (-values[key], key))
    return names.get(account_id, DELETED_ACCOUNT_NAME), values[account_id]


def week_start_of(moment: datetime) -> datetime:
    """The Monday 00:00 UTC that starts moment's week."""
    day = moment.astimezone(timezone.utc).date()
    monday = day - timedelta(days=day.weekday())
    return datetime.combine(monday, time(), tzinfo=timezone.utc)


@dataclass(frozen=True)
class WrittenTexts:
    """What one run of the writer leaves for the lobby to send: announcements for everyone online."""

    announcements: tuple[str, ...] = ()


class LadderWriter:
    """Writes the ladder's events onto the board, once each: custom seasons starting and ending,
    and the summary of the last full week. Runs in the server's housekeeping while the ladder is on."""

    def __init__(self, database: LobbyDatabase, book: LadderBook) -> None:
        """A writer on this database."""
        self._database = database
        self._texts = LadderTexts(database, book)

    @property
    def texts(self) -> LadderTexts:
        """The text builder, for the lobby's news block and lines."""
        return self._texts

    def run(self) -> WrittenTexts:
        """Write what is due; returns the announcements to send."""
        now = self._database.now()
        themes = self.server_themes()
        announcements = []
        for season in self._database.list_ladder_seasons():
            has_started = season.starts_at is not None and season.starts_at <= now
            has_ended = season.ends_at is not None and season.ends_at <= now
            if has_started and not has_ended and self._is_new_event(f"season-start:{season.season_id}"):
                if now - season.starts_at <= EVENT_MAX_AGE:
                    self._post(themes, {language: self._texts.season_start(season, language) for language in themes})
            if has_ended and self._is_new_event(f"season-end:{season.season_id}"):
                if now - season.ends_at <= EVENT_MAX_AGE:
                    self._post(themes, {language: self._texts.season_end(season, language) for language in themes})
                    announcements.append(self._texts.season_end_announcement(season))
        last_week = week_start_of(now) - timedelta(days=DAYS_PER_WEEK)
        if self._is_new_event(f"weekly:{last_week:%Y-%m-%d}"):
            self._post(themes, {language: self._texts.weekly_summary(last_week, language) for language in themes})
        keep = self._database.get_integer_setting(Setting.LADDER_BOARD_MESSAGES)
        for theme in themes.values():
            self._database.delete_board_messages_but_newest(theme.theme_id, keep)
        return WrittenTexts(tuple(announcements))

    def server_themes(self) -> dict[str, BoardTheme]:
        """The server's own board theme per language, created on first use."""
        themes = {
            theme.language: theme
            for theme in self._database.list_board_themes()
            if theme.is_server_written and theme.language in BOARD_THEME_NAMES
        }
        for language, name in BOARD_THEME_NAMES.items():
            if language not in themes:
                themes[language] = self._database.add_board_theme(
                    language, name, BOARD_THEME_DESCRIPTIONS[language], is_server_written=True
                )
        return themes

    def _is_new_event(self, event_key: str) -> bool:
        """True the first time an event is seen; noted before anything is written, so a restart in
        the middle never writes it twice."""
        return self._database.record_ladder_event(event_key)

    def _post(self, themes: dict[str, BoardTheme], texts: dict[str, BoardText | None]) -> None:
        """Post each language's text under that language's theme; a text the database refuses is
        logged and left out."""
        for language, theme in themes.items():
            text = texts.get(language)
            if text is None or not text.lines:
                continue
            try:
                self._database.post_board_message(theme.theme_id, text.title, text.body, is_by_server=True)
            except DatabaseError as error:
                log.warning("ladder: board text %r not posted: %s", text.title, error)
