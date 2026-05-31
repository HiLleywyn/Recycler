-- 0287_clanktank_leave_evade.sql
-- Adds leave/evade tracking columns to clanker_records.
-- A clanker who leaves the server is still tracked (record kept, left_at set).
-- A clanker who rejoins gets re-clanked automatically; rejoin_count increments.

ALTER TABLE clanker_records
    ADD COLUMN IF NOT EXISTS leave_count   INT  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rejoin_count  INT  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS left_at       TIMESTAMPTZ;

-- Fast lookup for the leavers list (currently absent from server)
CREATE INDEX IF NOT EXISTS idx_clanker_records_left
    ON clanker_records (guild_id, left_at DESC NULLS LAST)
    WHERE left_at IS NOT NULL;
