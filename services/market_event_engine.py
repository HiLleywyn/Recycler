"""services/market_event_engine.py  -  Multi-phase market event engine.

Manages active events in Redis, advances phases on a tick, announces
phase transitions in Discord, and exposes helpers for reading current
phase modifiers from any service (price engine, swap, staking, etc.).
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from configs.market_events_config import (
    EVENT_REGISTRY,
    ActiveEvent,
    EventPhase,
)

log = logging.getLogger("discoin.event_engine")

# Redis key helpers
_PREFIX = "discoin:event"


def _active_key(guild_id: int) -> str:
    return f"{_PREFIX}:active:{guild_id}"


def _cooldown_key(guild_id: int, event_id: str) -> str:
    return f"{_PREFIX}:cooldown:{guild_id}:{event_id}"


def _history_key(guild_id: int) -> str:
    return f"{_PREFIX}:history:{guild_id}"


# ── Read / Write active event state ─────────────────────────────────────────

async def get_active_event(redis, guild_id: int) -> ActiveEvent | None:
    """Read the active event for *guild_id* from Redis (or None)."""
    if redis is None:
        return None
    try:
        raw = await redis.get(_active_key(guild_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return ActiveEvent.from_dict(json.loads(raw))
    except Exception:
        log.warning("Corrupt active-event key for guild %s  -  clearing", guild_id)
        await redis.delete(_active_key(guild_id))
        return None


async def set_active_event(redis, ae: ActiveEvent) -> None:
    """Persist an ActiveEvent to Redis with a generous TTL."""
    if redis is None:
        return
    ev = EVENT_REGISTRY.get(ae.event_id)
    ttl = (ev.total_duration_seconds + 600) if ev else 7200
    await redis.setex(_active_key(ae.guild_id), ttl, json.dumps(ae.to_dict()))


async def clear_active_event(redis, guild_id: int) -> None:
    if redis is None:
        return
    await redis.delete(_active_key(guild_id))


# ── Cooldown helpers ─────────────────────────────────────────────────────────

async def set_cooldown(redis, guild_id: int, event_id: str, minutes: int) -> None:
    if redis is None:
        return
    await redis.setex(_cooldown_key(guild_id, event_id), minutes * 60, "1")


async def is_on_cooldown(redis, guild_id: int, event_id: str) -> bool:
    if redis is None:
        return False
    try:
        return bool(await redis.get(_cooldown_key(guild_id, event_id)))
    except Exception:
        return False


async def get_cooldown_remaining(redis, guild_id: int, event_id: str) -> int:
    """Return seconds remaining on cooldown, or 0."""
    if redis is None:
        return 0
    try:
        ttl = await redis.ttl(_cooldown_key(guild_id, event_id))
        return max(0, ttl)
    except Exception:
        return 0


# ── History helpers ──────────────────────────────────────────────────────────

async def push_history(redis, guild_id: int, entry: dict) -> None:
    """Append an event summary to guild history (Redis list, capped at 50)."""
    if redis is None:
        return
    key = _history_key(guild_id)
    await redis.lpush(key, json.dumps(entry))
    await redis.ltrim(key, 0, 49)
    await redis.expire(key, 86400 * 7)  # 7-day retention


async def get_history(redis, guild_id: int, limit: int = 10) -> list[dict]:
    if redis is None:
        return []
    try:
        raw_list = await redis.lrange(_history_key(guild_id), 0, limit - 1)
        return [json.loads(r) for r in raw_list]
    except Exception:
        return []


# ── Phase accessor ───────────────────────────────────────────────────────────

def resolve_effective_phase(ae: ActiveEvent) -> tuple[int, float]:
    """Compute the effective phase index and phase_started_at by fast-forwarding
    through any phases whose duration has already elapsed.

    Returns (effective_phase_index, effective_phase_started_at).
    If the event is fully expired, returns (len(phases), last_phase_end).
    """
    ev = EVENT_REGISTRY.get(ae.event_id)
    if ev is None:
        return ae.phase_index, ae.phase_started_at

    idx = ae.phase_index
    started = ae.phase_started_at
    now = time.time()

    while idx < len(ev.phases):
        phase = ev.phases[idx]
        phase_end = started + phase.duration_minutes * 60
        if now < phase_end:
            break
        # This phase has expired; advance
        started = phase_end
        idx += 1

    return idx, started


def get_current_phase(ae: ActiveEvent | None) -> EventPhase | None:
    """Return the current EventPhase for an active event, or None."""
    if ae is None:
        return None
    ev = EVENT_REGISTRY.get(ae.event_id)
    if ev is None:
        return None
    idx, _ = resolve_effective_phase(ae)
    if idx >= len(ev.phases):
        return None
    return ev.phases[idx]


def get_phase_modifiers(ae: ActiveEvent | None) -> dict[str, float]:
    """Return a dict of current-phase modifiers, defaulting to neutral.

    Uses resolve_effective_phase internally so modifiers are correct
    even if the tick task hasn't advanced the phase yet.
    """
    phase = get_current_phase(ae)
    if phase is None:
        return {
            "vol_multiplier": 1.0,
            "price_bias_pct_per_day": 0.0,
            "fee_multiplier": 1.0,
            "mining_difficulty_mult": 1.0,
            "staking_apy_mult": 1.0,
            "lending_rate_mult": 1.0,
            "liquidity_drain_pct": 0.0,
            "slippage_mult": 1.0,
        }
    return {
        "vol_multiplier": phase.vol_multiplier,
        "price_bias_pct_per_day": phase.price_bias_pct_per_day,
        "fee_multiplier": phase.fee_multiplier,
        "mining_difficulty_mult": phase.mining_difficulty_mult,
        "staking_apy_mult": phase.staking_apy_mult,
        "lending_rate_mult": phase.lending_rate_mult,
        "liquidity_drain_pct": phase.liquidity_drain_pct,
        "slippage_mult": phase.slippage_mult,
    }


# ── Event selection ──────────────────────────────────────────────────────────

async def pick_random_event(
    redis,
    guild_id: int,
    disabled: set[str] | None = None,
) -> str | None:
    """Weighted random selection respecting cooldowns and disabled set."""
    disabled = disabled or set()
    candidates: list[tuple[str, int]] = []
    for eid, ev in EVENT_REGISTRY.items():
        if eid in disabled:
            continue
        if await is_on_cooldown(redis, guild_id, eid):
            continue
        candidates.append((eid, ev.rarity_weight))
    if not candidates:
        return None
    ids, weights = zip(*candidates)
    return random.choices(ids, weights=weights, k=1)[0]


# ── Core engine: start / advance / end ───────────────────────────────────────

async def start_event(
    redis,
    guild_id: int,
    event_id: str,
    start_prices: dict[str, float] | None = None,
) -> ActiveEvent:
    """Start a new event for a guild, cancelling conflicting events."""
    ev = EVENT_REGISTRY[event_id]

    # Cancel conflicting events
    existing = await get_active_event(redis, guild_id)
    if existing and existing.event_id in ev.cancels:
        await end_event(redis, guild_id, cancelled=True)

    now = time.time()
    ae = ActiveEvent(
        guild_id=guild_id,
        event_id=event_id,
        phase_index=0,
        phase_started_at=now,
        event_started_at=now,
        start_prices=start_prices or {},
    )
    await set_active_event(redis, ae)
    await set_cooldown(redis, guild_id, event_id, ev.cooldown_minutes)
    return ae


async def advance_phase(redis, ae: ActiveEvent) -> ActiveEvent:
    """Move to the next phase. Returns updated ActiveEvent."""
    ae.phase_index += 1
    ae.phase_started_at = time.time()
    await set_active_event(redis, ae)
    return ae


async def end_event(
    redis,
    guild_id: int,
    cancelled: bool = False,
    end_prices: dict[str, float] | None = None,
) -> dict | None:
    """End the active event: clear state, push history entry. Returns summary or None."""
    ae = await get_active_event(redis, guild_id)
    if ae is None:
        return None

    ev = EVENT_REGISTRY.get(ae.event_id)
    duration = time.time() - ae.event_started_at

    summary: dict[str, Any] = {
        "event_id": ae.event_id,
        "display_name": ev.display_name if ev else ae.event_id,
        "started_at": ae.event_started_at,
        "ended_at": time.time(),
        "duration_seconds": round(duration, 1),
        "cancelled": cancelled,
    }

    # Calculate price impact if we have both start and end prices
    if ae.start_prices and end_prices:
        impacts: dict[str, float] = {}
        for sym, sp in ae.start_prices.items():
            ep = end_prices.get(sym)
            if ep and sp > 0:
                impacts[sym] = round(((ep - sp) / sp) * 100, 2)
        summary["price_impacts"] = impacts

    await push_history(redis, guild_id, summary)
    await clear_active_event(redis, guild_id)
    return summary


def should_advance_phase(ae: ActiveEvent) -> bool:
    """Return True if the current phase duration has elapsed."""
    ev = EVENT_REGISTRY.get(ae.event_id)
    if ev is None or ae.phase_index >= len(ev.phases):
        return True  # unknown or past-end → should clean up
    phase = ev.phases[ae.phase_index]
    return ae.phase_elapsed >= phase.duration_minutes * 60


def is_final_phase(ae: ActiveEvent) -> bool:
    ev = EVENT_REGISTRY.get(ae.event_id)
    if ev is None:
        return True
    return ae.phase_index >= len(ev.phases) - 1


def phase_time_remaining(ae: ActiveEvent) -> float:
    """Seconds remaining in the current phase (resolves effective phase)."""
    ev = EVENT_REGISTRY.get(ae.event_id)
    if ev is None:
        return 0.0
    idx, started = resolve_effective_phase(ae)
    if idx >= len(ev.phases):
        return 0.0
    phase = ev.phases[idx]
    return max(0.0, (started + phase.duration_minutes * 60) - time.time())


def event_time_remaining(ae: ActiveEvent) -> float:
    """Seconds remaining across all remaining phases (resolves effective phase)."""
    ev = EVENT_REGISTRY.get(ae.event_id)
    if ev is None:
        return 0.0
    idx, started = resolve_effective_phase(ae)
    now = time.time()
    total = 0.0
    for i, phase in enumerate(ev.phases):
        if i < idx:
            continue
        elif i == idx:
            total += max(0.0, (started + phase.duration_minutes * 60) - now)
        else:
            total += phase.duration_minutes * 60
    return total
