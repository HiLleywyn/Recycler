-- Phase 4 of the CC Buddy system: rarity, multi-pet collection, active flag.
--
-- Two big changes:
--   1. Every buddy now has a rarity tier (1..5) driving its bonus scaling
--      and stat decay rates. Tier is derived from species at hatch / swap /
--      reroll time and stored on the row so SQL-side decay math doesn't
--      need a CASE-over-species in every sweep.
--
--   2. Players can hold up to 3 owned buddies, but only ONE is "active" at
--      a time. Passive stat decay, chat-XP grants, and the buddy_bonus
--      multiplier only consider the active buddy. The partial unique index
--      shifts from "one owned per user" to "one ACTIVE owned per user".
--
-- Safe to run on existing data: new columns default to sensible values,
-- rarity_tier is backfilled from species, and the old index is dropped
-- before the new one is created (both are partial / conditional).

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS rarity_tier INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS is_active   BOOLEAN NOT NULL DEFAULT TRUE;

-- Guard the valid range. CHECK added idempotently via NOT VALID + VALIDATE
-- so re-running the migration doesn't trip an existing constraint.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddies_rarity_tier_chk'
    ) THEN
        ALTER TABLE cc_buddies
            ADD CONSTRAINT cc_buddies_rarity_tier_chk
            CHECK (rarity_tier BETWEEN 1 AND 5) NOT VALID;
        ALTER TABLE cc_buddies VALIDATE CONSTRAINT cc_buddies_rarity_tier_chk;
    END IF;
END $$;

-- Backfill rarity_tier from species for rows that were inserted before
-- rarity existed. New species land at 1 and get corrected on-demand by
-- the cog the next time the row is updated; no code path reads rarity
-- for an unknown species.
UPDATE cc_buddies SET rarity_tier = CASE species
    WHEN 'zenny'   THEN 1
    WHEN 'pyper'   THEN 1
    WHEN 'cobble'  THEN 1
    WHEN 'fox'     THEN 2
    WHEN 'crab'    THEN 2
    WHEN 'wolf'    THEN 3
    WHEN 'shrimp'  THEN 3
    WHEN 'glitch'  THEN 4
    WHEN 'octopus' THEN 4
    WHEN 'lobster' THEN 4
    WHEN 'nimbus'  THEN 5
    ELSE 1
END
WHERE rarity_tier = 1;   -- only touch rows we haven't corrected yet

-- Swap the per-user uniqueness from "one owned" to "one active". Multiple
-- owned buddies per user is now allowed; only one of them can be active.
DROP INDEX IF EXISTS cc_buddies_one_owned_per_user;

CREATE UNIQUE INDEX IF NOT EXISTS cc_buddies_one_active_per_user
    ON cc_buddies (guild_id, owner_user_id)
    WHERE status = 'owned' AND owner_user_id IS NOT NULL AND is_active;

-- Separate lookup path for "all of user's owned buddies" (the panel
-- paginator needs this).
CREATE INDEX IF NOT EXISTS cc_buddies_owner_all_idx
    ON cc_buddies (guild_id, owner_user_id, id)
    WHERE status = 'owned' AND owner_user_id IS NOT NULL;
