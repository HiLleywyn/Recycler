-- 0293_clank_escape_message_id.sql
-- Add message_id so each clanker's escape-room post can be found inside the shared thread.
ALTER TABLE clank_escape ADD COLUMN IF NOT EXISTS message_id BIGINT;
