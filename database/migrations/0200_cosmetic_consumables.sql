-- Migration 0200: cosmetic consumables inventory
-- Adds a JSONB column to users for tracking cosmetic-consumable counts.
-- Format: '{"starlight_aura": 2, "golden_badge": 1}'

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS cosmetics JSONB DEFAULT '{}'::jsonb NOT NULL;
