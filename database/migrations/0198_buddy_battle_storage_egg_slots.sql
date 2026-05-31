-- 0198_buddy_battle_storage_egg_slots.sql
--
-- Splits the single ``bud_slots_purchased`` cap on user_buddy_economy
-- into three distinct purchasable capacities, and adds the new
-- ``,buddy storage eggs`` egg-storage container.
--
--   * battle_slots_purchased     -- extra ACTIVE (status='owned') buddies
--                                   on top of the 3-slot base, max 7
--                                   (cap = 10 effective).
--   * storage_slots_purchased    -- extra STORED (status='stored') buddies
--                                   on top of the 10-slot base, max 9
--                                   (cap = 100 effective; +10 per upgrade).
--   * egg_storage_slots_purchased -- extra rows of buddy egg storage on
--                                   top of the 50-egg base, max 19
--                                   (cap = 1000 effective; +50 per upgrade).
--   * egg_storage                -- JSONB array of stored eggs
--                                   ({species, rarity_tier, rolled_at,
--                                   from}). Held-egg overflow lands here.
--
-- Migration of legacy ``bud_slots_purchased`` -> ``storage_slots_purchased``:
-- existing slot purchases are interpreted as storage upgrades because (a)
-- battle is hard-capped at +7 and (b) storage is the new primary "more
-- room" sink. We multiply the per-upgrade step (10) into the new slot
-- count and clamp at the new cap so a user who maxed the old 100-slot
-- system lands at the new 9-storage-slot ceiling rather than getting
-- a credit they can't spend.
--
-- Indexes mirror the existing user_buddy_economy_slots_idx pattern so
-- admin analytics on the new sinks have the same shape.

ALTER TABLE user_buddy_economy
    ADD COLUMN IF NOT EXISTS battle_slots_purchased     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS storage_slots_purchased    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS egg_storage_slots_purchased INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS egg_storage                JSONB   NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE user_buddy_economy
    ADD CONSTRAINT user_buddy_economy_battle_slots_chk
        CHECK (battle_slots_purchased  BETWEEN 0 AND 7),
    ADD CONSTRAINT user_buddy_economy_storage_slots_chk
        CHECK (storage_slots_purchased BETWEEN 0 AND 9),
    ADD CONSTRAINT user_buddy_economy_egg_slots_chk
        CHECK (egg_storage_slots_purchased BETWEEN 0 AND 19);

-- Backfill: convert each legacy bud_slots_purchased into a storage
-- slot upgrade (clamped at the new ceiling). Run once and only on
-- rows where the new column is still default-zero so re-runs are
-- idempotent and a player who already paid in the new system isn't
-- double-credited.
UPDATE user_buddy_economy
   SET storage_slots_purchased = LEAST(9, GREATEST(0, bud_slots_purchased))
 WHERE storage_slots_purchased = 0
   AND bud_slots_purchased     > 0;

-- Per-guild slot-purchase analytics (mirrors user_buddy_economy_slots_idx).
CREATE INDEX IF NOT EXISTS user_buddy_economy_battle_slots_idx
    ON user_buddy_economy (guild_id, battle_slots_purchased DESC)
 WHERE battle_slots_purchased > 0;

CREATE INDEX IF NOT EXISTS user_buddy_economy_storage_slots_idx
    ON user_buddy_economy (guild_id, storage_slots_purchased DESC)
 WHERE storage_slots_purchased > 0;

CREATE INDEX IF NOT EXISTS user_buddy_economy_egg_storage_slots_idx
    ON user_buddy_economy (guild_id, egg_storage_slots_purchased DESC)
 WHERE egg_storage_slots_purchased > 0;
