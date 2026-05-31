-- 0047_group_vault_tokens.sql
-- Group mining token vault: PoW network binding, per-block minting, locked LP

-- mining_groups: track which PoW network the group token is bound to,
--   its symbol (denormalised for fast lookup), and accumulated vault balance
ALTER TABLE mining_groups
    ADD COLUMN IF NOT EXISTS token_network   TEXT              DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS token_symbol    TEXT              DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS vault_token_bal NUMERIC(28,8) NOT NULL DEFAULT 0.0;

-- guild_tokens: vault_locked = TRUE → token cannot be traded, swapped,
--   transferred, or withdrawn by any player. Only the mining system may
--   increase/decrease its balance.
ALTER TABLE guild_tokens
    ADD COLUMN IF NOT EXISTS vault_locked BOOLEAN NOT NULL DEFAULT FALSE;

-- pools: vault_locked = TRUE → pool is read-only. No swaps, no LP deposits
--   or withdrawals. Reserves are updated only by the vault minting hook.
ALTER TABLE pools
    ADD COLUMN IF NOT EXISTS vault_locked BOOLEAN NOT NULL DEFAULT FALSE;
