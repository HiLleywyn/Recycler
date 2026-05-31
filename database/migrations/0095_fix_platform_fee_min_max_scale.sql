-- Migration 0095: Fix platform_fee_min / platform_fee_max values saved without
-- to_raw() scaling.
--
-- The PATCH /fee-settings API endpoint was missing the to_raw() conversion for
-- platform_fee_min and platform_fee_max (NUMERIC(36,0)), so dashboards that
-- saved fee settings through that endpoint stored human-scale numbers that got
-- rounded by the 0-scale NUMERIC column.  For example:
--   platform_fee_max = 20.00  ->  stored as 20    ->  to_human = 2e-17
--   platform_fee_min =  0.10  ->  stored as  0    ->  falsy -> falls back
-- After to_human the cap becomes effectively zero, clamping every wallet fee
-- to $0.  Any stored value below 10^15 (= $0.001 human-scale) cannot be a
-- legitimately-scaled fee -- rescale it by 10^18.
--
-- Values of 0 are left alone; the repo layer treats them as "unset" and
-- falls back to Config.WALLET_PLATFORM_FEE_MIN / _MAX.

UPDATE guild_settings
SET platform_fee_min = platform_fee_min * 1000000000000000000
WHERE platform_fee_min IS NOT NULL
  AND platform_fee_min > 0
  AND platform_fee_min < 1000000000000000;

UPDATE guild_settings
SET platform_fee_max = platform_fee_max * 1000000000000000000
WHERE platform_fee_max IS NOT NULL
  AND platform_fee_max > 0
  AND platform_fee_max < 1000000000000000;
