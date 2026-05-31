"""
core/framework/live.py  -  Live dashboard engine for Discoin.

Provides hash-based diff updates to Discord messages on a scheduled loop.
Messages are only edited when content actually changes, and edits are
throttled to stay well clear of Discord rate limits.

Architecture
────────────
• One global asyncio loop (1-second global tick)
• Per-dashboard interval (minimum _MIN_EDIT_INTERVAL seconds enforced)
• Hash comparison skips edits when nothing changed
• Dashboards auto-expire and have their buttons removed
• Interactions pause the live cycle briefly so the user's click
  is never immediately overwritten

Usage
─────
    from core.framework.live import live, LiveState
    import time

    # After sending a message:
    bot.live.register(LiveState(
        id=f"mining:{ctx.author.id}:{ctx.guild.id}",
        message_id=msg.id,
        channel_id=msg.channel.id,
        interval=5.0,            # tick every 5 s
        expires_at=time.time() + 600,  # live for 10 min

        get_data=lambda: db.get_mining_stats(ctx.author.id),
        render=lambda data: build_mining_embed(data),
    ))

    # On any button interaction in the live message:
    bot.live.pause(f"mining:{ctx.author.id}:{ctx.guild.id}", seconds=5)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import discord

# Minimum wall-clock seconds between successive edits of the same message.
# Discord's channel rate limit is ~5 edits/5s; we stay conservative.
_MIN_EDIT_INTERVAL: float = 3.0


# ══════════════════════════════════════════════════════════════════════════════
# LiveState
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LiveState:
    """
    Configuration and runtime state for a single live dashboard.

    Fields
    ──────
    id          Unique key (e.g. ``"mining:123:456"``).  Registering a new
                state with the same id replaces the old one.
    message_id  Discord message to edit.
    channel_id  Channel containing the message.
    interval    Seconds between tick attempts (>= _MIN_EDIT_INTERVAL).
    expires_at  Unix timestamp after which the dashboard stops ticking.
    get_data    Async callable that returns the data needed to render.
    render      Sync callable: data → discord.Embed or kwargs dict for
                msg.edit().  Return a dict to also update components, etc.
    """

    id: str
    message_id: int
    channel_id: int
    interval: float
    expires_at: float
    get_data: Callable[[], Awaitable[Any]]
    render: Callable[[Any], discord.Embed | dict[str, Any]]

    # ── Runtime (managed by LiveEngine) ────────────────────────────────────
    last_hash: str | None = field(default=None)
    last_edit: float = field(default=0.0)
    _paused_until: float = field(default=0.0)


# ══════════════════════════════════════════════════════════════════════════════
# LiveEngine
# ══════════════════════════════════════════════════════════════════════════════

class LiveEngine:
    """
    Global live-update scheduler.

    One instance (``live``) is created at module level and attached to the
    bot in ``core/framework/bot.py`` via ``live.init(bot)``.
    """

    def __init__(self) -> None:
        self._states: dict[str, LiveState] = {}
        self._task: asyncio.Task | None = None
        self._bot: Any = None  # discord.Client / Discoin

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def init(self, bot: Any) -> None:
        """Attach to the bot instance and start the background loop."""
        self._bot = bot
        self.start()

    def start(self) -> None:
        """Start the global tick loop (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        """Cancel the loop (called on bot shutdown)."""
        if self._task and not self._task.done():
            self._task.cancel()

    # ── Registration ───────────────────────────────────────────────────────

    def register(self, state: LiveState) -> None:
        """
        Register a live dashboard.

        Replaces any existing dashboard with the same ``state.id``.
        Starts the loop if it isn't already running.
        """
        self._states[state.id] = state
        if self._task is None or self._task.done():
            self.start()

    def unregister(self, state_id: str) -> None:
        """Remove a dashboard immediately without editing the message."""
        self._states.pop(state_id, None)

    def pause(self, state_id: str, seconds: float = 5.0) -> None:
        """
        Pause a dashboard for ``seconds`` seconds.

        Call this from button/select interaction handlers so the user's
        click is never immediately overwritten by a live tick.
        """
        state = self._states.get(state_id)
        if state:
            state._paused_until = time.time() + seconds

    def active_count(self) -> int:
        """Number of currently registered live dashboards."""
        return len(self._states)

    # ── Main loop ──────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """Single global 1-second tick loop."""
        while True:
            try:
                await asyncio.sleep(1.0)
                now = time.time()

                # 1. Collect and remove expired states
                expired = [k for k, s in self._states.items() if now > s.expires_at]
                for k in expired:
                    s = self._states.pop(k)
                    asyncio.create_task(self._on_expire(s))

                # 2. Tick states that are due (fire-and-forget per state)
                for state in list(self._states.values()):
                    if now < state._paused_until:
                        continue
                    if now - state.last_edit >= max(state.interval, _MIN_EDIT_INTERVAL):
                        asyncio.create_task(self._tick(state))

            except asyncio.CancelledError:
                break
            except Exception:
                pass  # Never crash the global loop

    # ── Tick ───────────────────────────────────────────────────────────────

    async def _tick(self, state: LiveState) -> None:
        """Fetch data, diff against last render, edit message if changed."""
        now = time.time()

        # Guard: expired (may have expired between loop check and task run)
        if now > state.expires_at:
            return

        # Guard: minimum edit interval
        if now - state.last_edit < _MIN_EDIT_INTERVAL:
            return

        try:
            data = await state.get_data()
            rendered = state.render(data)
            new_hash = _hash_render(rendered)

            if new_hash == state.last_hash:
                return  # Nothing changed  -  skip the edit

            state.last_hash = new_hash
            state.last_edit = time.time()

            channel = self._bot.get_channel(state.channel_id)
            if channel is None:
                channel = await self._bot.fetch_channel(state.channel_id)

            msg = await channel.fetch_message(state.message_id)

            if isinstance(rendered, discord.Embed):
                await msg.edit(embed=rendered)
            else:
                # dict: allows passing embed= and components= together
                await msg.edit(**rendered)

        except discord.NotFound:
            # Message was deleted  -  clean up
            self._states.pop(state.id, None)
        except Exception:
            pass  # Swallow transient errors (network, etc.)

    # ── Expiry ─────────────────────────────────────────────────────────────

    async def _on_expire(self, state: LiveState) -> None:
        """Remove interactive components when a dashboard expires."""
        try:
            channel = self._bot.get_channel(state.channel_id)
            if channel is None:
                return
            msg = await channel.fetch_message(state.message_id)
            await msg.edit(view=None)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _hash_render(rendered: discord.Embed | dict[str, Any]) -> str:
    """Produce a stable MD5 hex-digest of a rendered embed or message kwargs."""
    if isinstance(rendered, discord.Embed):
        data: Any = rendered.to_dict()
    else:
        data = rendered
    return hashlib.md5(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Global singleton
# ══════════════════════════════════════════════════════════════════════════════

#: Import this instance everywhere; call ``live.init(bot)`` once on startup.
live: LiveEngine = LiveEngine()
