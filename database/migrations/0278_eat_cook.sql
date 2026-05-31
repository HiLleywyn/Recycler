-- 0278_eat_cook.sql
--
-- Adds the "cook" pre-attack prep buff to Eat the Rich. ,eat cook spends a
-- USD fee and arms a one-shot success-odds bonus on the next ,eat made
-- within the buff window. The buff is stored as a single expiry timestamp
-- on the existing exploit_stats row -- while cook_until is in the future the
-- next eat applies Config.EAT_COOK_BONUS, then clears the column.
--
-- ADD COLUMN IF NOT EXISTS keeps this safe on databases that already have
-- exploit_stats (migration 0038).

ALTER TABLE exploit_stats
    ADD COLUMN IF NOT EXISTS cook_until TIMESTAMPTZ;
