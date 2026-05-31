"""
dungeon_config.py  -  Catalogs and tuning constants for the Discoin Delve crawler.

All tuning lives here so cogs/dungeon.py and services/dungeon.py never
hard-code mob stats, floor pools, item prices, classes, or animation
frames.

Sections (in order):
    Tuning constants
    Token economy (COPPER / SILVER / GOLD / RUNE)
    Animation frames
    Class catalog
    Mob catalog
    Floor catalog
    Weapon / armor / consumable catalogs
    Helper functions

Conventions:
    -- Mining rewards pay out in COPPER / SILVER / GOLD on the Crypt
       Network (all four Crypt tokens are in Config.EARN_ONLY_TOKENS).
       Shop prices are in RUNE (the network coin, also earn-only).
    -- ORE -> RUNE conversion is one-way (burn-swap or stake yield).
       RUNE -> USD cashout is one-way (burn at oracle minus impact).
       Neither side can be acquired with USD or via .buy / .swap from
       any other token. That is the entire pay-to-win firewall.
    -- Weights are arbitrary integers in a weighted random pool. Bigger
       integers => more common.
"""
from __future__ import annotations

import random
from typing import Final


# ============================================================================
# Tuning constants
# ============================================================================

MAX_FLOOR:               Final[int]   = 100
MAX_LEVEL:               Final[int]   = 50
STARTING_HP:             Final[int]   = 30
HP_PER_LEVEL:            Final[int]   = 4
XP_BASE:                 Final[int]   = 100
XP_GROWTH:               Final[float] = 1.20

RUN_COOLDOWN_S:          Final[int]   = 30
ACTION_COOLDOWN_S:       Final[int]   = 4

BATTLE_MAX_ROUNDS:       Final[int]   = 30
FLEE_BASE_CHANCE:        Final[float] = 0.55
FLEE_HP_PENALTY_PCT:     Final[float] = 0.15

CAPTURE_HP_THRESHOLD:    Final[float] = 0.30
CAPTURE_BASE_CHANCE:     Final[float] = 0.35
CAPTURE_PER_TIER_PENALTY: Final[float] = 0.05

MAX_PARTY_SIZE:          Final[int]   = 6

MINE_BASE_QTY:           Final[float] = 5.0
BOSS_FLOOR_INTERVAL:     Final[int]   = 5

CRIT_BASE:               Final[float] = 0.05
CRIT_SPD_SCALE:          Final[float] = 0.30
CRIT_MULT:               Final[float] = 1.6

BUDDY_ASSIST_DAMAGE_FRACTION: Final[float] = 0.50
BUDDY_ASSIST_TURN_CHANCE:     Final[float] = 0.45


# ============================================================================
# Stat-point allocation (mirrors buddy upgrade system)
# ============================================================================
# Every level grants STAT_POINTS_PER_LEVEL points. Players spend them via
# `,delve upgrade` across four lanes (Hardiness / Power / Vigor / Wisdom).
# Allocations are sticky -- they persist across class reroll, equip change,
# and run lifecycle. Only an explicit ,delve respec (Phase 4) clears them.
#
# Available points = level * STAT_POINTS_PER_LEVEL - (hp_alloc + atk_alloc + spd_alloc + int_alloc).
STAT_POINTS_PER_LEVEL:   Final[int]   = 1

# Per-point bonuses applied at player_combat_stats() build time inside
# services/dungeon.py. INT scales spell damage (damage scrolls + Druid /
# Mage skills) the same way ATK scales physical swings, so caster classes
# have a meaningful spend lane.
STAT_POINT_HP_BONUS:     Final[float] = 4.0
STAT_POINT_ATK_BONUS:    Final[float] = 0.6
STAT_POINT_SPD_BONUS:    Final[float] = 0.005
STAT_POINT_INT_BONUS:    Final[float] = 0.6


# ============================================================================
# Stat-point respec
# ============================================================================
# `,delve respec` refunds every spent point back to "available" so the
# player can rebuild from scratch (e.g. swapping a Power-stacked Warrior
# build onto an Archer + Vigor pivot). Price doubles per respec on the
# same delver so chronic re-statting is metered:
#
#   respec #n -> RESPEC_BASE_PRICE_USD * 2 ** (n - 1)
#
# Mirrors the `,buddy respec` curve in buddies_config.RESPEC_BASE_PRICE_USD
# (kept cheaper here: delve points trickle in level-by-level, so they
# matter less than a buddy's pooled allocation).
RESPEC_BASE_PRICE_USD: Final[float] = 10_000.0
RESPEC_GROWTH:         Final[float] = 2.0


def respec_cost_usd(respecs_used: int) -> float:
    """Cost in USD for the next stat-point respec. Doubles per respec."""
    n = max(0, int(respecs_used or 0))
    return float(RESPEC_BASE_PRICE_USD) * (RESPEC_GROWTH ** n)


# ============================================================================
# Class reroll
# ============================================================================
# `,delve reroll <new_class>` lets a player swap classes without losing
# level/XP/owned gear/captures/totals. Cost ramps so reroll-spamming isn't
# free; first reroll is cheap enough to recover from a mis-pick. Cleared
# state on reroll: equipped_weapon + equipped_armor (snap to new class
# starter), skill cooldown, current run (must rest first).
CLASS_REROLL_BASE_USD:   Final[float] = 5_000.0
CLASS_REROLL_GROWTH:     Final[float] = 2.0
CLASS_REROLL_COOLDOWN_S: Final[int]   = 6 * 3600   # 6h between rerolls


def class_reroll_cost_usd(rerolls_used: int) -> float:
    """Cost in USD for the next reroll. Doubles each prior reroll."""
    n = max(0, int(rerolls_used or 0))
    return float(CLASS_REROLL_BASE_USD) * (CLASS_REROLL_GROWTH ** n)


# ============================================================================
# Combat: ranged vs melee
# ============================================================================
# Ranged weapons (bow / crossbow) take their first swing before the mob
# regardless of SPD (kiting), and the mob's swing back deals
# RANGED_RETALIATION_MULT of its damage (the player can shoot from
# distance the first round, then draws into close-quarters trade).
# Out-of-ammo bows/crossbows fall back to OUT_OF_AMMO_DAMAGE_MULT of base
# damage (improvised throwing), so running dry isn't an instant loss.
RANGED_FIRST_STRIKE:        Final[bool]  = True
RANGED_RETALIATION_MULT:    Final[float] = 0.85
RANGED_CRIT_BONUS:          Final[float] = 0.05
OUT_OF_AMMO_DAMAGE_MULT:    Final[float] = 0.50

# Per-shot ammo consumption. Bows pull from arrow_bundle, crossbows from
# bolt_bundle (both new consumables). Each swing burns one. Ammo helpers
# in services/dungeon.py do the look-up; this constant exists so the
# fallback path (no ammo => half damage) can be tweaked without code edits.
AMMO_PER_RANGED_SWING:      Final[int]   = 1


# ============================================================================
# Weapon / armor type taxonomy
# ============================================================================
# Class-restricted equipment. Every WEAPON entry MUST declare a
# weapon_type from WEAPON_TYPES; every ARMOR entry MUST declare an
# armor_type from ARMOR_TYPES. Class metadata then declares which
# weapon/armor types it can equip; equip_item enforces this server-side.

WEAPON_TYPES: Final[tuple[str, ...]] = (
    "longsword", "shortsword", "axe", "mace",
    "bow", "crossbow",
    "staff", "rod",
)

ARMOR_TYPES: Final[tuple[str, ...]] = ("light", "medium", "heavy")

# Every ranged weapon also declares which ammo consumable it draws from.
RANGED_WEAPON_TYPES:  Final[tuple[str, ...]] = ("bow", "crossbow")

WEAPON_TYPE_AMMO_KEY: Final[dict[str, str]] = {
    "bow":      "arrow_bundle",
    "crossbow": "bolt_bundle",
}


# ============================================================================
# Token economy (Crypt Network)
# ============================================================================

CRYPT_NETWORK_SHORT: Final[str] = "cry"

COPPER_SYMBOL: Final[str] = "COPPER"
SILVER_SYMBOL: Final[str] = "SILVER"
GOLD_SYMBOL:   Final[str] = "GOLD"
RUNE_SYMBOL:   Final[str] = "RUNE"

ORE_SYMBOLS: Final[tuple[str, ...]] = (COPPER_SYMBOL, SILVER_SYMBOL, GOLD_SYMBOL)

COPPER_EMOJI: Final[str] = "\U0001FA99"
SILVER_EMOJI: Final[str] = "\U0001F948"
GOLD_EMOJI:   Final[str] = "\U0001F947"
RUNE_EMOJI:   Final[str] = "\U0001FAA8"

# RUNE accrued per ore-unit-staked per day. Tier-scaled so a player who
# stakes 1 GOLD earns ~60x what 1 COPPER earns, matching the floor depth
# required to find each tier.
ORE_STAKE_RUNE_PER_DAY: Final[dict[str, float]] = {
    COPPER_SYMBOL: 0.005,
    SILVER_SYMBOL: 0.040,
    GOLD_SYMBOL:   0.300,
}

ORE_BURN_LP_REWARD_BPS:    Final[int] = 100
RUNE_CASHOUT_LP_REWARD_BPS: Final[int] = 100


# ============================================================================
# Animation frames
# ============================================================================
# Each frame is wrapped in a code fence by the cog so the monospace
# alignment survives Discord's font rendering. Keep them under ~38 chars
# wide so they look good on phone clients.

_FRAME_TOWN = """\
       ______________
      /              \\
     /  CRYPT TAVERN  \\
    /__________________\\
    |  []         []   |
    |       _____      |
    |  []  |     | []  |
    |  []  |  X  | []  |
    |______|_____|_____|
     :: open for delvers ::
"""

_FRAME_CORRIDOR = """\
##########################
#  .  .  .  .  .  .  .   #
#                        #
#       @                #
#                        #
#  .  .  .  .  .  .  .   #
##########################
"""

_FRAME_MOB = """\
##########################
#                        #
#                        #
#       @         {glyph}      #
#                        #
#                        #
##########################
"""

_FRAME_ORE = """\
##########################
#  *      *      *       #
#    *    {ore_glyph}      *      #
#       @                #
#    *         *         #
#  *      *      *       #
##########################
"""

_FRAME_SHRINE = """\
##########################
#         | |            #
#       __|+|__          #
#      [_______]         #
#       @                #
#                        #
##########################
"""

_FRAME_STAIRS = """\
##########################
#                        #
#         _____          #
#        /____/          #
#       /____/   @       #
#      /____/            #
##########################
"""

_FRAME_BOSS = """\
##########################
#  !!!  ! !  ! !  !!!    #
#                        #
#       @     {glyph}        #
#                        #
#  !!!  ! !  ! !  !!!    #
##########################
"""

_FRAME_VICTORY = """\
   __     ___      _
   \\ \\   / (_) ___| |_ ___  _ __ _   _
    \\ \\ / /| |/ __| __/ _ \\| '__| | | |
     \\ V / | | (__| || (_) | |  | |_| |
      \\_/  |_|\\___|\\__\\___/|_|   \\__, |
                                  |___/
"""

_FRAME_DEFEAT = """\
   ____         __           _
  |  _ \\  ___ / _| ___  __ _| |_
  | | | |/ _ \\ |_ / _ \\/ _` | __|
  | |_| |  __/  _|  __/ (_| | |_
  |____/ \\___|_|  \\___|\\__,_|\\__|
        ... you fall to dust ...
"""

_FRAME_CAPTURE = """\
        .---.
       /     \\
      |  o_o  |
       \\  ^  /
        |||||
       (~~~~~)
   :: Captured! ::
"""

_FRAME_MINING = """\
##########################
#       _ /              #
#      ( )<              #
#       T  *  *  *       #
#       @                #
#                        #
##########################
"""

_FRAME_CHEST = """\
##########################
#       ______           #
#      /______\\          #
#     |  $$$$  |         #
#       @                #
#                        #
##########################
"""

_FRAME_SHRINE_PRAY = """\
##########################
#         | |            #
#       __|+|__          #
#      [__***__]    +    #
#       @  *kneels*      #
#       light gathers    #
##########################
"""

_FRAME_SHRINE_BLESSING = """\
##########################
#       \\\\|/             #
#      --(O)--            #
#       /|\\\\             #
#       @  *blessed*     #
#  light pours over you  #
##########################
"""

_FRAME_SHRINE_CURSE = """\
##########################
#       __  __           #
#      [_X][_X]          #
#       \\\\__//           #
#       @  *recoils*     #
#  the shrine bites back #
##########################
"""

_FRAME_RELIC_DROP = """\
##########################
#       *  .  *  .  *    #
#         _____          #
#        | <O> |   *glow #
#         '---'          #
#       @                #
#  a relic surfaces!     #
##########################
"""

_FRAME_CURSE_ARMED = """\
##########################
#         /\\\\            #
#        /  \\\\           #
#       / X  \\\\          #
#       \\\\____/         #
#       @  *whispers*    #
#  curse settles on you  #
##########################
"""

FRAMES: dict[str, str] = {
    "town":     _FRAME_TOWN,
    "corridor": _FRAME_CORRIDOR,
    "mob_room": _FRAME_MOB,
    "ore_room": _FRAME_ORE,
    "shrine":   _FRAME_SHRINE,
    "shrine_pray":     _FRAME_SHRINE_PRAY,
    "shrine_blessing": _FRAME_SHRINE_BLESSING,
    "shrine_curse":    _FRAME_SHRINE_CURSE,
    "stairs":   _FRAME_STAIRS,
    "boss_room": _FRAME_BOSS,
    "victory":  _FRAME_VICTORY,
    "defeat":   _FRAME_DEFEAT,
    "capture":  _FRAME_CAPTURE,
    "mining":   _FRAME_MINING,
    "chest":    _FRAME_CHEST,
    "relic_drop":  _FRAME_RELIC_DROP,
    "curse_armed": _FRAME_CURSE_ARMED,
}


# ============================================================================
# Scavenge (free 10-min wander between runs; mirrors farm forage)
# ============================================================================
# Free roll outside an active delve: small RUNE / ORE purses, occasional
# dungeon consumables (potions/scrolls), and on a rare jackpot drops a
# pulsing relic shard straight into the delve loot bag. No inputs
# consumed, 10-minute DB-clock cooldown on user_dungeon.last_scavenge_at.

SCAVENGE_COOLDOWN_S: Final[int] = 600   # 10 minutes between scavenges

SCAVENGE_OUTCOME_WEIGHTS: Final[dict[str, float]] = {
    "rune_purse_small":  30.0,
    "rune_purse_big":    12.0,
    "ore_pile_small":    22.0,
    "ore_pile_big":       8.0,
    "consumable_cache": 14.0,
    "scroll_find":       8.0,
    "relic_shard":       2.0,
    "empty":             4.0,
}

SCAVENGE_PAYOUTS: Final[dict[str, tuple[float, float]]] = {
    "rune_purse_small": (    20.0,    150.0),
    "rune_purse_big":   (   250.0,  2_000.0),
    "ore_pile_small":   (    30.0,    250.0),  # ore qty range; symbol picked per-run
    "ore_pile_big":     (   400.0,  3_000.0),
}

# Cheap-to-mid consumables only -- the legendary ones stay in the proper
# delve loot tables.
SCAVENGE_CONSUMABLE_POOL: Final[tuple[str, ...]] = (
    "potion_minor", "potion_major",
    "tame_charm", "pickaxe_oil", "rune_lure",
)
SCAVENGE_CONSUMABLE_QTY: Final[tuple[int, int]] = (1, 3)
SCAVENGE_CONSUMABLE_PICKS: Final[int] = 2  # distinct keys per drop

# Scrolls drop only the escape kind here -- the proper loot table covers
# the rest.
SCAVENGE_SCROLL_POOL: Final[tuple[str, ...]] = (
    "scroll_escape",
)
SCAVENGE_SCROLL_QTY: Final[tuple[int, int]] = (1, 2)

# Jackpot: a relic shard. Adds straight to the player's relic bag (the
# same one room rewards feed). Mirrors the ancient_tuber farm jackpot.
SCAVENGE_RELIC_QTY: Final[tuple[int, int]] = (1, 1)


def roll_scavenge_outcome(rng: random.Random | None = None) -> str:
    """Return a weighted scavenge outcome key."""
    rng = rng or random
    keys = list(SCAVENGE_OUTCOME_WEIGHTS.keys())
    weights = [SCAVENGE_OUTCOME_WEIGHTS[k] for k in keys]
    return rng.choices(keys, weights=weights, k=1)[0]


# ASCII frames for the reveal. Same shape as the farm-forage and
# beachcomb frames so the three commands feel like siblings.
_FRAME_SCAVENGE_START = "\n".join([
    "    o    *poking through the rubble*  ",
    "   /|       _____                     ",
    "   /\\      /     \\    .  ,  .         ",
    "          |  ?  |   .  ~  .            ",
    "           \\___/    .  Y  .            ",
    "  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~        ",
    "    surface ruin, crypt-side  ...      ",
])
_FRAME_SCAVENGE_RUNE = "\n".join([
    "    o    .   ___    .                 ",
    "   /|       /   \\                     ",
    "   /\\      | RUNE |  *hums*           ",
    "            \\___/                     ",
    "  ~  ~  ~  ~  ~  ~  ~  ~  ~           ",
    "    a small purse  -  RUNE!           ",
])
_FRAME_SCAVENGE_ORE = "\n".join([
    "    o    .  *  .  *                   ",
    "   /|     ___                         ",
    "   /\\    | * |  raw ore!              ",
    "         |***|                         ",
    "         |***|                         ",
    "  ~  ~  ~  ~  ~  ~  ~  ~  ~           ",
    "    a tidy ore pile  -  free ore!     ",
])
_FRAME_SCAVENGE_CONSUMABLE = "\n".join([
    "    o     ____   ____                 ",
    "   /|    | () | | () |                 ",
    "   /\\    |____| |____|  *clinks*       ",
    "                                       ",
    "  ~  ~  ~  ~  ~  ~  ~  ~  ~           ",
    "    a cache of consumables!            ",
])
_FRAME_SCAVENGE_SCROLL = "\n".join([
    "    o     .---.    *unfurls*           ",
    "   /|    |  S  |                       ",
    "   /\\   |  -- |                        ",
    "         |____|  scroll find           ",
    "  ~  ~  ~  ~  ~  ~  ~  ~  ~           ",
    "    sealed parchment in the dirt       ",
])
_FRAME_SCAVENGE_RELIC = "\n".join([
    "    o   *  .   *  .   *                ",
    "   /|       (( ))                      ",
    "   /\\      (( <O> ))   *glows*         ",
    "         (( pulsing ))                 ",
    "           (( ))                       ",
    "  ~  +  ~  +  ~  +  ~  +  ~           ",
    "    RELIC SHARD!  pulsing softly       ",
])
_FRAME_SCAVENGE_EMPTY = "\n".join([
    "    o      ,    ,                      ",
    "   /|         dust, more dust          ",
    "   /\\      ,   *  ,   ,                ",
    "         (cough)                       ",
    "  ~~~~~~~~~~~~~~~~~~~~~~~~~~           ",
    "    nothing  -  just bones             ",
])

FRAMES["scavenge_start"]      = _FRAME_SCAVENGE_START
FRAMES["scavenge_rune"]       = _FRAME_SCAVENGE_RUNE
FRAMES["scavenge_ore"]        = _FRAME_SCAVENGE_ORE
FRAMES["scavenge_consumable"] = _FRAME_SCAVENGE_CONSUMABLE
FRAMES["scavenge_scroll"]     = _FRAME_SCAVENGE_SCROLL
FRAMES["scavenge_relic"]      = _FRAME_SCAVENGE_RELIC
FRAMES["scavenge_empty"]      = _FRAME_SCAVENGE_EMPTY


# ============================================================================
# Class catalog
# ============================================================================
# Skill effect contract:
#   skill_mult           -- damage multiplier on the swing
#   skill_auto_crit      -- the swing always crits (rogue)
#   skill_cd             -- rounds before it can fire again
CLASSES: dict[str, dict] = {
    "warrior": {
        "key": "warrior", "name": "Warrior", "emoji": "\U00002694",
        "blurb": "Heavy plate, big swings, soaks hits.",
        "hp_mult": 1.30, "atk_base": 7, "def_base": 3, "spd_base": 0.45, "int_base": 0,
        "weapon_types": ("longsword", "shortsword", "axe", "mace"),
        "armor_types":  ("heavy",),
        "starter_weapon": "iron_shortsword",
        "starter_armor":  "chain_mail",
        "skill_key": "cleave", "skill_name": "Cleave",
        "skill_desc": "1.7x damage. 3-round cooldown.",
        "skill_mult": 1.70, "skill_auto_crit": False, "skill_cd": 3,
        "skill_kind": "melee",
    },
    "mage": {
        "key": "mage", "name": "Mage", "emoji": "\U0001F9D9",
        "blurb": "Glass cannon. Channels staves; fireball melts bosses.",
        "hp_mult": 0.85, "atk_base": 5, "def_base": 1, "spd_base": 0.55, "int_base": 9,
        "weapon_types": ("staff",),
        "armor_types":  ("light",),
        "starter_weapon": "novice_staff",
        "starter_armor":  "linen_robe",
        "skill_key": "fireball", "skill_name": "Fireball",
        "skill_desc": "2.2x damage scaled off Wisdom. 4-round cooldown.",
        "skill_mult": 2.20, "skill_auto_crit": False, "skill_cd": 4,
        "skill_kind": "spell",
    },
    "rogue": {
        "key": "rogue", "name": "Rogue", "emoji": "\U0001F5E1",
        "blurb": "Fast, dodgy, ruthless on first strike.",
        "hp_mult": 1.00, "atk_base": 6, "def_base": 2, "spd_base": 0.75, "int_base": 0,
        "weapon_types": ("shortsword",),
        "armor_types":  ("medium",),
        "starter_weapon": "iron_shortsword",
        "starter_armor":  "leather_jerkin",
        "skill_key": "backstab", "skill_name": "Backstab",
        "skill_desc": "2.5x damage and an automatic crit. 3-round cooldown.",
        "skill_mult": 2.50, "skill_auto_crit": True, "skill_cd": 3,
        "skill_kind": "melee",
    },
    "archer": {
        "key": "archer", "name": "Archer", "emoji": "\U0001F3F9",
        "blurb": "Strikes first from range. Bows + crossbows; medium leather.",
        "hp_mult": 0.95, "atk_base": 7, "def_base": 2, "spd_base": 0.70, "int_base": 0,
        "weapon_types": ("bow", "crossbow"),
        "armor_types":  ("medium",),
        "starter_weapon": "short_bow",
        "starter_armor":  "leather_jerkin",
        "skill_key": "volley", "skill_name": "Volley",
        "skill_desc": "Fires 3 arrows: 0.7x dmg each (~2.1x total) plus +15% crit. 4-round cooldown. Burns 3 ammo.",
        "skill_mult": 2.10, "skill_auto_crit": False, "skill_cd": 4,
        "skill_kind": "ranged",
    },
    "druid": {
        "key": "druid", "name": "Druid", "emoji": "\U0001F33F",
        "blurb": "Wild rod-channeller. Wildshape heals on the swing.",
        "hp_mult": 1.05, "atk_base": 4, "def_base": 2, "spd_base": 0.55, "int_base": 7,
        "weapon_types": ("rod",),
        "armor_types":  ("light",),
        "starter_weapon": "hawthorn_rod",
        "starter_armor":  "linen_robe",
        "skill_key": "wildshape", "skill_name": "Wildshape",
        "skill_desc": "Beast-form: 2.0x damage and heals 15% of max HP. 5-round cooldown.",
        "skill_mult": 2.00, "skill_auto_crit": False, "skill_cd": 5,
        "skill_kind": "spell",
    },
}


