-- 0075_scaled_integers.sql
-- Migrate all monetary/token-amount columns from NUMERIC(28,8) to NUMERIC(36,0)
-- using a x10^18 scale factor (1 human unit = 1_000_000_000_000_000_000 raw units).
--
-- Columns that represent RATES, PRICES, or MULTIPLIERS (reward_rate, price,
-- faucet_multiplier, tax_rate, etc.) are intentionally left unchanged.
--
-- Conversion factor: new_raw = ROUND(old_human * 1000000000000000000)
-- This is exact because NUMERIC(28,8) has at most 8 decimal places, and
-- 10^18 / 10^8 = 10^10 -- so any value * 10^18 produces an exact integer.

-- ============================================================================
-- 1. CORE: users
-- ============================================================================
ALTER TABLE users
    ALTER COLUMN wallet TYPE NUMERIC(36,0) USING ROUND(wallet * 1000000000000000000),
    ALTER COLUMN bank   TYPE NUMERIC(36,0) USING ROUND(bank   * 1000000000000000000);
ALTER TABLE users ALTER COLUMN wallet SET DEFAULT 20000000000000000000;
ALTER TABLE users ALTER COLUMN bank   SET DEFAULT 0;

-- ============================================================================
-- 2. GUILD SETTINGS: amount columns only (not rates/pct/multipliers)
-- ============================================================================
ALTER TABLE guild_settings
    ALTER COLUMN drop_min          TYPE NUMERIC(36,0) USING ROUND(drop_min          * 1000000000000000000),
    ALTER COLUMN drop_max          TYPE NUMERIC(36,0) USING ROUND(drop_max          * 1000000000000000000),
    ALTER COLUMN platform_fee_min  TYPE NUMERIC(36,0) USING ROUND(platform_fee_min  * 1000000000000000000),
    ALTER COLUMN platform_fee_max  TYPE NUMERIC(36,0) USING ROUND(platform_fee_max  * 1000000000000000000),
    ALTER COLUMN whale_alert_threshold TYPE NUMERIC(36,0) USING ROUND(whale_alert_threshold * 1000000000000000000);

-- ============================================================================
-- 3. GUILD TOKENS: amount columns (not start_price, daily_vol, tx_fee_rate)
-- ============================================================================
ALTER TABLE guild_tokens
    ALTER COLUMN circulating_supply TYPE NUMERIC(36,0) USING ROUND(circulating_supply * 1000000000000000000),
    ALTER COLUMN max_supply         TYPE NUMERIC(36,0) USING ROUND(max_supply         * 1000000000000000000),
    ALTER COLUMN gas_fee            TYPE NUMERIC(36,0) USING ROUND(gas_fee            * 1000000000000000000);
ALTER TABLE guild_tokens ALTER COLUMN circulating_supply SET DEFAULT 0;
ALTER TABLE guild_tokens ALTER COLUMN gas_fee SET DEFAULT 50000000000000000;

-- ============================================================================
-- 4. MARKET DATA: circulating_supply only (not price/high/low/ath -- rates)
-- ============================================================================
ALTER TABLE crypto_prices
    ALTER COLUMN circulating_supply TYPE NUMERIC(36,0) USING ROUND(circulating_supply * 1000000000000000000);
ALTER TABLE crypto_prices ALTER COLUMN circulating_supply SET DEFAULT 0;

-- ============================================================================
-- 5. HOLDINGS
-- ============================================================================
ALTER TABLE crypto_holdings
    ALTER COLUMN amount TYPE NUMERIC(36,0) USING ROUND(amount * 1000000000000000000);
ALTER TABLE crypto_holdings ALTER COLUMN amount SET DEFAULT 0;

ALTER TABLE wallet_holdings
    ALTER COLUMN amount TYPE NUMERIC(36,0) USING ROUND(amount * 1000000000000000000);
ALTER TABLE wallet_holdings ALTER COLUMN amount SET DEFAULT 0;

-- ============================================================================
-- 6. PRICE CANDLES: volume only (open/high/low/close are prices)
-- ============================================================================
ALTER TABLE price_candles
    ALTER COLUMN volume TYPE NUMERIC(36,0) USING ROUND(volume * 1000000000000000000);
ALTER TABLE price_candles ALTER COLUMN volume SET DEFAULT 0;

-- ============================================================================
-- 7. TRANSACTIONS: amount_in, amount_out, gas_fee (not price_at)
-- ============================================================================
ALTER TABLE transactions
    ALTER COLUMN amount_in  TYPE NUMERIC(36,0) USING ROUND(amount_in  * 1000000000000000000),
    ALTER COLUMN amount_out TYPE NUMERIC(36,0) USING ROUND(amount_out * 1000000000000000000),
    ALTER COLUMN gas_fee    TYPE NUMERIC(36,0) USING ROUND(gas_fee    * 1000000000000000000);
