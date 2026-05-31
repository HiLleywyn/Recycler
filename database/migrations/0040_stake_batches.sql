-- Migration 0040: per-deposit stake lock batches
-- Each manual stake/top-up gets its own row with its own staked_at so the
-- 24-hour lock countdown is tracked independently per batch rather than
-- being shared (and reset) across the whole position.
-- Safe to re-run: uses IF NOT EXISTS throughout.

CREATE TABLE IF NOT EXISTS stake_batches (
    id           BIGSERIAL    PRIMARY KEY,
    user_id      BIGINT       NOT NULL,
    guild_id     BIGINT       NOT NULL,
    validator_id TEXT         NOT NULL,
    symbol       TEXT         NOT NULL,
    amount       NUMERIC(28,8) NOT NULL DEFAULT 0,
    staked_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT chk_stake_batch_amount CHECK (amount >= 0)
);

CREATE INDEX IF NOT EXISTS idx_stake_batches_lookup
    ON stake_batches (user_id, guild_id, validator_id, staked_at ASC);
