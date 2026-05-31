-- Arena pays BUD + BBT, never FREN. The earlier arena ship paid FREN by
-- mistake -- FREN is the buddy-interaction loop currency (talk/feed/pet
-- drops) and the arena reward token is BUD (the network's stake-yield
-- token) plus BBT (the cross-game battle token). This migration aligns
-- the persisted column with the corrected reward path.
--
-- Rename arena_fren_earned_raw -> arena_bud_earned_raw on
-- user_buddy_economy. Existing FREN-earned values are preserved as the
-- starting BUD-earned counter so leaderboards and totals stay consistent
-- across the change. New writes go to arena_bud_earned_raw via the
-- updated resolve_arena_battle path in services/buddy_economy.py.

ALTER TABLE user_buddy_economy
    RENAME COLUMN arena_fren_earned_raw TO arena_bud_earned_raw;
