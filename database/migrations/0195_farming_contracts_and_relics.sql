-- Farming contracts + delve relics + delve cursed runs.
--
-- Farming additions:
--   user_farming.daily_contract             JSONB -- one rolling NPC order
--   user_farming.total_contracts_completed  BIGINT -- lifetime completion count
--
-- Delve additions:
--   user_dungeon.relics_owned   JSONB -- {"vampire_fang": 1, ...} stack inventory
--   user_dungeon.equipped_relic TEXT  -- key from RELICS, NULL = none
--   user_dungeon.run_curse      TEXT  -- key from RUN_CURSES, NULL = none
--   user_dungeon.total_curses_completed BIGINT
--
-- All JSONB columns default to '{}'::jsonb / NULL so existing rows backfill
-- cleanly without a separate UPDATE pass.

ALTER TABLE user_farming
    ADD COLUMN IF NOT EXISTS daily_contract            JSONB,
    ADD COLUMN IF NOT EXISTS total_contracts_completed BIGINT NOT NULL DEFAULT 0;

ALTER TABLE user_dungeon
    ADD COLUMN IF NOT EXISTS relics_owned             JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS equipped_relic           TEXT,
    ADD COLUMN IF NOT EXISTS run_curse                TEXT,
    ADD COLUMN IF NOT EXISTS total_curses_completed   BIGINT NOT NULL DEFAULT 0;

-- Leaderboard helper: top contract grinders.
CREATE INDEX IF NOT EXISTS user_farming_contracts_idx
    ON user_farming (guild_id, total_contracts_completed DESC);

-- Leaderboard helper: top cursed-run delvers.
CREATE INDEX IF NOT EXISTS user_dungeon_curses_idx
    ON user_dungeon (guild_id, total_curses_completed DESC);
