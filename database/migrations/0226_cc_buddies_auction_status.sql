-- Add 'auction' to the cc_buddies status check constraint.
--
-- services/auction.py sets status='auction' when escrowing a buddy into
-- the auction house, but 'auction' was never included in the canonical
-- cc_buddies_status_chk. Migration 0220 dropped and recreated the
-- constraint with only ('owned','shelter','stored','nesting'), which
-- caused a CheckViolationError on every AH buddy listing attempt.
--
-- This migration is idempotent: drop + recreate is safe to re-run.

ALTER TABLE cc_buddies DROP CONSTRAINT IF EXISTS cc_buddies_status_chk;
ALTER TABLE cc_buddies ADD CONSTRAINT cc_buddies_status_chk
    CHECK (status IN ('owned', 'shelter', 'stored', 'nesting', 'auction'));
