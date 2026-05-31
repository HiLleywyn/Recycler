"""
fishing_config.py  -  Catalogs and tuning constants for the Discoin fishing game.

All tuning lives here so cogs/fishing.py and services/fishing.py never
hard-code drop tables, rod tiers, bait, zones, or animation frames.

Sections (in order):
    Tuning constants
    Token economy (LURE / REEL)
    Animation frames
    Junk catalog
    Fish catalog
    Rod catalog
    Bait catalog
    Zone catalog
    Helper functions

Conventions:
    -- Fishing rewards pay out in LURE (the fishing-only token, see
       Config.EARN_ONLY_TOKENS in core/config.py). Shop prices are in REEL
       (the network coin, also earn-only). All amounts are floats here
       and get converted via core.framework.scale.to_raw at the call site.
    -- LURE -> REEL conversion is one-way (burn-swap or stake yield).
       REEL -> USD cashout is one-way (burn at oracle minus haircut).
       Neither token can be acquired with USD or via .buy / .swap from
       any other token. That is the entire pay-to-win firewall.
    -- Weights are arbitrary integers in a weighted random pool. Bigger
       integers => more common.
    -- All `weight_lbs` ranges are inclusive floats. Each catch rolls a
       size in [min_lbs, max_lbs]; the realised weight feeds the payout
       formula in services/fishing.py.
"""
from __future__ import annotations

import random
from typing import Final


# ============================================================================
# Tuning constants
# ============================================================================

# Cast cooldown. Halved by the global COOLDOWN_MULTIPLIER in
# core/framework/cooldowns.py so the user-facing wait is ~15 seconds.
CAST_COOLDOWN_S: Final[int] = 30

# Active session lifetime. If a player walks away mid-cast, the view
# auto-resolves as "the fish got away" so the row never sticks.
SESSION_TIMEOUT_S: Final[int] = 30

# Bite window: how long the user has to hit the HOOK button after a
# bite event triggers. Sub-sweet-spot reactions still land but earn a
# weaker quality multiplier.
HOOK_WINDOW_S: Final[float] = 3.0

# Sweet-spot window inside the hook window. Hooking inside this slice
# applies a quality bonus to the catch's weight and payout.
HOOK_SWEET_S: Final[float] = 1.0

# Quality multipliers applied to weight + payout based on reaction time.
HOOK_SWEET_BONUS: Final[float] = 1.5
HOOK_LATE_PENALTY: Final[float] = 0.85

# Combo system. Each successful catch in a row bumps the multiplier by
# COMBO_STEP, capped at COMBO_MAX. A miss / junk catch resets to 1.0x.
COMBO_STEP: Final[float] = 0.05
COMBO_MAX: Final[float] = 2.0

# Idle window after which the combo decays to 1.0x even without a miss.
# Uses DB-side clocks (EXTRACT(EPOCH FROM (NOW() - last_cast_at))).
COMBO_IDLE_RESET_S: Final[int] = 3600

# Junk-vs-fish-vs-bonus split BEFORE the rod / bait / zone modifiers
# apply. The numbers are weights, not percentages: 70 / 25 / 5 is the
# baseline. Rod tier shifts mass from junk -> fish; bait shifts mass
# from junk -> bonus.
BASE_OUTCOME_WEIGHTS: Final[dict[str, int]] = {
    "junk":  60,
    "fish":  35,
    "bonus":  5,   # money bag, mystery box, buddy egg
}

# Within the bonus bucket: relative weights for each bonus type.
BONUS_SUB_WEIGHTS: Final[dict[str, int]] = {
    "money_bag":   55,
    "mystery_box": 40,
    "buddy_egg":    5,
}

# Money-bag payout range in LURE (rolled per pull). Old USD payouts
# (25..1500) get a 10x bump on rename so the LURE economy starts at a
# similar wallet-feel as the previous USD economy with LURE oracle at
# $0.10 -- 250 LURE is roughly $25, 15000 LURE is roughly $1500.
MONEY_BAG_MIN_LURE: Final[float] = 250.0
MONEY_BAG_MAX_LURE: Final[float] = 15_000.0

# Mystery-box LURE payout range; opens to a LURE prize, slim chance of
# a free bait stack instead. The exact resolution lives in
# services/fishing.py so the catalog stays declarative.
MYSTERY_BOX_MIN_LURE: Final[float] = 1_000.0
MYSTERY_BOX_MAX_LURE: Final[float] = 50_000.0

# Buddy-egg cap: how many fishing-borne buddies a player can hatch from
# the eggs per real-world day (UTC). Prevents a whale spamming the rod
# from collapsing the buddy meta. Eggs above the cap fall back to a
# generous mystery-box payout (handled in services/fishing.py).
BUDDY_EGG_DAILY_CAP: Final[int] = 1

# Held-egg cap: how many UNHATCHED eggs a single player can keep in
# their on-person fishing inventory at once. The "with you" tier is
# fixed at 10 -- not upgradable -- so a player has to actively manage
# their pocket eggs rather than passively pile up the entire egg
# storage on the rod. Overflow lands in the buddy egg-storage
# container (services/buddy_storage_eggs) which IS upgradable in the
# buddy shop up to 1000 rows; only when BOTH are full does an egg
# roll fall back to a mystery-box LURE payout.
#
# The MAX_HELD_EGGS name is preserved so existing import sites keep
# working; it now mirrors buddies_config.EGG_HELD_HARD_CAP.
from configs.buddies_config import EGG_HELD_HARD_CAP as _EGG_HELD_HARD_CAP

MAX_HELD_EGGS: Final[int] = _EGG_HELD_HARD_CAP

# Per-rarity LURE sale price for held eggs (the player can ,fish egg
# sell back to the LURE wallet at any time). Mirrors the rarity ladder
# in buddies_config.RARITY_TIERS so a legendary egg pays out roughly
# what a player would expect a "lottery" item to be worth: meaningful
# but not life-changing. Values are in raw LURE (mints, no oracle move).
EGG_SELL_LURE_BY_TIER: Final[dict[int, float]] = {
    1:    25_000.0,    # common
    2:    60_000.0,    # uncommon
    3:   150_000.0,    # rare
    4:   400_000.0,    # epic
    5: 1_500_000.0,    # legendary
}

# ----------------------------------------------------------------------------
# Treasure maps
# ----------------------------------------------------------------------------
# The Soggy Treasure Map junk item (junk key = "map") is consumable via
# ,fish dig. The dig path rolls a weighted outcome from the loot table
# below: LURE chests (most common), REEL kicker, free rare bait, free
# trap, free held-egg, or the jackpot legendary-fish "ancient relic"
# that drops straight into fish_inventory at max-rolled weight.
#
# All payouts are MINTS (no oracle move) on the same wallet_holding
# code path stake yield uses, so the dig embed renders the standard
# _MINT_FOOTER. The map is consumed before the roll resolves so a
# crashed roll doesn't leak a free map.

# Cooldown between digs. DB-side clock; same enforcement model as the
# trap-collect cooldown.
TREASURE_DIG_COOLDOWN_S: Final[int] = 60

# Loot outcomes. Each entry is (key, weight) -- weights are arbitrary
# integers and do NOT need to sum to 100. Pick is one weighted-random
# call. Adjusted ranges live in TREASURE_PAYOUT below so a future
# rebalance only touches the numbers, not the catalog shape.
TREASURE_LOOT_WEIGHTS: Final[dict[str, int]] = {
    "lure_small":       30,   # 500-2,000 LURE
    "lure_medium":      25,   # 2,000-10,000 LURE
    "lure_large":       12,   # 10,000-50,000 LURE
    "reel_kicker":      15,   # 5-50 REEL
    "rare_bait":         8,   # 5-20 magic / chum bait
    "trap_cache":        4,   # 2-5 random trap (wire/oak)
    "wild_egg":          5,   # 1 held egg (random species + tier)
    "ancient_relic":     1,   # JACKPOT: legendary fish at max weight
}

# Per-outcome payout ranges. All amounts are HUMAN units (the service
# converts via to_raw at credit time). LURE chests roll uniformly in
# [min, max]; REEL kicker likewise. The bait / trap / fish payouts
# pick a uniform integer count in [min, max].
TREASURE_PAYOUT: Final[dict[str, tuple[float, float]]] = {
    "lure_small":     (   500.0,    2_000.0),
    "lure_medium":    ( 2_000.0,   10_000.0),
    "lure_large":     (10_000.0,   50_000.0),
    "reel_kicker":    (     5.0,       50.0),
    "rare_bait":      (     5.0,       20.0),    # qty range
    "trap_cache":     (     2.0,        5.0),    # qty range
    # wild_egg + ancient_relic don't need numeric ranges; they pick
    # species / fish-key from their own pools below.
}

# Bait keys eligible for the "rare_bait" treasure drop. Restricted to
# the high-tier bait so the player feels like they hit something
# scarce, not just a top-up of free worms.
TREASURE_RARE_BAIT_POOL: Final[tuple[str, ...]] = ("magic", "chum")

# Trap keys eligible for the "trap_cache" treasure drop. Limited to
# the lower-tier traps so the dig path doesn't trivialise the
# steel/abyssal pot grind.
TREASURE_TRAP_POOL: Final[tuple[str, ...]] = ("wire", "oak")

# Legendary fish keys eligible for the "ancient_relic" jackpot. Pulled
# uniformly from any FISH entry whose rarity is "legendary" and whose
# zones include at least one accessible tier (filter applied at roll
# time so the pool tracks catalog edits automatically).
TREASURE_JACKPOT_POOL_RARITY: Final[str] = "legendary"

# Buddy species that fishing eggs roll. Mirrors the water-themed entries
# already in buddies_config.SPECIES so we never invent a phantom species.
FISHING_BUDDY_SPECIES: Final[tuple[str, ...]] = (
    "shrimp", "crab", "octopus", "lobster", "wecco",
)

# Per-zone wild-buddy spawn pools. Hooked wild buddies should match the
# water type the player is fishing in -- Marlin types in deep ocean, swamp
# things in the Bayou, eldritch creeps in the Abyss. Falls back to
# ``FISHING_BUDDY_SPECIES`` for any zone not listed below so a future
# zone designer can ship the geography first and tune the pool later.
WILD_BUDDY_SPECIES_BY_ZONE: Final[dict[str, tuple[str, ...]]] = {
    # Tier 1 -- shallow water, shore creatures
    "pond":      ("shrimp", "crab"),
    "swamp":     ("shrek", "thornling", "crab"),
    "sewer":     ("crab", "robo", "glitch"),
    # Tier 2 -- mid water, mixed pelagic
    "lake":      ("crab", "octopus", "wecco"),
    "river":     ("shrimp", "crab", "wecco"),
    # Tier 3 -- open ocean / shore-deep
    "ocean":     ("octopus", "lobster", "wecco"),
    "dock":      ("crab", "lobster", "wecco"),
    "reef":      ("crab", "octopus", "lobster", "wecco"),
    # Tier 4 -- deep / kelp / glacier
    "kelp":      ("octopus", "lobster", "wecco"),
    "glacier":   ("wecco", "draclet", "lobster"),
    # Tier 5 -- temple / abyss
    "temple":    ("lobster", "octopus", "gloomer"),
    "abyss":     ("octopus", "wecco", "gloomer"),
    # Tier 6+ -- exotic biomes
    "trench":    ("octopus", "wecco", "gloomer"),
    "moonpool":  ("nimbus", "wecco", "gloomer"),
    "magma":     ("blazer", "lobster", "octopus"),
    "void":      ("gloomer", "octopus", "glitch"),
    "nebula":    ("nimbus", "glitch", "wecco"),
    "ouroboros": ("pyper", "wecco", "draclet"),
    # New zones
    "tidal_pool":          ("shrimp", "crab"),
    "mangrove":            ("shrimp", "crab", "wecco"),
    "shipwreck":           ("crab", "octopus", "lobster"),
    "bioluminescent_bay":  ("octopus", "wecco", "nimbus"),
    "crystal_caverns":     ("octopus", "wecco", "gloomer"),
    "storm_surge":         ("wecco", "octopus", "lobster"),
}


def wild_buddy_species_pool(zone: str) -> tuple[str, ...]:
    """Resolve the wild-buddy species pool for a fishing zone.

    Falls back to the catch-all ``FISHING_BUDDY_SPECIES`` so any zone
    added without an explicit entry still spawns a coherent water-themed
    creature instead of an empty pool.
    """
    pool = WILD_BUDDY_SPECIES_BY_ZONE.get(str(zone or "").lower())
    return pool or FISHING_BUDDY_SPECIES

# XP awarded by services/fishing.py per catch. Pure flavor: the value
# only feeds the local user_fishing.xp counter (own little level
# system); the season pass + achievements consume bus events instead.
FISH_XP_BY_RARITY: Final[dict[str, int]] = {
    "common":     5,
    "uncommon":  12,
    "rare":      30,
    "epic":      75,
    "legendary": 200,
}

# Per-fishing-level multiplier applied to payouts. Stops the curve from
# being too punishing while keeping Lv. 50 noticeably better than Lv. 1.
FISH_LEVEL_PAYOUT_PER_LEVEL: Final[float] = 0.01   # +1% per level
FISH_MAX_LEVEL: Final[int] = 50
FISH_XP_CURVE: Final[int] = 80  # see level_from_xp() below


# ============================================================================
# Token economy (LURE / REEL)
# ============================================================================
# Fishing pays in LURE (the fishing-only token). LURE has two one-way
# exits to REEL (the network coin):
#   * Burn-swap (instant): burn LURE, mint REEL at the live oracle ratio
#     (USD value preserved), with the same price-impact / slippage /
#     supply-burn machinery the rest of the codebase uses for .buy /
#     .sell. Big burns move the LURE oracle DOWN (sell pressure +
#     supply contraction) and the REEL oracle UP (mint pressure), and
#     the chart picks both of those up via crypto_prices.update_price.
#   * Stake yield (passive): LURE_STAKE_REEL_PER_DAY REEL accrued per
#     LURE staked per day. Compounds linearly until claimed.
# REEL has one one-way exit to USD:
#   * Burn cashout: identical mechanics to .sell -- decrements REEL
#     supply, applies the standard price-impact formula on the REEL
#     oracle, and credits users.wallet at the post-impact REEL price.
#     Same code path as cogs/trade.py .sell except it bypasses the
#     EARN_ONLY trade-routing block.
#
# Conversion rates are NOT hard-coded -- they are derived from the live
# oracle each call. PRICE_IMPACT_DIVISOR (core/config.py) governs slippage
# uniformly across .buy, .sell, and these fishing burns.

# Token symbols (used by services/fishing.py for update_wallet_holding
# and trade-cog gating). Exposed as module constants so a future rename
# only touches this file.
LURE_SYMBOL: Final[str]  = "LURE"
REEL_SYMBOL: Final[str]  = "REEL"
LURE_NETWORK_SHORT: Final[str] = "lur"

# Daily REEL yield per LURE staked. 0.01 means 1000 LURE staked for
# 1 day produces 10 REEL on claim, and 30 days produces 300 REEL.
# Linear (not compounded) so the math stays obvious in the panel.
LURE_STAKE_REEL_PER_DAY: Final[float] = 0.01

# Fraction (basis points) of every LURE/REEL burn or gear-spend USD
# value that is paid out as a USD reward to LP holders of pools
# containing the burned symbol. 100 bps = 1%. Distributed pro-rata to
# user lp_positions whose pool contains the burned token, mirroring
# the per-pool weighting that services/lp_yield.py uses for hourly
# yield. Set to 0 to disable.
GEAR_BURN_LP_REWARD_BPS: Final[int] = 100


# ============================================================================
# Wild-buddy battles
# ============================================================================
# Casting in deeper water occasionally hooks something with teeth. When
# a wild battle rolls, the cast resolves to ``outcome="wild_battle"`` and
# the cog renders a Challenge prompt instead of the normal catch frame.
# The opponent is a synthesised buddy_row built from FISHING_BUDDY_SPECIES,
# scaled by zone tier (level / rarity) and rod tier (rarity bias upward).
#
# Win:  reward LURE + fishing XP + small chance the wild buddy is
#       captured (uses the existing hatch_fishing_buddy path).
# Lose: no penalty, no consolation -- just bragging rights returned to sender.
# Decline / timeout: the wild buddy escapes; same as a missed hook.

# Per-cast base chance of hooking a wild buddy. Pond / shallow water
# starts at 5%; each zone tier above the rod's minimum tier adds
# WILD_BATTLE_ZONE_BONUS_PER_TIER. Capped at WILD_BATTLE_MAX_CHANCE so
# even the abyss never makes peaceful fishing impossible.
# Boosted ~5x from the original 1% / 1% / 8% so encounters feel like a
# real part of fishing instead of a once-an-hour curiosity.
WILD_BATTLE_BASE_CHANCE: Final[float]            = 0.05
WILD_BATTLE_ZONE_BONUS_PER_TIER: Final[float]    = 0.05
WILD_BATTLE_MAX_CHANCE: Final[float]             = 0.40

# Level + rarity scaling of the synthesised opponent. Level scales with
# zone tier so the abyss spawns big fights; rarity is influenced by rod
# tier so a Golden Rod hooks rarer wild buddies on average.
WILD_BATTLE_LEVEL_PER_ZONE_TIER: Final[int]      = 4    # zone tier 1 -> ~lv 4, tier 5 -> ~lv 20
WILD_BATTLE_LEVEL_JITTER: Final[int]             = 3    # +/- jitter on the base level
WILD_BATTLE_RARITY_PER_ROD_TIER: Final[float]    = 0.4  # higher rod = bias toward higher rarity tier

# Reward range for winning. Wins pay BOTH currencies:
#   * LURE  -- bulk of the reward, funds more casts at the live oracle.
#   * REEL  -- small kicker that mints REEL straight to the wallet so a
#              dedicated battler can skip the swap path entirely. The
#              REEL is minted (no oracle move) on the same code path as
#              stake yield -- see resolve_wild_battle in services/fishing.py.
# Cog rolls a uniform value in [min, max] for each, scales each by zone
# tier independently, and credits both via update_wallet_holding so the
# circulating-supply / chart accounting stays uniform with the rest of
# the cog.
WILD_BATTLE_WIN_LURE_MIN: Final[float]           = 500.0
WILD_BATTLE_WIN_LURE_MAX: Final[float]           = 5_000.0
WILD_BATTLE_WIN_LURE_PER_ZONE_TIER: Final[float] = 1.5  # multiplier per zone tier above 1

# REEL kicker. Small relative to the LURE haul -- ~$2-25 USD-equivalent
# at typical REEL oracle prices versus the $50-500 the LURE side pays --
# so the chart-economy still incentivises ,fish swap for big-ticket REEL
# without making wild battles a non-event for REEL accumulation.
WILD_BATTLE_WIN_REEL_MIN: Final[float]           = 2.0
WILD_BATTLE_WIN_REEL_MAX: Final[float]           = 25.0
WILD_BATTLE_WIN_REEL_PER_ZONE_TIER: Final[float] = 1.4

# Active-buddy XP reward on every wild-battle win. Mirrors the LURE/REEL
# scaling shape so a deep-zone fight pays the active buddy real progression
# instead of just the player. Multiplier per opponent rarity tier so a
# Legendary opponent gives a small XP bump.
WILD_BATTLE_WIN_XP_BASE: Final[int]              = 25
WILD_BATTLE_WIN_XP_PER_ZONE_TIER: Final[float]   = 1.5  # multiplier per zone tier above 1
WILD_BATTLE_WIN_XP_RARITY_MULT: Final[float]     = 0.10 # +10% per tier above 1


def wild_battle_xp_reward(zone_tier: int, rarity_tier: int = 1) -> int:
    """Active-buddy XP earned on a wild-battle win.

    Scales with zone tier (multiplicative, mirrors the LURE/REEL curve)
    and tacks on a small per-rarity bump so a Legendary wild buddy
    rewards more than a Common at the same zone. Conservative ceiling
    so chat / craft / expedition XP stay relevant.
    """
    base = WILD_BATTLE_WIN_XP_BASE
    zone_mult = 1.0 + max(0, int(zone_tier) - 1) * (
        WILD_BATTLE_WIN_XP_PER_ZONE_TIER - 1.0
    )
    rarity_mult = 1.0 + max(0, int(rarity_tier) - 1) * WILD_BATTLE_WIN_XP_RARITY_MULT
    return max(1, int(round(base * zone_mult * rarity_mult)))


# Chance on a win to "capture" the wild buddy -- routes through the
# existing buddy_egg hatch path (respects BUDDY_EGG_DAILY_CAP).
WILD_BATTLE_CAPTURE_CHANCE: Final[float]         = 0.20

# How long the Challenge prompt stays open before the wild buddy escapes.
WILD_BATTLE_PROMPT_TIMEOUT_S: Final[int]         = 60


# ============================================================================
# Sentinel section markers for chunked Edit() inserts
# ============================================================================
# Everything below here gets replaced piece-by-piece in subsequent edits.
# Don't put real content between these markers in the initial scaffold.

# === FRAMES_START ===
# ============================================================================
# Animation frames
# ============================================================================
# Each frame is a single ASCII block, rendered inside a Discord code block in
# the embed description. The cog edits the same message between frames so the
# whole sequence reads as one animation.
#
# Frame ids referenced from cogs/fishing.py:
#     "cast"    -- preparing the throw
#     "fly_1"   -- bait arcing through the air
#     "fly_2"   -- bait splash-landing
#     "wait_1"  -- floater bobbing, no bites
#     "wait_2"  -- floater bobbing further out
#     "nibble"  -- something is interested
#     "bite"    -- !! HOOK NOW window opens
#     "miss"    -- the catch got away
#     "reel_1"  -- pulling it back
#     "reel_2"  -- almost there
#     "trash"   -- pulled up junk
#     "fish"    -- pulled up a fish
#     "bonus"   -- pulled up a bonus drop
#     "egg"     -- pulled up a buddy egg


# ============================================================================
# Beachcomb (free 10-min wander)
# ============================================================================
# Mirror of farming_config.FORAGE_*. The player walks the shore between
# casts; outcomes credit small LURE / REEL purses, hand out a few baits,
# occasionally drop a Soggy Treasure Map directly into junk_inventory,
# and on a rare jackpot roll add a max-weight legendary fish straight to
# fish_inventory the same way the dig jackpot does. Free roll, no inputs.
# Cooldown lives on the DB clock as user_fishing.last_beachcomb_at.

BEACHCOMB_COOLDOWN_S: Final[int] = 600   # 10 minutes between beachcombs

# Outcome weights -- common LURE/REEL purses dominate, jackpot is rare.
BEACHCOMB_OUTCOME_WEIGHTS: Final[dict[str, float]] = {
    "lure_purse_small":   30.0,
    "lure_purse_big":     12.0,
    "reel_kicker_small":  20.0,
    "reel_kicker_big":     6.0,
    "bait_stash":         16.0,
    "treasure_map":        7.0,
    "ancient_relic":       2.0,
    "empty":               7.0,
}

BEACHCOMB_PAYOUTS: Final[dict[str, tuple[float, float]]] = {
    "lure_purse_small":  (   200.0,   1_500.0),
    "lure_purse_big":    ( 2_500.0,  18_000.0),
    "reel_kicker_small": (     2.0,      18.0),
    "reel_kicker_big":   (    25.0,     120.0),
}

# Bait stash drop pool -- only the cheap-to-mid baits so a fresh player
# isn't instantly maxing on legendaries from a free wander. Quantities
# are uniform integers in [lo, hi] per picked bait key.
BEACHCOMB_BAIT_POOL: Final[tuple[str, ...]] = (
    "worm", "shrimp", "minnow", "neon",
)
BEACHCOMB_BAIT_QTY: Final[tuple[int, int]] = (3, 8)
BEACHCOMB_BAIT_PICKS: Final[int] = 2  # distinct bait keys per drop

# Treasure-map drop count range. Maps live in junk_inventory["map"] and
# are spent by ,fish dig.
BEACHCOMB_MAP_QTY: Final[tuple[int, int]] = (1, 2)


def roll_beachcomb_outcome(rng: "_random.Random | None" = None) -> str:
    """Return a weighted beachcomb outcome key."""
    rng = rng or _random
    keys = list(BEACHCOMB_OUTCOME_WEIGHTS.keys())
    weights = [BEACHCOMB_OUTCOME_WEIGHTS[k] for k in keys]
    return rng.choices(keys, weights=weights, k=1)[0]


