"""
buddies_config.py  -  Species, ASCII frames, name pools, and tuning constants
for the CC Buddy system.

All tuning lives here. Cogs must read from this module rather than
hard-coding species data, decay rates, XP curves, or frame tables.
"""
from __future__ import annotations

import random
from typing import Final


# =============================================================================
# XP + leveling
# =============================================================================

# level = floor((1 + sqrt(1 + 8*xp/XP_CURVE)) / 2)
XP_CURVE: Final[int]   = 120
MAX_LEVEL: Final[int]  = 50

# Chat XP grant: one roll per message, bounded by cooldown.
CHAT_XP_MIN: Final[int]       = 3
CHAT_XP_MAX: Final[int]       = 8
CHAT_XP_COOLDOWN_S: Final[int] = 55


# =============================================================================
# Mood decay (per hour; Phase 2 applies these)
# =============================================================================

HUNGER_DECAY_PER_HOUR: Final[int]    = 2
HAPPINESS_DECAY_PER_HOUR: Final[int] = 2
ENERGY_DECAY_PER_HOUR: Final[int]    = 1

# Energy recovers instead of decaying during the hourly sweep when the buddy
# is well-cared-for (hunger and happiness both above ENERGY_REGEN_THRESHOLD).
# This models "buddy naps when left alone in a good mood" and gives the stat
# a natural way to climb back up between interactions.
ENERGY_REGEN_PER_HOUR: Final[int]    = 3
ENERGY_REGEN_THRESHOLD: Final[int]   = 30

# Mood penalty when hunger or happiness hits 0: bonus multiplier drops to 0.5.
MOOD_PENALTY_MULTIPLIER: Final[float] = 0.5

# Run-away trigger (Phase 2): all three conditions must hold.
RUNAWAY_IDLE_HOURS: Final[int] = 24

# Shelter grace window for leave/ban. Surrender and runaway skip the window
# and become adoptable immediately.
SHELTER_GRACE_HOURS: Final[int] = 24

# Background decay/runaway sweep interval. Decay math still works in whole
# hour steps on the DB clock (see last_decay_at), so ticks faster than 1 h
# just mean reduced latency between the hour rolling over and the stat drop
# showing up.
DECAY_TICK_INTERVAL_S: Final[int] = 300   # 5 min

# Adoption mood reset: spec-defined starting stats for an adopted buddy.
ADOPT_MOOD: Final[tuple[int, int, int]] = (50, 30, 70)   # hunger, happiness, energy

# Reclaim mood: the buddy is relieved to see its former owner, so slightly
# better than an adopt baseline but not a full reset.
RECLAIM_MOOD: Final[tuple[int, int, int]] = (50, 60, 70)


# =============================================================================
# Interaction effects (feed / pet / talk)
# =============================================================================

FEED_HUNGER_DELTA: Final[int]    = 25
FEED_HAPPINESS_DELTA: Final[int] = 3
FEED_ENERGY_DELTA: Final[int]    = 10   # food is fuel; feeding perks the buddy up

PET_HAPPINESS_DELTA: Final[int]  = 15
PET_ENERGY_DELTA: Final[int]     = -3

TALK_HAPPINESS_DELTA: Final[int] = 8
TALK_ENERGY_DELTA: Final[int]    = -5

# Per-action cooldowns so the panel can't be mashed.
FEED_COOLDOWN_S: Final[int]  = 600   # 10 min
PET_COOLDOWN_S: Final[int]   = 180   # 3 min
TALK_COOLDOWN_S: Final[int]  = 300   # 5 min

# How long to override the idle animation after an action press.
ACTION_OVERRIDE_S: Final[int] = 6


# =============================================================================
# Live panel
# =============================================================================

PANEL_TICK_INTERVAL_S: Final[float] = 4.0
PANEL_LIFETIME_S: Final[int]        = 600   # 10 min, matches mining dashboards

# Max name length (matches Discord's TextInput ceiling of 32 but kept tighter).
NAME_MIN_LEN: Final[int] = 1
NAME_MAX_LEN: Final[int] = 24

# Rename price in USD (paid out of wallet + bank via deduct_liquid).
# Renaming used to be on a cooldown; it is now uncapped but charges a
# flat fee per successful rename. Failed validations do not charge.
RENAME_PRICE_USD: Final[int] = 10_000


# =============================================================================
# Reroll + species swap
# =============================================================================
# Reroll: free re-roll of the hatched buddy. Up to REROLL_MAX per lifetime.
# Old buddy is hard-deleted (does NOT go to the shelter). Used for "I don't
# like what I rolled" regret within the first few hatches.
REROLL_MAX: Final[int] = 3

# Swap: paid species change on the active buddy. Keeps stats, XP, level,
# rarity tier, and stat allocations -- only species and name change. Price
# doubles each use.
#   swap #1 -> $1,000,000
#   swap #2 -> $2,000,000
#   swap #3 -> $4,000,000
#   swap #n -> SWAP_BASE_PRICE_USD * 2 ** (n - 1)
SWAP_BASE_PRICE_USD: Final[int] = 1_000_000

# Respec: paid refund of all spent stat points on the active buddy.
# Returns hp_alloc / atk_alloc / spd_alloc to 0 so the player can
# reallocate from scratch -- useful when a buddy mis-leveled into a
# losing build. Cheaper base than SWAP since respec only rearranges
# existing points; doubles per use on the same buddy:
#   respec #1 -> $50,000
#   respec #2 -> $100,000
#   respec #3 -> $200,000
#   respec #n -> RESPEC_BASE_PRICE_USD * 2 ** (n - 1)
RESPEC_BASE_PRICE_USD: Final[int] = 50_000


# =============================================================================
# P2P transfers + market (cc_buddy_transfers / cc_buddy_listings)
# =============================================================================
# Direct gift fee paid by the SENDER to move a buddy to another user.
# Flat dollar amount, deducted via deduct_liquid (wallet+bank). Keeps
# wash-trading / alt-account farming a tiny bit costly without making
# the feature feel pay-walled.
BUDDY_GIFT_FEE_USD: Final[int] = 1_000

# Market-sale tax taken from the SELLER's proceeds when a listing
# clears. 500 basis points = 5%. Burns to the void (currency drain).
BUDDY_MARKET_TAX_BPS: Final[int] = 500

# Listing price guards. The minimum stops "free for anyone who calls
# buy first" listing-spam; the maximum keeps an honest typo from
# locking up someone's wallet on a 999B sale.
BUDDY_MARKET_MIN_PRICE_USD: Final[int] = 100
BUDDY_MARKET_MAX_PRICE_USD: Final[int] = 1_000_000_000

# Max active listings per seller per guild. Stops someone from
# turning the market into their personal classifieds.
BUDDY_MARKET_MAX_LISTINGS_PER_USER: Final[int] = 10


# =============================================================================
# Hatch pricing (lifetime curve with a 7-day cool-off reset)
# =============================================================================
# First HATCH_FREE_COUNT lifetime hatches per user are free. Beyond that,
# hatching costs HATCH_BASE_PRICE_USD and doubles for each additional paid
# hatch in the current streak. After HATCH_STREAK_RESET_SECONDS without a
# new hatch the streak resets, so the next hatch is back to base price.
#
#   lifetime hatch #1..3 -> free
#   #4 (paid streak 0)   -> $10,000
#   #5 (paid streak 1)   -> $20,000
#   #6 (paid streak 2)   -> $40,000
#   #n (paid streak k)   -> HATCH_BASE_PRICE_USD * 2 ** k
#
# Paid out of wallet+bank via deduct_liquid; insufficient balance aborts
# the hatch with no state change.
HATCH_FREE_COUNT:            Final[int] = 3
HATCH_BASE_PRICE_USD:        Final[int] = 10_000
HATCH_STREAK_RESET_SECONDS:  Final[int] = 7 * 24 * 3600


# =============================================================================
# Stat-point allocation
# =============================================================================
# Each level grants STAT_POINTS_PER_LEVEL points. Players spend them across
# three tracks via ,buddy upgrade (Hardiness / Power / Vigor). Allocations
# are sticky -- they persist across swap and level changes. Only reroll
# (which destroys the buddy) clears them.
#
# Available points = level - (hp_alloc + atk_alloc + spd_alloc).
# The cap is enforced in the upgrade modal, not in the DB, so a future
# level downgrade (if we ever add one) can't violate a CHECK constraint.
STAT_POINTS_PER_LEVEL: Final[int] = 1

# Per-point bonuses applied at battle build time inside Fighter.from_row.
# HP and ATK are added to the base before the mood multiplier (so well-fed
# / happy buddies amplify their investment); SPD is added after the energy
# floor and capped at 1.0 by the engine.
STAT_POINT_HP_BONUS:  Final[float] = 3.0      # +3 max HP per point
STAT_POINT_ATK_BONUS: Final[float] = 0.5      # +0.5 ATK per point
STAT_POINT_SPD_BONUS: Final[float] = 0.005    # +0.005 SPD per point (cap 1.0)


# =============================================================================
# Species bonus lanes + rarity tiers
# =============================================================================
# Every species picks one "signature lane" where its buddy multiplier grows
# faster with level. Off-lane bonuses still accrue, just more slowly, so no
# species is strictly worse than another in a given feature -- just tilted.
#
# Lanes:
#   "chat"  -- chat-XP grant multiplier   (cogs/chat_leveling.py)
#   "work"  -- work / earn payout         (cogs/earn.py)
#   "trade" -- trade fee rebate           (cogs/trade.py)
#
# Rarity (1..5) is rolled independently of species at hatch / reroll time
# via ``roll_rarity()`` below and stored on each buddy row. Any species can
# come in any tier: a Legendary zenny and a Common nimbus are both possible.
# Species is flavor (appearance + ability flavor + bonus lane); rarity is
# the power dial (base stats, decay, regen, chat XP, ability magnitude).
#
# Bonus lanes
# -----------
# Every cog that pays out scaling rewards reads buddy_bonus(lane=...).
# The original three lanes (chat / work / trade) covered chat XP, the
# work command, and trade fee rebates. The expanded set adds five
# game-cog lanes so the active buddy actually shifts the needle inside
# fishing / farming / delves / crafting / battles too. See
# services/buddy_bonus.py for the formula; the lanes are the contract.
BONUS_LANES: Final[tuple[str, ...]] = (
    "chat", "work", "trade",
    "fishing", "farming", "delve", "craft", "battle",
)

BONUS_LANE_LABELS: Final[dict[str, str]] = {
    "chat":     "Chat XP",
    "work":     "Work payout",
    "trade":    "Trade fee rebate",
    "fishing":  "Fishing yield",
    "farming":  "Farm yield",
    "delve":    "Delve mining + capture",
    "craft":    "Craft success / output",
    "battle":   "Battle damage + XP",
}

# Per-level bonus growth. Applied as: pct = per_level * rarity_bonus_mult * level.
# Signature lane grows ~3x faster than off-lane lanes.
BONUS_SIG_PER_LEVEL: Final[float] = 0.003   # +0.3% / level before rarity multiplier
BONUS_OFF_PER_LEVEL: Final[float] = 0.001   # +0.1% / level before rarity multiplier

# Rarity-driven extra signature lanes
# -----------------------------------
# Rarer buddies get MORE bonus lanes treated as "signature." A Common
# only gets its species' bonus_lane at the fast ramp; Legendaries get
# the species lane PLUS up to 3 extra lanes deterministically chosen
# from a rotated stable per-species pool. The chosen lanes are picked
# in BONUS_LANES order starting from the species' primary, wrapping
# the list, so two buddies of the same species always pick the same
# extras at the same rarity tier.
RARITY_EXTRA_SIGNATURE_LANES: Final[dict[int, int]] = {
    1: 0,    # Common: species lane only
    2: 0,    # Uncommon: species lane only (slightly stronger via rarity_meta.bonus_mult)
    3: 1,    # Rare: species + 1 extra signature lane
    4: 2,    # Epic: species + 2 extra signature lanes
    5: 3,    # Legendary: species + 3 extra signature lanes
}


def buddy_bonus_lanes_for(species: str, rarity_tier: int) -> tuple[str, ...]:
    """Return every lane that the given (species, rarity) buddy treats
    as its signature -- includes the species' primary plus the rarity-
    granted extras. Order: primary first, extras following BONUS_LANES
    order from the primary's index.

    Used by ``services/buddy_bonus.py`` to decide whether to apply the
    SIG ramp vs the OFF ramp for a given lane lookup, and by the panel
    UI to surface "this buddy buffs lane X / Y / Z."
    """
    sp = SPECIES.get(species, {}) if 'SPECIES' in globals() else {}
    primary = str(sp.get("bonus_lane") or "")
    if primary not in BONUS_LANES:
        # Unknown species or not yet wired up: no lanes (safe default).
        return ()
    extras_n = RARITY_EXTRA_SIGNATURE_LANES.get(int(rarity_tier or 1), 0)
    out: list[str] = [primary]
    if extras_n > 0:
        # Walk BONUS_LANES starting just after the primary, wrapping
        # the tuple, taking the next N distinct lanes. Deterministic
        # per-species so two players' Legendary Zenny both buff the
        # same set.
        primary_idx = BONUS_LANES.index(primary)
        n_lanes = len(BONUS_LANES)
        for i in range(1, n_lanes):
            cand = BONUS_LANES[(primary_idx + i) % n_lanes]
            if cand in out:
                continue
            out.append(cand)
            if len(out) - 1 >= extras_n:
                break
    return tuple(out)

# Rarity tier IDs. Stored on cc_buddies.rarity_tier as 1..5.
RARITY_COMMON:    Final[int] = 1
RARITY_UNCOMMON:  Final[int] = 2
RARITY_RARE:      Final[int] = 3
RARITY_EPIC:      Final[int] = 4
RARITY_LEGENDARY: Final[int] = 5

# Tier metadata. Every code path (bonus calc, decay sweep, panel color,
# battle ability scaling) reads from this dict -- never hard-code tier
# stats elsewhere. ``ability_mult`` scales the numeric parameter of a
# species ability (dodge %, poison %, heal %, ATK buff, etc.) so a
# Legendary buddy's ability hits harder than a Common buddy's.
RARITY_TIERS: Final[dict[int, dict]] = {
    RARITY_COMMON: {
        "name":         "Common",
        "color_hex":    0x95a5a6,   # C_NEUTRAL
        "bonus_mult":   1.0,        # multiplies per-level bonus
        "decay_mult":   1.0,        # multiplies hourly decay (lower = slower decay)
        "regen_mult":   1.0,        # multiplies hourly energy regen
        "xp_mult":      1.0,        # multiplies buddy's own chat XP gain
        "hp_base":      100,        # pet-battle HP
        "atk_base":     10,         # pet-battle attack
        "ability_mult": 1.00,       # scales ability magnitude (chance / buff / heal %)
    },
    RARITY_UNCOMMON: {
        "name":         "Uncommon",
        "color_hex":    0x2ecc71,   # C_SUCCESS
        "bonus_mult":   1.3,
        "decay_mult":   0.90,
        "regen_mult":   1.15,
        "xp_mult":      1.15,
        "hp_base":      115,
        "atk_base":     12,
        "ability_mult": 1.15,
    },
    RARITY_RARE: {
        "name":         "Rare",
        "color_hex":    0x3498db,   # C_INFO
        "bonus_mult":   1.6,
        "decay_mult":   0.80,
        "regen_mult":   1.30,
        "xp_mult":      1.30,
        "hp_base":      130,
        "atk_base":     14,
        "ability_mult": 1.30,
    },
    RARITY_EPIC: {
        "name":         "Epic",
        "color_hex":    0x9b59b6,   # C_PURPLE
        "bonus_mult":   2.0,
        "decay_mult":   0.70,
        "regen_mult":   1.50,
        "xp_mult":      1.50,
        "hp_base":      150,
        "atk_base":     17,
        "ability_mult": 1.50,
    },
    RARITY_LEGENDARY: {
        "name":         "Legendary",
        "color_hex":    0xf1c40f,   # C_GOLD
        "bonus_mult":   2.5,
        "decay_mult":   0.55,
        "regen_mult":   1.75,
        "xp_mult":      2.0,
        "hp_base":      180,
        "atk_base":     22,
        "ability_mult": 1.80,
    },
}

# Rarity-roll weights. Drawn independently of species at hatch / reroll
# time so any species can land at any tier. Kept roughly in line with the
# legacy species-rarity distribution (~57% Common .. ~4% Legendary) so
# the hatch feel doesn't change dramatically.
RARITY_ROLL_WEIGHTS: Final[dict[int, int]] = {
    RARITY_COMMON:    58,
    RARITY_UNCOMMON:  18,
    RARITY_RARE:      11,
    RARITY_EPIC:       9,
    RARITY_LEGENDARY:  4,
}


