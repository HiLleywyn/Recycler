"""
services/auction.py  -  Generic auction house.

Replaces the buddy-only market with a single listings table that takes
any item kind: buddies, eggs, fish, crops, ore, weapons, armors,
consumables, crafted items, fungible tokens. Each listing references
an ``item_instances`` row (the NFT-style token id layer in
:mod:`services.items`) so the AH browse / buy / cancel flow only ever
needs to know token_id + price + currency.

Pricing rules (from the spec):
    * Sticker price + currency are set by the seller. Currency defaults
      to the item's home network coin (BUD for buddies, REEL for fish,
      RUNE for delve gear, INGOT for crafted items, HRV for crops).
    * Direct buy in the listed currency: buyer transfers price -> seller
      receives ``price * (1 - auction_fee_bps / 10_000)``. The fee is
      burned as a sink.
    * Cross-currency buy (buyer pays in a different token): buyer pays
      the equivalent amount via the existing AMM swap path so the
      slippage / impact lands the same way ``,buy`` / ``,sell`` /
      ``,trade swap`` already work.

Public API (top-level):
    create_listing(...)       -> (listing_id, token_id, msg)
    buy_listing(...)          -> SaleResult
    cancel_listing(...)       -> (ok, msg)
    browse_active(...)        -> list[dict]
    list_user_listings(...)   -> list[dict]
    expire_old(...)           -> int  (background sweep)
    find_listing(...)         -> dict | None
"""
from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass
from typing import Any

from core.framework.scale import to_human, to_raw
from services import items as _items

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# 5% house fee on every sale, burned as a sink. Keeps the AH from being
# a pure peer-to-peer wash that drains nothing back to the economy.
DEFAULT_AUCTION_FEE_BPS: int = 500

# Listings expire after 7 days by default. Sellers can pass an explicit
# ttl; this is the fallback. 0 = never expires.
DEFAULT_LISTING_TTL_DAYS: int = 7

# Item kinds the AH knows how to escrow + settle. Anything else gets a
# clean ValueError at create_listing time.
SUPPORTED_KINDS: tuple[str, ...] = (
    "buddy", "egg", "fish", "crop", "ore",
    "weapon", "armor", "consumable", "crafted",
    # Originally missing -- bait dropped from beachcomb / runtime,
    # dungeon junk (mats / salvage like enchanted_thread), dungeon
    # relics (miners_charm etc.), and shop / stone tokens are all
    # legitimate AH inventory now. Without these the bare-name AH
    # listing path resolved a contract but refused with "kind not
    # supported", trapping items in the player's inventory forever.
    "bait", "junk", "relic", "stone", "shop",
)

# Default currency per kind -- "the closest related crypto network"
# rule from the spec. Players can override at list time. For kinds
# whose individual contracts have a per-contract base_price_currency
# (bait -> REEL, dungeon junk -> RUNE, fishing junk -> ... mixed),
# the per-contract value still takes precedence at list time -- this
# table is just the fallback when the contract has no currency set.
DEFAULT_CURRENCY: dict[str, str] = {
    "buddy":      "BUD",
    "egg":        "BUD",
    "fish":       "LURE",
    "bait":       "REEL",
    "crop":       "HRV",
    "ore":        "RUNE",
    "weapon":     "RUNE",
    "armor":      "RUNE",
    "consumable": "RUNE",
    "crafted":    "INGOT",
    # Dungeon junk salvages to RUNE; fishing junk lives on the lure
    # network but most fishing junk has no real sell value (boot,
    # can) so RUNE is the safer fallback when the per-contract
    # currency isn't set. Per-contract overrides win when present.
    "junk":       "RUNE",
    "relic":      "RUNE",
    "stone":      "USDC",  # stones are stablecoin-priced in the shop
    "shop":       "USDC",
}

# Network short for each currency (used by wallet_holdings lookups).
_CURRENCY_NETWORK: dict[str, str] = {
    "BUD":   "bud",
    "FREN":  "bud",
    "BBT":   "bud",
    "LURE":  "lur",
    "REEL":  "lur",
    "HRV":   "har",
    "SEED":  "har",
    "RUNE":  "cry",
    "COPPER": "cry",
    "SILVER": "cry",
    "GOLD":   "cry",
    "INGOT": "fge",
    "FORGE": "fge",
    "FGD":   "fge",
}


@dataclass(slots=True)
class SaleResult:
    listing_id: int
    token_id: str
    kind: str
    qty: int
    seller_id: int
    buyer_id: int
    listed_price_raw: int
    paid_price_raw: int
    currency_paid: str
    seller_received_raw: int
    fee_burned_raw: int
    note: str = ""
    # Gavelstone (auction-house meta gem) extras paid AFTER settlement.
    # Both are denominated in ``currency_paid`` so receipts can quote a
    # single token. Zero when the buyer / seller doesn't own one.
    buyer_rebate_raw: int = 0
    seller_bonus_raw: int = 0


# ─── Escrow handlers ─────────────────────────────────────────────────────────
#
# Each item kind has a (lock, return, deliver) triple:
#
#   _lock_<kind>(db, gid, uid, ref, qty)        -> metadata dict
#       Pull the item out of the seller's source inventory and stash a
#       descriptor for the listing. Raises ValueError if the seller
#       doesn't own / has too few.
#   _return_<kind>(db, gid, uid, ref, qty, md)  -> None
#       Put the item back (cancel / expire path).
#   _deliver_<kind>(db, gid, buyer_uid, ref, qty, md) -> None
#       Hand the item to the buyer (sale settle path).
#
# ``ref`` is the source key (cc_buddies.id for buddies, fish_key for
# fish, weapon_key for weapons, etc.). ``md`` is the same metadata dict
# round-tripped through the listing row so deliver knows e.g. the
# weight / level / rarity of the locked item.


def _as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = _json.loads(value) if value else {}
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = _json.loads(value) if value else []
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


# ── Buddies ──────────────────────────────────────────────────────────────────


async def _lock_buddy(
    db: Any, gid: int, uid: int, ref: str, qty: int,
) -> dict:
    """``ref`` accepts a numeric buddy id OR a buddy name (case-insensitive
    exact match). Name lookup picks the highest-level match if multiple
    buddies share the same name -- usually the player's main is the one
    they want to surface.
    """
    raw = str(ref or "").strip()
    row = None
    try:
        bid_num = int(raw)
    except (TypeError, ValueError):
        bid_num = None
    if bid_num is not None:
        row = await db.fetch_one(
            "SELECT id, species, name, level, xp, rarity_tier, "
            "       hunger, happiness, energy, wins, losses, "
            "       for_sale, status, gender "
            "FROM cc_buddies "
            "WHERE id = $1 AND guild_id = $2 AND owner_user_id = $3 "
            "  AND status = 'owned'",
            bid_num, gid, uid,
        )
    if row is None and raw:
        row = await db.fetch_one(
            "SELECT id, species, name, level, xp, rarity_tier, "
            "       hunger, happiness, energy, wins, losses, "
            "       for_sale, status, gender "
            "FROM cc_buddies "
            "WHERE LOWER(name) = LOWER($1) "
            "  AND guild_id = $2 AND owner_user_id = $3 "
            "  AND status = 'owned' "
            "ORDER BY level DESC, id ASC LIMIT 1",
            raw, gid, uid,
        )
    if not row:
        raise ValueError(
            f"Couldn't find your owned buddy `{raw}`. "
            f"Pass the id from `,buddy stats` or the buddy's name."
        )
    bid = int(row["id"])
    if bool((row or {}).get("for_sale")):
        raise ValueError(
            f"Buddy `#{bid}` is already listed (legacy buddy market)."
        )
    # Flag the buddy as auction-escrowed via status='auction' (a new
    # status value -- migration 0173 doesn't enforce CHECK on this so
    # we can introduce it). Players can't activate / fight / breed an
    # escrowed buddy until cancel/sale clears the flag.
    await db.execute(
        "UPDATE cc_buddies SET status = 'auction', is_active = FALSE, "
        "updated_at = NOW() WHERE id = $1",
        bid,
    )
    return {
        "buddy_id":    bid,
        "species":     str(row.get("species") or ""),
        "name":        str(row.get("name") or ""),
        "level":       int(row.get("level") or 1),
        "xp":          int(row.get("xp") or 0),
        "rarity_tier": int(row.get("rarity_tier") or 1),
        "gender":      str(row.get("gender") or "").upper(),
        "wins":        int(row.get("wins") or 0),
        "losses":      int(row.get("losses") or 0),
    }


async def _return_buddy(
    db: Any, gid: int, uid: int, ref: str, qty: int, md: dict,
) -> None:
    # ``ref`` may be the seller-typed name (e.g. "Sparky") so we always
    # resolve through metadata.buddy_id, which _lock_buddy stamps on.
    bid_raw = (md or {}).get("buddy_id")
    if bid_raw is None:
        try:
            bid_raw = int(ref)
        except (TypeError, ValueError):
            raise ValueError(
                "Listing metadata is missing buddy_id; cannot return."
            )
    bid = int(bid_raw)
    await db.execute(
        "UPDATE cc_buddies SET status = 'owned', "
        "owner_user_id = $2, is_active = FALSE, updated_at = NOW() "
        "WHERE id = $1 AND status = 'auction'",
        bid, uid,
    )


async def _deliver_buddy(
    db: Any, gid: int, buyer_uid: int, ref: str, qty: int, md: dict,
) -> None:
    bid_raw = (md or {}).get("buddy_id")
    if bid_raw is None:
        try:
            bid_raw = int(ref)
        except (TypeError, ValueError):
            raise ValueError(
                "Listing metadata is missing buddy_id; cannot deliver."
            )
    bid = int(bid_raw)
    await db.execute(
        "UPDATE cc_buddies SET "
        "  owner_user_id = $2, status = 'owned', is_active = FALSE, "
        "  last_decay_at = NOW(), last_interacted_at = NOW(), "
        "  updated_at = NOW() "
        "WHERE id = $1",
        bid, int(buyer_uid),
    )


# ── Eggs (held_eggs JSONB on user_fishing) ──────────────────────────────────


_TIER_NAMES = {
    "common": 1, "uncommon": 2, "rare": 3, "epic": 4, "legendary": 5,
    "com": 1, "unc": 2, "rar": 3, "epi": 4, "leg": 5,
    "c": 1, "u": 2, "r": 3, "e": 4, "l": 5,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
    "t1": 1, "t2": 2, "t3": 3, "t4": 4, "t5": 5,
}


