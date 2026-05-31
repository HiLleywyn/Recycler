"""V3 Pillar 6 service: Apex Events.

Public surface:

    await try_roll(db, gid)             -- background heartbeat call
    await expire_finished(db, gid)      -- background cleanup
    await active(db, gid)               -- list active events for a guild
    await modifier(db, gid, key, default=1.0)
    await trigger(db, gid, event_id)    -- admin force-trigger
    render_event_poster(event)          -- Pillow PNG poster

Consumers (cogs/services across the bot) read modifiers via
``modifier(db, gid, key)`` which returns the cumulative modifier when
multiple events stack the same key. Default is 1.0 because most
modifiers are multiplicative; pass 0.0 for additive keys.

Inbox: ``try_roll`` writes a notification into every active player's
inbox via services.inbox.post_many when a new event starts.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from configs.apex_events_config import EVENTS
from core.config import Config

log = logging.getLogger(__name__)


# In-process modifier cache so the hot read path doesn't N+1 against
# the DB. Short TTL; safe to be a few seconds stale because event-driven
# gameplay is non-critical.
_MOD_CACHE_TTL = 30.0
_mod_cache: dict[int, tuple[float, dict[str, float]]] = {}


async def active(db, guild_id: int) -> list[dict]:
    """Return every Apex Event currently live in the guild."""
    try:
        rows = await db.fetch_all(
            "SELECT * FROM apex_events_active "
            "WHERE guild_id = $1 AND ends_at > now()",
            guild_id,
        )
        return [dict(r) for r in rows]
    except Exception:
        log.exception("apex_events: active query failed gid=%s", guild_id)
        return []


async def _refresh_modifier_cache(db, guild_id: int) -> dict[str, float]:
    """Recompute the cumulative-modifier dict from currently active events."""
    rows = await active(db, guild_id)
    cumulative: dict[str, float] = {}
    for r in rows:
        raw = r.get("modifiers") or "{}"
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            try:
                mods = json.loads(raw)
            except Exception:
                mods = {}
        else:
            mods = dict(raw)
        for k, v in mods.items():
            try:
                v_f = float(v)
            except Exception:
                continue
            # Multiplicative stacking: two events that both modify
            # mining.hashrate by 1.5x become 2.25x. Additive keys are
            # the caller's responsibility -- they pass default=0 and
            # interpret accordingly.
            cumulative[k] = cumulative.get(k, 1.0) * v_f
    _mod_cache[guild_id] = (time.time(), cumulative)
    return cumulative


async def modifier(
    db, guild_id: int, key: str, default: float = 1.0,
) -> float:
    """Return the active modifier value for ``key`` (or default).

    ``default`` is 1.0 because most modifier keys are multiplicative
    (e.g. mining.hashrate at 1.5 means +50%). For additive keys
    (e.g. mastery.xp_mult on top of base XP) pass default=0.0 and
    add accordingly.

    Cached per guild for _MOD_CACHE_TTL seconds so the read path is
    fast even on every minigame call.
    """
    now = time.time()
    cached = _mod_cache.get(guild_id)
    if cached and now - cached[0] < _MOD_CACHE_TTL:
        return float(cached[1].get(key, default))
    fresh = await _refresh_modifier_cache(db, guild_id)
    return float(fresh.get(key, default))


def invalidate(guild_id: int) -> None:
    """Force the next ``modifier`` call to refresh the cache.

    Called after ``try_roll`` / ``expire_finished`` so a freshly
    started or expired event is reflected immediately instead of
    waiting on the TTL.
    """
    _mod_cache.pop(guild_id, None)


async def try_roll(db, guild_id: int) -> dict | None:
    """Weighted-random roll: maybe start a new event in the guild.

    Background heartbeat calls this every ``APEX_EVENT_TICK`` seconds.
    A new event is started with probability ``APEX_EVENT_ROLL_PCT``;
    independent of cooldown the same event won't double-fire because
    the active-window check excludes it from the candidate pool.

    Returns the started event row (with metadata) or None when nothing
    rolled.
    """
    if not getattr(Config, "APEX_EVENTS_ENABLED", True):
        return None
    roll_pct = float(getattr(Config, "APEX_EVENT_ROLL_PCT", 0.05))
    if random.random() > roll_pct:
        return None

    live = await active(db, guild_id)
    live_ids = {r["event_id"] for r in live}
    candidates = [
        (eid, ev) for eid, ev in EVENTS.items() if eid not in live_ids
    ]
    if not candidates:
        return None

    total_w = sum(ev["weight"] for _, ev in candidates) or 1
    r = random.random() * total_w
    cum = 0
    chosen_id, chosen = candidates[-1]
    for eid, ev in candidates:
        cum += ev["weight"]
        if cum >= r:
            chosen_id, chosen = eid, ev
            break

    started = datetime.now(timezone.utc)
    ends = started + timedelta(seconds=int(chosen["duration_secs"]))
    try:
        await db.execute(
            "INSERT INTO apex_events_active "
            "(guild_id, event_id, started_at, ends_at, modifiers) "
            "VALUES ($1, $2, $3, $4, $5::jsonb) "
            "ON CONFLICT DO NOTHING",
            guild_id, chosen_id, started, ends,
            json.dumps(chosen["modifiers"]),
        )
    except Exception:
        log.exception(
            "apex_events: insert failed gid=%s event=%s",
            guild_id, chosen_id,
        )
        return None

    invalidate(guild_id)
    log.info(
        "apex_events: started event=%s gid=%s duration=%ss",
        chosen_id, guild_id, chosen["duration_secs"],
    )

    # Inbox broadcast -- best-effort; missing inbox table is OK.
    try:
        from services import inbox
        rows = await db.fetch_all(
            "SELECT user_id FROM users "
            "WHERE guild_id = $1 AND last_activity > now() - INTERVAL '7 days'",
            guild_id,
        )
        user_ids = [int(r["user_id"]) for r in rows]
        modifier_lines = "\n".join(
            f"- `{k}` x{v:.2f}" for k, v in chosen["modifiers"].items()
        )
        await inbox.post_many(
            db, user_ids, "market_event",
            f"Apex Event: {chosen['name']}",
            f"{chosen['flavour']}\n\nActive modifiers:\n{modifier_lines}\n\n"
            f"Ends: <t:{int(ends.timestamp())}:R>",
            severity=_severity_for(chosen.get("rarity", "info")),
            payload={"event_id": chosen_id},
            gid=guild_id,
        )
    except Exception:
        log.debug("apex_events: inbox broadcast skipped", exc_info=True)

    return {
        "event_id": chosen_id, "name": chosen["name"],
        "flavour": chosen["flavour"], "started_at": started,
        "ends_at": ends, "modifiers": chosen["modifiers"],
    }


def _severity_for(rarity: str) -> str:
    return {
        "info":         "info",
        "warning":      "warning",
        "volatile":     "warning",
        "catastrophe":  "critical",
    }.get(rarity, "info")


async def expire_finished(db, guild_id: int) -> int:
    """Move expired events to history. Returns count moved.

    The DELETE + INSERT runs in a single CTE so the TIMESTAMPTZ values
    never round-trip through Python -- if they did, asyncpg's reader
    converts them to epoch floats (see ``core.database._coerce``)
    and asyncpg's writer can't re-encode a float as TIMESTAMPTZ, which
    is exactly the bug this function used to hit in production
    (``DataError: invalid input for query argument $3: 1778531422.79``).
    Per CLAUDE.md "DB-side clocks for time comparisons" -- same idea,
    keep timestamp-typed values on the DB side end-to-end.
    """
    try:
        rows = await db.fetch_all(
            "WITH expired AS ("
            "  DELETE FROM apex_events_active "
            "  WHERE guild_id = $1 AND ends_at <= now() "
            "  RETURNING guild_id, event_id, started_at, ends_at, modifiers"
            ") "
            "INSERT INTO apex_events_history "
            "(guild_id, event_id, started_at, ended_at, modifiers) "
            "SELECT guild_id, event_id, started_at, ends_at, modifiers FROM expired "
            "RETURNING 1",
            guild_id,
        )
        if rows:
            invalidate(guild_id)
        return len(rows)
    except Exception:
        log.exception("apex_events: expire failed gid=%s", guild_id)
        return 0


async def trigger(db, guild_id: int, event_id: str) -> dict | None:
    """Admin / test entry point: force-start a specific event.

    Skips the roll probability and the active-window dedupe (so an
    operator can stack two of the same event for testing). Inbox
    broadcast still fires.
    """
    ev = EVENTS.get(event_id)
    if not ev:
        return None
    started = datetime.now(timezone.utc)
    ends = started + timedelta(seconds=int(ev["duration_secs"]))
    try:
        await db.execute(
            "INSERT INTO apex_events_active "
            "(guild_id, event_id, started_at, ends_at, modifiers) "
            "VALUES ($1, $2, $3, $4, $5::jsonb) "
            "ON CONFLICT DO NOTHING",
            guild_id, event_id, started, ends, json.dumps(ev["modifiers"]),
        )
        invalidate(guild_id)
        return {
            "event_id": event_id, "name": ev["name"],
            "flavour": ev["flavour"], "started_at": started,
            "ends_at": ends, "modifiers": ev["modifiers"],
        }
    except Exception:
        log.exception("apex_events: trigger failed gid=%s event=%s", guild_id, event_id)
        return None
