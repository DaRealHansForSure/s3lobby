# s3lobby

A lobby server for the original **The Settlers III** (Blue Byte, 1998).

The game has a built-in online lobby: log on, chat, open a game, find other players. It talked to
Blue Byte's server `siedler3svr.bluebyte.de:3344`, which went offline long ago, so the lobby has
been dead for years. s3lobby speaks the same UDP protocol, read from the game's own code, so an
unmodified copy of the game can use its lobby again, on a LAN or over the internet.

## Status

Tested with the GOG "Ultimate Collection" (German, game version 1.60) under Wine 10 on Linux:

- patch check, lobby server list and logon with accounts made on the server
- the lobby rooms and the game zones, switching between them
- opening a game, playing it, and coming back to the lobby

Implemented from the game's code, not yet tried with the real game: joining another player's game,
games listed in several zones, full games, game settings updated during setup, chat groups with
passwords, clans.

Not tested yet with two real players on two machines: chat between players, joining another
player's game, and the game's own DirectPlay connection between them.

## Requirements

- Python 3.10 or newer, nothing else.
- An original copy of The Settlers III on every player's machine. s3lobby contains no game files.

## Running the server

From this folder:

```
python3 -m s3lobby serve
```

or install it and use the `s3lobby` command:

```
pip install .
s3lobby serve --name "Our LAN" --verbose --log-file lobby.log
```

| Option | Default | Meaning |
|---|---|---|
| `--database` | `s3lobby.sqlite3` | SQLite file with accounts and lobby content |
| `--host` | all interfaces | address to listen on |
| `--port` | 3344 | UDP port |
| `--name` | `LAN Lobby` | server name shown in the game |
| `--public-host` | none | host name or IP that players from the internet log on to |
| `--verbose` | off | log every datagram with its bytes, secrets removed |
| `--log-file` | none | also write the log to this file |

## Accounts and lobby content

Players log on with an account created on the server. The game's own "create player" button is
refused. Creating an account prints an access code, which the player types into the game's
password field:

```
s3lobby account add Alice
s3lobby account code Alice --label "laptop"     another code, e.g. for a second PC
s3lobby account revoke-codes Alice
s3lobby account ban Alice --hours 24 --reason "spam"
s3lobby account admin Alice                      may use the admin chat commands
```

Player names follow the game's own rule: letters A to Z and single spaces. The server stores only
a salted hash of each access code. A refused logon shows the game's own dialog text: unknown name
or wrong code, banned, server full, already logged on, or "no clan member" when the player ticked
the logon dialog's Clan box without being in a clan. Clan members who tick the box are shown with
their clan in the room member lists. After ten failed attempts from one IP or for one name, further
attempts are refused for a while.

Clans, the Home tab news, the ChatZone themes, the MessageZone board and the settings live in the
same database:

```
s3lobby clan add Romans
s3lobby clan join Romans Alice
s3lobby news set --audience german --title "Willkommen" --file news-de.txt
s3lobby news set --audience other --title "Welcome" --file news-en.txt
s3lobby chat-theme add "General" --description "Talk about anything"
s3lobby board add-theme English "Tournaments"
s3lobby board post 1 --title "Saturday cup" --text "8 pm, 1 vs 1"
s3lobby setting list
```

Every command takes `--database` and works while the server runs.

## Pointing the game at the server

The game reads its server list from the registry: every **value name** under the key
`Index Server` is one server address, written as `host:port`. The value's type and data do not
matter. On 64-bit Windows, save this as a `.reg` file, put in your server's IP, and run it as
administrator:

```
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\BlueByte\Siedler3\1.0\Index Server]
"192.168.1.10:3344"=""
```

Always write the port; the game has no default. When the game runs without administrator rights,
Windows may give it a private copy of that key under
`HKEY_CURRENT_USER\Software\Classes\VirtualStore\MACHINE\SOFTWARE\WOW6432Node\BlueByte\...`; if a
copy exists there, put the value there too.

Then start the game, choose the online (Internet) game and log on with any name and password.

## Playing over the internet

- Forward UDP port 3344 on the server's router to the server.
- Start the server with `--public-host` set to the router's public address or host name, so the
  game logs on to that address instead of the server's LAN address.
- The lobby only brings players together. The match itself runs over DirectPlay directly between
  the players, so whoever hosts a game needs DirectPlay's ports open as well: TCP 47624 and
  TCP and UDP 2300 to 2400.

## Running the game on Linux with Wine

The lobby works with Wine's own libraries, but opening a game needs Microsoft's DirectPlay, which
the game uses for the match itself:

```
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
  passwords before it writes a message. The database file is readable by its owner only.

All limits are constants in `s3lobby/constants.py`, each with the reason for its value.

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
| `s3lobby/cli.py` | the command line |

## Development

Run the tests from this folder:

```
python3 -m unittest
```

The session tests start a real server on a free port and play the game's side with fake clients
that follow the rules read from the game, including the message sizes it asserts.

## Legal

The Settlers is a trademark of Ubisoft. This project is not affiliated with or endorsed by Ubisoft
or Blue Byte. It contains no code or data from the game; the protocol was worked out by studying
the behaviour of a legally owned copy, for interoperability.

## License

Not chosen yet.
