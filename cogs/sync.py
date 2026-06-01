"""cogs/sync.py -- live mirroring of messages and bans between channels/guilds.

* ``sync messages <#source> <#target>`` mirrors new messages from the source
  channel into the target channel via a webhook (preserving author name/avatar).
* ``sync bans <source_guild_id> <target_guild_id>`` propagates bans and unbans
  from the source guild to the target guild.

The bot must be present in both guilds with the relevant permissions.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from clanklib.permissions import ModCog
from core.framework.components import Container, send_v2
from core.framework.context import DiscoContext
from clanklib.settings import prefix as _prefix
from core.framework.ui import C_ERROR, C_INFO, C_SUCCESS, fmt_ts


class Sync(ModCog):
    @commands.group(name="sync", invoke_without_command=True)
    async def sync(self, ctx: DiscoContext) -> None:
        await self.sync_list(ctx)

    @sync.command(name="messages", aliases=["msg", "msgs"])
    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(manage_webhooks=True)
    async def sync_messages(
        self, ctx: DiscoContext, source: discord.TextChannel, target: discord.TextChannel
    ) -> None:
        """Mirror new messages from #source into #target."""
        try:
            webhook = await target.create_webhook(name="Recycler Sync")
        except discord.HTTPException as exc:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                f"Couldn't create a webhook on {target.mention}: {exc}"))
            return
        link_id = await self.db.sync.create(
            kind="messages", source_id=source.id, target_id=target.id,
            owner_id=ctx.author.id, guild_id=ctx.guild.id, target_webhook=webhook.url,
        )
        await send_v2(ctx, Container(accent_color=C_SUCCESS)
                      .text("## Message sync created")
                      .text(f"`#{link_id}` · {source.mention} -> {target.mention}")
                      .separator()
                      .text(f"-# Remove with `{self._p()}sync remove {link_id}`."))

    @sync.command(name="bans")
    @commands.is_owner()
    async def sync_bans(self, ctx: DiscoContext, source_guild_id: int, target_guild_id: int) -> None:
        """Propagate bans from one guild to another (bot owner only)."""
        if self.bot.get_guild(source_guild_id) is None or self.bot.get_guild(target_guild_id) is None:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "The bot must be in both the source and target guilds."))
            return
        link_id = await self.db.sync.create(
            kind="bans", source_id=source_guild_id, target_id=target_guild_id,
            owner_id=ctx.author.id, guild_id=ctx.guild.id,
        )
        await send_v2(ctx, Container(accent_color=C_SUCCESS)
                      .text("## Ban sync created")
                      .text(f"`#{link_id}` · bans from `{source_guild_id}` -> `{target_guild_id}`")
                      .separator()
                      .text(f"-# New bans propagate live. Apply the source's *existing* "
                            f"bans now with `{self._p()}sync backfill {link_id}`."))

    @sync.command(name="backfill")
    @commands.is_owner()
    async def sync_backfill(self, ctx: DiscoContext, link_id: int) -> None:
        """Apply a ban-sync link's source bans to the target, paced for safety."""
        link = await self.db.sync.get(link_id) if hasattr(self.db.sync, "get") else None
        if link is None:
            rows = await self.db.sync.list_for_guild(ctx.guild.id)
            link = next((r for r in rows if int(r["id"]) == link_id), None)
        if link is None or link.get("kind") != "bans":
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                f"No ban-sync link `#{link_id}` found."))
            return
        source = self.bot.get_guild(int(link["source_id"]))
        target = self.bot.get_guild(int(link["target_id"]))
        if source is None or target is None:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "The bot must be in both the source and target guilds."))
            return
        try:
            bans = [entry async for entry in source.bans()]
        except discord.Forbidden:
            await send_v2(ctx, Container(accent_color=C_ERROR).text(
                "I can't read the source guild's ban list (need Ban Members there)."))
            return
        if not bans:
            await send_v2(ctx, Container(accent_color=C_INFO).text("The source guild has no bans."))
            return

        from clanklib.ratelimit import BulkRunner
        progress = await ctx.send(view=Container(accent_color=C_INFO).text(
            f"Backfilling {len(bans)} ban(s) into {target.name}, paced...").build())

        async def _ban_one(entry) -> None:
            await target.ban(discord.Object(id=entry.user.id),
                             reason=f"ban-sync backfill #{link_id} from {source.name}",
                             delete_message_seconds=0)

        async def _prog(res) -> None:
            try:
                await progress.edit(view=Container(accent_color=C_INFO).text(
                    f"Ban backfill: {res.processed}/{res.total} "
                    f"({res.succeeded} applied, {res.failed} failed)...").build())
            except Exception:  # noqa: BLE001
                pass

        result = await BulkRunner().run(bans, _ban_one, progress=_prog)
        body = (f"Applied **{result.succeeded}** ban(s) to {target.name}."
                + (f" {result.failed} failed/already banned." if result.failed else "")
                + (f"\n\n**Stopped early:** {result.abort_reason}" if result.aborted else ""))
        try:
            await progress.edit(view=Container(
                accent_color=C_ERROR if result.aborted else C_SUCCESS).text(body).build())
        except Exception:  # noqa: BLE001
            await send_v2(ctx, Container(accent_color=C_SUCCESS).text(body))

    @sync.command(name="list", aliases=["ls"])
    async def sync_list(self, ctx: DiscoContext) -> None:
        rows = await self.db.sync.list_for_guild(ctx.guild.id)
        if not rows:
            await send_v2(ctx, Container(accent_color=C_INFO).text("## No sync links")
                          .text(f"Create one with `{self._p()}sync messages #a #b`."))
            return
        lines = []
        for r in rows:
            status = "on" if r.get("enabled") else "off"
            if r["kind"] == "messages":
                lines.append(f"`#{r['id']}` · messages <#{r['source_id']}> -> "
                             f"<#{r['target_id']}>  ({status})")
            else:
                lines.append(f"`#{r['id']}` · bans `{r['source_id']}` -> "
                             f"`{r['target_id']}`  ({status})")
        await send_v2(ctx, Container(accent_color=C_INFO)
                      .text(f"## Sync links ({len(rows)})").text("\n".join(lines)[:3900]))

    @sync.command(name="remove", aliases=["del", "rm", "delete"])
    @commands.has_guild_permissions(manage_guild=True)
    async def sync_remove(self, ctx: DiscoContext, link_id: int) -> None:
        deleted = await self.db.sync.delete(link_id, ctx.guild.id)
        color, text = (C_SUCCESS, f"Removed sync link `#{link_id}`.") if deleted else (
            C_ERROR, f"No sync link `#{link_id}` in this server.")
        await send_v2(ctx, Container(accent_color=color).text(text))

    # ── listeners ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None or message.guild is None:
            return
        links = await self.db.sync.links_for_source("messages", message.channel.id)
        for link in links:
            url = link.get("target_webhook")
            if not url:
                continue
            content = message.content or ""
            if message.attachments:
                content = (content + "\n" + "\n".join(a.url for a in message.attachments)).strip()
            if not content:
                continue
            try:
                webhook = discord.Webhook.from_url(url, client=self.bot)
                await webhook.send(
                    content=content[:2000],
                    username=message.author.display_name[:80],
                    avatar_url=str(message.author.display_avatar.url),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                continue

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        links = await self.db.sync.links_for_source("bans", guild.id)
        for link in links:
            target = self.bot.get_guild(int(link["target_id"]))
            if target is None:
                continue
            try:
                await target.ban(discord.Object(id=user.id), reason="Synced ban from source guild")
            except discord.HTTPException:
                continue

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        links = await self.db.sync.links_for_source("bans", guild.id)
        for link in links:
            target = self.bot.get_guild(int(link["target_id"]))
            if target is None:
                continue
            try:
                await target.unban(discord.Object(id=user.id), reason="Synced unban from source guild")
            except discord.HTTPException:
                continue

    def _p(self) -> str:
        return _prefix(self.bot)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Sync(bot))
