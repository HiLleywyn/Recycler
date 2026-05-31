"""
services/lexicon.py  -  Item lexicon source resolver.

Maps a contract address (kind + catalog_key) to a human-readable
"how to get it" string. Pulls from the same catalog dicts the NFT
bootstrap walks at startup so each entry surfaces real per-item
detail (which fishing zones for this fish, which recipe inputs for
this craft, which delve floor unlocks this weapon, ...).

Public API:
    source_lines(contract_row) -> list[str]
        Returns 1-N short lines describing how players can obtain
        the item this contract represents. Used by the ``,db``
        detail view.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Catalog loaders. Cached on first hit so repeated ,db calls don't re-import.
# Each loader returns the catalog dict or {} on failure (a missing config
# never breaks the lexicon -- the generic per-kind copy still renders).
# ---------------------------------------------------------------------------

_CATALOGS: dict[str, dict] = {}


def _catalog(name: str) -> dict:
    if name in _CATALOGS:
        return _CATALOGS[name]
    out: dict = {}
    try:
        if name == "FISH":
            from configs.fishing_config import FISH as _D
            out = dict(_D or {})
        elif name == "BAIT":
            from configs.fishing_config import BAIT as _D
            out = dict(_D or {})
        elif name == "JUNK":
            from configs.fishing_config import JUNK as _D
            out = dict(_D or {})
        elif name == "CROPS":
            from configs.farming_config import CROPS as _D
            out = dict(_D or {})
        elif name == "WEAPONS":
            from configs.dungeon_config import WEAPONS as _D
            out = dict(_D or {})
        elif name == "ARMOR":
            from configs.dungeon_config import ARMOR as _D
            out = dict(_D or {})
        elif name == "CONSUMABLES":
            from configs.dungeon_config import CONSUMABLES as _D
            out = dict(_D or {})
        elif name == "CRAFT_ITEMS":
            from configs.crafting_config import CRAFT_ITEMS as _D
            out = dict(_D or {})
        elif name == "SPECIES":
            from configs.buddies_config import SPECIES as _D
            out = dict(_D or {})
        elif name == "SHOP_ITEMS":
            from configs.items_config import SHOP_ITEMS as _D
            out = dict(_D or {})
    except Exception:
        log.exception("lexicon: catalog %s import failed", name)
        out = {}
    _CATALOGS[name] = out
    return out


def _fmt_zones(zones: Any) -> str:
    if not zones:
        return "any zone"
    try:
        names = [str(z).replace("_", " ").title() for z in zones]
    except Exception:
        return "any zone"
    return ", ".join(names) or "any zone"


def _crafted_recipes_for(target: str) -> list[tuple[str, dict]]:
    """Return crafted recipes that produce inputs ending in ``target``.

    Used by fish / crop / ore source lines to point at downstream uses
    ("worm bait drops 2 in the iron_pickaxe_oil recipe").
    """
    target = (target or "").lower()
    out: list[tuple[str, dict]] = []
    for key, meta in _catalog("CRAFT_ITEMS").items():
        inputs = (meta or {}).get("inputs") or {}
        for ing_path, qty in inputs.items():
            if str(ing_path).lower().endswith(target):
                out.append((str(key), dict(meta)))
                break
    return out


# ---------------------------------------------------------------------------
# Per-kind handlers. Each returns a list of short lines (no leading dash --
# the cog adds those).
# ---------------------------------------------------------------------------


def _src_buddy(_meta: dict, key: str) -> list[str]:
    spec = _catalog("SPECIES").get(key) or {}
    weight = int(spec.get("weight") or 0)
    rarity_part = (
        f" (species draw weight: {weight})" if weight else ""
    )
    lines: list[str] = []
    if spec.get("tagline"):
        lines.append(f"*{spec['tagline']}*")
    lines.extend([
        f"Hatch a held egg of this species via `,buddy hatch`{rarity_part}.",
        "Direct hatch from a `,fish` cast on the buddy_egg outcome.",
        "Capture wild ones from `,fish wild` or `,delve` mob fights.",
        "Adopt from `,buddy shelter` (surrendered buddies are free).",
        "Auction house: `,ah browse buddy`.",
    ])
    if spec.get("ability_name") and spec.get("ability_desc"):
        lines.append(
            f"Ability: **{spec['ability_name']}** -- {spec['ability_desc']}"
        )
    return lines


def _src_egg(_meta: dict, key: str) -> list[str]:
    spec = _catalog("SPECIES").get(key) or {}
    name = spec.get("name") or key.title()
    return [
        f"Roll a **{name}** egg from `,fish` casts (~5% chance, daily cap).",
        "Drop from `,delve` mob kills (rare).",
        "Collect from `,buddy nest collect` once incubation completes.",
        "Hatches into a buddy with gender + rarity rolled at hatch time.",
        "Auction house: `,ah browse egg`.",
    ]


def _src_fish(_meta: dict, key: str) -> list[str]:
    cat = _catalog("FISH").get(key) or {}
    if not cat:
        return [
            "Cast `,fish` -- bait + zone determine which species roll.",
            "Sell with `,fish sell` for LURE proportional to weight.",
            "Auction house: `,ah browse fish`.",
        ]
    zones = _fmt_zones(cat.get("zones"))
    rod_tier = int(cat.get("min_rod_tier") or 0)
    rod_part = (
        f" -- requires rod tier **{rod_tier}+**"
        if rod_tier > 0 else ""
    )
    rarity = str(cat.get("rarity") or "").title()
    lines = [
        f"Cast `,fish` in **{zones}**{rod_part}.",
        (
            f"Rolls between **{float(cat.get('min_lbs') or 0):.1f}** and "
            f"**{float(cat.get('max_lbs') or 0):.1f}** lbs."
        ),
        (
            f"Sell with `,fish sell` at "
            f"**{float(cat.get('base_lure') or 0):,.1f} LURE / lb** "
            f"(rarity multiplier {rarity})."
        ),
    ]
    used_in = _crafted_recipes_for(f"fish/{key}")
    if used_in:
        names = ", ".join(
            (m.get("name") or k).strip() for k, m in used_in[:3]
        )
        more = (
            f" (+{len(used_in) - 3} more)"
            if len(used_in) > 3 else ""
        )
        lines.append(f"Used in `,craft`: {names}{more}.")
    lines.append("Auction house: `,ah browse fish`.")
    return lines


def _src_bait(_meta: dict, key: str) -> list[str]:
    cat = _catalog("BAIT").get(key) or {}
    lines: list[str] = []
    if cat.get("price_reel"):
        lines.append(
            f"Buy from `,fish shop bait` at "
            f"**{float(cat['price_reel']):,.1f} REEL** each."
        )
    fish_b = float(cat.get("fish_bonus") or 0.0)
    rare_b = float(cat.get("rare_bonus") or 0.0)
    bonus_b = float(cat.get("bonus_bonus") or 0.0)
    if fish_b or rare_b or bonus_b:
        bits = []
        if fish_b:
            bits.append(f"+{fish_b * 100:.0f}% fish")
        if rare_b:
            bits.append(f"+{rare_b * 100:.0f}% rare pulls")
        if bonus_b:
            bits.append(f"+{bonus_b * 100:.0f}% bonus rolls")
        lines.append("Effects: " + " · ".join(bits) + ".")
    used_in = _crafted_recipes_for(f"bait/{key}")
    if used_in:
        names = ", ".join(
            (m.get("name") or k).strip() for k, m in used_in[:2]
        )
        lines.append(f"Crafted by recipes: {names}.")
    lines.append("Auction house: `,ah browse bait`.")
    return lines


def _src_junk(_meta: dict, key: str) -> list[str]:
    cat = _catalog("JUNK").get(key) or {}
    salvage = float(cat.get("salvage_lure") or 0.0)
    lines = [
        "Pulled from `,fish` casts as the consolation outcome (any zone).",
    ]
    if salvage > 0:
        lines.append(
            f"Sells for **{salvage:.2f} LURE** via `,fish sell`."
        )
    lines.append("Auction house: `,ah browse junk`.")
    return lines


def _src_crop(_meta: dict, key: str) -> list[str]:
    cat = _catalog("CROPS").get(key) or {}
    if not cat:
        return [
            "Plant the matching seed via `,farm plant`, harvest via `,farm`.",
            "Sell with `,farm sell` for HRV.",
            "Auction house: `,ah browse crop`.",
        ]
    rarity = str(cat.get("rarity") or "").title() or "Common"
    season = str(cat.get("season") or "").title()
    zone_t = int(cat.get("zone_tier") or 0)
    grow_s = int(cat.get("growth_seconds") or 0)
    secs = (
        f"{grow_s // 60}m" if grow_s and grow_s % 60 == 0
        else f"{grow_s}s" if grow_s else "?"
    )
    season_part = f", **{season}** crop" if season else ""
    lines = [
        f"Plant the **{key.title()}** seed in `,farm` -- {rarity} crop{season_part}.",
        (
            f"Yields **{int(cat.get('base_yield_min') or 0)}-"
            f"{int(cat.get('base_yield_max') or 0)}** units in "
            f"**{secs}** (zone tier {zone_t})."
        ),
        (
            f"Sells for **{float(cat.get('hrv_sell_price') or 0):.2f} HRV** "
            f"each via `,farm sell`."
        ),
    ]
    used_in = _crafted_recipes_for(f"crop/{key}")
    if used_in:
        names = ", ".join(
            (m.get("name") or k).strip() for k, m in used_in[:3]
        )
        more = (
            f" (+{len(used_in) - 3} more)"
            if len(used_in) > 3 else ""
        )
        lines.append(f"Used in `,craft`: {names}{more}.")
    lines.append("Auction house: `,ah browse crop`.")
    return lines


def _src_weapon(_meta: dict, key: str) -> list[str]:
    cat = _catalog("WEAPONS").get(key) or {}
    if not cat:
        return [
            "Buy from `,delve shop weapons` -- spent in RUNE.",
            "Equip with `,delve equip weapon <key>`.",
            "Auction house: `,ah browse weapon`.",
        ]
    tier = int(cat.get("tier") or 0)
    atk = int(cat.get("atk_bonus") or 0)
    price = float(cat.get("price_rune") or 0.0)
    lines = [
        f"Tier **T{tier}** weapon -- **+{atk} ATK** when equipped.",
    ]
    if price > 0:
        lines.append(
            f"Buy from `,delve shop weapons` for **{price:,.0f} RUNE**."
        )
    lines.append(f"Equip with `,delve equip weapon {key}`.")
    used_in = _crafted_recipes_for(f"weapon/{key}")
    if used_in:
        names = ", ".join(
            (m.get("name") or k).strip() for k, m in used_in[:2]
        )
        lines.append(f"Crafted by: {names}.")
    lines.append("Auction house: `,ah browse weapon`.")
    return lines


def _src_armor(_meta: dict, key: str) -> list[str]:
    cat = _catalog("ARMOR").get(key) or {}
    if not cat:
        return [
            "Buy from `,delve shop armor` -- spent in RUNE.",
            "Equip with `,delve equip armor <key>`.",
            "Auction house: `,ah browse armor`.",
        ]
    tier = int(cat.get("tier") or 0)
    deff = int(cat.get("def_bonus") or 0)
    price = float(cat.get("price_rune") or 0.0)
    lines = [
        f"Tier **T{tier}** armor -- **+{deff} DEF** when equipped.",
    ]
    if price > 0:
        lines.append(
            f"Buy from `,delve shop armor` for **{price:,.0f} RUNE**."
        )
    lines.append(f"Equip with `,delve equip armor {key}`.")
    lines.append("Auction house: `,ah browse armor`.")
    return lines


def _src_consumable(_meta: dict, key: str) -> list[str]:
    cat = _catalog("CONSUMABLES").get(key) or {}
    kind = str(cat.get("kind") or "").lower()
    value = float(cat.get("value") or 0.0)
    price = float(cat.get("price_rune") or 0.0)
    blurb = str(cat.get("blurb") or "")
    lines: list[str] = []
    if blurb:
        lines.append(f"*{blurb}*")
    if kind:
        eff = ""
        if kind == "heal":
            eff = f"Restores **{value * 100:.0f}%** of max HP."
        elif kind == "mine_boost":
            eff = f"Next `,delve mine` yields **+{value * 100:.0f}%** ore."
        elif kind == "charm":
            eff = f"Boosts the next capture attempt by **+{value * 100:.0f}%**."
        elif kind == "lure":
            eff = f"**{value * 100:.0f}%** chance the next room spawns a bonus mob."
        elif kind == "revive":
            eff = f"Auto-revives at **{value * 100:.0f}%** HP on KO."
        elif kind == "escape":
            eff = "Walks away from any combat unharmed."
        elif kind == "damage":
            eff = f"Spell scroll: deals **{value:.1f}x ATK** to active mob."
        if eff:
            lines.append(eff)
    if price > 0:
        lines.append(
            f"Buy from `,delve shop consumables` for **{price:,.0f} RUNE**."
        )
    lines.append(f"Use with `,delve use {key}`.")
    crafted_by = _crafted_recipes_for(f"consum/{key}")
    if crafted_by:
        names = ", ".join(
            (m.get("name") or k).strip() for k, m in crafted_by[:2]
        )
        lines.append(f"Crafted by: {names}.")
    lines.append("Auction house: `,ah browse consumable`.")
    return lines


def _src_crafted(_meta: dict, key: str) -> list[str]:
    cat = _catalog("CRAFT_ITEMS").get(key) or {}
    if not cat:
        return [
            f"Craft via `,craft {key}` once you have the inputs.",
            "Apply the result with `,craft apply <key>`.",
        ]
    inputs = cat.get("inputs") or {}
    fgd = float(cat.get("fgd_cost") or 0.0)
    spec = str(cat.get("specialty") or "").title()
    locked = bool(cat.get("requires_specialty"))
    min_lvl = int(cat.get("min_level") or 0)
    apply_t = str(cat.get("apply") or "")
    blurb = str(cat.get("blurb") or "")
    lines: list[str] = []
    if blurb:
        lines.append(f"*{blurb}*")
    spec_part = (
        f" (**{spec}** specialty"
        + (", locked" if locked else "")
        + ")"
        if spec else ""
    )
    lvl_part = f" -- min level **{min_lvl}**" if min_lvl else ""
    lines.append(
        f"Craft via `,craft {key}`{spec_part}{lvl_part}."
    )
    if inputs:
        ing = ", ".join(
            f"{int(qty)}× `{path}`"
            for path, qty in list(inputs.items())[:6]
        )
        lines.append(f"Inputs: {ing}.")
    if fgd > 0:
        lines.append(f"Crafting fee: **{fgd:,.1f} FGD** burned per craft.")
    if apply_t:
        lines.append(f"On `,craft apply {key}`: routes to `{apply_t}`.")
    lines.append("Auction house: `,ah browse crafted`.")
    return lines


def _src_ore(_meta: dict, key: str) -> list[str]:
    sym = key.upper()
    return [
        f"Mine `,delve` ore rooms -- **{sym}** drops scale with floor depth.",
        f"Stake via `,delve stake {sym}` for daily yields, or trade against "
        f"USD on the AMM at `,trade {sym}`.",
        f"Burn-swap into RUNE via `,delve burn {sym}` for dungeon currency.",
        "Note: ore is fungible (lives in your wallet), not a per-unit NFT.",
    ]


def _src_stone(_meta: dict, key: str) -> list[str]:
    cat = _catalog("SHOP_ITEMS").get(key) or {}
    name = cat.get("name") or key.title()
    blurb = str(cat.get("blurb") or "")
    lines: list[str] = []
    if blurb:
        lines.append(f"*{blurb}*")
    lines.extend([
        f"Buy from `,shop buy {key}` -- staked in DSD/USDC.",
        f"Level up via `,shop levelup {key}` to scale **{name}** bonuses.",
        f"Sell back any time via `,shop sell {key}` (returns most staked).",
    ])
    return lines


def _src_shop(_meta: dict, key: str) -> list[str]:
    cat = _catalog("SHOP_ITEMS").get(key) or {}
    blurb = str(cat.get("blurb") or "")
    lines: list[str] = []
    if blurb:
        lines.append(f"*{blurb}*")
    lines.extend([
        f"Buy from `,shop buy {key}` -- consumable, paid in any stablecoin.",
        "Activated automatically on the next eligible event.",
    ])
    return lines


_SOURCE_HANDLERS = {
    "buddy":      _src_buddy,
    "egg":        _src_egg,
    "fish":       _src_fish,
    "bait":       _src_bait,
    "junk":       _src_junk,
    "crop":       _src_crop,
    "weapon":     _src_weapon,
    "armor":      _src_armor,
    "consumable": _src_consumable,
    "crafted":    _src_crafted,
    "ore":        _src_ore,
    "stone":      _src_stone,
    "shop":       _src_shop,
}


def source_lines(contract_row: dict) -> list[str]:
    """Return acquisition lines for one contract row.

    ``contract_row`` is a dict-like with keys ``kind``, ``catalog_key``,
    ``metadata`` (already parsed). Falls back to a generic
    "auction house" hint when the kind doesn't have a dedicated
    handler.
    """
    if not contract_row:
        return ["Acquisition source unknown."]
    kind = str(contract_row.get("kind") or "").lower()
    key = str(contract_row.get("catalog_key") or "").lower()
    meta = contract_row.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json as _json
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    handler = _SOURCE_HANDLERS.get(kind)
    if not handler:
        return [
            "Auction house: `,ah browse` for live listings.",
            "No catalog source registered yet for this kind.",
        ]
    try:
        return list(handler(meta, key))
    except Exception:
        log.exception("lexicon source_lines failed kind=%s key=%s", kind, key)
        return ["Acquisition source unavailable."]
