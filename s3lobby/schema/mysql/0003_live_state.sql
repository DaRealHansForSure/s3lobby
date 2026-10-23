-- The running server's lobby as JSON (s3lobby/live.py), one row, for the web interface to show.
CREATE TABLE live_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot MEDIUMTEXT NOT NULL,
    written_at VARCHAR(32) NOT NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
