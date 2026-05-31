-- Add BTC reserve column to mining_groups.
-- Accrues when a group mines BTC blocks; spendable on Hall upgrades.
ALTER TABLE mining_groups
    ADD COLUMN IF NOT EXISTS reserve_btc NUMERIC(36,0) NOT NULL DEFAULT 0;
