-- The map a ladder match was played on, as its reports name it (a random map starts with a
-- backslash), for the statistics per map. Results stored before keep an empty name.
ALTER TABLE ladder_results ADD COLUMN map_name VARCHAR(255) NOT NULL DEFAULT '';
