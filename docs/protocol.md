# Settlers 3 online lobby protocol

The original Settlers 3 (GOG "Ultimate Collection") has an online lobby that talked to Blue Byte's
`siedler3svr.bluebyte.de:3344`, which is long gone. This document describes the protocol as read
from the game's code, so a replacement server can run on the LAN or on the internet. The server in
this project, [s3lobby](../README.md), implements it.

- **Binary**: `S3.EXE.orig` of the GOG "Ultimate Collection" (German), 3,842,048 bytes, SHA-256
  `00a925cf...804d950b`, image base 0x400000, menu version 1.60. All addresses below are virtual
  addresses in that file.
- **Source files** (from the asserts): `E:\Siedler3\Dirk\protocol.cpp` (connection layer),
  `Lobby.cpp` (messages, UDP socket), `lobbyclient.cpp` (client session and handlers).
- **Status**: read from code (REA with Hopper, capstone), then checked on 2026-10-23 with the GOG
  game under Wine 10 against this server: logon, rooms, opening a game, playing it and coming back
  work. Statements marked *inferred* follow from how
  the code uses a value, not from a direct read or a run.

## Pointing the game at a server

`Config.LoadRegistry` 0x40d700 opens `HKLM\Software\BlueByte\Siedler3\1.0\Index Server` and
enumerates the **value names** under it (0x40f3cf..0x40f400, RegEnumValueA with no data buffer).
Every name is one server address `host:port`; the value's type and data are never read. Only when
the key has no values does the game fall back to `siedler3svr.bluebyte.de:3344` (0x40f44a).

On 64-bit Windows the 32-bit game sees that key at:

```
HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\BlueByte\Siedler3\1.0\Index Server
    value name: 192.168.1.10:3344      (type REG_SZ, data empty)
```

As a `.reg` file:

```
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\BlueByte\Siedler3\1.0\Index Server]
"192.168.1.10:3344"=""
```

The game opens the key with `KEY_ALL_ACCESS` (0xf003f). Run without administrator rights, Windows
redirects that to the user's VirtualStore copy
(`HKCU\Software\Classes\VirtualStore\MACHINE\SOFTWARE\WOW6432Node\...`) if one exists. Set the
value in the copy the game actually reads.

The address is parsed by `CSocketAddress(host:port)` 0x5eca70: split at `:`, port via `atoi`, host
via `inet_addr` or, failing that, `gethostbyname`. There is no default port: always write it.
With several values the game asks all of them and keeps the first that answers (0x5c5418, 0x5cd2b6).

The client binds UDP port 3343 on all interfaces, or the next free port (0x5c4b55, 0x5c4bc8).

## Datagram and message format

One UDP datagram is one *block*: messages placed back to back, no extra framing (send 0x5cbad0,
receive 0x5c45e0, block parser 0x601810). A datagram is at most 0xffff bytes.

Every message starts with a 4-byte header (0x601e80, 0x6021a0, 0x6021b0):

| Offset | Type | Meaning |
|---|---|---|
| 0 | u8 | message type |
| 1 | u8 | 0 |
| 2 | u16 | size of the whole message, header included (at least 4) |

Body fields are written by a few helpers on the message class, little endian throughout:

| Helper | Address | Bytes |
|---|---|---|
| AppendU32 | 0x6038e0 | 4-byte unsigned |
| AppendString | 0x602830 | ANSI text plus the NUL byte |
| AppendBytes | 0x6027f0 | raw bytes (sockaddr, GUID) |

`sockaddr_in` is the Windows layout: family u16 (2), port u16 big endian, IPv4 address, 8 zero bytes.

Readers check sizes with asserts, and a failed assert throws: the client drops the connection
(state 8, "error"). So every size rule below is a hard rule for a server.

## Message types

The type is the argument of the base constructor 0x601e80 in each message class's constructor.
The receive dispatch 0x5eec50 maps `type - 4` through the jump table 0x5eeed4 to a handler slot;
the client's `CLobbySession` (vtable 0x65d01c) overrides the slots for messages a server sends.

