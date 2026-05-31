-- NFT collection gallery: per-slot images for sequential assignment during minting.
-- Each slot maps to one token_id (1-indexed). When a token is minted, its token_id
-- is used to look up the gallery slot; if found, that image is used instead of the
-- collection-level image_url.

CREATE TABLE nft_collection_images (
    id           SERIAL PRIMARY KEY,
    collection_id INT NOT NULL REFERENCES nft_collections(id) ON DELETE CASCADE,
    slot         INT NOT NULL,          -- 1-indexed, matches token_id
    image_url    TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, slot)
);

CREATE INDEX idx_nft_collection_images_collection ON nft_collection_images (collection_id);