FRAMES: Final[dict[str, str]] = {
    "cast": "\n".join([
        "        🎣              ",
        "       /                ",
        "      /                 ",
        "  o__/                  ",
        "  /\\                    ",
        "  /  \\                  ",
        "~~~~~~~~~~~~~~~~~~~~~~~ ",
        "  ~     ~     ~    ~    ",
    ]),
    "fly_1": "\n".join([
        "  o     ___                       ",
        " /|    /   \\__         .          ",
        " /\\   /        \\__      `.        ",
        "🎣              \\__      `>       ",
        "                   \\__    o       ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~     ~     ~    ~     ~        ",
    ]),
    "fly_2": "\n".join([
        "  o                               ",
        " /|                               ",
        " /\\                               ",
        "🎣                                ",
        "                          *splash*",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~o~~~~~  ",
        "  ~     ~     ~    ~     | ~      ",
        "                         ' .  .   ",
    ]),
    "wait_1": "\n".join([
        "  o                               ",
        " /|                               ",
        " /\\                               ",
        "🎣                                ",
        "                                  ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~o~~~~~~  ",
        "  ~     ~     ~    ~    | ~       ",
        "  ~ ~     ~      ~    ~ ~  ~      ",
    ]),
    "wait_2": "\n".join([
        "  o                               ",
        " /|                               ",
        " /\\                               ",
        "🎣        ~                       ",
        "                                  ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~o~~~~~~  ",
        "  ~     ~     ~    ~    |~        ",
        "    ~    ~  ~    ~ ~  ~     ~     ",
    ]),
    "wait_3": "\n".join([
        "  o                               ",
        " /|              ~                ",
        " /\\                               ",
        "🎣                                ",
        "                                  ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~o~~~~~~  ",
        "  ~     ~     ~    ~    /|        ",
        "  ~ . ~     ~    ~     ~   ~      ",
    ]),
    "peek": "\n".join([
        "  o                               ",
        " /|                               ",
        " /\\          shadow!              ",
        "🎣                                ",
        "             °o°                  ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~o~~~~~~  ",
        "  ~     ~  >°)))     ~   |~       ",
        "    ~    ~     ~    ~     ~  ~    ",
    ]),
    "false_alarm": "\n".join([
        "  o    ?                          ",
        " /|     huh.                      ",
        " /\\                               ",
        "🎣                                ",
        "                                  ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~o~~~~~~  ",
        "  ~     ~     ~    ~    /| ~      ",
        "    ~  just driftwood.   ~   ~    ",
    ]),
    "tug": "\n".join([
        "  o    !!                         ",
        " /|     |                         ",
        " /\\     |  *TUG TUG*              ",
        "🎣 ====/                          ",
        "       \\                          ",
        "~~~~~~~~~\\~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~    ~ |~  ~     ~     ~  ~     ",
        "       >°))) something BIG! ~     ",
    ]),
    "nibble": "\n".join([
        "  o                               ",
        " /|             *                 ",
        " /\\           * .                 ",
        "🎣           .                    ",
        "                                  ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~o~~~~~~  ",
        "  ~     ~     ~    ~    /| ~      ",
        "    ~    ~  ~    ~ ~  ~  >° )))   ",
    ]),
    "bite": "\n".join([
        "  o          !!                   ",
        " /|         !  !                  ",
        " /\\          !!                   ",
        "🎣           |                    ",
        "             |                    ",
        "~~~~~~~~~~~~~|~~~~~~~~~~~~~~~~~~  ",
        "  ~     ~    !  ~    ~     ~  ~   ",
        "    ~    >°)))> SNAP! ~    ~      ",
    ]),
    "miss": "\n".join([
        "  o    ?                          ",
        " /|                               ",
        " /\\           ~                   ",
        "🎣                                ",
        "                  ~ . ~           ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~     ~     ~    ~     ~  ~     ",
        "    ~    ~  ~    ~ ~  ~     ~     ",
    ]),
    "reel_1": "\n".join([
        "  o                               ",
        " /|         ~~~                   ",
        " /\\        /                      ",
        "🎣 ===\\___/                       ",
        "                                  ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  ",
        "  ~     ~     ~    ~     ~  ~     ",
        "         ><(((°>     ~  ~   ~     ",
    ]),
    "reel_2": "\n".join([
        "  o    ~~~                        ",
        " /|   /                           ",
        " /\\  /                            ",
        "🎣 =/    ><(((°>                  ",
        "    *splash*                      ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  ",
        "  ~     ~     ~    ~     ~  ~     ",
        "    ~     ~     ~    ~     ~  ~   ",
    ]),
    "reel_heavy": "\n".join([
        "  o    HNNGH!                     ",
        " /|   /\\                          ",
        " /\\  /  *creak*                   ",
        "🎣 ===\\___      *bend*            ",
        "          \\__                     ",
        "~~~~~~~~~~~~~\\~~~~~~~~~~~~~~~~~   ",
        "  ~     ~    \\\\\\    ~     ~  ~    ",
        "       <>><<((((##))))>>          ",
    ]),
    "reel_light": "\n".join([
        "  o    *whistle*                  ",
        " /|         ~~~~~                 ",
        " /\\        /                      ",
        "🎣 =====\\_/                       ",
        "          ><°>                    ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  ",
        "  ~     ~     ~    ~     ~  ~     ",
        "    ~     ~     ~    ~     ~  ~   ",
    ]),
    "reel_jump": "\n".join([
        "  o    \\o/    *LEAP!*             ",
        " /|        ><(((°>                ",
        " /\\        ___                    ",
        "🎣 ==/    /                       ",
        "        */                        ",
        "~~~~~~/~~~~~~~~~~~~~~~~~~~~~~~~~  ",
        "  ~  /   *splash!*    ~     ~  ~  ",
        "    /     ~     ~    ~     ~  ~   ",
    ]),
    "splash_in": "\n".join([
        "  o    \\o/                        ",
        " /|     |    *KSPLASH!*           ",
        " /\\    /|                         ",
        "🎣 ===   ><((((((°>               ",
        "      ::::::SPLASH:::::           ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  ",
        "  ~  ~  big one  ~ ~  ~  ~        ",
        "    ~     ~     ~    ~     ~  ~   ",
    ]),
    "trash": "\n".join([
        "  o                               ",
        " /|     ~                         ",
        " /\\    ~     [   ]                ",
        "🎣 ==={      |...|                ",
        "             |___|                ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  ",
        "  ~     ~  oh.  ~    ~     ~  ~   ",
        "    ~     ~     ~    ~     ~  ~   ",
    ]),
    "fish": "\n".join([
        "  o     \\o/                       ",
        " /|                                ",
        " /\\        ><((((°>                ",
        "🎣 ===                             ",
        "                                   ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~     ~     ~    ~     ~  ~      ",
        "    ~     ~     ~    ~     ~  ~    ",
    ]),
    "bonus": "\n".join([
        "  o    ✨ \\o/ ✨                  ",
        " /|        |                       ",
        " /\\       /$\\                      ",
        "🎣 ===   |   |                     ",
        "          \\_/                      ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~  ~     ~    ~     ~  ~  ~      ",
        "    ~     ~     ~    ~     ~  ~    ",
    ]),
    "egg": "\n".join([
        "  o    ✨✨✨                     ",
        " /|     .---.                      ",
        " /\\    / ??? \\                     ",
        "🎣 = (   o    )                    ",
        "       \\ ___ /                     ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~  ~  cracking...   ~  ~  ~      ",
        "    ~     ~     ~    ~     ~  ~    ",
    ]),
    "trap_drop": "\n".join([
        "  o                                ",
        " /|         |                      ",
        " /\\        |                       ",
        "🎣 ===\\    |                       ",
        "      \\__ #|#                      ",
        "~~~~~~~~~|#|~~~~~~~~~~~~~~~~~~~~   ",
        "  ~     #|#  *plop*  ~  ~  ~       ",
        "    ~    #    ~    ~     ~  ~      ",
    ]),
    "trap_soak": "\n".join([
        "  o     waiting...                 ",
        " /|                                ",
        " /\\                                ",
        "🎣                                 ",
        "                                   ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~     ~     ~    ~     ~  ~      ",
        "      [#####]   [#####]   [#####]  ",
        "      |     |   |     |   |     |  ",
        "       \\___/     \\___/     \\___/   ",
    ]),
    "dig_start": "\n".join([
        "  o     unfolds the map...           ",
        " /|       _____________              ",
        " /\\     /             /              ",
        "       /  X marks ?  /               ",
        "      /  the spot ? /                ",
        "     /_____________/                 ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~     ",
        "          (digging here)             ",
    ]),
    "dig_chest": "\n".join([
        "  o     \\o/    *clink*               ",
        " /|      |    .------.               ",
        " /\\     /|   |  $$$  |               ",
        "       / |   | ~~~~~ |               ",
        "      /  |   '------'                ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~     ",
        "    treasure!  treasure!  treasure!  ",
        "         (you found something!)      ",
    ]),
    "dig_empty": "\n".join([
        "  o    ?                             ",
        " /|        *digging*                 ",
        " /\\        *digging*                 ",
        "       __\\        /__                ",
        "      |   ::::::::   |               ",
        "      |   :: mud ::  |               ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~     ",
        "       (just dirt and worms)         ",
    ]),
    # ── Beachcomb (free wander; mirrors farm forage) ────────────────
    "beachcomb_start": "\n".join([
        "    o    *combing the shoreline*     ",
        "   /|     ___                        ",
        "   /\\    /   \\    .  ,  .            ",
        "        |  ?  |   ,  ~  ,            ",
        "         \\___/    .  Y  ,            ",
        "  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~       ",
        "       low tide -- what's left?      ",
    ]),
    "beachcomb_lure_purse": "\n".join([
        "    o    .  ,_,  .                   ",
        "   /|     /     \\                    ",
        "   /\\    | $$$  |   *clinks*         ",
        "          \\_____/                    ",
        "  ~  ~  ~  ~  ~  ~  ~  ~  ~          ",
        "   sandy purse  -  LURE inside!      ",
    ]),
    "beachcomb_reel_kicker": "\n".join([
        "    o    .   ____   .                ",
        "   /|       /    \\                   ",
        "   /\\      | REEL |  .  *kicks*      ",
        "            \\____/                   ",
        "  ~  ~  ~  ~  ~  ~  ~  ~  ~          ",
        "   a snagged tackle  -  REEL!        ",
    ]),
    "beachcomb_bait_stash": "\n".join([
        "    o    ,    .---.   .---.          ",
        "   /|       | bt |   | bt |  *wet*   ",
        "   /\\       |____|   |____|          ",
        "  ~  ~  ~  ~  ~  ~  ~  ~  ~          ",
        "   a tin of bait packets!            ",
    ]),
    "beachcomb_treasure_map": "\n".join([
        "    o     *unfolds slowly*           ",
        "   /|       _____________            ",
        "   /\\      /      X      /           ",
        "          /  ~~ ?? ~~  /             ",
        "         /_____________/             ",
        "  ~  ~  ~  ~  ~  ~  ~  ~  ~          ",
        "   soggy treasure map!  ,fish dig    ",
    ]),
    "beachcomb_jackpot": "\n".join([
        "    o   *  .   *  .   *              ",
        "   /|       (((   )))                ",
        "   /\\      (((  *  )))   *glows*     ",
        "           ((( <O> )))               ",
        "             (((   )))               ",
        "  ~  +  ~  +  ~  +  ~  +  ~          ",
        "   ANCIENT RELIC!  pulsing softly    ",
    ]),
    "beachcomb_empty": "\n".join([
        "    o      ,    ,                    ",
        "   /|         seaweed, seaweed       ",
        "   /\\      ,    *  ,    ,            ",
        "         (sigh)                      ",
        "  ~~~~~~~~~~~~~~~~~~~~~~~~~          ",
        "   nothing  -  just shells           ",
    ]),
    "egg_stored": "\n".join([
        "  o    .---.    *cradled*          ",
        " /|   /     \\                      ",
        " /\\  | ?:?:? |                     ",
        "🎣 = | :?:?: |  too many buddies... ",
        "      \\ ___ /  egg saved for later! ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~     ~    sealed up tight   ~   ",
        "    ~     ~     ~    ~     ~  ~    ",
    ]),
    "trap_haul": "\n".join([
        "  o     \\o/    *clackclack*        ",
        " /|      |       _                 ",
        " /\\     [#######] )(  )(  )(       ",
        "🎣 ===  |  >°<  |  ()  ()  ()      ",
        "        |__/_\\__|   ><    ><       ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~  full to the brim with crabs!  ",
        "    ~     ~     ~    ~     ~  ~    ",
    ]),
    "trap_ready": "\n".join([
        "  o     !!!    *splashing*         ",
        " /|                                ",
        " /\\                                ",
        "🎣                                 ",
        "                                   ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  [#####] READY  [#####] READY     ",
        "  |>><  |        |>><  |           ",
        "   \\___/          \\___/            ",
    ]),
    "trap_empty": "\n".join([
        "  o     ...                        ",
        " /|                                ",
        " /\\                                ",
        "🎣     no traps in the water       ",
        "                                   ",
        "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   ",
        "  ~   ~   (calm water)   ~   ~     ",
        "    ~         ~       ~    ~       ",
    ]),
}
# === FRAMES_END ===

# === JUNK_START ===
# ============================================================================
# Junk catalog
# ============================================================================
# Pulled when the outcome roll lands in the "junk" bucket. Junk still
# has tiny salvage value so a streak of bad rolls doesn't feel
# completely empty. Stored in user_fishing.junk_inventory (JSONB) and
# sold via ,fish sell.

JUNK: Final[dict[str, dict]] = {
    "boot":     {"emoji": "\U0001F462", "name": "Soggy Boot",            "salvage_lure": 0.50},
    "bottle":   {"emoji": "\U0001F37E", "name": "Glass Bottle",           "salvage_lure": 1.00},
    "can":      {"emoji": "\U0001F96B", "name": "Rusted Tin Can",         "salvage_lure": 0.75},
    "tire":     {"emoji": "\U0001F6DE", "name": "Old Tire",               "salvage_lure": 2.00},
    "cart":     {"emoji": "\U0001F6D2", "name": "Sunken Shopping Cart",   "salvage_lure": 5.00},
    "bag":      {"emoji": "\U0001F5D1", "name": "Mystery Trash Bag",      "salvage_lure": 1.50},
    "phone":    {"emoji": "\U0001F4F1", "name": "Waterlogged Phone",      "salvage_lure": 3.00},
    "wig":      {"emoji": "\U0001F484", "name": "Lost Wig",               "salvage_lure": 4.00},
    "duck":     {"emoji": "\U0001F986", "name": "Rubber Duck",            "salvage_lure": 6.00},
    "map":      {"emoji": "\U0001F5FA", "name": "Soggy Treasure Map",     "salvage_lure": 8.00},
    "keychain": {"emoji": "\U0001F511", "name": "Crypto Keychain",        "salvage_lure": 7.00},
    "modem":    {"emoji": "\U0001F4DF", "name": "Broken Modem",           "salvage_lure": 5.00},
    "vending":  {"emoji": "\U0001F37F", "name": "Half a Vending Machine", "salvage_lure": 12.00},
    "anchor":   {"emoji": "\U00002693", "name": "Anchor Charm",           "salvage_lure": 9.00},
    "shades":   {"emoji": "\U0001F576", "name": "Movie-Star Shades",      "salvage_lure": 4.50},
    "skull":    {"emoji": "\U0001F480", "name": "Pirate Skull",           "salvage_lure": 15.00},
    "dsd":      {"emoji": "\U0001FA99", "name": "Soggy DSD Note",         "salvage_lure": 10.00},
}

# Equal weights -- junk is RNG flavor, not a strategy lever.
JUNK_WEIGHTS: Final[dict[str, int]] = {k: 1 for k in JUNK.keys()}
# === JUNK_END ===

# === FISH_START ===
# ============================================================================
# Fish catalog
# ============================================================================
# Each fish has:
#     emoji        -- single emoji for embeds and select menus
#     name         -- display name
#     rarity       -- 'common' | 'uncommon' | 'rare' | 'epic' | 'legendary'
#     min_lbs      -- min realised weight (a per-cast random in [min, max])
#     max_lbs      -- max realised weight
#     base_lure     -- LURE per pound paid for selling fresh
#     zones        -- which zones this species can be pulled from
#     min_rod_tier -- lowest rod tier required to even hook this species
#
# Payout formula (services/fishing.py):
#     payout = base_lure * weight_lbs * combo_mult * quality_mult * zone_mult
#
# A "rare/epic/legendary" pull triggers a public announcement embed in the
# fishing channel (configurable per-guild) and an extra bonus to the
# sell price when the fish is unloaded later.

