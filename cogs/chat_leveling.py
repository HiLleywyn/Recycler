"""
cogs/chat_leveling.py  -  User-facing chat leveling commands and XP listener.

Every non-command message awards a random XP roll in the guild's configured
band, with a per-user cooldown and minimum-length filter to discourage spam.
On level-up the bot sends a card in the configured announce channel (or the
originating channel if none is set), syncs level-gated role rewards, and
optionally DMs the user.
"""
from __future__ import annotations

import logging
import random
import time

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only
from core.framework.ui import (
    C_GOLD, C_INFO, C_NAVY, C_PURPLE, C_SUCCESS, fmt_ts, send_paginated,
)
from services.chat_leveling import (
    LevelingConfig,
    add_xp,
    apply_streak,
    get_config,
    get_leaderboard,
    get_ranks,
    get_role_rewards,
    get_user,
    get_user_rank,
    rank_for_level,
    sync_member_roles,
    total_xp_for_level,
    xp_for_level_up,
)

log = logging.getLogger(__name__)

# Messages starting with any of these are treated as commands for another
# bot or this bot's command parser and skipped by the XP listener.
_COMMAND_PREFIXES = (",", ".", "/", "$", "!", "?")

# Per-process cooldown map: (guild_id, user_id) -> unix seconds of last grant.
_last_xp: dict[tuple[int, int], float] = {}


def _progress_bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "-" * width
    pct = max(0.0, min(1.0, done / total))
    filled = int(round(pct * width))
    return "#" * filled + "-" * (width - filled)


def _cooldown_ok(gid: int, uid: int, window: int) -> bool:
    now = time.time()
    key = (gid, uid)
    last = _last_xp.get(key, 0.0)
    if now - last < window:
        return False
    _last_xp[key] = now
    return True


