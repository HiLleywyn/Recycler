"""Live placeholder renderer for the chat AI reply pipeline.

Replaces the inline spinner / status logic that used to live inside
``_stream_ai_chat_to_message`` in cogs/help.py. Owns the placeholder
message and consumes events from
:func:`core.framework.agent_tools.complete_with_agent_tools_stream` to paint a
phase line ("queued (#3)" / "thinking" / "calling tool: ..." / "writing
response"), a braille spinner, and the streamed body buffer. Edits to
the placeholder are coalesced behind a single throttle so Discord's
~5 edits / 5s ceiling is never tripped.

The renderer keeps two text layers:

  * **Status header** (top of message): spinner + phase. Italicised so
    it reads as in-progress rather than a final reply. Hidden once
    delta text has started flowing.
  * **Body** (bottom of message): accumulated streamed text plus an
    optional tool-runs sub-line in Discord's `-# small text` syntax.

Once the stream is done, :meth:`finalize` writes the final body plus a
small-text footer with model / tool count / elapsed / token usage, and
attaches the supplied view (Sources, Regenerate, etc).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

import discord

log = logging.getLogger(__name__)


# Smooth braille spinner. Ten frames -- one tick every ~0.3s reads as
# a steady rotation against Discord's edit-throttle floor.
_BRAILLE_FRAMES: tuple[str, ...] = (
    "⠋", "⠙", "⠹", "⠸",
    "⠼", "⠴", "⠦", "⠧",
    "⠇", "⠏",
)
# Half-circle fallback for clients with narrow braille rendering.
_HALF_CIRCLE_FRAMES: tuple[str, ...] = ("◐", "◓", "◑", "◒")

# Spinner tick interval. Each tick MAY produce an edit if the throttle
# window has elapsed; otherwise the frame is just bumped in memory and
# painted on the next eligible tick. 0.3s is plenty granular for a
# perceived-smooth animation while keeping edit pressure low.
_DEFAULT_SPINNER_PERIOD = 0.3
# Minimum gap between Discord edits, in seconds. **Critical for
# reliability**: Discord rate-limits message edits to 5 per 5s per
# channel. With multiple concurrent AI replies in the same channel
# (e.g. a user firing 4 ``@mention`` messages in a row), the renderer's
# edit budget per reply has to leave room for OTHER replies' edits in
# the same bucket. 1.6s per reply means ~3 edits per 5s window per
# reply, so two concurrent replies still fit. The previous value
# (0.85s) put 5-8 edits per 5s window from a SINGLE reply, which
# saturated the bucket and pushed the final placeholder edit behind a
# pile of throttled delta paints -- by the time discord.py's internal
# 429 retry got around to the finalize edit, the outer ``wait_for``
# had often already given up or moved on, leaving the placeholder
# stuck at the last partial-buffer paint (visible to users as a
# mid-sentence cutoff with no footer + no buttons).
_DEFAULT_EDIT_THROTTLE = 1.6


class ChatStatusRenderer:
    """Owns the AI placeholder message and renders streaming progress.

    Usage:

        renderer = ChatStatusRenderer(
            placeholder, model="gemini-2.5-flash", started_at=time.monotonic(),
        )
        animator = asyncio.create_task(renderer.run())
        try:
            async for event in complete_with_agent_tools_stream(...):
                await renderer.feed(event)
        finally:
            animator.cancel()
            await renderer.finalize(
                body=final_text, view=reply_view, usage=usage,
            )
    """

    __slots__ = (
        "_placeholder", "_model", "_started_at",
        "_spinner_period", "_edit_throttle",
        "_buffer", "_status", "_queue_position",
        "_tool_runs", "_tool_names", "_frame_idx",
        "_last_edit", "_edit_failed",
        "_meta_model", "_meta_elapsed_ms", "_meta_usage",
        "_lock", "_done", "_view_attached",
    )

    def __init__(
        self,
        placeholder: discord.Message,
        *,
        model: str,
        started_at: float,
        spinner_period_s: float = _DEFAULT_SPINNER_PERIOD,
        edit_throttle_s: float = _DEFAULT_EDIT_THROTTLE,
    ) -> None:
        self._placeholder = placeholder
        self._model = model or ""
        self._started_at = started_at
        self._spinner_period = max(0.05, float(spinner_period_s))
        self._edit_throttle = max(0.3, float(edit_throttle_s))
        self._buffer: str = ""
        self._status: str = "thinking..."
        self._queue_position: int = 0
        self._tool_runs: list[str] = []
        self._tool_names: list[str] = []
        self._frame_idx: int = 0
        self._last_edit: float = 0.0
        self._edit_failed: bool = False
        # Filled in by the bridge's ``done`` event.
        self._meta_model: str = ""
        self._meta_elapsed_ms: int = 0
        self._meta_usage: dict = {}
        self._lock = asyncio.Lock()
        self._done = False
        # True once finalize lands an edit that includes the view. Read
        # by the cog's _patient_view_attach to skip the rescue when the
        # view already attached on the happy path.
        self._view_attached = False

    # ── public API ───────────────────────────────────────────────────────

    @property
    def tool_names(self) -> list[str]:
        """Tools that fired during this turn (unique, in call order)."""
        return list(self._tool_names)

    @property
    def edit_failed(self) -> bool:
        """True if the placeholder is gone (deleted / un-editable)."""
        return self._edit_failed

    @property
    def view_attached(self) -> bool:
        """True if finalize landed an edit that included the view."""
        return self._view_attached

    async def run(self) -> None:
        """Background spinner animator. Idles once content has streamed.

        Only paints while the buffer is EMPTY -- once the streaming
        delta events have started accumulating into the buffer, this
        coroutine stops doing anything so the channel's edit bucket is
        free for the eventual finalize edit. The animator's job is
        purely the "thinking..." spinner visible before the model
        starts producing tokens.
        """
        try:
            # Initial paint so the user sees movement immediately. Force
            # because the placeholder still says "_thinking..._" -- we
            # want to swap to the spinner ASAP.
            await self._edit(force=True)
            # Tick the spinner forward every period; the throttle inside
            # _edit gates whether the tick produces an actual API call.
            while not self._done and not self._buffer and not self._edit_failed:
                await asyncio.sleep(self._spinner_period)
                if self._done or self._buffer or self._edit_failed:
                    return
                await self._edit(force=False)
        except asyncio.CancelledError:
            pass

    async def feed(self, event: dict) -> None:
        """Consume one streaming event from the bridge.

        **Edit-budget rule**: Discord rate-limits message edits to ~5
        per 5s per channel. With multiple concurrent AI replies in the
        same channel each reply has to leave bucket room for the others
        AND for its own finalize edit. We therefore edit ONLY on phase
        changes that the user actually cares about (queue position
        change, new status, tool execution) and accumulate delta events
        SILENTLY into the buffer. The final body + footer + view land
        via :meth:`finalize` as a single edit -- which is far more
        likely to succeed than the previous "edit on every delta"
        approach that was burying the finalize edit behind 7+ throttled
        delta paints per reply.
        """
        kind = event.get("type")
        if kind == "queued":
            try:
                self._queue_position = int(event.get("position") or 0)
            except (TypeError, ValueError):
                self._queue_position = 0
            await self._edit(force=False)  # cheap throttled paint
        elif kind == "status":
            new_status = str(event.get("text") or self._status)
            status_changed = new_status != self._status
            self._status = new_status
            # Once we get any status from the bridge, the queue wait is
            # over -- we're in-flight.
            self._queue_position = 0
            if status_changed:
                await self._edit(force=False)
        elif kind == "tool_call":
            name = str(event.get("name") or "")
            ok = bool(event.get("ok"))
            marker = f"{'✓' if ok else '✗'} {name}"
            self._tool_runs.append(marker)
            if name and name not in self._tool_names:
                self._tool_names.append(name)
            await self._edit(force=False)
        elif kind == "delta":
            # Silent accumulation -- the finalize edit will land the
            # complete buffer in one shot. No per-delta edit (was the
            # primary cause of the channel edit-bucket saturation).
            self._buffer += str(event.get("text") or "")
        elif kind == "done":
            self._buffer = str(event.get("text") or self._buffer)
            self._meta_model = str(event.get("model") or self._meta_model)
            self._meta_elapsed_ms = int(event.get("elapsed_ms") or 0)
            self._meta_usage = dict(event.get("usage") or {})
            # No edit here -- finalize() lands the full state including
            # body + footer + view in a single edit. Force-edits here
            # have historically RACED with finalize and burned a slot
            # in the edit bucket for no user-visible gain.

    async def on_queue_position(self, position: int) -> None:
        """Called by the chat queue directly via the ticket's callback.

        Wraps :meth:`feed` so the chat queue's position callback can use
        the same path as bridge events. The bridge already emits
        ``queued`` events through the race-helper, so this is mostly a
        safety net for callers that wire the queue callback themselves.
        """
        await self.feed({"type": "queued", "position": int(position)})

    async def finalize(
        self,
        *,
        body: str,
        view: discord.ui.View | None,
        extra_views: Iterable[discord.ui.View] | None = None,
    ) -> None:
        """Land the final reply: complete body, footer, attached view.

        Trusts discord.py's internal 429 handling -- 429s sleep and retry
        inside the HTTP layer transparently, so wrapping the edit in our
        own 429 retry loop just stacks delay on top of delay. We only
        retry here for genuinely transient post-429 failures (5xx,
        network drops). On a hard 4xx, falls back ONCE to the same body
        without the view so the user at least sees the full reply, then
        the cog can attempt a separate follow-up view-attach if needed.

        ``view`` is the primary view attached to the message. ``extra_views``
        is unused by Discord (a Message holds one View at a time) but kept
        in the signature for forward compatibility.

        Catches broad Exception so a programmer error inside the View
        can't leave the placeholder stuck at a partial streaming paint.
        """
        self._done = True
        if self._edit_failed:
            log.info("[chat-status] finalize skipped: edit_failed already")
            return
        body = (body or "").strip()
        footer = self._footer_line()
        if not body:
            body = "_(no response)_"
        if len(body) > 1900:
            body = body[:1900]
        display = f"{body}\n-# {footer}" if footer else body
        display = display[:2000]
        log.debug(
            "[chat-status] finalize start: body=%d chars, footer=%d chars, view=%s, "
            "model=%s, elapsed_ms=%d, tools=%d",
            len(body), len(footer), type(view).__name__ if view else None,
            self._meta_model or "?", self._meta_elapsed_ms, len(self._tool_names),
        )
        # Attempt the full edit (content + view) once, trusting discord.py
        # to internally handle 429 by sleeping retry_after. On 5xx /
        # network error, retry up to 2 more times. On 4xx, drop the view
        # and try again with content only.
        try:
            await self._placeholder.edit(content=display, view=view)
            if view is not None:
                self._view_attached = True
            log.info("[chat-status] finalize ok (full)")
            return
        except discord.NotFound:
            self._edit_failed = True
            log.info("[chat-status] finalize: placeholder 404")
            return
        except discord.HTTPException as exc:
            status = getattr(exc, "status", None) or 0
            log.warning("[chat-status] finalize full-edit failed status=%d: %s",
                        status, exc)
            # Try content-only edit. If the view payload was the issue,
            # this lands the body+footer at least; the caller can attempt
            # a separate view-attach afterward.
            try:
                await self._placeholder.edit(content=display, view=None)
                log.info("[chat-status] finalize ok (content-only fallback)")
                return
            except discord.NotFound:
                self._edit_failed = True
                log.info("[chat-status] finalize content-only: placeholder 404")
                return
            except discord.HTTPException as exc2:
                log.warning("[chat-status] finalize content-only failed: %s", exc2)
            except Exception as exc2:
                log.warning("[chat-status] finalize content-only unexpected: %r", exc2)
        except asyncio.CancelledError:
            log.warning("[chat-status] finalize CANCELLED -- "
                        "outer wait_for likely timed out mid-edit")
            raise
        except Exception as exc:
            log.warning("[chat-status] finalize unexpected error: %r", exc)
        # Last-ditch: try once more after a short wait. If a transient
        # network or 5xx blip caused the failure, it usually heals
        # within 1-2 seconds.
        await asyncio.sleep(1.5)
        try:
            await self._placeholder.edit(content=display, view=view)
            if view is not None:
                self._view_attached = True
            log.info("[chat-status] finalize ok (after wait)")
        except Exception as exc:
            log.warning("[chat-status] finalize gave up: %r", exc)

    # ── internals ────────────────────────────────────────────────────────

    def _footer_line(self) -> str:
        """Build the small-text footer line shown beneath the reply.

        Mirrors the format from the pre-renderer help.py code so the
        observable Discord output is unchanged for users:
        ``gemini-2.5-flash  |  3 tools  |  2.4s  |  847 tokens``.
        """
        parts: list[str] = []
        model_label = self._meta_model or self._model
        if model_label:
            parts.append(model_label)
        if self._tool_names:
            n = len(self._tool_names)
            parts.append(f"{n} tool{'s' if n != 1 else ''}")
        elapsed = self._meta_elapsed_ms / 1000 if self._meta_elapsed_ms else (
            time.monotonic() - self._started_at
        )
        parts.append(f"{elapsed:.1f}s")
        total = self._meta_usage.get("total_tokens") or (
            (self._meta_usage.get("prompt_tokens") or 0)
            + (self._meta_usage.get("completion_tokens") or 0)
        )
        if total:
            parts.append(f"{total:,} tokens")
        return "  |  ".join(parts)

    def _next_frame(self) -> str:
        f = _BRAILLE_FRAMES[self._frame_idx % len(_BRAILLE_FRAMES)]
        self._frame_idx += 1
        return f

    def _phase_text(self) -> str:
        """Return the current phase string shown next to the spinner.

        Includes an elapsed-time counter so the user can see the request is
        still alive while a slow model (gemma4:31b-cloud at ~60 tok/s, etc)
        is grinding -- the old static "thinking..." line looked frozen.
        """
        if self._queue_position and self._queue_position > 0:
            return f"queued (#{self._queue_position})"
        elapsed = int(max(0.0, time.monotonic() - self._started_at))
        if elapsed >= 1:
            return f"{self._status} ({elapsed}s)"
        return self._status

    def _render_in_progress(self) -> str:
        """Build the in-progress placeholder string (buffer or spinner head)."""
        # Compact tool-runs sub-line: drop checks whose tool name is
        # already in the visible phase string so we don't print it twice.
        phase = self._phase_text()
        phase_lower = phase.lower()
        filtered_runs = [
            m for m in self._tool_runs
            if m.split(" ", 1)[-1].lower() not in phase_lower
        ]
        tools_line = "  ·  ".join(filtered_runs) if filtered_runs else ""
        if self._buffer:
            if tools_line:
                return f"-# {tools_line}\n{self._buffer}"
            return self._buffer
        head = f"{self._next_frame()} _{phase}_"
        if tools_line:
            return f"{head}\n-# {tools_line}"
        return head

    async def _edit(self, *, force: bool) -> None:
        """Edit the placeholder under the configured throttle.

        ``force=True`` always edits when the throttle window has elapsed
        and is used for state changes the user shouldn't miss (queue
        position bump, status flip, tool completion). ``force=False``
        is the delta-driven path that coalesces frequent token edits.
        """
        if self._edit_failed:
            return
        now = time.monotonic()
        if not force and (now - self._last_edit) < self._edit_throttle:
            return
        # Use a tight lock so concurrent feed() calls don't tear the
        # placeholder body. The lock body only contains the actual edit
        # call so contention stays bounded.
        async with self._lock:
            if self._edit_failed:
                return
            display = self._render_in_progress()
            display = display[:1990]
            try:
                await self._placeholder.edit(content=display)
                self._last_edit = now
            except discord.NotFound:
                self._edit_failed = True
            except discord.HTTPException as exc:
                if _is_rate_limit(exc):
                    # Burn the throttle window so we back off as a unit
                    # instead of pounding the bucket on every delta.
                    self._last_edit = now


def _is_rate_limit(exc: discord.HTTPException) -> bool:
    """Return True if the HTTP error is a Discord rate-limit (429).

    Mirrors the helper of the same name in cogs/help.py so the renderer
    doesn't have to reach back into the cog. Discord's ``HTTPException``
    exposes ``status``; we treat anything with a 429 as a rate limit.
    """
    return getattr(exc, "status", None) == 429
