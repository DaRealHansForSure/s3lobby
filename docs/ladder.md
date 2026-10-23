# Ladder concept ("Liga" and "Clanliga")

Status: a concept, nothing of it is built yet. It describes how s3lobby could rank ladder games
with the data the unmodified game already sends, what has to be decided first, and in which steps
it could be built. Facts about the game cite `docs/protocol.md` or an S3.EXE address; everything
else is a proposal.

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
castle; protocol.md 0x1f). A match therefore ends either when one side is wiped out or when the
other players left; there is no surrender and no draw in the report.

## Today

Every report is stored in `game_reports` and linked to its game (migration 0008); the web
interface decodes it. A ladder report is answered with 0x6a, and every player of a ladder game
gets a server line that ladder games are not ranked.

## Proposal

### 1. When a ladder result counts

A ladder game becomes a **result** only when all of these hold. Each failed rule is stored as the
reason, so admins see why a match did not count.

1. The game was opened on this server as a ladder game (description and reports agree on ladder
   type and flag 1), and no report has flag 2 (the game's own "not valid").
2. **Liga (type 1):** exactly two slots, both human (no computer flag), two different accounts on
   two teams. **Clanliga (type 2):** every human slot belongs to an account that showed a clan when
   it joined, each team is a single clan, and there are exactly two different clans. No computer
   opponents in either.
3. Every slot's lobby player id is an account that joined the game in the lobby (`game_players`).
4. There is a winning team, and the game time is at least a minimum (proposed 10 minutes, a
   setting), so a game ended right after the start cannot farm points.
5. **The reports agree.** Every end report that arrived names the same winner and the same slots.
   Either all players sent an end report, or the missing ones are marked as having left (slot flag
   2) in the others' reports: that is a win by the opponent leaving, which counts like any other
   win (*inferred* from 0x434b19 that the stayer's report marks the leaver; to confirm in a run).
6. A player who leaves before the match is decided and never comes back loses; a game where nobody
   sends an end report within a timeout after the game closed (proposed 10 minutes) is void.

Reports are tied to the logged on connection that sent them, so one player cannot send another
player's report. A player can still lie about the outcome; rule 5 then turns the match into a
**disputed** result that counts for nobody and is listed for admins to decide.

### 2. Rating

**Elo**, separately for Liga (per account) and Clanliga (per clan):

- Everyone starts at 1000. After a result the winner gains and the loser loses
  `K * (1 - expected)`, `expected = 1 / (1 + 10 ^ ((loser - winner) / 400))`.
- K is 32 for the first 10 results of a player (provisional, shown as such) and 16 afterwards.
- In Clanliga the two clans are rated against each other; the players' Liga ratings are untouched,
  but their personal Clanliga wins and losses are counted.
- **Ratings are derived, never stored as the truth.** They are recomputed from the list of counted
  results in order. An admin who voids or decides a result can therefore change history without
  any rating going wrong, and a change of K or the start value applies to everything at once.
- Abuse limits (settings): results between the same two accounts count at most N times a day
  (proposed 3), against win trading between friends.

Simpler alternatives, if Elo is too much: points per win (3 win, 0 loss) or a plain win rate with
a minimum number of games. Elo rewards beating strong players and suits a small community where
not everyone plays everyone.

**Seasons** (optional): a season is a date range; the standings show the current season, older
ones stay readable. Since ratings are derived, a season is only a filter on results.

### 3. What the players see

- **During the match**, unchanged from the game's point of view: the answer to an end report is
  0x6c ("stored") when the report itself passes the rules above, 0x6a when it cannot count
  (computer opponents, wrong slot count, flag 2). Whether the result then counts is decided after
  the other reports arrived, which the game cannot wait for.
- **Back in the lobby**, a server line to the players of the match: "Liga: Alice beats Bob, 1016 ->
  1032 (Bob 984 -> 968)", or why it did not count.
- **Chat commands:** `/rank [NAME]` (rating, place, wins, losses, provisional or not), `/top`
  (the first five), `/clanrank [CLAN]`.
- **Home tab:** the news text can end with the current top five. The game fetches it once per
  logon, so it shows the standings as of the logon (protocol.md, GetTextMessage).
- **Web interface:** a public page `/ladder` like the live lobby (standings for Liga and Clanliga,
  the last results), the rating on each account page for admins, and on each game page whether and
  how it counted.
- Not proposed: showing the rank in the member list's clan column. It would work technically (the
  column shows whatever text the server sends), but it hides the clan and changes what a run
  confirmed.

### 4. What admins can do

- See every ladder game with its state: counted, disputed, void, with the reason.
- Decide a disputed result (pick the winner) or void a counted one, in the web interface and with
  `s3lobby ladder decide|void GAME_ID`; ratings follow at once because they are derived.
- Settings: `ladder_enabled` (off by default), minimum game time, the daily limit per pair, the
  timeout for missing reports, the current season.

### 5. Database (one migration, 0010, for every dialect)

- `ladder_results`: game id, ladder type, state (counted, disputed, void), reason, winning team,
  game time, decided at, decided by (server or an admin account).
- `ladder_result_players`: result id, account id, clan id (for Clanliga), team, race, won.
- No rating table: ratings are computed from the results when asked and cached in memory by the
  web interface and the server; the number of results of a small lobby keeps this cheap.

The raw reports stay in `game_reports` as they are; a result can always be evaluated again from
them.

## Building it in steps

1. **Evaluate silently.** With `ladder_enabled` off the server keeps answering 0x6a and sending
   the "not ranked" line, but evaluates every ladder game into `ladder_results`. The admin pages
   show the outcome, so the rules can be checked against real matches first. A run has to confirm
   the open points below.
2. **Standings.** The rating, the public `/ladder` page, `/rank` and `/top`, the server line after a
   match, the admin decisions.
3. **Tell the game.** With `ladder_enabled` on, valid end reports get 0x6c and the "not ranked"
   line goes away. Off by default until a run with the real game shows the message (project rule
   for anything the game has not seen from this server).
4. **Clanliga** and seasons.

## To confirm in a run first

- A Liga match between two machines played to the end: both end reports, same winner, both slot
  records with the right account ids.
- The same match where the loser quits: does the winner's report mark the loser with flag 2, and
  does the loser's client send anything?
- The 0x6c answer during a ladder match (the game should show "Game status stored at the Lobby
  server.").
- What the game does in a Clanliga setup when a player has no clan.

## Questions for the owner

1. Elo, or simple points per win?
2. Does a win by the opponent leaving count fully?
3. Minimum game time (10 minutes?) and the daily limit per pair (3?).
4. Seasons from the start, or one everlasting ladder?
5. Clanliga in the first version, or later?