# =============================================================================
# Multi-pet collection
# =============================================================================
# Capacity is split into two distinct purchasable surfaces:
#
#   * BATTLE slots: status='owned' rows. These buddies decay, can be
#     promoted active, and can fight in arena / wild battles. Capped at
#     BATTLE_SLOTS_BASE + BATTLE_SLOTS_MAX_PURCHASED, hard ceiling
#     BATTLE_SLOTS_HARD_CAP.
#   * STORAGE slots: status='stored' rows. Frozen, no decay, can't fight,
#     don't compete for is_active. Capped at STORAGE_SLOTS_BASE +
#     STORAGE_SLOTS_MAX_PURCHASED, hard ceiling STORAGE_SLOTS_HARD_CAP.
#
# Wild captures auto-route: empty battle slot first, else into storage if
# room, else the encounter is rejected (or, on fishing wild-battle wins,
# overflows into the egg system). Players can move buddies between the
# two surfaces with ,buddy store / ,buddy retrieve.
#
# MAX_OWNED_BUDDIES is kept as an alias of BATTLE_SLOTS_BASE for any
# legacy import sites still reading the old name; it is the BATTLE base
# only and never includes storage.
BATTLE_SLOTS_BASE:           Final[int] = 3
BATTLE_SLOTS_MAX_PURCHASED:  Final[int] = 7   # +1 per upgrade, max 10 total
BATTLE_SLOTS_HARD_CAP:       Final[int] = BATTLE_SLOTS_BASE + BATTLE_SLOTS_MAX_PURCHASED

STORAGE_SLOTS_BASE:          Final[int] = 10
STORAGE_SLOTS_PER_UPGRADE:   Final[int] = 10  # +10 per upgrade
STORAGE_SLOTS_MAX_PURCHASED: Final[int] = 9   # 9 upgrades, max 100 total
STORAGE_SLOTS_HARD_CAP:      Final[int] = (
    STORAGE_SLOTS_BASE + STORAGE_SLOTS_PER_UPGRADE * STORAGE_SLOTS_MAX_PURCHASED
)

MAX_OWNED_BUDDIES: Final[int] = BATTLE_SLOTS_BASE


# Egg storage. Mirrors the battle/storage split: held eggs stay on the
# fishing inventory (cap 10, not upgradable, the "with you" tier), and
# overflow lands in egg_storage on user_buddy_economy. Storage is
# upgradable in the buddy shop -- +50 rows per upgrade, base 50, hard
# cap 1000 (19 upgrades).
EGG_HELD_HARD_CAP:           Final[int] = 10
EGG_STORAGE_BASE:            Final[int] = 50
EGG_STORAGE_PER_UPGRADE:     Final[int] = 50
EGG_STORAGE_MAX_PURCHASED:   Final[int] = 19
EGG_STORAGE_HARD_CAP:        Final[int] = (
    EGG_STORAGE_BASE + EGG_STORAGE_PER_UPGRADE * EGG_STORAGE_MAX_PURCHASED
)


# =============================================================================
# Daycare / Breeding
# =============================================================================
# Daycare follows a Pokemon-style "deposit two parents, wait, collect egg"
# pattern. The egg's species + rarity tier are pre-rolled at deposit time so
# the player can plan, not gambled at collect time. Players start with one
# nest slot and can buy up to NEST_SLOTS_MAX_PURCHASED more from the buddy
# shop (cc_buddy_daycare keyed on serial ``id`` post-migration 0215).
#
# DAYCARE_INCUBATION_S: how long the egg has to incubate before it can be
#   collected. Tuned so it's slower than a wild capture but faster than
#   grinding a buddy from level 1 -- a "passive" progression option.
# DAYCARE_FEE_BUD: flat BUD fee charged at deposit. Burned (no recipient)
#   so it's a real BUD sink, mirroring the buddy-shop economy.
# DAYCARE_RARITY_INHERIT_W: weights for the egg's rarity tier roll, conditioned
#   on the parents' tiers. Index = max(parent1_tier, parent2_tier) - 1, value
#   is the (down, equal, up) tuple for whether the egg lands one tier below,
#   at parity, or one tier above the higher parent.
DAYCARE_INCUBATION_S: Final[int]    = 6 * 3600   # 6 hours
DAYCARE_FEE_BUD: Final[float]       = 1000.0
DAYCARE_MIN_PARENT_LEVEL: Final[int] = 5

# Nest capacity. Each user starts with one nest slot and can buy up to
# nine more from the buddy shop (BUD-burned). Mirrors the BATTLE_SLOTS
# ladder so the in-game economy already reads as familiar.
NEST_SLOTS_BASE:           Final[int] = 1
NEST_SLOTS_MAX_PURCHASED:  Final[int] = 9   # +1 per upgrade, max 10 total
NEST_SLOTS_HARD_CAP:       Final[int] = NEST_SLOTS_BASE + NEST_SLOTS_MAX_PURCHASED
DAYCARE_RARITY_INHERIT_W: Final[tuple[tuple[int, int, int], ...]] = (
    # max parent tier 1 (Common)    : never down, mostly Common, rare Uncommon
    (0, 80, 20),
    # max parent tier 2 (Uncommon)  : 10% down, 70% same, 20% up
    (10, 70, 20),
    # max parent tier 3 (Rare)      : 15% down, 70% same, 15% up
    (15, 70, 15),
    # max parent tier 4 (Epic)      : 20% down, 70% same, 10% up
    (20, 70, 10),
    # max parent tier 5 (Legendary) : 25% down, 75% same, 0% up (cap)
    (25, 75, 0),
)

# Note: surrender -> rehatch used to be on a 7-day cooldown to block a
# free-reroll exploit. Hatching now costs USD past HATCH_FREE_COUNT (see
# above), so the cooldown is no longer needed -- the doubling price is
# the new gate. Removed in migration 0138.


# =============================================================================
# Pet battles
# =============================================================================
# Battles derive ALL numbers from existing stats (level, rarity, hunger,
# happiness, energy) plus a per-species ability. No new DB columns needed.
# Formula summary (applied inside services/buddy_battle.py):
#   HP  = (tier.hp_base  + level * 3) * (0.5 + 0.5 * hunger/100)
#   ATK = (tier.atk_base + level * 0.8) * (0.5 + 0.5 * happiness/100)
#   SPD = 0.5 + 0.5 * energy/100                 (0.5 .. 1.0)
# Crit chance = BATTLE_CRIT_BASE + BATTLE_CRIT_SPD_SCALE * SPD.
#
# Ability progression: every species has up to THREE ability slots that
# unlock as the buddy levels up. The primary (ability_key / ability_name /
# ability_desc) is active from Lv 1; the secondary unlocks at
# SECONDARY_ABILITY_LEVEL and the tertiary at TERTIARY_ABILITY_LEVEL.
# Engine: services/buddy_battle.py:_prime_ability primes the unlocked
# slots in level order so a Lv 30 buddy enters the fight with all three.

SECONDARY_ABILITY_LEVEL: Final[int] = 15
TERTIARY_ABILITY_LEVEL:  Final[int] = 30

# Healing cap. Once a fighter has self-healed this fraction of their
# starting max HP across the fight, further regen / preen / lifesteal
# heals are halved. Keeps healing buddies (gloomer / wecco / blazer)
# strong without letting them out-sustain every other archetype.
BATTLE_HEAL_SOFT_CAP_PCT: Final[float] = 1.20

# Above this HP fraction, periodic regen ticks (gloomer Lunar Regen,
# verdant Photo Synth) stop. Heals can still recover from low HP, but
# can't sit at 100% topping up to absorb the next hit risk-free.
BATTLE_REGEN_HP_CAP_PCT: Final[float] = 0.75

BATTLE_MAX_ROUNDS: Final[int] = 8

BATTLE_CRIT_BASE: Final[float]      = 0.10
BATTLE_CRIT_SPD_SCALE: Final[float] = 0.15
BATTLE_CRIT_MULT: Final[float]      = 1.80

# XP reward for the winner's active buddy. Formula rewards punching up:
#   xp_reward = max(BATTLE_XP_MIN, round(BATTLE_XP_SCALE * L_loser / L_winner))
# so a low-level winner beating a high-level buddy earns a big chunk, and a
# high-level bully grinding low-level buddies gets almost nothing.
BATTLE_XP_SCALE: Final[int] = 15
BATTLE_XP_MIN: Final[int]   = 3

# USD prize to the winning OWNER, computed with the same punch-up-friendly
# formula as XP:
#   usd = max(BATTLE_USD_MIN, round(BATTLE_USD_SCALE * L_loser / L_winner, 2))
# capped at BATTLE_USD_MAX so a level-1 bot challenger beating a level-100
# buddy can't print arbitrary money. Values are in dollars, credited to
# the winner's wallet via standard wallet update (not bank). This is the
# BASE prize; staked battles (,buddy battle @user <amount>) add the
# opponent's stake on top.
BATTLE_USD_SCALE: Final[float] = 150.0
BATTLE_USD_MIN:   Final[float] = 10.0
BATTLE_USD_MAX:   Final[float] = 10_000.0

# Player-vs-player stakes. Both challenger and opponent ante up the same
# amount; winner takes both stakes (2 x stake) on top of the base prize
# above. Draws refund both stakes. Declines / timeouts / aborts also
# refund. Bound below BATTLE_STAKE_MIN to keep floor spam cheap, capped
# at BATTLE_STAKE_MAX so one whale can't one-shot every other account.
BATTLE_STAKE_MIN: Final[float] = 1.0
BATTLE_STAKE_MAX: Final[float] = 1_000_000.0

# Per-user battle cooldown so the command doesn't get spammed and so the
# target can't be harassed with back-to-back challenges.
BATTLE_COOLDOWN_S: Final[int] = 300   # 5 min
# Seconds the challenge prompt stays open before auto-declining.
BATTLE_CHALLENGE_TIMEOUT_S: Final[int] = 45


# =============================================================================
# Species
# =============================================================================
# Each species has:
#   emoji     - one-character emoji shown in title/dialogue
#   tagline   - flavor description
#   weight    - relative hatch probability
#   name_pool - species-flavored base names; generator appends a short suffix
#               when the pool is exhausted to avoid collisions
#   frames    - mood -> ASCII face (3 lines max so embeds stay compact)
#   dialogue  - random lines used by the talk action fallback
# =============================================================================

