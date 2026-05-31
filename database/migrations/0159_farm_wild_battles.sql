-- Farm wild battles. Mirror columns to the dungeon's
-- 0154_dungeon_wild_battles migration so the farm cog can track
-- harvest-spawned wild buddy encounters with the same shape.
--
-- Spawn lifecycle:
--   ,farm harvest -> resolve_harvest -> 8% roll -> wild buddy spawned
--   ,farm battle -> services.buddy_battle.run_battle vs active CC buddy
--   resolve_wild_battle credits HRV + BBT + capture roll, bumps these.

ALTER TABLE user_farming
    ADD COLUMN IF NOT EXISTS wild_battles_won      BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wild_battles_lost     BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wild_buddies_captured BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pending_wild_buddy    JSONB;

CREATE INDEX IF NOT EXISTS user_farming_wild_won_idx
    ON user_farming (guild_id, wild_battles_won DESC)
    WHERE wild_battles_won > 0;
