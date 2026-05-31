"""
cogs/premium.py  -  User-facing premium commands + PremiumGateFailure handler.

Discoin runs as a shared multi-tenant bot. The trading economy, gambling,
bank/profile, and basic buddy management are free everywhere. Cost-heavy
or compute-heavy features (AI, fishing, crafting, delves, expeditions,
buddy battles/breeding/market) are gated behind a per-guild premium
subscription. This cog gives server owners the surface to:

    ,premium                  - alias for ,premium status
    ,premium status           - what's the current guild's tier?
    ,premium info             - pricing + feature list
    ,premium subscribe        - PayPal approval link (admin-only)
    ,premium cancel           - cancel a PayPal subscription (admin-only)
    ,premium features         - what's free vs paid

The owner-side ,admin premium grant/revoke/list/status lives in
cogs/admin.py since it hangs off the existing admin command group.

This cog also installs a global on_command_error listener that catches
PremiumGateFailure (raised by PremiumCog.cog_check) and routes it into
ctx.reply_premium_required so PremiumCog and @premium_required produce
the same UX.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only
from core.framework.ui import (
    C_GOLD, C_NEUTRAL, C_SUCCESS, fmt_ts,
)
from services import entitlements
from services.paypal import paypal_client, find_approve_link, PayPalError

log = logging.getLogger(__name__)


def _require_manage_guild_local():
    """Local copy so we don't import from cogs.admin (one-way dependency)."""
    async def predicate(ctx: DiscoContext) -> bool:
        if not ctx.guild:
            raise commands.CheckFailure("This command can only be used in a server.")
        if not ctx.author.guild_permissions.manage_guild:
            raise commands.CheckFailure(
                "You need **Manage Server** permission to manage premium subscriptions."
            )
        return True
    return commands.check(predicate)


