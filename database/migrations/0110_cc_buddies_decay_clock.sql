-- Phase 2 of the CC Buddy system: mood decay.
--
-- Adds a dedicated decay clock so the background decay task can advance in
-- whole-hour steps (fractional hours accumulate on the column). This is
-- separate from last_interacted_at, which tracks neglect (runaway trigger)
-- and is bumped on feed/pet/talk.

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS last_decay_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Speeds up the decay sweep and the shelter adopt listing.
CREATE INDEX IF NOT EXISTS cc_buddies_decay_sweep_idx
    ON cc_buddies (last_decay_at)
    WHERE status = 'owned';
