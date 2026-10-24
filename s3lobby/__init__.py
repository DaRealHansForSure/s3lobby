"""s3lobby: a lobby server for the original The Settlers III (Blue Byte, 1998).

The game's built-in online lobby talked to Blue Byte's server siedler3svr.bluebyte.de:3344, which
is long gone. This package speaks the same UDP protocol, read from the game's own code, so the
original game can use its lobby again: on a LAN, or over the internet. docs/protocol.md describes
the protocol.
"""

__version__ = "0.12.0"
