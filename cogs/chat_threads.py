"""ChatThreads -- background lifecycle + ,thread command surface.

Owns the in-memory set of active AI chat thread IDs (``bot._ai_thread_ids``,
read by core/framework/bot.py on_message on the hot path), the 12h-idle deletion
loop, the persistent in-thread control panels, and the player-facing
,thread commands. All the real work lives in services/chat_threads.py so
the natural-language path in cogs/help.py and these explicit commands stay
in lockstep.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.heartbeat import pulse, register_interval
from core.framework.middleware import guild_only
from core.framework.ui import C_INFO, C_SUCCESS

import services.chat_threads as chat_threads_svc

log = logging.getLogger(__name__)

_SWEEP_SECONDS = 600  # idle-deletion sweep cadence (10 min)


class ChatThreads(commands.Cog):
    """Lifecycle + commands for thread-based AI chat."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        # The hot-path set on_message checks. The cog owns it; the bot just
        # holds a reference so routing avoids a get_cog() call per message.
        self.active_thread_ids: set[int] = set()
        bot._ai_thread_ids = self.active_thread_ids
        self._idle_sweep.start()
        register_interval("chat_thread_sweep", _SWEEP_SECONDS)

    async def cog_load(self) -> None:
        """Repopulate the active-thread set + panel views after a restart."""
        try:
            rows = await self.bot.db.fetch_all(
                "SELECT thread_id, panel_message_id FROM chat_threads "
                "WHERE status='active'"
            )
            for r in rows:
                tid = int(r["thread_id"])
                self.active_thread_ids.add(tid)
                pmid = r.get("panel_message_id")
                if pmid:
                    try:
                        self.bot.add_view(
                            chat_threads_svc.ThreadPanelView(tid),
                            message_id=int(pmid),
                        )
                    except Exception:
                        log.warning(
                            "[chat_threads] could not re-register panel for %s",
                            tid, exc_info=True,
                        )
            log.info(
                "[chat_threads] tracking %d active AI threads",
                len(self.active_thread_ids),
            )
        except Exception:
            log.warning("[chat_threads] could not load active threads", exc_info=True)

    def cog_unload(self) -> None:
        self._idle_sweep.cancel()

    # -- Idle-deletion loop -------------------------------------------------

    @tasks.loop(seconds=_SWEEP_SECONDS)
    async def _idle_sweep(self) -> None:
        """Delete AI threads with no activity for 12h (DB-side clock)."""
        try:
            due = await self.bot.db.fetch_all(
                "SELECT thread_id, guild_id, history_key, saved FROM chat_threads "
                "WHERE status='active' "
                "AND EXTRACT(EPOCH FROM (NOW() - last_activity)) > $1",
                float(chat_threads_svc.IDLE_DELETE_SECONDS),
            )
            for row in due:
                try:
                    await chat_threads_svc.delete_idle_thread(self.bot, dict(row))
                except Exception:
                    log.warning(
                        "[chat_threads] idle delete failed for thread %s",
                        row.get("thread_id"), exc_info=True,
                    )
        except Exception:
            log.warning("[chat_threads] idle sweep failed", exc_info=True)
        pulse("chat_thread_sweep")

    @_idle_sweep.before_loop
    async def _before_idle_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        """Purge a manually-deleted thread from the DAG.

        Disco's own closes (``,thread close``, idle sweep, push) drop the
        thread from the active set BEFORE deleting it, so this listener
        only ever fires for a thread a user deleted by hand in Discord.
        Such a thread is fully purged -- row closed, links + group seat
        dropped, transcript and saved state (recall code) cleared -- so it
        can never linger in ``,thread list`` or stay linked into another
        conversation.
        """
        if thread.id not in self.active_thread_ids:
            return
        self.active_thread_ids.discard(thread.id)
        try:
            row = await chat_threads_svc.get_thread_row(self.bot.db, thread.id)
            if row and row.get("status") == "active":
                await chat_threads_svc.close_thread_row(
                    self.bot.db, thread.id,
                    drop_transcript=True,
                    forget_saved=True,
                    guild_id=int(row["guild_id"]),
                    history_key=row["history_key"],
                )
        except Exception:
            log.warning("[chat_threads] on_thread_delete cleanup failed", exc_info=True)

    # -- Helpers ------------------------------------------------------------

    async def _active_thread_row(self, ctx: DiscoContext) -> dict | None:
        """Return the chat_threads row when ctx is inside a live Disco thread."""
        ch = ctx.channel
        if not (isinstance(ch, discord.Thread) and ch.id in self.active_thread_ids):
            return None
        return await chat_threads_svc.get_thread_row(self.bot.db, ch.id)

    # -- ,thread command group ---------------------------------------------

    @commands.group(name="thread", aliases=["threads"], invoke_without_command=True)
    @guild_only
    async def thread_group(self, ctx: DiscoContext) -> None:
        """Manage your saved Disco chat threads."""
        p = ctx.clean_prefix
        embed = (
            card(
                "Disco Chat Threads",
                color=C_INFO,
                description=(
                    "Disco replies to `@` mentions inside their own thread to keep "
                    "channels clean. Threads delete themselves after 12h idle -- "
                    "save the ones worth keeping. Each thread carries a pinned "
                    "control panel with buttons for all of this."
                ),
            )
            .field(f"{p}thread save", "Save the current thread and mint a recall code.", False)
            .field(f"{p}thread unsave", "Drop a thread's saved state and recall code.", False)
            .field(f"{p}thread list", "List the threads you've saved.", False)
            .field(f"{p}thread find <code>", "Jump to a saved thread (links you to the original -- never makes a new one).", False)
            .field(f"{p}thread link <code|thread>", "Merge another thread's context in by recall code or thread id -- auto-saves it, no `,thread save` needed (up to 3).", False)
            .field(f"{p}thread group link <#>", "Merge a whole thread group's context in (up to 3).", False)
            .field(f"{p}thread group list", "List the thread groups in this server.", False)
            .field(f"{p}thread push <code>", "Permanently merge this thread into a linked one, then close it.", False)
            .field(f"{p}thread unlink <code|#|all>", "Remove a merged thread or group and free the slot.", False)
            .field(f"{p}thread links", "Show the threads and groups merged into this one.", False)
            .field(f"{p}thread ctx", "Dump this thread's full conversation context.", False)
            .field(f"{p}thread close [all]", "Close this thread (or all of yours). Closing rolls its context back out.", False)
            .field(
                "Who can do what",
                "Anyone in a thread can use `find`, `links`, and `ctx`. Only the "
                "thread owner or a mod can `save`, `unsave`, `link`, `unlink`, or `close`.",
                False,
            )
            .build()
        )
        await ctx.send(embed=embed)

    @thread_group.command(name="save")
    @guild_only
    async def thread_save(self, ctx: DiscoContext) -> None:
        """Save the current chat thread and mint a recall code."""
        row = await self._active_thread_row(ctx)
        if row is None:
            await ctx.reply_error("Run this inside a Disco chat thread to save it.")
            return
        if not chat_threads_svc.can_manage_thread(ctx.author, int(row["owner_id"])):
            await ctx.reply_error("Only the thread owner or a mod can save this thread.")
            return
        result = await chat_threads_svc.save_thread(self.bot, ctx.channel.id)
        if result is None:
            await ctx.reply_error("This thread isn't a Disco chat thread.")
            return
        await ctx.send(embed=self._save_embed(ctx, result))

    @thread_group.command(name="unsave")
    @guild_only
    async def thread_unsave(self, ctx: DiscoContext) -> None:
        """Drop the current thread's saved state and recall code."""
        row = await self._active_thread_row(ctx)
        if row is None:
            await ctx.reply_error("Run this inside a Disco chat thread to unsave it.")
            return
        if not chat_threads_svc.can_manage_thread(ctx.author, int(row["owner_id"])):
            await ctx.reply_error("Only the thread owner or a mod can unsave this thread.")
            return
        result = await chat_threads_svc.unsave_thread(self.bot, ctx.channel.id)
        if result is None or not result["was_saved"]:
            await ctx.reply_error("This thread isn't saved.")
            return
        await ctx.reply_success(
            f"Unsaved this thread. Recall code `{result['token']}` no longer "
            "resolves, and the thread can be auto-deleted after 12h idle.",
            title="Thread unsaved",
        )

    @thread_group.command(name="list")
    @guild_only
    async def thread_list(self, ctx: DiscoContext) -> None:
        """List the chat threads you've saved in this server."""
        rows = await chat_threads_svc.list_saved_threads(
            self.bot.db, ctx.guild.id, ctx.author.id
        )
        embed = chat_threads_svc.build_saved_list_embed(
            rows, owner_name=ctx.author.display_name
        )
        await ctx.send(embed=embed)

    @thread_group.command(name="find")
    @guild_only
    async def thread_find(self, ctx: DiscoContext, code: str) -> None:
        """Find a saved thread and link you to the original -- never makes a new one."""
        recalled = await chat_threads_svc.recall_thread(self.bot.db, code)
        if recalled is None:
            await ctx.reply_error(
                f"No saved thread with code `{code}`. Check `{ctx.clean_prefix}thread list`."
            )
            return
        tok = recalled["token"]
        thread = await chat_threads_svc.bump_thread(
            self.bot, int(recalled["thread_id"])
        )
        if thread is None:
            # The Discord thread is gone; surface the saved summary instead.
            await ctx.send(embed=chat_threads_svc.build_recall_summary_embed(recalled))
            return
        await ctx.reply_success(f"Found saved thread `{tok}`: {thread.mention}")

    @thread_group.command(name="link")
    @guild_only
    async def thread_link(self, ctx: DiscoContext, target: str) -> None:
        """Merge another thread's context in, by recall code or thread id."""
        row = await self._active_thread_row(ctx)
        if row is None:
            await ctx.reply_error(
                "Run this inside a Disco chat thread to link another thread into it."
            )
            return
        if not chat_threads_svc.can_manage_thread(ctx.author, int(row["owner_id"])):
            await ctx.reply_error("Only the thread owner or a mod can link threads here.")
            return
        # Accepts a recall code OR a thread id/mention. A thread named by id
        # that has not been saved yet is auto-saved here, so users never
        # need a separate ,thread save step just to link.
        recalled = await chat_threads_svc.resolve_link_target(
            self.bot.db, ctx.guild.id, target,
        )
        if recalled is None:
            await ctx.reply_error(
                f"No Disco thread matches `{target}`. Use a recall code, or "
                f"the thread's id / mention. Check `{ctx.clean_prefix}thread list`."
            )
            return
        ok, reason = await chat_threads_svc.link_thread(
            self.bot,
            source_thread_id=ctx.channel.id,
            guild_id=ctx.guild.id,
            recalled=recalled,
            user_id=ctx.author.id,
        )
        text = chat_threads_svc.link_reply_text(ok, reason, recalled["token"])
        if ok:
            await ctx.reply_success(text, title="Thread merged")
        else:
            await ctx.reply_error(text)

    @thread_group.command(name="unlink")
    @guild_only
    async def thread_unlink(self, ctx: DiscoContext, code: str) -> None:
        """Remove a merged thread (`<code>`), group (`<#>`), or `all`."""
        row = await self._active_thread_row(ctx)
        if row is None:
            await ctx.reply_error(
                "Run this inside a Disco chat thread to unlink something."
            )
            return
        if not chat_threads_svc.can_manage_thread(ctx.author, int(row["owner_id"])):
            await ctx.reply_error("Only the thread owner or a mod can unlink here.")
            return
        arg = code.strip()
        if arg.lower() == "all":
            n = await chat_threads_svc.unlink_all(self.bot, ctx.channel.id)
            if n == 0:
                await ctx.reply_error("This thread has nothing merged in.")
            else:
                await ctx.reply_success(
                    f"Unmerged {n} link(s). Those slots are free again.",
                    title="Links cleared",
                )
            return
        if arg.isdigit():
            removed = await chat_threads_svc.unlink_group(
                self.bot, ctx.channel.id, int(arg)
            )
            if removed:
                await ctx.reply_success(
                    f"Unmerged group `{arg}`. That group slot is free again.",
                    title="Group unlinked",
                )
            else:
                await ctx.reply_error(f"No group linked here with number `{arg}`.")
            return
        removed = await chat_threads_svc.unlink_thread(self.bot, ctx.channel.id, arg)
        if removed:
            await ctx.reply_success(
                f"Unmerged `{arg}`. That thread slot is free again.",
                title="Thread unlinked",
            )
        else:
            await ctx.reply_error(f"No thread linked here with code `{arg}`.")

    @thread_group.command(name="links")
    @guild_only
    async def thread_links(self, ctx: DiscoContext) -> None:
        """Show the threads and groups merged into the current thread."""
        row = await self._active_thread_row(ctx)
        if row is None:
            await ctx.reply_error(
                "Run this inside a Disco chat thread to see its merged links."
            )
            return
        thread_links = await chat_threads_svc.thread_link_rows(
            self.bot.db, ctx.channel.id
        )
        group_links = await chat_threads_svc.group_link_rows(
            self.bot.db, ctx.channel.id
        )
        resolved = await chat_threads_svc.resolve_linked_thread_rows(
            self.bot.db, ctx.channel.id
        )
        await ctx.send(
            embed=chat_threads_svc.build_links_embed(
                thread_links, group_links, resolved
            )
        )

    @thread_group.command(name="push")
    @guild_only
    async def thread_push(self, ctx: DiscoContext, code: str) -> None:
        """Permanently merge this thread into a linked one, then close it."""
        row = await self._active_thread_row(ctx)
        if row is None:
            await ctx.reply_error(
                "Run this inside a Disco chat thread to push it."
            )
            return
        if not chat_threads_svc.can_manage_thread(ctx.author, int(row["owner_id"])):
            await ctx.reply_error("Only the thread owner or a mod can push this thread.")
            return
        ok, reason, target_id = await chat_threads_svc.push_thread(
            self.bot,
            source_thread_id=ctx.channel.id,
            target_token=code,
            user_id=ctx.author.id,
        )
        if ok:
            # The source thread is closed by the push, so the confirmation
            # lands in the target thread the context was merged into.
            embed = card(
                "Thread pushed in",
                color=C_SUCCESS,
                description=(
                    f"{ctx.author.mention} pushed a thread into this one. "
                    "Its conversation has been summarised and merged here, "
                    "and the source thread is now closed."
                ),
            ).build()
            target = self.bot.get_channel(int(target_id))
            if target is None:
                try:
                    target = await self.bot.fetch_channel(int(target_id))
                except discord.HTTPException:
                    target = None
            if target is not None:
                try:
                    await target.send(embed=embed)
                except discord.HTTPException:
                    pass
            return
        msgs = {
            "no_source": "This isn't a Disco chat thread.",
            "no_target": f"No saved thread with code `{code}`.",
            "self": "You can't push a thread into itself.",
            "not_linked": (
                f"You can only push into a thread you've linked. Run "
                f"`{ctx.clean_prefix}thread link {code}` here first."
            ),
        }
        await ctx.reply_error(msgs.get(reason, "Couldn't push this thread."))

    @thread_group.group(
        name="group", aliases=["groups"], invoke_without_command=True,
    )
    @guild_only
    async def thread_groups(self, ctx: DiscoContext) -> None:
        """Thread groups: webs of linked threads you can merge in at once."""
        rows = await chat_threads_svc.list_user_groups(
            self.bot.db, ctx.guild.id, ctx.author.id
        )
        await ctx.send(
            embed=chat_threads_svc.build_groups_embed(
                rows, owner_name=ctx.author.display_name
            )
        )

    @thread_groups.command(name="list")
    @guild_only
    async def thread_group_list(self, ctx: DiscoContext) -> None:
        """List the thread groups you take part in."""
        rows = await chat_threads_svc.list_user_groups(
            self.bot.db, ctx.guild.id, ctx.author.id
        )
        await ctx.send(
            embed=chat_threads_svc.build_groups_embed(
                rows, owner_name=ctx.author.display_name
            )
        )

    @thread_groups.command(name="link")
    @guild_only
    async def thread_group_link(self, ctx: DiscoContext, group_id: int) -> None:
        """Merge an entire thread group's context into the current thread."""
        row = await self._active_thread_row(ctx)
        if row is None:
            await ctx.reply_error(
                "Run this inside a Disco chat thread to merge a group into it."
            )
            return
        if not chat_threads_svc.can_manage_thread(ctx.author, int(row["owner_id"])):
            await ctx.reply_error("Only the thread owner or a mod can link groups here.")
            return
        ok, reason = await chat_threads_svc.link_group(
            self.bot,
            source_thread_id=ctx.channel.id,
            guild_id=ctx.guild.id,
            group_id=group_id,
            user_id=ctx.author.id,
        )
        text = chat_threads_svc.group_link_reply_text(ok, reason, group_id)
        if ok:
            await ctx.reply_success(text, title="Group linked")
        else:
            await ctx.reply_error(text)

    @thread_group.command(name="ctx", aliases=["context"])
    @guild_only
    async def thread_ctx(self, ctx: DiscoContext) -> None:
        """Dump the current thread's full conversation context."""
        row = await self._active_thread_row(ctx)
        if row is None:
            await ctx.reply_error(
                "Run this inside a Disco chat thread to dump its context."
            )
            return
        transcript = await chat_threads_svc.get_thread_context(
            self.bot.db, int(row["guild_id"]), row["history_key"]
        )
        await ctx.send(
            embed=chat_threads_svc.build_context_embed(transcript, row.get("title"))
        )

    @thread_group.command(name="close")
    @guild_only
    async def thread_close(self, ctx: DiscoContext, scope: str | None = None) -> None:
        """Close the current thread, or `all` of your open Disco threads."""
        if scope and scope.strip().lower() == "all":
            rows = await self.bot.db.fetch_all(
                "SELECT * FROM chat_threads "
                "WHERE guild_id=$1 AND owner_id=$2 AND status='active'",
                ctx.guild.id, ctx.author.id,
            )
            if not rows:
                await ctx.reply_error("You have no open Disco threads in this server.")
                return
            for r in rows:
                try:
                    await chat_threads_svc.close_thread(
                        self.bot, dict(r),
                        reason=f"Closed via ,thread close all by {ctx.author}",
                    )
                except Exception:
                    log.warning(
                        "[chat_threads] close-all failed for %s",
                        r.get("thread_id"), exc_info=True,
                    )
            await ctx.reply_success(
                f"Closed {len(rows)} of your Disco thread(s).", title="Threads closed"
            )
            return
        row = await self._active_thread_row(ctx)
        if row is None:
            await ctx.reply_error(
                "Run this inside a Disco chat thread to close it "
                f"(or `{ctx.clean_prefix}thread close all`)."
            )
            return
        if not chat_threads_svc.can_manage_thread(ctx.author, int(row["owner_id"])):
            await ctx.reply_error("Only the thread owner or a mod can close this thread.")
            return
        await ctx.reply_success("Closing this thread now.")
        await chat_threads_svc.close_thread(
            self.bot, dict(row), reason=f"Closed via ,thread close by {ctx.author}"
        )

    @staticmethod
    def _save_embed(ctx: DiscoContext, result: dict) -> discord.Embed:
        tok = result["token"]
        title = "Thread already saved" if result["already_saved"] else "Thread saved"
        return (
            card(title, color=C_SUCCESS)
            .description(
                "This conversation is stored. Disco will keep it even after the "
                "thread is deleted."
            )
            .field("Recall code", f"`{tok}`", True)
            .field(
                "Recall it later",
                f"`{ctx.clean_prefix}thread find {tok}` jumps to it; "
                f"`{ctx.clean_prefix}thread link {tok}` pulls it into another thread.",
                False,
            )
            .build()
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(ChatThreads(bot))
