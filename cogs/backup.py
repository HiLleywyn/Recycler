"""cogs/backup.py  -  Scheduled PostgreSQL backups with admin restore commands.

Backups are stored in /data/backups/ using pg_dump, which creates
a fully consistent SQL dump of the live database without blocking writes.

Schedule: every BACKUP_INTERVAL_HOURS hours (default 6).
Retention: keeps the last BACKUP_KEEP snapshots (default 7), deletes older ones.

Admin commands:
  .admin backup create        -  trigger a manual backup now
  .admin backup list          -  show existing backups with timestamps and sizes
  .admin backup restore <fn>  -  copy a backup over the live DB and restart the bot
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.fuzzy import suggest_subcommand
from core.framework.middleware import guild_only
from core.framework.heartbeat import pulse, register_interval
from core.framework.ui import C_BLURPLE, C_ERROR, fmt_ts

log = logging.getLogger(__name__)

_BACKUP_DIR = "/data/backups"


def _backup_path(ts: float | None = None) -> str:
    t = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return os.path.join(_BACKUP_DIR, f"discoin_{t.strftime('%Y%m%d_%H%M%S')}.sql")


def _list_backups() -> list[str]:
    """Return backup filenames sorted oldest-first."""
    try:
        files = [
            f for f in os.listdir(_BACKUP_DIR)
            if f.startswith("discoin_") and f.endswith(".sql")
        ]
        return sorted(files)
    except FileNotFoundError:
        return []


def _require_manage_guild():
    async def predicate(ctx: DiscoContext) -> bool:
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.reply_error("You need **Manage Guild** to use this command.")
            return False
        return True
    return commands.check(predicate)


class Backup(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        self.scheduled_backup.start()
        register_interval("backup", Config.BACKUP_INTERVAL_HOURS * 3600)

    def cog_unload(self) -> None:
        self.scheduled_backup.cancel()

    # ── Scheduled backup loop ─────────────────────────────────────────────────

    @tasks.loop(hours=Config.BACKUP_INTERVAL_HOURS)
    async def scheduled_backup(self) -> None:
        try:
            await self._do_backup()
        except Exception as exc:
            log.error("Scheduled backup failed: %s", exc)
        pulse("backup")

    @scheduled_backup.before_loop
    async def _before_backup(self) -> None:
        await self.bot.wait_until_ready()

    async def _do_backup(self) -> str:
        """Create a pg_dump backup. Returns the backup file path."""
        import asyncio
        from urllib.parse import urlparse
        dest = _backup_path()
        dsn = Config.DATABASE_URL

        # Parse the DSN so we can pass credentials via env vars, which pg_dump
        # always honours (URL-embedded passwords are ignored by some versions).
        parsed = urlparse(dsn)
        env = {**os.environ}
        if parsed.hostname:
            env["PGHOST"] = parsed.hostname
        if parsed.port:
            env["PGPORT"] = str(parsed.port)
        if parsed.username:
            env["PGUSER"] = parsed.username
        if parsed.password:
            env["PGPASSWORD"] = parsed.password
        dbname = (parsed.path or "").lstrip("/") or parsed.hostname or "discoin"

        proc = await asyncio.create_subprocess_exec(
            "pg_dump", dbname, "-f", dest, "--no-owner", "--no-acl",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            # pg_dump sometimes writes errors to stdout; include both streams.
            detail = (stderr.decode().strip() or stdout.decode().strip())[:400]
            raise RuntimeError(f"pg_dump failed: {detail}")
        log.info("Backup created: %s", dest)
        self._prune_old_backups()
        return dest

    def _prune_old_backups(self) -> None:
        """Delete oldest backups if we exceed BACKUP_KEEP or BACKUP_MAX_AGE_DAYS."""
        files = _list_backups()
        # 1. Age-based pruning
        max_age_days = getattr(Config, "BACKUP_MAX_AGE_DAYS", 0)
        if max_age_days > 0:
            cutoff = time.time() - (max_age_days * 86400)
            for fname in list(files):
                fpath = os.path.join(_BACKUP_DIR, fname)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        files.remove(fname)
                        log.info("Pruned aged backup: %s (older than %d days)", fname, max_age_days)
                except OSError as exc:
                    log.warning("Could not prune backup %s: %s", fname, exc)

        # 2. Count-based pruning
        while len(files) > Config.BACKUP_KEEP:
            oldest = os.path.join(_BACKUP_DIR, files.pop(0))
            try:
                os.remove(oldest)
                log.info("Pruned old backup: %s", oldest)
            except OSError as exc:
                log.warning("Could not prune backup %s: %s", oldest, exc)

    # ── Admin commands ─────────────────────────────────────────────────────────

    @commands.group(name="backup", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def backup_cmd(self, ctx: DiscoContext) -> None:
        """Database backup commands. Usage: .backup <create|list|restore>"""
        if await suggest_subcommand(ctx, self.backup_cmd):
            return
        await ctx.send_help(ctx.command)

    @backup_cmd.command(name="create")
    @guild_only
    @_require_manage_guild()
    async def backup_create(self, ctx: DiscoContext) -> None:
        """Manually trigger a database backup now."""
        async with ctx.typing():
            try:
                dest = await self._do_backup()
                size = os.path.getsize(dest)
                fname = os.path.basename(dest)
                await ctx.reply_success(
                    f"Backup created: `{fname}` ({size / 1024 / 1024:.2f} MB)",
                    title="✅ Backup Created",
                )
            except Exception as exc:
                log.error("Manual backup failed: %s", exc)
                await ctx.reply_error(f"Backup failed: `{exc}`")

    @backup_cmd.command(name="list", aliases=["ls"])
    @guild_only
    @_require_manage_guild()
    async def backup_list(self, ctx: DiscoContext) -> None:
        """List all existing database backups."""
        files = _list_backups()
        if not files:
            await ctx.reply_error("No backups found in `/data/backups/`.")
            return

        lines = []
        for fname in reversed(files):  # newest first
            fpath = os.path.join(_BACKUP_DIR, fname)
            try:
                size_mb = os.path.getsize(fpath) / 1024 / 1024
                mtime = os.path.getmtime(fpath)
                ts = fmt_ts(mtime, "%Y-%m-%d %H:%M UTC")
                lines.append(f"`{fname}`  -  {size_mb:.2f} MB  -  {ts}")
            except OSError:
                lines.append(f"`{fname}`  -  (unreadable)")

        embed = (
            card("📦 Database Backups", description="\n".join(lines), color=C_BLURPLE)
            .footer(f"Stored in /data/backups/  •  Keeping last {Config.BACKUP_KEEP}")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @backup_cmd.command(name="restore")
    @guild_only
    @_require_manage_guild()
    async def backup_restore(self, ctx: DiscoContext, filename: str) -> None:
        """Restore the database from a backup file and restart the bot.
        Usage: .backup restore <filename>
        The bot will restart automatically after the restore."""
        files = _list_backups()
        if filename not in files:
            await ctx.reply_error(
                f"Backup `{filename}` not found. Use `.backup list` to see available backups."
            )
            return

        src = os.path.join(_BACKUP_DIR, filename)

        # Two-step confirmation: user must type the filename back
        confirm_embed = card(
            "⚠️ Restore Confirmation",
            description=(
                f"This will **overwrite the live database** with `{filename}` and **restart the bot**.\n\n"
                f"All data created after this backup was made will be **permanently lost**.\n\n"
                f"Type the filename below to confirm, or anything else to cancel.\n"
                f"You have 30 seconds."
            ),
            color=C_ERROR,
        ).build()
        await ctx.reply(embed=confirm_embed, mention_author=False)

        def check(m: discord.Message) -> bool:
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

        try:
            import asyncio
            msg = await self.bot.wait_for("message", check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.reply_error("Restore cancelled  -  timed out.")
            return

        if msg.content.strip() != filename:
            await ctx.reply_error("Restore cancelled  -  filename did not match.")
            return

        # Perform restore via psql
        try:
            import asyncio as _aio
            dsn = Config.DATABASE_URL
            proc = await _aio.create_subprocess_exec(
                "psql", dsn, "-f", src,
                stdout=_aio.subprocess.PIPE,
                stderr=_aio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode()[:300])
            await ctx.reply_success(
                f"Database restored from `{filename}`. Restarting now…",
                title="Restore Complete",
            )
            log.warning("Database restored from %s by %s  -  restarting", filename, ctx.author)
        except Exception as exc:
            log.error("Restore failed: %s", exc)
            await ctx.reply_error(f"Restore failed: `{exc}`")
            return

        # Restart: close the bot so the process supervisor (Docker) restarts it
        await self.bot.close()

    @backup_cmd.command(name="delete", aliases=["rm", "del"])
    @guild_only
    @_require_manage_guild()
    async def backup_delete(self, ctx: DiscoContext, filename: str) -> None:
        """Delete a specific backup file. Usage: .backup delete <filename>"""
        files = _list_backups()
        if filename not in files:
            await ctx.reply_error(
                f"Backup `{filename}` not found. Use `.backup list` to see available backups."
            )
            return
        fpath = os.path.join(_BACKUP_DIR, filename)
        try:
            os.remove(fpath)
            await ctx.reply_success(f"Deleted backup: `{filename}`")
        except OSError as exc:
            await ctx.reply_error(f"Failed to delete: `{exc}`")

    @backup_cmd.command(name="clear", aliases=["prune", "cleanup"])
    @guild_only
    @_require_manage_guild()
    async def backup_clear(self, ctx: DiscoContext, keep: int = 1) -> None:
        """Delete old backups, keeping the most recent N. Usage: .backup clear [keep=1]"""
        keep = max(0, keep)
        files = _list_backups()
        if not files:
            await ctx.reply_error("No backups to clear.")
            return
        # files is sorted oldest→newest; reverse to get newest first
        sorted_newest = list(reversed(files))
        to_delete = sorted_newest[keep:]
        if not to_delete:
            await ctx.reply_success(f"Nothing to clear  -  only {len(files)} backup(s), keeping {keep}.")
            return

        deleted = 0
        for fname in to_delete:
            try:
                os.remove(os.path.join(_BACKUP_DIR, fname))
                deleted += 1
            except OSError:
                pass
        await ctx.reply_success(
            f"Cleared **{deleted}** old backup(s). Kept the **{len(files) - deleted}** most recent.",
            title="🗑 Backups Cleared",
        )


    @backup_cmd.command(name="keep")
    @guild_only
    @_require_manage_guild()
    async def backup_keep(self, ctx: DiscoContext, count: int) -> None:
        """Set how many backups to keep (count-based retention).
        Usage: .backup keep <number>
        Default: 7. Older backups are auto-deleted after each scheduled backup."""
        if count < 1:
            await ctx.reply_error("Must keep at least 1 backup.")
            return
        if count > 100:
            await ctx.reply_error("Maximum 100 backups.")
            return
        Config.BACKUP_KEEP = count
        self._prune_old_backups()
        await ctx.reply_success(
            f"Backup retention set to **{count}** snapshots. "
            f"Older backups will be auto-deleted.",
            title="✅ Retention Updated",
        )

    @backup_cmd.command(name="maxage", aliases=["retention", "autodelete"])
    @guild_only
    @_require_manage_guild()
    async def backup_maxage(self, ctx: DiscoContext, days: int) -> None:
        """Set max age for backups in days (age-based retention).
        Usage: .backup maxage <days>
        Set to 0 to disable age-based deletion (only count-based).
        Example: .backup maxage 30   -  auto-delete backups older than 30 days."""
        if days < 0:
            await ctx.reply_error("Days must be 0 or positive.")
            return
        if days > 365:
            await ctx.reply_error("Maximum 365 days.")
            return
        Config.BACKUP_MAX_AGE_DAYS = days
        if days > 0:
            self._prune_old_backups()
            await ctx.reply_success(
                f"Backups older than **{days} days** will be auto-deleted.\n"
                f"Count-based retention: keep last **{Config.BACKUP_KEEP}**.",
                title="✅ Auto-Delete Updated",
            )
        else:
            await ctx.reply_success(
                f"Age-based auto-delete **disabled**.\n"
                f"Count-based retention: keep last **{Config.BACKUP_KEEP}**.",
                title="✅ Auto-Delete Disabled",
            )

    @backup_cmd.command(name="settings", aliases=["config", "status"])
    @guild_only
    @_require_manage_guild()
    async def backup_settings(self, ctx: DiscoContext) -> None:
        """Show current backup retention settings."""
        files = _list_backups()
        max_age = getattr(Config, "BACKUP_MAX_AGE_DAYS", 0)
        total_size = 0
        for f in files:
            try:
                total_size += os.path.getsize(os.path.join(_BACKUP_DIR, f))
            except OSError:
                pass

        embed = (
            card("⚙️ Backup Settings", color=C_BLURPLE)
            .field("Interval", f"Every **{Config.BACKUP_INTERVAL_HOURS}** hours", True)
            .field("Keep Count", f"**{Config.BACKUP_KEEP}** snapshots", True)
            .field("Max Age", f"**{max_age}** days" if max_age > 0 else "Disabled", True)
            .field("Current Backups", f"**{len(files)}** ({total_size / 1024 / 1024:.1f} MB)", True)
            .field("Storage Path", f"`{_BACKUP_DIR}`", True)
            .footer("Use .backup keep <n> or .backup maxage <days> to configure")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Backup(bot))
