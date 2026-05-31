-- 0224_group_lp_cost_basis.sql
--
-- ``,group pool harvest`` had a destructive bug: it removed the
-- ENTIRE LP position from the pool and credited the proceeds to the
-- group's reserve_usd, instead of claiming only the fee earnings
-- accrued above the position's cost basis. After harvest the group
-- ended up with ``lp_shares = 0``, so the per-tick LP-yield sweep
-- (services/lp_yield.tick_lp_yield_for_guild) -- which filters on
-- ``lp_shares > 0`` -- silently skipped the position forever, and
-- the LP value stayed pinned at 0.
--
-- The fix tracks each position's USD cost basis. Harvest now removes
-- only the fraction of LP whose value sits ABOVE that baseline, so the
-- principal stays in the pool and keeps earning swap fees +
-- per-tick yield. Going forward:
--   * seed_group_pool sets cost_basis_usd_raw = total seeded USD value
--   * deposit_group_lp_from_reserve (new helper) bumps both lp_shares
--     and cost_basis_usd_raw by the contributed USD value
--   * harvest_group_lp_fees_only computes the USD gain over basis,
--     translates that to a fractional LP burn, and never lowers the
--     remaining position below cost_basis
--
-- Backfill: every existing position gets its cost_basis_usd_raw set to
-- the CURRENT LP value (resv_a*price_a + resv_b*price_b) * (lp_shares
-- / total_lp). That treats any growth-to-date as already-paid
-- (conservative but safe -- nobody loses what's already in the pool).
-- Positions whose principal was already burned by the buggy harvest
-- (lp_shares = 0) get cost_basis_usd_raw = 0 and need a manual top-up
-- via ``,group pool deposit`` (the new founder-only recovery command)
-- to start earning yield again.
--
-- Idempotent. Safe to re-run.

ALTER TABLE group_lp_positions
    ADD COLUMN IF NOT EXISTS cost_basis_usd_raw NUMERIC(36, 0) NOT NULL DEFAULT 0;

UPDATE group_lp_positions glp
   SET cost_basis_usd_raw = COALESCE((
       SELECT (
                  ((p.reserve_a::NUMERIC * COALESCE(pa.price, 0)::NUMERIC)
                 + (p.reserve_b::NUMERIC * COALESCE(pb.price, 0)::NUMERIC))
                  * glp.lp_shares::NUMERIC
                  / NULLIF(p.total_lp::NUMERIC, 0)
              )::NUMERIC(36, 0)
         FROM pools p
         LEFT JOIN crypto_prices pa
                ON pa.symbol = p.token_a AND pa.guild_id = p.guild_id
         LEFT JOIN crypto_prices pb
                ON pb.symbol = p.token_b AND pb.guild_id = p.guild_id
        WHERE p.pool_id  = glp.pool_id
          AND p.guild_id = glp.guild_id
   ), 0)
 WHERE glp.cost_basis_usd_raw = 0
   AND glp.lp_shares > 0;
