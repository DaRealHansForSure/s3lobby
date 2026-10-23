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
| 0x1b | Home statistics: six u32 | S to C | 0x5d96b0 |
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
- Messages sent "unguaranteed" (flag of send 0x606ff0) travel in blocks without MessageId. The
  lobby never uses it: every call passes 0 for the flag (tested at 0x607069), including the two
  that pass it in a register, which is zeroed first (0x5c5ebe for the logout, 0x5ca80e in
  CreateGame). s3lobby therefore refuses lobby messages in blocks without MessageId: only someone
  sending from the player's address would send them.

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

A client accepts a connection from any peer: the lobby session's callbacks for an opened
connection only return true (0x5eec30, 0x5eec40), and a connection that closes for an address that
holds no group changes nothing (ConnectionClosed 0x5cc9fb..0x5cca39). s3lobby relies on that for
its optional reachability probe (setting `check_reachability`, off by default): from a second
port it sends `[MessageId 1][OpenConnection]` to a player from the internet. An answer means
the player's router lets in data from peers the player has not written to (*inferred* that the
game answers like any peer, PP.OpenIncoming 0x6045c0); the probe then closes with
`[MessageId 2][AcknowledgeMessages][MinMissingAcknowledge 2][CloseConnection]`.

### Timing

- Each poll of the lobby thread (after every datagram, or every 500 ms, 0x5c4564..0x5c4659) packs
  everything queued into one new block with the next id (FlushQueue 0x604ff0, called at 0x604a67
  before any timing check): there is no limit on blocks waiting for acknowledgement, and a
  message never joins a block already sent. Only sending is paced: at most one send pass per peer
  every 250 ms, in which every due block goes out as a datagram of its own, the first one carrying
  the acknowledgements (0x604b24..0x604d4a). s3lobby accepts client block ids within 32 of the
  oldest one missing; a client could only go further under heavy loss while it keeps sending, and
  a refused block is not acknowledged, so the client sends it again later.
- Blocks that only acknowledge carry MessageId with the client's last used id, not a new one
  (SendAckBlock 0x604ebb..0x604ecb).
- Retries of a block after 750, 1500, 3000, 5000, 5000, ... ms (table 0x65d498, 0x6017c0).
- After 5 s without hearing the peer the client stops resending data and only sends ack probes; at
  60 s it drops the peer (state 7). Any datagram resets that timer (table 0x65d47c).
- With nothing outstanding the client sends a keepalive every 5 s (0x604da4), during a match too
  (seen in a run). s3lobby uses that to tell a game that started over from a forged
  OpenConnection: a new nonce from the address of a logged on player is ignored while the old
  connection is not silent for 20 s (four keepalives).
- AdjustConnectionTimeout sets the sender's own timeout and tells the peer: the client sends
  600000 (10 minutes) when its match starts (UnlinkGameFromZones, 0x454d6f) and 60000 again when
  it leaves the game slots after the match (0x452072 in 0x451ee0).

### Closing

CloseConnection moves an open client (state 3) to 4 and tells its lobby layer, or a closing client
(5) to 6. In other states it is a data error, as are AcknowledgeMessages or CloseConnection from a
peer without a connection (0x608813).

A CloseConnection from the server ends the lobby session cleanly (PP.ReceivedCloseConnection
0x606050): an open client (state 3) checks it is in sync first (PP.CheckInSync 0x606310 via
InSync 0x606550), that is, that no earlier block of the *server's* is still missing on its side
(+0x170, ids received above the contiguous point, set by RegisterReceivedId 0x605a40), and throws
sync error 0x600 otherwise (0x6064be). Then it moves to state 4 and calls the session's
ConnectionClosed (0x5cc840) with both reasons 0. For the server's address
that logs out (0x5cc918) and posts status 6 with value 7 (0x5cc972). The lobby screen shows
status 6 as "The connection to the lobby server dropped." (0x44836a, text 0x900a7), a running
match as "Lobby connection lost!" (0x540d39, text 0x90146).

The rest of the state machine (state at +0x278; IsClosed 0x604980 is true for 0, 7 and 8):
- PP.Close (0x604790, through LP.Close 0x608ea0): state 3 queues CloseConnection and goes to 5,
  state 4 queues it and goes to 6; any other state asserts 'Invalid state'.
- ReceivedCloseConnection in state 5 goes to 6 without a check; in a state other than 3 or 5 it
  asserts '(state == iStateOpen) || (state == iStateCloseMe)'.
