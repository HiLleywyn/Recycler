-- Cache the member's display name so leaderboards can render a real name
-- for users who left the guild or were imported from another server. Kept
-- fresh on every XP grant from the current member's display_name.
ALTER TABLE chat_levels
    ADD COLUMN IF NOT EXISTS display_name TEXT;
