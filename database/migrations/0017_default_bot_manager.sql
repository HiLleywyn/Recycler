-- Migration 0017: Set default bot_manager_id to HiLleywyn (801280612111482890)
-- Backfill existing guilds that don't have a bot_manager set yet.

ALTER TABLE guild_settings
  ALTER COLUMN bot_manager_id SET DEFAULT 801280612111482890;

UPDATE guild_settings
  SET bot_manager_id = 801280612111482890
  WHERE bot_manager_id IS NULL;
