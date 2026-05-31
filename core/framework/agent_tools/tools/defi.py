"""
core/framework/agent_tools/tools/defi.py -- DeFi execution agent tools.

Wraps the existing ``services/swap.py``, ``services/stake.py`` and
``services/liquidity.py`` layers so the AI agent can plan and execute
swaps, staking, and liquidity provision on behalf of the user. Every
tool that moves real balances is classified MUTATE so the normal
approval gate in :func:`run_tool` fires before the handler runs; the
"plan/quote" variants are READ so the agent can model outcomes without
touching state.

Tools
-----
``defi.quote_swap``        -- dry-run an AMM swap (READ).
``defi.execute_swap``      -- perform the swap through services.swap (MUTATE).
``defi.pool_info``         -- inspect reserves / TVL / price for a pair (READ).
``defi.add_liquidity``     -- deposit into an AMM pool (MUTATE).
``defi.remove_liquidity``  -- withdraw LP position (MUTATE).
``defi.stake``             -- stake on a validator (MUTATE).
``defi.unstake``           -- unstake from a validator (MUTATE).
``defi.validators_list``   -- read-only list of active validators (READ).
"""
from __future__ import annotations

import logging

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.tools.defi")


# ── Swap helpers ─────────────────────────────────────────────────────────────

@tool(
    name="defi.quote_swap",
    summary=(
        "Price an AMM swap without touching balances. Returns amount_out, "
        "price impact, fees, and any warnings."
    ),
    risk=RiskLevel.READ,
    category="defi",
    params=[
        ParamSpec("token_in",  "symbol", description="Symbol being sold."),
        ParamSpec("token_out", "symbol", description="Symbol being bought."),
        ParamSpec("amount_in", "float",  min=0.0,
                  description="Amount of token_in to swap."),
        ParamSpec("gas_price", "str", required=False, default="medium",
                  choices=["low", "medium", "high"],
                  description="Gas tier to use for the quote."),
    ],
)
async def _quote_swap(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        from services.swap import compute_swap_quote
    except Exception as exc:
        return ToolResult.fail(f"swap_service_unavailable: {exc}")
    try:
        quote = await compute_swap_quote(
            ctx.db,
            guild_id=int(ctx.guild_id),
            user_id=int(ctx.user_id),
            token_in=str(args["token_in"]),
            token_out=str(args["token_out"]),
            amount_in=float(args["amount_in"]),
            gas_price=str(args.get("gas_price") or "medium"),
        )
    except Exception as exc:
        return ToolResult.fail(f"quote_crashed: {exc}")
    if isinstance(quote, str):
        return ToolResult.fail(quote)
    return ToolResult.success({
        "token_in":       quote.token_in,
        "token_out":      quote.token_out,
        "amount_in":      float(quote.amount_in),
        "amount_out":     float(quote.amount_out),
        "price_impact":   float(getattr(quote, "price_impact", 0.0) or 0.0),
        "fee_paid":       float(getattr(quote, "fee_paid", 0.0) or 0.0),
        "platform_fee":   float(getattr(quote, "platform_fee", 0.0) or 0.0),
        "total_gas_cost": float(getattr(quote, "total_gas_cost", 0.0) or 0.0),
        "gas_coin":       getattr(quote, "gas_coin", ""),
        "network":        getattr(quote, "network", ""),
        "warnings":       list(getattr(quote, "warnings", []) or []),
    })


@tool(
    name="defi.execute_swap",
    summary=(
        "Execute an AMM swap. Recomputes the quote server-side, then calls "
        "services.swap.execute_swap. Requires approval."
    ),
    risk=RiskLevel.MUTATE,
    category="defi",
    params=[
        ParamSpec("token_in",  "symbol", description="Symbol being sold."),
        ParamSpec("token_out", "symbol", description="Symbol being bought."),
        ParamSpec("amount_in", "float",  min=0.0,
                  description="Amount of token_in to swap."),
        ParamSpec("gas_price", "str", required=False, default="medium",
                  choices=["low", "medium", "high"]),
        ParamSpec("min_amount_out", "float", required=False, default=0.0, min=0.0,
                  description="Slippage floor; 0 applies the default 2% tolerance."),
    ],
)
async def _execute_swap(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        from services.swap import compute_swap_quote, execute_swap
    except Exception as exc:
        return ToolResult.fail(f"swap_service_unavailable: {exc}")
    try:
        quote = await compute_swap_quote(
            ctx.db,
            guild_id=int(ctx.guild_id),
            user_id=int(ctx.user_id),
            token_in=str(args["token_in"]),
            token_out=str(args["token_out"]),
            amount_in=float(args["amount_in"]),
            gas_price=str(args.get("gas_price") or "medium"),
            min_amount_out=float(args.get("min_amount_out") or 0.0),
        )
    except Exception as exc:
        return ToolResult.fail(f"quote_crashed: {exc}")
    if isinstance(quote, str):
        return ToolResult.fail(quote)
    try:
        result = await execute_swap(
            ctx.db, int(ctx.guild_id), int(ctx.user_id), quote,
        )
    except Exception as exc:
        return ToolResult.fail(f"swap_crashed: {exc}")
    if not result.success:
        return ToolResult.fail(result.error or "swap_rejected")
    return ToolResult.success({
        "tx_hash":    result.tx_hash,
        "mempool_id": result.mempool_id,
        "amount_in":  float(quote.amount_in),
        "amount_out": float(result.amount_out),
        "token_in":   quote.token_in,
        "token_out":  quote.token_out,
    })


# ── Pool inspection ──────────────────────────────────────────────────────────

@tool(
    name="defi.pool_info",
    summary="Read-only pool snapshot: reserves, price, and TVL for a pair.",
    risk=RiskLevel.READ,
    category="defi",
    params=[
        ParamSpec("token_a", "symbol"),
        ParamSpec("token_b", "symbol"),
    ],
)
async def _pool_info(ctx: ToolContext, args: dict) -> ToolResult:
    db = ctx.db
    try:
        pool_id, ca, cb = db.make_pool_id(str(args["token_a"]), str(args["token_b"]))
    except Exception as exc:
        return ToolResult.fail(f"pool_lookup_failed: {exc}")
    pool = await db.get_pool(pool_id, int(ctx.guild_id))
    if not pool:
        return ToolResult.fail(f"no_pool_for:{ca}/{cb}")
    price_a_row = await db.get_price(ca, int(ctx.guild_id))
    price_b_row = await db.get_price(cb, int(ctx.guild_id))
    p_a = float(price_a_row["price"]) if price_a_row else 0.0
    p_b = float(price_b_row["price"]) if price_b_row else 0.0
    reserve_a = float(pool.get("reserve_a") or 0.0)
    reserve_b = float(pool.get("reserve_b") or 0.0)
    tvl = reserve_a * p_a + reserve_b * p_b
    price_a_in_b = (reserve_b / reserve_a) if reserve_a > 0 else 0.0
    return ToolResult.success({
        "pool_id":   pool_id,
        "token_a":   ca,
        "token_b":   cb,
        "reserve_a": reserve_a,
        "reserve_b": reserve_b,
        "price_a_in_b": price_a_in_b,
        "price_a_usd":  p_a,
        "price_b_usd":  p_b,
        "tvl_usd":   tvl,
        "total_lp":  float(pool.get("total_lp") or 0.0),
    })


# ── Liquidity provision ──────────────────────────────────────────────────────

@tool(
    name="defi.add_liquidity",
    summary=(
        "Add liquidity to an AMM pool. For existing pools only amount_a is "
        "needed; amount_b is computed from the pool ratio. Requires approval."
    ),
    risk=RiskLevel.MUTATE,
    category="defi",
    params=[
        ParamSpec("token_a", "symbol"),
        ParamSpec("token_b", "symbol"),
        ParamSpec("amount_a", "float", min=0.0),
        ParamSpec("amount_b", "float", required=False, default=0.0, min=0.0,
                  description="Only used to seed a brand-new empty pool."),
    ],
)
async def _add_liquidity(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        from services.liquidity import add_liquidity
    except Exception as exc:
        return ToolResult.fail(f"lp_service_unavailable: {exc}")
    try:
        result = await add_liquidity(
            ctx.db,
            guild_id=int(ctx.guild_id),
            user_id=int(ctx.user_id),
            token_a=str(args["token_a"]),
            token_b=str(args["token_b"]),
            amount_a=float(args["amount_a"]),
            amount_b=float(args.get("amount_b") or 0.0),
        )
    except Exception as exc:
        return ToolResult.fail(f"add_liquidity_crashed: {exc}")
    if not result.success:
        return ToolResult.fail(result.error or "lp_rejected")
    return ToolResult.success({
        "pool_id":   getattr(result, "pool_id", None),
        "lp_minted": float(getattr(result, "lp_minted", 0.0) or 0.0),
        "amount_a":  float(getattr(result, "amount_a", 0.0) or 0.0),
        "amount_b":  float(getattr(result, "amount_b", 0.0) or 0.0),
        "tx_hash":   getattr(result, "tx_hash", None),
    })


@tool(
    name="defi.remove_liquidity",
    summary="Remove LP position from an AMM pool. Requires approval.",
    risk=RiskLevel.MUTATE,
    category="defi",
    params=[
        ParamSpec("token_a", "symbol"),
        ParamSpec("token_b", "symbol"),
        ParamSpec("lp_amount", "float", min=0.0,
                  description="LP token amount to burn."),
    ],
)
async def _remove_liquidity(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        from services.liquidity import remove_liquidity
    except Exception as exc:
        return ToolResult.fail(f"lp_service_unavailable: {exc}")
    try:
        result = await remove_liquidity(
            ctx.db,
            guild_id=int(ctx.guild_id),
            user_id=int(ctx.user_id),
            token_a=str(args["token_a"]),
            token_b=str(args["token_b"]),
            lp_amount=float(args["lp_amount"]),
        )
    except Exception as exc:
        return ToolResult.fail(f"remove_liquidity_crashed: {exc}")
    if not result.success:
        return ToolResult.fail(result.error or "lp_rejected")
    return ToolResult.success({
        "amount_a_out": float(getattr(result, "amount_a", 0.0) or 0.0),
        "amount_b_out": float(getattr(result, "amount_b", 0.0) or 0.0),
        "lp_burned":    float(getattr(result, "lp_burned", 0.0) or 0.0),
        "tx_hash":      getattr(result, "tx_hash", None),
    })


# ── Staking ──────────────────────────────────────────────────────────────────

@tool(
    name="defi.validators_list",
    summary="List active validators (id, network, name, stake token).",
    risk=RiskLevel.READ,
    category="defi",
    params=[
        ParamSpec("network", "str", required=False, default=None,
                  description="Filter by network name (e.g. 'Arcadia Network')."),
        ParamSpec("limit", "int", required=False, default=25, min=1, max=100),
    ],
)
async def _validators_list(ctx: ToolContext, args: dict) -> ToolResult:
    clauses = ["guild_id = $1"]
    params: list = [int(ctx.guild_id)]
    net = args.get("network")
    if net:
        params.append(str(net))
        clauses.append(f"network = ${len(params)}")
    params.append(int(args.get("limit") or 25))
    query = (
        "SELECT validator_id, name, network, commission_rate "
        "FROM validators WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY name ASC LIMIT ${len(params)}"
    )
    try:
        rows = await ctx.db.fetch_all(query, *params)
    except Exception as exc:
        return ToolResult.fail(f"db_error: {exc}")
    items = [
        {
            "id":         r.get("validator_id"),
            "name":       r.get("name"),
            "network":    r.get("network"),
            "commission": float(r.get("commission_rate") or 0.0),
        }
        for r in rows
    ]
    return ToolResult.success({"validators": items, "count": len(items)})


@tool(
    name="defi.stake",
    summary="Stake tokens on a validator. Requires approval.",
    risk=RiskLevel.MUTATE,
    category="defi",
    params=[
        ParamSpec("validator_id", "str", description="Validator id."),
        ParamSpec("amount", "float", min=0.0),
    ],
)
async def _stake(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        from services.stake import execute_stake
    except Exception as exc:
        return ToolResult.fail(f"stake_service_unavailable: {exc}")
    try:
        result = await execute_stake(
            ctx.db,
            guild_id=int(ctx.guild_id),
            user_id=int(ctx.user_id),
            validator_id=str(args["validator_id"]),
            amount=float(args["amount"]),
        )
    except Exception as exc:
        return ToolResult.fail(f"stake_crashed: {exc}")
    if not result.success:
        return ToolResult.fail(result.error or "stake_rejected")
    return ToolResult.success({
        "validator_id":   str(args["validator_id"]),
        "validator_name": getattr(result, "validator_name", ""),
        "amount":         float(getattr(result, "amount", 0.0) or 0.0),
        "symbol":         getattr(result, "symbol", ""),
        "tx_hash":        getattr(result, "tx_hash", None),
    })


@tool(
    name="defi.unstake",
    summary="Unstake tokens from a validator. Requires approval.",
    risk=RiskLevel.MUTATE,
    category="defi",
    params=[
        ParamSpec("validator_id", "str", description="Validator id."),
        ParamSpec("amount", "float", min=0.0),
    ],
)
async def _unstake(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        from services.stake import execute_unstake
    except Exception as exc:
        return ToolResult.fail(f"stake_service_unavailable: {exc}")
    try:
        result = await execute_unstake(
            ctx.db,
            guild_id=int(ctx.guild_id),
            user_id=int(ctx.user_id),
            validator_id=str(args["validator_id"]),
            amount=float(args["amount"]),
        )
    except Exception as exc:
        return ToolResult.fail(f"unstake_crashed: {exc}")
    if not result.success:
        return ToolResult.fail(result.error or "unstake_rejected")
    return ToolResult.success({
        "validator_id": str(args["validator_id"]),
        "amount":       float(getattr(result, "amount", 0.0) or 0.0),
        "symbol":       getattr(result, "symbol", ""),
        "tx_hash":      getattr(result, "tx_hash", None),
    })
