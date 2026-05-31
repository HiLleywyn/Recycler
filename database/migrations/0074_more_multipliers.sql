-- 0074_more_multipliers.sql
-- Extend per-guild earnings multipliers to cover all income sources.
-- All default to 1.0 (no change from current behaviour).

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS mining_multiplier    NUMERIC DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS staking_multiplier   NUMERIC DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS validator_multiplier NUMERIC DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS drops_multiplier     NUMERIC DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS beg_multiplier       NUMERIC DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS ape_multiplier       NUMERIC DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS savings_multiplier   NUMERIC DEFAULT 1.0;
