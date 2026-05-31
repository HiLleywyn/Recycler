-- 0084: Player warnings table for admin moderation
CREATE TABLE IF NOT EXISTS player_warnings (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    admin_id    BIGINT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS player_warnings_guild_user ON player_warnings (guild_id, user_id);
