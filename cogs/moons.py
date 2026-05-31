"""
cogs/moons.py -- Moons (MOON) economy: Lunar Mint (Slice 1).

Players stake a group token on Moon Network into the Lunar Mint and earn MOON
on an hourly tick. Emission is TWAP-valued, warmup-ramped, activity-bonused,
and capped per user / per guild / against MOON max_supply.
"""
from __future__ import annotations

import logging
import time
log = logging.getLogger(__name__)

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, module_allowed, no_bots
from core.framework.cooldowns import user_cooldown
from core.framework.utils import parse_amount
from core.framework.heartbeat import pulse, register_interval
from core.framework.embed import card
from core.framework.fuzzy import suggest_subcommand
from core.framework.scale import to_raw, to_human
from core.framework.ui import (
    C_NEUTRAL, C_PURPLE, C_AMBER, C_SUCCESS, C_WARNING, C_GOLD, FormatKit, fmt_token,
    fmt_ts, fmt_usd,
)

from constants.moons import (
    MOON_SYMBOL, MOON_NETWORK, MOON_NETWORK_SHORT,
    MOON_EMISSION_RATE, MOON_TWAP_WINDOW,
    GROUP_ACTIVITY_BONUS_MAX, GROUP_ACTIVITY_MIN_MINERS,
    GROUP_ACTIVITY_MIN_BLOCKS, GROUP_ACTIVITY_WINDOW_SECS,
    PER_USER_DAILY_MOON_CAP, PER_GUILD_DAILY_MOON_CAP, CAP_WINDOW_SECS,
    HOURLY_DRIP_FRACTION, MOON_POOL_MIN_STAKE,
    MOON_POOL_YIELD_BASKET, VAULT_LEVEL_EMISSION_BONUS,
    VAULT_LEVEL_EMISSION_BONUS_MAX, MOON_BURN_FEE_PCT,
    WRAPPED_STAKE_SYMBOLS, WRAPPED_STAKE_SELF_RATE, WRAPPED_STAKE_MOON_RATE,
    WRAPPED_STAKE_WARMUP_SECS, WRAPPED_STAKE_USER_MOON_CAP,
    WRAPPED_STAKE_GUILD_MOON_CAP, WRAPPED_STAKE_MIN, MOON_GAS_BURN_PCT,
)
import services.moon_gas as moon_gas

# Amount-argument words that mean "use the whole available balance".
_ALL_AMT = {"all", "everything", "max", "full", "entire", "total"}


def _staked_at_epoch(row: dict) -> float:
    """Coerce the lunar_stakes.staked_at column to epoch seconds."""
    sa = row.get("staked_at")
    if sa is None:
        return 0.0
    if hasattr(sa, "timestamp"):
        return sa.timestamp()
    return float(sa or 0)


async def _resolve_group_token(db, gid: int, sym: str) -> dict | None:
    """Return the token meta dict iff sym is a Moon Network group token."""
    tokens = await db.get_all_tokens_for_guild(gid)
    meta = tokens.get(sym)
    if not meta:
        return None
    if meta.get("token_type") != "group":
        return None
    if meta.get("network") != MOON_NETWORK:
        return None
    return meta


