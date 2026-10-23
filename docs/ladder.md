# Ladder concept ("Liga" and "Clanliga")

Status: a concept, nothing of it is built yet. It describes how s3lobby will rank ladder games with
the data the unmodified game already sends, and in which steps it is built. Facts about the game
cite `docs/protocol.md` or an S3.EXE address; everything else is design. The owner's decisions of
2026-10-23 are listed at the end and built into the text.

## What the game gives us

The game has no ladder of its own. All it knows is:

- **The choice in the game setup.** The host picks the ladder type: 0 normal game, 1
  "Ladder: 1 on 1" (shown as "Liga"), 2 "Ladder Game: Clans" ("Clanliga"). It travels in the game
  block of 0x20 and in the game description (u32 1 for a ladder game), which the host rewrites on
  every change of the setup (protocol.md, 0x20 and Game description).
- **The zone "League 1 vs 1"** (group 30), where such games are usually listed.
- **Two match reports per player (0x1f GameStatus).** Every client sends one when the match starts
  and one when a single team is left (0x53da69, 0x53ae66). Each carries the lobby game id, flags
  (1 ladder game, 2 "not valid for the ladder" as the game itself judges it: a ladder slot without
  a lobby player id, 0x5405ec), the winning team, the game time in 64 ms ticks, the map and one
  record per slot: lobby player id (our account id), race, slot, team, and flags (1 computer
  opponent, 2 a human who left the running match, 4 random race).
- **Two answers it can show.** During a ladder match only, Result 0x6c shows "Game status stored at
  the Lobby server." and 0x6a shows "Game status inconsistencies. Game not valid for ladder."
  (0x540a18, 0x540a3e, 0x540b89).

The game has no screen for standings, ranks or points: the only ladder texts in S3.EXE are
"Clanliga" and the log line "LOBBY: Invalid lobby id in liga game". Whatever a player sees of the
ladder has to come from the server: server lines, chat command answers, the Home tab's text, and
the web interface.

The winner is the only team that still owns a finished military building (small tower, big tower,
castle), and a player's buildings count only while that player still plays (protocol.md 0x1f,
+0x11943dc). A match therefore ends either when one side is wiped out or when the other side's
players left; there is no surrender and no draw in the report.

## Today

Every report is stored in `game_reports` and linked to its game (migration 0008); the web
interface decodes it. A ladder report is answered with 0x6a, and every player of a ladder game
gets a server line that ladder games are not ranked.

## 1. When a ladder result counts

A ladder game becomes a **result** only when all of these hold. Each failed rule is stored as the
reason, so admins see why a match did not count.

1. The game was opened on this server as a ladder game (description and reports agree on ladder
   type and flag 1), and no report has flag 2 (the game's own "not valid").
2. **Liga (type 1):** exactly two slots, both human (no computer flag), two different accounts on
   two teams. **Clanliga (type 2):** two teams of equal size (1 on 1 up to 4 on 4), no computer
   opponents, every player showed a clan when joining the game, each team is one clan, and the two
   clans differ.
3. Every slot's lobby player id is an account that joined the game in the lobby (`game_players`).
4. There is a winning team, and the game time is at least `ladder_minimum_minutes` (default 10),
   so a game ended right after the start cannot farm points.
5. **The reports agree.** Every end report that arrived names the same winner and the same slots.
   Players who did not send one must be marked as having left (slot flag 2) in the others' reports.
6. A game where no end report arrives within `ladder_report_timeout_minutes` (default 10) after the
   game closed is void.

Reports are tied to the logged on connection that sent them, so one player cannot send another
player's report. A player can still lie about the outcome; rule 5 then turns the match into a
**disputed** result that counts for nobody and is listed for admins to decide.

### A player who leaves

The game cannot tell a player who quits on purpose from one whose game crashed or whose connection
broke: either way the stayer's report marks the slot with flag 2 (*inferred* from 0x434b19; to
confirm in a run) and the stayer wins because the leaver's buildings stop counting.

Recommendation, built in this way:

- **Left after the minimum game time: a full loss**, a full win for the other side. By then the
  match was a real one, and treating a late quit any softer would make quitting a way out of a lost
  game, the most common abuse of any ladder.
- **Left before the minimum game time: void**, for both sides. Early leaves are mostly crashes,
  DirectPlay trouble or a wrong setup, and the same minimum already keeps short games out.
