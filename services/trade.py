"""Trade service layer  -  buy/sell token operations with USD.

Encapsulates all buy/sell logic: validation, fee calculation, price impact,
and execution. No Discord dependencies.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from core.config import Config
from core.framework.scale import to_human, to_raw
from services.swap import (
    apply_trade_oracle_impact,
    cancel_user_swap_reservation,
    reserve_user_swap_volume,
)


def _snap(value: float, decimals: int = 8) -> float:
    """Round a float to *decimals* places using Decimal to avoid IEEE 754 drift."""
    return float(Decimal(str(value)).quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP))

# ── Constants ────────────────────────────────────────────────────────────────
# Canonical network short-code map lives in :mod:`core.framework.network`.
from core.framework.network import FULL_TO_SHORT as _NET_MAP


# ── Per-(user, guild, symbol) anti-sandwich cooldown ────────────────────────
_trade_cd: dict[tuple[int, int, str], float] = {}


def check_trade_cooldown(user_id: int, guild_id: int, symbol: str) -> float:
    """Return seconds remaining on trade cooldown, or 0.0 if clear."""
    return max(0.0, _trade_cd.get((user_id, guild_id, symbol), 0.0) - _time.monotonic())


def set_trade_cooldown(user_id: int, guild_id: int, symbol: str, seconds: float = 30.0) -> None:
    """Stamp a per-(user, guild, symbol) trade cooldown."""
    _trade_cd[(user_id, guild_id, symbol)] = _time.monotonic() + seconds


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class TradeResult:
    success: bool
    tx_hash: str = ""
    amount: float = 0.0
    cost: float = 0.0
    fee: float = 0.0
    new_price: float = 0.0
    new_balance: float = 0.0
    error: str = ""


# ── Core functions ───────────────────────────────────────────────────────────

async def execute_buy(
    db,
    guild_id: int,
    user_id: int,
    symbol: str,
    amount: float,
) -> TradeResult:
    """Buy *amount* tokens of *symbol* using USD.

    Parameters
    ----------
    db:       Database handle (has all query methods).
    guild_id: Discord guild id.
    user_id:  Discord user id.
    symbol:   Token symbol to buy (e.g. "ARC").
    amount:   Quantity of tokens to buy (NOT USD amount).

    Returns
    -------
    TradeResult with success=True on success, or error message on failure.
    """
    symbol = symbol.upper()

    if amount <= 0:
        return TradeResult(success=False, error="Amount must be a positive number.")

    # USD is the base currency -- not buyable
    if symbol == "USD":
        return TradeResult(
            success=False,
            error="USD is the base currency. Use .buy USDC or .buy DSD for network stablecoins.",
        )

    # ── Validate token existence ─────────────────────────────────────────
    all_tokens = await db.get_all_tokens_for_guild(guild_id)
    if symbol not in all_tokens:
        return TradeResult(success=False, error=f"Unknown token: {symbol}")

    # ── Admin halts ──────────────────────────────────────────────────────
    if await db.is_token_disabled(guild_id, symbol):
        return TradeResult(success=False, error=f"{symbol} trading is currently disabled by an admin.")

    tok_net = all_tokens.get(symbol, {}).get("network", "")
    net_key = _NET_MAP.get(tok_net, "")
    if net_key and await db.is_network_halted(guild_id, net_key):
        return TradeResult(success=False, error=f"The {tok_net} is currently halted by an admin.")

    # ── Buyable restriction ──────────────────────────────────────────────
    if symbol not in Config.BUYABLE_WITH_USD:
        return TradeResult(
            success=False,
            error=(
                f"{symbol} cannot be purchased directly with USD. "
                f"Direct .buy is available for: {', '.join(sorted(Config.BUYABLE_WITH_USD))}."
            ),
        )

    # ── Price lookup ─────────────────────────────────────────────────────
    price_row = await db.get_price(symbol, guild_id)
    if not price_row:
        return TradeResult(success=False, error="Price data unavailable.")
    price = float(price_row["price"])

    # ── Cost calculation ─────────────────────────────────────────────────
    cost_usd = _snap(price * amount)

    # ── Fee calculation ──────────────────────────────────────────────────
    fee_cfg = await db.guilds.get_fee_config(guild_id)
    fee = _snap(max(
        fee_cfg["platform_fee_min"],
        min(fee_cfg["platform_fee_max"], cost_usd * fee_cfg["platform_fee_pct"]),
    ))

    # ── Balance check ────────────────────────────────────────────────────
    user = await db.get_user(user_id, guild_id)
    wallet = to_human(int(user["wallet"])) if user else 0.0
    if wallet < cost_usd + fee:
        return TradeResult(
            success=False,
            error=(
                f"Insufficient balance. Need ${cost_usd + fee:,.2f} "
                f"(${cost_usd:,.2f} + ${fee:,.2f} fee) but you only have ${wallet:,.2f}."
            ),
        )

    # ── Per-symbol trade cooldown (anti-sandwich) ────────────────────────
    if (cd := check_trade_cooldown(user_id, guild_id, symbol)) > 0:
        return TradeResult(success=False, error=f"Trade cooldown: {cd:.0f}s remaining for {symbol}.")

    # ── Price impact: applied as per-trade slippage, oracle not touched ──
    impact = cost_usd / Config.PRICE_IMPACT_DIVISOR
    if impact >= 0.50:
        return TradeResult(success=False, error="Trade too large  -  price impact exceeds 50%. Try a smaller amount.")
    # Effective fill price accounts for slippage; user gets fewer tokens
    eff_price = price * (1 + impact)
    eff_amount = _snap(cost_usd / max(1e-15, eff_price))

    # ── Volume limit (reserve atomically right before execution) ─────────
    allowed, remaining, reservation_id = await reserve_user_swap_volume(user_id, guild_id, cost_usd)
    if not allowed:
        return TradeResult(
            success=False,
            error=(
                f"Hourly volume limit reached. Remaining: ${remaining:,.2f}. "
                f"Limit: ${Config.USER_SWAP_HOURLY_LIMIT_USD:,.0f}/hour."
            ),
        )

    # ── Execute atomically ────────────────────────────────────────────────
    try:
        async with db.atomic():
            # Debit USD wallet (cost + fee)
            await db.update_wallet(user_id, guild_id, -to_raw(cost_usd + fee))

            # Credit token holding at effective (slippage-adjusted) amount
            new_holding = await db.update_holding(user_id, guild_id, symbol, to_raw(eff_amount))

            # Split fee to community reserves
            await db.split_to_community_reserves(guild_id, "USD", to_raw(fee))

            # Log transaction
            tx_hash = await db.log_tx(
                guild_id, user_id, "BUY",
                symbol_in="USD", amount_in=to_raw(cost_usd),
                symbol_out=symbol, amount_out=to_raw(eff_amount),
                price_at=price,
                network="usd",
            )
    except Exception as e:
        if reservation_id is not None:
            cancel_user_swap_reservation(user_id, guild_id, reservation_id)
        return TradeResult(success=False, error=str(e))

    # Push the trade into the oracle + candle so the chart reflects buys
    # within the same minute. Outside the atomic block: a chart write
    # failure must never roll back the trade itself.
    await apply_trade_oracle_impact(
        db, guild_id, symbol, usd_value=cost_usd, direction=+1,
    )

    set_trade_cooldown(user_id, guild_id, symbol)

    # ── Fetch updated wallet balance ─────────────────────────────────────
    updated_user = await db.get_user(user_id, guild_id)
    new_wallet = to_human(int(updated_user["wallet"])) if updated_user else 0.0

    return TradeResult(
        success=True,
        tx_hash=tx_hash,
        amount=eff_amount,
        cost=cost_usd,
        fee=fee,
        new_price=eff_price,
        new_balance=new_wallet,
    )


async def execute_sell(
    db,
    guild_id: int,
    user_id: int,
    symbol: str,
    amount: float,
) -> TradeResult:
    """Sell *amount* tokens of *symbol* for USD.

    Parameters
    ----------
    db:       Database handle (has all query methods).
    guild_id: Discord guild id.
    user_id:  Discord user id.
    symbol:   Token symbol to sell (e.g. "ARC").
    amount:   Quantity of tokens to sell.

    Returns
    -------
    TradeResult with success=True on success, or error message on failure.
    """
    symbol = symbol.upper()

    if amount <= 0:
        return TradeResult(success=False, error="Amount must be a positive number.")

    # ── Validate token existence ─────────────────────────────────────────
    all_tokens = await db.get_all_tokens_for_guild(guild_id)
    if symbol not in all_tokens:
        return TradeResult(success=False, error=f"Unknown token: {symbol}")

    # ── Admin halts ──────────────────────────────────────────────────────
    if await db.is_token_disabled(guild_id, symbol):
        return TradeResult(success=False, error=f"{symbol} trading is currently disabled by an admin.")

    tok_net = all_tokens.get(symbol, {}).get("network", "")
    net_key = _NET_MAP.get(tok_net, "")
    if net_key and await db.is_network_halted(guild_id, net_key):
        return TradeResult(success=False, error=f"The {tok_net} is currently halted by an admin.")

    # ── Sellable restriction (same set as buyable) ───────────────────────
    if symbol not in Config.BUYABLE_WITH_USD:
        return TradeResult(
            success=False,
            error=(
                f"{symbol} cannot be sold directly for USD. "
                f"Direct .sell is available for: {', '.join(sorted(Config.BUYABLE_WITH_USD))}."
            ),
        )

    # ── Holding check ────────────────────────────────────────────────────
    holding = await db.get_holding(user_id, guild_id, symbol)
    available = to_human(int(holding["amount"])) if holding else 0.0

    if available <= 0:
        return TradeResult(success=False, error=f"You have no {symbol} to sell.")
    if amount > available:
        return TradeResult(
            success=False,
            error=f"Insufficient {symbol}. You have {available:,.6f} but tried to sell {amount:,.6f}.",
        )

    # ── Price lookup ─────────────────────────────────────────────────────
    price_row = await db.get_price(symbol, guild_id)
    if not price_row:
        return TradeResult(success=False, error="Price data unavailable.")
    price = float(price_row["price"])

    # ── Revenue calculation ──────────────────────────────────────────────
    revenue = _snap(price * amount)

    # ── Fee calculation ──────────────────────────────────────────────────
    fee_cfg = await db.guilds.get_fee_config(guild_id)
    fee = _snap(max(
        fee_cfg["platform_fee_min"],
        min(fee_cfg["platform_fee_max"], revenue * fee_cfg["platform_fee_pct"]),
    ))
    net_revenue = _snap(revenue - fee)

    # ── Per-symbol trade cooldown (anti-sandwich) ────────────────────────
    if (cd := check_trade_cooldown(user_id, guild_id, symbol)) > 0:
        return TradeResult(success=False, error=f"Trade cooldown: {cd:.0f}s remaining for {symbol}.")

    # ── Volume limit (reserve atomically right before execution) ─────────
    allowed, remaining, reservation_id = await reserve_user_swap_volume(user_id, guild_id, revenue)
    if not allowed:
        return TradeResult(
            success=False,
            error=(
                f"Hourly volume limit reached. Remaining: ${remaining:,.2f}. "
                f"Limit: ${Config.USER_SWAP_HOURLY_LIMIT_USD:,.0f}/hour."
            ),
        )

    # ── Price impact: applied as per-trade slippage, oracle not touched ──
    impact = revenue / Config.PRICE_IMPACT_DIVISOR
    if impact >= 0.50:
        if reservation_id is not None:
            cancel_user_swap_reservation(user_id, guild_id, reservation_id)
        return TradeResult(success=False, error="Trade too large  -  price impact exceeds 50%. Try a smaller amount.")
    # Effective fill price accounts for slippage; user receives less USD
    eff_price = price * (1 - impact)
    eff_revenue = _snap(amount * eff_price)
    net_revenue = _snap(eff_revenue - fee)

    # ── Execute atomically ────────────────────────────────────────────────
    try:
        async with db.atomic():
            # Debit token holding
            await db.update_holding(user_id, guild_id, symbol, -to_raw(amount))

            # Credit USD wallet (net of fee, slippage-adjusted)
            new_wallet_raw = await db.update_wallet(user_id, guild_id, to_raw(net_revenue))

            # Split fee to community reserves
            await db.split_to_community_reserves(guild_id, "USD", to_raw(fee))

            # Log transaction
            tx_hash = await db.log_tx(
                guild_id, user_id, "SELL",
                symbol_in=symbol, amount_in=to_raw(amount),
                symbol_out="USD", amount_out=to_raw(eff_revenue),
                price_at=price,
                network="usd",
            )
    except Exception as e:
        if reservation_id is not None:
            cancel_user_swap_reservation(user_id, guild_id, reservation_id)
        return TradeResult(success=False, error=str(e))

    # Sell pressure  -  push the oracle down + write a candle row so the
    # chart reflects the sale. Outside the atomic block: a chart write
    # failure must never roll back the user's trade.
    await apply_trade_oracle_impact(
        db, guild_id, symbol, usd_value=eff_revenue, direction=-1,
    )

    set_trade_cooldown(user_id, guild_id, symbol)

    return TradeResult(
        success=True,
        tx_hash=tx_hash,
        amount=amount,
        cost=eff_revenue,
        fee=fee,
        new_price=eff_price,
        new_balance=to_human(int(new_wallet_raw)),
    )
