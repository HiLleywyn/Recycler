"""Wealth Bottleneck.

A single, transparent, leaderboard-rank-based throttle on every USD-equivalent
gain a player earns. Replaces the legacy "Wealth Equalizer" (daily cycle that
silently drained stones / bags / rigs / validator stakes / delegations / moon
stakes / mining rigs from rich players) and the V3 "Continuous Wealth
Equalizer" (Gini PI controller + streaming UBI ticks + per-day bonus cap).

Design (CLAUDE.md, plans/pls-stop-making-taxes-linear-wand.md):

- The bottleneck never touches a player's existing holdings. Stones, NFTs,
  CeFi bags, DeFi token holdings, validator stakes, delegations, Lunar Mint /
  Moon Pool stakes, mining rigs, LP positions, and savings deposits are
  all permanently off-limits. Only the credit currently being applied is
  scaled.
- It scales every fresh credit by a multiplier that depends only on the
  player's current rank in the guild's net-worth leaderboard:
      poorest -> 1.50x boost, median -> 1.00x neutral, richest -> 0.10x drag.
  The curve is configurable in ``Config.BOTTLENECK_CURVE``; default anchors
  are documented in :data:`BOTTLENECK_DEFAULT_CURVE`.
- The drag taken off the top of the leaderboard funds a per-guild USD pool
  (``wealth_pool``). The boost paid to the bottom is drawn from that same
  pool. When the pool is empty the boost falls to 1.0x  -  no value is
  printed out of thin air.
- Every credit that flows through ``apply_bottleneck`` writes one row to
  ``bottleneck_log`` (gross / net / drag / boost / multiplier / percentile)
  so players can audit exactly what the system did to each of their gains.

Public surface:

    from services.bottleneck import (
        CreditKind, BottleneckResult, apply_bottleneck,
        bottleneck_multiplier, lookup_percentile, get_pool_state,
    )

Caller pattern (see ``cogs/earn.py`` / ``cogs/bank.py`` / etc. for full
examples)::

    result = await apply_bottleneck(
        ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
        gross_raw=to_raw(amount), kind=CreditKind.WORK,
    )
    new_wallet_raw = await ctx.db.update_wallet(
        ctx.author.id, ctx.guild_id, result.total_to_wallet_raw,
    )
    embed.footer(fmt_bottleneck(result))

For non-stable credits (PoS rewards in network gas coin, moon-pool basket
payouts, mining rig outputs in network token, gamba game-token claims),
pass ``symbol`` and ``price_usd``; the function then scales the *token*
amount by the multiplier and routes any boost the player is owed to the
USD wallet separately. See :func:`apply_bottleneck` for the full contract.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

from core.config import Config
from core.framework.scale import to_human, to_raw
from services.net_worth import compute_bulk_net_worth

log = logging.getLogger(__name__)


# ── Credit-kind tags (audit log + display) ─────────────────────────────────

class CreditKind(str, Enum):
    """Tag for a credit, recorded in ``bottleneck_log.kind``.

    String-valued so it serialises to JSON / SQL transparently. Add new
    kinds here when wiring a new income source; the value is what shows
    up in audit dumps and ``,bottleneck me``.
    """
    WORK = "work"
    BEG = "beg"
    APE = "ape"
    DAILY = "daily"
    FAUCET = "faucet"
    DROP = "drop"
    TRADE_GAIN = "trade_gain"
    GAMBA_WIN = "gamba_win"
    GAMBA_YIELD = "gamba_yield"
    STAKE_YIELD = "stake_yield"
    LP_YIELD = "lp_yield"
    POS_REWARD = "pos_reward"
    DELEGATION_REWARD = "delegation_reward"
    MINING = "mining"
    NETWORK_CLAIM = "network_claim"
    SAVINGS_INTEREST = "savings_interest"
    OTHER = "other"


# ── Curve ──────────────────────────────────────────────────────────────────

# Anchors are (percentile, multiplier) where percentile=0.0 is the poorest
# ranked player and percentile=1.0 is the richest. Values between anchors
# are interpolated linearly; values outside are clamped to the endpoint.
# Median (0.50) sits at exactly 1.00 so half the leaderboard is boosted
# and half is dragged.
BOTTLENECK_DEFAULT_CURVE: list[tuple[float, float]] = [
    (0.00, 1.50),
    (0.25, 1.20),
    (0.50, 1.00),
    (0.75, 0.85),
    (0.90, 0.55),
    (0.99, 0.20),
    (1.00, 0.10),
]


def bottleneck_multiplier(percentile: float) -> float:
    """Return the multiplier for a player at ``percentile`` of the leaderboard.

    ``percentile`` is in ``[0.0, 1.0]`` where 0.0 = poorest, 1.0 = richest.
    Values outside the range are clamped. The curve is read from
    ``Config.BOTTLENECK_CURVE`` so a runtime tweak takes effect without
    code change.
    """
    if percentile < 0.0:
        percentile = 0.0
    if percentile > 1.0:
        percentile = 1.0
    raw = list(getattr(Config, "BOTTLENECK_CURVE", BOTTLENECK_DEFAULT_CURVE)) \
        or BOTTLENECK_DEFAULT_CURVE
    curve = sorted(((float(p), float(m)) for p, m in raw), key=lambda x: x[0])
    if percentile <= curve[0][0]:
        return curve[0][1]
    if percentile >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        lo_p, lo_m = curve[i]
        hi_p, hi_m = curve[i + 1]
        if lo_p <= percentile <= hi_p:
            t = (percentile - lo_p) / max(hi_p - lo_p, 1e-12)
            return lo_m + t * (hi_m - lo_m)
    return curve[-1][1]


def percentile_label(pctile: float) -> str:
    """Human label like ``top 10%`` / ``bottom 25%`` / ``median``.

    Used in ``fmt_bottleneck`` and the ,bottleneck command so the rank
    is communicated in the same words everywhere.
    """
    if pctile >= 0.99:
        return "top 1%"
    if pctile >= 0.90:
        return "top 10%"
    if pctile >= 0.75:
        return "top 25%"
    if pctile >= 0.55:
        return "upper half"
    if pctile >= 0.45:
        return "median"
    if pctile >= 0.25:
        return "lower half"
    if pctile >= 0.10:
        return "bottom 25%"
    if pctile >= 0.01:
        return "bottom 10%"
    return "bottom 1%"


# ── Net-worth ranking cache ────────────────────────────────────────────────
# Every credit calls into this module; ``compute_bulk_net_worth`` is
# expensive, so we keep a per-guild ranked snapshot and refresh on a
# short TTL. Tax/throttle accuracy to the millisecond is not required.

_RANK_CACHE_TTL: float = 60.0
_NW_CACHE_TTL: float = 300.0
_rank_cache: dict[int, tuple[float, list[tuple[int, float]]]] = {}
_nw_cache: dict[int, tuple[float, dict[int, float]]] = {}


async def cached_bulk_net_worth(db, guild_id: int) -> dict[int, float]:
    """Per-guild ``{uid: nw_usd}`` snapshot, cached 5 minutes.

    Used by callers that want raw NW values (e.g. ``,balance`` to show
    the current multiplier next to the user's wealth).
    """
    now = time.time()
    cached = _nw_cache.get(guild_id)
    if cached and now < cached[0]:
        return cached[1]
    snap = await compute_bulk_net_worth(
        guild_id, db, exclude_user_id=Config.COMMUNITY_RESERVE_USER_ID,
    )
    _nw_cache[guild_id] = (now + _NW_CACHE_TTL, snap)
    return snap


def invalidate_caches(guild_id: int) -> None:
    """Drop both caches for a guild  -  call after a major wealth shift."""
    _rank_cache.pop(guild_id, None)
    _nw_cache.pop(guild_id, None)


async def _ranked_networth(db, guild_id: int) -> list[tuple[int, float]]:
    """Return ``[(uid, nw)]`` sorted poorest->richest. Cached 60s per guild.

    Sorted ASCending so the index of a uid IS the player's rank from the
    bottom: rank 0 = poorest, rank n-1 = richest. percentile = rank/(n-1).
    """
    now = time.time()
    cached = _rank_cache.get(guild_id)
    if cached and now - cached[0] < _RANK_CACHE_TTL:
        return cached[1]
    try:
        nw = await cached_bulk_net_worth(db, guild_id)
    except Exception:
        log.exception("bottleneck: bulk NW lookup failed gid=%s", guild_id)
        return cached[1] if cached else []
    ranked = sorted(
        (
            (int(uid), float(v))
            for uid, v in nw.items()
            if int(uid) > 0 and float(v) > 0
        ),
        key=lambda x: x[1],
    )
    _rank_cache[guild_id] = (now, ranked)
    return ranked


async def lookup_percentile(
    db, *, uid: int, gid: int,
) -> tuple[float, float, int]:
    """Return ``(percentile, net_worth_usd, n_holders)`` for a user.

    ``percentile`` is in ``[0.0, 1.0]`` (0=poorest, 1=richest); a player
    not in the ranking (NW <= 0 or excluded) maps to 0.0 (treated as
    bottom of the curve so they get the boost tier). ``n_holders`` is the
    count of players in the live ranking, useful for the
    ``BOTTLENECK_MIN_HOLDERS`` gate.
    """
    ranked = await _ranked_networth(db, gid)
    n = len(ranked)
    if n == 0:
        return 0.0, 0.0, 0
    rank = next((i for i, (u, _) in enumerate(ranked) if u == uid), None)
    if rank is None:
        return 0.0, 0.0, n
    nw = ranked[rank][1]
    if n == 1:
        return 0.5, nw, 1  # solo guild: neutral
    pctile = rank / (n - 1)
    return pctile, nw, n


# ── Result type ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BottleneckResult:
    """Outcome of a single ``apply_bottleneck`` call.

    For STABLE-currency credits (USD / DSD), the caller credits
    :attr:`total_to_wallet_raw` as a single wallet update. For
    NON-STABLE credits, the caller credits :attr:`net_credit_raw` of
    the original token AND, separately, :attr:`boost_wallet_raw` of USD
    to the wallet.
    """
    kind: str
    symbol: str
    gross_raw: int          # what the caller would have credited unscaled
    net_credit_raw: int     # token amount actually credited (after drag)
    boost_wallet_raw: int   # USD-stable raw added to wallet from the pool
    drag_usd_raw: int       # USD-stable raw diverted into the pool
    multiplier: float       # the m value from the curve
    percentile: float       # 0.0 (poorest) .. 1.0 (richest)
    n_holders: int          # leaderboard size at lookup time
    pool_after_raw: int     # pool balance immediately after this call
    skipped: bool = False   # True when the gate (disabled/min holders/etc.) bypassed

    @property
    def total_to_wallet_raw(self) -> int:
        """Convenience for stable callers: net + boost in a single number.

        Asserts that the credit was stable so a non-stable caller doesn't
        accidentally pump foreign-token raw into the USD wallet.
        """
        assert self.symbol in _STABLE_SYMBOLS, (
            f"total_to_wallet_raw is stable-only; symbol={self.symbol!r}. "
            f"Non-stable callers must credit net_credit_raw to the token "
            f"holding and boost_wallet_raw to the USD wallet separately."
        )
        return self.net_credit_raw + self.boost_wallet_raw

    @property
    def lost_token_raw(self) -> int:
        """How many token units were not credited because of drag.

        For stable credits this equals ``drag_usd_raw``. For non-stable
        credits this is the token-side delta; the matching USD value is
        ``drag_usd_raw``.
        """
        return self.gross_raw - self.net_credit_raw


_STABLE_SYMBOLS: frozenset[str] = frozenset({"USD", "DSD"})


# ── Pool plumbing ──────────────────────────────────────────────────────────

async def _ensure_pool_row(db, guild_id: int) -> None:
    """Idempotent INSERT for the per-guild pool row."""
    await db.execute(
        "INSERT INTO wealth_pool (guild_id, pool_raw) VALUES ($1, 0) "
        "ON CONFLICT (guild_id) DO NOTHING",
        guild_id,
    )


async def get_pool_state(db, guild_id: int) -> dict:
    """Read-only snapshot of the per-guild pool: ``{pool_raw, pool_usd, updated_at}``."""
    await _ensure_pool_row(db, guild_id)
    row = await db.fetch_one(
        "SELECT pool_raw, updated_at FROM wealth_pool WHERE guild_id=$1",
        guild_id,
    )
    pool_raw = int(row.get("pool_raw") or 0) if row else 0
    return {
        "pool_raw": pool_raw,
        "pool_usd": to_human(pool_raw),
        "updated_at": row.get("updated_at") if row else None,
    }


# ── Public: apply ──────────────────────────────────────────────────────────

async def apply_bottleneck(
    db, *,
    uid: int,
    gid: int,
    gross_raw: int,
    kind: CreditKind | str,
    symbol: str = "USD",
    price_usd: float | None = None,
) -> BottleneckResult:
    """Scale a fresh income credit by the wealth bottleneck.

    Parameters
    ----------
    db
        Database handle (asyncpg-style; uses ``execute`` / ``fetch_one`` /
        ``atomic``).
    uid, gid
        User and guild IDs of the recipient.
    gross_raw
        Raw (NUMERIC(36,0) scaled by 10**18) amount in ``symbol`` that
        the caller would have credited absent the bottleneck.
    kind
        :class:`CreditKind` (or matching string) -  what kind of credit
        this is. Recorded in ``bottleneck_log`` so the audit trail is
        breakdown-able.
    symbol
        Currency of the credit. Default ``"USD"``. ``"USD"`` and ``"DSD"``
        are treated as stable (1.0x oracle); any other symbol is treated
        as a token and ``price_usd`` is used (looked up from
        ``crypto_prices`` if not supplied).
    price_usd
        Oracle price for non-stable ``symbol``. Optional; looked up when
        omitted. Pass it explicitly if the caller already has the price
        on hand to skip the lookup.

    Returns
    -------
    BottleneckResult
        See the dataclass docstring. The caller is responsible for
        applying ``net_credit_raw`` (in ``symbol``) and, for non-stable
        credits, ``boost_wallet_raw`` (USD) to the recipient.

    Notes
    -----
    Failure-tolerant: on any internal exception (NW lookup, pool read,
    etc.) the function returns a no-op result with ``skipped=True`` and
    the caller credits the gross amount unchanged. Income flow must
    never be blocked by a bottleneck hiccup.

    Excluded from bottleneck:
    - The community reserve sentinel (``Config.COMMUNITY_RESERVE_USER_ID``)
    - Any non-positive uid (group IDs, system accounts).
    - Guilds with fewer than ``Config.BOTTLENECK_MIN_HOLDERS`` players in
      the live ranking (small-server gate).
    - ``gross_raw <= 0`` (no-op).
    """
    kind_str = kind.value if isinstance(kind, CreditKind) else str(kind)
    sym = (symbol or "USD").upper()
    base = BottleneckResult(
        kind=kind_str, symbol=sym,
        gross_raw=int(gross_raw),
        net_credit_raw=int(gross_raw),
        boost_wallet_raw=0, drag_usd_raw=0,
        multiplier=1.0, percentile=0.5, n_holders=0,
        pool_after_raw=0, skipped=True,
    )

    if not getattr(Config, "BOTTLENECK_ENABLED", True):
        return base
    if int(gross_raw) <= 0:
        return base
    if uid == Config.COMMUNITY_RESERVE_USER_ID or int(uid) <= 0:
        return base

    try:
        pctile, _user_nw, n = await lookup_percentile(db, uid=uid, gid=gid)
    except Exception:
        log.exception("bottleneck: percentile lookup failed gid=%s uid=%s", gid, uid)
        return base

    min_holders = int(getattr(Config, "BOTTLENECK_MIN_HOLDERS", 5))
    if n < max(2, min_holders):
        return base

    mult = bottleneck_multiplier(pctile)
    if mult == 1.0:
        return BottleneckResult(
            kind=kind_str, symbol=sym,
            gross_raw=int(gross_raw),
            net_credit_raw=int(gross_raw),
            boost_wallet_raw=0, drag_usd_raw=0,
            multiplier=1.0, percentile=pctile, n_holders=n,
            pool_after_raw=(await get_pool_state(db, gid))["pool_raw"],
            skipped=False,
        )

    # Resolve oracle price for non-stable credits.
    if sym in _STABLE_SYMBOLS:
        price = 1.0
    else:
        if price_usd is None:
            try:
                price_row = await db.get_price(sym, gid)
                price = float(price_row["price"]) if price_row else 0.0
            except Exception:
                log.exception(
                    "bottleneck: price lookup failed gid=%s sym=%s",
                    gid, sym,
                )
                return base
        else:
            price = float(price_usd)
        if price <= 0:
            # No oracle -> no USD-equiv to log; fall through with no scaling
            # so a price-less token doesn't silently disappear.
            return base

    try:
        await _ensure_pool_row(db, gid)
        net_credit_raw = int(gross_raw)
        drag_usd_raw = 0
        boost_wallet_raw = 0

        if mult < 1.0:
            # ── Drag: shrink the credit and feed the pool. ────────────
            net_credit_raw = int(int(gross_raw) * mult)
            lost_token_raw = int(gross_raw) - net_credit_raw
            if sym in _STABLE_SYMBOLS:
                drag_usd_raw = lost_token_raw
            else:
                lost_human = to_human(lost_token_raw)
                drag_usd_raw = to_raw(lost_human * price)
            if drag_usd_raw > 0:
                async with db.atomic():
                    pool_row = await db.fetch_one(
                        "UPDATE wealth_pool SET pool_raw = pool_raw + $2, "
                        "updated_at = NOW() WHERE guild_id = $1 "
                        "RETURNING pool_raw",
                        gid, drag_usd_raw,
                    )
                    pool_after = int(pool_row.get("pool_raw") or 0) if pool_row else 0
                    await _log_row(
                        db, gid=gid, uid=uid, kind=kind_str, symbol=sym,
                        gross_raw=int(gross_raw),
                        net_credit_raw=net_credit_raw,
                        boost_wallet_raw=0,
                        drag_usd_raw=drag_usd_raw,
                        multiplier=mult, percentile=pctile,
                    )
            else:
                pool_after = (await get_pool_state(db, gid))["pool_raw"]

        else:
            # ── Boost: full credit + USD top-up from the pool. ────────
            gross_usd_human = to_human(int(gross_raw)) * price
            cap_pct = float(getattr(
                Config, "BOTTLENECK_MAX_BOOST_MULTIPLE_OF_GROSS", 1.0,
            ))
            desired_boost_usd = gross_usd_human * (mult - 1.0)
            desired_boost_usd = min(
                desired_boost_usd, gross_usd_human * cap_pct,
            )
            desired_boost_raw = to_raw(desired_boost_usd)
            pool_state = await get_pool_state(db, gid)
            pool_now_raw = int(pool_state["pool_raw"])
            boost_wallet_raw = max(0, min(desired_boost_raw, pool_now_raw))
            pool_after = pool_now_raw - boost_wallet_raw
            if boost_wallet_raw > 0:
                async with db.atomic():
                    pool_row = await db.fetch_one(
                        "UPDATE wealth_pool SET pool_raw = pool_raw - $2, "
                        "updated_at = NOW() WHERE guild_id = $1 "
                        "RETURNING pool_raw",
                        gid, boost_wallet_raw,
                    )
                    pool_after = int(pool_row.get("pool_raw") or 0) if pool_row else 0
                    await _log_row(
                        db, gid=gid, uid=uid, kind=kind_str, symbol=sym,
                        gross_raw=int(gross_raw),
                        net_credit_raw=net_credit_raw,
                        boost_wallet_raw=boost_wallet_raw,
                        drag_usd_raw=0,
                        multiplier=mult, percentile=pctile,
                    )

        return BottleneckResult(
            kind=kind_str, symbol=sym,
            gross_raw=int(gross_raw),
            net_credit_raw=net_credit_raw,
            boost_wallet_raw=boost_wallet_raw,
            drag_usd_raw=drag_usd_raw,
            multiplier=mult, percentile=pctile, n_holders=n,
            pool_after_raw=pool_after,
            skipped=False,
        )
    except Exception:
        log.exception(
            "bottleneck: apply failed gid=%s uid=%s kind=%s sym=%s",
            gid, uid, kind_str, sym,
        )
        return base


async def _log_row(
    db, *, gid: int, uid: int, kind: str, symbol: str,
    gross_raw: int, net_credit_raw: int,
    boost_wallet_raw: int, drag_usd_raw: int,
    multiplier: float, percentile: float,
) -> None:
    """Insert a single audit row into ``bottleneck_log``."""
    await db.execute(
        "INSERT INTO bottleneck_log "
        "(guild_id, user_id, kind, symbol, gross_raw, net_credit_raw, "
        " boost_wallet_raw, drag_usd_raw, multiplier, percentile) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        gid, uid, kind, symbol,
        int(gross_raw), int(net_credit_raw),
        int(boost_wallet_raw), int(drag_usd_raw),
        float(multiplier), float(percentile),
    )


# ── Public: read-side helpers (used by ,bottleneck command) ────────────────

async def get_user_history(
    db, *, uid: int, gid: int, days: int = 7,
) -> dict:
    """Aggregate one user's recent bottleneck activity.

    Returns ``{credits, drag_usd, boost_usd, net_swing_usd}`` over the
    last ``days`` days. ``net_swing_usd`` is positive when the user has
    been boosted on net, negative when dragged.
    """
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n, "
        "       COALESCE(SUM(drag_usd_raw), 0) AS drag, "
        "       COALESCE(SUM(boost_wallet_raw), 0) AS boost "
        "FROM bottleneck_log "
        "WHERE guild_id = $1 AND user_id = $2 "
        "  AND at >= NOW() - ($3 || ' days')::interval",
        gid, uid, str(int(days)),
    )
    drag = int(row.get("drag") or 0) if row else 0
    boost = int(row.get("boost") or 0) if row else 0
    n = int(row.get("n") or 0) if row else 0
    return {
        "credits": n,
        "drag_usd": to_human(drag),
        "boost_usd": to_human(boost),
        "net_swing_usd": to_human(boost - drag),
    }


async def realized_sell_gain_raw(
    db, *, uid: int, gid: int, symbol: str,
    sell_qty_raw: int, sell_price_usd: float,
) -> int:
    """Estimate realized USD gain on a single sell, in raw scaled USD.

    There is no per-position cost-basis column on ``crypto_holdings``, so
    this function reads the user's last 100 BUY transactions for the
    symbol and computes a weighted average buy price. Realized gain is
    ``max(0, (sell_price - avg_buy_price) * sell_qty)``. Returns 0 when
    the user has never bought the symbol on record (e.g. acquired via
    airdrop/faucet) so a no-history sell never gets bottlenecked as
    if the entire revenue were profit.
    """
    if int(sell_qty_raw) <= 0 or sell_price_usd <= 0:
        return 0
    rows = await db.fetch_all(
        "SELECT amount_in, amount_out FROM transactions "
        "WHERE user_id=$1 AND guild_id=$2 "
        "  AND symbol_out=$3 AND tx_type='BUY' "
        "ORDER BY ts DESC LIMIT 100",
        uid, gid, symbol,
    )
    total_cost_raw = 0
    total_qty_raw = 0
    for r in rows:
        total_cost_raw += int(r.get("amount_in") or 0)
        total_qty_raw += int(r.get("amount_out") or 0)
    if total_qty_raw <= 0:
        return 0
    avg_buy_price = (total_cost_raw / 10**18) / (total_qty_raw / 10**18)
    sell_qty = int(sell_qty_raw) / 10**18
    realized_usd = (sell_price_usd - avg_buy_price) * sell_qty
    if realized_usd <= 0:
        return 0
    return to_raw(realized_usd)


async def adaptive_faucet_multiplier(db, guild_id: int) -> float:
    """Per-capita-supply-adaptive multiplier for the auto-faucet payout.

    Curve: ``mult = REF / (REF + per_capita_supply)`` rescaled into
    ``[FAUCET_ADAPTIVE_MIN_MULT, FAUCET_ADAPTIVE_MAX_MULT]``. Pure read-
    only; safe to call from the faucet spawn path.

    Conceptually orthogonal to the wealth bottleneck (the faucet has no
    rank context  -  it spawns into a channel, not for a specific user)
    but lives here because both it and the bottleneck consult the same
    cached bulk-NW snapshot.
    """
    if not getattr(Config, "FAUCET_ADAPTIVE_ENABLED", True):
        return 1.0
    ref = max(float(Config.FAUCET_ADAPTIVE_REFERENCE_USD), 1.0)
    lo = float(Config.FAUCET_ADAPTIVE_MIN_MULT)
    hi = float(Config.FAUCET_ADAPTIVE_MAX_MULT)
    per_capita = await _supply_per_active(db, guild_id)
    if per_capita <= 0:
        return hi
    raw = ref / (ref + per_capita)
    return max(lo, min(hi, lo + (hi - lo) * raw))


async def _supply_per_active(db, guild_id: int, *, days: int = 7) -> float:
    """Total guild USD supply / active-player count (last ``days`` days)."""
    nw_map = await cached_bulk_net_worth(db, guild_id)
    total = sum(v for v in nw_map.values() if v > 0)
    active = await db.fetch_val(
        "SELECT COUNT(*) FROM users "
        "WHERE guild_id=$1 "
        "  AND (last_activity IS NULL "
        "       OR last_activity > now() - make_interval(days => $2))",
        guild_id, days,
    )
    n = int(active or 0)
    if n <= 0:
        n = sum(1 for v in nw_map.values() if v > 0) or 1
    return float(total) / float(max(n, 1))


async def get_recent_log(
    db, *, gid: int, uid: int | None = None, limit: int = 10,
) -> list[dict]:
    """Last ``limit`` log rows for a guild (optionally one user).

    Returned dicts have the same column shape as ``bottleneck_log`` plus
    ``gross_usd`` / ``net_usd`` / ``drag_usd`` / ``boost_usd`` (human
    floats) for direct rendering.
    """
    if uid is None:
        rows = await db.fetch_all(
            "SELECT * FROM bottleneck_log WHERE guild_id=$1 "
            "ORDER BY at DESC LIMIT $2",
            gid, int(limit),
        )
    else:
        rows = await db.fetch_all(
            "SELECT * FROM bottleneck_log WHERE guild_id=$1 AND user_id=$2 "
            "ORDER BY at DESC LIMIT $3",
            gid, uid, int(limit),
        )
    out = []
    for r in rows:
        out.append({
            **dict(r),
            "gross_usd": to_human(int(r.get("gross_raw") or 0)),
            "net_usd": to_human(int(r.get("net_credit_raw") or 0)),
            "drag_usd": to_human(int(r.get("drag_usd_raw") or 0)),
            "boost_usd": to_human(int(r.get("boost_wallet_raw") or 0)),
        })
    return out
