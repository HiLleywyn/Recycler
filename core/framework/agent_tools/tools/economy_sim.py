"""
core/framework/agent_tools/tools/economy_sim.py -- simulated economy tools.

Every tool in this file is a **what-if** calculator. None of them touch
persistent state; they project what *would* happen if X were true and
return the math. That means they are all ``RiskLevel.READ`` and can be
called freely by the AI agent when users ask questions like "how much
would it cost to dump 500 MTA into the USDC pool?" or "what happens to
the GDP if I drain half the vault?".

Tools
-----
``economy_sim.price_shock``    -- apply a +/- pct shock to a token and show
                                  the resulting holdings + net-worth deltas.
``economy_sim.whale_swap``     -- simulate a large AMM swap and report
                                  slippage, impact, and post-swap reserves.
``economy_sim.bank_run``       -- model the effect of N% of depositors
                                  withdrawing bank balances at once.
``economy_sim.supply_change``  -- model a virtual mint/burn on a token
                                  price via the configured elasticity.
``economy_sim.gdp_shock``      -- recompute guild GDP with a per-category
                                  multiplier and return the delta.
"""
from __future__ import annotations

import logging

from core.config import Config

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.tools.economy_sim")


# ── economy_sim.price_shock ──────────────────────────────────────────────────

@tool(
    name="economy_sim.price_shock",
    summary=(
        "Apply a hypothetical pct shock to a token's price and return "
        "projected holdings value changes guild-wide."
    ),
    risk=RiskLevel.READ,
    category="economy_sim",
    params=[
        ParamSpec("symbol", "symbol"),
        ParamSpec("shock_pct", "float", min=-99.0, max=1000.0,
                  description="Percentage change applied to the current price."),
    ],
)
async def _price_shock(ctx: ToolContext, args: dict) -> ToolResult:
    sym = str(args["symbol"]).upper()
    shock = float(args["shock_pct"]) / 100.0
    row = await ctx.db.fetch_one(
        "SELECT price FROM crypto_prices WHERE guild_id=$1 AND symbol=$2",
        int(ctx.guild_id), sym,
    )
    if not row:
        return ToolResult.fail(f"unknown_symbol: {sym}")
    current = float(row["price"] or 0.0)
    projected = current * (1.0 + shock)
    delta = projected - current

    holders = await ctx.db.fetch_all(
        "SELECT user_id, amount FROM crypto_holdings "
        "WHERE guild_id=$1 AND symbol=$2 AND amount > 0",
        int(ctx.guild_id), sym,
    )
    total_supply_held = sum(float(h.get("amount") or 0) for h in holders)
    current_mcap = total_supply_held * current
    projected_mcap = total_supply_held * projected
    holder_deltas = [
        {
            "user_id": int(h["user_id"]),
            "delta_usd": float(h.get("amount") or 0) * delta,
        }
        for h in holders
    ]
    holder_deltas.sort(key=lambda r: abs(r["delta_usd"]), reverse=True)
    return ToolResult.success({
        "symbol":          sym,
        "shock_pct":       float(args["shock_pct"]),
        "current_price":   current,
        "projected_price": projected,
        "per_token_delta": delta,
        "holders":         len(holder_deltas),
        "total_held":      total_supply_held,
        "current_mcap":    current_mcap,
        "projected_mcap":  projected_mcap,
        "mcap_delta":      projected_mcap - current_mcap,
        "top_affected":    holder_deltas[:10],
    })


# ── economy_sim.whale_swap ───────────────────────────────────────────────────

