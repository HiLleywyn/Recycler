from __future__ import annotations

import math
import time

import discord
from discord.ext import commands

from core.framework.embed import card
from core.framework.network import normalize_short as normalize_network_short
from core.framework.scale import to_human, to_raw

from core.config import Config
from core.framework.ui import send_paginated
from core.framework.tx import set_tx
from core.framework.cooldowns import user_cooldown
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework import whale as _whale
from core.framework.fuzzy import suggest_subcommand
from core.framework.links import sanitize_embed
from core.framework.utils import parse_amount, ActionSuggestionView
from core.framework.ui import (
    C_AMBER, C_ERROR, C_INFO, C_NEUTRAL, C_SUCCESS, C_WARNING,
    fmt_ts, fmt_usd,
    estimate_cefi_impact, slippage_banner,
)
from services.swap import cancel_depeg_reservation, is_depeg, reserve_depeg_buy
from services.trade import check_trade_cooldown, set_trade_cooldown




_DIRECT_BUY_TOKENS = frozenset(Config.TOKENS.keys())

# ── Trade confirmation view ─────────────────────────────────────────────────

class ConfirmTradeView(discord.ui.View):
    """Shown before executing .buy / .sell / .swap / .send.
    Only the initiating user can respond. Expires in 30 seconds (auto-cancel)."""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=30.0)
        self.confirmed: bool | None = None
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This isn't your trade confirmation.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        self.confirmed = False
        self.stop()


def _parse_sym_amt(arg1: str, arg2: str) -> tuple[str, str]:
    """Accept both 'SYM amount' and 'amount SYM' argument orders.
    Returns (symbol_upper, amount_str). 'all' is treated as an amount.
    A leading '$' is stripped to allow e.g. '$50 MTA'."""
    # Strip $ prefix  -  "$50" means the user is specifying a USD amount
    arg1, arg2 = str(arg1), str(arg2)
    a1 = arg1.lstrip("$") if arg1.startswith("$") else arg1
    a2 = arg2.lstrip("$") if arg2.startswith("$") else arg2
    if a1.lower() == "all":
        return a2.upper(), arg1  # keep original for "all" keyword
    if a2.lower() == "all":
        return a1.upper(), arg2
    try:
        float(a1)
        # arg1 is numeric → order is: amount SYM (keep original $-prefixed form)
        return a2.upper(), arg1
    except ValueError:
        # arg1 is non-numeric → order is: SYM amount
        return a1.upper(), arg2

_NETWORK_SHORT_MAP: dict[str, str] = {
    "Arcadia Network": "arc",
    "Discoin Network":  "dsc",
    "Sun Network":      "sun",
}

def _net_prefix(symbol: str, network_override: str = "") -> str:
    """Return the network shortcode for a token (used as tx hash prefix).
    network_override: full network name if available (e.g. from all_tokens lookup for custom tokens)."""
    if network_override:
        return _NETWORK_SHORT_MAP.get(network_override, "")
    t = Config.TOKENS.get(symbol, {})
    return _NETWORK_SHORT_MAP.get(t.get("network", ""), "")


