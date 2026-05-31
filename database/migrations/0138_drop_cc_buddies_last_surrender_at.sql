-- Drop cc_buddy_hatches.last_surrender_at.
--
-- The column was added in migration 0120 to back the 7-day surrender ->
-- rehatch cooldown, which existed to block users from cheaply rerolling
-- species / rarity by surrendering and re-hatching outside the limited
-- free-reroll counter.
--
-- Migration 0137 introduced paid hatching (first HATCH_FREE_COUNT free,
-- then a doubling USD fee with a 7-day idle reset). Paid pricing is now
-- the gate -- a player who surrenders a buddy and immediately re-hatches
-- pays at least HATCH_BASE_PRICE_USD per attempt, scaling exponentially
-- per streak. The cooldown is redundant with the cost curve, so the cog
-- no longer reads or writes this column. Drop it so the schema does not
-- carry a dead field.
--
-- Safe to drop unconditionally: the only writer (cogs/buddy.py surrender
-- command) and the only reader (cogs/buddy.py hatch command) were both
-- updated in the same change that ships this migration.

ALTER TABLE cc_buddy_hatches
    DROP COLUMN IF EXISTS last_surrender_at;
