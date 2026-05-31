-- Migration 0071: AI memory refresh system - tool memory, reaction memory, refresh tracking.
-- Adds per-user tool activation tracking, emoji reaction pattern storage, and
-- refresh timestamp columns so the memory refresh loop can decide when to re-summarize.

-- ── ai_tool_memory: per-user tool activation frequency ───────────────────────
-- Tracks which economy tools (mining, defi, trading, etc.) each user interacts with
-- most so the AI can tailor its context injections based on proven engagement patterns.
CREATE TABLE IF NOT EXISTS ai_tool_memory (
    user_id     BIGINT          NOT NULL,
    guild_id    BIGINT          NOT NULL,
    tool_key    TEXT            NOT NULL,
    use_count   INTEGER         NOT NULL DEFAULT 1,
    last_used   TIMESTAMPTZ     NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, tool_key)
);
CREATE INDEX IF NOT EXISTS idx_ai_tool_mem_user ON ai_tool_memory (user_id, guild_id);

-- ── ai_reaction_memory: per-user emoji reaction category patterns ─────────────
-- Stores how often each user reacts with each emotional category (loss, win, hype,
-- frustration, etc.) so the AI builds a picture of each player's personality.
CREATE TABLE IF NOT EXISTS ai_reaction_memory (
    user_id     BIGINT          NOT NULL,
    guild_id    BIGINT          NOT NULL,
    category    TEXT            NOT NULL,
    use_count   INTEGER         NOT NULL DEFAULT 1,
    last_used   TIMESTAMPTZ     NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, category)
);
CREATE INDEX IF NOT EXISTS idx_ai_react_mem_user ON ai_reaction_memory (user_id, guild_id);

-- ── ai_user_memory: add refresh tracking columns ─────────────────────────────
ALTER TABLE ai_user_memory ADD COLUMN IF NOT EXISTS last_refreshed_at TIMESTAMPTZ;
ALTER TABLE ai_user_memory ADD COLUMN IF NOT EXISTS refresh_count INTEGER NOT NULL DEFAULT 0;
