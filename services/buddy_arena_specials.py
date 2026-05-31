"""services/buddy_arena_specials.py -- Arena map special-location flows.

Specials are non-combat map nodes (kind in {"shop", "spring", "dig",
"trader"}). The player travels to one with ``,buddy map travel <zid>``
and interacts via ``,buddy map visit``. Each kind has its own UI flow
backed by helpers in this module.

Public surface (all async, all DB-touching):
    visit(ctx)                                    -- ,buddy map visit entry
    shop_offers()                                 -> list[ShopOffer]
    spring_apply(db, gid, uid)                    -> SpringResult
    dig_apply(db, gid, uid)                       -> DigResult
    trader_offers(db, gid, uid)                   -> list[TraderOffer]
    purchase_shop(db, gid, uid, item_key, qty)    -> PurchaseResult
    redeem_trader(db, gid, uid, slot_idx)         -> TraderResult

Per CLAUDE.md: DB-side clocks for cooldowns (EXTRACT EPOCH FROM NOW()),
``card()`` for all embeds, framework formatters for amounts.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

from configs.buddies_config import ARENA_ZONES, BATTLE_CONSUMABLES
from core.framework.scale import to_human, to_raw

log = logging.getLogger(__name__)


# ── Cooldowns (DB-side) ──────────────────────────────────────────────

SPRING_COOLDOWN_S: int = 60 * 60                 # 1 hour
DIG_COOLDOWN_S: int = 24 * 60 * 60               # 24 hours
TRADER_REFRESH_S: int = 6 * 60 * 60              # 6 hours


# ── BUD prices for shop ──────────────────────────────────────────────
#
# Pricing is hand-tuned against the BUD reward curve so a full battle
# inventory costs roughly two zone clears at the player's tier. Common
# items are intentionally cheap to keep the shop useful early; rare/
# epic items lean expensive because the dig + zone drop loop should
# remain the primary source.

SHOP_PRICES_BUD: dict[str, float] = {
    "berry_quick":  3.0,
    "berry_focus":  4.0,
    "vial_rage":    6.0,
    "vial_iron":    6.0,
    "dust_swift":   7.0,
    "cure_balm":    9.0,
    "shock_bolt":  10.0,
    "phoenix_tear": 50.0,
}


# ── Result dataclasses ───────────────────────────────────────────────

@dataclass(slots=True)
class ShopOffer:
    item_key: str
    name: str
    emoji: str
    description: str
    rarity: str
    price_bud_human: float


@dataclass(slots=True)
class PurchaseResult:
    ok: bool
    reason: str
    item_key: str
    qty: int
    bud_spent_raw: int
    new_inventory_qty: int


@dataclass(slots=True)
class SpringResult:
    ok: bool
    reason: str
    cooldown_remaining_s: float
    buddies_restored: int
    free_item_key: str | None


@dataclass(slots=True)
class DigResult:
    ok: bool
    reason: str
    cooldown_remaining_s: float
    item_key: str | None
    new_inventory_qty: int


@dataclass(slots=True)
class TraderOffer:
    slot_idx: int                # 0..2
    label: str
    cost_label: str
    grants_label: str
    cost_kind: str               # "bud" / "bbt"
    cost_amount_raw: int
    grant_item_key: str
    grant_qty: int


@dataclass(slots=True)
class TraderResult:
    ok: bool
    reason: str
    item_key: str
    qty: int
    cost_kind: str
    cost_amount_raw: int


# ── Shop ─────────────────────────────────────────────────────────────

def shop_offers() -> list[ShopOffer]:
    """Static shop catalogue -- every battle consumable, priced in BUD."""
    out: list[ShopOffer] = []
    for key, price in SHOP_PRICES_BUD.items():
        meta = BATTLE_CONSUMABLES.get(key) or {}
        out.append(ShopOffer(
            item_key=key,
            name=str(meta.get("name") or key),
            emoji=str(meta.get("emoji") or ""),
            description=str(meta.get("description") or ""),
            rarity=str(meta.get("rarity") or "common"),
            price_bud_human=float(price),
        ))
    return out


async def purchase_shop(
    db: Any, gid: int, uid: int, item_key: str, qty: int = 1,
) -> PurchaseResult:
    """Buy ``qty`` of ``item_key`` from the Mossy Market.

    Spends BUD via the standard wallet_holdings path; on success the
    item is appended to user_buddy_economy.battle_inventory (same
    column the in-battle dropdown reads).
    """
    item_key = str(item_key or "").strip().lower()
    qty = max(1, int(qty))
    price = SHOP_PRICES_BUD.get(item_key)
    if price is None:
        return PurchaseResult(
            ok=False, reason=f"`{item_key}` is not stocked at the shop.",
            item_key=item_key, qty=qty,
            bud_spent_raw=0, new_inventory_qty=0,
        )

    # Spend BUD via the shared economy helper (single source of truth).
    from services import buddy_economy as _be
    total_bud_human = float(price) * qty
    total_bud_raw = to_raw(total_bud_human)
    held = await _be.get_bud_wallet_raw(db, int(gid), int(uid))
    if held < total_bud_raw:
        return PurchaseResult(
            ok=False,
            reason=(
                f"Need {total_bud_human:,.4f} BUD; you have "
                f"{to_human(held):,.4f}."
            ),
            item_key=item_key, qty=qty,
            bud_spent_raw=0, new_inventory_qty=0,
        )
    await db.update_wallet_holding(
        int(uid), int(gid), _be.BUD_NETWORK_SHORT, _be.BUD_SYMBOL,
        -int(total_bud_raw),
    )

    # Grant items into battle_inventory (JSONB).
    from services import buddy_arena_map as _map
    await _map._grant_battle_item(
        db, int(gid), int(uid), item_key, qty=qty,
    )
    inv = await _map.battle_inventory(db, int(gid), int(uid))
    return PurchaseResult(
        ok=True, reason="",
        item_key=item_key, qty=qty,
        bud_spent_raw=int(total_bud_raw),
        new_inventory_qty=int(inv.get(item_key) or 0),
    )


# ── Spring ───────────────────────────────────────────────────────────

async def spring_apply(db: Any, gid: int, uid: int) -> SpringResult:
    """Restore all owned buddies to 100/100/100 and grant a free Cure Balm.

    Persistent buddy stats (hunger / happiness / energy) drift down
    over time. The spring is a free reset on a 1h DB-clock cooldown so
    players don't have to grind feed/play interactions just to keep
    their roster topped up before a deep zone run.
    """
    # Cooldown check (DB clock per CLAUDE.md).
    row = await db.fetch_one(
        "SELECT EXTRACT(EPOCH FROM (NOW() - last_spring_at)) AS dt "
        "FROM cc_buddy_map_progress "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    dt = float((row or {}).get("dt") or 1e9)
    if dt < SPRING_COOLDOWN_S:
        return SpringResult(
            ok=False, reason="The spring is calming -- come back soon.",
            cooldown_remaining_s=max(0.0, SPRING_COOLDOWN_S - dt),
            buddies_restored=0, free_item_key=None,
        )

    # Restore stats.
    n = await db.fetch_val(
        "WITH upd AS ("
        "  UPDATE cc_buddies "
        "     SET hunger = 100, happiness = 100, energy = 100, "
        "         last_interacted_at = NOW() "
        "   WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned' "
        "   RETURNING 1"
        ") SELECT COUNT(*)::int FROM upd",
        int(gid), int(uid),
    )
    # Grant a free Cure Balm.
    from services import buddy_arena_map as _map
    await _map._grant_battle_item(db, int(gid), int(uid), "cure_balm", qty=1)
    await db.execute(
        "UPDATE cc_buddy_map_progress SET last_spring_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    return SpringResult(
        ok=True, reason="",
        cooldown_remaining_s=0.0,
        buddies_restored=int(n or 0),
        free_item_key="cure_balm",
    )


# ── Dig ──────────────────────────────────────────────────────────────

# Weights are tuned so a daily dig averages roughly one common, one
# uncommon, one rare per week, with a phoenix tear roughly monthly.
_DIG_WEIGHTS: dict[str, float] = {
    "berry_quick":  30.0,
    "berry_focus":  20.0,
    "vial_rage":    15.0,
    "vial_iron":    15.0,
    "dust_swift":   12.0,
    "cure_balm":     5.0,
    "shock_bolt":    3.0,
    "phoenix_tear":  0.5,
}


def _weighted_dig_pick(
    luck_bonus: float = 0.0,
    rng: random.Random | None = None,
) -> str:
    rng = rng or random
    weights = dict(_DIG_WEIGHTS)
    if luck_bonus > 0:
        # Shift weight away from commons toward rares.
        for k, w in list(weights.items()):
            meta = BATTLE_CONSUMABLES.get(k) or {}
            rar = str(meta.get("rarity") or "common")
            if rar in ("rare", "epic"):
                weights[k] = w * (1.0 + luck_bonus)
    keys = list(weights.keys())
    cum = []
    s = 0.0
    for k in keys:
        s += float(weights[k])
        cum.append(s)
    pick = rng.random() * s
    for k, c in zip(keys, cum):
        if pick <= c:
            return k
    return keys[-1]


async def dig_apply(
    db: Any, gid: int, uid: int, *, luck_bonus: float = 0.0,
) -> DigResult:
    """Free random consumable on 24h cooldown.

    ``luck_bonus`` is the mastery passive (luck.rare_catch) which
    shifts the dig table toward rare/epic outcomes.
    """
    row = await db.fetch_one(
        "SELECT EXTRACT(EPOCH FROM (NOW() - last_dig_at)) AS dt "
        "FROM cc_buddy_map_progress "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    dt = float((row or {}).get("dt") or 1e9)
    if dt < DIG_COOLDOWN_S:
        return DigResult(
            ok=False, reason="Smith's stones won't pick today -- try tomorrow.",
            cooldown_remaining_s=max(0.0, DIG_COOLDOWN_S - dt),
            item_key=None, new_inventory_qty=0,
        )
    pick = _weighted_dig_pick(luck_bonus=float(luck_bonus or 0.0))
    from services import buddy_arena_map as _map
    await _map._grant_battle_item(db, int(gid), int(uid), pick, qty=1)
    await db.execute(
        "UPDATE cc_buddy_map_progress SET last_dig_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    inv = await _map.battle_inventory(db, int(gid), int(uid))
    return DigResult(
        ok=True, reason="",
        cooldown_remaining_s=0.0,
        item_key=pick,
        new_inventory_qty=int(inv.get(pick) or 0),
    )


# ── Trader ───────────────────────────────────────────────────────────

# Three trader slots, refreshed every TRADER_REFRESH_S seconds. The
# offer pool is fixed; which 3 appear is a deterministic shuffle keyed
# off (map_seed, refresh_window) so the same player sees the same
# offers across reads within a refresh window.

_TRADER_POOL: list[tuple[str, int, str, int, str]] = [
    # (cost_kind, cost_human, grant_item, grant_qty, label)
    ("bud",  8,   "berry_quick",  3, "3-pack of Quick Berries"),
    ("bud", 12,   "berry_focus",  2, "2-pack of Focus Berries"),
    ("bud", 25,   "vial_rage",    1, "Vial of Rage (combat boost)"),
    ("bud", 25,   "vial_iron",    1, "Iron Vial (damage cut)"),
    ("bud", 30,   "dust_swift",   1, "Swift Dust (permanent SPD)"),
    ("bud", 40,   "cure_balm",    1, "Cure Balm (cleanse + heal)"),
    ("bud", 60,   "shock_bolt",   1, "Shock Bolt (stun + dmg)"),
    ("bbt", 250,  "berry_quick",  5, "Bulk: 5 Quick Berries (BBT)"),
    ("bbt", 600,  "vial_rage",    2, "Pair: 2 Vials of Rage (BBT)"),
    ("bbt", 900,  "cure_balm",    1, "Cure Balm (BBT only)"),
    ("bud", 120,  "phoenix_tear", 1, "Phoenix Tear -- rare!"),
]


def _trader_window(map_seed: int) -> int:
    """Refresh window index for the current 6h slot."""
    import time as _time
    return int(_time.time() // TRADER_REFRESH_S) + (int(map_seed or 0) % 7919)


async def trader_offers(db: Any, gid: int, uid: int) -> list[TraderOffer]:
    """Three trader offers for the current refresh window."""
    progress = await db.fetch_one(
        "SELECT map_seed FROM cc_buddy_map_progress "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    seed = int((progress or {}).get("map_seed") or 0)
    win = _trader_window(seed)
    rng = random.Random(win)
    picks = rng.sample(range(len(_TRADER_POOL)), k=min(3, len(_TRADER_POOL)))
    out: list[TraderOffer] = []
    for idx, pool_idx in enumerate(picks):
        cost_kind, cost_human, item, qty, label = _TRADER_POOL[pool_idx]
        out.append(TraderOffer(
            slot_idx=idx,
            label=label,
            cost_label=f"{cost_human} {cost_kind.upper()}",
            grants_label=f"x{qty} {item}",
            cost_kind=cost_kind,
            cost_amount_raw=to_raw(float(cost_human)),
            grant_item_key=item,
            grant_qty=qty,
        ))
    return out


async def redeem_trader(
    db: Any, gid: int, uid: int, slot_idx: int,
) -> TraderResult:
    """Buy the trader's offer at ``slot_idx`` (0..2)."""
    offers = await trader_offers(db, int(gid), int(uid))
    if slot_idx < 0 or slot_idx >= len(offers):
        return TraderResult(
            ok=False, reason="That trader slot doesn't exist.",
            item_key="", qty=0, cost_kind="", cost_amount_raw=0,
        )
    offer = offers[slot_idx]
    from services import buddy_economy as _be
    if offer.cost_kind == "bud":
        held = await _be.get_bud_wallet_raw(db, int(gid), int(uid))
        if held < offer.cost_amount_raw:
            return TraderResult(
                ok=False, reason="Not enough BUD for that offer.",
                item_key=offer.grant_item_key, qty=offer.grant_qty,
                cost_kind=offer.cost_kind, cost_amount_raw=offer.cost_amount_raw,
            )
        await db.update_wallet_holding(
            int(uid), int(gid), _be.BUD_NETWORK_SHORT, _be.BUD_SYMBOL,
            -int(offer.cost_amount_raw),
        )
    elif offer.cost_kind == "bbt":
        # BBT lives on the same wallet_holdings rows as BUD (the buddy
        # network short), differentiated only by symbol.
        bbt_held = await _be._wallet_raw(db, int(gid), int(uid), _be.BBT_SYMBOL)
        if int(bbt_held or 0) < int(offer.cost_amount_raw):
            return TraderResult(
                ok=False, reason="Not enough BBT for that offer.",
                item_key=offer.grant_item_key, qty=offer.grant_qty,
                cost_kind=offer.cost_kind, cost_amount_raw=offer.cost_amount_raw,
            )
        await db.update_wallet_holding(
            int(uid), int(gid), _be.BUD_NETWORK_SHORT, _be.BBT_SYMBOL,
            -int(offer.cost_amount_raw),
        )
    else:
        return TraderResult(
            ok=False, reason="Unknown payment kind on that offer.",
            item_key="", qty=0, cost_kind=offer.cost_kind, cost_amount_raw=0,
        )

    from services import buddy_arena_map as _map
    await _map._grant_battle_item(
        db, int(gid), int(uid), offer.grant_item_key, qty=offer.grant_qty,
    )
    return TraderResult(
        ok=True, reason="",
        item_key=offer.grant_item_key, qty=offer.grant_qty,
        cost_kind=offer.cost_kind, cost_amount_raw=offer.cost_amount_raw,
    )


# ── Zone-kind dispatch ───────────────────────────────────────────────

def zone_kind(zone_id: str) -> str:
    """Return the special-kind for ``zone_id`` (shop/spring/dig/trader)
    or "" when the zone isn't a special location."""
    z = ARENA_ZONES.get(str(zone_id or ""), {})
    if str(z.get("region") or "") != "special":
        return ""
    return str(z.get("kind") or "")
