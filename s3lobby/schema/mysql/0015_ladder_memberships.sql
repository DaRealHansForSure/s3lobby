-- Players join the ladders they want to be ranked in: Liga, Clanliga and each custom season one
-- by one (docs/ladder.md). A membership runs from joined_at until left_at; a player who leaves and
-- joins again gets a new row and starts again at the start rating. A match counts in a ladder only
-- when every player in it was a member of that ladder when it was played.
CREATE TABLE ladder_memberships (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    account_id INTEGER NOT NULL,
    ladder VARCHAR(16) NOT NULL,
    season_id INTEGER,
    joined_at VARCHAR(32) NOT NULL,
    left_at VARCHAR(32),
    KEY ladder_memberships_by_account (account_id),
    CHECK (ladder IN ('liga', 'clanliga', 'season')),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (season_id) REFERENCES ladder_seasons(id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
-- A ladder game none of whose ladders had every player as a member: nothing is stored for it, and
-- this mark keeps the server from looking at it again.
ALTER TABLE games ADD COLUMN is_ladder_skipped INTEGER NOT NULL DEFAULT 0;
