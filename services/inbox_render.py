"""V3 Pillar 5: Pillow renderers for inbox surfaces.

    render_inbox_index(messages, *, display_name) -> bytes
        1000x800 board listing recent unread messages, severity-coloured.
    render_inbox_message(msg, *, display_name) -> bytes
        1000x600 detail view of one message.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from constants.ui import (
    C_AMBER,
    C_CHART_BG,
    C_CRIMSON,
    C_ERROR,
    C_GOLD,
    C_INFO,
    C_NAVY,
    C_SUBTLE,
    C_SUCCESS,
)
from core.framework.render import RenderCanvas


_SEVERITY_COLOR = {
    "info":     C_INFO,
    "success":  C_SUCCESS,
    "warning":  C_AMBER,
    "error":    C_ERROR,
    "critical": C_CRIMSON,
}


def _severity_color(s: str | None) -> int:
    return _SEVERITY_COLOR.get((s or "info").lower(), C_INFO)


def _ago(ts) -> str:
    if ts is None:
        return ""
    if not isinstance(ts, datetime):
        return str(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def render_inbox_index(
    messages: Sequence[dict],
    *,
    display_name: str = "Player",
    unread_count: int | None = None,
) -> bytes:
    """Inbox list view."""
    canvas = RenderCanvas(1000, 800, bg=C_NAVY, gradient_to=C_CHART_BG)
    count_label = (
        f"{unread_count} unread"
        if unread_count is not None
        else f"{len(messages)} recent"
    )
    canvas.title(
        f"Inbox  -  {display_name}",
        subtitle=count_label,
        color=C_GOLD,
    )
    if not messages:
        canvas.text(
            (60, 200),
            "All caught up. Nothing in the inbox.",
            color=0xBFC7D5, size=18,
        )
        return canvas.to_png_bytes()
    row_h = 70
    x0 = 40
    y0 = 110
    for i, msg in enumerate(messages[:9]):
        y = y0 + i * (row_h + 8)
        sev_color = _severity_color(msg.get("severity"))
        # Card
        canvas.rounded_panel(
            (x0, y, x0 + 920, y + row_h), color=C_CHART_BG, radius=12,
        )
        # Severity strip
        canvas.draw.rounded_rectangle(
            (x0, y, x0 + 8, y + row_h),
            radius=4,
            fill=(
                (sev_color >> 16) & 0xFF,
                (sev_color >> 8) & 0xFF,
                sev_color & 0xFF,
            ),
        )
        # Category pill
        canvas.pill_badge(
            (x0 + 24, y + 12),
            (msg.get("category") or "info").upper()[:18],
            color=sev_color, font_size=11,
        )
        # Title
        title = (msg.get("title") or "")[:80]
        canvas.text((x0 + 24, y + 36), title,
                    color=0xFFFFFF, size=15, bold=True)
        # Time + read indicator
        ago = _ago(msg.get("posted_at"))
        canvas.text(
            (x0 + 760, y + 12), ago,
            color=0x95A5A6, size=12,
        )
        if msg.get("read_at") is None:
            canvas.pill_badge(
                (x0 + 760, y + 36), "NEW", color=C_GOLD,
                font_size=10, padding=(8, 4),
            )
    if len(messages) > 9:
        canvas.text(
            (x0, y0 + 9 * (row_h + 8) + 4),
            f"+ {len(messages) - 9} more  -  use ,inbox <id> to expand or ,inbox clear to wipe.",
            color=C_SUBTLE, size=12,
        )
    canvas.footer(",inbox <id> to open a message")
    return canvas.to_png_bytes()


def render_inbox_message(msg: dict, *, display_name: str = "Player") -> bytes:
    """Single message detail view."""
    canvas = RenderCanvas(1000, 600, bg=C_NAVY, gradient_to=C_CHART_BG)
    sev_color = _severity_color(msg.get("severity"))
    canvas.title(
        msg.get("title") or "Inbox message",
        subtitle=(
            f"{(msg.get('category') or 'info').upper()}  -  "
            f"{_ago(msg.get('posted_at'))}"
        ),
        color=sev_color,
    )
    canvas.rounded_panel((40, 110, 960, 560), color=C_CHART_BG, radius=14)
    # Severity strip on the left
    canvas.draw.rounded_rectangle(
        (40, 110, 52, 560), radius=6,
        fill=(
            (sev_color >> 16) & 0xFF,
            (sev_color >> 8) & 0xFF,
            sev_color & 0xFF,
        ),
    )
    # Body -- naive word wrap to ~80 chars per line
    body = (msg.get("body") or "").splitlines() or [""]
    wrapped: list[str] = []
    for line in body:
        if not line:
            wrapped.append("")
            continue
        cur = ""
        for word in line.split(" "):
            if len(cur) + len(word) + 1 > 80:
                wrapped.append(cur)
                cur = word
            else:
                cur = (cur + " " + word).strip()
        if cur:
            wrapped.append(cur)
    y_text = 140
    for line in wrapped[:18]:
        canvas.text((70, y_text), line, color=0xDDE2EB, size=15)
        y_text += 22
    canvas.footer(f"For {display_name}")
    return canvas.to_png_bytes()
