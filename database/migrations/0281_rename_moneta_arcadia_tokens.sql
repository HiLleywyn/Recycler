-- 0281: Rename the four real-world-derived built-in tokens (and their two
-- networks) to original Discoin assets.
--
--   BTC  -> MTA   (Bitcoin       -> Moneta,      Bitcoin Network  -> Moneta Chain)
--   ETH  -> ARC   (Ethereum      -> Arcadia,     Ethereum Network -> Arcadia Network)
--   AAVE -> VTR   (Aave          -> Vantor)
--   PEPE -> STR   (Pepe          -> Stratum)
--   MBTC -> MMTA  (Moon Bitcoin  -> Moon Moneta)
--
--   network short keys:  btc -> mta,  eth -> arc
--   wallet-address prefix: "btc:" -> "mta:",  "eth:" -> "arc:"
--
-- Why:
--   The tokens were thin reskins of real cryptocurrencies. Renaming them to
--   self-owned assets removes the real-world dependency from every display,
--   help text, and the dashboard. The new symbols (MTA / ARC / VTR / STR /
--   MMTA) never existed before, so no wallet_holdings / crypto_prices row can
--   collide -- a plain UPDATE is safe.
--
-- Idempotent: every UPDATE is keyed on the OLD value, so re-running once the
-- data is on the new naming is a no-op. Every table/column is probed against
-- information_schema first so the migration is safe on partially-migrated DBs.

BEGIN;

DO $$
DECLARE
    -- (table, column) pairs that hold a token symbol.
    sym_cols text[] := ARRAY[
        'crypto_prices.symbol', 'crypto_holdings.symbol',
        'wallet_holdings.symbol', 'price_candles.symbol',
        'transactions.symbol_in', 'transactions.symbol_out',
        'transactions.gas_coin', 'stakes.symbol',
        'pos_validators.stake_token', 'guild_networks.stake_token',
        'rig_chain_assignments.chain_symbol', 'pow_network_state.chain_symbol',
        'mining_blocks.symbol', 'savings_deposits.symbol',
        'token_contracts.symbol', 'network_accepted_tokens.symbol',
        'nft_collections.mint_token', 'auto_compound_settings.symbol',
        'price_alerts.symbol', 'stake_batches.symbol'
    ];
    -- (table, column) pairs that hold a network reference (short key OR full
    -- name). Both forms are rewritten below; the non-matching form no-ops.
    net_cols text[] := ARRAY[
        'wallet_holdings.network', 'transactions.network',
        'pos_validators.network', 'pos_delegations.network',
        'chain_blocks.network', 'mempool.network',
        'validator_blocks.network', 'network_base_fees.network',
        'smart_contracts.network', 'wallet_addresses.network',
        'network_accepted_tokens.network', 'network_vaults.network',
        'guild_tokens.network', 'guild_networks.network_name',
        'nft_collections.network', 'mining_groups.token_network'
    ];
    -- old -> new value maps. ``::text[]`` rows are "old|new".
    sym_map text[] := ARRAY[
        'BTC|MTA', 'ETH|ARC', 'AAVE|VTR', 'PEPE|STR', 'MBTC|MMTA'
    ];
    net_map text[] := ARRAY[
        'btc|mta', 'eth|arc', 'BTC|MTA', 'ETH|ARC',
        'Bitcoin Network|Moneta Chain', 'Ethereum Network|Arcadia Network'
    ];
    spec text;
    tbl  text;
    col  text;
    m    text;
    oldv text;
    newv text;
BEGIN
    FOREACH spec IN ARRAY sym_cols LOOP
        tbl := split_part(spec, '.', 1);
        col := split_part(spec, '.', 2);
        IF EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = tbl AND column_name = col) THEN
            FOREACH m IN ARRAY sym_map LOOP
                oldv := split_part(m, '|', 1);
                newv := split_part(m, '|', 2);
                EXECUTE format('UPDATE %I SET %I = %L WHERE %I = %L',
                               tbl, col, newv, col, oldv);
            END LOOP;
        END IF;
    END LOOP;

    FOREACH spec IN ARRAY net_cols LOOP
        tbl := split_part(spec, '.', 1);
        col := split_part(spec, '.', 2);
        IF EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = tbl AND column_name = col) THEN
            FOREACH m IN ARRAY net_map LOOP
                oldv := split_part(m, '|', 1);
                newv := split_part(m, '|', 2);
                EXECUTE format('UPDATE %I SET %I = %L WHERE %I = %L',
                               tbl, col, newv, col, oldv);
            END LOOP;
        END IF;
    END LOOP;
END $$;

-- Wallet-address prefixes: the address PK is "<netkey>:<hash>". The new
-- prefixes never existed, so the rewritten PK cannot collide.
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

COMMIT;
