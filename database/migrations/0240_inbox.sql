-- V3 Pillar 5: Inbox / persistent notifications
--
-- Market events DM you once, raid notifications fly past in a busy
-- channel, season-end is announced in #general and gone. The inbox is
-- a durable per-user log of every notification-worthy event, so a
-- player can run ,inbox at any time and see what they missed.
--
-- payload_json carries optional per-category metadata (raid tx hash,
-- season number, market event id, etc.) so a "View detail" button
-- can re-render the original context.

CREATE TABLE IF NOT EXISTS user_inbox (
    id           BIGSERIAL    PRIMARY KEY,
    user_id      BIGINT       NOT NULL,
    guild_id     BIGINT,
    category     TEXT         NOT NULL,
    title        TEXT         NOT NULL,
    body         TEXT         NOT NULL,
    severity     TEXT         NOT NULL DEFAULT 'info'
                              CHECK (severity IN ('info', 'success', 'warning', 'error', 'critical')),
    payload_json JSONB,
    posted_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    read_at      TIMESTAMPTZ,
    dm_sent      BOOLEAN      NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS user_inbox_user_unread_idx
    ON user_inbox (user_id, posted_at DESC)
    WHERE read_at IS NULL;

CREATE INDEX IF NOT EXISTS user_inbox_user_full_idx
    ON user_inbox (user_id, posted_at DESC);

CREATE INDEX IF NOT EXISTS user_inbox_category_idx
    ON user_inbox (user_id, category, posted_at DESC);

CREATE TABLE IF NOT EXISTS user_inbox_prefs (
    user_id       BIGINT       NOT NULL,
    category      TEXT         NOT NULL,
    dm_enabled    BOOLEAN      NOT NULL DEFAULT false,
    PRIMARY KEY (user_id, category)
);
