"""buddy_gear_config.py -- wearable items for cc_buddies.

Two equipment slots per buddy:
  accessory  -- visible cosmetic (shows in ASCII panel as badge)
  charm      -- passive stat bonus (applies in expedition + battle)

Items can be crafted (via crafting_config recipes) or bought from the
shop. Equipped gear is stored in cc_buddies.gear JSONB:
  {"accessory": "flower_crown", "charm": "lucky_bell"}

Slot is cleared by equipping the same item again or using ,buddy gear unequip.

Starter shop tiers
------------------
A subset of items carries a ``starter_tier`` (1 / 2 / 3) flag. Those are
the basic kit sold for DSD via ``,buddy gear shop`` -- weaker than the
crafted gear but always available, no recipes required. Tier prices
double per tier so ladder progression mirrors a stable difficulty curve:
  Tier 1 -- $1,000   (Apprentice line, +2% stat)
  Tier 2 -- $5,000   (Initiate    line, +4% stat)
  Tier 3 -- $25,000  (Adept       line, +6% stat)
Only items with starter_tier set show up in the shop browse view.
"""
from __future__ import annotations

from typing import Final


# ── Gear item catalog ─────────────────────────────────────────────────────────
# Each entry has:
#   slot          -- "accessory" or "charm"
#   name          -- display name
#   emoji         -- shown in embed + inventory
#   blurb         -- one-line description
#   stat_bonus    -- dict of bonus keys applied in battle/expedition (or {})
#   craft_key     -- matching key in crafting_config.CRAFT_ITEMS (if craftable)
#   shop_cost_dsd -- DSD cost if sold in shop (None = craft-only)

