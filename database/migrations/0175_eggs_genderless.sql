-- 0175_eggs_genderless.sql
--
-- Reverses the egg-gender carry-over from migration 0174. Eggs are now
-- genderless until they hatch -- the canonical truth is that gender is
-- rolled by the hatch path (services/fishing.py:hatch_held_egg) right
-- before the cc_buddies row is inserted.
--
-- Three changes, all idempotent:
--
--   1. Drop egg_gender (and its CHECK) from cc_buddy_daycare. Eggs that
--      were sitting in the daycare with a pre-rolled gender now lose it
--      and will roll fresh at hatch time, matching freshly-laid eggs.
--   2. Strip the "gender" key from every entry in
--      user_fishing.held_eggs so the JSONB shape stays clean and no
--      future code path is tempted to honour the stale value.
--   3. Same for any 'gender' key inside auction_listings.metadata or
--      item_instances.metadata for kind='egg', so existing escrowed egg
--      listings settle without carrying a gender into the buyer's
--      inventory.
--
-- cc_buddies.gender stays exactly as it is -- buddies have a real,
-- canonical gender once they're hatched, and breeding still requires
-- one male + one female.

-- 1. cc_buddy_daycare.egg_gender ---------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'cc_buddy_daycare_egg_gender_chk'
    ) THEN
        ALTER TABLE cc_buddy_daycare
            DROP CONSTRAINT cc_buddy_daycare_egg_gender_chk;
    END IF;
END
$$;

ALTER TABLE cc_buddy_daycare
    DROP COLUMN IF EXISTS egg_gender;


-- 2. user_fishing.held_eggs --------------------------------------------------
-- Walk every row that still has at least one entry with a 'gender' key
-- and rebuild the array minus that key. jsonb '-' on an object removes a
-- top-level key; jsonb_agg + the SELECT below applies it per-element.
UPDATE user_fishing
   SET held_eggs = (
       SELECT COALESCE(
           jsonb_agg(elem - 'gender'),
           '[]'::jsonb
       )
         FROM jsonb_array_elements(held_eggs) AS elem
   ),
       updated_at = NOW()
 WHERE jsonb_typeof(held_eggs) = 'array'
   AND held_eggs::text LIKE '%"gender"%';


-- 3. auction_listings.metadata + item_instances.metadata --------------------
-- Strip 'gender' from any egg listing's metadata so settle paths don't
-- re-stamp the carried gender onto the buyer's held egg.
UPDATE auction_listings
   SET metadata = metadata - 'gender'
 WHERE kind = 'egg'
   AND metadata ? 'gender';

UPDATE item_instances
   SET metadata = metadata - 'gender',
       updated_at = NOW()
 WHERE kind = 'egg'
   AND metadata ? 'gender';
