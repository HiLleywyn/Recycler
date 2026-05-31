"""farming_config.py -- catalog of plots, crops, zones, fertilizer, weather,
recipes, pests, and ASCII frames for the new Farm minigame on the Harvest
Network. Mirrors the shape of fishing_config.py and dungeon_config.py.

HRV is the Harvest Network's tradeable / swappable coin. SEED is the
earn-only token that drops from harvests. Players plant seed packets
into plots, water and fertilize while the crop grows, and harvest the
ready plot for crops + SEED. Crops sell to the market for HRV; HRV
either swaps to REEL/RUNE/BUD via the carve-out pool, or burns to USD
via the cashout off-ramp.
"""
from __future__ import annotations

import datetime as _dt
import random as _random
from typing import Final


# ============================================================================
#  Tunables and clocks
# ============================================================================

ACTION_COOLDOWN_S:        Final[int]   = 4
PLANT_COOLDOWN_S:         Final[int]   = 8
WATER_COOLDOWN_S:         Final[int]   = 5
HARVEST_COOLDOWN_S:       Final[int]   = 4
WEATHER_TICK_S:           Final[int]   = 600
SESSION_TIMEOUT_S:        Final[int]   = 30

SEED_STAKE_HRV_PER_DAY:   Final[float] = 0.01
GEAR_BURN_LP_REWARD_BPS:  Final[int]   = 100
HRV_CASHOUT_LP_REWARD_BPS: Final[int]  = 100

COMBO_STEP:               Final[float] = 0.05
COMBO_MAX:                Final[float] = 1.5
COMBO_IDLE_RESET_S:       Final[int]   = 3600


# ============================================================================
#  Token / network
# ============================================================================

HRV_SYMBOL:               Final[str]   = "HRV"
SEED_SYMBOL:              Final[str]   = "SEED"
HRV_EMOJI:                Final[str]   = "\U0001F33E"   # wheat
SEED_EMOJI:               Final[str]   = "\U0001F331"   # seedling


# ============================================================================
#  Player level (mirrors user_fishing fish_level/fish_xp shape)
# ============================================================================
#
# Farming has its own level counter on user_farming.farm_level / farm_xp.
# XP is granted per harvest scaled by crop rarity, and feeds an arithmetic
# series identical in shape to fish_level so the two read consistently.
# Per-level +1% HRV payout multiplier applies at sell_crop time. Tuned so
# casual players cap around the same ~30-hour mark fishing does.

FARM_XP_BY_RARITY: Final[dict[str, int]] = {
    "common":     5,
    "uncommon":  12,
    "rare":      30,
    "epic":      75,
    "legendary": 200,
}

FARM_LEVEL_PAYOUT_PER_LEVEL: Final[float] = 0.01   # +1% HRV per level
FARM_MAX_LEVEL:              Final[int]   = 50
FARM_XP_CURVE:               Final[int]   = 80     # see level_from_xp()
HARVEST_NETWORK_SHORT:    Final[str]   = "har"

DEFAULT_ZONE:             Final[str]   = "meadow"
DEFAULT_WEATHER:          Final[str]   = "clear"

PLOT_COUNT_BY_TIER: Final[dict[int, int]] = {
    1: 4, 2: 6, 3: 8, 4: 10, 5: 14,
    6: 18, 7: 24, 8: 32, 9: 40,
}


# ============================================================================
#  PLOTS -- tiered upgrades priced in HRV (tier 1 free)
# ============================================================================
PLOTS: Final[dict[int, dict]] = {
    1: {
        "key": "dirt_patch", "name": "Dirt Patch", "emoji": "\U0001F7EB",
        "slots": 4, "price_hrv": 0.0, "max_water": 2,
        "growth_speed_mult": 1.0, "yield_bonus": 0.0,
        "max_zone_tier": 1,
        "blurb": "A square of dirt. Free and yours.",
    },
    2: {
        "key": "garden_bed", "name": "Garden Bed", "emoji": "\U0001FAB4",
        "slots": 6, "price_hrv": 750.0, "max_water": 3,
        "growth_speed_mult": 0.92, "yield_bonus": 0.05,
        "max_zone_tier": 2,
        "blurb": "Edged with stones. Holds water.",
    },
    3: {
        "key": "tilled_field", "name": "Tilled Field", "emoji": "\U0001F33E",
        "slots": 8, "price_hrv": 7_500.0, "max_water": 3,
        "growth_speed_mult": 0.85, "yield_bonus": 0.10,
        "max_zone_tier": 3,
        "blurb": "Properly tilled rows. Crops respect rows.",
    },
    4: {
        "key": "irrigated_field", "name": "Irrigated Field", "emoji": "\U0001F4A7",
        "slots": 10, "price_hrv": 60_000.0, "max_water": 4,
        "growth_speed_mult": 0.78, "yield_bonus": 0.18,
        "max_zone_tier": 4,
        "blurb": "Pipes hiss. Crops drink.",
    },
    5: {
        "key": "terraced_field", "name": "Terraced Field", "emoji": "⛰️",
        "slots": 14, "price_hrv": 500_000.0, "max_water": 4,
        "growth_speed_mult": 0.72, "yield_bonus": 0.25,
        "max_zone_tier": 5,
        "blurb": "Hillside switchbacks. Postcard pretty.",
    },
    6: {
        "key": "hydroponic_array", "name": "Hydroponic Array", "emoji": "\U0001F9EA",
        "slots": 18, "price_hrv": 5_000_000.0, "max_water": 5,
        "growth_speed_mult": 0.66, "yield_bonus": 0.32,
        "max_zone_tier": 6,
        "blurb": "Roots in nutrient mist. No soil. No mercy.",
    },
    7: {
        "key": "aero_greenhouse", "name": "Aero Greenhouse", "emoji": "\U0001F310",
        "slots": 24, "price_hrv": 25_000_000.0, "max_water": 5,
        "growth_speed_mult": 0.62, "yield_bonus": 0.40,
        "max_zone_tier": 7,
        "blurb": "Climate-locked dome. Weather can't enter.",
    },
    8: {
        "key": "crystal_terrarium", "name": "Crystal Terrarium", "emoji": "\U0001F48E",
        "slots": 32, "price_hrv": 120_000_000.0, "max_water": 6,
        "growth_speed_mult": 0.58, "yield_bonus": 0.50,
        "max_zone_tier": 8,
        "blurb": "Geode glow. Light stays after dark.",
    },
    9: {
        "key": "world_root_vault", "name": "World Root Vault", "emoji": "\U0001F333",
        "slots": 40, "price_hrv": 600_000_000.0, "max_water": 7,
        "growth_speed_mult": 0.55, "yield_bonus": 0.60,
        "max_zone_tier": 9,
        "blurb": "A vault grown from a single root. Eternal.",
    },
}


