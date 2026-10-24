# Issue log

Every problem met while building and running s3lobby: in runs with the real game, in the tests,
in the tools. Each entry says what was seen, the cause as far as it is known, what was done, and
the evidence, so nobody has to find the same thing twice. Newest first. Open issues stay here
with "open" until they are understood; CLAUDE.md lists the ones left for later on purpose.

How to add one: a heading with the date and a short title, then **Seen**, **Cause**, **Fix** (the
commit, or "open"), and **Evidence** (a log time, an S3.EXE address, a test). Facts about the game
cite their source as everywhere in this project (`docs/protocol.md`).

## Family: the game keeps its lobby state across a server restart

Several issues share one trigger: the server restarts while a game stays open, the game logs on
again, and it carries what it knew of the lobby into the new session. The server starts empty.
A run that restarts the server while a game sits in each tab would show the whole family.

### 2026-10-24: a remembered board theme is refused after a server restart

- **Seen:** after a server restart the MessageZone showed old entries, and opening a theme
  failed with "Der Wechsel in die Gruppe hat nicht geklappt". After going to the GameZone and
  back it worked. Wine, Bob, lobby-console.log 13:02:55 to 13:03:08.
- **Cause:** the game joined the MessageZone (4) and then the remembered theme groups
  (196865, 196610) directly, without joining the language group MessageThemesGerman (131072)
  first. The server built a language's theme groups only when that language group was joined,
  so the themes did not exist yet and the join was refused as "no such group".
- **Fix:** `73f49bc`: a join that names a missing board, chat theme or permanent chat group id
  builds that range first and looks again.
- **Evidence:** the log above; session test
  `test_a_remembered_theme_is_joined_without_its_language_group` fails without the fix.

### 2026-10-23: the MessageZone join is resent after a server restart (open)

- **Seen:** after a server restart while the game stayed logged on, its join of the MessageZone
  (block 2, AddPlayerToGroup 4) was resent every half second although every server datagram
  acknowledged it; the answer was byte for byte the one that worked minutes before. A freshly
  started game was fine. lobby.log 23:25:45 onwards (that log is gone).
- **Cause:** open. Probably the same family: the game expects something from its old state.
- **Fix:** open. Next step: restart the server while a game stays in the MessageZone, and compare
  the client's acknowledgement handling (send 0x606ff0, resend timer).

## The game

### 2026-10-24: the Home tab's news stays empty at the first visit (open)

- **Seen:** at the first visit to Home after the game started, the news area stayed empty, also
  at the next visit; after a new logon (the game still open) the news showed. Wine, Bob,
  lobby.log 13:17:51 to 13:18:20.
- **Cause:** open, *inferred* to be the game's display. The server sent the statistics and the
  text id list with the join (13:17:51.852), the game asked for text 0x91 (13:17:52.355) and got
  it a millisecond later (title "Title", text "Text"). The game keeps a fetched text for as long
  as it runs (0x60bbe0) and did not ask again, so it had the text; it seems to fill the page only
  when it builds it, not when the text arrives.
- **Fix:** open. An idea to try in a run: send the statistics again right after the text, as their
  arrival redraws the Home table, and maybe the text with it.
- **Evidence:** lobby.log around 13:17:52; the screenshots of the owner's run.

### 2026-10-24: the ChatZone's theme list stays empty (open)

- **Seen:** sometimes, after switching tabs, the ChatZone showed no themes while the group list
  below showed the theme's group ("[Server] Deutsch"). Wine, Bob, lobby.log 13:11:08.
- **Cause:** open; the server's answer was complete. The game joined the ChatZone and the theme
  it was in last in one block, and the server answered in one block with the ChatZone, its four
  themes, the joined theme and the permanent group in it. The game's list stayed empty anyway,
  so it is the game's display; *inferred*: it sometimes draws the screen before the theme list
  is filled.
- **Fix:** open. An idea to try only with evidence: answer the remembered theme's join in a block
  of its own, after the ChatZone's is acknowledged. Next step: note the time of a bad and a good
  switch and compare the two blocks.
- **Evidence:** lobby.log line by line around 13:11:08.917 (server block 0x1e).

### 2026-10-24: permanent chat groups "do not appear"

- **Seen:** no "[Server]" groups in the ChatZone after the feature was built.
- **Cause:** the setting `permanent_chat_groups` was off (its default until a run confirms
  it), and the Chat themes page, where the groups are managed, did not say so.
- **Fix:** `7299748` links the setting from that page while it is off; the section shows an
  on/off badge. Switching it on made the groups appear.
- **Evidence:** the settings table of the owner's database held no value for it.

### 2026-10-24: the game ignores the registry's language

- **Seen:** `--language pl` (the registry value `Language` set to 21) left the game German, and
  its patch check still reported German.
- **Cause:** the game takes its language from the voice archive: the sound loader reads the u32
  at offset 0x14 of each `Snd\siedler3_NN.dat` header (0x5fe760) and keeps the first that is not
  0xffffffff; 0x498142 copies it into the configuration.
- **Fix:** `scripts/game-language.sh` changes that field in a test copy of the game.
- **Evidence:** a run with the changed field reported English and Polish (lobby-console.log
  00:14:40, 00:15:59).

### 2026-10-23: "General" disappears and the ChatZone fails