- **Clanliga:** a team loses by leaving only when all of its players left; one player leaving
  while teammates fight on changes nothing, the end report decides as usual.
- **Visible, not punished further:** each player's count of matches lost by leaving is shown next
  to the rating (web, `/rank`), so admins and players see a habit; admins can void any result.

## 2. Rating

### Liga (players)

**Elo**, everyone starts at `ladder_start_rating` (1000). After a result the winner gains and the
loser loses `K * (1 - expected)`, `expected = 1 / (1 + 10 ^ ((loser - winner) / 400))`. K is
`ladder_k_provisional` (32) for a player's first `ladder_provisional_games` (10) results, shown as
"provisional", and `ladder_k` (16) afterwards.

### Clans

Clans get **two** numbers, because they answer different questions:

1. **Clanliga rating: how strong the clan is as a team.** Elo between clans, from Clanliga matches
   only, same formula and start value. A team match moves the clan ratings by the full K, whatever
   the team size, so a 3 on 3 does not count three times. The players' personal Clanliga record
   (played, won, lost, left) is kept as well, but no personal rating: in a team match one player's
   share of the win cannot be read from the report.
2. **Clan strength from Liga: how strong its players are.** The average Liga rating of the clan's
   best three members who are no longer provisional and played a Liga result in the last
   `ladder_active_days` (30). A clan with fewer than three such members shows no strength. This
   gives small clans that rarely field a full Clanliga team a place in a list too, and it cannot be
   inflated by one strong member alone.

Rules that keep clans fair:

- **A result belongs to the clan of the moment.** Each result stores the clan every player showed
  in that game, so a player who changes clan takes no Clanliga history along; the old clan keeps
  its wins and losses.
- **No mercenaries:** a player has to have been in the clan for `ladder_clan_member_days` (default
  3) before the match, or the match is void with that reason. Otherwise a clan could borrow a
  strong player for one evening. This needs the date a player joined their clan, which the
  database does not record yet (migration below).
- A deleted clan keeps its results in the history, listed under its last name.

### Shared rules

- **Ratings are derived, never stored as the truth.** They are recomputed from the list of counted
  results in order. An admin who voids or decides a result can therefore change history without
  any rating going wrong, and a changed setting applies to everything at once.
- **Win trading:** results between the same two accounts (Liga) or the same two clans (Clanliga)
  count at most `ladder_pair_daily_limit` (default 3) times per day; later ones are stored as void
  with that reason.

### Seasons

- **The infinite season** always runs: every counted result since the ladder started, never reset.
  It is the ladder players see by default, in the game and on the web.
- **Custom seasons** are created by admins whenever they like: a name, a start, an end (or none
  until an admin ends it), and which ladders count (Liga, Clanliga or both). Examples: a "Winter
  Cup" for December, a weekend tournament, a Clanliga season for the summer. In a custom season
  everyone starts at the start value and only that season's results count. Several custom seasons
  may run at the same time; one match then counts in each of them and in the infinite season.
- Admins can rename a custom season, move its dates, end it early or delete it. An ended season
  stays readable with its final standings and champions.

Because ratings are derived, a season is only a filter on the results; nothing is copied or reset
in the database, and a season created afterwards for past dates shows its standings at once.

## 3. What the players see

- **During the match**, unchanged from the game's point of view: the answer to an end report is
  0x6c ("stored") when the report itself passes the rules above, 0x6a when it cannot count
  (computer opponents, wrong slot count, flag 2). Whether the result then counts is decided after
  the other reports arrived, which the game cannot wait for.
- **Back in the lobby**, a server line to the players of the match: "Liga: Alice beats Bob, 1016 ->
  1032 (Bob 984 -> 968)", "Clanliga: Romans beat Amazons, 1012 -> 1028", or why it did not count.
- **Chat commands:** `/rank [NAME]` (infinite season and every running custom season: rating,
  place, won, lost, lost by leaving, provisional or not), `/top [SEASON]` (the first five, of the
  infinite season unless one is named), `/clanrank [CLAN]` (Clanliga rating and strength from
  Liga), `/seasons` (the running custom seasons and when they end).
- **Home tab:** the news text can end with the infinite season's top five players and clans. The game
  fetches it once per logon, so it shows the standings as of the logon (protocol.md,
  GetTextMessage).
