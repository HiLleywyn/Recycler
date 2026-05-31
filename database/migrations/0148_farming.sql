-- Farming minigame: per-user state, append-only harvest log, pest battles.
--
-- Three tables:
--   user_farming         -- one row per (guild, user); zone, plots, inventories, totals
--   farming_harvests     -- append-only harvest log; powers history + leaderboards
--   farming_pest_battles -- append-only pest battle log; powers achievements + stats
--
-- Plot state, crop / processed / fertilizer / seed-packet inventories live as
-- JSONB on user_farming so a single row covers everything the cog needs to
-- render the farm panel without a fan-out join. Counts are bounded by the
-- catalog max_stack values defined in the farming config and validated
-- server-side before each insert; the JSONB shape is intentionally flexible
-- so adding a new crop / fertilizer / recipe is a config-only change.
--
-- Active growth state (plots[*].planted_at, plots[*].crop_key, weather_until)
-- is denormalized onto user_farming for cheap reads between actions.
--
-- Cooldowns and seed-stake yield ticks use DB-side clocks
-- (EXTRACT(EPOCH FROM (NOW() - last_stake_yield_at))) per the project
-- rule; never compare Python now() to a Postgres timestamp.

CREATE TABLE IF NOT EXISTS user_farming (
    guild_id                 BIGINT       NOT NULL,
    user_id                  BIGINT       NOT NULL,
    current_zone             TEXT         NOT NULL DEFAULT 'meadow',          -- starter zone
    equipped_fertilizer      TEXT,                                             -- key from FERTILIZERS, NULL = none
    plot_tier                INTEGER      NOT NULL DEFAULT 1,                  -- tier 1 free; max tier 9
    plot_count               INTEGER      NOT NULL DEFAULT 4,                  -- unlocked plot slots
    plots                    JSONB        NOT NULL DEFAULT '[]'::jsonb,        -- list of plot objects
    crop_inventory           JSONB        NOT NULL DEFAULT '{}'::jsonb,        -- {crop_key: count}
    processed_inventory      JSONB        NOT NULL DEFAULT '{}'::jsonb,        -- {recipe_key: count}
    fertilizer_inventory     JSONB        NOT NULL DEFAULT '{}'::jsonb,        -- {fert_key: count}
    seed_packets             JSONB        NOT NULL DEFAULT '{}'::jsonb,        -- {crop_key: count}
    current_weather          TEXT         NOT NULL DEFAULT 'clear',            -- weather key
    weather_until            TIMESTAMPTZ,                                      -- DB clock
    total_planted            BIGINT       NOT NULL DEFAULT 0,
    total_harvested          BIGINT       NOT NULL DEFAULT 0,
    total_crops_grown_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_seed_earned_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_hrv_earned_raw     NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_usd_cashout_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    biggest_harvest_crop     TEXT,                                             -- crop key
    biggest_harvest_qty      INTEGER      NOT NULL DEFAULT 0,
    biggest_harvest_at       TIMESTAMPTZ,
    -- Per-seed stake state (mirrors fishing's lure_staked_raw + last_stake_yield_at)
    seed_staked_raw          NUMERIC(36, 0) NOT NULL DEFAULT 0,
    hrv_yield_pending_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    last_stake_yield_at      TIMESTAMPTZ,
    last_action_at           TIMESTAMPTZ,
    last_plant_at            TIMESTAMPTZ,
    last_harvest_at          TIMESTAMPTZ,
    is_acting                BOOLEAN      NOT NULL DEFAULT FALSE,              -- soft lock against double-action
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id),
    CONSTRAINT user_farming_plot_tier_chk    CHECK (plot_tier BETWEEN 1 AND 9),
    CONSTRAINT user_farming_plot_count_chk   CHECK (plot_count >= 0 AND plot_count <= 50),
    CONSTRAINT user_farming_stake_chk        CHECK (seed_staked_raw >= 0 AND hrv_yield_pending_raw >= 0),
    CONSTRAINT user_farming_biggest_qty_chk  CHECK (biggest_harvest_qty >= 0)
);

-- Zone rollups: who's farming where right now.
CREATE INDEX IF NOT EXISTS user_farming_zone_idx
    ON user_farming (guild_id, current_zone);

-- Leaderboard helper: top farmers by lifetime HRV earned, fastest path.
CREATE INDEX IF NOT EXISTS user_farming_payout_idx
    ON user_farming (guild_id, total_hrv_earned_raw DESC);

-- Leaderboard helper: top trophy holders (biggest_harvest_qty).
CREATE INDEX IF NOT EXISTS user_farming_biggest_idx
    ON user_farming (guild_id, biggest_harvest_qty DESC NULLS LAST)
    WHERE biggest_harvest_crop IS NOT NULL;


-- Append-only harvest log. One row per successful harvest (any crop, any
-- rarity). Powers the global "biggest harvest ever" leaderboard, the
-- player's own history pages, and any future season-based metrics.
CREATE TABLE IF NOT EXISTS farming_harvests (
    harvest_id        BIGSERIAL    PRIMARY KEY,
    guild_id          BIGINT       NOT NULL,
    user_id           BIGINT       NOT NULL,
    crop_key          TEXT         NOT NULL,
    rarity            TEXT         NOT NULL DEFAULT 'common',                  -- mirrored from CROPS catalog for fast leaderboards
    qty               INTEGER      NOT NULL DEFAULT 0,
    seed_earned_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    hrv_earned_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    zone              TEXT         NOT NULL,
    plot_tier         INTEGER      NOT NULL DEFAULT 1,
    fertilizer_key    TEXT,                                                    -- NULL if no fertilizer was applied
    weather           TEXT         NOT NULL DEFAULT 'clear',
    harvested_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Per-user history page: most recent harvests first.
CREATE INDEX IF NOT EXISTS farming_harvests_user_idx
    ON farming_harvests (guild_id, user_id, harvested_at DESC);

-- Global "biggest harvest" board.
CREATE INDEX IF NOT EXISTS farming_harvests_biggest_idx
    ON farming_harvests (guild_id, qty DESC NULLS LAST)
    WHERE qty > 0;

-- Rare-harvest splash feed (rare/epic/legendary) -- used by services/farming.py
-- to find recent trophy harvests for the events stream.
CREATE INDEX IF NOT EXISTS farming_harvests_splash_idx
    ON farming_harvests (guild_id, harvested_at DESC)
    WHERE rarity IN ('rare', 'epic', 'legendary');


-- Append-only pest battle log. One row per encountered pest (defeated,
-- captured, or fled). Powers achievements ("defeat 100 locusts"), pest
-- bestiary completion stats, and per-zone encounter analytics.
CREATE TABLE IF NOT EXISTS farming_pest_battles (
    battle_id       BIGSERIAL    PRIMARY KEY,
    guild_id        BIGINT       NOT NULL,
    user_id         BIGINT       NOT NULL,
    pest_key        TEXT         NOT NULL,
    outcome         TEXT         NOT NULL,                                     -- 'defeated' | 'captured' | 'fled' | 'lost'
    captured        BOOLEAN      NOT NULL DEFAULT FALSE,
    seed_drop_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    zone            TEXT         NOT NULL,
    fought_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Per-user pest battle feed: most recent battles first.
CREATE INDEX IF NOT EXISTS farming_pest_battles_user_idx
    ON farming_pest_battles (guild_id, user_id, fought_at DESC);
