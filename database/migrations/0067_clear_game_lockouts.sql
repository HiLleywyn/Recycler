-- Clear all antibot game lockouts. The antibot system is disabled.
UPDATE users SET game_lockout_until = NULL WHERE game_lockout_until IS NOT NULL;
