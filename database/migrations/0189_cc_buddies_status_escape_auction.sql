-- 0189_cc_buddies_status_escape_auction.sql
--
-- Re-adds 'escaped' and 'auction' to the cc_buddies.status CHECK
-- constraint. Migration 0169 (which added 'stored') dropped
-- 'escaped' from the previous {'owned', 'shelter', 'escaped'} set
-- when re-defining the constraint as {'owned', 'shelter', 'stored'}.
-- The escape-spawn flow (services.buddy_world.mark_escaped) and the
-- buddy AH listing flow (services.auction._lock_buddy +
-- create_listing_by_token) both set status to values that the
-- current constraint rejects -- ``,admin buddy spawn`` reproduces
-- the failure with CheckViolationError on every attempt.
--
-- Idempotent: drops + re-adds the CHECK with the full status set.
-- The pre-existing UPDATE in 0169 normalised any out-of-set rows to
-- 'shelter', and this migration only ADDS values to the set, so no
-- pre-flight cleanup is required -- the wider CHECK accepts every
-- existing row.

ALTER TABLE cc_buddies DROP CONSTRAINT IF EXISTS cc_buddies_status_chk;
ALTER TABLE cc_buddies ADD CONSTRAINT cc_buddies_status_chk
    CHECK (status IN ('owned', 'shelter', 'stored', 'escaped', 'auction'));
