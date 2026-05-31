-- 0282_eat_salad.sql
--
-- Eat the Rich "Salad Bowl" overhaul.
--
-- 1. The old one-shot ,eat cook buff (a single cook_until timestamp, added by
--    migration 0278) is replaced by the prep -> cook powerup CHAIN. Each
--    powerup charges for a wait duration and is then "armed" until consumed
--    by an eat. Stored as two charge-completion timestamps on exploit_stats:
--    a powerup is armed when its *_ready_at is non-null and <= now().
--
-- 2. The salad bowl: a multi-currency escrow that fills with every currency
--    type players steal from each other. One row per (guild_id, symbol).
--    Amounts are raw NUMERIC(36,0) scaled by 10^18, like every other
--    monetary column. ,eat salad gambles 1% to win 5% of the whole bowl.
--
-- All statements use IF EXISTS / IF NOT EXISTS so the migration is safe to
-- re-run on databases at any prior state.

ALTER TABLE exploit_stats DROP COLUMN IF EXISTS cook_until;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS prep_ready_at  TIMESTAMPTZ;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS cook_ready_at  TIMESTAMPTZ;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS salad_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE exploit_stats ADD COLUMN IF NOT EXISTS salad_won      INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS eat_salad_bowl (
    guild_id BIGINT        NOT NULL,
    symbol   TEXT          NOT NULL,
    amount   NUMERIC(36,0) NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, symbol)
);
