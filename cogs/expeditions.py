"""cogs/expeditions.py -- ``,expedition`` AI Buddy Expedition surface.

Send your active buddy on a 1-12 hour autonomous run to one of four
destinations (Whispering Forest / Coral Reef / Forgotten Mine / Ancient
Ruins). They come back with a procedural story log, a weighted loot
drop, and some XP.

Commands
--------
``,expedition`` (alias ``,exped`` / ``,trek``)
    Status panel: active runs (countdown), pending collectables, and
    a "Send" button that opens the picker view.

``,expedition send`` -- direct shortcut to the picker view.
``,expedition collect [id]`` -- collect a finished run (or all ready
    runs when no id passed).
``,expedition history`` -- last 10 collected runs.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import (
    C_GOLD, C_INFO, C_NAVY, C_SUCCESS, C_TEAL, fmt_ts,
)

import configs.expeditions_config as ec
from services import expeditions as exp_svc

log = logging.getLogger(__name__)


async def _publish_started(
    bot, *, ctx: DiscoContext, res: exp_svc.ExpeditionResult,
) -> None:
    """Fire-and-forget bus event. Quests + achievements subscribe to it.

    Failure is logged at debug level only -- the player has already had
    the expedition created in the DB, so a flaky bus publish must NOT
    block the success path or roll back the row.
    """
    try:
        await bot.bus.publish(
            "expedition_started",
            user=ctx.author,
            guild=ctx.guild,
            destination=res.destination,
            duration_seconds=res.duration_seconds,
            buddy_id=res.buddy_id,
            species=res.species,
            rarity_tier=res.rarity_tier,
            expedition_id=res.expedition_id,
        )
    except Exception:
        log.debug("expedition_started publish failed", exc_info=True)


async def _publish_collected(
    bot, *, ctx: DiscoContext, cr: exp_svc.CollectResult,
) -> None:
    """Fire-and-forget bus event for collected runs.

    Payload includes the loot summary so future quests can hook
    "collect 5 fish from Reef expeditions" without a service signature
    change.
    """
    try:
        await bot.bus.publish(
            "expedition_collected",
            user=ctx.author,
            guild=ctx.guild,
            destination=cr.destination,
            species=cr.species,
            xp_gained=cr.xp_gained,
            happiness_delta=cr.happiness_delta,
            loot=cr.loot,
            expedition_id=cr.expedition_id,
        )
    except Exception:
        log.debug("expedition_collected publish failed", exc_info=True)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_remaining(seconds_remaining: int | None) -> str:
    """Render DB-computed seconds-remaining as ``Xh Ym`` (or ``Ready!``).

    The value comes straight off the row's ``seconds_remaining`` column
    so the wall-clock display always matches what the DB just used to
    decide ready vs. running.
    """
    if seconds_remaining is None:
        return "?"
    s = int(seconds_remaining)
    if s <= 0:
        return "**Ready!**"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _dest_pretty(key: str) -> tuple[str, str]:
    """Return ``(emoji, name)`` for a destination key."""
    meta = ec.destination_meta(key) or {}
    return (
        str(meta.get("emoji") or "\U0001F30D"),
        str(meta.get("name") or key.title()),
    )


def _loot_summary_line(loot: dict) -> str:
    """Compress a loot dict into one short line for status / history."""
    bits: list[str] = []
    for sym, qty in (loot.get("ore") or {}).items():
        bits.append(f"{int(qty)} {sym}")
    rune = float(loot.get("rune") or 0.0)
    if rune > 0:
        bits.append(f"{rune:,.1f} RUNE")
    if loot.get("fish"):
        bits.append(f"{len(loot['fish'])} fish")
    if loot.get("crops"):
        bits.append(f"{len(loot['crops'])} crops")
    if loot.get("junk"):
        bits.append(f"{len(loot['junk'])} junk")
    if not bits:
        return "_(no loot)_"
    return " · ".join(bits)


def _build_status_embed(
    user: discord.abc.User, active: list[dict], ready: list[dict],
) -> discord.Embed:
    if not active and not ready:
        body = (
            "_(no active expeditions)_\n\n"
            "Send your active buddy on a timed run to gather loot from "
            "one of four destinations:\n"
            + "\n".join(
                f"{_dest_pretty(k)[0]} **{_dest_pretty(k)[1]}** -- "
                f"_{m['blurb']}_  ·  min level **{m['min_level']}**"
                for k, m in ec.DESTINATIONS.items()
            )
            + "\n\nTap **Send** to open the picker."
        )
        return (
            card(
                f"\U0001F392 {user.display_name}'s Expeditions",
                color=C_GOLD, description=body,
            )
            .footer(
                "Affinity-matched buddies hit harder · Higher-rarity buddies "
                "find more loot · Story log + loot drop on collect."
            )
            .build()
        )

    builder = card(
        f"\U0001F392 {user.display_name}'s Expeditions",
        color=C_TEAL,
    )
    if active:
        lines = []
        for r in active:
            emoji, name = _dest_pretty(str(r["destination"]))
            lines.append(
                f"{emoji} **{name}** -- buddy #{int(r['buddy_id'])}\n"
                f"-# `#{int(r['expedition_id'])}` · "
                f"started {fmt_ts(r.get('started_at'))} · "
                f"back in {_fmt_remaining(int(r.get('seconds_remaining') or 0))}"
            )
        builder.field(
            f"Running ({len(active)})", "\n".join(lines), inline=False,
        )
    if ready:
        lines = []
        for r in ready:
            emoji, name = _dest_pretty(str(r["destination"]))
            lines.append(
                f"{emoji} **{name}** -- run #`{int(r['expedition_id'])}` ready"
            )
        builder.field(
            f"\U0001F381 Ready to collect ({len(ready)})",
            "\n".join(lines),
            inline=False,
        )
    return builder.footer(
        "Tap Collect to claim a finished run · Send opens the picker."
    ).build()


# ---------------------------------------------------------------------------
# Picker view -- ``,expedition send``
# ---------------------------------------------------------------------------


class _DestSelect(discord.ui.Select):
    """Row-0 destination dropdown."""

    def __init__(self, parent: "_PickerView") -> None:
        opts = []
        for key, m in ec.DESTINATIONS.items():
            opts.append(discord.SelectOption(
                label=str(m["name"]),
                value=key,
                description=f"Min level {int(m['min_level'])} -- {m['blurb']}"[:100],
                emoji=str(m["emoji"]),
                default=(parent.destination == key),
            ))
        super().__init__(
            placeholder="Pick a destination...",
            options=opts, min_values=1, max_values=1, row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_PickerView" = self.view  # type: ignore[assignment]
        view.destination = str(self.values[0])
        await view._redraw(interaction)


class _DurationButton(discord.ui.Button):
    """Row-1 duration toggle. The view tracks one selection at a time;
    pressing a duration toggles its style and disables the others'
    primary highlight on the next refresh.
    """

    def __init__(self, dur: dict, parent: "_PickerView") -> None:
        super().__init__(
            label=str(dur["label"]),
            style=(
                discord.ButtonStyle.primary
                if parent.duration_key == dur["key"]
                else discord.ButtonStyle.secondary
            ),
            row=1,
        )
        self._dur_key = str(dur["key"])

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_PickerView" = self.view  # type: ignore[assignment]
        view.duration_key = self._dur_key
        await view._redraw(interaction)


class _BuddySelect(discord.ui.Select):
    """Row-2 buddy dropdown. Lets the player choose which buddy goes on
    the expedition instead of always defaulting to the active one.

    Eligible buddies are owned and not already on a running expedition.
    The picker view loads them once at construction time; if a buddy
    finishes / leaves between the initial load and Send, the service
    layer re-checks status under FOR UPDATE so the worst case is a
    "buddy is already on an expedition" error, not a double-deploy.
    """

    def __init__(self, parent: "_PickerView") -> None:
        opts: list[discord.SelectOption] = []
        # Discord caps a single select at 25 options. The picker takes
        # the first 25 (active first via the SQL ORDER) which mirrors
        # the buddy panel's dropdown limit.
        for b in parent.buddies[:25]:
            try:
                from configs.buddies_config import (
                    SPECIES as _SPECIES,
                    rarity_meta as _b_rarity,
                    effective_level as _eff_lvl,
                )
                emoji = (
                    str((_SPECIES.get(str(b.get("species") or "")) or {}).get("emoji") or "")
                    or "\U0001F436"
                )
                tier_name = str(
                    _b_rarity(int(b.get("rarity_tier") or 1)).get("name")
                    or "Common"
                )
                lvl = _eff_lvl(b)
            except Exception:
                emoji, tier_name, lvl = "\U0001F436", "Common", int(b.get("level") or 1)
            name = str(b.get("name") or "Buddy")
            active_tag = " · active" if b.get("is_active") else ""
            opts.append(discord.SelectOption(
                label=f"{name} -- L{lvl} {tier_name}"[:100],
                value=str(int(b.get("id") or 0)),
                description=(f"{tier_name}{active_tag}")[:100],
                emoji=emoji,
                default=(int(b.get("id") or 0) == parent.buddy_id),
            ))
        if not opts:
            opts = [discord.SelectOption(
                label="(no eligible buddies)", value="_none_", default=True,
            )]
        super().__init__(
            placeholder="Pick which buddy to send...",
            options=opts, min_values=1, max_values=1, row=2,
            disabled=(len(opts) == 1 and opts[0].value == "_none_"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_PickerView" = self.view  # type: ignore[assignment]
        v = str(self.values[0])
        if v == "_none_":
            await interaction.response.defer()
            return
        try:
            view.buddy_id = int(v)
        except ValueError:
            await interaction.response.defer()
            return
        await view._redraw(interaction)


class _PickerView(discord.ui.View):
    """Owner-locked, 5-min timeout. Picks destination + duration + buddy
    before the player commits via Send. Send closes the picker and stamps
    a confirmation embed in place; Cancel closes without sending.
    """

    def __init__(
        self, ctx: DiscoContext, *,
        default_dest: str = "forest",
        buddies: list[dict] | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.destination: str = default_dest
        self.duration_key: str = ec.DURATIONS[0]["key"]
        self.message: discord.Message | None = None
        self.buddies: list[dict] = list(buddies or [])
        # Default selection: the active buddy if one is eligible, else
        # whichever buddy SQL returned first.
        active_match = next(
            (int(b.get("id") or 0) for b in self.buddies if b.get("is_active")),
            int(self.buddies[0].get("id") or 0) if self.buddies else 0,
        )
        self.buddy_id: int = active_match
        self._build_components()

    def _build_components(self) -> None:
        self.clear_items()
        self.add_item(_DestSelect(self))
        for dur in ec.DURATIONS:
            self.add_item(_DurationButton(dict(dur), self))
        self.add_item(_BuddySelect(self))
        self.add_item(_SendButton())
        self.add_item(_CancelButton())

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your picker. Run `,expedition send` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def _build_embed(self) -> discord.Embed:
        emoji, name = _dest_pretty(self.destination)
        m = ec.destination_meta(self.destination) or {}
        dur = ec.duration_meta(self.duration_key) or {}
        body = (
            f"{emoji} **{name}** -- _{m.get('blurb', '')}_\n"
            f"-# Min level **{int(m.get('min_level') or 1)}** · "
            f"Affinity: **{str(m.get('species_affinity') or 'neutral').title()}**\n\n"
            f"Duration: **{dur.get('label', '?')}** "
            f"({int(dur.get('draws') or 0)} loot draws, "
            f"+{int(dur.get('xp_gain') or 0)} XP)"
        )
        # Show the currently selected buddy so the picker doesn't feel
        # like it's quietly defaulting to the active buddy. Players who
        # never touch the dropdown still see "Sending: <active buddy>".
        chosen = next(
            (b for b in self.buddies if int(b.get("id") or 0) == self.buddy_id),
            None,
        )
        if chosen:
            try:
                from configs.buddies_config import (
                    SPECIES as _SPECIES,
                    rarity_meta as _b_rarity,
                    effective_level as _eff_lvl,
                )
                b_emoji = str(
                    (_SPECIES.get(str(chosen.get("species") or "")) or {}).get("emoji") or "",
                )
                tier_name = str(
                    _b_rarity(int(chosen.get("rarity_tier") or 1)).get("name") or "Common",
                )
                lvl = _eff_lvl(chosen)
            except Exception:
                b_emoji, tier_name = "", "Common"
                lvl = int(chosen.get("level") or 1)
            buddy_name = str(chosen.get("name") or "Buddy")
            body += (
                f"\n\nSending: {b_emoji} **{buddy_name}** "
                f"(Lv. {lvl} {tier_name})"
            )
        elif not self.buddies:
            body += "\n\n_No eligible buddies right now -- hatch one or wait for an expedition to finish._"
        return (
            card(
                "\U0001F392 Send Expedition",
                color=C_INFO, description=body,
            )
            .footer(
                "Affinity-matched buddy = +25% loot quantity · "
                "Higher rarity = better odds per draw."
            )
            .build()
        )

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build_components()
        embed = self._build_embed()
        if interaction.response.is_done():
            if self.message is not None:
                try:
                    await self.message.edit(embed=embed, view=self)
                except discord.HTTPException:
                    pass
        else:
            await interaction.response.edit_message(embed=embed, view=self)


class _SendButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Send", emoji="\U0001F392",
            style=discord.ButtonStyle.success, row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_PickerView" = self.view  # type: ignore[assignment]
        try:
            res = await exp_svc.start_expedition(
                view.ctx.db,
                view.ctx.guild_id, int(interaction.user.id),
                destination=view.destination,
                duration_key=view.duration_key,
                buddy_id=int(view.buddy_id) if view.buddy_id else None,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except Exception as e:
            log.exception("expedition send failed")
            await interaction.response.send_message(
                f"Could not send: `{type(e).__name__}: {e}`.",
                ephemeral=True,
            )
            return
        await _publish_started(view.ctx.bot, ctx=view.ctx, res=res)
        emoji, dest_name = _dest_pretty(res.destination)
        body = (
            f"**{res.buddy_name}** ({res.species.title()}, T{res.rarity_tier}) "
            f"set off for the {emoji} **{dest_name}**.\n"
            f"-# Run `#{res.expedition_id}` -- back at **{fmt_ts(res.ends_at)}** "
            f"({_fmt_remaining(int(res.duration_seconds))})\n"
            f"-# Run `,expedition collect {res.expedition_id}` when they're back."
        )
        for child in view.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        embed = card(
            "\U0001F392 Expedition Started",
            color=C_SUCCESS, description=body,
        ).build()
        await interaction.response.edit_message(embed=embed, view=view)


class _CancelButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Cancel", emoji="\U0000274C",
            style=discord.ButtonStyle.secondary, row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_PickerView" = self.view  # type: ignore[assignment]
        for child in view.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        await interaction.response.edit_message(view=view)


# ---------------------------------------------------------------------------
# Status / collect helpers
# ---------------------------------------------------------------------------


class _StatusView(discord.ui.View):
    """Row-0: Send (opens picker), Collect All (if any ready), Refresh."""

    def __init__(self, ctx: DiscoContext) -> None:
        super().__init__(timeout=300)
        self.ctx = ctx
        self.message: discord.Message | None = None

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Not your panel.", ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _redraw(self, interaction: discord.Interaction) -> None:
        active = await exp_svc.list_active(
            self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
        )
        # ``seconds_remaining`` is computed DB-side, negative when the
        # run has finished. Split here without a Python clock compare.
        ready = [r for r in active if int(r.get("seconds_remaining") or 1) <= 0]
        running_only = [r for r in active if int(r.get("seconds_remaining") or 1) > 0]
        embed = _build_status_embed(self.ctx.author, running_only, ready)
        # Disable Collect All when nothing's ready.
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label == "Collect All":
                child.disabled = not ready
        if interaction.response.is_done():
            if self.message is not None:
                try:
                    await self.message.edit(embed=embed, view=self)
                except discord.HTTPException:
                    pass
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Send", emoji="\U0001F392",
        style=discord.ButtonStyle.primary, row=0,
    )
    async def btn_send(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        buddies = await exp_svc.list_eligible_buddies(
            self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
        )
        view = _PickerView(self.ctx, buddies=buddies)
        embed = view._build_embed()
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=False,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(
        label="Collect All", emoji="\U0001F381",
        style=discord.ButtonStyle.success, row=0,
    )
    async def btn_collect_all(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        ready = await exp_svc.list_collectable(
            self.ctx.db, self.ctx.guild_id, self.ctx.author.id,
        )
        if not ready:
            await interaction.response.send_message(
                "No expeditions ready to collect.", ephemeral=True,
            )
            return
        results = []
        failures = []
        for r in ready:
            try:
                cr = await exp_svc.collect_expedition(
                    self.ctx.db, self.ctx.bot,
                    self.ctx.guild_id, int(interaction.user.id),
                    int(r["expedition_id"]),
                )
                results.append(cr)
                await _publish_collected(self.ctx.bot, ctx=self.ctx, cr=cr)
            except ValueError as e:
                failures.append(str(e))
            except Exception as e:
                log.exception("collect failed exp=%s", r.get("expedition_id"))
                failures.append(f"{type(e).__name__}: {e}")
        if not results and failures:
            await interaction.response.send_message(
                "All collects failed:\n" + "\n".join(f"- {f}" for f in failures[:5]),
                ephemeral=True,
            )
            return
        # Surface a concise multi-collect receipt; full per-run stories
        # are available via `,expedition history` so the bulk receipt
        # stays short.
        body_lines = []
        for cr in results:
            emoji, name = _dest_pretty(cr.destination)
            body_lines.append(
                f"{emoji} **{name}** -- {cr.buddy_name} -- "
                f"{_loot_summary_line(cr.loot)} (+{cr.xp_gained} XP)"
            )
        await interaction.response.send_message(
            embed=card(
                f"\U0001F381 Collected {len(results)} Expeditions",
                color=C_SUCCESS,
                description="\n".join(body_lines),
            ).footer("`,expedition history` for the full story log.").build(),
            ephemeral=False,
        )
        await self._redraw(interaction)

    @discord.ui.button(
        label="Refresh", emoji="\U0001F504",
        style=discord.ButtonStyle.secondary, row=0,
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await self._redraw(interaction)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Expeditions(commands.Cog):
    """Send active buddies on autonomous timed runs for procedural loot."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_check(self, ctx) -> bool:
        """Premium gate: expeditions are paid; admins do NOT bypass."""
        from services import entitlements
        from core.framework.premium import PremiumGateFailure
        if not await entitlements.is_premium(ctx.guild_id, ctx.db):
            raise PremiumGateFailure("expeditions")
        return True

    @commands.hybrid_group(
        name="expedition",
        aliases=["exped", "trek", "exp"],
        invoke_without_command=True, with_app_command=False,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def expedition(self, ctx: DiscoContext) -> None:
        """Status panel for your buddy expeditions."""
        from services.onboarding import maybe_send_intro
        await maybe_send_intro(ctx, "expedition")
        active = await exp_svc.list_active(ctx.db, ctx.guild_id, ctx.author.id)
        ready = [r for r in active if int(r.get("seconds_remaining") or 1) <= 0]
        running_only = [r for r in active if int(r.get("seconds_remaining") or 1) > 0]
        view = _StatusView(ctx)
        for child in view.children:
            if isinstance(child, discord.ui.Button) and child.label == "Collect All":
                child.disabled = not ready
        embed = _build_status_embed(ctx.author, running_only, ready)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg

    @expedition.command(name="send", aliases=["go"])
    @guild_only
    @no_bots
    @ensure_registered
    async def expedition_send(self, ctx: DiscoContext) -> None:
        """Open the picker view to send an expedition."""
        buddies = await exp_svc.list_eligible_buddies(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        view = _PickerView(ctx, buddies=buddies)
        embed = view._build_embed()
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg

    @expedition.command(name="collect", aliases=["claim", "return"])
    @guild_only
    @no_bots
    @ensure_registered
    async def expedition_collect(
        self, ctx: DiscoContext, expedition_id: int | None = None,
    ) -> None:
        """Collect a finished expedition (or all ready when no id given)."""
        if expedition_id is None:
            ready = await exp_svc.list_collectable(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if not ready:
                await ctx.reply_error(
                    "No expeditions ready to collect. "
                    "Run `,expedition` to see what's still running."
                )
                return
            collected = []
            for r in ready:
                try:
                    cr = await exp_svc.collect_expedition(
                        ctx.db, ctx.bot, ctx.guild_id, ctx.author.id,
                        int(r["expedition_id"]),
                    )
                    collected.append(cr)
                    await _publish_collected(ctx.bot, ctx=ctx, cr=cr)
                except Exception:
                    log.exception("collect failed exp=%s", r.get("expedition_id"))
            if not collected:
                await ctx.reply_error("All collects failed; check logs.")
                return
            body = "\n".join(
                f"{_dest_pretty(c.destination)[0]} "
                f"**{_dest_pretty(c.destination)[1]}** -- {c.buddy_name} -- "
                f"{_loot_summary_line(c.loot)} (+{c.xp_gained} XP)"
                for c in collected
            )
            await ctx.send_embed(
                card(
                    f"\U0001F381 Collected {len(collected)} Expeditions",
                    color=C_SUCCESS, description=body,
                ).footer(
                    "`,expedition history` for the full story logs."
                ).build()
            )
            return
        try:
            cr = await exp_svc.collect_expedition(
                ctx.db, ctx.bot, ctx.guild_id, ctx.author.id,
                int(expedition_id),
            )
        except ValueError as e:
            await ctx.reply_error(str(e))
            return
        await _publish_collected(ctx.bot, ctx=ctx, cr=cr)
        await ctx.send_embed(_render_collect_embed(cr))

    @expedition.command(name="history", aliases=["log", "past"])
    @guild_only
    @no_bots
    @ensure_registered
    async def expedition_history(self, ctx: DiscoContext) -> None:
        """Show the last 10 collected expeditions."""
        rows = await ctx.db.fetch_all(
            """
            SELECT expedition_id, destination, started_at, collected_at,
                   xp_gained, happiness_delta, loot_json, story_json,
                   species_at_start
              FROM buddy_expeditions
             WHERE guild_id = $1 AND user_id = $2 AND status = 'collected'
             ORDER BY collected_at DESC
             LIMIT 10
            """,
            ctx.guild_id, ctx.author.id,
        )
        if not rows:
            await ctx.reply_error(
                "No collected expeditions yet. Send your buddy on a run "
                "via `,expedition send`."
            )
            return
        builder = card(
            f"\U0001F4DC {ctx.author.display_name}'s Expedition Log",
            color=C_NAVY,
        )
        for r in rows:
            emoji, name = _dest_pretty(str(r["destination"]))
            loot = r.get("loot_json") or {}
            if isinstance(loot, str):
                try:
                    import json as _json
                    loot = _json.loads(loot)
                except Exception:
                    loot = {}
            head = (
                f"{emoji} **{name}** -- "
                f"#{int(r['expedition_id'])} "
                f"_{str(r.get('species_at_start') or '').title()}_"
            )
            sub = (
                f"-# {fmt_ts(r.get('collected_at'))} · "
                f"{_loot_summary_line(loot)} · "
                f"+{int(r.get('xp_gained') or 0)} XP"
            )
            builder.field(head, sub, inline=False)
        await ctx.send_embed(builder.build())

    @expedition.command(name="help")
    @guild_only
    @no_bots
    async def expedition_help(self, ctx: DiscoContext) -> None:
        """Quick reference for the expedition system."""
        prefix = await ctx.get_guild_prefix()
        body = (
            f"**Send your active buddy** on a 1-12 hour autonomous run. "
            f"They come back with a procedural story log + a weighted "
            f"loot drop pulled from fishing, farming, and delve pools.\n\n"
            f"**Commands**\n"
            f"`{prefix}expedition` -- status panel (active + ready runs)\n"
            f"`{prefix}expedition send` -- open the picker view\n"
            f"`{prefix}expedition collect [id]` -- collect a finished run\n"
            f"`{prefix}expedition history` -- last 10 collected runs\n\n"
            f"**Affinity** -- buddies aligned with a destination earn "
            f"+25% loot quantity and a rarity bump on each draw. Higher "
            f"buddy rarity gives a flat per-draw multiplier. Longer runs "
            f"draw more times, not better odds per draw.\n\n"
            f"**Destinations**\n"
            + "\n".join(
                f"{m['emoji']} **{m['name']}** -- min level "
                f"**{int(m['min_level'])}** · _{m['blurb']}_"
                for m in ec.DESTINATIONS.values()
            )
        )
        await ctx.send_embed(
            card("\U0001F392 Expeditions Help", color=C_GOLD, description=body).build()
        )


def _render_collect_embed(cr: exp_svc.CollectResult) -> discord.Embed:
    """Big collect embed: destination header, story log paragraphs,
    loot field, XP/happiness footer.
    """
    emoji, name = _dest_pretty(cr.destination)
    color = C_SUCCESS
    builder = card(
        f"{emoji} {cr.buddy_name} returned from {name}",
        color=color,
    )
    if cr.story:
        builder.field(
            "Run log",
            "\n\n".join(cr.story),
            inline=False,
        )
    builder.field(
        "Loot",
        _loot_summary_line(cr.loot),
        inline=False,
    )
    foot_bits = [f"+{cr.xp_gained} XP"]
    if cr.happiness_delta:
        sign = "+" if cr.happiness_delta > 0 else ""
        foot_bits.append(f"happiness {sign}{cr.happiness_delta}")
    foot_bits.append(f"`#{cr.expedition_id}`")
    builder.footer(" · ".join(foot_bits))
    return builder.build()


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Expeditions(bot))
