-- 0193_buddy_capture_origin.sql
--
-- Adds capture_message_id + capture_channel_id to cc_buddies so an
-- admin can look up which buddy joined a player's collection from a
-- given Discord message id. The fishing wild-capture flow now stamps
-- the battle-result message id onto the new row immediately after the
-- result embed lands; players who say "I caught a buddy in this
-- message but I can't find it" can be answered by joining the message
-- id back to the buddy row instead of trawling cc_buddies by hatched_at.
--
-- Both columns are nullable: legacy rows (and any non-fishing capture
-- path -- daycare hatch, breeding, admin spawn) carry NULL. The
-- presence of a value is the signal that this buddy came from a wild
-- capture and we know which message announced it.
--
-- Discord message + channel ids are 64-bit snowflakes, so BIGINT.
-- INDEXED on the message id so ,admin buddy recover <message_id> is
-- O(log n) regardless of how big cc_buddies gets.

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS capture_message_id BIGINT,
    ADD COLUMN IF NOT EXISTS capture_channel_id BIGINT;

CREATE INDEX IF NOT EXISTS cc_buddies_capture_message_idx
    ON cc_buddies (capture_message_id)
 WHERE capture_message_id IS NOT NULL;
