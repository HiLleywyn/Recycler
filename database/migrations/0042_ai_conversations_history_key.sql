-- Add history_key column to ai_conversations for per-agent history separation
ALTER TABLE ai_conversations
    ADD COLUMN IF NOT EXISTS history_key TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_ai_conv_key
    ON ai_conversations (user_id, guild_id, history_key, ts DESC);