ALTER TABLE transactions ALTER COLUMN gas_fee SET DEFAULT 0;

-- ============================================================================
-- 8. LIQUIDITY POOLS
-- ============================================================================
ALTER TABLE pools
    ALTER COLUMN reserve_a TYPE NUMERIC(36,0) USING ROUND(reserve_a * 1000000000000000000),
    ALTER COLUMN reserve_b TYPE NUMERIC(36,0) USING ROUND(reserve_b * 1000000000000000000),
    ALTER COLUMN total_lp   TYPE NUMERIC(36,0) USING ROUND(total_lp  * 1000000000000000000);
ALTER TABLE pools ALTER COLUMN reserve_a SET DEFAULT 0;
ALTER TABLE pools ALTER COLUMN reserve_b SET DEFAULT 0;
ALTER TABLE pools ALTER COLUMN total_lp   SET DEFAULT 0;

ALTER TABLE lp_positions
    ALTER COLUMN lp_shares TYPE NUMERIC(36,0) USING ROUND(lp_shares * 1000000000000000000);
ALTER TABLE lp_positions ALTER COLUMN lp_shares SET DEFAULT 0;

ALTER TABLE lp_snapshots
    ALTER COLUMN entry_res_a_per_lp TYPE NUMERIC(36,0) USING ROUND(entry_res_a_per_lp * 1000000000000000000),
    ALTER COLUMN entry_res_b_per_lp TYPE NUMERIC(36,0) USING ROUND(entry_res_b_per_lp * 1000000000000000000);

-- ============================================================================
-- 9. STAKING
-- ============================================================================
ALTER TABLE stakes
    ALTER COLUMN amount TYPE NUMERIC(36,0) USING ROUND(amount * 1000000000000000000);
ALTER TABLE stakes ALTER COLUMN amount SET DEFAULT 0;

ALTER TABLE pos_validators
    ALTER COLUMN stake_amount         TYPE NUMERIC(36,0) USING ROUND(stake_amount         * 1000000000000000000),
    ALTER COLUMN total_rewards_earned TYPE NUMERIC(36,0) USING ROUND(total_rewards_earned * 1000000000000000000);
ALTER TABLE pos_validators ALTER COLUMN stake_amount         SET DEFAULT 0;
ALTER TABLE pos_validators ALTER COLUMN total_rewards_earned SET DEFAULT 0;

ALTER TABLE pos_delegations
    ALTER COLUMN amount       TYPE NUMERIC(36,0) USING ROUND(amount       * 1000000000000000000),
    ALTER COLUMN total_earned TYPE NUMERIC(36,0) USING ROUND(total_earned * 1000000000000000000);
ALTER TABLE pos_delegations ALTER COLUMN amount       SET DEFAULT 0;
ALTER TABLE pos_delegations ALTER COLUMN total_earned SET DEFAULT 0;

-- ============================================================================
-- 10. MINING: reward amounts only (not hashrate/difficulty)
-- ============================================================================
ALTER TABLE mining_network
    ALTER COLUMN current_reward TYPE NUMERIC(36,0) USING ROUND(current_reward * 1000000000000000000);
ALTER TABLE mining_network ALTER COLUMN current_reward SET DEFAULT 50000000000000000000;

ALTER TABLE pow_network_state
    ALTER COLUMN current_reward TYPE NUMERIC(36,0) USING ROUND(current_reward * 1000000000000000000);
ALTER TABLE pow_network_state ALTER COLUMN current_reward SET DEFAULT 0;

ALTER TABLE mining_blocks
    ALTER COLUMN reward TYPE NUMERIC(36,0) USING ROUND(reward * 1000000000000000000);

-- ============================================================================
-- 11. BLOCKCHAIN / VALIDATOR
-- ============================================================================
ALTER TABLE mempool
    ALTER COLUMN gas_fee TYPE NUMERIC(36,0) USING ROUND(gas_fee * 1000000000000000000);
ALTER TABLE mempool ALTER COLUMN gas_fee SET DEFAULT 0;

ALTER TABLE validator_blocks
    ALTER COLUMN total_gas_collected TYPE NUMERIC(36,0) USING ROUND(total_gas_collected * 1000000000000000000),
    ALTER COLUMN validator_reward    TYPE NUMERIC(36,0) USING ROUND(validator_reward    * 1000000000000000000),
    ALTER COLUMN treasury_cut        TYPE NUMERIC(36,0) USING ROUND(treasury_cut        * 1000000000000000000);
