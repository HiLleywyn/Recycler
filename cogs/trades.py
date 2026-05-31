from __future__ import annotations

import asyncio
import datetime

import discord
from discord.ext import commands, tasks

from core.framework.embed import card
from core.framework.heartbeat import pulse, register_interval

from core.config import Config
from constants.validators import MAX_SLASH_COUNT
from core.framework.scale import to_human
from core.framework.tx import set_tx
from core.framework.ai import complete as ai_complete, strip_links
from core.framework.bot import Discoin
from core.framework.ui import (
    C_BUY, C_SELL, C_INFO, C_GOLD, C_PURPLE, C_NEUTRAL, C_WARNING, C_AMBER, C_ERROR, C_SUCCESS,
    fmt_token, fmt_usd, fmt_pct, fmt_gas, fmt_ts, mention,
)

async def _usd_value_str(bot, symbol: str, amount: float, guild_id: int) -> str:
    """Return a string like '≈ $1,234.56' for a token amount, or '' if unknown."""
    if symbol == "USD":
        return f"**{fmt_usd(amount)}**"
    try:
        row = await bot.db.get_price(symbol, guild_id)
        if row and row["price"] > 0:
            usd = float(row["price"]) * amount
            return f"**{fmt_token(amount, symbol)}**  ≈ **{fmt_usd(usd)}**"
    except Exception:
        pass
    return f"**{fmt_token(amount, symbol)}**"

