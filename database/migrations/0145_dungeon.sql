-- Delve dungeon crawler: per-user state, captured buddies, run log, kill log.
--
-- Four tables:
--   user_dungeon     -- one row per (guild, user); class, gear, totals, active run state
--   dungeon_party    -- captured mob buddies owned by a player
--   dungeon_runs     -- append-only run log; powers run history + leaderboards
--   dungeon_kills    -- append-only kill log; powers achievements + bestiary stats
--
-- Inventories (consumables, weapons_owned, armor_owned) live as JSONB on
-- user_dungeon so a single row covers everything the cog needs to render
-- the dungeon panel without a fan-out join. Counts are bounded by catalog
-- max_stack values defined in the dungeon config and validated server-
-- side before each insert; the JSONB shape is intentionally flexible so
-- adding a new weapon / armor / consumable type is a config-only change.
--
-- Active run state (run_id, current_floor, current_room, current_mob_state,
-- current_room_payload) is denormalized onto user_dungeon for cheap reads
-- between actions; run_id also FKs into dungeon_runs for the audit trail.
--
-- Cooldowns and ore-stake yield ticks use DB-side clocks
-- (EXTRACT(EPOCH FROM (NOW() - last_stake_yield_at))) per the project
-- rule; never compare Python now() to a Postgres timestamp.

CREATE TABLE IF NOT EXISTS user_dungeon (
    guild_id                 BIGINT       NOT NULL,
    user_id                  BIGINT       NOT NULL,
    class_key                TEXT,                                          -- 'warrior' | 'mage' | 'rogue' | NULL until chosen
    level                    INTEGER      NOT NULL DEFAULT 1,
    xp                       BIGINT       NOT NULL DEFAULT 0,
    hp_max                   INTEGER      NOT NULL DEFAULT 30,
    current_hp               INTEGER      NOT NULL DEFAULT 30,
    equipped_weapon          TEXT         NOT NULL DEFAULT 'rusty_dagger',
    equipped_armor           TEXT         NOT NULL DEFAULT 'cloth_tunic',
    active_buddy_id          BIGINT,                                        -- FK to dungeon_party.party_id, nullable
    consumables              JSONB        NOT NULL DEFAULT '{}'::jsonb,                          -- {"potion_minor": 3, ...}
    weapons_owned            JSONB        NOT NULL DEFAULT '{"rusty_dagger": 1}'::jsonb,
    armor_owned              JSONB        NOT NULL DEFAULT '{"cloth_tunic": 1}'::jsonb,
    deepest_floor            INTEGER      NOT NULL DEFAULT 0,
    bosses_slain             INTEGER      NOT NULL DEFAULT 0,
    total_kills              BIGINT       NOT NULL DEFAULT 0,
    total_captures           BIGINT       NOT NULL DEFAULT 0,
    total_runs               BIGINT       NOT NULL DEFAULT 0,
    total_copper_mined_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_silver_mined_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_gold_mined_raw     NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_rune_earned_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    total_usd_cashout_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    skill_cd_remaining       INTEGER      NOT NULL DEFAULT 0,               -- rounds left on class skill cooldown
    last_run_started_at      TIMESTAMPTZ,
    last_run_ended_at        TIMESTAMPTZ,
    last_action_at           TIMESTAMPTZ,
    -- Active run state (NULL when not delving)
    run_id                   BIGINT,                                        -- FK to dungeon_runs.run_id, nullable
    current_floor            INTEGER      NOT NULL DEFAULT 0,
    current_room             INTEGER      NOT NULL DEFAULT 0,
    current_room_type        TEXT,                                          -- 'mob' | 'ore' | 'shrine' | 'stairs' | 'chest' | 'boss' | 'empty' | NULL
    current_mob_state        JSONB,                                         -- {"key": "goblin", "hp": 12, "max_hp": 18, ...} when in combat, else NULL
    current_room_payload     JSONB,                                         -- room-specific data (ore symbol+qty, chest contents)
    -- Per-ore stake state (mirrors fishing's lure_staked_raw + last_stake_yield_at)
    copper_staked_raw        NUMERIC(36, 0) NOT NULL DEFAULT 0,
    silver_staked_raw        NUMERIC(36, 0) NOT NULL DEFAULT 0,
    gold_staked_raw          NUMERIC(36, 0) NOT NULL DEFAULT 0,
    rune_yield_pending_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    last_stake_yield_at      TIMESTAMPTZ,
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id),
    CONSTRAINT user_dungeon_level_chk         CHECK (level >= 1),
    CONSTRAINT user_dungeon_xp_chk            CHECK (xp >= 0),
    CONSTRAINT user_dungeon_deepest_chk       CHECK (deepest_floor >= 0),
    CONSTRAINT user_dungeon_hp_chk            CHECK (current_hp >= 0 AND current_hp <= hp_max),
    CONSTRAINT user_dungeon_class_chk         CHECK (class_key IS NULL OR class_key IN ('warrior', 'mage', 'rogue'))
);

-- Leaderboard helper: deepest floor reached, fastest path.
CREATE INDEX IF NOT EXISTS user_dungeon_deepest_idx
    ON user_dungeon (guild_id, deepest_floor DESC);

