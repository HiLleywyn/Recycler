"""cogs/quests.py - daily + weekly quest view and claim.

Commands
--------
``,quests``
    View your current daily and weekly quests with progress bars. Also
    auto-assigns new quests if this is the first view for the current
    period.

``,quests claim <slot>``
    Claim a completed quest by slot number (1-based, as shown in the
    card). Use ``,quests claim all`` to sweep every completed unclaimed
    quest at once.

Reset cadence
-------------
Daily quests roll over at 00:00 UTC; weekly quests at ISO week boundary
(Mon 00:00 UTC). The "roll" is lazy - a new period just has a new
period_key, so quests appear automatically on first view after midnight.
"""
from __future__ import annotations

import datetime as _dt
import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import C_GOLD, C_SUCCESS, FormatKit

import configs.quests_config as _catalog
from services import quests as _svc

log = logging.getLogger(__name__)


def _quest_line(idx: int, row: dict) -> str:
    """Render a single quest row as one block in the embed."""
    tmpl = _catalog.get(row["quest_id"]) or {}
    name = tmpl.get("name", row["quest_id"])
    desc = tmpl.get("description", "")
    icon = tmpl.get("icon", "\U0001F3AF")
    progress = int(row["progress"])
    target = int(row["target"])
    reward = float(row["reward_usd"] or 0.0)

    if row["claimed"]:
        state = "\U00002705 Claimed"
    elif progress >= target:
        state = "\U0001F381 Ready to claim"
    else:
        state = f"{FormatKit.bar(progress, target, width=10, show_pct=False)} {progress}/{target}"

    return (
        f"**{idx}. {icon} {name}**  -  {FormatKit.usd(reward)}\n"
        f"{desc}\n"
        f"{state}"
    )


