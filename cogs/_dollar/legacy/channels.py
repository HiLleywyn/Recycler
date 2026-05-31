"""``$channels`` -- admin allowlist for the ``$`` namespace.

Independent of ``bot_channels`` so admins can enable ``$chart`` /
``$info`` in a chat channel without also enabling game commands
there.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_INFO

from ._shared import _CHANNEL_MENTION

if TYPE_CHECKING:
    from cogs.realmarket import RealMarket

log = logging.getLogger(__name__)


async def _show_help(ctx: DiscoContext) -> None:
    embed = (
        card(
            "📺 $channels (admin only)",
            description=(
                "Manage which channels allow the `$`-prefixed real-market "
                "commands. Separate from `bot_channels`, so you can enable "
                "`$chart` / `$info` in a chat channel without also "
                "enabling game commands there."
            ),
            color=C_INFO,
        )
        .field("`$channels add [#channel]`",
               "Allow `$chart` / `$info` in the given channel "
               "(defaults to the current channel).", False)
        .field("`$channels remove [#channel]`",
               "Remove the channel from the `$`-only allowlist.", False)
        .field("`$channels list`",
               "Show the current `$`-only allowlist and the `bot_channels` "
               "list (which also runs `$` commands).", False)
        .field("`$channels reset`",
               "Wipe the `$`-only allowlist.", False)
        .footer("Requires the Manage Server permission.")
        .build()
    )
    await ctx.reply(embed=embed, mention_author=False)


async def handle(ctx: DiscoContext, raw_args: str, *, cog: "RealMarket") -> None:
    member = ctx.author
    guild = ctx.guild
    if not isinstance(member, discord.Member) or not guild:
        await ctx.reply_error("Run `$channels` from inside a server.")
        return
    if not member.guild_permissions.manage_guild:
        await ctx.reply_error("You need the **Manage Server** permission to configure `$` channels.")
        return

    tokens = raw_args.split()
    if not tokens:
        await _show_help(ctx)
        return

    action = tokens[0].lower()
    target_id: int | None = None
    if len(tokens) > 1:
        arg = tokens[1].strip()
        m = _CHANNEL_MENTION.match(arg)
        if m:
            target_id = int(m.group(1))
        elif arg.isdigit():
            target_id = int(arg)
        else:
            await ctx.reply_error(
                "Couldn't parse the channel argument. Use a `#channel` "
                "mention, a numeric channel id, or leave it blank to use "
                "the current channel."
            )
            return

    if action in ("add", "allow", "enable"):
        ch_id = target_id or ctx.channel.id
        channel = guild.get_channel(ch_id) or guild.get_thread(ch_id)
        if channel is None:
            await ctx.reply_error(f"Channel `{ch_id}` isn't in this server.")
            return
        await cog.bot.db.guilds.add_realmarket_channel(guild.id, ch_id)
        await ctx.reply_success(
            f"<#{ch_id}> can now run `$chart` / `$info`. Game commands "
            "still need a separate `bot_channels` entry.",
            title="Channel enabled",
        )
        return

    if action in ("remove", "rm", "deny", "disable"):
        ch_id = target_id or ctx.channel.id
        await cog.bot.db.guilds.remove_realmarket_channel(guild.id, ch_id)
        await ctx.reply_success(
            f"<#{ch_id}> no longer runs `$chart` / `$info` (unless it's "
            "also in `bot_channels`).",
            title="Channel removed",
        )
        return

    if action in ("list", "ls", "show"):
        rm = await cog.bot.db.guilds.get_realmarket_channels(guild.id)
        bot_chs = await cog.bot.db.guilds.get_bot_channels(guild.id)
        lines: list[str] = []
        if rm:
            lines.append("**$-only allowlist** (configured via `$channels add`):")
            lines.extend(f"  · <#{c}>" for c in rm)
        else:
            lines.append("**$-only allowlist:** *(empty)*")
        lines.append("")
        if bot_chs:
            lines.append("**`bot_channels` (game surface, also runs $):**")
            lines.extend(f"  · <#{c}>" for c in bot_chs)
        else:
            lines.append("**`bot_channels`:** *(empty -- so $ commands run anywhere)*")
        embed = card(
            "📺 Real-market channel allowlist",
            description="\n".join(lines),
            color=C_INFO,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)
        return

    if action in ("reset", "clear", "wipe"):
        n = await cog.bot.db.guilds.clear_realmarket_channels(guild.id)
        await ctx.reply_success(
            f"Cleared {n} channel(s) from the `$`-only allowlist. The "
            "`bot_channels` list is untouched.",
            title="Allowlist reset",
        )
        return

    await _show_help(ctx)
