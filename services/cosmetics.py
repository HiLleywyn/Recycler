"""V3 Pillar 4: profile cosmetics service.

Public surface:
    grant(db, uid, item_path, source) -- idempotent, source-tagged
    equip(db, uid, slot, item_id)
    unequip(db, uid, slot)
    list_owned(db, uid)
    equipped(db, uid) -- {slot: item_id}
    inventory(db, uid) -- {slot: [item_id, ...]}

``item_path`` here means a `slot/id` string (e.g. `title/season_champ`).
Per-slot APIs accept the bare id (e.g. ``equip(db, uid, "title", "season_champ")``).
"""
from __future__ import annotations

import logging

from configs.cosmetics_config import SLOTS, TITLES, all_items

log = logging.getLogger(__name__)


_ALL = all_items()


async def grant(db, user_id: int, item_path: str, source: str = "system") -> bool:
    """Mark a cosmetic owned by the user. Idempotent: a second grant is a no-op."""
    if item_path not in _ALL:
        log.warning("cosmetics.grant: unknown item_path=%s", item_path)
        return False
    try:
        await db.execute(
            "INSERT INTO user_cosmetics_owned (user_id, item_id, source) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, item_id) DO NOTHING",
            user_id, item_path, source[:64],
        )
        return True
    except Exception:
        log.exception(
            "cosmetics.grant failed uid=%s item=%s", user_id, item_path,
        )
        return False


async def list_owned(db, user_id: int) -> list[str]:
    try:
        rows = await db.fetch_all(
            "SELECT item_id FROM user_cosmetics_owned WHERE user_id = $1 "
            "ORDER BY granted_at ASC",
            user_id,
        )
        return [str(r["item_id"]) for r in rows]
    except Exception:
        return []


async def inventory(db, user_id: int) -> dict[str, list[str]]:
    """Group owned cosmetics by slot.

    Every user gets the ``system``-tagged cosmetics by default even if
    they haven't been explicitly granted, so a brand-new player has at
    least the ``simple`` frame, ``star`` sigil, and ``midnight`` banner
    available in the gallery.
    """
    owned = set(await list_owned(db, user_id))
    out: dict[str, list[str]] = {slot: [] for slot in SLOTS}
    for slot, catalogue in SLOTS.items():
        for cid, entry in catalogue.items():
            unlock = (entry.get("unlock") or "").lower()
            path = f"{slot}/{cid}"
            if unlock == "system" or path in owned:
                out[slot].append(cid)
    return out


async def equipped(db, user_id: int) -> dict[str, str]:
    """Return ``{slot: item_id}`` of currently equipped cosmetics."""
    try:
        rows = await db.fetch_all(
            "SELECT slot, item_id FROM user_cosmetics_equipped WHERE user_id = $1",
            user_id,
        )
        out = {str(r["slot"]): str(r["item_id"]) for r in rows}
    except Exception:
        out = {}
    # Default equips for new players so the profile card never renders
    # naked. Title is intentionally left UNSET by default so the card
    # falls back to the player's actual job + level instead of forcing
    # a generic "Novice" label on every profile. The other three slots
    # have a baseline so the canvas isn't blank.
    out.setdefault("banner", "obsidian")
    out.setdefault("frame", "simple")
    out.setdefault("sigil", "star")
    return out


def cosmetics_for_achievement(badge_id: str) -> list[str]:
    """Return every ``slot/id`` cosmetic path gated on ``achievement:<badge_id>``.

    Read by :mod:`services.achievements` when a badge is granted so the
    associated cosmetic auto-unlocks for the user. Returns ``[]`` when no
    cosmetic targets the badge.
    """
    if not badge_id:
        return []
    needle = f"achievement:{badge_id}"
    return [
        path for path, entry in all_items().items()
        if str(entry.get("unlock", "")) == needle
    ]


async def title_passives(db, user_id: int, guild_id: int | None = None) -> dict[str, float]:
    """Return ``{effect_key: value}`` granted by the user's equipped title.

    Mirrors :func:`services.mastery.passives` so a caller can merge the
    two dicts and read the union with ``services.mastery.apply``:

        m = await mastery.passives(db, uid, gid)
        t = await title_passives(db, uid, gid)
        merged = {k: m.get(k, 0.0) + t.get(k, 0.0) for k in set(m) | set(t)}
        bonus = mastery.apply(merged, "econ.daily_bonus")

    Titles aren't guild-scoped (cosmetics are user-level), so the
    ``guild_id`` argument is accepted for signature parity but ignored.
    Returns ``{}`` for an opt-out player, a player with no title equipped,
    or a title that has no ``effect_key`` in :mod:`cosmetics_config`.
    """
    del guild_id  # signature parity with mastery.passives
    try:
        eq = await equipped(db, user_id)
    except Exception:
        return {}
    title_id = eq.get("title")
    if not title_id:
        return {}
    entry = TITLES.get(title_id)
    if not entry:
        return {}
    key = entry.get("effect_key")
    if not key:
        return {}
    try:
        value = float(entry.get("effect_value", 0.0))
    except (TypeError, ValueError):
        return {}
    if value == 0.0:
        return {}
    return {str(key): value}


