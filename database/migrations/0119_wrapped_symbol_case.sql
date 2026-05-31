-- Rename wrapped-coin Config keys from mBTC / mSUN to MBTC / MSUN so every
-- code path (which already uppercases on write) and every data row agree on
-- the same symbol. Also rescues any user balances that got stranded in the
-- wrong table while the case mismatch was live.
--
-- Background of the bug: Config.TOKENS used mixed-case keys ("mBTC", "mSUN")
-- but framework/scale + update_wallet_holding + make_pool_id all force
-- UPPER on the symbol column. So:
--   * crypto_prices was seeded with 'mBTC' (Config key verbatim).
--   * wallet_holdings rows were written with 'MBTC' (write helper uppercased).
--   * Pool rows used 'MBTC' (make_pool_id uppercases).
--   * Swap handlers looked up all_tokens['MBTC'] -> missed -> empty network
--     -> credit fell through to CeFi crypto_holdings with 'MBTC' and the
--     UI labelled it "Other Network".
--
-- After this migration all four tables speak MBTC/MSUN and any stuck CeFi
-- balances move to the Moon Network DeFi wallet where swap output was
-- supposed to land.
--
-- Idempotent:
--   * UPPER(symbol) returns the same value once the rename completes.
--   * The CeFi sweep deletes the source rows after upsert, so a second run
--     finds zero matching source rows and no-ops.

-- ── 1. Normalize crypto_prices symbols ─────────────────────────────────────
-- Only two symbols in scope; explicit UPSERT so a pre-existing uppercase
-- row (race condition after partial deploys) absorbs the lowercase row's
-- numbers instead of throwing a unique-key violation.
DO $$
DECLARE
    _row RECORD;
BEGIN
    FOR _row IN
        SELECT symbol, guild_id, price, open_price, day_high, day_low,
               circulating_supply, ath
          FROM crypto_prices
         WHERE symbol IN ('mBTC', 'mSUN')
    LOOP
        INSERT INTO crypto_prices
            (symbol, guild_id, price, open_price, day_high, day_low,
             circulating_supply, ath)
        VALUES
            (UPPER(_row.symbol), _row.guild_id, _row.price, _row.open_price,
             _row.day_high, _row.day_low, _row.circulating_supply, _row.ath)
        ON CONFLICT (symbol, guild_id) DO NOTHING;
        DELETE FROM crypto_prices
         WHERE symbol = _row.symbol AND guild_id = _row.guild_id;
    END LOOP;
END $$;

-- ── 2. Move stranded CeFi balances into Moon DeFi wallet ───────────────────
-- Any crypto_holdings row with MBTC / MSUN is a leftover from the pre-fix
-- swap path. Move it (summing amounts if the user also has a row on the
-- right side) and log the count.
DO $$
DECLARE
    swept INT;
BEGIN
    WITH moved AS (
        SELECT user_id, guild_id, symbol, amount
          FROM crypto_holdings
         WHERE symbol IN ('MBTC', 'MSUN')
           AND amount > 0
    )
    INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
    SELECT user_id, guild_id, 'moon', symbol, amount
      FROM moved
    ON CONFLICT (user_id, guild_id, network, symbol)
    DO UPDATE SET amount = wallet_holdings.amount + EXCLUDED.amount;

    GET DIAGNOSTICS swept = ROW_COUNT;
    IF swept > 0 THEN
        RAISE NOTICE 'migration 0119: swept % stranded MBTC/MSUN CeFi balance(s) into Moon DeFi wallets',
                     swept;
    END IF;

    -- Zero out the source CeFi rows so the balance can't be double-spent
    -- or re-swept on a subsequent run. Deleting the row entirely keeps
    -- the table clean.
    DELETE FROM crypto_holdings
     WHERE symbol IN ('MBTC', 'MSUN');
END $$;
