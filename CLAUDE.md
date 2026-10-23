# s3lobby

Lobby server for the online lobby of the original The Settlers III (Blue Byte, 1998). It replaces
Blue Byte's long dead `siedler3svr.bluebyte.de:3344` so unmodified copies of the game can use their
built-in lobby again. Open source project in a repository of its own. It began as a subfolder of
another project; its history was carried over, so the early commits describe that layout.

## Layout

- `s3lobby/constants.py`: every number of the protocol (MessageType, ResultCode, GroupFlag, sizes,
  timing). New numbers go here, never as literals in other modules.
- `s3lobby/wire.py`: bytes only: message header, blocks, FieldReader/FieldWriter, sockaddr_in.
- `s3lobby/messages.py`: one frozen dataclass per message with `encode()` and `decode()`.
- `s3lobby/connection.py`: the reliable layer for one client. No network access, testable with a
  fake clock.
- `s3lobby/lobby.py`: players, rooms, zones, games; one handler per lobby message type.
- `s3lobby/database.py`: LobbyDatabase, everything the lobby stores: accounts with access codes,
  clans, news, chat themes, board, sessions, games, game reports, settings. Its SQL is written once
  with `?` placeholders in the part of SQL that SQLite and MySQL share.
- `s3lobby/backends.py`: where the data lives. `SqliteBackend` (a file, the default) and
  `MysqlBackend` (MySQL 8.0.19+ or MariaDB 10.5+ through PyMySQL), chosen by the database location:
  a path, or `mysql://user@host/name`. Placeholders, transactions, dict rows, upserts, key errors.
- `s3lobby/migrations.py` and `s3lobby/schema/<dialect>/NNNN_name.sql`: the schema as numbered
  migration files, recorded with checksums in the table `schema_migrations`. A schema change adds
  the next number to every dialect folder (same number, same meaning); released files are never
  edited. Old SQLite files that only had PRAGMA user_version are adopted.
- `s3lobby/server.py`, `s3lobby/cli.py`, `s3lobby/admin.py`: asyncio UDP server, command line,
  administration subcommands.
- `s3lobby/addresses.py`: the address each client is shown for another (loopback and LAN players
  rewritten per receiver), applied to every message in `LobbyServer.send`.
- `s3lobby/upnp.py`, `s3lobby/forwarding.py`: UPnP port forwarding on the router (`--upnp`): the
  server's port and one outside port per LAN player, on a worker thread.
- `s3lobby/reachability.py`: the optional probe (setting `check_reachability`) whether others can
  reach a player directly, from a second UDP port.
- `s3lobby/live.py`: the running lobby as JSON; the server writes it to the database once a second
  when it changed, the web interface reads it. The two processes share nothing but the database.
- `s3lobby/web/`: the Flask web interface (`app.py` factory, one blueprint module per area,
  `templates/`, `static/`). Public live lobby, everything else for admins only.
- `docs/protocol.md`: the protocol as read from the game, with the S3.EXE address of every fact.
- `tests/`: unittest. `support.py` has a background server and a fake game client.

## Commands

Run from this folder:

- Tests: `python3 -m unittest`
- Server: `python3 -m s3lobby serve --verbose --log-file lobby.log`
- Administration: `python3 -m s3lobby account add NAME` and the other subcommands (`--help`)
- Web interface: `.venv/bin/python -m s3lobby web --database s3lobby.sqlite3` (needs Flask)
- All tests including the web ones: `.venv/bin/python -m unittest` (a `.venv` with `flask`)
- The database and session tests against MySQL or MariaDB (needs `pymysql`; drops every table of
  that database first): `S3LOBBY_TEST_DATABASE=mysql://user:password@127.0.0.1:3306/s3lobby_test
  .venv/bin/python -m unittest`. A throwaway server: `podman run -d --rm -p 127.0.0.1:3306:3306
  -e MYSQL_ROOT_PASSWORD=... -e MYSQL_DATABASE=s3lobby_test -e MYSQL_USER=... -e MYSQL_PASSWORD=...
  docker.io/library/mysql:8.4` (or `mariadb:11.4` with `MARIADB_` variables).