# ============================================================================
#  CROPS -- 20 entries from common wheat to legendary world_tree
# ============================================================================
CROPS: Final[dict[str, dict]] = {
    "wheat": {
        "key": "wheat", "name": "Wheat", "emoji": "\U0001F33E",
        "rarity": "common",
        "growth_seconds": 60,
        "base_yield_min": 4, "base_yield_max": 12,
        "seed_payout_min": 5, "seed_payout_max": 15,
        "hrv_sell_price": 0.5,
        "zone_tier": 1, "season": "spring",
        "blurb": "Bread starts here.",
    },
    "carrot": {
        "key": "carrot", "name": "Carrot", "emoji": "\U0001F955",
        "rarity": "common",
        "growth_seconds": 90,
        "base_yield_min": 3, "base_yield_max": 10,
        "seed_payout_min": 6, "seed_payout_max": 18,
        "hrv_sell_price": 0.7,
        "zone_tier": 1, "season": "spring",
        "blurb": "Pull straight up.",
    },
    "potato": {
        "key": "potato", "name": "Potato", "emoji": "\U0001F954",
        "rarity": "common",
        "growth_seconds": 120,
        "base_yield_min": 4, "base_yield_max": 11,
        "seed_payout_min": 7, "seed_payout_max": 20,
        "hrv_sell_price": 0.9,
        "zone_tier": 1, "season": "autumn",
        "blurb": "Reliable. Fries.",
    },
    "tomato": {
        "key": "tomato", "name": "Tomato", "emoji": "\U0001F345",
        "rarity": "common",
        "growth_seconds": 150,
        "base_yield_min": 3, "base_yield_max": 9,
        "seed_payout_min": 8, "seed_payout_max": 22,
        "hrv_sell_price": 1.2,
        "zone_tier": 2, "season": "summer",
        "blurb": "Technically a fruit.",
    },
    "corn": {
        "key": "corn", "name": "Corn", "emoji": "\U0001F33D",
        "rarity": "common",
        "growth_seconds": 180,
        "base_yield_min": 3, "base_yield_max": 8,
        "seed_payout_min": 9, "seed_payout_max": 25,
        "hrv_sell_price": 1.5,
        "zone_tier": 2, "season": "summer",
        "blurb": "Ears up. Listen for tractors.",
    },
    "pumpkin": {
        "key": "pumpkin", "name": "Pumpkin", "emoji": "\U0001F383",
        "rarity": "uncommon",
        "growth_seconds": 240,
        "base_yield_min": 2, "base_yield_max": 6,
        "seed_payout_min": 25, "seed_payout_max": 60,
        "hrv_sell_price": 4.0,
        "zone_tier": 3, "season": "autumn",
        "blurb": "Heavy. Carve later.",
    },
    "sunflower": {
        "key": "sunflower", "name": "Sunflower", "emoji": "\U0001F33B",
        "rarity": "uncommon",
        "growth_seconds": 270,
        "base_yield_min": 2, "base_yield_max": 5,
        "seed_payout_min": 30, "seed_payout_max": 70,
        "hrv_sell_price": 5.0,
        "zone_tier": 3, "season": "summer",
        "blurb": "Always faces the action.",
    },
    "watermelon": {
        "key": "watermelon", "name": "Watermelon", "emoji": "\U0001F349",
        "rarity": "uncommon",
        "growth_seconds": 300,
        "base_yield_min": 1, "base_yield_max": 4,
        "seed_payout_min": 35, "seed_payout_max": 80,
        "hrv_sell_price": 7.0,
        "zone_tier": 4, "season": "summer",
        "blurb": "Worth the wait. Worth the slice.",
    },
    "eggplant": {
        "key": "eggplant", "name": "Eggplant", "emoji": "\U0001F346",
        "rarity": "uncommon",
        "growth_seconds": 330,
        "base_yield_min": 2, "base_yield_max": 5,
        "seed_payout_min": 40, "seed_payout_max": 90,
        "hrv_sell_price": 8.5,
        "zone_tier": 4, "season": "autumn",
        "blurb": "Glossy. Suspicious.",
    },
    "sugarcane": {
        "key": "sugarcane", "name": "Sugarcane", "emoji": "\U0001F38B",
        "rarity": "uncommon",
        "growth_seconds": 360,
        "base_yield_min": 2, "base_yield_max": 6,
        "seed_payout_min": 45, "seed_payout_max": 100,
        "hrv_sell_price": 10.0,
        "zone_tier": 5, "season": "summer",
        "blurb": "Cuts itself when stressed.",
    },
    "strawberry": {
        "key": "strawberry", "name": "Strawberry", "emoji": "\U0001F353",
        "rarity": "rare",
        "growth_seconds": 480,
        "base_yield_min": 2, "base_yield_max": 5,
        "seed_payout_min": 120, "seed_payout_max": 280,
        "hrv_sell_price": 25.0,
        "zone_tier": 5, "season": "spring",
        "blurb": "Worth a market run.",
    },
    "blueberry": {
        "key": "blueberry", "name": "Blueberry", "emoji": "\U0001FAD0",
        "rarity": "rare",
        "growth_seconds": 540,
        "base_yield_min": 2, "base_yield_max": 5,
        "seed_payout_min": 150, "seed_payout_max": 320,
        "hrv_sell_price": 32.0,
        "zone_tier": 6, "season": "summer",
        "blurb": "Pop one to test. Eat the bush.",
    },
    "grape": {
        "key": "grape", "name": "Grape", "emoji": "\U0001F347",
        "rarity": "rare",
        "growth_seconds": 600,
        "base_yield_min": 1, "base_yield_max": 4,
        "seed_payout_min": 180, "seed_payout_max": 380,
        "hrv_sell_price": 45.0,
        "zone_tier": 6, "season": "autumn",
        "blurb": "Future cider, present joy.",
    },
    "dragonfruit": {
        "key": "dragonfruit", "name": "Dragonfruit", "emoji": "\U0001F432",
        "rarity": "rare",
        "growth_seconds": 660,
        "base_yield_min": 1, "base_yield_max": 3,
        "seed_payout_min": 220, "seed_payout_max": 460,
        "hrv_sell_price": 60.0,
        "zone_tier": 7, "season": "summer",
        "blurb": "Spiked. Pink. Mostly attitude.",
    },
    "starfruit": {
        "key": "starfruit", "name": "Starfruit", "emoji": "\U00002B50",
        "rarity": "rare",
        "growth_seconds": 720,
        "base_yield_min": 1, "base_yield_max": 3,
        "seed_payout_min": 260, "seed_payout_max": 540,
        "hrv_sell_price": 80.0,
        "zone_tier": 7, "season": "spring",
        "blurb": "Slice it crosswise. Trust me.",
    },
    "moonflower": {
        "key": "moonflower", "name": "Moonflower", "emoji": "\U0001F315",
        "rarity": "epic",
        "growth_seconds": 1080,
        "base_yield_min": 1, "base_yield_max": 2,
        "seed_payout_min": 700, "seed_payout_max": 1400,
        "hrv_sell_price": 220.0,
        "zone_tier": 8, "season": "autumn",
        "blurb": "Blooms only after sundown.",
    },
    "ghost_chili": {
        "key": "ghost_chili", "name": "Ghost Chili", "emoji": "\U0001F47B",
        "rarity": "epic",
        "growth_seconds": 1200,
        "base_yield_min": 1, "base_yield_max": 2,
        "seed_payout_min": 850, "seed_payout_max": 1700,
        "hrv_sell_price": 320.0,
        "zone_tier": 8, "season": "summer",
        "blurb": "You will regret eating this.",
    },
    "golden_apple": {
        "key": "golden_apple", "name": "Golden Apple", "emoji": "\U0001F34F",
        "rarity": "epic",
        "growth_seconds": 1500,
        "base_yield_min": 1, "base_yield_max": 2,
        "seed_payout_min": 1100, "seed_payout_max": 2200,
        "hrv_sell_price": 500.0,
        "zone_tier": 9, "season": "any",
        "blurb": "Mythic. Heavy. Worth a quest.",
    },
    "ambrosia": {
        "key": "ambrosia", "name": "Ambrosia", "emoji": "\U0001F36F",
        "rarity": "legendary",
        "growth_seconds": 3600,
        "base_yield_min": 1, "base_yield_max": 2,
        "seed_payout_min": 5000, "seed_payout_max": 10000,
        "hrv_sell_price": 1500.0,
        "zone_tier": 10, "season": "any",
        "blurb": "Food of gods. They're picky.",
    },
    "world_tree": {
        "key": "world_tree", "name": "World Tree Sapling", "emoji": "\U0001F333",
        "rarity": "legendary",
        "growth_seconds": 7200,
        "base_yield_min": 1, "base_yield_max": 2,
        "seed_payout_min": 8000, "seed_payout_max": 15000,
        "hrv_sell_price": 2500.0,
        "zone_tier": 10, "season": "any",
        "blurb": "Plant once. Wait forever. Worth it.",
    },

    # ── New crops (seeds only -- no direct plant purchase) ─────────────────

    "pepper": {
        "key": "pepper", "name": "Pepper", "emoji": "\U0001F336",
        "rarity": "common",
        "growth_seconds": 130,
        "base_yield_min": 3, "base_yield_max": 10,
        "seed_payout_min": 8, "seed_payout_max": 22,
        "hrv_sell_price": 1.0,
        "zone_tier": 2, "season": "summer",
        "blurb": "Hot enough to matter.",
    },
    "mushroom": {
        "key": "mushroom", "name": "Mushroom", "emoji": "\U0001F344",
        "rarity": "uncommon",
        "growth_seconds": 260,
        "base_yield_min": 2, "base_yield_max": 6,
        "seed_payout_min": 28, "seed_payout_max": 65,
        "hrv_sell_price": 4.5,
        "zone_tier": 3, "season": "autumn",
        "blurb": "Shade-grown. Alchemy staple.",
    },
    "lavender": {
        "key": "lavender", "name": "Lavender", "emoji": "\U0001F3F7",
        "rarity": "uncommon",
        "growth_seconds": 290,
        "base_yield_min": 2, "base_yield_max": 5,
        "seed_payout_min": 32, "seed_payout_max": 75,
        "hrv_sell_price": 5.5,
        "zone_tier": 4, "season": "spring",
        "blurb": "Calming. Used in brews and soaps.",
    },
    "rose": {
        "key": "rose", "name": "Rose", "emoji": "\U0001F339",
        "rarity": "rare",
        "growth_seconds": 520,
        "base_yield_min": 2, "base_yield_max": 4,
        "seed_payout_min": 130, "seed_payout_max": 290,
        "hrv_sell_price": 28.0,
        "zone_tier": 5, "season": "spring",
        "blurb": "Thorns included, beauty non-negotiable.",
    },
    "crystalmint": {
        "key": "crystalmint", "name": "Crystal Mint", "emoji": "\U0001F4A0",
        "rarity": "epic",
        "growth_seconds": 1150,
        "base_yield_min": 1, "base_yield_max": 2,
        "seed_payout_min": 750, "seed_payout_max": 1500,
        "hrv_sell_price": 250.0,
        "zone_tier": 8, "season": "any",
        "blurb": "Grows in arcane soil. Tastes like cold lightning.",
    },
    "dreamroot": {
        "key": "dreamroot", "name": "Dreamroot", "emoji": "\U0001F31B",
        "rarity": "legendary",
        "growth_seconds": 5400,
        "base_yield_min": 1, "base_yield_max": 2,
        "seed_payout_min": 6000, "seed_payout_max": 12000,
        "hrv_sell_price": 2000.0,
        "zone_tier": 9, "season": "any",
        "blurb": "Tastes of forgotten things. Used in the rarest brews.",
    },
}


# ============================================================================
#  Crop rarity weights + metadata
# ============================================================================

CROP_RARITY_WEIGHTS: Final[dict[str, int]] = {
    "common":    7000,
    "uncommon":  2000,
    "rare":       800,
    "epic":       180,
    "legendary":   20,
}

CROP_RARITY_META: Final[dict[str, dict]] = {
    "common":    {"label": "Common",    "color_hex": 0x95A5A6, "splash": False},
    "uncommon":  {"label": "Uncommon",  "color_hex": 0x2ECC71, "splash": False},
    "rare":      {"label": "Rare",      "color_hex": 0x3498DB, "splash": True},
    "epic":      {"label": "Epic",      "color_hex": 0x9B59B6, "splash": True},
    "legendary": {"label": "Legendary", "color_hex": 0xF1C40F, "splash": True},
}

_CROP_XP_BY_RARITY: Final[dict[str, int]] = {
    "common": 1, "uncommon": 3, "rare": 8, "epic": 20, "legendary": 50,
}


# ============================================================================
#  ZONES -- 10 zones from meadow (t1) to world_root (t10)
# ============================================================================
ZONES: Final[dict[str, dict]] = {
    "meadow": {
        "key": "meadow", "name": "Meadow", "emoji": "\U0001F33F",
        "zone_tier": 1, "plot_tier_required": 1, "season": "spring",
        "blurb": "Open grass. Bees included.",
        "default_weather_pool": ("clear", "sunny", "rain"),
    },
    "garden": {
        "key": "garden", "name": "Backyard Garden", "emoji": "\U0001FAB4",
        "zone_tier": 2, "plot_tier_required": 2, "season": "any",
        "blurb": "Fenced. The cat watches.",
        "default_weather_pool": ("clear", "sunny", "rain", "fog"),
    },
    "orchard": {
        "key": "orchard", "name": "Orchard", "emoji": "\U0001F34E",
        "zone_tier": 3, "plot_tier_required": 3, "season": "autumn",
        "blurb": "Rows of trees. Worms inspect.",
        "default_weather_pool": ("clear", "sunny", "fog", "rain"),
    },
    "greenhouse": {
        "key": "greenhouse", "name": "Greenhouse", "emoji": "\U0001F3D8",
        "zone_tier": 4, "plot_tier_required": 4, "season": "any",
        "blurb": "Glass walls. Steam rolls.",
        "default_weather_pool": ("clear", "sunny", "heatwave"),
    },
    "vineyard": {
        "key": "vineyard", "name": "Vineyard", "emoji": "\U0001F347",
        "zone_tier": 5, "plot_tier_required": 5, "season": "autumn",
        "blurb": "Rolling hills. Crows judge.",
        "default_weather_pool": ("clear", "sunny", "fog", "drought"),
    },
    "paddyfield": {
        "key": "paddyfield", "name": "Paddy Field", "emoji": "\U0001F35A",
        "zone_tier": 6, "plot_tier_required": 6, "season": "summer",
        "blurb": "Knee-deep water. Frogs argue.",
        "default_weather_pool": ("rain", "sunny", "heatwave", "fog"),
    },
    "bog": {
        "key": "bog", "name": "Bog", "emoji": "\U0001F33F",
        "zone_tier": 7, "plot_tier_required": 7, "season": "autumn",
        "blurb": "Mist clings. Things move.",
        "default_weather_pool": ("fog", "rain", "locusts", "blood_moon"),
    },
    "crystal_grove": {
        "key": "crystal_grove", "name": "Crystal Grove", "emoji": "\U0001F48E",
        "zone_tier": 8, "plot_tier_required": 8, "season": "any",
        "blurb": "Geode trees. Light fractures.",
        "default_weather_pool": ("clear", "harvest_moon", "golden_hour"),
    },
    "astral_field": {
        "key": "astral_field", "name": "Astral Field", "emoji": "\U0001F30C",
        "zone_tier": 9, "plot_tier_required": 9, "season": "any",
        "blurb": "Outside time. Stars are crops.",
        "default_weather_pool": ("harvest_moon", "golden_hour", "blood_moon", "clear"),
    },
    "world_root": {
        "key": "world_root", "name": "World Root", "emoji": "\U0001F333",
        "zone_tier": 10, "plot_tier_required": 9, "season": "any",
        "blurb": "Beneath the world tree. Eternal.",
        "default_weather_pool": ("golden_hour", "harvest_moon", "blood_moon"),
    },
}