def _parse_tier_token(tok: str) -> int | None:
    """Coerce 'legendary', 'leg', 'T5', '5', etc. into a rarity tier int."""
    if not tok:
        return None
    return _TIER_NAMES.get(str(tok).strip().lower())


async def _lock_egg(
    db: Any, gid: int, uid: int, ref: str, qty: int,
) -> dict:
    """Pop an egg out of ``user_fishing.held_eggs`` for listing.

    ``ref`` accepts:

    * a numeric index (``0`` = first held egg) -- see ``,fish egg`` for
      the per-egg index list;
    * a species name (``zenny``, ``crab``, ``wecco``, ...) -- picks the
      highest-rarity match;
    * a ``species:tier`` specifier so the player can target one specific
      rarity out of a stack of dupes -- e.g. ``wecco:legendary``,
      ``crab:T3``. Tier accepts the rarity name, the leg/epi/rar/unc/com
      shorthand, ``T1``..``T5``, or just ``1``..``5``.

    Eggs are genderless until they hatch, so the lock path no longer
    accepts a gender filter.
    """
    state = await db.fetch_one(
        "SELECT held_eggs FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid,
    )
    if not state:
        raise ValueError("You don't have any held eggs to list.")
    held = _as_list(state.get("held_eggs"))
    if not held:
        raise ValueError(
            "You don't have any held eggs. "
            "Get one from `,fish` (~5% per cast), `,delve` mob kills, "
            "or `,buddy nest collect`."
        )

    raw = str(ref or "").strip()
    idx: int | None = None
    # Numeric ref -> direct index.
    try:
        cand = int(raw)
        if 0 <= cand < len(held):
            idx = cand
    except (TypeError, ValueError):
        idx = None

    # species[:tier] path. Splits on ':' / '@' / '/' so `wecco:legendary`,
    # `wecco@T5`, `crab/3` all resolve.
    target_species: str | None = None
    target_tier: int | None = None
    if idx is None and raw:
        # Normalise all separators to ':' then split.
        normalised = raw.replace("@", ":").replace("/", ":")
        parts = [p.strip() for p in normalised.split(":") if p.strip()]
        if parts:
            target_species = parts[0].lower()
            for tok in parts[1:]:
                tier_try = _parse_tier_token(tok)
                if tier_try is not None and target_tier is None:
                    target_tier = tier_try
                else:
                    raise ValueError(
                        f"Couldn't parse `{tok}` as a tier. "
                        f"Use common/uncommon/rare/epic/legendary, "
                        f"T1..T5, or 1..5."
                    )

    if idx is None and target_species:
        matches = [
            (i, e) for i, e in enumerate(held)
            if str(e.get("species") or "").lower() == target_species
            and (
                target_tier is None
                or int(e.get("rarity_tier") or 1) == target_tier
            )
        ]
        if matches:
            # No explicit tier -> pick highest rarity. Explicit tier ->
            # pick the oldest one of that tier (deterministic, lets the
            # player drain duplicates in the order they rolled them).
            if target_tier is None:
                matches.sort(
                    key=lambda ie: int(ie[1].get("rarity_tier") or 1),
                    reverse=True,
                )
            idx = matches[0][0]
    if idx is None:
        # Helpful error: list what they actually hold, broken down by
        # species + tier so the player can see the exact tokens that
        # would resolve.
        from collections import Counter as _Counter
        try:
            from configs.buddies_config import rarity_meta as _b_rarity
        except Exception:
            _b_rarity = lambda t: {"name": f"Tier {t}"}  # type: ignore
        bucket: _Counter[tuple[str, int]] = _Counter()
        for e in held:
            sp = str(e.get("species") or "?").lower()
            tr = int(e.get("rarity_tier") or 1)
            bucket[(sp, tr)] += 1
        held_summary = ", ".join(
            f"{n}x {sp}:{(_b_rarity(tr).get('name') or f'T{tr}').lower()}"
            for (sp, tr), n in sorted(
                bucket.items(),
                key=lambda kv: (-kv[0][1], kv[0][0]),
            )
        ) or "no eggs"
        raise ValueError(
            f"No held egg matches `{raw}`. You hold: {held_summary}. "
            f"Try `,fish egg` for indices, or pass `<species>` / "
            f"`<species>:<tier>`."
        )

    egg = dict(held.pop(idx))
    await db.execute(
        "UPDATE user_fishing SET held_eggs = $3::jsonb, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid, _json.dumps(held),
    )
    return {
        "species":     str(egg.get("species") or ""),
        "rarity_tier": int(egg.get("rarity_tier") or 1),
        "rolled_at":   str(egg.get("rolled_at") or ""),
        "from":        str(egg.get("from") or "auction"),
    }


async def _return_egg(
    db: Any, gid: int, uid: int, ref: str, qty: int, md: dict,
) -> None:
    state = await db.fetch_one(
        "SELECT held_eggs FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid,
    )
    if not state:
        return
    held = _as_list(state.get("held_eggs"))
    held.append({
        "species":     str(md.get("species") or ""),
        "rarity_tier": int(md.get("rarity_tier") or 1),
        "rolled_at":   str(md.get("rolled_at") or ""),
        "from":        "auction_return",
    })
    await db.execute(
        "UPDATE user_fishing SET held_eggs = $3::jsonb, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid, _json.dumps(held),
    )


async def _deliver_egg(
    db: Any, gid: int, buyer_uid: int, ref: str, qty: int, md: dict,
) -> None:
    # Ensure buyer has a user_fishing row -- the held_eggs column lives on it.
    await db.execute(
        "INSERT INTO user_fishing (user_id, guild_id) VALUES ($2, $1) "
        "ON CONFLICT (guild_id, user_id) DO NOTHING",
        gid, buyer_uid,
    )
    state = await db.fetch_one(
        "SELECT held_eggs FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, buyer_uid,
    )
    if not state:
        # Should never happen after the upsert above, but the previous
        # implementation silently UPDATEd zero rows on this path so the
        # buyer was debited but never received the egg. Surface it.
        raise ValueError(
            "Could not initialise buyer fishing state for egg delivery."
        )
    held = _as_list(state.get("held_eggs"))
    # Eggs are genderless until they hatch -- gender is rolled at hatch
    # time, never stamped onto the held egg.
    held.append({
        "species":     str(md.get("species") or ""),
        "rarity_tier": int(md.get("rarity_tier") or 1),
        "rolled_at":   str(md.get("rolled_at") or ""),
        "from":        "auction_buy",
    })
    await db.execute(
        "UPDATE user_fishing SET held_eggs = $3::jsonb, "
        "total_eggs_laid = total_eggs_laid + 1, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, buyer_uid, _json.dumps(held),
    )


# ── Fish (fish_inventory JSONB list-of-entries on user_fishing) ─────────────
#
# fish_inventory is keyed by fish_key -> [{lbs, ts}, ...]. We list by
# fish_key and pop the heaviest entries first so the buyer gets quality.


async def _lock_fish(
    db: Any, gid: int, uid: int, ref: str, qty: int,
) -> dict:
    state = await db.fetch_one(
        "SELECT fish_inventory FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid,
    )
    inv = _as_dict((state or {}).get("fish_inventory"))
    entries = list(inv.get(ref) or [])
    if len(entries) < int(qty):
        raise ValueError(
            f"You only have {len(entries)} `{ref}` in your fish "
            f"inventory (need {qty})."
        )
    # Heaviest first so the listed bundle is the highlight reel.
    entries.sort(key=lambda e: float(e.get("lbs") or 0.0), reverse=True)
    locked = entries[: int(qty)]
    inv[ref] = entries[int(qty):]
    if not inv[ref]:
        inv.pop(ref, None)
    await db.execute(
        "UPDATE user_fishing SET fish_inventory = $3::jsonb, "
        "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
        gid, uid, _json.dumps(inv),
    )
    return {"fish_key": ref, "entries": locked}


async def _return_fish(
    db: Any, gid: int, uid: int, ref: str, qty: int, md: dict,
) -> None:
    await db.execute(
        "INSERT INTO user_fishing (user_id, guild_id) VALUES ($2, $1) "
        "ON CONFLICT (guild_id, user_id) DO NOTHING",
        gid, uid,
    )
    state = await db.fetch_one(
        "SELECT fish_inventory FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid,
    )
    inv = _as_dict((state or {}).get("fish_inventory"))
    cur = list(inv.get(ref) or [])
    cur.extend(md.get("entries") or [])
    inv[ref] = cur
    await db.execute(
        "UPDATE user_fishing SET fish_inventory = $3::jsonb, "
        "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
        gid, uid, _json.dumps(inv),
    )


async def _deliver_fish(
    db: Any, gid: int, buyer_uid: int, ref: str, qty: int, md: dict,
) -> None:
    await _return_fish(db, gid, buyer_uid, ref, qty, md)


# ── Crop (crop_inventory JSONB on user_farming, simple counts) ──────────────


async def _lock_simple_inv(
    db: Any, gid: int, uid: int, table: str, column: str,
    ref: str, qty: int,
) -> dict:
    """Generic count-in-JSONB lock used by crops / ore / weapons / armors /
    consumables / crafted items. Source row is a dict of {key: int_qty}.
    """
    state = await db.fetch_one(
        f"SELECT {column} FROM {table} "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid,
    )
    inv = _as_dict((state or {}).get(column))
    have = int(inv.get(ref) or 0)
    if have < int(qty):
        raise ValueError(
            f"You only have {have} `{ref}` (need {qty})."
        )
    inv[ref] = have - int(qty)
    if inv[ref] <= 0:
        inv.pop(ref, None)
    await db.execute(
        f"UPDATE {table} SET {column} = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid, _json.dumps(inv),
    )
    return {"key": ref, "qty": int(qty)}


