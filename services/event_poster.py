"""V3 Pillar 6 -- Pillow event poster.

    render_event_poster(event_id, *, name, flavour, modifiers, ends_at)

Returns a 1400x700 PNG with a themed backdrop, big modifier badges, and
a time-remaining bar. Attached to the announcement embed when an
Apex Event starts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from configs.apex_events_config import EVENTS
from constants.ui import (
    C_AMBER,
    C_BEAR,
    C_BULL,
    C_CATASTROPHE,
    C_CHART_BG,
    C_INFO,
    C_NAVY,
    C_VOLATILE,
)
from core.framework.render import RenderCanvas


_RARITY_COLOR = {
    "info":         C_INFO,
    "warning":      C_AMBER,
    "volatile":     C_VOLATILE,
    "catastrophe":  C_CATASTROPHE,
}


def render_event_poster(
    event_id: str,
    *,
    name: str | None = None,
    flavour: str | None = None,
    modifiers: Mapping[str, float] | None = None,
    ends_at: datetime | float | int | None = None,
) -> bytes:
    """Big poster card for an Apex Event."""
    catalogue = EVENTS.get(event_id, {})
    name = name or catalogue.get("name", event_id)
    flavour = flavour or catalogue.get("flavour", "")
    modifiers = dict(modifiers or catalogue.get("modifiers") or {})
    rarity = catalogue.get("rarity", "info")
    accent = _RARITY_COLOR.get(rarity, C_INFO)

    canvas = RenderCanvas(1400, 700, bg=C_NAVY, gradient_to=C_CHART_BG)
    canvas.title(
        f"APEX EVENT  -  {name.upper()}",
        subtitle=rarity.title(),
        color=accent,
    )
    # Halo behind the title to push the rarity vibe.
    canvas.halo((40, 30, 1100, 110), accent, radius=18, alpha=110)

    # Flavour panel
    canvas.rounded_panel((40, 130, 1360, 240), color=C_CHART_BG, radius=14)
    # Word-wrap flavour
    wrap_at = 90
    cur = ""
    y = 150
    for word in (flavour or "").split():
        if len(cur) + len(word) + 1 > wrap_at:
            canvas.text((60, y), cur, color=0xDDE2EB, size=16)
            y += 22
            cur = word
            if y > 220:
                break
        else:
            cur = (cur + " " + word).strip()
    if cur and y <= 220:
        canvas.text((60, y), cur, color=0xDDE2EB, size=16)

    # Modifier badges row
    canvas.text((60, 260), "ACTIVE MODIFIERS", color=accent, size=14, bold=True)
    badge_y = 290
    badge_x = 60
    for key, val in sorted(modifiers.items(), key=lambda kv: kv[0]):
        try:
            v = float(val)
        except Exception:
            continue
        label_color = _modifier_color(key, v)
        text = f"{key}  x{v:.2f}"
        rect = canvas.pill_badge(
            (badge_x, badge_y), text,
            color=label_color, font_size=14, padding=(14, 8),
        )
        # Stack badges in a 5-per-row grid.
        badge_x = rect[2] + 14
        if badge_x > 1300:
            badge_x = 60
            badge_y = rect[3] + 12

    # Time remaining bar
    canvas.divider(560, x0=60, x1=1340)
    canvas.text((60, 580), "Time remaining", color=0xBFC7D5, size=14)
    progress = _time_progress(ends_at)
    canvas.progress_bar(
        (60, 610, 1340, 638), progress,
        color=accent,
        label=_format_remaining(ends_at),
    )
    canvas.footer("Apex Event poster")
    return canvas.to_png_bytes()


def _modifier_color(key: str, v: float) -> int:
    # Heuristic: keys that read "boost" something get green when >1,
    # red when <1. Penalty-style keys flip the convention.
    if v >= 1.0:
        return C_BULL
    if v >= 0.5:
        return C_AMBER
    return C_BEAR


def _ends_at_epoch(ends_at: datetime | float | int | None) -> float | None:
    """Coerce a poster ``ends_at`` to a unix epoch float.

    DB timestamps come back as epoch floats via ``core.database._coerce``;
    callers in tests may still pass a ``datetime``. Accept either.
    """
    if ends_at is None:
        return None
    if isinstance(ends_at, (int, float)):
        return float(ends_at)
    if isinstance(ends_at, datetime):
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        return ends_at.timestamp()
    return None


def _time_progress(ends_at: datetime | float | int | None) -> float:
    end_ts = _ends_at_epoch(ends_at)
    if end_ts is None:
        return 0.0
    now_ts = datetime.now(timezone.utc).timestamp()
    if end_ts <= now_ts:
        return 0.0
    # Don't have started_at here -- fake it from a synthetic 6h baseline so
    # the bar always has *some* signal. Real callers pass ends_at and we
    # render "fraction remaining" against the baseline.
    total = 6 * 3600
    remaining = end_ts - now_ts
    return max(0.0, min(1.0, remaining / total))


def _format_remaining(ends_at: datetime | float | int | None) -> str:
    end_ts = _ends_at_epoch(ends_at)
    if end_ts is None:
        return ""
    now_ts = datetime.now(timezone.utc).timestamp()
    if end_ts <= now_ts:
        return "ended"
    secs = int(end_ts - now_ts)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"
