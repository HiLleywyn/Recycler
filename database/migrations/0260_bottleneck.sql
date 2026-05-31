-- 0260_bottleneck.sql
--
-- Wealth Bottleneck replaces the legacy Wealth Equalizer (daily wealth
-- tax + UBI cycle) and the V3 Continuous Wealth Equalizer (per-tx tax +
-- streaming UBI + Gini PI controller + per-day bonus cap). The new system
-- is a single rank-based multiplier applied to every economic credit:
-- wealthy players keep less of each gain, poor players get a top-up. All
-- accounting is in USD-stable; non-stable holdings (stones, bags, rigs,
-- stakes, NFTs, savings deposits) are never drained again.
--
-- This migration drops the entire legacy tax/equalizer schema (six tables,
-- including everything seeded in 0230 / 0231 / 0248 / 0249 / 0251) and
-- creates the two new tables the bottleneck service needs:
--
--   wealth_pool      one row per guild; per-guild USD pool that funds boost.
--   bottleneck_log   audit row per credit: gross, net, drag, boost, mult.
--
-- The new system is self-funding (drag in == boost out, never inflationary)
-- so there is no controller, no per-day bonus cap state, no Gini snapshot
-- table.

-- ── Drop the legacy schema ─────────────────────────────────────────────────
-- Dropped in dependency order. CASCADE handles any indexes / FKs introduced
-- by sibling migrations (e.g. 0246 / 0247 LP-restore audit columns).

DROP TABLE IF EXISTS cwe_user_tx_state         CASCADE;
DROP TABLE IF EXISTS cwe_controller_log        CASCADE;
DROP TABLE IF EXISTS cwe_curve                 CASCADE;
DROP TABLE IF EXISTS economy_health_snapshots  CASCADE;
DROP TABLE IF EXISTS wealth_redistribution_log CASCADE;
DROP TABLE IF EXISTS wealth_redistribution_pool CASCADE;

-- ── New schema ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS wealth_pool (
    guild_id    BIGINT          PRIMARY KEY,
    pool_raw    NUMERIC(36,0)   NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bottleneck_log (
    id                BIGSERIAL       PRIMARY KEY,
    guild_id          BIGINT          NOT NULL,
    user_id           BIGINT          NOT NULL,
    kind              TEXT            NOT NULL,
    symbol            TEXT            NOT NULL DEFAULT 'USD',
    gross_raw         NUMERIC(36,0)   NOT NULL,
    net_credit_raw    NUMERIC(36,0)   NOT NULL,
    boost_wallet_raw  NUMERIC(36,0)   NOT NULL DEFAULT 0,
    drag_usd_raw      NUMERIC(36,0)   NOT NULL DEFAULT 0,
    multiplier        DOUBLE PRECISION NOT NULL,
    percentile        DOUBLE PRECISION NOT NULL,
    at                TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS bottleneck_log_guild_at_idx
    ON bottleneck_log (guild_id, at DESC);

CREATE INDEX IF NOT EXISTS bottleneck_log_user_at_idx
    ON bottleneck_log (guild_id, user_id, at DESC);
