"""Trading endpoints  -  buy, sell, swap, and transfer operations."""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from api.v2 import idempotency
from api.v2.dependencies import get_current_user, get_db, get_orm_db, require_module
from api.v2.exceptions import (
    InsufficientBalanceError,
    NotFoundError,
    TokenHaltedError,
    ValidationError,
)
from api.v2.utils import to_iso
from core.config import Config
from core.framework.scale import to_human, to_raw
from api.v2.schemas.common import PaginatedResponse
from api.v2.schemas.trading import (
    BuyRequest,
    CefiDefiTransferRequest,
    SellRequest,
    SwapExecuteRequest,
    SwapQuote,
    SwapQuoteRequest,
    SwapResult,
    TradeResult,
    TransferRequest,
)
from api.v2.schemas.user import TransactionItem
from services.transfer import execute_transfer

router = APIRouter(prefix="/trading", tags=["trading"], dependencies=[require_module("crypto")])

# ---------------------------------------------------------------------------
# Decimal rounding helpers
# ---------------------------------------------------------------------------

from constants.trading import (
    USD_PRECISION,
    TOKEN_PRECISION,
    MIN_TRADE_USD as _MIN_TRADE_USD_FLOAT,
)

_MIN_TRADE_USD_DEC = Decimal(str(_MIN_TRADE_USD_FLOAT))


def _snap(value: float, precision: int = TOKEN_PRECISION) -> float:
    """Snap a float to exact DB precision to avoid float→NUMERIC comparison failures.
    Used before any WHERE amount >= $1 check so 20287.57 doesn't become 20287.570000001."""
    return float(Decimal(str(value)).quantize(Decimal(10) ** -precision, rounding=ROUND_UP))


async def _track_supply(db, guild_id: int, symbol: str, delta_raw: int) -> None:
    """Update circulating_supply in both crypto_prices and guild_tokens.

    ``delta_raw`` is a raw scaled integer (``to_raw(human)``) matching the
    NUMERIC(36,0) storage of circulating_supply.
    """
    if delta_raw == 0:
        return
    await db.execute(
        "UPDATE crypto_prices SET circulating_supply = GREATEST(0, circulating_supply + $1) "
        "WHERE guild_id = $2 AND symbol = $3",
        delta_raw, guild_id, symbol,
    )
    await db.execute(
        "UPDATE guild_tokens SET circulating_supply = GREATEST(0, circulating_supply + $1) "
        "WHERE guild_id = $2 AND symbol = $3",
        delta_raw, guild_id, symbol,
    )


def floor_amount(value: float, precision: int = TOKEN_PRECISION) -> float:
    """Truncate -- used for outputs (user receives)."""
    return float(Decimal(str(value)).quantize(Decimal(10) ** -precision, rounding=ROUND_DOWN))


def ceil_amount(value: float, precision: int = TOKEN_PRECISION) -> float:
    """Round up -- used for inputs (user pays)."""
    return float(Decimal(str(value)).quantize(Decimal(10) ** -precision, rounding=ROUND_UP))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

from constants.trading import (
    DEFAULT_FEE_PCT as _DEFAULT_FEE_PCT,
    DEFAULT_FEE_MIN as _DEFAULT_FEE_MIN,
    DEFAULT_FEE_MAX as _DEFAULT_FEE_MAX,
    PRICE_IMPACT_DIVISOR as _PRICE_IMPACT_DIVISOR,
    PRICE_FLOOR as _PRICE_FLOOR,
    DEFAULT_SWAP_FEE as _DEFAULT_SWAP_FEE,
    QUOTE_EXPIRY_SECS as _QUOTE_EXPIRY,
)
from services.trade import check_trade_cooldown, set_trade_cooldown

# In-memory quote store for swap quote binding/expiry
_QUOTE_STORE: dict[str, dict] = {}


def _tx_hash(guild_id: int, user_id: int, action: str) -> str:
    """Generate a unique transaction hash."""
    raw = f"{guild_id}:{user_id}:{action}:{time.time()}:{secrets.token_hex(4)}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def _get_fee_config(db, guild_id: int) -> dict:
    """Load the guild's fee configuration."""
    row = await db.fetchrow(
        """
        SELECT platform_fee_pct, platform_fee_min, platform_fee_max
        FROM guild_settings
        WHERE guild_id = $1
        """,
        guild_id,
    )
    if not row:
        return {
            "pct": _DEFAULT_FEE_PCT,
            "min": _DEFAULT_FEE_MIN,
            "max": _DEFAULT_FEE_MAX,
        }
    return {
        "pct": float(row["platform_fee_pct"]) if row["platform_fee_pct"] is not None else _DEFAULT_FEE_PCT,
        "min": to_human(int(row["platform_fee_min"])) if row["platform_fee_min"] is not None else _DEFAULT_FEE_MIN,
        "max": to_human(int(row["platform_fee_max"])) if row["platform_fee_max"] is not None else _DEFAULT_FEE_MAX,
    }


def _calc_fee(amount_usd: float, fee_cfg: dict) -> float:
    """Calculate the platform fee for a given USD amount."""
    return max(fee_cfg["min"], min(fee_cfg["max"], amount_usd * fee_cfg["pct"]))


async def _check_halted(db, guild_id: int, symbol: str) -> None:
    """Raise TokenHaltedError if the token or its network is disabled."""
    disabled_row = await db.fetchrow(
        "SELECT disabled_tokens FROM guild_settings WHERE guild_id = $1",
        guild_id,
    )
    if disabled_row and disabled_row["disabled_tokens"]:
        disabled = {s.strip().upper() for s in disabled_row["disabled_tokens"].split(",") if s.strip()}
        if symbol.upper() in disabled:
            raise TokenHaltedError(f"{symbol} trading is currently disabled.")


# ---------------------------------------------------------------------------
# POST /trading/buy  -  buy tokens with USD
# ---------------------------------------------------------------------------

