-- V3 Pillar 6: Apex Events (cross-system world events)
--
-- Existing cogs/events.py drives market-only events (pump / dump /
-- phase transitions). Apex Events sit one layer above: each event
-- injects MODIFIERS into multiple disciplines at once. A Solar Flare
-- might boost mining hashrate +50% AND penalise fishing -20% AND
-- raise dungeon mob damage +25%. Consumers in each system read the
-- relevant modifier via services.apex_events.modifier(key) and apply
-- it via the same one-line helper everywhere.

CREATE TABLE IF NOT EXISTS apex_events_active (
    id            BIGSERIAL    PRIMARY KEY,
    guild_id      BIGINT       NOT NULL,
    event_id      TEXT         NOT NULL,    -- references apex_events_config.EVENTS
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    ends_at       TIMESTAMPTZ  NOT NULL,
    modifiers     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    announced     BOOLEAN      NOT NULL DEFAULT false,
    UNIQUE (guild_id, event_id, started_at)
);

CREATE INDEX IF NOT EXISTS apex_events_active_window_idx
    ON apex_events_active (guild_id, ends_at);

CREATE TABLE IF NOT EXISTS apex_events_history (
    id            BIGSERIAL    PRIMARY KEY,
    guild_id      BIGINT       NOT NULL,
    event_id      TEXT         NOT NULL,
    started_at    TIMESTAMPTZ  NOT NULL,
    ended_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    modifiers     JSONB        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS apex_events_history_guild_idx
    ON apex_events_history (guild_id, started_at DESC);