def _next_reset_delta(period: str) -> _dt.timedelta:
    """Return the wall-clock duration until the current period rolls over.

    Daily resets at 00:00 UTC; weekly resets at ISO Monday 00:00 UTC.
    Returned as a ``timedelta`` so the caller can format it however it
    wants (we render as ``Xh Ym``).
    """
    now = _dt.datetime.utcnow()
    if period == "daily":
        tomorrow = (now + _dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        return tomorrow - now
    if period == "weekly":
        # Monday is weekday 0. Find the next Monday 00:00 UTC.
        days_ahead = 7 - now.weekday()
        nxt = (now + _dt.timedelta(days=days_ahead)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        return nxt - now
    return _dt.timedelta(0)


def _fmt_delta(delta: _dt.timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _build_card(user: discord.abc.User, rows: dict[str, list[dict]]) -> discord.Embed:
    builder = card(
        f"\U0001F3AF {user.display_name}'s Quests",
        color=C_GOLD,
    )
    for period, header in (("daily", "Daily"), ("weekly", "Weekly")):
        section = rows.get(period) or []
        if not section:
            continue
        lines = []
        for r in section:
            lines.append(_quest_line(int(r["slot"]) + 1, r))
        reset_in = _fmt_delta(_next_reset_delta(period))
        builder.field(
            f"{header} ({_svc.period_key(period)})  -  resets in {reset_in}",
            "\n\n".join(lines),
            inline=False,
        )
    builder.footer(
        "Use ,quests claim <n> to collect a completed reward  -  or 'claim all'."
    )
    return builder.build()


class Quests(commands.Cog):
    """Daily and weekly rotating quests for active players."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        try:
            _svc.attach_listeners(self.bot)
        except Exception as exc:
            log.exception("quests listener attach failed: %s", exc)

    @commands.group(name="quests", aliases=["quest", "q"], invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def quests(self, ctx: DiscoContext) -> None:
        """View your current daily and weekly quests."""
        rows = await _svc.current_for_user(ctx.db, ctx.author.id, ctx.guild_id)
        if not any(rows.values()):
            await ctx.reply_error("No quest templates are configured.")
            return
        await ctx.send_embed(_build_card(ctx.author, rows))

    @quests.command(name="help")
    @guild_only
    @no_bots
    async def quests_help(self, ctx: DiscoContext) -> None:
        """Explain how quests work and list every command."""
        daily = len([q for q in _catalog.QUESTS if q["period"] == "daily"])
        weekly = len([q for q in _catalog.QUESTS if q["period"] == "weekly"])
        embed = (
            card(
                "\U0001F3AF Quests Help",
                description=(
                    "Rotating daily and weekly objectives with USD rewards. "
                    "Progress ticks automatically as you play; claim when "
                    "each bar fills."
                ),
                color=C_GOLD,
            )
            .field(
                "Reset schedule",
                f"**Daily**: {_catalog.DAILY_SLOTS} slots, resets 00:00 UTC.\n"
                f"**Weekly**: {_catalog.WEEKLY_SLOTS} slots, resets Monday "
                f"00:00 UTC.\nQuests are picked at random from a pool of "
                f"**{daily}** daily templates and **{weekly}** weekly "
                f"templates the first time you view after a reset.",
                inline=False,
            )
            .field(
                "Commands",
                "`,quests` - your current quest card with progress bars\n"
                "`,quests claim <slot>` - claim a single completed quest\n"
                "`,quests claim all` - sweep every completed unclaimed quest",
                inline=False,
            )
            .field(
                "How progress works",
                "Each quest ticks +1 per qualifying activity (trade, work "
                "shift, mining payout, buddy win, etc.). Progress is capped "
                "at the target; overshooting doesn't waste anything.",
                inline=False,
            )
            .field(
                "Aliases",
                "`,quest` `,q` for the group; slots are 1-based in the order "
                "shown on the card (daily first, then weekly).",
                inline=False,
            )
            .footer(
                "Quests share triggers with achievements and the season "
                "pass, so one action counts toward all three."
            )
            .build()
        )
        await ctx.send_embed(embed)

    @quests.command(name="claim")
    @guild_only
    @no_bots
    @ensure_registered
    async def quests_claim(self, ctx: DiscoContext, target: str) -> None:
        """Claim a completed quest. ``target`` is a slot number (1..N) or 'all'.

        Slot numbers match the rendered order in ``,quests``: daily 1..3
        first, then weekly 1..2. Use ``all`` to sweep everything ready.
        """
        uid = ctx.author.id
        gid = ctx.guild_id
        db = ctx.db

        rows = await _svc.current_for_user(db, uid, gid)
        flat: list[tuple[str, dict]] = []
        for period in ("daily", "weekly"):
            for r in rows.get(period, []):
                flat.append((period, r))

        if not flat:
            await ctx.reply_error("No quests are available to claim.")
            return

        to_claim: list[tuple[str, int]] = []
        if target.lower() == "all":
            for period, r in flat:
                if not r["claimed"] and int(r["progress"]) >= int(r["target"]):
                    to_claim.append((period, int(r["slot"])))
            if not to_claim:
                await ctx.reply_error("No completed quests to claim yet.")
                return
        else:
            try:
                idx = int(target)
            except ValueError:
                await ctx.reply_error("Slot must be a number or 'all'.")
                return
            if idx < 1 or idx > len(flat):
                await ctx.reply_error(f"Slot must be between 1 and {len(flat)}.")
                return
            period, r = flat[idx - 1]
            to_claim.append((period, int(r["slot"])))

        total_reward = 0.0
        claimed_names: list[str] = []
        failures: list[str] = []
        for period, slot in to_claim:
            ok, msg, reward = await _svc.claim(self.bot, uid, gid, period, slot)
            if ok:
                total_reward += reward
                tmpl = _catalog.get(rows[period][slot]["quest_id"]) or {}
                claimed_names.append(tmpl.get("name", rows[period][slot]["quest_id"]))
            else:
                failures.append(msg)

        if not claimed_names:
            await ctx.reply_error(failures[0] if failures else "Nothing to claim.")
            return

        body = "\n".join(f"\U00002705 {n}" for n in claimed_names)
        body += f"\n\n**Total: {FormatKit.usd(total_reward)}**"
        await ctx.send_embed(
            card("Quest Rewards Claimed", description=body, color=C_SUCCESS).build()
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Quests(bot))