- State 6 ends (state 0, 0x604d55..0x604d76) once nothing of its own is unacknowledged (+0x94),
  no acknowledgement is owed (+0x18c), the peer's MinMissingAcknowledge equals the next id
  expected (+0x19c == +0x194 + 1), and an acknowledgement-only block arrived (+0x1a0, set by
  ReceivedBlock 0x605a27; 0x604a6c..0x604a8d). Until then it keeps sending acknowledgement blocks;
  after 60 s it times out (state 7).
- New messages are dropped silently outside states 1 to 4 (PP.Queue 0x6051a0).
- After the server closes, ConnectionClosed logs out (0x5cc918 to 0x5c5e80): the client sends one
  numbered block with Logout and CloseConnection (state 4 to 6).

s3lobby closes connections this way when an admin removes a player (a kick, a ban of a player who
is online, a deleted account) and when it stops. The reason for the player goes first; the
CloseConnection follows once the client has acknowledged every earlier block, so a lost reason
line cannot trip the sync check above (when the server stops it cannot wait and sends it at once).
It goes in a numbered block that acknowledges everything received so far, and lobby messages that
still arrive on the closing connection are ignored.

## Lobby layer: message bodies

Offsets count from the first byte after the header.

### Patch check (index server)

- **0x11 CheckFileVersion** (constructor 0x5e8380): u32 version (0xa0 = 160, global 0x6970f0),
  u32 edition ([0x7a7ff8]: 2 Mission CD, 3 Plus, 4 both), u32 language (config + 0x28, passed at
  0x43af70): the index into the languages of the game's text archive, 0 German, 1 English,
  2 Italian, 3 French, 4 Polish, 5 Spanish, 6 Korean, 7 Japanese. At start the game clamps it to the
  archive's language count (0x42e94b..0x42e97b). A German game sends 0 (seen in a run).
- **Answer**: Result (below) with code 9 means "no patch" (0x5cffe1): the handler marks the answer
  as received and takes the sender as server (0x5cff1c, 0x5cff27); the patch flag stays 0.
- **0x12 patch information** (0x5ccc50) is the alternative answer: u32 patch flag (0 = no patch),
  then string lists for the FTP patch. s3lobby never sends it and never says a patch is needed:
  every version check, whatever it reports, gets Result 9.

### Lobby server list

- **0x05 GetLobbyServerList** (0x5e16f0): empty body. Sent to the index server that answered.
- **0x06 answer** (parser 0x5e1710): pairs of strings to the end of the message, no count:
  `"host:port"`, then the server name. An empty list is client error 0x1e. Afterwards the client
  closes its connections and opens a new one to the lobby server for the logon.

### Logon

- **0x07 Logon** (constructor 0x5e1d50): u32 14 (a constant, `push 0xe` at 0x5e1d74; the client
  never sends another value and never compares it with anything from the server; *inferred*: a
  protocol version), string name,
  string password, u32 flags, string serial number. The client sends it to every listed server
  with the first server's name. Flags (built in CLobbyClient.Logon 0x5dd0b0..0x5dd0e4):

  | Bit | Meaning |
  |---|---|
  | 0x1 | "create player": the player used the logon dialog's create button (dialog flag +0xf0, set at 0x43a35c after the create dialog 0x43bf90) |
  | 0x2 | set when the game's edition setting is 1. This build never sets edition 1: it only writes 0, 2, 3 and 4 (0x40ee3b..0x40ef4a), so the bit is never sent |
  | 0x4 | the "Clan" box (registry ClanFlag, label 0x9013d "player is marked as clan member in the chat") |
