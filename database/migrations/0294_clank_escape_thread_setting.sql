-- 0294_clank_escape_thread_setting.sql
-- Runtime override for the shared escape-room thread, settable via ,clanker er setthread
-- without redeploying. Falls back to the CLANK_ESCAPE_THREAD_ID env var when unset/0.
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS clank_escape_thread BIGINT;
