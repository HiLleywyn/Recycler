-- Meta-economy themed stones: stones that level up off cross-cutting bot
-- systems rather than a single minigame surface.
--
--   gavelstones  -- auction house: XP from buys + settled sales (NOT from
--                   creating a listing). Pays buyer rebates + seller
--                   bonuses scaling with level.
--   anvilstones  -- crafting: XP from each ,craft action (regardless of
--                   qty). Boosts per-craft output qty.
--   chimerastones -- AMM swaps via ,swap / ,trade swap. Stacks on top of
--                   the existing Liqstone swap-fee discount.
--
-- Same row shape as the existing themed stones (tide / heart / crypt /
-- blood / bloom from migrations 0146 + 0150): one row per
-- (user_id, guild_id) with staked stable, level, XP, lp_currency,
-- acquired_at. All three are USD-priced (cost_stable in items_config.py
-- is the USD figure; the buy / level-up flow already knows how to debit
-- the bare USD wallet when accepted_currencies = ('USD',)).

CREATE TABLE IF NOT EXISTS gavelstones (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    level          INTEGER       NOT NULL DEFAULT 1,
    xp             NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    lp_currency    TEXT          NOT NULL DEFAULT 'USD',
    acquired_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_gavelstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_gavelstones_level CHECK (level >= 1),
    CONSTRAINT chk_gavelstones_xp    CHECK (xp >= 0)
);

CREATE TABLE IF NOT EXISTS anvilstones (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    level          INTEGER       NOT NULL DEFAULT 1,
    xp             NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    lp_currency    TEXT          NOT NULL DEFAULT 'USD',
    acquired_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_anvilstones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_anvilstones_level CHECK (level >= 1),
    CONSTRAINT chk_anvilstones_xp    CHECK (xp >= 0)
);

CREATE TABLE IF NOT EXISTS chimerastones (
    user_id        BIGINT        NOT NULL,
    guild_id       BIGINT        NOT NULL,
    level          INTEGER       NOT NULL DEFAULT 1,
    xp             NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    staked_amount  NUMERIC(36,0) NOT NULL DEFAULT 0,
    lp_currency    TEXT          NOT NULL DEFAULT 'USD',
    acquired_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_chimerastones_user FOREIGN KEY (user_id, guild_id)
        REFERENCES users(user_id, guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_chimerastones_level CHECK (level >= 1),
    CONSTRAINT chk_chimerastones_xp    CHECK (xp >= 0)
);
