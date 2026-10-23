-- The MessageZone board in the order admins choose: themes and messages carry a position, lowest
-- first (equal positions keep the order they were added in), and may be hidden: a hidden theme or
-- message stays stored but is not sent to the game. The themes the server writes itself are only
-- hidden, never deleted, by admins.
ALTER TABLE board_themes ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE board_themes ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0;
ALTER TABLE board_messages ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE board_messages ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0;
