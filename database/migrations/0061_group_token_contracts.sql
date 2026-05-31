-- 0061_group_token_contracts.sql
-- Add on-chain identity fields to guild_tokens and a per-home features table.
-- Group tokens get a contract_address + token_hash mirroring the NFT pattern.
-- trading_enabled controls whether an admin has explicitly unlocked the token
-- for player trading (default FALSE - group tokens start locked).

ALTER TABLE guild_tokens
    ADD COLUMN IF NOT EXISTS contract_address TEXT    DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS token_hash       TEXT    DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS trading_enabled  BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_guild_tokens_contract_address
    ON guild_tokens (contract_address)
    WHERE contract_address IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_guild_tokens_token_hash
    ON guild_tokens (token_hash)
    WHERE token_hash IS NOT NULL;
