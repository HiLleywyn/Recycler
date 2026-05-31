-- One-fight-at-a-time blocker.
--
-- Discoin lets a player engage in PvP buddy battles, delve wild
-- battles, fish wild battles, farm wild battles, and escaped-buddy
-- world events independently. Without coordination, a player can
-- start a buddy PvP and a delve wild battle and a fish wild battle
-- simultaneously, then click buttons in unpredictable order and
-- see weird state (e.g. HP from one battle leaking into another's
-- resolution because both fight views read the same active buddy
-- row).
--
-- ``active_fight_locks`` is the single source of truth: at most one
-- row per (guild, user). Entry-point commands (``,buddy battle``,
-- ``,delve battle``, ``,farm battle``, fish wild challenge button,
-- escape-event Challenge button) call ``services.fight_lock.acquire``
-- before kicking off; resolution paths call ``release``. A TTL-
-- expiry index lets stale locks self-clean so a crashed bot never
-- traps a player forever.
--
-- DB-side clocks via ``EXTRACT(EPOCH FROM (NOW() - locked_at))`` per
-- the project rule -- never compare Python now() to a Postgres
-- TIMESTAMPTZ.

CREATE TABLE IF NOT EXISTS active_fight_locks (
    guild_id    BIGINT       NOT NULL,
    user_id     BIGINT       NOT NULL,
    -- Kind labels we use today: buddy_pvp, fish_wild, delve_wild,
    -- farm_wild, escape_event. New kinds are free to add -- the
    -- service treats this column as opaque.
    lock_kind   TEXT         NOT NULL,
    -- Opaque ref the caller can stash for diagnostics (battle id,
    -- challenger message id, etc.). Never read for routing.
    lock_ref    TEXT,
    locked_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Soft TTL. ``services.fight_lock.acquire`` lets a new caller
    -- take over an existing row whose expires_at is in the past,
    -- so a hung battle never blocks the player permanently. Live
    -- battles refresh expires_at on each round.
    expires_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW() + INTERVAL '8 minutes',
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_active_fight_locks_expires
    ON active_fight_locks (expires_at);
