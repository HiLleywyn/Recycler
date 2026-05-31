-- 0267_sage_network.sql
--
-- Sage Network rollout: crypto learn-and-earn economy backing three new
-- games (,pattern / ,gauge / ,tknom).
--
--   * SAGE = network coin, EDU = game token. Both EARN_ONLY (declared in
--     config.py). Same firewall shape as Lure / Gamba: tokens enter via
--     correct answers, exit via SAGE -> USD burn cashout.
--   * user_sage: per-user progression. Holds sage XP / level, lifetime
--     correct + run counters, per-game best streaks, and lifetime SAGE
--     + EDU earned totals (for the balance page summary).
--   * sage_runs: leaderboard backing table. One row per finished run,
--     keyed on (user_id, guild_id, game, ended_at). MAX(score) per user
--     drives the per-game top-10.
--   * sage_stakes: EDU stake positions. Mirrors gamba_stakes minus the
--     yield_target column (single yield target: SAGE).
--   * sage_active: mid-run lock. Backs the AI's mid-game refusal in
--     cogs/disco_ai.py -- a row here means the user is mid-quiz and
--     Disco should refuse to give answers (and roast them a little).
--     Auto-expires after 5 minutes via age check in services/sage.

-- ── Per-user progression ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_sage (
    user_id                 BIGINT          NOT NULL,
    guild_id                BIGINT          NOT NULL,
    sage_xp                 BIGINT          NOT NULL DEFAULT 0,
    sage_level              INTEGER         NOT NULL DEFAULT 1,
    lifetime_correct        BIGINT          NOT NULL DEFAULT 0,
    lifetime_runs           BIGINT          NOT NULL DEFAULT 0,
    best_pattern_streak     INTEGER         NOT NULL DEFAULT 0,
    best_gauge_streak       INTEGER         NOT NULL DEFAULT 0,
    best_tknom_streak       INTEGER         NOT NULL DEFAULT 0,
    total_sage_earned_raw   NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    total_edu_earned_raw    NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS idx_user_sage_level
    ON user_sage (guild_id, sage_level DESC);


-- ── Run history (leaderboard source) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS sage_runs (
    run_id              BIGSERIAL       PRIMARY KEY,
    user_id             BIGINT          NOT NULL,
    guild_id            BIGINT          NOT NULL,
    game                TEXT            NOT NULL CHECK (game IN ('pattern', 'gauge', 'tknom')),
    score               INTEGER         NOT NULL DEFAULT 0,
    sage_earned_raw     NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    edu_earned_raw      NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    ended_at            TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sage_runs_lb
    ON sage_runs (guild_id, game, score DESC, ended_at);
CREATE INDEX IF NOT EXISTS idx_sage_runs_user
    ON sage_runs (guild_id, user_id, ended_at DESC);


-- ── EDU stake positions ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sage_stakes (
    user_id             BIGINT          NOT NULL,
    guild_id            BIGINT          NOT NULL,
    amount              NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    pending_yield_raw   NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    total_claimed       NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    last_accrue         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    staked_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS idx_sage_stakes_active
    ON sage_stakes (guild_id, user_id)
    WHERE amount > 0;


-- ── Active-run lock (drives AI mid-game refusal) ───────────────────────
-- One row per (user, guild) means the user is mid-quiz. Pattern/Gauge/
-- Tokenomics all share the lock so a player can't run two games at once
-- via cross-channel concurrency.
CREATE TABLE IF NOT EXISTS sage_active (
    user_id     BIGINT          NOT NULL,
    guild_id    BIGINT          NOT NULL,
    game        TEXT            NOT NULL,
    started_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS idx_sage_active_age
    ON sage_active (started_at);


-- ── Mastery track registration ─────────────────────────────────────────
-- Sage mastery track ("sage_scholar") feeds into the cross-game mastery
-- branches. The mastery_config Python side declares the curve; the row
-- itself lives in user_mastery (no schema change needed).
