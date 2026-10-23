-- A clan whose members the game lists with a green row: the server sends them as 0x26 with player
-- flag bit 0 (the member list colours that row, 0x449da1). Off for every clan until an admin
-- switches it on.
ALTER TABLE clans ADD COLUMN is_highlighted INTEGER NOT NULL DEFAULT 0;
