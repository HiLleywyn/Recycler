-- Server backups: a full, restorable snapshot of a guild (settings, roles,
-- channels, categories, permission overwrites and optionally recent messages).
CREATE TABLE IF NOT EXISTS backups (
    id            TEXT PRIMARY KEY,                       -- short public id
    owner_id      BIGINT NOT NULL,                        -- creator
    guild_id      BIGINT NOT NULL,                        -- source guild
    guild_name    TEXT NOT NULL,
    data          JSONB NOT NULL,                         -- serialized guild
    message_count INTEGER NOT NULL DEFAULT 0,
    encrypted     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_backups_owner   ON backups(owner_id);
CREATE INDEX IF NOT EXISTS idx_backups_guild   ON backups(guild_id);
CREATE INDEX IF NOT EXISTS idx_backups_created ON backups(created_at DESC);

-- Automatic, recurring backups per guild.
CREATE TABLE IF NOT EXISTS backup_intervals (
    guild_id       BIGINT PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    interval_hours INTEGER NOT NULL,
    keep           INTEGER NOT NULL DEFAULT 7,            -- rolling retention
    last_run_at    TIMESTAMPTZ,
    next_run_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enabled        BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_backup_intervals_due
    ON backup_intervals(next_run_at) WHERE enabled;