SPECIES: Final[dict[str, dict]] = {
    "zenny": {
        "emoji":   "\U0001F99C",   # parrot
        "tagline": "A scrappy little flyer. Lives for crumbs and loud opinions.",
        "weight":  25,
        "bonus_lane":  "chat",
        "bonus_label": "Chatterbox  -  chat XP scales up fast per level",
        "ability_key":  "extra_turn_every_3",
        "ability_name": "Chatterbox",
        "ability_desc": "Gets a bonus attack every 3rd round.",
        "name_pool": ["Zenny", "Pip", "Chirp", "Mango", "Kiwi", "Biscuit", "Mochi"],
        "dialogue": [
            "Good morning. I watched you sleep again.",
            "What is this hat-shaped food?",
            "I found a shiny thing. It is mine now.",
            "Please say my name one more time.",
        ],
        "frames": {
            "happy": "\n".join([
                "     .---.      ",
                "    | ^ ^ |     ",
                "     \\_V_/      ",
                "      | |       ",
                "      ^ ^       ",
            ]),
            "neutral": "\n".join([
                "     .---.      ",
                "    | o o |     ",
                "     \\_V_/      ",
                "      | |       ",
                "      ^ ^       ",
            ]),
            "hungry": "\n".join([
                "     .---.      ",
                "    | O O |     ",
                "     \\_V_/~     ",
                "      | |       ",
                "      ^ ^       ",
            ]),
            "sad": "\n".join([
                "     .---.      ",
                "    | x x |     ",
                "     \\___/      ",
                "      | |       ",
                "      . .       ",
            ]),
            "eating": "\n".join([
                "     .---.      ",
                "    | * * |     ",
                "     \\nom/      ",
                "      | |       ",
                "      ^ ^       ",
            ]),
            "petted": "\n".join([
                "  *  .---.  *   ",
                "    | ^ ^ |     ",
                "     \\_V_/      ",
                "      | |       ",
                "      ^ ^       ",
            ]),
            "talking": "\n".join([
                "     .---.      ",
                "    | o O |     ",
                "     \\_V_/ ?!   ",
                "      | |       ",
                "      ^ ^       ",
            ]),
        },
    },
    "pyper": {
        "emoji":   "\U0001F40D",   # snake
        "tagline": "A sleepy coil of attitude. Do not poke.",
        "weight":  18,
        "bonus_lane":  "trade",
        "bonus_label": "Sly Trader  -  trade fees rebate per level",
        "ability_key":  "poison_bite",
        "ability_name": "Poison Fang",
        "ability_desc": "25% chance per hit to poison (5% max HP / turn, 3 turns).",
        "name_pool": ["Pyper", "Slycoil", "Noodle", "Basil", "Hiss", "Ziggy"],
        "dialogue": [
            "I could bite you. I choose not to. You are welcome.",
            "Stop moving. I am trying to nap on your signal.",
            "The rock is warm. The rock is good.",
            "Why do you have so many legs.",
        ],
        "frames": {
            # Cobra silhouette: hooded head up top with tongue flicking right,
            # body curves down and left into a rattle tail on the far left.
            "happy": "\n".join([
                "         ___          ",
                "        /^ ^\\         ",
                "        \\   ~~~<      ",
                "         \\ /          ",
                "   ______/ /          ",
                " -=:_____/            ",
            ]),
            "neutral": "\n".join([
                "         ___          ",
                "        /. .\\         ",
                "        \\   ---<      ",
                "         \\ /          ",
                "   ______/ /          ",
                " -=:_____/            ",
            ]),
            "hungry": "\n".join([
                "         ___          ",
                "        /O O\\         ",
                "        \\   ~~~<      ",
                "         \\ /          ",
                "   ______/ /          ",
                " -=:_____/            ",
            ]),
            "sad": "\n".join([
                "         ___          ",
                "        /x x\\         ",
                "        \\    . .      ",
                "         \\ /          ",
                "   ______/ /          ",
                " -=:_____/            ",
            ]),
            "eating": "\n".join([
                "         ___          ",
                "        /* *\\         ",
                "        \\  nom<       ",
                "         \\ /          ",
                "   ______/ /          ",
                " -=:_____/            ",
            ]),
            "petted": "\n".join([
                "     *   ___   *      ",
                "        /^ ^\\         ",
                "        \\   ~~~<      ",
                "         \\ /          ",
                "   ______/ /          ",
                " -=:_____/            ",
            ]),
            "talking": "\n".join([
                "         ___          ",
                "        /o O\\         ",
                "        \\   sss< ?!   ",
                "         \\ /          ",
                "   ______/ /          ",
                " -=:_____/            ",
            ]),
        },
    },
    "cobble": {
        "emoji":   "\U0001F43E",   # paw
        "tagline": "Stubborn. Fluffy. Somehow always covered in dust.",
        "weight":  15,
        "bonus_lane":  "work",
        "bonus_label": "Digger  -  work payouts grow per level",
        "ability_key":  "dodge_20",
        "ability_name": "Lucky Paw",
        "ability_desc": "20% chance to dodge an incoming attack entirely.",
        "name_pool": ["Cobble", "Rocky", "Ash", "Pebble", "Biscuit", "Tuff"],
        "dialogue": [
            "I dug a hole. It is my hole.",
            "You cannot have the sock. The sock is structural.",
            "I saw a leaf. I am still recovering.",
            "Is it dinner time. Is it. Is it. Is it.",
        ],
        "frames": {
            "happy": "\n".join([
                "     /\\___/\\   ",
                "    ( ^   ^ )  ",
                "     >  w  <   ",
                "    /       \\  ",
                "   (_( V )_)   ",
            ]),
            "neutral": "\n".join([
                "     /\\___/\\   ",
                "    ( o   o )  ",
                "     >  -  <   ",
                "    /       \\  ",
                "   (_( V )_)   ",
            ]),
            "hungry": "\n".join([
                "     /\\___/\\   ",
                "    ( O   O )  ",
                "     >  ~  <   ",
                "    /       \\  ",
                "   (_( V )_)   ",
            ]),
            "sad": "\n".join([
                "     /\\___/\\   ",
                "    ( T   T )  ",
                "     >  _  <   ",
                "    /       \\  ",
                "   (_( V )_)   ",
            ]),
            "eating": "\n".join([
                "     /\\___/\\   ",
                "    ( *   * )  ",
                "     > nom <   ",
                "    /       \\  ",
                "   (_( V )_)   ",
            ]),
            "petted": "\n".join([
                "  *  /\\___/\\  *",
                "    ( ^   ^ )  ",
                "     >  w  <   ",
                "    /       \\  ",
                "   (_( V )_)   ",
            ]),
            "talking": "\n".join([
                "     /\\___/\\   ",
                "    ( o   O )  ",
                "     > !?! <   ",
                "    /       \\  ",
                "   (_( V )_)   ",
            ]),
        },
    },
    "glitch": {
        "emoji":   "\U0001F47E",   # alien monster
        "tagline": "Legally not a bug. Emotionally, entirely a bug.",
        "weight":  4,
        "bonus_lane":  "trade",
        "bonus_label": "Hacker  -  trade fee rebate grows fast per level",
        "ability_key":  "damage_reroll",
        "ability_name": "Segfault",
        "ability_desc": "20% chance per hit to reroll damage and keep the higher roll.",
        "name_pool": ["Glitch", "Nulp", "Void", "Byte", "Segfault", "Kernel"],
        "dialogue": [
            "`syntax error`: affection undefined.",
            "I 0x0F love you. Ignore the offset.",
            "Please do not unplug me again. I remember.",
            "You are running in debug mode today, I see.",
        ],
        "frames": {
            "happy": "\n".join([
                "    [#########]",
                "    [# ^   ^ #]",
                "    [#   w   #]",
                "    [#########]",
                "      | | | |  ",
            ]),
            "neutral": "\n".join([
                "    [#########]",
                "    [# o   o #]",
                "    [#   -   #]",
                "    [#########]",
                "      | | | |  ",
            ]),
            "hungry": "\n".join([
                "    [#########]",
                "    [# O   O #]",
                "    [#  ???  #]",
                "    [#########]",
                "      | | | |  ",
            ]),
            "sad": "\n".join([
                "    [#########]",
                "    [# x   x #]",
                "    [#   _   #]",
                "    [#########]",
                "      \\ \\ \\ \\  ",
            ]),
            "eating": "\n".join([
                "    [#########]",
                "    [# *   * #]",
                "    [#  nom  #]",
                "    [#########]",
                "      | | | |  ",
            ]),
            "petted": "\n".join([
                " *  [#########] *",
                "    [# ^   ^ #]  ",
                "    [#   w   #]  ",
                "    [#########]  ",
                "      | | | |    ",
            ]),
            "talking": "\n".join([
                "    [#########]",
                "    [# O   o #]",
                "    [#  ?!?  #]",
                "    [#########]",
                "      | | | |  ",
            ]),
        },
    },
    "nimbus": {
        "emoji":   "☁️",   # cloud
        "tagline": "A sentient weather front. Cries on command.",
        "weight":  2,
        "bonus_lane":  "chat",
        "bonus_label": "Rainmaker  -  legendary chat XP scaling per level",
        "ability_key":  "rain_skip_2",
        "ability_name": "Rain Dance",
        "ability_desc": "Once per battle: skips enemy's next 2 turns (heavy downpour).",
        "name_pool": ["Nimbus", "Mist", "Cirrus", "Drizzle", "Haze"],
        "dialogue": [
            "I made it rain in the kitchen again. Sorry.",
            "You look like you could use a small storm today.",
            "I am 73% sad and 27% cumulus.",
            "Do not reveal my true form to the humans.",
        ],
        "frames": {
            "happy": "\n".join([
                "         ___      ",
                "       _(   )_    ",
                "      (  ^ ^  )   ",
                "       (__w__)    ",
                "        '' ''     ",
            ]),
            "neutral": "\n".join([
                "         ___      ",
                "       _(   )_    ",
                "      (  o o  )   ",
                "       (__-__)    ",
                "        '' ''     ",
            ]),
            "hungry": "\n".join([
                "         ___      ",
                "       _(   )_    ",
                "      (  O O  )   ",
                "       (_____)    ",
                "        ~ ~ ~     ",
            ]),
            "sad": "\n".join([
                "         ___      ",
                "       _(   )_    ",
                "      (  T T  )   ",
                "       (__-__)    ",
                "        ' ' '     ",
            ]),
            "eating": "\n".join([
                "         ___      ",
                "       _(   )_    ",
                "      ( *nom* )   ",
                "       (_____)    ",
                "        '' ''     ",
            ]),
            "petted": "\n".join([
                "    *    ___   *  ",
                "       _(   )_    ",
                "      (  ^ u ^)   ",
                "       (__w__)    ",
                "        '' ''     ",
            ]),
            "talking": "\n".join([
                "         ___      ",
                "       _(   )_    ",
                "      (  o O  )   ",
                "       (_?!?_)    ",
                "        '' ''     ",
            ]),
        },
    },
    "fox": {
        "emoji":   "\U0001F98A",   # fox
        "tagline": "Clever, quick, and almost certainly up to something.",
        "weight":  10,
        "bonus_lane":  "trade",
        "bonus_label": "Clever Trader  -  trade fees rebate per level",
        "ability_key":  "first_strike",
        "ability_name": "First Strike",
        "ability_desc": "First attack of the battle is always a crit.",
        "name_pool": ["Rune", "Sable", "Clover", "Ember", "Juno", "Fen", "Ash"],
        "dialogue": [
            "What does the fox say? Annoying things, mostly.",
            "I buried your keys. You will find them eventually.",
            "Sneaky is just a lifestyle.",
            "Touch my tail and we fight.",
        ],
        "frames": {
            "happy": "\n".join([
                "    /\\ _ /\\      ",
                "   /  ^.^  \\     ",
                "   \\___w___/     ",
                "    (__|__)~~~   ",
            ]),
            "neutral": "\n".join([
                "    /\\ _ /\\      ",
                "   /  o.o  \\     ",
                "   \\___v___/     ",
                "    (__|__)~~~   ",
            ]),
            "hungry": "\n".join([
                "    /\\ _ /\\      ",
                "   /  O.O  \\     ",
                "   \\___~___/     ",
                "    (__|__)~~~   ",
            ]),
            "sad": "\n".join([
                "    /\\ _ /\\      ",
                "   /  T.T  \\     ",
                "   \\___._.__/    ",
                "    (__|__)_     ",
            ]),
            "eating": "\n".join([
                "    /\\ _ /\\      ",
                "   /  *.*  \\     ",
                "   \\__nom__/     ",
                "    (__|__)~~~   ",
            ]),
            "petted": "\n".join([
                "  * /\\ _ /\\ *    ",
                "   /  ^.^  \\     ",
                "   \\___w___/     ",
                "    (__|__)~~~   ",
            ]),
            "talking": "\n".join([
                "    /\\ _ /\\      ",
                "   /  o.O  \\     ",
                "   \\__!?!__/     ",
                "    (__|__)~~~   ",
            ]),
        },
    },
    "cat": {
        "emoji":   "\U0001F408",   # cat
        "tagline": "Aloof, smug, and yours to feed forever.",
        "weight":  14,
        "bonus_lane":  "trade",
        "bonus_label": "Sly Hunter  -  trade fees rebate per level",
        "ability_key":  "first_strike",
        "ability_name": "Pounce",
        "ability_desc": "First attack of the battle is always a crit.",
        "name_pool": [
            "Mochi", "Pickle", "Salem", "Toast", "Bean", "Loaf",
            "Whiskers", "Mittens", "Biscuit", "Noodle", "Soup",
            "Tofu", "Pudding", "Bagel", "Pumpkin",
        ],
        "dialogue": [
            "I knocked it off the table. On purpose.",
            "I love you. Now go away.",
            "Feed me. Then leave.",
            "*purrs aggressively*",
            "I will sit on your laptop now.",
            "The red dot. Where is it.",
        ],
        "frames": {
            "happy": "\n".join([
                "    /\\_/\\      ",
                "   ( ^.^ )     ",
                "   (\")_(\")     ",
            ]),
            "neutral": "\n".join([
                "    /\\_/\\      ",
                "   ( o.o )     ",
                "   (\")_(\")     ",
            ]),
            "hungry": "\n".join([
                "    /\\_/\\      ",
                "   ( O.O )     ",
                "   (\"~_~\")     ",
            ]),
            "sad": "\n".join([
                "    /\\_/\\      ",
                "   ( T_T )     ",
                "   (\")_(\")     ",
            ]),
            "eating": "\n".join([
                "    /\\_/\\      ",
                "   ( *.* )     ",
                "   ( nom )     ",
            ]),
            "petted": "\n".join([
                "  * /\\_/\\ *    ",
                "   ( ^.^ )     ",
                "   (\")_(\")     ",
            ]),
            "talking": "\n".join([
                "    /\\_/\\      ",
                "   ( o.O ) ?   ",
                "   (\")_(\")     ",
            ]),
        },
    },
    "wolf": {
        "emoji":   "\U0001F43A",   # wolf
        "tagline": "Pack loyalty, couch-shaped ambitions.",
        "weight":  6,
        "bonus_lane":  "work",
        "bonus_label": "Hunter  -  work payouts grow per level",
        "ability_key":  "low_hp_rage",
        "ability_name": "Pack Howl",
        "ability_desc": "ATK +50% for the rest of the battle once HP drops below 50%.",
        "name_pool": ["Fang", "Shadow", "Luna", "Ranger", "Ghost", "Timber"],
        "dialogue": [
            "I am pack. You are pack. The sofa is also pack.",
            "AWOOOOO. No reason.",
            "I will guard this couch with my life.",
            "You smell like food. Respect.",
        ],
        "frames": {
            "happy": "\n".join([
                "    /\\   /\\     ",
                "   / ^ v ^ \\    ",
                "   \\___U___/    ",
                "    / | | \\     ",
            ]),
            "neutral": "\n".join([
                "    /\\   /\\     ",
                "   / o v o \\    ",
                "   \\___-___/    ",
                "    / | | \\     ",
            ]),
            "hungry": "\n".join([
                "    /\\   /\\     ",
                "   / O v O \\    ",
                "   \\__VvV__/    ",
                "    / | | \\     ",
            ]),
            "sad": "\n".join([
                "    /\\   /\\     ",
                "   / T v T \\    ",
                "   \\___,___/    ",
                "    / | | \\     ",
            ]),
            "eating": "\n".join([
                "    /\\   /\\     ",
                "   / * v * \\    ",
                "   \\__nom__/    ",
                "    / | | \\     ",
            ]),
            "petted": "\n".join([
                "  * /\\   /\\ *   ",
                "   / ^ v ^ \\    ",
                "   \\___U___/    ",
                "    / | | \\     ",
            ]),
            "talking": "\n".join([
                "    /\\   /\\     ",
                "   / o v O \\    ",
                "   \\__AWO__/    ",
                "    / | | \\     ",
            ]),
        },
    },
    "crab": {
        "emoji":   "\U0001F980",   # crab
        "tagline": "Sidesteps problems. Keeps receipts.",
        "weight":  8,
        "bonus_lane":  "work",
        "bonus_label": "Receipts Keeper  -  work payouts grow per level",
        "ability_key":  "damage_reduction_20",
        "ability_name": "Hard Shell",
        "ability_desc": "Takes 20% less damage from every incoming hit.",
        "name_pool": ["Pinchy", "Sandy", "Reef", "Claudius", "Tide", "Molt"],
        "dialogue": [
            "Sidestep problems. Literally.",
            "Everything is mine. Pinch first, ask later.",
            "I found a button. I am keeping the button.",
            "Do not call me salty.",
        ],
        "frames": {
            "happy": "\n".join([
                "    .--------.    ",
                "  <|  ^    ^  |>  ",
                "   \\____WW____/   ",
                "     v  v  v  v   ",
            ]),
            "neutral": "\n".join([
                "    .--------.    ",
                "  <|  o    o  |>  ",
                "   \\____vv____/   ",
                "     v  v  v  v   ",
            ]),
            "hungry": "\n".join([
                "    .--------.    ",
                "  <|  O    O  |>  ",
                "   \\____~~____/   ",
                "     v  v  v  v   ",
            ]),
            "sad": "\n".join([
                "    .--------.    ",
                "  <|  T    T  |>  ",
                "   \\____..____/   ",
                "     '  '  '  '   ",
            ]),
            "eating": "\n".join([
                "    .--------.    ",
                "  <|  *    *  |>  ",
                "   \\___nom____/   ",
                "     v  v  v  v   ",
            ]),
            "petted": "\n".join([
                " *  .--------.  * ",
                "  <|  ^    ^  |>  ",
                "   \\____WW____/   ",
                "     v  v  v  v   ",
            ]),
            "talking": "\n".join([
                "    .--------.    ",
                "  <|  o    O  |>  ",
                "   \\___!?!____/   ",
                "     v  v  v  v   ",
            ]),
        },
    },
    "shrimp": {
        "emoji":   "\U0001F990",   # shrimp
        "tagline": "Small body. Loud personality. Absolutely not an appetizer.",
        "weight":  5,
        "bonus_lane":  "chat",
        "bonus_label": "Loudmouth  -  chat XP scales up fast per level",
        "ability_key":  "ink_atk_debuff_20",
        "ability_name": "Ink Cloud",
        "ability_desc": "Once per battle: permanently cuts enemy ATK by 20%.",
        "name_pool": ["Popcorn", "Scampi", "Bisque", "Tempura", "Gumbo", "Krill"],
        "dialogue": [
            "Small body. Big opinions.",
            "I am not an appetizer.",
            "The ocean is just big soup.",
            "Why are you so tall.",
        ],
        "frames": {
            "happy": "\n".join([
                "       \\/ \\/       ",
                "      (^ ^)___     ",
                "       \\______\\>>> ",
                "         ' ' '     ",
            ]),
            "neutral": "\n".join([
                "       \\/ \\/       ",
                "      (o o)___     ",
                "       \\______\\>>> ",
                "         ' ' '     ",
            ]),
            "hungry": "\n".join([
                "       \\/ \\/       ",
                "      (O O)___     ",
                "       \\_~~___\\>>> ",
                "         ' ' '     ",
            ]),
            "sad": "\n".join([
                "       \\/ \\/       ",
                "      (T T)___     ",
                "       \\_.____\\..  ",
                "         . . .     ",
            ]),
            "eating": "\n".join([
                "       \\/ \\/       ",
                "      (* *)___     ",
                "       \\_nom__\\>>> ",
                "         ' ' '     ",
            ]),
            "petted": "\n".join([
                "   *   \\/ \\/   *   ",
                "      (^ ^)___     ",
                "       \\______\\>>> ",
                "         ' ' '     ",
            ]),
            "talking": "\n".join([
                "       \\/ \\/       ",
                "      (o O)___     ",
                "       \\_?!?__\\>>> ",
                "         ' ' '     ",
            ]),
        },
    },
    "octopus": {
        "emoji":   "\U0001F419",   # octopus
        "tagline": "Eight arms, zero chill, unreasonable problem-solving skills.",
        "weight":  4,
        "bonus_lane":  "trade",
        "bonus_label": "Problem Solver  -  trade fee rebate grows fast per level",
        "ability_key":  "double_strike",
        "ability_name": "Eight Arms",
        "ability_desc": "Attacks hit twice (each at 65% damage -- 130% net).",
        "name_pool": ["Inko", "Tako", "Ollie", "Bubbles", "Kraken", "Squish"],
        "dialogue": [
            "I have eight hands and still lose the remote.",
            "I dreamt in colors you have never seen.",
            "Let me solve this puzzle. And this one. And this one.",
            "I may or may not be a shapeshifter.",
        ],
        "frames": {
            "happy": "\n".join([
                "      .---.       ",
                "     / ^ ^ \\      ",
                "     \\__u__/      ",
                "    /|/|/|/|\\~~   ",
            ]),
            "neutral": "\n".join([
                "      .---.       ",
                "     / o o \\      ",
                "     \\__-__/      ",
                "    /|/|/|/|\\~~   ",
            ]),
            "hungry": "\n".join([
                "      .---.       ",
                "     / O O \\      ",
                "     \\__~__/      ",
                "    /|/|/|/|\\~~   ",
            ]),
            "sad": "\n".join([
                "      .---.       ",
                "     / T T \\      ",
                "     \\__._.__/    ",
                "      | | | |     ",
            ]),
            "eating": "\n".join([
                "      .---.       ",
                "     / * * \\      ",
                "     \\_nom_/      ",
                "    /|/|/|/|\\~~   ",
            ]),
            "petted": "\n".join([
                "   *  .---.  *    ",
                "     / ^ ^ \\      ",
                "     \\__u__/      ",
                "    /|/|/|/|\\~~   ",
            ]),
            "talking": "\n".join([
                "      .---.       ",
                "     / o O \\      ",
                "     \\_!?!_/      ",
                "    /|/|/|/|\\~~   ",
            ]),
        },
    },
    "wecco": {
        "emoji":   "\U0001F986",   # duck
        "tagline": "A self-assured duck who remembers everything and everyone.",
        "weight":  2,
        "bonus_lane":  "work",
        "bonus_label": "Nine-to-Five Mallard  -  work payouts scale hardest per level",
        "ability_key":  "preen_heal",
        "ability_name": "Preen",
        "ability_desc": "Once per battle, when below 30% HP, heals to 50% HP and buffs ATK +12%.",
        "name_pool": ["Wecco", "Quackers", "Pondy", "Drizzle", "Puddle", "Splash", "Waddles"],
        "dialogue": [
            "I remember your last meal. It was insufficient.",
            "Quack. That was a warning.",
            "The pond is fine. The company could improve.",
            "I have notes. Would you like to hear them. You will.",
        ],
        "frames": {
            "happy": "\n".join([
                "      __            ",
                "    <(^ )___        ",
                "     ( . __)~       ",
                "      U U           ",
            ]),
            "neutral": "\n".join([
                "      __            ",
                "    <(o )___        ",
                "     ( . __)        ",
                "      U U           ",
            ]),
            "hungry": "\n".join([
                "      __            ",
                "    <(O )___        ",
                "     ( . __)~~      ",
                "      U U           ",
            ]),
            "sad": "\n".join([
                "      __            ",
                "    <(x )___        ",
                "     ( . __)        ",
                "      u u           ",
            ]),
            "eating": "\n".join([
                "      __            ",
                "    <(* )___        ",
                "     ( nom_)        ",
                "      U U           ",
            ]),
            "petted": "\n".join([
                "   *  __   *        ",
                "    <(^ )___        ",
                "     ( . __)        ",
                "      U U           ",
            ]),
            "talking": "\n".join([
                "      __            ",
                "    <(o )___  ?!    ",
                "     ( . __)        ",
                "      U U           ",
            ]),
        },
    },
    "lobster": {
        "emoji":   "\U0001F99E",   # lobster
        "tagline": "Ancient. Buttery. Has opinions about the thermostat.",
        "weight":  3,
        "bonus_lane":  "work",
        "bonus_label": "Old Guard  -  work payouts grow fast per level",
        "ability_key":  "stun_15",
        "ability_name": "Pincer Grip",
        "ability_desc": "15% chance per hit to stun the enemy (skip their next turn).",
        "name_pool": ["Thermidor", "Claws", "Rook", "Cardinal", "Chele", "Brine"],
        "dialogue": [
            "My therapist says I have control issues.",
            "Pinchy means hello.",
            "I outlived three of your plants.",
            "Stop calling me buttery.",
        ],
        "frames": {
            "happy": "\n".join([
                "   ~Y~    ~Y~     ",
                "   (X  ^ ^  X)    ",
                "    \\__ww__/      ",
                "     {v v v}      ",
            ]),
            "neutral": "\n".join([
                "   ~Y~    ~Y~     ",
                "   (X  o o  X)    ",
                "    \\__--__/      ",
                "     {v v v}      ",
            ]),
            "hungry": "\n".join([
                "   ~Y~    ~Y~     ",
                "   (X  O O  X)    ",
                "    \\__~~__/      ",
                "     {v v v}      ",
            ]),
            "sad": "\n".join([
                "   ~Y~    ~Y~     ",
                "   (X  T T  X)    ",
                "    \\__..__/      ",
                "     {. . .}      ",
            ]),
            "eating": "\n".join([
                "   ~Y~    ~Y~     ",
                "   (X  * *  X)    ",
                "    \\_nom__/      ",
                "     {v v v}      ",
            ]),
            "petted": "\n".join([
                " * ~Y~    ~Y~ *   ",
                "   (X  ^ ^  X)    ",
                "    \\__ww__/      ",
                "     {v v v}      ",
            ]),
            "talking": "\n".join([
                "   ~Y~    ~Y~     ",
                "   (X  o O  X)    ",
                "    \\_!?!__/      ",
                "     {v v v}      ",
            ]),
        },
    },
    "shrek": {
        "emoji":   "\U0001F9CC",   # troll / ogre
        "tagline": "Get out of my swamp. Layered like an onion. Yells at parfaits.",
        "weight":  5,
        "bonus_lane":  "work",
        "bonus_label": "Swamp Strong  -  work payouts grow per level",
        "ability_key":  "low_hp_rage",
        "ability_name": "Ogre Rage",
        "ability_desc": "ATK +50% for the rest of the battle once HP drops below 50%.",
        "name_pool": ["Shrek", "Swampy", "Onion", "Brogre", "Roar", "Mudwick"],
        "dialogue": [
            "What are you doing in my swamp.",
            "Ogres are like onions. We have layers. And smell.",
            "That'll do, donkey. That'll do.",
            "Better out than in, I always say.",
        ],
        "frames": {
            "happy": "\n".join([
                "   ()_____()      ",
                "   ( ^   ^ )      ",
                "   |  \\_/  |      ",
                "    \\_____/       ",
                "     | | |        ",
            ]),
            "neutral": "\n".join([
                "   ()_____()      ",
                "   ( o   o )      ",
                "   |   -   |      ",
                "    \\_____/       ",
                "     | | |        ",
            ]),
            "hungry": "\n".join([
                "   ()_____()      ",
                "   ( O   O )      ",
                "   |  ROAR |      ",
                "    \\_____/       ",
                "     | | |        ",
            ]),
            "sad": "\n".join([
                "   ()_____()      ",
                "   ( T   T )      ",
                "   |   .   |      ",
                "    \\_____/       ",
                "     | | |        ",
            ]),
            "eating": "\n".join([
                "   ()_____()      ",
                "   ( *   * )      ",
                "   |  nom  |      ",
                "    \\_____/       ",
                "     | | |        ",
            ]),
            "petted": "\n".join([
                " * ()_____() *    ",
                "   ( ^   ^ )      ",
                "   |  \\_/  |      ",
                "    \\_____/       ",
                "     | | |        ",
            ]),
            "talking": "\n".join([
                "   ()_____()      ",
                "   ( o   O )      ",
                "   |  !?!  |      ",
                "    \\_____/       ",
                "     | | |        ",
            ]),
        },
    },
    "spiderlenny": {
        "emoji":   "\U0001F577",   # spider
        "tagline": "Eight legs, two eyebrows, infinitely raised. ( ͡° ͜ʖ ͡°)",
        "weight":  3,
        "bonus_lane":  "chat",
        "bonus_label": "Web Spinner  -  chat XP scales up fast per level",
        "ability_key":  "extra_turn_every_3",
        "ability_name": "Many Legs",
        "ability_desc": "Gets a bonus attack every 3rd round.",
        "name_pool": ["Lenny", "Webby", "Skitter", "Boris", "Stringer", "Itsy"],
        "dialogue": [
            "Hey there. ( ͡° ͜ʖ ͡°)",
            "Please do not vacuum me. We have been through this.",
            "I built a web. It is art. Do not walk through it.",
            "I see you saw me. ( ͡° ͜ʖ ͡°)",
        ],
        "frames": {
            "happy": "\n".join([
                "   \\\\\\       ///   ",
                "    \\\\     //    ",
                "    ( ͡° ͜ʖ ͡°)    ",
                "    //     \\\\    ",
                "   ///       \\\\\\   ",
            ]),
            "neutral": "\n".join([
                "   \\\\\\       ///   ",
                "    \\\\     //    ",
                "    ( ͡o ͜ʖ ͡o)    ",
                "    //     \\\\    ",
                "   ///       \\\\\\   ",
            ]),
            "hungry": "\n".join([
                "   \\\\\\       ///   ",
                "    \\\\     //    ",
                "   ( ͡O ͜ʖ ͡O)~   ",
                "    //     \\\\    ",
                "   ///       \\\\\\   ",
            ]),
            "sad": "\n".join([
                "   |           |  ",
                "    \\\\     //    ",
                "    ( ͡T ͜ʖ ͡T)    ",
                "    //     \\\\    ",
                "   .           .  ",
            ]),
            "eating": "\n".join([
                "   \\\\\\       ///   ",
                "    \\\\     //    ",
                "  ( ͡* ͜ʖ ͡*) nom ",
                "    //     \\\\    ",
                "   ///       \\\\\\   ",
            ]),
            "petted": "\n".join([
                " * \\\\\\       /// * ",
                "    \\\\     //    ",
                "    ( ͡^ ͜ʖ ͡^)    ",
                "    //     \\\\    ",
                "   ///       \\\\\\   ",
            ]),
            "talking": "\n".join([
                "   \\\\\\       ///   ",
                "    \\\\     //    ",
                "  ( ͡o ͜ʖ ͡O) ?!  ",
                "    //     \\\\    ",
                "   ///       \\\\\\   ",
            ]),
        },
    },
    "donkey": {
        "emoji":   "\U0001F434",   # horse face (donkey reuse)
        "tagline": "Talks. Constantly. About waffles. About everything. Will not stop.",
        "weight":  4,
        "bonus_lane":  "chat",
        "bonus_label": "Motormouth  -  chat XP scales up fast per level",
        "ability_key":  "ink_atk_debuff_20",
        "ability_name": "Annoying Voice",
        "ability_desc": "Once per battle: permanently cuts enemy ATK by 20%.",
        "name_pool": ["Donkey", "Waffles", "Burrito", "Hee", "Haw", "Yappy"],
        "dialogue": [
            "We can stay up late, swappin' manly stories.",
            "I'm makin' waffles!",
            "Are we there yet. Are we there yet. Are we there yet.",
            "I like you. You're not a layered onion.",
        ],
        "frames": {
            "happy": "\n".join([
                "    /| /|         ",
                "   ( ^.^ )        ",
                "    \\_v_/         ",
                "    /| |\\         ",
                "    ' ' ' '       ",
            ]),
            "neutral": "\n".join([
                "    /| /|         ",
                "   ( o.o )        ",
                "    \\_-_/         ",
                "    /| |\\         ",
                "    ' ' ' '       ",
            ]),
            "hungry": "\n".join([
                "    /| /|         ",
                "   ( O.O )        ",
                "    \\HEE-/        ",
                "    /| |\\         ",
                "    ' ' ' '       ",
            ]),
            "sad": "\n".join([
                "    \\| |/         ",
                "   ( T.T )        ",
                "    \\___/         ",
                "    /| |\\         ",
                "    . . . .       ",
            ]),
            "eating": "\n".join([
                "    /| /|         ",
                "   ( *.* )        ",
                "    \\nom_/        ",
                "    /| |\\         ",
                "    ' ' ' '       ",
            ]),
            "petted": "\n".join([
                "  * /| /| *       ",
                "   ( ^.^ )        ",
                "    \\_v_/         ",
                "    /| |\\         ",
                "    ' ' ' '       ",
            ]),
            "talking": "\n".join([
                "    /| /|         ",
                "   ( o.O )        ",
                "    \\HAW!/        ",
                "    /| |\\         ",
                "    ' ' ' '       ",
            ]),
        },
    },
    "chungus": {
        "emoji":   "\U0001F407",   # rabbit
        "tagline": "Big. Round. Cursed. A bunny of unreasonable mass.",
        "weight":  2,
        "bonus_lane":  "work",
        "bonus_label": "Cursed Mass  -  work payouts grow per level",
        "ability_key":  "damage_reduction_20",
        "ability_name": "Cursed Mass",
        "ability_desc": "Takes 20% less damage from every incoming hit.",
        "name_pool": ["Chungus", "Big B", "Lasagna", "Rotund", "Massive", "Beefy"],
        "dialogue": [
            "I am big. I contain multitudes. And carrots.",
            "Do not call me chubby. The word is *cursed*.",
            "I have eaten the carrot. I will eat another.",
            "What's up, doc.",
        ],
        "frames": {
            "happy": "\n".join([
                "    () ()         ",
                "   ( ^   ^ )      ",
                "  (   \\_v_/  )    ",
                "  (___________)   ",
                "    'v'   'v'     ",
            ]),
            "neutral": "\n".join([
                "    () ()         ",
                "   ( o   o )      ",
                "  (   \\_-_/  )    ",
                "  (___________)   ",
                "    'v'   'v'     ",
            ]),
            "hungry": "\n".join([
                "    () ()         ",
                "   ( O   O )      ",
                "  (   \\_~_/  )    ",
                "  (___________)   ",
                "    'v'   'v'     ",
            ]),
            "sad": "\n".join([
                "    \\\\ //         ",
                "   ( T   T )      ",
                "  (   \\_._/  )    ",
                "  (___________)   ",
                "    '.'   '.'     ",
            ]),
            "eating": "\n".join([
                "    () ()         ",
                "   ( *   * )      ",
                "  (   \\nom/  )    ",
                "  (___________)   ",
                "    'v'   'v'     ",
            ]),
            "petted": "\n".join([
                " *  () ()  *      ",
                "   ( ^   ^ )      ",
                "  (   \\_v_/  )    ",
                "  (___________)   ",
                "    'v'   'v'     ",
            ]),
            "talking": "\n".join([
                "    () ()         ",
                "   ( o   O )      ",
                "  (   \\!?!/  )    ",
                "  (___________)   ",
                "    'v'   'v'     ",
            ]),
        },
    },
    "thornling": {
        "emoji":   "\U0001F335",   # cactus
        "tagline": "Poke it once. Go on. See what happens.",
        "weight":  5,
        "bonus_lane":  "work",
        "bonus_label": "Prickly Worker  -  work payouts grow per level",
        "ability_key":  "counter_25",
        "ability_name": "Prickle Back",
        "ability_desc": "25% chance when hit to immediately counter for 50% ATK damage.",
        "name_pool": ["Prick", "Spike", "Thorn", "Barb", "Stab", "Needle", "Cactus"],
        "dialogue": [
            "Go ahead. Touch me.",
            "The spines are not decorative. They are a warning.",
            "I survived a drought for six months. You are not my problem.",
            "No I cannot be petted. That is a feature.",
        ],
        "frames": {
            "happy": "\n".join([
                "    /\\/\\/\\    ",
                "   | ^   ^ |  ",
                "   |  (w)  |  ",
                "   |_______|  ",
                "   |||  |||   ",
            ]),
            "neutral": "\n".join([
                "    /\\/\\/\\    ",
                "   | o   o |  ",
                "   |  (-)  |  ",
                "   |_______|  ",
                "   |||  |||   ",
            ]),
            "hungry": "\n".join([
                "    /\\/\\/\\    ",
                "   | O   O |  ",
                "   |  (~)  |  ",
                "   |_______|  ",
                "   |||  |||   ",
            ]),
            "sad": "\n".join([
                "    /\\/\\/\\    ",
                "   | T   T |  ",
                "   |  (.)  |  ",
                "   |_______|  ",
                "   |||  |||   ",
            ]),
            "eating": "\n".join([
                "    /\\/\\/\\    ",
                "   | *   * |  ",
                "   | (nom) |  ",
                "   |_______|  ",
                "   |||  |||   ",
            ]),
            "petted": "\n".join([
                "  * /\\/\\/\\ *  ",
                "   | ^   ^ |  ",
                "   |  (w)  |  ",
                "   |_______|  ",
                "   |||  |||   ",
            ]),
            "talking": "\n".join([
                "    /\\/\\/\\    ",
                "   | o   O |  ",
                "   | (!?!) |  ",
                "   |_______|  ",
                "   |||  |||   ",
            ]),
        },
    },
    "gloomer": {
        "emoji":   "\U0001F311",   # new moon
        "tagline": "A little dark. A little damp. Healing on the inside.",
        "weight":  3,
        "bonus_lane":  "chat",
        "bonus_label": "Night Chatter  -  chat XP scales up per level",
        "ability_key":  "regen_3pct",
        "ability_name": "Lunar Regen",
        "ability_desc": "Heals 2% max HP each round (caps at 75% HP -- can't sit at full).",
        "name_pool": ["Gloomy", "Umbra", "Shade", "Dusk", "Murk", "Veil", "Mire"],
        "dialogue": [
            "The dark is not sad. The dark is just honest.",
            "I am healing. Slowly. You would not understand.",
            "Please do not ask me how I am. I am.",
            "The moon sees everything. So do I.",
        ],
        "frames": {
            "happy": "\n".join([
                "     .---.    ",
                "    ( ^ ^ )   ",
                "   ( \\ w / )  ",
                "    \\_____/   ",
                "    ~ ~ ~ ~   ",
            ]),
            "neutral": "\n".join([
                "     .---.    ",
                "    ( o o )   ",
                "   ( \\ - / )  ",
                "    \\_____/   ",
                "    ~ ~ ~ ~   ",
            ]),
            "hungry": "\n".join([
                "     .---.    ",
                "    ( O O )   ",
                "   ( \\ ~ / )  ",
                "    \\_____/   ",
                "    ~ ~ ~ ~   ",
            ]),
            "sad": "\n".join([
                "     .---.    ",
                "    ( T T )   ",
                "   ( \\ . / )  ",
                "    \\_____/   ",
                "    ~ ~ ~ ~   ",
            ]),
            "eating": "\n".join([
                "     .---.    ",
                "    ( * * )   ",
                "   ( \\nom/ )  ",
                "    \\_____/   ",
                "    ~ ~ ~ ~   ",
            ]),
            "petted": "\n".join([
                "  *  .---.  * ",
                "    ( ^ ^ )   ",
                "   ( \\ w / )  ",
                "    \\_____/   ",
                "    ~ ~ ~ ~   ",
            ]),
            "talking": "\n".join([
                "     .---.    ",
                "    ( o O )   ",
                "   ( \\!?!/ )  ",
                "    \\_____/   ",
                "    ~ ~ ~ ~   ",
            ]),
        },
    },
    "blazer": {
        "emoji":   "\U0001F525",   # fire
        "tagline": "Runs hot. Hits hard. Gives back a little of what it takes.",
        "weight":  5,
        "bonus_lane":  "trade",
        "bonus_label": "Hot Trader  -  trade fee rebate grows per level",
        "ability_key":  "lifesteal_20",
        "ability_name": "Flame Drain",
        "ability_desc": "Heals 15% of every hit's damage back to itself.",
        "name_pool": ["Ember", "Flare", "Cinder", "Scorch", "Blaze", "Ash", "Spark"],
        "dialogue": [
            "I am literally on fire. This is fine.",
            "Every hit I land feeds me. Think about that.",
            "Cold? No. Never. Stop asking.",
            "I do not burn out. I burn through.",
        ],
        "frames": {
            "happy": "\n".join([
                "   ^ ^ ^ ^ ^  ",
                "  /  ^     ^ \\",
                "  |  ( w )  | ",
                "   \\  ~~~  /  ",
                "    \\_____/   ",
            ]),
            "neutral": "\n".join([
                "   ^ ^ ^ ^ ^  ",
                "  /  o     o \\",
                "  |  ( - )  | ",
                "   \\  ~~~  /  ",
                "    \\_____/   ",
            ]),
            "hungry": "\n".join([
                "   . . . . .  ",
                "  /  O     O \\",
                "  |  ( ~ )  | ",
                "   \\        / ",
                "    \\_____/   ",
            ]),
            "sad": "\n".join([
                "   . . . . .  ",
                "  /  T     T \\",
                "  |  ( _ )  | ",
                "   \\        / ",
                "    \\_____/   ",
            ]),
            "eating": "\n".join([
                "   ^ ^ ^ ^ ^  ",
                "  /  *     * \\",
                "  |  (nom)  | ",
                "   \\  ~~~  /  ",
                "    \\_____/   ",
            ]),
            "petted": "\n".join([
                " * ^ ^ ^ ^ ^ *",
                "  /  ^     ^ \\",
                "  |  ( w )  | ",
                "   \\  ~~~  /  ",
                "    \\_____/   ",
            ]),
            "talking": "\n".join([
                "   ^ ^ ^ ^ ^  ",
                "  /  o     O \\",
                "  | ( !?! )  |",
                "   \\  ~~~  /  ",
                "    \\_____/   ",
            ]),
        },
    },
    "draclet": {
        "emoji":   "\U0001F432",   # dragon face
        "tagline": "Small dragon. Still a dragon. Will absolutely finish you off.",
        "weight":  3,
        "bonus_lane":  "trade",
        "bonus_label": "Hoard Instinct  -  trade fee rebate grows fast per level",
        "ability_key":  "execute_30",
        "ability_name": "Death Grip",
        "ability_desc": "Deals +80% bonus damage when the enemy is below 30% HP.",
        "name_pool": ["Drake", "Draco", "Fafnir", "Wyrm", "Scorch", "Nyx", "Claw"],
        "dialogue": [
            "I am a dragon. Yes. Even at this size.",
            "I see your HP bar. It concerns me. For you.",
            "Small? I prefer *precise*.",
            "Every hoard starts somewhere. Mine starts here.",
        ],
        "frames": {
            # Hand-rolled dragon silhouette: spike-crowned head with a
            # long snout, batwings flared off the shoulders, coiled tail
            # tipped with a barb. Each frame keeps the same structure so
            # mood swaps don't shift the silhouette around.
            "happy": "\n".join([
                "      /\\        /\\      ",
                "     /  \\______/  \\     ",
                "    /    ^    ^    \\    ",
                "   <    |  ww  |    >   ",
                "    \\    \\____/    /    ",
                "     \\____/    \\__/     ",
                "          )___)>~~      ",
            ]),
            "neutral": "\n".join([
                "      /\\        /\\      ",
                "     /  \\______/  \\     ",
                "    /    o    o    \\    ",
                "   <    |  --  |    >   ",
                "    \\    \\____/    /    ",
                "     \\____/    \\__/     ",
                "          )___)>~~      ",
            ]),
            "hungry": "\n".join([
                "      /\\        /\\      ",
                "     /  \\______/  \\     ",
                "    /    O    O    \\    ",
                "   <    |  ~~~ |    >   ",
                "    \\    \\____/    /    ",
                "     \\____/    \\__/     ",
                "          )___)>~~      ",
            ]),
            "sad": "\n".join([
                "      /\\        /\\      ",
                "     /  \\______/  \\     ",
                "    /    T    T    \\    ",
                "   <    |  ..  |    >   ",
                "    \\    \\____/    /    ",
                "     \\____/    \\__/     ",
                "          )___)>...     ",
            ]),
            "eating": "\n".join([
                "      /\\        /\\      ",
                "     /  \\______/  \\     ",
                "    /    *    *    \\    ",
                "   <    |  nom |    >   ",
                "    \\    \\____/    /    ",
                "     \\____/    \\__/     ",
                "          )___)>~~      ",
            ]),
            "petted": "\n".join([
                "    * /\\        /\\ *    ",
                "     /  \\______/  \\     ",
                "    /    ^    ^    \\    ",
                "   <    |  ww  |    >   ",
                "    \\    \\____/    /    ",
                "     \\____/    \\__/     ",
                "          )___)>~~      ",
            ]),
            "talking": "\n".join([
                "      /\\        /\\      ",
                "     /  \\______/  \\     ",
                "    /    o    O    \\    ",
                "   <    | RAWR |    >   ",
                "    \\    \\____/   /     ",
                "     \\____/    \\_/      ",
                "          )___)>~~      ",
            ]),
        },
    },
    "robo": {
        "emoji":   "\U0001F916",   # robot
        "tagline": "Optimizes in real time. Getting scarier by the round.",
        "weight":  4,
        "bonus_lane":  "work",
        "bonus_label": "Automated Worker  -  work payouts grow per level",
        "ability_key":  "atk_up_3rounds",
        "ability_name": "Overclock",
        "ability_desc": "ATK increases by 15% every 3rd round (up to 3 stacks).",
        "name_pool": ["Unit", "Zeta", "Axiom", "Servo", "Bleep", "Nand", "Core"],
        "dialogue": [
            "I have calculated the optimal response. It is silence.",
            "Processing your emotional needs. Please hold.",
            "My ATK is climbing. This is not a threat. This is a status update.",
            "Error: affection module not found. Proceeding anyway.",
        ],
        "frames": {
            "happy": "\n".join([
                "  +-------+  ",
                "  | [^][^]|  ",
                "  |   w   |  ",
                "  +-------+  ",
                "   |  |  |   ",
            ]),
            "neutral": "\n".join([
                "  +-------+  ",
                "  | [o][o]|  ",
                "  |   -   |  ",
                "  +-------+  ",
                "   |  |  |   ",
            ]),
            "hungry": "\n".join([
                "  +-------+  ",
                "  | [O][O]|  ",
                "  | ERROR |  ",
                "  +-------+  ",
                "   |  |  |   ",
            ]),
            "sad": "\n".join([
                "  +-------+  ",
                "  | [x][x]|  ",
                "  |  ___  |  ",
                "  +-------+  ",
                "   |  |  |   ",
            ]),
            "eating": "\n".join([
                "  +-------+  ",
                "  | [*][*]|  ",
                "  |  nom  |  ",
                "  +-------+  ",
                "   |  |  |   ",
            ]),
            "petted": "\n".join([
                "* +-------+ *",
                "  | [^][^]|  ",
                "  |   w   |  ",
                "  +-------+  ",
                "   |  |  |   ",
            ]),
            "talking": "\n".join([
                "  +-------+  ",
                "  | [o][O]|  ",
                "  |  !?!  |  ",
                "  +-------+  ",
                "   |  |  |   ",
            ]),
        },
    },
    "tortuga": {
        "emoji":   "\U0001F422",   # turtle
        "tagline": "Slow. Patient. Outlives the conversation.",
        "weight":  4,
        "bonus_lane":  "work",
        "bonus_label": "Steady Hand  -  work payouts grow per level",
        "ability_key":  "fortress_shell",
        "ability_name": "Fortress Shell",
        "ability_desc": "Reflects 12% of every incoming hit AND takes 15% less damage.",
        "name_pool": ["Tortuga", "Shelldon", "Crush", "Bouldur", "Plodder", "Mossback"],
        "dialogue": [
            "I will get there. Eventually.",
            "Hide is not retreat. Hide is strategy.",
            "I am older than your house plants. Show respect.",
            "The shell is not for show. The shell is the show.",
        ],
        "frames": {
            "happy": "\n".join([
                "      _____         ",
                "    _/     \\_       ",
                "   /  ^   ^  \\      ",
                "  | [_______] |     ",
                "   \\_  www  _/      ",
                "     v     v        ",
            ]),
            "neutral": "\n".join([
                "      _____         ",
                "    _/     \\_       ",
                "   /  o   o  \\      ",
                "  | [_______] |     ",
                "   \\_  ---  _/      ",
                "     v     v        ",
            ]),
            "hungry": "\n".join([
                "      _____         ",
                "    _/     \\_       ",
                "   /  O   O  \\      ",
                "  | [_______] |     ",
                "   \\_  ~~~  _/      ",
                "     v     v        ",
            ]),
            "sad": "\n".join([
                "      _____         ",
                "    _/     \\_       ",
                "   /  T   T  \\      ",
                "  | [_______] |     ",
                "   \\_  ...  _/      ",
                "     .     .        ",
            ]),
            "eating": "\n".join([
                "      _____         ",
                "    _/     \\_       ",
                "   /  *   *  \\      ",
                "  | [_______] |     ",
                "   \\_  nom  _/      ",
                "     v     v        ",
            ]),
            "petted": "\n".join([
                "  *   _____   *     ",
                "    _/     \\_       ",
                "   /  ^   ^  \\      ",
                "  | [_______] |     ",
                "   \\_  www  _/      ",
                "     v     v        ",
            ]),
            "talking": "\n".join([
                "      _____         ",
                "    _/     \\_       ",
                "   /  o   O  \\  ?!  ",
                "  | [_______] |     ",
                "   \\_  !?!  _/      ",
                "     v     v        ",
            ]),
        },
    },
    "jolt": {
        "emoji":   "\U0001F42D",   # mouse face (electric mouse)
        "tagline": "Fast. Loud. Static cling has a personality now.",
        "weight":  6,
        "bonus_lane":  "trade",
        "bonus_label": "Quick Trader  -  trade fee rebate grows fast per level",
        "ability_key":  "static_shock",
        "ability_name": "Static Shock",
        "ability_desc": "30% chance per hit to discharge a +50% damage shock.",
        "name_pool": ["Jolt", "Sparx", "Volt", "Buzz", "Zappy", "Tesla", "Coil"],
        "dialogue": [
            "Don't touch me. I'm warning you. ⚡",
            "I am 90% static and 10% poor decisions.",
            "Yes. That was me. Sorry not sorry.",
            "I run on snacks and grudges.",
        ],
        "frames": {
            "happy": "\n".join([
                "    \\^_^/        ",
                "   ( ^_^ )       ",
                "    >---<        ",
                "    /| |\\        ",
                "   ~/    \\~      ",
            ]),
            "neutral": "\n".join([
                "    /o_o\\        ",
                "   ( o_o )       ",
                "    >---<        ",
                "    /| |\\        ",
                "   ~/    \\~      ",
            ]),
            "hungry": "\n".join([
                "    /O_O\\        ",
                "   ( O_O )~      ",
                "    >---<        ",
                "    /| |\\        ",
                "   ~/    \\~      ",
            ]),
            "sad": "\n".join([
                "    \\T_T/        ",
                "   ( T_T )       ",
                "    >---<        ",
                "    /| |\\        ",
                "    .    .       ",
            ]),
            "eating": "\n".join([
                "    /*_*\\        ",
                "   ( nom )       ",
                "    >---<        ",
                "    /| |\\        ",
                "   ~/    \\~      ",
            ]),
            "petted": "\n".join([
                "  * \\^_^/ *      ",
                "   ( ^_^ )       ",
                "    >---<        ",
                "    /| |\\        ",
                "   ~/    \\~      ",
            ]),
            "talking": "\n".join([
                "    /o_O\\  ZAP!  ",
                "   ( !?! )       ",
                "    >---<        ",
                "    /| |\\        ",
                "   ~/    \\~      ",
            ]),
        },
    },
    "phantom": {
        "emoji":   "\U0001F47B",   # ghost
        "tagline": "Half-here, half-not. Always quietly judging you.",
        "weight":  3,
        "bonus_lane":  "chat",
        "bonus_label": "Whisperer  -  chat XP scales up fast per level",
        "ability_key":  "phase_shift",
        "ability_name": "Phase Shift",
        "ability_desc": "30% chance to phase through a hit AND reflect 25% ATK back at attacker.",
        "name_pool": ["Phantom", "Wisp", "Hollow", "Drift", "Echo", "Pale", "Sigh"],
        "dialogue": [
            "I'm not here. You're not here. Nothing is.",
            "I remember dying. It was Tuesday.",
            "Boo. (I am very tired.)",
            "Please walk through me again. I enjoyed that.",
        ],
        "frames": {
            "happy": "\n".join([
                "    .-~~~-.       ",
                "   /  ^ ^  \\      ",
                "   |   w   |      ",
                "    \\_____/       ",
                "     ' ' '        ",
            ]),
            "neutral": "\n".join([
                "    .-~~~-.       ",
                "   /  o o  \\      ",
                "   |   -   |      ",
                "    \\_____/       ",
                "     ' ' '        ",
            ]),
            "hungry": "\n".join([
                "    .-~~~-.       ",
                "   /  O O  \\      ",
                "   |  ~~~  |      ",
                "    \\_____/       ",
                "     ' ' '        ",
            ]),
            "sad": "\n".join([
                "    .-~~~-.       ",
                "   /  T T  \\      ",
                "   |   .   |      ",
                "    \\_____/       ",
                "     . . .        ",
            ]),
            "eating": "\n".join([
                "    .-~~~-.       ",
                "   /  * *  \\      ",
                "   |  nom  |      ",
                "    \\_____/       ",
                "     ' ' '        ",
            ]),
            "petted": "\n".join([
                "  *  .-~~~-.  *   ",
                "   /  ^ ^  \\      ",
                "   |   w   |      ",
                "    \\_____/       ",
                "     ' ' '        ",
            ]),
            "talking": "\n".join([
                "    .-~~~-.       ",
                "   /  o O  \\  ?!  ",
                "   |  !?!  |      ",
                "    \\_____/       ",
                "     ' ' '        ",
            ]),
        },
    },
    "verdant": {
        "emoji":   "\U0001F331",   # seedling
        "tagline": "A small green optimist. Photosynthesises everything.",
        "weight":  4,
        "bonus_lane":  "farming",
        "bonus_label": "Sprout  -  farm yield grows fast per level",
        "ability_key":  "photo_synth",
        "ability_name": "Photo Synth",
        "ability_desc": "Heals 1.5% max HP each round AND ATK +6% per round (up to 4 stacks).",
        "name_pool": ["Verdant", "Sprig", "Mossy", "Leafy", "Petal", "Sage", "Fern"],
        "dialogue": [
            "I would like more sun, please.",
            "Talking to me helps me grow. So talk.",
            "I made a flower today. It's small but it's mine.",
            "Compost is the gift you give the future.",
        ],
        "frames": {
            "happy": "\n".join([
                "      \\|/         ",
                "       Y          ",
                "      / \\         ",
                "    .-( ^ ^ )-.   ",
                "     \\__www__/    ",
            ]),
            "neutral": "\n".join([
                "      \\|/         ",
                "       Y          ",
                "      / \\         ",
                "    .-( o o )-.   ",
                "     \\__---__/    ",
            ]),
            "hungry": "\n".join([
                "      \\|/         ",
                "       y          ",
                "      / \\         ",
                "    .-( O O )-.   ",
                "     \\__~~~__/    ",
            ]),
            "sad": "\n".join([
                "      \\ /         ",
                "       .          ",
                "      / \\         ",
                "    .-( T T )-.   ",
                "     \\__...__/    ",
            ]),
            "eating": "\n".join([
                "      \\|/         ",
                "       Y          ",
                "      / \\         ",
                "    .-( * * )-.   ",
                "     \\__nom__/    ",
            ]),
            "petted": "\n".join([
                "  *   \\|/   *     ",
                "       Y          ",
                "      / \\         ",
                "    .-( ^ ^ )-.   ",
                "     \\__www__/    ",
            ]),
            "talking": "\n".join([
                "      \\|/         ",
                "       Y          ",
                "      / \\  ?!     ",
                "    .-( o O )-.   ",
                "     \\__!?!__/    ",
            ]),
        },
    },
    "mimik": {
        "emoji":   "\U0001F4E6",   # box
        "tagline": "Looks like loot. Acts like loot. Until it doesn't.",
        "weight":  2,
        "bonus_lane":  "delve",
        "bonus_label": "Treasure Sense  -  delve mining grows fast per level",
        "ability_key":  "ambush_strike",
        "ability_name": "Ambush",
        "ability_desc": "First hit each battle is a guaranteed crit; gains +20% crit chance afterward.",
        "name_pool": ["Mimik", "Trove", "Bait", "Lure", "Coffer", "Fakeout", "Catch"],
        "dialogue": [
            "I am definitely a normal chest. Open me.",
            "There's gold in here. Probably. Take a closer look.",
            "Snacks? Yes. Inside me. Reach in.",
            "Chest mode active. Please ignore the teeth.",
        ],
        "frames": {
            "happy": "\n".join([
                "   .-------.      ",
                "  /  ^   ^  \\     ",
                "  |  =====  |     ",
                "  |---------|     ",
                "  '---------'     ",
            ]),
            "neutral": "\n".join([
                "   .-------.      ",
                "  /  o   o  \\     ",
                "  |  =====  |     ",
                "  |---------|     ",
                "  '---------'     ",
            ]),
            "hungry": "\n".join([
                "   .-------.      ",
                "  /  O   O  \\     ",
                "  |  WWWWW  |     ",
                "  |---------|     ",
                "  '---------'     ",
            ]),
            "sad": "\n".join([
                "   .-------.      ",
                "  /  T   T  \\     ",
                "  |  -----  |     ",
                "  |---------|     ",
                "  '---------'     ",
            ]),
            "eating": "\n".join([
                "   .-------.      ",
                "  /  *   *  \\     ",
                "  |  nomnom |     ",
                "  |---------|     ",
                "  '---------'     ",
            ]),
            "petted": "\n".join([
                " *  .-------.  *  ",
                "  /  ^   ^  \\     ",
                "  |  =====  |     ",
                "  |---------|     ",
                "  '---------'     ",
            ]),
            "talking": "\n".join([
                "   .-------.      ",
                "  /  o   O  \\ ?!  ",
                "  |  !?!?!  |     ",
                "  |---------|     ",
                "  '---------'     ",
            ]),
        },
    },
}


