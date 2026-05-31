-- V3 Pillar 10: CWE default tax/UBI curve seed
--
-- The curve is keyed off the player's net-worth PERCENTILE in their
-- guild rather than absolute dollar brackets, which makes it
-- intrinsically self-balancing: as the economy grows the curve adapts
-- to where players sit relative to each other.
--
-- Persisted in cwe_curve so admins can override per-guild later. The
-- guild_id = 0 row is the global default every guild inherits when a
-- per-guild override is absent.

CREATE TABLE IF NOT EXISTS cwe_curve (
    guild_id          BIGINT      PRIMARY KEY,
    -- Tax brackets: anchor percentiles + the marginal tax rate at each
    -- anchor. The runtime interpolates linearly between consecutive
    -- anchors.
    --   bottom 50% -> 0%
    --   50-75%    -> linear 0 to 2%
    --   75-90%    -> linear 2% to 8%
    --   90-99%    -> linear 8% to 25%
    --   top 1%    -> flat 30%
    -- Encoded as parallel arrays so adding/removing anchors is just
    -- two UPDATEs.
    pctile_anchors    DOUBLE PRECISION[] NOT NULL,
    pctile_rates      DOUBLE PRECISION[] NOT NULL,
    -- Floor bonus curve. Bottom percentile -> bonus rate. Same anchor
    -- arrays semantics as the tax curve.
    bonus_anchors     DOUBLE PRECISION[] NOT NULL,
    bonus_rates       DOUBLE PRECISION[] NOT NULL,
    -- Per-day USD cap on the floor bonus so it isn't farmable.
    bonus_daily_cap   DOUBLE PRECISION NOT NULL DEFAULT 500.0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent seed of the global default row.
INSERT INTO cwe_curve (
    guild_id,
    pctile_anchors, pctile_rates,
    bonus_anchors,  bonus_rates,
    bonus_daily_cap
) VALUES (
    0,
    ARRAY[0.00, 0.50, 0.75, 0.90, 0.99, 1.00]::DOUBLE PRECISION[],
    ARRAY[0.00, 0.00, 0.02, 0.08, 0.25, 0.30]::DOUBLE PRECISION[],
    ARRAY[0.00, 0.25, 0.50, 1.00]::DOUBLE PRECISION[],
    ARRAY[0.05, 0.02, 0.00, 0.00]::DOUBLE PRECISION[],
    500.0
)
ON CONFLICT (guild_id) DO NOTHING;
