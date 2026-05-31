-- Migration 0081: Fix vault-locked pool reserves that were stored without 10^18 scaling.
--
-- create_vault_pool() and vault_add_to_pool() previously inserted human-scale
-- floats (e.g. 1.0, 500.0) directly into NUMERIC(36,0) columns that expect
-- 10^18-scaled integers.  Any vault pool with reserve_a < 10^9 was stored
-- without scaling.  Multiply those reserves by 10^18 to correct them.
--
-- Non-vault pools were always seeded before migration 0075 (which scaled them)
-- and are never re-seeded (ON CONFLICT DO NOTHING), so they are unaffected.

UPDATE pools
SET reserve_a = ROUND(reserve_a * 1000000000000000000),
    reserve_b = ROUND(reserve_b * 1000000000000000000),
    total_lp  = ROUND(total_lp  * 1000000000000000000)
WHERE vault_locked = TRUE
  AND reserve_a < 1000000000;
