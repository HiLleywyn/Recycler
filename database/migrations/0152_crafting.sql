-- Crafting minigame: per-user state, append-only craft log.
--
-- Two tables:
--   user_crafting     -- one row per (guild, user); level/XP, crafted-item
--                        inventory, INGOT stake state, lifetime totals
--   crafting_logs     -- append-only craft log; powers history + leaderboards
--
-- The crafted-item inventory lives as JSONB so adding a new recipe is a
-- config-only change in crafting_config.py. Counts are bounded by the
-- catalog max_stack values defined in CRAFT_ITEMS and validated server-side
-- before each insert.
--
-- INGOT stake / yield clocks use DB-side timestamps
-- (EXTRACT(EPOCH FROM (NOW() - last_stake_yield_at))) per the project rule;
-- never compare Python now() to a Postgres timestamp.

CREATE TABLE IF NOT EXISTS user_crafting (
    guild_id                 BIGINT       NOT NULL,
    user_id                  BIGINT       NOT NULL,
    crafting_level           INTEGER      NOT NULL DEFAULT 1,                  -- caps at 50
    crafting_xp              BIGINT       NOT NULL DEFAULT 0,
    crafted_inventory        JSONB        NOT NULL DEFAULT '{}'::jsonb,        -- {craft_key: count}
    total_crafts             BIGINT       NOT NULL DEFAULT 0,
    total_ingot_earned_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_forge_earned_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_usd_cashout_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    biggest_craft_key        TEXT,                                             -- recipe key
    biggest_craft_at         TIMESTAMPTZ,
    -- Per-INGOT stake state (mirrors farming's seed_staked_raw + last_stake_yield_at)
    ingot_staked_raw         NUMERIC(36, 0) NOT NULL DEFAULT 0,
    forge_yield_pending_raw  NUMERIC(36, 0) NOT NULL DEFAULT 0,
    last_stake_yield_at      TIMESTAMPTZ,
    last_craft_at            TIMESTAMPTZ,
    is_acting                BOOLEAN      NOT NULL DEFAULT FALSE,              -- soft lock against double-craft
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id),
    CONSTRAINT user_crafting_level_chk    CHECK (crafting_level BETWEEN 1 AND 50),
    CONSTRAINT user_crafting_xp_chk       CHECK (crafting_xp >= 0),
    CONSTRAINT user_crafting_stake_chk    CHECK (ingot_staked_raw >= 0 AND forge_yield_pending_raw >= 0)
);

-- Leaderboard helper: top crafters by lifetime FORGE earned.
CREATE INDEX IF NOT EXISTS user_crafting_payout_idx
    ON user_crafting (guild_id, total_forge_earned_raw DESC);

-- Leaderboard helper: top crafters by lifetime craft count.
CREATE INDEX IF NOT EXISTS user_crafting_count_idx
    ON user_crafting (guild_id, total_crafts DESC);


-- Append-only craft log. One row per successful ,craft make. Powers
-- the global "biggest craft" leaderboard, the player's own history pages,
-- and any future season-based metrics.
CREATE TABLE IF NOT EXISTS crafting_logs (
    log_id           BIGSERIAL    PRIMARY KEY,
    guild_id         BIGINT       NOT NULL,
    user_id          BIGINT       NOT NULL,
    craft_key        TEXT         NOT NULL,           -- recipe key from CRAFT_ITEMS
    qty              INTEGER      NOT NULL,           -- how many of the output were made
    rarity           TEXT,                            -- mirrored from recipe for cheap sort
    ingot_earned_raw NUMERIC(36, 0) NOT NULL DEFAULT 0,
    fgd_spent_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    crafted_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT crafting_logs_qty_chk CHECK (qty > 0)
);

CREATE INDEX IF NOT EXISTS crafting_logs_user_idx
    ON crafting_logs (guild_id, user_id, crafted_at DESC);

CREATE INDEX IF NOT EXISTS crafting_logs_recipe_idx
    ON crafting_logs (guild_id, craft_key, crafted_at DESC);

CREATE INDEX IF NOT EXISTS crafting_logs_payout_idx
    ON crafting_logs (guild_id, ingot_earned_raw DESC);
