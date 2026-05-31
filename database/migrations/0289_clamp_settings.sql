-- 0289_clamp_settings.sql
-- Clanktank clamp: per-guild enforcement toggles and cluster size enforcement.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS clamp_clear_urls      BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS clamp_clear_addresses BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS clamp_clear_scams     BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS clasp_auto_mute       BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS clasp_auto_delete     BOOLEAN NOT NULL DEFAULT FALSE;

-- Remove clusters below the minimum member threshold (5).
-- Unlink records first so foreign key constraints are respected.
UPDATE clanker_records
    SET cluster_id = NULL
    WHERE cluster_id IN (
        SELECT id FROM clanker_clusters c
        WHERE (
            SELECT COUNT(*) FROM clanker_cluster_members m WHERE m.cluster_id = c.id
        ) < 5
    );

DELETE FROM clanker_cluster_members
    WHERE cluster_id IN (
        SELECT c.id FROM clanker_clusters c
        WHERE (
            SELECT COUNT(*) FROM clanker_cluster_members m WHERE m.cluster_id = c.id
        ) < 5
    );

DELETE FROM clanker_clusters
    WHERE id NOT IN (SELECT DISTINCT cluster_id FROM clanker_cluster_members);