# ============================================================================
#  SEASONS
# ============================================================================

SEASONS: Final[tuple[str, ...]] = ("spring", "summer", "autumn", "winter")


def current_season() -> str:
    """Return current season based on real-world month (NH approx).

    Mar-May = spring, Jun-Aug = summer, Sep-Nov = autumn, Dec-Feb = winter.
    """
    m = _dt.datetime.utcnow().month
    if 3 <= m <= 5:
        return "spring"
    if 6 <= m <= 8:
        return "summer"
    if 9 <= m <= 11:
        return "autumn"
    return "winter"


# ============================================================================
#  FERTILIZERS -- 9 consumable yield/growth boosts priced in HRV
# ============================================================================
FERTILIZERS: Final[dict[str, dict]] = {
    "compost": {
        "key": "compost", "name": "Compost", "emoji": "\U0001F33F",
        "price_hrv": 50.0,
        "yield_mult": 1.10, "growth_mult": 0.95,
        "max_stack": 100,
        "blurb": "Coffee grounds, eggshells, sass.",
    },
    "manure": {
        "key": "manure", "name": "Aged Manure", "emoji": "\U0001F4A9",
        "price_hrv": 250.0,
        "yield_mult": 1.20, "growth_mult": 0.92,
        "max_stack": 100,
        "blurb": "Smells like profit.",
    },
    "bonemeal": {
        "key": "bonemeal", "name": "Bonemeal", "emoji": "\U0001F9B4",
        "price_hrv": 800.0,
        "yield_mult": 1.30, "growth_mult": 0.85,
        "max_stack": 75,
        "blurb": "From the previous farmer.",
    },
    "guano": {
        "key": "guano", "name": "Guano", "emoji": "\U0001F423",
        "price_hrv": 2_500.0,
        "yield_mult": 1.45, "growth_mult": 0.78,
        "max_stack": 75,
        "blurb": "Imported. Bat-certified.",
    },
    "dragon_dung": {
        "key": "dragon_dung", "name": "Dragon Dung", "emoji": "\U0001F432",
        "price_hrv": 10_000.0,
        "yield_mult": 1.65, "growth_mult": 0.70,
        "max_stack": 50,
        "blurb": "Hot to the touch. Maybe wear gloves.",
    },
    "miracle_growth": {
        "key": "miracle_growth", "name": "Miracle Growth", "emoji": "\U00002728",
        "price_hrv": 50_000.0,
        "yield_mult": 1.85, "growth_mult": 0.60,
        "max_stack": 50,
        "blurb": "Lab-grown. Nobody asks how.",
    },
    "golden_compost": {
        "key": "golden_compost", "name": "Golden Compost", "emoji": "\U0001F31F",
        "price_hrv": 250_000.0,
        "yield_mult": 2.05, "growth_mult": 0.55,
        "max_stack": 25,
        "blurb": "Marigold-spangled. Suspiciously fragrant.",
    },
    "ambrosia_extract": {
        "key": "ambrosia_extract", "name": "Ambrosia Extract", "emoji": "\U0001F377",
        "price_hrv": 1_000_000.0,
        "yield_mult": 2.25, "growth_mult": 0.48,
        "max_stack": 25,
        "blurb": "One drop per plot. Crops giggle.",
    },
    "world_root_silt": {
        "key": "world_root_silt", "name": "World Root Silt", "emoji": "\U0001F333",
        "price_hrv": 5_000_000.0,
        "yield_mult": 2.50, "growth_mult": 0.40,
        "max_stack": 25,
        "blurb": "Scraped from the World Root itself.",
    },
}


# ============================================================================
#  WEATHER -- 11 weather events that bias growth + yield
# ============================================================================
WEATHER: Final[dict[str, dict]] = {
    "clear": {
        "key": "clear", "name": "Clear", "emoji": "\U00002601",
        "growth_mult": 1.00, "yield_mult": 1.00, "rare_bonus": 0.0,
        "duration_minutes": 30, "weight": 30,
        "blurb": "Nothing happens. Crops still grow.",
        "can_spawn_pest": False,
    },
    "sunny": {
        "key": "sunny", "name": "Sunny", "emoji": "\U00002600",
        "growth_mult": 0.90, "yield_mult": 1.10, "rare_bonus": 0.0,
        "duration_minutes": 45, "weight": 20,
        "blurb": "Vitamin D for everyone.",
        "can_spawn_pest": False,
    },
    "rain": {
        "key": "rain", "name": "Rain", "emoji": "\U0001F327",
        "growth_mult": 0.80, "yield_mult": 1.15, "rare_bonus": 0.0,
        "duration_minutes": 60, "weight": 18,
        "blurb": "Soft drumming on leaves.",
        "can_spawn_pest": False,
    },
    "drought": {
        "key": "drought", "name": "Drought", "emoji": "\U0001F3DC",
        "growth_mult": 1.40, "yield_mult": 0.70, "rare_bonus": 0.0,
        "duration_minutes": 45, "weight": 5,
        "blurb": "Cracked earth. Crops squint.",
        "can_spawn_pest": False,
    },
    "frost": {
        "key": "frost", "name": "Frost", "emoji": "\U00002744",
        "growth_mult": 1.30, "yield_mult": 0.60, "rare_bonus": 0.0,
        "duration_minutes": 30, "weight": 4,
        "blurb": "White rim on every leaf.",
        "can_spawn_pest": False,
    },
    "heatwave": {
        "key": "heatwave", "name": "Heatwave", "emoji": "\U0001F525",
        "growth_mult": 1.25, "yield_mult": 0.85, "rare_bonus": 0.0,
        "duration_minutes": 30, "weight": 5,
        "blurb": "Boots stick to dirt.",
        "can_spawn_pest": False,
    },
    "fog": {
        "key": "fog", "name": "Fog", "emoji": "\U0001F32B",
        "growth_mult": 1.10, "yield_mult": 1.05, "rare_bonus": 0.05,
        "duration_minutes": 45, "weight": 5,
        "blurb": "Mystery in every row.",
        "can_spawn_pest": False,
    },
    "locusts": {
        "key": "locusts", "name": "Locusts", "emoji": "\U0001F997",
        "growth_mult": 1.10, "yield_mult": 0.50, "rare_bonus": 0.0,
        "duration_minutes": 20, "weight": 4,
        "blurb": "Wings everywhere. Hide the corn.",
        "can_spawn_pest": True,
    },
    "harvest_moon": {
        "key": "harvest_moon", "name": "Harvest Moon", "emoji": "\U0001F315",
        "growth_mult": 0.85, "yield_mult": 1.30, "rare_bonus": 0.10,
        "duration_minutes": 30, "weight": 3,
        "blurb": "Big orange moon. Lucky night.",
        "can_spawn_pest": False,
    },
    "golden_hour": {
        "key": "golden_hour", "name": "Golden Hour", "emoji": "\U0001F31E",
        "growth_mult": 0.70, "yield_mult": 1.50, "rare_bonus": 0.20,
        "duration_minutes": 15, "weight": 1,
        "blurb": "The light is perfect. Photo it.",
        "can_spawn_pest": False,
    },
    "blood_moon": {
        "key": "blood_moon", "name": "Blood Moon", "emoji": "\U0001F534",
        "growth_mult": 1.00, "yield_mult": 1.10, "rare_bonus": 0.30,
        "duration_minutes": 20, "weight": 1,
        "blurb": "Crops bloom red. Things wake.",
        "can_spawn_pest": True,
    },
}

WEATHER_WEIGHTS: Final[dict[str, int]] = {
    "clear":        30,
    "sunny":        20,
    "rain":         18,
    "drought":       5,
    "frost":         4,
    "heatwave":      5,
    "fog":           5,
    "locusts":       4,
    "harvest_moon":  3,
    "golden_hour":   1,
    "blood_moon":    1,
}


