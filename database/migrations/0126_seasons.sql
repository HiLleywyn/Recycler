-- Seasons: time-bounded leaderboard competitions with a prize pool.
--
-- A guild can have at most one ACTIVE season at a time (enforced by the
-- partial unique index below). Admins start a season manually via
-- ,season start; seasons end either manually (,season end) or when the
-- background check in services/seasons.py observes ends_at <= NOW().
--
-- On end, services/seasons.end_season() iterates the top N users by the
-- season's metric and writes one row per recipient to season_entries
-- with their final rank, metric value, and computed reward. Rewards are
-- paid at that time (no claim step needed for MVP; this keeps the flow
-- simple and guarantees everyone gets paid without a follow-up action).

CREATE TABLE IF NOT EXISTS seasons (
    season_id      BIGSERIAL    PRIMARY KEY,
    guild_id       BIGINT       NOT NULL,
    name           TEXT         NOT NULL,
    metric         TEXT         NOT NULL CHECK (metric IN ('net_worth')),
    prize_pool_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    started_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ends_at        TIMESTAMPTZ  NOT NULL,
    finalized_at   TIMESTAMPTZ,
    status         TEXT         NOT NULL DEFAULT 'active'
                                 CHECK (status IN ('active', 'finalized'))
);

-- At most one active season per guild.
CREATE UNIQUE INDEX IF NOT EXISTS seasons_one_active_per_guild_idx
    ON seasons (guild_id)
    WHERE status = 'active';

-- Lookup by guild + status is the hot path.
CREATE INDEX IF NOT EXISTS seasons_guild_status_idx
    ON seasons (guild_id, status);

-- Per-user snapshot of the final state + reward.
CREATE TABLE IF NOT EXISTS season_entries (
    season_id    BIGINT      NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
    user_id      BIGINT      NOT NULL,
    guild_id     BIGINT      NOT NULL,
    final_rank   INT         NOT NULL,
    metric_value DOUBLE PRECISION NOT NULL,
    reward_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (season_id, user_id)
);

CREATE INDEX IF NOT EXISTS season_entries_guild_rank_idx
    ON season_entries (guild_id, season_id, final_rank);
