"""
core/framework/agent_tools/tools/earn.py -- earn system tools.

    earn.status     cooldown state and current job info (READ).
    earn.work       execute a work session and earn USD (MUTATE).
    earn.daily      claim daily reward with streak bonus (MUTATE).
    earn.promote    promote to next job tier if requirements met (MUTATE).
"""
from __future__ import annotations

import logging
import random
import time

from core.config import Config
from core.framework.scale import to_human, to_raw

from ..core import RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.earn")

_WORK_CD_MIN = 60

_STREAK_WORK_TIERS = [
    (90,  0.70),
    (60,  0.80),
    (30,  0.85),
    (14,  0.90),
    (7,   0.95),
]

_48H = 172800


def _streak_work_mult(streak: int) -> float:
    for threshold, mult in _STREAK_WORK_TIERS:
        if streak >= threshold:
            return mult
    return 1.0


def _next_job_id(current: str) -> str | None:
    order = Config.JOB_ORDER
    idx = order.index(current) if current in order else 0
    if idx + 1 < len(order):
        return order[idx + 1]
    return None


# -- earn.status ---------------------------------------------------------------

@tool(
    name="earn.status",
    summary=(
        "Return the caller's earn status: current job, work/daily cooldown "
        "remaining, daily streak, estimated work earnings, and next-tier "
        "promotion requirements. READ-only."
    ),
    risk=RiskLevel.READ,
    category="earn",
    params=[],
)
async def earn_status(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)

    row = await ctx.db.get_user(uid, gid)
    if not row:
        return ToolResult.fail("user_not_found")

    job = await ctx.db.get_user_job(uid, gid)
    job_id = job.get("job_id", "HOMELESS") if job else "HOMELESS"
    job_cfg = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])

    now = time.time()

    # Work cooldown
    _lw = row.get("last_work")
    last_work = _lw.timestamp() if hasattr(_lw, "timestamp") else float(_lw or 0)
    work_cd = job_cfg.get("work_cooldown", Config.WORK_COOLDOWN)
    streak = int(row.get("daily_streak") or 0)
    streak_mult = _streak_work_mult(streak)
    if streak_mult < 1.0:
        work_cd = max(_WORK_CD_MIN, int(work_cd * streak_mult))
    work_remaining = max(0.0, work_cd - (now - last_work))

    # Daily cooldown
    _ld = row.get("last_daily")
    last_daily = _ld.timestamp() if hasattr(_ld, "timestamp") else float(_ld or 0)
    daily_remaining = max(0.0, Config.DAILY_COOLDOWN - (now - last_daily))

    # Earn range
    earn_min = to_human(job_cfg["earn"][0])
    earn_max = to_human(job_cfg["earn"][1])

    # Next job
    next_id = _next_job_id(job_id)
    next_req = None
    if next_id:
        ncfg = Config.JOBS[next_id]
        work_count = int(job.get("work_count", 0)) if job else 0
        from services.net_worth import compute_net_worth
        nw = await compute_net_worth(uid, gid, ctx.db)
        next_req = {
            "job_id": next_id,
            "title": ncfg["title"],
            "min_work_sessions": ncfg["min_work"],
            "work_sessions_done": work_count,
            "work_sessions_needed": max(0, ncfg["min_work"] - work_count),
            "min_net_worth_usd": ncfg["min_wealth"],
            "current_net_worth_usd": round(nw.total, 2),
            "net_worth_needed": round(max(0.0, ncfg["min_wealth"] - nw.total), 2),
        }

    return ToolResult.success({
        "job_id": job_id,
        "job_title": job_cfg["title"],
        "earn_range": {"min_usd": round(earn_min, 2), "max_usd": round(earn_max, 2)},
        "work_cooldown_secs": int(work_remaining),
        "work_ready": work_remaining == 0.0,
        "daily_cooldown_secs": int(daily_remaining),
        "daily_ready": daily_remaining == 0.0,
        "daily_streak": streak,
        "streak_cd_reduction_pct": round((1.0 - streak_mult) * 100, 1),
        "next_promotion": next_req,
    })


# -- earn.work -----------------------------------------------------------------

