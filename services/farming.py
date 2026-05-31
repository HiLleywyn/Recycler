"""services/farming.py -- service layer for the Farm minigame on the
Harvest Network. Mirrors services/fishing.py and services/dungeon.py.

HRV is the network coin (swappable, oracle-priced). SEED is the
earn-only token harvested from crops. The token firewall is identical
to the Lure / Crypt / Buddy networks:
- SEED -> HRV via burn_seed_for_hrv (1:1 USD value at oracle, slippage)
- HRV -> USD via cashout_hrv (one-way burn, slippage on oracle)
- HRV <-> {REEL, RUNE, BUD} via the carve-out AMM pools
- SEED has no AMM pool (earn-only-out)

Public API surface expected by cogs/farming.py:
- ensure_state, list_state, set_zone, set_fertilizer, force_unstuck
- get_or_roll_weather
- plant_seed, water_plot, apply_fertilizer, harvest_plot, clear_plot
- buy_plot_tier, buy_fertilizer, buy_seed_packet
- inventory_summary, sell_crop, process_recipe, sell_processed
- burn_seed_for_hrv, cashout_hrv
- accrued_stake_yield, stake_seed, claim_stake_yield, unstake_seed
- maybe_spawn_pest, resolve_pest_battle
- get_top_farmers, get_biggest_harvests, get_user_harvests
"""
from __future__ import annotations

import datetime as _dt
import json as _json_mod
import logging
import random
import time as _time
from dataclasses import dataclass, field
from typing import Any

import configs.farming_config as fc
from core.framework.scale import to_human, to_raw
from services.fishing import (
    _distribute_burn_lp_reward,
    _price_impact,
    _write_burn_candle,
    _oracle_price,
)

log = logging.getLogger(__name__)


# ============================================================================
#  JSON / state helpers (mirror services/fishing.py)
# ============================================================================

def _as_dict(value: Any) -> dict:
    """Normalize a JSONB column value to dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = _json_mod.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list(value: Any) -> list:
    """Normalize a JSONB column value to list."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = _json_mod.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _now_ts() -> int:
    """Unix epoch seconds (used for plot ready_at math)."""
    return int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())


def _now_iso() -> str:
    """ISO-8601 UTC string for jsonb timestamps inside plot dicts."""
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _json(payload: Any) -> str:
    """Serialize payload to a JSON string for ::jsonb casts."""
    return _json_mod.dumps(payload, default=str)


_JSONB_DICT_COLUMNS: tuple[str, ...] = (
    "crop_inventory",
    "processed_inventory",
    "fertilizer_inventory",
    "seed_packets",
    "daily_contract",
)
_JSONB_LIST_COLUMNS: tuple[str, ...] = ("plots",)


def _normalize_state(row: Any) -> dict:
    """Coerce a user_farming row's JSONB fields to dicts/lists."""
    if row is None:
        return {}
    out: dict[str, Any] = dict(row) if not isinstance(row, dict) else dict(row)
    for col in _JSONB_DICT_COLUMNS:
        out[col] = _as_dict(out.get(col))
    for col in _JSONB_LIST_COLUMNS:
        out[col] = _as_list(out.get(col))
    return out


# ============================================================================
#  Dataclasses
# ============================================================================

@dataclass
class FarmState:
    """Snapshot of user_farming row, plots normalized to list."""
    raw: dict = field(default_factory=dict)


@dataclass
class PlantResult:
    crop_key: str = ""
    plot_slot: int = -1
    ready_at: str = ""
    growth_seconds: int = 0
    ok: bool = False
    msg: str = ""


@dataclass
class WaterResult:
    plot_slot: int = -1
    watered_count: int = 0
    growth_speedup_pct: float = 0.0
    ok: bool = False
    msg: str = ""


@dataclass
class HarvestResult:
    plot_slot: int = -1
    crop_key: str = ""
    rarity: str = "common"
    qty: int = 0
    seed_raw: int = 0
    hrv_raw: int = 0
    combo_mult: float = 1.0
    combo_step: int = 0
    weather: str = "clear"
    fertilizer_key: str | None = None
    mutation: str | None = None
    seed_packets_returned: int = 0
    granted_badges: list[str] = field(default_factory=list)


@dataclass
class FertilizerApplyResult:
    plot_slot: int = -1
    fertilizer_key: str = ""
    yield_mult: float = 1.0
    growth_mult: float = 1.0
    ok: bool = False
    msg: str = ""


@dataclass
class BuyResult:
    kind: str = ""
    key: str = ""
    qty: int = 0
    hrv_spent_raw: int = 0
    new_tier: int = 0
    ok: bool = False
    msg: str = ""


@dataclass
class SellCropResult:
    crop_or_recipe_key: str = ""
    qty_sold: int = 0
    hrv_received_raw: int = 0
    slippage_pct: float = 0.0


@dataclass
class ProcessResult:
    recipe_key: str = ""
    qty_made: int = 0
    seed_bonus_raw: int = 0
    ok: bool = False
    msg: str = ""


@dataclass
class BurnResult:
    burned_seed_raw: int = 0
    minted_hrv_raw: int = 0
    impact_pct: float = 0.0


@dataclass
class CashoutResult:
    burned_hrv_raw: int = 0
    paid_usd_raw: int = 0
    impact_pct: float = 0.0


@dataclass
class StakeResult:
    staked_now_raw: int = 0
    total_staked_raw: int = 0
    paid_yield_raw: int = 0


@dataclass
class WeatherEvent:
    zone: str = ""
    weather_key: str = "clear"
    expires_at: str = ""


@dataclass
class PestBattleResolution:
    outcome: str = "continue"   # continue | pest_dead | player_fled | player_killed
    pest_key: str = ""
    captured: bool = False
    seed_drop_raw: int = 0
    log: list[str] = field(default_factory=list)
    pest_state: dict | None = None
    is_boss: bool = False