class Premium(commands.Cog):
    """Server-owner premium subscription surface."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self.expire_sweep.start()

    def cog_unload(self) -> None:
        self.expire_sweep.cancel()

    # ── background sweep ──────────────────────────────────────────

    @tasks.loop(minutes=15)
    async def expire_sweep(self) -> None:
        """Flip overdue rows to 'expired' every 15 minutes. Cheap UPDATE
        with a partial index, so this is fine to run in the bot loop."""
        try:
            await entitlements.expire_overdue(self.bot.db)
        except Exception:
            log.exception("premium expire_sweep failed")

    @expire_sweep.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # PremiumGateFailure is rendered by the bot's on_command_error in
    # core/framework/bot.py -- one place, one card, no duplicate replies.

    # ── onboarding ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Drop a welcome message when the bot is added to a new server.

        Posts to the system channel (or first writable text channel) with
        a one-liner about what's free, what's premium, and how to start
        a subscription. Falls back to DMing the guild owner if no channel
        is writable -- many self-hosted listings hit "Manage Server"
        permissions but not "View Channel" on the welcome target.
        """
        if entitlements.is_host_guild(guild.id):
            return  # the host's own home server doesn't need a sales pitch
        prefix = Config.PREFIX
        embed = (
            card(
                "\U0001F44B Welcome to Discoin!",
                description=(
                    "Discoin is a free Discord economy bot. The trading economy, "
                    "gambling, bank, profile, jobs, and basic buddy management are "
                    "free for **every server**, no subscription needed."
                ),
                color=C_GOLD,
            )
            .field(
                "Free everywhere",
                f"`{prefix}help` -- start here\n"
                f"`{prefix}work` `{prefix}daily` `{prefix}profile`\n"
                f"`{prefix}bal` `{prefix}trade` `{prefix}gamble`\n"
                f"`{prefix}buddy hatch`  -  get a buddy companion",
                False,
            )
            .field(
                "Premium features",
                "AI chat, fishing, farming, crafting, delves, expeditions, "
                "buddy battles, buddy breeding, buddy auction house",
                False,
            )
            .field(
                "Subscribe",
                f"Server admins: `{prefix}premium info` to see plans, "
                f"`{prefix}premium subscribe` for a PayPal link.",
                False,
            )
            .footer(f"{prefix}premium status -- check this server's tier")
            .build()
        )
        target = guild.system_channel
        if target is None or not target.permissions_for(guild.me).send_messages:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    target = ch
                    break
        try:
            if target is not None:
                await target.send(embed=embed)
                return
        except Exception:
            log.debug("on_guild_join: send to %s failed", target, exc_info=True)
        # Fallback: DM the owner
        try:
            owner = guild.owner or await self.bot.fetch_user(guild.owner_id)
            if owner is not None:
                await owner.send(embed=embed)
        except Exception:
            log.debug("on_guild_join: DM to owner of %s failed", guild.id, exc_info=True)

    # ── user-facing commands ──────────────────────────────────────

    @commands.group(name="premium", aliases=["sub", "subscribe", "subscription"],
                    invoke_without_command=True)
    @guild_only
    async def premium(self, ctx: DiscoContext) -> None:
        """Show this server's premium status. Default subcommand."""
        await self._cmd_status(ctx)

    @premium.command(name="status", aliases=["state"])
    @guild_only
    async def premium_status(self, ctx: DiscoContext) -> None:
        """Show this server's premium status."""
        await self._cmd_status(ctx)

    async def _cmd_status(self, ctx: DiscoContext) -> None:
        s = await entitlements.get_status(ctx.guild_id, ctx.db)
        prefix = await ctx.get_guild_prefix()
        if s.is_premium:
            color = C_GOLD if s.source != "host" else C_SUCCESS
            title = "\U0001F511 Premium -- ACTIVE"
            if s.source == "host":
                desc = "This is the host server. Every premium feature is unlocked."
            elif s.expires_at:
                desc = (
                    f"Premium is active and expires **{fmt_ts(s.expires_at)}**."
                )
            else:
                desc = "Premium is active (no expiry)."
        else:
            color = C_NEUTRAL
            title = "\U0001F512 Premium -- inactive"
            desc = (
                f"Premium isn't active on this server.\n\n"
                f"Run `{prefix}premium info` to see plans and subscribe."
            )
        b = card(title, description=desc, color=color)
        b.field("Source", s.source, True)
        b.field("Status", s.status, True)
        if s.expires_at and s.source != "host":
            b.field("Expires", fmt_ts(s.expires_at), True)
        if s.current_period_end and s.source == "paypal":
            b.field("Next billing", fmt_ts(s.current_period_end), True)
        if s.subscriber_user_id:
            b.field("Subscriber", f"<@{s.subscriber_user_id}>", True)
        b.footer(f"{prefix}premium info -- features and pricing")
        await ctx.reply(embed=b.build(), mention_author=False)

    @premium.command(name="info", aliases=["plans", "pricing"])
    @guild_only
    async def premium_info(self, ctx: DiscoContext) -> None:
        """Show plans, prices, and what's free vs paid."""
        prefix = await ctx.get_guild_prefix()
        b = card("\U0001F511 Discoin Premium", color=C_GOLD)
        b.description(
            "Premium unlocks the cost-heavy features for **everyone in this server**. "
            "The trading economy, gambling, bank, and basic buddy management are "
            "always free."
        )
        b.field(
            "Free everywhere",
            "Trading, gambling, bank/profile, basic buddy management "
            "(hatch, rename, storage, BUD economy)",
            False,
        )
        b.field(
            "Premium",
            ", ".join(entitlements.PREMIUM_FEATURES.values()),
            False,
        )
        if Config.PAYPAL_PLAN_ID_MONTHLY or Config.PAYPAL_PLAN_ID_YEARLY:
            plan_lines: list[str] = []
            if Config.PAYPAL_PLAN_ID_MONTHLY:
                plan_lines.append(f"Monthly  -  {Config.PREMIUM_PRICE_MONTHLY_DISPLAY}")
            if Config.PAYPAL_PLAN_ID_YEARLY:
                plan_lines.append(f"Yearly   -  {Config.PREMIUM_PRICE_YEARLY_DISPLAY}")
            b.field("Plans", "\n".join(plan_lines), False)
            b.field(
                "Subscribe",
                f"Server admins: run `{prefix}premium subscribe` to get a PayPal link.",
                False,
            )
        else:
            b.field(
                "Plans",
                "PayPal subscriptions aren't configured on this instance yet -- "
                "the bot owner can grant premium manually.",
                False,
            )
        await ctx.reply(embed=b.build(), mention_author=False)

    @premium.command(name="features", aliases=["what"])
    @guild_only
    async def premium_features(self, ctx: DiscoContext) -> None:
        """List every premium feature key + description."""
        lines = [f"`{k}`  -  {v}" for k, v in entitlements.PREMIUM_FEATURES.items()]
        b = card(
            "\U0001F511 Premium feature list",
            description="\n".join(lines),
            color=C_GOLD,
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @premium.command(name="subscribe", aliases=["buy", "purchase"])
    @guild_only
    @_require_manage_guild_local()
    async def premium_subscribe(
        self,
        ctx: DiscoContext,
        plan: str = "monthly",
    ) -> None:
        """Generate a PayPal subscription link. Admin-only.

        Usage:
            ,premium subscribe          # monthly
            ,premium subscribe yearly
        """
        plan = (plan or "monthly").lower()
        plan_id = (
            Config.PAYPAL_PLAN_ID_YEARLY if plan in ("yearly", "annual", "year")
            else Config.PAYPAL_PLAN_ID_MONTHLY
        )
        if not plan_id:
            await ctx.reply_error(
                "PayPal isn't configured on this Discoin instance. "
                "Ask the bot owner to set `PAYPAL_PLAN_ID_MONTHLY` / "
                "`PAYPAL_PLAN_ID_YEARLY` and reload."
            )
            return
        client = paypal_client()
        if not client.configured:
            await ctx.reply_error(
                "PayPal isn't configured on this Discoin instance. "
                "Ask the bot owner to set `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`."
            )
            return
        return_url = (Config.PAYPAL_RETURN_URL or "https://example.com/return").replace(
            "{gid}", str(ctx.guild_id))
        cancel_url = (Config.PAYPAL_CANCEL_URL or "https://example.com/cancel").replace(
            "{gid}", str(ctx.guild_id))
        try:
            resp = await client.create_subscription(
                plan_id,
                custom_id=str(ctx.guild_id),
                return_url=return_url,
                cancel_url=cancel_url,
            )
        except PayPalError as exc:
            log.exception("paypal create_subscription failed")
            await ctx.reply_error(
                f"PayPal rejected the subscription request: `{exc}`"
            )
            return
        approve = find_approve_link(resp)
        if not approve:
            await ctx.reply_error(
                "PayPal didn't return an approval link. Check the bot logs."
            )
            return
        b = card(
            "\U0001F511 Discoin Premium -- subscribe",
            description=(
                f"Click below to approve the **{plan}** subscription with PayPal. "
                f"Premium activates automatically on this server (`{ctx.guild_id}`) "
                f"once payment is confirmed."
            ),
            color=C_GOLD,
        )
        b.field("Approval link", approve, False)
        b.footer("The link is single-use and expires within 3 hours.")
        await ctx.reply(embed=b.build(), mention_author=False)

    @premium.command(name="cancel", aliases=["unsubscribe"])
    @guild_only
    @_require_manage_guild_local()
    async def premium_cancel(
        self,
        ctx: DiscoContext,
        *, reason: str = "Cancelled by server admin",
    ) -> None:
        """Cancel this server's PayPal subscription (admin-only)."""
        s = await entitlements.get_status(ctx.guild_id, ctx.db)
        if s.source == "host":
            await ctx.reply_error(
                "This is the host server -- it can't be cancelled here."
            )
            return
        if not s.paypal_subscription_id:
            await ctx.reply_error(
                "No PayPal subscription is linked to this server. "
                "If your premium was granted by the bot owner, ask them to revoke it."
            )
            return
        client = paypal_client()
        if not client.configured:
            await ctx.reply_error("PayPal isn't configured on this instance.")
            return
        confirmed = await ctx.confirm(
            f"Cancel PayPal subscription `{s.paypal_subscription_id}`? "
            f"You'll keep premium until **{fmt_ts(s.current_period_end) if s.current_period_end else 'now'}**."
        )
        if not confirmed:
            await ctx.reply(
                embed=card(
                    description="Cancellation aborted.",
                    color=C_NEUTRAL,
                ).build(),
                mention_author=False,
            )
            return
        try:
            await client.cancel_subscription(s.paypal_subscription_id, reason=reason)
        except PayPalError as exc:
            log.exception("paypal cancel_subscription failed")
            await ctx.reply_error(f"PayPal rejected the cancellation: `{exc}`")
            return
        # PayPal will fire BILLING.SUBSCRIPTION.CANCELLED -> we'll write the
        # row through the webhook. Show the user a confirmation now.
        await ctx.reply_success(
            "Cancellation submitted. You'll keep premium until the end of the "
            "current billing period; PayPal will confirm via webhook.",
            title="\U0001F512 Cancelling premium",
        )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Premium(bot))
