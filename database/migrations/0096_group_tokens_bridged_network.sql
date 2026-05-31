-- 0096: Move every group token onto the bridged "Group Network" pseudo-network.
--
-- Group tokens used to live on the founder's mining chain (Bitcoin or Sun),
-- which meant a COOK/FEM partnership pool tripped the cross-network swap
-- blocker even though the pool itself was perfectly valid. The new model:
--
--   guild_tokens.network        = 'Group Network'   (bridged, used by swap)
--   mining_groups.token_network = 'Bitcoin Network' (mining chain, vault pair)
--
-- Every group token now shares the same logical network, so cross-group swaps
-- work regardless of which PoW chain each founder picked. Vault pools still
-- pair the token with the mining chain's native coin (e.g. COOK/BTC).

-- Step 1: retag every group token on guild_tokens.
UPDATE guild_tokens
   SET network = 'Group Network'
 WHERE token_type = 'group'
   AND (network IS DISTINCT FROM 'Group Network');

-- Step 2: fold every old-network wallet_holdings row for group tokens into a
-- single 'grp' row per (user_id, guild_id, symbol). A naive rename breaks
-- when the same (user, guild, symbol) has balances on two different old
-- networks (e.g. TRAN held on both 'btc' and 'sun' because the group
-- switched chains once) -- renaming both rows to 'grp' collides on the
-- (user_id, guild_id, network, symbol) primary key. Aggregate-first avoids
-- that while also merging into any pre-existing 'grp' row.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'wallet_holdings' AND column_name = 'network'
    ) THEN
        -- 2a. Sum every old-network row per (user, guild, symbol) into the
        --     canonical 'grp' row, creating or updating as needed.
        INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
        SELECT wh.user_id, wh.guild_id, 'grp', wh.symbol, SUM(wh.amount)
          FROM wallet_holdings wh
          JOIN guild_tokens    gt
            ON gt.guild_id   = wh.guild_id
           AND gt.symbol     = wh.symbol
           AND gt.token_type = 'group'
         WHERE wh.network <> 'grp'
         GROUP BY wh.user_id, wh.guild_id, wh.symbol
        ON CONFLICT (user_id, guild_id, network, symbol)
        DO UPDATE SET amount = wallet_holdings.amount + EXCLUDED.amount;

        -- 2b. Drop every old-network row for group tokens now that their
        --     balances have been merged into the 'grp' row.
        DELETE FROM wallet_holdings AS wh
         USING guild_tokens AS gt
         WHERE gt.guild_id   = wh.guild_id
           AND gt.symbol     = wh.symbol
           AND gt.token_type = 'group'
           AND wh.network    <> 'grp';
    END IF;
END $$;

-- Step 3: bring historical transaction rows in line with live holdings so
-- activity feeds, trade-volume charts, and per-network filters show the
-- bridged group tokens together instead of split across btc/sun.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'transactions' AND column_name = 'network'
    ) THEN
        UPDATE transactions AS t
           SET network = 'grp'
          FROM guild_tokens AS gt
         WHERE gt.guild_id   = t.guild_id
           AND gt.token_type = 'group'
           AND (gt.symbol = t.symbol_in OR gt.symbol = t.symbol_out)
           AND COALESCE(t.network, '') <> 'grp';
    END IF;
END $$;
