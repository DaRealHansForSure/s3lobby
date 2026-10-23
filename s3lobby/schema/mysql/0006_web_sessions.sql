-- Logons to the web interface. The browser's cookie holds a random token; only its SHA-256 is kept
-- here, so the table alone logs nobody on. A logout ends the row, which a signed cookie alone
-- cannot do: a copied cookie stops working with it.
CREATE TABLE web_sessions (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    account_id INTEGER NOT NULL,
    access_code_id INTEGER NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    last_seen_at VARCHAR(32) NOT NULL,
    ended_at VARCHAR(32),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (access_code_id) REFERENCES access_codes(id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_bin;