-- Leaderboard helper: top hunters by lifetime kills.
CREATE INDEX IF NOT EXISTS user_dungeon_kills_idx
    ON user_dungeon (guild_id, total_kills DESC);


-- Captured buddies. One row per captured mob; status flips to 'released'
-- when the player releases it (rows are kept for audit + bestiary stats
-- rather than hard-deleted). hp_alloc / atk_alloc / spd_alloc are the
-- player's manual stat point allocations against this buddy.
CREATE TABLE IF NOT EXISTS dungeon_party (
    party_id        BIGSERIAL    PRIMARY KEY,
    guild_id        BIGINT       NOT NULL,
    owner_user_id   BIGINT       NOT NULL,
    species_key     TEXT         NOT NULL,                                  -- mob key
    name            TEXT         NOT NULL DEFAULT '',
    level           INTEGER      NOT NULL DEFAULT 1,
    xp              BIGINT       NOT NULL DEFAULT 0,
    hp_alloc        INTEGER      NOT NULL DEFAULT 0,
    atk_alloc       INTEGER      NOT NULL DEFAULT 0,
    spd_alloc       INTEGER      NOT NULL DEFAULT 0,
    captured_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    captured_floor  INTEGER      NOT NULL DEFAULT 1,
    wins            INTEGER      NOT NULL DEFAULT 0,
    losses          INTEGER      NOT NULL DEFAULT 0,
    status          TEXT         NOT NULL DEFAULT 'owned',                  -- 'owned' | 'released'
    released_at     TIMESTAMPTZ,
    CONSTRAINT dungeon_party_status_chk CHECK (status IN ('owned', 'released')),
    CONSTRAINT dungeon_party_level_chk  CHECK (level >= 1),
    CONSTRAINT dungeon_party_xp_chk     CHECK (xp >= 0),
    CONSTRAINT dungeon_party_alloc_chk  CHECK (hp_alloc >= 0 AND atk_alloc >= 0 AND spd_alloc >= 0)
);

-- Roster lookup for the party panel: filter by owner + 'owned' status.
CREATE INDEX IF NOT EXISTS dungeon_party_owner_idx
    ON dungeon_party (guild_id, owner_user_id, status);


-- Append-only run log. One row per dungeon expedition. Powers the run
-- history view, the global "deepest floor" leaderboard, and any future
-- season-based metrics. ended_at + outcome are NULL while the run is
-- still in progress; a single user has at most one open run at a time
-- (referenced by user_dungeon.run_id).
CREATE TABLE IF NOT EXISTS dungeon_runs (
    run_id            BIGSERIAL    PRIMARY KEY,
    guild_id          BIGINT       NOT NULL,
    user_id           BIGINT       NOT NULL,
    class_key         TEXT,
    started_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ended_at          TIMESTAMPTZ,
    floors_cleared    INTEGER      NOT NULL DEFAULT 0,
    deepest_floor     INTEGER      NOT NULL DEFAULT 0,
    mobs_killed       INTEGER      NOT NULL DEFAULT 0,
    captures          INTEGER      NOT NULL DEFAULT 0,
    copper_mined_raw  NUMERIC(36, 0) NOT NULL DEFAULT 0,
    silver_mined_raw  NUMERIC(36, 0) NOT NULL DEFAULT 0,
    gold_mined_raw    NUMERIC(36, 0) NOT NULL DEFAULT 0,
    rune_earned_raw   NUMERIC(36, 0) NOT NULL DEFAULT 0,
    outcome           TEXT,                                                 -- 'cleared' | 'died' | 'fled' | 'rest'
    CONSTRAINT dungeon_runs_outcome_chk CHECK (
        outcome IS NULL OR outcome IN ('cleared', 'died', 'fled', 'rest')
    )
);

-- Per-user history page: most recent runs first.
CREATE INDEX IF NOT EXISTS dungeon_runs_user_idx
    ON dungeon_runs (guild_id, user_id, started_at DESC);

-- Global "deepest floor" board.
CREATE INDEX IF NOT EXISTS dungeon_runs_deepest_idx
    ON dungeon_runs (guild_id, deepest_floor DESC);


-- Append-only kill log. One row per slain or captured mob. Powers
-- achievements ("kill 100 goblins"), bestiary completion stats, and
-- per-floor encounter analytics. captured=TRUE marks rows where the
-- mob was tamed into dungeon_party instead of slain.
CREATE TABLE IF NOT EXISTS dungeon_kills (
    kill_id     BIGSERIAL    PRIMARY KEY,
    guild_id    BIGINT       NOT NULL,
    user_id     BIGINT       NOT NULL,
    mob_key     TEXT         NOT NULL,
    mob_tier    INTEGER      NOT NULL DEFAULT 1,
    floor       INTEGER      NOT NULL DEFAULT 1,
    captured    BOOLEAN      NOT NULL DEFAULT FALSE,
    killed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Per-user kill feed: most recent kills first.
CREATE INDEX IF NOT EXISTS dungeon_kills_user_idx
    ON dungeon_kills (guild_id, user_id, killed_at DESC);
