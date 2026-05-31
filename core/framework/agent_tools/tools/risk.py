"""
core/framework/agent_tools/tools/risk.py -- portfolio risk analyzer.

Single powerful tool: risk.analyze.

Given a user id, returns:
  - net_worth_usd
  - loan health (LTV, liquidation price, buffer, at-risk flag)
  - portfolio concentration (largest holding as % of net worth)
  - stake slash exposure (total staked with %-of-net-worth)
  - gambling volatility flag (used daily by the social/gossip tool)

The goal is a single call that the AI can use to drive liquidation alerts,
degen warnings, and rebalance suggestions. No silver bullets, just numbers.
"""
from __future__ import annotations

import logging


from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.risk")


# Classic risk thresholds. Copied here because risk analysis is a single
# shared place -- not duplicated into every caller.
LTV_WARN = 0.80
LTV_DANGER = 0.88
LTV_LIQUIDATE = 0.90
CONCENTRATION_WARN = 0.60   # any single holding > 60% of net worth
CONCENTRATION_DANGER = 0.80


@tool(
    name="risk.analyze",
    summary=(
        "Compute portfolio risk: loan LTV and liquidation buffer, single-"
        "holding concentration, stake slash exposure. Returns structured "
        "flags the agent can branch on."
    ),
    risk=RiskLevel.READ,
    category="risk",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Player id to analyse. Defaults to the caller."),
    ],
)
async def analyze(ctx: ToolContext, args: dict) -> ToolResult:
    target = int(args.get("target_id") or ctx.user_id)

    try:
        from services.net_worth import compute_net_worth
    except Exception as exc:
        return ToolResult.fail(f"net_worth service unavailable: {exc}")
    try:
        nw = await compute_net_worth(target, ctx.guild_id, ctx.db)
    except Exception as exc:
        return ToolResult.fail(f"net_worth_compute_failed: {exc}")

    net_worth = float(nw.total)

    # ── Holdings concentration (single largest position / net worth) ─────
    prices = {
        r["symbol"]: float(r["price"])
        for r in await ctx.db.fetch_all(
            "SELECT symbol, price FROM crypto_prices WHERE guild_id=$1",
            int(ctx.guild_id),
        )
    }
    holdings_rows = await ctx.db.fetch_all(
        "SELECT symbol, amount FROM crypto_holdings "
        "WHERE guild_id=$1 AND user_id=$2",
        int(ctx.guild_id), target,
    )
    largest = {"symbol": "", "usd_value": 0.0}
    for r in holdings_rows:
        amt = r.h("amount")
        if amt <= 0:
            continue
        sym = str(r["symbol"])
        usd = amt * float(prices.get(sym, 0.0))
        if usd > largest["usd_value"]:
            largest = {"symbol": sym, "usd_value": usd}
    concentration_ratio = 0.0
    if net_worth > 0 and largest["usd_value"] > 0:
        concentration_ratio = largest["usd_value"] / net_worth

    # ── Loan health ───────────────────────────────────────────────────────
    # The loans table stores raw scaled integers for principal / outstanding
    # / collateral. Outstanding is the dollar-amount debt; collateral is the
    # USD valuation at loan creation. LTV is outstanding / collateral.
    loan_row = await ctx.db.fetch_one(
        """
        SELECT principal, outstanding, collateral
        FROM loans
        WHERE guild_id=$1 AND user_id=$2 AND outstanding > 0
        LIMIT 1
        """,
        int(ctx.guild_id), target,
    )
    loan_info: dict = {"active": False}
    if loan_row is not None:
        outstanding = loan_row.h("outstanding")
        collat = loan_row.h("collateral")
        ltv = 0.0
        if collat > 0:
            ltv = outstanding / collat
        buffer_pct = max(0.0, (LTV_LIQUIDATE - ltv) / LTV_LIQUIDATE * 100.0)
        loan_info = {
            "active": True,
            "outstanding_usd": round(outstanding, 2),
            "collateral_usd": round(collat, 2),
            "ltv": round(ltv, 4),
            "liquidation_ltv": LTV_LIQUIDATE,
            "buffer_pct": round(buffer_pct, 2),
            "at_risk": ltv >= LTV_WARN,
            "danger": ltv >= LTV_DANGER,
        }

    # ── Stake slash exposure ─────────────────────────────────────────────
    stake_total = float(nw.stake_value + nw.pos_stake_value + nw.delegation_value)
    stake_pct_of_nw = 0.0
    if net_worth > 0:
        stake_pct_of_nw = stake_total / net_worth

    flags: list[str] = []
    if loan_info.get("at_risk"):
        flags.append("loan_at_risk")
    if loan_info.get("danger"):
        flags.append("loan_near_liquidation")
    if concentration_ratio >= CONCENTRATION_DANGER:
        flags.append("concentration_danger")
    elif concentration_ratio >= CONCENTRATION_WARN:
        flags.append("concentration_warn")
    if stake_pct_of_nw > 0.75:
        flags.append("stake_heavy")
    if net_worth < 100.0:
        flags.append("broke")

    return ToolResult.success({
        "target_id": target,
        "net_worth_usd": round(net_worth, 2),
        "loan": loan_info,
        "largest_holding": {
            "symbol": largest["symbol"],
            "usd_value": round(largest["usd_value"], 2),
            "pct_of_net_worth": round(concentration_ratio * 100.0, 2),
        },
        "stake_exposure_pct": round(stake_pct_of_nw * 100.0, 2),
        "flags": flags,
    })