# ============================================================================
#  Lifecycle: ensure_state / list_state / setters
# ============================================================================
async def ensure_state(db: Any, guild_id: int, user_id: int) -> dict:
    """Insert a starter row if absent, then return the normalized state."""
    await db.execute(
        """
        INSERT INTO user_farming (guild_id, user_id, current_zone)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, user_id) DO NOTHING
        """,
        guild_id, user_id, fc.DEFAULT_ZONE,
    )
    row = await db.fetch_one(
        "SELECT * FROM user_farming WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    state = _normalize_state(row)
    # Auto-grow plot list to current plot_count if needed.
    plots = list(state.get("plots") or [])
    target_count = int(state.get("plot_count") or fc.PLOT_COUNT_BY_TIER.get(int(state.get("plot_tier") or 1), 4))
    while len(plots) < target_count:
        plots.append({
            "slot": len(plots),
            "state": "empty",
            "crop_key": None,
            "planted_at": None,
            "ready_at": None,
            "watered_count": 0,
            "fertilizer_key": None,
            "fertilizer_mult": 1.0,
            "weather_seed": "clear",
            "mutation": None,
        })
    if plots != list(state.get("plots") or []):
        await db.execute(
            "UPDATE user_farming SET plots = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(plots),
        )
        state["plots"] = plots
    return state


async def list_state(db: Any, guild_id: int, user_id: int) -> dict:
    """Read-only state fetch (no auto-growth). Returns normalized dict."""
    row = await db.fetch_one(
        "SELECT * FROM user_farming WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    return _normalize_state(row)


async def set_zone(db: Any, guild_id: int, user_id: int, zone_key: str) -> dict:
    z = fc.zone_meta(zone_key)
    if not z:
        raise ValueError(f"Unknown zone: {zone_key}")
    state = await ensure_state(db, guild_id, user_id)
    plot_tier = int(state.get("plot_tier") or 1)
    if int(z["plot_tier_required"]) > plot_tier:
        raise ValueError(
            f"Zone **{z['name']}** requires plot tier {z['plot_tier_required']}; "
            f"you're on tier {plot_tier}."
        )
    await db.execute(
        "UPDATE user_farming SET current_zone = $3, weather_until = NULL, "
        "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, z["key"],
    )
    return await list_state(db, guild_id, user_id)


async def set_fertilizer(db: Any, guild_id: int, user_id: int, key: str | None) -> dict:
    if key is not None:
        if key.lower() in ("none", "off", "clear", ""):
            key = None
        else:
            f = fc.fertilizer_meta(key)
            if not f:
                raise ValueError(f"Unknown fertilizer: {key}")
            key = f["key"]
    await db.execute(
        "UPDATE user_farming SET equipped_fertilizer = $3, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, key,
    )
    return await list_state(db, guild_id, user_id)


async def force_unstuck(db: Any, guild_id: int, user_id: int) -> bool:
    """Clear is_acting soft-lock (admin / self-rescue command)."""
    status = await db.execute(
        "UPDATE user_farming SET is_acting = FALSE, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2 AND is_acting = TRUE",
        guild_id, user_id,
    )
    return isinstance(status, str) and status.startswith("UPDATE ") and status != "UPDATE 0"


async def _set_acting(db: Any, guild_id: int, user_id: int, value: bool) -> bool:
    """Soft-lock toggle. Returns True only if the row's is_acting flipped."""
    if value:
        status = await db.execute(
            "UPDATE user_farming SET is_acting = TRUE, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2 AND is_acting = FALSE",
            guild_id, user_id,
        )
        return isinstance(status, str) and status != "UPDATE 0"
    else:
        await db.execute(
            "UPDATE user_farming SET is_acting = FALSE, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        return True


# ============================================================================
#  Weather rolling
# ============================================================================
async def get_or_roll_weather(db: Any, guild_id: int, user_id: int) -> WeatherEvent:
    """Return current weather, rolling a new one if expired.

    Weather is per-user (not per-guild) so each player gets their own
    rhythm. Rolls happen lazily: first call after `weather_until` passes
    picks a new weather based on the current zone's `default_weather_pool`
    weighted by global WEATHER_WEIGHTS.
    """
    state = await ensure_state(db, guild_id, user_id)
    weather_key = str(state.get("current_weather") or fc.DEFAULT_WEATHER)
    weather_until = _parse_iso(state.get("weather_until"))
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    if weather_until is None or now >= weather_until:
        zone = fc.zone_meta(str(state.get("current_zone") or fc.DEFAULT_ZONE)) or {}
        pool = list(zone.get("default_weather_pool") or ("clear",))
        # Bias toward zone's pool, but allow any weather to roll.
        rng = random.Random()
        weights = [fc.WEATHER_WEIGHTS.get(k, 1) * (3 if k in pool else 1) for k in fc.WEATHER]
        weather_key = rng.choices(list(fc.WEATHER), weights=weights, k=1)[0]
        meta = fc.weather_meta(weather_key) or {"duration_minutes": 30}
        new_until = now + _dt.timedelta(minutes=int(meta["duration_minutes"]))
        await db.execute(
            "UPDATE user_farming SET current_weather = $3, weather_until = $4, "
            "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, weather_key, new_until,
        )
        weather_until = new_until
    return WeatherEvent(
        zone=str(state.get("current_zone") or fc.DEFAULT_ZONE),
        weather_key=weather_key,
        expires_at=(weather_until.isoformat(timespec="seconds") if weather_until else ""),
    )


# ============================================================================
#  Plot actions: plant / water / fertilize / harvest / clear
# ============================================================================
async def plant_seed(
    db: Any, guild_id: int, user_id: int,
    plot_slot: int, crop_key: str,
) -> PlantResult:
    """Plant a seed packet into an empty plot.

    Consumes one entry from seed_packets[crop_key]. Sets the plot's
    growth start + ready_at using farming_config.growth_seconds adjusted
    by current weather + plot tier. Equipped fertilizer is NOT
    auto-applied here; players must call apply_fertilizer separately.
    """
    state = await ensure_state(db, guild_id, user_id)
    plots = list(state.get("plots") or [])
    if plot_slot < 0 or plot_slot >= len(plots):
        raise ValueError(f"Plot slot {plot_slot} out of range (have {len(plots)} plots).")
    p = dict(plots[plot_slot])
    if p.get("state") != "empty":
        raise ValueError(f"Plot {plot_slot} is not empty (state={p.get('state')}).")
    crop_meta = fc.crop_meta(crop_key)
    if not crop_meta:
        raise ValueError(f"Unknown crop: {crop_key}")
    seed_packets = _as_dict(state.get("seed_packets"))
    have = int(seed_packets.get(crop_meta["key"], 0) or 0)
    if have <= 0:
        raise ValueError(f"You don't have any {crop_meta['name']} seed packets. Buy with `,farm buy seed {crop_meta['key']} <qty>`.")
    seed_packets[crop_meta["key"]] = have - 1
    weather = await get_or_roll_weather(db, guild_id, user_id)
    w_meta = fc.weather_meta(weather.weather_key) or {"growth_mult": 1.0}
    plot_tier = int(state.get("plot_tier") or 1)
    grow_s = fc.growth_seconds(
        crop_meta["key"],
        fertilizer_growth_mult=1.0,
        weather_growth_mult=float(w_meta.get("growth_mult", 1.0)),
        plot_tier=plot_tier,
    )
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    ready = now + _dt.timedelta(seconds=grow_s)
    # Mutation roll: rare event at plant time. The mutation key sticks
    # on the plot dict and is read back at harvest time -- never mutates
    # after planting. Bloomstone yield bonus also nudges mutation odds so
    # late-game plot stones feel a little more alive.
    try:
        from services import themed_stones as _ts
        mut_bonus = await _ts.bloomstone_yield_bonus(db, user_id, guild_id)
    except Exception:
        mut_bonus = 0.0
    mutation = fc.roll_mutation(crop_meta["key"], bonus=float(mut_bonus or 0.0))
    p.update({
        "state": "growing",
        "crop_key": crop_meta["key"],
        "planted_at": now.isoformat(timespec="seconds"),
        "ready_at": ready.isoformat(timespec="seconds"),
        "watered_count": 0,
        "fertilizer_key": None,
        "fertilizer_mult": 1.0,
        "weather_seed": weather.weather_key,
        "mutation": mutation,
    })
    plots[plot_slot] = p
    await db.execute(
        """
        UPDATE user_farming SET
            plots = $3::jsonb,
            seed_packets = $4::jsonb,
            total_planted = total_planted + 1,
            last_plant_at = NOW(),
            last_action_at = NOW(),
            updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(plots), _json(seed_packets),
    )
    try:
        from services import themed_stones as _ts
        await _ts.grant_bloomstone_xp(db, user_id, guild_id, planted=1)
    except Exception:
        log.debug(
            "farming: themed_stones.grant_bloomstone_xp plant failed",
            exc_info=True,
        )
    return PlantResult(
        crop_key=crop_meta["key"],
        plot_slot=plot_slot,
        ready_at=p["ready_at"],
        growth_seconds=grow_s,
        ok=True,
        msg=f"Planted {crop_meta['emoji']} **{crop_meta['name']}** in plot {plot_slot}.",
    )


async def water_plot(
    db: Any, guild_id: int, user_id: int, plot_slot: int,
) -> WaterResult:
    """Water a growing plot. Each water shaves ~5% off remaining growth
    time, capped by the plot tier's max_water count."""
    state = await ensure_state(db, guild_id, user_id)
    plots = list(state.get("plots") or [])
    if plot_slot < 0 or plot_slot >= len(plots):
        raise ValueError(f"Plot slot {plot_slot} out of range.")
    p = dict(plots[plot_slot])
    if p.get("state") != "growing":
        raise ValueError(f"Plot {plot_slot} isn't growing (state={p.get('state')}).")
    plot_meta = fc.plot_meta(int(state.get("plot_tier") or 1)) or {}
    cap = int(plot_meta.get("max_water", 2))
    cur = int(p.get("watered_count", 0) or 0)
    if cur >= cap:
        raise ValueError(f"Already watered max times ({cap}). Wait it out.")
    speedup = 0.05  # 5% per water
    ready = _parse_iso(p.get("ready_at"))
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    if ready is None:
        ready = now
    remaining = max(0.0, (ready - now).total_seconds())
    new_remaining = remaining * (1.0 - speedup)
    new_ready = now + _dt.timedelta(seconds=new_remaining)
    p["watered_count"] = cur + 1
    p["ready_at"] = new_ready.isoformat(timespec="seconds")
    plots[plot_slot] = p
    await db.execute(
        "UPDATE user_farming SET plots = $3::jsonb, last_action_at = NOW(), "
        "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(plots),
    )
    return WaterResult(
        plot_slot=plot_slot,
        watered_count=p["watered_count"],
        growth_speedup_pct=speedup * 100,
        ok=True,
        msg=f"Watered plot {plot_slot} ({p['watered_count']}/{cap}).",
    )


async def apply_fertilizer(
    db: Any, guild_id: int, user_id: int,
    plot_slot: int, fert_key: str | None = None,
) -> FertilizerApplyResult:
    """Apply a fertilizer to a growing plot. Caches yield_mult on the
    plot dict so harvest_plot can read it. Consumes one fertilizer."""
    state = await ensure_state(db, guild_id, user_id)
    plots = list(state.get("plots") or [])
    if plot_slot < 0 or plot_slot >= len(plots):
        raise ValueError(f"Plot slot {plot_slot} out of range.")
    p = dict(plots[plot_slot])
    if p.get("state") != "growing":
        raise ValueError(f"Plot {plot_slot} isn't growing.")
    if p.get("fertilizer_key"):
        raise ValueError(f"Plot {plot_slot} already has fertilizer applied.")
    key = fert_key or state.get("equipped_fertilizer")
    if not key:
        raise ValueError("No fertilizer specified or equipped. Use `,farm equip <key>` first.")
    fmeta = fc.fertilizer_meta(key)
    if not fmeta:
        raise ValueError(f"Unknown fertilizer: {key}")
    fert_inv = _as_dict(state.get("fertilizer_inventory"))
    have = int(fert_inv.get(fmeta["key"], 0) or 0)
    if have <= 0:
        raise ValueError(f"You don't have any {fmeta['name']}. Buy with `,farm buy fertilizer {fmeta['key']} <qty>`.")
    fert_inv[fmeta["key"]] = have - 1
    # Apply growth boost retroactively to remaining time.
    growth_mult = float(fmeta.get("growth_mult", 1.0))
    ready = _parse_iso(p.get("ready_at"))
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    if ready and ready > now:
        remaining = (ready - now).total_seconds() * growth_mult
        p["ready_at"] = (now + _dt.timedelta(seconds=remaining)).isoformat(timespec="seconds")
    p["fertilizer_key"] = fmeta["key"]
    p["fertilizer_mult"] = float(fmeta.get("yield_mult", 1.0))
    plots[plot_slot] = p
    await db.execute(
        "UPDATE user_farming SET plots = $3::jsonb, fertilizer_inventory = $4::jsonb, "
        "last_action_at = NOW(), updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(plots), _json(fert_inv),
    )
    return FertilizerApplyResult(
        plot_slot=plot_slot,
        fertilizer_key=fmeta["key"],
        yield_mult=p["fertilizer_mult"],
        growth_mult=growth_mult,
        ok=True,
        msg=f"Applied {fmeta['emoji']} **{fmeta['name']}** to plot {plot_slot}.",
    )


async def apply_fertilizer_all(
    db: Any, guild_id: int, user_id: int, fert_key: str | None = None,
) -> tuple[list[int], str]:
    """Apply the equipped (or specified) fertilizer to every growing plot
    that doesn't already have one. Stops when the fertilizer inventory
    runs out. Single UPDATE at the end so partial state is never written.

    Returns ``(applied_slots, fertilizer_key)``. ``applied_slots`` is the
    1-based slot indices that received fertilizer; an empty list means
    nothing was eligible (no growing plots, all already fertilized, or
    no fertilizer left).
    """
    state = await ensure_state(db, guild_id, user_id)
    plots = list(state.get("plots") or [])
    key = fert_key or state.get("equipped_fertilizer")
    if not key:
        raise ValueError("No fertilizer equipped. Use `,farm equip <key>` first.")
    fmeta = fc.fertilizer_meta(key)
    if not fmeta:
        raise ValueError(f"Unknown fertilizer: {key}")
    fert_inv = _as_dict(state.get("fertilizer_inventory"))
    have = int(fert_inv.get(fmeta["key"], 0) or 0)
    if have <= 0:
        raise ValueError(
            f"You don't have any {fmeta['emoji']} **{fmeta['name']}**. "
            f"Buy with `,farm buy fertilizer {fmeta['key']} <qty>`."
        )

    growth_mult = float(fmeta.get("growth_mult", 1.0))
    yield_mult = float(fmeta.get("yield_mult", 1.0))
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    applied: list[int] = []

    for i, raw in enumerate(plots):
        if have <= 0:
            break
        p = dict(raw)
        if p.get("state") != "growing":
            continue
        if p.get("fertilizer_key"):
            continue
        ready = _parse_iso(p.get("ready_at"))
        if ready and ready > now:
            remaining = (ready - now).total_seconds() * growth_mult
            p["ready_at"] = (now + _dt.timedelta(seconds=remaining)).isoformat(timespec="seconds")
        p["fertilizer_key"] = fmeta["key"]
        p["fertilizer_mult"] = yield_mult
        plots[i] = p
        have -= 1
        applied.append(i + 1)

    if not applied:
        return ([], fmeta["key"])

    fert_inv[fmeta["key"]] = have
    await db.execute(
        "UPDATE user_farming SET plots = $3::jsonb, fertilizer_inventory = $4::jsonb, "
        "last_action_at = NOW(), updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(plots), _json(fert_inv),
    )
    return (applied, fmeta["key"])


async def harvest_plot(
    db: Any, guild_id: int, user_id: int, plot_slot: int,
) -> HarvestResult:
    """Harvest a ready plot. Rolls yield with fertilizer + weather mults,
    credits SEED + crop inventory, writes farming_harvests audit row.

    Returns HarvestResult.granted_badges is empty -- the cog populates
    it via _fan_out after the service call returns.
    """
    state = await ensure_state(db, guild_id, user_id)
    plots = list(state.get("plots") or [])
    if plot_slot < 0 or plot_slot >= len(plots):
        raise ValueError(f"Plot slot {plot_slot} out of range.")
    p = dict(plots[plot_slot])
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    ready = _parse_iso(p.get("ready_at"))
    if p.get("state") != "growing" and p.get("state") != "ready":
        raise ValueError(f"Plot {plot_slot} isn't ready ({p.get('state')}).")
    if ready is None or now < ready:
        eta = int((ready - now).total_seconds()) if ready else 0
        raise ValueError(f"Plot {plot_slot} isn't ready yet ({eta}s to go).")
    crop_key = str(p.get("crop_key") or "")
    crop_meta = fc.crop_meta(crop_key)
    if not crop_meta:
        # Plot lost its crop reference somehow; mark empty.
        p.update({"state": "empty", "crop_key": None, "planted_at": None,
                  "ready_at": None, "watered_count": 0,
                  "fertilizer_key": None, "fertilizer_mult": 1.0})
        plots[plot_slot] = p
        await db.execute(
            "UPDATE user_farming SET plots = $3::jsonb WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(plots),
        )
        raise ValueError(f"Plot {plot_slot} had an unknown crop key. Cleared.")
    weather_key = str(p.get("weather_seed") or "clear")
    w_meta = fc.weather_meta(weather_key) or {"yield_mult": 1.0}
    plot_meta = fc.plot_meta(int(state.get("plot_tier") or 1)) or {}
    rng = random.Random()
    fert_mult = float(p.get("fertilizer_mult", 1.0) or 1.0)
    weather_mult = float(w_meta.get("yield_mult", 1.0))
    plot_yield_bonus = float(plot_meta.get("yield_bonus", 0.0))
    # Bloomstone bonuses scale with stone level (0 if user doesn't own it).
    # Pull both before the roll so the same level applies to qty + SEED.
    try:
        from services import themed_stones as _ts
        bloom_yield_bonus = await _ts.bloomstone_yield_bonus(db, user_id, guild_id)
        bloom_seed_bonus  = await _ts.bloomstone_seed_drop_bonus(db, user_id, guild_id)
    except Exception:
        bloom_yield_bonus = 0.0
        bloom_seed_bonus  = 0.0
    # Perk + seasonal + combo modifiers. These are additive on top of
    # the existing fert / weather / bloomstone math.
    perks_inv = _as_dict(state.get("perks"))
    perk_yield_bonus = 0.0
    if fc.perk_active(perks_inv, "green_thumb"):
        perk_yield_bonus += float(fc.PERKS["green_thumb"]["yield_bonus"])
    season_mult = fc.seasonal_yield_mult(crop_key)
    # Yield = base roll * fert * weather * (1 + plot bonus + bloomstone bonus + perk bonus)
    qty = fc.yield_roll(
        crop_key, rng,
        fertilizer_mult=fert_mult,
        weather_mult=weather_mult * season_mult * (
            1.0 + plot_yield_bonus + bloom_yield_bonus + perk_yield_bonus
        ),
    )
    # Active-buddy farming-lane bonus inflates the harvest qty. Same
    # multiplier shape as the chat / work / trade buffs; signature-lane
    # buddies grow this into a real edge by mid-level. Best-effort.
    try:
        from services.buddy_bonus import buddy_bonus as _bb
        qty = int(round(qty * await _bb(db, guild_id, user_id, lane="farming")))
    except Exception:
        log.debug("farming buddy_bonus failed", exc_info=True)
    # Crop mutation kicker -- the plot rolled a mutation at plant time.
    # Apply yield + SEED multipliers; the HRV sell multiplier is folded
    # into the SEED payout because the crop inventory is fungible per key
    # (we credit the player extra SEED rather than tracking mutated stacks).
    mutation_key = str(p.get("mutation") or "") or None
    mut_meta = fc.mutation_meta(mutation_key) if mutation_key else None
    if mut_meta:
        qty = max(0, int(round(qty * float(mut_meta.get("yield_mult", 1.0)))))
    rarity = str(crop_meta.get("rarity", "common"))
    # Harvest combo. Step persists in user_farming.combo_step and resets
    # when the gap from last harvest exceeds COMBO_WINDOW_S.
    prev_combo = int(state.get("combo_step") or 0)
    last_h_at = state.get("last_harvest_at")
    combo_valid = False
    if last_h_at:
        try:
            if isinstance(last_h_at, _dt.datetime):
                gap = (now - last_h_at).total_seconds()
            else:
                gap = (now - _parse_iso(last_h_at)).total_seconds()
            combo_valid = gap <= fc.COMBO_WINDOW_S
        except Exception:
            combo_valid = False
    if not combo_valid:
        prev_combo = 0
    new_combo_step, combo_mult_eff = fc.harvest_combo_mult(prev_combo, perks_inv)
    qty = max(0, int(round(qty * combo_mult_eff)))
    # Late-game perks layered on after combo (so they don't compound combo).
    if rarity == "legendary" and fc.perk_active(perks_inv, "mythic_thumb"):
        qty += int(fc.PERKS["mythic_thumb"].get("legendary_bonus_qty") or 0)
    # SEED payout: random within crop's range, scaled by yield qty / mid,
    # then nudged up by the Bloomstone seed-drop bonus.
    seed_human = float(rng.uniform(
        float(crop_meta["seed_payout_min"]), float(crop_meta["seed_payout_max"]),
    )) * (qty / max(1.0, (crop_meta["base_yield_min"] + crop_meta["base_yield_max"]) / 2.0))
    seed_human *= (1.0 + bloom_seed_bonus)
    if mut_meta:
        seed_human *= float(mut_meta.get("seed_mult", 1.0))
    # Combo also nudges SEED so a 5-step combo feels like a real run.
    seed_human *= combo_mult_eff
    if rarity in ("rare", "epic", "legendary") and fc.perk_active(perks_inv, "gold_thumb"):
        seed_human *= 1.0 + float(fc.PERKS["gold_thumb"].get("rare_bonus") or 0.0)
    if weather_key in ("harvest_moon", "blood_moon") and fc.perk_active(perks_inv, "moonlit_grower"):
        seed_human *= float(fc.PERKS["moonlit_grower"].get("moon_yield_mult") or 1.0)
    seed_raw = int(to_raw(round(seed_human, 2)))
    # Credit SEED to user wallet (Harvest Network short = "har")
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
        seed_raw,
    )
    # Add crops to inventory
    inv = _as_dict(state.get("crop_inventory"))
    inv[crop_meta["key"]] = int(inv.get(crop_meta["key"], 0) or 0) + qty
    # Seed-return roll: chance the crop "goes to seed" and drops a few
    # packets of itself back into the player's seed_packets inventory.
    # Rarity-scaled odds + qty so common crops drop more often + more
    # packets than legendaries (rolled in farming_config.roll_seed_return).
    seed_packets = _as_dict(state.get("seed_packets"))
    seeds_returned = fc.roll_seed_return(rarity, rng)
    if seeds_returned > 0:
        seed_packets[crop_meta["key"]] = (
            int(seed_packets.get(crop_meta["key"], 0) or 0) + seeds_returned
        )
    # Reset plot
    p.update({
        "state": "empty", "crop_key": None, "planted_at": None,
        "ready_at": None, "watered_count": 0,
        "fertilizer_key": None, "fertilizer_mult": 1.0,
        "weather_seed": "clear",
        "mutation": None,
    })
    plots[plot_slot] = p
    # Farm-level XP: rarity-scaled per harvest. Stored as the cumulative
    # total on user_farming.farm_xp so level_from_xp can recompute it
    # cheaply at every read (mirrors fishing's fish_xp/fish_level pair).
    xp_gain = float(fc.farm_xp(crop_meta["key"]))
    new_total_xp = float(state.get("farm_xp") or 0.0) + xp_gain
    new_level = fc.level_from_xp(new_total_xp)
    await db.execute(
        """
        UPDATE user_farming SET
            plots = $3::jsonb,
            crop_inventory = $4::jsonb,
            seed_packets = $10::jsonb,
            total_harvested = total_harvested + 1,
            total_crops_grown_raw = total_crops_grown_raw + $5,
            total_seed_earned_raw = total_seed_earned_raw + $6::numeric,
            biggest_harvest_crop = CASE WHEN $5 > biggest_harvest_qty THEN $7 ELSE biggest_harvest_crop END,
            biggest_harvest_qty = GREATEST(biggest_harvest_qty, $5),
            biggest_harvest_at = CASE WHEN $5 > biggest_harvest_qty THEN NOW() ELSE biggest_harvest_at END,
            last_harvest_at = NOW(),
            last_action_at = NOW(),
            farm_xp = $8::numeric,
            farm_level = GREATEST(farm_level, $9),
            combo_step = $11,
            updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(plots), _json(inv),
        int(qty), int(seed_raw), crop_meta["key"],
        float(new_total_xp), int(new_level),
        _json(seed_packets), int(new_combo_step),
    )
    await record_harvest(
        db, guild_id, user_id,
        crop_key=crop_meta["key"], rarity=rarity, qty=qty,
        seed_earned_raw=seed_raw, hrv_earned_raw=0,
        zone=str(state.get("current_zone") or fc.DEFAULT_ZONE),
        plot_tier=int(state.get("plot_tier") or 1),
        fertilizer_key=p.get("fertilizer_key"),
        weather=weather_key,
    )
    # NFT layer sync: mint one crop token per harvested unit. Best-effort.
    try:
        from services import items as _items
        addr = _items.contract_address("crop", str(crop_meta["key"]))
        for unit_n in range(int(qty)):
            await _items.mint_unit(
                db,
                guild_id=guild_id,
                contract_address=addr,
                owner_user_id=user_id,
                metadata={
                    "crop_key": str(crop_meta["key"]),
                    "rarity":   str(rarity or ""),
                    "weather":  str(weather_key or ""),
                },
                mint_source="farming.harvest",
                source_table="user_farming.crop_inventory",
                source_id=f"{user_id}:{crop_meta['key']}:harvest:{int(_now_ts())}:{unit_n}",
            )
    except Exception:
        log.debug(
            "nft farming harvest mint sync failed gid=%s uid=%s key=%s",
            guild_id, user_id, crop_meta.get("key"), exc_info=True,
        )
    # Bloomstone XP -- one harvest, optionally a legendary kicker. Pest kills
    # and recipe processing grant their own XP from those call sites.
    try:
        from services import themed_stones as _ts
        await _ts.grant_bloomstone_xp(
            db, user_id, guild_id,
            harvested=1,
            legendary=(rarity == "legendary"),
        )
    except Exception:
        log.debug(
            "farming: themed_stones.grant_bloomstone_xp harvest failed",
            exc_info=True,
        )
    return HarvestResult(
        plot_slot=plot_slot,
        crop_key=crop_meta["key"],
        rarity=rarity,
        qty=qty,
        seed_raw=seed_raw,
        hrv_raw=0,
        combo_mult=combo_mult_eff,
        combo_step=int(new_combo_step),
        weather=weather_key,
        fertilizer_key=p.get("fertilizer_key"),
        mutation=mutation_key,
        seed_packets_returned=int(seeds_returned),
        granted_badges=[],
    )


async def clear_plot(
    db: Any, guild_id: int, user_id: int, plot_slot: int,
) -> bool:
    """Force-empty a plot (used to clear a withered crop)."""
    state = await ensure_state(db, guild_id, user_id)
    plots = list(state.get("plots") or [])
    if plot_slot < 0 or plot_slot >= len(plots):
        return False
    plots[plot_slot] = {
        "slot": plot_slot, "state": "empty", "crop_key": None,
        "planted_at": None, "ready_at": None,
        "watered_count": 0, "fertilizer_key": None,
        "fertilizer_mult": 1.0, "weather_seed": "clear",
        "mutation": None,
    }
    await db.execute(
        "UPDATE user_farming SET plots = $3::jsonb, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(plots),
    )
    return True


# ============================================================================
#  Shop: buy_plot_tier / buy_fertilizer / buy_seed_packet
# ============================================================================
async def buy_plot_tier(
    db: Any, guild_id: int, user_id: int, target_tier: int,
) -> BuyResult:
    """Upgrade plot tier by exactly +1 from current. Costs HRV."""
    state = await ensure_state(db, guild_id, user_id)
    cur_tier = int(state.get("plot_tier") or 1)
    if target_tier != cur_tier + 1:
        raise ValueError(f"Buy plot tiers one at a time. Next is tier {cur_tier + 1}.")
    plot_meta = fc.plot_meta(target_tier)
    if not plot_meta:
        raise ValueError(f"Unknown plot tier: {target_tier}")
    cost_human = float(plot_meta.get("price_hrv") or 0.0)
    cost_raw = int(to_raw(cost_human)) if cost_human > 0 else 0
    if cost_raw > 0:
        wh = await db.get_wallet_holding(
            user_id, guild_id, fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
        )
        bal = int(wh["amount"]) if wh else 0
        if bal < cost_raw:
            raise ValueError(
                f"Need {cost_human:,.2f} HRV, have {to_human(bal):,.2f}."
            )
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL, -cost_raw,
        )
    new_count = int(fc.PLOT_COUNT_BY_TIER.get(target_tier, 4))
    # Append empty plot dicts up to the new count.
    plots = list(state.get("plots") or [])
    while len(plots) < new_count:
        plots.append({
            "slot": len(plots), "state": "empty", "crop_key": None,
            "planted_at": None, "ready_at": None,
            "watered_count": 0, "fertilizer_key": None,
            "fertilizer_mult": 1.0, "weather_seed": "clear",
            "mutation": None,
        })
    await db.execute(
        """
        UPDATE user_farming SET
            plot_tier = $3,
            plot_count = $4,
            plots = $5::jsonb,
            updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, target_tier, new_count, _json(plots),
    )
    return BuyResult(
        kind="plot",
        key=plot_meta["key"],
        qty=1,
        hrv_spent_raw=cost_raw,
        new_tier=target_tier,
        ok=True,
        msg=(
            f"Upgraded to {plot_meta['emoji']} **{plot_meta['name']}** "
            f"(tier {target_tier}, {new_count} plots)."
        ),
    )


async def buy_fertilizer(
    db: Any, guild_id: int, user_id: int, fert_key: str, qty: int,
) -> BuyResult:
    """Buy ``qty`` of a fertilizer. Caps to max_stack of (existing + new)."""
    qty = max(1, int(qty or 1))
    fmeta = fc.fertilizer_meta(fert_key)
    if not fmeta:
        raise ValueError(f"Unknown fertilizer: {fert_key}")
    state = await ensure_state(db, guild_id, user_id)
    fert_inv = _as_dict(state.get("fertilizer_inventory"))
    have = int(fert_inv.get(fmeta["key"], 0) or 0)
    cap = int(fmeta.get("max_stack", 50))
    can_take = max(0, cap - have)
    actual = min(qty, can_take)
    if actual <= 0:
        raise ValueError(f"You're maxed on {fmeta['name']} ({have}/{cap}).")
    cost_human = float(fmeta["price_hrv"]) * actual
    cost_raw = int(to_raw(cost_human))
    wh = await db.get_wallet_holding(
        user_id, guild_id, fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
    )
    bal = int(wh["amount"]) if wh else 0
    if bal < cost_raw:
        raise ValueError(
            f"Need {cost_human:,.2f} HRV for {actual} {fmeta['name']}, "
            f"have {to_human(bal):,.2f}."
        )
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL, -cost_raw,
    )
    fert_inv[fmeta["key"]] = have + actual
    await db.execute(
        "UPDATE user_farming SET fertilizer_inventory = $3::jsonb, "
        "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(fert_inv),
    )
    return BuyResult(
        kind="fertilizer", key=fmeta["key"], qty=actual,
        hrv_spent_raw=cost_raw, new_tier=0, ok=True,
        msg=f"Bought {actual} {fmeta['emoji']} **{fmeta['name']}**.",
    )


async def buy_seed_packet(
    db: Any, guild_id: int, user_id: int, crop_key: str, qty: int,
) -> BuyResult:
    """Buy seed packets. Price = crop's hrv_sell_price * 0.20 each
    (so the seed cost is roughly 1/5 of one harvest's resale value).
    """
    qty = max(1, int(qty or 1))
    cmeta = fc.crop_meta(crop_key)
    if not cmeta:
        raise ValueError(f"Unknown crop: {crop_key}")
    price_each = float(cmeta["hrv_sell_price"]) * 0.20
    cost_human = price_each * qty
    cost_raw = int(to_raw(cost_human))
    state = await ensure_state(db, guild_id, user_id)
    wh = await db.get_wallet_holding(
        user_id, guild_id, fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
    )
    bal = int(wh["amount"]) if wh else 0
    if bal < cost_raw:
        raise ValueError(
            f"Need {cost_human:,.2f} HRV for {qty} {cmeta['name']} packets, "
            f"have {to_human(bal):,.2f}."
        )
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL, -cost_raw,
    )
    packets = _as_dict(state.get("seed_packets"))
    packets[cmeta["key"]] = int(packets.get(cmeta["key"], 0) or 0) + qty
    await db.execute(
        "UPDATE user_farming SET seed_packets = $3::jsonb, "
        "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(packets),
    )
    return BuyResult(
        kind="seed_packet", key=cmeta["key"], qty=qty,
        hrv_spent_raw=cost_raw, new_tier=0, ok=True,
        msg=f"Bought {qty}x {cmeta['emoji']} **{cmeta['name']}** seed packets.",
    )


# ============================================================================
#  Inventory + market: inventory_summary / sell_crop / process_recipe / sell_processed
# ============================================================================
def inventory_summary(state: dict) -> dict:
    """Return a structured summary of the user's farm inventory.

    Renders by category for the cog's `,farm bag` embed:
    - crops: [{key, name, emoji, count, rarity, hrv_each}]
    - processed: [{key, name, emoji, count, hrv_each}]
    - fertilizer: [{key, name, emoji, count}]
    - seed_packets: [{key, name, emoji, count, rarity}]
    Plus aggregate totals.
    """
    crops_inv = _as_dict(state.get("crop_inventory"))
    processed_inv = _as_dict(state.get("processed_inventory"))
    fert_inv = _as_dict(state.get("fertilizer_inventory"))
    packets = _as_dict(state.get("seed_packets"))

    crop_lines: list[dict] = []
    crop_total = 0
    for k, v in crops_inv.items():
        cnt = int(v or 0)
        if cnt <= 0:
            continue
        meta = fc.crop_meta(k) or {}
        crop_lines.append({
            "key": k, "name": meta.get("name", k.title()),
            "emoji": meta.get("emoji", ""), "count": cnt,
            "rarity": meta.get("rarity", "common"),
            "hrv_each": float(meta.get("hrv_sell_price", 0.0)),
        })
        crop_total += cnt

    proc_lines: list[dict] = []
    proc_total = 0
    for k, v in processed_inv.items():
        cnt = int(v or 0)
        if cnt <= 0:
            continue
        meta = fc.recipe_meta(k) or {}
        proc_lines.append({
            "key": k, "name": meta.get("name", k.title()),
            "emoji": meta.get("emoji", ""), "count": cnt,
            "hrv_each": float(meta.get("hrv_sell_price", 0.0)),
        })
        proc_total += cnt

    fert_lines: list[dict] = []
    for k, v in fert_inv.items():
        cnt = int(v or 0)
        if cnt <= 0:
            continue
        meta = fc.fertilizer_meta(k) or {}
        fert_lines.append({
            "key": k, "name": meta.get("name", k.title()),
            "emoji": meta.get("emoji", ""), "count": cnt,
        })

    packet_lines: list[dict] = []
    for k, v in packets.items():
        cnt = int(v or 0)
        if cnt <= 0:
            continue
        meta = fc.crop_meta(k) or {}
        packet_lines.append({
            "key": k, "name": meta.get("name", k.title()),
            "emoji": meta.get("emoji", ""), "count": cnt,
            "rarity": meta.get("rarity", "common"),
        })

    crop_lines.sort(key=lambda x: -x["count"])
    proc_lines.sort(key=lambda x: -x["count"])
    return {
        "crops": crop_lines,        "crops_total": crop_total,
        "processed": proc_lines,    "processed_total": proc_total,
        "fertilizer": fert_lines,
        "seed_packets": packet_lines,
    }


async def sell_crop(
    db: Any, guild_id: int, user_id: int,
    target: str, qty: int | None = None,
) -> SellCropResult:
    """Sell raw crops to the market for HRV at the catalog hrv_sell_price.

    target supports:
    - 'all'  -> sell every raw crop
    - 'junk' -> sell only commons + uncommons
    - <crop_key> -> sell specific crop (qty optional; default = all of it)

    No slippage curve here yet (mirrors fishing.sell_inventory's flat
    rate). Crops sell at the catalog hrv_sell_price.
    """
    state = await ensure_state(db, guild_id, user_id)
    inv = _as_dict(state.get("crop_inventory"))
    target = (target or "").strip().lower()
    sold_keys: list[tuple[str, int]] = []
    if target == "all":
        sold_keys = [(k, int(v or 0)) for k, v in inv.items() if int(v or 0) > 0]
    elif target == "junk":
        for k, v in inv.items():
            cnt = int(v or 0)
            if cnt <= 0:
                continue
            meta = fc.crop_meta(k)
            if meta and meta.get("rarity") in ("common", "uncommon"):
                sold_keys.append((k, cnt))
    else:
        meta = fc.crop_meta(target)
        if not meta:
            raise ValueError(f"Unknown crop: {target}")
        cnt = int(inv.get(meta["key"], 0) or 0)
        if cnt <= 0:
            raise ValueError(f"You don't have any {meta['name']} to sell.")
        sell_qty = min(int(qty or cnt), cnt)
        if sell_qty <= 0:
            raise ValueError("Nothing to sell.")
        sold_keys = [(meta["key"], sell_qty)]
    if not sold_keys:
        raise ValueError("Nothing to sell.")
    total_hrv_human = 0.0
    total_qty = 0
    last_key = ""
    for k, n in sold_keys:
        meta = fc.crop_meta(k) or {}
        price = float(meta.get("hrv_sell_price", 0.0))
        total_hrv_human += price * n
        total_qty += n
        inv[k] = int(inv.get(k, 0) or 0) - n
        last_key = k
    # Farm-level payout multiplier: +1% per farm_level above 1, mirrors
    # the fishing level_payout_mult curve so the two surfaces feel
    # identical in shape.
    farm_lvl = int(state.get("farm_level") or 1)
    total_hrv_human *= fc.level_payout_mult(farm_lvl)
    total_hrv_raw = int(to_raw(round(total_hrv_human, 2)))
    if total_hrv_raw > 0:
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL, total_hrv_raw,
        )
    await db.execute(
        """
        UPDATE user_farming SET
            crop_inventory = $3::jsonb,
            total_hrv_earned_raw = total_hrv_earned_raw + $4::numeric,
            updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(inv), int(total_hrv_raw),
    )
    # NFT layer sync: burn one crop token per unit sold. Best-effort.
    try:
        from services import items as _items
        for k, n in sold_keys:
            for _ in range(int(n)):
                await _items.consume_one(
                    db,
                    guild_id=guild_id, user_id=user_id,
                    contract_address=_items.contract_address("crop", str(k)),
                    reason="farming.sell",
                )
    except Exception:
        log.debug(
            "nft farming sell burn sync failed gid=%s uid=%s",
            guild_id, user_id, exc_info=True,
        )
    return SellCropResult(
        crop_or_recipe_key=last_key if len(sold_keys) == 1 else target,
        qty_sold=total_qty,
        hrv_received_raw=total_hrv_raw,
        slippage_pct=0.0,
    )


async def process_recipe(
    db: Any, guild_id: int, user_id: int,
    recipe_key: str, qty: int = 1,
) -> ProcessResult:
    """Combine raw crops into a recipe output. Consumes the `requires`
    bundle qty times, adds output to processed_inventory, credits a
    SEED bonus from seed_yield_bonus_min..max per craft.
    """
    qty = max(1, int(qty or 1))
    rmeta = fc.recipe_meta(recipe_key)
    if not rmeta:
        raise ValueError(f"Unknown recipe: {recipe_key}")
    state = await ensure_state(db, guild_id, user_id)
    inv = _as_dict(state.get("crop_inventory"))
    requires = dict(rmeta.get("requires") or {})
    for k, need in requires.items():
        have = int(inv.get(k, 0) or 0)
        if have < int(need) * qty:
            cmeta = fc.crop_meta(k) or {}
            raise ValueError(
                f"Need {int(need) * qty}x {cmeta.get('emoji','')} "
                f"**{cmeta.get('name', k)}**, have {have}."
            )
    for k, need in requires.items():
        inv[k] = int(inv.get(k, 0) or 0) - int(need) * qty
    proc = _as_dict(state.get("processed_inventory"))
    proc[rmeta["key"]] = int(proc.get(rmeta["key"], 0) or 0) + (int(rmeta.get("output_qty") or 1) * qty)
    rng = random.Random()
    seed_bonus_human = float(rng.uniform(
        float(rmeta["seed_yield_bonus_min"]),
        float(rmeta["seed_yield_bonus_max"]),
    )) * qty
    seed_bonus_raw = int(to_raw(round(seed_bonus_human, 2)))
    if seed_bonus_raw > 0:
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL, seed_bonus_raw,
        )
    await db.execute(
        """
        UPDATE user_farming SET
            crop_inventory = $3::jsonb,
            processed_inventory = $4::jsonb,
            total_seed_earned_raw = total_seed_earned_raw + $5::numeric,
            updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(inv), _json(proc), int(seed_bonus_raw),
    )
    # NFT layer sync: burn one crop token per consumed unit (recipes
    # consume raw crops). Best-effort.
    try:
        from services import items as _items
        for k, need in requires.items():
            for _ in range(int(need) * int(qty)):
                await _items.consume_one(
                    db,
                    guild_id=guild_id, user_id=user_id,
                    contract_address=_items.contract_address("crop", str(k)),
                    reason="farming.process",
                )
    except Exception:
        log.debug(
            "nft farming process burn sync failed gid=%s uid=%s",
            guild_id, user_id, exc_info=True,
        )
    try:
        from services import themed_stones as _ts
        await _ts.grant_bloomstone_xp(db, user_id, guild_id, recipe=qty)
    except Exception:
        log.debug(
            "farming: themed_stones.grant_bloomstone_xp recipe failed",
            exc_info=True,
        )
    return ProcessResult(
        recipe_key=rmeta["key"],
        qty_made=int(rmeta.get("output_qty") or 1) * qty,
        seed_bonus_raw=seed_bonus_raw,
        ok=True,
        msg=f"Made {int(rmeta.get('output_qty') or 1) * qty}x {rmeta['emoji']} **{rmeta['name']}**.",
    )


