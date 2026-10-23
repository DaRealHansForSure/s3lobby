-- What a game was, for the Games page: its lobby group id, the game as the lobby last showed it
-- (an AddGroupToGroup2 body: game block and description included, password never), who joined
-- it, and which game each match report belongs to. Group ids start again at every server start,
-- so a report is linked to its game when it arrives, not later by group id.
ALTER TABLE games ADD COLUMN group_id INTEGER;
ALTER TABLE games ADD COLUMN announcement BLOB;
CREATE TABLE game_players (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    joined_at TEXT NOT NULL
);
CREATE INDEX game_players_by_game ON game_players(game_id);
ALTER TABLE game_reports ADD COLUMN game_id INTEGER REFERENCES games(id) ON DELETE SET NULL;
CREATE INDEX game_reports_by_game ON game_reports(game_id);
