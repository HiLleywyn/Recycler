-- 0031_rugpull_expansion.sql
-- Add bounty, sabotage, defense streak, and tax decree to rugpull.

BEGIN;

-- King table: add defense streak, tax rate, sabotage pool
ALTER TABLE rugpull_king
    ADD COLUMN IF NOT EXISTS defense_streak INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tax_rate       NUMERIC(5,2) NOT NULL DEFAULT 1.00,
    ADD COLUMN IF NOT EXISTS sabotage_pool  NUMERIC(28,8) NOT NULL DEFAULT 0.0;

-- Bounty board: anyone can add to it, winner collects all
ALTER TABLE rugpull_king
    ADD COLUMN IF NOT EXISTS bounty_pool NUMERIC(28,8) NOT NULL DEFAULT 0.0;

-- Stats: track defenses and sabotages
ALTER TABLE rugpull_stats
    ADD COLUMN IF NOT EXISTS defenses       INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sabotages_done INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS bounties_placed NUMERIC(28,8) NOT NULL DEFAULT 0.0;

-- History log for recent attempts
CREATE TABLE IF NOT EXISTS rugpull_history (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    tier       TEXT NOT NULL,
    wager      NUMERIC(28,8) NOT NULL,
    won        BOOLEAN NOT NULL,
    king_id    BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rugpull_history_guild ON rugpull_history (guild_id, created_at DESC);

COMMIT;
