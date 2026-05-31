-- Ensure nft_collection_images exists on databases that missed migration 0028.
-- Safe to run on any DB: IF NOT EXISTS guards prevent errors if already present.

CREATE TABLE IF NOT EXISTS nft_collection_images (
    id            SERIAL PRIMARY KEY,
    collection_id INT NOT NULL REFERENCES nft_collections(id) ON DELETE CASCADE,
    slot          INT NOT NULL,
    image_url     TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, slot)
);

CREATE INDEX IF NOT EXISTS idx_nft_collection_images_collection
    ON nft_collection_images (collection_id);