@tool(
    name="economy_sim.whale_swap",
    summary=(
        "Project the outcome of a large AMM swap -- constant product math "
        "only, no state change. Shows slippage, post-swap reserves, effective "
        "price, and impact on the quoted pair."
    ),
    risk=RiskLevel.READ,
    category="economy_sim",
    params=[
        ParamSpec("token_in",  "symbol"),
        ParamSpec("token_out", "symbol"),
        ParamSpec("amount_in", "float", min=0.0),
    ],
)
async def _whale_swap(ctx: ToolContext, args: dict) -> ToolResult:
    ti = str(args["token_in"]).upper()
    to = str(args["token_out"]).upper()
    amt_in = float(args["amount_in"])
    if ti == to:
        return ToolResult.fail("token_in == token_out")
    if amt_in <= 0:
        return ToolResult.fail("amount_in must be positive")
    try:
        pool_id, ca, cb = ctx.db.make_pool_id(ti, to)
    except Exception as exc:
        return ToolResult.fail(f"pool_lookup: {exc}")
    pool = await ctx.db.get_pool(pool_id, int(ctx.guild_id))
    if not pool:
        return ToolResult.fail(f"no_pool_for:{ca}/{cb}")

    reserve_a = float(pool.get("reserve_a") or 0.0)
    reserve_b = float(pool.get("reserve_b") or 0.0)
    if reserve_a <= 0 or reserve_b <= 0:
        return ToolResult.fail("pool_dry")

    if ti == ca:
        r_in, r_out = reserve_a, reserve_b
    else:
        r_in, r_out = reserve_b, reserve_a

    fee = float(getattr(Config, "DEFAULT_SWAP_FEE_PCT", 0.003) or 0.003)
    amt_after_fee = amt_in * (1.0 - fee)
    k = r_in * r_out
    new_r_in = r_in + amt_after_fee
    new_r_out = k / new_r_in
    amt_out = r_out - new_r_out
    spot_before = r_out / r_in if r_in else 0.0
    effective = amt_out / amt_in if amt_in else 0.0
    impact_pct = ((spot_before - effective) / spot_before * 100.0) if spot_before else 0.0

    return ToolResult.success({
        "pool_id": pool_id,
        "token_in": ti,
        "token_out": to,
        "amount_in": amt_in,
        "amount_out": amt_out,
        "fee_pct": fee * 100.0,
        "reserve_in_before": r_in,
        "reserve_out_before": r_out,
        "reserve_in_after": new_r_in,
        "reserve_out_after": new_r_out,
        "spot_price_before": spot_before,
        "effective_price": effective,
        "price_impact_pct": impact_pct,
        "pool_drained_pct": (amt_in / r_in * 100.0) if r_in else 0.0,
    })


# ── economy_sim.bank_run ─────────────────────────────────────────────────────

@tool(
    name="economy_sim.bank_run",
    summary=(
        "Model a bank run. Projects how many players would hit the withdraw "
        "cap if the given percentage of bank depositors pulled at once."
    ),
    risk=RiskLevel.READ,
    category="economy_sim",
    params=[
        ParamSpec("withdraw_pct", "float", min=0.0, max=100.0,
                  description="Percent of bank depositors that withdraw."),
        ParamSpec("cap_usd", "float", required=False, default=0.0, min=0.0,
                  description="Per-player daily withdraw cap. 0 = no cap."),
    ],
)
async def _bank_run(ctx: ToolContext, args: dict) -> ToolResult:
    pct = float(args["withdraw_pct"]) / 100.0
    cap = float(args.get("cap_usd") or 0.0)
    rows = await ctx.db.fetch_all(
        "SELECT user_id, bank FROM users "
        "WHERE guild_id=$1 AND bank > 0 "
        "ORDER BY bank DESC",
        int(ctx.guild_id),
    )
    total_depositors = len(rows)
    runners = int(round(total_depositors * pct))
    running_sample = rows[:runners] if runners else []
    gross = sum(float(r.get("bank") or 0.0) for r in running_sample)
    if cap > 0:
        paid = sum(min(float(r.get("bank") or 0.0), cap) for r in running_sample)
    else:
        paid = gross
    shortfall = gross - paid
    capped_players = sum(
        1 for r in running_sample
        if cap > 0 and float(r.get("bank") or 0.0) > cap
    )
    return ToolResult.success({
        "total_depositors": total_depositors,
        "runners":          runners,
        "pct":              pct * 100.0,
        "gross_demanded":   gross,
        "paid_out":         paid,
        "shortfall":        shortfall,
        "capped_players":   capped_players,
        "cap_usd":          cap,
    })


# ── economy_sim.supply_change ────────────────────────────────────────────────

