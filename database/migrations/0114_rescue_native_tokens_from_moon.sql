-- 0114: Rescue native tokens (BTC, SUN, ETH, DSC) stranded on the Moon
-- Network back to their canonical networks.
--
-- Background: migration 0102 renamed the group-token network key from
-- 'grp' to 'moon', and 0103 consolidated group-token rows that landed
-- on the wrong network due to the swap credit bug. Neither migration
-- handled the inverse case: native tokens that ended up on network
-- 'moon'. Those rows are invisible to the swap router (which looks up
-- BTC on 'btc', SUN on 'sun', etc.) and therefore un-tradeable and
-- un-sendable for the affected players.
--
-- Fix: fold any BTC/SUN/ETH/DSC rows on 'moon' into the canonical
-- (user, guild, network, symbol) row, summing amounts where both sides
-- already exist. Log each rescued balance as a 'network_rescue'
-- transaction so affected players can see it in their history.
--
-- Then install a CHECK constraint so these native symbols can never
-- silently land on the wrong network again.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'wallet_holdings' AND column_name = 'network'
    ) THEN
        -- 1. Collect the stranded rows with their canonical target network.
        CREATE TEMP TABLE _rescue_rows ON COMMIT DROP AS
        SELECT
            wh.user_id,
            wh.guild_id,
            wh.symbol,
            wh.amount,
            CASE wh.symbol
                WHEN 'BTC' THEN 'btc'
                WHEN 'SUN' THEN 'sun'
                WHEN 'ETH' THEN 'eth'
                WHEN 'DSC' THEN 'dsc'
            END AS target_network
        FROM wallet_holdings wh
        WHERE wh.network = 'moon'
          AND wh.symbol IN ('BTC', 'SUN', 'ETH', 'DSC')
          AND wh.amount > 0;

        -- 2. Merge stranded amounts into the canonical rows.
        INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
        SELECT user_id, guild_id, target_network, symbol, amount
          FROM _rescue_rows
        ON CONFLICT (user_id, guild_id, network, symbol)
        DO UPDATE SET amount = wallet_holdings.amount + EXCLUDED.amount;

        -- 3. Delete the now-merged moon rows.
        DELETE FROM wallet_holdings wh
         USING _rescue_rows r
         WHERE wh.user_id  = r.user_id
           AND wh.guild_id = r.guild_id
           AND wh.network  = 'moon'
           AND wh.symbol   = r.symbol;

        -- 4. Audit log: one transactions row per rescued balance.
        INSERT INTO transactions (
            tx_hash, guild_id, user_id, tx_type,
            symbol_in, amount_in, symbol_out, amount_out, gas_fee, gas_coin
        )
        SELECT
            'rescue_' || gen_random_uuid()::text,
            r.guild_id,
            r.user_id,
            'network_rescue',
            NULL,
            NULL,
            r.symbol,
            r.amount,
            0,
            ''
        FROM _rescue_rows r;
    END IF;
END $$;

-- 5. Prevent recurrence: native symbols must live on their canonical network.
ALTER TABLE wallet_holdings
    ADD CONSTRAINT chk_native_symbol_network CHECK (
        CASE symbol
            WHEN 'BTC' THEN network = 'btc'
            WHEN 'SUN' THEN network = 'sun'
            WHEN 'ETH' THEN network = 'eth'
            WHEN 'DSC' THEN network = 'dsc'
            ELSE TRUE
        END
    );

COMMIT;
