-- Achievements: extend the existing badges / user_badges tables with a
-- reward_usd column and indexes. The Python catalog in achievements_config.py
-- is synced into the `badges` table on cog load via services/achievements.py.
--
-- No new tables are created here; the schema.sql `badges` and `user_badges`
-- tables predate this migration. We only add columns that did not exist
-- before (rewards, sort_order, secret) and an earned-at index for fast
-- per-user recent-achievement queries.

ALTER TABLE badges
    ADD COLUMN IF NOT EXISTS reward_usd DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE badges
    ADD COLUMN IF NOT EXISTS sort_order INT NOT NULL DEFAULT 0;
ALTER TABLE badges
    ADD COLUMN IF NOT EXISTS secret BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS user_badges_user_earned_idx
    ON user_badges (user_id, guild_id, earned_at DESC);
CREATE INDEX IF NOT EXISTS badges_category_sort_idx
    ON badges (category, sort_order);

-- Counts of each bus-event trigger per user. Incremented by
-- services/achievements.py whenever a relevant bus event fires. The
-- achievements service reads the counter against each catalog entry's
-- requirement.count to decide whether to award the badge.
CREATE TABLE IF NOT EXISTS achievement_progress (
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL,
    trigger    TEXT        NOT NULL,
    counter    BIGINT      NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id, trigger)
);
CREATE INDEX IF NOT EXISTS achievement_progress_user_idx
    ON achievement_progress (user_id, guild_id);
