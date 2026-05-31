-- 0279_chat_thread_links.sql
--
-- Thread linking + the in-thread control panel.
--
-- chat_thread_links records one row per "link": a saved thread (by its
-- recall token) pulled into a live Disco thread so its context travels
-- with the conversation. A thread can carry at most 3 links (4 threads
-- total counting itself); a single user can hold at most 3 links across
-- every thread they own -- the budget is enforced in code.
--
-- panel_message_id points at the control-panel message Disco posts in
-- each thread. The panel carries the Save / Close / Context / Links
-- buttons and the live list of connected threads, and is edited in
-- place whenever the thread's state changes.

CREATE TABLE IF NOT EXISTS chat_thread_links (
    id               BIGSERIAL    PRIMARY KEY,
    source_thread_id BIGINT       NOT NULL,
    linked_thread_id BIGINT       NOT NULL,
    linked_token     TEXT         NOT NULL,
    linked_by        BIGINT       NOT NULL,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_chat_thread_link UNIQUE (source_thread_id, linked_thread_id)
);

-- ,thread links / panel refresh read every link for one source thread.
CREATE INDEX IF NOT EXISTS idx_chat_thread_links_source
    ON chat_thread_links (source_thread_id);

-- The combined per-user link budget counts rows by linked_by.
CREATE INDEX IF NOT EXISTS idx_chat_thread_links_user
    ON chat_thread_links (linked_by);

-- The control-panel message Disco keeps up to date inside each thread.
ALTER TABLE chat_threads
    ADD COLUMN IF NOT EXISTS panel_message_id BIGINT;
