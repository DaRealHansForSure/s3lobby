-- The running server's lobby as JSON (s3lobby/live.py), one row, for the web interface to show.
CREATE TABLE live_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot TEXT NOT NULL,
    written_at TEXT NOT NULL
);
