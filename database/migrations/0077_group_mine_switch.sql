-- 0077_group_mine_switch.sql
-- Add mine_switched_at to mining_groups for group mine cooldown tracking.
ALTER TABLE mining_groups ADD COLUMN IF NOT EXISTS mine_switched_at TIMESTAMPTZ;
