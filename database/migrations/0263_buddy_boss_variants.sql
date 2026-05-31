-- 0263_buddy_boss_variants.sql
-- Boss-tamed buddies need a permanent marker so the renderer can
-- paint a unique overlay (crown / antlers / flame mane / etc.) and
-- the battle engine can swap in a boss-flavoured ability. Tracking
-- the zone they were tamed at also lets the player rename the buddy
-- without losing the "captured boss" cosmetic.

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS boss_zone_id TEXT NULL;

CREATE INDEX IF NOT EXISTS cc_buddies_boss_zone_id_idx
    ON cc_buddies (boss_zone_id)
    WHERE boss_zone_id IS NOT NULL;