- **0x09 Result**: u32 code, u32 value; size exactly 12 (0x5e1e30, 0x5e2080). The handler
  (0x5cd820) switches on the code (table 0x5d0618 / 0x5d05c0):

  | Code | Meaning, with the dialog text the game shows (text id 0x900nn = entry nn of the text archive) |
  |---|---|
  | 0x66 | logon accepted (LogonRetval 0, 0x5cde49), sender becomes the server |
  | 1, 5 | 0x9006a "An error occurred while connecting to the lobby." |
  | 2 | 0x9009f "There is already a player with this name. Please choose a different name." after the create button, else 0x90069 like code 3 (0x43bcfd..0x43bdc9) |
  | 3 | 0x90069 "The server refuses your combination of username and password." |
  | 6 | 0x9009d "Another player with this name or serial number is already logged in ..." |
  | 7 | 0x90017 "You entered an invalid serial number!" |
  | 0x67 | 0x9009e "The chosen player name is invalid. Only letters from A to Z ... and spaces" (German text: no two spaces in a row) |
  | 0x68 | 0x900ee "The maximum number of users for this server has been reached." |
  | 0x69 | 0x900f3 "On account of unacceptable behavior, you have been barred from the Lobby ..." |
  | 0x6d | 0x90139 "You're no clan member. ..." |
  | 9 | patch check: no patch |
  | 8 | patch check: failed |
  | 0x64, 0x65 | private chat accepted / refused (client to client, value = player id) |
  | 0xc | a group the player opened exists, value = its id: status 1/0 (0x5d052d). LM.PollStatus fills the lobby screen's empty slot with a CGroup of that id (0x5da8a0, 0x5db590): the channel LM+0x490 in the ChatZone (0x452577), the game LM+0x6e8 in the game setup (0x452a12, 0x452b53). An already filled slot makes the client leave the group instead (0x5dd520 -> 0x5ca5e0) |
  | 0xb | dialog "Der Wechsel in die Gruppe hat nicht geklappt" (status 2/5, 0x5d0557) |
  | 0x62 | dialog "Das Passwort stimmte nicht", value = group id (status 2/6, 0x5d0542) |

  The texts live in `GFX/siedler3_09.*.dat`: at offset 0x5c a u16 text count (374) and a u16
  language count (8), from 0x60 one u32 file offset per text, all texts of language 0 (German)
  first, then language 1 (English), and so on.

- **0x0e own player**: u32 player id, size exactly 8. Creates the client's own player record and
  asserts it does not exist yet: send it once. Its handler asserts `lpFrom == lpServer`, which only
  holds after Result 0x66, so the server sends `[Result 0x66][own player]` in that order, in one
  block. Joining a group dereferences the own player without a check (0x5ca46b): without 0x0e the
  client crashes there.
- **0x24 own player with clan** (handler 0x5d7830): u32 player id, u32 clan id, string clan name.
  The client accepts either 0x0e or 0x24 after the logon; send exactly one. It keeps the clan for
  the games it creates or joins and hands it to the other players over DirectPlay (0x470250,
  "CLAN-Pack"). The client never sends clan data itself, so clans have to come from the server.
- **0x0a Logout**: empty. The client closes without waiting for an answer.

The client checks nothing in the server's answers except the Result code: name, password and serial
can be accepted as they come.

### Groups (rooms and games)

- **0x0f AddGroupToGroup** (constructor 0x5e2d60): u32 parent, u32 group id, u32 flags, u32 maximum
  player count, three strings (name, description column, password), then optional parts by flag:
  - `0x400`: game block, 0x2c bytes: sockaddr_in of the host, the DirectPlay session GUID (16
    bytes), u32 game version (160, global 0x6970f0), then the checksum of the game's program as two
    u32, low half first (0x40b417, 0x61b6e0, a 64-bit value computed at start over the program).
    A client whose own checksum differs shows the game with its version and cannot join it
    (0x44b19d, 0x4436ac).
  - `0x40000`: description, u32 length then that many bytes: a serialized game description (below).
    A server forwards it unchanged.
  - `0x4000000`: the receiver asks for the group's images (GetImageList).

  Parent 0 tells the receiver it has joined that group (handler 0x5d2790); a parent that is a joined
  room makes the group visible inside it. Sent with group id 0 from another client, it is a private
  chat invitation.
- **0x20 AddGroupToGroup2** (constructor 0x5e3b40): as 0x0f, but the game block is 0x34 bytes:
  after the checksum come u32 Amazons (bit 0: the host allowed the Amazons, so the game needs the
  add-on; the game list marks it, 0x44b1e6..0x44b232) and u32 ladder type: 0 normal game,
  1 "Ladder: 1 on 1", 2 "Ladder Game: Clans", the host's choice in the game setup
  (0x4848b4..0x48493b, CreateGame 0x46180b..0x46181e), shown in the game list as "", "Liga" and
  "Clanliga" (0x44b843, 0x44b97a).
