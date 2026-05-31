"""crafting_config.py -- recipe catalog and tuning for the Forge minigame.

Mirrors fishing_config / farming_config / dungeon_config: pure data + helpers,
no Discord or DB imports. The crafting service (services/crafting.py) and cog
(cogs/crafting.py) read from here.

Token economy (defined in core/config.py, gated by Config.EARN_ONLY_TOKENS):
- INGOT  earn-only token, minted by ,craft make (mirrors SEED / LURE / COPPER)
- FORGE  network coin, oracle-priced; INGOT -> FORGE via burn-swap with slippage
- FGD    network stablecoin (Config.BUYABLE_WITH_USD), used to price recipe-input
         shop bundles and the FORGE_TICKET tax on bigger recipes

Inputs accepted by recipes:
- Fish keys from fishing_config.FISH (e.g. 'bass', 'kraken')
- Crop keys from farming_config.CROPS (e.g. 'wheat', 'pumpkin')
- Crop processed keys from farming_config.RECIPES (e.g. 'bread')
- Ore symbols from dungeon_config.ORE_SYMBOLS ('COPPER', 'SILVER', 'GOLD')
- Buddy-economy keys ('FREN' for stake-burn ingredient, 'BUD' tax)

Outputs route back into the source games via ,craft apply <item>:
- bait/<bait_key>     -> user_fishing.bait_inventory       (fishing)
- fert/<fert_key>     -> user_farming.fertilizer_inventory (farming)
- consum/<cons_key>   -> user_dungeon.consumables          (dungeon)
- buddy/<effect>      -> applied directly to active buddy  (buddies)
- cosmetic/<key>      -> users.cosmetics JSONB             (cosmetics)
                          ; ,inventory use <key> then grants the
                          linked role for items_config.duration_seconds

The output-route prefix is the FIRST token in the ``apply`` field; everything
after the slash is the target inventory key in the receiving system. Because
crafting only TOPS UP existing inventories, no integration-side changes are
required: a crafted bait drops into the fishing bait_inventory under the same
key the fishing shop uses, so equip/cast logic just works.
"""
from __future__ import annotations

from typing import Final


# ── Network / token symbols (mirror Config.TOKENS) ──────────────────────────

FORGE_NETWORK_SHORT: Final[str] = "fge"
FORGE_NETWORK_FULL:  Final[str] = "Forge Network"

FORGE_SYMBOL: Final[str] = "FORGE"
FGD_SYMBOL:   Final[str] = "FGD"
INGOT_SYMBOL: Final[str] = "INGOT"


# ── XP / level tuning ───────────────────────────────────────────────────────

# Same shape as fishing/farming: cap 50, polynomial curve so early levels are
# fast and late levels feel like an investment.
MAX_LEVEL:   Final[int]   = 50
XP_BASE:     Final[float] = 100.0
XP_GROWTH:   Final[float] = 1.18

# Soft cooldown between crafts so a script can't drain the LP pool in one
# tight loop. The DB-side clock lives on user_crafting.last_craft_at; the
# service compares EXTRACT(EPOCH FROM (NOW() - last_craft_at)) per the
# CLAUDE.md rule about never comparing Python now() to a Postgres timestamp.
CRAFT_COOLDOWN_SECONDS: Final[int] = 8

# INGOT -> FORGE burn-swap: same impact-based slippage as fishing/farming.
# The user-facing "slippage IS the fee" wording carries over verbatim.
GEAR_BURN_LP_REWARD_BPS: Final[int] = 100  # 1% to LP rewards on burn paths

# Daily INGOT-stake yield in FORGE (per INGOT staked). Mirrors farming's
# SEED-stake yield magnitude so a crafter who never burns can still drip
# FORGE out passively.
INGOT_STAKE_FORGE_PER_DAY: Final[float] = 0.01


# ── Rarity tiers (parity with fishing/farming) ──────────────────────────────

RARITIES: Final[tuple[str, ...]] = (
    "common", "uncommon", "rare", "epic", "legendary",
)


# ── Specialties ─────────────────────────────────────────────────────────────
# Crafting branches into six tracks. Each recipe declares a ``specialty``;
# a successful ,craft make bumps BOTH the aggregate crafting XP/level AND
# the matching specialty XP/level. The aggregate level still gates the
# recipe (min_level), specialties give parallel progress + flavor.
#
# Pick-2: each player picks up to 2 active specialties (stored on
# user_crafting.active_specialties via migration 0172). Recipes flagged
# ``requires_specialty: True`` only craft when that recipe's specialty is
# in the player's active set. In-specialty crafts also get a level-scaled
# INGOT mint bonus (+1% per specialty level) and full-rate XP. Off-
# specialty crafts (allowed for non-locked recipes) earn 50% XP.
#
# Selection limit + the bonus formula live as constants so the cog and
# the help text stay in lockstep.

SPECIALTIES: Final[tuple[str, ...]] = (
    "smithing", "alchemy", "cooking",
    "fletching", "tinkering", "enchanting",
)

# Cap on how many specialties one player can hold at once.
ACTIVE_SPECIALTY_CAP: Final[int] = 2

# In-specialty INGOT bonus per specialty level. Caps implicitly at MAX_LEVEL.
SPECIALTY_INGOT_BONUS_PER_LEVEL: Final[float] = 0.01   # +1% per Lv

# XP multiplier when crafting a (non-locked) recipe that's outside any of
# the player's active specialties. Lower = stronger nudge to specialise.
# Generalists STILL level up the aggregate + the recipe's specialty -- just
# 10x slower than an in-specialty crafter. Specialty-locked recipes (the
# ``requires_specialty`` flag) remain uncraftable until the player picks the
# matching branch; this multiplier only governs the general-tier recipes.
OFF_SPECIALTY_XP_MULT: Final[float] = 0.10

SPECIALTY_META: Final[dict[str, dict]] = {
    "smithing":  {
        "name": "Smithing",  "emoji": "\U0001F528",   # hammer
        "blurb": "Metal, oil, and ore-routed gear. Heavy-ore recipes "
                 "live here.",
    },
    "alchemy":   {
        "name": "Alchemy",   "emoji": "\U0001F9EA",   # test tube
        "blurb": "Potions, elixirs, vials, growth brews. Healing "
                 "and stat-fix consumables.",
    },
    "cooking":   {
        "name": "Cooking",   "emoji": "\U0001F35E",   # bread
        "blurb": "Treats, tonics, and edible preserves. Buddy "
                 "buffs come from here.",
    },
    "fletching": {
        "name": "Fletching", "emoji": "\U0001FAB1",   # worm
        "blurb": "Bait, lures, and chum for the rod.",
    },
    "tinkering": {
        "name": "Tinkering", "emoji": "\U0001F9F0",   # toolbox
        "blurb": "Charms, kits, gadgets, and toys.",
    },
    "enchanting": {
        "name": "Enchanting", "emoji": "\U00002728",   # sparkles
        "blurb": "Runes, fortune, oracles. Rarity rolls and "
                 "low-probability buffs.",
    },
}


# Material-source map: tells the recipe book where each input comes
# from so a player browsing recipes knows where to grind. Keyed by the
# input prefix (``fish/`` / ``crop/`` / ``ore/`` / ``token/`` /
# ``recipe/``); the cog uses this to label the row.
MATERIAL_SOURCES: Final[dict[str, str]] = {
    "fish":   "Fishing  -  ,fish (sells / hauls)",
    "crop":   "Farming  -  ,farm harvest -> crop_inventory",
    "recipe": "Farming  -  ,farm process recipes",
    "ore":    "Dungeon  -  ,delve mine (COPPER / SILVER / GOLD)",
    "token":  "Wallet   -  earn-only token (FREN, etc.)",
}


def material_source(input_key: str) -> str:
    """Resolve a recipe input key like ``fish/bass`` to a one-line
    "where to get it" hint for the recipe book.
    """
    prefix = str(input_key or "").split("/", 1)[0].lower()
    return MATERIAL_SOURCES.get(prefix, "Wallet")


def in_specialty(
    recipe_specialty: str, active: list[str] | tuple[str, ...] | None,
) -> bool:
    """True if ``recipe_specialty`` is in the player's active set.

    Empty or None active set means generalist -- never in-specialty,
    always pays the off-specialty XP penalty for non-locked recipes.
    """
    if not active:
        return False
    spec = str(recipe_specialty or "").lower()
    if not spec:
        return False
    return spec in {str(s or "").lower() for s in active}


def specialty_meta(key: str) -> dict:
    """Return the catalog entry for ``key`` or a Smithing-shaped fallback."""
    return SPECIALTY_META.get(
        str(key or "").lower(),
        {"name": str(key or "").title(), "emoji": "", "blurb": ""},
    )

# XP per craft, scales with rarity tier.
RARITY_XP: Final[dict[str, int]] = {
    "common":    8,
    "uncommon":  20,
    "rare":      50,
    "epic":      120,
    "legendary": 320,
}

# INGOT minted per craft, scales with rarity. Whole INGOT (raw conversion
# happens in services/crafting.py via to_raw).
RARITY_INGOT_PAYOUT: Final[dict[str, tuple[float, float]]] = {
    "common":    (4.0,    12.0),
    "uncommon":  (12.0,   30.0),
    "rare":      (30.0,   80.0),
    "epic":      (80.0,   220.0),
    "legendary": (220.0,  600.0),
}


