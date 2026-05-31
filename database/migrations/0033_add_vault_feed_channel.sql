-- Migration: Add vault_feed_channel column to guild_settings
-- Fixes: column "vault_feed_channel" of relation "guild_settings" does not exist

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS vault_feed_channel BIGINT;