- **Web interface:** a public page `/ladder` like the live lobby, with tabs Liga, Clanliga and
  clan strength, a choice between the infinite season and the custom ones (running first, then ended), and the last
  results; the rating on each account and clan page; on each game page whether and how it counted.
- Not planned: showing the rank in the member list's clan column. It would work technically (the
  column shows whatever text the server sends), but it hides the clan and changes what a run
  confirmed.

### Texts the server writes into the lobby

The game has two places for longer texts, and both suit the ladder in different ways. Both hold at
most 1200 bytes per text (`MAX_TEXT_MESSAGE_BYTES`) and come in a German and an English form,
because the game picks the news by its language (0x45351b) and the board by its language group.

- **The board (MessageZone), for things worth keeping.** A theme "Ladder" (German "Liga") that the
  server writes itself and admins cannot post into. It gets one message per event, not per match:
  a custom season starting (name, dates, what counts), a custom season ending (final top five and
  the champion of Liga and Clanliga), and a weekly summary (best climber, most matches, longest
  winning run, the top five). The newest `ladder_board_messages` (default 20) are kept, older ones
  are deleted. A run on Windows 10 showed the board and its messages working.
- **The news (Home tab), for the current state.** The admins' news text stays as they wrote it; with
  `ladder_news_standings` on, the server appends a short block when it hands out the text: the top
  five of the infinite season and the running custom seasons with their end. The game fetches the
  news once per logon, so a player sees the state of their logon, which is what news is.
- **Server lines, for the moment.** The line after a match (above), and with `ladder_logon_line` on,
  one line at logon: "Liga: 1043, place 6. Autumn Cup until 30 Nov: place 3." Off by default,
  because a line at every logon is noise for players who do not play the ladder; it is only sent
  to players with a ladder result.
