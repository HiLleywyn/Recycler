-- Treasure-map digging: ,fish dig consumes a "Soggy Treasure Map"
-- (junk_inventory key "map") and rolls a weighted loot outcome from
-- fishing_config.TREASURE_LOOT_WEIGHTS. The cooldown clock and the
-- lifetime counter both live on user_fishing alongside the rest of
-- the per-player fishing state.
--
-- last_treasure_dig_at  -- DB-side cooldown clock for ,fish dig.
--                          Compared via EXTRACT(EPOCH FROM (NOW() -
--                          last_treasure_dig_at)) per the project rule
--                          against Python now() vs Postgres timestamp.
-- total_treasures_dug   -- lifetime number of maps consumed
--                          (analytics + future achievements).

ALTER TABLE user_fishing
    ADD COLUMN IF NOT EXISTS last_treasure_dig_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS total_treasures_dug  BIGINT NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'user_fishing_dig_nonneg_chk'
    ) THEN
        ALTER TABLE user_fishing
            ADD CONSTRAINT user_fishing_dig_nonneg_chk
            CHECK (total_treasures_dug >= 0) NOT VALID;
        ALTER TABLE user_fishing VALIDATE CONSTRAINT user_fishing_dig_nonneg_chk;
    END IF;
END$$;
