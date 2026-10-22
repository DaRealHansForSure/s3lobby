# s3lobby

Lobby server for the online lobby of the original The Settlers III (Blue Byte, 1998). It replaces
Blue Byte's long dead `siedler3svr.bluebyte.de:3344` so unmodified copies of the game can use their
built-in lobby again. Open source project; for now it lives in `tools/s3lobby/` of another
repository and is meant to move into a repository of its own, so nothing here may depend on files
outside this folder.

## Layout

- `s3lobby/constants.py`: every number of the protocol (MessageType, ResultCode, GroupFlag, sizes,
  timing). New numbers go here, never as literals in other modules.
- `s3lobby/wire.py`: bytes only: message header, blocks, FieldReader/FieldWriter, sockaddr_in.
- `s3lobby/messages.py`: one frozen dataclass per message with `encode()` and `decode()`.
- `s3lobby/connection.py`: the reliable layer for one client. No network access, testable with a
  fake clock.
- `s3lobby/lobby.py`: players, rooms, zones, games; one handler per lobby message type.
- `s3lobby/database.py`: the SQLite file: accounts with access codes, clans, news, chat themes,
  board, sessions, games, game reports, settings. Schema versioned with PRAGMA user_version; a
  schema change adds an entry to SCHEMA_UPGRADES.
- `s3lobby/server.py`, `s3lobby/cli.py`, `s3lobby/admin.py`: asyncio UDP server, command line,
  administration subcommands.
- `docs/protocol.md`: the protocol as read from the game, with the S3.EXE address of every fact.
- `tests/`: unittest. `support.py` has a background server and a fake game client.

## Commands

Run from this folder:

- Tests: `python3 -m unittest`
- Server: `python3 -m s3lobby serve --verbose --log-file lobby.log`
- Administration: `python3 -m s3lobby account add NAME` and the other subcommands (`--help`)

Python 3.10 or newer, standard library only. Do not add dependencies.

## Rules

- Behaviour that a run with the real game confirmed is not changed without new evidence from the
  game (its code or a recorded session). Several odd looking rules exist only because the client
  breaks without them: the order Result before OwnPlayer, the leave confirmation, no confirmation
  when a host comes back from a match, server-assigned game ids. Their comments explain why.
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
