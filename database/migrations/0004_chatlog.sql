-- Chatlog archives: a saved copy of a channel's recent messages that can be
-- restored into a channel via webhooks (separate from full server backups).
CREATE TABLE IF NOT EXISTS chatlogs (
    id            TEXT PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    guild_id      BIGINT NOT NULL,
    channel_id    BIGINT,
    channel_name  TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    data          JSONB NOT NULL,                         -- ordered messages
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chatlogs_owner ON chatlogs(owner_id);
CREATE INDEX IF NOT EXISTS idx_chatlogs_guild ON chatlogs(guild_id);
