-- Migration 0036: add bounty_id column to reports for explicit bounty linking
-- Reports can now be tied to a specific bounty when submitted via ,report bounty <id>

ALTER TABLE reports ADD COLUMN IF NOT EXISTS bounty_id BIGINT REFERENCES bounties(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_reports_bounty ON reports (bounty_id) WHERE bounty_id IS NOT NULL;
