-- V3 Pillar 10: Continuous Wealth Equalizer (per-tx tax + streaming UBI)
--
-- Pre-V3 the wealth tax ran once a day per guild as a single big cycle.
-- Whales could extract value freely between cycles, UBI was lumpy, and
-- Gini moved in steps. V3 makes the tax flow continuous: every economic
-- credit (daily, work, harvest, claim, etc.) pays a marginal tax that
-- depends on the player's net-worth percentile. The pool drains
-- continuously back to recipients via a streaming UBI tick.
--
-- The flow stays identical -- tax in -> pool -> UBI out, fully
-- auditable, every payer linkable to every recipient -- only the
-- timing changes. So this migration only ADDS columns; the existing
-- wealth_redistribution_log + wealth_redistribution_pool tables keep
-- their shape and every existing query (,wealth flow, ,wealth top,
-- ,drs equalizer cycle, the API exports) is unchanged.

-- 1) Log-row linkage: each UBI payment points back at the most recent
--    payer it's sourced from so "who paid your UBI" lookups still work.
--    NULL on tax rows; non-NULL on ubi_tx rows when we can identify a
--    contributor. cycle_id groups every per-tx event into a rolling
--    24h "virtual cycle" so the legacy ,drs equalizer cycle <#> screen
--    still pages cleanly (just with many small rows instead of two big
--    ones per day).
ALTER TABLE wealth_redistribution_log
    ADD COLUMN IF NOT EXISTS linked_payer_id BIGINT,
    ADD COLUMN IF NOT EXISTS cycle_id        TEXT NOT NULL DEFAULT 'legacy';

-- Index so "show me everyone who paid my UBI" stays fast.
CREATE INDEX IF NOT EXISTS wealth_redistribution_log_linked_payer_idx
    ON wealth_redistribution_log (guild_id, linked_payer_id)
    WHERE linked_payer_id IS NOT NULL;

-- Index by virtual cycle.
CREATE INDEX IF NOT EXISTS wealth_redistribution_log_cycle_id_idx
    ON wealth_redistribution_log (guild_id, cycle_id, cycle_at DESC);

-- 2) Controller log: the Gini-targeting PI loop writes one row per
--    tick (5 min) so operators can audit the auto-balance behavior.
CREATE TABLE IF NOT EXISTS cwe_controller_log (
    id            BIGSERIAL   PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    gini          DOUBLE PRECISION NOT NULL,
    target_gini   DOUBLE PRECISION NOT NULL,
    tax_mult      DOUBLE PRECISION NOT NULL,
    ubi_mult      DOUBLE PRECISION NOT NULL,
    integral      DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS cwe_controller_log_guild_ts_idx
    ON cwe_controller_log (guild_id, ts DESC);

-- 3) Per-user per-day floor-bonus cap. The bottom 25% of holders get
--    a small pool-funded bonus on every economic credit. To keep this
--    unfarmable we cap each user's total bonus per UTC day. The
--    last_reset column gets bumped on the first credit each day.
CREATE TABLE IF NOT EXISTS cwe_user_tx_state (
    guild_id          BIGINT      NOT NULL,
    user_id           BIGINT      NOT NULL,
    taxed_today_raw   NUMERIC(36,0) NOT NULL DEFAULT 0,
    bonus_today_raw   NUMERIC(36,0) NOT NULL DEFAULT 0,
    last_reset        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);