# =============================================================================
# Level-gated ability progression (secondary @ Lv 15, tertiary @ Lv 30)
# =============================================================================
# Each species' primary ability is in SPECIES[*]["ability_key"] and is
# always active. ABILITY_PROGRESSION layers two more passive ability
# slots that unlock at SECONDARY_ABILITY_LEVEL and TERTIARY_ABILITY_LEVEL
# respectively. The engine reads the appropriate slot based on the
# fighter's level in services/buddy_battle.py:_prime_ability so a Lv 30
# Legendary buddy fights with all three at full magnitude.
#
# Magnitude scales with rarity exactly the same way primary abilities
# do (RARITY_TIERS[tier]['ability_mult']) so a Legendary buddy's
# secondary is more potent than a Common buddy's.
#
# Ability key reference (see services/buddy_battle.py for handlers):
#   sharp_claws       -- +10% ATK  (passive)
#   tough_hide        -- -10% damage taken  (passive)
#   evasive           -- +10% dodge chance  (passive)
#   lucky_crit        -- +10% crit chance  (passive)
#   swift_recovery    -- +1% max HP regen / round, capped at 75% HP
#   battle_focus      -- crit multiplier 1.80x -> 2.10x
#   iron_will         -- -15% damage taken (stacks with tough_hide)
#   second_wind       -- once / battle, heal 25% max HP at <30% HP
#   killing_blow      -- +50% damage when enemy <25% HP (stacks with execute)
#   berserker         -- +25% ATK once self HP <40%
#   elemental_affinity-- +15% magnitude on PRIMARY ability triggers
#
# Adding a new species: pick one secondary + one tertiary that fits
# the role. New keys must also be wired into _prime_ability + the
# relevant trigger sites in services/buddy_battle.py + cogs/buddy.py.

