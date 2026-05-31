-- 0073_group_hall.sql
-- Add Group Hall private thread support to mining groups.
-- Replaces old mining-focused group upgrades with Hall-focused upgrades.
-- Adds per-guild earnings multipliers (work, daily, gambling).

-- Group Hall thread columns
ALTER TABLE mining_groups
    ADD COLUMN IF NOT EXISTS hall_thread_id  BIGINT,
    ADD COLUMN IF NOT EXISTS hall_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS hall_opened_at  TIMESTAMPTZ;

-- Group Hall parent channel (admin sets this via .admin setchannel grouphall #ch)
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS grouphall_channel BIGINT;

-- Per-guild earnings multipliers (admin-configurable, default 1.0 = no change)
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS work_multiplier     NUMERIC DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS daily_multiplier    NUMERIC DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS gambling_multiplier NUMERIC DEFAULT 1.0;

-- Refund the USD cost of every old mining upgrade back to the group reserve
-- before deleting them, so groups are not short-changed by the system change.
--
-- Costs (all old upgrades were cost_sun * 0.01 USD):
--   Legacy uppercase IDs:
--     OVERCLOCK    500,000 SUN = $5,000    FIBER        750,000 SUN = $7,500
--     SYNDICATE  1,200,000 SUN = $12,000   LIQUID_POOL 2,000,000 SUN = $20,000
--     ASIC_BATCH 3,500,000 SUN = $35,000
--   V2 lowercase IDs:
--     overclock $750  overclock_ii $3,000  overclock_iii $10,000
--     reward_splitter $1,250  reward_splitter_ii $5,000
--     barracks $625  barracks_ii $2,500
--     xp_amplifier $1,000  xp_amplifier_ii $4,500
--     lucky_drill $1,500  lucky_drill_ii $6,250
--     solar_panels $500  solar_panels_ii $2,000  solar_panels_iii $7,500
UPDATE mining_groups mg
SET    reserve_usd = reserve_usd + refunds.total_refund
FROM (
    SELECT
        guild_id,
        group_id,
        SUM(
            CASE upgrade_id
                -- Legacy
                WHEN 'OVERCLOCK'    THEN  5000.00
                WHEN 'FIBER'        THEN  7500.00
                WHEN 'SYNDICATE'    THEN 12000.00
                WHEN 'LIQUID_POOL'  THEN 20000.00
                WHEN 'ASIC_BATCH'   THEN 35000.00
                -- V2 tiered
                WHEN 'overclock'          THEN   750.00
                WHEN 'overclock_ii'       THEN  3000.00
                WHEN 'overclock_iii'      THEN 10000.00
                WHEN 'reward_splitter'    THEN  1250.00
                WHEN 'reward_splitter_ii' THEN  5000.00
                WHEN 'barracks'           THEN   625.00
                WHEN 'barracks_ii'        THEN  2500.00
                WHEN 'xp_amplifier'       THEN  1000.00
                WHEN 'xp_amplifier_ii'    THEN  4500.00
                WHEN 'lucky_drill'        THEN  1500.00
                WHEN 'lucky_drill_ii'     THEN  6250.00
                WHEN 'solar_panels'       THEN   500.00
                WHEN 'solar_panels_ii'    THEN  2000.00
                WHEN 'solar_panels_iii'   THEN  7500.00
                ELSE 0.00
            END
        ) AS total_refund
    FROM  group_upgrades
    GROUP BY guild_id, group_id
    HAVING SUM(
        CASE upgrade_id
            WHEN 'OVERCLOCK'          THEN  5000.00
            WHEN 'FIBER'              THEN  7500.00
            WHEN 'SYNDICATE'          THEN 12000.00
            WHEN 'LIQUID_POOL'        THEN 20000.00
            WHEN 'ASIC_BATCH'         THEN 35000.00
            WHEN 'overclock'          THEN   750.00
            WHEN 'overclock_ii'       THEN  3000.00
            WHEN 'overclock_iii'      THEN 10000.00
            WHEN 'reward_splitter'    THEN  1250.00
            WHEN 'reward_splitter_ii' THEN  5000.00
            WHEN 'barracks'           THEN   625.00
            WHEN 'barracks_ii'        THEN  2500.00
            WHEN 'xp_amplifier'       THEN  1000.00
            WHEN 'xp_amplifier_ii'    THEN  4500.00
            WHEN 'lucky_drill'        THEN  1500.00
            WHEN 'lucky_drill_ii'     THEN  6250.00
            WHEN 'solar_panels'       THEN   500.00
            WHEN 'solar_panels_ii'    THEN  2000.00
            WHEN 'solar_panels_iii'   THEN  7500.00
            ELSE 0.00
        END
    ) > 0
) AS refunds
WHERE mg.guild_id = refunds.guild_id
  AND mg.group_id = refunds.group_id;

-- Now clear all old mining-focused upgrade records.
DELETE FROM group_upgrades;
