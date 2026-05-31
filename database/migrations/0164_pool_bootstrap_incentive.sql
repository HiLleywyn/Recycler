-- Pool bootstrap incentive: pay extra LP yield to positions in pools
-- with low TVL and low recent volume so the seeders who plant the FIRST
-- liquidity in a brand-new pool have an incentive that diminishes as
-- the pool fills up and starts trading. Decay is volume-driven so a
-- pool that someone whaled into but no one trades on still rewards
-- the next seeder.
--
-- recent_volume_usd_raw    -- rolling 24h-ish trade volume in USD (raw, scaled by 1e18).
-- recent_volume_window_at  -- when the rolling window last reset / decayed.
--
-- The LP yield tick reads both columns to compute a "bootstrap multiplier"
-- that compounds with the existing lock / user-token / group-pool
-- multipliers. The execute_swap path bumps recent_volume_usd_raw on every
-- successful swap; the tick decays it linearly so a quiet pool tapers
-- back into bonus-eligible.

ALTER TABLE pools
    ADD COLUMN IF NOT EXISTS recent_volume_usd_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS recent_volume_window_at TIMESTAMPTZ    NOT NULL DEFAULT NOW();

-- Index so the tick can quickly filter pools eligible for the bonus
-- (low TVL or low recent volume) without a full scan.
CREATE INDEX IF NOT EXISTS idx_pools_bootstrap_filter
    ON pools (guild_id, recent_volume_usd_raw);