@tool(
    name="earn.work",
    summary=(
        "Execute a work session for the caller and earn USD. Respects the "
        "per-job cooldown and streak-based reduction. Applies guild work "
        "multiplier. Returns amount earned and new wallet balance."
    ),
    risk=RiskLevel.MUTATE,
    category="earn",
    params=[],
)
async def earn_work(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)

    row = await ctx.db.get_user(uid, gid)
    if not row:
        return ToolResult.fail("user_not_found")

    job = await ctx.db.get_user_job(uid, gid)
    job_id = job.get("job_id", "HOMELESS") if job else "HOMELESS"
    job_cfg = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])

    # Cooldown check
    now = time.time()
    _lw = row.get("last_work")
    last_work = _lw.timestamp() if hasattr(_lw, "timestamp") else float(_lw or 0)
    work_cd = job_cfg.get("work_cooldown", Config.WORK_COOLDOWN)
    streak = int(row.get("daily_streak") or 0)
    streak_mult = _streak_work_mult(streak)
    if streak_mult < 1.0:
        work_cd = max(_WORK_CD_MIN, int(work_cd * streak_mult))

    elapsed = now - last_work
    if elapsed < work_cd:
        remaining = int(work_cd - elapsed)
        return ToolResult.fail(
            f"cooldown: {remaining}s remaining "
            f"({remaining // 60}m {remaining % 60}s)"
        )

    # Base amount
    earn_min = to_human(job_cfg["earn"][0])
    earn_max = to_human(job_cfg["earn"][1])
    amount = round(random.uniform(earn_min, earn_max), 2)

    # Guild work multiplier
    try:
        settings = await ctx.db.get_guild_settings(gid)
        mult = float(settings.get("work_multiplier") or 1.0)
        if mult != 1.0:
            amount = round(amount * mult, 2)
    except Exception:
        pass

    # Set cooldown immediately then apply wallet delta
    await ctx.db.set_cooldown(uid, gid, "last_work")
    new_wallet_raw = await ctx.db.update_wallet(uid, gid, to_raw(amount))

    # Log tx
    try:
        tx_hash = await ctx.db.log_tx(
            gid, uid, "WORK",
            symbol_out="USD", amount_out=to_raw(amount),
            network="usd",
        )
    except Exception:
        tx_hash = ""

    # Publish bus event for feed
    if ctx.bus:
        try:
            await ctx.bus.publish(
                "work_completed",
                guild_id=gid, user_id=uid, amount=amount, tx_hash=tx_hash,
            )
        except Exception:
            pass

    new_wallet = to_human(int(new_wallet_raw)) if new_wallet_raw is not None else None
    return ToolResult.success({
        "earned_usd": amount,
        "job_title": job_cfg["title"],
        "new_wallet_usd": round(new_wallet, 2) if new_wallet is not None else None,
        "tx_hash": tx_hash,
        "next_work_in_secs": work_cd,
    })


# -- earn.daily ----------------------------------------------------------------

