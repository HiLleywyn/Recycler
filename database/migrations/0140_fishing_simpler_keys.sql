-- Fishing keys: aggressive rename pass to drop the verbose
-- ``_bait``/``_pot``/``_fish``/``_crab``/``_can`` etc. suffixes so users
-- type ``,fish bait magic`` instead of ``,fish bait magic_lure`` and
-- ``,fish trap place steel`` instead of ``,fish trap place steel_pot``.
--
-- Backwards compatibility: NONE -- old keys are rewritten in place.
-- Nothing in the code accepts the old keys after this migration runs;
-- the catalog in fishing_config.py is the new source of truth.
--
-- Tables touched:
--   user_fishing.bait_inventory       (jsonb dict)
--   user_fishing.fish_inventory       (jsonb dict)
--   user_fishing.junk_inventory       (jsonb dict)
--   user_fishing.crab_trap_inventory  (jsonb dict)
--   user_fishing.placed_crab_traps    (jsonb array of {key, zone, placed_at})
--   user_fishing.equipped_bait        (text)
--   user_fishing.current_zone         (text)
--   user_fishing.biggest_fish         (text)
--   fishing_catches.fish_key          (text)
--   fishing_catches.junk_key          (text)
--   fishing_catches.zone              (text)
--   fishing_catches.bait_key          (text)
--
-- Helper function: pg_temp.rename_jsonb_keys(input, mapping) renames the
-- top-level keys of a JSONB dict. Used for every ``*_inventory`` column.
-- Defined in pg_temp so it's automatically dropped at session end and
-- never pollutes a follow-up migration.

CREATE OR REPLACE FUNCTION pg_temp.rename_jsonb_keys(
    input jsonb, mapping jsonb
) RETURNS jsonb
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT COALESCE(
        jsonb_object_agg(
            COALESCE(mapping->>key, key),
            value
        ),
        '{}'::jsonb
    )
    FROM jsonb_each(input);
$$;

-- ----------------------------------------------------------------------
-- Mapping payloads, kept inline so each UPDATE is auditable on its own.
-- ----------------------------------------------------------------------

-- Bait keys.
UPDATE user_fishing
   SET bait_inventory = pg_temp.rename_jsonb_keys(
       bait_inventory,
       '{
           "shrimp_bait": "shrimp",
           "minnow_bait": "minnow",
           "neon_bait":   "neon",
           "magic_lure":  "magic",
           "abyss_chum":  "chum"
       }'::jsonb
   )
 WHERE bait_inventory IS NOT NULL
   AND bait_inventory <> '{}'::jsonb;

-- Crab trap inventory keys.
UPDATE user_fishing
   SET crab_trap_inventory = pg_temp.rename_jsonb_keys(
       crab_trap_inventory,
       '{
           "wire_pot":    "wire",
           "oak_pot":     "oak",
           "steel_pot":   "steel",
           "abyssal_pot": "abyssal"
       }'::jsonb
   )
 WHERE crab_trap_inventory IS NOT NULL
   AND crab_trap_inventory <> '{}'::jsonb;

-- Junk keys.
UPDATE user_fishing
   SET junk_inventory = pg_temp.rename_jsonb_keys(
       junk_inventory,
       '{
           "tin_can":          "can",
           "shopping_cart":    "cart",
           "trash_bag":        "bag",
           "rubber_duck":      "duck",
           "old_map":          "map",
           "crypto_keychain":  "keychain",
           "broken_modem":     "modem",
           "vending_machine":  "vending",
           "anchor_charm":     "anchor",
           "sunglasses":       "shades",
           "sea_skull":        "skull",
           "old_dsd":          "dsd"
       }'::jsonb
   )
 WHERE junk_inventory IS NOT NULL
   AND junk_inventory <> '{}'::jsonb;

-- Fish keys (note: includes crab species, which live in the FISH catalog
-- but are gated min_rod_tier=99 so only traps roll them).
UPDATE user_fishing
   SET fish_inventory = pg_temp.rename_jsonb_keys(
       fish_inventory,
       '{
           "golden_fish":      "goldie",
           "discoin_fish":     "discoin",
           "sewer_eel":        "muckeel",
           "sewer_gator":      "gator",
           "arctic_char":      "char",
           "giant_squid":      "squid",
           "frost_serpent":    "serpent",
           "temple_guardian":  "guardian",
           "reef_phoenix":     "phoenix",
           "ancient_carp":     "ancient",
           "blue_crab":        "bluecrab",
           "mud_crab":         "mudcrab",
           "snow_crab":        "snowcrab",
           "coconut_crab":     "cococrab",
           "giant_crab":       "spidercrab",
           "king_crab":        "kingcrab",
           "void_crab":        "voidcrab"
       }'::jsonb
   )
 WHERE fish_inventory IS NOT NULL
   AND fish_inventory <> '{}'::jsonb;