- **Game description** (CLMGameDescription, constructor 0x4504e0): a whole message of type 0,
  header included, written by CreateGame (0x4547cc) and again by UpdateGameSettings (0x454df4)
  whenever the host changes the setup: u32 number of teams (the setup's team list, settings +0x50),
  u32 number of computer opponents (slots in state 2, 0x435ca0), u32 1 for a ladder game, string
  map name (the map's name, or a backslash and the random map's name for a random map,
  0x4616da..0x461796). The game list shows the map and the team count from it (0x44b160,
  0x44b500). Seen in a run: 2 teams, 0 computer opponents, ladder 1, `\Random`.
- **0x19 UpdateGroup** (constructor 0x5ea320): u32 group id, u32 update mask, u32 flags, u32 maximum
  player count, three strings, the 0x2c game block (the short form: no Amazons value and no ladder
  type) and the description (u32 length, bytes), both always present. Mask (0x5dbf80): 1 name, 2 second text, 4 third text, 8 flags, 0x10 maximum,
  0x20 game block, 0x40 description. **0x21** is the same with the 0x34 game block.
- **0x10 RemoveGroupFromGroup**: u32 parent, u32 group id; size exactly 12.
- **0x1a group counters** (handler 0x5d8580): u32 group id, u32 players, u32 maximum, u32 flags
  (bit 0: full), for a group shown inside a group the client is in. The handler stores them on the
  listed group (CGroup.SetCounters 0x5dbf60, +0x78, +0x7c, +0x80) and ignores a group no joined
  group lists (0x5d89c6). Only bit 0 of the flags is ever read (CGroup.IsFull 0x5db520), by the
  game list (0x44ba9b), the chat group list (0x44745e) and the join refusals for a full game
  (0x4437c1, text 0x900a6) and a full chat group (0x442cf2, before the password prompt); the client
  never compares players with the maximum itself. The game list shows "players/max" (0x44b635).

**Games, from opening to the match.** Read from the code (0x4547f6, 0x45506c, 0x45637f, 0x454c20)
and partly seen in runs:

1. The host sends AddGroupToGroup2 with parent = the zone it is in (or a chat group), group id
   0xffffffff, flags 0x840401, the maximum player count (the number of human slots: slots in state
   1, 0x435ac0; seen in runs: 2 for 1 vs 1, 1 for Human vs Computer), the long game block and the
   game description. Chat groups are opened the same way (CreateGame 0x5ca7d0, called from
   0x5dd5b9), without a game block.
2. The server picks the id, sends the group back to the host with parent 0 and Result 0xc with
   the id, and shows it in the zone with its counters. The host's setup keeps its game in an
   empty slot (SwitchZone 5 with an empty CGroup, 0x454780) until that Result fills it
   (0x452b53); the setup updates (0x45506c), "show game in GameZone" (0x456370) and the match
   start (0x454c20) all name the game in that slot.
3. While setting up, the host sends UpdateGroup (mask 0x50: maximum and description). Receivers
   take an update only for a group they see in a parent, never for one they are in (0x5d8de6): the
   server keeps the new state and passes the update to the zone.
4. "Show game in GameZone", the setup's middle button "Warp" (text 0x90052, built at 0x48bb44):
   AddGroupToGroup with the game's id, flags 0 and empty texts, then an UpdateGroup clearing
   0x1000. The server lists the game in that zone (seen in a run: zones 30 and 32). The button is
   enabled (0x4552c0, polled at 0x48e2ac) in the game setup (zone 5) once the game slot LM+0x6e8
   holds the game (Result 0xc) with a game block (0x400), while the game is not yet listed
   (0x1000) or when it was opened from a chat group (LM+0x2de == 3). "Weiter" needs the same
   game slot (0x455280, polled at 0x48e273).
5. A joiner first connects DirectPlay to the host (0x443bc8), then leaves the GameZone groups and
   sends AddPlayerToGroup for the game (0x460c8b). It gets the game with parent 0 and every member.
6. At the match start the host sends RemoveGroupFromGroup(zone, game) for every zone the game is
   listed in, and AdjustConnectionTimeout 600000. The game leaves the zone lists; it goes on for its
   players.

**Tabs and zones.** The lobby screen has four tabs, each a group the client joins on its own (button
handler 0x4424a0, labels 0x90097..0x9009a): 1 Home, 2 ChatZone, 3 GameZone, 4 MessageZone. On the
GameZone tab the client also joins one game zone per set bit of the registry value
`GameZoneSelection` (bit i joins group 30 + i, 0x457030; GOG writes 511): 30 League 1 vs 1,
31 Free-for-all, 32 1 vs 1, 33 2 vs 2, 34 3-4 vs 3-4, 35 5+ vs 5+, 36 3 or more teams, 37 Human vs
Computer, 38 Other games (client texts set at 0x441313). Seen in a run: after logon one block leaves
group 1, joins group 3 and joins 30 to 38; switching tabs leaves the current tab and joins another.
There is no request for a list of groups: the client simply joins these ids.

Group names are shown only in chat theme and chat group lists (second text as "Description"), in
the game list, as "Channel: name" inside a chat group, and in the chat log; the MessageZone looks
for groups named `MessageThemes<Language>` (0x453831). Receivers never store the third text or the
maximum player count of an announcement (0x5d2c27..0x5d2c7f); the third text is the password when
a player creates a chat group (0x4546cb).

**Flags the client tests** on a received group:

| Flag | Meaning |
|---|---|
| 0x80 | the group receives the Home statistics 0x1b (0x5d997d) |
| 0x400 | game block present: a lobby game (0x4552a8) |
| 0x1000 | joining asks for a password (0x442d1a); also set on a game not yet shown in a zone (0x455319) |
| 0x40000 | description present |
| 0x4000000 | the client asks for the group's image list (0x5d3261) |

Games are opened with 0x840401, chat groups with 0x301073 (0x1000 included when a password is set).

**What a client sees must be mirrored by the server.** Three things drop the connection: an
announcement of a group the client already sees in the same parent (0x5db9ad), a
RemoveGroupFromGroup for a group it does not see in that parent, or while it is not in the parent
(0x5d5828, 0x5dbcc3), and a RemovePlayerFromGroup for a group it is not in (0x5d6af0). A server has
to know, per client, which groups it sees where.

**Leaving needs the server's confirmation.** `LeaveGroup` 0x5ca5e0 only sends RemovePlayerFromGroup;
the group stays on the client's joined list until the server sends RemovePlayerFromGroup with the
client's own player id back (handler 0x5d6af0 compares the player with `lpPlayer` at 0x5d721e and
removes the group at 0x5d7257). Without it, joining the same group again makes the client throw on
the AddGroupToGroup (assert `! vJoinedGroups.contains`) and drop the connection.