class ChatLeveling(commands.Cog):
    """XP listener plus user-facing ,level commands."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # -- Listener ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        if message.webhook_id:
            return
        if message.type != discord.MessageType.default and message.type != discord.MessageType.reply:
            return

        content = (message.content or "").strip()
        if not content:
            return
        if content.startswith(_COMMAND_PREFIXES):
            return

        gid = message.guild.id
        uid = message.author.id

        clanktank = self.bot.get_cog("Clanktank")
        if clanktank and await clanktank.is_clanker(uid, gid):
            return

        try:
            cfg = await get_config(self.bot.db, gid)
        except Exception:
            log.debug("on_message: get_config failed gid=%s", gid, exc_info=True)
            return
        if not cfg.enabled:
            return
        if len(content) < cfg.min_chars:
            return
        if not _cooldown_ok(gid, uid, cfg.cooldown_seconds):
            return

        lo = min(cfg.xp_min, cfg.xp_max)
        hi = max(cfg.xp_min, cfg.xp_max)
        amount = random.randint(max(0, lo), max(0, hi))
        if amount <= 0:
            return

        try:
            _streak_days, _mult = await apply_streak(self.bot.db, gid, uid, cfg)
        except Exception:
            log.debug("on_message: apply_streak failed gid=%s uid=%s", gid, uid, exc_info=True)
            _mult = 1.0
        if _mult > 1.0:
            amount = max(1, int(round(amount * _mult)))

        try:
            from services.buddy_bonus import buddy_bonus
            _buddy_mult = await buddy_bonus(self.bot.db, gid, uid, lane="chat")
            if _buddy_mult > 1.0:
                amount = max(1, int(round(amount * _buddy_mult)))
        except Exception:
            log.debug("on_message: buddy_bonus failed gid=%s uid=%s", gid, uid, exc_info=True)

        author = message.author
        stored_name = author.display_name if isinstance(author, (discord.Member, discord.User)) else None
        try:
            old_level, new_level, new_xp, _msgs = await add_xp(
                self.bot.db, gid, uid, amount, cfg,
                display_name=stored_name,
            )
        except Exception:
            log.debug("on_message: add_xp failed gid=%s uid=%s", gid, uid, exc_info=True)
            return

        if new_level > old_level:
            await self._level_up_flow(message, cfg, new_level)
            try:
                await self.bot.bus.publish(
                    "chat_level_up",
                    guild=message.guild, user=author,
                    old_level=old_level, new_level=new_level,
                )
            except Exception:
                log.debug("chat_level_up publish failed gid=%s uid=%s", gid, uid, exc_info=True)

    async def _level_up_flow(
        self, message: discord.Message, cfg: LevelingConfig, new_level: int,
    ) -> None:
        guild = message.guild
        member = message.author
        if guild is None or not isinstance(member, discord.Member):
            return

        # Role rewards + rank title.
        try:
            rewards = await get_role_rewards(self.bot.db, guild.id)
        except Exception:
            rewards = []
        try:
            ranks = await get_ranks(self.bot.db, guild.id)
        except Exception:
            ranks = []

        added_ids: list[int] = []
        if rewards:
            try:
                added_ids, _ = await sync_member_roles(
                    member, new_level, rewards, cfg.stack_roles,
                )
            except Exception:
                log.debug("_level_up_flow: sync_member_roles failed gid=%s uid=%s",
                          guild.id, member.id, exc_info=True)

        rank_name = rank_for_level(new_level, ranks)

        added_role_names: list[str] = []
        for rid in added_ids:
            r = guild.get_role(rid)
            if r is not None:
                added_role_names.append(r.name)

        embed = (
            card(f"Level up! {member.display_name}", color=C_GOLD)
            .description(f"{member.mention} reached **level {new_level}**!")
            .field("New Level", f"**{new_level}**", True)
            .field_if(rank_name is not None, "Rank", f"**{rank_name}**" if rank_name else "", True)
            .field_if(
                bool(added_role_names),
                "Roles Unlocked",
                ", ".join(added_role_names) if added_role_names else "",
                False,
            )
            .footer(f"Keep chatting to earn more XP  -  {fmt_ts(time.time())}")
            .build()
        )

        target_channel: discord.abc.Messageable | None = message.channel
        if cfg.announce_channel:
            ch = guild.get_channel(cfg.announce_channel)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                target_channel = ch

        try:
            await target_channel.send(embed=embed)
        except discord.HTTPException:
            log.debug("_level_up_flow: send failed gid=%s uid=%s", guild.id, member.id)

        if cfg.dm_levelup:
            try:
                await member.send(
                    embed=card(
                        f"Level up in {guild.name}",
                        description=f"You reached **level {new_level}**!",
                        color=C_GOLD,
                    )
                    .field_if(rank_name is not None, "Rank", str(rank_name or ""), True)
                    .build()
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    # -- ,level group --------------------------------------------------------

    @commands.group(name="level", invoke_without_command=True)
    @guild_only
    async def level(self, ctx: DiscoContext, member: discord.Member | None = None) -> None:
        """Show your level card, or another member's.  Alias for ,level rank."""
        await self._show_rank(ctx, member)

    @level.command(name="rank")
    @guild_only
    async def level_rank(self, ctx: DiscoContext, member: discord.Member | None = None) -> None:
        """Show your current level, rank title, and progress to next level."""
        await self._show_rank(ctx, member)

    @commands.command(name="rank")
    @guild_only
    async def rank_shortcut(self, ctx: DiscoContext, member: discord.Member | None = None) -> None:
        """Shortcut for ,level rank -- your level card, or another member's."""
        await self._show_rank(ctx, member)

    async def _show_rank(self, ctx: DiscoContext, member: discord.Member | None) -> None:
        target = member or ctx.author
        gid = ctx.guild_id

        cfg = await get_config(ctx.db, gid)
        row = await get_user(ctx.db, gid, target.id)
        total_xp = int(row.get("xp") or 0)
        messages = int(row.get("total_messages") or 0)
        streak_days = int(row.get("streak_days") or 0)
        # Stored level is authoritative -- rank and leaderboard must always
        # show the same number. Admins realign stored levels with a changed
        # curve via ,levelconfig recompute.
        level = int(row.get("level") or 0)
        floor = total_xp_for_level(level, cfg)
        needed = xp_for_level_up(level, cfg)
        into = max(0, total_xp - floor)
        ranks = await get_ranks(ctx.db, gid)
        rank_name = rank_for_level(level, ranks)
        # Fall back to the highest reward role the user has earned when the
        # server hasn't configured rank titles. Gives ",rank" something
        # meaningful to show out of the box.
        if rank_name is None:
            rewards = await get_role_rewards(ctx.db, gid)
            earned = [(lvl, rid) for lvl, rid in rewards if lvl <= level]
            if earned:
                top_lvl, top_rid = max(earned, key=lambda p: p[0])
                role = ctx.guild.get_role(top_rid) if ctx.guild else None
                rank_name = role.name if role else f"Lvl {top_lvl} role"
        position = await get_user_rank(ctx.db, gid, target.id)

        # V3: Pillow-rendered level card. The renderer reads the player's
        # equipped cosmetics so this card visually matches ,profile.
        try:
            from services import cosmetics as _cos
            from services.level_render import render_level_card
            avatar_bytes: bytes | None = None
            if target.display_avatar:
                try:
                    avatar_bytes = await target.display_avatar.read()
                except Exception:
                    pass
            equipped = await _cos.equipped(ctx.db, target.id)
            png = render_level_card(
                user_name=target.display_name,
                avatar_bytes=avatar_bytes,
                level=level,
                rank_name=rank_name,
                total_xp=total_xp,
                level_floor_xp=floor,
                level_next_xp=floor + needed,
                messages=messages,
                streak_days=streak_days,
                position=position,
                equipped=equipped,
            )
            import io as _io
            file = discord.File(_io.BytesIO(png), filename="level.png")
            embed = (
                card(f"{target.display_name}'s Level", color=C_PURPLE)
                .description(
                    f"Level **{level}**  -  "
                    f"{into:,} / {needed:,} XP to next  -  "
                    + (f"#**{position}** on the leaderboard"
                       if position is not None else "Unranked")
                )
                .image("attachment://level.png")
                .build()
            )
            await ctx.reply(embed=embed, file=file, mention_author=False)
            return
        except Exception:
            log.debug(
                "level: PNG render failed, falling back to embed",
                exc_info=True,
            )

        # Fallback embed (used if the PNG renderer fails for any reason)
        bar = _progress_bar(into, needed)
        embed = (
            card(f"{target.display_name}'s Level", color=C_PURPLE)
            .field("Level", f"**{level}**", True)
            .field("Rank", f"**{rank_name}**" if rank_name else "-", True)
            .field(
                "Leaderboard",
                f"#**{position}**" if position is not None else "Unranked",
                True,
            )
            .field(
                "Progress",
                f"`{bar}`  {into:,} / {needed:,} XP",
                False,
            )
            .field("Total XP", f"{total_xp:,}", True)
            .field("Messages", f"{messages:,}", True)
            .field("Streak", f"{streak_days} day{'s' if streak_days != 1 else ''}", True)
            .build()
        )
        if target.display_avatar:
            embed.set_thumbnail(url=target.display_avatar.url)
        await ctx.send_embed(embed)

    async def _resolve_name(
        self, ctx: DiscoContext, uid: int, cached: str | None = None,
    ) -> str:
        """Best-effort display name for a user id.

        Priority:
          1. current guild member cache (fresh guild nickname),
          2. the display_name we cached on the chat_levels row at import
             time or the last XP grant,
          3. the bot's global user cache,
          4. the literal ``User {uid}`` string.

        Never hits the network per call -- the leaderboard pre-warms the
        member cache with a single bulk query_members before iterating.
        A per-row fetch_member / fetch_user would send one HTTP request
        per leaderboard entry and stall the command.
        """
        if ctx.guild:
            m = ctx.guild.get_member(uid)
            if m is not None:
                return m.display_name
        if cached:
            return cached
        u = self.bot.get_user(uid)
        if u is not None:
            return u.display_name if hasattr(u, "display_name") else u.name
        return f"User {uid}"

    _LB_MEDALS = ["\U0001f947", "\U0001f948", "\U0001f949"]
    _LB_RANK_TITLES = ["Chatterbox", "Runner-up", "Third Place"]
    _LB_RANK_BARS = ["\u2588" * 20, "\u2588" * 18 + "\u2591" * 2, "\u2588" * 16 + "\u2591" * 4]
    _LB_PER_PAGE = 10
    # Over-fetch so filtering out members who left the guild still leaves
    # enough rows to fill the leaderboard pages.
    _LB_FETCH_LIMIT = 500
    _LB_MAX_DISPLAY = 100

    def _lb_row(self, rank: int, name: str, lvl: int, xp: int, msgs: int, is_caller: bool) -> str:
        """Format one leaderboard row. Ranks 0-2 get medal treatment."""
        you = "  \u25c4 **you**" if is_caller else ""
        stats = f"Lvl **{lvl}**  -  {xp:,} XP  -  {msgs:,} msgs"
        if rank == 0:
            return (
                f"{self._LB_MEDALS[0]} **{name}**  -  {stats}{you}\n"
                f"\u2003`{self._LB_RANK_BARS[0]}` *{self._LB_RANK_TITLES[0]}*"
            )
        if rank < 3:
            return f"{self._LB_MEDALS[rank]} **{name}**  -  {stats}{you}"
        return f"`#{rank + 1:>2}`  **{name}**  -  {stats}{you}"

    @level.command(name="leaderboard", aliases=["lb", "top"])
    @guild_only
    async def level_leaderboard(self, ctx: DiscoContext) -> None:
        """Top chat-XP earners in the server."""
        await self.render_leaderboard(ctx)

    async def render_leaderboard(self, ctx: DiscoContext) -> None:
        """Build and send the paginated chat-XP leaderboard. Public entry
        point for the bank cog's ,lb dispatcher."""
        gid = ctx.guild_id
        rows = await get_leaderboard(ctx.db, gid, limit=self._LB_FETCH_LIMIT, offset=0)
        if not rows:
            await ctx.reply_error("No one has earned any chat XP yet.")
            return

        caller_id = ctx.author.id if ctx.author else 0

        # Warm the member cache with ONE gateway op so get_member hits
        # ctx.guild.get_member instead of falling through to "User {uid}".
        # We deliberately don't call guild.chunk() here -- for large guilds
        # that can block the whole command for 20+ seconds, which looks like
        # ",lb level" is broken. query_members targets just the leaderboard
        # IDs and returns in a single round-trip.
        uids = [int(r["user_id"]) for r in rows]
        if ctx.guild is not None:
            missing = [uid for uid in uids if ctx.guild.get_member(uid) is None]
            # Discord gateway caps user_ids at 100 per request.
            for j in range(0, len(missing), 100):
                batch = missing[j : j + 100]
                try:
                    await ctx.guild.query_members(
                        user_ids=batch, limit=len(batch), cache=True,
                    )
                except Exception as e:
                    log.warning(
                        "leaderboard: query_members failed gid=%s n=%d: %s",
                        gid, len(batch), e,
                    )

        # Drop rows whose user isn't in the guild anymore. Old bot exports
        # include ex-members (and rows with corrupted IDs from before the
        # CSV float-precision fix shipped) -- both render as anonymous
        # "User {id}" entries on the leaderboard, which is ugly and
        # confusing. Gate membership on the guild member cache we just
        # warmed -- anyone we couldn't resolve then is either gone, or the
        # id itself is invalid.
        bot_user = getattr(getattr(ctx, "bot", None), "user", None)
        bot_id = int(bot_user.id) if bot_user is not None else 0
        entries: list[tuple[int, str, int, int, int]] = []
        if ctx.guild is not None:
            for r in rows:
                uid = int(r["user_id"])
                # Drop placeholder / bot / left-guild rows in one pass.
                if uid <= 0 or (bot_id and uid == bot_id):
                    continue
                member = ctx.guild.get_member(uid)
                if member is None or getattr(member, "bot", False):
                    continue
                lvl = int(r["level"] or 0)
                if lvl <= 0 and int(r["xp"] or 0) <= 0:
                    continue
                xp_val = int(r["xp"] or 0)
                msgs = int(r["total_messages"] or 0)
                entries.append((uid, member.display_name, lvl, xp_val, msgs))
                if len(entries) >= self._LB_MAX_DISPLAY:
                    break

        if not entries:
            await ctx.reply_error(
                "No ranked members are in this server right now.  "
                "If you just imported levels, wait a moment for the member cache to warm up and try again."
            )
            return

        # Caller rank within the filtered list (positions on-screen).
        caller_rank: int | None = None
        for idx, (uid, _, _, _, _) in enumerate(entries):
            if uid == caller_id:
                caller_rank = idx + 1
                break

        total_pages = max(1, (len(entries) + self._LB_PER_PAGE - 1) // self._LB_PER_PAGE)
        base_footer = f"{ctx.guild.name}  -  Sorted by total XP"
        if caller_rank is not None:
            base_footer += f"  -  \U0001f4cd Your rank: #{caller_rank}"
        else:
            base_footer += "  -  \U0001f4cd You are not ranked yet"

        pages: list[discord.Embed] = []
        for pi in range(total_pages):
            start = pi * self._LB_PER_PAGE
            chunk = entries[start:start + self._LB_PER_PAGE]
            lines: list[str] = []
            for offset, (uid, name, lvl, xp_val, msgs) in enumerate(chunk):
                rank = start + offset
                lines.append(self._lb_row(
                    rank, name, lvl, xp_val, msgs, is_caller=(uid == caller_id),
                ))
            embed = (
                card(
                    f"\U0001f4ac Chat XP Leaderboard  -  {ctx.guild.name}",
                    color=C_GOLD if pi == 0 else C_NAVY,
                )
                .description("\n".join(lines))
                .footer(f"{base_footer}  -  Page {pi + 1}/{total_pages}")
            )
            if pi == 0 and ctx.guild and ctx.guild.icon:
                embed = embed.thumbnail(ctx.guild.icon.url)
            pages.append(embed.build())

        await send_paginated(ctx, pages)

    @level.command(name="ranks")
    @guild_only
    async def level_ranks(self, ctx: DiscoContext) -> None:
        """List all configured rank titles."""
        ranks = await get_ranks(ctx.db, ctx.guild_id)
        if not ranks:
            await ctx.reply_error("No rank titles have been configured.")
            return
        lines = [f"**Lvl {lvl}**  -  {name}" for lvl, name in ranks]
        embed = (
            card("Rank Titles", color=C_INFO)
            .description("\n".join(lines))
            .build()
        )
        await ctx.send_embed(embed)

    @level.command(name="roles")
    @guild_only
    async def level_roles(self, ctx: DiscoContext) -> None:
        """List all configured role rewards."""
        rewards = await get_role_rewards(ctx.db, ctx.guild_id)
        if not rewards:
            await ctx.reply_error("No role rewards have been configured.")
            return
        lines: list[str] = []
        for lvl, rid in rewards:
            role = ctx.guild.get_role(rid) if ctx.guild else None
            target = role.mention if role else f"`unknown role {rid}`"
            lines.append(f"**Lvl {lvl}**  -  {target}")
        embed = (
            card("Role Rewards", color=C_SUCCESS)
            .description("\n".join(lines))
            .build()
        )
        await ctx.send_embed(embed)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(ChatLeveling(bot))