-- placed_crab_traps is an array of {key, zone, placed_at}; rewrite each
-- element's key + zone fields. We rebuild the array with jsonb_agg over
-- jsonb_array_elements so every entry is normalised in one pass.
UPDATE user_fishing
   SET placed_crab_traps = COALESCE(
       (
           SELECT jsonb_agg(
               jsonb_set(
                   jsonb_set(
                       elem,
                       '{key}',
                       to_jsonb(CASE elem->>'key'
                           WHEN 'wire_pot'    THEN 'wire'
                           WHEN 'oak_pot'     THEN 'oak'
                           WHEN 'steel_pot'   THEN 'steel'
                           WHEN 'abyssal_pot' THEN 'abyssal'
                           ELSE elem->>'key'
                       END)
                   ),
                   '{zone}',
                   to_jsonb(CASE elem->>'zone'
                       WHEN 'discoin_dock'   THEN 'dock'
                       WHEN 'coral_reef'     THEN 'reef'
                       WHEN 'kelp_forest'    THEN 'kelp'
                       WHEN 'glacier_bay'    THEN 'glacier'
                       WHEN 'sunken_temple'  THEN 'temple'
                       ELSE elem->>'zone'
                   END)
               )
           )
           FROM jsonb_array_elements(placed_crab_traps) elem
       ),
       '[]'::jsonb
   )
 WHERE placed_crab_traps IS NOT NULL
   AND placed_crab_traps <> '[]'::jsonb;

-- TEXT columns: equipped_bait, current_zone, biggest_fish.
UPDATE user_fishing
   SET equipped_bait = CASE equipped_bait
       WHEN 'shrimp_bait' THEN 'shrimp'
       WHEN 'minnow_bait' THEN 'minnow'
       WHEN 'neon_bait'   THEN 'neon'
       WHEN 'magic_lure'  THEN 'magic'
       WHEN 'abyss_chum'  THEN 'chum'
       ELSE equipped_bait
   END
 WHERE equipped_bait IN ('shrimp_bait', 'minnow_bait', 'neon_bait', 'magic_lure', 'abyss_chum');

UPDATE user_fishing
   SET current_zone = CASE current_zone
       WHEN 'discoin_dock'  THEN 'dock'
       WHEN 'coral_reef'    THEN 'reef'
       WHEN 'kelp_forest'   THEN 'kelp'
       WHEN 'glacier_bay'   THEN 'glacier'
       WHEN 'sunken_temple' THEN 'temple'
       ELSE current_zone
   END
 WHERE current_zone IN ('discoin_dock', 'coral_reef', 'kelp_forest', 'glacier_bay', 'sunken_temple');

UPDATE user_fishing
   SET biggest_fish = CASE biggest_fish
       WHEN 'golden_fish'      THEN 'goldie'
       WHEN 'discoin_fish'     THEN 'discoin'
       WHEN 'sewer_eel'        THEN 'muckeel'
       WHEN 'sewer_gator'      THEN 'gator'
       WHEN 'arctic_char'      THEN 'char'
       WHEN 'giant_squid'      THEN 'squid'
       WHEN 'frost_serpent'    THEN 'serpent'
       WHEN 'temple_guardian'  THEN 'guardian'
       WHEN 'reef_phoenix'     THEN 'phoenix'
       WHEN 'ancient_carp'     THEN 'ancient'
       WHEN 'blue_crab'        THEN 'bluecrab'
       WHEN 'mud_crab'         THEN 'mudcrab'
       WHEN 'snow_crab'        THEN 'snowcrab'
       WHEN 'coconut_crab'     THEN 'cococrab'
       WHEN 'giant_crab'       THEN 'spidercrab'
       WHEN 'king_crab'        THEN 'kingcrab'
       WHEN 'void_crab'        THEN 'voidcrab'
       ELSE biggest_fish
   END
 WHERE biggest_fish IN (
       'golden_fish', 'discoin_fish', 'sewer_eel', 'sewer_gator',
       'arctic_char', 'giant_squid', 'frost_serpent', 'temple_guardian',
       'reef_phoenix', 'ancient_carp', 'blue_crab', 'mud_crab',
       'snow_crab', 'coconut_crab', 'giant_crab', 'king_crab', 'void_crab'
   );

