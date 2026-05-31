-- 0235_gamba_to_wallet_holdings.sql
--
-- Move every Gamba Network token (GBC + the 8 game-themed tokens) out of
-- ``crypto_holdings`` and into ``wallet_holdings`` on the ``gam`` network
-- short. This brings Gamba Network into line with every other earn-only
-- network (Lure / Crypt / Buddy / Harvest / Forge): players hold the coin
-- + earn-tokens in their DeFi wallet, ``,wallet list`` displays the
-- balances, and ``,wallet create gam`` works the same as the rest.
--
-- Before this migration, the gamba surface routed through
-- ``db.update_holding`` (CeFi crypto_holdings) which meant:
--   * ``,wallet create gam`` failed because "gam" wasn't a known network
--   * ``,wallet list`` never surfaced GBC even though the user owned some
--   * ``,gamba cashout`` worked but read off a different table than the
--     wallet UI showed, which made the GBC effectively invisible
--
-- The matching code change in services/gamba.py + cogs/{chess,checkers,
-- play}.py + services/buddy_economy.py switches every Gamba-token wallet
-- read/write to ``update_wallet_holding`` keyed by ``gam`` so the data
-- and the UI agree from now on.

-- Move existing balances. ON CONFLICT folds any pre-existing wallet_holdings
-- rows together so a player who somehow had both the CeFi and DeFi side
-- (e.g. from a manual admin transfer) ends up with a single combined row.
INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
SELECT user_id, guild_id, 'gam', symbol, amount
  FROM crypto_holdings
 WHERE symbol IN (
        'GBC', 'GAMBIT', 'CROWN', 'VEIN', 'PIP',
        'EDGE', 'ACE', 'NOIR', 'CHERRY'
       )
   AND amount > 0
    ON CONFLICT (user_id, guild_id, network, symbol)
    DO UPDATE SET amount = wallet_holdings.amount + EXCLUDED.amount;

-- Drop the now-orphaned crypto_holdings rows. Circulating supply was
-- already accounted for by the original update_holding call that wrote
-- those rows, so we deliberately DO NOT touch crypto_prices /
-- guild_tokens here -- the supply totals are unchanged, only the storage
-- table is.
DELETE FROM crypto_holdings
 WHERE symbol IN (
        'GBC', 'GAMBIT', 'CROWN', 'VEIN', 'PIP',
        'EDGE', 'ACE', 'NOIR', 'CHERRY'
       );