async def sell_processed(
    db: Any, guild_id: int, user_id: int,
    recipe_key: str, qty: int | None = None,
) -> SellCropResult:
    """Sell processed goods at recipe.hrv_sell_price."""
    rmeta = fc.recipe_meta(recipe_key)
    if not rmeta:
        raise ValueError(f"Unknown recipe: {recipe_key}")
    state = await ensure_state(db, guild_id, user_id)
    proc = _as_dict(state.get("processed_inventory"))
    have = int(proc.get(rmeta["key"], 0) or 0)
    if have <= 0:
        raise ValueError(f"You don't have any {rmeta['name']}.")
    sell_qty = min(int(qty or have), have)
    if sell_qty <= 0:
        raise ValueError("Nothing to sell.")
    hrv_human = float(rmeta["hrv_sell_price"]) * sell_qty
    hrv_raw = int(to_raw(round(hrv_human, 2)))
    proc[rmeta["key"]] = have - sell_qty
    if hrv_raw > 0:
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL, hrv_raw,
        )
    await db.execute(
        """
        UPDATE user_farming SET
            processed_inventory = $3::jsonb,
            total_hrv_earned_raw = total_hrv_earned_raw + $4::numeric,
            updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(proc), int(hrv_raw),
    )
    return SellCropResult(
        crop_or_recipe_key=rmeta["key"],
        qty_sold=sell_qty,
        hrv_received_raw=hrv_raw,
        slippage_pct=0.0,
    )


# ============================================================================
#  Token economy: burn_seed_for_hrv / cashout_hrv
# ============================================================================

async def burn_seed_for_hrv(
    db: Any, guild_id: int, user_id: int, amt_raw: int,
) -> BurnResult:
    """Burn SEED, mint HRV, push both oracles by the standard impact formula.

    Conversion: USD value at the live SEED oracle is preserved into HRV
    at the live HRV oracle. After the trade the SEED oracle drops by
    ``impact`` (sell-pressure + supply contraction) and the HRV oracle
    drops as well (mint pressure -- extra supply). The chart picks both
    up via ``crypto_prices.update_price``.

    No fixed rate, no minimum, no fee that disappears into thin air --
    the slippage IS the fee.
    """
    if amt_raw <= 0:
        raise ValueError("Amount must be positive.")

    held = await db.get_wallet_holding(
        user_id, guild_id, fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
    )
    held_raw = int((held or {}).get("amount") or 0)
    if held_raw < int(amt_raw):
        raise ValueError(f"You only have {to_human(held_raw):,.4f} SEED.")

    seed_oracle_before = await _oracle_price(db, guild_id, fc.SEED_SYMBOL)
    hrv_oracle_before = await _oracle_price(db, guild_id, fc.HRV_SYMBOL)
    if seed_oracle_before <= 0 or hrv_oracle_before <= 0:
        raise ValueError("Oracle price is currently zero -- try again in a moment.")

    seed_human = to_human(int(amt_raw))
    usd_value = seed_human * seed_oracle_before

    rows = await db.fetch_all(
        "SELECT symbol, circulating_supply FROM crypto_prices "
        "WHERE guild_id = $1 AND symbol = ANY($2::text[])",
        int(guild_id), [fc.SEED_SYMBOL, fc.HRV_SYMBOL],
    )
    supply: dict[str, float] = {}
    for r in (rows or []):
        supply[str(r["symbol"]).upper()] = to_human(int(r.get("circulating_supply") or 0))

    seed_impact = _price_impact(usd_value, seed_oracle_before, supply.get(fc.SEED_SYMBOL, 0.0))
    hrv_impact = _price_impact(usd_value, hrv_oracle_before, supply.get(fc.HRV_SYMBOL, 0.0))

    # Effective HRV price accounts for mint slippage.
    eff_hrv_price = hrv_oracle_before * (1.0 + hrv_impact / 2.0)
    hrv_minted_human = usd_value / max(1e-12, eff_hrv_price)
    hrv_minted_raw = to_raw(hrv_minted_human)
    if hrv_minted_raw <= 0:
        raise ValueError("Burn produces zero HRV -- raise the SEED amount.")

    # Burn SEED from wallet.
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
        -int(amt_raw),
    )
    try:
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
            int(hrv_minted_raw),
        )
    except Exception:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
                int(amt_raw),
            )
        except Exception:
            log.exception("burn_seed_for_hrv: refund of SEED also failed "
                          "uid=%s gid=%s amt=%s", user_id, guild_id, amt_raw)
        raise

    seed_oracle_after = max(1e-9, seed_oracle_before * (1.0 - seed_impact))
    hrv_oracle_after = max(1e-9, hrv_oracle_before * (1.0 + hrv_impact))
    try:
        await db.update_price(fc.SEED_SYMBOL, guild_id, seed_oracle_after)
        await db.update_price(fc.HRV_SYMBOL, guild_id, hrv_oracle_after)
    except Exception:
        log.exception(
            "burn_seed_for_hrv: oracle update failed gid=%s -- chart will "
            "lag until the next drift tick", guild_id,
        )

    await _write_burn_candle(
        db, guild_id, fc.SEED_SYMBOL,
        seed_oracle_before, seed_oracle_after, usd_value,
    )
    await _write_burn_candle(
        db, guild_id, fc.HRV_SYMBOL,
        hrv_oracle_before, hrv_oracle_after, usd_value,
    )

    fee_usd = usd_value * (int(fc.GEAR_BURN_LP_REWARD_BPS) / 10_000.0)
    if fee_usd > 0:
        await _distribute_burn_lp_reward(db, guild_id, fc.SEED_SYMBOL, fee_usd / 2.0)
        await _distribute_burn_lp_reward(db, guild_id, fc.HRV_SYMBOL, fee_usd / 2.0)

    await db.execute(
        """
        UPDATE user_farming
           SET total_hrv_earned_raw = total_hrv_earned_raw + $3::numeric,
               updated_at           = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(hrv_minted_raw),
    )

    return BurnResult(
        burned_seed_raw=int(amt_raw),
        minted_hrv_raw=int(hrv_minted_raw),
        impact_pct=float(max(seed_impact, hrv_impact)),
    )


