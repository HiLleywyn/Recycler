"""
core/framework/agent_tools/tools/savings.py -- savings account tools.

    savings.summary   caller's savings deposits across every stablecoin
                      with current principal and last-interest timestamp (READ).
    savings.deposit   deposit USD or SUN from wallet/holdings into savings (MUTATE).
    savings.withdraw  withdraw from savings back to wallet/holdings (MUTATE).
"""
from __future__ import annotations

import logging
import math

from core.config import Config

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.savings")


def _stablecoin_symbols() -> list[str]:
    return [
        sym for sym, cfg in Config.TOKENS.items()
        if cfg.get("stablecoin") is True
    ]


@tool(
    name="savings.summary",
    summary=(
        "Return the caller's savings deposits across every supported "
        "stablecoin, with current principal, last-interest time, and the "
        "guaranteed floor APY from the savings rate model."
    ),
    risk=RiskLevel.READ,
    category="savings",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def summary(ctx: ToolContext, args: dict) -> ToolResult:
    target = int(args.get("target_id") or ctx.user_id)
    gid = int(ctx.guild_id)

    # Base savings rate is daily -- express as an approximate APY floor
    # (the real paid rate varies with borrow utilisation).
    try:
        base_daily = float(
            Config.SAVINGS_RATE_MODEL.get("base_savings_rate", 0.0) or 0.0
        )
    except Exception:
        base_daily = 0.0
    base_apy_floor = ((1.0 + base_daily) ** 365 - 1.0) if base_daily > 0 else 0.0

    deposits: list[dict] = []
    total_usd = 0.0
    for sym in _stablecoin_symbols():
        try:
            row = await ctx.db.get_savings_deposit(target, gid, sym)
        except Exception as exc:
            log.warning("[savings.summary] read failed for %s: %s", sym, exc)
            continue
        if not row:
            continue
        amount = row.h("amount")
        if amount <= 0:
            continue
        last_interest = row.get("last_interest")
        total_usd += amount  # stablecoin -> $1 peg
        deposits.append({
            "symbol":         sym,
            "amount":         round(amount, 6),
            "usd_value":      round(amount, 2),
            "last_interest":  last_interest,
        })

    return ToolResult.success({
        "target_id":      target,
        "total_usd":      round(total_usd, 2),
        "deposit_count":  len(deposits),
        "deposits":       deposits,
        "base_apy_floor": round(base_apy_floor, 4),
    })


# -- savings.deposit -----------------------------------------------------------

@tool(
    name="savings.deposit",
    summary=(
        "Deposit USD (from wallet) or SUN (from holdings) into the savings "
        "account to earn dynamic interest. "
        "Returns new savings balance and the amount deposited."
    ),
    risk=RiskLevel.MUTATE,
    category="savings",
    params=[
        ParamSpec("amount", "float", min=0.0,
                  description="Amount to deposit in human units."),
        ParamSpec("symbol", "str", required=False, default="USD",
                  description="Token to deposit: USD or SUN."),
    ],
)
async def deposit(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)
    amount = float(args.get("amount") or 0)
    symbol = str(args.get("symbol") or "USD").upper()

    if not math.isfinite(amount) or amount <= 0:
        return ToolResult.fail("amount must be a positive finite number")

    try:
        from services.savings import deposit_savings
    except Exception as exc:
        return ToolResult.fail(f"savings_service_unavailable: {exc}")

    result = await deposit_savings(ctx.db, gid, uid, symbol, amount)
    if not result.success:
        return ToolResult.fail(f"deposit_failed: {result.error}")

    if ctx.bus:
        try:
            await ctx.bus.publish(
                "savings_deposited",
                guild_id=gid, user_id=uid,
                amount=amount, symbol=symbol,
            )
        except Exception:
            pass

    return ToolResult.success({
        "deposited": round(result.amount, 6),
        "symbol": result.symbol,
        "new_savings_balance": round(result.new_savings_balance, 6),
    })


# -- savings.withdraw ----------------------------------------------------------

@tool(
    name="savings.withdraw",
    summary=(
        "Withdraw USD or SUN from savings back to wallet/holdings. "
        "Interest accrued up to this point is kept. "
        "Returns new savings balance."
    ),
    risk=RiskLevel.MUTATE,
    category="savings",
    params=[
        ParamSpec("amount", "float", min=0.0,
                  description="Amount to withdraw in human units."),
        ParamSpec("symbol", "str", required=False, default="USD",
                  description="Token to withdraw: USD or SUN."),
    ],
)
async def withdraw(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)
    amount = float(args.get("amount") or 0)
    symbol = str(args.get("symbol") or "USD").upper()

    if not math.isfinite(amount) or amount <= 0:
        return ToolResult.fail("amount must be a positive finite number")

    try:
        from services.savings import withdraw_savings
    except Exception as exc:
        return ToolResult.fail(f"savings_service_unavailable: {exc}")

    result = await withdraw_savings(ctx.db, gid, uid, symbol, amount)
    if not result.success:
        return ToolResult.fail(f"withdrawal_failed: {result.error}")

    if ctx.bus:
        try:
            await ctx.bus.publish(
                "savings_withdrawn",
                guild_id=gid, user_id=uid,
                amount=amount, symbol=symbol,
            )
        except Exception:
            pass

    return ToolResult.success({
        "withdrawn": round(result.amount, 6),
        "symbol": result.symbol,
        "new_savings_balance": round(result.new_savings_balance, 6),
    })
