"""cogs/events.py - Dynamic multi-phase market events system.

Events evolve through phases (buildup -> peak -> aftermath), each with
distinct modifiers for volatility, bias, fees, liquidity, mining,
staking, and lending.  Phase transitions are announced in Discord with
escalating tone and visuals.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only, module_cog_check
from core.framework.ui import C_INFO, C_AMBER, C_GRAY, C_BLURPLE, fmt_ts, fmt_usd

from configs.market_events_config import (
    EVENT_REGISTRY,
    EventPhase,
    MarketEvent,
    C_BEAR,
)
from services.market_event_engine import (
    get_active_event,
    clear_active_event,
    get_history,
    get_cooldown_remaining,
    pick_random_event,
    start_event,
    advance_phase,
    end_event,
    should_advance_phase,
    is_final_phase,
    event_time_remaining,
    resolve_effective_phase,
)

log = logging.getLogger("discoin.events")

# ── Legacy compat: keep MARKET_EVENTS importable for status.py / dev.py ──────
# Maps old flat keys to the new registry.
MARKET_EVENTS = {eid: {
    "title": ev.display_name,
    "emoji": ev.emoji,
    "description": ev.description,
    "vol_mult": ev.phases[0].vol_multiplier,
    "bias": ev.phases[0].price_bias_pct_per_day / 100.0,
    "duration": ev.total_duration_seconds,
    "weight": ev.rarity_weight,
    "color": ev.phases[0].embed_color,
} for eid, ev in EVENT_REGISTRY.items()}


# ── Embed builders ───────────────────────────────────────────────────────────

def _phase_embed(
    ev: MarketEvent,
    phase: EventPhase,
    phase_idx: int,
    total_phases: int,
    phase_end_ts: float,
    is_start: bool = False,
) -> discord.Embed:
    """Build a Discord embed for a phase transition."""
    phase_label = phase.name.replace("_", " ").title()
    title = f"{ev.emoji} {ev.display_name} \u2014 {phase_label}"
    if is_start:
        title = f"{ev.emoji} {ev.display_name}"

    _b = card(title, description=phase.flavor_text, color=phase.embed_color)

    if is_start:
        _b._embed.description = f"*{ev.description}*\n\n**Phase 1: {phase_label}**\n{phase.flavor_text}"

    # Modifier fields
    bias = phase.price_bias_pct_per_day
    direction = "\U0001f4c8 Bullish" if bias > 0 else "\U0001f4c9 Bearish" if bias < 0 else "\u27a1\ufe0f Neutral"
    _b.field("Direction", direction, True)
    _b.field("Volatility", f"{phase.vol_multiplier:.1f}x", True)
    _b.field("Bias", f"{bias:+.1f}%/day", True)

    # Show non-default modifiers
    extras = []
    if phase.fee_multiplier != 1.0:
        extras.append(f"Fees: {phase.fee_multiplier:.1f}x")
    if phase.slippage_mult != 1.0:
        extras.append(f"Slippage: {phase.slippage_mult:.1f}x")
    if phase.liquidity_drain_pct != 0.0:
        if phase.liquidity_drain_pct > 0:
            extras.append(f"Liquidity drain: {phase.liquidity_drain_pct:+.0f}%")
        else:
            extras.append(f"Liquidity inflow: {abs(phase.liquidity_drain_pct):.0f}%")
    if phase.mining_difficulty_mult != 1.0:
        extras.append(f"Mining diff: {phase.mining_difficulty_mult:.1f}x")
    if phase.staking_apy_mult != 1.0:
        extras.append(f"Staking APY: {phase.staking_apy_mult:.1f}x")
    if phase.lending_rate_mult != 1.0:
        extras.append(f"Lending rate: {phase.lending_rate_mult:.1f}x")
    if extras:
        _b.field("Active Modifiers", " \u2022 ".join(extras), False)

    _b.footer(f"Phase {phase_idx + 1}/{total_phases} - Ends {fmt_ts(int(phase_end_ts))}")
    return _b.build()


def _event_end_embed(ev: MarketEvent, summary: dict) -> discord.Embed:
    """Build an embed for when an event ends."""
    duration_m = int(summary.get("duration_seconds", 0)) // 60
    desc = f"The **{ev.display_name}** has concluded after {duration_m} minutes."
    if summary.get("cancelled"):
        desc = f"The **{ev.display_name}** was cancelled."

    _b = card(f"{ev.emoji} {ev.display_name} \u2014 Over", description=desc, color=C_GRAY)

    impacts = summary.get("price_impacts", {})
    if impacts:
        top = sorted(impacts.items(), key=lambda x: abs(x[1]), reverse=True)[:6]
        impact_lines = []
        for sym, pct in top:
            arrow = "\U0001f7e2" if pct >= 0 else "\U0001f534"
            impact_lines.append(f"{arrow} **{sym}**: {pct:+.2f}%")
        _b.field("Price Impact", "\n".join(impact_lines), False)

    _b.footer("Markets returning to normal conditions")
    return _b.build()


def _event_embed(event_key: str, remaining_seconds: float = 0) -> discord.Embed:
    """Legacy embed builder  -  used by admin commands for quick display."""
    ev = EVENT_REGISTRY.get(event_key)
    if ev is None:
        return card("Unknown Event", description=event_key, color=C_INFO).build()
    phase = ev.phases[0]
    bias_pct = phase.price_bias_pct_per_day
    direction = "\U0001f4c8 Bullish" if bias_pct > 0 else "\U0001f4c9 Bearish" if bias_pct < 0 else "\u27a1\ufe0f Neutral"
    _b = card(f"{ev.emoji} {ev.display_name}", description=ev.description, color=phase.embed_color)
    _b.field("Direction", direction, True)
    _b.field("Phases", str(len(ev.phases)), True)
    _b.field("Total Duration", f"{ev.total_duration_seconds // 60}min", True)
    if remaining_seconds > 0:
        m, s = divmod(int(remaining_seconds), 60)
        _b.field("Expires", f"{m}m {s}s", True)
    _b.footer("Multi-phase event \u2014 modifiers change as phases progress")
    return _b.build()


# ── Announcement helper ──────────────────────────────────────────────────────

async def _announce(bot: Discoin, guild: discord.Guild, content: str, embed: discord.Embed) -> None:
    """Send an event announcement to the configured channel."""
    settings = await bot.db.get_guild_settings(guild.id)
    ch_id = (settings.get("events_channel") or settings.get("crypto_channel")) if settings else None
    if not ch_id:
        log.debug("[events._announce] No events/crypto channel configured for guild %s", guild.id)
        return
    ch = guild.get_channel(int(ch_id))
    if ch is None:
        log.warning(
            "[events._announce] Channel %s not found in guild %s (deleted or bot lacks access?)",
            ch_id, guild.id,
        )
        return
    if not hasattr(ch, "send"):
        log.warning("[events._announce] Channel %s is not a text channel in guild %s", ch_id, guild.id)
        return
    try:
        await ch.send(content=content, embed=embed)
    except discord.Forbidden:
        log.warning(
            "[events._announce] Missing permissions to send in channel %s (guild %s)",
            ch_id, guild.id,
        )
    except Exception as exc:
        log.error(
            "[events._announce] Failed to send to channel %s (guild %s): %s",
            ch_id, guild.id, exc, exc_info=True,
        )


async def _dm_event_start(bot: Discoin, guild: discord.Guild, ev: MarketEvent, embed: discord.Embed) -> None:
    """DM users who opted in to event notifications."""
    try:
        rows = await bot.db.fetch_all(
            "SELECT user_id FROM user_prefs WHERE guild_id=$1 AND dm_events=TRUE",
            guild.id,
        )
        for row in (rows or [])[:50]:
            try:
                member = guild.get_member(row["user_id"])
                if member:
                    await member.send(
                        content=f"**{ev.emoji} Market event on {guild.name}!**",
                        embed=embed,
                    )
            except Exception:
                pass
    except Exception:
        pass


# ── Trigger entry point ─────────────────────────────────────────────────────

async def trigger_event(db, guild: discord.Guild, event_key: str, bot=None) -> None:
    """Activate a multi-phase market event for a guild.

    This is the main entry point called by both the drift task (random trigger)
    and admin commands.
    """
    ev = EVENT_REGISTRY.get(event_key)
    if ev is None:
        return

    # Get Redis handle
    redis = getattr(bot, "bus", None) and getattr(bot.bus, "_redis", None)

    # Capture start prices for impact tracking
    start_prices: dict[str, float] = {}
    if bot:
        try:
            prices = await db.get_all_prices(guild.id)
            start_prices = {r["symbol"]: float(r["price"]) for r in prices}
        except Exception:
            pass

    # Start the event in Redis
    ae = await start_event(redis, guild.id, event_key, start_prices)

    # Also write to guild_settings for backward compat with the drift task
    phase = ev.phases[0]
    bias_per_tick = phase.price_bias_pct_per_day / 100.0  # convert pct to fraction
    from datetime import timedelta
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ev.total_duration_seconds)
    await db.set_guild_event(
        guild.id, event_key, phase.vol_multiplier, bias_per_tick, expires_at,
    )

    # Build and send announcement
    if bot:
        phase_end_ts = ae.phase_started_at + phase.duration_minutes * 60
        embed = _phase_embed(ev, phase, 0, len(ev.phases), phase_end_ts, is_start=True)
        await _announce(bot, guild, f"**{ev.emoji} MARKET EVENT: {ev.display_name.upper()}**", embed)
        await _dm_event_start(bot, guild, ev, embed)

    # Publish event start to bus
    if bot and hasattr(bot, "bus"):
        await bot.bus.publish("market_event_started", guild=guild, event_id=event_key)


# ── Helper for reading active event from Redis ──────────────────────────────

def _pick_random_event() -> str:
    """Legacy compatibility  -  pick using weights from registry."""
    keys = list(EVENT_REGISTRY.keys())
    weights = [EVENT_REGISTRY[k].rarity_weight for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


# ── Cog ──────────────────────────────────────────────────────────────────────

class Events(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._event_tick.start()

    def cog_unload(self) -> None:
        self._event_tick.cancel()

    async def cog_check(self, ctx) -> bool:
        return await module_cog_check(self.bot, ctx, "events")

    @property
    def _redis(self):
        bus = getattr(self.bot, "bus", None)
        return getattr(bus, "_redis", None) if bus else None

    # ── Background tick  -  phase progression ──────────────────────────────────

    @tasks.loop(seconds=20)
    async def _event_tick(self) -> None:
        """Check every guild for phase advancement or event expiry."""
        for guild in self.bot.guilds:
            try:
                await self._tick_guild(guild)
            except Exception as exc:
                log.error(
                    "[events._event_tick] guild %s: %s", guild.id, exc, exc_info=True
                )

    @_event_tick.before_loop
    async def _before_event_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild) -> None:
        redis = self._redis
        ae = await get_active_event(redis, guild.id)
        if ae is None:
            # No active event  -  roll for a random one based on guild's event_frequency
            await self._maybe_trigger_random_event(guild, redis)
            return

        ev = EVENT_REGISTRY.get(ae.event_id)
        if ev is None:
            await clear_active_event(redis, guild.id)
            await self.bot.db.clear_guild_event(guild.id)
            return

        if not should_advance_phase(ae):
            return

        # Loop to catch up on multiple elapsed phases in one tick
        # (e.g. if a short phase elapsed while the bot was busy)
        max_advances = len(ev.phases)  # safety cap
        for _ in range(max_advances):
            if not should_advance_phase(ae):
                break

            if is_final_phase(ae):
                # Event complete  -  collect end prices and clean up
                end_prices: dict[str, float] = {}
                try:
                    prices = await self.bot.db.get_all_prices(guild.id)
                    end_prices = {r["symbol"]: float(r["price"]) for r in prices}
                except Exception:
                    pass
                summary = await end_event(redis, guild.id, end_prices=end_prices)
                await self.bot.db.clear_guild_event(guild.id)

                # Announce end
                if summary:
                    embed = _event_end_embed(ev, summary)
                    await _announce(self.bot, guild, f"**{ev.emoji} {ev.display_name} \u2014 Event Over**", embed)

                await self.bot.bus.publish("market_event_ended", guild=guild, event_id=ae.event_id)
                return  # event is done, no more phases to process
            else:
                # Advance to next phase
                ae = await advance_phase(redis, ae)
                phase = ev.phases[ae.phase_index]

                # Update guild_settings with new phase modifiers
                bias_per_tick = phase.price_bias_pct_per_day / 100.0
                remaining_seconds = event_time_remaining(ae)
                from datetime import timedelta
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=remaining_seconds)
                await self.bot.db.set_guild_event(
                    guild.id, ae.event_id, phase.vol_multiplier, bias_per_tick, expires_at,
                )

                # Announce phase transition
                phase_end_ts = ae.phase_started_at + phase.duration_minutes * 60
                embed = _phase_embed(ev, phase, ae.phase_index, len(ev.phases), phase_end_ts)
                phase_label = phase.name.replace("_", " ").title()
                await _announce(
                    self.bot, guild,
                    f"**{ev.emoji} {ev.display_name} \u2014 Phase {ae.phase_index + 1}: {phase_label}**",
                    embed,
                )

                await self.bot.bus.publish(
                    "market_event_phase",
                    guild=guild,
                    event_id=ae.event_id,
                    phase_index=ae.phase_index,
                    phase_name=phase.name,
                )

    async def _maybe_trigger_random_event(self, guild: discord.Guild, redis) -> None:
        """Roll against the guild's event_frequency and start a random event if it fires."""
        try:
            settings = await self.bot.db.get_guild_settings(guild.id)
            if not settings:
                return
            if not settings.get("module_events", True):
                return
            freq = float(settings.get("event_frequency") or 0.0005)
            if freq <= 0:
                return
            if random.random() >= freq:
                return  # didn't fire this tick

            disabled_raw = settings.get("disabled_events", "") or ""
            disabled = set(filter(None, disabled_raw.split(",")))
            event_key = await pick_random_event(redis, guild.id, disabled)
            if event_key is None:
                return

            log.info(
                "[events] Random event '%s' triggered for guild %s (freq=%.4f)",
                event_key, guild.id, freq,
            )
            await trigger_event(self.bot.db, guild, event_key, bot=self.bot)
        except Exception as exc:
            log.error(
                "[events._maybe_trigger_random_event] guild %s: %s", guild.id, exc, exc_info=True
            )

    # ── Player commands ──────────────────────────────────────────────────────
    # All under the `event` / `events` group to avoid colliding with
    # the `crypto` cog's "market" alias.

    @commands.hybrid_group(name="event", aliases=["events"], invoke_without_command=True, with_app_command=False)
    @guild_only
    async def event(self, ctx: DiscoContext) -> None:
        """View the current market event."""
        await self._show_status(ctx)

    @event.command(name="status")
    @guild_only
    async def event_status(self, ctx: DiscoContext) -> None:
        """Show the current active event, phase, and modifiers."""
        await self._show_status(ctx)

    async def _show_status(self, ctx: DiscoContext) -> None:
        redis = self._redis
        ae = await get_active_event(redis, ctx.guild_id)
        if ae is None:
            await ctx.reply(
                embed=card(
                    "\U0001f4e1 Market Status",
                    description="No active market event. Markets are calm... for now.",
                    color=C_INFO,
                ).build(),
                mention_author=False,
            )
            return

        ev = EVENT_REGISTRY.get(ae.event_id)
        if ev is None:
            await ctx.reply_error("Unknown event state.")
            return

        # Resolve effective phase (fast-forward past elapsed phases)
        eff_idx, eff_started = resolve_effective_phase(ae)
        if eff_idx >= len(ev.phases):
            # Event fully expired but tick hasn't cleaned it up yet
            await ctx.reply(
                embed=card(
                    "\U0001f4e1 Market Status",
                    description=f"The **{ev.display_name}** event is wrapping up...",
                    color=C_GRAY,
                ).build(),
                mention_author=False,
            )
            return

        phase = ev.phases[eff_idx]
        phase_end_ts = eff_started + phase.duration_minutes * 60
        embed = _phase_embed(ev, phase, eff_idx, len(ev.phases), phase_end_ts)
        total_remaining = event_time_remaining(ae)
        m, s = divmod(int(total_remaining), 60)

        # Add total event time as an extra field
        embed.add_field(name="Total Event Time Left", value=f"{m}m {s}s", inline=True)

        await ctx.reply(embed=embed, mention_author=False)

    @event.command(name="list", aliases=["types"])
    @guild_only
    async def event_list(self, ctx: DiscoContext) -> None:
        """View all possible market event types."""
        lines = []
        for eid, ev in EVENT_REGISTRY.items():
            dur_m = ev.total_duration_seconds // 60
            phases_str = f"{len(ev.phases)} phases"
            lines.append(
                f"{ev.emoji} **{ev.display_name}** \u2014 {phases_str}, "
                f"{dur_m}min, rarity {ev.rarity_weight}"
            )
        embed = card("\U0001f4cb Market Event Types", description="\n".join(lines), color=C_INFO)
        embed.footer("Events trigger randomly or can be started by admins")
        await ctx.reply(embed=embed.build(), mention_author=False)

    @event.command(name="history")
    @guild_only
    async def event_history(self, ctx: DiscoContext) -> None:
        """Show the last 10 market events."""
        redis = self._redis
        history = await get_history(redis, ctx.guild_id, limit=10)
        if not history:
            await ctx.reply(
                embed=card(
                    "\U0001f4dc Event History",
                    description="No recent events recorded.",
                    color=C_INFO,
                ).build(),
                mention_author=False,
            )
            return

        lines = []
        for h in history:
            eid = h.get("event_id", "?")
            ev = EVENT_REGISTRY.get(eid)
            emoji = ev.emoji if ev else "\u2753"
            name = h.get("display_name", eid)
            dur = int(h.get("duration_seconds", 0)) // 60
            started = int(h.get("started_at", 0))
            tag = " (cancelled)" if h.get("cancelled") else ""

            impacts = h.get("price_impacts", {})
            impact_str = ""
            if impacts:
                top = sorted(impacts.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
                parts = [f"{sym} {pct:+.1f}%" for sym, pct in top]
                impact_str = f" [{', '.join(parts)}]"

            ts_str = fmt_ts(int(started)) if started else "?"
            lines.append(f"{emoji} **{name}** \u2014 {dur}min{tag}{impact_str} ({ts_str})")

        await ctx.reply(
            embed=card(
                "\U0001f4dc Event History",
                description="\n".join(lines),
                color=C_INFO,
            ).footer("Showing last 10 events").build(),
            mention_author=False,
        )

    @event.command(name="forecast")
    @guild_only
    async def event_forecast(self, ctx: DiscoContext) -> None:
        """Vague hints about upcoming events based on cooldown status."""
        redis = self._redis
        hints = []

        # Check which events are close to coming off cooldown
        for eid, ev in EVENT_REGISTRY.items():
            remaining = await get_cooldown_remaining(redis, ctx.guild_id, eid)
            if remaining == 0:
                continue
            # Only hint if cooldown is <25% remaining
            total_cd = ev.cooldown_minutes * 60
            if remaining < total_cd * 0.25:
                # Vague hints based on event type
                _hints = {
                    "bull_run": "Analysts sense growing optimism in the market...",
                    "bear_market": "Smart money appears to be hedging positions...",
                    "fed_rate_hike": "Whispers of monetary policy changes...",
                    "fed_rate_cut": "Central bankers appear dovish in recent speeches...",
                    "black_swan": "An eerie calm has settled over the markets...",
                    "whale_pump": "Large wallet movements detected on-chain...",
                    "rug_pull": "A project's social media activity seems... suspicious...",
                    "pandemic": "Global health officials are holding emergency meetings...",
                    "regulation": "Lobbyists are unusually active on Capitol Hill...",
                    "adoption": "Tech blogs buzzing about a major upcoming announcement...",
                    "etf_approved": "SEC filing documents are being reviewed...",
                    "exchange_hack": "Security researchers flagging unusual network traffic...",
                }
                hint = _hints.get(eid, "Something is brewing...")
                hints.append(f"\U0001f52e {hint}")

        if not hints:
            desc = "The crystal ball is cloudy. No clear signals detected."
        else:
            desc = "\n".join(hints[:5])

        await ctx.reply(
            embed=card(
                "\U0001f52e Market Forecast",
                description=desc,
                color=C_AMBER,
            ).footer("Forecasts are vague and unreliable \u2014 trade at your own risk").build(),
            mention_author=False,
        )

    # ── Vault / Server Level Commands ────────────────────────────────────────

    @commands.hybrid_group(name="vault", aliases=["vaults"], invoke_without_command=True, with_app_command=False)
    @guild_only
    async def vault_group(self, ctx: DiscoContext, *, network: str = "") -> None:
        """View this server's network vault levels and progression."""
        from constants.vaults import (
            VAULT_DISPLAY, ALL_VAULT_NETWORKS, level_for_balance,
            next_threshold, progress_pct, MAX_LEVEL,
        )

        network = network.strip().lower()
        if network and network not in ALL_VAULT_NETWORKS:
            await ctx.reply(
                embed=card(
                    description=f"Unknown network. Valid: {', '.join(ALL_VAULT_NETWORKS)}",
                    color=C_BEAR,
                ).build(),
                mention_author=False,
            )
            return

        vaults = await ctx.db.get_all_vaults(ctx.guild_id)
        vault_map = {v["network"]: v for v in vaults}

        if network:
            # Single network detail view
            v = vault_map.get(network, {"balance": 0.0, "level": 0})
            bal = float(v["balance"])
            lvl = level_for_balance(network, bal)
            disp = VAULT_DISPLAY.get(network, {"name": network.upper(), "emoji": "\U0001f4e6", "color": C_GRAY})
            nxt = next_threshold(network, lvl)
            pct = progress_pct(network, bal, lvl)
            bar_len = 20
            filled = int(pct * bar_len)
            bar = "\u2588" * filled + "\u2591" * (bar_len - filled)

            desc = (
                f"**Level {lvl}** / {MAX_LEVEL}\n"
                f"Balance: **${bal:,.2f}**\n\n"
                f"`[{bar}]` {pct*100:.1f}%\n"
            )
            if nxt:
                desc += f"Next level at **{fmt_usd(nxt)}**  ({fmt_usd(nxt - bal)} to go)"
            else:
                desc += "\U0001f451 **MAX LEVEL REACHED**"

            await ctx.reply(
                embed=card(
                    f"{disp['emoji']} {disp['name']} Vault",
                    description=desc,
                    color=disp["color"],
                ).footer("Vaults grow from transaction fees - keep trading!").build(),
                mention_author=False,
            )
        else:
            # Overview of all networks
            lines = []
            for net in ALL_VAULT_NETWORKS:
                v = vault_map.get(net, {"balance": 0.0, "level": 0})
                bal = float(v["balance"])
                lvl = level_for_balance(net, bal)
                disp = VAULT_DISPLAY.get(net, {"name": net.upper(), "emoji": "\U0001f4e6", "color": C_GRAY})
                pct = progress_pct(net, bal, lvl)
                nxt = next_threshold(net, lvl)
                bar_len = 10
                filled = int(pct * bar_len)
                bar = "\u2588" * filled + "\u2591" * (bar_len - filled)

                nxt_str = fmt_usd(nxt) if nxt else "MAX"
                lines.append(
                    f"{disp['emoji']} **{disp['name']}** - Level **{lvl}**\n"
                    f"\u2003`[{bar}]` ${bal:,.2f} / {nxt_str}"
                )

            desc = "\n\n".join(lines)
            await ctx.reply(
                embed=card(
                    "\U0001f3e6 Server Network Vaults",
                    description=desc,
                    color=C_BLURPLE,
                ).footer("Use ,vault <network> for details - e.g. ,vault sun").build(),
                mention_author=False,
            )

    @vault_group.command(name="sun")
    @guild_only
    async def vault_sun(self, ctx: DiscoContext) -> None:
        """View the Sun Network vault."""
        await self.vault_group(ctx, network="sun")

    @vault_group.command(name="mta")
    @guild_only
    async def vault_btc(self, ctx: DiscoContext) -> None:
        """View the Moneta Chain vault."""
        await self.vault_group(ctx, network="mta")

    @vault_group.command(name="arc")
    @guild_only
    async def vault_eth(self, ctx: DiscoContext) -> None:
        """View the Arcadia Network vault."""
        await self.vault_group(ctx, network="arc")

    @vault_group.command(name="dsc")
    @guild_only
    async def vault_dsc(self, ctx: DiscoContext) -> None:
        """View the Discoin Network vault."""
        await self.vault_group(ctx, network="dsc")


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Events(bot))
