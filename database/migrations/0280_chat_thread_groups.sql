-- 0280_chat_thread_groups.sql
--
-- Thread groups + the link-as-merge / context-rollback model.
--
-- Linking is reframed as merging. A link is now a LIVE reference, not a
-- copy: a thread's AI context is assembled fresh every turn from the set
-- of threads/groups it currently links (transitively), so closing a
-- thread instantly removes its contribution from every conversation that
-- referenced it -- the "rollback" behaviour falls out for free.
--
-- chat_thread_groups assigns a stable per-guild integer id to a web of
-- linked threads. chat_thread_group_members lists the threads in a group
-- (a thread belongs to at most one group). Groups merge when bridged by
-- a link and are retired when their last member closes.
--
-- chat_thread_links gains a link_kind: a 'thread' link references one
-- thread by recall token (the existing shape), a 'group' link references
-- a whole group by id. A source thread carries at most 3 of each.

CREATE TABLE IF NOT EXISTS chat_thread_groups (
    guild_id   BIGINT      NOT NULL,
    group_id   BIGINT      NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, group_id)
);

CREATE TABLE IF NOT EXISTS chat_thread_group_members (
    guild_id  BIGINT      NOT NULL,
    group_id  BIGINT      NOT NULL,
    thread_id BIGINT      NOT NULL,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, thread_id),
    FOREIGN KEY (guild_id, group_id)
        REFERENCES chat_thread_groups (guild_id, group_id) ON DELETE CASCADE
);

-- Group context assembly + ,thread group list scan members by group.
CREATE INDEX IF NOT EXISTS idx_ctgm_group
    ON chat_thread_group_members (guild_id, group_id);

-- Extend chat_thread_links to carry either a thread link or a group link.
ALTER TABLE chat_thread_links
    ADD COLUMN IF NOT EXISTS link_kind       TEXT   NOT NULL DEFAULT 'thread',
    ADD COLUMN IF NOT EXISTS linked_group_id BIGINT;

-- Group links leave linked_thread_id / linked_token NULL.
ALTER TABLE chat_thread_links ALTER COLUMN linked_thread_id DROP NOT NULL;
ALTER TABLE chat_thread_links ALTER COLUMN linked_token     DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_ctl_kind'
    ) THEN
        ALTER TABLE chat_thread_links
            ADD CONSTRAINT chk_ctl_kind CHECK (link_kind IN ('thread', 'group'));
    END IF;
END $$;

-- One group can only be linked into a source thread once.
CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_thread_group_link
    ON chat_thread_links (source_thread_id, linked_group_id)
    WHERE linked_group_id IS NOT NULL;