| Type | Class / meaning | Direction | Client handler |
|---|---|---|---|
| 0x00 | AcknowledgeMessages | both | protocol |
| 0x01 | MinMissingAcknowledge | both | protocol |
| 0x02 | MessageId | both | protocol |
| 0x03 | CloseConnection | both | protocol |
| 0xfe | AdjustConnectionTimeout | both | ignored (0x45c380) |
| 0xff | OpenConnection | both | protocol |
| 0x05 | GetLobbyServerList | C to S | |
| 0x06 | lobby server list | S to C | 0x5cd310 |
| 0x07 | Logon | C to S | |
| 0x09 | Result | both | 0x5cd820 |
| 0x0a | Logout | C to S | |
| 0x0b | chat line (old) | S to C | 0x5d0800 |
| 0x0c | AddPlayerToGroup | both | 0x5d5990 |
| 0x0d | RemovePlayerFromGroup | both | 0x5d6af0 |
| 0x0e | own player | S to C | 0x5d7300 |
| 0x0f | AddGroupToGroup | both | 0x5d2790 |
| 0x10 | RemoveGroupFromGroup | both | 0x5d5360 |
| 0x11 | CheckFileVersion | C to S | |
| 0x12 | patch information | S to C | 0x5ccc50 |
| 0x14 | number list for a group | S to C | 0x5d7d60 |
| 0x15 | text message | S to C | 0x5d8270 |
| 0x16 | GetTextMessage | C to S | |
| 0x19 | UpdateGroup | both | 0x5d8a50 |
| 0x1a | three group counters | S to C | 0x5d8580 |
| 0x1b | 24-byte group record | S to C | 0x5d96b0 |
| 0x1c | image list | S to C | 0x5d4e50 |
| 0x1d | GetImageList | C to S | |
| 0x1e | ChatMessage2 | both | 0x5d1610 |
| 0x1f | GameStatus | C to S | |
| 0x20 | AddGroupToGroup2 | both | 0x5d3b50 |
| 0x21 | UpdateGroup2 | both | 0x5d9090 |
| 0x22 | ChatCommand | C to S | |
| 0x23 | chat command answer | S to C | 0x5d2420 |
| 0x24 | own player with clan | S to C | 0x5d7830 |
| 0x25, 0x26 | AddPlayerToGroup variants | S to C | 0x5d5f60, 0x5d6520 |

Types 4, 8, 0x13, 0x17 and 0x18 are neither built nor handled by this client.

## Connection layer

`CLobbyProtocol` (0x606ed0) keeps one `CPlayerProtocol` (vtable 0x65d564) per peer address.

### Messages

| Type | Size | Body |
|---|---|---|
| 0x00 AcknowledgeMessages | 4 + 4n | n x u32 block ids of the peer (n may be 0) (0x603580) |
| 0x01 MinMissingAcknowledge | 8 | u32: the lowest of the sender's own blocks not yet acknowledged; never 0 (0x603880, 0x6038f0) |
| 0x02 MessageId | 8 | u32 block id; only counts as the first message of a block (0x601d70) |
| 0x03 CloseConnection | 4 | none (0x603b40) |
| 0xfe AdjustConnectionTimeout | 8 | u32 timeout in ms (0x603bc0) |
| 0xff OpenConnection | 8 | u32 connection nonce, the client picks `rand() % 255 + 1` (0x603b60, 0x604404) |

### Numbering and acknowledgement

- Each side numbers its blocks per peer: 1, 2, 3, ..., one id per block, not per message. A
  numbered block starts with `MessageId(n)`. No wraparound (assert `msgid < UINT_MAX`, 0x605a69).
- The receiver acknowledges every numbered block, duplicates included, with AcknowledgeMessages.
  It keeps repeating an id until the sender answers MinMissingAcknowledge(v) with v above it
  ("I hold your acks for all my blocks below v", 0x605d90).
- Acknowledging an id the peer never sent is a data error (assert `vNonAckMessages.contains`,
  0x6051d0): the connection dies.
