-- Logons to the web interface. The browser's cookie holds a random token; only its SHA-256 is kept
-- here, so the table alone logs nobody on. A logout ends the row, which a signed cookie alone
-- cannot do: a copied cookie stops working with it.
CREATE TABLE web_sessions (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    access_code_id INTEGER NOT NULL REFERENCES access_codes(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ended_at TEXT
);
