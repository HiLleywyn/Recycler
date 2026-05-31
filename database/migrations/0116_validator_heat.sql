-- Validator heat: persistent per-validator state that gives stakers a reason
-- to watch and chase. Updated every hourly tick by cogs/stake.py:
--   - natural decay toward 0 (heat *= 0.92 per tick, so ~10 hours to half)
--   - HOT event: +0.20 (capped at +1.0)
--   - COLD event: -0.20 (capped at -1.0)
-- Reward multiplier is then tilted by up to +/-15% per tick (heat * 0.15),
-- stacking multiplicatively with the existing HOT/COLD event multiplier.
-- So a persistently hot validator at heat=1.0 on a HOT tick pays 2.0 * 1.15
-- = 2.3x, while a persistently cold one at heat=-1.0 on a COLD tick pays
-- 0.4 * 0.85 = 0.34x.
--
-- heat is stored as NUMERIC(6,4) so it can hold any value in [-1.0000, 1.0000]
-- with four decimal places of precision -- plenty for a dampened smoothing
-- scalar. Default 0 means existing and newly-seeded validators start neutral.
-- Safe to re-run: ADD COLUMN IF NOT EXISTS + existing rows get the default.

ALTER TABLE validators
    ADD COLUMN IF NOT EXISTS heat NUMERIC(6,4) NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'validators_heat_chk'
    ) THEN
        ALTER TABLE validators
            ADD CONSTRAINT validators_heat_chk
            CHECK (heat BETWEEN -1 AND 1) NOT VALID;
        ALTER TABLE validators VALIDATE CONSTRAINT validators_heat_chk;
    END IF;
END $$;
