-- 0252_buddy_arena_map.sql
-- Buddy Battles expansion: arena map travel state, zone trophies,
-- battle consumable inventory, and tournament progression.
--
-- The arena used to be a flat infinite queue (services/buddy_economy.py
-- resolve_arena_battle). This migration adds the scaffolding for a
-- branching map of 14 zones across 3 regions plus a final champion
-- tournament, so a player can travel zone-by-zone, fight tier-matched
-- AI in each, clear region bosses, and unlock the tournament bracket.
--
-- Battle consumables (Quick Berry, Phoenix Tear, etc.) are added via a
-- bait_inventory-style JSONB column on user_buddy_economy so the
-- battle view can read/write per-user counts without joining a second
-- table. Catalogue lives in buddies_config.BATTLE_CONSUMABLES.

-- ── Travel state ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cc_buddy_map_progress (
    guild_id              BIGINT       NOT NULL,
    user_id               BIGINT       NOT NULL,
    current_zone_id       TEXT         NOT NULL DEFAULT 'plains_gate',
    cleared_zones         TEXT[]       NOT NULL DEFAULT '{}',
    region_unlocks        TEXT[]       NOT NULL DEFAULT ARRAY['plains']::TEXT[],
    tournament_state      TEXT         NOT NULL DEFAULT 'locked',
    tournament_round      INTEGER      NOT NULL DEFAULT 0,
    last_travel_at        TIMESTAMPTZ,
    last_zone_battle_at   TIMESTAMPTZ,
    map_seed              BIGINT       NOT NULL DEFAULT 0,
    champion_count        INTEGER      NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id),
    CONSTRAINT cc_buddy_map_tournament_state_chk
        CHECK (tournament_state IN ('locked','qualified','in_progress','champion')),
    CONSTRAINT cc_buddy_map_tournament_round_chk
        CHECK (tournament_round BETWEEN 0 AND 4)
);

CREATE INDEX IF NOT EXISTS cc_buddy_map_progress_zone_idx
    ON cc_buddy_map_progress (guild_id, current_zone_id);

CREATE INDEX IF NOT EXISTS cc_buddy_map_progress_tourney_idx
    ON cc_buddy_map_progress (guild_id, tournament_state);


-- ── Per-zone trophy ledger ──────────────────────────────────────────────
-- One row per (user, zone) recorded on first clear; best_score is
-- rounds-remaining (lower is better, since a fast clear takes fewer
-- rounds). Used by the map renderer to stamp trophy badges and by
-- ,buddy map status to show personal bests.
CREATE TABLE IF NOT EXISTS cc_buddy_zone_trophies (
    guild_id     BIGINT       NOT NULL,
    user_id      BIGINT       NOT NULL,
    zone_id      TEXT         NOT NULL,
    cleared_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    best_score   INTEGER      NOT NULL DEFAULT 0,
    clear_count  INTEGER      NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, user_id, zone_id)
);

CREATE INDEX IF NOT EXISTS cc_buddy_zone_trophies_zone_idx
    ON cc_buddy_zone_trophies (guild_id, zone_id, best_score DESC);


-- ── Battle consumable inventory ─────────────────────────────────────────
-- Mirrors user_fishing.bait_inventory: a JSONB {item_key: int_count} on
-- user_buddy_economy. Loaded into Fighter.item_cd at battle start,
-- decremented on use, and re-saved at battle end.
ALTER TABLE user_buddy_economy
    ADD COLUMN IF NOT EXISTS battle_inventory JSONB NOT NULL DEFAULT '{}'::jsonb;


-- ── Tournament history (one row per tournament run) ─────────────────────
CREATE TABLE IF NOT EXISTS cc_buddy_tournament_runs (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT       NOT NULL,
    user_id       BIGINT       NOT NULL,
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ,
    final_round   INTEGER      NOT NULL DEFAULT 0,
    outcome       TEXT         NOT NULL DEFAULT 'in_progress',
    buddy_id      BIGINT,
    CONSTRAINT cc_buddy_tournament_outcome_chk
        CHECK (outcome IN ('in_progress','champion','eliminated'))
);

CREATE INDEX IF NOT EXISTS cc_buddy_tournament_runs_user_idx
    ON cc_buddy_tournament_runs (guild_id, user_id, started_at DESC);