ABILITY_PROGRESSION: Final[dict[str, dict]] = {
    # Existing species
    "zenny":       {"sec": "sharp_claws",    "ter": "battle_focus"},
    "pyper":       {"sec": "swift_recovery", "ter": "killing_blow"},
    "cobble":      {"sec": "evasive",        "ter": "iron_will"},
    "glitch":      {"sec": "lucky_crit",     "ter": "elemental_affinity"},
    "nimbus":      {"sec": "tough_hide",     "ter": "second_wind"},
    "fox":         {"sec": "sharp_claws",    "ter": "killing_blow"},
    "cat":         {"sec": "evasive",        "ter": "killing_blow"},
    "wolf":        {"sec": "sharp_claws",    "ter": "berserker"},
    "crab":        {"sec": "tough_hide",     "ter": "iron_will"},
    "shrimp":      {"sec": "evasive",        "ter": "battle_focus"},
    "octopus":     {"sec": "lucky_crit",     "ter": "battle_focus"},
    "wecco":       {"sec": "tough_hide",     "ter": "second_wind"},
    "lobster":     {"sec": "sharp_claws",    "ter": "iron_will"},
    "shrek":       {"sec": "tough_hide",     "ter": "berserker"},
    "spiderlenny": {"sec": "evasive",        "ter": "elemental_affinity"},
    "donkey":      {"sec": "sharp_claws",    "ter": "second_wind"},
    "chungus":     {"sec": "tough_hide",     "ter": "iron_will"},
    "thornling":   {"sec": "tough_hide",     "ter": "berserker"},
    "gloomer":     {"sec": "swift_recovery", "ter": "second_wind"},
    "blazer":      {"sec": "sharp_claws",    "ter": "berserker"},
    "draclet":     {"sec": "sharp_claws",    "ter": "killing_blow"},
    "robo":        {"sec": "sharp_claws",    "ter": "elemental_affinity"},
    # New species
    "tortuga":     {"sec": "tough_hide",     "ter": "iron_will"},
    "jolt":        {"sec": "lucky_crit",     "ter": "battle_focus"},
    "phantom":     {"sec": "evasive",        "ter": "killing_blow"},
    "verdant":     {"sec": "swift_recovery", "ter": "iron_will"},
    "mimik":       {"sec": "lucky_crit",     "ter": "berserker"},
}


