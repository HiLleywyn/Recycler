-- Per-guild configuration. One row per guild; the framework seeds an empty
-- row on join. Columns the framework + clank read directly live as real
-- columns; everything the settings cog toggles lives in the `features` JSONB.
--
-- This migration runs first (0001) so the clank migrations (0285+) that
-- ALTER guild_settings have a table to extend.
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id              BIGINT PRIMARY KEY,
    prefix                TEXT,                       -- per-guild prefix override
    bot_channels          TEXT,                       -- CSV of channel ids (framework)
    log_channel           BIGINT,                     -- audit/event log target
    -- clank containment channels (read by cogs/clank.py; also fall back to env)
    clanktank_channel     BIGINT,
    clanktank_log_channel BIGINT,
    -- feature toggles + free-form settings managed by cogs/settings.py
    features              JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
