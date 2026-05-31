-- Time-limited cosmetic role grants.
--
-- Cosmetics are no longer toggle-on-toggle-off; using one grants the
-- linked Discord role for a fixed duration (default 1 hour, see
-- items_config.py:<cosmetic>.duration_seconds) and the role is removed
-- automatically when the grant expires. Each (user, guild, item) pair
-- can have a row at a time; re-using the same cosmetic before expiry
-- refreshes the deadline (handled in cogs/shop.py).

CREATE TABLE IF NOT EXISTS cosmetic_role_grants (
    id          BIGSERIAL    PRIMARY KEY,
    user_id     BIGINT       NOT NULL,
    guild_id    BIGINT       NOT NULL,
    item_key    TEXT         NOT NULL,
    role_id     BIGINT       NOT NULL,
    granted_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ  NOT NULL,
    UNIQUE (user_id, guild_id, item_key)
);

CREATE INDEX IF NOT EXISTS idx_cosmetic_role_grants_expires
    ON cosmetic_role_grants (expires_at);

CREATE INDEX IF NOT EXISTS idx_cosmetic_role_grants_user_guild
    ON cosmetic_role_grants (user_id, guild_id);
