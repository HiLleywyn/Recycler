"""services/gamba.py  -  Gamba Network economy: stake yield + token mints.

The Gamba Network is a closed earn-only economy attached to the gambling
surface. Each gamba game (chess, checkers, mines, dice, coinflip, blackjack,
roulette, slots) mints a small amount of its themed token (GAMBIT / CROWN /
VEIN / PIP / EDGE / ACE / NOIR / CHERRY) on every win, alongside the
existing USD payout. Players stake those tokens to drip a yield token --
GBC by default, or BUD when the position's ``yield_target`` is flipped.
The math is identical to fishing's LURE -> REEL stake yield:

    accrued_raw = staked_raw * STAKE_RATE_BY_TARGET[target] * elapsed_days

Lazy accrual: every stake / unstake / claim re-computes elapsed yield since
``last_accrue`` and adds it to ``pending_yield_raw``. No background tick
needed; the position keeps earning across bot restarts because the
timestamp lives on the DB clock.

The cog (cogs/gamba.py) wires this service into the unified
``core.framework.staking`` panel; the existing chess / checkers cogs and the
six gambling games in cogs/play.py call :func:`award_game_token` on every
win. ``,gamba shop`` reads ``items_config.SHOP_ITEMS`` filtered by the
GBC currency.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time as _time
from dataclasses import dataclass
from typing import Any

from core.config import Config
from core.framework.scale import to_human, to_raw

log = logging.getLogger(__name__)


# ── Constants pulled out of Config so callers don't have to ─────────────
GAMBA_NETWORK: str = "Gamba Network"
GAMBA_NETWORK_SHORT: str = Config.GAMBA_NETWORK_SHORT
GBC_SYMBOL: str = Config.GAMBA_COIN
GAME_TOKEN: dict[str, str] = dict(Config.GAMBA_GAME_TOKEN)
GAME_TOKEN_SET: frozenset[str] = frozenset(GAME_TOKEN.values())
STAKE_GBC_PER_DAY: float = float(Config.GAMBA_STAKE_GBC_PER_DAY)
STAKE_BUD_PER_DAY: float = float(Config.GAMBA_STAKE_BUD_PER_DAY)
TOKEN_MINT_PER_USD_WIN: float = float(Config.GAMBA_TOKEN_MINT_PER_USD_WIN)
# LP-holder kickback on a GBC cashout. Mirrors buddy_economy's
# BUD_CASHOUT_LP_REWARD_BPS (1%) so any future GBC pool benefits the
# same way RUNE / HRV / BUD pools do.
GBC_CASHOUT_LP_REWARD_BPS: int = 100

# Per-position yield-target. Each gamba_stakes row picks one target;
# the same staked PIP / ACE / etc. drips one or the other, never both.
# Adding a future target (REEL, HRV, ...) is a one-line config change.
YIELD_TARGET_GBC: str = "GBC"
YIELD_TARGET_BUD: str = "BUD"
YIELD_TARGETS: frozenset[str] = frozenset({YIELD_TARGET_GBC, YIELD_TARGET_BUD})
STAKE_RATE_BY_TARGET: dict[str, float] = {
    YIELD_TARGET_GBC: STAKE_GBC_PER_DAY,
    YIELD_TARGET_BUD: STAKE_BUD_PER_DAY,
}


def _normalise_target(target: str | None) -> str:
    """Resolve a target string to a canonical YIELD_TARGETS member.

    Accepts None / empty / unknown by returning the GBC default so
    legacy rows (or callers that haven't been updated) keep working.
    """
    t = (target or "").strip().upper()
    return t if t in YIELD_TARGETS else YIELD_TARGET_GBC


def game_token_for(game: str) -> str | None:
    """Return the earn-only token symbol minted on a win in ``game``.

    ``game`` is the canonical game name (chess / checkers / mines / dice /
    coinflip / blackjack / roulette / slots). Unknown game names return
    ``None`` so callers can no-op without raising.
    """
    return GAME_TOKEN.get((game or "").lower())


# ============================================================================
# Yield accrual  -  identical math to services/fishing.py:_accrue_pending
# ============================================================================

def _accrue_pending(
    staked_raw: int, last_at: Any, target: str,
) -> tuple[int, int]:
    """Return ``(elapsed_seconds, accrued_yield_raw)`` for a stake position.

    Mirrors fishing's helper exactly so the staking grind feels uniform
    across networks. ``last_at`` may be a datetime, an epoch float (per
    the project's _coerce convention), or None. ``target`` selects which
    rate from ``STAKE_RATE_BY_TARGET`` is applied.
    """
    if staked_raw <= 0 or not last_at:
        return 0, 0
    rate = STAKE_RATE_BY_TARGET.get(_normalise_target(target), 0.0)
    if rate <= 0:
        return 0, 0
    if isinstance(last_at, _dt.datetime):
        last_ts = last_at.timestamp()
    else:
        last_ts = float(last_at)
    now_ts = float(_time.time())
    elapsed = max(0, int(now_ts - last_ts))
    if elapsed <= 0:
        return 0, 0
    rate_raw = to_raw(rate)
    accrued_raw = (staked_raw * rate_raw * elapsed) // (to_raw(1.0) * 86400)
    return elapsed, int(accrued_raw)


# ============================================================================
# Stake row helpers
# ============================================================================

@dataclass
class StakeRow:
    """Snapshot of a single (user, symbol) stake position."""
    user_id: int
    guild_id: int
    symbol: str
    staked_raw: int
    pending_yield_raw: int
    total_claimed_raw: int
    auto_compound: bool
    total_compounded_raw: int
    yield_target: str


async def _ensure_row(db: Any, guild_id: int, user_id: int, symbol: str) -> dict:
    """Return the gamba_stakes row, inserting an empty one if missing."""
    await db.execute(
        """
        INSERT INTO gamba_stakes (user_id, guild_id, symbol)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, guild_id, symbol) DO NOTHING
        """,
        user_id, guild_id, symbol,
    )
    row = await db.fetch_one(
        """
        SELECT * FROM gamba_stakes
         WHERE user_id=$1 AND guild_id=$2 AND symbol=$3
        """,
        user_id, guild_id, symbol,
    )
    return dict(row or {})


def _row_snapshot(row: dict) -> StakeRow:
    return StakeRow(
        user_id=int(row.get("user_id") or 0),
        guild_id=int(row.get("guild_id") or 0),
        symbol=str(row.get("symbol") or ""),
        staked_raw=int(row.get("amount") or 0),
        pending_yield_raw=int(row.get("pending_yield_raw") or 0),
        total_claimed_raw=int(row.get("total_claimed") or 0),
        auto_compound=bool(row.get("auto_compound") or False),
        total_compounded_raw=int(row.get("total_compounded") or 0),
        yield_target=_normalise_target(row.get("yield_target")),
    )


async def get_stake(db: Any, guild_id: int, user_id: int, symbol: str) -> StakeRow:
    """Return a fresh snapshot of the stake position (no accrual write)."""
    row = await _ensure_row(db, guild_id, user_id, symbol)
    return _row_snapshot(row)


async def list_stakes(db: Any, guild_id: int, user_id: int) -> list[StakeRow]:
    """All non-zero gamba stake rows for a user, sorted by symbol."""
    rows = await db.fetch_all(
        """
        SELECT * FROM gamba_stakes
         WHERE guild_id=$1 AND user_id=$2 AND amount > 0
         ORDER BY symbol
        """,
        guild_id, user_id,
    )
    return [_row_snapshot(dict(r)) for r in (rows or [])]


async def accrued_yield(
    db: Any, guild_id: int, user_id: int, symbol: str,
) -> tuple[int, str]:
    """Read-only: pending payout (raw) + the yield target.

    Returns ``(pending_raw, target)`` where ``target`` is ``"GBC"`` or
    ``"BUD"``. Callers that only care about the raw amount can index
    ``[0]``; renderers that show the symbol next to the amount want
    ``[1]`` to pick the right oracle / emoji.
    """
    row = await _ensure_row(db, guild_id, user_id, symbol)
    staked = int(row.get("amount") or 0)
    pending = int(row.get("pending_yield_raw") or 0)
    target = _normalise_target(row.get("yield_target"))
    _, fresh = _accrue_pending(staked, row.get("last_accrue"), target)
    return pending + fresh, target


async def total_accrued_yield(
    db: Any, guild_id: int, user_id: int,
) -> dict[str, int]:
    """Sum of pending payouts across every game-token stake, keyed by target.

    Returns ``{"GBC": raw, "BUD": raw}`` -- unconditionally includes
    both keys (zero when no positions point at that target) so callers
    don't need a defensive ``.get``.
    """
    rows = await db.fetch_all(
        """
        SELECT amount, pending_yield_raw, last_accrue, yield_target
          FROM gamba_stakes
         WHERE guild_id=$1 AND user_id=$2 AND amount > 0
        """,
        guild_id, user_id,
    )
    out: dict[str, int] = {YIELD_TARGET_GBC: 0, YIELD_TARGET_BUD: 0}
    for r in rows or []:
        staked = int(r.get("amount") or 0)
        pending = int(r.get("pending_yield_raw") or 0)
        target = _normalise_target(r.get("yield_target"))
        _, fresh = _accrue_pending(staked, r.get("last_accrue"), target)
        out[target] = out.get(target, 0) + pending + fresh
    return out


# ============================================================================
# Stake / unstake / claim  -  match the discfun_stakes flow
# ============================================================================

@dataclass
class StakeResult:
    symbol: str
    staked_raw: int
    delta_raw: int
    yield_paid_raw: int
    pending_yield_raw: int
    yield_target: str


# Buddy Network constants used by the BUD-target payout dispatch.
# Imported lazily inside _credit_yield to avoid a top-level cyclic
# import (services.buddy_economy already imports nothing from here,
# but pulling Config + the symbol/short into a function-local lookup
# keeps that decoupled regardless).
_BUD_NETWORK_SHORT: str = "bud"
_BUD_SYMBOL: str = "BUD"


async def _credit_yield(
    db: Any, guild_id: int, user_id: int, target: str, amount_raw: int,
) -> int:
    """Credit ``amount_raw`` of the target yield token to the user's wallet.

    Returns the actual amount credited (drops to 0 on credit failure so
    the caller can record the right pending-cleared total without aborting
    the stake-row update). GBC lands in wallet_holdings on the Gamba
    Network short ("gam"); BUD lands in wallet_holdings on the Buddy
    Network short. Both paths flow through the DeFi wallet so ``,wallet
    list`` surfaces every gamba balance.

    Routes the credit through the Wealth Bottleneck so high-rank players
    have their yield throttled and bottom-rank players get a USD wallet
    top-up sourced from the per-guild pool.
    """
    if amount_raw <= 0:
        return 0
    target = _normalise_target(target)
    sym = GBC_SYMBOL if target == YIELD_TARGET_GBC else _BUD_SYMBOL
    net_short = GAMBA_NETWORK_SHORT if target == YIELD_TARGET_GBC else _BUD_NETWORK_SHORT
    try:
        from services.bottleneck import apply_bottleneck, CreditKind
        bn = await apply_bottleneck(
            db, uid=int(user_id), gid=int(guild_id),
            gross_raw=int(amount_raw),
            kind=CreditKind.GAMBA_YIELD, symbol=sym,
        )
        if bn.net_credit_raw > 0:
            await db.update_wallet_holding(
                user_id, guild_id, net_short, sym, int(bn.net_credit_raw),
            )
        if bn.boost_wallet_raw > 0:
            await db.update_wallet(
                user_id, guild_id, int(bn.boost_wallet_raw),
            )
        return int(bn.net_credit_raw)
    except Exception:
        log.exception(
            "gamba._credit_yield: %s payout failed uid=%s gid=%s amt=%s",
            target, user_id, guild_id, amount_raw,
        )
        return 0


async def stake(
    db: Any, guild_id: int, user_id: int, symbol: str, amount_raw: int,
    *, yield_target: str | None = None,
) -> StakeResult:
    """Move a game token from wallet -> stake. Crystallises pending yield first.

    Optional ``yield_target`` opens (or rotates) the position to drip BUD
    instead of the default GBC. When omitted, the existing target on the
    row is preserved -- new rows default to GBC.
    """
    if symbol not in GAME_TOKEN_SET:
        raise ValueError(
            f"{symbol} is not a Gamba Network game token. "
            f"Stakeable: {', '.join(sorted(GAME_TOKEN_SET))}."
        )
    if amount_raw <= 0:
        raise ValueError("Amount must be positive.")

    row = await _ensure_row(db, guild_id, user_id, symbol)
    cur_staked = int(row.get("amount") or 0)
    pending = int(row.get("pending_yield_raw") or 0)
    cur_target = _normalise_target(row.get("yield_target"))
    new_target = (
        _normalise_target(yield_target) if yield_target is not None else cur_target
    )

    # If the stake already had a different target, crystallise pending
    # at the OLD target's rate and pay it out now -- otherwise the
    # carried-over pending would mint as the new target's token, which
    # is free yield + a wrong-token receipt. set_yield_target shares
    # this rule, so the helper takes care of it.
    if cur_target != new_target and (cur_staked > 0 or pending > 0):
        await set_yield_target(db, guild_id, user_id, symbol, new_target)
        # Re-read after the flip so we don't double-account the pending.
        row = await _ensure_row(db, guild_id, user_id, symbol)
        cur_staked = int(row.get("amount") or 0)
        pending = int(row.get("pending_yield_raw") or 0)
        cur_target = new_target

    _, fresh = _accrue_pending(cur_staked, row.get("last_accrue"), cur_target)
    new_pending = pending + fresh

    # Burn token from wallet (raises ValueError on insufficient funds).
    await db.update_wallet_holding(
        user_id, guild_id, GAMBA_NETWORK_SHORT, symbol, -int(amount_raw),
    )
    new_staked = cur_staked + int(amount_raw)
    await db.execute(
        """
        UPDATE gamba_stakes
           SET amount            = $4::numeric,
               pending_yield_raw = $5::numeric,
               yield_target      = $6,
               last_accrue       = NOW()
         WHERE user_id=$1 AND guild_id=$2 AND symbol=$3
        """,
        user_id, guild_id, symbol,
        int(new_staked), int(new_pending), cur_target,
    )
    return StakeResult(
        symbol=symbol,
        staked_raw=int(new_staked),
        delta_raw=int(amount_raw),
        yield_paid_raw=0,
        pending_yield_raw=int(new_pending),
        yield_target=cur_target,
    )


async def unstake(
    db: Any, guild_id: int, user_id: int, symbol: str, amount_raw: int,
) -> StakeResult:
    """Move a game token from stake -> wallet. Auto-claims pending yield."""
    if symbol not in GAME_TOKEN_SET:
        raise ValueError(f"{symbol} is not a Gamba Network game token.")
    requested = max(0, int(amount_raw))
    row = await _ensure_row(db, guild_id, user_id, symbol)
    cur_staked = int(row.get("amount") or 0)
    pending = int(row.get("pending_yield_raw") or 0)
    target = _normalise_target(row.get("yield_target"))
    _, fresh = _accrue_pending(cur_staked, row.get("last_accrue"), target)
    payout = pending + fresh

    if cur_staked <= 0 or requested <= 0:
        raise ValueError(f"You have no {symbol} staked.")
    actual = min(requested, cur_staked)
    new_staked = cur_staked - actual

    # Credit unlocked tokens first; if that fails the row stays unchanged.
    await db.update_wallet_holding(
        user_id, guild_id, GAMBA_NETWORK_SHORT, symbol, int(actual),
    )
    if payout > 0:
        payout = await _credit_yield(db, guild_id, user_id, target, int(payout))
    await db.execute(
        """
        UPDATE gamba_stakes
           SET amount            = $4::numeric,
               pending_yield_raw = 0,
               total_claimed     = total_claimed + $5::numeric,
               last_accrue       = NOW()
         WHERE user_id=$1 AND guild_id=$2 AND symbol=$3
        """,
        user_id, guild_id, symbol, int(new_staked), int(payout),
    )
    return StakeResult(
        symbol=symbol,
        staked_raw=int(new_staked),
        delta_raw=-int(actual),
        yield_paid_raw=int(payout),
        pending_yield_raw=0,
        yield_target=target,
    )


async def claim(
    db: Any, guild_id: int, user_id: int, symbol: str | None = None,
) -> StakeResult:
    """Pay out accrued yield. ``symbol=None`` claims across every game token.

    For ``symbol=None`` the returned ``StakeResult`` has ``symbol = ""`` and
    ``yield_paid_raw`` is the sum across every position. Stakes stay locked.
    Honours ``auto_compound`` per-position: when on, the yield payout is
    rolled into the same stake's ``amount`` instead of being credited
    (regardless of yield target -- the rolled raw becomes more game token).
    The returned ``yield_target`` is the most-recent position's target;
    in the multi-target case, callers wanting the breakdown should call
    :func:`total_accrued_yield` instead.
    """
    if symbol is not None and symbol not in GAME_TOKEN_SET:
        raise ValueError(f"{symbol} is not a Gamba Network game token.")
    if symbol is None:
        rows = await list_stakes(db, guild_id, user_id)
    else:
        rows = [await get_stake(db, guild_id, user_id, symbol)]

    total_paid = 0
    last_staked = 0
    last_sym = symbol or ""
    last_target = YIELD_TARGET_GBC
    for snap in rows:
        if snap.staked_raw <= 0:
            continue
        # Re-read with the row's last_accrue so the math stays atomic.
        row = await _ensure_row(db, guild_id, user_id, snap.symbol)
        staked = int(row.get("amount") or 0)
        pending = int(row.get("pending_yield_raw") or 0)
        ac = bool(row.get("auto_compound") or False)
        target = _normalise_target(row.get("yield_target"))
        _, fresh = _accrue_pending(staked, row.get("last_accrue"), target)
        payout = pending + fresh
        if payout <= 0:
            continue
        if ac:
            # Auto-compound: convert pending yield -> additional stake.
            # 1:1 raw mapping (mirrors discfun's auto-compound) regardless
            # of yield target -- the player picks up extra game token,
            # which on the next cycle accrues at the same target's rate.
            new_staked = staked + payout
            await db.execute(
                """
                UPDATE gamba_stakes
                   SET amount            = $4::numeric,
                       pending_yield_raw = 0,
                       total_compounded  = total_compounded + $5::numeric,
                       last_accrue       = NOW()
                 WHERE user_id=$1 AND guild_id=$2 AND symbol=$3
                """,
                user_id, guild_id, snap.symbol, int(new_staked), int(payout),
            )
            last_staked = int(new_staked)
        else:
            credited = await _credit_yield(
                db, guild_id, user_id, target, int(payout),
            )
            if credited <= 0:
                continue
            await db.execute(
                """
                UPDATE gamba_stakes
                   SET pending_yield_raw = 0,
                       total_claimed     = total_claimed + $4::numeric,
                       last_accrue       = NOW()
                 WHERE user_id=$1 AND guild_id=$2 AND symbol=$3
                """,
                user_id, guild_id, snap.symbol, int(credited),
            )
            payout = credited
            last_staked = int(staked)
        total_paid += int(payout)
        last_sym = snap.symbol
        last_target = target

    if total_paid <= 0:
        raise ValueError(
            "No yield has accrued yet. Try again after some time has passed."
        )
    return StakeResult(
        symbol=last_sym if symbol else "",
        staked_raw=int(last_staked),
        delta_raw=0,
        yield_paid_raw=int(total_paid),
        pending_yield_raw=0,
        yield_target=last_target,
    )


async def set_autocompound(
    db: Any, guild_id: int, user_id: int, symbol: str, on: bool,
) -> bool:
    """Toggle auto-compound on a single game-token stake. Returns final state."""
    if symbol not in GAME_TOKEN_SET:
        raise ValueError(f"{symbol} is not a Gamba Network game token.")
    await _ensure_row(db, guild_id, user_id, symbol)
    new = bool(on)
    await db.execute(
        """
        UPDATE gamba_stakes
           SET auto_compound = $4
         WHERE user_id=$1 AND guild_id=$2 AND symbol=$3
        """,
        user_id, guild_id, symbol, new,
    )
    return new


async def set_yield_target(
    db: Any, guild_id: int, user_id: int, symbol: str, target: str,
) -> tuple[str, int]:
    """Switch the yield target on a single game-token stake.

    Crystallises the existing pending payout AT THE OLD TARGET'S RATE,
    pays it out to the OLD target's wallet (so a player flipping
    GBC -> BUD doesn't lose 12h of accrued GBC), then flips the row
    and resets ``last_accrue``. Returns ``(new_target, paid_raw)``.

    No-op (returns ``(current_target, 0)``) when the requested target
    matches the row's existing target. Raises ``ValueError`` for an
    unknown symbol or target.
    """
    if symbol not in GAME_TOKEN_SET:
        raise ValueError(f"{symbol} is not a Gamba Network game token.")
    new_target = _normalise_target(target)
    if (target or "").strip().upper() not in YIELD_TARGETS:
        raise ValueError(
            f"Unknown yield target: {target!r}. "
            f"Use one of: {', '.join(sorted(YIELD_TARGETS))}."
        )
    row = await _ensure_row(db, guild_id, user_id, symbol)
    cur_target = _normalise_target(row.get("yield_target"))
    if cur_target == new_target:
        return new_target, 0

    staked = int(row.get("amount") or 0)
    pending = int(row.get("pending_yield_raw") or 0)
    _, fresh = _accrue_pending(staked, row.get("last_accrue"), cur_target)
    payout_old = pending + fresh
    paid = 0
    if payout_old > 0:
        paid = await _credit_yield(
            db, guild_id, user_id, cur_target, int(payout_old),
        )

    await db.execute(
        """
        UPDATE gamba_stakes
           SET yield_target      = $4,
               pending_yield_raw = 0,
               total_claimed     = total_claimed + $5::numeric,
               last_accrue       = NOW()
         WHERE user_id=$1 AND guild_id=$2 AND symbol=$3
        """,
        user_id, guild_id, symbol, new_target, int(paid),
    )
    return new_target, int(paid)


# ============================================================================
# GBC -> USD burn cashout  -  mirrors fishing.cashout_reel / dungeon.cashout_rune
# ============================================================================
#
# Same firewall shape as the other earn-only network coins (REEL / RUNE /
# HRV / BUD / FORGE): one-way burn at the live oracle minus impact-based
# slippage. No fixed haircut -- the slippage IS the fee, and dust cashouts
# get dust slippage. The eight game tokens (PIP / ACE / VEIN / ...) stay
# in stakes / wallet untouched, so a player can cash out their GBC and
# still unstake or restake their game-token positions afterwards.

def _price_impact_max() -> float:
    return float(getattr(Config, "PRICE_IMPACT_MAX", 0.40))


async def _oracle_price_db(db: Any, guild_id: int, symbol: str) -> float:
    row = await db.fetch_one(
        "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), symbol.upper(),
    )
    if row and row.get("price") is not None:
        return float(row["price"])
    return float(Config.TOKENS.get(symbol.upper(), {}).get("start_price", 1.0))


async def _supply_human(db: Any, guild_id: int, symbol: str) -> float:
    row = await db.fetch_one(
        "SELECT circulating_supply FROM crypto_prices "
        "WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), symbol.upper(),
    )
    return to_human(int((row or {}).get("circulating_supply") or 0))


def _price_impact(usd_value: float, oracle: float, supply_human: float) -> float:
    """Same impact formula .buy / .sell / fishing / dungeon use."""
    impact = usd_value / float(Config.PRICE_IMPACT_DIVISOR)
    market_cap = max(0.0, oracle * supply_human)
    if market_cap > 0 and usd_value > 0.001 * market_cap:
        mc_ratio = usd_value / market_cap
        impact *= min(1.0 + mc_ratio * 2.0, 5.0)
    return min(impact, _price_impact_max())


def _minute_ts() -> int:
    return int(_time.time()) // 60 * 60


async def _write_burn_candle(
    db: Any, guild_id: int, symbol: str,
    oracle_before: float, oracle_after: float, volume_usd: float,
) -> None:
    try:
        await db.upsert_candle(
            int(guild_id), f"{symbol.upper()}USD", _minute_ts(),
            open_=float(oracle_before),
            high=max(float(oracle_before), float(oracle_after)),
            low=min(float(oracle_before), float(oracle_after)),
            close=float(oracle_after),
            volume_delta=float(max(0.0, volume_usd)),
        )
    except Exception:
        log.exception("gamba candle write failed gid=%s sym=%s", guild_id, symbol)


async def _distribute_burn_lp_reward(
    db: Any, guild_id: int, symbol: str, fee_usd: float,
) -> float:
    """Pay a USD slice to LP holders of any pool containing ``symbol``.
    Mirrors services.fishing._distribute_burn_lp_reward exactly so any
    future GBC pool earns the same kickback fishing/dungeon pools do.
    """
    if fee_usd <= 0:
        return 0.0
    sym = symbol.upper()
    rows = await db.fetch_all(
        """
        SELECT lp.user_id, lp.pool_id, lp.lp_shares, p.total_lp
          FROM lp_positions lp
          JOIN pools p
            ON p.pool_id = lp.pool_id
           AND p.guild_id = lp.guild_id
         WHERE lp.guild_id = $1
           AND lp.lp_shares > 0
           AND COALESCE(p.vault_locked, FALSE) = FALSE
           AND (p.token_a = $2 OR p.token_b = $2)
        """,
        int(guild_id), sym,
    )
    if not rows:
        return 0.0
    weights: list[tuple[int, float]] = []
    total_weight = 0.0
    for r in rows:
        total_lp = int(r.get("total_lp") or 0)
        shares = int(r.get("lp_shares") or 0)
        if total_lp <= 0 or shares <= 0:
            continue
        w = shares / total_lp
        weights.append((int(r["user_id"]), w))
        total_weight += w
    if total_weight <= 0:
        return 0.0
    paid = 0.0
    for uid, w in weights:
        payout_usd = fee_usd * (w / total_weight)
        payout_raw = to_raw(payout_usd)
        if payout_raw <= 0:
            continue
        try:
            async with db.atomic():
                await db.update_wallet(uid, int(guild_id), int(payout_raw))
                await db.log_tx(
                    int(guild_id), uid, "LP_BURN_REWARD",
                    symbol_in=sym,
                    symbol_out="USD", amount_out=int(payout_raw),
                    network="usd",
                )
            paid += payout_usd
        except Exception:
            log.exception(
                "gamba LP reward credit failed gid=%s uid=%s sym=%s usd=%.6f",
                guild_id, uid, sym, payout_usd,
            )
    return paid


@dataclass
class CashoutResult:
    """Receipt for a single GBC -> USD burn cashout."""
    gbc_burned_raw: int
    usd_credited_raw: int
    gbc_oracle_before: float
    gbc_oracle_after: float
    price_impact_pct: float
    revenue_usd: float
    lp_reward_usd: float


async def get_gbc_wallet_raw(
    db: Any, guild_id: int, user_id: int,
) -> int:
    """Read the user's current raw GBC balance from the gam-network wallet."""
    row = await db.get_wallet_holding(
        int(user_id), int(guild_id), GAMBA_NETWORK_SHORT, GBC_SYMBOL,
    )
    return int((row or {}).get("amount") or 0)


async def get_game_token_wallet_raw(
    db: Any, guild_id: int, user_id: int, symbol: str,
) -> int:
    """Read the user's current raw balance of a Gamba Network token.

    Used by callers that need to read GBC or any of the eight game tokens
    (PIP / ACE / VEIN / EDGE / NOIR / CHERRY / GAMBIT / CROWN) without
    knowing the storage layout. Mirrors :func:`get_gbc_wallet_raw` and
    keeps the wallet_holdings dispatch in one place.
    """
    sym = (symbol or "").upper()
    row = await db.get_wallet_holding(
        int(user_id), int(guild_id), GAMBA_NETWORK_SHORT, sym,
    )
    return int((row or {}).get("amount") or 0)


async def cashout_gbc(
    db: Any, guild_id: int, user_id: int, gbc_amount_raw: int,
) -> CashoutResult:
    """Burn GBC, push the GBC oracle DOWN, credit the user's USD wallet.

    Identical mechanics to ``services/fishing.cashout_reel`` and
    ``services/dungeon.cashout_rune``. The full quantity leaves
    ``wallet_holdings`` on the ``gam`` network short (which auto-decrements
    ``crypto_prices.circulating_supply`` in
    ``database.users.update_wallet_holding`` -- that IS the burn), the
    standard ``Config.PRICE_IMPACT_DIVISOR`` formula computes a downward
    price impact, and the user receives USD at the post-impact GBC oracle.

    The eight game-token positions (PIP / ACE / VEIN / EDGE / NOIR /
    CHERRY / GAMBIT / CROWN) are not touched -- a player can keep cashing
    out their accumulated GBC without losing their stake yield engine.
    """
    if gbc_amount_raw <= 0:
        raise ValueError("Amount must be positive.")

    held = await get_gbc_wallet_raw(db, guild_id, user_id)
    if held < int(gbc_amount_raw):
        raise ValueError(f"You only have {to_human(held):,.4f} GBC.")

    oracle_before = await _oracle_price_db(db, guild_id, GBC_SYMBOL)
    if oracle_before <= 0:
        raise ValueError("GBC oracle price is currently zero -- try again later.")

    gbc_human = to_human(int(gbc_amount_raw))
    revenue_usd = gbc_human * oracle_before
    supply_human = await _supply_human(db, guild_id, GBC_SYMBOL)
    impact = _price_impact(revenue_usd, oracle_before, supply_human)

    # Effective sell price (post-impact). Average between pre- and
    # post-impact oracle, identical to .sell + every other cashout path.
    eff_price = oracle_before * (1.0 - impact / 2.0)
    usd_credit_human = gbc_human * eff_price
    usd_credit_raw = to_raw(usd_credit_human)

    # Reject dust cashouts BEFORE the burn. If the credit would round to
    # 0 raw, the burn would still execute -- erasing the player's GBC for
    # nothing. Surface a clear error so the player can cash out a larger
    # amount instead.
    if usd_credit_raw <= 0:
        raise ValueError(
            "Amount too small to cash out -- USD credit would round to "
            "zero. Try a larger amount."
        )

    # Burn + credit must commit together. db.atomic() rolls both back if
    # the credit fails, so a transient DB error can never leave the
    # player with the GBC gone and no USD credited.
    async with db.atomic():
        await db.update_wallet_holding(
            int(user_id), int(guild_id),
            GAMBA_NETWORK_SHORT, GBC_SYMBOL, -int(gbc_amount_raw),
        )
        await db.update_wallet(
            int(user_id), int(guild_id), int(usd_credit_raw),
        )

    oracle_after = max(1e-9, oracle_before * (1.0 - impact))
    try:
        await db.update_price(GBC_SYMBOL, int(guild_id), oracle_after)
    except Exception:
        log.exception(
            "cashout_gbc: oracle update failed gid=%s -- chart will lag "
            "until the next drift tick", guild_id,
        )

    await _write_burn_candle(
        db, guild_id, GBC_SYMBOL, oracle_before, oracle_after, revenue_usd,
    )

    fee_usd = revenue_usd * (int(GBC_CASHOUT_LP_REWARD_BPS) / 10_000.0)
    lp_paid = 0.0
    if fee_usd > 0:
        lp_paid = await _distribute_burn_lp_reward(
            db, guild_id, GBC_SYMBOL, fee_usd,
        )

    return CashoutResult(
        gbc_burned_raw=int(gbc_amount_raw),
        usd_credited_raw=int(usd_credit_raw),
        gbc_oracle_before=float(oracle_before),
        gbc_oracle_after=float(oracle_after),
        price_impact_pct=float(impact),
        revenue_usd=float(revenue_usd),
        lp_reward_usd=float(lp_paid),
    )


# ============================================================================
# Win-side mints  -  called from the gambling cogs after every win
# ============================================================================

async def award_game_token(
    db: Any, guild_id: int, user_id: int, game: str, profit_usd: float,
    *, side_bet_double: bool = False,
) -> tuple[str, int]:
    """Mint the game-themed token on a win. Returns ``(symbol, amount_raw)``.

    ``profit_usd`` is the human-USD profit on the win (not the gross
    payout). A non-positive profit returns ``("", 0)`` so the caller can
    no-op without conditional logic. ``side_bet_double`` doubles the mint
    when the player has armed a Side Bet Slip consumable.
    """
    sym = game_token_for(game)
    if not sym:
        return "", 0
    if profit_usd <= 0:
        return sym, 0
    multiplier = 2.0 if side_bet_double else 1.0
    # V3 Pillar 6: Apex Event modifiers stack on top (e.g. Blood Moon +15%).
    try:
        from services import apex_events as _ev
        multiplier *= float(await _ev.modifier(db, guild_id, "gamba.payout", 1.0))
    except Exception:
        pass
    amount_human = float(profit_usd) * TOKEN_MINT_PER_USD_WIN * multiplier
    if amount_human <= 0:
        return sym, 0
    amount_raw = int(to_raw(amount_human))
    if amount_raw <= 0:
        return sym, 0
    try:
        await db.update_wallet_holding(
            user_id, guild_id, GAMBA_NETWORK_SHORT, sym, int(amount_raw),
        )
    except Exception:
        log.exception(
            "gamba.award_game_token: mint failed uid=%s gid=%s sym=%s",
            user_id, guild_id, sym,
        )
        return sym, 0
    return sym, int(amount_raw)


# ============================================================================
# Effective live APY  -  for stake-panel header parity with ,fun stakes
# ============================================================================

def effective_apy_pct(target: str = YIELD_TARGET_GBC) -> float:
    """Return the headline APY for the stake panel for a given yield target.

    Game tokens drip the target's daily rate. At parity oracle prices
    (1 token = 1 yield-token) the APY is ``rate_per_day * 365``. Real
    APY varies with the yield-token oracle vs the staked-token oracle;
    callers can multiply by the price ratio for a more accurate display.
    """
    rate = STAKE_RATE_BY_TARGET.get(_normalise_target(target), STAKE_GBC_PER_DAY)
    return float(rate * 365.0 * 100.0)


# ============================================================================
# Gamba Shop consumables  -  shared inventory helpers
# ============================================================================

# Canonical item keys for the three GBC-priced consumables. Definitions
# (name / cost / description) live in items_config.SHOP_ITEMS so the
# shop browser, transfer fee logic, and transfer hooks all see them.
SHOP_ITEMS: tuple[str, ...] = (
    "lucky_chip",
    "house_marker",
    "side_bet_slip",
)
SHOP_ITEM_SET: frozenset[str] = frozenset(SHOP_ITEMS)


async def get_consumable_count(
    db: Any, guild_id: int, user_id: int, item_key: str,
) -> int:
    """Return the player's current stock of a Gamba Shop consumable."""
    row = await db.fetch_one(
        """
        SELECT count FROM gamba_consumables
         WHERE user_id=$1 AND guild_id=$2 AND item_key=$3
        """,
        user_id, guild_id, item_key,
    )
    return int((row or {}).get("count") or 0)


async def list_consumables(
    db: Any, guild_id: int, user_id: int,
) -> dict[str, int]:
    """Return ``{item_key: count}`` for every gamba consumable the user owns."""
    rows = await db.fetch_all(
        """
        SELECT item_key, count FROM gamba_consumables
         WHERE user_id=$1 AND guild_id=$2 AND count > 0
        """,
        user_id, guild_id,
    )
    return {str(r["item_key"]): int(r["count"]) for r in (rows or [])}


async def add_consumable(
    db: Any, guild_id: int, user_id: int, item_key: str, qty: int = 1,
) -> int:
    """Credit ``qty`` of ``item_key``. Returns the new total."""
    if item_key not in SHOP_ITEM_SET:
        raise ValueError(f"Unknown gamba item: {item_key}")
    if qty <= 0:
        raise ValueError("Quantity must be positive.")
    new = await db.fetch_val(
        """
        INSERT INTO gamba_consumables (user_id, guild_id, item_key, count)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, guild_id, item_key) DO UPDATE
            SET count = gamba_consumables.count + EXCLUDED.count
        RETURNING count
        """,
        user_id, guild_id, item_key, int(qty),
    )
    return int(new or 0)


async def consume_if_present(
    db: Any, guild_id: int, user_id: int, item_key: str,
) -> bool:
    """Decrement one unit of ``item_key`` if available. Returns True if consumed.

    The atomic UPDATE-with-guard pattern means concurrent gambles never
    double-spend a single chip. Used by the auto-apply hooks in the
    gambling cogs (Lucky Chip on win, House Marker on loss, Side Bet
    Slip on win).
    """
    if item_key not in SHOP_ITEM_SET:
        return False
    row = await db.fetch_val(
        """
        UPDATE gamba_consumables
           SET count = count - 1
         WHERE user_id=$1 AND guild_id=$2 AND item_key=$3 AND count > 0
        RETURNING count
        """,
        user_id, guild_id, item_key,
    )
    return row is not None


__all__ = [
    "GAMBA_NETWORK",
    "GAMBA_NETWORK_SHORT",
    "GBC_SYMBOL",
    "GAME_TOKEN",
    "GAME_TOKEN_SET",
    "STAKE_GBC_PER_DAY",
    "STAKE_BUD_PER_DAY",
    "STAKE_RATE_BY_TARGET",
    "TOKEN_MINT_PER_USD_WIN",
    "GBC_CASHOUT_LP_REWARD_BPS",
    "YIELD_TARGET_GBC",
    "YIELD_TARGET_BUD",
    "YIELD_TARGETS",
    "SHOP_ITEMS",
    "SHOP_ITEM_SET",
    "StakeResult",
    "StakeRow",
    "CashoutResult",
    "game_token_for",
    "get_stake",
    "list_stakes",
    "accrued_yield",
    "total_accrued_yield",
    "stake",
    "unstake",
    "claim",
    "set_autocompound",
    "set_yield_target",
    "award_game_token",
    "effective_apy_pct",
    "get_consumable_count",
    "list_consumables",
    "add_consumable",
    "consume_if_present",
    "get_gbc_wallet_raw",
    "cashout_gbc",
]
