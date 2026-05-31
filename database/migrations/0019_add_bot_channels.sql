-- Add bot_channels column for no-prefix mode
-- Stores comma-separated channel IDs where commands work without prefix
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS bot_channels TEXT NOT NULL DEFAULT '';
