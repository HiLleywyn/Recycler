-- NFT collections and marketplace

CREATE TABLE IF NOT EXISTS nft_collections (
    id            SERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    name          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    network       TEXT NOT NULL DEFAULT 'ETH',
    description   TEXT NOT NULL DEFAULT '',
    image_url     TEXT NOT NULL DEFAULT '',
    max_supply    INT,
    mint_price    NUMERIC(20,8) NOT NULL DEFAULT 0,
    mint_token    TEXT NOT NULL DEFAULT 'ETH',
    minted_count  INT NOT NULL DEFAULT 0,
    creator_id    BIGINT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(guild_id, symbol)
);

CREATE TABLE IF NOT EXISTS nfts (
    id             SERIAL PRIMARY KEY,
    guild_id       BIGINT NOT NULL,
    collection_id  INT NOT NULL REFERENCES nft_collections(id),
    token_id       INT NOT NULL,
    owner_id       BIGINT NOT NULL,
    name           TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    image_url      TEXT NOT NULL DEFAULT '',
    rarity         TEXT NOT NULL DEFAULT 'common',
    metadata       JSONB NOT NULL DEFAULT '{}',
    minted_by      BIGINT NOT NULL,
    minted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(collection_id, token_id)
);
CREATE INDEX IF NOT EXISTS idx_nfts_owner ON nfts(owner_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_nfts_collection ON nfts(collection_id);

CREATE TABLE IF NOT EXISTS nft_listings (
    id          SERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    nft_id      INT NOT NULL REFERENCES nfts(id) ON DELETE CASCADE,
    seller_id   BIGINT NOT NULL,
    price       NUMERIC(20,8) NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'USD',
    listed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(nft_id)
);