# ============================================================================
# Ability catalog
# ============================================================================
# Each class now has a tuple of THREE abilities (CLASS_ABILITIES below).
# The first entry is the legacy "skill_key" so existing combat paths
# (,delve skill, the old single Skill button) keep firing the same
# move. The two extras are picked by the in-combat ability buttons.
#
# Effect contract per ability:
#   name / emoji        -- UI surface
#   kind                -- "melee" | "ranged" | "spell"  (matches skill_kind)
#   mult                -- per-swing damage multiplier
#   swings              -- how many swings the ability fires (default 1)
#   auto_crit           -- swing always crits
#   cd                  -- rounds before re-fire
#   target              -- "mob" (default; deals damage) | "self" (heal/buff,
#                          no-attack)
#   heal_pct            -- self-heal as fraction of hp_max (target=self only)
#   stun_rounds         -- additional stun applied to mob on hit
#   mark_rounds         -- grants ``marked_target`` buff for N swings
#   def_pierce_pct      -- ignores this fraction of mob def for the swing
#   crit_bonus          -- adds flat crit chance for the swing
#   lifesteal_pct       -- one-shot lifesteal for the ability (additive on top
#                          of weapon-affix lifesteal)
#   ammo_cost           -- ammo burned per cast (ranged abilities only)
#   blurb               -- short tooltip for the bag panel / help text

ABILITIES: Final[dict[str, dict]] = {
    # ── Warrior ─────────────────────────────────────────────────────────
    "cleave": {
        "key": "cleave", "name": "Cleave", "emoji": "\U00002694",
        "kind": "melee", "mult": 1.70, "auto_crit": False, "cd": 3,
        "blurb": "1.7x damage in one heavy swing.",
    },
    "shield_bash": {
        "key": "shield_bash", "name": "Shield Bash", "emoji": "\U0001F6E1",
        "kind": "melee", "mult": 1.20, "auto_crit": False, "cd": 4,
        "stun_rounds": 1,
        "blurb": "1.2x damage and stuns the mob for 1 round.",
    },
    "whirlwind": {
        "key": "whirlwind", "name": "Whirlwind", "emoji": "\U0001F32A",
        "kind": "melee", "mult": 0.95, "swings": 3, "auto_crit": False, "cd": 5,
        "blurb": "3 swings at 0.95x each (~2.85x total).",
    },
    # ── Mage ────────────────────────────────────────────────────────────
    "fireball": {
        "key": "fireball", "name": "Fireball", "emoji": "\U0001F525",
        "kind": "spell", "mult": 2.20, "auto_crit": False, "cd": 4,
        "blurb": "2.2x damage scaled off Wisdom.",
    },
    "frostbolt": {
        "key": "frostbolt", "name": "Frostbolt", "emoji": "\U00002744",
        "kind": "spell", "mult": 1.50, "auto_crit": False, "cd": 3,
        "stun_rounds": 1,
        "blurb": "1.5x damage and freezes the mob for 1 round.",
    },
    "arcane_missile": {
        "key": "arcane_missile", "name": "Arcane Missile", "emoji": "\U00002728",
        "kind": "spell", "mult": 0.90, "swings": 3, "auto_crit": False, "cd": 5,
        "blurb": "3 missiles at 0.9x each (~2.7x total).",
    },
    # ── Rogue ───────────────────────────────────────────────────────────
    "backstab": {
        "key": "backstab", "name": "Backstab", "emoji": "\U0001F5E1",
        "kind": "melee", "mult": 2.50, "auto_crit": True, "cd": 3,
        "blurb": "2.5x damage and an automatic crit.",
    },
    "shadowstep": {
        "key": "shadowstep", "name": "Shadowstep", "emoji": "\U0001F575",
        "kind": "melee", "mult": 1.00, "auto_crit": False, "cd": 4,
        "mark_rounds": 2,
        "blurb": "1x hit; next 2 swings auto-crit.",
    },
    "poison_strike": {
        "key": "poison_strike", "name": "Poison Strike", "emoji": "\U0001F9EA",
        "kind": "melee", "mult": 1.60, "auto_crit": False, "cd": 4,
        "lifesteal_pct": 0.10,
        "blurb": "1.6x damage and lifesteals 10% of mob max HP on kill.",
    },
    # ── Archer ──────────────────────────────────────────────────────────
    "volley": {
        "key": "volley", "name": "Volley", "emoji": "\U0001F3F9",
        "kind": "ranged", "mult": 0.70, "swings": 3, "auto_crit": False, "cd": 4,
        "crit_bonus": 0.15, "ammo_cost": 3,
        "blurb": "3 arrows (~2.1x total) at +15% crit. Burns 3 ammo.",
    },
    "piercing_shot": {
        "key": "piercing_shot", "name": "Piercing Shot", "emoji": "\U0001F3AF",
        "kind": "ranged", "mult": 2.30, "auto_crit": False, "cd": 5,
        "def_pierce_pct": 0.50, "ammo_cost": 1,
        "blurb": "2.3x damage. Ignores 50% of mob defence.",
    },
    "aimed_shot": {
        "key": "aimed_shot", "name": "Aimed Shot", "emoji": "\U0001F3F9",
        "kind": "ranged", "mult": 1.60, "auto_crit": True, "cd": 3,
        "ammo_cost": 1,
        "blurb": "1.6x damage and auto-crit.",
    },
    # ── Druid ───────────────────────────────────────────────────────────
    "wildshape": {
        "key": "wildshape", "name": "Wildshape", "emoji": "\U0001F43E",
        "kind": "spell", "mult": 2.00, "auto_crit": False, "cd": 5,
        "heal_pct": 0.15,
        "blurb": "2.0x damage and heals 15% of max HP.",
    },
    "entangle": {
        "key": "entangle", "name": "Entangle", "emoji": "\U0001F33F",
        "kind": "spell", "mult": 0.80, "auto_crit": False, "cd": 3,
        "stun_rounds": 1,
        "blurb": "0.8x damage and roots the mob for 1 round.",
    },
    "regrowth": {
        "key": "regrowth", "name": "Regrowth", "emoji": "\U0001F33B",
        "kind": "spell", "mult": 0.0, "swings": 0, "cd": 4,
        "target": "self", "heal_pct": 0.30,
        "blurb": "Heal 30% of max HP. No attack swing this round.",
    },
}


CLASS_ABILITIES: Final[dict[str, tuple[str, ...]]] = {
    # Position 0 is the legacy "skill_key" -- mirrors CLASSES[*]['skill_key']
    # so old combat paths keep working.
    "warrior": ("cleave",   "shield_bash",   "whirlwind"),
    "mage":    ("fireball", "frostbolt",     "arcane_missile"),
    "rogue":   ("backstab", "shadowstep",    "poison_strike"),
    "archer":  ("volley",   "piercing_shot", "aimed_shot"),
    "druid":   ("wildshape", "entangle",     "regrowth"),
}


def ability_meta(key: str | None) -> dict | None:
    """Look up an ability meta dict by key, or None if not registered."""
    if not key:
        return None
    return ABILITIES.get(str(key).lower())


def class_abilities(class_key: str | None) -> tuple[str, ...]:
    """Return the tuple of ability keys available to a class.

    Empty tuple for unknown classes -- callers should defend with their
    class's existing fallback rather than crashing.
    """
    return CLASS_ABILITIES.get(str(class_key or "").lower(), ())


def ability_swings(ability: dict) -> int:
    """Return the swing count for an ability (default 1; 0 for self-target)."""
    if ability.get("target") == "self":
        return int(ability.get("swings", 0))
    return int(ability.get("swings", 1))


# ============================================================================
# Mob catalog
# ============================================================================
# Tier scaling baseline:
#   t1: hp ~18,  atk ~4
#   t2: hp ~32,  atk ~7   (~1.7x)
#   t3: hp ~55,  atk ~11  (~2.7x)
#   t4: hp ~95,  atk ~18  (~4.5x)
#   t5: hp ~180, atk ~32  (~7.5x; bosses)
MOBS: dict[str, dict] = {
    # ---- Tier 1 ----
    "goblin": {
        "name": "Goblin", "emoji": "\U0001F47A", "tier": 1,
        "hp_base": 18, "atk_base": 4, "def_base": 1, "spd_base": 0.50,
        "ascii": "g", "xp": 25,
        "ore_drop": COPPER_SYMBOL, "ore_qty": 2.0, "rune_drop": 0.0,
        "blurb": "Sneaky, weak, abundant.",
    },
    "kobold": {
        "name": "Kobold", "emoji": "\U0001F98E", "tier": 1,
        "hp_base": 16, "atk_base": 5, "def_base": 1, "spd_base": 0.55,
        "ascii": "k", "xp": 28,
        "ore_drop": COPPER_SYMBOL, "ore_qty": 2.0, "rune_drop": 0.0,
        "blurb": "Yipping pack-fighter.",
    },
    "giant_rat": {
        "name": "Giant Rat", "emoji": "\U0001F400", "tier": 1,
        "hp_base": 22, "atk_base": 3, "def_base": 0, "spd_base": 0.60,
        "ascii": "r", "xp": 22,
        "ore_drop": None, "ore_qty": 0.0, "rune_drop": 0.0,
        "blurb": "Diseased, twitchy, fast.",
    },
    # ---- Tier 2 ----
    "skeleton": {
        "name": "Skeleton", "emoji": "\U0001F480", "tier": 2,
        "hp_base": 32, "atk_base": 7, "def_base": 2, "spd_base": 0.50,
        "ascii": "s", "xp": 55,
        "ore_drop": SILVER_SYMBOL, "ore_qty": 1.0, "rune_drop": 0.0,
        "blurb": "Brittle bones, sharp blade.",
        "tags": ("undead",),
    },
    "bat": {
        "name": "Cave Bat", "emoji": "\U0001F987", "tier": 2,
        "hp_base": 28, "atk_base": 6, "def_base": 1, "spd_base": 0.80,
        "ascii": "b", "xp": 50,
        "ore_drop": None, "ore_qty": 0.0, "rune_drop": 0.0,
        "blurb": "Hard to hit, soft to kill.",
    },
    "slime": {
        "name": "Slime", "emoji": "\U0001F7E2", "tier": 2,
        "hp_base": 40, "atk_base": 5, "def_base": 3, "spd_base": 0.40,
        "ascii": "S", "xp": 60,
        "ore_drop": COPPER_SYMBOL, "ore_qty": 4.0, "rune_drop": 0.0,
        "blurb": "Soaks damage. Slow as paint.",
    },
    # ---- Tier 3 ----
    "ghoul": {
        "name": "Ghoul", "emoji": "\U0001F9DF", "tier": 3,
        "hp_base": 55, "atk_base": 11, "def_base": 2, "spd_base": 0.55,
        "ascii": "G", "xp": 110,
        "ore_drop": SILVER_SYMBOL, "ore_qty": 2.0, "rune_drop": 0.5,
        "blurb": "Hungry. Always hungry.",
        "tags": ("undead",),
    },
    "spider": {
        "name": "Cave Spider", "emoji": "\U0001F577", "tier": 3,
        "hp_base": 48, "atk_base": 12, "def_base": 1, "spd_base": 0.70,
        "ascii": "x", "xp": 115,
        "ore_drop": None, "ore_qty": 0.0, "rune_drop": 0.5,
        "blurb": "Eight legs, zero hesitation.",
    },
    "kobold_shaman": {
        "name": "Kobold Shaman", "emoji": "\U0001F9D9", "tier": 3,
        "hp_base": 50, "atk_base": 13, "def_base": 2, "spd_base": 0.55,
        "ascii": "K", "xp": 120,
        "ore_drop": SILVER_SYMBOL, "ore_qty": 2.5, "rune_drop": 1.0,
        "blurb": "Channels rancid power.",
    },
    # ---- Tier 4 ----
    "wraith": {
        "name": "Wraith", "emoji": "\U0001F47B", "tier": 4,
        "hp_base": 90, "atk_base": 17, "def_base": 3, "spd_base": 0.65,
        "ascii": "w", "xp": 220,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 0.5, "rune_drop": 1.5,
        "blurb": "Half-real. Wholly hostile.",
        "tags": ("undead",),
    },
    "troll": {
        "name": "Troll", "emoji": "\U0001F9CC", "tier": 4,
        "hp_base": 110, "atk_base": 19, "def_base": 5, "spd_base": 0.45,
        "ascii": "T", "xp": 240,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 0.6, "rune_drop": 2.0,
        "blurb": "Regenerates between rounds.",
    },
    # ---- Tier 4/5 bosses ----
    "ogre_lord": {
        "name": "Ogre Lord", "emoji": "\U0001F479", "tier": 4,
        "hp_base": 160, "atk_base": 22, "def_base": 4, "spd_base": 0.45,
        "ascii": "O", "xp": 600,
        "ore_drop": SILVER_SYMBOL, "ore_qty": 12.0, "rune_drop": 5.0,
        "blurb": "Floor 5 boss. Hits like a wagon.",
        "boss": True,
    },
    "lich": {
        "name": "Lich", "emoji": "\U00002620", "tier": 5,
        "hp_base": 220, "atk_base": 30, "def_base": 6, "spd_base": 0.55,
        "ascii": "L", "xp": 1500,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 5.0, "rune_drop": 15.0,
        "blurb": "Floor 10 boss. Phylactery still in the floor below.",
        "boss": True,
        "tags": ("undead",),
    },
    "dragon": {
        "name": "Wyrm", "emoji": "\U0001F409", "tier": 5,
        "hp_base": 320, "atk_base": 38, "def_base": 8, "spd_base": 0.55,
        "ascii": "D", "xp": 4000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 12.0, "rune_drop": 50.0,
        "blurb": "Floor 15 boss. Hoard included.",
        "boss": True,
    },
    "ancient_one": {
        "name": "Ancient One", "emoji": "\U0001F47E", "tier": 5,
        "hp_base": 600, "atk_base": 50, "def_base": 12, "spd_base": 0.60,
        "ascii": "A", "xp": 12000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 25.0, "rune_drop": 200.0,
        "blurb": "Floor 20 'final' boss. Older than the dungeon itself.",
        "boss": True,
    },
    # ── Deep-floor bosses (F25..F40) ────────────────────────────────────
    # Power scaling above the F20 ancient_one. Each new boss roughly
    # doubles the previous tier's HP + ATK so the depth-scale plus
    # tier already pushes the difficulty curve where players who
    # already cleared F20 want to keep grinding.
    "abyssal_titan": {
        "name": "Abyssal Titan", "emoji": "\U0001F40B", "tier": 5,
        "hp_base": 1100, "atk_base": 72, "def_base": 18, "spd_base": 0.45,
        "ascii": "T", "xp": 28000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 50.0, "rune_drop": 600.0,
        "blurb": "Floor 25 boss. A whale-sized leviathan that swims the void.",
        "boss": True,
    },
    "phoenix_lord": {
        "name": "Phoenix Lord", "emoji": "\U0001F526", "tier": 5,
        "hp_base": 1900, "atk_base": 105, "def_base": 22, "spd_base": 0.70,
        "ascii": "P", "xp": 60000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 100.0, "rune_drop": 1500.0,
        "blurb": "Floor 30 boss. Burns hot enough to relight the sun.",
        "boss": True,
    },
    "void_warden": {
        "name": "Void Warden", "emoji": "\U0001F300", "tier": 5,
        "hp_base": 3200, "atk_base": 150, "def_base": 30, "spd_base": 0.65,
        "ascii": "V", "xp": 130000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 200.0, "rune_drop": 4000.0,
        "blurb": "Floor 35 boss. Keeps the seal between dungeons closed.",
        "boss": True,
    },
    "the_archon": {
        "name": "The Archon", "emoji": "\U0001F451", "tier": 5,
        "hp_base": 5500, "atk_base": 220, "def_base": 45, "spd_base": 0.75,
        "ascii": "@", "xp": 300000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 500.0, "rune_drop": 12000.0,
        "blurb": "Floor 40 boss. Built the dungeon, owns the keys.",
        "boss": True,
    },
    # ── F45..F100 elder bosses (each ~1.6x previous tier) ──────────────
    "world_serpent": {
        "name": "World Serpent", "emoji": "\U0001F40D", "tier": 5,
        "hp_base": 9000, "atk_base": 320, "def_base": 60, "spd_base": 0.55,
        "ascii": "S", "xp": 500000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 800.0, "rune_drop": 25000.0,
        "blurb": "Floor 45 boss. Coiled around the dungeon's roots.",
        "boss": True,
    },
    "celestial_judge": {
        "name": "Celestial Judge", "emoji": "\U00002696", "tier": 5,
        "hp_base": 14000, "atk_base": 460, "def_base": 80, "spd_base": 0.65,
        "ascii": "J", "xp": 800000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 1200.0, "rune_drop": 50000.0,
        "blurb": "Floor 50 boss. Weighs the worth of every delver.",
        "boss": True,
    },
    "obsidian_giant": {
        "name": "Obsidian Giant", "emoji": "\U0001F5FB", "tier": 5,
        "hp_base": 22000, "atk_base": 640, "def_base": 110, "spd_base": 0.40,
        "ascii": "O", "xp": 1300000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 1800.0, "rune_drop": 100000.0,
        "blurb": "Floor 55 boss. Each fist is a mountain in motion.",
        "boss": True,
    },
    "moonflame_seraph": {
        "name": "Moonflame Seraph", "emoji": "\U0001F320", "tier": 5,
        "hp_base": 35000, "atk_base": 900, "def_base": 140, "spd_base": 0.80,
        "ascii": "F", "xp": 2000000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 2700.0, "rune_drop": 200000.0,
        "blurb": "Floor 60 boss. Wings of cold fire, voice of bells.",
        "boss": True,
    },
    "abyssal_shogun": {
        "name": "Abyssal Shogun", "emoji": "\U0001F5E1", "tier": 5,
        "hp_base": 55000, "atk_base": 1300, "def_base": 180, "spd_base": 0.75,
        "ascii": "X", "xp": 3000000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 4000.0, "rune_drop": 400000.0,
        "blurb": "Floor 65 boss. Commands every defeated mob below.",
        "boss": True,
    },
    "leviathan_god": {
        "name": "Leviathan God", "emoji": "\U0001F30A", "tier": 5,
        "hp_base": 90000, "atk_base": 1900, "def_base": 240, "spd_base": 0.55,
        "ascii": "L", "xp": 5000000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 6000.0, "rune_drop": 800000.0,
        "blurb": "Floor 70 boss. The deep itself, given a face.",
        "boss": True,
    },
    "nightmare_king": {
        "name": "Nightmare King", "emoji": "\U0001F47A", "tier": 5,
        "hp_base": 150000, "atk_base": 2800, "def_base": 320, "spd_base": 0.70,
        "ascii": "N", "xp": 8000000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 9000.0, "rune_drop": 1500000.0,
        "blurb": "Floor 75 boss. Rules the part of you that cannot dream.",
        "boss": True,
    },
    "demiurge": {
        "name": "Demiurge", "emoji": "\U0001F441", "tier": 5,
        "hp_base": 240000, "atk_base": 4200, "def_base": 420, "spd_base": 0.85,
        "ascii": "D", "xp": 13000000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 14000.0, "rune_drop": 3000000.0,
        "blurb": "Floor 80 boss. Made the rules. Hates that you broke them.",
        "boss": True,
    },
    "void_emperor": {
        "name": "Void Emperor", "emoji": "\U0001F451", "tier": 5,
        "hp_base": 380000, "atk_base": 6200, "def_base": 560, "spd_base": 0.80,
        "ascii": "E", "xp": 21000000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 22000.0, "rune_drop": 6000000.0,
        "blurb": "Floor 85 boss. Sits on the throne the Archon set up.",
        "boss": True,
    },
    "primordial_chaos": {
        "name": "Primordial Chaos", "emoji": "\U0001F300", "tier": 5,
        "hp_base": 600000, "atk_base": 9000, "def_base": 720, "spd_base": 0.90,
        "ascii": "C", "xp": 35000000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 35000.0, "rune_drop": 12000000.0,
        "blurb": "Floor 90 boss. Older than language, hungrier than fire.",
        "boss": True,
    },
    "the_unmaker": {
        "name": "The Unmaker", "emoji": "\U0001F4A5", "tier": 5,
        "hp_base": 950000, "atk_base": 13000, "def_base": 950, "spd_base": 0.95,
        "ascii": "U", "xp": 55000000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 55000.0, "rune_drop": 25000000.0,
        "blurb": "Floor 95 boss. Erases anything it touches, including you.",
        "boss": True,
    },
    "the_first": {
        "name": "The First", "emoji": "\U00002728", "tier": 5,
        "hp_base": 1500000, "atk_base": 19000, "def_base": 1300, "spd_base": 1.0,
        "ascii": "*", "xp": 100000000,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 100000.0, "rune_drop": 60000000.0,
        "blurb": "Floor 100 TRUE-true final boss. The first thing that ever was.",
        "boss": True,
    },
    # ── New regular mobs for variety on deeper floors ──────────────────
    "shroom_imp": {
        "name": "Shroom Imp", "emoji": "\U0001F344", "tier": 1,
        "hp_base": 20, "atk_base": 4, "def_base": 1, "spd_base": 0.55,
        "ascii": "m", "xp": 24,
        "ore_drop": COPPER_SYMBOL, "ore_qty": 1.5, "rune_drop": 0.0,
        "blurb": "Spores in your face. Hands in your bag.",
    },
    "wisp": {
        "name": "Wisp", "emoji": "\U0001F4AB", "tier": 2,
        "hp_base": 28, "atk_base": 7, "def_base": 1, "spd_base": 0.85,
        "ascii": "*", "xp": 55,
        "ore_drop": SILVER_SYMBOL, "ore_qty": 1.0, "rune_drop": 0.0,
        "blurb": "Pretty. Painful.",
    },
    "minotaur": {
        "name": "Minotaur", "emoji": "\U0001F402", "tier": 3,
        "hp_base": 70, "atk_base": 13, "def_base": 3, "spd_base": 0.55,
        "ascii": "M", "xp": 130,
        "ore_drop": SILVER_SYMBOL, "ore_qty": 3.0, "rune_drop": 1.0,
        "blurb": "Knows every dead-end. None of them are dead-ends to him.",
    },
    "basilisk": {
        "name": "Basilisk", "emoji": "\U0001F432", "tier": 4,
        "hp_base": 95, "atk_base": 18, "def_base": 4, "spd_base": 0.55,
        "ascii": "b", "xp": 240,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 0.6, "rune_drop": 2.0,
        "blurb": "Don't make eye contact with it.",
    },
    "demon": {
        "name": "Demon", "emoji": "\U0001F608", "tier": 4,
        "hp_base": 105, "atk_base": 20, "def_base": 4, "spd_base": 0.65,
        "ascii": "d", "xp": 260,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 0.7, "rune_drop": 2.5,
        "blurb": "Speaks with your voice. Lies with your face.",
    },
    "lich_acolyte": {
        "name": "Lich Acolyte", "emoji": "\U0001F480", "tier": 4,
        "hp_base": 100, "atk_base": 22, "def_base": 5, "spd_base": 0.55,
        "ascii": "a", "xp": 270,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 0.8, "rune_drop": 3.0,
        "blurb": "Studied at the Lich's feet. Failed the final exam.",
        "tags": ("undead",),
    },
    "drake": {
        "name": "Drake", "emoji": "\U0001F409", "tier": 5,
        "hp_base": 200, "atk_base": 30, "def_base": 7, "spd_base": 0.65,
        "ascii": "k", "xp": 700,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 4.0, "rune_drop": 8.0,
        "blurb": "Smaller than the Wyrm. Faster, too.",
    },
    "banshee": {
        "name": "Banshee", "emoji": "\U0001F47B", "tier": 5,
        "hp_base": 160, "atk_base": 34, "def_base": 6, "spd_base": 0.80,
        "ascii": "h", "xp": 750,
        "ore_drop": GOLD_SYMBOL, "ore_qty": 4.0, "rune_drop": 9.0,
        "blurb": "Her scream cuts armour. Bring earplugs.",
        "tags": ("undead",),
    },
}