# ============================================================================
#  RECIPES -- combine raw crops into processed goods (better HRV value)
# ============================================================================
RECIPES: Final[dict[str, dict]] = {
    "bread": {
        "key": "bread", "name": "Bread", "emoji": "\U0001F35E",
        "requires": {"wheat": 3},
        "output_qty": 1,
        "seed_yield_bonus_min": 10, "seed_yield_bonus_max": 30,
        "hrv_sell_price": 2.5,
        "blurb": "Crust crackles. Inside soft.",
    },
    "salad": {
        "key": "salad", "name": "Garden Salad", "emoji": "\U0001F957",
        "requires": {"tomato": 2, "carrot": 1},
        "output_qty": 1,
        "seed_yield_bonus_min": 15, "seed_yield_bonus_max": 40,
        "hrv_sell_price": 4.0,
        "blurb": "Crisp. Vinaigrette optional.",
    },
    "soup": {
        "key": "soup", "name": "Hearty Soup", "emoji": "\U0001F372",
        "requires": {"potato": 2, "carrot": 2},
        "output_qty": 1,
        "seed_yield_bonus_min": 18, "seed_yield_bonus_max": 45,
        "hrv_sell_price": 5.0,
        "blurb": "Bowl warms the hands.",
    },
    "stew": {
        "key": "stew", "name": "Farmer Stew", "emoji": "\U0001F963",
        "requires": {"potato": 3, "tomato": 2, "corn": 1},
        "output_qty": 1,
        "seed_yield_bonus_min": 35, "seed_yield_bonus_max": 90,
        "hrv_sell_price": 12.0,
        "blurb": "Sticks to your bones.",
    },
    "pie": {
        "key": "pie", "name": "Pumpkin Pie", "emoji": "\U0001F967",
        "requires": {"pumpkin": 2, "wheat": 2},
        "output_qty": 1,
        "seed_yield_bonus_min": 50, "seed_yield_bonus_max": 130,
        "hrv_sell_price": 18.0,
        "blurb": "Spice. Crust. Tradition.",
    },
    "jam": {
        "key": "jam", "name": "Berry Jam", "emoji": "\U0001F36F",
        "requires": {"strawberry": 3, "blueberry": 2},
        "output_qty": 1,
        "seed_yield_bonus_min": 200, "seed_yield_bonus_max": 500,
        "hrv_sell_price": 60.0,
        "blurb": "Spread on bread. Or eat with spoon.",
    },
    "cider": {
        "key": "cider", "name": "Vineyard Cider", "emoji": "\U0001F377",
        "requires": {"grape": 5},
        "output_qty": 1,
        "seed_yield_bonus_min": 280, "seed_yield_bonus_max": 650,
        "hrv_sell_price": 80.0,
        "blurb": "Fizzes. Slightly criminal.",
    },
    "ambrosia_brew": {
        "key": "ambrosia_brew", "name": "Ambrosia Brew", "emoji": "\U0001F378",
        "requires": {"ambrosia": 1, "golden_apple": 2, "moonflower": 1},
        "output_qty": 1,
        "seed_yield_bonus_min": 6000, "seed_yield_bonus_max": 14000,
        "hrv_sell_price": 5_000.0,
        "blurb": "One sip. Peace for an hour.",
    },
    "world_loaf": {
        "key": "world_loaf", "name": "World Loaf", "emoji": "\U0001F35E",
        "requires": {"world_tree": 1, "wheat": 50},
        "output_qty": 1,
        "seed_yield_bonus_min": 30000, "seed_yield_bonus_max": 70000,
        "hrv_sell_price": 25_000.0,
        "blurb": "Eternal sourdough. Never goes stale.",
    },
}


# ============================================================================
#  PESTS -- spawn during locusts / blood_moon weather; battle for capture
# ============================================================================
PESTS: Final[dict[str, dict]] = {
    "aphid": {
        "key": "aphid", "name": "Aphid Swarm", "emoji": "\U0001F41B",
        "hp": 12, "atk": 3, "capture_chance": 0.40,
        "drop_seed_min": 5, "drop_seed_max": 20,
        "min_zone_tier": 1,
        "blurb": "Tiny. Many. Bad for leaves.",
        "boss": False,
    },
    "locust": {
        "key": "locust", "name": "Locust", "emoji": "\U0001F997",
        "hp": 25, "atk": 6, "capture_chance": 0.30,
        "drop_seed_min": 12, "drop_seed_max": 45,
        "min_zone_tier": 2,
        "blurb": "Hops with bad intent.",
        "boss": False,
    },
    "crow": {
        "key": "crow", "name": "Crop Crow", "emoji": "\U0001F426",
        "hp": 35, "atk": 8, "capture_chance": 0.25,
        "drop_seed_min": 25, "drop_seed_max": 80,
        "min_zone_tier": 3,
        "blurb": "Caws at the scarecrow. The scarecrow blinks.",
        "boss": False,
    },
    "gopher": {
        "key": "gopher", "name": "Gopher", "emoji": "\U0001F43F",
        "hp": 50, "atk": 10, "capture_chance": 0.20,
        "drop_seed_min": 50, "drop_seed_max": 150,
        "min_zone_tier": 4,
        "blurb": "Pops up. Steals. Vanishes.",
        "boss": False,
    },
    "hornworm": {
        "key": "hornworm", "name": "Hornworm", "emoji": "\U0001F41B",
        "hp": 70, "atk": 14, "capture_chance": 0.15,
        "drop_seed_min": 100, "drop_seed_max": 300,
        "min_zone_tier": 5,
        "blurb": "The size of a thumb. Eats whole plants.",
        "boss": False,
    },
    "root_blight": {
        "key": "root_blight", "name": "Root Blight", "emoji": "\U0001F47B",
        "hp": 95, "atk": 18, "capture_chance": 0.10,
        "drop_seed_min": 200, "drop_seed_max": 600,
        "min_zone_tier": 7,
        "blurb": "Underground rot. Plants wilt before you see it.",
        "boss": False,
    },
    "the_blight": {
        "key": "the_blight", "name": "The Blight", "emoji": "\U0001F479",
        "hp": 150, "atk": 25, "capture_chance": 0.06,
        "drop_seed_min": 800, "drop_seed_max": 2000,
        "min_zone_tier": 8,
        "blurb": "Boss pest. Blood moon spawn. Eats whole zones.",
        "boss": True,
    },
}


# ============================================================================
#  FRAMES -- ASCII art shown in embeds (plain ASCII only, no em-dashes)
# ============================================================================
FRAMES: Final[dict[str, str]] = {
    "meadow_idle": "\n".join([
        "  . , ' . * , ' . ,  ",
        " ,  '  .  ,  ~b~  .  ",
        "  *  , ' .  ,  ' . * ",
        " ,  '  .  ,  '  .  , ",
        "~~~~~~~~~~~~~~~~~~~~~",
        "  grass | flowers    ",
    ]),
    "tilled": "\n".join([
        "   Y                 ",
        "   |   (pitchfork)   ",
        "===|=================",
        "=========================",
        "=== === === === === ==",
        "=========================",
        "   fresh tilled rows  ",
    ]),
    "sprout": "\n".join([
        "                     ",
        "  , ' , ' , ' , ' ,  ",
        " /  | | /  | | /  |  ",
        "=========================",
        "=== === === === === ==",
        "=========================",
        "   first shoots up    ",
    ]),
    "growing": "\n".join([
        "                     ",
        "  v v v  v v v  v v  ",
        " wWw wWw wWw wWw wWw ",
        "  | | |  | | |  | |  ",
        "=========================",
        "=== === === === === ==",
        "   half-grown crop    ",
    ]),
    "mature": "\n".join([
        "  *Y* *Y* *Y* *Y* *  ",
        "  |Y| |Y| |Y| |Y| |  ",
        "  Y Y  Y Y  Y Y  Y Y ",
        "  | |  | |  | |  | | ",
        "  | |  | |  | |  | | ",
        "=========================",
        "   ripe and ready     ",
    ]),
    "harvest": "\n".join([
        "    >>--C  * .  *    ",
        "   >>--C    *  .  *  ",
        "  * Y Y  * Y Y  * Y  ",
        "   >>--C  cut! cut!  ",
        "  * .  * .  * .  * . ",
        "=========================",
        "   sickle swings!     ",
    ]),
    "rain": "\n".join([
        " . | . | . | . | . | ",
        "| . | . | . | . | .  ",
        " . | . | . | . | . | ",
        "  , ' , ' , ' , ' ,  ",
        " /  | | /  | | /  |  ",
        "=========================",
        "   soft rain falls    ",
    ]),
    "drought": "\n".join([
        "        (*)          ",
        "       -----         ",
        "  beams:  \\  |  /   ",
        "           \\ | /    ",
        "_/\\_/\\_/\\_/\\_/\\_/\\ ",
        " _/\\_/\\_/\\_/\\_/\\_ ",
        "   cracked earth      ",
    ]),
    "locusts": "\n".join([
        " >w< ,oo,  >w<  ,oo, ",
        ",oo,  >w< ,oo,  >w<  ",
        " >w<  ,oo, >w< ,oo,  ",
        "  ,oo, >w<  ,oo, >w< ",
        " >w<  ,oo,  >w< ,oo, ",
        ",oo,   >w<  ,oo, >w< ",
        "   swarm descends     ",
    ]),
    "harvest_moon": "\n".join([
        "  .   *     .   *  . ",
        "    (       )        ",
        "   ( O O O O )       ",
        "    (       )        ",
        "  *   .   *   .   *  ",
        "  Y Y  Y Y  Y Y  Y Y ",
        "   big orange moon    ",
    ]),
    "pest_attack": "\n".join([
        "                     ",
        "   ,Y,               ",
        "   /|\\ <~~  >w<     ",
        "   | |  <~~ >w<      ",
        "~~~~~~~~~~~~~~~~~>   ",
        "=========================",
        "   pest on the move   ",
    ]),
    "boss_blight": "\n".join([
        "  \\\\  [O]  //       ",
        "   \\\\__|__//        ",
        "  /  ^v^v^  \\       ",
        " |   ( X )   |      ",
        "  \\  /   \\  /      ",
        "   \\/  .  \\/       ",
        "   THE  BLIGHT       ",
    ]),
    "victory": "\n".join([
        "  *  +  * + *  +  *  ",
        " +  * [###] *  + *  +",
        "  * + (#Y#) + *  + * ",
        " +  * (( )) *  + *  +",
        "  *  + ((Y)) + *  *  ",
        " + *  + * + *  +  *  ",
        "   harvest complete   ",
    ]),
    "wilt": "\n".join([
        "                     ",
        "  ..  ..  ..  ..  .. ",
        " ~v~  ~v~ ~v~  ~v~  ",
        "  ,_, ,_,  ,_, ,_,  ",
        "  | |  | |  | |  | | ",
        "=========================",
        "   crops are wilting  ",
    ]),
    "forage_start": "\n".join([
        "    o    *off the path*             ",
        "   /|     ___                       ",
        "   /\\    /   \\   .  ,  .           ",
        "         | ?? |  , wWw ,            ",
        "         \\___/   .  Y  ,            ",
        "  ~~~~~~~~~~~~~~~~~~~~~~~~          ",
        "   wandering the brambles...        ",
    ]),
    "forage_seed_pile": "\n".join([
        "    o   .  *  .   *                 ",
        "   /|     ___                       ",
        "   /\\    | * |  spilling SEED       ",
        "         | * |   (a tidy pile!)     ",
        "         |***|                      ",
        "   ~  ~  ~  ~  ~  ~  ~  ~  ~        ",
        "   you find a seed cache!           ",
    ]),
    "forage_hrv_purse": "\n".join([
        "    o    .  ,_,  .                  ",
        "   /|     /     \\                   ",
        "   /\\    | $$$  |   *clinks*        ",
        "          \\_____/                   ",
        "   ~  ~  ~  ~  ~  ~  ~  ~  ~        ",
        "   a leather purse  -  HRV inside!  ",
    ]),
    "forage_packets": "\n".join([
        "    o      .---.  .---.  .---.      ",
        "   /|     |seed|  |seed|  |seed|     ",
        "   /\\     |____|  |____|  |____|     ",
        "                                     ",
        "   ~  ~  ~  ~  ~  ~  ~  ~  ~         ",
        "   a stash of seed packets!          ",
    ]),
    "forage_fertilizer": "\n".join([
        "    o    .---.    *thunk*           ",
        "   /|   |  ::  |   (a sack!)        ",
        "   /\\   |  ::  |                    ",
        "        |__::__|  fertilizer find   ",
        "   ~  ~  ~  ~  ~  ~  ~  ~  ~        ",
        "   you tear a hole and dig in       ",
    ]),
    "forage_jackpot": "\n".join([
        "    o   *  .   *  .   *  .          ",
        "   /|       (( ))                   ",
        "   /\\      (( Y ))    *glows*       ",
        "          (( <O> ))                 ",
        "           (( ))                    ",
        "   ~  +  ~  +  ~  +  ~  +  ~        ",
        "   ANCIENT TUBER!  pulsing softly   ",
    ]),
    "forage_empty": "\n".join([
        "    o      ,    ,                   ",
        "   /|       brambles, brambles      ",
        "   /\\      ,   *  ,   ,             ",
        "         (sigh)                     ",
        "   ~~~~~~~~~~~~~~~~~~~~~~~~         ",
        "   nothing  -  just stickers        ",
    ]),
    "mutation_burst": "\n".join([
        "       *   .   *   .   *            ",
        "   .  *      ___      *  .          ",
        "      *    .'   '.    *             ",
        "   .       |  *  |       .          ",
        "      *    '. _ .'    *             ",
        "   .   *    /Y\\    *   .            ",
        "       a mutation blooms!           ",
    ]),
}


