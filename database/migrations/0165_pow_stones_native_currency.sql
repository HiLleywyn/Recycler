-- Phase C followup #2: hashstone / lockstone / liqstone accepted_currencies
-- were narrowed in items_config.py so each stone's stake currency
-- reflects its actual purpose:
--
--   hashstone (PoW mining gear) -> ("BTC", "SUN")
--   lockstone (PoS staking gear) -> ("DSC", "ETH")
--   liqstone  (LP gear, $-denom)  -> ("DSD", "USDC")
--
-- Existing rows were created with lp_currency in the legacy DSD/USDC/
-- DSC/ETH set. ,inv / ,bal show "DSD staked" for hashstones and
-- lockstones because the schema default is 'DSD' and was never updated.
-- This migration flips every legacy lp_currency to the canonical first
-- accepted symbol and rescales staked_amount via the per-guild oracle
-- so the displayed token amount matches the original USD value:
--   new_staked = old_staked_raw / oracle_price
-- For liqstone we keep DSD/USDC rows where they are (still in the new
-- accepted_currencies set) and flip only DSC/ETH legacy rows back to
-- DSD with rescale.

-- Hashstones: any non-(BTC, SUN) -> BTC.
UPDATE hashstones h
   SET lp_currency  = 'BTC',
       staked_amount = (
            h.staked_amount::NUMERIC / GREATEST(p.price, 0.0001)
       )::NUMERIC(36,0)
  FROM crypto_prices p
 WHERE p.symbol   = 'BTC'
   AND p.guild_id = h.guild_id
   AND h.lp_currency NOT IN ('BTC', 'SUN');
UPDATE hashstones SET lp_currency = 'BTC'
 WHERE lp_currency NOT IN ('BTC', 'SUN');

-- Lockstones: any non-(DSC, ETH) -> DSC.
UPDATE lockstones l
   SET lp_currency  = 'DSC',
       staked_amount = (
            l.staked_amount::NUMERIC / GREATEST(p.price, 0.0001)
       )::NUMERIC(36,0)
  FROM crypto_prices p
 WHERE p.symbol   = 'DSC'
   AND p.guild_id = l.guild_id
   AND l.lp_currency NOT IN ('DSC', 'ETH');
UPDATE lockstones SET lp_currency = 'DSC'
 WHERE lp_currency NOT IN ('DSC', 'ETH');

-- Liqstones: DSC/ETH legacy rows back to DSD ($1 peg means a 1:1
-- value conversion via the DSC/ETH oracle).
UPDATE liqstones q
   SET lp_currency  = 'DSD',
       staked_amount = (
            q.staked_amount::NUMERIC * GREATEST(p.price, 0.0001)
       )::NUMERIC(36,0)
  FROM crypto_prices p
 WHERE p.symbol   IN ('DSC', 'ETH')
   AND p.symbol   = q.lp_currency
   AND p.guild_id = q.guild_id
   AND q.lp_currency NOT IN ('DSD', 'USDC');
UPDATE liqstones SET lp_currency = 'DSD'
 WHERE lp_currency NOT IN ('DSD', 'USDC');