### Players

- **0x0c AddPlayerToGroup** (constructor 0x5e4e10): u32 group id, u32 player id, sockaddr_in,
  string name, string (never read by the client). Size above 28. The client sends it to join a
  group (0x5ca430); the server sends it for each member.
- **0x25** (handler 0x5d5f60): 0x0c's fields, then u32 clan id and string clan name (readers
  0x5e6550, 0x5e6740). The room member list shows the clan name in its second column (0x449d34).
  With clan id 0 and an empty name it behaves exactly like 0x0c (0x5d5ea5).
- **0x26** (handler 0x5d6520): 0x25 plus u32 player flags, right after the clan name (the flags
  getter 0x5e7900 skips the three strings and 31 bytes of fixed fields). Bit 0 draws the row's
  name and clan in green (0xff00, 0x449da1..0x449dbe); with the clan name exactly "BB" (text at
  0x69a568) they get 0xff3300 instead (0x449de2..0x449eb5; *inferred*: Blue Byte staff), which is
  blue: the game's colours are Windows colour values, blue byte first (seen in a run: a member of
  the highlighted clan "BB" listed in blue, the connection unharmed). No other bit and no other
  colour exists, so clans cannot have colours of their own. s3lobby sends bit 0 for admins when
  the setting `mark_admins` is on, and for players who show a clan an admin marked green
  (`s3lobby clan highlight`, the Clans page).
- **0x0d RemovePlayerFromGroup**: u32 group id, u32 player id; size exactly 12. Sent by the client
  to leave (0x5ca5e0).

The sockaddr in 0x0c matters: clients send room chat directly to each member's address (loop
0x5c661e), and the client's own idea of its address is 0.0.0.0 (it binds INADDR_ANY). The server
must fill in the address it sees datagrams come from.

That address is right only for clients in the same network. s3lobby rewrites it, and the host
address of a game block, for each receiving client (`s3lobby/addresses.py`): a player seen at
127.0.0.1 is shown at the server machine's address that faces the receiver, and a player at a
private address is shown to a receiver on the internet at the configured public host.

### Chat

- **0x1e ChatMessage2**: u32 group id (0 = private), u32 sender player id, u32 kind (0 normal,
  1 whisper), string text. Client to client.
- **0x0b**: u32 group id, u32 player id, string text (older form).
- **0x22 ChatCommand**: u32 group id, u32 0, string command; the client handles /MSG, /WHISPER,
  /IGNORE, /UNIGNORE and /CHATLOG itself (0x5c67a0) and sends every other slash command to the
  server (0x5c859c). **0x23** answers with u32 group id, u32 unused, u32 n, n strings; the handler
  (0x5d2420) only checks it comes from the server and turns each string into a chat line from
  player 0, which passes the chat filter for any group (0x453f56) and shows as `[Server]: text` in
  the chat room view (0x44a43c), or as an in-game message during a match (0x541042). The group id
  only names the group in the client's chat log file, so one 0x23 per client reaches it wherever it
  is: this is how a server makes announcements. 0x0b and 0x1e cannot be used for that: their
  handlers require a sender who is a member of a joined group and drop the connection otherwise
  (0x5d107e, 0x5d1e8e).