async def _credit_simple_inv(
    db: Any, gid: int, uid: int, table: str, column: str,
    ref: str, qty: int,
) -> None:
    """Deliver / return helper. Ensures the user has a row, then bumps."""
    if table == "user_farming":
        await db.execute(
            "INSERT INTO user_farming (user_id, guild_id) VALUES ($2, $1) "
            "ON CONFLICT (guild_id, user_id) DO NOTHING",
            gid, uid,
        )
    elif table == "user_dungeon":
        # user_dungeon has stricter NOT NULLs; only insert if missing.
        exists = await db.fetch_val(
            "SELECT 1 FROM user_dungeon "
            "WHERE guild_id = $1 AND user_id = $2",
            gid, uid,
        )
        if not exists:
            raise ValueError(
                "Buyer hasn't picked a delve class yet (auctioned item "
                "needs `,delve class <warrior|mage|rogue>` first)."
            )
    elif table == "user_crafting":
        exists = await db.fetch_val(
            "SELECT 1 FROM user_crafting "
            "WHERE guild_id = $1 AND user_id = $2",
            gid, uid,
        )
        if not exists:
            await db.execute(
                "INSERT INTO user_crafting (user_id, guild_id) "
                "VALUES ($2, $1) "
                "ON CONFLICT (guild_id, user_id) DO NOTHING",
                gid, uid,
            )
    state = await db.fetch_one(
        f"SELECT {column} FROM {table} "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid,
    )
    inv = _as_dict((state or {}).get(column))
    inv[ref] = int(inv.get(ref) or 0) + int(qty)
    await db.execute(
        f"UPDATE {table} SET {column} = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid, _json.dumps(inv),
    )


async def _lock_crop(db, gid, uid, ref, qty):
    return await _lock_simple_inv(
        db, gid, uid, "user_farming", "crop_inventory", ref, qty,
    )


async def _return_crop(db, gid, uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, uid, "user_farming", "crop_inventory", ref, int(qty),
    )


async def _deliver_crop(db, gid, buyer_uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, buyer_uid, "user_farming", "crop_inventory",
        ref, int(qty),
    )


# ── Ore (raw counts on user_dungeon: copper_qty / silver_qty / gold_qty) ────
#
# Ore lives as scalar columns (not JSONB) on user_dungeon, named
# {ore}_qty. Refs are the upper-case symbol (COPPER / SILVER / GOLD).


_ORE_COLUMNS = {
    "COPPER": "copper_qty",
    "SILVER": "silver_qty",
    "GOLD":   "gold_qty",
}


async def _lock_ore(db, gid, uid, ref, qty):
    col = _ORE_COLUMNS.get(str(ref).upper())
    if not col:
        raise ValueError(f"Unknown ore `{ref}` (use COPPER / SILVER / GOLD).")
    have = await db.fetch_val(
        f"SELECT {col} FROM user_dungeon "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid,
    )
    if int(have or 0) < int(qty):
        raise ValueError(
            f"You only have {int(have or 0)} {ref} ore (need {qty})."
        )
    await db.execute(
        f"UPDATE user_dungeon SET {col} = {col} - $3 "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid, int(qty),
    )
    return {"symbol": ref.upper(), "qty": int(qty)}


async def _return_ore(db, gid, uid, ref, qty, md):
    col = _ORE_COLUMNS.get(str(ref).upper())
    if not col:
        return
    await db.execute(
        f"UPDATE user_dungeon SET {col} = {col} + $3 "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, uid, int(qty),
    )


async def _deliver_ore(db, gid, buyer_uid, ref, qty, md):
    col = _ORE_COLUMNS.get(str(ref).upper())
    if not col:
        return
    exists = await db.fetch_val(
        "SELECT 1 FROM user_dungeon "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, buyer_uid,
    )
    if not exists:
        raise ValueError(
            "Buyer hasn't picked a delve class yet "
            "(`,delve class <warrior|mage|rogue>` first)."
        )
    await db.execute(
        f"UPDATE user_dungeon SET {col} = {col} + $3 "
        "WHERE guild_id = $1 AND user_id = $2",
        gid, buyer_uid, int(qty),
    )


# ── Weapon / armor / consumable (JSONB on user_dungeon) ────────────────────


async def _lock_weapon(db, gid, uid, ref, qty):
    return await _lock_simple_inv(
        db, gid, uid, "user_dungeon", "weapons_owned", ref, qty,
    )


async def _return_weapon(db, gid, uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, uid, "user_dungeon", "weapons_owned", ref, int(qty),
    )


async def _deliver_weapon(db, gid, buyer_uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, buyer_uid, "user_dungeon", "weapons_owned",
        ref, int(qty),
    )


async def _lock_armor(db, gid, uid, ref, qty):
    return await _lock_simple_inv(
        db, gid, uid, "user_dungeon", "armor_owned", ref, qty,
    )


async def _return_armor(db, gid, uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, uid, "user_dungeon", "armor_owned", ref, int(qty),
    )


async def _deliver_armor(db, gid, buyer_uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, buyer_uid, "user_dungeon", "armor_owned",
        ref, int(qty),
    )


async def _lock_consumable(db, gid, uid, ref, qty):
    return await _lock_simple_inv(
        db, gid, uid, "user_dungeon", "consumables", ref, qty,
    )


async def _return_consumable(db, gid, uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, uid, "user_dungeon", "consumables", ref, int(qty),
    )


async def _deliver_consumable(db, gid, buyer_uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, buyer_uid, "user_dungeon", "consumables",
        ref, int(qty),
    )


# ── Crafted items (user_crafting.crafted_inventory) ────────────────────────


async def _lock_crafted(db, gid, uid, ref, qty):
    return await _lock_simple_inv(
        db, gid, uid, "user_crafting", "crafted_inventory", ref, qty,
    )


async def _return_crafted(db, gid, uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, uid, "user_crafting", "crafted_inventory",
        ref, int(qty),
    )


async def _deliver_crafted(db, gid, buyer_uid, ref, qty, md):
    await _credit_simple_inv(
        db, gid, buyer_uid, "user_crafting", "crafted_inventory",
        ref, int(qty),
    )


# Dispatch tables -- keyed by kind so the public API can stay small.
_LOCK_HANDLERS = {
    "buddy":      _lock_buddy,
    "egg":        _lock_egg,
    "fish":       _lock_fish,
    "crop":       _lock_crop,
    "ore":        _lock_ore,
    "weapon":     _lock_weapon,
    "armor":      _lock_armor,
    "consumable": _lock_consumable,
    "crafted":    _lock_crafted,
}
_RETURN_HANDLERS = {
    "buddy":      _return_buddy,
    "egg":        _return_egg,
    "fish":       _return_fish,
    "crop":       _return_crop,
    "ore":        _return_ore,
    "weapon":     _return_weapon,
    "armor":      _return_armor,
    "consumable": _return_consumable,
    "crafted":    _return_crafted,
}
_DELIVER_HANDLERS = {
    "buddy":      _deliver_buddy,
    "egg":        _deliver_egg,
    "fish":       _deliver_fish,
    "crop":       _deliver_crop,
    "ore":        _deliver_ore,
    "weapon":     _deliver_weapon,
    "armor":      _deliver_armor,
    "consumable": _deliver_consumable,
    "crafted":    _deliver_crafted,
}


# Source-table strings used by item_instances rows. Same shape as the
# escrow handlers' lookups so the NFT layer can map cleanly back to the
# canonical row at any time.
_SOURCE_TABLES = {
    "buddy":      "cc_buddies",
    "egg":        "user_fishing.held_eggs",
    "fish":       "user_fishing.fish_inventory",
    "crop":       "user_farming.crop_inventory",
    "ore":        "user_dungeon.ore",
    "weapon":     "user_dungeon.weapons_owned",
    "armor":      "user_dungeon.armor_owned",
    "consumable": "user_dungeon.consumables",
    "crafted":    "user_crafting.crafted_inventory",
}


# ─── Public API ──────────────────────────────────────────────────────────────


def _normalise_currency(currency: str | None, kind: str) -> str:
    """Resolve the listed-in currency for a listing.

    Falls back to the kind's default network coin when the seller doesn't
    pass one. Always upper-cased so wallet_holdings + display lines
    match.
    """
    sym = (currency or "").strip().upper()
    if not sym:
        sym = DEFAULT_CURRENCY.get(kind, "USD")
    return sym


def _kind_to_catalog_key(kind: str, ref: str, item_md: dict) -> str:
    """Resolve the contract catalog_key for a listing.

    Buddies + eggs key by species; fish keys by fish_key; everything else
    uses the user-typed ``ref`` directly (which is already the catalog
    key in those flows, e.g. 'worm' for bait, 'bronze_sword' for weapon).
    """
    md = item_md or {}
    if kind in ("buddy", "egg"):
        return str(md.get("species") or ref or "").lower()
    if kind == "fish":
        return str(md.get("fish_key") or ref or "").lower()
    return str(ref or "").lower()


async def _resolve_tokens_for_listing(
    db: Any,
    *,
    guild_id: int,
    seller_user_id: int,
    kind: str,
    ref: str,
    qty: int,
    item_md: dict,
    source_table: str,
    source_id: str,
) -> list[dict]:
    """Find the seller's oldest N existing per-unit tokens for this
    listing, or fall back to a single legacy lazy-mint.

    Phase-2 PR7: multi-qty listings now escrow N tokens (oldest
    first) instead of a single bundle. Each token gets
    ``owner_user_id = NULL`` set; the caller wires the listing_id
    via the join table after the listing INSERT.

    Returns a list with the PRIMARY token first (used for the
    auction_listings.token_id FK) and the remaining N-1 tokens
    after. For pre-Phase-1 items with no contract / no owned tokens,
    falls back to the legacy ``mint_token`` path with N=1.
    """
    catalog_key = _kind_to_catalog_key(kind, ref, item_md)
    contract_addr = _items.contract_address(kind, catalog_key)
    contract = await _items.get_contract(db, address=contract_addr)
    if contract:
        owned = await _items.list_owned(
            db,
            guild_id=guild_id, user_id=seller_user_id,
            contract_address=contract_addr,
            limit=int(qty),
        )
        if owned:
            escrowed: list[dict] = []
            for tok in owned:
                await db.execute(
                    "UPDATE item_instances SET owner_user_id = NULL, "
                    "updated_at = NOW() WHERE token_id = $1",
                    str(tok["token_id"]),
                )
                refreshed = await _items.get_token(db, str(tok["token_id"]))
                escrowed.append(refreshed or dict(tok))
            return escrowed

    # Legacy fall-through: pre-Phase-1 items don't have a per-unit
    # token yet. Mint one via the original deterministic path and let
    # the auction settle on it as a single bundle. Phase-1 backfill
    # will eventually link it to a contract; until then it works
    # exactly like before.
    token_row = await _items.mint_token(
        db,
        guild_id=guild_id,
        kind=kind,
        source_table=source_table,
        source_id=source_id,
        owner_user_id=None,
        metadata=item_md,
    )
    return [token_row]