FISH: Final[dict[str, dict]] = {
    # ---- Common ----
    "minnow":    {"emoji": "\U0001F41F", "name": "Minnow",       "rarity": "common",
                  "min_lbs": 0.1, "max_lbs": 0.5, "base_lure": 8.0,
                  "zones": ("pond", "lake", "river"),         "min_rod_tier": 0},
    "carp":      {"emoji": "\U0001F41F", "name": "Carp",         "rarity": "common",
                  "min_lbs": 1.0, "max_lbs": 6.0, "base_lure": 4.0,
                  "zones": ("pond", "lake", "river"),         "min_rod_tier": 0},
    "perch":     {"emoji": "\U0001F41F", "name": "Perch",        "rarity": "common",
                  "min_lbs": 0.5, "max_lbs": 3.0, "base_lure": 6.0,
                  "zones": ("pond", "lake"),                  "min_rod_tier": 0},
    "sardine":   {"emoji": "\U0001F41F", "name": "Sardine",      "rarity": "common",
                  "min_lbs": 0.1, "max_lbs": 0.4, "base_lure": 9.0,
                  "zones": ("ocean", "dock"),          "min_rod_tier": 0},
    "anchovy":   {"emoji": "\U0001F41F", "name": "Anchovy",      "rarity": "common",
                  "min_lbs": 0.05, "max_lbs": 0.3, "base_lure": 12.0,
                  "zones": ("ocean", "dock"),          "min_rod_tier": 0},

    # ---- Uncommon ----
    "bass":      {"emoji": "\U0001F420", "name": "Largemouth Bass","rarity": "uncommon",
                  "min_lbs": 2.0, "max_lbs": 9.0, "base_lure": 18.0,
                  "zones": ("lake", "river"),                  "min_rod_tier": 1},
    "trout":     {"emoji": "\U0001F420", "name": "Rainbow Trout","rarity": "uncommon",
                  "min_lbs": 2.0, "max_lbs": 8.0, "base_lure": 22.0,
                  "zones": ("river", "lake"),                  "min_rod_tier": 1},
    "catfish":   {"emoji": "\U0001F420", "name": "Channel Catfish","rarity": "uncommon",
                  "min_lbs": 3.0, "max_lbs": 25.0, "base_lure": 14.0,
                  "zones": ("river", "lake", "pond"),          "min_rod_tier": 1},
    "mackerel":  {"emoji": "\U0001F420", "name": "Mackerel",    "rarity": "uncommon",
                  "min_lbs": 1.0, "max_lbs": 6.0, "base_lure": 20.0,
                  "zones": ("ocean", "dock"),          "min_rod_tier": 1},
    "herring":   {"emoji": "\U0001F420", "name": "Herring",     "rarity": "uncommon",
                  "min_lbs": 0.4, "max_lbs": 1.8, "base_lure": 24.0,
                  "zones": ("ocean", "dock"),          "min_rod_tier": 1},

    # ---- Rare ----
    "salmon":    {"emoji": "\U0001F41F", "name": "King Salmon",  "rarity": "rare",
                  "min_lbs": 8.0, "max_lbs": 35.0, "base_lure": 38.0,
                  "zones": ("river", "ocean"),                 "min_rod_tier": 2},
    "pike":      {"emoji": "\U0001F420", "name": "Northern Pike","rarity": "rare",
                  "min_lbs": 6.0, "max_lbs": 25.0, "base_lure": 42.0,
                  "zones": ("lake", "river"),                  "min_rod_tier": 2},
    "swordfish": {"emoji": "\U0001F421", "name": "Swordfish",    "rarity": "rare",
                  "min_lbs": 50.0, "max_lbs": 250.0, "base_lure": 28.0,
                  "zones": ("ocean", "dock"),           "min_rod_tier": 2},
    "eel":       {"emoji": "\U0001F40D", "name": "Electric Eel", "rarity": "rare",
                  "min_lbs": 3.0, "max_lbs": 18.0, "base_lure": 55.0,
                  "zones": ("river", "abyss"),                  "min_rod_tier": 2},
    "lobster":   {"emoji": "\U0001F99E", "name": "Lobster",      "rarity": "rare",
                  "min_lbs": 1.0, "max_lbs": 12.0, "base_lure": 90.0,
                  "zones": ("ocean", "dock"),           "min_rod_tier": 2},

    # ---- Epic ----
    "marlin":    {"emoji": "\U0001F421", "name": "Blue Marlin",   "rarity": "epic",
                  "min_lbs": 100.0, "max_lbs": 800.0, "base_lure": 60.0,
                  "zones": ("ocean", "abyss"),                  "min_rod_tier": 3},
    "tuna":      {"emoji": "\U0001F421", "name": "Bluefin Tuna",  "rarity": "epic",
                  "min_lbs": 80.0, "max_lbs": 600.0, "base_lure": 75.0,
                  "zones": ("ocean", "abyss"),                  "min_rod_tier": 3},
    "octopus":   {"emoji": "\U0001F419", "name": "Giant Octopus", "rarity": "epic",
                  "min_lbs": 15.0, "max_lbs": 90.0, "base_lure": 140.0,
                  "zones": ("ocean", "abyss"),                  "min_rod_tier": 3},
    "manta":     {"emoji": "\U0001F420", "name": "Manta Ray",     "rarity": "epic",
                  "min_lbs": 200.0, "max_lbs": 2000.0, "base_lure": 30.0,
                  "zones": ("ocean", "abyss"),                  "min_rod_tier": 3},
    "shark":     {"emoji": "\U0001F988", "name": "Tiger Shark",   "rarity": "epic",
                  "min_lbs": 150.0, "max_lbs": 1200.0, "base_lure": 50.0,
                  "zones": ("ocean", "abyss"),                  "min_rod_tier": 3},

    # ---- Legendary ----
    "kraken":    {"emoji": "\U0001F419", "name": "Kraken",        "rarity": "legendary",
                  "min_lbs": 500.0, "max_lbs": 4000.0, "base_lure": 220.0,
                  "zones": ("abyss",),                          "min_rod_tier": 4},
    "leviathan": {"emoji": "\U0001F40B", "name": "Leviathan",     "rarity": "legendary",
                  "min_lbs": 1000.0, "max_lbs": 9000.0, "base_lure": 180.0,
                  "zones": ("abyss",),                          "min_rod_tier": 4},
    "goldie":    {"emoji": "\U0001F41F", "name": "Golden Fish",  "rarity": "legendary",
                  "min_lbs": 1.0, "max_lbs": 4.0, "base_lure": 25_000.0,
                  "zones": ("pond", "lake", "river", "ocean", "dock", "abyss"),
                  "min_rod_tier": 0},   # the universal jackpot any rod can hit
    "discoin":   {"emoji": "\U0001FA99", "name": "Discoin Fish", "rarity": "legendary",
                  "min_lbs": 5.0, "max_lbs": 20.0, "base_lure": 400.0,
                  "zones": ("dock",),                   "min_rod_tier": 3},

    # ---- Expanded common ----
    "bluegill":  {"emoji": "\U0001F41F", "name": "Bluegill",     "rarity": "common",
                  "min_lbs": 0.2, "max_lbs": 1.5, "base_lure": 7.0,
                  "zones": ("pond", "lake", "swamp"),           "min_rod_tier": 0},
    "sunfish":   {"emoji": "\U0001F41F", "name": "Pumpkinseed",  "rarity": "common",
                  "min_lbs": 0.2, "max_lbs": 1.2, "base_lure": 7.5,
                  "zones": ("pond", "lake", "river"),           "min_rod_tier": 0},
    "mudfish":   {"emoji": "\U0001F41F", "name": "Mudfish",      "rarity": "common",
                  "min_lbs": 0.5, "max_lbs": 4.0, "base_lure": 5.0,
                  "zones": ("pond", "swamp", "river"),          "min_rod_tier": 0},
    "smelt":     {"emoji": "\U0001F41F", "name": "Rainbow Smelt","rarity": "common",
                  "min_lbs": 0.05, "max_lbs": 0.4, "base_lure": 11.0,
                  "zones": ("river", "ocean", "glacier"),   "min_rod_tier": 0},
    "snail":     {"emoji": "\U0001F40C", "name": "Pond Snail",   "rarity": "common",
                  "min_lbs": 0.05, "max_lbs": 0.3, "base_lure": 4.0,
                  "zones": ("pond", "swamp"),                   "min_rod_tier": 0},

    # ---- Expanded uncommon ----
    "koi":       {"emoji": "\U0001F420", "name": "Painted Koi",  "rarity": "uncommon",
                  "min_lbs": 1.5, "max_lbs": 8.0, "base_lure": 32.0,
                  "zones": ("pond", "lake"),                    "min_rod_tier": 1},
    "snapper":   {"emoji": "\U0001F420", "name": "Red Snapper",  "rarity": "uncommon",
                  "min_lbs": 1.5, "max_lbs": 12.0, "base_lure": 26.0,
                  "zones": ("ocean", "reef"),             "min_rod_tier": 1},
    "parrotfish":{"emoji": "\U0001F420", "name": "Parrotfish",   "rarity": "uncommon",
                  "min_lbs": 1.0, "max_lbs": 7.0, "base_lure": 28.0,
                  "zones": ("reef",),                     "min_rod_tier": 1},
    "walleye":   {"emoji": "\U0001F420", "name": "Walleye",      "rarity": "uncommon",
                  "min_lbs": 2.0, "max_lbs": 11.0, "base_lure": 21.0,
                  "zones": ("lake", "river"),                   "min_rod_tier": 1},
    "snakehead": {"emoji": "\U0001F40D", "name": "Snakehead",    "rarity": "uncommon",
                  "min_lbs": 2.5, "max_lbs": 14.0, "base_lure": 24.0,
                  "zones": ("river", "swamp"),                  "min_rod_tier": 1},
    "muckeel":   {"emoji": "\U0001F40D", "name": "Sewer Eel",    "rarity": "uncommon",
                  "min_lbs": 1.5, "max_lbs": 9.0, "base_lure": 30.0,
                  "zones": ("sewer",),                          "min_rod_tier": 1},

    # ---- Expanded rare ----
    "barracuda": {"emoji": "\U0001F420", "name": "Great Barracuda","rarity": "rare",
                  "min_lbs": 8.0, "max_lbs": 45.0, "base_lure": 44.0,
                  "zones": ("ocean", "reef"),             "min_rod_tier": 2},
    "sturgeon":  {"emoji": "\U0001F421", "name": "Lake Sturgeon","rarity": "rare",
                  "min_lbs": 30.0, "max_lbs": 200.0, "base_lure": 36.0,
                  "zones": ("lake", "river", "glacier"),    "min_rod_tier": 2},
    "mahimahi":  {"emoji": "\U0001F420", "name": "Mahi-mahi",    "rarity": "rare",
                  "min_lbs": 15.0, "max_lbs": 60.0, "base_lure": 50.0,
                  "zones": ("ocean", "reef"),             "min_rod_tier": 2},
    "grouper":   {"emoji": "\U0001F421", "name": "Goliath Grouper","rarity": "rare",
                  "min_lbs": 40.0, "max_lbs": 300.0, "base_lure": 32.0,
                  "zones": ("ocean", "reef"),             "min_rod_tier": 2},
    "arapaima":  {"emoji": "\U0001F421", "name": "Arapaima",     "rarity": "rare",
                  "min_lbs": 50.0, "max_lbs": 250.0, "base_lure": 38.0,
                  "zones": ("river", "swamp"),                  "min_rod_tier": 2},
    "gator":     {"emoji": "\U0001F40A", "name": "Sewer Gator", "rarity": "rare",
                  "min_lbs": 20.0, "max_lbs": 150.0, "base_lure": 60.0,
                  "zones": ("sewer", "swamp"),                  "min_rod_tier": 2},

    # ---- Expanded epic ----
    "sailfish":  {"emoji": "\U0001F421", "name": "Atlantic Sailfish","rarity": "epic",
                  "min_lbs": 60.0, "max_lbs": 220.0, "base_lure": 80.0,
                  "zones": ("ocean", "reef"),             "min_rod_tier": 3},
    "char":      {"emoji": "\U0001F420", "name": "Arctic Char",  "rarity": "epic",
                  "min_lbs": 6.0, "max_lbs": 30.0, "base_lure": 130.0,
                  "zones": ("glacier",),                    "min_rod_tier": 3},
    "squid":     {"emoji": "\U0001F991", "name": "Giant Squid",  "rarity": "epic",
                  "min_lbs": 50.0, "max_lbs": 700.0, "base_lure": 90.0,
                  "zones": ("kelp", "abyss"),            "min_rod_tier": 3},
    "coelacanth":{"emoji":"\U0001F420", "name": "Coelacanth",    "rarity": "epic",
                  "min_lbs": 100.0, "max_lbs": 350.0, "base_lure": 160.0,
                  "zones": ("kelp", "temple"),    "min_rod_tier": 3},
    "anglerfish":{"emoji":"\U0001F41F", "name": "Anglerfish",    "rarity": "epic",
                  "min_lbs": 5.0, "max_lbs": 40.0, "base_lure": 220.0,
                  "zones": ("abyss", "temple"),          "min_rod_tier": 3},

    # ---- Expanded legendary ----
    "serpent":   {"emoji": "\U0001F40D", "name": "Frost Serpent","rarity": "legendary",
                  "min_lbs": 200.0, "max_lbs": 2500.0, "base_lure": 250.0,
                  "zones": ("glacier",),                    "min_rod_tier": 4},
    "guardian":  {"emoji": "\U0001F432", "name": "Temple Guardian","rarity": "legendary",
                  "min_lbs": 300.0, "max_lbs": 4000.0, "base_lure": 280.0,
                  "zones": ("temple",),                  "min_rod_tier": 4},
    "phoenix":   {"emoji": "\U0001F420", "name": "Reef Phoenix", "rarity": "legendary",
                  "min_lbs": 5.0, "max_lbs": 25.0, "base_lure": 18_000.0,
                  "zones": ("reef", "temple"),     "min_rod_tier": 4},
    "ancient":   {"emoji": "\U0001F420", "name": "Ancient Carp", "rarity": "legendary",
                  "min_lbs": 50.0, "max_lbs": 800.0, "base_lure": 500.0,
                  "zones": ("temple", "swamp"),          "min_rod_tier": 4},

    # ---- Crabs (trap-only; min_rod_tier=99 makes them un-fishable with a rod) ----
    "bluecrab":  {"emoji": "\U0001F980", "name": "Blue Crab",   "rarity": "common",
                  "min_lbs": 0.3, "max_lbs": 1.5, "base_lure": 18.0,
                  "zones": ("ocean", "dock", "reef"),           "min_rod_tier": 99},
    "mudcrab":   {"emoji": "\U0001F980", "name": "Mud Crab",    "rarity": "common",
                  "min_lbs": 0.4, "max_lbs": 2.0, "base_lure": 14.0,
                  "zones": ("swamp", "river", "sewer", "pond", "lake"), "min_rod_tier": 99},
    "snowcrab":  {"emoji": "\U0001F980", "name": "Snow Crab",   "rarity": "uncommon",
                  "min_lbs": 1.0, "max_lbs": 5.0, "base_lure": 36.0,
                  "zones": ("glacier", "ocean"),                "min_rod_tier": 99},
    "cococrab":  {"emoji": "\U0001F980", "name": "Coconut Crab","rarity": "rare",
                  "min_lbs": 5.0, "max_lbs": 20.0, "base_lure": 90.0,
                  "zones": ("reef", "kelp"),                    "min_rod_tier": 99},
    "spidercrab":{"emoji": "\U0001F980", "name": "Spider Crab", "rarity": "epic",
                  "min_lbs": 15.0, "max_lbs": 80.0, "base_lure": 180.0,
                  "zones": ("kelp", "abyss", "temple"),         "min_rod_tier": 99},
    "kingcrab":  {"emoji": "\U0001F980", "name": "King Crab",   "rarity": "epic",
                  "min_lbs": 8.0, "max_lbs": 40.0, "base_lure": 320.0,
                  "zones": ("glacier", "abyss"),                "min_rod_tier": 99},
    "voidcrab":      {"emoji": "\U0001F980", "name": "Void Crab",      "rarity": "legendary",
                      "min_lbs": 20.0, "max_lbs": 200.0, "base_lure": 600.0,
                      "zones": ("abyss", "temple"),                 "min_rod_tier": 99},
    # ---- Additional crabs for new and expanded zones (trap-only) ----
    "horseshoecrab": {"emoji": "\U0001F980", "name": "Horseshoe Crab", "rarity": "common",
                      "min_lbs": 1.0, "max_lbs": 3.0, "base_lure": 10.0,
                      "zones": ("tidal_pool", "mangrove", "ocean"),  "min_rod_tier": 99},
    "ghostcrab":     {"emoji": "\U0001F980", "name": "Ghost Crab",     "rarity": "uncommon",
                      "min_lbs": 0.2, "max_lbs": 1.0, "base_lure": 30.0,
                      "zones": ("shipwreck", "reef", "tidal_pool"),  "min_rod_tier": 99},
    "mantiscrab":    {"emoji": "\U0001F980", "name": "Mantis Shrimp",  "rarity": "rare",
                      "min_lbs": 0.5, "max_lbs": 3.0, "base_lure": 75.0,
                      "zones": ("reef", "kelp", "bioluminescent_bay"), "min_rod_tier": 99},
    "crystalcrab":   {"emoji": "\U0001F980", "name": "Crystal Crab",   "rarity": "epic",
                      "min_lbs": 2.0, "max_lbs": 12.0, "base_lure": 200.0,
                      "zones": ("crystal_caverns",),                 "min_rod_tier": 99},
    "stormcrab":     {"emoji": "\U0001F980", "name": "Storm Crab",     "rarity": "rare",
                      "min_lbs": 5.0, "max_lbs": 25.0, "base_lure": 110.0,
                      "zones": ("storm_surge", "ocean"),             "min_rod_tier": 99},
    "fireventcrab":  {"emoji": "\U0001F980", "name": "Fire Vent Crab", "rarity": "epic",
                      "min_lbs": 10.0, "max_lbs": 50.0, "base_lure": 350.0,
                      "zones": ("magma",),                           "min_rod_tier": 99},
    "moonphasecrab": {"emoji": "\U0001F980", "name": "Moon Phase Crab","rarity": "epic",
                      "min_lbs": 8.0, "max_lbs": 40.0, "base_lure": 420.0,
                      "zones": ("moonpool",),                        "min_rod_tier": 99},
    "nebulacrab":    {"emoji": "\U0001F980", "name": "Nebula Crab",    "rarity": "legendary",
                      "min_lbs": 30.0, "max_lbs": 180.0, "base_lure": 750.0,
                      "zones": ("nebula",),                          "min_rod_tier": 99},
    # ---- Deep / endgame fish (zones tier 6+) ----
    "anglerfish": {"emoji": "\U0001F420", "name": "Anglerfish",  "rarity": "uncommon",
                   "min_lbs": 3.0, "max_lbs": 18.0, "base_lure": 220.0,
                   "zones": ("trench", "abyss"),                "min_rod_tier": 4},
    "viperfish": {"emoji": "\U0001F41F", "name": "Viperfish",    "rarity": "rare",
                  "min_lbs": 2.0, "max_lbs": 10.0, "base_lure": 380.0,
                  "zones": ("trench", "abyss"),                 "min_rod_tier": 5},
    "moonfish":  {"emoji": "\U0001F319", "name": "Moonfish",     "rarity": "rare",
                  "min_lbs": 4.0, "max_lbs": 25.0, "base_lure": 480.0,
                  "zones": ("moonpool", "void"),                "min_rod_tier": 5},
    "lavafish":  {"emoji": "\U0001F525", "name": "Lavafish",     "rarity": "rare",
                  "min_lbs": 3.0, "max_lbs": 15.0, "base_lure": 520.0,
                  "zones": ("magma",),                          "min_rod_tier": 5},
    "obsidian_eel": {"emoji": "\U0001FA90", "name": "Obsidian Eel", "rarity": "rare",
                    "min_lbs": 6.0, "max_lbs": 30.0, "base_lure": 600.0,
                    "zones": ("magma", "void"),                 "min_rod_tier": 6},
    "voidfish":  {"emoji": "\U0001F300", "name": "Voidfish",     "rarity": "epic",
                  "min_lbs": 8.0, "max_lbs": 60.0, "base_lure": 1200.0,
                  "zones": ("void", "nebula"),                  "min_rod_tier": 7},
    "phoenix_fish": {"emoji": "\U0001F525", "name": "Phoenix Fish", "rarity": "epic",
                    "min_lbs": 5.0, "max_lbs": 40.0, "base_lure": 1500.0,
                    "zones": ("magma", "moonpool"),             "min_rod_tier": 6},
    "starfish_god": {"emoji": "\U00002B50", "name": "Star Fish God", "rarity": "epic",
                    "min_lbs": 10.0, "max_lbs": 100.0, "base_lure": 1800.0,
                    "zones": ("nebula", "void"),                "min_rod_tier": 7},
    "leviathan_calf": {"emoji": "\U0001F40B", "name": "Leviathan Calf", "rarity": "epic",
                      "min_lbs": 50.0, "max_lbs": 400.0, "base_lure": 2400.0,
                      "zones": ("trench", "void"),              "min_rod_tier": 7},
    "celestial_carp": {"emoji": "\U00002728", "name": "Celestial Carp", "rarity": "legendary",
                      "min_lbs": 30.0, "max_lbs": 200.0, "base_lure": 4500.0,
                      "zones": ("nebula",),                     "min_rod_tier": 8},
    "void_leviathan": {"emoji": "\U0001F40B", "name": "Void Leviathan", "rarity": "legendary",
                      "min_lbs": 200.0, "max_lbs": 1500.0, "base_lure": 9000.0,
                      "zones": ("void", "nebula"),              "min_rod_tier": 8},
    "moon_kraken": {"emoji": "\U0001F419", "name": "Moon Kraken", "rarity": "legendary",
                   "min_lbs": 300.0, "max_lbs": 2000.0, "base_lure": 12000.0,
                   "zones": ("moonpool", "void"),               "min_rod_tier": 8},
    "world_serpent_spawn": {"emoji": "\U0001F40D", "name": "World-Serpent Spawn", "rarity": "legendary",
                            "min_lbs": 500.0, "max_lbs": 4000.0, "base_lure": 25000.0,
                            "zones": ("ouroboros",),            "min_rod_tier": 9},
    "ouroboros_eel": {"emoji": "\U0001F40D", "name": "Ouroboros Eel", "rarity": "legendary",
                     "min_lbs": 100.0, "max_lbs": 800.0, "base_lure": 18000.0,
                     "zones": ("ouroboros",),                   "min_rod_tier": 9},
    "the_first_fish": {"emoji": "\U00002728", "name": "The First Fish", "rarity": "legendary",
                       "min_lbs": 1000.0, "max_lbs": 10000.0, "base_lure": 60000.0,
                       "zones": ("ouroboros",),                 "min_rod_tier": 9},

    # ---- Tidal Pool zone ----
    "shore_hopper":  {"emoji": "\U0001F990", "name": "Shore Hopper",       "rarity": "common",
                      "min_lbs": 0.1, "max_lbs": 0.6, "base_lure": 6.0,
                      "zones": ("tidal_pool", "pond"),           "min_rod_tier": 0},
    "anemonefish":   {"emoji": "\U0001F420", "name": "Anemonefish",         "rarity": "uncommon",
                      "min_lbs": 0.5, "max_lbs": 3.0, "base_lure": 25.0,
                      "zones": ("tidal_pool", "reef"),           "min_rod_tier": 0},

    # ---- Mangrove Thicket zone ----
    "mudskipper":    {"emoji": "\U0001F41F", "name": "Mudskipper",          "rarity": "common",
                      "min_lbs": 0.3, "max_lbs": 2.5, "base_lure": 5.0,
                      "zones": ("mangrove", "swamp"),            "min_rod_tier": 0},
    "archerfish":    {"emoji": "\U0001F421", "name": "Archerfish",           "rarity": "uncommon",
                      "min_lbs": 1.0, "max_lbs": 5.0, "base_lure": 28.0,
                      "zones": ("mangrove", "river"),            "min_rod_tier": 1},
    "tarpon":        {"emoji": "\U0001F41F", "name": "Silver Tarpon",        "rarity": "rare",
                      "min_lbs": 20.0, "max_lbs": 120.0, "base_lure": 45.0,
                      "zones": ("mangrove", "ocean"),            "min_rod_tier": 1},

    # ---- Sunken Galleon zone ----
    "wrasse":        {"emoji": "\U0001F420", "name": "Wrasse",               "rarity": "uncommon",
                      "min_lbs": 1.0, "max_lbs": 8.0, "base_lure": 26.0,
                      "zones": ("shipwreck", "reef"),            "min_rod_tier": 2},
    "ghost_eel":     {"emoji": "\U0001F40D", "name": "Ghost Eel",            "rarity": "rare",
                      "min_lbs": 5.0, "max_lbs": 28.0, "base_lure": 60.0,
                      "zones": ("shipwreck", "abyss"),           "min_rod_tier": 2},
    "treasure_fish": {"emoji": "\U0001FA99", "name": "Treasure Carp",       "rarity": "epic",
                      "min_lbs": 2.0, "max_lbs": 12.0, "base_lure": 180.0,
                      "zones": ("shipwreck",),                   "min_rod_tier": 2},

    # ---- Bioluminescent Bay zone ----
    "lanternfish":   {"emoji": "\U0001F4A1", "name": "Lanternfish",          "rarity": "uncommon",
                      "min_lbs": 0.1, "max_lbs": 0.8, "base_lure": 35.0,
                      "zones": ("bioluminescent_bay", "trench"), "min_rod_tier": 3},
    "sea_firefly":   {"emoji": "\U00002728", "name": "Sea Firefly",          "rarity": "rare",
                      "min_lbs": 0.05, "max_lbs": 0.4, "base_lure": 70.0,
                      "zones": ("bioluminescent_bay",),          "min_rod_tier": 3},
    "crystal_ray":   {"emoji": "\U0001F48E", "name": "Crystal Ray",          "rarity": "epic",
                      "min_lbs": 30.0, "max_lbs": 200.0, "base_lure": 120.0,
                      "zones": ("bioluminescent_bay",),          "min_rod_tier": 3},

    # ---- Crystal Caverns zone ----
    "cave_blind":    {"emoji": "\U0001F41F", "name": "Blind Cave Fish",      "rarity": "common",
                      "min_lbs": 0.2, "max_lbs": 1.0, "base_lure": 5.0,
                      "zones": ("crystal_caverns",),             "min_rod_tier": 4},
    "crystal_tetra": {"emoji": "\U0001F48E", "name": "Crystal Tetra",       "rarity": "uncommon",
                      "min_lbs": 0.3, "max_lbs": 2.0, "base_lure": 30.0,
                      "zones": ("crystal_caverns",),             "min_rod_tier": 4},
    "gem_pike":      {"emoji": "\U0001F48E", "name": "Gem Pike",             "rarity": "rare",
                      "min_lbs": 8.0, "max_lbs": 40.0, "base_lure": 55.0,
                      "zones": ("crystal_caverns",),             "min_rod_tier": 4},
    "prism_guardian":{"emoji": "\U0001F308", "name": "Prism Guardian",      "rarity": "legendary",
                      "min_lbs": 80.0, "max_lbs": 600.0, "base_lure": 350.0,
                      "zones": ("crystal_caverns",),             "min_rod_tier": 4},

    # ---- Storm Surge zone ----
    "stormfish":     {"emoji": "\U000026A1", "name": "Stormfish",            "rarity": "rare",
                      "min_lbs": 15.0, "max_lbs": 80.0, "base_lure": 50.0,
                      "zones": ("storm_surge", "ocean"),         "min_rod_tier": 5},
    "maelstrom_eel": {"emoji": "\U0001F40D", "name": "Maelstrom Eel",       "rarity": "legendary",
                      "min_lbs": 150.0, "max_lbs": 1200.0, "base_lure": 300.0,
                      "zones": ("storm_surge",),                 "min_rod_tier": 5},

    # ---- Sewer additions ----
    "sludge_fish":   {"emoji": "\U0001F41F", "name": "Sludge Feeder",        "rarity": "common",
                      "min_lbs": 0.3, "max_lbs": 3.0, "base_lure": 4.0,
                      "zones": ("sewer",),                       "min_rod_tier": 0},
    "pipe_goby":     {"emoji": "\U0001F41F", "name": "Pipe Goby",             "rarity": "uncommon",
                      "min_lbs": 0.5, "max_lbs": 4.0, "base_lure": 22.0,
                      "zones": ("sewer", "river"),               "min_rod_tier": 1},
    # ---- Tidal Pool additions ----
    "blenny":        {"emoji": "\U0001F41F", "name": "Blenny",                "rarity": "common",
                      "min_lbs": 0.1, "max_lbs": 0.5, "base_lure": 7.0,
                      "zones": ("tidal_pool", "reef"),           "min_rod_tier": 0},
    "pipefish":      {"emoji": "\U0001F420", "name": "Pipefish",              "rarity": "uncommon",
                      "min_lbs": 0.1, "max_lbs": 0.8, "base_lure": 30.0,
                      "zones": ("tidal_pool", "kelp"),           "min_rod_tier": 0},
    "goby":          {"emoji": "\U0001F41F", "name": "Goby",                  "rarity": "common",
                      "min_lbs": 0.1, "max_lbs": 0.4, "base_lure": 6.0,
                      "zones": ("tidal_pool", "river", "pond"),  "min_rod_tier": 0},
    # ---- Ocean additions ----
    "hammerhead":    {"emoji": "\U0001F988", "name": "Hammerhead Shark",      "rarity": "epic",
                      "min_lbs": 200.0, "max_lbs": 800.0, "base_lure": 55.0,
                      "zones": ("ocean",),                       "min_rod_tier": 3},
    "lionfish":      {"emoji": "\U0001F421", "name": "Lionfish",              "rarity": "rare",
                      "min_lbs": 2.0, "max_lbs": 8.0, "base_lure": 65.0,
                      "zones": ("ocean", "reef"),                "min_rod_tier": 2},
    # ---- Swamp additions ----
    "bowfin":        {"emoji": "\U0001F41F", "name": "Bowfin",                "rarity": "rare",
                      "min_lbs": 3.0, "max_lbs": 14.0, "base_lure": 40.0,
                      "zones": ("swamp", "river"),               "min_rod_tier": 2},
    "alligator_gar": {"emoji": "\U0001F40A", "name": "Alligator Gar",        "rarity": "epic",
                      "min_lbs": 40.0, "max_lbs": 200.0, "base_lure": 100.0,
                      "zones": ("swamp", "river"),               "min_rod_tier": 3},
    # ---- Reef and Kelp additions ----
    "seahorse":      {"emoji": "\U0001F420", "name": "Seahorse",              "rarity": "uncommon",
                      "min_lbs": 0.05, "max_lbs": 0.3, "base_lure": 45.0,
                      "zones": ("reef", "kelp", "tidal_pool"),   "min_rod_tier": 1},
    "lingcod":       {"emoji": "\U0001F421", "name": "Lingcod",               "rarity": "rare",
                      "min_lbs": 8.0, "max_lbs": 60.0, "base_lure": 48.0,
                      "zones": ("kelp", "ocean"),                "min_rod_tier": 2},
    # ---- Glacier additions ----
    "halibut":       {"emoji": "\U0001F421", "name": "Pacific Halibut",       "rarity": "rare",
                      "min_lbs": 20.0, "max_lbs": 400.0, "base_lure": 40.0,
                      "zones": ("glacier", "ocean"),             "min_rod_tier": 2},
    "beluga_sturgeon": {"emoji": "\U0001F421", "name": "Beluga Sturgeon",     "rarity": "epic",
                        "min_lbs": 100.0, "max_lbs": 1200.0, "base_lure": 95.0,
                        "zones": ("glacier",),                   "min_rod_tier": 3},
    # ---- Trench additions ----
    "dragonfish":    {"emoji": "\U0001F41F", "name": "Dragonfish",            "rarity": "rare",
                      "min_lbs": 1.0, "max_lbs": 8.0, "base_lure": 400.0,
                      "zones": ("trench", "abyss"),              "min_rod_tier": 5},
    "pelican_eel":   {"emoji": "\U0001F40D", "name": "Pelican Eel",           "rarity": "epic",
                      "min_lbs": 2.0, "max_lbs": 15.0, "base_lure": 1000.0,
                      "zones": ("trench",),                      "min_rod_tier": 5},
    # ---- Storm Surge additions ----
    "tempest_shark": {"emoji": "\U0001F988", "name": "Tempest Shark",         "rarity": "epic",
                      "min_lbs": 100.0, "max_lbs": 500.0, "base_lure": 90.0,
                      "zones": ("storm_surge",),                 "min_rod_tier": 5},
    "thunderskate":  {"emoji": "\U000026A1", "name": "Thunderskate",          "rarity": "uncommon",
                      "min_lbs": 5.0, "max_lbs": 30.0, "base_lure": 32.0,
                      "zones": ("storm_surge", "ocean"),         "min_rod_tier": 5},
    # ---- Moonpool additions ----
    "oarfish":       {"emoji": "\U0001F41F", "name": "Oarfish",               "rarity": "epic",
                      "min_lbs": 100.0, "max_lbs": 700.0, "base_lure": 1400.0,
                      "zones": ("moonpool",),                    "min_rod_tier": 6},
    "lunar_cod":     {"emoji": "\U0001F319", "name": "Lunar Cod",             "rarity": "rare",
                      "min_lbs": 3.0, "max_lbs": 20.0, "base_lure": 500.0,
                      "zones": ("moonpool",),                    "min_rod_tier": 6},
    # ---- Mangrove addition ----
    "payara":        {"emoji": "\U0001F421", "name": "Payara",                "rarity": "rare",
                      "min_lbs": 5.0, "max_lbs": 30.0, "base_lure": 48.0,
                      "zones": ("mangrove", "river"),            "min_rod_tier": 1},
}

