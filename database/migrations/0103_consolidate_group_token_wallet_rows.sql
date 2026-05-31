-- 0103: Consolidate group-token wallet_holdings rows that landed on the
-- wrong network due to the swap credit bug.
--
-- Bug: the AMM swap flow computed a single ``swap_network`` (net_in or
-- net_out, first non-empty wins) and used its short key for BOTH the
-- debit of token_in AND the credit of token_out. For vault-pair pools
-- where the two sides live on different networks (e.g. CAT group token on
-- Moon Network paired against BTC on Bitcoin Network), the output token
-- was credited to the mempool chain's wallet instead of its own:
--
--     swap BTC -> CAT    swap_network = "Bitcoin Network"
--                        CAT credited to wallet_holdings(network='btc')
--                        instead of wallet_holdings(network='moon').
--
-- That produced the duplicate-network display bug (same symbol appearing
-- in two network sections of the DeFi wallet embed) and trapped the
-- output balance where the user could not swap it back out.
--
-- Code fix: per-token network resolution in the instant + mempool swap
-- paths (cogs/trade.py, cogs/validators.py, cogs/stake.py).
--
-- Data fix: for every group token, fold any non-moon wallet_holdings row
-- back into its canonical 'moon' row. Mirrors migration 0096's aggregate-
-- first pattern so overlapping (user, guild, symbol) rows on two bad
-- networks cannot trip the primary key.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'wallet_holdings' AND column_name = 'network'
    ) THEN
        -- 1. Sum every non-moon group-token row per (user, guild, symbol)
        --    into the canonical moon row.
        INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
        SELECT wh.user_id, wh.guild_id, 'moon', wh.symbol, SUM(wh.amount)
          FROM wallet_holdings wh
          JOIN guild_tokens    gt
            ON gt.guild_id   = wh.guild_id
           AND gt.symbol     = wh.symbol
           AND gt.token_type = 'group'
         WHERE wh.network <> 'moon'
         GROUP BY wh.user_id, wh.guild_id, wh.symbol
        ON CONFLICT (user_id, guild_id, network, symbol)
        DO UPDATE SET amount = wallet_holdings.amount + EXCLUDED.amount;

        -- 2. Drop the non-moon rows whose balances are now merged.
        DELETE FROM wallet_holdings AS wh
         USING guild_tokens AS gt
         WHERE gt.guild_id   = wh.guild_id
           AND gt.symbol     = wh.symbol
           AND gt.token_type = 'group'
           AND wh.network    <> 'moon';
    END IF;
END $$;

COMMIT;