async def create_listing_by_token(
    db: Any,
    *,
    guild_id: int,
    seller_user_id: int,
    token_id: str,
    price: float,
    currency: str | None = None,
    ttl_days: int | None = None,
    notes: str = "",
) -> tuple[int, str, str]:
    """Token-id-driven list path. ``,ah list <token_id> <price>``.

    Owns the listing flow end-to-end without going through ``_lock_*``
    JSONB inventory handlers. The NFT is the listing -- escrowing the
    token (``owner_user_id = NULL`` + listing_id pointer) is the only
    state change needed. JSONB inventories drift independently;
    ``,admin items reconcile`` is the cure for that.

    Validation:
      * Token must exist, not be burned, not be escrowed in another
        listing, and be owned by ``seller_user_id`` in this guild.
      * Currency defaults to the contract's home network coin via
        ``DEFAULT_CURRENCY[kind]``.
    """
    tid = str(token_id or "").strip().lower()
    if ":" not in tid:
        raise ValueError(
            f"`{token_id}` doesn't look like a token id "
            f"(expected `<network>:<hex>`)."
        )
    tok = await _items.get_token(db, tid)
    if not tok:
        raise ValueError(f"No token `{token_id}` exists.")
    if tok.get("burned_at") is not None:
        raise ValueError(f"Token `{token_id}` is burned.")
    if int(tok.get("guild_id") or 0) != int(guild_id):
        raise ValueError(f"Token `{token_id}` isn't from this server.")
    if tok.get("listing_id"):
        raise ValueError(
            f"Token `{token_id}` is already escrowed in listing "
            f"#{int(tok['listing_id'])}."
        )
    if int(tok.get("owner_user_id") or 0) != int(seller_user_id):
        raise ValueError(f"You don't own token `{token_id}`.")

    contract = None
    if tok.get("contract_id"):
        contract = await _items.get_contract(
            db, contract_id=int(tok["contract_id"]),
        )
    if not contract:
        raise ValueError(
            f"Token `{token_id}` has no contract registered. "
            f"Bot needs to redeploy contracts; tell an admin."
        )
    kind = str(contract.get("kind") or tok.get("kind") or "").lower()
    catalog_key = str(contract.get("catalog_key") or "").lower()
    if not kind:
        raise ValueError(
            f"Token `{token_id}` is missing kind metadata."
        )
    if kind not in SUPPORTED_KINDS:
        raise ValueError(
            f"Kind `{kind}` not supported by the auction house yet. "
            f"Supported: {', '.join(SUPPORTED_KINDS)}."
        )

    if float(price) <= 0:
        raise ValueError("Price must be positive.")
    listed_currency = _normalise_currency(currency, kind)
    price_raw = to_raw(float(price))

    md = tok.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = _json.loads(md)
        except Exception:
            md = {}

    # For buddies the token's metadata is a SNAPSHOT from mint time;
    # the live cc_buddies row has the current level / rarity / name /
    # gender. Refresh from the live row so listings advertise what the
    # buddy actually is right now, not what it was at hatch.
    if kind == "buddy":
        try:
            src_id = tok.get("source_id")
            bid = (
                int(src_id) if src_id is not None and str(src_id).isdigit()
                else int(md.get("buddy_id") or 0)
            )
        except (TypeError, ValueError):
            bid = 0
        if bid > 0:
            live = await db.fetch_one(
                "SELECT id, name, species, level, xp, rarity_tier, gender, "
                "       hunger, happiness, energy, wins, losses "
                "  FROM cc_buddies WHERE id = $1",
                bid,
            )
            if live:
                md = {
                    **md,
                    "buddy_id":    int(live.get("id") or bid),
                    "name":        str(live.get("name") or md.get("name") or ""),
                    "species":     str(live.get("species") or md.get("species") or ""),
                    "level":       int(live.get("level") or 1),
                    "xp":          int(live.get("xp") or 0),
                    "rarity_tier": int(live.get("rarity_tier") or 1),
                    "gender":      str(live.get("gender") or md.get("gender") or "").upper(),
                    "hunger":      int(live.get("hunger") or 0),
                    "happiness":   int(live.get("happiness") or 0),
                    "energy":      int(live.get("energy") or 0),
                    "wins":        int(live.get("wins") or 0),
                    "losses":      int(live.get("losses") or 0),
                }

    # Display ref for the receipt + listing.metadata.ref.
    if kind == "buddy":
        ref_str = str(md.get("name") or md.get("buddy_id") or catalog_key)
    elif kind == "egg":
        ref_str = str(md.get("species") or catalog_key)
    else:
        ref_str = catalog_key

    async with db.atomic():
        # Charge listing gas BEFORE state changes; atomic rollback if
        # the seller can't cover it.
        list_gas: tuple[int, str] | None = None
        try:
            list_gas = await _items.charge_gas(
                db,
                guild_id=guild_id,
                payer_user_id=seller_user_id,
                network_short=str(tok.get("network") or ""),
                event_type="list",
            )
        except ValueError:
            raise

        ttl = int(
            ttl_days if ttl_days is not None else DEFAULT_LISTING_TTL_DAYS,
        )
        expires_clause = (
            "NOW() + ($7::int * INTERVAL '1 day')"
            if ttl > 0 else "NULL"
        )
        listing_meta = {
            "ref": ref_str,
            "catalog_key": catalog_key,
            "kind": kind,
            # Marker so cancel_listing / buy_listing know this listing
            # came in via the NFT-only path. They skip the legacy
            # _RETURN_HANDLERS / _DELIVER_HANDLERS JSONB writes when
            # they see this flag -- the NFT escrow / transfer is the
            # only state change a token-path listing needs.
            "path": "token",
            **{
                k: v for k, v in md.items()
                if k not in ("contract", "unit_index")
            },
        }
        listing_row = await db.fetch_one(
            f"""
            INSERT INTO auction_listings
                (guild_id, seller_user_id, token_id, kind, qty,
                 currency, price_raw, auction_fee_bps, status,
                 listed_at, expires_at, notes, metadata)
            VALUES
                ($1, $2, $3, $4, $5, $6, $8::numeric, $9, 'active',
                 NOW(), {expires_clause}, $10, $11::jsonb)
            RETURNING id
            """,
            guild_id, seller_user_id, tid, kind, 1,
            listed_currency, ttl, str(price_raw),
            DEFAULT_AUCTION_FEE_BPS, str(notes or "")[:500],
            _json.dumps(listing_meta),
        )
        listing_id = int(listing_row["id"])

        # Escrow the token + write the join row.
        await db.execute(
            "UPDATE item_instances SET listing_id = $2, "
            "owner_user_id = NULL, updated_at = NOW() "
            "WHERE token_id = $1",
            tid, listing_id,
        )
        await db.execute(
            "INSERT INTO auction_listing_tokens "
            "(listing_id, token_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            listing_id, tid,
        )

        # JSONB inventory sync. Pre-fix the token path skipped the
        # legacy _LOCK_HANDLERS so the per-cog JSONB count (e.g.
        # ``user_fishing.fish_inventory[<key>]``) stayed put after the
        # listing was created -- the player still saw the fish in
        # ``,fish inv`` even though the NFT was escrowed. Wire the lock
        # handler back in for kinds that maintain a parallel JSONB
        # count alongside the NFT layer. Wrapped in try/except: if the
        # JSONB row is genuinely missing (NFT minted via expedition /
        # backfill with no JSONB entry) we don't want to roll back the
        # listing -- the NFT escrow is the source of truth and the AH
        # state is correct either way.
        if kind in (
            "fish", "crop", "ore", "weapon", "armor", "consumable", "crafted",
        ):
            try:
                lock_h = _LOCK_HANDLERS.get(kind)
                if lock_h is not None:
                    await lock_h(
                        db, int(guild_id), int(seller_user_id),
                        catalog_key, 1,
                    )
            except Exception:
                log.debug(
                    "token-path JSONB lock failed kind=%s key=%s",
                    kind, catalog_key, exc_info=True,
                )

        # Buddies have a parallel cc_buddies row whose ``status`` field
        # gates every other system (BuddyPanel, battle, breed, expedition,
        # mood interactions). The legacy _lock_buddy flips that to
        # 'auction'; the token path used to skip this so a listed buddy
        # stayed visible in ,buddy and was still battleable. Mirror the
        # flip here so the two surfaces stay consistent.
        if kind == "buddy":
            try:
                src_id = tok.get("source_id")
                bid = (
                    int(src_id) if src_id is not None and str(src_id).isdigit()
                    else int(md.get("buddy_id") or 0)
                )
            except (TypeError, ValueError):
                bid = 0
            if bid > 0:
                await db.execute(
                    "UPDATE cc_buddies "
                    "   SET status = 'auction', is_active = FALSE, "
                    "       updated_at = NOW() "
                    " WHERE id = $1 AND status = 'owned'",
                    bid,
                )
                # Stamp the buddy_id onto the listing metadata so cancel /
                # buy can recover it without re-deriving from the token.
                listing_meta["buddy_id"] = bid
                await db.execute(
                    "UPDATE auction_listings "
                    "   SET metadata = $2::jsonb "
                    " WHERE id = $1",
                    listing_id, _json.dumps(listing_meta),
                )
        try:
            gas_raw_v = list_gas[0] if list_gas else None
            gas_cur_v = list_gas[1] if list_gas else None
            await _items.log_event(
                db,
                token_id=tid,
                event_type="list",
                from_user_id=int(seller_user_id),
                listing_id=int(listing_id),
                price_raw=int(price_raw),
                currency=str(listed_currency),
                gas_raw=gas_raw_v,
                gas_currency=gas_cur_v,
            )
        except Exception:
            log.debug(
                "log_event(list) failed listing=%s tok=%s",
                listing_id, tid, exc_info=True,
            )

    msg = (
        f"Listed **{ref_str}** ({kind}) for "
        f"**{float(price):,.2f} {listed_currency}**. "
        f"Token: `{_items.short_id(tid)}`."
    )
    if ttl_days and ttl_days > 0:
        msg += f" Expires in {int(ttl_days)}d."
    return listing_id, tid, msg


