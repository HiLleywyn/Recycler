"""cogs/approvals.py  -  agent-tool approval UI.

A DANGER-risk tool call (or a MUTATE call invoked by an agent actor)
returns ``approval_required`` from the tool framework. The AI bridge
persists an ``agent_approvals`` row via :func:`request_approval` and
surfaces an ``approval_required`` event, which :class:`Help` turns
into a card + :class:`ApprovalView` posted to the channel.

This cog provides:

  - :class:`ApprovalView`       Approve / Deny button view.
  - ``.approve <id>``           prefix command to approve by id.
  - ``.deny <id>``              prefix command to deny by id.
  - ``.approvals``              list the caller's pending approvals.

On approval, the tool is re-invoked through :func:`run_tool` with a
fresh :class:`ToolContext` that has ``approved=True``, so the executor
lets the handler run this time. The resulting :class:`ToolResult` is
rendered back into the original card (or posted as a follow-up when
invoked via slash/prefix) so the user can see the outcome.
"""
from __future__ import annotations

import json as _json
import logging

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only
from core.framework.ui import C_ERROR, C_NEUTRAL, C_SUCCESS, C_WARNING, fmt_ts

log = logging.getLogger(__name__)


# ── View ─────────────────────────────────────────────────────────────────────


class ApprovalView(discord.ui.View):
    """Approve / Deny buttons for a pending ``agent_approvals`` row.

    Only the user who originally triggered the tool call may click.
    The view times out after 10 minutes to match the DB row expiry.
    """

    def __init__(
        self,
        *,
        bot: Discoin,
        approval_id: int,
        author_id: int,
        tool_name: str,
        args: dict,
    ) -> None:
        super().__init__(timeout=600.0)
        self.bot = bot
        self.approval_id = int(approval_id)
        self.author_id = int(author_id)
        self.tool_name = str(tool_name)
        self.args = dict(args)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the player who triggered this call can decide on it.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="agent_approval_approve",
    )
    async def _approve(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()
        outcome = await _resolve_approval(
            self.bot,
            approval_id=self.approval_id,
            decider_id=interaction.user.id,
            guild_id=interaction.guild_id or 0,
            tool_name=self.tool_name,
            args=self.args,
            approve=True,
        )
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        embed = _outcome_embed(outcome, tool_name=self.tool_name)
        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.HTTPException:
            try:
                await interaction.followup.send(embed=embed)
            except Exception:
                pass

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        custom_id="agent_approval_deny",
    )
    async def _deny(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()
        outcome = await _resolve_approval(
            self.bot,
            approval_id=self.approval_id,
            decider_id=interaction.user.id,
            guild_id=interaction.guild_id or 0,
            tool_name=self.tool_name,
            args=self.args,
            approve=False,
        )
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        embed = _outcome_embed(outcome, tool_name=self.tool_name)
        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.HTTPException:
            try:
                await interaction.followup.send(embed=embed)
            except Exception:
                pass


# ── Shared resolution path ───────────────────────────────────────────────────


async def _resolve_approval(
    bot: Discoin,
    *,
    approval_id: int,
    decider_id: int,
    guild_id: int,
    tool_name: str | None,
    args: dict | None,
    approve: bool,
) -> dict:
    """Mark an approval row decided and (if approved) re-run the tool.

    Returns an outcome dict:

        {"status": "approved" | "denied" | "expired" | "error" | "unknown_tool",
         "tool": <tool name>,
         "result": <ToolResult dict> | None,
         "error": <str> | None}
    """
    try:
        from core.framework.agent_tools import (
            ToolContext,
            decide_approval,
            run_tool,
        )
    except Exception as exc:
        log.warning("[approvals] framework import failed: %s", exc)
        return {"status": "error", "tool": tool_name or "?", "error": str(exc)}

    # Look up the row so we always have canonical tool/args/reason, even
    # when the caller used the ,approve <id> command without the view.
    row = await bot.db.fetch_one(
        "SELECT tool, args, reason, guild_id, user_id, status, "
        "EXTRACT(EPOCH FROM (expires_at - NOW())) AS ttl "
        "FROM agent_approvals WHERE id=$1",
        int(approval_id),
    )
    if row is None:
        return {
            "status": "expired",
            "tool": tool_name or "?",
            "error": f"approval {approval_id} not found",
        }
    if str(row.get("status") or "") != "pending":
        return {
            "status": str(row.get("status") or "expired"),
            "tool": str(row.get("tool") or tool_name or "?"),
            "error": "approval is no longer pending",
        }
    if float(row.get("ttl") or 0) <= 0:
        return {
            "status": "expired",
            "tool": str(row.get("tool") or tool_name or "?"),
            "error": "approval expired",
        }

    row_tool = str(row.get("tool") or "")
    raw_args = row.get("args") or {}
    if isinstance(raw_args, str):
        try:
            row_args = _json.loads(raw_args or "{}")
        except Exception:
            row_args = {}
    else:
        row_args = dict(raw_args)

    changed = await decide_approval(
        bot.db,
        approval_id=int(approval_id),
        decider_id=int(decider_id),
        approve=approve,
    )
    if not changed:
        return {
            "status": "expired",
            "tool": row_tool,
            "error": "approval could not be updated (expired or already decided)",
        }

    if not approve:
        return {"status": "denied", "tool": row_tool, "result": None}

    # Approved -- re-run the tool with ctx.approved=True so the executor
    # lets the handler go through.
    tool_ctx = ToolContext(
        user_id=int(row.get("user_id") or decider_id),
        guild_id=int(row.get("guild_id") or guild_id),
        db=bot.db,
        bus=getattr(bot, "bus", None),
        actor="user",
        approved=True,
        dry_run=False,
    )
    try:
        result = await run_tool(row_tool, tool_ctx, row_args)
    except Exception as exc:
        log.exception("[approvals] re-run crashed for %s", row_tool)
        return {
            "status": "error",
            "tool": row_tool,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "status": "approved",
        "tool": row_tool,
        "result": {
            "ok": bool(result.ok),
            "data": result.data,
            "error": result.error,
            "meta": dict(result.meta or {}),
        },
    }


def _outcome_embed(outcome: dict, *, tool_name: str) -> discord.Embed:
    """Render an approval outcome as a card embed."""
    status = str(outcome.get("status") or "unknown")
    tool = str(outcome.get("tool") or tool_name or "?")

    if status == "approved":
        result = outcome.get("result") or {}
        ok = bool(result.get("ok"))
        color = C_SUCCESS if ok else C_ERROR
        title = f"Approved: {tool}" if ok else f"Approved but tool failed: {tool}"
        builder = card(title, color=color)
        if ok:
            data = result.get("data") or {}
            try:
                preview = _json.dumps(data, default=str, indent=2)
            except Exception:
                preview = str(data)
            if len(preview) > 1000:
                preview = preview[:990] + "\n...[truncated]"
            builder = builder.field(
                "Result",
                f"```json\n{preview}\n```" if preview.strip() else "_no data_",
                inline=False,
            )
        else:
            builder = builder.field(
                "Error",
                f"`{result.get('error') or 'unknown error'}`",
                inline=False,
            )
        return builder.build()

    if status == "denied":
        return card(
            f"Denied: {tool}",
            description="The tool call was not executed.",
            color=C_NEUTRAL,
        ).build()

    if status == "expired":
        return card(
            f"Approval expired: {tool}",
            description=str(outcome.get("error") or "too late to decide"),
            color=C_WARNING,
        ).build()

    return card(
        f"Approval failed: {tool}",
        description=str(outcome.get("error") or "unknown error"),
        color=C_ERROR,
    ).build()


# ── Cog + prefix commands ────────────────────────────────────────────────────


class Approvals(commands.Cog):
    """Prefix commands for interacting with pending agent tool approvals."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.command(name="approve")
    @guild_only
    async def approve_cmd(
        self, ctx: DiscoContext, approval_id: int | None = None,
    ) -> None:
        """Approve a pending agent tool call by id.

        Usage: ``,approve <id>``  -  the id is shown in the approval
        card the AI posts when a DANGER tool wants to run.
        """
        if approval_id is None:
            await ctx.reply_error_hint(
                "Missing approval id.",
                hint=f"{ctx.prefix}approve <id>",
                command_name="approvals",
            )
            return
        outcome = await _resolve_approval(
            self.bot,
            approval_id=int(approval_id),
            decider_id=int(ctx.author.id),
            guild_id=int(ctx.guild_id),
            tool_name=None,
            args=None,
            approve=True,
        )
        await ctx.reply(embed=_outcome_embed(outcome, tool_name="?"), mention_author=False)

    @commands.command(name="deny")
    @guild_only
    async def deny_cmd(
        self, ctx: DiscoContext, approval_id: int | None = None,
    ) -> None:
        """Deny a pending agent tool call by id.

        Usage: ``,deny <id>``
        """
        if approval_id is None:
            await ctx.reply_error_hint(
                "Missing approval id.",
                hint=f"{ctx.prefix}deny <id>",
                command_name="approvals",
            )
            return
        outcome = await _resolve_approval(
            self.bot,
            approval_id=int(approval_id),
            decider_id=int(ctx.author.id),
            guild_id=int(ctx.guild_id),
            tool_name=None,
            args=None,
            approve=False,
        )
        await ctx.reply(embed=_outcome_embed(outcome, tool_name="?"), mention_author=False)

    @commands.command(name="approvals")
    @guild_only
    async def approvals_cmd(self, ctx: DiscoContext) -> None:
        """List your pending agent tool approvals in this server."""
        rows = await ctx.db.fetch_all(
            """
            SELECT id, tool, reason,
                   EXTRACT(EPOCH FROM created_at) AS created_ts,
                   EXTRACT(EPOCH FROM expires_at) AS expires_ts
            FROM agent_approvals
            WHERE guild_id = $1
              AND user_id  = $2
              AND status   = 'pending'
              AND expires_at > NOW()
            ORDER BY id DESC
            LIMIT 10
            """,
            int(ctx.guild_id), int(ctx.author.id),
        )
        if not rows:
            await ctx.reply(
                embed=card(
                    "No pending approvals",
                    description="You have nothing waiting on your decision.",
                    color=C_NEUTRAL,
                ).build(),
                mention_author=False,
            )
            return

        builder = card(
            "Pending approvals",
            description=(
                f"Decide with `{ctx.prefix}approve <id>` or "
                f"`{ctx.prefix}deny <id>` (or click the buttons on the "
                "approval card when the AI posts one)."
            ),
            color=C_WARNING,
        ).footer(f"Showing up to 10 pending approvals  -  {ctx.author.display_name}")
        for r in rows:
            reason = str(r.get("reason") or "").strip() or "no reason given"
            if len(reason) > 300:
                reason = reason[:290] + "..."
            created = fmt_ts(float(r.get("created_ts") or 0))
            builder = builder.field(
                f"#{int(r['id'])}  -  {r['tool']}",
                f"{reason}\n_submitted {created}_",
                inline=False,
            )
        await ctx.reply(embed=builder.build(), mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Approvals(bot))
