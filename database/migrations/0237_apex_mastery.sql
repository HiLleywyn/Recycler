-- V3 Pillar 2: Apex Mastery -- cross-system meta-progression
--
-- Every minigame in Discoin had its own XP track. A player with 10k
-- fishing levels got nothing from it outside fishing. Apex Mastery
-- binds nine disciplines (fisher / farmer / delver / trader / gambler
-- / raider / tamer / validator / crafter) into one career: each
-- discipline emits mastery XP into a per-track row, levelling a track
-- grants mastery points, points unlock nodes on a fixed ~80-node
-- skill tree that grants permanent passive buffs applied across the
-- whole bot.

CREATE TABLE IF NOT EXISTS user_mastery (
    guild_id      BIGINT      NOT NULL,
    user_id       BIGINT      NOT NULL,
    track         TEXT        NOT NULL,
    xp            BIGINT      NOT NULL DEFAULT 0,
    level         INTEGER     NOT NULL DEFAULT 1,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id, track)
);

CREATE INDEX IF NOT EXISTS user_mastery_user_idx
    ON user_mastery (guild_id, user_id);

CREATE TABLE IF NOT EXISTS user_mastery_nodes (
    guild_id      BIGINT      NOT NULL,
    user_id       BIGINT      NOT NULL,
    node_id       TEXT        NOT NULL,
    unlocked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id, node_id)
);

CREATE INDEX IF NOT EXISTS user_mastery_nodes_user_idx
    ON user_mastery_nodes (guild_id, user_id);

CREATE TABLE IF NOT EXISTS user_mastery_meta (
    guild_id            BIGINT      NOT NULL,
    user_id             BIGINT      NOT NULL,
    points_spent        INTEGER     NOT NULL DEFAULT 0,
    points_available    INTEGER     NOT NULL DEFAULT 0,
    last_reset_at       TIMESTAMPTZ,
    resets_used         INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
