-- Fix ai_conversations history_key column default from 'disco' to 'default'.
-- Existing rows are unaffected (agent keys like 'disco' are still valid values).
-- This only changes what gets written when no history_key is explicitly supplied.
ALTER TABLE ai_conversations
    ALTER COLUMN history_key SET DEFAULT 'default';
