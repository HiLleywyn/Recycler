-- 0058: Add gm_announce_role_id to guild_settings for GM announcement pings.
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS gm_announce_role_id BIGINT;
