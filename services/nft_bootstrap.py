"""
services/nft_bootstrap.py  -  Catalog-walking contract deploy.

Phase 1 of the per-unit NFT layer. Walks every catalog dict in the
config files (fishing_config.BAIT, dungeon_config.WEAPONS, etc.) and
calls :func:`services.items.upsert_contract` for each entry, so adding
a new item to a config file automatically "deploys" its contract on
the next bot boot.

Idempotent: safe to call on every startup. Existing contract rows get
a metadata refresh (display name / emoji / base price) but their
contract_id and address stay stable.

Public API:
    deploy_all_contracts(db) -> dict[str, int]
        Returns a count-per-kind summary of how many contracts were
        ensured. Used by main.py during the post-migration startup
        phase.
"""
from __future__ import annotations

import logging
from typing import Any

from core.framework.scale import to_raw
from services import items as _items

log = logging.getLogger(__name__)


# Default per-kind network fallback when the catalog doesn't specify
# one. Matches services.items.KIND_NETWORK_DEFAULTS plus the new kinds
# (bait / junk / shop / stone) that aren't represented in the original
# auction-house surface.
_KIND_NETWORK = {
    "buddy":      "bud",
    "egg":        "bud",
    "fish":       "lur",
    "bait":       "lur",
    "junk":       "lur",
    "crop":       "har",
    "ore":        "cry",
    "weapon":     "cry",
    "armor":      "cry",
    "consumable": "cry",
    "crafted":    "fge",
    "stone":      "dsc",
    "shop":       "dsc",
    "token":      "fge",
}