# Rarity-roll weights inside the "fish" bucket. Bigger = more common.
# Cumulative probability of a non-common pull lands roughly:
#   uncommon ~30%, rare ~10%, epic ~3%, legendary ~0.6%.
RARITY_WEIGHTS: Final[dict[str, int]] = {
    "common":    560,
    "uncommon":  290,
    "rare":      100,
    "epic":       42,
    "legendary":   8,
}

# Rarity meta -- color, label, multiplier on the base LURE on sale.
RARITY_META: Final[dict[str, dict]] = {
    "common":    {"label": "Common",    "color_hex": 0x95a5a6, "sell_mult": 1.0,  "splash": False},
    "uncommon":  {"label": "Uncommon",  "color_hex": 0x2ecc71, "sell_mult": 1.1,  "splash": False},
    "rare":      {"label": "Rare",      "color_hex": 0x3498db, "sell_mult": 1.25, "splash": True},
    "epic":      {"label": "Epic",      "color_hex": 0x9b59b6, "sell_mult": 1.5,  "splash": True},
    "legendary": {"label": "Legendary", "color_hex": 0xf1c40f, "sell_mult": 2.0,  "splash": True},
}

# ============================================================================
# Fish facts
# ============================================================================
# Per-species flavor text shown on the catch embed. Each entry is a tuple
# of 2-4 short facts; one is chosen at random each catch so repeat
# fishers see variety. Keys must match FISH exactly. Fish with no entry
# silently skip the fact line (no error).

FISH_FACTS: Final[dict[str, tuple[str, ...]]] = {
    # ---- Common ----
    "minnow":    ("Often used as bait themselves -- the food chain is merciless.",
                  "Schools of thousands navigate as if sharing a single mind.",
                  "The word 'minnow' covers hundreds of different small species."),
    "carp":      ("Carp can live over 20 years and remember their own reflection.",
                  "Considered invasive in North America, sacred in East Asia.",
                  "Koi are just ornamental carp bred for color. Same fish, fancy hat."),
    "perch":     ("Perch hunt in coordinated packs, herding prey into tight balls.",
                  "Their stripe pattern breaks up their outline in weedy shallows.",
                  "One of the most widely distributed freshwater fish on Earth."),
    "sardine":   ("A single sardine school can number in the millions.",
                  "They sense each other's movement through water pressure, not sight.",
                  "Named after Sardinia, where they were once so thick you could walk on them."),
    "anchovy":   ("Schools so dense they appear as a solid mass on sonar.",
                  "The Roman condiment garum was made from fermented anchovies.",
                  "Anchovies are the backbone of the Mediterranean food web."),
    "bluegill":  ("The blue patch on their gill covers gives them their name.",
                  "Males fan circular nests and guard eggs aggressively.",
                  "They can hybridize with several other sunfish species."),
    "sunfish":   ("The pumpkinseed earns its name from its rounded orange-flecked body.",
                  "Males build circular nests that cluster into spawning colonies.",
                  "Small but ferocious -- they'll chase fish five times their size."),
    "mudfish":   ("Mudfish survive droughts by burrowing into damp mud and waiting.",
                  "They can breathe air and cross short stretches of land.",
                  "Tastes exactly like what it lives in. Bon appetit."),
    "smelt":     ("Smelt runs in spring attract bears, eagles, and humans alike.",
                  "Dried smelt were historically used as torches -- that's how oily they are.",
                  "They spawn in river gravel, then return to open water."),
    "snail":     ("Freshwater snails graze algae and are vital to pond health.",
                  "Some pond snail species live over 15 years. Slow and very steady.",
                  "You caught a snail. With a fishing rod. This is fine."),
    "shore_hopper": ("Shore hoppers shelter in rock crevices when the tide rises.",
                     "They detect wave patterns by sensing water pressure changes.",
                     "So small they're routinely mistaken for wet sand."),
    "mudskipper":   ("Mudskippers breathe through their skin when out of water.",
                     "They haul themselves around on pectoral fins like stubby arms.",
                     "Males dig burrows, guard eggs, and are aggressively devoted fathers."),
    "sludge_fish":  ("Adapted to near-zero oxygen by growing an extra gill chamber.",
                     "Its flavor is a subject of ongoing scientific debate.",
                     "Has never seen sunlight. Thriving anyway."),
    "blenny":       ("Blennies hop between tide pools using their fins to push off rocks.",
                     "Many species perform elaborate courtship dances on exposed boulders.",
                     "Can survive hours out of water as long as their skin stays damp."),
    "goby":         ("Gobies are one of the most species-rich fish families, with 2,000+ members.",
                     "Their fused pelvic fins form a suction cup that anchors them to rocks.",
                     "Tiny but territorial -- they'll challenge fish ten times their size."),
    "cave_blind":   ("After thousands of generations underground, its eyes are now vestigial.",
                     "It navigates entirely by detecting water pressure changes.",
                     "Cave fish populations evolve faster than almost any other vertebrate."),
    # ---- Uncommon ----
    "bass":      ("Bass are ambush predators that memorize their territory obsessively.",
                  "Largemouth bass can eat prey up to half their own body size.",
                  "Sport fishing tournaments revolve around them like a second religion."),
    "trout":     ("Trout require cold, clean water -- they're canaries for stream health.",
                  "They return to the exact stream they hatched in to spawn.",
                  "Their spots act as camouflage over the dappled riverbed."),
    "catfish":   ("A catfish's whiskers are packed with taste receptors -- they taste with their face.",
                  "They can detect electric fields in the water using modified cells.",
                  "Channel catfish can live over 40 years in the wild."),
    "mackerel":  ("Mackerel have no swim bladder -- they must swim continuously or sink.",
                  "Their iridescent scales shift color as the light angle changes.",
                  "They burn through food so fast they can double in size in a season."),
    "herring":   ("Herring communicate by releasing bubbles from their swim bladders.",
                  "A single school can span several kilometers and weigh thousands of tons.",
                  "They spawn in huge synchronized events that carpet entire bays."),
    "koi":       ("Koi are ornamental carp selectively bred for color over a thousand years.",
                  "Some individual koi live over 200 years -- the record holder was 226.",
                  "Their color patterns are unique to each fish, like fingerprints."),
    "snapper":   ("Red snappers live in loose reef communities with established hierarchies.",
                  "Their deep-red coloration fades to pale pink in very deep water.",
                  "They can live up to 50 years. You may have caught someone's great-grandchild."),
    "parrotfish": ("Parrotfish have fused teeth that form a beak for scraping coral.",
                   "They produce white sand -- essentially digested and excreted coral.",
                   "They secrete a mucus sleeping bag around themselves every night."),
    "walleye":   ("Walleye have a reflective layer in their eyes for hunting in low light.",
                  "They're most active at dusk and dawn, when visibility favors them.",
                  "State fish of Minnesota -- rivaled only by hot dish in cultural importance."),
    "snakehead": ("Snakeheads can breathe air and travel overland between water bodies.",
                  "They're apex predators that establish dominance within hours of entering new water.",
                  "US authorities require the immediate killing of any snakehead caught."),
    "muckeel":   ("Sewer eels develop electroreceptors to navigate in zero-visibility water.",
                  "They can survive in water with almost no dissolved oxygen.",
                  "Nobody has successfully kept one in an aquarium. It prefers worse conditions."),
    "anemonefish": ("Clownfish are coated in mucus that protects them from the anemone's sting.",
                    "If the female dies, the dominant male changes sex to replace her.",
                    "Their wiggling dance actually aerates the anemone's tentacles."),
    "wrasse":    ("Cleaner wrasse set up stations where larger fish queue for parasite removal.",
                  "Most wrasse species change sex from female to male mid-life.",
                  "Their pharyngeal jaw is a second set of jaws in their throat."),
    "seahorse":  ("Seahorses are the only fish where males carry and birth the young.",
                  "They're the slowest fish in the ocean -- top speed about 1.5 m/h.",
                  "Pairs greet each other every morning with a synchronized color-change dance."),
    "pipefish":  ("Pipefish are related to seahorses -- and, like them, males carry the eggs.",
                  "They hover motionless among kelp, their body a perfect camouflage.",
                  "Some species are thinner than a pencil and over a foot long."),
    "pipe_goby": ("This goby evolved specifically to navigate sewer pipe currents.",
                  "It can echolocate off pipe walls in complete darkness.",
                  "Scientists suspect it has been in the sewers longer than the sewers have."),
    "archerfish": ("Archerfish shoot jets of water to knock insects off overhanging branches.",
                   "They account for light refraction when aiming -- physics in a fish.",
                   "A shot can travel up to 3 meters with enough force to stun prey."),
    "thunderskate": ("Thunderskates generate weak electric pulses to navigate storm-churned water.",
                     "Their flattened body planes through current like a flying wing.",
                     "The electrical discharge intensifies in proximity to actual lightning."),
    "lanternfish":  ("Lanternfish are possibly the most abundant vertebrate on Earth by total mass.",
                     "They migrate hundreds of meters up to feed at night, then descend at dawn.",
                     "Their bioluminescent organs match surface light to become invisible from below."),
    "crystal_tetra": ("Crystal tetras travel in synchronized schools that refract light like prisms.",
                      "Their scales grow a new crystalline layer each year, like tree rings.",
                      "Researchers use their scale patterns to date geological cavern formations."),
    # ---- Rare ----
    "salmon":    ("Salmon return to the exact stream they hatched in, guided by chemical memory.",
                  "After spawning, Pacific salmon die and their bodies feed the whole stream.",
                  "Their pink flesh comes from astaxanthin in the krill they eat."),
    "pike":      ("Northern pike are apex ambush predators that strike with explosive acceleration.",
                  "They can live over 30 years and grow more cannibalistic with age.",
                  "A large pike will eat ducklings, muskrats, and smaller pike without hesitation."),
    "swordfish": ("Swordfish slash through schools with their bill, then return to eat the stunned fish.",
                  "Uniquely, they maintain their brain warmer than the surrounding water.",
                  "They can exceed 1,400 pounds and swim over 60 mph in short bursts."),
    "eel":       ("Electric eels can generate 600 volts -- enough to stun a horse.",
                  "They breathe air, surfacing every few minutes like a fishy whale.",
                  "Prey is immobilized before the eel even needs to bite."),
    "lobster":   ("Lobsters are crustaceans, not fish. Close enough.",
                  "They can live over 100 years and show no biological signs of aging.",
                  "They communicate by urinating on each other. It's called chemical signaling."),
    "barracuda": ("Barracuda can accelerate to 36 mph -- faster than you can react.",
                  "They're attracted to shiny objects and have mistaken jewelry for prey.",
                  "A single barracuda sometimes herds an entire school of fish alone."),
    "sturgeon":  ("Sturgeon have been largely unchanged for 200 million years.",
                  "Lake sturgeon can live over 150 years and grow to 7 feet.",
                  "Their eggs are harvested as caviar -- some species worth more than gold per pound."),
    "mahimahi":  ("Mahi-mahi grow 1 inch per day as juveniles -- fastest growth of any large fish.",
                  "They flash electric blue and gold when excited.",
                  "'Mahi' means 'strong' in Hawaiian. Nothing to do with dolphins."),
    "grouper":   ("Goliath grouper can weigh 800 pounds and inhale prey whole.",
                  "They gather in spawning aggregations of hundreds at the same location every year.",
                  "Some individuals defend the same reef territory for over 30 years."),
    "arapaima":  ("The arapaima is one of the world's largest freshwater fish, reaching 15 feet.",
                  "It breathes air and must surface every 20 minutes -- you can actually hear them.",
                  "It jumps from the water to grab insects, birds, and small monkeys."),
    "gator":     ("Florida sewer gators began as escaped pets in the 1970s.",
                  "They've been down there long enough to develop mild photosensitivity.",
                  "Pest control stopped counting at some point. It's an ecosystem now."),
    "bowfin":    ("Bowfin are a relic species unchanged for over 150 million years.",
                  "They breathe air using a modified swim bladder as a primitive lung.",
                  "Males guard eggs with ferocity that discourages any intruder."),
    "lionfish":  ("Lionfish deliver venom through 18 spines -- not fatal, but extremely painful.",
                  "They've become one of the most destructive invasive species in the Atlantic.",
                  "They eat almost any fish that fits in their mouth, including reef juveniles."),
    "tarpon":    ("Tarpon can live over 80 years and the largest exceed 8 feet.",
                  "They roll at the surface to gulp air when oxygen is low.",
                  "Their armor-like scales have been found in pre-Columbian artifacts."),
    "ghost_eel": ("No confirmed sightings before 2019 -- previously thought to be sailor myth.",
                  "Its bioluminescent skin glows brightest in the presence of stress hormones.",
                  "Wreck divers report it appears from nowhere and vanishes the same way."),
    "halibut":   ("Pacific halibut are born with eyes on both sides. One migrates as they mature.",
                  "Both eyes end up on the same side. The other side is blank.",
                  "The largest halibut ever caught weighed 515 pounds."),
    "lingcod":   ("Raw lingcod flesh is a vivid turquoise-green -- it turns white when cooked.",
                  "Males guard the nest and are the primary parental caretakers.",
                  "They're ambush predators that sit motionless and then explode into action."),
    "payara":    ("The payara, or 'vampire fish', has hollow fangs up to 6 inches long.",
                  "Its teeth point backward so prey cannot escape once gripped.",
                  "It preys on piranha. Let that sink in."),
    "viperfish": ("Viperfish have teeth so large they can't close their mouths.",
                  "They swim at 2 mph but attack in bursts that blur on camera.",
                  "Their bioluminescent lure dangles from a dorsal spine over their head."),
    "moonfish":  ("Moonfish are warm-blooded, maintaining body temperature in near-freezing water.",
                  "They swim vertically -- upright in the water column, scanning below them.",
                  "Their orbit brings them to the surface only during specific lunar phases."),
    "lavafish":  ("Lavafish coat their scales with iron sulfide mined from the vent itself.",
                  "Blood temperature exceeds 140 degrees Fahrenheit. It evolved in it.",
                  "They eat chemosynthetic bacteria that grow in the vent discharge."),
    "obsidian_eel": ("Its skin is formed from volcanic glass spun at the cellular level.",
                     "It leaves a faint scratch on the seafloor wherever it moves.",
                     "Contact burns. Not hypothetically."),
    "gem_pike":  ("Crystal deposits accumulate on its scales over decades of cave life.",
                  "The older the fish, the more faceted its surface -- it refracts lanternlight.",
                  "Cavern miners once used gem pikes as living flashlights."),
    "dragonfish": ("Dragonfish produce red bioluminescence invisible to most deep-sea creatures.",
                   "This lets them hunt unseen, like a sniper with night-vision.",
                   "Their photophore organs can be individually controlled, like pixels."),
    "lunar_cod": ("Lunar cod feed only during specific lunar phases, then fast until the next.",
                  "Their scales align with moon phase cycles in measurable magnetic patterns.",
                  "You caught one. It looked at the moon. This felt personal."),
    "stormfish": ("Stormfish heart rhythm synchronizes with lightning strike intervals.",
                  "The electrical discharge from a storm accelerates their metabolism.",
                  "They swim toward weather fronts. Toward. Not away."),
    "sea_firefly": ("Sea fireflies secrete bioluminescent fluid that glows for hours after contact.",
                    "Japanese soldiers in WWII used dried sea fireflies as low-visibility reading lights.",
                    "Its glow fades completely when frightened -- it goes dark under stress."),
    # ---- Epic ----
    "marlin":    ("Blue marlin can reach 11 feet and weigh over 1,800 pounds.",
                  "They use their bill to stun prey before circling back to feed.",
                  "Spawning aggregations travel thousands of miles following the Gulf Stream."),
    "tuna":      ("Bluefin tuna are warm-blooded and maintain heat in cold water.",
                  "They swim continuously at up to 50 mph and never fully stop to sleep.",
                  "A single bluefin sold for $3 million USD at Tsukiji market in 2019."),
    "octopus":   ("Octopus have three hearts and blue copper-based blood.",
                  "Each arm has its own neural cluster and thinks partly independently.",
                  "They can change both color and texture within 200 milliseconds."),
    "manta":     ("Manta rays have the largest brain-to-body ratio of any fish.",
                  "They're the only fish confirmed to recognize themselves in mirrors.",
                  "Mantas have no stinger. Their size is their only defense."),
    "shark":     ("Sharks predate dinosaurs by 200 million years and outlasted every mass extinction.",
                  "They replace teeth continuously -- some species shed 35,000 teeth in a lifetime.",
                  "They can detect one part blood per million parts water."),
    "sailfish":  ("Sailfish are the fastest fish, clocked at 68 mph in short bursts.",
                  "They raise their dorsal fin to herd sardines into tight bait balls.",
                  "Their bill slashes through schools, stunning prey for easy collection."),
    "char":      ("Arctic char live in some of the coldest freshwater on Earth.",
                  "They survive under Arctic ice in water hovering just above freezing.",
                  "Their flesh is prized for its rich fat content built up against the cold."),
    "squid":     ("Giant squid eyes can reach 10 inches across -- the largest of any animal.",
                  "They have three hearts and propel themselves by jet.",
                  "Their beak is hard enough to cut steel cable. Scientists confirmed this."),
    "coelacanth": ("Coelacanths were believed extinct for 66 million years until 1938.",
                   "Their lobed fins move in a pattern that prefigures the walking limb.",
                   "They give birth to live young after a gestation of up to three years."),
    "anglerfish": ("The bioluminescent lure is produced by symbiotic bacteria inside the bulb.",
                   "Deep-sea anglerfish fuse permanently to a mate and share a bloodstream.",
                   "In the trench, they're among the top predators despite being quite small."),
    "hammerhead": ("The hammer-shaped head provides 360-degree vertical vision.",
                   "Electroreceptors span the hammerhead, giving them exceptional prey detection.",
                   "They can sense a fish buried under sand from several meters away."),
    "alligator_gar": ("Alligator gar have existed for over 100 million years, largely unchanged.",
                      "Their interlocked ganoid scales were used as arrowheads by indigenous peoples.",
                      "They can breathe air and survive in oxygen-depleted water."),
    "crystal_ray": ("Its crystalline spine channels electrical charge across its full wingspan.",
                    "The glow intensifies in deeper water, attracting prey like a moving lamp.",
                    "Scientists believe the crystal structure grows a new layer each season."),
    "beluga_sturgeon": ("Beluga sturgeon are the largest freshwater fish, reaching 24 feet.",
                        "Their eggs, beluga caviar, sell for up to $25,000 per kilogram.",
                        "A beluga can live over 100 years and retains its ancient armored body plan."),
    "pelican_eel": ("The pelican eel can unhinge its jaw to swallow prey many times its size.",
                    "Its luminous tail tip dangles in dark water to lure curious prey closer.",
                    "It is almost entirely stomach. A mouth with a fish attached."),
    "tempest_shark": ("Tempest sharks navigate by reading the electromagnetic field of lightning strikes.",
                      "A storm's charge actually triggers their hunting instinct.",
                      "They breach the surface during lightning. Nobody told them not to."),
    "oarfish":   ("Oarfish are the longest bony fish alive, reaching up to 56 feet.",
                  "Sightings of beached oarfish likely inspired sea serpent myths worldwide.",
                  "They've been observed swimming vertically, tail pointing straight down."),
    "treasure_fish": ("Its gold-iridescent scales convinced 16th-century sailors of cursed doubloons.",
                      "It lives only in wrecks and seems to treat accumulated treasure as substrate.",
                      "Technically still owns the ship it lives in under maritime salvage law."),
    "phoenix_fish": ("Phoenix fish are plasma-blooded -- body temperature is technically ionized.",
                     "When mortally startled, they dissolve into light and reconstitute in minutes.",
                     "Scientists observe them through heat-resistant equipment. Regular cameras melt."),
    "starfish_god": ("Despite the name it bears no relation to starfish -- its classification is disputed.",
                     "Its seven limbs regenerate independently and are capable of individual feeding.",
                     "Three universities are in an ongoing argument about what phylum it belongs to."),
    "leviathan_calf": ("This is a juvenile leviathan. It is already three times your size.",
                       "Adult leviathans are measured in miles. This one is measured in boats.",
                       "You caught it. You have made a choice about how you spend your evening."),
    "voidfish":  ("Photophobic -- it dissolves the light immediately around itself as a defense.",
                  "Exists partially outside conventional spatial dimensions.",
                  "Deep-sea cameras malfunction near it. Shadow expands. No footage recovered."),
    # ---- Legendary ----
    "kraken":    ("Norse sailors who returned from encounters with the kraken usually didn't.",
                  "The giant squid, its real-world counterpart, can reach 43 feet.",
                  "It doesn't come to you. You came to it. Consider that."),
    "leviathan": ("Every ocean mythology on Earth independently invented the leviathan.",
                  "The biblical leviathan breathed fire. This one is wetter but no less annoyed.",
                  "It has existed in some form longer than most geological features."),
    "goldie":    ("There's one in every body of water. It watches. It knows.",
                  "No two anglers catch the same goldie -- and yet it is always the same fish.",
                  "Biologists have given up trying to classify it. The paperwork alone took a decade."),
    "discoin":   ("The only fish with a verified on-chain wallet.",
                  "Its scales are DSD-yellow. That's not a coincidence.",
                  "You caught the fish the bot runs on. This is a conflict of interest."),
    "serpent":   ("Frost crystals form on its scales in real-time as it moves.",
                  "Glacier myths across every northern culture describe exactly this animal.",
                  "The temperature in its immediate vicinity drops measurably."),
    "guardian":  ("The Temple Guardian has watched over these sunken halls for thousands of years.",
                  "It allows itself to be caught once per generation, then returns.",
                  "Several archaeologists believe the temple was built specifically to house it."),
    "phoenix":   ("It dies in the warmth of the coral and is reborn within the same tide.",
                  "Every reef phoenix is technically the same fish on an infinite loop.",
                  "Catching it breaks the cycle -- temporarily. It's already reforming somewhere."),
    "ancient":   ("The Ancient Carp predates every empire. It has watched all of them fall.",
                  "Its scales carry sediment layers dating to the Holocene.",
                  "It will outlive everything currently worried about outliving things."),
    "celestial_carp": ("Celestial carp travel between water bodies via meteor showers.",
                       "Their scales contain microscopic impact craters from atmospheric entry.",
                       "You caught one. It's unclear how it got here from the nebula."),
    "void_leviathan": ("Its shadow arrives several seconds before it does.",
                       "It exists at the intersection of two spatial geometries that shouldn't overlap.",
                       "When it surfaces, the water doesn't ripple. It just isn't there anymore."),
    "moon_kraken": ("Coastal tidal patterns shift measurably when the moon kraken breathes.",
                    "It surfaces once per lunar cycle. Every maritime culture has a different name.",
                    "The pull you felt on the line before the catch -- that was its patience."),
    "world_serpent_spawn": ("This is a child of Jormungandr, the world-encircling serpent of Norse myth.",
                            "The original can be found biting its own tail below the observable universe.",
                            "This one is very young. It is still twice the length of your boat."),
    "ouroboros_eel": ("It is simultaneously swallowing its own tail in an adjacent dimension.",
                      "Every time it completes the loop, a minor timeline is consumed.",
                      "It doesn't struggle on the line. It finds this tiresome."),
    "the_first_fish": ("Before anything else, there was this fish.",
                       "It predates water. It is not pleased about the situation.",
                       "Every fish that has ever lived is, in some technical sense, this fish."),
    "prism_guardian": ("Ancient civilizations ground its scales into mirrors of extraordinary clarity.",
                       "The oldest known prism guardian mirrors date to 4000 BC.",
                       "Its scales grow new refractive layers every century."),
    "maelstrom_eel": ("The storm doesn't disturb it. It is the storm.",
                      "Fishermen who catch one report the weather clears immediately afterward.",
                      "Its body generates enough static charge to be detected from 3 miles away."),
    # ---- Crabs (trap-only) ----
    "bluecrab":  ("Blue crab is Maryland's most recognizable cultural export.",
                  "They molt 25 or more times before reaching adult size.",
                  "Soft-shell crab is a regular crab caught immediately after molting."),
    "mudcrab":   ("Mud crab claws can generate over 500 pounds of grip force.",
                  "They walk sideways for mechanical efficiency -- all legs point the same direction.",
                  "They bury themselves in mud to survive temperature extremes."),
    "snowcrab":  ("Snow crab legs can span 3 feet across on large adults.",
                  "Harvested commercially in the Bering Sea under extremely harsh conditions.",
                  "Their cold-water flesh has a naturally sweet, delicate flavor."),
    "cococrab":  ("Coconut crabs are the world's largest land arthropod, up to 9 pounds.",
                  "They crack open coconuts with their claws -- hence the name.",
                  "They can live up to 60 years and remember threats for life."),
    "spidercrab": ("The Japanese spider crab has the largest leg span of any arthropod -- 18 feet.",
                   "They camouflage themselves by attaching sponges and anemones to their shells.",
                   "Despite their appearance, they're slow-moving and harmless."),
    "kingcrab":  ("King crabs are not true crabs -- they're more closely related to hermit crabs.",
                  "They walk in 'pods' during migration, stacking on top of each other.",
                  "A single king crab leg can weigh over a pound."),
    "voidcrab":  ("No scientific classification has been agreed upon. There's a committee.",
                  "Its shell absorbs light rather than reflecting it -- it appears as a gap.",
                  "Don't think about it."),
    "horseshoecrab": ("Horseshoe crabs are closer to arachnids than true crabs -- ancient relatives.",
                      "Their blue blood is used in biomedical testing for bacterial contamination.",
                      "They've been unchanged for 450 million years. Evolution decided they were done."),
    "ghostcrab": ("Ghost crabs can run sideways at 10 mph -- the fastest crustacean on land.",
                  "They cool down by dipping their legs in the surf, then retreating inland.",
                  "Their pale coloring provides camouflage on sand at night."),
    "mantiscrab": ("Mantis shrimp punch with 1,500 Newtons -- enough to shatter aquarium glass.",
                   "They see 16 types of color versus humans' 3.",
                   "Their punch cavitates the water around it. The bubble collapse hits almost as hard."),
    "crystalcrab": ("Its shell has piezoelectric properties -- it generates charge when compressed.",
                    "Crystal cavern water gives the shell its refractive lattice structure.",
                    "The older the crab, the more faceted and gem-like the shell becomes."),
    "stormcrab": ("Its claws build up static charge through friction when snapping repeatedly.",
                  "It releases charge through its antennae during the snap.",
                  "Fishermen avoid handling them in thunderstorms."),
    "fireventcrab": ("Fire vent crabs live at temperatures that would cook any other crustacean.",
                     "Their hemolymph carries a heat-absorbing compound found nowhere else in biology.",
                     "The shell is so thermally insulated it stays cool to the touch externally."),
    "moonphasecrab": ("Moon phase crabs only molt during new moons, when tidal pull is weakest.",
                      "Their molt cycle is so precisely lunar that sailors used them as calendars.",
                      "They pulse bioluminescent signals that mirror current moon phase data."),
    "nebulacrab": ("Nebula crabs leave a faint iridescent trail as they move through dark water.",
                   "Their shell is studded with crystallized starfall -- actual celestial mineral deposits.",
                   "Biologists have found no mechanism by which they could exist. They exist anyway."),
    # ---- Other new fish ----
    "anemonefish": ("Clownfish live in anemone tentacles that would kill most fish -- they're immune.",
                    "If the female dies, the dominant male changes sex to replace her.",
                    "Their wiggling dance actually aerates the anemone's tentacles."),
    "wrasse":    ("Cleaner wrasse set up 'stations' where larger fish queue for parasite removal.",
                  "Most wrasse species change sex from female to male mid-life.",
                  "Their pharyngeal jaw is a second set of jaws in their throat for crushing prey."),
    "tarpon":    ("Tarpon can live over 80 years and the largest exceed 8 feet.",
                  "They roll at the surface to gulp air when dissolved oxygen is low.",
                  "Their armor-like scales have been found in pre-Columbian archaeological sites."),
    "ghost_eel": ("No confirmed sightings before 2019 -- previously believed to be sailor myth.",
                  "Its bioluminescent skin glows brightest in the presence of stress hormones.",
                  "Wreck divers report it appears from nowhere and vanishes the same way."),
    "lanternfish":  ("Lanternfish may be the most abundant vertebrate on Earth by total mass.",
                     "They migrate hundreds of meters upward to feed at night, then descend at dawn.",
                     "Their photophores match downwelling surface light to become invisible from below."),
    "sea_firefly": ("Sea fireflies secrete bioluminescent fluid that glows for hours after contact.",
                    "Japanese soldiers in WWII used dried sea fireflies as low-visibility reading lights.",
                    "Its glow fades completely when the creature is frightened."),
    "treasure_fish": ("Its gold-iridescent scales convinced 16th-century sailors of cursed doubloons.",
                      "It lives exclusively in wrecks and treats accumulated treasure as substrate.",
                      "Technically still owns the ship it lives in under maritime salvage law."),
}

