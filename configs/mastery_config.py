"""V3 Pillar 2: Apex Mastery node + track catalogue.

Declarative. Adding a new node is one dict entry. Adding a new track
is one entry in ``TRACKS`` -- the XP / level math is shared.

Tracks emit mastery XP from existing minigame cogs (single-line
``services.mastery.add_mastery(uid, gid, track, xp)`` calls at the
end of fishing catches / farming harvests / dungeon clears / etc.).
Hitting a level threshold grants a mastery point. Points are spent
on the node tree; node effects apply via ``services.mastery.passive``.
"""
from __future__ import annotations


# ── Tracks ─────────────────────────────────────────────────────────────
# Each track caps at TRACK_MAX_LEVEL. XP per level follows a smooth
# ``growth^n`` curve so early levels feel achievable and high levels
# are a real commitment.
#
# Player report tuning the curve down a notch: with the old 1.15^n
# curve + 100 base XP and the previous /5 (/10) XP grant divisor a
# single Full Protocol Heist was minting L20+ in one action and a
# regular play session was producing 28 mastery points -- "Raider L27
# I don't even know what the hell that is ... played the game once".
#
# Bumped base 100 -> 300 (3x) and growth 1.15 -> 1.22 (steeper) so
# mid-tier requires real commitment while the cap stays aspirational.
# Sample milestones under the new curve, paired with the per-action
# XP cap (1500 XP in services/mastery.xp_for_action):
#   L5   ->      1,656 XP    (~     1 capped action)
#   L10  ->      6,797 XP    (~     4)
#   L20  ->     58,267 XP    (~    38)
#   L27  ->    238,537 XP    (~   159)
#   L50  ->     23.2M XP     (way beyond a single session of any kind)
#   L100 ->  483.4B XP       (intentionally unreachable on human time)
TRACK_MAX_LEVEL: int = 100
TRACK_BASE_XP: int = 300
TRACK_XP_GROWTH: float = 1.22

# Branches the skill tree splits into. Used by the renderer to colour
# nodes consistently across the board.
BRANCHES = ("economy", "combat", "luck", "utility")


# Player-facing descriptions for each branch. Surfaced by
# ``,mastery branches`` and the main ``,mastery`` board's legend.
BRANCH_INFO: dict[str, dict] = {
    "economy": {
        "label": "Economy",
        "emoji": "\U0001F4B0",
        "tagline": "Stack more cash on every passive income line.",
        "what_it_does": (
            "Boost daily / work payouts, savings APR, LP yield, and trim "
            "auction-house fees. Pure money multipliers."
        ),
    },
    "combat": {
        "label": "Combat",
        "emoji": "⚔️",
        "tagline": "Hit harder, tank deeper, win more PvP rolls.",
        "what_it_does": (
            "Dungeon damage, exploit defence, gamba payout, buddy battle "
            "damage. The 'I want to win fights' branch."
        ),
    },
    "luck": {
        "label": "Luck",
        "emoji": "\U0001F340",
        "tagline": "Tilt every random roll your way.",
        "what_it_does": (
            "Legendary catch rate, crop double chance, dungeon loot, drop "
            "pickup bonuses, gamba streaks. RNG smoothing."
        ),
    },
    "utility": {
        "label": "Utility",
        "emoji": "\U0001F9F0",
        "tagline": "Spend less time waiting; squeeze more from every action.",
        "what_it_does": (
            "Cooldown cuts, expedition speed, crafting speed, mining "
            "hashrate, LP auto-claim. Quality-of-life force-multipliers."
        ),
    },
}