# ============================================================================
#  Helper functions (lookups + rolls)
# ============================================================================

def farm_xp(crop_key: str) -> int:
    """XP awarded for harvesting one ``crop_key`` (rarity-scaled).

    Mirrors fishing.fish_xp -- legendaries pay 200 XP, commons pay 5,
    so a Wheat plot is a quick steady earn while a World Tree Sapling
    feels like a genuine milestone.
    """
    spec = CROPS.get(crop_key)
    if not spec:
        return 0
    return int(FARM_XP_BY_RARITY.get(spec.get("rarity", "common"), 0))


def level_from_xp(xp: float) -> int:
    """Inverse arithmetic series -- same shape as fishing.level_from_xp.

    Tuned by ``FARM_XP_CURVE`` so casual play caps at level 50 around
    the same ~30-hour mark fishing does.
    """
    import math
    if xp <= 0:
        return 1
    raw = (1 + math.sqrt(1 + 8 * xp / FARM_XP_CURVE)) / 2
    lvl = int(math.floor(raw))
    return max(1, min(FARM_MAX_LEVEL, lvl))


def xp_to_next(xp: float) -> tuple[int, int]:
    """Return ``(xp_into_level, xp_for_next_level)`` for a progress bar.

    At max level the second value is 0 so the caller can render a flat
    bar instead of an absurd "100/0" fragment.
    """
    lvl = level_from_xp(xp)
    if lvl >= FARM_MAX_LEVEL:
        return (int(xp), 0)
    floor_xp = (lvl - 1) * lvl // 2 * FARM_XP_CURVE
    next_xp  = lvl * (lvl + 1) // 2 * FARM_XP_CURVE
    return (max(0, int(xp - floor_xp)), max(1, int(next_xp - floor_xp)))


def level_payout_mult(level: int) -> float:
    """Per-level HRV-payout boost. Lv. 1 = 1.0x, Lv. 50 = ~1.49x."""
    return 1.0 + max(0, level - 1) * FARM_LEVEL_PAYOUT_PER_LEVEL


# ============================================================================
# Wild buddy battles + harvest egg drops
# ============================================================================
# Mirrors fishing_config.WILD_BATTLE_* + dungeon_config.WILD_BATTLE_*.
# The species pool draws from buddies_config.SPECIES so captures land
# in the standard cc_buddies shelter the rest of the bot uses. Plant /
# harvest aesthetic: nature spirits, bug pests, goblin types.

WILD_BATTLE_SPECIES: Final[tuple[str, ...]] = (
    "wecco", "shrek", "donkey", "cobble", "fox",
)

# Per-zone wild-buddy spawn pools. A meadow harvest spooks meadow critters
# (donkeys, foxes, thornlings); a bog harvest pulls bog things (shrek,
# gloomer); the astral field draws sky / void creatures. Falls back to
# ``WILD_BATTLE_SPECIES`` when a zone isn't listed so a future zone
# designer can ship the geography first and tune the pool later.
WILD_BUDDY_SPECIES_BY_ZONE: Final[dict[str, tuple[str, ...]]] = {
    "meadow":         ("donkey", "fox", "thornling"),
    "garden":         ("thornling", "fox", "spiderlenny"),
    "orchard":        ("fox", "thornling", "spiderlenny"),
    "greenhouse":     ("thornling", "spiderlenny", "donkey"),
    "vineyard":       ("fox", "thornling", "donkey"),
    "paddyfield":     ("shrek", "thornling", "donkey"),
    "bog":            ("shrek", "gloomer", "thornling"),
    "crystal_grove":  ("thornling", "glitch", "draclet"),
    "astral_field":   ("nimbus", "glitch", "thornling"),
    "world_root":     ("thornling", "draclet", "gloomer"),
}


def wild_buddy_species_pool(zone: str) -> tuple[str, ...]:
    """Resolve the wild-buddy species pool for a farm zone.

    Falls back to ``WILD_BATTLE_SPECIES`` so any zone added without an
    explicit entry still spawns a coherent farm-themed creature instead
    of an empty pool.
    """
    pool = WILD_BUDDY_SPECIES_BY_ZONE.get(str(zone or "").lower())
    return pool or WILD_BATTLE_SPECIES

WILD_BATTLE_BASE_CHANCE:           Final[float] = 0.08
WILD_BATTLE_DEPTH_BONUS_PER_TIER:  Final[float] = 0.01   # +1%/zone tier
WILD_BATTLE_MAX_CHANCE:            Final[float] = 0.30

WILD_BATTLE_LEVEL_PER_ZONE_TIER:   Final[float] = 1.5
WILD_BATTLE_LEVEL_JITTER:          Final[int]   = 3
WILD_BATTLE_RARITY_PER_ZONE_TIER:  Final[float] = 0.20

WILD_BATTLE_WIN_HRV_MIN:           Final[float] = 5.0
WILD_BATTLE_WIN_HRV_MAX:           Final[float] = 35.0
WILD_BATTLE_WIN_HRV_PER_TIER:      Final[float] = 1.4
WILD_BATTLE_WIN_BBT_MIN:           Final[float] = 0.5
WILD_BATTLE_WIN_BBT_MAX:           Final[float] = 3.0
WILD_BATTLE_WIN_BBT_PER_TIER:      Final[float] = 1.3

WILD_BATTLE_CAPTURE_CHANCE:        Final[float] = 0.20
WILD_BATTLE_PROMPT_TIMEOUT_S:      Final[int]   = 60

# Egg drop on harvest (separate roll from wild battle). Lands in the
# user's held-egg slot via services.fishing.hatch_fishing_buddy so
# there's still only one egg system.
HARVEST_EGG_CHANCE:                Final[float] = 0.02   # 2% per harvest


def wild_battle_chance(zone_tier: int) -> float:
    """Per-harvest chance of a wild-buddy spawn at the given zone tier."""
    extra = max(0, int(zone_tier) - 1) * WILD_BATTLE_DEPTH_BONUS_PER_TIER
    return min(WILD_BATTLE_MAX_CHANCE, WILD_BATTLE_BASE_CHANCE + extra)


def roll_wild_battle(zone_tier: int, zone: str | None = None) -> dict:
    """Synthesise a wild-buddy opponent matching the cc_buddies row
    shape that services.buddy_battle.Fighter.from_row accepts.
    Mood pinned at 100 so wild buddies always fight at peak.
    ``zone`` (e.g. ``"bog"``) selects the species pool via
    :func:`wild_buddy_species_pool` so a player harvesting in the
    crystal grove gets crystal-grove buddies, not generic farm ones.
    """
    import random as _r
    pool = wild_buddy_species_pool(zone) if zone else WILD_BATTLE_SPECIES
    species = _r.choice(pool)
    base_level = max(1, int(round(int(zone_tier) * WILD_BATTLE_LEVEL_PER_ZONE_TIER)))
    level = max(
        1,
        base_level + _r.randint(-WILD_BATTLE_LEVEL_JITTER, WILD_BATTLE_LEVEL_JITTER),
    )
    base_tier = _r.choices((1, 2, 3, 4, 5), weights=(50, 25, 15, 7, 3), k=1)[0]
    bias = int(int(zone_tier) * WILD_BATTLE_RARITY_PER_ZONE_TIER)
    rarity_tier = max(1, min(5, base_tier + bias))
    return {
        "id": 0, "owner_user_id": 0,
        "species": species, "name": species.title(),
        "rarity_tier": rarity_tier, "level": level,
        "hunger": 100, "happiness": 100, "energy": 100,
        "hp_alloc": 0, "atk_alloc": 0, "spd_alloc": 0,
    }


def wild_battle_hrv_reward(zone_tier: int) -> float:
    """HRV prize for winning a wild-buddy battle."""
    import random as _r
    base = _r.uniform(WILD_BATTLE_WIN_HRV_MIN, WILD_BATTLE_WIN_HRV_MAX)
    multiplier = 1.0 + max(0, int(zone_tier) - 1) * (WILD_BATTLE_WIN_HRV_PER_TIER - 1.0)
    return round(base * multiplier, 2)


def wild_battle_bbt_reward(zone_tier: int) -> float:
    """BBT kicker for winning a wild-buddy battle. Universal token,
    same shape as fishing's wild-battle reel kicker.
    """
    import random as _r
    if WILD_BATTLE_WIN_BBT_MAX <= 0:
        return 0.0
    base = _r.uniform(WILD_BATTLE_WIN_BBT_MIN, WILD_BATTLE_WIN_BBT_MAX)
    multiplier = 1.0 + max(0, int(zone_tier) - 1) * (WILD_BATTLE_WIN_BBT_PER_TIER - 1.0)
    return round(base * multiplier, 2)


