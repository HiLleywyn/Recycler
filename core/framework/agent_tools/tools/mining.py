"""
core/framework/agent_tools/tools/mining.py -- mining/PoW read tools.

    mining.status   caller's full mining dashboard: rigs, hashrate,
                    mode, estimated earnings, ROI (READ).
    mining.rigs     available rigs catalog + quantities owned (READ).
    mining.history  last 10 blocks mined in this guild (READ).
"""
from __future__ import annotations

import logging

from core.config import Config
from core.framework.scale import to_human

from ..core import ParamSpec, RiskLevel, ToolContext, ToolResult, tool

log = logging.getLogger("discoin.agent_tools.mining")

_TICK = 60  # seconds per mining tick (matches chain_group.py)


def _pow_current_reward(block_height: int, cfg: dict) -> float:
    halvings = block_height // cfg["halving_blocks"]
    return cfg["initial_reward"] / (2 ** halvings)


# -- mining.status -------------------------------------------------------------

@tool(
    name="mining.status",
    summary=(
        "Return the caller's mining dashboard: active rigs, hashrate per "
        "chain, mining mode, estimated hourly/daily earnings per chain, "
        "electricity cost, net USD/hr, ROI days, and slot usage."
    ),
    risk=RiskLevel.READ,
    category="mining",
    params=[
        ParamSpec("target_id", "uid", required=False, default=None,
                  description="Optional player id. Defaults to the caller."),
    ],
)
async def mining_status(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(args.get("target_id") or ctx.user_id)
    gid = int(ctx.guild_id)

    all_chain_rigs = await ctx.db.get_user_all_chain_rigs(uid, gid)
    mining_cfg = await ctx.db.get_user_mining_config(uid, gid)
    mode = (mining_cfg.get("mode") or "pool") if mining_cfg else "pool"

    job_row = await ctx.db.get_user_job(uid, gid)
    job_id = job_row["job_id"] if job_row else "HOMELESS"
    job_cfg = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])
    max_slots = job_cfg.get("rig_slots", 2)

    # Tally per-rig quantities
    rig_total_qty: dict[str, int] = {}
    for r in all_chain_rigs:
        rig_total_qty[r["rig_id"]] = rig_total_qty.get(r["rig_id"], 0) + r["quantity"]
    used_slots = sum(rig_total_qty.values())

    # Resale value (50%)
    rigs_cfg = Config.MINING_RIGS
    rig_resale = sum(
        to_human(rigs_cfg[rid]["price"]) * qty * 0.5
        for rid, qty in rig_total_qty.items() if rid in rigs_cfg
    )

    # Per-chain stats
    chain_stats: list[dict] = []
    total_usd_per_day = 0.0
    elec_per_hr = 0.0

    for sym, pow_cfg in Config.POW_NETWORKS.items():
        user_hr = await ctx.db.get_user_chain_hashrate(uid, gid, sym)
        network = await ctx.db.get_pow_network(gid, sym)
        if not network:
            continue
        net_hr = max(network.get("total_hashrate") or 1, 1)
        difficulty = network.get("difficulty") or pow_cfg["initial_difficulty"]
        block_height = network.get("block_height") or 0
        block_reward = _pow_current_reward(block_height, pow_cfg)

        price_row = await ctx.db.get_price(sym, gid)
        sym_usd = float(price_row["price"]) if price_row and price_row.get("price") else 0.0

        lam = (user_hr * _TICK / difficulty) if difficulty > 0 else 0
        blocks_per_hr = lam * (3600 / _TICK)
        est_hr = blocks_per_hr * block_reward
        usd_per_day = est_hr * 24 * sym_usd

        # Electricity for this chain
        sym_rigs = [r for r in all_chain_rigs if r["chain_symbol"] == sym]
        watts = sum(
            rigs_cfg[r["rig_id"]]["power"] * r["quantity"]
            for r in sym_rigs if r["rig_id"] in rigs_cfg
        )
        chain_elec = watts / 1000 * pow_cfg.get("electricity_rate", 0.0)
        elec_per_hr += chain_elec

        total_usd_per_day += usd_per_day
        chain_stats.append({
            "symbol": sym,
            "hashrate_mhs": user_hr,
            "network_hashrate_mhs": net_hr,
            "block_reward": round(block_reward, 8),
            "block_height": block_height,
            "token_price_usd": round(sym_usd, 6),
            "est_tokens_per_hr": round(est_hr, 8),
            "est_usd_per_day": round(usd_per_day, 4),
            "electricity_usd_per_hr": round(chain_elec, 6),
        })

    roi_days = rig_resale / total_usd_per_day if total_usd_per_day > 0 else None
    net_usd_hr = (total_usd_per_day / 24) - elec_per_hr

    # Active rig details
    active_rigs = []
    for rid, qty in rig_total_qty.items():
        if rid not in rigs_cfg:
            continue
        rcfg = rigs_cfg[rid]
        active_rigs.append({
            "rig_id": rid,
            "name": rcfg.get("name", rid),
            "quantity": qty,
            "hashrate_each_mhs": rcfg.get("hashrate", 0),
            "power_watts": rcfg.get("power", 0),
            "price_usd": round(to_human(rcfg["price"]), 2),
            "resale_usd": round(to_human(rcfg["price"]) * 0.5, 2),
        })

    return ToolResult.success({
        "uid": uid,
        "mining_mode": mode,
        "slots_used": used_slots,
        "slots_max": max_slots,
        "active_rigs": active_rigs,
        "chain_stats": chain_stats,
        "rig_resale_value_usd": round(rig_resale, 2),
        "total_est_usd_per_day": round(total_usd_per_day, 4),
        "electricity_cost_usd_per_hr": round(elec_per_hr, 6),
        "net_usd_per_hr": round(net_usd_hr, 4),
        "roi_days": round(roi_days, 1) if roi_days is not None else None,
    })