TRACKS: dict[str, dict] = {
    "fisher": {
        "label": "Fisher",
        "emoji": "\U0001F41F",
        "xp_source": "Every `,fish` cast pays a flat XP grant, with bonus XP on rare catches.",
        "synergy": "Pairs with Luck branch nodes (Sharp Eye, Treasure Hunter).",
    },
    "farmer": {
        "label": "Farmer",
        "emoji": "\U0001F33E",
        "xp_source": "Every `,farm harvest` and pest-battle win grants XP.",
        "synergy": "Bounty Field (Luck) doubles harvested crops at 8%.",
    },
    "delver": {
        "label": "Delver",
        "emoji": "\U0001F5E1",
        "xp_source": "Each `,delve` floor cleared and boss kill grants XP scaling with depth.",
        "synergy": "Sharp Edge (Combat) and Treasure Hunter (Luck) both apply.",
    },
    "trader": {
        "label": "Trader",
        "emoji": "\U0001F4C8",
        "xp_source": "Every `,buy` / `,sell` / `,swap` grants XP proportional to volume.",
        "synergy": "Auction House Discount and Liquid Crown (Economy).",
    },
    "gambler": {
        "label": "Gambler",
        "emoji": "\U0001F3B0",
        "xp_source": "Every gamba round grants XP; wagering bigger pays more XP.",
        "synergy": "Lucky Streak (Combat) and Hot Hand (Luck) stack on payouts.",
    },
    "raider": {
        "label": "Raider",
        "emoji": "\U0001F3F4",
        "xp_source": "Each `,eat` run grants XP; successfully eating the rich grants a bonus.",
        "synergy": "Iron Firewall (defence) and Pack Leader (buddy battle).",
    },
    "tamer": {
        "label": "Tamer",
        "emoji": "\U0001F436",
        "xp_source": "Every `,buddy feed` / `,buddy battle` / `,buddy hatch` grants XP.",
        "synergy": "Pack Leader (Combat) directly buffs buddy damage.",
    },
    "validator": {
        "label": "Validator",
        "emoji": "⚖️",
        "xp_source": "Vault deposits, governance votes, and validator-guard usage grant XP.",
        "synergy": "Compounding (Economy) and Liquid Crown amplify validator yield.",
    },
    "crafter": {
        "label": "Crafter",
        "emoji": "\U0001F528",
        "xp_source": "Each `,craft` build grants XP scaling with tier and material cost.",
        "synergy": "Forge Master (Utility) trims 15% off crafting time.",
    },
    "sage_scholar": {
        "label": "Scholar",
        "emoji": "\U0001F4DA",
        "xp_source": "Each finished `,pattern`/`,gauge`/`,tknom` run grants XP proportional to the run's score.",
        "synergy": "Trader synergies (Liquid Crown, Auction House Discount) compound with Sage cashout payouts.",
    },
}


def xp_for_level(level: int) -> int:
    """Cumulative XP required to reach ``level``.

    Level 1 = 0 XP, level 2 = TRACK_BASE_XP, level n = sum of geometric
    series. Pure function so the renderer and ``add_mastery`` agree.
    """
    if level <= 1:
        return 0
    total = 0
    cur = TRACK_BASE_XP
    for _ in range(level - 1):
        total += int(cur)
        cur *= TRACK_XP_GROWTH
    return total


def level_for_xp(xp: int) -> int:
    """Inverse of ``xp_for_level``. Returns the highest level <= xp."""
    if xp <= 0:
        return 1
    cum = 0
    cur = TRACK_BASE_XP
    for lvl in range(2, TRACK_MAX_LEVEL + 1):
        cum += int(cur)
        if cum > xp:
            return lvl - 1
        cur *= TRACK_XP_GROWTH
    return TRACK_MAX_LEVEL


def points_for_level(level: int) -> int:
    """Mastery points awarded for being AT ``level``.

    Linear-ish curve: 1 point per level with a milestone bonus at
    multiples of 10. Means a player at L50 has roughly 55 points.
    """
    if level <= 1:
        return 0
    base = level - 1
    milestones = level // 10
    return base + milestones


