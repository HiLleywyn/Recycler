"""
core/framework/agent_tools/tools/history.py -- transaction history read tool.

    history.transactions   caller's recent on-chain/ledger tx history,
                           capped + lightly summarised.

READ-only. Delegates to ``db.get_user_tx_history`` so the AI can answer
"what did I just do" / "why is my wallet empty" without any bespoke
query code.
"""
from __future__ import annotations

import logging

from core.framework.scale import to_human

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.history")


_MAX_LIMIT = 50


@tool(
    name="history.transactions",
    summary=(
        "Return recent transactions (trades, transfers, faucet, work, "
        "etc.) for the caller (or another player). Each row is lightly "
        "summarised so the AI can describe the flow without getting lost "
        "in raw columns."
    ),
    risk=RiskLevel.READ,
    category="history",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
        ParamSpec("limit", "int", required=False, default=20, min=1, max=_MAX_LIMIT,
                  description=f"Max rows to return (1..{_MAX_LIMIT})."),
    ],
)
async def transactions(ctx: ToolContext, args: dict) -> ToolResult:
    target = int(args.get("target_id") or ctx.user_id)
    limit  = int(args.get("limit") or 20)
    limit  = max(1, min(_MAX_LIMIT, limit))

    try:
        rows = await ctx.db.get_user_tx_history(
            target, int(ctx.guild_id), limit,
        )
    except Exception as exc:
        log.warning("[history.transactions] read failed: %s", exc)
        return ToolResult.fail(f"history_read_failed: {exc}")

    items: list[dict] = []
    for r in rows or []:
        raw_amt = r.get("amount")
        try:
            amount = to_human(int(raw_amt or 0))
        except Exception:
            # Some tx rows carry human-scale floats already (legacy).
            try:
                amount = float(raw_amt or 0.0)
            except Exception:
                amount = 0.0
        items.append({
            "tx_hash":   str(r.get("tx_hash") or ""),
            "type":      str(r.get("type") or r.get("kind") or ""),
            "symbol":    str(r.get("symbol") or ""),
            "amount":    round(amount, 8),
            "network":   str(r.get("network") or ""),
            "ts":        r.get("ts"),
            "note":      str(r.get("note") or "")[:200],
        })

    return ToolResult.success({
        "target_id": target,
        "count":     len(items),
        "limit":     limit,
        "items":     items,
    })
