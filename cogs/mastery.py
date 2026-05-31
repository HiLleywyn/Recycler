"""``,mastery`` cog -- cross-system progression for Discoin.

Mastery is Discoin's nine-track + twenty-node passive progression layer.
Every minigame feeds a track; track levels grant points; points unlock
passives that apply across the entire bot. The XP and node math lives
in ``mastery_config``; this cog is presentation + the small command
surface a player needs to plan their build.

Commands:
    ,mastery                -- PNG board + summary embed
    ,mastery tracks         -- list all 9 tracks + where XP comes from
    ,mastery branches       -- explain the 4 skill-tree branches
    ,mastery unlock <id>    -- spend points on a node
    ,mastery info <id>      -- inspect a single node before spending
    ,mastery reset          -- paid wipe (cost doubles each reset)
"""
from __future__ import annotations

import io
import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import C_ERROR, C_GOLD, C_INFO, C_PURPLE, C_SUCCESS, C_TEAL
from configs.mastery_config import (
    BRANCH_INFO,
    BRANCHES,
    NODES,
    NODES_BY_ID,
    TRACKS,
    TRACK_MAX_LEVEL,
    xp_for_level,
)
from services import mastery as _svc
from services.mastery_render import render_mastery_board

log = logging.getLogger(__name__)


def _branch_color(branch: str) -> int:
    return {
        "economy": C_GOLD,
        "combat":  C_ERROR,
        "luck":    C_PURPLE,
        "utility": C_TEAL,
    }.get(branch, C_INFO)


def _suggest_next_nodes(summary, *, limit: int = 4) -> list[dict]:
    """Pick affordable, unblocked nodes the player could buy right now.

    Sorted by cost ascending so the cheapest pick-ups surface first.
    Empty list when nothing matches.
    """
    out: list[dict] = []
    available = int(summary.points_available)
    for node in sorted(NODES, key=lambda n: int(n.get("cost", 0))):
        if node["id"] in summary.unlocked:
            continue
        if int(node.get("cost", 0)) > available:
            continue
        prereqs = node.get("prereqs", []) or []
        if any(p not in summary.unlocked for p in prereqs):
            continue
        out.append(node)
        if len(out) >= limit:
            break
    return out


def _track_summary_line(key: str, info: dict | None) -> str:
    meta = TRACKS.get(key, {"label": key, "emoji": "⭐"})
    if not info:
        return f"{meta['emoji']} **{meta['label']}** -- L1  (0 XP)"
    lvl = int(info.get("level", 1))
    xp = int(info.get("xp", 0))
    nxt = int(info.get("next_threshold", 0))
    if lvl >= TRACK_MAX_LEVEL:
        return (
            f"{meta['emoji']} **{meta['label']}** -- L{lvl} (MAX)  "
            f"-  {xp:,} XP"
        )
    return (
        f"{meta['emoji']} **{meta['label']}** -- L{lvl}  "
        f"({xp:,} / {nxt:,} XP to L{lvl + 1})"
    )