@tool(
    name="earn.daily",
    summary=(
        "Claim the caller's daily reward. Applies streak bonuses, guild daily "
        "multiplier, and job daily_bonus perk. Returns amount earned, new "
        "streak count, and new wallet balance."
    ),
    risk=RiskLevel.MUTATE,
    category="earn",
    params=[],
)
async def earn_daily(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)

    row = await ctx.db.get_user(uid, gid)
    if not row:
        return ToolResult.fail("user_not_found")

    now = time.time()
    _ld = row.get("last_daily")
    last_daily = _ld.timestamp() if hasattr(_ld, "timestamp") else float(_ld or 0)
    elapsed = now - last_daily

    if elapsed < Config.DAILY_COOLDOWN:
        remaining = int(Config.DAILY_COOLDOWN - elapsed)
        h, rem = divmod(remaining, 3600)
        m = rem // 60
        return ToolResult.fail(f"cooldown: {h}h {m}m remaining")

    # Set cooldown immediately
    await ctx.db.set_cooldown(uid, gid, "last_daily")

    # Streak logic
    old_streak = int(row.get("daily_streak") or 0)
    if elapsed < _48H:
        new_streak = min(old_streak + 1, Config.DAILY_MAX_STREAK)
    else:
        new_streak = 1

    base_reward = (
        to_human(Config.DAILY_AMOUNT)
        + (new_streak - 1) * to_human(Config.DAILY_STREAK_BONUS)
    )

    # Guild multiplier
    try:
        settings = await ctx.db.get_guild_settings(gid)
        dmult = float(settings.get("daily_multiplier") or 1.0)
        if dmult != 1.0:
            base_reward = round(base_reward * dmult, 2)
    except Exception:
        pass

    # Job daily_bonus perk
    job = await ctx.db.get_user_job(uid, gid)
    job_id = job.get("job_id", "HOMELESS") if job else "HOMELESS"
    job_cfg = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])
    daily_bonus = job_cfg.get("perks", {}).get("daily_bonus", 0.0)
    reward = round(base_reward * (1.0 + daily_bonus), 2)

    # Update wallet and streak
    new_wallet_raw = await ctx.db.update_wallet(uid, gid, to_raw(reward))
    import datetime as _dt
    await ctx.db.update_streak(uid, gid, new_streak, _dt.datetime.now(_dt.timezone.utc))

    try:
        tx_hash = await ctx.db.log_tx(
            gid, uid, "DAILY",
            symbol_out="USD", amount_out=to_raw(reward),
            network="usd",
        )
    except Exception:
        tx_hash = ""

    if ctx.bus:
        try:
            await ctx.bus.publish(
                "daily_claimed",
                guild_id=gid, user_id=uid, amount=reward,
                streak=new_streak, tx_hash=tx_hash,
            )
        except Exception:
            pass

    new_wallet = to_human(int(new_wallet_raw)) if new_wallet_raw is not None else None
    return ToolResult.success({
        "earned_usd": reward,
        "new_streak": new_streak,
        "streak_bonus_pct": round((new_streak - 1) * to_human(Config.DAILY_STREAK_BONUS) / max(to_human(Config.DAILY_AMOUNT), 1) * 100, 1),
        "new_wallet_usd": round(new_wallet, 2) if new_wallet is not None else None,
        "tx_hash": tx_hash,
    })


# -- earn.promote --------------------------------------------------------------

@tool(
    name="earn.promote",
    summary=(
        "Promote the caller to the next job tier if all requirements are met "
        "(minimum work sessions + minimum net worth). Returns new job title, "
        "earn range, and perks unlocked. Fails with the gap if not ready."
    ),
    risk=RiskLevel.MUTATE,
    category="earn",
    params=[],
)
async def earn_promote(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)

    job = await ctx.db.get_user_job(uid, gid)
    if not job:
        return ToolResult.fail("user_not_found")

    job_id = job.get("job_id", "HOMELESS")
    next_id = _next_job_id(job_id)
    if not next_id:
        return ToolResult.fail("already_max_tier: already at the top job tier (Exploiter)")

    next_cfg = Config.JOBS[next_id]
    work_count = int(job.get("work_count", 0))

    if work_count < next_cfg["min_work"]:
        needed = next_cfg["min_work"] - work_count
        return ToolResult.fail(
            f"not_enough_work_sessions: need {needed} more "
            f"(have {work_count}, need {next_cfg['min_work']})"
        )

    from services.net_worth import compute_net_worth
    nw = await compute_net_worth(uid, gid, ctx.db)
    if nw.total < next_cfg["min_wealth"]:
        needed = next_cfg["min_wealth"] - nw.total
        return ToolResult.fail(
            f"insufficient_net_worth: need ${needed:,.2f} more "
            f"(have ${nw.total:,.2f}, need ${next_cfg['min_wealth']:,.2f})"
        )

    old_cfg = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])
    await ctx.db.update_job(uid, gid, next_id, work_count, int(job.get("total_earned", 0)))

    if ctx.bus:
        try:
            await ctx.bus.publish(
                "promoted",
                guild_id=gid, user_id=uid,
                old_job=job_id, new_job=next_id,
            )
        except Exception:
            pass

    new_perks = next_cfg.get("perks", {})
    return ToolResult.success({
        "promoted_from": {"job_id": job_id, "title": old_cfg["title"]},
        "promoted_to": {
            "job_id": next_id,
            "title": next_cfg["title"],
            "earn_range": {
                "min_usd": round(to_human(next_cfg["earn"][0]), 2),
                "max_usd": round(to_human(next_cfg["earn"][1]), 2),
            },
        },
        "perks": {k: v for k, v in new_perks.items()},
    })
