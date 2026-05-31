-- 0270_market_watchlist.sql
--
-- Per-user real-market watchlist for the $watch command. Each row is one
-- symbol the user wants notified about. ``target_price`` and ``direction``
-- (``above`` / ``below``) make the row an active alert; when both are
-- NULL the row is just a passive watchlist entry shown in $watch list.

CREATE TABLE IF NOT EXISTS market_watchlist (
    id            BIGSERIAL    PRIMARY KEY,
    user_id       BIGINT       NOT NULL,
    guild_id      BIGINT       NOT NULL,
    symbol        TEXT         NOT NULL,
    asset_class   TEXT         NOT NULL DEFAULT 'crypto',
    target_price  DOUBLE PRECISION,
    direction     TEXT,
    notify_channel BIGINT,
    triggered_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, guild_id, symbol, target_price, direction)
);

CREATE INDEX IF NOT EXISTS market_watchlist_user_idx
    ON market_watchlist (user_id, guild_id);

CREATE INDEX IF NOT EXISTS market_watchlist_active_idx
    ON market_watchlist (symbol, triggered_at)
    WHERE target_price IS NOT NULL AND triggered_at IS NULL;
