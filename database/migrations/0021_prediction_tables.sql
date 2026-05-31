-- Prediction markets (polymarket-style betting)

CREATE TABLE IF NOT EXISTS prediction_markets (
    id              SERIAL PRIMARY KEY,
    guild_id        BIGINT NOT NULL,
    question        TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'general',
    options         JSONB NOT NULL DEFAULT '["YES","NO"]',
    end_time        TIMESTAMPTZ NOT NULL,
    resolved_option TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    total_pool      NUMERIC(20,2) NOT NULL DEFAULT 0,
    created_by      BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pred_markets_guild ON prediction_markets(guild_id, status);

CREATE TABLE IF NOT EXISTS prediction_bets (
    id          SERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    market_id   INT NOT NULL REFERENCES prediction_markets(id),
    user_id     BIGINT NOT NULL,
    option      TEXT NOT NULL,
    amount      NUMERIC(20,2) NOT NULL,
    placed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pred_bets_market ON prediction_bets(market_id);
CREATE INDEX IF NOT EXISTS idx_pred_bets_user ON prediction_bets(user_id, guild_id);