# ── Node tree ──────────────────────────────────────────────────────────
# Each node:
#   id             - stable string id
#   name           - display label
#   branch         - one of BRANCHES (drives colour)
#   cost           - points
#   prereqs        - list of node ids that must be unlocked first
#   effect_key     - dot-path used by services.mastery.passive() readers
#   effect_value   - magnitude (interpretation is per-effect)
#   description    - shown on the renderer + ,mastery info
#
# Effects are additive across nodes that share a key, except where the
# reader explicitly multiplies (e.g. gamba payout). Each consumer
# reads its key once and clamps via its own sanity rule.
NODES: list[dict] = [
    # ─ Economy branch ─────────────────────────────────────────────────
    {"id": "econ.daily_bonus.1",  "name": "Reliable Returns I",
     "branch": "economy", "cost": 1, "prereqs": [],
     "effect_key": "econ.daily_bonus", "effect_value": 0.05,
     "description": "+5% on `,daily` payouts."},
    {"id": "econ.daily_bonus.2",  "name": "Reliable Returns II",
     "branch": "economy", "cost": 2, "prereqs": ["econ.daily_bonus.1"],
     "effect_key": "econ.daily_bonus", "effect_value": 0.10,
     "description": "+10% on `,daily` payouts (stacks)."},
    {"id": "econ.interest_bonus.1", "name": "Compounding I",
     "branch": "economy", "cost": 2, "prereqs": [],
     "effect_key": "econ.interest_bonus", "effect_value": 0.10,
     "description": "+10% savings APR."},
    {"id": "econ.lp_yield_bonus.1", "name": "Liquid Crown",
     "branch": "economy", "cost": 3, "prereqs": ["econ.interest_bonus.1"],
     "effect_key": "econ.lp_yield_bonus", "effect_value": 0.15,
     "description": "+15% LP yield payouts."},
    {"id": "econ.auction_fee_cut", "name": "House Discount",
     "branch": "economy", "cost": 2, "prereqs": [],
     "effect_key": "econ.auction_fee_cut", "effect_value": 0.25,
     "description": "Cut auction-house fees by 25%."},

    # ─ Combat branch ──────────────────────────────────────────────────
    {"id": "combat.dungeon_dmg.1", "name": "Sharp Edge I",
     "branch": "combat", "cost": 2, "prereqs": [],
     "effect_key": "combat.dungeon_dmg", "effect_value": 0.05,
     "description": "+5% damage on `,delve` attacks."},
    {"id": "combat.dungeon_dmg.2", "name": "Sharp Edge II",
     "branch": "combat", "cost": 3, "prereqs": ["combat.dungeon_dmg.1"],
     "effect_key": "combat.dungeon_dmg", "effect_value": 0.10,
     "description": "+10% damage on `,delve` (stacks)."},
    {"id": "combat.exploit_def.1", "name": "Iron Firewall I",
     "branch": "combat", "cost": 2, "prereqs": [],
     "effect_key": "combat.exploit_def", "effect_value": 0.15,
     "description": "+15% defence roll against players trying to eat you."},
    {"id": "combat.gamba_payout", "name": "Lucky Streak",
     "branch": "combat", "cost": 3, "prereqs": [],
     "effect_key": "combat.gamba_payout", "effect_value": 0.10,
     "description": "+10% gamba payouts on wins."},
    {"id": "combat.buddy_dmg", "name": "Pack Leader",
     "branch": "combat", "cost": 3, "prereqs": ["combat.exploit_def.1"],
     "effect_key": "combat.buddy_dmg", "effect_value": 0.10,
     "description": "+10% buddy battle damage."},

    # ─ Luck branch ────────────────────────────────────────────────────
    {"id": "luck.rare_catch", "name": "Sharp Eye",
     "branch": "luck", "cost": 2, "prereqs": [],
     "effect_key": "luck.rare_catch", "effect_value": 0.10,
     "description": "+10% legendary fish rate."},
    {"id": "luck.crop_double", "name": "Bounty Field",
     "branch": "luck", "cost": 3, "prereqs": [],
     "effect_key": "luck.crop_double", "effect_value": 0.08,
     "description": "8% chance a harvested crop doubles."},
    {"id": "luck.dungeon_loot", "name": "Treasure Hunter",
     "branch": "luck", "cost": 3, "prereqs": ["luck.rare_catch"],
     "effect_key": "luck.dungeon_loot", "effect_value": 0.15,
     "description": "+15% chance of rare drops in `,delve`."},
    {"id": "luck.drop_pickup", "name": "Magnet Hands",
     "branch": "luck", "cost": 2, "prereqs": [],
     "effect_key": "luck.drop_pickup", "effect_value": 0.20,
     "description": "+20% pickup bonus on `,drop` claims."},
    {"id": "luck.gamba_streak", "name": "Hot Hand",
     "branch": "luck", "cost": 4, "prereqs": ["luck.crop_double"],
     "effect_key": "luck.gamba_streak", "effect_value": 0.05,
     "description": "+5% chance of a streak bonus on gamba wins."},

    # ─ Utility branch ─────────────────────────────────────────────────
    {"id": "utility.expedition_speed", "name": "Fast March",
     "branch": "utility", "cost": 2, "prereqs": [],
     "effect_key": "utility.expedition_speed", "effect_value": 0.10,
     "description": "Expeditions finish 10% sooner."},
    {"id": "utility.cooldown_cut", "name": "Brisk Pace",
     "branch": "utility", "cost": 3, "prereqs": [],
     "effect_key": "utility.cooldown_cut", "effect_value": 0.10,
     "description": "Trim 10% off `,work` / `,daily` / `,fish` cooldowns."},
    {"id": "utility.crafting_speed", "name": "Forge Master",
     "branch": "utility", "cost": 3, "prereqs": ["utility.cooldown_cut"],
     "effect_key": "utility.crafting_speed", "effect_value": 0.15,
     "description": "Crafting is 15% faster."},
    {"id": "utility.mining_hashrate", "name": "Cooled Rigs",
     "branch": "utility", "cost": 3, "prereqs": [],
     "effect_key": "utility.mining_hashrate", "effect_value": 0.10,
     "description": "+10% mining hashrate."},
    {"id": "utility.lp_autoclaim", "name": "Auto Skim",
     "branch": "utility", "cost": 4, "prereqs": ["utility.crafting_speed"],
     "effect_key": "utility.lp_autoclaim", "effect_value": 1.0,
     "description": "LP yield auto-claims to wallet once a day."},

    # ── Buddy Battles expansion nodes ─────────────────────────────────
    # 18 nodes spanning all 4 branches. Added with the buddy arena map
    # / tournament / battle-consumables expansion so the metagame has
    # progression to chase past the original 20 nodes. Effect keys are
    # read at runtime by ``services.mastery.apply_passive`` and consumed
    # across earn / bank / shop / validators / buddy / crafting /
    # breeding / etc.
    #
    # Economy
    {"id": "econ.bank_yield",        "name": "Vault Interest",
     "branch": "economy", "cost": 3, "prereqs": [],
     "effect_key": "econ.bank_yield",        "effect_value": 0.08,
     "description": "+8% on `,bank deposit` interest accrual."},
    {"id": "econ.shop_discount",     "name": "Loyal Customer",
     "branch": "economy", "cost": 3, "prereqs": ["econ.daily_bonus.1"],
     "effect_key": "econ.shop_discount",     "effect_value": 0.05,
     "description": "5% off `,shop buy` prices (clamped at 50%)."},
    {"id": "econ.work_bonus",        "name": "Overtime Hustle",
     "branch": "economy", "cost": 2, "prereqs": [],
     "effect_key": "econ.work_bonus",        "effect_value": 0.10,
     "description": "+10% on `,work` payouts."},
    {"id": "econ.validator_yield",   "name": "Block Royalties",
     "branch": "economy", "cost": 4, "prereqs": ["econ.lp_yield_bonus.1"],
     "effect_key": "econ.validator_yield",   "effect_value": 0.12,
     "description": "+12% validator block reward share."},

    # Combat (heavy on buddy / tournament since this is the expansion)
    {"id": "combat.buddy_dmg.2",     "name": "Pack Leader II",
     "branch": "combat", "cost": 4, "prereqs": ["combat.buddy_dmg"],
     "effect_key": "combat.buddy_dmg",       "effect_value": 0.10,
     "description": "+10% buddy battle damage (stacks with I)."},
    {"id": "combat.tourney_xp",      "name": "Arena Veteran",
     "branch": "combat", "cost": 3, "prereqs": ["combat.buddy_dmg"],
     "effect_key": "combat.tourney_xp",      "effect_value": 0.25,
     "description": "+25% XP from arena and tournament wins."},
    {"id": "combat.consumable_cd",   "name": "Quickdraw",
     "branch": "combat", "cost": 3, "prereqs": [],
     "effect_key": "combat.consumable_cd",   "effect_value": 1,
     "description": "Battle consumables come off CD 1 round sooner."},
    {"id": "combat.zone_travel",     "name": "Trailblazer",
     "branch": "combat", "cost": 2, "prereqs": [],
     "effect_key": "combat.zone_travel",     "effect_value": 1,
     "description": "Travel +1 zone per `,buddy travel` step."},
    {"id": "combat.extra_slot",      "name": "Wingbuddy",
     "branch": "combat", "cost": 5, "prereqs": ["combat.buddy_dmg.2"],
     "effect_key": "combat.extra_slot",      "effect_value": 1,
     "description": "+1 battle slot -- bring a second buddy into arena."},

    # Luck
    {"id": "luck.zone_drops",        "name": "Spoils of War",
     "branch": "luck", "cost": 3, "prereqs": [],
     "effect_key": "luck.zone_drops",        "effect_value": 0.15,
     "description": "+15% chance for a consumable drop on zone clear."},
    {"id": "luck.craft_double",      "name": "Twin Forge",
     "branch": "luck", "cost": 4, "prereqs": ["luck.crop_double"],
     "effect_key": "luck.craft_double",      "effect_value": 0.10,
     "description": "10% chance a craft outputs a bonus item."},
    {"id": "luck.egg_rarity",        "name": "Golden Hatch",
     "branch": "luck", "cost": 4, "prereqs": ["luck.rare_catch"],
     "effect_key": "luck.egg_rarity",        "effect_value": 0.08,
     "description": "+8% chance daycare eggs roll one rarity higher."},

    # Utility
    {"id": "utility.feed_efficiency","name": "Hearty Meals",
     "branch": "utility", "cost": 2, "prereqs": [],
     "effect_key": "utility.feed_efficiency","effect_value": 0.20,
     "description": "Feeding restores +20% extra hunger."},
    {"id": "utility.tx_fee_cut",     "name": "Light Touch",
     "branch": "utility", "cost": 3, "prereqs": [],
     "effect_key": "utility.tx_fee_cut",     "effect_value": 0.15,
     "description": "Cut wallet transfer / move fees 15%."},
    {"id": "utility.expedition_loot","name": "Heavy Pockets",
     "branch": "utility", "cost": 3, "prereqs": ["utility.expedition_speed"],
     "effect_key": "utility.expedition_loot","effect_value": 0.20,
     "description": "+20% loot from buddy expeditions."},
    {"id": "utility.chat_xp",        "name": "Charisma Bonus",
     "branch": "utility", "cost": 2, "prereqs": [],
     "effect_key": "utility.chat_xp",        "effect_value": 0.15,
     "description": "+15% chat XP gain for your active buddy."},
    {"id": "utility.gas_cut",        "name": "Slipstream Trades",
     "branch": "utility", "cost": 4, "prereqs": ["utility.tx_fee_cut"],
     "effect_key": "utility.gas_cut",        "effect_value": 0.20,
     "description": "Cut trade gas 20%."},
    {"id": "utility.daycare_speed",  "name": "Warm Nest",
     "branch": "utility", "cost": 3, "prereqs": [],
     "effect_key": "utility.daycare_speed",  "effect_value": 0.15,
     "description": "Eggs hatch 15% sooner."},
]


NODES_BY_ID: dict[str, dict] = {n["id"]: n for n in NODES}
TOTAL_POINTS_REQUIRED: int = sum(n["cost"] for n in NODES)
