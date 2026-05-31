-- Wild-buddy battles from fishing.
--
-- Three counters on user_fishing for analytics + achievements:
--   wild_battles_won       -- lifetime wins against wild aquatic buddies
--   wild_battles_lost      -- lifetime losses (no penalty, just tracked)
--   wild_buddies_captured  -- lifetime captures (post-win capture-roll hits)
--
-- The fishing_catches table also gains a new outcome value 'wild_battle'.
-- A row is appended when the spawn fires (regardless of how the fight
-- resolves) so the player's ,fish history shows the encounter even if
-- they bail on the prompt.

ALTER TABLE user_fishing
    ADD COLUMN IF NOT EXISTS wild_battles_won      BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wild_battles_lost     BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wild_buddies_captured BIGINT NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'user_fishing_wild_nonneg_chk'
    ) THEN
        ALTER TABLE user_fishing
            ADD CONSTRAINT user_fishing_wild_nonneg_chk
            CHECK (
                wild_battles_won      >= 0
                AND wild_battles_lost     >= 0
                AND wild_buddies_captured >= 0
            ) NOT VALID;
        ALTER TABLE user_fishing VALIDATE CONSTRAINT user_fishing_wild_nonneg_chk;
    END IF;
END$$;

-- Add 'wild_battle' to the allowed fishing_catches.outcome set. Drop and
-- recreate the existing CHECK so the new value validates the same way
-- the original did.
ALTER TABLE fishing_catches
    DROP CONSTRAINT IF EXISTS fishing_catches_outcome_chk;

ALTER TABLE fishing_catches
    ADD CONSTRAINT fishing_catches_outcome_chk CHECK (
        outcome IN ('fish', 'junk', 'money_bag', 'mystery_box', 'buddy_egg', 'wild_battle')
    );
