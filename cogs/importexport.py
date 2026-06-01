"""cogs/importexport.py -- move backups in and out of the bot as JSON files."""
from __future__ import annotations

import io
import json
import re
import secrets

import discord
from discord.ext import commands

from clanklib.permissions import ModCog
from core.framework.components import Container, send_v2
from core.framework.context import DiscoContext
from core.framework.ui import C_ERROR, C_INFO, C_SUCCESS
from clanklib import serializer
from clanklib.settings import prefix as _prefix

_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_MAX_IMPORT_BYTES = 8 * 1024 * 1024  # 8 MB


class ImportExport(ModCog):
    @commands.command(name="export")
    async def export_cmd(self, ctx: DiscoContext, backup_id: str) -> None:
        """Download a backup as a portable JSON file."""
        if not _ID_RE.match(backup_id.lower()):
            await send_v2(ctx, Container(accent_color=C_ERROR).text("Invalid backup id."))
            return
        row = await self.db.backups.get(backup_id.lower())
        if row is None or int(row["owner_id"]) != ctx.author.id:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                f"No backup `{backup_id}` owned by you."))
            return
        payload = {
            "format": "recycler.backup",
            "version": serializer.SCHEMA_VERSION,
            "guild_name": row["guild_name"],
            "data": row["data"],
        }
        buf = io.BytesIO(json.dumps(payload, indent=2).encode("utf-8"))
        file = discord.File(buf, filename=f"backup-{backup_id}.json")
        await ctx.send(
            content=f"Backup `{backup_id}` ({row['guild_name']}).",
            file=file,
        )

    @commands.command(name="import")
    @commands.has_guild_permissions(administrator=True)
    async def import_cmd(self, ctx: DiscoContext) -> None:
        """Import a backup from an attached JSON file (creates a new backup)."""
        if not ctx.message.attachments:
            await send_v2(ctx, Container(accent_color=C_INFO)
                          .text("## Import a backup")
                          .text("Attach a `.json` file exported with "
                                f"`{self._p()}export` to this command."))
            return
        att = ctx.message.attachments[0]
        if att.size > _MAX_IMPORT_BYTES:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "That file is too large to import (max 8 MB)."))
            return
        try:
            raw = await att.read()
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "That file isn't valid JSON."))
            return

        data = payload.get("data", payload)
        if not isinstance(data, dict) or "channels" not in data or "roles" not in data:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "That doesn't look like a Recycler backup (missing roles/channels)."))
            return

        bid = secrets.token_hex(4)
        await self.db.backups.create(
            backup_id=bid, owner_id=ctx.author.id, guild_id=ctx.guild.id,
            guild_name=payload.get("guild_name", "imported"), data=data,
            message_count=int(data.get("message_count", 0)),
        )
        await send_v2(ctx, Container(accent_color=C_SUCCESS)
                      .text("## Backup imported")
                      .text(f"Stored as `{bid}` "
                            f"({len(data.get('roles', []))} roles, "
                            f"{len(data.get('channels', []))} channels).")
                      .separator()
                      .text(f"-# Apply it with `{self._p()}backup load {bid}`."))

    def _p(self) -> str:
        return _prefix(self.bot)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImportExport(bot))