- Database: `python3 -m s3lobby database status|migrate|copy --to LOCATION`

Python 3.10 or newer. The server and the command line use the standard library only; do not add
dependencies. Two optional extras are the exceptions. Flask for `s3lobby/web/` (`web`): nothing
outside `s3lobby/web/` imports it, `s3lobby/web/__init__.py` imports nothing (so test discovery
works without Flask), and `cli.py` imports the web app only inside the `web` command. PyMySQL for
MySQL (`mysql`): imported only inside `MysqlBackend.__init__`, so SQLite needs nothing extra.

## Rules

- Behaviour that a run with the real game confirmed is not changed without new evidence from the
  game (its code or a recorded session). Several odd looking rules exist only because the client
  breaks without them: the order Result before OwnPlayer, the leave confirmation, no confirmation
  when a host comes back from a match, server-assigned game ids. Their comments explain why.
- The server never tells the game that a patch is needed: every version check gets Result
  NO_PATCH_NEEDED, whatever version, edition or language it reports, and PATCH_INFORMATION (0x12)
  is never sent. This is the owner's rule; do not research or build a patch path.
- Every fact about the protocol cites where it comes from: an address in S3.EXE (GOG release,
  SHA-256 `00a925cf...804d950b`) or "seen in a run". Mark guesses as inferred. Update
  `docs/protocol.md` together with the code.
- A new message gets a class in `messages.py` with `encode` and `decode` and a round trip test in
  `tests/test_messages.py`. A size the client asserts goes into `SIZE_THE_CLIENT_ASSERTS` in
  `tests/support.py`.
- A new lobby behaviour gets a session test in `tests/test_session.py` whose docstring says what
  breaks in the game without it.
- Code style: descriptive names, docstrings and why-comments, early returns, no magic numbers, no
  compressed one-liners, four spaces. Never write an em dash or en dash anywhere (code, comments,
  docs, commit messages); use a plain hyphen.
- Never commit game files, the game's executable, or recordings that contain them.
- Network safety, learned from the security review (`a05c8b9`, `e91a88f`):
  - Anything answered before a logon is no bigger than the request: sender addresses can be
    forged, and a large answer would hit someone else.
  - Every value a client sends is bounded (sizes, counts, timeouts, rates); housekeeping steps
    are guarded so one failure cannot stop retries and timeouts for everyone.
  - Text from clients is relayed byte for byte, but passes `wire.readable_text` before it is
    stored or shown: undefined Windows-1252 bytes arrive as surrogates, which UTF-8 cannot encode.
  - A setting that makes the server send something no run with the real game has seen yet
    (`mark_admins`, `check_reachability`) is off by default until a run confirms it.
- Web interface: every POST form carries `csrf_field()`; every admin blueprint registers
  `redirect_visitors_to_logon` as its before_request hook. Pages run no inline script or style (the
  Content-Security-Policy forbids them); colours, sizes and spaces come from the UI kit's tokens in
  `static/tokens.css`, never as literals. Texts for the game are validated by the database
  (`check_game_text`), not by the views, so the command line and the web enforce the same rules.

## Checking against the real game

Logs from the server with `--verbose` show every datagram in hex; that is how behaviour was
confirmed. The game itself runs under Wine (needs `winetricks directplay` for opening games) or on
Windows, pointed at the server through the registry key described in `README.md`. When the client
drops a connection, the last block from the server it did not acknowledge in the log is the one
it rejected; find the handler for its message types in S3.EXE (dispatch table in
`docs/protocol.md`) to see which assert failed.

## Git

- No `Co-Authored-By` or other generated-by lines in commit messages.
- Merges always with `--no-ff`.
- Identity of this repository (stored in its local git config, the whole history was rewritten
  to it): DaRealHansForSure <stuff+s3@kognetwork.ch>. The project is MIT licensed under that name.
- Work on a feature branch, merge it into `main` with `--no-ff`, push to `origin` (gitea at
  192.168.1.8) when the owner asks. The `github` remote (DaRealHansForSure/s3lobby) signs in with
  its own key through `.git/ssh_config` (core.sshCommand); GitHub refused that key until the
  owner adds it to the account, and the key needs `ssh-add` for its passphrase.
