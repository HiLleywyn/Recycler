-- Beachcomb's BEACHCOMB_BAIT_POOL listed "cricket", but the BAIT
-- catalog never defined it. Players who rolled bait_stash and got
-- "cricket" picked ended up with a key in user_fishing.bait_inventory
-- they could never use for fishing (bait_meta returns None) and
-- nft_backfill spammed warnings on every boot trying to find a
-- bait.cricket contract that was never deployed.
--
-- Fix: rewrite every "cricket" key in bait_inventory to "worm",
-- folding the count into the existing "worm" stack if one is present.
-- Capped at the bait's max_stack via the same rule the live code uses
-- (worm.max_stack = 500 in fishing_config.BAIT). Idempotent -- on a
-- second run no rows still have a "cricket" key, so the UPDATE is a
-- no-op.
--
-- The pool itself is fixed in fishing_config.py so future beachcombs
-- can never re-introduce the orphan key.

UPDATE user_fishing
SET bait_inventory = jsonb_set(
        bait_inventory - 'cricket',
        '{worm}',
        to_jsonb(LEAST(
            500,
            COALESCE((bait_inventory->>'worm')::int, 0)
            + COALESCE((bait_inventory->>'cricket')::int, 0)
        ))
    )
WHERE jsonb_typeof(bait_inventory) = 'object'
  AND bait_inventory ? 'cricket';
