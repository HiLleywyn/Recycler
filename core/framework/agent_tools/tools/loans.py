"""
core/framework/agent_tools/tools/loans.py -- loans tools.

    loans.summary   caller's active loan state: principal, outstanding,
                    collateral, and last interest timestamp (READ).
    loans.borrow    borrow USD using bank balance as collateral (MUTATE).
    loans.repay     repay an outstanding loan partially or in full (MUTATE).
"""
from __future__ import annotations

import datetime
import logging

from core.config import Config
from core.framework.scale import to_raw

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.loans")


@tool(
    name="loans.summary",
    summary=(
        "Return the caller's current loan state: principal, outstanding, "
        "collateral. Returns ``{has_loan: false}`` when the caller has no "
        "loan in this server."
    ),
    risk=RiskLevel.READ,
    category="loans",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def summary(ctx: ToolContext, args: dict) -> ToolResult:
    target = int(args.get("target_id") or ctx.user_id)
    try:
        row = await ctx.db.get_loan(target, int(ctx.guild_id))
    except Exception as exc:
        log.warning("[loans.summary] read failed: %s", exc)
        return ToolResult.fail(f"loan_read_failed: {exc}")

    if not row:
        return ToolResult.success({
            "target_id": target,
            "has_loan":  False,
        })

    principal   = row.h("principal")
    outstanding = row.h("outstanding")
    collateral  = row.h("collateral")

    return ToolResult.success({
        "target_id":      target,
        "has_loan":       True,
        "principal":      round(principal, 2),
        "outstanding":    round(outstanding, 2),
        "collateral":     round(collateral, 2),
        "last_interest":  row.get("last_interest"),
    })


# -- loans.borrow --------------------------------------------------------------

_L = Config.LENDING
_MAX_LTV = _L["MAX_LTV"]
_LIQ_THR = _L["LIQUIDATION_THRESHOLD"]


@tool(
    name="loans.borrow",
    summary=(
        "Borrow USD against the caller's bank balance as collateral. "
        "Collateral is locked at borrow_amount / MAX_LTV. "
        "Daily interest accrues. Returns loan details and rate."
    ),
    risk=RiskLevel.MUTATE,
    category="loans",
    params=[
        ParamSpec("amount", "float", min=0.0,
                  description="USD amount to borrow."),
    ],
)
async def borrow(ctx: ToolContext, args: dict) -> ToolResult:
    import math
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)
    amount = float(args.get("amount") or 0)

    if not math.isfinite(amount) or amount <= 0:
        return ToolResult.fail("amount must be a positive finite number")

    existing = await ctx.db.get_loan(uid, gid)
    if existing:
        outstanding_h = round(existing.h("outstanding"), 2)
        return ToolResult.fail(
            f"already_have_loan: outstanding balance is ${outstanding_h:,.2f}. "
            "Use loans.repay to clear it first."
        )

    collateral = amount / _MAX_LTV
    row = await ctx.db.get_user(uid, gid)
    if not row:
        return ToolResult.fail("user_not_found")

    bank_h = row.h("bank")
    if bank_h < collateral:
        return ToolResult.fail(
            f"insufficient_bank_collateral: need ${collateral:,.2f} in bank "
            f"(you have ${bank_h:,.2f})"
        )

    async with ctx.db.atomic():
        await ctx.db.update_bank(uid, gid, to_raw(-collateral))
        await ctx.db.update_wallet(uid, gid, to_raw(amount))
        await ctx.db.upsert_loan(
            uid, gid,
            principal=to_raw(amount),
            outstanding=to_raw(amount),
            collateral=to_raw(collateral),
            last_interest=datetime.datetime.now(datetime.timezone.utc),
        )

    try:
        tx_hash = await ctx.db.log_tx(
            gid, uid, "LEND",
            symbol_in="COLLATERAL", amount_in=to_raw(collateral),
            symbol_out="USD", amount_out=to_raw(amount),
            network="usd",
        )
    except Exception:
        tx_hash = ""

    if ctx.bus:
        try:
            await ctx.bus.publish(
                "loan_opened",
                guild_id=gid, user_id=uid,
                amount=amount, collateral=collateral, tx_hash=tx_hash,
            )
        except Exception:
            pass

    return ToolResult.success({
        "borrowed_usd": round(amount, 2),
        "collateral_locked_usd": round(collateral, 2),
        "ltv_pct": round(amount / collateral * 100, 1),
        "liquidation_ltv_pct": round(_LIQ_THR * 100, 1),
        "tx_hash": tx_hash,
    })


