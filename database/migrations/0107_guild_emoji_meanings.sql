-- Per-guild custom emoji meaning index.
--
-- The AI chat system prompt previously showed custom emojis with only a static
-- tone tag ([loss]/[win]/[hype]/...) derived from substring matching the
-- emoji name. That's too shallow to capture the nuance of a given server's
-- emoji palette, so the bot ends up describing them generically.
--
-- This migration adds two tables:
--
--   guild_emoji_meanings   stores an LLM-produced per-emoji description
--                          (derived from vision + recent usage samples) that
--                          the system prompt can surface to the chat model.
--
--   guild_emoji_usage      captures short snippets around each time a custom
--                          emoji gets used in chat, so the re-indexer can
--                          feed real usage context back into the description
--                          ("usually sent after a rug pull", etc.). Pruned on
--                          a rolling window.

CREATE TABLE IF NOT EXISTS guild_emoji_meanings (
    guild_id    BIGINT       NOT NULL,
    emoji_id    BIGINT       NOT NULL,
    name        TEXT         NOT NULL,
    animated    BOOLEAN      NOT NULL DEFAULT FALSE,
    description TEXT         NOT NULL,
    category    TEXT,
    source      TEXT         NOT NULL DEFAULT 'vision',
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, emoji_id)
);

CREATE INDEX IF NOT EXISTS idx_guild_emoji_meanings_updated
    ON guild_emoji_meanings (guild_id, updated_at);

CREATE TABLE IF NOT EXISTS guild_emoji_usage (
    id         BIGSERIAL    PRIMARY KEY,
    guild_id   BIGINT       NOT NULL,
    emoji_id   BIGINT       NOT NULL,
    user_id    BIGINT       NOT NULL,
    snippet    TEXT         NOT NULL,
    ts         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_guild_emoji_usage_lookup
    ON guild_emoji_usage (guild_id, emoji_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_guild_emoji_usage_ts
    ON guild_emoji_usage (ts);