- **Announcements to everyone online** only for the end of a custom season ("Autumn Cup is over:
  Jin wins with 1088"), since that is the one event everyone should hear at once.

Per match, nothing is written to the board or the news: a busy evening would push everything else
out, and the match line and the web page already carry it.

## 4. What admins can do

- See every ladder game with its state: counted, disputed, void, with the reason.
- Decide a disputed result (pick the winner) or void a counted one, in the web interface and with
  `s3lobby ladder decide|void GAME_ID`; ratings follow at once because they are derived.
- Manage custom seasons on the web and with `s3lobby ladder season add NAME --start DATE
  [--end DATE] [--ladder liga|clanliga|both]`, `season edit`, `season end` and `season delete`.
- Every number is a setting, on the Settings page and with `s3lobby setting set`:

  | Setting | Default | Meaning |
  |---|---|---|
  | `ladder_enabled` | false | answer valid reports with 0x6c and drop the "not ranked" line |
  | `ladder_minimum_minutes` | 10 | shorter matches and earlier leaves are void |
  | `ladder_report_timeout_minutes` | 10 | how long after the game closed reports may still arrive |
  | `ladder_pair_daily_limit` | 3 | counted results per pair of players or clans and day |
  | `ladder_start_rating` | 1000 | everyone's rating before the first result |
  | `ladder_k` | 16 | Elo K after the provisional results |
  | `ladder_k_provisional` | 32 | Elo K for the first results |
  | `ladder_provisional_games` | 10 | how many results are provisional |
  | `ladder_active_days` | 30 | how recent a Liga result must be for the clan strength |
  | `ladder_clan_member_days` | 3 | how long a player must be in a clan for Clanliga |
  | `ladder_inactive_days` | 60 | hide players without a result for this long from the infinite season |
  | `ladder_board_messages` | 20 | how many messages the server keeps in its board theme |
  | `ladder_news_standings` | true | append the top five and running seasons to the news |
  | `ladder_logon_line` | false | a line with rating and place at logon, for ladder players only |
  | `ladder_max_custom_seasons` | 50 | how many custom seasons may exist |

### Edge cases

- **Two copies of one installation** (same serial number, as in development mode) never count
  against each other: void with that reason. The same IP address is no reason, since players on
  one LAN share it.
- **A server restart during a match** loses the lobby game the reports refer to (game ids live in
  memory). Reports for an unknown game are stored as today and the match is void ("server
  restarted"), never guessed.
- **Renamed or deleted accounts:** results keep the account id. A deleted account shows as
  "deleted player" in old results and leaves the standings; the ratings of everyone else stay as
  they are, because results are never removed with it.
- **Banned players** leave the standings while banned; their past results keep counting for their
  opponents.
- **Inactive players:** after `ladder_inactive_days` (default 60) without a result a player is hidden
  from the infinite season's standings (not from custom ones) until their next result. The rating
  itself does not decay; Elo has no reason to punish a pause.
- **Disputes decided later:** the players of a disputed match get a server line at their next logon
  telling them how it was decided.
- **Times** of seasons and results are UTC, like the rest of the web interface.
- **Clanliga in the game:** whether the game itself demands clans for a Clanliga setup is not known
  yet (in the run list below); the server checks the rules either way.
- **Later, if wanted:** statistics per race and per map (who wins with which race, which maps are
  played), and an export of all results as CSV for other tools.

### Security

The ladder adds no new way in: it reads reports the server already accepts, and everything else is
admin pages and server output. What it must keep, following the network and web rules of the
project:

- **Reports.** Only from a logged on player's numbered blocks, as today. New: a report counts for
  the ladder only for a game its sender joined in the lobby (`game_players`); today the server
  stores a report for any game id a logged on player names, which is fine for statistics but not
  for a ranking. A forged datagram that guesses the next block number could still send a report in
  a player's name (SECURITY.md); the agreement rule turns such a report into a dispute, never into
  a result, as long as the real players' reports disagree with it. The admins' decision is the
  backstop.
- **Bounds.** Report slots are already bounded by the decoder; results per game, seasons (at most
  `ladder_max_custom_seasons`, default 50) and generated texts (1200 bytes, the newest 20 board
  messages) are bounded too, so no player and no admin action can grow them without limit.
- **Generated texts** are built only from text checked when it was stored: player names, clan and
  season names (`check_game_text`), numbers and dates; nothing a client typed into the chat. The
  whole text passes `check_game_text` again before it is stored or sent; one that fails is logged
  and dropped. Too long texts lose whole lines from their end. A board text leaves room for the
  byline the server puts in front of every board message when it sends it (author and date); the
  server's `text_message` would otherwise drop the last lines to stay within 1200 bytes.
- **Once per event:** each board message and announcement is stored with its event, so a restart
  never posts it twice and a crash in the middle never posts half.
- **Chat commands** (`/rank`, `/top`, `/clanrank`, `/seasons`) get a rate limit per player, their
  arguments are cut to `MAX_GROUP_TEXT_LENGTH`, and the answers go through `fit_answer`. Ratings are
  computed once per change and cached, so a burst of commands costs no recomputation.
- **Housekeeping** (the report timeout, the weekly summary, season ends) runs guarded like the
  other housekeeping steps, so a database failure in one cannot stop the rest.
- **Web:** the public `/ladder` page shows only names, clans, ratings and results, all of which the
  live lobby already shows; addresses, access codes and reports' raw bytes stay on admin pages.
  Every admin form carries `csrf_field()`, every admin blueprint redirects visitors, and the pages
  use no inline script or style.

### Without the web interface

The lobby runs with and without the web interface, and the ladder must too. Anyone with the code
should be able to build their own pages, bots or tools on it. So:

- **The logic is a module of its own,** `s3lobby/ladder.py`, standard library only like the server:
  evaluating a game into a result, computing ratings from results (`compute_ratings(results,
  settings)`, a pure function), standings per season, clan strength, and building the generated
  texts. The server, the command line and the web interface all call it; none of them holds
  ladder rules of its own.
- **Players need no web page:** results, standings and seasons reach them in the game through the
  match line, the chat commands, the board and the news.
- **Every admin action has a command,** the web pages are only a second way to the same functions:

  ```sh
  s3lobby ladder games [--state disputed|waiting|counted|void] [--season NAME]
  s3lobby ladder show GAME_ID
  s3lobby ladder decide GAME_ID --winner TEAM
  s3lobby ladder void GAME_ID [--reason TEXT]
  s3lobby ladder standings [--season NAME] [--ladder liga|clanliga|clans]
  s3lobby ladder season list|add|edit|end|delete ...
  s3lobby ladder texts [--preview weekly|news]
  s3lobby setting set ladder_minimum_minutes 15
  ```

- **Machine readable output for other tools:** `ladder standings` and `ladder results` take
  `--format text|json|csv`. The JSON is documented in `docs/ladder.md` once built and versioned
  with a `format` number, so a change that would break a reader gets a new number.
- **The data is plain tables** (`ladder_results`, `ladder_result_players`, `ladder_seasons`, below)
  in SQLite or MySQL, documented with their columns, so a tool may also read the database directly,
  as the web interface does with the live lobby. Ratings are not stored; a tool computes them with
  `compute_ratings` or takes them from the JSON.

## 5. Database (one migration, 0010, for every dialect)

- `ladder_results`: game id, ladder type, state (counted, disputed, void), reason, winning team,
  game time, decided at, decided by (server or an admin account).
- `ladder_result_players`: result id, account id, clan id at the time (for Clanliga), team, race,
  won, left.
- `ladder_seasons`: name, start, end (null while open), ladders counted, created by. The
  infinite season is not a row: it is all results.
- `accounts.clan_joined_at`: when the account joined its current clan (null for the members of
  today; they count as members since before the ladder started).
- No rating table: ratings are computed from the results when asked and cached in memory by the
  web interface and the server; the number of results of a small lobby keeps this cheap.

The raw reports stay in `game_reports` as they are; a result can always be evaluated again from
them.

## Mockups

Static pages on the web interface's own stylesheets and the UI kit's components, with made up names
and numbers (`docs/mockups/ladder/`, each page as HTML and in light and dark). The few new classes
they need are in `docs/mockups/ladder/ladder.css`, built from the kit's tokens and without inline
styles, so the real pages can take them over into `app.css` as they are.

| Page | What it shows |
|---|---|
| [Liga](mockups/ladder/ladder-liga-light.png) ([dark](mockups/ladder/ladder-liga-dark.png), [phone](mockups/ladder/ladder-liga-light-phone.png)) | the public `/ladder` page: tabs for Liga, Clanliga and clan strength, season and player filters, the top three as cards, the rating over the last weeks as a chart, the standings with form (last five results), win rate bars and losses by leaving, and the last results |
| [Clanliga](mockups/ladder/ladder-clanliga-light.png) ([dark](mockups/ladder/ladder-clanliga-dark.png)) | the clans as cards with rating, form, usual team size and members, and their strength from Liga as a bar chart |
| [Ladder games](mockups/ladder/ladder-admin-light.png) ([dark](mockups/ladder/ladder-admin-dark.png)) | for admins: games filtered by state with counts, the decision dialog for a disputed game, the infinite and custom seasons as cards with progress, a form for a new custom season, and the ladder settings |
| [Ladder texts](mockups/ladder/ladder-texts-light.png) ([dark](mockups/ladder/ladder-texts-dark.png)) | for admins: the board theme the server writes with its messages and their size, the weekly summary in German and English, season start and end, the news with the ladder block, the server lines, and the checks every generated text passes |
| [Game page](mockups/ladder/ladder-game-light.png) ([dark](mockups/ladder/ladder-game-dark.png)) | the ladder part of a game's page: both end reports side by side, the verdict, the rating changes per season, and what the players read in the game |

## Building it in steps

1. **Evaluate silently.** With `ladder_enabled` off the server keeps answering 0x6a and sending the
   "not ranked" line, but evaluates every ladder game into `ladder_results`. The admin pages show
   the outcome, so the rules can be checked against real matches first. A run has to confirm the
   open points below.
2. **Standings.** Liga and Clanliga ratings, clan strength, the infinite season and custom seasons, the public
   `/ladder` page, the chat commands, the server line after a match, the admin decisions.
3. **Tell the game.** With `ladder_enabled` on, valid end reports get 0x6c and the "not ranked"
   line goes away. Off by default until a run with the real game shows the message (project rule
   for anything the game has not seen from this server).

## To confirm in a run first

- A Liga match between two machines played to the end: both end reports, same winner, both slot
  records with the right account ids.
- The same match where the loser quits: does the winner's report mark the loser with flag 2, and
  does the loser's client send anything?
- A Clanliga match 2 on 2 (four copies, or two machines with two copies each): slots, teams and
  what the game does when a player has no clan.
- The 0x6c answer during a ladder match (the game should show "Game status stored at the Lobby
  server.").

## Decisions (owner, 2026-10-23)

1. Elo, and a concept for clans (section 2, Clans).
2. Leaving: decided by recommendation (section 1, A player who leaves).
3. Minimum game time and daily limit per pair, both configurable (section 4).
4. An infinite season that always runs, and custom seasons admins create (section 2, Seasons).
5. Clanliga in the first version.