def crop_meta(key: str) -> dict | None:
    """Look up a crop by key OR display name.

    Accepts the canonical key (``"world_tree"``), the display name with any
    case/spacing (``"world tree sapling"``, ``"World_Tree_Sapling"``), or
    just the spaces-to-underscores form a user is likely to type after
    reading the field embed. Returns ``None`` only when nothing matches.
    """
    if not key:
        return None
    s = str(key).lower().strip()
    # Fast path: exact key match.
    if s in CROPS:
        return CROPS[s]
    # Try treating dashes/spaces as underscores so 'world tree sapling' or
    # 'world-tree-sapling' resolve the same way as 'world_tree_sapling'.
    norm = s.replace(" ", "_").replace("-", "_")
    if norm in CROPS:
        return CROPS[norm]
    # Fall back to matching against the display name, with the same
    # space/underscore normalization. This is what catches the
    # ',farm plant world_tree_sapling' case where the user typed the
    # display name as the key.
    for c in CROPS.values():
        cname = str(c.get("name") or "").lower()
        if not cname:
            continue
        cname_norm = cname.replace(" ", "_").replace("-", "_")
        if s == cname or norm == cname_norm:
            return c
    # Last resort: prefix match on the canonical key so a user typing
    # ',farm plant world' lands on world_tree.
    matches = [c for k, c in CROPS.items() if k.startswith(norm)]
    if len(matches) == 1:
        return matches[0]
    return None


def fertilizer_meta(key: str) -> dict | None:
    return FERTILIZERS.get(str(key or "").lower())


def zone_meta(key: str) -> dict | None:
    return ZONES.get(str(key or "").lower())


def weather_meta(key: str) -> dict | None:
    return WEATHER.get(str(key or "").lower())


def recipe_meta(key: str) -> dict | None:
    return RECIPES.get(str(key or "").lower())


def plot_meta(tier: int) -> dict | None:
    try:
        return PLOTS.get(int(tier))
    except (TypeError, ValueError):
        return None


def pest_meta(key: str) -> dict | None:
    return PESTS.get(str(key or "").lower())


def is_boss_pest(key: str) -> bool:
    p = pest_meta(key)
    return bool(p and p.get("boss"))


def crop_xp_by_rarity(rarity: str) -> int:
    return _CROP_XP_BY_RARITY.get(str(rarity or "").lower(), 1)


def pick_weather(rng: _random.Random | None = None) -> str:
    """Weighted pick from WEATHER_WEIGHTS."""
    rng = rng or _random
    keys = list(WEATHER_WEIGHTS.keys())
    weights = [WEATHER_WEIGHTS[k] for k in keys]
    return rng.choices(keys, weights=weights, k=1)[0]


def pick_pest_for_zone(
    zone_key: str, weather: str, rng: _random.Random | None = None,
) -> str | None:
    """Roll a pest key when ``weather`` allows it. None if peace."""
    w = weather_meta(weather)
    if not w or not w.get("can_spawn_pest"):
        return None
    rng = rng or _random
    z = zone_meta(zone_key) or {}
    z_tier = int(z.get("zone_tier") or 1)
    # Boss pest only on blood_moon
    if weather == "blood_moon" and rng.random() < 0.20:
        return "the_blight"
    # Otherwise weighted small-pest pool, scaled by zone tier
    candidates = [k for k, v in PESTS.items()
                  if not v.get("boss") and int(v.get("min_zone_tier", 1)) <= z_tier]
    if not candidates:
        return None
    return rng.choice(candidates)


def yield_roll(
    crop_key: str,
    rng: _random.Random | None = None,
    *,
    fertilizer_mult: float = 1.0,
    weather_mult: float = 1.0,
) -> int:
    """Roll a final crop count for one harvest event."""
    meta = crop_meta(crop_key)
    if not meta:
        return 0
    rng = rng or _random
    base = rng.randint(int(meta["base_yield_min"]), int(meta["base_yield_max"]))
    mult = float(fertilizer_mult) * float(weather_mult)
    return max(0, int(round(base * mult)))


def growth_seconds(
    crop_key: str,
    *,
    fertilizer_growth_mult: float = 1.0,
    weather_growth_mult: float = 1.0,
    plot_tier: int = 1,
) -> int:
    """Final growth time in seconds, with plot speed factored in."""
    meta = crop_meta(crop_key)
    if not meta:
        return 60
    plot = plot_meta(plot_tier) or {}
    plot_mult = float(plot.get("growth_speed_mult", 1.0))
    secs = float(meta["growth_seconds"]) * plot_mult \
        * float(fertilizer_growth_mult) * float(weather_growth_mult)
    return max(15, int(round(secs)))


# ============================================================================
#  Crop Mutations
# ============================================================================
#
# A small chance at plant time that a seed germinates into a special variant.
# Mutations are stored on the plot dict (`plot["mutation"] = key`) and kicker
# multipliers are applied at harvest_plot time. Bloomstone tiers nudge the
# base mutation chance up via `mutation_chance_bonus` -- the existing themed-
# stones pipeline; if the helper is missing we fall back to flat odds.

CROP_MUTATIONS: Final[dict[str, dict]] = {
    "golden": {
        "key": "golden", "name": "Golden",
        "emoji": "\U00002728",                                # sparkles
        "yield_mult": 1.5,
        "seed_mult":  2.5,
        "hrv_sell_mult": 2.0,
        "blurb": "Glints in every breeze. Pays double SEED at harvest.",
    },
    "giant": {
        "key": "giant", "name": "Giant",
        "emoji": "\U0001F995",                                # sauropod
        "yield_mult": 3.0,
        "seed_mult":  1.0,
        "hrv_sell_mult": 1.0,
        "blurb": "Grew way too big. Triple harvest count.",
    },
    "rainbow": {
        "key": "rainbow", "name": "Rainbow",
        "emoji": "\U0001F308",                                # rainbow
        "yield_mult": 2.0,
        "seed_mult":  4.0,
        "hrv_sell_mult": 2.5,
        "blurb": "Iridescent. Massive SEED + HRV payout.",
    },
}

# Base mutation roll, scaled down for higher-rarity crops so a legendary
# isn't trivially 8x'd. Picked at plant time so the player sees the prize
# all the way through growth.
MUTATION_BASE_CHANCE: Final[float] = 0.04
MUTATION_RARITY_MULT: Final[dict[str, float]] = {
    "common":    1.00,
    "uncommon":  0.85,
    "rare":      0.70,
    "epic":      0.55,
    "legendary": 0.40,
}
MUTATION_WEIGHTS: Final[list[tuple[str, float]]] = [
    ("golden",  0.55),
    ("giant",   0.30),
    ("rainbow", 0.15),
]


def mutation_meta(key: str | None) -> dict | None:
    if not key:
        return None
    return CROP_MUTATIONS.get(str(key).lower())


def roll_mutation(
    crop_key: str, rng: _random.Random | None = None, *, bonus: float = 0.0,
) -> str | None:
    """Roll a mutation key for a freshly planted crop. Returns None if no mutation.

    ``bonus`` is an additive multiplier on the base chance (e.g. a rainy
    weather lookup, a Bloomstone bonus). Capped so even maxed bonuses can't
    push past ~25% on a common crop.
    """
    rng = rng or _random
    meta = crop_meta(crop_key)
    if not meta:
        return None
    rarity = str(meta.get("rarity", "common"))
    chance = MUTATION_BASE_CHANCE * MUTATION_RARITY_MULT.get(rarity, 1.0)
    chance *= (1.0 + max(0.0, float(bonus)))
    chance = min(0.25, chance)
    if rng.random() >= chance:
        return None
    keys    = [k for k, _ in MUTATION_WEIGHTS]
    weights = [w for _, w in MUTATION_WEIGHTS]
    return rng.choices(keys, weights=weights, k=1)[0]


# ============================================================================
#  Seed Return on Harvest
# ============================================================================
#
# Small per-harvest chance the crop "goes to seed" -- the player gets
# back a few seed packets of what they just harvested, on top of the
# normal qty + SEED + HRV payout. Common crops drop more often but
# fewer; legendary crops drop rarely but with a higher floor since
# you can't easily restock world_tree saplings any other way.

SEED_RETURN_CHANCE_BY_RARITY: Final[dict[str, float]] = {
    "common":    0.30,
    "uncommon":  0.22,
    "rare":      0.16,
    "epic":      0.11,
    "legendary": 0.07,
}

# (min, max) packet count when the roll hits. Keeps high-rarity drops
# small so a lucky legendary harvest doesn't trivialise the seed shop.
SEED_RETURN_QTY_BY_RARITY: Final[dict[str, tuple[int, int]]] = {
    "common":    (1, 3),
    "uncommon":  (1, 3),
    "rare":      (1, 2),
    "epic":      (1, 2),
    "legendary": (1, 1),
}


def roll_seed_return(
    rarity: str, rng: _random.Random | None = None,
) -> int:
    """Maybe drop seed packets of the harvested crop. Returns 0 on miss."""
    rng = rng or _random
    chance = float(SEED_RETURN_CHANCE_BY_RARITY.get(rarity, 0.0))
    if chance <= 0.0 or rng.random() >= chance:
        return 0
    lo, hi = SEED_RETURN_QTY_BY_RARITY.get(rarity, (1, 1))
    return rng.randint(int(lo), int(hi))


# ============================================================================
#  Daily Contracts
# ============================================================================
#
# Each player gets one rotating contract per UTC day, deterministically rolled
# from (user_id, guild_id, date) so the same crop sticks all day even after
# crashes. Players hand in matching crops from inventory for a flat HRV +
# SEED payout that scales with crop rarity. Stored as JSONB on user_farming
# so a single column carries the active offer; total_contracts_completed
# tracks lifetime completions for badges + leaderboards.

# Tunable: HRV reward = base_per_unit * qty_required * rarity_multiplier
CONTRACT_HRV_PER_UNIT: Final[float] = 4.0
CONTRACT_SEED_PER_UNIT: Final[float] = 6.0

# Scale per crop rarity tier so a 12x legendary contract isn't only 4x HRV.
CONTRACT_RARITY_MULT: Final[dict[str, float]] = {
    "common":     1.00,
    "uncommon":   1.60,
    "rare":       2.80,
    "epic":       5.50,
    "legendary": 12.00,
}