class Trades(commands.Cog):
    """Trade announcement feed and transaction lookup."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._stats: dict[int, dict] = {}
        bus = bot.bus
        bus.subscribe("trade",             self._on_trade)
        bus.subscribe("mm_trade",          self._on_mm_trade)
        bus.subscribe("oracle_rebalance",   self._on_oracle_rebalance)
        bus.subscribe("swap_trade",        self._on_swap_trade)
        bus.subscribe("lp_added",          self._on_lp_added)
        bus.subscribe("lp_removed",        self._on_lp_removed)
        bus.subscribe("staked",            self._on_staked)
        bus.subscribe("unstaked",          self._on_unstaked)
        bus.subscribe("validator_slashed", self._on_slash)
        bus.subscribe("validator_reward",  self._on_validator_reward)
        bus.subscribe("validator_block",   self._on_validator_block)
        bus.subscribe("gamble_result",     self._on_gamble)
        bus.subscribe("drop_claimed",      self._on_drop)
        bus.subscribe("mining_tick_complete", self._on_mining_tick_complete)
        bus.subscribe("pow_mining_tick", self._on_pow_mining_tick)
        bus.subscribe("block_bundled",     self._on_block_bundled)
        bus.subscribe("mine_rig_bought",   self._on_mine_buy)
        bus.subscribe("loan_liquidated",   self._on_liquidation)
        bus.subscribe("promoted",           self._on_promoted)
        bus.subscribe("daily_claimed",     self._on_daily)
        bus.subscribe("work_completed",    self._on_work)
        bus.subscribe("transfer",          self._on_transfer)
        bus.subscribe("token_send",        self._on_token_send)
        bus.subscribe("loan_opened",          self._on_loan_opened)
        bus.subscribe("loan_repaid",          self._on_loan_repaid)
        bus.subscribe("deposit",           self._on_deposit)
        bus.subscribe("withdraw",          self._on_withdraw)
        bus.subscribe("wallet_created",    self._on_wallet_created)
        bus.subscribe("crypto_withdraw",   self._on_crypto_withdraw)
        bus.subscribe("crypto_deposit",    self._on_crypto_deposit)
        bus.subscribe("contract_event",    self._on_contract_event)
        bus.subscribe("pos_validator_slashed", self._on_pos_validator_slashed)
        bus.subscribe("whale_alert", self._on_whale_alert)
        self.hourly_summary.start()
        register_interval("hourly_summary", 3600)

    def cog_unload(self) -> None:
        self.hourly_summary.cancel()
        bus = self.bot.bus
        bus.unsubscribe("trade",             self._on_trade)
        bus.unsubscribe("mm_trade",          self._on_mm_trade)
        bus.unsubscribe("oracle_rebalance",   self._on_oracle_rebalance)
        bus.unsubscribe("swap_trade",        self._on_swap_trade)
        bus.unsubscribe("lp_added",          self._on_lp_added)
        bus.unsubscribe("lp_removed",        self._on_lp_removed)
        bus.unsubscribe("staked",            self._on_staked)
        bus.unsubscribe("unstaked",          self._on_unstaked)
        bus.unsubscribe("validator_slashed", self._on_slash)
        bus.unsubscribe("validator_reward",  self._on_validator_reward)
        bus.unsubscribe("validator_block",   self._on_validator_block)
        bus.unsubscribe("gamble_result",     self._on_gamble)
        bus.unsubscribe("drop_claimed",      self._on_drop)
        bus.unsubscribe("mining_tick_complete", self._on_mining_tick_complete)
        bus.unsubscribe("pow_mining_tick", self._on_pow_mining_tick)
        bus.unsubscribe("block_bundled",     self._on_block_bundled)
        bus.unsubscribe("mine_rig_bought",   self._on_mine_buy)
        bus.unsubscribe("loan_liquidated",   self._on_liquidation)
        bus.unsubscribe("promoted",          self._on_promoted)
        bus.unsubscribe("daily_claimed",     self._on_daily)
        bus.unsubscribe("work_completed",    self._on_work)
        bus.unsubscribe("transfer",          self._on_transfer)
        bus.unsubscribe("token_send",        self._on_token_send)
        bus.unsubscribe("loan_opened",          self._on_loan_opened)
        bus.unsubscribe("loan_repaid",          self._on_loan_repaid)
        bus.unsubscribe("deposit",           self._on_deposit)
        bus.unsubscribe("withdraw",          self._on_withdraw)
        bus.unsubscribe("wallet_created",    self._on_wallet_created)
        bus.unsubscribe("crypto_withdraw",   self._on_crypto_withdraw)
        bus.unsubscribe("crypto_deposit",    self._on_crypto_deposit)
        bus.unsubscribe("contract_event",    self._on_contract_event)
        bus.unsubscribe("pos_validator_slashed", self._on_pos_validator_slashed)
        bus.unsubscribe("whale_alert", self._on_whale_alert)

    # ── Channel helpers ───────────────────────────────────────────────────────

    async def _get_channel(
        self, guild: discord.Guild, col: str
    ) -> discord.TextChannel | discord.Thread | None:
        settings = await self.bot.db.get_guild_settings(guild.id)
        ch_id = settings.get(col)
        if not ch_id:
            return None
        ch = guild.get_channel_or_thread(ch_id)
        if ch is None:
            # Thread/forum post may not be in cache  -  fetch from API
            try:
                ch = await self.bot.fetch_channel(ch_id)
            except Exception:
                return None
        # Accept text channels and threads (including forum post threads)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            return ch
        return None

    async def _send(
        self, guild: discord.Guild, col: str,
        content: str | None = None, embed: discord.Embed | None = None,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        if embed:
            try:
                from core.framework.links import LinkManager
                lm = LinkManager()
                lm.process_embed(embed)
            except Exception:
                pass
        feed_ch = await self._get_channel(guild, col)
        sent_ids: set[int] = set()
        if channel is not None:
            try:
                await channel.send(content=content, embed=embed)
                sent_ids.add(channel.id)
            except Exception:
                pass
        if feed_ch and feed_ch.id not in sent_ids:
            if isinstance(feed_ch, discord.Thread) and feed_ch.archived:
                try:
                    await feed_ch.edit(archived=False)
                except Exception:
                    pass
            try:
                await feed_ch.send(content=content, embed=embed)
            except (discord.HTTPException, OSError):
                pass

    def _stat(self, guild_id: int, key: str, delta: float = 1.0) -> None:
        bucket = self._stats.setdefault(guild_id, {})
        bucket[key] = bucket.get(key, 0.0) + delta

    # ── Event handlers ────────────────────────────────────────────────────────

    async def _on_trade(
        self, guild: discord.Guild, user: discord.Member,
        action: str, symbol: str, amount: float, price: float, total: float, tx_hash: str,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        token = Config.TOKENS.get(symbol, {})
        emoji = token.get("emoji", "●")
        network = token.get("network", "")
        is_buy = action == "BUY"
        footer = f"tx:{tx_hash}"
        if network:
            footer += f"  •  {network}"
        embed = (
            card(f"{'🟢 BUY' if is_buy else '🔴 SELL'}  -  {emoji} {symbol}", color=C_BUY if is_buy else C_SELL)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Amount", f"`{fmt_token(amount, symbol, emoji)}`", True)
            .field("Price",  fmt_usd(price),                          True)
            .field("Total",  f"**{fmt_usd(total)}**",                 True)
            .footer(footer)
            .build()
        )
        self._stat(guild.id, "buy_vol" if is_buy else "sell_vol", total)
        await self._send(guild, "trade_channel", embed=embed, channel=channel)

    async def _on_mm_trade(
        self, guild: discord.Guild, symbol: str, direction: int,
        old_price: float, new_price: float, pct_change: float,
        volume: float, tx_hash: str, bot_name: str, verb: str,
    ) -> None:
        token = Config.TOKENS.get(symbol, {})
        emoji = token.get("emoji", "●")
        embed = (
            card(f"🤖 {bot_name}  -  {verb} {emoji}{symbol}", color=C_BUY if direction > 0 else C_SELL)
            .field("Price", f"{fmt_usd(old_price)} → {fmt_usd(new_price)} ({fmt_pct(pct_change)})", False)
            .field("Volume", f"**{fmt_usd(volume)}**",          True)
            .field("Signal", "📈" if direction > 0 else "📉",  True)
            .build()
        )
        set_tx(embed, guild.id, tx_hash, footer_extra="Market Maker")
        await self._send(guild, "trade_channel", embed=embed)

    async def _on_swap_trade(
        self, guild: discord.Guild, user: discord.Member,
        token_in: str, amount_in: float, token_out: str, amount_out: float,
        pool_id: str, price_impact: float, tx_hash: str,
        gas_fee: float = 0.0, gas_coin: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        def _e(sym: str) -> str:
            return Config.TOKENS.get(sym, {}).get("emoji", "💵") if sym != "USD" else "💵"
        # Compute USD values for PnL display
        sent_usd = recv_usd = 0.0
        try:
            pr_in  = await self.bot.db.get_price(token_in,  guild.id)
            pr_out = await self.bot.db.get_price(token_out, guild.id)
            sent_usd = amount_in  * (float(pr_in["price"])  if pr_in  else (1.0 if token_in  in ("USD","USDC","DSD") else 0.0))
            recv_usd = amount_out * (float(pr_out["price"]) if pr_out else (1.0 if token_out in ("USD","USDC","DSD") else 0.0))
        except Exception:
            pass
        pnl = recv_usd - sent_usd
        _b = (
            card("🔄 SWAP", color=C_INFO)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Sent",     f"`{fmt_token(amount_in, token_in, _e(token_in))}`",   True)
            .field("Received", f"`{fmt_token(amount_out, token_out, _e(token_out))}`", True)
        )
        if sent_usd > 0 or recv_usd > 0:
            sign = "+" if pnl >= 0 else ""
            _b.field("PnL (USD)", f"**{sign}{fmt_usd(pnl)}**", True)
        _b.field("Pool",   f"`{pool_id}`",             True)
        _b.field("Impact", f"{price_impact*100:.3f}%", True)
        if gas_fee > 0 and gas_coin:
            gas_em = Config.TOKENS.get(gas_coin, {}).get("emoji", "●")
            _b.field("Gas Fee", f"`{fmt_gas(gas_fee, gas_coin, gas_em)}`", True)
        embed = _b.build()
        set_tx(embed, guild.id, tx_hash)
        self._stat(guild.id, "swap_count")
        await self._send(guild, "crypto_channel", embed=embed, channel=channel)

    async def _on_oracle_rebalance(
        self, guild: discord.Guild, pool_id: str,
        token_a: str, old_price: float, new_price: float, pct_change: float,
        tx_hash: str = "",
    ) -> None:
        arrow = "📈" if pct_change > 0 else "📉"
        color = C_BUY if pct_change > 0 else C_SELL
        embed = (
            card(f"⚖️ Oracle Rebalance  -  `{pool_id}`", color=color)
            .field("Pool",      f"`{pool_id}`",                               False)
            .field("Old Price", fmt_usd(old_price),                           True)
            .field("New Price", fmt_usd(new_price),                           True)
            .field("Deviation", f"{arrow} {fmt_pct(pct_change)} corrected",  True)
            .build()
        )
        if tx_hash:
            set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "pools_channel", embed=embed)

    async def _on_lp_added(
        self, guild: discord.Guild, user: discord.Member,
        pool_id: str, lp_minted: float, tx_hash: str,
        amount_a: float = 0.0, amount_b: float = 0.0,
        token_a: str = "", token_b: str = "",
        gas_fee: float = 0.0, gas_coin: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        _b = (
            card("🌊 LIQUIDITY ADDED", color=C_BUY)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Pool",      f"`{pool_id}`",                       True)
            .field("LP Minted", f"`{fmt_token(lp_minted, 'LP')}`",  True)
        )
        if token_a and amount_a:
            _b.field(f"Deposited {token_a}", f"`{fmt_token(amount_a, token_a)}`", True)
        if token_b and amount_b:
            _b.field(f"Deposited {token_b}", f"`{fmt_token(amount_b, token_b)}`", True)
        if gas_fee > 0 and gas_coin:
            gas_em = Config.TOKENS.get(gas_coin, {}).get("emoji", "●")
            _b.field("Gas Fee", f"`{fmt_gas(gas_fee, gas_coin, gas_em)}`", True)
        embed = _b.build()
        set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "pools_channel", embed=embed, channel=channel)

    async def _on_lp_removed(
        self, guild: discord.Guild, user: discord.Member,
        pool_id: str, lp_burned: float, tx_hash: str,
        amount_a: float = 0.0, amount_b: float = 0.0,
        token_a: str = "", token_b: str = "",
        gas_fee: float = 0.0, gas_coin: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        _b = (
            card("🌊 LIQUIDITY REMOVED", color=C_WARNING)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Pool",     f"`{pool_id}`",                      True)
            .field("LP Burned", f"`{fmt_token(lp_burned, 'LP')}`", True)
        )
        if token_a and amount_a:
            _b.field(f"Received {token_a}", f"`{fmt_token(amount_a, token_a)}`", True)
        if token_b and amount_b:
            _b.field(f"Received {token_b}", f"`{fmt_token(amount_b, token_b)}`", True)
        if gas_fee > 0 and gas_coin:
            gas_em = Config.TOKENS.get(gas_coin, {}).get("emoji", "●")
            _b.field("Gas Fee", f"`{fmt_gas(gas_fee, gas_coin, gas_em)}`", True)
        embed = _b.build()
        set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "pools_channel", embed=embed, channel=channel)

    async def _on_staked(
        self, guild: discord.Guild, user: discord.Member,
        validator_id: str, symbol: str, amount: float, tx_hash: str,
        gas_fee: float = 0.0, gas_coin: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        cfg = await self.bot.db.get_validator(validator_id, guild.id) or {}
        vname  = cfg.get("name", validator_id)
        vemoji = cfg.get("emoji", "⚡")
        network = cfg.get("network", "")
        amt_str = await _usd_value_str(self.bot, symbol, amount, guild.id)
        _b = (
            card(f"🌐 FARM DEPOSIT  -  {vemoji} {vname}", color=C_BUY)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Amount", amt_str,              True)
            .field("Farm",   f"{vemoji} {vname}", True)
        )
        if network:
            _b.field("Network", network, True)
        reward_rate = cfg.get("reward_rate", 0)
        if reward_rate:
            _b.field("Hourly Rate", f"{reward_rate/24*100:.4f}%", True)
        if gas_fee > 0 and gas_coin:
            gas_em = Config.TOKENS.get(gas_coin, {}).get("emoji", "●")
            _b.field("Gas Fee", f"`{fmt_gas(gas_fee, gas_coin, gas_em)}`", True)
        embed = _b.build()
        set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "staking_channel", embed=embed, channel=channel)

    async def _on_unstaked(
        self, guild: discord.Guild, user: discord.Member,
        validator_id: str, symbol: str, amount: float, tx_hash: str,
        gas_fee: float = 0.0, gas_coin: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        cfg = await self.bot.db.get_validator(validator_id, guild.id) or {}
        vname  = cfg.get("name", validator_id)
        vemoji = cfg.get("emoji", "⚡")
        network = cfg.get("network", "")
        amt_str = await _usd_value_str(self.bot, symbol, amount, guild.id)
        _b = (
            card(f"↩️ FARM WITHDRAWAL  -  {vemoji} {vname}", color=C_WARNING)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Amount", amt_str,              True)
            .field("Farm",   f"{vemoji} {vname}", True)
        )
        if network:
            _b.field("Network", network, True)
        if gas_fee > 0 and gas_coin:
            gas_em = Config.TOKENS.get(gas_coin, {}).get("emoji", "●")
            _b.field("Gas Fee", f"`{fmt_gas(gas_fee, gas_coin, gas_em)}`", True)
        embed = _b.build()
        set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "staking_channel", embed=embed, channel=channel)

    async def _on_slash(
        self, guild: discord.Guild, validator_id: str, victims: list, avg_loss_pct: float,
    ) -> None:
        cfg = await self.bot.db.get_validator(validator_id, guild.id) or {}
        name    = cfg.get("name", validator_id)
        vemoji  = cfg.get("emoji", "⚡")
        network = cfg.get("network", "")
        _b = (
            card(f"⚡ FARM SLASH  -  {vemoji} {name}", color=C_SELL)
            .field("Stakers Slashed", str(len(victims)),        True)
            .field("Avg Loss",        f"{avg_loss_pct*100:.1f}%", True)
        )
        if network:
            _b.field("Network", network, True)
        if victims:
            top = victims[:5]
            lines = []
            for v in top:
                m = guild.get_member(v["user_id"])
                n = m.display_name if m else f"User {v['user_id']}"
                pct = v["loss"] / v["amount"] * 100 if v["amount"] > 0 else 0
                lines.append(f"• {n}: `-{to_human(v['loss']):,.4f}` ({pct:.1f}%)")
            _b.field("Victims", "\n".join(lines), False)
        embed = _b.build()
        ai_flags = await self.bot.db.get_ai_flags(guild.id)
        if ai_flags["events"] and Config.OPENROUTER_API_KEY:
            ai_prompts = await self.bot.db.get_ai_prompts(guild.id)
            events_prompt = (
                ai_prompts.get("events")
                or "You are a concise market reporter. Summarize the event in one factual sentence using the numbers provided. No hype."
            )
            narration = await ai_complete(
                [
                    {"role": "system", "content": events_prompt},
                    {"role": "user", "content": f"Farm '{name}' was slashed. {len(victims)} staker(s) affected. Average stake loss: {avg_loss_pct*100:.1f}%."},
                ],
                max_tokens=50,
            )
            if narration:
                embed.description = f"> {strip_links(narration)}"
        await self._send(guild, "staking_channel", embed=embed)

        # ── DM each affected staker ──────────────────────────────────────────
        for victim in victims:
            prefs = await self.bot.db.get_user_prefs(victim["user_id"], guild.id)
            if not prefs.get("dm_staking", 0):
                continue
            member = guild.get_member(victim["user_id"])
            if not member:
                continue
            _v_loss = to_human(victim["loss"])
            _v_amt  = to_human(victim["amount"])
            pct = _v_loss / _v_amt * 100 if _v_amt > 0 else 0
            dm_embed = (
                card(f"⚡ Stake Slashed  -  {vemoji} {name}", color=C_SELL)
                .field("Farm",      f"{vemoji} {name}",                              True)
                .field("Network",   network or " - ",                                  True)
                .field("Loss",      f"`-{_v_loss:,.6f}` ({pct:.1f}%)",              True)
                .field("Remaining", f"`{_v_amt - _v_loss:,.6f}`",                   True)
                .footer("Use `/notify staking off` to disable these DMs.")
                .build()
            )
            try:
                await member.send(embed=dm_embed)
            except discord.HTTPException:
                pass

    async def _on_pos_validator_slashed(
        self,
        guild: discord.Guild,
        validator_user_id: int,
        network: str,
        slash_result: dict,
        reason: str = "",
        action_type: str = "",
    ) -> None:
        """Feed embed when a PoS validator is automatically slashed for a rejected transaction."""
        _b = (
            card(
                "⚠️ PoS Validator Slashed",
                description=(
                    f"{mention(validator_user_id, guild)} submitted a rejected `{action_type.upper() or 'TX'}` "
                    f"on **{network}** and has been penalized."
                ),
                color=C_SELL,
            )
            .field("Slashed",     f"{to_human(slash_result.get('slashed_amount', 0)):,.4f}", True)
            .field("New Stake",   f"{to_human(slash_result.get('new_stake', 0)):,.4f}",      True)
            .field("Slash Count", f"{slash_result.get('slash_count', 0)}/{MAX_SLASH_COUNT}",       True)
        )
        if slash_result.get("deactivated"):
            _b.field("⛔ Status", f"Validator **deactivated** after {MAX_SLASH_COUNT} slashes. Delegators refunded.", False)
        if reason:
            _b.field("Reason", reason[:200], False)
        embed = _b.build()
        # Try validators channel first, fall back to staking channel
        ch = await self._get_channel(guild, "validators_channel") or await self._get_channel(guild, "staking_channel")
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.HTTPException:
                pass

        # ── DM validator owner ───────────────────────────────────────────────
        owner = guild.get_member(validator_user_id)
        if owner:
            prefs = await self.bot.db.get_user_prefs(validator_user_id, guild.id)
            if prefs.get("dm_validator", 0):
                deactivated = slash_result.get("deactivated", False)
                dm_embed = (
                    card("⚠️ Your PoS Validator Was Slashed", color=C_SELL)
                    .field("Network",    network,                                                   True)
                    .field("Action",     action_type.upper() or "TX",                              True)
                    .field("Slashed",    f"{to_human(slash_result.get('slashed_amount', 0)):,.6f}",           True)
                    .field("Remaining",  f"{to_human(slash_result.get('new_stake', 0)):,.6f}",                True)
                    .field("Slash Count",f"{slash_result.get('slash_count', 0)}/{MAX_SLASH_COUNT}",                True)
                )
                if deactivated:
                    dm_embed.field("⛔ Status", "Your validator was **deactivated**. Delegators have been refunded.", False)
                if reason:
                    dm_embed.field("Reason", reason[:200], False)
                dm_embed.footer("Use `/notify validator off` to disable these DMs.")
                try:
                    await owner.send(embed=dm_embed.build())
                except discord.HTTPException:
                    pass

        # ── DM affected delegators ───────────────────────────────────────────
        if slash_result.get("delegator_losses"):
            for dl in slash_result["delegator_losses"]:
                d_member = guild.get_member(dl["user_id"])
                if not d_member:
                    continue
                prefs = await self.bot.db.get_user_prefs(dl["user_id"], guild.id)
                if not prefs.get("dm_validator", 0):
                    continue
                d_embed = (
                    card("⚠️ Delegated Stake Slashed", color=C_SELL)
                    .field("Validator", mention(validator_user_id, guild), True)
                    .field("Network",   network,                           True)
                    .field("Loss",      f"{to_human(dl['loss']):,.6f}",                             True)
                    .footer("Use `/notify validator off` to disable these DMs.")
                    .build()
                )
                try:
                    await d_member.send(embed=d_embed)
                except discord.HTTPException:
                    pass

    async def _on_validator_reward(
        self, guild: discord.Guild, validator_id: str, symbol: str,
        staker_count: int, total_rewarded: float, tx_hash: str = "",
        event_tag: str | None = None,
        heat: float | None = None,
        **_extra_kwargs,
    ) -> None:
        # ``heat`` is accepted (and currently unused in this embed) so the
        # bus publisher in cogs/stake.py can evolve the payload without
        # crashing every live listener. Same reason for **_extra_kwargs:
        # any future field added to the publish call gets absorbed here
        # instead of raising "got an unexpected keyword argument" into
        # the RedisBus log.
        cfg = await self.bot.db.get_validator(validator_id, guild.id) or {}
        name    = cfg.get("name", validator_id)
        vemoji  = cfg.get("emoji", "✅")
        network = cfg.get("network", "")
        token   = Config.TOKENS.get(symbol, {})
        semoji  = token.get("emoji", "●")
        footer = f"tx:{tx_hash}" if tx_hash else "Hourly farm yield"
        # HOT / COLD are validator-wide tick events; badge the title so the
        # staking channel shows every staker why this payout felt different.
        if event_tag == "HOT":
            title_prefix = "🔥 HOT TICK  -  FARM REWARD"
            color = C_GOLD
        elif event_tag == "COLD":
            title_prefix = "🧊 COLD TICK  -  FARM REWARD"
            color = C_SELL
        else:
            title_prefix = "✅ FARM REWARD"
            color = C_BUY
        _b = (
            card(f"{title_prefix}  -  {vemoji} {name}", color=color)
            .field("Stakers Rewarded", str(staker_count),                            True)
            .field("Total Distributed", fmt_token(to_human(int(total_rewarded)), symbol, semoji), True)
        )
        if event_tag == "HOT":
            _b.field("MEV Bonus", "**2x** base yield this tick", True)
        elif event_tag == "COLD":
            _b.field("Missed Attestation", "**0.4x** base yield this tick", True)
        if network:
            _b.field("Network", network, True)
        _b.footer(footer)
        embed = _b.build()
        await self._send(guild, "staking_channel", embed=embed)

    async def _on_gamble(
        self, guild: discord.Guild, user: discord.Member,
        game: str, token: str, bet: float,
        delta: float, won: bool, tx_hash: str,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        if token == "USD":
            change_str = f"**{'+'if won else '-'}{fmt_usd(abs(delta))}**"
            bet_str = fmt_usd(bet)
        else:
            change_str = f"**{'+'if won else '-'}{fmt_token(abs(delta), token)}**"
            bet_str = fmt_token(bet, token)
        try:
            avatar_url = user.display_avatar.url
        except Exception:
            avatar_url = None
        embed = (
            card(f"{'🟢' if won else '🔴'} {game.upper()}  -  {'WON' if won else 'LOST'}", color=C_BUY if won else C_SELL)
            .author(user.display_name, icon_url=avatar_url)
            .field("Wager",   bet_str,    True)
            .field("Outcome", change_str, True)
            .build()
        )
        set_tx(embed, guild.id, tx_hash)
        self._stat(guild.id, "gamble_wins" if won else "gamble_losses")
        self._stat(guild.id, "gamble_vol", bet)
        await self._send(guild, "gambling_channel", embed=embed, channel=channel)

    async def _on_drop(
        self, guild: discord.Guild, user: discord.Member, amount: float,
        tx_hash: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        embed = (
            card("💰 DROP CLAIMED", color=C_GOLD)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Winner", user.mention,             True)
            .field("Amount", f"**{fmt_usd(amount)}**", True)
            .build()
        )
        if tx_hash:
            set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "drops_channel", embed=embed, channel=channel)

    async def _on_block_bundled(
        self, guild: discord.Guild, block_num: int, block_hash: str, tx_count: int,
        network: str = "", **kwargs,
    ) -> None:
        self._stat(guild.id, "blocks_bundled")
        # SUN blocks are already announced by the mining system (_on_block / _on_pool_block)
        if not network or network == "sun":
            return

        # PoS chain block confirmed  -  post to the validators/staking/trade channel
        settings = await self.bot.db.get_guild_settings(guild.id)
        if not (settings.get("validators_channel") or settings.get("staking_channel") or settings.get("trade_channel")):
            return

        # Fetch tx breakdown: user txns vs oracle rebalances
        txns_all = await self.bot.db.get_chain_block_txns(guild.id, block_num, limit=200, network=network)
        user_txns = [t for t in txns_all if t.get("tx_type") not in ("ARB", "ORACLE_REBALANCE")]
        oracle_count = len(txns_all) - len(user_txns)

        net_label = network.upper()
        net_full  = {"ARC": "Arcadia Network", "DSC": "Discoin Network", "SUN": "Sun Network", "MTA": "Moneta Chain"}.get(net_label, net_label)

        _b = (
            card(f"✅ Block #{block_num:,} Confirmed  [{net_label}]", color=C_PURPLE)
            .field("🔗 Network",   net_full,                   True)
            .field("📦 User Txns", str(len(user_txns)),        True)
        )
        if oracle_count:
            _b.field("⚖ Oracle Rebalances", str(oracle_count), True)
        _b.field("🔑 Hash", f"`{block_hash[:20]}…`",     False)
        _b.field("⚡ Consensus", "PoS Validators",        True)
        _b.footer(f"Use .block {network} for full details")
        embed = _b.build()

        import datetime
        embed.timestamp = datetime.datetime.utcnow()
        # Use the validators/staking/trade channel key name that _send expects
        ch_key = (
            "validators_channel" if settings.get("validators_channel")
            else "staking_channel" if settings.get("staking_channel")
            else "trade_channel"
        )
        await self._send(guild, ch_key, embed=embed)

    async def _on_mining_tick_complete(self, guild: discord.Guild, summary: dict) -> None:
        """Single embed per mining tick covering all solo/pool/group payouts."""
        sun_row = await self.bot.db.get_price("SUN", guild.id)
        sun_price = float(sun_row["price"]) if sun_row else 0.0
        counts = await self.bot.db.get_mining_mode_counts(guild.id)

        bh     = summary["block_height"]
        reward = summary["block_reward"]
        thr    = summary["total_hashrate"]

        _b = (
            card(f"⛏ Block #{bh:,}  -  ☀ SUN", color=C_AMBER)
            .field("Block Reward",  fmt_token(reward, "SUN", "☀"),   True)
            .field("Net Hashrate",  f"{thr:,.0f} MH/s",              True)
            .field(
                "Miners",
                f"Solo: {counts['solo']}  |  Pool: {counts['pool']}  |  Group: {counts['group']}",
                True,
            )
        )

        # Solo payouts
        if summary["solo_payouts"]:
            lines = []
            for uid, blks, earned, uhr, _ in summary["solo_payouts"]:
                m = guild.get_member(uid)
                name = m.display_name if m else f"User {uid}"
                share = uhr / thr * 100 if thr > 0 else 0
                usd_str = f" ≈ {fmt_usd(earned * sun_price)}" if sun_price > 0 else ""
                lines.append(
                    f"• {name}  -  {blks} block(s) → {fmt_token(earned, 'SUN', '☀')}"
                    f"{usd_str} ({share:.1f}%)"
                )
            _b.field("⚡ Solo", "\n".join(lines), False)

        # Pool payouts
        if summary["pool_total"] > 0:
            pool_usd = summary["pool_total"] * sun_price
            pool_usd_str = f" ≈ {fmt_usd(pool_usd)}" if sun_price > 0 else ""
            lines = [f"Total: {fmt_token(summary['pool_total'], 'SUN', '☀')}{pool_usd_str}"]
            for entry in summary["pool_payouts"]:
                uid, share_pct, earned = entry[0], entry[1], entry[2]
                m = guild.get_member(uid)
                name = m.display_name if m else f"Miner {uid}"
                lines.append(f"• **{name}**  -  {share_pct:.1f}% → {fmt_token(earned, 'SUN', '☀')}")
            extra = counts.get("pool", 0) - len(summary["pool_payouts"])
            if extra > 0:
                lines.append(f"  _(+{extra} more)_")
            _b.field(
                f"🏊 Pool (+{summary['pool_blocks']} block(s))",
                "\n".join(lines),
                False,
            )

        # Group payouts
        for grp in summary["groups"]:
            blk_count = grp.get("blocks", 0)
            total_usd_str = f" ≈ {fmt_usd(grp['total_reward'] * sun_price)}" if sun_price > 0 else ""
            reserve_usd_str = f" ≈ {fmt_usd(grp['reserve_cut'] * sun_price)}" if sun_price > 0 else ""
            lines = [
                f"Total: {fmt_token(grp['total_reward'], 'SUN', '☀')}{total_usd_str} | "
                f"Reserve: {fmt_token(grp['reserve_cut'], 'SUN', '☀')}{reserve_usd_str}"
            ]
            # Show vault token minted (if any)
            _vault_minted = grp.get("vault_tokens_minted", 0)
            _vault_sym = grp.get("vault_token_sym", "")
            if _vault_minted and _vault_sym:
                lines.append(f"Vault: +{_vault_minted:,} {_vault_sym} minted")
            # Show active group upgrade bonuses
            _grp_upgrades = grp.get("upgrades", [])
            if _grp_upgrades:
                lines.append(f"Upgrades: {' · '.join(_grp_upgrades)}")
            for entry in grp["members"]:
                uid, earned, wpct = entry[0], entry[1], entry[2]
                m = guild.get_member(uid)
                name = m.display_name if m else f"User {uid}"
                member_usd_str = f" ≈ {fmt_usd(earned * sun_price)}" if sun_price > 0 else ""
                lines.append(f"• {name} -> {fmt_token(earned, 'SUN', '☀')}{member_usd_str} ({wpct:.0f}%)")
            _b.field(
                f"👥 {grp['name']} (+{blk_count} block(s))",
                "\n".join(lines),
                False,
            )

        _b.footer("Pool rewards proportional to hashrate · difficulty retargets every 2,016 blocks")
        embed = _b.build()
        n_blocks = len(summary["solo_payouts"]) + summary["pool_blocks"] + len(summary["groups"])
        self._stat(guild.id, "blocks_found", n_blocks)
        await self._send(guild, "mine_channel", embed=embed)

        # Record block in history  -  use top solo earner or None for pool/group blocks
        top_miner: int | None = None
        if summary["solo_payouts"]:
            top_miner = max(summary["solo_payouts"], key=lambda x: x[2])[0]  # (uid, blks, earned, ...)
        await self.bot.db.log_block(guild.id, bh, top_miner, reward, thr, "SUN")

        # DM each group member individually with their personal payout
        for grp in summary["groups"]:
            blk_count = grp.get("blocks", 0)
            _grp_upgrades = grp.get("upgrades", [])
            _upgrade_line = f"\nUpgrades: {' · '.join(_grp_upgrades)}" if _grp_upgrades else ""
            _vault_minted = grp.get("vault_tokens_minted", 0)
            _vault_sym = grp.get("vault_token_sym", "")
            _vault_line = f"\nVault: +{_vault_minted:,} {_vault_sym} minted" if _vault_minted and _vault_sym else ""
            for entry in grp["members"]:
                uid, earned, wpct = entry[0], entry[1], entry[2]
                per_member_reserve = entry[3] if len(entry) > 3 else 0.0
                usd_str = f" ≈ {fmt_usd(earned * sun_price)}" if sun_price > 0 else ""
                dm_b = card(
                    "⛏ Group Mining Payout",
                    description=f"Your group **{grp['name']}** mined **+{blk_count} block(s)**.{_upgrade_line}{_vault_line}",
                    color=C_AMBER,
                )
                dm_b.field("Your Share", f"{fmt_token(earned, 'SUN', '☀')}{usd_str} ({wpct:.0f}%)", True)
                if per_member_reserve > 0:
                    res_usd_str = f" ≈ {fmt_usd(per_member_reserve * sun_price)}" if sun_price > 0 else ""
                    dm_b.field("💾 Reserve", f"{fmt_token(per_member_reserve, 'SUN', '☀')}{res_usd_str}", True)
                dm_embed = dm_b.build()
                await self._dm(uid, guild, dm_embed, category="mining", network="sun")

    async def _on_pow_mining_tick(
        self,
        guild: discord.Guild,
        symbol: str,
        emoji: str,
        chain_name: str,
        block_height: int,
        block_reward: float,
        blocks_mined: int,
        total_hashrate: float,
        payouts: list[tuple],
        group_info: list[dict] | None = None,
        group_member_reserve: dict[int, float] | None = None,
        group_member_vault_info: dict | None = None,
    ) -> None:
        """Feed embed + DMs for non-SUN PoW mining (MTA, etc.)."""
        price_row = await self.bot.db.get_price(symbol, guild.id)
        price = float(price_row["price"]) if price_row else 0.0

        # ── Feed embed ───────────────────────────────────────────────────────
        _warmup_blocks = Config.POW_NETWORKS.get(symbol, {}).get("warmup_blocks", 0)
        _in_warmup = _warmup_blocks > 0 and block_height < _warmup_blocks
        _warmup_note = (
            f"\n_Warmup phase: rewards scale to full over first {_warmup_blocks:,} blocks._"
            if _in_warmup else ""
        )
        reward_usd_str = f" ≈ {fmt_usd(block_reward * price)}" if price > 0 else ""
        _b = (
            card(f"⛏ Block #{block_height:,}  -  {emoji} {symbol}", color=C_AMBER)
            .field("Block Reward", f"{fmt_token(block_reward, symbol, emoji)}{reward_usd_str}", True)
            .field("Net Hashrate", f"{total_hashrate:,.0f} MH/s",                               True)
            .field("Miners",       str(len(payouts)),                                            True)
        )

        # Top miners (solo + pool; group members shown in their own section below)
        _grp_uids = {uid for grp in (group_info or []) for uid, _, _ in grp.get("members", [])}
        non_grp_payouts = [e for e in payouts if e[0] not in _grp_uids]
        sorted_payouts = sorted(non_grp_payouts, key=lambda x: x[1], reverse=True)
        if sorted_payouts:
            lines = []
            for entry in sorted_payouts[:5]:
                uid, earned, share_pct = entry[0], entry[1], entry[2]
                m = guild.get_member(uid)
                name = m.display_name if m else f"User {uid}"
                usd_str = f" ≈ {fmt_usd(earned * price)}" if price > 0 else ""
                lines.append(
                    f"• {name}  -  {fmt_token(earned, symbol, emoji)}{usd_str} ({share_pct:.1f}%)"
                )
            extra = len(non_grp_payouts) - 5
            if extra > 0:
                lines.append(f"  _(+{extra} more)_")
            _b.field(f"⛏ Payouts (+{blocks_mined} block{'s' if blocks_mined > 1 else ''})", "\n".join(lines) + _warmup_note, False)

        # Group sections: total, reserve (with USD), members, vault token
        for grp in (group_info or []):
            blk_count = grp.get("blocks", 0)
            total_usd_str = f" ≈ {fmt_usd(grp['total_reward'] * price)}" if price > 0 else ""
            reserve_usd_str = f" ≈ {fmt_usd(grp['reserve_cut'] * price)}" if price > 0 else ""
            grp_lines = [
                f"Total: {fmt_token(grp['total_reward'], symbol, emoji)}{total_usd_str} | "
                f"Reserve: {fmt_token(grp['reserve_cut'], symbol, emoji)}{reserve_usd_str}"
            ]
            _vault_minted = grp.get("vault_tokens_minted", 0)
            _vault_sym = grp.get("vault_token_sym", "")
            if _vault_minted and _vault_sym:
                grp_lines.append(f"Vault: +{_vault_minted:,} {_vault_sym} minted")
            for uid, earned, wpct in grp.get("members", []):
                m = guild.get_member(uid)
                name = m.display_name if m else f"User {uid}"
                usd_str = f" ≈ {fmt_usd(earned * price)}" if price > 0 else ""
                grp_lines.append(f"• {name} -> {fmt_token(earned, symbol, emoji)}{usd_str} ({wpct:.0f}%)")
            _b.field(f"👥 {grp['name']} (+{blk_count} block{'s' if blk_count != 1 else ''})", "\n".join(grp_lines), False)

        _b.footer(f"All miners share {emoji} {symbol} proportionally by hashrate")
        embed = _b.build()
        self._stat(guild.id, "blocks_found", blocks_mined)
        await self._send(guild, "mine_channel", embed=embed)

        # ── Record block in history ───────────────────────────────────────────
        all_sorted = sorted(payouts, key=lambda x: x[1], reverse=True)
        top_miner_id = all_sorted[0][0] if all_sorted else None
        await self.bot.db.log_block(
            guild.id, block_height, top_miner_id, block_reward, total_hashrate, symbol
        )

        # ── DM each miner ────────────────────────────────────────────────────
        _grp_vault_map = group_member_vault_info or {}
        _grp_reserve_map = group_member_reserve or {}
        for entry in payouts:
            uid, earned, share_pct = entry[0], entry[1], entry[2]
            boosted_hr = entry[3] if len(entry) > 3 else 0
            hs_bonus = entry[4] if len(entry) > 4 else 0
            usd_str = f" ≈ {fmt_usd(earned * price)}" if price > 0 else ""
            bonus_line = f"\n💎 Hashstone: **+{int(hs_bonus * 100)}%** hashrate" if hs_bonus > 0 else ""
            _vault_info = _grp_vault_map.get(uid)
            vault_line = (
                f"\nVault: +{_vault_info[2]:,} {_vault_info[1]} minted to **{_vault_info[0]}**"
                if _vault_info and _vault_info[1] else ""
            )
            dm_b = card(
                f"⛏ {chain_name} Mining Payout",
                description=f"**+{blocks_mined} block{'s' if blocks_mined > 1 else ''}** mined on the {chain_name} network.{bonus_line}{vault_line}",
                color=C_AMBER,
            )
            dm_b.field(
                f"{emoji} Your Cut",
                f"**{fmt_token(earned, symbol, emoji)}**{usd_str}",
                True,
            )
            reserve_cut = _grp_reserve_map.get(uid, 0.0)
            if reserve_cut > 0:
                reserve_usd_str = f" ≈ {fmt_usd(reserve_cut * price)}" if price > 0 else ""
                dm_b.field("💾 Reserve", f"{fmt_token(reserve_cut, symbol, emoji)}{reserve_usd_str}", True)
            if boosted_hr > 0:
                dm_b.field("⛏ Hashrate", f"**{boosted_hr:,.0f} MH/s**", True)
            dm_b.field("📊 Pool Share", f"**{share_pct:.1f}%**", True)
            dm_b.field("🌐 Server", guild.name, True)
            dm_embed = dm_b.build()
            await self._dm(uid, guild, dm_embed, category="mining", network=chain_name)
            await asyncio.sleep(0.5)

    async def _on_mine_buy(
        self, guild: discord.Guild, user: discord.Member,
        rig_id: str, qty: int, total_cost: float, tx_hash: str,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        rig = Config.MINING_RIGS.get(rig_id, {})
        hr_added = rig.get("hashrate", 0) * qty
        embed = (
            card("🖥️ RIG PURCHASED", color=C_BUY)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Rig",            f"**{qty}×** {rig.get('name', rig_id)} {rig.get('emoji','')}", True)
            .field("Hashrate Added", f"+{hr_added:,} MH/s",             True)
            .field("Cost",           f"**{fmt_usd(to_human(int(total_cost)))}**",       True)
            .build()
        )
        set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "mine_channel", embed=embed, channel=channel)

    async def _on_liquidation(
        self, guild: discord.Guild, user_id: int, collateral: float, outstanding: float,
        tx_hash: str = "",
    ) -> None:
        member  = guild.get_member(user_id)
        user_str = mention(user_id, guild)
        display = member.display_name if member else f"User {user_id}"
        _b = card("💀 LOAN LIQUIDATED", color=C_SELL)
        if member:
            _b.author(display, icon_url=member.display_avatar.url)
        _b.field("Borrower",          user_str,                      True)
        _b.field("Outstanding",       f"**{fmt_usd(outstanding)}**", True)
        _b.field("Collateral Seized", fmt_usd(collateral),           True)
        _b.field("Trigger",           "LTV ≥ 90%",                  True)
        embed = _b.build()
        ai_flags = await self.bot.db.get_ai_flags(guild.id)
        if ai_flags["events"] and Config.OPENROUTER_API_KEY:
            ai_prompts = await self.bot.db.get_ai_prompts(guild.id)
            events_prompt = (
                ai_prompts.get("events")
                or "You are a concise market reporter. Summarize the event in one factual sentence using the numbers provided. No hype."
            )
            narration = await ai_complete(
                [
                    {"role": "system", "content": events_prompt},
                    {"role": "user", "content": f"{display} was liquidated. Loan outstanding: ${outstanding:,.2f}. Collateral seized: ${collateral:,.2f}. LTV exceeded 90%."},
                ],
                max_tokens=50,
            )
            if narration:
                embed.description = f"> {strip_links(narration)}"
        if tx_hash:
            set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "trade_channel", embed=embed)

    async def _on_promoted(
        self, guild: discord.Guild, user: discord.Member, old_job: str, new_job: str,
    ) -> None:
        old_cfg = Config.JOBS.get(old_job, {})
        new_cfg = Config.JOBS.get(new_job, {})
        earn = new_cfg.get("earn", (0, 0))
        _b = (
            card("🎉 PROMOTION!", color=C_AMBER)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("From",          old_cfg.get("title", old_job),           True)
            .field("→ To",          f"**{new_cfg.get('title', new_job)}**", True)
            .field("New Pay Range", f"${earn[0]:,} - ${earn[1]:,} / session", True)
        )
        if new_cfg.get("description"):
            _b.field("Role", new_cfg["description"], False)
        perks = new_cfg.get("perks", {})
        if perks:
            perk_lines = []
            if perks.get("daily_bonus"):
                perk_lines.append(f"Daily bonus: +{perks['daily_bonus']*100:.0f}%")
            if perks.get("stake_bonus"):
                perk_lines.append(f"Staking bonus: +{perks['stake_bonus']*100:.0f}%")
            if perks.get("swap_fee") is not None:
                perk_lines.append(f"Swap fee rebate: {perks['swap_fee']*100:.1f}%")
            if perk_lines:
                _b.field("New Perks", "\n".join(perk_lines), False)
        embed = _b.build()
        await self._send(guild, "job_channel", embed=embed)

    async def _on_daily(
        self, guild: discord.Guild, user: discord.Member, amount: float, streak: int,
        tx_hash: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        streak_icon = "🔥" if streak >= 7 else "✨" if streak >= 3 else "📆"
        embed = (
            card("📅 DAILY REWARD", color=C_GOLD)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Amount", f"**+{fmt_usd(amount)}**",                                    True)
            .field("Streak", f"{streak_icon} {streak} day{'s' if streak != 1 else ''}",  True)
            .build()
        )
        if tx_hash:
            set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "job_channel", embed=embed, channel=channel)

    async def _on_work(
        self, guild: discord.Guild, user: discord.Member, amount: float, job_title: str,
        tx_hash: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        embed = (
            card("💼 WORK SESSION", color=C_BUY)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Earned", f"**+{fmt_usd(amount)}**", True)
            .field("Job",    job_title,                  True)
            .build()
        )
        if tx_hash:
            set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "job_channel", embed=embed, channel=channel)

    async def _dm(
        self,
        user_id: int,
        guild: discord.Guild,
        embed: discord.Embed,
        category: str = "",
        network: str = "",
    ) -> None:
        """Send a DM to a user. Silently fails if DMs are closed or user opted out.
        category: 'mining' | 'transfer' | 'validator' | 'staking'  -  checked against user_prefs.
        network: optional network short name for per-network mute checks.
        """
        try:
            if category:
                col = {"mining": "dm_mining", "transfer": "dm_transfer",
                       "validator": "dm_validator", "staking": "dm_staking"}.get(category)
                if col:
                    prefs = await self.bot.db.get_user_prefs(user_id, guild.id)
                    if not prefs.get(col, 1):
                        return
                # Per-network mute check
                if network and category in ("mining", "staking", "validator"):
                    muted = await self.bot.db.get_muted_networks(user_id, guild.id, category)
                    if network.strip().lower() in muted:
                        return
            member = guild.get_member(user_id) or self.bot.get_user(user_id)
            if member is None:
                try:
                    member = await self.bot.fetch_user(user_id)
                except Exception:
                    return
            if member and not member.bot:
                from core.framework.links import sanitize_embed
                sanitize_embed(embed)
                await member.send(embed=embed)
        except Exception:
            pass

    async def _on_transfer(
        self, guild: discord.Guild, sender: discord.Member, recipient: discord.Member,
        amount: float, tx_hash: str,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        embed = (
            card("💸 TRANSFER", color=C_INFO)
            .author(sender.display_name, icon_url=sender.display_avatar.url)
            .field("From",   sender.mention,           True)
            .field("To",     recipient.mention,        True)
            .field("Amount", f"**{fmt_usd(amount)}**", False)
            .build()
        )
        set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "trade_channel", embed=embed, channel=channel)

        # DM recipient
        dm_embed = (
            card(
                "💸 You Received a Transfer",
                description=f"**{sender.display_name}** sent you **{fmt_usd(amount)}** in **{guild.name}**.",
                color=C_BUY,
            )
            .build()
        )
        set_tx(dm_embed, guild.id, tx_hash)
        await self._dm(recipient.id, guild, dm_embed, category="transfer")

    async def _on_token_send(
        self, guild: discord.Guild, sender: discord.Member,
        to_address: str, symbol: str, amount: float, tx_hash: str,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        emoji = Config.TOKENS.get(symbol, {}).get("emoji", "●")
        short_addr = to_address[:8] + "…" + to_address[-6:] if len(to_address) > 16 else to_address
        embed = (
            card("📤 TOKEN SEND", color=C_INFO)
            .author(sender.display_name, icon_url=sender.display_avatar.url)
            .field("From",   sender.mention,                             True)
            .field("To",     f"`{short_addr}`",                          True)
            .field("Amount", f"**{fmt_token(amount, symbol, emoji)}**",  False)
            .build()
        )
        set_tx(embed, guild.id, tx_hash, "Full: {to_address}")
        await self._send(guild, "trade_channel", embed=embed, channel=channel)

        # DM recipient  -  look up the wallet address owner
        try:
            addr_row = await self.bot.db.get_wallet_address(to_address)
            if addr_row:
                fmt = fmt_usd(amount) if symbol == "USD" else fmt_token(amount, symbol, emoji)
                dm_embed = (
                    card(
                        "📬 You Received Tokens",
                        description=f"**{sender.display_name}** sent you **{fmt}** in **{guild.name}**.",
                        color=C_BUY,
                    )
                    .field("To Address", f"`{to_address}`", False)
                    .build()
                )
                set_tx(dm_embed, guild.id, tx_hash)
                await self._dm(addr_row["user_id"], guild, dm_embed, category="transfer")
        except Exception:
            pass

    async def _on_loan_opened(
        self, guild: discord.Guild, user: discord.Member,
        amount: float, collateral: float, tx_hash: str,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        daily_interest = amount * Config.LENDING["DAILY_RATE"]
        embed = (
            card("🏦 LOAN OPENED", color=C_INFO)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Borrowed",       f"**{fmt_usd(amount)}**",        True)
            .field("Collateral",     fmt_usd(collateral),              True)
            .field("LTV",            f"{amount/collateral*100:.1f}%", True)
            .field(
                "Daily Interest",
                f"{fmt_usd(daily_interest)} ({Config.LENDING['DAILY_RATE']*100:.0f}%/day)",
                True,
            )
            .build()
        )
        set_tx(embed, guild.id, tx_hash)
        await self._send(guild, "trade_channel", embed=embed, channel=channel)

    async def _on_loan_repaid(
        self, guild: discord.Guild, user: discord.Member,
        amount_paid: float, remaining: float,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        fully = remaining <= 0.001
        embed = (
            card("✅ LOAN FULLY REPAID" if fully else "💳 PARTIAL REPAYMENT", color=C_BUY)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Paid",      f"**{fmt_usd(amount_paid)}**",                   True)
            .field("Remaining", "Cleared 🎉" if fully else fmt_usd(remaining),  True)
            .build()
        )
        await self._send(guild, "trade_channel", embed=embed, channel=channel)

    async def _on_wallet_created(
        self, guild: discord.Guild, user: discord.Member,
        network: str = "", address: str = "", label: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        _b = (
            card("🔐 WALLET CREATED", color=C_PURPLE)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Network", network or " - ",                       True)
            .field("Address", f"`{address}`" if address else " - ",   False)
        )
        if label:
            _b.field("Label", label, True)
        embed = _b.build()
        await self._send(guild, "wallet_channel", embed=embed, channel=channel)

    async def _on_crypto_withdraw(
        self, guild: discord.Guild, user: discord.Member,
        symbol: str = "", amount: float = 0.0,
        network: str = "", platform_fee: float = 0.0, gas_fee: float = 0.0,
        gas_coin: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        """CeFi → DeFi withdrawal."""
        _b = (
            card("📤 CRYPTO WITHDRAW  (CeFi → DeFi)", color=C_WARNING)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Token",   f"**{symbol}**",      True)
            .field("Amount",  f"**{amount:,.6f}**", True)
            .field("Network", network or " - ",        True)
        )
        if platform_fee > 0:
            _b.field("Platform Fee", f"**${platform_fee:,.2f}**", True)
        if gas_fee > 0 and gas_coin:
            _b.field("Gas Fee", f"**{gas_fee:,.6f} {gas_coin}**", True)
        embed = _b.build()
        await self._send(guild, "wallet_channel", embed=embed, channel=channel)

    async def _on_crypto_deposit(
        self, guild: discord.Guild, user: discord.Member,
        symbol: str = "", amount: float = 0.0,
        network: str = "", gas_fee: float = 0.0, gas_coin: str = "",
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        """DeFi → CeFi deposit."""
        _b = (
            card("📥 CRYPTO DEPOSIT  (DeFi → CeFi)", color=C_SUCCESS)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Token",   f"**{symbol}**",      True)
            .field("Amount",  f"**{amount:,.6f}**", True)
            .field("Network", network or " - ",        True)
        )
        if gas_fee > 0 and gas_coin:
            _b.field("Gas Fee", f"**{gas_fee:,.6f} {gas_coin}**", True)
        embed = _b.build()
        await self._send(guild, "wallet_channel", embed=embed, channel=channel)

    async def _on_deposit(
        self, guild: discord.Guild, user: discord.Member, amount: float,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        embed = (
            card("🏦 DEPOSIT", color=C_NEUTRAL)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Amount",    fmt_usd(amount),  True)
            .field("Direction", "Wallet → Bank", True)
            .build()
        )
        await self._send(guild, "trade_channel", embed=embed, channel=channel)

    async def _on_withdraw(
        self, guild: discord.Guild, user: discord.Member, amount: float,
        channel: discord.abc.MessageableChannel | None = None,
    ) -> None:
        embed = (
            card("💵 WITHDRAWAL", color=C_NEUTRAL)
            .author(user.display_name, icon_url=user.display_avatar.url)
            .field("Amount",    fmt_usd(amount),  True)
            .field("Direction", "Bank → Wallet", True)
            .build()
        )
        await self._send(guild, "trade_channel", embed=embed, channel=channel)

    # ── Hourly summary ────────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def hourly_summary(self) -> None:
        for guild in self.bot.guilds:
            stats = self._stats.pop(guild.id, {})
            if not any(stats.values()):
                continue
            ch = await self._get_channel(guild, "trade_channel")
            if not ch:
                continue
            settings = await self.bot.db.get_guild_settings(guild.id)
            color = settings.get("embed_color") or C_INFO
            now = fmt_ts(datetime.datetime.utcnow(), "%Y-%m-%d %H:00 UTC")
            buy_vol       = stats.get("buy_vol", 0.0)
            sell_vol      = stats.get("sell_vol", 0.0)
            swap_count    = int(stats.get("swap_count", 0))
            gamble_wins   = int(stats.get("gamble_wins", 0))
            gamble_losses = int(stats.get("gamble_losses", 0))
            gamble_vol    = stats.get("gamble_vol", 0.0)
            blocks        = int(stats.get("blocks_found", 0))

            _b = card(f"📊 Hourly Summary  -  {now}", color=color)
            if buy_vol or sell_vol:
                _b.field(
                    "💹 Crypto Trades",
                    f"🟢 Buy: **{fmt_usd(buy_vol)}**\n🔴 Sell: **{fmt_usd(sell_vol)}**",
                    True,
                )
            if swap_count:
                _b.field("🔄 Swaps", f"**{swap_count}** swaps", True)
            if gamble_wins or gamble_losses:
                total_g = gamble_wins + gamble_losses
                wr = gamble_wins / total_g * 100 if total_g else 0
                _b.field(
                    "🎲 Gambling",
                    f"{total_g} games  •  {wr:.0f}% win rate\nVol: **{fmt_usd(gamble_vol)}**",
                    True,
                )
            if blocks:
                _b.field("⛏️ Mining", f"**{blocks}** block(s)", True)
            embed = _b.build()

            try:
                from core.framework.links import sanitize_embed
                sanitize_embed(embed)
                await ch.send(embed=embed)
            except Exception:
                pass
        pulse("hourly_summary")

    @hourly_summary.before_loop
    async def before_hourly(self) -> None:
        await self.bot.wait_until_ready()

    # ── Validator block feed ────────────────────────────────────────────────────

    async def _on_validator_block(self, **kwargs) -> None:
        """Post a validator block confirmation embed to the staking channel."""
        guild = kwargs['guild']
        network = kwargs['network']
        validator = kwargs['validator']
        block_id = kwargs['block_id']
        results = kwargs['results']
        total_gas = kwargs['total_gas']
        gas_coin = kwargs['gas_coin']
        validator_reward = kwargs['validator_reward']
        treasury_cut = kwargs['treasury_cut']
        is_valid = kwargs.get('is_valid', True)
        """Post a validator block confirmation embed to the staking channel."""
        settings = await self.bot.db.get_guild_settings(guild.id)
        channel_id = (
            settings.get("validators_channel")
            or settings.get("staking_channel")
            or settings.get("trade_channel")
        )
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        confirmed = [r for r in results if r["success"]]
        rejected  = [r for r in results if not r["success"]]

        _gc = Config.TOKENS.get(gas_coin, {})
        _ge = _gc.get("emoji", "●")
        _b = (
            card(
                f"{'✅' if is_valid else '❌'} Validator Block #{block_id} {'Confirmed' if is_valid else 'Rejected'}",
                color=C_PURPLE if is_valid else C_ERROR,
            )
            .field("🔗 Network",    network,                                                                True)
            .field("👤 Validator",  mention(validator['user_id'], guild) if validator else "Admin",        True)
            .field("📦 Actions",    f"{len(confirmed)} confirmed / {len(rejected)} rejected",              True)
            .field("⛽ Gas Collected", fmt_gas(total_gas, gas_coin, _ge),           True)
            .field("🏆 Validator Cut", fmt_gas(validator_reward, gas_coin, _ge),   True)
            .field("🏛 Treasury Cut",  fmt_gas(treasury_cut, gas_coin, _ge),        True)
        )

        if not is_valid:
            _b.field("❌ Reason", "No valid transactions in this block", False)

        # Show up to 5 confirmed actions
        if confirmed:
            action_lines = []
            for r in confirmed[:5]:
                a = r["action"]
                tier_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(a["gas_price"], "⚪")
                import json as _j
                try:
                    p = _j.loads(a["payload"]) if isinstance(a["payload"], str) else a["payload"]
                except Exception:
                    p = {}
                if a["action_type"] == "send":
                    detail = f"{p.get('amount', '?')} {p.get('symbol', '?')} → {mention(p.get('to_user_id', 0), guild)}"
                elif a["action_type"] == "swap":
                    detail = f"{p.get('amount_in', '?')} {p.get('token_in', '?')} → {p.get('token_out', '?')}"
                elif a["action_type"] == "buy":
                    detail = f"${p.get('amount_usd', '?')} USD → {p.get('symbol', '?')}"
                elif a["action_type"] == "sell":
                    detail = f"{p.get('amount', '?')} {p.get('symbol', '?')} → USD"
                elif a["action_type"] == "stake":
                    detail = f"{p.get('amount', '?')} {p.get('symbol', '?')} → validator"
                elif a["action_type"] == "unstake":
                    detail = f"unstake {p.get('amount', '?')} {p.get('symbol', '?')}"
                elif a["action_type"] == "addlp":
                    detail = f"{p.get('amount_a', '?')} {p.get('token_a', '?')} + {p.get('amount_b', '?')} {p.get('token_b', '?')}"
                elif a["action_type"] == "removelp":
                    detail = f"{p.get('lp_shares', '?')} LP from pool {p.get('pool_id', '?')}"
                elif a["action_type"] == "contract_deploy":
                    detail = f"deploy `{p.get('name', '?')}` ({p.get('type', 'custom')})"
                elif a["action_type"] == "contract_call":
                    addr = p.get('address', '?')
                    detail = f"`{p.get('function', '?')}()` on `{addr[:10]}…`"
                else:
                    detail = r.get("reason", "")
                action_lines.append(
                    f"{tier_emoji} `{a['action_type'].upper()}` {mention(a['user_id'], guild)} {detail}  -  ⛽ {fmt_gas(a['gas_fee'], gas_coin, _ge)}"
                )
            if len(confirmed) > 5:
                action_lines.append(f"*...and {len(confirmed)-5} more*")
            _b.field(
                "✅ Processed",
                "\n".join(action_lines),
                False,
            )

        if rejected:
            rej_lines = []
            for r in rejected[:3]:
                a = r["action"]
                rej_lines.append(f"❌ `{a['action_type'].upper()}` {mention(a['user_id'], guild)}  -  {r['reason']}")
            _b.field("❌ Rejected", "\n".join(rej_lines), False)

        embed = _b.build()
        import datetime
        embed.timestamp = datetime.datetime.utcnow()
        try:
            from core.framework.links import sanitize_embed
            sanitize_embed(embed)
            await channel.send(embed=embed)
        except Exception:
            pass

        # DM each user whose action was processed in this block
        for r in results:
            a = r["action"]
            uid = a["user_id"]
            action_type = a["action_type"].upper()

            if r["success"]:
                dm_embed = (
                    card(
                        f"✅ {action_type} Confirmed",
                        description=f"Your action was confirmed in a validator block on **{network}**.",
                        color=C_SUCCESS,
                    )
                    .field("Network",    network,                                                True)
                    .field("Block ID",   f"#{block_id}",                                        True)
                    .field("Gas Paid",   fmt_gas(a['gas_fee'], gas_coin, _ge),                  True)
                    .field("Validator",  mention(validator['user_id'], guild),                   True)
                    .footer(guild.name)
                    .build()
                )
            else:
                dm_embed = (
                    card(
                        f"❌ {action_type} Rejected",
                        description=f"Your action was rejected by the validator on **{network}**. Your tokens have been refunded.",
                        color=C_ERROR,
                    )
                    .field("Reason",    r["reason"],                                                          False)
                    .field("Gas Paid",  f"{fmt_gas(a['gas_fee'], gas_coin, _ge)} (not refunded)",             True)
                    .field("Server",    guild.name,                                                           True)
                    .build()
                )

            await self._dm(uid, guild, dm_embed, category="validator", network=network)
            await asyncio.sleep(0.5)

        # DM the validator their reward
        if validator_reward > 0:
            val_dm = (
                card(
                    "🏆 Validator Reward",
                    description=f"You validated a block on **{network}** and earned gas fees.",
                    color=C_PURPLE,
                )
                .field("Reward",  f"**{fmt_gas(validator_reward, gas_coin, _ge)}**",  True)
                .field("Actions", str(len(results)),                                   True)
                .field("Block",   f"#{block_id}",                                     True)
                .footer(guild.name)
                .build()
            )
            await self._dm(validator["user_id"], guild, val_dm, category="validator", network=network)

    async def _on_contract_event(self, **kwargs) -> None:
        """Post a contract interaction summary to the contracts feed channel."""
        import json as _j, datetime
        guild     = kwargs["guild"]
        contract  = kwargs["contract"]
        action    = kwargs.get("action", "call")   # deploy|call|fund|withdraw|pause|resume
        caller_id = kwargs["caller_id"]
        block_id  = kwargs.get("block_id")
        events    = kwargs.get("events", [])
        extra     = kwargs.get("extra", {})
        function  = kwargs.get("function", "")

        settings = await self.bot.db.get_guild_settings(guild.id)
        channel_id = (
            settings.get("contracts_channel")
            or settings.get("validators_channel")
            or settings.get("trade_channel")
        )
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        addr  = contract["address"]
        name  = contract["name"]
        ctype = contract.get("type", "custom")

        _TITLES = {
            "deploy":   ("🚀 Contract Deployed",  C_SUCCESS),
            "call":     ("📜 Contract Call",       C_PURPLE),
            "fund":     ("💸 Contract Funded",     C_SUCCESS),
            "withdraw": ("💰 Contract Withdrawal", C_AMBER),
            "pause":    ("⏸ Contract Paused",      C_ERROR),
            "resume":   ("▶️ Contract Resumed",     C_SUCCESS),
        }
        title, color = _TITLES.get(action, ("📜 Contract", C_PURPLE))

        _b = (
            card(f"{title}  -  {name}", color=color)
            .field("Address", f"`{addr}`",                         False)
            .field("Type",    ctype,                                True)
            .field("Network", contract.get("network", "?"),        True)
            .field("By",      mention(caller_id, guild),            True)
        )

        if action == "call" and function:
            _b.field("Function", f"`{function}()`", True)
            _b.field("Total Calls", str(contract.get("call_count", 0) + 1), True)
        elif action == "deploy":
            desc_text = contract.get("description", "")
            if desc_text:
                _b.field("Description", desc_text, False)
            fn_names = list(contract.get("definition", {}).get("functions", {}).keys())
            if fn_names:
                _b.field("Functions", ", ".join(f"`{f}`" for f in fn_names), False)
        elif action in ("fund", "withdraw"):
            sym = extra.get("symbol", "?")
            amt = extra.get("amount", 0)
            cfg = Config.TOKENS.get(sym, {})
            _b.field(
                "Amount",
                f"**{fmt_token(amt, sym, cfg.get('emoji', ''))}**",
                True,
            )

        if block_id is not None:
            _b.field("Block", f"#{block_id}", True)

        if events:
            lines = []
            for e in events[:5]:
                raw = e.get("data", {})
                data = _j.loads(raw) if isinstance(raw, str) else raw
                data_str = ", ".join(f"{k}={v}" for k, v in data.items()) if data else ""
                lines.append(f"**{e['event']}**" + (f": {data_str}" if data_str else ""))
            _b.field("Events", "\n".join(lines), False)

        embed = _b.build()
        embed.timestamp = datetime.datetime.utcnow()
        try:
            from core.framework.links import sanitize_embed
            sanitize_embed(embed)
            await channel.send(embed=embed)
        except Exception:
            pass

    async def _on_whale_alert(self, guild: discord.Guild, user_id: int, action: str,
                               usd_value: float, **kwargs) -> None:
        """Handle whale alert events  -  post to channel and DM opted-in users."""
        action_labels = {
            "swap": "swapped", "buy": "bought", "sell": "sold",
            "transfer": "transferred", "stake": "staked", "unstake": "unstaked",
            "addlp": "added LP", "removelp": "removed LP",
            "deposit": "deposited", "withdraw": "withdrew",
            "gamble": "gambled", "loan": "borrowed",
            "liquidation": "liquidated", "send": "sent",
            "mining": "mined",
        }
        action_emojis = {
            "swap": "\U0001f501", "buy": "\U0001f7e2", "sell": "\U0001f534",
            "transfer": "\U0001f4e4", "stake": "\U0001f48e", "unstake": "\U0001f4e4",
            "addlp": "\U0001f30a", "removelp": "\U0001f30a",
            "deposit": "\U0001f3e6", "withdraw": "\U0001f3e6",
            "gamble": "\U0001f3b0", "loan": "\U0001f4b3",
            "liquidation": "\u26a0\ufe0f", "send": "\U0001f4e8",
            "mining": "\u26cf\ufe0f",
        }
        label = action_labels.get(action, action)
        action_emoji = action_emojis.get(action, "\U0001f4b0")
        symbol = kwargs.get("symbol", "")
        symbol_in = kwargs.get("symbol_in", "")
        symbol_out = kwargs.get("symbol_out", "")
        network = kwargs.get("network", "")
        amount = kwargs.get("amount", 0.0)
        amount_in = kwargs.get("amount_in", 0.0)
        amount_out = kwargs.get("amount_out", 0.0)

        # Build movement description
        if symbol_in and symbol_out:
            if amount_in and amount_out:
                movement = f"**{amount_in:,.4f} {symbol_in}** \u2192 **{amount_out:,.4f} {symbol_out}**"
            else:
                movement = f"**{symbol_in}** \u2192 **{symbol_out}**"
        elif symbol:
            if amount:
                movement = f"**{amount:,.4f} {symbol}**"
            else:
                movement = f"**{symbol}**"
        else:
            movement = ""

        _b = (
            card(f"\U0001f6a8 Whale Alert", color=C_AMBER)
            .field(f"{action_emoji} Action", f"**{label.title()}**", True)
            .field("\U0001f4b5 Value", f"**{fmt_usd(usd_value)}**", True)
            .field("\U0001f464 User", mention(user_id, guild, self.bot), True)
        )
        if movement:
            _b = _b.field("\U0001f4e6 Movement", movement, False)
        if network:
            _b = _b.field("\U0001f310 Network", network, True)
        embed = _b.timestamp().build()

        # Send to whale alerts channel
        await self._send(guild, "whale_alerts_channel", embed=embed)

        # DM opted-in users in the background. Serial per-user DB lookups plus
        # Discord's DM rate limits can take a minute for a large guild, and
        # bus.publish awaits every listener -- running this inline blocks the
        # originating command (e.g. gamble) until every DM is delivered.
        asyncio.create_task(
            self._broadcast_whale_dms(guild, user_id, embed, kwargs.get("network", "")),
        )

    async def _broadcast_whale_dms(
        self, guild: discord.Guild, user_id: int,
        embed: discord.Embed, alert_network: str,
    ) -> None:
        try:
            all_users = await self.bot.db.get_all_guild_users(guild.id)
            for u in all_users:
                if u["user_id"] == user_id:
                    continue  # don't DM the whale themselves
                prefs = await self.bot.db.get_user_prefs(u["user_id"], guild.id)
                if not prefs.get("dm_whale_alerts", 0):
                    continue
                # Per-network mute check for whale alerts
                if alert_network:
                    muted = await self.bot.db.get_muted_networks(u["user_id"], guild.id, "whale")
                    if alert_network.strip().lower() in muted:
                        continue
                await self._dm(u["user_id"], guild, embed)
        except Exception:
            pass

async def setup(bot: Discoin) -> None:
    await bot.add_cog(Trades(bot))
