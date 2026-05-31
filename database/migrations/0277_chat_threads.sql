-- 0277_chat_threads.sql
--
-- Thread-based conversational AI plus a save/recall ("memory") system.
--
-- Every @mention or reply that triggers Disco's chat AI now spawns a
-- Discord thread off the user's message; the reply and the rest of the
-- back-and-forth live inside that thread instead of the general channel,
-- which keeps busy channels clean.
--
-- chat_threads tracks one row per AI thread. history_key links the thread
-- to its isolated ai_conversations rows ('thread:<thread_id>'). token is
-- NULL until a user "saves" the thread, after which it holds an 8-char
-- lowercase-alphanumeric recall code. Idle threads (no activity for 12h)
-- are deleted by a background loop; the DB-side clock keeps container
-- skew irrelevant. Deleting an unsaved thread also drops its
-- ai_conversations rows; saved threads keep their transcript forever.

CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id         BIGINT       PRIMARY KEY,
    guild_id          BIGINT       NOT NULL,
    owner_id          BIGINT       NOT NULL,
    parent_channel_id BIGINT       NOT NULL,
    history_key       TEXT         NOT NULL,
    token             TEXT,
    title             TEXT         NOT NULL DEFAULT 'AI chat',
    saved             BOOLEAN      NOT NULL DEFAULT FALSE,
    summary           TEXT,
    status            TEXT         NOT NULL DEFAULT 'active',
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_activity     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    closed_at         TIMESTAMPTZ,
    CONSTRAINT chk_chat_thread_status CHECK (status IN ('active', 'deleted'))
);

-- Recall by code: one global-unique token per saved thread.
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_threads_token
    ON chat_threads (token) WHERE token IS NOT NULL;

-- The idle-deletion loop scans active threads by last activity.
CREATE INDEX IF NOT EXISTS idx_chat_threads_idle
    ON chat_threads (last_activity) WHERE status = 'active';

-- ,thread list filters a guild's saved threads.
CREATE INDEX IF NOT EXISTS idx_chat_threads_guild
    ON chat_threads (guild_id, status);

-- get_thread_conversation() filters ai_conversations by (guild_id,
-- history_key) only; the legacy idx_ai_conv_key leads with user_id and
-- cannot serve that lookup.
CREATE INDEX IF NOT EXISTS idx_ai_conv_thread
    ON ai_conversations (guild_id, history_key, ts DESC);

-- Per-guild kill switch. Default TRUE: threaded chat is the new normal;
-- a guild can flip it off to restore inline replies.
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS ai_chat_threaded BOOLEAN NOT NULL DEFAULT TRUE;