# ── Recipes (CRAFT_ITEMS) ───────────────────────────────────────────────────
#
# Each recipe is keyed by a unique craft_key and has:
#   name, emoji, rarity            -- display + payout tier
#   inputs                          -- {ingredient_key: count} consumed per craft
#       ingredient_key formats:
#         'fish/<fish_key>'    -> from fishing_config.FISH (e.g. 'fish/bass')
#         'crop/<crop_key>'    -> from farming_config.CROPS (e.g. 'crop/wheat')
#         'recipe/<key>'       -> processed crop from farming_config.RECIPES
#         'ore/<SYMBOL>'       -> COPPER / SILVER / GOLD from dungeon
#         'token/<SYMBOL>'     -> wallet-held earn-only token (e.g. 'token/FREN')
#   fgd_cost                        -- FGD burned as crafting fee (stable, USD-pegged)
#   min_level                       -- crafting_level required
#   apply                           -- where the crafted item routes on ,craft apply
#       formats:
#         'bait/<bait_key>'    -> tops up user_fishing.bait_inventory
#         'fert/<fert_key>'    -> tops up user_farming.fertilizer_inventory
#         'consum/<cons_key>'  -> tops up user_dungeon.consumables
#         'buddy/<effect>'     -> direct effect on active buddy (no inventory)
#       The post-slash key MUST already exist in the receiving catalog (BAIT,
#       FERTILIZERS, CONSUMABLES) so equip/cast logic doesn't need to know
#       about crafting at all.
#   max_stack                       -- per-user inventory cap on the crafted item
#   blurb                           -- one-line flavor