ALTER TABLE validator_blocks ALTER COLUMN total_gas_collected SET DEFAULT 0;
ALTER TABLE validator_blocks ALTER COLUMN validator_reward    SET DEFAULT 0;
ALTER TABLE validator_blocks ALTER COLUMN treasury_cut        SET DEFAULT 0;

ALTER TABLE network_base_fees
    ALTER COLUMN base_fee TYPE NUMERIC(36,0) USING ROUND(base_fee * 1000000000000000000);
ALTER TABLE network_base_fees ALTER COLUMN base_fee SET DEFAULT 0;

ALTER TABLE guild_treasury
    ALTER COLUMN balance TYPE NUMERIC(36,0) USING ROUND(balance * 1000000000000000000);
ALTER TABLE guild_treasury ALTER COLUMN balance SET DEFAULT 0;

-- ============================================================================
-- 12. LENDING
-- ============================================================================
ALTER TABLE loans
    ALTER COLUMN principal   TYPE NUMERIC(36,0) USING ROUND(principal   * 1000000000000000000),
    ALTER COLUMN outstanding TYPE NUMERIC(36,0) USING ROUND(outstanding * 1000000000000000000),
    ALTER COLUMN collateral  TYPE NUMERIC(36,0) USING ROUND(collateral  * 1000000000000000000);

ALTER TABLE sun_loans
    ALTER COLUMN collateral_sun TYPE NUMERIC(36,0) USING ROUND(collateral_sun * 1000000000000000000),
    ALTER COLUMN borrow_amount  TYPE NUMERIC(36,0) USING ROUND(borrow_amount  * 1000000000000000000),
    ALTER COLUMN outstanding    TYPE NUMERIC(36,0) USING ROUND(outstanding    * 1000000000000000000);

ALTER TABLE savings_deposits
    ALTER COLUMN amount TYPE NUMERIC(36,0) USING ROUND(amount * 1000000000000000000);

-- ============================================================================
-- 13. ITEMS (stones: staked_amount only, not xp which is a game-mechanic value)
-- ============================================================================
ALTER TABLE hashstones
    ALTER COLUMN staked_amount TYPE NUMERIC(36,0) USING ROUND(staked_amount * 1000000000000000000);
ALTER TABLE hashstones ALTER COLUMN staked_amount SET DEFAULT 0;

ALTER TABLE lockstones
    ALTER COLUMN staked_amount TYPE NUMERIC(36,0) USING ROUND(staked_amount * 1000000000000000000);
ALTER TABLE lockstones ALTER COLUMN staked_amount SET DEFAULT 0;

ALTER TABLE vaultstones
    ALTER COLUMN staked_amount TYPE NUMERIC(36,0) USING ROUND(staked_amount * 1000000000000000000);
ALTER TABLE vaultstones ALTER COLUMN staked_amount SET DEFAULT 0;

ALTER TABLE gambastones
    ALTER COLUMN staked_amount TYPE NUMERIC(36,0) USING ROUND(staked_amount * 1000000000000000000);
ALTER TABLE gambastones ALTER COLUMN staked_amount SET DEFAULT 0;

ALTER TABLE liqstones
    ALTER COLUMN staked_amount TYPE NUMERIC(36,0) USING ROUND(staked_amount * 1000000000000000000);
ALTER TABLE liqstones ALTER COLUMN staked_amount SET DEFAULT 0;

-- ============================================================================
-- 14. GAMES
-- ============================================================================
ALTER TABLE game_results
    ALTER COLUMN bet_amount TYPE NUMERIC(36,0) USING ROUND(bet_amount * 1000000000000000000),
    ALTER COLUMN payout     TYPE NUMERIC(36,0) USING ROUND(payout     * 1000000000000000000),
    ALTER COLUMN profit     TYPE NUMERIC(36,0) USING ROUND(profit     * 1000000000000000000);
ALTER TABLE game_results ALTER COLUMN bet_amount SET DEFAULT 0;
ALTER TABLE game_results ALTER COLUMN payout     SET DEFAULT 0;
ALTER TABLE game_results ALTER COLUMN profit     SET DEFAULT 0;

ALTER TABLE game_sessions
    ALTER COLUMN bet_amount TYPE NUMERIC(36,0) USING ROUND(bet_amount * 1000000000000000000);

