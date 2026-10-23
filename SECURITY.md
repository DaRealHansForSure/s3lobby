# Security

## The game sends its password in plain text

**Read this before you run a server, and before you build anything on this project.**

The Settlers III (1998) talks to its lobby over plain UDP, without any encryption. Its logon
message (`0x07 Logon`, see [docs/protocol.md](docs/protocol.md)) carries the player name and the
password as readable text, and every chat line travels the same way. Anyone who can see the
traffic between a player and the server can read them: someone on the same Wi-Fi, the network
of a LAN party, a provider, any machine along the route on the internet. Sender addresses on UDP
can also be forged. None of this can be fixed on the server side: the game is unmodified and
always sends this way.

s3lobby is built around that weakness:

- **Players never choose a password.** The server issues random access codes (12 characters),
  and the game's "create player" button is refused. A code that leaks gives access to this lobby
  and nothing else. An admin may switch the button on (setting `allow_create_player`); then a
  player chooses their own password (at least 8 characters, any case), which they may also use
  elsewhere and which crosses the network in plain text like every password of this game. Each
  address creates at most a few accounts an hour, and the game never hears that a name is taken:
  every refusal is the one for a wrong password.
- **Codes are stored only as salted scrypt hashes** and can be revoked one by one. Give every PC
  its own code (`s3lobby account code NAME --label ...`) and revoke the one that leaked.
- **Failed logons are limited** per address and per name, so codes cannot be guessed.
- **The web interface uses the same access codes.** Run it on a LAN only, or behind a reverse
  proxy with HTTPS (see the README), so codes do not cross the internet in plain text there too.

## For projects based on this one

If you build your own lobby, launcher or service on this code or on its protocol notes:

- **Never use a real password anywhere in this mechanism.** Do not let players type, choose or
  reuse a password they also use elsewhere (an e-mail, a forum, a game account, a Windows logon).
  Whatever the game sends as password is public to anyone on the path.
- **Never ask for or accept an existing account's password** through the game's logon dialog,
  and never forward the game's password to another service to "check" it.
- **Generate the codes yourself**, random and long enough, store only a slow salted hash, make
  them revocable, and limit failed attempts.
- **Treat everything the game sends as untrusted**: lengths, counts, texts and sender addresses.
  The game clients are from 1998 and break on unexpected answers; the server must never send more
  bytes to an address than it received before a logon, or it becomes a tool to flood others.

## Known limits

- Access codes and chat cross the network in plain text (above).
- On networks that allow forged sender addresses, someone who knows a logged on player's address
  and port can send datagrams that look like theirs, without seeing the answers. The server
  refuses lobby messages outside the game's numbered blocks (the game sends none, see
  docs/protocol.md), accepts block numbers only within 32 of the expected one, and ignores a new
  connection from the address while the player's game still sends its keepalives, so a forger can
  no longer end a session or act for a player with one blind datagram. The game's datagrams carry
  no secret, though: a forger who keeps guessing the next block number can still slip in an
  ordinary action (chat, joining, leaving). Admin chat commands stay protected by `/confirm`, a
  code the forger never sees.
- A connection to a MySQL server on another machine should use `?tls=verify` (or `verify-ca` with
  your own authority) in its location; without it, nobody checks whom the server talks to.