CRAFT_ITEMS: Final[dict[str, dict]] = {
    # ── Fishing-bound outputs (route to bait_inventory) ────────────────────
    "worm_bundle": {
        "name": "Worm Bundle", "emoji": "\U0001FAB1", "rarity": "common",
        "specialty": "fletching",
        "inputs": {"crop/wheat": 2, "ore/COPPER": 5},
        "fgd_cost": 1.0, "min_level": 1,
        "apply": "bait/worm", "max_stack": 500,
        "blurb": "Two fistfuls of compost-worms wrapped in straw. Cheap chum.",
    },
    "shrimp_chum": {
        "name": "Shrimp Chum", "emoji": "\U0001F990", "rarity": "common",
        "specialty": "fletching",
        "inputs": {"fish/sardine": 3, "fish/anchovy": 3, "crop/corn": 1},
        "fgd_cost": 3.0, "min_level": 2,
        "apply": "bait/shrimp", "max_stack": 250,
        "blurb": "Saltwater pulp, ground until it stops complaining.",
    },
    "neon_lure_kit": {
        "name": "Neon Lure Kit", "emoji": "\U0001F4A1", "rarity": "uncommon",
        "specialty": "tinkering",
        "inputs": {"fish/perch": 2, "ore/SILVER": 3, "ore/COPPER": 8},
        "fgd_cost": 12.0, "min_level": 5,
        "apply": "bait/neon", "max_stack": 100,
        "blurb": "Wired and dipped. Glows long enough to matter.",
    },
    "magic_lure": {
        "name": "Magic Lure", "emoji": "\U00002728", "rarity": "rare",
        "specialty": "fletching",
        "inputs": {"fish/eel": 1, "ore/GOLD": 2, "ore/SILVER": 4, "crop/sunflower": 2},
        "fgd_cost": 75.0, "min_level": 12,
        "apply": "bait/magic", "max_stack": 50,
        "blurb": "Smells faintly of mythology and warm copper.",
    },
    "abyssal_chum": {
        "name": "Abyssal Chum", "emoji": "\U0001F9EC", "rarity": "epic",
        "specialty": "fletching",
        "inputs": {"fish/octopus": 1, "fish/shark": 1, "ore/GOLD": 8, "token/FREN": 50},
        "fgd_cost": 400.0, "min_level": 22,
        "apply": "bait/chum", "max_stack": 25,
        "blurb": "Wakes up the things that sleep at the bottom.",
    },

    # ── Farming-bound outputs (route to fertilizer_inventory) ──────────────
    "compost_brick": {
        "name": "Compost Brick", "emoji": "\U0001F33F", "rarity": "common",
        "specialty": "alchemy",
        "inputs": {"crop/wheat": 4, "fish/minnow": 4},
        "fgd_cost": 2.0, "min_level": 1,
        "apply": "fert/compost", "max_stack": 100,
        "blurb": "Pressed cube of crop trim and fish-meal. Mostly nitrogen.",
    },
    "manure_cake": {
        "name": "Manure Cake", "emoji": "\U0001F4A9", "rarity": "common",
        "specialty": "alchemy",
        "inputs": {"crop/carrot": 3, "crop/potato": 2, "ore/COPPER": 4},
        "fgd_cost": 6.0, "min_level": 4,
        "apply": "fert/manure", "max_stack": 100,
        "blurb": "Aged on purpose. Smells like profit.",
    },
    "bonemeal_grind": {
        "name": "Bonemeal Grind", "emoji": "\U0001F9B4", "rarity": "uncommon",
        "specialty": "smithing",
        "inputs": {"fish/swordfish": 1, "fish/lobster": 2, "ore/SILVER": 4},
        "fgd_cost": 25.0, "min_level": 8,
        "apply": "fert/bonemeal", "max_stack": 75,
        "blurb": "Skeletons go in, fertilizer comes out. Don't ask.",
    },
    "guano_brick": {
        "name": "Guano Brick", "emoji": "\U0001F423", "rarity": "rare",
        "specialty": "alchemy",
        "inputs": {"fish/eel": 2, "ore/GOLD": 1, "ore/SILVER": 6},
        "fgd_cost": 90.0, "min_level": 14,
        "apply": "fert/guano", "max_stack": 75,
        "blurb": "Imported. Bat-certified. Faintly luminous.",
    },
    "miracle_growth_vial": {
        "name": "Miracle Growth Vial", "emoji": "\U00002728", "rarity": "epic",
        "specialty": "alchemy",
        "inputs": {"fish/marlin": 1, "fish/tuna": 1, "ore/GOLD": 6, "token/FREN": 100},
        "fgd_cost": 600.0, "min_level": 25,
        "apply": "fert/miracle_growth", "max_stack": 50,
        "blurb": "Lab-grown. Nobody asks how. Crops twitch when poured.",
    },

    # ── Dungeon-bound outputs (route to user_dungeon.consumables) ──────────
    "minor_potion_brew": {
        "name": "Minor Potion Brew", "emoji": "\U0001F9EA", "rarity": "common",
        "specialty": "alchemy",
        "inputs": {"crop/tomato": 2, "fish/minnow": 2, "ore/COPPER": 2},
        "fgd_cost": 2.0, "min_level": 1,
        "apply": "consum/potion_minor", "max_stack": 99,
        "blurb": "Heals 25% of max HP. Tastes like tomato bisque.",
    },
    "major_potion_brew": {
        "name": "Major Potion Brew", "emoji": "\U0001F9EA", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"fish/bass": 1, "crop/pumpkin": 1, "ore/SILVER": 2},
        "fgd_cost": 10.0, "min_level": 6,
        "apply": "consum/potion_major", "max_stack": 99,
        "blurb": "Heals 60% of max HP. Tastes like fall.",
    },
    "elixir_distill": {
        "name": "Elixir Distill", "emoji": "\U0001F9EA", "rarity": "rare",
        "specialty": "alchemy",
        "inputs": {"fish/salmon": 1, "crop/sunflower": 2, "ore/GOLD": 1, "ore/SILVER": 4},
        "fgd_cost": 60.0, "min_level": 15,
        "apply": "consum/elixir", "max_stack": 50,
        "blurb": "Full heal. The bottle hums.",
    },
    "phoenix_down_pluck": {
        "name": "Phoenix Down Pluck", "emoji": "\U0001F525", "rarity": "epic",
        "specialty": "alchemy",
        "inputs": {"fish/leviathan": 1, "ore/GOLD": 6, "token/FREN": 80},
        "fgd_cost": 300.0, "min_level": 22,
        "apply": "consum/phoenix_down", "max_stack": 25,
        "blurb": "Auto-revive at 50% HP on KO. Smells like burnt feathers.",
    },
    "tame_charm_carve": {
        "name": "Taming Charm Carve", "emoji": "\U0001F9FF", "rarity": "uncommon",
        "specialty": "tinkering",
        "inputs": {"fish/carp": 2, "crop/eggplant": 1, "ore/SILVER": 2},
        "fgd_cost": 12.0, "min_level": 5,
        "apply": "consum/tame_charm", "max_stack": 50,
        "blurb": "Whittled bone. Mob-shaped. +20% next capture.",
    },
    "diamond_pickaxe_oil": {
        "name": "Diamond Pickaxe Oil", "emoji": "\U0001F48E", "rarity": "rare",
        "specialty": "smithing",
        "inputs": {"fish/lobster": 2, "ore/GOLD": 3, "ore/SILVER": 8},
        "fgd_cost": 70.0, "min_level": 13,
        "apply": "consum/diamond_pickaxe", "max_stack": 50,
        "blurb": "Cracks the rock open. +150% ore on the next mine.",
    },

    # ── Buddy-bound outputs (apply directly to active buddy) ───────────────
    # These are one-shot consumables, NOT inventory items. Applying spends
    # the craft from crafted_inventory and routes through services/crafting.py
    # which calls into services/buddy_lifecycle helpers to bump the active
    # buddy's stats / mood. The buddy itself never grows a new inventory
    # column -- the effect is applied immediately on ,craft apply <key>.
    "buddy_treat": {
        "name": "Buddy Treat", "emoji": "\U0001F36A", "rarity": "common",
        "specialty": "cooking",
        "inputs": {"crop/wheat": 3, "crop/carrot": 2, "fish/minnow": 1},
        "fgd_cost": 2.0, "min_level": 1,
        "apply": "buddy/feed", "max_stack": 200,
        # Effect: +35 hunger, +5 happiness, +15 energy on active buddy
        # (more generous than the free ,buddy feed, with no cooldown).
        "blurb": "A treat. Crunchy on the outside, soft on the soul.",
    },
    "buddy_toy": {
        "name": "Buddy Toy", "emoji": "\U0001F9F8", "rarity": "common",
        "specialty": "tinkering",
        "inputs": {"crop/corn": 4, "ore/COPPER": 3},
        "fgd_cost": 4.0, "min_level": 2,
        "apply": "buddy/play", "max_stack": 100,
        # Effect: +25 happiness on active buddy (no cooldown).
        "blurb": "Squeaks. The buddy figures out the squeak.",
    },
    "buddy_tonic": {
        "name": "Buddy Tonic", "emoji": "\U0001F9EA", "rarity": "uncommon",
        "specialty": "cooking",
        "inputs": {"fish/trout": 1, "crop/tomato": 2, "ore/SILVER": 1},
        "fgd_cost": 15.0, "min_level": 6,
        "apply": "buddy/restore", "max_stack": 50,
        # Effect: full mood reset (hunger/happiness/energy all -> 100).
        "blurb": "Restores hunger / happiness / energy to full. Smells minty.",
    },
    "training_brew": {
        "name": "Training Brew", "emoji": "\U0001F378", "rarity": "rare",
        "specialty": "alchemy",
        "inputs": {"fish/pike": 1, "crop/pumpkin": 1, "ore/GOLD": 1, "ore/SILVER": 4},
        "fgd_cost": 75.0, "min_level": 14,
        "apply": "buddy/xp", "max_stack": 25,
        # Effect: grants 500 XP to the active buddy (one level for most).
        "blurb": "+500 XP for the active buddy. Tastes like effort.",
    },
    "rarity_reroll_potion": {
        "name": "Rarity Reroll Potion", "emoji": "\U0001F52E", "rarity": "legendary",
        "specialty": "alchemy", "requires_specialty": True,
        "inputs": {"fish/kraken": 1, "ore/GOLD": 12, "token/FREN": 200},
        "fgd_cost": 2500.0, "min_level": 35,
        "apply": "buddy/reroll_rarity", "max_stack": 5,
        # Effect: rerolls active buddy's rarity tier on the standard table.
        "blurb": "Rerolls the active buddy's rarity tier. Practically forbidden.",
    },

    # ────────────────────────────────────────────────────────────────────
    # Specialty-locked recipes ("requires_specialty": True). Only craftable
    # when that recipe's specialty is in the player's active set
    # (,craft specialize <key>). 3 per specialty -- one low/mid/late.
    # ────────────────────────────────────────────────────────────────────

    # ── Smithing (locked) ──────────────────────────────────────────────
    "iron_pickaxe_oil": {
        "name": "Iron Pickaxe Oil", "emoji": "\U0001F6E0", "rarity": "uncommon",
        "specialty": "smithing", "requires_specialty": True,
        "inputs": {"ore/COPPER": 10, "ore/SILVER": 2},
        "fgd_cost": 8.0, "min_level": 4,
        "apply": "consum/iron_pickaxe", "max_stack": 50,
        "blurb": "+50% ore on the next mine. Smells industrial.",
    },
    "silver_chain_links": {
        "name": "Silver Chain Links", "emoji": "\U0001F517", "rarity": "rare",
        "specialty": "smithing", "requires_specialty": True,
        "inputs": {"ore/SILVER": 12, "ore/COPPER": 20},
        "fgd_cost": 55.0, "min_level": 11,
        "apply": "consum/silver_chain", "max_stack": 40,
        "blurb": "Restraint cordage. Tames the next wild capture cleanly.",
    },
    "adamant_plate_blank": {
        "name": "Adamant Plate Blank", "emoji": "\U0001F6E1", "rarity": "legendary",
        "specialty": "smithing", "requires_specialty": True,
        "inputs": {"ore/GOLD": 18, "ore/SILVER": 30, "fish/leviathan": 1},
        "fgd_cost": 1800.0, "min_level": 30,
        "apply": "consum/adamant_plate", "max_stack": 5,
        "blurb": "Halves all incoming damage on the next delve battle.",
    },

    # ── Alchemy (locked) ───────────────────────────────────────────────
    "antidote_brew": {
        "name": "Antidote Brew", "emoji": "\U0001F33F", "rarity": "common",
        "specialty": "alchemy", "requires_specialty": True,
        "inputs": {"crop/tomato": 4, "crop/eggplant": 1, "ore/COPPER": 1},
        "fgd_cost": 3.0, "min_level": 3,
        "apply": "consum/antidote", "max_stack": 99,
        "blurb": "Cures poison + most debuffs on the next mob hit.",
    },
    "regen_draught": {
        "name": "Regen Draught", "emoji": "\U0001F9EA", "rarity": "rare",
        "specialty": "alchemy", "requires_specialty": True,
        "inputs": {"fish/salmon": 2, "crop/sunflower": 3, "ore/SILVER": 6},
        "fgd_cost": 95.0, "min_level": 16,
        "apply": "consum/regen_draught", "max_stack": 50,
        "blurb": "+10% HP per round for the rest of the run.",
    },
    "philosophers_phial": {
        "name": "Philosopher's Phial", "emoji": "\U0001F9EA", "rarity": "legendary",
        "specialty": "alchemy", "requires_specialty": True,
        "inputs": {"fish/kraken": 1, "ore/GOLD": 20, "crop/sunflower": 5},
        "fgd_cost": 3500.0, "min_level": 38,
        "apply": "buddy/full_revive", "max_stack": 3,
        "blurb": "Full mood + HP reset on every owned buddy. Once-a-life.",
    },

    # ── Cooking (locked) ───────────────────────────────────────────────
    "buddy_feast": {
        "name": "Buddy Feast", "emoji": "\U0001F35B", "rarity": "common",
        "specialty": "cooking", "requires_specialty": True,
        "inputs": {"crop/wheat": 5, "crop/carrot": 3, "fish/sardine": 2},
        "fgd_cost": 4.0, "min_level": 2,
        "apply": "buddy/feast", "max_stack": 100,
        # Effect: +50 hunger, +10 happiness, +20 energy on every owned buddy.
        "blurb": "Communal meal. Whole shelter eats. Hunger 0 -> 50 across the board.",
    },
    "harvest_pie": {
        "name": "Harvest Pie", "emoji": "\U0001F967", "rarity": "rare",
        "specialty": "cooking", "requires_specialty": True,
        "inputs": {"crop/wheat": 6, "crop/pumpkin": 2, "crop/sunflower": 1},
        "fgd_cost": 80.0, "min_level": 14,
        "apply": "buddy/xp_big", "max_stack": 25,
        # Effect: 1500 XP to the active buddy, ~3 levels at low end.
        "blurb": "+1500 XP for the active buddy. Smells like an autumn fair.",
    },
    "ambrosia": {
        "name": "Ambrosia", "emoji": "\U0001F37D", "rarity": "legendary",
        "specialty": "cooking", "requires_specialty": True,
        "inputs": {"fish/kraken": 1, "fish/leviathan": 1, "crop/sunflower": 8, "ore/GOLD": 5},
        "fgd_cost": 4200.0, "min_level": 40,
        "apply": "buddy/permanent_mood_resist", "max_stack": 1,
        # Effect: permanent +25% mood-decay resist on the active buddy.
        "blurb": "Food of the gods. Permanently slows mood decay on one buddy.",
    },

    # ── Fletching (locked) ─────────────────────────────────────────────
    "silver_lure": {
        "name": "Silver Lure", "emoji": "\U0001FA99", "rarity": "uncommon",
        "specialty": "fletching", "requires_specialty": True,
        "inputs": {"fish/perch": 4, "ore/SILVER": 5, "ore/COPPER": 6},
        "fgd_cost": 18.0, "min_level": 6,
        "apply": "bait/silver", "max_stack": 100,
        "blurb": "Polished and reflective. Pulls rare-tier hooks more often.",
    },
    "siren_call_lure": {
        "name": "Siren Call", "emoji": "\U0001F3B6", "rarity": "rare",
        "specialty": "fletching", "requires_specialty": True,
        "inputs": {"fish/eel": 2, "ore/GOLD": 1, "fish/octopus": 1},
        "fgd_cost": 110.0, "min_level": 18,
        "apply": "bait/siren", "max_stack": 40,
        "blurb": "Hums underwater. Doubles the next legendary-roll attempt.",
    },
    "worldsnake_lure": {
        "name": "Worldsnake Lure", "emoji": "\U0001F40D", "rarity": "legendary",
        "specialty": "fletching", "requires_specialty": True,
        "inputs": {"fish/leviathan": 1, "ore/GOLD": 25, "token/FREN": 300},
        "fgd_cost": 5500.0, "min_level": 42,
        "apply": "bait/worldsnake", "max_stack": 3,
        "blurb": "Forged from a coil scale. Forces the Ouroboros zone roll.",
    },

    # ── Tinkering (locked) ─────────────────────────────────────────────
    "pocket_compass": {
        "name": "Pocket Compass", "emoji": "\U0001F9ED", "rarity": "common",
        "specialty": "tinkering", "requires_specialty": True,
        "inputs": {"ore/COPPER": 8, "ore/SILVER": 1},
        "fgd_cost": 5.0, "min_level": 3,
        "apply": "consum/pocket_compass", "max_stack": 50,
        "blurb": "Skips the next empty room on a delve. Always points down.",
    },
    "weighted_dice": {
        "name": "Weighted Dice", "emoji": "\U0001F3B2", "rarity": "rare",
        "specialty": "tinkering", "requires_specialty": True,
        "inputs": {"ore/SILVER": 8, "ore/GOLD": 1, "crop/eggplant": 2},
        "fgd_cost": 70.0, "min_level": 12,
        "apply": "consum/weighted_dice", "max_stack": 30,
        "blurb": "+15% capture chance on the next wild battle. Very illegal.",
    },
    "omni_kit": {
        "name": "Omni-Tool Kit", "emoji": "\U0001F9F0", "rarity": "legendary",
        "specialty": "tinkering", "requires_specialty": True,
        "inputs": {"ore/GOLD": 15, "fish/marlin": 1, "fish/swordfish": 1, "token/FREN": 150},
        "fgd_cost": 3200.0, "min_level": 36,
        "apply": "consum/omni_kit", "max_stack": 3,
        "blurb": "Auto-applies the best ore / bait / charm boost active in your bag.",
    },

    # ── Enchanting (locked, brand new specialty) ───────────────────────
    "minor_rune": {
        "name": "Minor Rune", "emoji": "\U0001F523", "rarity": "common",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"crop/tomato": 2, "ore/COPPER": 4},
        "fgd_cost": 4.0, "min_level": 2,
        "apply": "consum/minor_rune", "max_stack": 99,
        "blurb": "+5% next-roll luck. Cheap, weak, ubiquitous.",
    },
    "fortune_charm": {
        "name": "Fortune Charm", "emoji": "\U0001F340", "rarity": "uncommon",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"crop/sunflower": 3, "ore/SILVER": 4, "fish/perch": 1},
        "fgd_cost": 22.0, "min_level": 7,
        "apply": "consum/fortune_charm", "max_stack": 50,
        "blurb": "+10% rare-tier chance on the next 5 rolls. Smells faintly of clover.",
    },
    "oracle_lens": {
        "name": "Oracle Lens", "emoji": "\U0001F52E", "rarity": "rare",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"fish/octopus": 1, "ore/GOLD": 2, "ore/SILVER": 8},
        "fgd_cost": 140.0, "min_level": 17,
        "apply": "consum/oracle_lens", "max_stack": 25,
        "blurb": "Peeks the next gamble outcome before you commit.",
    },
    "temporal_seal": {
        "name": "Temporal Seal", "emoji": "\U0000231B", "rarity": "epic",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"fish/marlin": 1, "ore/GOLD": 8, "token/FREN": 100},
        "fgd_cost": 800.0, "min_level": 26,
        "apply": "consum/temporal_seal", "max_stack": 10,
        "blurb": "Skips a single cooldown -- daily, work, or any minigame action.",
    },
    "starforged_relic": {
        "name": "Starforged Relic", "emoji": "\U00002728", "rarity": "legendary",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"fish/kraken": 1, "ore/GOLD": 25, "token/FREN": 250, "crop/sunflower": 6},
        "fgd_cost": 6000.0, "min_level": 44,
        "apply": "buddy/permanent_xp_mult", "max_stack": 1,
        "blurb": "Permanent +25% chat-XP gain on the active buddy. Once per buddy.",
    },

    # ────────────────────────────────────────────────────────────────────
    # General-tier additions. No specialty required, available to anyone.
    # ────────────────────────────────────────────────────────────────────

    "compost_starter": {
        "name": "Compost Starter", "emoji": "\U0001F33E", "rarity": "common",
        "specialty": "alchemy",
        "inputs": {"crop/wheat": 1, "fish/anchovy": 1},
        "fgd_cost": 0.5, "min_level": 1,
        "apply": "fert/compost", "max_stack": 100,
        "blurb": "Cheaper compost. The kindergarten of fertilizer.",
    },
    "worm_jar": {
        "name": "Worm Jar", "emoji": "\U0001FAB1", "rarity": "common",
        "specialty": "fletching",
        "inputs": {"crop/carrot": 1, "fish/minnow": 1},
        "fgd_cost": 0.5, "min_level": 1,
        "apply": "bait/worm", "max_stack": 500,
        "blurb": "Two worms in a jar. Surprisingly effective.",
    },
    "mineral_water": {
        "name": "Mineral Water", "emoji": "\U0001F4A7", "rarity": "common",
        "specialty": "alchemy",
        "inputs": {"ore/COPPER": 2, "crop/tomato": 1},
        "fgd_cost": 1.0, "min_level": 1,
        "apply": "consum/potion_minor", "max_stack": 99,
        "blurb": "Lightly mineralised. Heals 25%, tastes vaguely of pennies.",
    },
    "trail_mix": {
        "name": "Trail Mix", "emoji": "\U0001F95C", "rarity": "common",
        "specialty": "cooking",
        "inputs": {"crop/wheat": 2, "crop/sunflower": 1},
        "fgd_cost": 1.0, "min_level": 1,
        "apply": "buddy/feed", "max_stack": 200,
        "blurb": "Nuts, seeds, dried fruit. Cheap pet snack.",
    },
    "tinker_clip": {
        "name": "Tinker Clip", "emoji": "\U0001F4CC", "rarity": "common",
        "specialty": "tinkering",
        "inputs": {"ore/COPPER": 3, "crop/wheat": 1},
        "fgd_cost": 1.0, "min_level": 1,
        "apply": "consum/tame_charm", "max_stack": 50,
        "blurb": "Spring clip. +10% on the next capture, no question asked.",
    },

    # ────────────────────────────────────────────────────────────────────
    # Cheeses. All cooking-track. Two general-tier (anyone can craft, off-
    # spec gets the 10% XP rate) and three specialty-locked for dedicated
    # Cooks. Outputs route through existing buddy effects so they land in
    # the active buddy without any integration-side changes.
    # ────────────────────────────────────────────────────────────────────
    "curd_wheel": {
        "name": "Curd Wheel", "emoji": "\U0001F9C0", "rarity": "common",
        "specialty": "cooking",
        "inputs": {"crop/wheat": 3, "crop/corn": 1},
        "fgd_cost": 2.0, "min_level": 1,
        "apply": "buddy/feed", "max_stack": 200,
        "blurb": "Fresh, mild, faintly squeaky. The kindergarten of cheese.",
    },
    "sharp_cheddar": {
        "name": "Sharp Cheddar", "emoji": "\U0001F9C0", "rarity": "uncommon",
        "specialty": "cooking",
        "inputs": {"crop/wheat": 4, "crop/corn": 2, "ore/SILVER": 1},
        "fgd_cost": 14.0, "min_level": 5,
        "apply": "buddy/feed", "max_stack": 100,
        "blurb": "Aged in a cave nobody talks about. Bites back.",
    },
    "smoked_gouda": {
        "name": "Smoked Gouda", "emoji": "\U0001F9C0", "rarity": "rare",
        "specialty": "cooking", "requires_specialty": True,
        "inputs": {"crop/wheat": 5, "crop/carrot": 3, "ore/SILVER": 2, "fish/trout": 1},
        "fgd_cost": 70.0, "min_level": 13,
        # Effect: +50 hunger, +10 happiness, +20 energy across every owned
        # buddy (same as buddy_feast). Enough fat to feed the shelter.
        "apply": "buddy/feast", "max_stack": 50,
        "blurb": "Hardwood-smoked. Chewy crust, melt-in-the-mouth heart.",
    },
    "bleu_cheese": {
        "name": "Bleu Cheese", "emoji": "\U0001F9C0", "rarity": "epic",
        "specialty": "cooking", "requires_specialty": True,
        "inputs": {"crop/wheat": 4, "fish/eel": 1, "ore/SILVER": 4,
                   "ore/GOLD": 1, "fish/octopus": 1},
        "fgd_cost": 380.0, "min_level": 23,
        # Effect: full mood reset on the active buddy. Adventurous palates
        # only -- buddies don't get a vote.
        "apply": "buddy/restore", "max_stack": 25,
        "blurb": "Veined, pungent, faintly luminous. Buddies pretend to enjoy it.",
    },
    "truffle_brie": {
        "name": "Truffle Brie", "emoji": "\U0001F9C0", "rarity": "legendary",
        "specialty": "cooking", "requires_specialty": True,
        "inputs": {"fish/marlin": 1, "crop/sunflower": 4,
                   "ore/GOLD": 6, "token/FREN": 150},
        "fgd_cost": 4500.0, "min_level": 40,
        # Effect: re-uses buddy/feast for the whole-shelter mood lift; the
        # legendary mint range + INGOT bonus make this the cooking
        # endgame loop, not the apply effect itself.
        "apply": "buddy/feast", "max_stack": 5,
        "blurb": "Soft-ripened, truffle-laced. The whole shelter eats and remembers.",
    },

    # ── Archer-bound (route to user_dungeon.consumables ammo / scrolls) ────
    "arrow_bundle_craft": {
        "name": "Arrow Bundle", "emoji": "\U0001F3F9", "rarity": "common",
        "specialty": "fletching",
        "inputs": {"crop/wheat": 2, "ore/COPPER": 4},
        "fgd_cost": 1.0, "min_level": 1,
        "apply": "consum/arrow_bundle", "max_stack": 999,
        "blurb": "20 fletched arrows. Bow-class ammo. Bow draws 1 per shot.",
    },
    "broadhead_bundle_craft": {
        "name": "Broadhead Bundle", "emoji": "\U0001F3F9", "rarity": "uncommon",
        "specialty": "fletching",
        "inputs": {"crop/wheat": 2, "ore/SILVER": 3, "ore/COPPER": 6},
        "fgd_cost": 12.0, "min_level": 6,
        "apply": "consum/broadhead_bundle", "max_stack": 500,
        "blurb": "15 broadheads. +25% per-shot damage. Wider wound channel.",
    },
    "bolt_bundle_craft": {
        "name": "Bolt Bundle", "emoji": "\U0001F3F9", "rarity": "common",
        "specialty": "smithing",
        "inputs": {"ore/COPPER": 6, "ore/SILVER": 1},
        "fgd_cost": 1.5, "min_level": 1,
        "apply": "consum/bolt_bundle", "max_stack": 999,
        "blurb": "20 forged bolts. Crossbow-class ammo. Crossbow draws 1 per shot.",
    },
    "piercing_bolts_craft": {
        "name": "Piercing Bolts", "emoji": "\U0001F3F9", "rarity": "uncommon",
        "specialty": "smithing",
        "inputs": {"ore/SILVER": 3, "ore/GOLD": 1, "ore/COPPER": 4},
        "fgd_cost": 14.0, "min_level": 7,
        "apply": "consum/piercing_bolts", "max_stack": 500,
        "blurb": "15 hardened-tip bolts. +30% per-shot damage. Punches plate.",
    },
    "scroll_volley_inscribe": {
        "name": "Scroll of Volley", "emoji": "\U0001F3F9", "rarity": "rare",
        "specialty": "enchanting",
        "inputs": {"ore/SILVER": 4, "ore/GOLD": 2, "fish/perch": 1},
        "fgd_cost": 60.0, "min_level": 15,
        "apply": "consum/scroll_volley", "max_stack": 50,
        "blurb": "Charges your next basic ranged shot to fire 3 arrows. Burns 3 ammo.",
    },
    "scroll_mark_target_inscribe": {
        "name": "Scroll of Mark Target", "emoji": "\U0001F3AF", "rarity": "rare",
        "specialty": "enchanting",
        "inputs": {"ore/SILVER": 6, "fish/squid": 1, "crop/sunflower": 1},
        "fgd_cost": 45.0, "min_level": 12,
        "apply": "consum/scroll_mark_target", "max_stack": 50,
        "blurb": "Next 3 attacks against the active mob auto-crit.",
    },

    # ── Druid-bound (nature scrolls + brews; route to consumables) ─────────
    "thorn_aura_brew_craft": {
        "name": "Thorn Aura Brew", "emoji": "\U0001F33F", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/sunflower": 2, "crop/carrot": 2, "ore/SILVER": 1},
        "fgd_cost": 25.0, "min_level": 8,
        "apply": "consum/thorn_aura_brew", "max_stack": 50,
        "blurb": "Reflect 30% of melee damage back at the attacker for 4 rounds.",
    },
    "wildshape_potion_craft": {
        "name": "Wildshape Potion", "emoji": "\U0001F43B", "rarity": "rare",
        "specialty": "alchemy",
        "inputs": {"crop/pumpkin": 2, "fish/eel": 1, "ore/GOLD": 1},
        "fgd_cost": 80.0, "min_level": 16,
        "apply": "consum/wildshape_potion", "max_stack": 30,
        "blurb": "+50% ATK and heal 5% max HP per turn for 3 rounds.",
    },
    "regrowth_brew_craft": {
        "name": "Regrowth Brew", "emoji": "\U0001F33A", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/sunflower": 3, "crop/wheat": 2, "ore/COPPER": 4},
        "fgd_cost": 18.0, "min_level": 6,
        "apply": "consum/regrowth_brew", "max_stack": 60,
        "blurb": "+10% max HP per round for 5 rounds. Smells like spring rain.",
    },

    # ── Mage-bound (mana scrolls; route to consumables) ────────────────────
    "mana_draught_distill": {
        "name": "Mana Draught", "emoji": "\U0001F9EA", "rarity": "rare",
        "specialty": "alchemy",
        "inputs": {"crop/sunflower": 2, "ore/GOLD": 1, "fish/eel": 1},
        "fgd_cost": 70.0, "min_level": 14,
        "apply": "consum/mana_draught", "max_stack": 40,
        "blurb": "Resets your class-skill cooldown. Crackles in the bottle.",
    },
    "scroll_sanctuary_inscribe": {
        "name": "Scroll of Sanctuary", "emoji": "\U0001F4DC", "rarity": "rare",
        "specialty": "enchanting",
        "inputs": {"ore/GOLD": 2, "ore/SILVER": 6, "crop/sunflower": 2},
        "fgd_cost": 110.0, "min_level": 18,
        "apply": "consum/scroll_sanctuary", "max_stack": 30,
        "blurb": "Halve incoming damage for 2 rounds. The dungeon almost wants you alive.",
    },

    # ────────────────────────────────────────────────────────────────────────
    # Cross-game consumables with stat bonuses. These tie farming crops into
    # every other minigame: fish, farm, expedition, buddy, dungeon.
    # All require the new crops (mushroom, lavender, crystalmint, dreamroot,
    # pepper, rose) that were added to farming_config in the same update.
    # Routes: consum/* -> user_dungeon.consumables / fishing / farming /
    #         expedition buffs; buddy/* -> active buddy effects.
    # ────────────────────────────────────────────────────────────────────────

    # ── Fishing buffs ────────────────────────────────────────────────────────
    "anglers_paste": {
        "name": "Angler's Paste", "emoji": "\U0001F41F", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/mushroom": 2, "fish/minnow": 3, "ore/COPPER": 2},
        "fgd_cost": 12.0, "min_level": 5,
        "apply": "consum/anglers_paste", "max_stack": 80,
        "blurb": "+30% catch rate and +1 rarity bias for 20 casts.",
    },
    "siren_bait_brew": {
        "name": "Siren Bait Brew", "emoji": "\U0001F9DC", "rarity": "rare",
        "specialty": "alchemy",
        "inputs": {"crop/rose": 2, "crop/lavender": 2, "fish/salmon": 1,
                   "ore/SILVER": 2},
        "fgd_cost": 65.0, "min_level": 14,
        "apply": "consum/siren_bait_brew", "max_stack": 40,
        "blurb": "Legendary fish can't resist this scent. +2 rarity bias for 10 casts.",
    },

    # ── Farming buffs ────────────────────────────────────────────────────────
    "harvest_tonic": {
        "name": "Harvest Tonic", "emoji": "\U0001F9EA", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/mushroom": 3, "crop/lavender": 2, "ore/COPPER": 3},
        "fgd_cost": 18.0, "min_level": 6,
        "apply": "consum/harvest_tonic", "max_stack": 60,
        "blurb": "Your next 5 harvests yield +50%. Smells earthy.",
    },
    "growth_serum": {
        "name": "Growth Serum", "emoji": "\U0001F33F", "rarity": "rare",
        "specialty": "alchemy",
        "inputs": {"crop/crystalmint": 1, "crop/mushroom": 3, "ore/SILVER": 2},
        "fgd_cost": 90.0, "min_level": 16,
        "apply": "consum/growth_serum", "max_stack": 30,
        "blurb": "Halves grow time on your next planted crop. Don't drink it.",
    },
    "world_sap_distill": {
        "name": "World Sap", "emoji": "\U0001F333", "rarity": "epic",
        "specialty": "alchemy", "requires_specialty": True,
        "inputs": {"crop/dreamroot": 1, "crop/crystalmint": 2,
                   "crop/ambrosia": 1, "ore/GOLD": 4},
        "fgd_cost": 800.0, "min_level": 30,
        "apply": "consum/world_sap", "max_stack": 10,
        "blurb": "Triples yield on your next 3 harvests. Tastes of everything at once.",
    },

    # ── Expedition buffs ─────────────────────────────────────────────────────
    "expedition_ration": {
        "name": "Expedition Ration", "emoji": "\U0001F9F3", "rarity": "common",
        "specialty": "cooking",
        "inputs": {"crop/pepper": 2, "crop/potato": 3, "crop/wheat": 2},
        "fgd_cost": 5.0, "min_level": 2,
        "apply": "consum/expedition_ration", "max_stack": 100,
        "blurb": "+2 extra draws on your buddy's next expedition.",
    },
    "scouts_brew": {
        "name": "Scout's Brew", "emoji": "\U0001F9EA", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/lavender": 3, "crop/mushroom": 2, "ore/COPPER": 4},
        "fgd_cost": 22.0, "min_level": 8,
        "apply": "consum/scouts_brew", "max_stack": 50,
        "blurb": "+25% loot qty and affinity bonus on your buddy's next expedition.",
    },
    "void_draught": {
        "name": "Void Draught", "emoji": "\U0001F300", "rarity": "epic",
        "specialty": "alchemy", "requires_specialty": True,
        "inputs": {"crop/dreamroot": 2, "crop/crystalmint": 1,
                   "ore/GOLD": 5, "fish/kraken": 1},
        "fgd_cost": 600.0, "min_level": 28,
        "apply": "consum/void_draught", "max_stack": 10,
        "blurb": "Unlocks the Void Rift for your buddy's next expedition regardless of level.",
    },

    # ── Buddy stat buffs ─────────────────────────────────────────────────────
    "buddy_energizer": {
        "name": "Buddy Energizer", "emoji": "\U000026A1", "rarity": "uncommon",
        "specialty": "cooking",
        "inputs": {"crop/crystalmint": 1, "crop/lavender": 2, "ore/SILVER": 1},
        "fgd_cost": 20.0, "min_level": 7,
        "apply": "buddy/energy_boost", "max_stack": 80,
        "blurb": "+50 energy and +10 happiness to your active buddy.",
    },
    "rose_petal_tea": {
        "name": "Rose Petal Tea", "emoji": "\U0001F375", "rarity": "rare",
        "specialty": "cooking",
        "inputs": {"crop/rose": 3, "crop/lavender": 2, "ore/SILVER": 1},
        "fgd_cost": 55.0, "min_level": 12,
        "apply": "buddy/calm", "max_stack": 50,
        "blurb": "Calms a stressed buddy: +30 happiness and -0 hunger (pure mood reset).",
    },
    "dreamroot_elixir": {
        "name": "Dreamroot Elixir", "emoji": "\U0001F31B", "rarity": "legendary",
        "specialty": "alchemy", "requires_specialty": True,
        "inputs": {"crop/dreamroot": 1, "crop/ambrosia": 1,
                   "ore/GOLD": 6, "fish/leviathan": 1},
        "fgd_cost": 3000.0, "min_level": 40,
        "apply": "buddy/xp_surge", "max_stack": 5,
        "blurb": "+2000 XP to your active buddy and resets all cooldowns. Rare as they come.",
    },

    # ── Dungeon survivability ────────────────────────────────────────────────
    "vigor_brew": {
        "name": "Vigor Brew", "emoji": "\U0001F7E5", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/lavender": 3, "crop/wheat": 2, "ore/COPPER": 4},
        "fgd_cost": 16.0, "min_level": 6,
        "apply": "consum/vigor_brew", "max_stack": 60,
        "blurb": "+15% max HP for the next dungeon floor. Tastes like grass.",
    },
    "pepper_salve": {
        "name": "Pepper Salve", "emoji": "\U0001F336", "rarity": "common",
        "specialty": "alchemy",
        "inputs": {"crop/pepper": 3, "crop/carrot": 2, "ore/COPPER": 2},
        "fgd_cost": 6.0, "min_level": 3,
        "apply": "consum/pepper_salve", "max_stack": 100,
        "blurb": "+10% ATK on next dungeon combat. Burns on application.",
    },
    "dreamroot_ward": {
        "name": "Dreamroot Ward", "emoji": "\U0001F9FF", "rarity": "epic",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"crop/dreamroot": 1, "crop/crystalmint": 1,
                   "ore/GOLD": 3, "ore/SILVER": 4},
        "fgd_cost": 400.0, "min_level": 25,
        "apply": "consum/dreamroot_ward", "max_stack": 15,
        "blurb": "Absorbs the next lethal hit (like phoenix_down) and heals 25% max HP.",
    },

    # ── Buddy gear (wearable items, route to buddy/equip/<slot>/<item_key>) ──
    "flower_crown_craft": {
        "name": "Flower Crown", "emoji": "\U0001F33C", "rarity": "common",
        "specialty": "cooking",
        "inputs": {"crop/rose": 2, "crop/lavender": 3, "crop/sunflower": 1},
        "fgd_cost": 5.0, "min_level": 3,
        "apply": "buddy/equip/accessory/flower_crown", "max_stack": 5,
        "blurb": "A woven ring of wild blooms for your buddy's head.",
    },
    "star_badge_craft": {
        "name": "Star Badge", "emoji": "\U00002B50", "rarity": "uncommon",
        "specialty": "tinkering",
        "inputs": {"ore/SILVER": 3, "ore/COPPER": 4, "crop/sunflower": 1},
        "fgd_cost": 22.0, "min_level": 8,
        "apply": "buddy/equip/accessory/star_badge", "max_stack": 5,
        "blurb": "A polished metal badge. Crafted by a tinkerer who knew what matters.",
    },
    "golden_collar_craft": {
        "name": "Golden Collar", "emoji": "\U0001F451", "rarity": "rare",
        "specialty": "smithing",
        "inputs": {"ore/GOLD": 3, "ore/SILVER": 2, "crop/rose": 1},
        "fgd_cost": 75.0, "min_level": 15,
        "apply": "buddy/equip/accessory/golden_collar", "max_stack": 3,
        "blurb": "Heavy gold links shaped into a collar. Your buddy will preen.",
    },
    "lucky_bell_craft": {
        "name": "Lucky Bell", "emoji": "\U0001F514", "rarity": "uncommon",
        "specialty": "tinkering",
        "inputs": {"ore/COPPER": 5, "ore/SILVER": 2, "crop/lavender": 2},
        "fgd_cost": 30.0, "min_level": 9,
        "apply": "buddy/equip/charm/lucky_bell", "max_stack": 3,
        "blurb": "Rings with every step. +5% expedition loot when worn.",
    },
    "battle_charm_craft": {
        "name": "Battle Charm", "emoji": "\U00002694", "rarity": "rare",
        "specialty": "enchanting",
        "inputs": {"ore/SILVER": 4, "ore/GOLD": 1, "fish/pike": 1},
        "fgd_cost": 65.0, "min_level": 14,
        "apply": "buddy/equip/charm/battle_charm", "max_stack": 3,
        "blurb": "+8% ATK in buddy battles. Carved from a trophy fang.",
    },
    "vitality_stone_craft": {
        "name": "Vitality Stone", "emoji": "\U0001F9E1", "rarity": "rare",
        "specialty": "alchemy",
        "inputs": {"crop/crystalmint": 1, "ore/GOLD": 2, "ore/SILVER": 3},
        "fgd_cost": 90.0, "min_level": 17,
        "apply": "buddy/equip/charm/vitality_stone", "max_stack": 3,
        "blurb": "+10% max HP in buddy battles. Warm to the touch always.",
    },
    "growth_charm_craft": {
        "name": "Growth Charm", "emoji": "\U0001F331", "rarity": "rare",
        "specialty": "alchemy",
        "inputs": {"crop/crystalmint": 1, "crop/lavender": 3, "ore/SILVER": 2},
        "fgd_cost": 80.0, "min_level": 14,
        "apply": "buddy/equip/charm/growth_charm", "max_stack": 3,
        "blurb": "+10% XP from all sources. The charm grows as your buddy does.",
    },
    "void_amulet_craft": {
        "name": "Void Amulet", "emoji": "\U0001F300", "rarity": "epic",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"crop/dreamroot": 1, "ore/GOLD": 4, "ore/SILVER": 3,
                   "fish/kraken": 1},
        "fgd_cost": 350.0, "min_level": 24,
        "apply": "buddy/equip/charm/void_amulet", "max_stack": 2,
        "blurb": "+15% loot in the Void Rift. The swirl inside never stops.",
    },

    # ── Cosmetic outputs (route to users.cosmetics inventory) ──────────────
    # Cosmetics used to be ,shop buy items; they're craft-only now and
    # the apply target ``cosmetic/<key>`` lands one in the player's
    # cosmetics JSONB. Using one via ,inventory use grants the linked
    # Discord role for items_config.duration_seconds (default 1 hour).
    "shimmer_dust": {
        "name": "Shimmer Dust", "emoji": "\U0001F48E", "rarity": "common",
        "specialty": "alchemy",
        "inputs": {"crop/lavender": 2, "ore/COPPER": 4},
        "fgd_cost": 4.0, "min_level": 2,
        "apply": "cosmetic/glamour_kit", "max_stack": 25,
        "blurb": "Pinch of refined sparkle. Crafts a Glamour Kit (1h Glamour role).",
    },
    "moon_essence": {
        "name": "Moon Essence", "emoji": "\U0001F311", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/dreamroot": 1, "ore/SILVER": 4, "fish/eel": 1},
        "fgd_cost": 25.0, "min_level": 8,
        "apply": "cosmetic/night_crystal", "max_stack": 10,
        "blurb": "Pure midnight, distilled. Crafts a Night Crystal (1h Night role).",
    },
    "aurora_prism": {
        "name": "Aurora Prism", "emoji": "\U0001F308", "rarity": "rare",
        "specialty": "enchanting",
        "inputs": {"crop/crystalmint": 2, "crop/rose": 2,
                   "ore/GOLD": 3, "fish/manta": 1},
        "fgd_cost": 120.0, "min_level": 16,
        "apply": "cosmetic/aurora_pass", "max_stack": 5,
        "blurb": "Cut from the high-altitude sky-band. Crafts an Aurora Pass (1h Aurora role).",
    },

    # ────────────────────────────────────────────────────────────────────────
    # General-tier expansion. Cross-game routes that use crops + fish + ore
    # to feed every other minigame's inventory. Mostly common/uncommon so
    # newer crafters have a denser ladder under the existing rare/epic tier.
    # ────────────────────────────────────────────────────────────────────────
    "hooked_minnows": {
        "name": "Hooked Minnows", "emoji": "\U0001F95F", "rarity": "common",
        "specialty": "cooking",
        "inputs": {"crop/wheat": 3, "fish/minnow": 2},
        "fgd_cost": 1.5, "min_level": 2,
        "apply": "bait/worm", "max_stack": 500,
        "blurb": "Dough-balled minnows on a string. The chef's take on bait.",
    },
    "copper_chum_jar": {
        "name": "Copper Chum Jar", "emoji": "\U0001FAA3", "rarity": "common",
        "specialty": "smithing",
        "inputs": {"ore/COPPER": 4, "crop/corn": 1, "fish/anchovy": 2},
        "fgd_cost": 3.0, "min_level": 3,
        "apply": "bait/shrimp", "max_stack": 250,
        "blurb": "Soldered tin jar. Releases scent on the first cast.",
    },
    "silken_compass": {
        "name": "Silken Compass", "emoji": "\U0001F9ED", "rarity": "common",
        "specialty": "tinkering",
        "inputs": {"ore/COPPER": 6, "ore/SILVER": 1, "crop/wheat": 1},
        "fgd_cost": 8.0, "min_level": 6,
        "apply": "consum/pocket_compass", "max_stack": 50,
        "blurb": "Same compass, less specialty gating. Pointed-down accuracy.",
    },
    "mycelium_compost": {
        "name": "Mycelium Compost", "emoji": "\U0001F344", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/mushroom": 4, "crop/wheat": 2, "fish/minnow": 2},
        "fgd_cost": 16.0, "min_level": 7,
        "apply": "fert/compost", "max_stack": 100,
        "blurb": "Spore-rich loam. Crops sprout the same day they're sown.",
    },
    "gilded_arrow_bundle": {
        "name": "Gilded Arrow Bundle", "emoji": "\U0001F3F9", "rarity": "uncommon",
        "specialty": "fletching",
        "inputs": {"crop/wheat": 3, "ore/SILVER": 4, "ore/COPPER": 2},
        "fgd_cost": 28.0, "min_level": 10,
        "apply": "consum/broadhead_bundle", "max_stack": 500,
        "blurb": "15 silver-tipped broadheads. Punches above its tier.",
    },
    "pepper_torch": {
        "name": "Pepper Torch", "emoji": "\U0001F525", "rarity": "uncommon",
        "specialty": "smithing",
        "inputs": {"crop/pepper": 3, "fish/carp": 1, "ore/COPPER": 4},
        "fgd_cost": 30.0, "min_level": 11,
        "apply": "consum/pepper_salve", "max_stack": 100,
        "blurb": "Capsaicin-soaked rag on a stick. +10% ATK, burns the hand a bit.",
    },
    "feastmaster_loaf": {
        "name": "Feastmaster Loaf", "emoji": "\U0001F35E", "rarity": "rare",
        "specialty": "cooking",
        "inputs": {"crop/wheat": 8, "crop/pumpkin": 2, "crop/carrot": 3,
                   "fish/trout": 1},
        "fgd_cost": 95.0, "min_level": 16,
        # Effect: +300 XP for the active buddy (mirrors harvest_pie).
        "apply": "buddy/xp_big", "max_stack": 25,
        "blurb": "+300 XP for the active buddy. Crust like a war shield.",
    },
    "thunderstrike_lure": {
        "name": "Thunderstrike Lure", "emoji": "\U000026A1", "rarity": "epic",
        "specialty": "fletching",
        "inputs": {"fish/manta": 1, "fish/swordfish": 1, "ore/GOLD": 8,
                   "ore/SILVER": 6, "crop/sunflower": 4},
        "fgd_cost": 1100.0, "min_level": 28,
        "apply": "bait/chum", "max_stack": 25,
        "blurb": "Wired with stormglass. The water flinches when you drop it.",
    },

    # ── Specialty-locked additions (one each across smithing / tinkering /
    # enchanting; alchemy + cooking already deep). ─────────────────────────
    "gambit_chip": {
        "name": "Gambit Chip", "emoji": "\U0001F3B0", "rarity": "rare",
        "specialty": "tinkering", "requires_specialty": True,
        "inputs": {"ore/SILVER": 6, "ore/GOLD": 2, "fish/octopus": 1},
        "fgd_cost": 60.0, "min_level": 11,
        # Reuses oracle_lens slot: peek the next gamble before committing.
        "apply": "consum/oracle_lens", "max_stack": 25,
        "blurb": "Casino-grade poker chip with a hidden mirror. Peek the next bet.",
    },
    "harvest_grimoire": {
        "name": "Harvest Grimoire", "emoji": "\U0001F4D6", "rarity": "rare",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"crop/lavender": 3, "ore/SILVER": 4, "ore/GOLD": 1,
                   "crop/dreamroot": 1},
        "fgd_cost": 130.0, "min_level": 19,
        "apply": "consum/scouts_brew", "max_stack": 50,
        "blurb": "Hand-bound spellbook of expedition prayers. +25% loot, +affinity.",
    },
    "molten_quench_oil": {
        "name": "Molten Quench Oil", "emoji": "\U0001F525", "rarity": "rare",
        "specialty": "smithing", "requires_specialty": True,
        "inputs": {"fish/lobster": 3, "ore/GOLD": 4, "ore/SILVER": 12,
                   "fish/swordfish": 1},
        "fgd_cost": 220.0, "min_level": 20,
        "apply": "consum/diamond_pickaxe", "max_stack": 50,
        "blurb": "Volcanic quench. Your next mine cracks the rock to the seam.",
    },

    # ────────────────────────────────────────────────────────────────────────
    # Forge-Sealed recipes -- the FORGE-token sink. These consume FORGE
    # directly via the existing token/<SYMBOL> input parser (services/
    # crafting._consume_token), giving FORGE a real burn destination beyond
    # the one-way USD cashout. Specialist-locked, late-game tier so the
    # FORGE you stake-yielded out of INGOT actually has a place to go.
    # ────────────────────────────────────────────────────────────────────────
    "forgemaster_band_craft": {
        "name": "Forgemaster Band", "emoji": "\U0001F9E1", "rarity": "epic",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"ore/GOLD": 6, "ore/SILVER": 4, "fish/octopus": 1,
                   "token/FORGE": 15, "crop/sunflower": 3},
        "fgd_cost": 800.0, "min_level": 28,
        "apply": "buddy/equip/accessory/forgemaster_band", "max_stack": 2,
        "blurb": "Ember-warm headband stamped with the forge sigil. Forge-sealed.",
    },
    "forge_seal_pendant_craft": {
        "name": "Forge-Seal Pendant", "emoji": "\U0001F525", "rarity": "legendary",
        "specialty": "smithing", "requires_specialty": True,
        "inputs": {"ore/GOLD": 8, "ore/SILVER": 6, "fish/marlin": 1,
                   "token/FORGE": 25, "crop/lavender": 4},
        "fgd_cost": 2200.0, "min_level": 30,
        "apply": "buddy/equip/charm/forge_seal_pendant", "max_stack": 1,
        "blurb": "+8% ATK and +8% HP. The seal hisses when struck.",
    },
    "forgeheart_elixir": {
        "name": "Forgeheart Elixir", "emoji": "\U0001F9EA", "rarity": "legendary",
        "specialty": "alchemy", "requires_specialty": True,
        "inputs": {"fish/kraken": 1, "ore/GOLD": 12, "crop/ambrosia": 1,
                   "token/FORGE": 50, "crop/dreamroot": 4},
        "fgd_cost": 5800.0, "min_level": 45,
        "apply": "buddy/xp_surge", "max_stack": 3,
        "blurb": "Drinks like a furnace. +XP surge and resets every cooldown.",
    },

    # ────────────────────────────────────────────────────────────────────────
    # Buddy-gear craft recipes for the new entries in buddy_gear_config.
    # Routes through buddy/equip/<slot>/<item_key> just like the existing
    # gear recipes; the buddy_gear_config.BUDDY_GEAR entries carry the
    # stat_bonus payload so battle / expedition readers don't need to know
    # about crafting at all.
    # ────────────────────────────────────────────────────────────────────────
    "silken_ribbon_craft": {
        "name": "Silken Ribbon", "emoji": "\U0001F397", "rarity": "common",
        "specialty": "fletching",
        "inputs": {"crop/lavender": 4, "crop/rose": 2, "crop/corn": 2},
        "fgd_cost": 4.0, "min_level": 3,
        "apply": "buddy/equip/accessory/silken_ribbon", "max_stack": 5,
        "blurb": "Hand-loomed silk band. Catches every breeze.",
    },
    "iron_band_craft": {
        "name": "Iron Band", "emoji": "\U0001FA84", "rarity": "uncommon",
        "specialty": "smithing",
        "inputs": {"ore/COPPER": 8, "ore/SILVER": 3, "fish/perch": 1},
        "fgd_cost": 18.0, "min_level": 6,
        "apply": "buddy/equip/charm/iron_band", "max_stack": 3,
        "blurb": "Beaten iron strap. +5% max HP charm for buddy battles.",
    },
    "mossy_amulet_craft": {
        "name": "Mossy Amulet", "emoji": "\U0001F33F", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/mushroom": 3, "crop/lavender": 2, "ore/SILVER": 2},
        "fgd_cost": 22.0, "min_level": 8,
        "apply": "buddy/equip/charm/mossy_amulet", "max_stack": 3,
        "blurb": "Lichen-bound stone. +5% XP charm; still growing.",
    },
    "mosaic_scarf_craft": {
        "name": "Mosaic Scarf", "emoji": "\U0001F9E3", "rarity": "rare",
        "specialty": "cooking",
        "inputs": {"crop/pumpkin": 3, "crop/sunflower": 3, "ore/SILVER": 2,
                   "crop/carrot": 2},
        "fgd_cost": 55.0, "min_level": 12,
        "apply": "buddy/equip/accessory/mosaic_scarf", "max_stack": 3,
        "blurb": "Patchwork of every season's harvest. No two are alike.",
    },
    "sunbeam_locket_craft": {
        "name": "Sunbeam Locket", "emoji": "\U0001F506", "rarity": "rare",
        "specialty": "smithing",
        "inputs": {"ore/SILVER": 4, "ore/GOLD": 3, "crop/sunflower": 4,
                   "fish/trout": 1},
        "fgd_cost": 110.0, "min_level": 18,
        "apply": "buddy/equip/charm/sunbeam_locket", "max_stack": 3,
        "blurb": "+10% expedition loot. Catches a sliver of every dawn.",
    },
    "tempest_amulet_craft": {
        "name": "Tempest Amulet", "emoji": "\U000026C8", "rarity": "epic",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"ore/GOLD": 5, "ore/SILVER": 6, "fish/manta": 1,
                   "crop/crystalmint": 2, "fish/eel": 1},
        "fgd_cost": 400.0, "min_level": 22,
        "apply": "buddy/equip/charm/tempest_amulet", "max_stack": 2,
        "blurb": "+12% ATK. Crackles when the buddy roars.",
    },

    # ── Buddy Battle consumables (apply: battle/<key>) ─────────────────
    # Routes to user_buddy_economy.battle_inventory; the buddy battle
    # view reads from there to populate the in-battle dropdown.
    # Catalogue + effect math lives in
    # ``buddies_config.BATTLE_CONSUMABLES``.
    "berry_quick_craft": {
        "name": "Quick Berry", "emoji": "\U0001F353", "rarity": "common",
        "specialty": "alchemy",
        "inputs": {"crop/wheat": 2, "crop/tomato": 2},
        "fgd_cost": 1.0, "min_level": 2,
        "apply": "battle/berry_quick", "max_stack": 99,
        "blurb": "Restore 25% HP mid-battle. CD 3 rounds.",
    },
    "berry_focus_craft": {
        "name": "Focus Berry", "emoji": "\U0001FAD0", "rarity": "common",
        "specialty": "alchemy",
        "inputs": {"crop/carrot": 2, "fish/minnow": 1},
        "fgd_cost": 1.5, "min_level": 3,
        "apply": "battle/berry_focus", "max_stack": 99,
        "blurb": "Next attack is a guaranteed crit. CD 4 rounds.",
    },
    "vial_rage_craft": {
        "name": "Vial of Rage", "emoji": "\U0001F9EA", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"crop/eggplant": 2, "ore/COPPER": 4, "fish/perch": 1},
        "fgd_cost": 8.0, "min_level": 6,
        "apply": "battle/vial_rage", "max_stack": 50,
        "blurb": "+30% ATK for 2 rounds. CD 5 rounds.",
    },
    "vial_iron_craft": {
        "name": "Iron Vial", "emoji": "\U0001F9F2", "rarity": "uncommon",
        "specialty": "alchemy",
        "inputs": {"ore/COPPER": 6, "ore/SILVER": 1, "crop/tomato": 1},
        "fgd_cost": 8.0, "min_level": 6,
        "apply": "battle/vial_iron", "max_stack": 50,
        "blurb": "-25% damage taken for 2 rounds. CD 5 rounds.",
    },
    "dust_swift_craft": {
        "name": "Swift Dust", "emoji": "\U0001F4A8", "rarity": "uncommon",
        "specialty": "tinkering",
        "inputs": {"ore/SILVER": 2, "crop/sunflower": 1, "fish/anchovy": 1},
        "fgd_cost": 10.0, "min_level": 8,
        "apply": "battle/dust_swift", "max_stack": 50,
        "blurb": "+0.30 SPD for the rest of the battle. CD 4 rounds.",
    },
    "cure_balm_craft": {
        "name": "Cure Balm", "emoji": "\U0001F33F", "rarity": "rare",
        "specialty": "alchemy", "requires_specialty": True,
        "inputs": {"crop/sunflower": 3, "fish/octopus": 1, "ore/SILVER": 3},
        "fgd_cost": 45.0, "min_level": 12,
        "apply": "battle/cure_balm", "max_stack": 30,
        "blurb": "Clear all debuffs + 10% HP. CD 3 rounds.",
    },
    "shock_bolt_craft": {
        "name": "Shock Bolt", "emoji": "\U000026A1", "rarity": "rare",
        "specialty": "tinkering", "requires_specialty": True,
        "inputs": {"ore/SILVER": 5, "ore/GOLD": 1, "fish/eel": 1},
        "fgd_cost": 80.0, "min_level": 16,
        "apply": "battle/shock_bolt", "max_stack": 25,
        "blurb": "0.60x ATK bolt + stun 1 turn. CD 6 rounds.",
    },
    "phoenix_tear_craft": {
        "name": "Phoenix Tear", "emoji": "\U0001F525", "rarity": "epic",
        "specialty": "enchanting", "requires_specialty": True,
        "inputs": {"ore/GOLD": 4, "fish/shark": 1,
                   "crop/sunflower": 4, "token/FREN": 50},
        "fgd_cost": 600.0, "min_level": 28,
        "apply": "battle/phoenix_tear", "max_stack": 5,
        "blurb": "Once per battle: revive at 35% HP on KO.",
    },
}