class MoonNavView(discord.ui.View):
    """Dropdown navigation for the /moon overview hub. Each option re-renders
    the message in place with that panel's embed -- the dropdown effectively
    runs the matching ,moon subcommand without leaving the hub."""

    _PANELS = [
        ("overview",    "Overview",     "\U0001F315"),
        ("help",        "Help",         "\U00002753"),
        ("stats",       "Stats",        "\U0001F4CA"),
        ("gas",         "Gas",          "\U000026FD"),
        ("supply",      "Supply",       "\U0001F4B0"),
        ("health",      "Health",       "\U0001F49A"),
        ("stakes",      "Your Stakes",  "\U0001F4CC"),
        ("pools",       "Pools",        "\U0001F30A"),
        ("burns",       "Burns",        "\U0001F525"),
        ("leaderboard", "Leaderboard",  "\U0001F3C6"),
    ]

    def __init__(self, cog: "Moons", gid: int, uid: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.gid = gid
        self.uid = uid
        self._select = discord.ui.Select(
            placeholder="Jump to a Moon Network panel...",
            options=[
                discord.SelectOption(label=lbl, value=val, emoji=emo)
                for val, lbl, emo in self._PANELS
            ],
        )
        self._select.callback = self._on_select
        self.add_item(self._select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.uid:
            await interaction.response.send_message(
                "This isn't your Moon hub -- run `/moon` yourself.", ephemeral=True,
            )
            return
        choice = self._select.values[0]
        try:
            embed = await self.cog._moon_panel(choice, self.gid, self.uid)
        except Exception:
            log.exception("MoonNavView panel render failed: %s", choice)
            await interaction.response.send_message(
                "That panel failed to load -- try again.", ephemeral=True,
            )
            return
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


class Moons(commands.Cog):
    """Moons (MOON) economy -- Lunar Mint (Slice 1)."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self.lunar_tick.start()
        register_interval("lunar_tick", 3600)

    def cog_unload(self) -> None:
        self.lunar_tick.cancel()

    async def cog_check(self, ctx) -> bool:
        if ctx.guild:
            if not await module_allowed(ctx, "moons"):
                raise commands.CheckFailure("The **moons** module is disabled on this server.")
        return True

    # ── Commands ──────────────────────────────────────────────────────────

    @commands.hybrid_group(name="moon", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def moon(self, ctx: DiscoContext) -> None:
        """Moons economy -- stake group tokens to earn MOON."""
        if await suggest_subcommand(ctx, self.moon):
            return
        await ctx.send_help(ctx.command)

    async def _bulk_stake_all_group_tokens(self, ctx: DiscoContext) -> None:
        """Iterate every group token on this guild's Moon Network and stake
        the user's entire holding for each. Used by `,moon stake everything`.

        Skips symbols the user does not hold. Uses the raw on-chain amount
        (no float round-trip) so the same safeguard that `moon pool stake
        all` relies on applies here too.
        """
        uid = ctx.author.id
        gid = ctx.guild_id

        tokens = await ctx.db.get_all_tokens_for_guild(gid)
        group_symbols = [
            (sym, meta) for sym, meta in tokens.items()
            if meta.get("token_type") == "group" and meta.get("network") == MOON_NETWORK
        ]
        if not group_symbols:
            await ctx.reply_error(
                "This server has no group tokens on Moon Network to stake."
            )
            return

        staked: list[tuple[str, str, float]] = []  # (emoji, symbol, qty_h)
        skipped_empty = 0
        for sym, meta in group_symbols:
            holding = await ctx.db.get_wallet_holding(uid, gid, MOON_NETWORK_SHORT, sym)
            avail_raw = int(holding["amount"]) if holding else 0
            if avail_raw <= 0:
                skipped_empty += 1
                continue

            async with ctx.db.atomic():
                await ctx.db.update_wallet_holding(uid, gid, MOON_NETWORK_SHORT, sym, -avail_raw)
                await ctx.db.upsert_lunar_stake(uid, gid, sym, avail_raw)
                await ctx.db.log_tx(
                    gid, uid, "LUNAR_STAKE",
                    symbol_in=sym, amount_in=avail_raw,
                    network=MOON_NETWORK_SHORT,
                )
            staked.append((meta.get("emoji", ""), sym, to_human(avail_raw)))

        if not staked:
            await ctx.reply_error(
                "You don't hold any group tokens on Moon Network right now. "
                "Mine or trade some first, then try `,moon stake everything` again."
            )
            return

        lines = [
            f"{emoji + ' ' if emoji else ''}**{sym}** +{fmt_token(qty_h, sym)}"
            for emoji, sym, qty_h in staked
        ]
        b = (
            card(f"\U0001F315 Lunar Mint -- Bulk Stake", color=C_PURPLE)
            .description(f"Staked every group token you held into the Lunar Mint.")
            .field(f"Positions opened / topped up ({len(staked)})", "\n".join(lines), False)
        )
        if skipped_empty:
            b = b.field(
                "Skipped",
                f"{skipped_empty} group token(s) you didn't hold",
                True,
            )
        b = b.footer(
            f"{ctx.prefix}moon info to track pending MOON on each position  |  "
            f"{ctx.prefix}moon autocompound on to auto-stake earned MOON into the Moon Pool"
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @moon.command(name="stake")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_stake(self, ctx: DiscoContext, symbol: str, amount: str = "") -> None:
        """Stake a group token into the Lunar Mint to earn MOON.

        `,moon stake everything` -- stake every group token you hold on
        Moon Network into the Lunar Mint in one shot (no SYMBOL needed).
        """
        uid = ctx.author.id
        gid = ctx.guild_id

        # Bulk-stake branch: ,moon stake everything  /  ,moon stake all
        # (no symbol means "stake all your Moon Network group tokens").
        _ALL_TOKENS = {"everything", "all", "max", "full", "entire", "total"}
        if symbol.lower() in _ALL_TOKENS:
            await self._bulk_stake_all_group_tokens(ctx)
            await self._stake_everything_extra(ctx)
            return

        # ,moon stake claim -- harvest pending wrapped-stake (mMTA/mSUN) rewards.
        if symbol.lower() in {"claim", "claims", "harvest", "collect", "rewards"}:
            await self._wrapped_claim_flow(ctx)
            return

        sym = symbol.upper()

        # Wrapped-asset dual-yield staking: stake mMTA -> earn mMTA + MOON.
        if sym in WRAPPED_STAKE_SYMBOLS:
            await self._wrapped_stake_flow(ctx, sym, amount)
            return

        # ,moon stake moon -- route MOON into the Moon Pool (Tier 2).
        if sym == MOON_SYMBOL:
            if not amount:
                await ctx.reply_error(
                    f"Usage: `{ctx.prefix}moon stake moon <amount|all>`"
                )
                return
            await self.moon_pool_stake.callback(self, ctx, amount)
            return

        if not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}moon stake <SYMBOL> <amount|all>` "
                f"or `{ctx.prefix}moon stake everything` to stake every group token you hold."
            )
            return

        meta = await _resolve_group_token(ctx.db, gid, sym)
        if not meta:
            await ctx.reply_error(f"**{sym}** is not a group token on Moon Network.")
            return

        holding = await ctx.db.get_wallet_holding(uid, gid, MOON_NETWORK_SHORT, sym)
        avail_raw = int(holding["amount"]) if holding else 0
        avail_h = to_human(avail_raw)

        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _is_all:
            qty_raw = avail_raw
            qty_h = avail_h
        else:
            try:
                parsed, usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number or `all`.")
                return
            if usd_mode:
                price_row = await ctx.db.get_price(sym, gid)
                px = float(price_row["price"]) if price_row and price_row.get("price", 0) else 0.0
                qty_h = parsed / px if px > 0 else 0.0
            else:
                qty_h = parsed
            qty_raw = to_raw(qty_h)

        if qty_raw <= 0:
            await ctx.reply_error(f"You have no **{sym}** available to stake.")
            return

        if qty_raw > avail_raw:
            await ctx.reply_error(
                f"You only have **{fmt_token(avail_h, sym)}** in your Moon Network wallet."
            )
            return

        existing = await ctx.db.get_lunar_stake(uid, gid, sym)
        if not await moon_gas.can_afford_gas(ctx.db, gid, uid, "stake"):
            await ctx.reply_error(
                f"You need **{moon_gas.gas_cost('stake'):.2f} MOON** for gas to stake."
            )
            return

        async with ctx.db.atomic():
            gas = await moon_gas.charge_gas(ctx.db, gid, uid, "stake")
            await ctx.db.update_wallet_holding(uid, gid, MOON_NETWORK_SHORT, sym, -qty_raw)
            row = await ctx.db.upsert_lunar_stake(uid, gid, sym, qty_raw)
            await ctx.db.log_tx(
                gid, uid, "LUNAR_STAKE",
                symbol_in=sym, amount_in=qty_raw,
                network=MOON_NETWORK_SHORT,
                gas_fee=to_raw(gas.cost) if gas.cost > 0 else 0,
                gas_coin="MOON" if gas.charged else "",
            )

        new_total_h = to_human(int(row["amount"]))
        staked_at_ts = _staked_at_epoch(row)
        action = "Topped up" if existing else "Opened"
        emoji = meta.get("emoji", "")

        embed = (
            card(f"{emoji} Lunar Mint -- {sym}", color=C_PURPLE)
            .description(f"{action} your **{sym}** lunar position.")
            .field("Staked this time", fmt_token(qty_h, sym), True)
            .field("New position", fmt_token(new_total_h, sym), True)
            .field(*moon_gas.gas_field(gas))
            .footer(f"Use {ctx.prefix}moon info {sym} to track pending MOON")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @moon.command(name="unstake")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_unstake(self, ctx: DiscoContext, symbol: str, amount: str = "all") -> None:
        """Withdraw from a lunar position. 5% burn if within 48h of opening."""
        uid = ctx.author.id
        gid = ctx.guild_id
        sym = symbol.upper()

        # ,moon unstake everything -- exit every Moon Network position.
        if sym in {"EVERYTHING", "ALL", "MAX", "FULL", "ENTIRE", "TOTAL"}:
            await self._moon_unstake_everything(ctx)
            return

        # Wrapped-asset staking tier (mMTA / mSUN).
        if sym in WRAPPED_STAKE_SYMBOLS:
            await self._wrapped_unstake_flow(ctx, sym, amount)
            return

        # ,moon unstake moon -- withdraw from the Moon Pool (Tier 2).
        if sym == MOON_SYMBOL:
            await self.moon_pool_unstake.callback(self, ctx, amount)
            return

        row = await ctx.db.get_lunar_stake(uid, gid, sym)
        if not row or int(row.get("amount", 0) or 0) <= 0:
            await ctx.reply_error(f"You have no **{sym}** lunar position.")
            return

        pos_raw = int(row["amount"])
        pos_h = to_human(pos_raw)

        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _is_all:
            qty_raw = pos_raw
            qty_h = pos_h
        else:
            try:
                parsed, _usd = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number or `all`.")
                return
            qty_h = parsed
            qty_raw = to_raw(qty_h)

        if qty_raw <= 0:
            await ctx.reply_error("Unstake amount must be positive.")
            return

        if qty_raw > pos_raw:
            qty_raw = pos_raw
            qty_h = pos_h

        # Early unstake penalty: 5% burn if inside the 48h window
        staked_at_ts = _staked_at_epoch(row)
        age = max(0.0, time.time() - staked_at_ts)
        penalty_raw = 0
        if age < Config.STAKING_EARLY_UNSTAKE_WINDOW:
            penalty_raw = int(qty_raw * Config.STAKING_EARLY_UNSTAKE_PENALTY)
        net_raw = qty_raw - penalty_raw

        if not await moon_gas.can_afford_gas(ctx.db, gid, uid, "unstake"):
            await ctx.reply_error(
                f"You need **{moon_gas.gas_cost('unstake'):.2f} MOON** for gas to unstake."
            )
            return

        async with ctx.db.atomic():
            gas = await moon_gas.charge_gas(ctx.db, gid, uid, "unstake")
            remaining_raw = await ctx.db.subtract_lunar_stake(uid, gid, sym, qty_raw)
            await ctx.db.update_wallet_holding(uid, gid, MOON_NETWORK_SHORT, sym, net_raw)
            if penalty_raw > 0:
                # Burn: decrement the group token's circulating supply in guild_tokens
                await ctx.db.execute(
                    "UPDATE guild_tokens SET circulating_supply = GREATEST(0, circulating_supply - $1) "
                    "WHERE guild_id=$2 AND symbol=$3",
                    penalty_raw, gid, sym,
                )
            if remaining_raw == 0:
                await ctx.db.reset_lunar_session(uid, gid, sym)
            await ctx.db.log_tx(
                gid, uid, "LUNAR_UNSTAKE",
                symbol_in=sym, amount_in=qty_raw,
                symbol_out=sym, amount_out=net_raw,
                network=MOON_NETWORK_SHORT,
                gas_fee=to_raw(gas.cost) if gas.cost > 0 else 0,
                gas_coin="MOON" if gas.charged else "",
            )

        meta = await _resolve_group_token(ctx.db, gid, sym) or {}
        emoji = meta.get("emoji", "")
        penalty_h = to_human(penalty_raw)
        net_h = to_human(net_raw)
        rem_h = to_human(remaining_raw)

        b = (
            card(f"{emoji} Lunar Mint -- Unstaked {sym}", color=C_PURPLE)
            .field("Withdrawn", fmt_token(qty_h, sym), True)
            .field("Returned", fmt_token(net_h, sym), True)
            .field("Remaining", fmt_token(rem_h, sym), True)
            .field(*moon_gas.gas_field(gas))
        )
        if penalty_raw > 0:
            pct = Config.STAKING_EARLY_UNSTAKE_PENALTY * 100
            b.field(
                "Early Unstake Burn",
                f"-{fmt_token(penalty_h, sym)} ({pct:.0f}% within 48h)",
                False,
            ).color(C_WARNING)
        await ctx.reply(embed=b.build(), mention_author=False)

    @moon.command(name="info")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_info(self, ctx: DiscoContext, symbol: str = "") -> None:
        """Show your lunar positions with APY, warmup, and pending MOON."""
        await self._render_info(ctx, symbol)

    @moon.command(name="list")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_list(self, ctx: DiscoContext) -> None:
        """Alias of `.moon info` -- show all lunar positions."""
        await self._render_info(ctx, "")

    async def _render_info(self, ctx: DiscoContext, symbol: str) -> None:
        uid = ctx.author.id
        gid = ctx.guild_id
        sym = symbol.upper() if symbol else ""

        if sym:
            row = await ctx.db.get_lunar_stake(uid, gid, sym)
            rows = [row] if row and int(row.get("amount", 0) or 0) > 0 else []
        else:
            rows = await ctx.db.get_lunar_stakes_for_user(uid, gid)

        if not rows:
            msg = (
                f"No lunar position for **{sym}**." if sym
                else "You have no active lunar positions."
            )
            b = (
                card("\U0001F315 Lunar Mint", color=C_NEUTRAL)
                .description(msg)
                .footer(f"Use {ctx.prefix}moon stake <SYMBOL> <amount> to open a position")
            )
            await ctx.reply(embed=b.build(), mention_author=False)
            return

        tokens = await ctx.db.get_all_tokens_for_guild(gid)
        b = card("\U0001F315 Lunar Mint -- Positions", color=C_PURPLE)
        total_session = 0.0
        total_earned = 0.0

        for row in rows:
            r_sym = row["symbol"]
            meta = tokens.get(r_sym, {})
            emoji = meta.get("emoji", "")
            stake_raw = int(row["amount"])
            stake_h = to_human(stake_raw)

            # Candles are keyed as "{SYMBOL}USD" by the drift loop, not by the
            # bare symbol. Every prior get_twap caller on this path has been
            # passing "CAT" / "COOK" / etc. and getting 0 back, because the
            # row it's looking for is actually stored under "CATUSD" /
            # "COOKUSD". Use the USD-denominated key so TWAP actually
            # resolves for group tokens. Spot price is still the fallback
            # while the candle history is thin.
            twap, _ = await ctx.db.get_twap(f"{r_sym}USD", gid, window=MOON_TWAP_WINDOW)
            if twap <= 0:
                price_row = await ctx.db.get_price(r_sym, gid)
                if price_row and price_row.get("price", 0):
                    twap = float(price_row["price"])
            stake_usd = stake_h * twap

            staked_at_ts = _staked_at_epoch(row)
            age = max(0.0, time.time() - staked_at_ts)
            warmup = min(1.0, age / Config.STAKING_WARMUP_SECONDS) if Config.STAKING_WARMUP_SECONDS > 0 else 1.0

            miners, blocks = await ctx.db.get_group_activity_for_token(
                gid, r_sym, GROUP_ACTIVITY_WINDOW_SECS,
            )
            m_ratio = min(1.0, miners / max(1, GROUP_ACTIVITY_MIN_MINERS))
            b_ratio = min(1.0, blocks / max(1, GROUP_ACTIVITY_MIN_BLOCKS))
            activity_mult = 1.0 + GROUP_ACTIVITY_BONUS_MAX * min(m_ratio, b_ratio)

            base_moons = stake_usd * MOON_EMISSION_RATE / 24.0
            pending = base_moons * warmup * activity_mult

            # APY estimate in % (nominal, pre-caps, post-activity-bonus)
            apy = MOON_EMISSION_RATE * 365.0 * activity_mult * 100.0

            session_h = float(row.get("session_earned", 0.0) or 0.0)
            lifetime_h = float(row.get("total_earned", 0.0) or 0.0)
            total_session += session_h
            total_earned += lifetime_h

            bar = FormatKit.bar(warmup, 1.0, width=10)
            body = (
                f"Staked: **{fmt_token(stake_h, r_sym)}** ({fmt_usd(stake_usd)})\n"
                f"Warmup: `{bar}`\n"
                f"Activity: {miners} miners / {blocks} blocks "
                f"(x{activity_mult:.2f})\n"
                f"APY (est): **{apy:.1f}%** | Next tick: "
                f"**{fmt_token(pending, MOON_SYMBOL)}**\n"
                f"Earned: {fmt_token(lifetime_h, MOON_SYMBOL)} "
                f"(session {fmt_token(session_h, MOON_SYMBOL)})\n"
                f"Opened: {fmt_ts(staked_at_ts)}"
            )
            b.field(f"{emoji} {r_sym}", body, False)

        b.footer(
            f"Lifetime MOON earned: {total_earned:.4f} "
            f"| {ctx.prefix}moon stake <SYMBOL> <amount>"
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── Autocompound toggle ───────────────────────────────────────────────

    @moon.command(name="autocompound", aliases=["ac"])
    @guild_only
    @no_bots
    @ensure_registered
    async def moon_autocompound(self, ctx: DiscoContext, mode: str = "status") -> None:
        """Toggle whether Lunar Mint MOON auto-stakes into the Moon Pool.

        ``,moon autocompound on`` -- earned MOON goes straight into your Moon
        Pool position each tick instead of landing in your Moon Network wallet.
        ``,moon autocompound off`` -- earned MOON lands in the wallet (default).
        ``,moon autocompound status`` -- show current setting.
        """
        uid = ctx.author.id
        gid = ctx.guild_id
        mode = (mode or "status").strip().lower()

        if mode == "status":
            row = await ctx.db.fetch_one(
                "SELECT moon_autocompound FROM user_prefs WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
            on = bool((row or {}).get("moon_autocompound"))
            b = card(
                "\U0001F315 Lunar Autocompound",
                color=C_SUCCESS if on else C_NEUTRAL,
            ).description(
                "\U0001F7E2 **On** -- Lunar Mint MOON auto-stakes into your Moon Pool position."
                if on else
                "\U0001F534 **Off** -- Lunar Mint MOON lands in your Moon Network wallet."
            ).footer(
                f"{ctx.prefix}moon autocompound on  |  {ctx.prefix}moon autocompound off"
            )
            await ctx.reply(embed=b.build(), mention_author=False)
            return

        if mode in {"on", "enable", "true", "1"}:
            new_val = True
        elif mode in {"off", "disable", "false", "0"}:
            new_val = False
        else:
            await ctx.reply_error("Usage: `,moon autocompound on|off|status`.")
            return

        await ctx.db.execute(
            """
            INSERT INTO user_prefs (user_id, guild_id, moon_autocompound)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, guild_id) DO UPDATE SET
                moon_autocompound = EXCLUDED.moon_autocompound
            """,
            uid, gid, new_val,
        )
        await ctx.reply_success(
            (
                "Lunar Mint MOON will auto-stake into your Moon Pool position "
                "from the next tick onward."
                if new_val else
                "Autocompound is off. Lunar Mint MOON will land in your wallet "
                "starting from the next tick."
            ),
            title="\U0001F315 Lunar Autocompound",
        )

    # ── Wrap / unwrap native mining coins ──────────────────────────────────

    @moon.command(name="wrap")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_wrap(self, ctx: DiscoContext, coin: str, amount: str) -> None:
        """Wrap native MTA or SUN into its Moon-Network equivalent (MMTA/MSUN).

        Usage: ``.moon wrap mta 0.5`` / ``.moon wrap sun 10``.

        Burns native MTA/SUN from your PoW-network DeFi wallet and mints the
        same amount of MMTA/MSUN into your Moon Network wallet. 1:1 peg --
        no fee. Use this to acquire the Moon-Network-side liquidity you need
        to swap into group tokens (every group pairs with MMTA or MSUN).
        """
        await self._wrap_flow(ctx, coin, amount, direction="wrap")

    @moon.command(name="unwrap")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_unwrap(self, ctx: DiscoContext, coin: str, amount: str) -> None:
        """Unwrap MMTA or MSUN back into its native coin.

        Usage: ``.moon unwrap mmta 0.5`` / ``.moon unwrap msun 10``.

        Burns MMTA/MSUN from your Moon Network wallet and credits the same
        amount of native MTA/SUN into your PoW-chain DeFi wallet. 1:1 peg,
        no fee.
        """
        await self._wrap_flow(ctx, coin, amount, direction="unwrap")

    async def _wrap_flow(
        self, ctx: DiscoContext, coin: str, amount: str, *, direction: str,
    ) -> None:
        """Shared implementation for ``.moon wrap`` and ``.moon unwrap``.

        Wrap mints an mCOIN on Moon Network in exchange for an equal amount
        of native COIN burned from the user's PoW-chain DeFi wallet. Unwrap
        does the reverse. Both operations are 1:1 with no fee -- the peg is
        enforced by arbitrage, not by the bot charging a spread.
        """
        from constants.moons import (
            wrapped_coin as _wrapped_coin,
            native_coin_for_wrapped as _native_for_wrapped,
            WRAPPED_FOR_NATIVE as _WRAPPED_FOR_NATIVE,
        )

        uid = ctx.author.id
        gid = ctx.guild_id
        coin_u = coin.upper()

        if direction == "wrap":
            if coin_u not in _WRAPPED_FOR_NATIVE:
                supported = ", ".join(sorted(_WRAPPED_FOR_NATIVE))
                await ctx.reply_error(
                    f"Only {supported} can be wrapped. Got `{coin}`."
                )
                return
            native_sym  = coin_u
            wrapped_sym = _wrapped_coin(native_sym)
            native_net  = Config.TOKENS.get(native_sym, {}).get("network", "")
        else:
            native_sym = _native_for_wrapped(coin_u) or ""
            if not native_sym:
                supported = ", ".join(_WRAPPED_FOR_NATIVE.values())
                await ctx.reply_error(
                    f"Only {supported} can be unwrapped. Got `{coin}`."
                )
                return
            wrapped_sym = coin_u
            native_net  = Config.TOKENS.get(native_sym, {}).get("network", "")

        native_net_short = {
            "Moneta Chain": "mta",
            "Sun Network":     "sun",
        }.get(native_net)
        if not native_net_short:
            await ctx.reply_error(
                f"No mining network configured for {native_sym} -- cannot {direction}."
            )
            return

        # Respect admin token disables. If either side of the trade is
        # disabled the op fails so admins can flip a kill switch and have
        # it actually stick (previously users could still mint wrapped
        # coins by calling wrap directly).
        if await ctx.db.is_token_disabled(gid, native_sym):
            await ctx.reply_error(f"**{native_sym}** trading is disabled in this guild.")
            return
        if await ctx.db.is_token_disabled(gid, wrapped_sym):
            await ctx.reply_error(f"**{wrapped_sym}** trading is disabled in this guild.")
            return

        # Balance read depends on direction.
        src_net_short = native_net_short if direction == "wrap" else MOON_NETWORK_SHORT
        src_sym       = native_sym if direction == "wrap" else wrapped_sym
        holding = await ctx.db.get_wallet_holding(uid, gid, src_net_short, src_sym)
        avail_raw = int(holding["amount"]) if holding else 0
        avail_h = to_human(avail_raw)

        is_all = amount.lower() in {"all", "everything", "max", "full"}
        if is_all:
            qty_raw = avail_raw
            qty_h = avail_h
        else:
            try:
                parsed, usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error(f"Couldn't parse `{amount}`. Examples: `0.5`, `all`.")
                return
            if usd_mode:
                # Convert USD -> coin via current oracle price.
                price_row = await ctx.db.get_price(src_sym, gid)
                price = float(price_row["price"]) if price_row else 0.0
                if price <= 0:
                    await ctx.reply_error(f"Price unavailable for {src_sym}; try a raw amount instead of USD.")
                    return
                qty_h = float(parsed) / price
            else:
                qty_h = float(parsed)
            qty_raw = to_raw(qty_h)

        if qty_raw <= 0:
            await ctx.reply_error(f"Amount must be positive.")
            return
        if qty_raw > avail_raw:
            await ctx.reply_error(
                f"Insufficient **{src_sym}** balance. You have "
                f"{fmt_token(avail_h, src_sym)}; tried {fmt_token(to_human(qty_raw), src_sym)}."
            )
            return

        # MOON gas pre-check (wrap is the free on-ramp; unwrap charges gas).
        if not await moon_gas.can_afford_gas(ctx.db, gid, uid, direction):
            await ctx.reply_error(
                f"You need **{moon_gas.gas_cost(direction):.2f} MOON** for gas "
                f"to {direction}."
            )
            return
        gas = moon_gas.GasResult(direction, 0.0, 0.0, 0.0, charged=False)

        # Atomic burn-on-one-side, mint-on-the-other. If either leg fails
        # the whole operation rolls back so the 1:1 peg can never be broken
        # by a partial-failure drift.
        try:
            async with ctx.db.atomic():
                if direction == "wrap":
                    # Burn native on its PoW chain, mint wrapped on Moon.
                    await ctx.db.update_wallet_holding(uid, gid, native_net_short, native_sym, -qty_raw)
                    await ctx.db.update_wallet_holding(uid, gid, MOON_NETWORK_SHORT, wrapped_sym, qty_raw)
                else:
                    # Burn wrapped on Moon, mint native on its PoW chain.
                    await ctx.db.update_wallet_holding(uid, gid, MOON_NETWORK_SHORT, wrapped_sym, -qty_raw)
                    await ctx.db.update_wallet_holding(uid, gid, native_net_short, native_sym, qty_raw)
                gas = await moon_gas.charge_gas(ctx.db, gid, uid, direction)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        # Log a transaction so .history shows the flow.
        tx_type = "MOON_WRAP" if direction == "wrap" else "MOON_UNWRAP"
        in_sym  = native_sym  if direction == "wrap" else wrapped_sym
        out_sym = wrapped_sym if direction == "wrap" else native_sym
        await ctx.db.log_tx(
            gid, uid, tx_type,
            symbol_in=in_sym,  amount_in=qty_raw,
            symbol_out=out_sym, amount_out=qty_raw,
            network=MOON_NETWORK_SHORT if direction == "wrap" else native_net_short,
            gas_fee=to_raw(gas.cost) if gas.cost > 0 else 0,
            gas_coin="MOON" if gas.charged else "",
        )

        verb = "Wrapped" if direction == "wrap" else "Unwrapped"
        arrow_from = f"{fmt_token(to_human(qty_raw), in_sym)}"
        arrow_to   = f"{fmt_token(to_human(qty_raw), out_sym)}"
        embed = card(
            f"\U0001F315 {verb}",
            description=(
                f"**{arrow_from}** -> **{arrow_to}** (1:1 peg, no spread).\n"
                f"Swap {out_sym} into any group token on Moon Network with "
                f"`{ctx.prefix}trade swap {out_sym} <GROUP_TOKEN> <amount>`.\n"
                f"{moon_gas.gas_line(gas)}"
                if direction == "wrap" else
                f"**{arrow_from}** -> **{arrow_to}** (1:1 peg, no spread).\n"
                f"Native {out_sym} is now in your {native_net} DeFi wallet.\n"
                f"{moon_gas.gas_line(gas)}"
            ),
            color=C_SUCCESS,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── Burn MOON for group tokens ─────────────────────────────────────────

    @moon.command(name="burn")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_burn(self, ctx: DiscoContext, amount: str) -> None:
        """Burn MOON for an equal-USD slice of every active guild group token.

        MOON is destroyed (circulating_supply decremented) and the USD value
        of the burn is split evenly across every group token registered on
        this guild's Moon Network. Each slice is converted to the token's
        symbol amount using the current crypto_prices row.

        Acts as an atomic AMM swap: MOON gets sell-side price impact (the
        oracle drops just like a `,trade sell`), and each group token in the
        basket gets buy-side impact (oracle rises like a `,trade buy`). A
        small ``MOON_BURN_FEE_PCT`` "gas" fee is taken in MOON on top of the
        burn and is destroyed as well -- there is no vault rebate.
        """
        uid = ctx.author.id
        gid = ctx.guild_id

        holding = await ctx.db.get_wallet_holding(uid, gid, MOON_NETWORK_SHORT, MOON_SYMBOL)
        avail_raw = int(holding["amount"]) if holding else 0
        avail_h = to_human(avail_raw)

        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _is_all:
            qty_raw = avail_raw
            qty_h = avail_h
        else:
            try:
                parsed, usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number or `all`.")
                return
            if usd_mode:
                price_row = await ctx.db.get_price(MOON_SYMBOL, gid)
                px = float(price_row["price"]) if price_row and price_row.get("price", 0) else 0.0
                qty_h = parsed / px if px > 0 else 0.0
            else:
                qty_h = parsed
            qty_raw = min(to_raw(qty_h), avail_raw)

        if qty_raw <= 0:
            await ctx.reply_error(f"You have no **{MOON_SYMBOL}** to burn.")
            return

        # Price MOON via the live crypto_prices row so the USD value of the
        # burn matches any other in-game valuation of MOON this tick.
        price_row = await ctx.db.get_price(MOON_SYMBOL, gid)
        moon_px = float(price_row["price"]) if price_row and price_row.get("price", 0) else 0.0
        if moon_px <= 0:
            await ctx.reply_error(f"**{MOON_SYMBOL}** has no price on this server; burn is unavailable.")
            return

        # ── Sell-side impact on MOON ──────────────────────────────────────
        # Mirror cogs/trade.py:2035 so a moon burn moves the MOON oracle the
        # same way a same-USD .sell would. Without this the burn was a free
        # exit valve  -  whales could dump MOON for group tokens with zero
        # price feedback.
        moon_circ_h = (
            price_row.h("circulating_supply")
            if price_row and hasattr(price_row, "h") else
            to_human(int((price_row or {}).get("circulating_supply", 0) or 0))
        )
        moon_gross_usd = qty_h * moon_px
        moon_mkt_cap = moon_px * moon_circ_h
        moon_impact = moon_gross_usd / Config.PRICE_IMPACT_DIVISOR
        if moon_mkt_cap > 0 and moon_gross_usd > 0.001 * moon_mkt_cap:
            mc_ratio = moon_gross_usd / moon_mkt_cap
            moon_impact *= min(1.0 + mc_ratio * 2.0, 5.0)
        moon_impact = min(moon_impact, 0.95)
        eff_moon_px = max(1e-9, moon_px * (1 - moon_impact))

        # ── Gas-like burn fee (taken in MOON, destroyed) ──────────────────
        fee_raw = int(qty_raw * MOON_BURN_FEE_PCT)
        net_qty_raw = qty_raw - fee_raw
        net_qty_h = to_human(net_qty_raw)
        fee_h = to_human(fee_raw)

        # Enumerate every group token on this guild's Moon Network. These are
        # the only targets for the burn basket; mining coins / stables / foreign
        # networks stay out of it.
        tokens = await ctx.db.get_all_tokens_for_guild(gid)
        group_tokens: list[tuple[str, str, float, float]] = []
        for sym, meta in tokens.items():
            if meta.get("token_type") != "group":
                continue
            if meta.get("network") != MOON_NETWORK:
                continue
            gp_row = await ctx.db.get_price(sym, gid)
            gp = float(gp_row["price"]) if gp_row and gp_row.get("price", 0) else 0.0
            if gp <= 0:
                continue
            gp_circ_h = (
                gp_row.h("circulating_supply")
                if gp_row and hasattr(gp_row, "h") else
                to_human(int((gp_row or {}).get("circulating_supply", 0) or 0))
            )
            group_tokens.append((sym, meta.get("emoji", ""), gp, gp_circ_h))

        if not group_tokens:
            await ctx.reply_error(
                "This server has no group tokens with live prices on Moon Network. "
                "Nothing to burn into."
            )
            return

        # USD value priced at the post-impact MOON oracle so the burner eats
        # the slippage the burn just caused, exactly like a sell would.
        burn_usd = net_qty_h * eff_moon_px
        per_slot_usd = burn_usd / len(group_tokens)

        # Pre-compute every credit + per-token buy impact so the atomic block
        # either lands the whole basket or none of it. Group token oracles are
        # nudged up by `eff_gp = gp * (1 + impact_gt)` -- same formula buys
        # use in cogs/trade.py:1866.
        credits: list[tuple[str, str, int, float, float, float, float]] = []
        for sym, emoji, gp, circ_h in group_tokens:
            mkt_cap = gp * circ_h
            impact = per_slot_usd / Config.PRICE_IMPACT_DIVISOR
            if mkt_cap > 0 and per_slot_usd > 0.001 * mkt_cap:
                mc_ratio = per_slot_usd / mkt_cap
                impact *= min(1.0 + mc_ratio * 2.0, 5.0)
            eff_gp = max(1e-9, gp * (1 + impact))
            tok_h = per_slot_usd / eff_gp
            tok_raw = to_raw(tok_h)
            if tok_raw <= 0:
                continue
            credits.append((sym, emoji, tok_raw, tok_h, gp, eff_gp, impact))

        if not credits:
            await ctx.reply_error("Burn amount is too small to produce any group token units.")
            return

        async with ctx.db.atomic():
            # Debit MOON (full amount including fee). update_wallet_holding
            # already decrements circulating_supply on the negative delta, so
            # the destruction of MOON is recorded once -- not twice.
            await ctx.db.update_wallet_holding(
                uid, gid, MOON_NETWORK_SHORT, MOON_SYMBOL, -qty_raw,
            )
            # Move the MOON oracle so the next burn / trade eats fresh slippage
            # instead of the pre-burn price.
            await ctx.db.update_price(MOON_SYMBOL, gid, eff_moon_px)
            # Mint each group token slice into the burner's wallet on the
            # Moon Network. update_wallet_holding bumps the token's own
            # circulating_supply so stats stay in sync. update_price moves the
            # oracle so subsequent buyers/sellers see the post-burn regime.
            for sym, emoji, tok_raw, tok_h, gp, eff_gp, impact in credits:
                await ctx.db.update_wallet_holding(
                    uid, gid, MOON_NETWORK_SHORT, sym, tok_raw,
                )
                await ctx.db.update_price(sym, gid, eff_gp)
                await ctx.db.add_trade_volume(gid, f"{sym}USD", to_raw(per_slot_usd))
                await ctx.db.log_tx(
                    gid, uid, "MOON_BURN_TO_GROUP",
                    symbol_in=MOON_SYMBOL, amount_in=qty_raw,
                    symbol_out=sym, amount_out=tok_raw,
                    network=MOON_NETWORK_SHORT,
                )
            # Volume on the MOON side too so candle volume reflects both legs
            # of the swap.
            await ctx.db.add_trade_volume(gid, f"{MOON_SYMBOL}USD", to_raw(moon_gross_usd))

        # Realign cached candle-open prices so the next drift tick builds on
        # the post-burn regime instead of pre-burn.
        await ctx.bot.bus.publish("prices_updated", guild=ctx.guild)

        lines: list[str] = []
        for sym, emoji, _raw, tok_h, gp, eff_gp, impact in credits:
            icon = f"{emoji} " if emoji else ""
            impact_str = f"  *(+{impact*100:.2f}%)*" if impact >= 0.0001 else ""
            lines.append(
                f"{icon}**{sym}** +{fmt_token(tok_h, sym)}  @ `{fmt_usd(eff_gp)}`{impact_str}"
            )

        # Discord caps embed field values at 1024 chars. Group token registries
        # can easily exceed that  -  truncate with a tail summary so the embed
        # never silently drops basket entries past the limit.
        basket_value = "\n".join(lines)
        if len(basket_value) > 1024:
            kept: list[str] = []
            running = 0
            for line in lines:
                add = len(line) + 1  # +1 for newline
                if running + add > 950:  # leave room for the "+N more" tail
                    break
                kept.append(line)
                running += add
            remaining = len(lines) - len(kept)
            kept.append(f"...and **+{remaining}** more group tokens")
            basket_value = "\n".join(kept)

        b = (
            card(f"\U0001F525 Burned {MOON_SYMBOL}", color=C_AMBER)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("\U0001F315 Burned",     fmt_token(net_qty_h, MOON_SYMBOL),  True)
            .field(
                "⛽ Gas Fee",
                f"{fmt_token(fee_h, MOON_SYMBOL)}\n"
                f"({MOON_BURN_FEE_PCT*100:.2f}% destroyed)",
                True,
            )
            .field("\U0001F4B5 USD Value",  fmt_usd(burn_usd),                  True)
            .field(
                "\U0001F4C9 MOON Fill",
                f"`{fmt_usd(moon_px)}` -> `{fmt_usd(eff_moon_px)}`\n"
                f"Slippage: `-{moon_impact*100:.3f}%`",
                True,
            )
            .field("\U0001F4E6 Tokens",     str(len(credits)),                  True)
            .field("\U0001F4B0 Per Slot",   fmt_usd(per_slot_usd),              True)
            .field("\U0001F9FA Basket",     basket_value,                       False)
            .footer(
                f"MOON sold + group tokens bought atomically  |  "
                f"{ctx.prefix}moon list to browse group tokens"
            )
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── Moon Pool (Tier 2) ────────────────────────────────────────────────

    @moon.group(name="pool", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def moon_pool(self, ctx: DiscoContext, c1: str = "", c2: str = "") -> None:
        """Moon Network liquidity pools.

        `,moon pool <C1> <C2>` shows one pool; `,moon pool add/remove` manage
        liquidity. `,moon pool stake/unstake/info` remain as the legacy Moon
        Pool (Tier 2) staking path -- `,moon stake moon` is the new one.
        """
        if c1 and c2:
            await self._moon_pool_detail(ctx, c1.upper(), c2.upper())
            return
        if await suggest_subcommand(ctx, self.moon_pool):
            return
        await ctx.reply(
            embed=await self._moon_pools_embed(ctx.guild_id), mention_author=False,
        )

    async def _moon_pool_detail(self, ctx: DiscoContext, c1: str, c2: str) -> None:
        """Render one Moon Network liquidity pool's reserves + LP shares."""
        a, b = sorted([c1, c2])
        row = await ctx.db.fetch_one(
            "SELECT * FROM pools WHERE guild_id=$1 AND pool_id=$2",
            ctx.guild_id, f"{a}-{b}",
        )
        if not row:
            await ctx.reply_error(f"No **{a}/{b}** liquidity pool on this server.")
            return
        embed = (
            card(f"\U0001F30A Moon Pool -- {a} / {b}", color=C_NEUTRAL)
            .field(f"{a} reserve", fmt_token(to_human(int(row['reserve_a'])), a), True)
            .field(f"{b} reserve", fmt_token(to_human(int(row['reserve_b'])), b), True)
            .field("LP Shares", f"{to_human(int(row['total_lp'])):,.4f}", True)
            .footer(f"{ctx.prefix}moon pool add {a} {b} <amt1> <amt2> to add liquidity")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _moon_delegate(
        self, ctx: DiscoContext, action: str, attr: str, *args: str,
    ) -> None:
        """Charge MOON gas, then run a Trade-cog command (swap / addlp /
        removelp). A failed delegate still costs gas -- mirrors how a real
        failed transaction still consumes gas."""
        gid, uid = ctx.guild_id, ctx.author.id
        if not await moon_gas.can_afford_gas(ctx.db, gid, uid, action):
            await ctx.reply_error(
                f"You need **{moon_gas.gas_cost(action):.2f} MOON** for gas."
            )
            return
        trade = self.bot.get_cog("Trade")
        target = getattr(trade, attr, None) if trade else None
        if target is None:
            await ctx.reply_error("That Moon Network action is unavailable right now.")
            return
        gas = await moon_gas.charge_gas(ctx.db, gid, uid, action)
        try:
            await target.callback(trade, ctx, *args)
        finally:
            if gas.charged:
                await ctx.send(moon_gas.gas_line(gas))

    @moon_pool.command(name="add")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_pool_add(
        self, ctx: DiscoContext, c1: str = "", c2: str = "",
        amt1: str = "", amt2: str = "",
    ) -> None:
        """Add liquidity to a Moon Network pool."""
        if not (c1 and c2 and amt1 and amt2):
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}moon pool add <C1> <C2> <amt1> <amt2>`"
            )
            return
        await self._moon_delegate(ctx, "pool_add", "addlp", c1, c2, amt1, amt2)

    @moon_pool.command(name="remove")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_pool_remove(
        self, ctx: DiscoContext, c1: str = "", c2: str = "", shares: str = "all",
    ) -> None:
        """Remove liquidity from a Moon Network pool."""
        if not (c1 and c2):
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}moon pool remove <C1> <C2> [shares|all]`"
            )
            return
        await self._moon_delegate(ctx, "pool_remove", "removelp", c1, c2, shares)

    @moon.command(name="swap")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_swap(
        self, ctx: DiscoContext, c1: str = "", c2: str = "", amount: str = "",
    ) -> None:
        """Swap Moon Network assets (charges MOON gas)."""
        if not (c1 and c2 and amount):
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}moon swap <FROM> <TO> <amount|all>`"
            )
            return
        await self._moon_delegate(ctx, "swap", "swap", c1, c2, amount)

    @moon_pool.command(name="stake")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_pool_stake(self, ctx: DiscoContext, amount: str) -> None:
        """Stake MOON into the Moon Pool (Tier 2) to earn MTA / ARC / DSC / SUN."""
        uid = ctx.author.id
        gid = ctx.guild_id

        holding = await ctx.db.get_wallet_holding(uid, gid, MOON_NETWORK_SHORT, MOON_SYMBOL)
        avail_raw = int(holding["amount"]) if holding else 0
        avail_h = to_human(avail_raw)

        gas_raw = to_raw(moon_gas.gas_cost("stake"))
        if amount.lower() in _ALL_AMT:
            # Reserve the MOON gas fee so "stake all" still leaves enough to
            # pay it. Use the raw DB value directly -- to_raw(to_human(raw))
            # can overshoot by a few base units on 18-decimal values.
            qty_raw = max(0, avail_raw - gas_raw)
            qty_h = to_human(qty_raw)
        else:
            try:
                parsed, usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number or `all`.")
                return
            if usd_mode:
                price_row = await ctx.db.get_price(MOON_SYMBOL, gid)
                px = float(price_row["price"]) if price_row and price_row.get("price", 0) else 0.0
                qty_h = parsed / px if px > 0 else 0.0
            else:
                qty_h = parsed
            qty_raw = min(to_raw(qty_h), avail_raw)

        if qty_raw <= 0:
            await ctx.reply_error(f"You have no **{MOON_SYMBOL}** available to stake.")
            return

        if qty_raw + gas_raw > avail_raw:
            await ctx.reply_error(
                f"You need **{fmt_token(to_human(qty_raw + gas_raw), MOON_SYMBOL)}** "
                f"(stake + {moon_gas.gas_cost('stake'):.2f} MOON gas) but only have "
                f"**{fmt_token(avail_h, MOON_SYMBOL)}**."
            )
            return

        existing = await ctx.db.get_moon_stake(uid, gid)
        has_position = bool(existing and int(existing.get("amount", 0) or 0) > 0)

        if not has_position and qty_h < MOON_POOL_MIN_STAKE:
            await ctx.reply_error(
                f"Opening a Moon Pool position requires at least "
                f"**{fmt_token(MOON_POOL_MIN_STAKE, MOON_SYMBOL)}**."
            )
            return

        async with ctx.db.atomic():
            gas = await moon_gas.charge_gas(ctx.db, gid, uid, "stake")
            await ctx.db.update_wallet_holding(
                uid, gid, MOON_NETWORK_SHORT, MOON_SYMBOL, -qty_raw,
            )
            row = await ctx.db.upsert_moon_stake(uid, gid, qty_raw)
            await ctx.db.log_tx(
                gid, uid, "MOON_POOL_STAKE",
                symbol_in=MOON_SYMBOL, amount_in=qty_raw,
                network=MOON_NETWORK_SHORT,
                gas_fee=to_raw(gas.cost) if gas.cost > 0 else 0,
                gas_coin="MOON" if gas.charged else "",
            )

        new_total_raw = int(row["amount"])
        new_total_h = to_human(new_total_raw)
        pool_total_raw = await ctx.db.get_moon_pool_total_raw(gid)
        share_pct = (new_total_raw / pool_total_raw * 100.0) if pool_total_raw > 0 else 0.0
        action = "Topped up" if has_position else "Opened"

        embed = (
            card(f"\U0001F315 Moon Pool -- {MOON_SYMBOL}", color=C_PURPLE)
            .description(f"{action} your Moon Pool position.")
            .field("Staked this time", fmt_token(qty_h, MOON_SYMBOL), True)
            .field("New position", fmt_token(new_total_h, MOON_SYMBOL), True)
            .field("Pool Share", f"{share_pct:.2f}%", True)
            .field(*moon_gas.gas_field(gas))
            .footer(f"Use {ctx.prefix}moon pool info to track pending yield")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @moon_pool.command(name="unstake")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_pool_unstake(self, ctx: DiscoContext, amount: str = "all") -> None:
        """Withdraw MOON from the Moon Pool. 5% burn if within 48h of opening."""
        uid = ctx.author.id
        gid = ctx.guild_id

        row = await ctx.db.get_moon_stake(uid, gid)
        if not row or int(row.get("amount", 0) or 0) <= 0:
            await ctx.reply_error("You have no Moon Pool position.")
            return

        pos_raw = int(row["amount"])
        pos_h = to_human(pos_raw)

        _is_all = amount.lower() in {"all", "everything", "max", "full", "entire", "total"}
        if _is_all:
            qty_raw = pos_raw
            qty_h = pos_h
        else:
            try:
                parsed, _usd = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number or `all`.")
                return
            qty_h = parsed
            qty_raw = to_raw(qty_h)

        if qty_raw <= 0:
            await ctx.reply_error("Unstake amount must be positive.")
            return

        if qty_raw > pos_raw:
            qty_raw = pos_raw
            qty_h = pos_h

        staked_at_ts = _staked_at_epoch(row)
        age = max(0.0, time.time() - staked_at_ts)
        penalty_raw = 0
        if age < Config.STAKING_EARLY_UNSTAKE_WINDOW:
            penalty_raw = int(qty_raw * Config.STAKING_EARLY_UNSTAKE_PENALTY)
        net_raw = qty_raw - penalty_raw

        async with ctx.db.atomic():
            remaining_raw = await ctx.db.subtract_moon_stake(uid, gid, qty_raw)
            await ctx.db.update_wallet_holding(
                uid, gid, MOON_NETWORK_SHORT, MOON_SYMBOL, net_raw,
            )
            # Charge gas AFTER the credit so the unstaked MOON itself covers
            # it -- a fully-staked player with no liquid MOON can still exit.
            gas = await moon_gas.charge_gas(ctx.db, gid, uid, "unstake")
            if penalty_raw > 0:
                # Burn MOON itself by decrementing circulating_supply on the
                # crypto_prices row for the Moon Network coin.
                await ctx.db.execute(
                    "UPDATE crypto_prices "
                    "SET circulating_supply = GREATEST(0, circulating_supply - $1) "
                    "WHERE symbol='MOON' AND guild_id=$2",
                    penalty_raw, gid,
                )
            if remaining_raw == 0:
                await ctx.db.execute(
                    "UPDATE moon_stakes SET session_earned = 0 "
                    "WHERE user_id=$1 AND guild_id=$2",
                    uid, gid,
                )
            await ctx.db.log_tx(
                gid, uid, "MOON_POOL_UNSTAKE",
                symbol_in=MOON_SYMBOL, amount_in=qty_raw,
                symbol_out=MOON_SYMBOL, amount_out=net_raw,
                network=MOON_NETWORK_SHORT,
                gas_fee=to_raw(gas.cost) if gas.cost > 0 else 0,
                gas_coin="MOON" if gas.charged else "",
            )

        penalty_h = to_human(penalty_raw)
        net_h = to_human(net_raw)
        rem_h = to_human(remaining_raw)

        b = (
            card(f"\U0001F315 Moon Pool -- Unstaked {MOON_SYMBOL}", color=C_PURPLE)
            .field("Withdrawn", fmt_token(qty_h, MOON_SYMBOL), True)
            .field("Returned", fmt_token(net_h, MOON_SYMBOL), True)
            .field("Remaining", fmt_token(rem_h, MOON_SYMBOL), True)
            .field(*moon_gas.gas_field(gas))
        )
        if penalty_raw > 0:
            pct = Config.STAKING_EARLY_UNSTAKE_PENALTY * 100
            b.field(
                "Early Unstake Burn",
                f"-{fmt_token(penalty_h, MOON_SYMBOL)} ({pct:.0f}% within 48h)",
                False,
            ).color(C_WARNING)
        await ctx.reply(embed=b.build(), mention_author=False)

    @moon_pool.command(name="info")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def moon_pool_info(self, ctx: DiscoContext) -> None:
        """Show your Moon Pool position and next-tick yield estimate."""
        uid = ctx.author.id
        gid = ctx.guild_id

        row = await ctx.db.get_moon_stake(uid, gid)
        if not row or int(row.get("amount", 0) or 0) <= 0:
            b = (
                card("\U0001F315 Moon Pool", color=C_NEUTRAL)
                .description(
                    f"You have no Moon Pool position. Stake at least "
                    f"**{fmt_token(MOON_POOL_MIN_STAKE, MOON_SYMBOL)}** to start "
                    f"earning a basket of MTA / ARC / DSC / SUN from Moon Network vault yield."
                )
                .footer(f"Use {ctx.prefix}moon pool stake <amount> to open a position")
            )
            await ctx.reply(embed=b.build(), mention_author=False)
            return

        stake_raw = int(row["amount"])
        stake_h = to_human(stake_raw)

        pool_total_raw = await ctx.db.get_moon_pool_total_raw(gid)
        distributable_h = await ctx.db.get_moon_vault_distributable(gid)
        share = (stake_raw / pool_total_raw) if pool_total_raw > 0 else 0.0
        share_pct = share * 100.0

        staked_at_ts = _staked_at_epoch(row)
        age_next = max(0.0, time.time() + 3600.0 - staked_at_ts)
        warmup_next = (
            min(1.0, age_next / Config.STAKING_WARMUP_SECONDS)
            if Config.STAKING_WARMUP_SECONDS > 0 else 1.0
        )
        age_now = max(0.0, time.time() - staked_at_ts)
        warmup_now = (
            min(1.0, age_now / Config.STAKING_WARMUP_SECONDS)
            if Config.STAKING_WARMUP_SECONDS > 0 else 1.0
        )

        next_drip_usd = distributable_h * HOURLY_DRIP_FRACTION * share * warmup_next

        # MOON spot for USD-value of stake + annualised APR at current vault burn.
        price_row = await ctx.db.get_price(MOON_SYMBOL, gid)
        moon_px = float(price_row["price"]) if price_row and price_row.get("price", 0) else 0.0
        stake_usd = stake_h * moon_px
        # Hourly yield extrapolated to a full year. This is illustrative, not
        # guaranteed -- vault refills from trade fees and drains as stakers
        # take it, so real APR swings with guild trading volume.
        apr_pct = 0.0
        if stake_usd > 0 and warmup_next > 0:
            yearly_usd = next_drip_usd * 24 * 365
            apr_pct = (yearly_usd / stake_usd) * 100.0

        # Per-token lifetime earned, from the MOON_POOL_YIELD tx log.
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
            f"`{r['symbol_out']}` {fmt_token(to_human(int(r['total_raw'])), r['symbol_out'])}"
            for r in yield_rows if int(r["total_raw"] or 0) > 0
        ]

        ac_row = await ctx.db.fetch_one(
            "SELECT moon_autocompound FROM user_prefs WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        ac_on = bool((ac_row or {}).get("moon_autocompound"))

        basket_label = " / ".join(sym for sym, _ in MOON_POOL_YIELD_BASKET)
        drip_hours = int(1.0 / HOURLY_DRIP_FRACTION)

        b = card(f"\U0001F315 Moon Pool -- {MOON_SYMBOL}", color=C_PURPLE).description(
            f"Staking MOON earns you an equal-USD basket of **{basket_label}** "
            f"each hour, funded by Moon Network trade fees."
        )

        # Your position -- the personal snapshot.
        b = b.field("Staked", fmt_token(stake_h, MOON_SYMBOL), True)
        b = b.field("Pool Share", f"{share_pct:.2f}%", True)
        b = b.field("Est. APR", f"{apr_pct:.1f}%" if apr_pct > 0 else "--", True)

        # What you'll get next tick.
        b = b.field(
            "Next Tick (est)",
            f"{fmt_usd(next_drip_usd)} split across {basket_label}",
            False,
        )

        # Warmup bar only when still ramping; 100% warmups just add noise.
        if warmup_now < 1.0:
            bar = FormatKit.bar(warmup_now, 1.0, width=10)
            b = b.field(
                "Warmup",
                f"`{bar}` {warmup_now * 100:.0f}% -- full yield at "
                f"{fmt_ts(staked_at_ts + Config.STAKING_WARMUP_SECONDS)}",
                False,
            )

        # Real lifetime earnings: the actual tokens that landed in the user's
        # wallets, not a USD-denominated summary that doesn't match anything
        # they can spend.
        b = b.field(
            "Lifetime Earned",
            "\n".join(earned_lines) if earned_lines else "_nothing paid out yet -- wait for the next hourly tick_",
            False,
        )

        # Guild-wide vault explainer. Users kept asking "what the hell is the
        # vault" because a $-figure in a personal card reads like something
        # they own; it's actually a shared refilling budget.
        b = b.field(
            "Guild Vault",
            f"{fmt_usd(distributable_h)} -- this is Moon Network's shared trade-fee pot, "
            f"split across every staker in the pool. {drip_hours}h of drips at the current "
            f"balance. Refills as players swap on Moon Network, drains as stakers take yield.",
            False,
        )

        b = b.field(
            "Autocompound",
            (
                "\U0001F7E2 **on** -- Lunar Mint MOON auto-stakes into this pool"
                if ac_on else
                f"\U0001F534 **off** -- Lunar Mint MOON lands in your wallet. "
                f"Flip with `{ctx.prefix}moon autocompound on`."
            ),
            False,
        )
        b = b.field("Opened", fmt_ts(staked_at_ts), True)

        b = b.footer(
            f"Drip: 1/{drip_hours} of vault per hour, equal USD split across {basket_label}."
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── Tick loop ─────────────────────────────────────────────────────────

    # ── Network info: stats / gas / supply / health / burns / leaderboard ─

    async def _moon_sum(self, gid: int, col: str, *where: str) -> float:
        """SUM a transactions column over Moon Network rows (human-scaled).

        ``col`` / ``where`` are code-controlled literals -- never user input."""
        clause = " AND ".join(where) if where else "TRUE"
        row = await self.bot.db.fetch_one(
            f"SELECT COALESCE(SUM({col}), 0) AS t FROM transactions "
            f"WHERE guild_id=$1 AND {clause}",
            gid,
        )
        return to_human(int(row["t"])) if row and row["t"] is not None else 0.0

    async def _moon_help_embed(self) -> discord.Embed:
        p = ","
        return (
            card("\U0001F315 Moon Network -- Command Guide", color=C_PURPLE)
            .description(
                "MOON is the gas and yield token of the bridged Moon Network. "
                "`/moon` opens this hub; every action below is a prefix command."
            )
            .field(
                "Staking",
                f"`{p}moon stakes` -- all your positions\n"
                f"`{p}moon stake moon|mmta|msun <amt>` -- open / top up\n"
                f"`{p}moon stake claim` -- harvest pending rewards\n"
                f"`{p}moon unstake <asset> <amt|all>` -- withdraw",
                False,
            )
            .field(
                "Assets",
                f"`{p}moon wrap <coin> <amt>` -- MTA/SUN -> mMTA/mSUN (free)\n"
                f"`{p}moon unwrap <coin> <amt>` -- back to native\n"
                f"`{p}moon burn <amt>` -- burn MOON for a group-token basket",
                False,
            )
            .field(
                "Network",
                f"`{p}moon stats` `{p}moon gas` `{p}moon supply`\n"
                f"`{p}moon health` `{p}moon burns` `{p}moon pools`\n"
                f"`{p}moon leaderboard` -- top wallets / stakers / burners",
                False,
            )
            .footer("Every Moon action costs MOON gas -- 60% burned, 40% to the vault.")
            .build()
        )

    async def _moon_stats_embed(self, gid: int) -> discord.Embed:
        """24h network activity: transactions, volume, users, gas, burns."""
        from datetime import datetime, timezone, timedelta
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        db = self.bot.db
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT user_id) AS u, "
            "       COALESCE(SUM(gas_fee), 0) AS g "
            "FROM transactions WHERE guild_id=$1 AND ts >= $2 "
            "AND (tx_type LIKE 'MOON%' OR tx_type LIKE 'LUNAR%')",
            gid, since,
        )
        n = int(row["n"]) if row else 0
        users = int(row["u"]) if row else 0
        gas_h = to_human(int(row["g"])) if row and row["g"] is not None else 0.0
        tps = n / 86400.0
        return (
            card("\U0001F4CA Moon Network -- 24h Stats", color=C_PURPLE)
            .field("Transactions", f"{n:,}", True)
            .field("Throughput", f"{tps:.4f} tx/s", True)
            .field("Active Users", f"{users:,}", True)
            .field("Gas Collected", f"{gas_h:.4f} MOON", True)
            .field("Gas Burned", f"{gas_h * MOON_GAS_BURN_PCT:.4f} MOON", True)
            .field("To Vault", f"{gas_h * (1 - MOON_GAS_BURN_PCT):.4f} MOON", True)
            .footer("Rolling 24h window across all Moon Network activity.")
            .build()
        )

    async def _moon_gas_embed(self, gid: int) -> discord.Embed:
        """Current per-action gas schedule + the recent average paid."""
        from constants.moons import MOON_GAS_COSTS
        avg_h = 0.0
        row = await self.bot.db.fetch_one(
            "SELECT AVG(gas_fee) AS a FROM transactions "
            "WHERE guild_id=$1 AND gas_coin='MOON' AND gas_fee > 0",
            gid,
        )
        if row and row["a"] is not None:
            avg_h = to_human(int(row["a"]))
        sched = "\n".join(
            f"`{act:<11}` {cost:.2f} MOON" + ("  (free on-ramp)" if cost <= 0 else "")
            for act, cost in MOON_GAS_COSTS.items()
        )
        return (
            card("\U000026FD Moon Network -- Gas", color=C_AMBER)
            .description(
                f"Flat per-action MOON fee. **{MOON_GAS_BURN_PCT*100:.0f}%** of every "
                f"fee is burned; the rest funds Moon Pool yield."
            )
            .field("Gas Schedule", sched, False)
            .field("Average Paid", f"{avg_h:.4f} MOON", True)
            .build()
        )

    async def _moon_supply_embed(self, gid: int) -> discord.Embed:
        db = self.bot.db
        price_row = await db.get_price(MOON_SYMBOL, gid)
        price = float(price_row["price"]) if price_row else 0.0
        circ_h = price_row.h("circulating_supply") if price_row else 0.0
        max_supply = float(Config.TOKENS[MOON_SYMBOL]["max_supply"])
        pct = (circ_h / max_supply * 100.0) if max_supply > 0 else 0.0
        emitted = await self._moon_sum(
            gid, "amount_out",
            "tx_type IN ('LUNAR_MINT', 'LUNAR_MINT_AUTOCOMPOUND', 'MOON_WCLAIM')",
            "symbol_out='MOON'",
        )
        gas_burned = await self._moon_sum(gid, "gas_fee", "gas_coin='MOON'")
        gas_burned *= MOON_GAS_BURN_PCT
        return (
            card("\U0001F315 Moon Network -- MOON Supply", color=C_PURPLE)
            .field("Max Supply", f"{max_supply:,.0f} MOON", True)
            .field("Circulating", f"{circ_h:,.0f} MOON", True)
            .field("% Circulating", f"{pct:.2f}%", True)
            .field("Lifetime Emitted", f"{emitted:,.2f} MOON", True)
            .field("Burned via Gas", f"{gas_burned:,.2f} MOON", True)
            .field("MOON Price", fmt_usd(price), True)
            .footer("Gas burn + caps are tuned to keep the network deflationary.")
            .build()
        )

    async def _moon_burns_embed(self, gid: int) -> discord.Embed:
        gas_total = await self._moon_sum(gid, "gas_fee", "gas_coin='MOON'")
        gas_burned = gas_total * MOON_GAS_BURN_PCT
        basket_burn = await self._moon_sum(
            gid, "amount_in", "tx_type='MOON_BURN_TO_GROUP'", "symbol_in='MOON'",
        )
        return (
            card("\U0001F525 Moon Network -- Burns", color=C_AMBER)
            .description("Every MOON destruction on the network, lifetime.")
            .field("Burned via Gas", f"{gas_burned:,.2f} MOON", True)
            .field("Burned via ,moon burn", f"{basket_burn:,.2f} MOON", True)
            .field("Total Burned", f"{gas_burned + basket_burn:,.2f} MOON", True)
            .footer("60% of every gas fee is burned; ,moon burn destroys MOON outright.")
            .build()
        )

    async def _moon_health_embed(self, gid: int) -> discord.Embed:
        db = self.bot.db
        distributable = 0.0
        try:
            distributable = await db.get_moon_vault_distributable(gid)
        except Exception:
            pass
        pool_total = 0.0
        try:
            pool_total = to_human(await db.get_moon_pool_total_raw(gid))
        except Exception:
            pass
        price_row = await db.get_price(MOON_SYMBOL, gid)
        price = float(price_row["price"]) if price_row else 0.0
        status = "Healthy" if distributable > 0 or pool_total > 0 else "Quiet"
        return (
            card("\U0001F49A Moon Network -- Health", color=C_SUCCESS)
            .field("Status", status, True)
            .field("MOON Price", fmt_usd(price), True)
            .field("Moon Pool Staked", f"{pool_total:,.2f} MOON", True)
            .field("Vault Yield Queued", fmt_usd(distributable), True)
            .footer("Vault yield drips to Moon Pool stakers each hour.")
            .build()
        )

    async def _moon_pools_embed(self, gid: int) -> discord.Embed:
        rows = await self.bot.db.fetch_all(
            "SELECT pool_id, token_a, token_b, reserve_a, reserve_b "
            "FROM pools WHERE guild_id=$1 "
            "AND (token_a IN ('MOON','MMTA','MSUN') OR token_b IN ('MOON','MMTA','MSUN')) "
            "ORDER BY pool_id LIMIT 20",
            gid,
        )
        b = card("\U0001F30A Moon Network -- Liquidity Pools", color=C_NEUTRAL)
        if not rows:
            b.description("No Moon Network liquidity pools yet.")
            return b.build()
        for r in rows:
            b.field(
                f"{r['token_a']} / {r['token_b']}",
                f"{fmt_token(to_human(int(r['reserve_a'])), r['token_a'])}\n"
                f"{fmt_token(to_human(int(r['reserve_b'])), r['token_b'])}",
                True,
            )
        b.footer("Add liquidity with ,addlp; swap with ,trade swap.")
        return b.build()

    async def _moon_leaderboard_embed(self, gid: int) -> discord.Embed:
        db = self.bot.db
        holders = await db.fetch_all(
            "SELECT user_id, amount FROM wallet_holdings "
            "WHERE guild_id=$1 AND network='moon' AND symbol='MOON' AND amount > 0 "
            "ORDER BY amount DESC LIMIT 5",
            gid,
        )
        burners = await db.fetch_all(
            "SELECT user_id, COALESCE(SUM(gas_fee), 0) AS g FROM transactions "
            "WHERE guild_id=$1 AND gas_coin='MOON' AND user_id IS NOT NULL "
            "GROUP BY user_id ORDER BY g DESC LIMIT 5",
            gid,
        )

        def _name(uid: int) -> str:
            m = self.bot.get_user(int(uid))
            return m.display_name if m else f"User {uid}"

        hold_lines = "\n".join(
            f"`{i+1}.` {_name(r['user_id'])} -- {fmt_token(to_human(int(r['amount'])), 'MOON')}"
            for i, r in enumerate(holders)
        ) or "Nobody holds MOON yet."
        burn_lines = "\n".join(
            f"`{i+1}.` {_name(r['user_id'])} -- "
            f"{to_human(int(r['g'])) * MOON_GAS_BURN_PCT:.2f} MOON"
            for i, r in enumerate(burners)
        ) or "No gas burned yet."
        return (
            card("\U0001F3C6 Moon Network -- Leaderboard", color=C_GOLD)
            .field("Top MOON Holders", hold_lines, False)
            .field("Top Burners (gas)", burn_lines, False)
            .build()
        )

    async def _moon_stakes_embed(self, gid: int, uid: int) -> discord.Embed:
        db = self.bot.db
        b = card("\U0001F315 Moon Network -- Your Stakes", color=C_PURPLE)
        any_pos = False

        lunar = await db.get_lunar_stakes_for_user(uid, gid)
        if lunar:
            any_pos = True
            lines = "\n".join(
                f"**{r['symbol']}** -- {fmt_token(to_human(int(r['amount'])), r['symbol'])} "
                f"(earned {r.h('total_earned') if hasattr(r, 'h') else 0:.2f} MOON)"
                for r in lunar
            )
            b.field("Lunar Mint (group tokens -> MOON)", lines, False)

        wrapped = await db.get_wrapped_stakes_for_user(uid, gid)
        if wrapped:
            any_pos = True
            lines = "\n".join(
                f"**{r['symbol']}** -- {fmt_token(to_human(int(r['amount'])), r['symbol'])} "
                f"| pending {fmt_token(to_human(int(r['pending_self'])), r['symbol'])} + "
                f"{fmt_token(to_human(int(r['pending_moon'])), 'MOON')}"
                for r in wrapped
            )
            b.field("Wrapped Staking (mMTA / mSUN dual-yield)", lines, False)

        pool = await db.get_moon_stake(uid, gid)
        if pool and int(pool.get("amount", 0) or 0) > 0:
            any_pos = True
            b.field(
                "Moon Pool (MOON -> MTA/ARC/DSC/SUN)",
                f"{fmt_token(to_human(int(pool['amount'])), 'MOON')} staked",
                False,
            )

        if not any_pos:
            b.description("You have no Moon Network stakes yet. Try `,moon stake mmta <amount>`.")
        return b.build()

    async def _moon_overview_embed(self, gid: int) -> discord.Embed:
        db = self.bot.db
        price_row = await db.get_price(MOON_SYMBOL, gid)
        price = float(price_row["price"]) if price_row else 0.0
        circ_h = price_row.h("circulating_supply") if price_row else 0.0
        pool_total = 0.0
        try:
            pool_total = to_human(await db.get_moon_pool_total_raw(gid))
        except Exception:
            pass
        return (
            card("\U0001F315 Moon Network", color=C_PURPLE)
            .description(
                "The bridged yield network. Use the dropdown below to explore "
                "stats, supply, staking, pools, burns and more."
            )
            .field("MOON Price", fmt_usd(price), True)
            .field("Circulating", f"{circ_h:,.0f} MOON", True)
            .field("Moon Pool Staked", f"{pool_total:,.0f} MOON", True)
            .footer("Every Moon action burns MOON -- a deflationary yield network.")
            .build()
        )

    async def _moon_panel(self, choice: str, gid: int, uid: int) -> discord.Embed:
        """Dispatch a nav-dropdown choice to its renderer."""
        if choice == "help":
            return await self._moon_help_embed()
        if choice == "stats":
            return await self._moon_stats_embed(gid)
        if choice == "gas":
            return await self._moon_gas_embed(gid)
        if choice == "supply":
            return await self._moon_supply_embed(gid)
        if choice == "health":
            return await self._moon_health_embed(gid)
        if choice == "burns":
            return await self._moon_burns_embed(gid)
        if choice == "pools":
            return await self._moon_pools_embed(gid)
        if choice == "leaderboard":
            return await self._moon_leaderboard_embed(gid)
        if choice == "stakes":
            return await self._moon_stakes_embed(gid, uid)
        return await self._moon_overview_embed(gid)

    @moon.command(name="help", aliases=["guide", "commands"])
    @guild_only
    async def moon_help(self, ctx: DiscoContext) -> None:
        """Moon Network command guide."""
        await ctx.reply(embed=await self._moon_help_embed(), mention_author=False)

    @moon.command(name="stats")
    @guild_only
    async def moon_stats(self, ctx: DiscoContext) -> None:
        """Moon Network 24h activity stats."""
        await ctx.reply(embed=await self._moon_stats_embed(ctx.guild_id), mention_author=False)

    @moon.command(name="gas")
    @guild_only
    async def moon_gas_cmd(self, ctx: DiscoContext) -> None:
        """Current Moon Network gas schedule."""
        await ctx.reply(embed=await self._moon_gas_embed(ctx.guild_id), mention_author=False)

    @moon.command(name="supply")
    @guild_only
    async def moon_supply(self, ctx: DiscoContext) -> None:
        """MOON supply: max, circulating, emitted, burned."""
        await ctx.reply(embed=await self._moon_supply_embed(ctx.guild_id), mention_author=False)

    @moon.command(name="health")
    @guild_only
    async def moon_health(self, ctx: DiscoContext) -> None:
        """Moon Network health snapshot."""
        await ctx.reply(embed=await self._moon_health_embed(ctx.guild_id), mention_author=False)

    @moon.command(name="burns")
    @guild_only
    async def moon_burns(self, ctx: DiscoContext) -> None:
        """Lifetime MOON burn stats."""
        await ctx.reply(embed=await self._moon_burns_embed(ctx.guild_id), mention_author=False)

    @moon.command(name="pools")
    @guild_only
    async def moon_pools(self, ctx: DiscoContext) -> None:
        """Moon Network liquidity pools."""
        await ctx.reply(embed=await self._moon_pools_embed(ctx.guild_id), mention_author=False)

    @moon.command(name="leaderboard", aliases=["lb", "top"])
    @guild_only
    async def moon_leaderboard(self, ctx: DiscoContext) -> None:
        """Top Moon Network wallets, stakers and burners."""
        await ctx.reply(embed=await self._moon_leaderboard_embed(ctx.guild_id), mention_author=False)

    @moon.command(name="stakes")
    @guild_only
    @no_bots
    @ensure_registered
    async def moon_stakes(self, ctx: DiscoContext) -> None:
        """All your Moon Network staking positions."""
        await ctx.reply(
            embed=await self._moon_stakes_embed(ctx.guild_id, ctx.author.id),
            mention_author=False,
        )

    @app_commands.command(name="moon", description="Moon Network overview hub")
    @app_commands.guild_only()
    async def moon_slash(self, interaction: discord.Interaction) -> None:
        """The only Moon Network slash command: an overview hub with a
        dropdown to every prefix sub-panel."""
        gid = interaction.guild_id
        uid = interaction.user.id
        embed = await self._moon_overview_embed(gid)
        view = MoonNavView(self, gid, uid)
        await interaction.response.send_message(embed=embed, view=view)

    # ── Bulk staking helpers (,moon stake/unstake everything) ─────────────

    async def _stake_everything_extra(self, ctx: DiscoContext) -> None:
        """After ,moon stake everything bulk-stakes group tokens, also stake
        any mMTA / mSUN / MOON the player still holds."""
        uid, gid = ctx.author.id, ctx.guild_id
        for sym in WRAPPED_STAKE_SYMBOLS:
            h = await ctx.db.get_wallet_holding(uid, gid, MOON_NETWORK_SHORT, sym)
            if h and int(h.get("amount", 0) or 0) > 0:
                await self._wrapped_stake_flow(ctx, sym, "all")
        mh = await ctx.db.get_wallet_holding(uid, gid, MOON_NETWORK_SHORT, MOON_SYMBOL)
        if mh and int(mh.get("amount", 0) or 0) > 0:
            await self.moon_pool_stake.callback(self, ctx, "all")

    async def _moon_unstake_everything(self, ctx: DiscoContext) -> None:
        """Withdraw every Moon Network staking position the player holds:
        wrapped (mMTA/mSUN), the Moon Pool, and all Lunar Mint positions."""
        uid, gid = ctx.author.id, ctx.guild_id
        done = 0
        for p in await ctx.db.get_wrapped_stakes_for_user(uid, gid):
            if int(p.get("amount", 0) or 0) > 0:
                await self._wrapped_unstake_flow(ctx, p["symbol"], "all")
                done += 1
        mp = await ctx.db.get_moon_stake(uid, gid)
        if mp and int(mp.get("amount", 0) or 0) > 0:
            await self.moon_pool_unstake.callback(self, ctx, "all")
            done += 1
        for r in await ctx.db.get_lunar_stakes_for_user(uid, gid):
            if int(r.get("amount", 0) or 0) > 0:
                await self.moon_unstake.callback(self, ctx, r["symbol"], "all")
                done += 1
        if done == 0:
            await ctx.reply_error("You have no Moon Network stakes to unstake.")

    # ── Wrapped-asset dual-yield staking (mMTA / mSUN) ────────────────────

    async def _wrapped_stake_flow(
        self, ctx: DiscoContext, sym: str, amount: str,
    ) -> None:
        """Stake mMTA or mSUN. The position accrues `sym` + MOON hourly into a
        pending bucket, claimed via ``,moon stake claim``."""
        uid, gid = ctx.author.id, ctx.guild_id
        if not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}moon stake {sym.lower()} <amount|all>`"
            )
            return

        holding = await ctx.db.get_wallet_holding(uid, gid, MOON_NETWORK_SHORT, sym)
        avail_raw = int(holding["amount"]) if holding else 0
        avail_h = to_human(avail_raw)

        if amount.lower() in _ALL_AMT:
            qty_raw, qty_h = avail_raw, avail_h
        else:
            try:
                parsed, usd_mode = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number or `all`.")
                return
            if usd_mode:
                pr = await ctx.db.get_price(sym, gid)
                px = float(pr["price"]) if pr and pr.get("price") else 0.0
                qty_h = parsed / px if px > 0 else 0.0
            else:
                qty_h = parsed
            qty_raw = to_raw(qty_h)

        if qty_raw <= 0:
            await ctx.reply_error(f"You have no **{sym}** to stake.")
            return
        if qty_raw > avail_raw:
            await ctx.reply_error(
                f"You only have **{fmt_token(avail_h, sym)}** in your Moon wallet."
            )
            return
        min_h = WRAPPED_STAKE_MIN.get(sym, 0.0)
        if qty_h < min_h:
            await ctx.reply_error(
                f"Minimum **{sym}** stake is **{fmt_token(min_h, sym)}**."
            )
            return

        if not await moon_gas.can_afford_gas(ctx.db, gid, uid, "stake"):
            await ctx.reply_error(
                f"You need **{moon_gas.gas_cost('stake'):.2f} MOON** for gas to "
                f"stake. Earn MOON in the Lunar Mint or swap into it."
            )
            return

        existing = await ctx.db.get_wrapped_stake(uid, gid, sym)
        async with ctx.db.atomic():
            gas = await moon_gas.charge_gas(ctx.db, gid, uid, "stake")
            await ctx.db.update_wallet_holding(
                uid, gid, MOON_NETWORK_SHORT, sym, -qty_raw,
            )
            row = await ctx.db.upsert_wrapped_stake(uid, gid, sym, qty_raw)
            # Reset the accrual cursor so the larger principal does not
            # back-credit hours it was not staked at that size.
            await ctx.db.touch_wrapped_accrual(uid, gid, sym)
            await ctx.db.log_tx(
                gid, uid, "MOON_WSTAKE",
                symbol_in=sym, amount_in=qty_raw,
                network=MOON_NETWORK_SHORT,
                gas_fee=to_raw(gas.cost) if gas.cost > 0 else 0,
                gas_coin="MOON" if gas.charged else "",
            )

        new_total_h = to_human(int(row["amount"]))
        embed = (
            card(f"\U0001F315 Moon Staking -- {sym}", color=C_PURPLE)
            .description(
                f"{'Topped up' if existing else 'Opened'} your **{sym}** stake. "
                f"It accrues **{sym} + MOON** every hour -- harvest with "
                f"`{ctx.prefix}moon stake claim`."
            )
            .field("Staked now", fmt_token(qty_h, sym), True)
            .field("Position", fmt_token(new_total_h, sym), True)
            .field(*moon_gas.gas_field(gas))
            .footer("12h warmup ramp -- yield scales in over the first half-day.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _wrapped_unstake_flow(
        self, ctx: DiscoContext, sym: str, amount: str,
    ) -> None:
        """Withdraw an mMTA/mSUN stake. 5% burn within 48h of opening. Pending
        rewards survive the unstake and stay claimable."""
        uid, gid = ctx.author.id, ctx.guild_id
        row = await ctx.db.get_wrapped_stake(uid, gid, sym)
        if not row or int(row.get("amount", 0) or 0) <= 0:
            await ctx.reply_error(f"You have no **{sym}** stake.")
            return

        pos_raw = int(row["amount"])
        pos_h = to_human(pos_raw)
        if amount.lower() in _ALL_AMT:
            qty_raw, qty_h = pos_raw, pos_h
        else:
            try:
                parsed, _usd = parse_amount(amount)
            except ValueError:
                await ctx.reply_error("Amount must be a number or `all`.")
                return
            qty_h = parsed
            qty_raw = to_raw(qty_h)
        if qty_raw <= 0:
            await ctx.reply_error("Unstake amount must be positive.")
            return
        if qty_raw > pos_raw:
            qty_raw, qty_h = pos_raw, pos_h

        if not await moon_gas.can_afford_gas(ctx.db, gid, uid, "unstake"):
            await ctx.reply_error(
                f"You need **{moon_gas.gas_cost('unstake'):.2f} MOON** for gas "
                f"to unstake."
            )
            return

        age = max(0.0, time.time() - _staked_at_epoch(row))
        penalty_raw = 0
        if age < Config.STAKING_EARLY_UNSTAKE_WINDOW:
            penalty_raw = int(qty_raw * Config.STAKING_EARLY_UNSTAKE_PENALTY)
        net_raw = qty_raw - penalty_raw

        async with ctx.db.atomic():
            gas = await moon_gas.charge_gas(ctx.db, gid, uid, "unstake")
            remaining = await ctx.db.subtract_wrapped_stake(uid, gid, sym, qty_raw)
            # Only the net is returned to the wallet; the penalty portion is
            # never re-credited, so it stays out of circulating supply.
            await ctx.db.update_wallet_holding(
                uid, gid, MOON_NETWORK_SHORT, sym, net_raw,
            )
            await ctx.db.log_tx(
                gid, uid, "MOON_WUNSTAKE",
                symbol_in=sym, amount_in=qty_raw,
                symbol_out=sym, amount_out=net_raw,
                network=MOON_NETWORK_SHORT,
                gas_fee=to_raw(gas.cost) if gas.cost > 0 else 0,
                gas_coin="MOON" if gas.charged else "",
            )

        b = (
            card(f"\U0001F315 Moon Staking -- Unstaked {sym}", color=C_PURPLE)
            .field("Withdrawn", fmt_token(qty_h, sym), True)
            .field("Returned", fmt_token(to_human(net_raw), sym), True)
            .field("Remaining", fmt_token(to_human(remaining), sym), True)
            .field(*moon_gas.gas_field(gas))
        )
        if penalty_raw > 0:
            b.field(
                "Early Unstake Burn",
                f"-{fmt_token(to_human(penalty_raw), sym)} "
                f"({Config.STAKING_EARLY_UNSTAKE_PENALTY * 100:.0f}% within 48h)",
                False,
            ).color(C_WARNING)
        b.footer(f"Pending rewards stay claimable -- {ctx.prefix}moon stake claim")
        await ctx.reply(embed=b.build(), mention_author=False)

    async def _wrapped_claim_flow(self, ctx: DiscoContext) -> None:
        """Harvest pending mMTA/mSUN-stake rewards into the wallet. With nothing
        pending, doubles as a status view of the player's wrapped stakes."""
        uid, gid = ctx.author.id, ctx.guild_id
        positions = await ctx.db.get_wrapped_stakes_for_user(uid, gid)
        claimable = [
            p for p in positions
            if int(p.get("pending_self", 0) or 0) > 0
            or int(p.get("pending_moon", 0) or 0) > 0
        ]

        if not claimable:
            active = [p for p in positions if int(p.get("amount", 0) or 0) > 0]
            if not active:
                await ctx.reply_error(
                    f"You have no Moon stakes. Open one with "
                    f"`{ctx.prefix}moon stake mmta <amount>`."
                )
                return
            b = card("\U0001F315 Moon Staking", color=C_NEUTRAL).description(
                "Nothing to claim yet -- mMTA/mSUN stakes accrue hourly."
            )
            for p in active:
                b.field(
                    p["symbol"],
                    f"Staked: {fmt_token(to_human(int(p['amount'])), p['symbol'])}",
                    True,
                )
            await ctx.reply(embed=b.build(), mention_author=False)
            return

        if not await moon_gas.can_afford_gas(ctx.db, gid, uid, "claim"):
            await ctx.reply_error(
                f"You need **{moon_gas.gas_cost('claim'):.2f} MOON** for gas "
                f"to claim."
            )
            return

        lines: list[str] = []
        async with ctx.db.atomic():
            gas = await moon_gas.charge_gas(ctx.db, gid, uid, "claim")
            first = True
            for p in claimable:
                sym = p["symbol"]
                self_raw, moon_raw = await ctx.db.claim_wrapped_pending(uid, gid, sym)
                if self_raw > 0:
                    await ctx.db.update_wallet_holding(
                        uid, gid, MOON_NETWORK_SHORT, sym, self_raw,
                    )
                if moon_raw > 0:
                    await ctx.db.update_wallet_holding(
                        uid, gid, MOON_NETWORK_SHORT, MOON_SYMBOL, moon_raw,
                    )
                await ctx.db.log_tx(
                    gid, uid, "MOON_WCLAIM",
                    symbol_in=sym, amount_in=self_raw,
                    symbol_out=MOON_SYMBOL, amount_out=moon_raw,
                    network=MOON_NETWORK_SHORT,
                    gas_fee=to_raw(gas.cost) if first and gas.cost > 0 else 0,
                    gas_coin="MOON" if first and gas.charged else "",
                )
                first = False
                lines.append(
                    f"**{sym}**  +{fmt_token(to_human(self_raw), sym)}"
                    f"  &  +{fmt_token(to_human(moon_raw), 'MOON')}"
                )

        embed = (
            card("\U0001F315 Moon Staking -- Rewards Claimed", color=C_SUCCESS)
            .description("\n".join(lines))
            .field(*moon_gas.gas_field(gas))
            .footer("Rewards keep accruing -- claim again anytime.")
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _tick_wrapped_stakes(self, db, gid: int, now: float) -> None:
        """Hourly accrual for mMTA/mSUN stakes: credit `sym` + MOON into each
        position's pending bucket, scaled by the 12h warmup and clamped by the
        per-user / per-guild MOON caps and MOON max-supply headroom."""
        rows = await db.get_wrapped_stakes_for_guild(gid)
        if not rows:
            return

        moon_price_row = await db.get_price(MOON_SYMBOL, gid)
        moon_px = float(moon_price_row["price"]) if moon_price_row else 0.0
        circ_h = moon_price_row.h("circulating_supply") if moon_price_row else 0.0
        headroom_h = max(
            0.0, float(Config.TOKENS[MOON_SYMBOL]["max_supply"]) - circ_h,
        )
        guild_budget = WRAPPED_STAKE_GUILD_MOON_CAP / 24.0
        user_hourly_cap = WRAPPED_STAKE_USER_MOON_CAP / 24.0
        guild_emitted = 0.0

        for row in rows:
            uid = row["user_id"]
            sym = row["symbol"]
            amount_h = to_human(int(row["amount"]))
            if amount_h <= 0:
                continue
            last = row.get("last_accrued_at")
            last_ts = last.timestamp() if hasattr(last, "timestamp") else float(last or now)
            elapsed = max(0.0, now - last_ts)
            if elapsed < 60.0:
                continue
            days = elapsed / 86400.0
            warmup = min(
                1.0,
                max(0.0, (now - _staked_at_epoch(row)) / WRAPPED_STAKE_WARMUP_SECS),
            )

            # Self-asset leg: a fraction of the staked amount.
            self_h = amount_h * WRAPPED_STAKE_SELF_RATE * days * warmup

            # MOON leg: a fraction of the staked USD value, valued in MOON,
            # then clamped by the caps and remaining max-supply headroom.
            price_row = await db.get_price(sym, gid)
            sym_px = float(price_row["price"]) if price_row else 0.0
            moon_h = 0.0
            if moon_px > 0:
                staked_usd = amount_h * sym_px
                moon_h = (staked_usd * WRAPPED_STAKE_MOON_RATE * days * warmup) / moon_px
            moon_h = max(0.0, min(
                moon_h, user_hourly_cap,
                max(0.0, guild_budget - guild_emitted),
                max(0.0, headroom_h - guild_emitted),
            ))
            guild_emitted += moon_h

            self_raw = to_raw(self_h)
            moon_raw = to_raw(moon_h)
            if self_raw <= 0 and moon_raw <= 0:
                await db.touch_wrapped_accrual(uid, gid, sym)
                continue
            earned_usd = self_h * sym_px + moon_h * moon_px
            await db.accrue_wrapped_pending(
                uid, gid, sym,
                self_raw=self_raw, moon_raw=moon_raw, earned_usd=earned_usd,
            )

    @tasks.loop(hours=1)
    async def lunar_tick(self) -> None:
        db = self.bot.db
        now = time.time()

        for guild in self.bot.guilds:
            gid = guild.id
            try:
                if not await db.module_enabled(gid, "moons"):
                    continue
                rows = await db.get_lunar_stakes_for_guild(gid)
            except Exception:
                log.exception("lunar_tick: failed to load stakes for guild %s", gid)
                continue

            if rows:
                # MOON max-supply headroom is shared across this guild's positions.
                price_row = await db.get_price(MOON_SYMBOL, gid)
                circ_h = price_row.h("circulating_supply") if price_row else 0.0
                max_supply = float(Config.TOKENS[MOON_SYMBOL]["max_supply"])
                headroom_h = max(0.0, max_supply - circ_h)

                for row in rows:
                    try:
                        await self._tick_row(db, gid, row, now, headroom_h)
                    except Exception:
                        log.exception(
                            "lunar_tick: row failed gid=%s uid=%s sym=%s",
                            gid, row.get("user_id"), row.get("symbol"),
                        )

            # Tier 2 distribution runs independently of Tier 1: a guild with
            # zero Lunar Mint stakers can still have MOON Pool stakers earning
            # DSD off the vault's accumulated distributable balance.
            try:
                await self._tick_distribute_moon_pool(guild)
            except Exception:
                log.exception(
                    "lunar_tick: moon pool distribution failed gid=%s", gid,
                )

            # Wrapped-asset staking (mMTA / mSUN) accrual -- independent of
            # both tiers above; accrues `sym` + MOON into pending buckets.
            try:
                await self._tick_wrapped_stakes(db, gid, now)
            except Exception:
                log.exception(
                    "lunar_tick: wrapped staking accrual failed gid=%s", gid,
                )

        pulse("lunar_tick")

    async def _tick_distribute_moon_pool(self, guild: discord.Guild) -> None:
        """Hourly Tier 2 distribution: drip 1/96 of the Moon Network vault's
        distributable balance to MOON stakers, pro-rata by staked amount.

        Yield is paid out as a small USD-equal slice of each network's native
        coin -- MTA, ARC, DSC, SUN (see MOON_POOL_YIELD_BASKET). MOON itself
        is not in the basket, keeping Tier 2 a pure revenue share with no
        inflation loop on the yield token. Per-symbol credits land in the
        native network wallet for each coin, same path as ordinary wallet
        holdings.
        """
        db = self.bot.db
        gid = guild.id

        distributable = await db.get_moon_vault_distributable(gid)
        if distributable <= 0:
            return

        stakes = await db.get_moon_stakes_for_guild(gid)
        if not stakes:
            return

        pool_total_raw = await db.get_moon_pool_total_raw(gid)
        if pool_total_raw <= 0:
            return

        drip = distributable * HOURLY_DRIP_FRACTION
        if drip <= 0:
            return

        # Resolve per-basket prices once per tick instead of once per staker.
        # Any symbol without a live price is dropped from the basket for this
        # tick; the USD budget is redistributed across whatever remains.
        basket_prices: list[tuple[str, str, float]] = []
        for sym, net_short in MOON_POOL_YIELD_BASKET:
            price_row = await db.get_price(sym, gid)
            px = float(price_row["price"]) if price_row and price_row.get("price", 0) else 0.0
            if px > 0:
                basket_prices.append((sym, net_short, px))
        if not basket_prices:
            log.warning(
                "[moon_pool] tick: no basket token has a price for gid=%s, skipping drip",
                gid,
            )
            return

        per_slot_share = 1.0 / len(basket_prices)

        now = time.time()
        paid_total_usd = 0.0

        for row in stakes:
            stake_raw = int(row["amount"])
            if stake_raw <= 0:
                continue
            staked_at_ts = _staked_at_epoch(row)
            warmup = (
                min(1.0, max(0.0, now - staked_at_ts) / Config.STAKING_WARMUP_SECONDS)
                if Config.STAKING_WARMUP_SECONDS > 0 else 1.0
            )
            if warmup <= 0:
                continue
            share = stake_raw / pool_total_raw
            payout_usd = drip * share * warmup  # USD-equivalent (vault is DSD-denominated)
            if payout_usd <= 0:
                continue

            uid = int(row["user_id"])
            # Pre-compute per-slot raw amounts so an empty basket entry (price
            # dropped out) doesn't leave the atomic() half-applied.
            credits: list[tuple[str, str, int]] = []
            for sym, net_short, px in basket_prices:
                slot_usd = payout_usd * per_slot_share
                tok_raw = to_raw(slot_usd / px)
                if tok_raw <= 0:
                    continue
                credits.append((sym, net_short, tok_raw))
            if not credits:
                continue

            from services.bottleneck import apply_bottleneck, CreditKind
            async with self.bot.db.atomic():
                _boost_total_raw = 0
                for sym, net_short, tok_raw in credits:
                    _bn_mp = await apply_bottleneck(
                        db, uid=uid, gid=gid,
                        gross_raw=int(tok_raw),
                        kind=CreditKind.NETWORK_CLAIM, symbol=sym,
                    )
                    if _bn_mp.net_credit_raw > 0:
                        await db.update_wallet_holding(
                            uid, gid, net_short, sym, int(_bn_mp.net_credit_raw),
                        )
                        await db.log_tx(
                            gid, uid, "MOON_POOL_YIELD",
                            symbol_in=MOON_SYMBOL, amount_in=stake_raw,
                            symbol_out=sym, amount_out=int(_bn_mp.net_credit_raw),
                            network=net_short,
                        )
                    if _bn_mp.boost_wallet_raw > 0:
                        _boost_total_raw += int(_bn_mp.boost_wallet_raw)
                if _boost_total_raw > 0:
                    await db.update_wallet(uid, gid, _boost_total_raw)
                await db.record_moon_earnings(uid, gid, payout_usd)
            paid_total_usd += payout_usd

        if paid_total_usd > 0:
            await db.drain_moon_vault_distributable(gid, paid_total_usd)

    async def _tick_row(
        self, db, gid: int, row: dict, now: float, headroom_h: float,
    ) -> None:
        uid = int(row["user_id"])
        sym = row["symbol"]
        stake_raw = int(row["amount"])
        if stake_raw <= 0:
            return

        stake_h = to_human(stake_raw)
        # 24h TWAP is the preferred valuation (kills the whale-pump vector on
        # a thinly-traded group token). It is only defined when the token
        # has >= 2 one-minute candles in the window though, so a freshly
        # launched or dormant group token returns 0 here. Fall back to the
        # spot price row in that case -- a just-created group token has a
        # seeded crypto_prices row at 0.01 which is correct for emission.
        # Pump risk is already bounded by the per-user / per-guild / max-
        # supply caps below, so falling back is safer than emitting nothing.
        # Candles are keyed as "{SYMBOL}USD", not the bare symbol -- see the
        # drift loop in cogs/trade.py :: _drift_guild. Passing "CAT" here
        # always returned 0 which forced the spot fallback below; using the
        # right key lets the TWAP anti-pump valuation actually protect us.
        twap, _ = await db.get_twap(f"{sym}USD", gid, window=MOON_TWAP_WINDOW)
        if twap <= 0:
            price_row = await db.get_price(sym, gid)
            if price_row:
                twap = float(price_row["price"])
        stake_usd = stake_h * twap
        if stake_usd <= 0:
            return  # No price data at all -- skip, do not emit against $0

        staked_at_ts = _staked_at_epoch(row)
        age = max(0.0, now - staked_at_ts)
        warmup = min(1.0, age / Config.STAKING_WARMUP_SECONDS) if Config.STAKING_WARMUP_SECONDS > 0 else 1.0

        miners, blocks = await db.get_group_activity_for_token(
            gid, sym, GROUP_ACTIVITY_WINDOW_SECS,
        )
        m_ratio = min(1.0, miners / max(1, GROUP_ACTIVITY_MIN_MINERS))
        b_ratio = min(1.0, blocks / max(1, GROUP_ACTIVITY_MIN_BLOCKS))
        activity_mult = 1.0 + GROUP_ACTIVITY_BONUS_MAX * min(m_ratio, b_ratio)

        # Vault-level bonus: +VAULT_LEVEL_EMISSION_BONUS per Moon Network vault
        # level, capped at VAULT_LEVEL_EMISSION_BONUS_MAX. Rewards servers whose
        # Moon Network has actual trade volume flowing through the vault.
        vault_row = await db.fetch_one(
            "SELECT level FROM network_vaults WHERE guild_id=$1 AND network='moon'",
            gid,
        )
        vault_level = int(vault_row["level"]) if vault_row else 0
        level_bonus = min(
            VAULT_LEVEL_EMISSION_BONUS_MAX,
            VAULT_LEVEL_EMISSION_BONUS * vault_level,
        )
        level_mult = 1.0 + level_bonus

        base_moons = stake_usd * MOON_EMISSION_RATE / 24.0
        moons_h = base_moons * warmup * activity_mult * level_mult

        user_mined = await db.get_user_moon_minted_recent(uid, gid, now - CAP_WINDOW_SECS)
        moons_h = min(moons_h, max(0.0, PER_USER_DAILY_MOON_CAP - user_mined))
        guild_mined = await db.get_guild_moon_minted_recent(gid, now - CAP_WINDOW_SECS)
        moons_h = min(moons_h, max(0.0, PER_GUILD_DAILY_MOON_CAP - guild_mined))
        moons_h = min(moons_h, headroom_h)

        if moons_h <= 0:
            return

        moons_raw = to_raw(moons_h)
        if moons_raw <= 0:
            return

        # Autocompound: when on, the freshly-minted MOON bypasses the wallet
        # and is staked directly into the Moon Pool on the same tick. Supply
        # is still incremented (MOON is minted into existence) and the stake
        # upsert is pass-through; users see the position grow without any
        # further action. Opt-in, default off, toggled via ,moon autocompound.
        ac_row = await db.fetch_one(
            "SELECT moon_autocompound FROM user_prefs WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        ac_on = bool((ac_row or {}).get("moon_autocompound"))

        async with self.bot.db.atomic():
            if ac_on:
                # Stake straight into the Moon Pool. Skip the wallet round-trip
                # so there's no dust lost to to_raw float-rounding.
                await db.upsert_moon_stake(uid, gid, moons_raw)
            else:
                await db.update_wallet_holding(
                    uid, gid, MOON_NETWORK_SHORT, MOON_SYMBOL, moons_raw,
                )
            await db.execute(
                "UPDATE crypto_prices SET circulating_supply = circulating_supply + $1 "
                "WHERE symbol='MOON' AND guild_id=$2",
                moons_raw, gid,
            )
            await db.record_lunar_earnings(uid, gid, sym, moons_h)
            await db.log_tx(
                gid, uid,
                "LUNAR_MINT_AUTOCOMPOUND" if ac_on else "LUNAR_MINT",
                symbol_in=sym, amount_in=stake_raw,
                symbol_out=MOON_SYMBOL, amount_out=moons_raw,
                network=MOON_NETWORK_SHORT,
            )

    @lunar_tick.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Moons(bot))
