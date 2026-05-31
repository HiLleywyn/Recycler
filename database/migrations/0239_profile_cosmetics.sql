-- V3 Pillar 4: profile cosmetics
--
-- Per-user (NOT per-guild) identity layer: Title, Banner, Frame, Sigil.
-- Migrations 0200/0202/0204 cover *role*-style cosmetics; those keep
-- their tables. This layer is additive: it gives players a way to
-- present an identity on the profile card across every server.

CREATE TABLE IF NOT EXISTS user_cosmetics_owned (
    user_id      BIGINT      NOT NULL,
    item_id      TEXT        NOT NULL,
    granted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT        NOT NULL DEFAULT 'system',
    PRIMARY KEY (user_id, item_id)
);

CREATE INDEX IF NOT EXISTS user_cosmetics_owned_user_idx
    ON user_cosmetics_owned (user_id, granted_at DESC);

CREATE TABLE IF NOT EXISTS user_cosmetics_equipped (
    user_id      BIGINT      NOT NULL,
    slot         TEXT        NOT NULL CHECK (slot IN ('title', 'banner', 'frame', 'sigil')),
    item_id      TEXT        NOT NULL,
    equipped_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, slot)
);
