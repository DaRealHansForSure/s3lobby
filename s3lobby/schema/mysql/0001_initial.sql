-- The first schema, the same tables as sqlite/0001_initial.sql in MySQL's words.
-- Times are UTC text in ISO 8601 format, as in SQLite, so both compare them the same way.
-- Text compares byte by byte (utf8mb4_bin) as in SQLite, except the account and clan names, which
-- SQLite compares with NOCASE: those use utf8mb4_general_ci, which also ignores accents.
-- Columns with a unique key or an index are VARCHAR, since MySQL cannot index unbounded TEXT.
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL UNIQUE,
    is_admin INTEGER NOT NULL DEFAULT 0,
    banned_until VARCHAR(32),
    ban_reason TEXT NOT NULL DEFAULT (''),
    created_at VARCHAR(32) NOT NULL,
    last_logon_at VARCHAR(32)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE TABLE access_codes (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    account_id INTEGER NOT NULL,
    code_hash VARCHAR(255) NOT NULL,
    label TEXT NOT NULL DEFAULT (''),
    created_at VARCHAR(32) NOT NULL,
    last_used_at VARCHAR(32),
    revoked_at VARCHAR(32),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE INDEX access_codes_by_account ON access_codes(account_id);
CREATE TABLE clans (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL UNIQUE,
    created_at VARCHAR(32) NOT NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE TABLE clan_members (
    account_id INTEGER PRIMARY KEY,
    clan_id INTEGER NOT NULL,
    joined_at VARCHAR(32) NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (clan_id) REFERENCES clans(id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE TABLE news (
    audience VARCHAR(16) PRIMARY KEY CHECK (audience IN ('german', 'other')),
    title VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    updated_at VARCHAR(32) NOT NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE TABLE chat_themes (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL UNIQUE,
    description VARCHAR(255) NOT NULL DEFAULT '',
    created_at VARCHAR(32) NOT NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE TABLE board_themes (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    language VARCHAR(32) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description VARCHAR(255) NOT NULL DEFAULT '',
    created_at VARCHAR(32) NOT NULL,
    UNIQUE (language, name)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE TABLE board_messages (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    theme_id INTEGER NOT NULL,
    title VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    author VARCHAR(255) NOT NULL DEFAULT '',
    posted_at VARCHAR(32) NOT NULL,
    FOREIGN KEY (theme_id) REFERENCES board_themes(id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE INDEX board_messages_by_theme ON board_messages(theme_id);
CREATE TABLE lobby_sessions (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    account_id INTEGER NOT NULL,
    started_at VARCHAR(32) NOT NULL,
    ended_at VARCHAR(32),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL,
    host_account_id INTEGER,
    opened_at VARCHAR(32) NOT NULL,
    closed_at VARCHAR(32),
    FOREIGN KEY (host_account_id) REFERENCES accounts(id) ON DELETE SET NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE TABLE game_reports (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    account_id INTEGER,
    group_id INTEGER NOT NULL,
    report MEDIUMBLOB NOT NULL,
    received_at VARCHAR(32) NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
CREATE TABLE settings (
    `key` VARCHAR(255) PRIMARY KEY,
    value TEXT NOT NULL
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
