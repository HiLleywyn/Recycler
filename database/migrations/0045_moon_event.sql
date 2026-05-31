-- 0045: One-time moon event  -  supply expansion + event activation.
--
-- Supply: all token circulating supplies increased by 50%.
-- Event: activates 'moon' for 24h. The oracle (cogs/trade.py _drift_guild)
--        detects this event and applies per-token bias overrides automatically:
--        regular tokens → ~30% daily drift, network coins → ~80% daily drift,
--        daily circuit breaker disabled for non-stablecoins during moon.
--        No manual event_bias/event_vol_mult override needed in DB.

BEGIN;

-- Step 1: Expand circulating supply of all built-in tokens by 50%.
UPDATE crypto_prices
SET circulating_supply = circulating_supply * 1.5
WHERE circulating_supply > 0;

-- Step 2: Expand circulating supply of all custom guild tokens by 50%.
UPDATE guild_tokens
SET circulating_supply = circulating_supply * 1.5
WHERE circulating_supply > 0;

-- Step 3: Activate moon event on all guilds for 24 hours.
UPDATE guild_settings
SET current_event    = 'moon',
    event_expires_at = NOW() + INTERVAL '24 hours';

COMMIT;