### The Home tab

- When a client joins Home (group 1) the server sends the statistics and the list of news text ids.
  Home is announced with flag 0x80, and with 0x4000000 when there is a banner.
- **0x1b statistics** (handler 0x5d96b0, table 0x448d20): six u32, no group id, stored on every
  joined group with flag 0x80: players online, players in the last hour, players in the last day,
  games open, games in the last hour, games in the last day (cells 0x448dfc..0x44921d). A first
  value of 0 shows "-".
- **0x14 text id list** (handler 0x5d7d60): u32 group id, u32 n, n u32; size exactly 12 + 4n; the
  group must be joined (0x5d81a6). On Home the client picks id 0x91 in a German game and 0x92 in
  any other, falling back to the list's first entry (0x45351b..0x453606), then sends:
- **0x16 GetTextMessage**: u32 text id. **0x15** answers u32 text id, string title, u32 n, n
  strings. The client takes it only for the id it waits for (0x5d8483), only with a non-empty title
  (0x5cb642; without one the page stays at "Loading message"), and keeps it for the session
  (0x60bbe0). It shows the title, an empty line, then the lines (0x4497ff). Without a list entry it
  shows "No message of the day present" (0x4497c3).
- **0x1d GetImageList**: u32 group id, sent for joined groups with flag 0x4000000 (0x5d3261).
  **0x1c** answers u32 group id, u32 n (n <= 0 is an empty list, 0x5ec063), n x (u32 banner id,
  u32 banner sub id, string url) (reader 0x5ec000). The client takes it only for a group it is in
  and drops the connection otherwise (assert 'index != -1', 0x5d5258..0x5d5296), so s3lobby
  answers only for a group the player is in. Only Home uses it (getter 0x5db560, called once at
  0x4468aa), and only the last entry (0x44c620):
  - banner id 0: nothing happens (0x449894);
  - otherwise the pair is compared with the registry values BannerId and BannerSubId (0x5ebfd0),
    never the url: a new picture needs a new id or sub id;
  - the new pair is stored on the lobby screen before the download ends (0x44998f) and kept in the
    configuration only on success (0x448960, 0x413ba0), with the picture as `Banner\Banner.png`.
    After a failure the finished download stays registered (0x448983), so that lobby screen fetches
    no banner again; the next lobby screen (constructor 0x43e800 clears it at 0x4400ab and reloads
    the configured pair at 0x441668) tries again (*inferred*: on the next entry into the lobby);
  - the url: the first six characters are skipped unchecked, the host runs to the first slash
    after them, the path from there (0x46501f..0x465110). The client connects anonymously on port
    21 (only Blue Byte's old host 193.158.243.3 got a special login, 0x4659fd..0x465a0f), with
    flags 0, so in active mode (PORT). s3lobby therefore requires `ftp://HOST/PATH` without port or
    user, and offers no banner while its id is 0.

### The ChatZone tab

The ChatZone (group 2) lists chat themes: groups whose name and second text fill the theme list's
name and "Description" columns (0x44711d, 0x447186). A player chooses a theme by joining it, and
sees the chat groups listed inside. A player opens a chat group with AddGroupToGroup2 under the
theme, group id 0xffffffff, flags 0x300073, maximum players 0xffffffff (no limit).

With a password it goes differently (CreateChatGroup 0x4542c0 compares the password with the empty
text at 0x454315): the client joins **group 5**, a group it builds itself with id 5 and empty
texts (0x454516, switched to at 0x452120), and opens the chat group under group 5 with flags
0x301073 (0x1000: password), the creator's name in brackets as second text ("(Bob) ", format
0x69a8ac) and the password as third text (0x4546cb); it leaves group 5 and the ChatZone in the same
block (seen in a run). Blue Byte's server must have had group 5; without it the join is answered
with Result 0xb ("Der Wechsel in die Gruppe hat nicht geklappt") and the chat group never opens
(seen in a run). s3lobby keeps group 5 hidden (listed nowhere) and lists such a chat group in the
chat theme its creator was in last as well, so the others find it there (*inferred*: how the
original server showed them is unknown). Joining one sends the password as
the second string of AddPlayerToGroup; a wrong one is answered with Result 0x62 ("Das Passwort
stimmte nicht", value = group id). Inside a chat group the client shows "Channel: name" (0x446b79).
The creator's lobby screen leaves the ChatZone with an empty channel (CreateChatGroup 0x4542c0
switches to zone 3 with an empty CGroup, 0x4520e0). Chat, whisper and "Erzeuge Spiel" go to the
channel's group id (LM.SendChat 0x454140 sends to LM+0x490), so the server answers the new group
with Result 0xc, value = its id, after the group (parent 0) and the creator's member entry; the
screen then takes it as its channel (0x452577). Without it the channel stayed without name and
members and nothing worked until the player left and joined the group again (seen in a run).

