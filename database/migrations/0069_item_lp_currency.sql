-- Add lp_currency column to all stone tables to track which stablecoin
-- the item was purchased with (DSD or USDC). This determines which LP pool
-- the item's staked amount goes to.

ALTER TABLE hashstones  ADD COLUMN IF NOT EXISTS lp_currency TEXT NOT NULL DEFAULT 'DSD';
ALTER TABLE lockstones  ADD COLUMN IF NOT EXISTS lp_currency TEXT NOT NULL DEFAULT 'DSD';
ALTER TABLE vaultstones ADD COLUMN IF NOT EXISTS lp_currency TEXT NOT NULL DEFAULT 'DSD';
ALTER TABLE liqstones   ADD COLUMN IF NOT EXISTS lp_currency TEXT NOT NULL DEFAULT 'DSD';
ALTER TABLE gambastones ADD COLUMN IF NOT EXISTS lp_currency TEXT NOT NULL DEFAULT 'DSD';
