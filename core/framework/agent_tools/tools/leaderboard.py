"""
core/framework/agent_tools/tools/leaderboard.py -- net-worth leaderboard read tools.

    leaderboard.top    top N players by total net worth in the current guild.
    leaderboard.rank   caller's (or another player's) rank + net worth.

Both are READ and route through ``services.net_worth.compute_bulk_net_worth``
so the numbers always match the displays in ``cogs/bank.py`` and similar.
"""
from __future__ import annotations

import logging

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.leaderboard")


_MAX_TOP = 25


async def _bulk_net_worth(ctx: ToolContext) -> dict[int, float]:
    try:
        from services.net_worth import compute_bulk_net_worth
    except Exception as exc:
        raise RuntimeError(f"net_worth service unavailable: {exc}") from exc
    return await compute_bulk_net_worth(int(ctx.guild_id), ctx.db)


@tool(
    name="leaderboard.top",
    summary=(
        "Return the top N players by total net worth in the current "
        "guild. Uses the single canonical compute_bulk_net_worth service "
        "so it matches every other leaderboard display."
    ),
    risk=RiskLevel.READ,
    category="leaderboard",
    params=[
        ParamSpec("limit", "int", required=False, default=10, min=1, max=_MAX_TOP,
                  description=f"Number of top players to return (1..{_MAX_TOP})."),
    ],
)
async def top(ctx: ToolContext, args: dict) -> ToolResult:
    limit = int(args.get("limit") or 10)
    limit = max(1, min(_MAX_TOP, limit))

    try:
        bulk = await _bulk_net_worth(ctx)
    except Exception as exc:
        return ToolResult.fail(str(exc))

    ranked = sorted(bulk.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return ToolResult.success({
        "limit":     limit,
        "player_count": len(bulk),
        "top": [
            {"user_id": uid, "net_worth_usd": round(v, 2), "rank": i + 1}
            for i, (uid, v) in enumerate(ranked)
        ],
    })


@tool(
    name="leaderboard.rank",
    summary=(
        "Return a player's current rank and net worth in the guild "
        "leaderboard. Defaults to the caller."
    ),
    risk=RiskLevel.READ,
    category="leaderboard",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def rank(ctx: ToolContext, args: dict) -> ToolResult:
    target = int(args.get("target_id") or ctx.user_id)
    try:
        bulk = await _bulk_net_worth(ctx)
    except Exception as exc:
        return ToolResult.fail(str(exc))

    ranked = sorted(bulk.items(), key=lambda kv: kv[1], reverse=True)
    my_rank = 0
    my_net_worth = float(bulk.get(target, 0.0))
    for i, (uid, _val) in enumerate(ranked, start=1):
        if uid == target:
            my_rank = i
            break

    return ToolResult.success({
        "target_id":      target,
        "rank":           my_rank,
        "net_worth_usd":  round(my_net_worth, 2),
        "player_count":   len(bulk),
    })