-- ============================================================================
-- 15. USER PROFILES
-- ============================================================================
ALTER TABLE user_profiles
    ALTER COLUMN total_trade_volume TYPE NUMERIC(36,0) USING ROUND(total_trade_volume * 1000000000000000000),
    ALTER COLUMN realized_pnl       TYPE NUMERIC(36,0) USING ROUND(realized_pnl       * 1000000000000000000),
    ALTER COLUMN best_trade_pnl     TYPE NUMERIC(36,0) USING ROUND(best_trade_pnl     * 1000000000000000000),
    ALTER COLUMN worst_trade_pnl    TYPE NUMERIC(36,0) USING ROUND(worst_trade_pnl    * 1000000000000000000),
    ALTER COLUMN total_wagered      TYPE NUMERIC(36,0) USING ROUND(total_wagered      * 1000000000000000000),
    ALTER COLUMN total_game_profit  TYPE NUMERIC(36,0) USING ROUND(total_game_profit  * 1000000000000000000);
ALTER TABLE user_profiles ALTER COLUMN total_trade_volume SET DEFAULT 0;
ALTER TABLE user_profiles ALTER COLUMN realized_pnl       SET DEFAULT 0;
ALTER TABLE user_profiles ALTER COLUMN best_trade_pnl     SET DEFAULT 0;
ALTER TABLE user_profiles ALTER COLUMN worst_trade_pnl    SET DEFAULT 0;
ALTER TABLE user_profiles ALTER COLUMN total_wagered      SET DEFAULT 0;
ALTER TABLE user_profiles ALTER COLUMN total_game_profit  SET DEFAULT 0;

ALTER TABLE user_jobs
    ALTER COLUMN total_earned TYPE NUMERIC(36,0) USING ROUND(total_earned * 1000000000000000000);
ALTER TABLE user_jobs ALTER COLUMN total_earned SET DEFAULT 0;

ALTER TABLE pnl_snapshots
    ALTER COLUMN net_worth TYPE NUMERIC(36,0) USING ROUND(net_worth * 1000000000000000000);
ALTER TABLE pnl_snapshots ALTER COLUMN net_worth SET DEFAULT 0;

-- ============================================================================
-- 16. MINING GROUPS: reserve amounts (not reserve_pct which is a rate)
-- ============================================================================
ALTER TABLE mining_groups
    ALTER COLUMN reserve_sun TYPE NUMERIC(36,0) USING ROUND(reserve_sun * 1000000000000000000),
    ALTER COLUMN reserve_usd TYPE NUMERIC(36,0) USING ROUND(reserve_usd * 1000000000000000000);
ALTER TABLE mining_groups ALTER COLUMN reserve_sun SET DEFAULT 0;
ALTER TABLE mining_groups ALTER COLUMN reserve_usd SET DEFAULT 0;

-- ============================================================================
-- 17. RUGPULL
-- ============================================================================
ALTER TABLE rugpull_king
    ALTER COLUMN vault_amount  TYPE NUMERIC(36,0) USING ROUND(vault_amount  * 1000000000000000000),
    ALTER COLUMN sabotage_pool TYPE NUMERIC(36,0) USING ROUND(sabotage_pool * 1000000000000000000),
    ALTER COLUMN bounty_pool   TYPE NUMERIC(36,0) USING ROUND(bounty_pool   * 1000000000000000000);
ALTER TABLE rugpull_king ALTER COLUMN vault_amount  SET DEFAULT 0;
ALTER TABLE rugpull_king ALTER COLUMN sabotage_pool SET DEFAULT 0;
ALTER TABLE rugpull_king ALTER COLUMN bounty_pool   SET DEFAULT 0;

ALTER TABLE rugpull_stats
    ALTER COLUMN total_wagered   TYPE NUMERIC(36,0) USING ROUND(total_wagered   * 1000000000000000000),
    ALTER COLUMN bounties_placed TYPE NUMERIC(36,0) USING ROUND(bounties_placed * 1000000000000000000);
ALTER TABLE rugpull_stats ALTER COLUMN total_wagered   SET DEFAULT 0;
ALTER TABLE rugpull_stats ALTER COLUMN bounties_placed SET DEFAULT 0;

ALTER TABLE rugpull_history
    ALTER COLUMN wager TYPE NUMERIC(36,0) USING ROUND(wager * 1000000000000000000);

-- ============================================================================
-- 18. NETWORK VAULTS
-- ============================================================================
ALTER TABLE network_vaults
    ALTER COLUMN balance TYPE NUMERIC(36,0) USING ROUND(balance * 1000000000000000000);
ALTER TABLE network_vaults ALTER COLUMN balance SET DEFAULT 0;

