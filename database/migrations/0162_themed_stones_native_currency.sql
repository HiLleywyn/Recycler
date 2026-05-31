-- Phase C followup: themed stones were originally created with
-- lp_currency='DSD' (the schema default) before the per-stone
-- accepted_currencies migration shipped. ,inventory and ,bal therefore
-- show "DSD staked" for every stone, even though tide/heart/crypt/blood
-- /bloomstones now have a single canonical native currency:
--
--   tidestones  -> REEL  (Lure Network)
--   heartstones -> BUD   (Buddy Network)
--   cryptstones -> RUNE  (Crypt Network)
--   bloodstones -> BBT   (Buddy Battle)
--   bloomstones -> HRV   (Harvest Network)
--   vaultstones -> USD   (bare wallet)
--
-- This migration flips every legacy DSD row to its canonical currency.
-- The staked_amount column is rescaled via the per-guild crypto_prices
-- row so the displayed token amount matches the original USD value:
--   new_staked = old_staked_dsd_raw / oracle_price
-- For vaultstones the staked_amount stays the same (USD is also pegged
-- to $1 so no rescale is needed; the row was written in raw 1e18 USD).

-- Tidestones: DSD -> REEL.
UPDATE tidestones t
   SET lp_currency  = 'REEL',
       staked_amount = (
            t.staked_amount::NUMERIC / GREATEST(p.price, 0.0001)
       )::NUMERIC(36,0)
  FROM crypto_prices p
 WHERE p.symbol   = 'REEL'
   AND p.guild_id = t.guild_id
   AND t.lp_currency = 'DSD';

-- If REEL has no oracle row for a guild, just flip the symbol so the
-- display stops lying. Numbers stay as-is (1 REEL = $1 fallback).
UPDATE tidestones SET lp_currency = 'REEL' WHERE lp_currency = 'DSD';

-- Heartstones: DSD -> BUD.
UPDATE heartstones h
   SET lp_currency  = 'BUD',
       staked_amount = (
            h.staked_amount::NUMERIC / GREATEST(p.price, 0.0001)
       )::NUMERIC(36,0)
  FROM crypto_prices p
 WHERE p.symbol   = 'BUD'
   AND p.guild_id = h.guild_id
   AND h.lp_currency = 'DSD';
UPDATE heartstones SET lp_currency = 'BUD' WHERE lp_currency = 'DSD';

-- Cryptstones: DSD -> RUNE.
UPDATE cryptstones c
   SET lp_currency  = 'RUNE',
       staked_amount = (
            c.staked_amount::NUMERIC / GREATEST(p.price, 0.0001)
       )::NUMERIC(36,0)
  FROM crypto_prices p
 WHERE p.symbol   = 'RUNE'
   AND p.guild_id = c.guild_id
   AND c.lp_currency = 'DSD';
UPDATE cryptstones SET lp_currency = 'RUNE' WHERE lp_currency = 'DSD';

-- Bloodstones: DSD -> BBT.
UPDATE bloodstones b
   SET lp_currency  = 'BBT',
       staked_amount = (
            b.staked_amount::NUMERIC / GREATEST(p.price, 0.0001)
       )::NUMERIC(36,0)
  FROM crypto_prices p
 WHERE p.symbol   = 'BBT'
   AND p.guild_id = b.guild_id
   AND b.lp_currency = 'DSD';
UPDATE bloodstones SET lp_currency = 'BBT' WHERE lp_currency = 'DSD';

-- Bloomstones: DSD -> HRV.
UPDATE bloomstones bm
   SET lp_currency  = 'HRV',
       staked_amount = (
            bm.staked_amount::NUMERIC / GREATEST(p.price, 0.0001)
       )::NUMERIC(36,0)
  FROM crypto_prices p
 WHERE p.symbol   = 'HRV'
   AND p.guild_id = bm.guild_id
   AND bm.lp_currency = 'DSD';
UPDATE bloomstones SET lp_currency = 'HRV' WHERE lp_currency = 'DSD';

-- Vaultstones: DSD -> USD (bare-wallet path; no oracle rescale needed
-- because both DSD and USD are pegged $1).
UPDATE vaultstones SET lp_currency = 'USD' WHERE lp_currency = 'DSD';
