"""
core/framework/agent_tools/tools/eat.py -- Eat the Rich read tools.

    eat.stats     caller's (or target's) class-war record: eats, devoured,
                  lost, win rate (READ).
    eat.history   last 10 eats in this guild (READ).
    eat.status    caller's private-security-detail state (READ).
"""
from __future__ import annotations

import logging
import time

from core.config import Config
from core.framework.scale import to_human

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.eat")


# -- eat.stats -----------------------------------------------------------------

@tool(
    name="eat.stats",
    summary=(
        "Return Eat the Rich stats for the caller or another player: eat "
        "attempts, successful eats, total devoured/lost, and win rate."
    ),
    risk=RiskLevel.READ,
    category="eat",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id to look up. Defaults to the caller."),
    ],
)
async def eat_stats(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(args.get("target_id") or ctx.user_id)
    gid = int(ctx.guild_id)

    row = await ctx.db.fetch_one(
        "SELECT * FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
        uid, gid,
    )

    if not row:
        return ToolResult.success({
            "target_id": uid,
            "eats_attempted": 0,
            "eats_won": 0,
            "win_rate_pct": None,
            "total_devoured_usd": 0.0,
            "total_lost_usd": 0.0,
            "net_usd": 0.0,
            "times_hunted": 0,
            "times_survived": 0,
        })

    attempted = int(row["heists_attempted"])
    won = int(row["heists_won"])
    devoured = round(row.h("total_stolen"), 2)
    targeted = int(row.get("times_targeted") or 0)
    survived = int(row.get("times_defended") or 0)
    lost = round(row.h("total_lost"), 2)
    win_rate = round(won / attempted * 100, 1) if attempted > 0 else None

    return ToolResult.success({
        "target_id": uid,
        "eats_attempted": attempted,
        "eats_won": won,
        "win_rate_pct": win_rate,
        "total_devoured_usd": devoured,
        "total_lost_usd": lost,
        "net_usd": round(devoured - lost, 2),
        "times_hunted": targeted,
        "times_survived": survived,
        "tactics": {
            tid: {"success_chance_pct": round(tcfg["success"] * 100, 0), "label": tcfg["label"]}
            for tid, tcfg in Config.EAT_TACTICS.items()
        },
    })


# -- eat.history ---------------------------------------------------------------

@tool(
    name="eat.history",
    summary=(
        "Return the last 10 eats in this server: eater, target, tactic, "
        "outcome (won/got-away/security-blocked), and amount devoured."
    ),
    risk=RiskLevel.READ,
    category="eat",
    params=[],
)
async def eat_history(ctx: ToolContext, args: dict) -> ToolResult:
    gid = int(ctx.guild_id)
    try:
        rows = await ctx.db.fetch_all(
            "SELECT * FROM exploit_history WHERE guild_id=$1 ORDER BY created_at DESC LIMIT 10",
            gid,
        )
    except Exception as exc:
        log.warning("[eat.history] db error: %s", exc)
        return ToolResult.fail(f"db_error: {exc}")

    events = []
    for r in rows:
        devoured = round(r.h("stolen"), 2) if r.get("won") else 0.0
        events.append({
            "eater_id": r.get("attacker_id"),
            "target_id": r.get("target_id"),
            "tactic": r.get("tier"),
            "won": bool(r.get("won")),
            "security_blocked": bool(r.get("shielded")),
            "devoured_usd": devoured,
            "created_at": r.get("created_at"),
        })

    return ToolResult.success({"eats": events, "total": len(events)})


# -- eat.status ----------------------------------------------------------------

@tool(
    name="eat.status",
    summary=(
        "Return the caller's security detail, powerup chain, and salad bowl "
        "state. Security: whether a detail is on duty and its remaining time. "
        "Powerups: prep and cook state (none/charging/armed) and seconds until "
        "each is ready. Bowl: total USD value in the shared salad bowl."
    ),
    risk=RiskLevel.READ,
    category="eat",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def eat_status(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(args.get("target_id") or ctx.user_id)
    gid = int(ctx.guild_id)

    shield_row = await ctx.db.fetch_one(
        "SELECT * FROM exploit_shields WHERE user_id=$1 AND guild_id=$2",
        uid, gid,
    )

    security_active = False
    security_secs_remaining = 0
    security_cooldown_secs = 0
    now = time.time()

    if shield_row:
        active_until = shield_row.get("active_until")
        if active_until:
            exp = active_until.timestamp() if hasattr(active_until, "timestamp") else float(active_until)
            if exp > now:
                security_active = True
                security_secs_remaining = int(exp - now)

        if not security_active:
            last_used = shield_row.get("last_used_at")
            if last_used:
                lu_ts = last_used.timestamp() if hasattr(last_used, "timestamp") else float(last_used)
                cd_remaining = Config.EAT_FORTIFY_COOLDOWN - (now - lu_ts)
                security_cooldown_secs = max(0, int(cd_remaining))

    # Powerup chain state (DB-side time comparison).
    pw_row = await ctx.db.fetch_one(
        "SELECT "
        "  CASE WHEN prep_ready_at IS NULL THEN 'none' "
        "       WHEN prep_ready_at > now() THEN 'charging' "
        "       ELSE 'armed' END AS prep_state, "
        "  GREATEST(0, EXTRACT(EPOCH FROM (prep_ready_at - now()))::int) AS prep_secs, "
        "  CASE WHEN cook_ready_at IS NULL THEN 'none' "
        "       WHEN cook_ready_at > now() THEN 'charging' "
        "       ELSE 'armed' END AS cook_state, "
        "  GREATEST(0, EXTRACT(EPOCH FROM (cook_ready_at - now()))::int) AS cook_secs "
        "FROM exploit_stats WHERE user_id=$1 AND guild_id=$2",
        uid, gid,
    )
    prep_state = pw_row["prep_state"] if pw_row else "none"
    prep_secs = int(pw_row["prep_secs"] or 0) if pw_row else 0
    cook_state = pw_row["cook_state"] if pw_row else "none"
    cook_secs = int(pw_row["cook_secs"] or 0) if pw_row else 0

    # Salad bowl total USD value.
    bowl_rows = await ctx.db.fetch_all(
        "SELECT symbol, amount FROM eat_salad_bowl WHERE guild_id=$1 AND amount > 0",
        gid,
    )
    bowl_usd = 0.0
    bowl_currencies: list[str] = []
    for r in bowl_rows:
        sym = r["symbol"]
        amt = to_human(int(r["amount"]))
        if sym == "USD":
            bowl_usd += amt
        else:
            price_row = await ctx.db.get_price(sym, gid)
            price = float(price_row["price"]) if price_row else 0.0
            bowl_usd += amt * price
        bowl_currencies.append(sym)

    return ToolResult.success({
        "target_id": uid,
        "security_active": security_active,
        "security_secs_remaining": security_secs_remaining,
        "security_cooldown_secs": security_cooldown_secs,
        "fortify_cost_usd": round(to_human(int(Config.EAT_FORTIFY_COST)), 2),
        "fortify_duration_hrs": Config.EAT_FORTIFY_DURATION // 3600,
        "prep_state": prep_state,
        "prep_secs_until_armed": prep_secs,
        "cook_state": cook_state,
        "cook_secs_until_armed": cook_secs,
        "bowl_total_usd": round(bowl_usd, 2),
        "bowl_currencies": bowl_currencies,
    })
