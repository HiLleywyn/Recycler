"""cogs/templates.py -- shareable, structure-only server templates."""
from __future__ import annotations

import re
import secrets

import discord
from discord.ext import commands

from clanklib.permissions import ModCog
from core.framework.components import Container, send_v2
from core.framework.context import DiscoContext
from core.framework.ui import C_ERROR, C_GOLD, C_INFO, C_SUCCESS, fmt_ts
from clanklib import serializer
from clanklib.settings import prefix as _prefix
from cogs._ui import confirm_v2

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    base = _SLUG_RE.sub("-", name.lower()).strip("-")[:24] or "template"
    return f"{base}-{secrets.token_hex(2)}"


class Templates(ModCog):
    @commands.group(name="template", aliases=["templates", "tpl"], invoke_without_command=True)
    async def template(self, ctx: DiscoContext) -> None:
        await self.template_browse(ctx)

    @template.command(name="create", aliases=["new", "save"])
    @commands.has_guild_permissions(administrator=True)
    async def template_create(self, ctx: DiscoContext, *, args: str) -> None:
        """Create a template from this server: ``template create Name | description``."""
        name, _, desc = args.partition("|")
        name, desc = name.strip()[:80], desc.strip()[:300]
        if not name:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "Give a name, e.g. `template create Gaming Hub | A cozy gaming server`."))
            return
        data = serializer.serialize_template(ctx.guild)
        tid = _slug(name)
        await self.db.templates.create(
            template_id=tid, name=name, description=desc,
            owner_id=ctx.author.id, data=data,
        )
        await send_v2(ctx, Container(accent_color=C_SUCCESS)
                      .text("## Template created")
                      .text(f"**{name}** · `{tid}`\n"
                            f"{len(data['roles'])} roles, {len(data['channels'])} channels")
                      .separator()
                      .text(f"-# Anyone can apply it with `{self._p()}template load {tid}`."))

    @template.command(name="load", aliases=["apply", "use"])
    @commands.has_guild_permissions(administrator=True)
    @commands.bot_has_guild_permissions(administrator=True)
    async def template_load(self, ctx: DiscoContext, template_id: str) -> None:
        row = await self.db.templates.get(template_id.lower())
        if row is None:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                f"No template `{template_id}`."))
            return
        ok = await confirm_v2(
            ctx, title="Apply template?",
            body=(f"This **deletes current channels and roles** and rebuilds "
                  f"`{ctx.guild.name}` from template **{row['name']}**. "
                  f"This cannot be undone."),
        )
        if not ok:
            return
        async with ctx.typing():
            stats = await serializer.restore_guild(
                ctx.guild, row["data"], serializer.RestoreOptions(restore_messages=False)
            )
            await self.db.templates.increment_uses(template_id.lower())
        dest = ctx.guild.system_channel or next(iter(ctx.guild.text_channels), None) or ctx.channel
        await send_v2(dest, Container(accent_color=C_SUCCESS if not stats.errors else C_GOLD)
                      .text("## Template applied").text(stats.summary()))

    @template.command(name="browse", aliases=["search", "explore"])
    async def template_browse(self, ctx: DiscoContext, *, query: str = "") -> None:
        rows = await self.db.templates.browse(query=query.strip())
        if not rows:
            await send_v2(ctx, Container(accent_color=C_INFO).text(
                "## No templates found").text(
                f"Publish one with `{self._p()}template create`."))
            return
        lines = [
            f"{'(featured) ' if r.get('featured') else ''}`{r['id']}` · **{r['name']}**  "
            f"· {r.get('uses', 0)} uses"
            + (f"\n-# {r['description']}" if r.get("description") else "")
            for r in rows
        ]
        title = f"Templates matching '{query}'" if query else "Community templates"
        await send_v2(ctx, Container(accent_color=C_INFO)
                      .text(f"## {title} ({len(rows)})")
                      .text("\n".join(lines)[:3900]))

    @template.command(name="info")
    async def template_info(self, ctx: DiscoContext, template_id: str) -> None:
        row = await self.db.templates.get(template_id.lower())
        if row is None:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                f"No template `{template_id}`."))
            return
        data = row["data"]
        await send_v2(ctx, Container(accent_color=C_INFO)
                      .text(f"## {row['name']}")
                      .text((row.get("description") or "_No description._") + "\n\n"
                            f"**ID** `{row['id']}` · **Uses** {row.get('uses', 0)}  "
                            f"**Created** {fmt_ts(row['created_at'])}\n"
                            f"**Roles** {len(data.get('roles', []))} · "
                            f"**Channels** {len(data.get('channels', []))}"))

    @template.command(name="delete", aliases=["del", "rm"])
    async def template_delete(self, ctx: DiscoContext, template_id: str) -> None:
        deleted = await self.db.templates.delete(template_id.lower(), ctx.author.id)
        color, text = (C_SUCCESS, f"Deleted template `{template_id}`.") if deleted else (
            C_ERROR, f"No template `{template_id}` owned by you.")
        await send_v2(ctx, Container(accent_color=color).text(text))

    def _p(self) -> str:
        return _prefix(self.bot)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Templates(bot))
