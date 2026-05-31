-- 0232_command_usage.sql
--
-- Per-invocation log of every prefix command that fires through
-- Discoin.on_command, plus an aggregated all-time roll-up. The detail
-- rows back the 24h / 7d windows on ,admin commandstats; the totals
-- table keeps the cumulative count even if the detail table is later
-- pruned, so admins keep a persistent "since launch" reading that
-- survives bot resets.
--
-- command_path = ctx.command.qualified_name (e.g. 'admin give',
--                'fish cast', 'gamba slots'). Captures both the
--                top-level command and any nested subcommand.
-- args_text    = whatever the user typed after the qualified name,
--                trimmed and capped at 200 chars to keep the table
--                bounded against pathological inputs.
-- guild_id     = NULL for DM invocations on the detail table; the
--                totals table coalesces DMs to 0 because PK columns
--                in PostgreSQL must be NOT NULL.

CREATE TABLE IF NOT EXISTS command_usage (
    id           BIGSERIAL    PRIMARY KEY,
    guild_id     BIGINT,
    user_id      BIGINT       NOT NULL,
    command_path TEXT         NOT NULL,
    args_text    TEXT         NOT NULL DEFAULT '',
    used_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS command_usage_guild_used_idx
    ON command_usage (guild_id, used_at DESC);

CREATE INDEX IF NOT EXISTS command_usage_path_used_idx
    ON command_usage (command_path, used_at DESC);

CREATE INDEX IF NOT EXISTS command_usage_used_at_idx
    ON command_usage (used_at DESC);

CREATE TABLE IF NOT EXISTS command_usage_totals (
    guild_id     BIGINT       NOT NULL DEFAULT 0,
    command_path TEXT         NOT NULL,
    args_text    TEXT         NOT NULL DEFAULT '',
    total_count  BIGINT       NOT NULL DEFAULT 0,
    first_seen   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, command_path, args_text)
);

CREATE INDEX IF NOT EXISTS command_usage_totals_guild_path_idx
    ON command_usage_totals (guild_id, command_path);
