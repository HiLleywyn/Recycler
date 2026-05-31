-- Migration 0041: Add server_events and channel_context tables for AI gossip context.
-- These tables were added to schema.sql in the social-context/AI-gossip feature but
-- no migration was created, so existing databases (including staging) are missing them.
-- Also widens ai_user_memory.memory from VARCHAR to TEXT for richer context storage.

CREATE TABLE IF NOT EXISTS server_events (
    id          BIGSERIAL       PRIMARY KEY,
    guild_id    BIGINT          NOT NULL,
    channel_id  BIGINT,
    user_id     BIGINT          NOT NULL,
    event_type  TEXT            NOT NULL,
    summary     TEXT            NOT NULL,
    amount      NUMERIC(28,8)   DEFAULT 0,
    metadata    JSONB           DEFAULT '{}',
    ts          TIMESTAMPTZ     NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_server_events_guild ON server_events (guild_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_server_events_user  ON server_events (user_id, guild_id, ts DESC);

CREATE TABLE IF NOT EXISTS channel_context (
    id              BIGSERIAL       PRIMARY KEY,
    guild_id        BIGINT          NOT NULL,
    channel_id      BIGINT          NOT NULL,
    user_id         BIGINT          NOT NULL,
    event_type      TEXT            NOT NULL,
    content         TEXT            NOT NULL DEFAULT '',
    target_user_id  BIGINT,
    metadata        JSONB           DEFAULT '{}',
    ts              TIMESTAMPTZ     NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_channel_ctx_guild ON channel_context (guild_id, channel_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_channel_ctx_user  ON channel_context (user_id, guild_id, ts DESC);

-- Widen ai_user_memory to support richer context (up to 500 chars).
-- ALTER TYPE is safe to run multiple times on TEXT columns (no-op if already TEXT).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ai_user_memory'
          AND column_name = 'memory'
          AND data_type != 'text'
    ) THEN
        ALTER TABLE ai_user_memory ALTER COLUMN memory TYPE TEXT;
    END IF;
END $$;
