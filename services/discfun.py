"""Disc.Fun service  -  Pump.fun-style proto-token bonding curve.

Pure math + DB layer for the Disc.Fun launchpad. No Discord deps, so the
cog and any future API endpoint can both call into the same primitives.

Architecture:

    DFUN  -- the launchpad's quote currency. A built-in Config.TOKENS
            entry on the Discoin Network with auto-seeded DSC/DFUN,
            DFUN/DSD and DFUN/MOON pools (see Config.TOKENS["DFUN"]).
    Proto -- a virtual-DFUN bonding curve. Constant product on virtual
            reserves; anyone can deploy with a flat DFUN fee.
    Graduation -- once ``real_quote_collected >= graduation_quote``,
            the proto is promoted to a full guild token with TWO real
            pools: SYMBOL/DFUN (deep, native) seeded with the curve's
            collected DFUN + the LP slice of supply, AND a SYMBOL/DSC
            bridge so the token routes against the Discoin Network's
            native coin too. Both pools' LP is uncredited (locked).

Curve math (tokens_in/out denominated in raw 1e18 scale):

    buy:   tokens_out = (V_t * net_in) / (V_q + net_in)
    sell:  quote_out  = (V_q * tokens_in) / (V_t + tokens_in)

    spot price = V_q / V_t (quote per token).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from core.config import Config
from core.framework.scale import SCALE, to_raw

log = logging.getLogger(__name__)


# ── Reserved symbols & validation ───────────────────────────────────────────

# Symbols we never let proto deployers grab. Built-in token symbols are
# additionally blocked at the DB boundary by ``add_guild_token``; this list
# covers everything else (stablecoins, network coins, common reserved words).
_RESERVED_SYMBOLS: frozenset[str] = frozenset({
    "ALL", "USD", "USDC", "DSD", "MOON", "BUD", "FREN", "SEED", "HRV",
    "REEL", "RUNE", "ORE", "EGG", "FISH", "CROP", "GEM", "XP", "DFUN",
    "FUN", "PROTO",
})


def quote_symbol() -> str:
    return str(Config.DISCFUN["quote_symbol"])


def quote_emoji() -> str:
    sym = quote_symbol()
    return str(Config.TOKENS.get(sym, {}).get("emoji", "🎢"))


def validate_symbol(sym: str) -> str | None:
    """Return None if ``sym`` is acceptable, else a player-facing error."""
    if not sym:
        return "Symbol is required."
    if len(sym) > 8:
        return "Symbol must be 8 characters or fewer."
    if not sym.isalnum():
        return "Symbol must be alphanumeric (A-Z, 0-9)."
    if sym.isdigit():
        return "Symbol must contain at least one letter."
    if sym in _RESERVED_SYMBOLS or sym in Config.TOKENS:
        return f"Symbol `{sym}` is reserved or already exists."
    return None


def validate_name(name: str) -> str | None:
    if not name:
        return "Name is required."
    if len(name) > 32:
        return "Name must be 32 characters or fewer."
    return None


_CUSTOM_EMOJI_RE = re.compile(r"\A<a?:[A-Za-z0-9_]{2,32}:\d{15,21}>\Z")


def is_custom_emoji(emoji: str) -> bool:
    """True if ``emoji`` is a Discord custom-emoji mention.

    Format: ``<:name:id>`` (static) or ``<a:name:id>`` (animated).
    Discord renders these inline anywhere the bot has access to the
    emoji (i.e. shares a guild that owns it), which is the case for
    any emoji a player posted from inside a guild Disco is in.
    """
    return bool(_CUSTOM_EMOJI_RE.match(emoji or ""))


def validate_emoji(emoji: str) -> str | None:
    """Accept either a unicode glyph (1-4 chars, no whitespace) or a
    Discord custom-emoji mention (``<:name:id>`` / ``<a:name:id>``).

    Custom emojis are stored verbatim -- discord.py renders them inline
    as long as the bot can see the source emoji, which it can for any
    server the bot shares with the player.
    """
    if not emoji:
        return None  # allowed -- service falls back to default
    if is_custom_emoji(emoji):
        return None
    if len(emoji) > 4 or any(c.isspace() for c in emoji):
        return (
            "Emoji must be a single unicode glyph or a Discord custom "
            "emoji like `<:name:1234567890>` (just paste the emoji from "
            "the picker and Discord will expand it for you)."
        )
    return None


# ── Bonding curve math (pure functions, raw-int domain) ─────────────────────

@dataclass(frozen=True)
class BuyQuote:
    tokens_out_raw: int       # net tokens delivered to buyer
    fee_quote_raw:  int       # fee withheld from gross quote in
    new_virtual_quote: int
    new_virtual_token: int
    spot_price_raw: int       # post-trade quote/token (raw scale)


@dataclass(frozen=True)
class SellQuote:
    quote_out_raw:  int       # net quote delivered to seller (after fee)
    fee_quote_raw:  int
    new_virtual_quote: int
    new_virtual_token: int
    spot_price_raw: int


def _spot_price_raw(v_quote: int, v_token: int) -> int:
    """Return quote-per-token as a raw-scaled int (1.0 == SCALE)."""
    if v_token <= 0:
        return 0
    return (v_quote * SCALE) // v_token


def quote_buy(
    *,
    virtual_quote_raw: int,
    virtual_token_raw: int,
    quote_in_raw: int,
    fee_bps: int,
) -> BuyQuote:
    """Quote a buy of ``quote_in_raw`` quote currency against the curve.

    Fee is taken off the input before it touches the curve, mirroring how
    pump.fun and Uniswap-v2 handle swap fees. ValueError on bad inputs.
    """
    if quote_in_raw <= 0:
        raise ValueError("Buy amount must be positive.")
    if virtual_quote_raw <= 0 or virtual_token_raw <= 0:
        raise ValueError("Curve reserves are not initialised.")
    fee_quote_raw = (quote_in_raw * fee_bps) // 10_000
    net_in = quote_in_raw - fee_quote_raw
    tokens_out = (virtual_token_raw * net_in) // (virtual_quote_raw + net_in)
    if tokens_out <= 0:
        raise ValueError("Buy is too small to receive any tokens.")
    new_v_q = virtual_quote_raw + net_in
    new_v_t = virtual_token_raw - tokens_out
    return BuyQuote(
        tokens_out_raw=tokens_out,
        fee_quote_raw=fee_quote_raw,
        new_virtual_quote=new_v_q,
        new_virtual_token=new_v_t,
        spot_price_raw=_spot_price_raw(new_v_q, new_v_t),
    )


def quote_sell(
    *,
    virtual_quote_raw: int,
    virtual_token_raw: int,
    tokens_in_raw: int,
    fee_bps: int,
) -> SellQuote:
    """Quote a sell of ``tokens_in_raw`` tokens back to the curve.

    Fee is taken off the gross quote owed, so the seller receives the net.
    """
    if tokens_in_raw <= 0:
        raise ValueError("Sell amount must be positive.")
    if virtual_quote_raw <= 0 or virtual_token_raw <= 0:
        raise ValueError("Curve reserves are not initialised.")
    gross_out = (virtual_quote_raw * tokens_in_raw) // (virtual_token_raw + tokens_in_raw)
    if gross_out <= 0:
        raise ValueError("Sell is too small to release any quote.")
    fee_quote_raw = (gross_out * fee_bps) // 10_000
    net_out = gross_out - fee_quote_raw
    new_v_q = virtual_quote_raw - gross_out
    new_v_t = virtual_token_raw + tokens_in_raw
    return SellQuote(
        quote_out_raw=net_out,
        fee_quote_raw=fee_quote_raw,
        new_virtual_quote=new_v_q,
        new_virtual_token=new_v_t,
        spot_price_raw=_spot_price_raw(new_v_q, new_v_t),
    )


# ── DB primitives ───────────────────────────────────────────────────────────

async def get_proto_by_symbol(db, guild_id: int, symbol: str) -> dict | None:
    return await db.fetch_one(
        "SELECT * FROM proto_tokens WHERE guild_id=$1 AND symbol=$2",
        guild_id, symbol.upper(),
    )


async def get_proto_by_id(db, proto_id: int) -> dict | None:
    return await db.fetch_one(
        "SELECT * FROM proto_tokens WHERE proto_id=$1", proto_id,
    )


async def list_active_protos(
    db, guild_id: int, *, limit: int = 25, sort: str = "new",
) -> list[dict]:
    """Active protos. ``sort`` is one of ``new`` / ``hot`` / ``progress`` / ``mcap``."""
    sort = sort.lower()
    if sort == "hot":
        order_by = "volume_quote DESC, created_at DESC"
    elif sort == "progress":
        order_by = "real_quote_collected DESC, created_at DESC"
    elif sort == "mcap":
        # spot-price * total_supply, computed inline so we don't need a
        # generated column. virtual_token > 0 by invariant on a live curve.
        order_by = "(virtual_quote * total_supply / virtual_token) DESC"
    else:  # "new"
        order_by = "created_at DESC"
    return await db.fetch_all(
        f"SELECT * FROM proto_tokens "
        f"WHERE guild_id=$1 AND graduated=FALSE "
        f"ORDER BY {order_by} LIMIT $2",
        guild_id, limit,
    )


async def list_recent_graduates(db, guild_id: int, limit: int = 10) -> list[dict]:
    return await db.fetch_all(
        "SELECT * FROM proto_tokens "
        "WHERE guild_id=$1 AND graduated=TRUE "
        "ORDER BY graduated_at DESC LIMIT $2",
        guild_id, limit,
    )


async def get_user_proto_holding(db, proto_id: int, user_id: int) -> int:
    val = await db.fetch_val(
        "SELECT amount FROM proto_token_holdings WHERE proto_id=$1 AND user_id=$2",
        proto_id, user_id,
    )
    return int(val or 0)


async def list_user_proto_holdings(db, guild_id: int, user_id: int) -> list[dict]:
    return await db.fetch_all(
        "SELECT h.amount, h.cost_basis, p.* FROM proto_token_holdings h "
        "JOIN proto_tokens p ON p.proto_id = h.proto_id "
        "WHERE h.guild_id=$1 AND h.user_id=$2 AND h.amount > 0 "
        "ORDER BY p.created_at DESC",
        guild_id, user_id,
    )


async def user_active_value_quote(db, guild_id: int, user_id: int) -> float:
    """Total quote-currency value of a user's active (un-graduated) protos.

    Sum across all active positions of (held_amount * current_spot). Returns
    a human-scale float so callers can multiply by the DFUN/USD oracle.
    """
    rows = await db.fetch_all(
        "SELECT h.amount, p.virtual_quote, p.virtual_token "
        "FROM proto_token_holdings h "
        "JOIN proto_tokens p ON p.proto_id = h.proto_id "
        "WHERE h.guild_id=$1 AND h.user_id=$2 "
        "  AND h.amount > 0 AND p.graduated = FALSE",
        guild_id, user_id,
    )
    total = 0.0
    for r in rows:
        v_q = int(r["virtual_quote"])
        v_t = int(r["virtual_token"])
        if v_t <= 0:
            continue
        held = int(r["amount"]) / SCALE
        spot = v_q / v_t
        total += held * spot
    return total


async def user_staked_value_dfun(db, guild_id: int, user_id: int) -> tuple[float, float]:
    """User's Disc.Fun staked-position value + pending DFUN yield, both in DFUN.

    Used by ``services/net_worth.compute_net_worth`` so staked Disc.Fun
    positions count toward the player's overall net worth on the same
    footing as their active curve holdings (otherwise locking up a token
    in ``,fun stake`` would visually delete it from the dashboard).
    Returns (staked_value_dfun, pending_dfun) so callers can break out
    "live position" vs "harvestable yield" if they want.
    """
    rows = await db.fetch_all(
        "SELECT symbol, amount, pending_dfun FROM discfun_stakes "
        "WHERE guild_id=$1 AND user_id=$2 AND amount > 0",
        guild_id, user_id,
    )
    staked = 0.0
    pending = 0.0
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        amt_raw = int(r.get("amount") or 0)
        if not sym or amt_raw <= 0:
            continue
        spot = await _amm_spot_dfun(db, guild_id, sym)
        if spot > 0:
            staked += (amt_raw / SCALE) * spot
        pending += int(r.get("pending_dfun") or 0) / SCALE
    return staked, pending


async def list_top_holders(db, proto_id: int, limit: int = 10) -> list[dict]:
    return await db.fetch_all(
        "SELECT user_id, amount, cost_basis FROM proto_token_holdings "
        "WHERE proto_id=$1 AND amount > 0 "
        "ORDER BY amount DESC LIMIT $2",
        proto_id, limit,
    )


async def list_recent_trades(db, proto_id: int, limit: int = 10) -> list[dict]:
    return await db.fetch_all(
        "SELECT * FROM proto_token_trades WHERE proto_id=$1 "
        "ORDER BY created_at DESC LIMIT $2",
        proto_id, limit,
    )


# ── Inactivity sweep ────────────────────────────────────────────────────────

# A proto with no buyer for this long gets erased -- balances zeroed via
# ON DELETE CASCADE on proto_token_holdings, audit trail purged, symbol
# freed up for someone else. Only buys count toward "activity"; a proto
# kept on life support by sells alone is dead by definition.
INACTIVITY_DESTROY_SECS: int = 7 * 24 * 60 * 60   # 7 days
INACTIVITY_SWEEP_INTERVAL_S: int = 60 * 60        # run hourly


async def inactivity_remaining_secs(proto: dict) -> int | None:
    """Return seconds left before this proto is auto-destroyed, or None.

    Returns None for graduated protos (the inactivity rule does not
    apply post-graduation). Returns 0 once the proto is past the
    threshold and is eligible to be swept on the next tick.
    """
    if bool(proto.get("graduated")):
        return None
    last_buy = proto.get("last_buy_at")
    if last_buy is None:
        return INACTIVITY_DESTROY_SECS
    try:
        elapsed = float(last_buy)  # epoch float (post-_coerce)
    except (TypeError, ValueError):
        return INACTIVITY_DESTROY_SECS
    import time as _time
    age = max(0, int(_time.time()) - int(elapsed))
    return max(0, INACTIVITY_DESTROY_SECS - age)


async def sweep_inactive_protos(db) -> list[dict]:
    """Delete every non-graduated proto whose last buy is too old.

    DB-side clock (``EXTRACT(EPOCH FROM (NOW() - last_buy_at))``) so
    the threshold is unaffected by container clock drift. The DELETE
    cascades to ``proto_token_holdings`` (zeroing every holder's
    balance for free, no per-row Python loop) and
    ``proto_token_trades`` via the foreign keys declared on those
    tables. Returns the deleted proto rows so the cog can log /
    surface them. Best-effort: any failure logs and returns ``[]``
    so the sweep loop keeps running.
    """
    try:
        rows = await db.fetch_all(
            "DELETE FROM proto_tokens "
            "WHERE graduated = FALSE "
            "  AND EXTRACT(EPOCH FROM (NOW() - last_buy_at)) >= $1 "
            "RETURNING proto_id, guild_id, creator_id, symbol, name, emoji, "
            "          last_buy_at",
            int(INACTIVITY_DESTROY_SECS),
        )
        return rows or []
    except Exception:
        log.exception("sweep_inactive_protos failed")
        return []


# ── Deploy ──────────────────────────────────────────────────────────────────

DEPLOY_COOLDOWN_SECS: int = 24 * 60 * 60  # 1 deploy per user per guild per 24h


async def deploy_cooldown_remaining(db, guild_id: int, user_id: int) -> int:
    """Return seconds left on the user's per-guild deploy cooldown, or 0.

    DB-side clock (``EXTRACT(EPOCH FROM (NOW() - created_at))``) so the window
    is unaffected by container/DB clock skew. Looks at the user's most recent
    proto deploy in this guild and gates a fresh deploy until 24h after it.
    """
    row = await db.fetch_one(
        "SELECT EXTRACT(EPOCH FROM (NOW() - created_at))::FLOAT AS elapsed "
        "FROM proto_tokens "
        "WHERE guild_id=$1 AND creator_id=$2 "
        "ORDER BY created_at DESC LIMIT 1",
        guild_id, user_id,
    )
    if row is None:
        return 0
    elapsed = float(row.get("elapsed") or 0.0)
    remaining = int(DEPLOY_COOLDOWN_SECS - elapsed)
    return max(0, remaining)


EDIT_FEE_MULTIPLIER: int = 2  # ,fun edit costs 2x deploy_fee in DFUN.


async def edit_proto_token(
    db,
    *,
    guild_id: int,
    user_id: int,
    symbol: str,
    new_name: str | None = None,
    new_emoji: str | None = None,
) -> dict:
    """Update name and/or emoji on a non-graduated proto. Charges 2x deploy fee.

    Refused when:
      * proto doesn't exist for ``(guild_id, symbol)``
      * caller isn't the original creator
      * proto has already graduated (it is now a real token managed by
        the standard ,token surface; rename / re-emoji has to go through
        whatever path that system exposes, not Disc.Fun)
      * neither ``new_name`` nor ``new_emoji`` is provided
      * caller can't afford the edit fee in DFUN on the Discoin Network

    Caller is responsible for input validation (use ``validate_name``
    and ``validate_emoji``). Returns the updated proto_tokens row.
    """
    if new_name is None and new_emoji is None:
        raise ValueError("Pass a new name, a new emoji, or both.")
    sym = symbol.upper()
    cfg = Config.DISCFUN
    qsym = quote_symbol()

    proto = await get_proto_by_symbol(db, guild_id, sym)
    if proto is None:
        raise ValueError(f"No Disc.Fun proto `{sym}` on this server.")
    if int(proto.get("creator_id") or 0) != int(user_id):
        raise ValueError(
            f"Only the original deployer of `{sym}` can edit it."
        )
    if bool(proto.get("graduated")):
        raise ValueError(
            f"`{sym}` has already graduated -- it's a regular token now "
            f"and Disc.Fun no longer manages its metadata."
        )

    # Reject a no-op early so we don't charge for nothing.
    cur_name  = str(proto.get("name") or "")
    cur_emoji = str(proto.get("emoji") or "")
    name_change  = new_name  is not None and new_name  != cur_name
    emoji_change = new_emoji is not None and new_emoji != cur_emoji
    if not (name_change or emoji_change):
        raise ValueError(
            "Nothing to change -- the new values match the current ones."
        )

    fee_raw = to_raw(float(cfg["deploy_fee"]) * EDIT_FEE_MULTIPLIER)
    if fee_raw > 0:
        held = await db.get_wallet_holding(user_id, guild_id, "dsc", qsym)
        bal_raw = int(held["amount"]) if held else 0
        if bal_raw < fee_raw:
            raise ValueError(
                f"Editing a Disc.Fun proto costs "
                f"`{float(cfg['deploy_fee']) * EDIT_FEE_MULTIPLIER:,.0f} {qsym}` "
                f"(2x deploy fee). Your DSC-network {qsym} balance is "
                f"`{bal_raw / SCALE:,.4f}`."
            )
        await db.update_wallet_holding(user_id, guild_id, "dsc", qsym, -fee_raw)

    # Conditional UPDATE: only writes the columns the caller actually wants
    # changed, so a name-only edit doesn't clobber emoji and vice versa.
    next_name  = new_name  if name_change  else cur_name
    next_emoji = new_emoji if emoji_change else cur_emoji
    row = await db.fetch_one(
        """UPDATE proto_tokens
              SET name = $3, emoji = $4
            WHERE guild_id = $1 AND symbol = $2 AND graduated = FALSE
            RETURNING *""",
        guild_id, sym, next_name, next_emoji,
    )
    if row is None:
        # Race: it graduated between our check and the UPDATE. Refund.
        if fee_raw > 0:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id, "dsc", qsym, fee_raw,
                )
            except Exception:
                log.exception(
                    "edit_proto_token refund failed user=%s sym=%s gid=%s",
                    user_id, sym, guild_id,
                )
        raise ValueError(
            f"`{sym}` graduated mid-edit -- the fee was refunded."
        )
    return row


async def deploy_proto_token(
    db,
    *,
    guild_id: int,
    creator_id: int,
    symbol: str,
    name: str,
    emoji: str,
) -> dict:
    """Create a proto token and charge the creator the deploy fee in DFUN.

    Caller is responsible for input validation (use validate_*). Returns the
    inserted proto_tokens row. Raises ValueError on collisions, shortfall, or
    daily-cap (1 deploy per user per guild per 24h).
    """
    sym = symbol.upper()
    cfg = Config.DISCFUN
    qsym = quote_symbol()

    cooldown = await deploy_cooldown_remaining(db, guild_id, creator_id)
    if cooldown > 0:
        hrs, rem = divmod(cooldown, 3600)
        mins = rem // 60
        raise ValueError(
            f"You can only deploy one Disc.Fun token per day. "
            f"Try again in {hrs}h {mins}m."
        )

    if await get_proto_by_symbol(db, guild_id, sym) is not None:
        raise ValueError(f"Proto token `{sym}` already exists.")
    existing = await db.get_all_tokens_for_guild(guild_id)
    if sym in existing:
        raise ValueError(f"Symbol `{sym}` is already in use by a deployed token.")

    deploy_fee_raw = to_raw(cfg["deploy_fee"])
    if deploy_fee_raw > 0:
        await db.update_wallet_holding(creator_id, guild_id, "dsc", qsym, -deploy_fee_raw)

    v_q_raw = to_raw(cfg["initial_virtual_quote"])
    v_tok_raw = to_raw(cfg["initial_virtual_token"])
    total_raw = to_raw(cfg["total_supply"])
    curve_raw = to_raw(cfg["curve_supply"])
    grad_raw = to_raw(cfg["graduation_quote"])

    row = await db.fetch_one(
        """INSERT INTO proto_tokens
            (guild_id, creator_id, symbol, name, emoji, quote_symbol,
             virtual_quote, virtual_token,
             initial_virtual_quote, initial_virtual_token,
             total_supply, curve_supply, graduation_quote,
             trade_fee_bps)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $7, $8, $9, $10, $11, $12)
           RETURNING *""",
        guild_id, creator_id, sym, name, emoji or cfg["default_emoji"], qsym,
        v_q_raw, v_tok_raw,
        total_raw, curve_raw, grad_raw,
        int(cfg["trade_fee_bps"]),
    )
    return row


# ── Buy / sell ──────────────────────────────────────────────────────────────

def _max_quote_in_for_supply_cap(
    *,
    virtual_quote_raw: int,
    virtual_token_raw: int,
    fee_bps: int,
    max_tokens_out: int,
) -> int:
    """Return the largest ``quote_in_raw`` that buys <= max_tokens_out.

    Pure constant-product inversion. Conservatively floor-rounded so the
    subsequent ``quote_buy`` call can never push tokens_out above the
    curve cap due to floor-division skew.
    """
    if max_tokens_out <= 0:
        return 0
    if max_tokens_out >= virtual_token_raw:
        # Curve is mathematically incapable of absorbing this; treat as 0.
        return 0
    new_v_t = virtual_token_raw - max_tokens_out
    # exact_net_in = v_q * max_out / (v_t - max_out)
    net_in_floor = (virtual_quote_raw * max_tokens_out) // new_v_t
    if net_in_floor <= 0:
        return 0
    # gross_in = net_in / (1 - fee_bps/10000)
    denom = 10_000 - int(fee_bps)
    if denom <= 0:
        return 0
    return (net_in_floor * 10_000) // denom


async def buy_proto_token(
    db,
    *,
    guild_id: int,
    user_id: int,
    proto_id: int,
    quote_in_raw: int,
) -> tuple[BuyQuote, dict, bool, int]:
    """Execute a buy. Returns (quote, updated proto row, graduated_now,
    quote_in_used_raw).

    The fourth return value is the amount of quote actually charged to
    the buyer. It equals the requested ``quote_in_raw`` in the common
    case, but is smaller when the curve had less remaining supply than
    the requested buy could absorb -- see "curve-cap clamp" below.

    Curve-cap clamp: if the requested quote_in would push
    ``tokens_in_circulation`` past ``curve_supply``, the buy is reduced
    to whatever quantity exactly tops out the remaining curve supply,
    the unused quote is never charged, and the proto graduates on the
    spot. This prevents the historical foot-gun (most visible after
    migration 0219 lowered ``graduation_quote`` from 50M to 10M without
    retuning virtual reserves) where a curve could fill its 800M-token
    supply cap before reaching the new DFUN target, leaving the proto
    un-buyable AND un-graduated.
    """
    proto = await get_proto_by_id(db, proto_id)
    if proto is None:
        raise ValueError("Proto token not found.")
    if proto["graduated"]:
        raise ValueError(f"`{proto['symbol']}` has already graduated  -  trade it on the AMM.")

    fee_bps = int(proto["trade_fee_bps"])
    qsym = str(proto["quote_symbol"])
    v_q_raw = int(proto["virtual_quote"])
    v_t_raw = int(proto["virtual_token"])
    curve_raw = int(proto["curve_supply"])
    circ_raw = int(proto["tokens_in_circulation"])
    remaining_supply = max(0, curve_raw - circ_raw)
    if remaining_supply <= 0:
        # Should be unreachable -- curve hitting the cap should have
        # already graduated via the clamp below -- but if a stale row
        # somehow has circ == curve and graduated = FALSE, finish the
        # job here instead of erroring forever.
        try:
            await graduate_proto_token(db, proto_id=proto_id)
        except Exception:
            log.exception(
                "graduate_proto_token (post-cap) failed proto_id=%s",
                proto_id,
            )
        raise ValueError(
            f"`{proto['symbol']}` curve is sold out and is graduating. "
            f"Retry the buy on the AMM in a moment."
        )

    max_safe_in = _max_quote_in_for_supply_cap(
        virtual_quote_raw=v_q_raw,
        virtual_token_raw=v_t_raw,
        fee_bps=fee_bps,
        max_tokens_out=remaining_supply,
    )
    if quote_in_raw > max_safe_in > 0:
        quote_in_raw = max_safe_in
    elif max_safe_in <= 0:
        # No room left to fit even a one-unit buy; force graduation.
        try:
            await graduate_proto_token(db, proto_id=proto_id)
        except Exception:
            log.exception(
                "graduate_proto_token (no-room) failed proto_id=%s",
                proto_id,
            )
        raise ValueError(
            f"`{proto['symbol']}` curve has no room left and is "
            f"graduating now. Trade it on the AMM in a moment."
        )

    quote = quote_buy(
        virtual_quote_raw=v_q_raw,
        virtual_token_raw=v_t_raw,
        quote_in_raw=quote_in_raw,
        fee_bps=fee_bps,
    )

    new_circ = circ_raw + quote.tokens_out_raw
    if new_circ > curve_raw:
        # Defensive: floor-division should have prevented this, but if
        # it ever happens we'd rather fail loud than mis-mint tokens.
        raise ValueError(
            "Buy exceeds remaining curve supply. Try a smaller size or "
            "wait for graduation."
        )

    # Charge quote up front. Raises ValueError on insufficient balance.
    await db.update_wallet_holding(user_id, guild_id, "dsc", qsym, -quote_in_raw)

    fee_post = quote.fee_quote_raw
    net_in_post = quote_in_raw - fee_post
    new_real = int(proto["real_quote_collected"]) + net_in_post

    # Track if this user is a brand-new holder so we increment holder_count.
    pre_holding = await get_user_proto_holding(db, proto_id, user_id)
    holder_delta = 1 if pre_holding == 0 else 0

    # last_buy_at refresh keeps the "use it or lose it" inactivity sweep
    # off the proto's back -- only buys count, sells don't (the rule is
    # spelled out on migration 0221). Stamping NOW() inside the same
    # UPDATE keeps the write atomic with the curve-state update.
    updated = await db.fetch_one(
        """UPDATE proto_tokens
              SET virtual_quote          = $2,
                  virtual_token          = $3,
                  real_quote_collected   = $4,
                  tokens_in_circulation  = $5,
                  volume_quote           = volume_quote + $6,
                  trade_count            = trade_count + 1,
                  holder_count           = holder_count + $7,
                  last_buy_at            = NOW()
            WHERE proto_id = $1 AND graduated = FALSE
            RETURNING *""",
        proto_id,
        quote.new_virtual_quote,
        quote.new_virtual_token,
        new_real,
        new_circ,
        quote_in_raw,
        holder_delta,
    )
    if updated is None:
        await db.update_wallet_holding(user_id, guild_id, "dsc", qsym, quote_in_raw)
        raise ValueError("Trade collided with graduation. Try again.")

    await db.execute(
        """INSERT INTO proto_token_holdings
            (proto_id, guild_id, user_id, amount, cost_basis)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (proto_id, user_id)
           DO UPDATE SET
               amount     = proto_token_holdings.amount     + EXCLUDED.amount,
               cost_basis = proto_token_holdings.cost_basis + EXCLUDED.cost_basis""",
        proto_id, guild_id, user_id, quote.tokens_out_raw, net_in_post,
    )
    await db.execute(
        """INSERT INTO proto_token_trades
            (proto_id, guild_id, user_id, side, quote_amount, token_amount,
             fee_quote, price_after)
           VALUES ($1, $2, $3, 'buy', $4, $5, $6, $7)""",
        proto_id, guild_id, user_id, quote_in_raw, quote.tokens_out_raw,
        quote.fee_quote_raw, quote.spot_price_raw,
    )

    graduated_now = False
    cap_filled = (new_circ >= curve_raw)
    quote_target_hit = (new_real >= int(proto["graduation_quote"]))
    if cap_filled or quote_target_hit:
        try:
            await graduate_proto_token(db, proto_id=proto_id)
            graduated_now = True
        except Exception:
            log.exception(
                "graduate_proto_token failed for proto_id=%s -- curve still tradeable",
                proto_id,
            )

    return quote, updated, graduated_now, quote_in_raw


async def sell_proto_token(
    db,
    *,
    guild_id: int,
    user_id: int,
    proto_id: int,
    tokens_in_raw: int,
) -> tuple[SellQuote, dict]:
    """Execute a sell. Returns (quote, updated proto row).

    Cannot trigger graduation (only buys add real quote).
    """
    proto = await get_proto_by_id(db, proto_id)
    if proto is None:
        raise ValueError("Proto token not found.")
    if proto["graduated"]:
        raise ValueError(f"`{proto['symbol']}` has already graduated  -  trade it on the AMM.")

    held = await get_user_proto_holding(db, proto_id, user_id)
    if held < tokens_in_raw:
        raise ValueError("You don't own that many tokens.")

    fee_bps = int(proto["trade_fee_bps"])
    qsym = str(proto["quote_symbol"])
    quote = quote_sell(
        virtual_quote_raw=int(proto["virtual_quote"]),
        virtual_token_raw=int(proto["virtual_token"]),
        tokens_in_raw=tokens_in_raw,
        fee_bps=fee_bps,
    )

    gross_out = quote.quote_out_raw + quote.fee_quote_raw
    real_held = int(proto["real_quote_collected"])
    if gross_out > real_held:
        raise ValueError(
            "Curve has insufficient real quote to fill this sell. Try smaller."
        )

    new_circ = max(0, int(proto["tokens_in_circulation"]) - tokens_in_raw)
    new_real = real_held - gross_out

    burned = await db.fetch_one(
        """UPDATE proto_token_holdings
              SET amount = amount - $3
            WHERE proto_id=$1 AND user_id=$2 AND amount >= $3
            RETURNING amount""",
        proto_id, user_id, tokens_in_raw,
    )
    if burned is None:
        raise ValueError("Holding changed mid-sell. Try again.")
    user_now_zero = int(burned["amount"]) == 0
    holder_delta = -1 if user_now_zero else 0

    updated = await db.fetch_one(
        """UPDATE proto_tokens
              SET virtual_quote          = $2,
                  virtual_token          = $3,
                  real_quote_collected   = $4,
                  tokens_in_circulation  = $5,
                  volume_quote           = volume_quote + $6,
                  trade_count            = trade_count + 1,
                  holder_count           = GREATEST(0, holder_count + $7)
            WHERE proto_id = $1 AND graduated = FALSE
            RETURNING *""",
        proto_id,
        quote.new_virtual_quote,
        quote.new_virtual_token,
        new_real,
        new_circ,
        gross_out,
        holder_delta,
    )
    if updated is None:
        await db.execute(
            """UPDATE proto_token_holdings SET amount = amount + $3
               WHERE proto_id=$1 AND user_id=$2""",
            proto_id, user_id, tokens_in_raw,
        )
        raise ValueError("Trade collided with graduation. Try again.")

    # Pay the user (net of fee) and reduce their cost basis proportionally.
    await db.update_wallet_holding(user_id, guild_id, "dsc", qsym, quote.quote_out_raw)
    await db.execute(
        """UPDATE proto_token_holdings
              SET cost_basis = GREATEST(0, cost_basis -
                                            ((cost_basis * $3) / GREATEST(amount + $3, 1)))
            WHERE proto_id=$1 AND user_id=$2""",
        proto_id, user_id, tokens_in_raw,
    )

    await db.execute(
        """INSERT INTO proto_token_trades
            (proto_id, guild_id, user_id, side, quote_amount, token_amount,
             fee_quote, price_after)
           VALUES ($1, $2, $3, 'sell', $4, $5, $6, $7)""",
        proto_id, guild_id, user_id, quote.quote_out_raw, tokens_in_raw,
        quote.fee_quote_raw, quote.spot_price_raw,
    )

    return quote, updated


# ── Graduation ──────────────────────────────────────────────────────────────

async def graduate_proto_token(db, *, proto_id: int) -> dict:
    """Promote a proto token into a full guild token + DFUN/DSC pools.

    Idempotent: re-calling on a graduated proto is a no-op. The remaining
    curve supply (total_supply - tokens_in_circulation) is split between
    the SYMBOL/DFUN pool (deep, native -- gets the bulk of the LP slice
    plus all collected DFUN) and a SYMBOL/DSC bridge pool (smaller, lets
    the new token route against Discoin's network coin too). Existing
    proto holders are credited real wallet_holdings on the dsc network in
    exchange for their proto balances.
    """
    proto = await get_proto_by_id(db, proto_id)
    if proto is None:
        raise ValueError("Proto token not found.")
    if proto["graduated"]:
        return proto

    cfg = Config.DISCFUN
    sym = proto["symbol"]
    gid = int(proto["guild_id"])
    name = proto["name"]
    emoji = proto["emoji"]
    qsym = str(proto["quote_symbol"])

    real_quote_raw = int(proto["real_quote_collected"])
    circ_raw = int(proto["tokens_in_circulation"])
    total_raw = int(proto["total_supply"])
    lp_token_raw = max(0, total_raw - circ_raw)

    # Atomically flip graduated. Bail if we lost the race.
    locked = await db.fetch_one(
        """UPDATE proto_tokens
              SET graduated = TRUE, graduated_at = NOW()
            WHERE proto_id = $1 AND graduated = FALSE
            RETURNING *""",
        proto_id,
    )
    if locked is None:
        return await get_proto_by_id(db, proto_id) or proto

    # Final spot price as the genesis oracle price.
    final_v_q = int(proto["virtual_quote"])
    final_v_t = int(proto["virtual_token"])
    quote_token = Config.TOKENS.get(qsym, {})
    quote_usd = float(quote_token.get("start_price", 1.0))
    start_price_quote = (final_v_q / final_v_t) if final_v_t > 0 else (
        cfg["initial_virtual_quote"] / cfg["initial_virtual_token"]
    )
    # Convert spot from quote-per-token to USD-per-token using the quote's
    # genesis oracle price. Cheap approximation; the per-guild oracle drifts
    # from there as soon as `crypto_prices` updates.
    start_price_usd = start_price_quote * quote_usd

    try:
        # total_raw is the bonding-curve cap in raw units; pass it straight
        # through so the structured guild_tokens.max_supply column is
        # populated from minute zero (the JSON contract params blob below
        # carries the same value for backwards-compat).
        await db.add_guild_token(
            gid, sym, name, emoji, "PoS", "Discoin Network",
            start_price_usd, float(cfg["graduation_daily_vol"]),
            max_supply=int(total_raw),
        )
    except ValueError:
        await db.execute(
            "UPDATE proto_tokens SET graduated=FALSE, graduated_at=NULL "
            "WHERE proto_id=$1", proto_id,
        )
        raise
    await db.execute(
        "UPDATE guild_tokens SET token_type=$1, circulating_supply=$2 "
        "WHERE guild_id=$3 AND symbol=$4",
        "discfun", circ_raw, gid, sym,
    )
    await db.execute(
        "INSERT INTO crypto_prices (symbol, guild_id, price, open_price, day_high, day_low) "
        "VALUES ($1,$2,$3,$3,$3,$3) ON CONFLICT DO NOTHING",
        sym, gid, start_price_usd,
    )
    # The token's primary home is the Discoin Network (the curve quote
    # is DFUN which lives on dsc; holders' graduated balance gets
    # credited on the dsc wallet below). It's ALSO accepted on the
    # Moon Network so the SYMBOL/MOON bridge pool seeded just below
    # routes through Moon-Network wallets, not Discoin-Network ones --
    # otherwise the pool reads as a Discoin-Network pair when one side
    # of it (MOON) is fundamentally a Moon Network coin. Idempotent
    # via ON CONFLICT inside add_token_to_network_wallet.
    await db.add_token_to_network_wallet(gid, "Discoin Network", sym)
    await db.add_token_to_network_wallet(gid, "Moon Network", sym)

    await db.set_token_contract(gid, sym, {
        "type":          "ERC-20",
        "network":       "DSC",
        "deployer":      int(proto["creator_id"]),
        "origin":        "discfun",
        "moon_swappable": True,
        "max_supply":    total_raw // SCALE,
        "burn_rate":     float(cfg["graduation_burn_rate"]),
        "transfer_fee":  float(cfg["graduation_transfer_fee"]),
    })

    # ── Pool seeding ──────────────────────────────────────────────────────
    # 90 / 10 split of the LP slice: the headline SYMBOL/DFUN pool gets the
    # bulk of liquidity (matches the curve's quote currency), and a smaller
    # SYMBOL/DSC bridge pool seeds with 10% so the token routes against the
    # Discoin Network's stake coin from minute zero. LP shares aren't
    # credited to anyone -- the pool row's total_lp is set by create_pool
    # but no LP positions are minted, so the liquidity is permanently
    # locked (same effect as pump.fun "burning" the raydium LP).
    if lp_token_raw > 0 and real_quote_raw > 0:
        # SYMBOL/DFUN -- deep pool, all collected quote + 90% of LP supply.
        primary_token_raw = (lp_token_raw * 9) // 10
        primary_quote_raw = real_quote_raw
        bridge_token_raw = lp_token_raw - primary_token_raw

        pool_id, ca, cb = db.make_pool_id(sym, qsym)
        if not await db.get_pool(pool_id, gid):
            tok_h = primary_token_raw / SCALE
            quo_h = primary_quote_raw / SCALE
            ra = tok_h if ca == sym else quo_h
            rb = quo_h if ca == sym else tok_h
            await db.create_pool(pool_id, gid, ca, cb, ra, rb)

        # SYMBOL/DSC bridge -- 10% of the LP slice paired with DSC valued
        # at the curve's final spot price (in DFUN) translated through the
        # configured DFUN/DSC ratio. This keeps the bridge price coherent
        # with the primary pool at the moment of graduation.
        if bridge_token_raw > 0:
            dsc_token = Config.TOKENS.get("DSC", {})
            dsc_usd = float(dsc_token.get("start_price", 0.05)) or 0.05
            # USD-per-token for the new SYMBOL = start_price_usd.
            # DSC reserve to balance bridge_token_raw at that price:
            bridge_token_h = bridge_token_raw / SCALE
            bridge_dsc_h = (bridge_token_h * start_price_usd) / dsc_usd
            if bridge_dsc_h > 0:
                pool_id_dsc, ca2, cb2 = db.make_pool_id(sym, "DSC")
                if not await db.get_pool(pool_id_dsc, gid):
                    ra = bridge_token_h if ca2 == sym else bridge_dsc_h
                    rb = bridge_dsc_h if ca2 == sym else bridge_token_h
                    await db.create_pool(pool_id_dsc, gid, ca2, cb2, ra, rb)

    # MOON bridge so the new token routes through the Moon Network economy.
    try:
        await db.seed_moon_swap_pool(gid, sym)
    except Exception:
        log.warning(
            "graduate: MOON pair seed failed for sym=%s gid=%s -- next boot retries",
            sym, gid,
        )

    # Credit each holder's proto balance into real wallet_holdings, then
    # zero the proto holdings so we can't double-pay.
    holders = await db.fetch_all(
        "SELECT user_id, amount FROM proto_token_holdings "
        "WHERE proto_id=$1 AND amount > 0",
        proto_id,
    )
    for h in holders:
        try:
            await db.update_wallet_holding(
                int(h["user_id"]), gid, "dsc", sym, int(h["amount"]),
            )
        except Exception:
            log.exception(
                "graduate: failed crediting holder uid=%s proto=%s -- skipping",
                h.get("user_id"), proto_id,
            )
    await db.execute(
        "UPDATE proto_token_holdings SET amount = 0 WHERE proto_id=$1",
        proto_id,
    )

    log.info(
        "discfun: graduated proto_id=%s sym=%s gid=%s holders=%s real_quote=%.2f",
        proto_id, sym, gid, len(holders), real_quote_raw / SCALE,
    )
    return locked


# ── Stats helpers ───────────────────────────────────────────────────────────

def progress_pct(real_quote_raw: int, graduation_quote_raw: int) -> float:
    """Return graduation progress as 0.0-1.0."""
    if graduation_quote_raw <= 0:
        return 0.0
    return min(1.0, real_quote_raw / graduation_quote_raw)


def market_cap_raw(virtual_quote_raw: int, virtual_token_raw: int, total_supply_raw: int) -> int:
    """Market cap in raw quote units (price * total_supply)."""
    if virtual_token_raw <= 0:
        return 0
    return (virtual_quote_raw * total_supply_raw) // virtual_token_raw


# ── Candles (built from proto_token_trades, not the global candle table) ────

CHART_TIMEFRAMES: dict[str, int] = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "1h":  3600,
    "4h":  14400,
    "1d":  86400,
}


async def fetch_proto_trades_for_candles(
    db, proto_id: int, since_ts: int, limit: int = 5000,
) -> list[dict]:
    """Trades since ``since_ts`` for a proto, oldest-first (candle order)."""
    return await db.fetch_all(
        "SELECT created_at, price_after, quote_amount "
        "FROM proto_token_trades "
        "WHERE proto_id=$1 AND created_at >= to_timestamp($2) "
        "ORDER BY created_at ASC LIMIT $3",
        proto_id, since_ts, limit,
    )


def build_proto_candles(trades: list[dict], tf_seconds: int) -> list[dict]:
    """Aggregate trades into OHLCV candles bucketed by ``tf_seconds``.

    Each trade row carries:
      * ``created_at``  -- epoch float (post-_coerce)
      * ``price_after`` -- DFUN per token *after* the trade, persisted at
        the project-wide raw 1e18 scale (the column was declared
        NUMERIC(36, 18) but ``buy_proto_token`` / ``sell_proto_token``
        write ``BuyQuote.spot_price_raw`` directly, which is
        ``(virtual_quote * SCALE) // virtual_token``). We divide by SCALE
        on read so candle prices come out in human DFUN-per-token units
        and the chart Y-axis isn't a 14-digit nightmare.
      * ``quote_amount`` -- raw DFUN volume
    """
    if not trades:
        return []
    buckets: dict[int, dict] = {}
    for t in trades:
        ts = t.get("created_at")
        if ts is None:
            continue
        ts_int = int(float(ts))
        bucket = (ts_int // tf_seconds) * tf_seconds
        price = float(t.get("price_after") or 0.0) / SCALE
        if price <= 0:
            continue
        vol = float(int(t.get("quote_amount") or 0)) / SCALE
        b = buckets.get(bucket)
        if b is None:
            buckets[bucket] = {
                "ts": bucket, "open": price, "high": price,
                "low": price, "close": price, "volume": vol,
            }
        else:
            b["high"]   = max(b["high"], price)
            b["low"]    = min(b["low"], price)
            b["close"]  = price
            b["volume"] = b["volume"] + vol
    return sorted(buckets.values(), key=lambda x: x["ts"])


def candles_to_lwc(candles: list[dict]) -> list[dict]:
    """Lightweight-charts payload format (drops volume, keeps ``time`` key)."""
    return [
        {"time": c["ts"], "open": c["open"], "high": c["high"],
         "low": c["low"], "close": c["close"]}
        for c in candles
    ]


def synthetic_origin_candle(proto: dict) -> dict | None:
    """A single OPEN-tick candle at the curve's genesis price.

    Used as a left-pad anchor when a proto has zero or one trade so the
    chart still renders something instead of a "not enough history" wall
    on a brand-new launch.
    """
    iv_q = int(proto.get("initial_virtual_quote") or 0)
    iv_t = int(proto.get("initial_virtual_token") or 0)
    if iv_q <= 0 or iv_t <= 0:
        return None
    spot = (iv_q / iv_t) if iv_t else 0.0
    if spot <= 0:
        return None
    created = proto.get("created_at")
    if created is None:
        return None
    ts = int(float(created))
    return {"ts": ts, "open": spot, "high": spot, "low": spot, "close": spot, "volume": 0.0}


def current_spot_candle(proto: dict, ts: int) -> dict | None:
    """Trailing candle at ``ts`` showing the live spot price.

    Lets the chart visually keep up with the curve between trades -- without
    this, an idle proto's chart freezes at the last trade's price even though
    nothing has actually moved.
    """
    v_q = int(proto.get("virtual_quote") or 0)
    v_t = int(proto.get("virtual_token") or 0)
    if v_q <= 0 or v_t <= 0:
        return None
    spot = v_q / v_t
    if spot <= 0:
        return None
    return {"ts": ts, "open": spot, "high": spot, "low": spot, "close": spot, "volume": 0.0}


def graduation_price_dfun(proto: dict) -> float:
    """The DFUN/token price the curve will be at the moment graduation triggers.

    By constant-product invariant V_q' * V_t' = V_q * V_t and at graduation
    real_quote_collected = graduation_quote, so spot at graduation is
    (V_q + (graduation_quote - real_quote_collected) * (1 - fee))
       / (V_t - tokens_remaining_to_sell). Returns 0.0 if it can't compute.
    """
    try:
        v_q = int(proto["virtual_quote"])
        v_t = int(proto["virtual_token"])
        grad = int(proto["graduation_quote"])
        real = int(proto["real_quote_collected"])
        fee_bps = int(proto.get("trade_fee_bps") or 0)
    except (KeyError, TypeError, ValueError):
        return 0.0
    if v_t <= 0 or grad <= 0:
        return 0.0
    remaining_quote = max(0, grad - real)
    if remaining_quote <= 0:
        return v_q / v_t
    # account for trade fee taken out of input
    net_quote_needed = remaining_quote * (10_000 - fee_bps) / 10_000
    new_v_q = v_q + net_quote_needed
    # tokens_out from the remaining quote: V_t * net_in / (V_q + net_in)
    tokens_out = (v_t * net_quote_needed) / (v_q + net_quote_needed)
    new_v_t = v_t - tokens_out
    if new_v_t <= 0:
        return 0.0
    return new_v_q / new_v_t


# ── Staking (graduated proto tokens -> DFUN yield) ──────────────────────────

async def total_staked_dfun_value(db, guild_id: int) -> float:
    """Sum the live DFUN spot value of every active stake in the guild.

    Used as the denominator for the variable-APY emission curve. We
    join discfun_stakes against the SYMBOL/DFUN AMM pool to pick up
    each symbol's live reserves and value the stake at the current
    bonding-pool spot. Symbols with no live pool (shouldn't happen
    post-graduation but defensive) contribute zero.
    """
    rows = await db.fetch_all(
        "SELECT symbol, amount FROM discfun_stakes "
        "WHERE guild_id=$1 AND amount > 0",
        guild_id,
    )
    total = 0.0
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        amt_raw = int(r.get("amount") or 0)
        if not sym or amt_raw <= 0:
            continue
        spot = await _amm_spot_dfun(db, guild_id, sym)
        if spot <= 0:
            continue
        total += (amt_raw / SCALE) * spot
    return total


def _staking_emission_bounds() -> tuple[float, float, float, float]:
    """Return (emission_dfun_per_day, max_daily_rate, min_daily_rate, fallback_apy).

    All five Disc.Fun staking knobs come from ``Config.DISCFUN``. The
    fallback_apy is the legacy fixed APY, used when the variable path
    can't compute (e.g. no live stake to value against).
    """
    cfg = Config.DISCFUN
    emission   = float(cfg.get("staking_emission_dfun_per_day", 0.0) or 0.0)
    max_apy    = float(cfg.get("staking_max_apy_pct", 10_000.0) or 0.0) / 100.0
    min_apy    = float(cfg.get("staking_min_apy_pct", 0.0) or 0.0)        / 100.0
    fallback   = float(cfg.get("staking_apy", 0.50) or 0.0)
    max_daily  = max_apy / 365.0
    min_daily  = min_apy / 365.0
    return emission, max_daily, min_daily, fallback


async def current_staking_daily_rate(
    db, guild_id: int,
    *,
    total_dfun_override: float | None = None,
) -> float:
    """Effective daily yield rate for Disc.Fun stakes in this guild.

    Mirrors ``services.safety_module.sm_current_daily_rate``:

      daily_rate = emission_dfun_per_day / total_staked_dfun_value
                   clamped to [min_apy / 365, max_apy / 365]

    When TVL is zero the max cap kicks in; as TVL grows the rate
    compresses but never drops below the floor. Pass
    ``total_dfun_override`` to skip the (per-stake) AMM probe when the
    caller already has the number (hot-path, e.g. ``_accrue_stake``
    accruing many positions in sequence -- though the GIL-friendly
    happy path is one accrual per call so the override mostly exists
    for tests / panel rendering).
    """
    emission, max_daily, min_daily, fallback = _staking_emission_bounds()
    if emission <= 0:
        # Emission disabled -> drop back to the legacy fixed APY so
        # existing positions keep earning at the old rate.
        return fallback / 365.0
    if total_dfun_override is not None:
        tvl = float(total_dfun_override)
    else:
        tvl = await total_staked_dfun_value(db, guild_id)
    if tvl <= 0:
        return max_daily
    return max(min(emission / tvl, max_daily), min_daily)


async def current_staking_apy_pct(
    db, guild_id: int,
    *,
    total_dfun_override: float | None = None,
) -> float:
    """Live Disc.Fun staking APY as a percentage (e.g. 137.5)."""
    daily = await current_staking_daily_rate(
        db, guild_id, total_dfun_override=total_dfun_override,
    )
    return daily * 365.0 * 100.0


async def _amm_spot_dfun(db, guild_id: int, symbol: str) -> float:
    """SYMBOL price quoted in DFUN via the SYMBOL/DFUN AMM pool.

    Returns 0.0 when the pool doesn't exist or has empty reserves. Used as
    the basis for staking yield so it tracks live market value rather than
    a stale oracle.
    """
    pool_id, ca, _cb = db.make_pool_id(symbol, "DFUN")
    pool = await db.get_pool(pool_id, guild_id)
    if not pool:
        return 0.0
    ra = float(pool.get("reserve_a") or 0.0)
    rb = float(pool.get("reserve_b") or 0.0)
    if ra <= 0 or rb <= 0:
        return 0.0
    if ca == symbol.upper():
        return rb / ra  # DFUN per SYMBOL
    return ra / rb


async def _is_graduated_discfun_token(db, guild_id: int, symbol: str) -> bool:
    """True iff symbol is a Disc.Fun-origin token that has graduated.

    Lets us reject `,fun stake DSC` etc cleanly even though DSC has its own
    pool: only protos that bonded all the way through the curve qualify.
    """
    row = await db.fetch_one(
        "SELECT graduated FROM proto_tokens WHERE guild_id=$1 AND symbol=$2",
        guild_id, symbol.upper(),
    )
    return bool(row and row.get("graduated"))


def _accrue_yield_raw(
    *, amount_raw: int, spot_dfun: float, apy: float, elapsed_secs: float,
) -> int:
    """Compute DFUN yield (raw scale) accrued by a position.

    yield_dfun = amount * spot * apy * (elapsed / SECONDS_PER_YEAR)
    """
    if amount_raw <= 0 or spot_dfun <= 0 or apy <= 0 or elapsed_secs <= 0:
        return 0
    seconds_per_year = 365.0 * 86400.0
    amount_h = amount_raw / SCALE
    yield_human = amount_h * spot_dfun * apy * (elapsed_secs / seconds_per_year)
    return to_raw(yield_human)


async def _accrue_stake(db, *, guild_id: int, user_id: int, symbol: str) -> dict | None:
    """Bring a stake row up to NOW(). Returns the row.

    Two modes, selected by the row's ``auto_compound`` flag:

    * **OFF (default)** -- yield accumulates as DFUN in ``pending_dfun``,
      claimable separately via ``,fun claim``.
    * **ON** -- the DFUN yield is virtually swapped 1:1 at the live
      ``SYMBOL/DFUN`` spot price into more of the staked token and added
      back to ``amount``. No AMM round-trip (so the pool isn't churned by
      micro-compounds), and ``amount`` grows continuously which means
      next-tick yield is computed off the bigger position -- true
      compounding.

    Idempotent. DB-side ``EXTRACT(EPOCH FROM (NOW() - last_accrue))`` so
    the elapsed window is unaffected by container/DB clock skew.
    """
    sym = symbol.upper()
    row = await db.fetch_one(
        "SELECT *, EXTRACT(EPOCH FROM (NOW() - last_accrue))::FLOAT AS _elapsed "
        "FROM discfun_stakes WHERE guild_id=$1 AND user_id=$2 AND symbol=$3",
        guild_id, user_id, sym,
    )
    if row is None:
        return None
    amount_raw = int(row.get("amount") or 0)
    elapsed = float(row.get("_elapsed") or 0.0)
    if amount_raw <= 0 or elapsed <= 0:
        return row
    spot = await _amm_spot_dfun(db, guild_id, sym)
    # Variable APY: emission-based, like Safety Module / Cetus. Yields
    # compress as guild-wide TVL grows but never drop below the floor.
    daily_rate = await current_staking_daily_rate(db, guild_id)
    apy = daily_rate * 365.0
    accrued_dfun = _accrue_yield_raw(
        amount_raw=amount_raw, spot_dfun=spot, apy=apy, elapsed_secs=elapsed,
    )
    if accrued_dfun <= 0:
        return row

    if bool(row.get("auto_compound")):
        # Virtual compound: convert DFUN -> SYM at spot, add to position.
        if spot <= 0:
            # No live pool -> nothing we can compound at; defer accrual to
            # a future tick. Don't lose the elapsed window: leave the row
            # alone so next call retries with a fresh price.
            return row
        compounded_sym_raw = to_raw((accrued_dfun / SCALE) / spot)
        if compounded_sym_raw <= 0:
            return row
        updated = await db.fetch_one(
            "UPDATE discfun_stakes "
            "   SET amount           = amount + $4, "
            "       total_compounded = total_compounded + $4, "
            "       last_accrue      = NOW() "
            " WHERE guild_id=$1 AND user_id=$2 AND symbol=$3 "
            "RETURNING *",
            guild_id, user_id, sym, compounded_sym_raw,
        )
        return updated or row

    updated = await db.fetch_one(
        "UPDATE discfun_stakes "
        "   SET pending_dfun = pending_dfun + $4, "
        "       last_accrue  = NOW() "
        " WHERE guild_id=$1 AND user_id=$2 AND symbol=$3 "
        "RETURNING *",
        guild_id, user_id, sym, accrued_dfun,
    )
    return updated or row


async def set_autocompound(
    db, *, guild_id: int, user_id: int, symbol: str, enabled: bool,
) -> dict | None:
    """Toggle autocompound on a stake row, accruing first under the OLD mode.

    Accruing first means any yield earned before the toggle is preserved
    in whatever form the user had set: DFUN if it was OFF (added to
    ``pending_dfun``, sweep-able with ``,fun claim``), or SYM if it was ON
    (already auto-restaked into ``amount``). Then we flip the flag so
    subsequent accruals follow the new mode.
    """
    sym = symbol.upper()
    row = await db.fetch_one(
        "SELECT 1 FROM discfun_stakes WHERE guild_id=$1 AND user_id=$2 AND symbol=$3",
        guild_id, user_id, sym,
    )
    if row is None:
        return None
    await _accrue_stake(db, guild_id=guild_id, user_id=user_id, symbol=sym)
    return await db.fetch_one(
        "UPDATE discfun_stakes SET auto_compound=$4 "
        "WHERE guild_id=$1 AND user_id=$2 AND symbol=$3 "
        "RETURNING *",
        guild_id, user_id, sym, bool(enabled),
    )


async def stake_token(
    db, *, guild_id: int, user_id: int, symbol: str, amount_raw: int,
) -> tuple[int, dict]:
    """Stake ``amount_raw`` of ``symbol`` (graduated Disc.Fun token).

    Returns (newly_staked_raw, updated row). Raises ValueError on invalid
    input or insufficient wallet balance. Auto-accrues any pending yield
    on the existing stake before adding to it, so the average price for
    yield accrual stays honest.
    """
    sym = symbol.upper()
    if amount_raw <= 0:
        raise ValueError("Stake amount must be positive.")
    if not await _is_graduated_discfun_token(db, guild_id, sym):
        raise ValueError(
            f"`{sym}` is not a graduated Disc.Fun token. Only protos that "
            f"completed their bonding curve can be staked here."
        )
    # Accrue first so the existing position settles at its old basis.
    await _accrue_stake(db, guild_id=guild_id, user_id=user_id, symbol=sym)
    # Charge the wallet (raises ValueError on shortfall).
    await db.update_wallet_holding(user_id, guild_id, "dsc", sym, -amount_raw)
    row = await db.fetch_one(
        """INSERT INTO discfun_stakes
            (user_id, guild_id, symbol, amount, last_accrue, staked_at)
           VALUES ($1, $2, $3, $4, NOW(), NOW())
           ON CONFLICT (user_id, guild_id, symbol)
           DO UPDATE SET
               amount      = discfun_stakes.amount + EXCLUDED.amount,
               last_accrue = NOW()
           RETURNING *""",
        user_id, guild_id, sym, amount_raw,
    )
    return amount_raw, row


async def unstake_token(
    db, *, guild_id: int, user_id: int, symbol: str, amount_raw: int,
) -> tuple[int, int, dict]:
    """Unstake ``amount_raw`` and auto-claim any pending yield.

    Returns (unstaked_raw, claimed_dfun_raw, updated row).
    """
    sym = symbol.upper()
    if amount_raw <= 0:
        raise ValueError("Unstake amount must be positive.")
    await _accrue_stake(db, guild_id=guild_id, user_id=user_id, symbol=sym)
    # Atomic remove. Bail if not enough staked.
    burned = await db.fetch_one(
        """UPDATE discfun_stakes
              SET amount      = amount - $4,
                  last_accrue = NOW()
            WHERE guild_id=$1 AND user_id=$2 AND symbol=$3 AND amount >= $4
            RETURNING *""",
        guild_id, user_id, sym, amount_raw,
    )
    if burned is None:
        raise ValueError("You don't have that much staked.")
    # Return the tokens.
    await db.update_wallet_holding(user_id, guild_id, "dsc", sym, amount_raw)
    # Claim yield as part of the unstake.
    claimed = await _claim_pending(db, guild_id=guild_id, user_id=user_id, symbol=sym)
    final = await db.fetch_one(
        "SELECT * FROM discfun_stakes WHERE guild_id=$1 AND user_id=$2 AND symbol=$3",
        guild_id, user_id, sym,
    )
    return amount_raw, claimed, final or burned


async def _claim_pending(
    db, *, guild_id: int, user_id: int, symbol: str,
) -> int:
    """Internal: zero pending_dfun on a row and credit the user's DFUN wallet."""
    sym = symbol.upper()
    row = await db.fetch_one(
        """UPDATE discfun_stakes
              SET pending_dfun  = 0,
                  total_claimed = total_claimed + pending_dfun
            WHERE guild_id=$1 AND user_id=$2 AND symbol=$3
            RETURNING (
                SELECT pending_dfun FROM discfun_stakes
                WHERE guild_id=$1 AND user_id=$2 AND symbol=$3
            ) AS prev""",
        guild_id, user_id, sym,
    )
    # The RETURNING subselect above runs after the UPDATE so it always sees
    # 0. Use a cleaner two-step instead.
    pre = await db.fetch_val(
        "SELECT pending_dfun FROM discfun_stakes "
        "WHERE guild_id=$1 AND user_id=$2 AND symbol=$3",
        guild_id, user_id, sym,
    )
    pre_raw = int(pre or 0)
    if pre_raw <= 0:
        return 0
    await db.execute(
        "UPDATE discfun_stakes "
        "   SET total_claimed = total_claimed + $4, "
        "       pending_dfun  = 0 "
        " WHERE guild_id=$1 AND user_id=$2 AND symbol=$3",
        guild_id, user_id, sym, pre_raw,
    )
    await db.update_wallet_holding(user_id, guild_id, "dsc", "DFUN", pre_raw)
    return pre_raw


async def claim_stake(
    db, *, guild_id: int, user_id: int, symbol: str,
) -> int:
    """Public claim entrypoint -- accrues then sweeps pending DFUN."""
    sym = symbol.upper()
    await _accrue_stake(db, guild_id=guild_id, user_id=user_id, symbol=sym)
    return await _claim_pending(db, guild_id=guild_id, user_id=user_id, symbol=sym)


async def claim_all_stakes(db, *, guild_id: int, user_id: int) -> int:
    """Accrue + claim across every active stake. Returns total DFUN paid."""
    rows = await db.fetch_all(
        "SELECT symbol FROM discfun_stakes "
        "WHERE guild_id=$1 AND user_id=$2 AND amount > 0",
        guild_id, user_id,
    )
    total = 0
    for r in rows:
        try:
            total += await claim_stake(
                db, guild_id=guild_id, user_id=user_id, symbol=str(r["symbol"]),
            )
        except Exception:
            log.exception("claim_all_stakes: skipping symbol=%s", r.get("symbol"))
    return total


async def list_user_stakes(
    db, guild_id: int, user_id: int, *, accrue: bool = True,
) -> list[dict]:
    """All active stakes for a user, optionally accruing pending first.

    The accrue pass is a write, so set ``accrue=False`` for read-only paths
    that just want a snapshot (e.g. rendering a status embed without
    persisting yield until the user claims).
    """
    if accrue:
        rows0 = await db.fetch_all(
            "SELECT symbol FROM discfun_stakes "
            "WHERE guild_id=$1 AND user_id=$2 AND amount > 0",
            guild_id, user_id,
        )
        for r in rows0:
            await _accrue_stake(db, guild_id=guild_id, user_id=user_id, symbol=str(r["symbol"]))
    return await db.fetch_all(
        "SELECT * FROM discfun_stakes "
        "WHERE guild_id=$1 AND user_id=$2 AND amount > 0 "
        "ORDER BY amount DESC",
        guild_id, user_id,
    )


async def list_graduated_holdings(db, guild_id: int, user_id: int) -> list[dict]:
    """Wallet holdings of graduated Disc.Fun tokens. Used by `,fun stake everything`."""
    return await db.fetch_all(
        "SELECT wh.symbol, wh.amount, p.emoji, p.name "
        "FROM wallet_holdings wh "
        "JOIN proto_tokens p "
        "  ON p.guild_id = wh.guild_id "
        " AND p.symbol   = wh.symbol "
        " AND p.graduated = TRUE "
        "WHERE wh.user_id=$1 AND wh.guild_id=$2 "
        "  AND wh.network='dsc' AND wh.amount > 0",
        user_id, guild_id,
    )
