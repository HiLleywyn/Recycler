-- Buddy stake now accepts FREN OR BBT (both yield BUD at the same per-day
-- rate). Add the bbt_staked_raw column on user_buddy_economy so the
-- ledger can carry a separate BBT position alongside the existing
-- fren_staked_raw column. _accrue_pending() in services/buddy_economy.py
-- sums both before computing the BUD drip.

ALTER TABLE user_buddy_economy
    ADD COLUMN IF NOT EXISTS bbt_staked_raw NUMERIC(36, 0) NOT NULL DEFAULT 0;
