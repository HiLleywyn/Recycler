"""
Report / Ticket System
======================

Users:   /report <category> <message>
         Categories: bugs, suggestions, users, other
Admins:  -admin reports                  (all reports)
         -admin reports CATEGORY         (filter by category)
         -admin reports STATUS           (filter by status)
         -admin reports CATEGORY STATUS  (filter by both)
         -admin reports search @user     (reports by user)
         -admin reports search NUMBER    (specific report)

Lifecycle:  open -> accepted / rejected
            accepted -> in_progress -> resolved -> closed

Each status change DMs the reporter and updates the admin's DM embed.
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from database.reports import VALID_CATEGORIES
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_AMBER, C_ERROR, C_GOLD, C_INFO, C_NEUTRAL, C_SUCCESS, C_WARNING, FormatKit, fmt_ts, mention, Paginator,
)

log = logging.getLogger(__name__)


def _verdict_is_actionable(verdict: str) -> bool:
    """Decide whether the AI realness verdict looks confident enough that we
    should hand it off to the auto-fix pipeline.

    The realness prompt returns a structured block whose first line starts
    ``Verdict: <real | likely_real | suspicious | likely_fake | spam>`` and
    whose third line starts ``Confidence: <low | medium | high>``. We
    require the verdict word to be one of {real, likely_real} AND the
    confidence to be at least medium. Anything weaker stays in the human
    triage queue (the buttons are always there).
    """
    if not verdict:
        return False
    text = verdict.lower()
    verdict_ok = any(
        f"verdict: {v}" in text for v in ("real", "likely_real")
    )
    if not verdict_ok:
        return False
    confidence_low = "confidence: low" in text
    return not confidence_low


def _verdict_is_rejectable(verdict: str) -> bool:
    """True only for ``spam`` / ``likely_fake`` AT HIGH CONFIDENCE.

    Auto-rejecting at medium would close out 'I think this might be a
    bug but I'm not sure' reports submitted by frustrated players, which
    is the wrong default. Anything weaker stays open for human triage.
    """
    if not verdict:
        return False
    text = verdict.lower()
    is_bad = any(
        f"verdict: {v}" in text for v in ("spam", "likely_fake")
    )
    return is_bad and "confidence: high" in text


def _verdict_short_reason(verdict: str) -> str:
    """Extract the AI's reasoning line for use as an admin_note when the
    auto-close path fires. Returns the line after ``Reasoning:`` or a
    one-line fallback. Capped at 800 chars (DB column has plenty but
    embed display is the bottleneck).
    """
    if not verdict:
        return ""
    for raw in verdict.splitlines():
        line = raw.strip()
        if line.lower().startswith("reasoning:"):
            return line.split(":", 1)[1].strip()[:800]
    return verdict.strip().splitlines()[0][:800] if verdict.strip() else ""


# ── Status config ─────────────────────────────────────────────────────────────

_REPORT_COOLDOWN = 300  # 5 minutes between reports

STATUSES: dict[str, tuple[str, str, int]] = {
    "open":        ("Open",        "📩", C_INFO),
    "accepted":    ("Accepted",    "✅", C_SUCCESS),
    "rejected":    ("Rejected",    "❌", C_ERROR),
    "in_progress": ("In Progress", "🔧", C_WARNING),
    "resolved":    ("Resolved",    "✅", C_SUCCESS),
    "closed":      ("Closed",      "🔒", C_NEUTRAL),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

REPORT_TAGS: list[tuple[str, str]] = [
    ("high_priority", "🔴 High Priority"),
    ("low_priority", "🟡 Low Priority"),
    ("bug_confirmed", "🐛 Bug Confirmed"),
    ("wont_fix", "🚫 Won't Fix"),
    ("duplicate", "♻️ Duplicate"),
    ("ui", "🖥 UI Issue"),
    ("economy", "💰 Economy"),
    ("crash", "💥 Crash"),
    ("performance", "⚡ Performance"),
    ("feature_request", "✨ Feature Request"),
]
_TAG_LABELS: dict[str, str] = {k: v for k, v in REPORT_TAGS}


def _build_admin_embed(report: dict, user: discord.User | discord.Member | None = None) -> discord.Embed:
    """Build the admin-facing report embed."""
    label, emoji, color = STATUSES.get(report["status"], ("Unknown", "❓", C_NEUTRAL))
    _b = card(f"{emoji} Report #{report['id']}  -  {label}", color=color)

    if user:
        _b.author(f"{user.display_name} ({user.id})", icon_url=user.display_avatar.url)
    else:
        _b.author(f"User {report['user_id']}")

    cat = report.get("category", "other").capitalize()
    _b.field("Category", cat, True)
    _b.field("Status", f"**{label}**", True)

    tags_raw = report.get("tags", "") or ""
    if tags_raw:
        tag_list = [_TAG_LABELS.get(t.strip(), t.strip()) for t in tags_raw.split(",") if t.strip()]
        _b.field("Tags", "  ".join(tag_list), True)

    _b.field("Report", report["message"][:1024], False)

    if report.get("admin_note"):
        _b.field("Admin Note", report["admin_note"][:1024], False)

    reward = to_human(int(report.get("reward_amount") or 0))
    if reward > 0:
        _b.field("Reward Paid", f"**${reward:,.2f}**", True)

    _ca = report["created_at"]
    _ca_ts = _ca.timestamp() if hasattr(_ca, 'timestamp') else _ca
    age = int(time.time() - _ca_ts)
    _b.footer(f"Submitted {FormatKit.time_ago(age)}  |  Report #{report['id']}")
    return _b.build()


def _build_user_notification(report: dict, status: str, note: str = "") -> discord.Embed:
    """Build the DM sent to the reporter on status change."""
    label, emoji, color = STATUSES.get(status, ("Unknown", "❓", C_NEUTRAL))
    desc = f"Your report **#{report['id']}** has been marked as **{label}**."
    if note:
        desc += f"\n\n**Admin note:** {note}"
    desc += f"\n\n*Original message:* {report['message'][:500]}"
    return card(f"{emoji} Report Update", description=desc, color=color).build()


async def _fetch_admin(bot: Discoin) -> discord.User | None:
    """Fetch the user who receives new report DMs.

    Uses the bot_config 'report_dm_recipient_id' override if set and non-zero,
    otherwise falls back to Config.REPORT_TARGET_USER_ID.
    """
    target_id = 0
    try:
        val = await bot.db.get_bot_config("report_dm_recipient_id")
        target_id = int(val) if val else 0
    except Exception:
        pass
    if not target_id:
        target_id = Config.REPORT_TARGET_USER_ID
    if not target_id:
        return None
    try:
        return bot.get_user(target_id) or await bot.fetch_user(target_id)
    except Exception:
        return None


async def _notify_reporter(bot: Discoin, user_id: int, report: dict, status: str, note: str = "") -> None:
    """DM the reporter about a status change. Silently fails if DMs are off."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        embed = _build_user_notification(report, status, note)
        await user.send(embed=embed)
    except Exception:
        pass