# -- loans.repay ---------------------------------------------------------------

@tool(
    name="loans.repay",
    summary=(
        "Repay an outstanding loan. Pass amount='all' to fully repay. "
        "Proportional collateral is returned to the bank on each payment. "
        "Full repayment closes the loan and returns all collateral."
    ),
    risk=RiskLevel.MUTATE,
    category="loans",
    params=[
        ParamSpec("amount", "str",
                  description="USD amount to repay, or 'all' to fully repay."),
    ],
)
async def repay(ctx: ToolContext, args: dict) -> ToolResult:
    import math
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)
    amount_raw = str(args.get("amount") or "all").strip().lower()

    loan = await ctx.db.get_loan(uid, gid)
    if not loan:
        return ToolResult.fail("no_active_loan")

    outstanding_h = loan.h("outstanding")
    collateral_h = loan.h("collateral")

    if amount_raw == "all":
        repay_amt = outstanding_h
    else:
        try:
            repay_amt = float(amount_raw.lstrip("$").replace(",", ""))
        except ValueError:
            return ToolResult.fail("invalid amount: use a number or 'all'")
        if not math.isfinite(repay_amt) or repay_amt <= 0:
            return ToolResult.fail("amount must be a positive finite number")

    repay_amt = min(repay_amt, outstanding_h)

    row = await ctx.db.get_user(uid, gid)
    if not row:
        return ToolResult.fail("user_not_found")
    wallet_h = row.h("wallet")
    if wallet_h < repay_amt - 0.005:
        return ToolResult.fail(
            f"insufficient_wallet: need ${repay_amt:,.2f} but have ${wallet_h:,.2f}"
        )

    new_outstanding = outstanding_h - repay_amt
    frac_repaid = repay_amt / outstanding_h if outstanding_h > 0 else 1.0
    collateral_returned = collateral_h * frac_repaid
    loan_closed = new_outstanding <= 0.001

    if loan_closed:
        async with ctx.db.atomic():
            await ctx.db.update_wallet(uid, gid, to_raw(-repay_amt))
            await ctx.db.delete_loan(uid, gid)
            await ctx.db.update_bank(uid, gid, loan["collateral"])
    else:
        remaining_collateral = collateral_h - collateral_returned
        async with ctx.db.atomic():
            await ctx.db.update_wallet(uid, gid, to_raw(-repay_amt))
            await ctx.db.update_bank(uid, gid, to_raw(collateral_returned))
            await ctx.db.upsert_loan(
                uid, gid,
                loan["principal"], to_raw(new_outstanding),
                to_raw(remaining_collateral),
                datetime.datetime.now(datetime.timezone.utc),
            )

    try:
        tx_hash = await ctx.db.log_tx(
            gid, uid, "REPAY",
            symbol_in="USD", amount_in=to_raw(repay_amt),
            symbol_out="COLLATERAL", amount_out=to_raw(collateral_returned),
            network="usd",
        )
    except Exception:
        tx_hash = ""

    if ctx.bus:
        try:
            await ctx.bus.publish(
                "loan_repaid",
                guild_id=gid, user_id=uid,
                amount_paid=repay_amt,
                remaining=round(new_outstanding, 2) if not loan_closed else 0.0,
            )
        except Exception:
            pass

    return ToolResult.success({
        "repaid_usd": round(repay_amt, 2),
        "collateral_returned_usd": round(collateral_returned, 2),
        "loan_closed": loan_closed,
        "remaining_outstanding_usd": round(max(0.0, new_outstanding), 2),
        "tx_hash": tx_hash,
    })
