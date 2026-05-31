-- Migration 0202: per-guild cosmetic role name overrides
-- Allows admins to map each cosmetic item key to a custom Discord role name.
-- Format: '{"glamour_kit": "VIP Glam", "aurora_pass": "Rainbow"}'

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS cosmetic_role_overrides JSONB DEFAULT '{}'::jsonb NOT NULL;
