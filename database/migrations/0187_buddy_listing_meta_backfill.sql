-- 0187_buddy_listing_meta_backfill.sql
--
-- Backfill ``auction_listings.metadata`` for every active buddy
-- listing with the live ``cc_buddies`` row's level / rarity_tier /
-- name / gender / species / buddy_id. Pre-existing listings created
-- before the create_listing_by_token refresh fix landed had only
-- the token's mint-time metadata (often just buddy_id), so the AH
-- browse + inspect render fell through to "Tier 1" defaults on
-- every buddy regardless of actual rarity.
--
-- Idempotent: jsonb_build_object overwrites any previously-stamped
-- keys with the live values, so re-running this migration just
-- refreshes again. Active-only filter so closed / cancelled / sold
-- rows stay frozen at their settled-at snapshot.

UPDATE auction_listings al
   SET metadata = COALESCE(al.metadata, '{}'::jsonb)
                  || jsonb_build_object(
                       'buddy_id',    cb.id,
                       'name',        COALESCE(cb.name, ''),
                       'species',     COALESCE(cb.species, ''),
                       'level',       COALESCE(cb.level, 1),
                       'xp',          COALESCE(cb.xp, 0),
                       'rarity_tier', COALESCE(cb.rarity_tier, 1),
                       'gender',      UPPER(COALESCE(cb.gender, '')),
                       'wins',        COALESCE(cb.wins, 0),
                       'losses',      COALESCE(cb.losses, 0)
                  )
  FROM cc_buddies cb
 WHERE al.kind = 'buddy'
   AND al.status = 'active'
   AND (
       (al.metadata->>'buddy_id')::bigint = cb.id
       OR (
           al.token_id IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM item_instances ii
                WHERE ii.token_id = al.token_id
                  AND ii.source_table = 'cc_buddies'
                  AND ii.source_id = cb.id::text
           )
       )
   );
