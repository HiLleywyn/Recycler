"""clanklib/ratelimit.py -- paced execution for mass moderation actions.

Bursting hundreds of bans / role edits / clanks at once is what earns a
Cloudflare-level 429 (the multi-hour ban we hit cleaving 500 accounts). discord.py
sleeps through per-route limits, but it cannot undo a global/Cloudflare ban -- the
only safe play is to never burst. :class:`BulkRunner` serializes a batch, paces it
with a small adaptive delay, backs off on 429, and -- crucially -- aborts the whole
run after a few consecutive 429s instead of hammering on into a long ban.

Usage::

    runner = BulkRunner()
    result = await runner.run(members, do_one, progress=update_msg)

``do_one`` is an async callable taking one item; raising signals a failure for
that item (counted, not fatal). A 429 is detected from ``discord.HTTPException``
and retried with backoff up to a cap.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

import discord

log = logging.getLogger(__name__)


@dataclass
class BulkResult:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    aborted: bool = False
    abort_reason: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.succeeded + self.failed

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.processed)


def _retry_after(exc: Exception) -> Optional[float]:
    """Pull a retry-after (seconds) out of a 429, else None."""
    if not isinstance(exc, discord.HTTPException):
        return None
    if getattr(exc, "status", None) != 429:
        return None
    ra = getattr(exc, "retry_after", None)
    if ra is None:
        resp = getattr(exc, "response", None)
        try:
            ra = float(resp.headers.get("Retry-After")) if resp is not None else None
        except (TypeError, ValueError):
            ra = None
    return float(ra) if ra is not None else 1.0


class BulkRunner:
    """Serialized, paced executor with 429 backoff and an abort circuit-breaker."""

    def __init__(
        self,
        *,
        base_delay: float = 1.0,
        max_delay: float = 8.0,
        max_consecutive_429: int = 3,
        max_retries: int = 2,
        long_retry_abort: float = 60.0,
    ) -> None:
        # Pace between actions. 1.0s/action keeps us far under the global limit
        # while staying tolerable for a few-hundred-item batch.
        self.base_delay = base_delay
        self.max_delay = max_delay
        # Stop the whole run after this many 429s in a row -- the signal that
        # we're approaching a Cloudflare ban; better to abort than escalate.
        self.max_consecutive_429 = max_consecutive_429
        # Per-item retries on 429 before that item is counted failed.
        self.max_retries = max_retries
        # If Discord asks us to wait longer than this, treat it as a global/
        # Cloudflare limit and abort rather than sleeping for ages.
        self.long_retry_abort = long_retry_abort

    async def run(
        self,
        items: Sequence[Any],
        action: Callable[[Any], Awaitable[Any]],
        *,
        progress: Optional[Callable[[BulkResult], Awaitable[None]]] = None,
        progress_every: int = 25,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> BulkResult:
        result = BulkResult(total=len(items))
        delay = self.base_delay
        consecutive_429 = 0

        for idx, item in enumerate(items):
            if cancel is not None and cancel():
                result.aborted = True
                result.abort_reason = "cancelled"
                break

            ok, hit_429, fatal = await self._one(item, action, result)
            if fatal:
                result.aborted = True
                result.abort_reason = fatal
                break

            if hit_429:
                consecutive_429 += 1
                delay = min(self.max_delay, delay * 2)  # back off
                if consecutive_429 >= self.max_consecutive_429:
                    result.aborted = True
                    result.abort_reason = (
                        f"aborted after {consecutive_429} consecutive rate limits "
                        f"to avoid a longer ban; {result.remaining} not processed"
                    )
                    break
            else:
                consecutive_429 = 0
                # Decay the delay back toward the base after clean actions.
                delay = max(self.base_delay, delay * 0.8)

            if progress is not None and (idx + 1) % progress_every == 0:
                try:
                    await progress(result)
                except Exception:  # noqa: BLE001
                    pass

            # Pace: sleep between actions (skip after the last item).
            if idx < len(items) - 1:
                await asyncio.sleep(delay)

        if progress is not None:
            try:
                await progress(result)
            except Exception:  # noqa: BLE001
                pass
        return result

    async def _one(
        self, item: Any, action: Callable[[Any], Awaitable[Any]], result: BulkResult,
    ) -> tuple[bool, bool, str]:
        """Run one item with retry-on-429. Returns (ok, hit_429, fatal_reason)."""
        hit_429 = False
        for attempt in range(self.max_retries + 1):
            try:
                await action(item)
                result.succeeded += 1
                return True, hit_429, ""
            except Exception as exc:  # noqa: BLE001
                ra = _retry_after(exc)
                if ra is not None:
                    hit_429 = True
                    if ra > self.long_retry_abort:
                        # A long wait means a global/Cloudflare limit: abort.
                        result.failed += 1
                        return False, True, (
                            f"Discord asked us to wait {int(ra)}s (global rate "
                            f"limit); aborting to avoid a longer ban"
                        )
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(self.max_delay, ra + 0.5))
                        continue
                    result.failed += 1
                    if len(result.errors) < 10:
                        result.errors.append(f"{item}: rate limited")
                    return False, True, ""
                # Non-429 failure: count it and move on.
                result.failed += 1
                if len(result.errors) < 10:
                    result.errors.append(f"{item}: {type(exc).__name__}: {exc}")
                return False, False, ""
        return False, hit_429, ""
