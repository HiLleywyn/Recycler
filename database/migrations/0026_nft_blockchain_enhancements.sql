-- NFT blockchain enhancements: hashes, contracts, sale history fixes
-- Adds proper blockchain identity to NFTs (contract addresses, token hashes)
-- Fixes nft_sales schema for correct column references
-- Adds contract_address to collections, token_hash to individual NFTs

-- Collection contract addresses (deployed on-chain)
ALTER TABLE nft_collections ADD COLUMN IF NOT EXISTS contract_address TEXT NOT NULL DEFAULT '';

-- Individual NFT token hashes (unique on-chain identifier)
ALTER TABLE nfts ADD COLUMN IF NOT EXISTS token_hash TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_nfts_token_hash ON nfts(token_hash) WHERE token_hash != '';

-- nft_sales was missing from base schema.sql  -  ensure it exists
CREATE TABLE IF NOT EXISTS nft_sales (
    id            SERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    nft_id        INT NOT NULL REFERENCES nfts(id),
    collection_id INT NOT NULL REFERENCES nft_collections(id),
    seller_id     BIGINT NOT NULL,
    buyer_id      BIGINT NOT NULL,
    price         NUMERIC(20,8) NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    sold_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nft_sales_nft ON nft_sales(nft_id);
CREATE INDEX IF NOT EXISTS idx_nft_sales_collection ON nft_sales(collection_id);
CREATE INDEX IF NOT EXISTS idx_nft_sales_guild ON nft_sales(guild_id);

-- Ensure slot_metadata and is_locked exist (from 0025 but may be missing)
ALTER TABLE nft_collections ADD COLUMN IF NOT EXISTS slot_metadata JSONB NOT NULL DEFAULT '[]';
ALTER TABLE nft_collections ADD COLUMN IF NOT EXISTS is_locked BOOLEAN NOT NULL DEFAULT FALSE;