- **Seen:** after an admin added a chat theme, "General" was gone and entering the ChatZone
  showed "Der Wechsel in die Gruppe hat nicht geklappt".
- **Cause:** General was offered only while no theme existed. The game rejoins the theme it was
  in last as soon as the tab opens; that theme no longer existed.
- **Fix:** `6da9b36`: General is built in and always listed under its old id.
- **Evidence:** lobby-console.log, "cannot join group 65536: no such group" at 23:50:01.

### 2026-10-23: a kicked or banned player cannot read the reason

- **Seen:** kicks and bans worked, but the reason line never showed; the game went straight to
  "connection to the lobby server dropped".
- **Cause:** the server closed the connection right after the line.
- **Fix:** `e63b133` closes it about six seconds later (`KICK_REASON_READING_SECONDS`), and
  `1ba38e4` labels the reason ("Reason: ..." or "No reason given."), as a bare colon after the
  ban's date was easy to miss.

### 2026-10-23: the board's order is not applied in the game

- **Seen:** themes ordered on the web showed in another order in the game.
- **Cause:** the game sorts a group's visible groups by group id (CGroup operator< 0x5db570),
  not in the order they arrive.
- **Fix:** `6b41d54`: a board theme's group id holds its place on the board.

### 2026-10-23: a chat hint on the MessageZone

- **Seen:** the server line about chatting on the board appeared, but the MessageZone screen has
  no chat field.
- **Fix:** `a5cc948` removed the hint and its setting.

## The server

### 2026-10-24: a password with an undefined Windows-1252 byte breaks the logon

- **Seen:** found while building Create Player: a password holding one of the five bytes
  Windows-1252 leaves undefined (such as 0x81) arrives as a surrogate.
- **Cause:** encoding it for scrypt raised UnicodeEncodeError instead of refusing the logon.
- **Fix:** `e5d23dc` encodes with surrogateescape.

## The database

### 2026-10-24: `database copy` refuses every new target

- **Seen:** the copy test failed after migration 0014: "the target is not empty: chat_themes".
- **Cause:** migration 0014 fills chat themes in every new database, so no fresh target was empty.
- **Fix:** `c4f464c`: tables a migration seeds are replaced by the source's rows.

### 2026-10-23: an edited migration stops the server

- **Seen:** after migration 0010 was changed, a database that had applied it refused to start.
- **Cause:** the checksum guard: an applied migration must never change.
- **Fix:** the change went into a new migration (0013); CLAUDE.md now says never to edit a file
  the owner's database has applied.

## The web interface

### 2026-10-24: a double line under the admin menu

- **Seen:** two lines under the admin menu when a page had no section links, the upper one wider
  than the content.
- **Fix:** `94d7249`: one line, the header's; the section links get a line as wide as the content.

### 2026-10-24: ladder reports called "unranked here"

- **Seen:** every ladder game's reports were labelled "ladder game, unranked here" on the games
  page, though the ladder ranks them.
- **Fix:** `be7e53d`.

## Tools and tests

### 2026-10-24: the Info screenshot shows the owner's home folder

- **Seen:** the README's Info picture showed the demo database as an absolute path, and with it
  the user name of the machine that took the pictures. Published with 0.12.0.
- **Cause:** the screenshot script gave the web interface the database's absolute path, and the
  Info page shows the location as it was given.
- **Fix:** `47285db`: the script opens the database by its bare file name and refuses to take a
  picture of a page that shows the home folder. The history of `main`, `develop` and the tags
  v0.12.0 and v0.12.1 was rewritten so every Info picture in it is the clean one, and
  force-pushed to both remotes.
- **Evidence:** every blob of `docs/images/info-*.png` reachable from main, develop and the tags is
  one of the two new pictures.

### 2026-10-24: a README picture shows a stopped server

- **Seen:** retaking the screenshots before a merge, the account page lost Jin's "online" badge,
  though nothing in the code had changed.
- **Cause:** the screenshot script wrote the demo's live lobby once at the start; the web
  interface takes a live state older than 30 seconds (`LIVE_STATE_STALE_SECONDS`) for a stopped
  server, and the pages late in the list were taken after that.
- **Fix:** the script writes the live state again before each page.
- **Evidence:** the earlier and the new account picture compared pixel by pixel.

### 2026-10-24: a long test run is killed

- **Seen:** the suite against MySQL and MariaDB in one command ended with exit code 137 before
  MariaDB finished.
- **Cause:** the command ran past the tool's time limit (MySQL alone takes almost seven minutes).
- **Fix:** long runs go into the background, one database at a time.

### 2026-10-24: ImageMagick draws the logo wrong

- **Seen:** the PNG from `magick logo.svg` lost the shield's gold outline and drew the flagpole
  black.
- **Cause:** ImageMagick's own SVG renderer.
- **Fix:** pictures from SVG and HTML are taken with headless Chrome.

### 2026-10-24: a test helper waits for the wrong message

- **Seen:** session tests joining a chat group timed out.
- **Cause:** `join_group` in tests/support.py waits for AddGroupToGroup (0x0f), which rooms get;
  a chat group comes back as AddGroupToGroup2 (0x20).
- **Fix:** tests that join chat groups use `join_chat_group` (tests/test_chat_zone.py).
