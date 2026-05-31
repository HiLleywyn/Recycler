-- Disco command group: per-user reply-mode preference + saved AI messages.
--
-- disco_user_prefs   -- whether Disco answers a member inline (chat) or in a
--                       thread. Only members who have unlocked the ,disco
--                       group (boost / level 50 / staff) can change it; the
--                       default 'thread' is the native behaviour everyone
--                       else keeps.
-- disco_saved_messages -- a member's personally bookmarked Disco answers, with
--                       the question that triggered them and a jump link.

CREATE TABLE IF NOT EXISTS disco_user_prefs (
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL,
    reply_mode TEXT        NOT NULL DEFAULT 'thread',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS disco_saved_messages (
    id                 BIGSERIAL   PRIMARY KEY,
    user_id            BIGINT      NOT NULL,
    guild_id           BIGINT      NOT NULL,
    channel_id         BIGINT      NOT NULL,
    disco_message_id   BIGINT      NOT NULL,
    trigger_message_id BIGINT,
    prompt_text        TEXT        NOT NULL DEFAULT '',
    response_text      TEXT        NOT NULL DEFAULT '',
    jump_url           TEXT,
    saved_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_disco_saved_user
    ON disco_saved_messages (user_id, guild_id, saved_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_disco_saved_unique
    ON disco_saved_messages (user_id, guild_id, disco_message_id);
