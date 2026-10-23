-- Announcements written by another process (the command line, the web interface) for the
-- running server to send to every player.
CREATE TABLE pending_announcements (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    text TEXT NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    sent_at VARCHAR(32)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
