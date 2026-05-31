-- Migration: Add dedicated faucet spawn channel
-- Allows admins to set a channel specifically for faucet drops
-- independent of the drops_spawn_channel setting.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS faucet_channel BIGINT;
