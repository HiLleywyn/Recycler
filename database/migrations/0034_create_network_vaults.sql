-- Migration: Ensure network_vaults table exists
-- Fixes: "Relation network_vaults does not exist" on servers where
-- migration 0027 was recorded as applied without the table being created.

CREATE TABLE IF NOT EXISTS network_vaults (
    guild_id   BIGINT        NOT NULL,
    network    TEXT          NOT NULL,
    balance    NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    level      INTEGER       NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, network),
    CONSTRAINT fk_vault_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_vault_balance CHECK (balance >= 0)
);