# Additional facts merged with FISH_FACTS by fish_fact(). Keeping them
# separate makes both dicts easier to read and extend independently.
FISH_FACTS_EXTRA: Final[dict[str, tuple[str, ...]]] = {
    # ---- Common ----
    "minnow":    ("They warn each other of danger using chemical alarm signals released when injured.",
                  "Young bass fry eat minnows. Young minnows eat smaller minnows. It goes down."),
    "carp":      ("The oldest confirmed koi -- 'Hanako' -- died in 1977 at an estimated 226 years old.",
                  "Carp can detect magnetic fields, which they use for spatial navigation in large lakes."),
    "perch":     ("Perch eggs are laid in long accordion-like ribbons draped over submerged vegetation.",
                  "They travel in schools of up to 400 during spring and fall migrations."),
    "sardine":   ("The Great Sardine Run off South Africa draws sharks, dolphins, and whales for weeks.",
                  "Sardines can live up to 14 years -- longer than most people expect from a tin."),
    "anchovy":   ("They filter-feed on plankton using gill rakers, not active hunting.",
                  "Their fermented oil is the secret depth in more sauces than you'd want to know."),
    "bluegill":  ("Bluegill can recognize individual human faces -- confirmed in a controlled study.",
                  "Their nesting depressions can be identified for years after by the circular mark."),
    "sunfish":   ("Their young are nearly transparent at hatching -- visible organs and all.",
                  "Pumpkinseed sunfish were introduced to Europe in the 1800s as ornamental fish."),
    "mudfish":   ("They emit an audible groan when disturbed. It travels surprisingly well through mud.",
                  "Found on every continent except Antarctica, which probably hasn't thought of it yet."),
    "smelt":     ("A single smelt spawning run can involve millions of fish crowding into one stream.",
                  "Eulachon smelt of the Pacific Northwest were so oily they burned as candles."),
    "snail":     ("Freshwater snails are intermediate hosts for parasites that cause swimmer's itch.",
                  "You caught a snail with a fishing rod. This has never happened to anyone else here."),
    "shore_hopper": ("Shore hoppers use polarized skylight to navigate back to the water's edge.",
                     "Their population size is a standard metric for tidal pool biodiversity assessments."),
    "mudskipper":   ("Mudskippers can climb mangrove roots using their fins. Not quickly, but determinedly.",
                     "Their burrow acts as a humid retreat and incubation chamber, guarded by the male."),
    "sludge_fish":  ("It appears to have lost the ability to photosynthesize. It never had this ability.",
                     "No two sewer systems produce the same variant. Local evolution is unusually rapid."),
    "blenny":       ("Some blenny species mimic venomous fish to avoid predation -- an elaborate lie.",
                     "Their courtship involves the male performing acrobatic jumps from rock to rock."),
    "goby":         ("Mudskippers are technically gobies. Every mudskipper is a goby.",
                     "The smallest vertebrate on Earth is a goby species -- 7.9 mm fully grown."),
    "cave_blind":   ("Their immune system has relaxed so completely underground that surface exposure is risky.",
                     "They live 3 to 5 times longer than their surface relatives due to low metabolism."),
    # ---- Uncommon ----
    "bass":      ("Largemouth bass have been introduced to over 50 countries. They dominate wherever they land.",
                  "Studies show bass remember specific lure shapes after being caught once and avoid them."),
    "trout":     ("Steelhead are rainbow trout that migrate to the ocean and return to freshwater to spawn.",
                  "They can detect magnetic north and use it as a compass during long upstream migrations."),
    "catfish":   ("Wels catfish in Europe have been observed beaching themselves to grab pigeons from shore.",
                  "The Mekong giant catfish can exceed 600 pounds -- the largest freshwater fish on record."),
    "mackerel":  ("Mackerel flesh deteriorates within hours of death. Maximum freshness or nothing.",
                  "Atlantic mackerel migrate hundreds of miles between their summer and winter ranges."),
    "herring":   ("Herrings produce synchronized bubbles from their swim bladders -- a form of communication.",
                  "Wars were fought over herring rights in medieval Europe. Several. Actual wars."),
    "koi":       ("Koi can be trained to eat from a hand within weeks of patient practice.",
                  "A champion koi sold in Japan for $2.2 million USD in 2018."),
    "snapper":   ("Red snapper hover precisely over the reef by oscillating their pectoral fins.",
                  "Their red coloration comes from carotenoids in the crustaceans they eat."),
    "parrotfish": ("A school of parrotfish sleeps communally wrapped in a shared mucus cloud.",
                   "Large terminal-phase parrotfish are so colorful they're classified as separate species."),
    "walleye":   ("Their glassy eyes -- which give them the name -- contain a reflective tapetum layer.",
                  "The walleye's lateral line detects water vibrations with extraordinary precision."),
    "snakehead": ("Snakehead fish have been found in storm drains miles from the nearest water.",
                  "They use their pectoral fins to walk short distances overland, especially after rain."),
    "muckeel":   ("Its slime coat contains antibacterial compounds not found in any surface species.",
                  "It appears entirely absent from freshwater records before the industrial era."),
    "anemonefish": ("Clownfish make rapid clicks to claim territory -- a sound unique to their species.",
                    "They've been observed cultivating algae patches near their anemone for nutrition."),
    "wrasse":    ("Cleaner wrasse are one of the few non-human animals to pass the mirror self-recognition test.",
                  "They provide a genuine public health service to the reef, reducing parasite load overall."),
    "seahorse":  ("Seahorse pregnancies can produce 100 to 1,000 live young in a single birth.",
                  "They have no stomach -- food passes through so fast they must eat almost constantly."),
    "pipefish":  ("Male pipefish selectively reabsorb embryos if the female partner is deemed unattractive.",
                  "Their rigid body means they cannot curl -- they are terrible at left turns."),
    "pipe_goby": ("Its eggs are anchored to sewer walls with a specialized adhesive mucus.",
                  "Population estimates are complicated by the difficulty of entering its preferred habitat."),
    "archerfish": ("Young archerfish learn precision shooting by watching adults -- it's a taught skill.",
                   "They can track a target's trajectory mid-flight and adjust their aim in real time."),
    "thunderskate": ("Thunderskates lay rectangular egg cases with tendril anchors -- called mermaid's purses.",
                     "Their electroreceptive field is sensitive enough to detect the Earth's magnetic field."),
    "lanternfish":  ("Their daily migration is so large it appears on naval sonar as a moving false seafloor.",
                     "Sperm whales dive to eat them. One whale can consume millions of lanternfish per day."),
    "crystal_tetra": ("When stressed, their refractive scales scatter light into chaotic disorienting patterns.",
                      "Their synchronized schooling is studied in robotics for swarm algorithm development."),
    # ---- Rare ----
    "salmon":    ("Atlantic salmon -- unlike Pacific -- can survive spawning and return to spawn again.",
                  "A salmon leaping a waterfall can clear 11 feet of vertical height in one jump."),
    "pike":      ("Pike use electroreceptors to detect the galvanic field of fish in murky zero-vis water.",
                  "A pike will ambush prey larger than itself if it judges the attack angle favorable."),
    "swordfish": ("Swordfish dive to 2,000 feet in pursuit of prey, tolerating dramatic pressure changes.",
                  "Young swordfish have scales and teeth that disappear entirely as they mature."),
    "eel":       ("European eels spawn in the Sargasso Sea -- a journey of over 5,000 miles each way.",
                  "Eel blood is mildly toxic to humans. Cooking denatures the toxin. Eat it cooked."),
    "lobster":   ("Lobsters navigate home from miles away using chemical gradients in the water.",
                  "A defeated lobster avoids fighting the winner again -- they track individual rivals."),
    "barracuda": ("Barracuda carry ciguatera toxins accumulated from their prey. Rare, but nasty.",
                  "They've been documented following larger predators to steal prey at the last second."),
    "sturgeon":  ("Sturgeon leap completely out of the water during spawning runs. Nobody knows why.",
                  "Their skeleton is entirely cartilage -- they're technically primitive fish."),
    "mahimahi":  ("Mahi-mahi school under floating debris and weed lines -- they treat flotsam as habitat.",
                  "Their growth rate is among the fastest of any large fish. They're almost always young adults."),
    "grouper":   ("Nassau groupers follow moray eels on hunts and ambush prey the eel flushes out.",
                  "They use color change to communicate mood and dominance within their reef community."),
    "arapaima":  ("Arapaima can jump 6 feet from the water to grab fruit and small animals from branches.",
                  "Local Amazonian people used arapaima tongue bones as nail files. Dense as sandpaper."),
    "gator":     ("A sewer alligator's bite force is around 2,000 psi. The pipes didn't help it escape.",
                  "They keep the pipe goby and sewer eel populations in check. Something has to."),
    "bowfin":    ("Bowfin are relics of the Jurassic. Dinosaurs ate them. Dinosaurs are gone.",
                  "Their larvae use a sticky head organ to anchor to substrate before learning to swim."),
    "lionfish":  ("Lionfish hunt in groups, herding fish into crevices using their spread fins as walls.",
                  "They're reportedly delicious. Eating them is how divers fight back."),
    "tarpon":    ("A tarpon's scales can reach 3 inches across and are used as jewelry in coastal cultures.",
                  "They've been recorded rolling at the surface 100 times per hour in low-oxygen water."),
    "ghost_eel": ("It appears to spawn in open water with no substrate anchor -- eggs float unmoored.",
                  "No known natural predators have been identified. The abyss, apparently, respects it."),
    "halibut":   ("Juvenile halibut swim upright until one eye migrates. Then they settle flat forever.",
                  "Halibut have been recorded diving to 3,600 feet to follow seasonal prey migrations."),
    "lingcod":   ("Lingcod can change color to match their surroundings in seconds.",
                  "A large female may be attended by multiple smaller males competing for her eggs."),
    "payara":    ("The payara's fang socket evolved independently from those of other fanged fish.",
                  "In captivity, payara refuse dead food entirely. They only strike moving prey."),
    "viperfish": ("At depth, viperfish maintain absolute stillness for hours, then explode into a strike.",
                  "Their dorsal spine lure pulses in slow rhythms -- researchers think it mimics heartbeat."),
    "moonfish":  ("Moonfish have been found with deep-pressure fish in their stomachs despite living mid-water.",
                  "Their warm-blooded circulation system evolved independently -- not inherited from ancestors."),
    "lavafish":  ("At the surface, lavafish cool so rapidly they suffer thermal shock in under a minute.",
                  "They produce a natural antifreeze-like compound that works in the opposite direction."),
    "obsidian_eel": ("The glass-like skin is amorphous solid, not mineral crystal -- unique in biology.",
                     "When it moves, the volcanic glass flexes without shattering. Nothing else does this."),
    "gem_pike":  ("In darkness, gem pikes appear to glow. They don't -- they amplify ambient light.",
                  "Their crystal coating grows so thick over decades the fish inside is almost incidental."),
    "dragonfish": ("The red light they emit is invisible to almost all other deep-sea creatures.",
                   "Some dragonfish have retinas sensitive to their own red light -- a unique coevolution."),
    "lunar_cod": ("No two lunar cod have been found in the same location twice. They never aggregate.",
                  "Their liver accumulates tidal data as measurable chemical concentrations."),
    "stormfish": ("Stormfish schools form geometric patterns during lightning events -- purpose unknown.",
                  "They've been found at sea during Category 5 hurricanes. Moving toward the eye."),
    "sea_firefly": ("Their bioluminescent luciferin is found nowhere else in the marine world.",
                    "One frightened sea firefly can trigger a chain reaction that lights up an entire bay."),
    # ---- Epic ----
    "marlin":    ("A marlin at speed leaves a sound in the water. Hydrophones describe it as 'a door closing'.",
                  "Blue marlin travel at depth, surfacing only to feed -- their bill cuts water like a scalpel."),
    "tuna":      ("A bluefin tuna's heart beats 10 times per second during high-speed pursuit.",
                  "Tuna school by size, not species. A bluefin school often contains other tuna species."),
    "octopus":   ("Octopus dream -- their chromatophores pulse in visible patterns during sleep.",
                  "They have a protein called reflectin that lets them tune their skin's refractive index."),
    "manta":     ("Manta rays leap fully out of the water -- possibly to remove parasites, possibly for fun.",
                  "Their cephalic fins funnel plankton into their mouths during filter-feeding."),
    "shark":     ("The Greenland shark lives over 400 years, reaching sexual maturity at about 150.",
                  "Their sixth sense -- the ampullae of Lorenzini -- detects the electrical field of heartbeats."),
    "sailfish":  ("A sailfish's raised dorsal fin may act as a sail in strong wind, conserving energy.",
                  "Their skin has microscopic ridges that reduce drag -- a design copied for competition swimsuits."),
    "char":      ("Arctic char in isolated lakes can be so genetically distinct they're arguably separate species.",
                  "They're among the northernmost freshwater fish on Earth, degrees below the Arctic circle."),
    "squid":     ("Giant squid communicate via bioluminescent patterns displayed across their mantle.",
                  "Their blood is blue -- it uses copper-based hemocyanin instead of hemoglobin."),
    "coelacanth": ("Their fins contain rudimentary limb bones -- a living record of where evolution was heading.",
                   "Coelacanth meat is full of oils and compounds that make it almost inedible to humans."),
    "anglerfish": ("The symbiotic bacteria in the lure cannot survive outside the fish.",
                   "In some anglerfish species, a female can host up to 8 fused males simultaneously."),
    "hammerhead": ("Hammerheads have the widest electroreceptive field of any known shark.",
                   "They visit cleaning stations on reefs and queue patiently like any other reef fish."),
    "alligator_gar": ("Gar scales are impervious to most hooks -- they evolved to repel prehistoric predators.",
                      "Their gas bladder absorbs oxygen from gulped air, functioning as a primitive lung."),
    "crystal_ray": ("When threatened, it releases its stored electrical charge in a single full-body pulse.",
                    "Its crystalline body captures and reemits nearby bioluminescence as a passive lure."),
    "beluga_sturgeon": ("The Caspian Sea's beluga population declined 90% in the 20th century from overfishing.",
                        "A beluga sturgeon's rostrum is packed with electroreceptors for locating prey under sediment."),
    "pelican_eel": ("Its luminous tail lure is chemically distinct from the rest of its bioluminescence.",
                    "At rest, its inflated stomach can make it positively buoyant despite living at crushing depth."),
    "tempest_shark": ("Their electroreceptors are tuned to the specific frequency generated by nearby lightning.",
                      "They hunt what the lightning stuns. They are the second strike."),
    "oarfish":   ("Oarfish can voluntarily shed sections of their tail to escape predators, like a lizard.",
                  "Their pelvic fin ends in a paddle used for sensing low-frequency vibrations."),
    "treasure_fish": ("Its descendants colonize any sufficiently abandoned wreck within a decade.",
                      "Raised in a tank, treasure fish decorate their enclosure with shiny objects. Pure instinct."),
    "phoenix_fish": ("The plasma regeneration requires a minimum ambient temperature of 40 Celsius.",
                     "When calm, its body temperature drops to ambient. It is still quite warm."),
    "starfish_god": ("Each of its seven limbs maintains a separate circadian rhythm. It never fully sleeps.",
                     "Attempts to measure its intelligence have been abandoned on grounds of unfairness."),
    "leviathan_calf": ("Its mother, if present, would be too large to fit in the same body of water as the player.",
                       "It does not appear distressed. This may be the most alarming thing about it."),
    "voidfish":  ("The darkness around it is not the absence of light. It is something else.",
                  "Instruments nearby it break. Instruments measuring it disagree on the answer."),
    # ---- Legendary ----
    "kraken":    ("The colossal squid -- larger than the giant squid -- may be the true kraken of history.",
                  "In 1857, an 18-inch-circumference piece of tentacle washed ashore in the Bahamas."),
    "leviathan": ("Thomas Hobbes named his treatise on power 'Leviathan' -- a thing that cannot be beaten.",
                  "It surfaces so rarely that its documentation in mythology postdates other sea monsters."),
    "goldie":    ("Repeated catches have logged the goldie in entirely contradictory locations simultaneously.",
                  "Folk tales say the golden fish grants wishes. This one is in your tackle box. Ask it."),
    "discoin":   ("Its heartbeat transmits a weak Bluetooth signal. The protocol is not standard.",
                  "It is the only fish that could theoretically be fined for securities violations."),
    "serpent":   ("The ice formed by its passage contains crystalline structures not seen in natural frost.",
                  "Nothing eats it. Even very large things have tried. It ends the same way."),
    "guardian":  ("The inscriptions in the sunken temple describe a fish that guards the gate. This is it.",
                  "It has been in this temple longer than the temple has been underwater."),
    "phoenix":   ("Fishermen who catch a reef phoenix report a smell of warm stone and burned salt.",
                  "The flash of its rebirth is sometimes mistaken for lightning by ships far overhead."),
    "ancient":   ("Its scales contain chemical signatures from rivers that no longer exist.",
                  "It has been documented, lost, rediscovered, and disputed more times than astronomy allows."),
    "celestial_carp": ("The minerals in its bones cannot be synthesized on Earth.",
                       "It navigates using starfall angles calibrated to a different solar system's sky."),
    "void_leviathan": ("It registers on no instrument built after a certain date. Older instruments see it clearly.",
                       "Being near it produces a specific kind of quiet that has no scientific description."),
    "moon_kraken": ("Historical accounts of the moon kraken use past, present, and future tense simultaneously.",
                    "It is not afraid of you. It is not aware of you. These may be the same thing."),
    "world_serpent_spawn": ("Its scales contain compressed star-stuff from the void that predates the solar system.",
                            "This is not the serpent that encircles the world. That one is larger. Much larger."),
    "ouroboros_eel": ("Time near it behaves strangely. You have already caught it. You haven't caught it yet.",
                      "The loop it completes erases its own beginning. It was always already here."),
    "the_first_fish": ("It is simultaneously caught and uncaught. Your inventory holds both states.",
                       "Theologians and physicists have been arguing about this fish for different reasons."),
    "prism_guardian": ("The cavern it inhabits did not exist before the prism guardian entered it.",
                       "Every culture that developed near a cave system has a creation myth featuring this fish."),
    "maelstrom_eel": ("The eel doesn't generate the storm as a byproduct. The storm is the point.",
                      "Deep seafarers learned: if the eel is already in the trap, leave the trap. Leave the boat."),
    # ---- Crabs ----
    "bluecrab":  ("The mating ritual involves the male carrying the female for several days before spawning.",
                  "They detect salinity changes of as little as 0.1 parts per thousand."),
    "mudcrab":   ("They can regrow a lost claw after molting -- smaller at first, then full size after more molts.",
                  "Mud crabs are farmed commercially across Southeast Asia, a major aquaculture crop."),
    "snowcrab":  ("Snow crabs walk up to 100 miles across the seafloor during migration.",
                  "Their legs are the raw material for surimi -- the processed crab used in imitation crab sticks."),
    "cococrab":  ("Coconut crabs smell decomposing matter from a kilometer away using their antenna.",
                  "They hoard shiny objects in their burrows. Purpose unclear. Very unclear."),
    "spidercrab": ("Japanese spider crabs gather in large molting aggregations for mutual protection.",
                   "Their lifespan -- over 100 years -- makes them among the longest-lived crustaceans."),
    "kingcrab":  ("King crabs are often found hosting dozens of hitchhiker organisms on their shells.",
                  "Their 'pod' migration moves mass populations with no single leader coordinating them."),
    "voidcrab":  ("Every attempt to name it formally has resulted in the taxonomist's notes going missing.",
                  "The committee has met 14 times. Each meeting reached the same disagreement."),
    "horseshoecrab": ("Horseshoe crabs have 9 eyes, including two sensitive to UV moonlight.",
                      "LAL -- from their blood -- is used to test every IV drug and medical device for contamination."),
    "ghostcrab": ("Ghost crabs stridulate by rubbing body parts together, producing a warning growl.",
                  "They dig burrows 4 feet deep in sand, using them for sleep and breeding."),
    "mantiscrab": ("Mantis shrimp see 16 types of color -- but their color discrimination is paradoxically poor.",
                   "Some species live with the same partner in the same burrow for over 20 years."),
    "crystalcrab": ("The crystalcrab's shell is the hardest known biological material in the cavern ecosystem.",
                    "It produces a clicking sound by flexing its carapace -- rare among crustaceans."),
    "stormcrab": ("Its electromagnetic shell acts as a Faraday cage, protecting it from its own discharge.",
                  "Storm crabs have been found miles inland after hurricanes. They were not placed there."),
    "fireventcrab": ("They press themselves against vent outlets to regulate body temperature directly.",
                     "Their hemolymph recirculates heat from the claw before releasing waste warmth."),
    "moonphasecrab": ("The molt cycle encoded in their shell pattern can be read backward to determine age.",
                      "Fishers have used moonphasecrab behavior as a tide and weather predictor for centuries."),
    "nebulacrab": ("Each specimen carries mineral deposits from a different celestial body.",
                   "They have never been observed reproducing. New ones appear. This is not explained."),
}

