-- Fix group members whose user_mining_config.mode was never set to 'group'
-- when they joined. These users are in mining_group_members but get_group_miners()
-- never returns them, so their group gets no hashrate and no rewards.

-- Set mode='group' for all members currently in mining_group_members
INSERT INTO user_mining_config (user_id, guild_id, mode)
SELECT mgm.user_id, mgm.guild_id, 'group'
FROM mining_group_members mgm
ON CONFLICT (user_id, guild_id) DO UPDATE SET mode = 'group';

-- Remove them from mining_pool_members (can't be in pool and group simultaneously)
DELETE FROM mining_pool_members mp
WHERE EXISTS (
    SELECT 1 FROM mining_group_members mgm
    WHERE mgm.user_id = mp.user_id AND mgm.guild_id = mp.guild_id
);
