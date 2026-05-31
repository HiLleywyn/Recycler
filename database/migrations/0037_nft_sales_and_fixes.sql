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

ALTER TABLE nft_collections ADD COLUMN IF NOT EXISTS slot_metadata JSONB NOT NULL DEFAULT '[]';
ALTER TABLE nft_collections ADD COLUMN IF NOT EXISTS is_locked BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE nft_listings ALTER COLUMN currency SET DEFAULT 'ETH';
