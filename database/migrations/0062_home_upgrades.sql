-- 0062_home_upgrades.sql
-- Player home enhancements: rent, per-home auto-delete, custom commands,
-- mood emoji, and welcome message.

ALTER TABLE player_homes
    ADD COLUMN IF NOT EXISTS rent_amount         NUMERIC(28,8) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rent_last_collected TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS auto_delete_after   INTEGER,       -- seconds; NULL = off
    ADD COLUMN IF NOT EXISTS mood_emoji          TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS welcome_msg         TEXT NOT NULL DEFAULT '';

-- Per-home custom trigger -> response commands
CREATE TABLE IF NOT EXISTS home_commands (
    user_id    BIGINT NOT NULL,
    guild_id   BIGINT NOT NULL,
    trigger    TEXT   NOT NULL,
    response   TEXT   NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, trigger),
    CONSTRAINT fk_home_commands_home
        FOREIGN KEY (user_id, guild_id)
        REFERENCES player_homes (user_id, guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_home_commands_guild
    ON home_commands (guild_id);
