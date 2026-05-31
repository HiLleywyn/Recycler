-- 0233_gamba_ai_difficulty.sql
--
-- Per-match AI difficulty for chess and checkers PvE. Players were
-- losing every PvE match because the AI search depth was hardcoded
-- (chess depth=2, checkers depth=4) -- competent enough to crush
-- casual gamblers who just wanted a soft VEIN/GAMBIT/CROWN faucet.
-- The new column lets ,chess play and ,checkers play accept an
-- "easy" / "normal" / "hard" arg; the cog picks search depth from
-- it. Default 'normal' so existing rows keep their current strength.

ALTER TABLE gamba_chess_matches
    ADD COLUMN IF NOT EXISTS ai_difficulty TEXT NOT NULL DEFAULT 'normal';

ALTER TABLE gamba_checkers_matches
    ADD COLUMN IF NOT EXISTS ai_difficulty TEXT NOT NULL DEFAULT 'normal';
