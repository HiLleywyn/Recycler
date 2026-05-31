"""
services/item_pricing.py  -  Pricing helpers for the item NFT layer.

Centralises three things the inspect / list / lexicon views all need:

* USD oracle conversion for any (currency, raw_amount) pair.
* "Last sold" + "average sold" + "best sold" stats per contract.
* Per-token best-effort price = last sold for the contract, falling
  back to base_price (catalog) when nothing has settled yet.

Public API:
    contract_price_summary(db, contract_id)
        -> {"last_sold_raw", "last_sold_currency", "last_sold_usd_raw",
            "last_sold_at", "avg_sold_usd_raw", "n_sales",
            "base_price_raw"}
    estimate_token_value_usd(db, token_row, contract_row)
        -> int | None  (USD raw scaled, or None when unknown)
    usd_value_raw(db, guild_id, currency, raw_amount)
        -> int | None  (USD raw scaled at current oracle, or None)
"""
from __future__ import annotations

import logging
from typing import Any

from core.framework.scale import to_human, to_raw

log = logging.getLogger(__name__)


async def usd_value_raw(
    db: Any, guild_id: int, currency: str, raw_amount: int,
) -> int | None:
    """Convert a raw-scaled amount in ``currency`` to USD raw.

    USD inputs round-trip unchanged. For any other symbol we read the
    oracle via ``db.get_price`` and multiply through. Returns None when
    the oracle isn't available or the symbol is unknown -- callers
    should treat that as "USD unknown" not zero.
    """
    cur = (currency or "").upper()
    raw = int(raw_amount or 0)
    if raw <= 0:
        return 0
    if cur == "USD":
        return raw
    try:
        row = await db.get_price(cur, guild_id)
    except Exception:
        return None
    if not row:
        return None
    p = float(row.get("price") or 0.0)
    if p <= 0:
        return None
    h = to_human(raw) * p
    return int(to_raw(max(0.0, h)))


async def contract_price_summary(
    db: Any, contract_id: int,
) -> dict:
    """Aggregate a contract's recent 'sold' events.

    Returns a dict with last/avg/best sale stats. All raw amounts are
    USD-scaled; ``last_sold_raw`` is the per-token slice in the listed
    currency at sale time.
    """
    out: dict = {
        "last_sold_raw":      None,
        "last_sold_currency": None,
        "last_sold_usd_raw":  None,
        "last_sold_at":       None,
        "avg_sold_usd_raw":   None,
        "best_sold_usd_raw":  None,
        "n_sales":            0,
        "base_price_raw":     None,
    }
    if not contract_id:
        return out
    contract = await db.fetch_one(
        "SELECT base_price_raw FROM item_contracts WHERE contract_id = $1",
        int(contract_id),
    )
    if contract and contract.get("base_price_raw") is not None:
        try:
            out["base_price_raw"] = int(contract["base_price_raw"])
        except (TypeError, ValueError):
            out["base_price_raw"] = None

    last = await db.fetch_one(
        """
        SELECT price_raw, currency, price_usd_raw, created_at
          FROM item_token_events
         WHERE contract_id = $1 AND event_type = 'sold'
         ORDER BY created_at DESC
         LIMIT 1
        """,
        int(contract_id),
    )
    if last:
        try:
            out["last_sold_raw"] = (
                int(last["price_raw"]) if last.get("price_raw") is not None else None
            )
        except (TypeError, ValueError):
            out["last_sold_raw"] = None
        out["last_sold_currency"] = (
            str(last["currency"]) if last.get("currency") else None
        )
        try:
            out["last_sold_usd_raw"] = (
                int(last["price_usd_raw"])
                if last.get("price_usd_raw") is not None else None
            )
        except (TypeError, ValueError):
            out["last_sold_usd_raw"] = None
        out["last_sold_at"] = last.get("created_at")

    agg = await db.fetch_one(
        """
        SELECT COUNT(*)              AS n,
               AVG(price_usd_raw)    AS avg_usd,
               MAX(price_usd_raw)    AS best_usd
          FROM item_token_events
         WHERE contract_id = $1 AND event_type = 'sold'
           AND price_usd_raw IS NOT NULL
        """,
        int(contract_id),
    )
    if agg:
        try:
            out["n_sales"] = int(agg.get("n") or 0)
        except (TypeError, ValueError):
            out["n_sales"] = 0
        for src, dst in (("avg_usd", "avg_sold_usd_raw"),
                         ("best_usd", "best_sold_usd_raw")):
            v = agg.get(src)
            if v is None:
                continue
            try:
                out[dst] = int(v)
            except (TypeError, ValueError):
                out[dst] = None
    return out


async def estimate_token_value_usd(
    db: Any, guild_id: int,
    contract_id: int | None,
    base_price_raw: int | None,
) -> int | None:
    """Best-effort per-token USD value (raw-scaled).

    Priority: last sale's USD snapshot -> contract base_price_raw
    (already USD-raw per items_config) -> None. Used to populate the
    USD column in ``,items`` overview / ``,items list`` rows.
    """
    if contract_id:
        summary = await contract_price_summary(db, int(contract_id))
        if summary.get("last_sold_usd_raw") is not None:
            return int(summary["last_sold_usd_raw"])
        if summary.get("base_price_raw") is not None:
            return int(summary["base_price_raw"])
    if base_price_raw is not None:
        try:
            return int(base_price_raw)
        except (TypeError, ValueError):
            return None
    return None


async def render_catalog_price(
    db: Any, guild_id: int, contract_row: dict,
) -> tuple[str, int | None]:
    """Single source of truth for "what's this contract's catalog price?"

    Reads the contract's native + USD columns and returns a display
    string plus the best-effort USD raw value (for sums / sort order).

    Display format mirrors the AH:
      - native + USD oracle:   ``‘120.00 RUNE’  ·  ‘$45.21’``
      - native only:           ``‘120.00 RUNE’``
      - USD only (shop/stone): ``‘$45.21’``
      - neither:               ``“”`` (empty -- caller decides skip)

    USD raw priority for the second return value:
      1. ``base_price_raw`` (USD-pegged catalog -- shop / stone).
      2. ``base_price_native_raw`` converted via current oracle.
      3. None when neither side has a price.

    All amounts are raw 10^18; conversions go through ``to_human``.
    """
    if not contract_row:
        return "", None

    native_raw = contract_row.get("base_price_native_raw")
    native_cur = contract_row.get("base_price_currency")
    usd_raw = contract_row.get("base_price_raw")

    try:
        native_raw = int(native_raw) if native_raw is not None else None
    except (TypeError, ValueError):
        native_raw = None
    try:
        usd_raw = int(usd_raw) if usd_raw is not None else None
    except (TypeError, ValueError):
        usd_raw = None
    native_cur = (
        str(native_cur).upper().strip() if native_cur else None
    ) or None

    bits: list[str] = []
    if native_raw is not None and native_cur:
        bits.append(f"`{to_human(native_raw):,.2f} {native_cur}`")

    # Pick the USD raw value to display + return.
    usd_for_return: int | None = None
    if usd_raw is not None and usd_raw > 0:
        usd_for_return = usd_raw
    elif native_raw is not None and native_cur and native_cur != "USD":
        # Convert native -> USD via oracle. Cheap; cached upstream.
        converted = await usd_value_raw(db, guild_id, native_cur, native_raw)
        if converted is not None:
            usd_for_return = converted

    if usd_for_return is not None and usd_for_return > 0:
        bits.append(f"`${to_human(usd_for_return):,.2f}`")

    return "  ·  ".join(bits), usd_for_return
