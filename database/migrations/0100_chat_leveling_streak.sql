-- Chat-leveling streak tracking and streak-based XP multiplier.
--
-- Linear ramp: xp_multiplier = 1 + streak_pct_per_day/100 * min(streak_days, streak_max_days).
-- Defaults reproduce the source system's behaviour (+1% per day, capped at 10 days = +10%).
ALTER TABLE chat_levels
    ADD COLUMN IF NOT EXISTS streak_days      INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_active_date DATE;

ALTER TABLE chat_level_config
    ADD COLUMN IF NOT EXISTS streak_max_days    INTEGER NOT NULL DEFAULT 10,
    ADD COLUMN IF NOT EXISTS streak_pct_per_day INTEGER NOT NULL DEFAULT 1;
