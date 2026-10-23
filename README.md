<div align="center">

<img src="docs/images/logo.svg" alt="s3lobby logo" width="128" height="128">

# s3lobby

**Bring the online lobby of the original The Settlers III back to life.**

[![Tests](https://github.com/DaRealHansForSure/s3lobby/actions/workflows/tests.yml/badge.svg)](https://github.com/DaRealHansForSure/s3lobby/actions/workflows/tests.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-1b3a6b?logo=python&logoColor=white)
![No dependencies](https://img.shields.io/badge/dependencies-none-17603f)
[![MIT License](https://img.shields.io/badge/license-MIT-17603f)](LICENSE)
![SQLite, MySQL, MariaDB](https://img.shields.io/badge/database-SQLite%20%7C%20MySQL%20%7C%20MariaDB-1b3a6b)
![UDP 3344](https://img.shields.io/badge/protocol-UDP%203344-6d5a10)
![The Settlers III](https://img.shields.io/badge/game-The%20Settlers%20III%20(1998)-dec470)

[Quick start](#quick-start) ·
[Database](#choosing-a-database) ·
[Web interface](#web-interface) ·
[Protocol](docs/protocol.md) ·
[Development](#development)

</div>

---

The Settlers III (Blue Byte, 1998) has a built-in online lobby: log on, chat, open a game, find
other players. It talked to Blue Byte's server `siedler3svr.bluebyte.de:3344`, which went offline
long ago, so the lobby has been dead for years.

**s3lobby** speaks the same UDP protocol, read from the game's own code, so an **unmodified copy of
the game** can use its lobby again, on a LAN or over the internet. No patch, no launcher, no game
files: point the game at your server and log on.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/live-lobby-dark.png">
  <img src="docs/images/live-lobby-light.png" alt="The live lobby page of the s3lobby web interface: three players online and one open game">
</picture>

## Contents

- [Features](#features)
- [Status](#status)
- [Quick start](#quick-start)
- [Installation](#installation)
- [Running the server](#running-the-server)
- [Choosing a database](#choosing-a-database)
- [Accounts and lobby content](#accounts-and-lobby-content)
- [Web interface](#web-interface)
- [Pointing the game at the server](#pointing-the-game-at-the-server)
- [Playing over the internet](#playing-over-the-internet)
- [Running the game on Linux with Wine](#running-the-game-on-linux-with-wine)
- [Security](#security)
- [How it works](#how-it-works)
- [Development](#development)
- [Legal](#legal)

## Features

- **The original lobby, working again**: logon, the lobby rooms and game zones, chat, opening and
  joining games, chat groups, clans, the Home tab with news and statistics, the ChatZone and the
  MessageZone board.
- **Accounts made by the server's admins**: players log on with an access code instead of a
  self-chosen password; codes are stored as salted scrypt hashes only.
- **Server lines in the game's chat**: welcome message, announcements, and the chat commands
  `/who`, `/clan`, `/help` and, for admins, `/announce`.
- **Web interface**: a public live view of the lobby, and administration of accounts, clans, news,
  themes, the board, settings and announcements, with past games and match reports.
- **SQLite or MySQL**: a single file by default; MySQL or MariaDB with one switch, and a command
  that moves an existing lobby across.
- **Schema migrations** that upgrade the database in place on every start, with checksums that
  catch edited migration files.
- **Hardened for the open internet**: rate limits before parsing, bounded state per client,
  re-encoded texts, throttled logs.
- **Standard library only**: the server and the command line need nothing but Python. Flask and
  PyMySQL are optional extras.

## Status

Tested with the GOG "Ultimate Collection" (German, game version 1.60) under Wine 10 on Linux:

- patch check, lobby server list and logon with accounts made on the server
- the lobby rooms and the game zones, switching between them
- opening a game, playing it, and coming back to the lobby

Implemented from the game's code, not yet tried with the real game: joining another player's game,
games listed in several zones, full games, game settings updated during setup, chat groups with
passwords, clans, the Home tab's news and statistics, the banner, the ChatZone, the MessageZone,
the chat commands, match reports and the web interface.

Not tested yet with two real players on two machines: chat between players, joining another
player's game, and the game's own DirectPlay connection between them.

## Quick start

```sh
git clone https://github.com/DaRealHansForSure/s3lobby.git
cd s3lobby
python3 -m s3lobby serve --name "Our LAN"        # the lobby server, on UDP port 3344
python3 -m s3lobby account add Alice              # in a second terminal: prints Alice's access code
```

Then [point the game at the server](#pointing-the-game-at-the-server), choose the online game in
the game's main menu and log on as Alice with the access code as password.

## Installation

Requirements:

- Python 3.10 or newer. Nothing else for the server and the command line.
- An original copy of The Settlers III on every player's machine. s3lobby contains no game files.

Run it straight from the checkout with `python3 -m s3lobby`, or install it into a virtual
environment to get the `s3lobby` command and the optional extras:

```sh
python3 -m venv .venv
.venv/bin/pip install '.[web]'          # with the web interface (Flask)
.venv/bin/pip install '.[web,mysql]'    # also with MySQL and MariaDB support (PyMySQL)
```

The command lands in `.venv/bin/s3lobby`. Run it from there, or activate the environment first
(`source .venv/bin/activate`) to type plain `s3lobby`. For working on the code, install with
`pip install -e` instead, so the command follows your changes.

## Running the server

```sh
s3lobby serve --name "Our LAN" --verbose --log-file lobby.log
```

| Option | Default | Meaning |
|---|---|---|
| `--database` | `s3lobby.sqlite3` | where accounts and lobby content live: a SQLite file or a `mysql://` location |
| `--host` | all interfaces | address to listen on |
| `--port` | 3344 | UDP port |
| `--name` | `LAN Lobby` | server name shown in the game |
| `--public-host` | none | host name or IP that players from the internet log on to |
| `--verbose` | off | log every datagram with its bytes, secrets removed |
| `--log-file` | none | also write the log to this file |

## Choosing a database

By default everything lives in one SQLite file, `s3lobby.sqlite3`, readable by its owner only.
Nothing to set up, and plenty for a LAN or a community server.

To keep the lobby in **MySQL** (8.0.19 or newer) or **MariaDB** (10.5 or newer) instead, install
the `mysql` extra and give every command a `mysql://` location in place of the file name:

```sh
export S3LOBBY_DATABASE_PASSWORD='...'
s3lobby serve --database mysql://s3lobby@db.example.org:3306/s3lobby
```

The password may also be part of the location (`mysql://user:password@host/name`, special
characters percent-encoded), but then it shows in process lists and shell history; the
environment variable keeps it out. Logs and error messages never show it. The database must exist
and the user needs the rights to create and alter tables in it:

```sql
CREATE DATABASE s3lobby CHARACTER SET utf8mb4;
CREATE USER 's3lobby'@'%' IDENTIFIED BY '...';
GRANT ALL PRIVILEGES ON s3lobby.* TO 's3lobby'@'%';
```

**Migrations.** The schema is a series of numbered SQL files in `s3lobby/schema/`, one folder per
database. Every start applies what the database is missing and records it in the table
`schema_migrations`. A database changed by a newer s3lobby, or a migration file edited after it
was applied, stops the start instead of breaking data. SQLite files from older s3lobby versions
are upgraded in place.

```sh
s3lobby database status       # applied and pending migrations, without changing anything
s3lobby database migrate      # apply the pending ones now, without starting the server
```

**Moving a lobby** from SQLite to MySQL, or back: stop the server, copy everything into the new,
empty database, then point every command at the new location. Ids are kept, so access codes,
clans and statistics stay intact.

```sh
s3lobby database copy --database s3lobby.sqlite3 --to mysql://s3lobby@db.example.org/s3lobby
```

## Accounts and lobby content

Players log on with an account created on the server. The game's own "create player" button is
refused. Creating an account prints an access code, which the player types into the game's
password field:

```sh
s3lobby account add Alice
s3lobby account code Alice --label "laptop"     # another code, e.g. for a second PC
s3lobby account revoke-codes Alice
s3lobby account ban Alice --hours 24 --reason "spam"   # online or not; an online player is removed
s3lobby account kick Alice --reason "spam"             # remove from the running lobby; may log on again
s3lobby account admin Alice                      # may use the admin chat commands
```

Player names follow the game's own rule: letters A to Z and single spaces. Neither the name nor the
access code is case sensitive: codes are made of capitals and digits only, so a player may type
them in any case without making them easier to guess. The server stores only a salted hash of each
access code. A refused logon shows the game's own dialog text: unknown name or wrong code, banned,
server full, already logged on, or "no clan member" when the player ticked the logon dialog's Clan
box without being in a clan. Clan members who tick the box are shown with their clan in the room
member lists. After ten failed attempts from one IP or for one name, further attempts are refused
for a while.

Clans, the Home tab news, the ChatZone themes, the MessageZone board and the settings live in the
same database:

```sh
s3lobby clan add Romans
s3lobby clan join Romans Alice
s3lobby news set --audience german --title "Willkommen" --file news-de.txt
s3lobby news set --audience other --title "Welcome" --file news-en.txt
s3lobby chat-theme add "General" --description "Talk about anything"
s3lobby board add-theme English "Tournaments"
s3lobby board post 1 --title "Saturday cup" --text "8 pm, 1 vs 1"
s3lobby setting list
```

The Home tab shows the news for the game's language (German, or the other text for every other
language) and a statistics table of players and games. An advert banner can be shown too: the game
downloads it by anonymous FTP whenever the banner id changes.

```sh
s3lobby setting set banner_url ftp://192.168.1.10/banner.png
s3lobby setting set banner_id 2
```

A line for every player in the running lobby, and a welcome line after each logon:

```sh
s3lobby announce "Server restart at 22:00"
s3lobby setting set welcome_message "Welcome to our LAN lobby!"
s3lobby setting set mark_admins true    # admins' rows in another colour; not yet tried with the game
```

In the game, players can type `/who`, `/clan` and `/help` into the chat. Admins also have
`/announce TEXT`, `/kick NAME [REASON]` and `/ban NAME HOURS [REASON]` (names may contain spaces).
Answers and announcements show as `[Server]` lines. Match reports the game sends after a game are
stored in the database.

A removed player (a kick, a ban, a deleted account) gets the reason as a server line and is
disconnected within a second; the game then says the connection to the lobby server dropped. A
kicked player may log on again, a banned one is refused until the ban ends. When the server stops,
every player is told at once instead of waiting a minute for a timeout.

Ladder games ("Liga", "Clanliga") can be opened and played, but they are not ranked: Blue Byte's
server kept the ladder, this one does not. Their players get a server line saying so, and during
the match the game shows "Game not valid for ladder." instead of a ranked result.

Every command takes `--database` and works while the server runs. The web interface below does
the same from a browser.

## Web interface

A small web application on the same database: a live view of the lobby for everyone, and pages for
admins to manage accounts and access codes, clans, news, chat themes, the message board, settings
and announcements, to remove players from the running lobby, and to look through past games and
match reports.

```sh
.venv/bin/pip install '.[web]'
export S3LOBBY_WEB_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
.venv/bin/s3lobby web --database s3lobby.sqlite3
```

Then open http://127.0.0.1:8080. The live lobby shows who is online, the open games and chat
groups and who is in which room, refreshed every few seconds; the running server writes that state
to the database once a second. Admins log on with their player name and one of their access codes,
the same pair as in the game; only accounts with admin rights get in. A new access code is shown
once, right after it is made.

| Option | Default | Meaning |
|---|---|---|
| `--database` | `s3lobby.sqlite3` | the server's database: a SQLite file or a `mysql://` location |
| `--host` | `127.0.0.1` | address to listen on |
| `--port` | 8080 | TCP port |
| `--secure-cookies` | off | send the session cookie over HTTPS only |
| `--behind-proxy` | off | take the client address from the reverse proxy's `X-Forwarded-For` |

`S3LOBBY_WEB_SECRET` signs the session cookie; without it admins are logged out whenever the web
interface restarts. `s3lobby web` uses Flask's own server, which is fine on a LAN. To reach the
interface from the internet, put it behind a reverse proxy that adds HTTPS, and use both options
above, or run it under a WSGI server:

```sh
S3LOBBY_DATABASE=/srv/lobby.sqlite3 S3LOBBY_WEB_SECRET=... S3LOBBY_WEB_SECURE_COOKIES=1 \
S3LOBBY_WEB_BEHIND_PROXY=1 gunicorn "s3lobby.web.app:create_app_from_environment()"
```

## Pointing the game at the server

The game reads its server list from the registry: every **value name** under the key
`Index Server` is one server address, written as `host:port`. The value's type and data do not
matter. On 64-bit Windows, save this as a `.reg` file, put in your server's IP, and run it as
administrator:

```reg
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\BlueByte\Siedler3\1.0\Index Server]
"192.168.1.10:3344"=""
```

Always write the port; the game has no default. When the game runs without administrator rights,
Windows may give it a private copy of that key under
`HKEY_CURRENT_USER\Software\Classes\VirtualStore\MACHINE\SOFTWARE\WOW6432Node\BlueByte\...`; if a
copy exists there, put the value there too.

Then start the game, choose the online (Internet) game and log on with an account's name and
access code.

## Playing over the internet

- Forward UDP port 3344 on the server's router to the server.
- Start the server with `--public-host` set to the router's public address or host name, so the
  game logs on to that address instead of the server's LAN address.
- The lobby only brings players together. The match itself runs over DirectPlay directly between
  the players, so whoever hosts a game needs DirectPlay's ports open as well: TCP 47624 and
  TCP and UDP 2300 to 2400.
- Chat also goes directly between the players, to the addresses the server hands out. The server
  gives every player addresses it can reach: a player on the server's own machine is shown at the
  machine's LAN address, and, with `--public-host` set, players in the server's LAN are shown to
  internet players at the public address. For an internet player to reach a LAN player there, the
  router must forward that player's UDP port (the game uses 3343, or the next free port) and, to
  host games, DirectPlay's ports to that player's machine. A public host name is looked up again
  every five minutes, so a dynamic DNS name works.

## Running the game on Linux with Wine

The lobby works with Wine's own libraries, but opening a game needs Microsoft's DirectPlay, which
the game uses for the match itself:

```sh
WINEPREFIX=/path/to/prefix winetricks directplay
```

On Ubuntu, `winetricks` cannot find `wineserver`; set
`WINESERVER=/usr/lib/x86_64-linux-gnu/wine/wineserver` first.

The GOG release stores the serial number as the registry value `SingleNumber` (the key four times
in a row). If the game asks for the serial every time you open the online lobby, a typed serial
has overwritten that value with something it cannot read back: restore the value GOG's
`regs.cmd` writes.

## Security

The server is reachable by anyone who can send it a UDP datagram, with any sender address, and the
original game clients it talks to were written in 1998. So:

- Every datagram is checked against a rate limit per sender before it is even parsed, and against
  size and message count limits after. Connections are limited in total and per IP, and a
  connection whose first answer is never acknowledged (a forged sender) ends after 15 seconds.
- The state kept per client is bounded: duplicate detection works within a window of block ids, and
  a client that stops acknowledging is dropped instead of queueing more for it.
- Whatever a client sends for other players to see (group names, descriptions) is decoded fully,
  checked against length limits and for control characters, and passed on re-encoded from the
  checked fields, never as the raw bytes received. Chat group passwords are never passed on.
- A malformed message is refused with a warning; warnings about one sender are throttled so they
  cannot flood the log. A bug triggered by a crafted message is logged with its traceback and the
  server keeps serving everyone else.
- Access codes are stored as hashes only. The `--verbose` log removes access codes and chat group
  passwords before it writes a message. The SQLite file is readable by its owner only, and a MySQL
  password never appears in logs.
- Texts stored for the game (news, themes, board messages, announcements, settings) are checked
  when they are stored, from the command line and the web interface alike: only characters the
  game's code page has, within the lengths the lobby passes on.
- The web interface: every form carries a per-session token against cross-site requests; pages
  send a strict Content-Security-Policy and load nothing from elsewhere; names from game clients
  are escaped. A session ends at the next click when its access code is revoked or its account
  loses admin rights or is banned. Failed web logons are limited like those from the game.

All limits are constants in `s3lobby/constants.py`, each with the reason for its value. Found a
security problem? Please report it privately to the maintainer rather than in a public issue.

## How it works

[docs/protocol.md](docs/protocol.md) describes the protocol: the datagram format, the reliable
layer the game builds on top of UDP, every message, and the order of a lobby session. Every
statement there names the address in the game's executable it was read from.

The code follows the same layers:

| Module | Responsibility |
|---|---|
| `s3lobby/constants.py` | the protocol's numbers: message types, result codes, flags, timing |
| `s3lobby/wire.py` | bytes: message header, blocks, field reader and writer |
| `s3lobby/messages.py` | one class per message, with `encode` and `decode` |
| `s3lobby/connection.py` | the reliable layer for one client: numbering, acknowledgements, retries |
| `s3lobby/guards.py` | rate limits and a throttled log against flooding senders |
| `s3lobby/lobby.py` | players, rooms, zones and games, and the answer to every request |
| `s3lobby/server.py` | the UDP socket and one connection per client |
| `s3lobby/addresses.py` | the address each player is shown to another, so chat and joins can reach it |
| `s3lobby/database.py` | everything stored: accounts, clans, lobby content, statistics, settings |
| `s3lobby/backends.py` | SQLite and MySQL behind one interface, chosen by the database location |
| `s3lobby/migrations.py`, `s3lobby/schema/` | the schema as numbered migration files per database |
| `s3lobby/live.py` | the running lobby as the server writes it for the web interface |
| `s3lobby/cli.py`, `s3lobby/admin.py` | the command line |
| `s3lobby/web/` | the web interface (Flask) |

## Development

Run the tests from the checkout:

```sh
python3 -m unittest                 # everything except the web tests
.venv/bin/python -m unittest        # with Flask installed: everything
```

The session tests start a real server on a free port and play the game's side with fake clients
that follow the rules read from the game, including the message sizes it asserts.

The database and session tests also run against MySQL or MariaDB. Point `S3LOBBY_TEST_DATABASE` at
a throwaway database (every table in it is dropped before each test), for example in a container:

```sh
podman run -d --rm --name s3lobby-mysql -p 127.0.0.1:3306:3306 \
    -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=s3lobby_test \
    -e MYSQL_USER=s3lobby -e MYSQL_PASSWORD=s3lobby docker.io/library/mysql:8.4
S3LOBBY_TEST_DATABASE=mysql://s3lobby:s3lobby@127.0.0.1:3306/s3lobby_test .venv/bin/python -m unittest
```

### Contributing

Issues and pull requests are welcome. A few rules keep the project honest about a protocol nobody
documented:

- Every fact about the protocol cites where it comes from: an address in the game's executable, or
  "seen in a run" with the real game. Guesses are marked as inferred, and `docs/protocol.md` is
  updated together with the code.
- Behaviour confirmed with the real game is not changed without new evidence from the game.
- A new message gets a class with `encode` and `decode` and a round trip test; a new lobby
  behaviour gets a session test that says what breaks in the game without it.
- A schema change adds the next migration number to every folder in `s3lobby/schema/`; released
  migration files are never edited.
- The server and the command line stay on the standard library.
- Never commit game files, the game's executable, or recordings that contain them.

## Legal

The Settlers is a trademark of Ubisoft. This project is not affiliated with or endorsed by Ubisoft
or Blue Byte. It contains no code or data from the game; the protocol was worked out by studying
the behaviour of a legally owned copy, for interoperability.

## License

s3lobby is released under the [MIT License](LICENSE). Copyright (c) 2026 DaRealHansForSure.

The protocol was reverse engineered from the original game and the code written from scratch by
DaRealHansForSure. You may use, change and share it, also in your own projects, as long as you
keep the copyright notice and name the author. A link back to this repository is appreciated.