-- ----------------------------------------------------------------------
-- fishing_catches: append-only log. Rewrite the four key/text columns so
-- ,fish history and the leaderboard render the renamed catalog correctly.
-- ----------------------------------------------------------------------

UPDATE fishing_catches
   SET fish_key = CASE fish_key
       WHEN 'golden_fish'      THEN 'goldie'
       WHEN 'discoin_fish'     THEN 'discoin'
       WHEN 'sewer_eel'        THEN 'muckeel'
       WHEN 'sewer_gator'      THEN 'gator'
       WHEN 'arctic_char'      THEN 'char'
       WHEN 'giant_squid'      THEN 'squid'
       WHEN 'frost_serpent'    THEN 'serpent'
       WHEN 'temple_guardian'  THEN 'guardian'
       WHEN 'reef_phoenix'     THEN 'phoenix'
       WHEN 'ancient_carp'     THEN 'ancient'
       WHEN 'blue_crab'        THEN 'bluecrab'
       WHEN 'mud_crab'         THEN 'mudcrab'
       WHEN 'snow_crab'        THEN 'snowcrab'
       WHEN 'coconut_crab'     THEN 'cococrab'
       WHEN 'giant_crab'       THEN 'spidercrab'
       WHEN 'king_crab'        THEN 'kingcrab'
       WHEN 'void_crab'        THEN 'voidcrab'
       ELSE fish_key
   END
 WHERE fish_key IN (
       'golden_fish', 'discoin_fish', 'sewer_eel', 'sewer_gator',
       'arctic_char', 'giant_squid', 'frost_serpent', 'temple_guardian',
       'reef_phoenix', 'ancient_carp', 'blue_crab', 'mud_crab',
       'snow_crab', 'coconut_crab', 'giant_crab', 'king_crab', 'void_crab'
   );

UPDATE fishing_catches
   SET junk_key = CASE junk_key
       WHEN 'tin_can'          THEN 'can'
       WHEN 'shopping_cart'    THEN 'cart'
       WHEN 'trash_bag'        THEN 'bag'
       WHEN 'rubber_duck'      THEN 'duck'
       WHEN 'old_map'          THEN 'map'
       WHEN 'crypto_keychain'  THEN 'keychain'
       WHEN 'broken_modem'     THEN 'modem'
       WHEN 'vending_machine'  THEN 'vending'
       WHEN 'anchor_charm'     THEN 'anchor'
       WHEN 'sunglasses'       THEN 'shades'
       WHEN 'sea_skull'        THEN 'skull'
       WHEN 'old_dsd'          THEN 'dsd'
       ELSE junk_key
   END
 WHERE junk_key IN (
       'tin_can', 'shopping_cart', 'trash_bag', 'rubber_duck', 'old_map',
       'crypto_keychain', 'broken_modem', 'vending_machine', 'anchor_charm',
       'sunglasses', 'sea_skull', 'old_dsd'
   );

UPDATE fishing_catches
   SET zone = CASE zone
       WHEN 'discoin_dock'  THEN 'dock'
       WHEN 'coral_reef'    THEN 'reef'
       WHEN 'kelp_forest'   THEN 'kelp'
       WHEN 'glacier_bay'   THEN 'glacier'
       WHEN 'sunken_temple' THEN 'temple'
       ELSE zone
   END
 WHERE zone IN ('discoin_dock', 'coral_reef', 'kelp_forest', 'glacier_bay', 'sunken_temple');

UPDATE fishing_catches
   SET bait_key = CASE bait_key
       WHEN 'shrimp_bait' THEN 'shrimp'
       WHEN 'minnow_bait' THEN 'minnow'
       WHEN 'neon_bait'   THEN 'neon'
       WHEN 'magic_lure'  THEN 'magic'
       WHEN 'abyss_chum'  THEN 'chum'
       ELSE bait_key
   END
 WHERE bait_key IN ('shrimp_bait', 'minnow_bait', 'neon_bait', 'magic_lure', 'abyss_chum');