A player who opens a game inside a chat group ("Erzeuge Spiel") leaves the chat group in the
same block (seen in a run). Leaving zone 3 keeps the channel (0x451ee0), and back from the game
setup the client joins it again (SwitchZone 3 with an empty group joins the kept one, 0x451160).
When that group no longer exists the answer is Result 0xb and its dialog; under Wine the dialog
did not show over the full screen game and the screen stayed frozen (seen in a run). So s3lobby
keeps an empty chat group open while a game opened in it is open and two minutes after that
(*inferred*: what Blue Byte's server did is unknown).
Without any theme listed, players cannot open chat groups at all.

### The MessageZone tab

The MessageZone (group 4) is a message board. Inside it the client looks for a group named
`MessageThemes` followed by its language (German, English, Italian, French, Polish, Spanish,
Korean), falling back to `MessageThemesEnglish` (0x453831, 0x4539ce), and joins it. The groups
listed in that language group are the themes; a theme's text id list (0x14) holds its messages
(0x453bb8), which the client asks for one by one with 0x16 and shows with a header built from its
own texts "Message ... of ..." (0x9005c..0x9005e). Without a theme selected it shows "No message
theme selected" (0x90059), for an empty theme "No messages for this selection" (0x9005b).

A client bug, not reachable from the server: the screen's group lookup (0x452390) returns the
GameZone slot instead of the MessageZone's own group for level 0 (0x452455). That slot is always
empty there (leaving the GameZone clears it, 0x452039), so chat typed on the board screen is
silently not sent (0x443e4e), and a private chat invitation from there looks the invited player up
in the client's first joined group instead (0x44532b, 0x44d664). The board itself uses levels 1
and 2 (0x446cb0, 0x446d0a) and works (seen in a run on Windows 10: a theme's message fetched with
0x16 and shown, the join answered with 0x26 for an admin). Only patching S3.EXE would fix it (the `add ecx, 0x5ac` at
0x452459 would need 0x824, the MessageZone's own group); s3lobby works with unmodified games, so
it can only say so: with the setting `board_chat_hint` a player who opens the MessageZone hears
once per logon, as a server line, that chat there reaches nobody (off until a run confirms it).

### Other

- **0x1f GameStatus** (Logic.SendGameStatus 0x540500, constructor 0x5ec280): a report on the
  match. Every client of a lobby game sends one when the match starts (unconditionally, 0x53da69,
  right after "CLogicThread: Initialisierung abgeschlossen") and one when a single team is left
  (0x53ae66, once). Body: u32 group id (the lobby game), u32 flags (1 ladder game, game settings
  +0x2a; 2 not valid for the ladder: a ladder slot without lobby player id, 0x5405ec, or the lobby
  manager's mark +0x675), u32 winning team (0xffffffff while more than one team is left: a team
  counts while it still owns a finished military building, 0x5406e7..0x540768: the game's
  building records (count 0x1499e60, 0x3a bytes each, owner +0, type +6) of type 0x10 BigTower,
  0x12 Castle (the computer player's "Baue BigTower" and "Baue Castle", 0x57da96, 0x57db52) and
  0xf (*inferred*: the small tower) whose +0x21 is 0 (set 1 when placed, 0x4dd33f, 0 by 0x4dbc50;
  *inferred*: finished), counted for the owner's team when that player plays (+0x11943dc); one team
  with any is the winner, more than one or none leave 0xffffffff), u32
  game time (the game's tick counter, global 0x7dfd48: one tick per 64 ms, the logic thread's
  timer at 0x53d761, shown by the game's own clock as (ticks * 64) / 1000 seconds at 0x4b3fa2; it
  stands still while the game waits for its players), string map (the map file of the game
  settings, +0x2c via 0x436a60), u32 n, then n records of six u32 per slot in the match
  (0x5ec316..0x5ec505):

  | Field | Meaning |
  |---|---|
  | flags | 1 computer opponent (slot state 2), 2 slot state 3: a human who left the DirectPlay session after it was closed to new players (written only by PARAMS: DestroyPlayer 0x434b19 when "NET: OnDeletePlayer" 0x470980 reports it with the session closed, set at 0x471571; *inferred*: closed because the match started), 4 the race was "Random" in the setup and the race field holds the one drawn (setup record +0x64 is 0xff, 0x540625..0x54063e; seen in a run) |
  | lobby player id | the id the lobby gave the slot's player (0x1195d7c table) |
  | race | 0 Romans, 1 Egyptians, 2 Asians, 3 Amazons: the setup's texts for these values (0x9002d..0x9002f at 0x49053c..0x4907b1, 0x90155 at 0x4908e5); any other value it shows as "Random" (0x4909f0) |
  | unused | always 0 (0x54061d) |
  | slot | slot number, 0 to 19 |
  | team | the slot's team, numbered as the winning team |

  Slot states in the setup (names at 0x48fd26..0x490309): 0 free, 1 human, 2 computer opponent,
  4 closed. The game shows the answer only during a ladder game (0x540a18): Result 0x6c as "Game
  status stored at the Lobby server." (0x540a3e), 0x6a as "Game status inconsistencies. Game not
  valid for ladder." (0x540b89). For other games it ignores the answer.

  s3lobby stores every report as sent and ranks ladder games itself (s3lobby/ladder.py,
  docs/ladder.md). It answers 0x6c, except for a ladder game (flag 1): with the setting
  `ladder_enabled` off (the default until a run confirms the answer) every ladder report gets 0x6a,
  so the game tells the players that the result does not count, and every player of a ladder game
  gets one server line (ChatCommandAnswer to group 0) saying that ladder games are not ranked: the
  host when opening the game or when an UpdateGroup with a new description turns it into a ladder
  game, a joiner when joining. With it on, a ladder report that can count (two teams, no computer
  opponents, not marked invalid by the game) gets 0x6c, and the players hear the result once every
  report is in (*inferred* that the game shows 0x6c as intended: untried). The description decides, as the host rewrites it on every change of the setup;
  the game block keeps the choice made when the game was opened.

## Session sequence

| Step | Client | Server |
|---|---|---|
| 1 | block 1: OpenConnection, CheckFileVersion (to every index server) | block 1: OpenConnection, acks, Result 9 |
| 2 | GetLobbyServerList | lobby server list: `"ip:port"`, name |
| 3 | closes, opens a new connection: OpenConnection, Logon | OpenConnection, acks, Result 0x66, own player 0x0e |
| 4 | AddPlayerToGroup for its lobby room (1 to 3) and its zones (30 to 38) | AddGroupToGroup (parent 0), AddPlayerToGroup per member incl. the client, the games in the room |
| 5 | chat to the other members directly | tells members about arrivals (0x0c) and departures (0x0d) |
| 6 | AddGroupToGroup2 under the room (open a game) | the game with parent 0, the host's member entry and Result 0xc (its id) to the host; relays it to the room, host address filled in |
| 7 | game ends: AdjustConnectionTimeout 60000, joins its rooms again (never leaves the game) | takes the player out of its game without telling it (a RemovePlayerFromGroup for a game the client already dropped, as after "Zurück" in the game setup, hangs the game), closes it, answers the joins |
| 8 | Logout, CloseConnection | removes the player from its groups |

## Starting a game

The lobby only brokers. The host creates a DirectPlay session (0x4611e0), then announces it as a
group (0x20, flag 0x400) with its address and the session GUID (every recorded game had a new one).
A joining client reads both (group fields +0x30, +0x54, CGroup.GetSessionGuid 0x5db4c0) and
`JoinLobbyGame` 0x467e00 builds a `CTCPConnectionShortcut`
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

- Two real players on two machines: the game's own DirectPlay connection across a network, which
  needs the host address the server hands out to be reachable. Seen in a run on one machine (two
  copies under Wine, 2026-10-23): chat between the two players in one chat group, a second player
  joining a game opened in that chat group, the host starting the match, both reporting it (start
  and winner, the same slots), and both coming back to the lobby while the server closes the game.

Not answerable from the game: what Blue Byte's server named the zone groups 30..38 and the rooms
1..3. Only a recording of the original server would tell; s3lobby uses the names the client's own
texts give them (0x441313).
