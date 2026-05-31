-- Crafting specialties: pick-2 selection + Enchanting branch.
--
-- Adds:
--   * active_specialties TEXT[]  -- the (up to 2) specialty keys the
--                                   player has actively selected. Empty
--                                   array = "generalist" (no bonuses,
--                                   no specialty-locked recipes).
--   * enchanting_xp / enchanting_level for the new sixth specialty.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS, IF NOT EXISTS index).

ALTER TABLE user_crafting
    ADD COLUMN IF NOT EXISTS active_specialties TEXT[]   NOT NULL DEFAULT '{}';
ALTER TABLE user_crafting
    ADD COLUMN IF NOT EXISTS enchanting_xp      BIGINT   NOT NULL DEFAULT 0;
ALTER TABLE user_crafting
    ADD COLUMN IF NOT EXISTS enchanting_level   INTEGER  NOT NULL DEFAULT 1;

-- The picker enforces "max 2" at the application layer; a CHECK
-- constraint here would also work but would fight an empty default
-- on every existing row. The column-default empty array stays valid.
ALTER TABLE user_crafting
    DROP CONSTRAINT IF EXISTS user_crafting_active_specialties_chk;
ALTER TABLE user_crafting
    ADD CONSTRAINT user_crafting_active_specialties_chk
    CHECK (array_length(active_specialties, 1) IS NULL
           OR array_length(active_specialties, 1) <= 2);
