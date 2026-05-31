-- Migration 0046: per-guild heal AI provider config
-- Adds three nullable columns to guild_settings so admins can override
-- the global AI backend/model for the ,health analyze command.
-- Safe to run on existing databases; all columns are nullable with NULL defaults.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS heal_ai_backend  TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS heal_ai_model    TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS heal_ai_base_url TEXT DEFAULT NULL;
