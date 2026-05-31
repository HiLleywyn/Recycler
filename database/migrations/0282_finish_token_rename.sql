-- 0282: Finish the token rename started in 0281.
--
-- 0281 renamed BTC/ETH/AAVE/PEPE/MBTC -> MTA/ARC/VTR/STR/MMTA, but its column
-- list was hand-built from schema.sql and so missed every symbol/network
-- column that lives on a table added by a later migration -- notably
-- pos_delegations.token, moon_wrapped_stakes.symbol, pools.token_a/token_b,
-- sun_loans.borrow_symbol and the nft/auction "currency" columns. It also
-- left two CHECK constraints pinned to the old names.
--
-- That gap crashed undelegation: a pre-rename delegation row still carried
-- token = 'ETH' while 0281 had already moved its network to 'arc', so
-- crediting the refund into wallet_holdings tripped chk_native_symbol_network
-- ("symbol 'ETH' must live on network 'eth'").
--
-- This migration discovers EVERY symbol- and network-bearing column directly
-- from information_schema, so no migration-added table can be missed. The old
-- built-in symbols (BTC/ETH/AAVE/PEPE/MBTC) and network keys are reserved, so
-- an exact-match UPDATE only ever touches built-in rows.
--
-- Idempotent: every UPDATE is keyed on the OLD value; constraints are dropped
-- IF EXISTS before being re-added.

BEGIN;

-- Drop the two token-pinned CHECK constraints so the rewrite below can move
-- rows onto the new symbols.
ALTER TABLE IF EXISTS wallet_holdings    DROP CONSTRAINT IF EXISTS chk_native_symbol_network;
ALTER TABLE IF EXISTS moon_wrapped_stakes DROP CONSTRAINT IF EXISTS chk_moon_wrapped_symbol;

DO $$
DECLARE
    rec  record;
    m    text;
    oldv text;
    newv text;
    sym_map text[] := ARRAY['BTC|MTA', 'ETH|ARC', 'AAVE|VTR', 'PEPE|STR', 'MBTC|MMTA'];
    net_map text[] := ARRAY[
        'btc|mta', 'eth|arc', 'BTC|MTA', 'ETH|ARC',
        'Bitcoin Network|Moneta Chain', 'Ethereum Network|Arcadia Network'
    ];
BEGIN
    -- Token-symbol columns, discovered by name across the whole public schema.
    FOR rec IN
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND data_type IN ('text', 'character varying')
           AND column_name IN (
               'symbol', 'symbol_in', 'symbol_out', 'gas_coin', 'stake_token',
               'chain_symbol', 'mint_token', 'token', 'token_a', 'token_b',
               'borrow_symbol', 'currency', 'base_token', 'quote_token',
               'payout_symbol'
           )
    LOOP
        FOREACH m IN ARRAY sym_map LOOP
            oldv := split_part(m, '|', 1);
            newv := split_part(m, '|', 2);
            EXECUTE format('UPDATE %I SET %I = %L WHERE %I = %L',
                           rec.table_name, rec.column_name, newv,
                           rec.column_name, oldv);
        END LOOP;
    END LOOP;

    -- Network columns (short key OR full name; both forms are rewritten).
    FOR rec IN
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND data_type IN ('text', 'character varying')
           AND column_name IN ('network', 'network_name', 'token_network')
    LOOP
        FOREACH m IN ARRAY net_map LOOP
            oldv := split_part(m, '|', 1);
            newv := split_part(m, '|', 2);
            EXECUTE format('UPDATE %I SET %I = %L WHERE %I = %L',
                           rec.table_name, rec.column_name, newv,
                           rec.column_name, oldv);
        END LOOP;
    END LOOP;
END $$;

-- Wallet-address prefixes ("<netkey>:<hash>").
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'wallet_addresses' AND column_name = 'address') THEN
        UPDATE wallet_addresses SET address = 'mta:' || substring(address from 5)
         WHERE address LIKE 'btc:%';
        UPDATE wallet_addresses SET address = 'arc:' || substring(address from 5)
         WHERE address LIKE 'eth:%';
    END IF;
END $$;

