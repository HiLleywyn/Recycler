"""V3 Pillar 7: ``,start`` onboarding deck cog."""
from __future__ import annotations

import io
import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import C_GOLD, C_INFO
from services import onboarding_deck as _svc

log = logging.getLogger(__name__)


class _DeckView(discord.ui.View):
    """Two-button navigator: Next / Skip."""

    def __init__(
        self,
        bot: Discoin,
        user_id: int,
        *,
        current_idx: int = 0,
        timeout: float = 600.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user_id = user_id
        # The View owns the navigation cursor so a "Next" press always
        # advances exactly one card from what's on screen, even if the
        # persisted DB row is ahead (e.g. user rerunning after completion).
        self.current_idx = current_idx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This onboarding deck belongs to someone else.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Next card", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, _btn) -> None:
        from services.onboarding_deck import set_progress, DECK, render_card
        nxt = min(len(DECK), self.current_idx + 1)
        self.current_idx = nxt
        await set_progress(self.bot.db, self.user_id, nxt)
        png = render_card(nxt)
        file = discord.File(io.BytesIO(png), filename="start.png")
        title = (
            f"Discoin Tour  -  card {nxt + 1}/{len(DECK)}"
            if nxt < len(DECK)
            else "Discoin Tour  -  complete"
        )
        if nxt < len(DECK):
            desc_lines = [f"**{DECK[nxt]['title']}**"]
            blurb = DECK[nxt].get("blurb")
            if blurb:
                desc_lines.append(blurb)
            desc = "\n\n".join(desc_lines)
        else:
            desc = (
                "You've seen the whole deck. Every command shown is "
                "live right now -- pick one and try it."
            )
        await interaction.response.edit_message(
            embed=card(
                title,
                description=desc,
                color=C_GOLD,
            )
            .image("attachment://start.png")
            .footer(
                "Tap Next to advance  -  Skip to dismiss"
                if nxt < len(DECK)
                else "Run ,help anytime to dig deeper"
            )
            .build(),
            attachments=[file],
            view=(self if nxt < len(DECK) else None),
        )

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, _btn) -> None:
        from services.onboarding_deck import skip
        await skip(self.bot.db, self.user_id)
        await interaction.response.edit_message(
            embed=card(
                "Discoin Tour  -  skipped",
                description=(
                    "No problem. Run `,tour` (or its aliases `,onboard` / "
                    "`,newplayer`) any time to resume from the first card."
                ),
                color=C_INFO,
            ).build(),
            view=None,
        )


class Onboarding(commands.Cog):
    """`,start` -- interactive onboarding deck."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # NOTE: ',start' is already claimed by cogs/overview.py (the unified
    # dashboard / launcher). The V3 onboarding deck uses ',tour' (with
    # 'onboard' / 'newplayer' aliases) so the two coexist without a
    # CommandRegistrationError at cog-load time.
    @commands.hybrid_command(
        name="tour", aliases=["onboard", "newplayer"],
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def tour(self, ctx: DiscoContext) -> None:
        """Walk through the Discoin onboarding deck."""
        cur = await _svc.get_progress(ctx.db, ctx.author.id)
        if cur >= len(_svc.DECK):
            cur = 0  # rerun -- start from the top
        png = _svc.render_card(cur)
        file = discord.File(io.BytesIO(png), filename="start.png")
        deck_card = _svc.DECK[cur]
        desc_lines = [f"**{deck_card['title']}**"]
        if deck_card.get("blurb"):
            desc_lines.append(deck_card["blurb"])
        embed = (
            card(
                f"Discoin Tour  -  card {cur + 1}/{len(_svc.DECK)}",
                color=C_GOLD,
            )
            .description("\n\n".join(desc_lines))
            .image("attachment://start.png")
            .footer("Tap Next to advance  -  Skip to dismiss")
            .build()
        )
        view = _DeckView(self.bot, ctx.author.id, current_idx=cur)
        await ctx.reply(embed=embed, file=file, view=view, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Onboarding(bot))
