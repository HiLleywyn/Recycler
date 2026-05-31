-- Migration 0199: changelog auto-post channel + last-posted tracker
-- Adds two columns to guild_settings so the bot can auto-post the
-- latest changelog entry to a designated channel once per day.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS changelog_channel   BIGINT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS changelog_last_posted TEXT   DEFAULT NULL;
