-- Migration 0091: per-guild search backend override
-- Adds a nullable column to guild_settings so admins can choose between
-- ddg, openrouter, perplexity, and ollama search without touching env vars.
-- NULL means "use the SEARCH_BACKEND env var" (default: ddg).

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS search_backend TEXT DEFAULT NULL;
