-- Migration: Add faucet module columns
-- Adds module_faucet, faucet_multiplier, and faucet_tokens to guild_settings.
-- Copies existing module_drops value to module_faucet so current servers keep their setting.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS module_faucet    BOOLEAN      DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS faucet_multiplier NUMERIC(28,8) DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS faucet_tokens    TEXT         NOT NULL DEFAULT '';

-- Inherit the old module_drops toggle so servers that disabled drops stay disabled for faucet
UPDATE guild_settings
   SET module_faucet = module_drops
 WHERE module_drops IS NOT NULL;