async def cashout_hrv(
    db: Any, guild_id: int, user_id: int, amt_raw: int,
) -> CashoutResult:
    """Burn HRV, push the HRV oracle DOWN, credit users.wallet with USD.

    Identical mechanics to ``cogs/trade.py .sell``: the full quantity
    leaves the user's wallet (decrementing crypto_prices.circulating_supply
    via update_wallet_holding, which IS the burn), the standard
    _price_impact formula computes a downward price impact, and the user
    receives USD at the post-impact HRV oracle price.

    No fixed haircut, no minimum -- the slippage IS the fee.
    """
    if amt_raw <= 0:
        raise ValueError("Amount must be positive.")

    held = await db.get_wallet_holding(
        user_id, guild_id, fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
    )
    held_raw = int((held or {}).get("amount") or 0)
    if held_raw < int(amt_raw):
        raise ValueError(f"You only have {to_human(held_raw):,.4f} HRV.")

    hrv_oracle_before = await _oracle_price(db, guild_id, fc.HRV_SYMBOL)
    if hrv_oracle_before <= 0:
        raise ValueError("HRV oracle price is currently zero -- try again later.")

    hrv_human = to_human(int(amt_raw))
    revenue_usd = hrv_human * hrv_oracle_before

    row = await db.fetch_one(
        "SELECT circulating_supply FROM crypto_prices "
        "WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), fc.HRV_SYMBOL,
    )
    supply_human = to_human(int((row or {}).get("circulating_supply") or 0))
    impact = _price_impact(revenue_usd, hrv_oracle_before, supply_human)

    eff_price = hrv_oracle_before * (1.0 - impact / 2.0)
    usd_credit_human = hrv_human * eff_price

    # Group Industry bonus: members of a group with a farming-bonus
    # upgrade (Greenhouse Wing / Guild Market / Master Industries) earn
    # the bonus on every farming cashout, anywhere.
    try:
        from services.group_reserve import member_activity_bonus
        _farming_bonus = await member_activity_bonus(db, guild_id, user_id, "farming")
    except Exception:
        log.debug("group farming bonus probe failed", exc_info=True)
        _farming_bonus = 0.0
    if _farming_bonus > 0:
        usd_credit_human *= (1.0 + _farming_bonus)

    usd_credit_raw = to_raw(usd_credit_human)

    # Burn HRV first; refund on credit failure.
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
        -int(amt_raw),
    )
    if usd_credit_raw > 0:
        try:
            await db.update_wallet(user_id, guild_id, int(usd_credit_raw))
        except Exception:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id,
                    fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
                    int(amt_raw),
                )
            except Exception:
                log.exception("cashout_hrv: HRV refund failed uid=%s gid=%s amt=%s",
                              user_id, guild_id, amt_raw)
            raise

    hrv_oracle_after = max(1e-9, hrv_oracle_before * (1.0 - impact))
    try:
        await db.update_price(fc.HRV_SYMBOL, guild_id, hrv_oracle_after)
    except Exception:
        log.exception(
            "cashout_hrv: oracle update failed gid=%s -- chart will lag "
            "until the next drift tick", guild_id,
        )

    await _write_burn_candle(
        db, guild_id, fc.HRV_SYMBOL,
        hrv_oracle_before, hrv_oracle_after, revenue_usd,
    )

    fee_usd = revenue_usd * (int(fc.GEAR_BURN_LP_REWARD_BPS) / 10_000.0)
    if fee_usd > 0:
        await _distribute_burn_lp_reward(db, guild_id, fc.HRV_SYMBOL, fee_usd)

    # Group reserve tribute: system-funded grant on the gross USD value
    # of the cashout. The user's payout is unaffected.
    try:
        from services.group_reserve import tribute_from_activity
        await tribute_from_activity(
            db, guild_id, user_id, float(revenue_usd), "farming",
        )
    except Exception:
        log.debug("group farming tribute failed", exc_info=True)

    await db.execute(
        """
        UPDATE user_farming
           SET total_usd_cashout_raw = total_usd_cashout_raw + $3::numeric,
               updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(usd_credit_raw),
    )
    return CashoutResult(
        burned_hrv_raw=int(amt_raw),
        paid_usd_raw=int(usd_credit_raw),
        impact_pct=float(impact),
    )


# ============================================================================
#  Stake / yield (mirrors fishing's LURE-stake block)
# ============================================================================

def _accrue_pending(staked_raw: int, last_at: Any) -> tuple[int, int]:
    """Return ``(elapsed_seconds, accrued_hrv_raw)`` for a SEED stake position.

    ``last_at`` may be a datetime, an epoch float (per the project's _coerce
    convention), or None. Using ``time.time()`` for the diff keeps this pure
    Python and avoids datetime subtraction edge cases. The DB clock is the
    source of truth -- callers always re-read after writes -- so a small
    drift between Python and DB clocks is acceptable for a passive yield.
    """
    if staked_raw <= 0 or not last_at:
        return 0, 0
    if isinstance(last_at, _dt.datetime):
        last_ts = last_at.timestamp()
    else:
        last_ts = float(last_at)
    now_ts = float(_time.time())
    elapsed = max(0, int(now_ts - last_ts))
    if elapsed <= 0:
        return 0, 0
    # accrued = staked * rate_per_day * elapsed_days
    # Doing this in raw space: staked_raw is already scaled by 10**18, the
    # rate is dimensionless, divide by SECS_PER_DAY at the end.
    rate_raw = to_raw(fc.SEED_STAKE_HRV_PER_DAY)
    accrued_raw = (staked_raw * rate_raw * elapsed) // (to_raw(1.0) * 86400)
    return elapsed, int(accrued_raw)


async def accrued_stake_yield(
    db: Any, guild_id: int, user_id: int,
) -> int:
    """Read-only: how much HRV would be claimable right now (raw)."""
    state = await list_state(db, guild_id, user_id)
    staked = int(state.get("seed_staked_raw") or 0)
    pending = int(state.get("hrv_yield_pending_raw") or 0)
    _, fresh = _accrue_pending(staked, state.get("last_stake_yield_at"))
    return pending + fresh


async def stake_seed(
    db: Any, guild_id: int, user_id: int, amt_raw: int,
) -> StakeResult:
    """Move SEED from wallet -> stake. Crystallises any pending HRV yield first.

    Crystallising on every write keeps the math simple: ``last_stake_yield_at``
    only ever measures uninterrupted accrual on the CURRENT staked balance.

    No minimum -- staking 1 SEED for a day is just as valid as staking
    a million. Yield scales linearly with the staked balance.
    """
    if amt_raw <= 0:
        raise ValueError("Amount must be positive.")

    state = await ensure_state(db, guild_id, user_id)
    cur_staked = int(state.get("seed_staked_raw") or 0)
    pending = int(state.get("hrv_yield_pending_raw") or 0)
    _, fresh = _accrue_pending(cur_staked, state.get("last_stake_yield_at"))
    new_pending = pending + fresh

    # Deduct SEED from wallet (raises ValueError on insufficient).
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
        -int(amt_raw),
    )
    new_staked = cur_staked + int(amt_raw)
    await db.execute(
        """
        UPDATE user_farming
           SET seed_staked_raw        = $3::numeric,
               hrv_yield_pending_raw  = $4::numeric,
               last_stake_yield_at    = NOW(),
               updated_at             = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_staked), int(new_pending),
    )
    return StakeResult(
        staked_now_raw=int(amt_raw),
        total_staked_raw=int(new_staked),
        paid_yield_raw=0,
    )


