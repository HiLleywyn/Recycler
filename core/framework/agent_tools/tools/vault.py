"""
core/framework/agent_tools/tools/vault.py -- network vault read tool.

    vault.state   list every network vault in the current guild with
                  balance + current level (used for server progression).

READ-only. Delegates to ``db.get_all_vaults``.
"""
from __future__ import annotations

import logging

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.vault")


@tool(
    name="vault.state",
    summary=(
        "Return the server-wide network vault state: per-network balances "
        "and current level. Used to answer 'how far are we from the next "
        "server level' questions."
    ),
    risk=RiskLevel.READ,
    category="vault",
    params=[
        ParamSpec("network", "str", required=False, default=None,
                  description=(
                      "Optional filter (short key: 'sun', 'mta', 'arc', "
                      "'dsc'). Omit to return every vault."
                  )),
    ],
)
async def state(ctx: ToolContext, args: dict) -> ToolResult:
    net = str(args.get("network") or "").strip().lower() or None
    try:
        rows = await ctx.db.get_all_vaults(int(ctx.guild_id))
    except Exception as exc:
        log.warning("[vault.state] read failed: %s", exc)
        return ToolResult.fail(f"vault_read_failed: {exc}")

    vaults: list[dict] = []
    total_balance = 0.0
    for r in rows or []:
        short = str(r.get("network") or "").lower()
        if net and short != net:
            continue
        balance = float(r.get("balance") or 0.0)
        level   = int(r.get("level") or 0)
        total_balance += balance
        vaults.append({
            "network": short,
            "balance": round(balance, 4),
            "level":   level,
        })

    vaults.sort(key=lambda v: v["balance"], reverse=True)
    return ToolResult.success({
        "count":         len(vaults),
        "total_balance": round(total_balance, 4),
        "vaults":        vaults,
    })
