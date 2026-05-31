-- Create item tables for gambastones, gambling saves, validator guards, yield guards.
CREATE TABLE IF NOT EXISTS gambastones (
    user_id     BIGINT        NOT NULL,
    guild_id    BIGINT        NOT NULL,
    level       INTEGER       NOT NULL DEFAULT 1,
    xp          NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_sun  NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    acquired_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_gambastones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_gambastones_level CHECK (level >= 1),
    CONSTRAINT chk_gambastones_xp    CHECK (xp >= 0)
);

CREATE TABLE IF NOT EXISTS gambling_save_inventory (
    user_id  BIGINT  NOT NULL,
    guild_id BIGINT  NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_gambling_save_inv_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_gambling_save_count CHECK (count >= 0)
);

CREATE TABLE IF NOT EXISTS validator_guard_inventory (
    user_id  BIGINT  NOT NULL,
    guild_id BIGINT  NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_validator_guard_inv_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_validator_guard_count CHECK (count >= 0)
);

CREATE TABLE IF NOT EXISTS yield_guard_inventory (
    user_id  BIGINT  NOT NULL,
    guild_id BIGINT  NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_yield_guard_inv_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_yield_guard_count CHECK (count >= 0)
);
