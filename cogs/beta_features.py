"""
cogs/beta_features.py  -  Auto-compound (open) and price alerts (beta).

Price alerts require beta access. Grant via:
    .admin beta grant price_alerts @user
"""
from __future__ import annotations

import logging

from discord.ext import commands, tasks

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only, no_bots, ensure_registered, check_beta_access
from core.framework.ui import C_SUCCESS, C_ERROR, C_INFO, C_AMBER, C_PURPLE, fmt_token, fmt_ts
from core.framework.cooldowns import user_cooldown

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Auto-Compound
# ═══════════════════════════════════════════════════════════════════════════

class BetaFeatures(commands.Cog):
    """Beta feature commands for opted-in testers."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._alert_check.start()

    def cog_unload(self) -> None:
        self._alert_check.cancel()

    # ── Auto-Compound Commands ────────────────────────────────────────────

    @commands.hybrid_group(name="autocompound", aliases=["ac"], invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def autocompound(self, ctx: DiscoContext) -> None:
        """Auto-restake staking rewards back into the same farm each tick."""
        await ctx.invoke(self.ac_status)

    @autocompound.command(name="on", aliases=["enable"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ac_on(self, ctx: DiscoContext, farm_id: str = "all") -> None:
        """Enable auto-compound for a farm (or all farms)."""
        uid, gid = ctx.author.id, ctx.guild_id
        stakes = await ctx.db.get_user_stakes(uid, gid)
        if not stakes:
            await ctx.reply_error("You have no active staking positions.")
            return

        enabled = []
        for s in stakes:
            vid = s["validator_id"]
            sym = s["symbol"]
            if farm_id.lower() != "all" and vid.upper() != farm_id.upper():
                continue
            await ctx.db.execute(
                """INSERT INTO auto_compound_settings (user_id, guild_id, validator_id, symbol, enabled)
                   VALUES ($1, $2, $3, $4, TRUE)
                   ON CONFLICT (user_id, guild_id, validator_id, symbol)
                   DO UPDATE SET enabled = TRUE""",
                uid, gid, vid, sym,
            )
            enabled.append(f"**{vid}** ({sym})")

        if not enabled:
            await ctx.reply_error(f"Farm `{farm_id}` not found in your positions.")
            return

        desc = "Rewards will be automatically restaked:\n" + "\n".join(f"{e}" for e in enabled)
        embed = card("Auto-Compound Enabled", description=desc, color=C_SUCCESS)
        await ctx.reply(embed=embed.build(), mention_author=False)

    @autocompound.command(name="off", aliases=["disable"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ac_off(self, ctx: DiscoContext, farm_id: str = "all") -> None:
        """Disable auto-compound for a farm (or all farms)."""
        uid, gid = ctx.author.id, ctx.guild_id
        if farm_id.lower() == "all":
            await ctx.db.execute(
                "UPDATE auto_compound_settings SET enabled = FALSE WHERE user_id = $1 AND guild_id = $2",
                uid, gid,
            )
        else:
            await ctx.db.execute(
                "UPDATE auto_compound_settings SET enabled = FALSE "
                "WHERE user_id = $1 AND guild_id = $2 AND validator_id = $3",
                uid, gid, farm_id.upper(),
            )
        await ctx.reply(
            embed=card("⏸️ Auto-Compound Disabled", description=f"Disabled for **{farm_id}**.", color=C_AMBER).build(),
            mention_author=False,
        )

    @autocompound.command(name="status", aliases=["list"])
    @guild_only
    @no_bots
    @ensure_registered
    async def ac_status(self, ctx: DiscoContext) -> None:
        """View your auto-compound settings."""
        rows = await ctx.db.fetch_all(
            "SELECT * FROM auto_compound_settings WHERE user_id = $1 AND guild_id = $2 ORDER BY validator_id",
            ctx.author.id, ctx.guild_id,
        )
        if not rows:
            await ctx.reply(
                embed=card("🔄 Auto-Compound", description="No auto-compound rules set.\nUse `.autocompound on [farm]` to enable.", color=C_INFO).build(),
                mention_author=False,
            )
            return

        lines = []
        total_compounded = 0.0
        total_count = 0
        for r in rows:
            status = "✅ Active" if r["enabled"] else "⏸️ Paused"
            comp_amt = r.h("total_compounded")
            comp_cnt = int(r.get("compound_count") or 0)
            total_compounded += comp_amt
            total_count += comp_cnt
            last = r.get("last_compound_at")
            last_str = f" - last: {fmt_ts(last)}" if last else ""
            stat_str = f"  -  {fmt_token(comp_amt, r['symbol'])} compounded ({comp_cnt}x){last_str}" if comp_cnt > 0 else ""
            lines.append(f"{status} **{r['validator_id']}** ({r['symbol']}){stat_str}")
        embed = card("🔄 Auto-Compound Status", description="\n".join(lines), color=C_PURPLE)
        if total_count > 0:
            embed.field("📊 Compounds", f"**{total_count}x** total", True)
        embed.footer(f"Toggle: .autocompound on/off [farm]")
        await ctx.reply(embed=embed.build(), mention_author=False)

    # ── Price Alerts Commands ─────────────────────────────────────────────

    @commands.hybrid_group(name="alert", aliases=["pricealert", "pa"], invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def alert(self, ctx: DiscoContext) -> None:
        """Set price alerts that DM you. Requires beta access."""
        if not await check_beta_access(self.bot, ctx.guild, ctx.author, "price_alerts"):
            await ctx.reply_error("Price alerts are a **beta feature**. Ask an admin to grant access.")
            return
        await ctx.invoke(self.alert_list)

    @alert.command(name="add", aliases=["set", "create"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def alert_add(self, ctx: DiscoContext, symbol: str, direction: str, price: float) -> None:
        """Set a price alert. Usage: .alert add MTA above 50000"""
        if not await check_beta_access(self.bot, ctx.guild, ctx.author, "price_alerts"):
            await ctx.reply_error("Price alerts are a **beta feature**. Ask an admin to grant access.")
            return

        symbol = symbol.upper()
        direction = direction.lower()
        _DIRECTIONS = ("above", "below")
        if direction not in _DIRECTIONS:
            await ctx.reply_error(f"Direction must be {' or '.join(f'`{d}`' for d in _DIRECTIONS)}.")
            return
        if price <= 0:
            await ctx.reply_error("Price must be positive.")
            return

        # Check token exists
        price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        if not price_row:
            await ctx.reply_error(f"Token **{symbol}** not found.")
            return

        current_price = float(price_row["price"])

        # Limit alerts per user
        existing = await ctx.db.fetch_all(
            "SELECT id FROM price_alerts WHERE user_id = $1 AND guild_id = $2 AND triggered = FALSE",
            ctx.author.id, ctx.guild_id,
        )
        if len(existing) >= 10:
            await ctx.reply_error("You can have at most **10 active alerts**. Remove some first.")
            return

        await ctx.db.execute(
            "INSERT INTO price_alerts (user_id, guild_id, symbol, direction, target_price) VALUES ($1, $2, $3, $4, $5)",
            ctx.author.id, ctx.guild_id, symbol, direction, price,
        )

        arrow = "📈" if direction == "above" else "📉"
        embed = card(
            f"{arrow} Price Alert Set",
            description=(
                f"**{symbol}** {direction} **${price:,.4f}**\n"
                f"Current price: **${current_price:,.4f}**\n"
                f"You'll receive a DM when triggered."
            ),
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @alert.command(name="list", aliases=["ls", "status"])
    @guild_only
    @no_bots
    @ensure_registered
    async def alert_list(self, ctx: DiscoContext) -> None:
        """View your active price alerts."""
        if not await check_beta_access(self.bot, ctx.guild, ctx.author, "price_alerts"):
            await ctx.reply_error("Price alerts are a **beta feature**. Ask an admin to grant access.")
            return

        rows = await ctx.db.fetch_all(
            "SELECT * FROM price_alerts WHERE user_id = $1 AND guild_id = $2 AND triggered = FALSE ORDER BY created_at",
            ctx.author.id, ctx.guild_id,
        )
        if not rows:
            await ctx.reply(
                embed=card("🔔 Price Alerts", description="No active alerts.\nUse `.alert add MTA above 50000` to create one.", color=C_INFO).build(),
                mention_author=False,
            )
            return

        lines = []
        for r in rows:
            arrow = "📈" if r["direction"] == "above" else "📉"
            lines.append(f"{arrow} #{r['id']} **{r['symbol']}** {r['direction']} **${float(r['target_price']):,.4f}**")
        embed = card("🔔 Price Alerts", description="\n".join(lines), color=C_INFO)
        embed.footer("Remove: .alert remove <id>")
        await ctx.reply(embed=embed.build(), mention_author=False)

    @alert.command(name="remove", aliases=["delete", "rm"])
    @guild_only
    @no_bots
    @ensure_registered
    async def alert_remove(self, ctx: DiscoContext, alert_id: int) -> None:
        """Remove a price alert by ID."""
        if not await check_beta_access(self.bot, ctx.guild, ctx.author, "price_alerts"):
            await ctx.reply_error("Price alerts are a **beta feature**. Ask an admin to grant access.")
            return

        result = await ctx.db.execute(
            "DELETE FROM price_alerts WHERE id = $1 AND user_id = $2 AND guild_id = $3",
            alert_id, ctx.author.id, ctx.guild_id,
        )
        await ctx.reply(
            embed=card("🗑️ Alert Removed", description=f"Alert #{alert_id} deleted.", color=C_AMBER).build(),
            mention_author=False,
        )

    @alert.command(name="clear", aliases=["clearall", "removeall"])
    @guild_only
    @no_bots
    @ensure_registered
    async def alert_clear(self, ctx: DiscoContext) -> None:
        """Remove all your price alerts."""
        if not await check_beta_access(self.bot, ctx.guild, ctx.author, "price_alerts"):
            await ctx.reply_error("Price alerts are a **beta feature**. Ask an admin to grant access.")
            return

        result = await ctx.db.execute(
            "DELETE FROM price_alerts WHERE user_id = $1 AND guild_id = $2",
            ctx.author.id, ctx.guild_id,
        )
        await ctx.reply(
            embed=card("🗑️ Alerts Cleared", description="All your price alerts have been removed.", color=C_AMBER).build(),
            mention_author=False,
        )

    # ── Background task: check prices against alerts ──────────────────────

    @tasks.loop(minutes=5)
    async def _alert_check(self) -> None:
        """Check all active price alerts against current prices."""
        try:
            rows = await self.bot.db.fetch_all(
                "SELECT DISTINCT guild_id, symbol FROM price_alerts WHERE triggered = FALSE"
            )
            for row in rows:
                gid, sym = row["guild_id"], row["symbol"]
                price_row = await self.bot.db.get_price(sym, gid)
                if not price_row:
                    continue
                current = float(price_row["price"])

                # Atomically mark alerts as triggered and return them (prevents duplicate DMs)
                triggered = await self.bot.db.fetch_all(
                    "UPDATE price_alerts SET triggered = TRUE, triggered_at = now() "
                    "WHERE guild_id = $1 AND symbol = $2 AND triggered = FALSE "
                    "AND ((direction = 'above' AND $3 >= target_price) OR (direction = 'below' AND $3 <= target_price)) "
                    "RETURNING *",
                    gid, sym, current,
                )
                for alert in triggered:
                    try:
                        user = await self.bot.fetch_user(alert["user_id"])
                        if user:
                            arrow = "📈" if alert["direction"] == "above" else "📉"
                            guild = self.bot.get_guild(gid)
                            guild_name = guild.name if guild else f"Guild {gid}"
                            embed = card(
                                f"{arrow} Price Alert Triggered!",
                                description=(
                                    f"**{sym}** is now **${current:,.4f}**\n"
                                    f"Your alert: {alert['direction']} **${float(alert['target_price']):,.4f}**\n"
                                    f"Server: {guild_name}"
                                ),
                                color=C_SUCCESS if alert["direction"] == "above" else C_ERROR,
                            ).build()
                            await user.send(embed=embed)
                    except Exception:
                        pass  # can't DM user  -  skip

            # Cleanup: delete triggered alerts older than 7 days
            await self.bot.db.execute(
                "DELETE FROM price_alerts WHERE triggered = TRUE AND triggered_at < now() - interval '7 days'"
            )
        except Exception as exc:
            log.warning("Price alert check failed: %s", exc)

    @_alert_check.before_loop
    async def _before_alert_check(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Discoin) -> None:
    await bot.add_cog(BetaFeatures(bot))
