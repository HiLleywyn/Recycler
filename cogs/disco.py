"""cogs/disco.py -- the player-facing ,disco command group.

The bare ``,disco`` command is a help page anyone can open. Every nested
command is gated: it only runs for members who have unlocked the group by
boosting the server, reaching chat level 50, or being staff (see
services/disco_access.py). The group is prefix-only -- there is no slash
command.

Nested commands:
    ,disco chat / threads        -- pick inline replies or thread replies
    ,disco ctx [@user|#channel|server|clear]
                                 -- inspect (or wipe your own) AI context
    ,disco save                  -- bookmark a Disco answer (reply to one)
    ,disco unsave [num]          -- drop a bookmark
    ,disco saved [num]           -- browse your bookmarks
    ,disco optin / optout        -- AI context tracking opt-in/out
    ,disco gif "search term"     -- search GIPHY for a GIF
    ,disco image / video         -- media generation (coming soon)
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.ui import (
    C_INFO,
    C_NEUTRAL,
    C_PURPLE,
    CategoryPaginator,
    fmt_ts,
)
from services.ai_context_render import build_aictx_pages
from services.disco_access import DISCO_LEVEL_UNLOCK, get_disco_access

log = logging.getLogger(__name__)


def _disco_unlocked():
    """Check: gate a nested ,disco command behind premium + the unlock rule."""

    async def predicate(ctx: DiscoContext) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        from core.framework.premium import PremiumGateFailure
        from services import entitlements

        if not await entitlements.is_premium(ctx.guild_id, ctx.db):
            # The whole Disco AI surface is a premium feature; nested
            # commands stay off on non-premium servers.
            raise PremiumGateFailure("ai")
        access = await get_disco_access(ctx.author, ctx.guild, ctx.db)
        if not access.unlocked:
            raise commands.CheckFailure(
                "`,disco` commands are locked for you. They unlock for server "
                f"**boosters**, members at **level {DISCO_LEVEL_UNLOCK}+**, and "
                "**staff**. Run `,disco` to see your status."
            )
        return True

    return commands.check(predicate)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Disco(commands.Cog):
    """The ,disco command group: player-facing Disco AI controls."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # -- bare ,disco : help page (open to everyone) ------------------------

    @commands.group(name="disco", invoke_without_command=True)
    @guild_only
    @no_bots
    async def disco(self, ctx: DiscoContext, *, _rest: str = "") -> None:
        """Disco AI controls. Run ,disco for the full help page."""
        await self._send_help(ctx)

    async def _send_help(self, ctx: DiscoContext) -> None:
        p = ctx.clean_prefix or ","
        from services import entitlements

        try:
            premium = await entitlements.is_premium(ctx.guild_id, ctx.db)
        except Exception:  # noqa: BLE001
            premium = True
        access = await get_disco_access(ctx.author, ctx.guild, ctx.db)

        b = card(
            "Disco AI",
            color=C_INFO if access.unlocked else C_NEUTRAL,
            description=(
                "Disco answers `@`mentions and replies with a small, "
                "memory-backed AI. These commands let you tune how it talks "
                "to you and keep the answers worth keeping."
            ),
        )
        b = (
            b.field(
                "Talk style",
                f"`{p}disco chat` -- Disco replies inline in-channel\n"
                f"`{p}disco threads` -- Disco replies inside its own thread",
                False,
            )
            .field(
                "Context",
                f"`{p}disco ctx` -- what Disco knows about you\n"
                f"`{p}disco ctx @user` -- look up another member\n"
                f"`{p}disco ctx #channel` / `server` -- channel or server context\n"
                f"`{p}disco ctx clear` -- wipe what Disco learned about you",
                False,
            )
            .field(
                "Saved answers",
                f"`{p}disco save` -- reply to a Disco message to bookmark it\n"
                f"`{p}disco saved [num]` -- browse your bookmarks\n"
                f"`{p}disco unsave <num>` -- drop a bookmark",
                False,
            )
            .field(
                "Privacy",
                f"`{p}disco optout` -- stop Disco learning about you\n"
                f"`{p}disco optin` -- opt back in (everyone starts opted in)",
                False,
            )
            .field(
                "Media",
                f"`{p}disco gif \"search term\"` -- search GIPHY for a GIF\n"
                f"`{p}disco image|video \"prompt\"` -- image/video generation (coming soon)",
                False,
            )
        )

        status = access.label
        if access.unlocked:
            mode = await self.bot.db.get_disco_reply_mode(ctx.author.id, ctx.guild_id)
            status += f"\nReply mode: **{'inline chat' if mode == 'chat' else 'threads'}**"
        else:
            status += (
                f"\nUnlock by **boosting the server**, reaching **level "
                f"{DISCO_LEVEL_UNLOCK}**, or being **staff**. The bare "
                f"`{p}disco` page stays open to everyone."
            )
        if not premium:
            status += "\n\nThis server is not premium -- nested commands are off here."
        b = b.field("Your status", status, False)
        b = b.footer("Disco AI -- prefix-only command group")
        await ctx.reply(embed=b.build(), mention_author=False)

    # -- reply mode --------------------------------------------------------

    @disco.command(name="chat")
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_chat(self, ctx: DiscoContext) -> None:
        """Switch Disco to inline in-channel replies instead of threads."""
        await self.bot.db.set_disco_reply_mode(ctx.author.id, ctx.guild_id, "chat")
        await ctx.reply_success(
            "Disco will now answer you with a normal in-channel reply instead "
            "of opening a thread. Switch back any time with `,disco threads`.",
            title="Reply mode: inline chat",
        )

    @disco.command(name="threads", aliases=["thread"])
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_threads(self, ctx: DiscoContext) -> None:
        """Switch Disco back to replying inside its own thread."""
        await self.bot.db.set_disco_reply_mode(ctx.author.id, ctx.guild_id, "thread")
        await ctx.reply_success(
            "Disco will now answer you inside its own thread to keep channels "
            "tidy. Switch to inline replies any time with `,disco chat`.",
            title="Reply mode: threads",
        )

    # -- context inspector -------------------------------------------------

    @disco.command(name="ctx", aliases=["context"])
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_ctx(self, ctx: DiscoContext, *, target: str = "") -> None:
        """Inspect AI context: yours, a member's, a channel's, or the server's."""
        arg = target.strip()
        low = arg.lower()

        if low == "clear":
            await self.bot.db.wipe_ai_user_state(ctx.author.id, ctx.guild_id)
            await ctx.reply_success(
                "Wiped what Disco had learned about you in this server -- "
                "memory, traits, conversation history, and your personal "
                "facts. The next conversation rebuilds context from scratch.",
                title="Your Disco context cleared",
            )
            return

        if ctx.message.channel_mentions:
            await self._show_channel_ctx(ctx, ctx.message.channel_mentions[0])
            return

        if low in ("server", "guild"):
            await self._show_server_ctx(ctx)
            return

        mentioned = [m for m in ctx.message.mentions if m.id != self.bot.user.id]
        if mentioned:
            await self._show_user_ctx(ctx, mentioned[0])
            return

        if arg:
            member = self._resolve_named_member(ctx, arg)
            if member is None:
                await ctx.reply_error(
                    f"Couldn't find a member matching `{arg}`. Pass a "
                    "@mention, a user ID, `#channel`, `server`, or `clear`."
                )
                return
            await self._show_user_ctx(ctx, member)
            return

        await self._show_user_ctx(ctx, ctx.author)

    def _resolve_named_member(
        self, ctx: DiscoContext, raw: str,
    ) -> discord.Member | None:
        raw = raw.strip().strip("<@!>")
        if raw.isdigit():
            return ctx.guild.get_member(int(raw))
        low = raw.lower()
        return discord.utils.find(
            lambda m: m.display_name.lower() == low or m.name.lower() == low,
            ctx.guild.members,
        )

    async def _show_user_ctx(
        self, ctx: DiscoContext, member: discord.abc.User,
    ) -> None:
        footer = "Also: ,disco ctx @user / #channel / server / clear"
        pages = await build_aictx_pages(
            self.bot.db,
            member.id,
            ctx.guild_id,
            member.display_name,
            avatar_url=member.display_avatar.url,
            footer=footer,
        )
        await ctx.paginate(pages)

    async def _show_channel_ctx(
        self, ctx: DiscoContext, channel: discord.abc.GuildChannel,
    ) -> None:
        gid = ctx.guild_id
        scope = f"guild:{gid}"
        listening = None
        episodes: list[dict] = []
        feed_rows = 0
        try:
            listening = await self.bot.db.fetch_val(
                "SELECT 1 FROM disco_passive_channels "
                "WHERE guild_id=$1 AND channel_id=$2",
                gid, channel.id,
            )
        except Exception:  # noqa: BLE001
            listening = None
        try:
            episodes = await self.bot.db.fetch_all(
                "SELECT summary, EXTRACT(EPOCH FROM created_at) AS created_at "
                "FROM disco_episodes WHERE scope=$1 AND $2 = ANY(tags) "
                "ORDER BY created_at DESC LIMIT 8",
                scope, f"channel:{channel.id}",
            )
        except Exception:  # noqa: BLE001
            episodes = []
        try:
            feed_rows = int(await self.bot.db.fetch_val(
                "SELECT COUNT(*) FROM channel_context "
                "WHERE guild_id=$1 AND channel_id=$2",
                gid, channel.id,
            ) or 0)
        except Exception:  # noqa: BLE001
            feed_rows = 0

        b = card(f"Disco channel context -- #{channel.name}", color=C_INFO)
        b = b.field(
            "Passive learning",
            "On -- Disco logs ambient messages here" if listening
            else "Off -- Disco only sees mentions/replies here",
            True,
        )
        b = b.field("Tracked signals", str(feed_rows), True)
        if episodes:
            lines = "\n".join(
                f"- {_clip(e.get('summary') or '', 110)} "
                f"({fmt_ts(e.get('created_at'))})"
                for e in episodes
            )
        else:
            lines = "(no episodes recorded for this channel yet)"
        b = b.field("Recent episodes", _clip(lines, 1024), False)
        b = b.footer("Also: ,disco ctx @user / #channel / server / clear")
        await ctx.reply(embed=b.build(), mention_author=False)

    async def _show_server_ctx(self, ctx: DiscoContext) -> None:
        gid = ctx.guild_id
        scope = f"guild:{gid}"
        facts: list[dict] = []
        episodes: list[dict] = []
        optouts = 0
        try:
            facts = await self.bot.db.fetch_all(
                "SELECT key, value FROM disco_facts WHERE scope=$1 "
                "ORDER BY updated_at DESC LIMIT 12",
                scope,
            )
        except Exception:  # noqa: BLE001
            facts = []
        try:
            episodes = await self.bot.db.fetch_all(
                "SELECT summary FROM disco_episodes WHERE scope=$1 "
                "ORDER BY created_at DESC LIMIT 8",
                scope,
            )
        except Exception:  # noqa: BLE001
            episodes = []
        try:
            optouts = int(await self.bot.db.fetch_val(
                "SELECT COUNT(*) FROM ai_opt_outs WHERE guild_id=$1", gid,
            ) or 0)
        except Exception:  # noqa: BLE001
            optouts = 0

        b = card(f"Disco server context -- {ctx.guild.name}", color=C_INFO)
        if facts:
            fact_lines = "\n".join(
                f"`{f['key']}`: {_clip(f.get('value') or '', 90)}" for f in facts
            )
        else:
            fact_lines = "(no server facts learned yet)"
        b = b.field("Server facts", _clip(fact_lines, 1024), False)
        if episodes:
            ep_lines = "\n".join(
                f"- {_clip(e.get('summary') or '', 110)}" for e in episodes
            )
        else:
            ep_lines = "(no server episodes recorded yet)"
        b = b.field("Recent episodes", _clip(ep_lines, 1024), False)
        b = b.field("Members opted out of AI tracking", str(optouts), True)
        b = b.footer("Also: ,disco ctx @user / #channel / clear")
        await ctx.reply(embed=b.build(), mention_author=False)

    # -- saved answers -----------------------------------------------------

    def _is_disco_message(self, msg: discord.Message | None) -> bool:
        """True when *msg* is one of Disco's own text answers."""
        if msg is None or self.bot.user is None:
            return False
        return msg.author.id == self.bot.user.id and bool((msg.content or "").strip())

    async def _resolve_referenced_message(
        self, ctx: DiscoContext,
    ) -> discord.Message | None:
        ref = ctx.message.reference
        if ref is None:
            return None
        if isinstance(ref.resolved, discord.Message):
            return ref.resolved
        if ref.message_id:
            try:
                return await ctx.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        return None

    async def _resolve_trigger(
        self, disco_msg: discord.Message,
    ) -> discord.Message | None:
        """Find the human message that prompted one of Disco's answers."""
        ref = disco_msg.reference
        if ref is not None:
            cand: discord.Message | None = None
            if isinstance(ref.resolved, discord.Message):
                cand = ref.resolved
            elif ref.message_id:
                try:
                    cand = await disco_msg.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    cand = None
            if cand is not None and not cand.author.bot:
                return cand
        try:
            async for m in disco_msg.channel.history(limit=20, before=disco_msg):
                if not m.author.bot and (m.content or "").strip():
                    return m
        except discord.HTTPException:
            pass
        return None

    @disco.command(name="save")
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_save(self, ctx: DiscoContext) -> None:
        """Bookmark a Disco answer. Run this as a reply to one of its messages."""
        disco_msg = await self._resolve_referenced_message(ctx)
        if not self._is_disco_message(disco_msg):
            await ctx.reply_error(
                "Reply to one of Disco's messages with `,disco save` to "
                "bookmark that answer."
            )
            return

        trigger = await self._resolve_trigger(disco_msg)
        response_text = (disco_msg.content or "").strip()
        prompt_text = (
            (trigger.content or "").strip() if trigger else ""
        ) or "(original message could not be found)"

        saved = await self.bot.db.add_disco_saved_message(
            ctx.author.id,
            ctx.guild_id,
            disco_msg.channel.id,
            disco_msg.id,
            trigger.id if trigger else None,
            _clip(prompt_text, 1500),
            _clip(response_text, 3000),
            disco_msg.jump_url,
        )
        if not saved:
            await ctx.reply_error(
                "You've already saved that Disco answer. See `,disco saved`."
            )
            return
        await ctx.reply_success(
            "Bookmarked that exchange. View it any time with `,disco saved`.",
            title="Disco answer saved",
        )

    @disco.command(name="unsave")
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_unsave(self, ctx: DiscoContext, index: int | None = None) -> None:
        """Drop a bookmarked Disco answer by its number (or reply to one)."""
        rows = await self.bot.db.list_disco_saved_messages(
            ctx.author.id, ctx.guild_id,
        )
        if not rows:
            await ctx.reply_error("You have no saved Disco answers.")
            return

        target: dict | None = None
        if index is not None:
            if index < 0 or index >= len(rows):
                await ctx.reply_error(
                    f"No saved answer `{index}`. You have {len(rows)} "
                    f"(0-{len(rows) - 1}). See `,disco saved`."
                )
                return
            target = rows[index]
        else:
            disco_msg = await self._resolve_referenced_message(ctx)
            if disco_msg is not None:
                target = next(
                    (r for r in rows
                     if int(r["disco_message_id"]) == disco_msg.id),
                    None,
                )
            if target is None:
                await ctx.reply_error(
                    "Give the number to drop -- `,disco unsave <num>` -- or "
                    "run it as a reply to a saved Disco message."
                )
                return

        ok = await self.bot.db.delete_disco_saved_message(
            ctx.author.id, ctx.guild_id, int(target["id"]),
        )
        if not ok:
            await ctx.reply_error("Couldn't drop that bookmark -- try again.")
            return
        await ctx.reply_success(
            "Removed that answer from your saved messages.",
            title="Bookmark dropped",
        )

    def _saved_detail_embed(
        self, ctx: DiscoContext, row: dict, index: int, total: int,
    ) -> discord.Embed:
        prompt = (row.get("prompt_text") or "").strip() or "(unavailable)"
        response = (row.get("response_text") or "").strip() or "(empty)"
        channel = self.bot.get_channel(int(row["channel_id"]))
        if isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            where = channel.mention
        else:
            where = f"channel `{row['channel_id']}`"

        b = card(f"Saved Disco answer #{index}", color=C_PURPLE)
        b = b.field("Your question", _clip(prompt, 1024), False)
        b = b.field("Disco's answer", _clip(response, 1024), False)
        b = b.field(
            "Where & when",
            f"{where}  -  {fmt_ts(row.get('saved_at'))}",
            False,
        )
        jump = row.get("jump_url")
        if jump:
            b = b.field("Jump to message", f"[Open in chat]({jump})", False)
        b = b.footer(
            f"{index} of {total - 1}  -  ,disco unsave {index} to remove"
        )
        return b.build()

    @disco.command(name="saved")
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_saved(self, ctx: DiscoContext, index: int | None = None) -> None:
        """Browse your bookmarked Disco answers, or open one by number."""
        rows = await self.bot.db.list_disco_saved_messages(
            ctx.author.id, ctx.guild_id,
        )
        if not rows:
            await ctx.reply_error(
                "You haven't saved any Disco answers yet. Reply to one of "
                "Disco's messages with `,disco save`."
            )
            return

        if index is not None:
            if index < 0 or index >= len(rows):
                await ctx.reply_error(
                    f"No saved answer `{index}`. You have {len(rows)} "
                    f"(0-{len(rows) - 1})."
                )
                return
            await ctx.reply(
                embed=self._saved_detail_embed(ctx, rows[index], index, len(rows)),
                mention_author=False,
            )
            return

        categories: dict[str, list[discord.Embed]] = {}
        for i, row in enumerate(rows):
            snippet = (row.get("prompt_text") or "").strip() or "saved answer"
            label = _clip(f"#{i} - {snippet}", 90)
            # Guard against duplicate dropdown labels.
            if label in categories:
                label = f"{label} ({i})"
            categories[label] = [self._saved_detail_embed(ctx, row, i, len(rows))]
        await CategoryPaginator.send(ctx, categories)

    # -- privacy -----------------------------------------------------------

    @disco.command(name="optout")
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_optout(self, ctx: DiscoContext) -> None:
        """Opt out of AI context tracking. Disco forgets what it knows about you."""
        already = await self.bot.db.is_ai_opted_out(ctx.author.id, ctx.guild_id)
        if already:
            await ctx.reply_error_hint(
                "You're already opted out.",
                hint=f"Use {ctx.clean_prefix}disco optin to re-enable memory.",
                command_name="disco optin",
            )
            return
        await self.bot.db.set_ai_opt_out(ctx.author.id, ctx.guild_id)
        await ctx.reply_success(
            "Wiped your AI memory, conversation history, and learned traits. "
            "Disco no longer remembers anything about you in this server. "
            f"You can reverse this with `{ctx.clean_prefix}disco optin`.",
            title="Opted out of AI context",
        )

    @disco.command(name="optin")
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_optin(self, ctx: DiscoContext) -> None:
        """Opt back in to AI context tracking (everyone starts opted in)."""
        was = await self.bot.db.is_ai_opted_out(ctx.author.id, ctx.guild_id)
        if not was:
            await ctx.reply_error("You weren't opted out.")
            return
        await self.bot.db.clear_ai_opt_out(ctx.author.id, ctx.guild_id)
        await ctx.reply_success(
            "Welcome back. Disco will start learning about you again from "
            "here on.",
            title="Opted in to AI context",
        )

    # -- media generation --------------------------------------------------

    @disco.command(name="image", aliases=["video"])
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_image(self, ctx: DiscoContext, *, prompt: str = "") -> None:
        """Generate an image or video from a prompt (coming soon)."""
        kind = (ctx.invoked_with or "image").lower()
        if kind not in ("image", "video"):
            kind = "image"
        await ctx.reply(
            embed=card(
                f"Disco {kind} generation",
                color=C_NEUTRAL,
                description=(
                    f"Disco {kind} generation is **coming soon**. The command "
                    "is here so you know it's on the way, but it isn't wired "
                    "up yet -- check back later."
                ),
            ).build(),
            mention_author=False,
        )

    @disco.command(name="gif")
    @guild_only
    @no_bots
    @ensure_registered
    @_disco_unlocked()
    async def disco_gif(self, ctx: DiscoContext, *, prompt: str = "") -> None:
        """Search GIPHY for a GIF matching your prompt."""
        from core.config import Config
        from services.giphy import search_gif

        if not Config.GIPHY_API_KEY:
            await ctx.reply_error(
                "GIPHY is not configured on this server. "
                "Set `GIPHY_API_KEY` in the bot's environment to enable GIF search."
            )
            return

        query = prompt.strip()
        if not query:
            await ctx.reply_error(
                f"Provide a search term: `{ctx.clean_prefix}disco gif <search term>`"
            )
            return

        gif_url = await search_gif(query)
        if not gif_url:
            await ctx.reply_error(
                f"No GIFs found for **{query}** -- try a different search term."
            )
            return

        await ctx.reply(
            embed=card(
                "Disco GIF",
                color=C_INFO,
            )
            .field("Search", query, True)
            .image(gif_url)
            .build(),
            mention_author=False,
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Disco(bot))
