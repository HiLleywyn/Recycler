-- ============================================================================
-- 0059: Player Homes
-- Per-player private thread ("home") purchased via the shop.
-- Each player can own one home per guild. The bot creates a Discord thread
-- and mediates faux permissions (invite, kick, lock, rename) on their behalf.
-- ============================================================================

-- Parent channel where home threads are spawned (set by admins per guild)
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS home_channel BIGINT;

-- One home per player per guild
CREATE TABLE IF NOT EXISTS player_homes (
    user_id      BIGINT        NOT NULL,
    guild_id     BIGINT        NOT NULL,
    thread_id    BIGINT        NOT NULL,
    channel_id   BIGINT        NOT NULL,
    home_name    TEXT          NOT NULL DEFAULT 'My Home',
    description  TEXT          NOT NULL DEFAULT '',
    is_locked    BOOLEAN       NOT NULL DEFAULT FALSE,
    purchased_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_player_homes_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_player_homes_guild
    ON player_homes (guild_id);

CREATE INDEX IF NOT EXISTS idx_player_homes_thread
    ON player_homes (thread_id);
