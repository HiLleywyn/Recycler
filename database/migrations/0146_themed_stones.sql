-- Themed leveled stones for the four minigame surfaces:
--   tidestones   -- fishing (Tidestone)
--   heartstones  -- buddy companionship (Heartstone)
--   cryptstones  -- dungeon delving (Cryptstone)
--   bloodstones  -- buddy battles (Bloodstone)
--
-- Same row shape as the existing hashstones / lockstones / vaultstones /
-- liqstones / gambastones tables: one row per (user_id, guild_id) with
-- staked DSD, level, XP, and a stored lp_currency tag (so cross-currency
-- payouts can be converted at sell time the same way the older stones
-- already do via migration 0069). Generic CRUD lives in
-- database/users.py::_get_stone / _create_stone / _add_stone_xp_delta /
-- etc., which take the table name as a parameter -- the new tables drop
-- straight into that machinery without a code change beyond the per-
-- stone wrapper methods (added alongside this migration).

CREATE TABLE IF NOT EXISTS tidestones (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    level          INTEGER       NOT NULL DEFAULT 1,
    xp             NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    lp_currency    TEXT          NOT NULL DEFAULT 'DSD',
    acquired_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_tidestones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_tidestones_level CHECK (level >= 1),
    CONSTRAINT chk_tidestones_xp    CHECK (xp >= 0)
);

CREATE TABLE IF NOT EXISTS heartstones (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    level          INTEGER       NOT NULL DEFAULT 1,
    xp             NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    lp_currency    TEXT          NOT NULL DEFAULT 'DSD',
    acquired_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_heartstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_heartstones_level CHECK (level >= 1),
    CONSTRAINT chk_heartstones_xp    CHECK (xp >= 0)
);

CREATE TABLE IF NOT EXISTS cryptstones (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    level          INTEGER       NOT NULL DEFAULT 1,
    xp             NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    lp_currency    TEXT          NOT NULL DEFAULT 'DSD',
    acquired_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_cryptstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_cryptstones_level CHECK (level >= 1),
    CONSTRAINT chk_cryptstones_xp    CHECK (xp >= 0)
);

CREATE TABLE IF NOT EXISTS bloodstones (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    level          INTEGER       NOT NULL DEFAULT 1,
    xp             NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    lp_currency    TEXT          NOT NULL DEFAULT 'DSD',
    acquired_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_bloodstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_bloodstones_level CHECK (level >= 1),
    CONSTRAINT chk_bloodstones_xp    CHECK (xp >= 0)
);