# ============================================================================
# Mini-boss catalog
# ============================================================================
# Mini-bosses are tougher named variants of regular mobs that spawn on
# combat rooms with MINI_BOSS_SPAWN_CHANCE, gated to MINI_BOSS_MIN_FLOOR
# upward and skipping main-boss floors. Stats inherit the mob_meta scaling
# (so ~2x HP and ~1.5x ATK over the parent at the same depth) plus a flat
# rune_drop kicker. Their distinguishing feature is a guaranteed roll on
# MINI_BOSS_LOOT -- usually a piece of rare/uncommon gear or rare junk.
#
# Each entry carries:
#   key, name, emoji, tier, hp_base, atk_base, def_base, spd_base, xp
#   floor_min / floor_max  -- the depth window this mini-boss can spawn in
#   parent      -- mob_key it visually descends from (used for tag inheritance)
#   loot_pool   -- which MINI_BOSS_LOOT bucket to roll on kill
#   blurb       -- flavour text shown in the encounter prompt
MINI_BOSS_MIN_FLOOR: Final[int] = 3
MINI_BOSS_MAX_FLOOR: Final[int] = 49
MINI_BOSS_SPAWN_CHANCE: Final[float] = 0.10  # per qualifying mob room

MINI_BOSSES: Final[dict[str, dict]] = {
    "goblin_king": {
        "key": "goblin_king",
        "name": "Goblin King", "emoji": "\U0001F451", "tier": 2,
        "hp_base": 60, "atk_base": 9, "def_base": 2, "spd_base": 0.55,
        "ascii": "G", "xp": 200,
        "rune_drop": 4.0, "ore_drop": COPPER_SYMBOL, "ore_qty": 8.0,
        "floor_min": 3, "floor_max": 12,
        "parent": "goblin", "loot_pool": "early",
        "blurb": "Crowned with a torn brass-buckle. Owns the pack.",
    },
    "rat_brood_mother": {
        "key": "rat_brood_mother",
        "name": "Rat Brood-Mother", "emoji": "\U0001F400", "tier": 2,
        "hp_base": 65, "atk_base": 8, "def_base": 1, "spd_base": 0.70,
        "ascii": "R", "xp": 220,
        "rune_drop": 5.0, "ore_drop": SILVER_SYMBOL, "ore_qty": 1.5,
        "floor_min": 3, "floor_max": 12,
        "parent": "giant_rat", "loot_pool": "early",
        "blurb": "Heavy with younglings. They chitter from her fur.",
    },
    "bone_captain": {
        "key": "bone_captain",
        "name": "Bone Captain", "emoji": "\U0001F480", "tier": 3,
        "hp_base": 110, "atk_base": 14, "def_base": 4, "spd_base": 0.55,
        "ascii": "B", "xp": 380,
        "rune_drop": 8.0, "ore_drop": SILVER_SYMBOL, "ore_qty": 4.0,
        "floor_min": 6, "floor_max": 18,
        "parent": "skeleton", "loot_pool": "mid",
        "tags": ("undead",),
        "blurb": "Wields the rapier of a long-dead sergeant.",
    },
    "spider_empress": {
        "key": "spider_empress",
        "name": "Spider Empress", "emoji": "\U0001F577", "tier": 3,
        "hp_base": 130, "atk_base": 16, "def_base": 3, "spd_base": 0.80,
        "ascii": "X", "xp": 420,
        "rune_drop": 10.0, "ore_drop": SILVER_SYMBOL, "ore_qty": 4.5,
        "floor_min": 8, "floor_max": 22,
        "parent": "spider", "loot_pool": "mid",
        "blurb": "Eight eyes, all on you.",
    },
    "shaman_elder": {
        "key": "shaman_elder",
        "name": "Shaman Elder", "emoji": "\U0001F9D9", "tier": 3,
        "hp_base": 120, "atk_base": 17, "def_base": 4, "spd_base": 0.60,
        "ascii": "K", "xp": 440,
        "rune_drop": 11.0, "ore_drop": SILVER_SYMBOL, "ore_qty": 5.0,
        "floor_min": 8, "floor_max": 22,
        "parent": "kobold_shaman", "loot_pool": "mid",
        "blurb": "Channels for the whole tribe. Eyes white in the trance.",
    },
    "troll_patriarch": {
        "key": "troll_patriarch",
        "name": "Troll Patriarch", "emoji": "\U0001F9CC", "tier": 4,
        "hp_base": 240, "atk_base": 26, "def_base": 7, "spd_base": 0.45,
        "ascii": "T", "xp": 700,
        "rune_drop": 18.0, "ore_drop": GOLD_SYMBOL, "ore_qty": 1.5,
        "floor_min": 14, "floor_max": 28,
        "parent": "troll", "loot_pool": "high",
        "blurb": "Sired half the trolls in this dungeon. Remembers each one.",
    },
    "wraith_lord": {
        "key": "wraith_lord",
        "name": "Wraith Lord", "emoji": "\U0001F47B", "tier": 4,
        "hp_base": 200, "atk_base": 26, "def_base": 6, "spd_base": 0.70,
        "ascii": "W", "xp": 720,
        "rune_drop": 22.0, "ore_drop": GOLD_SYMBOL, "ore_qty": 1.4,
        "floor_min": 16, "floor_max": 30,
        "parent": "wraith", "loot_pool": "high",
        "tags": ("undead",),
        "blurb": "Half-real and twice the malice. Phases between rounds.",
    },
    "demon_warlock": {
        "key": "demon_warlock",
        "name": "Demon Warlock", "emoji": "\U0001F608", "tier": 4,
        "hp_base": 220, "atk_base": 30, "def_base": 6, "spd_base": 0.65,
        "ascii": "d", "xp": 760,
        "rune_drop": 26.0, "ore_drop": GOLD_SYMBOL, "ore_qty": 1.6,
        "floor_min": 18, "floor_max": 34,
        "parent": "demon", "loot_pool": "high",
        "blurb": "Bound to a contract older than its name. Knows yours.",
    },
    "drake_alpha": {
        "key": "drake_alpha",
        "name": "Drake Alpha", "emoji": "\U0001F409", "tier": 5,
        "hp_base": 380, "atk_base": 42, "def_base": 9, "spd_base": 0.65,
        "ascii": "K", "xp": 1300,
        "rune_drop": 50.0, "ore_drop": GOLD_SYMBOL, "ore_qty": 6.0,
        "floor_min": 24, "floor_max": 40,
        "parent": "drake", "loot_pool": "deep",
        "blurb": "Larger than the wyrmlings. Smaller than its rage.",
    },
    "banshee_queen": {
        "key": "banshee_queen",
        "name": "Banshee Queen", "emoji": "\U0001F47B", "tier": 5,
        "hp_base": 320, "atk_base": 48, "def_base": 8, "spd_base": 0.85,
        "ascii": "h", "xp": 1400,
        "rune_drop": 60.0, "ore_drop": GOLD_SYMBOL, "ore_qty": 6.5,
        "floor_min": 28, "floor_max": 46,
        "parent": "banshee", "loot_pool": "deep",
        "tags": ("undead",),
        "blurb": "Her scream is the song of a thousand widows.",
    },
}


def mini_boss_meta(key: str | None) -> dict | None:
    """Return the mini-boss meta dict for a key, or None if not found."""
    if not key:
        return None
    return MINI_BOSSES.get(str(key).lower())


def pick_mini_boss_for_floor(
    floor: int, rng: random.Random,
) -> str | None:
    """Pick a mini-boss key whose floor window includes ``floor``.

    Returns None if no mini-boss is eligible at this depth (e.g. floor
    too shallow / too deep). Caller must already have decided that a
    mini-boss is going to spawn -- this just picks WHICH one.
    """
    eligible = [
        k for k, meta in MINI_BOSSES.items()
        if int(meta.get("floor_min", 0)) <= floor <= int(meta.get("floor_max", MAX_FLOOR))
    ]
    if not eligible:
        return None
    return rng.choice(eligible)


def should_spawn_mini_boss(
    floor: int, is_boss_floor: bool, rng: random.Random,
) -> bool:
    """Roll the per-mob-room mini-boss spawn chance.

    Skips floors below MINI_BOSS_MIN_FLOOR and any boss floor (the boss
    room slot is reserved for the floor's main boss). Returns True if a
    mini-boss should replace the rolled mob.
    """
    if is_boss_floor:
        return False
    if floor < MINI_BOSS_MIN_FLOOR or floor > MINI_BOSS_MAX_FLOOR:
        return False
    return rng.random() < MINI_BOSS_SPAWN_CHANCE


# ============================================================================
# Boss & mini-boss loot tables
# ============================================================================
# After a confirmed kill of a mob with ``boss=True`` or ``mini_boss=True``,
# resolve_attack rolls one extra drop on the appropriate table here. The
# pools reference WEAPONS / ARMOR / JUNK keys so adding new delve-only
# items in Phase 3 is just a matter of editing this table.
#
# Pools per loot bucket:
#   ``early``  -- F3..F12 mini-bosses
#   ``mid``    -- F6..F22 mini-bosses
#   ``high``   -- F14..F34 mini-bosses
#   ``deep``   -- F24..F46 mini-bosses
#   ``f5``..``f100``  -- per-main-boss tables, keyed by FLOOR_BOSS_LOOT_KEY
#
# Each pool entry is ``(key, weight, kind)`` where ``kind`` is one of
# ``weapon`` / ``armor`` / ``junk``. Roll picks ONE entry by weight; if
# the player already owns the rolled gear (weapons_owned / armor_owned),
# the kill credits a fallback junk drop instead so the kill always pays
# something tangible.

# Fallback junk for "you already own that gear" cases. Always rare or
# better so the consolation prize still feels earned.
_LOOT_FALLBACK_JUNK_KEYS: tuple[str, ...] = (
    "glowing_crystal", "dragon_scale",
)

MINI_BOSS_LOOT: Final[dict[str, tuple[tuple[str, float, str], ...]]] = {
    "early": (
        # Uncommon early-floor gear -- usable from F3 onward.
        ("iron_shortsword",   18.0, "weapon"),
        ("steel_longsword",   15.0, "weapon"),
        ("iron_mace",         12.0, "weapon"),
        ("light_crossbow",    12.0, "weapon"),
        ("studded_leather",   15.0, "armor"),
        ("chain_mail",        14.0, "armor"),
        ("scale_brigandine",   8.0, "armor"),
        # Delve-only rare drops -- 8% chance window for a real reward.
        ("boneblade",          8.0, "weapon"),
        ("boneplate",          6.0, "armor"),
        # Junk variety on the kill ticket.
        ("glowing_crystal",   12.0, "junk"),
        ("monster_fang",      14.0, "junk"),
        ("enchanted_thread",   8.0, "junk"),
        ("beast_blood",        8.0, "junk"),
    ),
    "mid": (
        ("silvered_dirk",     12.0, "weapon"),
        ("silvered_blade",    12.0, "weapon"),
        ("rune_axe",           8.0, "weapon"),
        ("recurve_bow",       10.0, "weapon"),
        ("arbalest",           8.0, "weapon"),
        ("oaken_staff",        8.0, "weapon"),
        ("scale_brigandine",  10.0, "armor"),
        ("ranger_garb",       10.0, "armor"),
        ("rune_plate",         8.0, "armor"),
        ("silk_robe",          8.0, "armor"),
        # Delve-only rare drops.
        ("ghoul_cleaver",      8.0, "weapon"),
        ("spell_blade",        8.0, "weapon"),
        ("shadow_cloak",       6.0, "armor"),
        # Junk.
        ("glowing_crystal",   14.0, "junk"),
        ("dragon_scale",      10.0, "junk"),
        ("runed_chip",         9.0, "junk"),
        ("warding_charm",      4.0, "junk"),
    ),
    "high": (
        ("shadowstrike",       6.0, "weapon"),
        ("rune_axe",           8.0, "weapon"),
        ("warbow",             8.0, "weapon"),
        ("heavy_crossbow",     8.0, "weapon"),
        ("stormwood_staff",    7.0, "weapon"),
        ("wildwood_rod",       7.0, "weapon"),
        ("dragon_plate",       8.0, "armor"),
        ("wyrm_hide",          8.0, "armor"),
        ("spell_silk_robe",    8.0, "armor"),
        # Delve-only epic drops -- the high-end of mini-boss pool.
        ("frost_dagger",       6.0, "weapon"),
        ("shadow_bow",         6.0, "weapon"),
        ("voidstaff_relic",    5.0, "weapon"),
        ("vampiric_mail",      5.0, "armor"),
        ("phoenix_garb",       5.0, "armor"),
        # Junk.
        ("dragon_scale",      14.0, "junk"),
        ("mana_dust",         10.0, "junk"),
        ("phoenix_feather",    5.0, "junk"),
        ("blink_dust",         4.0, "junk"),
    ),
    "deep": (
        ("moonlit_kris",       6.0, "weapon"),
        ("mythril_sword",      8.0, "weapon"),
        ("morningstar",        6.0, "weapon"),
        ("elven_longbow",      7.0, "weapon"),
        ("rune_crossbow",      7.0, "weapon"),
        ("arcane_staff",       7.0, "weapon"),
        ("yew_rod",            7.0, "weapon"),
        ("mythril_plate",      8.0, "armor"),
        ("drake_scale",        8.0, "armor"),
        ("enchanter_robe",     8.0, "armor"),
        # Delve-only epic+ drops.
        ("thornlash_relic",    6.0, "weapon"),
        ("dragonfang_dirk",    3.0, "weapon"),
        ("archmage_robe",      5.0, "armor"),
        ("phoenix_garb",       5.0, "armor"),
        # Junk -- the rare kicker tier.
        ("dragon_scale",      14.0, "junk"),
        ("phoenix_feather",    8.0, "junk"),
        ("dragon_heart_fragment", 4.0, "junk"),
        ("void_essence",       6.0, "junk"),
        ("elder_potion",       3.0, "junk"),
    ),
}

# Main-boss tables keyed off the mob.key of the boss. Each main boss has
# a richer drop pool than its mini-boss counterpart -- including the
# legendary-tier delve-only items, which only the main-floor bosses can
# drop. Bosses that don't have an entry fall back on the closest-tier
# MINI_BOSS_LOOT bucket.
BOSS_LOOT: Final[dict[str, tuple[tuple[str, float, str], ...]]] = {
    "ogre_lord": MINI_BOSS_LOOT["mid"] + (
        ("morningstar",         6.0, "weapon"),
        ("mythril_plate",       6.0, "armor"),
        ("frost_dagger",        4.0, "weapon"),
        ("vampiric_mail",       3.0, "armor"),
    ),
    "lich": MINI_BOSS_LOOT["high"] + (
        ("astral_cleaver",      6.0, "weapon"),
        ("nightshade_blade",    6.0, "weapon"),
        ("starwoven_robe",      6.0, "armor"),
        ("voidstaff_relic",     5.0, "weapon"),
        ("archmage_robe",       4.0, "armor"),
        ("dragon_heart_fragment", 4.0, "junk"),
    ),
    "dragon": MINI_BOSS_LOOT["deep"] + (
        ("phoenix_talon",       6.0, "weapon"),
        ("phoenix_mail",        6.0, "armor"),
        ("phoenix_hide",        6.0, "armor"),
        ("dragonfang_dirk",     4.0, "weapon"),
        ("phoenix_garb",        4.0, "armor"),
        ("dragon_heart_fragment", 5.0, "junk"),
    ),
    "ancient_one": MINI_BOSS_LOOT["deep"] + (
        ("abyssal_dirk",        6.0, "weapon"),
        ("void_blade",          6.0, "weapon"),
        ("nebula_robe",         6.0, "armor"),
        ("void_plate",          6.0, "armor"),
        ("dragonfang_dirk",     5.0, "weapon"),
        ("void_aegis",          3.0, "armor"),
        ("void_essence",        6.0, "junk"),
    ),
}


def boss_loot_pool(mob_key: str | None) -> tuple[tuple[str, float, str], ...] | None:
    """Return the BOSS_LOOT bucket for a main-boss key, or None if not defined."""
    if not mob_key:
        return None
    return BOSS_LOOT.get(str(mob_key).lower())


def mini_boss_loot_pool(pool_key: str | None) -> tuple[tuple[str, float, str], ...] | None:
    """Return the MINI_BOSS_LOOT bucket for a pool key (early/mid/high/deep)."""
    if not pool_key:
        return None
    return MINI_BOSS_LOOT.get(str(pool_key).lower())


def roll_loot_table(
    pool: tuple[tuple[str, float, str], ...] | None,
    rng: random.Random,
) -> tuple[str, str] | None:
    """Pick one (item_key, kind) from a loot pool by weight.

    Returns None if the pool is empty / missing. ``kind`` is
    ``"weapon"`` / ``"armor"`` / ``"junk"`` so the caller knows which
    catalog + inventory column to write into.
    """
    if not pool:
        return None
    keys = [p[0] for p in pool]
    weights = [float(p[1]) for p in pool]
    kinds = [p[2] for p in pool]
    if sum(weights) <= 0:
        return None
    pick = rng.choices(range(len(pool)), weights=weights, k=1)[0]
    return keys[pick], kinds[pick]


def loot_fallback_junk(rng: random.Random) -> str:
    """Pick a high-tier junk key as a 'you already own that gear' consolation."""
    return rng.choice(_LOOT_FALLBACK_JUNK_KEYS)


# ============================================================================
# Floor catalog
# ============================================================================
# Each floor has 5-8 rooms; the boss caps a floor when set. Themes drive
# the embed accent color. Mob/ore pools shift with depth so floor 1 is
# all-copper goblins and floor 19 is wyrm-flavoured gold caves.
_C_STONE:  Final[int] = 0x95A5A6
_C_COPPER: Final[int] = 0xCD7F32
_C_SILVER: Final[int] = 0xC0C0C0
_C_GOLD:   Final[int] = 0xFFD700
_C_BOSS:   Final[int] = 0x8B0000