# ── Helper functions ────────────────────────────────────────────────────────

def craft_meta(craft_key: str) -> dict | None:
    """Return the catalog entry for ``craft_key`` or None if unknown."""
    return CRAFT_ITEMS.get(str(craft_key or "").lower())


def xp_for_level(level: int) -> int:
    """Total XP threshold required to BE level ``level`` (level 1 = 0 XP).

    Mirrors dungeon_config.xp_for_level: each level costs ``XP_BASE *
    XP_GROWTH ** (level - 1)`` rounded to int, summed.
    """
    if level <= 1:
        return 0
    if level > MAX_LEVEL:
        level = MAX_LEVEL
    total = 0.0
    span = float(XP_BASE)
    for _ in range(1, level):
        total += span
        span *= XP_GROWTH
    return int(total)


def level_from_xp(xp: int) -> int:
    """Return the level a player with ``xp`` total XP has reached, capped at
    :data:`MAX_LEVEL`.
    """
    if xp <= 0:
        return 1
    lvl = 1
    while lvl < MAX_LEVEL and xp >= xp_for_level(lvl + 1):
        lvl += 1
    return lvl


def parse_input_key(key: str) -> tuple[str, str]:
    """Split ``'fish/bass'`` -> ``('fish', 'bass')``.

    Returns ``('', '')`` on malformed input so the caller can fail soft.
    Recognised prefixes: ``fish``, ``crop``, ``recipe``, ``ore``, ``token``.
    """
    s = str(key or "").strip()
    if "/" not in s:
        return ("", "")
    kind, _, sub = s.partition("/")
    return (kind.lower(), sub.strip())


def parse_apply_target(apply: str) -> tuple[str, str]:
    """Split ``'bait/worm'`` -> ``('bait', 'worm')``.

    Returns ``('', '')`` on malformed input. Recognised prefixes:
    ``bait``, ``fert``, ``consum``, ``weapon``, ``armor``, ``buddy``,
    ``cosmetic`` (cosmetics are craft-only; the cosmetic key matches a
    SHOP_ITEMS entry with category=cosmetic), ``battle`` (buddy-battle
    consumables that route into user_buddy_economy.battle_inventory;
    the post-slash key matches buddies_config.BATTLE_CONSUMABLES).
    """
    return parse_input_key(apply)


def recipes_at_level(level: int) -> list[tuple[str, dict]]:
    """Return all (key, meta) pairs whose ``min_level`` <= ``level``,
    sorted by min_level then alphabetically. Used by ,craft list.
    """
    out = [(k, v) for k, v in CRAFT_ITEMS.items()
           if int(v.get("min_level", 1)) <= int(level)]
    out.sort(key=lambda kv: (int(kv[1].get("min_level", 1)), kv[0]))
    return out

