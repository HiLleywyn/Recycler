-- Expand seasons.metric + add season_counters for activity-based metrics.
-- Migration 0126 created the column with CHECK (metric IN ('net_worth')).
-- services/seasons.py now supports volume, trades, pass_xp, and
-- buddy_wins in addition to net_worth, so the constraint widens to match.
--
-- season_counters is the generic per-season activity counter table used
-- for metrics that don't have their own dedicated store (volume/trades
-- live in transactions; pass_xp lives in season_xp; everything else can
-- accumulate here). Rows are keyed by (season_id, user_id, counter) so a
-- single user can have one counter per metric type within a season.

ALTER TABLE seasons DROP CONSTRAINT IF EXISTS seasons_metric_check;
ALTER TABLE seasons
    ADD CONSTRAINT seasons_metric_check
    CHECK (metric IN ('net_worth', 'volume', 'trades', 'pass_xp', 'buddy_wins'));

CREATE TABLE IF NOT EXISTS season_counters (
    season_id   BIGINT      NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
    user_id     BIGINT      NOT NULL,
    guild_id    BIGINT      NOT NULL,
    counter     TEXT        NOT NULL,
    value       BIGINT      NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (season_id, user_id, counter)
);

-- Hot path: look up a user's counter standings in a season.
CREATE INDEX IF NOT EXISTS season_counters_season_counter_idx
    ON season_counters (season_id, counter, value DESC);

-- Per-user lookup used when incrementing multiple counters for the same
-- event (currently just buddy_wins, but easy to extend).
CREATE INDEX IF NOT EXISTS season_counters_user_idx
    ON season_counters (user_id, guild_id, season_id);