class Mastery(commands.Cog):
    """Cross-system meta-progression for Discoin."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Wire per-micro-action mastery XP listeners onto the bus.

        Without this every track only gained XP on the big end-of-loop
        cashout (delve cashout / fish reel cashout / craft forge
        cashout). Active players reasonably expected each catch /
        harvest / craft / kill to nudge the relevant track. The
        listener attaches a small flat per-event XP grant for each.
        """
        try:
            from services import mastery as _svc
            _svc.attach_listeners(self.bot)
        except Exception as exc:
            log.exception("mastery listener attach failed: %s", exc)

    @commands.hybrid_group(name="mastery", invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def mastery(self, ctx: DiscoContext) -> None:
        """Show your Mastery board: tracks, points, nodes, suggested unlocks."""
        summary = await _svc.mastery_summary(ctx.db, ctx.author.id, ctx.guild_id)
        png = render_mastery_board(
            summary,
            display_name=ctx.author.display_name,
        )
        file = discord.File(io.BytesIO(png), filename="mastery.png")
        unlocked_count = len(summary.unlocked)
        total = len(NODES)

        # Branch unlock counts (e.g. economy 2/5)
        per_branch: dict[str, tuple[int, int]] = {}
        for branch in BRANCHES:
            in_branch = [n for n in NODES if n["branch"] == branch]
            owned = sum(1 for n in in_branch if n["id"] in summary.unlocked)
            per_branch[branch] = (owned, len(in_branch))
        branch_line = "  ".join(
            f"{BRANCH_INFO[b]['emoji']} **{BRANCH_INFO[b]['label']}** "
            f"{per_branch[b][0]}/{per_branch[b][1]}"
            for b in BRANCHES
        )

        # Top 3 track lines (highest level first) -- the rest live in
        # ,mastery tracks so this embed stays scannable.
        sorted_tracks = sorted(
            TRACKS.keys(),
            key=lambda k: int((summary.tracks.get(k) or {}).get("level", 1)),
            reverse=True,
        )
        top_lines = [
            _track_summary_line(k, summary.tracks.get(k))
            for k in sorted_tracks[:3]
        ]

        # Next-suggested affordable nodes
        suggestions = _suggest_next_nodes(summary, limit=4)
        if suggestions:
            sug_lines = [
                f"`{n['id']}`  -  **{n['name']}** ({n['cost']} pt"
                f"{'s' if n['cost'] != 1 else ''})\n"
                f"   {n['description']}"
                for n in suggestions
            ]
            sug_value = "\n".join(sug_lines)
        elif summary.points_available <= 0:
            sug_value = (
                "No points banked. Level any track to earn points "
                "(+1 per level, bonus every 10 levels)."
            )
        else:
            sug_value = (
                "Every affordable node is blocked by a prereq. Run "
                "`,mastery info <node_id>` to plan a path."
            )

        embed = (
            card("Mastery  -  cross-system progression", color=C_GOLD)
            .description(
                f"**{summary.points_available}** point"
                f"{'s' if summary.points_available != 1 else ''} available  -  "
                f"**{unlocked_count}/{total}** nodes unlocked  -  "
                f"**{summary.points_spent}** spent  -  "
                f"resets used: **{summary.resets_used}**"
            )
            .field("Branches", branch_line, False)
            .field("Top tracks", "\n".join(top_lines) or "_No XP yet._", False)
            .field(
                f"Suggested next unlocks ({len(suggestions)})",
                sug_value[:1024],
                False,
            )
            .image("attachment://mastery.png")
            .footer(
                "`,mastery tracks` -- all 9 tracks  -  "
                "`,mastery branches` -- branch guide  -  "
                "`,mastery unlock <id>` -- spend points"
            )
            .build()
        )
        await ctx.reply(embed=embed, file=file, mention_author=False)

    @mastery.command(name="tracks")
    async def mastery_tracks(self, ctx: DiscoContext) -> None:
        """List all 9 mastery tracks and where each one earns XP from."""
        summary = await _svc.mastery_summary(ctx.db, ctx.author.id, ctx.guild_id)
        embed = card(
            "Mastery tracks  -  9 paths, 1 board",
            color=C_INFO,
            description=(
                "Every minigame feeds one of these tracks. Hit a level "
                "milestone and you bank a mastery point to spend on the "
                "node tree. Levels cap at "
                f"**L{TRACK_MAX_LEVEL}**, with a bonus point at every 10."
            ),
        )
        for key, meta in TRACKS.items():
            info = summary.tracks.get(key) or {}
            lvl = int(info.get("level", 1))
            xp = int(info.get("xp", 0))
            next_thr = int(info.get("next_threshold", xp_for_level(lvl + 1)))
            progress_str = (
                f"L{lvl} (MAX)  -  {xp:,} XP"
                if lvl >= TRACK_MAX_LEVEL
                else f"L{lvl}  -  {xp:,} / {next_thr:,} XP"
            )
            body = (
                f"_{meta.get('xp_source', '')}_\n"
                f"**Synergy:** {meta.get('synergy', '_None._')}\n"
                f"**Progress:** {progress_str}"
            )
            embed.field(
                f"{meta['emoji']}  {meta['label']}",
                body[:1024],
                inline=False,
            )
        embed.footer(
            "`,mastery` -- full board PNG  -  "
            "`,mastery branches` -- branch guide"
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @mastery.command(name="branches")
    async def mastery_branches(self, ctx: DiscoContext) -> None:
        """Explain the four branches of the mastery node tree."""
        summary = await _svc.mastery_summary(ctx.db, ctx.author.id, ctx.guild_id)
        embed = card(
            "Mastery branches  -  four ways to spec",
            color=C_GOLD,
            description=(
                "The 20-node tree splits into four colour-coded branches. "
                "Most builds mix two. Pick what your gameplay loop looks "
                "like and follow the prereq chain."
            ),
        )
        for branch in BRANCHES:
            meta = BRANCH_INFO[branch]
            in_branch = [n for n in NODES if n["branch"] == branch]
            owned = sum(1 for n in in_branch if n["id"] in summary.unlocked)
            total_cost = sum(int(n["cost"]) for n in in_branch)
            node_list = "\n".join(
                f"  - `{n['id']}` ({n['cost']} pt) -- {n['description']}"
                for n in in_branch
            )
            body = (
                f"_{meta['tagline']}_\n"
                f"{meta['what_it_does']}\n\n"
                f"**Progress:** {owned}/{len(in_branch)} unlocked  -  "
                f"full clear costs **{total_cost}** points.\n"
                f"{node_list}"
            )
            embed.field(
                f"{meta['emoji']}  {meta['label']}",
                body[:1024],
                inline=False,
            )
        embed.footer(
            "`,mastery info <id>` -- inspect a single node  -  "
            "`,mastery unlock <id>` -- spend points"
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @mastery.command(name="unlock")
    async def mastery_unlock(self, ctx: DiscoContext, *, node_id: str) -> None:
        """Spend mastery points to unlock a node.

        Run `,mastery` first to see your board. ``node_id`` is the
        short id printed on each card (e.g. ``econ.daily_bonus.1``).
        """
        node_id = node_id.strip().lower()
        ok, msg = await _svc.unlock_node(ctx.db, ctx.author.id, ctx.guild_id, node_id)
        if not ok:
            await ctx.reply_error(msg)
            return
        await ctx.reply_success(msg, title="Mastery unlock")

    @mastery.command(name="reset")
    async def mastery_reset(self, ctx: DiscoContext) -> None:
        """Refund every spent mastery point (paid; cost doubles each reset)."""
        ok, msg, cost = await _svc.reset_tree(ctx.db, ctx.author.id, ctx.guild_id)
        if not ok:
            await ctx.reply_error(msg)
            return
        embed = (
            card("Mastery reset", color=C_SUCCESS)
            .description(msg)
            .field("Next reset cost", f"${cost * 2:,.2f}", True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @mastery.command(name="info")
    async def mastery_info(self, ctx: DiscoContext, *, node_id: str) -> None:
        """Inspect a single node's effect and prereqs."""
        node = NODES_BY_ID.get(node_id.strip().lower())
        if not node:
            await ctx.reply_error(f"Unknown node `{node_id}`.")
            return
        # Tree forward links: which nodes does this one unlock?
        unlocks = [n for n in NODES if node["id"] in (n.get("prereqs") or [])]
        prereq_lines = (
            "\n".join(
                f"- `{p}`  -  {NODES_BY_ID.get(p, {}).get('name', p)}"
                for p in node.get("prereqs", [])
            )
            or "_None -- this is a root node._"
        )
        unlocks_lines = (
            "\n".join(
                f"- `{n['id']}`  -  {n['name']} ({n['cost']} pt)"
                for n in unlocks
            )
            or "_None -- this is a leaf node._"
        )
        branch_meta = BRANCH_INFO.get(node["branch"], {})
        embed = (
            card(node["name"], color=_branch_color(node["branch"]))
            .description(node["description"])
            .field(
                "Branch",
                f"{branch_meta.get('emoji', '')} {branch_meta.get('label', node['branch'].title())}",
                True,
            )
            .field("Cost", f"{node['cost']} point{'s' if node['cost'] != 1 else ''}", True)
            .field(
                "Effect",
                f"`{node['effect_key']}` += `{node['effect_value']}`",
                True,
            )
            .field("Prereqs", prereq_lines, False)
            .field("Unlocks", unlocks_lines, False)
            .footer(f"Node id: {node['id']}  -  ,mastery unlock {node['id']}")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Mastery(bot))
