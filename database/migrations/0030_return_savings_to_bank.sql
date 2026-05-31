-- 0030_return_savings_to_bank.sql
-- Move all player savings deposits (USD and SUN) back to bank/CeFi holdings.
-- The savings vault feature is being removed; players should not lose funds.

BEGIN;

-- 1. USD savings -> bank balance
UPDATE users u
SET    bank = u.bank + s.amount
FROM   savings_deposits s
WHERE  s.user_id  = u.user_id
  AND  s.guild_id = u.guild_id
  AND  s.symbol   = 'USD'
  AND  s.amount   > 0;

-- 2. SUN savings -> CeFi holdings (crypto_holdings table)
--    If the user already has a SUN holding row, add to it.
--    If not, insert a new row.
INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
SELECT s.user_id, s.guild_id, 'SUN', s.amount
FROM   savings_deposits s
WHERE  s.symbol = 'SUN'
  AND  s.amount > 0
ON CONFLICT (user_id, guild_id, symbol)
DO UPDATE SET amount = crypto_holdings.amount + EXCLUDED.amount;

-- 3. Delete all savings deposit records (both USD and SUN)
DELETE FROM savings_deposits
WHERE  symbol IN ('USD', 'SUN')
  AND  amount > 0;

COMMIT;
