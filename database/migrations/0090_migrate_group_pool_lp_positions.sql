-- 0090: Backfill group LP positions for existing partnership pools.
--
-- Some existing group partnership pools were created before group_lp_positions
-- was consistently written during acceptance. Backfill missing rows so both
-- participating groups are represented in LP tracking.

WITH pool_groups AS (
    SELECT
        p.guild_id,
        p.pool_id,
        p.created_at,
        p.total_lp::NUMERIC AS total_lp,
        ga.group_id AS group_a_id,
        gb.group_id AS group_b_id
    FROM pools p
    JOIN mining_groups ga
      ON ga.guild_id = p.guild_id
     AND ga.token_symbol = p.token_a
    JOIN mining_groups gb
      ON gb.guild_id = p.guild_id
     AND gb.token_symbol = p.token_b
     AND gb.group_id <> ga.group_id
    WHERE COALESCE(p.is_group_pool, FALSE) = TRUE
      AND COALESCE(p.vault_locked, FALSE) = FALSE
),
pool_state AS (
    SELECT
        pg.*,
        COALESCE(existing.existing_lp, 0)::NUMERIC AS existing_lp,
        glp_a.group_id AS has_a,
        glp_b.group_id AS has_b
    FROM pool_groups pg
    LEFT JOIN (
        SELECT guild_id, pool_id, SUM(lp_shares)::NUMERIC AS existing_lp
        FROM group_lp_positions
        GROUP BY guild_id, pool_id
    ) existing
      ON existing.guild_id = pg.guild_id
     AND existing.pool_id = pg.pool_id
    LEFT JOIN group_lp_positions glp_a
      ON glp_a.guild_id = pg.guild_id
     AND glp_a.pool_id = pg.pool_id
     AND glp_a.group_id = pg.group_a_id
    LEFT JOIN group_lp_positions glp_b
      ON glp_b.guild_id = pg.guild_id
     AND glp_b.pool_id = pg.pool_id
     AND glp_b.group_id = pg.group_b_id
),
alloc AS (
    SELECT
        ps.*,
        GREATEST(ps.total_lp - ps.existing_lp, 0)::NUMERIC AS remaining_lp
    FROM pool_state ps
)
INSERT INTO group_lp_positions (group_id, guild_id, pool_id, lp_shares, seeded_at)
SELECT
    v.group_id,
    a.guild_id,
    a.pool_id,
    v.lp_shares,
    a.created_at
FROM alloc a
JOIN LATERAL (
    SELECT
        a.group_a_id AS group_id,
        CASE
            -- group A already has a position: no backfill needed for this row
            WHEN a.has_a IS NOT NULL THEN NULL
            -- both groups missing: split remaining LP 50/50 (floor half to A)
            WHEN a.has_b IS NULL THEN FLOOR(a.remaining_lp / 2)
            -- only group A missing: assign all remaining LP to A
            ELSE a.remaining_lp
        END AS lp_shares
    UNION ALL
    SELECT
        a.group_b_id AS group_id,
        CASE
            -- group B already has a position: no backfill needed for this row
            WHEN a.has_b IS NOT NULL THEN NULL
            -- both groups missing: B gets the remainder after A's floor half
            WHEN a.has_a IS NULL THEN a.remaining_lp - FLOOR(a.remaining_lp / 2)
            -- only group B missing: assign all remaining LP to B
            ELSE a.remaining_lp
        END AS lp_shares
) v ON TRUE
WHERE v.lp_shares IS NOT NULL
ON CONFLICT (group_id, guild_id, pool_id) DO NOTHING;
