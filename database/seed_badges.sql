-- ============================================================================
-- Discoin v2  -  Default Badge Definitions
-- Run after schema_v2.sql to populate the badges table.
-- ============================================================================

INSERT INTO badges (badge_id, name, description, icon, category, requirement)
VALUES
    (
        'first_trade',
        'First Trade',
        'Complete your very first trade on the exchange.',
        '🔰',
        'trading',
        '{"total_trades": 1}'::jsonb
    ),
    (
        'century_trader',
        'Century Trader',
        'Execute 100 trades  -  you live and breathe the market.',
        '💯',
        'trading',
        '{"total_trades": 100}'::jsonb
    ),
    (
        'whale',
        'Whale',
        'Accumulate a net worth of 1,000,000 or more.',
        '🐋',
        'milestone',
        '{"net_worth": 1000000}'::jsonb
    ),
    (
        'diamond_hands',
        'Diamond Hands',
        'Hold a single token for 30 consecutive days without selling.',
        '💎',
        'trading',
        '{"held_token_30d": true}'::jsonb
    ),
    (
        'miner_1m',
        'Miner 1M',
        'Reach a combined hashrate of 1,000,000 across all rigs.',
        '⛏️',
        'mining',
        '{"total_hashrate": 1000000}'::jsonb
    ),
    (
        'degen',
        'Degen',
        'Wager a cumulative total of 100,000 in games.',
        '🎰',
        'gambling',
        '{"total_wagered": 100000}'::jsonb
    ),
    (
        'lucky_streak',
        'Lucky Streak',
        'Win 10 games in a row  -  fortune favors the bold.',
        '🍀',
        'gambling',
        '{"consecutive_wins": 10}'::jsonb
    ),
    (
        'pool_shark',
        'Pool Shark',
        'Maintain active LP positions in 5 or more pools simultaneously.',
        '🦈',
        'trading',
        '{"lp_positions": 5}'::jsonb
    ),
    (
        'validator',
        'Validator',
        'Register and run a PoS validator node on any network.',
        '🛡️',
        'staking',
        '{"pos_validator": true}'::jsonb
    ),
    (
        'millionaire',
        'Millionaire',
        'Reach a net worth of 1,000,000  -  welcome to the club.',
        '💰',
        'milestone',
        '{"net_worth": 1000000}'::jsonb
    )
ON CONFLICT (badge_id) DO NOTHING;
