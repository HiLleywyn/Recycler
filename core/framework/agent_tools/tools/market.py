"""
core/framework/agent_tools/tools/market.py -- market data tools.

Powerful batched tools rather than one-per-field:

    market.snapshot     all token prices + 24h change + active event
    market.pool         reserves, TVL, swap fee, depth for one pool
    market.active_event the current market-event phase (bull, bear, swan, ...)
"""
from __future__ import annotations

import logging

from core.config import Config
from constants.trading import DEFAULT_SWAP_FEE
from core.framework.scale import SCALE, to_human

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.market")


@tool(
    name="market.snapshot",
    summary=(
        "Return the current oracle price, 24h open, day high/low, and ATH "
        "for every active token in this guild. Optionally filter to a "
        "single symbol."
    ),
    risk=RiskLevel.READ,
    category="market",
    cooldown_s=1,
    params=[
        ParamSpec("symbol", "symbol", required=False, default=None,
                  description="Optional: limit to a single token."),
    ],
)
async def snapshot(ctx: ToolContext, args: dict) -> ToolResult:
    sym = args.get("symbol")
    if sym:
        rows = await ctx.db.fetch_all(
            "SELECT symbol, price, open_price, day_high, day_low, ath, "
            "circulating_supply, updated_at FROM crypto_prices "
            "WHERE guild_id=$1 AND symbol=$2",
            int(ctx.guild_id), sym,
        )
    else:
        rows = await ctx.db.fetch_all(
            "SELECT symbol, price, open_price, day_high, day_low, ath, "
            "circulating_supply, updated_at FROM crypto_prices "
            "WHERE guild_id=$1 ORDER BY symbol",
            int(ctx.guild_id),
        )
    if not rows:
        return ToolResult.fail("no_prices: crypto_prices empty for this guild")

    out: list[dict] = []
    for r in rows:
        price = float(r["price"])
        open_p = float(r["open_price"])
        change_pct = 0.0
        if open_p > 0:
            change_pct = (price - open_p) / open_p * 100.0
        out.append({
            "symbol": str(r["symbol"]),
            "price": price,
            "open_price": open_p,
            "day_high": float(r["day_high"]),
            "day_low": float(r["day_low"]),
            "ath": float(r["ath"]),
            "change_pct": round(change_pct, 4),
            "circulating_supply": to_human(int(r["circulating_supply"] or 0)),
        })
    return ToolResult.success({"prices": out, "count": len(out)})


@tool(
    name="market.pool",
    summary=(
        "Return reserves and derived market data for an AMM pool: reserves, "
        "mid price, TVL in USD, dynamic max-swap fraction, swap fee."
    ),
    risk=RiskLevel.READ,
    category="market",
    params=[
        ParamSpec("token_a", "symbol", description="First token symbol."),
        ParamSpec("token_b", "symbol", description="Second token symbol."),
    ],
)
async def pool(ctx: ToolContext, args: dict) -> ToolResult:
    a, b = args["token_a"], args["token_b"]
    if a == b:
        return ToolResult.fail("token_a and token_b must differ")
    row = await ctx.db.fetch_one(
        """
        SELECT * FROM pools
        WHERE guild_id=$1
          AND (
              (token_a=$2 AND token_b=$3)
           OR (token_a=$3 AND token_b=$2)
          )
        LIMIT 1
        """,
        int(ctx.guild_id), a, b,
    )
    if row is None:
        return ToolResult.fail(f"no_pool: {a}/{b}")

    r_a = row.h("reserve_a")
    r_b = row.h("reserve_b")
    sym_a = str(row["token_a"])
    sym_b = str(row["token_b"])

    prices = {
        r["symbol"]: float(r["price"])
        for r in await ctx.db.fetch_all(
            "SELECT symbol, price FROM crypto_prices WHERE guild_id=$1 AND symbol IN ($2,$3)",
            int(ctx.guild_id), sym_a, sym_b,
        )
    }
    tvl_usd = r_a * prices.get(sym_a, 0.0) + r_b * prices.get(sym_b, 0.0)

    # Mirror compute_swap_quote()'s dynamic fraction so the agent sees the
    # same cap the real swap pipeline will enforce. Thin pools get a tighter
    # cap; everything else uses the default. LOW_LIQUIDITY_THRESHOLD is
    # stored in raw scaled units (matching scaled reserve math), so divide
    # by SCALE to compare against human-unit TVL.
    low_liq_threshold_usd = float(Config.LOW_LIQUIDITY_THRESHOLD) / float(SCALE)
    if tvl_usd < low_liq_threshold_usd:
        max_swap_pct = float(Config.LOW_LIQUIDITY_SWAP_FRACTION)
    else:
        max_swap_pct = float(Config.MAX_SWAP_FRACTION)

    mid_price_a_per_b = (r_a / r_b) if r_b > 0 else 0.0
    return ToolResult.success({
        "pool_id": str(row.get("pool_id") or ""),
        "token_a": sym_a,
        "token_b": sym_b,
        "reserve_a": r_a,
        "reserve_b": r_b,
        "tvl_usd": round(tvl_usd, 2),
        "mid_price_a_per_b": round(mid_price_a_per_b, 8),
        "swap_fee_rate": DEFAULT_SWAP_FEE,
        "max_swap_pct": max_swap_pct,
        "lp_total": row.h("total_lp"),
    })


@tool(
    name="market.active_event",
    summary=(
        "Return the active market-event phase for this guild: name, severity, "
        "directional bias, and remaining seconds."
    ),
    risk=RiskLevel.READ,
    category="market",
)
async def active_event(ctx: ToolContext, args: dict) -> ToolResult:
    # The market_event_engine service stores the active event in a well-known
    # table.  We query defensively so a missing table returns a clean "none"
    # instead of a traceback.
    try:
        row = await ctx.db.fetch_one(
            """
            SELECT event_type, phase, started_at, ends_at,
                   bias, vol_mult, severity
            FROM market_events
            WHERE guild_id=$1 AND ends_at > NOW()
            ORDER BY started_at DESC
            LIMIT 1
            """,
            int(ctx.guild_id),
        )
    except Exception as exc:
        log.debug("[market.active_event] %s", exc)
        return ToolResult.success({"active": False, "reason": "no_event_table"})

    if row is None:
        return ToolResult.success({"active": False})
    return ToolResult.success({
        "active": True,
        "event_type": str(row.get("event_type") or ""),
        "phase": str(row.get("phase") or ""),
        "severity": str(row.get("severity") or ""),
        "bias": float(row.get("bias") or 0.0),
        "vol_mult": float(row.get("vol_mult") or 1.0),
        "started_at": row.get("started_at"),
        "ends_at": row.get("ends_at"),
    })