-- Re-canonicalise pools after the symbol rename.
-- make_pool_id() stores the pair alphabetically and derives pool_id as
-- "<a>-<b>", so a renamed token can change both the sort order and the
-- derived pool_id. The block above already rewrote pools.token_a /
-- pools.token_b to the new symbols, but pool_id is a composite string it
-- could not reach, and the same id is carried on lp_positions / lp_snapshots
-- / group_lp_positions.
--
-- Rebuilding pool_id is NOT collision-free: seed_pools() runs on every bot
-- startup with the post-rename config, so any boot in the window between
-- 0281 and this migration auto-seeded a fresh pool under the NEW name (e.g.
-- MTA-USD) right alongside the stale pre-rename pool (BTC-USD). A blind
-- UPDATE of pool_id then trips the (pool_id, guild_id) primary key and rolls
-- the whole migration back, crash-looping startup.
--
-- For each stale pool: if its canonical id is already taken, MERGE it into
-- the existing pool -- sum reserves and total_lp, fold in per-user and group
-- LP positions -- then drop the stale row. Otherwise repoint the pool and
-- its child rows to the new id in lockstep. Either branch leaves pool_id
-- consistent with token_a/token_b. Idempotent: a re-run finds no stale rows.
DO $$
DECLARE
    rec record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'pools' AND column_name = 'pool_id') THEN
        RETURN;
    END IF;

    FOR rec IN
        SELECT pool_id                                                     AS old_id,
               guild_id,
               LEAST(token_a, token_b)                                     AS new_a,
               GREATEST(token_a, token_b)                                  AS new_b,
               LEAST(token_a, token_b) || '-' || GREATEST(token_a, token_b) AS new_id
          FROM pools
         WHERE pool_id <> LEAST(token_a, token_b) || '-' || GREATEST(token_a, token_b)
    LOOP
        IF EXISTS (SELECT 1 FROM pools
                    WHERE pool_id = rec.new_id AND guild_id = rec.guild_id) THEN
            -- Collision: fold the stale pool into the pool that already
            -- holds the canonical id, then drop the stale row.
            UPDATE pools tgt
               SET reserve_a = tgt.reserve_a + src.reserve_a,
                   reserve_b = tgt.reserve_b + src.reserve_b,
                   total_lp  = tgt.total_lp  + src.total_lp
              FROM pools src
             WHERE tgt.pool_id = rec.new_id AND tgt.guild_id = rec.guild_id
               AND src.pool_id = rec.old_id AND src.guild_id = rec.guild_id;

            INSERT INTO lp_positions (user_id, guild_id, pool_id, lp_shares, added_at)
            SELECT user_id, guild_id, rec.new_id, lp_shares, added_at
              FROM lp_positions
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id
            ON CONFLICT (user_id, guild_id, pool_id)
            DO UPDATE SET lp_shares = lp_positions.lp_shares + EXCLUDED.lp_shares;
            DELETE FROM lp_positions
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id;

            INSERT INTO group_lp_positions
                   (group_id, guild_id, pool_id, lp_shares, seeded_at,
                    last_harvest_at, cost_basis_usd_raw)
            SELECT group_id, guild_id, rec.new_id, lp_shares, seeded_at,
                   last_harvest_at, cost_basis_usd_raw
              FROM group_lp_positions
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id
            ON CONFLICT (group_id, guild_id, pool_id)
            DO UPDATE SET lp_shares          = group_lp_positions.lp_shares
                                             + EXCLUDED.lp_shares,
                          cost_basis_usd_raw = group_lp_positions.cost_basis_usd_raw
                                             + EXCLUDED.cost_basis_usd_raw;
            DELETE FROM group_lp_positions
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id;

            -- lp_snapshots store per-position entry ratios that cannot be
            -- summed; keep the surviving pool's snapshot, drop the stale one.
            DELETE FROM lp_snapshots
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id;

            DELETE FROM pools
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id;
        ELSE
            -- No collision: repoint the pool and its children to the new id.
            UPDATE lp_positions       SET pool_id = rec.new_id
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id;
            UPDATE lp_snapshots       SET pool_id = rec.new_id
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id;
            UPDATE group_lp_positions SET pool_id = rec.new_id
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id;
            UPDATE pools
               SET token_a = rec.new_a,
                   token_b = rec.new_b,
                   pool_id = rec.new_id
             WHERE pool_id = rec.old_id AND guild_id = rec.guild_id;
        END IF;
    END LOOP;
END $$;

-- Rebuild the native-symbol guard with the renamed symbols / network keys.
-- Migration 0114 pinned it to BTC/SUN/ETH/DSC; the Moneta and Arcadia coins
-- are now MTA (mta) and ARC (arc).
ALTER TABLE IF EXISTS wallet_holdings ADD CONSTRAINT chk_native_symbol_network CHECK (
    CASE symbol
        WHEN 'MTA' THEN network = 'mta'
        WHEN 'SUN' THEN network = 'sun'
        WHEN 'ARC' THEN network = 'arc'
        WHEN 'DSC' THEN network = 'dsc'
        ELSE TRUE
    END
);

-- Rebuild the wrapped-stake guard (migration 0279 pinned it to MBTC/MSUN).
ALTER TABLE IF EXISTS moon_wrapped_stakes
    ADD CONSTRAINT chk_moon_wrapped_symbol CHECK (symbol IN ('MMTA', 'MSUN'));

COMMIT;