async def claim_stake_yield(
    db: Any, guild_id: int, user_id: int,
) -> StakeResult:
    """Pay out accrued HRV to the user's wallet. Stake stays locked.

    Resets the accrual clock to NOW(). Returns the post-op stake balance
    plus the HRV paid out (so the cog can render a receipt).
    """
    state = await ensure_state(db, guild_id, user_id)
    cur_staked = int(state.get("seed_staked_raw") or 0)
    pending = int(state.get("hrv_yield_pending_raw") or 0)
    _, fresh = _accrue_pending(cur_staked, state.get("last_stake_yield_at"))
    payout = pending + fresh
    if payout <= 0:
        raise ValueError(
            "No HRV has accrued yet. Try again after some time has passed."
        )

    await db.update_wallet_holding(
        user_id, guild_id,
        fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
        int(payout),
    )
    await db.execute(
        """
        UPDATE user_farming
           SET hrv_yield_pending_raw  = 0,
               last_stake_yield_at    = NOW(),
               total_hrv_earned_raw   = total_hrv_earned_raw + $3::numeric,
               updated_at             = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(payout),
    )
    return StakeResult(
        staked_now_raw=0,
        total_staked_raw=int(cur_staked),
        paid_yield_raw=int(payout),
    )


async def unstake_seed(
    db: Any, guild_id: int, user_id: int, amt_raw: int,
) -> StakeResult:
    """Move SEED from stake -> wallet. Crystallises and pays accrued HRV.

    ``amt_raw`` is capped at the user's current staked balance so the
    cog can pass a sentinel like ``2**62`` to mean "all of it". Always pays
    out any accrued HRV alongside the unlocked SEED so the user never
    loses pending yield by unstaking.
    """
    state = await ensure_state(db, guild_id, user_id)
    cur_staked = int(state.get("seed_staked_raw") or 0)
    pending = int(state.get("hrv_yield_pending_raw") or 0)
    _, fresh = _accrue_pending(cur_staked, state.get("last_stake_yield_at"))
    payout = pending + fresh

    requested = max(0, int(amt_raw))
    if cur_staked <= 0 or requested <= 0:
        raise ValueError("You have no SEED staked.")
    actual = min(requested, cur_staked)
    new_staked = cur_staked - actual

    # Credit unlocked SEED first; if that fails, the row stays unchanged.
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
        int(actual),
    )
    if payout > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
                int(payout),
            )
        except Exception:
            log.exception("unstake_seed: HRV yield payout failed uid=%s gid=%s",
                          user_id, guild_id)
            payout = 0  # don't credit ledger if the wallet write failed
    await db.execute(
        """
        UPDATE user_farming
           SET seed_staked_raw        = $3::numeric,
               hrv_yield_pending_raw  = 0,
               last_stake_yield_at    = NOW(),
               total_hrv_earned_raw   = total_hrv_earned_raw + $4::numeric,
               updated_at             = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_staked), int(payout),
    )
    return StakeResult(
        staked_now_raw=-int(actual),
        total_staked_raw=int(new_staked),
        paid_yield_raw=int(payout),
    )


