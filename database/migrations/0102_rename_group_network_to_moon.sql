-- 0102: Rename the bridged group-token network from "Group Network" / "grp"
-- to "Moon Network" / "moon" and migrate any group tokens whose symbols
-- collide with built-in tokens (BTC, SUN, ETH, USDC, AAVE, DSC, DSD, DSY).
--
-- Why:
--   * The wallet display rendered the network header as "grp" because no
--     VAULT_DISPLAY entry existed for the short key. Renaming to "moon" with
--     a registered display name fixes the cosmetic bug and matches the
--     degen vibe of the bridged group-token chain.
--   * Group tokens whose tag derived to a built-in symbol (e.g. tag="btc"
--     -> sym="BTC") silently shadowed the native token: crypto_prices,
--     pools, and tx-history are all keyed by symbol alone, so a Moon
--     Network "BTC" row in wallet_holdings was un-swappable -- the swap
--     command read the built-in BTC entry from get_all_tokens_for_guild()
--     (which skips colliding guild_tokens rows), routed to Bitcoin Network,
--     and never touched the user's Moon Network balance. Renaming each
--     colliding group token to <sym>M (BTC -> BTCM, SUN -> SUNM, ...) gives
--     the group token its own identity in every shared namespace so users
--     can finally trade it.
--
-- Idempotent: re-running is a no-op once the data is on the new naming.

BEGIN;

-- Step 1: rename the canonical full network name on guild_tokens.
UPDATE guild_tokens
   SET network = 'Moon Network'
 WHERE network = 'Group Network';

-- Step 2: rename the short network key on wallet_holdings.
-- A user can in principle have BOTH a 'grp' row and a stale 'moon' row for
-- the same (user, guild, symbol) -- aggregate-then-delete avoids the
-- (user_id, guild_id, network, symbol) primary-key collision, mirroring
-- the pattern used in migration 0096.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'wallet_holdings' AND column_name = 'network'
    ) THEN
        INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
        SELECT user_id, guild_id, 'moon', symbol, SUM(amount)
          FROM wallet_holdings
         WHERE network = 'grp'
         GROUP BY user_id, guild_id, symbol
        ON CONFLICT (user_id, guild_id, network, symbol)
        DO UPDATE SET amount = wallet_holdings.amount + EXCLUDED.amount;

        DELETE FROM wallet_holdings WHERE network = 'grp';
    END IF;
END $$;

-- Step 3: rename the short network key on transactions.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'transactions' AND column_name = 'network'
    ) THEN
        UPDATE transactions SET network = 'moon' WHERE network = 'grp';
    END IF;
END $$;

-- Step 4: rename the short network key on wallet_addresses (if present).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'wallet_addresses' AND column_name = 'network'
    ) THEN
        UPDATE wallet_addresses SET network = 'moon' WHERE network = 'grp';
    END IF;
END $$;

-- Step 5: rename the short network key on network_vaults / vault_levels
-- (if present) so vault progression rolls forward under the new key.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'network_vaults' AND column_name = 'network'
    ) THEN
        UPDATE network_vaults SET network = 'moon' WHERE network = 'grp';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'vault_levels' AND column_name = 'network'
    ) THEN
        UPDATE vault_levels SET network = 'moon' WHERE network = 'grp';
    END IF;
END $$;

-- Step 6: rename existing guild_tokens whose symbol collides with a built-in
-- token. Each colliding row gets a new symbol "<sym>M"; if that's also taken
-- (another guild already has a real token "BTCM"), append a numeric suffix
-- until a free slot is found. All FK-adjacent tables (wallet_holdings,
-- transactions, mining_groups, crypto_prices) are updated in lock-step so
-- balances and history stay attached to the renamed token.
DO $$
DECLARE
    builtin_syms text[] := ARRAY[
        'BTC','SUN','ETH','USDC','AAVE','DSC','DSD','DSY'
    ];
    rec record;
    new_sym text;
    suffix int;
BEGIN
    FOR rec IN
        SELECT guild_id, symbol
          FROM guild_tokens
         WHERE token_type = 'group'
           AND symbol = ANY(builtin_syms)
    LOOP
        -- Find a free <sym>M / <sym>M2 / <sym>M3 ... slot in this guild.
        new_sym := rec.symbol || 'M';
        suffix  := 2;
        WHILE EXISTS (
            SELECT 1 FROM guild_tokens
             WHERE guild_id = rec.guild_id AND symbol = new_sym
        ) OR new_sym = ANY(builtin_syms)
        LOOP
            new_sym := rec.symbol || 'M' || suffix::text;
            suffix  := suffix + 1;
        END LOOP;

        -- 6a. guild_tokens: rename the row.
        UPDATE guild_tokens
           SET symbol = new_sym
         WHERE guild_id = rec.guild_id AND symbol = rec.symbol;

        -- 6b. wallet_holdings: only the Moon Network rows belong to the
        --     group token; the native-network rows (e.g. btc) belong to the
        --     built-in BTC and must stay on the original symbol.
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'wallet_holdings' AND column_name = 'network'
        ) THEN
            -- Aggregate-first to merge into any pre-existing target row.
            INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
            SELECT user_id, guild_id, 'moon', new_sym, SUM(amount)
              FROM wallet_holdings
             WHERE guild_id = rec.guild_id
               AND network  = 'moon'
               AND symbol   = rec.symbol
             GROUP BY user_id, guild_id
            ON CONFLICT (user_id, guild_id, network, symbol)
            DO UPDATE SET amount = wallet_holdings.amount + EXCLUDED.amount;

            DELETE FROM wallet_holdings
             WHERE guild_id = rec.guild_id
               AND network  = 'moon'
               AND symbol   = rec.symbol;
        END IF;

        -- 6c. transactions on Moon Network referencing the old symbol.
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'transactions' AND column_name = 'network'
        ) THEN
            UPDATE transactions
               SET symbol_in = new_sym
             WHERE guild_id = rec.guild_id
               AND network  = 'moon'
               AND symbol_in = rec.symbol;
            UPDATE transactions
               SET symbol_out = new_sym
             WHERE guild_id = rec.guild_id
               AND network  = 'moon'
               AND symbol_out = rec.symbol;
        END IF;

        -- 6d. mining_groups: rebind the group's token_symbol if it pointed
        --     at the old symbol.
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'mining_groups' AND column_name = 'token_symbol'
        ) THEN
            UPDATE mining_groups
               SET token_symbol = new_sym
             WHERE guild_id = rec.guild_id
               AND token_symbol = rec.symbol;
        END IF;

        -- 6e. crypto_prices: built-in BTC already owns the (BTC, guild_id)
        --     row, so we INSERT a fresh price row for the renamed group
        --     token at the seed price (matches what _ensure_group_token
        --     would have done for a non-colliding tag in the first place).
        INSERT INTO crypto_prices
            (symbol, guild_id, price, open_price, day_high, day_low)
        VALUES
            (new_sym, rec.guild_id, 0.01, 0.01, 0.01, 0.01)
        ON CONFLICT (symbol, guild_id) DO NOTHING;
    END LOOP;
END $$;

COMMIT;
