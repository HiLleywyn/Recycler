"""cogs/sage.py  -  Sage Network player surface.

Houses the three Sage games and the ``,sage`` command group:

    ,pattern                    start a Pattern Lab run
    ,gauge                      start an Indicator Gauge run
    ,tknom                      start a Tokenomics Card run
    ,sage info                  network reference card
    ,sage me                    your progression (XP, level, bests)
    ,sage stake <amt|all>       stake EDU to drip SAGE
    ,sage unstake <amt|all>     unwind an EDU stake (auto-claims yield)
    ,sage claim                 pay out accrued SAGE yield
    ,sage cashout <amt|all>     burn SAGE -> credit USD at oracle minus impact
    ,sage lb [pattern|gauge|tknom]   leaderboards per game

Each game uses the same SageGameView for the multi-choice flow:
attaches a Pillow PNG, posts the question embed, awaits a button click
within a per-game timer. Correct answer -> mint SAGE + EDU, advance to
the next round; wrong answer -> end the run.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import discord
from discord.ext import commands

import configs.sage_config as sc
from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_AMBER, C_ERROR, C_GOLD, C_INFO, C_PURPLE, C_SUCCESS, C_TEAL,
    fmt_token, fmt_usd,
)
from services import sage as sage_svc
from services import sage_render as sage_img

log = logging.getLogger(__name__)


_PATTERN_TIMER = int(Config.SAGE_TIMER_PATTERN_S)
_GAUGE_TIMER   = int(Config.SAGE_TIMER_GAUGE_S)
_TKNOM_TIMER   = int(Config.SAGE_TIMER_TKNOM_S)
_CYCLE_TIMER   = int(Config.SAGE_TIMER_CYCLE_S)


def _parse_amount_or_all(text: str) -> tuple[bool, float]:
    s = (text or "").strip().lower()
    if not s:
        raise ValueError("Pass a number or `all`.")
    if s in ("all", "max", "everything"):
        return True, 0.0
    s = s.replace(",", "").replace("_", "")
    try:
        amt = float(s)
    except ValueError as exc:
        raise ValueError(f"Invalid amount: `{text}`.") from exc
    if amt <= 0:
        raise ValueError("Amount must be positive.")
    return False, amt


# ============================================================================
# Game view
# ============================================================================

class SageGameView(discord.ui.View):
    """Multi-choice button view for one round of a Sage game.

    Stores per-round state (correct key, options, run accumulators).
    On a click, calls back into the cog to credit + render the next round
    or end the run. On timeout (no click), treated as wrong answer.
    """

    def __init__(
        self,
        cog: "SageCog",
        ctx: DiscoContext,
        game: str,
        question_key: str,
        options: list[tuple[str, str]],  # (option_key, button_label)
        correct_key: str,
        round_index: int,
        timer_s: int,
        run_state: dict,
    ) -> None:
        super().__init__(timeout=timer_s)
        self.cog = cog
        self.ctx = ctx
        self.game = game
        self.question_key = question_key
        self.correct_key = correct_key
        self.round_index = round_index
        self.timer_s = timer_s
        self.run_state = run_state
        self._answered = False
        for opt_key, label in options:
            self.add_item(SageOptionButton(opt_key, label))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the player who started the run can answer.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self._answered:
            return
        self._answered = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        # Treat timeout as wrong.
        try:
            await self.cog._on_answer(self.ctx, self, picked_key=None)
        except Exception:
            log.exception("sage view timeout handler failed")


class SageOptionButton(discord.ui.Button):
    def __init__(self, option_key: str, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.option_key = option_key

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SageGameView = self.view  # type: ignore[assignment]
        if view._answered:
            await interaction.response.defer()
            return
        view._answered = True
        for item in view.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
                if item.option_key == view.correct_key:  # type: ignore[attr-defined]
                    item.style = discord.ButtonStyle.success
                elif item is self:
                    item.style = discord.ButtonStyle.danger
        try:
            await interaction.response.edit_message(view=view)
        except Exception:
            log.debug("sage: edit_message after answer failed", exc_info=True)
        await view.cog._on_answer(view.ctx, view, picked_key=self.option_key)


# ============================================================================
# Cog
# ============================================================================

class SageCog(commands.Cog):
    """Sage Network: three crypto learn-and-earn games + EDU/SAGE economy."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # ── ,sage root group ────────────────────────────────────────────────

    @commands.group(
        name="sage", aliases=["sg", "sagenet"], invoke_without_command=True,
    )
    @guild_only
    @no_bots
    @ensure_registered
    async def sage(self, ctx: DiscoContext) -> None:
        """Sage Network hub. ``,sage info`` for the reference card."""
        await self._info(ctx)

    @sage.command(name="info")
    @guild_only
    @no_bots
    @ensure_registered
    async def sage_info(self, ctx: DiscoContext) -> None:
        await self._info(ctx)

    async def _info(self, ctx: DiscoContext) -> None:
        p = ctx.prefix or Config.PREFIX
        emb = (
            card(
                "\U0001F4DA Sage Network",
                description=(
                    "Crypto learn-and-earn. Three timed quiz games mint **EDU** "
                    "(game token) and **SAGE** (network coin) on every correct "
                    "answer, with rewards scaling as your run goes on. One "
                    "wrong answer ends the run."
                ),
                color=C_GOLD,
            )
            .field(
                "\U0001F4C8 Pattern Lab",
                (
                    f"`{p}pattern` -- identify the chart pattern. {_PATTERN_TIMER}s per round.\n"
                    f"Round 5+ may splice two patterns into one chart -- pick "
                    f"both halves for a **{int(sc.COMPOUND_REWARD_MULT*100-100)}% reward bonus**."
                ),
                False,
            )
            .field(
                "\U0001F4CA Indicator Gauge",
                f"`{p}gauge` -- bear / neutral / bull on an indicator card. {_GAUGE_TIMER}s per round.",
                False,
            )
            .field(
                "\U0001F9EE Tokenomics Card",
                f"`{p}tknom` -- classify a synthetic token's supply curve. {_TKNOM_TIMER}s per round.",
                False,
            )
            .field(
                "\U0001F300 Cycle Phase",
                f"`{p}cycle` -- accumulation / markup / distribution / markdown. {_CYCLE_TIMER}s per round.",
                False,
            )
            .field(
                "\U0001F4B0 Earn / Stake / Cash Out",
                (
                    f"Each correct answer mints {int(Config.SAGE_COIN_SHARE*100)}% **SAGE** + "
                    f"{int(Config.SAGE_TOKEN_SHARE*100)}% **EDU** of the round reward.\n"
                    f"`{p}sage stake <amt>` -- stake EDU to passively drip SAGE.\n"
                    f"`{p}sage claim` -- pay out accrued SAGE yield.\n"
                    f"`{p}sage cashout <amt>` -- burn SAGE -> USD (oracle minus impact)."
                ),
                False,
            )
            .field(
                "\U0001F6D2 Sage Shop",
                (
                    f"`{p}sage shop` -- spend SAGE on one-run consumables "
                    f"(extra time, 50/50, 2x XP, survive a wrong answer).\n"
                    f"`{p}sage buy <item>` -- purchase."
                ),
                False,
            )
            .field(
                "\U0001F3C6 Leaderboards",
                (
                    f"`{p}sage lb pattern|gauge|tknom|cycle` -- best-run leaderboards.\n"
                    f"`{p}sage lb level` -- top Sage levels by XP."
                ),
                False,
            )
            .footer(
                "Disco refuses to give answers while you're mid-run. "
                "Use your eyes."
            )
            .build()
        )
        await ctx.reply(embed=emb, mention_author=False)

    # ── ,sage me / progress ─────────────────────────────────────────────

    @sage.command(name="me", aliases=["progress", "stats"])
    @guild_only
    @no_bots
    @ensure_registered
    async def sage_me(self, ctx: DiscoContext) -> None:
        prog = await sage_svc.get_progress(ctx.db, ctx.guild_id, ctx.author.id)
        owned = await sage_svc.get_items(ctx.db, ctx.guild_id, ctx.author.id)
        owned_str = "  ·  ".join(
            f"{sc.SAGE_SHOP_ITEMS[k]['emoji']} {sc.SAGE_SHOP_ITEMS[k]['name']} x{v}"
            for k, v in owned.items() if k in sc.SAGE_SHOP_ITEMS
        )
        cur_xp_in_lvl, xp_to_next = sc.sage_xp_progress(prog.sage_xp)
        pct = cur_xp_in_lvl / max(1, xp_to_next)
        bar_len = 16
        filled = int(bar_len * pct)
        bar = "█" * filled + "░" * (bar_len - filled)
        emb = (
            card(
                f"\U0001F393 {ctx.author.display_name} · Sage Profile",
                color=C_PURPLE,
            )
            .field("Sage Level", f"**Lv {prog.sage_level}**", True)
            .field("XP", f"`{cur_xp_in_lvl:,}/{xp_to_next:,}`\n`{bar}`", True)
            .field("Total Correct", f"{prog.lifetime_correct:,}", True)
            .field("Runs", f"{prog.lifetime_runs:,}", True)
            .field(
                "Pattern best",
                f"{prog.best_pattern_streak} \U0001F4C8",
                True,
            )
            .field(
                "Gauge best",
                f"{prog.best_gauge_streak} \U0001F4CA",
                True,
            )
            .field(
                "Tknom best",
                f"{prog.best_tknom_streak} \U0001F9EE",
                True,
            )
            .field(
                "Cycle best",
                f"{prog.best_cycle_streak} \U0001F300",
                True,
            )
            .field(
                "Lifetime SAGE earned",
                fmt_token(to_human(prog.total_sage_earned_raw), "SAGE"),
                True,
            )
            .field(
                "Lifetime EDU earned",
                fmt_token(to_human(prog.total_edu_earned_raw), "EDU"),
                True,
            )
            .field_if(
                bool(owned_str),
                "\U0001F392 Consumables",
                owned_str or "None",
                False,
            )
            .build()
        )
        await ctx.reply(embed=emb, mention_author=False)

    # ── leaderboards ────────────────────────────────────────────────────

    @sage.command(name="lb", aliases=["leaderboard", "top"])
    @guild_only
    @no_bots
    @ensure_registered
    async def sage_lb(
        self, ctx: DiscoContext, game: Optional[str] = None,
    ) -> None:
        valid = set(sc.GAMES) | {"level", "levels", "lvl"}
        if game and game.lower() not in valid:
            await ctx.reply_error(
                f"Unknown leaderboard: `{game}`. Pick one of "
                f"{', '.join(sc.GAMES)}, or `level`."
            )
            return

        # Level leaderboard branch.
        if game and game.lower() in ("level", "levels", "lvl"):
            rows = await sage_svc.top_levels(ctx.db, ctx.guild_id, limit=10)
            lines = []
            for i, row in enumerate(rows, 1):
                medal = ("\U0001F947", "\U0001F948", "\U0001F949")[i - 1] if i <= 3 else f"{i}."
                uid = int(row.get("user_id") or 0)
                lvl = int(row.get("sage_level") or 1)
                xp = int(row.get("sage_xp") or 0)
                lines.append(
                    f"{medal} <@{uid}>  ·  Lv **{lvl}**  ·  `{xp:,}` XP"
                )
            if not lines:
                lines.append("No Sage scholars yet -- run a game to register.")
            emb = (
                card(
                    "\U0001F393 Sage Levels · Leaderboard",
                    description="\n".join(lines),
                    color=C_PURPLE,
                )
                .footer(f"{ctx.prefix or Config.PREFIX}sage lb level")
                .build()
            )
            await ctx.reply(embed=emb, mention_author=False)
            return

        games = [game.lower()] if game else list(sc.GAMES)
        embeds: list[discord.Embed] = []
        for g in games:
            rows = await sage_svc.top_runs(ctx.db, ctx.guild_id, g, limit=10)
            lines = []
            for i, row in enumerate(rows, 1):
                medal = ("\U0001F947", "\U0001F948", "\U0001F949")[i - 1] if i <= 3 else f"{i}."
                uid = int(row.get("user_id") or 0)
                best = int(row.get("best") or 0)
                lines.append(f"{medal} <@{uid}>  ·  **{best}** correct")
            if not lines:
                lines.append("No runs recorded yet -- be the first.")
            emb = (
                card(
                    f"{sc.GAME_EMOJIS.get(g, '')} {sc.GAME_TITLES.get(g, g.title())} · Leaderboard",
                    description="\n".join(lines),
                    color=C_GOLD,
                )
                .footer(f"{ctx.prefix or Config.PREFIX}sage lb {g}")
                .build()
            )
            embeds.append(emb)
        if len(embeds) == 1:
            await ctx.reply(embed=embeds[0], mention_author=False)
        else:
            await ctx.paginate(embeds)

    # ── stake / unstake / claim / cashout ──────────────────────────────

    @sage.command(name="stake", aliases=["stakes", "stakeinfo"])
    @guild_only
    @no_bots
    @ensure_registered
    async def sage_stake(
        self, ctx: DiscoContext, amount: Optional[str] = None,
    ) -> None:
        # No-arg form (also fires for the `stakes` / `stakeinfo` aliases):
        # show the user's current stake position, pending yield, and APY.
        if not amount:
            await self._show_stake_status(ctx)
            return
        try:
            is_all, amt_h = _parse_amount_or_all(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if is_all:
            held = await sage_svc.get_edu_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if held <= 0:
                await ctx.reply_error("You have no **EDU** to stake.")
                return
            req_raw = int(held)
        else:
            req_raw = int(to_raw(amt_h))
        try:
            res = await sage_svc.stake(
                ctx.db, ctx.guild_id, ctx.author.id, req_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        apy = sage_svc.effective_apy_pct()
        emb = (
            card("\U0001F510 EDU Staked", color=C_SUCCESS)
            .field("Staked", fmt_token(to_human(req_raw), "EDU"), True)
            .field("Position", fmt_token(to_human(res.staked_raw), "EDU"), True)
            .field("Drip rate", f"~`{apy:,.1f}%` APY (parity)", True)
            .footer(
                f"{ctx.prefix or Config.PREFIX}sage claim to pay out pending SAGE."
            )
            .build()
        )
        await ctx.reply(embed=emb, mention_author=False)

    async def _show_stake_status(self, ctx: DiscoContext) -> None:
        """Display the user's current EDU stake + accrued SAGE yield."""
        snap = await sage_svc.get_stake(ctx.db, ctx.guild_id, ctx.author.id)
        pending_total = await sage_svc.accrued_yield(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        wallet_edu = await sage_svc.get_edu_wallet_raw(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        apy = sage_svc.effective_apy_pct()
        p = ctx.prefix or Config.PREFIX
        emb = (
            card(
                "\U0001F510 EDU Stake",
                description=(
                    "Stake EDU to passively drip SAGE. Yield accrues by the "
                    "second; claim or unstake at any time to crystallise it."
                ),
                color=C_INFO,
            )
            .field("Staked", fmt_token(to_human(snap.staked_raw), "EDU"), True)
            .field("Wallet EDU", fmt_token(to_human(wallet_edu), "EDU"), True)
            .field("Drip rate", f"~`{apy:,.1f}%` APY (parity)", True)
            .field(
                "Pending yield",
                fmt_token(to_human(int(pending_total)), "SAGE"),
                True,
            )
            .field(
                "Lifetime claimed",
                fmt_token(to_human(snap.total_claimed_raw), "SAGE"),
                True,
            )
            .blank(True)
            .footer(
                f"{p}sage stake <amt|all>  ·  {p}sage claim  ·  {p}sage unstake <amt|all>"
            )
            .build()
        )
        await ctx.reply(embed=emb, mention_author=False)

    @sage.command(name="unstake", aliases=["withdraw"])
    @guild_only
    @no_bots
    @ensure_registered
    async def sage_unstake(
        self, ctx: DiscoContext, amount: Optional[str] = None,
    ) -> None:
        if not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}sage unstake <amt|all>`"
            )
            return
        try:
            is_all, amt_h = _parse_amount_or_all(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if is_all:
            snap = await sage_svc.get_stake(ctx.db, ctx.guild_id, ctx.author.id)
            req_raw = int(snap.staked_raw)
            if req_raw <= 0:
                await ctx.reply_error("You have no **EDU** staked.")
                return
        else:
            req_raw = int(to_raw(amt_h))
        try:
            res = await sage_svc.unstake(
                ctx.db, ctx.guild_id, ctx.author.id, req_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        emb = (
            card("\U0001F513 EDU Unstaked", color=C_SUCCESS)
            .field("Unstaked", fmt_token(to_human(req_raw), "EDU"), True)
            .field("Position", fmt_token(to_human(res.staked_raw), "EDU"), True)
            .field_if(
                int(res.yield_paid_raw) > 0,
                "Yield paid",
                fmt_token(to_human(int(res.yield_paid_raw)), "SAGE"),
                True,
            )
            .build()
        )
        await ctx.reply(embed=emb, mention_author=False)

    @sage.command(name="claim", aliases=["harvest"])
    @guild_only
    @no_bots
    @ensure_registered
    async def sage_claim(self, ctx: DiscoContext) -> None:
        try:
            paid = await sage_svc.claim(ctx.db, ctx.guild_id, ctx.author.id)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        emb = (
            card("\U0001F4B0 SAGE Yield Claimed", color=C_SUCCESS)
            .field("Paid", fmt_token(to_human(paid), "SAGE"), False)
            .footer(
                f"{ctx.prefix or Config.PREFIX}sage cashout <amt> to burn SAGE -> USD."
            )
            .build()
        )
        await ctx.reply(embed=emb, mention_author=False)

    @sage.command(name="cashout")
    @guild_only
    @no_bots
    @ensure_registered
    async def sage_cashout(
        self, ctx: DiscoContext, amount: Optional[str] = None,
    ) -> None:
        if not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}sage cashout <amt|all>`"
            )
            return
        try:
            is_all, amt_h = _parse_amount_or_all(amount)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        if is_all:
            held = await sage_svc.get_sage_wallet_raw(
                ctx.db, ctx.guild_id, ctx.author.id,
            )
            if held <= 0:
                await ctx.reply_error("You have no **SAGE** to cash out.")
                return
            req_raw = int(held)
        else:
            req_raw = int(to_raw(amt_h))
        try:
            res = await sage_svc.cashout_sage(
                ctx.db, ctx.guild_id, ctx.author.id, req_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        emb = (
            card("\U0001F525 SAGE -> USD Cashout", color=C_GOLD)
            .field("Burned", fmt_token(to_human(res.sage_burned_raw), "SAGE"), True)
            .field("Credited", fmt_usd(to_human(res.usd_credited_raw)), True)
            .field("Oracle", f"`{res.sage_oracle_before:.4f}` -> `{res.sage_oracle_after:.4f}`", True)
            .field("Price impact", f"`{res.price_impact_pct*100:.2f}%`", True)
            .build()
        )
        await ctx.reply(embed=emb, mention_author=False)

    # ── shop ────────────────────────────────────────────────────────────

    @sage.command(name="shop", aliases=["store", "items"])
    @guild_only
    @no_bots
    @ensure_registered
    async def sage_shop(self, ctx: DiscoContext) -> None:
        """Browse the Sage Shop -- SAGE-priced one-run consumables."""
        owned = await sage_svc.get_items(ctx.db, ctx.guild_id, ctx.author.id)
        held = await sage_svc.get_sage_wallet_raw(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        p = ctx.prefix or Config.PREFIX
        emb = card(
            "\U0001F6D2 Sage Shop",
            description=(
                "One-run consumables for the Sage games, priced in **SAGE**. "
                f"Buy with `{p}sage buy <item> [qty]`.\n"
                f"Balance: **{fmt_token(to_human(held), 'SAGE')}**"
            ),
            color=C_GOLD,
        )
        for key in sc.SAGE_SHOP_ORDER:
            item = sc.SAGE_SHOP_ITEMS[key]
            have = owned.get(key, 0)
            emb = emb.field(
                f"{item['emoji']} {item['name']}  ·  {item['price_sage']:g} SAGE",
                (
                    f"{item['blurb']}\n"
                    f"Owned: **{have}**  ·  `{p}sage buy {key}`"
                ),
                False,
            )
        emb = emb.footer(
            "Time Crystal / Insight Lens / Scholar's Draft apply at run "
            "start. Second Wind is spent only if it saves a wrong answer."
        )
        await ctx.reply(embed=emb.build(), mention_author=False)

    @sage.command(name="buy", aliases=["purchase"])
    @guild_only
    @no_bots
    @ensure_registered
    async def sage_buy(
        self, ctx: DiscoContext,
        item: Optional[str] = None, qty: Optional[str] = "1",
    ) -> None:
        """Buy a Sage Shop consumable with SAGE."""
        if not item:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}sage buy <item> [qty]`. "
                f"See `{ctx.prefix}sage shop`."
            )
            return
        key = sc.resolve_shop_item(item)
        if key is None:
            await ctx.reply_error(
                f"Unknown item `{item}`. See `{ctx.prefix}sage shop` "
                f"for the catalogue."
            )
            return
        try:
            n = int(str(qty or "1").replace(",", "").strip())
        except ValueError:
            await ctx.reply_error("Quantity must be a whole number.")
            return
        if n <= 0:
            await ctx.reply_error("Quantity must be positive.")
            return
        try:
            cost = await sage_svc.buy_item(
                ctx.db, ctx.guild_id, ctx.author.id, key, n,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        meta = sc.SAGE_SHOP_ITEMS[key]
        owned = await sage_svc.get_items(ctx.db, ctx.guild_id, ctx.author.id)
        emb = (
            card(f"{meta['emoji']} Purchased", color=C_SUCCESS)
            .field("Item", f"{meta['name']} x{n}", True)
            .field("Cost", fmt_token(cost, "SAGE"), True)
            .field("Now owned", f"{owned.get(key, 0)}", True)
            .footer(
                f"{ctx.prefix or Config.PREFIX}pattern / gauge / tknom / "
                f"cycle -- it applies on your next run."
            )
            .build()
        )
        await ctx.reply(embed=emb, mention_author=False)

    # ── game commands (also exposed at top level for convenience) ───────

    @commands.command(name="pattern", aliases=["chartpattern", "patternlab"])
    @guild_only
    @no_bots
    @ensure_registered
    async def cmd_pattern(self, ctx: DiscoContext) -> None:
        """Start a Pattern Lab run -- identify named chart patterns."""
        await self._start_run(ctx, sc.GAME_PATTERN)

    @commands.command(name="gauge", aliases=["indicator", "indicatorgauge"])
    @guild_only
    @no_bots
    @ensure_registered
    async def cmd_gauge(self, ctx: DiscoContext) -> None:
        """Start an Indicator Gauge run -- bear / neutral / bull readings."""
        await self._start_run(ctx, sc.GAME_GAUGE)

    @commands.command(name="tknom", aliases=["tokenomics", "tokenomicscard"])
    @guild_only
    @no_bots
    @ensure_registered
    async def cmd_tknom(self, ctx: DiscoContext) -> None:
        """Start a Tokenomics Card run -- classify supply curves."""
        await self._start_run(ctx, sc.GAME_TKNOM)

    @commands.command(name="cycle", aliases=["marketcycle", "phase", "cyclephase"])
    @guild_only
    @no_bots
    @ensure_registered
    async def cmd_cycle(self, ctx: DiscoContext) -> None:
        """Start a Cycle Phase run -- classify the market cycle stage."""
        await self._start_run(ctx, sc.GAME_CYCLE)

    # ── shared run flow ─────────────────────────────────────────────────

    async def _start_run(self, ctx: DiscoContext, game: str) -> None:
        if await sage_svc.has_active(ctx.db, ctx.guild_id, ctx.author.id):
            await ctx.reply_error(
                "You already have a Sage run in progress. Finish it first."
            )
            return
        await sage_svc.start_session(ctx.db, ctx.guild_id, ctx.author.id, game)
        # Spend the run-start consumables (Time Crystal / Insight Lens /
        # Scholar's Draft). Second Wind is left in inventory and only spent
        # if it actually saves a wrong answer later in the run.
        perks = await sage_svc.apply_run_perks(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        items = await sage_svc.get_items(ctx.db, ctx.guild_id, ctx.author.id)
        perks["second_wind_ready"] = items.get("second_wind", 0) > 0
        run_state = {
            "score":             0,
            "sage_raw_total":    0,
            "edu_raw_total":     0,
            "asked_keys":        set(),
            "compound":          None,
            "perks":             perks,
            "second_wind_used":  False,
            "perk_note":         self._format_perk_note(perks),
        }
        await self._render_round(ctx, game, run_state, round_index=1)

    @staticmethod
    def _format_perk_note(perks: dict) -> str:
        """One-line summary of the consumables active for this run."""
        bits: list[str] = []
        for key in perks.get("consumed", []):
            meta = sc.SAGE_SHOP_ITEMS.get(key, {})
            bits.append(f"{meta.get('emoji', '')} {meta.get('name', key)}")
        if perks.get("second_wind_ready"):
            meta = sc.SAGE_SHOP_ITEMS["second_wind"]
            bits.append(f"{meta['emoji']} {meta['name']} (armed)")
        if not bits:
            return ""
        return "\U0001F381 Consumables active: " + "  ·  ".join(bits)

    async def _consume_second_wind(
        self, ctx: DiscoContext, view: "SageGameView",
    ) -> bool:
        """Spend a Second Wind to forgive a wrong answer. Once per run.

        Returns True only when an item was actually decremented, so a
        player with no Second Wind (or who already used theirs) falls
        through to the normal run-over path.
        """
        rs = view.run_state
        perks = rs.get("perks", {})
        if not perks.get("second_wind_ready") or rs.get("second_wind_used"):
            return False
        ok = await sage_svc.consume_item(
            ctx.db, ctx.guild_id, ctx.author.id, "second_wind", 1,
        )
        if ok:
            rs["second_wind_used"] = True
        return ok

    async def _send_second_wind_embed(
        self, ctx: DiscoContext, view: "SageGameView", *, body: str,
    ) -> None:
        """Post the 'Second Wind saved you' embed and keep the run alive."""
        meta = sc.SAGE_SHOP_ITEMS["second_wind"]
        emb = (
            card(
                f"{meta['emoji']} Second Wind · Round {view.round_index}",
                description=body,
                color=C_INFO,
            )
            .field(
                "Run total",
                (
                    f"{view.run_state['score']} correct · "
                    f"{fmt_token(to_human(view.run_state['sage_raw_total']), 'SAGE')} · "
                    f"{fmt_token(to_human(view.run_state['edu_raw_total']), 'EDU')}"
                ),
                False,
            )
            .build()
        )
        await ctx.send(embed=emb)

    @staticmethod
    def _trim_options(
        options: list[tuple[str, str]], correct_key: str, fewer: bool,
    ) -> list[tuple[str, str]]:
        """Drop one wrong option when the Insight Lens perk is active.

        Never trims below two options, and never drops the correct one.
        """
        if not fewer or len(options) <= 2:
            return options
        wrong_idx = [i for i, o in enumerate(options) if o[0] != correct_key]
        if not wrong_idx:
            return options
        drop = random.choice(wrong_idx)
        return [o for i, o in enumerate(options) if i != drop]

    def _build_question(
        self, game: str, round_index: int, run_state: dict,
    ) -> tuple[str, str, list[tuple[str, str]], int, str]:
        """Return (question_key, correct_key, options, timer_s, explanation_seed).

        Picks a fresh question (not yet asked in this run), shuffles
        options including the correct answer + difficulty-scaled distractors.
        An active Insight Lens perk trims one wrong option from every round.
        """
        rng = random.Random()
        fewer = bool(run_state.get("perks", {}).get("fewer_options", False))
        if game == sc.GAME_PATTERN:
            pool = [p for p in sc.PATTERNS if p["key"] not in run_state["asked_keys"]]
            if not pool:
                pool = list(sc.PATTERNS)
                run_state["asked_keys"] = set()
            q = rng.choice(pool)
            run_state["asked_keys"].add(q["key"])
            # Distractor selection: easier early rounds prefer different-bias
            # picks (less visually similar); later rounds pull from same-bias.
            candidates = list(sc.PATTERNS)
            distractor_pool = [
                p for p in candidates if p["key"] != q["key"]
            ]
            preferred = [p for p in distractor_pool if p["key"] in q["distractors"]]
            others = [p for p in distractor_pool if p["key"] not in q["distractors"]]
            if round_index <= 3:
                pick = others + preferred
            else:
                pick = preferred + others
            distractors = pick[:max(0, sc.PATTERN_OPTION_COUNT - 1)]
            opts = [q] + distractors
            rng.shuffle(opts)
            options = [(p["key"], p["name"]) for p in opts]
            options = self._trim_options(options, q["key"], fewer)
            return q["key"], q["key"], options, _PATTERN_TIMER, ""
        if game == sc.GAME_GAUGE:
            pool = [i for i in sc.INDICATORS if i["key"] not in run_state["asked_keys"]]
            if not pool:
                pool = list(sc.INDICATORS)
                run_state["asked_keys"] = set()
            q = rng.choice(pool)
            run_state["asked_keys"].add(q["key"])
            options = [
                (k, f"{sc.GAUGE_OPTION_EMOJI[k]} {sc.GAUGE_OPTION_LABELS[k]}")
                for k in sc.GAUGE_OPTIONS
            ]
            options = self._trim_options(options, q["answer"], fewer)
            return q["key"], q["answer"], options, _GAUGE_TIMER, ""
        if game == sc.GAME_TKNOM:
            pool = [t for t in sc.TOKENOMICS if t["key"] not in run_state["asked_keys"]]
            if not pool:
                pool = list(sc.TOKENOMICS)
                run_state["asked_keys"] = set()
            q = rng.choice(pool)
            run_state["asked_keys"].add(q["key"])
            options = [
                (k, f"{sc.TKNOM_OPTION_EMOJI[k]} {sc.TKNOM_OPTION_LABELS[k]}")
                for k in sc.TKNOM_OPTIONS
            ]
            options = self._trim_options(options, q["answer"], fewer)
            return q["key"], q["answer"], options, _TKNOM_TIMER, ""
        if game == sc.GAME_CYCLE:
            pool = [c for c in sc.CYCLE_PHASES if c["key"] not in run_state["asked_keys"]]
            if not pool:
                pool = list(sc.CYCLE_PHASES)
                run_state["asked_keys"] = set()
            q = rng.choice(pool)
            run_state["asked_keys"].add(q["key"])
            options = [
                (k, f"{sc.CYCLE_OPTION_EMOJI[k]} {sc.CYCLE_OPTION_LABELS[k]}")
                for k in sc.CYCLE_OPTIONS
            ]
            options = self._trim_options(options, q["answer"], fewer)
            return q["key"], q["answer"], options, _CYCLE_TIMER, ""
        raise ValueError(f"Unknown sage game: {game}")

    def _build_compound_question(
        self, round_index: int, run_state: dict,
    ) -> Optional[dict]:
        """Pick a compound pattern entry whose shape keys haven't been asked.

        Returns None when no eligible compound exists (so the caller falls
        back to a single-pattern round). The chosen compound's shape keys
        are immediately marked asked so successive rounds rotate cleanly.
        """
        rng = random.Random()
        asked = run_state.get("asked_keys", set())
        candidates = [
            c for c in sc.COMPOUND_PATTERNS
            if all(s["shape"] not in asked for s in c["stages"])
        ]
        if not candidates:
            return None
        c = rng.choice(candidates)
        for s in c["stages"]:
            run_state.setdefault("asked_keys", set()).add(s["shape"])
        return c

    def _compound_stage_options(
        self, correct_shape: str, fewer: bool = False,
    ) -> list[tuple[str, str]]:
        """Return a shuffled multi-choice list for a single compound stage.

        Distractors lean same-bias (harder discrimination) since compounds
        only trigger at round 5+. Mirrors the late-round Pattern Lab logic.
        An active Insight Lens perk trims one wrong option.
        """
        rng = random.Random()
        q = sc.PATTERN_BY_KEY.get(correct_shape)
        if q is None:
            q = sc.PATTERNS[0]
        candidates = [p for p in sc.PATTERNS if p["key"] != q["key"]]
        preferred = [p for p in candidates if p["key"] in q["distractors"]]
        others = [p for p in candidates if p["key"] not in q["distractors"]]
        distractors = (preferred + others)[:max(0, sc.PATTERN_OPTION_COUNT - 1)]
        opts = [q] + distractors
        rng.shuffle(opts)
        options = [(p["key"], p["name"]) for p in opts]
        return self._trim_options(options, q["key"], fewer)

    async def _render_round(
        self, ctx: DiscoContext, game: str,
        run_state: dict, round_index: int,
    ) -> None:
        # Compound Pattern-Lab round: spliced chart, two sequential stages.
        # Roll for compound only on Pattern Lab; if the roll fires but no
        # eligible compound exists, fall back to a single-pattern round.
        if game == sc.GAME_PATTERN and not run_state.get("compound"):
            chance = sc.compound_chance_for_round(round_index)
            if chance > 0.0 and random.random() < chance:
                compound = self._build_compound_question(round_index, run_state)
                if compound is not None:
                    await self._render_compound_stage(
                        ctx, run_state, round_index, compound, stage=1,
                    )
                    return

        try:
            qk, correct, options, timer_s, _ = self._build_question(
                game, round_index, run_state,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        # Time Crystal perk extends every round timer for the run.
        timer_s += int(run_state.get("perks", {}).get("bonus_time", 0))

        seed = random.randint(1, 1_000_000)
        if game == sc.GAME_PATTERN:
            file = sage_img.pattern_file(qk, seed)
            attachment = file.filename
            reward_round = sage_svc.round_reward_usd(round_index)
            emb = (
                card(
                    f"\U0001F4C8 Pattern Lab · Round {round_index}",
                    description=(
                        f"Identify the pattern on the chart.\n\n"
                        f"\U0001F4B8 Round reward: **{fmt_usd(reward_round)}** "
                        f"(split {int(Config.SAGE_COIN_SHARE*100)}/{int(Config.SAGE_TOKEN_SHARE*100)} SAGE/EDU)"
                    ),
                    color=C_GOLD,
                )
                .footer(f"You have {timer_s}s. One wrong answer ends the run.")
                .build()
            )
            emb.set_image(url=f"attachment://{attachment}")
        elif game == sc.GAME_GAUGE:
            file = sage_img.gauge_file(qk, seed)
            attachment = file.filename
            reward_round = sage_svc.round_reward_usd(round_index)
            emb = (
                card(
                    f"\U0001F4CA Indicator Gauge · Round {round_index}",
                    description=(
                        f"Read the indicator. Is the signal **Bearish**, "
                        f"**Neutral**, or **Bullish**?\n\n"
                        f"\U0001F4B8 Round reward: **{fmt_usd(reward_round)}**"
                    ),
                    color=C_INFO,
                )
                .footer(f"You have {timer_s}s. One wrong answer ends the run.")
                .build()
            )
            emb.set_image(url=f"attachment://{attachment}")
        elif game == sc.GAME_TKNOM:
            file = sage_img.tknom_file(qk, seed)
            attachment = file.filename
            reward_round = sage_svc.round_reward_usd(round_index)
            emb = (
                card(
                    f"\U0001F9EE Tokenomics Card · Round {round_index}",
                    description=(
                        f"Classify the token's supply curve.\n\n"
                        f"\U0001F4B8 Round reward: **{fmt_usd(reward_round)}**"
                    ),
                    color=C_AMBER,
                )
                .footer(f"You have {timer_s}s. One wrong answer ends the run.")
                .build()
            )
            emb.set_image(url=f"attachment://{attachment}")
        else:  # cycle
            file = sage_img.cycle_file(qk, seed)
            attachment = file.filename
            reward_round = sage_svc.round_reward_usd(round_index)
            emb = (
                card(
                    f"\U0001F300 Cycle Phase · Round {round_index}",
                    description=(
                        f"Classify the market cycle phase from the snapshot.\n\n"
                        f"\U0001F4B8 Round reward: **{fmt_usd(reward_round)}**"
                    ),
                    color=C_TEAL,
                )
                .footer(f"You have {timer_s}s. One wrong answer ends the run.")
                .build()
            )
            emb.set_image(url=f"attachment://{attachment}")

        # Round 1 surfaces the run's active consumables in the description.
        if round_index == 1 and run_state.get("perk_note"):
            emb.description = (
                (emb.description or "") + "\n\n" + run_state["perk_note"]
            )

        view = SageGameView(
            self, ctx, game,
            question_key=qk,
            options=options,
            correct_key=correct,
            round_index=round_index,
            timer_s=timer_s,
            run_state=run_state,
        )
        # Round 1 replies to the user's command for acknowledgement; rounds
        # 2+ send to the channel directly. Guilds with cmd_delete_after set
        # auto-delete the original ,pattern message mid-run, which makes
        # ctx.reply fall back to ctx.send -- and the discord.File whose
        # BytesIO was consumed by the failed reply uploads zero bytes on
        # the retry, leaving the embed pointing at an empty attachment.
        if round_index == 1:
            await ctx.reply(embed=emb, file=file, view=view, mention_author=False)
        else:
            await ctx.send(embed=emb, file=file, view=view)

    async def _render_compound_stage(
        self, ctx: DiscoContext, run_state: dict, round_index: int,
        compound: dict, stage: int,
    ) -> None:
        """Render one stage (1 or 2) of a compound Pattern-Lab round.

        Both stages share a single PNG -- the spliced chart -- with the
        prompt asking for the LEFT or RIGHT half. The compound state lives
        in ``run_state['compound']`` and is cleared once the round resolves.
        """
        stages = compound["stages"]
        if stage == 1:
            seed = random.randint(1, 1_000_000)
            run_state["compound"] = {
                "active":         True,
                "compound_key":   compound["key"],
                "compound_name":  compound["name"],
                "stages":         stages,
                "stage":          1,
                "seed":           seed,
                "picked_left":    None,
                "stage1_correct": None,
            }
        else:
            run_state["compound"]["stage"] = 2
            seed = int(run_state["compound"]["seed"])

        stage_def = stages[stage - 1]
        correct_shape = stage_def["answer"]
        fewer = bool(run_state.get("perks", {}).get("fewer_options", False))
        options = self._compound_stage_options(correct_shape, fewer)
        file = sage_img.pattern_compound_file(
            compound["key"], stages, seed,
        )
        attachment = file.filename
        side = "LEFT" if stage == 1 else "RIGHT"
        timer_s = _PATTERN_TIMER + int(run_state.get("perks", {}).get("bonus_time", 0))
        reward_round = sage_svc.round_reward_usd(round_index) * float(sc.COMPOUND_REWARD_MULT)
        bonus_pct = int(sc.COMPOUND_REWARD_MULT * 100 - 100)
        emb = (
            card(
                f"\U0001F4C8 Pattern Lab · Round {round_index} · Compound",
                description=(
                    f"Two patterns spliced into one chart. Step **{stage} of 2** -- "
                    f"identify the **{side}** half.\n\n"
                    f"\U0001F4B8 Round reward (both right): **{fmt_usd(reward_round)}** "
                    f"({bonus_pct}% bonus)"
                ),
                color=C_GOLD,
            )
            .footer(
                f"You have {timer_s}s. Wrong on either half ends the run."
            )
            .build()
        )
        emb.set_image(url=f"attachment://{attachment}")
        view = SageGameView(
            self, ctx, sc.GAME_PATTERN,
            question_key=compound["key"],
            options=options,
            correct_key=correct_shape,
            round_index=round_index,
            timer_s=timer_s,
            run_state=run_state,
        )
        if round_index == 1 and stage == 1:
            await ctx.reply(embed=emb, file=file, view=view, mention_author=False)
        else:
            await ctx.send(embed=emb, file=file, view=view)

    async def _on_answer(
        self, ctx: DiscoContext, view: SageGameView,
        picked_key: Optional[str],
    ) -> None:
        compound_state = view.run_state.get("compound") or {}
        if compound_state.get("active"):
            await self._on_answer_compound(ctx, view, picked_key)
            return

        correct = picked_key == view.correct_key

        # Second Wind: a wrong answer is forgiven (once per run) if armed.
        if not correct and await self._consume_second_wind(ctx, view):
            explanation, correct_label = self._explanation_for(
                view.game, view.question_key, view.correct_key,
            )
            await self._send_second_wind_embed(
                ctx, view,
                body=(
                    f"Wrong -- but your **Second Wind** absorbed it. The run "
                    f"continues; this round pays nothing.\n\n"
                    f"**Answer:** {correct_label}\n\n{explanation}"
                ),
            )
            await asyncio.sleep(1.2)
            await self._render_round(
                ctx, view.game, view.run_state, view.round_index + 1,
            )
            return

        # Resolve round in DB.
        try:
            res = await sage_svc.resolve_round(
                ctx.db, ctx.guild_id, ctx.author.id, view.game,
                correct=correct, round_index=view.round_index,
                xp_mult=float(view.run_state.get("perks", {}).get("xp_mult", 1.0)),
            )
        except Exception:
            log.exception("sage._on_answer: resolve_round failed")
            return

        # Update accumulators on a correct answer.
        if correct:
            view.run_state["score"] += 1
            view.run_state["sage_raw_total"] += int(res.sage_credited_raw)
            view.run_state["edu_raw_total"] += int(res.edu_credited_raw)
            # Fire quest/achievement events for the correct answer.
            await self._emit_event(
                "sage_correct",
                user_id=ctx.author.id, guild_id=ctx.guild_id,
                game=view.game, round_index=view.round_index,
            )
            for milestone in (10, 25):
                if view.run_state["score"] == milestone:
                    await self._emit_event(
                        f"sage_streak_{milestone}",
                        user_id=ctx.author.id, guild_id=ctx.guild_id,
                        game=view.game,
                    )

        # Build the explanation embed.
        explanation, correct_label = self._explanation_for(
            view.game, view.question_key, view.correct_key,
        )
        title = (
            f"{sc.GAME_EMOJIS.get(view.game, '')} Correct! · Round {view.round_index}"
            if correct
            else f"\U0000274C Wrong · Run over after {view.run_state['score']} correct"
        )
        color = C_SUCCESS if correct else C_ERROR
        body = (
            f"**Answer:** {correct_label}\n\n{explanation}"
        )
        emb = (
            card(title, description=body, color=color)
            .field(
                "Round reward",
                (
                    f"+{fmt_token(to_human(res.sage_credited_raw), 'SAGE')} · "
                    f"+{fmt_token(to_human(res.edu_credited_raw), 'EDU')}"
                ) if correct else "No reward.",
                False,
            )
            .field(
                "Run total",
                (
                    f"{view.run_state['score']} correct · "
                    f"{fmt_token(to_human(view.run_state['sage_raw_total']), 'SAGE')} · "
                    f"{fmt_token(to_human(view.run_state['edu_raw_total']), 'EDU')}"
                ),
                False,
            )
            .field_if(
                res.leveled_up,
                "\U0001F31F Sage Level Up!",
                f"You are now Lv **{res.new_level}**.",
                False,
            )
            .build()
        )
        # Same reasoning as _render_round below: the original ,pattern
        # command may have been auto-deleted by now, so go straight to
        # send() instead of paying for a doomed reply -> send fallback.
        await ctx.send(embed=emb)

        if not correct:
            # Run over: record + clear lock.
            try:
                await sage_svc.finalise_run(
                    ctx.db, ctx.guild_id, ctx.author.id, view.game,
                    score=view.run_state["score"],
                    total_sage_raw=view.run_state["sage_raw_total"],
                    total_edu_raw=view.run_state["edu_raw_total"],
                )
            except Exception:
                log.exception("sage: finalise_run failed")
            # Quest/achievement run-finished event.
            await self._emit_event(
                "sage_run_finished",
                user_id=ctx.author.id, guild_id=ctx.guild_id,
                game=view.game, score=view.run_state["score"],
            )
            # Triathlete check: does the user now have a run in all 3 games?
            try:
                row = await ctx.db.fetch_val(
                    """
                    SELECT COUNT(DISTINCT game)
                      FROM sage_runs
                     WHERE guild_id = $1 AND user_id = $2
                    """,
                    int(ctx.guild_id), int(ctx.author.id),
                )
                if int(row or 0) >= 3:
                    await self._emit_event(
                        "sage_triathlete",
                        user_id=ctx.author.id, guild_id=ctx.guild_id,
                    )
            except Exception:
                log.debug("sage: triathlete check failed", exc_info=True)
            return

        # Correct: small pause then next round.
        await asyncio.sleep(1.2)
        await self._render_round(
            ctx, view.game, view.run_state, view.round_index + 1,
        )

    async def _on_answer_compound(
        self, ctx: DiscoContext, view: SageGameView,
        picked_key: Optional[str],
    ) -> None:
        """Handle a click on a compound stage.

        Stage 1 click  -- record + advance to stage 2 (no DB resolve yet).
        Stage 2 click  -- resolve_round(compound=True) with correct = both
        stages right; clear the compound state; emit events as normal.
        """
        compound = view.run_state["compound"]
        stage_idx = int(compound["stage"])
        correct_this_stage = picked_key == view.correct_key

        if stage_idx == 1:
            compound["picked_left"] = picked_key
            compound["stage1_correct"] = bool(correct_this_stage)
            # If they got the first half wrong, end the run immediately
            # but still show both correct answers in the explanation.
            if not correct_this_stage:
                await self._finalise_compound_round(
                    ctx, view, both_correct=False,
                )
                return
            # Otherwise advance to stage 2 with the same chart.
            await asyncio.sleep(0.6)
            await self._render_compound_stage(
                ctx, view.run_state, view.round_index,
                {
                    "key":   compound["compound_key"],
                    "name":  compound["compound_name"],
                    "stages": compound["stages"],
                },
                stage=2,
            )
            return

        # Stage 2 click: both stages have now been answered.
        both_correct = bool(compound["stage1_correct"]) and bool(correct_this_stage)
        await self._finalise_compound_round(ctx, view, both_correct=both_correct)

    async def _finalise_compound_round(
        self, ctx: DiscoContext, view: SageGameView, *, both_correct: bool,
    ) -> None:
        """Resolve a compound round in the DB and post the explanation embed.

        ``both_correct`` drives the reward (correct=True only when both
        stages were right). Run continues on success; ends on any miss.
        """
        compound = view.run_state.get("compound") or {}
        stages = compound.get("stages", [])
        compound_key = compound.get("compound_key", "")
        compound_name = compound.get("compound_name", "Compound")
        compound_def = sc.COMPOUND_BY_KEY.get(compound_key) or {}

        left_correct_key = stages[0]["answer"] if stages else ""
        right_correct_key = stages[1]["answer"] if len(stages) > 1 else ""
        left_name = (sc.PATTERN_BY_KEY.get(left_correct_key) or {}).get("name", left_correct_key)
        right_name = (sc.PATTERN_BY_KEY.get(right_correct_key) or {}).get("name", right_correct_key)
        explanation = compound_def.get("explanation", "")

        # Second Wind: forgive a missed compound (once per run) if armed.
        if not both_correct and await self._consume_second_wind(ctx, view):
            view.run_state["compound"] = None
            await self._send_second_wind_embed(
                ctx, view,
                body=(
                    f"Missed the compound -- but your **Second Wind** "
                    f"absorbed it. The run continues; this round pays "
                    f"nothing.\n\n"
                    f"**Compound:** {compound_name}\n"
                    f"- LEFT half: **{left_name}**\n"
                    f"- RIGHT half: **{right_name}**\n\n"
                    f"{explanation}"
                ),
            )
            await asyncio.sleep(1.2)
            await self._render_round(
                ctx, view.game, view.run_state, view.round_index + 1,
            )
            return

        try:
            res = await sage_svc.resolve_round(
                ctx.db, ctx.guild_id, ctx.author.id, view.game,
                correct=both_correct,
                round_index=view.round_index,
                compound=True,
                xp_mult=float(view.run_state.get("perks", {}).get("xp_mult", 1.0)),
            )
        except Exception:
            log.exception("sage._on_answer_compound: resolve_round failed")
            return

        if both_correct:
            view.run_state["score"] += 1
            view.run_state["sage_raw_total"] += int(res.sage_credited_raw)
            view.run_state["edu_raw_total"] += int(res.edu_credited_raw)
            await self._emit_event(
                "sage_correct",
                user_id=ctx.author.id, guild_id=ctx.guild_id,
                game=view.game, round_index=view.round_index,
                compound=True,
            )
            for milestone in (10, 25):
                if view.run_state["score"] == milestone:
                    await self._emit_event(
                        f"sage_streak_{milestone}",
                        user_id=ctx.author.id, guild_id=ctx.guild_id,
                        game=view.game,
                    )

        title = (
            f"\U0001F4C8 Correct! · Round {view.round_index} · Compound"
            if both_correct
            else f"\U0000274C Wrong · Run over after {view.run_state['score']} correct"
        )
        color = C_SUCCESS if both_correct else C_ERROR
        body = (
            f"**Compound:** {compound_name}\n"
            f"• LEFT half: **{left_name}**\n"
            f"• RIGHT half: **{right_name}**\n\n"
            f"{explanation}"
        )
        emb = (
            card(title, description=body, color=color)
            .field(
                "Round reward",
                (
                    f"+{fmt_token(to_human(res.sage_credited_raw), 'SAGE')} · "
                    f"+{fmt_token(to_human(res.edu_credited_raw), 'EDU')} "
                    f"({int(sc.COMPOUND_REWARD_MULT*100-100)}% compound bonus)"
                ) if both_correct else "No reward.",
                False,
            )
            .field(
                "Run total",
                (
                    f"{view.run_state['score']} correct · "
                    f"{fmt_token(to_human(view.run_state['sage_raw_total']), 'SAGE')} · "
                    f"{fmt_token(to_human(view.run_state['edu_raw_total']), 'EDU')}"
                ),
                False,
            )
            .field_if(
                res.leveled_up,
                "\U0001F31F Sage Level Up!",
                f"You are now Lv **{res.new_level}**.",
                False,
            )
            .build()
        )
        await ctx.send(embed=emb)

        # Clear compound state regardless of outcome.
        view.run_state["compound"] = None

        if not both_correct:
            try:
                await sage_svc.finalise_run(
                    ctx.db, ctx.guild_id, ctx.author.id, view.game,
                    score=view.run_state["score"],
                    total_sage_raw=view.run_state["sage_raw_total"],
                    total_edu_raw=view.run_state["edu_raw_total"],
                )
            except Exception:
                log.exception("sage: finalise_run failed (compound)")
            await self._emit_event(
                "sage_run_finished",
                user_id=ctx.author.id, guild_id=ctx.guild_id,
                game=view.game, score=view.run_state["score"],
            )
            return

        await asyncio.sleep(1.2)
        await self._render_round(
            ctx, view.game, view.run_state, view.round_index + 1,
        )

    async def _emit_event(self, event: str, **kwargs) -> None:
        """Fire a bus event for quests/achievements to consume.

        Best-effort: a missing bus or registry failure must not break the
        game flow. Mirrors services/fishing._publish_economy_event shape.
        """
        try:
            bus = getattr(self.bot, "bus", None)
            if bus is None:
                return
            await bus.publish(event, **kwargs)
        except Exception:
            log.debug("sage: bus publish %s failed", event, exc_info=True)

    def _explanation_for(
        self, game: str, question_key: str, correct_key: str,
    ) -> tuple[str, str]:
        if game == sc.GAME_PATTERN:
            p = sc.PATTERN_BY_KEY.get(correct_key) or {}
            return p.get("explanation", ""), p.get("name", correct_key)
        if game == sc.GAME_GAUGE:
            i = sc.INDICATOR_BY_KEY.get(question_key) or {}
            return i.get("explanation", ""), sc.GAUGE_OPTION_LABELS.get(correct_key, correct_key)
        if game == sc.GAME_CYCLE:
            c = sc.CYCLE_BY_KEY.get(question_key) or {}
            return c.get("explanation", ""), sc.CYCLE_OPTION_LABELS.get(correct_key, correct_key)
        t = sc.TOKENOMICS_BY_KEY.get(question_key) or {}
        return t.get("explanation", ""), sc.TKNOM_OPTION_LABELS.get(correct_key, correct_key)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(SageCog(bot))
