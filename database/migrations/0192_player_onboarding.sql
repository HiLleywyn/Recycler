-- Player onboarding: welcome DM dedup + per-module first-touch tracking.
--
-- welcomed_users
--   One row per Discord user who has received the introduction DM. Keyed on
--   user_id alone (not (user_id, guild_id)) because the welcome message is
--   sent on first interaction across the entire bot, not first interaction
--   in a guild -- a player joining a second guild should not be DM'd again.
--
-- user_module_seen
--   Tracks the first time a user has touched a major game / module (farming,
--   fishing, delve, mining, gambling, buddy, etc). Used to fire one-shot
--   "first time playing" intro hints from Disco. Also user-only so the
--   intro doesn't repeat across guilds for the same player.

CREATE TABLE IF NOT EXISTS welcomed_users (
    user_id  BIGINT       PRIMARY KEY,
    sent_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_module_seen (
    user_id        BIGINT       NOT NULL,
    module         TEXT         NOT NULL,
    first_seen_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, module)
);

CREATE INDEX IF NOT EXISTS idx_user_module_seen_user
    ON user_module_seen (user_id);
