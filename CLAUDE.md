# s3lobby

Lobby server for the online lobby of the original The Settlers III (Blue Byte, 1998). It replaces
Blue Byte's long dead `siedler3svr.bluebyte.de:3344` so unmodified copies of the game can use their
built-in lobby again. Open source project in a repository of its own. It began in `tools/s3lobby/`
of another repository; its history was carried over from there, so the early commits
describe that layout.

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
