-- Add game_lockout_until column to users table for antibot lockout tracking.
ALTER TABLE users ADD COLUMN IF NOT EXISTS game_lockout_until TIMESTAMPTZ;