-- ============================================================================
-- 19. PREDICTION MARKETS
-- ============================================================================
ALTER TABLE prediction_markets
    ALTER COLUMN prize_pool TYPE NUMERIC(36,0) USING ROUND(prize_pool * 1000000000000000000),
    ALTER COLUMN total_pool TYPE NUMERIC(36,0) USING ROUND(total_pool * 1000000000000000000);
ALTER TABLE prediction_markets ALTER COLUMN prize_pool SET DEFAULT 0;
ALTER TABLE prediction_markets ALTER COLUMN total_pool SET DEFAULT 0;

ALTER TABLE prediction_bets
    ALTER COLUMN amount TYPE NUMERIC(36,0) USING ROUND(amount * 1000000000000000000);

-- ============================================================================
-- 20. AUTO COMPOUND
-- ============================================================================
ALTER TABLE auto_compound_settings
    ALTER COLUMN total_compounded TYPE NUMERIC(36,0) USING ROUND(total_compounded * 1000000000000000000);
ALTER TABLE auto_compound_settings ALTER COLUMN total_compounded SET DEFAULT 0;

-- ============================================================================
-- 21. EXPLOIT
-- ============================================================================
ALTER TABLE exploit_stats
    ALTER COLUMN total_stolen TYPE NUMERIC(36,0) USING ROUND(total_stolen * 1000000000000000000),
    ALTER COLUMN total_lost   TYPE NUMERIC(36,0) USING ROUND(total_lost   * 1000000000000000000);
ALTER TABLE exploit_stats ALTER COLUMN total_stolen SET DEFAULT 0;
ALTER TABLE exploit_stats ALTER COLUMN total_lost   SET DEFAULT 0;

ALTER TABLE exploit_history
    ALTER COLUMN wager  TYPE NUMERIC(36,0) USING ROUND(wager  * 1000000000000000000),
    ALTER COLUMN stolen TYPE NUMERIC(36,0) USING ROUND(stolen * 1000000000000000000);
ALTER TABLE exploit_history ALTER COLUMN stolen SET DEFAULT 0;

-- ============================================================================
-- 22. STAKE BATCHES
-- ============================================================================
ALTER TABLE stake_batches
    ALTER COLUMN amount TYPE NUMERIC(36,0) USING ROUND(amount * 1000000000000000000);
ALTER TABLE stake_batches ALTER COLUMN amount SET DEFAULT 0;

-- ============================================================================
-- 23. GOVERNANCE
-- ============================================================================
ALTER TABLE governance_proposals
    ALTER COLUMN supply_snapshot TYPE NUMERIC(36,0) USING ROUND(supply_snapshot * 1000000000000000000);
ALTER TABLE governance_proposals ALTER COLUMN supply_snapshot SET DEFAULT 0;

ALTER TABLE governance_votes
    ALTER COLUMN voting_power TYPE NUMERIC(36,0) USING ROUND(voting_power * 1000000000000000000);
ALTER TABLE governance_votes ALTER COLUMN voting_power SET DEFAULT 0;

-- ============================================================================
-- 24. NFT
-- ============================================================================
ALTER TABLE nft_collections
    ALTER COLUMN mint_price TYPE NUMERIC(36,0) USING ROUND(mint_price * 1000000000000000000);
ALTER TABLE nft_collections ALTER COLUMN mint_price SET DEFAULT 0;

ALTER TABLE nft_listings
    ALTER COLUMN price TYPE NUMERIC(36,0) USING ROUND(price * 1000000000000000000);

ALTER TABLE nft_sales
    ALTER COLUMN price TYPE NUMERIC(36,0) USING ROUND(price * 1000000000000000000);

-- ============================================================================
-- 25. REPORTS / BOUNTIES
-- ============================================================================
ALTER TABLE reports
    ALTER COLUMN reward_amount TYPE NUMERIC(36,0) USING ROUND(reward_amount * 1000000000000000000);
ALTER TABLE reports ALTER COLUMN reward_amount SET DEFAULT 0;

ALTER TABLE bounties
    ALTER COLUMN reward_amount TYPE NUMERIC(36,0) USING ROUND(reward_amount * 1000000000000000000);
ALTER TABLE bounties ALTER COLUMN reward_amount SET DEFAULT 0;

-- ============================================================================
-- 26. SERVER EVENTS
-- ============================================================================
ALTER TABLE server_events
    ALTER COLUMN amount TYPE NUMERIC(36,0) USING ROUND(amount * 1000000000000000000);
ALTER TABLE server_events ALTER COLUMN amount SET DEFAULT 0;
