"""
cogs/bank.py  -  CeFi Banking: deposit, withdraw, transfer, move, savings, loans.

Top-level group: /bank
Subgroups:
  /bank savings   -  USD savings account (earn interest)
  /bank loan      -  Borrow against collateral

Direct subcommands:
  /bank deposit <amount>             -  USD wallet → bank
  /bank withdraw <amount>            -  USD bank → wallet
  /bank transfer <user> <amount>     -  send USD to another user
  /bank move <amount> <token> <from> <to>  -  universal storage mover
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import math
import time

import discord
from discord.ext import commands, tasks

from core.config import Config
from services.rate_model import compute_rates, utilization_str
from cogs.shop import (
    _item_stat,
    _vaultstone_stat,
    _liqstone_stat,
    notify_item_levelup_ready,
    cap_xp,
    _stone_price_map,
    _stone_staked_usd,
)
from core.framework.utils import guild_currency_name, parse_amount
from core.framework.network import (
    FULL_TO_SHORT as _FULL_TO_SHORT,
    SHORT_TO_FULL as _NETWORK_SHORTS,
    STABLE_NETWORK as _STABLE_NETWORK,
    normalize_full as normalize_network_full,
    stable_emoji as _stable_emoji,
)
from core.framework.scale import to_human, to_raw
from core.framework.tx import set_tx
from core.framework.heartbeat import pulse, register_interval
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework.cooldowns import user_cooldown
from core.framework.utils import parse_sym_amt
from core.framework import whale as _whale
import re

from core.framework.ui import C_AMBER, C_CRIMSON, C_ERROR, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_PURPLE, C_SUBTLE, C_SUCCESS, C_TEAL, C_WARNING, CategoryPaginator, ConfirmView, FormatKit, ValidatorSelectView, fmt_bonus, fmt_pct, fmt_token, fmt_ts, fmt_usd, mention
from core.framework.embed import card
from core.framework.ui import send_paginated
from core.framework.fuzzy import suggest_subcommand
from constants.validators import MAX_SLASH_COUNT, NET_SHORT as _V_NET_SHORT
from cogs.validators import gas_fee_for_network

log = logging.getLogger(__name__)

_NET_NATIVE: dict[str, str] = {
    "sun": "SUN",
    "mta": "MTA",
    "arc": "ARC",
    "dsc": "DSC",
    "lur": "REEL",   # Lure Network coin (earn-only -- see Config.EARN_ONLY_TOKENS)
    "cry": "RUNE",   # Crypt Network coin (earn-only -- see Config.EARN_ONLY_TOKENS)
    "bud": "BUD",    # Buddy Network coin (earn-only -- see Config.EARN_ONLY_TOKENS)
    "har": "HRV",    # Harvest Network coin (earn-only -- see Config.EARN_ONLY_TOKENS)
    "gam": "GBC",    # Gamba Network coin (earn-only -- see Config.EARN_ONLY_TOKENS)
}


def _fmt_amt(amt: float) -> str:
    """Magnitude-aware token amount formatter for compact wallet rows.

    Renders with comma separators and trims trailing decimals so a 6-figure
    holding like 757041.5106 reads as ``757,041.51`` while a small one like
    0.000123 still shows enough precision.
    """
    a = abs(amt)
    if a >= 1000:
        return f"{amt:,.2f}"
    if a >= 1:
        return f"{amt:,.4f}"
    if a > 0:
        return f"{amt:.6f}"
    return "0"


# Skip wallet rows worth less than this in USD when rendering balance pages.
# Below 1 cent the row is just visual noise -- the position is still reflected
# in the network total + summary aggregates.
_DUST_USD = 0.005

# Discord embed description limit is 4096 chars. Reserve 96 for the truncation
# tail so we never round-trip past the API's hard cap on whales with long
# token catalogs.
_DESC_LIMIT = 4000


def _truncate_desc(text: str) -> str:
    """Hard-cap a description block at the embed-safe limit with a tail hint."""
    if len(text) <= _DESC_LIMIT:
        return text
    return text[:_DESC_LIMIT].rstrip() + "\n-# (truncated -- view by network)"

_M = Config.SAVINGS_RATE_MODEL

# ── Transaction spinner ───────────────────────────────────────────────────────
_TX_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

async def _tx_animate(msg: discord.Message, label: str) -> None:
    """Spin braille frames on msg until cancelled."""
    for frame in _TX_SPIN:
        try:
            await msg.edit(
                embed=card(f"{frame} {label}").color(C_AMBER).build()
            )
        except Exception:
            return
        await asyncio.sleep(0.38)
_L = Config.LENDING
_ALLOWED_BORROW: frozenset[str] = frozenset(Config.NETWORK_STAKE_TOKEN.values()) | {"USD"}
_ALLOWED_BORROW_STR = " · ".join(sorted(_ALLOWED_BORROW))

BADGE_EMOJIS = {
    "first_trade": "\U0001f4b1",     # currency exchange
    "diamond_hands": "\U0001f48e",   # gem
    "whale": "\U0001f40b",           # whale
    "miner": "\u26cf\ufe0f",         # pick
    "validator": "\U0001f3d7\ufe0f", # building construction
    "gambler": "\U0001f3b0",         # slot machine
    "stone_master": "\U0001f48e",    # gem
    "group_founder": "\U0001f465",   # busts in silhouette
    "generous": "\U0001f381",        # wrapped gift
    "survivor": "\U0001f525",        # fire
}


class Bank(commands.Cog):
    """CeFi banking: deposit, withdraw, transfer, move, savings, loans."""

    @commands.hybrid_group(name="bank", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def bank(self, ctx: DiscoContext) -> None:
        """CeFi banking commands."""
        if await suggest_subcommand(ctx, self.bank):
            return
        p = ctx.prefix or Config.PREFIX
        await ctx.reply(
            embed=(
                card("🏦 Discoin Bank")
                .description(
                    "Your all-in-one CeFi banking hub. Move, grow, and borrow against your assets."
                )
                .color(C_INFO)
                .field(
                    "💳 Wallet & Bank",
                    f"`{p}bank deposit <amount>`  -  wallet → bank\n"
                    f"`{p}bank withdraw <amount>`  -  bank → wallet\n"
                    f"`{p}bank transfer <user> <amount>`  -  send USD",
                    True,
                )
                .field(
                    "🔀 Asset Movement",
                    f"`{p}bank move <amt> <token> <from> <to>`\n"
                    "Locations: `cash` · `bank` · `wallet` · `vault`",
                    True,
                )
                .field(
                    "💰 Savings Vault",
                    f"`{p}bank savings`  -  view balances & rates\n"
                    f"`{p}bank savings deposit`  -  earn interest on USD\n"
                    f"`{p}bank savings rates`  -  full rate curve",
                    True,
                )
                .field(
                    "💼 Loans & Borrowing",
                    f"`{p}bank loan borrow <amt>`  -  borrow USD\n"
                    f"`{p}bank loan repay`  -  repay USD loan\n"
                    f"`{p}bank loan status`  -  view active loan",
                    True,
                )
                .footer("Savings earn dynamic APY  •  Loans accrue interest hourly")
                .build()
            ),
            mention_author=False,
        )

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._loan_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.savings_interest_tick.start()
        self.loan_interest_tick.start()
        register_interval("savings_interest", 1800)
        register_interval("loan_interest", 1800)

    def cog_unload(self) -> None:
        self.savings_interest_tick.cancel()
        self.loan_interest_tick.cancel()

    # ── Module check ────────────────────────────────────────────────────────
    async def cog_check(self, ctx) -> bool:
        return await module_cog_check(self.bot, ctx, "economy")

    # ════════════════════════════════════════════════════════════════════════
    #  DEPOSIT / WITHDRAW (USD wallet ↔ bank)
    # ════════════════════════════════════════════════════════════════════════

    @bank.command(name="deposit", aliases=["dep"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def deposit(self, ctx: DiscoContext, amount: str) -> None:
        """Deposit USD from wallet to bank. Usage: /bank deposit <amount>"""
        await self._usd_deposit(ctx, amount)

    async def _usd_deposit(self, ctx: DiscoContext, amount_str: str) -> None:
        row = ctx.user_row
        if amount_str.lower() == "all":
            amt = row["wallet"]  # raw int
        else:
            try:
                amt = to_raw(parse_amount(amount_str)[0])
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
                return
        if amt <= 0:
            await ctx.reply_error("Amount must be positive.")
            return
        if amt > row["wallet"]:
            await ctx.reply_error(f"You only have **{row.h('wallet'):,.2f}** in your wallet.")
            return
        old_bank = row["bank"]
        amt_h = to_human(amt)
        old_bank_h = to_human(old_bank)
        # Confirmation
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(
            embed=(
                card("🏦 Confirm Deposit", color=C_AMBER)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📥 Amount",       f"**${amt_h:,.2f}**",                                           True)
                .field("💳 Wallet After", f"**${row.h('wallet') - amt_h:,.2f}**",                         True)
                .field("🏦 Bank After",   f"**${old_bank_h + amt_h:,.2f}**",                             True)
                .field("", f"Expires {fmt_ts(int(time.time() + 30))}", False)
                .build()
            ),
            view=view,
            mention_author=False,
        )
        confirmed = await view.wait_result()
        if not confirmed:
            await conf_msg.edit(embed=card("", description="Deposit cancelled.").color(C_SUBTLE).build(), view=None)
            return
        anim_task = asyncio.create_task(_tx_animate(conf_msg, "Depositing..."))
        try:
            _, new_bank = await ctx.db.deposit_to_bank(ctx.author.id, ctx.guild_id, amt)
        finally:
            anim_task.cancel()
        new_bank_h = to_human(new_bank)
        await conf_msg.edit(
            embed=(
                card("🏦 Deposit Confirmed")
                .color(C_SUCCESS)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📥 Deposited",    f"**${amt_h:,.2f}**",       True)
                .field("🏦 Bank",         f"**${new_bank_h:,.2f}**",  True)
                .field("📊 Change",       f"{fmt_usd(old_bank_h)} -> {fmt_usd(new_bank_h)}", True)
                .footer("Funds are safe in your bank  •  /bank withdraw to access them")
                .timestamp()
                .build()
            ),
            view=None,
        )
        await ctx.bot.bus.publish("deposit", guild=ctx.guild, user=ctx.author, amount=amt_h)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "deposit", amt_h, symbol="USD", amount=amt_h)

    @bank.command(name="withdraw", aliases=["with"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def withdraw(self, ctx: DiscoContext, amount: str) -> None:
        """Withdraw USD from bank to wallet. Usage: /bank withdraw <amount>"""
        await self._usd_withdraw(ctx, amount)

    async def _usd_withdraw(self, ctx: DiscoContext, amount_str: str) -> None:
        row = ctx.user_row
        if amount_str.lower() == "all":
            amt = row["bank"]  # raw int
        else:
            try:
                amt = to_raw(parse_amount(amount_str)[0])
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
                return
        if amt <= 0:
            await ctx.reply_error("Amount must be positive.")
            return
        if amt > row["bank"]:
            await ctx.reply_error(f"You only have **{row.h('bank'):,.2f}** in your bank.")
            return
        old_wallet = row["wallet"]
        amt_h = to_human(amt)
        old_wallet_h = to_human(old_wallet)
        # Confirmation
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(
            embed=(
                card("💳 Confirm Withdrawal", color=C_AMBER)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📤 Amount",       f"**${amt_h:,.2f}**",                              True)
                .field("🏦 Bank After",   f"**${row.h('bank') - amt_h:,.2f}**",             True)
                .field("💳 Wallet After", f"**${old_wallet_h + amt_h:,.2f}**",              True)
                .field("", f"Expires {fmt_ts(int(time.time() + 30))}", False)
                .build()
            ),
            view=view,
            mention_author=False,
        )
        confirmed = await view.wait_result()
        if not confirmed:
            await conf_msg.edit(embed=card("", description="Withdrawal cancelled.").color(C_SUBTLE).build(), view=None)
            return
        anim_task = asyncio.create_task(_tx_animate(conf_msg, "Withdrawing..."))
        try:
            _, new_wallet = await ctx.db.withdraw_from_bank(ctx.author.id, ctx.guild_id, amt)
        finally:
            anim_task.cancel()
        new_wallet_h = to_human(new_wallet)
        await conf_msg.edit(
            embed=(
                card("💳 Withdrawal Complete")
                .color(C_SUCCESS)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📤 Withdrawn",  f"**${amt_h:,.2f}**",        True)
                .field("💳 Wallet",     f"**${new_wallet_h:,.2f}**", True)
                .field("📊 Change",     f"{fmt_usd(old_wallet_h)} -> {fmt_usd(new_wallet_h)}", True)
                .footer("Funds are now in your wallet  •  /bank deposit to save them")
                .timestamp()
                .build()
            ),
            view=None,
        )
        await ctx.bot.bus.publish("withdraw", guild=ctx.guild, user=ctx.author, amount=amt_h)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "withdraw", amt_h, symbol="USD", amount=amt_h)

    # ════════════════════════════════════════════════════════════════════════
    #  TRANSFER (USD → another user)
    # ════════════════════════════════════════════════════════════════════════

    @bank.command(name="transfer", aliases=["give", "pay"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def transfer(self, ctx: DiscoContext, target: discord.Member, amount: str) -> None:
        """Transfer coins from your wallet to another user."""
        if target.id == ctx.author.id:
            await ctx.reply_error("You can't transfer to yourself.")
            return
        if target.bot:
            await ctx.reply_error("You can't transfer to a bot.")
            return
        # Strip leading $ so users can type e.g. "$50"
        clean_amount = str(amount).lstrip("$")
        try:
            amount_raw = to_raw(float(clean_amount))
        except ValueError:
            await ctx.reply_error("Invalid amount.")
            return
        if amount_raw <= 0:
            await ctx.reply_error("Amount must be positive.")
            return
        row = ctx.user_row
        wallet_raw = row["wallet"]
        if amount_raw > wallet_raw:
            await ctx.reply_error(f"You only have **{to_human(wallet_raw):,.2f}** in your wallet.")
            return
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        cur_label = guild_currency_name(settings)
        amount_h = to_human(amount_raw)
        wallet_h = to_human(wallet_raw)

        from cogs.crypto import ConfirmTradeView
        conf_embed = (
            card("📤 Confirm Transfer")
            .color(C_AMBER)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("📤 Amount",       f"**{amount_h:,.2f} {cur_label}**",                     True)
            .field("👤 Recipient",    target.mention,                                           True)
            .field("💳 Your Wallet",  f"{fmt_usd(wallet_h)} -> **{fmt_usd(wallet_h - amount_h)}**", True)
            .field("", f"This cannot be undone  · Expires {fmt_ts(int(time.time() + 30))}", False)
            .build()
        )
        conf_view = ConfirmTradeView(ctx.author.id)
        conf_msg = await ctx.reply(embed=conf_embed, view=conf_view, mention_author=False)
        await conf_view.wait()
        if not conf_view.confirmed:
            await conf_msg.edit(
                embed=card("").description("Transfer cancelled.").color(C_SUBTLE).build(),
                view=None,
            )
            return

        anim_task = asyncio.create_task(_tx_animate(conf_msg, "Sending transfer..."))
        try:
            tx_hash = await ctx.db.transfer_wallet(
                ctx.guild_id, ctx.author.id, target.id, amount_raw
            )
        finally:
            anim_task.cancel()

        remaining_h = wallet_h - amount_h
        await conf_msg.edit(
            embed=(
                card("📤 Transfer Sent")
                .color(C_SUCCESS)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📤 Amount",    f"**{amount_h:,.2f} {cur_label}**", True)
                .field("👤 Recipient", target.mention,                      True)
                .field("💳 Remaining", f"**${remaining_h:,.2f}**",         True)
                .footer(f"tx: {tx_hash}")
                .timestamp()
                .build()
            ),
            view=None,
        )
        await ctx.bot.bus.publish("transfer", guild=ctx.guild, sender=ctx.author,
            recipient=target, amount=amount_h, tx_hash=tx_hash)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "transfer", amount_h, symbol="USD", amount=amount_h)

    # ════════════════════════════════════════════════════════════════════════
    #  MOVE (universal storage mover)
    # ════════════════════════════════════════════════════════════════════════

    _STORAGE_ALIASES: dict[str, str] = {
        "cash": "cash", "c": "cash", "pocket": "cash",
        "bank": "bank", "b": "bank", "cefi": "bank",
        "wallet": "wallet", "w": "wallet", "defi": "wallet",
        "vault": "vault", "v": "vault", "save": "vault", "savings": "vault",
    }

    def _normalize_storage(self, s: str) -> str | None:
        return self._STORAGE_ALIASES.get(s.lower())

    @classmethod
    def _storage_hint(cls) -> str:
        """Build a dynamic hint string from _STORAGE_ALIASES."""
        from collections import defaultdict
        groups: defaultdict[str, list[str]] = defaultdict(list)
        for alias, canon in cls._STORAGE_ALIASES.items():
            if alias != canon:
                groups[canon].append(alias)
        parts = []
        for canon in dict.fromkeys(cls._STORAGE_ALIASES.values()):
            shorts = ", ".join(groups[canon])
            parts.append(f"`{canon}` ({shorts})" if shorts else f"`{canon}`")
        return ", ".join(parts)

    def _classify_token(self, token: str) -> str:
        if token == "USD":
            return "usd"
        if token == "SUN":
            return "sun"
        return "crypto"

    async def _get_move_balance(
        self, ctx: DiscoContext, token: str, from_loc: str
    ) -> float:
        """Return the human-scale balance for the given token in the given location."""
        uid, gid = ctx.author.id, ctx.guild_id
        if from_loc == "cash":
            return to_human(ctx.user_row["wallet"])
        if from_loc == "bank":
            if token == "USD":
                return to_human(ctx.user_row["bank"])
            h = await ctx.db.get_holding(uid, gid, token)
            return to_human(h["amount"]) if h else 0.0
        if from_loc == "wallet":
            tok_cfg = Config.TOKENS.get(token, {})
            if not tok_cfg:
                all_tok = await ctx.db.get_all_tokens_for_guild(gid)
                tok_cfg = all_tok.get(token, {})
            tok_net_full = tok_cfg.get("network", "")
            net_short = _FULL_TO_SHORT.get(tok_net_full, "")
            if not net_short:
                return 0.0
            h = await ctx.db.get_wallet_holding(uid, gid, net_short, token)
            return to_human(h["amount"]) if h else 0.0
        if from_loc == "vault":
            dep = await ctx.db.get_savings_deposit(uid, gid, token)
            return to_human(dep["amount"]) if dep else 0.0
        return 0.0

    async def _crypto_deposit(self, ctx: DiscoContext, first_arg: str, amount_str: str) -> None:
        """Move crypto from DeFi wallet_holdings → CeFi crypto_holdings."""
        first_lower = first_arg.lower()
        first_upper = first_arg.upper()
        # Network short matches ONLY when that network has a native token.  Moon
        # Network ("moon") has no native coin, so pass through to token lookup.
        if first_lower in _NETWORK_SHORTS and first_lower in _NET_NATIVE:
            net_short = first_lower
            sym = _NET_NATIVE[net_short]
        elif first_upper in _NET_NATIVE.values():
            sym = first_upper
            net_short = next(k for k, v in _NET_NATIVE.items() if v == sym)
        else:
            tok_cfg = Config.TOKENS.get(first_upper, {})
            if not tok_cfg:
                all_tok = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
                tok_cfg = all_tok.get(first_upper, {})
            if not tok_cfg:
                await ctx.reply_error(
                    f"Unknown token or network `{first_arg}`.\n"
                    f"Use a network code (`arc`/`sol`/`bnb`/`sun`) or token symbol."
                )
                return
            sym = first_upper
            tok_net_full = tok_cfg.get("network", "")
            net_short = _FULL_TO_SHORT.get(tok_net_full, "")
            if not net_short:
                await ctx.reply_error(f"Cannot determine network for `{sym}`.")
                return
        if amount_str.lower() == "all":
            h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, net_short, sym)
            _dep_all_raw = int(h["amount"]) if h else 0
            amt = to_human(_dep_all_raw)
            if amt <= 0:
                await ctx.reply_error(f"You have no **{sym}** in your {_NETWORK_SHORTS[net_short]} DeFi wallet to withdraw.")
                return
        else:
            _dep_all_raw = 0
            try:
                amt = parse_amount(amount_str)[0]
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
                return
        if not math.isfinite(amt) or amt <= 0:
            await ctx.reply_error("Amount must be greater than zero.")
            return
        if not await ctx.db.has_defi_wallet(ctx.author.id, ctx.guild_id, net_short):
            await ctx.reply_error_action(
                f"You don't have a **{_NETWORK_SHORTS[net_short]}** wallet.",
                f"Create {_NETWORK_SHORTS[net_short]} Wallet",
                f"wallet create {net_short}",
                rerun_original=True,
            )
            return
        h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, net_short, sym)
        bal = to_human(h["amount"]) if h else 0.0
        if bal < amt:
            await ctx.reply_error(f"You only have **{bal:,.6f} {sym}** in your {_NETWORK_SHORTS[net_short]} DeFi wallet.")
            return

        tok_cfg = Config.TOKENS.get(sym, {})
        emoji = tok_cfg.get("emoji", "●")
        conf_embed = (
            card("📥 Confirm Withdraw from DeFi")
            .description(
                f"Move **{amt:,.6f} {emoji}{sym}** from your DeFi wallet → CeFi holdings\n"
                "No platform fee  -  this direction is free."
            )
            .color(C_AMBER)
            .field("🔐 From", f"**{_NETWORK_SHORTS[net_short]}** DeFi Wallet", True)
            .field("🏦 To", "CeFi Holdings", True)
            .field("💎 Amount", f"**{amt:,.6f} {emoji}{sym}**", True)
            .field("", f"Expires {fmt_ts(int(time.time() + 30))}", False)
            .build()
        )
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(embed=conf_embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await conf_msg.edit(view=None)
        if not confirmed:
            await conf_msg.edit(embed=card(description="Withdrawal cancelled.", color=C_NEUTRAL).build())
            return

        _wallet_hold_delta = -_dep_all_raw if _dep_all_raw else to_raw(-amt)
        try:
            await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, net_short, sym, _wallet_hold_delta)
        except ValueError:
            await conf_msg.edit(
                embed=card(description=f"Insufficient {sym} balance in your DeFi wallet.", color=C_ERROR).build(),
                view=None,
            )
            return
        # Credit CeFi with exact raw amount when depositing all, to avoid float round-trip error
        _cefi_credit = _dep_all_raw if _dep_all_raw else to_raw(amt)
        new_cefi = await ctx.db.update_holding(ctx.author.id, ctx.guild_id, sym, _cefi_credit)
        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "DEPOSIT",
            symbol_in=sym, amount_in=to_raw(amt),
            symbol_out=sym, amount_out=to_raw(amt),
            network=net_short,
        )
        result_embed = (
            card("📤 Withdrawn from DeFi")
            .description(
                f"Moved **{amt:,.6f} {emoji}{sym}** from your DeFi wallet to CeFi holdings."
            )
            .color(C_SUCCESS)
            .field("🔐 From", f"**{_NETWORK_SHORTS[net_short]}** DeFi Wallet", True)
            .field("🏦 CeFi Balance", f"**{to_human(new_cefi):,.6f} {emoji}{sym}**", True)
            .build()
        )
        await conf_msg.edit(embed=result_embed)
        await ctx.bot.bus.publish(
            "crypto_deposit",
            guild=ctx.guild, user=ctx.author,
            symbol=sym, amount=amt,
            network=_NETWORK_SHORTS.get(net_short, net_short),
        )
        _usd = await _whale.usd_value_of(ctx.bot, sym, amt, ctx.guild_id)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "deposit", _usd, symbol=sym, network=net_short, amount=amt)

    async def _crypto_withdraw(self, ctx: DiscoContext, first_arg: str, amount_str: str) -> None:
        """Move crypto from CeFi crypto_holdings → DeFi wallet_holdings."""
        first_lower = first_arg.lower()
        first_upper = first_arg.upper()
        # Network short matches ONLY when that network has a native token.  Moon
        # Network ("moon") is a valid short code but has no native token, so we
        # fall through to the token-symbol lookup (MOON as a group token).
        if first_lower in _NETWORK_SHORTS and first_lower in _NET_NATIVE:
            net_short = first_lower
            sym = _NET_NATIVE[net_short]
        elif first_upper in _NET_NATIVE.values():
            sym = first_upper
            net_short = next(k for k, v in _NET_NATIVE.items() if v == sym)
        else:
            tok_cfg = Config.TOKENS.get(first_upper, {})
            if not tok_cfg:
                all_tok = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
                tok_cfg = all_tok.get(first_upper, {})
            if not tok_cfg:
                await ctx.reply_error(
                    f"Unknown token or network `{first_arg}`.\n"
                    f"Use a network code (`arc`/`sol`/`bnb`/`sun`) or token symbol."
                )
                return
            sym = first_upper
            tok_net_full = tok_cfg.get("network", "")
            net_short = _FULL_TO_SHORT.get(tok_net_full, "")
            if not net_short:
                await ctx.reply_error(f"Cannot determine network for `{sym}`.")
                return
        if amount_str.lower() == "all":
            h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, sym)
            _all_raw = int(h["amount"]) if h else 0
            amt = to_human(_all_raw)
            if amt <= 0:
                await ctx.reply_error(f"You have no **{sym}** in your CeFi holdings to deposit.")
                return
        else:
            _all_raw = 0
            try:
                amt = parse_amount(amount_str)[0]
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
                return
        if not math.isfinite(amt) or amt <= 0:
            await ctx.reply_error("Amount must be greater than zero.")
            return
        if not await ctx.db.has_defi_wallet(ctx.author.id, ctx.guild_id, net_short):
            await ctx.reply_error_action(
                f"You don't have a **{_NETWORK_SHORTS[net_short]}** wallet.",
                f"Create {_NETWORK_SHORTS[net_short]} Wallet",
                f"wallet create {net_short}",
                rerun_original=True,
            )
            return
        # Re-read holding to get fresh balance (handles race if amount_str was "all")
        h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, sym)
        cefi_bal = to_human(h["amount"]) if h else 0.0
        if cefi_bal < amt - 1e-9:
            await ctx.reply_error(
                f"Insufficient CeFi balance. You have **{cefi_bal:,.6f} {sym}** in CeFi holdings.\n"
                f"Buy more with `.buy {sym.lower()} <amount>`"
            )
            return
        # For "all", snap to the raw balance to avoid float round-trip precision error
        if _all_raw:
            _fresh_raw = int(h["amount"]) if h else 0
            _hold_delta = -_fresh_raw
            amt = to_human(_fresh_raw)
        else:
            _hold_delta = to_raw(-amt)
        _fee_cfg = await ctx.db.guilds.get_fee_config(ctx.guild_id)
        price_row_wd = await ctx.db.get_price(sym, ctx.guild_id)
        _price_wd = float(price_row_wd["price"]) if price_row_wd else 0.0
        usd_value_wd = amt * _price_wd
        raw_fee_wd = usd_value_wd * _fee_cfg["platform_fee_pct"]
        platform_fee = max(_fee_cfg["platform_fee_min"], min(_fee_cfg["platform_fee_max"], raw_fee_wd))
        # Fee is deducted from the crypto being moved (crypto-native behavior).
        # Convert USD fee to crypto units at the current price.
        fee_in_crypto = platform_fee / _price_wd if _price_wd > 0 else 0.0
        if fee_in_crypto >= amt:
            await ctx.reply_error(
                f"Move too small  -  the minimum fee (${_fee_cfg['platform_fee_min']:,.2f}) "
                f"exceeds the value of **{amt:,.6f} {sym}** (${usd_value_wd:,.4f}). "
                f"Move a larger amount."
            )
            return
        net_amt = amt - fee_in_crypto
        tok_cfg = Config.TOKENS.get(sym, {})
        emoji = tok_cfg.get("emoji", "●")

        reserve_half_wd = platform_fee / 2.0
        conf_wd = (
            card("📤 Confirm Move to DeFi Wallet")
            .description(
                f"Move **{amt:,.6f} {emoji}{sym}** from CeFi holdings to your **{_NETWORK_SHORTS[net_short]}** DeFi wallet.\n\n"
                f"**Platform fee:** {fee_in_crypto:,.6f} {sym} "
                f"(${platform_fee:,.2f}, {_fee_cfg['platform_fee_pct']*100:.2g}% of ${usd_value_wd:,.2f})\n"
                f"**You will receive:** {net_amt:,.6f} {emoji}{sym}\n"
                f"↳ **${reserve_half_wd:,.2f}** → USD Vault"
            )
            .color(C_AMBER)
            .build()
        )
        view_wd = ConfirmView(ctx.author.id, timeout=30)
        conf_msg_wd = await ctx.reply(embed=conf_wd, view=view_wd, mention_author=False)
        confirmed_wd = await view_wd.wait_result()
        if confirmed_wd is not True:
            try:
                await conf_msg_wd.edit(embed=card("", description="Move cancelled.", color=C_NEUTRAL).build(), view=None)
            except Exception:
                pass
            return

        # Debit full amount from CeFi holdings; credit DeFi with net (amt - fee).
        # The fee portion is burned from the crypto side and half of its USD value
        # is routed to the community reserve (matching the sell-fee accounting).
        await ctx.db.update_holding(ctx.author.id, ctx.guild_id, sym, _hold_delta)
        if _all_raw:
            _fee_crypto_raw = to_raw(fee_in_crypto)
            _defi_credit = max(0, _fresh_raw - _fee_crypto_raw)
        else:
            _defi_credit = to_raw(net_amt)
        new_defi = await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, net_short, sym, _defi_credit)
        if platform_fee > 0:
            await ctx.db.split_to_community_reserves(ctx.guild_id, "USD", to_raw(platform_fee))
        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "WITHDRAW",
            symbol_in=sym, amount_in=to_raw(amt),
            symbol_out=sym, amount_out=to_raw(net_amt),
            network=net_short,
        )
        result_embed = (
            card("📤 Crypto Withdraw")
            .description(
                f"Moved **{net_amt:,.6f} {emoji}{sym}** from CeFi holdings to your DeFi wallet.\n"
                f"Platform fee: **{fee_in_crypto:,.6f} {sym}** "
                f"(${platform_fee:,.2f}, {_fee_cfg['platform_fee_pct']*100:.2g}% of ${usd_value_wd:,.2f})\n"
                f"↳ **${reserve_half_wd:,.2f}** → USD Vault\n"
                f"DeFi wallet balance: **{to_human(new_defi):,.6f} {sym}**"
            )
            .color(C_SUCCESS)
            .build()
        )
        await ctx.reply(embed=result_embed, mention_author=False)
        await ctx.bot.bus.publish(
            "crypto_withdraw",
            guild=ctx.guild, user=ctx.author,
            symbol=sym, amount=amt,
            network=_NETWORK_SHORTS.get(net_short, net_short),
            platform_fee=platform_fee,
        )
        _usd = await _whale.usd_value_of(ctx.bot, sym, amt, ctx.guild_id)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "withdraw", _usd, symbol=sym, network=net_short, amount=amt)

    async def _move_everything(self, ctx: DiscoContext, from_loc: str, to_loc: str) -> None:
        """Move ALL tokens and USD from one storage location to another."""
        uid, gid = ctx.author.id, ctx.guild_id
        from_norm = self._normalize_storage(from_loc)
        to_norm = self._normalize_storage(to_loc)
        if from_norm is None:
            await ctx.reply_error(f"Unknown storage `{from_loc}`. Use: {self._storage_hint()}")
            return
        if to_norm is None:
            await ctx.reply_error(f"Unknown storage `{to_loc}`. Use: {self._storage_hint()}")
            return
        if from_norm == to_norm:
            await ctx.reply_error("Source and destination must be different.")
            return

        # Gather all balances in from_norm
        items: list[tuple[str, float]] = []  # (token, amount)
        valid_tokens = set((await ctx.db.get_all_tokens_for_guild(gid)).keys()) | {"USD"}

        if from_norm == "cash":
            cash = to_human(ctx.user_row["wallet"])
            if cash > 0:
                items.append(("USD", cash))
        elif from_norm == "bank":
            bank_bal = to_human(ctx.user_row["bank"])
            if bank_bal > 0:
                items.append(("USD", bank_bal))
            holdings = await ctx.db.get_holdings(uid, gid)
            for h in holdings:
                amt = to_human(h["amount"])
                if amt > 0 and h["symbol"] in valid_tokens:
                    items.append((h["symbol"], amt))
        elif from_norm == "wallet":
            all_wallet = await ctx.db.get_all_wallet_holdings(uid, gid)
            for h in all_wallet:
                amt = to_human(h.get("amount", 0))
                if amt > 0 and h["symbol"] in valid_tokens:
                    items.append((h["symbol"], amt))
        elif from_norm == "vault":
            for tok in ["USD", "SUN"]:
                dep = await ctx.db.get_savings_deposit(uid, gid, tok)
                if dep and to_human(dep["amount"]) > 0:
                    items.append((tok, to_human(dep["amount"])))

        if not items:
            await ctx.reply_error(f"You have nothing in **{from_norm}** to move.")
            return

        # Build confirmation embed showing what will be moved
        move_lines = []
        total_fees = 0.0
        for tok, amt in items:
            if tok == "USD":
                move_lines.append(f"**${amt:,.2f}** USD")
            else:
                tcfg = Config.TOKENS.get(tok, {})
                emoji = tcfg.get("emoji", "")
                line = f"**{amt:,.6f}** {emoji}{tok}"
                # Show platform fee estimate for bank→wallet crypto moves
                if from_norm == "bank" and to_norm == "wallet" and self._classify_token(tok) in ("crypto", "sun"):
                    _fee_cfg = await ctx.db.guilds.get_fee_config(gid)
                    price_row = await ctx.db.get_price(tok, gid)
                    _price = float(price_row["price"]) if price_row else 0.0
                    usd_val = amt * _price
                    raw_fee = usd_val * _fee_cfg["platform_fee_pct"]
                    plat_fee = max(_fee_cfg["platform_fee_min"], min(_fee_cfg["platform_fee_max"], raw_fee))
                    fee_tok = plat_fee / _price if _price > 0 else 0.0
                    line += f"  💸 {fee_tok:,.6f} {tok} fee (${plat_fee:,.2f})"
                    total_fees += plat_fee
                move_lines.append(line)

        desc = (
            f"Moving **everything** from **{from_norm}** → **{to_norm}**:\n\n"
            + "\n".join(move_lines)
        )
        if total_fees > 0:
            desc += f"\n\n💸 **Total platform fees:** ${total_fees:,.2f} (deducted from each token)"
        desc += f"\n\n*{len(items)} asset(s) total. Some routes may not be valid and will be skipped.*"
        if ctx.is_chain_step:
            msg = None
        else:
            confirm_embed = card("📦 Move Everything", description=desc, color=C_AMBER).footer("Confirm within 30 seconds").build()
            view = ConfirmView(ctx.author.id, timeout=30)
            msg = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
            view.message = msg
            confirmed = await view.wait_result()
            if confirmed is not True:
                try:
                    await msg.edit(embed=card("📦 Move Cancelled", color=C_NEUTRAL).build(), view=None)
                except Exception:
                    pass
                return

        # Execute moves one by one, collecting results
        moved: list[str] = []
        skipped: list[str] = []
        for tok, amt in items:
            try:
                actual = await self._get_move_balance(ctx, tok, from_norm)
                if actual <= 0:
                    skipped.append(f"{tok}: no balance")
                    continue
                amt = min(amt, actual)
                tok_class = self._classify_token(tok)
                move_net = ""
                move_fee = 0.0
                fee_in_crypto = 0.0
                net_amt = amt
                # Execute the actual transfer directly
                if tok_class == "usd" and from_norm == "cash" and to_norm == "bank":
                    await ctx.db.update_wallet(uid, gid, to_raw(-amt))
                    await ctx.db.update_bank(uid, gid, to_raw(amt))
                elif tok_class == "usd" and from_norm == "bank" and to_norm == "cash":
                    await ctx.db.update_bank(uid, gid, to_raw(-amt))
                    await ctx.db.update_wallet(uid, gid, to_raw(amt))
                elif tok_class == "usd" and from_norm in ("cash", "bank") and to_norm == "vault":
                    if from_norm == "cash":
                        await ctx.db.update_wallet(uid, gid, to_raw(-amt))
                    else:
                        await ctx.db.update_bank(uid, gid, to_raw(-amt))
                    existing = await ctx.db.get_savings_deposit(uid, gid, "USD")
                    old_bal_raw = int(existing["amount"]) if existing else 0
                    await ctx.db.upsert_savings_deposit(uid, gid, "USD", old_bal_raw + to_raw(amt), time.time())
                elif tok_class == "usd" and from_norm == "vault" and to_norm in ("cash", "bank"):
                    dep = await ctx.db.get_savings_deposit(uid, gid, "USD")
                    if not dep or to_human(dep["amount"]) <= 0:
                        skipped.append(f"{tok}: no vault balance")
                        continue
                    amt = min(amt, to_human(dep["amount"]))
                    new_bal_raw = int(dep["amount"]) - to_raw(amt)
                    if to_norm == "cash":
                        await ctx.db.update_wallet(uid, gid, to_raw(amt))
                    else:
                        await ctx.db.update_bank(uid, gid, to_raw(amt))
                    if new_bal_raw < to_raw(0.001):
                        await ctx.db.delete_savings_deposit(uid, gid, "USD")
                    else:
                        await ctx.db.upsert_savings_deposit(uid, gid, "USD", new_bal_raw, dep["last_interest"])
                elif tok_class in ("crypto", "sun") and from_norm == "wallet" and to_norm == "bank":
                    tok_cfg = Config.TOKENS.get(tok, {})
                    tok_net_full = tok_cfg.get("network", "")
                    move_net = _FULL_TO_SHORT.get(tok_net_full, "")
                    if not move_net:
                        skipped.append(f"{tok}: unknown network")
                        continue
                    await ctx.db.update_wallet_holding(uid, gid, move_net, tok, to_raw(-amt))
                    await ctx.db.update_holding(uid, gid, tok, to_raw(amt))
                elif tok_class in ("crypto", "sun") and from_norm == "bank" and to_norm == "wallet":
                    tok_cfg = Config.TOKENS.get(tok, {})
                    tok_net_full = tok_cfg.get("network", "")
                    move_net = _FULL_TO_SHORT.get(tok_net_full, "")
                    if not move_net:
                        skipped.append(f"{tok}: unknown network")
                        continue
                    if not await ctx.db.has_defi_wallet(uid, gid, move_net):
                        skipped.append(f"{tok}: no {move_net} wallet")
                        continue
                    # Platform fee: deducted from the crypto amount itself (not USD wallet)
                    _fee_cfg = await ctx.db.guilds.get_fee_config(gid)
                    price_row = await ctx.db.get_price(tok, gid)
                    _price = float(price_row["price"]) if price_row else 0.0
                    usd_val = amt * _price
                    raw_fee = usd_val * _fee_cfg["platform_fee_pct"]
                    move_fee = max(_fee_cfg["platform_fee_min"], min(_fee_cfg["platform_fee_max"], raw_fee))
                    # Apex Mastery: Light Touch (utility.tx_fee_cut)
                    # trims the move fee. Min floor still applies after
                    # so the protocol isn't paying to ship tiny moves.
                    try:
                        from services import mastery as _mastery_t
                        _mp = await _mastery_t.passives(ctx.db, uid, gid)
                        _tx_cut = float(_mp.get("utility.tx_fee_cut") or 0.0)
                        if _tx_cut > 0:
                            move_fee = max(
                                _fee_cfg["platform_fee_min"],
                                move_fee * max(0.0, 1.0 - _tx_cut),
                            )
                    except Exception:
                        log.debug(
                            "utility.tx_fee_cut passive read failed",
                            exc_info=True,
                        )
                    fee_in_crypto = move_fee / _price if _price > 0 else 0.0
                    if fee_in_crypto >= amt:
                        skipped.append(
                            f"{tok}: move too small (fee ${move_fee:,.2f} > value ${usd_val:,.4f})"
                        )
                        continue
                    amt_raw = to_raw(amt)
                    net_amt = amt - fee_in_crypto
                    net_amt_raw = to_raw(net_amt)
                    await ctx.db.update_holding(uid, gid, tok, -amt_raw)
                    await ctx.db.update_wallet_holding(uid, gid, move_net, tok, net_amt_raw)
                    if move_fee > 0:
                        await ctx.db.split_to_community_reserves(gid, "USD", to_raw(move_fee))
                else:
                    skipped.append(f"{tok}: unsupported route {from_norm}->{to_norm}")
                    continue
                # Log each move transaction (non-fatal)
                try:
                    await ctx.db.log_tx(
                        gid, uid, "MOVE",
                        symbol_in=tok, amount_in=to_raw(amt),
                        symbol_out=tok, amount_out=to_raw(net_amt),
                        network=move_net,
                    )
                except Exception:
                    pass
                if tok == "USD":
                    moved.append(f"**${amt:,.2f}** USD")
                else:
                    if fee_in_crypto > 0:
                        fee_str = f"  💸 {fee_in_crypto:,.6f} {tok} fee (${move_fee:,.2f})"
                    else:
                        fee_str = ""
                    moved.append(f"**{net_amt:,.6f}** {tok}{fee_str}")
            except Exception as exc:
                skipped.append(f"{tok}: {str(exc)[:50]}")

        result_lines = []
        if moved:
            result_lines.append("**Moved:**\n" + "\n".join(moved))
        if skipped:
            result_lines.append("**Skipped:**\n" + "\n".join(skipped))

        result_embed = (
            card("📦 Move Everything - Complete", color=C_SUCCESS if moved else C_ERROR)
            .description("\n\n".join(result_lines) or "Nothing was moved.")
            .footer(f"{from_norm} → {to_norm}")
            .build()
        )
        try:
            await msg.edit(embed=result_embed, view=None)
        except Exception:
            await ctx.reply(embed=result_embed, mention_author=False)

    @bank.command(name="move", aliases=["mv"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def move(
        self,
        ctx: DiscoContext,
        amount: str,
        token: str,
        from_loc: str,
        to_loc: str,
    ) -> None:
        """Move funds between storage locations.
        Usage: /bank move <amount|all> <token> <from> <to>
               /bank move everything <from> <to>
        Storage: cash/c, bank/b, wallet/w, vault/v"""
        # Handle "everything"  -  amount=everything, token=from, from_loc=to, to_loc unused
        if amount.lower() == "everything":
            await self._move_everything(ctx, from_loc=token, to_loc=from_loc)
            return
        token = token.upper()

        from_norm = self._normalize_storage(from_loc)
        to_norm = self._normalize_storage(to_loc)
        if from_norm is None:
            await ctx.reply_error(f"Unknown storage `{from_loc}`. Use: {self._storage_hint()}")
            return
        if to_norm is None:
            await ctx.reply_error(f"Unknown storage `{to_loc}`. Use: {self._storage_hint()}")
            return
        if from_norm == to_norm:
            await ctx.reply_error("Source and destination must be different.")
            return

        valid_set = set((await ctx.db.get_all_tokens_for_guild(ctx.guild_id)).keys()) | {"USD"}
        if token not in valid_set:
            await ctx.reply_error(f"Unknown token **{token}**.")
            return
        tok_class = self._classify_token(token)

        amount = str(amount)
        _moving_all = amount.lower() == "all"
        if _moving_all:
            amt = await self._get_move_balance(ctx, token, from_norm)
            if amt <= 0:
                await ctx.reply_error(f"You have no **{token}** in **{from_norm}**.")
                return
        else:
            try:
                _parsed, _usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
                return
            if _usd_mode and token != "USD":
                # $1 dsc → convert USD to token quantity via price
                price_row = await ctx.db.get_price(token, ctx.guild_id)
                if not price_row or float(price_row["price"]) <= 0:
                    await ctx.reply_error(f"Cannot look up price for **{token}**.")
                    return
                amt = _parsed / float(price_row["price"])
            else:
                amt = _parsed
        if not math.isfinite(amt) or amt <= 0:
            await ctx.reply_error("Amount must be a positive number.")
            return

        uid, gid = ctx.author.id, ctx.guild_id

        # ---- cash <-> bank (USD) ----
        if tok_class == "usd" and from_norm == "cash" and to_norm == "bank":
            await self._usd_deposit(ctx, "all" if _moving_all else str(amt))
            return
        if tok_class == "usd" and from_norm == "bank" and to_norm == "cash":
            await self._usd_withdraw(ctx, "all" if _moving_all else str(amt))
            return

        # ---- cash -> vault (USD savings) ----
        if tok_class == "usd" and from_norm == "cash" and to_norm == "vault":
            min_dep_h = to_human(_M["min_deposit"])
            if amt < min_dep_h:
                await ctx.reply_error(f"Minimum savings deposit is **${min_dep_h:,.2f}**.")
                return
            amt_raw = ctx.user_row["wallet"] if _moving_all else to_raw(amt)
            if ctx.user_row["wallet"] < amt_raw:
                await ctx.reply_error(f"You only have **${to_human(ctx.user_row['wallet']):,.2f}** in your wallet.")
                return
            existing = await ctx.db.get_savings_deposit(uid, gid, "USD")
            old_bal_raw = existing["amount"] if existing else 0
            new_bal_raw = old_bal_raw + amt_raw
            spin_msg = await ctx.reply(embed=card(f"{_TX_SPIN[0]} Moving to vault...").color(C_AMBER).build(), mention_author=False)
            anim_task = asyncio.create_task(_tx_animate(spin_msg, "Moving to vault..."))
            try:
                await ctx.db.update_wallet(uid, gid, -amt_raw)
                await ctx.db.upsert_savings_deposit(uid, gid, "USD", new_bal_raw, time.time())
            finally:
                anim_task.cancel()
            _usd_borrow_d, _usd_save_d, _, _, _ = await self._market_info(gid, "USD")
            new_bal_h = to_human(new_bal_raw)
            # Vaultstone XP: award on savings deposit for immediate feedback
            _VS_CFG_dep = Config.SHOP_ITEMS.get("vaultstone", {})
            if _VS_CFG_dep:
                vaultstone_dep = await ctx.db.get_vaultstone(uid, gid)
                if vaultstone_dep and vaultstone_dep["level"] < _VS_CFG_dep.get("max_level", 50):
                    _base_xp_dep = _VS_CFG_dep.get("xp_per_interest", 10.0) * 0.25
                    _dep_xp_scale = min(Config.XP_SCALE_MAX, max(0.0, amt / Config.XP_SAVINGS_REFERENCE_USD))
                    _vs_xp = _base_xp_dep * _dep_xp_scale
                    xp_result = await ctx.db.add_vaultstone_xp(uid, gid, _vs_xp)
                    if xp_result:
                        live_xp, live_level = xp_result
                        capped_xp = cap_xp(live_xp, live_level, _VS_CFG_dep)
                        if capped_xp < live_xp:
                            await ctx.db.update_vaultstone_xp(uid, gid, capped_xp, live_level)
                        await notify_item_levelup_ready(ctx.bot, uid, ctx.guild, "vaultstone", live_xp - _vs_xp, live_xp, live_level, vaultstone_dep["staked_amount"])
            await spin_msg.edit(
                embed=(
                    card("💰 Savings Deposit")
                    .color(C_SUCCESS)
                    .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                    .field("📥 Deposited",        f"**${amt:,.2f}**",                                   True)
                    .field("💰 Vault Balance",     f"**${new_bal_h:,.2f}**",                             True)
                    .field("📈 Savings APY",       f"**{_usd_save_d * 365:.1f}%/yr**  ({_usd_save_d * 100:.3f}%/day)", True)
                    .field("📊 Est. Daily Yield",  f"**+${new_bal_h * _usd_save_d:,.4f}**/day",         True)
                    .footer("Interest compounds hourly  •  /bank savings withdraw to access funds")
                    .timestamp()
                    .build()
                ),
            )
            return

        # ---- vault -> cash (USD unsave) ----
        if tok_class == "usd" and from_norm == "vault" and to_norm == "cash":
            dep = await ctx.db.get_savings_deposit(uid, gid, "USD")
            if not dep or dep["amount"] <= 0:
                await ctx.reply_error("You have no USD in savings.")
                return
            dep_raw = dep["amount"]  # raw int
            amt_raw = dep_raw if _moving_all else min(to_raw(amt), dep_raw)
            amt_h = to_human(amt_raw)
            new_bal_raw = dep_raw - amt_raw
            new_bal_h = to_human(new_bal_raw)
            spin_msg = await ctx.reply(embed=card(f"{_TX_SPIN[0]} Withdrawing from vault...").color(C_AMBER).build(), mention_author=False)
            anim_task = asyncio.create_task(_tx_animate(spin_msg, "Withdrawing from vault..."))
            try:
                await ctx.db.update_wallet(uid, gid, amt_raw)
            finally:
                anim_task.cancel()
            if new_bal_h < 0.001:
                await ctx.db.delete_savings_deposit(uid, gid, "USD")
            else:
                await ctx.db.upsert_savings_deposit(uid, gid, "USD", new_bal_raw, dep["last_interest"])
            vault_str = f"**${new_bal_h:,.2f}**" if new_bal_h >= 0.001 else "*Account closed*"
            await spin_msg.edit(
                embed=(
                    card("💰 Savings Withdrawal")
                    .color(C_SUCCESS)
                    .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                    .field("📤 Withdrawn",      f"**${amt_h:,.2f}**",  True)
                    .field("💰 Vault Remaining", vault_str,              True)
                    .footer("Funds moved to your wallet  •  /bank savings deposit to re-save")
                    .timestamp()
                    .build()
                ),
            )
            return

        # ---- bank -> vault (USD savings directly from bank) ----
        if tok_class == "usd" and from_norm == "bank" and to_norm == "vault":
            min_dep_h2 = to_human(_M["min_deposit"])
            if amt < min_dep_h2:
                await ctx.reply_error(f"Minimum savings deposit is **${min_dep_h2:,.2f}**.")
                return
            amt_raw2 = ctx.user_row["bank"] if _moving_all else to_raw(amt)
            if ctx.user_row["bank"] < amt_raw2:
                await ctx.reply_error(f"You only have **${to_human(ctx.user_row['bank']):,.2f}** in your bank.")
                return
            existing = await ctx.db.get_savings_deposit(uid, gid, "USD")
            old_bal_raw2 = existing["amount"] if existing else 0
            new_bal_raw2 = old_bal_raw2 + amt_raw2
            spin_msg2 = await ctx.reply(embed=card(f"{_TX_SPIN[0]} Moving to vault...").color(C_AMBER).build(), mention_author=False)
            anim_task = asyncio.create_task(_tx_animate(spin_msg2, "Moving to vault..."))
            try:
                await ctx.db.update_bank(uid, gid, -amt_raw2)
                await ctx.db.upsert_savings_deposit(uid, gid, "USD", new_bal_raw2, time.time())
                _usd_borrow_d2, _usd_save_d2, _, _, _ = await self._market_info(gid, "USD")
                _VS_CFG_dep2 = Config.SHOP_ITEMS.get("vaultstone", {})
                if _VS_CFG_dep2:
                    vaultstone_dep2 = await ctx.db.get_vaultstone(uid, gid)
                    if vaultstone_dep2 and vaultstone_dep2["level"] < _VS_CFG_dep2.get("max_level", 50):
                        _base_xp_dep2 = _VS_CFG_dep2.get("xp_per_interest", 10.0) * 0.25
                        _dep_xp_scale2 = min(Config.XP_SCALE_MAX, max(0.0, amt / Config.XP_SAVINGS_REFERENCE_USD))
                        _vs_xp2 = _base_xp_dep2 * _dep_xp_scale2
                        xp_result2 = await ctx.db.add_vaultstone_xp(uid, gid, _vs_xp2)
                        if xp_result2:
                            live_xp2, live_level2 = xp_result2
                            capped_xp2 = cap_xp(live_xp2, live_level2, _VS_CFG_dep2)
                            if capped_xp2 < live_xp2:
                                await ctx.db.update_vaultstone_xp(uid, gid, capped_xp2, live_level2)
                            await notify_item_levelup_ready(ctx.bot, uid, ctx.guild, "vaultstone", live_xp2 - _vs_xp2, live_xp2, live_level2, vaultstone_dep2["staked_amount"])
            finally:
                anim_task.cancel()
            new_bal_h2 = to_human(new_bal_raw2)
            await spin_msg2.edit(
                embed=(
                    card("💰 Savings Deposit")
                    .color(C_SUCCESS)
                    .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                    .field("📥 Deposited",        f"**${amt:,.2f}**  (from bank)",                        True)
                    .field("💰 Vault Balance",     f"**${new_bal_h2:,.2f}**",                              True)
                    .field("📈 Savings APY",       f"**{_usd_save_d2 * 365:.1f}%/yr**  ({_usd_save_d2 * 100:.3f}%/day)", True)
                    .field("📊 Est. Daily Yield",  f"**+${new_bal_h2 * _usd_save_d2:,.4f}**/day",         True)
                    .footer("Interest compounds hourly  •  /bank savings withdraw to access funds")
                    .timestamp()
                    .build()
                ),
            )
            return

        # ---- vault -> bank (USD unsave to bank) ----
        if tok_class == "usd" and from_norm == "vault" and to_norm == "bank":
            dep = await ctx.db.get_savings_deposit(uid, gid, "USD")
            if not dep or dep["amount"] <= 0:
                await ctx.reply_error("You have no USD in savings.")
                return
            dep_h = to_human(dep["amount"])
            amt = min(amt, dep_h)
            new_bal = dep_h - amt
            spin_msg3 = await ctx.reply(embed=card(f"{_TX_SPIN[0]} Withdrawing from vault...").color(C_AMBER).build(), mention_author=False)
            anim_task = asyncio.create_task(_tx_animate(spin_msg3, "Withdrawing from vault..."))
            try:
                await ctx.db.update_bank(uid, gid, to_raw(amt))
                if new_bal < 0.001:
                    await ctx.db.delete_savings_deposit(uid, gid, "USD")
                else:
                    await ctx.db.upsert_savings_deposit(uid, gid, "USD", to_raw(new_bal), dep["last_interest"])
            finally:
                anim_task.cancel()
            vault_str2 = f"**${new_bal:,.2f}**" if new_bal >= 0.001 else "*Account closed*"
            await spin_msg3.edit(
                embed=(
                    card("💰 Savings Withdrawal")
                    .color(C_SUCCESS)
                    .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                    .field("📤 Withdrawn",      f"**${amt:,.2f}**",  True)
                    .field("💰 Vault Remaining", vault_str2,          True)
                    .field("🏦 Moved To",        "Your bank account", True)
                    .footer("Funds moved to your bank  •  /bank savings deposit to re-save")
                    .timestamp()
                    .build()
                ),
            )
            return

        # ---- bank -> wallet (crypto CeFi -> DeFi, has fee) ----
        if tok_class in ("crypto", "sun") and from_norm == "bank" and to_norm == "wallet":
            await self._crypto_withdraw(ctx, token, "all" if _moving_all else str(amt))
            return

        # ---- wallet -> bank (crypto DeFi -> CeFi) ----
        if tok_class in ("crypto", "sun") and from_norm == "wallet" and to_norm == "bank":
            await self._crypto_deposit(ctx, token, "all" if _moving_all else str(amt))
            return

        # Invalid route
        p = ctx.prefix or Config.PREFIX
        usd_routes = f"`{p}bank move USD cash bank` / `bank cash` / `cash vault` / `vault cash` / `bank vault` / `vault bank`"
        sun_routes = f"`{p}bank move SUN bank wallet` / `wallet bank`"
        crypto_routes = f"`{p}bank move ARC bank wallet` / `wallet bank`"
        await ctx.reply_error(
            f"Can't move **{token}** from **{from_norm}** to **{to_norm}**.\n\n"
            f"**USD:** {usd_routes}\n"
            f"**SUN:** {sun_routes}\n"
            f"**Crypto:** {crypto_routes}"
        )

    # ════════════════════════════════════════════════════════════════════════
    #  SAVINGS SUBGROUP
    # ════════════════════════════════════════════════════════════════════════

    @bank.group(name="savings", aliases=["save"], invoke_without_command=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def savings(self, ctx: DiscoContext) -> None:
        """Show your savings balances and current market rates."""
        if await suggest_subcommand(ctx, self.savings):
            return
        usd_dep = await ctx.db.get_savings_deposit(ctx.author.id, ctx.guild_id, "USD")

        usd_b = to_human(usd_dep["amount"]) if usd_dep else 0.0

        usd_borrow, usd_save, usd_util, usd_total_dep, usd_total_bor = \
            await self._market_info(ctx.guild_id, "USD")

        vaultstone = await ctx.db.get_vaultstone(ctx.author.id, ctx.guild_id)
        _vs_bonus = _item_stat(vaultstone, "interest_bonus")

        _b = (
            card("💰 Savings Accounts")
            .color(C_INFO)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .description("Your savings positions and current market rates.")
        )

        # ── Wealth Bottleneck preview ──────────────────────────────────
        # Show the player exactly how the rank-based bottleneck will scale
        # their interest accrual on the next savings tick so they can see
        # whether their listed APY will land at full or get clipped (or
        # boosted from the community pool).
        from services.bottleneck import lookup_percentile, bottleneck_multiplier
        _pctile, _user_nw, _n = await lookup_percentile(
            ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
        )
        _wt_mult = bottleneck_multiplier(_pctile) if _n >= max(2, int(getattr(Config, "BOTTLENECK_MIN_HOLDERS", 5))) else 1.0

        # ── USD savings position
        _eff_save = usd_save * _wt_mult
        if usd_b > 0:
            _b.field(
                "💵 USD Savings",
                (
                    f"**Balance:** ${usd_b:,.2f}\n"
                    f"**APY:** {fmt_bonus(f'{usd_save * 365:.1f}%/yr', _vs_bonus)}  ({usd_save * 100:.3f}%/day)\n"
                    f"**Est. daily:** +${usd_b * _eff_save:,.4f}"
                ),
                True,
            )
        else:
            _b.field(
                "💵 USD Savings",
                f"*No active deposit*\n**Available APY:** {fmt_bonus(f'{usd_save * 365:.1f}%/yr', _vs_bonus)}\n`/bank savings deposit` to start earning",
                True,
            )

        if _wt_mult != 1.0:
            from services.bottleneck import percentile_label
            _arrow = "📈" if _wt_mult > 1.0 else "📉"
            _b.field(
                f"⚖️ Wealth Bottleneck {_arrow}",
                (
                    f"You sit at the **{percentile_label(_pctile)}** of the "
                    f"leaderboard (≈ ${_user_nw:,.0f}) - bottleneck "
                    f"multiplier **x{_wt_mult:.2f}**.\n"
                    f"Effective APY: **{_eff_save * 365:.1f}%/yr** "
                    f"({_eff_save * 100:.3f}%/day)\n"
                    f"`,help bottleneck` for the full curve."
                ),
                False,
            )

        embed = (
            _b
            .field(
                "📊 Pool Stats",
                f"💵 USD: ${usd_total_dep:,.2f} dep / ${usd_total_bor:,.2f} bor · {utilization_str(usd_util)}",
                True,
            )
            .footer("Interest auto-compounds hourly  •  /bank savings rates for full rate curves")
            .timestamp()
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _market_info(self, guild_id: int, symbol: str) -> tuple[float, float, float, float, float]:
        total_dep_raw, total_bor_raw = await self.bot.db.get_savings_totals(guild_id, symbol)
        total_dep = to_human(total_dep_raw)
        total_bor = to_human(total_bor_raw)
        borrow_daily, savings_daily, utilization = compute_rates(total_dep, total_bor)
        return borrow_daily, savings_daily, utilization, total_dep, total_bor

    @savings.command(name="deposit", aliases=["save"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def save_usd(self, ctx: DiscoContext, amount: str) -> None:
        """Deposit USD into savings and earn dynamic interest."""
        amount_raw: int
        if amount.lower() == "all":
            fresh = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            if not fresh or int(fresh.get("wallet", 0) or 0) <= 0:
                await ctx.reply_error("Your wallet is empty.")
                return
            amount_raw = int(fresh["wallet"])
            amount = to_human(amount_raw)
        else:
            try:
                amount = parse_amount(amount)[0]
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
                return
            if not math.isfinite(amount) or amount <= 0:
                await ctx.reply_error("Amount must be a positive number.")
                return
            if amount < to_human(_M["min_deposit"]):
                await ctx.reply_error(f"Minimum deposit is **${to_human(_M['min_deposit']):,.2f}**.")
                return
            if to_human(ctx.user_row["wallet"]) < amount:
                await ctx.reply_error(
                    f"You only have **${to_human(ctx.user_row['wallet']):,.2f}** in your wallet."
                )
                return
            amount_raw = to_raw(amount)
        if not math.isfinite(amount) or amount <= 0:
            await ctx.reply_error("Amount must be a positive number.")
            return
        if amount < to_human(_M["min_deposit"]):
            await ctx.reply_error(f"Minimum deposit is **${to_human(_M['min_deposit']):,.2f}**.")
            return

        borrow_daily, savings_daily, utilization, total_dep, total_bor = \
            await self._market_info(ctx.guild_id, "USD")
        _annual_yield = amount * savings_daily * 365

        # Confirmation
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(
            embed=(
                card("💰 Confirm Savings Deposit", color=C_AMBER)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📥 Amount",          f"**${amount:,.2f}**",                                    True)
                .field("📈 APY",             f"**{savings_daily * 365:.1f}%/yr**",                     True)
                .field("📅 Est. Annual",     f"**+${_annual_yield:,.2f}**/yr",                         True)
                .field("", f"⚠️ Funds locked in savings vault  ·  Expires {fmt_ts(int(time.time() + 30))}", False)
                .build()
            ),
            view=view,
            mention_author=False,
        )
        confirmed = await view.wait_result()
        if not confirmed:
            await conf_msg.edit(embed=card("", description="Deposit cancelled.").color(C_SUBTLE).build(), view=None)
            return

        async with ctx.db.atomic():
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -amount_raw)
            await ctx.db.savings_deposit(ctx.author.id, ctx.guild_id, "USD", amount_raw)
        dep_after = await ctx.db.get_savings_deposit(ctx.author.id, ctx.guild_id, "USD")
        new_balance = to_human(dep_after["amount"]) if dep_after else amount
        _annual_yield2 = new_balance * savings_daily * 365
        await conf_msg.edit(
            embed=(
                card("💰 Savings Deposit Confirmed")
                .color(C_SUCCESS)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📥 Deposited",       f"**${amount:,.2f}**\n💰 Vault: **${new_balance:,.2f}**", True)
                .field("📈 Yield",           f"**{savings_daily * 365:.1f}%/yr** ({savings_daily * 100:.3f}%/day)\n+${new_balance * savings_daily:,.4f}/day · +${_annual_yield2:,.2f}/yr", True)
                .field("🏦 Utilization",     utilization_str(utilization),                              True)
                .footer("/bank savings withdraw to access funds")
                .timestamp()
                .build()
            ),
            view=None,
        )

    @savings.command(name="withdraw", aliases=["unsave"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def unsave_usd(self, ctx: DiscoContext, amount: str = "all") -> None:
        """Withdraw USD from savings back to your wallet."""
        deposit = await ctx.db.get_savings_deposit(ctx.author.id, ctx.guild_id, "USD")
        if not deposit or deposit["amount"] <= 0:
            await ctx.reply_error("You have no USD in savings.")
            return

        deposit_h = to_human(deposit["amount"])
        if amount.lower() == "all":
            withdraw = deposit_h
        else:
            try:
                withdraw = parse_amount(amount)[0]
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
                return
            if not math.isfinite(withdraw) or withdraw <= 0:
                await ctx.reply_error("Amount must be a positive number.")
                return

        withdraw = min(withdraw, deposit_h)
        new_balance = deposit_h - withdraw
        usd_vault_remaining = f"**${new_balance:,.2f}**" if new_balance >= 0.001 else "*Account closed*"

        # Confirmation
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(
            embed=(
                card("💰 Confirm Savings Withdrawal", color=C_AMBER)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📤 Amount",          f"**${withdraw:,.2f}**",   True)
                .field("💰 Vault After",     usd_vault_remaining,        True)
                .field("", f"⚠️ Withdrawing from savings vault  ·  Expires {fmt_ts(int(time.time() + 30))}", False)
                .build()
            ),
            view=view,
            mention_author=False,
        )
        confirmed = await view.wait_result()
        if not confirmed:
            await conf_msg.edit(embed=card("", description="Withdrawal cancelled.").color(C_SUBTLE).build(), view=None)
            return

        async with ctx.db.atomic():
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(withdraw))
            if new_balance < 0.001:
                await ctx.db.delete_savings_deposit(ctx.author.id, ctx.guild_id, "USD")
            else:
                await ctx.db.upsert_savings_deposit(
                    ctx.author.id, ctx.guild_id, "USD", to_raw(new_balance), deposit["last_interest"]
                )

        await conf_msg.edit(
            embed=(
                card("💰 Savings Withdrawal")
                .color(C_SUCCESS)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📤 Withdrawn",      f"**${withdraw:,.2f}**", True)
                .field("💳 Wallet",         "Updated",                True)
                .field("💰 Vault",          usd_vault_remaining,      True)
                .footer("Funds returned to your wallet  •  /bank savings deposit to re-save")
                .timestamp()
                .build()
            ),
            view=None,
        )

    @savings.command(name="rates", aliases=["marketrates", "apy"])
    @guild_only
    async def rates(self, ctx: DiscoContext) -> None:
        """Show current borrow and savings rates for the USD pool."""
        usd_borrow, usd_save, usd_util, usd_dep, usd_bor = \
            await self._market_info(ctx.guild_id, "USD")

        _b = (
            card("📊 Market Rates")
            .description(
                "Rates follow a **utilization kink model**. Above 80% utilization, rates spike sharply."
            )
            .color(C_NAVY)
        )

        def _rate_row(borrow: float, save: float, util: float,
                      dep: float, bor: float, unit: str = "$") -> str:
            return (
                f"Util: {utilization_str(util)}\n"
                f"Borrow: **{borrow * 365:.2f}%/yr**\n"
                f"Save: **{save * 365:.2f}%/yr**\n"
                f"Pool: {unit}{dep:,.2f} / {unit}{bor:,.2f}"
            )

        _b.field("💵 USD Pool", _rate_row(usd_borrow, usd_save, usd_util, usd_dep, usd_bor), True)

        m = Config.SAVINGS_RATE_MODEL
        benchmarks = [0.0, 0.50, 0.80, 0.90, 1.0]
        lines = []
        for u in benchmarks:
            b, s, _ = compute_rates(1000.0, 1000.0 * u, model=m)
            marker = " ◀" if u == m["optimal_utilization"] else ""
            lines.append(f"`{u:>5.0%}` → B:**{b*100:.2f}%** S:**{s*100:.2f}%**{marker}")
        _b.field("📈 Rate Curve", "\n".join(lines), True)

        embed = _b.footer("/bank savings deposit to save  •  /bank loan borrow to take a loan").build()
        await ctx.reply(embed=embed, mention_author=False)

    # ════════════════════════════════════════════════════════════════════════
    #  LOAN SUBGROUP
    # ════════════════════════════════════════════════════════════════════════

    @bank.group(name="loan", invoke_without_command=True)
    @guild_only
    @no_bots
    async def loan(self, ctx: DiscoContext) -> None:
        """Loan commands. Subcommands: borrow, repay, status"""
        if await suggest_subcommand(ctx, self.loan):
            return
        await ctx.reply_error(
            "Choose a subcommand: `borrow` `repay` `status`\n"
            f"Example: `{ctx.prefix}bank loan borrow 500`"
        )

    @loan.command(name="borrow")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def borrow(self, ctx: DiscoContext, amount: float) -> None:
        """Borrow USD using your bank as collateral."""
        if not math.isfinite(amount):
            await ctx.reply_error("Amount must be a finite number.")
            return
        if amount <= 0:
            await ctx.reply_error("Amount must be positive.")
            return
        lock_key = (ctx.author.id, ctx.guild_id)
        lock = self._loan_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("A borrow/repay is already in progress.")
            return
        async with lock:
            await self._do_borrow(ctx, amount)

    async def _do_borrow(self, ctx: DiscoContext, amount: float) -> None:
        existing = await ctx.db.get_loan(ctx.author.id, ctx.guild_id)
        if existing:
            await ctx.reply_error(
                f"You already have an active loan of **${to_human(existing['outstanding']):,.2f}**. "
                "Use `/bank loan repay all` first."
            )
            return

        collateral = amount / _L["MAX_LTV"]

        fresh_row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        if to_human(fresh_row["bank"]) < collateral:
            await ctx.reply_error(
                f"You need **${collateral:,.2f}** in your bank as collateral "
                f"(you have **${to_human(fresh_row['bank']):,.2f}**)."
            )
            return

        async with ctx.db.atomic():
            await ctx.db.update_bank(ctx.author.id, ctx.guild_id, to_raw(-collateral))
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(amount))
            await ctx.db.upsert_loan(
                ctx.author.id, ctx.guild_id,
                principal=to_raw(amount),
                outstanding=to_raw(amount),
                collateral=to_raw(collateral),
                last_interest=datetime.datetime.now(datetime.timezone.utc),
            )

        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "LEND",
            symbol_in="COLLATERAL", amount_in=to_raw(collateral),
            symbol_out="USD", amount_out=to_raw(amount),
            price_at=None,
            network="usd",
        )

        total_dep, total_bor = await ctx.db.get_savings_totals(ctx.guild_id, "USD")
        borrow_daily, savings_daily, utilization = compute_rates(total_dep, total_bor)
        ltv_pct = amount / collateral * 100
        daily_interest = amount * borrow_daily
        # Compute savings interest bonus for display
        _job_row = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        _job_cfg = Config.JOBS.get(_job_row["job_id"] if _job_row else "HOMELESS", Config.JOBS["HOMELESS"])
        _int_bonus = _job_cfg.get("perks", {}).get("interest_bonus", 0.0)
        hashstone = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
        _int_bonus += _item_stat(hashstone, "interest_bonus")
        vaultstone = await ctx.db.get_vaultstone(ctx.author.id, ctx.guild_id)
        _int_bonus += _vaultstone_stat(vaultstone, "interest_bonus")
        _sav_rate_str = fmt_bonus(f"{savings_daily*365:.1f}%/yr", _int_bonus) if savings_daily > 0 else ""
        _sav_line = f"\n💰 Savers earn: {_sav_rate_str}" if _sav_rate_str else ""
        embed = (
            card("💼 Loan Approved")
            .color(C_INFO)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("💳 Loan",     f"**${amount:,.2f}** → wallet\n🔒 Collateral: ${collateral:,.2f}\n📊 LTV: **{ltv_pct:.1f}%**", True)
            .field("📈 Rates",    f"**{borrow_daily*365:.1f}%/yr** ({borrow_daily*100:.3f}%/day)\n💸 Est: **${daily_interest:,.4f}**/day{_sav_line}", True)
            .field("⚠️ Liq. At",  f"LTV ≥ **{_L['LIQUIDATION_THRESHOLD']*100:.0f}%**",                 True)
            .footer("Interest accrues hourly  •  /bank loan repay  •  /bank loan status")
            .timestamp()
            .build()
        )
        set_tx(embed, ctx.guild_id, tx_hash, "Use /bank loan repay to reduce your loan  |  /bank loan status to view")
        await ctx.reply(embed=embed, mention_author=False)
        await ctx.bot.bus.publish("loan_opened", guild=ctx.guild, user=ctx.author,
            amount=amount, collateral=collateral, tx_hash=tx_hash)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "loan", amount, symbol="USD", amount=amount)

    @loan.command(name="repay")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def repay(self, ctx: DiscoContext, amount: str = "all") -> None:
        """Repay your loan. Usage: /bank loan repay [amount|all]"""
        lock_key = (ctx.author.id, ctx.guild_id)
        lock = self._loan_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("A borrow/repay is already in progress.")
            return
        async with lock:
            await self._do_repay(ctx, amount)

    async def _do_repay(self, ctx: DiscoContext, amount: str) -> None:
        loan = await ctx.db.get_loan(ctx.author.id, ctx.guild_id)
        if not loan:
            await ctx.reply_error("You have no active loan.")
            return

        outstanding_h = to_human(loan["outstanding"])
        collateral_h = to_human(loan["collateral"])
        if amount.lower() == "all":
            repay_amt = outstanding_h
        else:
            try:
                repay_amt = parse_amount(amount)[0]
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
                return
            if not math.isfinite(repay_amt):
                await ctx.reply_error("Amount must be a finite number.")
                return

        repay_amt = min(repay_amt, outstanding_h)
        if repay_amt <= 0:
            await ctx.reply_error("Amount must be positive.")
            return

        fresh_row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        if to_human(fresh_row["wallet"]) < repay_amt:
            await ctx.reply_error(
                f"You only have **${to_human(fresh_row['wallet']):,.2f}** but need **${repay_amt:,.2f}**."
            )
            return

        new_outstanding = outstanding_h - repay_amt
        frac_repaid = repay_amt / outstanding_h
        collateral_returned = collateral_h * frac_repaid

        if new_outstanding <= 0.001:
            async with ctx.db.atomic():
                await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(-repay_amt))
                await ctx.db.delete_loan(ctx.author.id, ctx.guild_id)
                await ctx.db.update_bank(ctx.author.id, ctx.guild_id, loan["collateral"])
            repay_tx = await ctx.db.log_tx(
                ctx.guild_id, ctx.author.id, "REPAY",
                symbol_in="USD", amount_in=to_raw(repay_amt),
                symbol_out="COLLATERAL", amount_out=loan["collateral"],
                network="usd",
            )
            await ctx.reply(
                embed=(
                    card("💸 Loan Fully Repaid")
                    .color(C_SUCCESS)
                    .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                    .description("Your loan has been cleared. Collateral has been returned to your bank.")
                    .field("📤 Amount Repaid",     f"**${repay_amt:,.2f}**",               True)
                    .field("🔓 Collateral Returned", f"**${collateral_h:,.2f}**  → bank", True)
                    .field("📋 Loan Status",        "**Closed**",                           True)
                    .footer(f"tx: {repay_tx}")
                    .timestamp()
                    .build()
                ),
                mention_author=False,
            )
            await ctx.bot.bus.publish("loan_repaid", guild=ctx.guild, user=ctx.author,
                amount_paid=repay_amt, remaining=0.0)
        else:
            remaining_collateral = collateral_h - collateral_returned
            async with ctx.db.atomic():
                await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(-repay_amt))
                await ctx.db.update_bank(ctx.author.id, ctx.guild_id, to_raw(collateral_returned))
                await ctx.db.upsert_loan(
                    ctx.author.id, ctx.guild_id,
                    loan["principal"], to_raw(new_outstanding),
                    to_raw(remaining_collateral), time.time(),
                )
            repay_tx = await ctx.db.log_tx(
                ctx.guild_id, ctx.author.id, "REPAY",
                symbol_in="USD", amount_in=to_raw(repay_amt),
                symbol_out="COLLATERAL", amount_out=to_raw(collateral_returned),
                network="usd",
            )
            new_ltv = new_outstanding / remaining_collateral * 100 if remaining_collateral > 0 else 0
            embed = (
                card("💸 Partial Repayment")
                .color(C_SUCCESS)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📤 Repaid",            f"**${repay_amt:,.2f}**",            True)
                .field("📋 Outstanding",        f"**${new_outstanding:,.2f}**",      True)
                .field("🔓 Collateral Freed",   f"**${collateral_returned:,.2f}**",  True)
                .field("📊 New LTV",            f"**{new_ltv:.1f}%**",               True)
                .footer("/bank loan repay all to fully clear the loan")
                .timestamp()
                .build()
            )
            set_tx(embed, ctx.guild_id, repay_tx)
            await ctx.reply(embed=embed, mention_author=False)
            await ctx.bot.bus.publish("loan_repaid", guild=ctx.guild, user=ctx.author,
                amount_paid=repay_amt, remaining=new_outstanding)

    @loan.command(name="status", aliases=["debt", "info"])
    @guild_only
    @no_bots
    @ensure_registered
    async def loanstatus(self, ctx: DiscoContext) -> None:
        """Show your current loan details."""
        loan = await ctx.db.get_loan(ctx.author.id, ctx.guild_id)
        if not loan:
            await ctx.reply_error("You have no active loan.")
            return

        total_dep, total_bor = await ctx.db.get_savings_totals(ctx.guild_id, "USD")
        borrow_daily, savings_daily, _ = compute_rates(total_dep, total_bor)
        _loan_out_h = to_human(loan["outstanding"])
        _loan_col_h = to_human(loan["collateral"])
        _loan_pri_h = to_human(loan["principal"])
        ltv = _loan_out_h / _loan_col_h * 100 if loan["collateral"] > 0 else 0
        danger = ltv >= 80
        color = C_ERROR if danger else C_INFO
        daily_interest = _loan_out_h * borrow_daily

        _b = (
            card("💼 Loan Status")
            .color(color)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("💳 Loan",       f"Principal: **${_loan_pri_h:,.2f}**\nOutstanding: **${_loan_out_h:,.2f}**\n🔒 Collateral: ${_loan_col_h:,.2f}", True)
            .field("📊 Risk",       f"LTV: **{ltv:.1f}%** {'⚠️ HIGH' if danger else ''}\n⚠️ Liq. at ≥ {_L['LIQUIDATION_THRESHOLD']*100:.0f}%", True)
            .field("📈 Rates",      f"**{borrow_daily*365:.1f}%/yr** (dynamic)\n💸 Est: **${daily_interest:,.4f}**/day", True)
        )
        if savings_daily > 0:
            _b.field("💡 Savers Earn", f"**{savings_daily*365:.1f}%/yr** · `/bank savings deposit`", True)
        embed = _b.footer("/bank loan repay [amount|all] to reduce your loan").timestamp().build()
        await ctx.reply(embed=embed, mention_author=False)

    # ════════════════════════════════════════════════════════════════════════
    #  BACKGROUND TASKS
    # ════════════════════════════════════════════════════════════════════════

    @tasks.loop(minutes=30)
    async def savings_interest_tick(self) -> None:
        from services.bottleneck import apply_bottleneck, CreditKind
        for guild in self.bot.guilds:
            _sav_gs = await self.bot.db.get_guild_settings(guild.id)
            _savings_mult = float(_sav_gs.get("savings_multiplier") or 1.0)
            for symbol in ("USD", "SUN"):
                deposits = await self.bot.db.get_all_savings_deposits(guild.id, symbol)
                if not deposits:
                    continue

                total_dep, total_bor = await self.bot.db.get_savings_totals(guild.id, symbol)
                _, savings_daily, _ = compute_rates(total_dep, total_bor)
                hourly_rate = savings_daily / 24.0

                for row in deposits:
                    _li = row["last_interest"]
                    _li_ts = _li.timestamp() if hasattr(_li, 'timestamp') else _li
                    elapsed_hours = (time.time() - _li_ts) / 3600.0
                    # Build a multiplier from elapsed time + bonuses.
                    # Both are independent of the deposit amount, so computing them from the
                    # snapshot is correct. The live DB balance gets multiplied, not stale+delta.
                    base_factor = (1 + hourly_rate) ** max(elapsed_hours, 0)
                    interest_delta_rate = base_factor - 1.0  # e.g. 0.0024 for ~0.24% growth
                    job = await self.bot.db.get_user_job(row["user_id"], guild.id)
                    job_cfg = Config.JOBS.get(job.get("job_id", "HOMELESS"), Config.JOBS["HOMELESS"])
                    interest_bonus = job_cfg.get("perks", {}).get("interest_bonus", 0.0)
                    hashstone = await self.bot.db.get_hashstone(row["user_id"], guild.id)
                    interest_bonus += _item_stat(hashstone, "interest_bonus")
                    vaultstone = await self.bot.db.get_vaultstone(row["user_id"], guild.id)
                    interest_bonus += _vaultstone_stat(vaultstone, "interest_bonus")
                    if interest_bonus > 0:
                        interest_delta_rate *= (1.0 + interest_bonus)
                    interest_delta_rate *= _savings_mult
                    # Apex Mastery: Vault Interest (econ.bank_yield) +
                    # Compounding (econ.interest_bonus) layer on top of
                    # job / stone / guild-mult bonuses. Read passives
                    # per-user-per-tick; cost is a single SELECT and the
                    # tick is already O(N depositors).
                    try:
                        from services import mastery as _mastery_b
                        _mp = await _mastery_b.passives(
                            self.bot.db, row["user_id"], guild.id,
                        )
                        _mb = float(_mp.get("econ.bank_yield") or 0.0)
                        _mb += float(_mp.get("econ.interest_bonus") or 0.0)
                        if _mb > 0:
                            interest_delta_rate *= (1.0 + _mb)
                    except Exception:
                        pass
                    # Wealth Bottleneck: scale this tick's interest gain by
                    # leaderboard rank. Drag from rich savers feeds the
                    # per-guild pool; bottom of the leaderboard gets a USD
                    # wallet top-up sourced from the same pool. Principal
                    # is never touched - only the gross interest accrued
                    # since the previous tick is taxed.
                    _principal_raw = int(row["amount"])
                    _gross_interest_raw = int(_principal_raw * interest_delta_rate)
                    _bn_savings = await apply_bottleneck(
                        self.bot.db,
                        uid=int(row["user_id"]), gid=guild.id,
                        gross_raw=_gross_interest_raw,
                        kind=CreditKind.SAVINGS_INTEREST,
                        symbol=symbol if symbol in ("USD", "DSD") else symbol,
                    )
                    # The savings deposit only ever holds ``symbol`` units,
                    # so the deposit grows by net_credit_raw and the boost
                    # (always USD) is pushed to the wallet separately.
                    if _principal_raw > 0:
                        net_multiplier = (
                            1.0 + (_bn_savings.net_credit_raw / _principal_raw)
                        )
                    else:
                        net_multiplier = 1.0
                    now_dt = datetime.datetime.now(datetime.timezone.utc)
                    new_amount = await self.bot.db.apply_savings_interest(
                        row["user_id"], guild.id, symbol, net_multiplier, now_dt
                    )
                    if new_amount is None:
                        continue  # deposit was withdrawn before tick could apply interest
                    if symbol == "USD" and _bn_savings.boost_wallet_raw > 0:
                        await self.bot.db.update_wallet(
                            row["user_id"], guild.id,
                            int(_bn_savings.boost_wallet_raw),
                        )
                    elif symbol != "USD" and _bn_savings.boost_wallet_raw > 0:
                        # Non-USD savings (e.g. SUN) get the USD bottleneck
                        # boost credited to the USD wallet, not to the
                        # foreign deposit. Same convention as PoS rewards.
                        await self.bot.db.update_wallet(
                            row["user_id"], guild.id,
                            int(_bn_savings.boost_wallet_raw),
                        )
                    _VS_CFG = Config.SHOP_ITEMS.get("vaultstone", {})
                    if vaultstone and vaultstone["level"] < _VS_CFG.get("max_level", 50) and new_amount > 0:
                        base_xp = _VS_CFG.get("xp_per_interest", 10.0)
                        # Use live post-interest balance for XP scale, not the stale snapshot.
                        if symbol == "USD":
                            _savings_usd = to_human(new_amount)
                        else:
                            _tp_row = await self.bot.db.get_price(symbol, guild.id)
                            _tp = float(_tp_row["price"]) if _tp_row else 0.0
                            _savings_usd = to_human(new_amount) * _tp
                        # No minimum floor  -  zero savings = zero XP (fixes bug where XP_SCALE_MIN
                        # caused XP gain even with no savings)
                        if _savings_usd > 0:
                            xp_scale = min(Config.XP_SCALE_MAX, _savings_usd / Config.XP_SAVINGS_REFERENCE_USD)
                            xp_gain = base_xp * xp_scale
                            # Atomic delta-add  -  concurrent XP grants from mining/staking won't be wiped.
                            xp_result = await self.bot.db.add_vaultstone_xp(row["user_id"], guild.id, xp_gain)
                            if xp_result:
                                live_xp, live_level = xp_result
                                capped_xp = cap_xp(live_xp, live_level, _VS_CFG)
                                if capped_xp < live_xp:
                                    # XP exceeded level cap  -  clamp it
                                    await self.bot.db.update_vaultstone_xp(row["user_id"], guild.id, capped_xp, live_level)
                                await notify_item_levelup_ready(self.bot, row["user_id"], guild, "vaultstone", live_xp - xp_gain, live_xp, live_level, vaultstone["staked_amount"])

                    # ── Liquidity Mining: emit VTR/DSY to USD/DSD savers ─────────────
                    # Mirrors Vantor liquidity incentives: depositing USD earns VTR tokens,
                    # depositing DSD earns DSY tokens -- proportional to deposit size and time.
                    _sm_lm_map = {"USD": "VTR"}  # DSD savings not yet active
                    _lm_symbol = _sm_lm_map.get(symbol)
                    if _lm_symbol and _lm_symbol in Config.SAFETY_MODULE:
                        _lm_cfg = Config.SAFETY_MODULE[_lm_symbol]
                        _lm_daily = _lm_cfg.get("lm_daily", 0.0)
                        _deposit_usd = to_human(new_amount)
                        if _deposit_usd > 0 and _lm_daily > 0:
                            _lm_price_row = await self.bot.db.fetch_one(
                                "SELECT price FROM crypto_prices WHERE guild_id=$1 AND symbol=$2",
                                guild.id, _lm_symbol,
                            )
                            _lm_price = float(_lm_price_row["price"]) if _lm_price_row else 0.0
                            if _lm_price > 0:
                                _lm_usd_value = _deposit_usd * _lm_daily * elapsed_hours / 24.0
                                _lm_amount_h = _lm_usd_value / _lm_price
                                if _lm_amount_h > 0:
                                    _lm_raw = to_raw(_lm_amount_h)
                                    await self.bot.db.update_wallet_holding(
                                        row["user_id"], guild.id,
                                        _lm_cfg["network"], _lm_symbol, _lm_raw,
                                    )

        pulse("savings_interest")

    @savings_interest_tick.before_loop
    async def before_savings_tick(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def loan_interest_tick(self) -> None:
        for guild in self.bot.guilds:
            loans = await self.bot.db.get_all_loans(guild.id)
            if not loans:
                continue

            total_dep, total_bor = await self.bot.db.get_savings_totals(guild.id, "USD")
            borrow_daily, _, _ = compute_rates(total_dep, total_bor)
            hourly_rate = borrow_daily / 24.0

            for loan in loans:
                _li = loan["last_interest"]
                _li_ts = _li.timestamp() if hasattr(_li, 'timestamp') else _li
                elapsed_hours = (time.time() - _li_ts) / 3600
                multiplier = (1 + hourly_rate) ** max(elapsed_hours, 0)

                # Atomically apply compound interest to the live row.
                # Returns None if the loan was repaid between the initial fetch and now.
                now_dt = datetime.datetime.now(datetime.timezone.utc)
                result = await self.bot.db.apply_loan_interest(
                    loan["user_id"], guild.id, multiplier, now_dt
                )
                if result is None:
                    continue  # loan was repaid concurrently  -  skip
                new_outstanding, live_collateral = result

                ltv = new_outstanding / live_collateral if live_collateral > 0 else float("inf")

                if ltv >= _L["LIQUIDATION_THRESHOLD"]:
                    # Check for Yield Guard  -  absorbs one liquidation event
                    _yg_used = await self.bot.db.use_yield_guard(loan["user_id"], guild.id)
                    if _yg_used:
                        # Guard consumed: reset interest timer only (outstanding already updated atomically)
                        await self.bot.db.upsert_loan(
                            loan["user_id"], guild.id,
                            loan["principal"], new_outstanding,
                            live_collateral, time.time(),
                        )
                        await self.bot.bus.publish(
                            "yield_guard_used",
                            guild=guild,
                            user_id=loan["user_id"],
                            ltv=ltv,
                        )
                        continue
                    # Apply liquidation penalty: burn penalty% of collateral
                    penalty = _L["LIQUIDATION_PENALTY"]
                    collateral_after_penalty = live_collateral * (1 - penalty)
                    excess = collateral_after_penalty - new_outstanding
                    if excess > 0:
                        await self.bot.db.update_bank(loan["user_id"], guild.id, excess)
                    await self.bot.db.delete_loan(loan["user_id"], guild.id)
                    liq_tx = await self.bot.db.log_tx(
                        guild.id, loan["user_id"], "LIQUIDATION",
                        symbol_in="COLLATERAL", amount_in=collateral_after_penalty,
                        symbol_out="USD", amount_out=new_outstanding,
                        network="usd",
                    )
                    await self.bot.bus.publish(
                        "loan_liquidated",
                        guild=guild,
                        user_id=loan["user_id"],
                        collateral=collateral_after_penalty,
                        outstanding=new_outstanding,
                        tx_hash=liq_tx,
                    )
                    await _whale.check(self.bot, guild, loan["user_id"], "liquidation", collateral_after_penalty, symbol="USD", amount=collateral_after_penalty)
                # else: interest already applied atomically by apply_loan_interest above
        pulse("loan_interest")

    @loan_interest_tick.before_loop
    async def before_loan_tick(self) -> None:
        await self.bot.wait_until_ready()

    # ════════════════════════════════════════════════════════════════════════
    #  HIDDEN PREFIX ALIASES (backward compatibility)
    # ════════════════════════════════════════════════════════════════════════

    @commands.command(name="deposit", hidden=True, aliases=["dep"])
    @guild_only
    @no_bots
    @ensure_registered
    async def _dep_alias(self, ctx: DiscoContext, amount: str) -> None:
        await self._usd_deposit(ctx, amount)

    @commands.command(name="withdraw", hidden=True, aliases=["with"])
    @guild_only
    @no_bots
    @ensure_registered
    async def _with_alias(self, ctx: DiscoContext, amount: str) -> None:
        await self._usd_withdraw(ctx, amount)

    @commands.command(name="transfer", hidden=True, aliases=["give", "pay"])
    @guild_only
    @no_bots
    @ensure_registered
    async def _transfer_alias(self, ctx: DiscoContext, target: discord.Member = None, amount: float = 0) -> None:
        if target is None:
            await ctx.reply_error(f"Usage: `{ctx.prefix}give @user <amount>`  -  e.g. `{ctx.prefix}give @Alice 100`")
            return
        if amount <= 0:
            await ctx.reply_error(f"Usage: `{ctx.prefix}give @user <amount>`  -  e.g. `{ctx.prefix}give {target.mention} 100`")
            return
        await self.transfer(ctx, target=target, amount=amount)

    @commands.command(name="move", hidden=True, aliases=["mv"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def _move_alias(self, ctx: DiscoContext, amount: str = "", token: str = "", from_loc: str = "", to_loc: str = "") -> None:
        if not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}mv <amount|all> <token> <from> <to>`\n"
                f"Or: `{ctx.prefix}mv everything <from> <to>`\n"
                f"Example: `{ctx.prefix}mv 100 USD cash bank`"
            )
            return
        if amount.lower() == "everything" and token and from_loc:
            await self._move_everything(ctx, from_loc=token, to_loc=from_loc)
            return
        if not token or not from_loc or not to_loc:
            await ctx.reply_error("Usage: `.move <amount|all> <token> <from> <to>` or `.move everything <from> <to>`")
            return
        await self.move(ctx, amount=amount, token=token, from_loc=from_loc, to_loc=to_loc)

    @commands.command(name="save", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def _save_alias(self, ctx: DiscoContext, amount: str) -> None:
        await self.save_usd(ctx, amount=amount)

    @commands.command(name="unsave", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def _unsave_alias(self, ctx: DiscoContext, amount: str = "all") -> None:
        await self.unsave_usd(ctx, amount=amount)

    @commands.command(name="rates", hidden=True)
    @guild_only
    async def _rates_alias(self, ctx: DiscoContext) -> None:
        await self.rates(ctx)

    @commands.command(name="borrow", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def _borrow_alias(self, ctx: DiscoContext, amount: float) -> None:
        await self.borrow(ctx, amount=amount)

    @commands.command(name="repay", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def _repay_alias(self, ctx: DiscoContext, amount: str = "all") -> None:
        await self.repay(ctx, amount=amount)



    _LEADERBOARD_PER_PAGE = 10
    _LB_MEDALS = ["🥇", "🥈", "🥉"]
    _LB_RANK_TITLES = ["Champion", "Runner-up", "Third Place"]
    _LB_RANK_BARS = ["████████████████████", "██████████████████░░", "████████████████░░░░"]

    def _lb_rank_prefix(self, rank: int, name: str, value_str: str, is_top3_detail: bool = True, is_caller: bool = False) -> str:
        """Format a leaderboard entry with special treatment for ranks 1-3."""
        you = " ◄ **you**" if is_caller else ""
        if rank < 3 and is_top3_detail:
            medal = self._LB_MEDALS[rank]
            title = self._LB_RANK_TITLES[rank]
            bar = self._LB_RANK_BARS[rank]
            return f"{medal} **{name}**  -  {value_str}{you}\n\u2003`{bar}` *{title}*"
        elif rank < 3:
            return f"{self._LB_MEDALS[rank]} **{name}**  -  {value_str}{you}"
        else:
            return f"`{rank + 1}.` **{name}**  -  {value_str}{you}"

    def _lb_caller_info(self, user_ids: list[int], caller_id: int) -> tuple[int | None, bool]:
        """Find the caller's rank (0-indexed) in the full ranked list. Returns (rank, found)."""
        for i, uid in enumerate(user_ids):
            if uid == caller_id:
                return i, True
        return None, False

    def _lb_rank_footer(self, base_footer: str, caller_rank: int | None) -> str:
        """Append caller rank note to a footer string."""
        if caller_rank is not None:
            return f"{base_footer}\n📍 Your rank: #{caller_rank + 1}"
        return f"{base_footer}\n📍 You are not ranked yet"

    @commands.hybrid_command(name="leaderboard", aliases=["lb", "top"])
    @guild_only
    async def leaderboard(self, ctx: DiscoContext, *, category: str = "") -> None:
        """Server leaderboard.

        Built-in (this cog): trading, gambling, work, staking, hashrate, lp,
        streaks, rugpull, eat, token <SYM>.
        Game subcommands (delegated): buddy, battles, arena, fish, biggest,
        delve, farm, craft, achievements, level.
        """
        fl = category.lower().strip()
        if fl in ("hashrate", "hash", "mining", "miners"):
            await self._lb_hashrate(ctx)
        elif fl in ("trading", "trades", "traders"):
            await self._lb_trading(ctx)
        elif fl in ("gambling", "gamble", "gamblers"):
            await self._lb_gambling(ctx)
        elif fl in ("work", "workers", "working"):
            await self._lb_work(ctx)
        elif fl in ("staking", "stake", "stakers"):
            await self._lb_staking(ctx)
        elif fl in ("lp", "liquidity", "pools", "providers"):
            await self._lb_lp(ctx)
        elif fl in ("streaks", "streak", "daily"):
            await self._lb_streaks(ctx)
        elif fl in ("rugpull", "rug", "rugs", "king"):
            await self._lb_rugpull(ctx)
        elif fl in ("eat", "eats", "eattherich", "classwar", "rich"):
            await self._lb_eat(ctx)
        elif fl in ("level", "levels", "rank", "ranks", "xp", "chat", "chatxp"):
            await self._delegate_lb(ctx, "ChatLeveling", "render_leaderboard")
        elif fl in ("buddy", "buddies", "pet", "pets"):
            await self._delegate_lb(ctx, "Buddy", "buddy_leaderboard")
        elif fl in ("battles", "buddybattles", "blb", "battleboard"):
            await self._delegate_lb(ctx, "Buddy", "buddy_battles")
        elif fl in ("arena", "arenas"):
            await self._delegate_lb(ctx, "Buddy", "buddy_arena_lb")
        elif fl in ("delve", "dungeon", "delvers", "delving"):
            await self._delegate_lb(ctx, "Delve", "delve_lb")
        elif fl in ("craft", "crafting", "forge", "smith", "smithing"):
            await self._delegate_lb(ctx, "Crafting", "craft_lb")
        elif fl in ("fish", "fishing", "fishers"):
            await self._delegate_lb(ctx, "Fishing", "fish_lb", arg="payout")
        elif fl in ("biggest", "trophy", "trophies", "weight"):
            await self._delegate_lb(ctx, "Fishing", "fish_lb", arg="biggest")
        elif fl in ("farm", "farming", "farmers"):
            await self._delegate_lb(ctx, "Farming", "farm_lb")
        elif fl in ("achievements", "achievement", "achv", "badges", "badge"):
            await self._delegate_lb(
                ctx, "Achievements", "achievements_leaderboard",
            )
        elif fl in ("help", "categories", "?"):
            cats = (
                "**Available leaderboard categories:**\n\n"
                "**🏆 Wealth + economy**\n"
                "🏆 `lb`  -  Net worth (default)\n"
                "📊 `lb trading`  -  Trading P&L\n"
                "🎰 `lb gambling`  -  Gambling profit\n"
                "🔨 `lb work`  -  Work earnings\n"
                "🔐 `lb staking`  -  Staked value\n"
                "⛏️ `lb hashrate`  -  Mining hashrate\n"
                "🌊 `lb lp`  -  Liquidity provider value\n"
                "🔥 `lb streaks`  -  Daily claim streaks\n"
                "🪤 `lb rugpull`  -  Rugpull title holders\n"
                "🍽️ `lb eat`  -  Eat the Rich net wealth\n"
                "🪙 `lb <TOKEN>`  -  Holdings of a specific token\n\n"
                "**🎮 Games + minigames**\n"
                "🐶 `lb buddy`  -  Top buddies by level / XP\n"
                "⚔️ `lb battles`  -  Top buddies by battle wins\n"
                "🏟️ `lb arena`  -  Arena fighters\n"
                "🗺️ `lb delve`  -  Deepest delvers\n"
                "🎣 `lb fish`  -  Top fishers by payout\n"
                "🐟 `lb biggest`  -  Trophy catches by weight\n"
                "🌾 `lb farm`  -  Top farmers\n"
                "🔨 `lb craft`  -  Top crafters by FORGE earned\n"
                "🏷️ `lb achievements`  -  Most badges earned\n"
                "💬 `lb level`  -  Chat XP leaderboard"
            )
            await ctx.reply(embed=card("📋 Leaderboard Categories", description=cats, color=C_INFO).build(), mention_author=False)
        elif fl.startswith("token ") or fl.startswith("coin "):
            sym = fl.split(None, 1)[-1].strip().upper()
            if not sym:
                await ctx.reply_error("Specify a token. Example: `lb token ARC`")
                return
            await self._lb_token(ctx, sym)
        elif fl:
            # Assume bare word is a token symbol
            sym = fl.split()[0].upper()
            all_tok = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
            if sym in Config.TOKENS or sym in all_tok:
                await self._lb_token(ctx, sym)
            else:
                await ctx.reply_error(
                    f"Unknown category `{fl}`.\n"
                    f"Try: `lb buddy`, `lb craft`, `lb fish`, `lb delve`, `lb farm`, `lb trading`, `lb token ARC`\n"
                    f"Use `lb help` for all categories."
                )
        else:
            await self._lb_networth(ctx)

    async def _delegate_lb(
        self,
        ctx: DiscoContext,
        cog_name: str,
        attr: str,
        arg: str | None = None,
    ) -> None:
        """Dispatch ``,lb <subcat>`` to a leaderboard living on another cog.

        ``attr`` may be a plain async method (e.g. ``render_leaderboard``
        on ChatLeveling) or a ``commands.Command`` wrapper (the per-cog
        ``@buddy.command(name="lb")`` callbacks). For the wrapped form we
        call ``.callback(cog, ctx, ...)`` so we bypass the framework's
        cooldown/check pipeline -- the wrapper around ``,lb`` already
        gates access via ``@guild_only``.
        """
        cog = ctx.bot.get_cog(cog_name)
        if cog is None:
            await ctx.reply_error(
                f"`{cog_name}` module is not loaded on this bot.",
            )
            return
        target = getattr(cog, attr, None)
        if target is None:
            await ctx.reply_error(
                f"Internal error: `{cog_name}.{attr}` is missing.",
            )
            return
        callback = getattr(target, "callback", None)
        try:
            if callback is not None:
                if arg is not None:
                    await callback(cog, ctx, arg)
                else:
                    await callback(cog, ctx)
            else:
                if arg is not None:
                    await target(ctx, arg)
                else:
                    await target(ctx)
        except Exception:
            log.exception(
                "lb dispatch failed cog=%s attr=%s", cog_name, attr,
            )
            await ctx.reply_error(
                "Could not load that leaderboard right now.",
            )

    def _lb_name(self, ctx: DiscoContext, user_id: int) -> str:
        m = ctx.guild.get_member(user_id)
        return m.display_name if m else f"User {user_id}"

    async def _lb_fetch_missing(self, ctx: DiscoContext, user_ids: list[int]) -> None:
        missing = [uid for uid in user_ids if ctx.guild.get_member(uid) is None]
        if missing:
            try:
                await ctx.guild.query_members(user_ids=missing, cache=True)
            except Exception:
                pass

    async def _lb_resolve_and_filter(
        self,
        ctx: DiscoContext,
        rows: list,
        *,
        uid_key,
    ) -> list:
        """Return ``rows`` minus user IDs that don't belong on a leaderboard.

        Filters out:
          * ``user_id == 0`` -- placeholder rows that have leaked into
            the DB since day 1 and have no Discord user behind them.
          * ``ctx.bot.user.id`` -- the bot's own row (Disco itself
            shows up on a few of the trade-volume LBs because it
            arbitrages its own oracle pump events).
          * Any other ``member.bot == True`` user (other bots in the
            guild, e.g. dev / staging deployments).
          * Members no longer in the guild -- left or banned. We use
            ``get_member`` after a single ``query_members`` round-trip
            so banned members and members who left are both treated
            uniformly (Discord doesn't report bans through guild
            membership; missing membership covers both).

        ``uid_key`` is a callable that pulls the user id out of each
        row -- pass ``operator.itemgetter("user_id")`` for dict rows
        or ``lambda r: r[0]`` for tuple rows.
        """
        bot_id = int(getattr(getattr(ctx, "bot", None), "user", None).id) if (
            getattr(ctx, "bot", None) and getattr(ctx.bot, "user", None)
        ) else 0
        try:
            ids = [int(uid_key(r)) for r in rows]
        except Exception:
            return list(rows)
        await self._lb_fetch_missing(ctx, ids)
        guild = ctx.guild
        filtered: list = []
        for r in rows:
            try:
                uid = int(uid_key(r))
            except Exception:
                continue
            if uid <= 0:
                continue
            if bot_id and uid == bot_id:
                continue
            member = guild.get_member(uid) if guild else None
            if member is None:
                continue
            if getattr(member, "bot", False):
                continue
            filtered.append(r)
        return filtered

    async def _lb_networth(self, ctx: DiscoContext) -> None:
        from services.net_worth import compute_bulk_net_worth

        gid = ctx.guild_id
        bot_id = ctx.bot.user.id
        user_val = await compute_bulk_net_worth(gid, ctx.db, exclude_user_id=bot_id)

        ranked_all = sorted(user_val.items(), key=lambda x: x[1], reverse=True)
        # Filter out user_id 0, the bot itself, other bots, and members
        # who left the guild before slicing -- mirrors what every other
        # _lb_* surface does (see _lb_resolve_and_filter).
        ranked = await self._lb_resolve_and_filter(
            ctx, ranked_all, uid_key=lambda r: r[0],
        )
        ranked = ranked[:50]

        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [uid for uid, _ in ranked]
        caller_rank, caller_found = self._lb_caller_info(all_ids, caller_id)
        base_footer = "Net worth = wallet + bank + crypto + DeFi + LP + rigs + validators + delegations + savings + items + NFTs - loans\nUse ,lb help for more categories"
        display_rows = ranked[:50]

        pages = []
        for chunk_start in range(0, len(display_rows), self._LEADERBOARD_PER_PAGE):
            chunk = display_rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, (uid, nw) in enumerate(chunk):
                rank = chunk_start + i
                line = self._lb_rank_prefix(rank, self._lb_name(ctx, uid), fmt_usd(nw), is_top3_detail=(chunk_start == 0), is_caller=(uid == caller_id))
                lines.append(line)
            footer = self._lb_rank_footer(base_footer, caller_rank)
            embed = card("🏆 Net Worth Leaderboard", color=C_AMBER).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_token(self, ctx: DiscoContext, sym: str) -> None:
        gid  = ctx.guild_id
        rows = await ctx.db.get_leaderboard_by_token(gid, sym, limit=200)
        if not rows:
            await ctx.reply_error(f"No one holds **{sym}** in this server.")
            return

        price_row = await ctx.db.get_price(sym, gid)
        price     = float(price_row["price"]) if price_row else 0.0
        all_tok   = await ctx.db.get_all_tokens_for_guild(gid)
        tok_cfg   = Config.TOKENS.get(sym) or all_tok.get(sym) or {}
        emoji     = tok_cfg.get("emoji", "●")

        rows = await self._lb_resolve_and_filter(
            ctx, rows, uid_key=lambda r: r["user_id"],
        )
        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [r["user_id"] for r in rows]
        caller_rank, _ = self._lb_caller_info(all_ids, caller_id)
        display_rows = rows[:50]

        pages = []
        for chunk_start in range(0, len(display_rows), self._LEADERBOARD_PER_PAGE):
            chunk = display_rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, row in enumerate(chunk):
                rank = chunk_start + i
                amt_h = row.h("amount")
                usd_str = f" ≈ ${amt_h * price:,.2f}" if price > 0 else ""
                you = " ◄ **you**" if row["user_id"] == caller_id else ""
                val_str = f"{_fmt_amt(amt_h)} {emoji}{sym}{usd_str}{you}"
                line = self._lb_rank_prefix(rank, self._lb_name(ctx, row["user_id"]), val_str, is_top3_detail=(chunk_start == 0))
                lines.append(line)
            footer = self._lb_rank_footer(f"Holdings of {sym}", caller_rank)
            embed = card(f"🏆 {emoji}{sym} Leaderboard", color=C_AMBER).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_hashrate(self, ctx: DiscoContext) -> None:
        gid      = ctx.guild_id
        all_rigs = await ctx.db.get_all_guild_rigs(gid)

        user_hr: dict[int, float] = {}
        for r in all_rigs:
            rig_cfg = Config.MINING_RIGS.get(r["rig_id"])
            if rig_cfg:
                user_hr[r["user_id"]] = user_hr.get(r["user_id"], 0.0) + rig_cfg["hashrate"] * r["quantity"]

        if not user_hr:
            await ctx.reply_error("No miners found in this server.")
            return

        ranked_all = sorted(user_hr.items(), key=lambda x: x[1], reverse=True)[:200]
        ranked = await self._lb_resolve_and_filter(
            ctx, ranked_all, uid_key=lambda r: r[0],
        )
        total_hr = sum(hr for _, hr in ranked)
        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [uid for uid, _ in ranked]
        caller_rank, _ = self._lb_caller_info(all_ids, caller_id)
        display_rows = ranked[:50]

        pages = []
        for chunk_start in range(0, len(display_rows), self._LEADERBOARD_PER_PAGE):
            chunk = display_rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, (uid, hr) in enumerate(chunk):
                rank  = chunk_start + i
                share = hr / total_hr * 100 if total_hr > 0 else 0
                line  = self._lb_rank_prefix(rank, self._lb_name(ctx, uid), f"{hr:,.0f} MH/s ({share:.1f}%)", is_top3_detail=(chunk_start == 0), is_caller=(uid == caller_id))
                lines.append(line)
            footer = self._lb_rank_footer(f"Total network hashrate: {total_hr:,.0f} MH/s", caller_rank)
            embed = card("🏆 Hashrate Leaderboard", color=C_AMBER).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_trading(self, ctx: DiscoContext) -> None:
        """Leaderboard by realized trading P&L."""
        gid = ctx.guild_id
        rows = await ctx.db.get_trading_leaderboard(gid, limit=200)
        if not rows:
            await ctx.reply_error("No trading activity recorded yet.")
            return
        rows = await self._lb_resolve_and_filter(
            ctx, rows, uid_key=lambda r: r["user_id"],
        )
        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [r["user_id"] for r in rows]
        caller_rank, _ = self._lb_caller_info(all_ids, caller_id)
        display_rows = rows[:50]
        pages = []
        for chunk_start in range(0, len(display_rows), self._LEADERBOARD_PER_PAGE):
            chunk = display_rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, row in enumerate(chunk):
                rank = chunk_start + i
                pnl = row.h("realized_pnl")
                trades = int(row.get("total_trades", 0))
                sign = "+" if pnl >= 0 else ""
                val_str = f"{sign}${pnl:,.2f} ({trades:,} trades)"
                line = self._lb_rank_prefix(rank, self._lb_name(ctx, row["user_id"]), val_str, is_top3_detail=(chunk_start == 0), is_caller=(row["user_id"] == caller_id))
                lines.append(line)
            footer = self._lb_rank_footer("Ranked by realized profit/loss (sell revenue - buy cost)", caller_rank)
            embed = card("📊 Trading Leaderboard", color=C_AMBER).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_gambling(self, ctx: DiscoContext) -> None:
        """Leaderboard by gambling profit."""
        gid = ctx.guild_id
        rows = await ctx.db.get_gambling_leaderboard(gid, limit=200)
        if not rows:
            await ctx.reply_error("No gambling activity recorded yet.")
            return
        rows = await self._lb_resolve_and_filter(
            ctx, rows, uid_key=lambda r: r["user_id"],
        )
        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [r["user_id"] for r in rows]
        caller_rank, _ = self._lb_caller_info(all_ids, caller_id)
        display_rows = rows[:50]
        pages = []
        for chunk_start in range(0, len(display_rows), self._LEADERBOARD_PER_PAGE):
            chunk = display_rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, row in enumerate(chunk):
                rank = chunk_start + i
                pnl = row.h("net_pnl")
                games = int(row.get("total_games", 0))
                sign = "+" if pnl >= 0 else ""
                val_str = f"{sign}${pnl:,.2f} ({games:,} games)"
                line = self._lb_rank_prefix(rank, self._lb_name(ctx, row["user_id"]), val_str, is_top3_detail=(chunk_start == 0), is_caller=(row["user_id"] == caller_id))
                lines.append(line)
            footer = self._lb_rank_footer("Ranked by net gambling profit/loss", caller_rank)
            embed = card("🎰 Gambling Leaderboard", color=C_AMBER).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_work(self, ctx: DiscoContext) -> None:
        """Leaderboard by total shifts completed."""
        gid = ctx.guild_id
        rows = await ctx.db.get_work_leaderboard(gid, limit=50)
        if not rows:
            await ctx.reply_error("No work activity recorded yet.")
            return
        rows = await self._lb_resolve_and_filter(
            ctx, rows, uid_key=lambda r: r["user_id"],
        )
        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [r["user_id"] for r in rows]
        caller_rank, _ = self._lb_caller_info(all_ids, caller_id)
        pages = []
        for chunk_start in range(0, len(rows), self._LEADERBOARD_PER_PAGE):
            chunk = rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, row in enumerate(chunk):
                rank = chunk_start + i
                count = int(row.get("work_count", 0))
                earned = row.h("total_earned")
                val_str = f"{count:,} shifts ({fmt_usd(earned)} earned)"
                line = self._lb_rank_prefix(rank, self._lb_name(ctx, row["user_id"]), val_str, is_top3_detail=(chunk_start == 0), is_caller=(row["user_id"] == caller_id))
                lines.append(line)
            footer = self._lb_rank_footer("Ranked by total shifts completed", caller_rank)
            embed = card("🔨 Work Leaderboard", color=C_INFO).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_staking(self, ctx: DiscoContext) -> None:
        """Leaderboard by total staked value (PoW + PoS)."""
        gid = ctx.guild_id
        prices_list = await ctx.db.get_all_prices(gid)
        prices = {r["symbol"]: float(r["price"]) for r in prices_list}
        stakes = await ctx.db.get_all_guild_stakes(gid)
        pos_vals = await ctx.db.get_pos_validators(gid)
        delegations = await ctx.db.get_all_guild_delegations(gid)
        user_val: dict[int, float] = {}
        for s in stakes:
            user_val[s["user_id"]] = user_val.get(s["user_id"], 0.0) + s.h("amount") * prices.get(s["symbol"], 0.0)
        for pv in pos_vals:
            user_val[pv["user_id"]] = user_val.get(pv["user_id"], 0.0) + pv.h("stake_amount") * prices.get(pv["stake_token"], 0.0)
        for d in delegations:
            user_val[d["user_id"]] = user_val.get(d["user_id"], 0.0) + d.h("amount") * prices.get(d["token"], 0.0)
        if not user_val:
            await ctx.reply_error("No staking activity in this server.")
            return
        ranked_all = sorted(user_val.items(), key=lambda x: x[1], reverse=True)[:200]
        ranked = await self._lb_resolve_and_filter(
            ctx, ranked_all, uid_key=lambda r: r[0],
        )
        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [uid for uid, _ in ranked]
        caller_rank, _ = self._lb_caller_info(all_ids, caller_id)
        display_rows = ranked[:50]
        pages = []
        for chunk_start in range(0, len(display_rows), self._LEADERBOARD_PER_PAGE):
            chunk = display_rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, (uid, val) in enumerate(chunk):
                rank = chunk_start + i
                line = self._lb_rank_prefix(rank, self._lb_name(ctx, uid), fmt_usd(val), is_top3_detail=(chunk_start == 0), is_caller=(uid == caller_id))
                lines.append(line)
            footer = self._lb_rank_footer("Total value staked across PoW validators, PoS validators, and delegations", caller_rank)
            embed = card("🔐 Staking Leaderboard", color=C_AMBER).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_lp(self, ctx: DiscoContext) -> None:
        """Leaderboard by total LP value across all pools (includes liqstone bonus)."""
        gid = ctx.guild_id
        lp_pos = await ctx.db.get_all_guild_lp_positions(gid)
        if not lp_pos:
            await ctx.reply_error("No liquidity providers in this server.")
            return
        prices_list = await ctx.db.get_all_prices(gid)
        prices = {r["symbol"]: float(r["price"]) for r in prices_list}

        # Pre-fetch liqstones for all LP holders for bonus calculation
        _liq_cache: dict[int, float] = {}
        async def _get_liq_bonus(uid: int) -> float:
            if uid not in _liq_cache:
                lqs = await ctx.db.get_liqstone(uid, gid)
                _liq_cache[uid] = _liqstone_stat(lqs, "lp_reward_bonus")
            return _liq_cache[uid]

        user_val: dict[int, float] = {}
        for lp in lp_pos:
            if float(lp["total_lp"]) > 0:
                share = float(lp["lp_shares"]) / float(lp["total_lp"])
                val = (
                    share * lp.h("reserve_a") * prices.get(lp["token_a"], 0.0)
                    + share * lp.h("reserve_b") * prices.get(lp["token_b"], 0.0)
                )
                bonus = await _get_liq_bonus(lp["user_id"])
                val *= (1.0 + bonus)
                user_val[lp["user_id"]] = user_val.get(lp["user_id"], 0.0) + val
        if not user_val:
            await ctx.reply_error("No liquidity providers in this server.")
            return
        ranked_all = sorted(user_val.items(), key=lambda x: x[1], reverse=True)[:200]
        ranked = await self._lb_resolve_and_filter(
            ctx, ranked_all, uid_key=lambda r: r[0],
        )
        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [uid for uid, _ in ranked]
        caller_rank, _ = self._lb_caller_info(all_ids, caller_id)
        display_rows = ranked[:50]
        pages = []
        for chunk_start in range(0, len(display_rows), self._LEADERBOARD_PER_PAGE):
            chunk = display_rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, (uid, val) in enumerate(chunk):
                rank = chunk_start + i
                line = self._lb_rank_prefix(rank, self._lb_name(ctx, uid), fmt_usd(val), is_top3_detail=(chunk_start == 0), is_caller=(uid == caller_id))
                lines.append(line)
            footer = self._lb_rank_footer("Ranked by total liquidity pool value", caller_rank)
            embed = card("🌊 LP Value Leaderboard", color=C_TEAL).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_streaks(self, ctx: DiscoContext) -> None:
        """Leaderboard by daily claim streak."""
        gid = ctx.guild_id
        all_users = await ctx.db.get_all_guild_users(gid)
        if not all_users:
            await ctx.reply_error("No users found.")
            return
        user_streaks = [(u["user_id"], int(u.get("daily_streak", 0))) for u in all_users if int(u.get("daily_streak", 0)) > 0]
        if not user_streaks:
            await ctx.reply_error("No active daily streaks in this server.")
            return
        ranked_all = sorted(user_streaks, key=lambda x: x[1], reverse=True)[:200]
        ranked = await self._lb_resolve_and_filter(
            ctx, ranked_all, uid_key=lambda r: r[0],
        )
        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [uid for uid, _ in ranked]
        caller_rank, _ = self._lb_caller_info(all_ids, caller_id)
        display_rows = ranked[:50]
        pages = []
        for chunk_start in range(0, len(display_rows), self._LEADERBOARD_PER_PAGE):
            chunk = display_rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, (uid, streak) in enumerate(chunk):
                rank = chunk_start + i
                fire = "🔥" * min(streak // 10, 5)
                line = self._lb_rank_prefix(rank, self._lb_name(ctx, uid), f"{streak:,} day streak {fire}", is_top3_detail=(chunk_start == 0), is_caller=(uid == caller_id))
                lines.append(line)
            footer = self._lb_rank_footer("Ranked by consecutive daily claim streak", caller_rank)
            embed = card("🔥 Daily Streak Leaderboard", color=C_GOLD).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_rugpull(self, ctx: DiscoContext) -> None:
        """Rugpull leaderboard  -  by total time holding the King of Rugs role."""
        gid = ctx.guild_id
        rows = await ctx.db.fetch_all(
            "SELECT * FROM rugpull_stats WHERE guild_id=$1 AND total_hold_seconds > 0 ORDER BY total_hold_seconds DESC LIMIT 50",
            gid,
        )
        if not rows:
            await ctx.reply_error("No rugpull history yet. Use `,rugpull` to start!")
            return
        rows = await self._lb_resolve_and_filter(
            ctx, rows, uid_key=lambda r: r["user_id"],
        )
        # Get current king
        king_row = await ctx.db.fetch_one(
            "SELECT user_id FROM rugpull_king WHERE guild_id=$1", gid
        )
        king_id = king_row["user_id"] if king_row else None
        caller_id = ctx.author.id if ctx.author else 0
        all_ids = [r["user_id"] for r in rows]
        caller_rank, _ = self._lb_caller_info(all_ids, caller_id)
        display_rows = rows[:50]
        pages = []
        for chunk_start in range(0, len(display_rows), self._LEADERBOARD_PER_PAGE):
            chunk = display_rows[chunk_start:chunk_start + self._LEADERBOARD_PER_PAGE]
            lines = []
            for i, row in enumerate(chunk):
                rank = chunk_start + i
                uid = row["user_id"]
                total_secs = int(row["total_hold_seconds"])
                hours = total_secs // 3600
                mins = (total_secs % 3600) // 60
                wins = int(row.get("wins", 0))
                crown = " 👑" if uid == king_id else ""
                val_str = f"{hours}h {mins}m held ({wins} wins){crown}"
                line = self._lb_rank_prefix(rank, self._lb_name(ctx, uid), val_str, is_top3_detail=(chunk_start == 0), is_caller=(uid == caller_id))
                lines.append(line)
            footer = self._lb_rank_footer("Ranked by total time holding King of Rugs  ·  👑 = current king", caller_rank)
            embed = card("🪤 Rugpull Leaderboard", color=C_CRIMSON).description("\n".join(lines)).footer(footer).build()
            pages.append(embed)
        await send_paginated(ctx, pages)

    async def _lb_eat(self, ctx: DiscoContext) -> None:
        """EatChain leaderboard. Delegates to the EatChain cog's multi-tab
        board so ,lb eat and ,eat lb are always identical (single source)."""
        cog = self.bot.get_cog("EatTheRich")
        if cog is not None and hasattr(cog, "_do_lb"):
            await cog._do_lb(ctx)
            return
        await ctx.reply_error(
            "The EatChain leaderboard is unavailable right now."
        )

    # ── $balance (paginated, with PnL, --type flag) ────────────────────────────

    @commands.hybrid_command(name="balance", aliases=["bal", "bals", "net", "networth", "p"])
    @guild_only
    @no_bots
    @ensure_registered
    async def balance(self, ctx: DiscoContext, *, flags: str = "") -> None:
        """Show your balance, profile, and positions. Flags: crypto, nodes, mining, lending
        Examples: .balance  |  .me  |  .balance crypto  |  .balance mining"""
        flags_lower = flags.lower()
        # Parse network filter: network <name>
        network_filter: str | None = None
        if "network" in flags_lower:
            parts = flags_lower.split("network")
            net_arg = parts[-1].strip().split()[0] if parts[-1].strip() else None
            if net_arg:
                # Canonical normalization handles "sun", "sun network",
                # "moneta", "mta", etc. Falls back to ``.title()`` for
                # anything unrecognized so existing input keeps working.
                network_filter = normalize_network_full(net_arg) or net_arg.title()

        uid = ctx.author.id
        gid = ctx.guild_id
        row = ctx.user_row
        settings = await ctx.db.get_guild_settings(gid)
        cur_name = guild_currency_name(settings)

        # Canonical net-worth components (moon_stake_value, moon_pool_stake_value,
        # nft_value, loan_liability, etc.) live on the NetWorthResult so every
        # surface agrees with services/net_worth.py. Do not recompute inline.
        from services.net_worth import compute_net_worth
        nw = await compute_net_worth(uid, gid, ctx.db)

        # Progression snapshot (achievements, streak, pass tier, active
        # challenges). Rendered on Summary + Profile tabs so these systems
        # feel native to the balance card instead of living in a parallel
        # ,achievements / ,streak / ,season pass cluster.
        from services.progression import user_snapshot as _prog_snapshot
        _prog = await _prog_snapshot(ctx.db, uid, gid)

        # Gather data for all pages
        wallet, bank = row.h("wallet"), row.h("bank")

        # Crypto holdings with PnL  -  grouped by network
        all_holdings = await ctx.db.get_holdings(uid, gid)
        all_tokens_cfg = await ctx.db.get_all_tokens_for_guild(gid)
        # by_net: {network_name: [(line_str, val), ...]}
        by_net: dict[str, list[tuple[str, float]]] = {}
        stable_lines: list[tuple[str, float]] = []
        crypto_lines: list[str] = []  # flat fallback for pagination
        crypto_value = 0.0
        crypto_pnl = 0.0
        for h in all_holdings:
            sym = h["symbol"]
            tcfg = all_tokens_cfg.get(sym, {})
            tok_net = tcfg.get("network", "") or "Other"
            if network_filter and tok_net != network_filter:
                continue
            price_row = await ctx.db.get_price(sym, gid)
            cur_price  = float(price_row["price"]) if price_row else 0.0
            open_price = float(price_row["open_price"]) if price_row else 0.0
            h_amount = to_human(h["amount"])
            val = cur_price * h_amount
            pnl = (cur_price - open_price) * h_amount
            crypto_value += val
            crypto_pnl += pnl
            if val < _DUST_USD and abs(pnl) < _DUST_USD:
                continue
            is_stable = tcfg.get("stablecoin") or tcfg.get("consensus") == "Fiat"
            if is_stable:
                pct_str = "= $1.00"
            else:
                pct = (cur_price - open_price) / open_price * 100 if open_price > 0 else 0.0
                arrow = "▲" if pct >= 0 else "▼"
                pct_str = f"{arrow}{abs(pct):.1f}%"
            emoji = tcfg.get("emoji", "●")
            line = f"{emoji} **{sym}**  ·  {_fmt_amt(h_amount)}  ·  **${val:,.2f}**  {pct_str}"
            flat_pnl = f"{'▲' if pnl >= 0 else '▼'} ${abs(pnl):,.2f}"
            crypto_lines.append(f"{emoji} **{sym}**: {_fmt_amt(h_amount)}  ≈ **${val:,.2f}**  {flat_pnl}")
            if is_stable:
                stable_lines.append((line, val))
            else:
                by_net.setdefault(tok_net, []).append((line, val))

        # Stakes
        lockstone  = await ctx.db.get_lockstone(uid, gid)
        stakes = await ctx.db.get_user_stakes(uid, gid)
        _ls_bonus = _item_stat(lockstone, "stake_bonus")
        stake_lines: list[str] = []
        stake_value = 0.0
        for s in stakes:
            if network_filter:
                v_net = Config.VALIDATORS.get(s["validator_id"], {}).get("network", "")
                if not v_net:
                    v_net = (await ctx.db.get_validator(s["validator_id"], gid) or {}).get("network", "")
                if v_net != network_filter:
                    continue
            price_row = await ctx.db.get_price(s["symbol"], gid)
            s_amount = to_human(s["amount"])
            val = float(price_row["price"]) * s_amount if price_row else 0.0
            stake_value += val
            daily = s_amount * s["reward_rate"] / max(Config.STAKING_REWARD_DIVISOR, 1e-9)
            stake_id_display = f"#{s.get('validator_id', '?')} " if s.get('validator_id') is not None else ""
            stake_lines.append(
                f"{s['emoji']} **{stake_id_display}{s['name']}** | {s_amount:.4f} {s['symbol']}"
                f"  ≈ **${val:,.2f}**  {fmt_bonus(f'+{daily:.6f}/day', _ls_bonus)}"
            )

        # LP positions
        lp_positions = await ctx.db.get_user_lp_positions(uid, gid)
        lp_lines: list[str] = []
        lp_value = 0.0
        lp_gain_total = 0.0
        _lp_liqstone = await ctx.db.get_liqstone(uid, gid)
        _lp_lq_bonus = _liqstone_stat(_lp_liqstone, "lp_reward_bonus")
        for lp in lp_positions:
            if lp["total_lp"] <= 0:
                continue
            frac = float(lp["lp_shares"]) / float(lp["total_lp"])
            val_a = to_human(lp["reserve_a"]) * frac
            val_b = to_human(lp["reserve_b"]) * frac
            ta, tb = lp["token_a"], lp["token_b"]
            if tb == "USD":
                usd_val = val_b * 2
                price_a = (val_b / val_a) if val_a > 0 else 0.0
                price_b = 1.0
            elif ta == "USD":
                usd_val = val_a * 2
                price_a = 1.0
                price_b = (val_a / val_b) if val_b > 0 else 0.0
            else:
                p_a = await ctx.db.get_price(ta, gid)
                p_b = await ctx.db.get_price(tb, gid)
                price_a = float(p_a["price"]) if p_a else 0.0
                price_b = float(p_b["price"]) if p_b else 0.0
                usd_val = val_a * price_a + val_b * price_b
            lp_value += usd_val
            snap = await ctx.db.get_lp_snapshot(uid, gid, lp["pool_id"])
            gain_usd = 0.0
            if snap:
                cur_a_per_lp = float(lp["reserve_a"]) / float(lp["total_lp"])
                cur_b_per_lp = float(lp["reserve_b"]) / float(lp["total_lp"])
                gain_a = max(0.0, (cur_a_per_lp - float(snap["entry_res_a_per_lp"])) * lp.h("lp_shares"))
                gain_b = max(0.0, (cur_b_per_lp - float(snap["entry_res_b_per_lp"])) * lp.h("lp_shares"))
                gain_usd = (gain_a * price_a + gain_b * price_b) * (1.0 + _lp_lq_bonus)
            lp_gain_total += gain_usd
            gain_str = f"  +{fmt_usd(gain_usd)}" if gain_usd > 0.005 else ""
            lp_lines.append(f"**{ta}/{tb}**: ≈ **{fmt_usd(usd_val)}**{gain_str}")

        # Mining
        rigs = await ctx.db.get_user_rigs(uid, gid)
        rig_value = sum(
            to_human(Config.MINING_RIGS[r["rig_id"]]["price"]) * r["quantity"] * 0.5
            for r in rigs if r["rig_id"] in Config.MINING_RIGS
        )
        total_hr = sum(
            Config.MINING_RIGS[r["rig_id"]]["hashrate"] * r["quantity"]
            for r in rigs if r["rig_id"] in Config.MINING_RIGS
        )
        # SUN is mined to DeFi wallet
        sun_h = await ctx.db.get_wallet_holding(uid, gid, "sun", "SUN")
        sun_bal = to_human(sun_h["amount"]) if sun_h else 0.0
        sun_price_row = await ctx.db.get_price("SUN", gid)
        sun_usd = float(sun_price_row["price"]) if sun_price_row else 0.0
        sun_usd_value = sun_bal * sun_usd
        mining_cfg = await ctx.db.get_user_mining_config(uid, gid)
        mine_mode = mining_cfg.get("mode", "pool").title()

        # DeFi wallet holdings (on-chain)
        all_wallet_holdings = await ctx.db.get_all_wallet_holdings(uid, gid)
        defi_by_net: dict[str, list[tuple[str, float]]] = {}
        defi_value = 0.0
        defi_pnl = 0.0
        for wh in all_wallet_holdings:
            wsym = wh["symbol"]
            wnet = _NETWORK_SHORTS.get(wh["network"], wh["network"])
            if network_filter and wnet != network_filter:
                continue
            wtcfg = all_tokens_cfg.get(wsym, {})
            wprice_row = await ctx.db.get_price(wsym, gid)
            if not wprice_row:
                continue
            wcur = float(wprice_row["price"])
            wopen = float(wprice_row["open_price"])
            wh_amount = to_human(wh["amount"])
            wval = wcur * wh_amount
            wpnl = (wcur - wopen) * wh_amount
            defi_value += wval
            defi_pnl += wpnl
            if wval < _DUST_USD and abs(wpnl) < _DUST_USD:
                continue
            wemoji = wtcfg.get("emoji", "●")
            arrow = "▲" if wpnl >= 0 else "▼"
            wline = (
                f"{wemoji} **{wsym}**  ·  {_fmt_amt(wh_amount)}  ·  "
                f"**${wval:,.2f}**  {arrow} ${abs(wpnl):,.2f}"
            )
            defi_by_net.setdefault(wnet, []).append((wline, wval))
        crypto_pnl += defi_pnl

        # Loan
        loan = await ctx.db.get_loan(uid, gid)
        loan_liability = to_human(loan["outstanding"]) if loan else 0.0

        # Delegations
        delegations = await ctx.db.get_user_delegations(uid, gid)
        delegation_value = 0.0
        delegation_lines: list[str] = []
        for d in delegations:
            price_row = await ctx.db.get_price(d["token"], gid)
            price = float(price_row["price"]) if price_row else 0.0
            d_amount = to_human(d["amount"])
            val = d_amount * price
            delegation_value += val
            delegation_lines.append(
                f"{mention(d['validator_user_id'], ctx.guild)}  -  **{d_amount:,.4f} {d['token']}**  ≈ **${val:,.2f}**"
                f"  *(earned: {to_human(d.get('total_earned', 0)):,.6f} {d['token']})*"
            )

        # Savings deposits
        usd_save = await ctx.db.get_savings_deposit(uid, gid, "USD")
        savings_value = to_human(usd_save["amount"]) if usd_save else 0.0

        # PoS validator stakes
        pos_validators = await ctx.db.get_user_pos_validators(uid, gid)
        pos_stake_value = 0.0
        for _pv in pos_validators:
            if _pv["stake_amount"] > 0:
                _pv_p = await ctx.db.get_price(_pv["stake_token"], gid)
                pos_stake_value += to_human(_pv["stake_amount"]) * (float(_pv_p["price"]) if _pv_p else 0.0)

        # Job + group (for profile page)
        job = await ctx.db.get_user_job(uid, gid)
        job_cfg = Config.JOBS.get(job["job_id"], Config.JOBS["HOMELESS"])
        grp = await ctx.db.get_user_mining_group(uid, gid)
        grp_str = " - "
        grp_upgrade_parts: list[str] = []
        if grp:
            role = "👑 Founder" if grp.get("founder_id") == uid else "Member"
            grp_str = f"**{grp['name']}** ({role})"
            # Fetch Hall upgrades for profile display
            _grp_upgrades = await ctx.db.get_group_upgrades(gid, grp["group_id"])
            _grp_uids = {u["upgrade_id"] for u in _grp_upgrades}
            _hall_cfg = Config.GROUP_HALL_UPGRADES
            _hall_gambling = 0.0
            _hall_daily = 0.0
            _hall_work = 0.0
            _extra_slots = 0
            _hall_unlocks: list[str] = []
            for _uid in _grp_uids:
                _eff = _hall_cfg.get(_uid, {}).get("effect", {})
                _hall_gambling += _eff.get("hall_gambling_bonus", 0.0)
                _hall_daily    += _eff.get("hall_daily_bonus", 0.0)
                _hall_work     += _eff.get("hall_work_bonus", 0.0)
                _extra_slots   += int(_eff.get("group_max_members", 0))
                if "hall_unlock" in _eff:
                    _hall_unlocks.append(_eff["hall_unlock"])
            if _hall_gambling > 0:
                grp_upgrade_parts.append(f"🎲 +{_hall_gambling*100:.0f}% gambling (Hall)")
            if _hall_daily > 0:
                grp_upgrade_parts.append(f"📅 +{_hall_daily*100:.0f}% daily (Hall)")
            if _hall_work > 0:
                grp_upgrade_parts.append(f"💼 +{_hall_work*100:.0f}% work (Hall)")
            if _extra_slots > 0:
                grp_upgrade_parts.append(f"🏗️ +{_extra_slots} member slots")
            if _hall_unlocks:
                grp_upgrade_parts.append(f"📋 Hall: {', '.join(_hall_unlocks)}")

        # Savings rates (for savings tab)
        _usd_total_dep, _usd_total_bor = await ctx.db.get_savings_totals(gid, "USD")
        _usd_borrow_daily, _usd_save_daily, _usd_util = compute_rates(_usd_total_dep, _usd_total_bor)
        _usd_b = to_human(usd_save["amount"]) if usd_save else 0.0

        # Items (stones + consumables). Per-stone rows are still fetched
        # individually for the per-stone Items tab below; the totalled
        # ``items_value`` comes from ``nw.items_value`` (services/
        # net_worth.py canonical) so the summary tab and the Items tab
        # can't drift from each other or from CLAUDE.md's "Net worth is
        # computed in ONE place".
        hashstone   = await ctx.db.get_hashstone(uid, gid)
        lockstone  = await ctx.db.get_lockstone(uid, gid)
        vaultstone = await ctx.db.get_vaultstone(uid, gid)
        liqstone   = await ctx.db.get_liqstone(uid, gid)
        gambastone = await ctx.db.fetch_one(
            "SELECT * FROM gambastones WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        vg_count = await ctx.db.get_validator_guard_count(uid, gid)
        yg_count = await ctx.db.get_yield_guard_count(uid, gid)
        # Don't re-derive items value here -- CLAUDE.md says NW is
        # computed in services/net_worth.py only. The inline sum used
        # to assume every stone was $1-pegged, which double-undercounted
        # hashstones (MTA/SUN) and lockstones (DSC/ARC).
        items_value = float(getattr(nw, "items_value", 0.0) or 0.0)

        # Net worth: single source of truth in services/net_worth.py. Includes
        # moon_stake_value, moon_pool_stake_value, and nft_value that the old
        # inline sum silently omitted.
        total_net = nw.total

        # ── Direct flag → single embed ─────────────────────────────────────
        if "crypto" in flags_lower:
            pnl_icon = "📈" if crypto_pnl >= 0 else "📉"
            _b = card("📈 Crypto Holdings", color=C_INFO).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            if not by_net and not stable_lines:
                _b = _b.description("No crypto holdings.")
            else:
                # One field per network, sorted
                for net_name in sorted(by_net.keys()):
                    entries = by_net[net_name]
                    net_total = sum(v for _, v in entries)
                    field_text = "\n".join(line for line, _ in entries)
                    # Truncate if too long (Discord 1024 char limit)
                    if len(field_text) > 1000:
                        lines_list = [line for line, _ in entries]
                        field_text = "\n".join(lines_list[:8]) + f"\n…+{len(lines_list)-8} more"
                    _b = _b.field(f"🌐 {net_name}  (≈${net_total:,.2f})", field_text, True)
                if stable_lines:
                    stable_total = sum(v for _, v in stable_lines)
                    _b = _b.field(f"💵 Stablecoins  (≈${stable_total:,.2f})", "\n".join(line for line, _ in stable_lines), False)
            _b = _b.footer(f"Total: ${crypto_value:,.2f}  |  PnL {pnl_icon} ${crypto_pnl:+,.2f} today")
            embed = _b.build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        if "nodes" in flags_lower or "staking" in flags_lower:
            _b = card("🌐 Yield Farming Positions", color=C_PURPLE).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            if stake_lines:
                for i in range(0, len(stake_lines), 10):
                    _b = _b.field("Yield Farms" if i == 0 else "\u200b", "\n".join(stake_lines[i:i+10]), False)
            else:
                _b = _b.description("No active stakes.")
            if delegation_lines:
                _b = _b.field(f"🤝 Delegations  (≈${delegation_value:,.2f})", "\n".join(delegation_lines[:10]), False)
            # Safety Module summary (VTR/DSY single-token yield staking)
            sm_lines: list[str] = []
            for sm_sym in ("VTR", "DSY"):
                sm_row = await ctx.db.get_sm_stake(uid, gid, sm_sym)
                if not sm_row or int(sm_row.get("amount", 0)) <= 0:
                    continue
                sm_emoji = Config.TOKENS.get(sm_sym, {}).get("emoji", "")
                sm_staked_h = to_human(int(sm_row["amount"]))
                sm_price_row = await ctx.db.get_price(sm_sym, gid)
                sm_price = float(sm_price_row["price"]) if sm_price_row else 0.0
                sm_status = "🔒 cooldown" if sm_row.get("cooldown_at") else "✅ earning"
                sm_lines.append(
                    f"{sm_emoji} **{sm_sym}** -- {sm_staked_h:,.4f}  "
                    f"≈ **${sm_staked_h * sm_price:,.2f}**  ·  {sm_status}"
                )
            if sm_lines:
                _b = _b.field(
                    f"🛡 Safety Module  (≈${float(nw.safety_module_value):,.2f})",
                    "\n".join(sm_lines), False,
                )
            _b = _b.footer(
                f"Total node value: ~${stake_value:,.2f}  |  "
                f"Delegated: ~${delegation_value:,.2f}  |  "
                f"Safety Module: ~${float(nw.safety_module_value):,.2f}"
            )
            embed = _b.build()
            _sel_view = ValidatorSelectView(
                ctx.author.id,
                stakes=stakes,
                delegations=delegations,
                pos_validators=pos_validators,
                db=ctx.db,
                guild_id=gid,
            )
            await ctx.reply(
                embed=embed,
                view=_sel_view if _sel_view.has_options else None,
                mention_author=False,
            )
            return

        if "lp" in flags_lower or "liquidity" in flags_lower:
            _lqs = liqstone
            _lq_bonus = _liqstone_stat(_lqs, "lp_reward_bonus")
            _b = card("🌊 Liquidity Positions", color=C_TEAL).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            _gain_footer = ""
            if lp_positions:
                detail_lines: list[str] = []
                total_gain = 0.0
                for lp in lp_positions:
                    if lp["total_lp"] <= 0:
                        continue
                    ta, tb = lp["token_a"], lp["token_b"]
                    frac = float(lp["lp_shares"]) / float(lp["total_lp"])
                    val_a = to_human(lp["reserve_a"]) * frac
                    val_b = to_human(lp["reserve_b"]) * frac
                    share_pct = frac * 100
                    p_a = p_b = None
                    if tb == "USD":
                        usd_val = val_b * 2
                    elif ta == "USD":
                        usd_val = val_a * 2
                    else:
                        p_a = await ctx.db.get_price(ta, gid)
                        p_b = await ctx.db.get_price(tb, gid)
                        usd_val = (
                            val_a * (float(p_a["price"]) if p_a else 0)
                            + val_b * (float(p_b["price"]) if p_b else 0)
                        )
                    snap = await ctx.db.get_lp_snapshot(uid, gid, lp["pool_id"])
                    if tb == "USD":
                        price_a = (val_b / val_a) if val_a > 0 else 0.0
                        price_b = 1.0
                    elif ta == "USD":
                        price_a = 1.0
                        price_b = (val_a / val_b) if val_b > 0 else 0.0
                    else:
                        price_a = float(p_a["price"]) if p_a else 0.0
                        price_b = float(p_b["price"]) if p_b else 0.0
                    gain_usd = 0.0
                    if snap:
                        cur_a_per_lp = float(lp["reserve_a"]) / float(lp["total_lp"])
                        cur_b_per_lp = float(lp["reserve_b"]) / float(lp["total_lp"])
                        gain_a = max(0.0, (cur_a_per_lp - float(snap["entry_res_a_per_lp"])) * lp.h("lp_shares"))
                        gain_b = max(0.0, (cur_b_per_lp - float(snap["entry_res_b_per_lp"])) * lp.h("lp_shares"))
                        gain_usd = (gain_a * price_a + gain_b * price_b) * (1.0 + _lq_bonus)
                    total_gain += gain_usd
                    gain_pct = (gain_usd / max(usd_val - gain_usd, 1e-9)) * 100 if usd_val > 0 else 0.0
                    gain_str = fmt_bonus(f"+{fmt_usd(gain_usd)} ({fmt_pct(gain_pct)})", _lq_bonus, "Liqstone")
                    since_str = f"  ·  Since {fmt_ts(lp['added_at'])}" if lp.get("added_at") else ""
                    detail_lines.append(
                        f"**{ta}/{tb}**  ({share_pct:.2f}% of pool){since_str}\n"
                        f"  {fmt_usd(usd_val)}  ·  Gain: **{gain_str}**\n"
                        f"  {val_a:.4f} {ta} + {val_b:.4f} {tb}"
                    )
                for i in range(0, len(detail_lines), 5):
                    _b = _b.field("Pools" if i == 0 else "\u200b", "\n\n".join(detail_lines[i:i+5]), False)
                _total_gain_pct = (total_gain / max(lp_value - total_gain, 1e-9)) * 100 if lp_value > 0 else 0.0
                _gain_footer = f"  |  Gained: {fmt_usd(total_gain)} ({fmt_pct(_total_gain_pct)})"
            else:
                _b = _b.description("No active LP positions.")
            _lq_footer = f"  |  Liqstone 💎+{int(_lq_bonus * 100)}% LP rewards" if _lq_bonus else ""
            _b = _b.footer(f"Total LP value: {fmt_usd(lp_value)}{_gain_footer}{_lq_footer}")
            await ctx.reply(embed=_b.build(), mention_author=False)
            return

        if "mining" in flags_lower:
            _ss_bonus = _item_stat(hashstone, "mining_bonus")
            _b = (
                card("⛏ Mining", color=C_AMBER)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("Mode",         f"**{mine_mode}**",                                     True)
                .field("Hashrate",     f"**{fmt_bonus(f'{total_hr:,} MH/s', _ss_bonus)}**",   True)
                .field("☀ SUN (DeFi)", f"{sun_bal:.6f}  ≈ ${sun_usd_value:,.2f}",             True)
            )
            if rigs:
                rig_lines = [
                    f"{Config.MINING_RIGS[r['rig_id']]['emoji']} {Config.MINING_RIGS[r['rig_id']]['name']} × {r['quantity']}"
                    for r in rigs if r["rig_id"] in Config.MINING_RIGS and r["quantity"] > 0
                ]
                _b = _b.field("Rigs", "\n".join(rig_lines) or " - ", False)
            embed = _b.build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        if "lending" in flags_lower:
            _b = card("🏦 Lending", color=C_ERROR).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            if loan:
                ltv = to_human(loan["outstanding"]) / to_human(loan["collateral"]) * 100 if loan["collateral"] > 0 else 0
                _b = (
                    _b.field("Outstanding", fmt_usd(to_human(loan['outstanding'])),       True)
                      .field("Collateral",  fmt_usd(to_human(loan['collateral'])),         True)
                      .field("LTV",         f"{ltv:.1f}%",                                True)
                )
            else:
                _b = _b.description("No active loans.")
            embed = _b.build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        # ── Category paginated view ────────────────────────────────────────
        categories: dict[str, list[discord.Embed]] = {}

        # 💎 Summary (shown first  -  default landing page)
        # Headline -> liquid cash row -> sorted holdings list. Progression
        # (achievements / streak / pass / challenges) lives on the Profile
        # tab; Summary stays a clean financial card.
        pnl_arrow = "▲" if crypto_pnl >= 0 else "▼"
        headline = (
            f"**${total_net:,.2f}**  ·  net worth\n"
            f"{pnl_arrow} **${abs(crypto_pnl):,.2f}**  ·  PnL today (vs open)"
        )

        # Fold per-system twin entries into one row each so the card reads
        # as one line per system instead of two adjacent same-emoji entries.
        moon_total    = float(nw.moon_stake_value)    + float(nw.moon_pool_stake_value)
        delve_total   = float(nw.delve_stake_value)   + float(nw.delve_party_value)
        farm_total    = (
            float(nw.farming_stake_value)
            + float(nw.farming_plot_value)
            + float(nw.farming_inventory_value)
        )
        craft_total   = float(nw.crafting_stake_value) + float(nw.crafting_inventory_value)

        # Build holdings list: (emoji+label, value). Sorted by value desc
        # below so the biggest positions are on top.
        _categories: list[tuple[str, float]] = [
            ("📈 CeFi Crypto",     crypto_value),
            ("🔐 DeFi Wallet",     defi_value),
            ("🌐 Nodes",           stake_value),
            ("🛡 Safety Module",   float(nw.safety_module_value)),
            ("🌊 LP",              lp_value),
            ("🖥️ Rigs",           rig_value),
            ("🤝 Delegations",     delegation_value),
            ("⛓️ Validator",      pos_stake_value),
            ("🌕 Moon",            moon_total),
            ("💰 Savings",         savings_value),
            ("🎒 Items",           items_value),
            ("\U0001F3A8 NFTs",    float(nw.nft_value)),
            ("\U0001F33E Farming", farm_total),
            ("\U0001F528 Crafting",craft_total),
            ("\U0001F3A3 Fishing", float(nw.fishing_stake_value)),
            ("\U0001F5FA Delve",   delve_total),
            ("\U0001F436 Buddy",   float(nw.buddy_economy_value)),
            ("🎢 Disc.Fun",        float(nw.disc_fun_value)),
            ("\U0001F3B0 Gamba",   float(nw.gamba_stake_value)),
            ("\U0001F4DA Sage",    float(nw.sage_stake_value)),
            ("\U0001F37D EatChain", float(nw.eat_stake_value)),
        ]
        _categories.sort(key=lambda kv: kv[1], reverse=True)
        holding_lines = [
            f"{label}  **${value:,.2f}**"
            for label, value in _categories if value > 0
        ]

        _b = (
            card("💎 Balance Summary", color=C_GOLD)
            .author(f"{ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
            .description(headline)
            .field("💵 Wallet", f"**${wallet:,.2f}**", True)
            .field("🏦 Bank",   f"**${bank:,.2f}**",   True)
        )
        if loan_liability > 0:
            _b = _b.field("🔴 Loan", f"-**${loan_liability:,.2f}**", True)
        # Two-column Holdings layout. Discord embeds render up to 3
        # inline fields per row, so emitting the list as 2 inline fields
        # gives a clean side-by-side layout. Even-half on the left,
        # odd-half on the right -- biggest position top-left.
        if holding_lines:
            mid = (len(holding_lines) + 1) // 2
            left, right = holding_lines[:mid], holding_lines[mid:]
            _b = _b.field("Holdings", "\n".join(left), True)
            if right:
                # Zero-width space as the right-column header so the two
                # columns read as one labelled block instead of two.
                _b = _b.field("​", "\n".join(right), True)
        summary_embed = _b.build()
        categories["💎 Summary"] = [summary_embed]

        # 🪪 Profile (placed after Summary so Summary is the default landing page)
        _grp_display = grp_str
        if grp_upgrade_parts:
            _grp_display += "\n" + " · ".join(grp_upgrade_parts)
        from services.progression import format_inline as _prog_inline
        _prof = (
            card("🪪 Player Profile", color=C_PURPLE)
            .author(f"{ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
            .field("💼 Job",    f"**{job_cfg['title']}**\n{job['work_count']} sessions",                                          True)
            .field("💵 Wallet", f"**${wallet:,.2f}**",                                                                            True)
            .field("🏦 Bank",   f"**${bank:,.2f}**",                                                                              True)
            .field("⛏ Mining",  f"**{mine_mode}** | {fmt_bonus(f'{total_hr:,} MH/s', _item_stat(hashstone, 'mining_bonus'))}\n☀ {sun_bal:.6f} SUN  ≈ ${sun_usd_value:,.2f}",           True)
            .field("👥 Group",  _grp_display,                                                                                      True)
            .field("⭐ Progression", _prog_inline(_prog),                                                                          False)
            .field("💎 Net Worth", f"**${total_net:,.2f}**",                                                                      False)
        )
        # Wealth Bottleneck preview line. Cheap (cached rank lookup); the
        # field stays out of the embed entirely when the system is dormant
        # for this guild so small servers don't see a confusing "x1.00".
        try:
            from services.bottleneck import (
                bottleneck_multiplier, lookup_percentile, percentile_label,
            )
            _bn_pct, _, _bn_n = await lookup_percentile(
                ctx.db, uid=uid, gid=gid,
            )
            _bn_min = int(getattr(Config, "BOTTLENECK_MIN_HOLDERS", 5))
            if _bn_n >= max(2, _bn_min):
                _bn_mult = bottleneck_multiplier(_bn_pct)
                _bn_arrow = "📈" if _bn_mult > 1.0 else ("📉" if _bn_mult < 1.0 else "—")
                _prof.field(
                    f"⚖️ Wealth Bottleneck {_bn_arrow}",
                    (
                        f"Rank: **{percentile_label(_bn_pct)}** "
                        f"({_bn_pct*100:.1f}%-ile)\n"
                        f"Multiplier on every USD-equiv gain: **x{_bn_mult:.2f}**\n"
                        f"`,bottleneck` for breakdown / curve / community pool."
                    ),
                    False,
                )
        except Exception:
            pass
        categories["🪪 Profile"] = [_prof.build()]

        # 🎒 Items  -  split into leveled items + consumables
        _SS_cfg  = Config.SHOP_ITEMS.get("hashstone", {})
        _LS_cfg  = Config.SHOP_ITEMS.get("lockstone", {})
        _VS_cfg  = Config.SHOP_ITEMS.get("vaultstone", {})
        _LQ_cfg  = Config.SHOP_ITEMS.get("liqstone", {})
        _VG_cfg  = Config.SHOP_ITEMS.get("validator_guard", {})
        _YG_cfg  = Config.SHOP_ITEMS.get("yield_guard", {})
        # Pre-fetch oracle prices once so the per-stone field can show
        # the staked balance's USD value without re-running async price
        # lookups inside the sync render helper.
        _stone_prices = await _stone_price_map(ctx.db, gid)
        _ITEM_STAT_LABELS = {
            "work_daily_bonus":    ("💼", "Work/Daily"),
            "mining_bonus":        ("⛏",  "Mining"),
            "stake_bonus":         ("📈", "Staking"),
            "interest_bonus":      ("🏦", "Interest"),
            "swap_fee_discount":   ("🔄", "Swap fee reduc"),
            "lp_reward_bonus":     ("🌊", "LP rewards"),
        }
        _XP_SOURCES = {
            "hashstone":   "XP from: mining blocks",
            "lockstone":  "XP from: staking & validator blocks",
            "vaultstone": "XP from: savings deposits & interest",
            "liqstone":   "XP from: providing LP (value x hold time)",
        }
        def _bal_stone_field(stone, cfg, name_key):
            if not cfg:
                return " - "
            accepted_now = list(cfg.get("accepted_currencies") or ("DSD", "USDC"))
            if not stone:
                accepted_str = " / ".join(f"`{c}`" for c in accepted_now)
                return (
                    f"Not owned  -  `/shop buy {name_key}` for "
                    f"**{fmt_usd(to_human(cfg.get('cost_stable', 0)))}** "
                    f"-- pay in {accepted_str}."
                )
            lv    = stone["level"]
            xp    = stone["xp"]
            staked = to_human(stone["staked_amount"])
            stone_cur = (stone.get("lp_currency") or "").upper()
            if not stone_cur or (accepted_now and stone_cur not in accepted_now):
                stone_cur = accepted_now[0] if accepted_now else "DSD"
            stone_emoji = (
                _stable_emoji(stone_cur)
                if stone_cur in _STABLE_NETWORK or stone_cur == "USD"
                else (Config.TOKENS.get(stone_cur, {}).get("emoji", ""))
            )
            max_lv = cfg.get("max_level", 100)
            base   = cfg.get("xp_per_level_base", 80)
            fill = int(12 * min((xp - base * lv * (lv - 1) // 2) / max(1, base * lv), 1))
            bar = "█" * fill + "░" * (12 - fill)
            staked_usd = _stone_staked_usd(staked, stone_cur, _stone_prices)
            usd_str = f" (≈ {fmt_usd(staked_usd)})" if staked_usd > 0 else ""
            staked_str = f"{fmt_token(staked, stone_cur, stone_emoji)}{usd_str}"
            lines = []
            if lv < max_lv:
                xp_start = base * lv * (lv - 1) // 2
                xp_next  = base * (lv + 1) * lv // 2
                xp_str = f"{xp - xp_start:,.1f} / {xp_next - xp_start:,.0f} XP"
                lines.append(f"**Level {lv} / {max_lv}** · {staked_str} staked")
                lines.append(f"`{bar}` {xp_str}  <- next level")
            else:
                lines.append(f"**Level {lv} / {max_lv} MAX** · {staked_str} staked")
            bonus_parts = []
            for sk, (em, lb) in _ITEM_STAT_LABELS.items():
                val = cfg.get("stats", {}).get(sk, 0.0)
                if val == 0.0:
                    continue
                eff = val * lv
                bonus_parts.append(f"{em} {lb}: **+{eff*100:.1f}%**")
            if bonus_parts:
                lines.append("  ·  ".join(bonus_parts))
            if lv < max_lv and name_key in _XP_SOURCES:
                lines.append(f"*{_XP_SOURCES[name_key]}*")
            return "\n".join(lines)

        # Leveled items embed
        # Iterate every configured stone (matches ,inventory). Previous
        # hand-rolled list of 4 stones (hash/lock/vault/liq) silently
        # dropped gambastones AND every themed stone (tide / heart /
        # crypt / blood / bloom / gavel / anvil / chimera) -- players
        # who owned them saw nothing on the balance Items tab.
        from cogs.shop import _STONE_CFGS as _SHOP_STONE_CFGS
        _items_b = (
            card("⛏️ Items", color=C_GOLD)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        )
        _owned_stones: dict[str, dict | None] = {
            "hashstone":  hashstone,
            "lockstone":  lockstone,
            "vaultstone": vaultstone,
            "liqstone":   liqstone,
            "gambastone": gambastone,
        }
        for _skey, _scfg in _SHOP_STONE_CFGS.items():
            if not _scfg or _scfg.get("disabled"):
                continue
            if _skey not in _owned_stones:
                # Themed / meta-economy stones use the same db.get_<key>
                # convention as the shop cog. Falling back to None when
                # the helper isn't available means the field renders as
                # "Not owned" instead of crashing.
                try:
                    _getter = getattr(ctx.db, f"get_{_skey}")
                    _owned_stones[_skey] = await _getter(uid, gid)
                except AttributeError:
                    _owned_stones[_skey] = None
                except Exception:
                    _owned_stones[_skey] = None
            _items_b = _items_b.field(
                f"{_scfg.get('emoji', '')} {_scfg.get('name', _skey.title())}",
                _bal_stone_field(_owned_stones.get(_skey), _scfg, _skey),
                False,
            )
        _items_b = _items_b.footer("/inventory levelup <stone> to level up  |  /shop to buy or sell")

        # Consumables embed
        _cons_b = (
            card("🧰 Consumables", color=C_WARNING)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        )
        if _VG_cfg:
            _cons_b = _cons_b.field(f"{_VG_cfg.get('emoji','🛡️')} Validator Guard", f"**{vg_count}** / {_VG_cfg.get('max_stack', 50)}", True)
        if _YG_cfg:
            _cons_b = _cons_b.field(f"{_YG_cfg.get('emoji','🔐')} Yield Guard", f"**{yg_count}** / {_YG_cfg.get('max_stack', 50)}", True)
        _cons_b = _cons_b.footer("/shop buy <consumable> [qty]")
        categories["🎒 Items"] = [_items_b.build(), _cons_b.build()]

        # 🔐 DeFi Wallets -- one description block grouped by network. Network
        # subtotals live in the section header so an 8-network player can scan
        # the page without fighting Discord's inline-field column wrap.
        if defi_by_net:
            defi_blocks: list[str] = []
            for net_name in sorted(defi_by_net.keys()):
                entries = defi_by_net[net_name]
                if not entries:
                    continue
                net_total = sum(v for _, v in entries)
                shown = entries[:8]
                body = "\n".join(line for line, _ in shown)
                if len(entries) > 8:
                    body += f"\n-# +{len(entries)-8} more"
                defi_blocks.append(
                    f"**🌐 {net_name}**  ·  ≈ ${net_total:,.2f}\n{body}"
                )
            defi_desc = _truncate_desc("\n\n".join(defi_blocks)) or " - "
            defi_embed = (
                card(f"🔐 DeFi Wallet  ·  ≈ ${defi_value:,.2f}", color=C_PURPLE)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .description(defi_desc)
                .footer(".wallet deposit, .wallet withdraw, .wallet list")
                .build()
            )
            categories["🔐 DeFi"] = [defi_embed]

        # 📈 Crypto (CeFi) -- mirrors the DeFi page format so the two read the
        # same. Stablecoins stay in their own section at the end since they
        # don't slot under any one chain.
        if by_net or stable_lines:
            crypto_blocks: list[str] = []
            for net_name in sorted(by_net.keys()):
                entries = by_net[net_name]
                if not entries:
                    continue
                net_total = sum(v for _, v in entries)
                shown = entries[:8]
                body = "\n".join(line for line, _ in shown)
                if len(entries) > 8:
                    body += f"\n-# +{len(entries)-8} more"
                crypto_blocks.append(
                    f"**🌐 {net_name}**  ·  ≈ ${net_total:,.2f}\n{body}"
                )
            if stable_lines:
                stable_total = sum(v for _, v in stable_lines)
                stable_shown = stable_lines[:8]
                stable_body = "\n".join(line for line, _ in stable_shown)
                if len(stable_lines) > 8:
                    stable_body += f"\n-# +{len(stable_lines)-8} more"
                crypto_blocks.append(
                    f"**💵 Stablecoins**  ·  ≈ ${stable_total:,.2f}\n{stable_body}"
                )
            crypto_desc = _truncate_desc("\n\n".join(crypto_blocks)) or " - "
            pnl_sign = "+" if crypto_pnl >= 0 else ""
            crypto_embed = (
                card(f"📈 CeFi Crypto  ·  ≈ ${crypto_value:,.2f}", color=C_INFO)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .description(crypto_desc)
                .footer(f"PnL today: {pnl_sign}${crypto_pnl:,.2f}  |  .crypto buy/sell to trade")
                .build()
            )
            categories["📈 Crypto"] = [crypto_embed]

        # 🌐 Nodes  -  identical to /stake mine
        if stakes:
            _stake_by_net: dict[str, list] = {}
            _total_daily: dict[str, float] = {}
            for s in stakes:
                _snet = s.get("network") or Config.VALIDATORS.get(s["validator_id"], {}).get("network", "") or "Other"
                _stake_by_net.setdefault(_snet, []).append(s)
                _s_amount = to_human(s["amount"])
                _daily = _s_amount * s["reward_rate"] / max(Config.STAKING_REWARD_DIVISOR, 1e-9)
                _total_daily[s["symbol"]] = _total_daily.get(s["symbol"], 0.0) + _daily
            _summary_str = "  ".join(f"+{fmt_token(v, k)}" for k, v in _total_daily.items())
            node_pages: list[discord.Embed] = []
            for _net, _net_stakes in sorted(_stake_by_net.items()):
                _nb = (
                    card(f"💼 Yield Farming  -  {_net}  (approx ${stake_value:,.2f})", color=C_PURPLE)
                    .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                )
                for s in _net_stakes:
                    _s_amount_h = to_human(s["amount"])
                    _daily = _s_amount_h * s["reward_rate"] / max(Config.STAKING_REWARD_DIVISOR, 1e-9)
                    _daily_str = fmt_bonus(f"+{fmt_token(_daily, s['symbol'])}", _ls_bonus)
                    _uptime_bar = FormatKit.bar(s["uptime_rate"], 1.0, width=8)
                    _nb.field(f"{s['emoji']} {s['name']}", f"🪙 {Config.currency_label(s['symbol'], detail=True)}", True)
                    _nb.field("💎 Staked",    f"**{fmt_token(_s_amount_h, s['symbol'])}**",  True)
                    _nb.field("📊 Daily Est", f"**{_daily_str}**", True)
                    _nb.field("⏱ Uptime",    f"`{_uptime_bar}`",                              True)
                _nb.footer(f"📈 Est. total daily yield: {_summary_str}")
                node_pages.append(_nb.build())
            categories["🌐 Nodes"] = node_pages

        # 🌕 Lunar Mint / Moon Pool (Moons system)
        # moon_stake_value / moon_pool_stake_value are read off the nw result
        # computed at the top of this command.
        lunar_rows = await ctx.db.get_lunar_stakes_for_user(uid, gid)
        if lunar_rows:
            _lb = (
                card(
                    f"\U0001F315 Lunar Mint  (approx ${nw.moon_stake_value:,.2f})",
                    color=C_PURPLE,
                )
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            )
            for row in lunar_rows:
                sym = row["symbol"]
                emoji = all_tokens_cfg.get(sym, {}).get("emoji", "")
                session_earned = float(row.get("session_earned") or 0)
                total_earned = float(row.get("total_earned") or 0)
                _lb.field(
                    f"{emoji} {sym}",
                    (
                        f"\U0001F48E Staked: **{fmt_token(row.h('amount'), sym)}**\n"
                        f"\U0001F4B0 Session: `{fmt_token(session_earned, 'MOON')}`\n"
                        f"\U0001F3C6 Lifetime: `{fmt_token(total_earned, 'MOON')}`"
                    ),
                    True,
                )
            _lb.footer(
                ".moon stake <SYMBOL> <amount> to open/top up  |  "
                ".moon unstake <SYMBOL> to close"
            )
            categories["\U0001F315 Lunar Mint"] = [_lb.build()]

        moon_pool_row = await ctx.db.get_moon_stake(uid, gid)
        moon_pool_raw = int(moon_pool_row["amount"]) if moon_pool_row else 0
        if moon_pool_raw > 0:
            pool_total_raw = await ctx.db.get_moon_pool_total_raw(gid)
            share = (moon_pool_raw / pool_total_raw) if pool_total_raw > 0 else 0.0
            vault_usd = await ctx.db.get_moon_vault_distributable(gid)

            # Per-token lifetime yield, summed straight from the MOON_POOL_YIELD
            # tx log. Single-USD session_earned / total_earned columns on
            # moon_stakes don't reflect the basket split that actually lands
            # in the user's wallets, so we bypass them here.
            yield_rows = await ctx.db.fetch_all(
                """
                SELECT symbol_out, SUM(amount_out)::TEXT AS total_raw
                  FROM transactions
                 WHERE user_id = $1 AND guild_id = $2
                   AND tx_type = 'MOON_POOL_YIELD'
                   AND symbol_out IS NOT NULL AND amount_out IS NOT NULL
                 GROUP BY symbol_out
                """,
                uid, gid,
            )
            earned_lines = [
                f"{r['symbol_out']}: {fmt_token(to_human(int(r['total_raw'])), r['symbol_out'])}"
                for r in yield_rows if int(r["total_raw"] or 0) > 0
            ]

            # Projected next-tick payout for this user in USD. 1/96 of the
            # vault goes out per hour, scaled by this user's pool share.
            from constants.moons import HOURLY_DRIP_FRACTION
            next_drip_usd = vault_usd * HOURLY_DRIP_FRACTION * share

            _mpb = (
                card(
                    f"\U0001F315 Moon Pool  (approx ${nw.moon_pool_stake_value:,.2f})",
                    color=C_PURPLE,
                )
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("\U0001F48E Staked", fmt_token(to_human(moon_pool_raw), "MOON"), True)
                .field("\U0001F3D6 Pool Share", fmt_pct(share * 100), True)
                .field(
                    "\U000023E9 Next Tick (est)",
                    f"{fmt_usd(next_drip_usd)}\n_split across MTA / ARC / DSC / SUN_",
                    True,
                )
                .field(
                    "\U0001F3C6 Lifetime Earned",
                    "\n".join(earned_lines) if earned_lines else "_nothing paid out yet_",
                    False,
                )
                .footer(
                    ".moon pool stake <amt|all> to stake MOON, "
                    "earn an equal-USD basket of MTA / ARC / DSC / SUN each hour"
                )
            )
            categories["\U0001F315 Moon Pool"] = [_mpb.build()]

        # 🌊 LP & Mining
        if lp_lines or total_hr > 0:
            _b = card("🌊 LP & Mining", color=C_TEAL).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            if lp_lines:
                _lp_header_gain = f"  ·  Gained: +{fmt_usd(lp_gain_total)}" if lp_gain_total > 0.005 else ""
                _b = _b.field(f"🌊 LP Positions  (≈{fmt_usd(lp_value)}{_lp_header_gain})", "\n".join(lp_lines[:15]) or " - ", False)
            if total_hr > 0:
                rig_lines = [
                    f"{Config.MINING_RIGS[r['rig_id']]['emoji']} {Config.MINING_RIGS[r['rig_id']]['name']} × {r['quantity']}"
                    for r in rigs if r["rig_id"] in Config.MINING_RIGS and r["quantity"] > 0
                ]
                _b = (
                    _b.field("⛏ Mode",         f"**{mine_mode}**",                                    True)
                      .field("📡 Hashrate",     f"**{fmt_bonus(f'{total_hr:,} MH/s', _item_stat(hashstone, 'mining_bonus'))}**", True)
                      .field("☀ SUN (DeFi)",    f"{sun_bal:.6f}  ≈ ${sun_usd_value:,.2f}",            True)
                )
                if rig_lines:
                    _b = _b.field("🖥️ Rigs", "\n".join(rig_lines), False)
            categories["🌊 LP & Mining"] = [_b.build()]

        # ⛓️ PoS Validator  -  identical to /stake validator stats
        if pos_validators:
            _vb = card("⛓️ PoS Validator Stats", color=C_PURPLE).author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            for _pv in pos_validators:
                _pv_status = "✅ Active" if _pv["is_active"] else "❌ Inactive"
                _pv_lock = "✅ Unlocked"
                _pv_slu = _pv.get("stake_locked_until")
                _pv_slu_ts = _pv_slu.timestamp() if hasattr(_pv_slu, "timestamp") else _pv_slu
                if _pv_slu_ts and time.time() < _pv_slu_ts:
                    _rem = int(_pv_slu_ts - time.time())
                    _pv_lock = f"🔒 {_rem//3600}h {(_rem%3600)//60}m left"
                _slashes = _pv.get("slash_count", 0)
                _slash_str = f"⚠️ {_slashes}/{MAX_SLASH_COUNT}" if _slashes > 0 else "✅ Clean"
                _vb.field(f"🔗 {_pv.get('network','Unknown')}", _pv_status, True)
                _vb.field("💎 Stake",     f"**{to_human(_pv['stake_amount']):,.4f}** {_pv['stake_token']}",  True)
                _vb.field("🏆 Blocks",    f"**{_pv['total_blocks_validated']:,}** validated",                True)
                _vb.field("💰 Earned",    f"**{to_human(_pv['total_rewards_earned']):,.4f} USD**",            True)
                _vb.field("🛡 Slashes",   _slash_str,                                                True)
                _vb.field("🔒 Lock",      _pv_lock,                                                  True)
            _vb.footer(".stake validator stats for details  |  .stake validator unregister to exit")
            categories["⛓️ Validator"] = [_vb.build()]

        # 💰 Savings  -  identical to /bank savings
        if _usd_b > 0:
            _sb = (
                card("💰 Savings Accounts", color=C_INFO)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            )
            _vs_int_bonus = _item_stat(vaultstone, "interest_bonus")
            _sb.field(
                "💵 USD Savings",
                (
                    f"**Balance:** ${_usd_b:,.2f}\n"
                    f"**APY:** {fmt_bonus(f'{_usd_save_daily * 365:.1f}%/yr', _vs_int_bonus)}  ({_usd_save_daily * 100:.3f}%/day)\n"
                    f"**Est. daily:** +${_usd_b * _usd_save_daily:,.4f}"
                ),
                True,
            )
            _sb.field(
                "📊 USD Pool", f"**Deposited:** ${to_human(_usd_total_dep):,.2f}\n**Borrowed:** ${to_human(_usd_total_bor):,.2f}\n**Utilization:** {utilization_str(_usd_util)}", True,
            ).footer("Interest auto-compounds hourly  |  /bank savings deposit to earn")
            categories["💰 Savings"] = [_sb.build()]

        # 🏦 Lending  -  identical to /bank loan status
        if loan_liability > 0:
            _loan_out = to_human(loan["outstanding"])
            _loan_col = to_human(loan["collateral"])
            _ltv = _loan_out / _loan_col * 100 if _loan_col > 0 else 0
            _danger = _ltv >= 80
            _daily_interest = _loan_out * _usd_borrow_daily
            _lb = (
                card("🏦 USD Loan", color=C_ERROR if _danger else C_INFO)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("💳 Principal",          f"**${to_human(loan['principal']):,.2f}**",           True)
                .field("📋 Outstanding",         f"**${_loan_out:,.2f}**",                             True)
                .field("🔒 Collateral (Locked)", f"**${_loan_col:,.2f}**",                             True)
                .field("📊 LTV",                 f"**{_ltv:.1f}%** {'HIGH RISK' if _danger else ''}",  True)
                .field("📈 Borrow APY",           f"**{_usd_borrow_daily*365:.1f}%/yr**  (dynamic)",   True)
                .field("💸 Est. Daily Cost",      f"**${_daily_interest:,.4f}**/day",                   True)
                .field("⚠️ Liquidation At",      f"LTV ≥ **{_L['LIQUIDATION_THRESHOLD']*100:.0f}%**",                    True)
                .footer("/bank loan repay [amount|all] to reduce your loan")
                .timestamp()
            )
            if _usd_save_daily > 0:
                _lb.field("💡 Savers Earn", f"**{_usd_save_daily*365:.1f}%/yr**  •  `/bank savings deposit`", True)
            categories["🏦 USD Loan"] = [_lb.build()]

        # 🤝 Delegations
        if delegation_lines:
            del_e = (
                card(f"🤝 Delegations  (≈${delegation_value:,.2f})", color=C_PURPLE)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .description("\n".join(delegation_lines[:15]))
                .footer(".mydelegations for lock status  |  .vundelegate to withdraw")
                .build()
            )
            categories["🤝 Delegations"] = [del_e]

        # 🎮 Games -- combined dropdown across every minigame surface
        # (Fishing / Farming / Delve / Crafting / Buddy / Gamba / Sage).
        # Lets a player see "what do I own across all the games?" in one
        # embed without flipping between 7 dropdowns. Per-game sections
        # below stay as their own dropdowns when they have content, so
        # this is additive -- no duplication on the summary side.
        try:
            from core.framework.scale import to_human as _to_human
            _g_lines: list[str] = []
            _g_subtotal: float = 0.0

            # Fishing (LURE+REEL wallet is in defi_wallet; stake_value
            # captures the staked LURE + accrued REEL yield).
            _fish_total = float(nw.fishing_stake_value)
            if _fish_total > 0:
                _g_lines.append(
                    f"\U0001F3A3 **Fishing**  ·  ≈ ${_fish_total:,.2f}  "
                    f"*(LURE stake + REEL drip)*"
                )
                _g_subtotal += _fish_total

            # Farming (stake + plots + inventory).
            _farm_total = (
                float(nw.farming_stake_value)
                + float(nw.farming_plot_value)
                + float(nw.farming_inventory_value)
            )
            if _farm_total > 0:
                _g_lines.append(
                    f"\U0001F33E **Farming**  ·  ≈ ${_farm_total:,.2f}  "
                    f"*(SEED stake + HRV drip + plots + crops)*"
                )
                _g_subtotal += _farm_total

            # Delve (stake + captured-party value).
            _delve_total = float(nw.delve_stake_value) + float(nw.delve_party_value)
            if _delve_total > 0:
                _g_lines.append(
                    f"\U0001F5FA **Delve**  ·  ≈ ${_delve_total:,.2f}  "
                    f"*(ore stake + RUNE drip + captured party)*"
                )
                _g_subtotal += _delve_total

            # Crafting (stake + crafted inventory).
            _craft_total = (
                float(nw.crafting_stake_value)
                + float(nw.crafting_inventory_value)
            )
            if _craft_total > 0:
                _g_lines.append(
                    f"\U0001F528 **Crafting**  ·  ≈ ${_craft_total:,.2f}  "
                    f"*(INGOT stake + FORGE drip + crafted items)*"
                )
                _g_subtotal += _craft_total

            # Buddy (FREN stake + BUD drip + slot sink).
            _buddy_total = float(nw.buddy_economy_value)
            if _buddy_total > 0:
                _g_lines.append(
                    f"\U0001F436 **Buddy**  ·  ≈ ${_buddy_total:,.2f}  "
                    f"*(FREN/BBT stake + BUD drip + slot sink)*"
                )
                _g_subtotal += _buddy_total

            # Gamba (game-token stakes + pending GBC/BUD yield).
            _gamba_total = float(nw.gamba_stake_value)
            if _gamba_total > 0:
                _g_lines.append(
                    f"\U0001F3B0 **Gamba**  ·  ≈ ${_gamba_total:,.2f}  "
                    f"*(8 game-token stakes + GBC/BUD drip)*"
                )
                _g_subtotal += _gamba_total

            # Sage (EDU stake + pending SAGE yield).
            _sage_total = float(nw.sage_stake_value)
            if _sage_total > 0:
                _g_lines.append(
                    f"\U0001F4DA **Sage**  ·  ≈ ${_sage_total:,.2f}  "
                    f"*(EDU stake + SAGE drip)*"
                )
                _g_subtotal += _sage_total

            # EatChain (staked $EAT in the ,eat minigame).
            _eat_total = float(nw.eat_stake_value)
            if _eat_total > 0:
                _g_lines.append(
                    f"\U0001F37D **EatChain**  ·  ≈ ${_eat_total:,.2f}  "
                    f"*(staked $EAT)*"
                )
                _g_subtotal += _eat_total

            # Items inventory (leveled stones live here too -- they back
            # in-game power for the games above).
            _items_total = float(nw.items_value)
            if _items_total > 0:
                _g_lines.append(
                    f"\U0001F392 **Items / Stones**  ·  ≈ ${_items_total:,.2f}  "
                    f"*(hash / lock / vault / gamba / liq stakes)*"
                )
                _g_subtotal += _items_total

            if _g_lines:
                _p = ctx.prefix or Config.PREFIX
                _games_b = (
                    card(
                        f"🎮 Games  ·  ≈ ${_g_subtotal:,.2f}",
                        description=(
                            "Everything you own across every game surface, "
                            "combined into one view.\n"
                        ),
                        color=C_INFO,
                    )
                    .author(
                        ctx.author.display_name,
                        icon_url=ctx.author.display_avatar.url,
                    )
                    .field(
                        "By Game",
                        _truncate_desc("\n".join(_g_lines)) or "—",
                        False,
                    )
                    .field(
                        "Σ Across all games",
                        f"**${_g_subtotal:,.2f}**",
                        False,
                    )
                    .footer(
                        f"Per-game dropdowns below for drill-down  ·  "
                        f"{_p}fish stake / {_p}farm stake / {_p}delve stake / "
                        f"{_p}craft stake / {_p}buddy stake / {_p}gamba stake / "
                        f"{_p}sage stake"
                    )
                    .build()
                )
                categories["🎮 Games"] = [_games_b]
        except Exception:
            log.debug("games combined dropdown failed", exc_info=True)

        # 🎢 Disc.Fun (DFUN wallet + active proto positions + stakes)
        # Always render the section if the user has ANY Disc.Fun exposure:
        # a DFUN balance in their crypto wallet, an active proto position
        # on the bonding curve, or a staked graduated token with pending
        # DFUN yield. The previous gate (`nw.disc_fun_value > 0`) hid the
        # whole dropdown when a user had only DFUN balance and no proto
        # positions -- "0 dfun" on a non-empty wallet was the symptom.
        try:
            from services import discfun as _disc_fun
            _df_rows = await _disc_fun.list_user_proto_holdings(ctx.db, gid, uid)
        except Exception:
            _df_rows = []
        # User's raw DFUN balance (sits in crypto_holdings as a regular token).
        try:
            _dfun_holding = await ctx.db.get_holding(uid, gid, "DFUN")
            _dfun_balance = float(_dfun_holding.get("amount", 0.0) or 0.0) if _dfun_holding else 0.0
        except Exception:
            _dfun_balance = 0.0
        # DFUN-denominated stake value + pending yield.
        try:
            _df_staked_dfun, _df_pending_dfun = await _disc_fun.user_staked_value_dfun(
                ctx.db, gid, uid,
            )
        except Exception:
            _df_staked_dfun, _df_pending_dfun = 0.0, 0.0
        # DFUN-denominated value of active proto positions.
        _df_active = [r for r in _df_rows if not r["graduated"]]
        _df_graduated = [r for r in _df_rows if r["graduated"]]
        _df_proto_lines: list[str] = []
        _df_active_dfun = 0.0
        for r in _df_active:
            held = r.h("amount")
            v_q = int(r["virtual_quote"])
            v_t = int(r["virtual_token"])
            spot = (v_q / v_t) if v_t else 0.0
            value_q = held * spot
            _df_active_dfun += value_q
            _df_proto_lines.append(
                f"{r['emoji']} **{r['symbol']}**  ·  `{held:,.4f}` @ "
                f"`{spot:.6e}`  ≈ `{value_q:,.4f} DFUN`"
            )
        _df_grad_lines: list[str] = []
        for r in _df_graduated:
            held = r.h("amount")
            _df_grad_lines.append(
                f"{r['emoji']} **{r['symbol']}**  ·  `{held:,.4f}`  *(graduated)*"
            )
        # Only render if SOMETHING is non-zero.
        _has_any = (
            _dfun_balance > 0
            or _df_active_dfun > 0
            or _df_staked_dfun > 0
            or _df_pending_dfun > 0
            or _df_graduated
        )
        if _has_any:
            _dfun_price_row = await ctx.db.get_price("DFUN", gid)
            _dfun_usd = (
                float(_dfun_price_row["price"]) if _dfun_price_row
                else float(Config.TOKENS.get("DFUN", {}).get("start_price", 0.10) or 0.10)
            )
            _df_total_dfun = (
                _dfun_balance + _df_active_dfun + _df_staked_dfun + _df_pending_dfun
            )
            _df_usd_total = _df_total_dfun * _dfun_usd

            _builder = (
                card(
                    f"🎢 Disc.Fun  ·  ≈ ${_df_usd_total:,.2f}",
                    color=C_GOLD,
                )
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            )
            # Headline line: actual DFUN holdings (this is the field that
            # was always reading 0 before -- pull from the crypto_holdings
            # row, not from the proto-position curve aggregate).
            _builder = _builder.field(
                "💵 DFUN balance",
                f"`{_dfun_balance:,.4f} DFUN`",
                True,
            )
            _builder = _builder.field(
                "🌱 Staked",
                (
                    f"`{_df_staked_dfun:,.4f} DFUN`"
                    if _df_staked_dfun > 0 else "—"
                ),
                True,
            )
            _builder = _builder.field(
                "🪙 Pending yield",
                (
                    f"`{_df_pending_dfun:,.4f} DFUN`"
                    if _df_pending_dfun > 0 else "—"
                ),
                True,
            )
            if _df_proto_lines:
                _builder = _builder.field(
                    "🚀 Active proto positions",
                    _truncate_desc("\n".join(_df_proto_lines[:10])),
                    False,
                )
            if _df_grad_lines:
                _builder = _builder.field(
                    "🎓 Graduated holdings",
                    _truncate_desc("\n".join(_df_grad_lines[:10])),
                    False,
                )
            _builder = _builder.field(
                "Σ Total (DFUN)",
                f"`{_df_total_dfun:,.4f} DFUN`  ≈ **${_df_usd_total:,.2f}**",
                False,
            ).footer(
                f"{ctx.prefix or Config.PREFIX}fun bag for full PnL  |  "
                f"{ctx.prefix or Config.PREFIX}fun list for hot launches"
            )
            categories["🎢 Disc.Fun"] = [_builder.build()]

        # 📚 Sage Network (SAGE + EDU wallet + EDU stake + bests).
        try:
            from services import sage as _sage_svc
            _sage_balance = await _sage_svc.get_sage_wallet_raw(ctx.db, gid, uid)
            _edu_balance = await _sage_svc.get_edu_wallet_raw(ctx.db, gid, uid)
            _sage_stake = await _sage_svc.get_stake(ctx.db, gid, uid)
            _sage_pending = await _sage_svc.accrued_yield(ctx.db, gid, uid)
            _sage_prog = await _sage_svc.get_progress(ctx.db, gid, uid)
        except Exception:
            _sage_balance = 0
            _edu_balance = 0
            _sage_stake = None
            _sage_pending = 0
            _sage_prog = None
        _has_sage = (
            (_sage_balance and _sage_balance > 0)
            or (_edu_balance and _edu_balance > 0)
            or (_sage_stake and _sage_stake.staked_raw > 0)
            or (_sage_pending and _sage_pending > 0)
            or (_sage_prog and (_sage_prog.lifetime_runs > 0 or _sage_prog.sage_level > 1))
        )
        if _has_sage:
            from core.framework.scale import to_human as _to_human
            _sage_h = _to_human(int(_sage_balance or 0))
            _edu_h = _to_human(int(_edu_balance or 0))
            _stake_h = _to_human(int(_sage_stake.staked_raw if _sage_stake else 0))
            _pending_h = _to_human(int(_sage_pending or 0))
            _sage_row = await ctx.db.get_price("SAGE", gid)
            _sage_usd = (
                float(_sage_row["price"]) if _sage_row
                else float(Config.TOKENS.get("SAGE", {}).get("start_price", 1.0) or 1.0)
            )
            _edu_row = await ctx.db.get_price("EDU", gid)
            _edu_usd = (
                float(_edu_row["price"]) if _edu_row
                else float(Config.TOKENS.get("EDU", {}).get("start_price", 0.10) or 0.10)
            )
            _sage_total_usd = (
                _sage_h * _sage_usd + (_edu_h + _stake_h) * _edu_usd + _pending_h * _sage_usd
            )
            _sb = (
                card(
                    f"📚 Sage Network  ·  ≈ ${_sage_total_usd:,.2f}",
                    color=C_GOLD,
                )
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("💠 SAGE balance", f"`{_sage_h:,.4f} SAGE`", True)
                .field("🎓 EDU balance", f"`{_edu_h:,.4f} EDU`", True)
                .field("🔐 EDU staked", f"`{_stake_h:,.4f} EDU`", True)
                .field(
                    "🪙 Pending yield",
                    f"`{_pending_h:,.6f} SAGE`" if _pending_h > 0 else "—",
                    True,
                )
            )
            if _sage_prog is not None:
                _sb = _sb.field(
                    "📈 Sage Level",
                    f"Lv **{_sage_prog.sage_level}**",
                    True,
                ).field(
                    "🎯 Bests",
                    (
                        f"`{_sage_prog.best_pattern_streak}` pattern · "
                        f"`{_sage_prog.best_gauge_streak}` gauge · "
                        f"`{_sage_prog.best_tknom_streak}` tknom"
                    ),
                    True,
                )
            _sb = _sb.footer(
                f"{ctx.prefix or Config.PREFIX}pattern · "
                f"{ctx.prefix or Config.PREFIX}gauge · "
                f"{ctx.prefix or Config.PREFIX}tknom  |  "
                f"{ctx.prefix or Config.PREFIX}sage stake / claim / cashout"
            )
            categories["📚 Sage"] = [_sb.build()]

        # Position management quick-action hints shown as buttons in relevant categories
        _p = ctx.prefix or Config.PREFIX
        _balance_action_hints: dict[str, list[tuple[str, str, str]]] = {
            "🌊 LP & Mining": [
                ("Add LP", "➕", f"**Add liquidity:** `{_p}pool add TOKEN1/TOKEN2 <amount>`\nExample: `{_p}pool add ARC/USD 500`"),
                ("Remove LP", "➖", f"**Remove liquidity:** `{_p}pool remove TOKEN1/TOKEN2 <amount>`\nExample: `{_p}pool remove ARC/USD all`"),
                ("Mine", "⛏️", f"**Start mining:** `{_p}mine start`\nView rigs: `{_p}my rigs`"),
            ],
            "🌐 Nodes": [
                ("Stake", "🔒", f"**Stake tokens:** `{_p}stake <amount> <token> <validator>`\nExample: `{_p}stake 100 ARC LIDO`"),
                ("Unstake", "🔓", f"**Unstake tokens:** `{_p}unstake <validator>`\nExample: `{_p}unstake LIDO`\n⚠️ Early unstake (< 48h) has a 5% penalty."),
            ],
            "⛓️ Validator": [
                ("Status", "📊", f"**Check your validator:** `{_p}stake validator stats`", "stake validator stats"),
                ("Deregister", "🚪", f"**Unregister validator:** `{_p}stake validator unregister <network>`"),
            ],
            "💰 Savings": [
                ("Deposit", "📥", f"**Deposit to savings:** `{_p}savings deposit <amount>`"),
                ("Withdraw", "📤", f"**Withdraw from savings:** `{_p}savings withdraw <amount>`"),
            ],
            "🏦 USD Loan": [
                ("Borrow", "💳", f"**Borrow USD:** `{_p}bank loan borrow <amount>`"),
                ("Repay", "💵", f"**Repay loan:** `{_p}bank loan repay <amount|all>`"),
            ],

            "🔐 DeFi": [
                ("Deposit", "📥", f"**Deposit to DeFi wallet:** `{_p}bank deposit <token> <amount>`"),
                ("Withdraw", "📤", f"**Withdraw from DeFi:** `{_p}bank withdraw <token> <amount>`"),
            ],
            "📈 Crypto": [
                ("Buy", "📥", f"**Buy crypto:** `{_p}buy <amount> <token>`\nExample: `{_p}buy 10 ARC`"),
                ("Sell", "📤", f"**Sell crypto:** `{_p}sell <amount> <token>`\nExample: `{_p}sell 10 ARC`"),
                ("Deposit", "🔐", f"**Move to DeFi:** `{_p}move <amount> <token> b w`"),
                ("Withdraw", "🏦", f"**Move to CeFi:** `{_p}move <amount> <token> w b`"),
            ],
            "🎒 Items": [
                ("Shop", "🛍️", f"**Browse shop:** `{_p}shop`", "shop"),
                ("Use", "✨", f"**Use an item:** `{_p}use <item>`"),
                ("Inventory", "🎒", f"**View inventory:** `{_p}inventory`", "inventory"),
            ],
        }
        await CategoryPaginator.send(ctx, categories, action_hints=_balance_action_hints)

        # Drill-down selector: appears below the paginator when user has stakes/delegations/validators
        _drill = ValidatorSelectView(
            ctx.author.id,
            stakes=stakes,
            delegations=delegations,
            pos_validators=pos_validators,
            db=ctx.db,
            guild_id=gid,
        )
        if _drill.has_options:
            _drill_embed = (
                card("🔍 Position Details", color=C_NAVY)
                .description("Select a node, delegation, or validator below to view details.")
                .build()
            )
            await ctx.reply(embed=_drill_embed, view=_drill, mention_author=False)

    # ── $notify ────────────────────────────────────────────────────────────────

    _NOTIFY_KEYS = {
        "mining":       "dm_mining",
        "transfer":     "dm_transfer",
        "validator":    "dm_validator",
        "staking":      "dm_staking",
        "itemlevelup":  "dm_itemlevelup",
        "autolevelup":  "dm_autolevelup",
        "whalealerts":  "dm_whale_alerts",
        "2fa":          "dm_2fa",
        "events":       "dm_events",
        "nft":          "dm_nft",
        "predictions":  "dm_predictions",
    }
    _NOTIFY_DISPLAY = {
        "mining":       "Mining",
        "transfer":     "Transfer",
        "validator":    "Validator",
        "staking":      "Staking",
        "itemlevelup":  "Item Level Up",
        "autolevelup":  "Auto Level-Up",
        "whalealerts":  "Whale Alerts",
        "2fa":          "2FA / Security",
        "events":       "Market Events",
        "nft":          "NFTs",
        "predictions":  "Predictions",
    }

    # Categories that support per-network muting
    _NETWORK_MUTE_CATS = {"mining", "staking", "validator", "whalealerts"}
    _NETWORK_MUTE_DB_KEYS = {
        "mining": "mining", "staking": "staking",
        "validator": "validator", "whalealerts": "whale",
    }

    @commands.hybrid_command(name="notify", aliases=["notifications"], with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def notify(self, ctx: DiscoContext, category: str = "", state: str = "", network: str = "") -> None:
        """Manage your DM notification preferences.

        Usage:
          .notify                               -  show current settings
          .notify <category> on|off             -  toggle a category
          .notify <category> <network> on|off   -  toggle a specific network within a category
        Categories: mining, transfer, validator, staking, itemlevelup, whalealerts
        """
        prefs = await ctx.db.get_user_prefs(ctx.author.id, ctx.guild_id)

        if not category:
            # Show current prefs. Every DM toggle is opt-in (default
            # OFF -- see migration 0208_notify_default_off.sql) so the
            # fallback is 0, not 1, when a column is somehow missing.
            _b = card("🔔 Your DM Notifications", color=C_INFO)
            for label, col in self._NOTIFY_KEYS.items():
                val = prefs.get(col, 0)
                display = self._NOTIFY_DISPLAY.get(label, label.title())
                status_str = "On" if val else "Off"
                # Show muted networks inline for applicable categories
                db_key = self._NETWORK_MUTE_DB_KEYS.get(label)
                if db_key and val:
                    muted = await ctx.db.get_muted_networks(ctx.author.id, ctx.guild_id, db_key)
                    if muted:
                        status_str += f"\nMuted: {', '.join(sorted(muted))}"
                _b = _b.field(f"{'✅' if val else '❌'} {display}", status_str, True)
            embed = _b.footer(
                "All notifications are off by default -- opt in with "
                ",notify <category> on\n"
                "Per-network: ,notify <category> <network> on|off"
            ).build()
            await ctx.reply(embed=embed, mention_author=False)
            return

        cat = category.lower()
        col = self._NOTIFY_KEYS.get(cat)
        if not col:
            valid = ", ".join(f"`{k}`" for k in self._NOTIFY_KEYS)
            await ctx.reply_error(f"Unknown category `{category}`. Valid: {valid}")
            return

        # Check if 'state' is actually a network name (e.g. .notify mining arc off)
        if state and cat in self._NETWORK_MUTE_CATS and state.lower() not in ("on", "off", "enable", "disable", "1", "0", "true", "false"):
            # state is a network name, network is the actual state
            net_name = state.lower()
            net_state = network.lower() if network else ""
            db_key = self._NETWORK_MUTE_DB_KEYS[cat]

            if not net_state:
                # Toggle
                is_muted = await ctx.db.toggle_muted_network(ctx.author.id, ctx.guild_id, db_key, net_name)
                status = "muted ❌" if is_muted else "unmuted ✅"
            elif net_state in ("off", "disable", "0", "false", "mute"):
                muted = await ctx.db.get_muted_networks(ctx.author.id, ctx.guild_id, db_key)
                muted.add(net_name)
                await ctx.db.set_muted_networks(ctx.author.id, ctx.guild_id, db_key, muted)
                status = "muted ❌"
            elif net_state in ("on", "enable", "1", "true", "unmute"):
                muted = await ctx.db.get_muted_networks(ctx.author.id, ctx.guild_id, db_key)
                muted.discard(net_name)
                await ctx.db.set_muted_networks(ctx.author.id, ctx.guild_id, db_key, muted)
                status = "unmuted ✅"
            else:
                await ctx.reply_error("State must be `on` or `off`.")
                return
            display = self._NOTIFY_DISPLAY.get(cat, cat.title())
            await ctx.reply_success(
                f"**{display}** notifications for network **{net_name}** are now **{status}**.",
                title="Network Notification Updated",
            )
            return

        if not state:
            # Just toggle. Default is OFF (opt-in) so a fresh row with
            # no value reads as 0; toggle-without-state flips that to 1.
            current = prefs.get(col, 0)
            value = 0 if current else 1
        elif state.lower() in ("on", "enable", "1", "true"):
            value = 1
        elif state.lower() in ("off", "disable", "0", "false"):
            value = 0
        else:
            await ctx.reply_error("State must be `on` or `off`.")
            return

        await ctx.db.set_user_pref(ctx.author.id, ctx.guild_id, col, value)
        status = "enabled ✅" if value else "disabled ❌"
        await ctx.reply_success(
            f"**{cat.title()}** DM notifications are now **{status}**.",
            title="Notification Updated",
        )

    # ── $wallet ────────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="wallet", invoke_without_command=True, with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def wallet_cmd(self, ctx: DiscoContext) -> None:
        """Manage your on-chain wallet addresses. Subcommands: create, list, delete, info"""
        if await suggest_subcommand(ctx, self.wallet_cmd):
            return
        await ctx.send_help(ctx.command)

    @wallet_cmd.command(name="create")
    @guild_only
    @no_bots
    @ensure_registered
    async def wallet_create(self, ctx: DiscoContext, network: str, *, label: str = "") -> None:
        """Create a wallet on a specific network. Usage: .wallet create <arc|sol|bnb|sun> [label]
        Wallets are network-scoped  -  only tokens from that network can be sent/received."""
        net_key = network.lower()
        if net_key not in _NETWORK_SHORTS:
            valid = " | ".join(f"`{k}`" for k in _NETWORK_SHORTS)
            await ctx.reply_error(
                f"Unknown network `{network}`. Valid: {valid}\n"
                f"Example: `.wallet create arc My ARC Wallet`"
            )
            return

        network_name = _NETWORK_SHORTS[net_key]
        clean_label  = label.strip() or None
        if clean_label and len(clean_label) > 50:
            await ctx.reply_error("Label must be 50 characters or fewer.")
            return

        already_has = await ctx.db.has_defi_wallet(ctx.author.id, ctx.guild_id, net_key)
        if already_has:
            existing = await ctx.db.get_defi_wallet_address(ctx.author.id, ctx.guild_id, net_key)
            await ctx.reply_error(
                f"You already have a **{network_name}** wallet: `{existing['address']}`\n"
                "Only 1 wallet per network is allowed. Delete it first with `.wallet delete <address>`."
            )
            return

        address = await ctx.db.create_wallet_address(
            ctx.author.id, ctx.guild_id,
            label=clean_label, is_temp=False,
            network=network_name, address_prefix=net_key,
        )

        # Log wallet creation as a transaction so it appears in explorer
        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "WALLET_CREATE",
            symbol_in="WALLET", amount_in=0,
            network=net_key,
        )

        # Include custom tokens registered for this network
        tokens_on_net = await ctx.db.get_network_accepted_tokens(ctx.guild_id, network_name)
        embed = (
            card(f"✅ Wallet Created  -  {network_name}", color=C_SUCCESS)
            .field("Address",         f"`{address}`",                           False)
            .field("Network",         network_name,                             True)
            .field("Label",           clean_label or " - ",                       True)
            .field("Tokens",          ", ".join(tokens_on_net) or "USD only",   True)
            .footer(f"Use .send {address} <token> <amount> to receive tokens")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)
        await ctx.bot.bus.publish(
            "wallet_created",
            guild=ctx.guild, user=ctx.author,
            network=network_name, address=address, label=clean_label or "",
        )

    @wallet_cmd.command(name="list", aliases=["ls"])
    @guild_only
    @no_bots
    @ensure_registered
    async def wallet_list(self, ctx: DiscoContext) -> None:
        """List all your wallet addresses."""
        addresses = await ctx.db.get_user_addresses(ctx.author.id, ctx.guild_id)
        if not addresses:
            await ctx.reply_error_action(
                "You have no wallet addresses.",
                "Create SUN Wallet",
                "wallet create sun",
                rerun_original=True,
            )
            return

        import time as _time
        _short_map = _FULL_TO_SHORT
        _b = card("Your Wallets", color=C_INFO)
        now = _time.time()
        for addr in addresses:
            if addr.get("is_temp") and addr.get("expires_at"):
                remaining = addr["expires_at"] - now
                if remaining <= 0:
                    exp_str = "Expired"
                else:
                    h_r, rem = divmod(int(remaining), 3600)
                    exp_str = f"Expires in {h_r}h {rem//60}m"
            else:
                exp_str = "Permanent"
            label = addr.get("label") or " - "
            ts = fmt_ts(addr["created_at"], "%Y-%m-%d")
            net_full = addr.get("network", "")
            net_short = _short_map.get(net_full, "")
            # Fetch DeFi holdings for this wallet's network
            holdings_str = ""
            if net_short:
                holdings = await ctx.db.get_wallet_holdings_for_network(ctx.author.id, ctx.guild_id, net_short)
                if holdings:
                    holdings_str = "  |  " + "  ".join(f"{to_human(h['amount']):,.4f} {h['symbol']}" for h in holdings)
                else:
                    holdings_str = "  |  Empty"
            net_label = f"  |  {net_full}" if net_full else ""
            _b = _b.field(f"`{addr['address']}`", f"Label: {label}{net_label}{holdings_str}  |  {exp_str}  |  {ts}", False)
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @wallet_cmd.command(name="delete", aliases=["del", "rm"])
    @guild_only
    @no_bots
    @ensure_registered
    async def wallet_delete(self, ctx: DiscoContext, address: str) -> None:
        """Delete one of your wallet addresses. Usage: $wallet delete <address>"""
        deleted = await ctx.db.delete_wallet_address(address, ctx.author.id)
        if not deleted:
            await ctx.reply_error("Address not found or you don't own it.")
            return
        # Log wallet deletion in transaction explorer
        net_key = address.split(":")[0] if ":" in address else "sun"
        await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "WALLET_DELETE",
            symbol_in="WALLET", amount_in=0,
            network=net_key,
        )
        await ctx.reply_success(f"Address `{address}` deleted.", title="Address Deleted")

    @wallet_cmd.command(name="info")
    @guild_only
    async def wallet_info(self, ctx: DiscoContext, address: str = "") -> None:
        """Look up who owns a wallet address, or show your own wallets when called without an argument.
        Usage: ,wallet info [address]"""
        if not address:
            await self.wallet_list(ctx)
            return
        row = await ctx.db.get_wallet_address(address)
        if not row:
            await ctx.reply_error("Address not found.")
            return

        import time as _time
        _ea = row.get("expires_at")
        _ea_ts = _ea.timestamp() if hasattr(_ea, 'timestamp') else _ea if _ea else None
        if row.get("is_temp") and _ea_ts and _ea_ts < _time.time():
            await ctx.reply_error("This address has expired.")
            return

        member = ctx.guild.get_member(row["user_id"])
        owner_str = member.mention if member else f"User {row['user_id']}"
        label = row.get("label") or " - "
        ts = fmt_ts(row["created_at"], "%Y-%m-%d %H:%M UTC")
        _b = (
            card("Address Info", color=C_NEUTRAL)
            .field("Address", f"`{address}`", True)
            .field("Owner",   owner_str,      True)
            .field("Label",   label,          True)
            .field("Created", ts,             True)
        )
        if row.get("network"):
            _b = _b.field("Network", row["network"], True)
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    @wallet_cmd.command(name="deposit", aliases=["dep"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def wallet_deposit(self, ctx: DiscoContext, arg1: str, arg2: str = "") -> None:
        """Move crypto from CeFi holdings TO your DeFi wallet. Accepts <token> <amount> or <amount> <token>.
        Platform fee applies. Example: .wallet deposit ARC 0.5  or  .wallet deposit 0.5 ARC"""
        if not arg2:
            await ctx.reply_error("Usage: `.wallet deposit <token> <amount>` or `.wallet deposit <amount> <token>`")
            return
        token, amount = parse_sym_amt(arg1, arg2)
        await self._crypto_withdraw(ctx, token, amount)

    @wallet_cmd.command(name="withdraw", aliases=["wd"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def wallet_withdraw(self, ctx: DiscoContext, arg1: str, arg2: str = "") -> None:
        """Move crypto FROM your DeFi wallet to CeFi holdings. Accepts <token> <amount> or <amount> <token>.
        Example: .wallet withdraw ARC 0.5  or  .wallet withdraw 0.5 ARC"""
        if not arg2:
            await ctx.reply_error("Usage: `.wallet withdraw <token> <amount>` or `.wallet withdraw <amount> <token>`")
            return
        token, amount = parse_sym_amt(arg1, arg2)
        await self._crypto_deposit(ctx, token, amount)

    # ── $send ──────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="send", with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    async def send_to_address(
        self, ctx: DiscoContext, target: str, amount: str, network: str = "", token: str = ""
    ) -> None:
        """Send tokens from your DeFi wallet to another user's DeFi wallet.
        Usage: .send <@user|username|user_id> <amount> <network> [token]
               .send <wallet_address> <amount> [token]
        Examples: .send @Lleywyn 5 arc   .send @user $25 arc
                  .send arc:abc123def456 5
                  .send @Lleywyn 5 dsc DSY
        Token defaults to the native network token (ARC/DSC/SUN/MTA)."""
        # Parse amount  -  handle $-prefixed, plain numbers, and "all"
        is_all = str(amount).lower() == "all"
        amt = 0.0
        if not is_all:
            clean_amount = str(amount).lstrip("$").replace(",", "")
            try:
                amt = float(clean_amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `$<amount>`, or `all`.")
                return
            if not math.isfinite(amt) or amt <= 0:
                await ctx.reply_error("Amount must be a positive number.")
                return

        recipient_id: int | None = None
        net_short: str = ""

        # ── Mode A: wallet address (contains ":" and looks like a known address) ──
        addr_row = await ctx.db.get_wallet_address(target) if ":" in target else None
        if addr_row:
            recipient_id = addr_row["user_id"]
            # derive network from stored address network field
            net_short = next(
                (k for k, v in _NETWORK_SHORTS.items() if v == addr_row["network"]),
                target.partition(":")[0].lower(),
            )
        else:
            # ── Mode B: user target  -  network must be provided separately ──
            if not network:
                await ctx.reply_error(
                    "Specify a network when sending to a user.\n"
                    "Examples: `.send @Lleywyn 5 arc`  |  `.send arc:abc123 5`\n"
                    "Valid networks: arc, sol, bnb, sun, mta, avax, pol, atom, sui, apt, near"
                )
                return

            net_short = network.lower()
            if net_short not in _NETWORK_SHORTS:
                await ctx.reply_error(
                    f"Unknown network `{network}`. Valid: {', '.join(_NETWORK_SHORTS)}"
                )
                return

            # resolve target → user_id: mention, plain ID, or display name
            mention_match = re.fullmatch(r"<@!?(\d+)>", target)
            if mention_match:
                recipient_id = int(mention_match.group(1))
            elif target.isdigit():
                recipient_id = int(target)
            else:
                # username / display name search
                name_lower = target.lstrip("@").lower()
                member = discord.utils.find(
                    lambda m: m.name.lower() == name_lower or m.display_name.lower() == name_lower,
                    ctx.guild.members,
                )
                if member:
                    recipient_id = member.id
                else:
                    await ctx.reply_error(
                        f"Could not find user `{target}` in this server.\n"
                        "Use a @mention, username, or user ID."
                    )
                    return

        if recipient_id == ctx.author.id:
            await ctx.reply_error("You can't send to yourself.")
            return

        network_name = _NETWORK_SHORTS[net_short]
        sym = token.upper() if token else _NET_NATIVE.get(net_short, "")
        if not sym:
            await ctx.reply_error(f"Could not determine token for network `{net_short}`.")
            return

        tok_cfg = Config.TOKENS.get(sym, {})
        if not tok_cfg:
            all_tok = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
            tok_cfg = all_tok.get(sym, {})
        emoji = tok_cfg.get("emoji", "●") if tok_cfg else "●"

        # 1. Verify sender has a wallet on this network
        sender_wallet = await ctx.db.get_defi_wallet_address(ctx.author.id, ctx.guild_id, net_short)
        if not sender_wallet:
            await ctx.reply_error_action(
                f"You don't have a **{network_name}** wallet.",
                f"Create {network_name} Wallet",
                f"wallet create {net_short}",
                rerun_original=True,
            )
            return

        # 2. Verify recipient has a wallet on this network
        recipient_wallet = await ctx.db.get_defi_wallet_address(recipient_id, ctx.guild_id, net_short)
        if not recipient_wallet:
            member = ctx.guild.get_member(recipient_id)
            name = member.display_name if member else f"User {recipient_id}"
            await ctx.reply_error(
                f"**{name}** doesn't have a **{network_name}** wallet.\n"
                "They need to create one with `.wallet create " + net_short + "`"
            )
            return

        # 3. Check sender's DeFi wallet balance
        holding = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, net_short, sym)
        balance = to_human(holding["amount"]) if holding else 0.0
        if is_all:
            amt = balance
            if amt <= 0:
                await ctx.reply_error(f"You have no **{emoji}{sym}** in your {network_name} wallet.")
                return
        if balance < amt:
            await ctx.reply_error(
                f"Insufficient wallet balance. You have **{balance:,.6f} {emoji}{sym}** in your {network_name} wallet.\n"
                f"Fund your wallet with `.wallet deposit {net_short} <amount>`"
            )
            return

        # 4. Apply token contract (transfer_fee, burn_rate)  -  always computed
        net_amount, burned = await ctx.db.apply_contract_transfer(ctx.guild_id, sym, amt)
        fee = amt - net_amount - burned

        # 5. Ensure recipient user row exists
        await ctx.db.ensure_user(recipient_id, ctx.guild_id)

        # 6. Check for active validators  -  route through mempool if present
        _all_v = await ctx.db.get_pos_validators_for_network(ctx.guild_id, network_name)
        _active_v = [v for v in _all_v if v["is_active"]]
        _has_pow = False
        if network_name == "Sun Network" and not _active_v:
            _all_rigs = await ctx.db.get_all_guild_rigs(ctx.guild_id)
            _has_pow = any(r["quantity"] > 0 for r in _all_rigs)
        _use_mempool = bool(_active_v or _has_pow)

        if _use_mempool:
            # Parse gas flag from raw message content (default medium)
            _raw_content = (ctx.message.content if ctx.message else "").lower()
            _gas_price = "medium"
            if "gas high" in _raw_content or "high" in _raw_content:
                _gas_price = "high"
            elif "gas low" in _raw_content or "low" in _raw_content:
                _gas_price = "low"

            _gas_coin, _gas_fee = await gas_fee_for_network(
                ctx.db, ctx.guild_id, "send", _gas_price, network_name
            )
            _gas_cfg = Config.TOKENS.get(_gas_coin, {})
            _gas_emoji = _gas_cfg.get("emoji", "●")
            _net_s = _V_NET_SHORT.get(network_name, "")
            _gas_h = (
                await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, _net_s, _gas_coin)
                if _net_s
                else await ctx.db.get_holding(ctx.author.id, ctx.guild_id, _gas_coin)
            )
            _gas_balance = to_human(_gas_h["amount"]) if _gas_h else 0.0
            if _gas_balance < _gas_fee:
                await ctx.reply_error(
                    f"Need **`{_gas_fee:,.6f} {_gas_emoji}{_gas_coin}`** for gas.\n"
                    f"Your balance: **`{_gas_balance:,.6f} {_gas_emoji}{_gas_coin}`**"
                )
                return

            # Lock tokens (debit send amount + gas fee from sender)
            await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, net_short, sym, to_raw(-amt))
            if _net_s:
                await ctx.db.update_wallet_holding(
                    ctx.author.id, ctx.guild_id, _net_s, _gas_coin, to_raw(-_gas_fee)
                )
            else:
                await ctx.db.update_holding(ctx.author.id, ctx.guild_id, _gas_coin, to_raw(-_gas_fee))

            # Submit to mempool
            _action_id = await ctx.db.add_to_mempool(
                guild_id=ctx.guild_id,
                network=network_name,
                user_id=ctx.author.id,
                action_type="send",
                payload={
                    "to_user_id": recipient_id,
                    "symbol": sym,
                    "amount": net_amount,   # post-contract amount recipient receives
                },
                gas_price=_gas_price,
                gas_fee=to_raw(_gas_fee),
            )

            member = ctx.guild.get_member(recipient_id)
            to_str = member.mention if member else f"User {recipient_id}"
            _q_embed = (
                card("⏳ Transfer Queued", color=C_INFO)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("To",       to_str,         True)
                .field("Network",  network_name,   True)
                .field("Amount",   f"{amt:,.6f} {emoji}{sym}", True)
                .field("Gas",      f"{_gas_fee:,.6f} {_gas_emoji}{_gas_coin} ({_gas_price})", True)
                .field("Action ID", str(_action_id), True)
                .footer("Transfer will execute in the next validator block (~120s). Tokens are locked.")
                .build()
            )
            await ctx.reply(embed=_q_embed, mention_author=False)
            return

        # ── Instant path (no active validators) ──────────────────────────────
        await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, net_short, sym, to_raw(-amt))
        await ctx.db.update_wallet_holding(recipient_id, ctx.guild_id, net_short, sym, to_raw(net_amount))

        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "SEND",
            symbol_in=sym, amount_in=to_raw(amt),
            symbol_out=sym, amount_out=to_raw(net_amount),
            price_at=None,
            network=net_short,
        )

        member = ctx.guild.get_member(recipient_id)
        to_str = member.mention if member else f"User {recipient_id}"

        def fmt(v: float) -> str:
            return f"{v:,.6f} {emoji}{sym}"

        _b = (
            card("📤 Transfer Sent", color=C_SUCCESS)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("From",     ctx.author.mention, True)
            .field("To",       to_str,              True)
            .field("Network",  network_name,        True)
            .field("Sent",     fmt(amt),             True)
            .field("Received", fmt(net_amount),     True)
        )
        if fee > 0 or burned > 0:
            parts_list = []
            if fee > 0:
                parts_list.append(f"Fee: {fmt(fee)}")
            if burned > 0:
                parts_list.append(f"Burned 🔥: {fmt(burned)}")
            _b = _b.field("Protocol", "  |  ".join(parts_list), True)
        result_embed = _b.build()
        set_tx(result_embed, ctx.guild_id, tx_hash)
        await ctx.reply(embed=result_embed, mention_author=False)
        await ctx.bot.bus.publish(
            "token_send",
            guild=ctx.guild,
            sender=ctx.author,
            to_address=recipient_wallet["address"],
            symbol=sym,
            amount=net_amount,
            tx_hash=tx_hash,
        )
        _usd = await _whale.usd_value_of(ctx.bot, sym, net_amount, ctx.guild_id)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "send", _usd, symbol=sym, amount=net_amount)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Bank(bot))
