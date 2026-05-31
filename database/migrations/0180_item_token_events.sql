-- 0180_item_token_events.sql
--
-- Per-token event log for the item NFT layer. One row per
-- mint / transfer / list / unlist / sold / burn so the inspect view
-- can show a token's full provenance, and so per-contract price
-- history can be aggregated for the lexicon / market views.
--
-- Idempotent: re-runs are safe (CREATE TABLE IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS item_token_events (
    event_id        BIGSERIAL PRIMARY KEY,
    token_id        TEXT      NOT NULL,
    contract_id     BIGINT,
    event_type      TEXT      NOT NULL,
    from_user_id    BIGINT,
    to_user_id      BIGINT,
    listing_id      BIGINT,
    -- Price fields land on 'sold' events (auction settle). Other
    -- event types leave them NULL.
    price_raw       NUMERIC(36, 0),
    currency        TEXT,
    -- USD snapshot at event time so a later oracle move doesn't
    -- distort historical reads.
    price_usd_raw   NUMERIC(36, 0),
    metadata        JSONB     NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT item_token_events_type_chk CHECK (event_type IN (
        'mint', 'transfer', 'list', 'unlist', 'sold', 'burn'
    ))
);

-- Token's own history: ordered ascending so the inspect render
-- can walk forward.
CREATE INDEX IF NOT EXISTS item_token_events_by_token_idx
    ON item_token_events (token_id, event_id);

-- Per-contract market history: powers the lexicon's "recent sales"
-- + price-history queries for one contract.
CREATE INDEX IF NOT EXISTS item_token_events_contract_idx
    ON item_token_events (contract_id, event_type, created_at DESC)
    WHERE contract_id IS NOT NULL;

-- Sold-only fast-path for price aggregation.
CREATE INDEX IF NOT EXISTS item_token_events_sold_idx
    ON item_token_events (contract_id, created_at DESC)
    WHERE event_type = 'sold';
