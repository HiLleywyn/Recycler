-- 0291_automod_scam_hunter.sql
-- AutoMod auto-clank toggle and scam hunter channel/whitelist.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS automod_auto_clank  BOOLEAN  NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS scam_report_channel BIGINT,
    ADD COLUMN IF NOT EXISTS scam_hunter_ids     BIGINT[] NOT NULL DEFAULT '{}';
