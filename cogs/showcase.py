"""cogs/showcase.py -- ``,me`` command. Paginated showcase of the
player's stats / inventory / skills / buddies across every system.

One command (``,me`` + alias ``,showcase``) opens an embed with a
``Select`` menu of tabs (Overview / Wallet / Fishing / Farming /
Dungeon / Crafting / Buddies / Achievements). Picking a tab edits
the same message in place. A ``Bump`` button re-posts the message
at the bottom of the channel via the standard
core.framework.persistent_embeds helper so a long chat doesn't bury the
view.

All the heavy-lifting -- DB reads + composing each tab's content --
lives in services/showcase.py. This cog is presentation only.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.persistent_embeds import attach_bump_button
from core.framework.ui import (
    C_BUDDY, C_CRAFTING, C_DUNGEON, C_FARMING, C_FISHING, C_GOLD, C_INFO,
)
from services import showcase as _svc

log = logging.getLogger(__name__)


_SECTION_KEYS = (
    "overview", "wallet", "fishing", "farming",
    "dungeon", "crafting", "buddy", "achievements",
)


def _section(result: "_svc.ShowcaseResult", key: str) -> "_svc.ShowcaseSection":
    """Map the section key off ShowcaseResult."""
    return getattr(result, key)


def _build_embed(
    result: "_svc.ShowcaseResult",
    section_key: str,
    member: discord.User | discord.Member,
) -> discord.Embed:
    sec = _section(result, section_key)
    body = "\n".join(sec.lines) if sec.lines else "_(empty)_"
    color = {
        "overview":     C_GOLD,
        "wallet":       C_GOLD,
        "fishing":      C_FISHING,
        "farming":      C_FARMING,
        "dungeon":      C_DUNGEON,
        "crafting":     C_CRAFTING,
        "buddy":        C_BUDDY,
        "achievements": C_GOLD,
    }.get(section_key, C_INFO)
    embed = (
        card(sec.title, color=color, description=body)
        .footer(
            f"{member.display_name} · pick a tab below to switch · bump to re-post"
        )
        .build()
    )
    embed.set_author(
        name=member.display_name,
        icon_url=member.display_avatar.url if hasattr(member, "display_avatar") else None,
    )
    return embed


class _ShowcaseSelect(discord.ui.Select):
    """Tab-picker select. Each option corresponds to a ShowcaseResult
    field; switching just edits the existing message embed.
    """

    def __init__(self, owner_id: int, result: "_svc.ShowcaseResult", member):
        self.owner_id = int(owner_id)
        self.result = result
        self.member = member
        options = [
            discord.SelectOption(label="Overview",     value="overview",     emoji="\U0001F464"),
            discord.SelectOption(label="Wallet",       value="wallet",       emoji="\U0001F4B0"),
            discord.SelectOption(label="Fishing",      value="fishing",      emoji="\U0001F3A3"),
            discord.SelectOption(label="Farming",      value="farming",      emoji="\U0001F33E"),
            discord.SelectOption(label="Dungeon",      value="dungeon",      emoji="\U0001F5FA"),
            discord.SelectOption(label="Crafting",     value="crafting",     emoji="\U0001F528"),
            discord.SelectOption(label="Buddies",      value="buddy",        emoji="\U0001F436"),
            discord.SelectOption(label="Achievements", value="achievements", emoji="\U0001F3C5"),
        ]
        super().__init__(
            placeholder="Switch tab...", min_values=1, max_values=1,
            options=options, row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the owner of this showcase can switch tabs. "
                "Run `,me` to open your own.",
                ephemeral=True,
            )
            return
        choice = (self.values or ["overview"])[0]
        try:
            embed = _build_embed(self.result, choice, self.member)
        except Exception:
            log.debug("showcase: build embed failed", exc_info=True)
            await interaction.response.send_message(
                "Tab unavailable.", ephemeral=True,
            )
            return
        await interaction.response.edit_message(embed=embed, view=self.view)


class _PanelJumpButton(discord.ui.Button):
    """One-tap navigation button. On click posts the matching panel's
    command as an ephemeral hint so the player can copy-paste / re-run
    without leaving the showcase.
    """

    def __init__(
        self, owner_id: int, *,
        label: str, emoji: str, command: str, blurb: str,
        row: int = 2,
    ) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            row=row,
        )
        self.owner_id = int(owner_id)
        self.command = command
        self.blurb = blurb

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This isn't your showcase. Run `,me` to open your own.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"\U000027A1 **{self.label}**: run `{self.command}`\n"
            f"-# {self.blurb}",
            ephemeral=True,
        )


class _ShowcaseView(discord.ui.View):
    """Holds the tab-picker + jump buttons + a bump button. Long
    timeout so the showcase stays scrubbable for the rest of a
    session, then auto-expires.

    Layout follows top-down "what you want to do next":
      Row 0: Tab picker             (switch what's on screen)
      Row 1: Items / AH / Lexicon   (browse / inventory)
      Row 2: Craft / Fish / Farm    (jump into a system)
      Row 3: Bump                   (re-post at the bottom; rare action)
    """

    def __init__(self, owner_id: int, result, member) -> None:
        super().__init__(timeout=600)  # 10 min
        self.add_item(_ShowcaseSelect(owner_id, result, member))
        # Quick-jump buttons -- send ephemeral hints with the right
        # command for each panel. Same UX win as a hub menu without
        # the cross-cog invoke complexity.
        for jump in (
            ("Items",   "\U0001F4E6", ",items",
             "Per-unit NFT browser."),
            ("AH",      "\U0001F3DB", ",ah",
             "Auction-house catalog browser."),
            ("Lexicon", "\U0001F4D6", ",db",
             "Item lookup + 'where to get it'."),
        ):
            label, emoji, cmd, blurb = jump
            self.add_item(_PanelJumpButton(
                owner_id, label=label, emoji=emoji,
                command=cmd, blurb=blurb, row=1,
            ))
        for jump in (
            ("Craft",   "\U0001F528", ",craft list",
             "Recipes browser; pick + craft from the dropdown."),
            ("Fish",    "\U0001F3A3", ",fish stats",
             "Tackle box; equip bait + cast from the panel."),
            ("Farm",    "\U0001F33E", ",farm",
             "Plots + plant/harvest dropdowns."),
        ):
            label, emoji, cmd, blurb = jump
            self.add_item(_PanelJumpButton(
                owner_id, label=label, emoji=emoji,
                command=cmd, blurb=blurb, row=2,
            ))
        # Bump drops + re-posts at the bottom of the channel so this
        # view doesn't get buried by chat. Pinned to the last row by
        # itself so it reads as a one-shot housekeeping action rather
        # than another navigation jump.
        attach_bump_button(self, owner_id, row=3)


class Showcase(commands.Cog):
    """``,me`` -- single-pane stats / inventory / skills / buddies dashboard."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # ``profile`` used to live as an alias here, but V3 promoted
    # ``,profile`` to a first-class hybrid_group in cogs/profile.py
    # (the cosmetic-themed PNG card with the equip/unequip/shop/buy
    # subcommands). Keeping the alias here would collide with that
    # registration -- one of the two ends up shadowed and players
    # see whichever loaded second. The legacy showcase still runs
    # as ``,me`` and ``,showcase``.
    @commands.hybrid_command(name="me", aliases=["showcase"], with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def me_cmd(
        self, ctx: DiscoContext, target: discord.Member | None = None,
    ) -> None:
        """Show your overall stats, wallet, skills, and buddies.

        ``,me``           -- your own showcase
        ``,me @other``    -- view another player's (read-only)
        """
        member = target or ctx.author
        async with ctx.typing():
            try:
                result = await _svc.compute_showcase(
                    ctx.db, ctx.guild_id, int(member.id),
                    member_name=member.display_name,
                )
            except Exception:
                log.exception(
                    "showcase: compute failed for uid=%s gid=%s",
                    member.id, ctx.guild_id,
                )
                await ctx.reply_error(
                    "Couldn't load the showcase right now -- try again."
                )
                return
        embed = _build_embed(result, "overview", member)
        view = _ShowcaseView(int(ctx.author.id), result, member)
        await ctx.reply(embed=embed, view=view, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Showcase(bot))
