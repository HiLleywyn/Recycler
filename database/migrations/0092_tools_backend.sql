-- Migration 0092: per-guild agent loop backend override
-- Adds a nullable column to guild_settings so admins can route the
-- agent tool loop to ollama or openrouter without touching env vars.
-- NULL means "use the TOOLS_BACKEND env var" (default: openrouter).

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS tools_backend TEXT DEFAULT NULL;
