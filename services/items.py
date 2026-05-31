"""
services/items.py  -  NFT-style item-instance layer.

Every ownable item in the bot can be referenced by a stable
``<network>:<hex>`` token id. Format examples::

    bud:k889kak       -- a buddy
    reel:81819kak     -- a fish (Lure Network item)
    hrv:af09c12       -- a crop (Harvest Network item)
    rune:b3d201c      -- an ore stack (Crypt Network)
    fge:9931ee2       -- a crafted item (Forge Network)

The hex part is content-derived from ``(source_table, source_id, salt)``
so the same source row always resolves to the same token id -- IDs are
deterministic, not random. That keeps the auction house, achievement
"this exact buddy" links, and any future NFT-style transfers stable
across restarts and migrations.

Public API:
    KIND_NETWORK_DEFAULTS -- mapping of kind -> default network
    mint_token(db, guild_id, kind, source_table, source_id, **fields) -> dict
    get_token(db, token_id) -> dict | None
    set_owner(db, token_id, owner_user_id, listing_id=None)
    set_listing(db, token_id, listing_id)
    transfer(db, token_id, new_owner_user_id)
    short_id(token_id)         -- pretty-print helper
    parse_id(token_id)         -- (network, hex) tuple
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from typing import Any

log = logging.getLogger(__name__)


# Default network per item kind. Items "bought with USD" (like buddies
# from the original buddy market) use the closest related crypto
# network -- buddies live on the Buddy Network so they're 'bud', delve
# gear on the Crypt Network so 'rune', etc. Override at mint time when
# the source row carries a more specific network.
KIND_NETWORK_DEFAULTS: dict[str, str] = {
    "buddy":      "bud",
    "egg":        "bud",
    "fish":       "lur",
    "crop":       "har",
    "ore":        "cry",
    "weapon":     "cry",
    "armor":      "cry",
    "consumable": "cry",
    "crafted":    "fge",
    "token":      "fge",   # generic fallback
}


# 8-character salt mixed into every hex so token ids can't be guessed by
# diffing source rows alone. Fixed-at-deploy is fine -- this is integrity
# obscurity, not crypto.
_TOKEN_SALT = "a8c3-d9f2-discoin-auction-2026"


def _hex_for(source_table: str, source_id: str | int, network: str) -> str:
    """Deterministic 8-char hex for this source row.

    Uses BLAKE2b (fast, available stdlib) over salt + network + table +
    id. We slice the digest to 8 hex chars -- that's 32 bits ~= 4B token
    space per network, plenty for any single-game catalog.
    """
    h = hashlib.blake2b(digest_size=8)
    h.update(_TOKEN_SALT.encode("utf-8"))
    h.update(b"|")
    h.update(network.lower().encode("utf-8"))
    h.update(b"|")
    h.update(source_table.encode("utf-8"))
    h.update(b"|")
    h.update(str(source_id).encode("utf-8"))
    return h.hexdigest()[:8]


def _build_token_id(network: str, source_table: str, source_id: str | int) -> str:
    return f"{network.lower()}:{_hex_for(source_table, source_id, network)}"


def parse_id(token_id: str) -> tuple[str, str]:
    """Split ``"bud:k889kak"`` into ``("bud", "k889kak")``.

    Raises ``ValueError`` for malformed strings so callers can fail fast.
    """
    s = (token_id or "").strip().lower()
    if ":" not in s:
        raise ValueError(f"Bad token id `{token_id}` (expected '<net>:<hex>').")
    net, hx = s.split(":", 1)
    if not net or not hx or not all(c in "0123456789abcdef" for c in hx):
        raise ValueError(f"Bad token id `{token_id}`.")
    return net, hx


def short_id(token_id: str) -> str:
    """Compact display form: keeps the prefix, shortens the hex tail.

    ``bud:k889kakabcd`` -> ``bud:k889..bcd`` for any tail >= 8 chars.
    """
    try:
        net, hx = parse_id(token_id)
    except ValueError:
        return token_id or "?"
    if len(hx) <= 8:
        return f"{net}:{hx}"
    return f"{net}:{hx[:4]}..{hx[-3:]}"


async def get_token(db: Any, token_id: str) -> dict | None:
    """Read the item_instances row for ``token_id`` or None if missing."""
    return await db.fetch_one(
        "SELECT * FROM item_instances WHERE token_id = $1",
        str(token_id or "").lower(),
    )


async def find_token(
    db: Any, source_table: str, source_id: str | int, network: str | None = None,
) -> dict | None:
    """Reverse-lookup: find the token id for an existing source row.

    Useful for "has this buddy ever been minted?" checks. None when the
    source has no ``item_instances`` row yet.
    """
    return await db.fetch_one(
        "SELECT * FROM item_instances "
        "WHERE source_table = $1 AND source_id = $2 "
        "  AND ($3::text IS NULL OR network = $3) "
        "LIMIT 1",
        str(source_table), str(source_id),
        str(network or "").lower() or None,
    )


async def mint_token(
    db: Any,
    *,
    guild_id: int,
    kind: str,
    source_table: str,
    source_id: str | int,
    owner_user_id: int | None = None,
    network: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Idempotent: mint a token id for the given source row, or return
    the existing row if one was already minted.

    The hex is content-derived so re-minting the same source returns the
    same id. Concurrent mints converge to one row via the PK on
    ``token_id``; the ``ON CONFLICT (token_id) DO UPDATE`` clause re-
    asserts the latest owner / metadata / listing state.
    """
    if not kind:
        raise ValueError("mint_token: kind required")
    net = (
        str(network or "").lower()
        or KIND_NETWORK_DEFAULTS.get(kind, "fge")
    )
    token_id = _build_token_id(net, str(source_table), source_id)
    md_json = json.dumps(metadata or {})
    row = await db.fetch_one(
        """
        INSERT INTO item_instances (
            token_id, guild_id, network, kind,
            source_table, source_id, owner_user_id, metadata
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        ON CONFLICT (token_id) DO UPDATE SET
            owner_user_id = COALESCE(EXCLUDED.owner_user_id, item_instances.owner_user_id),
            metadata      = item_instances.metadata || EXCLUDED.metadata,
            updated_at    = NOW()
        RETURNING *
        """,
        token_id, int(guild_id), net, str(kind),
        str(source_table), str(source_id),
        int(owner_user_id) if owner_user_id is not None else None,
        md_json,
    )
    return dict(row) if row else {}


