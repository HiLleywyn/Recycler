-- 0185_user_daily_streak.sql
--
-- Daily login streak per (user, guild). Drives the ``,hub`` panel:
-- visiting the hub once per UTC day claims a streak bonus that scales
-- with consecutive-day count. Missing a day resets ``current_streak``
-- to zero on the next claim; ``longest_streak`` is the all-time high.
--
-- One row per (user, guild). Reads / writes happen on every ``,hub``
-- call so a single primary-key lookup is the only cost. ``last_claim_utc``
-- is a date (not timestamp) -- the streak rolls at UTC midnight, no
-- container-clock skew, and "did I already claim today" is a single
-- date equality check.
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS user_daily_streak (
    user_id          BIGINT       NOT NULL,
    guild_id         BIGINT       NOT NULL,
    current_streak   INTEGER      NOT NULL DEFAULT 0,
    longest_streak   INTEGER      NOT NULL DEFAULT 0,
    last_claim_utc   DATE,
    total_claims     INTEGER      NOT NULL DEFAULT 0,
    total_reward_usd_raw NUMERIC(36, 0) NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS user_daily_streak_guild_idx
    ON user_daily_streak (guild_id, current_streak DESC);
