"""Shared view + button helpers for the ``$`` namespace.

The Sources button leans on the existing ``_SourcesView`` /
``_make_sources_button`` factory in :mod:`cogs.help` so we get one
canonical implementation that already handles ephemeral replies and
URL sanitisation. We only re-export the factory and apply our own
allowlist sanitiser from :mod:`services.market_ai` on top.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

log = logging.getLogger(__name__)


def make_sources_button(
    citations: list[dict[str, Any]],
    user_id: int,
) -> discord.ui.View | None:
    """Wrap :func:`cogs.help._make_sources_button` with our allowlist
    sanitiser. Returns ``None`` if no trusted citations survive.
    """
    if not citations:
        return None
    try:
        from cogs.help import _SourcesView
    except Exception as exc:
        log.debug("[$_dollar.views] cannot import _SourcesView: %s", exc)
        return None
    payload = [
        {"title": c.get("title") or c.get("url", ""), "url": c.get("url", "")}
        for c in citations
        if isinstance(c, dict) and c.get("url")
    ]
    if not payload:
        return None
    try:
        view = _SourcesView(payload, author_id=user_id)
    except Exception as exc:
        log.debug("[$_dollar.views] _SourcesView failed: %s", exc)
        return None
    # _SourcesView's constructor drops anything that fails sanitization;
    # don't show the button if everything got filtered out.
    if not getattr(view, "results", None):
        return None
    return view
