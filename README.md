<div align="center">

<img src="docs/images/logo.svg" alt="s3lobby logo" width="128" height="128">

# s3lobby

**Bring the online lobby of the original The Settlers III back to life.**

[![Release 0.10.0](https://img.shields.io/badge/release-0.10.0-1b3a6b?style=flat-square)](CHANGELOG.md)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-1b3a6b?style=flat-square&logo=python&logoColor=white)
![No dependencies](https://img.shields.io/badge/dependencies-none-17603f?style=flat-square)
[![MIT License](https://img.shields.io/badge/license-MIT-17603f?style=flat-square)](LICENSE)
![SQLite, MySQL, MariaDB](https://img.shields.io/badge/database-SQLite%20%7C%20MySQL%20%7C%20MariaDB-1b3a6b?style=flat-square)
![UDP 3344](https://img.shields.io/badge/protocol-UDP%203344-6d5a10?style=flat-square)
![The Settlers III](https://img.shields.io/badge/game-The%20Settlers%20III%20(1998)-dec470?style=flat-square)

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
  <img src="docs/images/live-lobby-light.png" alt="The live lobby page of the s3lobby web interface: five players online, one open game and a chat group, two players in green and one in blue as in the game's member lists">
</picture>

## Contents

- [Features](#features)
- [Status](#status)
- [Known issues](#known-issues)
- [Quick start](#quick-start)
- [Installation](#installation)
- [Running the server](#running-the-server)
- [Choosing a database](#choosing-a-database)
- [Accounts and lobby content](#accounts-and-lobby-content)
- [Web interface](#web-interface)
- [Screenshots](#screenshots)
- [Pointing the game at the server](#pointing-the-game-at-the-server)
- [Playing over the internet](#playing-over-the-internet)
- [Running the game on Linux with Wine](#running-the-game-on-linux-with-wine)
- [Security](#security)
- [How it works](#how-it-works)
- [Roadmap](#roadmap)
- [Development](#development)
- [Legal](#legal)

## Features

- **The original lobby, working again**: logon, the lobby rooms and game zones, chat, opening and
  joining games, chat groups with and without a password, clans, the Home tab with news,
  statistics and an advert banner, the ChatZone and the MessageZone board.
- **Clans and admins in colour**: the game's member lists draw marked clans and admins in green
  (the clan "BB" in blue, the colour the game kept for Blue Byte's staff).
- **Accounts made by the server's admins**: players log on with an access code instead of a
  self-chosen password; codes are stored as salted scrypt hashes only.
- **Server lines in the game's chat**: welcome message, announcements, and the chat commands
  `/who`, `/clan`, `/help` and, for admins, `/announce`.
- **Web interface**: a public live view of the lobby, and administration of accounts, clans, news,
  themes, the board, settings and announcements. Every game gets a page with its setup, teams,
  winners and time of play, decoded from the reports the game sends; every account a list of its
  games and results.
- **For testing**: a development mode that lets every account log on with `1234` from the LAN,
  and scripts that start and stop the server and the web interface together.
- **SQLite or MySQL**: a single file by default; MySQL or MariaDB with one switch, and a command
  that moves an existing lobby across.
- **Schema migrations** that upgrade the database in place on every start, with checksums that
  catch edited migration files.
- **Hardened for the open internet**: rate limits before parsing, bounded state per client,
  re-encoded texts, throttled logs.
- **Standard library only**: the server and the command line need nothing but Python. Flask and
  PyMySQL are optional extras.

## Status

> [!NOTE]
> **Tested on two systems only.** Everything below that says "tested" ran on this server machine:
> Ubuntu 26.04.1 LTS (Linux 7.0, GNOME on Wayland), Python 3.14, Wine 10.0 with Microsoft's
> DirectPlay (`winetricks directplay`), the GOG "Ultimate Collection" of The Settlers III in German
> (game version 1.60), the server and the game on the same machine, at most two copies of the game
> at once. One more machine in the same LAN ran the same game on Windows 10 Pro 64-bit (build
> 19045) against that server. The automated tests also ran against MySQL 8.4 and MariaDB 11.4 in
> containers. Not tried yet: other releases or languages of the game, a server on Windows or
> macOS, players on different networks, and a match between players on different machines.
> Reports from other systems are welcome.

Tested with the GOG "Ultimate Collection" (German, game version 1.60) under Wine 10 on Linux, and
from Windows 10 in the LAN where noted:

- patch check, lobby server list and logon with accounts made on the server
- the lobby rooms and the game zones, switching between them
- the ChatZone: chat themes, opening chat groups, chat between two players in one group
- opening a game (in a zone or from a chat group), changing its setup, listing it in a zone
  ("Warp"), another player joining it, playing the match, the match reports, coming back to the
  lobby (two copies of the game on one machine)
- clans shown in the member lists, and a clan's row coloured by the server
- a chat group with a password: opening it and its creator getting in
- the chat commands `/who` and `/net`
- server lines, and the web interface with every page
- from Windows 10: logon, a chat group, a game opened from it and listed in a zone, a match with
  its reports, coming back, and the MessageZone with a board message read

Implemented from the game's code, not yet tried with the real game: full games, another player
joining a chat group with its password, the Home tab's news and statistics, the banner, the other
chat commands, and kicks and bans of players who are online.

Not tested yet with players on two machines: the game's own DirectPlay connection between them.

## Known issues

- **A player who leaves too quickly stays listed for a minute.** Leaving the MessageZone or the
  lobby while the game still waits for the server's answer can close the game's side without a
  Logout (seen once in a run from Windows 10; *inferred*: a click during the answer). The server
  then sees a quiet connection and removes the player after 60 seconds; until then the others
  still see them. Logging on again from the same PC works once the old connection has been quiet
  for 20 seconds, so the game's retries may take a few seconds longer. A server cannot tell a game
  that left this way from one that is only slow, so it waits.
- **Chat typed on the MessageZone screen reaches nobody.** This is a bug of the game itself
  (`docs/protocol.md`, "The MessageZone tab"), which only a patched S3.EXE could fix. The board's
  messages work. The setting `board_chat_hint` tells players so once per logon.
- **Ladder games are played but not ranked.** See the [Roadmap](#roadmap).

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
| `--upnp` | off | let the router forward the server's port and one port per LAN player (UPnP) |
| `--dev` | off | development mode: every account also logs on with the password `1234`, from this machine and its LAN only, and several players may share a serial number; refused together with `--public-host` or `--upnp` |
| `--verbose` | off | log every datagram with its bytes, secrets removed; with `--log-file` only into the file, the console stays at INFO |
| `--log-file` | none | also write the log to this file |

To stop it, press Ctrl+C, or stop every running server and web interface at once:

```sh
scripts/stop.sh            # clean stop: players are told, UPnP forwardings removed
scripts/stop.sh --force    # also kill whatever still runs after 10 seconds
```

To start the server and the web interface together in the background, from the checkout:

```sh
scripts/start.sh           # datagrams in lobby.log, output in lobby-console.log and web-console.log
scripts/start.sh --dev     # both in development mode
```

## Choosing a database

By default everything lives in one SQLite file, `s3lobby.sqlite3`, readable by its owner only.
Nothing to set up, and plenty for a LAN or a community server.

To keep the lobby in **MySQL** (8.0.19 or newer) or **MariaDB** (10.5 or newer) instead, install
the `mysql` extra and give every command a `mysql://` location in place of the file name:

```sh
export S3LOBBY_DATABASE_PASSWORD='...'
s3lobby serve --database mysql://s3lobby@db.example.org:3306/s3lobby
```

For a database on another machine, add `?tls=verify` to the location to encrypt the connection
and check the server's certificate and host name, or `?tls=verify-ca&ca=/path/ca.pem` to check it
against your own authority only (for example the `ca.pem` MySQL creates for itself). Without the
option the driver may still encrypt, but never checks whom it talks to.

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
s3lobby clan highlight Romans    # members who show the clan get a green row in the game
s3lobby news set --audience german --title "Willkommen" --file news-de.txt
s3lobby news set --audience other --title "Welcome" --file news-en.txt
s3lobby chat-theme add "General" --description "Talk about anything"
s3lobby board add-theme English "Tournaments"
s3lobby board post 1 --title "Saturday cup" --text "8 pm, 1 vs 1"
s3lobby setting list
```

The Home tab shows the news for the game's language (German, or the other text for every other
language) and a statistics table of players and games. An advert banner can be shown too: the game
downloads it by anonymous FTP whenever the banner id or sub id changes, never for a new address
alone. The address is `ftp://HOST/PATH` without a port or a user (the game always connects
anonymously on port 21, in active mode), and the id must not be 0, which the game ignores.

```sh
s3lobby setting set banner_url ftp://192.168.1.10/banner.png
s3lobby setting set banner_id 2
```

A line for every player in the running lobby, and a welcome line after each logon:

```sh
s3lobby announce "Server restart at 22:00"
s3lobby setting set welcome_message "Welcome to our LAN lobby!"
s3lobby setting set mark_admins true    # admins' rows green in the game's member lists
s3lobby setting set board_chat_hint true  # tell players chat on the message board reaches nobody (a game bug)
```

In the game, players can type `/who`, `/clan` and `/help` into the chat. Admins also have
`/announce TEXT`, `/kick NAME [REASON]` and `/ban NAME HOURS [REASON]` (names may contain spaces);
each waits for `/confirm CODE` with a code the server shows only to the admin.
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
match reports: each game with the setup its host chose (type, map, teams, computer opponents,
player limit, Amazons, version), who joined it, when its match started and how it ended, and
every report decoded field by field, and a page per game with its teams and winners.

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
| `--dev` | off | development mode: every admin also logs on with the password `1234`, from this machine and its LAN only; refused together with `--behind-proxy` |

`S3LOBBY_WEB_SECRET` signs the session cookie; without it admins are logged out whenever the web
interface restarts. `s3lobby web` uses Flask's own server, which is fine on a LAN. To reach the
interface from the internet, put it behind a reverse proxy that adds HTTPS, and use both options
above, or run it under a WSGI server:

```sh
S3LOBBY_DATABASE=/srv/lobby.sqlite3 S3LOBBY_WEB_SECRET=... S3LOBBY_WEB_SECURE_COOKIES=1 \
S3LOBBY_WEB_BEHIND_PROXY=1 gunicorn "s3lobby.web.app:create_app_from_environment()"
```

## Screenshots

The screenshots show a demo lobby. Names in green and blue are the ones the game's member lists
draw in those colours.

<table>
  <tr>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/images/games-dark.png">
        <img src="docs/images/games-light.png" alt="The Games page: one line per game with host, type, map, players, opening time, result and how long it was open">
      </picture>
      <p align="center"><b>Games</b>: every game opened in the lobby, one line each.</p>
    </td>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/images/game-details-dark.png">
        <img src="docs/images/game-details-light.png" alt="A game's page: when it was opened, started, decided and closed, the winner, the teams with slot, race and lobby id, and the setup">
      </picture>
      <p align="center"><b>A game</b>: timeline, winners, teams, setup and reports.</p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/images/account-dark.png">
        <img src="docs/images/account-light.png" alt="An account's page: its access codes, the form for a new code, and the games it joined with team, race and result">
      </picture>
      <p align="center"><b>An account</b>: access codes and the games it played.</p>
    </td>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/images/clans-dark.png">
        <img src="docs/images/clans-light.png" alt="The Clans page: clans with their members, which ones the game lists in green or blue, and the buttons to change that">
      </picture>
      <p align="center"><b>Clans</b>: members, and the colour the game lists them in.</p>
    </td>
  </tr>
</table>

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

The lobby connects the players; chat and the match itself then run directly between their games.
So both the server and the players need to be reachable.

**The server.** Forward UDP port 3344 on the server's router to the server, and start it with
`--public-host` set to the router's public address or a (dynamic) DNS name. Or let the router do it:

```sh
s3lobby serve --upnp
```

With `--upnp` the server asks the router by UPnP to forward its port, takes the router's external
address as the public one (unless `--public-host` names another), and gives every player in its
own network an outside UDP port of their own (33430 and up). Internet players then reach every LAN
player, not just one. The forwardings are renewed while the server runs and removed when it stops.
UPnP must be switched on in the router.

**The players.** Chat goes directly between the games, to the addresses the server hands out. The
server makes those reachable: a player on the server's own machine is shown at the machine's LAN
address, and LAN players are shown to internet players at the public address (and their own
outside port with `--upnp`). Most home routers still drop chat from someone the player has not
written to yet; the game retries for a minute, so a conversation usually gets going once both
sides have written. A player who wants to receive chat from the first line on forwards the game's
port on their own router.

| Who | Forward to their PC |
|---|---|
| the server | UDP 3344 (or `--upnp`) |
| a player on the internet | UDP 3343, for chat from players they have not written to |
| whoever hosts a game | TCP 47624 and TCP and UDP 2300 to 2400 (DirectPlay, for the match) |

In the game, `/net` shows a player the address the server sees them at and whether their router
changed the game's port. With the setting `check_reachability` on, the server also tests after
each logon whether other players can write to the player first; the lobby page shows the result to
admins and `/net` to the player. That test is not yet tried with the real game, so it is off by
default.

Two players behind the same home router, playing on a server elsewhere, reach each other only if
that router can send traffic back into its own network ("hairpinning"); the game never tells the
server its local address, so the server cannot help there.

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

**The game sends its password and all chat in plain text.** s3lobby therefore issues random
access codes instead of letting players pick passwords; never reuse a real password with it. See
[SECURITY.md](SECURITY.md) for what that means for players, server owners and projects built on
this one, and for how to report a problem.

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
| `s3lobby/groups.py` | one group (room, game or chat group) and the rules about groups |
| `s3lobby/logon.py` | who may log on: accounts, access codes, bans, limits, development mode |
| `s3lobby/commands.py` | the chat commands (`/who`, `/clan`, `/announce`, `/kick`, `/ban`, ...) |
| `s3lobby/server.py` | the UDP socket and one connection per client |
| `s3lobby/addresses.py` | the address each player is shown to another, so chat and joins can reach it |
| `s3lobby/upnp.py`, `s3lobby/forwarding.py` | port forwarding on the router by UPnP (`--upnp`) |
| `s3lobby/reachability.py` | the optional test whether other players can reach a player directly |
| `s3lobby/database.py` | everything stored: accounts, clans, lobby content, statistics, settings |
| `s3lobby/backends/` | SQLite and MySQL behind one interface, chosen by the database location: `base.py`, `location.py`, `sqlite.py`, `mysql.py` |
| `s3lobby/migrations.py`, `s3lobby/schema/` | the schema as numbered migration files per database |
| `s3lobby/live.py` | the running lobby as the server writes it for the web interface |
| `s3lobby/cli.py`, `s3lobby/admin.py` | the command line |
| `s3lobby/web/` | the web interface (Flask) |

## Roadmap

Planned or wished for, roughly in order:

- **Tests with the real game** for everything marked as untried above, and with two players on
  two machines.
- **Ladder games ("Liga", "Clanliga") with a real ranking.** The game already reports each ladder
  match to the server with its players, teams, races and the winning team. s3lobby stores those
  reports but ranks nothing yet and answers "Game not valid for ladder." The plan: count the
  results per player and per clan, show the standings on the web interface and the Home tab, and
  confirm valid ladder results to the game (Result 0x6c) once the reports are trusted enough,
  for example only when every player of a match sent a matching report.
- **What a match tells the lobby** is all that a ranking can use: the game reports each match
  twice, at the start and when one team is left, with its slots, teams, races and the winning team
  (the only team that still has a finished military building). The match itself runs between the
  players over DirectPlay and never passes through the server.

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

## Legal

The Settlers is a trademark of Ubisoft. This project is not affiliated with or endorsed by Ubisoft
or Blue Byte. It contains no code or data from the game; the protocol was worked out by studying
the behaviour of a legally owned copy, for interoperability.

## License

s3lobby is released under the [MIT License](LICENSE). Copyright (c) 2026 DaRealHansForSure.

The protocol was reverse engineered from the original game and the code written from scratch by
DaRealHansForSure, together with Claude, an AI assistant made by Anthropic. You may use, change
and share it, also in your own projects, as long as you keep the copyright notice and name the
author. A link back to this repository is appreciated.
