-- KEY is a reserved word in MySQL: the settings column is called name in every dialect.
ALTER TABLE settings RENAME COLUMN `key` TO name;
