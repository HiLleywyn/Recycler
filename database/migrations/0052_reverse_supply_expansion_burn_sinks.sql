-- 0052: Reverse migration 0045's permanent supply expansion, clean reserve_sun.
--
-- Migration 0045 inflated all circulating supplies by 1.5x as a one-shot "moon
-- event" supply shock. This permanently diluted every token, killing price
-- momentum. This migration undoes that expansion.
--
-- Also zeros reserve_sun in mining_groups  -  that column is dead (all new
-- accumulation uses reserve_usd). Historical SUN that accumulated there
-- is acknowledged as permanently removed from active circulation.

-- Reverse the 0045 supply expansion on built-in tokens.
UPDATE crypto_prices
SET circulating_supply = ROUND((circulating_supply / 1.5)::numeric, 8)
WHERE circulating_supply > 0;

-- Reverse the 0045 supply expansion on custom guild tokens.
UPDATE guild_tokens
SET circulating_supply = ROUND((circulating_supply / 1.5)::numeric, 8)
WHERE circulating_supply > 0;

-- Zero out legacy reserve_sun (guarded in case column was already removed).
DO $$
BEGIN
    IF EXISTS (SELECT FROM information_schema.columns
               WHERE table_name = 'mining_groups' AND column_name = 'reserve_sun') THEN
        UPDATE mining_groups SET reserve_sun = 0 WHERE reserve_sun > 0;
    END IF;
END $$;
