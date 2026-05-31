-- V3 Pillar 7: onboarding deck progress tracker
--
-- Existing services/onboarding.py is a skeleton without progress
-- persistence. V3's ``,start`` flow walks a new player through five
-- PNG cards (wallet / earn / trade / buddy / mastery) and stores
-- which cards they've completed so reruns resume where they left off.

CREATE TABLE IF NOT EXISTS user_onboarding (
    user_id        BIGINT       PRIMARY KEY,
    deck_progress  INTEGER      NOT NULL DEFAULT 0,
    completed_at   TIMESTAMPTZ,
    skipped_at     TIMESTAMPTZ,
    last_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
