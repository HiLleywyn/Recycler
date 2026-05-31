-- Migration: Create per-network vault system for server progression
-- Replaces the single guild_treasury with per-network vaults that track
-- fee revenue and unlock server levels at defined thresholds.

CREATE TABLE IF NOT EXISTS network_vaults (
    guild_id   BIGINT        NOT NULL,
    network    TEXT          NOT NULL,   -- 'sun', 'btc', 'eth', 'dsc'
    balance    NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    level      INTEGER       NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, network),
    CONSTRAINT fk_vault_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    CONSTRAINT chk_vault_balance CHECK (balance >= 0)
);

-- Add a guild setting column for the vault feed channel
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS vault_feed_channel BIGINT DEFAULT NULL;