# Display metadata for each level-gated ability key. Used by the buddy
# panel + species roster to surface what unlocks at each tier.
ABILITY_KIT: Final[dict[str, dict]] = {
    "sharp_claws": {
        "name": "Sharp Claws",
        "desc": "+10% ATK on every hit (passive).",
    },
    "tough_hide": {
        "name": "Tough Hide",
        "desc": "Takes 10% less damage from every incoming hit.",
    },
    "evasive": {
        "name": "Evasive",
        "desc": "+10% dodge chance against every attack.",
    },
    "lucky_crit": {
        "name": "Lucky Strike",
        "desc": "+10% crit chance on every attack.",
    },
    "swift_recovery": {
        "name": "Swift Recovery",
        "desc": "Heals 1% max HP each round (capped at 75% HP).",
    },
    "battle_focus": {
        "name": "Battle Focus",
        "desc": "Crit damage 1.80x -> 2.10x. Crits hit even harder.",
    },
    "iron_will": {
        "name": "Iron Will",
        "desc": "Takes a further 15% less damage (stacks with shells).",
    },
    "second_wind": {
        "name": "Second Wind",
        "desc": "Once per battle: heals 25% max HP when below 30% HP.",
    },
    "killing_blow": {
        "name": "Killing Blow",
        "desc": "+50% damage when the enemy is below 25% HP.",
    },
    "berserker": {
        "name": "Berserker",
        "desc": "Triggers ATK +25% for the rest of the battle once HP drops below 40%.",
    },
    "elemental_affinity": {
        "name": "Affinity",
        "desc": "+15% magnitude on this buddy's primary ability triggers.",
    },
}


def species_ability_progression(species: str) -> dict[str, dict]:
    """Return the unlock plan for a species, or an empty dict.

    Output: ``{"primary": {...}, "secondary": {...}, "tertiary": {...}}``
    where each entry has at least ``key``, ``name``, ``desc``,
    ``unlock_level``. Used by the buddy panel + species roster to render
    the full ability progression and by the engine's ``_prime_ability``
    to know which slots to activate based on the buddy's level.
    """
    sp = SPECIES.get(species, {})
    if not sp:
        return {}
    out: dict[str, dict] = {
        "primary": {
            "key":  str(sp.get("ability_key")  or ""),
            "name": str(sp.get("ability_name") or ""),
            "desc": str(sp.get("ability_desc") or ""),
            "unlock_level": 1,
        },
    }
    plan = ABILITY_PROGRESSION.get(species, {})
    sec_key = str(plan.get("sec") or "")
    ter_key = str(plan.get("ter") or "")
    if sec_key and sec_key in ABILITY_KIT:
        kit = ABILITY_KIT[sec_key]
        out["secondary"] = {
            "key":  sec_key,
            "name": kit["name"],
            "desc": kit["desc"],
            "unlock_level": SECONDARY_ABILITY_LEVEL,
        }
    if ter_key and ter_key in ABILITY_KIT:
        kit = ABILITY_KIT[ter_key]
        out["tertiary"] = {
            "key":  ter_key,
            "name": kit["name"],
            "desc": kit["desc"],
            "unlock_level": TERTIARY_ABILITY_LEVEL,
        }
    return out


# =============================================================================
# Name-pool fallback suffix length (short, numeric, avoids collisions)
# =============================================================================
NAME_SUFFIX_MIN: Final[int] = 10
NAME_SUFFIX_MAX: Final[int] = 99


# =============================================================================
# Mood -> frame key resolution
# =============================================================================

def frame_key_for_mood(hunger: int, happiness: int, energy: int) -> str:
    """Return the idle frame key that best matches current mood stats.

    Action overrides (eating/petted/talking) are chosen by the panel view,
    not by this function -- this only resolves the passive idle animation.
    """
    if hunger <= 10:
        return "hungry"
    if happiness <= 15:
        return "sad"
    if energy <= 10:
        return "sad"
    if happiness >= 75 and hunger >= 40:
        return "happy"
    return "neutral"


def mood_label(hunger: int, happiness: int, energy: int) -> str:
    """One-word mood label for the footer."""
    if hunger <= 10:
        return "Hungry"
    if happiness <= 15:
        return "Sad"
    if energy <= 10:
        return "Sleepy"
    if happiness >= 75 and hunger >= 40:
        return "Happy"
    return "Content"


def roll_rarity() -> int:
    """Draw a rarity tier (1..5) from ``RARITY_ROLL_WEIGHTS``.

    Rarity is rolled independently of species at hatch / reroll time so
    any species can come in any tier. The stored ``cc_buddies.rarity_tier``
    column is the buddy's permanent identity from there on; swap does not
    re-roll.
    """
    tiers  = list(RARITY_ROLL_WEIGHTS.keys())
    weights = list(RARITY_ROLL_WEIGHTS.values())
    return int(random.choices(tiers, weights=weights, k=1)[0])


# Gender constants. Stored as the canonical 'M' / 'F' single-char codes
# in cc_buddies.gender + held_eggs[].gender. Display surfaces translate
# to ♂ / ♀ via gender_glyph() so the data layer never carries Unicode.
GENDER_MALE: str   = "M"
GENDER_FEMALE: str = "F"
GENDER_GLYPH: dict[str, str] = {GENDER_MALE: "♂", GENDER_FEMALE: "♀"}
GENDER_LABEL: dict[str, str] = {GENDER_MALE: "Male", GENDER_FEMALE: "Female"}


def roll_gender() -> str:
    """50/50 random gender draw. Used at egg-creation time (fishing
    drop, daycare collect, wild capture) so the gender is locked in
    before the egg is even visible to the player.
    """
    return random.choice((GENDER_MALE, GENDER_FEMALE))


def gender_glyph(gender: str | None) -> str:
    """Return the ♂ / ♀ symbol for a gender code, or empty string when
    the gender is missing / unknown (legacy rows that pre-date 0174).
    """
    return GENDER_GLYPH.get(str(gender or "").strip().upper(), "")


def parse_gender_token(tok: str) -> str | None:
    """Coerce 'm', 'male', 'boy', '♂', etc. into the canonical 'M' / 'F'
    codes. Returns None when the token doesn't look like a gender.
    """
    s = str(tok or "").strip().lower()
    if s in ("m", "male", "boy", "♂", "♂"):
        return GENDER_MALE
    if s in ("f", "female", "girl", "♀", "♀"):
        return GENDER_FEMALE
    return None


def rarity_meta(tier: int) -> dict:
    """Return the rarity tier metadata, falling back to Common."""
    return RARITY_TIERS.get(int(tier) if tier else RARITY_COMMON, RARITY_TIERS[RARITY_COMMON])


def level_from_xp(xp: int) -> int:
    """Inverse of the XP curve. Capped at MAX_LEVEL."""
    if xp <= 0:
        return 1
    # xp = XP_CURVE * L * (L-1) / 2   =>   L = floor((1 + sqrt(1 + 8x/c)) / 2)
    from math import floor, sqrt
    lvl = floor((1 + sqrt(1 + 8 * xp / XP_CURVE)) / 2)
    return max(1, min(MAX_LEVEL, lvl))


def xp_to_next(xp: int) -> tuple[int, int]:
    """Return (xp_into_current_level, xp_needed_for_next_level)."""
    lvl = level_from_xp(xp)
    if lvl >= MAX_LEVEL:
        return (0, 0)
    # Total XP required to have reached level L (from 1) is XP_CURVE*L*(L-1)/2.
    floor_xp = XP_CURVE * lvl * (lvl - 1) // 2
    next_xp  = XP_CURVE * (lvl + 1) * lvl // 2
    return (xp - floor_xp, next_xp - floor_xp)


def xp_for_level(level: int) -> int:
    """Minimum XP needed to be at ``level``. Inverse of level_from_xp."""
    lvl = max(1, min(MAX_LEVEL, int(level or 1)))
    return XP_CURVE * lvl * (lvl - 1) // 2


def effective_level(row: dict | None) -> int:
    """Return the canonical level for a cc_buddies row.

    Level is derived from XP via :func:`level_from_xp`. When a row was
    created with an explicit level (e.g. wild captures) but XP has not
    caught up, the stored ``level`` column wins so the buddy keeps the
    rank it was promised. The two converge once the level table is
    backfilled (see migration 0198) and every XP-update path also bumps
    level, so this MAX is just defensive against legacy rows.
    """
    if not row:
        return 1
    xp = int(row.get("xp") or 0)
    stored = int(row.get("level") or 1)
    return max(1, min(MAX_LEVEL, max(level_from_xp(xp), stored)))


# =============================================================================
# Arena Map (Buddy Battles expansion)
# =============================================================================
# A branching node-graph of 14 zones across 3 themed regions plus a
# champion-tournament hub. Each player has a single travel cursor
# (cc_buddy_map_progress.current_zone_id) and may move to any
# zone listed in ``neighbors`` of the current zone, gated by
# ``tier_min`` (their active buddy's level must clear the gate).
#
# Clearing all three region bosses flips tournament_state from
# locked -> qualified, opening ``,buddy tourney start``.

ARENA_REGIONS: Final[dict[str, dict]] = {
    "plains": {
        "label":       "Verdant Plains",
        "tagline":     "Wind, wheat, and weak knees.",
        "theme_color": 0x4caf50,   # leaf green
        "boss_zone":   "plains_arena",
    },
    "stone": {
        "label":       "Stoneheart Pass",
        "tagline":     "Old roads. Older grudges.",
        "theme_color": 0x9e8b6e,   # dust tan
        "boss_zone":   "stone_colosseum",
    },
    "tide": {
        "label":       "Tideway Coast",
        "tagline":     "Salt spray and slow tides.",
        "theme_color": 0x1abc9c,   # teal
        "boss_zone":   "tide_amphitheatre",
    },
    "forest": {
        "label":       "Whispering Forest",
        "tagline":     "Bark cracks. So do shins.",
        "theme_color": 0x2e7d32,   # deep green
        "boss_zone":   "druid_circle",
    },
    "volcano": {
        "label":       "Caldera Ridge",
        "tagline":     "Heat haze and hot tempers.",
        "theme_color": 0xd84315,   # ember orange
        "boss_zone":   "magma_caldera",
    },
}


# Zone graph -- directed neighbours; tier_min gates by buddy level.
# Each zone:
#   id            -- DB key (cc_buddy_map_progress.current_zone_id)
#   name          -- player-facing label
#   region        -- key into ARENA_REGIONS (or 'tournament' / 'side')
#   tier_min      -- minimum active buddy level to enter
#   tier_max      -- AI opponent ceiling level
#   neighbors     -- list of zone ids the player may travel to
#   boss          -- bool: clearing unlocks the next region
#   tagline       -- short flavor (one line)
#   reward_usd    -- progression curve marker (NOT a DSD payout).
#                    services.buddy_arena_map.zone_rewards_human()
#                    converts this into the BUD + BBT amounts actually
#                    credited on clear. Zones never pay DSD.
#   item_drop     -- battle-consumable key that may drop on clear
#   bg_gradient   -- (top_hex, bottom_hex) for the battle scene backdrop
#   mood_bias     -- string, biases AI ability rolls ("aggressive", "defensive", ...)

