-- 0273_sage_shop.sql
--
-- Sage Shop: per-user inventory of SAGE-priced consumables.
--
--   * sage_items holds one row per (user, guild, item_key) with the owned
--     quantity. Item keys are config-driven (sage_config.SAGE_SHOP_ITEMS),
--     so there is intentionally no CHECK constraint on item_key -- adding a
--     new shop item must never require a schema migration.
--   * Items are bought with SAGE (services/sage.buy_item burns from the
--     Sage Network wallet) and consumed on the next run.

CREATE TABLE IF NOT EXISTS sage_items (
    user_id     BIGINT       NOT NULL,
    guild_id    BIGINT       NOT NULL,
    item_key    TEXT         NOT NULL,
    qty         INTEGER      NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id, item_key),
    CONSTRAINT chk_sage_items_qty CHECK (qty >= 0)
);

CREATE INDEX IF NOT EXISTS idx_sage_items_owned
    ON sage_items (guild_id, user_id)
    WHERE qty > 0;
