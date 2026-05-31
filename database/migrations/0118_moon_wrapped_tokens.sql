-- Moon Network wrapped coins: mBTC / mSUN replace raw BTC / SUN / DSD as the
-- trading pairs for every group token. Pools are system-seeded at boot in
-- database/pools.py::seed_group_genesis_pools; this migration clears the
-- stale pair shapes so the reseeder can take over.
--
-- What gets cleared:
--   * Group-token / DSD pools       (taken out of the genesis shape)
--   * Group-token / raw-BTC pools   (replaced by TOKEN/mBTC)
--   * Group-token / raw-SUN pools   (replaced by TOKEN/mSUN)
--   * Their lp_positions + lp_snapshots rows
--
-- LP safety (the important bit):
--   Any user who was providing liquidity on one of these stale pools is
--   REFUNDED their pro-rata share of BOTH sides of the pool before the
--   pool row is deleted. The credit goes to their DeFi wallet on the
--   appropriate network:
--       DSD         -> dsc network
--       BTC         -> btc network
--       SUN         -> sun network
--       group token -> moon network (bridged Moon Network, same place
--                                   a user's swapped group tokens land)
--   System-seeded genesis liquidity with no lp_positions rows backing it
--   vanishes with the pool -- which is correct, no user is owed it.
--   Vault-locked pools have no lp_positions either (vault_locked blocks
--   LP ops), so those drain silently too.
--
-- What stays:
--   * TOKEN / MOON pools (Lunar Mint off-ramp, unchanged).
--   * TOKEN / TOKEN partnership pools between two group tokens.
--   * Any pool whose OTHER side is not DSD/BTC/SUN.
--
-- Idempotent: re-running is a no-op because the JOIN to guild_tokens
-- (token_type='group') AND the pool-side filter only match a bounded
-- set; once those pools are deleted, the array is empty and nothing
-- happens. Safe to re-run.

DO $$
DECLARE
    stale_pool_ids TEXT[];
    refund_rows    INT;
BEGIN
    SELECT array_agg(DISTINCT p.pool_id)
      INTO stale_pool_ids
      FROM pools p
      JOIN guild_tokens gt
        ON gt.guild_id = p.guild_id
       AND gt.token_type = 'group'
       AND gt.symbol IN (p.token_a, p.token_b)
     WHERE (p.token_a IN ('DSD', 'BTC', 'SUN') OR p.token_b IN ('DSD', 'BTC', 'SUN'));

    IF stale_pool_ids IS NOT NULL THEN
        -- ── Refund LP providers BEFORE deleting the pools ────────────────
        -- Each lp_positions row becomes two wallet_holdings credits, one
        -- per side of the pool, scaled by lp_shares / total_lp. FLOOR on
        -- the raw NUMERIC arithmetic keeps everything integer so nothing
        -- drifts across the decimal boundary.
        WITH refund_flat AS (
            SELECT
                lp.user_id,
                lp.guild_id,
                p.token_a AS sym,
                FLOOR(
                    p.reserve_a::NUMERIC
                    * lp.lp_shares::NUMERIC
                    / NULLIF(p.total_lp::NUMERIC, 0)
                )::NUMERIC(36,0) AS amt
              FROM lp_positions lp
              JOIN pools p
                ON p.pool_id = lp.pool_id AND p.guild_id = lp.guild_id
             WHERE p.pool_id = ANY(stale_pool_ids)
               AND lp.lp_shares > 0
               AND p.total_lp > 0
            UNION ALL
            SELECT
                lp.user_id,
                lp.guild_id,
                p.token_b AS sym,
                FLOOR(
                    p.reserve_b::NUMERIC
                    * lp.lp_shares::NUMERIC
                    / NULLIF(p.total_lp::NUMERIC, 0)
                )::NUMERIC(36,0) AS amt
              FROM lp_positions lp
              JOIN pools p
                ON p.pool_id = lp.pool_id AND p.guild_id = lp.guild_id
             WHERE p.pool_id = ANY(stale_pool_ids)
               AND lp.lp_shares > 0
               AND p.total_lp > 0
        )
        INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
        SELECT
            user_id,
            guild_id,
            CASE sym
                WHEN 'DSD' THEN 'dsc'
                WHEN 'BTC' THEN 'btc'
                WHEN 'SUN' THEN 'sun'
                -- Every other symbol in this refund set is a user-created
                -- group token, which lives on the bridged Moon Network.
                ELSE 'moon'
            END AS network,
            sym,
            amt
          FROM refund_flat
         WHERE amt > 0
        ON CONFLICT (user_id, guild_id, network, symbol)
        DO UPDATE SET amount = wallet_holdings.amount + EXCLUDED.amount;

        GET DIAGNOSTICS refund_rows = ROW_COUNT;
        IF refund_rows > 0 THEN
            RAISE NOTICE 'migration 0118: refunded % wallet_holdings credit(s) from stale group-token pools',
                         refund_rows;
        END IF;

        -- Order matters: snapshots + positions before pools for FK cleanup.
        DELETE FROM lp_snapshots
         WHERE pool_id = ANY(stale_pool_ids);
        DELETE FROM lp_positions
         WHERE pool_id = ANY(stale_pool_ids);
        DELETE FROM pools
         WHERE pool_id = ANY(stale_pool_ids);

        RAISE NOTICE 'migration 0118: cleared % stale group-token pool(s) for reseeding',
                     array_length(stale_pool_ids, 1);
    END IF;
END $$;