ARENA_ZONES: Final[dict[str, dict]] = {
    # ── Plains region ─────────────────────────────────────────────────
    "plains_gate": {
        "name": "Plains Gate", "region": "plains",
        "tier_min": 1, "tier_max": 5,
        "neighbors": ["grassy_meadow", "windmill_lane"],
        "boss": False,
        "tagline": "Where every buddy's journey starts.",
        "reward_usd": 250, "item_drop": "berry_quick",
        "bg_gradient": (0x6abf69, 0x2c5e3b), "mood_bias": "neutral",
    },
    "grassy_meadow": {
        "name": "Grassy Meadow", "region": "plains",
        "tier_min": 3, "tier_max": 8,
        "neighbors": ["plains_arena", "windmill_lane"],
        "boss": False,
        "tagline": "Tall grass, taller stories.",
        "reward_usd": 500, "item_drop": "berry_focus",
        "bg_gradient": (0x82c97d, 0x355c2b), "mood_bias": "aggressive",
    },
    "windmill_lane": {
        "name": "Windmill Lane", "region": "plains",
        "tier_min": 4, "tier_max": 10,
        "neighbors": ["plains_arena", "ember_grove"],
        "boss": False,
        "tagline": "Old gears, fresh fights.",
        "reward_usd": 700, "item_drop": "dust_swift",
        "bg_gradient": (0xb1b46d, 0x4a4a25), "mood_bias": "defensive",
    },
    "plains_arena": {
        "name": "Plains Arena", "region": "plains",
        "tier_min": 6, "tier_max": 14,
        "neighbors": ["stone_pass"],
        "boss": True,
        "tagline": "Region boss: the Meadow King and his retinue.",
        "reward_usd": 2_500, "item_drop": "vial_rage",
        "bg_gradient": (0xf2c75c, 0x6b4a17), "mood_bias": "aggressive",
    },

    # ── Stone region ──────────────────────────────────────────────────
    "stone_pass": {
        "name": "Stone Pass", "region": "stone",
        "tier_min": 8, "tier_max": 14,
        "neighbors": ["quarry_pit", "obsidian_ridge"],
        "boss": False,
        "tagline": "Cold rocks, warmer welcomes.",
        "reward_usd": 1_200, "item_drop": "vial_iron",
        "bg_gradient": (0xa49080, 0x3b332c), "mood_bias": "defensive",
    },
    "quarry_pit": {
        "name": "Quarry Pit", "region": "stone",
        "tier_min": 10, "tier_max": 18,
        "neighbors": ["stone_colosseum", "obsidian_ridge"],
        "boss": False,
        "tagline": "Dust in your eyes, grit in your gut.",
        "reward_usd": 1_800, "item_drop": "cure_balm",
        "bg_gradient": (0x8a7361, 0x2f261d), "mood_bias": "neutral",
    },
    "obsidian_ridge": {
        "name": "Obsidian Ridge", "region": "stone",
        "tier_min": 12, "tier_max": 20,
        "neighbors": ["stone_colosseum", "moonlit_pool"],
        "boss": False,
        "tagline": "Sharp edges, sharper opponents.",
        "reward_usd": 2_200, "item_drop": "shock_bolt",
        "bg_gradient": (0x4a4757, 0x141320), "mood_bias": "aggressive",
    },
    "stone_colosseum": {
        "name": "Stone Colosseum", "region": "stone",
        "tier_min": 14, "tier_max": 24,
        "neighbors": ["tide_shore"],
        "boss": True,
        "tagline": "Region boss: the Granite Champion and his shieldmates.",
        "reward_usd": 5_500, "item_drop": "vial_iron",
        "bg_gradient": (0xc0c0c0, 0x33333a), "mood_bias": "defensive",
    },

    # ── Tide region ───────────────────────────────────────────────────
    "tide_shore": {
        "name": "Tide Shore", "region": "tide",
        "tier_min": 16, "tier_max": 22,
        "neighbors": ["coral_cove", "lighthouse_hop"],
        "boss": False,
        "tagline": "The water remembers every fight.",
        "reward_usd": 3_000, "item_drop": "cure_balm",
        "bg_gradient": (0x4dd0e1, 0x0e3a4a), "mood_bias": "neutral",
    },
    "coral_cove": {
        "name": "Coral Cove", "region": "tide",
        "tier_min": 18, "tier_max": 26,
        "neighbors": ["tide_amphitheatre", "lighthouse_hop"],
        "boss": False,
        "tagline": "Pretty rocks bite.",
        "reward_usd": 4_200, "item_drop": "shock_bolt",
        "bg_gradient": (0xff7fa3, 0x3a1c4a), "mood_bias": "aggressive",
    },
    "lighthouse_hop": {
        "name": "Lighthouse Hop", "region": "tide",
        "tier_min": 20, "tier_max": 30,
        "neighbors": ["tide_amphitheatre"],
        "boss": False,
        "tagline": "One light. Many shadows.",
        "reward_usd": 5_000, "item_drop": "phoenix_tear",
        "bg_gradient": (0xffd86b, 0x1a2233), "mood_bias": "defensive",
    },
    "tide_amphitheatre": {
        "name": "Tide Amphitheatre", "region": "tide",
        "tier_min": 22, "tier_max": 34,
        "neighbors": ["champion_hall"],
        "boss": True,
        "tagline": "Region boss: the Tideborn Sovereign.",
        "reward_usd": 12_000, "item_drop": "phoenix_tear",
        "bg_gradient": (0x4fc3f7, 0x0a2740), "mood_bias": "aggressive",
    },

    # ── Side zones (hidden / conditional unlock) ──────────────────────
    "ember_grove": {
        "name": "Ember Grove", "region": "side",
        "tier_min": 5, "tier_max": 15,
        "neighbors": ["windmill_lane"],
        "boss": False,
        "tagline": "Warm wood, warmer welcomes. (Side route)",
        "reward_usd": 1_500, "item_drop": "vial_rage",
        "bg_gradient": (0xff7043, 0x331a10), "mood_bias": "aggressive",
        "hidden": True,
    },
    "moonlit_pool": {
        "name": "Moonlit Pool", "region": "side",
        "tier_min": 14, "tier_max": 24,
        "neighbors": ["obsidian_ridge"],
        "boss": False,
        "tagline": "Reflections fight back. (Side route)",
        "reward_usd": 4_000, "item_drop": "dust_swift",
        "bg_gradient": (0xa987d8, 0x1d1633), "mood_bias": "defensive",
        "hidden": True,
    },

    # ── Forest region (new) -- unlocks after Plains Arena clear ──────
    "whisper_path": {
        "name": "Whisper Path", "region": "forest",
        "tier_min": 8, "tier_max": 14,
        "neighbors": ["stone_pass", "fern_hollow", "caravan_clearing"],
        "boss": False,
        "tagline": "The trees know your name. They aren't happy about it.",
        "reward_usd": 1_300, "item_drop": "berry_focus",
        "bg_gradient": (0x386641, 0x0f1f15), "mood_bias": "neutral",
        "clear_target": 3,
    },
    "fern_hollow": {
        "name": "Fern Hollow", "region": "forest",
        "tier_min": 10, "tier_max": 16,
        "neighbors": ["whisper_path", "thorn_thicket", "mossy_market"],
        "boss": False,
        "tagline": "Soft moss. Hard hits.",
        "reward_usd": 1_700, "item_drop": "cure_balm",
        "bg_gradient": (0x4f7942, 0x172614), "mood_bias": "defensive",
        "clear_target": 3,
    },
    "thorn_thicket": {
        "name": "Thorn Thicket", "region": "forest",
        "tier_min": 12, "tier_max": 18,
        "neighbors": ["fern_hollow", "druid_circle"],
        "boss": False,
        "tagline": "Watch your step. The forest fights back.",
        "reward_usd": 2_100, "item_drop": "vial_rage",
        "bg_gradient": (0x3f5f3a, 0x10180e), "mood_bias": "aggressive",
        "clear_target": 3,
    },
    "druid_circle": {
        "name": "Druid Circle", "region": "forest",
        "tier_min": 14, "tier_max": 22,
        "neighbors": ["thorn_thicket", "volcano_gate"],
        "boss": True, "boss_species": "thornling",
        "tagline": "Region boss: the Bramble Druid and her bound wardens.",
        "reward_usd": 6_000, "item_drop": "phoenix_tear",
        "bg_gradient": (0x2d4a2e, 0x0a140a), "mood_bias": "defensive",
        "clear_target": 1,
    },

    # ── Volcano region (new) -- unlocks after Forest boss ────────────
    "volcano_gate": {
        "name": "Volcano Gate", "region": "volcano",
        "tier_min": 18, "tier_max": 24,
        "neighbors": ["druid_circle", "ember_steppes", "ash_springs"],
        "boss": False,
        "tagline": "The air shimmers. Your buddy's fur stands on end.",
        "reward_usd": 3_500, "item_drop": "vial_iron",
        "bg_gradient": (0xb33a1c, 0x33120a), "mood_bias": "aggressive",
        "clear_target": 3,
    },
    "ember_steppes": {
        "name": "Ember Steppes", "region": "volcano",
        "tier_min": 20, "tier_max": 26,
        "neighbors": ["volcano_gate", "lava_tube"],
        "boss": False,
        "tagline": "Black sand, black smoke, blacker mood.",
        "reward_usd": 4_400, "item_drop": "dust_swift",
        "bg_gradient": (0x8c2e0f, 0x1d0905), "mood_bias": "aggressive",
        "clear_target": 3,
    },
    "lava_tube": {
        "name": "Lava Tube", "region": "volcano",
        "tier_min": 22, "tier_max": 28,
        "neighbors": ["ember_steppes", "magma_caldera", "smith_camp"],
        "boss": False,
        "tagline": "Narrow tunnels. Nowhere to dodge.",
        "reward_usd": 5_500, "item_drop": "shock_bolt",
        "bg_gradient": (0xff5722, 0x140404), "mood_bias": "defensive",
        "clear_target": 3,
    },
    "magma_caldera": {
        "name": "Magma Caldera", "region": "volcano",
        "tier_min": 24, "tier_max": 32,
        "neighbors": ["lava_tube", "tide_shore"],
        "boss": True, "boss_species": "blazer",
        "tagline": "Region boss: the Caldera Tyrant and its forgeguard.",
        "reward_usd": 9_000, "item_drop": "phoenix_tear",
        "bg_gradient": (0xff3d00, 0x180400), "mood_bias": "aggressive",
        "clear_target": 1,
    },

    # ── Special locations (no combat, separate UI flow) ───────────────
    "mossy_market": {
        "name": "Mossy Market", "region": "special",
        "kind": "shop",
        "tier_min": 1, "tier_max": 50,
        "neighbors": ["fern_hollow"],
        "boss": False,
        "tagline": "Vines for shelves. Coins for trouble.",
        "reward_usd": 0, "item_drop": None,
        "bg_gradient": (0x5d7553, 0x1a1d10), "mood_bias": "neutral",
    },
    "ash_springs": {
        "name": "Ash Springs", "region": "special",
        "kind": "spring",
        "tier_min": 1, "tier_max": 50,
        "neighbors": ["volcano_gate", "smith_camp"],
        "boss": False,
        "tagline": "Warm water, scorched stone. Your buddy sighs in relief.",
        "reward_usd": 0, "item_drop": None,
        "bg_gradient": (0xff8a65, 0x3a1a14), "mood_bias": "neutral",
    },
    "smith_camp": {
        "name": "Smith's Camp", "region": "special",
        "kind": "dig",
        "tier_min": 1, "tier_max": 50,
        "neighbors": ["lava_tube", "ash_springs"],
        "boss": False,
        "tagline": "Pick a stone, win a prize. Daily.",
        "reward_usd": 0, "item_drop": None,
        "bg_gradient": (0x7a5230, 0x1c130a), "mood_bias": "neutral",
    },
    "caravan_clearing": {
        "name": "Caravan Clearing", "region": "special",
        "kind": "trader",
        "tier_min": 1, "tier_max": 50,
        "neighbors": ["druid_circle", "whisper_path"],
        "boss": False,
        "tagline": "Wagons, woven rugs, and offers that change with the wind.",
        "reward_usd": 0, "item_drop": None,
        "bg_gradient": (0x6d4c41, 0x1c130e), "mood_bias": "neutral",
    },

    # ── Tournament hub ────────────────────────────────────────────────
    "champion_hall": {
        "name": "Champion Hall", "region": "tournament",
        "tier_min": 25, "tier_max": 50,
        "neighbors": [],
        "boss": False,
        "tagline": "The road ends here. The bracket begins.",
        "reward_usd": 25_000, "item_drop": "phoenix_tear",
        "bg_gradient": (0xf1c40f, 0x2c1f00), "mood_bias": "aggressive",
    },
}


def _mirror_arena_neighbors(zones: dict[str, dict]) -> None:
    """Make every neighbour edge bidirectional in-place.

    The original graph was authored one-directionally (a -> b without
    b -> a), which stranded players when they travelled "forward" and
    needed to back out. The map is a node graph, not a one-way river:
    if A lists B as a neighbour, B gets A back.
    """
    for zid, z in zones.items():
        if not isinstance(z.get("neighbors"), list):
            continue
        for nb in list(z["neighbors"]):
            nz = zones.get(nb)
            if not nz:
                continue
            nbrs = nz.setdefault("neighbors", [])
            if zid not in nbrs:
                nbrs.append(zid)


_mirror_arena_neighbors(ARENA_ZONES)


# Per-zone wild opponent pools. Each list is the species that may
# spawn as a "Wild X" when the player runs `,buddy map battle` at
# that zone. The pool is keyed by zone id; zones not listed fall
# back to ARENA_DEFAULT_WILD_POOL. Boss zones don't use these --
# they use the boss_species field on the zone instead.
ARENA_DEFAULT_WILD_POOL: Final[list[str]] = ["zenny", "fox", "cobble"]

ZONE_WILD_POOLS: Final[dict[str, list[str]]] = {
    # Plains -- soft starter species
    "plains_gate":       ["zenny", "fox", "cobble", "cat"],
    "grassy_meadow":     ["zenny", "fox", "thornling", "cat"],
    "windmill_lane":     ["zenny", "fox", "draclet", "cat"],
    # Stone -- rocky and tougher
    "stone_pass":        ["cobble", "wolf", "thornling"],
    "quarry_pit":        ["cobble", "wolf", "draclet"],
    "obsidian_ridge":    ["cobble", "draclet", "blazer"],
    # Tide -- aquatic
    "tide_shore":        ["crab", "shrimp", "fox"],
    "coral_cove":        ["crab", "shrimp"],
    "lighthouse_hop":    ["crab", "fox", "draclet"],
    # Forest (new) -- cats stalk the woods too
    "whisper_path":      ["fox", "thornling", "zenny", "cat"],
    "fern_hollow":       ["thornling", "fox", "wolf", "cat"],
    "thorn_thicket":     ["thornling", "wolf", "draclet"],
    # Volcano (new)
    "volcano_gate":      ["blazer", "draclet", "cobble"],
    "ember_steppes":     ["blazer", "draclet", "wolf"],
    "lava_tube":         ["blazer", "draclet"],
    # Side
    "ember_grove":       ["blazer", "fox", "thornling"],
    "moonlit_pool":      ["draclet", "shrimp", "thornling", "cat"],
}

# Boss species are forced (rarity_tier 3) per boss zone.
ZONE_BOSS_SPECIES: Final[dict[str, str]] = {
    "plains_arena":       "wolf",
    "stone_colosseum":    "cobble",
    "tide_amphitheatre":  "crab",
    "druid_circle":       "thornling",
    "magma_caldera":      "blazer",
}


# Per-boss variants. Each boss zone gets a unique display name, a unique
# named ability that overrides the species default, and a visual overlay
# painted on top of the species portrait so the captured boss reads as a
# distinct creature (a Meadow King is visibly NOT the same as a generic
# wolf you might own). Overlay key references a draw routine in
# services/buddy_portrait.py:_draw_boss_overlay.
#
# These also drive the boss intro embed in `,buddy map boss`.

BOSS_VARIANTS: Final[dict[str, dict]] = {
    "plains_arena": {
        "display_name": "Meadow King",
        "title":        "King of the Verdant Plains",
        "ability_key":  "low_hp_rage",       # rage when wounded
        "ability_name": "Royal Fury",
        "overlay":      "crown",             # gold crown + golden mane
        "accent_tint":  0xFFD54F,
    },
    "stone_colosseum": {
        "display_name": "Granite Champion",
        "title":        "Unbroken Champion of Stoneheart",
        "ability_key":  "damage_reduction_20",  # tough hide
        "ability_name": "Bulwark",
        "overlay":      "helm",                 # iron helm with horns
        "accent_tint":  0xB0BEC5,
    },
    "tide_amphitheatre": {
        "display_name": "Tideborn Sovereign",
        "title":        "Sovereign of the Restless Sea",
        "ability_key":  "rain_skip_2",       # rain dance
        "ability_name": "Tide Decree",
        "overlay":      "trident_crown",     # coral crown + trident shape
        "accent_tint":  0x4DD0E1,
    },
    "druid_circle": {
        "display_name": "Bramble Druid",
        "title":        "Warden of the Whispering Forest",
        "ability_key":  "regen_3pct",        # lunar/photosynth regen
        "ability_name": "Verdant Renewal",
        "overlay":      "antlers",           # antler crown + leaf aura
        "accent_tint":  0x66BB6A,
    },
    "magma_caldera": {
        "display_name": "Caldera Tyrant",
        "title":        "Tyrant of Caldera Ridge",
        "ability_key":  "atk_up_3rounds",    # overclock fury
        "ability_name": "Forge Roar",
        "overlay":      "flame_mane",        # flame mane + ember aura
        "accent_tint":  0xFF6E40,
    },
}


def boss_variant(zone_id: str) -> dict:
    """Return the BOSS_VARIANTS entry for ``zone_id`` or {}."""
    return BOSS_VARIANTS.get(str(zone_id or ""), {})


def arena_graph_asymmetries() -> list[tuple[str, str]]:
    """Return any one-way edges still present in ARENA_ZONES.

    Pure diagnostic for the ``,buddy map graph`` admin check. Always
    returns ``[]`` once :func:`_mirror_arena_neighbors` has run on
    module load, but kept as a safety net for future zone authors.
    """
    bad: list[tuple[str, str]] = []
    for zid, z in ARENA_ZONES.items():
        for nb in z.get("neighbors") or []:
            nz = ARENA_ZONES.get(nb) or {}
            if zid not in (nz.get("neighbors") or []):
                bad.append((zid, nb))
    return bad


