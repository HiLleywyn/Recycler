-- Migration 0082: Sync group token pool reserves with vault_token_bal.
--
-- vault_add_to_pool() previously stored human-scale floats directly into
-- NUMERIC(36,0) columns (e.g. 500.0 truncated to 500 instead of 5e20).
-- Migration 0081 only rescaled pools where reserve_a < 1e9.  Pools whose
-- seed was already correctly scaled (reserve_a >= 1e9) were skipped, so
-- the pre-fix LP additions were negligible and the pool never reflected
-- the accumulated vault_token_bal.
--
-- Additionally, groups that had their network set via migration 0080
-- (not via the group token network command) may have had vault_add_to_pool
-- create their pool without vault_locked=TRUE. This migration covers both
-- vault_locked and non-vault_locked group token pools.
--
-- This migration computes, for each group token pool, the canonical target
-- token reserve (vault_token_bal * 1e18) and updates both sides while
-- preserving the current price ratio, then recomputes total_lp.
--
-- Only updates pools where the group token reserve is strictly less than
-- vault_token_bal to avoid overwriting pools that are already correct.

WITH target AS (
    SELECT
        p.pool_id,
        p.guild_id,
        p.reserve_a,
        p.reserve_b,
        p.token_a,
        p.token_b,
        ROUND(mg.vault_token_bal * 1000000000000000000) AS vault_raw,
        gt.symbol                                        AS tok_sym
    FROM pools p
    JOIN mining_groups mg
         ON mg.guild_id    = p.guild_id
    JOIN guild_tokens gt
         ON gt.guild_id    = mg.guild_id
        AND gt.symbol      = mg.token_symbol
    WHERE mg.vault_token_bal  > 0
      AND (p.token_a = gt.symbol OR p.token_b = gt.symbol)
),
adjusted AS (
    SELECT
        pool_id,
        guild_id,
        vault_raw,
        tok_sym,
        reserve_a,
        reserve_b,
        token_a,
        token_b,
        -- Determine which reserve holds the group token and which holds the coin
        CASE
            WHEN token_b = tok_sym THEN
                -- group token is reserve_b; only update if pool is behind
                CASE WHEN vault_raw > reserve_b THEN vault_raw ELSE reserve_b END
            WHEN token_a = tok_sym THEN
                -- group token is reserve_a; scale reserve_b proportionally
                CASE
                    WHEN vault_raw > reserve_a AND reserve_a > 0
                        THEN ROUND(vault_raw::NUMERIC * reserve_b::NUMERIC / reserve_a::NUMERIC)
                    ELSE reserve_b
                END
        END AS new_reserve_b,
        CASE
            WHEN token_a = tok_sym THEN
                CASE WHEN vault_raw > reserve_a THEN vault_raw ELSE reserve_a END
            WHEN token_b = tok_sym THEN
                CASE
                    WHEN vault_raw > reserve_b AND reserve_b > 0
                        THEN ROUND(vault_raw::NUMERIC * reserve_a::NUMERIC / reserve_b::NUMERIC)
                    ELSE reserve_a
                END
        END AS new_reserve_a
    FROM target
    WHERE
        -- only touch pools where the group token reserve is behind vault_token_bal
        (token_b = tok_sym AND vault_raw > reserve_b)
     OR (token_a = tok_sym AND vault_raw > reserve_a)
)
UPDATE pools p
SET
    reserve_a = adj.new_reserve_a,
    reserve_b = adj.new_reserve_b,
    total_lp  = ROUND(sqrt(adj.new_reserve_a::NUMERIC * adj.new_reserve_b::NUMERIC))
FROM adjusted adj
WHERE p.pool_id   = adj.pool_id
  AND p.guild_id  = adj.guild_id;