- Run commands from the repository folder without a `cd` prefix, and stay inside this folder
  unless the owner says otherwise (the game's own installation folder, which `build/lobby/re.py`
  reads, is read only and only for looking up addresses).

## Continuing in a new session

- Local, ignored files: `HANDOVER.md` (state and next steps for the next agent), `build/lobby/`
  (reverse engineering helpers: `python3 -P build/lobby/re.py fnq ADDR`, `xref VALUE`, `str ADDR`;
  names in `names.json`), `build/wine/` (the game under Wine), `build/skills-ref/` (review
  references, see below).
- Skills applied to all code: the owner's global `senior-code-style` (naming, early returns, braces
  where the language has them, why-comments, no magic numbers, no compressed one-liners) and
  `no-em-dash`. `find-skills` was used to pick review references from skills.sh.
- Review references, read but not installed (third-party, untrusted; fetch again with curl from
  raw.githubusercontent.com into `build/skills-ref/`):
  - Python quality: `trailofbits/skills` plugins/modern-python, `affaan-m/ecc` .kiro/skills/python-patterns
  - Network security: `trailofbits/skills` plugins/sharp-edges, `openai/skills` skills/.curated/security-threat-model
  - Web security: `openai/skills` skills/.curated/security-best-practices with references/python-flask-web-server-security.md
- Checks before a commit: `.venv/bin/python -m pyflakes s3lobby tests`, `.venv/bin/python -m
  unittest`, the suite against MySQL 8.4 and MariaDB 11.4 (podman, see Commands) when the database
  changed, and `grep` for em and en dashes in what changed.
- UPnP can be tried against the owner's router (192.168.1.1): a mapping added in a test must be
  deleted again, and `GetSpecificPortMappingEntry` (error 714) confirms it is gone.

## Open review findings

Fixed so far: web hardening (open redirect, server-side web sessions, lockout, headers), the
housekeeping guard, block budget, acknowledgement and timeout caps, no Home answers before a logon,
logon deadline, readable text, the log budget, game logon limits and check budget, wrong group
password and group update limits, admin rights read at the command, update passwords hidden in
the log. Still to do, most important first:

- UPnP: control URL only on the answering device and plain http, no proxies or redirects, a
  global external address only, an overall discovery deadline; forward only private addresses
  from the game's ports; one lock around mapping changes (forward and release race today).
- `decode_block` should stop at the message limit instead of parsing a whole datagram first.
- The reachability probe only for confirmed connections.
- SIGTERM handler for a clean stop; cancel background router tasks before stopping forwarding;
  close what `start_server` opened when it fails halfway.
- Database: indexes on `lobby_sessions(ended_at)` and `games(closed_at)`, statistics cached for a
  few seconds, a short SQLite busy timeout, ROLLBACK when COMMIT fails, driver errors wrapped as
  `DatabaseError`, `is_matching_access_code` returning False for a malformed hash, settings saved
  in one transaction, `forget_client` updating memory and clients before the database.
- Input: `isdigit()` accepts characters `int()` refuses (use `isascii() and isdigit()`); the web
  settings checkbox must accept every true value (`1`, `yes`, `on`); access code labels limited and
  ban reasons checked with `check_game_text`; `/games` also catching `ValueError` from a report.
- Announcements queued while the server is down are marked sent at its start without reaching
  anyone: skip them or fix the text in the web interface.
- Typing and tidiness: optional return types (`create_account`, `from_stored_time`), transport
  typing, `RouterForwarding._run` with a TypeVar, duplicated constants (`FIRST_PRINTABLE_CHARACTER`,
  `MAX_BAN_HOURS`, `DEFAULT_BAN_HOURS`, true values, `HTTP_NOT_FOUND`), frozen dataclasses holding
  lists.
- Needs a recorded session before any change (it touches confirmed behaviour): dropping unnumbered
  lobby messages from logged on players and narrowing the block window (a forged datagram from an
  admin's address can run /ban today), ignoring a nonce reset from a logged on address, and a
  confirmation code for admin chat commands. MySQL over TLS (`ssl=` for remote hosts) is a choice
  for the owner.
