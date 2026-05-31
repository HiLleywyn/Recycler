-- 0264_overhaul_core_systems.sql
-- Adds support for:
--   * Farming overhaul: harvest combos, hand tools, farmer perks, scarecrow placements.
--   * Fishing overhaul: rod augments, monster + treasure counters,
--     weekly fishing tournaments.
--   * Delve arena PvP: per-season user ELO tables, match history,
--     duel invites, season window.
--
-- Plain ASCII only (no em/en dashes), idempotent where the underlying
-- column / table may already exist on older deploys.

-- ----------------------------------------------------------------------
-- Farming: combo + tools + perks + scarecrow placements
-- ----------------------------------------------------------------------
ALTER TABLE user_farming
    ADD COLUMN IF NOT EXISTS tools          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS perks          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS combo_step     INT   NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS best_combo_step INT  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS scarecrow_count INT  NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS user_farming_perks_idx
    ON user_farming USING GIN (perks);
CREATE INDEX IF NOT EXISTS user_farming_tools_idx
    ON user_farming USING GIN (tools);

-- ----------------------------------------------------------------------
-- Fishing: rod augments + monster / treasure counters + tournaments
-- ----------------------------------------------------------------------
ALTER TABLE user_fishing
    ADD COLUMN IF NOT EXISTS augments         JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS monsters_defeated INT  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS treasures_pulled INT  NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS user_fishing_augments_idx
    ON user_fishing USING GIN (augments);

CREATE TABLE IF NOT EXISTS fishing_tournaments (
    season_id       BIGSERIAL PRIMARY KEY,
    guild_id        BIGINT NOT NULL,
    theme           TEXT NOT NULL,
    start_ts        TIMESTAMPTZ NOT NULL,
    end_ts          TIMESTAMPTZ NOT NULL,
    payout_pool_raw NUMERIC(36,0) NOT NULL DEFAULT 0,
    settled         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS fishing_tournaments_guild_idx
    ON fishing_tournaments (guild_id, end_ts DESC);

CREATE TABLE IF NOT EXISTS fishing_tournament_entries (
    season_id   BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    guild_id    BIGINT NOT NULL,
    score_raw   NUMERIC(36,0) NOT NULL DEFAULT 0,
    score_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    rank        INT,
    paid_at     TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (season_id, user_id)
);
CREATE INDEX IF NOT EXISTS fishing_tournament_entries_score_idx
    ON fishing_tournament_entries (season_id, guild_id, score_raw DESC);

-- ----------------------------------------------------------------------
-- Delve arena PvP
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS delve_arena_seasons (
    season_id   BIGSERIAL PRIMARY KEY,
    start_ts    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    end_ts      TIMESTAMPTZ NOT NULL,
    settled     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS user_delve_arena (
    user_id       BIGINT NOT NULL,
    guild_id      BIGINT NOT NULL,
    season_id     BIGINT NOT NULL,
    elo           INT NOT NULL DEFAULT 100,
    peak_elo      INT NOT NULL DEFAULT 100,
    wins          INT NOT NULL DEFAULT 0,
    losses        INT NOT NULL DEFAULT 0,
    streak        INT NOT NULL DEFAULT 0,
    best_streak   INT NOT NULL DEFAULT 0,
    last_fight_at TIMESTAMPTZ,
    profile_snap  JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (user_id, guild_id, season_id)
);
CREATE INDEX IF NOT EXISTS user_delve_arena_lb_idx
    ON user_delve_arena (guild_id, season_id, elo DESC);

CREATE TABLE IF NOT EXISTS delve_arena_matches (
    match_id      BIGSERIAL PRIMARY KEY,
    season_id     BIGINT NOT NULL,
    guild_id      BIGINT NOT NULL,
    p1_uid        BIGINT NOT NULL,
    p2_uid        BIGINT NOT NULL,
    winner_uid    BIGINT,
    p1_elo_before INT,
    p1_elo_after  INT,
    p2_elo_before INT,
    p2_elo_after  INT,
    rounds        INT NOT NULL DEFAULT 0,
    flawless      BOOLEAN NOT NULL DEFAULT FALSE,
    mode          TEXT NOT NULL DEFAULT 'async',
    ranked        BOOLEAN NOT NULL DEFAULT TRUE,
    replay        JSONB NOT NULL DEFAULT '{}'::jsonb,
    fought_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS delve_arena_matches_user_idx
    ON delve_arena_matches (p1_uid, p2_uid, fought_at DESC);
CREATE INDEX IF NOT EXISTS delve_arena_matches_guild_idx
    ON delve_arena_matches (guild_id, fought_at DESC);

CREATE TABLE IF NOT EXISTS delve_arena_duel_invites (
    invite_id      BIGSERIAL PRIMARY KEY,
    season_id      BIGINT NOT NULL,
    guild_id       BIGINT NOT NULL,
    challenger_uid BIGINT NOT NULL,
    target_uid     BIGINT NOT NULL,
    ranked         BOOLEAN NOT NULL DEFAULT TRUE,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS delve_arena_duel_invites_target_idx
    ON delve_arena_duel_invites (target_uid, status, created_at DESC);
CREATE INDEX IF NOT EXISTS delve_arena_duel_invites_challenger_idx
    ON delve_arena_duel_invites (challenger_uid, created_at DESC);
