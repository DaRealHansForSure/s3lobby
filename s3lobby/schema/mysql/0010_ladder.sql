-- The ladder (docs/ladder.md). A game's players keep the clan they showed when joining it and since
-- when they were in that clan (Clanliga counts a player for a clan only after some days in it), and
-- a number for their game installation: players of one game whose games share a serial number get
-- the same number, so the ladder can refuse two copies of one installation without storing serials.
-- The ladder the host chose (LadderType 1 or 2; NULL for a normal game), as the game was last shown.
ALTER TABLE games ADD COLUMN ladder_type INTEGER, ADD INDEX games_by_ladder_type (ladder_type);
ALTER TABLE game_players
    ADD COLUMN clan_id INTEGER,
    ADD COLUMN clan_joined_at VARCHAR(32),
    ADD COLUMN installation INTEGER,
    ADD FOREIGN KEY (clan_id) REFERENCES clans(id) ON DELETE SET NULL;
-- One result per ladder game, as the server evaluated it or an admin decided it. Ratings are not
-- stored: they are computed from the counted results in order.
CREATE TABLE ladder_results (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    game_id INTEGER NOT NULL,
    ladder_type INTEGER NOT NULL,
    state VARCHAR(16) NOT NULL,
    reason VARCHAR(255) NOT NULL DEFAULT '',
    winning_team INTEGER,
    game_ticks INTEGER NOT NULL DEFAULT 0,
    played_at VARCHAR(32) NOT NULL,
    decided_at VARCHAR(32) NOT NULL,
    decided_by_account_id INTEGER,
    UNIQUE KEY ladder_results_by_game (game_id),
    INDEX ladder_results_by_time (played_at),
    CHECK (state IN ('counted', 'disputed', 'void')),
    FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
    FOREIGN KEY (decided_by_account_id) REFERENCES accounts(id) ON DELETE SET NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
-- account_id has no foreign key on purpose: a deleted account keeps its id here, so its opponents'
-- ratings are replayed exactly as they were played (the account only loses its name).
CREATE TABLE ladder_result_players (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    result_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    clan_id INTEGER,
    team INTEGER NOT NULL,
    race INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    has_won INTEGER NOT NULL DEFAULT 0,
    has_left INTEGER NOT NULL DEFAULT 0,
    INDEX ladder_result_players_by_result (result_id),
    INDEX ladder_result_players_by_account (account_id),
    FOREIGN KEY (result_id) REFERENCES ladder_results(id) ON DELETE CASCADE,
    FOREIGN KEY (clan_id) REFERENCES clans(id) ON DELETE SET NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
-- Custom seasons admins create; the infinite season is not a row, it is every counted result.
-- The name compares without case, like account and clan names.
CREATE TABLE ladder_seasons (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL,
    starts_at VARCHAR(32) NOT NULL,
    ends_at VARCHAR(32),
    counts VARCHAR(16) NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    created_by_account_id INTEGER,
    UNIQUE KEY ladder_seasons_by_name (name),
    CHECK (counts IN ('liga', 'clanliga', 'both')),
    FOREIGN KEY (created_by_account_id) REFERENCES accounts(id) ON DELETE SET NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
-- Texts the server wrote on its own (board messages, announcements), one row per event, so a restart
-- never writes one twice.
CREATE TABLE ladder_events (
    event_key VARCHAR(191) NOT NULL PRIMARY KEY,
    happened_at VARCHAR(32) NOT NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