# === FISH_END ===

# === RODS_START ===
# ============================================================================
# Rod catalog
# ============================================================================
# Rods are stored as a single integer column (user_fishing.rod_tier);
# the catalog metadata below is purely the lookup the cog uses to
# render and price each tier. tier 0 (twig rod) is everyone's free
# starter so the player can ,fish from minute one.

RODS: Final[dict[int, dict]] = {
    0: {
        "key": "twig",
        "name": "Twig Rod",
        "emoji": "\U0001F33F",   # herb
        "price_reel": 0.0,
        "fish_bonus":     0.00,    # +% to the fish-bucket weight
        "rare_bonus":     0.00,    # +% to within-bucket rarity roll
        "weight_bonus":   0.00,    # +% multiplier to caught fish weight
        "sweet_window":   0.00,    # +s added to the sweet-spot window
        "max_zone_tier":  1,       # highest zone tier this rod can fish in
        "blurb":          "A literal stick. Better than nothing.",
    },
    1: {
        "key": "bamboo",
        "name": "Bamboo Rod",
        "emoji": "\U0001F38B",
        "price_reel": 750.0,
        "fish_bonus":     0.10,
        "rare_bonus":     0.05,
        "weight_bonus":   0.05,
        "sweet_window":   0.10,
        "max_zone_tier":  2,
        "blurb":          "Springy and forgiving. The honest entry rod.",
    },
    2: {
        "key": "fiberglass",
        "name": "Fiberglass Rod",
        "emoji": "\U0001F3A3",
        "price_reel": 7_500.0,
        "fish_bonus":     0.18,
        "rare_bonus":     0.12,
        "weight_bonus":   0.12,
        "sweet_window":   0.20,
        "max_zone_tier":  3,
        "blurb":          "Light, sturdy, and unlocks the big-water zones.",
    },
    3: {
        "key": "carbon",
        "name": "Carbon Composite Rod",
        "emoji": "\U0001F38B",
        "price_reel": 60_000.0,
        "fish_bonus":     0.25,
        "rare_bonus":     0.22,
        "weight_bonus":   0.20,
        "sweet_window":   0.30,
        "max_zone_tier":  4,
        "blurb":          "Pro-grade graphite. Pulls trophies out of deep water.",
    },
    4: {
        "key": "golden",
        "name": "Golden Rod",
        "emoji": "\U0001F947",
        "price_reel": 500_000.0,
        "fish_bonus":     0.32,
        "rare_bonus":     0.34,
        "weight_bonus":   0.30,
        "sweet_window":   0.40,
        "max_zone_tier":  5,
        "blurb":          "Solid 24k. Possibly cursed. Definitely effective.",
    },
    5: {
        "key": "abyssal",
        "name": "Abyssal Rod",
        "emoji": "\U0001F30A",
        "price_reel": 5_000_000.0,
        "fish_bonus":     0.40,
        "rare_bonus":     0.50,
        "weight_bonus":   0.45,
        "sweet_window":   0.55,
        "max_zone_tier":  6,
        "blurb":          "Hums faintly. Has opinions. Owns the abyss.",
    },
    6: {
        "key": "moonlit",
        "name": "Moonlit Rod",
        "emoji": "\U0001F319",
        "price_reel": 25_000_000.0,
        "fish_bonus":     0.48,
        "rare_bonus":     0.62,
        "weight_bonus":   0.55,
        "sweet_window":   0.70,
        "max_zone_tier":  7,
        "blurb":          "Glows soft silver. Calls fish out of moonlit water.",
    },
    7: {
        "key": "leviathan",
        "name": "Leviathan Rod",
        "emoji": "\U0001F40B",
        "price_reel": 120_000_000.0,
        "fish_bonus":     0.58,
        "rare_bonus":     0.78,
        "weight_bonus":   0.70,
        "sweet_window":   0.85,
        "max_zone_tier":  8,
        "blurb":          "Carved from a leviathan rib. Pulls things that bite back.",
    },
    8: {
        "key": "celestial",
        "name": "Celestial Rod",
        "emoji": "\U00002728",
        "price_reel": 600_000_000.0,
        "fish_bonus":     0.70,
        "rare_bonus":     0.95,
        "weight_bonus":   0.85,
        "sweet_window":   1.00,
        "max_zone_tier":  9,
        "blurb":          "Catches starlight. Catches anything else with the same ease.",
    },
    9: {
        "key": "godlike",
        "name": "Godlike Rod",
        "emoji": "\U0001F451",
        "price_reel": 3_000_000_000.0,
        "fish_bonus":     0.85,
        "rare_bonus":     1.20,
        "weight_bonus":   1.05,
        "sweet_window":   1.20,
        "max_zone_tier":  10,
        "blurb":          "The endgame. The rod even the abyss respects.",
    },
}

# Sell value when downgrading rods. Currently disabled (rods are
# permanent upgrades), but kept here for future "trade in" mechanic.
ROD_SELL_REFUND_FRAC: Final[float] = 0.0
# === RODS_END ===

# === BAIT_START ===
# ============================================================================
# Bait catalog
# ============================================================================
# Bait is consumed one-per-cast when equipped. Each cast deducts one
# from the player's chosen bait stack (user_fishing.bait_inventory
# JSONB) and skips deduction when the stack is empty so the player
# falls back to a no-bait cast.
#
# Stack semantics:
#   buy:    ,fish buy <bait_key> <qty>
#   equip:  ,fish bait <bait_key>
#   unequip: ,fish bait none
# The equipped bait key is on user_fishing.equipped_bait.

BAIT: Final[dict[str, dict]] = {
    "worm": {
        "name":         "Earth Worm",
        "emoji":        "\U0001FAB1",   # worm
        "price_reel":    1.0,
        "max_stack":    500,
        "fish_bonus":   0.05,    # +% to fish bucket weight
        "rare_bonus":   0.02,
        "bonus_bonus":  0.00,
        "blurb":        "Cheap, dependable, slightly sad.",
    },
    "shrimp": {
        "name":         "Live Shrimp",
        "emoji":        "\U0001F990",
        "price_reel":    3.0,
        "max_stack":    250,
        "fish_bonus":   0.10,
        "rare_bonus":   0.06,
        "bonus_bonus":  0.01,
        "blurb":        "Saltwater fish go nuts for them.",
    },
    "minnow": {
        "name":         "Bait Minnow",
        "emoji":        "\U0001F41F",
        "price_reel":    6.0,
        "max_stack":    150,
        "fish_bonus":   0.12,
        "rare_bonus":   0.10,
        "bonus_bonus":  0.02,
        "blurb":        "A small fish. To catch a bigger fish.",
    },
    "neon": {
        "name":         "Neon Lure",
        "emoji":        "\U0001F4A1",
        "price_reel":    25.0,
        "max_stack":    100,
        "fish_bonus":   0.15,
        "rare_bonus":   0.18,
        "bonus_bonus":  0.05,
        "blurb":        "Glows in low light. Attracts curious things.",
    },
    "magic": {
        "name":         "Magic Lure",
        "emoji":        "\U00002728",
        "price_reel":    150.0,
        "max_stack":    50,
        "fish_bonus":   0.20,
        "rare_bonus":   0.30,
        "bonus_bonus":  0.10,
        "blurb":        "Smells faintly of mythology. Pulls rare fish out of thin water.",
    },
    "chum": {
        "name":         "Abyssal Chum",
        "emoji":        "\U0001F9EC",
        "price_reel":    800.0,
        "max_stack":    25,
        "fish_bonus":   0.25,
        "rare_bonus":   0.45,
        "bonus_bonus":  0.18,
        "blurb":        "Wakes up the things that sleep at the bottom.",
    },
}
# === BAIT_END ===

# === CRAB_TRAPS_START ===
# ============================================================================
# Crab trap catalog
# ============================================================================
# Crab traps are deployable items that passively catch crabs while the
# player is offline. Buying a trap burns REEL out of the player's wallet
# (same _apply_gear_spend_burn_effect path as rod and bait spends), so
# every purchase moves the REEL oracle / chart / LP rewards exactly like
# upgrading a rod -- the user-facing rule "everything affects the chart
# of whatever it uses" stays consistent across the cog.
#
# Lifecycle (matches services/fishing.py):
#   1. ,fish trap buy <key> <qty>     -- adds undeployed traps to the
#                                        crab_trap_inventory JSONB column.
#                                        Burns REEL -> chart impact.
#   2. ,fish trap place <key> [qty]   -- moves N traps from the inventory
#                                        into placed_crab_traps JSONB. Each
#                                        trap records its zone + placed_at.
#   3. wait soak_seconds...           -- DB-side clock, no Python now().
#   4. ,fish trap collect             -- pays out LURE for every soaked
#                                        trap (see ``trap_yield`` below) and
#                                        rolls a random crab species per
#                                        trap into fish_inventory. Single-use:
#                                        the trap is consumed on collect.
#
# Slippage / impact:
#   * Trap PURCHASE = REEL burn -> REEL oracle drops (gear-spend impact).
#   * Trap COLLECTION = LURE wallet credit (mints LURE supply); handled
#     identically to a fish sale, no oracle bump (fishing already runs that
#     way -- LURE earned from any source flows through update_wallet_holding
#     without an extra oracle write).
#
# Cap on placed traps. Stops a whale from carpet-bombing a zone with
# 1000 traps and farming the JSONB.

CRAB_TRAP_PLACED_CAP: Final[int] = 8

# Cool-down between collect calls (DB-side). 30s is enough to space out
# spam clicks without making the loop feel slow.
CRAB_TRAP_COLLECT_COOLDOWN_S: Final[int] = 30

# Trap catalog. ``soak_seconds`` is how long the trap needs to sit in
# the water before it is "ready" to haul. ``base_yield_lure`` is the
# pre-zone-multiplier LURE bag of crabs the trap pays out; the actual
# payout rolls in [0.7, 1.4] of base * zone payout multiplier. Each
# trap also rolls a crab species (see ``roll_crab``); higher-tier traps
# bias toward rarer crabs.
#
# Each trap has:
#   key                  -- internal id (matches inventory keys)
#   name, emoji          -- display
#   price_reel           -- REEL per trap (chart-burning purchase price)
#   max_stack            -- max undeployed traps in inventory
#   soak_seconds         -- wait time before "ready"
#   base_yield_lure      -- LURE haul midpoint (pre-zone-mult, pre-roll)
#   crab_rarity_bias     -- per-tier shift up the rarity ladder for the
#                           rolled crab species (0 = no shift, 4 = always
#                           legendary if available)
#   max_zone_tier        -- highest zone tier this trap can be placed in
#   blurb

CRAB_TRAPS: Final[dict[str, dict]] = {
    # Crab traps are now permanent gear (rod-priced, never consumed).
    # Each purchase grants ONE trap that goes back into ``crab_trap_inventory``
    # whenever it's collected -- ``services/fishing.py::collect_crab_traps``
    # returns the trap to inventory instead of dropping it. Players still
    # cap how many they can place at once via ``CRAB_TRAP_PLACED_CAP``.
    #
    # ``max_stack`` is now an arbitrarily large hold cap (so you can own
    # 100+ wire pots if you really want to flood the bayou) but the
    # economic gate is the per-trap REEL price, which is rod-tier
    # expensive. ``max_zone_tier`` raised so deeper zones get traps too.
    "wire": {
        "key":              "wire",
        "name":             "Wire Pot",
        "emoji":            "\U0001F578",
        "price_reel":        750.0,            # rod-tier 1 price
        "max_stack":         500,              # effectively unlimited
        "soak_seconds":     15 * 60,
        "base_yield_lure":   90.0,
        "crab_rarity_bias":  0,
        "max_zone_tier":     2,
        "blurb":             "Bent chicken wire and zip ties. Catches the basics. Permanent.",
    },
    "oak": {
        "key":              "oak",
        "name":             "Oak Pot",
        "emoji":            "\U0001FAA3",
        "price_reel":        7_500.0,          # rod-tier 2 price
        "max_stack":         500,
        "soak_seconds":     30 * 60,
        "base_yield_lure":   320.0,
        "crab_rarity_bias":  1,
        "max_zone_tier":     3,
        "blurb":             "Old-school slatted oak. Patient fishermen swear by it. Permanent.",
    },
    "steel": {
        "key":              "steel",
        "name":             "Steel Pot",
        "emoji":            "\U0001F9F2",
        "price_reel":        60_000.0,         # rod-tier 3 price
        "max_stack":         500,
        "soak_seconds":     60 * 60,
        "base_yield_lure":   1_400.0,
        "crab_rarity_bias":  2,
        "max_zone_tier":     5,
        "blurb":             "Welded rebar cage with a one-way funnel. Indestructible. Permanent.",
    },
    "abyssal": {
        "key":              "abyssal",
        "name":             "Abyssal Pot",
        "emoji":            "\U0001F573",
        "price_reel":        500_000.0,        # rod-tier 4 price
        "max_stack":         500,
        "soak_seconds":     2 * 60 * 60,
        "base_yield_lure":   8_000.0,
        "crab_rarity_bias":  3,
        "max_zone_tier":     6,
        "blurb":             "Hums when no one is looking. Pulls up things that hum back. Permanent.",
    },
    "moonlit": {
        "key":              "moonlit",
        "name":             "Moonlit Pot",
        "emoji":            "\U0001F319",
        "price_reel":        5_000_000.0,      # rod-tier 5 price
        "max_stack":         500,
        "soak_seconds":     3 * 60 * 60,
        "base_yield_lure":   45_000.0,
        "crab_rarity_bias":  4,
        "max_zone_tier":     7,
        "blurb":             "Glows with cold light. Crabs walk willingly inside. Permanent.",
    },
    "leviathan": {
        "key":              "leviathan",
        "name":             "Leviathan Pot",
        "emoji":            "\U0001F40B",
        "price_reel":        25_000_000.0,     # rod-tier 6 price
        "max_stack":         500,
        "soak_seconds":     6 * 60 * 60,
        "base_yield_lure":   200_000.0,
        "crab_rarity_bias":  5,
        "max_zone_tier":     8,
        "blurb":             "Carved from a leviathan rib bone. Big enough to be a small house. Permanent.",
    },
    "celestial": {
        "key":              "celestial",
        "name":             "Celestial Pot",
        "emoji":            "\U00002728",
        "price_reel":        120_000_000.0,    # rod-tier 7 price
        "max_stack":         500,
        "soak_seconds":     12 * 60 * 60,
        "base_yield_lure":   1_000_000.0,
        "crab_rarity_bias":  6,
        "max_zone_tier":     9,
        "blurb":             "Catches its own starlight. Catches anything else, too. Permanent.",
    },
    "godlike": {
        "key":              "godlike",
        "name":             "Godlike Pot",
        "emoji":            "\U0001F451",
        "price_reel":        600_000_000.0,    # rod-tier 8 price
        "max_stack":         500,
        "soak_seconds":     24 * 60 * 60,
        "base_yield_lure":   5_000_000.0,
        "crab_rarity_bias":  7,
        "max_zone_tier":     10,
        "blurb":             "The endgame trap. Closes the loop. Permanent.",
    },
}

# Yield jitter: actual LURE haul = base * zone.payout_mult * uniform(LO, HI)
CRAB_TRAP_YIELD_JITTER: Final[tuple[float, float]] = (0.7, 1.4)

# How many crab specimens a trap rolls into fish_inventory on collect.
# Higher-tier traps roll more crabs.
CRAB_TRAP_CRABS_PER_COLLECT: Final[dict[str, int]] = {
    "wire":      1,
    "oak":       2,
    "steel":     3,
    "abyssal":   4,
    "moonlit":   5,
    "leviathan": 7,
    "celestial": 9,
    "godlike":   12,
}
# === CRAB_TRAPS_END ===

# === ZONES_START ===
# ============================================================================
# Zone catalog
# ============================================================================
# Zones gate which fish can be hooked and which rod tiers can fish
# them. They also apply a flat payout multiplier so the deeper zones
# pay better even on the same species.
#
# rod_tier requirement gates *entry*: a player can't switch into the
# zone with a weaker rod. Once inside, a fish below the rod tier still
# rolls -- the rod is the floor, not the ceiling.
#
# Seasonal zones exist (e.g. "frozen_pond" during a Winter season) and
# are toggled live by services/fishing.py via season metadata. The
# baseline zones below are always available.

ZONES: Final[dict[str, dict]] = {
    "pond": {
        "name":         "Local Pond",
        "emoji":        "\U0001F33F",
        "tier":         1,
        "min_rod_tier": 0,
        "payout_mult":  1.00,
        "junk_bonus":   0.00,
        "rare_bonus":   0.00,
        "blurb":        "Calm, shallow, full of carp.",
    },
    "lake": {
        "name":         "Mountain Lake",
        "emoji":        "\U0001F3D4",
        "tier":         2,
        "min_rod_tier": 1,
        "payout_mult":  1.10,
        "junk_bonus":  -0.05,
        "rare_bonus":   0.04,
        "blurb":        "Cold water, big bass, postcard views.",
    },
    "river": {
        "name":         "River Run",
        "emoji":        "\U0001F30A",
        "tier":         2,
        "min_rod_tier": 1,
        "payout_mult":  1.10,
        "junk_bonus":  -0.05,
        "rare_bonus":   0.06,
        "blurb":        "Fast water. Fast fish. Trout heaven.",
    },
    "ocean": {
        "name":         "Open Ocean",
        "emoji":        "\U0001F30A",
        "tier":         3,
        "min_rod_tier": 2,
        "payout_mult":  1.25,
        "junk_bonus":  -0.10,
        "rare_bonus":   0.10,
        "blurb":        "Deep blue. Marlin, tuna, the occasional shark.",
    },
    "dock": {
        "name":         "Discoin Dock",
        "emoji":        "\U0001FA99",
        "tier":         3,
        "min_rod_tier": 2,
        "payout_mult":  1.30,
        "junk_bonus":  -0.05,
        "rare_bonus":   0.12,
        "blurb":        "The bot's home pier. Pulls the rare Discoin Fish.",
    },
    "abyss": {
        "name":         "The Abyss",
        "emoji":        "\U0001F300",
        "tier":         5,
        "min_rod_tier": 3,
        "payout_mult":  1.50,
        "junk_bonus":  -0.15,
        "rare_bonus":   0.18,
        "blurb":        "Things move down here. Nothing should.",
    },
    # ---- Expanded zones ----
    "swamp": {
        "name":         "Bayou Swamp",
        "emoji":        "\U0001FAB7",   # lotus (close enough to swampy vibes)
        "tier":         1,
        "min_rod_tier": 0,
        "payout_mult":  1.05,
        "junk_bonus":   0.05,    # extra junk -- swamps are gross
        "rare_bonus":   0.02,
        "blurb":        "Knee-deep mud, gators, and the smell of regret.",
    },
    "sewer": {
        "name":         "City Sewer",
        "emoji":        "\U0001F573",   # hole
        "tier":         1,
        "min_rod_tier": 0,
        "payout_mult":  1.10,    # slightly better payout to offset junk
        "junk_bonus":   0.10,    # MOSTLY junk down here
        "rare_bonus":   0.00,
        "blurb":        "Reeks. But the sewer alligators pay nicely.",
    },
    "reef": {
        "name":         "Coral Reef",
        "emoji":        "\U0001FAB8",   # coral
        "tier":         3,
        "min_rod_tier": 2,
        "payout_mult":  1.28,
        "junk_bonus":  -0.10,
        "rare_bonus":   0.10,
        "blurb":        "Technicolor fish dart through living architecture.",
    },
    "kelp": {
        "name":         "Kelp Forest",
        "emoji":        "\U0001F33F",
        "tier":         4,
        "min_rod_tier": 3,
        "payout_mult":  1.36,
        "junk_bonus":  -0.10,
        "rare_bonus":   0.14,
        "blurb":        "An underwater jungle. Big things hide in the canopy.",
    },
    "glacier": {
        "name":         "Glacier Bay",
        "emoji":        "\U0001F9CA",
        "tier":         4,
        "min_rod_tier": 3,
        "payout_mult":  1.40,
        "junk_bonus":  -0.12,
        "rare_bonus":   0.13,
        "blurb":        "Cold cuts the line. Cold fish pay double.",
    },
    "temple": {
        "name":         "Sunken Temple",
        "emoji":        "\U0001F3DB",   # classical building
        "tier":         5,
        "min_rod_tier": 4,
        "payout_mult":  1.55,
        "junk_bonus":  -0.18,
        "rare_bonus":   0.22,
        "blurb":        "Ancient stone, ancient fish, ancient curses.",
    },
    "trench": {
        "name":         "Mariana Trench",
        "emoji":        "\U0001F30A",
        "tier":         6,
        "min_rod_tier": 5,
        "payout_mult":  1.75,
        "junk_bonus":  -0.20,
        "rare_bonus":   0.28,
        "blurb":        "Pressure crushes most things. The fish here didn't get the memo.",
    },
    "moonpool": {
        "name":         "Moon Pool",
        "emoji":        "\U0001F319",
        "tier":         7,
        "min_rod_tier": 6,
        "payout_mult":  2.00,
        "junk_bonus":  -0.22,
        "rare_bonus":   0.34,
        "blurb":        "Glows when the surface is dark. Rare runs at moonrise.",
    },
    "magma": {
        "name":         "Magma Vents",
        "emoji":        "\U0001F30B",
        "tier":         7,
        "min_rod_tier": 6,
        "payout_mult":  2.10,
        "junk_bonus":  -0.18,
        "rare_bonus":   0.32,
        "blurb":        "Boiling water. The fish are mad and on fire.",
    },
    "void": {
        "name":         "Voidwater",
        "emoji":        "\U0001F300",
        "tier":         8,
        "min_rod_tier": 7,
        "payout_mult":  2.50,
        "junk_bonus":  -0.25,
        "rare_bonus":   0.42,
        "blurb":        "Black water with the wrong number of dimensions.",
    },
    "nebula": {
        "name":         "Nebula Falls",
        "emoji":        "\U0001F30C",
        "tier":         9,
        "min_rod_tier": 8,
        "payout_mult":  3.00,
        "junk_bonus":  -0.30,
        "rare_bonus":   0.55,
        "blurb":        "Liquid starlight pours from the ceiling.",
    },
    "ouroboros": {
        "name":         "The Ouroboros",
        "emoji":        "\U0001F40D",
        "tier":         10,
        "min_rod_tier": 9,
        "payout_mult":  4.00,
        "junk_bonus":  -0.40,
        "rare_bonus":   0.75,
        "blurb":        "The world-snake's eye. Casts here close the loop.",
    },

    # ---- New zones: unique mechanics and challenges ----
    "tidal_pool": {
        "name":         "Tidal Pool",
        "emoji":        "\U0001F30A",
        "tier":         1,
        "min_rod_tier": 0,
        "payout_mult":  1.03,
        "junk_bonus":  -0.05,    # clear water -- very little junk
        "rare_bonus":   0.01,
        "blurb":        "Shallow pools left by the tide. Clear water, tiny species, zero danger.",
    },
    "mangrove": {
        "name":         "Mangrove Thicket",
        "emoji":        "\U0001F334",
        "tier":         2,
        "min_rod_tier": 1,
        "payout_mult":  1.15,
        "junk_bonus":   0.03,    # tangled roots trap extra debris
        "rare_bonus":   0.08,    # unusual species shelter in the roots
        "blurb":        "Tangled mangrove roots hide unusual species. Expect some debris.",
    },
    "shipwreck": {
        "name":         "Sunken Galleon",
        "emoji":        "\U0001F6A2",
        "tier":         3,
        "min_rod_tier": 2,
        "payout_mult":  1.35,
        "junk_bonus":   0.02,    # old wreck sheds debris -- slight junk bump
        "rare_bonus":   0.16,    # the hold shelters rare catches
        "blurb":        "A sunken galleon. The hold is guarded by rare catches -- and plenty of old junk.",
    },
    "bioluminescent_bay": {
        "name":         "Bioluminescent Bay",
        "emoji":        "\U0001F31F",
        "tier":         4,
        "min_rod_tier": 3,
        "payout_mult":  1.45,
        "junk_bonus":  -0.15,    # the glow draws only live things -- near junk-free
        "rare_bonus":   0.18,
        "blurb":        "Water that glows on its own. The light calls rare fish in from the deep.",
    },
    "crystal_caverns": {
        "name":         "Crystal Caverns",
        "emoji":        "\U0001F52E",
        "tier":         5,
        "min_rod_tier": 4,
        "payout_mult":  1.60,
        "junk_bonus":  -0.20,    # underground lake -- no surface trash ever reaches here
        "rare_bonus":   0.25,
        "blurb":        "A subterranean lake in a crystal cathedral. Zero junk. Unearthly species.",
    },
    "storm_surge": {
        "name":         "Storm Surge",
        "emoji":        "\U000026A1",
        "tier":         6,
        "min_rod_tier": 5,
        "payout_mult":  1.80,
        "junk_bonus":   0.05,    # maelstrom churns up surface debris
        "rare_bonus":   0.30,    # chaos drives rare fish to the surface
        "blurb":        "A surface maelstrom. Extra junk from the churn -- offset by the highest rare bonus at this tier.",
    },
}

