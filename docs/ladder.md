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

### Seasons and all time

Both run side by side:

- **All time:** every counted result since the ladder started, never reset.
- **Seasons:** the current season's standings start everyone at the start value and use only that
  season's results. A season is a date range set by admins (a name, a start, an end); the setting
  `ladder_season_months` (default 3) proposes the next one automatically when the current one ends.
  Past seasons stay readable with their final standings and champions.

Because ratings are derived, a season is only a filter on the results; nothing is copied or reset
in the database.

## 3. What the players see

- **During the match**, unchanged from the game's point of view: the answer to an end report is
  0x6c ("stored") when the report itself passes the rules above, 0x6a when it cannot count
  (computer opponents, wrong slot count, flag 2). Whether the result then counts is decided after
  the other reports arrived, which the game cannot wait for.
- **Back in the lobby**, a server line to the players of the match: "Liga: Alice beats Bob, 1016 ->
  1032 (Bob 984 -> 968)", "Clanliga: Romans beat Amazons, 1012 -> 1028", or why it did not count.
- **Chat commands:** `/rank [NAME]` (season and all time: rating, place, won, lost, lost by
  leaving, provisional or not), `/top` (the season's first five), `/clanrank [CLAN]` (Clanliga
  rating and strength from Liga), `/season` (name and end of the current season).
- **Home tab:** the news text can end with the season's top five players and clans. The game
  fetches it once per logon, so it shows the standings as of the logon (protocol.md,
  GetTextMessage).
- **Web interface:** a public page `/ladder` like the live lobby, with tabs Liga, Clanliga and
  clan strength, a switch between the current season, past seasons and all time, and the last
  results; the rating on each account and clan page; on each game page whether and how it counted.
- Not planned: showing the rank in the member list's clan column. It would work technically (the
  column shows whatever text the server sends), but it hides the clan and changes what a run
  confirmed.

## 4. What admins can do

- See every ladder game with its state: counted, disputed, void, with the reason.
- Decide a disputed result (pick the winner) or void a counted one, in the web interface and with
  `s3lobby ladder decide|void GAME_ID`; ratings follow at once because they are derived.
- Manage seasons: `s3lobby ladder season add NAME --start DATE --end DATE` and on the web.
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
  | `ladder_season_months` | 3 | length proposed for the next season |

## 5. Database (one migration, 0010, for every dialect)

- `ladder_results`: game id, ladder type, state (counted, disputed, void), reason, winning team,
  game time, decided at, decided by (server or an admin account).
- `ladder_result_players`: result id, account id, clan id at the time (for Clanliga), team, race,
  won, left.
- `ladder_seasons`: name, start, end.
- `accounts.clan_joined_at`: when the account joined its current clan (null for the members of
  today; they count as members since before the ladder started).
- No rating table: ratings are computed from the results when asked and cached in memory by the
  web interface and the server; the number of results of a small lobby keeps this cheap.

The raw reports stay in `game_reports` as they are; a result can always be evaluated again from
them.

## Mockups

Static pages on the web interface's own stylesheets, with made up names and numbers
(`docs/mockups/ladder/`, each page as HTML and in light and dark):

| Page | What it shows |
|---|---|
| [Liga](mockups/ladder/ladder-liga-light.png) ([dark](mockups/ladder/ladder-liga-dark.png)) | the public `/ladder` page: season choice, standings with rating, last change, won, lost, left, provisional players, and the last results |
| [Clanliga](mockups/ladder/ladder-clanliga-light.png) ([dark](mockups/ladder/ladder-clanliga-dark.png)) | the clans' Clanliga ratings and their strength from Liga |
| [Ladder games](mockups/ladder/ladder-admin-light.png) ([dark](mockups/ladder/ladder-admin-dark.png)) | for admins: every evaluated game with its state and reason, deciding and voiding, the seasons and their champions |
| [Game page](mockups/ladder/ladder-game-light.png) ([dark](mockups/ladder/ladder-game-dark.png)) | the ladder part of a game's page, and the server lines and chat command answers the players read in the game |

## Building it in steps

1. **Evaluate silently.** With `ladder_enabled` off the server keeps answering 0x6a and sending the
   "not ranked" line, but evaluates every ladder game into `ladder_results`. The admin pages show
   the outcome, so the rules can be checked against real matches first. A run has to confirm the
   open points below.
2. **Standings.** Liga and Clanliga ratings, clan strength, seasons and all time, the public
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
4. Seasons and all time side by side (section 2, Seasons and all time).
5. Clanliga in the first version.
