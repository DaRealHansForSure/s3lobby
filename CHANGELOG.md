# Changelog

Every notable change to s3lobby. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0.0 a minor
version may change behaviour; 1.0.0 comes once the lobby is confirmed with the real game on two
machines.

## [Unreleased]

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

[Unreleased]: https://github.com/DaRealHansForSure/s3lobby/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DaRealHansForSure/s3lobby/releases/tag/v0.1.0