@tool(
    name="economy_sim.supply_change",
    summary=(
        "Project a token price after a virtual mint/burn using a constant "
        "elasticity model: new_price = current * (old_supply / new_supply)^e."
    ),
    risk=RiskLevel.READ,
    category="economy_sim",
    params=[
        ParamSpec("symbol",   "symbol"),
        ParamSpec("delta_supply", "float",
                  description="Positive = mint, negative = burn (human units)."),
        ParamSpec("elasticity", "float", required=False, default=1.0,
                  min=0.0, max=10.0,
                  description="Price-to-supply elasticity exponent."),
    ],
)
async def _supply_change(ctx: ToolContext, args: dict) -> ToolResult:
    sym = str(args["symbol"]).upper()
    delta = float(args["delta_supply"])
    e = float(args.get("elasticity") or 1.0)
    row = await ctx.db.fetch_one(
        "SELECT price, circulating_supply FROM crypto_prices "
        "WHERE guild_id=$1 AND symbol=$2",
        int(ctx.guild_id), sym,
    )
    if not row:
        return ToolResult.fail(f"unknown_symbol: {sym}")
    current_price = float(row["price"] or 0.0)
    current_supply = float(row.get("circulating_supply") or 0.0)
    new_supply = current_supply + delta
    if new_supply <= 0:
        return ToolResult.fail("new_supply_non_positive")
    ratio = current_supply / new_supply if new_supply else 1.0
    new_price = current_price * (ratio ** e)
    return ToolResult.success({
        "symbol": sym,
        "current_price":  current_price,
        "projected_price": new_price,
        "current_supply": current_supply,
        "new_supply":     new_supply,
        "delta_supply":   delta,
        "elasticity":     e,
        "price_pct_change": ((new_price / current_price - 1.0) * 100.0) if current_price else 0.0,
        "current_mcap":  current_supply * current_price,
        "projected_mcap": new_supply * new_price,
    })


# ── economy_sim.gdp_shock ────────────────────────────────────────────────────

@tool(
    name="economy_sim.gdp_shock",
    summary=(
        "Project guild GDP under hypothetical multipliers per category "
        "(wallet, bank, crypto, items). Returns current vs projected totals."
    ),
    risk=RiskLevel.READ,
    category="economy_sim",
    params=[
        ParamSpec("wallet_mult", "float", required=False, default=1.0, min=0.0, max=100.0),
        ParamSpec("bank_mult",   "float", required=False, default=1.0, min=0.0, max=100.0),
        ParamSpec("crypto_mult", "float", required=False, default=1.0, min=0.0, max=100.0),
        ParamSpec("items_mult",  "float", required=False, default=1.0, min=0.0, max=100.0),
    ],
)
async def _gdp_shock(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        from services.net_worth import compute_bulk_net_worth
    except Exception as exc:
        return ToolResult.fail(f"net_worth_unavailable: {exc}")
    wm = float(args.get("wallet_mult") or 1.0)
    bm = float(args.get("bank_mult") or 1.0)
    cm = float(args.get("crypto_mult") or 1.0)
    im = float(args.get("items_mult") or 1.0)

    try:
        worths = await compute_bulk_net_worth(int(ctx.guild_id), ctx.db)
    except Exception as exc:
        return ToolResult.fail(f"net_worth_crashed: {exc}")
    current = float(sum(worths.values())) if worths else 0.0

    # Per-category aggregate reads so we can rescale independently.
    wallet_sum = float(await ctx.db.fetch_val(
        "SELECT COALESCE(SUM(wallet),0) FROM users WHERE guild_id=$1",
        int(ctx.guild_id),
    ) or 0.0)
    bank_sum = float(await ctx.db.fetch_val(
        "SELECT COALESCE(SUM(bank),0) FROM users WHERE guild_id=$1",
        int(ctx.guild_id),
    ) or 0.0)
    crypto_row = await ctx.db.fetch_all(
        "SELECT ch.symbol, SUM(ch.amount) AS amt "
        "FROM crypto_holdings ch WHERE ch.guild_id=$1 GROUP BY ch.symbol",
        int(ctx.guild_id),
    )
    crypto_value = 0.0
    for r in crypto_row:
        sym = r.get("symbol")
        amt = float(r.get("amt") or 0.0)
        price_row = await ctx.db.fetch_one(
            "SELECT price FROM crypto_prices WHERE guild_id=$1 AND symbol=$2",
            int(ctx.guild_id), sym,
        )
        price = float(price_row["price"]) if price_row else 0.0
        crypto_value += amt * price

    items_residual = max(0.0, current - wallet_sum - bank_sum - crypto_value)

    projected = (
        wallet_sum * wm
        + bank_sum * bm
        + crypto_value * cm
        + items_residual * im
    )
    return ToolResult.success({
        "current_gdp":    current,
        "projected_gdp":  projected,
        "delta":          projected - current,
        "delta_pct":      ((projected / current - 1.0) * 100.0) if current else 0.0,
        "components": {
            "wallet":  wallet_sum,
            "bank":    bank_sum,
            "crypto":  crypto_value,
            "items_residual": items_residual,
        },
        "multipliers": {
            "wallet": wm, "bank": bm, "crypto": cm, "items": im,
        },
    })
