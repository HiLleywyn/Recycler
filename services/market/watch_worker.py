"""Background worker that drives ``$watch`` alerts.

Polls active rows in :sql:`market_watchlist` every
:data:`Config.MARKET_ALERT_INTERVAL` seconds, fetches a fresh quote per
distinct symbol via the market router, and fires a Discord alert when
the configured threshold is crossed. Each row triggers exactly once --
the worker stamps ``triggered_at`` after delivery so re-triggers require
the user to recreate the alert.

The worker is owned by ``cogs/realmarket.py``: an instance is created on
cog load and torn down on unload. Failures are swallowed per-row so one
broken provider can't kill the loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from core.config import Config
from core.framework.embed import card
from core.framework.ui import C_SUCCESS, C_WARNING, fmt_usd

from .router import get_router

log = logging.getLogger(__name__)


class WatchWorker:
    """Single-instance background loop."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="market.watch_worker")
        log.info("[market.watch] worker started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None
        log.info("[market.watch] worker stopped")

    async def _run(self) -> None:
        interval = max(15, int(getattr(Config, "MARKET_ALERT_INTERVAL", 60)))
        # First tick after a short delay so we don't pound the DB at boot.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("[market.watch] tick crashed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        db = getattr(self.bot, "db", None)
        if db is None:
            return
        try:
            rows = await db.fetch_all(
                "SELECT id, user_id, guild_id, symbol, asset_class, "
                "       target_price, direction, notify_channel "
                "FROM market_watchlist "
                "WHERE target_price IS NOT NULL "
                "  AND direction IS NOT NULL "
                "  AND triggered_at IS NULL "
                "ORDER BY created_at ASC "
                "LIMIT 200",
            )
        except Exception as exc:
            log.debug("[market.watch] DB fetch failed: %s", exc)
            return
        if not rows:
            return

        # Group by symbol so we hit each provider once per tick.
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            key = (r.get("symbol") or "").upper()
            if not key:
                continue
            by_symbol.setdefault(key, []).append(dict(r))

        router = get_router(self.bot)
        for sym, batch in by_symbol.items():
            try:
                resolved = await router.resolve(sym)
            except Exception:
                continue
            if resolved is None:
                continue
            try:
                quote = await router.quote(resolved)
            except Exception:
                continue
            if quote is None or quote.price_usd <= 0:
                continue
            price = float(quote.price_usd)
            for row in batch:
                target = row.get("target_price")
                if target is None:
                    continue
                try:
                    target_val = float(target)
                except (TypeError, ValueError):
                    continue
                direction = (row.get("direction") or "").lower()
                triggered = (
                    (direction == "above" and price >= target_val)
                    or (direction == "below" and price <= target_val)
                )
                if not triggered:
                    continue
                await self._deliver(row, sym, price, target_val, direction)

    async def _deliver(
        self,
        row: dict[str, Any],
        symbol: str,
        price: float,
        target: float,
        direction: str,
    ) -> None:
        """Mark the row triggered, send a Discord ping. Failure to deliver
        does NOT un-trigger the row -- we'd rather miss a notification
        than spam a user with re-triggers."""
        db = getattr(self.bot, "db", None)
        if db is not None:
            try:
                await db.execute(
                    "UPDATE market_watchlist "
                    "SET triggered_at = NOW() "
                    "WHERE id = $1",
                    row["id"],
                )
            except Exception as exc:
                log.debug("[market.watch] mark-triggered failed: %s", exc)
                return

        bot = self.bot
        channel_id = row.get("notify_channel")
        user_id = row.get("user_id")
        channel = None
        if channel_id:
            try:
                channel = bot.get_channel(int(channel_id))
                if channel is None:
                    channel = await bot.fetch_channel(int(channel_id))
            except Exception:
                channel = None
        if channel is None:
            # Fall back to DM.
            try:
                user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
                channel = await user.create_dm()
            except Exception:
                log.debug("[market.watch] no destination for row %s", row.get("id"))
                return

        arrow = "🔺" if direction == "above" else "🔻"
        color = C_SUCCESS if direction == "above" else C_WARNING
        embed = (
            card(
                f"{arrow} $watch · {symbol} triggered",
                description=(
                    f"<@{user_id}> -- **{symbol}** crossed your alert.\n"
                    f"Now: **{fmt_usd(price)}** "
                    f"({direction} target {fmt_usd(target)})"
                ),
                color=color,
            )
            .footer("$watch · one-shot alert · re-add to re-arm")
            .build()
        )
        try:
            await channel.send(
                content=f"<@{user_id}>",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except Exception as exc:
            log.debug("[market.watch] send failed: %s", exc)
