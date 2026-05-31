-- Backfill LP positions for existing stone holders.
-- All existing stones default to DSD, so LP goes into the DSD-USD pool.
-- This creates LP positions proportional to each stone's staked_amount.
--
-- For each stone holder: add staked_amount/2 to each side of the DSD-USD pool,
-- mint LP shares proportionally, and record a snapshot for fee tracking.

-- Step 1: Aggregate total staked_amount per user across all stones
CREATE TEMP TABLE _item_lp_backfill AS
SELECT user_id, guild_id, SUM(staked_amount) AS total_staked
FROM (
    SELECT user_id, guild_id, staked_amount FROM hashstones WHERE staked_amount > 0
    UNION ALL
    SELECT user_id, guild_id, staked_amount FROM lockstones WHERE staked_amount > 0
    UNION ALL
    SELECT user_id, guild_id, staked_amount FROM vaultstones WHERE staked_amount > 0
    UNION ALL
    SELECT user_id, guild_id, staked_amount FROM liqstones WHERE staked_amount > 0
    UNION ALL
    SELECT user_id, guild_id, staked_amount FROM gambastones WHERE staked_amount > 0
) sub
GROUP BY user_id, guild_id
HAVING SUM(staked_amount) > 0;

-- Step 2: For each user+guild, add LP to the DSD-USD pool.
-- We compute lp_minted as: (half / reserve_a) * total_lp (proportional mint).
-- If the user already has LP in DSD-USD, we add to it.
INSERT INTO lp_positions (user_id, guild_id, pool_id, lp_shares, added_at)
SELECT
    b.user_id,
    b.guild_id,
    'DSD-USD',
    -- proportional LP: (staked/2 / reserve_a) * total_lp
    CASE
        WHEN p.reserve_a > 0 AND p.total_lp > 0
        THEN (b.total_staked / 2.0 / p.reserve_a) * p.total_lp
        ELSE b.total_staked / 2.0  -- fallback: 1:1 if pool is empty
    END,
    NOW()
FROM _item_lp_backfill b
JOIN pools p ON p.pool_id = 'DSD-USD' AND p.guild_id = b.guild_id
ON CONFLICT (user_id, guild_id, pool_id)
DO UPDATE SET lp_shares = lp_positions.lp_shares + EXCLUDED.lp_shares;

-- Step 3: Add the staked amounts to pool reserves (both sides equally)
UPDATE pools p
SET
    reserve_a = p.reserve_a + agg.total_add,
    reserve_b = p.reserve_b + agg.total_add,
    total_lp  = p.total_lp  + agg.total_lp_minted
FROM (
    SELECT
        b.guild_id,
        SUM(b.total_staked / 2.0) AS total_add,
        SUM(
            CASE
                WHEN p2.reserve_a > 0 AND p2.total_lp > 0
                THEN (b.total_staked / 2.0 / p2.reserve_a) * p2.total_lp
                ELSE b.total_staked / 2.0
            END
        ) AS total_lp_minted
    FROM _item_lp_backfill b
    JOIN pools p2 ON p2.pool_id = 'DSD-USD' AND p2.guild_id = b.guild_id
    GROUP BY b.guild_id
) agg
WHERE p.pool_id = 'DSD-USD' AND p.guild_id = agg.guild_id;

-- Step 4: Create LP snapshots for fee tracking (entry point = current reserves after adding)
INSERT INTO lp_snapshots (user_id, guild_id, pool_id, entry_res_a_per_lp, entry_res_b_per_lp)
SELECT
    b.user_id,
    b.guild_id,
    'DSD-USD',
    CASE WHEN p.total_lp > 0 THEN p.reserve_a / p.total_lp ELSE 1.0 END,
    CASE WHEN p.total_lp > 0 THEN p.reserve_b / p.total_lp ELSE 1.0 END
FROM _item_lp_backfill b
JOIN pools p ON p.pool_id = 'DSD-USD' AND p.guild_id = b.guild_id
ON CONFLICT (user_id, guild_id, pool_id)
DO UPDATE SET
    entry_res_a_per_lp = EXCLUDED.entry_res_a_per_lp,
    entry_res_b_per_lp = EXCLUDED.entry_res_b_per_lp;

DROP TABLE _item_lp_backfill;
