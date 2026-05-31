-- Per-home command unlocks and custom command slot tracking.
-- Home owners pay to unlock bot commands for their home thread.
-- Custom commands require purchased slots.

CREATE TABLE IF NOT EXISTS home_unlocked_commands (
    user_id     BIGINT      NOT NULL,
    guild_id    BIGINT      NOT NULL,
    command     TEXT        NOT NULL,
    unlocked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, command),
    CONSTRAINT fk_home_unlocked_home
        FOREIGN KEY (user_id, guild_id)
        REFERENCES player_homes (user_id, guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_home_unlocked_commands_home
    ON home_unlocked_commands (user_id, guild_id);

ALTER TABLE player_homes
    ADD COLUMN IF NOT EXISTS custom_cmd_slots INT NOT NULL DEFAULT 0;
