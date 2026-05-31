"""
core/framework/agent_tools/tools/wallet.py -- wallet + transaction simulator tools.

Single powerful tool per capability, not one-per-field:

    wallet.portfolio           summarise a user's whole net worth breakdown.
    wallet.simulate_swap       preview an AMM swap without touching state.
    wallet.simulate_transfer   preview a token transfer (fees, net, burn).

All three are READ-only. They never touch the economy.
"""
from __future__ import annotations

import logging

from core.config import Config
from core.framework.scale import to_human

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.wallet")


# ── wallet.portfolio ──────────────────────────────────────────────────────────

@tool(
    name="wallet.portfolio",
    summary=(
        "Return a full portfolio snapshot for the caller (or another player): "
        "net worth, wallet + bank, token holdings, staked positions, loans."
    ),
    risk=RiskLevel.READ,
    category="wallet",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def portfolio(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        from services.net_worth import compute_net_worth
    except Exception as exc:
        return ToolResult.fail(f"net_worth service unavailable: {exc}")

    target_id = int(args.get("target_id") or ctx.user_id)
    try:
        nw = await compute_net_worth(target_id, ctx.guild_id, ctx.db)
    except Exception as exc:
        log.warning("[wallet.portfolio] compute failed: %s", exc)
        return ToolResult.fail(f"portfolio_compute_failed: {exc}")

    price_rows = await ctx.db.fetch_all(
        "SELECT symbol, price FROM crypto_prices WHERE guild_id=$1",
        int(ctx.guild_id),
    )
    prices = {r["symbol"]: float(r["price"]) for r in price_rows}

    # Aggregate all token balances across every source the net_worth service
    # already resolved: CeFi holdings, DeFi wallet_holdings (which hold
    # deployed group tokens like CAT), LP positions, and NPC yield stakes.
    # Previously this tool only queried `crypto_holdings`, which is CeFi only,
    # so the AI happily told players they had 0 of their group token even
    # when they held millions in DeFi or LP.
    aggregates: dict[str, dict[str, float]] = {}

    def _bump(sym: str, field: str, amt: float) -> None:
        if not sym or amt <= 0:
            return
        row = aggregates.setdefault(sym, {
            "total": 0.0, "cefi": 0.0, "defi": 0.0, "lp": 0.0, "staked": 0.0,
        })
        row[field] += amt
        row["total"] += amt

    for h in nw.holdings:
        _bump(str(h.get("symbol") or ""), "cefi", to_human(int(h.get("amount") or 0)))
    for wh in nw.wallet_holdings:
        _bump(str(wh.get("symbol") or ""), "defi", to_human(int(wh.get("amount") or 0)))
    for lp in nw.lp_positions:
        _bump(str(lp.get("token_a") or ""), "lp", float(lp.get("amount_a") or 0))
        _bump(str(lp.get("token_b") or ""), "lp", float(lp.get("amount_b") or 0))
    for s in nw.stakes:
        _bump(str(s.get("symbol") or ""), "staked", to_human(int(s.get("amount") or 0)))

    all_holdings: list[dict] = []
    for sym, row in aggregates.items():
        price = float(prices.get(sym, 0.0))
        all_holdings.append({
            "symbol": sym,
            "amount": round(row["total"], 8),
            "usd_value": round(row["total"] * price, 2),
            "cefi": round(row["cefi"], 8),
            "defi": round(row["defi"], 8),
            "lp": round(row["lp"], 8),
            "staked": round(row["staked"], 8),
        })
    all_holdings.sort(key=lambda h: h["usd_value"], reverse=True)

    data = {
        "target_id": target_id,
        "net_worth_usd": round(nw.total, 2),
        "components": {
            "wallet": round(nw.wallet, 2),
            "bank": round(nw.bank, 2),
            "cefi_crypto": round(nw.cefi_crypto, 2),
            "defi_wallet": round(nw.defi_wallet, 2),
            "stake_value": round(nw.stake_value, 2),
            "pos_stake_value": round(nw.pos_stake_value, 2),
            "moon_stake_value": round(nw.moon_stake_value, 2),
            "moon_pool_stake_value": round(nw.moon_pool_stake_value, 2),
            "lp_value": round(nw.lp_value, 2),
            "rig_value": round(nw.rig_value, 2),
            "delegation_value": round(nw.delegation_value, 2),
            "savings_value": round(nw.savings_value, 2),
            "items_value": round(nw.items_value, 2),
            "nft_value": round(nw.nft_value, 2),
            "loan_liability": round(nw.loan_liability, 2),
        },
        "holdings": all_holdings,
        "top_holdings": all_holdings[:5],
    }
    return ToolResult.success(data)


# ── wallet.simulate_swap ─────────────────────────────────────────────────────

@tool(
    name="wallet.simulate_swap",
    summary=(
        "Simulate an AMM swap against the live pool state. Returns the "
        "exact quote (output, price impact, fees, gas, network) the real "
        "swap pipeline would produce. Does not move any funds."
    ),
    risk=RiskLevel.READ,
    category="wallet",
    params=[
        ParamSpec("from_symbol", "symbol", description="Input token (e.g. ARC)"),
        ParamSpec("to_symbol", "symbol", description="Output token (e.g. USDC)"),
        ParamSpec("amount_in", "float", min=0.0,
                  description="Input amount in human units (not raw)."),
    ],
)
async def simulate_swap(ctx: ToolContext, args: dict) -> ToolResult:
    src = args["from_symbol"]
    dst = args["to_symbol"]
    amt_in = float(args["amount_in"])

    if src == dst:
        return ToolResult.fail("from_symbol and to_symbol must differ")
    if amt_in <= 0:
        return ToolResult.fail("amount_in must be positive")

    # Delegate to the canonical quoter so the simulator never drifts from
    # the real swap pipeline (fees, network gating, halts, dynamic max-swap
    # fraction, low-liquidity handling, slippage warnings, gas computation).
    try:
        from services.swap import compute_swap_quote, SwapQuote
    except Exception as exc:
        return ToolResult.fail(f"swap_service_unavailable: {exc}")

    quote = await compute_swap_quote(
        ctx.db,
        int(ctx.guild_id),
        int(ctx.user_id),
        src,
        dst,
        amt_in,
        gas_price="medium",
        min_amount_out=0.0,
    )
    if not isinstance(quote, SwapQuote):
        # compute_swap_quote returns a string error message on rejection.
        return ToolResult.fail(str(quote))

    return ToolResult.success({
        "pool": {
            "pool_id": str(quote.pool_id or ""),
            "token_in": quote.token_in,
            "token_out": quote.token_out,
            "reserve_in": quote.reserve_in,
            "reserve_out": quote.reserve_out,
        },
        "amount_in": quote.amount_in,
        "amount_out": round(quote.amount_out, 8),
        "fee_rate": quote.fee,
        "fee_paid": round(quote.fee_amount, 8),
        "mid_price": round(quote.spot_price, 8),
        "exec_price": round(quote.exec_price, 8),
        "price_impact_pct": round(quote.price_impact * 100.0, 4),
        "warnings": list(quote.warnings or []),
        "network": quote.network,
        "use_mempool": quote.use_mempool,
        "gas_coin": quote.gas_coin,
        "gas_fee": round(quote.gas_fee, 8),
        "platform_fee": round(quote.platform_fee, 8),
        "total_gas_cost": round(quote.total_gas_cost, 8),
        "swap_usd_value": round(quote.swap_usd_value, 2),
    })


# ── wallet.simulate_transfer ─────────────────────────────────────────────────

@tool(
    name="wallet.simulate_transfer",
    summary=(
        "Simulate sending a token between wallets: fees, burn, and net "
        "received by the recipient. Does not touch any state."
    ),
    risk=RiskLevel.READ,
    category="wallet",
    params=[
        ParamSpec("symbol", "symbol", description="Token symbol (e.g. ARC, DSC)"),
        ParamSpec("amount", "float", min=0.0,
                  description="Human-unit amount to send."),
    ],
)
async def simulate_transfer(ctx: ToolContext, args: dict) -> ToolResult:
    sym = args["symbol"]
    amt = float(args["amount"])
    if amt <= 0:
        return ToolResult.fail("amount must be positive")

    tok = Config.TOKENS.get(sym)
    if tok is None:
        return ToolResult.fail(f"unknown_token: {sym}")

    fee_rate = float(tok.get("tx_fee_rate", 0.0))
    burn_rate = float(tok.get("burn_rate", 0.0))
    gas_fee_usd = float(tok.get("gas_fee", 0.0))

    fee_amt = amt * fee_rate
    burn_amt = amt * burn_rate
    net_received = amt - fee_amt - burn_amt

    return ToolResult.success({
        "symbol": sym,
        "amount_sent": amt,
        "tx_fee": round(fee_amt, 12),
        "tx_fee_rate": fee_rate,
        "burned": round(burn_amt, 12),
        "burn_rate": burn_rate,
        "gas_fee_usd": gas_fee_usd,
        "net_received": round(net_received, 12),
    })
