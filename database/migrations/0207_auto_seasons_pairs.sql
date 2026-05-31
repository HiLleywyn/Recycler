-- Auto-rotation for seasons + challenges. When enabled, the bot
-- automatically starts the next themed pair (one season + 5 paired
-- challenges) whenever:
--   * the active season's ``ends_at`` passes (time schedule), OR
--   * every paired challenge for the current season finishes
--     (completion schedule -- success or fail).
--
-- Rotation cycles through the pairs defined in seasons_pairs_config.py
-- using ``auto_seasons_pair_idx`` as the next-up cursor. Admins can
-- override the default duration / prize pool per guild; both fall back
-- to the env defaults when NULL.
--
-- Read in services/auto_seasons.py at every rotation tick so the change
-- takes effect on the next season end.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_seasons_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS auto_seasons_days INTEGER,
    ADD COLUMN IF NOT EXISTS auto_seasons_pool_usd NUMERIC(36, 0),
    ADD COLUMN IF NOT EXISTS auto_seasons_challenge_pool_usd NUMERIC(36, 0),
    ADD COLUMN IF NOT EXISTS auto_seasons_pair_idx INTEGER NOT NULL DEFAULT 0;