# -- mining.rigs ---------------------------------------------------------------

@tool(
    name="mining.rigs",
    summary=(
        "List all available mining rigs (catalog) plus how many the caller "
        "owns per chain. Includes price, hashrate, power draw, and whether "
        "a slot is available to buy more."
    ),
    risk=RiskLevel.READ,
    category="mining",
    params=[],
)
async def mining_rigs(ctx: ToolContext, args: dict) -> ToolResult:
    uid = int(ctx.user_id)
    gid = int(ctx.guild_id)

    owned = await ctx.db.get_user_all_chain_rigs(uid, gid)
    owned_map: dict[str, dict[str, int]] = {}
    for r in owned:
        owned_map.setdefault(r["rig_id"], {})[r["chain_symbol"]] = r["quantity"]

    job_row = await ctx.db.get_user_job(uid, gid)
    job_id = job_row["job_id"] if job_row else "HOMELESS"
    job_cfg = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])
    max_slots = job_cfg.get("rig_slots", 2)
    used_slots = sum(sum(v.values()) for v in owned_map.values())

    rigs_cfg = Config.MINING_RIGS
    catalog: list[dict] = []
    for rid, rcfg in rigs_cfg.items():
        by_chain = owned_map.get(rid, {})
        catalog.append({
            "rig_id": rid,
            "name": rcfg.get("name", rid),
            "price_usd": round(to_human(rcfg["price"]), 2),
            "hashrate_mhs": rcfg.get("hashrate", 0),
            "power_watts": rcfg.get("power", 0),
            "owned": {sym: qty for sym, qty in by_chain.items()},
            "total_owned": sum(by_chain.values()),
        })
    catalog.sort(key=lambda r: r["price_usd"])

    return ToolResult.success({
        "catalog": catalog,
        "slots_used": used_slots,
        "slots_max": max_slots,
        "slots_available": max(0, max_slots - used_slots),
    })


# -- mining.history ------------------------------------------------------------

@tool(
    name="mining.history",
    summary=(
        "Return the last 10 blocks mined on this server, across all PoW "
        "chains. Shows miner, chain, reward, and block height."
    ),
    risk=RiskLevel.READ,
    category="mining",
    params=[],
)
async def mining_history(ctx: ToolContext, args: dict) -> ToolResult:
    gid = int(ctx.guild_id)
    try:
        blocks = await ctx.db.get_recent_blocks(gid, limit=10)
    except Exception as exc:
        log.warning("[mining.history] get_recent_blocks failed: %s", exc)
        return ToolResult.fail(f"db_error: {exc}")

    if not blocks:
        return ToolResult.success({"blocks": [], "total": 0})

    result = []
    for b in blocks:
        result.append({
            "block_height": b.get("block_height"),
            "chain_symbol": b.get("chain_symbol") or b.get("symbol"),
            "miner_user_id": b.get("miner_id") or b.get("user_id"),
            "reward": round(float(b.get("reward") or 0), 8),
            "mode": b.get("mode") or "pool",
            "mined_at": b.get("mined_at"),
        })

    return ToolResult.success({"blocks": result, "total": len(result)})
