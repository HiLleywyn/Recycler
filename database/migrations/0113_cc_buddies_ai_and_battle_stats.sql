-- Phase 5 of the CC Buddy system: battle record tracking, AI-backed
-- personality memory, and a full previous-owner history chain.
--
-- Three additions:
--   1. Battle record columns (wins, losses, battle_count, last_battle_at)
--      let the panel show a W-L line and let us drive a battle-only
--      leaderboard that isn't just level/XP.
--
--   2. ai_memory JSONB is a per-buddy memory blob the AI reply service
--      (services/buddy_ai.py) reads to build personality context and
--      writes back after each interaction. Shape:
--         {
--           "traits":        ["food obsessed", "clingy", ...],
--           "quirks":        ["names every rock", ...],
--           "recent_events": [{"ts": 1700..., "kind": "pet|feed|talk|battle|adopt|reclaim|shelter|runaway", "summary": "..."}],
--           "owner_notes":   { "<uid>": "short note the buddy has about this owner" }
--         }
--      Capped to ~20 events by the writer; oldest drop first.
--
--   3. previous_owners JSONB is an append-only history of PAST owners
--      only. Each entry represents one completed ownership tenure.
--      Current ownership is tracked by the live owner_user_id column;
--      this blob is purely "people who used to own me and how it
--      ended". Used by the AI so buddies remember (and trash-talk)
--      banned or abandoning ex-owners, even after the user record is
--      gone. Shape:
--         [
--           {"user_id": 123, "display_name": "Foo",
--            "from_ts": 1700..., "to_ts": 1701...,
--            "reason": "surrendered|ran_away|left_guild|banned"}
--         ]
--      Entries are appended when a buddy transitions out of 'owned'
--      (shelter intake). Adoption / reclaim does NOT write here.
--
-- All defaults are safe: new columns land with 0 wins/losses and empty
-- JSONB blobs. Existing rows get previous_owners backfilled from their
-- current owner_user_id so the AI doesn't think long-established
-- buddies are strangers to their owner.

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS wins            INTEGER      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS losses          INTEGER      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS battle_count    INTEGER      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_battle_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ai_memory       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS previous_owners JSONB        NOT NULL DEFAULT '[]'::jsonb;

-- Guard the counters: battle_count must equal wins + losses + draws, but
-- draws aren't tracked yet so we only assert non-negative. Keeping it
-- permissive makes future additions (e.g. a draws column) painless.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddies_battle_counts_chk'
    ) THEN
        ALTER TABLE cc_buddies
            ADD CONSTRAINT cc_buddies_battle_counts_chk
            CHECK (wins >= 0 AND losses >= 0 AND battle_count >= 0) NOT VALID;
        ALTER TABLE cc_buddies VALIDATE CONSTRAINT cc_buddies_battle_counts_chk;
    END IF;
END $$;

-- Battle leaderboard lookup path: wins desc, then battle_count asc so a
-- 10-2 record outranks a 10-20 record on ties. Partial index -- shelter
-- rows never show on the battle board.
CREATE INDEX IF NOT EXISTS cc_buddies_battle_board_idx
    ON cc_buddies (guild_id, wins DESC, battle_count ASC, id ASC)
    WHERE status = 'owned';

-- Backfill previous_owners for shelter buddies that already have a
-- former_owner_id on the row. Currently-owned buddies get left as an
-- empty list -- their current owner is tracked by owner_user_id, and
-- this blob is strictly the completed-ownership history. The from_ts
-- falls back to hatched_at because that's the nearest signal we have
-- for "when the relationship started".
UPDATE cc_buddies
   SET previous_owners = jsonb_build_array(
           jsonb_build_object(
               'user_id',      former_owner_id,
               'display_name', NULL,
               'from_ts',      EXTRACT(EPOCH FROM COALESCE(hatched_at, NOW()))::bigint,
               'to_ts',        EXTRACT(EPOCH FROM COALESCE(abandoned_at, NOW()))::bigint,
               'reason',       COALESCE(abandoned_reason, 'surrendered')
           )
       )
 WHERE status = 'shelter'
   AND former_owner_id IS NOT NULL
   AND (previous_owners IS NULL OR previous_owners = '[]'::jsonb);
