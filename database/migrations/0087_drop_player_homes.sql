-- Remove player homes feature: drop tables and home_channel column
DROP TABLE IF EXISTS home_unlocked_commands CASCADE;
DROP TABLE IF EXISTS home_commands CASCADE;
DROP TABLE IF EXISTS player_homes CASCADE;

ALTER TABLE guild_settings DROP COLUMN IF EXISTS home_channel;
