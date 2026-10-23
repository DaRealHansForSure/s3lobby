-- Announcements written by another process (the command line, the web interface) for the
-- running server to send to every player.
CREATE TABLE pending_announcements (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