# Required quantity: random within rarity-scoped range. Tuned so a common
# contract is doable in 1-2 harvests, a legendary takes 4-6.
CONTRACT_QTY_RANGES: Final[dict[str, tuple[int, int]]] = {
    "common":    (8, 18),
    "uncommon":  (6, 12),
    "rare":      (4,  9),
    "epic":      (3,  6),
    "legendary": (2,  5),
}

# How many days a contract stays open before the offer rolls over even if
# the player never opens the panel. Currently one day (UTC).
CONTRACT_VALID_DAYS: Final[int] = 1


# ============================================================================
#  Foraging minigame
# ============================================================================
#
# Lightweight wander-the-fields minigame, modelled after fish dig: cooldown
# gated, no consumable required. Outcomes are weighted across small/medium
# HRV/SEED purses, seed packet stashes, fertilizer packs, and a rare
# "Ancient Tuber" jackpot that drops a legendary crop straight into the
# inventory. Cooldown lives on the DB clock as last_forage_at.

FORAGE_COOLDOWN_S: Final[int] = 600   # 10 minutes between forages

# Outcome weights -- common chunks of HRV / SEED dominate, jackpot is rare.
FORAGE_OUTCOME_WEIGHTS: Final[dict[str, float]] = {
    "hrv_purse_small":     30.0,
    "hrv_purse_big":       12.0,
    "seed_pile_small":     22.0,
    "seed_pile_big":        8.0,
    "seed_packets":        14.0,
    "fertilizer_find":      8.0,
    "ancient_tuber":        2.0,
    "empty":                4.0,
}

FORAGE_PAYOUTS: Final[dict[str, tuple[float, float]]] = {
    "hrv_purse_small": (40.0,    300.0),
    "hrv_purse_big":   (500.0, 4_500.0),
    "seed_pile_small": (50.0,    300.0),
    "seed_pile_big":   (400.0, 3_000.0),
}

# Seed-packet drop pool: only zone-tier-1-through-5 crops so a fresh
# player benefits without instantly maxing their inventory on legendaries.
FORAGE_PACKET_QTY: Final[tuple[int, int]] = (2, 6)
FORAGE_PACKET_MAX_ZONE_TIER: Final[int] = 5

# Fertilizer drop pool: cheap-to-mid tiers; saves the meta cosmetics for
# the proper shop.
FORAGE_FERTILIZER_POOL: Final[tuple[str, ...]] = (
    "compost", "manure", "bonemeal", "guano",
)
FORAGE_FERTILIZER_QTY: Final[tuple[int, int]] = (1, 4)

# Jackpot crop. Adds straight to crop_inventory at high stack -- the
# player gets to ride the rare legendary sell price without growing it.
FORAGE_JACKPOT_CROP: Final[str] = "ambrosia"
FORAGE_JACKPOT_QTY: Final[tuple[int, int]] = (3, 8)


def roll_forage_outcome(rng: _random.Random | None = None) -> str:
    """Return a weighted forage outcome key."""
    rng = rng or _random
    keys = list(FORAGE_OUTCOME_WEIGHTS.keys())
    weights = [FORAGE_OUTCOME_WEIGHTS[k] for k in keys]
    return rng.choices(keys, weights=weights, k=1)[0]


def forage_packet_pool() -> tuple[str, ...]:
    """Crops eligible for the seed-packet stash drop. Filtered by zone tier."""
    return tuple(
        c["key"] for c in CROPS.values()
        if int(c.get("zone_tier") or 1) <= FORAGE_PACKET_MAX_ZONE_TIER
    )


def _contract_seed(date_iso: str, user_id: int, guild_id: int) -> int:
    """Deterministic per-(date, user, guild) seed for the daily roll."""
    return hash((date_iso, int(user_id), int(guild_id))) & 0xFFFFFFFF


def roll_daily_contract(
    user_id: int, guild_id: int, *, farm_level: int = 1, date_iso: str | None = None,
) -> dict:
    """Pick today's contract for ``user_id``. Same dict every call within a day.

    Picks a random crop the player can plausibly grow (zone_tier <= farm_level
    + 2 so low-level players aren't asked for legendaries) and a random qty
    inside the rarity-scoped range. Reward = base * qty * rarity multiplier
    so higher rarities still pay more even with smaller piles.
    """
    today = date_iso or _dt.datetime.now(tz=_dt.timezone.utc).date().isoformat()
    rng = _random.Random(_contract_seed(today, user_id, guild_id))
    farm_level = max(1, int(farm_level))
    # Player level gates which zone tiers can show up. We map roughly:
    #   level 1-5   -> zone tier 1-3
    #   level 6-15  -> zone tier 1-5
    #   level 16-30 -> zone tier 1-7
    #   level 31+   -> all tiers
    if farm_level <= 5:
        max_zone_tier = 3
    elif farm_level <= 15:
        max_zone_tier = 5
    elif farm_level <= 30:
        max_zone_tier = 7
    else:
        max_zone_tier = 10
    pool = [c for c in CROPS.values() if int(c.get("zone_tier") or 1) <= max_zone_tier]
    if not pool:
        pool = list(CROPS.values())
    crop = rng.choice(pool)
    rarity = str(crop.get("rarity") or "common")
    lo, hi = CONTRACT_QTY_RANGES.get(rarity, (5, 10))
    qty = rng.randint(lo, hi)
    rmult = CONTRACT_RARITY_MULT.get(rarity, 1.0)
    hrv_reward = round(CONTRACT_HRV_PER_UNIT * qty * rmult, 2)
    seed_reward = round(CONTRACT_SEED_PER_UNIT * qty * rmult, 2)
    expires = _dt.datetime.fromisoformat(today) + _dt.timedelta(days=CONTRACT_VALID_DAYS)
    return {
        "date": today,
        "crop_key": str(crop["key"]),
        "rarity": rarity,
        "qty_required": int(qty),
        "qty_delivered": 0,
        "hrv_reward_human": float(hrv_reward),
        "seed_reward_human": float(seed_reward),
        "expires_at": expires.isoformat(),
        "completed": False,
        "completed_at": None,
    }


# ============================================================================
#  EXPANSION -- tools, perks, harvest combos, seasonal enforcement, grid
# ============================================================================
#
# Layered on top of the original Farm config. Everything here is opt-in
# at the service-level so older saves with empty ``tools`` / ``perks``
# JSONB columns keep working without migrations beyond the column add.

# Hand tools -- 4 kinds, 3 tiers each. Purchased with HRV. Each kind
# has a single "best" tier in the player's inventory; planting / watering /
# harvesting reads ``tools[<kind>]`` for the active tier.
TOOLS: Final[dict[str, dict]] = {
    # Hoe -- knocks down the plant cooldown so a streak of plot-flips
    # doesn't dead-time on the player.
    "hoe_rough":     {"kind": "hoe", "tier": 1, "name": "Rough Hoe",
                       "emoji": "\U0001FAB5", "price_hrv": 1_500.0,
                       "plant_cooldown_mult": 0.85,
                       "blurb": "Wood handle. Bent head. Works."},
    "hoe_refined":   {"kind": "hoe", "tier": 2, "name": "Refined Hoe",
                       "emoji": "\U0001FAB5", "price_hrv": 80_000.0,
                       "plant_cooldown_mult": 0.70,
                       "blurb": "Hardwood, iron edge. Bites in clean."},
    "hoe_master":    {"kind": "hoe", "tier": 3, "name": "Masterwork Hoe",
                       "emoji": "\U0001FAB5", "price_hrv": 4_000_000.0,
                       "plant_cooldown_mult": 0.55,
                       "blurb": "Forged to your grip. Plants land themselves."},
    # Watering Can -- multi-plot water (rough=1 plot, refined=3, master=5).
    "can_rough":     {"kind": "can", "tier": 1, "name": "Tin Watering Can",
                       "emoji": "\U0001F4A7", "price_hrv": 1_500.0,
                       "water_radius": 1,
                       "blurb": "Holds water. Mostly."},
    "can_refined":   {"kind": "can", "tier": 2, "name": "Brass Watering Can",
                       "emoji": "\U0001F4A7", "price_hrv": 80_000.0,
                       "water_radius": 3,
                       "blurb": "Wide rose, even sprinkle."},
    "can_master":    {"kind": "can", "tier": 3, "name": "Cloudburst Can",
                       "emoji": "\U0001F4A7", "price_hrv": 4_000_000.0,
                       "water_radius": 5,
                       "blurb": "Glass globe holds a drizzle inside."},
    # Sickle -- bulk-harvest (rough=1, refined=4, master=all ready).
    "sickle_rough":  {"kind": "sickle", "tier": 1, "name": "Hand Sickle",
                       "emoji": "\U0001F33E", "price_hrv": 2_000.0,
                       "harvest_radius": 1,
                       "blurb": "Sharp curve. Steady swing."},
    "sickle_refined":{"kind": "sickle", "tier": 2, "name": "Refined Sickle",
                       "emoji": "\U0001F33E", "price_hrv": 120_000.0,
                       "harvest_radius": 4,
                       "blurb": "Balanced. Cuts four stems at a time."},
    "sickle_master": {"kind": "sickle", "tier": 3, "name": "Reaper's Sickle",
                       "emoji": "\U00002604", "price_hrv": 6_000_000.0,
                       "harvest_radius": 99,
                       "blurb": "One swing. Whole field. Quiet."},
    # Scarecrow -- placement object (counted on user_farming.scarecrow_count).
    # Reduces pest spawn chance per scarecrow up; cumulative caps at 3.
    "scarecrow_straw":  {"kind": "scarecrow", "tier": 1, "name": "Straw Scarecrow",
                          "emoji": "\U0001F478", "price_hrv": 800.0,
                          "pest_reduction": 0.10,
                          "blurb": "Crows blink first."},
    "scarecrow_iron":   {"kind": "scarecrow", "tier": 2, "name": "Iron Scarecrow",
                          "emoji": "\U0001F916", "price_hrv": 35_000.0,
                          "pest_reduction": 0.20,
                          "blurb": "Looks like it might move. Doesn't."},
    "scarecrow_runic":  {"kind": "scarecrow", "tier": 3, "name": "Runic Scarecrow",
                          "emoji": "\U00002728", "price_hrv": 1_200_000.0,
                          "pest_reduction": 0.35,
                          "blurb": "Pests forget the field exists."},
}

TOOL_KINDS: Final[tuple[str, ...]] = ("hoe", "can", "sickle", "scarecrow")
SCARECROW_CAP: Final[int] = 3


def tool_meta(key: str) -> dict | None:
    if not key:
        return None
    return TOOLS.get(str(key))


def tools_by_kind(kind: str) -> tuple[dict, ...]:
    """All tool metas for a given kind, sorted by tier ascending."""
    return tuple(sorted(
        (t for t in TOOLS.values() if t.get("kind") == kind),
        key=lambda t: int(t.get("tier") or 0),
    ))


