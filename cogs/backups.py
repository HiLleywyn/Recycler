"""cogs/backups.py -- create, restore, list and schedule server backups.

A backup is a full snapshot of a guild (settings, roles, categories,
channels, permission overwrites and optionally recent messages) stored as
one JSONB row. Restoring rebuilds that structure into the current server.
Everything is free; a generous per-user cap (env ``BACKUP_MAX_PER_USER``)
only guards against runaway storage.
"""
from __future__ import annotations

import re
import secrets

import discord
from discord.ext import commands, tasks

from clanklib.permissions import ModCog
from core.framework.components import Container, send_v2
from core.framework.context import DiscoContext
from core.framework.ui import C_ERROR, C_GOLD, C_INFO, C_SUCCESS, fmt_ts
from clanklib import serializer
from clanklib.settings import prefix as _prefix, setting_int
from cogs._ui import confirm_v2

_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _new_id() -> str:
    return secrets.token_hex(4)


class Backups(ModCog):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot)
        self._auto_backup_loop.start()

    def cog_unload(self) -> None:
        self._auto_backup_loop.cancel()

    # ── commands ──────────────────────────────────────────────────────────────

    @commands.group(name="backup", aliases=["backups", "bk"], invoke_without_command=True)
    async def backup(self, ctx: DiscoContext) -> None:
        await self.backup_list(ctx)

    @backup.command(name="create", aliases=["new", "save"])
    @commands.has_guild_permissions(administrator=True)
    @commands.bot_has_guild_permissions(administrator=True)
    async def backup_create(self, ctx: DiscoContext, *, options: str = "") -> None:
        """Create a full backup: settings, roles and their permissions,
        categories, channels with their permission overwrites, and recent
        messages per channel.

        A backup captures the whole server by default. To skip message
        archiving (a structure-only backup), add ``no-messages``. To capture
        more or fewer messages per channel, add ``messages:200``.
        """
        count = await self.db.backups.count_for_owner(ctx.author.id)
        max_per_user = setting_int(self.bot, "BACKUP_MAX_PER_USER", 50)
        if count >= max_per_user:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                f"## Backup limit reached\nYou have {count} backups (max "
                f"{max_per_user}). Delete one with `{self._p()}backup delete <id>`."))
            return

        # Messages are captured by default. ``no-messages`` makes it
        # structure-only; ``messages:N`` (or the legacy ``chatlog:N``) sets the
        # per-channel cap.
        opts_lower = options.lower()
        if "no-messages" in opts_lower or "no-chatlog" in opts_lower:
            msg_limit = 0
        else:
            m = re.search(r"(?:messages|chatlog):?(\d+)?", opts_lower)
            if m and m.group(1):
                msg_limit = min(int(m.group(1)), serializer.MAX_MESSAGE_LIMIT)
            else:
                msg_limit = serializer.DEFAULT_MESSAGE_LIMIT

        async with ctx.typing():
            data = await serializer.serialize_guild(
                ctx.guild, include_messages=msg_limit > 0, message_limit=msg_limit
            )
            bid = _new_id()
            await self.db.backups.create(
                backup_id=bid, owner_id=ctx.author.id, guild_id=ctx.guild.id,
                guild_name=ctx.guild.name, data=data,
                message_count=data.get("message_count", 0),
            )

        panel = (
            Container(accent_color=C_SUCCESS)
            .text("## Backup created")
            .text(f"**ID** `{bid}`\n"
                  f"**Roles** {len(data['roles'])} · "
                  f"**Categories** {len(data['categories'])} · "
                  f"**Channels** {len(data['channels'])}"
                  + (f" · **Messages** {data['message_count']}" if msg_limit
                     else " · structure only"))
            .separator()
            .text(f"-# Restore the whole thing with `{self._p()}backup load {bid}` "
                  f"(this overwrites the server).")
        )
        await send_v2(ctx, panel)

    @backup.command(name="load", aliases=["restore"])
    @commands.has_guild_permissions(administrator=True)
    @commands.bot_has_guild_permissions(administrator=True)
    async def backup_load(self, ctx: DiscoContext, backup_id: str, *, flags: str = "") -> None:
        """Restore the whole backup: roles and their permissions, categories,
        channels with their overwrites, server settings, and the archived
        messages. Add ``structure-only`` to skip replaying messages.
        """
        row = await self._owned(ctx, backup_id)
        if row is None:
            return
        ok = await confirm_v2(
            ctx,
            title="Load backup?",
            body=(f"This will **delete all current channels and roles** and rebuild "
                  f"`{ctx.guild.name}` from backup `{backup_id}` "
                  f"(taken {fmt_ts(row['created_at'])}), including roles, "
                  f"permissions, channels, and archived messages. This cannot "
                  f"be undone."),
        )
        if not ok:
            return

        # Full restore by default; ``structure-only`` / ``no-messages`` skips
        # the message replay.
        fl = flags.lower()
        opts = serializer.RestoreOptions(
            restore_messages=not ("structure-only" in fl or "no-messages" in fl),
        )

        # Surface permission problems up front rather than letting every
        # create_role / create_channel fail silently into the error list.
        me = ctx.guild.me
        if not me.guild_permissions.administrator and not (
            me.guild_permissions.manage_roles and me.guild_permissions.manage_channels
        ):
            await send_v2(ctx, Container(accent_color=C_ERROR)
                          .text("## Cannot restore")
                          .text("I need **Manage Roles** and **Manage Channels** to "
                                "rebuild the server. Run "
                                f"`{self._p()}setup` to see exactly what to grant."))
            return

        async with ctx.typing():
            stats = await serializer.restore_guild(ctx.guild, row["data"], opts)

        # The invoking channel may have been deleted; fall back to system channel.
        dest = ctx.channel if ctx.guild.get_channel(ctx.channel.id) else (
            ctx.guild.system_channel or next(iter(ctx.guild.text_channels), None)
        )
        if dest is None:
            return
        panel = (
            Container(accent_color=C_SUCCESS if not stats.errors else C_GOLD)
            .text("## Backup restored")
            .text(stats.summary())
        )
        if stats.errors:
            panel.separator()
            panel.text(f"**{len(stats.errors)} item(s) could not be restored** "
                       "(usually because the bot's role is not high enough). "
                       f"Run `{self._p()}setup` to check permissions.")
            panel.text("```\n" + "\n".join(stats.errors[:8])[:900] + "\n```")
        await send_v2(dest, panel)

    @backup.command(name="list", aliases=["ls"])
    async def backup_list(self, ctx: DiscoContext) -> None:
        rows = await self.db.backups.list_for_owner(ctx.author.id)
        if not rows:
            await send_v2(ctx, Container(accent_color=C_INFO).text(
                "## No backups yet").text(
                f"Create one with `{self._p()}backup create`."))
            return
        lines = [
            f"`{r['id']}` · **{r['guild_name']}** · {r.get('message_count', 0)} msgs · "
            f"{fmt_ts(r['created_at'])}"
            for r in rows
        ]
        panel = (
            Container(accent_color=C_INFO)
            .text(f"## Your backups ({len(rows)})")
            .text("\n".join(lines)[:3900])
        )
        await send_v2(ctx, panel)

    @backup.command(name="info")
    async def backup_info(self, ctx: DiscoContext, backup_id: str) -> None:
        row = await self._owned(ctx, backup_id)
        if row is None:
            return
        data = row["data"]
        panel = (
            Container(accent_color=C_INFO)
            .text(f"## Backup `{backup_id}`")
            .text(f"**Server** {row['guild_name']}\n"
                  f"**Created** {fmt_ts(row['created_at'])}\n"
                  f"**Roles** {len(data.get('roles', []))}\n"
                  f"**Categories** {len(data.get('categories', []))}\n"
                  f"**Channels** {len(data.get('channels', []))}\n"
                  f"**Messages** {row.get('message_count', 0)}")
        )
        await send_v2(ctx, panel)

    @backup.command(name="delete", aliases=["del", "rm"])
    async def backup_delete(self, ctx: DiscoContext, backup_id: str) -> None:
        deleted = await self.db.backups.delete(backup_id, ctx.author.id)
        color, text = (C_SUCCESS, f"Deleted backup `{backup_id}`.") if deleted else (
            C_ERROR, f"No backup `{backup_id}` owned by you.")
        await send_v2(ctx, Container(accent_color=color).text(text))

    @backup.command(name="interval", aliases=["auto"])
    @commands.has_guild_permissions(administrator=True)
    async def backup_interval(self, ctx: DiscoContext, hours: str = "", keep: int = 7) -> None:
        """Set automatic backups, e.g. ``backup interval 24`` (every 24h). Use
        ``backup interval off`` to stop."""
        if hours.lower() in ("off", "stop", "0", ""):
            cleared = await self.db.backups.clear_interval(ctx.guild.id)
            await send_v2(ctx, Container(accent_color=C_INFO).text(
                "Automatic backups disabled." if cleared else "No schedule was set."))
            return
        if not hours.isdigit() or int(hours) < 1:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "Give a whole number of hours, e.g. `backup interval 24`."))
            return
        await self.db.backups.set_interval(
            guild_id=ctx.guild.id, owner_id=ctx.author.id,
            interval_hours=int(hours), keep=max(1, int(keep)),
        )
        await send_v2(ctx, Container(accent_color=C_SUCCESS).text(
            "## Automatic backups on").text(
            f"Every **{int(hours)}h**, keeping the newest **{max(1, int(keep))}**."))

    # ── auto-backup task ────────────────────────────────────────────────────

    @tasks.loop(minutes=15)
    async def _auto_backup_loop(self) -> None:
        try:
            due = await self.db.backups.due_intervals()
        except Exception:  # noqa: BLE001 - DB may be mid-restart
            return
        for sched in due:
            guild = self.bot.get_guild(int(sched["guild_id"]))
            if guild is None:
                continue
            try:
                data = await serializer.serialize_guild(guild)
                await self.db.backups.create(
                    backup_id=_new_id(), owner_id=int(sched["owner_id"]),
                    guild_id=guild.id, guild_name=guild.name, data=data,
                )
                await self.db.backups.prune_oldest(guild.id, int(sched.get("keep", 7)))
                await self.db.backups.mark_interval_ran(guild.id)
                self.log.info("auto-backup for guild %s", guild.id)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("auto-backup failed for %s: %s", sched["guild_id"], exc)

    @_auto_backup_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _p(self) -> str:
        return _prefix(self.bot)

    async def _owned(self, ctx: DiscoContext, backup_id: str):  # type: ignore[no-untyped-def]
        if not _ID_RE.match(backup_id.lower()):
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "That doesn't look like a backup id (8 hex characters)."))
            return None
        row = await self.db.backups.get(backup_id.lower())
        if row is None or int(row["owner_id"]) != ctx.author.id:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                f"No backup `{backup_id}` owned by you."))
            return None
        return row


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Backups(bot))
