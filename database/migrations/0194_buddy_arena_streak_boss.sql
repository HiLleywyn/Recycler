-- Buddy Network arena: streak tracking + daily boss surface.
--
-- Adds two clusters of columns to user_buddy_economy:
--
--   Streak tracking. Every arena win bumps arena_streak by 1; every loss
--   resets it to 0. arena_best_streak is the lifetime high-water mark
--   (for the leaderboard and the player panel). Both feed an additive
--   reward multiplier in services.buddy_economy.resolve_arena_battle so
--   stringing wins together pays meaningfully more than grinding the
--   same fight cold.
--
--   Daily boss. The arena boss is a once-per-day high-level encounter
--   with 4x payouts (BUD + BBT) and a fixed +5 level bump above the
--   player's active buddy. arena_boss_wins / arena_boss_losses are the
--   lifetime counters; last_arena_boss_at gates the once-per-day cooldown
--   on the DB clock (no Python clocks for cooldowns).

ALTER TABLE user_buddy_economy
    ADD COLUMN IF NOT EXISTS arena_streak           BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS arena_best_streak      BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS arena_boss_wins        BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS arena_boss_losses      BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_arena_boss_at     TIMESTAMPTZ;

-- Best-streak leaderboard helper. Same shape as the lifetime-wins index:
-- only index rows that have actually scored a streak so the index stays
-- tight on a fresh / mostly-cold guild.
CREATE INDEX IF NOT EXISTS user_buddy_economy_arena_best_streak_idx
    ON user_buddy_economy (guild_id, arena_best_streak DESC)
    WHERE arena_best_streak > 0;
