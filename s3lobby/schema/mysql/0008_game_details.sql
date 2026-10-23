-- What a game was, for the Games page: its lobby group id, the game as the lobby last showed it
-- (an AddGroupToGroup2 body: game block and description included, password never), who joined
-- it, and which game each match report belongs to. Group ids start again at every server start,
-- so a report is linked to its game when it arrives, not later by group id.
ALTER TABLE games ADD COLUMN group_id INTEGER, ADD COLUMN announcement MEDIUMBLOB;
CREATE TABLE game_players (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    game_id INTEGER NOT NULL,
    account_id INTEGER,
    joined_at VARCHAR(32) NOT NULL,
    INDEX game_players_by_game (game_id),
    FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
ALTER TABLE game_reports
    ADD COLUMN game_id INTEGER,
    ADD INDEX game_reports_by_game (game_id),
    ADD FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE SET NULL;
