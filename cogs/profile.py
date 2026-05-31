"""V3 Pillar 4: ``,profile`` cog -- equippable cosmetics + PNG profile card."""
from __future__ import annotations

import io
import logging

import discord
from discord.ext import commands

from core.config import Config
from configs.cosmetics_config import SLOTS, THEMES
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import C_GOLD, C_INFO
from services import cosmetics as _svc

log = logging.getLogger(__name__)


async def _build_shop_payload(
    db,
    user_id: int,
    guild_id: int,
    *,
    theme: str | None,
    page: int,
) -> tuple[discord.Embed, discord.File, int, int]:
    """Compute the shop embed + PNG for a given (theme, page) selection.

    Returns ``(embed, file, page, total_pages)`` so the view can keep
    its button-enabled state in sync after each interaction.
    """
    from core.framework.scale import to_human
    from services.profile_render import render_shop, shop_paginate

    listings = _svc.shop_listings(theme=theme)
    owned = set(await _svc.list_owned(db, user_id))
    wallet_usd = 0.0
    try:
        user = await db.get_user(user_id, guild_id)
        wallet_usd = to_human(int(user.get("wallet") or 0))
    except Exception:
        pass

    slice_, page, total_pages = shop_paginate(listings, page=int(page))
    png = render_shop(
        slice_, theme=theme, owned=owned, wallet_usd=wallet_usd,
        page=page, total_pages=total_pages,
    )
    file = discord.File(io.BytesIO(png), filename="shop.png")

    theme_label = (
        THEMES.get(theme, {}).get("label", theme.title())
        if theme else "All themes"
    )
    desc = (
        f"Wallet: **${wallet_usd:,.2f}**  -  "
        f"Theme: **{theme_label}**  -  "
        f"Page **{page}/{total_pages}** ({len(listings)} items)\n"
        f"Buy with `,profile buy <slot> <id>`. "
        f"Use the buttons to flip pages and the dropdown to switch themes."
    )
    embed = (
        card("Cosmetic Shop", color=C_GOLD)
        .description(desc)
        .image("attachment://shop.png")
        .build()
    )
    return embed, file, page, total_pages


