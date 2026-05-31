-- Arena reward token changed from BUD to FREN.
-- Rename the per-user earned-amount counter to match the new currency
-- so column names reflect actual semantics.
ALTER TABLE user_buddy_economy
    RENAME COLUMN arena_bud_earned_raw TO arena_fren_earned_raw;
