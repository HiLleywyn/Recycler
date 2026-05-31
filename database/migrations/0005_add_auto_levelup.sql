-- Add auto_levelup column to user_settings for automatic item level-ups.
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS auto_levelup BOOLEAN NOT NULL DEFAULT FALSE;
