"""
services/buddy_market.py  -  P2P transfers + marketplace for buddies + eggs.

Two surfaces share the same audit + listings tables (see migration 0144):

    Direct gift -- ,buddy gift / ,fish egg gift
        gift_buddy(db, gid, sender, recipient, buddy_id)
        gift_egg lives in services/fishing.py (already shipped) since
        eggs are owned by user_fishing rows, not cc_buddies.

    Marketplace -- ,buddy market / list / delist / buy + ,fish egg ditto
        list_buddy / delist_buddy / buy_listed_buddy
        list_egg / buy_listed_egg
        browse_listings (paginated)

Buddy listings soft-lock the row via ``cc_buddies.for_sale = TRUE`` +
``active_listing_id``. Battle / level / cast / shelter paths consult
``for_sale`` so a listed buddy can't be active or fight while the
listing is open. Cancellation / sale clears the flag in the same
transaction as the listings-table mutation.

All prices are USD raw-scaled (Discoin convention). Sale tax is the
seller-side burn of BUDDY_MARKET_TAX_BPS basis points; the buyer pays
the full asking price, the seller receives ``asking - tax``, and the
delta gets deducted-and-not-credited (effective burn).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.framework.scale import to_human, to_raw

import configs.buddies_config as bc

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Direct gift: ,buddy gift @user [buddy]
# ---------------------------------------------------------------------------


@dataclass
class GiftResult:
    """Receipt for a successful ``gift_buddy`` call."""
    transfer_id:    int
    buddy_id:       int
    buddy_name:     str
    species:        str
    rarity_tier:    int
    fee_paid_raw:   int           # USD raw deducted from the sender


async def gift_buddy(
    db: Any, guild_id: int, sender_id: int, recipient_id: int,
    buddy_id: int,
) -> GiftResult:
    """Move a buddy from ``sender_id`` to ``recipient_id`` for a flat fee.

    Validations (all raise ``ValueError`` with a player-friendly message
    so the cog can surface them via ``ctx.reply_error``):
      * sender != recipient
      * buddy belongs to sender + status='owned'
      * buddy is NOT for_sale (must delist first)
      * recipient has shelter room (< MAX_OWNED_BUDDIES)
      * sender can pay BUDDY_GIFT_FEE_USD via wallet+bank

    On success: deducts the fee, updates cc_buddies.owner_user_id,
    clears is_active (the recipient promotes manually), and writes a
    cc_buddy_transfers row of kind='gift'. The whole sequence runs in a
    single atomic block so a partial failure can never split the buddy
    between two owners.
    """
    if sender_id == recipient_id:
        raise ValueError("Can't gift a buddy to yourself.")

    # Sender's row.
    row = await db.fetch_one(
        "SELECT id, species, name, rarity_tier, owner_user_id, "
        "  status, is_active, for_sale "
        "FROM cc_buddies WHERE id = $1 AND guild_id = $2",
        int(buddy_id), int(guild_id),
    )
    if not row:
        raise ValueError(f"No buddy with id `{buddy_id}` in this guild.")
    if int(row["owner_user_id"]) != int(sender_id):
        raise ValueError("That buddy isn't yours to gift.")
    if str(row["status"]) != "owned":
        raise ValueError(
            f"Buddy is currently `{row['status']}`. Only owned buddies "
            f"can be gifted."
        )
    if bool(row.get("for_sale")):
        raise ValueError(
            "This buddy is currently listed on the market. "
            "Delist it first with `,buddy delist <listing_id>`."
        )

    # Recipient capacity.
    held = await db.fetch_val(
        "SELECT COUNT(*) FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned'",
        int(guild_id), int(recipient_id),
    )
    from services.buddy_economy import user_max_battle_slots as _max_battle
    cap = await _max_battle(db, int(guild_id), int(recipient_id))
    if int(held or 0) >= cap:
        raise ValueError(
            f"Recipient already holds the max **{cap}** "
            f"battle-active buddies. They need to store one, surrender, "
            f"or buy a battle slot upgrade."
        )

    fee_raw = to_raw(float(bc.BUDDY_GIFT_FEE_USD))

    async with db.atomic():
        # Pull fee from sender. Raises ValueError on shortfall, which
        # is the message the cog surfaces.
        try:
            await db.deduct_liquid(int(sender_id), int(guild_id), int(fee_raw))
        except ValueError:
            raise ValueError(
                f"Gift fee is **${bc.BUDDY_GIFT_FEE_USD:,}** "
                f"(wallet + bank combined). You don't have enough."
            )

        # Hand the buddy over. Clear is_active so the recipient
        # explicitly promotes; this avoids surprise demotions of
        # whichever buddy they had active before.
        upd = await db.fetch_one(
            "UPDATE cc_buddies SET "
            "  owner_user_id = $1, "
            "  is_active = FALSE, "
            "  updated_at = NOW() "
            "WHERE id = $2 AND owner_user_id = $3 AND status = 'owned' "
            "  AND for_sale = FALSE "
            "RETURNING id, species, name, rarity_tier",
            int(recipient_id), int(buddy_id), int(sender_id),
        )
        if not upd:
            # Lost a race -- another command picked the buddy up.
            raise ValueError(
                "Gift didn't go through -- the buddy's state changed. Try again."
            )

        # Audit row.
        tx = await db.fetch_one(
            "INSERT INTO cc_buddy_transfers "
            "  (guild_id, from_user_id, to_user_id, buddy_id, "
            "   transfer_kind, price_raw, fee_raw) "
            "VALUES ($1, $2, $3, $4, 'gift', 0, $5) "
            "RETURNING transfer_id",
            int(guild_id), int(sender_id), int(recipient_id),
            int(buddy_id), int(fee_raw),
        )

    return GiftResult(
        transfer_id=int((tx or {}).get("transfer_id") or 0),
        buddy_id=int(upd["id"]),
        buddy_name=str(upd["name"] or ""),
        species=str(upd["species"] or ""),
        rarity_tier=int(upd["rarity_tier"] or 1),
        fee_paid_raw=int(fee_raw),
    )


async def get_owned_buddies(
    db: Any, guild_id: int, user_id: int,
) -> list[dict]:
    """Return every owned buddy for ``(guild_id, user_id)`` for the cog
    to look up by id-or-name when the user types ``,buddy gift @x carl``."""
    rows = await db.fetch_all(
        "SELECT id, species, name, rarity_tier, level, is_active, "
        "  for_sale, active_listing_id "
        "FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned' "
        "ORDER BY is_active DESC, id ASC",
        int(guild_id), int(user_id),
    )
    return [dict(r) for r in (rows or [])]


def find_buddy_by_id_or_name(
    rows: list[dict], needle: str | None,
) -> dict | None:
    """Resolve a buddy from a user-supplied id-or-name token.

    ``needle is None`` -> return the active buddy if one exists.
    Numeric needle -> exact id match.
    String needle -> case-insensitive exact name match, falling back to
    a unique case-insensitive prefix match. Ambiguous matches return
    None so the cog can prompt for disambiguation.
    """
    if not rows:
        return None
    if needle is None or not str(needle).strip():
        for r in rows:
            if bool(r.get("is_active")):
                return r
        return rows[0]
    s = str(needle).strip()
    if s.isdigit():
        wanted = int(s)
        for r in rows:
            if int(r["id"]) == wanted:
                return r
        return None
    sl = s.lower()
    exact = [r for r in rows if str(r.get("name") or "").lower() == sl]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None  # name collision -- caller asks for id
    pref = [r for r in rows if str(r.get("name") or "").lower().startswith(sl)]
    if len(pref) == 1:
        return pref[0]
    return None


# ---------------------------------------------------------------------------
# Marketplace: list / delist / buy (buddies + eggs share the listings table)
# ---------------------------------------------------------------------------

import json as _json_mod


def _jsonb(payload: Any) -> str:
    """Serialise a Python value for an asyncpg ``$n::jsonb`` bind.

    Mirrors ``services.fishing._json``; duplicated here so the marketplace
    module doesn't import a private helper across services.
    """
    return _json_mod.dumps(payload, separators=(",", ":"))


def _eggs_as_list(value: Any) -> list:
    """Normalise ``user_fishing.held_eggs`` to a Python list.

    asyncpg returns JSONB columns as raw JSON strings in this project
    (no codec is registered), so a stored ``[]`` arrives as the string
    ``"[]"``. Accepts list / str / None and always returns a list.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = _json_mod.loads(value)
        except (ValueError, TypeError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _tier_name(tier: int) -> str:
    """Look up the human-readable tier name (``Common`` .. ``Legendary``)."""
    meta = bc.RARITY_TIERS.get(int(tier or 1))
    return str((meta or {}).get("name") or f"T{int(tier or 1)}")


def _buddy_label(species: str, name: str | None, tier: int) -> str:
    """Render the display string used on every buddy receipt + browse row."""
    nm = (name or "").strip() or "Unnamed"
    return f'{_tier_name(tier)} {str(species or "buddy").title()} "{nm}"'


def _egg_label(species: str, tier: int) -> str:
    """Render the display string used on every egg receipt + browse row."""
    return f"{_tier_name(tier)} {str(species or 'mystery').title()} Egg"


@dataclass
class ListResult:
    """Receipt for a successful ``list_buddy`` / ``list_egg`` call."""
    listing_id:       int
    kind:             str               # 'buddy' | 'egg'
    label:            str
    asking_price_raw: int


@dataclass
class DelistResult:
    """Receipt for a successful ``delist_buddy`` / ``cancel_egg_listing`` call."""
    listing_id:       int
    kind:             str
    label:            str


@dataclass
class BuyResult:
    """Receipt for a successful ``buy_listed_buddy`` / ``buy_listed_egg`` call."""
    listing_id:          int
    kind:                str
    label:               str
    buddy_id:            int | None
    egg_payload:         dict | None
    price_paid_raw:      int
    tax_paid_raw:        int
    seller_credited_raw: int
    transfer_id:         int


def _validate_price_usd(price_usd: float) -> int:
    """Bounds-check + raw-scale ``price_usd``. Raises ValueError on miss."""
    try:
        p = float(price_usd)
    except (TypeError, ValueError):
        raise ValueError("Price must be a number in USD.")
    if p < float(bc.BUDDY_MARKET_MIN_PRICE_USD):
        raise ValueError(
            f"Minimum listing price is **${bc.BUDDY_MARKET_MIN_PRICE_USD:,}**."
        )
    if p > float(bc.BUDDY_MARKET_MAX_PRICE_USD):
        raise ValueError(
            f"Maximum listing price is **${bc.BUDDY_MARKET_MAX_PRICE_USD:,}**."
        )
    return int(to_raw(p))


async def _count_active_listings(db: Any, guild_id: int, seller_id: int) -> int:
    """Count the seller's open offers across both buddies AND eggs."""
    n = await db.fetch_val(
        "SELECT COUNT(*) FROM cc_buddy_listings "
        "WHERE guild_id = $1 AND seller_user_id = $2 AND status = 'active'",
        int(guild_id), int(seller_id),
    )
    return int(n or 0)


async def list_buddy(
    db: Any, guild_id: int, seller_id: int, buddy_id: int,
    price_usd: float,
) -> ListResult:
    """Open a marketplace listing for ``buddy_id`` at ``price_usd``.

    Soft-locks the buddy: ``cc_buddies.for_sale`` flips to TRUE and
    ``active_listing_id`` is wired to the new row, both inside the same
    transaction as the INSERT. Battle / cast / level / promote paths
    consult ``for_sale`` so a listed buddy can't fight or train. The
    buddy is also force-deactivated so a buyer can't accidentally race
    the seller's active-pet bonus.
    """
    asking_raw = _validate_price_usd(price_usd)

    row = await db.fetch_one(
        "SELECT id, species, name, rarity_tier, owner_user_id, "
        "  status, for_sale "
        "FROM cc_buddies WHERE id = $1 AND guild_id = $2",
        int(buddy_id), int(guild_id),
    )
    if not row:
        raise ValueError(f"No buddy with id `{buddy_id}` in this guild.")
    if int(row["owner_user_id"]) != int(seller_id):
        raise ValueError("That buddy isn't yours to list.")
    if str(row["status"]) != "owned":
        raise ValueError(
            f"Buddy is currently `{row['status']}`. Only owned buddies "
            f"can be listed."
        )
    if bool(row.get("for_sale")):
        raise ValueError("That buddy is already on the market.")

    open_count = await _count_active_listings(db, int(guild_id), int(seller_id))
    if open_count >= int(bc.BUDDY_MARKET_MAX_LISTINGS_PER_USER):
        raise ValueError(
            f"You already have **{open_count}** active listings (max "
            f"**{bc.BUDDY_MARKET_MAX_LISTINGS_PER_USER}**). Cancel one first."
        )

    species = str(row["species"] or "")
    nm      = str(row.get("name") or "")
    tier    = int(row.get("rarity_tier") or 1)
    label   = _buddy_label(species, nm, tier)

    async with db.atomic():
        ins = await db.fetch_one(
            "INSERT INTO cc_buddy_listings "
            "  (guild_id, seller_user_id, buddy_id, asking_price_raw) "
            "VALUES ($1, $2, $3, $4) "
            "RETURNING listing_id",
            int(guild_id), int(seller_id), int(buddy_id), int(asking_raw),
        )
        new_listing_id = int((ins or {}).get("listing_id") or 0)
        if not new_listing_id:
            raise ValueError("Listing didn't go through. Try again.")

        upd = await db.fetch_one(
            "UPDATE cc_buddies SET "
            "  for_sale          = TRUE, "
            "  active_listing_id = $1, "
            "  is_active         = FALSE, "
            "  updated_at        = NOW() "
            "WHERE id = $2 AND owner_user_id = $3 "
            "  AND status = 'owned' AND for_sale = FALSE "
            "RETURNING id",
            int(new_listing_id), int(buddy_id), int(seller_id),
        )
        if not upd:
            raise ValueError(
                "Listing race -- the buddy's state changed mid-flight. Try again."
            )

    return ListResult(
        listing_id=int(new_listing_id),
        kind="buddy",
        label=label,
        asking_price_raw=int(asking_raw),
    )


async def delist_buddy(
    db: Any, guild_id: int, seller_id: int, listing_id: int,
) -> DelistResult:
    """Cancel an open buddy listing and unlock the buddy."""
    row = await db.fetch_one(
        "SELECT listing_id, seller_user_id, buddy_id, status "
        "FROM cc_buddy_listings "
        "WHERE listing_id = $1 AND guild_id = $2",
        int(listing_id), int(guild_id),
    )
    if not row:
        raise ValueError(f"No listing with id `{listing_id}`.")
    if int(row["seller_user_id"]) != int(seller_id):
        raise ValueError("That listing isn't yours to cancel.")
    if str(row["status"]) != "active":
        raise ValueError(f"Listing is already `{row['status']}`.")
    if row.get("buddy_id") is None:
        raise ValueError(
            "That's an egg listing -- use `,fish egg unlist` instead."
        )

    bud = await db.fetch_one(
        "SELECT id, species, name, rarity_tier "
        "FROM cc_buddies WHERE id = $1 AND guild_id = $2",
        int(row["buddy_id"]), int(guild_id),
    )
    label = _buddy_label(
        str((bud or {}).get("species") or ""),
        str((bud or {}).get("name") or ""),
        int((bud or {}).get("rarity_tier") or 1),
    )

    async with db.atomic():
        cancelled = await db.fetch_one(
            "UPDATE cc_buddy_listings SET "
            "  status       = 'cancelled', "
            "  cancelled_at = NOW() "
            "WHERE listing_id = $1 AND status = 'active' "
            "RETURNING listing_id",
            int(listing_id),
        )
        if not cancelled:
            raise ValueError("Listing already closed -- refresh and try again.")

        await db.execute(
            "UPDATE cc_buddies SET "
            "  for_sale          = FALSE, "
            "  active_listing_id = NULL, "
            "  updated_at        = NOW() "
            "WHERE id = $1",
            int(row["buddy_id"]),
        )

    return DelistResult(
        listing_id=int(listing_id),
        kind="buddy",
        label=label,
    )


async def buy_listed_buddy(
    db: Any, guild_id: int, buyer_id: int, listing_id: int,
) -> BuyResult:
    """Purchase a buddy listing. Buyer pays full asking; seller gets
    asking - tax (tax = ``BUDDY_MARKET_TAX_BPS`` of asking, burned).
    """
    row = await db.fetch_one(
        "SELECT listing_id, seller_user_id, buddy_id, asking_price_raw, status "
        "FROM cc_buddy_listings "
        "WHERE listing_id = $1 AND guild_id = $2",
        int(listing_id), int(guild_id),
    )
    if not row:
        raise ValueError(f"No listing with id `{listing_id}`.")
    if str(row["status"]) != "active":
        raise ValueError(f"Listing is `{row['status']}` -- not for sale.")
    if row.get("buddy_id") is None:
        raise ValueError(
            "That's an egg listing -- use `,fish egg buy` instead."
        )
    seller_id = int(row["seller_user_id"])
    if seller_id == int(buyer_id):
        raise ValueError("Can't buy your own listing.")

    bud = await db.fetch_one(
        "SELECT id, species, name, rarity_tier "
        "FROM cc_buddies WHERE id = $1 AND guild_id = $2",
        int(row["buddy_id"]), int(guild_id),
    )
    if not bud:
        raise ValueError("Buddy vanished -- ask the seller to relist.")

    held = await db.fetch_val(
        "SELECT COUNT(*) FROM cc_buddies "
        "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned'",
        int(guild_id), int(buyer_id),
    )
    # Honour the BUD-purchased battle slot cap. Market buys land into
    # the active battle pool only; if it's full the buyer needs to free
    # a slot or upgrade before completing the purchase.
    try:
        from services import buddy_economy as _bes
        max_owned = await _bes.user_max_battle_slots(
            db, int(guild_id), int(buyer_id),
        )
    except Exception:
        max_owned = int(bc.MAX_OWNED_BUDDIES)
    if int(held or 0) >= max_owned:
        raise ValueError(
            f"You already hold the max **{max_owned}** battle-active "
            f"buddies. Store one or buy a battle slot upgrade at "
            f"`,buddy slot battle buy`."
        )

    asking_raw = int(row["asking_price_raw"])
    tax_raw    = int((asking_raw * int(bc.BUDDY_MARKET_TAX_BPS)) // 10000)
    seller_raw = int(asking_raw - tax_raw)
    label      = _buddy_label(
        str(bud.get("species") or ""),
        str(bud.get("name") or ""),
        int(bud.get("rarity_tier") or 1),
    )

    async with db.atomic():
        # Listings are denominated in BUD on the Buddy Network. The buyer
        # can pay in BUD directly; if they're short, ``auto_buy_bud_for_market``
        # fills the gap from their USD wallet at the live BUD/USD oracle
        # minus the standard mint impact (chart moves, LP slice paid).
        try:
            from services import buddy_economy as _bes
            await _bes.auto_buy_bud_for_market(
                db, int(guild_id), int(buyer_id), int(asking_raw),
            )
            await db.update_wallet_holding(
                int(buyer_id), int(guild_id),
                _bes.BUD_NETWORK_SHORT, _bes.BUD_SYMBOL,
                -int(asking_raw),
            )
        except ValueError as exc:
            raise ValueError(
                f"You can't afford **{to_human(asking_raw):,.4f} BUD** "
                f"(neither in your BUD wallet nor convertible from USD): {exc}"
            )
        if seller_raw > 0:
            await db.update_wallet_holding(
                int(seller_id), int(guild_id),
                _bes.BUD_NETWORK_SHORT, _bes.BUD_SYMBOL,
                int(seller_raw),
            )

        sold = await db.fetch_one(
            "UPDATE cc_buddy_listings SET "
            "  status        = 'sold', "
            "  buyer_user_id = $1, "
            "  sold_at       = NOW() "
            "WHERE listing_id = $2 AND status = 'active' "
            "RETURNING listing_id",
            int(buyer_id), int(listing_id),
        )
        if not sold:
            raise ValueError(
                "Listing was just snapped up by someone else. Try another."
            )

        moved = await db.fetch_one(
            "UPDATE cc_buddies SET "
            "  owner_user_id     = $1, "
            "  for_sale          = FALSE, "
            "  active_listing_id = NULL, "
            "  is_active         = FALSE, "
            "  updated_at        = NOW() "
            "WHERE id = $2 AND owner_user_id = $3 AND status = 'owned' "
            "RETURNING id",
            int(buyer_id), int(row["buddy_id"]), int(seller_id),
        )
        if not moved:
            raise ValueError(
                "Ownership swap failed mid-flight -- refresh and try again."
            )

        tx = await db.fetch_one(
            "INSERT INTO cc_buddy_transfers "
            "  (guild_id, from_user_id, to_user_id, buddy_id, "
            "   transfer_kind, price_raw, fee_raw) "
            "VALUES ($1, $2, $3, $4, 'sale', $5, $6) "
            "RETURNING transfer_id",
            int(guild_id), int(seller_id), int(buyer_id),
            int(row["buddy_id"]), int(asking_raw), int(tax_raw),
        )

    return BuyResult(
        listing_id=int(listing_id),
        kind="buddy",
        label=label,
        buddy_id=int(row["buddy_id"]),
        egg_payload=None,
        price_paid_raw=int(asking_raw),
        tax_paid_raw=int(tax_raw),
        seller_credited_raw=int(seller_raw),
        transfer_id=int((tx or {}).get("transfer_id") or 0),
    )


# ---- Egg listings: list / cancel / buy ------------------------------------


async def list_egg(
    db: Any, guild_id: int, seller_id: int, species: str,
    price_usd: float,
) -> ListResult:
    """Open a marketplace listing for one held egg of ``species``.

    Pops the OLDEST matching egg from ``user_fishing.held_eggs`` into
    the listing's ``egg_payload`` JSONB column. Cancellation refunds it;
    sale appends it to the buyer's held_eggs.
    """
    asking_raw = _validate_price_usd(price_usd)

    sp = str(species or "").strip().lower()
    if not sp:
        raise ValueError("Pick a species to list.")

    open_count = await _count_active_listings(db, int(guild_id), int(seller_id))
    if open_count >= int(bc.BUDDY_MARKET_MAX_LISTINGS_PER_USER):
        raise ValueError(
            f"You already have **{open_count}** active listings (max "
            f"**{bc.BUDDY_MARKET_MAX_LISTINGS_PER_USER}**). Cancel one first."
        )

    state = await db.fetch_one(
        "SELECT held_eggs FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        int(guild_id), int(seller_id),
    )
    eggs = list(_eggs_as_list((state or {}).get("held_eggs")))
    if not eggs:
        raise ValueError("You have no held eggs to list.")

    popped: dict | None = None
    kept: list = []
    for e in eggs:
        if popped is None and str((e or {}).get("species") or "").lower() == sp:
            popped = dict(e)
            continue
        kept.append(e)
    if popped is None:
        raise ValueError(f"You hold no **{sp}** eggs to list.")

    tier  = int((popped or {}).get("rarity_tier") or 1)
    label = _egg_label(sp, tier)

    async with db.atomic():
        await db.execute(
            "UPDATE user_fishing SET "
            "  held_eggs  = $3::jsonb, "
            "  updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            int(guild_id), int(seller_id), _jsonb(kept),
        )
        ins = await db.fetch_one(
            "INSERT INTO cc_buddy_listings "
            "  (guild_id, seller_user_id, egg_payload, asking_price_raw) "
            "VALUES ($1, $2, $3::jsonb, $4) "
            "RETURNING listing_id",
            int(guild_id), int(seller_id), _jsonb(popped), int(asking_raw),
        )
        new_listing_id = int((ins or {}).get("listing_id") or 0)
        if not new_listing_id:
            raise ValueError("Listing didn't go through. Try again.")

    return ListResult(
        listing_id=int(new_listing_id),
        kind="egg",
        label=label,
        asking_price_raw=int(asking_raw),
    )


async def cancel_egg_listing(
    db: Any, guild_id: int, seller_id: int, listing_id: int,
) -> DelistResult:
    """Cancel an open egg listing and return the egg to the seller."""
    try:
        import configs.fishing_config as fc
    except Exception:
        log.exception("cancel_egg_listing: fishing_config import failed")
        raise ValueError("Egg system unavailable -- try again shortly.")

    row = await db.fetch_one(
        "SELECT listing_id, seller_user_id, buddy_id, egg_payload, status "
        "FROM cc_buddy_listings "
        "WHERE listing_id = $1 AND guild_id = $2",
        int(listing_id), int(guild_id),
    )
    if not row:
        raise ValueError(f"No listing with id `{listing_id}`.")
    if int(row["seller_user_id"]) != int(seller_id):
        raise ValueError("That listing isn't yours to cancel.")
    if str(row["status"]) != "active":
        raise ValueError(f"Listing is already `{row['status']}`.")
    if row.get("buddy_id") is not None:
        raise ValueError(
            "That's a buddy listing -- use `,buddy delist` instead."
        )

    payload_raw = row.get("egg_payload")
    if isinstance(payload_raw, str):
        try:
            payload = _json_mod.loads(payload_raw)
        except (ValueError, TypeError):
            payload = None
    else:
        payload = payload_raw
    if not isinstance(payload, dict):
        raise ValueError("Listing payload corrupt -- contact a moderator.")

    state = await db.fetch_one(
        "SELECT held_eggs FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        int(guild_id), int(seller_id),
    )
    held = list(_eggs_as_list((state or {}).get("held_eggs")))
    if len(held) >= int(fc.MAX_HELD_EGGS):
        raise ValueError(
            "Held-egg cap reached. Sell or hatch some before cancelling."
        )

    tier  = int((payload or {}).get("rarity_tier") or 1)
    sp    = str((payload or {}).get("species") or "")
    label = _egg_label(sp, tier)
    held.append(payload)

    async with db.atomic():
        cancelled = await db.fetch_one(
            "UPDATE cc_buddy_listings SET "
            "  status       = 'cancelled', "
            "  cancelled_at = NOW() "
            "WHERE listing_id = $1 AND status = 'active' "
            "RETURNING listing_id",
            int(listing_id),
        )
        if not cancelled:
            raise ValueError("Listing already closed -- refresh and try again.")

        await db.execute(
            "UPDATE user_fishing SET "
            "  held_eggs  = $3::jsonb, "
            "  updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            int(guild_id), int(seller_id), _jsonb(held),
        )

    return DelistResult(
        listing_id=int(listing_id),
        kind="egg",
        label=label,
    )


async def buy_listed_egg(
    db: Any, guild_id: int, buyer_id: int, listing_id: int,
) -> BuyResult:
    """Purchase an egg listing. Buyer pays full; seller gets asking - tax.
    Egg payload is rewritten with from='market_buy' and appended to the
    buyer's ``held_eggs``.
    """
    try:
        import configs.fishing_config as fc
    except Exception:
        log.exception("buy_listed_egg: fishing_config import failed")
        raise ValueError("Egg system unavailable -- try again shortly.")

    row = await db.fetch_one(
        "SELECT listing_id, seller_user_id, buddy_id, egg_payload, "
        "  asking_price_raw, status "
        "FROM cc_buddy_listings "
        "WHERE listing_id = $1 AND guild_id = $2",
        int(listing_id), int(guild_id),
    )
    if not row:
        raise ValueError(f"No listing with id `{listing_id}`.")
    if str(row["status"]) != "active":
        raise ValueError(f"Listing is `{row['status']}` -- not for sale.")
    if row.get("buddy_id") is not None:
        raise ValueError(
            "That's a buddy listing -- use `,buddy buy` instead."
        )
    seller_id = int(row["seller_user_id"])
    if seller_id == int(buyer_id):
        raise ValueError("Can't buy your own listing.")

    payload_raw = row.get("egg_payload")
    if isinstance(payload_raw, str):
        try:
            payload = _json_mod.loads(payload_raw)
        except (ValueError, TypeError):
            payload = None
    else:
        payload = payload_raw
    if not isinstance(payload, dict):
        raise ValueError("Listing payload corrupt -- contact a moderator.")

    state = await db.fetch_one(
        "SELECT held_eggs FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        int(guild_id), int(buyer_id),
    )
    held = list(_eggs_as_list((state or {}).get("held_eggs")))
    if len(held) >= int(fc.MAX_HELD_EGGS):
        raise ValueError(
            f"You're at the held-egg cap (**{fc.MAX_HELD_EGGS}**). "
            f"Hatch or sell some before buying more."
        )

    asking_raw = int(row["asking_price_raw"])
    tax_raw    = int((asking_raw * int(bc.BUDDY_MARKET_TAX_BPS)) // 10000)
    seller_raw = int(asking_raw - tax_raw)

    delivered = dict(payload)
    delivered["from"] = "market_buy"
    tier  = int((payload or {}).get("rarity_tier") or 1)
    sp    = str((payload or {}).get("species") or "")
    label = _egg_label(sp, tier)
    new_held = held + [delivered]

    async with db.atomic():
        # Egg listings are also BUD-denominated on the Buddy Network.
        # auto_buy_bud_for_market fills any shortfall from USD with the
        # standard mint impact applied.
        try:
            from services import buddy_economy as _bes
            await _bes.auto_buy_bud_for_market(
                db, int(guild_id), int(buyer_id), int(asking_raw),
            )
            await db.update_wallet_holding(
                int(buyer_id), int(guild_id),
                _bes.BUD_NETWORK_SHORT, _bes.BUD_SYMBOL,
                -int(asking_raw),
            )
        except ValueError as exc:
            raise ValueError(
                f"You can't afford **{to_human(asking_raw):,.4f} BUD** "
                f"(neither in your BUD wallet nor convertible from USD): {exc}"
            )
        if seller_raw > 0:
            await db.update_wallet_holding(
                int(seller_id), int(guild_id),
                _bes.BUD_NETWORK_SHORT, _bes.BUD_SYMBOL,
                int(seller_raw),
            )

        sold = await db.fetch_one(
            "UPDATE cc_buddy_listings SET "
            "  status        = 'sold', "
            "  buyer_user_id = $1, "
            "  sold_at       = NOW() "
            "WHERE listing_id = $2 AND status = 'active' "
            "RETURNING listing_id",
            int(buyer_id), int(listing_id),
        )
        if not sold:
            raise ValueError(
                "Listing was just snapped up by someone else. Try another."
            )

        await db.execute(
            "UPDATE user_fishing SET "
            "  held_eggs  = $3::jsonb, "
            "  updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            int(guild_id), int(buyer_id), _jsonb(new_held),
        )

        tx = await db.fetch_one(
            "INSERT INTO cc_buddy_transfers "
            "  (guild_id, from_user_id, to_user_id, egg_payload, "
            "   transfer_kind, price_raw, fee_raw) "
            "VALUES ($1, $2, $3, $4::jsonb, 'sale', $5, $6) "
            "RETURNING transfer_id",
            int(guild_id), int(seller_id), int(buyer_id),
            _jsonb(delivered), int(asking_raw), int(tax_raw),
        )

    return BuyResult(
        listing_id=int(listing_id),
        kind="egg",
        label=label,
        buddy_id=None,
        egg_payload=delivered,
        price_paid_raw=int(asking_raw),
        tax_paid_raw=int(tax_raw),
        seller_credited_raw=int(seller_raw),
        transfer_id=int((tx or {}).get("transfer_id") or 0),
    )


# ---- Browse / panel reads -------------------------------------------------


async def browse_listings(
    db: Any, guild_id: int, *,
    kind: str | None = None,
    species: str | None = None,
    rarity_tier: int | None = None,
    min_level: int | None = None,
    max_level: int | None = None,
    min_price_raw: int | None = None,
    max_price_raw: int | None = None,
    page: int = 1, per_page: int = 8,
) -> dict:
    """Return paginated active listings with optional filters.

    ``kind``: 'buddy' / 'egg' / None (both)
    ``species``: case-insensitive species key match. Applies to buddy
        listings via the cc_buddies JOIN AND to egg listings via the
        ``egg_payload->>'species'`` JSONB extract -- so a "wecco" filter
        with kind=None matches both buddy listings AND egg listings.
    ``rarity_tier``: integer 1..5; same dual-source semantics as species.
    ``min_level`` / ``max_level``: buddy-only filter. When set, the
        function automatically restricts to kind='buddy' (eggs have no
        level) so the user sees a coherent result set.
    ``min_price_raw`` / ``max_price_raw``: USD raw bounds; applies to
        both buddy and egg listings via ``l.asking_price_raw``.

    Returns ``{page, per_page, total, rows}`` -- same shape as before so
    the cog renderer doesn't change.
    """
    page     = max(1, int(page or 1))
    per_page = max(1, min(25, int(per_page or 8)))
    offset   = (page - 1) * per_page

    # Auto-restrict to buddies when a level filter is set -- eggs have
    # no level field and would otherwise drop out of the result set
    # silently because the SQL below NULL-checks the level column.
    if (min_level is not None or max_level is not None) and kind is None:
        kind = "buddy"

    where = ["l.guild_id = $1", "l.status = 'active'"]
    args: list[Any] = [int(guild_id)]

    def _bind(val: Any) -> str:
        """Add ``val`` to the args list and return its $N bind token."""
        args.append(val)
        return f"${len(args)}"

    if kind == "buddy":
        where.append("l.buddy_id IS NOT NULL")
    elif kind == "egg":
        where.append("l.buddy_id IS NULL")

    if species:
        sp = str(species).strip().lower()
        # Match buddy listings via JOINed cc_buddies.species OR egg
        # listings via egg_payload's species. The two halves are
        # mutually exclusive (buddy_id NULL XOR egg_payload NULL via
        # the migration's CHECK), so the OR can't double-count rows.
        sp_bind = _bind(sp)
        where.append(
            f"((l.buddy_id IS NOT NULL AND LOWER(b.species) = {sp_bind}) "
            f" OR (l.buddy_id IS NULL "
            f"     AND LOWER(l.egg_payload->>'species') = {sp_bind}))"
        )

    if rarity_tier is not None:
        rt = int(rarity_tier)
        rt_bind = _bind(rt)
        where.append(
            f"((l.buddy_id IS NOT NULL AND b.rarity_tier = {rt_bind}) "
            f" OR (l.buddy_id IS NULL "
            f"     AND (l.egg_payload->>'rarity_tier')::INTEGER = {rt_bind}))"
        )

    # Level filters apply only to buddy listings; the JOIN populates
    # b.level only when l.buddy_id IS NOT NULL, so the comparison is
    # naturally NULL-safe (NULL fails the >= / <= predicate).
    if min_level is not None:
        where.append(f"b.level >= {_bind(int(min_level))}")
    if max_level is not None:
        where.append(f"b.level <= {_bind(int(max_level))}")

    if min_price_raw is not None and int(min_price_raw) > 0:
        where.append(f"l.asking_price_raw >= {_bind(int(min_price_raw))}")
    if max_price_raw is not None and int(max_price_raw) > 0:
        where.append(f"l.asking_price_raw <= {_bind(int(max_price_raw))}")

    where_sql = " AND ".join(where)

    # COUNT query needs the same JOIN if any filter touches the
    # buddies table (species/tier/level all do via the b.* aliases).
    join_sql = ""
    if any(p in where_sql for p in ("b.species", "b.rarity_tier", "b.level")):
        join_sql = "LEFT JOIN cc_buddies b ON b.id = l.buddy_id"

    total_sql = (
        f"SELECT COUNT(*) FROM cc_buddy_listings l {join_sql} "
        f"WHERE {where_sql}"
    )
    total = int(await db.fetch_val(total_sql, *args) or 0)

    page_bind   = _bind(int(per_page))
    offset_bind = _bind(int(offset))
    rows = await db.fetch_all(
        f"""
        SELECT
            l.listing_id, l.seller_user_id, l.buddy_id, l.egg_payload,
            l.asking_price_raw, l.listed_at,
            b.species AS buddy_species, b.name AS buddy_name,
            b.rarity_tier AS buddy_tier, b.level AS buddy_level
        FROM cc_buddy_listings l
        LEFT JOIN cc_buddies b ON b.id = l.buddy_id
        WHERE {where_sql}
        ORDER BY l.listed_at DESC
        LIMIT {page_bind} OFFSET {offset_bind}
        """,
        *args,
    )

    out_rows = [_market_row_to_dict(r) for r in (rows or [])]
    return {
        "page":     int(page),
        "per_page": int(per_page),
        "total":    int(total),
        "rows":     out_rows,
    }


async def get_listing_by_id(
    db: Any, guild_id: int, listing_id: int,
) -> dict | None:
    """Single-row lookup for an active listing, in the same render shape
    ``browse_listings`` produces.

    Used by the cog's unified ``,buddy buy`` / ``,buddy delist`` dispatch
    so the cog can detect buddy-vs-egg from one row read instead of
    paginating the whole market.
    """
    row = await db.fetch_one(
        """
        SELECT
            l.listing_id, l.seller_user_id, l.buddy_id, l.egg_payload,
            l.asking_price_raw, l.listed_at,
            b.species AS buddy_species, b.name AS buddy_name,
            b.rarity_tier AS buddy_tier, b.level AS buddy_level
        FROM cc_buddy_listings l
        LEFT JOIN cc_buddies b ON b.id = l.buddy_id
        WHERE l.listing_id = $1 AND l.guild_id = $2 AND l.status = 'active'
        """,
        int(listing_id), int(guild_id),
    )
    return _market_row_to_dict(row) if row else None


async def get_user_listings(
    db: Any, guild_id: int, user_id: int,
) -> list[dict]:
    """Return ``user_id``'s own active listings for the mylistings panel."""
    rows = await db.fetch_all(
        """
        SELECT
            l.listing_id, l.seller_user_id, l.buddy_id, l.egg_payload,
            l.asking_price_raw, l.listed_at,
            b.species AS buddy_species, b.name AS buddy_name,
            b.rarity_tier AS buddy_tier, b.level AS buddy_level
        FROM cc_buddy_listings l
        LEFT JOIN cc_buddies b ON b.id = l.buddy_id
        WHERE l.guild_id = $1 AND l.seller_user_id = $2 AND l.status = 'active'
        ORDER BY l.listed_at DESC
        """,
        int(guild_id), int(user_id),
    )
    return [_market_row_to_dict(r) for r in (rows or [])]


def _market_row_to_dict(r: Any) -> dict:
    """Render a listing row (from browse_listings or get_user_listings) into
    the shared display dict the cog renderer consumes."""
    is_buddy = r.get("buddy_id") is not None
    if is_buddy:
        sp    = str(r.get("buddy_species") or "")
        nm    = str(r.get("buddy_name") or "")
        tier  = int(r.get("buddy_tier") or 1)
        level = int(r.get("buddy_level") or 1)
        return {
            "listing_id":       int(r["listing_id"]),
            "kind":             "buddy",
            "label":            _buddy_label(sp, nm, tier),
            "asking_price_raw": int(r["asking_price_raw"]),
            "seller_user_id":   int(r["seller_user_id"]),
            "listed_at":        r.get("listed_at"),
            "buddy_id":         int(r["buddy_id"]),
            "species":          sp,
            "rarity_tier":      tier,
            "level":            level,
            "name":             nm,
        }
    payload_raw = r.get("egg_payload")
    if isinstance(payload_raw, str):
        try:
            payload = _json_mod.loads(payload_raw)
        except (ValueError, TypeError):
            payload = {}
    else:
        payload = payload_raw or {}
    sp   = str((payload or {}).get("species") or "")
    tier = int((payload or {}).get("rarity_tier") or 1)
    return {
        "listing_id":       int(r["listing_id"]),
        "kind":             "egg",
        "label":            _egg_label(sp, tier),
        "asking_price_raw": int(r["asking_price_raw"]),
        "seller_user_id":   int(r["seller_user_id"]),
        "listed_at":        r.get("listed_at"),
        "buddy_id":         None,
        "species":          sp,
        "rarity_tier":      tier,
        "level":            0,
        "name":             "",
    }
