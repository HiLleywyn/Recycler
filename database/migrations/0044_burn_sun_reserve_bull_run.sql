-- 0044: Burn all SUN held in the community savings reserve and trigger a
--       24-hour bull run across every guild.
--
-- The savings reserve (user_id = 0) accumulates SUN from protocol fees.
-- Removing it from circulating supply causes a permanent supply shock.
-- The bull run event then drives price upward for 24 hours.

BEGIN;

-- Step 1: Subtract the per-guild reserve balance from SUN circulating supply.
--         GREATEST(0, ...) prevents going negative if supply data is inconsistent.
UPDATE crypto_prices cp
SET circulating_supply = GREATEST(
    0,
    cp.circulating_supply - COALESCE(
        (SELECT SUM(sd.amount)
         FROM savings_deposits sd
         WHERE sd.user_id = 0
           AND sd.symbol = 'SUN'
           AND sd.guild_id = cp.guild_id),
        0
    )
)
WHERE cp.symbol = 'SUN';

-- Step 2: Delete the reserve deposits so they can never be double-counted.
DELETE FROM savings_deposits
WHERE user_id = 0
  AND symbol   = 'SUN';

-- Step 3: Activate a 24-hour bull run on every guild that has SUN.
--         event_bias = 1.5  →  ~150% annualised upward price drift per day
--         event_vol_mult = 1.6  →  60% more volatile than baseline
UPDATE guild_settings
SET current_event    = 'bull_run',
    event_bias       = 1.5,
    event_vol_mult   = 1.6,
    event_expires_at = NOW() + INTERVAL '24 hours'
WHERE guild_id IN (
    SELECT DISTINCT guild_id FROM crypto_prices WHERE symbol = 'SUN'
);

COMMIT;
