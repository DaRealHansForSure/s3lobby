-- Players to remove from the running lobby, queued by another process (the command line, the web
-- interface) or by a ban or an account deletion, for the running server to carry out. The account
-- id has no foreign key: a deleted account must still be removed from the lobby.
CREATE TABLE pending_kicks (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    taken_at TEXT
);
