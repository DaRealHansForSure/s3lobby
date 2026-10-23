-- Chat themes for the games of one language: language holds a name of constants.GAME_LANGUAGES,
-- or is empty for every game.
--
-- Then the chat themes a lobby starts with, as the game's manual describes the ChatZone: a meeting
-- room to find players for a game, a room for a topic (beginners), and one room per language the
-- game ships in, named in that language and shown only to games of that language (Korean is left
-- out: the game's text is Windows-1252). Added once; admins may remove them like any theme, and a
-- name that exists already is left alone. General is not stored: the server always lists it.
ALTER TABLE chat_themes ADD COLUMN language VARCHAR(32) NOT NULL DEFAULT '';
INSERT INTO chat_themes (name, description, language, created_at)
SELECT 'Meeting Room', 'Meet here to find players for a game', '', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00')
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM chat_themes WHERE name = 'Meeting Room');
INSERT INTO chat_themes (name, description, language, created_at)
SELECT 'Beginners', 'Questions and first games for new players', '', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00')
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM chat_themes WHERE name = 'Beginners');
INSERT INTO chat_themes (name, description, language, created_at)
SELECT 'Deutsch', 'Für deutschsprachige Spieler', 'German', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00')
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM chat_themes WHERE name = 'Deutsch');
INSERT INTO chat_themes (name, description, language, created_at)
SELECT 'English', 'For English speaking players', 'English', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00')
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM chat_themes WHERE name = 'English');
INSERT INTO chat_themes (name, description, language, created_at)
SELECT 'Français', 'Pour les joueurs francophones', 'French', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00')
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM chat_themes WHERE name = 'Français');
INSERT INTO chat_themes (name, description, language, created_at)
SELECT 'Italiano', 'Per giocatori di lingua italiana', 'Italian', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00')
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM chat_themes WHERE name = 'Italiano');
INSERT INTO chat_themes (name, description, language, created_at)
SELECT 'Polski', 'Dla graczy z Polski', 'Polish', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00')
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM chat_themes WHERE name = 'Polski');
INSERT INTO chat_themes (name, description, language, created_at)
SELECT 'Español', 'Para jugadores hispanohablantes', 'Spanish', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00')
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM chat_themes WHERE name = 'Español');
