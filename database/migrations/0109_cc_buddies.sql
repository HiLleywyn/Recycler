-- CC Buddy system: per-user ASCII companions.
--
-- Two tables:
--   cc_buddies         -- single source of truth for every buddy (owned or shelter)
--   cc_buddy_hatches   -- append-only log; authoritative "one hatch per lifetime"
--
-- Phase 1 covers hatch + stats + rename. Shelter / adoption columns are
-- created here so Phase 2 needs no schema change.

CREATE TABLE IF NOT EXISTS cc_buddies (
    id                BIGSERIAL PRIMARY KEY,
    guild_id          BIGINT      NOT NULL,
    owner_user_id     BIGINT,                              -- NULL when status = 'shelter'
    former_owner_id   BIGINT,                              -- set when buddy enters shelter
    species           TEXT        NOT NULL,
    name              TEXT        NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'owned',  -- 'owned' | 'shelter'
    xp                BIGINT      NOT NULL DEFAULT 0,
    level             INTEGER     NOT NULL DEFAULT 1,
    hunger            INTEGER     NOT NULL DEFAULT 70,
    happiness         INTEGER     NOT NULL DEFAULT 70,
    energy            INTEGER     NOT NULL DEFAULT 70,
    hatched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_interacted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_xp_at        TIMESTAMPTZ,                         -- chat-XP cooldown clock (DB-side)
    last_rename_at    TIMESTAMPTZ,                         -- rename cooldown clock (DB-side)
    rename_count      INTEGER     NOT NULL DEFAULT 0,      -- analytics only, not for cooldown
    abandoned_at      TIMESTAMPTZ,
    abandoned_reason  TEXT,
    adoptable_after   TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT cc_buddies_status_chk CHECK (status IN ('owned', 'shelter')),
    CONSTRAINT cc_buddies_level_chk  CHECK (level >= 1),
    CONSTRAINT cc_buddies_mood_chk   CHECK (
        hunger    BETWEEN 0 AND 100
        AND happiness BETWEEN 0 AND 100
        AND energy    BETWEEN 0 AND 100
    )
);

-- Exactly one active buddy per user per guild (enforced in the DB).
-- Shelter rows (owner_user_id IS NULL) are excluded from the uniqueness.
CREATE UNIQUE INDEX IF NOT EXISTS cc_buddies_one_owned_per_user
    ON cc_buddies (guild_id, owner_user_id)
    WHERE status = 'owned' AND owner_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS cc_buddies_owner_idx
    ON cc_buddies (guild_id, owner_user_id)
    WHERE status = 'owned';

CREATE INDEX IF NOT EXISTS cc_buddies_shelter_idx
    ON cc_buddies (guild_id, adoptable_after)
    WHERE status = 'shelter';

-- Append-only hatch log. One row per lifetime hatch; UNIQUE (guild_id, user_id)
-- is the authoritative "has_ever_hatched" signal.
CREATE TABLE IF NOT EXISTS cc_buddy_hatches (
    guild_id       BIGINT      NOT NULL,
    user_id        BIGINT      NOT NULL,
    hatched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_species  TEXT        NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
