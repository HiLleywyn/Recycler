-- 0231_economy_health_snapshots.sql
--
-- Periodic distributional snapshots driving the ,economy Health tab
-- trend deltas, the adaptive-faucet per-capita supply multiplier, and
-- the wealth-equalizer cycle history. Each row is the cheap, aggregate
-- form of a full economy_snapshots row -- numbers only, no JSONB state
-- -- so reading 30 rows for a trend chart is an indexed range scan.
--
-- The full economy_snapshots table (migration 0056) is for rollback;
-- it stores raw user state. This table stores distribution metrics
-- (Gini, percentiles, top-N concentration, redistribution pool) for
-- soundness telemetry only.

CREATE TABLE IF NOT EXISTS economy_health_snapshots (
    id           BIGSERIAL    PRIMARY KEY,
    guild_id     BIGINT       NOT NULL,
    snapshot_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    n_holders    INTEGER      NOT NULL DEFAULT 0,
    total_supply DOUBLE PRECISION NOT NULL DEFAULT 0,
    gini         DOUBLE PRECISION NOT NULL DEFAULT 0,
    top1_pct     DOUBLE PRECISION NOT NULL DEFAULT 0,
    top8_pct     DOUBLE PRECISION NOT NULL DEFAULT 0,
    top25_pct    DOUBLE PRECISION NOT NULL DEFAULT 0,
    median       DOUBLE PRECISION NOT NULL DEFAULT 0,
    p90          DOUBLE PRECISION NOT NULL DEFAULT 0,
    p99          DOUBLE PRECISION NOT NULL DEFAULT 0,
    pool_usd     DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS economy_health_snapshots_guild_ts_idx
    ON economy_health_snapshots (guild_id, snapshot_at DESC);

-- Closed-loop game-token burn phase reuses wealth_redistribution_log so
-- ,wealth flow / ,wealth top render the burn entries alongside the USD
-- tax + UBI flow. The 0230 migration's CHECK constraint pinned kind to
-- ('tax', 'ubi'); widen it to accept the new 'token_burn' kind too.
ALTER TABLE wealth_redistribution_log
    DROP CONSTRAINT IF EXISTS wealth_redistribution_log_kind_check;
ALTER TABLE wealth_redistribution_log
    ADD CONSTRAINT wealth_redistribution_log_kind_check
    CHECK (kind IN ('tax', 'ubi', 'token_burn'));
