-- 0050: Fix wallet_holdings/transactions network key "discoin" -> "dsc"
--       and rescale staked_amount values to match new item pricing.

-- Fix network key mismatch: wallet_holdings stored under "discoin" are
-- invisible to move/bank commands which use the canonical short key "dsc".
--
-- Some users may have BOTH a "discoin" row AND a "dsc" row for the same
-- (user_id, guild_id, symbol) if they received DSD via different code paths.
-- We merge those by adding the discoin amount into the dsc row, then deleting
-- the discoin row, before renaming any remaining discoin rows to dsc.
--
-- All blocks guarded in case the table or column does not yet exist.

DO $$
BEGIN
    IF EXISTS (
        SELECT FROM information_schema.columns
        WHERE table_name = 'wallet_holdings' AND column_name = 'network'
    ) THEN
        -- Step 1: Where both 'discoin' and 'dsc' rows exist, add amounts into the dsc row.
        UPDATE wallet_holdings AS dst
        SET    amount = dst.amount + src.amount
        FROM   wallet_holdings AS src
        WHERE  src.network  = 'discoin'
          AND  dst.network  = 'dsc'
          AND  src.user_id  = dst.user_id
          AND  src.guild_id = dst.guild_id
          AND  src.symbol   = dst.symbol;

        -- Step 2: Delete the now-merged 'discoin' rows that have a matching 'dsc' row.
        DELETE FROM wallet_holdings
        WHERE  network = 'discoin'
          AND  EXISTS (
                 SELECT 1 FROM wallet_holdings dup
                 WHERE  dup.network  = 'dsc'
                   AND  dup.user_id  = wallet_holdings.user_id
                   AND  dup.guild_id = wallet_holdings.guild_id
                   AND  dup.symbol   = wallet_holdings.symbol
               );

        -- Step 3: Rename any remaining 'discoin' rows that have no 'dsc' counterpart.
        UPDATE wallet_holdings SET network = 'dsc' WHERE network = 'discoin';
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT FROM information_schema.columns
        WHERE table_name = 'transactions' AND column_name = 'network'
    ) THEN
        UPDATE transactions SET network = 'dsc' WHERE LOWER(network) = 'discoin';
    END IF;
END $$;

-- Rescale stone stake column to match new item prices (new prices = 2x original).
-- Column may be named staked_sun (old) or staked_amount (after 0051/entrypoint rename).
-- Each block checks which name exists and uses it. Gambastone: 6000->10000 (5/3x).
DO $$
DECLARE col TEXT;
BEGIN
    -- hashstones
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'hashstones') THEN
        SELECT column_name INTO col FROM information_schema.columns
        WHERE table_name='hashstones' AND column_name IN ('staked_sun','staked_amount') LIMIT 1;
        IF col IS NOT NULL THEN
            EXECUTE format('UPDATE hashstones SET %I = ROUND((%I * 2.0)::numeric, 2)', col, col);
        END IF;
    END IF;
    -- lockstones
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'lockstones') THEN
        SELECT column_name INTO col FROM information_schema.columns
        WHERE table_name='lockstones' AND column_name IN ('staked_sun','staked_amount') LIMIT 1;
        IF col IS NOT NULL THEN
            EXECUTE format('UPDATE lockstones SET %I = ROUND((%I * 2.0)::numeric, 2)', col, col);
        END IF;
    END IF;
    -- vaultstones
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'vaultstones') THEN
        SELECT column_name INTO col FROM information_schema.columns
        WHERE table_name='vaultstones' AND column_name IN ('staked_sun','staked_amount') LIMIT 1;
        IF col IS NOT NULL THEN
            EXECUTE format('UPDATE vaultstones SET %I = ROUND((%I * 2.0)::numeric, 2)', col, col);
        END IF;
    END IF;
    -- liqstones
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'liqstones') THEN
        SELECT column_name INTO col FROM information_schema.columns
        WHERE table_name='liqstones' AND column_name IN ('staked_sun','staked_amount') LIMIT 1;
        IF col IS NOT NULL THEN
            EXECUTE format('UPDATE liqstones SET %I = ROUND((%I * 2.0)::numeric, 2)', col, col);
        END IF;
    END IF;
    -- gambastones (5/3x)
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'gambastones') THEN
        SELECT column_name INTO col FROM information_schema.columns
        WHERE table_name='gambastones' AND column_name IN ('staked_sun','staked_amount') LIMIT 1;
        IF col IS NOT NULL THEN
            EXECUTE format('UPDATE gambastones SET %I = ROUND((%I * 5.0 / 3.0)::numeric, 2)', col, col);
        END IF;
    END IF;
END $$;
