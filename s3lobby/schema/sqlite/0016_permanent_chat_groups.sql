-- Chat groups the server keeps open in a chat theme, ready to chat in without opening one first
-- (setting permanent_chat_groups). chat_theme_id NULL is the built-in theme General. Every theme
-- starts with one group named after it; admins add more or remove them.
CREATE TABLE permanent_chat_groups (
    id INTEGER PRIMARY KEY,
    chat_theme_id INTEGER REFERENCES chat_themes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO permanent_chat_groups (chat_theme_id, name, created_at)
SELECT NULL, 'General', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now');
INSERT INTO permanent_chat_groups (chat_theme_id, name, created_at)
SELECT id, name, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') FROM chat_themes ORDER BY id;
