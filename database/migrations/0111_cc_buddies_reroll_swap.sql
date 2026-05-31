-- Buddy reroll + paid species swap.
--
-- Reroll:  up to 3 free re-rolls per lifetime. Replaces the user's active
--          buddy with a newly rolled species + name; the old buddy is
--          HARD-DELETED (not sent to shelter). Counter lives on the
--          append-only hatch log so it persists through surrender + re-adopt.
--
-- Swap:    paid species change. Keeps stats, XP, level, mood, and hunger;
--          only species + name change. Price doubles each time: $1M, $2M,
--          $4M, $8M, ... Counter is a column on the buddy itself so it
--          resets if the buddy is surrendered and a new one is adopted.

ALTER TABLE cc_buddy_hatches
    ADD COLUMN IF NOT EXISTS reroll_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS swap_count INTEGER NOT NULL DEFAULT 0;
