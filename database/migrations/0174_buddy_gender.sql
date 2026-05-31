-- 0174_buddy_gender.sql
--
-- Adds gender to CC Buddies so the daycare can require opposite-gender
-- pairs (eggs are made by one of each, like every other tamagotchi-style
-- breeder). Held eggs carry a pre-rolled gender on their JSONB row so
-- the player sees what they'd hatch before they hatch it; the column on
-- cc_buddies is the canonical truth post-hatch.
--
-- Existing rows get a 50/50 random backfill so no buddy ends up
-- ungendered after the migration. The CHECK enforces the canonical
-- 'M' / 'F' values; no NULLs allowed -- every buddy has one.

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS gender TEXT;

-- Backfill before tightening the constraint. random() is per-row so each
-- existing buddy gets its own 50/50 roll.
UPDATE cc_buddies
   SET gender = CASE WHEN random() < 0.5 THEN 'M' ELSE 'F' END
 WHERE gender IS NULL OR gender NOT IN ('M', 'F');

ALTER TABLE cc_buddies
    ALTER COLUMN gender SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'cc_buddies_gender_chk'
    ) THEN
        ALTER TABLE cc_buddies
            ADD CONSTRAINT cc_buddies_gender_chk
            CHECK (gender IN ('M', 'F'));
    END IF;
END
$$;

-- Hot-path index for any future "find me an opposite-gender breeding
-- partner" query. Keeps the existing owner / status indexes alone.
CREATE INDEX IF NOT EXISTS cc_buddies_gender_idx
    ON cc_buddies (guild_id, owner_user_id, gender);


-- Daycare eggs need a pre-rolled gender too so the player can see what
-- they'd hatch before they hatch it (mirrors how species + rarity are
-- already pre-rolled at deposit time). Existing daycare rows from before
-- 0174 get a 50/50 backfill so the collect path always has a gender to
-- carry into the held egg.
ALTER TABLE cc_buddy_daycare
    ADD COLUMN IF NOT EXISTS egg_gender TEXT;

UPDATE cc_buddy_daycare
   SET egg_gender = CASE WHEN random() < 0.5 THEN 'M' ELSE 'F' END
 WHERE egg_gender IS NULL OR egg_gender NOT IN ('M', 'F');

ALTER TABLE cc_buddy_daycare
    ALTER COLUMN egg_gender SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'cc_buddy_daycare_egg_gender_chk'
    ) THEN
        ALTER TABLE cc_buddy_daycare
            ADD CONSTRAINT cc_buddy_daycare_egg_gender_chk
            CHECK (egg_gender IN ('M', 'F'));
    END IF;
END
$$;
