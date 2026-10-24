# Changelog

Every notable change to s3lobby. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0.0 a minor
version may change behaviour; the owner decides when 1.0.0 comes.

## [Unreleased]

### Added

- The Chat themes page shows beside "Permanent chat groups" whether their setting is on (green)
  or off (red).
- The log names every migration a database receives ("applied migration 0016
  permanent_chat_groups"), and the server's start line gives the schema version.
- More ladder chat commands: `/opponents` (Liga members online, closest to your rating first, also
  "Online now" on the Ladder page), `/results` (your last results with rating changes), `/vs NAME`
  (your record against one player), `/pending` (your games still waiting for a result),
  `/season NAME` (a season's members, top three and your place), `/top clanliga` and
  `/ladder rules`.
- Who is in which ladder, everywhere: the Ladder page counts and names each ladder's members, the
  player pages and the admin account pages list a player's ladders (with Join and Take out for
  admins), and the ladder's MessageZone theme starts with a message on how to join, what counts
  and how many joined, which the server keeps up to date and on top. The news block names the
  member counts too.
- Permanent chat groups: with the setting `permanent_chat_groups` (off until a run with the game
  confirms it) every chat theme keeps chat groups open, marked "[Server]", that never close and
  that players join to chat at once. Each theme starts with one named after it (General too);
  admins add and remove them on the Chat themes page or with `s3lobby chat-theme add-group` and
  `remove-group` (migration 0016).
- `s3lobby board move-theme|move-message ID up|down` and `hide-theme`, `show-theme`,
  `hide-message`, `show-message`: the board's order and visibility from the command line, as on
  the web; `board list` marks hidden and server-written themes.

### Changed

- **Players take part only in the ladders they join.** `/ladder join` in the game (or
  `/ladder join clanliga`, `/ladder join SEASON`), `/ladder leave` and `/ladder`; admins use the new
  Ladder > Members page or `s3lobby ladder join|leave|members`. A ladder game whose players did not
  all join a ladder it counts in is stored nowhere, the game hears "not valid for ladder", and the
  players hear who had not joined. A player who leaves keeps their results for their opponents and
  starts at the start rating when joining again. Migration 0015 adds the memberships. Results
  stored before were played before anyone joined, so they count for nobody.

### Fixed

- Group ids cannot run out into the game's reserved id: once the last id is given out, opening
  another game or chat group is refused with "Der Wechsel in die Gruppe hat nicht geklappt" and a
  warning in the log; a permanent chat group whose id would reach the players' groups is not
  opened.
- A game's reports no longer call a ladder game "unranked here": the ladder ranks it, and the
  game's page shows the result.

## [0.11.0] - 2026-10-24

### Added

- Scripts for testing with the real game: `scripts/run-game-wine.sh` starts a copy under Wine in a
  window of its own, and `scripts/game-language.sh` sets the language a test copy reports, for the
  chat themes of one language.
- Chat themes for one language: a theme can be shown only to games of that language (web form
  "Shown to", `chat-theme add --language`). The lobby learns a game's language from its patch
  check. A new lobby starts with the rooms the game's manual describes: Meeting Room, Beginners
  and one per language (Deutsch, English, Français, Italiano, Polski, Español), each only for its
  own language. Admins may remove them; General always stays.
- The game's "Create Player" button can make accounts: setting `allow_create_player`, off by
  default (Settings, Logons). The password typed in the game becomes the account's access code
  (at least 8 characters, any case); each address creates at most three accounts at once, then
  one an hour. A taken name is never reported as taken (the game's Result 2): every refusal is
  the wrong password one, and a player's own password on their own name simply logs on.
- A ladder for Liga and Clanliga ([docs/ladder.md](docs/ladder.md)). The server evaluates every
  ladder game once its reports are in: it counts when the players' reports agree, the match lasted
  long enough (`ladder_minimum_minutes`) and the players joined it in the lobby; disagreeing
  reports make it disputed, and two copies of one installation, computer opponents, a restart
  during the game or a fourth result between the same sides in a day make it void. Elo ratings
  for players (Liga) and clans (Clanliga), a clan strength from its members' Liga ratings, an
  infinite season and custom seasons admins create (migration 0010).
- `s3lobby ladder`: standings and results per season as text, JSON (with a format number) or CSV;
  show, decide and void a game's result; create, change, end and delete custom seasons.
- Chat commands `/rank`, `/top`, `/clanrank` and `/seasons`, limited per player.
- A public Ladder page beside the live lobby (top three, rating chart, standings with form and win
  rate, last results), a Ladder games page for admins (decide, void, seasons), the ladder's
  numbers on the Settings page, and the ladder result on each game's page.
- While the ladder is on, the server writes it into the lobby: a board theme of its own ("Liga",
  "Ladder") with season starts, season ends and a weekly summary, the top five under the news,
  an announcement when a custom season ends, and at logon a line about a decided dispute and,
  with `ladder_logon_line`, the player's place. Board themes the server writes cannot be posted
  into. Settings `ladder_board_messages` and `ladder_news_standings`.
- Setting `ladder_enabled` (off until a run confirms it): ladder reports that can count are
  answered with "Game status stored at the Lobby server." instead of "not valid for ladder", the
  "not ranked" line goes, and players hear their result after the match.
- A public page per player (`/players/NAME`): their ladder place and record, their rating over
  time, their place in running custom seasons, and the games the lobby saw them play with
  opponents, race, result and time of play. Names in the standings and the live lobby link to it.
- The message board in an order admins choose: themes and messages are dragged into place
  (SortableJS, bundled with the web interface) or moved with Up and Down, new messages come
  first, and the server's ladder theme starts on top. Themes and messages can be hidden from the
  game; the server's own theme and messages can only be hidden, not removed (migration 0011).
  The game sorts a list's groups by their id (0x5db570), so each board theme's group id now holds
  its place on the board.
- Admins act on players from the live lobby: each player has Message (a server line only that
  player sees, if online), Kick and Ban; `s3lobby announce TEXT --to NAME` does the same from the
  command line, and the Announcements page shows whom each line was for and whether it arrived
  (migration 0012). The live lobby has a search, and long name lists show the first names and how
  many more there are.
- Kicks, bans, removals, voiding and deciding a ladder game, and ending or deleting a season ask
  for a confirmation in a dialog first.
- Statistics of the ladder: how often each race is played and wins, and which maps are played and
  how long their matches take, on the Ladder page ("Races and maps"), on each player page (their
  races) and with `s3lobby ladder stats` (migration 0013 keeps each result's map).
- Long lists in the admin area (accounts, games, ladder games) come in pages of 50; the accounts
  page has a name search.
- `scripts/start.sh --follow` shows the lobby's log right after starting it.

### Changed

- The web interface is split: the live lobby and the ladder show everyone the same, and every admin
  page lives under `/admin` with a start page of its own and a grouped navigation.
- Forms line up on their labels, the settings are grouped into sections, and tables stay on one
  line (short dates, the game type in one word, winners by name).
- Putting a member into the clan it is in already keeps the date it joined.
- A settings form that does not send a field keeps that setting's stored value.

### Removed

- The setting `board_chat_hint` and its server line: the MessageZone screen has no chat field
  (seen in a run), so there is no chat there to warn about.

### Fixed

- `s3lobby database copy` accepts a new target again: the chat themes a new database starts
  with no longer count as content and are replaced by the source's.
- "General" stays on the ChatZone tab when admins add their own themes. The game joins the theme
  it was in last as soon as the tab opens, and failed with "Der Wechsel in die Gruppe hat nicht
  geklappt" once General was gone (seen in a run). It can no longer be added a second time.
- A kicked or banned player can read the reason: the connection now closes a few seconds after
  the reason line instead of at once, which sent the game straight to its "connection dropped"
  dialog (seen in a run). The others still see the player leave at once.
- The kick and ban lines name the admin's reason as "Reason: ...", or say "No reason given."
  when there is none; a bare colon after the ban's end date was easy to miss.
- A logon whose password holds one of the five bytes Windows-1252 leaves undefined (such as 0x81)
  is refused like any wrong code; hashing it raised an error before.

### Key commits

- [`8a7b0a8`](https://github.com/DaRealHansForSure/s3lobby/commit/8a7b0a8) feat(ladder): evaluate ladder games once their reports are in
- [`60db657`](https://github.com/DaRealHansForSure/s3lobby/commit/60db657) feat(web): give admins an area of their own under /admin
- [`7ef29e7`](https://github.com/DaRealHansForSure/s3lobby/commit/7ef29e7) feat(logon): let the game's create player button make accounts

## [0.10.0] - 2026-10-23

### Fixed

- Chat groups with a password open again: the game opens them under group 5, which the server
  did not have, so the creator got "Der Wechsel in die Gruppe hat nicht geklappt". Group 5 exists
  now (hidden), and such a chat group is listed in the creator's chat theme for the others.
- A player removed by a kick, a ban or a deleted account gets the server's CloseConnection only
  after acknowledging the block with the reason: a lost reason line no longer turns the clean close
  into a sync error in the game.
- A banner with id 0, which the game ignores, is no longer offered, and a banner address must be
  `ftp://HOST/PATH` without port or user, the only form the game can fetch.
- A player who opens a chat group or a game is told its id (Result 0xc), as the game expects:
  the chat group is their channel at once, so chat, whisper and new games work without leaving
  and joining again, and the game setup's updates name the game.
- A chat group whose last player left stays open for two minutes, and as long as a game opened
  in it is open: the game leaves the chat group when its player opens a game there and joins it
  again on the way back, and found it closed (under Wine the screen then froze).
- A player who joined a game no longer gets the host's updates for it: the game takes no update for
  a group its player is in (0x5d8de6).
- A logon no longer fails when the database cannot record the session, and a match report that
  cannot be stored is still answered; both only feed the statistics.
- The verbose log hides a chat group's password in the clan forms of a join (0x25, 0x26) too.
- The web interface reads a live lobby written by a newer server, ignoring fields it does not know.

### Added

- Clans can be marked green: their members who show the clan get a green row in the game's member
  lists (the only colour the game has for this), switched on the Clans page or with
  `s3lobby clan highlight` (migration 0009). Off for every clan; not yet tried with the game.
- `scripts/stop.sh` stops every running lobby server and web interface cleanly, `--force`
  kills what still runs after ten seconds.
- `scripts/start.sh` starts the lobby server and the web interface together in the background,
  with `--dev` for development mode, and checks that both came up.
- `s3lobby serve --dev`: every account also logs on with the password `1234`, from this machine
  and its LAN only, for testing with the game; it refuses to start with `--public-host` or `--upnp`.
  It also accepts several players with the same serial number, whatever `unique_serial_numbers`
  says, so two copies of one installation can be online at once.
- `s3lobby web --dev`: admins also log on to the web interface with `1234`, from this machine and
  its LAN only; the session counts as the admin's newest access code and ends when that code is
  revoked. It refuses to start with `--behind-proxy`.
- The Games page decodes games fully: the setup the host chose (game type, map, teams, computer
  opponents, player limit, Amazons, version, checksum, session), who joined, when the match
  started and how it ended; each match report shows its game, game time, flags and every slot.
  The server keeps a game's setup without the host's address and its players (migration 0008),
  and links each report to its game.
- A page for each game (`/games/ID`): when it was opened, started, decided and closed, the winners,
  the teams with slot, race and lobby id, the setup, who joined when, and every report.
  Players link to their account page while the account exists, with its id.
- Each account page lists the games the account joined, with its team, race and result.

### Changed

- `mark_admins` is on by default: admins get a green row in the game's member lists, now that a
  run confirmed the message. A server that turned it off keeps it off.
- The web interface shows names in the colour the game's member lists draw them in (live lobby,
  accounts, account pages, clans): green, or blue for the clan BB.
- Match reports and game pages show the time of play in minutes and seconds: one tick of the
  game's time is 64 ms, read from the game.
- `s3lobby serve --verbose --log-file FILE` writes the datagrams only into the file; the console
  shows INFO, so it stays readable during a test run.
- A slot's flag 0x4 means its race was "Random" in the setup (the report holds the race drawn);
  it was shown as "no player number". The race numbers 0 to 3 are now read from the setup's texts.
- A match report without a winner says "No winner yet" instead of "Match started": a game with a
  single team names its winner already in the report sent at the start.

### Security

- The image list (the Home tab's banner) is sent only for a group the player is in; the game drops
  the connection for any other.
- Datagrams forged with a logged on player's address can no longer act for the player or end their
  session with a single blind try: lobby messages outside numbered blocks are refused (the game
  never sends any), block numbers are accepted only within 32 of the expected one, and a new
  connection from the address is ignored while the player's game still sends its keepalives.

### Key commits

- [`dca8e6a`](https://github.com/DaRealHansForSure/s3lobby/commit/dca8e6a) fix(lobby): name a new chat group as the creator's channel
- [`f7f5254`](https://github.com/DaRealHansForSure/s3lobby/commit/f7f5254) fix(server): refuse forged datagrams from a logged on player's address
- [`3a2d88e`](https://github.com/DaRealHansForSure/s3lobby/commit/3a2d88e) feat(web): decode games fully on the games page

## [0.9.0] - 2026-10-23

### Added

- Admin chat commands wait for `/confirm` with a code only the admin sees.
- MySQL connections encrypted and verified on request (`?tls=verify`, `?tls=verify-ca&ca=PATH`).
- Indexes for the statistics queries (migration 0007).

### Changed

- Each database backend has a file of its own in `s3lobby/backends/`.
- Tighter types and shared constants.

### Fixed

- SIGTERM stops the server cleanly; a start that fails halfway closes what it opened.
- Database failures no longer break the lobby, and reach callers as `DatabaseError`.
- Strict number checks, checked access code labels and ban reasons, and a Games page that
  survives malformed reports.
- Announcements queued while the server was stopped are dropped openly instead of being marked
  as sent.

### Security

- The UPnP client ignores proxies and redirects, talks only to the device that answered, accepts
  only a public external address, and forwards only game ports of machines in the LAN.
- The reachability probe goes only to confirmed connections.

### Key commits

- [`245accd`](https://github.com/DaRealHansForSure/s3lobby/commit/245accd) feat(commands): make admin chat commands wait for a confirmation code
- [`41236a5`](https://github.com/DaRealHansForSure/s3lobby/commit/41236a5) feat(database): encrypt and verify MySQL connections on request
- [`d7683ba`](https://github.com/DaRealHansForSure/s3lobby/commit/d7683ba) refactor(database): give each database backend a file of its own

## [0.8.0] - 2026-10-23

### Added

- SECURITY.md: the game sends its password and chat in plain text, and what that means.
- A roadmap in the README, with ranked ladder games.

### Changed

- The GitHub Actions run on Node.js 24.

### Fixed

- Bytes the game's code page leaves undefined no longer break the database and the live lobby.

### Security

- Web interface: no open redirect after logon; server-side sessions (migration 0006) that a logout
  ends and that last at most 12 hours; no lockout of admins by others; bounded logon checks;
  no-store caching, isolation headers, HSTS and a `__Host-` cookie with HTTPS.
- Server: guarded housekeeping, a block size budget, capped acknowledgements and timeouts, no
  answers before a logon, a logon deadline and a total log budget.
- Game logon failures counted per address, a budget of code checks, and limits on wrong group
  passwords and group updates per player; admin rights read at each command.

### Key commits

- [`0010486`](https://github.com/DaRealHansForSure/s3lobby/commit/0010486) fix(web): harden the web interface after a security review
- [`7b9a086`](https://github.com/DaRealHansForSure/s3lobby/commit/7b9a086) fix(server): keep one client from stopping the server or flooding others
- [`0dccb84`](https://github.com/DaRealHansForSure/s3lobby/commit/0dccb84) docs(security): warn about the game's plain text password in SECURITY.md

## [0.7.0] - 2026-10-23

### Added

- Kicks and bans of players who are online: `/kick` and `/ban` in the chat, `s3lobby account kick`,
  and a button on the web interface, through a kick queue in the database (migration 0005).
- The server closes connections cleanly when it removes a player and when it stops.
- `/net` tells a player the address the server sees and what to forward on their router.
- An optional check whether other players can reach a player directly (`check_reachability`).
- Port forwarding on the router by UPnP (`--upnp`), with an outside port per LAN player.
- Optional marking of admins in the member lists (`mark_admins`).

### Fixed

- Players on the server's machine or in its LAN are shown to others at addresses they can reach.

### Key commits

- [`d2ec4a1`](https://github.com/DaRealHansForSure/s3lobby/commit/d2ec4a1) feat(lobby): remove kicked and banned players from the running lobby
- [`83b6282`](https://github.com/DaRealHansForSure/s3lobby/commit/83b6282) fix(server): show every player at an address the receiving client can reach
- [`3ec5690`](https://github.com/DaRealHansForSure/s3lobby/commit/3ec5690) feat(server): forward ports on the router by UPnP with --upnp

## [0.6.0] - 2026-10-23

### Added

- Players of ladder games ("Liga", "Clanliga") are told these games are not ranked, and the game
  shows "Game not valid for ladder." for their match reports.
- The live lobby shows each game's map, team count and ladder flag.
- The Games page decodes match reports: map, outcome, players, teams and races.

### Changed

- The protocol notes are corrected, and their open questions trimmed.

### Key commits

- [`a54db47`](https://github.com/DaRealHansForSure/s3lobby/commit/a54db47) feat(lobby): tell players that ladder games are not supported
- [`a3b8938`](https://github.com/DaRealHansForSure/s3lobby/commit/a3b8938) feat(web): show map, teams and ladder games live, and decode match reports
- [`b773839`](https://github.com/DaRealHansForSure/s3lobby/commit/b773839) docs(protocol): correct and complete the protocol notes

## [0.5.0] - 2026-10-23

### Added

- MySQL and MariaDB as an alternative to SQLite (optional `mysql` extra), chosen by a
  `mysql://user@host/name` database location.
- Schema migrations as numbered SQL files per database, recorded with checksums; older SQLite
  files are upgraded in place.
- `s3lobby database status`, `migrate` and `copy --to`, to move a lobby between databases.
- GitHub Actions run the tests on SQLite, MySQL and MariaDB.
- The MIT License.

### Changed

- The README is the project's front page, with a logo, a screenshot and guides.
- The settings column is called `name` (migration 0004), as KEY is reserved in MySQL.

### Key commits

- [`09b3377`](https://github.com/DaRealHansForSure/s3lobby/commit/09b3377) feat(database): run on SQLite or MySQL, with migration files
- [`23a9089`](https://github.com/DaRealHansForSure/s3lobby/commit/23a9089) ci: run the tests on GitHub Actions
- [`2dcd39a`](https://github.com/DaRealHansForSure/s3lobby/commit/2dcd39a) chore: license s3lobby under MIT

## [0.4.0] - 2026-10-23

### Added

- The game's language is read from its version check.
- Game blocks, game descriptions and match reports are decoded in full.

### Changed

- s3lobby lives in a repository of its own.

### Fixed

- The server never tells the game that a patch is needed, whatever version it reports.

### Key commits

- [`d306cbb`](https://github.com/DaRealHansForSure/s3lobby/commit/d306cbb) fix(protocol): never tell the game that a patch is needed
- [`38892b1`](https://github.com/DaRealHansForSure/s3lobby/commit/38892b1) feat(protocol): decode game blocks and game descriptions
- [`8d07390`](https://github.com/DaRealHansForSure/s3lobby/commit/8d07390) feat(protocol): decode match reports in full

## [0.3.0] - 2026-10-23

### Added

- A web interface (optional `web` extra, Flask): a public live lobby, and pages for admins to
  manage accounts, access codes, clans, news, themes, the board, settings and announcements.
- The running server writes its lobby to the database once a second for the web interface.
- Access codes are accepted in any case, and the code a logon matched is recorded.

### Changed

- Every setting is stored under its own key.

### Fixed

- Texts and settings the game cannot show are refused when they are stored.

### Key commits

- [`851ba04`](https://github.com/DaRealHansForSure/s3lobby/commit/851ba04) feat(web): add a web interface for the live lobby and administration
- [`c5aabf1`](https://github.com/DaRealHansForSure/s3lobby/commit/c5aabf1) feat(live): write the running lobby to the database for other processes
- [`ac13a8c`](https://github.com/DaRealHansForSure/s3lobby/commit/ac13a8c) refactor(database): store every setting under its own key

## [0.2.0] - 2026-10-23

### Added

- A SQLite database for accounts with access codes, clans, news, chat themes, the message board,
  statistics and settings.
- Administration subcommands (`account`, `clan`, `news`, `chat-theme`, `board`, `setting`,
  `announce`) and `s3lobby serve`.
- Logon with accounts made on the server and random access codes; the game's own "create player"
  is refused. Clan members are shown with their clan.
- The Home tab with news, statistics and the advert banner, the chat themes on the ChatZone tab,
  and the message board on the MessageZone tab.
- Chat commands `/who`, `/clan`, `/help` and, for admins, `/announce`; server lines for every player.

### Fixed

- The server mirrors which groups each client sees, and handles games the way the client expects;
  the game dropped the connection otherwise.

### Security

- Rate limits, size checks and bounded state against hostile and broken datagrams.

### Key commits

- [`138c987`](https://github.com/DaRealHansForSure/s3lobby/commit/138c987) feat(database): add the SQLite database for accounts, clans and lobby content
- [`3418f0a`](https://github.com/DaRealHansForSure/s3lobby/commit/3418f0a) feat(logon): log on with server-made accounts and show clans
- [`492f16d`](https://github.com/DaRealHansForSure/s3lobby/commit/492f16d) fix(lobby): mirror what each client sees and handle games as the client expects

## [0.1.0] - 2026-10-23

### Added

- A lobby server for the original The Settlers III that speaks the game's own UDP protocol, read
  from its code: the reliable connection layer, the version check, the lobby server list, the
  logon, the lobby tabs and game zones, opening games and handing the match to DirectPlay.
- The Python package `s3lobby` with `python -m s3lobby`, standard library only.
- A unittest suite with a background server and a fake game client that asserts the message sizes
  the game asserts.
- `docs/protocol.md`: the protocol with the address in the game's executable for every fact.
- README, packaging and CLAUDE.md.

### Key commits

- [`1b43d38`](https://github.com/DaRealHansForSure/s3lobby/commit/1b43d38) feat(server): add a LAN lobby server for the original Settlers 3
- [`b76d102`](https://github.com/DaRealHansForSure/s3lobby/commit/b76d102) refactor: rework the lobby server into the s3lobby package
- [`46582ce`](https://github.com/DaRealHansForSure/s3lobby/commit/46582ce) docs(protocol): move the lobby protocol document into the project

[Unreleased]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/DaRealHansForSure/s3lobby/releases/tag/v0.1.0