@router.post(
    "/buy",
    response_model=TradeResult,
    summary="Buy tokens with USD",
)
async def buy_token(
    body: BuyRequest,
    request: Request,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> TradeResult:
    """Buy tokens using your USD wallet balance.

    Provide either ``amount`` (number of tokens) or ``amount_usd`` (USD to
    spend).  The price impact is calculated and applied atomically.
    """
    # Idempotency check
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        cached = await idempotency.check(idem_key)
        if cached is not None:
            return cached

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    symbol = body.symbol.upper()

    if symbol == "USD":
        raise ValidationError("USD is the base currency and cannot be bought.")
    if symbol not in Config.BUYABLE_WITH_USD:
        raise ValidationError(f"{symbol} cannot be bought directly with USD. Use swap instead.")

    await _check_halted(db, guild_id, symbol)

    # ── Per-symbol anti-sandwich cooldown ────────────────────────────────
    if (cd := check_trade_cooldown(user_id, guild_id, symbol)) > 0:
        from api.v2.exceptions import RateLimitedError
        raise RateLimitedError(f"Trade cooldown: {cd:.0f}s remaining for {symbol}.")

    # --- Determine price (AMM pool preferred, oracle fallback) ---
    # Pool reserves and wallets are raw NUMERIC(36,0) * 10**18; convert to
    # human-scale floats for the AMM math so the same price/impact formulas
    # work for both the AMM and oracle paths.
    a, b = sorted(["USD", symbol])
    pool_id = f"{a}-{b}"
    pool_row = await db.fetchrow(
        "SELECT reserve_a, reserve_b, total_lp FROM pools WHERE pool_id = $1 AND guild_id = $2",
        pool_id, guild_id,
    )
    use_amm = bool(pool_row) and int(pool_row.get("total_lp", 0) or 0) > 0

    reserve_usd = 0.0
    reserve_token = 0.0
    if use_amm:
        ra_h = to_human(int(pool_row["reserve_a"] or 0))
        rb_h = to_human(int(pool_row["reserve_b"] or 0))
        # Pool stores tokens in alphabetical order
        if "USD" == a:
            reserve_usd, reserve_token = ra_h, rb_h
        else:
            reserve_usd, reserve_token = rb_h, ra_h
        if reserve_token > 0 and reserve_usd > 0:
            price = reserve_usd / reserve_token
        else:
            use_amm = False

    if not use_amm:
        # Oracle fallback
        price_row = await db.fetchrow(
            "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
            guild_id, symbol,
        )
        if not price_row:
            raise NotFoundError(f"Token '{symbol}' not found.")
        price = float(price_row["price"])
        if price <= 0:
            raise ValidationError(f"Price for {symbol} is unavailable.")

    # Determine token amount and USD cost
    if body.amount is not None:
        amount = body.amount
        cost_usd = price * amount
    else:
        cost_usd = body.amount_usd  # type: ignore[assignment]
        amount = cost_usd / price

    # Minimum trade size check
    if Decimal(str(cost_usd)) < _MIN_TRADE_USD_DEC:
        raise ValidationError("Trade amount too small (minimum $0.01)")

    # Validate AMM pool can fulfill the order
    if use_amm and amount >= reserve_token:
        raise ValidationError(
            f"Insufficient pool liquidity. Pool has {reserve_token:.8f} {symbol} "
            f"but you requested {amount:.8f}."
        )

    # Fee
    fee_cfg = await _get_fee_config(db, guild_id)
    fee = _calc_fee(cost_usd, fee_cfg)
    total_cost = ceil_amount(cost_usd + fee, USD_PRECISION)

    # Price impact / new price calculation
    if use_amm:
        # AMM: new spot price after reserves shift
        new_reserve_usd = reserve_usd + cost_usd
        new_reserve_token = reserve_token - amount
        new_price = new_reserve_usd / new_reserve_token if new_reserve_token > 0 else price
        eff_amount = amount  # AMM: amount unchanged, pool math handles slippage
    else:
        impact = cost_usd / _PRICE_IMPACT_DIVISOR
        # Apply impact as per-trade slippage; oracle not touched for oracle-priced trades
        eff_price = price * (1 + impact)
        eff_amount = floor_amount(cost_usd / max(1e-15, eff_price))
        new_price = eff_price  # informational; not written to DB for oracle path

    total_cost = _snap(total_cost, USD_PRECISION)

    # Raw scaled amounts for DB writes
    total_cost_raw = to_raw(total_cost)
    cost_usd_raw = to_raw(cost_usd)
    amount_raw = to_raw(floor_amount(amount))
    eff_amount_raw = to_raw(floor_amount(eff_amount))

    # Execute atomically  -  balance check inside transaction to prevent race conditions
    tx_hash = _tx_hash(guild_id, user_id, f"BUY:{symbol}:{amount}")

    async with db.transaction():
        # Atomic check-and-deduct  -  RETURNING ensures we know if it matched
        deducted = await db.fetchrow(
            "UPDATE users SET wallet = wallet - $1 WHERE user_id = $2 AND guild_id = $3 AND wallet >= $1 RETURNING wallet",
            total_cost_raw, user_id, guild_id,
        )
        if deducted is None:
            raise InsufficientBalanceError("Insufficient USD balance.")

        # Credit holding at slippage-adjusted amount
        await db.execute(
            """
            INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, guild_id, symbol)
            DO UPDATE SET amount = crypto_holdings.amount + $4
            """,
            user_id, guild_id, symbol, eff_amount_raw,
        )

        # Update pool reserves if AMM pricing was used
        if use_amm:
            new_reserve_usd_raw = to_raw(new_reserve_usd)
            new_reserve_token_raw = to_raw(new_reserve_token)
            if "USD" == a:
                await db.execute(
                    "UPDATE pools SET reserve_a = $1, reserve_b = $2 WHERE pool_id = $3 AND guild_id = $4",
                    new_reserve_usd_raw, new_reserve_token_raw, pool_id, guild_id,
                )
            else:
                await db.execute(
                    "UPDATE pools SET reserve_a = $1, reserve_b = $2 WHERE pool_id = $3 AND guild_id = $4",
                    new_reserve_token_raw, new_reserve_usd_raw, pool_id, guild_id,
                )
            # AMM: update oracle to reflect the new pool spot price
            await db.execute(
                "UPDATE crypto_prices SET price = $1 WHERE guild_id = $2 AND symbol = $3",
                floor_amount(new_price),
                guild_id,
                symbol,
            )
        # Non-AMM oracle path: do NOT update crypto_prices here; only pool
        # rebalance and market events are allowed to move the oracle price

        # Log transaction (amount_in/amount_out columns are raw scaled ints)
        await db.execute(
            """
            INSERT INTO transactions (tx_hash, guild_id, user_id, tx_type,
                                      symbol_in, amount_in, symbol_out, amount_out, price_at, ts)
            VALUES ($1, $2, $3, 'BUY', 'USD', $4, $5, $6, $7, now())
            """,
            tx_hash,
            guild_id,
            user_id,
            cost_usd_raw,
            symbol,
            eff_amount_raw,
            floor_amount(price),
        )

        # Fetch new balance inside transaction to avoid read-after-write race
        new_row = await db.fetchrow(
            "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
            user_id,
            guild_id,
        )
        new_balance = to_human(int(new_row["wallet"] or 0)) if new_row else 0.0

    set_trade_cooldown(user_id, guild_id, symbol)

    # Track volume in current candle (best-effort  -  no-op if candle row absent)
    _candle_ts = datetime.fromtimestamp(int(time.time()) // 60 * 60, tz=timezone.utc)
    await db.execute(
        "UPDATE price_candles SET volume = volume + $1 WHERE guild_id=$2 AND symbol=$3 AND ts=$4",
        cost_usd, guild_id, symbol, _candle_ts,
    )

    result = TradeResult(
        success=True,
        tx_hash=tx_hash,
        symbol=symbol,
        amount=floor_amount(eff_amount),
        cost=ceil_amount(cost_usd, USD_PRECISION),
        fee=floor_amount(fee),
        new_price=floor_amount(new_price),
        new_balance=new_balance,
    )

    if idem_key:
        await idempotency.store(idem_key, result)
    return result


# ---------------------------------------------------------------------------
# POST /trading/sell  -  sell tokens for USD
# ---------------------------------------------------------------------------

@router.post(
    "/sell",
    response_model=TradeResult,
    summary="Sell tokens for USD",
)
async def sell_token(
    body: SellRequest,
    request: Request,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> TradeResult:
    """Sell tokens from your holdings for USD.

    Provide either ``amount`` (number of tokens) or ``amount_usd`` (USD
    target).  The trade is executed atomically with price impact.
    """
    # Idempotency check
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        cached = await idempotency.check(idem_key)
        if cached is not None:
            return cached

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    symbol = body.symbol.upper()

    await _check_halted(db, guild_id, symbol)

    # ── Per-symbol anti-sandwich cooldown ────────────────────────────────
    if (cd := check_trade_cooldown(user_id, guild_id, symbol)) > 0:
        from api.v2.exceptions import RateLimitedError
        raise RateLimitedError(f"Trade cooldown: {cd:.0f}s remaining for {symbol}.")

    # --- Determine price (AMM pool preferred, oracle fallback) ---
    # Pool reserves are raw NUMERIC(36,0) * 10**18; convert to human-scale for
    # the constant-product math, then convert back at the DB write boundary.
    a, b = sorted(["USD", symbol])
    pool_id = f"{a}-{b}"
    pool_row = await db.fetchrow(
        "SELECT reserve_a, reserve_b, total_lp FROM pools WHERE pool_id = $1 AND guild_id = $2",
        pool_id, guild_id,
    )
    use_amm = bool(pool_row) and int(pool_row.get("total_lp", 0) or 0) > 0

    reserve_usd = 0.0
    reserve_token = 0.0
    if use_amm:
        ra_h = to_human(int(pool_row["reserve_a"] or 0))
        rb_h = to_human(int(pool_row["reserve_b"] or 0))
        # Pool stores tokens in alphabetical order
        if "USD" == a:
            reserve_usd, reserve_token = ra_h, rb_h
        else:
            reserve_usd, reserve_token = rb_h, ra_h
        if reserve_token > 0 and reserve_usd > 0:
            price = reserve_usd / reserve_token
        else:
            use_amm = False

    if not use_amm:
        # Oracle fallback
        price_row = await db.fetchrow(
            "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
            guild_id, symbol,
        )
        if not price_row:
            raise NotFoundError(f"Token '{symbol}' not found.")
        price = float(price_row["price"])
        if price <= 0:
            raise ValidationError(f"Price for {symbol} is unavailable.")

    # Determine amount
    if body.amount is not None:
        amount = body.amount
    else:
        amount = body.amount_usd / price  # type: ignore[operator]

    revenue = price * amount

    # Minimum trade size check
    if Decimal(str(revenue)) < _MIN_TRADE_USD_DEC:
        raise ValidationError("Trade amount too small (minimum $0.01)")

    # Validate AMM pool can fulfill the USD payout
    if use_amm and revenue >= reserve_usd:
        raise ValidationError(
            f"Insufficient pool liquidity. Pool has {reserve_usd:.2f} USD "
            f"but trade requires {revenue:.2f} USD."
        )

    # Fee
    fee_cfg = await _get_fee_config(db, guild_id)
    fee = _calc_fee(revenue, fee_cfg)
    net_revenue = floor_amount(revenue - fee, USD_PRECISION)

    # Price impact / new price calculation
    if use_amm:
        # AMM: new spot price after reserves shift
        new_reserve_usd = reserve_usd - revenue
        new_reserve_token = reserve_token + amount
        new_price = new_reserve_usd / new_reserve_token if new_reserve_token > 0 else price
        eff_revenue = revenue  # AMM: pool math handles slippage, revenue unchanged
    else:
        impact = revenue / _PRICE_IMPACT_DIVISOR
        # Apply impact as per-trade slippage; oracle not touched for oracle-priced trades
        eff_price = max(_PRICE_FLOOR, price * (1 - impact))
        eff_revenue = floor_amount(amount * eff_price, USD_PRECISION)
        new_price = eff_price  # informational; not written to DB for oracle path
    net_revenue = floor_amount(eff_revenue - fee, USD_PRECISION)

    # Raw scaled amounts for DB writes
    amount_raw = to_raw(floor_amount(amount))
    net_revenue_raw = to_raw(net_revenue)
    revenue_raw = to_raw(floor_amount(revenue, USD_PRECISION))
    eff_revenue_raw = to_raw(floor_amount(eff_revenue, USD_PRECISION))

    tx_hash = _tx_hash(guild_id, user_id, f"SELL:{symbol}:{amount}")

    async with db.transaction():
        # Atomic check-and-deduct holdings  -  RETURNING ensures we know if it matched
        deducted = await db.fetchrow(
            "UPDATE crypto_holdings SET amount = amount - $1 WHERE user_id = $2 AND guild_id = $3 AND symbol = $4 AND amount >= $1 RETURNING amount",
            amount_raw, user_id, guild_id, symbol,
        )
        if deducted is None:
            raise InsufficientBalanceError(f"Insufficient {symbol} balance.")
        # Tokens go to pool reserves  -  net supply unchanged

        # Credit wallet at slippage-adjusted net revenue
        await db.execute(
            "UPDATE users SET wallet = wallet + $1 WHERE user_id = $2 AND guild_id = $3",
            net_revenue_raw,
            user_id,
            guild_id,
        )

        # Update pool reserves if AMM pricing was used
        if use_amm:
            new_reserve_usd_raw = to_raw(new_reserve_usd)
            new_reserve_token_raw = to_raw(new_reserve_token)
            if "USD" == a:
                await db.execute(
                    "UPDATE pools SET reserve_a = $1, reserve_b = $2 WHERE pool_id = $3 AND guild_id = $4",
                    new_reserve_usd_raw, new_reserve_token_raw, pool_id, guild_id,
                )
            else:
                await db.execute(
                    "UPDATE pools SET reserve_a = $1, reserve_b = $2 WHERE pool_id = $3 AND guild_id = $4",
                    new_reserve_token_raw, new_reserve_usd_raw, pool_id, guild_id,
                )
            # AMM: update oracle to reflect the new pool spot price
            await db.execute(
                "UPDATE crypto_prices SET price = $1 WHERE guild_id = $2 AND symbol = $3",
                floor_amount(new_price),
                guild_id,
                symbol,
            )
        # Non-AMM oracle path: do NOT update crypto_prices here; only pool
        # rebalance and market events are allowed to move the oracle price

        # Log transaction (amount_in/amount_out are raw scaled ints)
        await db.execute(
            """
            INSERT INTO transactions (tx_hash, guild_id, user_id, tx_type,
                                      symbol_in, amount_in, symbol_out, amount_out, price_at, ts)
            VALUES ($1, $2, $3, 'SELL', $4, $5, 'USD', $6, $7, now())
            """,
            tx_hash,
            guild_id,
            user_id,
            symbol,
            amount_raw,
            eff_revenue_raw,
            floor_amount(price),
        )

        # Fetch new balance inside transaction to avoid read-after-write race
        new_row = await db.fetchrow(
            "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
            user_id,
            guild_id,
        )
        new_balance = to_human(int(new_row["wallet"] or 0)) if new_row else 0.0

    set_trade_cooldown(user_id, guild_id, symbol)

    # Track volume in current candle (best-effort  -  no-op if candle row absent)
    _candle_ts = datetime.fromtimestamp(int(time.time()) // 60 * 60, tz=timezone.utc)
    await db.execute(
        "UPDATE price_candles SET volume = volume + $1 WHERE guild_id=$2 AND symbol=$3 AND ts=$4",
        eff_revenue, guild_id, symbol, _candle_ts,
    )

    result = TradeResult(
        success=True,
        tx_hash=tx_hash,
        symbol=symbol,
        amount=floor_amount(amount),
        cost=floor_amount(eff_revenue, USD_PRECISION),
        fee=floor_amount(fee),
        new_price=floor_amount(new_price),
        new_balance=new_balance,
    )

    if idem_key:
        await idempotency.store(idem_key, result)
    return result


# ---------------------------------------------------------------------------
# POST /trading/swap/quote  -  get swap quote (read-only)
# ---------------------------------------------------------------------------

@router.post(
    "/swap/quote",
    response_model=SwapQuote,
    summary="Get swap quote",
)
async def swap_quote(
    body: SwapQuoteRequest,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> SwapQuote:
    """Compute a price quote for swapping token_in to token_out.

    This is read-only and does not execute the swap.
    """
    guild_id = int(user["guild_id"])
    token_in = body.token_in.upper()
    token_out = body.token_out.upper()
    amount_in = body.amount_in

    if token_in == token_out:
        raise ValidationError("Cannot swap a token for itself.")

    # Find the pool (tokens stored in canonical alphabetical order)
    a, b = sorted([token_in, token_out])
    pool_id = f"{a}-{b}"

    pool_row = await db.fetchrow(
        "SELECT * FROM pools WHERE pool_id = $1 AND guild_id = $2",
        pool_id,
        guild_id,
    )
    if not pool_row:
        raise NotFoundError(f"No liquidity pool for {token_in}/{token_out}.")

    # Determine reserves based on swap direction.
    # Pool reserves are raw NUMERIC(36,0) * 10**18; convert to human-scale for
    # the constant-product math so the formula stays in a single scale.
    if token_in == a:
        reserve_in = to_human(int(pool_row["reserve_a"] or 0))
        reserve_out = to_human(int(pool_row["reserve_b"] or 0))
    else:
        reserve_in = to_human(int(pool_row["reserve_b"] or 0))
        reserve_out = to_human(int(pool_row["reserve_a"] or 0))

    if reserve_in <= 0 or reserve_out <= 0:
        raise ValidationError("Pool has no liquidity.")

    # Constant-product AMM math
    fee = _DEFAULT_SWAP_FEE
    amount_in_after_fee = amount_in * (1 - fee)
    amount_out = reserve_out * amount_in_after_fee / (reserve_in + amount_in_after_fee)
    spot_price = reserve_out / reserve_in
    exec_price = amount_out / amount_in if amount_in > 0 else 0.0
    price_impact = max(0.0, (spot_price - exec_price) / spot_price) if spot_price > 0 else 0.0

    # Generate bound quote with expiry
    quote_id = secrets.token_urlsafe(16)
    expires_at = time.time() + _QUOTE_EXPIRY
    state_hash = hashlib.sha256(f"{reserve_in}:{reserve_out}".encode()).hexdigest()[:16]

    _QUOTE_STORE[quote_id] = {
        "expires_at": expires_at,
        "state_hash": state_hash,
        "amount_out": floor_amount(amount_out),
    }

    return SwapQuote(
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        amount_out=floor_amount(amount_out),
        price_impact_pct=round(price_impact * 100, 4),
        fee=floor_amount(amount_in * fee),
        route=f"{token_in} -> {token_out} via {pool_id}",
        quote_id=quote_id,
        expires_at=expires_at,
        pool_state_hash=state_hash,
    )


# ---------------------------------------------------------------------------
# POST /trading/swap/execute  -  execute swap
# ---------------------------------------------------------------------------

@router.post(
    "/swap/execute",
    response_model=SwapResult,
    summary="Execute a token swap",
)
async def swap_execute(
    body: SwapExecuteRequest,
    request: Request,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> SwapResult:
    """Execute a swap of token_in for token_out through the AMM pool.

    The swap is atomic  -  if any step fails, everything rolls back.
    """
    # Idempotency check
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        cached = await idempotency.check(idem_key)
        if cached is not None:
            return cached

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    token_in = body.token_in.upper()
    token_out = body.token_out.upper()
    amount_in = body.amount_in

    if token_in == token_out:
        raise ValidationError("Cannot swap a token for itself.")

    await _check_halted(db, guild_id, token_in)
    await _check_halted(db, guild_id, token_out)

    # Pool lookup
    a, b = sorted([token_in, token_out])
    pool_id = f"{a}-{b}"

    pool_row = await db.fetchrow(
        "SELECT * FROM pools WHERE pool_id = $1 AND guild_id = $2",
        pool_id,
        guild_id,
    )
    if not pool_row:
        raise NotFoundError(f"No liquidity pool for {token_in}/{token_out}.")

    # Pool reserves are raw NUMERIC(36,0) * 10**18; convert to human-scale for
    # the constant-product math and then convert back at the DB write boundary.
    if token_in == a:
        reserve_in = to_human(int(pool_row["reserve_a"] or 0))
        reserve_out = to_human(int(pool_row["reserve_b"] or 0))
    else:
        reserve_in = to_human(int(pool_row["reserve_b"] or 0))
        reserve_out = to_human(int(pool_row["reserve_a"] or 0))

    if reserve_in <= 0 or reserve_out <= 0:
        raise ValidationError("Pool has no liquidity.")

    # Quote binding validation
    if body.quote_id:
        quote = _QUOTE_STORE.pop(body.quote_id, None)
        if quote is None:
            raise ValidationError("Invalid or already-used quote ID")
        _ea = quote["expires_at"]
        _ea_ts = _ea.timestamp() if hasattr(_ea, 'timestamp') else _ea
        if time.time() > _ea_ts:
            raise ValidationError("Quote expired (5s limit)")
        current_hash = hashlib.sha256(f"{reserve_in}:{reserve_out}".encode()).hexdigest()[:16]
        if current_hash != quote["state_hash"]:
            raise ValidationError("Pool state changed since quote  -  request a new quote")

    # AMM math
    fee = _DEFAULT_SWAP_FEE
    amount_in_after_fee = amount_in * (1 - fee)
    amount_out = reserve_out * amount_in_after_fee / (reserve_in + amount_in_after_fee)
    spot_price = reserve_out / reserve_in
    exec_price = amount_out / amount_in if amount_in > 0 else 0.0
    price_impact = max(0.0, (spot_price - exec_price) / spot_price) if spot_price > 0 else 0.0

    # Minimum trade size check (estimate USD value)
    cost_usd_estimate = amount_in if token_in == "USD" else amount_in * spot_price
    if Decimal(str(cost_usd_estimate)) < _MIN_TRADE_USD_DEC:
        raise ValidationError("Trade amount too small (minimum $0.01)")

    # Slippage check
    if body.min_amount_out > 0 and amount_out < body.min_amount_out:
        raise ValidationError(
            f"Slippage protection: output {amount_out:.6f} {token_out} "
            f"is below minimum {body.min_amount_out:.6f}."
        )

    slippage_limit = body.slippage_pct / 100
    expected_out = amount_in * spot_price
    if expected_out > 0 and (expected_out - amount_out) / expected_out > slippage_limit:
        raise ValidationError(f"Slippage of {price_impact*100:.2f}% exceeds tolerance of {body.slippage_pct}%.")

    tx_hash = _tx_hash(guild_id, user_id, f"SWAP:{token_in}:{token_out}:{amount_in}")

    # New reserves (human-scale for the invariant check, raw for the DB write)
    new_reserve_in = reserve_in + amount_in
    new_reserve_out = reserve_out - amount_out

    # Constant-product invariant check
    k_before = reserve_in * reserve_out
    k_after = new_reserve_in * new_reserve_out
    if k_after < k_before * 0.9999:  # Tiny tolerance for float rounding
        raise ValidationError("Invariant violation: pool k decreased")

    # Raw scaled amounts for DB writes
    amount_in_raw = to_raw(amount_in)
    amount_out_raw = to_raw(floor_amount(amount_out))
    new_reserve_in_raw = to_raw(new_reserve_in)
    new_reserve_out_raw = to_raw(new_reserve_out)

    async with db.transaction():
        # Atomic check-and-deduct  -  RETURNING ensures we know if it matched
        if token_in == "USD":
            deducted = await db.fetchrow(
                "UPDATE users SET wallet = wallet - $1 WHERE user_id = $2 AND guild_id = $3 AND wallet >= $1 RETURNING wallet",
                amount_in_raw, user_id, guild_id,
            )
            if deducted is None:
                raise InsufficientBalanceError("Insufficient USD balance.")
        else:
            deducted = await db.fetchrow(
                "UPDATE crypto_holdings SET amount = amount - $1 WHERE user_id = $2 AND guild_id = $3 AND symbol = $4 AND amount >= $1 RETURNING amount",
                amount_in_raw, user_id, guild_id, token_in,
            )
            if deducted is None:
                raise InsufficientBalanceError(f"Insufficient {token_in} balance.")

        # Credit output
        if token_out == "USD":
            await db.execute(
                "UPDATE users SET wallet = wallet + $1 WHERE user_id = $2 AND guild_id = $3",
                amount_out_raw,
                user_id,
                guild_id,
            )
        else:
            await db.execute(
                """
                INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, guild_id, symbol)
                DO UPDATE SET amount = crypto_holdings.amount + $4
                """,
                user_id,
                guild_id,
                token_out,
                amount_out_raw,
            )

        # Update pool reserves
        if token_in == a:
            await db.execute(
                "UPDATE pools SET reserve_a = $1, reserve_b = $2 WHERE pool_id = $3 AND guild_id = $4",
                new_reserve_in_raw,
                new_reserve_out_raw,
                pool_id,
                guild_id,
            )
        else:
            await db.execute(
                "UPDATE pools SET reserve_a = $1, reserve_b = $2 WHERE pool_id = $3 AND guild_id = $4",
                new_reserve_out_raw,
                new_reserve_in_raw,
                pool_id,
                guild_id,
            )

        # Log transaction (amount_in/amount_out are raw scaled ints)
        await db.execute(
            """
            INSERT INTO transactions (tx_hash, guild_id, user_id, tx_type,
                                      symbol_in, amount_in, symbol_out, amount_out, price_at, ts)
            VALUES ($1, $2, $3, 'SWAP', $4, $5, $6, $7, $8, now())
            """,
            tx_hash,
            guild_id,
            user_id,
            token_in,
            amount_in_raw,
            token_out,
            amount_out_raw,
            floor_amount(exec_price),
        )

    # Track volume for both sides of the swap (best-effort  -  no-op if candle absent)
    _candle_ts = datetime.fromtimestamp(int(time.time()) // 60 * 60, tz=timezone.utc)
    # Compute USD value: if one side is USD use that amount directly, else look up price
    if token_in == "USD":
        _vol_usd = amount_in
    elif token_out == "USD":
        _vol_usd = float(amount_out)
    else:
        _price_row = await db.fetchrow(
            "SELECT price FROM crypto_prices WHERE guild_id=$1 AND symbol=$2", guild_id, token_in,
        )
        _vol_usd = amount_in * float(_price_row["price"]) if _price_row else 0.0
    if _vol_usd > 0:
        await db.execute(
            "UPDATE price_candles SET volume = volume + $1 WHERE guild_id=$2 AND symbol=$3 AND ts=$4",
            _vol_usd, guild_id, token_in, _candle_ts,
        )
        await db.execute(
            "UPDATE price_candles SET volume = volume + $1 WHERE guild_id=$2 AND symbol=$3 AND ts=$4",
            _vol_usd, guild_id, token_out, _candle_ts,
        )

    result = SwapResult(
        success=True,
        tx_hash=tx_hash,
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        amount_out=floor_amount(amount_out),
        price_impact_pct=round(price_impact * 100, 4),
        fee=floor_amount(amount_in * fee),
    )

    if idem_key:
        await idempotency.store(idem_key, result)
    return result


# ---------------------------------------------------------------------------
# POST /trading/transfer  -  send USD to user
# ---------------------------------------------------------------------------

@router.post(
    "/transfer",
    response_model=TradeResult,
    summary="Transfer USD to another user",
)
async def transfer_usd(
    body: TransferRequest,
    request: Request,
    orm_db=Depends(get_orm_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> TradeResult:
    """Transfer USD from your wallet to another user's wallet.

    Delegates to :func:`services.transfer.execute_transfer`, which handles
    raw-scaled-int conversion, the atomic debit/credit pair, and ledger
    logging via the guarded ``transfer_wallet`` DB call. The raw SQL path
    that lived here previously wrote human-scale floats directly into the
    raw ``NUMERIC(36,0)`` wallet column.
    """
    # Idempotency check
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        cached = await idempotency.check(idem_key)
        if cached is not None:
            return cached

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    to_user_id = body.to_user_id
    amount = body.amount

    result = await execute_transfer(orm_db, guild_id, user_id, to_user_id, amount)
    if not result.success:
        err = result.error.lower()
        if "insufficient" in err:
            raise InsufficientBalanceError(result.error)
        if "not found" in err or "recipient" in err:
            raise NotFoundError(result.error)
        raise ValidationError(result.error)

    trade_result = TradeResult(
        success=True,
        tx_hash=result.tx_hash,
        symbol="USD",
        amount=floor_amount(result.amount, USD_PRECISION),
        cost=floor_amount(result.amount, USD_PRECISION),
        fee=0.0,
        new_price=1.0,
        new_balance=result.new_balance,
    )

    if idem_key:
        await idempotency.store(idem_key, trade_result)
    return trade_result


# ---------------------------------------------------------------------------
# GET /trading/history  -  paginated trade history with filters
# ---------------------------------------------------------------------------

@router.get(
    "/history",
    response_model=PaginatedResponse,
    summary="Get trade history",
)
async def trade_history(
    tx_type: str | None = Query(None, description="Filter by tx type (BUY, SELL, SWAP, TRANSFER)."),
    symbol: str | None = Query(None, description="Filter by token symbol."),
    limit: int = Query(50, ge=1, le=200, description="Page size."),
    offset: int = Query(0, ge=0, description="Offset for pagination."),
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> PaginatedResponse:
    """Return paginated trade history for the authenticated user.

    Supports filtering by transaction type and symbol.
    """
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    # Build dynamic WHERE clause
    conditions = ["guild_id = $1", "user_id = $2"]
    params: list[Any] = [guild_id, user_id]
    idx = 3

    if tx_type:
        conditions.append(f"tx_type = ${idx}")
        params.append(tx_type.upper())
        idx += 1

    if symbol:
        sym = symbol.upper()
        conditions.append(f"(symbol_in = ${idx} OR symbol_out = ${idx})")
        params.append(sym)
        idx += 1

    where = " AND ".join(conditions)

    # Count
    count_row = await db.fetchrow(
        f"SELECT COUNT(*) AS cnt FROM transactions WHERE {where}",
        *params,
    )
    total = int(count_row["cnt"]) if count_row else 0

    # Fetch page
    params.extend([limit, offset])
    rows = await db.fetch(
        f"""
        SELECT tx_hash, tx_type, symbol_in, amount_in, symbol_out, amount_out,
               gas_fee, ts
        FROM transactions
        WHERE {where}
        ORDER BY ts DESC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params,
    )

    # transactions.amount_in / amount_out / gas_fee are raw NUMERIC(36,0) * 10**18;
    # unscale for the API response.
    items = [
        TransactionItem(
            tx_hash=r["tx_hash"],
            tx_type=r["tx_type"],
            symbol_in=r["symbol_in"],
            amount_in=to_human(int(r["amount_in"])) if r["amount_in"] is not None else None,
            symbol_out=r["symbol_out"],
            amount_out=to_human(int(r["amount_out"])) if r["amount_out"] is not None else None,
            fee=0.0,
            gas_fee=to_human(int(r["gas_fee"])) if r["gas_fee"] is not None else 0.0,
            ts=to_iso(r["ts"]),
        ).model_dump()
        for r in rows
    ]

    return PaginatedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /trading/recent-trades  -  simplified recent trades (for Bank page)
# ---------------------------------------------------------------------------

@router.get(
    "/recent-trades",
    summary="Get recent trades (simplified)",
)
async def recent_trades(
    limit: int = Query(15, ge=1, le=50, description="Max trades to return."),
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return the most recent trades for the authenticated user in a simplified format."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await db.fetch(
        """
        SELECT tx_hash, tx_type, symbol_in, amount_in, symbol_out, amount_out, price_at, ts
        FROM transactions
        WHERE guild_id = $1 AND user_id = $2
        ORDER BY ts DESC
        LIMIT $3
        """,
        guild_id, user_id, limit,
    )

    # transactions.amount_in / amount_out are raw NUMERIC(36,0) * 10**18; unscale
    # for the simplified response. price_at stays a float (prices are floats).
    results = []
    for r in rows:
        tx_type = r["tx_type"]
        amt_in_h = to_human(int(r["amount_in"])) if r["amount_in"] else 0.0
        amt_out_h = to_human(int(r["amount_out"])) if r["amount_out"] else 0.0
        # Determine the primary symbol and amounts for display
        if tx_type == "BUY":
            symbol = r["symbol_out"]
            amount = amt_out_h
            price = float(r["price_at"]) if r["price_at"] else 0.0
            total = amt_in_h
        elif tx_type == "SELL":
            symbol = r["symbol_in"]
            amount = amt_in_h
            price = float(r["price_at"]) if r["price_at"] else 0.0
            total = amt_out_h
        else:
            symbol = r["symbol_in"] or r["symbol_out"] or ""
            amount = amt_in_h
            price = float(r["price_at"]) if r["price_at"] else 0.0
            total = amt_out_h

        results.append({
            "id": r["tx_hash"],
            "type": tx_type.lower(),
            "symbol": symbol,
            "amount": round(amount, 8),
            "price": round(price, 8),
            "total": round(total, 8),
            "timestamp": to_iso(r["ts"]),
        })

    return results


# ---------------------------------------------------------------------------
# POST /trading/cefi-to-defi  -  move tokens from CeFi to DeFi wallet
# ---------------------------------------------------------------------------

@router.post(
    "/cefi-to-defi",
    summary="Transfer tokens from CeFi to DeFi wallet",
)
async def cefi_to_defi(
    body: CefiDefiTransferRequest,
    request: Request,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Move tokens from CeFi holdings (crypto_holdings) to a DeFi wallet (wallet_holdings).

    Requires the user to have a DeFi wallet on the target network.
    """
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        cached = await idempotency.check(idem_key)
        if cached is not None:
            return cached

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    symbol = body.symbol.upper()
    amount = body.amount
    network = body.network
    # Raw scaled integer for every DB write  -  holdings are NUMERIC(36,0) * 10**18.
    amount_raw = to_raw(amount)

    # 1. Verify the user has a wallet on the target network
    wallet_row = await db.fetchrow(
        "SELECT 1 FROM defi_wallets WHERE user_id = $1 AND guild_id = $2 AND network = $3",
        user_id, guild_id, network,
    )
    if not wallet_row:
        raise NotFoundError(f"No DeFi wallet found on network '{network}'. Create one first.")

    # 2. Check sufficient CeFi balance (raw-to-raw comparison)
    holding_row = await db.fetchrow(
        "SELECT amount FROM crypto_holdings WHERE user_id = $1 AND guild_id = $2 AND symbol = $3",
        user_id, guild_id, symbol,
    )
    if not holding_row or int(holding_row["amount"] or 0) < amount_raw:
        raise InsufficientBalanceError(f"Insufficient CeFi {symbol} balance.")

    tx_hash = _tx_hash(guild_id, user_id, f"CEFI_TO_DEFI:{symbol}:{amount}")

    # 3. Atomically debit CeFi, credit DeFi
    async with db.transaction():
        deducted = await db.fetchrow(
            "UPDATE crypto_holdings SET amount = amount - $1 WHERE user_id = $2 AND guild_id = $3 AND symbol = $4 AND amount >= $1 RETURNING amount",
            amount_raw, user_id, guild_id, symbol,
        )
        if deducted is None:
            raise InsufficientBalanceError(f"Insufficient CeFi {symbol} balance.")

        await db.execute(
            """INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id, guild_id, network, symbol)
               DO UPDATE SET amount = wallet_holdings.amount + $5""",
            user_id, guild_id, network, symbol, amount_raw,
        )

        # 4. Log the transaction (amount_in/amount_out are raw scaled ints)
        await db.execute(
            """INSERT INTO transactions (tx_hash, guild_id, user_id, tx_type,
                                          symbol_in, amount_in, symbol_out, amount_out, price_at, ts)
               VALUES ($1, $2, $3, 'CEFI_TO_DEFI', $4, $5, $4, $5, 1.0, now())""",
            tx_hash, guild_id, user_id, symbol, amount_raw,
        )

        # 5. Fetch new balances inside transaction to avoid read-after-write race
        new_cefi = await db.fetchrow(
            "SELECT amount FROM crypto_holdings WHERE user_id = $1 AND guild_id = $2 AND symbol = $3",
            user_id, guild_id, symbol,
        )
        new_defi = await db.fetchrow(
            "SELECT amount FROM wallet_holdings WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND symbol = $4",
            user_id, guild_id, network, symbol,
        )

    result = {
        "success": True,
        "tx_hash": tx_hash,
        "symbol": symbol,
        "amount": amount,
        "network": network,
        "cefi_balance": to_human(int(new_cefi["amount"] or 0)) if new_cefi else 0.0,
        "defi_balance": to_human(int(new_defi["amount"] or 0)) if new_defi else 0.0,
    }

    if idem_key:
        await idempotency.store(idem_key, result)
    return result


# ---------------------------------------------------------------------------
# POST /trading/defi-to-cefi  -  move tokens from DeFi wallet to CeFi
# ---------------------------------------------------------------------------

@router.post(
    "/defi-to-cefi",
    summary="Transfer tokens from DeFi wallet to CeFi",
)
async def defi_to_cefi(
    body: CefiDefiTransferRequest,
    request: Request,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Move tokens from a DeFi wallet (wallet_holdings) back to CeFi holdings (crypto_holdings).

    Requires the user to have a DeFi wallet on the source network.
    """
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        cached = await idempotency.check(idem_key)
        if cached is not None:
            return cached

    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    symbol = body.symbol.upper()
    amount = body.amount
    network = body.network
    # Raw scaled integer for every DB write  -  holdings are NUMERIC(36,0) * 10**18.
    amount_raw = to_raw(amount)

    # 1. Verify the user has a wallet on the source network
    wallet_row = await db.fetchrow(
        "SELECT 1 FROM defi_wallets WHERE user_id = $1 AND guild_id = $2 AND network = $3",
        user_id, guild_id, network,
    )
    if not wallet_row:
        raise NotFoundError(f"No DeFi wallet found on network '{network}'.")

    # 2. Check sufficient DeFi balance (raw-to-raw comparison)
    holding_row = await db.fetchrow(
        "SELECT amount FROM wallet_holdings WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND symbol = $4",
        user_id, guild_id, network, symbol,
    )
    if not holding_row or int(holding_row["amount"] or 0) < amount_raw:
        raise InsufficientBalanceError(f"Insufficient DeFi {symbol} balance on {network}.")

    tx_hash = _tx_hash(guild_id, user_id, f"DEFI_TO_CEFI:{symbol}:{amount}")

    # 3. Atomically debit DeFi, credit CeFi
    async with db.transaction():
        deducted = await db.fetchrow(
            "UPDATE wallet_holdings SET amount = amount - $1 WHERE user_id = $2 AND guild_id = $3 AND network = $4 AND symbol = $5 AND amount >= $1 RETURNING amount",
            amount_raw, user_id, guild_id, network, symbol,
        )
        if deducted is None:
            raise InsufficientBalanceError(f"Insufficient DeFi {symbol} balance on {network}.")

        await db.execute(
            """INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, guild_id, symbol)
               DO UPDATE SET amount = crypto_holdings.amount + $4""",
            user_id, guild_id, symbol, amount_raw,
        )

        # 4. Log the transaction (amount_in/amount_out are raw scaled ints)
        await db.execute(
            """INSERT INTO transactions (tx_hash, guild_id, user_id, tx_type,
                                          symbol_in, amount_in, symbol_out, amount_out, price_at, ts)
               VALUES ($1, $2, $3, 'DEFI_TO_CEFI', $4, $5, $4, $5, 1.0, now())""",
            tx_hash, guild_id, user_id, symbol, amount_raw,
        )

        # 5. Fetch new balances inside transaction to avoid read-after-write race
        new_cefi = await db.fetchrow(
            "SELECT amount FROM crypto_holdings WHERE user_id = $1 AND guild_id = $2 AND symbol = $3",
            user_id, guild_id, symbol,
        )
        new_defi = await db.fetchrow(
            "SELECT amount FROM wallet_holdings WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND symbol = $4",
            user_id, guild_id, network, symbol,
        )

    result = {
        "success": True,
        "tx_hash": tx_hash,
        "symbol": symbol,
        "amount": amount,
        "network": network,
        "cefi_balance": to_human(int(new_cefi["amount"] or 0)) if new_cefi else 0.0,
        "defi_balance": to_human(int(new_defi["amount"] or 0)) if new_defi else 0.0,
    }

    if idem_key:
        await idempotency.store(idem_key, result)
    return result


# ---------------------------------------------------------------------------
# POST /trading/bank-deposit  -  move USD from wallet to bank
# ---------------------------------------------------------------------------

@router.post(
    "/bank-deposit",
    summary="Deposit USD from wallet to bank",
)
async def bank_deposit(
    body: dict,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Move USD from the user's wallet to their bank account."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    amount = float(body.get("amount", 0))

    if amount <= 0:
        raise ValidationError("Amount must be positive.")

    # Wallet and bank columns are raw NUMERIC(36,0) * 10**18.
    amount_raw = to_raw(amount)

    async with db.transaction():
        row = await db.fetchrow(
            "SELECT wallet, bank FROM users WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )
        if not row or int(row["wallet"] or 0) < amount_raw:
            raise InsufficientBalanceError("Insufficient wallet balance.")

        await db.execute(
            "UPDATE users SET wallet = wallet - $1, bank = bank + $1 WHERE user_id = $2 AND guild_id = $3",
            amount_raw, user_id, guild_id,
        )

        # Fetch updated balances inside transaction to avoid read-after-write race
        updated = await db.fetchrow(
            "SELECT wallet, bank FROM users WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )

    return {
        "success": True,
        "message": f"Deposited ${amount:,.2f} to bank.",
        "wallet": to_human(int(updated["wallet"] or 0)) if updated else 0.0,
        "bank": to_human(int(updated["bank"] or 0)) if updated else 0.0,
    }


# ---------------------------------------------------------------------------
# POST /trading/bank-withdraw  -  move USD from bank to wallet
# ---------------------------------------------------------------------------

@router.post(
    "/bank-withdraw",
    summary="Withdraw USD from bank to wallet",
)
async def bank_withdraw(
    body: dict,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Move USD from the user's bank account to their wallet."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])
    amount = float(body.get("amount", 0))

    if amount <= 0:
        raise ValidationError("Amount must be positive.")

    # Wallet and bank columns are raw NUMERIC(36,0) * 10**18.
    amount_raw = to_raw(amount)

    async with db.transaction():
        row = await db.fetchrow(
            "SELECT wallet, bank FROM users WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )
        if not row or int(row["bank"] or 0) < amount_raw:
            raise InsufficientBalanceError("Insufficient bank balance.")

        await db.execute(
            "UPDATE users SET wallet = wallet + $1, bank = bank - $1 WHERE user_id = $2 AND guild_id = $3",
            amount_raw, user_id, guild_id,
        )

        # Fetch updated balances inside transaction to avoid read-after-write race
        updated = await db.fetchrow(
            "SELECT wallet, bank FROM users WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )

    return {
        "success": True,
        "message": f"Withdrew ${amount:,.2f} from bank.",
        "wallet": to_human(int(updated["wallet"] or 0)) if updated else 0.0,
        "bank": to_human(int(updated["bank"] or 0)) if updated else 0.0,
    }
