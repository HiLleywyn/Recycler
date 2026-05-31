-- V3 unified indices pass.
--
-- Each V3 migration shipped with the indices its own queries need.
-- This file is the catch-all for cross-pillar query patterns the
-- per-pillar files missed: per-guild leaderboard queries against
-- the inbox, per-user mastery-by-time queries, per-pair clan-war
-- contribution searches.

CREATE INDEX IF NOT EXISTS user_inbox_unread_count_idx
    ON user_inbox (user_id) WHERE read_at IS NULL;

CREATE INDEX IF NOT EXISTS user_cosmetics_owned_recent_idx
    ON user_cosmetics_owned (user_id, granted_at DESC);

CREATE INDEX IF NOT EXISTS clan_war_contributions_user_match_idx
    ON clan_war_contributions (match_id, user_id, contributed_at DESC);
