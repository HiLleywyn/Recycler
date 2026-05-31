-- 0230_wealth_redistribution.sql
--
-- Wealth Equalizer: progressive wealth tax + UBI stipend.
--
-- The daily background task drains a slice of every player's liquid
-- stablecoin holdings (wallet + bank + USD savings) on a progressive
-- bracket ladder, accumulates the proceeds in wealth_redistribution_pool,
-- then pays the pool out as a flat UBI stipend to active players whose
-- net worth is below the poverty line. Per-cycle activity is logged in
-- wealth_redistribution_log so ,wealth can render a recap and admins can
-- audit who paid what / who received what without re-running the math.

CREATE TABLE IF NOT EXISTS wealth_redistribution_pool (
    guild_id     BIGINT      PRIMARY KEY,
    pool_raw     NUMERIC(36,0) NOT NULL DEFAULT 0,
    last_tax_at  TIMESTAMPTZ,
    last_ubi_at  TIMESTAMPTZ,
    cycles       INTEGER     NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wealth_redistribution_log (
    id          BIGSERIAL   PRIMARY KEY,
    guild_id    BIGINT      NOT NULL,
    user_id     BIGINT      NOT NULL,
    kind        TEXT        NOT NULL CHECK (kind IN ('tax', 'ubi')),
    amount_raw  NUMERIC(36,0) NOT NULL,
    net_worth_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
    cycle_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wealth_redistribution_log_guild_cycle_idx
    ON wealth_redistribution_log (guild_id, cycle_at DESC);

CREATE INDEX IF NOT EXISTS wealth_redistribution_log_user_idx
    ON wealth_redistribution_log (guild_id, user_id, cycle_at DESC);
