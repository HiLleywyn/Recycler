-- Buddy "computer" storage: a third status alongside 'owned' / 'shelter'
-- where a player can stash spare buddies without surrendering them. Stored
-- buddies keep their owner_user_id but are excluded from the active /
-- max-owned counts and from the mood-decay sweep.
--
-- Idempotent: drops + re-adds the CHECK constraint and the lookup index.
--
-- Production note: the original migration (which dropped + re-added the
-- constraint without a normalisation step first) crashed at startup on
-- environments that had a row whose ``status`` was outside the original
-- {'owned', 'shelter'} set (e.g. legacy / hand-edited rows that pre-dated
-- the original CHECK). The UPDATE below normalises any such row into
-- 'shelter' so re-adding the tightened CHECK never trips. Safe no-op when
-- the column is already clean.

UPDATE cc_buddies SET status = 'shelter'
 WHERE status IS NULL
    OR status NOT IN ('owned', 'shelter', 'stored');

ALTER TABLE cc_buddies DROP CONSTRAINT IF EXISTS cc_buddies_status_chk;
ALTER TABLE cc_buddies ADD CONSTRAINT cc_buddies_status_chk
    CHECK (status IN ('owned', 'shelter', 'stored'));

-- Lookup index for the per-user storage panel. Stored rows are owned-by-
-- somebody (so the partial index has a populated owner_user_id), and the
-- panel queries always filter on guild + user.
CREATE INDEX IF NOT EXISTS cc_buddies_stored_idx
    ON cc_buddies (guild_id, owner_user_id)
    WHERE status = 'stored';