def active_tool(tools_inv: dict, kind: str) -> dict | None:
    """Resolve the highest-tier owned tool for a given kind.

    ``tools_inv`` is the JSONB ``user_farming.tools`` dict, mapping
    tool_key -> count (1 or 0). Returns ``None`` when nothing is owned
    for the kind.
    """
    inv = tools_inv or {}
    best: dict | None = None
    best_tier = -1
    for tkey, count in inv.items():
        if not count:
            continue
        meta = TOOLS.get(str(tkey))
        if meta is None or meta.get("kind") != kind:
            continue
        tier = int(meta.get("tier") or 0)
        if tier > best_tier:
            best = meta
            best_tier = tier
    return best


# Farmer perks -- chosen at farm-level milestones. JSONB shape:
# ``user_farming.perks = {perk_key: True, ...}``. Resetting clears the dict
# at a small HRV burn (handled at the cog/service level).

PERKS: Final[dict[str, dict]] = {
    "green_thumb":       {"name": "Green Thumb", "unlock_level": 5,
                           "yield_bonus": 0.05, "blurb": "All crops +5% qty."},
    "dry_thumb":         {"name": "Dry Thumb",   "unlock_level": 10,
                           "water_cost_mult": 0.75, "blurb": "Water actions cost 25% less stamina."},
    "rainmaker":         {"name": "Rainmaker", "unlock_level": 15,
                           "weather_reroll": True, "blurb": "Reroll the field weather once a day."},
    "combo_master":      {"name": "Combo Master", "unlock_level": 20,
                           "combo_step_bonus": 0.05,
                           "blurb": "Each combo step rewards an extra +5%."},
    "pest_warden":       {"name": "Pest Warden", "unlock_level": 25,
                           "pest_chance_mult": 0.70,
                           "blurb": "Pests spawn 30% less often."},
    "seedling_savant":   {"name": "Seedling Savant", "unlock_level": 30,
                           "free_plant_chance": 0.05,
                           "blurb": "5% chance to plant without consuming a seed packet."},
    "gold_thumb":        {"name": "Gold Thumb", "unlock_level": 40,
                           "rare_bonus": 0.20,
                           "blurb": "Rare-or-better crops +20% extra SEED payout."},
    "mythic_thumb":      {"name": "Mythic Thumb", "unlock_level": 50,
                           "legendary_bonus_qty": 1,
                           "blurb": "Legendary harvests pay +1 extra unit."},
    "moonlit_grower":    {"name": "Moonlit Grower", "unlock_level": 35,
                           "moon_yield_mult": 1.25,
                           "blurb": "Harvest under Harvest/Blood Moons pays 25% extra."},
    "tournament_titan":  {"name": "Tournament Titan", "unlock_level": 45,
                           "contract_reward_mult": 1.25,
                           "blurb": "Daily harvest contracts pay 25% more HRV + SEED."},
}


def perk_meta(key: str) -> dict | None:
    return PERKS.get(str(key)) if key else None


def perk_active(perks_inv: dict, key: str) -> bool:
    return bool((perks_inv or {}).get(str(key)))


def perks_available(level: int) -> tuple[str, ...]:
    """Tuple of perk keys the player has unlocked at this farm level."""
    return tuple(
        k for k, m in PERKS.items()
        if int(level) >= int(m.get("unlock_level") or 999)
    )


# Harvest combo math. A combo "step" is incremented each time the player
# harvests a plot within COMBO_WINDOW_S of the previous harvest. Bonus
# caps at COMBO_MAX_STEP * COMBO_STEP_BONUS.

COMBO_WINDOW_S:           Final[int]   = 10
COMBO_MAX_STEP:           Final[int]   = 6
COMBO_STEP_BONUS:         Final[float] = 0.10
COMBO_LEGEND_THRESHOLD:   Final[int]   = 5   # legend trigger for achievements


def harvest_combo_mult(prev_step: int, perks_inv: dict | None = None) -> tuple[int, float]:
    """Compute the new combo step + payout multiplier.

    ``prev_step`` is the player's previous combo count (0 if expired or
    fresh). Returns ``(new_step, mult)`` where ``mult`` >= 1.0.

    The Combo Master perk widens the per-step bonus.
    """
    step = min(int(prev_step or 0) + 1, COMBO_MAX_STEP)
    bonus_per = COMBO_STEP_BONUS
    if perks_inv and perk_active(perks_inv, "combo_master"):
        bonus_per += float(PERKS["combo_master"].get("combo_step_bonus") or 0.0)
    return step, 1.0 + bonus_per * (step - 1)


def seasonal_yield_mult(crop_key: str, season: str | None = None) -> float:
    """Return the yield multiplier from in/out-of-season planting.

    In-season crops yield +15%; off-season crops yield -40%. A
    season field of ``"any"`` is always in-season.
    """
    meta = crop_meta(crop_key)
    if not meta:
        return 1.0
    crop_season = str(meta.get("season") or "any").lower()
    if crop_season == "any":
        return 1.0
    cur = (season or current_season()).lower()
    if cur == crop_season:
        return 1.15
    return 0.60


# ------- Expansion content: crops, pests, recipes ----------------------
# Appended to the live dicts so the rest of the cog picks them up
# automatically through the existing meta helpers.

CROPS["saffron"] = {
    "key": "saffron", "name": "Saffron", "emoji": "\U0001F33A",
    "rarity": "epic",
    "growth_seconds": 1320,
    "base_yield_min": 1, "base_yield_max": 2,
    "seed_payout_min": 900, "seed_payout_max": 1900,
    "hrv_sell_price": 410.0,
    "zone_tier": 9, "season": "summer",
    "blurb": "Three threads per flower. Worth its weight.",
}
CROPS["moon_grape"] = {
    "key": "moon_grape", "name": "Moon Grape", "emoji": "\U0001F347",
    "rarity": "rare",
    "growth_seconds": 720,
    "base_yield_min": 2, "base_yield_max": 4,
    "seed_payout_min": 220, "seed_payout_max": 480,
    "hrv_sell_price": 45.0,
    "zone_tier": 6, "season": "autumn",
    "blurb": "Glows under moonlight. Cellar door wines.",
}
CROPS["sunheart"] = {
    "key": "sunheart", "name": "Sunheart", "emoji": "\U0001F31E",
    "rarity": "legendary",
    "growth_seconds": 5400,
    "base_yield_min": 1, "base_yield_max": 2,
    "seed_payout_min": 7000, "seed_payout_max": 14000,
    "hrv_sell_price": 2200.0,
    "zone_tier": 10, "season": "summer",
    "blurb": "A fruit that keeps a piece of the sun.",
}
CROPS["mooncress"] = {
    "key": "mooncress", "name": "Mooncress", "emoji": "\U0001F33F",
    "rarity": "epic",
    "growth_seconds": 1200,
    "base_yield_min": 1, "base_yield_max": 3,
    "seed_payout_min": 820, "seed_payout_max": 1600,
    "hrv_sell_price": 280.0,
    "zone_tier": 8, "season": "winter",
    "blurb": "Silver leaves; grows where shadow lingers.",
}

PESTS["honey_thief"] = {
    "key": "honey_thief", "name": "Honey Thief", "emoji": "\U0001F41D",
    "hp": 42, "atk": 9, "capture_chance": 0.22,
    "drop_seed_min": 35, "drop_seed_max": 110,
    "min_zone_tier": 3,
    "blurb": "Stings first. Apologizes later.",
    "boss": False,
}
PESTS["locust_king"] = {
    "key": "locust_king", "name": "Locust King", "emoji": "\U0001F451",
    "hp": 220, "atk": 32, "capture_chance": 0.05,
    "drop_seed_min": 1200, "drop_seed_max": 3000,
    "min_zone_tier": 5,
    "blurb": "Locust storm boss. Wears the swarm like a cloak.",
    "boss": True,
}
PESTS["crop_wraith"] = {
    "key": "crop_wraith", "name": "Crop Wraith", "emoji": "\U0001F47B",
    "hp": 380, "atk": 48, "capture_chance": 0.03,
    "drop_seed_min": 4000, "drop_seed_max": 9000,
    "min_zone_tier": 8,
    "blurb": "Legendary blight boss. Wilts whatever it touches.",
    "boss": True,
}

RECIPES["spiced_cider"] = {
    "key": "spiced_cider", "name": "Spiced Cider", "emoji": "\U0001F377",
    "requires": {"grape": 4, "pepper": 1},
    "output_qty": 1,
    "seed_yield_bonus_min": 320, "seed_yield_bonus_max": 700,
    "hrv_sell_price": 95.0,
    "blurb": "Cider with a kick.",
}
RECIPES["sunheart_tonic"] = {
    "key": "sunheart_tonic", "name": "Sunheart Tonic", "emoji": "\U0001F375",
    "requires": {"sunheart": 1, "lavender": 2},
    "output_qty": 1,
    "seed_yield_bonus_min": 8000, "seed_yield_bonus_max": 18000,
    "hrv_sell_price": 6_500.0,
    "blurb": "One sip; one bright afternoon.",
}
RECIPES["mooncress_salve"] = {
    "key": "mooncress_salve", "name": "Mooncress Salve", "emoji": "\U0001FAA7",
    "requires": {"mooncress": 2, "honey_thief": 1},  # honey_thief drop (future)
    "output_qty": 1,
    "seed_yield_bonus_min": 1100, "seed_yield_bonus_max": 2400,
    "hrv_sell_price": 360.0,
    "blurb": "Bruise-healing salve. Smells like rain.",
}

# Optional weather additions -- additive to WEATHER + WEATHER_WEIGHTS.
WEATHER["hailstorm"] = {
    "key": "hailstorm", "name": "Hailstorm", "emoji": "\U0001F328",
    "growth_mult": 1.20, "yield_mult": 0.75, "rare_bonus": 0.05,
    "duration_minutes": 15, "weight": 3,
    "blurb": "Crops huddle. Some win.",
    "can_spawn_pest": False,
}
WEATHER["gold_rain"] = {
    "key": "gold_rain", "name": "Gold Rain", "emoji": "\U0001F4B0",
    "growth_mult": 0.80, "yield_mult": 1.50, "rare_bonus": 0.10,
    "duration_minutes": 10, "weight": 1,
    "blurb": "Rare. Brief. Every drop earns.",
    "can_spawn_pest": False,
}
WEATHER_WEIGHTS["hailstorm"] = 3
WEATHER_WEIGHTS["gold_rain"] = 1
