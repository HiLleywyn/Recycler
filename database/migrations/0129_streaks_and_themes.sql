-- Daily login streaks + themed season XP multipliers.
--
-- user_streaks: tracked per (user_id, guild_id) and incremented once per
-- day when the user claims daily. last_claim_date is a DATE (UTC) so the
-- "consecutive day" check is exact: yesterday == today - 1 day. If the
-- gap is longer, the streak resets to 1 on the next claim.
--
-- seasons.xp_multipliers: per-event XP multipliers applied in grant_xp
-- when a season has a theme. Stored as JSONB so adding themes later
-- needs no schema change -- it's keyed by bus event name (matching
-- seasonpass_config.XP_EVENTS keys). Empty dict '{}' means "no theme"
-- (classic season).

CREATE TABLE IF NOT EXISTS user_streaks (
    user_id         BIGINT      NOT NULL,
    guild_id        BIGINT      NOT NULL,
    current_streak  INT         NOT NULL DEFAULT 0,
    longest_streak  INT         NOT NULL DEFAULT 0,
    last_claim_date DATE,
    total_claims    INT         NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS user_streaks_guild_current_idx
    ON user_streaks (guild_id, current_streak DESC);

ALTER TABLE seasons
    ADD COLUMN IF NOT EXISTS xp_multipliers JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE seasons
    ADD COLUMN IF NOT EXISTS theme TEXT NOT NULL DEFAULT 'classic';
