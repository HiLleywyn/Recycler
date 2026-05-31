"""
cogs/chat_leveling_admin.py  -  Admin ,levelconfig commands.

Owns every mutator for the chat leveling stack:
  * toggle / rate / cooldown / min-chars / announce-channel / dm / stack
  * curve coefficients (quad, lin, base)
  * role rewards and rank titles
  * manual user level / xp overrides and resets
  * bulk CSV import from the message attachment (the primary migration path
    off of MEE6 / Arcane / Tatsu)

Every command requires the Manage Server permission.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only
from core.framework.ui import (
    C_ERROR, C_INFO, C_NAVY, C_SUCCESS, C_WARNING,
)
from services.chat_leveling import (
    LevelingConfig,
    add_role_reward,
    delete_rank,
    get_config,
    get_leaderboard,
    get_role_rewards,
    level_from_total_xp,
    recompute_levels,
    remove_role_reward,
    set_config_field,
    set_rank,
    set_user_level,
    set_user_xp,
    sync_member_roles,
)
from services.chat_leveling_csv import ImportResult, parse_and_import

log = logging.getLogger(__name__)


def _require_manage_guild():
    async def predicate(ctx: DiscoContext) -> bool:
        if not ctx.guild:
            raise commands.CheckFailure("This command can only be used in a server.")
        if not ctx.author.guild_permissions.manage_guild:
            raise commands.CheckFailure(
                "You need **Manage Server** permission to use this command."
            )
        return True
    return commands.check(predicate)


def _parse_onoff(value: str) -> bool | None:
    v = value.strip().lower()
    if v in ("on", "enable", "enabled", "true", "yes", "1"):
        return True
    if v in ("off", "disable", "disabled", "false", "no", "0"):
        return False
    return None


class ChatLevelingAdmin(commands.Cog):
    """Admin-only ,levelconfig command group."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # -- Root group -----------------------------------------------------------

    @commands.group(name="levelconfig", aliases=["lvlcfg"], invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def levelconfig(self, ctx: DiscoContext) -> None:
        """Chat leveling admin group.  Run `,levelconfig show` for current config."""
        await self._show(ctx)

    # -- show -----------------------------------------------------------------

    @levelconfig.command(name="show")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_show(self, ctx: DiscoContext) -> None:
        """Display the current leveling configuration."""
        await self._show(ctx)

    async def _show(self, ctx: DiscoContext) -> None:
        cfg = await get_config(ctx.db, ctx.guild_id)
        announce = "same channel as the level-up"
        if cfg.announce_channel:
            ch = ctx.guild.get_channel(cfg.announce_channel) if ctx.guild else None
            announce = ch.mention if ch else f"`unknown channel {cfg.announce_channel}`"

        ranks = await ctx.db.fetch_all(
            "SELECT level, rank_name FROM chat_level_ranks WHERE guild_id=$1 "
            "ORDER BY level ASC",
            ctx.guild_id,
        )
        roles = await ctx.db.fetch_all(
            "SELECT level, role_id FROM chat_level_roles WHERE guild_id=$1 "
            "ORDER BY level ASC, role_id ASC",
            ctx.guild_id,
        )

        rank_lines: list[str] = []
        for r in ranks[:10]:
            rank_lines.append(f"Lvl {int(r['level'])}  -  {r['rank_name']}")
        if len(ranks) > 10:
            rank_lines.append(f"...and {len(ranks) - 10} more")

        role_lines: list[str] = []
        for r in roles[:10]:
            rid = int(r["role_id"])
            role = ctx.guild.get_role(rid) if ctx.guild else None
            role_lines.append(f"Lvl {int(r['level'])}  -  {role.mention if role else f'`unknown {rid}`'}")
        if len(roles) > 10:
            role_lines.append(f"...and {len(roles) - 10} more")

        embed = (
            card("Chat Leveling Config", color=C_NAVY)
            .field("Enabled", "yes" if cfg.enabled else "no", True)
            .field("XP per message", f"{cfg.xp_min}  -  {cfg.xp_max}", True)
            .field("Cooldown", f"{cfg.cooldown_seconds}s", True)
            .field("Min chars", str(cfg.min_chars), True)
            .field("Announce channel", announce, True)
            .field("DM on level-up", "yes" if cfg.dm_levelup else "no", True)
            .field("Stack role rewards", "yes" if cfg.stack_roles else "no", True)
            .field(
                "Curve (quad / lin / base)",
                f"{cfg.curve_quad} / {cfg.curve_lin} / {cfg.curve_base}",
                True,
            )
            .field(
                "Streak bonus",
                f"+{cfg.streak_pct_per_day}% / day, cap {cfg.streak_max_days}d "
                f"(max +{cfg.streak_max_days * cfg.streak_pct_per_day}%)",
                True,
            )
            .field(
                "Rank titles",
                "\n".join(rank_lines) if rank_lines else "(none)",
                False,
            )
            .field(
                "Role rewards",
                "\n".join(role_lines) if role_lines else "(none)",
                False,
            )
            .footer("Use ,levelconfig <subcommand> to change a value")
            .build()
        )
        await ctx.send_embed(embed)

    # -- Simple toggles -------------------------------------------------------

    @levelconfig.command(name="enable")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_enable(self, ctx: DiscoContext) -> None:
        await set_config_field(ctx.db, ctx.guild_id, "enabled", True)
        await ctx.reply_success("Chat leveling is now **enabled**.", title="Enabled")

    @levelconfig.command(name="disable")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_disable(self, ctx: DiscoContext) -> None:
        await set_config_field(ctx.db, ctx.guild_id, "enabled", False)
        await ctx.reply_success("Chat leveling is now **disabled**.", title="Disabled")

    # -- Numeric settings -----------------------------------------------------

    @levelconfig.command(name="rate")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_rate(self, ctx: DiscoContext, xp_min: int, xp_max: int) -> None:
        """Set the random XP range rolled for every chat message."""
        if not (1 <= xp_min <= xp_max <= 1000):
            await ctx.reply_error("Need `1 <= min <= max <= 1000`.")
            return
        await set_config_field(ctx.db, ctx.guild_id, "xp_min", xp_min)
        await set_config_field(ctx.db, ctx.guild_id, "xp_max", xp_max)
        await ctx.reply_success(f"XP rate set to **{xp_min} - {xp_max}** per message.", title="Rate")

    @levelconfig.command(name="cooldown")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_cooldown(self, ctx: DiscoContext, seconds: int) -> None:
        """Per-user cooldown between XP grants."""
        if not (1 <= seconds <= 3600):
            await ctx.reply_error("Cooldown must be between `1` and `3600` seconds.")
            return
        await set_config_field(ctx.db, ctx.guild_id, "cooldown_seconds", seconds)
        await ctx.reply_success(f"Cooldown set to **{seconds}s**.", title="Cooldown")

    @levelconfig.command(name="minchars")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_minchars(self, ctx: DiscoContext, n: int) -> None:
        """Minimum message length required to earn XP."""
        if not (0 <= n <= 1000):
            await ctx.reply_error("Min chars must be between `0` and `1000`.")
            return
        await set_config_field(ctx.db, ctx.guild_id, "min_chars", n)
        await ctx.reply_success(f"Minimum message length set to **{n}** characters.", title="Min chars")

    @levelconfig.command(name="channel")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_channel(self, ctx: DiscoContext, target: str | None = None) -> None:
        """Set or clear the level-up announcement channel.  Pass `clear` to unset."""
        if target is None or target.strip().lower() == "clear":
            await set_config_field(ctx.db, ctx.guild_id, "announce_channel", None)
            await ctx.reply_success("Announcement channel cleared; level-ups announce in the same channel.", title="Channel")
            return
        ch_obj: discord.TextChannel | None = None
        try:
            ch_obj = await commands.TextChannelConverter().convert(ctx, target)
        except commands.BadArgument:
            await ctx.reply_error("Could not resolve that channel.  Mention a text channel or pass `clear`.")
            return
        await set_config_field(ctx.db, ctx.guild_id, "announce_channel", ch_obj.id)
        await ctx.reply_success(f"Level-ups will announce in {ch_obj.mention}.", title="Channel")

    @levelconfig.command(name="dm")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_dm(self, ctx: DiscoContext, state: str) -> None:
        """Toggle DM on level-up."""
        val = _parse_onoff(state)
        if val is None:
            await ctx.reply_error("Use `on` or `off`.")
            return
        await set_config_field(ctx.db, ctx.guild_id, "dm_levelup", val)
        await ctx.reply_success(f"DM on level-up **{'enabled' if val else 'disabled'}**.", title="DM")

    @levelconfig.command(name="stackroles")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_stackroles(self, ctx: DiscoContext, state: str) -> None:
        """Stack role rewards, or keep only the highest earned."""
        val = _parse_onoff(state)
        if val is None:
            await ctx.reply_error("Use `on` or `off`.")
            return
        await set_config_field(ctx.db, ctx.guild_id, "stack_roles", val)
        note = "stacked" if val else "only the highest tier is kept"
        await ctx.reply_success(f"Role rewards: **{note}**.", title="Stack roles")

    @levelconfig.command(name="curve")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_curve(self, ctx: DiscoContext, quad: int, lin: int, base: int) -> None:
        """Set the level-up XP curve.  Default `5 50 100` matches MEE6."""
        for name, v in (("quad", quad), ("lin", lin), ("base", base)):
            if not (0 <= v <= 10000):
                await ctx.reply_error(f"`{name}` must be between 0 and 10000.")
                return
        await set_config_field(ctx.db, ctx.guild_id, "curve_quad", quad)
        await set_config_field(ctx.db, ctx.guild_id, "curve_lin", lin)
        await set_config_field(ctx.db, ctx.guild_id, "curve_base", base)
        await ctx.reply_success(
            f"Curve set to `{quad}*n^2 + {lin}*n + {base}` XP per level-up.",
            title="Curve",
        )

    @levelconfig.command(name="recompute")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_recompute(
        self, ctx: DiscoContext,
        quad: int | None = None, lin: int | None = None, base: int | None = None,
    ) -> None:
        """Recalculate every user's level from their stored XP.

        Run this after changing the curve to re-derive correct levels without
        re-importing.  Optionally pass ``quad lin base`` to set a new curve and
        recompute in a single step:  ``,levelconfig recompute 3 30 60``.
        """
        # Validate optional curve args together.
        curve_args = (quad, lin, base)
        if any(v is not None for v in curve_args):
            if any(v is None for v in curve_args):
                await ctx.reply_error("Pass all three curve values, or none.  Example: `,levelconfig recompute 3 30 60`.")
                return
            for name, v in (("quad", quad), ("lin", lin), ("base", base)):
                if not (0 <= v <= 10000):
                    await ctx.reply_error(f"`{name}` must be between 0 and 10000.")
                    return
            await set_config_field(ctx.db, ctx.guild_id, "curve_quad", quad)
            await set_config_field(ctx.db, ctx.guild_id, "curve_lin", lin)
            await set_config_field(ctx.db, ctx.guild_id, "curve_base", base)

        cfg = await get_config(ctx.db, ctx.guild_id)
        status_msg = await ctx.send(
            embed=card(
                "Recomputing levels...",
                description=f"Curve: `{cfg.curve_quad}*n^2 + {cfg.curve_lin}*n + {cfg.curve_base}`",
                color=C_INFO,
            ).build()
        )
        try:
            updated, unchanged = await recompute_levels(ctx.db, ctx.guild_id, cfg)
        except Exception as exc:
            log.exception("recompute_levels failed gid=%s", ctx.guild_id)
            await status_msg.edit(
                embed=card(
                    "Recompute failed",
                    description=f"`{type(exc).__name__}: {exc}`",
                    color=C_ERROR,
                ).build()
            )
            return

        embed = (
            card("Levels recomputed", color=C_SUCCESS)
            .field("Curve", f"`{cfg.curve_quad}*n^2 + {cfg.curve_lin}*n + {cfg.curve_base}`", False)
            .field("Users changed", f"{updated:,}", True)
            .field("Unchanged", f"{unchanged:,}", True)
            .footer("Run ,level leaderboard to verify.  Tweak the curve and rerun if needed.")
            .build()
        )
        try:
            await status_msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send_embed(embed)

    @levelconfig.command(name="preview")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_preview(
        self, ctx: DiscoContext, quad: int, lin: int, base: int,
    ) -> None:
        """Preview top-10 levels under a candidate curve without writing.

        Useful for finding the right curve after importing from a source
        system with an unknown curve.  Once a curve looks right, run
        ``,levelconfig recompute <quad> <lin> <base>`` to apply it.
        """
        for name, v in (("quad", quad), ("lin", lin), ("base", base)):
            if not (0 <= v <= 10000):
                await ctx.reply_error(f"`{name}` must be between 0 and 10000.")
                return
        proposed = LevelingConfig(curve_quad=quad, curve_lin=lin, curve_base=base)
        rows = await get_leaderboard(ctx.db, ctx.guild_id, limit=10, offset=0)
        if not rows:
            await ctx.reply_error("No chat-level rows to preview.")
            return

        lines: list[str] = []
        for idx, r in enumerate(rows, start=1):
            uid = int(r["user_id"])
            xp = int(r["xp"])
            current = int(r["level"] or 0)
            proposed_lvl = level_from_total_xp(xp, proposed)
            name = ctx.guild.get_member(uid).display_name if (ctx.guild and ctx.guild.get_member(uid)) else None
            if name is None:
                u = self.bot.get_user(uid)
                if u is not None:
                    name = u.display_name if hasattr(u, "display_name") else u.name
                else:
                    try:
                        fetched = await self.bot.fetch_user(uid)
                        name = fetched.display_name if hasattr(fetched, "display_name") else fetched.name
                    except (discord.NotFound, discord.HTTPException):
                        name = f"User {uid}"
            arrow = "->" if proposed_lvl != current else "=="
            lines.append(
                f"`#{idx:>2}`  **{name}**  -  {xp:,} XP  -  Lvl {current} {arrow} **{proposed_lvl}**"
            )

        embed = (
            card("Curve preview", color=C_INFO)
            .description(
                f"Proposed curve: `{quad}*n^2 + {lin}*n + {base}`\n\n"
                + "\n".join(lines)
            )
            .footer("Run ,levelconfig recompute <quad> <lin> <base> to apply.")
            .build()
        )
        await ctx.send_embed(embed)

    @levelconfig.command(name="streak")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_streak(self, ctx: DiscoContext, max_days: int, pct_per_day: int) -> None:
        """Configure the daily-chat XP multiplier.

        Linear ramp: XP is multiplied by ``1 + pct_per_day/100 * min(streak, max_days)``.
        Defaults of ``10 1`` give +1% per day up to +10% at a 10-day streak,
        matching the source system.
        """
        if not (0 <= max_days <= 365):
            await ctx.reply_error("`max_days` must be between 0 and 365.")
            return
        if not (0 <= pct_per_day <= 100):
            await ctx.reply_error("`pct_per_day` must be between 0 and 100.")
            return
        await set_config_field(ctx.db, ctx.guild_id, "streak_max_days", max_days)
        await set_config_field(ctx.db, ctx.guild_id, "streak_pct_per_day", pct_per_day)
        cap_pct = max_days * pct_per_day
        await ctx.reply_success(
            f"Streak bonus set to **+{pct_per_day}%** per day, capped at **{max_days}** days "
            f"(max bonus **+{cap_pct}%**).",
            title="Streak",
        )

    # -- Role rewards ---------------------------------------------------------

    @levelconfig.command(name="addrole")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_addrole(self, ctx: DiscoContext, level: int, role: discord.Role) -> None:
        """Grant a role when the member reaches `level`.

        `role` accepts a role ID, role name, or mention.  **Pass the role ID
        to avoid pinging members of that role when running the command.**
        """
        if level < 0:
            await ctx.reply_error("Level must be >= 0.")
            return
        if role.managed or role.is_default():
            await ctx.reply_error("That role is managed by Discord or the @everyone role.")
            return
        me = ctx.guild.me if ctx.guild else None
        if me and role >= me.top_role:
            await ctx.reply_error("I cannot manage a role at or above my highest role.")
            return
        await add_role_reward(ctx.db, ctx.guild_id, level, role.id)
        await ctx.reply_success(f"{role.mention} will be granted at level **{level}**.", title="Role reward added")

    @levelconfig.command(name="removerole")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_removerole(
        self, ctx: DiscoContext, level: int, role: discord.Role | None = None,
    ) -> None:
        """Remove a role reward.  Omit role to remove every reward at that level.

        `role` accepts a role ID, role name, or mention.  Pass the role ID to
        avoid pinging the role's members.
        """
        deleted = await remove_role_reward(
            ctx.db, ctx.guild_id, level, role.id if role else None,
        )
        if deleted == 0:
            await ctx.reply_error("No matching role reward was configured.")
            return
        suffix = f" ({role.mention})" if role else ""
        await ctx.reply_success(f"Removed **{deleted}** role reward(s) at level {level}{suffix}.", title="Role reward removed")

    # -- Rank titles ----------------------------------------------------------

    @levelconfig.command(name="addrank")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_addrank(self, ctx: DiscoContext, level: int, *, name: str) -> None:
        """Set (or overwrite) the rank title shown for `level`+."""
        if level < 0:
            await ctx.reply_error("Level must be >= 0.")
            return
        name = name.strip()
        if not name or len(name) > 64:
            await ctx.reply_error("Rank name must be between 1 and 64 characters.")
            return
        await set_rank(ctx.db, ctx.guild_id, level, name)
        await ctx.reply_success(f"Rank title at level **{level}** set to **{name}**.", title="Rank set")

    @levelconfig.command(name="removerank")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_removerank(self, ctx: DiscoContext, level: int) -> None:
        """Delete the rank title at `level`."""
        ok = await delete_rank(ctx.db, ctx.guild_id, level)
        if not ok:
            await ctx.reply_error("No rank title configured at that level.")
            return
        await ctx.reply_success(f"Rank title at level **{level}** removed.", title="Rank removed")

    # -- Manual overrides -----------------------------------------------------

    @levelconfig.command(name="setlevel")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_setlevel(self, ctx: DiscoContext, member: discord.Member, level: int) -> None:
        """Force a member to a specific level.  Their XP is set to the level's floor."""
        if level < 0:
            await ctx.reply_error("Level must be >= 0.")
            return
        cfg = await get_config(ctx.db, ctx.guild_id)
        total_xp = await set_user_level(ctx.db, ctx.guild_id, member.id, level, cfg)
        await ctx.reply_success(
            f"{member.mention} set to **level {level}** ({total_xp:,} XP).",
            title="Level set",
        )

    @levelconfig.command(name="setxp")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_setxp(self, ctx: DiscoContext, member: discord.Member, total_xp: int) -> None:
        """Set a member's total XP directly.  Level is recomputed from the curve."""
        if total_xp < 0:
            await ctx.reply_error("XP must be >= 0.")
            return
        cfg = await get_config(ctx.db, ctx.guild_id)
        new_level = await set_user_xp(ctx.db, ctx.guild_id, member.id, total_xp, cfg)
        await ctx.reply_success(
            f"{member.mention} set to **{total_xp:,} XP** (level {new_level}).",
            title="XP set",
        )

    @levelconfig.command(name="reset")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_reset(self, ctx: DiscoContext, member: discord.Member) -> None:
        """Wipe a member's chat-level row."""
        await ctx.db.execute(
            "DELETE FROM chat_levels WHERE guild_id=$1 AND user_id=$2",
            ctx.guild_id, member.id,
        )
        await ctx.reply_success(f"Cleared chat-level data for {member.mention}.", title="Reset")

    @levelconfig.command(name="resetall")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_resetall(self, ctx: DiscoContext) -> None:
        """Wipe every user's chat-level row for this guild."""
        ok = await ctx.confirm(
            "This will delete **every** chat-level row for this server.  Continue?",
        )
        if not ok:
            await ctx.reply_error("Reset cancelled.")
            return
        status = await ctx.db.execute(
            "DELETE FROM chat_levels WHERE guild_id=$1", ctx.guild_id,
        )
        try:
            n = int(status.split()[-1])
        except (ValueError, IndexError):
            n = 0
        await ctx.reply_success(f"Deleted **{n:,}** chat-level rows.", title="Reset all")

    @levelconfig.command(name="resyncroles", aliases=["syncroles"])
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_resyncroles(self, ctx: DiscoContext) -> None:
        """Grant every member the reward roles their stored level has earned.

        Run this once after a bulk import to backfill role rewards that
        ``on_message`` only ever grants at level-up. Additive-only: a member
        never loses a role they already hold, even if they haven't earned it.
        """
        rewards = await get_role_rewards(ctx.db, ctx.guild_id)
        if not rewards:
            await ctx.reply_error(
                "No role rewards configured.  Use `,levelconfig addrole <level> <role>` first."
            )
            return

        cfg = await get_config(ctx.db, ctx.guild_id)
        rows = await ctx.db.fetch_all(
            "SELECT user_id, level FROM chat_levels WHERE guild_id=$1 AND level > 0",
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply_error("No users with a stored level in this server yet.")
            return

        ok = await ctx.confirm(
            f"Resync role rewards for **{len(rows):,}** members using the "
            f"{'stacking' if cfg.stack_roles else 'replace-lower-tier'} policy?",
            timeout=60.0,
        )
        if not ok:
            await ctx.reply_error("Resync cancelled.")
            return

        status_msg = await ctx.send(
            embed=card(
                "Resyncing role rewards...",
                description=f"Processing **{len(rows):,}** members.",
                color=C_INFO,
            ).build()
        )

        # Warm the member cache so get_member hits for everyone in the guild.
        guild = ctx.guild
        if guild is not None and not guild.chunked:
            try:
                await guild.chunk(cache=True)
            except Exception as e:
                log.warning("resyncroles: guild.chunk failed gid=%s: %s", ctx.guild_id, e)

        processed = 0
        touched = 0
        added_total = 0
        removed_total = 0
        not_in_guild = 0
        errors = 0
        last_edit = asyncio.get_event_loop().time()

        for r in rows:
            uid = int(r["user_id"])
            lvl = int(r["level"] or 0)
            processed += 1
            member = guild.get_member(uid) if guild else None
            if member is None:
                not_in_guild += 1
            else:
                try:
                    added, removed = await sync_member_roles(
                        member, lvl, rewards, cfg.stack_roles,
                    )
                    if added or removed:
                        touched += 1
                        added_total += len(added)
                        removed_total += len(removed)
                except discord.Forbidden:
                    errors += 1
                except Exception:
                    errors += 1
                    log.debug(
                        "resyncroles: sync failed gid=%s uid=%s",
                        ctx.guild_id, uid, exc_info=True,
                    )

            now = asyncio.get_event_loop().time()
            if now - last_edit >= 2.0:
                last_edit = now
                try:
                    await status_msg.edit(
                        embed=card(
                            "Resyncing role rewards...",
                            description=f"Processed **{processed:,} / {len(rows):,}** members.",
                            color=C_INFO,
                        ).build()
                    )
                except discord.HTTPException:
                    pass

        color = C_SUCCESS if errors == 0 else C_WARNING
        embed = (
            card("Role resync finished", color=color)
            .field("Members checked", f"{processed:,}", True)
            .field("Members updated", f"{touched:,}", True)
            .field("Roles added", f"{added_total:,}", True)
            .field("Roles removed", f"{removed_total:,}", True)
            .field("Not in guild", f"{not_in_guild:,}", True)
            .field("Errors", f"{errors:,}", True)
            .footer(
                "Members who left the guild were skipped.  Re-run after they rejoin."
            )
            .build()
        )
        try:
            await status_msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send_embed(embed)

    # -- CSV import -----------------------------------------------------------

    @levelconfig.command(name="import")
    @guild_only
    @_require_manage_guild()
    async def lvlcfg_import(self, ctx: DiscoContext, mode: str | None = None) -> None:
        """Import levels from a CSV attachment.

        Pass `dryrun` (or `--dry-run`) to validate the file without writing
        anything to the database.  Attach a `.csv` file when running this
        command.  Accepted columns (case insensitive, any order):
          * user id:       user_id, id, discord_id, userid
          * level:         level, lvl
          * total XP:      xp, total_xp, experience
          * messages:      messages, total_messages          (optional)
          * streak:        streak_days, streak, daily_streak (optional)
          * last active:   last_active_date, last_active     (optional)
        """
        dry_run = False
        if mode is not None:
            flag = mode.strip().lower().lstrip("-")
            if flag in ("dryrun", "dry_run", "dry"):
                dry_run = True
            else:
                await ctx.reply_error(
                    "Unknown option.  Use `dryrun` to validate without writing, or omit for a real import."
                )
                return

        if not ctx.message.attachments:
            await ctx.reply_error_hint(
                "Attach a CSV file when running this command.",
                hint="levelconfig import (with a .csv attachment)",
                command_name="levelconfig import",
            )
            return

        attachment = ctx.message.attachments[0]
        if not attachment.filename.lower().endswith(".csv"):
            await ctx.reply_error("The attachment must be a `.csv` file.")
            return
        if attachment.size > 10 * 1024 * 1024:
            await ctx.reply_error("CSV file is larger than 10 MB.  Split it and import in chunks.")
            return

        try:
            csv_bytes = await attachment.read()
        except Exception as exc:
            await ctx.reply_error(f"Could not download the attachment: {exc}")
            return

        line_count = csv_bytes.count(b"\n")  # cheap estimate
        if dry_run:
            prompt = (
                f"**Dry run**: validate approximately **{line_count:,}** rows from "
                f"`{attachment.filename}` WITHOUT writing to `chat_levels`?"
            )
        else:
            prompt = (
                f"Import approximately **{line_count:,}** rows from `{attachment.filename}` "
                "into `chat_levels`?  Matching users will be overwritten."
            )
        ok = await ctx.confirm(prompt, timeout=60.0)
        if not ok:
            await ctx.reply_error("Import cancelled.")
            return

        cfg = await get_config(ctx.db, ctx.guild_id)
        header = "Dry run: parsing" if dry_run else "Importing chat levels..."
        status_msg = await ctx.send(
            embed=card(
                header,
                description=f"Parsing `{attachment.filename}`...",
                color=C_INFO,
            ).build()
        )

        last_edit = 0.0

        async def _progress(done: int, total: int) -> None:
            nonlocal last_edit
            now = asyncio.get_event_loop().time()
            if now - last_edit < 2.0:
                return
            last_edit = now
            try:
                await status_msg.edit(
                    embed=card(
                        header,
                        description=f"Processed **{done:,} / {total:,}** rows.",
                        color=C_INFO,
                    ).build()
                )
            except discord.HTTPException:
                pass

        try:
            result: ImportResult = await parse_and_import(
                ctx.db, ctx.guild_id, cfg, csv_bytes,
                progress_cb=_progress, dry_run=dry_run,
            )
        except Exception as exc:
            log.exception("CSV import failed gid=%s dry_run=%s", ctx.guild_id, dry_run)
            await status_msg.edit(
                embed=card(
                    "Import failed",
                    description=f"`{type(exc).__name__}: {exc}`",
                    color=C_ERROR,
                ).build()
            )
            return

        total = result.total_rows
        color = C_SUCCESS if result.errored == 0 else C_WARNING
        title = "Dry run finished" if dry_run else "Import finished"
        would_label = "Would import" if dry_run else "Imported"
        footer = (
            "No rows were written.  Re-run without `dryrun` to commit."
            if dry_run
            else "Users will keep their imported level; chatting adds XP from there."
        )
        embed = (
            card(title, color=color)
            .description(f"Source: `{attachment.filename}`")
            .field("Rows", f"{total:,}", True)
            .field(would_label, f"{result.imported:,}", True)
            .field("Skipped", f"{result.skipped:,}", True)
            .field("Errored", f"{result.errored:,}", True)
            .field_if(
                bool(result.first_errors),
                "First errors",
                "\n".join(result.first_errors[:5]),
                False,
            )
            .footer(footer)
            .build()
        )
        try:
            await status_msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send_embed(embed)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(ChatLevelingAdmin(bot))