# Tournament bracket -- 4 rounds, single elimination vs scaling AI.
# Each entry:
#   round         -- 1..4
#   label         -- player-facing tier name
#   level_bonus   -- +N levels added to the AI opponent
#   reward_usd    -- DSD payout for clearing
#   reward_item   -- consumable awarded on clear
TOURNAMENT_BRACKET: Final[tuple[dict, ...]] = (
    {"round": 1, "label": "Quarterfinal",
     "level_bonus": 0,  "reward_usd": 5_000,  "reward_item": "berry_focus"},
    {"round": 2, "label": "Semifinal",
     "level_bonus": 5,  "reward_usd": 10_000, "reward_item": "vial_iron"},
    {"round": 3, "label": "Final",
     "level_bonus": 10, "reward_usd": 25_000, "reward_item": "cure_balm"},
    {"round": 4, "label": "Champion Match",
     "level_bonus": 15, "reward_usd": 100_000, "reward_item": "phoenix_tear"},
)


TOURNAMENT_FINAL_TITLE: Final[str] = "Buddy Champion"
TRAVEL_COOLDOWN_S: Final[int] = 30
ZONE_BATTLE_COOLDOWN_S: Final[int] = 60


# =============================================================================
# Battle consumables (Buddy Battles expansion)
# =============================================================================
# Catalogue of items selectable via the in-battle dropdown. Each item:
#   key          -- stable id (matches items_config key + crafting_config apply)
#   name, emoji  -- display
#   round_cd     -- rounds-on-cooldown after a use (Pokemon-style)
#   rarity       -- common/uncommon/rare/epic
#   effect       -- one of:
#                     heal_pct           -- restore N % of max HP
#                     atk_buff_temp      -- +N atk_mult for ``duration`` rounds
#                     def_buff_temp      -- 1 - N dmg_taken_mult for ``duration``
#                     crit_next          -- next attack guaranteed crit (1 round)
#                     spd_perm           -- +N spd for the rest of the battle
#                     cleanse_heal       -- clear debuffs + N % HP heal
#                     shock_attack       -- throw N x ATK dmg, stun foe 1 turn
#                     revive             -- self-revive once at N % HP if KO'd
#   magnitude    -- numeric scale read by the effect resolver
#   duration     -- rounds the effect lasts (0 = instant)
#   description  -- player-facing one-liner for the dropdown

BATTLE_CONSUMABLES: Final[dict[str, dict]] = {
    "berry_quick": {
        "name":     "Quick Berry",      "emoji":    "\U0001F353",  # strawberry
        "round_cd": 3, "rarity":  "common",
        "effect":   "heal_pct", "magnitude": 0.25, "duration": 0,
        "description": "Restore 25% of max HP.",
    },
    "berry_focus": {
        "name":     "Focus Berry",      "emoji":    "\U0001FAD0",  # blueberries
        "round_cd": 4, "rarity":  "common",
        "effect":   "crit_next", "magnitude": 1.0, "duration": 1,
        "description": "Your next attack is a guaranteed crit.",
    },
    "vial_rage": {
        "name":     "Vial of Rage",     "emoji":    "\U0001F9EA",  # test tube
        "round_cd": 5, "rarity":  "uncommon",
        "effect":   "atk_buff_temp", "magnitude": 0.30, "duration": 2,
        "description": "+30% ATK for 2 rounds.",
    },
    "vial_iron": {
        "name":     "Iron Vial",        "emoji":    "\U0001F9F2",  # magnet (defence)
        "round_cd": 5, "rarity":  "uncommon",
        "effect":   "def_buff_temp", "magnitude": 0.25, "duration": 2,
        "description": "-25% damage taken for 2 rounds.",
    },
    "dust_swift": {
        "name":     "Swift Dust",       "emoji":    "\U0001F4A8",  # dash
        "round_cd": 4, "rarity":  "uncommon",
        "effect":   "spd_perm", "magnitude": 0.30, "duration": 0,
        "description": "+0.30 SPD for the rest of the battle.",
    },
    "cure_balm": {
        "name":     "Cure Balm",        "emoji":    "\U0001F33F",  # herb
        "round_cd": 3, "rarity":  "rare",
        "effect":   "cleanse_heal", "magnitude": 0.10, "duration": 0,
        "description": "Clear debuffs + restore 10% max HP.",
    },
    "shock_bolt": {
        "name":     "Shock Bolt",       "emoji":    "\U000026A1",  # lightning
        "round_cd": 6, "rarity":  "rare",
        "effect":   "shock_attack", "magnitude": 0.60, "duration": 1,
        "description": "Hurl a 0.60 x ATK bolt; stun foe 1 turn.",
    },
    "phoenix_tear": {
        "name":     "Phoenix Tear",     "emoji":    "\U0001F525",  # fire
        "round_cd": 99, "rarity":  "epic",
        "effect":   "revive", "magnitude": 0.35, "duration": 0,
        "description": "Once per battle: revive at 35% HP on KO.",
    },
}


def battle_consumable(key: str) -> dict | None:
    """Look up a consumable by key, or None if unknown."""
    return BATTLE_CONSUMABLES.get(str(key or "").strip().lower())


# =============================================================================
# FPS animation (Buddy Battles expansion)
# =============================================================================
# Short edit-loop bursts used during attack / consumable / KO events.
# Edits per frame stay above Discord's "edit faster than X" rate-limit
# but well below the 5/2s ceiling. A single battle hard-caps total
# bursts so a stalled fight can't spam edits.

BATTLE_FRAME_INTERVAL_S: Final[float] = 0.18    # ~5.5 FPS -- snappy, well under Discord 5/2s edit ceiling
BATTLE_BURST_FRAMES: Final[int] = 4             # 4 frames per attack burst (~0.7s total)
BATTLE_MAX_BURSTS_PER_BATTLE: Final[int] = 30   # hard cap on edit loops


# =============================================================================
# Generic battle ASCII frames
# =============================================================================
# Used as a fallback when a species lacks a battle-specific frame in
# SPECIES[species]['frames']. Keyed off body-silhouette category;
# species can also override per-frame by adding e.g. 'attack' to their
# own frames dict. The 5 new frames extend the existing
# happy/neutral/hungry/sad/eating/petted/talking set.

BATTLE_FRAMES_GENERIC: Final[dict[str, str]] = {
    "attack": "\n".join([
        "     .---.      ",
        "    | > < |  >> ",
        "     \\\\\\\\/_      ",
        "      |X|       ",
        "     /   \\      ",
    ]),
    "hurt": "\n".join([
        "    .---.       ",
        "   | x _ |  !!  ",
        "    \\___/       ",
        "     | |        ",
        "    -   -       ",
    ]),
    "victory": "\n".join([
        "    .---.   *   ",
        "   | ^ ^ |  *   ",
        "    \\_-_/   *   ",
        "     |Y|        ",
        "    / | \\       ",
    ]),
    "down": "\n".join([
        "                ",
        "    .---.       ",
        "   | X X | zzz  ",
        "    \\___/       ",
        "    _____       ",
    ]),
    "using_item": "\n".join([
        "     .---.      ",
        "    | o o | (*) ",
        "     \\sip/      ",
        "      |U|       ",
        "      ^ ^       ",
    ]),
}


# =============================================================================
# Per-boss ASCII frames
# =============================================================================
# Boss-tamed buddies (cc_buddies.boss_zone_id set) need to read as
# visually distinct on the `,buddy` panel + battle log, not just on the
# Pillow portrait. Each boss zone gets a full frame set so they don't
# inherit the generic species art.
#
# Frame keys covered: neutral / happy / sad / hungry / eating / petted
# / talking + battle frames (attack / hurt / victory / down /
# using_item). Anything missing falls through battle_frame() to the
# species default.

BOSS_ASCII_FRAMES: Final[dict[str, dict[str, str]]] = {
    "plains_arena": {  # Meadow King -- crowned wolf
        "neutral": "\n".join([
            "    _MWM_       ",
            "   /( ^_^)\\     ",
            "   | \\___/ |    ",
            "    \\__|__/     ",
            "    /     \\     ",
        ]),
        "happy": "\n".join([
            "    _MWM_       ",
            "   /( ^o^)\\     ",
            "   | \\--/ |     ",
            "    \\__-_/      ",
            "    /| | |\\     ",
        ]),
        "attack": "\n".join([
            "   _MMWMM_  >>  ",
            "  /(>___<)\\     ",
            "  | \\WWW/ |     ",
            "  | =vVv= |     ",
            "   /__|__\\      ",
        ]),
        "hurt": "\n".join([
            "    _M_M_       ",
            "   /( x_x)\\     ",
            "   | \\-_/ |  !! ",
            "    \\__|__/     ",
            "    /  -  \\     ",
        ]),
        "victory": "\n".join([
            "  *_MWM_*       ",
            "   /( ^_^)\\     ",
            "   | \\^_^/ |    ",
            "    \\_VvV_/     ",
            "    /__|__\\     ",
        ]),
        "down": "\n".join([
            "                ",
            "    _MWM_       ",
            "   /( X_X)\\  zZz",
            "   | \\___/ |    ",
            "    _______     ",
        ]),
        "using_item": "\n".join([
            "    _MWM_       ",
            "   /( o_o)\\ (*) ",
            "   | \\sip/ |    ",
            "    \\__|__/     ",
            "    /     \\     ",
        ]),
    },
    "stone_colosseum": {  # Granite Champion -- horned boulder helm
        "neutral": "\n".join([
            "  /\\ ___ /\\     ",
            " /( [###]) \\    ",
            " \\__|^_^|__/    ",
            "    \\___/       ",
            "    /   \\       ",
        ]),
        "happy": "\n".join([
            "  /\\ ___ /\\     ",
            " /( [###]) \\    ",
            " \\__|^o^|__/    ",
            "    \\\\_//       ",
            "   _/   \\_      ",
        ]),
        "attack": "\n".join([
            "  /\\ ___ /\\  >> ",
            " /( [###]) \\    ",
            " \\__|>O<|__/=== ",
            "    \\=v=/       ",
            "   _/| |\\_      ",
        ]),
        "hurt": "\n".join([
            "  /\\ _x_ /\\     ",
            " /( [#X#]) \\ !! ",
            " \\__|x_x|__/    ",
            "    \\_-_/       ",
            "   _/   \\_      ",
        ]),
        "victory": "\n".join([
            "* /\\ ___ /\\ *   ",
            " /( [###]) \\    ",
            " \\__|^_^|__/    ",
            "    \\_v_/       ",
            "   _|<|>|_      ",
        ]),
        "down": "\n".join([
            "                ",
            "  /\\ _X_ /\\     ",
            " /( [###]) \\ z  ",
            " \\__|X_X|__/    ",
            "    _____       ",
        ]),
        "using_item": "\n".join([
            "  /\\ ___ /\\     ",
            " /( [###]) \\ (*)",
            " \\__|o_o|__/    ",
            "    \\sip/       ",
            "   _/   \\_      ",
        ]),
    },
    "tide_amphitheatre": {  # Tideborn Sovereign -- crowned crab
        "neutral": "\n".join([
            "    YYY YYY     ",
            "   ( o   o )    ",
            "  __\\ ===/__    ",
            " / [_O_O_]  \\   ",
            "  >  > <  <     ",
        ]),
        "happy": "\n".join([
            "    YYY YYY     ",
            "   ( ^   ^ )    ",
            "  __\\ uuu/__    ",
            " / [_O_O_]  \\   ",
            "  >  > <  <     ",
        ]),
        "attack": "\n".join([
            "    YYYWYYY  >> ",
            "  >( >   < )<   ",
            "  __\\ ===/__    ",
            " / [_O_O_]  \\   ",
            " >>  > <  <<    ",
        ]),
        "hurt": "\n".join([
            "    Y_Y Y_Y     ",
            "   ( x   x ) !! ",
            "  __\\ -_-/__    ",
            " / [_O_O_]  \\   ",
            "  =  = =  =     ",
        ]),
        "victory": "\n".join([
            "  * YYYWYYY *   ",
            "   ( ^   ^ )    ",
            "  __\\ vVv/__    ",
            " / [_O_O_]  \\   ",
            "  ^  ^ ^  ^     ",
        ]),
        "down": "\n".join([
            "                ",
            "    YYY YYY     ",
            "   ( X   X ) z  ",
            "  __\\ ___/__    ",
            "  =====v====    ",
        ]),
        "using_item": "\n".join([
            "    YYY YYY     ",
            "   ( o   o )(*) ",
            "  __\\sip/__     ",
            " / [_O_O_]  \\   ",
            "  >  > <  <     ",
        ]),
    },
    "druid_circle": {  # Bramble Druid -- antlered thornling
        "neutral": "\n".join([
            "   \\.Y_Y./      ",
            "    Y___Y       ",
            "   ( *_* )      ",
            "    \\_v_/       ",
            "   #/   \\#      ",
        ]),
        "happy": "\n".join([
            "   \\.Y_Y./      ",
            "    Y___Y       ",
            "   ( ^o^ )      ",
            "    \\_o_/       ",
            "   #/   \\#      ",
        ]),
        "attack": "\n".join([
            "   \\.YWY./   >> ",
            "    Y___Y       ",
            "   ( *_* )===   ",
            "    \\#v#/       ",
            "   #/| |\\#      ",
        ]),
        "hurt": "\n".join([
            "    \\Y_Y/       ",
            "    Y_x_Y    !! ",
            "   ( x_x )      ",
            "    \\___/       ",
            "   #/   \\#      ",
        ]),
        "victory": "\n".join([
            "  * \\.Y_Y./ *   ",
            "    Y###Y       ",
            "   ( ^_^ )      ",
            "    \\_v_/       ",
            "   #|#|#|#      ",
        ]),
        "down": "\n".join([
            "                ",
            "    \\Y_Y/       ",
            "    Y X Y    zZ ",
            "   ( X_X )      ",
            "    _____       ",
        ]),
        "using_item": "\n".join([
            "   \\.Y_Y./      ",
            "    Y___Y    (*)",
            "   ( o_o )      ",
            "    \\sip/       ",
            "   #/   \\#      ",
        ]),
    },
    "magma_caldera": {  # Caldera Tyrant -- flame-maned blazer
        "neutral": "\n".join([
            "  /\\/W\\/W\\/\\    ",
            "  / )^_^( \\     ",
            "  | \\___/ |     ",
            "   \\_____/      ",
            "   ~/   \\~      ",
        ]),
        "happy": "\n".join([
            "  /\\/W\\/W\\/\\    ",
            "  / )^o^( \\     ",
            "  | \\---/ |     ",
            "   \\___-/       ",
            "   ~/   \\~      ",
        ]),
        "attack": "\n".join([
            " /WW/W\\/W\\WW\\>> ",
            " / )>___<( \\===>",
            " | \\WWWWW/ |    ",
            "  \\=vVv=v/      ",
            "  ~/| | |\\~     ",
        ]),
        "hurt": "\n".join([
            "  /\\/_\\/_\\/\\    ",
            "  / )x_x( \\  !! ",
            "  | \\-_-/ |     ",
            "   \\_____/      ",
            "   ~/   \\~      ",
        ]),
        "victory": "\n".join([
            "* /\\/W\\/W\\/\\ *  ",
            "  / )^_^( \\     ",
            "  | \\^^^/ |     ",
            "   \\_vVv_/      ",
            "   ~|~|~|~      ",
        ]),
        "down": "\n".join([
            "                ",
            "  /\\/_\\/_\\/\\    ",
            "  / )X_X( \\  zZ ",
            "  | \\___/ |     ",
            "   ~~~~~~~      ",
        ]),
        "using_item": "\n".join([
            "  /\\/W\\/W\\/\\    ",
            "  / )o_o( \\ (*) ",
            "  | \\sip/ |     ",
            "   \\___-/       ",
            "   ~/   \\~      ",
        ]),
    },
}


def battle_frame(
    species: str, frame_key: str, *, boss_zone_id: str = "",
) -> str:
    """Return the ASCII frame for a species in the given battle key.

    Resolution order:
        1. BOSS_ASCII_FRAMES[boss_zone_id][frame_key] (if boss_zone_id set)
        2. SPECIES[species]['frames'][frame_key]
        3. BATTLE_FRAMES_GENERIC[frame_key]
        4. SPECIES[species]['frames']['neutral']
        5. ""

    Captured bosses (cc_buddies.boss_zone_id set) pull their unique
    crown / helm / antlers / flame mane ASCII so they read as visually
    distinct from a generic same-species buddy on the `,buddy` panel.
    """
    bzid = str(boss_zone_id or "").strip()
    if bzid:
        boss_frames = BOSS_ASCII_FRAMES.get(bzid) or {}
        if frame_key in boss_frames:
            return boss_frames[frame_key]
    spec = SPECIES.get(str(species or "").strip().lower(), {})
    frames = spec.get("frames", {}) if isinstance(spec, dict) else {}
    return (frames.get(frame_key)
            or BATTLE_FRAMES_GENERIC.get(frame_key)
            or frames.get("neutral")
            or "")