- Blocks that hold only acks carry `MessageId(last own id)`, which the receiver ignores.
- Delivery to the lobby layer is not in order: a new block is delivered when it arrives, a
  duplicate is acknowledged but not delivered again.
- Messages sent "unguaranteed" (flag of send 0x606ff0) travel in blocks without MessageId.

### Opening a connection

1. The client queues OpenConnection(nonce) (0x608a80, 0x604420) and the next send pass packs it
   with whatever else is queued into block 1: `[MessageId 1][OpenConnection nonce][...]`.
2. While opening (state 1) the client only retries block 1 and drops numbered blocks with another
   id (0x6059bb).
3. An acknowledgement of block 1 is not enough. The connection opens (state 3) only when the
   client receives the server's own OpenConnection (0x605eb3, 0x605ec8), and that must come in a
   block with id 1 (data error otherwise, "PROT: OpenConnection can only be delivered in message
   with id 1", 0x607f13).
4. So the server's first block is
   `[MessageId 1][OpenConnection S][AcknowledgeMessages 1][MinMissingAcknowledge 1][answers...]`.
   Keep S for the connection; a different S later means "peer restarted" and resets it (0x605eec).

### Timing

- At most one send pass per peer every 250 ms.
- Retries of a block after 750, 1500, 3000, 5000, 5000, ... ms (table 0x65d498, 0x6017c0).
- After 5 s without hearing the peer the client stops resending data and only sends ack probes; at
  60 s it drops the peer (state 7). Any datagram resets that timer (table 0x65d47c).
- With nothing outstanding the client sends a keepalive every 5 s (0x604da4).
- AdjustConnectionTimeout sets the sender's own timeout and tells the peer: the client sends
  600000 and 60000 (0x454d6f, 0x452072; *inferred*: 10 minutes around a running game).

### Closing

CloseConnection moves an open client (state 3) to 4 and tells its lobby layer, or a closing client
(5) to 6. In other states it is a data error, as are AcknowledgeMessages or CloseConnection from a
peer without a connection (0x608813).

## Lobby layer: message bodies

Offsets count from the first byte after the header.

### Patch check (index server)

- **0x11 CheckFileVersion** (constructor 0x5e8380): u32 version (0xa0 = 160, global 0x6970f0),
  u32 edition ([0x7a7ff8]: 2 Mission CD, 3 Plus, 4 both), u32 unknown (config + 0x28).
- **Answer**: Result (below) with code 9 means "no patch" (0x5cffe1): the handler marks the answer
  as received and takes the sender as server (0x5cff1c, 0x5cff27); the patch flag stays 0.
- **0x12 patch information** (0x5ccc50) is the alternative answer: u32 patch flag (0 = no patch),
  then string lists for the FTP patch. Every accessor asserts `size > 16`, so it is easy to get
  wrong; the server uses Result 9.

### Lobby server list

- **0x05 GetLobbyServerList** (0x5e16f0): empty body. Sent to the index server that answered.
- **0x06 answer** (parser 0x5e1710): pairs of strings to the end of the message, no count:
  `"host:port"`, then the server name. An empty list is client error 0x1e. Afterwards the client
  closes its connections and opens a new one to the lobby server for the logon.

### Logon

- **0x07 Logon** (constructor 0x5e1d50): u32 14 (*inferred*: protocol version), string name,
  string password, u32 flags (bit 0 and bit 2 from the dialog, bit 1 set for edition 1), string
  serial number. The client sends it to every listed server with the first server's name.
- **0x09 Result**: u32 code, u32 value; size exactly 12 (0x5e1e30, 0x5e2080). The handler
  (0x5cd820) switches on the code (table 0x5d0618 / 0x5d05c0):

  | Code | Meaning |
  |---|---|
  | 0x66 | logon accepted (LogonRetval 0, 0x5cde49), sender becomes the server |
  | 1, 2, 3, 5, 6, 7, 0x67, 0x68, 0x69, 0x6d | logon refused, each with its own dialog text |
  | 9 | patch check: no patch |
  | 8 | patch check: failed |
  | 0x64, 0x65 | private chat accepted / refused (client to client, value = player id) |

- **0x0e own player**: u32 player id, size exactly 8. Creates the client's own player record and
  asserts it does not exist yet: send it once. Its handler asserts `lpFrom == lpServer`, which only
  holds after Result 0x66, so the server sends `[Result 0x66][own player]` in that order, in one
  block. Joining a group dereferences the own player without a check (0x5ca46b): without 0x0e the
  client crashes there.
- **0x24 own player with clan**: u32 player id, u32 clan value, string name.
- **0x0a Logout**: empty. The client closes without waiting for an answer.

The client checks nothing in the server's answers except the Result code: name, password and serial
can be accepted as they come.

### Groups (rooms and games)

- **0x0f AddGroupToGroup** (constructor 0x5e2d60): u32 parent, u32 group id, u32 flags, u32 unknown,
  three strings (the first is the name), then optional parts by flag:
  - `0x400`: game block, 0x2c bytes: sockaddr_in of the host, 16 bytes (*inferred*: DirectPlay
    session GUID), 3 x u32.
  - `0x40000`: description, u32 length then that many bytes (the client stores a whole serialized
    game description message there; a server forwards it unchanged).
  - `0x4000000`: the receiver asks for the group's images (GetImageList).

  Parent 0 tells the receiver it has joined that group (handler 0x5d2790); a parent that is a joined
  room makes the group visible inside it. Sent with group id 0 from another client, it is a private
  chat invitation.
- **0x20 AddGroupToGroup2** (constructor 0x5e3b40): as 0x0f, game block 0x34 bytes (5 x u32).
  Opening a game sends this with flags 0x840401 (0x5ca7d0), parent = the zone it is opened in, and
  group id 0xffffffff: the server picks the id. Seen in a run, in the same block the host leaves
  all other zones and, after the game, its lobby room and zone. The host only counts itself in the
  game when the server sends the game back with parent 0.
- **0x19 UpdateGroup**: u32 group id, u32 update mask, u32 flags, u32 unknown, three strings, the
  0x2c game block and the description, both always present. Mask (0x5dbf80): 1 string 1,
  2 string 2, 8 flags, 0x10 unknown u32, 0x20 game block, 0x40 description. **0x21** is the same
  with the 0x34 game block.
- **0x10 RemoveGroupFromGroup**: u32 parent, u32 group id; size exactly 12.

Seen in a run: after logon the client sends one block that leaves group 1 (it was never in it),
joins group 3 and joins groups 30 to 38, one per set bit of the registry value `GameZoneSelection`
(GOG writes 511, nine bits). Switching lobby rooms in the screen leaves the current group and
joins 1, 2 or 3 (with the zones). A server has to accept a join for any group id; there is no
request that lists groups first.

**Leaving needs the server's confirmation.** `LeaveGroup` 0x5ca5e0 only sends RemovePlayerFromGroup;
the group stays on the client's joined list until the server sends RemovePlayerFromGroup with the
client's own player id back (handler 0x5d6af0 compares the player with `lpPlayer` at 0x5d721e and
removes the group at 0x5d7257). Without it, joining the same group again makes the client throw on
the AddGroupToGroup (assert `! vJoinedGroups.contains`) and drop the connection.