# Default starting zone for any new fisher.
DEFAULT_ZONE: Final[str] = "pond"
# === ZONES_END ===

# === HELPERS_START ===
# ============================================================================
# Helper functions
# ============================================================================
# Pure-Python helpers consumed by both services/fishing.py and
# cogs/fishing.py. Anything that needs DB access lives in the service
# layer; this module stays import-cheap and side-effect-free.

def rod_meta(tier: int) -> dict:
    """Return the rod metadata dict for ``tier``, clamped to a valid range."""
    if tier in RODS:
        return RODS[tier]
    if tier < 0:
        return RODS[0]
    return RODS[max(RODS.keys())]


def zone_meta(zone: str) -> dict:
    """Return the zone metadata dict, falling back to the default zone."""
    return ZONES.get(zone) or ZONES[DEFAULT_ZONE]


def fish_meta(fish_key: str) -> dict | None:
    """Return the fish entry by key, or ``None`` for an unknown key."""
    return FISH.get(fish_key)


def junk_meta(junk_key: str) -> dict | None:
    """Return the junk entry by key, or ``None`` for an unknown key."""
    return JUNK.get(junk_key)


def fish_fact(fish_key: str) -> str:
    """Return a random fact for a fish species, or '' if none is defined."""
    key = str(fish_key or "")
    facts = (*FISH_FACTS.get(key, ()), *FISH_FACTS_EXTRA.get(key, ()))
    return random.choice(facts) if facts else ""


def bait_meta(bait_key: str | None) -> dict | None:
    """Return the bait entry by key, or ``None`` if no/unknown bait."""
    if not bait_key:
        return None
    return BAIT.get(bait_key)


def rarity_meta(rarity: str) -> dict:
    """Return the rarity meta dict; unknown rarities map to common."""
    return RARITY_META.get(rarity, RARITY_META["common"])


def fish_in_zone(zone: str) -> list[str]:
    """Return all fish keys legal in ``zone``."""
    return [k for k, v in FISH.items() if zone in v.get("zones", ())]


def roll_fish(zone: str, rod_tier: int) -> str | None:
    """Pick a fish key for the given zone + rod tier.

    Two-stage roll:
      1. Pick a rarity bucket using ``RARITY_WEIGHTS``.
      2. Filter eligible fish (zone + rod-tier floor) and pick one
         uniformly inside the bucket.

    Falls through to the next-lower bucket if the first roll has no
    candidates so the caller never gets ``None`` from a misconfigured
    catalog. Returns ``None`` only if literally no fish matches the
    zone (shouldn't happen with the default catalog).
    """
    buckets = ["legendary", "epic", "rare", "uncommon", "common"]
    weights = [RARITY_WEIGHTS[b] for b in buckets]
    chosen = random.choices(buckets, weights=weights, k=1)[0]
    # Fall-through: if the chosen bucket is empty for this zone/tier,
    # walk down toward common until we find a candidate.
    order = buckets[buckets.index(chosen):] + ["common"]
    for tier_label in order:
        candidates = [
            k for k, v in FISH.items()
            if v["rarity"] == tier_label
            and zone in v["zones"]
            and v["min_rod_tier"] <= rod_tier
        ]
        if candidates:
            return random.choice(candidates)
    return None


def roll_junk() -> str:
    """Pick a junk key (uniform over ``JUNK_WEIGHTS``)."""
    keys = list(JUNK_WEIGHTS.keys())
    weights = list(JUNK_WEIGHTS.values())
    return random.choices(keys, weights=weights, k=1)[0]


