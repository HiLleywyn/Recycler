-- Add ai_chat_channels column: per-guild allowlist for ambient AI chatter.
-- Stores comma-separated channel IDs where Disco is allowed to post
-- unsolicited ambient crypto commentary. Empty = allow everywhere.
-- Reactive paths (,ask / @mention / reply-to-bot) are not affected by this
-- setting; they continue to work in any channel the bot can see.
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS ai_chat_channels TEXT NOT NULL DEFAULT '';
