-- Persistence for ,admin pump and the auto-pump scheduler.
-- Pre-0210 these events lived only in cogs/trade.py::_admin_price_events
-- (an in-memory dict), so a bot restart silently dropped every live pump
-- and the chart froze wherever the last drift tick had landed it.
-- This table mirrors the dict so drift_task can rehydrate on startup.

CREATE TABLE IF NOT EXISTS admin_price_events (
    guild_id        BIGINT          NOT NULL,
    symbol          VARCHAR(16)     NOT NULL,
    pattern         VARCHAR(32)     NOT NULL,
    magnitude_pct   DOUBLE PRECISION NOT NULL,
    seed            BIGINT          NOT NULL,
    start_price     DOUBLE PRECISION NOT NULL,
    start_ts        DOUBLE PRECISION NOT NULL,  -- epoch seconds
    end_ts          DOUBLE PRECISION NOT NULL,  -- epoch seconds
    PRIMARY KEY (guild_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_admin_price_events_end_ts
    ON admin_price_events (end_ts);
