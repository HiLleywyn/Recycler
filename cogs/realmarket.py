"""Real-crypto market commands invoked via the ``$`` prefix.

This module is intentionally slim. The 12 legacy handlers (``_handle_chart``,
``_handle_scan``, ``_handle_info``, ``_handle_global``, ``_handle_top``,
``_handle_trending``, ``_handle_movers``, ``_handle_heatmap``,
``_handle_fear_greed``, ``_handle_dominance``, ``_handle_convert``,
``_handle_channels``) have been moved into :mod:`cogs._dollar.legacy` -- one
file per handler -- and every method on this cog is now a thin shim that
delegates to its migrated body. The dispatcher + lifecycle + channel-gate +
``_resolve_or_error`` glue all stay here because they're tightly coupled to
the ``on_message`` listener.

``,chart`` (game-token chart in :mod:`cogs.trade`) and ``$chart`` (live
CoinGecko chart) must be able to coexist as distinct commands with the same
short name. discord.py doesn't allow two commands to share a name, so the
``$`` namespace is intercepted with this listener and bypasses the command
framework dispatch entirely.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from services.real_market import RealMarketClient

from cogs._dollar.legacy import (
    channels as legacy_channels,
    chart as legacy_chart,
    convert as legacy_convert,
    info as legacy_info,
    market_wide as legacy_market,
    scan as legacy_scan,
)

log = logging.getLogger(__name__)


_DISPATCH_ALLOWLIST = frozenset({
    "chart", "c", "info", "i", "help", "h", "channels", "ch",
    "scan", "s", "pattern", "p",
    "global", "g", "total", "overview",
    "top", "t", "markets",
    "trending", "tr",
    "gainers", "winners",
    "losers", "dumpers",
    "heatmap", "hm", "hmap",
    "fear", "fg", "feargreed", "greed",
    "dom", "dominance",
    "convert", "conv",
    # New top-level groups (additive, no clutter):
    "query", "q", "ask",
    "watch", "w", "alerts", "alert",
    "compare", "cmp", "vs",
    "oracle", "or",
    "funding", "fund", "fr",
    "oi", "openinterest", "open_interest",
    "market", "m", "umbrella",
    "status", "health", "diag",
})


class RealMarket(commands.Cog):
    """``$``-prefixed real-crypto market commands.

    Dispatched via :meth:`on_message` rather than the standard command
    framework so the ``chart`` / ``info`` names can coexist with the
    game-side ``,chart`` and any future ``,info``. Handler bodies live
    in :mod:`cogs._dollar.legacy`; this class only carries the
    dispatcher + lifecycle + ``_resolve_or_error`` glue.
    """

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self.client = RealMarketClient(bot)
        # 5s per-user cooldown so a quick double-tap doesn't double-render.
        self._cooldown = commands.CooldownMapping.from_cooldown(
            1, 5.0, commands.BucketType.user,
        )
        # Background watch-alert worker. Started in ``cog_load`` so we
        # don't poll the DB before the connection pool is up.
        self._watch_worker = None

    async def cog_load(self) -> None:
        try:
            from services.market.watch_worker import WatchWorker
            self._watch_worker = WatchWorker(self.bot)
            self._watch_worker.start()
        except Exception:
            log.exception("[realmarket] watch worker failed to start")

    async def cog_unload(self) -> None:
        if self._watch_worker is not None:
            try:
                await self._watch_worker.stop()
            except Exception:
                log.exception("[realmarket] watch worker stop crashed")
        await self.client.close()

    # ── on_message dispatch ───────────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _route_dollar(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return
        if not Config.REAL_MARKET_ENABLED:
            return
        content = (message.content or "").strip()
        if not content.startswith("$") or len(content) < 2:
            return
        # Avoid clashing with currency-text patterns like "$5" or "$$":
        # everything after the `$` must start with a letter.
        body = content[1:].lstrip()
        if not body or not body[0].isalpha():
            return

        parts = body.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if sub not in _DISPATCH_ALLOWLIST:
            return

        # $channels is an admin command -- skip the bot-channel gate so an
        # admin can configure the allowlist from any channel they can use
        # the bot in. Every other $ command respects the gate.
        if sub not in ("channels", "ch"):
            if not await self._channel_allowed(message):
                return  # silent ignore so $-traffic outside allowed channels is invisible

        bucket = self._cooldown.get_bucket(message)
        retry = bucket.update_rate_limit()
        if retry:
            try:
                ctx = await self.bot.get_context(message, cls=DiscoContext)
                await ctx.reply_cooldown(retry)
            except Exception:
                pass
            return

        try:
            ctx = await self.bot.get_context(message, cls=DiscoContext)
        except Exception:
            log.exception("[$] get_context failed")
            return

        try:
            await self._dispatch(ctx, sub, rest)
        except Exception:
            log.exception("[$%s] handler crashed", sub)
            try:
                await ctx.reply_error(
                    f"`$" + sub + "` failed -- the host logs have the details."
                )
            except Exception:
                pass
        finally:
            # Mirror Bot.on_command's user-message auto-delete (cmd_delete_after).
            await self._schedule_cmd_message_delete(message)

    async def _dispatch(self, ctx: DiscoContext, sub: str, rest: str) -> None:
        """Route a ``$``-subcommand to the right migrated handler."""
        if sub in ("chart", "c"):
            await self._handle_chart(ctx, rest)
        elif sub in ("info", "i"):
            await self._handle_info(ctx, rest)
        elif sub in ("scan", "s", "pattern", "p"):
            # Optional ``ai`` modifier: strip it before the legacy
            # handler runs, then fire the AI overlay as a follow-up.
            want_ai = False
            scan_rest = rest
            if rest:
                tokens = rest.split()
                if any(t.lower() == "ai" for t in tokens):
                    want_ai = True
                    scan_rest = " ".join(
                        t for t in tokens if t.lower() != "ai"
                    )
            await self._handle_scan(ctx, scan_rest)
            if want_ai:
                try:
                    from cogs._dollar.scan_ai import maybe_run_scan_ai
                    tokens = (scan_rest or "").split()
                    symbol = tokens[0] if tokens else ""
                    tf = tokens[1] if len(tokens) > 1 else None
                    if symbol:
                        await maybe_run_scan_ai(
                            ctx, symbol=symbol, timeframe=tf,
                        )
                except Exception:
                    log.exception("[$scan ai] overlay failed")
        elif sub in ("global", "g", "total", "overview"):
            await self._handle_global(ctx, rest)
        elif sub in ("top", "t", "markets"):
            await self._handle_top(ctx, rest)
        elif sub in ("trending", "tr"):
            await self._handle_trending(ctx, rest)
        elif sub in ("gainers", "winners"):
            await self._handle_movers(ctx, rest, direction="gainers")
        elif sub in ("losers", "dumpers"):
            await self._handle_movers(ctx, rest, direction="losers")
        elif sub in ("heatmap", "hm", "hmap"):
            await self._handle_heatmap(ctx, rest)
        elif sub in ("fear", "fg", "feargreed", "greed"):
            await self._handle_fear_greed(ctx)
        elif sub in ("dom", "dominance"):
            await self._handle_dominance(ctx)
        elif sub in ("convert", "conv"):
            await self._handle_convert(ctx, rest)
        elif sub in ("query", "q", "ask"):
            from cogs._dollar.query_handler import handle_query
            await handle_query(ctx, rest)
        elif sub in ("watch", "w", "alerts", "alert"):
            from cogs._dollar.watch_handler import handle_watch
            await handle_watch(ctx, rest)
        elif sub in ("compare", "cmp", "vs"):
            from cogs._dollar.compare_handler import handle_compare
            await handle_compare(ctx, rest)
        elif sub in ("oracle", "or"):
            from cogs._dollar.oracle_handler import handle_oracle
            await handle_oracle(ctx, rest)
        elif sub in ("funding", "fund", "fr"):
            from cogs._dollar.derivs_handler import handle_funding
            await handle_funding(ctx, rest)
        elif sub in ("oi", "openinterest", "open_interest"):
            from cogs._dollar.derivs_handler import handle_oi
            await handle_oi(ctx, rest)
        elif sub in ("market", "m", "umbrella"):
            from cogs._dollar.market_handler import handle_market
            await handle_market(ctx, rest, self)
        elif sub in ("status", "health", "diag"):
            from cogs._dollar.status_handler import handle_status
            await handle_status(ctx, rest)
        elif sub in ("help", "h"):
            from cogs._dollar.help_handler import handle_help_v2
            await handle_help_v2(ctx)
        elif sub in ("channels", "ch"):
            await self._handle_channels(ctx, rest)

    async def _schedule_cmd_message_delete(self, message: discord.Message) -> None:
        """Auto-delete the user's ``$``-command message after the guild's
        ``cmd_delete_after`` window. Replicates
        :meth:`core.framework.bot.Discoin.on_command` because the on_message
        listener bypasses discord.py's command dispatch (the place that
        hook is wired up).
        """
        if not message.guild:
            return
        try:
            settings = await self.bot.db.get_guild_settings(message.guild.id)
            delay = int(settings.get("cmd_delete_after", 0) or 0)
        except Exception:
            return
        if delay <= 0:
            return

        bot = self.bot

        async def _delete(m: discord.Message = message, d: int = delay) -> None:
            await asyncio.sleep(d)
            bot._autodelete_done.add(m.id)

            async def _cleanup(mid: int = m.id) -> None:
                await asyncio.sleep(10)
                bot._autodelete_done.discard(mid)

            asyncio.create_task(_cleanup())
            try:
                await m.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                log.warning(
                    "[$ autodelete] cannot delete command in #%s -- bot needs "
                    "Manage Messages permission",
                    getattr(m.channel, "name", "?"),
                )
            except Exception as exc:
                log.warning("[$ autodelete] delete failed: %s", exc)

        task = asyncio.create_task(_delete())
        bot._autodelete_tasks.add(task)
        task.add_done_callback(bot._autodelete_tasks.discard)
        bot._autodelete_by_msg[message.id] = task
        task.add_done_callback(
            lambda _t, mid=message.id: bot._autodelete_by_msg.pop(mid, None)
        )

    async def _channel_allowed(self, message: discord.Message) -> bool:
        """Return True if this channel is allowed to run $-commands.

        Effective allowlist is the union of two admin-configured sets:
        ``guild_settings.bot_channels`` (the global game-command list, so
        any channel admins already opened for ``,`` commands gets ``$``
        for free) and ``guild_settings.realmarket_channels`` (an extra
        list configured via ``$channels add`` that enables ``$`` without
        opening the channel up to the rest of the game surface).
        When BOTH are empty the gate is open everywhere -- matches the
        bot's existing default.
        """
        guild = message.guild
        if guild is None:
            return False
        try:
            bot_chs = set(await self.bot.db.guilds.get_bot_channels(guild.id))
        except Exception:
            bot_chs = set()
        try:
            rm_chs = set(await self.bot.db.guilds.get_realmarket_channels(guild.id))
        except Exception:
            rm_chs = set()
        effective = bot_chs | rm_chs
        if not effective:
            return True
        ch = message.channel
        ch_id = getattr(ch, "id", 0)
        if ch_id in effective:
            return True
        if isinstance(ch, discord.Thread) and getattr(ch, "parent_id", 0) in effective:
            return True
        return False

    # ── shared helpers ────────────────────────────────────────────

    async def _resolve_or_error(
        self, ctx: DiscoContext, symbol: str, *, command_name: str,
    ) -> dict | None:
        """CoinGecko-first symbol resolver with router fallthrough.

        Crypto path: CoinGecko's resolver is canonical (it carries the
        cached ``id`` every downstream method expects). For anything else
        (equities / ETFs / forex / commodities / indices) the
        cross-asset market router fills in a synthetic record and tags
        the asset class so the migrated chart / info handlers can route
        into :mod:`cogs._dollar.chart_handler` /
        :mod:`cogs._dollar.info_handler`.
        """
        record = await self.client.resolve_symbol(symbol)
        if record and record.get("id"):
            record["_asset_class"] = "crypto"
            return record
        try:
            from services.market.router import get_router
            router = get_router(self.bot)
            resolved = await router.resolve(symbol)
        except Exception:
            log.exception("[$%s] router.resolve crashed", command_name)
            resolved = None
        if resolved is not None and resolved.asset_class.value != "crypto":
            return {
                "id": resolved.provider_id,
                "symbol": resolved.symbol,
                "name": resolved.name,
                "thumb": resolved.image,
                "_asset_class": resolved.asset_class.value,
                "_resolved": resolved,
            }
        await ctx.reply_error_hint(
            f"Couldn't find a market for `{symbol.upper()}`.",
            hint=(
                "Crypto: ticker like MTA, ARC, SOL, XRP, DOGE -- or a "
                "CoinGecko id like `moneta`.\n"
                "Equities / ETFs: ticker like AAPL, MSFT, SPY, QQQ.\n"
                "Forex: pair like EURUSD or EUR=X.\n"
                "Commodity / index futures: ^GSPC, GC=F, CL=F."
            ),
            command_name=command_name,
        )
        return None

    # ── handler shims (bodies live in cogs/_dollar/legacy/) ───────
    #
    # These exist so the dispatcher and ``cogs._dollar.market_handler``
    # can keep using ``cog._handle_X`` without caring where the body
    # actually lives.

    async def _handle_chart(self, ctx: DiscoContext, raw_args: str) -> None:
        await legacy_chart.handle(ctx, raw_args, cog=self)

    async def _handle_scan(self, ctx: DiscoContext, raw_args: str) -> None:
        await legacy_scan.handle(ctx, raw_args, cog=self)

    async def _handle_info(self, ctx: DiscoContext, raw_args: str) -> None:
        await legacy_info.handle(ctx, raw_args, cog=self)

    async def _handle_global(self, ctx: DiscoContext, raw_args: str) -> None:
        await legacy_market.handle_global(ctx, raw_args, cog=self)

    async def _handle_top(self, ctx: DiscoContext, raw_args: str) -> None:
        await legacy_market.handle_top(ctx, raw_args, cog=self)

    async def _handle_trending(self, ctx: DiscoContext, raw_args: str) -> None:
        await legacy_market.handle_trending(ctx, raw_args, cog=self)

    async def _handle_movers(
        self, ctx: DiscoContext, raw_args: str, *, direction: str,
    ) -> None:
        await legacy_market.handle_movers(
            ctx, raw_args, direction=direction, cog=self,
        )

    async def _handle_heatmap(self, ctx: DiscoContext, raw_args: str) -> None:
        await legacy_market.handle_heatmap(ctx, raw_args, cog=self)

    async def _handle_fear_greed(self, ctx: DiscoContext) -> None:
        await legacy_market.handle_fear_greed(ctx, cog=self)

    async def _handle_dominance(self, ctx: DiscoContext) -> None:
        await legacy_market.handle_dominance(ctx, cog=self)

    async def _handle_convert(self, ctx: DiscoContext, raw_args: str) -> None:
        await legacy_convert.handle(ctx, raw_args, cog=self)

    async def _handle_channels(self, ctx: DiscoContext, raw_args: str) -> None:
        await legacy_channels.handle(ctx, raw_args, cog=self)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(RealMarket(bot))