### Players

- **0x0c AddPlayerToGroup** (constructor 0x5e4e10): u32 group id, u32 player id, sockaddr_in,
  string name, string (never read by the client). Size above 28. The client sends it to join a
  group (0x5ca430); the server sends it for each member.
- **0x25 / 0x26**: 0x0c plus u32 and string (*inferred*: clan), 0x26 one more u32.
- **0x0d RemovePlayerFromGroup**: u32 group id, u32 player id; size exactly 12. Sent by the client
  to leave (0x5ca5e0).

The sockaddr in 0x0c matters: clients send room chat directly to each member's address (loop
0x5c661e), and the client's own idea of its address is 0.0.0.0 (it binds INADDR_ANY). The server
must fill in the address it sees datagrams come from.

### Chat

- **0x1e ChatMessage2**: u32 group id (0 = private), u32 sender player id, u32 kind (0 normal,
  1 whisper), string text. Client to client.
- **0x0b**: u32 group id, u32 player id, string text (older form).
- **0x22 ChatCommand**: u32 group id, u32 0, string command; unknown slash commands go to the
  server. **0x23** answers with u32 group id, u32 unused, u32 n, n strings, shown as `[Server]`.

### Other

- **0x16 GetTextMessage**: u32 text id. **0x15** answers u32 text id, string, u32 n, n strings.
- **0x1d GetImageList**: u32 group id. **0x1c** answers u32 group id, u32 n, n x (u32, u32, string).
- **0x14**: u32 group id, u32 n, n x u32; size exactly 12 + 4n.
- **0x1a**: u32 group id, 3 x u32. **0x1b**: 6 x u32. Meanings unknown.
- **0x1f GameStatus** (0x540500): u32 group id, 3 x u32 (flags with 1 = league game, winning team,
  ...), string, u32 n, n x 6 u32 per player. No answer expected.