# ============================================================================
#  Pest battle (gated on locusts / blood_moon weather)
# ============================================================================
async def maybe_spawn_pest(
    db: Any, guild_id: int, user_id: int, plot_slot: int,
    rng: random.Random | None = None,
) -> dict | None:
    """Roll a pest spawn for a growing plot if the current weather allows.

    Returns the pest_state dict (and writes it onto plots[slot].pest_state),
    or None if no spawn. Cog uses the returned dict to flip its view to
    a battle layout. Caller should call this opportunistically from
    water_plot / harvest attempts -- or on a tick.
    """
    rng = rng or random.Random()
    state = await ensure_state(db, guild_id, user_id)
    plots = list(state.get("plots") or [])
    if plot_slot < 0 or plot_slot >= len(plots):
        return None
    p = dict(plots[plot_slot])
    if p.get("state") not in ("growing", "ready"):
        return None
    if p.get("pest_state"):
        return p["pest_state"]
    weather = str(state.get("current_weather") or "clear")
    pest_key = fc.pick_pest_for_zone(
        str(state.get("current_zone") or fc.DEFAULT_ZONE),
        weather, rng,
    )
    if not pest_key:
        return None
    meta = fc.pest_meta(pest_key) or {}
    pest_state = {
        "key": pest_key,
        "hp": int(meta.get("hp", 10)),
        "max_hp": int(meta.get("hp", 10)),
        "atk": int(meta.get("atk", 3)),
        "boss": bool(meta.get("boss", False)),
    }
    p["pest_state"] = pest_state
    plots[plot_slot] = p
    await db.execute(
        "UPDATE user_farming SET plots = $3::jsonb, last_action_at = NOW(), "
        "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(plots),
    )
    return pest_state


async def resolve_pest_battle(
    db: Any, guild_id: int, user_id: int,
    plot_slot: int, action: str,
) -> PestBattleResolution:
    """Resolve one round of a pest battle. action in 'attack', 'capture', 'flee'.

    Player atk = 12 (flat), max_hp = 60 (flat -- farming doesn't have a
    character sheet). Returns a PestBattleResolution describing what
    happened. The cog handles the cc_buddies insert on capture.
    """
    state = await ensure_state(db, guild_id, user_id)
    plots = list(state.get("plots") or [])
    if plot_slot < 0 or plot_slot >= len(plots):
        raise ValueError("Plot slot out of range.")
    p = dict(plots[plot_slot])
    pest = p.get("pest_state")
    if not pest:
        raise ValueError("No pest in this plot.")
    log: list[str] = []
    rng = random.Random()
    PLAYER_ATK = 12
    pest_meta = fc.pest_meta(str(pest.get("key", ""))) or {}
    pest_name = pest_meta.get("name", str(pest.get("key", "?")))
    pest_emoji = pest_meta.get("emoji", "")

    if action == "flee":
        # 75% chance to flee a small pest, 35% boss
        flee_chance = 0.35 if pest.get("boss") else 0.75
        if rng.random() < flee_chance:
            p["pest_state"] = None
            plots[plot_slot] = p
            await db.execute(
                "UPDATE user_farming SET plots = $3::jsonb, last_action_at = NOW(), "
                "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id, _json(plots),
            )
            log.append(f"You flee from {pest_emoji} **{pest_name}**.")
            return PestBattleResolution(
                outcome="player_fled", pest_key=str(pest.get("key", "")),
                captured=False, seed_drop_raw=0, log=log,
                pest_state=None, is_boss=bool(pest.get("boss")),
            )
        log.append(f"Your escape fails! {pest_emoji} **{pest_name}** keeps after you.")
        # Pest gets a free hit
        # fall through to a hit-trade

    if action == "capture":
        # Roll capture; chance scales by missing HP fraction
        max_hp = max(1, int(pest.get("max_hp", 1)))
        hp = max(0, int(pest.get("hp", 0)))
        damage_frac = 1.0 - (hp / max_hp)
        base = float(pest_meta.get("capture_chance", 0.10))
        # Capture chance = base * (0.5 + 0.5 * damage_frac); never above 0.95
        chance = min(0.95, base * (0.5 + 0.5 * damage_frac) * 2.0)
        if rng.random() < chance:
            seed_human = float(rng.uniform(
                float(pest_meta.get("drop_seed_min", 1)),
                float(pest_meta.get("drop_seed_max", 5)),
            ))
            seed_raw = int(to_raw(round(seed_human, 2)))
            if seed_raw > 0:
                await db.update_wallet_holding(
                    user_id, guild_id,
                    fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL, seed_raw,
                )
            p["pest_state"] = None
            plots[plot_slot] = p
            await db.execute(
                """
                UPDATE user_farming SET
                    plots = $3::jsonb,
                    last_action_at = NOW(),
                    updated_at = NOW()
                 WHERE guild_id = $1 AND user_id = $2
                """,
                guild_id, user_id, _json(plots),
            )
            await db.execute(
                """
                INSERT INTO farming_pest_battles
                    (guild_id, user_id, pest_key, outcome, captured,
                     seed_drop_raw, zone)
                VALUES ($1, $2, $3, 'pest_dead', TRUE, $4::numeric, $5)
                """,
                guild_id, user_id, str(pest.get("key", "")),
                int(seed_raw),
                str(state.get("current_zone") or fc.DEFAULT_ZONE),
            )
            log.append(f"\U0001F9F2 You captured {pest_emoji} **{pest_name}**!")
            return PestBattleResolution(
                outcome="pest_dead", pest_key=str(pest.get("key", "")),
                captured=True, seed_drop_raw=seed_raw, log=log,
                pest_state=None, is_boss=bool(pest.get("boss")),
            )
        log.append(f"\U0001F4A8 The {pest_name} slips your net!")
        # fall through: pest gets a turn

    # action == "attack" path (or fall-through from failed flee/capture)
    if action == "attack":
        # Player swing
        crit = rng.random() < 0.10
        dmg = PLAYER_ATK * (2 if crit else 1)
        pest["hp"] = max(0, int(pest.get("hp", 0)) - dmg)
        log.append(
            f"\U00002694 You hit {pest_emoji} **{pest_name}** for **{dmg}**"
            + ("  *crit*" if crit else "")
            + "."
        )
        if pest["hp"] <= 0:
            seed_human = float(rng.uniform(
                float(pest_meta.get("drop_seed_min", 1)),
                float(pest_meta.get("drop_seed_max", 5)),
            ))
            seed_raw = int(to_raw(round(seed_human, 2)))
            if seed_raw > 0:
                await db.update_wallet_holding(
                    user_id, guild_id,
                    fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL, seed_raw,
                )
            p["pest_state"] = None
            plots[plot_slot] = p
            await db.execute(
                "UPDATE user_farming SET plots = $3::jsonb, last_action_at = NOW(), "
                "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id, _json(plots),
            )
            await db.execute(
                """
                INSERT INTO farming_pest_battles
                    (guild_id, user_id, pest_key, outcome, captured,
                     seed_drop_raw, zone)
                VALUES ($1, $2, $3, 'pest_dead', FALSE, $4::numeric, $5)
                """,
                guild_id, user_id, str(pest.get("key", "")),
                int(seed_raw),
                str(state.get("current_zone") or fc.DEFAULT_ZONE),
            )
            log.append(f"{pest_emoji} **{pest_name}** falls. (+{seed_human:.2f} SEED)")
            return PestBattleResolution(
                outcome="pest_dead", pest_key=str(pest.get("key", "")),
                captured=False, seed_drop_raw=seed_raw, log=log,
                pest_state=None, is_boss=bool(pest.get("boss")),
            )

    # Pest counter-attack (only if it survived the player's swing or
    # the player failed flee/capture)
    pest_dmg = int(pest.get("atk", 1))
    log.append(f"{pest_emoji} **{pest_name}** bites back for **{pest_dmg}**.")
    p["pest_state"] = pest
    plots[plot_slot] = p
    await db.execute(
        "UPDATE user_farming SET plots = $3::jsonb, last_action_at = NOW(), "
        "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(plots),
    )
    return PestBattleResolution(
        outcome="continue", pest_key=str(pest.get("key", "")),
        captured=False, seed_drop_raw=0, log=log,
        pest_state=pest, is_boss=bool(pest.get("boss")),
    )


