"""
core/framework/agent_tools/tools/staking.py -- staking + validator read tool.

    staking.summary   caller's complete staking footprint: NPC validator
                      stakes, PoS validator (self-owned), and delegations
                      (stake on someone else's PoS validator).

READ-only. Reuses ``db.get_user_stakes``, ``db.get_user_pos_validators``,
and ``db.get_user_delegations`` so the AI can answer "how am I staking"
in a single call.
"""
from __future__ import annotations

import logging

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.staking")


@tool(
    name="staking.summary",
    summary=(
        "Return the caller's complete staking footprint: NPC validator "
        "stakes, self-owned PoS validators, and delegations made to other "
        "validators."
    ),
    risk=RiskLevel.READ,
    category="staking",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def summary(ctx: ToolContext, args: dict) -> ToolResult:
    target = int(args.get("target_id") or ctx.user_id)
    gid = int(ctx.guild_id)

    # ── NPC validator stakes ──────────────────────────────────────────────
    npc_stakes: list[dict] = []
    try:
        rows = await ctx.db.get_user_stakes(target, gid)
    except Exception as exc:
        log.warning("[staking.summary] NPC stakes failed: %s", exc)
        rows = []
    for r in rows or []:
        amt = float(r.get("amount") or 0.0)
        if amt <= 0:
            continue
        npc_stakes.append({
            "validator_id":  str(r.get("validator_id") or ""),
            "name":          str(r.get("name") or ""),
            "emoji":         str(r.get("emoji") or ""),
            "network":       str(r.get("network") or ""),
            "symbol":        str(r.get("symbol") or ""),
            "amount":        round(amt, 8),
            "reward_rate":   float(r.get("reward_rate") or 0.0),
            "uptime_rate":   float(r.get("uptime_rate") or 0.0),
            "slash_rate":    float(r.get("slash_rate") or 0.0),
        })

    # ── PoS validators (self-owned) ───────────────────────────────────────
    pos_validators: list[dict] = []
    try:
        rows = await ctx.db.get_user_pos_validators(target, gid)
    except Exception as exc:
        log.warning("[staking.summary] PoS validators failed: %s", exc)
        rows = []
    for r in rows or []:
        pos_validators.append({
            "network":         str(r.get("network") or ""),
            "stake_token":     str(r.get("stake_token") or ""),
            "stake_amount":    float(r.get("stake_amount") or 0.0),
            "commission_rate": float(r.get("commission_rate") or 0.0),
            "is_active":       bool(r.get("is_active")),
            "slash_count":     int(r.get("slash_count") or 0),
            "total_blocks":    int(r.get("total_blocks_validated") or 0),
            "total_rewards":   float(r.get("total_rewards_earned") or 0.0),
        })

    # ── Delegations (staking someone else's validator) ───────────────────
    delegations: list[dict] = []
    try:
        rows = await ctx.db.get_user_delegations(target, gid)
    except Exception as exc:
        log.warning("[staking.summary] delegations failed: %s", exc)
        rows = []
    for r in rows or []:
        amt = float(r.get("amount") or 0.0)
        if amt <= 0:
            continue
        delegations.append({
            "validator_user_id": int(r.get("validator_user_id") or 0),
            "network":           str(r.get("network") or ""),
            "token":              str(r.get("token") or ""),
            "amount":            round(amt, 8),
            "locked_until":      r.get("locked_until"),
            "session_earned":    float(r.get("session_earned") or 0.0),
        })

    return ToolResult.success({
        "target_id":           target,
        "npc_stake_count":     len(npc_stakes),
        "npc_stakes":          npc_stakes,
        "pos_validator_count": len(pos_validators),
        "pos_validators":      pos_validators,
        "delegation_count":    len(delegations),
        "delegations":         delegations,
    })
