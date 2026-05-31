-- 0215_buddy_nest_multi_slot.sql
--
-- Lets a player run multiple incubation nests at once. Previously
-- ``cc_buddy_daycare`` was keyed on (guild_id, user_id), capping every
-- player at exactly one active nest. We now key on a synthetic
-- BIGSERIAL ``id`` and add a buddy-shop sink (``nest_slots_purchased``)
-- so a player can pay BUD to widen their cap from 1 (base) up to 10
-- (9 purchased upgrades, mirroring the BATTLE_SLOTS ladder).
--
-- The table name stays ``cc_buddy_daycare`` so historical
-- ``daycare_egg_collected`` bus events stay valid; only the surface in
-- the cog is rebranded "nest".
--
-- Idempotent / re-runnable: every step guards on existence so a
-- partially applied migration heals on the next boot.

-- 1. cc_buddy_daycare: drop the (guild_id, user_id) PK, add a serial id.
ALTER TABLE cc_buddy_daycare
    ADD COLUMN IF NOT EXISTS id BIGSERIAL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'cc_buddy_daycare_pkey'
           AND conrelid = 'cc_buddy_daycare'::regclass
    ) THEN
        -- Only drop if the existing PK is the legacy (guild_id, user_id)
        -- composite. If a previous run already promoted ``id``, leave it.
        IF (
            SELECT array_length(conkey, 1)
              FROM pg_constraint
             WHERE conname  = 'cc_buddy_daycare_pkey'
               AND conrelid = 'cc_buddy_daycare'::regclass
        ) = 2 THEN
            ALTER TABLE cc_buddy_daycare
                DROP CONSTRAINT cc_buddy_daycare_pkey;
        END IF;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'cc_buddy_daycare_pkey'
           AND conrelid = 'cc_buddy_daycare'::regclass
    ) THEN
        ALTER TABLE cc_buddy_daycare
            ADD CONSTRAINT cc_buddy_daycare_pkey PRIMARY KEY (id);
    END IF;
END $$;

-- Owner lookup is now a regular index (was implicit in the old PK).
CREATE INDEX IF NOT EXISTS cc_buddy_daycare_owner_idx
    ON cc_buddy_daycare (guild_id, user_id);

-- Application-level check in services/buddy_breeding.deposit() blocks
-- a buddy from sitting in two nests at once (parent1 OR parent2 across
-- any of the user's rows). The non-unique parent1_id / parent2_id
-- indexes from 0170 already speed up the cross-row lookup; keeping
-- them non-unique avoids blocking edge cases where a buddy
-- transitioned between owners post-collect.

-- 2. user_buddy_economy: nest slot purchases (mirrors battle/storage/egg).
ALTER TABLE user_buddy_economy
    ADD COLUMN IF NOT EXISTS nest_slots_purchased INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname  = 'user_buddy_economy_nest_slots_chk'
           AND conrelid = 'user_buddy_economy'::regclass
    ) THEN
        ALTER TABLE user_buddy_economy
            ADD CONSTRAINT user_buddy_economy_nest_slots_chk
                CHECK (nest_slots_purchased BETWEEN 0 AND 9);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS user_buddy_economy_nest_slots_idx
    ON user_buddy_economy (guild_id, nest_slots_purchased DESC)
 WHERE nest_slots_purchased > 0;