FLOORS: dict[int, dict] = {
    1: {"depth": 1, "name": "Crumbling Antechamber", "theme": "stone", "rooms": 5,
        "mob_pool": ("goblin", "kobold", "giant_rat"),
        "mob_pool_weights": (4, 3, 3),
        "ore_pool": (COPPER_SYMBOL,), "ore_pool_weights": (1,),
        "boss": None, "color_hex": _C_STONE},
    2: {"depth": 2, "name": "Damp Tunnels", "theme": "stone", "rooms": 6,
        "mob_pool": ("goblin", "kobold", "giant_rat"),
        "mob_pool_weights": (4, 4, 2),
        "ore_pool": (COPPER_SYMBOL,), "ore_pool_weights": (1,),
        "boss": None, "color_hex": _C_COPPER},
    3: {"depth": 3, "name": "Goblin Warren", "theme": "stone", "rooms": 6,
        "mob_pool": ("goblin", "kobold", "skeleton"),
        "mob_pool_weights": (4, 3, 1),
        "ore_pool": (COPPER_SYMBOL,), "ore_pool_weights": (1,),
        "boss": None, "color_hex": _C_COPPER},
    4: {"depth": 4, "name": "Bone Pits", "theme": "stone", "rooms": 7,
        "mob_pool": ("kobold", "skeleton", "bat"),
        "mob_pool_weights": (3, 3, 2),
        "ore_pool": (COPPER_SYMBOL, SILVER_SYMBOL), "ore_pool_weights": (4, 1),
        "boss": None, "color_hex": _C_COPPER},
    5: {"depth": 5, "name": "Hall of the Ogre Lord", "theme": "boss", "rooms": 4,
        "mob_pool": ("skeleton", "bat", "slime"),
        "mob_pool_weights": (3, 2, 2),
        "ore_pool": (COPPER_SYMBOL, SILVER_SYMBOL), "ore_pool_weights": (3, 2),
        "boss": "ogre_lord", "color_hex": _C_BOSS},
    6: {"depth": 6, "name": "Slime Sumps", "theme": "silver", "rooms": 6,
        "mob_pool": ("slime", "skeleton", "bat", "spider"),
        "mob_pool_weights": (3, 3, 2, 1),
        "ore_pool": (SILVER_SYMBOL, COPPER_SYMBOL), "ore_pool_weights": (3, 2),
        "boss": None, "color_hex": _C_SILVER},
    7: {"depth": 7, "name": "Forgotten Crypts", "theme": "silver", "rooms": 6,
        "mob_pool": ("skeleton", "ghoul", "spider"),
        "mob_pool_weights": (3, 2, 2),
        "ore_pool": (SILVER_SYMBOL, COPPER_SYMBOL), "ore_pool_weights": (3, 1),
        "boss": None, "color_hex": _C_SILVER},
    8: {"depth": 8, "name": "Spider Glades", "theme": "silver", "rooms": 7,
        "mob_pool": ("spider", "ghoul", "kobold_shaman"),
        "mob_pool_weights": (3, 3, 2),
        "ore_pool": (SILVER_SYMBOL,), "ore_pool_weights": (1,),
        "boss": None, "color_hex": _C_SILVER},
    9: {"depth": 9, "name": "Hollow Catacombs", "theme": "silver", "rooms": 7,
        "mob_pool": ("ghoul", "kobold_shaman", "wraith"),
        "mob_pool_weights": (3, 2, 1),
        "ore_pool": (SILVER_SYMBOL, GOLD_SYMBOL), "ore_pool_weights": (5, 1),
        "boss": None, "color_hex": _C_SILVER},
    10: {"depth": 10, "name": "Lich's Sanctum", "theme": "boss", "rooms": 4,
         "mob_pool": ("ghoul", "wraith", "kobold_shaman"),
         "mob_pool_weights": (2, 2, 2),
         "ore_pool": (SILVER_SYMBOL, GOLD_SYMBOL), "ore_pool_weights": (3, 2),
         "boss": "lich", "color_hex": _C_BOSS},
    11: {"depth": 11, "name": "Ashen Halls", "theme": "gold", "rooms": 6,
         "mob_pool": ("wraith", "ghoul", "spider"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL, SILVER_SYMBOL), "ore_pool_weights": (2, 3),
         "boss": None, "color_hex": _C_GOLD},
    12: {"depth": 12, "name": "Sundered Vaults", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll", "ghoul"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL, SILVER_SYMBOL), "ore_pool_weights": (2, 2),
         "boss": None, "color_hex": _C_GOLD},
    13: {"depth": 13, "name": "Trollmarsh", "theme": "gold", "rooms": 7,
         "mob_pool": ("troll", "wraith", "spider"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    14: {"depth": 14, "name": "Drakeshell Pass", "theme": "gold", "rooms": 7,
         "mob_pool": ("troll", "wraith", "kobold_shaman"),
         "mob_pool_weights": (3, 3, 1),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    15: {"depth": 15, "name": "Wyrm's Roost", "theme": "boss", "rooms": 4,
         "mob_pool": ("troll", "wraith"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "dragon", "color_hex": _C_BOSS},
    16: {"depth": 16, "name": "Glittering Deep", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll", "ghoul"),
         "mob_pool_weights": (3, 3, 1),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    17: {"depth": 17, "name": "Mirrorhall", "theme": "gold", "rooms": 7,
         "mob_pool": ("troll", "wraith", "kobold_shaman"),
         "mob_pool_weights": (3, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    18: {"depth": 18, "name": "Black Vault", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    19: {"depth": 19, "name": "Outer Sanctum", "theme": "gold", "rooms": 8,
         "mob_pool": ("wraith", "troll", "kobold_shaman"),
         "mob_pool_weights": (3, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    20: {"depth": 20, "name": "Throne of the Ancient One", "theme": "boss", "rooms": 4,
         "mob_pool": ("troll", "wraith"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "ancient_one", "color_hex": _C_BOSS},
    # ── Deep tier (F21..F40): post-Ancient-One difficulty ──────────────
    # Mob pool stays t4-t5 but the depth-scale (1.0 + 0.07 * (depth-1))
    # pushes raw stats roughly 2.4x at F20 -> 3.7x at F40. Bosses at
    # 25 / 30 / 35 / 40 roughly double each tier so the curve stays
    # painful no matter how geared the player is.
    21: {"depth": 21, "name": "Sunken Cathedral", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll", "ghoul"),
         "mob_pool_weights": (3, 3, 1),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    22: {"depth": 22, "name": "Drowned Temple", "theme": "gold", "rooms": 7,
         "mob_pool": ("troll", "wraith", "kobold_shaman"),
         "mob_pool_weights": (3, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    23: {"depth": 23, "name": "Tide Caverns", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    24: {"depth": 24, "name": "Leviathan Approach", "theme": "gold", "rooms": 8,
         "mob_pool": ("troll", "wraith", "kobold_shaman"),
         "mob_pool_weights": (3, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    25: {"depth": 25, "name": "Trench of the Titan", "theme": "boss", "rooms": 4,
         "mob_pool": ("troll", "wraith"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "abyssal_titan", "color_hex": _C_BOSS},
    26: {"depth": 26, "name": "Magma Veins", "theme": "gold", "rooms": 7,
         "mob_pool": ("troll", "wraith"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    27: {"depth": 27, "name": "Embered Halls", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll", "ghoul"),
         "mob_pool_weights": (3, 3, 1),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    28: {"depth": 28, "name": "Lava Chamber", "theme": "gold", "rooms": 7,
         "mob_pool": ("troll", "wraith"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    29: {"depth": 29, "name": "Pyre Approach", "theme": "gold", "rooms": 8,
         "mob_pool": ("wraith", "troll", "kobold_shaman"),
         "mob_pool_weights": (3, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    30: {"depth": 30, "name": "Pyre of the Phoenix", "theme": "boss", "rooms": 4,
         "mob_pool": ("wraith", "troll"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "phoenix_lord", "color_hex": _C_BOSS},
    31: {"depth": 31, "name": "Void Approach", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    32: {"depth": 32, "name": "Riftspine", "theme": "gold", "rooms": 7,
         "mob_pool": ("troll", "wraith", "ghoul"),
         "mob_pool_weights": (3, 3, 1),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    33: {"depth": 33, "name": "Hollow Eye", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    34: {"depth": 34, "name": "Star-Black Vault", "theme": "gold", "rooms": 8,
         "mob_pool": ("troll", "wraith", "kobold_shaman"),
         "mob_pool_weights": (3, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    35: {"depth": 35, "name": "Seal of the Void Warden", "theme": "boss", "rooms": 4,
         "mob_pool": ("wraith", "troll"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "void_warden", "color_hex": _C_BOSS},
    36: {"depth": 36, "name": "Throne Antechamber", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    37: {"depth": 37, "name": "Shattered Sky", "theme": "gold", "rooms": 7,
         "mob_pool": ("troll", "wraith"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    38: {"depth": 38, "name": "Architect's Garden", "theme": "gold", "rooms": 7,
         "mob_pool": ("wraith", "troll", "kobold_shaman"),
         "mob_pool_weights": (3, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    39: {"depth": 39, "name": "Final Stair", "theme": "gold", "rooms": 8,
         "mob_pool": ("troll", "wraith"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    40: {"depth": 40, "name": "Throne of the Archon", "theme": "boss", "rooms": 4,
         "mob_pool": ("wraith", "troll"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "the_archon", "color_hex": _C_BOSS},
    # ── Endgame tier (F41..F100) ────────────────────────────────────────
    # Boss every 5 floors. Mob pools blend the new variety mobs (drake,
    # banshee, basilisk, demon, lich_acolyte, minotaur) on top of the
    # classic troll / wraith / kobold_shaman so the deeper grind feels
    # different from the F1..F20 climb. Depth-scale (1.0 + 0.07 * depth-1)
    # plus the bigger boss HP keeps difficulty climbing.
    41: {"depth": 41, "name": "Wyrmhall", "theme": "gold", "rooms": 7,
         "mob_pool": ("drake", "wraith", "troll"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    42: {"depth": 42, "name": "Hollow Spire", "theme": "gold", "rooms": 7,
         "mob_pool": ("banshee", "wraith", "drake"),
         "mob_pool_weights": (2, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    43: {"depth": 43, "name": "Oubliette", "theme": "gold", "rooms": 7,
         "mob_pool": ("demon", "drake", "wraith"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    44: {"depth": 44, "name": "Serpent's Coil", "theme": "gold", "rooms": 8,
         "mob_pool": ("basilisk", "demon", "drake"),
         "mob_pool_weights": (2, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    45: {"depth": 45, "name": "Lair of the World Serpent", "theme": "boss", "rooms": 4,
         "mob_pool": ("basilisk", "drake"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "world_serpent", "color_hex": _C_BOSS},
    46: {"depth": 46, "name": "Court of the Judges", "theme": "gold", "rooms": 7,
         "mob_pool": ("lich_acolyte", "wraith", "demon"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    47: {"depth": 47, "name": "Halls of Judgment", "theme": "gold", "rooms": 7,
         "mob_pool": ("lich_acolyte", "banshee", "wraith"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    48: {"depth": 48, "name": "Scales of the Judge", "theme": "gold", "rooms": 7,
         "mob_pool": ("lich_acolyte", "demon", "drake"),
         "mob_pool_weights": (2, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    49: {"depth": 49, "name": "Verdict Hallway", "theme": "gold", "rooms": 8,
         "mob_pool": ("demon", "lich_acolyte"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    50: {"depth": 50, "name": "Bench of the Celestial Judge", "theme": "boss", "rooms": 4,
         "mob_pool": ("lich_acolyte", "demon"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "celestial_judge", "color_hex": _C_BOSS},
    51: {"depth": 51, "name": "Volcanic Threshold", "theme": "gold", "rooms": 7,
         "mob_pool": ("drake", "demon", "basilisk"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    52: {"depth": 52, "name": "Obsidian Reach", "theme": "gold", "rooms": 7,
         "mob_pool": ("demon", "drake"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    53: {"depth": 53, "name": "Stone-Heart", "theme": "gold", "rooms": 7,
         "mob_pool": ("basilisk", "drake", "demon"),
         "mob_pool_weights": (2, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    54: {"depth": 54, "name": "Quake Halls", "theme": "gold", "rooms": 8,
         "mob_pool": ("basilisk", "demon"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    55: {"depth": 55, "name": "Step of the Obsidian Giant", "theme": "boss", "rooms": 4,
         "mob_pool": ("basilisk", "demon"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "obsidian_giant", "color_hex": _C_BOSS},
    56: {"depth": 56, "name": "Lunar Boughs", "theme": "gold", "rooms": 7,
         "mob_pool": ("banshee", "drake", "lich_acolyte"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    57: {"depth": 57, "name": "Silver Tide", "theme": "gold", "rooms": 7,
         "mob_pool": ("banshee", "wisp", "wraith"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    58: {"depth": 58, "name": "Glow Caves", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "banshee", "drake"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    59: {"depth": 59, "name": "Auroral March", "theme": "gold", "rooms": 8,
         "mob_pool": ("banshee", "drake"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    60: {"depth": 60, "name": "Choir of the Moonflame", "theme": "boss", "rooms": 4,
         "mob_pool": ("banshee", "drake"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "moonflame_seraph", "color_hex": _C_BOSS},
    61: {"depth": 61, "name": "Bloodied Pavilion", "theme": "gold", "rooms": 7,
         "mob_pool": ("demon", "drake", "minotaur"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    62: {"depth": 62, "name": "War Drum Hall", "theme": "gold", "rooms": 7,
         "mob_pool": ("minotaur", "demon", "drake"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    63: {"depth": 63, "name": "Fortress Yard", "theme": "gold", "rooms": 7,
         "mob_pool": ("minotaur", "lich_acolyte"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    64: {"depth": 64, "name": "Spear-Forest", "theme": "gold", "rooms": 8,
         "mob_pool": ("minotaur", "demon"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    65: {"depth": 65, "name": "Banner of the Shogun", "theme": "boss", "rooms": 4,
         "mob_pool": ("minotaur", "demon"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "abyssal_shogun", "color_hex": _C_BOSS},
    66: {"depth": 66, "name": "Drowned Plaza", "theme": "gold", "rooms": 7,
         "mob_pool": ("drake", "banshee", "wisp"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    67: {"depth": 67, "name": "Coral Halls", "theme": "gold", "rooms": 7,
         "mob_pool": ("drake", "wraith"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    68: {"depth": 68, "name": "Tide of Salt", "theme": "gold", "rooms": 7,
         "mob_pool": ("banshee", "drake", "demon"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    69: {"depth": 69, "name": "Pelagic Drop", "theme": "gold", "rooms": 8,
         "mob_pool": ("drake", "banshee"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    70: {"depth": 70, "name": "Maw of the Leviathan", "theme": "boss", "rooms": 4,
         "mob_pool": ("drake", "banshee"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "leviathan_god", "color_hex": _C_BOSS},
    71: {"depth": 71, "name": "Sleepless Garden", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "banshee", "demon"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    72: {"depth": 72, "name": "Dreamthorn", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "banshee"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    73: {"depth": 73, "name": "Hollow Eye II", "theme": "gold", "rooms": 7,
         "mob_pool": ("banshee", "demon", "drake"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    74: {"depth": 74, "name": "Pillar of Sighs", "theme": "gold", "rooms": 8,
         "mob_pool": ("wisp", "banshee", "demon"),
         "mob_pool_weights": (2, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    75: {"depth": 75, "name": "Throne of Nightmare", "theme": "boss", "rooms": 4,
         "mob_pool": ("wisp", "banshee"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "nightmare_king", "color_hex": _C_BOSS},
    76: {"depth": 76, "name": "Architect's Workshop", "theme": "gold", "rooms": 7,
         "mob_pool": ("lich_acolyte", "demon", "drake"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    77: {"depth": 77, "name": "Geometry Hall", "theme": "gold", "rooms": 7,
         "mob_pool": ("lich_acolyte", "demon"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    78: {"depth": 78, "name": "Forge of Forms", "theme": "gold", "rooms": 7,
         "mob_pool": ("lich_acolyte", "demon", "drake"),
         "mob_pool_weights": (2, 3, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    79: {"depth": 79, "name": "Pre-Causal Stair", "theme": "gold", "rooms": 8,
         "mob_pool": ("demon", "lich_acolyte", "drake"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    80: {"depth": 80, "name": "Cradle of the Demiurge", "theme": "boss", "rooms": 4,
         "mob_pool": ("demon", "lich_acolyte"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "demiurge", "color_hex": _C_BOSS},
    81: {"depth": 81, "name": "Hollow Throne Approach", "theme": "gold", "rooms": 7,
         "mob_pool": ("demon", "drake"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    82: {"depth": 82, "name": "Shattered Crown Hall", "theme": "gold", "rooms": 7,
         "mob_pool": ("demon", "lich_acolyte", "drake"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    83: {"depth": 83, "name": "Black Banner Field", "theme": "gold", "rooms": 7,
         "mob_pool": ("demon", "minotaur", "drake"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    84: {"depth": 84, "name": "Iron Throne Hall", "theme": "gold", "rooms": 8,
         "mob_pool": ("demon", "minotaur"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    85: {"depth": 85, "name": "Throne of the Void Emperor", "theme": "boss", "rooms": 4,
         "mob_pool": ("demon", "minotaur"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "void_emperor", "color_hex": _C_BOSS},
    86: {"depth": 86, "name": "Pre-Cosmic Bedrock", "theme": "gold", "rooms": 7,
         "mob_pool": ("basilisk", "demon", "wisp"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    87: {"depth": 87, "name": "Crawling Mantle", "theme": "gold", "rooms": 7,
         "mob_pool": ("basilisk", "wisp"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    88: {"depth": 88, "name": "Tidal Madness", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "drake", "banshee"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    89: {"depth": 89, "name": "Egg of Worlds", "theme": "gold", "rooms": 8,
         "mob_pool": ("basilisk", "wisp"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    90: {"depth": 90, "name": "Birthplace of Chaos", "theme": "boss", "rooms": 4,
         "mob_pool": ("basilisk", "wisp"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "primordial_chaos", "color_hex": _C_BOSS},
    91: {"depth": 91, "name": "Annulled Hall", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "demon", "drake"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    92: {"depth": 92, "name": "Erasure Stair", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "drake"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    93: {"depth": 93, "name": "Anti-Light", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "banshee", "demon"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    94: {"depth": 94, "name": "Negative Heart", "theme": "gold", "rooms": 8,
         "mob_pool": ("wisp", "banshee"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    95: {"depth": 95, "name": "Approach to the Unmaker", "theme": "boss", "rooms": 4,
         "mob_pool": ("wisp", "banshee"),
         "mob_pool_weights": (2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": "the_unmaker", "color_hex": _C_BOSS},
    96: {"depth": 96, "name": "First Light", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "drake", "banshee"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    97: {"depth": 97, "name": "First Word", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "lich_acolyte"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    98: {"depth": 98, "name": "Pre-Beginning", "theme": "gold", "rooms": 7,
         "mob_pool": ("wisp", "banshee", "lich_acolyte"),
         "mob_pool_weights": (3, 2, 2),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    99: {"depth": 99, "name": "Final Stair", "theme": "gold", "rooms": 8,
         "mob_pool": ("wisp", "banshee"),
         "mob_pool_weights": (3, 3),
         "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
         "boss": None, "color_hex": _C_GOLD},
    100: {"depth": 100, "name": "First Place", "theme": "boss", "rooms": 4,
          "mob_pool": ("wisp", "banshee"),
          "mob_pool_weights": (2, 2),
          "ore_pool": (GOLD_SYMBOL,), "ore_pool_weights": (1,),
          "boss": "the_first", "color_hex": _C_BOSS},
}


# ============================================================================
# Weapon / armor / consumable catalogs
# ============================================================================
# All shop prices quoted in RUNE (Crypt Network coin). RUNE is earn-only
# so the player must mine + burn-swap or stake to afford anything past
# the starter tier. Price ladder is rough geometric so each tier feels
# like a real upgrade.

WILD_BATTLE_SPECIES: Final[tuple[str, ...]] = (
    # Physical / rocky / general buddies pulled from buddies_config.SPECIES.
    # Mirrors fishing_config.FISHING_BUDDY_SPECIES (water-themed pool) -- the
    # cog feeds these into services.buddy_battle.Fighter.from_row exactly the
    # way the fishing wild-battle path does, so any species name in
    # buddies_config.SPECIES works without further plumbing.
    "cobble", "shrek", "donkey", "chungus", "zenny",
)

# Per-theme wild-buddy spawn pools keyed by floor theme (see FLOORS[N]
# ``theme``: stone | silver | gold | boss). Shallow stone floors pull
# rats/cobble vibes; gold-tier floors get dragons and elementals; boss
# floors crank toward late-game species. Falls back to
# ``WILD_BATTLE_SPECIES`` for any theme not listed.
WILD_BUDDY_SPECIES_BY_THEME: Final[dict[str, tuple[str, ...]]] = {
    "stone":  ("cobble", "zenny", "spiderlenny"),
    "silver": ("cobble", "chungus", "spiderlenny", "donkey"),
    "gold":   ("chungus", "draclet", "robo", "blazer"),
    "boss":   ("draclet", "blazer", "robo", "glitch", "gloomer"),
}


def wild_buddy_species_pool(floor: int) -> tuple[str, ...]:
    """Resolve the wild-buddy species pool for a delve floor.

    Reads the floor's ``theme`` (stone / silver / gold / boss) via
    :func:`floor_meta` and falls back to the catch-all
    ``WILD_BATTLE_SPECIES`` pool if the theme isn't in the map.
    """
    meta = floor_meta(int(floor or 1)) or {}
    theme = str(meta.get("theme") or "").lower()
    pool = WILD_BUDDY_SPECIES_BY_THEME.get(theme)
    return pool or WILD_BATTLE_SPECIES

# Wild-buddy battle tuning. Mirrors fishing's WILD_BATTLE_* block so the
# behaviour and feel is consistent across the two earn-economy minigames.
# Spawn chance scales with floor depth (deeper = scarier = more likely to
# attract a wild buddy), capped so it never crowds out mob/ore rooms.
WILD_BATTLE_BASE_CHANCE: Final[float]            = 0.06
WILD_BATTLE_DEPTH_BONUS_PER_FLOOR: Final[float]  = 0.005   # +0.5%/floor
WILD_BATTLE_MAX_CHANCE: Final[float]             = 0.30

# Opponent scaling. Floor depth drives both level and rarity bias; the
# player's class doesn't gate the spawn (any class can fight any wild buddy)
# but it nudges rarity weights so a deeper-floor warrior still sees epics.
WILD_BATTLE_LEVEL_PER_FLOOR: Final[float]        = 0.8     # floor 5 -> ~lv 4
WILD_BATTLE_LEVEL_JITTER: Final[int]             = 3
WILD_BATTLE_RARITY_PER_FLOOR: Final[float]       = 0.05    # +1 tier every 20 floors

# Reward floor / ceiling per win. Scales with floor depth on the same shape
# fishing uses for zone tier. RUNE is the network coin (oracle-priced); the
# ore kicker is a flat physical-currency drop so deep delvers still get
# burnable supply if RUNE is currently expensive.
WILD_BATTLE_WIN_RUNE_MIN: Final[float]           = 5.0
WILD_BATTLE_WIN_RUNE_MAX: Final[float]           = 50.0
WILD_BATTLE_WIN_RUNE_PER_FLOOR: Final[float]     = 1.5
WILD_BATTLE_WIN_ORE_MIN: Final[float]            = 1.0
WILD_BATTLE_WIN_ORE_MAX: Final[float]            = 8.0
WILD_BATTLE_WIN_ORE_PER_FLOOR: Final[float]      = 1.4

# Active-buddy XP reward on every wild-battle win. Mirrors RUNE/ore in
# scaling shape so deep delvers feel a real progression curve on their
# fighting buddy without runaway numbers. Multiplier on opponent rarity
# tier so a Legendary wild buddy is ~1.4x XP vs a Common.
WILD_BATTLE_WIN_XP_BASE: Final[int]              = 25
WILD_BATTLE_WIN_XP_PER_FLOOR: Final[float]       = 8.0
WILD_BATTLE_WIN_XP_RARITY_MULT: Final[float]     = 0.10   # +10% per tier above 1


def wild_battle_xp_reward(floor: int, rarity_tier: int = 1) -> int:
    """Active-buddy XP earned on a wild-battle win.

    Scales linearly with floor depth and applies a small flat multiplier
    on the opponent's rarity tier. Floor 1 Common -> 33 XP; Floor 10
    Legendary -> ~140 XP. Conservative so chat / craft / expedition
    XP sources stay relevant.
    """
    base = WILD_BATTLE_WIN_XP_BASE + (
        max(0, int(floor) - 1) * WILD_BATTLE_WIN_XP_PER_FLOOR
    )
    mult = 1.0 + max(0, int(rarity_tier) - 1) * WILD_BATTLE_WIN_XP_RARITY_MULT
    return max(1, int(round(base * mult)))

# Capture chance after a win. Conservative (matches fishing's 20%) so wild
# captures stay rare enough to feel earned. Capture insertion respects
# MAX_OWNED_BUDDIES; a full shelter just refuses the capture and keeps the
# RUNE/ore haul (no penalty).
WILD_BATTLE_CAPTURE_CHANCE: Final[float]         = 0.20

# UI prompt window. Mirrors the fishing battle prompt so the cog can
# reuse the same timeout magic number.
WILD_BATTLE_PROMPT_TIMEOUT_S: Final[int]         = 60


def wild_battle_chance(floor: int) -> float:
    """Per-room chance of a wild-buddy spawn at the given floor depth."""
    extra = max(0, int(floor) - 1) * WILD_BATTLE_DEPTH_BONUS_PER_FLOOR
    return min(WILD_BATTLE_MAX_CHANCE, WILD_BATTLE_BASE_CHANCE + extra)


def roll_wild_battle(floor: int, class_key: str | None = None) -> dict:
    """Roll a synthesised wild-buddy opponent for a delve.

    Returns a dict matching the cc_buddies row shape that
    ``services.buddy_battle.Fighter.from_row`` accepts. Mood is pinned at
    100 so wild buddies always fight at peak. ``class_key`` is currently
    unused but the slot is kept so a future patch can bias the rarity
    weights per class without an API break.
    """
    import random as _r
    pool = wild_buddy_species_pool(floor)
    species = _r.choice(pool)
    base_level = max(1, int(round(int(floor) * WILD_BATTLE_LEVEL_PER_FLOOR)))
    level = max(
        1,
        base_level + _r.randint(-WILD_BATTLE_LEVEL_JITTER, WILD_BATTLE_LEVEL_JITTER),
    )
    base_tier = _r.choices((1, 2, 3, 4, 5), weights=(50, 25, 15, 7, 3), k=1)[0]
    bias = int(int(floor) * WILD_BATTLE_RARITY_PER_FLOOR)
    rarity_tier = max(1, min(5, base_tier + bias))
    return {
        "id": 0,                     # 0 = wild / synthesised, never persisted
        "owner_user_id": 0,          # 0 signals PvE to the engine
        "species": species,
        "name": species.title(),
        "rarity_tier": rarity_tier,
        "level": level,
        "hunger":    100,
        "happiness": 100,
        "energy":    100,
        "hp_alloc":  0,
        "atk_alloc": 0,
        "spd_alloc": 0,
    }


def wild_battle_rune_reward(floor: int) -> float:
    """RUNE prize for winning a wild-buddy battle. Scales with floor depth."""
    import random as _r
    base = _r.uniform(WILD_BATTLE_WIN_RUNE_MIN, WILD_BATTLE_WIN_RUNE_MAX)
    multiplier = 1.0 + max(0, int(floor) - 1) * (WILD_BATTLE_WIN_RUNE_PER_FLOOR - 1.0)
    return round(base * multiplier, 2)


def wild_battle_ore_reward(floor: int) -> tuple[str, float]:
    """Bonus ore drop on a wild-buddy win. Returns ``(symbol, qty_human)``.

    Symbol bias matches ``pick_ore_for_floor`` -- deeper floors lean toward
    silver / gold over copper. Returns a non-zero qty by construction.
    """
    import random as _r
    if WILD_BATTLE_WIN_ORE_MAX <= 0:
        return (COPPER_SYMBOL, 0.0)
    base = _r.uniform(WILD_BATTLE_WIN_ORE_MIN, WILD_BATTLE_WIN_ORE_MAX)
    multiplier = 1.0 + max(0, int(floor) - 1) * (WILD_BATTLE_WIN_ORE_PER_FLOOR - 1.0)
    qty = round(base * multiplier, 2)
    f = max(1, int(floor))
    if f >= 20:
        sym = _r.choices(ORE_SYMBOLS, weights=(20, 35, 45), k=1)[0]
    elif f >= 10:
        sym = _r.choices(ORE_SYMBOLS, weights=(35, 45, 20), k=1)[0]
    else:
        sym = _r.choices(ORE_SYMBOLS, weights=(70, 25, 5), k=1)[0]
    return (sym, qty)


# ============================================================================
# Rarity system
# ============================================================================
# Single rarity ladder for delve weapons / armor / junk. Mirrors the
# canonical RARITY_COLORS / RARITY_DOT in constants/ui.py so a "rare"
# weapon reads the same blue dot in the bag, the shop, and the loot
# embed. All existing catalog entries default to "common" via
# ``item_rarity()`` -- a missing field is NOT an error.
#
# The rarer the item, the higher the stat bonus (RARITY_STAT_MULT) and
# the more affix slots it carries (RARITY_AFFIX_COUNT). Existing items
# get a flat 0.80x base-stat reduction (BASE_STAT_FACTOR) so common
# gear feels deliberately weaker than the new rarer drops introduced by
# delve chests, mini-bosses, and bosses.
#
# Affixes are catalog-level (fixed per item key) so we don't need a
# per-instance migration. New rare+ items in Phase 3 declare an
# ``affixes`` dict with any of:
#   phys_dmg_pct       -- multiplier on melee + ranged swing damage
#   spell_dmg_pct      -- multiplier on skill damage (skill_kind=spell)
#   crit_pct           -- flat crit chance bump
#   hp_pct             -- flat HP-max bump
#   vs_undead_atk_pct  -- bonus damage vs ``undead``-tagged mobs
#   vs_undead_def_pct  -- damage reduction taken FROM undead-tagged mobs
#   lifesteal_pct      -- heal-on-kill fraction of mob max HP

RARITY_TIERS: Final[tuple[str, ...]] = (
    "common", "uncommon", "rare", "epic", "legendary",
)

# Rarity rank lookup -- 0=common .. 4=legendary -- used for sort order in
# bag / shop displays so legendaries float to the top.
RARITY_RANK: Final[dict[str, int]] = {t: i for i, t in enumerate(RARITY_TIERS)}

# Stat multiplier on weapon atk_bonus / armor def_bonus. The 0.80 base
# factor on common items is the explicit "make existing gear weaker"
# tuning; rarer drops climb back past 1.0 so a rare iron shortsword can
# beat an epic-tier base value flat-out.
BASE_STAT_FACTOR: Final[float] = 0.80
RARITY_STAT_MULT: Final[dict[str, float]] = {
    "common":    1.00,
    "uncommon":  1.15,
    "rare":      1.35,
    "epic":      1.60,
    "legendary": 2.00,
}

# Junk salvage_rune scales the same way -- a "rare salvage" sells for
# more so deeper-floor delvers get RUNE-per-bag bumps from the same
# bag size.
RARITY_JUNK_RUNE_MULT: Final[dict[str, float]] = {
    "common":    1.00,
    "uncommon":  1.40,
    "rare":      2.00,
    "epic":      3.00,
    "legendary": 5.00,
}

# Price multiplier for shop-purchasable rare items (most rare+ items are
# delve-only and priced via their catalog entries -- this is a fallback).
RARITY_PRICE_MULT: Final[dict[str, float]] = {
    "common":    1.00,
    "uncommon":  2.00,
    "rare":      4.00,
    "epic":      8.00,
    "legendary": 16.00,
}


def item_rarity(meta: dict | None) -> str:
    """Return the canonical rarity key of an item meta dict.

    Defaults to ``"common"`` when the field is missing -- existing
    catalog entries don't have to be touched to be valid.
    """
    if not meta:
        return "common"
    raw = str(meta.get("rarity") or "common").lower().strip()
    return raw if raw in RARITY_RANK else "common"


def effective_atk_bonus(weapon_meta: dict | None) -> int:
    """Return the post-rarity atk_bonus a weapon contributes to combat.

    Reads the catalog ``atk_bonus`` then applies BASE_STAT_FACTOR and
    the rarity multiplier. Both ``player_combat_stats`` AND any display
    formatter MUST go through this helper so the panel value matches
    what the engine actually swings with.
    """
    if not weapon_meta:
        return 0
    base = float(weapon_meta.get("atk_bonus") or 0)
    if base <= 0:
        return int(base)
    rarity = item_rarity(weapon_meta)
    mult = RARITY_STAT_MULT.get(rarity, 1.0) * BASE_STAT_FACTOR
    return max(1, int(round(base * mult)))


def effective_def_bonus(armor_meta: dict | None) -> int:
    """Return the post-rarity def_bonus an armor piece contributes."""
    if not armor_meta:
        return 0
    base = float(armor_meta.get("def_bonus") or 0)
    if base <= 0:
        return int(base)
    rarity = item_rarity(armor_meta)
    mult = RARITY_STAT_MULT.get(rarity, 1.0) * BASE_STAT_FACTOR
    return max(1, int(round(base * mult)))


def effective_salvage_rune(junk_meta: dict | None) -> float:
    """Return the per-unit RUNE refund a junk item sells for, post-rarity."""
    if not junk_meta:
        return 0.0
    base = float(junk_meta.get("salvage_rune") or 0.0)
    rarity = item_rarity(junk_meta)
    return round(base * RARITY_JUNK_RUNE_MULT.get(rarity, 1.0), 4)


def item_affixes(meta: dict | None) -> dict[str, float]:
    """Return the affix dict for an item (empty if none).

    Affixes are read by ``services.dungeon.resolve_attack`` to bias
    damage / mitigation per-swing and by display formatters to surface
    the "+10% phys, +15% vs undead" tag in the bag.
    """
    if not meta:
        return {}
    raw = meta.get("affixes") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}


# Affix display labels -- ordered for stable rendering in bag / shop panels.
_AFFIX_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("phys_dmg_pct",      "phys dmg"),
    ("spell_dmg_pct",     "spell dmg"),
    ("crit_pct",          "crit"),
    ("hp_pct",            "HP"),
    ("vs_undead_atk_pct", "vs undead"),
    ("vs_undead_def_pct", "undead resist"),
    ("lifesteal_pct",     "lifesteal"),
)


def affix_summary_lines(affixes: dict[str, float]) -> list[str]:
    """Render an item's affixes as short ``+10% phys dmg`` strings.

    Order is fixed so two items with the same affix set always render
    identically (avoids cosmetic diff in the bag).
    """
    out: list[str] = []
    for key, label in _AFFIX_LABELS:
        v = float(affixes.get(key) or 0.0)
        if v == 0:
            continue
        sign = "+" if v > 0 else ""
        pct = int(round(v * 100))
        out.append(f"{sign}{pct}% {label}")
    return out


def rarity_dot(rarity: str) -> str:
    """Colored-circle glyph for a rarity. Falls back to brown on bad input.

    Mirrors ``constants.ui.RARITY_DOT`` so this stays the single source
    of truth even when called from `dungeon_config.py` (no circular
    import risk -- we just inline the table).
    """
    return _RARITY_DOT_LOCAL.get(rarity, _RARITY_DOT_LOCAL["common"])


# Inlined to avoid importing framework code into config -- the values
# match constants/ui.py RARITY_DOT exactly. If the canonical table
# changes, mirror the change here.
_RARITY_DOT_LOCAL: Final[dict[str, str]] = {
    "common":    "\U0001F7E4",  # brown circle
    "uncommon":  "\U0001F7E2",  # green circle
    "rare":      "\U0001F535",  # blue circle
    "epic":      "\U0001F7E3",  # purple circle
    "legendary": "\U0001F7E1",  # yellow circle
}


def rarity_label(rarity: str) -> str:
    """Title-case label for a rarity key (e.g. "Legendary")."""
    return str(rarity or "common").capitalize()


def gear_sell_value(meta: dict | None) -> float:
    """Return the RUNE refund for selling a piece of gear.

    Shop-bought items refund 50% of their listed ``price_rune`` (the
    ``_GEAR_SELL_RATE`` policy in services.dungeon). Delve-only drops
    have ``price_rune == 0`` because they were never for sale, so we
    synthesise a sell value from ``tier`` and the rarity-price ladder
    instead -- a rare T3 item refunds ~360 RUNE; a legendary T8 ~3800
    RUNE -- so trading up loot still pays a meaningful amount.
    """
    if not meta:
        return 0.0
    price = float(meta.get("price_rune") or 0.0)
    if price > 0:
        return round(price * 0.50, 4)
    if not meta.get("delve_only"):
        return 0.0
    tier = max(1, int(meta.get("tier") or 1))
    rarity = item_rarity(meta)
    base = tier * 30.0
    return round(base * RARITY_PRICE_MULT.get(rarity, 1.0), 4)


WEAPONS: dict[str, dict] = {
    # ── Melee blades / shortswords / axes / maces ────────────────────────
    "rusty_dagger": {
        "key": "rusty_dagger", "name": "Rusty Dagger", "emoji": "\U0001F5E1",
        "tier": 0, "atk_bonus": 0, "price_rune": 0.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Better than fists. Barely.",
    },
    "iron_shortsword": {
        "key": "iron_shortsword", "name": "Iron Shortsword", "emoji": "\U0001F5E1",
        "tier": 1, "atk_bonus": 3, "price_rune": 15.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Reliable starter steel.",
    },
    "silvered_dirk": {
        "key": "silvered_dirk", "name": "Silvered Dirk", "emoji": "\U0001F5E1",
        "tier": 2, "atk_bonus": 8, "price_rune": 65.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Silver-coated edge. Bites the things that don't bleed.",
    },
    "venom_kiss": {
        "key": "venom_kiss", "name": "Venom Kiss", "emoji": "\U0001F5E1",
        "tier": 3, "atk_bonus": 15, "price_rune": 220.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Hollow channel along the blade. Always wet.",
    },
    "shadowstrike": {
        "key": "shadowstrike", "name": "Shadowstrike", "emoji": "\U0001F5E1",
        "tier": 4, "atk_bonus": 26, "price_rune": 720.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "The blade vanishes between heartbeats. So does the target.",
    },
    "moonlit_kris": {
        "key": "moonlit_kris", "name": "Moonlit Kris", "emoji": "\U0001F5E1",
        "tier": 5, "atk_bonus": 43, "price_rune": 2300.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Wavy folded steel. Reflects only the moon, never the wielder.",
    },
    "nightshade_blade": {
        "key": "nightshade_blade", "name": "Nightshade Blade", "emoji": "\U0001F5E1",
        "tier": 6, "atk_bonus": 67, "price_rune": 6700.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Forged in starless dark. The wound forgets it was made.",
    },
    "steel_longsword": {
        "key": "steel_longsword", "name": "Steel Longsword", "emoji": "\U00002694",
        "tier": 2, "atk_bonus": 7, "price_rune": 60.0,
        "weapon_type": "longsword", "attack_kind": "melee",
        "blurb": "Standard adventurer issue.",
    },
    "silvered_blade": {
        "key": "silvered_blade", "name": "Silvered Blade", "emoji": "\U00002694",
        "tier": 3, "atk_bonus": 14, "price_rune": 200.0,
        "weapon_type": "longsword", "attack_kind": "melee",
        "blurb": "Hums softly near undead.",
    },
    "rune_axe": {
        "key": "rune_axe", "name": "Rune Axe", "emoji": "\U0001FA93",
        "tier": 4, "atk_bonus": 25, "price_rune": 700.0,
        "weapon_type": "axe", "attack_kind": "melee",
        "blurb": "Etched with cracking sigils.",
    },
    "mythril_sword": {
        "key": "mythril_sword", "name": "Mythril Sword", "emoji": "\U00002694",
        "tier": 5, "atk_bonus": 42, "price_rune": 2200.0,
        "weapon_type": "longsword", "attack_kind": "melee",
        "blurb": "Light as a feather, bites like a shark.",
    },
    "astral_cleaver": {
        "key": "astral_cleaver", "name": "Astral Cleaver", "emoji": "\U0001F5E1",
        "tier": 6, "atk_bonus": 65, "price_rune": 6500.0,
        "weapon_type": "axe", "attack_kind": "melee",
        "blurb": "Each swing leaves a trail of stars. Mostly aesthetic.",
    },
    "phoenix_talon": {
        "key": "phoenix_talon", "name": "Phoenix Talon", "emoji": "\U0001F5E1",
        "tier": 7, "atk_bonus": 95, "price_rune": 18000.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Always warm. Wounds it inflicts cauterise themselves.",
    },
    "abyssal_dirk": {
        "key": "abyssal_dirk", "name": "Abyssal Dirk", "emoji": "\U0001F5E1",
        "tier": 8, "atk_bonus": 142, "price_rune": 51000.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Pulled from the trench-floor. Still cold to the touch.",
    },
    "wyrmfang_kris": {
        "key": "wyrmfang_kris", "name": "Wyrmfang Kris", "emoji": "\U0001F5E1",
        "tier": 9, "atk_bonus": 212, "price_rune": 145_000.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Carved from a hatchling wyrm's tooth. The point never dulls.",
    },
    "starshard_dirk": {
        "key": "starshard_dirk", "name": "Starshard Dirk", "emoji": "\U0001F5E1",
        "tier": 10, "atk_bonus": 322, "price_rune": 410_000.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "Edge ground from a fallen star. Cuts shadows clean off bodies.",
    },
    "first_fang": {
        "key": "first_fang", "name": "First Fang", "emoji": "\U0001F5E1",
        "tier": 11, "atk_bonus": 505, "price_rune": 1_215_000.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "blurb": "The first thing the First broke off. Older than the idea of a knife.",
    },
    "void_blade": {
        "key": "void_blade", "name": "Void Blade", "emoji": "\U00002694",
        "tier": 8, "atk_bonus": 140, "price_rune": 50000.0,
        "weapon_type": "longsword", "attack_kind": "melee",
        "blurb": "What it cuts, the dungeon forgets.",
    },
    "wyrmfang_greatsword": {
        "key": "wyrmfang_greatsword", "name": "Wyrmfang Greatsword", "emoji": "\U0001F5E1",
        "tier": 9, "atk_bonus": 210, "price_rune": 140000.0,
        "weapon_type": "longsword", "attack_kind": "melee",
        "blurb": "Forged from the Wyrm's tooth. Still hungry.",
    },
    "archons_edge": {
        "key": "archons_edge", "name": "Archon's Edge", "emoji": "\U00002694",
        "tier": 10, "atk_bonus": 320, "price_rune": 400000.0,
        "weapon_type": "longsword", "attack_kind": "melee",
        "blurb": "Cuts through the rules of the dungeon.",
    },
    "soul_reaver": {
        "key": "soul_reaver", "name": "Soul Reaver", "emoji": "\U0001FA93",
        "tier": 11, "atk_bonus": 500, "price_rune": 1_200_000.0,
        "weapon_type": "axe", "attack_kind": "melee",
        "blurb": "The endgame weapon. Whoever wields it stops aging.",
    },

    # ── Maces (warrior-only flavor branch; high def-pen feel via type) ───
    "iron_mace": {
        "key": "iron_mace", "name": "Iron Mace", "emoji": "\U0001FA93",
        "tier": 2, "atk_bonus": 7, "price_rune": 60.0,
        "weapon_type": "mace", "attack_kind": "melee",
        "blurb": "A studded lump of metal. Doesn't ask permission.",
    },
    "war_mace": {
        "key": "war_mace", "name": "War Mace", "emoji": "\U0001FA93",
        "tier": 4, "atk_bonus": 26, "price_rune": 720.0,
        "weapon_type": "mace", "attack_kind": "melee",
        "blurb": "Forged for cracking shields.",
    },
    "morningstar": {
        "key": "morningstar", "name": "Morningstar", "emoji": "\U0001F31F",
        "tier": 6, "atk_bonus": 67, "price_rune": 6700.0,
        "weapon_type": "mace", "attack_kind": "melee",
        "blurb": "Spiked head on a chain. Whirls before it bites.",
    },
    "wyrmbone_maul": {
        "key": "wyrmbone_maul", "name": "Wyrmbone Maul", "emoji": "\U0001FA93",
        "tier": 9, "atk_bonus": 215, "price_rune": 145_000.0,
        "weapon_type": "mace", "attack_kind": "melee",
        "blurb": "Two-handed maul carved from a wyrm's vertebra.",
    },

    # ── Bows (Archer ranged, draws from arrow_bundle) ─────────────────────
    "short_bow": {
        "key": "short_bow", "name": "Short Bow", "emoji": "\U0001F3F9",
        "tier": 1, "atk_bonus": 3, "price_rune": 15.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Yew with a hemp string. Strung in a hurry.",
    },
    "longbow": {
        "key": "longbow", "name": "Longbow", "emoji": "\U0001F3F9",
        "tier": 2, "atk_bonus": 8, "price_rune": 65.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Six feet of yew. Reaches across rooms.",
    },
    "recurve_bow": {
        "key": "recurve_bow", "name": "Recurve Bow", "emoji": "\U0001F3F9",
        "tier": 3, "atk_bonus": 15, "price_rune": 210.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Limbs curl outward; fires faster than it has any right to.",
    },
    "warbow": {
        "key": "warbow", "name": "War Bow", "emoji": "\U0001F3F9",
        "tier": 4, "atk_bonus": 26, "price_rune": 720.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Pulls 150 lbs. Snaps wrists if you're not strong.",
    },
    "elven_longbow": {
        "key": "elven_longbow", "name": "Elven Longbow", "emoji": "\U0001F3F9",
        "tier": 5, "atk_bonus": 44, "price_rune": 2300.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Sings under tension. Arrows fly true on their own.",
    },
    "composite_bow": {
        "key": "composite_bow", "name": "Composite Bow", "emoji": "\U0001F3F9",
        "tier": 6, "atk_bonus": 66, "price_rune": 6700.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Horn, sinew, and yew laminated under heat. Punches like a crossbow.",
    },
    "stormcaller_bow": {
        "key": "stormcaller_bow", "name": "Stormcaller Bow", "emoji": "\U000026A1",
        "tier": 7, "atk_bonus": 96, "price_rune": 18500.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Strung with cloud-thread. Each loose carries a thunderclap.",
    },
    "voidstring_bow": {
        "key": "voidstring_bow", "name": "Voidstring Bow", "emoji": "\U0001F300",
        "tier": 8, "atk_bonus": 143, "price_rune": 51500.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Bowstring spun from un-light. Arrows arrive before they leave.",
    },
    "dragonbone_bow": {
        "key": "dragonbone_bow", "name": "Dragonbone Bow", "emoji": "\U0001F409",
        "tier": 9, "atk_bonus": 215, "price_rune": 145_000.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Carved from a wyrm's rib. Pulls itself.",
    },
    "archon_bow": {
        "key": "archon_bow", "name": "Archon Bow", "emoji": "\U0001F451",
        "tier": 10, "atk_bonus": 322, "price_rune": 405_000.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Drawn once, by the Archon, at the dawn of the dungeon.",
    },
    "world_tree_bow": {
        "key": "world_tree_bow", "name": "World-Tree Bow", "emoji": "\U0001F33A",
        "tier": 11, "atk_bonus": 510, "price_rune": 1_220_000.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "blurb": "Cut from the root that holds the dungeon up.",
    },

    # ── Crossbows (Archer ranged, draws from bolt_bundle) ─────────────────
    "light_crossbow": {
        "key": "light_crossbow", "name": "Light Crossbow", "emoji": "\U0001F3F9",
        "tier": 1, "atk_bonus": 4, "price_rune": 18.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Wood lath, hemp string. Slow trigger but anyone can aim it.",
    },
    "hand_crossbow": {
        "key": "hand_crossbow", "name": "Hand Crossbow", "emoji": "\U0001F3F9",
        "tier": 2, "atk_bonus": 9, "price_rune": 75.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "One-handed. Reloads slowly but punches like a sin.",
    },
    "arbalest": {
        "key": "arbalest", "name": "Arbalest", "emoji": "\U0001F3F9",
        "tier": 3, "atk_bonus": 16, "price_rune": 220.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Steel prod and a goat's-foot lever. Punches above its size.",
    },
    "heavy_crossbow": {
        "key": "heavy_crossbow", "name": "Heavy Crossbow", "emoji": "\U0001F3F9",
        "tier": 4, "atk_bonus": 27, "price_rune": 740.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Cranked with a windlass. Bolts go through plate.",
    },
    "rune_crossbow": {
        "key": "rune_crossbow", "name": "Rune Crossbow", "emoji": "\U0001F3F9",
        "tier": 5, "atk_bonus": 45, "price_rune": 2350.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Etched stock glows when nocked. Bolts track a hair toward warmth.",
    },
    "repeating_crossbow": {
        "key": "repeating_crossbow", "name": "Repeating Crossbow", "emoji": "\U0001F3F9",
        "tier": 6, "atk_bonus": 68, "price_rune": 6800.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Magazine of bolts. Fires until you stop pulling.",
    },
    "stormbolt_crossbow": {
        "key": "stormbolt_crossbow", "name": "Stormbolt Crossbow", "emoji": "\U000026A1",
        "tier": 7, "atk_bonus": 98, "price_rune": 18800.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Lightning in the lath. Each bolt cracks the air on release.",
    },
    "siege_crossbow": {
        "key": "siege_crossbow", "name": "Siege Crossbow", "emoji": "\U0001F3F9",
        "tier": 8, "atk_bonus": 145, "price_rune": 51000.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Designed for breaking gates. Mob doesn't notice the difference.",
    },
    "wyrmbolt_crossbow": {
        "key": "wyrmbolt_crossbow", "name": "Wyrmbolt Crossbow", "emoji": "\U0001F409",
        "tier": 9, "atk_bonus": 218, "price_rune": 148_000.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Stocked from a wyrm's spine. Bolts hiss like the beast it came from.",
    },
    "voidshot_crossbow": {
        "key": "voidshot_crossbow", "name": "Voidshot Crossbow", "emoji": "\U0001F300",
        "tier": 10, "atk_bonus": 325, "price_rune": 410_000.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Bolts wink out mid-air and reappear past the target's spine.",
    },
    "world_tree_crossbow": {
        "key": "world_tree_crossbow", "name": "World-Tree Crossbow", "emoji": "\U0001F33A",
        "tier": 11, "atk_bonus": 515, "price_rune": 1_225_000.0,
        "weapon_type": "crossbow", "attack_kind": "ranged",
        "ammo_key": "bolt_bundle",
        "blurb": "Stock cut from the same root as the World-Tree Bow. Older sibling.",
    },

    # ── Staves (Mage caster; channels INT) ────────────────────────────────
    "novice_staff": {
        "key": "novice_staff", "name": "Novice Staff", "emoji": "\U0001FA84",
        "tier": 0, "atk_bonus": 0, "price_rune": 0.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "A walking stick with apprentice runes. Mostly walking, less stick.",
    },
    "apprentice_staff": {
        "key": "apprentice_staff", "name": "Apprentice Staff", "emoji": "\U0001FA84",
        "tier": 1, "atk_bonus": 4, "price_rune": 18.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Cracked crystal at the head. Buzzes when you think hard.",
    },
    "oaken_staff": {
        "key": "oaken_staff", "name": "Oaken Staff", "emoji": "\U0001FA84",
        "tier": 2, "atk_bonus": 8, "price_rune": 65.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Carved from a single oak limb. Steady channel, no kickback.",
    },
    "runed_staff": {
        "key": "runed_staff", "name": "Runed Staff", "emoji": "\U0001FA84",
        "tier": 3, "atk_bonus": 16, "price_rune": 220.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Etched glyphs flicker mid-cast.",
    },
    "stormwood_staff": {
        "key": "stormwood_staff", "name": "Stormwood Staff", "emoji": "\U0001FA84",
        "tier": 4, "atk_bonus": 27, "price_rune": 730.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Carved from a tree split by lightning. Crackles when it casts.",
    },
    "arcane_staff": {
        "key": "arcane_staff", "name": "Arcane Staff", "emoji": "\U0001FA84",
        "tier": 5, "atk_bonus": 45, "price_rune": 2400.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Floating crystal orbits the head.",
    },
    "celestial_staff": {
        "key": "celestial_staff", "name": "Celestial Staff", "emoji": "\U00002728",
        "tier": 6, "atk_bonus": 68, "price_rune": 6800.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Pole studded with bound starlight. Hums in time with the orrery.",
    },
    "archmage_staff": {
        "key": "archmage_staff", "name": "Archmage Staff", "emoji": "\U0001FA84",
        "tier": 7, "atk_bonus": 100, "price_rune": 19000.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Held only by registered arch-magi. The staff knows.",
    },
    "starforged_staff": {
        "key": "starforged_staff", "name": "Starforged Staff", "emoji": "\U00002728",
        "tier": 8, "atk_bonus": 145, "price_rune": 52000.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Hammered out on a meteor anvil. The head is a captive sun.",
    },
    "void_staff": {
        "key": "void_staff", "name": "Void Staff", "emoji": "\U0001F300",
        "tier": 9, "atk_bonus": 220, "price_rune": 150_000.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Channel-end opens onto somewhere else. Don't peek.",
    },
    "archon_staff": {
        "key": "archon_staff", "name": "Archon Staff", "emoji": "\U0001F451",
        "tier": 10, "atk_bonus": 325, "price_rune": 415_000.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Once held by the Archon. Still listens for an old voice.",
    },
    "primordial_staff": {
        "key": "primordial_staff", "name": "Primordial Staff", "emoji": "\U0001F31F",
        "tier": 11, "atk_bonus": 520, "price_rune": 1_240_000.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "blurb": "Cut from a tree that grew before light did.",
    },

    # ── Rods (Druid focus; nature channeling) ─────────────────────────────
    "hawthorn_rod": {
        "key": "hawthorn_rod", "name": "Hawthorn Rod", "emoji": "\U0001F33F",
        "tier": 0, "atk_bonus": 0, "price_rune": 0.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Living branch. Small white flowers in spring.",
    },
    "willow_rod": {
        "key": "willow_rod", "name": "Willow Rod", "emoji": "\U0001F33F",
        "tier": 1, "atk_bonus": 4, "price_rune": 17.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Whip-thin and supple. Channels nature with a teenager's enthusiasm.",
    },
    "oakheart_rod": {
        "key": "oakheart_rod", "name": "Oakheart Rod", "emoji": "\U0001F33F",
        "tier": 2, "atk_bonus": 8, "price_rune": 65.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Heartwood from a hundred-year oak. Steady channel.",
    },
    "ash_rod": {
        "key": "ash_rod", "name": "Ash Rod", "emoji": "\U0001F33F",
        "tier": 3, "atk_bonus": 16, "price_rune": 215.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Cut from a lightning-touched ash. Holds a charge between casts.",
    },
    "wildwood_rod": {
        "key": "wildwood_rod", "name": "Wildwood Rod", "emoji": "\U0001F33F",
        "tier": 4, "atk_bonus": 27, "price_rune": 730.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Pulse of forest behind every wave.",
    },
    "yew_rod": {
        "key": "yew_rod", "name": "Yew Rod", "emoji": "\U0001F33F",
        "tier": 5, "atk_bonus": 44, "price_rune": 2300.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Cured graveyard yew. Unsettling to channel through, but the woods listen.",
    },
    "thornlash_rod": {
        "key": "thornlash_rod", "name": "Thornlash Rod", "emoji": "\U0001F339",
        "tier": 6, "atk_bonus": 67, "price_rune": 6700.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Sprouts thorns mid-swing. Bleeds the target.",
    },
    "heartwood_rod": {
        "key": "heartwood_rod", "name": "Heartwood Rod", "emoji": "\U0001F33B",
        "tier": 7, "atk_bonus": 98, "price_rune": 18800.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Cut from a tree's literal heart. Faintly warm, faintly beating.",
    },
    "elder_rod": {
        "key": "elder_rod", "name": "Elder Rod", "emoji": "\U0001F33F",
        "tier": 8, "atk_bonus": 145, "price_rune": 51_000.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Carved from elder. Whispers herblore between rounds.",
    },
    "ironbark_rod": {
        "key": "ironbark_rod", "name": "Ironbark Rod", "emoji": "\U0001F33F",
        "tier": 9, "atk_bonus": 218, "price_rune": 148_000.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Bark hard as plate. Doubles as a bludgeon when the spells run dry.",
    },
    "verdant_rod": {
        "key": "verdant_rod", "name": "Verdant Rod", "emoji": "\U0001F33A",
        "tier": 10, "atk_bonus": 325, "price_rune": 405_000.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "Leaves regrow as you channel. Some claim it heals when held.",
    },
    "world_root_rod": {
        "key": "world_root_rod", "name": "World-Root Rod", "emoji": "\U0001F33B",
        "tier": 11, "atk_bonus": 510, "price_rune": 1_210_000.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "blurb": "A piece of the root that holds every world together.",
    },

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  Delve-only weapons (drop only from chests / mini-bosses /       ║
    # ║  bosses; delve_only=True hides them from the buy shop)           ║
    # ╚══════════════════════════════════════════════════════════════════╝
    "boneblade": {
        "key": "boneblade", "name": "Boneblade", "emoji": "\U0001F5E1",
        "tier": 3, "atk_bonus": 12, "price_rune": 0.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "rarity": "rare", "delve_only": True,
        "affixes": {"phys_dmg_pct": 0.05, "vs_undead_atk_pct": 0.20},
        "blurb": "Carved from a Lich Acolyte's femur. Sings near skeletons.",
    },
    "ghoul_cleaver": {
        "key": "ghoul_cleaver", "name": "Ghoul Cleaver", "emoji": "\U0001FA93",
        "tier": 4, "atk_bonus": 18, "price_rune": 0.0,
        "weapon_type": "axe", "attack_kind": "melee",
        "rarity": "rare", "delve_only": True,
        "affixes": {"phys_dmg_pct": 0.08, "lifesteal_pct": 0.02},
        "blurb": "Heavy iron, jagged edge. Drinks back a sip every kill.",
    },
    "frost_dagger": {
        "key": "frost_dagger", "name": "Frost Dagger", "emoji": "\U0001F5E1",
        "tier": 5, "atk_bonus": 30, "price_rune": 0.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "rarity": "epic", "delve_only": True,
        "affixes": {"phys_dmg_pct": 0.10, "crit_pct": 0.05},
        "blurb": "Edge always cold to the touch. The wound it makes never warms back up.",
    },
    "shadow_bow": {
        "key": "shadow_bow", "name": "Shadow Bow", "emoji": "\U0001F3F9",
        "tier": 5, "atk_bonus": 32, "price_rune": 0.0,
        "weapon_type": "bow", "attack_kind": "ranged",
        "ammo_key": "arrow_bundle",
        "rarity": "epic", "delve_only": True,
        "affixes": {"phys_dmg_pct": 0.10, "crit_pct": 0.05},
        "blurb": "Limbs cut from a tree that grew in shadow. Arrows fly silent.",
    },
    "spell_blade": {
        "key": "spell_blade", "name": "Spell Blade", "emoji": "\U0001FA84",
        "tier": 4, "atk_bonus": 18, "price_rune": 0.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "rarity": "rare", "delve_only": True,
        "affixes": {"spell_dmg_pct": 0.12},
        "blurb": "Hybrid focus: half blade, half wand. Channels through the cross-guard.",
    },
    "voidstaff_relic": {
        "key": "voidstaff_relic", "name": "Voidstaff Relic", "emoji": "\U0001F300",
        "tier": 6, "atk_bonus": 50, "price_rune": 0.0,
        "weapon_type": "staff", "attack_kind": "melee",
        "rarity": "epic", "delve_only": True,
        "affixes": {"spell_dmg_pct": 0.18, "crit_pct": 0.05},
        "blurb": "Channel-end opens onto somewhere quieter. Spells leak through louder.",
    },
    "thornlash_relic": {
        "key": "thornlash_relic", "name": "Thornlash Relic", "emoji": "\U0001F339",
        "tier": 5, "atk_bonus": 32, "price_rune": 0.0,
        "weapon_type": "rod", "attack_kind": "melee",
        "rarity": "epic", "delve_only": True,
        "affixes": {"spell_dmg_pct": 0.12, "lifesteal_pct": 0.03},
        "blurb": "Sprouts thorns mid-cast. Bleeds the target. Drinks the bleed.",
    },
    "dragonfang_dirk": {
        "key": "dragonfang_dirk", "name": "Dragonfang Dirk", "emoji": "\U0001F409",
        "tier": 7, "atk_bonus": 75, "price_rune": 0.0,
        "weapon_type": "shortsword", "attack_kind": "melee",
        "rarity": "legendary", "delve_only": True,
        "affixes": {"phys_dmg_pct": 0.15, "crit_pct": 0.10, "lifesteal_pct": 0.03},
        "blurb": "A wyrm's tooth, sharpened on regret. Strikes through plate.",
    },
}

ARMOR: dict[str, dict] = {
    # ── Light armor (Mage / Druid) ───────────────────────────────────────
    "cloth_tunic": {
        "key": "cloth_tunic", "name": "Cloth Tunic", "emoji": "\U0001F455",
        "tier": 0, "def_bonus": 0, "price_rune": 0.0,
        "armor_type": "light",
        "blurb": "Better than nothing. Slightly.",
    },
    "linen_robe": {
        "key": "linen_robe", "name": "Linen Robe", "emoji": "\U0001F457",
        "tier": 1, "def_bonus": 1, "price_rune": 14.0,
        "armor_type": "light",
        "blurb": "Spun thin. Won't catch on a casting gesture.",
    },
    "cotton_robe": {
        "key": "cotton_robe", "name": "Cotton Robe", "emoji": "\U0001F457",
        "tier": 2, "def_bonus": 4, "price_rune": 55.0,
        "armor_type": "light",
        "blurb": "Heavier weave, double-belted. Survives a thrown punch, sometimes.",
    },
    "silk_robe": {
        "key": "silk_robe", "name": "Silk Robe", "emoji": "\U0001F457",
        "tier": 3, "def_bonus": 7, "price_rune": 220.0,
        "armor_type": "light",
        "blurb": "Conductive enough to push spell efficiency a hair.",
    },
    "spell_silk_robe": {
        "key": "spell_silk_robe", "name": "Spell-Silk Robe", "emoji": "\U0001F457",
        "tier": 4, "def_bonus": 14, "price_rune": 600.0,
        "armor_type": "light",
        "blurb": "Silk soaked in stilled mana. Hangs straight even mid-cast.",
    },
    "enchanter_robe": {
        "key": "enchanter_robe", "name": "Enchanter Robe", "emoji": "\U0001F9D9",
        "tier": 5, "def_bonus": 22, "price_rune": 2200.0,
        "armor_type": "light",
        "blurb": "Threaded with focus glyphs. Slight glow at the cuffs.",
    },
    "arcanist_robe": {
        "key": "arcanist_robe", "name": "Arcanist Robe", "emoji": "\U0001F9D9",
        "tier": 6, "def_bonus": 35, "price_rune": 6100.0,
        "armor_type": "light",
        "blurb": "Layered runic stoles. The glyphs argue with each other softly.",
    },
    "starwoven_robe": {
        "key": "starwoven_robe", "name": "Starwoven Robe", "emoji": "\U00002728",
        "tier": 7, "def_bonus": 52, "price_rune": 16500.0,
        "armor_type": "light",
        "blurb": "Cloth dipped in nebulae. Glints when you move.",
    },
    "nebula_robe": {
        "key": "nebula_robe", "name": "Nebula Robe", "emoji": "\U00002728",
        "tier": 8, "def_bonus": 78, "price_rune": 46500.0,
        "armor_type": "light",
        "blurb": "Hem trails actual nebulae. Spells fall out of it like rain.",
    },
    "voidweave_robe": {
        "key": "voidweave_robe", "name": "Voidweave Robe", "emoji": "\U0001F300",
        "tier": 9, "def_bonus": 115, "price_rune": 132_000.0,
        "armor_type": "light",
        "blurb": "Half not-there. Spells slip out of it cleanly.",
    },
    "archon_robe": {
        "key": "archon_robe", "name": "Archon Robe", "emoji": "\U0001F451",
        "tier": 10, "def_bonus": 175, "price_rune": 380000.0,
        "armor_type": "light",
        "blurb": "Cut to the Archon's measure. The cuffs remember every spell ever cast.",
    },
    "first_robe": {
        "key": "first_robe", "name": "First Robe", "emoji": "\U0001F31F",
        "tier": 11, "def_bonus": 280, "price_rune": 1_120_000.0,
        "armor_type": "light",
        "blurb": "The robe the First wore while shaping the world.",
    },

    # ── Medium armor (Rogue / Archer) ────────────────────────────────────
    "leather_jerkin": {
        "key": "leather_jerkin", "name": "Leather Jerkin", "emoji": "\U0001F9BA",
        "tier": 1, "def_bonus": 2, "price_rune": 12.0,
        "armor_type": "medium",
        "blurb": "Boiled hide. Smells like a tannery.",
    },
    "studded_leather": {
        "key": "studded_leather", "name": "Studded Leather", "emoji": "\U0001F9BA",
        "tier": 2, "def_bonus": 5, "price_rune": 50.0,
        "armor_type": "medium",
        "blurb": "Brass studs over hardened hide.",
    },
    "scale_brigandine": {
        "key": "scale_brigandine", "name": "Scale Brigandine", "emoji": "\U0001F9BA",
        "tier": 3, "def_bonus": 11, "price_rune": 200.0,
        "armor_type": "medium",
        "blurb": "Tiny iron scales sewn into a quilted shell.",
    },
    "ranger_garb": {
        "key": "ranger_garb", "name": "Ranger Garb", "emoji": "\U0001F9BA",
        "tier": 4, "def_bonus": 19, "price_rune": 620.0,
        "armor_type": "medium",
        "blurb": "Hide oiled with pine sap. Quiet across stone.",
    },
    "wyrm_hide": {
        "key": "wyrm_hide", "name": "Wyrm Hide", "emoji": "\U0001F409",
        "tier": 5, "def_bonus": 32, "price_rune": 2050.0,
        "armor_type": "medium",
        "blurb": "Cured young-wyrm scale. Flame-tolerant.",
    },
    "drake_scale": {
        "key": "drake_scale", "name": "Drake Scale", "emoji": "\U0001F409",
        "tier": 6, "def_bonus": 50, "price_rune": 6200.0,
        "armor_type": "medium",
        "blurb": "Overlapping drake scales lacquered to leather. Sheds claws.",
    },
    "shadow_leather": {
        "key": "shadow_leather", "name": "Shadow Leather", "emoji": "\U0001F575",
        "tier": 7, "def_bonus": 72, "price_rune": 16500.0,
        "armor_type": "medium",
        "blurb": "Dyed in pitch. The dark sticks to it.",
    },
    "void_leather": {
        "key": "void_leather", "name": "Void Leather", "emoji": "\U0001F300",
        "tier": 8, "def_bonus": 105, "price_rune": 46000.0,
        "armor_type": "medium",
        "blurb": "Tanned in a pocket of un-space. Half the hits land somewhere else.",
    },
    "phoenix_hide": {
        "key": "phoenix_hide", "name": "Phoenix Hide", "emoji": "\U0001F525",
        "tier": 9, "def_bonus": 152, "price_rune": 132_000.0,
        "armor_type": "medium",
        "blurb": "Smolders if shoulders are tense. Fire-immune.",
    },
    "archon_hide": {
        "key": "archon_hide", "name": "Archon Hide", "emoji": "\U0001F451",
        "tier": 10, "def_bonus": 235, "price_rune": 385000.0,
        "armor_type": "medium",
        "blurb": "Cured from the Archon's mantle-beast. Carries authority on the shoulders.",
    },
    "first_hide": {
        "key": "first_hide", "name": "First Hide", "emoji": "\U0001F33B",
        "tier": 11, "def_bonus": 340, "price_rune": 1_110_000.0,
        "armor_type": "medium",
        "blurb": "Cured from the first beast. Older than every other hide.",
    },

    # ── Heavy armor (Warrior) ────────────────────────────────────────────
    "chain_mail": {
        "key": "chain_mail", "name": "Chain Mail", "emoji": "\U0001F9BA",
        "tier": 2, "def_bonus": 5, "price_rune": 50.0,
        "armor_type": "heavy",
        "blurb": "Rings on rings on rings.",
    },
    "plate_armor": {
        "key": "plate_armor", "name": "Plate Armor", "emoji": "\U0001F6E1",
        "tier": 3, "def_bonus": 10, "price_rune": 180.0,
        "armor_type": "heavy",
        "blurb": "Heavy, hot, hard to dent.",
    },
    "rune_plate": {
        "key": "rune_plate", "name": "Rune Plate", "emoji": "\U0001F6E1",
        "tier": 4, "def_bonus": 18, "price_rune": 600.0,
        "armor_type": "heavy",
        "blurb": "Sigil-bound steel.",
    },
    "dragon_plate": {
        "key": "dragon_plate", "name": "Dragon Plate", "emoji": "\U0001F6E1",
        "tier": 5, "def_bonus": 30, "price_rune": 2000.0,
        "armor_type": "heavy",
        "blurb": "Smithed from wyrmscale. Doesn't burn.",
    },
    "mythril_plate": {
        "key": "mythril_plate", "name": "Mythril Plate", "emoji": "\U0001F6E1",
        "tier": 6, "def_bonus": 48, "price_rune": 6000.0,
        "armor_type": "heavy",
        "blurb": "Mythril woven thin. Light, silver, indifferent to fire.",
    },
    "phoenix_mail": {
        "key": "phoenix_mail", "name": "Phoenix Mail", "emoji": "\U0001F6E1",
        "tier": 7, "def_bonus": 70, "price_rune": 16000.0,
        "armor_type": "heavy",
        "blurb": "Reignites itself when you take a fatal hit. Once.",
    },
    "void_plate": {
        "key": "void_plate", "name": "Void Plate", "emoji": "\U0001F6E1",
        "tier": 8, "def_bonus": 100, "price_rune": 45000.0,
        "armor_type": "heavy",
        "blurb": "Half there, half not. Half the damage gets through, the other half doesn't exist.",
    },
    "primordial_aegis": {
        "key": "primordial_aegis", "name": "Primordial Aegis", "emoji": "\U0001F6E1",
        "tier": 9, "def_bonus": 150, "price_rune": 130000.0,
        "armor_type": "heavy",
        "blurb": "Older than scale. Older than steel. Older than the idea of armor.",
    },
    "archons_aegis": {
        "key": "archons_aegis", "name": "Archon's Aegis", "emoji": "\U0001F6E1",
        "tier": 10, "def_bonus": 230, "price_rune": 380000.0,
        "armor_type": "heavy",
        "blurb": "Even the Archon couldn't break this on a tantrum day.",
    },
    "first_skin": {
        "key": "first_skin", "name": "First Skin", "emoji": "\U0001F6E1",
        "tier": 11, "def_bonus": 360, "price_rune": 1_100_000.0,
        "armor_type": "heavy",
        "blurb": "The endgame armor. The first thing The First made for itself.",
    },

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  Delve-only armor pieces (drop only from chests / mini-bosses /  ║
    # ║  bosses; delve_only=True hides them from the buy shop)           ║
    # ╚══════════════════════════════════════════════════════════════════╝
    "boneplate": {
        "key": "boneplate", "name": "Boneplate", "emoji": "\U0001F480",
        "tier": 4, "def_bonus": 22, "price_rune": 0.0,
        "armor_type": "heavy",
        "rarity": "rare", "delve_only": True,
        "affixes": {"vs_undead_def_pct": 0.20, "hp_pct": 0.05},
        "blurb": "Bone plates lashed over leather. Undead can't bring themselves to hit it hard.",
    },
    "shadow_cloak": {
        "key": "shadow_cloak", "name": "Shadow Cloak", "emoji": "\U0001F575",
        "tier": 5, "def_bonus": 28, "price_rune": 0.0,
        "armor_type": "medium",
        "rarity": "rare", "delve_only": True,
        "affixes": {"crit_pct": 0.05, "hp_pct": 0.10},
        "blurb": "Stitched from cooled shadow. Hugs you back.",
    },
    "archmage_robe": {
        "key": "archmage_robe", "name": "Archmage Robe", "emoji": "\U0001F9D9",
        "tier": 6, "def_bonus": 28, "price_rune": 0.0,
        "armor_type": "light",
        "rarity": "epic", "delve_only": True,
        "affixes": {"spell_dmg_pct": 0.15, "hp_pct": 0.05},
        "blurb": "Robe of an unaffiliated arch-magus. Channels spells like the staff is doing it for you.",
    },
    "vampiric_mail": {
        "key": "vampiric_mail", "name": "Vampiric Mail", "emoji": "\U0001F6E1",
        "tier": 6, "def_bonus": 38, "price_rune": 0.0,
        "armor_type": "heavy",
        "rarity": "epic", "delve_only": True,
        "affixes": {"lifesteal_pct": 0.05, "hp_pct": 0.10},
        "blurb": "Drinks back a sip from each mob it sees fall.",
    },
    "phoenix_garb": {
        "key": "phoenix_garb", "name": "Phoenix Garb", "emoji": "\U0001F525",
        "tier": 7, "def_bonus": 55, "price_rune": 0.0,
        "armor_type": "medium",
        "rarity": "epic", "delve_only": True,
        "affixes": {"hp_pct": 0.15, "vs_undead_def_pct": 0.10},
        "blurb": "Stitched from phoenix down. Never quite cools.",
    },
    "void_aegis": {
        "key": "void_aegis", "name": "Void Aegis", "emoji": "\U0001F300",
        "tier": 8, "def_bonus": 80, "price_rune": 0.0,
        "armor_type": "heavy",
        "rarity": "legendary", "delve_only": True,
        "affixes": {"hp_pct": 0.20, "vs_undead_def_pct": 0.15, "crit_pct": 0.05},
        "blurb": "Hammered out of an un-piece of nothing. Absorbs more than it deflects.",
    },
}

CONSUMABLES: dict[str, dict] = {
    "potion_minor": {
        "key": "potion_minor", "name": "Minor Potion", "emoji": "\U0001F9EA",
        "kind": "heal", "value": 0.25, "price_rune": 5.0,
        "blurb": "Heals 25% of max HP.",
    },
    "potion_major": {
        "key": "potion_major", "name": "Major Potion", "emoji": "\U0001F9EA",
        "kind": "heal", "value": 0.60, "price_rune": 18.0,
        "blurb": "Heals 60% of max HP.",
    },
    "elixir": {
        "key": "elixir", "name": "Elixir of Life", "emoji": "\U0001F9EA",
        "kind": "heal", "value": 1.0, "price_rune": 60.0,
        "blurb": "Full heal. Rare and pricey.",
    },
    "scroll_escape": {
        "key": "scroll_escape", "name": "Scroll of Escape", "emoji": "\U0001F4DC",
        "kind": "escape", "value": 1.0, "price_rune": 25.0,
        "blurb": "Walk away from any combat unharmed.",
    },
    "tame_charm": {
        "key": "tame_charm", "name": "Taming Charm", "emoji": "\U0001F9FF",
        "kind": "charm", "value": 0.20, "price_rune": 12.0,
        "blurb": "Boosts the next capture attempt by +20%.",
    },
    "pickaxe_oil": {
        "key": "pickaxe_oil", "name": "Pickaxe Oil", "emoji": "\U0001FA92",
        "kind": "mine_boost", "value": 0.50, "price_rune": 8.0,
        "blurb": "Next ,delve mine yields +50% ore.",
    },
    "rune_lure": {
        "key": "rune_lure", "name": "Rune Lure", "emoji": "\U0001FAA8",
        "kind": "lure", "value": 0.25, "price_rune": 4.0,
        "blurb": "25% chance the next room spawns a bonus mob.",
    },
    # ── Bigger consumables (deep-floor pricing) ──────────────────────────
    "potion_supreme": {
        "key": "potion_supreme", "name": "Supreme Potion", "emoji": "\U0001F9EA",
        "kind": "heal", "value": 0.80, "price_rune": 35.0,
        "blurb": "Heals 80% of max HP. Tastes like blue raspberry.",
    },
    "phoenix_down": {
        "key": "phoenix_down", "name": "Phoenix Down", "emoji": "\U0001F525",
        "kind": "revive", "value": 0.50, "price_rune": 250.0,
        "blurb": "On KO, auto-revives at 50% HP. Consumed once and gone.",
    },
    "greater_charm": {
        "key": "greater_charm", "name": "Greater Charm", "emoji": "\U0001F9FF",
        "kind": "charm", "value": 0.40, "price_rune": 40.0,
        "blurb": "Boosts the next capture attempt by +40%.",
    },
    "legendary_charm": {
        "key": "legendary_charm", "name": "Legendary Charm", "emoji": "\U00002728",
        "kind": "charm", "value": 0.60, "price_rune": 150.0,
        "blurb": "Boosts the next capture attempt by +60%. Practically guaranteed.",
    },
    "diamond_pickaxe": {
        "key": "diamond_pickaxe", "name": "Diamond Pickaxe Oil", "emoji": "\U0001F48E",
        "kind": "mine_boost", "value": 1.50, "price_rune": 60.0,
        "blurb": "Next mine action yields +150% ore. Cracks the rock open.",
    },
    "rune_siren": {
        "key": "rune_siren", "name": "Rune Siren", "emoji": "\U0001F47E",
        "kind": "lure", "value": 0.60, "price_rune": 25.0,
        "blurb": "60% chance the next room spawns a bonus mob.",
    },
    # ── Damage spells (new 'damage' kind, wired in services/dungeon.py) ──
    # Scrolls cast a one-shot offensive spell at the active mob. Damage
    # is scaled off the player's effective ATK so a low-level player
    # gets a smaller boom than a maxed-out mage. The mob still gets to
    # swing afterwards (unless the spell kills it), so scrolls are a
    # tempo tool, not an instakill button.
    "scroll_smite": {
        "key": "scroll_smite", "name": "Scroll of Smite", "emoji": "\U0001F4DC",
        "kind": "damage", "value": 4.0, "price_rune": 50.0,
        "blurb": "Deals 4x your ATK to the active mob. Holy.",
    },
    "scroll_chain_lightning": {
        "key": "scroll_chain_lightning", "name": "Scroll of Chain Lightning", "emoji": "\U000026A1",
        "kind": "damage", "value": 6.0, "price_rune": 120.0,
        "blurb": "Deals 6x your ATK. Smells like ozone.",
    },
    "scroll_meteor": {
        "key": "scroll_meteor", "name": "Scroll of Meteor", "emoji": "\U00002604",
        "kind": "damage", "value": 10.0, "price_rune": 400.0,
        "blurb": "Deals 10x your ATK. The room shakes for a beat after.",
    },
    "scroll_apocalypse": {
        "key": "scroll_apocalypse", "name": "Scroll of Apocalypse", "emoji": "\U0001F4A5",
        "kind": "damage", "value": 25.0, "price_rune": 2500.0,
        "blurb": "Deals 25x your ATK. One use. Probably overkill on a goblin.",
    },
    "scroll_unmake": {
        "key": "scroll_unmake", "name": "Scroll of Unmaking", "emoji": "\U0001F300",
        "kind": "damage", "value": 60.0, "price_rune": 18000.0,
        "blurb": "Deals 60x your ATK. The mob isn't 'killed', it's 'erased'.",
    },

    # ── Ranged ammo (consumed per swing by bows / crossbows) ─────────────
    # Each "bundle" stacks AMMO_PACK_SIZE shots in one inventory slot;
    # services/dungeon.py burns AMMO_PER_RANGED_SWING ammo per ranged
    # attack and falls back to OUT_OF_AMMO_DAMAGE_MULT damage when empty.
    "arrow_bundle": {
        "key": "arrow_bundle", "name": "Arrow Bundle", "emoji": "\U0001F3F9",
        "kind": "ammo", "value": 1.0, "price_rune": 6.0,
        "ammo_for": "bow", "pack_size": 20,
        "blurb": "20 fletched arrows. Bows pull from this stack per shot.",
    },
    "bolt_bundle": {
        "key": "bolt_bundle", "name": "Bolt Bundle", "emoji": "\U0001F3F9",
        "kind": "ammo", "value": 1.0, "price_rune": 7.0,
        "ammo_for": "crossbow", "pack_size": 20,
        "blurb": "20 forged bolts. Crossbows pull from this stack per shot.",
    },
    "broadhead_bundle": {
        "key": "broadhead_bundle", "name": "Broadhead Bundle", "emoji": "\U0001F3F9",
        "kind": "ammo", "value": 1.5, "price_rune": 22.0,
        "ammo_for": "bow", "pack_size": 15, "ammo_dmg_mult": 1.25,
        "blurb": "Wider heads, deeper wounds. +25% per-shot damage.",
    },
    "piercing_bolts": {
        "key": "piercing_bolts", "name": "Piercing Bolts", "emoji": "\U0001F3F9",
        "kind": "ammo", "value": 1.5, "price_rune": 26.0,
        "ammo_for": "crossbow", "pack_size": 15, "ammo_dmg_mult": 1.30,
        "blurb": "Hardened tips that punch through plate. +30% per-shot damage.",
    },

    # ── Class-flavored consumables ───────────────────────────────────────
    # New 'buff' kind applies a temporary in-room effect held in the
    # mob_state JSONB (mark_target -> next swing crits) or the player
    # state itself (volley_charged -> next basic attack fires 3 shots).
    # Druid 'wildshape_potion' / 'thorn_aura' lean on the heal-on-swing
    # + retaliation_dmg fields the engine reads in resolve_attack.
    "scroll_volley": {
        "key": "scroll_volley", "name": "Scroll of Volley", "emoji": "\U0001F3F9",
        "kind": "buff", "value": 3.0, "price_rune": 90.0,
        "buff": "volley_charged", "duration_rounds": 1,
        "blurb": "Next basic ranged attack fires 3 shots at once. Burns 3 ammo.",
    },
    "scroll_mark_target": {
        "key": "scroll_mark_target", "name": "Scroll of Mark Target", "emoji": "\U0001F3AF",
        "kind": "buff", "value": 1.0, "price_rune": 60.0,
        "buff": "marked_target", "duration_rounds": 3,
        "blurb": "Next 3 attacks against the active mob auto-crit.",
    },
    "thorn_aura_brew": {
        "key": "thorn_aura_brew", "name": "Thorn Aura Brew", "emoji": "\U0001F33F",
        "kind": "buff", "value": 0.30, "price_rune": 70.0,
        "buff": "thorn_aura", "duration_rounds": 4,
        "blurb": "Reflect 30% of melee damage back at the attacker for 4 rounds.",
    },
    "wildshape_potion": {
        "key": "wildshape_potion", "name": "Wildshape Potion", "emoji": "\U0001F43B",
        "kind": "buff", "value": 0.50, "price_rune": 110.0,
        "buff": "wildshape", "duration_rounds": 3,
        "blurb": "+50% ATK and heal 5% max HP per turn for 3 rounds. Druids only feel right doing this.",
    },
    "regrowth_brew": {
        "key": "regrowth_brew", "name": "Regrowth Brew", "emoji": "\U0001F33A",
        "kind": "regen", "value": 0.10, "price_rune": 35.0,
        "duration_rounds": 5,
        "blurb": "Heals 10% of max HP at the start of each round for 5 rounds.",
    },
    "mana_draught": {
        "key": "mana_draught", "name": "Mana Draught", "emoji": "\U0001F9EA",
        "kind": "skill_reset", "value": 1.0, "price_rune": 80.0,
        "blurb": "Resets your class-skill cooldown to 0. Smells faintly of crackling air.",
    },
    "scroll_sanctuary": {
        "key": "scroll_sanctuary", "name": "Scroll of Sanctuary", "emoji": "\U0001F4DC",
        "kind": "buff", "value": 0.50, "price_rune": 150.0,
        "buff": "sanctuary", "duration_rounds": 2,
        "blurb": "Halve incoming damage for 2 rounds. The dungeon almost wants you alive.",
    },
}


# ============================================================================
# Class / equipment helpers
# ============================================================================
# These helpers wrap class -> weapon/armor compatibility checks so the
# cog and service never duplicate the lookup. Adding a new class is then
# a config-only change: append to CLASSES with weapon_types/armor_types
# and the equip + reroll paths immediately respect the restriction.

def class_weapon_types(class_key: str) -> tuple[str, ...]:
    """Return the tuple of weapon_type strings a class can wield."""
    meta = class_meta(class_key) or {}
    return tuple(meta.get("weapon_types") or ())


def class_armor_types(class_key: str) -> tuple[str, ...]:
    """Return the tuple of armor_type strings a class can wear."""
    meta = class_meta(class_key) or {}
    return tuple(meta.get("armor_types") or ())


def weapon_allowed_for_class(weapon_key: str, class_key: str) -> bool:
    """True iff the class's weapon_types includes the weapon's type."""
    w = weapon_meta(weapon_key) or {}
    if not w:
        return False
    wt = str(w.get("weapon_type") or "")
    return wt in class_weapon_types(class_key)


def armor_allowed_for_class(armor_key: str, class_key: str) -> bool:
    """True iff the class's armor_types includes the armor's type."""
    a = armor_meta(armor_key) or {}
    if not a:
        return False
    at = str(a.get("armor_type") or "")
    return at in class_armor_types(class_key)


def starter_weapon_for_class(class_key: str) -> str:
    """Default weapon for a freshly-picked or rerolled class."""
    meta = class_meta(class_key) or {}
    return str(meta.get("starter_weapon") or "rusty_dagger")


def starter_armor_for_class(class_key: str) -> str:
    """Default armor for a freshly-picked or rerolled class."""
    meta = class_meta(class_key) or {}
    return str(meta.get("starter_armor") or "cloth_tunic")


def is_ranged_weapon(weapon_key: str) -> bool:
    """True if the weapon's attack_kind is 'ranged' (bow / crossbow)."""
    w = weapon_meta(weapon_key) or {}
    return str(w.get("attack_kind") or "melee") == "ranged"


def weapon_ammo_key(weapon_key: str) -> str | None:
    """Consumable key the weapon pulls per shot, or None for melee."""
    w = weapon_meta(weapon_key) or {}
    return w.get("ammo_key") or None


def stat_points_available(level: int, hp: int, atk: int, spd: int, int_: int) -> int:
    """Unspent stat points for a given delve level + alloc tuple."""
    earned = max(0, int(level or 0) * STAT_POINTS_PER_LEVEL)
    spent = max(0, int(hp or 0) + int(atk or 0) + int(spd or 0) + int(int_ or 0))
    return max(0, earned - spent)



# ============================================================================
# Helper functions
# ============================================================================

def xp_for_level(level: int) -> int:
    """Total XP threshold to BE level ``level``. Level 1 = 0 XP."""
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
    """Inverse of xp_for_level. Capped at MAX_LEVEL."""
    if xp <= 0:
        return 1
    lvl = 1
    span = float(XP_BASE)
    cur = 0.0
    while lvl < MAX_LEVEL and cur + span <= xp:
        cur += span
        span *= XP_GROWTH
        lvl += 1
    return lvl


# Per-delve-level payout boost on out-of-run gathers (scavenge). Lv 1 =
# 1.0x, Lv 50 = ~1.49x. Mirrors farming_config.level_payout_mult and
# fishing_config.level_payout_mult so the three out-of-run gathers all
# scale consistently with player progression.
DELVE_LEVEL_PAYOUT_PER_LEVEL: float = 0.01


def level_payout_mult(level: int) -> float:
    """Per-delve-level payout boost. Lv 1 = 1.0x, Lv 50 = ~1.49x."""
    return 1.0 + max(0, int(level) - 1) * DELVE_LEVEL_PAYOUT_PER_LEVEL


def xp_to_next(xp: int) -> tuple[int, int]:
    """Return (xp_into_level, span_to_next). At MAX_LEVEL returns (0, 0)."""
    lvl = level_from_xp(xp)
    if lvl >= MAX_LEVEL:
        return 0, 0
    base = xp_for_level(lvl)
    nxt  = xp_for_level(lvl + 1)
    return int(xp - base), int(nxt - base)


def floor_meta(depth: int) -> dict:
    """Return FLOORS[depth], capping at MAX_FLOOR; falls back to depth 1."""
    if depth < 1:
        depth = 1
    if depth > MAX_FLOOR:
        depth = MAX_FLOOR
    return FLOORS.get(depth) or FLOORS.get(1) or {}


def mob_meta(key: str) -> dict | None:
    """Look up a mob by key. Falls through to the mini-boss catalog
    so any caller (combat, display, achievements) that hands us a
    mini-boss key still resolves to a meta dict rather than None.

    The two key spaces are disjoint by convention -- never reuse a
    mob key for a mini-boss or vice versa.
    """
    if not key:
        return None
    k = str(key)
    return MOBS.get(k) or MINI_BOSSES.get(k)


def weapon_meta(key: str) -> dict | None:
    return WEAPONS.get(key)


def armor_meta(key: str) -> dict | None:
    return ARMOR.get(key)


def consumable_meta(key: str) -> dict | None:
    return CONSUMABLES.get(key)


def class_meta(key: str) -> dict | None:
    return CLASSES.get(key)


def is_boss_floor(depth: int) -> bool:
    meta = FLOORS.get(depth) or {}
    return bool(meta.get("boss")) and (depth % BOSS_FLOOR_INTERVAL == 0)


def _weighted_pick(pool: tuple, weights: tuple, rng: random.Random) -> str:
    if not pool:
        return ""
    total = sum(weights) or 1
    r = rng.random() * total
    acc = 0.0
    for key, w in zip(pool, weights):
        acc += w
        if r <= acc:
            return key
    return pool[-1]


def pick_mob_for_floor(depth: int, rng: random.Random) -> str:
    meta = floor_meta(depth)
    return _weighted_pick(
        tuple(meta.get("mob_pool") or ()),
        tuple(meta.get("mob_pool_weights") or ()),
        rng,
    )


def pick_ore_for_floor(depth: int, rng: random.Random) -> str:
    meta = floor_meta(depth)
    return _weighted_pick(
        tuple(meta.get("ore_pool") or (COPPER_SYMBOL,)),
        tuple(meta.get("ore_pool_weights") or (1,)),
        rng,
    )


def mine_qty_roll(depth: int, ore_symbol: str, rng: random.Random) -> float:
    """Base qty * (1 + 0.10 * depth) * uniform(0.6, 1.4) * tier_scale."""
    tier_scale = {COPPER_SYMBOL: 1.0, SILVER_SYMBOL: 0.40, GOLD_SYMBOL: 0.10}
    scale = tier_scale.get(ore_symbol, 1.0)
    base = MINE_BASE_QTY * (1.0 + 0.10 * max(0, depth)) * scale
    return float(base * rng.uniform(0.6, 1.4))


def capture_chance(mob_key: str, hp_pct: float, charm: bool) -> float:
    """0.0 above CAPTURE_HP_THRESHOLD; tier-scaled below it."""
    if hp_pct > CAPTURE_HP_THRESHOLD:
        return 0.0
    meta = MOBS.get(mob_key) or {}
    tier = max(1, int(meta.get("tier") or 1))
    hp_factor = 1.0 - (hp_pct / max(1e-9, CAPTURE_HP_THRESHOLD)) * 0.5
    base = CAPTURE_BASE_CHANCE * hp_factor
    base -= (tier - 1) * CAPTURE_PER_TIER_PENALTY
    if charm:
        base += 0.20
    return max(0.02, min(0.95, base))


# ============================================================================
#  Relics
# ============================================================================
#
# Passive items found rarely in deep-floor chests. Players equip ONE relic at
# a time via ``,delve relic equip <key>`` and its effect dict folds into
# combat / mining / drop calculations server-side. Effect keys are scanned
# at the call sites that need them (e.g. player_combat_stats reads
# ``hp_max_mult`` / ``spd_bonus`` / ``crit_bonus`` / ``int_dmg_mult``,
# mine_ore reads ``mine_yield_mult``, _credit_rune reads ``rune_drop_mult``,
# combat reads ``lifesteal_pct``).
#
# Drop logic: each chest opened past ``RELIC_MIN_FLOOR`` rolls a relic at
# ``RELIC_DROP_BASE_CHANCE`` (scaled up linearly with floor depth and capped).
# Relic rarity is drawn from RELIC_DROP_WEIGHTS so legendaries are genuinely
# rare without ever being unreachable.

RELIC_MIN_FLOOR:        Final[int]   = 5
RELIC_DROP_BASE_CHANCE: Final[float] = 0.06
RELIC_DROP_DEPTH_BONUS: Final[float] = 0.005   # +0.5% per floor past min
RELIC_DROP_MAX_CHANCE:  Final[float] = 0.30

RELIC_DROP_WEIGHTS: Final[dict[str, float]] = {
    "common":    50.0,
    "uncommon":  28.0,
    "rare":      14.0,
    "epic":       6.0,
    "legendary":  2.0,
}

RELICS: Final[dict[str, dict]] = {
    "miners_charm": {
        "key": "miners_charm", "name": "Miner's Charm",
        "emoji": "\U000026CF",                              # pickaxe
        "rarity": "common",
        "blurb": "Tugged from a forgotten vein. Veins yield more.",
        "effects": {"mine_yield_mult": 1.25},
    },
    "lucky_coin": {
        "key": "lucky_coin", "name": "Lucky Coin",
        "emoji": "\U0001FA99",                               # coin
        "rarity": "common",
        "blurb": "Some swings just land cleaner with this in your pouch.",
        "effects": {"crit_bonus": 0.08},
    },
    "iron_heart": {
        "key": "iron_heart", "name": "Iron Heart",
        "emoji": "\U0001FAC0",                               # anatomical heart
        "rarity": "uncommon",
        "blurb": "A still-warm relic. Beats with you.",
        "effects": {"hp_max_mult": 1.15},
    },
    "swiftboots": {
        "key": "swiftboots", "name": "Swiftboots",
        "emoji": "\U0001F462",                               # boot
        "rarity": "uncommon",
        "blurb": "Light enough you forget you're wearing them.",
        "effects": {"spd_bonus": 0.06},
    },
    "runes_eye": {
        "key": "runes_eye", "name": "Rune's Eye",
        "emoji": "\U0001F441",                               # eye
        "rarity": "rare",
        "blurb": "Sees the runes hidden in every kill.",
        "effects": {"rune_drop_mult": 1.25},
    },
    "vampire_fang": {
        "key": "vampire_fang", "name": "Vampire Fang",
        "emoji": "\U0001F9DB",                               # vampire
        "rarity": "rare",
        "blurb": "Drinks back what it strikes.",
        "effects": {"lifesteal_pct": 0.08},
    },
    "arcane_focus": {
        "key": "arcane_focus", "name": "Arcane Focus",
        "emoji": "\U0001F52E",                               # crystal ball
        "rarity": "epic",
        "blurb": "Spells bite deeper through this prism.",
        "effects": {"int_dmg_mult": 1.30, "crit_bonus": 0.05},
    },
    "thorn_aegis": {
        "key": "thorn_aegis", "name": "Thorn Aegis",
        "emoji": "\U0001F6E1",                               # shield
        "rarity": "epic",
        "blurb": "Hits feed the briar. The briar bites back.",
        "effects": {"thorns_pct": 0.20, "hp_max_mult": 1.05},
    },
    "godslayer_eye": {
        "key": "godslayer_eye", "name": "Godslayer's Eye",
        "emoji": "\U0001F441‍\U0001F5E8",              # eye in speech
        "rarity": "legendary",
        "blurb": "Found behind a sealed door. Of course it works.",
        "effects": {
            "crit_bonus": 0.15,
            "rune_drop_mult": 1.50,
            "mine_yield_mult": 1.20,
            "lifesteal_pct": 0.05,
        },
    },
}


def relic_meta(key: str | None) -> dict | None:
    if not key:
        return None
    return RELICS.get(str(key).lower())


def relic_drop_chance(floor: int) -> float:
    """Per-chest relic drop probability at a given floor depth."""
    if floor < RELIC_MIN_FLOOR:
        return 0.0
    bonus = (floor - RELIC_MIN_FLOOR) * RELIC_DROP_DEPTH_BONUS
    return min(RELIC_DROP_MAX_CHANCE, RELIC_DROP_BASE_CHANCE + bonus)


def roll_relic(floor: int, rng: random.Random) -> str | None:
    """Roll a relic drop for a chest at ``floor``. Returns None if no drop."""
    chance = relic_drop_chance(floor)
    if chance <= 0.0 or rng.random() >= chance:
        return None
    rarity = _weighted_pick(
        tuple(RELIC_DROP_WEIGHTS.keys()),
        tuple(RELIC_DROP_WEIGHTS.values()),
        rng,
    )
    pool = [k for k, v in RELICS.items() if v.get("rarity") == rarity]
    if not pool:
        # Fallback to any relic if the rarity bucket is empty so a roll
        # never silently fails.
        pool = list(RELICS.keys())
    return rng.choice(pool)


def relic_effect(relic_key: str | None, effect_key: str, default: float = 0.0) -> float:
    """Read one effect off the equipped relic. Returns ``default`` if the
    relic doesn't define that effect (or no relic is equipped)."""
    meta = relic_meta(relic_key)
    if not meta:
        return float(default)
    effects = dict(meta.get("effects") or {})
    return float(effects.get(effect_key, default))


# ============================================================================
#  Cursed Runs
# ============================================================================
#
# Optional self-imposed run modifier. Players activate via ``,delve curse
# <key>`` BEFORE ``,delve start``; the curse name persists on
# user_dungeon.run_curse and clears on rest. Each curse increases run
# difficulty (mob HP/damage, etc.) in exchange for a flat reward multiplier
# on RUNE drops, ore yield, and chest payouts. Lifetime completions track
# on user_dungeon.total_curses_completed.

RUN_CURSES: Final[dict[str, dict]] = {
    "bloodmoon": {
        "key": "bloodmoon", "name": "Bloodmoon",
        "emoji": "\U0001F315",                               # full moon
        "blurb": "Mobs hit harder. Drops bleed deeper.",
        "mob_dmg_mult": 1.30,
        "mob_hp_mult":  1.00,
        "rune_mult":    1.50,
        "ore_mult":     1.25,
        "chest_mult":   1.50,
    },
    "frenzy": {
        "key": "frenzy", "name": "Frenzy",
        "emoji": "\U0001F525",                               # fire
        "blurb": "Beasts overflow with HP. Kills overflow with RUNE.",
        "mob_dmg_mult": 1.05,
        "mob_hp_mult":  1.50,
        "rune_mult":    1.60,
        "ore_mult":     1.10,
        "chest_mult":   1.20,
    },
    "famine": {
        "key": "famine", "name": "Famine",
        "emoji": "\U0001F35E",                               # bread (struck out)
        "blurb": "No potions allowed. Rewards heaped on what survives.",
        "mob_dmg_mult": 1.10,
        "mob_hp_mult":  1.10,
        "rune_mult":    2.00,
        "ore_mult":     1.40,
        "chest_mult":   2.00,
        "block_potions": True,
    },
    "abyssal": {
        "key": "abyssal", "name": "Abyssal Pact",
        "emoji": "\U0001F300",                               # cyclone
        "blurb": "The deep wants you. It will pay for the chance.",
        "mob_dmg_mult": 1.50,
        "mob_hp_mult":  1.50,
        "rune_mult":    2.50,
        "ore_mult":     1.75,
        "chest_mult":   2.50,
    },
}


def curse_meta(key: str | None) -> dict | None:
    if not key:
        return None
    return RUN_CURSES.get(str(key).lower())


def curse_mult(curse_key: str | None, effect: str, default: float = 1.0) -> float:
    """Read a multiplier off the active curse, defaulting to ``default``
    (1.0 for the *_mult fields, 0.0 for booleans coerced to floats)."""
    meta = curse_meta(curse_key)
    if not meta:
        return float(default)
    return float(meta.get(effect, default))


# ============================================================================
#  Shrine boons
# ============================================================================
#
# When a player runs ``,delve pray`` in a shrine room, one of these boons
# fires. Outcomes split between blessings (most outcomes; HP, RUNE, ATK
# buff, free relic) and small curses (HP cost in exchange for the next
# chest paying double). Catalog is data-only; the resolver in services
# branches on ``kind`` and applies effects.

SHRINE_OUTCOME_WEIGHTS: Final[dict[str, float]] = {
    "full_heal":      30.0,
    "rune_pile":      22.0,
    "atk_blessing":   18.0,
    "swift_blessing": 12.0,
    "relic_gift":      8.0,
    "shrine_curse":   10.0,
}

SHRINE_BOONS: Final[dict[str, dict]] = {
    "full_heal": {
        "key": "full_heal", "name": "Mending Light",
        "kind": "heal_full", "blurb": "Warm light closes every wound.",
    },
    "rune_pile": {
        "key": "rune_pile", "name": "Rune Offering",
        "kind": "rune", "amount_min": 5.0, "amount_max": 80.0,
        "depth_scale": 0.20,    # +20% per floor depth
        "blurb": "A pile of runes spills from the altar.",
    },
    "atk_blessing": {
        "key": "atk_blessing", "name": "Smiting Blessing",
        "kind": "buff", "buff_key": "shrine_atk",
        "value": 0.40, "duration": 6,
        "blurb": "Your next swings hit 40% harder for 6 rounds.",
    },
    "swift_blessing": {
        "key": "swift_blessing", "name": "Swift Blessing",
        "kind": "buff", "buff_key": "shrine_spd",
        "value": 0.20, "duration": 6,
        "blurb": "+20% effective speed for 6 rounds (more first-strikes).",
    },
    "relic_gift": {
        "key": "relic_gift", "name": "Relic of the Old Faith",
        "kind": "relic", "min_floor": 1,
        "blurb": "A relic materializes in your pack.",
    },
    "shrine_curse": {
        "key": "shrine_curse", "name": "Cracked Promise",
        "kind": "curse", "hp_cost_pct": 0.25,
        "blurb": "The shrine bites: -25% HP, but a debt accrues.",
    },
}


def shrine_meta(key: str | None) -> dict | None:
    if not key:
        return None
    return SHRINE_BOONS.get(str(key).lower())


def roll_shrine_outcome(rng: random.Random) -> str:
    """Weighted pick from SHRINE_OUTCOME_WEIGHTS."""
    keys = list(SHRINE_OUTCOME_WEIGHTS.keys())
    weights = [SHRINE_OUTCOME_WEIGHTS[k] for k in keys]
    return _weighted_pick(tuple(keys), tuple(weights), rng)


# ============================================================================
#  Junk + craft mats + usable item drops
# ============================================================================
#
# Mirrors fishing's JUNK system: a counter-dict inventory of low-stakes
# salvage and breadcrumb items dropped from combat / chests / mining.
# Three flavors:
#   * salvage: trash that only sells for RUNE (broken gear, torn cloth)
#   * mat:     craft material (no ,craft consumer yet -- forward-compat)
#   * usable:  in-run item the player can ``,delve use <key>`` for an
#              effect (heal / ammo / crit-charm / smoke escape)
#
# Drop rolls run AFTER combat wins, on chest opens, and on mining --
# always as a SECONDARY drop, never replacing the primary loot. The
# weighted catalog below biases toward trash; mats are mid-rare; usables
# are rare.

JUNK_DROP_BASE_CHANCE: Final[float] = 0.32       # per qualifying event
JUNK_DROP_DEPTH_BONUS: Final[float] = 0.005      # +0.5% per floor
JUNK_DROP_MAX_CHANCE:  Final[float] = 0.65

# Min-floor gates per kind so a tier-1 goblin doesn't drop dragon scales.
JUNK_KIND_FLOOR_GATE: Final[dict[str, int]] = {
    "salvage": 1,
    "mat":     5,
    "usable":  3,
}

JUNK: Final[dict[str, dict]] = {
    # ---- Salvage: pure sellable trash, 1 RUNE per unit-ish ---------------
    "broken_blade": {
        "key": "broken_blade", "name": "Broken Blade",
        "emoji": "\U0001F5E1\U0000FE0F",
        "kind": "salvage", "salvage_rune": 0.50,
        "blurb": "Snapped at the hilt. Still kinda pointy.",
    },
    "torn_cloth": {
        "key": "torn_cloth", "name": "Torn Cloth",
        "emoji": "\U0001F9F5",
        "kind": "salvage", "salvage_rune": 0.30,
        "blurb": "Once a tunic. Now a rag.",
    },
    "rusted_buckle": {
        "key": "rusted_buckle", "name": "Rusted Buckle",
        "emoji": "\U0001FA9D",
        "kind": "salvage", "salvage_rune": 0.40,
        "blurb": "Pried off some long-dead adventurer's belt.",
    },
    "cracked_shield": {
        "key": "cracked_shield", "name": "Cracked Shield",
        "emoji": "\U0001F6E1\U0000FE0F",
        "kind": "salvage", "salvage_rune": 0.80,
        "blurb": "Heavy. Useless. Pawnable.",
    },
    "tarnished_coin": {
        "key": "tarnished_coin", "name": "Tarnished Coin",
        "emoji": "\U0001FA99",
        "kind": "salvage", "salvage_rune": 1.20,
        "blurb": "Pre-Crypt minting. Collectors care; you do not.",
    },
    "moldy_tome": {
        "key": "moldy_tome", "name": "Moldy Tome",
        "emoji": "\U0001F4D6",
        "kind": "salvage", "salvage_rune": 1.50,
        "blurb": "Pages stuck together. Smells like a basement.",
    },
    "skull_token": {
        "key": "skull_token", "name": "Skull Token",
        "emoji": "\U0001F480",
        "kind": "salvage", "salvage_rune": 2.50,
        "blurb": "Tier-locked merchant trinket.",
    },
    # ---- Craft mats (no ,craft consumer yet, but priced + collectable) ---
    "monster_fang": {
        "key": "monster_fang", "name": "Monster Fang",
        "emoji": "\U0001F9B7",
        "kind": "mat", "salvage_rune": 4.00,
        "blurb": "Forge mat. Curiously sharp.",
    },
    "ectoplasm": {
        "key": "ectoplasm", "name": "Ectoplasm",
        "emoji": "\U0001F47B",
        "kind": "mat", "salvage_rune": 5.00,
        "blurb": "Slick, cold, alive in your pouch.",
    },
    "bone_fragment": {
        "key": "bone_fragment", "name": "Bone Fragment",
        "emoji": "\U0001F9B4",
        "kind": "mat", "salvage_rune": 3.50,
        "blurb": "Whose? Yours, now.",
    },
    "glowing_crystal": {
        "key": "glowing_crystal", "name": "Glowing Crystal",
        "emoji": "\U0001F48E",
        "kind": "mat", "rarity": "uncommon", "salvage_rune": 8.00,
        "blurb": "Hums faintly. Probably fine.",
    },
    "dragon_scale": {
        "key": "dragon_scale", "name": "Dragon Scale",
        "emoji": "\U0001F432",
        "kind": "mat", "rarity": "rare", "salvage_rune": 18.00,
        "blurb": "Rare. Pretty. Heavy.",
    },
    # ---- Usables: ,delve use <key> applies an in-run effect ---------------
    # Effects route through services.dungeon.use_consumable on a parallel
    # path -- the JUNK usable kinds resolve to the same on-screen feels
    # (heal, escape, ammo bundle) without sharing the consumables shop
    # catalog so they read as found-loot rather than purchasable consumables.
    "healing_herb": {
        "key": "healing_herb", "name": "Healing Herb",
        "emoji": "\U0001F33F",
        "kind": "usable", "use_kind": "heal", "use_value": 0.30,
        "salvage_rune": 2.00,
        "blurb": "Chew or sell. Heals 30% HP.",
    },
    "smoke_bomb": {
        "key": "smoke_bomb", "name": "Smoke Bomb",
        "emoji": "\U0001F4A8",
        "kind": "usable", "use_kind": "escape", "use_value": 0.0,
        "salvage_rune": 5.00,
        "blurb": "Pops white smoke -- bail out of any non-boss fight.",
    },
    "lucky_charm": {
        "key": "lucky_charm", "name": "Lucky Charm",
        "emoji": "\U0001F340",
        "kind": "usable", "use_kind": "buff_crit", "use_value": 0.20,
        "use_duration": 5,
        "salvage_rune": 6.00,
        "blurb": "+20% crit for 5 rounds.",
    },
    "scrap_arrow_bundle": {
        "key": "scrap_arrow_bundle", "name": "Scrap Arrow Bundle",
        "emoji": "\U0001F3F9",
        "kind": "usable", "use_kind": "ammo", "use_value": 10,
        "ammo_key": "arrow_bundle",
        "salvage_rune": 3.00,
        "blurb": "Salvaged shafts. Ten arrows for any bow.",
    },
    # ---- New common-tier salvage ------------------------------------------
    "rotting_pelt": {
        "key": "rotting_pelt", "name": "Rotting Pelt",
        "emoji": "\U0001F43A",
        "kind": "salvage", "salvage_rune": 0.45,
        "blurb": "Smells like a dead thing. Sells like one too.",
    },
    "broken_arrow": {
        "key": "broken_arrow", "name": "Broken Arrow",
        "emoji": "\U0001F3F9",
        "kind": "salvage", "salvage_rune": 0.55,
        "blurb": "Snapped shaft. The fletching's intact, at least.",
    },
    "cracked_helm": {
        "key": "cracked_helm", "name": "Cracked Helm",
        "emoji": "\U0001FA96",
        "kind": "salvage", "salvage_rune": 1.10,
        "blurb": "Big ding right above the eye-slit.",
    },
    "ash_dust": {
        "key": "ash_dust", "name": "Ash Dust",
        "emoji": "\U0001F32B",
        "kind": "salvage", "salvage_rune": 0.35,
        "blurb": "Whatever burned to make this is best left unsaid.",
    },
    # ---- Uncommon-tier mats (rarer drops, better salvage rate) -----------
    "enchanted_thread": {
        "key": "enchanted_thread", "name": "Enchanted Thread",
        "emoji": "\U0001F9F5",
        "kind": "mat", "rarity": "uncommon", "salvage_rune": 9.00,
        "blurb": "Glints faintly. Tailors will pay double.",
    },
    "runed_chip": {
        "key": "runed_chip", "name": "Runed Chip",
        "emoji": "\U0001F4AC",
        "kind": "mat", "rarity": "uncommon", "salvage_rune": 11.00,
        "blurb": "Fragment of a shattered focus crystal.",
    },
    "beast_blood": {
        "key": "beast_blood", "name": "Beast Blood",
        "emoji": "\U0001FA78",
        "kind": "mat", "rarity": "uncommon", "salvage_rune": 8.00,
        "blurb": "Still warm. Smiths use it for tempering.",
    },
    "mana_dust": {
        "key": "mana_dust", "name": "Mana Dust",
        "emoji": "\U0001F4A0",
        "kind": "mat", "rarity": "uncommon", "salvage_rune": 12.00,
        "blurb": "Powder-fine, faintly luminescent. Pinches of it scrub clean a spell-cast.",
    },
    # ---- Rare-tier mats (boss / mini-boss drops only) --------------------
    "void_essence": {
        "key": "void_essence", "name": "Void Essence",
        "emoji": "\U0001F300",
        "kind": "mat", "rarity": "rare", "salvage_rune": 28.00,
        "blurb": "Smaller than a pebble; weighs more than the bag.",
    },
    "phoenix_feather": {
        "key": "phoenix_feather", "name": "Phoenix Feather",
        "emoji": "\U0001F526",
        "kind": "mat", "rarity": "rare", "salvage_rune": 35.00,
        "blurb": "Always warm. Re-grows itself overnight if you let it.",
    },
    "dragon_heart_fragment": {
        "key": "dragon_heart_fragment", "name": "Dragon-Heart Fragment",
        "emoji": "\U0001F525",
        "kind": "mat", "rarity": "rare", "salvage_rune": 42.00,
        "blurb": "Beats once a day, on its own.",
    },
    # ---- Rare usables (delve-only) ---------------------------------------
    "blink_dust": {
        "key": "blink_dust", "name": "Blink Dust",
        "emoji": "\U0001F4A8",
        "kind": "usable", "rarity": "rare",
        "use_kind": "escape", "use_value": 0.0,
        "salvage_rune": 14.00,
        "blurb": "Pinches of starless dust -- escape any non-boss in a wink.",
    },
    "elder_potion": {
        "key": "elder_potion", "name": "Elder Potion",
        "emoji": "\U0001F9EA",
        "kind": "usable", "rarity": "rare",
        "use_kind": "heal", "use_value": 1.0,
        "salvage_rune": 22.00,
        "blurb": "Distilled from elder roots. Restores 100% of max HP.",
    },
    "warding_charm": {
        "key": "warding_charm", "name": "Warding Charm",
        "emoji": "\U0001F340",
        "kind": "usable", "rarity": "rare",
        "use_kind": "buff_crit", "use_value": 0.35,
        "use_duration": 5,
        "salvage_rune": 20.00,
        "blurb": "+35% SPD/crit for 5 rounds. The good charm.",
    },
}

# Per-kind drop weights -- sums normalized at roll time. Salvage is most
# common, mat mid, usable rare. Floor gating still applies on top.
JUNK_KIND_WEIGHTS: Final[dict[str, float]] = {
    "salvage": 60.0,
    "mat":     30.0,
    "usable":  10.0,
}


def junk_meta(key: str | None) -> dict | None:
    if not key:
        return None
    return JUNK.get(str(key).lower())


def junk_drop_chance(floor: int, *, source: str = "combat") -> float:
    """Probability that a qualifying event drops one junk item.

    ``source`` lets us tune chest/mine drops slightly differently from
    combat -- chests already drop relics + RUNE so junk fires at the
    base rate; combat fires more often (it's the bulk of player time).
    Mining is the lightest path (already pays raw ore + scaled depth).
    """
    base = JUNK_DROP_BASE_CHANCE
    if source == "chest":
        base *= 0.75
    elif source == "mine":
        base *= 0.40
    bonus = max(0, floor - 1) * JUNK_DROP_DEPTH_BONUS
    return min(JUNK_DROP_MAX_CHANCE, base + bonus)


def roll_junk_drop(floor: int, rng: random.Random, *, source: str = "combat") -> str | None:
    """Roll a junk-key drop for a qualifying event. Returns None if no drop."""
    chance = junk_drop_chance(floor, source=source)
    if chance <= 0.0 or rng.random() >= chance:
        return None
    # Pick a kind that the floor unlocks.
    kinds_eligible = [
        k for k, gate in JUNK_KIND_FLOOR_GATE.items()
        if floor >= int(gate)
    ]
    if not kinds_eligible:
        return None
    weights = [float(JUNK_KIND_WEIGHTS.get(k, 1.0)) for k in kinds_eligible]
    kind = rng.choices(kinds_eligible, weights=weights, k=1)[0]
    pool = [k for k, m in JUNK.items() if str(m.get("kind") or "") == kind]
    if not pool:
        return None
    return rng.choice(pool)


__all__ = (
    "MAX_FLOOR", "MAX_LEVEL", "STARTING_HP", "HP_PER_LEVEL",
    "XP_BASE", "XP_GROWTH", "RUN_COOLDOWN_S", "ACTION_COOLDOWN_S",
    "BATTLE_MAX_ROUNDS", "FLEE_BASE_CHANCE", "FLEE_HP_PENALTY_PCT",
    "CAPTURE_HP_THRESHOLD", "CAPTURE_BASE_CHANCE", "CAPTURE_PER_TIER_PENALTY",
    "MAX_PARTY_SIZE", "MINE_BASE_QTY", "BOSS_FLOOR_INTERVAL",
    "CRIT_BASE", "CRIT_SPD_SCALE", "CRIT_MULT",
    "BUDDY_ASSIST_DAMAGE_FRACTION", "BUDDY_ASSIST_TURN_CHANCE",
    "STAT_POINTS_PER_LEVEL", "STAT_POINT_HP_BONUS", "STAT_POINT_ATK_BONUS",
    "STAT_POINT_SPD_BONUS", "STAT_POINT_INT_BONUS",
    "RESPEC_BASE_PRICE_USD", "RESPEC_GROWTH", "respec_cost_usd",
    "CLASS_REROLL_BASE_USD", "CLASS_REROLL_GROWTH", "CLASS_REROLL_COOLDOWN_S",
    "RANGED_FIRST_STRIKE", "RANGED_RETALIATION_MULT", "RANGED_CRIT_BONUS",
    "OUT_OF_AMMO_DAMAGE_MULT", "AMMO_PER_RANGED_SWING",
    "WEAPON_TYPES", "ARMOR_TYPES", "RANGED_WEAPON_TYPES", "WEAPON_TYPE_AMMO_KEY",
    "CRYPT_NETWORK_SHORT",
    "COPPER_SYMBOL", "SILVER_SYMBOL", "GOLD_SYMBOL", "RUNE_SYMBOL",
    "ORE_SYMBOLS",
    "COPPER_EMOJI", "SILVER_EMOJI", "GOLD_EMOJI", "RUNE_EMOJI",
    "ORE_STAKE_RUNE_PER_DAY",
    "ORE_BURN_LP_REWARD_BPS", "RUNE_CASHOUT_LP_REWARD_BPS",
    "SCAVENGE_COOLDOWN_S", "SCAVENGE_OUTCOME_WEIGHTS",
    "SCAVENGE_PAYOUTS", "SCAVENGE_CONSUMABLE_POOL",
    "SCAVENGE_CONSUMABLE_QTY", "SCAVENGE_CONSUMABLE_PICKS",
    "SCAVENGE_SCROLL_POOL", "SCAVENGE_SCROLL_QTY", "SCAVENGE_RELIC_QTY",
    "roll_scavenge_outcome",
    "FRAMES", "CLASSES", "MOBS", "FLOORS",
    "WEAPONS", "ARMOR", "CONSUMABLES",
    "RELICS", "RUN_CURSES", "SHRINE_BOONS", "SHRINE_OUTCOME_WEIGHTS",
    "JUNK", "JUNK_KIND_WEIGHTS", "JUNK_KIND_FLOOR_GATE",
    "JUNK_DROP_BASE_CHANCE", "JUNK_DROP_DEPTH_BONUS", "JUNK_DROP_MAX_CHANCE",
    "junk_meta", "junk_drop_chance", "roll_junk_drop",
    "RARITY_TIERS", "RARITY_RANK", "BASE_STAT_FACTOR",
    "RARITY_STAT_MULT", "RARITY_JUNK_RUNE_MULT", "RARITY_PRICE_MULT",
    "item_rarity", "effective_atk_bonus", "effective_def_bonus",
    "effective_salvage_rune", "item_affixes", "affix_summary_lines",
    "rarity_dot", "rarity_label", "gear_sell_value",
    "MINI_BOSSES", "MINI_BOSS_MIN_FLOOR", "MINI_BOSS_MAX_FLOOR",
    "MINI_BOSS_SPAWN_CHANCE", "MINI_BOSS_LOOT", "BOSS_LOOT",
    "mini_boss_meta", "pick_mini_boss_for_floor", "should_spawn_mini_boss",
    "boss_loot_pool", "mini_boss_loot_pool", "roll_loot_table",
    "loot_fallback_junk",
    "ABILITIES", "CLASS_ABILITIES",
    "ability_meta", "class_abilities", "ability_swings",
    "RELIC_MIN_FLOOR", "RELIC_DROP_BASE_CHANCE",
    "RELIC_DROP_DEPTH_BONUS", "RELIC_DROP_MAX_CHANCE",
    "WILD_BATTLE_SPECIES", "WILD_BUDDY_SPECIES_BY_THEME",
    "wild_buddy_species_pool", "wild_battle_chance", "roll_wild_battle",
    "xp_for_level", "level_from_xp", "xp_to_next",
    "floor_meta", "mob_meta", "weapon_meta", "armor_meta",
    "consumable_meta", "class_meta",
    "relic_meta", "relic_drop_chance", "roll_relic", "relic_effect",
    "curse_meta", "curse_mult",
    "shrine_meta", "roll_shrine_outcome",
    "class_weapon_types", "class_armor_types",
    "weapon_allowed_for_class", "armor_allowed_for_class",
    "starter_weapon_for_class", "starter_armor_for_class",
    "is_ranged_weapon", "weapon_ammo_key",
    "stat_points_available", "class_reroll_cost_usd",
    "is_boss_floor",
    "pick_mob_for_floor", "pick_ore_for_floor",
    "mine_qty_roll", "capture_chance",
)
