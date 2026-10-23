-- The ladder (docs/ladder.md). A game's players keep the clan they showed when joining it and since
-- when they were in that clan (Clanliga counts a player for a clan only after some days in it), and
-- a number for their game installation: players of one game whose games share a serial number get
-- the same number, so the ladder can refuse two copies of one installation without storing serials.
-- The ladder the host chose (LadderType 1 or 2; NULL for a normal game), as the game was last shown.
ALTER TABLE games ADD COLUMN ladder_type INTEGER;
CREATE INDEX games_by_ladder_type ON games(ladder_type);
ALTER TABLE game_players ADD COLUMN clan_id INTEGER REFERENCES clans(id) ON DELETE SET NULL;
ALTER TABLE game_players ADD COLUMN clan_joined_at TEXT;
ALTER TABLE game_players ADD COLUMN installation INTEGER;
-- One result per ladder game, as the server evaluated it or an admin decided it. Ratings are not
-- stored: they are computed from the counted results in order.
CREATE TABLE ladder_results (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL UNIQUE REFERENCES games(id) ON DELETE CASCADE,
    ladder_type INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('counted', 'disputed', 'void')),
    reason TEXT NOT NULL DEFAULT '',
    winning_team INTEGER,
    game_ticks INTEGER NOT NULL DEFAULT 0,
    played_at TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    decided_by_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL
);
CREATE INDEX ladder_results_by_time ON ladder_results(played_at);
-- account_id has no foreign key on purpose: a deleted account keeps its id here, so its opponents'
-- ratings are replayed exactly as they were played (the account only loses its name).
CREATE TABLE ladder_result_players (
    id INTEGER PRIMARY KEY,
    result_id INTEGER NOT NULL REFERENCES ladder_results(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL,
    clan_id INTEGER REFERENCES clans(id) ON DELETE SET NULL,
    team INTEGER NOT NULL,
    race INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    has_won INTEGER NOT NULL DEFAULT 0,
    has_left INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ladder_result_players_by_result ON ladder_result_players(result_id);
CREATE INDEX ladder_result_players_by_account ON ladder_result_players(account_id);
-- Custom seasons admins create; the infinite season is not a row, it is every counted result.
CREATE TABLE ladder_seasons (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    counts TEXT NOT NULL CHECK (counts IN ('liga', 'clanliga', 'both')),
    created_at TEXT NOT NULL,
    created_by_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL
);
-- Texts the server wrote on its own (board messages, announcements), one row per event, so a restart
-- never writes one twice.
CREATE TABLE ladder_events (
    event_key TEXT PRIMARY KEY,
    happened_at TEXT NOT NULL
);
