-- Migration 0057: extend economy_snapshots with stone and LP position data.
-- Without these, a rollback could restore wallet balances while leaving
-- stone purchases or LP deposits intact, causing double-spend.

ALTER TABLE economy_snapshots
    ADD COLUMN IF NOT EXISTS stones       JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS lp_positions JSONB NOT NULL DEFAULT '[]';
