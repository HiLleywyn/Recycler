-- Track LP shares that are locked by item stakes (hashstone, lockstone, vaultstone, liqstone).
-- Locked shares cannot be manually removed via removelp while the item is held.
-- The lock is incremented by _item_lp_add and decremented by _item_lp_remove (on stone sale).
ALTER TABLE lp_positions
    ADD COLUMN IF NOT EXISTS locked_lp_shares NUMERIC(36,0) NOT NULL DEFAULT 0;