async def equip(db, user_id: int, slot: str, item_id: str) -> tuple[bool, str]:
    """Equip ``item_id`` into ``slot``. Returns ``(ok, message)``."""
    slot = slot.lower()
    item_id = item_id.lower()
    if slot not in SLOTS:
        return False, f"Unknown slot `{slot}`."
    if item_id not in SLOTS[slot]:
        return False, f"Unknown item `{item_id}` for slot `{slot}`."
    # Verify ownership unless it's a system default.
    path = f"{slot}/{item_id}"
    unlock = (SLOTS[slot][item_id].get("unlock") or "").lower()
    if unlock != "system":
        owned = await list_owned(db, user_id)
        if path not in owned:
            return False, (
                f"You don't own `{item_id}`. "
                f"Run `,profile gallery` to see what you have."
            )
    try:
        await db.execute(
            "INSERT INTO user_cosmetics_equipped (user_id, slot, item_id) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, slot) DO UPDATE SET "
            "  item_id = EXCLUDED.item_id, "
            "  equipped_at = now()",
            user_id, slot, item_id,
        )
    except Exception:
        log.exception(
            "cosmetics.equip failed uid=%s slot=%s item=%s",
            user_id, slot, item_id,
        )
        return False, "Equip failed -- try again."
    return True, f"Equipped **{SLOTS[slot][item_id]['label']}** as your {slot}."


async def unequip(db, user_id: int, slot: str) -> bool:
    """Remove the equipped item from ``slot`` (falls back to system default)."""
    try:
        await db.execute(
            "DELETE FROM user_cosmetics_equipped WHERE user_id = $1 AND slot = $2",
            user_id, slot.lower(),
        )
        return True
    except Exception:
        return False


def display_label(slot: str, item_id: str) -> str:
    """Pure helper used by the profile renderer."""
    entry = SLOTS.get(slot, {}).get(item_id)
    if not entry:
        return ""
    return str(entry.get("label", item_id))


# ── Shop ──────────────────────────────────────────────────────────────
def shop_price_usd(unlock: str) -> float | None:
    """Parse ``shop:1234.5`` -> 1234.5. Returns None for non-shop entries."""
    if not unlock:
        return None
    s = str(unlock).strip().lower()
    if not s.startswith("shop:"):
        return None
    try:
        return float(s[5:])
    except ValueError:
        return None


def shop_listings(*, theme: str | None = None) -> list[dict]:
    """Return every shop-purchasable cosmetic, optionally filtered by theme.

    Each entry: ``{slot, id, label, price_usd, theme, ...catalogue}``.
    Sorted by (theme, price) for stable rendering.
    """
    out: list[dict] = []
    for slot, catalogue in SLOTS.items():
        for cid, entry in catalogue.items():
            price = shop_price_usd(entry.get("unlock", ""))
            if price is None:
                continue
            t = entry.get("theme", "general")
            if theme and t != theme:
                continue
            out.append({
                **entry,
                "slot": slot,
                "id": cid,
                "price_usd": price,
                "theme": t,
            })
    return sorted(out, key=lambda r: (r["theme"], r["price_usd"]))


async def buy(
    db, user_id: int, gid: int, slot: str, item_id: str,
) -> tuple[bool, str, float]:
    """Purchase a cosmetic. Debits wallet (or bank fallback) atomically.

    Returns ``(ok, message, price_paid)``. Failure modes:
      - slot/item unknown
      - item not shop-purchasable (achievement/season/mastery-locked)
      - already owned
      - insufficient funds (wallet+bank combined)
    """
    slot = slot.lower().strip()
    item_id = item_id.lower().strip()
    if slot not in SLOTS or item_id not in SLOTS[slot]:
        return False, f"Unknown cosmetic `{slot}/{item_id}`.", 0.0
    entry = SLOTS[slot][item_id]
    price_usd = shop_price_usd(entry.get("unlock", ""))
    if price_usd is None:
        return False, (
            f"`{slot}/{item_id}` isn't shop-purchasable -- it unlocks via "
            f"`{entry.get('unlock', 'unknown')}`."
        ), 0.0
    path = f"{slot}/{item_id}"
    owned = await list_owned(db, user_id)
    if path in owned:
        return False, f"You already own `{item_id}`.", price_usd
    try:
        from core.framework.scale import to_raw
        price_raw = int(to_raw(float(price_usd)))
        user = await db.get_user(user_id, gid)
        wallet = int(user.get("wallet") or 0) if user else 0
        bank = int(user.get("bank") or 0) if user else 0
        if wallet + bank < price_raw:
            return False, (
                f"Need ${price_usd:,.2f}; you have only "
                f"${(wallet + bank) / 1e18:,.2f} between wallet + bank."
            ), price_usd
        async with db.atomic():
            from_wallet = min(wallet, price_raw)
            from_bank = price_raw - from_wallet
            if from_wallet > 0:
                await db.update_wallet(user_id, gid, -from_wallet)
            if from_bank > 0:
                await db.execute(
                    "UPDATE users SET bank = bank - $3 "
                    "WHERE user_id = $1 AND guild_id = $2",
                    user_id, gid, from_bank,
                )
            await db.execute(
                "INSERT INTO user_cosmetics_owned (user_id, item_id, source) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (user_id, item_id) DO NOTHING",
                user_id, path, "shop",
            )
            try:
                await db.log_tx(
                    gid, user_id, "COSMETIC_BUY",
                    symbol_in="USD", amount_in=price_raw,
                    symbol_out="cosmetic", amount_out=0,
                    price_at=1.0, network="usd",
                )
            except Exception:
                pass
    except Exception:
        log.exception(
            "cosmetics.buy failed uid=%s slot=%s item=%s",
            user_id, slot, item_id,
        )
        return False, "Purchase failed -- try again.", price_usd
    return True, (
        f"Bought **{entry['label']}** for ${price_usd:,.2f}. "
        f"Equip with `,profile equip {slot} {item_id}`."
    ), price_usd