async def find_owned_buddy_token(
    db: Any,
    *,
    guild_id: int,
    seller_user_id: int,
    buddy_id: int,
) -> str | None:
    """Resolve a buddy id (e.g. ``1234`` from ``,buddy stats``) to the
    seller's NFT token id for that buddy.

    Used by ``,ah list <buddy_id> <price>`` and the buddy panel's
    "List on AH" button so players can list buddies by id without
    reaching for the token id. Verifies ownership on cc_buddies AND on
    item_instances. If the buddy has never had an NFT minted (which is
    the normal case -- buddies are only minted lazily on first list),
    mints one on the fly so the caller never has to.

    Returns None when no match resolves (wrong owner, surrendered,
    burned, or already escrowed in another listing).
    """
    # cc_buddies side: must be currently owned by the caller in this
    # guild and not surrendered.
    own_row = await db.fetch_one(
        """
        SELECT id FROM cc_buddies
         WHERE guild_id = $1 AND id = $2 AND owner_user_id = $3
           AND status = 'owned'
        """,
        int(guild_id), int(buddy_id), int(seller_user_id),
    )
    if not own_row:
        return None
    # NFT side: find the token whose source row matches this buddy.
    tok = await _items.find_token(
        db, source_table="cc_buddies", source_id=int(buddy_id),
    )
    if tok is None:
        # Lazy-mint: brand-new buddies don't have an item_instances
        # row until something forces one (auction listing, gift via
        # NFT path, etc). cc_buddies is already the source of truth
        # for ownership, so mint a token owned by the seller and
        # carry on. mint_token is idempotent on (source_table,
        # source_id) so concurrent callers converge on one row.
        try:
            tok = await _items.mint_token(
                db,
                guild_id=int(guild_id),
                kind="buddy",
                source_table="cc_buddies",
                source_id=int(buddy_id),
                owner_user_id=int(seller_user_id),
            )
        except Exception:
            return None
        if not tok:
            return None
    if tok.get("burned_at") is not None:
        return None
    if tok.get("listing_id"):
        return None
    if int(tok.get("owner_user_id") or 0) != int(seller_user_id):
        return None
    return str(tok["token_id"])


async def find_owned_token_for_contract(
    db: Any,
    *,
    guild_id: int,
    seller_user_id: int,
    name_or_address: str,
) -> str | None:
    """Resolve a bare name (e.g. ``minnow``) or a contract address
    (``bait.minnow``) to one of the seller's owned token ids.

    Used by ``,ah list <name> <price>`` so players don't have to
    know token ids. Picks the OLDEST owned token of the matching
    contract (FIFO, matches the AH multi-qty escrow order).

    Returns None when no match resolves.
    """
    raw = (name_or_address or "").strip().lower()
    if not raw:
        return None
    # Exact address match wins first.
    contract = await db.fetch_one(
        "SELECT * FROM item_contracts WHERE address = $1",
        raw,
    )
    if not contract:
        # Try LOWER(name) exact, then LOWER(catalog_key) exact.
        contract = await db.fetch_one(
            "SELECT * FROM item_contracts "
            "WHERE LOWER(name) = $1 OR LOWER(catalog_key) = $1 "
            "ORDER BY contract_id LIMIT 1",
            raw,
        )
    if not contract:
        # Fuzzy substring fallback: most-distinct match by kind preference.
        contract = await db.fetch_one(
            "SELECT * FROM item_contracts "
            "WHERE LOWER(catalog_key) LIKE '%' || $1 || '%' "
            "   OR LOWER(name) LIKE '%' || $1 || '%' "
            "ORDER BY "
            "   CASE WHEN LOWER(catalog_key) = $1 THEN 0 "
            "        WHEN LOWER(name) = $1 THEN 1 ELSE 2 END, "
            "   contract_id LIMIT 1",
            raw,
        )
    if not contract:
        return None

    # Oldest-first owned token of that contract.
    owned = await _items.list_owned(
        db,
        guild_id=int(guild_id),
        user_id=int(seller_user_id),
        contract_address=str(contract["address"]),
        limit=1,
    )
    if owned:
        return str(owned[0]["token_id"])

    # No NFT exists for this contract under this user, but the player
    # might still HAVE one in a JSONB inventory (e.g. phoenix_talon
    # dropped from a boss kill via _credit_loot_drop, which never minted
    # an NFT). Lazy-mint a unit from the JSONB count if possible -- the
    # decrement + mint happen atomically so we never duplicate or lose
    # inventory. Returns None if the player genuinely doesn't own one.
    minted = await _items.lazy_mint_from_jsonb(
        db,
        guild_id=int(guild_id),
        user_id=int(seller_user_id),
        contract=dict(contract),
    )
    if minted:
        return str(minted["token_id"])
    return None


async def create_listing(
    db: Any,
    *,
    guild_id: int,
    seller_user_id: int,
    kind: str,
    ref: str,
    qty: int = 1,
    price: float,
    currency: str | None = None,
    ttl_days: int | None = None,
    notes: str = "",
) -> tuple[int, str, str]:
    """List one item / stack on the auction house.

    Returns ``(listing_id, token_id, message)``. Raises ``ValueError``
    on validation problems (bad kind, not owned, over the per-user cap).
    Wraps the source-inventory lock + listing insert in one transaction
    so a partial failure rolls everything back.
    """
    kind = (kind or "").strip().lower()
    if kind not in SUPPORTED_KINDS:
        raise ValueError(
            f"Unknown kind `{kind}`. Supported: "
            f"{', '.join(SUPPORTED_KINDS)}."
        )
    if int(qty) <= 0:
        raise ValueError("Quantity must be positive.")
    if float(price) <= 0:
        raise ValueError("Price must be positive.")
    listed_currency = _normalise_currency(currency, kind)
    price_raw = to_raw(float(price))

    lock = _LOCK_HANDLERS[kind]

    # PgDatabase.atomic() sets a contextvar so every db.* call inside
    # the block runs on the SAME pooled connection in one transaction.
    # We pass `db` (the wrapper) into handlers, not the raw asyncpg
    # conn -- handlers need fetch_one/fetch_all/execute helpers that
    # only exist on the wrapper, not on raw asyncpg.Connection.
    async with db.atomic():
        item_md = await lock(db, guild_id, seller_user_id, ref, int(qty))
        # Phase 2 PR4: prefer reusing the seller's existing per-unit
        # token (minted at item creation time via mint_unit) so an
        # auction listing transfers the SAME on-chain token rather
        # than minting a duplicate. Falls back to the legacy
        # mint_token path for items that pre-date the per-unit layer
        # (no contract row, or no owned token row yet).
        source_table = _SOURCE_TABLES[kind]
        if kind == "buddy":
            source_id = str(item_md.get("buddy_id"))
        elif kind == "egg":
            # Eggs don't have a stable source id (they live as list
            # entries). Use a content-derived synthetic so we still get
            # a deterministic-ish token. random_hex would also work,
            # but content-derived means the same egg listed twice gets
            # the same id.
            source_id = (
                f"{seller_user_id}:{item_md.get('species')}:"
                f"{item_md.get('rarity_tier')}:{item_md.get('rolled_at')}"
            )
        else:
            source_id = f"{seller_user_id}:{ref}:{int(qty)}"

        token_rows = await _resolve_tokens_for_listing(
            db,
            guild_id=guild_id,
            seller_user_id=seller_user_id,
            kind=kind,
            ref=ref,
            qty=int(qty),
            item_md=item_md,
            source_table=source_table,
            source_id=source_id,
        )
        if not token_rows:
            raise ValueError(
                "Couldn't escrow any tokens for this listing. "
                "Try `,nft list` to verify what you own."
            )
        primary = token_rows[0]
        token_id = str(primary["token_id"])
        all_token_ids = [str(t["token_id"]) for t in token_rows]

        # Charge listing gas in the network's native coin BEFORE the
        # listing row is written so a gas debit failure rolls back the
        # transaction (atomic context) and leaves the seller's
        # inventory + tokens untouched.
        list_gas: tuple[int, str] | None = None
        try:
            list_gas = await _items.charge_gas(
                db,
                guild_id=guild_id,
                payer_user_id=seller_user_id,
                network_short=str(primary.get("network") or ""),
                event_type="list",
            )
        except ValueError:
            # Re-raise so the cog surfaces the wallet-shortage message.
            raise

        ttl = int(
            ttl_days if ttl_days is not None else DEFAULT_LISTING_TTL_DAYS,
        )
        expires_clause = (
            "NOW() + ($7::int * INTERVAL '1 day')"
            if ttl > 0 else "NULL"
        )
        listing_row = await db.fetch_one(
            f"""
            INSERT INTO auction_listings
                (guild_id, seller_user_id, token_id, kind, qty,
                 currency, price_raw, auction_fee_bps, status,
                 listed_at, expires_at, notes, metadata)
            VALUES
                ($1, $2, $3, $4, $5, $6, $8::numeric, $9, 'active',
                 NOW(), {expires_clause}, $10, $11::jsonb)
            RETURNING id
            """,
            guild_id, seller_user_id, token_id, kind, int(qty),
            listed_currency, ttl, str(price_raw),
            DEFAULT_AUCTION_FEE_BPS, str(notes or "")[:500],
            _json.dumps({"ref": ref, **item_md}),
        )
        listing_id = int(listing_row["id"])
        # Backfill listing_id on every escrowed token + insert the
        # join-table rows so cancel / settle paths can sweep them all.
        for tid in all_token_ids:
            await db.execute(
                "UPDATE item_instances SET listing_id = $2, "
                "owner_user_id = NULL, updated_at = NOW() "
                "WHERE token_id = $1",
                tid, listing_id,
            )
            await db.execute(
                "INSERT INTO auction_listing_tokens "
                "(listing_id, token_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                listing_id, tid,
            )
            try:
                # Gas is charged once per listing (not per-qty token);
                # stamp it on the primary token's list event only.
                gas_raw_v = None
                gas_cur_v = None
                if (
                    list_gas is not None
                    and tid == token_id
                ):
                    gas_raw_v, gas_cur_v = list_gas
                await _items.log_event(
                    db,
                    token_id=tid,
                    event_type="list",
                    from_user_id=int(seller_user_id),
                    listing_id=int(listing_id),
                    price_raw=int(price_raw),
                    currency=str(listed_currency),
                    gas_raw=gas_raw_v,
                    gas_currency=gas_cur_v,
                )
            except Exception:
                log.debug(
                    "log_event(list) failed listing=%s tok=%s",
                    listing_id, tid, exc_info=True,
                )

    msg = (
        f"Listed **{int(qty)}x {ref}** ({kind}) for "
        f"**{float(price):,.2f} {listed_currency}**. "
        f"Token: `{_items.short_id(token_id)}`."
    )
    if ttl > 0:
        msg += f" Expires in {ttl}d."
    return listing_id, token_id, msg