BUDDY_GEAR: Final[dict[str, dict]] = {
    # ── Accessories (cosmetic, visible in embed) ──────────────────────────────
    "flower_crown": {
        "slot": "accessory",
        "name": "Flower Crown",
        "emoji": "\U0001F33C",
        "blurb": "A woven ring of wild blooms. Very photogenic.",
        "stat_bonus": {},
        "craft_key": "flower_crown_craft",
        "shop_cost_dsd": None,
    },
    "star_badge": {
        "slot": "accessory",
        "name": "Star Badge",
        "emoji": "\U00002B50",
        "blurb": "A polished metal badge. Shows everyone who's boss.",
        "stat_bonus": {},
        "craft_key": "star_badge_craft",
        "shop_cost_dsd": 25.0,
    },
    "golden_collar": {
        "slot": "accessory",
        "name": "Golden Collar",
        "emoji": "\U0001F451",
        "blurb": "Flashy. Heavy. Your buddy wears it with dignity.",
        "stat_bonus": {},
        "craft_key": "golden_collar_craft",
        "shop_cost_dsd": None,
    },
    "ribbon_bow": {
        "slot": "accessory",
        "name": "Ribbon Bow",
        "emoji": "\U0001F380",
        "blurb": "Silky ribbon tied in a perfect bow. Adorable.",
        "stat_bonus": {},
        "craft_key": None,
        "shop_cost_dsd": 15.0,
    },
    "silken_ribbon": {
        "slot": "accessory",
        "name": "Silken Ribbon",
        "emoji": "\U0001F397",
        "blurb": "Hand-loomed silk band. Catches every breeze.",
        "stat_bonus": {},
        "craft_key": "silken_ribbon_craft",
        "shop_cost_dsd": None,
    },
    "mosaic_scarf": {
        "slot": "accessory",
        "name": "Mosaic Scarf",
        "emoji": "\U0001F9E3",
        "blurb": "Patchwork of every season's harvest. No two are alike.",
        "stat_bonus": {},
        "craft_key": "mosaic_scarf_craft",
        "shop_cost_dsd": None,
    },
    "forgemaster_band": {
        "slot": "accessory",
        "name": "Forgemaster Band",
        "emoji": "\U0001F9E1",
        "blurb": "An ember-warm headband stamped with the forge sigil. Forge-sealed.",
        "stat_bonus": {},
        "craft_key": "forgemaster_band_craft",
        "shop_cost_dsd": None,
    },

    # ── Charms (passive bonuses, apply in battle + expedition) ────────────────
    "lucky_bell": {
        "slot": "charm",
        "name": "Lucky Bell",
        "emoji": "\U0001F514",
        "blurb": "+5% expedition loot qty. Rings with every step.",
        "stat_bonus": {"expedition_loot_pct": 0.05},
        "craft_key": "lucky_bell_craft",
        "shop_cost_dsd": None,
    },
    "battle_charm": {
        "slot": "charm",
        "name": "Battle Charm",
        "emoji": "\U00002694",
        "blurb": "+8% ATK in buddy battles.",
        "stat_bonus": {"atk_pct": 0.08},
        "craft_key": "battle_charm_craft",
        "shop_cost_dsd": None,
    },
    "vitality_stone": {
        "slot": "charm",
        "name": "Vitality Stone",
        "emoji": "\U0001F9E1",
        "blurb": "+10% max HP in buddy battles.",
        "stat_bonus": {"hp_pct": 0.10},
        "craft_key": "vitality_stone_craft",
        "shop_cost_dsd": None,
    },
    "growth_charm": {
        "slot": "charm",
        "name": "Growth Charm",
        "emoji": "\U0001F331",
        "blurb": "+10% XP gain from all sources.",
        "stat_bonus": {"xp_pct": 0.10},
        "craft_key": "growth_charm_craft",
        "shop_cost_dsd": 80.0,
    },
    "void_amulet": {
        "slot": "charm",
        "name": "Void Amulet",
        "emoji": "\U0001F300",
        "blurb": "+15% loot in Void Rift expeditions. Hums faintly.",
        "stat_bonus": {"void_loot_pct": 0.15},
        "craft_key": "void_amulet_craft",
        "shop_cost_dsd": None,
    },
    "iron_band": {
        "slot": "charm",
        "name": "Iron Band",
        "emoji": "\U0001FA84",
        "blurb": "+5% max HP in buddy battles. A starter's strap.",
        "stat_bonus": {"hp_pct": 0.05},
        "craft_key": "iron_band_craft",
        "shop_cost_dsd": None,
    },
    "mossy_amulet": {
        "slot": "charm",
        "name": "Mossy Amulet",
        "emoji": "\U0001F33F",
        "blurb": "+5% XP from all sources. Lichen-bound, still growing.",
        "stat_bonus": {"xp_pct": 0.05},
        "craft_key": "mossy_amulet_craft",
        "shop_cost_dsd": None,
    },
    "sunbeam_locket": {
        "slot": "charm",
        "name": "Sunbeam Locket",
        "emoji": "\U0001F506",
        "blurb": "+10% expedition loot qty. Catches a sliver of every dawn.",
        "stat_bonus": {"expedition_loot_pct": 0.10},
        "craft_key": "sunbeam_locket_craft",
        "shop_cost_dsd": None,
    },
    "tempest_amulet": {
        "slot": "charm",
        "name": "Tempest Amulet",
        "emoji": "\U000026C8",
        "blurb": "+12% ATK in buddy battles. Crackles when the buddy roars.",
        "stat_bonus": {"atk_pct": 0.12},
        "craft_key": "tempest_amulet_craft",
        "shop_cost_dsd": None,
    },
    "forge_seal_pendant": {
        "slot": "charm",
        "name": "Forge-Seal Pendant",
        "emoji": "\U0001F525",
        "blurb": "+8% ATK and +8% max HP. Forge-sealed; stamp never cools.",
        "stat_bonus": {"atk_pct": 0.08, "hp_pct": 0.08},
        "craft_key": "forge_seal_pendant_craft",
        "shop_cost_dsd": None,
    },

    # ── Extended accessory line (cosmetic, no stat bonus) ─────────────────────
    "tiny_top_hat": {
        "slot": "accessory",
        "name": "Tiny Top Hat",
        "emoji": "\U0001F3A9",
        "blurb": "Distinguished. Practical. Marginally too small.",
        "stat_bonus": {},
        "craft_key": None,
        "shop_cost_dsd": 30.0,
    },
    "scarf": {
        "slot": "accessory",
        "name": "Cozy Scarf",
        "emoji": "\U0001F9E3",
        "blurb": "Knitted with love. Or guilt. Hard to tell.",
        "stat_bonus": {},
        "craft_key": "scarf_craft",
        "shop_cost_dsd": 12.0,
    },
    "tiny_glasses": {
        "slot": "accessory",
        "name": "Tiny Glasses",
        "emoji": "\U0001F453",
        "blurb": "Reading glasses for a buddy who reads above their pay grade.",
        "stat_bonus": {},
        "craft_key": None,
        "shop_cost_dsd": 18.0,
    },
    "moon_pendant": {
        "slot": "accessory",
        "name": "Moon Pendant",
        "emoji": "\U0001F319",
        "blurb": "Glows faintly at night. Sells well at moonlit auctions.",
        "stat_bonus": {},
        "craft_key": "moon_pendant_craft",
        "shop_cost_dsd": None,
    },

    # ── Extended charm line (passive battle / expedition bonuses) ─────────────
    "swiftness_charm": {
        "slot": "charm",
        "name": "Swiftness Charm",
        "emoji": "\U0001F4A8",
        "blurb": "+0.05 SPD in buddy battles. Gusty.",
        "stat_bonus": {"spd_flat": 0.05},
        "craft_key": "swiftness_charm_craft",
        "shop_cost_dsd": None,
    },
    "crit_focus": {
        "slot": "charm",
        "name": "Crit Focus",
        "emoji": "\U0001F3AF",
        "blurb": "+5% crit chance in buddy battles.",
        "stat_bonus": {"crit_chance_pct": 0.05},
        "craft_key": "crit_focus_craft",
        "shop_cost_dsd": None,
    },
    "warding_charm": {
        "slot": "charm",
        "name": "Warding Charm",
        "emoji": "\U0001F6E1",
        "blurb": "Takes 7% less damage in buddy battles.",
        "stat_bonus": {"dr_pct": 0.07},
        "craft_key": "warding_charm_craft",
        "shop_cost_dsd": None,
    },
    "lifesteal_charm": {
        "slot": "charm",
        "name": "Bloodthorn Charm",
        "emoji": "\U0001FA78",
        "blurb": "Heal 5% of damage dealt in buddy battles.",
        "stat_bonus": {"lifesteal_pct": 0.05},
        "craft_key": "lifesteal_charm_craft",
        "shop_cost_dsd": None,
    },
    "regen_charm": {
        "slot": "charm",
        "name": "Bloomheart Pendant",
        "emoji": "\U0001F33F",
        "blurb": "+1% max HP regen per round (soft-capped at 75% HP).",
        "stat_bonus": {"regen_pct": 0.01},
        "craft_key": "regen_charm_craft",
        "shop_cost_dsd": 60.0,
    },
    "stamina_charm": {
        "slot": "charm",
        "name": "Endurance Charm",
        "emoji": "\U0001F947",
        "blurb": "Buddy starts interactive battles with +1 stamina.",
        "stat_bonus": {"start_stamina": 1},
        "craft_key": "stamina_charm_craft",
        "shop_cost_dsd": 45.0,
    },
    "ward_aegis": {
        "slot": "charm",
        "name": "Aegis of Ward",
        "emoji": "\U0001F6E0",
        "blurb": "Reflects 5% of every incoming hit. Stacks with shells.",
        "stat_bonus": {"reflect_pct": 0.05},
        "craft_key": "ward_aegis_craft",
        "shop_cost_dsd": None,
    },
    "treasure_compass": {
        "slot": "charm",
        "name": "Treasure Compass",
        "emoji": "\U0001F9ED",
        "blurb": "+10% gold from delve expeditions. Always points down.",
        "stat_bonus": {"delve_gold_pct": 0.10},
        "craft_key": "treasure_compass_craft",
        "shop_cost_dsd": 75.0,
    },
    "harvest_amulet": {
        "slot": "charm",
        "name": "Harvest Amulet",
        "emoji": "\U0001F33E",
        "blurb": "+12% loot on forest / farm expeditions.",
        "stat_bonus": {"farm_loot_pct": 0.12},
        "craft_key": "harvest_amulet_craft",
        "shop_cost_dsd": None,
    },
    "tide_pearl": {
        "slot": "charm",
        "name": "Tide Pearl",
        "emoji": "\U0001F30A",
        "blurb": "+12% loot on reef expeditions. Smells like the sea.",
        "stat_bonus": {"reef_loot_pct": 0.12},
        "craft_key": "tide_pearl_craft",
        "shop_cost_dsd": None,
    },

    # ── Starter gear (DSD-priced, sold via ,buddy gear shop) ──────────────────
    # Three tiers of basic kit. Magnitudes are deliberately weaker than the
    # crafted items above so a player who finds a forge / void / battle
    # charm still has a reason to graduate off the starter ladder. Each
    # carries a ``starter_tier`` so the shop browse view can group them.

    # Tier 1 -- Apprentice ($1,000 each, +2% stat)
    "apprentice_collar": {
        "slot": "accessory",
        "name": "Apprentice Collar",
        "emoji": "\U0001F539",
        "blurb": "Plain leather. The buddy looks like they mean business.",
        "stat_bonus": {},
        "craft_key": None,
        "shop_cost_dsd": 200.0,
        "starter_tier": 1,
    },
    "apprentice_band": {
        "slot": "charm",
        "name": "Apprentice Band",
        "emoji": "\U0001F94A",
        "blurb": "+2% ATK in buddy battles. Stiff, but it does the job.",
        "stat_bonus": {"atk_pct": 0.02},
        "craft_key": None,
        "shop_cost_dsd": 1000.0,
        "starter_tier": 1,
    },
    "apprentice_pad": {
        "slot": "charm",
        "name": "Apprentice Pad",
        "emoji": "\U0001F9F6",
        "blurb": "+2% max HP in buddy battles. Padded. Mildly comforting.",
        "stat_bonus": {"hp_pct": 0.02},
        "craft_key": None,
        "shop_cost_dsd": 1000.0,
        "starter_tier": 1,
    },
    "apprentice_sash": {
        "slot": "charm",
        "name": "Apprentice Sash",
        "emoji": "\U0001F3F1",
        "blurb": "+0.02 SPD in buddy battles. Lightweight cotton sash.",
        "stat_bonus": {"spd_flat": 0.02},
        "craft_key": None,
        "shop_cost_dsd": 1000.0,
        "starter_tier": 1,
    },

    # Tier 2 -- Initiate ($5,000 each, +4% stat)
    "initiate_pin": {
        "slot": "accessory",
        "name": "Initiate Pin",
        "emoji": "\U0001F4CC",
        "blurb": "A small enamel pin. Marks a buddy who's seen a few fights.",
        "stat_bonus": {},
        "craft_key": None,
        "shop_cost_dsd": 1500.0,
        "starter_tier": 2,
    },
    "initiate_amulet": {
        "slot": "charm",
        "name": "Initiate Amulet",
        "emoji": "\U0001F4FF",
        "blurb": "+4% ATK in buddy battles. Polished bronze, lightly engraved.",
        "stat_bonus": {"atk_pct": 0.04},
        "craft_key": None,
        "shop_cost_dsd": 5000.0,
        "starter_tier": 2,
    },
    "initiate_brooch": {
        "slot": "charm",
        "name": "Initiate Brooch",
        "emoji": "\U0001F49A",
        "blurb": "+4% max HP in buddy battles. Holds a chip of jade.",
        "stat_bonus": {"hp_pct": 0.04},
        "craft_key": None,
        "shop_cost_dsd": 5000.0,
        "starter_tier": 2,
    },
    "initiate_anklet": {
        "slot": "charm",
        "name": "Initiate Anklet",
        "emoji": "\U0001F47E",
        "blurb": "+0.03 SPD in buddy battles. Jingles when the buddy bolts.",
        "stat_bonus": {"spd_flat": 0.03},
        "craft_key": None,
        "shop_cost_dsd": 5000.0,
        "starter_tier": 2,
    },

    # Tier 3 -- Adept ($25,000 each, +6% stat or focused effect)
    "adept_medallion": {
        "slot": "accessory",
        "name": "Adept Medallion",
        "emoji": "\U0001F947",
        "blurb": "Gold-plated medallion. The buddy is officially trained.",
        "stat_bonus": {},
        "craft_key": None,
        "shop_cost_dsd": 8000.0,
        "starter_tier": 3,
    },
    "adept_talisman": {
        "slot": "charm",
        "name": "Adept Talisman",
        "emoji": "\U0001F525",
        "blurb": "+6% ATK in buddy battles. Warm to the touch.",
        "stat_bonus": {"atk_pct": 0.06},
        "craft_key": None,
        "shop_cost_dsd": 25000.0,
        "starter_tier": 3,
    },
    "adept_aegis": {
        "slot": "charm",
        "name": "Adept Aegis",
        "emoji": "\U0001F6E1",
        "blurb": "+6% max HP and -3% damage taken. A solid all-rounder.",
        "stat_bonus": {"hp_pct": 0.06, "dr_pct": 0.03},
        "craft_key": None,
        "shop_cost_dsd": 25000.0,
        "starter_tier": 3,
    },
    "adept_focus": {
        "slot": "charm",
        "name": "Adept Focus",
        "emoji": "\U0001F4A0",
        "blurb": "+3% crit chance in buddy battles. Cut from a clear quartz.",
        "stat_bonus": {"crit_chance_pct": 0.03},
        "craft_key": None,
        "shop_cost_dsd": 25000.0,
        "starter_tier": 3,
    },
}