class Crypto(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    async def cog_check(self, ctx) -> bool:
        return await module_cog_check(self.bot, ctx, "crypto")

    # ── LP-derived USD price ───────────────────────────────────────────────

    async def _derive_usd_price(self, symbol: str, guild_id: int) -> float | None:
        """For sub-tokens: chain through a TOKEN/SUN pool to get USD price."""
        for bridge in ("SUN", "USD"):
            pool_id, ca, cb = self.bot.db.make_pool_id(symbol, bridge)
            pool = await self.bot.db.get_pool(pool_id, guild_id)
            if not pool or pool["reserve_a"] <= 0 or pool["reserve_b"] <= 0:
                continue
            ratio = float(pool["reserve_b"]) / float(pool["reserve_a"]) if ca == symbol else float(pool["reserve_a"]) / float(pool["reserve_b"])
            if bridge == "USD":
                return ratio
            bridge_row = await self.bot.db.get_price(bridge, guild_id)
            if bridge_row:
                return ratio * float(bridge_row["price"])
        return None


    # ── $crypto ───────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="crypto", aliases=["prices"], invoke_without_command=True, with_app_command=False)
    @guild_only
    async def crypto_group(self, ctx: DiscoContext, filter: str = "") -> None:
        """Show token prices. Accepts network, stablecoin, or token filters.
        Examples: .prices  .prices --arc  .prices --sol  .prices USDC  .prices --arb  .prices --sun"""
        if await suggest_subcommand(ctx, self.crypto_group):
            return
        rows = await ctx.db.get_all_prices(ctx.guild_id)
        if not rows:
            await ctx.reply_error("No price data yet. Try again in a moment.")
            return

        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        price_map = {r["symbol"]: r for r in rows}

        # Normalize: strip leading dashes so --arc == arc
        raw = filter.lstrip("-").upper()

        # Resolve filter type
        _NET_SHORT = {
            "SUN": "Sun Network",       "MTA": "Moneta Chain",   "MONETA": "Moneta Chain",
            "ARC": "Arcadia Network",  "ARCADIA": "Arcadia Network",
            "DSC": "Discoin Network",   "DISCOIN": "Discoin Network",
        }
        _STABLE_QUOTES = {
            "USDC": "Arcadia Network",
            "DSD":  "Discoin Network",
        }

        filter_network = _NET_SHORT.get(raw, "")        # e.g. "arc" → Arcadia Network
        quote_network  = _STABLE_QUOTES.get(raw, "")   # e.g. "USDC" → Arcadia Network (price-in-stablecoin mode)
        filter_token   = raw if (raw and raw in all_tokens and raw not in _NET_SHORT and raw not in _STABLE_QUOTES) else ""

        # Network name from a token symbol (e.g. --arb → Arcadia Network)
        if filter_token and not filter_network:
            tok_net = all_tokens.get(filter_token, {}).get("network", "")
            if tok_net:
                filter_network = tok_net

        # Build display groups
        by_network: dict[str, list] = {}
        for symbol, row in price_map.items():
            tcfg = all_tokens.get(symbol, {})
            network = tcfg.get("network") or "Other / PoW"
            if quote_network and network != quote_network:
                continue
            if filter_network and not quote_network and network != filter_network:
                continue
            # When filtering to a specific token, show its whole network for context
            by_network.setdefault(network, []).append((symbol, row, tcfg))

        footer_hint = "Filters: --arc, --dsc, --sun, --mta, --USDC, --DSD, or a token symbol (e.g. --vtr)."
        pages = []

        for network in sorted(by_network):
            net_stable = Config.NETWORK_STABLECOIN.get(network, "")
            title_quote = raw if raw and raw not in _NET_SHORT else "USD"
            _b = card(
                f"📈 {network}  -  {title_quote}",
                color=C_AMBER,
            )
            entries = sorted(by_network[network], key=lambda x: x[0])
            # If filtering to a specific token, only show that one
            if filter_token:
                entries = [(s, r, t) for s, r, t in entries if s == filter_token]

            for symbol, row, tcfg in entries:
                if tcfg.get("stablecoin") or tcfg.get("consensus") == "Fiat":
                    emoji = tcfg.get("emoji", "💵")
                    _b.field(f"{emoji} {symbol}", "**$1.00**   -   *pegged*", True)
                    continue

                pct_change = (
                    (float(row["price"]) - float(row["open_price"])) / float(row["open_price"]) * 100
                    if row["open_price"] > 0 else 0.0
                )
                sign = "▲" if pct_change >= 0 else "▼"
                emoji = tcfg.get("emoji", "●")

                if quote_network and net_stable:
                    # Price in stablecoin (pool-derived)
                    pool_id, ca, cb = ctx.db.make_pool_id(symbol, net_stable)
                    pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
                    if pool and pool["reserve_a"] > 0 and pool["reserve_b"] > 0:
                        pp = float(pool["reserve_b"]) / float(pool["reserve_a"]) if ca == symbol else float(pool["reserve_a"]) / float(pool["reserve_b"])
                        price_str = f"**{pp:,.6f} {net_stable}**"
                    else:
                        price_str = f"**{row['price']:,.6f} {net_stable}** *(oracle)*"
                    _b.field(
                        f"{emoji} {symbol}",
                        f"Price: {price_str}  {sign} {abs(pct_change):.2f}%",
                        True,
                    )
                else:
                    # USD mode with pool subtext
                    price_str = f"**${row['price']:,.6f}**"
                    pool_subtext = ""
                    if net_stable:
                        pool_id, ca, cb = ctx.db.make_pool_id(symbol, net_stable)
                        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
                        if pool and pool["reserve_a"] > 0 and pool["reserve_b"] > 0:
                            pp = float(pool["reserve_b"]) / float(pool["reserve_a"]) if ca == symbol else float(pool["reserve_a"]) / float(pool["reserve_b"])
                            pool_subtext = f"\nPool: `{pp:,.4f} {net_stable}`"
                    _b.field(
                        f"{emoji} {symbol}",
                        (
                            f"Price: {price_str}  {sign} {abs(pct_change):.2f}%\n"
                            f"High: `${row['day_high']:,.4f}`  Low: `${row['day_low']:,.4f}`"
                            f"{pool_subtext}"
                        ),
                        True,
                    )

            _b.footer(footer_hint)
            embed = _b.build()
            if embed.fields:
                pages.append(embed)

        if not pages:
            available = ", ".join(f"`{r['symbol']}`" for r in rows[:20])
            await ctx.reply_error(
                f"No price data for `{filter}`. "
                f"Available tokens: {available}"
            )
            return

        # Network dropdown -- mirrors cogs/trade.py::PoolNetworkSelect so the
        # prices view navigates the same way as .pool list. One page per
        # network already; the Select just lets you jump without pressing
        # next/previous. When only one network has prices we fall back to
        # the plain paginator for a cleaner UI.
        if len(pages) <= 1:
            await send_paginated(ctx, pages)
            return

        networks_in_order = sorted(by_network)
        _NET_EMOJIS = {
            "Sun Network":      "☀",
            "Moneta Chain":  "🔸",
            "Arcadia Network": "🔷",
            "Discoin Network":  "🪙",
            "Moon Network":     "\U0001F315",
            "Other / PoW":      "🌐",
        }
        page_by_network: dict[str, discord.Embed] = dict(zip(networks_in_order, pages))
        first_net = networks_in_order[0]

        class PriceNetworkSelect(discord.ui.Select):
            def __init__(self_inner) -> None:
                options = [
                    discord.SelectOption(
                        label=net,
                        value=net,
                        emoji=_NET_EMOJIS.get(net, "🌐"),
                        description=f"{len(by_network[net])} token(s)",
                        default=(net == first_net),
                    )
                    for net in networks_in_order
                ]
                super().__init__(
                    placeholder="Select a network…",
                    min_values=1, max_values=1,
                    options=options,
                )

            async def callback(self_inner, interaction: discord.Interaction) -> None:
                selected = self_inner.values[0]
                for opt in self_inner.options:
                    opt.default = (opt.value == selected)
                await interaction.response.edit_message(
                    embed=page_by_network[selected], view=view,
                )

        class PriceListView(discord.ui.View):
            def __init__(self_inner) -> None:
                super().__init__(timeout=120)
                self_inner.add_item(PriceNetworkSelect())

            async def on_timeout(self_inner) -> None:
                try:
                    for item in self_inner.children:
                        item.disabled = True
                    await msg.edit(view=self_inner)
                except Exception:
                    pass

        view = PriceListView()
        msg = await ctx.reply(embed=page_by_network[first_net], view=view, mention_author=False)

    # ── $buy ──────────────────────────────────────────────────────────────────

    @crypto_group.command(name="buy")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def buy(self, ctx: DiscoContext, arg1: str, arg2: str = "", *, flags: str = "") -> None:
        """Buy coins/stablecoins with USD (or SUN). Accepts 'SYM amount' or 'amount SYM'.
        Flags: yes to skip confirmation.  with SUN to pay with SUN instead of USD.
        Only network coins (ARC/DSC/MTA/SUN) and stablecoins (USDC/DSD) can be bought directly.
        For other tokens (e.g. VTR, DSY) use .swap."""
        # Flexible arg order
        if not arg2:
            await ctx.reply_error(f"Usage: `{ctx.prefix}crypto buy <SYMBOL> <amount>` or `{ctx.prefix}crypto buy <amount> <SYMBOL>`")
            return
        symbol, amount_str = _parse_sym_amt(arg1, arg2)
        fl = flags.lower()
        auto_confirm = "yes" in fl or "-y" in fl
        pay_with_sun = "with sun" in fl

        # USD is the base currency  -  not a buyable token
        if symbol == "USD":
            await ctx.reply_error(
                "USD is the base currency  -  you already have it in your wallet.\n"
                f"Use `{ctx.prefix}crypto buy USDC` or `{ctx.prefix}crypto buy DSD` to get network stablecoins."
            )
            return

        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        if symbol not in all_tokens:
            await ctx.reply_error(f"Unknown token `{symbol}`. Use `{ctx.prefix}crypto info {symbol}` or `{ctx.prefix}crypto` to see all tokens.")
            return

        # ── Admin halts ───────────────────────────────────────────────────────
        if await ctx.db.is_token_disabled(ctx.guild_id, symbol):
            await ctx.reply_error(f"**{symbol}** trading is currently disabled by an admin.")
            return
        tok_net = all_tokens.get(symbol, {}).get("network", "")
        net_key = normalize_network_short(tok_net)
        if net_key and await ctx.db.is_network_halted(ctx.guild_id, net_key):
            await ctx.reply_error(f"The **{tok_net}** is currently halted by an admin. Transactions are paused.")
            return

        # Restrict .buy to coins + stablecoins only
        if symbol not in Config.BUYABLE_WITH_USD:
            network_name = all_tokens.get(symbol, {}).get("network", "")
            stablecoin = Config.NETWORK_STABLECOIN.get(network_name, "stablecoin")
            await ctx.reply_error(
                f"**{symbol}** cannot be purchased directly with USD.\n"
                f"Use `{ctx.prefix}trade swap {stablecoin} {symbol} <amount>` instead.\n"
                f"Direct `{ctx.prefix}crypto buy` is available for: {', '.join(sorted(Config.BUYABLE_WITH_USD))}."
            )
            return

        # Validate SUN-as-payment rules
        _NETWORK_COINS = {"ARC", "DSC"}  # coins payable with SUN
        _STABLECOINS = set(Config.NETWORK_STABLECOIN.values())
        if pay_with_sun:
            if symbol == "SUN":
                await ctx.reply_error("You can't buy SUN with SUN. Use USD to buy SUN.")
                return
            if symbol in _STABLECOINS:
                await ctx.reply_error(
                    f"Stablecoins can't be purchased with SUN.\n"
                    f"Use `{ctx.prefix}crypto buy {symbol} <amount>` with USD instead."
                )
                return
            if symbol not in _NETWORK_COINS:
                await ctx.reply_error(
                    f"SUN can only be used to buy network coins: **ARC, DSC**.\n"
                    f"Use USD to buy **{symbol}**."
                )
                return

        # ── Per-symbol anti-sandwich cooldown ────────────────────────────────
        if (_cd := check_trade_cooldown(ctx.author.id, ctx.guild_id, symbol)) > 0:
            await ctx.reply_cooldown(_cd)
            return

        price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        if not price_row:
            await ctx.db.seed_prices(ctx.guild_id)
            price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        if not price_row:
            await ctx.reply_error("Price data unavailable.")
            return

        # Load SUN balance/rate for SUN-payment path
        if pay_with_sun:
            sun_row = await ctx.db.get_price("SUN", ctx.guild_id)
            sun_h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, "SUN")
            sun_bal = sun_h.h("amount") if sun_h else 0.0
            sun_usd_rate = float(sun_row["price"]) if sun_row else 0.0
        else:
            pass  # USD payment path continues below

        _fee_cfg = await ctx.db.guilds.get_fee_config(ctx.guild_id)
        _fee_est = 0.0
        _sun_fee_est = 0.0
        _buying_all = amount_str.lower() == "all"
        if _buying_all:
            if pay_with_sun:
                if sun_usd_rate <= 0:
                    await ctx.reply_error("SUN price unavailable. Cannot process SUN payment right now.")
                    return
                # Reserve SUN fee up-front so cost_sun + fee_sun == sun_bal exactly
                _sun_fee_est = sun_bal * _fee_cfg["platform_fee_pct"]
                _cost_sun_all = max(0.0, sun_bal - _sun_fee_est)
                usd_equiv = _cost_sun_all * sun_usd_rate
                qty = usd_equiv / float(price_row["price"]) if price_row["price"] > 0 else 0.0
            else:
                # Reserve the fee up-front so cost_usd + buy_fee == wallet exactly
                _wallet_h = ctx.user_row.h("wallet")
                _fee_est = max(_fee_cfg["platform_fee_min"],
                               min(_fee_cfg["platform_fee_max"],
                                   _wallet_h * _fee_cfg["platform_fee_pct"]))
                _cost_all = max(0.0, _wallet_h - _fee_est)
                qty = _cost_all / float(price_row["price"]) if price_row["price"] > 0 else 0.0
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount_str)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `all`, or `$<usd>` (e.g. `$100`).")
                return
            if _usd_mode:
                qty = _parsed / float(price_row["price"]) if price_row["price"] > 0 else 0.0
            else:
                qty = _parsed
        if not math.isfinite(qty) or qty <= 0:
            await ctx.reply_error("Amount must be a positive finite number.")
            return

        cost_usd = float(price_row["price"]) * qty
        # "all" path: clamp cost to exact balance to avoid float rounding errors
        if _buying_all and not pay_with_sun:
            cost_usd = min(cost_usd, max(0.0, ctx.user_row.h("wallet") - _fee_est))
            qty = cost_usd / float(price_row["price"]) if price_row["price"] > 0 else qty
        if pay_with_sun:
            cost_sun = cost_usd / sun_usd_rate if sun_usd_rate > 0 else float("inf")
            # SUN fee: flat PCT of cost_sun (no USD min/max  -  SUN is a game token)
            buy_fee_sun = cost_sun * _fee_cfg["platform_fee_pct"]
            buy_fee_sun_reserve = buy_fee_sun / 4.0
            total_sun_cost = cost_sun + buy_fee_sun
            if total_sun_cost > sun_bal:
                await ctx.reply_error(
                    f"That costs **{cost_sun:,.4f} SUN** + **{buy_fee_sun:,.4f} SUN** fee = **{total_sun_cost:,.4f} SUN** "
                    f"but you only have **{sun_bal:,.4f} SUN**."
                )
                return
            payment_str = f"{cost_sun:,.4f} SUN (≈ ${cost_usd:,.2f})"
        else:
            # USD fee: percentage-based, clamped to min/max
            buy_fee = max(_fee_cfg["platform_fee_min"],
                          min(_fee_cfg["platform_fee_max"], cost_usd * _fee_cfg["platform_fee_pct"]))
            buy_fee_reserve = buy_fee / 2.0
            if cost_usd + buy_fee > ctx.user_row.h("wallet"):
                await ctx.reply_error(
                    f"That costs **${cost_usd:,.4f}** + **${buy_fee:,.2f}** fee = **${cost_usd+buy_fee:,.2f}** "
                    f"but you only have **${ctx.user_row.h('wallet'):,.2f}**."
                )
                return
            payment_str = f"{fmt_usd(cost_usd)} USD"

        # Confirmation view
        if not auto_confirm:
            # Estimated slippage preview - matches the execute-path formula so
            # "receive" reflects what actually lands in the wallet.
            _spot_price_buy = float(price_row["price"])
            _circ_supply_est_buy = to_human(int(all_tokens.get(symbol, {}).get("circulating_supply") or 0))
            _est_impact_buy = estimate_cefi_impact(
                cost_usd, _spot_price_buy, _circ_supply_est_buy, is_sell=False,
            )
            _est_eff_price_buy = _spot_price_buy * (1 + _est_impact_buy)
            _est_qty_after_impact = (cost_usd / _est_eff_price_buy) if _est_eff_price_buy > 0 else qty
            _buy_banner, _buy_color_override = slippage_banner(_est_impact_buy)
            if pay_with_sun:
                _buy_usd_res = buy_fee_sun / 2 * sun_usd_rate if sun_usd_rate > 0 else 0.0
                fee_line = (
                    f"**Protocol fee:** {buy_fee_sun:,.6f} SUN ({_fee_cfg['platform_fee_pct']*100:.2g}% of {cost_sun:,.4f} SUN)"
                    f"\n↳ **${_buy_usd_res:,.4f}** → USD Vault"
                )
            else:
                fee_line = (
                    f"**Protocol fee:** ${buy_fee:,.2f} ({_fee_cfg['platform_fee_pct']*100:.2g}% of ${cost_usd:,.2f})"
                    f"\n↳ **${buy_fee/2:,.2f}** → USD Vault"
                )
            desc = (
                f"{_buy_banner}"
                f"Send **`{payment_str}`**\n"
                f"Receive ≈ **`{_est_qty_after_impact:,.6f} {symbol}`**\n"
                f"Spot: 1 {symbol} = `${_spot_price_buy:,.4f}`  →  est. fill `${_est_eff_price_buy:,.4f}`\n"
                f"📊 **Price impact:** `-{_est_impact_buy*100:.3f}%`\n"
                f"{fee_line}"
                f"\n\nExpires {fmt_ts(int(time.time() + 30))}  ·  Use `yes` to skip confirmation."
            )
            conf_embed = (
                card(
                    f"🛒 Confirm Buy  -  {Config.currency_label(symbol)}",
                    description=desc,
                    color=_buy_color_override if _buy_color_override is not None else C_AMBER,
                )
                .build()
            )
            view = ConfirmTradeView(ctx.author.id)
            conf_msg = await ctx.reply(embed=conf_embed, view=view, mention_author=False)
            await view.wait()
            if not view.confirmed:
                cancel_embed = card("", description="Purchase cancelled.", color=C_NEUTRAL).build()
                sanitize_embed(cancel_embed)
                await conf_msg.edit(embed=cancel_embed, view=None)
                return

        # ── Re-check balances after confirmation (prevents stale-state exploits) ─
        if pay_with_sun:
            sun_h_fresh = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, "SUN")
            sun_bal_fresh = sun_h_fresh.h("amount") if sun_h_fresh else 0.0
            if total_sun_cost > sun_bal_fresh:
                await ctx.reply_error(
                    f"Balance changed since confirmation. Need **{total_sun_cost:,.4f} SUN** "
                    f"but you now have **{sun_bal_fresh:,.4f} SUN**."
                )
                return
        else:
            fresh_user = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            _fresh_wallet_raw = int(fresh_user["wallet"]) if fresh_user else 0
            fresh_wallet = to_human(_fresh_wallet_raw)
            if _buying_all:
                # Re-resolve the "all" cost against the fresh balance so any small
                # between-confirmation drift (or fee clamp) doesn't spuriously
                # block a full-wallet buy.
                _fee_est = max(
                    _fee_cfg["platform_fee_min"],
                    min(_fee_cfg["platform_fee_max"],
                        fresh_wallet * _fee_cfg["platform_fee_pct"]),
                )
                cost_usd = max(0.0, fresh_wallet - _fee_est)
                buy_fee = _fee_est
                qty = cost_usd / float(price_row["price"]) if price_row["price"] > 0 else qty
                if cost_usd <= 0:
                    await ctx.reply_error("Your balance is too low to complete this purchase.")
                    return
            elif cost_usd + buy_fee > fresh_wallet:
                await ctx.reply_error(
                    f"Balance changed since confirmation. Need **${cost_usd + buy_fee:,.2f}** "
                    f"but you now have **${fresh_wallet:,.2f}**."
                )
                return

        # ── Depeg buy cap  -  throttle accumulation at distressed prices ───────
        _cur_price = float(price_row["price"])
        _ath = float(price_row.get("ath") or 0.0)
        _depeg_reservation_ts: float | None = None
        if is_depeg(_cur_price, _ath):
            _allowed, _remaining, _depeg_reservation_ts = await reserve_depeg_buy(
                ctx.author.id, ctx.guild_id, symbol, cost_usd
            )
            if not _allowed:
                await ctx.reply_error(
                    f"**{symbol}** is in depeg mode (price is below "
                    f"{Config.DEPEG_THRESHOLD*100:.0f}% of its all-time high).\n"
                    f"Daily buy limit: **${Config.DEPEG_DAILY_BUY_USD:,.0f}**  -  "
                    f"remaining today: **${_remaining:,.2f}**."
                )
                return

        # ── Execute trade atomically ─────────────────────────────────────────
        _old_price_buy = float(price_row["price"])
        impact = cost_usd / Config.PRICE_IMPACT_DIVISOR
        tok_meta_buy = all_tokens.get(symbol, {})
        circ_supply_buy = to_human(int(tok_meta_buy.get("circulating_supply") or 0))
        mkt_cap_buy = _old_price_buy * circ_supply_buy
        if mkt_cap_buy > 0 and cost_usd > 0.001 * mkt_cap_buy:
            mc_ratio = cost_usd / mkt_cap_buy
            mc_multiplier = min(1.0 + mc_ratio * 2.0, 5.0)  # cap at 5x to prevent runaway pumps
            impact = impact * mc_multiplier
        _eff_price_buy = max(1e-15, _old_price_buy * (1 + impact))
        qty = cost_usd / _eff_price_buy

        try:
            async with ctx.db.atomic():
                if pay_with_sun:
                    await ctx.db.update_holding(ctx.author.id, ctx.guild_id, "SUN", to_raw(-(cost_sun + buy_fee_sun)))
                    await ctx.db.split_to_community_reserves(ctx.guild_id, "SUN", to_raw(buy_fee_sun), sun_usd_rate)
                else:
                    _wallet_delta = to_raw(-(cost_usd + buy_fee))
                    if _buying_all:
                        _wallet_delta = -min(-_wallet_delta, _fresh_wallet_raw)
                    await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, _wallet_delta)
                    await ctx.db.split_to_community_reserves(ctx.guild_id, "USD", to_raw(buy_fee))
                new_holding = await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, to_raw(qty))
                await ctx.db.update_price(symbol, ctx.guild_id, _eff_price_buy)
                await ctx.db.upsert_candle(
                    ctx.guild_id, f"{symbol}USD", int(time.time()) // 60 * 60,
                    open_=_old_price_buy,
                    high=max(_old_price_buy, _eff_price_buy),
                    low=min(_old_price_buy, _eff_price_buy),
                    close=_eff_price_buy,
                    volume_delta=cost_usd,
                )
                tx_hash = await ctx.db.log_tx(
                    ctx.guild_id, ctx.author.id, "BUY",
                    symbol_in="SUN" if pay_with_sun else "USD",
                    amount_in=to_raw(cost_sun) if pay_with_sun else to_raw(cost_usd),
                    symbol_out=symbol, amount_out=to_raw(qty),
                    price_at=_eff_price_buy,
                    network="sun" if pay_with_sun else "usd",
                )
        except Exception:
            if _depeg_reservation_ts is not None:
                cancel_depeg_reservation(ctx.author.id, ctx.guild_id, symbol, _depeg_reservation_ts)
            raise
        set_trade_cooldown(ctx.author.id, ctx.guild_id, symbol)
        await ctx.bot.bus.publish("prices_updated", guild=ctx.guild)
        await ctx.bot.bus.publish(
            "trade", guild=ctx.guild, user=ctx.author,
            action="BUY", symbol=symbol, amount=qty,
            price=_eff_price_buy, total=cost_usd, tx_hash=tx_hash,
        )
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "buy", cost_usd, symbol=symbol, amount=qty)

        _b = (
            card(f"🟢 Bought {Config.currency_label(symbol)}", color=C_SUCCESS)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("🪙 Amount", f"**{qty:,.6f} {symbol}**",                 True)
            .field("💵 Paid",   payment_str,                                  True)
            .field("📈 Fill Price",  f"${_eff_price_buy:,.4f}\n📊 Slippage: `+{impact*100:.3f}%`", True)
        )
        if pay_with_sun:
            _sun_fee_usd_res = buy_fee_sun / 2.0 * sun_usd_rate if sun_usd_rate > 0 else 0.0
            _b.field("🏦 Fee",
                f"`{buy_fee_sun:,.6f} SUN`\n↳ ${_sun_fee_usd_res:,.4f} → Vault",
                True)
        else:
            _buy_fee_usd_res = buy_fee / 2.0
            _b.field("🏦 Fee",
                f"`${buy_fee:,.2f}`\n↳ ${_buy_fee_usd_res:,.2f} → Vault",
                True)
        _b.field("💰 Holding",  f"**{to_human(int(new_holding)):,.6f} {symbol}**", True)
        result_embed = _b.build()
        set_tx(result_embed, ctx.guild.id, tx_hash)
        if auto_confirm:
            await ctx.reply(embed=result_embed, mention_author=False)
        else:
            sanitize_embed(result_embed)
            await conf_msg.edit(embed=result_embed, view=None)  # type: ignore[reportUnboundVariable]

        # ── Auto-create wallet on first buy for a network ──────────────────
        if net_key and tok_net:
            try:
                has_wallet = await ctx.db.has_defi_wallet(ctx.author.id, ctx.guild_id, net_key)
                if not has_wallet:
                    address = await ctx.db.create_wallet_address(
                        ctx.author.id, ctx.guild_id,
                        label=None, is_temp=False,
                        network=tok_net, address_prefix=net_key,
                    )
                    await ctx.db.log_tx(
                        ctx.guild_id, ctx.author.id, "WALLET_CREATE",
                        symbol_in="WALLET", amount_in=0,
                        network=net_key,
                    )
                    wallet_embed = (
                        card(f"🆕 {tok_net} Wallet Created", color=C_INFO)
                        .description(
                            f"Since this is your first **{symbol}** purchase, "
                            f"a **{tok_net}** wallet was automatically created for you!\n"
                            f"Address: `{address}`"
                        )
                        .footer("View all your wallets with the button below")
                        .build()
                    )
                    view = ActionSuggestionView(ctx, "📋 View My Wallets", "wallet list")
                    await ctx.send(embed=wallet_embed, view=view)
            except Exception:
                pass  # wallet creation is best-effort; don't block the buy

    # ── $sell ─────────────────────────────────────────────────────────────────

    @crypto_group.command(name="sell")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def sell(self, ctx: DiscoContext, arg1: str, arg2: str = "", *, flags: str = "") -> None:
        """Sell coins/stablecoins for USD. Accepts 'SYM amount' or 'amount SYM'.
        Use '.sell everything' to sell all sellable holdings.
        Flags: yes to skip confirmation.
        Only network coins (ARC/DSC/MTA/SUN) and stablecoins (USDC/DSD) can be sold for USD.
        For other tokens, use .swap TOKEN stablecoin all."""
        if arg1.lower() == "everything" and not arg2:
            trade_cog = self.bot.get_cog("Trade")
            if trade_cog:
                await trade_cog._sell_everything(ctx, flags=flags)
            return
        if not arg2:
            await ctx.reply_error(f"Usage: `{ctx.prefix}crypto sell <SYMBOL> <amount>` or `{ctx.prefix}crypto sell <amount> <SYMBOL>`")
            return
        symbol, amount_str = _parse_sym_amt(arg1, arg2)
        fl = flags.lower()
        auto_confirm = "yes" in fl or "-y" in fl

        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        if symbol not in all_tokens:
            await ctx.reply_error(f"Unknown token `{symbol}`.")
            return

        # ── Admin halts ───────────────────────────────────────────────────────
        if await ctx.db.is_token_disabled(ctx.guild_id, symbol):
            await ctx.reply_error(f"**{symbol}** trading is currently disabled by an admin.")
            return
        _tok_net = all_tokens.get(symbol, {}).get("network", "")
        _net_key2 = normalize_network_short(_tok_net)
        if _net_key2 and await ctx.db.is_network_halted(ctx.guild_id, _net_key2):
            await ctx.reply_error(f"The **{_tok_net}** is currently halted by an admin. Transactions are paused.")
            return

        # Same restriction as .buy  -  only coins and stablecoins can be sold for USD
        if symbol not in Config.BUYABLE_WITH_USD:
            network_name = all_tokens.get(symbol, {}).get("network", "")
            stablecoin = Config.NETWORK_STABLECOIN.get(network_name, "stablecoin")
            await ctx.reply_error(
                f"**{symbol}** cannot be sold directly for USD.\n"
                f"Use `{ctx.prefix}trade swap {symbol} {stablecoin} all` first, then `{ctx.prefix}trade sell {stablecoin} all`.\n"
                f"Direct `{ctx.prefix}crypto sell` is available for: {', '.join(sorted(Config.BUYABLE_WITH_USD))}."
            )
            return

        # ── Per-symbol anti-sandwich cooldown ────────────────────────────────
        if (_cd_sell := check_trade_cooldown(ctx.author.id, ctx.guild_id, symbol)) > 0:
            await ctx.reply_cooldown(_cd_sell)
            return

        holding = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, symbol)
        available_raw = int(holding["amount"]) if holding else 0
        available = to_human(available_raw)

        price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        if not price_row:
            await ctx.db.seed_prices(ctx.guild_id)
            price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        if not price_row:
            await ctx.reply_error("Price data unavailable.")
            return

        _selling_all = amount_str.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _selling_all:
            amt_raw = available_raw
            amt = available
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount_str)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `all`, or `$<usd>` (e.g. `$100`).")
                return
            if _usd_mode:
                amt = _parsed / float(price_row["price"]) if price_row["price"] > 0 else 0.0
            else:
                amt = _parsed
            if not math.isfinite(amt):
                await ctx.reply_error("Amount must be a finite number.")
                return
            amt_raw = to_raw(amt)

        if amt_raw <= 0 or available_raw == 0:
            await ctx.reply_error(f"You have no **{symbol}** to sell.")
            return
        if amt_raw > available_raw:
            await ctx.reply_error(f"You only have **{available:.4f} {symbol}**.")
            return

        _fee_cfg = await ctx.db.guilds.get_fee_config(ctx.guild_id)
        revenue = float(price_row["price"]) * amt
        sell_fee = max(_fee_cfg["platform_fee_min"],
                       min(_fee_cfg["platform_fee_max"], revenue * _fee_cfg["platform_fee_pct"]))
        if sell_fee >= revenue:
            await ctx.reply_error(
                f"Trade too small  -  the minimum fee (${_fee_cfg['platform_fee_min']:,.2f}) "
                f"exceeds your gross revenue (${revenue:,.4f}). "
                f"Sell a larger amount."
            )
            return
        sell_fee_reserve = sell_fee / 2.0
        net_revenue = revenue - sell_fee

        # Confirmation view
        if not auto_confirm:
            # Estimated slippage preview - mirrors the execute-path formula so
            # "receive" matches what actually lands in the wallet.
            _spot_price_sell = float(price_row["price"])
            _circ_supply_est_sell = to_human(int(all_tokens.get(symbol, {}).get("circulating_supply") or 0))
            _est_impact_sell = estimate_cefi_impact(
                revenue, _spot_price_sell, _circ_supply_est_sell, is_sell=True,
            )
            _est_eff_price_sell = max(0.0, _spot_price_sell * (1 - _est_impact_sell))
            _est_eff_revenue = amt * _est_eff_price_sell
            _est_net_after_impact = max(0.0, _est_eff_revenue - sell_fee)
            _sell_banner, _sell_color_override = slippage_banner(_est_impact_sell)
            desc = (
                f"{_sell_banner}"
                f"Send **`{amt:,.6f} {symbol}`**\n"
                f"Receive ≈ **`${_est_net_after_impact:,.2f} USD`**  *(after impact + fee)*\n"
                f"Spot: 1 {symbol} = `${_spot_price_sell:,.4f}`  →  est. fill `${_est_eff_price_sell:,.4f}`\n"
                f"📊 **Price impact:** `-{_est_impact_sell*100:.3f}%`  "
                f"(gross at spot would be `${revenue:,.2f}`)\n"
                f"**Protocol fee:** ${sell_fee:,.2f} ({_fee_cfg['platform_fee_pct']*100:.2g}%)\n"
                f"↳ **${sell_fee/2:,.2f}** → USD Vault"
                f"\n\nExpires {fmt_ts(int(time.time() + 30))}  ·  Use `yes` to skip confirmation."
            )
            conf_embed = (
                card(
                    f"🛒 Confirm Sell  -  {Config.currency_label(symbol)}",
                    description=desc,
                    color=_sell_color_override if _sell_color_override is not None else C_AMBER,
                )
                .build()
            )
            view = ConfirmTradeView(ctx.author.id)
            conf_msg = await ctx.reply(embed=conf_embed, view=view, mention_author=False)
            await view.wait()
            if not view.confirmed:
                await conf_msg.edit(
                    embed=card("", description="Sale cancelled.", color=C_NEUTRAL).build(),
                    view=None,
                )
                return

        # ── Re-check holding after confirmation ─────────────────────────────
        fresh_holding = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, symbol)
        fresh_raw = int(fresh_holding["amount"]) if fresh_holding else 0
        fresh_available = to_human(fresh_raw)
        if _selling_all:
            # Re-snap to fresh raw balance so any interest/airdrop that landed
            # between confirmation and execution doesn't leave dust.
            amt_raw = fresh_raw
            amt = fresh_available
        elif amt_raw > fresh_raw:
            await ctx.reply_error(
                f"Balance changed since confirmation. You now have **{fresh_available:,.6f} {symbol}** "
                f"but tried to sell **{amt:,.6f}**."
            )
            return

        # ── Execute trade atomically ─────────────────────────────────────────
        impact = revenue / Config.PRICE_IMPACT_DIVISOR
        _sell_cur_price = float(price_row["price"])
        circ_supply_sell = to_human(int(all_tokens.get(symbol, {}).get("circulating_supply") or 0))
        mkt_cap_sell = _sell_cur_price * circ_supply_sell
        if mkt_cap_sell > 0 and revenue > 0.001 * mkt_cap_sell:
            mc_ratio_sell = revenue / mkt_cap_sell
            mc_mult_sell = min(1.0 + mc_ratio_sell * 2.0, 5.0)
            impact = impact * mc_mult_sell
        impact = min(impact, 0.95)  # never wipe more than 95% of price in one sell
        _eff_price_sell = max(1e-9, _sell_cur_price * (1 - impact))
        eff_revenue = amt * _eff_price_sell
        net_revenue = eff_revenue - sell_fee

        async with ctx.db.atomic():
            # Use amt_raw so "all" empties the holding exactly (no dust left
            # behind from float round-trip).
            await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, -amt_raw)
            new_wallet_raw = await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(net_revenue))
            await ctx.db.split_to_community_reserves(ctx.guild_id, "USD", to_raw(sell_fee))
            await ctx.db.update_price(symbol, ctx.guild_id, _eff_price_sell)
            await ctx.db.upsert_candle(
                ctx.guild_id, f"{symbol}USD", int(time.time()) // 60 * 60,
                open_=_sell_cur_price,
                high=max(_sell_cur_price, _eff_price_sell),
                low=min(_sell_cur_price, _eff_price_sell),
                close=_eff_price_sell,
                volume_delta=eff_revenue,
            )
            tx_hash = await ctx.db.log_tx(
                ctx.guild_id, ctx.author.id, "SELL",
                symbol_in=symbol, amount_in=amt_raw,
                symbol_out="USD", amount_out=to_raw(eff_revenue),
                price_at=_eff_price_sell,
                network="usd",
            )
        set_trade_cooldown(ctx.author.id, ctx.guild_id, symbol)
        await ctx.bot.bus.publish("prices_updated", guild=ctx.guild)
        await ctx.bot.bus.publish(
            "trade", guild=ctx.guild, user=ctx.author,
            action="SELL", symbol=symbol, amount=amt,
            price=_eff_price_sell, total=eff_revenue, tx_hash=tx_hash,
        )
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "sell", eff_revenue, symbol=symbol, amount=amt)

        _sell_usd_res = sell_fee / 2.0
        result_embed = (
            card(f"🔴 Sold {Config.currency_label(symbol)}", color=C_WARNING)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("🪙 Sold",       f"**{amt:.4f} {symbol}**",                    True)
            .field("💵 Received",   f"**${net_revenue:,.4f} USD**",               True)
            .field("📈 Fill Price", f"${_eff_price_sell:.4f}\n📊 Slippage: `-{impact*100:.3f}%`", True)
            .field("🏦 Fee",
                f"`${sell_fee:,.2f}`\n↳ ${_sell_usd_res:,.2f} → Vault",
                True)
            .field("💰 Wallet",     f"**${to_human(int(new_wallet_raw)):,.2f}**", True)
            .build()
        )
        set_tx(result_embed, ctx.guild.id, tx_hash, f"slippage: -{impact*100:.3f}%")
        if auto_confirm:
            await ctx.reply(embed=result_embed, mention_author=False)
        else:
            sanitize_embed(result_embed)
            await conf_msg.edit(embed=result_embed, view=None)  # type: ignore[reportUnboundVariable]

    # ── $portfolio ────────────────────────────────────────────────────────────

    @crypto_group.command(name="portfolio", aliases=["port", "holdings"])
    @guild_only
    @no_bots
    @ensure_registered
    async def portfolio(self, ctx: DiscoContext) -> None:
        """Show your crypto holdings and current value."""
        holdings = await ctx.db.get_holdings(ctx.author.id, ctx.guild_id)
        if not holdings:
            await ctx.reply_error_action(
                f"You have no crypto holdings. Use `{ctx.prefix}crypto buy SYMBOL amount` to get started.",
                "Buy Crypto",
                "buy",
            )
            return

        _b = (
            card("💼 Portfolio", color=C_INFO)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        )

        total_value = 0.0
        for h in holdings:
            symbol = h["symbol"]
            if symbol in _DIRECT_BUY_TOKENS:
                price_row = await ctx.db.get_price(symbol, ctx.guild_id)
                usd_price = float(price_row["price"]) if price_row else 0.0
                price_note = f"${usd_price:.4f}"
            else:
                usd_price = await self._derive_usd_price(symbol, ctx.guild_id) or 0.0
                if usd_price:
                    price_note = f"${usd_price:.4f} (pool)"
                else:
                    price_row = await ctx.db.get_price(symbol, ctx.guild_id)
                    usd_price = float(price_row["price"]) if price_row else 0.0
                    price_note = f"${usd_price:.4f} (oracle)"
            amount_h = h.h("amount")
            value = usd_price * amount_h
            total_value += value
            _b.field(
                Config.currency_label(symbol, detail=True),
                (
                    f"Amount: **{amount_h:.4f}**\n"
                    f"Price: {price_note}\n"
                    f"Value: **${value:,.4f}**"
                ),
                True,
            )

        _b.field("Total Value", f"**${total_value:,.4f}**", False)
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── .tokeninfo ────────────────────────────────────────────────────────

    @crypto_group.command(name="info", aliases=["ti", "token", "tokeninfo"])
    @guild_only
    async def tokeninfo(self, ctx: DiscoContext, symbol: str) -> None:
        """Show detailed info for a token: price, contract rules, LP liquidity.
        Usage: .tokeninfo ARC"""
        symbol = symbol.upper()

        # Resolve token config (built-in or custom)
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        tcfg = all_tokens.get(symbol) or Config.TOKENS.get(symbol)
        if symbol == "USD":
            tcfg = Config.USD_META

        price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        if not price_row and not tcfg:
            await ctx.reply_error(f"Token **{symbol}** not found. Use `{ctx.prefix}crypto` to see all tokens.")
            return

        # Price data
        price     = float(price_row["price"])      if price_row else (tcfg.get("start_price", 1.0) if tcfg else 0.0)
        open_p    = float(price_row["open_price"]) if price_row else price
        day_high  = float(price_row["day_high"])   if price_row else price
        day_low   = float(price_row["day_low"])    if price_row else price
        pct_chg   = (price - open_p) / open_p * 100 if open_p > 0 else 0.0
        arrow     = "▲" if pct_chg >= 0 else "▼"

        # Token metadata
        name      = (tcfg.get("name") if tcfg else None) or symbol
        emoji     = (tcfg.get("emoji") if tcfg else None) or "●"
        consensus = (tcfg.get("consensus") if tcfg else None) or " - "
        network   = (tcfg.get("network") if tcfg else None) or " - "
        start_p   = (tcfg.get("start_price") if tcfg else None) or 0.0
        daily_vol = (tcfg.get("daily_vol") if tcfg else None) or 0.0

        # Contract info
        contract  = await ctx.db.get_token_contract(ctx.guild_id, symbol)
        if isinstance(contract, dict) and "params" in contract:
            import json
            params = json.loads(contract["params"]) if isinstance(contract["params"], str) else contract.get("params", {})
        else:
            params = contract if isinstance(contract, dict) else {}
        fee_rate   = params.get("transfer_fee", 0.0)
        burn_rate  = params.get("burn_rate", 0.0)
        max_supply = params.get("max_supply", 0.0)

        # LP liquidity  -  sum all pools containing this token
        all_pools = await ctx.db.get_all_pools(ctx.guild_id)
        lp_usd = 0.0
        pool_pairs: list[str] = []
        for pool in all_pools:
            ta, tb = pool["token_a"], pool["token_b"]
            if symbol not in (ta, tb):
                continue
            # Value the pool in USD via the oracle
            pa = await ctx.db.get_price(ta, ctx.guild_id)
            pb = await ctx.db.get_price(tb, ctx.guild_id)
            pa_usd = float(pa["price"]) if pa else 1.0
            pb_usd = float(pb["price"]) if pb else 1.0
            pool_val = pool.h("reserve_a") * pa_usd + pool.h("reserve_b") * pb_usd
            lp_usd += pool_val
            pair_str = f"{ta}/{tb}"
            if pair_str not in pool_pairs:
                pool_pairs.append(pair_str)

        # Build embed
        color = C_SUCCESS if pct_chg >= 0 else C_ERROR
        _b = (
            card(f"{emoji} {name}  ({symbol})", color=color)
            .field("Network",   network,   True)
            .field("Consensus", consensus, True)
            .field("Price",     f"**${price:,.6f}**  {arrow} {pct_chg:+.2f}%", False)
            .field("24h High",  f"${day_high:,.6f}", True)
            .field("24h Low",   f"${day_low:,.6f}",  True)
        )
        if start_p:
            _b.field("Start Price", f"${start_p:,.6f}", True)
        if daily_vol:
            _b.field("Daily Vol",   f"{daily_vol*100:.1f}%", True)

        # Market cap
        token_row = all_tokens.get(symbol, {})
        circ_supply = token_row.get("circulating_supply") or 0.0
        max_sup_tok = token_row.get("max_supply") or max_supply or 0.0
        if circ_supply > 0:
            mkt_cap = price * circ_supply
            supply_pct = f" ({circ_supply / max_sup_tok * 100:.1f}% of max)" if max_sup_tok > 0 else ""
            _b.field("Market Cap",
                f"**${mkt_cap:,.2f}**",
                True)
            _b.field("Circulating Supply",
                f"{circ_supply:,.0f} {symbol}{supply_pct}",
                True)
            if max_sup_tok > 0:
                _b.field("Max Supply", f"{max_sup_tok:,.0f}", True)

        # Contract section
        if fee_rate or burn_rate or max_supply:
            contract_lines = []
            if fee_rate:
                contract_lines.append(f"Transfer fee: **{fee_rate*100:.2f}%**")
            if burn_rate:
                contract_lines.append(f"Burn rate: **{burn_rate*100:.2f}%**")
            if max_supply:
                contract_lines.append(f"Max supply: **{max_supply:,.0f}**")
            _b.field("⚙ Contract Rules", "\n".join(contract_lines), False)
        else:
            _b.field("⚙ Contract", "No rules set  (uncapped, no fees)", False)

        # LP liquidity
        if lp_usd > 0:
            pairs_str = ", ".join(pool_pairs[:4])
            _b.field("LP Liquidity", f"**${lp_usd:,.2f}**  ({pairs_str})", False)
        else:
            _b.field("LP Liquidity", "Not pooled", False)

        _b.footer(f"{ctx.prefix}crypto for market overview  •  {ctx.prefix}admin contract to set rules")
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)


    # ── Backward-compat prefix stubs for .buy and .sell ──────────────────────

    @commands.command(name="buy", hidden=True)
    async def _buy_compat(self, ctx: DiscoContext, arg1: str = "", arg2: str = "", *, flags: str = "") -> None:
        await ctx.invoke(self.buy, arg1=arg1, arg2=arg2, flags=flags)

    @commands.command(name="sell", hidden=True)
    async def _sell_compat(self, ctx: DiscoContext, arg1: str = "", arg2: str = "", *, flags: str = "") -> None:
        await ctx.invoke(self.sell, arg1=arg1, arg2=arg2, flags=flags)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Crypto(bot))
