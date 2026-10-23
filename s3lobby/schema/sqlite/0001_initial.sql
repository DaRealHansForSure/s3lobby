-- The first schema: accounts, access codes, clans, Home news, chat and board themes, the board,
-- lobby sessions and games for the statistics, match reports and settings.
-- Times are UTC text in ISO 8601 format, which compares correctly as text.
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_admin INTEGER NOT NULL DEFAULT 0,
    banned_until TEXT,
    ban_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_logon_at TEXT
);
CREATE TABLE access_codes (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX access_codes_by_account ON access_codes(account_id);
CREATE TABLE clans (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE clan_members (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL
);
CREATE TABLE news (
    audience TEXT PRIMARY KEY CHECK (audience IN ('german', 'other')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE chat_themes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE board_themes (
    id INTEGER PRIMARY KEY,
    language TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (language, name)
);
CREATE TABLE board_messages (
    id INTEGER PRIMARY KEY,
    theme_id INTEGER NOT NULL REFERENCES board_themes(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL
);
CREATE INDEX board_messages_by_theme ON board_messages(theme_id);
CREATE TABLE lobby_sessions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE games (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    host_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE game_reports (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    group_id INTEGER NOT NULL,
    report BLOB NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