# ============================================================================
#  Leaderboards / queries
# ============================================================================
async def get_top_farmers(db: Any, guild_id: int, limit: int = 10) -> list[dict]:
    """Top farmers by lifetime HRV earned."""
    rows = await db.fetch_all(
        """
        SELECT user_id, total_hrv_earned_raw, total_seed_earned_raw,
               total_harvested, plot_tier, biggest_harvest_qty
        FROM user_farming
        WHERE guild_id = $1
        ORDER BY total_hrv_earned_raw DESC NULLS LAST
        LIMIT $2
        """,
        guild_id, int(limit),
    )
    return [dict(r) for r in (rows or [])]


async def get_biggest_harvests(db: Any, guild_id: int, limit: int = 10) -> list[dict]:
    """Top single-harvest qty rows from the farming_harvests log."""
    rows = await db.fetch_all(
        """
        SELECT user_id, crop_key, rarity, qty, seed_earned_raw,
               zone, plot_tier, weather, harvested_at
        FROM farming_harvests
        WHERE guild_id = $1 AND qty > 0
        ORDER BY qty DESC, harvested_at DESC
        LIMIT $2
        """,
        guild_id, int(limit),
    )
    return [dict(r) for r in (rows or [])]


async def get_user_harvests(
    db: Any, guild_id: int, user_id: int, limit: int = 20,
) -> list[dict]:
    """Most recent harvests for one user (for `,farm history`)."""
    rows = await db.fetch_all(
        """
        SELECT crop_key, rarity, qty, seed_earned_raw, hrv_earned_raw,
               zone, plot_tier, weather, harvested_at
        FROM farming_harvests
        WHERE guild_id = $1 AND user_id = $2
        ORDER BY harvested_at DESC
        LIMIT $3
        """,
        guild_id, user_id, int(limit),
    )
    return [dict(r) for r in (rows or [])]


# ============================================================================
#  Audit log writer
# ============================================================================

async def record_harvest(
    db: Any, guild_id: int, user_id: int, *,
    crop_key: str, rarity: str, qty: int,
    seed_earned_raw: int, hrv_earned_raw: int,
    zone: str, plot_tier: int,
    fertilizer_key: str | None, weather: str,
) -> None:
    """Append a row to farming_harvests for leaderboards + achievements."""
    await db.execute(
        """
        INSERT INTO farming_harvests
            (guild_id, user_id, crop_key, rarity, qty,
             seed_earned_raw, hrv_earned_raw, zone, plot_tier,
             fertilizer_key, weather)
        VALUES ($1, $2, $3, $4, $5, $6::numeric, $7::numeric, $8, $9, $10, $11)
        """,
        guild_id, user_id, str(crop_key), str(rarity or "common"), int(qty),
        int(seed_earned_raw), int(hrv_earned_raw),
        str(zone), int(plot_tier),
        fertilizer_key, str(weather or "clear"),
    )



# ============================================================================
#  Daily Contracts
# ============================================================================
#
# One rolling NPC contract per (user, guild) per UTC day. The roll is
# deterministic on (date, user_id, guild_id) so the same contract sticks
# even if the panel never opened. Contracts are stored on
# user_farming.daily_contract as a small JSON blob; turn-in burns crop
# inventory and credits HRV + SEED. Lifetime completions are tracked on
# total_contracts_completed for leaderboards + future badges.

@dataclass
class ContractView:
    """Read-only snapshot returned by ``get_daily_contract`` for the cog."""
    contract:    dict
    fresh_today: bool
    can_turn_in: bool
    have:        int


@dataclass
class ContractTurnInResult:
    crop_key:        str
    qty_turned_in:   int
    hrv_paid_raw:    int
    seed_paid_raw:   int
    completed:       bool


async def get_daily_contract(
    db: Any, guild_id: int, user_id: int,
) -> ContractView:
    """Return today's contract, rolling a fresh one if the stored one is
    expired or missing. Always persists the latest dict back to the DB so
    progress survives a panel close."""
    state = await ensure_state(db, guild_id, user_id)
    today = _dt.datetime.now(tz=_dt.timezone.utc).date().isoformat()
    stored = _as_dict(state.get("daily_contract"))
    fresh = False
    needs_roll = (
        not stored
        or str(stored.get("date") or "") != today
    )
    if needs_roll:
        contract = fc.roll_daily_contract(
            user_id=user_id, guild_id=guild_id,
            farm_level=int(state.get("farm_level") or 1),
            date_iso=today,
        )
        await db.execute(
            "UPDATE user_farming SET daily_contract = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(contract),
        )
        fresh = True
    else:
        contract = dict(stored)
    inv = _as_dict(state.get("crop_inventory"))
    have = int(inv.get(str(contract.get("crop_key")), 0) or 0)
    can_turn_in = (
        not bool(contract.get("completed"))
        and have > 0
        and int(contract.get("qty_delivered") or 0) < int(contract.get("qty_required") or 0)
    )
    return ContractView(
        contract=contract,
        fresh_today=fresh,
        can_turn_in=can_turn_in,
        have=have,
    )


