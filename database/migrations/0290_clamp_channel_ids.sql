-- 0290_clamp_channel_ids.sql
-- Clamp guard channel list: array of channel IDs where ambient detection runs.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS clamp_channel_ids BIGINT[] NOT NULL DEFAULT '{}';