# Convenience: ordered tier label table for the shop view.
STARTER_TIER_LABELS: Final[dict[int, str]] = {
    1: "Apprentice (Tier 1)",
    2: "Initiate (Tier 2)",
    3: "Adept (Tier 3)",
}


def starter_gear_by_tier() -> dict[int, list[tuple[str, dict]]]:
    """Return ``{tier: [(key, meta), ...]}`` for every starter-flagged
    item, ordered by tier ascending and then by key. Used by the
    ``,buddy gear shop`` browse view.
    """
    out: dict[int, list[tuple[str, dict]]] = {1: [], 2: [], 3: []}
    for key, meta in BUDDY_GEAR.items():
        tier = int(meta.get("starter_tier") or 0)
        if tier <= 0:
            continue
        out.setdefault(tier, []).append((key, meta))
    for tier in out:
        out[tier].sort(key=lambda kv: kv[0])
    return out


def gear_meta(key: str) -> dict | None:
    return BUDDY_GEAR.get((key or "").lower())


def gear_display(gear: dict) -> str:
    """Format the equipped gear dict for the buddy panel embed field."""
    if not gear:
        return "_(none)_"
    lines = []
    for slot in ("accessory", "charm"):
        item_key = gear.get(slot)
        if not item_key:
            continue
        meta = BUDDY_GEAR.get(str(item_key) or "")
        if meta:
            lines.append(
                f"{meta['emoji']} **{meta['name']}** ({slot}) -- {meta['blurb']}"
            )
        else:
            lines.append(f"\U0001F4E6 `{item_key}` ({slot})")
    return "\n".join(lines) if lines else "_(none)_"


__all__ = [
    "BUDDY_GEAR",
    "STARTER_TIER_LABELS",
    "gear_meta",
    "gear_display",
    "starter_gear_by_tier",
]
