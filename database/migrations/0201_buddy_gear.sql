-- Migration 0201: buddy gear (equipment) slots
-- Adds a JSONB gear column to cc_buddies for equippable items.
-- Format: '{"accessory": "flower_crown", "charm": "lucky_bell"}'

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS gear JSONB DEFAULT '{}'::jsonb NOT NULL;
