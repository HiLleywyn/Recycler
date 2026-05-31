-- Burn all SUN held in the community reserve (user_id=0).
-- The SUN vault fee split has been removed; any SUN accumulated
-- in the reserve is permanently removed from circulation.

-- Decrement circulating supply by the amount being burned
UPDATE crypto_prices
SET    circulating_supply = circulating_supply - COALESCE(
           (SELECT SUM(sd.amount)
            FROM   savings_deposits sd
            WHERE  sd.user_id = 0
              AND  sd.symbol  = 'SUN'
              AND  sd.guild_id = crypto_prices.guild_id),
           0)
WHERE  symbol = 'SUN'
  AND  EXISTS (
      SELECT 1 FROM savings_deposits
      WHERE user_id = 0 AND symbol = 'SUN' AND guild_id = crypto_prices.guild_id AND amount > 0
  );

-- Delete the SUN reserve deposits
DELETE FROM savings_deposits
WHERE  user_id = 0
  AND  symbol  = 'SUN';
