-- 0269_market_ohlcv.sql
--
-- Postgres-backed OHLCV cache for the $ market dispatcher. The provider
-- layer (services/market/) writes here best-effort so $chart / $scan can
-- answer from local storage when Redis is cold or the upstream is rate-
-- limited. Schema deliberately stays small -- this is a cache, not
-- authoritative history.

CREATE TABLE IF NOT EXISTS market_ohlcv (
    symbol       TEXT        NOT NULL,
    asset_class  TEXT        NOT NULL,
    tf           TEXT        NOT NULL,
    ts           BIGINT      NOT NULL,
    o            DOUBLE PRECISION NOT NULL,
    h            DOUBLE PRECISION NOT NULL,
    l            DOUBLE PRECISION NOT NULL,
    c            DOUBLE PRECISION NOT NULL,
    v            DOUBLE PRECISION NOT NULL DEFAULT 0,
    provider     TEXT        NOT NULL DEFAULT '',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, tf, ts)
);

CREATE INDEX IF NOT EXISTS market_ohlcv_symbol_tf_ts_idx
    ON market_ohlcv (symbol, tf, ts DESC);

CREATE INDEX IF NOT EXISTS market_ohlcv_provider_idx
    ON market_ohlcv (provider);