async def set_owner(
    db: Any, token_id: str, owner_user_id: int | None,
    *, listing_id: int | None | type[Ellipsis] = ...,
) -> None:
    """Update the current owner (and optionally the active listing fk).

    ``listing_id=None`` clears the listing pointer; ``listing_id=...``
    (the default) leaves it unchanged. ``owner_user_id=None`` flags the
    item as escrowed by an auction listing.
    """
    if listing_id is ...:
        await db.execute(
            "UPDATE item_instances SET owner_user_id = $2, updated_at = NOW() "
            "WHERE token_id = $1",
            str(token_id).lower(),
            int(owner_user_id) if owner_user_id is not None else None,
        )
    else:
        await db.execute(
            "UPDATE item_instances SET owner_user_id = $2, "
            "listing_id = $3, updated_at = NOW() "
            "WHERE token_id = $1",
            str(token_id).lower(),
            int(owner_user_id) if owner_user_id is not None else None,
            int(listing_id) if listing_id is not None else None,
        )


async def transfer(
    db: Any, token_id: str, new_owner_user_id: int,
) -> None:
    """Reassign ownership. Logs a 'transfer' event with the from/to
    pair so the token's history walks correctly.
    """
    cur = await get_token(db, token_id)
    from_id = (
        int(cur["owner_user_id"])
        if cur and cur.get("owner_user_id") is not None else None
    )
    await set_owner(
        db, token_id, int(new_owner_user_id), listing_id=None,
    )
    try:
        await log_event(
            db,
            token_id=token_id,
            event_type="transfer",
            contract_id=int(cur["contract_id"]) if cur and cur.get("contract_id") else None,
            from_user_id=from_id,
            to_user_id=int(new_owner_user_id),
        )
    except Exception:
        log.debug("log_event(transfer) failed token=%s", token_id, exc_info=True)


