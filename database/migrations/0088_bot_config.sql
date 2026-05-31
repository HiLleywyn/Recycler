-- Bot-wide key/value config store used for runtime-configurable settings
-- that must survive restarts (e.g. report DM recipient override).
CREATE TABLE IF NOT EXISTS bot_config (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