class _ThemeSelect(discord.ui.Select):
    """Dropdown to jump between themes (or 'All') without retyping the command."""

    def __init__(self, current_theme: str | None) -> None:
        options: list[discord.SelectOption] = [
            discord.SelectOption(
                label="All themes",
                value="__all__",
                description="Show every cosmetic across every theme.",
                default=(current_theme is None),
            )
        ]
        for key, meta in THEMES.items():
            options.append(
                discord.SelectOption(
                    label=meta.get("label", key.title()),
                    value=key,
                    description=f"Show only the {key} theme.",
                    default=(current_theme == key),
                )
            )
        super().__init__(
            placeholder="Pick a theme",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _ShopView = self.view  # type: ignore[assignment]
        choice = self.values[0]
        view.theme = None if choice == "__all__" else choice
        view.page = 1
        await view.refresh(interaction)


class _ShopView(discord.ui.View):
    """Buttoned + dropdown navigator for `,profile shop`.

    Replaces the old "type `,profile shop X 2`" page-number flow.
    """

    def __init__(
        self,
        bot: Discoin,
        user_id: int,
        guild_id: int,
        *,
        theme: str | None,
        page: int,
        total_pages: int,
        timeout: float = 180.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user_id = user_id
        self.guild_id = guild_id
        self.theme = theme
        self.page = page
        self.total_pages = total_pages
        self.add_item(_ThemeSelect(theme))
        self._sync_button_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This shop view belongs to someone else. "
                "Run `,profile shop` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    def _sync_button_state(self) -> None:
        self.prev_btn.disabled = self.page <= 1
        self.next_btn.disabled = self.page >= self.total_pages
        self.page_label.label = f"Page {self.page} / {self.total_pages}"

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Rebuild the embed + PNG for the current (theme, page) and edit."""
        embed, file, page, total_pages = await _build_shop_payload(
            self.bot.db, self.user_id, self.guild_id,
            theme=self.theme, page=self.page,
        )
        self.page = page
        self.total_pages = total_pages
        self._sync_button_state()
        await interaction.response.edit_message(
            embed=embed, attachments=[file], view=self,
        )

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, _btn) -> None:
        self.page = max(1, self.page - 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Page 1 / 1", style=discord.ButtonStyle.primary, row=0, disabled=True)
    async def page_label(self, interaction: discord.Interaction, _btn) -> None:
        # Inert -- the label is its own state indicator.
        await interaction.response.defer()

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, _btn) -> None:
        self.page = min(self.total_pages, self.page + 1)
        await self.refresh(interaction)


class Profile(commands.Cog):
    """Player identity surface: title, banner, frame, sigil."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.hybrid_group(name="profile", invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def profile(
        self, ctx: DiscoContext, member: discord.Member | None = None,
    ) -> None:
        """Render your (or someone else's) Apex profile card."""
        target = member or ctx.author
        equipped = await _svc.equipped(ctx.db, target.id)
        try:
            from services.net_worth import compute_net_worth
            nw = await compute_net_worth(target.id, ctx.guild_id, ctx.db)
            nw_total = float(nw.total)
        except Exception:
            nw_total = 0.0
        # Mastery roll-up (best-effort; safe to skip if cog isn't loaded yet)
        mastery_summary = None
        try:
            from services import mastery as mastery_svc
            ms = await mastery_svc.mastery_summary(ctx.db, target.id, ctx.guild_id)
            mastery_summary = {
                "tracks": ms.tracks,
                "unlocked_count": len(ms.unlocked),
            }
        except Exception:
            pass
        # Avatar bytes (best-effort)
        avatar_bytes: bytes | None = None
        try:
            avatar_bytes = await target.display_avatar.read()
        except Exception:
            pass

        # Job + level for the subtitle line under the name. When the
        # player hasn't equipped a custom title, this is what shows up
        # (e.g. "DeFi Degen  -  Level 4") -- no more generic "Novice".
        _job_title: str | None = None
        _job_level: int | None = None
        try:
            from core.config import Config as _Cfg
            _job_row = await ctx.db.get_user_job(target.id, ctx.guild_id)
            if _job_row:
                _job_id = _job_row.get("job_id") or "HOMELESS"
                _job_cfg = _Cfg.JOBS.get(_job_id, _Cfg.JOBS.get("HOMELESS", {}))
                _job_title = str(_job_cfg.get("title") or _job_id.title())
                _job_level = int(_job_row.get("level") or 1)
        except Exception:
            pass

        # V3 polish flesh-out: more stats on the profile card.
        _chat_level: int | None = None
        _chat_rank: str | None = None
        _streak_days: int | None = None
        _achievements_unlocked: int | None = None
        _days_since_join: int | None = None
        _top_mastery: list[tuple[str, int]] = []
        try:
            from services.chat_leveling import (
                get_user as _cl_user, get_ranks as _cl_ranks,
                rank_for_level as _cl_rank_for,
            )
            _cl_row = await _cl_user(ctx.db, ctx.guild_id, target.id)
            if _cl_row:
                _chat_level = int(_cl_row.get("level") or 0)
                _streak_days = int(_cl_row.get("streak_days") or 0)
                try:
                    _ranks = await _cl_ranks(ctx.db, ctx.guild_id)
                    _chat_rank = _cl_rank_for(_chat_level, _ranks)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            row = await ctx.db.fetch_one(
                "SELECT COUNT(*)::int AS c FROM user_achievements "
                "WHERE user_id=$1",
                target.id,
            )
            if row:
                _achievements_unlocked = int(row.get("c") or 0)
        except Exception:
            pass
        try:
            row = await ctx.db.fetch_one(
                "SELECT EXTRACT(EPOCH FROM (NOW() - created_at)) AS s "
                "FROM users WHERE user_id=$1 AND guild_id=$2",
                target.id, ctx.guild_id,
            )
            if row and row.get("s") is not None:
                _days_since_join = int(float(row["s"]) // 86400)
        except Exception:
            pass
        if mastery_summary and mastery_summary.get("tracks"):
            tracks = mastery_summary["tracks"]
            ordered = sorted(
                tracks.items(),
                key=lambda kv: int(kv[1].get("level", 0)),
                reverse=True,
            )[:3]
            _top_mastery = [
                (str(name).title(), int(t.get("level", 0)))
                for name, t in ordered if int(t.get("level", 0)) > 0
            ]

        from services.profile_render import render_profile_card
        png = render_profile_card(
            user_name=target.display_name,
            avatar_bytes=avatar_bytes,
            equipped=equipped,
            net_worth_usd=nw_total,
            mastery_summary=mastery_summary,
            job_title=_job_title,
            job_level=_job_level,
            chat_level=_chat_level,
            chat_rank=_chat_rank,
            streak_days=_streak_days,
            achievements_unlocked=_achievements_unlocked,
            days_since_join=_days_since_join,
            top_mastery_tracks=_top_mastery,
        )
        file = discord.File(io.BytesIO(png), filename="profile.png")
        _eq_summary = "  -  ".join(
            f"**{slot}:** `{equipped.get(slot) or '(none)'}`"
            for slot in ("title", "banner", "frame", "sigil")
        )
        # Wealth Bottleneck preview: show the live multiplier so players
        # can see at a glance how their next gain will land. Cheap call
        # backed by the rank cache; failure is silent.
        bn_line = ""
        try:
            from services.bottleneck import (
                bottleneck_multiplier, lookup_percentile, percentile_label,
            )
            _pctile, _, _n = await lookup_percentile(
                ctx.db, uid=target.id, gid=ctx.guild_id,
            )
            _min = int(getattr(Config, "BOTTLENECK_MIN_HOLDERS", 5))
            if _n >= max(2, _min):
                _mult = bottleneck_multiplier(_pctile)
                bn_line = (
                    f"  -  Bottleneck: x{_mult:.2f} "
                    f"({percentile_label(_pctile)})"
                )
        except Exception:
            pass
        embed = (
            card("Profile", color=C_GOLD)
            .description(
                f"<@{target.id}>\n"
                f"{_eq_summary}\n"
                f"Customize with `,profile help`."
            )
            .image("attachment://profile.png")
            .footer(f"Net worth: ${nw_total:,.0f}{bn_line}")
            .build()
        )
        await ctx.reply(embed=embed, file=file, mention_author=False)

    @profile.command(name="equip")
    async def profile_equip(
        self, ctx: DiscoContext, slot: str, item_id: str,
    ) -> None:
        """Equip a cosmetic. Usage: `,profile equip <slot> <id>`"""
        ok, msg = await _svc.equip(ctx.db, ctx.author.id, slot, item_id)
        if not ok:
            await ctx.reply_error(msg)
            return
        await ctx.reply_success(msg, title="Cosmetic equipped")

    @profile.command(name="unequip")
    async def profile_unequip(self, ctx: DiscoContext, slot: str) -> None:
        """Unequip the cosmetic in a slot."""
        slot = slot.lower()
        if slot not in SLOTS:
            await ctx.reply_error(f"Unknown slot `{slot}`.")
            return
        ok = await _svc.unequip(ctx.db, ctx.author.id, slot)
        if not ok:
            await ctx.reply_error("Unequip failed.")
            return
        await ctx.reply_success(
            f"Cleared `{slot}`. Defaulting to the system baseline.",
            title="Cosmetic cleared",
        )

    @profile.command(name="gallery")
    async def profile_gallery(self, ctx: DiscoContext) -> None:
        """Render the gallery of cosmetics you own."""
        inv = await _svc.inventory(ctx.db, ctx.author.id)
        from services.profile_render import render_gallery
        png = render_gallery(inv, user_name=ctx.author.display_name)
        file = discord.File(io.BytesIO(png), filename="gallery.png")
        embed = (
            card("Cosmetic Gallery", color=C_INFO)
            .description("Use `,profile equip <slot> <id>` to wear an item.")
            .image("attachment://gallery.png")
            .build()
        )
        await ctx.reply(embed=embed, file=file, mention_author=False)

    @profile.command(name="shop", aliases=["store"])
    async def profile_shop(
        self, ctx: DiscoContext,
        theme: str | None = None,
    ) -> None:
        """Browse the cosmetic shop. Use the buttons + dropdown to navigate.

        Pass an optional starting theme: `,profile shop cats` opens the
        view filtered to the Cats theme. Without a theme it opens on
        "All themes". Page navigation, theme switching, and the wallet
        readout are all driven by the attached UI -- no need to retype
        the command to flip a page.
        """
        if theme is not None:
            theme = theme.lower().strip() or None
        if theme and theme not in THEMES:
            await ctx.reply_error(
                f"Unknown theme `{theme}`. Available: "
                f"{', '.join(sorted(THEMES.keys()))}."
            )
            return
        if not _svc.shop_listings(theme=theme):
            await ctx.reply_error("No items in that theme yet.")
            return
        embed, file, page, total_pages = await _build_shop_payload(
            ctx.db, ctx.author.id, ctx.guild_id,
            theme=theme, page=1,
        )
        view = _ShopView(
            self.bot, ctx.author.id, ctx.guild_id,
            theme=theme, page=page, total_pages=total_pages,
        )
        await ctx.reply(embed=embed, file=file, view=view, mention_author=False)

    @profile.command(name="buy", aliases=["purchase"])
    async def profile_buy(
        self, ctx: DiscoContext, slot: str, item_id: str,
    ) -> None:
        """Buy a shop-listed cosmetic with USD.

        Run `,profile shop` to browse first. Example: `,profile buy
        sigil cat`. Fund comes from wallet first, bank second.
        """
        ok, msg, price = await _svc.buy(
            ctx.db, ctx.author.id, ctx.guild_id, slot, item_id,
        )
        if not ok:
            await ctx.reply_error(msg)
            return
        await ctx.reply_success(msg, title="Cosmetic purchased")


    @profile.command(name="help", aliases=["commands", "?"])
    async def profile_help(self, ctx: DiscoContext) -> None:
        """Show every ,profile subcommand with a one-line example."""
        from configs.cosmetics_config import THEMES
        themes_line = ", ".join(sorted(THEMES.keys()))
        embed = (
            card("Profile -- Customization Guide", color=C_GOLD)
            .description(
                "Your profile card themes off four equipped cosmetic "
                "slots: **title**, **banner**, **frame**, **sigil**. "
                "What you equip here also themes your `,level`/`,rank` "
                "card and every payout receipt (`,daily`, `,work`, "
                "`,ape`, `,beg`)."
            )
            .field(
                "View",
                "`,profile` -- your card\n"
                "`,profile @user` -- someone else's",
                False,
            )
            .field(
                "Equip / unequip",
                "`,profile equip <slot> <id>` -- e.g. "
                "`,profile equip sigil cat`\n"
                "`,profile unequip <slot>` -- removes it\n"
                "Slots: `title`, `banner`, `frame`, `sigil`",
                False,
            )
            .field(
                "Browse",
                "`,profile gallery` -- everything you own\n"
                "`,profile shop` -- the full cosmetic shop (buttons + dropdown)\n"
                "`,profile shop <theme>` -- open the shop pre-filtered",
                False,
            )
            .field(
                "Buy",
                "`,profile buy <slot> <id>` -- spend USD on a "
                "shop-listed cosmetic (wallet first, bank fallback). "
                "After buying, equip it with `,profile equip`.",
                False,
            )
            .field(
                "Themes",
                themes_line,
                False,
            )
            .field(
                "Defaults",
                "New players start with a black **obsidian** banner, "
                "**simple** frame, **star** sigil, and **no title**. "
                "The subtitle on the profile card falls back to your "
                "job + level when no custom title is equipped.",
                False,
            )
            .footer("All four slots are global -- equipped once, used in every server.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Profile(bot))
