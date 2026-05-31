-- Farming level system. Mirrors the user_fishing level/xp shape so the
-- two surfaces feel consistent: arithmetic XP curve, +1% HRV payout per
-- level, max level 50, ~30 hours of casual play to cap.
--
-- XP is granted by services.farming.harvest_plot per crop (rarity-scaled)
-- and the level multiplier applies at services.farming.sell_crop time.

ALTER TABLE user_farming
    ADD COLUMN IF NOT EXISTS farm_level INTEGER       NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS farm_xp    NUMERIC(28,8) NOT NULL DEFAULT 0;

ALTER TABLE user_farming
    DROP CONSTRAINT IF EXISTS user_farming_farm_level_chk;

ALTER TABLE user_farming
    ADD CONSTRAINT user_farming_farm_level_chk
        CHECK (farm_level >= 1 AND farm_xp >= 0);
