-- Safety Module auto-compound: when enabled, hourly staking_tick
-- re-stakes earned yield back into the position instead of paying
-- it out to the DeFi wallet.
ALTER TABLE safety_module_stakes
    ADD COLUMN IF NOT EXISTS auto_compound BOOLEAN NOT NULL DEFAULT FALSE;
