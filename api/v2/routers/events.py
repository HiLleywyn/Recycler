"""Market event endpoints  -  active event, history, and registry config."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.v2.dependencies import get_current_user, get_redis

from configs.market_events_config import EVENT_REGISTRY
from services.market_event_engine import (
    get_active_event,
    get_current_phase,
    get_phase_modifiers,
    get_history,
    event_time_remaining,
    phase_time_remaining,
)

router = APIRouter(prefix="/market/event", tags=["market-events"])


# ---------------------------------------------------------------------------
# GET /market/event/active  -  current event + phase + modifiers
# ---------------------------------------------------------------------------

@router.get("/active", summary="Get active market event")
async def active_event(
    user: dict[str, Any] = Depends(get_current_user),
    redis=Depends(get_redis),
) -> dict:
    """Return the currently active event with phase detail and modifiers.

    Returns ``{"active": false}`` when no event is running.
    """
    guild_id = int(user["guild_id"])
    ae = await get_active_event(redis, guild_id)
    if ae is None:
        return {"active": False}

    ev = EVENT_REGISTRY.get(ae.event_id)
    if ev is None:
        return {"active": False}

    phase = get_current_phase(ae)
    mods = get_phase_modifiers(ae)

    return {
        "active": True,
        "event_id": ae.event_id,
        "display_name": ev.display_name,
        "emoji": ev.emoji,
        "description": ev.description,
        "phase_index": ae.phase_index,
        "phase_name": phase.name if phase else None,
        "phase_flavor": phase.flavor_text if phase else None,
        "total_phases": len(ev.phases),
        "phase_time_remaining_s": round(phase_time_remaining(ae), 1),
        "event_time_remaining_s": round(event_time_remaining(ae), 1),
        "phase_end_ts": round(ae.phase_started_at + (phase.duration_minutes * 60 if phase else 0)),
        "event_started_at": round(ae.event_started_at),
        "modifiers": mods,
    }


# ---------------------------------------------------------------------------
# GET /market/event/history  -  past events
# ---------------------------------------------------------------------------

@router.get("/history", summary="Get market event history")
async def event_history(
    user: dict[str, Any] = Depends(get_current_user),
    redis=Depends(get_redis),
    limit: int = 10,
) -> list[dict]:
    """Return the last N events with timestamps and price impacts."""
    guild_id = int(user["guild_id"])
    return await get_history(redis, guild_id, limit=min(limit, 50))


# ---------------------------------------------------------------------------
# GET /market/event/config  -  event registry for dashboard docs
# ---------------------------------------------------------------------------

@router.get("/config", summary="Get event registry config")
async def event_config() -> list[dict]:
    """Return the full event registry (definitions, phases, modifiers).

    Useful for dashboard tooltips and documentation pages.
    """
    result = []
    for eid, ev in EVENT_REGISTRY.items():
        phases = []
        for p in ev.phases:
            phases.append({
                "name": p.name,
                "duration_minutes": p.duration_minutes,
                "vol_multiplier": p.vol_multiplier,
                "price_bias_pct_per_day": p.price_bias_pct_per_day,
                "fee_multiplier": p.fee_multiplier,
                "mining_difficulty_mult": p.mining_difficulty_mult,
                "staking_apy_mult": p.staking_apy_mult,
                "lending_rate_mult": p.lending_rate_mult,
                "liquidity_drain_pct": p.liquidity_drain_pct,
                "slippage_mult": p.slippage_mult,
                "embed_color": hex(p.embed_color),
                "flavor_text": p.flavor_text,
            })
        result.append({
            "event_id": eid,
            "display_name": ev.display_name,
            "emoji": ev.emoji,
            "description": ev.description,
            "rarity_weight": ev.rarity_weight,
            "cooldown_minutes": ev.cooldown_minutes,
            "total_duration_minutes": ev.total_duration_seconds // 60,
            "phases": phases,
            "on_start_effects": list(ev.on_start_effects),
            "on_end_effects": list(ev.on_end_effects),
            "cancels": list(ev.cancels),
            "stackable": ev.stackable,
        })
    return result
