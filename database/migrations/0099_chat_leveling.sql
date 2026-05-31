-- Chat leveling: per-guild XP system with configurable rate, rank titles, and role rewards.
--
-- Per-user state. xp is cumulative total XP for the guild.
CREATE TABLE IF NOT EXISTS chat_levels (
    guild_id         BIGINT      NOT NULL,
    user_id          BIGINT      NOT NULL,
    xp               BIGINT      NOT NULL DEFAULT 0,
    level            INTEGER     NOT NULL DEFAULT 0,
    total_messages   BIGINT      NOT NULL DEFAULT 0,
    last_message_at  TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_levels_guild_xp ON chat_levels (guild_id, xp DESC);
CREATE INDEX IF NOT EXISTS idx_chat_levels_guild_level ON chat_levels (guild_id, level DESC);

-- Per-guild configuration. One row per guild.
-- Curve: xp_for_level_up(n) = curve_quad * n^2 + curve_lin * n + curve_base
-- Defaults mirror the classic MEE6 curve (5*n^2 + 50*n + 100).
CREATE TABLE IF NOT EXISTS chat_level_config (
    guild_id           BIGINT  PRIMARY KEY,
    enabled            BOOLEAN NOT NULL DEFAULT FALSE,
    xp_min             INTEGER NOT NULL DEFAULT 15,
    xp_max             INTEGER NOT NULL DEFAULT 25,
    cooldown_seconds   INTEGER NOT NULL DEFAULT 60,
    min_chars          INTEGER NOT NULL DEFAULT 4,
    announce_channel   BIGINT,
    dm_levelup         BOOLEAN NOT NULL DEFAULT FALSE,
    stack_roles        BOOLEAN NOT NULL DEFAULT TRUE,
    curve_quad         INTEGER NOT NULL DEFAULT 5,
    curve_lin          INTEGER NOT NULL DEFAULT 50,
    curve_base         INTEGER NOT NULL DEFAULT 100,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Role rewards. Multiple roles can be bound to the same level.
CREATE TABLE IF NOT EXISTS chat_level_roles (
    guild_id  BIGINT  NOT NULL,
    level     INTEGER NOT NULL,
    role_id   BIGINT  NOT NULL,
    PRIMARY KEY (guild_id, level, role_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_level_roles_guild ON chat_level_roles (guild_id, level);

-- Rank titles. One title per level threshold. Users display the highest
-- rank at or below their current level.
CREATE TABLE IF NOT EXISTS chat_level_ranks (
    guild_id   BIGINT  NOT NULL,
    level      INTEGER NOT NULL,
    rank_name  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, level)
);