async def _deploy_one(
    db: Any,
    *,
    kind: str,
    catalog_key: str,
    name: str,
    network: str | None = None,
    rarity_tier: int | None = None,
    base_price_usd: float | None = None,
    base_price_native: float | None = None,
    base_price_currency: str | None = None,
    emoji: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Wrapper around items.upsert_contract that converts catalog prices
    to raw scaled ints.

    ``base_price_usd`` lands in the legacy USD column (used by shop /
    stone, where the catalog already quotes in stable). ``base_price_native``
    + ``base_price_currency`` carry the network-token price every other
    catalog uses (REEL for bait, RUNE for weapons, FGD for crafted, ...).
    """
    base_raw = (
        int(to_raw(float(base_price_usd)))
        if base_price_usd is not None and float(base_price_usd) > 0
        else None
    )
    native_raw = (
        int(to_raw(float(base_price_native)))
        if base_price_native is not None and float(base_price_native) > 0
        else None
    )
    cur = (base_price_currency or "").upper() or None
    if native_raw is None:
        cur = None      # no point keeping a currency with no amount
    await _items.upsert_contract(
        db,
        kind=kind,
        catalog_key=str(catalog_key),
        name=str(name),
        network=network or _KIND_NETWORK.get(kind, "fge"),
        rarity_tier=rarity_tier,
        base_price_raw=base_raw,
        base_price_native_raw=native_raw,
        base_price_currency=cur,
        emoji=emoji,
        metadata=metadata,
    )


async def _deploy_buddies(db: Any) -> int:
    """One contract per buddy species. Buddies share a 'buddy' kind but
    each species is its own contract (so an Iggy buddy and a Wecco
    buddy have separate token addresses and separate unit counters).
    """
    n = 0
    try:
        from configs.buddies_config import SPECIES
    except Exception:
        log.exception("nft_bootstrap: buddies_config import failed")
        return 0
    for key, meta in (SPECIES or {}).items():
        try:
            await _deploy_one(
                db,
                kind="buddy",
                catalog_key=str(key),
                name=str((meta or {}).get("name") or str(key).title()),
                emoji=str((meta or {}).get("emoji") or ""),
                metadata={"catalog": "buddies_config.SPECIES"},
            )
            n += 1
        except Exception as e:
            log.error("deploy_buddies failed for %s: %r", key, e, exc_info=True)
    return n


async def _deploy_eggs(db: Any) -> int:
    """One contract per fishable buddy species (eggs are the pre-hatch
    form). Sharing the species key with buddies is intentional -- the
    address differs by kind prefix (``buddy.zenny`` vs ``egg.zenny``).
    """
    n = 0
    try:
        from configs.buddies_config import SPECIES
    except Exception:
        return 0
    for key, meta in (SPECIES or {}).items():
        try:
            await _deploy_one(
                db,
                kind="egg",
                catalog_key=str(key),
                name=f"{str((meta or {}).get('name') or str(key).title())} Egg",
                emoji=str((meta or {}).get("emoji") or ""),
                metadata={"catalog": "buddies_config.SPECIES"},
            )
            n += 1
        except Exception as e:
            log.error("deploy_eggs failed for %s: %r", key, e, exc_info=True)
    return n


async def _deploy_fish_bait_junk(db: Any) -> tuple[int, int, int]:
    """Fish / bait / junk contracts come from fishing_config.

    FISH contracts are per-species (the 'lbs' lives in token metadata at
    mint time). BAIT contracts are per-bait-type. JUNK contracts cover
    the consolation pulls (boots, cans, etc.) so they're tradable too.
    """
    fn = bn = jn = 0
    try:
        from configs.fishing_config import BAIT, FISH, JUNK
    except Exception:
        log.exception("nft_bootstrap: fishing_config import failed")
        return (0, 0, 0)
    _RARITY_TIER = {
        "common":    1,
        "uncommon":  2,
        "rare":      3,
        "epic":      4,
        "legendary": 5,
    }
    for key, meta in (FISH or {}).items():
        try:
            rt = (
                int((meta or {}).get("rarity_tier") or 0)
                or _RARITY_TIER.get(
                    str((meta or {}).get("rarity") or "").lower()
                )
                or None
            )
            # ``base_lure`` is a per-pound rate -- not a per-unit price --
            # so we stash it as catalog metadata for display rather than as
            # a base price (price varies with the token's lbs at mint).
            await _deploy_one(
                db, kind="fish", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                rarity_tier=rt,
                metadata={
                    "catalog":      "fishing_config.FISH",
                    "base_lure_per_lb": float(
                        (meta or {}).get("base_lure") or 0
                    ) or None,
                },
            )
            fn += 1
        except Exception as e:
            log.error("deploy_fish failed for %s: %r", key, e, exc_info=True)
    for key, meta in (BAIT or {}).items():
        try:
            await _deploy_one(
                db, kind="bait", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                base_price_native=(
                    float((meta or {}).get("price_reel") or 0) or None
                ),
                base_price_currency="REEL",
                metadata={"catalog": "fishing_config.BAIT"},
            )
            bn += 1
        except Exception as e:
            log.error("deploy_bait failed for %s: %r", key, e, exc_info=True)
    for key, meta in (JUNK or {}).items():
        try:
            await _deploy_one(
                db, kind="junk", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                metadata={"catalog": "fishing_config.JUNK"},
            )
            jn += 1
        except Exception as e:
            log.error("deploy_junk failed for %s: %r", key, e, exc_info=True)
    return fn, bn, jn


async def _deploy_crops(db: Any) -> int:
    n = 0
    try:
        from configs.farming_config import CROPS
    except Exception:
        log.exception("nft_bootstrap: farming_config import failed")
        return 0
    _RARITY_TIER = {
        "common":    1,
        "uncommon":  2,
        "rare":      3,
        "epic":      4,
        "legendary": 5,
    }
    for key, meta in (CROPS or {}).items():
        try:
            rt = _RARITY_TIER.get(
                str((meta or {}).get("rarity") or "").lower()
            )
            await _deploy_one(
                db, kind="crop", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                rarity_tier=rt,
                base_price_native=(
                    float((meta or {}).get("hrv_sell_price") or 0) or None
                ),
                base_price_currency="HRV",
                metadata={"catalog": "farming_config.CROPS"},
            )
            n += 1
        except Exception as e:
            log.error("deploy_crops failed for %s: %r", key, e, exc_info=True)
    return n


async def _deploy_dungeon_gear(db: Any) -> tuple[int, int, int, int, int, int]:
    """Weapons, armor, consumables, junk (mats / salvage), and relics
    from dungeon_config.

    Ore (COPPER/SILVER/GOLD) is intentionally NOT deployed -- ore is a
    fungible token that lives in wallet_holdings with oracle prices,
    stake yields, and AMM trades. Per-unit NFT semantics break the
    fractional-amount + economic-currency model. See CHANGELOG for
    the design call.

    Dungeon JUNK (mats / salvage) and RELICS were missed in the
    original bootstrap, so items like ``enchanted_thread`` and
    ``miners_charm`` had no contract and couldn't be listed on the
    auction house. Both are now deployed alongside weapons / armor
    / consumables.
    """
    wn = an = cn = jn = rn = 0
    try:
        from configs.dungeon_config import ARMOR, CONSUMABLES, WEAPONS, JUNK, RELICS
    except Exception:
        log.exception("nft_bootstrap: dungeon_config import failed")
        return (0, 0, 0, 0, 0, 0)
    for key, meta in (WEAPONS or {}).items():
        try:
            await _deploy_one(
                db, kind="weapon", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                rarity_tier=int((meta or {}).get("rarity_tier") or 0) or None,
                base_price_native=(
                    float((meta or {}).get("price_rune") or 0) or None
                ),
                base_price_currency="RUNE",
                metadata={"catalog": "dungeon_config.WEAPONS"},
            )
            wn += 1
        except Exception as e:
            log.error("deploy_weapons failed for %s: %r", key, e, exc_info=True)
    for key, meta in (ARMOR or {}).items():
        try:
            await _deploy_one(
                db, kind="armor", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                rarity_tier=int((meta or {}).get("rarity_tier") or 0) or None,
                base_price_native=(
                    float((meta or {}).get("price_rune") or 0) or None
                ),
                base_price_currency="RUNE",
                metadata={"catalog": "dungeon_config.ARMOR"},
            )
            an += 1
        except Exception as e:
            log.error("deploy_armor failed for %s: %r", key, e, exc_info=True)
    for key, meta in (CONSUMABLES or {}).items():
        try:
            await _deploy_one(
                db, kind="consumable", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                base_price_native=(
                    float((meta or {}).get("price_rune") or 0) or None
                ),
                base_price_currency="RUNE",
                metadata={"catalog": "dungeon_config.CONSUMABLES"},
            )
            cn += 1
        except Exception as e:
            log.error("deploy_consumables failed for %s: %r", key, e, exc_info=True)
    # Dungeon junk: mats / salvage / event scraps that drop from combat
    # and chest opens. Salvage value lives on the catalog as
    # ``salvage_rune``; that's a per-unit RUNE base price for AH listings.
    _RARITY_TIER = {
        "common": 1, "uncommon": 2, "rare": 3, "epic": 4, "legendary": 5,
    }
    for key, meta in (JUNK or {}).items():
        try:
            await _deploy_one(
                db, kind="junk", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                rarity_tier=_RARITY_TIER.get(
                    str((meta or {}).get("rarity") or "").lower()
                ),
                base_price_native=(
                    float((meta or {}).get("salvage_rune") or 0) or None
                ),
                base_price_currency="RUNE",
                network="cry",
                metadata={"catalog": "dungeon_config.JUNK"},
            )
            jn += 1
        except Exception as e:
            log.error("deploy_dungeon_junk failed for %s: %r", key, e, exc_info=True)
    # Dungeon relics: rarity-tiered passive-effect items dropped from
    # chests / shrines / scavenges. No catalog price -- relic value is
    # fluid and player-driven, so listings always require an explicit
    # asking price (no auto-suggested floor).
    for key, meta in (RELICS or {}).items():
        try:
            await _deploy_one(
                db, kind="relic", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                rarity_tier=_RARITY_TIER.get(
                    str((meta or {}).get("rarity") or "").lower()
                ),
                network="cry",
                metadata={"catalog": "dungeon_config.RELICS"},
            )
            rn += 1
        except Exception as e:
            log.error("deploy_relics failed for %s: %r", key, e, exc_info=True)
    # Ore stays fungible (wallet_holdings + oracle prices). 0 deployed.
    return wn, an, cn, 0, jn, rn


async def _deploy_crafted(db: Any) -> int:
    n = 0
    try:
        from configs.crafting_config import CRAFT_ITEMS
    except Exception:
        log.exception("nft_bootstrap: crafting_config import failed")
        return 0
    # Crafting recipes encode rarity as a string ("rare" / "legendary" / ...).
    _RARITY_TIER = {
        "common":    1,
        "uncommon":  2,
        "rare":      3,
        "epic":      4,
        "legendary": 5,
    }
    for key, meta in (CRAFT_ITEMS or {}).items():
        try:
            rt_raw = (meta or {}).get("rarity_tier") or _RARITY_TIER.get(
                str((meta or {}).get("rarity") or "").lower()
            )
            await _deploy_one(
                db, kind="crafted", catalog_key=key,
                name=str((meta or {}).get("name") or key.title()),
                emoji=str((meta or {}).get("emoji") or ""),
                rarity_tier=int(rt_raw) if rt_raw else None,
                base_price_native=(
                    float((meta or {}).get("fgd_cost") or 0) or None
                ),
                base_price_currency="FGD",
                metadata={"catalog": "crafting_config.CRAFT_ITEMS"},
            )
            n += 1
        except Exception as e:
            log.error("deploy_crafted failed for %s: %r", key, e, exc_info=True)
    return n


async def _deploy_shop(db: Any) -> int:
    """Shop items (charms / saves / guards / stones / etc.). One
    contract per SHOP_ITEMS entry. Stones get a separate ``stone``
    kind so the level/xp metadata can attach distinctly.
    """
    n = 0
    try:
        from configs.items_config import SHOP_ITEMS
    except Exception:
        log.exception("nft_bootstrap: items_config import failed")
        return 0
    for key, meta in (SHOP_ITEMS or {}).items():
        try:
            kind = (
                "stone" if str((meta or {}).get("table") or "").endswith("stones")
                else "shop"
            )
            # cost_stable in items_config is ALREADY raw-scaled (10^18),
            # not a USD float. Pass it straight through to upsert_contract
            # as base_price_raw -- _deploy_one would double-scale it via
            # to_raw and overflow NUMERIC(36, 0).
            cost_raw_in = (meta or {}).get("cost_stable")
            try:
                base_raw = int(cost_raw_in) if cost_raw_in else None
            except (TypeError, ValueError):
                base_raw = None
            await _items.upsert_contract(
                db,
                kind=kind,
                catalog_key=str(key),
                name=str((meta or {}).get("name") or key.title()),
                network=_KIND_NETWORK.get(kind, "fge"),
                rarity_tier=None,
                base_price_raw=base_raw,
                emoji=str((meta or {}).get("emoji") or "") or None,
                metadata={"catalog": "items_config.SHOP_ITEMS"},
            )
            n += 1
        except Exception as e:
            log.error("deploy_shop failed for %s: %r", key, e, exc_info=True)
    return n


async def deploy_all_contracts(db: Any) -> dict[str, int]:
    """Walk every catalog and deploy / refresh every contract.

    Idempotent. Returns a per-kind count summary. Logs (and skips) any
    individual failures so a malformed catalog entry never blocks the
    rest of the deploy. Designed to be called once during bot startup
    after migrations have run.
    """
    summary: dict[str, int] = {}
    summary["buddy"] = await _deploy_buddies(db)
    summary["egg"] = await _deploy_eggs(db)
    fn, bn, jn = await _deploy_fish_bait_junk(db)
    summary["fish"] = fn
    summary["bait"] = bn
    summary["junk"] = jn
    summary["crop"] = await _deploy_crops(db)
    wn, an, cn, on, jdn, rn = await _deploy_dungeon_gear(db)
    summary["weapon"] = wn
    summary["armor"] = an
    summary["consumable"] = cn
    summary["ore"] = on
    summary["dungeon_junk"] = jdn
    summary["relic"] = rn
    summary["crafted"] = await _deploy_crafted(db)
    summary["shop_or_stone"] = await _deploy_shop(db)
    log.info("nft_bootstrap: deployed contracts %s", summary)
    return summary