## Session sequence

| Step | Client | Server |
|---|---|---|
| 1 | block 1: OpenConnection, CheckFileVersion (to every index server) | block 1: OpenConnection, acks, Result 9 |
| 2 | GetLobbyServerList | lobby server list: `"ip:port"`, name |
| 3 | closes, opens a new connection: OpenConnection, Logon | OpenConnection, acks, Result 0x66, own player 0x0e |
| 4 | AddPlayerToGroup for its lobby room (1 to 3) and its zones (30 to 38) | AddGroupToGroup (parent 0), AddPlayerToGroup per member incl. the client, the games in the room |
| 5 | chat to the other members directly | tells members about arrivals (0x0c) and departures (0x0d) |
| 6 | AddGroupToGroup2 under the room (open a game) | relays it to the room, host address filled in |
| 7 | game ends: AdjustConnectionTimeout 60000, joins its rooms again (never leaves the game) | takes the player out of its game without telling it (a RemovePlayerFromGroup for a game the client already dropped, as after "Zurück" in the game setup, hangs the game), closes it, answers the joins |
| 8 | Logout, CloseConnection | removes the player from its groups |

## Starting a game

The lobby only brokers. The host creates a DirectPlay session (0x4611e0), then announces it as a
group (0x20, flag 0x400) with its address and the session GUID (*inferred*). A joining client reads
both (group fields +0x30, +0x54) and `JoinLobbyGame` 0x467e00 builds a `CTCPConnectionShortcut`
"Internet connection to " + host IP (0x5bacb0), then opens the session by GUID over DirectPlay's
default ports. The match itself never passes through the lobby server. In game, each slot's lobby
id is the player id (0x53d438); a league game with lobby id 0 logs `LOBBY: Invalid lobby id in
liga game`.

## Running the game under Wine

Tested with Wine 10.0 (Ubuntu). The lobby itself needs only UDP, but opening a game needs a working
DirectPlay session first (the host creates it before it announces the game; without it nothing
reaches the lobby server). Wine's builtin DirectPlay did not do it; Microsoft's from
`winetricks directplay` did (Ubuntu keeps `wineserver` off the PATH: set
`WINESERVER=/usr/lib/x86_64-linux-gnu/wine/wineserver`).

GOG's `S3.EXE` reads the serial from `SingleNumber` (the GOG value is the key four times; the game
keeps the first quarter, 0x40fe99). A serial typed into the online dialog is saved XOR-encoded
(0x410086..0x4100cf) but GOG removed the decode, so the saved value is unusable and the game asks
on every start. Restoring GOG's value fixes it.

## Open questions

Each needs a run of the real game (start S3 with `-netmon`, which writes traffic to `trace.lis`,
and compare with the server's `--verbose` log):

- Two real players: chat between clients, joining another player's game, the game's own
  DirectPlay connection between two machines.
- What the real server named the zone groups 30..38 and rooms 1..3.
- The meaning of the unknown u32 fields, the Logon flag bits and the dialog texts of the refusal
  codes (texts 0x900xx live in the game's text files).
- Which registry view the game reads when it runs without administrator rights (WOW6432Node or
  VirtualStore).