async def cancel_listing(
    db: Any, listing_id: int, seller_user_id: int,
) -> tuple[bool, str]:
    """Pull a listing. Returns escrowed item back to the seller.

    Only the original seller can cancel. Raises ``ValueError`` if the
    listing is missing, already settled, or owned by someone else.
    """
    row = await db.fetch_one(
        "SELECT * FROM auction_listings WHERE id = $1",
        int(listing_id),
    )
    if not row:
        raise ValueError(f"Listing #{listing_id} not found.")
    if int(row["seller_user_id"]) != int(seller_user_id):
        raise ValueError("That's not your listing.")
    if row["status"] != "active":
        raise ValueError(f"Listing #{listing_id} is `{row['status']}`.")

    md = _as_dict(row.get("metadata"))
    ref = str(md.get("ref") or "")
    kind = str(row["kind"])
    is_token_path = (str(md.get("path") or "").lower() == "token")

    # Token-path listings never touched JSONB on create, so they don't
    # need the _RETURN_HANDLERS write-back on cancel. The NFT sweep
    # below is the only state change this listing needs to reverse.
    return_h = None
    if not is_token_path:
        return_h = _RETURN_HANDLERS.get(kind)
        if not return_h:
            raise ValueError(f"No return handler for kind `{kind}`.")

    async with db.atomic():
        if return_h is not None:
            await return_h(
                db, int(row["guild_id"]), int(seller_user_id),
                ref, int(row["qty"]), md,
            )
        await db.execute(
            "UPDATE auction_listings SET status = 'cancelled', "
            "cancelled_at = NOW() WHERE id = $1",
            int(listing_id),
        )
        # Sweep every escrowed token back to the seller. The join
        # table is the source of truth for multi-qty listings; falls
        # back to the legacy single token_id for pre-PR7 listings.
        token_ids = [
            str(r["token_id"]) for r in (
                await db.fetch_all(
                    "SELECT token_id FROM auction_listing_tokens "
                    "WHERE listing_id = $1",
                    int(listing_id),
                ) or []
            )
        ] or [str(row["token_id"])]

        # Gas: cancellation costs the seller a small fee in the
        # network's coin (charged once, stamped on the primary token's
        # unlist event). Failure aborts the cancel via atomic rollback.
        primary_tid = token_ids[0] if token_ids else None
        unlist_gas: tuple[int, str] | None = None
        if primary_tid:
            primary_tok = await _items.get_token(db, primary_tid)
            try:
                unlist_gas = await _items.charge_gas(
                    db,
                    guild_id=int(row["guild_id"]),
                    payer_user_id=int(seller_user_id),
                    network_short=str(
                        (primary_tok or {}).get("network") or ""
                    ),
                    event_type="unlist",
                )
            except ValueError:
                raise

        for tid in token_ids:
            await db.execute(
                "UPDATE item_instances SET listing_id = NULL, "
                "owner_user_id = $2, updated_at = NOW() "
                "WHERE token_id = $1",
                tid, int(seller_user_id),
            )
            try:
                gas_raw_v = None
                gas_cur_v = None
                if (
                    unlist_gas is not None
                    and tid == primary_tid
                ):
                    gas_raw_v, gas_cur_v = unlist_gas
                await _items.log_event(
                    db,
                    token_id=tid,
                    event_type="unlist",
                    to_user_id=int(seller_user_id),
                    listing_id=int(listing_id),
                    gas_raw=gas_raw_v,
                    gas_currency=gas_cur_v,
                )
            except Exception:
                log.debug(
                    "log_event(unlist) failed listing=%s tok=%s",
                    listing_id, tid, exc_info=True,
                )
        # Token-path buddies need their cc_buddies row flipped back to
        # 'owned' so the BuddyPanel + battle / breed / expedition gates
        # see the buddy as available again. _return_buddy already does
        # this for the legacy path; the token path skipped it earlier.
        if is_token_path and kind == "buddy":
            try:
                bid = int(md.get("buddy_id") or 0)
            except (TypeError, ValueError):
                bid = 0
            if bid > 0:
                await db.execute(
                    "UPDATE cc_buddies "
                    "   SET status = 'owned', is_active = FALSE, "
                    "       owner_user_id = $2, updated_at = NOW() "
                    " WHERE id = $1 AND status = 'auction'",
                    bid, int(seller_user_id),
                )

        # Token-path JSONB return: mirror the create-path lock so the
        # per-cog inventory count goes back up by one when the player
        # cancels. Wrapped because the JSONB row may not exist (token
        # was minted without a JSONB entry); the NFT layer is the
        # source of truth and the cancel still succeeds.
        if is_token_path and kind in (
            "fish", "crop", "ore", "weapon", "armor", "consumable", "crafted",
        ):
            try:
                ret_h = _RETURN_HANDLERS.get(kind)
                if ret_h is not None:
                    await ret_h(
                        db, int(row["guild_id"]), int(seller_user_id),
                        ref, int(row["qty"]), md,
                    )
            except Exception:
                log.debug(
                    "token-path JSONB return failed kind=%s ref=%s",
                    kind, ref, exc_info=True,
                )

    return True, (
        f"Listing #{listing_id} cancelled. **{ref}** is back with you."
    )


async def find_listing(db: Any, listing_id: int) -> dict | None:
    return await db.fetch_one(
        "SELECT * FROM auction_listings WHERE id = $1",
        int(listing_id),
    )


