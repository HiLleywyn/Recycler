-- Moons: Lunar Mint autocompound preference.
--
-- When TRUE, MOON emitted by the Lunar Mint for this user's staked group
-- tokens is automatically deposited into the Moon Pool (Tier 2) on the
-- same tick, instead of landing in the user's Moon Network wallet.
--
-- See cogs/moons.py :: _tick_row for the tick-time check and
-- cogs/moons.py :: moon_autocompound for the user-facing toggle.

ALTER TABLE user_prefs
    ADD COLUMN IF NOT EXISTS moon_autocompound BOOLEAN NOT NULL DEFAULT FALSE;
