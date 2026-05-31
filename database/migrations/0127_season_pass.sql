-- Season battle pass: XP accumulation + tier claims per user per season.
--
-- Scoped by season_id so XP resets when a new season starts and finalized
-- seasons keep their pass state intact. Tier curve + reward amounts live
-- in seasonpass_config.py (single source of truth); this migration only
-- stores the per-user counter + claim ledger.

CREATE TABLE IF NOT EXISTS season_xp (
    season_id    BIGINT      NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
    user_id      BIGINT      NOT NULL,
    guild_id     BIGINT      NOT NULL,
    xp           BIGINT      NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (season_id, user_id)
);

-- Hot path: look up a user's XP in the active season.
CREATE INDEX IF NOT EXISTS season_xp_user_idx
    ON season_xp (user_id, guild_id, season_id);

-- Per-tier claim ledger. One row per (season, user, tier) means a tier
-- can only be claimed once -- enforced by the composite primary key.
CREATE TABLE IF NOT EXISTS season_tier_claims (
    season_id    BIGINT      NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
    user_id      BIGINT      NOT NULL,
    guild_id     BIGINT      NOT NULL,
    tier         INT         NOT NULL,
    reward_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,
    claimed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (season_id, user_id, tier)
);

CREATE INDEX IF NOT EXISTS season_tier_claims_user_idx
    ON season_tier_claims (user_id, guild_id, season_id);