def roll_outcome(rod_tier: int, bait_key: str | None, zone: str) -> str:
    """Roll the top-level outcome bucket.

    Returns one of: ``"junk"``, ``"fish"``, ``"bonus"``.
    Rod fish_bonus shifts mass from junk -> fish; bait bonus_bonus
    shifts mass from junk -> bonus; zone junk_bonus is signed (deep
    zones reduce junk weight directly).
    """
    rod = rod_meta(rod_tier)
    bait = bait_meta(bait_key) or {}
    zone_data = zone_meta(zone)

    base = dict(BASE_OUTCOME_WEIGHTS)
    fish_bonus = float(rod.get("fish_bonus", 0.0)) + float(bait.get("fish_bonus", 0.0))
    bonus_bonus = float(bait.get("bonus_bonus", 0.0))
    junk_pen = float(zone_data.get("junk_bonus", 0.0))   # negative shrinks junk

    # Convert percentage shifts into integer weight movements relative
    # to the junk pool so the totals stay roughly conserved.
    fish_shift = int(round(base["junk"] * fish_bonus))
    bonus_shift = int(round(base["junk"] * bonus_bonus))
    junk_shift = int(round(base["junk"] * junk_pen))

    base["junk"]  = max(1, base["junk"]  - fish_shift - bonus_shift + junk_shift)
    base["fish"]  = base["fish"]  + fish_shift
    base["bonus"] = base["bonus"] + bonus_shift

    keys = list(base.keys())
    weights = [base[k] for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


def roll_bonus_subtype() -> str:
    """Pick which kind of bonus dropped: money_bag, mystery_box, buddy_egg."""
    keys = list(BONUS_SUB_WEIGHTS.keys())
    weights = list(BONUS_SUB_WEIGHTS.values())
    return random.choices(keys, weights=weights, k=1)[0]


def wild_battle_chance(zone_tier: int) -> float:
    """Per-cast chance of hooking a wild buddy in the given zone."""
    extra = max(0, int(zone_tier) - 1) * WILD_BATTLE_ZONE_BONUS_PER_TIER
    return min(WILD_BATTLE_MAX_CHANCE, WILD_BATTLE_BASE_CHANCE + extra)


def roll_wild_battle(
    zone_tier: int, rod_tier: int, zone: str | None = None,
) -> dict:
    """Roll a synthesised wild-buddy opponent.

    Returns a dict matching the cc_buddies row shape that
    ``services.buddy_battle.Fighter.from_row`` accepts. Mood stats are
    pinned at 100 so wild buddies always fight at peak; level + rarity
    scale with zone / rod tier. ``zone`` (e.g. ``"abyss"``) selects the
    species pool via :func:`wild_buddy_species_pool` so a player fishing
    in the swamp hooks swamp-themed buddies, not generic deep-sea ones.
    """
    pool = wild_buddy_species_pool(zone) if zone else FISHING_BUDDY_SPECIES
    species = random.choice(pool)
    base_level = max(1, int(zone_tier) * WILD_BATTLE_LEVEL_PER_ZONE_TIER)
    level = max(1, base_level + random.randint(-WILD_BATTLE_LEVEL_JITTER, WILD_BATTLE_LEVEL_JITTER))

    # Rarity: 1..5, with a per-rod-tier bias upward. Roll a random tier
    # then nudge it up by floor(rod_tier * bias) capped at 5.
    base_tier = random.choices((1, 2, 3, 4, 5), weights=(50, 25, 15, 7, 3), k=1)[0]
    bias = int(rod_tier * WILD_BATTLE_RARITY_PER_ROD_TIER)
    rarity_tier = max(1, min(5, base_tier + bias))

    return {
        "id": 0,                       # 0 = wild / synthesised, never persisted
        "owner_user_id": 0,            # 0 signals PvE to the engine
        "species": species,
        "name": species.title(),       # cog can override with flavor names
        "rarity_tier": rarity_tier,
        "level": level,
        "hunger":    100,
        "happiness": 100,
        "energy":    100,
        # Wild buddies have no allocations; engine reads these as zero.
        "hp_alloc":  0,
        "atk_alloc": 0,
        "spd_alloc": 0,
    }


def wild_battle_lure_reward(zone_tier: int) -> float:
    """LURE prize for winning a wild-buddy battle. Scales with zone depth."""
    base = random.uniform(WILD_BATTLE_WIN_LURE_MIN, WILD_BATTLE_WIN_LURE_MAX)
    multiplier = 1.0 + max(0, int(zone_tier) - 1) * (WILD_BATTLE_WIN_LURE_PER_ZONE_TIER - 1.0)
    return round(base * multiplier, 2)


def wild_battle_reel_reward(zone_tier: int) -> float:
    """REEL kicker for winning a wild-buddy battle. Scales with zone depth.

    Mirrors ``wild_battle_lure_reward`` but with a softer per-tier
    multiplier so deep-water REEL hauls don't drown the swap-and-stake
    path. Returns 0.0 when min/max are misconfigured to <= 0 so a future
    designer can disable the kicker without touching call sites.
    """
    if WILD_BATTLE_WIN_REEL_MAX <= 0:
        return 0.0
    base = random.uniform(WILD_BATTLE_WIN_REEL_MIN, WILD_BATTLE_WIN_REEL_MAX)
    multiplier = 1.0 + max(0, int(zone_tier) - 1) * (WILD_BATTLE_WIN_REEL_PER_ZONE_TIER - 1.0)
    return round(base * multiplier, 2)


def roll_weight(fish_key: str, rod_tier: int, quality_mult: float) -> float:
    """Roll the realised weight for ``fish_key`` given the rod and quality.

    The rod's weight_bonus and a per-cast quality multiplier
    (sweet-spot vs late hook) compound into the size roll. The result
    is clamped between min_lbs and max_lbs * 2 so a perfect hook on a
    legendary rod can produce an over-cap "monster" record.
    """
    spec = FISH.get(fish_key)
    if not spec:
        return 0.0
    rod = rod_meta(rod_tier)
    base = random.uniform(float(spec["min_lbs"]), float(spec["max_lbs"]))
    weight = base * (1.0 + float(rod.get("weight_bonus", 0.0))) * float(quality_mult)
    return max(float(spec["min_lbs"]) * 0.5, min(float(spec["max_lbs"]) * 2.0, weight))


def fish_payout(fish_key: str, weight_lbs: float, *,
                combo_mult: float, quality_mult: float, zone: str) -> float:
    """Return the LURE paid for selling a fresh fish.

    The same formula is used by services/fishing.py at sell-time so
    the value the user sees on catch matches what they get when they
    cash out. Sell-time may also apply a small staleness discount
    after N hours but the catalog stays simple.
    """
    spec = FISH.get(fish_key)
    if not spec:
        return 0.0
    rarity_mult = float(rarity_meta(spec["rarity"]).get("sell_mult", 1.0))
    zone_mult = float(zone_meta(zone).get("payout_mult", 1.0))
    raw = float(spec["base_lure"]) * float(weight_lbs) * combo_mult * quality_mult \
        * rarity_mult * zone_mult
    return round(max(0.0, raw), 2)


def fish_xp(fish_key: str) -> int:
    """XP awarded for catching ``fish_key``. Junk and bonuses give 0."""
    spec = FISH.get(fish_key)
    if not spec:
        return 0
    return int(FISH_XP_BY_RARITY.get(spec["rarity"], 0))


def level_from_xp(xp: int) -> int:
    """Inverse of an arithmetic series: solves for level given total XP.

    Same shape as buddies_config.level_from_xp so the two systems feel
    consistent. The constant ``FISH_XP_CURVE`` is tuned for ~30 hours
    of casual fishing to hit max level.
    """
    import math
    if xp <= 0:
        return 1
    raw = (1 + math.sqrt(1 + 8 * xp / FISH_XP_CURVE)) / 2
    lvl = int(math.floor(raw))
    return max(1, min(FISH_MAX_LEVEL, lvl))


def xp_to_next(xp: int) -> tuple[int, int]:
    """Return (xp_into_level, xp_for_next_level_total).

    Used by the panel's progress bar. When at MAX_LEVEL the second
    value is 0 so the caller can render a flat bar.
    """
    lvl = level_from_xp(xp)
    if lvl >= FISH_MAX_LEVEL:
        return (xp, 0)
    floor_xp = (lvl - 1) * lvl // 2 * FISH_XP_CURVE
    next_xp  = lvl * (lvl + 1) // 2 * FISH_XP_CURVE
    return (max(0, xp - floor_xp), max(1, next_xp - floor_xp))


def quality_for_reaction(reaction_s: float) -> float:
    """Map reaction time (seconds) to a quality multiplier.

    Sub-sweet-spot reactions earn ``HOOK_SWEET_BONUS``. Anything past
    the full hook window earns the late penalty. The curve is a
    simple step rather than a polynomial so users get clear feedback:
    "fast = bonus, slow = penalty".
    """
    if reaction_s <= HOOK_SWEET_S:
        return HOOK_SWEET_BONUS
    if reaction_s <= HOOK_WINDOW_S:
        return 1.0
    return HOOK_LATE_PENALTY


# ---------------------------------------------------------------------------
# Dynamic hook window (per-cast scaling)
# ---------------------------------------------------------------------------
# Pre-fix: every cast had a flat 3.0s window regardless of gear, level,
# zone, or what was biting. Now the window scales:
#   - Rod tier widens it (better rod = more time)
#   - Player level widens it
#   - Zone tier tightens it (deeper zones are chaotic)
#   - Random jitter +- 15% so streaks don't feel mechanical
# Rarity-based scaling (legendary = much tighter) lives in the cog where
# the bite outcome can optionally be pre-rolled; this helper handles
# the four pre-bite inputs only.

# Per-input multipliers. Tuned so a fresh L1 player on a tier-0 rod in
# zone tier 1 gets ~3s; a maxed L20 player on a tier-5 rod in zone 1
# gets ~5s; the same L20 in zone tier 5 gets ~3.5s.
_HOOK_ROD_MULT_PER_TIER:    Final[float] = 0.06   # +6% per rod tier
_HOOK_LEVEL_MULT_PER_LEVEL: Final[float] = 0.015  # +1.5% per fishing level
_HOOK_ZONE_MULT_PER_TIER:   Final[float] = -0.08  # -8% per zone tier above 1
_HOOK_JITTER:               Final[float] = 0.15   # +- 15% random
_HOOK_FLOOR_S:              Final[float] = 1.2    # never below 1.2s
_HOOK_CEIL_S:               Final[float] = 6.0    # never above 6.0s


def compute_hook_window(
    rod_tier: int,
    fish_level: int,
    zone_tier: int,
    rarity_tier: int = 1,
    *,
    rng: "random.Random | None" = None,
) -> float:
    """Per-cast hook window in seconds.

    ``rarity_tier`` is the BUCKET rarity hint (1-5, where 5 is
    legendary). The cog can pass 1 when the bite outcome hasn't been
    pre-rolled yet (window depends on gear/level/zone only); pass the
    actual rarity when pre-rolling is available so legendary bites
    feel correspondingly tight.
    """
    import random as _r
    base = HOOK_WINDOW_S
    rod_m = 1.0 + max(0, int(rod_tier)) * _HOOK_ROD_MULT_PER_TIER
    lvl_m = 1.0 + max(0, int(fish_level) - 1) * _HOOK_LEVEL_MULT_PER_LEVEL
    zone_m = 1.0 + max(0, int(zone_tier) - 1) * _HOOK_ZONE_MULT_PER_TIER
    # Rarity tightens the window non-linearly: T1 = 1.0x, T5 = 0.5x.
    rt = max(1, min(5, int(rarity_tier or 1)))
    rarity_m = 1.0 - (rt - 1) * 0.125
    rng = rng or _r
    jitter = 1.0 + (rng.random() * 2.0 - 1.0) * _HOOK_JITTER
    raw = base * rod_m * lvl_m * zone_m * rarity_m * jitter
    return max(_HOOK_FLOOR_S, min(_HOOK_CEIL_S, raw))


# ---------------------------------------------------------------------------
# Secondary action ("REEL" / "PULL")
# ---------------------------------------------------------------------------
# Sometimes mid-bite the player has to perform an extra action. Hits
# the immersion of a real fight; rewards focused players with double
# bonus when they nail BOTH the sweet hook AND the secondary.
#
# Trigger semantics (cog-side):
#   - SECONDARY_TRIGGER_CHANCE per cast determines if the secondary
#     prompt fires after the bite frame.
#   - If it fires and the player misses the secondary -> CATCH FAILS.
#   - If it fires and the player hits + sweet hook -> 2.0x bonus.
#   - If it fires and the player hits, no sweet hook -> 1.5x bonus
#     (matches the existing sweet bonus level).
#   - If it doesn't fire -> behaviour unchanged from the legacy flow.

SECONDARY_TRIGGER_CHANCE: Final[float] = 0.25  # 25% per cast
SECONDARY_WINDOW_S:       Final[float] = 2.0   # tight react window
SECONDARY_DOUBLE_BONUS:   Final[float] = 2.0   # sweet + secondary multiplier
SECONDARY_SOLO_BONUS:     Final[float] = 1.5   # secondary only (= legacy sweet)


def level_payout_mult(level: int) -> float:
    """Per-level payout boost. Level 1 = 1.0x, level 50 = ~1.49x."""
    return 1.0 + max(0, level - 1) * FISH_LEVEL_PAYOUT_PER_LEVEL


# ----------------------------------------------------------------------------
# Crab trap helpers
# ----------------------------------------------------------------------------

def crab_trap_meta(key: str) -> dict | None:
    """Return the crab trap metadata dict, or None for an unknown key."""
    return CRAB_TRAPS.get(key)


# All FISH entries with min_rod_tier == 99 are crab species: catchable
# only via traps. Pre-compute the keys so roll_crab() doesn't iterate
# the catalog every call.
CRAB_KEYS: Final[tuple[str, ...]] = tuple(
    k for k, v in FISH.items() if int(v.get("min_rod_tier") or 0) >= 99
)


def crab_in_zone(zone: str) -> list[str]:
    """Return all crab species keys legal in ``zone``."""
    return [
        k for k in CRAB_KEYS
        if zone in FISH[k].get("zones", ())
    ]


def roll_crab(zone: str, trap_key: str) -> str | None:
    """Pick a crab species key for a trap pulled from ``zone``.

    Two-stage roll mirrors ``roll_fish``: pick a rarity bucket using
    RARITY_WEIGHTS shifted by the trap's ``crab_rarity_bias``, then
    pick a uniform crab inside that bucket. Falls down toward common
    if the chosen bucket has no candidates so the caller never gets
    None for a misconfigured zone (returns None only when literally no
    crab species lives in ``zone``).
    """
    trap = CRAB_TRAPS.get(trap_key) or {}
    bias = int(trap.get("crab_rarity_bias") or 0)
    buckets = ["legendary", "epic", "rare", "uncommon", "common"]
    # Bias slides probability mass UP the rarity ladder by `bias` slots.
    # Achieved by zeroing out the first `5 - bias` lowest rarity weights
    # so the higher tiers dominate the pick.
    weights = [RARITY_WEIGHTS[b] for b in buckets]
    if bias > 0:
        # Inflate the top-(bias+1) weights by 4x each so high-tier traps
        # actually feel different without abandoning the fall-through
        # path for low-zone-tier deployments (those zones won't HAVE the
        # rarer crabs and the loop below walks back down).
        for i in range(min(bias + 1, len(weights))):
            weights[i] *= 4
    chosen = random.choices(buckets, weights=weights, k=1)[0]
    # Walk DOWN from the chosen bucket toward common first (preserves
    # the "rarity-or-lower" intent of the roll). If the zone has no
    # candidates at the chosen tier or below (e.g. abyss only stocks
    # epic+ crabs), fall back to walking UP toward legendary so the
    # haul is never empty in zones that DO have crabs.
    chosen_idx = buckets.index(chosen)
    order = buckets[chosen_idx:] + list(reversed(buckets[:chosen_idx]))
    for tier_label in order:
        candidates = [
            k for k in CRAB_KEYS
            if FISH[k]["rarity"] == tier_label
            and zone in FISH[k]["zones"]
        ]
        if candidates:
            return random.choice(candidates)
    return None


# ----------------------------------------------------------------------------
# Cast-animation hint pools
# ----------------------------------------------------------------------------
# Each frame in the cast animation pulls a random hint string from a
# pool below.  Two casts never feel identical because every frame
# (cast / fly / wait / nibble / tug / bite / reel) re-rolls its hint.
# Adding a new line is config-only -- no cog change needed.
#
# Keep entries short enough to fit cleanly under the ASCII frame
# inside a Discord embed description (the frame block is the bulk of
# the visual; the hint is one trailing line).

HINT_POOLS: Final[dict[str, tuple[str, ...]]] = {
    "cast": (
        "Winding up the throw...",
        "Steadying the rod...",
        "Picking your spot...",
        "Eyeing the water...",
        "Cocking back...",
    ),
    "fly_1": (
        "The line whistles through the air...",
        "Bait sails over the water...",
        "Long cast -- looking smooth...",
        "Arc of the line catches the light...",
    ),
    "fly_2": (
        "*splash*",
        "Plop! Bait lands.",
        "*sploosh* -- settling in...",
        "*ker-plunk*",
    ),
    "wait_1": (
        "The float bobs gently.",
        "Quiet water.",
        "Patience...",
        "The line drifts.",
        "Nothing yet.",
    ),
    "wait_2": (
        "Bobber drifting...",
        "Slight ripple.",
        "Water still calm.",
        "Wind picks up.",
        "Float dips, then settles.",
    ),
    "wait_3": (
        "Something stirring underneath?",
        "A bubble breaks the surface.",
        "Reflections shift.",
        "The bobber tilts.",
    ),
    "peek": (
        "A shadow slides past...",
        "Something just looked at the bait.",
        "*flash* -- scales in the depth.",
        "The water darkens for a moment.",
    ),
    "false_alarm": (
        "Just driftwood. Calm down.",
        "*shrug* -- nothing.",
        "False alarm. Keep waiting.",
        "Caught on a weed. Tug it free.",
    ),
    "nibble": (
        "Something is sniffing!",
        "*tap tap* -- was that something?",
        "The bobber dipped!",
        "Tiny pull on the line...",
    ),
    "tug": (
        "LINE GOING TIGHT!",
        "Whatever it is, it's BIG!",
        "Hold on tight...",
        "Rod's bending HARD!",
    ),
    "bite": (
        "GO GO GO!",
        "STRIKE NOW!",
        "HOOK IT!",
        "PULL!",
        "*RIP* -- HOOK NOW!",
    ),
    "reel_1": (
        "Reeling in...",
        "Pulling steadily...",
        "Coming up slow...",
        "Cranking the handle...",
    ),
    "reel_2": (
        "Almost there...",
        "Coming up...",
        "One last yank...",
        "Surface in sight...",
    ),
    "reel_heavy": (
        "It's PULLING BACK!",
        "Rod is BENDING!",
        "Line creaking...",
        "Heavy heavy heavy...",
    ),
    "reel_light": (
        "Easy pull...",
        "Smooth reel...",
        "No fight at all...",
        "Light as anything...",
    ),
    "reel_jump": (
        "IT JUMPED!",
        "*LEAP!* over the surface!",
        "Airborne!",
        "Tail-flick mid-air!",
    ),
    "splash_in": (
        "BIG SPLASH!",
        "Surface explodes!",
        "*KSPLASH!* it broke water!",
        "Gotcha!",
    ),
}


def random_hint(frame_key: str) -> str:
    """Pick a random hint string for ``frame_key`` from ``HINT_POOLS``.

    Returns an empty string for unknown keys so the caller can append
    it unconditionally without a None check.
    """
    pool = HINT_POOLS.get(frame_key)
    if not pool:
        return ""
    return random.choice(pool)


def roll_treasure_loot() -> str:
    """Pick a treasure outcome key from ``TREASURE_LOOT_WEIGHTS``.

    Pure helper -- no DB. Returns one of: 'lure_small', 'lure_medium',
    'lure_large', 'reel_kicker', 'rare_bait', 'trap_cache', 'wild_egg',
    'ancient_relic'. The service layer fans out from this key into the
    actual amount/qty roll (see TREASURE_PAYOUT) and the wallet credit.
    """
    keys = list(TREASURE_LOOT_WEIGHTS.keys())
    weights = list(TREASURE_LOOT_WEIGHTS.values())
    return random.choices(keys, weights=weights, k=1)[0]


def roll_treasure_jackpot_fish() -> str | None:
    """Pick a uniformly random legendary fish key for the dig jackpot.

    Crabs (which live in the FISH catalog with min_rod_tier=99) are
    excluded -- the jackpot should feel like an "ancient relic", not a
    bonus crab haul. Returns ``None`` only when no legendary fish keys
    qualify (catalog misconfiguration; defensive guard).
    """
    pool = [
        k for k, v in FISH.items()
        if v.get("rarity") == TREASURE_JACKPOT_POOL_RARITY
        and int(v.get("min_rod_tier") or 0) < 99
    ]
    if not pool:
        return None
    return random.choice(pool)


def egg_sell_lure(rarity_tier: int) -> float:
    """LURE paid when a player sells a held egg at ``rarity_tier``.

    Falls back to the common-tier price for any tier outside the table
    so a future rarity ladder bump doesn't accidentally drop a row to
    zero. Returns 0.0 only when the table is empty (which never happens
    in practice; a defensive guard for unit tests / mocks).
    """
    if not EGG_SELL_LURE_BY_TIER:
        return 0.0
    fallback = EGG_SELL_LURE_BY_TIER.get(1, 0.0)
    return float(EGG_SELL_LURE_BY_TIER.get(int(rarity_tier), fallback))


def trap_yield_lure(trap_key: str, zone: str) -> float:
    """Roll the LURE haul for a single trap pulled from ``zone``.

    Pulls from the trap's ``base_yield_lure``, scaled by the zone's
    ``payout_mult`` and a uniform jitter from ``CRAB_TRAP_YIELD_JITTER``
    so two collects of the same trap in the same zone never feel
    identical. Returns 0.0 for an unknown trap key (caller's problem).
    """
    trap = CRAB_TRAPS.get(trap_key)
    if not trap:
        return 0.0
    base = float(trap.get("base_yield_lure") or 0.0)
    z = ZONES.get(zone) or ZONES[DEFAULT_ZONE]
    mult = float(z.get("payout_mult") or 1.0)
    lo, hi = CRAB_TRAP_YIELD_JITTER
    jitter = random.uniform(lo, hi)
    return round(max(0.0, base * mult * jitter), 2)


# ============================================================================
#  EXPANSION -- sea monsters, rod augments, zone-locked legendaries,
#               depth + current modifiers, weekly tournament constants
# ============================================================================

# Sea monsters. Spawn in their min-rod tier zones (and weather-gated for some).
# A monster encounter replaces a normal pull and triggers a dedicated boss
# fight view (see ``cogs.fishing._SeaMonsterFightView``).

MONSTERS: Final[dict[str, dict]] = {
    "kraken_spawn": {
        "key": "kraken_spawn", "name": "Kraken Spawn", "emoji": "\U0001F419",
        "tier": 6, "min_zone_tier": 6,
        "hp": 220, "stamina_cost": 14,
        "lure_reward_min": 4_000, "lure_reward_max": 9_000,
        "reel_reward_min": 1_500, "reel_reward_max": 3_200,
        "augment_frag_chance": 0.40,
        "ascii": (
            "  _________   ",
            " /  o   o  \\  ",
            "<  \\_____/  > ",
            " \\ |||||| /   ",
            "  ~~~~~~~~~   ",
        ),
        "blurb": "Eight arms, eight ways to ruin a rod.",
    },
    "reef_wyrm": {
        "key": "reef_wyrm", "name": "Reef Wyrm", "emoji": "\U0001F409",
        "tier": 6, "min_zone_tier": 5,
        "hp": 180, "stamina_cost": 12,
        "lure_reward_min": 3_000, "lure_reward_max": 7_000,
        "reel_reward_min": 1_200, "reel_reward_max": 2_600,
        "augment_frag_chance": 0.35,
        "ascii": (
            "    ____      ",
            "  /     '--<  ",
            " (  o.o     ) ",
            "  \\______==/  ",
            "  ~~~~~~~~~   ",
        ),
        "blurb": "Coiled in the coral. Strikes like a whip.",
    },
    "storm_eel": {
        "key": "storm_eel", "name": "Storm Eel", "emoji": "\U000026A1",
        "tier": 7, "min_zone_tier": 6,
        "hp": 260, "stamina_cost": 16,
        "lure_reward_min": 6_000, "lure_reward_max": 14_000,
        "reel_reward_min": 2_500, "reel_reward_max": 5_500,
        "augment_frag_chance": 0.45,
        "ascii": (
            "  >  ~~~~ ~~~  ",
            " /  o========  ",
            " \\______..__/  ",
            "  ~~~~~~~~~~~  ",
            "               ",
        ),
        "blurb": "Crackles with charge. Reel dry or it shocks back.",
    },
    "sunken_king": {
        "key": "sunken_king", "name": "Sunken King", "emoji": "\U0001F451",
        "tier": 7, "min_zone_tier": 7,
        "hp": 360, "stamina_cost": 18,
        "lure_reward_min": 12_000, "lure_reward_max": 26_000,
        "reel_reward_min": 5_000, "reel_reward_max": 11_000,
        "augment_frag_chance": 0.55,
        "ascii": (
            "    /^^^\\     ",
            "  /| o o |\\   ",
            "  | =====  |  ",
            "   \\_____/    ",
            "    ~ ~ ~     ",
        ),
        "blurb": "Drowned, crowned, and unimpressed by your line.",
    },
    "magma_maw": {
        "key": "magma_maw", "name": "Magma Maw", "emoji": "\U0001F525",
        "tier": 8, "min_zone_tier": 7,
        "hp": 500, "stamina_cost": 22,
        "lure_reward_min": 25_000, "lure_reward_max": 55_000,
        "reel_reward_min": 12_000, "reel_reward_max": 24_000,
        "augment_frag_chance": 0.65,
        "ascii": (
            "  ___       ",
            " /vvv\\=#    ",
            "<( O  )>   ",
            " \\====/    ",
            " ~ ~ ~ ~   ",
        ),
        "blurb": "A shark with a furnace where its belly should be.",
    },
    "void_lure": {
        "key": "void_lure", "name": "Void Lure", "emoji": "\U0001F311",
        "tier": 9, "min_zone_tier": 8,
        "hp": 720, "stamina_cost": 26,
        "lure_reward_min": 55_000, "lure_reward_max": 120_000,
        "reel_reward_min": 25_000, "reel_reward_max": 60_000,
        "augment_frag_chance": 0.80,
        "ascii": (
            "    .  *  .    ",
            "   .  ( ) *    ",
            "  *  (   ) .   ",
            "   .  ( )  *   ",
            "    *  .  *    ",
        ),
        "blurb": "Glows like bait you would never bring up.",
    },
    "ouroboros_hatchling": {
        "key": "ouroboros_hatchling", "name": "Ouroboros Hatchling",
        "emoji": "\U0001F40D",
        "tier": 10, "min_zone_tier": 9,
        "hp": 1_100, "stamina_cost": 32,
        "lure_reward_min": 150_000, "lure_reward_max": 320_000,
        "reel_reward_min": 65_000, "reel_reward_max": 140_000,
        "augment_frag_chance": 0.95,
        "ascii": (
            "   .--..--.    ",
            "  /  o    o\\   ",
            " (  ~~~~~~  )  ",
            "  \\___==___/   ",
            "   tail bites  ",
        ),
        "blurb": "Already swallowing its own tail. You're optional.",
    },
}


def monster_meta(key: str) -> dict | None:
    if not key:
        return None
    return MONSTERS.get(str(key))


MONSTER_BASE_CHANCE:         Final[float] = 0.015     # ~1.5% per cast at tier 6+
MONSTER_DEPTH_BONUS_PER_TIER: Final[float] = 0.004    # +0.4%/zone tier
MONSTER_MAX_CHANCE:          Final[float] = 0.08


def monster_spawn_chance(zone_tier: int, *, weather: str | None = None) -> float:
    """Per-cast monster encounter chance.

    Returns 0 for zones below tier 6 -- monsters live deep. The base
    chance bumps with zone tier, and certain "spawn weather" doubles it.
    """
    if int(zone_tier or 0) < 6:
        return 0.0
    base = MONSTER_BASE_CHANCE + max(0, int(zone_tier) - 6) * MONSTER_DEPTH_BONUS_PER_TIER
    if (weather or "").lower() in ("storm", "blood_tide", "void_storm"):
        base *= 2.0
    return min(MONSTER_MAX_CHANCE, base)


def roll_monster_for_zone(
    zone: str, *, rod_tier: int, weather: str | None = None,
    rng: random.Random | None = None,
) -> str | None:
    """Roll a monster key matching the zone tier + rod tier constraints.

    Returns None if no monster passes the spawn check. Filters out
    monsters whose ``min_zone_tier`` exceeds the zone or whose tier
    exceeds the rod's catch capacity by more than 2.
    """
    rng = rng or random
    z = ZONES.get(zone) or {}
    zt = int(z.get("tier") or 1)
    chance = monster_spawn_chance(zt, weather=weather)
    if chance <= 0 or rng.random() >= chance:
        return None
    pool = [
        m for m in MONSTERS.values()
        if int(m.get("min_zone_tier") or 0) <= zt
        and int(m.get("tier") or 0) <= int(rod_tier) + 2
    ]
    if not pool:
        return None
    return str(rng.choice(pool).get("key"))


# Rod augments. Three categories slot independently. Each augment is a
# single owned-or-not item on user_fishing.augments JSONB.

AUGMENTS: Final[dict[str, dict]] = {
    # Lines -- break resistance + snap-line risk reduction.
    "line_silk":    {"kind": "line", "tier": 1, "name": "Silk Line",
                      "price_reel": 5_000.0, "snap_resist": 0.05,
                      "blurb": "Smooth. Polite. Holds steady."},
    "line_braided": {"kind": "line", "tier": 2, "name": "Braided Line",
                      "price_reel": 50_000.0, "snap_resist": 0.10,
                      "blurb": "Multi-strand. Takes a yank."},
    "line_steel":   {"kind": "line", "tier": 3, "name": "Steel Leader",
                      "price_reel": 400_000.0, "snap_resist": 0.18,
                      "blurb": "A real shark stopper."},
    "line_chained": {"kind": "line", "tier": 4, "name": "Chained Line",
                      "price_reel": 4_000_000.0, "snap_resist": 0.30,
                      "blurb": "Doesn't snap. Period."},
    "line_void":    {"kind": "line", "tier": 5, "name": "Void Filament",
                      "price_reel": 30_000_000.0, "snap_resist": 0.45,
                      "blurb": "Threaded with starless dark."},
    # Lures -- rare bias on the catch roll.
    "lure_feather": {"kind": "lure", "tier": 1, "name": "Feather Lure",
                      "price_reel": 6_000.0, "rare_bias": 0.04,
                      "blurb": "Pretty. Pretends to be a bug."},
    "lure_spinner": {"kind": "lure", "tier": 2, "name": "Spinner Lure",
                      "price_reel": 60_000.0, "rare_bias": 0.08,
                      "blurb": "Catches the eye. And the fish."},
    "lure_glow":    {"kind": "lure", "tier": 3, "name": "Glow Lure",
                      "price_reel": 500_000.0, "rare_bias": 0.14,
                      "blurb": "Soft pulse. Big fish look."},
    "lure_arcane":  {"kind": "lure", "tier": 4, "name": "Arcane Lure",
                      "price_reel": 5_000_000.0, "rare_bias": 0.22,
                      "blurb": "Hums. Smells of fortune."},
    "lure_oracle":  {"kind": "lure", "tier": 5, "name": "Oracle Lure",
                      "price_reel": 40_000_000.0, "rare_bias": 0.35,
                      "blurb": "Knows what's biting before you cast."},
    # Reels -- shorter cast cycle and slight payout bump.
    "reel_oak":     {"kind": "reel", "tier": 1, "name": "Oak Reel",
                      "price_reel": 7_000.0, "cast_speed_mult": 0.95,
                      "blurb": "Solid wood. Quiet clicks."},
    "reel_bronze":  {"kind": "reel", "tier": 2, "name": "Bronze Reel",
                      "price_reel": 70_000.0, "cast_speed_mult": 0.88,
                      "blurb": "Smooth wind. Crisp release."},
    "reel_carbon":  {"kind": "reel", "tier": 3, "name": "Carbon Reel",
                      "price_reel": 600_000.0, "cast_speed_mult": 0.78,
                      "blurb": "Featherweight. Lightning fast."},
    "reel_magnet":  {"kind": "reel", "tier": 4, "name": "Magnet Reel",
                      "price_reel": 6_000_000.0, "cast_speed_mult": 0.68,
                      "blurb": "Snaps to the strike."},
    "reel_celest":  {"kind": "reel", "tier": 5, "name": "Celestial Reel",
                      "price_reel": 50_000_000.0, "cast_speed_mult": 0.55,
                      "blurb": "Casts before you decide to."},
}

AUGMENT_KINDS: Final[tuple[str, ...]] = ("line", "lure", "reel")


def augment_meta(key: str) -> dict | None:
    if not key:
        return None
    return AUGMENTS.get(str(key))


def augments_by_kind(kind: str) -> tuple[dict, ...]:
    return tuple(sorted(
        (a for a in AUGMENTS.values() if a.get("kind") == kind),
        key=lambda a: int(a.get("tier") or 0),
    ))


def active_augment(augments_inv: dict, kind: str) -> dict | None:
    """Highest-tier owned augment of the given kind."""
    inv = augments_inv or {}
    best: dict | None = None
    best_tier = -1
    for akey, owned in inv.items():
        if not owned:
            continue
        meta = AUGMENTS.get(str(akey))
        if not meta or meta.get("kind") != kind:
            continue
        t = int(meta.get("tier") or 0)
        if t > best_tier:
            best = meta
            best_tier = t
    return best


# Zone-locked legendaries. Mapping legendary fish_key -> zone_key.
# Only that single zone rolls the legendary; other zones never spawn it.

ZONE_LOCKED_LEGENDARY: Final[dict[str, str]] = {
    "moon_kraken":       "moonpool",
    "void_kraken":       "void",
    "leviathan":         "trench",
    "ancient_fish":      "temple",
    "ouroboros_serpent": "ouroboros",
}


def zone_lock_for(fish_key: str) -> str | None:
    return ZONE_LOCKED_LEGENDARY.get(str(fish_key)) if fish_key else None


def legendary_allowed_in_zone(fish_key: str, zone: str) -> bool:
    """Pre-roll gate -- legendaries with a lock only pass in their zone."""
    lock = zone_lock_for(fish_key)
    return lock is None or lock == str(zone)


# Depth + current per zone. Defaults give a flat 1.0x so older zones
# don't change behaviour until they get explicit tuning here.

ZONE_DEPTH_BAND: Final[dict[str, tuple[float, float]]] = {
    "pond":              (1.0, 1.05),
    "lake":              (1.0, 1.10),
    "river":             (1.0, 1.12),
    "ocean":             (1.05, 1.25),
    "reef":              (1.05, 1.25),
    "kelp":              (1.10, 1.35),
    "glacier":           (1.10, 1.40),
    "abyss":             (1.20, 1.65),
    "trench":            (1.25, 1.80),
    "moonpool":          (1.20, 1.55),
    "magma":             (1.20, 1.60),
    "void":              (1.30, 2.00),
    "nebula":            (1.40, 2.10),
    "ouroboros":         (1.50, 2.40),
}

ZONE_CURRENT: Final[dict[str, str]] = {
    "river":      "swift",
    "ocean":      "swift",
    "trench":     "riptide",
    "storm_surge": "riptide",
    "magma":      "riptide",
    "void":       "riptide",
    "moonpool":   "calm",
    "pond":       "calm",
    "swamp":      "calm",
    "lake":       "calm",
    "abyss":      "calm",
}


def zone_depth_factor(zone: str, rng: random.Random | None = None) -> float:
    rng = rng or random
    lo, hi = ZONE_DEPTH_BAND.get(str(zone), (1.0, 1.0))
    return float(rng.uniform(lo, hi))


def zone_current(zone: str) -> str:
    return ZONE_CURRENT.get(str(zone), "calm")


def current_modifiers(current: str) -> tuple[float, float, float]:
    """Return ``(sweet_window_mult, payout_mult, snap_risk)``.

    calm: wider sweet window, flat payout, no extra snap risk.
    swift: tighter window, +5% payout.
    riptide: tightest window, +15% payout, +5% snap-line risk.
    """
    c = str(current or "calm").lower()
    if c == "swift":
        return (0.90, 1.05, 0.0)
    if c == "riptide":
        return (0.75, 1.15, 0.05)
    return (1.30, 1.00, 0.0)


# Weekly tournaments. State + entries live in DB tables (see migration).
# These constants pace the schedule + reward pools.

TOURNAMENT_DURATION_DAYS: Final[int] = 7
TOURNAMENT_TOP_PAYOUT_RATIO: Final[tuple[float, ...]] = (
    0.40, 0.20, 0.12, 0.08, 0.05, 0.04, 0.04, 0.03, 0.02, 0.02,
)
TOURNAMENT_THEMES: Final[tuple[dict, ...]] = (
    {"key": "biggest_catch", "name": "Biggest Catch",
     "blurb": "Largest single fish by weight wins."},
    {"key": "most_legendary", "name": "Legendary Hunt",
     "blurb": "Catch the most legendaries."},
    {"key": "total_weight", "name": "Heavy Hauler",
     "blurb": "Pile up the most total pounds."},
    {"key": "variety", "name": "Variety Run",
     "blurb": "Land the most distinct species."},
)
TOURNAMENT_MIN_POOL_LURE: Final[float] = 25_000.0


def tournament_theme(idx: int) -> dict:
    return dict(TOURNAMENT_THEMES[int(idx) % len(TOURNAMENT_THEMES)])


def tournament_score(theme_key: str, catch: dict, running: dict) -> float:
    """Update + return the running score for a tournament entry.

    ``catch`` is the per-cast result dict (rarity, weight_lbs, fish_key).
    ``running`` is the entry's accumulator state. Returns the new score
    so the caller can write it back to fishing_tournament_entries.
    """
    if theme_key == "biggest_catch":
        cur = float(running.get("biggest_lbs") or 0.0)
        new = max(cur, float(catch.get("weight_lbs") or 0.0))
        running["biggest_lbs"] = new
        return new
    if theme_key == "most_legendary":
        n = int(running.get("legendary_count") or 0)
        if str(catch.get("rarity")) == "legendary":
            n += 1
        running["legendary_count"] = n
        return float(n)
    if theme_key == "total_weight":
        total = float(running.get("total_lbs") or 0.0) + float(catch.get("weight_lbs") or 0.0)
        running["total_lbs"] = total
        return total
    if theme_key == "variety":
        species = set(running.get("species_set") or [])
        species.add(str(catch.get("fish_key") or ""))
        species.discard("")
        running["species_set"] = list(species)
        return float(len(species))
    return float(running.get("score") or 0.0)


__all__ = [
    "BAIT", "BASE_OUTCOME_WEIGHTS", "BONUS_SUB_WEIGHTS",
    "BUDDY_EGG_DAILY_CAP", "CAST_COOLDOWN_S", "COMBO_IDLE_RESET_S",
    "COMBO_MAX", "COMBO_STEP",
    "CRAB_KEYS", "CRAB_TRAPS", "CRAB_TRAP_COLLECT_COOLDOWN_S",
    "CRAB_TRAP_CRABS_PER_COLLECT", "CRAB_TRAP_PLACED_CAP",
    "CRAB_TRAP_YIELD_JITTER",
    "EGG_SELL_LURE_BY_TIER",
    "TREASURE_DIG_COOLDOWN_S", "TREASURE_JACKPOT_POOL_RARITY",
    "TREASURE_LOOT_WEIGHTS", "TREASURE_PAYOUT",
    "TREASURE_RARE_BAIT_POOL", "TREASURE_TRAP_POOL",
    "BEACHCOMB_COOLDOWN_S", "BEACHCOMB_OUTCOME_WEIGHTS",
    "BEACHCOMB_PAYOUTS", "BEACHCOMB_BAIT_POOL", "BEACHCOMB_BAIT_QTY",
    "BEACHCOMB_BAIT_PICKS", "BEACHCOMB_MAP_QTY",
    "roll_beachcomb_outcome",
    "DEFAULT_ZONE", "FISH", "FISH_FACTS", "FISH_FACTS_EXTRA", "FISHING_BUDDY_SPECIES",
    "FISH_LEVEL_PAYOUT_PER_LEVEL", "FISH_MAX_LEVEL", "FISH_XP_BY_RARITY",
    "FISH_XP_CURVE", "FRAMES", "HINT_POOLS",
    "HOOK_LATE_PENALTY", "HOOK_SWEET_BONUS",
    "HOOK_SWEET_S", "HOOK_WINDOW_S", "JUNK", "JUNK_WEIGHTS",
    "LURE_NETWORK_SHORT", "LURE_STAKE_REEL_PER_DAY", "LURE_SYMBOL",
    "MAX_HELD_EGGS",
    "MONEY_BAG_MAX_LURE", "MONEY_BAG_MIN_LURE", "MYSTERY_BOX_MAX_LURE",
    "MYSTERY_BOX_MIN_LURE", "RARITY_META", "RARITY_WEIGHTS",
    "REEL_SYMBOL", "RODS",
    "ROD_SELL_REFUND_FRAC", "SESSION_TIMEOUT_S",
    "WILD_BATTLE_BASE_CHANCE", "WILD_BATTLE_CAPTURE_CHANCE",
    "WILD_BATTLE_LEVEL_JITTER", "WILD_BATTLE_LEVEL_PER_ZONE_TIER",
    "WILD_BATTLE_MAX_CHANCE", "WILD_BATTLE_PROMPT_TIMEOUT_S",
    "WILD_BATTLE_RARITY_PER_ROD_TIER",
    "WILD_BATTLE_WIN_LURE_MAX", "WILD_BATTLE_WIN_LURE_MIN",
    "WILD_BATTLE_WIN_LURE_PER_ZONE_TIER",
    "WILD_BATTLE_WIN_REEL_MAX", "WILD_BATTLE_WIN_REEL_MIN",
    "WILD_BATTLE_WIN_REEL_PER_ZONE_TIER",
    "WILD_BATTLE_ZONE_BONUS_PER_TIER",
    "WILD_BUDDY_SPECIES_BY_ZONE",
    "ZONES",
    "MONSTERS", "AUGMENTS", "AUGMENT_KINDS",
    "ZONE_LOCKED_LEGENDARY", "ZONE_DEPTH_BAND", "ZONE_CURRENT",
    "MONSTER_BASE_CHANCE", "MONSTER_DEPTH_BONUS_PER_TIER", "MONSTER_MAX_CHANCE",
    "TOURNAMENT_DURATION_DAYS", "TOURNAMENT_TOP_PAYOUT_RATIO",
    "TOURNAMENT_THEMES", "TOURNAMENT_MIN_POOL_LURE",
    "monster_meta", "monster_spawn_chance", "roll_monster_for_zone",
    "augment_meta", "augments_by_kind", "active_augment",
    "zone_lock_for", "legendary_allowed_in_zone",
    "zone_depth_factor", "zone_current", "current_modifiers",
    "tournament_theme", "tournament_score",
    "bait_meta", "crab_in_zone", "crab_trap_meta",
    "egg_sell_lure",
    "fish_fact", "fish_in_zone", "fish_meta", "fish_payout", "fish_xp",
    "junk_meta", "level_from_xp", "level_payout_mult",
    "quality_for_reaction", "random_hint",
    "rarity_meta", "rod_meta", "roll_bonus_subtype",
    "roll_crab", "roll_fish", "roll_junk", "roll_outcome",
    "roll_treasure_jackpot_fish", "roll_treasure_loot", "roll_weight",
    "roll_wild_battle", "trap_yield_lure",
    "wild_battle_chance", "wild_battle_lure_reward", "wild_battle_reel_reward",
    "wild_buddy_species_pool",
    "xp_to_next", "zone_meta",
]
# === HELPERS_END ===