async def browse_active(
    db: Any,
    guild_id: int,
    *,
    kind: str | None = None,
    seller_user_id: int | None = None,
    max_price: float | None = None,
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Return active listings.

    Filters: ``kind`` / ``seller_user_id`` / ``max_price`` (in the
    listed currency, raw-or-human is fine -- converted via to_raw).
    Sorts:
        * ``newest`` -- most recently listed first (default)
        * ``cheapest`` -- lowest price first
        * ``expensive`` -- highest price first
        * ``expiring`` -- closest expiry first; non-expiring listings
                          fall to the end
    """
    sort_clause = {
        "newest":    "listed_at DESC",
        "cheapest":  "price_raw ASC",
        "expensive": "price_raw DESC",
        "expiring":  (
            "COALESCE(expires_at, "
            "'9999-12-31'::timestamptz) ASC, listed_at DESC"
        ),
    }.get(str(sort or "newest").lower(), "listed_at DESC")
    max_price_raw = (
        int(to_raw(float(max_price))) if max_price is not None else None
    )
    return await db.fetch_all(
        f"""
        SELECT * FROM auction_listings
         WHERE guild_id = $1
           AND status = 'active'
           AND ($2::text IS NULL OR kind = $2)
           AND ($3::bigint IS NULL OR seller_user_id = $3)
           AND ($4::numeric IS NULL OR price_raw <= $4::numeric)
           AND (expires_at IS NULL OR expires_at > NOW())
         ORDER BY {sort_clause}
         LIMIT $5 OFFSET $6
        """,
        int(guild_id), kind, seller_user_id,
        str(max_price_raw) if max_price_raw is not None else None,
        int(limit), int(offset),
    ) or []


async def search_active(
    db: Any,
    guild_id: int,
    query: str,
    *,
    kind: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Free-text search over active listings.

    Matches against the user-facing ``ref`` (e.g. species or item name),
    the buddy ``name`` if present, the egg ``species`` if present, and
    the NFT-style ``token_id``. Case-insensitive substring match. The
    token_id match also works on the short 8-char form.

    Filter by ``kind`` if you want to narrow it before searching.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return []
    return await db.fetch_all(
        """
        SELECT * FROM auction_listings
         WHERE guild_id = $1
           AND status = 'active'
           AND ($2::text IS NULL OR kind = $2)
           AND (expires_at IS NULL OR expires_at > NOW())
           AND (
                 LOWER(COALESCE(metadata->>'ref', '')) LIKE '%' || $3 || '%'
              OR LOWER(COALESCE(metadata->>'name', '')) LIKE '%' || $3 || '%'
              OR LOWER(COALESCE(metadata->>'species', '')) LIKE '%' || $3 || '%'
              OR LOWER(COALESCE(token_id, '')) LIKE '%' || $3 || '%'
           )
         ORDER BY listed_at DESC
         LIMIT $4
        """,
        int(guild_id), kind, needle, int(limit),
    ) or []


async def list_user_listings(
    db: Any, guild_id: int, user_id: int, *, status: str = "active",
) -> list[dict]:
    """All listings for a user, optionally filtered by status."""
    return await db.fetch_all(
        """
        SELECT * FROM auction_listings
         WHERE guild_id = $1 AND seller_user_id = $2
           AND status = $3
         ORDER BY listed_at DESC
        """,
        int(guild_id), int(user_id), str(status),
    ) or []


async def trade_history(
    db: Any, guild_id: int, user_id: int, *, limit: int = 50,
) -> list[dict]:
    """Settled-trade log: every listing where the user was either
    seller or buyer and the listing reached a terminal state
    (sold / cancelled / expired). Newest first.

    Adds two derived columns the cog renders:
      ``role``  -- 'sold' (user was seller, listing sold), 'bought'
                   (user was buyer, listing sold), 'cancelled', or
                   'expired' (user was seller).
      ``settled_currency`` -- the currency the trade actually paid in.
    """
    rows = await db.fetch_all(
        """
        SELECT * FROM auction_listings
         WHERE guild_id = $1
           AND status IN ('sold', 'cancelled', 'expired')
           AND ((seller_user_id = $2)
                OR (buyer_user_id = $2 AND status = 'sold'))
         ORDER BY COALESCE(settled_at, cancelled_at, listed_at) DESC
         LIMIT $3
        """,
        int(guild_id), int(user_id), int(limit),
    ) or []
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        st = str(d.get("status") or "")
        if st == "sold":
            if int(d.get("seller_user_id") or 0) == int(user_id):
                d["role"] = "sold"
            else:
                d["role"] = "bought"
        else:
            d["role"] = st  # cancelled / expired
        d["settled_currency"] = (
            str(d.get("sold_currency") or d.get("currency") or "")
        )
        out.append(d)
    return out


async def last_sold_price(
    db: Any, *, contract_address: str,
) -> dict | None:
    """Return the most-recent sold price + currency for a contract, or
    None when the contract has never sold. Reads item_token_events
    directly so it sees every settle path (token-id listings, name
    listings, kind/ref listings) without duplicating the join logic
    every caller would otherwise reimplement.

    Result shape: ``{"price_raw": int, "currency": str,
    "price_usd_raw": int, "sold_at": datetime}`` or ``None``. Mirrors
    the columns the lexicon CTE projects so call sites can use the
    same FormatKit helpers downstream.
    """
    addr = (contract_address or "").strip().lower()
    if not addr:
        return None
    row = await db.fetch_one(
        "SELECT ev.price_raw, ev.currency, ev.price_usd_raw, "
        "       ev.created_at AS sold_at "
        "  FROM item_token_events ev "
        "  JOIN item_contracts ic ON ic.contract_id = ev.contract_id "
        " WHERE ic.address = $1 "
        "   AND ev.event_type = 'sold' "
        " ORDER BY ev.created_at DESC "
        " LIMIT 1",
        addr,
    )
    if not row:
        return None
    return {
        "price_raw":     int(row.get("price_raw") or 0),
        "currency":      str(row.get("currency") or ""),
        "price_usd_raw": int(row.get("price_usd_raw") or 0),
        "sold_at":       row.get("sold_at"),
    }


async def expire_old(db: Any) -> int:
    """Background sweep: mark expired-but-still-active listings as
    expired and return their items to the sellers. Returns the count
    expired so the caller can log it.
    """
    rows = await db.fetch_all(
        "SELECT * FROM auction_listings "
        "WHERE status = 'active' "
        "  AND expires_at IS NOT NULL "
        "  AND expires_at <= NOW() "
        "LIMIT 500",
    ) or []
    if not rows:
        return 0
    n = 0
    for row in rows:
        try:
            md = _as_dict(row.get("metadata"))
            ref = str(md.get("ref") or "")
            kind = str(row["kind"])
            handler = _RETURN_HANDLERS.get(kind)
            if not handler:
                continue
            async with db.atomic():
                await handler(
                    db, int(row["guild_id"]),
                    int(row["seller_user_id"]),
                    ref, int(row["qty"]), md,
                )
                await db.execute(
                    "UPDATE auction_listings SET status = 'expired', "
                    "settled_at = NOW() WHERE id = $1",
                    int(row["id"]),
                )
                await db.execute(
                    "UPDATE item_instances SET listing_id = NULL, "
                    "owner_user_id = $2, updated_at = NOW() "
                    "WHERE token_id = $1",
                    str(row["token_id"]),
                    int(row["seller_user_id"]),
                )
            n += 1
        except Exception:
            log.exception("auction expire_old: row %s failed", row.get("id"))
    return n


# ── Buy ──────────────────────────────────────────────────────────────────────


async def _wallet_balance_raw(
    db: Any, guild_id: int, user_id: int, symbol: str,
) -> int:
    """Read raw wallet balance for ``symbol``. USD reads come off
    ``users.wallet`` directly; everything else from wallet_holdings via
    the canonical (network, symbol) lookup.
    """
    sym = symbol.upper()
    if sym == "USD":
        ur = await db.get_user(user_id, guild_id)
        return int((ur or {}).get("wallet") or 0)
    net = _CURRENCY_NETWORK.get(sym)
    if not net:
        # Best-effort: pull whichever network has the symbol.
        row = await db.fetch_one(
            "SELECT amount FROM wallet_holdings "
            "WHERE guild_id = $1 AND user_id = $2 AND symbol = $3 "
            "LIMIT 1",
            guild_id, user_id, sym,
        )
        return int((row or {}).get("amount") or 0)
    row = await db.fetch_one(
        "SELECT amount FROM wallet_holdings "
        "WHERE guild_id = $1 AND user_id = $2 "
        "  AND network = $3 AND symbol = $4",
        guild_id, user_id, net, sym,
    )
    return int((row or {}).get("amount") or 0)


async def _debit_wallet(
    db: Any, guild_id: int, user_id: int, symbol: str, raw: int,
) -> None:
    sym = symbol.upper()
    if sym == "USD":
        await db.execute(
            "UPDATE users SET wallet = wallet - $3::numeric "
            "WHERE id = $2 AND guild_id = $1 AND wallet >= $3::numeric",
            guild_id, user_id, str(int(raw)),
        )
        return
    net = _CURRENCY_NETWORK.get(sym, "fge")
    await db.update_wallet_holding(
        user_id, guild_id, net, sym, -int(raw),
    )


async def _credit_wallet(
    db: Any, guild_id: int, user_id: int, symbol: str, raw: int,
) -> None:
    sym = symbol.upper()
    if sym == "USD":
        await db.execute(
            "UPDATE users SET wallet = wallet + $3::numeric "
            "WHERE id = $2 AND guild_id = $1",
            guild_id, user_id, str(int(raw)),
        )
        return
    net = _CURRENCY_NETWORK.get(sym, "fge")
    await db.update_wallet_holding(
        user_id, guild_id, net, sym, int(raw),
    )


async def _convert_via_swap(
    db: Any, guild_id: int, user_id: int,
    pay_symbol: str, listed_symbol: str, listed_price_raw: int,
) -> tuple[int, float]:
    """Cross-currency buy: convert ``pay_symbol`` -> ``listed_symbol``
    via the AMM with slippage. Returns ``(paid_in_pay_raw, impact_pct)``.

    Slippage uses the same impact formula every other swap surface in
    the bot uses (``services.fishing._price_impact``): impact scales
    with USD value of the trade and the pay-token's market cap, so a
    big buy moves more than a small one. Falls back to a flat 1%
    band when no oracle / supply data is available so the caller
    always gets a finite quote.
    """
    listed_h = to_human(int(listed_price_raw))
    # Spot prices for both sides (oracle).
    pa = await db.get_price(pay_symbol, guild_id)
    pb = await db.get_price(listed_symbol, guild_id)
    pa_v = float((pa or {}).get("price") or 0.0)
    pb_v = float((pb or {}).get("price") or 0.0)
    if pa_v <= 0 or pb_v <= 0:
        raise ValueError(
            f"Can't price {pay_symbol} <-> {listed_symbol} right now."
        )
    usd_value = listed_h * pb_v
    pay_h_pre = usd_value / pa_v

    # Real impact: same formula as fishing/farming/buddy burn-swaps.
    # Mirrors what cogs/trade.py .buy / .sell does so a cross-currency
    # AH buy and a direct ,trade swap of the same size move the chart
    # the same amount.
    impact = await _real_impact(db, guild_id, pay_symbol, pa_v, usd_value)
    pay_h = pay_h_pre * (1.0 + impact)
    return int(to_raw(pay_h)), impact


async def _real_impact(
    db: Any,
    guild_id: int,
    pay_symbol: str,
    oracle: float,
    usd_value: float,
) -> float:
    """Compute the AMM-style impact percentage for moving ``usd_value``
    USD of ``pay_symbol``. Reuses ``services.fishing._price_impact`` so
    every swap surface in the bot lands on the same number for the
    same trade size.

    ``supply_human`` comes from ``crypto_prices.circulating_supply``
    when available; falls back to ``Config.TOKENS[sym]['max_supply']``
    so newly-deployed tokens still get a sensible bound. Returns 0.01
    (1%) as a last-resort floor when neither source exists.
    """
    try:
        from services.fishing import _price_impact as fish_impact
        from core.config import Config

        sym = pay_symbol.upper()
        # Prefer live circulating supply -- matches the chart math.
        sup_row = await db.fetch_one(
            "SELECT circulating_supply FROM crypto_prices "
            "WHERE guild_id = $1 AND symbol = $2",
            int(guild_id), sym,
        )
        supply_h = 0.0
        if sup_row and sup_row.get("circulating_supply") is not None:
            supply_h = float(
                to_human(int(sup_row["circulating_supply"] or 0))
            )
        if supply_h <= 0:
            cfg = Config.TOKENS.get(sym) or {}
            supply_h = float(cfg.get("max_supply") or 0)
        if supply_h <= 0 or oracle <= 0:
            return 0.01
        return float(fish_impact(usd_value, oracle, supply_h))
    except Exception:
        log.debug(
            "auction _real_impact: falling back to 1%% (sym=%s)",
            pay_symbol, exc_info=True,
        )
        return 0.01


async def buy_listing(
    db: Any,
    *,
    guild_id: int,
    buyer_user_id: int,
    listing_id: int,
    pay_currency: str | None = None,
) -> SaleResult:
    """Settle a listing.

    ``pay_currency`` defaults to the listed currency (a direct trade,
    no swap impact). Passing a different symbol routes through the
    swap path and applies slippage / impact, mirroring ,buy / ,sell /
    ,trade swap shape.
    """
    row = await db.fetch_one(
        "SELECT *,"
        " CASE WHEN expires_at IS NOT NULL"
        "      THEN EXTRACT(EPOCH FROM (NOW() - expires_at))"
        "      ELSE NULL END AS _secs_since_expiry"
        " FROM auction_listings WHERE id = $1",
        int(listing_id),
    )
    if not row:
        raise ValueError(f"Listing #{listing_id} not found.")
    if row["status"] != "active":
        raise ValueError(f"Listing #{listing_id} is `{row['status']}`.")
    if int(row["seller_user_id"]) == int(buyer_user_id):
        raise ValueError("You can't buy your own listing.")
    _secs_exp = row.get("_secs_since_expiry")
    if _secs_exp is not None and float(_secs_exp) >= 0:
        raise ValueError(
            f"Listing #{listing_id} has expired. Have the seller relist."
        )

    listed_currency = str(row["currency"]).upper()
    listed_price_raw = int(row["price_raw"])
    pay_sym = (pay_currency or listed_currency).strip().upper()

    if pay_sym == listed_currency:
        pay_raw = listed_price_raw
        impact = 0.0
    else:
        pay_raw, impact = await _convert_via_swap(
            db, guild_id, buyer_user_id,
            pay_sym, listed_currency, listed_price_raw,
        )

    # Buyer balance check.
    held = await _wallet_balance_raw(db, guild_id, buyer_user_id, pay_sym)
    if held < pay_raw:
        raise ValueError(
            f"You only have {to_human(held):,.4f} {pay_sym} "
            f"(need {to_human(pay_raw):,.4f})."
        )

    fee_bps = int(row.get("auction_fee_bps") or DEFAULT_AUCTION_FEE_BPS)
    fee_raw = listed_price_raw * fee_bps // 10_000
    seller_credit_raw = listed_price_raw - fee_raw

    md = _as_dict(row.get("metadata"))
    ref = str(md.get("ref") or "")
    kind = str(row["kind"])
    is_token_path = (str(md.get("path") or "").lower() == "token")

    # Token-path listings deliver via the auction_listing_tokens NFT
    # sweep below -- they never wrote to JSONB, so they don't need
    # _DELIVER_HANDLERS' JSONB write either.
    deliver_h = None
    if not is_token_path:
        deliver_h = _DELIVER_HANDLERS.get(kind)
        if not deliver_h:
            raise ValueError(f"No deliver handler for kind `{kind}`.")

    async with db.atomic():
        # Re-check the listing is still active inside the transaction
        # so two concurrent buyers can't both win.
        live = await db.fetch_one(
            "SELECT status FROM auction_listings WHERE id = $1 "
            "FOR UPDATE",
            int(listing_id),
        )
        if not live or live["status"] != "active":
            raise ValueError(
                f"Listing #{listing_id} was just settled by another buyer."
            )

        # 1) buyer pays.
        await _debit_wallet(
            db, guild_id, buyer_user_id, pay_sym, pay_raw,
        )
        # 2) seller gets credited (in the listed currency, not the
        #    buyer's pay currency -- that's the AMM's job).
        await _credit_wallet(
            db, guild_id, int(row["seller_user_id"]),
            listed_currency, seller_credit_raw,
        )
        # 3) deliver the item to the buyer (legacy JSONB path; the
        #    token-path branch leaves JSONB alone and relies on the
        #    NFT sweep at step 5).
        if deliver_h is not None:
            await deliver_h(
                db, guild_id, buyer_user_id, ref, int(row["qty"]), md,
            )
        # 4) close the listing.
        await db.execute(
            "UPDATE auction_listings SET "
            "  status = 'sold', "
            "  buyer_user_id = $2, "
            "  sold_price_raw = $3::numeric, "
            "  sold_currency = $4, "
            "  settled_at = NOW() "
            "WHERE id = $1",
            int(listing_id), int(buyer_user_id),
            str(pay_raw), pay_sym,
        )
        # 5) flip every escrowed token to the buyer. Multi-qty
        #    listings have N tokens in auction_listing_tokens; legacy
        #    single-bundle listings fall back to the primary id.
        token_ids = [
            str(r["token_id"]) for r in (
                await db.fetch_all(
                    "SELECT token_id FROM auction_listing_tokens "
                    "WHERE listing_id = $1",
                    int(listing_id),
                ) or []
            )
        ] or [str(row["token_id"])]
        # USD snapshot for the sold-event log. Best-effort -- if the
        # oracle isn't available we just store NULL for price_usd_raw.
        sold_usd_raw_per_token: int | None = None
        try:
            oracle = await db.get_price(listed_currency, guild_id)
            oracle_v = float((oracle or {}).get("price") or 0.0)
            if oracle_v > 0:
                # Per-token USD = (listed_price / qty) * oracle.
                qty_n = max(1, int(row["qty"]))
                listed_h = to_human(int(listed_price_raw))
                per_token_h = (listed_h / qty_n) * oracle_v
                sold_usd_raw_per_token = int(to_raw(per_token_h))
        except Exception:
            log.debug(
                "buy_listing: oracle USD snapshot failed listing=%s",
                listing_id, exc_info=True,
            )

        # Per-token allocation of the listed price -- sale event is
        # per-token so the price_raw stamp is the per-unit slice.
        qty_n = max(1, int(row["qty"]))
        per_token_price_raw = int(int(listed_price_raw) // qty_n)

        # Gas: buyer pays a flat fee in the network's coin on top of
        # the price. Stamped on the primary token's sold event so
        # the inspect history shows it once.
        primary_tid = token_ids[0] if token_ids else None
        sold_gas: tuple[int, str] | None = None
        if primary_tid:
            primary_tok = await _items.get_token(db, primary_tid)
            try:
                sold_gas = await _items.charge_gas(
                    db,
                    guild_id=guild_id,
                    payer_user_id=int(buyer_user_id),
                    network_short=str(
                        (primary_tok or {}).get("network") or ""
                    ),
                    event_type="sold",
                )
            except ValueError:
                raise

        for tid in token_ids:
            await db.execute(
                "UPDATE item_instances SET "
                "  owner_user_id = $2, listing_id = NULL, "
                "  updated_at = NOW() "
                "WHERE token_id = $1",
                tid, int(buyer_user_id),
            )
            try:
                gas_raw_v = None
                gas_cur_v = None
                if (
                    sold_gas is not None
                    and tid == primary_tid
                ):
                    gas_raw_v, gas_cur_v = sold_gas
                await _items.log_event(
                    db,
                    token_id=tid,
                    event_type="sold",
                    from_user_id=int(row["seller_user_id"]),
                    to_user_id=int(buyer_user_id),
                    listing_id=int(listing_id),
                    price_raw=per_token_price_raw,
                    currency=str(listed_currency),
                    gas_raw=gas_raw_v,
                    gas_currency=gas_cur_v,
                    price_usd_raw=sold_usd_raw_per_token,
                )
            except Exception:
                log.debug(
                    "log_event(sold) failed listing=%s tok=%s",
                    listing_id, tid, exc_info=True,
                )

        # Token-path buddy: transfer the cc_buddies row to the buyer.
        # Mirrors what _deliver_buddy does in the legacy path.
        if is_token_path and kind == "buddy":
            try:
                bid = int(md.get("buddy_id") or 0)
            except (TypeError, ValueError):
                bid = 0
            if bid > 0:
                await db.execute(
                    "UPDATE cc_buddies "
                    "   SET owner_user_id = $2, status = 'owned', "
                    "       is_active = FALSE, updated_at = NOW() "
                    " WHERE id = $1 AND status IN ('auction', 'owned')",
                    bid, int(buyer_user_id),
                )

        # Token-path JSONB delivery: deposit the bought item into the
        # buyer's per-cog inventory. Mirrors the legacy deliver path
        # so the buyer sees their purchase in `,fish inv` / `,farm inv`
        # / etc. immediately, not just in `,items`. Wrapped because the
        # legacy deliver may try to derive lbs / metadata that the
        # token doesn't carry; the NFT transfer above already gave the
        # buyer ownership at the source-of-truth layer, so any failure
        # here just means the JSONB is slightly behind (player still
        # has the NFT, can ,items inspect it, can re-list it, etc.).
        if is_token_path and kind in (
            "fish", "crop", "ore", "weapon", "armor", "consumable", "crafted",
        ):
            try:
                deliver_h = _DELIVER_HANDLERS.get(kind)
                if deliver_h is not None:
                    await deliver_h(
                        db, int(guild_id), int(buyer_user_id),
                        ref, int(row["qty"]), md,
                    )
            except Exception:
                log.debug(
                    "token-path JSONB deliver failed kind=%s ref=%s",
                    kind, ref, exc_info=True,
                )

    # ── Gavelstone (auction-house meta gem) post-settle extras ──────────
    # Buyer rebate + seller bonus credited in the listed currency on top
    # of the settlement transfer. Both scale per Gavelstone level on each
    # side; either party without a Gavelstone simply gets 0. XP grants
    # fire for both sides too: one ,ah buy = +xp_per_buy on the buyer,
    # one settled listing = +xp_per_sale on the seller. Listing creation
    # is intentionally NOT a grant (otherwise spam-listing would farm).
    # All best-effort -- a Gavelstone hiccup must not roll back the sale.
    seller_bonus_raw = 0
    buyer_rebate_raw = 0
    try:
        from services import themed_stones as _ts
        seller_bonus_pct = await _ts.gavelstone_seller_bonus(
            db, int(row["seller_user_id"]), int(guild_id),
        )
        buyer_rebate_pct = await _ts.gavelstone_buyer_rebate(
            db, int(buyer_user_id), int(guild_id),
        )
        if seller_bonus_pct > 0:
            seller_bonus_raw = int(listed_price_raw * seller_bonus_pct)
            if seller_bonus_raw > 0:
                await _credit_wallet(
                    db, int(guild_id), int(row["seller_user_id"]),
                    listed_currency, seller_bonus_raw,
                )
        if buyer_rebate_pct > 0:
            # Rebate is paid in the currency the buyer actually paid in
            # (so a USD-buyer sees their rebate in USD), against the same
            # amount they paid -- mirrors how VIP swap_fee rebates work.
            buyer_rebate_raw = int(pay_raw * buyer_rebate_pct)
            if buyer_rebate_raw > 0:
                await _credit_wallet(
                    db, int(guild_id), int(buyer_user_id),
                    pay_sym, buyer_rebate_raw,
                )
    except Exception:
        log.debug(
            "gavelstone rebate/bonus credit failed listing=%s",
            listing_id, exc_info=True,
        )
    try:
        from services import themed_stones as _ts
        await _ts.grant_gavelstone_xp(
            db, int(buyer_user_id), int(guild_id), bought=True,
        )
        await _ts.grant_gavelstone_xp(
            db, int(row["seller_user_id"]), int(guild_id), sold=True,
        )
    except Exception:
        log.debug(
            "gavelstone xp grant failed listing=%s", listing_id, exc_info=True,
        )

    return SaleResult(
        listing_id=int(listing_id),
        token_id=str(row["token_id"]),
        kind=kind,
        qty=int(row["qty"]),
        seller_id=int(row["seller_user_id"]),
        buyer_id=int(buyer_user_id),
        listed_price_raw=listed_price_raw,
        paid_price_raw=pay_raw,
        currency_paid=pay_sym,
        seller_received_raw=seller_credit_raw,
        fee_burned_raw=fee_raw,
        note=(
            f"Cross-currency swap: ~{impact * 100:.1f}% slippage."
            if impact > 0 else "Direct trade in listed currency."
        ),
        buyer_rebate_raw=int(buyer_rebate_raw),
        seller_bonus_raw=int(seller_bonus_raw),
    )
