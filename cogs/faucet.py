"""Faucet module  -  hourly crypto drops with random per-person token selection.

Each auto-faucet tick selects a random crypto for each individual claimant,
converting the USD value to that token at the current market price.  The base
USD value is scaled by the server's GDP and by the admin-configurable multiplier.

User-donated airdrops (`.airdrop`) are unchanged: the donor picks the token and
everyone who claims gets an equal share of that specific token.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.cooldowns import user_cooldown
from core.framework.links import sanitize_embed
from core.framework.middleware import ensure_registered, guild_only, module_allowed, module_cog_check, no_bots
from core.framework.ui import C_GOLD, C_SUBTLE, C_SUCCESS, fmt_ts, fmt_usd, mention
from core.framework.heartbeat import pulse, register_interval
from core.framework.scale import to_human, to_raw

log = logging.getLogger(__name__)

_COLLECT_WINDOW = Config.DROP_COLLECT_WINDOW  # seconds the faucet stays open

# Group tokens are distributed at a reduced rate (half the USD value) to keep
# built-in tokens appealing while still rewarding community token holders.
_GROUP_TOKEN_RATE: float = 0.5

# Canonical source lives in :mod:`core.framework.network`; keep the local alias so
# existing call sites don't need to change.
from core.framework.network import FULL_TO_SHORT as _NET_SHORT

# Tokens that are eligible for random faucet selection by default
# (stablecoins and fiat-pegged tokens are excluded to keep it interesting)
_FAUCET_ELIGIBLE: list[str] = [
    sym for sym, cfg in Config.TOKENS.items()
    if not cfg.get("stablecoin") and cfg.get("consensus") not in ("Fiat",)
]


class FaucetClaimButton(discord.ui.Button):
    """Button claimants click to enter a faucet drop."""

    def __init__(self, usd_value: float, *, is_airdrop: bool = False, symbol: str = "USD") -> None:
        if is_airdrop:
            label = f"Claim ${usd_value:,.2f}" if symbol == "USD" else f"Claim {usd_value:,.4f} {symbol}"
        else:
            label = f"Claim ~${usd_value:,.2f} in crypto"
        super().__init__(
            style=discord.ButtonStyle.success,
            label=label,
            emoji="🚰" if not is_airdrop else "💰",
            custom_id="faucet_claim",
        )
        self.usd_value = usd_value
        self.symbol = symbol  # fixed symbol for airdrops; "RANDOM" for auto-faucet
        self.is_airdrop = is_airdrop
        self.claimants: set[int] = set()
        self._lock = asyncio.Lock()

    async def callback(self, interaction: discord.Interaction) -> None:
        # Security check: locked-out users cannot claim
        engine = getattr(interaction.client, "security_engine", None)
        if engine and getattr(engine, "is_running", False):
            try:
                allowed, _reason = await engine.check_user_allowed(
                    interaction.guild_id, interaction.user.id, "all"
                )
                if not allowed:
                    await interaction.response.send_message(
                        "❌ Your account is currently restricted. Contact a server admin.",
                        ephemeral=True,
                    )
                    return
            except Exception:
                pass  # never block on security failure

        async with self._lock:
            if interaction.user.id in self.claimants:
                await interaction.response.send_message(
                    "You're already in  -  wait for the faucet to distribute!", ephemeral=True
                )
                return
            self.claimants.add(interaction.user.id)
            count = len(self.claimants)

        await interaction.response.send_message(
            f"✅ You're in! **{count}** claimer{'s' if count != 1 else ''} so far. "
            f"Claim window closes and distributes in **{_COLLECT_WINDOW}s**.",
            ephemeral=True,
        )

        self.label = f"🎉 {count} claimer{'s' if count != 1 else ''}  -  Click to join!"
        try:
            await interaction.message.edit(view=self.view)
        except Exception:
            pass


class FaucetView(discord.ui.View):
    def __init__(
        self,
        usd_value: float,
        faucet_cog: "Faucet",
        guild_id: int,
        *,
        is_airdrop: bool = False,
        symbol: str = "USD",
    ) -> None:
        super().__init__(timeout=_COLLECT_WINDOW)
        self.usd_value = usd_value
        self.is_airdrop = is_airdrop
        self.symbol = symbol
        self.faucet_cog = faucet_cog
        self.guild_id = guild_id
        self.button = FaucetClaimButton(usd_value, is_airdrop=is_airdrop, symbol=symbol)
        self.add_item(self.button)

    async def on_timeout(self) -> None:
        # Only auto-faucets register on the per-guild dedup slot; airdrops
        # bypass it so they can overlap. Popping here regardless used to
        # clear an unrelated active auto-faucet's slot whenever an airdrop
        # expired.
        if not self.is_airdrop:
            self.faucet_cog._active_faucets.pop(self.guild_id, None)
        self.button.disabled = True

        claimants = list(self.button.claimants)

        if not claimants:
            self.button.label = "Faucet expired"
            self.button.style = discord.ButtonStyle.secondary
            try:
                await self.message.edit(  # type: ignore[attr-defined]
                    embed=card(
                        "🚰 Faucet Expired",
                        description="Nobody claimed the faucet in time.",
                        color=C_SUBTLE,
                    ).build(),
                    view=self,
                )
            except Exception:
                pass
            return

        guild = self.message.guild  # type: ignore[attr-defined]
        db = self.faucet_cog.bot.db
        bus = self.faucet_cog.bot.bus

        if self.is_airdrop:
            await self._distribute_airdrop(claimants, guild, db, bus)
        else:
            await self._distribute_faucet(claimants, guild, db, bus)

    # ── Airdrop distribution (fixed token, equal share) ─────────────────

    async def _distribute_airdrop(self, claimants, guild, db, bus) -> None:
        symbol = self.symbol
        cut = round(self.usd_value / len(claimants), 6 if symbol != "USD" else 2)
        net_prefix: str = ""
        if symbol != "USD":
            token_cfg = Config.TOKENS.get(symbol, {})
            net_prefix = _NET_SHORT.get(token_cfg.get("network", ""), "")

        # DB ledger columns are raw-scaled NUMERIC(36,0); convert human cut -> raw once.
        cut_raw = to_raw(cut)
        async with db.atomic():
            for uid in claimants:
                await db.ensure_user(uid, guild.id)
                if symbol == "USD":
                    await db.update_wallet(uid, guild.id, cut_raw)
                elif net_prefix:
                    await db.update_wallet_holding(uid, guild.id, net_prefix, symbol, cut_raw)
                else:
                    await db.update_holding(uid, guild.id, symbol, cut_raw)

        cut_str = fmt_usd(cut) if symbol == "USD" else f"{cut:,.6f} {symbol}"
        winner_lines: list[str] = []
        for uid in claimants:
            tx_hash = await db.log_tx(
                guild.id, uid, "DROP_CLAIM",
                symbol_out=symbol, amount_out=cut_raw,
                network=net_prefix or "usd",
            )
            member = guild.get_member(uid)
            winner_lines.append(f"• {member.mention if member else mention(uid, guild)} → **{cut_str}**")
            await bus.publish(
                "drop_claimed",
                user=member or discord.Object(id=uid),
                amount=cut,
                guild=guild,
                tx_hash=tx_hash,
            )

        n = len(claimants)
        total_str = fmt_usd(self.usd_value) if symbol == "USD" else f"{self.usd_value:,.6f} {symbol}"
        self.button.label = f"Distributed to {n} claimer{'s' if n != 1 else ''}"
        self.button.style = discord.ButtonStyle.secondary
        embed = card(
            "💰 Airdrop Distributed!",
            description=(
                f"**{total_str}** split among **{n}** claimer{'s' if n != 1 else ''}  "
                f"(**{cut_str}** each)\n\n" + "\n".join(winner_lines)
            ),
            color=C_SUCCESS,
        ).build()
        try:
            await self.message.edit(embed=embed, view=self)  # type: ignore[attr-defined]
        except Exception:
            pass

    # ── Faucet distribution (random token per person, GDP-scaled) ────────

    async def _distribute_faucet(self, claimants, guild, db, bus) -> None:
        settings = await db.get_guild_settings(guild.id)
        faucet_multiplier = float(settings.get("faucet_multiplier") or 1.0)
        faucet_tokens_raw: str = settings.get("faucet_tokens") or ""

        # Total USD value after multiplier
        total_usd = round(self.usd_value * faucet_multiplier, 2)
        per_person_usd = round(total_usd / len(claimants), 2)

        # Determine eligible token pool. Admins can pin a whitelist via
        # faucet_tokens; otherwise default to USD + built-in tradeable tokens.
        if faucet_tokens_raw.strip():
            eligible = [s.strip().upper() for s in faucet_tokens_raw.split(",") if s.strip()]
            allow_group_tokens = True  # respect explicit inclusion below too
        else:
            eligible = ["USD"] + _FAUCET_ELIGIBLE
            allow_group_tokens = True

        # Pull group tokens (trading-enabled, has a bound network) and
        # fold them into the eligible pool.  They are distributed at
        # _GROUP_TOKEN_RATE of the per-person USD value to keep built-in
        # tokens the more attractive draw.
        group_token_syms: set[str] = set()
        group_networks: dict[str, str] = {}  # symbol -> net_name
        if allow_group_tokens:
            try:
                g_tokens = await db.get_group_tokens(guild.id)
            except Exception:
                g_tokens = []
            for gt in g_tokens or []:
                if not gt.get("trading_enabled"):
                    continue
                if gt.get("vault_locked"):
                    continue
                net_name = (gt.get("network") or "").strip()
                if not net_name:
                    continue
                sym = (gt.get("symbol") or "").upper()
                if not sym:
                    continue
                if faucet_tokens_raw.strip() and sym not in eligible:
                    # Admin whitelist active and this group token isn't on it.
                    continue
                group_token_syms.add(sym)
                group_networks[sym] = net_name
                if sym not in eligible:
                    eligible.append(sym)

        # Fetch current prices and filter to tokens with valid prices
        prices: dict[str, float] = {"USD": 1.0}
        for sym in eligible:
            if sym == "USD":
                continue
            try:
                row = await db.get_price(sym, guild.id)
                if row and float(row["price"]) > 0:
                    prices[sym] = float(row["price"])
            except Exception:
                pass

        eligible = [s for s in eligible if s in prices]
        if not eligible:
            eligible = ["USD"]

        winner_lines: list[str] = []
        # Credit each claimant their random token amount.
        # DB ledger columns are raw-scaled NUMERIC(36,0); always cross the
        # human -> raw boundary via to_raw() before calling update_*.
        per_person_usd_raw = to_raw(per_person_usd)
        per_person_usd_group = round(per_person_usd * _GROUP_TOKEN_RATE, 2)
        per_person_usd_group_raw = to_raw(per_person_usd_group)
        credited_usd: dict[int, int] = {}  # uid -> raw USD value actually credited
        from services.bottleneck import apply_bottleneck, CreditKind
        async with db.atomic():
            for uid in claimants:
                await db.ensure_user(uid, guild.id)
                sym = random.choice(eligible)
                price = prices[sym]
                is_group = sym in group_token_syms
                # Group tokens pay out half the per-person USD value.
                effective_usd = per_person_usd_group if is_group else per_person_usd
                effective_usd_raw = per_person_usd_group_raw if is_group else per_person_usd_raw
                credited_usd[uid] = effective_usd_raw
                if sym == "USD":
                    bn = await apply_bottleneck(
                        db, uid=uid, gid=guild.id,
                        gross_raw=effective_usd_raw, kind=CreditKind.FAUCET,
                    )
                    await db.update_wallet(uid, guild.id, bn.total_to_wallet_raw)
                    amount = bn.total_to_wallet_raw / 10**18
                    net_prefix = ""
                else:
                    gross_token_raw = to_raw(round(effective_usd / price, 8))
                    bn = await apply_bottleneck(
                        db, uid=uid, gid=guild.id,
                        gross_raw=gross_token_raw, kind=CreditKind.FAUCET,
                        symbol=sym, price_usd=price,
                    )
                    if is_group:
                        net_name = group_networks.get(sym, "")
                        net_prefix = _NET_SHORT.get(net_name, "")
                    else:
                        token_cfg = Config.TOKENS.get(sym, {})
                        net_prefix = _NET_SHORT.get(token_cfg.get("network", ""), "")
                    if net_prefix:
                        await db.update_wallet_holding(uid, guild.id, net_prefix, sym, bn.net_credit_raw)
                    else:
                        await db.update_holding(uid, guild.id, sym, bn.net_credit_raw)
                    if bn.boost_wallet_raw > 0:
                        await db.update_wallet(uid, guild.id, bn.boost_wallet_raw)
                    amount = bn.net_credit_raw / 10**18

                # Store per-claimant result for the summary embed
                if sym == "USD":
                    amount_str = fmt_usd(amount)
                else:
                    amount_str = f"{amount:,.6f} {sym}"
                if is_group:
                    amount_str += " *(group token, half value)*"
                member = guild.get_member(uid)
                winner_lines.append(
                    f"• {member.mention if member else mention(uid, guild)} → **{amount_str}**"
                )

        # Log transactions and publish events (outside atomic block).
        # Use the actual credited USD value so ledger audit + drop_claimed
        # downstream consumers see the right amount for group-token winners.
        for uid in claimants:
            try:
                uid_raw = credited_usd.get(uid, per_person_usd_raw)
                tx_hash = await db.log_tx(
                    guild.id, uid, "FAUCET_CLAIM",
                    symbol_out="USD", amount_out=uid_raw,
                    network="usd",
                )
                member = guild.get_member(uid)
                await bus.publish(
                    "drop_claimed",
                    user=member or discord.Object(id=uid),
                    amount=to_human(uid_raw),
                    guild=guild,
                    tx_hash=tx_hash,
                )
            except Exception:
                pass

        n = len(claimants)
        self.button.label = f"Distributed to {n} claimer{'s' if n != 1 else ''}"
        self.button.style = discord.ButtonStyle.secondary

        embed = card(
            "🚰 Faucet Distributed!",
            description=(
                f"**~${total_usd:,.2f}** in crypto split among **{n}** claimer{'s' if n != 1 else ''}\n"
                f"*(~${per_person_usd:,.2f} each  -  random token per person)*\n\n"
                + "\n".join(winner_lines)
            ),
            color=C_SUCCESS,
        ).build()
        try:
            await self.message.edit(embed=embed, view=self)  # type: ignore[attr-defined]
        except Exception:
            pass


class Faucet(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._active_faucets: dict[int, float] = {}  # guild_id → spawn timestamp
        self.auto_faucet_task.start()
        register_interval("faucet", Config.AUTO_DROP_INTERVAL)

    def cog_unload(self) -> None:
        self.auto_faucet_task.cancel()

    # ── Module check ────────────────────────────────────────────────────

    async def cog_check(self, ctx) -> bool:
        return await module_cog_check(self.bot, ctx, "faucet")

    # ── Auto-faucet background task ─────────────────────────────────────

    @tasks.loop(seconds=Config.AUTO_DROP_INTERVAL)
    async def auto_faucet_task(self) -> None:
        for guild in self.bot.guilds:
            channel = await self._find_channel(guild)
            if channel:
                await self._spawn_faucet(channel)
        pulse("faucet")

    @auto_faucet_task.before_loop
    async def before_auto_faucet(self) -> None:
        await self.bot.wait_until_ready()
        # No extra sleep here  -  the tasks.loop interval already adds the first delay.
        # Previously had a duplicate asyncio.sleep(AUTO_DROP_INTERVAL) which doubled
        # the wait (30 min + 30 min = 60 min) before the first drop on every restart.

    async def _find_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        settings = await self.bot.db.get_guild_settings(guild.id)
        ch_id = (
            settings.get("faucet_channel")
            or settings.get("drops_spawn_channel")
            or settings.get("drops_channel")
        )
        if ch_id:
            ch = guild.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
                return ch
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if perms.send_messages and perms.embed_links:
                return channel
        return None

    # ── Core faucet spawn ────────────────────────────────────────────────

    async def _spawn_faucet(
        self,
        channel: discord.TextChannel,
        *,
        amount: float | None = None,
        symbol: str = "USD",
        donor: discord.Member | None = None,
    ) -> bool:
        """Spawn a faucet/airdrop view in ``channel``. Returns True on success.

        User airdrops (``donor is not None``) are NEVER deduped -- every
        donation must produce its own message because the donor already
        paid before this runs. The previous behavior silently returned
        when another faucet was active, destroying the second user's
        tokens with no message. Auto-faucets still dedup per-guild so
        the bot doesn't spam multiple drops.
        """
        guild_id = channel.guild.id
        is_airdrop = donor is not None

        # Auto-faucet dedup (skipped for user airdrops).
        if not is_airdrop:
            spawn_ts = self._active_faucets.get(guild_id)
            if spawn_ts is not None:
                if time.time() - spawn_ts <= _COLLECT_WINDOW + 60:
                    return False
                self._active_faucets.pop(guild_id, None)

        if amount is None:
            # Auto-faucet: per-capita-supply-adaptive USD value.
            # Guild setting columns are stored as raw-scaled NUMERIC(36,0) (see
            # migration 0075_scaled_integers.sql), so descale to human USD here.
            settings = await self.bot.db.get_guild_settings(guild_id)
            _raw_min = settings.get("drop_min")
            _raw_max = settings.get("drop_max")
            drop_min = to_human(_raw_min) if _raw_min else Config.DROP_MIN
            drop_max = to_human(_raw_max) if _raw_max else Config.DROP_MAX
            base_amount = round(random.uniform(drop_min, drop_max), 2)
            _faucet_mult = float(settings.get("faucet_multiplier") or 1.0)
            try:
                from services.bottleneck import adaptive_faucet_multiplier
                _adapt = await adaptive_faucet_multiplier(self.bot.db, guild_id)
            except Exception:
                # Adaptive layer must never block the faucet itself.
                log.debug("faucet: adaptive multiplier failed", exc_info=True)
                _adapt = 1.0
            amount = round(base_amount * _faucet_mult * _adapt, 2)
            # Clamp at drop_min so a bone-dry server doesn't get a sub-floor
            # drop, and at drop_max * MAX_MULT so a brand-new server doesn't
            # blow through admin-configured ceilings.
            _ceiling = drop_max * float(Config.FAUCET_ADAPTIVE_MAX_MULT) * _faucet_mult
            amount = max(drop_min, min(amount, _ceiling))
            symbol = "USD"

        view = FaucetView(
            amount, self, guild_id,
            is_airdrop=is_airdrop,
            symbol=symbol if is_airdrop else "RANDOM",
        )

        expires_at = int(time.time() + _COLLECT_WINDOW)
        if is_airdrop:
            amount_str = fmt_usd(amount) if symbol == "USD" else f"{amount:,.6f} {symbol}"
            donor_line = f"\nDonated by {donor.mention}"
            title = "💰 Token Drop!"
            desc = (
                f"**{amount_str}** appeared!{donor_line}\n"
                f"Click the button to enter  -  distributes {fmt_ts(expires_at)}.\n"
                f"Everyone who clicks gets an equal share!"
            )
            footer = f"Closes {fmt_ts(expires_at)} · split equally among all claimers"
        else:
            amount_str = f"~${amount:,.2f}"
            title = "🚰 Crypto Faucet!"
            desc = (
                f"**{amount_str}** in crypto is up for grabs!\n"
                f"Click the button to enter  -  distributes {fmt_ts(expires_at)}.\n"
                f"Each claimer gets a **random token** worth their equal share!\n"
                f"*(You might get MTA, DSC, ARC, SUN, or more!)*"
            )
            footer = f"Closes {fmt_ts(expires_at)} · random crypto per person · equal USD value"

        embed = (
            card(title, description=desc, color=C_GOLD)
            .footer(footer)
            .build()
        )

        sanitize_embed(embed)

        try:
            msg = await channel.send(embed=embed, view=view)
        except Exception:
            # Only auto-faucets touch the dedup slot; airdrops bypass it.
            if not is_airdrop:
                self._active_faucets.pop(guild_id, None)
            raise
        # Only auto-faucets register on the dedup slot. Airdrops are
        # always free to overlap; the FaucetView's on_timeout knows to
        # skip the pop when its is_airdrop flag is set.
        if not is_airdrop:
            self._active_faucets[guild_id] = time.time()
        view.message = msg  # type: ignore[attr-defined]
        return True

    # ── $faucet (manual, mod-only) ───────────────────────────────────────

    @commands.hybrid_command(name="faucet", with_app_command=False)
    @guild_only
    @commands.has_permissions(manage_guild=True)
    async def manual_faucet(self, ctx: DiscoContext) -> None:
        """[Mod] Manually spawn a crypto faucet drop in this channel."""
        await self._spawn_faucet(ctx.channel)  # type: ignore[arg-type]

    # ── $airdrop (user-facing) ───────────────────────────────────────────

    @commands.hybrid_command(name="airdrop", with_app_command=False)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(10)
    async def user_airdrop(self, ctx: DiscoContext, amount: str, symbol: str = "USD") -> None:
        """Donate your own tokens to spawn a public drop in this channel.

        Usage: .airdrop <amount|all> [symbol]
        Example: .airdrop 500 USD    -  drops $500 from your wallet
                 .airdrop all DSD   -  drops your entire DSD balance
                 .airdrop 1.5 DSC   -  drops 1.5 DSC from your DeFi wallet
        """
        if not await module_allowed(ctx, "faucet"):
            await ctx.reply_error("The **faucet** module is disabled on this server.")
            return

        symbol = symbol.upper()
        _is_all = amount.lower() == "all"

        if not _is_all:
            try:
                amount_val = float(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number or `all`.")
                return
            if not math.isfinite(amount_val) or amount_val <= 0:
                await ctx.reply_error("Amount must be a positive, finite number.")
                return
        else:
            amount_val = 0.0  # resolved below per symbol

        if symbol == "USD":
            user_row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            # wallet is a raw-scaled NUMERIC(36,0) int via _coerce; descale for
            # comparisons/display and to_raw() the human delta for the DB write.
            bal_h = user_row.h("wallet") if user_row else 0.0
            if _is_all:
                amount_val = bal_h
            if amount_val <= 0:
                await ctx.reply_error(f"Nothing to airdrop. You have **${bal_h:,.2f}**.")
                return
            if bal_h < amount_val:
                await ctx.reply_error(f"Insufficient balance. You have **${bal_h:,.2f}**.")
                return
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -to_raw(amount_val))
            net_prefix = ""
        else:
            token_cfg = Config.TOKENS.get(symbol)
            if not token_cfg:
                await ctx.reply_error(f"Unknown token **{symbol}**.")
                return
            net_prefix = _NET_SHORT.get(token_cfg.get("network", ""), "")
            if not net_prefix:
                await ctx.reply_error(f"**{symbol}** cannot be airdropped (unsupported network).")
                return
            holding = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, net_prefix, symbol)
            have_h = holding.h("amount") if holding else 0.0
            if _is_all:
                amount_val = have_h
            if amount_val <= 0:
                await ctx.reply_error(f"Nothing to airdrop. You have **{have_h:,.6f} {symbol}**.")
                return
            if have_h < amount_val:
                await ctx.reply_error(
                    f"Insufficient **{symbol}** holdings. You have **{have_h:,.6f} {symbol}**."
                )
                return
            await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, net_prefix, symbol, -to_raw(amount_val))

        msg = f"✅ Dropping **{'$'+f'{amount_val:,.2f}' if symbol == 'USD' else f'{amount_val:,.6f} {symbol}'}**!"
        if ctx.interaction:
            await ctx.reply(msg, ephemeral=True)
        else:
            await ctx.reply(msg, mention_author=False, delete_after=5)
        # Spawn the airdrop. If it fails for ANY reason (channel send blocked,
        # permission missing, etc.) refund the donor so we never silently
        # destroy the tokens they paid up-front. Multiple concurrent
        # airdrops are allowed -- the dedup gate inside _spawn_faucet only
        # applies to auto-faucets now.
        spawned = False
        try:
            spawned = await self._spawn_faucet(
                ctx.channel, amount=amount_val, symbol=symbol, donor=ctx.author,  # type: ignore[arg-type]
            )
        except Exception:
            log.exception("airdrop: _spawn_faucet raised; refunding donor")
            spawned = False

        if not spawned:
            try:
                if symbol == "USD":
                    await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(amount_val))
                else:
                    await ctx.db.update_wallet_holding(
                        ctx.author.id, ctx.guild_id, net_prefix, symbol, to_raw(amount_val),
                    )
                await ctx.reply_error(
                    "Couldn't post the airdrop in this channel, so your tokens were refunded. "
                    "Try again in a different channel."
                )
            except Exception:
                log.exception(
                    "airdrop: refund FAILED for donor %s amount=%s %s -- manual fix needed",
                    ctx.author.id, amount_val, symbol,
                )


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Faucet(bot))
