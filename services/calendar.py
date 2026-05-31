"""
services/calendar.py  -  Aggregates upcoming + recurring server events.

Single source of truth for the ``,calendar`` view + the calendar tile
on the unified ``,today`` panel + the auto-post the admin event /
challenge commands trigger when they create new schedule items.

Surfaces three classes of item:

* **active_challenge** -- rows from ``guild_challenges`` with status
  ``'active'``, fetched via ``services.challenges.list_active``.
* **market_event** -- the currently-active market event (Pump / Crash /
  Moon / etc.), fetched via ``cogs.events.get_active_event`` from Redis.
  At most one per guild at a time.
* **recurring** -- daily reset (quest period flip, daily reward),
  weekly reset (weekly quest period, BUD validator unlock), and the
  next season tick. Computed from the wall clock; no DB hit.

Everything returned as a flat list of ``CalendarItem`` dicts so the
view layer can render them as mines-style button tiles. Sort key is
``(active_first, starts_at, ends_at)`` so live items lead and the rest
are queued by next-scheduled-time.

This module is presentation-agnostic: the cog (cogs/calendar.py) owns
the embed + view + auto-post wiring. Keeping the data path here means
the API can expose the same calendar at /v2/guild/<gid>/calendar with
zero duplication.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Item shape
# ----------------------------------------------------------------------------

@dataclass
class CalendarItem:
    """Flat dict-shape one calendar tile.

    ``kind`` decides the tile color in the view layer:
      * ``"challenge"``  -> blurple
      * ``"market"``     -> gold
      * ``"recurring"``  -> gray
    """
    key:        str                     # stable id for the tile
    kind:       str                     # 'challenge' | 'market' | 'recurring'
    title:      str                     # short label (max 60 chars for tiles)
    blurb:      str                     # one-line description
    starts_at:  float | None = None     # epoch seconds (UTC)
    ends_at:    float | None = None     # epoch seconds (UTC)
    active_now: bool         = False    # currently running
    emoji:      str          = ""
    cmd_hint:   str          = ""       # e.g. ",challenge" or ",event"
    extra:      dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "title": self.title,
            "blurb": self.blurb,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "active_now": self.active_now,
            "emoji": self.emoji,
            "cmd_hint": self.cmd_hint,
            "extra": self.extra,
        }


# ----------------------------------------------------------------------------
# Recurring schedule helpers
# ----------------------------------------------------------------------------

def _next_daily_reset_utc(now: _dt.datetime) -> _dt.datetime:
    """Next 00:00 UTC after ``now``."""
    tomorrow = (now + _dt.timedelta(days=1)).date()
    return _dt.datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                        0, 0, 0, tzinfo=_dt.timezone.utc)


def _next_weekly_reset_utc(now: _dt.datetime) -> _dt.datetime:
    """Next Monday 00:00 UTC after ``now`` (weekday 0)."""
    days_ahead = (7 - now.weekday()) % 7 or 7
    target = (now + _dt.timedelta(days=days_ahead)).date()
    return _dt.datetime(target.year, target.month, target.day,
                        0, 0, 0, tzinfo=_dt.timezone.utc)


def _recurring_items(now: _dt.datetime) -> list[CalendarItem]:
    """Scheduled-on-the-clock resets every guild shares.

    Pure-function -- doesn't hit the DB. The wall clock is the source of
    truth for daily quest period / weekly quest period flips.
    """
    daily = _next_daily_reset_utc(now)
    weekly = _next_weekly_reset_utc(now)
    return [
        CalendarItem(
            key="recurring:daily_reset",
            kind="recurring",
            title="Daily reset",
            blurb=(
                "Daily quests roll over, daily reward becomes claimable, "
                "expedition / ,delve cooldowns refresh."
            ),
            starts_at=daily.timestamp(),
            ends_at=None,
            active_now=False,
            emoji="\U0001F4C5",
            cmd_hint=",today",
        ),
        CalendarItem(
            key="recurring:weekly_reset",
            kind="recurring",
            title="Weekly reset",
            blurb=(
                "Weekly quests roll over, BUD validator vesting tick, "
                "any weekly leaderboards reset."
            ),
            starts_at=weekly.timestamp(),
            ends_at=None,
            active_now=False,
            emoji="\U0001F4C6",
            cmd_hint=",quests",
        ),
    ]


# ----------------------------------------------------------------------------
# Per-source fetchers
# ----------------------------------------------------------------------------

async def _fetch_challenges(db: Any, guild_id: int) -> list[CalendarItem]:
    try:
        from services import challenges as _ch
        rows = await _ch.list_active(db, int(guild_id))
    except Exception:
        log.debug("calendar: challenges fetch failed", exc_info=True)
        return []
    items: list[CalendarItem] = []
    for r in rows or []:
        try:
            from services import challenges as _ch
            label = _ch.trigger_label(str(r.get("trigger") or ""))
        except Exception:
            label = str(r.get("trigger") or "?")
        ends = r.get("ends_at")
        ends_ts = (
            ends.timestamp() if hasattr(ends, "timestamp")
            else float(ends) if ends else None
        )
        starts = r.get("started_at")
        starts_ts = (
            starts.timestamp() if hasattr(starts, "timestamp")
            else float(starts) if starts else None
        )
        target = int(r.get("target") or 0)
        progress = int(r.get("progress") or 0)
        pool = float(r.get("reward_pool_usd") or 0.0)
        items.append(CalendarItem(
            key=f"challenge:{int(r.get('challenge_id') or 0)}",
            kind="challenge",
            title=str(r.get("name") or "Challenge"),
            blurb=(
                f"{progress:,} / {target:,} {label.lower()}  ·  "
                f"pool ${pool:,.2f}"
            ),
            starts_at=starts_ts,
            ends_at=ends_ts,
            active_now=True,
            emoji="\U0001F3AF",
            cmd_hint=",challenge",
            extra={
                "challenge_id": int(r.get("challenge_id") or 0),
                "trigger": str(r.get("trigger") or ""),
                "target": target, "progress": progress,
                "reward_pool_usd": pool,
            },
        ))
    return items


async def _fetch_market_event(redis: Any, guild_id: int) -> list[CalendarItem]:
    """Surface the currently-running market event, if any."""
    if redis is None:
        return []
    try:
        from cogs.events import get_active_event  # type: ignore
    except Exception:
        return []
    try:
        ae = await get_active_event(redis, int(guild_id))
    except Exception:
        log.debug("calendar: get_active_event failed", exc_info=True)
        return []
    if not ae:
        return []
    try:
        from configs.market_events_config import EVENT_REGISTRY
        ev = EVENT_REGISTRY.get(getattr(ae, "event_id", "") or "")
    except Exception:
        ev = None
    name = getattr(ev, "display_name", None) or getattr(ae, "event_id", "?")
    blurb = getattr(ev, "description", "Live market event running.") or "Live market event running."
    started = float(getattr(ae, "event_started_at", 0.0) or 0.0)
    total_secs = (
        sum(p.duration_minutes for p in getattr(ev, "phases", ())) * 60
        if ev else 0
    )
    ends = (started + total_secs) if started and total_secs else None
    return [CalendarItem(
        key=f"market:{getattr(ae, 'event_id', '')}",
        kind="market",
        title=str(name),
        blurb=str(blurb)[:200],
        starts_at=started or None,
        ends_at=ends,
        active_now=True,
        emoji=str(getattr(ev, "emoji", "") or "\U0001F4B9"),
        cmd_hint=",event",
        extra={"event_id": str(getattr(ae, "event_id", ""))},
    )]


# ----------------------------------------------------------------------------
# Public aggregator
# ----------------------------------------------------------------------------

async def list_calendar(
    db: Any, guild_id: int, *, redis: Any = None,
) -> list[CalendarItem]:
    """Return every calendar tile sorted live-first then by next time.

    ``redis`` is optional -- when omitted the market-event lane is
    skipped (the active event lives in Redis, not the DB).
    """
    out: list[CalendarItem] = []
    out.extend(await _fetch_challenges(db, guild_id))
    out.extend(await _fetch_market_event(redis, guild_id))
    now = _dt.datetime.now(_dt.timezone.utc)
    out.extend(_recurring_items(now))

    def _sort_key(it: CalendarItem) -> tuple:
        # Live items first; among live, sort by ends_at (next to expire);
        # among scheduled, sort by starts_at.
        live_rank = 0 if it.active_now else 1
        ts = it.ends_at if it.active_now else it.starts_at
        ts = ts if ts is not None else 9_999_999_999.0
        return (live_rank, ts)
    out.sort(key=_sort_key)
    return out


__all__ = ("CalendarItem", "list_calendar")
