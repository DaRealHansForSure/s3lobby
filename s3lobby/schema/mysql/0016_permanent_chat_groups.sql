-- Chat groups the server keeps open in a chat theme, ready to chat in without opening one first
-- (setting permanent_chat_groups). chat_theme_id NULL is the built-in theme General. Every theme
-- starts with one group named after it; admins add more or remove them.
CREATE TABLE permanent_chat_groups (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    chat_theme_id INTEGER,
    name VARCHAR(64) NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    FOREIGN KEY (chat_theme_id) REFERENCES chat_themes(id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
INSERT INTO permanent_chat_groups (chat_theme_id, name, created_at)
SELECT NULL, 'General', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00') FROM DUAL;
INSERT INTO permanent_chat_groups (chat_theme_id, name, created_at)
SELECT id, name, DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00') FROM chat_themes ORDER BY id;
