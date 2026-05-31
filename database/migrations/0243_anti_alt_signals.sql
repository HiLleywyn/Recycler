-- V3 hardening: anti-alt detection signals.
--
-- The pre-V3 economy_security cog had per-command cooldowns + per-user
-- rate limits, but nothing that flagged multi-account farming patterns
-- (two accounts joining within 30 seconds, identical buy/sell pairs,
-- shared payout flow, etc.). V3 ships the data layer for those
-- heuristics; the actual flagging surface stays in
-- cogs/economy_security.py.
--
-- Soft-flag only. No auto-bans. The admin panel surfaces flagged
-- pairs so operators decide.

CREATE TABLE IF NOT EXISTS user_security_signals (
    id            BIGSERIAL    PRIMARY KEY,
    user_id       BIGINT       NOT NULL,
    guild_id      BIGINT       NOT NULL,
    signal_kind   TEXT         NOT NULL,    -- e.g. 'twin_join', 'shared_payout', 'lockstep_trade'
    other_user_id BIGINT,
    payload_json  JSONB,
    severity      INTEGER      NOT NULL DEFAULT 1 CHECK (severity BETWEEN 1 AND 5),
    flagged_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ,
    resolved_by   BIGINT,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS user_security_signals_pair_idx
    ON user_security_signals (guild_id, user_id, other_user_id)
    WHERE other_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS user_security_signals_unresolved_idx
    ON user_security_signals (guild_id, flagged_at DESC)
    WHERE resolved_at IS NULL;
