-- 0053: Reset staked_amount on all stone tables to current DSD prices.
--
-- Migration 0050 multiplied existing staked values by 2x, but those values
-- were SUN-era quantities (e.g. 71,000 SUN), not DSD amounts. Multiplying
-- made them absurdly large (142,000). Since the shop has switched to DSD,
-- we reset all staked_amount to the correct current purchase price for each
-- stone type. Level and XP are preserved.
--
-- Current prices: hashstone 7500, lockstone 6000, vaultstone 5000,
--                 liqstone 8000, gambastone 10000.

DO $$
BEGIN
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='hashstones'  AND column_name='staked_amount') THEN UPDATE hashstones  SET staked_amount = 7500;  END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='lockstones'  AND column_name='staked_amount') THEN UPDATE lockstones  SET staked_amount = 6000;  END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='vaultstones' AND column_name='staked_amount') THEN UPDATE vaultstones SET staked_amount = 5000;  END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='liqstones'   AND column_name='staked_amount') THEN UPDATE liqstones   SET staked_amount = 8000;  END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='gambastones' AND column_name='staked_amount') THEN UPDATE gambastones SET staked_amount = 10000; END IF;
END $$;
