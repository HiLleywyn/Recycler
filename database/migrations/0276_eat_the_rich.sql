-- 0276_eat_the_rich.sql
--
-- The ,exploit / ,pvp "Crypto Heist" minigame has been reformulated into the
-- "Eat the Rich" game (cogs/eat_the_rich.py). The new game has no opt-in:
-- everyone is fair game, but you can only eat players richer than you.
--
-- That makes the PvP opt-in flag and the toggle-lock timestamp dead columns,
-- so they are dropped here. DROP COLUMN IF EXISTS keeps this safe whether or
-- not the columns were ever created on a given database.
--
-- The exploit_shields / exploit_stats / exploit_history tables are kept as-is
-- (the new game reuses them) so live player records are not disturbed.

ALTER TABLE user_prefs DROP COLUMN IF EXISTS pvp_enabled;
ALTER TABLE user_prefs DROP COLUMN IF EXISTS pvp_last_exploit;
