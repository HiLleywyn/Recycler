-- Add dm_autolevelup column to user_prefs for auto level-up DM notifications.
ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_autolevelup BOOLEAN DEFAULT TRUE;
