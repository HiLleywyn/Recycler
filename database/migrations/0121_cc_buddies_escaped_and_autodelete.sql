-- Escaped shelter buddy world events + buddy message autodelete.
--
-- Two separate features folded into one migration because they ship in
-- the same release and neither is large enough to warrant its own file.
--
-- (a) Escaped status on cc_buddies.
--     The periodic "escaped buddy" world event pulls a random shelter
--     row, flips it to 'escaped', and posts a public Battle prompt in
--     the guild's bot channel. Anyone who wins the PvE fight adopts
--     the buddy. If the prompt expires unclaimed, the row flips back
--     to 'shelter'. Escaped rows are excluded from normal shelter
--     listings so the same buddy can't be adopted out from under an
--     active world event.
--
-- (b) buddy_message_delete_after on guild_settings.
--     Admins set how long (in seconds) buddy-related embeds linger
--     before auto-deleting. Applies to battle challenges, battle
--     results, escape events, and related UI. NULL or <= 0 disables.

-- (a) cc_buddies: add 'escaped' to the status CHECK, add an escaped_at
-- timestamp column. The CHECK constraint is recreated idempotently.
ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS escaped_at TIMESTAMPTZ NULL;

-- The original constraint from 0109 is named cc_buddies_status_chk (not
-- _check). We drop BOTH possible names to be idempotent against any
-- earlier broken run of this migration, then recreate under the
-- canonical _chk name. Migration 0122 re-applies this for deployments
-- that ran an earlier buggy version of this file.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddies_status_check'
    ) THEN
        ALTER TABLE cc_buddies DROP CONSTRAINT cc_buddies_status_check;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddies_status_chk'
    ) THEN
        ALTER TABLE cc_buddies DROP CONSTRAINT cc_buddies_status_chk;
    END IF;
    ALTER TABLE cc_buddies
        ADD CONSTRAINT cc_buddies_status_chk
        CHECK (status IN ('owned', 'shelter', 'escaped'));
END $$;

CREATE INDEX IF NOT EXISTS cc_buddies_escaped_idx
    ON cc_buddies (guild_id, escaped_at)
    WHERE status = 'escaped';

-- (b) guild_settings: buddy message autodelete (per-guild, seconds).
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS buddy_message_delete_after INTEGER NULL;