async def random_hex(n: int = 8) -> str:
    """Generate a cryptographically-random hex string of ``n`` chars.

    For one-shot mints where there's no source row to derive from
    (e.g. ad-hoc auction-house listings of bare token amounts).
    """
    return secrets.token_hex(max(1, n // 2))[:n]


# ─── Event log ──────────────────────────────────────────────────────────────
#
# Every state transition on a token (mint / transfer / list / unlist /
# sold / burn) writes one row to ``item_token_events``. The inspect
# view walks a single token's events forward; the lexicon's price
# history aggregates 'sold' events per-contract.


async def log_event(
    db: Any,
    *,
    token_id: str,
    event_type: str,
    contract_id: int | None = None,
    from_user_id: int | None = None,
    to_user_id: int | None = None,
    listing_id: int | None = None,
    price_raw: int | None = None,
    currency: str | None = None,
    price_usd_raw: int | None = None,
    gas_raw: int | None = None,
    gas_currency: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Append one event to ``item_token_events``.

    Best-effort by callers: wrap the call in try/except so an event-log
    write failure never breaks the underlying mint / transfer / sale.
    """
    md_json = json.dumps(metadata or {})
    cid = (
        int(contract_id) if contract_id is not None else None
    )
    if cid is None:
        # Fall back to looking it up on item_instances. Cheap.
        try:
            row = await db.fetch_one(
                "SELECT contract_id FROM item_instances WHERE token_id = $1",
                str(token_id).lower(),
            )
            cid = (
                int(row["contract_id"]) if row and row.get("contract_id")
                else None
            )
        except Exception:
            cid = None
    await db.execute(
        """
        INSERT INTO item_token_events (
            token_id, contract_id, event_type,
            from_user_id, to_user_id, listing_id,
            price_raw, currency, price_usd_raw,
            gas_raw, gas_currency, metadata
        )
        VALUES (
            $1, $2, $3,
            $4, $5, $6,
            $7::numeric, $8, $9::numeric,
            $10::numeric, $11, $12::jsonb
        )
        """,
        str(token_id).lower(), cid, str(event_type),
        int(from_user_id) if from_user_id is not None else None,
        int(to_user_id) if to_user_id is not None else None,
        int(listing_id) if listing_id is not None else None,
        str(int(price_raw)) if price_raw is not None else None,
        str(currency) if currency else None,
        str(int(price_usd_raw)) if price_usd_raw is not None else None,
        str(int(gas_raw)) if gas_raw is not None else None,
        str(gas_currency) if gas_currency else None,
        md_json,
    )


# ─── Gas fees ───────────────────────────────────────────────────────────────
#
# Every player-initiated state transition on a token (transfer / list
# / unlist / sold) pays a flat gas fee in the network's native coin.
# The fee is charged BEFORE the underlying state change so a debit
# failure cleanly blocks the transition (no half-applied state).
#
# Mints (catch / harvest / craft / shop / hatch) and burns (consumed
# in gameplay) don't pay gas -- those are gameplay outcomes, not
# player-signed transactions.

# Network short -> native gas coin. Mirrors items_config.SHOP_ITEMS
# pricing currencies + the catalog tradeable token per network.
_NETWORK_GAS_COIN: dict[str, str] = {
    "bud":  "BUD",
    "lur":  "LURE",
    "har":  "HRV",
    "cry":  "RUNE",
    "fge":  "INGOT",
    "dsc":  "DSC",
    "arc":  "ARC",
    "mta":  "MTA",
    "sun":  "SUN",
    "moon": "MOON",
}

# Network short -> wallet_holdings.network short (most match the
# token short directly; lure has historic 'lur' wallet rows).
_NETWORK_WALLET_NET: dict[str, str] = {
    "bud":  "bud",
    "lur":  "lur",
    "har":  "har",
    "cry":  "cry",
    "fge":  "fge",
    "dsc":  "dsc",
    "arc":  "arc",
    "mta":  "mta",
    "sun":  "sun",
    "moon": "moon",
}

# Per-event gas amount, in human units of the native coin. Tuned so
# gifting a stack or making a few listings doesn't drain the player.
GAS_FEES: dict[str, float] = {
    "transfer": 0.01,    # gift one token
    "list":     0.05,    # post to auction house
    "unlist":   0.01,    # cancel an auction listing
    "sold":     0.10,    # paid by BUYER on auction settle
}


def gas_coin_for_network(network_short: str) -> str | None:
    """Return the native gas coin symbol for a network short code,
    or None when unknown.
    """
    return _NETWORK_GAS_COIN.get((network_short or "").lower())


def gas_amount_for(event_type: str) -> float:
    """Per-event gas fee in human units of the network's coin. Returns
    0.0 for events that don't pay gas (mint / burn).
    """
    return float(GAS_FEES.get((event_type or "").lower(), 0.0))


async def charge_gas(
    db: Any,
    *,
    guild_id: int,
    payer_user_id: int,
    network_short: str,
    event_type: str,
) -> tuple[int, str] | None:
    """Debit gas from ``payer_user_id``'s wallet for one event.

    Returns ``(gas_raw, currency)`` so the caller can stamp it on the
    event log. Returns None when the event type doesn't pay gas. Raises
    ``ValueError("Insufficient ... balance")`` (re-thrown from
    ``update_wallet_holding``) on a failed debit -- the caller should
    surface that to the player and abort the transition.
    """
    fee_h = gas_amount_for(event_type)
    if fee_h <= 0:
        return None
    coin = gas_coin_for_network(network_short)
    if not coin:
        # Unknown network -- don't block, but also don't silently grant
        # free transitions. Return None so the caller logs no gas.
        log.debug(
            "charge_gas: no native coin for network=%s (event=%s)",
            network_short, event_type,
        )
        return None
    wallet_net = _NETWORK_WALLET_NET.get(
        (network_short or "").lower(), (network_short or "").lower(),
    )
    # Lazy import: core.framework.scale is the canonical raw <-> human helper.
    from core.framework.scale import to_raw as _to_raw
    fee_raw = int(_to_raw(fee_h))
    if fee_raw <= 0:
        return None
    try:
        await db.update_wallet_holding(
            int(payer_user_id), int(guild_id),
            wallet_net, coin, -int(fee_raw),
        )
    except ValueError:
        raise ValueError(
            f"Need **{fee_h:,.4f} {coin}** for gas to complete this "
            f"{event_type}. Top up your {coin} wallet first."
        )
    return (int(fee_raw), str(coin))


async def get_token_events(
    db: Any, token_id: str, *, limit: int = 25,
) -> list[dict]:
    """Return events for one token, oldest first, capped at ``limit``."""
    rows = await db.fetch_all(
        """
        SELECT * FROM item_token_events
         WHERE token_id = $1
         ORDER BY event_id ASC
         LIMIT $2
        """,
        str(token_id).lower(), int(limit),
    )
    return [dict(r) for r in (rows or [])]


async def get_contract_sales(
    db: Any, contract_id: int, *, limit: int = 25,
) -> list[dict]:
    """Recent 'sold' events for one contract, newest first."""
    rows = await db.fetch_all(
        """
        SELECT * FROM item_token_events
         WHERE contract_id = $1 AND event_type = 'sold'
         ORDER BY created_at DESC
         LIMIT $2
        """,
        int(contract_id), int(limit),
    )
    return [dict(r) for r in (rows or [])]


# ─── Contract registry ──────────────────────────────────────────────────────
#
# A "contract" is the type-level deploy ("WormBait", "BronzeSword",
# "ZennyEgg"). Tokens minted from a contract are the per-unit instances
# ("worm bait #5739"). Migration 0176 adds the ``contracts`` table; the
# helpers below provide the public API.
#
# Contract addresses are dotted lower-case strings shaped like
# ``<kind>.<catalog_key>``: e.g. ``bait.worm``, ``weapon.bronze_sword``,
# ``egg.zenny``. They're stable across restarts and used as the lookup
# key when minting per-unit tokens.


def contract_address(kind: str, catalog_key: str) -> str:
    """Canonical contract address for a (kind, catalog_key) pair.

    Lower-cased and shaped to match the
    ``contracts_address_shape_chk`` regex in migration 0176. Spaces in
    the catalog key are turned into underscores so display names like
    ``"Bronze Sword"`` collapse to ``weapon.bronze_sword``.
    """
    k = (kind or "").strip().lower()
    key = (catalog_key or "").strip().lower().replace(" ", "_").replace("-", "_")
    return f"{k}.{key}"


async def get_contract(
    db: Any, *, address: str | None = None, contract_id: int | None = None,
) -> dict | None:
    """Read a contract row by address (preferred) or numeric id."""
    if contract_id is not None:
        row = await db.fetch_one(
            "SELECT * FROM item_contracts WHERE contract_id = $1",
            int(contract_id),
        )
        return dict(row) if row else None
    if address is None:
        return None
    row = await db.fetch_one(
        "SELECT * FROM item_contracts WHERE address = $1",
        str(address).lower(),
    )
    return dict(row) if row else None


async def upsert_contract(
    db: Any,
    *,
    kind: str,
    catalog_key: str,
    name: str,
    network: str | None = None,
    rarity_tier: int | None = None,
    base_price_raw: int | None = None,
    base_price_native_raw: int | None = None,
    base_price_currency: str | None = None,
    emoji: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Idempotent contract deploy.

    Creates the contract row if missing, refreshes display fields if
    present. Returns the upserted row. Used by the startup bootstrap
    that walks every catalog dict.

    ``base_price_raw`` is the USD-pegged catalog price (raw 10^18).
    ``base_price_native_raw`` + ``base_price_currency`` carry the
    catalog price in its native token (e.g. ``1 REEL`` for worm bait,
    ``60 RUNE`` for diamond pickaxe oil). Either column can be NULL on
    contracts whose catalog doesn't quote a price.
    """
    if not kind or not catalog_key:
        raise ValueError("upsert_contract: kind + catalog_key required")
    addr = contract_address(kind, catalog_key)
    net = (network or "").lower() or KIND_NETWORK_DEFAULTS.get(kind, "fge")
    md_json = json.dumps(metadata or {})
    cur_norm = (base_price_currency or "").upper() or None
    row = await db.fetch_one(
        """
        INSERT INTO item_contracts (
            address, network, kind, catalog_key, name,
            rarity_tier, base_price_raw,
            base_price_native_raw, base_price_currency,
            emoji, metadata
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::numeric,
                $8::numeric, $9, $10, $11::jsonb)
        ON CONFLICT (address) DO UPDATE SET
            network               = EXCLUDED.network,
            name                  = EXCLUDED.name,
            rarity_tier           = COALESCE(EXCLUDED.rarity_tier, item_contracts.rarity_tier),
            base_price_raw        = COALESCE(EXCLUDED.base_price_raw, item_contracts.base_price_raw),
            base_price_native_raw = COALESCE(EXCLUDED.base_price_native_raw, item_contracts.base_price_native_raw),
            base_price_currency   = COALESCE(EXCLUDED.base_price_currency, item_contracts.base_price_currency),
            emoji                 = COALESCE(EXCLUDED.emoji, item_contracts.emoji),
            metadata              = item_contracts.metadata || EXCLUDED.metadata,
            updated_at            = NOW()
        RETURNING *
        """,
        addr, net, kind, str(catalog_key).lower(), str(name),
        int(rarity_tier) if rarity_tier is not None else None,
        str(int(base_price_raw)) if base_price_raw is not None else None,
        (
            str(int(base_price_native_raw))
            if base_price_native_raw is not None else None
        ),
        cur_norm,
        str(emoji or "") or None,
        md_json,
    )
    return dict(row) if row else {}


async def list_contracts(
    db: Any, *, kind: str | None = None,
) -> list[dict]:
    """Return every deployed contract, optionally filtered by kind."""
    if kind:
        rows = await db.fetch_all(
            "SELECT * FROM item_contracts WHERE kind = $1 ORDER BY address",
            str(kind).lower(),
        )
    else:
        rows = await db.fetch_all(
            "SELECT * FROM item_contracts ORDER BY kind, catalog_key",
        )
    return [dict(r) for r in (rows or [])]


# ─── Per-unit minting ───────────────────────────────────────────────────────
#
# Every individual unit of an item gets its own token row. A stack of 50
# COPPER becomes 50 rows in item_instances, each with a unique token_id,
# its own unit_index inside the contract, and metadata captured at mint
# time (e.g. the lbs of a fish, the level of a weapon).
#
# The token_id hex is content-derived from (contract_address, unit_index)
# so the same (contract, unit) pair always resolves to the same id, even
# across restarts and replays. Deterministic ids make the auction house,
# transfer logs, and per-token achievements stable.


async def _next_unit_index(db: Any, contract_id: int) -> int:
    """Allocate the next unit_index for a contract.

    A simple ``COUNT + 1`` is racy under concurrent mints. We use
    ``MAX(unit_index) + 1`` inside a row-locking SELECT FOR UPDATE on
    the contract row so two parallel mint_unit calls serialise.
    """
    # Row-lock the contract so concurrent mints serialise. The contract
    # row is the natural lock subject -- one row per contract.
    # Use fetch_val with FOR UPDATE so asyncpg actually executes a
    # row-returning SELECT and the lock is held for the duration of the
    # implicit transaction (db.execute() on a SELECT is a no-op for
    # row-locking semantics on some asyncpg paths).
    await db.fetch_val(
        "SELECT contract_id FROM item_contracts "
        "WHERE contract_id = $1 FOR UPDATE",
        int(contract_id),
    )
    last = await db.fetch_val(
        "SELECT COALESCE(MAX(unit_index), 0) FROM item_instances "
        "WHERE contract_id = $1",
        int(contract_id),
    )
    return int(last or 0) + 1


async def mint_unit(
    db: Any,
    *,
    guild_id: int,
    contract_address: str,
    owner_user_id: int | None,
    metadata: dict | None = None,
    mint_source: str = "runtime",
    source_table: str = "",
    source_id: str | int = "",
) -> dict:
    """Mint a fresh per-unit token from a contract.

    Returns the new ``item_instances`` row. Allocates a unit_index off
    the contract's monotonic counter, derives a deterministic token_id
    from (contract_address, unit_index), and stamps owner / metadata /
    mint_source onto the row.

    ``source_table`` / ``source_id`` are optional and only used when the
    backing inventory keeps a separate row (e.g. ``cc_buddies`` for
    buddies). Pure JSONB-count inventories (bait, ore, crops) leave
    these empty -- the token row IS the canonical inventory entry.
    """
    addr = str(contract_address).lower()
    contract = await get_contract(db, address=addr)
    if not contract:
        raise ValueError(f"mint_unit: unknown contract `{addr}`")

    cid = int(contract["contract_id"])
    net = str(contract["network"])
    kind = str(contract["kind"])

    unit_index = await _next_unit_index(db, cid)
    token_id = _build_token_id(net, addr, unit_index)
    md = dict(metadata or {})
    md.setdefault("contract", addr)
    md.setdefault("unit_index", unit_index)
    md_json = json.dumps(md)

    row = await db.fetch_one(
        """
        INSERT INTO item_instances (
            token_id, guild_id, network, kind,
            source_table, source_id, owner_user_id,
            contract_id, unit_index, mint_source,
            minted_at, metadata
        )
        VALUES (
            $1, $2, $3, $4,
            $5, $6, $7,
            $8, $9, $10,
            NOW(), $11::jsonb
        )
        ON CONFLICT (token_id) DO UPDATE SET
            owner_user_id = COALESCE(EXCLUDED.owner_user_id, item_instances.owner_user_id),
            metadata      = item_instances.metadata || EXCLUDED.metadata,
            updated_at    = NOW()
        RETURNING *
        """,
        token_id, int(guild_id), net, kind,
        str(source_table or ""), str(source_id or ""),
        int(owner_user_id) if owner_user_id is not None else None,
        cid, int(unit_index), str(mint_source),
        md_json,
    )
    # Append a 'mint' event to the token log. Best-effort.
    try:
        await log_event(
            db,
            token_id=token_id,
            event_type="mint",
            contract_id=cid,
            to_user_id=owner_user_id,
            metadata={"mint_source": str(mint_source)},
        )
    except Exception:
        log.debug("log_event(mint) failed token=%s", token_id, exc_info=True)
    return dict(row) if row else {}


async def burn_unit(
    db: Any, token_id: str, *, reason: str = "consumed",
) -> bool:
    """Mark a token as burned. Append-only -- the row stays.

    Burning clears owner_user_id and listing_id, sets burned_at = NOW(),
    and stamps the burn reason into metadata. Returns True if a row was
    affected. Idempotent: burning an already-burned token is a no-op.
    """
    cur = await get_token(db, token_id)
    from_id = (
        int(cur["owner_user_id"])
        if cur and cur.get("owner_user_id") is not None else None
    )
    cid = (
        int(cur["contract_id"]) if cur and cur.get("contract_id") else None
    )
    row = await db.fetch_one(
        """
        UPDATE item_instances
           SET burned_at     = NOW(),
               owner_user_id = NULL,
               listing_id    = NULL,
               metadata      = metadata || jsonb_build_object('burn_reason', $2::text),
               updated_at    = NOW()
         WHERE token_id = $1 AND burned_at IS NULL
        RETURNING token_id
        """,
        str(token_id).lower(), str(reason or "consumed"),
    )
    if row is not None:
        try:
            await log_event(
                db,
                token_id=token_id,
                event_type="burn",
                contract_id=cid,
                from_user_id=from_id,
                metadata={"reason": str(reason or "consumed")},
            )
        except Exception:
            log.debug("log_event(burn) failed token=%s", token_id, exc_info=True)
    return row is not None


async def consume_one(
    db: Any,
    *,
    guild_id: int,
    user_id: int,
    contract_address: str,
    reason: str = "consumed",
) -> dict | None:
    """Burn the oldest unit of a contract owned by a user.

    Returns the burned ``item_instances`` row, or None if the user has
    no unburned units of that contract. "Oldest first" is FIFO by
    unit_index so consumption order is stable + deterministic.
    """
    addr = str(contract_address).lower()
    contract = await get_contract(db, address=addr)
    if not contract:
        raise ValueError(f"consume_one: unknown contract `{addr}`")
    cid = int(contract["contract_id"])

    target = await db.fetch_one(
        """
        SELECT token_id FROM item_instances
         WHERE guild_id = $1 AND owner_user_id = $2
           AND contract_id = $3 AND burned_at IS NULL
         ORDER BY unit_index ASC
         LIMIT 1
        """,
        int(guild_id), int(user_id), cid,
    )
    if not target:
        return None
    ok = await burn_unit(db, str(target["token_id"]), reason=reason)
    if not ok:
        return None
    return await get_token(db, str(target["token_id"]))


async def count_owned(
    db: Any,
    *,
    guild_id: int,
    user_id: int,
    contract_address: str,
) -> int:
    """Live count of unburned tokens owned by a user under a contract."""
    addr = str(contract_address).lower()
    contract = await get_contract(db, address=addr)
    if not contract:
        return 0
    return int(await db.fetch_val(
        """
        SELECT COUNT(*) FROM item_instances
         WHERE guild_id = $1 AND owner_user_id = $2
           AND contract_id = $3 AND burned_at IS NULL
        """,
        int(guild_id), int(user_id), int(contract["contract_id"]),
    ) or 0)


# ─── Lazy mint from JSONB inventories ────────────────────────────────────────
#
# Most player inventories live in JSONB count-maps on per-feature tables
# (user_dungeon.weapons_owned, user_fishing.bait_inventory, ...). The
# original NFT design assumed acquisition paths would always mint at the
# same time -- but several dungeon paths (boss loot, junk drops, relic
# drops, consumable drops) only update the JSONB and never mint, leaving
# the player with items they own in the inventory display but can't list
# on the auction house ("Couldn't find an owned NFT matching ...").
#
# ``lazy_mint_from_jsonb`` is the rescue path. When the AH bare-name
# resolver finds a contract but no NFT, it asks this helper to look the
# catalog_key up in the right JSONB column and mint one unit on the fly,
# decrementing the count atomically. The minted token then flows into
# create_listing exactly like any other NFT.
#
# The mapping is intentionally compact -- adding a new kind is one tuple
# in ``_LAZY_INV_PLAN``. Kinds that don't have a JSONB inventory (ore
# is fungible / wallet-held; buddies live in cc_buddies; eggs in held_eggs
# list which is structured) are absent and the helper returns None.

# (table, column, [...]) -- the helper tries each column for the kind
# until it finds a non-zero count for ``catalog_key``. Multiple rows
# means the kind spans tables (e.g. junk: dungeon JUNK + fishing JUNK,
# or crafted outputs that route into per-feature inventories via
# ``apply: <surface>/<key>`` in crafting_config.CRAFT_ITEMS).
_LAZY_INV_PLAN: dict[str, tuple[tuple[str, str], ...]] = {
    "weapon":     (("user_dungeon",  "weapons_owned"),),
    "armor":      (("user_dungeon",  "armor_owned"),),
    "consumable": (("user_dungeon",  "consumables"),),
    "relic":      (("user_dungeon",  "relics_owned"),),
    "junk":       (
        ("user_dungeon",  "junk_inventory"),  # dungeon JUNK -- mats / salvage
        ("user_fishing",  "junk_inventory"),  # fishing JUNK -- boots, cans
    ),
    "bait":       (
        ("user_fishing",  "bait_inventory"),
    ),
    "crop":       (("user_farming",  "crop_inventory"),),
    # Crafted contracts cover both raw crafted outputs (kept in
    # user_crafting.crafted_inventory until the player applies them)
    # AND crafted items that have already been routed to a per-feature
    # inventory via ``apply: bait/*`` (-> user_fishing.bait_inventory),
    # ``apply: consum/*`` (-> user_dungeon.consumables), or
    # ``apply: fert/*`` (-> user_farming.fertilizer_inventory). The lazy
    # mint walks all four so ,ah list <name> resolves the same crafted
    # contract regardless of where the player is currently storing it.
    "crafted":    (
        ("user_crafting", "crafted_inventory"),
        ("user_dungeon",  "consumables"),
        ("user_fishing",  "bait_inventory"),
        ("user_farming",  "fertilizer_inventory"),
    ),
}


async def lazy_mint_from_jsonb(
    db: Any,
    *,
    guild_id: int,
    user_id: int,
    contract: dict,
) -> dict | None:
    """Mint one token by consuming one count from the player's JSONB
    inventory for this contract's (kind, catalog_key).

    Returns the new ``item_instances`` row, or ``None`` if the player
    doesn't have any of this item in any candidate JSONB column.

    Idempotent under crash: the JSONB decrement and the mint happen in
    the same transaction so either both succeed or both roll back. The
    decrement removes the key entirely when its count drops to zero so
    the inventory display stays clean.
    """
    kind = str(contract.get("kind") or "").lower()
    catalog_key = str(contract.get("catalog_key") or "").lower()
    if not kind or not catalog_key:
        return None
    plan = _LAZY_INV_PLAN.get(kind)
    if not plan:
        return None
    addr = str(contract.get("address") or contract_address(kind, catalog_key))

    # Walk candidate (table, column) pairs in order and lazy-mint from
    # the first one with a non-zero count. ``db.atomic()`` is the
    # framework's single-transaction wrapper; the SELECT...FOR UPDATE
    # + UPDATE + mint all fire inside it so a crash mid-flight either
    # rolls back the inventory decrement or doesn't mint.
    for table, column in plan:
        try:
            async with db.atomic() as conn:
                row = await conn.fetchrow(
                    f"SELECT {column} AS inv FROM {table} "
                    f"WHERE guild_id = $1 AND user_id = $2 "
                    f"FOR UPDATE",
                    int(guild_id), int(user_id),
                )
                if not row:
                    continue
                inv = row.get("inv") or {}
                if isinstance(inv, str):
                    try:
                        inv = json.loads(inv)
                    except Exception:
                        inv = {}
                cnt = 0
                try:
                    cnt = int(inv.get(catalog_key) or 0)
                except (TypeError, ValueError):
                    cnt = 0
                if cnt <= 0:
                    continue
                # Decrement -- remove the key entirely when it hits zero
                # so the inventory display doesn't show "0× phoenix talon"
                new_inv = dict(inv)
                if cnt - 1 <= 0:
                    new_inv.pop(catalog_key, None)
                else:
                    new_inv[catalog_key] = cnt - 1
                await conn.execute(
                    f"UPDATE {table} SET {column} = $3::jsonb "
                    f"WHERE guild_id = $1 AND user_id = $2",
                    int(guild_id), int(user_id),
                    json.dumps(new_inv),
                )
                # Mint inside the same atomic block so the inventory
                # decrement is atomic with the token row creation. The
                # mint helper picks up the in-flight transaction via
                # the framework's ContextVar plumbing.
                token = await mint_unit(
                    db,
                    guild_id=int(guild_id),
                    contract_address=addr,
                    owner_user_id=int(user_id),
                    metadata={
                        "catalog_key": catalog_key,
                        "lazy_mint":   True,
                        "lazy_source": f"{table}.{column}",
                    },
                    mint_source="lazy_mint.ah_list",
                    source_table=f"{table}.{column}",
                    source_id=f"{int(user_id)}:{catalog_key}:{int(__import__('time').time())}",
                )
                return token
        except Exception:
            log.exception(
                "lazy_mint_from_jsonb failed kind=%s key=%s table=%s.%s",
                kind, catalog_key, table, column,
            )
            continue
    return None


async def list_owned(
    db: Any,
    *,
    guild_id: int,
    user_id: int,
    contract_address: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Return tokens owned by a user. Filter by contract or kind.

    With neither filter you get every unburned token they own (capped
    at ``limit``). Sorted by contract + unit_index for stable display.
    """
    if contract_address:
        contract = await get_contract(db, address=str(contract_address).lower())
        if not contract:
            return []
        rows = await db.fetch_all(
            """
            SELECT * FROM item_instances
             WHERE guild_id = $1 AND owner_user_id = $2
               AND contract_id = $3 AND burned_at IS NULL
             ORDER BY unit_index ASC
             LIMIT $4
            """,
            int(guild_id), int(user_id),
            int(contract["contract_id"]), int(limit),
        )
    elif kind:
        rows = await db.fetch_all(
            """
            SELECT * FROM item_instances
             WHERE guild_id = $1 AND owner_user_id = $2
               AND kind = $3 AND burned_at IS NULL
             ORDER BY contract_id ASC, unit_index ASC
             LIMIT $4
            """,
            int(guild_id), int(user_id), str(kind).lower(), int(limit),
        )
    else:
        rows = await db.fetch_all(
            """
            SELECT * FROM item_instances
             WHERE guild_id = $1 AND owner_user_id = $2
               AND burned_at IS NULL
             ORDER BY contract_id ASC, unit_index ASC
             LIMIT $3
            """,
            int(guild_id), int(user_id), int(limit),
        )
    return [dict(r) for r in (rows or [])]
