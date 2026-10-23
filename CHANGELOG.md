# Changelog

Every notable change to s3lobby. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0.0 a minor
version may change behaviour; 1.0.0 comes once the lobby is confirmed with the real game on two
machines.

## [Unreleased]

## [0.6.0] - 2026-10-23

### Added

- Players of ladder games ("Liga", "Clanliga") are told these games are not ranked, and the game
  shows "Game not valid for ladder." for their match reports.
- The live lobby shows each game's map, team count and ladder flag.
- The Games page decodes match reports: map, outcome, players, teams and races.

### Changed

- The protocol notes are corrected, and their open questions trimmed.

## [0.5.0] - 2026-10-23

### Added

- MySQL and MariaDB as an alternative to SQLite (optional `mysql` extra), chosen by a
  `mysql://user@host/name` database location.
- Schema migrations as numbered SQL files per database, recorded with checksums; older SQLite
  files are upgraded in place.
- `s3lobby database status`, `migrate` and `copy --to`, to move a lobby between databases.
- GitHub Actions run the tests on SQLite, MySQL and MariaDB.
- The MIT License.

### Changed

- The README is the project's front page, with a logo, a screenshot and guides.
- The settings column is called `name` (migration 0004), as KEY is reserved in MySQL.

## [0.4.0] - 2026-10-23

### Added

- The game's language is read from its version check.
- Game blocks, game descriptions and match reports are decoded in full.

### Changed

- s3lobby lives in a repository of its own.

### Fixed

- The server never tells the game that a patch is needed, whatever version it reports.

## [0.3.0] - 2026-10-23

### Added

- A web interface (optional `web` extra, Flask): a public live lobby, and pages for admins to
  manage accounts, access codes, clans, news, themes, the board, settings and announcements.
- The running server writes its lobby to the database once a second for the web interface.
- Access codes are accepted in any case, and the code a logon matched is recorded.

### Changed

- Every setting is stored under its own key.

### Fixed

- Texts and settings the game cannot show are refused when they are stored.

## [0.2.0] - 2026-10-23

### Added

- A SQLite database for accounts with access codes, clans, news, chat themes, the message board,
  statistics and settings.
- Administration subcommands (`account`, `clan`, `news`, `chat-theme`, `board`, `setting`,
  `announce`) and `s3lobby serve`.
- Logon with accounts made on the server and random access codes; the game's own "create player"
  is refused. Clan members are shown with their clan.
- The Home tab with news, statistics and the advert banner, the chat themes on the ChatZone tab,
  and the message board on the MessageZone tab.
- Chat commands `/who`, `/clan`, `/help` and, for admins, `/announce`; server lines for every player.

### Fixed

- The server mirrors which groups each client sees, and handles games the way the client expects;
  the game dropped the connection otherwise.

### Security

- Rate limits, size checks and bounded state against hostile and broken datagrams.

## [0.1.0] - 2026-10-23

### Added

- A lobby server for the original The Settlers III that speaks the game's own UDP protocol, read
  from its code: the reliable connection layer, the version check, the lobby server list, the
  logon, the lobby tabs and game zones, opening games and handing the match to DirectPlay.
- The Python package `s3lobby` with `python -m s3lobby`, standard library only.
- A unittest suite with a background server and a fake game client that asserts the message sizes
  the game asserts.
- `docs/protocol.md`: the protocol with the address in the game's executable for every fact.
- README, packaging and CLAUDE.md.

[Unreleased]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/DaRealHansForSure/s3lobby/releases/tag/v0.1.0
