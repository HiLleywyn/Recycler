-- 0229_gamba_match_auto_bump.sql
--
-- Per-match "auto bump" toggle for chess and checkers. When ON, the
-- cog re-posts the match panel at the bottom of the channel after the
-- AI's reply (PvE) or the opponent's move (PvP) so the player whose
-- turn it now is can't lose track of the panel as the channel scrolls
-- past it. The toggle button on the in-game view flips the flag.

ALTER TABLE gamba_chess_matches
    ADD COLUMN IF NOT EXISTS auto_bump BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE gamba_checkers_matches
    ADD COLUMN IF NOT EXISTS auto_bump BOOLEAN NOT NULL DEFAULT FALSE;
