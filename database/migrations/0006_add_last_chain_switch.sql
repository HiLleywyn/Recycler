-- Add last_chain_switch column to user_mining_config for chain-switch cooldown.
-- Tracks when a user last reassigned rigs between chains to enforce a cooldown
-- preventing chain-hopping exploits.
ALTER TABLE user_mining_config ADD COLUMN IF NOT EXISTS last_chain_switch TIMESTAMPTZ;
