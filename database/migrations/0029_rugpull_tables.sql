-- 0029_rugpull_tables.sql
-- Rugpull minigame: King of Rugs role competition

CREATE TABLE IF NOT EXISTS rugpull_king (
    guild_id      BIGINT  NOT NULL PRIMARY KEY,
    user_id       BIGINT  NOT NULL,
    vault_amount  NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    crowned_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_rugpull_king_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rugpull_stats (
    user_id            BIGINT NOT NULL,
    guild_id           BIGINT NOT NULL,
    wins               INTEGER NOT NULL DEFAULT 0,
    losses             INTEGER NOT NULL DEFAULT 0,
    total_wagered      NUMERIC(28,8) NOT NULL DEFAULT 0.0,
    total_hold_seconds BIGINT NOT NULL DEFAULT 0,
    longest_hold_secs  BIGINT NOT NULL DEFAULT 0,
    last_crowned_at    TIMESTAMPTZ,
    last_dethroned_at  TIMESTAMPTZ,
    PRIMARY KEY (user_id, guild_id),
    CONSTRAINT fk_rugpull_stats_guild FOREIGN KEY (guild_id)
        REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);
