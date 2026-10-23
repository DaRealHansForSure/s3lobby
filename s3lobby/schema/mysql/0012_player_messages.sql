-- An announcement may be meant for one player only (a server line from an admin to that player);
-- account_id is NULL for every player. reached_count is how many players the server sent it to:
-- 0 for a player who was not online when it was taken. Older rows keep NULL there.
ALTER TABLE pending_announcements
    ADD COLUMN account_id INTEGER,
    ADD COLUMN reached_count INTEGER,
    ADD FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE;