async def turn_in_daily_contract(
    db: Any, guild_id: int, user_id: int,
) -> ContractTurnInResult:
    """Turn in as much of today's contract as the player's crop inventory
    will cover. Pays HRV + SEED proportional to the share delivered, with
    the full reward unlocked only on the unit that completes the order.

    Mid-progress turn-ins are scaled (you turn in half, get half) so the
    player can split the haul across multiple harvests without losing
    payout. The full reward dict still pays at completion -- math sums
    out to the advertised total.
    """
    view = await get_daily_contract(db, guild_id, user_id)
    contract = dict(view.contract)
    if contract.get("completed"):
        raise ValueError("Today's contract is already complete. New one rolls at UTC midnight.")
    crop_key = str(contract.get("crop_key") or "")
    crop_meta = fc.crop_meta(crop_key) or {}
    if not crop_meta:
        raise ValueError("Contract crop is unknown -- something went wrong rolling today's order.")
    state = await ensure_state(db, guild_id, user_id)
    inv = _as_dict(state.get("crop_inventory"))
    have = int(inv.get(crop_key, 0) or 0)
    if have <= 0:
        raise ValueError(
            f"No {crop_meta.get('name', crop_key)} to turn in. Harvest some first."
        )
    required = int(contract.get("qty_required") or 0)
    delivered = int(contract.get("qty_delivered") or 0)
    needed = max(0, required - delivered)
    if needed <= 0:
        raise ValueError("Contract already filled. Use `,farm contract` to see today's status.")
    take = min(have, needed)
    inv[crop_key] = have - take
    if inv[crop_key] <= 0:
        inv.pop(crop_key, None)
    new_delivered = delivered + take
    completed_now = (new_delivered >= required)
    contract["qty_delivered"] = new_delivered
    contract["completed"] = bool(completed_now)
    if completed_now:
        contract["completed_at"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    # Payout share: proportional to the qty turned in this call. Same
    # arithmetic shape used by fishing's daily-contract sister system.
    share = float(take) / max(1.0, float(required))
    hrv_human = float(contract.get("hrv_reward_human") or 0.0) * share
    seed_human = float(contract.get("seed_reward_human") or 0.0) * share
    hrv_raw = int(to_raw(round(hrv_human, 2)))
    seed_raw = int(to_raw(round(seed_human, 2)))
    if hrv_raw > 0:
        await db.update_wallet_holding(
            user_id, guild_id, fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL, hrv_raw,
        )
    if seed_raw > 0:
        await db.update_wallet_holding(
            user_id, guild_id, fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL, seed_raw,
        )
    completion_bump = 1 if completed_now else 0
    await db.execute(
        """
        UPDATE user_farming SET
            crop_inventory             = $3::jsonb,
            daily_contract             = $4::jsonb,
            total_contracts_completed  = total_contracts_completed + $5,
            total_hrv_earned_raw       = total_hrv_earned_raw + $6::numeric,
            total_seed_earned_raw      = total_seed_earned_raw + $7::numeric,
            updated_at                 = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(inv), _json(contract),
        int(completion_bump), int(hrv_raw), int(seed_raw),
    )
    return ContractTurnInResult(
        crop_key=crop_key,
        qty_turned_in=take,
        hrv_paid_raw=hrv_raw,
        seed_paid_raw=seed_raw,
        completed=bool(completed_now),
    )


# ============================================================================
#  Forage minigame
# ============================================================================
#
# Wander-the-fields minigame mirroring services.fishing.dig_treasure_map
# in shape (cooldown gate -> weighted outcome roll -> single dataclass
# receipt). No consumable required -- the cooldown is the gate. Outcomes
# fan out to HRV/SEED credits, seed packet stashes, fertilizer packs, or
# the rare Ancient Tuber jackpot which lands a legendary crop directly in
# crop_inventory.

@dataclass
class ForageResult:
    outcome_key:    str
    label:          str
    hrv_credited:   float = 0.0
    seed_credited:  float = 0.0
    packets_added:  list[tuple[str, int]] = field(default_factory=list)
    fertilizer_added: tuple[str, int] | None = None
    jackpot_crop:   tuple[str, int] | None = None


_FORAGE_LABELS: dict[str, str] = {
    "hrv_purse_small": "Small Coin Purse",
    "hrv_purse_big":   "Heavy Coin Purse",
    "seed_pile_small": "Spilled Seed Cache",
    "seed_pile_big":   "Sun-warmed SEED Pile",
    "seed_packets":    "Stash of Seed Packets",
    "fertilizer_find": "Sack of Fertilizer",
    "ancient_tuber":   "ANCIENT TUBER",
    "empty":           "Just Brambles",
}


async def farm_forage(
    db: Any, guild_id: int, user_id: int,
) -> ForageResult:
    """Wander the fields once. Cooldown enforced via DB-side clock on
    user_farming.last_forage_at -- no Python now() vs Postgres TIMESTAMPTZ.
    Stamps the cooldown + bumps total_forages BEFORE the loot resolves so
    a transient crash never lets a player double-roll."""
    state = await ensure_state(db, guild_id, user_id)

    # DB-side cooldown clock: returns elapsed seconds since the last
    # forage (0 if never foraged). Same shape as fishing's trap-collect
    # and treasure-dig cooldowns.
    cd_row = await db.fetch_one(
        """
        SELECT
            CASE
                WHEN last_forage_at IS NULL THEN 0
                ELSE EXTRACT(EPOCH FROM (NOW() - last_forage_at))::INTEGER
            END AS elapsed_s
          FROM user_farming
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
    )
    elapsed_s = int((cd_row or {}).get("elapsed_s") or 0)
    if elapsed_s > 0 and elapsed_s < int(fc.FORAGE_COOLDOWN_S):
        wait = int(fc.FORAGE_COOLDOWN_S - elapsed_s)
        raise ValueError(
            f"You're still catching your breath. Forage again in **{wait}s**."
        )

    rng = random.Random()
    outcome = fc.roll_forage_outcome(rng)
    label = _FORAGE_LABELS.get(outcome, outcome.replace("_", " ").title())

    crops_inv = _as_dict(state.get("crop_inventory"))
    seed_packets = _as_dict(state.get("seed_packets"))
    fert_inv = _as_dict(state.get("fertilizer_inventory"))

    hrv_credited = 0.0
    seed_credited = 0.0
    packets_added: list[tuple[str, int]] = []
    fertilizer_added: tuple[str, int] | None = None
    jackpot_crop: tuple[str, int] | None = None

    # Farm-level payout multiplier: forage gains scale with the player's
    # farm level so a Lv 40 forager doesn't pull the same handful of HRV
    # as a fresh Lv 1 (mirrors the harvest payout shape in farm_harvest).
    _farm_lvl = int(state.get("farm_level") or 1)
    _lvl_mult = fc.level_payout_mult(_farm_lvl)
    if outcome in ("hrv_purse_small", "hrv_purse_big"):
        lo, hi = fc.FORAGE_PAYOUTS[outcome]
        hrv_credited = round(rng.uniform(lo, hi) * _lvl_mult, 2)
    elif outcome in ("seed_pile_small", "seed_pile_big"):
        lo, hi = fc.FORAGE_PAYOUTS[outcome]
        seed_credited = round(rng.uniform(lo, hi) * _lvl_mult, 2)
    elif outcome == "seed_packets":
        pool = fc.forage_packet_pool()
        if pool:
            qty_lo, qty_hi = fc.FORAGE_PACKET_QTY
            # Two distinct crops at smallish counts so the drop feels
            # like a varied stash rather than a same-flavour pile.
            picks = rng.sample(list(pool), k=min(2, len(pool)))
            for crop_key in picks:
                qty = rng.randint(qty_lo, qty_hi)
                seed_packets[crop_key] = int(seed_packets.get(crop_key, 0) or 0) + qty
                packets_added.append((crop_key, qty))
    elif outcome == "fertilizer_find":
        fert_key = rng.choice(fc.FORAGE_FERTILIZER_POOL)
        qty_lo, qty_hi = fc.FORAGE_FERTILIZER_QTY
        qty = rng.randint(qty_lo, qty_hi)
        fert_inv[fert_key] = int(fert_inv.get(fert_key, 0) or 0) + qty
        fertilizer_added = (fert_key, qty)
    elif outcome == "ancient_tuber":
        crop_key = fc.FORAGE_JACKPOT_CROP
        qty_lo, qty_hi = fc.FORAGE_JACKPOT_QTY
        qty = rng.randint(qty_lo, qty_hi)
        crops_inv[crop_key] = int(crops_inv.get(crop_key, 0) or 0) + qty
        jackpot_crop = (crop_key, qty)

    # Credit token wallets. Both calls mint via update_wallet_holding
    # (no oracle move, mirroring the harvest credit path) so a forage
    # purse reads as a small additive event rather than a chart shock.
    if hrv_credited > 0:
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
            int(to_raw(hrv_credited)),
        )
    if seed_credited > 0:
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.HARVEST_NETWORK_SHORT, fc.SEED_SYMBOL,
            int(to_raw(seed_credited)),
        )
    await db.execute(
        """
        UPDATE user_farming SET
            crop_inventory       = $3::jsonb,
            seed_packets         = $4::jsonb,
            fertilizer_inventory = $5::jsonb,
            last_forage_at       = NOW(),
            total_forages        = total_forages + 1,
            total_hrv_earned_raw = total_hrv_earned_raw + $6::numeric,
            total_seed_earned_raw = total_seed_earned_raw + $7::numeric,
            updated_at           = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(crops_inv), _json(seed_packets), _json(fert_inv),
        int(to_raw(hrv_credited)) if hrv_credited > 0 else 0,
        int(to_raw(seed_credited)) if seed_credited > 0 else 0,
    )
    return ForageResult(
        outcome_key=outcome,
        label=label,
        hrv_credited=hrv_credited,
        seed_credited=seed_credited,
        packets_added=packets_added,
        fertilizer_added=fertilizer_added,
        jackpot_crop=jackpot_crop,
    )


# ============================================================================
# Wild buddy battles + harvest-egg drops (Phase B)
# ============================================================================
#
# Mirror services/fishing.resolve_wild_battle + services/dungeon.resolve_wild_battle
# but on the Harvest Network. Wild battles spawn at a depth-scaled chance
# during ``,farm harvest``; the cog stashes the synth opponent on
# user_farming.pending_wild_buddy and the player engages via ,farm battle.
# Win pays HRV + BBT + capture roll. Loss is a counter bump only.

@dataclass(slots=True)
class FarmWildBattleResolution:
    won: bool
    captured: bool
    hrv_reward_raw: int
    bbt_reward_raw: int
    captured_buddy_row: dict | None
    new_won_total: int
    new_lost_total: int
    new_captured_total: int


async def maybe_spawn_wild_battle(
    db: Any, guild_id: int, user_id: int, zone: str,
) -> dict | None:
    """Roll a wild-buddy spawn for a fresh harvest action. Returns the
    synthesised opponent dict (matches cc_buddies row shape) on a hit,
    None otherwise. Stores the spawn on user_farming.pending_wild_buddy
    so ,farm battle has something to fight even if the harvest reply
    is dismissed.

    Skipped when the player already has a pending wild buddy -- one
    fight at a time keeps the queue obvious.
    """
    import random as _r
    state = await db.fetch_one(
        "SELECT pending_wild_buddy FROM user_farming "
        "WHERE guild_id=$1 AND user_id=$2",
        int(guild_id), int(user_id),
    )
    if state and state.get("pending_wild_buddy"):
        return None
    zone_meta = fc.zone_meta(zone) or {}
    zone_tier = int(zone_meta.get("tier") or 1)
    base_chance = fc.wild_battle_chance(zone_tier)
    spawn_chance = base_chance
    attractor_on = False
    try:
        from services.buddy_economy import attractor_active as _att
        if await _att(db, int(guild_id), int(user_id)):
            attractor_on = True
            spawn_chance = min(1.0, base_chance * 2.0)
    except Exception:
        log.debug("farm attractor probe failed", exc_info=True)
    if _r.random() >= spawn_chance:
        return None
    wild = fc.roll_wild_battle(zone_tier, zone=zone)
    # Stamp the attractor flag onto the persisted JSONB so the cog can
    # render a magnet badge when the player accepts the encounter.
    if attractor_on:
        wild = {**wild, "attractor_pulled": True}
    try:
        await db.execute(
            "UPDATE user_farming SET pending_wild_buddy = $3::jsonb, "
            "updated_at = NOW() WHERE guild_id=$1 AND user_id=$2",
            int(guild_id), int(user_id), _json_mod.dumps(wild),
        )
    except Exception:
        log.exception(
            "farm wild-spawn: failed to persist wild buddy uid=%s gid=%s",
            user_id, guild_id,
        )
        return None
    return wild


async def resolve_wild_battle(
    db: Any, guild_id: int, user_id: int,
    *, won: bool, zone: str,
    opponent_species: str | None = None,
    opponent_level: int = 1,
    opponent_rarity_tier: int = 1,
    bonus_pct: float = 0.0,
) -> FarmWildBattleResolution:
    """Persist the outcome of a farm wild-buddy battle. Mirrors
    services/dungeon.resolve_wild_battle exactly -- HRV + BBT credit on
    win, capture roll respecting MAX_OWNED_BUDDIES, counter bump.
    """
    bonus_mult = 1.0 + max(0.0, float(bonus_pct))
    captured = False
    captured_buddy_row: dict | None = None
    hrv_reward_raw = 0
    bbt_reward_raw = 0
    zone_meta = fc.zone_meta(zone) or {}
    zone_tier = int(zone_meta.get("tier") or 1)

    if won:
        # HRV credit -- standard mint via wallet_holdings, no oracle drop
        # (mirrors stake-yield mints elsewhere; keeps battle rewards
        # additive without crashing the chart on a 100-win streak).
        hrv_human = fc.wild_battle_hrv_reward(zone_tier) * bonus_mult
        hrv_reward_raw = to_raw(hrv_human) if hrv_human > 0 else 0
        if hrv_reward_raw > 0:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id,
                    fc.HARVEST_NETWORK_SHORT, fc.HRV_SYMBOL,
                    int(hrv_reward_raw),
                )
                await db.execute(
                    "UPDATE user_farming "
                    "SET total_hrv_earned_raw = total_hrv_earned_raw + $3::numeric, "
                    "updated_at = NOW() WHERE guild_id=$1 AND user_id=$2",
                    guild_id, user_id, int(hrv_reward_raw),
                )
            except Exception:
                log.exception(
                    "farm wild-battle: HRV credit failed uid=%s gid=%s",
                    user_id, guild_id,
                )
                hrv_reward_raw = 0

        # BBT credit -- universal battle reward via the buddy_economy
        # mint helper. Best-effort; failure here doesn't roll back HRV.
        try:
            from services import buddy_economy as _bes
            bbt_human = fc.wild_battle_bbt_reward(zone_tier) * bonus_mult
            bbt_reward_raw = await _bes.mint_bbt_reward(
                db, guild_id, user_id, float(bbt_human), source="farm_wild",
            )
        except Exception:
            log.exception(
                "farm wild-battle: BBT mint failed uid=%s gid=%s",
                user_id, guild_id,
            )

        # Capture roll. Same shape as fishing/dungeon -- battle slots
        # first, else into storage if there's room, else skip cleanly
        # and the player keeps the HRV/BBT win.
        import random as _r
        if _r.random() < fc.WILD_BATTLE_CAPTURE_CHANCE and opponent_species:
            try:
                from services.buddy_economy import (
                    capture_destination as _dest,
                )
                _capture_dest = await _dest(db, guild_id, user_id)
                if _capture_dest is not None:
                    capture_status = (
                        "owned" if _capture_dest == "battle" else "stored"
                    )
                    species_capture = str(opponent_species)
                    try:
                        from services.buddy_names import generate_name
                        new_name = await generate_name(species_capture, db, guild_id)
                    except Exception:
                        new_name = species_capture.title()
                    try:
                        await db.execute(
                            "INSERT INTO cc_buddy_hatches "
                            "(guild_id, user_id, first_species) "
                            "VALUES ($1, $2, $3) "
                            "ON CONFLICT (guild_id, user_id) DO NOTHING",
                            guild_id, user_id, species_capture,
                        )
                    except Exception:
                        log.exception(
                            "farm wild-battle: cc_buddy_hatches insert failed",
                        )
                    from configs.buddies_config import (
                        roll_gender as _roll_gender,
                        xp_for_level as _xp_for_level,
                    )
                    _cap_level = int(max(1, opponent_level))
                    new_row = await db.fetch_one(
                        "INSERT INTO cc_buddies "
                        "(guild_id, owner_user_id, species, name, "
                        " status, is_active, rarity_tier, level, xp, gender) "
                        "VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7, $8, $9) "
                        "RETURNING *",
                        guild_id, user_id, species_capture, new_name,
                        str(capture_status),
                        int(max(1, opponent_rarity_tier)),
                        _cap_level,
                        int(_xp_for_level(_cap_level)),
                        _roll_gender(),
                    )
                    if new_row:
                        captured = True
                        captured_buddy_row = dict(new_row)
            except Exception:
                log.exception(
                    "farm wild-battle: capture insert failed uid=%s gid=%s",
                    user_id, guild_id,
                )

    # Counter bump + clear pending_wild_buddy in one UPDATE.
    row = await db.fetch_one(
        """
        UPDATE user_farming
           SET wild_battles_won      = wild_battles_won
                                     + (CASE WHEN $3 THEN 1 ELSE 0 END),
               wild_battles_lost     = wild_battles_lost
                                     + (CASE WHEN $3 THEN 0 ELSE 1 END),
               wild_buddies_captured = wild_buddies_captured
                                     + (CASE WHEN $4 THEN 1 ELSE 0 END),
               pending_wild_buddy    = NULL,
               updated_at            = NOW()
         WHERE guild_id=$1 AND user_id=$2
        RETURNING wild_battles_won, wild_battles_lost, wild_buddies_captured
        """,
        guild_id, user_id, bool(won), bool(captured),
    )
    new_won = int((row or {}).get("wild_battles_won") or 0)
    new_lost = int((row or {}).get("wild_battles_lost") or 0)
    new_cap = int((row or {}).get("wild_buddies_captured") or 0)
    return FarmWildBattleResolution(
        won=bool(won),
        captured=bool(captured),
        hrv_reward_raw=int(hrv_reward_raw),
        bbt_reward_raw=int(bbt_reward_raw),
        captured_buddy_row=captured_buddy_row,
        new_won_total=new_won,
        new_lost_total=new_lost,
        new_captured_total=new_cap,
    )


async def maybe_drop_harvest_egg(
    db: Any, guild_id: int, user_id: int,
) -> bool:
    """Roll a held-egg drop on a harvest action. Returns True iff the
    player got an egg (lands in the same held-egg slot fishing uses --
    one egg system across the bot).
    """
    import random as _r
    if _r.random() >= fc.HARVEST_EGG_CHANCE:
        return False
    try:
        from services import fishing as _fish
        await _fish.hatch_fishing_buddy(
            db, guild_id, user_id, source="farm_harvest",
        )
        return True
    except Exception:
        log.debug("farm harvest egg drop failed", exc_info=True)
        return False
