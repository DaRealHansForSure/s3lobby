-- The Home tab's statistics count open and recently closed sessions and games on every logon,
-- logout and game change; these tables only grow, so the counts need indexes.
CREATE INDEX lobby_sessions_by_end ON lobby_sessions(ended_at);
CREATE INDEX games_by_close ON games(closed_at);
