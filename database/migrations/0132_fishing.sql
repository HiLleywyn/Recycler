-- Fishing minigame: per-user state + append-only catch log.
--
-- Two tables:
--   user_fishing       -- one row per (guild, user); rod, bait, combo, totals
--   fishing_catches    -- append-only log; powers leaderboard + history
--
-- Bait inventory and the un-sold fish/junk inventory live as JSONB on
-- user_fishing so a single row covers everything the cog needs to
-- render the panel without a fan-out join. Counts are bounded by the
-- catalog max_stack values in fishing_config.py and validated server-
-- side before each insert; the JSONB shape is intentionally flexible
-- so adding a new fish / bait / junk type is a config-only change.
--
-- Cooldowns and combo-decay use DB-side clocks
-- (EXTRACT(EPOCH FROM (NOW() - last_cast_at))) per the project rule;
-- never compare Python now() to a Postgres timestamp.

CREATE TABLE IF NOT EXISTS user_fishing (
    guild_id           BIGINT       NOT NULL,
    user_id            BIGINT       NOT NULL,
    rod_tier           INTEGER      NOT NULL DEFAULT 0,
    equipped_bait      TEXT,                                 -- key from BAIT, NULL = no bait
    current_zone       TEXT         NOT NULL DEFAULT 'pond',
    bait_inventory     JSONB        NOT NULL DEFAULT '{}'::jsonb,   -- {"worm": 12, ...}
    fish_inventory     JSONB        NOT NULL DEFAULT '{}'::jsonb,   -- {"bass": [{"lbs": 4.2, "ts": 1234}], ...}
    junk_inventory     JSONB        NOT NULL DEFAULT '{}'::jsonb,   -- {"boot": 3, ...}
    total_caught       BIGINT       NOT NULL DEFAULT 0,
    total_junk         BIGINT       NOT NULL DEFAULT 0,
    total_weight_lbs   DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_payout_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,    -- lifetime $ from fishing
    biggest_fish       TEXT,                                  -- fish key
    biggest_lbs        DOUBLE PRECISION NOT NULL DEFAULT 0,
    biggest_caught_at  TIMESTAMPTZ,
    current_combo      INTEGER      NOT NULL DEFAULT 0,
    longest_combo      INTEGER      NOT NULL DEFAULT 0,
    fish_xp            BIGINT       NOT NULL DEFAULT 0,
    fish_level         INTEGER      NOT NULL DEFAULT 1,
    last_cast_at       TIMESTAMPTZ,
    last_buddy_egg_at  TIMESTAMPTZ,                           -- for BUDDY_EGG_DAILY_CAP
    buddy_eggs_today   INTEGER      NOT NULL DEFAULT 0,
    is_casting         BOOLEAN      NOT NULL DEFAULT FALSE,   -- soft lock against double-cast
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id),
    CONSTRAINT user_fishing_rod_tier_chk    CHECK (rod_tier >= 0),
    CONSTRAINT user_fishing_level_chk       CHECK (fish_level >= 1),
    CONSTRAINT user_fishing_combo_chk       CHECK (current_combo >= 0 AND longest_combo >= 0),
    CONSTRAINT user_fishing_buddy_cap_chk   CHECK (buddy_eggs_today >= 0)
);

CREATE INDEX IF NOT EXISTS user_fishing_guild_idx
    ON user_fishing (guild_id);

-- Leaderboard helper: top fishers by lifetime payout, fastest path.
CREATE INDEX IF NOT EXISTS user_fishing_payout_idx
    ON user_fishing (guild_id, total_payout_raw DESC);

-- Leaderboard helper: top trophy holders (biggest_lbs).
CREATE INDEX IF NOT EXISTS user_fishing_biggest_idx
    ON user_fishing (guild_id, biggest_lbs DESC NULLS LAST)
    WHERE biggest_fish IS NOT NULL;


-- Append-only catch log. One row per successful pull (fish, junk, or
-- bonus). Powers the global "biggest catch ever" leaderboard, the
-- player's own history pages, and any future season-based metrics.
CREATE TABLE IF NOT EXISTS fishing_catches (
    catch_id      BIGSERIAL    PRIMARY KEY,
    guild_id      BIGINT       NOT NULL,
    user_id       BIGINT       NOT NULL,
    outcome       TEXT         NOT NULL,                     -- 'fish' | 'junk' | 'money_bag' | 'mystery_box' | 'buddy_egg'
    fish_key      TEXT,                                       -- when outcome='fish'
    rarity        TEXT,                                       -- mirrored from FISH catalog for fast leaderboards
    junk_key      TEXT,                                       -- when outcome='junk'
    weight_lbs    DOUBLE PRECISION,                          -- only for fish
    payout_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,         -- USD value awarded immediately (money_bag, mystery_box)
    quality_mult  DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    combo_mult    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    zone          TEXT,
    rod_tier      INTEGER      NOT NULL DEFAULT 0,
    bait_key      TEXT,
    caught_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT fishing_catches_outcome_chk CHECK (
        outcome IN ('fish', 'junk', 'money_bag', 'mystery_box', 'buddy_egg')
    )
);

CREATE INDEX IF NOT EXISTS fishing_catches_user_idx
    ON fishing_catches (guild_id, user_id, caught_at DESC);

-- Global "biggest fish" board.
CREATE INDEX IF NOT EXISTS fishing_catches_biggest_idx
    ON fishing_catches (guild_id, weight_lbs DESC NULLS LAST)
    WHERE outcome = 'fish';

-- Rare-pull splash feed (rare/epic/legendary) -- used by services/fishing.py
-- to find recent trophies for the events stream.
CREATE INDEX IF NOT EXISTS fishing_catches_splash_idx
    ON fishing_catches (guild_id, caught_at DESC)
    WHERE rarity IN ('rare', 'epic', 'legendary');
