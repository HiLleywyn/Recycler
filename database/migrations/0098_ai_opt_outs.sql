-- Per-guild AI context opt-out list.
-- Users on this table are excluded from:
--   * ai_conversations (history is not saved for them)
--   * ai_user_memory / ai_user_traits updates
--   * ambient chatter (Disco never auto-posts on their messages)
--   * channel_context logging (no social-graph capture)
-- They can still talk to Disco; Disco just won't remember or learn.
CREATE TABLE IF NOT EXISTS ai_opt_outs (
    user_id   BIGINT      NOT NULL,
    guild_id  BIGINT      NOT NULL,
    opted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_opt_outs_guild ON ai_opt_outs (guild_id);
