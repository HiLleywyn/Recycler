-- Buddy nest fix: introduce a fourth status 'nesting' alongside
-- 'owned' / 'stored' / 'shelter'. Parents deposited into the nest
-- (cc_buddy_daycare) flip to status='nesting' so they no longer
-- compete for the user's battle (status='owned') or storage
-- (status='stored') slot caps. They're still owned by the user
-- (owner_user_id stays populated) and still visible via ,buddy nest,
-- but they:
--   * don't count against MAX_OWNED_BUDDIES / battle slot cap
--   * don't count against the storage slot cap
--   * are excluded from mood-decay, runaway, expedition, and battle
--     SELECTs (those all filter on status = 'owned')
--   * are NOT for-sale eligible (market filters on status = 'owned')
-- On collect/cancel the parents are returned: prefer 'owned' if there
-- is room under the battle cap, otherwise 'stored' if there is room
-- under the storage cap, otherwise the action is refused with a clear
-- "free a slot first" error. See services/buddy_breeding.py for the
-- runtime logic.
--
-- Idempotent: drops + re-adds the CHECK; safe to re-run. Any row
-- already outside the new four-value set is normalised to 'shelter'
-- the same way 0169 normalised pre-'stored' rows.

UPDATE cc_buddies SET status = 'shelter'
 WHERE status IS NULL
    OR status NOT IN ('owned', 'shelter', 'stored', 'nesting');

ALTER TABLE cc_buddies DROP CONSTRAINT IF EXISTS cc_buddies_status_chk;
ALTER TABLE cc_buddies ADD CONSTRAINT cc_buddies_status_chk
    CHECK (status IN ('owned', 'shelter', 'stored', 'nesting'));

-- Lookup index for the nest panel: nesting rows are owner-attached
-- and cogs query by (guild_id, owner_user_id) to render parent state
-- alongside the cc_buddy_daycare slot row.
CREATE INDEX IF NOT EXISTS cc_buddies_nesting_idx
    ON cc_buddies (guild_id, owner_user_id)
    WHERE status = 'nesting';
