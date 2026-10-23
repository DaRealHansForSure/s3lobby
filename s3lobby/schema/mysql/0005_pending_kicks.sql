-- Players to remove from the running lobby, queued by another process (the command line, the web
-- interface) or by a ban or an account deletion, for the running server to carry out. The account
-- id has no foreign key: a deleted account must still be removed from the lobby.
CREATE TABLE pending_kicks (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    account_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    taken_at VARCHAR(32)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
