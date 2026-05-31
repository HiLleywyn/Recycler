-- Add a "source" column to ai_user_traits so passive-chat extraction can
-- be distinguished from the existing tone / reaction / behavior signals.
-- The existing decay + promotion math doesn't change; this just lets us
-- query "where did this trait come from?" so admins can see what the
-- passive learner contributed.
ALTER TABLE ai_user_traits
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'event';

-- Partial index so queries that filter by source (e.g. "show me everything
-- the auto-learner has accumulated for this user") stay fast without
-- bloating the main b-tree for the common source='event' path.
CREATE INDEX IF NOT EXISTS ai_user_traits_source_idx
    ON ai_user_traits (source)
    WHERE source <> 'event';
