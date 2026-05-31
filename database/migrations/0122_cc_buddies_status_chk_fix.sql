-- Fix the cc_buddies status CHECK constraint so 'escaped' is accepted.
--
-- Background: migration 0121 tried to drop+recreate the check under the
-- name cc_buddies_status_CHECK, but the original constraint from 0109
-- is named cc_buddies_status_CHK. Result on existing deploys:
--   * the old _chk constraint was never dropped (still rejects 'escaped')
--   * a second _check constraint was added alongside it (allows 'escaped')
-- Any INSERT/UPDATE that tries to set status='escaped' fails on the old
-- _chk constraint with:
--   "new row violates check constraint "cc_buddies_status_chk""
--
-- This migration is idempotent and converges both deploy states:
--   * freshly-migrated DBs (both constraints present)
--   * brand-new DBs that haven't applied 0121 yet
--   * DBs that somehow lost one constraint
-- by dropping both possible names and recreating a single canonical
-- cc_buddies_status_chk that allows all three valid statuses.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddies_status_check'
    ) THEN
        ALTER TABLE cc_buddies DROP CONSTRAINT cc_buddies_status_check;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddies_status_chk'
    ) THEN
        ALTER TABLE cc_buddies DROP CONSTRAINT cc_buddies_status_chk;
    END IF;

    ALTER TABLE cc_buddies
        ADD CONSTRAINT cc_buddies_status_chk
        CHECK (status IN ('owned', 'shelter', 'escaped'));
END $$;
