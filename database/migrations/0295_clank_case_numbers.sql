-- 0295_clank_case_numbers.sql
-- Sequential, searchable case numbers for every containment event.
-- clank_case_counter tracks the per-guild sequence; clanker_records.case_num
-- stores the assigned number so records are searchable after tank exit.
CREATE TABLE IF NOT EXISTS clank_case_counter (
    guild_id  BIGINT  PRIMARY KEY,
    last_num  BIGINT  NOT NULL DEFAULT 0
);

ALTER TABLE clanker_records ADD COLUMN IF NOT EXISTS case_num BIGINT;
