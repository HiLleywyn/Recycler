"""Liquidity service layer  -  shared by Discord commands and web API.

Handles adding and removing liquidity from AMM pools.
No Discord dependencies.

All AMM arithmetic (reserves, LP share math) stays in raw int space
so 10^18-scaled balances don't leak into IEEE-754 floats. ``to_raw``
and ``to_human`` only touch the user boundary.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from core.config import Config
from core.framework.scale import SCALE, to_human, to_raw

# NUMERIC(36, 0) ceiling: 10^36 - 1. Any raw value at or above this would
# cause a PostgreSQL "numeric field overflow" on insert/update.
_MAX_NUMERIC_36: int = 10**36 - 1


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class LPResult:
    success: bool
    tx_hash: str = ""
    amount_a: float = 0.0
    amount_b: float = 0.0
    token_a: str = ""
    token_b: str = ""
    lp_tokens: float = 0.0
    error: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_balance_raw(db, user_id: int, guild_id: int, symbol: str) -> int:
    """Return user balance for a token as a raw scaled int.

    USD uses the wallet column; everything else uses crypto_holdings.
    """
    if symbol == "USD":
        user = await db.get_user(user_id, guild_id)
        return int(user["wallet"]) if user else 0
    holding = await db.get_holding(user_id, guild_id, symbol)
    return int(holding["amount"]) if holding else 0


async def _debit_raw(db, user_id: int, guild_id: int, symbol: str, amount_raw: int) -> None:
    """Debit a raw-scaled amount of a token from the user."""
    if symbol == "USD":
        await db.update_wallet(user_id, guild_id, -amount_raw)
    else:
        await db.update_holding(user_id, guild_id, symbol, -amount_raw)


async def _credit_raw(db, user_id: int, guild_id: int, symbol: str, amount_raw: int) -> None:
    """Credit a raw-scaled amount of a token to the user."""
    if symbol == "USD":
        await db.update_wallet(user_id, guild_id, amount_raw)
    else:
        await db.update_holding(user_id, guild_id, symbol, amount_raw)


def _isqrt(n: int) -> int:
    """Integer square root for geometric-mean LP mint on an empty pool."""
    if n < 0:
        raise ValueError("isqrt of negative")
    return math.isqrt(n)


# ── Core functions ───────────────────────────────────────────────────────────

async def add_liquidity(
    db,
    guild_id: int,
    user_id: int,
    token_a: str,
    token_b: str,
    amount_a: float,
    amount_b: float = 0.0,
) -> LPResult:
    """Add liquidity to an AMM pool.

    If the pool already has reserves, ``amount_b`` is calculated from the
    current ratio to maintain the price. The caller-supplied ``amount_b``
    is ignored for existing pools. For new/empty pools both amounts must
    be provided.

    Returns an LPResult with the amounts deposited and LP tokens minted.
    """
    token_a, token_b = token_a.upper(), token_b.upper()

    # ── Validation ────────────────────────────────────────────────────────
    if token_a == token_b:
        return LPResult(success=False, error="Cannot add liquidity with the same token on both sides.")

    if amount_a <= 0:
        return LPResult(success=False, error="Amount must be positive.")

    # ── Pool lookup (canonical ordering) ──────────────────────────────────
    pool_id, canon_a, canon_b = db.make_pool_id(token_a, token_b)
    pool = await db.get_pool(pool_id, guild_id)
    if not pool:
        return LPResult(success=False, error=f"No pool exists for {token_a}/{token_b}.")

    reserve_a_raw = int(pool["reserve_a"])
    reserve_b_raw = int(pool["reserve_b"])
    total_lp_raw = int(pool["total_lp"])

    # Map caller tokens to canonical order (raw space)
    if token_a == canon_a:
        res_for_a_raw, res_for_b_raw = reserve_a_raw, reserve_b_raw
    else:
        res_for_a_raw, res_for_b_raw = reserve_b_raw, reserve_a_raw

    # ── Calculate required amount_b ───────────────────────────────────────
    amount_a_raw = to_raw(amount_a)
    if res_for_a_raw > 0 and res_for_b_raw > 0:
        # Existing pool  -  enforce ratio in raw int space (exact).
        # amount_b_raw / amount_a_raw = res_for_b_raw / res_for_a_raw
        amount_b_raw = amount_a_raw * res_for_b_raw // res_for_a_raw
        amount_b = to_human(amount_b_raw)
    else:
        # Empty/new pool  -  both amounts required
        if amount_b <= 0:
            return LPResult(
                success=False,
                error="Both amounts must be provided for an empty pool.",
            )
        amount_b_raw = to_raw(amount_b)

    # ── Check user balances ───────────────────────────────────────────────
    bal_a_raw = await _get_balance_raw(db, user_id, guild_id, token_a)
    if bal_a_raw < amount_a_raw:
        return LPResult(
            success=False,
            error=(
                f"Insufficient {token_a} balance "
                f"(have {to_human(bal_a_raw):,.6f}, need {amount_a:,.6f})."
            ),
        )

    bal_b_raw = await _get_balance_raw(db, user_id, guild_id, token_b)
    if bal_b_raw < amount_b_raw:
        return LPResult(
            success=False,
            error=(
                f"Insufficient {token_b} balance "
                f"(have {to_human(bal_b_raw):,.6f}, need {amount_b:,.6f})."
            ),
        )

    # ── Calculate LP tokens to mint (raw int space) ───────────────────────
    if total_lp_raw > 0:
        if res_for_a_raw <= 0:
            return LPResult(success=False, error="Pool has zero reserves")
        # lp_minted_raw = total_lp_raw * amount_a_raw / res_for_a_raw
        lp_minted_raw = total_lp_raw * amount_a_raw // res_for_a_raw
    else:
        # Initial liquidity  -  geometric mean of the two amounts.
        # sqrt(a_raw * b_raw) stays in the same raw-scale as a_raw and b_raw
        # (because sqrt of SCALE^2 == SCALE).
        lp_minted_raw = _isqrt(amount_a_raw * amount_b_raw)

    if lp_minted_raw <= 0:
        return LPResult(success=False, error="Cannot mint zero LP tokens")

    # ── Execute atomically ──────────────────────────────────────────────
    try:
        async with db.atomic():
            # Debit both tokens from user
            await _debit_raw(db, user_id, guild_id, token_a, amount_a_raw)
            await _debit_raw(db, user_id, guild_id, token_b, amount_b_raw)

            # Update pool reserves (map back to canonical order)
            if token_a == canon_a:
                delta_canon_a, delta_canon_b = amount_a_raw, amount_b_raw
            else:
                delta_canon_a, delta_canon_b = amount_b_raw, amount_a_raw

            new_res_a_raw = reserve_a_raw + delta_canon_a
            new_res_b_raw = reserve_b_raw + delta_canon_b
            new_total_lp_raw = total_lp_raw + lp_minted_raw
            if (
                new_res_a_raw > _MAX_NUMERIC_36
                or new_res_b_raw > _MAX_NUMERIC_36
                or new_total_lp_raw > _MAX_NUMERIC_36
            ):
                raise OverflowError(
                    "Transaction value too large to process safely. "
                    "Try a smaller amount."
                )
            await db.update_pool_reserves(
                pool_id, guild_id,
                new_res_a_raw, new_res_b_raw, new_total_lp_raw,
            )

            # Update user LP position
            await db.update_lp_position(user_id, guild_id, pool_id, lp_minted_raw)

            # Log transaction
            tx_hash = await db.log_tx(
                guild_id, user_id, "ADDLP",
                symbol_in=token_a, amount_in=amount_a_raw,
                symbol_out=token_b, amount_out=amount_b_raw,
            )
    except Exception as e:
        return LPResult(success=False, error=str(e))

    return LPResult(
        success=True,
        tx_hash=tx_hash,
        amount_a=amount_a,
        amount_b=amount_b,
        token_a=token_a,
        token_b=token_b,
        lp_tokens=to_human(lp_minted_raw),
    )


async def remove_liquidity(
    db,
    guild_id: int,
    user_id: int,
    token_a: str,
    token_b: str,
    share_pct: float,
) -> LPResult:
    """Remove liquidity from an AMM pool by percentage of the user's LP position.

    share_pct is a value between 0 and 100 (e.g. 50 = remove half).

    Returns an LPResult with the amounts returned and LP tokens burned.
    """
    token_a, token_b = token_a.upper(), token_b.upper()

    # ── Validation ────────────────────────────────────────────────────────
    if share_pct <= 0 or share_pct > 100:
        return LPResult(success=False, error="Share percentage must be between 0 and 100.")

    # ── Pool lookup ───────────────────────────────────────────────────────
    pool_id, canon_a, canon_b = db.make_pool_id(token_a, token_b)
    pool = await db.get_pool(pool_id, guild_id)
    if not pool:
        return LPResult(success=False, error=f"No pool exists for {token_a}/{token_b}.")

    reserve_a_raw = int(pool["reserve_a"])
    reserve_b_raw = int(pool["reserve_b"])
    total_lp_raw = int(pool["total_lp"])

    if total_lp_raw <= 0:
        return LPResult(success=False, error="Pool has no liquidity to remove.")

    # ── Get user LP position ──────────────────────────────────────────────
    lp_pos = await db.get_user_lp(user_id, guild_id, pool_id)
    user_lp_raw = int(lp_pos["lp_shares"]) if lp_pos else 0

    if user_lp_raw <= 0:
        return LPResult(success=False, error="You have no LP position in this pool.")

    # ── Calculate amounts to return (raw int space) ───────────────────────
    # lp_to_burn_raw = user_lp_raw * share_pct / 100 using integer math.
    # share_pct is already a float percentage, convert the ratio to an
    # int fraction of SCALE so the multiplication stays in int space.
    pct_num = int(share_pct * SCALE)
    pct_den = 100 * SCALE
    lp_to_burn_raw = user_lp_raw * pct_num // pct_den
    if lp_to_burn_raw <= 0:
        return LPResult(success=False, error="Share percentage too small to burn any LP.")

    # Amounts in canonical order
    return_canon_a_raw = reserve_a_raw * lp_to_burn_raw // total_lp_raw
    return_canon_b_raw = reserve_b_raw * lp_to_burn_raw // total_lp_raw

    # Map back to caller's token order
    if token_a == canon_a:
        return_a_raw, return_b_raw = return_canon_a_raw, return_canon_b_raw
    else:
        return_a_raw, return_b_raw = return_canon_b_raw, return_canon_a_raw

    # ── Execute atomically ──────────────────────────────────────────────
    try:
        async with db.atomic():
            # Credit both tokens to user
            await _credit_raw(db, user_id, guild_id, token_a, return_a_raw)
            await _credit_raw(db, user_id, guild_id, token_b, return_b_raw)

            # Reduce pool reserves
            new_res_a_raw = reserve_a_raw - return_canon_a_raw
            new_res_b_raw = reserve_b_raw - return_canon_b_raw
            new_total_lp_raw = total_lp_raw - lp_to_burn_raw
            await db.update_pool_reserves(
                pool_id, guild_id,
                new_res_a_raw, new_res_b_raw, new_total_lp_raw,
            )

            # Reduce user LP position
            await db.update_lp_position(user_id, guild_id, pool_id, -lp_to_burn_raw)

            # Log transaction
            tx_hash = await db.log_tx(
                guild_id, user_id, "REMOVELP",
                symbol_in=token_a, amount_in=return_a_raw,
                symbol_out=token_b, amount_out=return_b_raw,
            )
    except Exception as e:
        return LPResult(success=False, error=str(e))

    return LPResult(
        success=True,
        tx_hash=tx_hash,
        amount_a=to_human(return_a_raw),
        amount_b=to_human(return_b_raw),
        token_a=token_a,
        token_b=token_b,
        lp_tokens=to_human(lp_to_burn_raw),
    )


# =============================================================================
# User-created-token LP exposure
# =============================================================================
# Tools for pricing the slice of a user's LP book that sits in pools with a
# user-created token on at least one side. "User-created" means anything in
# guild_tokens -- that covers mining-group tokens (token_type='group'),
# tokens deployed via the Protocol Dev / Exploiter `can_deploy_token`
# perk, and anything admins added through `.admin token add`. Built-in
# tokens live in Config.TOKENS and are intentionally NOT shadowed by
# guild_tokens rows, so membership in guild_tokens is a clean boundary.
#
# These hooks (work/daily bonus, Liqstone XP multiplier, .mylp badge)
# give user-created tokens gameplay weight without letting whales farm
# nominal LP for free rewards:
#
#   * The hooks price in USD (not share count), so thin / low-price
#     positions are mathematically irrelevant.
#   * The work/daily bonus is capped (Config.USER_LP_WORK_BONUS_CAP).
#   * Liqstone XP is bounded by its existing per-tick cap.
# =============================================================================

async def user_created_token_symbols(db, guild_id: int) -> set[str]:
    """Fetch every user-created token symbol in a guild.

    One SELECT on guild_tokens, which holds only guild-side tokens
    (never shadows Config.TOKENS built-ins). Cheap enough to call per
    hot-path command; downstream callers short-circuit on empty.
    """
    rows = await db.get_guild_tokens(guild_id)
    return {r["symbol"] for r in rows or []}


async def user_created_lp_value_usd(
    db, user_id: int, guild_id: int,
    *,
    symbols: set[str] | None = None,
) -> float:
    """Return the USD value of a user's LP in user-created-token pools.

    A position counts when at least one side of the pool is a user-
    created token (see ``user_created_token_symbols``). Value is the
    user's pro-rata share of pool reserves in USD at live prices --
    same formula as .mylp and services/net_worth.py. Returns 0.0 when
    the guild has no user-created tokens or the user has no matching
    positions.
    """
    if symbols is None:
        symbols = await user_created_token_symbols(db, guild_id)
    if not symbols:
        return 0.0

    positions = await db.get_user_lp_positions(user_id, guild_id)
    if not positions:
        return 0.0

    total_usd = 0.0
    price_cache: dict[str, float] = {"USD": 1.0}
    for lp in positions:
        if int(lp["total_lp"] or 0) <= 0:
            continue
        ta = lp["token_a"]
        tb = lp["token_b"]
        if ta not in symbols and tb not in symbols:
            continue
        frac = float(lp["lp_shares"]) / float(lp["total_lp"])
        val_a = lp.h("reserve_a") * frac
        val_b = lp.h("reserve_b") * frac
        for sym in (ta, tb):
            if sym not in price_cache:
                pr = await db.get_price(sym, guild_id)
                price_cache[sym] = float(pr["price"]) if pr else 0.0
        total_usd += val_a * price_cache[ta] + val_b * price_cache[tb]
    return total_usd


def user_lp_work_bonus_pct(user_lp_usd: float) -> float:
    """Convert a user's user-created-token LP USD exposure into a bonus.

    Linear: +Config.USER_LP_WORK_BONUS_PER_USD per dollar of LP value,
    capped at Config.USER_LP_WORK_BONUS_CAP. The cap makes the bonus
    meaningful-but-bounded so whales can't farm it infinitely, and the
    linear ramp gives smaller LPs partial credit instead of an all-or-
    nothing threshold.
    """
    if user_lp_usd <= 0:
        return 0.0
    raw = float(user_lp_usd) * float(Config.USER_LP_WORK_BONUS_PER_USD)
    return min(float(Config.USER_LP_WORK_BONUS_CAP), raw)