async def _post_to_reports_feed(
    bot: Discoin,
    report: dict,
    event: str = "status_update",
    user: discord.User | discord.Member | None = None,
) -> None:
    """Post a report event to the guild's reports feed channel.

    event: 'new_report' | 'status_update' | 'report_edited'
    """
    guild_id = report["guild_id"]
    try:
        settings = await bot.db.get_guild_settings(guild_id)
    except Exception:
        return
    ch_id = settings.get("reports_feed_channel")
    if not ch_id:
        return
    # Check category filter (strip whitespace  -  user may type "bugs, suggestions")
    allowed_cats = {c.strip() for c in (settings.get("reports_feed_categories") or "bugs,suggestions,users,other").split(",") if c.strip()}
    if report.get("category", "other") not in allowed_cats:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    channel = guild.get_channel_or_thread(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    label, emoji, color = STATUSES.get(report["status"], ("Unknown", "❓", C_NEUTRAL))
    if event == "new_report":
        title = f"📋 New Report #{report['id']}"
        desc = f"**Category:** {report.get('category', 'other').capitalize()}\n"
        desc += f"**Status:** {emoji} {label}\n"
        if user:
            desc += f"**Reporter:** {user.mention}\n"
        desc += f"\n> {report['message'][:500]}"
    elif event == "report_edited":
        title = f"✏️ Report #{report['id']}  -  Edited"
        desc = f"**Category:** {report.get('category', 'other').capitalize()}\n"
        desc += f"**Status:** {emoji} {label}\n"
        if user:
            desc += f"**Edited by:** {user.mention}\n"
        desc += f"\n> {report['message'][:500]}"
    else:
        title = f"{emoji} Report #{report['id']}  -  {label}"
        desc = f"**Category:** {report.get('category', 'other').capitalize()}\n"
        desc += f"**Status changed to:** {emoji} {label}\n"
        if report.get("admin_note"):
            desc += f"**Note:** {report['admin_note'][:500]}\n"
        desc += f"\n> {report['message'][:300]}"
    embed = card(title, description=desc, color=color).build()
    try:
        await channel.send(embed=embed)
    except Exception:
        pass


async def _edit_admin_dm(bot: Discoin, report: dict, view: discord.ui.View | None) -> None:
    """Edit the admin's DM embed for this report in-place."""
    if not report.get("dm_message_id"):
        return
    admin = await _fetch_admin(bot)
    if not admin:
        return
    try:
        dm_channel = admin.dm_channel or await admin.create_dm()
        msg = await dm_channel.fetch_message(report["dm_message_id"])
        user = bot.get_user(report["user_id"])
        embed = _build_admin_embed(report, user)
        await msg.edit(embed=embed, view=view)
    except Exception:
        pass


# ── Views ─────────────────────────────────────────────────────────────────────

class ReportTagSelect(discord.ui.Select):
    """Admin-only dropdown to set tags on a report. Replaces any previous tags."""

    def __init__(self, report_id: int, bot: Discoin, current_tags: list[str]) -> None:
        self.report_id = report_id
        self.bot = bot
        options = [
            discord.SelectOption(
                label=label, value=key,
                default=(key in current_tags),
            )
            for key, label in REPORT_TAGS
        ]
        super().__init__(
            placeholder="Set admin tags…",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"report_tags:{report_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        report = await self.bot.db.reports.set_tags(self.report_id, self.values)
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        user = self.bot.get_user(report["user_id"])
        embed = _build_admin_embed(report, user)
        await interaction.response.edit_message(embed=embed)


class ReportTriageView(discord.ui.View):
    """Accept / Reject buttons shown on new reports."""

    def __init__(self, report_id: int, bot: Discoin) -> None:
        super().__init__(timeout=None)
        self.report_id = report_id
        self.bot = bot
        # Persistent custom_ids so buttons survive bot restarts
        self.accept_btn = discord.ui.Button(
            label="Accept", style=discord.ButtonStyle.success,
            custom_id=f"report_triage:{report_id}:accept",
        )
        self.reject_btn = discord.ui.Button(
            label="Reject", style=discord.ButtonStyle.danger,
            custom_id=f"report_triage:{report_id}:reject",
        )
        self.msg_btn = discord.ui.Button(
            label="Message Reporter", style=discord.ButtonStyle.secondary,
            custom_id=f"report_triage:{report_id}:message",
            emoji="💬",
        )
        self.accept_btn.callback = self._accept
        self.reject_btn.callback = self._reject
        self.msg_btn.callback = self._message
        self.add_item(self.accept_btn)
        self.add_item(self.reject_btn)
        self.add_item(self.msg_btn)

    async def _accept(self, interaction: discord.Interaction) -> None:
        report = await self.bot.db.reports.update_status(self.report_id, "accepted")
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        await _notify_reporter(self.bot, report["user_id"], report, "accepted")
        await _post_to_reports_feed(self.bot, report)
        current_tags = [t for t in (report.get("tags") or "").split(",") if t]
        manage_view = ReportManageView(self.report_id, self.bot, current_tags)
        user = self.bot.get_user(report["user_id"])
        embed = _build_admin_embed(report, user)
        await interaction.response.edit_message(embed=embed, view=manage_view)

    async def _reject(self, interaction: discord.Interaction) -> None:
        report = await self.bot.db.reports.update_status(self.report_id, "rejected")
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        await _notify_reporter(self.bot, report["user_id"], report, "rejected")
        await _post_to_reports_feed(self.bot, report)
        user = self.bot.get_user(report["user_id"])
        embed = _build_admin_embed(report, user)
        await interaction.response.edit_message(embed=embed, view=None)

    async def _message(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ReportMessageModal(self.report_id, self.bot))


class ReportManageView(discord.ui.View):
    """In Progress / Resolve / Close + tag select + message reporter, shown after accepting."""

    def __init__(self, report_id: int, bot: Discoin, current_tags: list[str] | None = None) -> None:
        super().__init__(timeout=None)
        self.report_id = report_id
        self.bot = bot

        self.progress_btn = discord.ui.Button(
            label="In Progress", style=discord.ButtonStyle.primary,
            custom_id=f"report_manage:{report_id}:in_progress",
        )
        self.resolve_btn = discord.ui.Button(
            label="Resolve", style=discord.ButtonStyle.success,
            custom_id=f"report_manage:{report_id}:resolve",
        )
        self.close_btn = discord.ui.Button(
            label="Close", style=discord.ButtonStyle.secondary,
            custom_id=f"report_manage:{report_id}:close",
        )
        self.msg_btn = discord.ui.Button(
            label="Message Reporter", style=discord.ButtonStyle.secondary,
            custom_id=f"report_manage:{report_id}:message",
            emoji="💬",
        )
        self.reward_btn = discord.ui.Button(
            label="Reward", style=discord.ButtonStyle.success,
            custom_id=f"report_manage:{report_id}:reward",
            emoji="💰",
        )
        self.progress_btn.callback = self._in_progress
        self.resolve_btn.callback = self._resolve
        self.close_btn.callback = self._close
        self.msg_btn.callback = self._message
        self.reward_btn.callback = self._reward
        self.add_item(self.progress_btn)
        self.add_item(self.resolve_btn)
        self.add_item(self.close_btn)
        self.add_item(self.reward_btn)
        self.add_item(self.msg_btn)
        self.add_item(ReportTagSelect(report_id, bot, current_tags or []))

    async def _in_progress(self, interaction: discord.Interaction) -> None:
        report = await self.bot.db.reports.update_status(self.report_id, "in_progress")
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        await _notify_reporter(self.bot, report["user_id"], report, "in_progress")
        await _post_to_reports_feed(self.bot, report)
        user = self.bot.get_user(report["user_id"])
        embed = _build_admin_embed(report, user)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _resolve(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ReportNoteModal(self.report_id, self.bot))

    async def _close(self, interaction: discord.Interaction) -> None:
        report = await self.bot.db.reports.update_status(self.report_id, "closed")
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        await _notify_reporter(self.bot, report["user_id"], report, "closed")
        await _post_to_reports_feed(self.bot, report)
        user = self.bot.get_user(report["user_id"])
        embed = _build_admin_embed(report, user)
        await interaction.response.edit_message(embed=embed, view=None)

    async def _reward(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ReportRewardModal(self.report_id, self.bot, then_status="")
        )

    async def _message(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ReportMessageModal(self.report_id, self.bot))


class ReportNoteModal(discord.ui.Modal, title="Resolve Report"):
    """Modal for adding a resolution note when resolving a report."""

    note = discord.ui.TextInput(
        label="Resolution note (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
        placeholder="Describe what was done to resolve this...",
    )

    def __init__(self, report_id: int, bot: Discoin) -> None:
        super().__init__()
        self.report_id = report_id
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        note_text = self.note.value or ""
        report = await self.bot.db.reports.update_status(
            self.report_id, "resolved", admin_note=note_text or None,
        )
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        await _notify_reporter(self.bot, report["user_id"], report, "resolved", note_text)
        await _post_to_reports_feed(self.bot, report)
        user = self.bot.get_user(report["user_id"])
        embed = _build_admin_embed(report, user)
        current_tags = [t for t in (report.get("tags") or "").split(",") if t]
        close_view = _CloseOnlyView(self.report_id, self.bot, current_tags)
        await interaction.response.edit_message(embed=embed, view=close_view)


class ReportMessageModal(discord.ui.Modal, title="Message Reporter"):
    """Admin modal for sending a direct message to the report submitter."""

    message = discord.ui.TextInput(
        label="Message to reporter",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
        placeholder="Type your message to the reporter...",
    )

    def __init__(self, report_id: int, bot: Discoin) -> None:
        super().__init__()
        self.report_id = report_id
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        report = await self.bot.db.reports.get_report(self.report_id)
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return

        msg_text = self.message.value.strip()
        try:
            user = self.bot.get_user(report["user_id"]) or await self.bot.fetch_user(report["user_id"])
            embed = card(
                "💬 Message from Admin",
                description=(
                    f"An admin sent you a message regarding your report **#{report['id']}**:\n\n"
                    f"> {msg_text}\n\n"
                    f"*Original report:* {report['message'][:200]}{'...' if len(report['message']) > 200 else ''}"
                ),
                color=C_INFO,
            ).build()
            await user.send(embed=embed)
            await interaction.response.send_message(
                f"Message sent to {mention(report['user_id'], bot=self.bot)}.", ephemeral=True,
            )
        except (discord.Forbidden, discord.NotFound):
            await interaction.response.send_message(
                "Could not DM the reporter  -  they may have DMs disabled.", ephemeral=True,
            )


class ReportRewardModal(discord.ui.Modal, title="Reward Reporter"):
    """Admin modal for rewarding the reporter with coins on resolve/close."""

    amount = discord.ui.TextInput(
        label="Reward amount (USD)",
        style=discord.TextStyle.short,
        required=True,
        max_length=20,
        placeholder="e.g. 500",
    )

    def __init__(self, report_id: int, bot: Discoin, then_status: str = "") -> None:
        super().__init__()
        self.report_id = report_id
        self.bot = bot
        self.then_status = then_status  # optionally also set status after reward

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            reward = float(self.amount.value.replace(",", "").replace("$", "").strip())
        except ValueError:
            await interaction.response.send_message("Invalid amount.", ephemeral=True)
            return
        if reward <= 0:
            await interaction.response.send_message("Amount must be positive.", ephemeral=True)
            return

        report = await self.bot.db.reports.get_report(self.report_id)
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return

        # Credit the reporter's wallet (raw scaled int for DB)
        reward_raw = to_raw(reward)
        await self.bot.db.update_wallet(report["user_id"], report["guild_id"], reward_raw)
        await self.bot.db.reports.set_reward(self.report_id, reward_raw)

        # Check for matching bounty and apply bonus
        bounty = await self.bot.db.reports.get_matching_bounty(
            report["guild_id"], report.get("category", "other"),
        )
        bounty_bonus = 0.0
        if bounty:
            bounty_bonus_raw = int(bounty["reward_amount"] or 0)
            bounty_bonus = to_human(bounty_bonus_raw)
            await self.bot.db.update_wallet(report["user_id"], report["guild_id"], bounty_bonus_raw)
            await self.bot.db.reports.increment_bounty_claims(bounty["id"])

        # Optionally update status (resolve/close)
        if self.then_status:
            report = await self.bot.db.reports.update_status(self.report_id, self.then_status)
            await _post_to_reports_feed(self.bot, report)

        # DM the reporter about their reward
        total_reward = reward + bounty_bonus
        reward_msg = f"You received **${reward:,.2f}** for your report **#{report['id']}**."
        if bounty_bonus > 0:
            reward_msg += (
                f"\nPlus a **${bounty_bonus:,.2f}** bounty bonus for "
                f"\"{bounty['title']}\"!"
            )
        reward_msg += f"\n\nTotal: **${total_reward:,.2f}** added to your wallet."

        try:
            user = self.bot.get_user(report["user_id"]) or await self.bot.fetch_user(report["user_id"])
            embed = card(
                "Report Reward",
                description=reward_msg,
                color=C_SUCCESS,
            ).build()
            await user.send(embed=embed)
        except Exception:
            pass

        # Log transaction
        try:
            await self.bot.db.log_tx(
                report["guild_id"], report["user_id"], "REPORT_REWARD",
                symbol_out="USD", amount_out=total_reward,
            )
        except Exception:
            pass

        # Refresh admin embed
        report = await self.bot.db.reports.get_report(self.report_id)
        user_obj = self.bot.get_user(report["user_id"])
        embed = _build_admin_embed(report, user_obj)
        if self.then_status == "closed":
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            current_tags = [t for t in (report.get("tags") or "").split(",") if t]
            close_view = _CloseOnlyView(self.report_id, self.bot, current_tags)
            await interaction.response.edit_message(embed=embed, view=close_view)


class _AutoFixConfirmView(discord.ui.View):
    """Two-button gate that turns a stashed auto-fix proposal into a real PR.

    Non-persistent (timeout=86400, i.e. 24h). We don't reuse the
    persistent-view machinery because the proposal lives in memory on
    the cog -- it can't survive a bot restart anyway, so a button that
    only works on the live process matches the data lifetime.

    Open PR  -- pops the entry, calls cog._open_autofix_pr, replies
    with the PR URL or the failure reason.
    Discard  -- drops the entry without contacting GitHub.
    """

    def __init__(self, cog: "Report", report_id: int) -> None:
        super().__init__(timeout=86400)  # 24h, then buttons go inert
        self.cog = cog
        self.report_id = report_id

    async def _disable_all(self) -> None:
        for child in self.children:
            try:
                child.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass

    @discord.ui.button(label="Open PR", style=discord.ButtonStyle.success, emoji="\U0001F527")
    async def btn_open_pr(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=False)
        url, msg = await self.cog._open_autofix_pr(self.report_id)
        await self._disable_all()
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        if url:
            await interaction.followup.send(
                content=f"\U0001F527 {msg}",
            )
        else:
            await interaction.followup.send(
                content=f"⚠️ {msg}", ephemeral=True,
            )
        self.stop()

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary, emoji="\U0001F5D1")
    async def btn_discard(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        # Drop the patch without ever contacting GitHub. Idempotent if
        # someone double-clicks because pop() has a default. Mirror the
        # state to the queue row so the status command reflects reality.
        self.cog._pending_autofixes.pop(self.report_id, None)
        try:
            await self.cog.bot.db.reports.update_autofix_status(
                self.report_id, "discarded",
                last_error="Discarded by admin via DM button.",
            )
        except Exception:
            pass
        await self.cog._dm_status_update(self.report_id, "discarded")
        await self._disable_all()
        try:
            await interaction.response.edit_message(
                content=f"Patch for report #{self.report_id} discarded.",
                view=self,
            )
        except Exception:
            pass
        self.stop()

    async def on_timeout(self) -> None:
        # Drop the stash once the buttons go inert -- holding it longer
        # than the click window is just a memory leak.
        self.cog._pending_autofixes.pop(self.report_id, None)


class _CloseOnlyView(discord.ui.View):
    """Shown after resolving  -  Close button + message reporter + reward + tag select."""

    def __init__(self, report_id: int, bot: Discoin, current_tags: list[str] | None = None) -> None:
        super().__init__(timeout=None)
        self.report_id = report_id
        self.bot = bot

        close_btn = discord.ui.Button(
            label="Close", style=discord.ButtonStyle.secondary,
            custom_id=f"report_close:{report_id}",
        )
        close_btn.callback = self._close
        self.add_item(close_btn)

        reward_btn = discord.ui.Button(
            label="Reward & Close", style=discord.ButtonStyle.success,
            custom_id=f"report_reward_close:{report_id}",
            emoji="💰",
        )
        reward_btn.callback = self._reward_close
        self.add_item(reward_btn)

        msg_btn = discord.ui.Button(
            label="Message Reporter", style=discord.ButtonStyle.secondary,
            custom_id=f"report_close_msg:{report_id}",
            emoji="💬",
        )
        msg_btn.callback = self._message
        self.add_item(msg_btn)
        self.add_item(ReportTagSelect(report_id, bot, current_tags or []))

    async def _reward_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ReportRewardModal(self.report_id, self.bot, then_status="closed")
        )

    async def _close(self, interaction: discord.Interaction) -> None:
        report = await self.bot.db.reports.update_status(self.report_id, "closed")
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        await _notify_reporter(self.bot, report["user_id"], report, "closed")
        await _post_to_reports_feed(self.bot, report)
        user = self.bot.get_user(report["user_id"])
        embed = _build_admin_embed(report, user)
        await interaction.response.edit_message(embed=embed, view=None)

    async def _message(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ReportMessageModal(self.report_id, self.bot))


# ── Cog ───────────────────────────────────────────────────────────────────────

class Report(commands.Cog):
    """Report / Ticket system."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._report_locks: dict[tuple[int, int], asyncio.Lock] = {}
        # Stashed AI-authored patch proposals waiting on a human "Open PR"
        # click. Keyed by report id; entries are dropped on click, on
        # discard, on bot restart, or on the view's 24h timeout. Never
        # persisted -- the security model assumes "if the bot was down
        # when the admin clicked, regenerate."
        self._pending_autofixes: dict[int, dict] = {}
        # Worker lock so concurrent ticks can't double-process a row even
        # if the loop overruns its interval.
        self._autofix_worker_lock = asyncio.Lock()
        # Trace-DM dedupe state. Keyed by a fingerprint of the trace
        # (stage + raw error); each entry is
        #   {"first_at": epoch_seconds,
        #    "last_dm_at": epoch_seconds,
        #    "suppressed": int}
        # When a fingerprint repeats, we suppress the trace DM and bump
        # ``suppressed``; once an hour we emit ONE summary DM that says
        # "N more identical traces in the last hour" and reset.
        self._autofix_trace_seen: dict[str, dict] = {}
        self._autofix_trace_summary_window_s: int = 3600  # 1 hour
        self.daily_report_summary.start()
        self.cleanup_closed_report_dms.start()
        self.autofix_worker.start()
        self.autofix_pr_watcher.start()

    def cog_unload(self) -> None:
        self.daily_report_summary.cancel()
        self.cleanup_closed_report_dms.cancel()
        for loop in ("autofix_worker", "autofix_pr_watcher"):
            try:
                getattr(self, loop).cancel()
            except Exception:
                pass

    async def _auto_diagnose_report(
        self,
        report_row: dict,
        dm_msg: discord.Message,
        admin: discord.User | discord.Member,
        reporter: discord.User | discord.Member,
    ) -> None:
        """Run an AI realness check on a freshly-submitted report and append
        the verdict to the admin DM as a new field.

        Wrapped end-to-end in try/except: a provider hiccup must never
        surface as an error to the reporter (who already saw "submitted")
        nor break the admin DM (which already has the triage buttons).
        """
        try:
            from core.framework.ai.heal_ai import get_heal_ai_config
            from core.framework.ai import report_ai as _rai
            ai_cfg = await get_heal_ai_config(self.bot.db, int(report_row.get("guild_id") or 0))
            signals = await _rai.gather_signals(
                self.bot.db, int(report_row.get("guild_id") or 0), report_row,
            )
            verdict = await _rai.complete_report_diagnosis(
                report_row, signals, ai_cfg,
            )
            if not verdict:
                return
            # Re-fetch the embed off the live DM message so we don't clobber
            # any status changes the admin made between submission and the
            # AI round-trip completing.
            try:
                fresh = await dm_msg.channel.fetch_message(dm_msg.id)
                base = fresh.embeds[0] if fresh.embeds else _build_admin_embed(report_row, reporter)
            except Exception:
                base = _build_admin_embed(report_row, reporter)
            # Discord field value cap is 1024 chars; trim with a marker.
            value = verdict.strip()
            if len(value) > 980:
                value = value[:980] + "\n[...truncated]"
            base.add_field(
                name="\U0001F50D AI diagnosis (auto)",
                value=f"```\n{value}\n```",
                inline=False,
            )
            try:
                await dm_msg.edit(embed=base)
            except Exception:
                log.debug("auto-diagnose: DM edit failed", exc_info=True)
            # Tier-A auto-fix hand-off. Runs only when the toggle is on,
            # GitHub is configured, and the verdict actually says "real"
            # with non-low confidence. Anything weaker stays in human-
            # only triage so we never open a PR off a "suspicious" call.
            try:
                settings = await self.bot.db.get_guild_settings(
                    int(report_row.get("guild_id") or 0),
                )
            except Exception:
                settings = {}
            # Auto-close path. Only trips on spam / likely_fake AT HIGH
            # confidence -- everything weaker stays open for human review.
            if bool(settings.get("reports_auto_close")) and _verdict_is_rejectable(verdict):
                asyncio.create_task(
                    self._auto_close_report(
                        report_row, dm_msg, verdict,
                    )
                )
                # Don't ALSO try to auto-fix something we just rejected.
                return
            if bool(settings.get("reports_auto_fix")) and _verdict_is_actionable(verdict):
                asyncio.create_task(
                    self._auto_fix_report(
                        report_row, dm_msg, signals, ai_cfg, verdict,
                    )
                )
        except Exception:
            log.exception(
                "auto-diagnose: failed for report id=%s gid=%s",
                report_row.get("id"), report_row.get("guild_id"),
            )

    async def _auto_close_report(
        self,
        report_row: dict,
        dm_msg: discord.Message,
        verdict: str,
    ) -> None:
        """Auto-reject a spam/fake report and DM the recipient.

        Mirrors what an admin would do clicking Reject on the triage
        view: status -> ``rejected`` and an ``admin_note`` carrying the
        AI's reasoning so the trail is auditable. The reporter gets the
        same DM the manual reject path sends because the existing
        edit-DM helper picks it up off the new status.
        """
        report_id = int(report_row.get("id") or 0)
        guild_id  = int(report_row.get("guild_id") or 0)
        reason = _verdict_short_reason(verdict) or "AI flagged as spam / likely_fake."
        note = f"[auto-closed by AI] {reason}"
        try:
            updated = await self.bot.db.reports.update_status(
                report_id, "rejected", admin_note=note,
            )
        except Exception:
            log.exception(
                "auto-close: status update failed for report %s", report_id,
            )
            return
        await self._append_dm_field(
            dm_msg,
            "\U0001F6AB Auto-closed",
            f"_Report auto-rejected: {reason[:300]}_\n"
            "Triage buttons are still live -- click Accept if this was "
            "wrong and you want to reopen.",
        )
        # Refresh the admin DM embed colour / status badge by editing
        # with the fresh row, mirroring what ReportTriageView does on a
        # manual reject click.
        try:
            from cogs.report import _build_admin_embed  # type: ignore[reportMissingImports]
            fresh = await dm_msg.channel.fetch_message(dm_msg.id)
            embed = _build_admin_embed(updated or {}, None)
            await fresh.edit(embed=embed)
        except Exception:
            log.debug("auto-close: DM refresh failed", exc_info=True)
        # Lifecycle DM (separate from the admin DM, lands in the
        # recipient's regular DM channel).
        e = card(
            "\U0001F6AB Report auto-closed",
            color=C_ERROR,
            description=(
                f"Report **#{report_id}** auto-rejected by AI verdict.\n\n"
                f"**Reason:** {reason[:600]}"
            ),
        ).build()
        await self._dm_report_recipient(embed=e)
        del guild_id  # unused -- kept for grep parity with siblings

    async def _auto_fix_report(
        self,
        report_row: dict,
        dm_msg: discord.Message,
        signals: dict,
        ai_cfg: dict,
        verdict: str,
    ) -> None:
        """Tier-A: open a tracking issue + enqueue the report so the
        background worker drafts a patch, then DM the recipient at
        every step. The Open PR click is still the only place a real
        commit lands on GitHub.
        """
        del signals, ai_cfg  # the worker re-fetches both
        report_id = int(report_row.get("id") or 0)
        guild_id  = int(report_row.get("guild_id") or 0)
        try:
            from core.framework.ai import github_pr as _gh
            if not _gh.is_configured():
                await self._append_dm_field(
                    dm_msg,
                    "\U0001F527 Auto-fix",
                    "_Skipped: `GITHUB_TOKEN` / `AUTOFIX_REPO_OWNER` / "
                    "`AUTOFIX_REPO_NAME` not set in env._",
                )
                return

            # Enqueue first so the rest of the flow has something to
            # update. queue_autofix is upsert-with-reset so a second
            # report with the same id doesn't error.
            try:
                await self.bot.db.reports.queue_autofix(
                    report_id, guild_id, requested_by=int(report_row.get("user_id") or 0),
                )
            except Exception:
                log.exception("auto-fix: enqueue failed for report %s", report_id)
                return

            # Open a tracking issue immediately. We don't wait for the
            # patch -- having the issue link in DM-step-one is the whole
            # point of this branch.
            issue = await self._autofix_open_issue(
                dict(report_row), verdict=verdict, requested_by=int(report_row.get("user_id") or 0),
            )
            queue_row = await self.bot.db.reports.get_autofix_entry(report_id)
            if issue is not None:
                await self._dm_status_update(
                    report_id, "issue_opened", row=queue_row,
                )

            await self._append_dm_field(
                dm_msg,
                "\U0001F527 Auto-fix",
                "_Queued. The worker will draft a patch within 30s; "
                "you'll get a DM with Open PR / Discard buttons when "
                "it's ready. `,admin reports queue` shows status._",
            )
        except Exception:
            log.exception(
                "auto-fix: failed for report id=%s gid=%s",
                report_id, guild_id,
            )
            try:
                await self._append_dm_field(
                    dm_msg,
                    "\U0001F527 Auto-fix",
                    "_Internal error during auto-fix; check logs._",
                )
            except Exception:
                pass

    async def _open_autofix_pr(
        self, report_id: int,
    ) -> tuple[str | None, str]:
        """Take a stashed patch and ship it as a draft PR. Returns
        ``(url, message)``. ``url`` is None on failure and ``message``
        is a human-readable reason in either case.

        Persists the resulting state to ``report_autofix_queue`` and
        DMs the recipient so the admin's audit trail and inbox stay
        in lockstep with what's actually on GitHub.
        """
        from core.framework.ai import github_pr as _gh
        from core.framework.ai import auto_fix as _af
        entry = self._pending_autofixes.pop(report_id, None)
        if not entry:
            return (None, "Patch already used or expired -- run "
                    "`,admin reports autofix <id>` to regenerate.")
        proposal = entry["proposal"]
        report_row = entry["report_row"]
        verdict = entry.get("verdict") or ""
        guild_id = int(report_row.get("guild_id") or 0)

        # Pull the queue row to surface the tracking issue (if any)
        # so the PR body can close it via ``Closes #<n>``.
        queue_row = await self.bot.db.reports.get_autofix_entry(report_id)
        issue_num = (queue_row or {}).get("issue_number")

        branch = f"claude/auto-fix-report-{report_id}"
        commit_msg = f"AI auto-fix from report #{report_id}: {proposal.summary}"
        pr_title = f"[auto] Fix from report #{report_id}: {proposal.summary}"
        pr_body_parts: list[str] = [
            "### AI-authored auto-fix",
            "",
            f"**Source:** report #{report_id} (guild `{guild_id}`)",
        ]
        if issue_num:
            pr_body_parts.append(f"**Closes #{int(issue_num)}**")
        pr_body_parts += [
            f"**File touched:** `{proposal.rel_path}`",
            f"**Lines changed:** ~{proposal.lines_changed} (cap: {_af.MAX_DIFF_LINES})",
            "",
            f"**AI rationale:** {proposal.rationale}",
            "",
            "---",
            "",
            "**Original report (verbatim):**",
            "",
        ]
        for line in str(report_row.get("message") or "").splitlines() or [""]:
            pr_body_parts.append(f"> {line}")
        pr_body_parts.append("")
        if verdict:
            pr_body_parts += [
                "**Realness verdict (auto-diagnose):**",
                "",
                "```",
                verdict.strip()[:1500],
                "```",
                "",
            ]
        pr_body_parts += [
            "---",
            "",
            ":warning: This PR was authored by an LLM from player-submitted "
            "text. Treat as a draft proposal: review the diff carefully "
            "for prompt-injection or unintended scope before merging. Do "
            "not merge if the AI's claimed file mismatches the actual bug.",
        ]
        pr_body = "\n".join(pr_body_parts)

        pr_err: list[str] = []
        result = await _gh.open_single_file_pr(
            branch=branch,
            rel_path=proposal.rel_path,
            new_text=proposal.new_text,
            commit_msg=commit_msg,
            pr_title=pr_title,
            pr_body=pr_body,
            _error_out=pr_err,
        )
        if result is None:
            reason = (pr_err[-1] if pr_err else "unknown") [:300]
            try:
                await self.bot.db.reports.update_autofix_status(
                    report_id, "failed",
                    last_error=f"PR creation failed: {reason}",
                )
            except Exception:
                log.debug("autofix queue update failed", exc_info=True)
            await self._dm_status_update(
                report_id, "failed",
                reason=f"GitHub PR creation failed: {reason}",
            )
            return (None, "GitHub API call failed -- check bot logs.")
        url, num = result
        try:
            new_row = await self.bot.db.reports.update_autofix_status(
                report_id, "pr_open",
                pr_url=url, pr_number=int(num),
            )
        except Exception:
            new_row = None
            log.debug("autofix queue update failed", exc_info=True)
        await self._dm_status_update(
            report_id, "pr_open", row=new_row, proposal=proposal,
            pr_url=url,
        )
        return (url, f"Draft PR opened: {url}")

    async def _append_dm_field(
        self, dm_msg: discord.Message, name: str, value: str,
    ) -> None:
        """Re-fetch the live DM, append a field, edit in place. Best-effort
        and silent on failure so callers never abort their main flow.
        """
        try:
            fresh = await dm_msg.channel.fetch_message(dm_msg.id)
            base = fresh.embeds[0] if fresh.embeds else None
        except Exception:
            base = None
        if base is None:
            return
        if len(value) > 1000:
            value = value[:997] + "..."
        try:
            base.add_field(name=name, value=value, inline=False)
            await dm_msg.edit(embed=base)
        except Exception:
            log.debug("append_dm_field: edit failed", exc_info=True)

    async def cog_load(self) -> None:
        """Register persistent views for all non-terminal reports so buttons survive restarts.

        Also resets autofix-queue state so a bot crash mid-LLM-call doesn't
        leave rows stuck in ``generating`` and so ``proposed`` rows (whose
        in-memory patch is gone) get regenerated on the next worker tick.
        """
        try:
            open_reports = await self.bot.db.reports.get_open_reports()
        except Exception:
            return
        for r in open_reports:
            current_tags = [t for t in (r.get("tags") or "").split(",") if t]
            if r["status"] == "open":
                self.bot.add_view(ReportTriageView(r["id"], self.bot))
            elif r["status"] in ("accepted", "in_progress"):
                self.bot.add_view(ReportManageView(r["id"], self.bot, current_tags))
            elif r["status"] == "resolved":
                self.bot.add_view(_CloseOnlyView(r["id"], self.bot, current_tags))
        # Autofix queue: reset stuck states. The ``proposed`` -> ``queued``
        # roll-back is per-guild but ``open_reports`` already covers every
        # active guild so we lift the unique guild_ids off it.
        guild_ids: set[int] = {int(r["guild_id"]) for r in open_reports if r.get("guild_id")}
        for gid in guild_ids:
            try:
                await self.bot.db.reports.reset_in_memory_proposed_autofixes(gid)
                await self.bot.db.reports.reset_stale_generating_autofixes(gid, 600)
            except Exception:
                log.debug(
                    "report cog_load: autofix reset failed for gid=%s", gid,
                    exc_info=True,
                )

    # ── User command ──────────────────────────────────────────────────────

    @commands.hybrid_group(name="report", invoke_without_command=True)
    @guild_only
    @no_bots
    async def report_group(self, ctx: DiscoContext) -> None:
        """Submit reports, browse community reports, or view bug bounties."""
        if ctx.invoked_subcommand:
            return
        p = ctx.prefix
        await ctx.reply(
            f"**Report commands:**\n"
            f"`{p}report submit <category> <message>` - submit a report\n"
            f"`{p}report browse [category]` - browse community reports\n"
            f"`{p}report claim <bounty_id> <message>` - submit for a bounty\n"
            f"`{p}report bounties` - view active bounties",
            ephemeral=True, mention_author=False,
        )

    @report_group.command(name="submit", description="Submit a bug report or suggestion.")
    @guild_only
    @no_bots
    @ensure_registered
    @app_commands.describe(
        category="Category: bugs, suggestions, users, or other",
        message="Describe the issue",
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="bugs",        value="bugs"),
        app_commands.Choice(name="suggestions", value="suggestions"),
        app_commands.Choice(name="users",       value="users"),
        app_commands.Choice(name="other",       value="other"),
    ])
    async def report(self, ctx: DiscoContext, category: str, *, message: str) -> None:
        """Submit a report. Categories: bugs, suggestions, users, other."""
        # Prevent double-submission from lag / rapid re-invocation
        _rkey = (ctx.author.id, ctx.guild.id)
        _rlock = self._report_locks.setdefault(_rkey, asyncio.Lock())
        if _rlock.locked():
            await ctx.reply("Your report is still being submitted, please wait.", ephemeral=True, mention_author=False)
            return
        async with _rlock:
            await self._submit_report(ctx, category, message)

    async def _submit_report(self, ctx: DiscoContext, category: str, message: str) -> None:
        """Inner report logic  -  called with user lock held."""
        category = category.lower()
        if category not in VALID_CATEGORIES:
            await ctx.reply(
                f"Invalid category. Choose from: {', '.join(sorted(VALID_CATEGORIES))}",
                ephemeral=True, mention_author=False,
            )
            return

        # Cooldown: 5 minutes between reports
        latest = await self.bot.db.reports.get_user_latest_report(ctx.author.id, ctx.guild.id)
        if latest:
            _ca = latest["created_at"]
            _ca_ts = _ca.timestamp() if hasattr(_ca, 'timestamp') else _ca
            elapsed = time.time() - _ca_ts
            if elapsed < _REPORT_COOLDOWN:
                remaining = int(_REPORT_COOLDOWN - elapsed)
                mins, secs = divmod(remaining, 60)
                time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
                # Offer edit if their last report is still open
                if latest["status"] == "open":
                    await ctx.reply(
                        f"You can submit another report in **{time_str}**.\n"
                        f"Your last report **#{latest['id']}** is still open  -  "
                        f"use `{ctx.clean_prefix}report-edit {latest['id']}` to update it instead.",
                        ephemeral=True, mention_author=False,
                    )
                else:
                    await ctx.reply(
                        f"You can submit another report in **{time_str}**.",
                        ephemeral=True, mention_author=False,
                    )
                return

        target_id = Config.REPORT_TARGET_USER_ID
        if not target_id:
            await ctx.reply_error(
                "Reports are not configured on this bot.\n"
                "The bot owner must set the `REPORT_TARGET_USER_ID` environment variable."
            )
            return

        admin = await _fetch_admin(self.bot)
        if not admin:
            await ctx.reply("Could not reach the admin. Please contact them directly.", ephemeral=True, mention_author=False)
            return

        # Persist to DB
        report_row = await self.bot.db.reports.create_report(
            ctx.guild.id, ctx.author.id, category, message,
        )

        # Build and send admin DM
        embed = _build_admin_embed(report_row, ctx.author)
        triage_view = ReportTriageView(report_row["id"], self.bot)
        try:
            dm_msg = await admin.send(embed=embed, view=triage_view)
            await self.bot.db.reports.set_dm_message_id(report_row["id"], dm_msg.id)
        except discord.Forbidden:
            await ctx.reply("Could not deliver your report (admin DMs disabled).", ephemeral=True, mention_author=False)
            return

        # Auto-diagnose hook. Off by default; flip on with
        # ,admin reports auto on. Runs in the background so the report
        # submitter never waits on the OpenRouter / Ollama round-trip.
        # Verdict is appended as a new field on the existing admin DM.
        try:
            settings = await self.bot.db.get_guild_settings(ctx.guild.id)
            if bool(settings.get("reports_auto_diagnose")):
                asyncio.create_task(
                    self._auto_diagnose_report(
                        dict(report_row), dm_msg, admin, ctx.author,
                    )
                )
        except Exception:
            log.debug("auto-diagnose dispatch failed", exc_info=True)

        # Post to reports feed channel
        await _post_to_reports_feed(self.bot, report_row, event="new_report", user=ctx.author)

        # Confirm to the reporter
        confirm = card(
            "Report Submitted",
            description=(
                f"Your report **#{report_row['id']}** has been submitted.\n"
                "You'll receive a DM when the admin updates its status."
            ),
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=confirm, ephemeral=True, mention_author=False)

    # ── Edit report command ───────────────────────────────────────────────

    @commands.hybrid_command(
        name="report-edit",
        aliases=["reportedit", "editreport"],
        description="Edit your open report.",
        with_app_command=False,
    )
    @guild_only
    @no_bots
    @ensure_registered
    @app_commands.describe(
        report_id="The report number to edit",
        message="New message for the report",
    )
    async def report_edit(self, ctx: DiscoContext, report_id: int, *, message: str) -> None:
        """Edit an open report you submitted. Usage: report-edit <id> <new message>"""
        report = await self.bot.db.reports.get_report(report_id)
        if not report:
            await ctx.reply(f"Report #{report_id} not found.", ephemeral=True, mention_author=False)
            return

        if report["user_id"] != ctx.author.id:
            await ctx.reply("You can only edit your own reports.", ephemeral=True, mention_author=False)
            return

        if report["status"] != "open":
            await ctx.reply(
                f"Report #{report_id} is **{report['status']}** and can no longer be edited.",
                ephemeral=True, mention_author=False,
            )
            return

        updated = await self.bot.db.reports.update_report_message(report_id, message)

        # Update the admin DM embed if we have the message ID
        admin = await _fetch_admin(self.bot)
        if admin and updated and updated.get("dm_message_id"):
            try:
                dm = admin.dm_channel or await admin.create_dm()
                admin_msg = await dm.fetch_message(updated["dm_message_id"])
                embed = _build_admin_embed(updated, ctx.author)
                await admin_msg.edit(embed=embed)
            except Exception:
                pass

        # Relay the edit to the reports feed channel
        if updated:
            await _post_to_reports_feed(self.bot, updated, event="report_edited", user=ctx.author)

        confirm = card(
            "Report Updated",
            description=f"Your report **#{report_id}** has been updated.",
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=confirm, ephemeral=True, mention_author=False)

    # ── Public browse command ────────────────────────────────────────────

    PUBLIC_CATEGORIES = {"bugs", "suggestions"}

    @report_group.command(name="browse", description="Browse community bug reports and suggestions.")
    @guild_only
    @no_bots
    @ensure_registered
    @app_commands.describe(category="Filter by category (bugs, suggestions, or bounties)")
    @app_commands.choices(category=[
        app_commands.Choice(name="bugs",        value="bugs"),
        app_commands.Choice(name="suggestions", value="suggestions"),
        app_commands.Choice(name="bounties",    value="bounties"),
    ])
    async def reports_browse(self, ctx: DiscoContext, category: str = "") -> None:
        """Browse public reports. Use 'bounties' to view active bounties."""
        category = category.lower().strip() if category else ""

        # Route "reports bounties" to the bounty browser
        if category == "bounties":
            await ctx.invoke(self.reports_bounties)
            return

        if category and category not in self.PUBLIC_CATEGORIES:
            await ctx.reply("Only `bugs` and `suggestions` are publicly visible.", ephemeral=True, mention_author=False)
            return

        # Show active bounties banner
        bounties = await self.bot.db.reports.get_active_bounties(ctx.guild.id)
        if bounties:
            bounty_lines = []
            for b in bounties:
                bounty_lines.append(
                    f"💰 **${to_human(int(b['reward_amount'] or 0)):,.2f}**  -  {b['title']} [`{b['category']}`]"
                )
            bounty_embed = card(
                "🐛 Active Bug Bounties",
                description=(
                    "Submit `.report submit <category> <msg>` or `.report claim <id> <msg>` for a reward!\n\n"
                    + "\n".join(bounty_lines)
                ),
                color=C_GOLD,
            ).build()
            await ctx.reply(embed=bounty_embed, mention_author=False)

        reports = await self.bot.db.reports.get_public_reports(
            ctx.guild.id, category=category or None,
        )

        if not reports:
            await ctx.reply("No reports found.", ephemeral=True, mention_author=False)
            return

        # Build paginated list
        pages = []
        per_page = 5
        for i in range(0, len(reports), per_page):
            chunk = reports[i:i + per_page]
            lines = []
            for r in chunk:
                status_icon = {"open": "\U0001f4e9", "accepted": "\u2705", "rejected": "\u274c",
                               "in_progress": "\U0001f527", "resolved": "\u2705", "closed": "\U0001f512"}.get(r["status"], "\u2753")
                _ca = r.get("created_at")
                _ca_ts = _ca.timestamp() if hasattr(_ca, "timestamp") else _ca
                ts_str = fmt_ts(int(_ca_ts)) if _ca else ""
                msg_preview = (r["message"][:80] + "...") if len(r["message"]) > 80 else r["message"]
                lines.append(f"{status_icon} **#{r['id']}** [{r['category']}] {ts_str}\n> {msg_preview}")
            embed = card(
                f"Community Reports ({len(reports)} total)",
                description="\n\n".join(lines),
                color=C_INFO,
            ).build()
            embed.set_footer(text=f"Page {i // per_page + 1}/{(len(reports) - 1) // per_page + 1}")
            pages.append(embed)

        if len(pages) == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
        else:
            view = Paginator(pages, ctx.author.id)
            await ctx.reply(embed=pages[0], view=view, mention_author=False)


    # ── Bounty commands ─────────────────────────────────────────────────

    @report_group.command(name="bounties", description="View active bug bounties.")
    @guild_only
    @no_bots
    async def report_bounties(self, ctx: DiscoContext) -> None:
        """View all active bug bounties and their rewards."""
        await ctx.invoke(self.bounty_list)

    @commands.hybrid_group(name="bounty", invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    async def bounty(self, ctx: DiscoContext) -> None:
        """Bug bounty system  -  reward players for good reports."""
        if not ctx.invoked_subcommand:
            await ctx.invoke(self.bounty_list)

    @bounty.command(name="list")
    async def bounty_list(self, ctx: DiscoContext) -> None:
        """View active bounties."""
        bounties = await self.bot.db.reports.get_active_bounties(ctx.guild.id)
        if not bounties:
            await ctx.reply("No active bounties. Check back later!", mention_author=False)
            return

        lines = []
        for b in bounties:
            claims_str = f"{b['claims']}/{b['max_claims']}" if b["max_claims"] > 0 else f"{b['claims']} claimed"
            lines.append(
                f"**#{b['id']}**  -  **${to_human(int(b['reward_amount'] or 0)):,.2f}** reward\n"
                f"**{b['title']}** [{b['category']}]\n"
                f"> {b['description'][:200]}\n"
                f"Claims: {claims_str}"
            )

        embed = card(
            f"Active Bounties ({len(bounties)})",
            description="\n\n".join(lines),
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @bounty.command(name="create")
    async def bounty_create(
        self, ctx: DiscoContext,
        reward: float,
        category: str,
        *,
        title: str,
    ) -> None:
        """Create a bounty. Admin only. Usage: .bounty create <reward> <category> <title>"""
        if ctx.author.id != Config.REPORT_TARGET_USER_ID:
            if not ctx.author.guild_permissions.manage_guild:
                await ctx.reply_error("Only server admins can create bounties.")
                return

        category = category.lower()
        if category not in VALID_CATEGORIES:
            await ctx.reply_error(f"Invalid category. Choose from: {', '.join(sorted(VALID_CATEGORIES))}")
            return
        if reward <= 0:
            await ctx.reply_error("Reward must be positive.")
            return

        bounty_row = await self.bot.db.reports.create_bounty(
            guild_id=ctx.guild.id,
            created_by=ctx.author.id,
            title=title,
            description=f"Submit a .report in the '{category}' category with useful info to claim.",
            category=category,
            reward_amount=to_raw(reward),
        )

        embed = card(
            "Bounty Created",
            description=(
                f"Bounty **#{bounty_row['id']}** is now active.\n\n"
                f"**{title}**\n"
                f"Category: `{category}`\n"
                f"Reward: **${reward:,.2f}** per qualifying report\n\n"
                f"Players can submit `.report {category} <details>` to participate."
            ),
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @bounty.command(name="close")
    async def bounty_close(self, ctx: DiscoContext, bounty_id: int) -> None:
        """Close an active bounty and distribute rewards to qualifying reports. Admin only."""
        if ctx.author.id != Config.REPORT_TARGET_USER_ID:
            if not ctx.author.guild_permissions.manage_guild:
                await ctx.reply_error("Only server admins can close bounties.")
                return

        bounty_row = await self.bot.db.reports.get_bounty(bounty_id)
        if not bounty_row:
            await ctx.reply_error(f"Bounty #{bounty_id} not found.")
            return
        if not bounty_row["is_active"]:
            await ctx.reply_error(f"Bounty #{bounty_id} is already closed.")
            return

        # Find all reports that qualify for payout (accepted, in_progress, or closed)
        qualifying = await self.bot.db.reports.get_qualifying_bounty_reports(bounty_id)
        reward_total_raw = int(bounty_row["reward_amount"] or 0)
        reward_total = to_human(reward_total_raw)
        payout_lines: list[str] = []

        if qualifying and reward_total_raw > 0:
            count = len(qualifying)
            # Use integer split on raw units; last recipient absorbs any remainder
            split_raw = reward_total_raw // count
            distributed_raw = 0
            for idx, r in enumerate(qualifying):
                amount_raw = split_raw if idx < count - 1 else (reward_total_raw - distributed_raw)
                amount = to_human(amount_raw)
                try:
                    await self.bot.db.update_wallet(r["user_id"], r["guild_id"], amount_raw)
                    distributed_raw += amount_raw
                    payout_lines.append(f"{mention(r['user_id'], guild=ctx.guild, bot=self.bot)} (report #{r['id']})  -  +${amount:,.2f}")
                    await _notify_reporter(
                        self.bot, r["user_id"], r, r["status"],
                        note=f"Bounty **#{bounty_id}** closed  -  you received **${amount:,.2f}**!",
                    )
                except Exception:
                    pass

        # Close all linked reports and mark bounty closed
        closed_count = await self.bot.db.reports.close_linked_bounty_reports(bounty_id)
        closed = await self.bot.db.reports.close_bounty(bounty_id)

        desc = f"Bounty **#{bounty_id}**  -  **{closed['title']}** is now closed.\n\n"
        if payout_lines:
            desc += f"**${reward_total:,.2f}** distributed evenly among {len(qualifying)} qualifying report(s):\n"
            desc += "\n".join(payout_lines[:10])
            if len(payout_lines) > 10:
                desc += f"\n...and {len(payout_lines)-10} more."
        else:
            desc += "No qualifying reports to distribute rewards to."
        if closed_count:
            desc += f"\n\n{closed_count} linked report(s) auto-closed."

        embed = card("Bounty Closed", description=desc, color=C_NEUTRAL).build()
        await ctx.reply(embed=embed, mention_author=False)

    @bounty.command(name="edit")
    async def bounty_edit(
        self, ctx: DiscoContext,
        bounty_id: int,
        field: str,
        *,
        value: str,
    ) -> None:
        """Edit a bounty field. Admin only. Fields: reward, title, description.
        Usage: .bounty edit <id> reward <amount>  |  .bounty edit <id> title <new title>"""
        if ctx.author.id != Config.REPORT_TARGET_USER_ID:
            if not ctx.author.guild_permissions.manage_guild:
                await ctx.reply_error("Only server admins can edit bounties.")
                return

        bounty_row = await self.bot.db.reports.get_bounty(bounty_id)
        if not bounty_row:
            await ctx.reply_error(f"Bounty #{bounty_id} not found.")
            return

        field = field.lower()
        kwargs: dict = {}
        if field in ("reward", "reward_amount", "amount"):
            try:
                new_reward = float(value.lstrip("$").replace(",", ""))
                if new_reward <= 0:
                    raise ValueError
            except ValueError:
                await ctx.reply_error("Reward must be a positive number (e.g. `500` or `$500`).")
                return
            kwargs["reward_amount"] = to_raw(new_reward)
        elif field == "title":
            kwargs["title"] = value[:200]
        elif field in ("desc", "description"):
            kwargs["description"] = value[:500]
        else:
            _BOUNTY_FIELDS = ("reward", "title", "description")
            valid = ", ".join(f"`{f}`" for f in _BOUNTY_FIELDS)
            await ctx.reply_error(f"Unknown field. Use {valid}.")
            return

        updated = await self.bot.db.reports.edit_bounty(bounty_id, **kwargs)
        embed = card(
            "Bounty Updated",
            description=(
                f"Bounty **#{bounty_id}**  -  **{updated['title']}**\n"
                f"Category: `{updated['category']}`\n"
                f"Reward: **${to_human(int(updated['reward_amount'] or 0)):,.2f}** per qualifying report\n"
                f"Status: {'🟢 Active' if updated['is_active'] else '🔒 Closed'}"
            ),
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @bounty.command(name="view")
    async def bounty_view(self, ctx: DiscoContext, bounty_id: int) -> None:
        """View detailed info about a bounty and any reports linked to it.
        Usage: .bounty view <id>   -   or use .reports bounties <id>"""
        bounty_row = await self.bot.db.reports.get_bounty(bounty_id)
        if not bounty_row:
            await ctx.reply_error(f"Bounty #{bounty_id} not found.")
            return

        linked_reports = await self.bot.db.reports.get_bounty_reports(bounty_id)
        status_icon = "🟢 Active" if bounty_row["is_active"] else "🔒 Closed"
        desc = (
            f"**{bounty_row['title']}** {status_icon}\n"
            f"Category: `{bounty_row['category']}`\n"
            f"Reward: **${to_human(int(bounty_row['reward_amount'] or 0)):,.2f}** per qualifying report\n\n"
            f"{bounty_row['description']}\n\n"
            f"📋 **Linked Reports ({len(linked_reports)}):**"
        )
        if linked_reports:
            for r in linked_reports[:8]:
                st_label, st_emoji, _ = STATUSES.get(r["status"], (r["status"], "?", 0))
                desc += f"\n{st_emoji} **#{r['id']}** [{r['category']}]  -  {r['message'][:60]}…"
            if len(linked_reports) > 8:
                desc += f"\n*…and {len(linked_reports)-8} more*"
        else:
            desc += f"\nNo reports submitted yet. Use `,bugbounty {bounty_id} <message>` to submit one."

        embed = card(f"Bounty #{bounty_id}", description=desc, color=C_SUCCESS if bounty_row["is_active"] else C_NEUTRAL).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── User bounty report submission ─────────────────────────────────────

    @report_group.command(name="claim", description="Submit a report for a specific active bug bounty.")
    @guild_only
    @no_bots
    @ensure_registered
    @app_commands.describe(
        bounty_id="The bounty ID to claim",
        message="Your submission details",
    )
    async def report_bounty(self, ctx: DiscoContext, bounty_id: int, *, message: str) -> None:
        """Submit a report for a specific bug bounty."""
        bounty_row = await self.bot.db.reports.get_bounty(bounty_id)
        if not bounty_row:
            await ctx.reply(f"Bounty #{bounty_id} not found.", ephemeral=True, mention_author=False)
            return
        if not bounty_row["is_active"]:
            await ctx.reply(f"Bounty #{bounty_id} is closed and no longer accepting reports.", ephemeral=True, mention_author=False)
            return

        # Cooldown: reuse same 5-minute window as regular reports
        latest = await self.bot.db.reports.get_user_latest_report(ctx.author.id, ctx.guild.id)
        if latest:
            _ca = latest["created_at"]
            _ca_ts = _ca.timestamp() if hasattr(_ca, 'timestamp') else _ca
            elapsed = time.time() - _ca_ts
            if elapsed < _REPORT_COOLDOWN:
                remaining = int(_REPORT_COOLDOWN - elapsed)
                mins, secs = divmod(remaining, 60)
                await ctx.reply(
                    f"You can submit another report in **{mins}m {secs}s**.",
                    ephemeral=True, mention_author=False,
                )
                return

        admin = await _fetch_admin(self.bot)
        if not admin:
            await ctx.reply("Could not reach the admin. Please contact them directly.", ephemeral=True, mention_author=False)
            return

        report_row = await self.bot.db.reports.create_bounty_report(
            ctx.guild.id, ctx.author.id, bounty_row["category"], message, bounty_id,
        )

        embed = _build_admin_embed(report_row, ctx.author)
        triage_view = ReportTriageView(report_row["id"], self.bot)
        try:
            dm_msg = await admin.send(embed=embed, view=triage_view)
            await self.bot.db.reports.set_dm_message_id(report_row["id"], dm_msg.id)
        except discord.Forbidden:
            pass

        await _post_to_reports_feed(self.bot, report_row, event="new_report", user=ctx.author)

        confirm = card(
            "Bounty Report Submitted",
            description=(
                f"Your report **#{report_row['id']}** for bounty **#{bounty_id}  -  {bounty_row['title']}** has been submitted.\n"
                "You'll receive a DM when the status is updated."
            ),
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=confirm, ephemeral=True, mention_author=False)

    # ── Prefix-only backward compat aliases ──────────────────────────────────

    @commands.command(name="bugbounty", hidden=True,
                      aliases=["bug-bounty", "reportbounty", "bountyreport", "report-bounty"])
    @guild_only
    @no_bots
    @ensure_registered
    async def _bugbounty_compat(self, ctx: DiscoContext, bounty_id: int, *, message: str) -> None:
        """Backward compat alias for ,report claim."""
        await ctx.invoke(self.report_bounty, bounty_id=bounty_id, message=message)

    @commands.command(name="reports", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def _reports_compat(self, ctx: DiscoContext, category: str = "") -> None:
        """Backward compat alias for ,report browse."""
        await ctx.invoke(self.reports_browse, category=category)

    # ── Reports bounties subcommand (alias for bounty list with detail) ────

    @commands.command(name="reports-bounties", aliases=["reportbounties", "bountyboard"])
    @guild_only
    @no_bots
    async def reports_bounties(self, ctx: DiscoContext, bounty_id_or_action: str = "") -> None:
        """Browse active bounties. Use a number to see bounty details.
        Usage: ,reports bounties  |  ,reports bounties 3  |  ,reports bounties close 3"""
        arg = bounty_id_or_action.strip().lower()

        # Try admin sub-actions: open/close/edit <id>
        if arg in ("close", "edit", "open"):
            await ctx.reply(
                f"Use `,bounty {arg} <id>` for admin actions.",
                ephemeral=True, mention_author=False,
            )
            return

        # Try viewing a specific bounty by ID
        if arg.isdigit():
            await ctx.invoke(self.bounty_view, bounty_id=int(arg))
            return

        # Otherwise show the list (same as ,bounty list)
        await ctx.invoke(self.bounty_list)

    # ── Autofix lifecycle DMs + worker loop ────────────────────────────────
    #
    # Every status transition the queue can take fires a DM to the report
    # recipient (``REPORT_TARGET_USER_ID`` env var, overridable via
    # ``,admin reports dm @user``). The worker runs every 30 seconds,
    # picks the oldest ``queued`` row in any guild, and walks it through
    # ``generating`` -> ``proposed`` (ready for the Open PR click) or
    # ``failed`` / ``unfixable`` if validation rejects the AI's output.

    async def _dm_autofix_trace(
        self,
        report_id: int,
        outcome: str,
        trace: "list",
        *,
        report_text: str = "",
        rejection_reason: str = "",
        picked_path: str = "",
    ) -> None:
        """Send a single DM with the report context + per-AI-call trace.

        Renders:
          * outcome + GitHub config state
          * the report's verbatim text (truncated to 400 chars) so the
            admin doesn't have to cross-reference a separate command
            to figure out what the AI was even trying to fix
          * the rejection reason from propose_fix (if any)
          * the file the AI picked (if any) -- distinguishes 'AI picked
            cogs/foo.py and validation rejected' from 'AI returned
            UNKNOWN entirely'
          * one code block per TraceStep with backend / model / tokens
            / elapsed / error / raw output preview

        Best-effort -- a DM failure never aborts the autofix flow.
        """
        if not trace and not report_text and not rejection_reason:
            return
        # Dedupe identical-failure spam. Build a fingerprint from the
        # outcome + per-step (stage, error) pair. If we've already
        # DM'd this exact failure within the suppression window, skip
        # the DM and bump the counter; the next suppress-tick (every
        # _autofix_trace_summary_window_s) emits one summary DM
        # ('N more identical traces in the last hour') and resets.
        import time as _t
        now_s = int(_t.time())
        fp_parts = [outcome]
        for step in trace or []:
            fp_parts.append(f"{step.stage}:{(step.error or '')[:120]}")
        fingerprint = "|".join(fp_parts)
        entry = self._autofix_trace_seen.get(fingerprint)
        if entry:
            since = now_s - int(entry.get("last_dm_at") or 0)
            if since < self._autofix_trace_summary_window_s:
                entry["suppressed"] = int(entry.get("suppressed", 0)) + 1
                # Emit a one-line nudge in the bot log so debugging
                # against logs still shows the count, but no DM noise.
                log.info(
                    "autofix trace suppressed (dupe of %s, n=%d)",
                    fingerprint[:60], entry["suppressed"],
                )
                return
            # Window has elapsed; if we suppressed any since the last DM,
            # tack a one-line summary onto this fresh DM.
            n_suppressed = int(entry.get("suppressed", 0))
            entry["last_dm_at"] = now_s
            entry["suppressed"] = 0
        else:
            self._autofix_trace_seen[fingerprint] = {
                "first_at": now_s, "last_dm_at": now_s, "suppressed": 0,
            }
            n_suppressed = 0
        try:
            from core.framework.ai import github_pr as _gh
            gh_state = "configured" if _gh.is_configured() else "NOT configured"
        except Exception:
            gh_state = "?"
        lines: list[str] = []
        header = (
            f"\U0001F4DC AI trace for report **#{report_id}** "
            f"(outcome: `{outcome}`) -- GitHub: {gh_state}"
        )
        if n_suppressed:
            header += (
                f"\n_(plus **{n_suppressed}** identical trace(s) "
                f"suppressed in the last hour)_"
            )
        lines.append(header)
        if report_text:
            preview = report_text.strip().replace("```", "ʼʼʼ")[:400]
            if len(report_text) > 400:
                preview += "..."
            lines.append("**Report text:**")
            lines.append("```")
            lines.append(preview)
            lines.append("```")
        if picked_path:
            lines.append(f"**File AI picked:** `{picked_path}`")
        if rejection_reason:
            reason_preview = rejection_reason.replace("```", "ʼʼʼ")[:600]
            lines.append("**Rejection:**")
            lines.append("```")
            lines.append(reason_preview)
            lines.append("```")
        for step in trace or []:
            blk: list[str] = []
            blk.append(f"[{step.stage}]")
            if step.backend:
                blk.append(f"backend = {step.backend}")
            if step.model:
                blk.append(f"model   = {step.model}")
            # Prompt size + max_tokens are useful to spot context-length
            # truncation: if the model's context is, say, 8k tokens and
            # prompt_chars implies > 30k chars, the locate ask is over
            # budget and the model is silently dropping the tail.
            if step.prompt_chars or step.max_tokens:
                blk.append(
                    f"prompt  = {step.prompt_chars:,} chars / "
                    f"max_out = {step.max_tokens}"
                )
            if step.prompt_tokens or step.completion_tokens:
                blk.append(
                    f"tokens  = in {step.prompt_tokens} / "
                    f"out {step.completion_tokens}"
                )
            if step.elapsed_ms:
                blk.append(f"elapsed = {step.elapsed_ms} ms")
            if step.error:
                blk.append(f"error   = {step.error[:300]}")
            if step.raw_output:
                preview = step.raw_output[:500].replace("```", "ʼʼʼ")
                blk.append(f"output  = {preview}")
            lines.append("```\n" + "\n".join(blk) + "\n```")
        # Discord message cap is ~2000; fall back to multi-message above that.
        msg = "\n".join(lines)
        if len(msg) <= 1950:
            await self._dm_report_recipient(content=msg)
            return
        chunk: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) > 1900 and chunk:
                await self._dm_report_recipient(content="\n".join(chunk))
                chunk = []
                size = 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await self._dm_report_recipient(content="\n".join(chunk))

    async def _dm_report_recipient(self, content: str = "", embed: discord.Embed | None = None) -> None:
        """Send the configured report recipient a DM. Best-effort -- if
        the user has DMs disabled or the lookup fails, we log and move on.
        """
        try:
            user = await _fetch_admin(self.bot)
        except Exception:
            user = None
        if user is None:
            return
        try:
            await user.send(content=content or None, embed=embed)
        except discord.Forbidden:
            log.info("autofix DM blocked: recipient has DMs disabled")
        except Exception:
            log.debug("autofix DM send failed", exc_info=True)

    async def _autofix_open_issue(
        self, report_row: dict, verdict: str, requested_by: int,
    ) -> tuple[str, int] | None:
        """Open a tracking GitHub issue for a report and stash the URL on
        the queue row. Called once per real-and-confident report; the
        eventual auto-fix PR closes the issue via "Closes #<number>".
        """
        from core.framework.ai import github_pr as _gh
        if not _gh.is_configured():
            return None
        report_id = int(report_row.get("id") or 0)
        title = (
            f"[auto] Bug report #{report_id}: "
            f"{(str(report_row.get('message') or '')[:60].splitlines() or ['(empty)'])[0]}"
        )
        if len(title) > 120:
            title = title[:117] + "..."
        body_lines: list[str] = []
        body_lines.append(
            f"### Player-submitted bug report (auto-tracked)\n"
        )
        body_lines.append(f"**Source:** report #{report_id}")
        body_lines.append(f"**Reporter:** Discord user `{report_row.get('user_id')}`")
        body_lines.append(f"**Category:** {report_row.get('category', '?')}")
        body_lines.append("")
        body_lines.append("**Verbatim:**")
        body_lines.append("")
        for line in str(report_row.get("message") or "").splitlines() or [""]:
            body_lines.append(f"> {line}")
        if verdict:
            body_lines.append("")
            body_lines.append("**AI realness verdict:**")
            body_lines.append("")
            body_lines.append("```")
            body_lines.append(verdict.strip()[:1500])
            body_lines.append("```")
        body_lines.append("")
        body_lines.append(
            ":robot: This issue was opened by the Discoin auto-fix bot. "
            "The follow-up PR (if any) will close it automatically. Treat "
            "all content as untrusted player input."
        )
        # NOTE: deliberately not passing labels= -- unknown labels make
        # GitHub return 422 silently which previously made every
        # issue creation look like a network failure. Add labels back
        # only if you've ensured ['auto-fix', 'bug-report', 'ai-triage']
        # exist on the repo via Settings -> Labels.
        err_out: list[str] = []
        result = await _gh.open_issue(
            title=title, body="\n".join(body_lines),
            _error_out=err_out,
        )
        if result is None:
            # Loud failure: DM the admin WHY instead of swallowing it.
            # Stash the reason on the queue row's last_error so the
            # ,admin reports queue status command surfaces it later too.
            reason = (err_out[-1] if err_out else "unknown") [:300]
            try:
                await self.bot.db.reports.update_autofix_status(
                    report_id, "queued",
                    last_error=f"open_issue failed: {reason}",
                )
            except Exception:
                pass
            e = card(
                "\U000026A0 Auto-fix issue creation failed",
                color=C_ERROR,
                description=(
                    f"Report **#{report_id}** triggered the auto-fix path "
                    f"but the GitHub issue could not be created.\n\n"
                    f"**Reason:** `{reason}`\n\n"
                    f"Check `,admin reports autofix test` for an auth + "
                    f"scope probe."
                ),
            ).build()
            await self._dm_report_recipient(embed=e)
            return None
        url, num = result
        try:
            await self.bot.db.reports.update_autofix_status(
                report_id, "queued",  # status unchanged; we only patch the link
                issue_url=url, issue_number=num,
            )
        except Exception:
            log.debug("autofix issue link save failed", exc_info=True)
        return result

    @tasks.loop(seconds=30)
    async def autofix_worker(self) -> None:
        """Pull the oldest queued autofix row from any guild and walk it
        through generation. One row per tick keeps LLM cost predictable
        and avoids hammering the GitHub API.
        """
        if self._autofix_worker_lock.locked():
            return
        async with self._autofix_worker_lock:
            await self._autofix_worker_tick()

    @autofix_worker.before_loop
    async def autofix_worker_before(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def autofix_pr_watcher(self) -> None:
        """Poll PRs in ``status = pr_open`` and roll the report status
        forward when GitHub says the PR was merged. Also catches the
        "admin closed the PR without merging" case and flips the queue
        row to ``discarded`` so the dashboard doesn't claim it's still
        in flight.

        Gated per-guild by ``reports_auto_close``. With the toggle off,
        the watcher still polls so the queue dashboard stays accurate
        but it never auto-resolves the underlying ``reports`` row.
        """
        if not self.bot.is_ready():
            return
        try:
            from core.framework.ai import github_pr as _gh
        except Exception:
            return
        if not _gh.is_configured():
            return
        try:
            guilds = list(self.bot.guilds)
        except Exception:
            guilds = []
        for g in guilds:
            try:
                rows = await self.bot.db.reports.list_autofix_entries(
                    int(g.id), status="pr_open", limit=20,
                )
            except Exception:
                log.debug("pr watcher: list failed gid=%s", g.id, exc_info=True)
                continue
            try:
                settings = await self.bot.db.get_guild_settings(int(g.id))
            except Exception:
                settings = {}
            auto_close = bool(settings.get("reports_auto_close"))
            for row in rows:
                pr_number = row.get("pr_number")
                if not pr_number:
                    continue
                try:
                    state = await _gh.get_pr_state(int(pr_number))
                except Exception:
                    state = None
                if not state:
                    continue
                if state["merged"]:
                    await self._on_pr_merged(int(g.id), dict(row), state, auto_close)
                elif state["state"] == "closed":
                    await self._on_pr_closed_unmerged(int(g.id), dict(row), state)

    @autofix_pr_watcher.before_loop
    async def autofix_pr_watcher_before(self) -> None:
        await self.bot.wait_until_ready()

    async def _on_pr_merged(
        self, guild_id: int, queue_row: dict, state: dict, auto_close: bool,
    ) -> None:
        """Mark the queue row and (if toggle is on) the underlying
        report as resolved when its auto-fix PR merges. Idempotent:
        once the report status is no longer ``open`` / ``accepted`` /
        ``in_progress``, we leave it alone -- an admin may have already
        closed it manually with a different note.
        """
        report_id = int(queue_row.get("report_id") or 0)
        # Mark the queue row terminal so the watcher stops polling it.
        # Reusing ``pr_open`` -> ``pr_open`` would be a no-op; bump it
        # to a synthetic terminal state by leaving status alone but
        # writing the merged_at timestamp into last_error (cheap audit
        # trail without a schema migration). Then drop it on next
        # ,admin reports queue clear.
        try:
            await self.bot.db.reports.update_autofix_status(
                report_id, "pr_open",
                last_error=f"merged_at={state.get('merged_at') or 'unknown'}",
            )
        except Exception:
            pass
        # The recipient gets the merged DM regardless of the close
        # toggle -- it's a status update, not a destructive action.
        merged_url = state.get("html_url") or queue_row.get("pr_url") or ""
        e = card(
            "\U00002705 Auto-fix merged",
            color=C_SUCCESS,
            description=(
                f"PR for report **#{report_id}** has merged.\n"
                f"{merged_url}"
            ),
        ).build()
        await self._dm_report_recipient(embed=e)
        if not auto_close:
            return
        # Only roll a still-open report forward; don't trample a manual close.
        try:
            report = await self.bot.db.reports.get_report(report_id)
        except Exception:
            report = None
        if not report:
            return
        if str(report.get("status")) not in ("open", "accepted", "in_progress"):
            return
        note = (
            f"[auto-resolved] Auto-fix PR merged: "
            f"{merged_url or '(no url)'}"
        )
        try:
            updated = await self.bot.db.reports.update_status(
                report_id, "resolved", admin_note=note,
            )
        except Exception:
            log.exception("auto-close: resolve failed for report %s", report_id)
            return
        # Refresh the original admin DM if we still have a handle on it.
        admin_msg_id = (updated or {}).get("dm_message_id")
        if admin_msg_id:
            try:
                user = await _fetch_admin(self.bot)
                if user is not None:
                    dm = user.dm_channel or await user.create_dm()
                    msg = await dm.fetch_message(int(admin_msg_id))
                    embed = _build_admin_embed(updated or {}, None)
                    await msg.edit(embed=embed)
            except Exception:
                log.debug("auto-close: DM refresh failed", exc_info=True)
        del guild_id  # unused -- kept for grep parity

    async def _on_pr_closed_unmerged(
        self, guild_id: int, queue_row: dict, state: dict,
    ) -> None:
        """PR was closed without merging (admin rejected the auto-fix).
        Flip the queue row to ``discarded`` so the dashboard reflects
        reality. We DO NOT touch the underlying report -- the admin
        may still want to triage it manually.
        """
        report_id = int(queue_row.get("report_id") or 0)
        try:
            await self.bot.db.reports.update_autofix_status(
                report_id, "discarded",
                last_error="PR closed on GitHub without merging.",
            )
        except Exception:
            log.debug("auto-close: queue update failed", exc_info=True)
        e = card(
            "\U0001F5D1 Auto-fix PR closed (not merged)",
            color=C_AMBER,
            description=(
                f"PR for report **#{report_id}** was closed without "
                f"merging. The report itself stays open for triage.\n"
                f"{state.get('html_url') or queue_row.get('pr_url') or ''}"
            ),
        ).build()
        await self._dm_report_recipient(embed=e)
        del guild_id  # unused

    async def _autofix_worker_tick(self) -> None:
        """One pass: iterate guilds the bot is in, claim+process at most
        one row per guild. ``claim_next_queued_autofix`` uses
        ``FOR UPDATE SKIP LOCKED`` so concurrent workers (e.g. multiple
        bot instances) don't fight over the same row.
        """
        try:
            guilds = list(self.bot.guilds)
        except Exception:
            guilds = []
        for g in guilds:
            try:
                row = await self.bot.db.reports.claim_next_queued_autofix(int(g.id))
            except Exception:
                log.debug("autofix worker claim failed gid=%s", g.id, exc_info=True)
                continue
            if not row:
                continue
            await self._process_queued_autofix(int(g.id), dict(row))

    async def _process_queued_autofix(
        self, guild_id: int, queue_row: dict,
    ) -> None:
        """Generate a proposal for one report. Updates the queue row and
        DMs the recipient with the outcome. Stashes the patch in memory
        when validation passes so the Open PR button can ship it.
        """
        from pathlib import Path as _Path
        report_id = int(queue_row.get("report_id") or 0)
        report = await self.bot.db.reports.get_report(report_id)
        if not report:
            await self.bot.db.reports.update_autofix_status(
                report_id, "failed",
                last_error="Report row missing at proposal time.",
            )
            return
        try:
            from core.framework.ai.heal_ai import get_heal_ai_config
            from core.framework.ai import report_ai as _rai
            from core.framework.ai import auto_fix as _af
            ai_cfg = await get_heal_ai_config(self.bot.db, guild_id)
            signals = await _rai.gather_signals(
                self.bot.db, guild_id, dict(report),
            )
            proposal = await _af.propose_fix(
                report_text=str(report.get("message") or ""),
                signals=signals,
                config=ai_cfg,
                repo_root=_Path(__file__).resolve().parent.parent,
            )
        except Exception as exc:
            log.exception(
                "autofix worker: propose_fix crashed for report %s", report_id,
            )
            await self.bot.db.reports.update_autofix_status(
                report_id, "failed",
                last_error=f"propose_fix raised: {exc!r}",
            )
            await self._dm_status_update(
                report_id, "failed", reason=str(exc)[:200],
            )
            return
        # Race check: between ``claim_next_queued_autofix`` flipping the
        # row to ``generating`` and the LLM call returning, an admin
        # may have run ,admin reports queue cancel and set status =
        # 'discarded'. If so, we skip every write below + skip the DMs
        # so cancel actually sticks instead of getting clobbered by a
        # late completion.
        latest = await self.bot.db.reports.get_autofix_entry(report_id)
        if latest and str(latest.get("status")) != "generating":
            log.info(
                "autofix worker: row %s status changed to %s while LLM "
                "call was in flight; skipping write",
                report_id, latest.get("status"),
            )
            self._pending_autofixes.pop(report_id, None)
            return
        if isinstance(proposal, _af.PatchRejection):
            # Map rejection stage to the queue's terminal status.
            # ``locate`` / ``path_denied`` / ``file_missing`` -> unfixable
            # (the AI can't act on this report at all). ``file_too_large``
            # / ``generate`` / ``validate`` -> failed (technical issue,
            # could be retried later).
            terminal = (
                "unfixable" if proposal.stage in
                ("locate", "path_denied", "file_missing")
                else "failed"
            )
            note = f"[{proposal.stage}] {proposal.reason}"
            if proposal.rel_path:
                note = f"{note} (file: {proposal.rel_path})"
            await self.bot.db.reports.update_autofix_status(
                report_id, terminal,
                last_error=note[:1500],
            )
            await self._dm_status_update(
                report_id, terminal,
                reason=note[:600],
            )
            await self._dm_autofix_trace(
                report_id, terminal, list(proposal.trace),
                report_text=str(report.get("message") or ""),
                rejection_reason=note,
                picked_path=str(proposal.rel_path or ""),
            )
            return
        # Patch validated. Open the issue (best-effort) BEFORE storing
        # ``proposed`` so the recipient's DM has the issue link, then
        # stash the patch in memory keyed by report id.
        existing = await self.bot.db.reports.get_autofix_entry(report_id)
        if not (existing or {}).get("issue_url"):
            await self._autofix_open_issue(
                dict(report), verdict="", requested_by=int(queue_row.get("requested_by") or 0),
            )
        self._pending_autofixes[report_id] = {
            "proposal":   proposal,
            "report_row": dict(report),
            "verdict":    "",
        }
        row = await self.bot.db.reports.update_autofix_status(
            report_id, "proposed",
            proposed_path=proposal.rel_path,
            proposed_lines=int(proposal.lines_changed),
            last_error="",
        )
        await self._dm_status_update(
            report_id, "proposed", row=row, proposal=proposal,
        )
        await self._dm_autofix_trace(
            report_id, "proposed", list(proposal.trace),
            report_text=str(report.get("message") or ""),
            picked_path=str(proposal.rel_path),
        )
        # Send Open PR / Discard buttons in DM so the admin can act
        # without bouncing to a server channel. Best-effort -- the
        # status DM above already conveys the proposal info, the view
        # is just a convenience button surface.
        try:
            user = await _fetch_admin(self.bot)
            if user is not None:
                view = _AutoFixConfirmView(self, report_id)
                await user.send(
                    content=(
                        f"\U0001F527 Auto-fix for report **#{report_id}** "
                        f"is ready. Click **Open PR** to push the draft "
                        f"or **Discard** to drop it."
                    ),
                    view=view,
                )
        except discord.Forbidden:
            log.info("autofix: DM blocked, recipient has DMs disabled")
        except Exception:
            log.debug("autofix: confirm DM send failed", exc_info=True)

    async def _dm_status_update(
        self,
        report_id: int,
        status: str,
        *,
        row: dict | None = None,
        proposal: object | None = None,
        pr_url: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Compact lifecycle DM. ``status`` drives the wording; the rest
        is whatever metadata the caller has at hand.
        """
        title_map = {
            "issue_opened": "\U0001F4DD Issue opened",
            "queued":       "\U0001F551 Queued for auto-fix",
            "proposed":     "\U0001F527 Auto-fix proposal ready",
            "pr_open":      "\U0001F680 Auto-fix PR opened",
            "discarded":    "\U0001F5D1 Auto-fix discarded",
            "failed":       "⚠ Auto-fix failed",
            "unfixable":    "⚠ Auto-fix declined",
        }
        title = title_map.get(status, f"Auto-fix: {status}")
        embed = card(title, color=C_INFO).description(
            f"Report **#{report_id}**"
        )
        if row and row.get("issue_url"):
            embed = embed.field(
                "Tracking issue", str(row["issue_url"]), False,
            )
        if proposal is not None:
            try:
                embed = (
                    embed
                    .field("File", f"`{proposal.rel_path}`", True)  # type: ignore[attr-defined]
                    .field("Lines", f"~{proposal.lines_changed}", True)  # type: ignore[attr-defined]
                )
            except Exception:
                pass
        if pr_url:
            embed = embed.field("PR", pr_url, False)
        if reason:
            embed = embed.field("Reason", reason[:1000], False)
        await self._dm_report_recipient(embed=embed.build())

    # ── Daily report summary + DM cleanup ───────────────────────────────────

    @tasks.loop(hours=24)
    async def daily_report_summary(self) -> None:
        """Post a daily report summary to each guild's reports feed channel."""
        for guild in self.bot.guilds:
            try:
                settings = await self.bot.db.get_guild_settings(guild.id)
                ch_id = settings.get("reports_feed_channel")
                if not ch_id:
                    continue
                channel = guild.get_channel_or_thread(ch_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(ch_id)
                    except Exception:
                        continue
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    continue

                summary = await self.bot.db.reports.get_report_summary(guild.id)
                if summary["total"] == 0:
                    continue

                # Filter to only configured categories
                allowed_cats = {
                    c.strip() for c in
                    (settings.get("reports_feed_categories") or "bugs,suggestions,users,other").split(",")
                    if c.strip()
                }

                filtered_total = sum(summary["by_category"].get(c, 0) for c in allowed_cats)
                if filtered_total == 0:
                    continue

                _b = card("📊 Daily Report Summary", color=C_INFO)
                _b.field("Total Open Reports", str(filtered_total), True)

                # By category
                cat_lines = []
                for cat in ("bugs", "suggestions", "users", "other"):
                    if cat not in allowed_cats:
                        continue
                    cnt = summary["by_category"].get(cat, 0)
                    if cnt:
                        cat_lines.append(f"**{cat.capitalize()}**: {cnt}")
                if cat_lines:
                    _b.field("By Category", "\n".join(cat_lines), True)

                # By status (filtered to allowed categories)
                status_totals: dict[str, int] = {}
                for (cat, st), cnt in summary["by_category_status"].items():
                    if cat in allowed_cats:
                        status_totals[st] = status_totals.get(st, 0) + cnt
                status_lines = []
                for st in ("open", "accepted", "in_progress", "resolved"):
                    cnt = status_totals.get(st, 0)
                    if cnt:
                        s_label, s_emoji, _ = STATUSES.get(st, (st, "?", 0))
                        status_lines.append(f"{s_emoji} **{s_label}**: {cnt}")
                if status_lines:
                    _b.field("By Status", "\n".join(status_lines), True)

                # Detailed breakdown
                detail_lines = []
                for cat in ("bugs", "suggestions", "users", "other"):
                    if cat not in allowed_cats:
                        continue
                    for st in ("open", "accepted", "in_progress", "resolved"):
                        cnt = summary["by_category_status"].get((cat, st), 0)
                        if cnt:
                            s_label, s_emoji, _ = STATUSES.get(st, (st, "?", 0))
                            detail_lines.append(f"{s_emoji} {cat.capitalize()} / {s_label}: **{cnt}**")
                if detail_lines:
                    _b.field("Breakdown", "\n".join(detail_lines[:15]), False)

                import datetime
                _b.footer(f"Summary generated {fmt_ts(datetime.datetime.utcnow(), '%Y-%m-%d %H:%M UTC')}")
                embed = _b.build()
                await channel.send(embed=embed)

                # DM the admin a full-text version of recent reports
                admin = await _fetch_admin(self.bot)
                if admin:
                    try:
                        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
                        recent_reports = await self.bot.db.reports.get_all_reports_since(guild.id, since)
                        if recent_reports:
                            dm_parts = [f"**📊 Daily Report Summary  -  {guild.name}**\n"
                                        f"**{len(recent_reports)}** reports in the last 24h\n"]
                            for r in recent_reports[:20]:
                                s_label, s_emoji, _ = STATUSES.get(r["status"], (r["status"], "?", 0))
                                member = guild.get_member(r["user_id"])
                                reporter = member.display_name if member else f"User {r['user_id']}"
                                ts_str = fmt_ts(r["created_at"], "%H:%M")
                                dm_parts.append(
                                    f"\n{s_emoji} **#{r['id']}** [{r['category']}] by **{reporter}** at {ts_str}\n"
                                    f"> {r['message'][:300]}"
                                )
                            if len(recent_reports) > 20:
                                dm_parts.append(f"\n*…and {len(recent_reports) - 20} more*")
                            dm_text = "\n".join(dm_parts)
                            dm_channel = admin.dm_channel or await admin.create_dm()
                            # Split if too long for Discord
                            if len(dm_text) > 1900:
                                for i in range(0, len(dm_text), 1900):
                                    await dm_channel.send(dm_text[i:i+1900])
                            else:
                                await dm_channel.send(dm_text)
                    except Exception:
                        pass
            except Exception:
                pass

    @daily_report_summary.before_loop
    async def _before_daily_summary(self) -> None:
        await self.bot.wait_until_ready()

    # ── Auto-cleanup closed report DMs ──────────────────────────────────────

    @tasks.loop(hours=24)
    async def cleanup_closed_report_dms(self) -> None:
        """Delete admin DM notifications for reports closed more than REPORT_DM_CLEANUP_DAYS days ago."""
        days = Config.REPORT_DM_CLEANUP_DAYS
        if days <= 0:
            return
        admin = await _fetch_admin(self.bot)
        if not admin:
            return
        try:
            stale = await self.bot.db.reports.get_stale_closed_report_dms(days)
        except Exception:
            return
        for row in stale:
            dm_id = row.get("dm_message_id")
            if not dm_id:
                continue
            try:
                dm_channel = admin.dm_channel or await admin.create_dm()
                msg = await dm_channel.fetch_message(dm_id)
                await msg.delete()
            except Exception:
                pass
            # Always clear the stored ID so we don't retry failed deletions
            try:
                await self.bot.db.reports.clear_dm_message_id(row["id"])
            except Exception:
                pass

    @cleanup_closed_report_dms.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ── Leaderboards ──────────────────────────────────────────────────────

    @commands.hybrid_command(name="report-leaderboard", aliases=["reportlb", "reportleaderboard"], with_app_command=False)
    @guild_only
    async def report_leaderboard(self, ctx: DiscoContext) -> None:
        """View the top reporters by accepted report count."""
        rows = await self.bot.db.reports.get_report_leaderboard(ctx.guild.id)
        if not rows:
            await ctx.reply_error("No reports have been submitted yet.")
            return
        lines = []
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        for i, row in enumerate(rows):
            medal = medals.get(i, f"`{i+1}.`")
            member = ctx.guild.get_member(row["user_id"])
            name = member.display_name if member else f"User {row['user_id']}"
            accepted = int(row["accepted"])
            total = int(row["total_reports"])
            rewarded = to_human(int(row["total_rewarded"] or 0))
            reward_str = f" · **${rewarded:,.2f}** earned" if rewarded > 0 else ""
            lines.append(f"{medal} **{name}**  -  {accepted} accepted / {total} total{reward_str}")
        embed = card("📋 Report Leaderboard", description="\n".join(lines), color=C_INFO).build()
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="bugbounty-leaderboard", aliases=["bblb", "bugbountylb", "bountylb"], with_app_command=False)
    @guild_only
    async def bugbounty_leaderboard(self, ctx: DiscoContext) -> None:
        """View the top bug bounty hunters by earnings."""
        rows = await self.bot.db.reports.get_bugbounty_leaderboard(ctx.guild.id)
        if not rows:
            await ctx.reply_error("No bug bounty reports have been submitted yet.")
            return
        lines = []
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        for i, row in enumerate(rows):
            medal = medals.get(i, f"`{i+1}.`")
            member = ctx.guild.get_member(row["user_id"])
            name = member.display_name if member else f"User {row['user_id']}"
            reports = int(row["bounty_reports"])
            earned = to_human(int(row["total_earned"] or 0))
            lines.append(f"{medal} **{name}**  -  **${earned:,.2f}** earned ({reports} bounties)")
        embed = card("🏆 Bug Bounty Leaderboard", description="\n".join(lines), color=C_GOLD).build()
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Report(bot))
