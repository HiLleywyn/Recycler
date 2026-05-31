-- Migration 0056: economy_snapshots table for admin rollback feature
-- Stores periodic snapshots of wallets, holdings, prices, and pools per guild.
-- Snapshots are taken every 30 minutes by the snapshot background task.

CREATE TABLE IF NOT EXISTS economy_snapshots (
    id                  BIGSERIAL    PRIMARY KEY,
    guild_id            BIGINT       NOT NULL,
    taken_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    wallets             JSONB        NOT NULL DEFAULT '[]',
    crypto_holdings     JSONB        NOT NULL DEFAULT '[]',
    wallet_holdings     JSONB        NOT NULL DEFAULT '[]',
    prices              JSONB        NOT NULL DEFAULT '[]',
    pools               JSONB        NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_economy_snapshots_guild_ts
    ON economy_snapshots (guild_id, taken_at DESC);

-- Keep only the last 96 snapshots per guild (48 hours at 30-min intervals).
-- Older rows are pruned by the snapshot task after each insert.
