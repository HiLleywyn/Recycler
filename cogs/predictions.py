"""cogs/predictions.py - Polymarket-style prediction markets.

Players bet on real-world outcomes (YES/NO or multi-choice). Winnings are
proportional to your share of the winning pool (parimutuel system). Markets
are created by admins and resolved when the outcome is known.

5% house cut goes to the player who created the market (or treasury).
"""
from __future__ import annotations

import json
import time

from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots, module_cog_check
from core.framework.ui import C_GOLD, C_SUCCESS, ConfirmView, fmt_usd, fmt_ts
from core.framework.fuzzy import suggest_subcommand
from core.framework.scale import to_raw, to_human

HOUSE_CUT = 0.05  # 5% cut from the winning pool


class Predictions(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_check(self, ctx) -> bool:

        return await module_cog_check(self.bot, ctx, "predictions")

    # -- $predict group --

    @commands.hybrid_group(name="predict", aliases=["prediction", "bet"], invoke_without_command=True, with_app_command=False)
    @guild_only
    async def predict(self, ctx: DiscoContext) -> None:
        """Prediction market commands. Bet on outcomes of real events."""
        if await suggest_subcommand(ctx, self.predict):
            return
        await ctx.send_group_help(self.predict, title="Prediction Markets", color=C_GOLD)

    # -- $predict list --

    @predict.command(name="list", aliases=["open", "markets"])
    @guild_only
    async def predict_list(self, ctx: DiscoContext) -> None:
        """View all open prediction markets."""
        markets = await ctx.db.get_open_markets(ctx.guild_id)
        if not markets:
            await ctx.reply_error("No open prediction markets right now.")
            return

        embed = card("Open Prediction Markets", color=C_GOLD)
        for m in markets[:15]:
            options = json.loads(m["options"]) if isinstance(m["options"], str) else m["options"]
            pool = float(m["total_pool"])
            prize_pool = float(m.get("prize_pool") or 0)
            end_str = fmt_ts(m["end_time"], "%b %d, %Y %H:%M UTC")
            cat = m.get("category", "general")
            prize_str = f" | Prize Pool: ${prize_pool:,.2f}" if prize_pool > 0 else ""
            embed.field(
                f"#{m['id']} - {m['question']}",
                f"Pool: ${pool:,.2f}{prize_str} | Options: {', '.join(options)} | Ends: {end_str} | {cat}",
                False,
            )
        embed.footer(f"Use {ctx.prefix}predict view <id> for details, {ctx.prefix}predict bet <id> <option> <amount> to bet")
        await ctx.reply(embed=embed.build(), mention_author=False)

    # -- $predict view <id> --

    @predict.command(name="view", aliases=["info", "details"])
    @guild_only
    async def predict_view(self, ctx: DiscoContext, market_id: int = None) -> None:
        """View details and odds for a prediction market."""
        if market_id is None:
            await self.predict_list(ctx)
            return
        market = await ctx.db.get_market(market_id)
        if not market or market["guild_id"] != ctx.guild_id:
            await ctx.reply_error("Market not found.")
            return

        options = json.loads(market["options"]) if isinstance(market["options"], str) else market["options"]
        pools = await ctx.db.get_market_pools(market_id)
        total = float(market["total_pool"])

        status_emoji = {"open": "🟢", "closed": "🔴", "resolved": "✅", "cancelled": "❌"}
        embed = card(f"#{market_id} - {market['question']}", color=C_GOLD)
        if market.get("description"):
            embed.description(market["description"])

        embed.field("Status", f"{status_emoji.get(market['status'], '?')} {market['status'].title()}", True)
        embed.field("Category", market.get("category", "general").title(), True)
        prize_pool = float(market.get("prize_pool") or 0)
        pool_str = fmt_usd(total) + (f" ({fmt_usd(prize_pool)} seeded)" if prize_pool > 0 else "")
        embed.field("Total Pool", pool_str, True)

        embed.field("Ends", fmt_ts(market["end_time"], "%b %d, %Y %H:%M UTC"), True)

        # Show odds per option
        for opt in options:
            opt_pool = pools.get(opt, 0.0)
            pct = (opt_pool / total * 100) if total > 0 else 0
            potential = (total / opt_pool) if opt_pool > 0 else 0
            marker = " (WINNER)" if market["status"] == "resolved" and market.get("resolved_option") == opt else ""
            embed.field(
                f"{opt}{marker}",
                f"Pool: ${opt_pool:,.2f} ({pct:.1f}%) | Payout: {potential:.2f}x",
                True,
            )

        # Show user's bets on this market
        if ctx.author:
            user_bets = await ctx.db.get_user_bets(ctx.author.id, ctx.guild_id)
            my_bets = [b for b in user_bets if b["market_id"] == market_id]
            if my_bets:
                bet_lines = [f"{fmt_usd(to_human(int(b['amount'] or 0)))} on {b['option']}" for b in my_bets]
                embed.field("Your Bets", " | ".join(bet_lines), False)

        embed.footer(f"Bet with {ctx.prefix}predict bet {market_id} <option> <amount>")
        await ctx.reply(embed=embed.build(), mention_author=False)

    # -- $predict bet <id> <option> <amount> --

    @predict.command(name="bet", aliases=["wager", "place"])
    @guild_only
    @no_bots
    @ensure_registered
    async def predict_bet(self, ctx: DiscoContext, market_id: int, option: str, amount: str) -> None:
        """Place a bet on a prediction market outcome. Use 'all' for your full wallet."""
        # Parse amount  -  support "all" keyword
        user = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        wallet = user.h("wallet")

        if amount.lower() == "all":
            bet_amount = wallet
        else:
            try:
                bet_amount = float(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number or `all`.")
                return

        if bet_amount <= 0:
            await ctx.reply_error("Bet amount must be positive.")
            return
        if bet_amount < 1:
            await ctx.reply_error("Minimum bet is $1.")
            return

        market = await ctx.db.get_market(market_id)
        if not market or market["guild_id"] != ctx.guild_id:
            await ctx.reply_error("Market not found.")
            return
        if market["status"] != "open":
            await ctx.reply_error(f"This market is {market['status']}. No new bets allowed.")
            return

        # Check if market has expired (end_time may be epoch float from DB coercion)
        now_ts = time.time()
        end_time = market["end_time"]
        if hasattr(end_time, "timestamp"):
            end_ts = end_time.timestamp()
        elif isinstance(end_time, (int, float)):
            end_ts = float(end_time)
        else:
            end_ts = float("inf")
        if now_ts >= end_ts:
            await ctx.reply_error("This market has expired. Waiting for resolution.")
            return

        options = json.loads(market["options"]) if isinstance(market["options"], str) else market["options"]
        option_upper = option.upper()
        if option_upper not in [o.upper() for o in options]:
            await ctx.reply_error(f"Invalid option. Choose from: {', '.join(options)}")
            return
        # Match the exact casing from the options list
        option = next(o for o in options if o.upper() == option_upper)

        # Check balance
        if wallet < bet_amount:
            await ctx.reply_error(f"You need **${bet_amount:,.2f}** but only have **${wallet:,.2f}** in your wallet.")
            return
        amount = bet_amount  # reassign for downstream code

        view = ConfirmView(ctx.author.id)
        msg = await ctx.reply(
            f"Bet **${amount:,.2f}** on **{option}** for:\n> {market['question']}",
            view=view, mention_author=False,
        )
        confirmed = await view.wait_result()
        if not confirmed:
            await msg.edit(content="Bet cancelled.", view=None)
            return

        # Re-check balance after confirmation (prevent TOCTOU race)
        fresh_user = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        fresh_wallet = to_human(fresh_user.get("wallet", 0) or 0)
        if fresh_wallet < amount:
            await msg.edit(
                content=f"Balance changed since confirmation. You now have **${fresh_wallet:,.2f}**.",
                view=None,
            )
            return

        # Re-check market is still open (could have been resolved during confirmation)
        fresh_market = await ctx.db.get_market(market_id)
        if not fresh_market or fresh_market["status"] != "open":
            await msg.edit(content="Market closed while you were confirming.", view=None)
            return

        # Deduct from wallet and place bet
        await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(-amount))
        await ctx.db.place_bet(ctx.guild_id, market_id, ctx.author.id, option, amount)

        # Calculate current odds
        pools = await ctx.db.get_market_pools(market_id)
        new_total = sum(pools.values())
        my_pool = pools.get(option, 0.0)
        payout_mult = (new_total / my_pool) if my_pool > 0 else 0

        embed = card("Bet Placed", color=C_SUCCESS)
        embed.field("Market", market["question"], False)
        embed.field("Your Bet", f"{fmt_usd(amount)} on {option}", True)
        embed.field("Current Odds", f"{payout_mult:.2f}x payout if {option} wins", True)
        embed.field("Pool Total", fmt_usd(new_total), True)
        await msg.edit(content=None, embed=embed.build(), view=None)

    # -- $predict mybets --

    @predict.command(name="mybets", aliases=["bets", "my"])
    @guild_only
    @no_bots
    @ensure_registered
    async def predict_mybets(self, ctx: DiscoContext) -> None:
        """View all your active prediction bets."""
        bets = await ctx.db.get_user_bets(ctx.author.id, ctx.guild_id)
        if not bets:
            await ctx.reply_error("You haven't placed any bets yet.")
            return

        embed = card(f"{ctx.author.display_name}'s Prediction Bets", color=C_GOLD)
        for b in bets[:20]:
            status_emoji = {"open": "🟢", "closed": "🔴", "resolved": "✅", "cancelled": "❌"}
            s = b.get("status", "open")
            won = s == "resolved" and b.get("resolved_option") == b["option"]
            result = " (WON)" if won else (" (LOST)" if s == "resolved" else "")
            embed.field(
                f"#{b['market_id']} - {b.get('question', '?')[:60]}",
                f"{fmt_usd(to_human(int(b['amount'] or 0)))} on {b['option']} | {status_emoji.get(s, '?')} {s.title()}{result}",
                False,
            )
        await ctx.reply(embed=embed.build(), mention_author=False)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Predictions(bot))
