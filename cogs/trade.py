from __future__ import annotations

import asyncio
import io
import logging
import math
import random
import re
import time

import aiohttp
import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.scale import to_raw, to_human
from core.framework.amounts import resolve_all_spend
from core.framework.ui import send_paginated, FormatKit
from core.framework.embed import card
from core.framework.heartbeat import pulse, register_interval
from core.framework.tx import set_tx
from core.framework.ai import complete as ai_complete, strip_links
from core.framework.cooldowns import user_cooldown
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import ensure_registered, guild_only, module_cog_check, no_bots
from core.framework import whale as _whale
from core.framework.ui import (
    C_AMBER, C_BUY, C_CHART_BG, C_ERROR, C_INFO, C_NEUTRAL, C_PURPLE, C_SELL, C_SUCCESS, C_TEAL, C_WARNING,
    ConfirmView, fmt_token, fmt_usd, fmt_pct, fmt_gas, fmt_ts, fmt_bonus,
    estimate_cefi_impact, slippage_banner,
)
from core.framework.fuzzy import suggest_subcommand
from cogs.shop import _liqstone_stat
from services.vault import deposit_to_vault, credit_vault_volume
from services.trade import check_trade_cooldown, set_trade_cooldown
from services.lp_yield import tick_lp_yield_for_guild
from constants.vaults import NETWORK_TO_VAULT as _VAULT_NET_MAP
from services.market_event_engine import get_active_event, get_phase_modifiers

try:
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

log = logging.getLogger(__name__)

# Active admin price events: (guild_id, symbol) -> {start_ts, end_ts, start_price,
# end_ts, pattern, magnitude_pct, seed}.  Written by ,admin pump (cogs/admin.py)
# and the auto-pump scheduler; read by ``_drift_guild`` every PRICE_TICK_SECONDS.
#
# Persisted to the ``admin_price_events`` table (migration 0210) so a deploy
# or container restart mid-pump rehydrates here on cog load instead of
# silently freezing the chart at whatever the last drift tick wrote.
_admin_price_events: dict[tuple[int, str], dict] = {}

# ══════════════════════════════════════════════════════════════════════════════
#  Constants & helpers  -  crypto.py
# ══════════════════════════════════════════════════════════════════════════════

def gbm_step(
    price: float,
    daily_vol: float,
    dt_seconds: float,
    start_price: float = 0.0,
    twap: float = 0.0,
    twap_stddev: float = 0.0,
    open_price: float = 0.0,
    ath: float = 0.0,
    vol_multiplier: float = 1.0,
    price_bias_pct_per_day: float = 0.0,
) -> float:
    """TWAP-anchored GBM step with Bollinger regime caps and daily circuit breaker.

    Institutional-grade oracle: uses rolling TWAP for continuous mean reversion
    and dynamic per-tick caps based on Bollinger band regime (how far price is
    from the TWAP in standard-deviation terms).  A daily hard limit prevents
    any token from drifting more than ±30% from its daily open.

    When ``ath`` is supplied and the current price is below
    ``Config.DEPEG_THRESHOLD × ath`` (depeg mode), the upward daily drift cap
    is tightened to ``Config.ORACLE_RECOVERY_CAP`` to slow runaway recoveries
    that would give cheap-buy players gamebreaking gains.

    ``vol_multiplier`` scales daily volatility (from market events).
    ``price_bias_pct_per_day`` adds a directional drift term (% per day, from
    market events  -  positive = upward bias, negative = downward).
    """
    if dt_seconds <= 0:
        return price
    effective_vol = daily_vol * max(0.0, vol_multiplier)
    mu = price_bias_pct_per_day / 100.0  # convert % → decimal daily drift

    # ── Recovery bias: automatic upward drift when below start_price ────────
    # Scales linearly with undervaluation: further below = stronger pull up.
    # At 50% below start_price, injects full ORACLE_RECOVERY_BIAS per day.
    # At 10% below, injects ~20% of the bias.  At or above start_price: zero.
    if start_price > 0 and price < start_price:
        underval = (start_price - price) / start_price  # 0..1
        mu += (Config.ORACLE_RECOVERY_BIAS / 100.0) * underval

    dt = dt_seconds / 86400.0
    z = random.gauss(0, 1)
    raw = max(1e-9, price * math.exp((mu - 0.5 * effective_vol ** 2) * dt + effective_vol * math.sqrt(dt) * z))

    # ── Continuous TWAP mean reversion ───────────────────────────────────────
    # When price is below start_price the TWAP is anchored at the depressed
    # level and actively prevents recovery.  Skip reversion entirely for
    # upward moves in that case so the recovery bias can actually work.
    if twap > 0:
        deviation_from_twap = (raw - twap) / twap
        _skip_reversion = (
            start_price > 0
            and raw < start_price
            and deviation_from_twap > 0
        )
        if not _skip_reversion:
            raw -= raw * Config.ORACLE_REVERSION_STRENGTH * deviation_from_twap

    # ── Bollinger regime-switching per-tick cap ──────────────────────────────
    if twap > 0 and twap_stddev > 0:
        bands = abs(price - twap) / twap_stddev
        if bands > 2:
            cap = Config.ORACLE_CAP_CONTAINMENT
        elif bands > 1:
            cap = Config.ORACLE_CAP_CAUTIOUS
        else:
            cap = Config.ORACLE_CAP_NORMAL
    else:
        cap = getattr(Config, "MAX_TICK_CHANGE", 0.03)
    raw = max(price * (1.0 - cap), min(price * (1.0 + cap), raw))

    # ── Daily circuit breaker (tightened during depeg recovery) ─────────────
    if open_price > 0:
        if ath > 0 and price < ath * Config.DEPEG_THRESHOLD:
            # Depeg mode: tighten the *upward* cap to ORACLE_RECOVERY_CAP so
            # recovery rallies cannot compound faster than intended.
            up_limit   = open_price * (1.0 + Config.ORACLE_RECOVERY_CAP)
            down_limit = open_price * (1.0 - Config.ORACLE_DAILY_MAX_DRIFT)
        else:
            up_limit   = open_price * (1.0 + Config.ORACLE_DAILY_MAX_DRIFT)
            down_limit = open_price * (1.0 - Config.ORACLE_DAILY_MAX_DRIFT)
        raw = max(down_limit, min(up_limit, raw))

    # ── Legacy soft mean reversion toward start_price (fallback) ─────────────
    if start_price > 0 and twap <= 0:
        ratio = raw / start_price
        if ratio > 3.0 or ratio < 0.33:
            raw = raw * 0.99 + start_price * 0.01

    if Config.LOG_LARGE_PRICE_MOVES:
        pct_change = abs(raw - price) / price if price > 0 else 0.0
        if pct_change >= 0.015:
            from core.framework.log import warn
            warn(f"[Oracle] {pct_change:.1%} price move: {price:.6f} → {raw:.6f}")

    return max(1e-9, raw)


def _minute_ts() -> int:
    return int(time.time()) // 60 * 60


# ── MM trade flavor ────────────────────────────────────────────────────────

_MM_PERSONAS: dict[str, dict] = {
    "MarketBot":  {
        "avatar": "https://robohash.org/MarketBot?set=set3&size=80x80",
        "system": "cold algorithmic quant, precise and data-driven",
    },
    "AlgoTrader": {
        "avatar": "https://robohash.org/AlgoTrader?set=set3&size=80x80",
        "system": "trendfollower, always riding momentum, degen AF",
    },
    "Sentinel-7": {
        "avatar": "https://robohash.org/Sentinel7?set=set3&size=80x80",
        "system": "risk manager, cautious but opportunistic",
    },
    "ArbEngine":  {
        "avatar": "https://robohash.org/ArbEngine?set=set3&size=80x80",
        "system": "pure arbitrageur, exploits every spread",
    },
    "DeepLiquid": {
        "avatar": "https://robohash.org/DeepLiquid?set=set3&size=80x80",
        "system": "liquidity provider, market-neutral, philosophical",
    },
}
_MM_NAMES = list(_MM_PERSONAS.keys())

_MM_BUY_FLAVORS = [
    "accumulated a position in",
    "added to long in",
    "opened a bid on",
    "swept the ask in",
]

_MM_SELL_FLAVORS = [
    "offloaded into",
    "trimmed exposure in",
    "hit the bid in",
    "unloaded bags in",
]

_DIRECT_BUY_TOKENS = frozenset(Config.TOKENS.keys())

_NETWORK_SHORT_MAP: dict[str, str] = {
    "Sun Network":      "sun",
    "Moneta Chain":  "mta",
    "Arcadia Network": "arc",
    "Discoin Network":  "dsc",
}


def _net_prefix(symbol: str, network_override: str = "") -> str:
    """Return the network shortcode for a token (used as tx hash prefix).
    network_override: full network name if available (e.g. from all_tokens lookup for custom tokens)."""
    if network_override:
        return _NETWORK_SHORT_MAP.get(network_override, "")
    t = Config.TOKENS.get(symbol, {})
    return _NETWORK_SHORT_MAP.get(t.get("network", ""), "")


def _parse_sym_amt(arg1: str, arg2: str) -> tuple[str, str]:
    """Accept both 'SYM amount' and 'amount SYM' argument orders.
    Returns (symbol_upper, amount_str). 'all' is treated as an amount.
    A '$' prefix on the amount (e.g. '$100') marks it as a USD amount."""
    if arg1.lower() == "all":
        return arg2.upper(), arg1
    if arg2.lower() == "all":
        return arg1.upper(), arg2
    # Check for $ prefix (USD amount mode)
    if arg1.startswith("$"):
        return arg2.upper(), arg1
    if arg2.startswith("$"):
        return arg1.upper(), arg2
    try:
        float(arg1)
        # arg1 is numeric → order is: amount SYM
        return arg2.upper(), arg1
    except ValueError:
        # arg1 is non-numeric → order is: SYM amount
        return arg1.upper(), arg2


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


# ══════════════════════════════════════════════════════════════════════════════
#  Chart helpers -- pulled in from core/framework/chart.py (single source of truth
#  per CLAUDE.md). The pure indicator math, layout flag parser, candle
#  aggregator, and render pipeline all live there so the simulated and
#  real-crypto charts call the same code.
# ══════════════════════════════════════════════════════════════════════════════

from core.framework.chart import (
    _TIMEFRAMES,
    _aggregate,
    parse_chart_args,
    build_chart_png,
    build_footer_chips,
)

_VALID_TOKENS = set(Config.TOKENS.keys()) | {"USD"}




def _parse_pair(pair: str, extra_tokens: frozenset[str] | set[str] = frozenset()) -> tuple[str, str] | None:
    """Parse a pair like 'CATUSD' or 'BTCETH' into its two token symbols.

    ``extra_tokens`` lets callers with per-guild context inject guild-registered
    group tokens (which aren't in :data:`_VALID_TOKENS` at module-load time) so
    pairs like ``CATUSD`` can be parsed. Without this, group tokens silently
    fail to resolve even though they have live candles keyed as ``{SYM}USD``.
    """
    pair = pair.upper()
    effective = _VALID_TOKENS | set(extra_tokens)
    # Bare symbol implies USD quote. Saves users from having to type the
    # `USD` suffix on every ,chart call.
    if pair in effective and pair != "USD":
        return pair, "USD"
    all_syms = sorted(effective, key=len, reverse=True)
    for sym in all_syms:
        if pair.startswith(sym) and pair[len(sym):] in effective:
            return sym, pair[len(sym):]
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Constants & helpers  -  pools.py
# ══════════════════════════════════════════════════════════════════════════════

from constants.trading import (
    DEFAULT_SWAP_FEE as _DEFAULT_SWAP_FEE,
    SWAP_PLATFORM_FEE_PCT as _SWAP_PLATFORM_FEE_PCT,
    SLIPPAGE_WARN as _SLIPPAGE_WARN,
)
_TREASURY_SEED      = Config.POOL_SEED_STABLECOIN

# Tokens treated as $1 stable quote for ARB math  -  same logic as USD
_STABLE_QUOTE = frozenset({"USD", "USDC", "DSD"})  # stablecoins treated as $1 in arb math

# Canonical source lives in :mod:`core.framework.network`; keep this alias so
# existing callers in this file don't need to change.
from core.framework.network import FULL_TO_SHORT as _NET_SHORT


def _resolve_token_wallet_net(token_sym: str, current_full_net: str) -> str:
    """Return the short network code where this token's wallet_holdings row lives.

    For built-in tokens we always trust ``Config.TOKENS[sym]['network']``
    over the in-flight ``current_full_net`` lookup. The in-flight lookup
    reads from a merged guild-token dict that has occasionally been
    observed to come back without a network field on a token (a stale /
    partial cache or a malformed guild_tokens row). When that happens on
    a built-in token like MOON, the swap path used to fall through to the
    swap-network short, which routed the wallet debit to a completely
    different network's wallet_holdings -- the user's MOON would still be
    sitting on Moon Network but the swap would query
    ``wallet_holdings(network='arc', symbol='MOON')`` and see zero.

    Anchoring built-in tokens to ``Config.TOKENS`` first eliminates that
    failure mode. Player-deployed tokens fall back to ``current_full_net``
    since ``Config.TOKENS`` doesn't know about them.
    """
    builtin_net = Config.TOKENS.get(token_sym, {}).get("network", "")
    if builtin_net:
        s = _NET_SHORT.get(builtin_net, "")
        if s:
            return s
    return _NET_SHORT.get(current_full_net, "")


# ── Anti-drain state (shared with API via services.swap) ─────────────────
from services.swap import (
    check_user_swap_volume as _check_user_swap_volume,
    record_user_swap_volume as _record_user_swap_volume,
    reserve_depeg_buy as _reserve_depeg_buy,
    cancel_depeg_reservation as _cancel_depeg_reservation,
    liqstone_swap_fee_discount as _liqstone_swap_fee_discount,
    apply_liqstone_discount as _apply_liqstone_discount,
    chimerastone_swap_fee_discount as _chimerastone_swap_fee_discount,
    is_depeg as _is_depeg,
    reserve_pool_swap as _reserve_pool_swap,
    cancel_pool_swap_reservation as _cancel_pool_swap_reservation,
    is_moon_swappable_pair as _is_moon_swappable_pair,
    is_bud_swappable_pair as _is_bud_swappable_pair,
    apply_swap_oracle_nudge as _swap_oracle_nudge,
)
_user_large_lp_removal: dict[tuple[int, int, str], float] = {}


def _token_label(symbol: str) -> str:
    return Config.currency_label(symbol)


def _solve_arb_quadratic(a_c: float, b_c: float, c_c: float) -> float | None:
    """Solve a_c*x² + b_c*x + c_c = 0 for the positive root. Returns None if no real solution."""
    disc = b_c * b_c - 4.0 * a_c * c_c
    if disc < 0 or a_c == 0:
        return None
    x = (-b_c + math.sqrt(disc)) / (2.0 * a_c)
    return x if x > 0 else None


# ══════════════════════════════════════════════════════════════════════════════
#  Trade Cog
# ══════════════════════════════════════════════════════════════════════════════

class Trade(commands.Cog):

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        # last close price per (guild_id, symbol) for candle open tracking
        self._last_price: dict[tuple[int, str], float] = {}
        # Per-pool cooldown tracker: pool_id → unix timestamp of last oracle rebalance
        self._last_rebalance_ts: dict[str, float] = {}
        # Per-guild lock: prevents concurrent _on_prices_updated calls for the same
        # guild (drift_task and MM loop can both publish prices_updated in the same
        # second  -  without this the cooldown check/write spans many awaits and races)
        self._rebalance_locks: dict[int, asyncio.Lock] = {}

        self.drift_task.start()
        self.daily_reset_task.start()
        self.lp_yield_task.start()
        register_interval("price_drift_trade", Config.PRICE_TICK_SECONDS)
        register_interval("lp_yield", int(Config.LP_YIELD_TICK_HOURS * 3600))
        # MM trade loop is started after bot is ready (random interval needs asyncio)
        self._mm_task: asyncio.Task | None = None

        # Subscribe to prices_updated for pool oracle rebalancer
        bot.bus.subscribe("prices_updated", self._on_prices_updated)

    async def cog_check(self, ctx) -> bool:
        return await module_cog_check(self.bot, ctx, "crypto")

    def cog_unload(self) -> None:
        self.drift_task.cancel()
        self.daily_reset_task.cancel()
        self.lp_yield_task.cancel()
        if self._mm_task:
            self._mm_task.cancel()
        self.bot.bus.unsubscribe("prices_updated", self._on_prices_updated)

    # ══════════════════════════════════════════════════════════════════════════
    #  LP-derived USD price (from crypto.py)
    # ══════════════════════════════════════════════════════════════════════════

    async def _derive_usd_price(self, symbol: str, guild_id: int) -> float | None:
        """For sub-tokens: chain through a TOKEN/SUN pool to get USD price."""
        for bridge in ("SUN", "USD"):
            pool_id, ca, cb = self.bot.db.make_pool_id(symbol, bridge)
            pool = await self.bot.db.get_pool(pool_id, guild_id)
            if not pool or pool["reserve_a"] <= 0 or pool["reserve_b"] <= 0:
                continue
            ratio = pool["reserve_b"] / pool["reserve_a"] if ca == symbol else pool["reserve_a"] / pool["reserve_b"]
            if bridge == "USD":
                return ratio
            bridge_row = await self.bot.db.get_price(bridge, guild_id)
            if bridge_row:
                return ratio * float(bridge_row["price"])
        return None

    # ══════════════════════════════════════════════════════════════════════════
    #  Background tasks  -  drift, daily reset, MM trades (from crypto.py)
    # ══════════════════════════════════════════════════════════════════════════

    @tasks.loop(seconds=Config.PRICE_TICK_SECONDS)
    async def drift_task(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self._drift_guild(guild)
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "[trade.drift_task] Error in guild %s (%s): %s",
                    guild.id, type(e).__name__, e, exc_info=True,
                )
        pulse("price_drift_trade")

    async def _drift_guild(self, guild) -> None:
        prices = await self.bot.db.get_all_prices(guild.id)
        if not prices:
            await self.bot.db.seed_prices(guild.id)
            prices = await self.bot.db.get_all_prices(guild.id)
        # Seed any custom guild tokens not yet in crypto_prices
        custom_tokens = await self.bot.db.get_guild_tokens(guild.id)
        for t in custom_tokens:
            p = t["start_price"]
            await self.bot.db.execute(
                "INSERT INTO crypto_prices "
                "(symbol, guild_id, price, open_price, day_high, day_low) VALUES ($1,$2,$3,$4,$5,$6) "
                "ON CONFLICT DO NOTHING",
                t["symbol"], guild.id, p, p, p, p,
            )
        if custom_tokens:
            prices = await self.bot.db.get_all_prices(guild.id)
        # Build merged token map for daily_vol lookup
        all_tokens = await self.bot.db.get_all_tokens_for_guild(guild.id)

        # Fetch market-event modifiers once per guild per tick
        _redis = getattr(getattr(self.bot, "bus", None), "_redis", None)
        _ae = await get_active_event(_redis, guild.id)
        _mods = get_phase_modifiers(_ae)
        _vol_mult  = _mods["vol_multiplier"]
        _price_bias = _mods["price_bias_pct_per_day"]

        # Moon event: per-token bias overrides and relaxed caps
        # Regular tokens → 30% daily drift; network coins → 80% daily drift.
        # Daily circuit breaker is disabled for non-stablecoins so 80%+ moves
        # are not capped at the normal ±20%/day limit.
        _is_moon = _ae is not None and _ae.event_id == "moon"

        # Fix 3: batch-fetch all TWAPs once per tick rather than N sequential queries
        all_twaps = await self.bot.db.get_all_twaps(guild.id, Config.ORACLE_TWAP_WINDOW)

        ts = _minute_ts()
        for row in prices:
            symbol = row["symbol"]
            tcfg     = all_tokens.get(symbol, {})
            daily_vol   = tcfg.get("daily_vol") or 0.04
            start_price = tcfg.get("start_price") or 0.0

            usd_sym   = f"{symbol}USD"
            ts_key    = (guild.id, f"_ts_{symbol}")
            price_key = (guild.id, usd_sym)

            # At the turn of a new minute, capture the current oracle as the
            # candle open BEFORE the GBM step so open == previous close.
            if ts != self._last_price.get(ts_key, 0):
                self._last_price[ts_key]    = ts
                self._last_price[price_key] = float(row["price"])

            open_price = self._last_price.get(price_key, float(row["price"]))

            # Admin price event: bypass GBM entirely for deterministic pump/dump moves
            _pump_ev = _admin_price_events.get((guild.id, symbol))
            if _pump_ev:
                from services.chart_patterns import compute_price as _pat_price
                _now = time.time()
                _start_ts = _pump_ev["start_ts"]
                _end_ts   = _pump_ev["end_ts"]
                _pct_done = 1.0 if _now >= _end_ts else (_now - _start_ts) / max(1e-9, _end_ts - _start_ts)
                new_price = _pat_price(
                    _pump_ev["pattern"], _pct_done,
                    _pump_ev["magnitude_pct"], _pump_ev["seed"],
                    _pump_ev["start_price"],
                )
                if _now >= _end_ts:
                    del _admin_price_events[(guild.id, symbol)]
                    try:
                        await self.bot.db.delete_admin_price_event(guild.id, symbol)
                    except Exception:
                        log.exception(
                            "drift: failed to clear persisted pump event "
                            "gid=%s sym=%s", guild.id, symbol,
                        )
                await self.bot.db.update_price(symbol, guild.id, new_price)
                await self.bot.db.execute(
                    "UPDATE crypto_prices SET open_price=$1 WHERE symbol=$2 AND guild_id=$3",
                    new_price, symbol, guild.id,
                )
                self._last_price[price_key] = new_price
                await self.bot.db.upsert_candle(
                    guild.id, usd_sym, ts,
                    open_=open_price, high=max(open_price, new_price),
                    low=min(open_price, new_price), close=new_price, volume_delta=0.0,
                )
                continue

            # Per-token moon overrides
            _is_stablecoin = bool(tcfg.get("stablecoin"))
            _is_network_coin = (
                not _is_stablecoin
                and tcfg.get("network") in _NETWORK_SHORT_MAP
            )
            if _is_moon and not _is_stablecoin:
                _token_bias = 40.0 if _is_network_coin else 15.0
                _token_vol  = max(_vol_mult, 1.5)
                # Disable daily circuit breaker  -  moon moves are intentional
                _open_price = 0.0
            else:
                _token_bias = _price_bias
                _token_vol  = _vol_mult
                _open_price = row.get("open_price", 0.0)

            twap, twap_stddev = all_twaps.get(usd_sym, (0.0, 0.0))
            new_price = gbm_step(
                float(row["price"]), daily_vol, Config.PRICE_TICK_SECONDS,
                start_price=start_price,
                twap=twap, twap_stddev=twap_stddev,
                open_price=_open_price,
                ath=float(row.get("ath") or 0.0),
                vol_multiplier=_token_vol,
                price_bias_pct_per_day=_token_bias,
            )

            # Wrapped-coin peg: MMTA / MSUN clamp to their underlying's
            # live oracle price within peg_band each tick. Lets the wrapper
            # drift freely inside the band (so AMM pool imbalances can move
            # the chart a few percent) but snaps it back at the edges so it
            # can never decouple from MTA / SUN. The anchor is the oracle,
            # not a hard peg -- real DeFi wrappers do the same via external
            # redemption backstops.
            _peg_to = tcfg.get("peg_to")
            if _peg_to:
                _anchor_price = 0.0
                for _r in prices:
                    if _r["symbol"] == _peg_to:
                        _anchor_price = float(_r["price"])
                        break
                if _anchor_price > 0:
                    _band = float(tcfg.get("peg_band") or 0.02)
                    _lo = _anchor_price * (1.0 - _band)
                    _hi = _anchor_price * (1.0 + _band)
                    if new_price > _hi:
                        new_price = _hi
                    elif new_price < _lo:
                        new_price = _lo

            await self.bot.db.update_price(symbol, guild.id, new_price)

            # Upsert 1-min candle.  high/low always span both open and close
            # so the candle is geometrically valid (no filled rectangles).
            await self.bot.db.upsert_candle(
                guild.id, usd_sym, ts,
                open_=open_price,
                high=max(open_price, new_price),
                low=min(open_price, new_price),
                close=new_price,
                volume_delta=0.0,
            )

        await self.bot.bus.publish("prices_updated", guild=guild)

    @drift_task.before_loop
    async def before_drift(self) -> None:
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self.bot.db.seed_prices(guild.id)
            await self.bot.db.seed_pools(guild.id)
            await self._seed_house_account(guild.id)
        # Rehydrate persisted admin pump events so a deploy mid-pump
        # resumes the curve where it left off instead of freezing the
        # chart. Events whose end_ts has already passed are dropped.
        try:
            now = time.time()
            persisted = await self.bot.db.load_admin_price_events()
            stale: list[tuple[int, str]] = []
            for r in persisted:
                gid = int(r["guild_id"])
                sym = r["symbol"]
                end_ts = float(r["end_ts"])
                if end_ts <= now:
                    stale.append((gid, sym))
                    continue
                _admin_price_events[(gid, sym)] = {
                    "start_ts":      float(r["start_ts"]),
                    "end_ts":        end_ts,
                    "start_price":   float(r["start_price"]),
                    "pattern":       r["pattern"],
                    "magnitude_pct": float(r["magnitude_pct"]),
                    "seed":          int(r["seed"]),
                }
            for gid, sym in stale:
                await self.bot.db.delete_admin_price_event(gid, sym)
            if _admin_price_events:
                log.info(
                    "drift: rehydrated %d admin pump event(s) from DB",
                    len(_admin_price_events),
                )
        except Exception:
            log.exception("drift: failed to rehydrate admin pump events")
        # Start MM loop after bot is ready
        self._mm_task = asyncio.create_task(self._mm_trade_loop())

    # ── Daily reset task (UTC midnight) ───────────────────────────────────

    @tasks.loop(hours=24)
    async def daily_reset_task(self) -> None:
        """Reset open_price, day_high, day_low to current price at UTC midnight."""
        for guild in self.bot.guilds:
            await self.bot.db.reset_daily_prices(guild.id)
        from core.framework.log import ok
        ok("[Oracle] Daily price stats reset (open/high/low)")

    @daily_reset_task.before_loop
    async def before_daily_reset(self) -> None:
        await self.bot.wait_until_ready()
        # Sleep until next UTC midnight
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        midnight = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((midnight - now).total_seconds())

    # ── LP yield distribution (hourly) ────────────────────────────────────

    @tasks.loop(hours=Config.LP_YIELD_TICK_HOURS)
    async def lp_yield_task(self) -> None:
        """Pay hourly LP yield to every active LP position across every guild.

        See ``services/lp_yield.py`` for the per-position math. Skipped guilds
        and per-row failures are logged but do not abort the tick -- one
        misconfigured pool should never block payouts to everyone else.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)
        for guild in self.bot.guilds:
            try:
                if not await self.bot.db.module_enabled(guild.id, "crypto"):
                    continue
                result = await tick_lp_yield_for_guild(self.bot.db, guild.id)
                if result.user_payouts or result.group_payouts:
                    _log.info(
                        "[lp_yield] gid=%s users=%d groups=%d total=$%.2f (user $%.2f + group $%.2f)",
                        guild.id, result.user_payouts, result.group_payouts,
                        result.total_user_usd + result.total_group_usd,
                        result.total_user_usd, result.total_group_usd,
                    )
            except Exception:
                _log.exception("[lp_yield] tick failed for guild %s", guild.id)
        pulse("lp_yield")

    @lp_yield_task.before_loop
    async def before_lp_yield(self) -> None:
        await self.bot.wait_until_ready()

    async def _seed_house_account(self, guild_id: int) -> None:
        """Ensure the bot itself has a user account with a large starting balance."""
        await self.bot.db.ensure_user(self.bot.user.id, guild_id)
        house = await self.bot.db.get_user(self.bot.user.id, guild_id)
        if house and abs(house["wallet"] - Config.STARTING_BALANCE) < 1.0:
            await self.bot.db.execute(
                "UPDATE users SET wallet=1000000.0 WHERE user_id=$1 AND guild_id=$2",
                self.bot.user.id, guild_id,
            )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Seed prices (and other tables) when the bot joins a new guild mid-run."""
        await self.bot.db.seed_prices(guild.id)
        await self.bot.db.seed_pools(guild.id)
        await self._seed_house_account(guild.id)

    # ── MM trade loop (random 120 - 300s interval) ───────────────────────────────

    async def _mm_trade_loop(self) -> None:
        """Background loop that fires synthetic MM trades at random intervals."""
        await self.bot.wait_until_ready()
        _tick = 0
        while not self.bot.is_closed():
            interval = random.uniform(*Config.MM_TRADE_INTERVAL)
            await asyncio.sleep(interval)
            _tick += 1
            _guilds = list(self.bot.guilds)
            # Fix 4: run all guilds concurrently  -  IO-bound so gather is safe
            _mm_results = await asyncio.gather(
                *(self._mm_guild_tick(g, _tick) for g in _guilds),
                return_exceptions=True,
            )
            for _g, _r in zip(_guilds, _mm_results):
                if isinstance(_r, Exception):
                    log.error(
                        "[MM] guild %s tick %s failed: %s", _g.id, _tick, _r
                    )
            # Notify pool-arb listener after all MM trades settle
            await asyncio.gather(
                *(self.bot.bus.publish("prices_updated", guild=g) for g in _guilds),
                return_exceptions=True,
            )

    async def _mm_guild_tick(self, guild: discord.Guild, tick: int) -> None:
        """Run one MM trade tick for a single guild."""
        ai_flags = await self.bot.db.get_ai_flags(guild.id)
        personas = await self.bot.db.get_active_mm_personas(guild.id)
        if personas:
            persona = random.choice(personas)
        else:
            _name = random.choice(_MM_NAMES)
            p = _MM_PERSONAS[_name]
            persona = {"name": _name, "system_prompt": p["system"],
                       "avatar_url": p["avatar"], "trade_bias": "neutral", "emoji": "🤖"}
        # Fix 5: fetch prices once here and pass them down  -  avoids a second
        # get_all_prices call inside _do_mm_trade
        prices = await self.bot.db.get_all_prices(guild.id)
        ai_decision = None
        if ai_flags["mm"] and Config.OPENROUTER_API_KEY and prices:
            ai_decision = await self._ai_mm_decision(persona, prices)
        await self._do_mm_trade(guild, persona=persona, ai_decision=ai_decision, prices=prices)
        if tick % 10 == 0 and ai_flags["commentary"] and Config.OPENROUTER_API_KEY:
            await self._post_ai_commentary(guild)

    async def _do_mm_trade(
        self, guild: discord.Guild,
        persona: dict | None = None,
        ai_decision: dict | None = None,
        prices: list[dict] | None = None,
    ) -> None:
        if prices is None:
            prices = await self.bot.db.get_all_prices(guild.id)
        if not prices:
            return

        bot_uid = self.bot.user.id
        if persona is None:
            _name = random.choice(_MM_NAMES)
            p = _MM_PERSONAS[_name]
            persona = {"name": _name, "system_prompt": p["system"],
                       "avatar_url": p["avatar"], "trade_bias": "neutral", "emoji": "🤖"}
        bot_name = persona["name"]

        if ai_decision:
            _sym_raw = ai_decision.get("symbol") or random.choice(prices)["symbol"]
            symbol    = str(_sym_raw).upper()
            direction = 1 if ai_decision.get("direction", 1) > 0 else -1
            nudge_pct = float(ai_decision.get("nudge_pct", random.uniform(0.001, 0.005)))
            mm_volume = float(ai_decision.get("volume", random.uniform(50, 500)))
            verb      = ai_decision.get("quip") or random.choice(_MM_BUY_FLAVORS if direction > 0 else _MM_SELL_FLAVORS)
            nudge_pct = max(0.0005, min(0.01, nudge_pct))
            mm_volume = max(10.0, min(2000.0, mm_volume))
        else:
            row = random.choice(prices)
            symbol = row["symbol"]
            bias = persona.get("trade_bias", "neutral")
            if bias == "bull":
                direction = 1 if random.random() < 0.70 else -1
            elif bias == "bear":
                direction = -1 if random.random() < 0.70 else 1
            elif bias == "random":
                direction = random.choice([-1, 1])
                nudge_pct = random.uniform(0.002, 0.012)
                mm_volume = round(random.uniform(20, 800), 2)
                verb = random.choice(_MM_BUY_FLAVORS if direction > 0 else _MM_SELL_FLAVORS)
            else:  # neutral
                direction = random.choice([-1, 1])
            if bias != "random":
                nudge_pct = random.uniform(0.001, 0.005)
                mm_volume = round(random.uniform(50, 500), 2)
                verb = random.choice(_MM_BUY_FLAVORS if direction > 0 else _MM_SELL_FLAVORS)

        # Find the price row for chosen symbol
        price_row = next((p for p in prices if p["symbol"] == symbol), random.choice(prices))
        all_tokens = await self.bot.db.get_all_tokens_for_guild(guild.id)
        symbol = price_row["symbol"]

        # Re-fetch the live price right before writing so a user trade that
        # committed between the prices snapshot and this nudge isn't overwritten
        # with a stale-based value (e.g. whale buy takes MTA to $687, MM nudges
        # from the snapshot's $56 back to $56.17 and erases the impact).
        fresh_row = await self.bot.db.get_price(symbol, guild.id)
        old_price = float(fresh_row["price"]) if fresh_row else float(price_row["price"])
        new_price = max(0.001, old_price * (1 + direction * nudge_pct))
        mm_volume = round(mm_volume, 2)

        await self.bot.db.update_price(symbol, guild.id, new_price)

        # Track house account balance (raw SQL to bypass balance guard)
        mm_qty = mm_volume / new_price
        if direction > 0:  # MM buys: house spends USD, gains token
            await self.bot.db.execute(
                "UPDATE users SET wallet=GREATEST(0, wallet-$1) WHERE user_id=$2 AND guild_id=$3",
                to_raw(mm_volume), bot_uid, guild.id,
            )
            await self.bot.db.update_holding(bot_uid, guild.id, symbol, to_raw(mm_qty))
        else:  # MM sells: house gains USD, loses token
            await self.bot.db.execute(
                "UPDATE users SET wallet=wallet+$1 WHERE user_id=$2 AND guild_id=$3",
                to_raw(mm_volume), bot_uid, guild.id,
            )
            await self.bot.db.execute(
                "UPDATE crypto_holdings SET amount=GREATEST(0, amount-$1) WHERE user_id=$2 AND guild_id=$3 AND symbol=$4",
                to_raw(mm_qty), bot_uid, guild.id, symbol,
            )

        # Log MM trade attributed to the house account
        tx_type = "MM_BUY" if direction > 0 else "MM_SELL"
        tx_hash = await self.bot.db.log_tx(
            guild.id, bot_uid, tx_type,
            symbol_in="USD", amount_in=to_raw(mm_volume),
            symbol_out=symbol, amount_out=to_raw(mm_qty),
            price_at=new_price,
        )
        await self.bot.db.add_trade_volume(guild.id, f"{symbol}USD", to_raw(mm_volume))

        pct_change = (new_price - old_price) / old_price * 100
        token_meta = all_tokens.get(symbol, Config.TOKENS.get(symbol, {}))
        emoji = token_meta.get("emoji", "")
        trade_line = (
            f"{emoji}**{symbol}**  "
            f"${old_price:.4f} → ${new_price:.4f} ({pct_change:+.2f}%)  "
            f"|  vol **${mm_volume:,.0f}**  |  {verb}  |  `tx:{tx_hash}`"
        )

        # Post via Discord webhook if configured, else fall back to EventBus
        webhook_row = await self.bot.db.get_mm_webhook(guild.id)
        if webhook_row:
            wh_url = f"https://discord.com/api/webhooks/{webhook_row['webhook_id']}/{webhook_row['webhook_token']}"
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(wh_url, json={
                        "username": bot_name,
                        "avatar_url": persona.get("avatar_url") or persona.get("avatar", ""),
                        "content": trade_line,
                    }) as resp:
                        if resp.status < 200 or resp.status >= 300:
                            from core.framework import log
                            log.warn(f"MM webhook post failed (status {resp.status}) for guild {guild.id}")
                            raise RuntimeError("webhook failed")
            except Exception as _wh_exc:
                log.debug(
                    "[MM] webhook delivery failed for guild %s: %s", guild.id, _wh_exc
                )
                # Webhook failed; fall back to EventBus
                await self.bot.bus.publish(
                    "mm_trade", guild=guild, symbol=symbol, direction=direction,
                    old_price=old_price, new_price=new_price, pct_change=pct_change,
                    volume=mm_volume, tx_hash=tx_hash, bot_name=bot_name, verb=verb,
                )
        else:
            await self.bot.bus.publish(
                "mm_trade", guild=guild, symbol=symbol, direction=direction,
                old_price=old_price, new_price=new_price, pct_change=pct_change,
                volume=mm_volume, tx_hash=tx_hash, bot_name=bot_name, verb=verb,
            )

    # ── AI helpers ────────────────────────────────────────────────────────────

    async def _ai_mm_decision(
        self, persona: dict, prices: list[dict]
    ) -> dict | None:
        """Ask OpenRouter to analyze the market and return a trading decision dict."""
        import json
        persona_name = persona["name"]
        persona_desc = persona.get("system_prompt", "")
        summary = "\n".join(
            f"{r['symbol']}: ${r['price']:.4f} (24h {((r['price'] - r['open_price']) / r['open_price'] * 100):+.1f}%)"
            for r in prices
            if r.get("open_price", 0) > 0
        )
        result = await ai_complete(
            [
                {
                    "role": "system",
                    "content": (
                        f"You are {persona_name}. {persona_desc} "
                        "You are a market maker in a Discord economy game. "
                        "Reply ONLY with valid JSON (no markdown, no explanation): "
                        '{"symbol":"SUN","direction":1,"nudge_pct":0.003,"volume":200,'
                        '"quip":"short degen comment max 10 words"}'
                        " direction=1 means buy, -1 means sell. nudge_pct 0.001-0.008."
                    ),
                },
                {"role": "user", "content": f"Market:\n{summary}\n\nDecide your next trade."},
            ],
            max_tokens=80,
            temperature=1.0,
        )
        if not result:
            return None
        try:
            # Extract the first/last JSON object in the string robustly
            start = result.find("{")
            end = result.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(result[start:end+1])
        except Exception:
            pass
        return None

    async def _post_ai_commentary(self, guild: discord.Guild) -> None:
        """Post an AI-generated market summary via the MM webhook or trade channel."""
        prices = await self.bot.db.get_all_prices(guild.id)
        if not prices:
            return
        summary = "\n".join(
            f"{r['symbol']}: ${r['price']:.4f} (24h {((r['price'] - r['open_price']) / r['open_price'] * 100):+.1f}%)"
            for r in prices
            if r.get("open_price", 0) > 0
        )
        # Use per-guild commentary prompt override if set
        ai_prompts = await self.bot.db.get_ai_prompts(guild.id)
        commentary_prompt = (
            ai_prompts.get("commentary")
            or "You are a dry, terse market analyst. Summarize the market movement in one sentence using the real numbers provided. No hype. No slang. Just facts."
        )
        text = await ai_complete(
            [
                {"role": "system", "content": commentary_prompt},
                {"role": "user", "content": f"Current market:\n{summary}"},
            ],
            max_tokens=100,
            temperature=0.7,
        )
        if not text:
            return
        text = strip_links(text)
        # Use first active persona for commentary, fall back to MarketBot
        personas = await self.bot.db.get_active_mm_personas(guild.id)
        comm_persona = personas[0] if personas else {
            "name": "MarketBot",
            "avatar_url": _MM_PERSONAS["MarketBot"]["avatar"],
        }
        webhook_row = await self.bot.db.get_mm_webhook(guild.id)
        if webhook_row:
            wh_url = f"https://discord.com/api/webhooks/{webhook_row['webhook_id']}/{webhook_row['webhook_token']}"
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(wh_url, json={
                        "username": comm_persona["name"],
                        "avatar_url": comm_persona.get("avatar_url", ""),
                        "content": f"📊 **Market Update**  -  {text}",
                    }) as resp:
                        if resp.status < 200 or resp.status >= 300:
                            from core.framework import log
                            log.warn(f"AI commentary webhook failed (status {resp.status}) for guild {guild.id}")
                            raise RuntimeError("webhook failed")
                return
            except Exception:
                pass
        # Fallback: post via EventBus to trade channel
        settings = await self.bot.db.get_guild_settings(guild.id)
        if settings and settings.get("trade_channel"):
            ch = guild.get_channel(settings["trade_channel"])
            if ch:
                await ch.send(f"📊 **MarketBot**  -  {text}")

    # ══════════════════════════════════════════════════════════════════════════
    #  Oracle rebalancer (from pools.py)
    # ══════════════════════════════════════════════════════════════════════════

    async def _on_prices_updated(self, guild: discord.Guild) -> None:
        """Rebalance TOKEN/USD pools when oracle and pool price diverge beyond threshold.
        Uses the constant-product quadratic to find the exact swap needed to align
        pool price with oracle, then executes it through the swap formula so k grows
        (fee accumulates in pool = real yield for LPs).
        Rate-limited to Config.POOL_ARB_COOLDOWN seconds per pool."""
        # Per-guild lock: drift_task and MM loop both publish prices_updated and can
        # fire this handler concurrently for the same guild.  Without the lock the
        # cooldown read→check→write spans several awaits and both invocations can pass
        # the cooldown guard and double-rebalance the same pool.
        lock = self._rebalance_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            await self._rebalance_pools_for_guild(guild)

    async def _rebalance_pools_for_guild(self, guild: discord.Guild) -> None:
        """Inner rebalance logic  -  always called under the per-guild lock."""
        pools = await self.bot.db.get_all_pools(guild.id)
        ts = int(time.time()) // 60 * 60

        now = time.time()
        for pool in pools:
            ta, tb = pool["token_a"], pool["token_b"]

            # ── True crypto/crypto: emit cross-pair candle, skip oracle rebalance ─
            # (Stablecoins USDC/DSD are treated as $1 quote for rebalance math)
            if ta not in _STABLE_QUOTE and tb not in _STABLE_QUOTE:
                if pool["reserve_a"] > 0:
                    price = pool["reserve_b"] / pool["reserve_a"]
                    sym = f"{ta}{tb}"
                    await self.bot.db.upsert_candle(guild.id, sym, ts, open_=price, high=price, low=price, close=price)
                continue

            # Per-pool cooldown: skip if rebalanced too recently
            pool_key = f"{guild.id}:{pool['pool_id']}"
            last_rb = self._last_rebalance_ts.get(pool_key, 0.0)
            if now - last_rb < Config.POOL_ARB_COOLDOWN:
                continue

            ra = pool["reserve_a"]
            rb = pool["reserve_b"]
            if ra <= 0 or rb <= 0:
                continue

            # Oracle rebalances run at zero fee so k stays constant.
            # Charging a fee here lets players profit by manipulating oracle prices
            # to trigger repeated rebalances (pump → rebalance fee → dump → fee again).
            # LP yield comes from user-initiated swaps only.
            fee = 0.0
            f   = 1.0
            k   = ra * rb

            if tb in _STABLE_QUOTE:
                # TOKEN is ta, stablecoin/USD is tb  -  pool_price = stable per TOKEN ≈ USD per TOKEN
                token_sym  = ta
                if token_sym == "USD":
                    oracle = 1.0  # USD is always $1  -  rebalances stablecoin/USD pools
                else:
                    oracle_row = await self.bot.db.get_price(token_sym, guild.id)
                    if not oracle_row:
                        continue
                    oracle = float(oracle_row["price"])
                if oracle <= 0:
                    continue
                pool_price = rb / ra

                deviation = abs(pool_price - oracle) / oracle
                if deviation < Config.POOL_ARB_THRESHOLD:
                    continue

                if pool_price > oracle:
                    P = oracle
                    dx = _solve_arb_quadratic(P * f, P * ra * (1.0 + f), P * ra * ra - k)
                    if not dx:
                        continue
                    out_b = rb * dx * f / (ra + dx * f)
                    new_a, new_b = ra + dx, rb - out_b
                else:
                    P = oracle
                    dy = _solve_arb_quadratic(f, rb * (1.0 + f), rb * rb - P * k)
                    if not dy:
                        continue
                    out_a = ra * dy * f / (rb + dy * f)
                    new_a, new_b = ra - out_a, rb + dy

                old_price = pool_price
                new_price = new_b / new_a if new_a > 0 else oracle

            elif ta in _STABLE_QUOTE:
                # TOKEN is tb, stablecoin/USD is ta  -  pool_price = stable per TOKEN ≈ USD per TOKEN
                token_sym  = tb
                if token_sym == "USD":
                    oracle = 1.0  # USD is always $1
                else:
                    oracle_row = await self.bot.db.get_price(token_sym, guild.id)
                    if not oracle_row:
                        continue
                    oracle = float(oracle_row["price"])
                if oracle <= 0:
                    continue
                pool_price = ra / rb

                deviation = abs(pool_price - oracle) / oracle
                if deviation < Config.POOL_ARB_THRESHOLD:
                    continue

                if pool_price > oracle:
                    P = oracle
                    dx = _solve_arb_quadratic(P * f, P * rb * (1.0 + f), P * rb * rb - k)
                    if not dx:
                        continue
                    out_a = ra * dx * f / (rb + dx * f)
                    new_a, new_b = ra - out_a, rb + dx
                else:
                    P = oracle
                    dy = _solve_arb_quadratic(f, ra * (1.0 + f), ra * ra - P * k)
                    if not dy:
                        continue
                    out_b = rb * dy * f / (ra + dy * f)
                    new_a, new_b = ra + dy, rb - out_b

                old_price = pool_price
                new_price = new_a / new_b if new_b > 0 else oracle
            else:
                continue

            pct_change = (new_price - old_price) / old_price * 100
            # Defensive: a pool with tiny-priced tokens and deep reserves can
            # produce intermediate values past the NUMERIC(36,0) ceiling
            # (10^36 raw units) when the arb quadratic blows up. Skip the
            # pool on overflow rather than letting the bus listener crash
            # the whole rebalance pass.
            try:
                _new_a_int = int(new_a)
                _new_b_int = int(new_b)
                if abs(_new_a_int) >= 10**36 or abs(_new_b_int) >= 10**36:
                    log.warning(
                        "trade.rebalance: skipping pool=%s (overflow guard); "
                        "new_a=%s new_b=%s",
                        pool["pool_id"], new_a, new_b,
                    )
                    continue
                await self.bot.db.update_pool_reserves(
                    pool["pool_id"], guild.id, _new_a_int, _new_b_int,
                    pool["total_lp"],
                )
            except Exception as exc:
                log.warning(
                    "trade.rebalance: pool=%s update failed: %s: %s",
                    pool["pool_id"], type(exc).__name__, exc,
                )
                continue

            # Update cooldown timestamp for this pool
            self._last_rebalance_ts[pool_key] = now

            # Reset LP snapshots  -  reserves changed, fee tracking restarts from now
            if pool["total_lp"] > 0:
                positions = await self.bot.db.get_pool_lp_positions(pool["pool_id"], guild.id)
                rpa = new_a / pool["total_lp"]  # raw/raw = human ratio
                rpb = new_b / pool["total_lp"]
                for pos in positions:
                    if pos.get("lp_shares", 0) > 0:
                        await self.bot.db.upsert_lp_snapshot(
                            pos["user_id"], guild.id, pool["pool_id"], to_raw(rpa), to_raw(rpb)
                        )

            # Determine network from the non-stablecoin/non-USD token in the pair
            token_sym = ta if tb in _STABLE_QUOTE else tb
            rebalance_net = _NET_SHORT.get(
                Config.TOKENS.get(token_sym, {}).get("network", ""), ""
            )
            rebalance_tx = await self.bot.db.log_tx(
                guild.id, None, "ORACLE_REBALANCE",
                symbol_in=ta, amount_in=None,
                symbol_out=tb, amount_out=None,
                price_at=new_price,
                network=rebalance_net,
            )
            # Only post to feed when price change is significant (>2%)
            if abs(pct_change) > 2.0:
                await self.bot.bus.publish(
                    "oracle_rebalance",
                    guild=guild,
                    pool_id=pool["pool_id"],
                    token_a=ta,
                    old_price=old_price,
                    new_price=new_price,
                    pct_change=pct_change,
                    tx_hash=rebalance_tx,
                )

        # ── Cross-pair rebalance ──────────────────────────────────────────────
        # After all TOKEN/USD pools have been oracle-anchored, derive each
        # token's effective USD price from the just-updated reserves and use
        # that to rebalance TOKEN/TOKEN pools.  Without this step the cross-pair
        # exchange rates float freely and enable triangular arbitrage loops.
        token_usd_prices: dict[str, float] = {}
        all_prices = await self.bot.db.get_all_prices(guild.id)
        for p in all_prices:
            token_usd_prices[p["symbol"]] = float(p["price"])
        for sc in ("USDC", "DSD"):
            token_usd_prices[sc] = 1.0
        token_usd_prices["USD"] = 1.0

        # Re-fetch pool list to get latest reserves after the USD rebalances above.
        pools = await self.bot.db.get_all_pools(guild.id)
        for pool in pools:
            ta, tb = pool["token_a"], pool["token_b"]
            if ta in _STABLE_QUOTE or tb in _STABLE_QUOTE:
                continue  # already handled by the USD rebalance loop
            price_a = token_usd_prices.get(ta)
            price_b = token_usd_prices.get(tb)
            if not price_a or not price_b or price_b <= 0:
                continue
            implied_ratio = price_a / price_b  # tb per ta at fair value
            ra, rb = pool["reserve_a"], pool["reserve_b"]
            if ra <= 0 or rb <= 0:
                continue
            pool_ratio = rb / ra
            deviation = abs(pool_ratio - implied_ratio) / implied_ratio
            if deviation < Config.POOL_ARB_THRESHOLD:
                continue
            pool_key = f"{guild.id}:{pool['pool_id']}"
            last_rb = self._last_rebalance_ts.get(pool_key, 0.0)
            if now - last_rb < Config.POOL_ARB_COOLDOWN:
                continue
            # Zero fee for oracle rebalances (same reasoning as the USD pool loop above)
            f2 = 1.0
            k2 = ra * rb
            if pool_ratio > implied_ratio:
                P = implied_ratio
                dx = _solve_arb_quadratic(P * f2, P * ra * (1.0 + f2), P * ra * ra - k2)
                if not dx:
                    continue
                out_b = rb * dx * f2 / (ra + dx * f2)
                new_a, new_b = ra + dx, rb - out_b
            else:
                P = implied_ratio
                dy = _solve_arb_quadratic(f2, rb * (1.0 + f2), rb * rb - P * k2)
                if not dy:
                    continue
                out_a = ra * dy * f2 / (rb + dy * f2)
                new_a, new_b = ra - out_a, rb + dy
            if new_a <= 0 or new_b <= 0:
                continue
            # Same defensive guard as the USD-pool branch above: a token
            # whose supply has blown past the NUMERIC(36,0) ceiling (10^36
            # raw units) can produce intermediate values that overflow the
            # reserves column. Skip the pool on overflow rather than
            # letting the listener crash the whole rebalance pass.
            try:
                _xnew_a = int(new_a)
                _xnew_b = int(new_b)
                if abs(_xnew_a) >= 10**36 or abs(_xnew_b) >= 10**36:
                    log.warning(
                        "trade.rebalance: skipping cross-pair pool=%s (overflow guard); "
                        "new_a=%s new_b=%s",
                        pool["pool_id"], new_a, new_b,
                    )
                    continue
                await self.bot.db.update_pool_reserves(
                    pool["pool_id"], guild.id, _xnew_a, _xnew_b, pool["total_lp"],
                )
            except Exception as exc:
                log.warning(
                    "trade.rebalance: cross-pair pool=%s update failed: %s: %s",
                    pool["pool_id"], type(exc).__name__, exc,
                )
                continue
            self._last_rebalance_ts[pool_key] = now
            # Reset LP snapshots so fee tracking restarts from correct ratios
            if pool["total_lp"] > 0:
                positions = await self.bot.db.get_pool_lp_positions(pool["pool_id"], guild.id)
                rpa2 = new_a / pool["total_lp"]  # raw/raw = human ratio
                rpb2 = new_b / pool["total_lp"]
                for pos in positions:
                    if pos.get("lp_shares", 0) > 0:
                        await self.bot.db.upsert_lp_snapshot(
                            pos["user_id"], guild.id, pool["pool_id"], to_raw(rpa2), to_raw(rpb2)
                        )

    # ══════════════════════════════════════════════════════════════════════════
    #  Pool helpers (from pools.py)
    # ══════════════════════════════════════════════════════════════════════════

    async def _valid_tokens(self, guild_id: int) -> set[str]:
        tokens = await self.bot.db.get_all_tokens_for_guild(guild_id)
        return set(tokens.keys()) | {"USD"}

    async def _debit(self, ctx: DiscoContext, symbol: str, amount: float, network: str = "") -> bool:
        if symbol == "USD":
            if amount > to_human(ctx.user_row["wallet"]):
                await ctx.reply_error(f"You only have **`{fmt_usd(to_human(ctx.user_row['wallet']))}`** in your wallet.")
                return False
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(-amount))
        elif network:
            h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, network, symbol)
            h_raw = int(h["amount"]) if h else 0
            avail = to_human(h_raw)
            if amount > avail + 1e-9:
                await ctx.reply_error(f"You only have **`{fmt_token(avail, symbol)}`** in your DeFi wallet.")
                return False
            # Clamp to the exact raw balance to prevent float round-trip from overshooting by 1-2 ulps.
            deduct_raw = min(to_raw(amount), h_raw)
            await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, network, symbol, -deduct_raw)
        else:
            h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, symbol)
            h_raw = int(h["amount"]) if h else 0
            avail = to_human(h_raw)
            if amount > avail + 1e-9:
                await ctx.reply_error(f"You only have **`{fmt_token(avail, symbol)}`**.")
                return False
            # Clamp to the exact raw balance to prevent float round-trip from overshooting by 1-2 ulps.
            deduct_raw = min(to_raw(amount), h_raw)
            await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, -deduct_raw)
        return True

    async def _credit(self, ctx: DiscoContext, symbol: str, amount: float, network: str = "") -> None:
        if symbol == "USD":
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(amount))
        elif network:
            await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, network, symbol, to_raw(amount))
        else:
            await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, to_raw(amount))

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade (invoke_without_command → prices)
    # ══════════════════════════════════════════════════════════════════════════

    @commands.hybrid_group(name="trade", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def trade(self, ctx: DiscoContext, filter: str = "") -> None:
        """Show token prices. Accepts network, stablecoin, or token filters.
        Examples: .trade  .trade --arc  .trade --sol  .trade USDC  .trade --arb  .trade --sun"""
        if await suggest_subcommand(ctx, self.trade):
            return
        try:
            from services.onboarding import maybe_send_intro
            await maybe_send_intro(ctx, "trade")
        except Exception:
            pass
        rows = await ctx.db.get_all_prices(ctx.guild_id)
        if not rows:
            await ctx.reply_error("No price data yet. Try again in a moment.")
            return

        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        price_map = {r["symbol"]: r for r in rows}

        # Normalize: strip leading dashes so --arc == arc
        raw = filter.lstrip("-").upper()

        # Resolve filter type
        _NET_SHORT_FILTER = {
            "SUN": "Sun Network",       "MTA": "Moneta Chain",   "MONETA": "Moneta Chain",
            "ARC": "Arcadia Network",  "ARCADIA": "Arcadia Network",
            "DSC": "Discoin Network",   "DISCOIN": "Discoin Network",
        }
        _STABLE_QUOTES = {
            "USDC": "Arcadia Network",
            "DSD":  "Discoin Network",
        }

        filter_network = _NET_SHORT_FILTER.get(raw, "")        # e.g. "arc" → Arcadia Network
        quote_network  = _STABLE_QUOTES.get(raw, "")   # e.g. "USDC" → Arcadia Network (price-in-stablecoin mode)
        filter_token   = raw if (raw and raw in all_tokens and raw not in _NET_SHORT_FILTER and raw not in _STABLE_QUOTES) else ""

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

        footer_hint = "Filters: --arc, --dsc, --sun, --mta, --USDC, --DSD, or a token symbol."
        pages = []

        for network in sorted(by_network):
            net_stable = Config.NETWORK_STABLECOIN.get(network, "")
            title_quote = raw if raw and raw not in _NET_SHORT_FILTER else "USD"
            _b = card(f"📈 {network} Prices", description=f"Quote: **{title_quote}**  ·  Live oracle data").color(C_AMBER)
            entries = sorted(by_network[network], key=lambda x: x[0])
            # If filtering to a specific token, only show that one
            if filter_token:
                entries = [(s, r, t) for s, r, t in entries if s == filter_token]

            for symbol, row, tcfg in entries:
                if tcfg.get("stablecoin") or tcfg.get("consensus") == "Fiat":
                    emoji = tcfg.get("emoji", "💵")
                    _b.field(f"{emoji} {symbol}", "**$1.0000**\n🔒 *pegged · stable*", True)
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
                        pp = pool["reserve_b"] / pool["reserve_a"] if ca == symbol else pool["reserve_a"] / pool["reserve_b"]
                        price_str = f"**{pp:,.6f} {net_stable}**"
                    else:
                        price_str = f"**{row['price']:,.6f} {net_stable}** *(oracle)*"
                    _b.field(
                        f"{emoji} {symbol}",
                        f"💵 {price_str}\n{sign} **{abs(pct_change):.2f}%**",
                        True,
                    )
                else:
                    # USD mode  -  richer terminal-style display
                    price_str = f"**${row['price']:,.6f}**"
                    pool_subtext = ""
                    if net_stable:
                        pool_id, ca, cb = ctx.db.make_pool_id(symbol, net_stable)
                        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
                        if pool and pool["reserve_a"] > 0 and pool["reserve_b"] > 0:
                            pp = pool["reserve_b"] / pool["reserve_a"] if ca == symbol else pool["reserve_a"] / pool["reserve_b"]
                            pool_subtext = f"\n💧 Pool: `{pp:,.4f} {net_stable}`"
                    _b.field(
                        f"{emoji} {symbol}",
                        (
                            f"💵 {price_str}  {sign} **{abs(pct_change):.2f}%**\n"
                            f"📊 H: `${row['day_high']:,.4f}`  L: `${row['day_low']:,.4f}`"
                            f"{pool_subtext}"
                        ),
                        True,
                    )

            embed = _b.build()
            if embed.fields:
                embed.set_footer(text=footer_hint)
                pages.append(embed)

        if not pages:
            available = ", ".join(f"`{r['symbol']}`" for r in rows[:20])
            await ctx.reply_error(
                f"No price data for `{filter}`. "
                f"Available tokens: {available}"
            )
            return
        await send_paginated(ctx, pages)

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade prices (explicit subcommand alias)
    # ══════════════════════════════════════════════════════════════════════════

    @trade.command(name="prices")
    @guild_only
    async def prices(self, ctx: DiscoContext, filter: str = "") -> None:
        """Show token prices (same as bare /trade)."""
        await self.trade(ctx, filter=filter)

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade history
    # ══════════════════════════════════════════════════════════════════════════

    @trade.command(name="history", aliases=["log", "txs"])
    @guild_only
    @ensure_registered
    async def trade_history(self, ctx: DiscoContext, filter: str = "all") -> None:
        """Show your recent trade history. Filter: all (default), buy, sell, swap."""
        filter = filter.lower()
        valid_filters = ("all", "buy", "sell", "swap")
        if filter not in valid_filters:
            await ctx.reply_error(
                f"Invalid filter `{filter}`. Use one of: `all`, `buy`, `sell`, `swap`."
            )
            return

        tx_type = None if filter == "all" else filter
        trades = await ctx.db.get_user_trade_history(
            ctx.author.id, ctx.guild_id, limit=25, tx_type=tx_type,
        )
        if not trades:
            await ctx.reply_error("No trades found." if filter == "all" else f"No `{filter}` trades found.")
            return

        per_page = 8
        pages = []
        total = len(trades)
        for i in range(0, total, per_page):
            chunk = trades[i : i + per_page]
            lines = []
            for t in chunk:
                tx_type_val = t["tx_type"].upper()
                _ain = t.h("amount_in")
                _aout = t.h("amount_out")
                if tx_type_val == "BUY":
                    desc = f"\U0001f4e5 **Bought** {_aout:.4f} {t['symbol_out']} for ${_ain:,.2f}"
                elif tx_type_val == "SELL":
                    desc = f"\U0001f4e4 **Sold** {_ain:.4f} {t['symbol_in']} for ${_aout:,.2f}"
                elif tx_type_val == "SWAP":
                    desc = f"\U0001f501 **Swapped** {_ain:.4f} {t['symbol_in']} for {_aout:.4f} {t['symbol_out']}"
                else:
                    desc = f"{t['tx_type']} {_ain:.4f} {t['symbol_in']}"

                gas_fee = float(t.get("gas_fee", 0) or 0)
                if gas_fee > 0:
                    desc += f" (gas: {gas_fee:.6f} {t.get('gas_coin', '')})"

                ts_val = t["ts"].timestamp() if hasattr(t["ts"], "timestamp") else float(t["ts"] or 0)
                elapsed = int(time.time() - ts_val)
                desc += f" -- {FormatKit.time_ago(max(0, elapsed))}"
                lines.append(desc)

            start_idx = i + 1
            end_idx = i + len(chunk)
            body = "\n".join(lines)
            embed = card(
                "Trade History",
                description=body,
                color=C_INFO,
            ).build()
            embed.set_footer(text=f"Showing {start_idx}-{end_idx} of {total} | Filter: {filter}")
            pages.append(embed)

        await send_paginated(ctx, pages)

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade buy
    # ══════════════════════════════════════════════════════════════════════════

    @trade.command(name="buy")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def buy(self, ctx: DiscoContext, arg1: str, arg2: str = "", *, flags: str = "") -> None:
        """Buy coins/stablecoins with USD (or SUN). Accepts 'SYM amount' or 'amount SYM'.
        Use $<amount> to specify a USD amount (e.g. '.buy ARC $100' buys $100 of ARC).
        Flags: yes to skip confirmation.  with SUN to pay with SUN instead of USD.
        Only network coins and stablecoins can be bought directly. For other tokens, use .swap."""
        # Flexible arg order
        if not arg2:
            await ctx.reply_error(f"Usage: `{ctx.prefix}trade buy <SYMBOL> <amount>` or `{ctx.prefix}trade buy <amount> <SYMBOL>`")
            return
        symbol, amount_str = _parse_sym_amt(arg1, arg2)
        fl = flags.lower()
        auto_confirm = "yes" in fl or "-y" in fl
        pay_with_sun = "with sun" in fl

        # USD is the base currency  -  not a buyable token
        if symbol == "USD":
            await ctx.reply_error(
                "USD is the base currency  -  you already have it in your wallet.\n"
                f"Use `{ctx.prefix}trade buy USDC` or `{ctx.prefix}trade buy DSD` to get network stablecoins."
            )
            return

        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        if symbol not in all_tokens:
            await ctx.reply_error(f"Unknown token `{symbol}`. Use `{ctx.prefix}trade prices` to see all tokens.")
            return

        # ── Admin halts ───────────────────────────────────────────────────────
        if await ctx.db.is_token_disabled(ctx.guild_id, symbol):
            await ctx.reply_error(f"**{symbol}** trading is currently disabled by an admin.")
            return
        tok_net = all_tokens.get(symbol, {}).get("network", "")
        net_key = _NET_SHORT.get(tok_net, "")
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
                f"Direct `{ctx.prefix}trade buy` is available for: {', '.join(sorted(Config.BUYABLE_WITH_USD))}."
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
                    f"Use `{ctx.prefix}trade buy {symbol} <amount>` with USD instead."
                )
                return
            if symbol not in _NETWORK_COINS:
                await ctx.reply_error(
                    f"SUN can only be used to buy network coins: **ARC, DSC**.\n"
                    f"Use USD to buy **{symbol}**."
                )
                return

        # ── Per-symbol anti-sandwich cooldown ────────────────────────────────
        if (_cd_buy := check_trade_cooldown(ctx.author.id, ctx.guild_id, symbol)) > 0:
            await ctx.reply_cooldown(_cd_buy)
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
            sun_bal = to_human(sun_h["amount"]) if sun_h else 0.0
            sun_usd_rate = float(sun_row["price"]) if sun_row else 0.0
        else:
            pass  # USD payment path continues below

        _fee_cfg = await ctx.db.guilds.get_fee_config(ctx.guild_id)
        _fee_est = 0.0
        _sun_fee_est = 0.0
        pct = _fee_cfg["platform_fee_pct"]
        fee_min = _fee_cfg["platform_fee_min"]
        fee_max = _fee_cfg["platform_fee_max"]
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
                # "buy all" with USD: cost + fee(cost) = wallet exactly.
                wallet = to_human(ctx.user_row["wallet"])
                _cost_all, _fee_est = resolve_all_spend(wallet, pct, fee_min, fee_max)
                qty = _cost_all / float(price_row["price"]) if price_row["price"] > 0 else 0.0
        else:
            # Check for $-prefixed USD amount (e.g. "$100" = buy $100 worth)
            amount_str = str(amount_str)
            _usd_mode = amount_str.startswith("$")
            _raw = amount_str.lstrip("$")
            try:
                _parsed = float(_raw)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `all`, or `$<usd>` (e.g. `$100`).")
                return
            if _usd_mode:
                # Convert USD amount to token quantity
                qty = _parsed / float(price_row["price"]) if price_row["price"] > 0 else 0.0
            else:
                qty = _parsed
        if not math.isfinite(qty) or qty <= 0:
            await ctx.reply_error("Amount must be a positive finite number.")
            return

        cost_usd = float(price_row["price"]) * qty
        # buy-all: restore the pre-resolved cost to avoid price round-trip drift
        # (cost_all / price * price != cost_all in float, causing spurious balance errors)
        if _buying_all and not pay_with_sun:
            cost_usd = _cost_all
        if pay_with_sun:
            cost_sun = cost_usd / sun_usd_rate if sun_usd_rate > 0 else float("inf")
            # SUN fee: flat PCT of cost_sun (no USD min/max  -  SUN is a game token)
            buy_fee_sun = cost_sun * _fee_cfg["platform_fee_pct"]
            buy_fee_sun_reserve = buy_fee_sun / 4.0
            total_sun_cost = cost_sun + buy_fee_sun
            if total_sun_cost > sun_bal:
                await ctx.reply_error(
                    f"That costs **{cost_sun:,.4f} SUN** + **{buy_fee_sun:,.4f} SUN** fee = **{total_sun_cost:,.4f} SUN** "
                    f"but you only have **{sun_bal:,.8g} SUN**."
                )
                return
            payment_str = f"{cost_sun:,.4f} SUN (≈ ${cost_usd:,.2f})"
        else:
            # USD fee: percentage-based, clamped to min/max.
            # Buddy fee rebate is applied BEFORE the clamp so it doesn't
            # silently disappear on tiny trades that hit the min floor.
            _raw_buy_fee = cost_usd * _fee_cfg["platform_fee_pct"]
            _buy_fee_rebate_pct = 0.0
            _buy_fee_rebate_amt = 0.0
            try:
                from services.buddy_bonus import buddy_bonus
                _buddy_mult = await buddy_bonus(ctx.db, ctx.guild_id, ctx.author.id, lane="trade")
                if _buddy_mult > 1.0:
                    _pre_rebate = _raw_buy_fee
                    _raw_buy_fee = _raw_buy_fee / _buddy_mult
                    _buy_fee_rebate_pct = 1.0 - (1.0 / _buddy_mult)
                    _buy_fee_rebate_amt = _pre_rebate - _raw_buy_fee
            except Exception:
                pass  # buddy subsystem must never break trade
            buy_fee = max(_fee_cfg["platform_fee_min"],
                          min(_fee_cfg["platform_fee_max"], _raw_buy_fee))
            buy_fee_reserve = buy_fee / 2.0
            if cost_usd + buy_fee > to_human(ctx.user_row["wallet"]):
                await ctx.reply_error(
                    f"That costs **${cost_usd:,.4f}** + **${buy_fee:,.2f}** fee = **${cost_usd+buy_fee:,.2f}** "
                    f"but you only have **${to_human(ctx.user_row['wallet']):,.2f}**."
                )
                return
            payment_str = f"{fmt_usd(cost_usd)} USD"

        # Confirmation view
        if not auto_confirm:
            # Estimated slippage preview - matches the same formula the execute
            # path uses so the fill below is what the user actually gets.
            _spot_price_buy = float(price_row["price"])
            _circ_supply_est_buy = to_human(all_tokens.get(symbol, {}).get("circulating_supply") or 0)
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
            _exp = int(time.time() + 30)
            desc = (
                f"{_buy_banner}"
                f"Send **`{payment_str}`**\n"
                f"Receive ≈ **`{_est_qty_after_impact:,.6f} {symbol}`**\n"
                f"Spot: 1 {symbol} = `${_spot_price_buy:,.4f}`  →  est. fill `${_est_eff_price_buy:,.4f}`\n"
                f"📊 **Price impact:** `-{_est_impact_buy*100:.3f}%`\n"
                f"{fee_line}"
                f"\n\nExpires {fmt_ts(int(_exp))}  ·  Use `yes` to skip confirmation."
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
                from core.framework.links import sanitize_embed
                sanitize_embed(cancel_embed)
                await conf_msg.edit(embed=cancel_embed, view=None)
                return

        # Volume limit check (shared with swap service)
        allowed, remaining = _check_user_swap_volume(ctx.author.id, ctx.guild_id, cost_usd)
        if not allowed:
            await ctx.reply_error(
                f"Hourly volume limit reached. Remaining: **`{fmt_usd(remaining)}`**. "
                f"Limit: `{fmt_usd(Config.USER_SWAP_HOURLY_LIMIT_USD)}`/hour."
            )
            return

        # ── Re-check balances after confirmation (prevents stale-state exploits) ─
        if pay_with_sun:
            sun_h_fresh = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, "SUN")
            sun_bal_fresh = to_human(sun_h_fresh["amount"]) if sun_h_fresh else 0.0
            if total_sun_cost > sun_bal_fresh:
                await ctx.reply_error(
                    f"Balance changed since confirmation. Need **{total_sun_cost:,.4f} SUN** "
                    f"but you now have **{sun_bal_fresh:,.8g} SUN**."
                )
                return
        else:
            fresh_user = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
            _fresh_wallet_raw = int(fresh_user["wallet"]) if fresh_user else 0
            fresh_wallet = to_human(_fresh_wallet_raw)
            if _buying_all:
                # Re-resolve from fresh balance so float drift / minor balance
                # changes between confirmation and execution never block the trade.
                cost_usd, buy_fee = resolve_all_spend(fresh_wallet, pct, fee_min, fee_max)
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
        _cur_price_buy = float(price_row["price"])
        _ath_buy = float(price_row.get("ath") or 0.0)
        _depeg_reservation_ts: float | None = None
        if _is_depeg(_cur_price_buy, _ath_buy):
            _allowed_buy, _remaining_buy, _depeg_reservation_ts = await _reserve_depeg_buy(
                ctx.author.id, ctx.guild_id, symbol, cost_usd
            )
            if not _allowed_buy:
                await ctx.reply_error(
                    f"**{symbol}** is in depeg mode (price is below "
                    f"{Config.DEPEG_THRESHOLD*100:.0f}% of its all-time high).\n"
                    f"Daily buy limit: **${Config.DEPEG_DAILY_BUY_USD:,.0f}**  -  "
                    f"remaining today: **${_remaining_buy:,.2f}**."
                )
                return

        # ── Token contract: load burn / xfer fee parameters ───────────────────
        _buy_contract = await ctx.db.get_token_contract(ctx.guild_id, symbol)
        _buy_params = _buy_contract if isinstance(_buy_contract, dict) else {}
        if isinstance(_buy_params.get("params"), str):
            import json as _json_buy
            _buy_params = _json_buy.loads(_buy_params["params"])
        _builtin_buy_burn = Config.TOKENS.get(symbol, {}).get("burn_rate", 0.0)
        _buy_burn_rate = _buy_params.get("burn_rate", 0.0) or _builtin_buy_burn
        _buy_xfer_fee = _buy_params.get("transfer_fee", 0.0)

        # ── Execute trade atomically ─────────────────────────────────────────
        _old_price_buy = float(price_row["price"])
        impact = cost_usd / Config.PRICE_IMPACT_DIVISOR
        tok_meta_buy = all_tokens.get(symbol, {})
        circ_supply_buy = to_human(tok_meta_buy.get("circulating_supply") or 0)
        mkt_cap_buy = _old_price_buy * circ_supply_buy
        if mkt_cap_buy > 0 and cost_usd > 0.001 * mkt_cap_buy:
            mc_ratio = cost_usd / mkt_cap_buy
            mc_multiplier = min(1.0 + mc_ratio * 2.0, 5.0)  # cap at 5x to prevent runaway pumps
            impact = impact * mc_multiplier
        _eff_price_buy = max(1e-15, _old_price_buy * (1 + impact))
        qty = cost_usd / _eff_price_buy
        # Compute burn / fee against the post-impact qty so the displayed
        # numbers, the supply burn, and the user's actual fill all agree.
        # Computing them off the spot-price qty (pre-impact) leaves the user
        # with a different post-contract balance than the embed advertises.
        _buy_burned = qty * _buy_burn_rate
        _buy_fee_tokens = qty * _buy_xfer_fee
        qty_after_contract = qty - _buy_burned - _buy_fee_tokens

        try:
            async with ctx.db.atomic():
                if pay_with_sun:
                    await ctx.db.update_holding(ctx.author.id, ctx.guild_id, "SUN", to_raw(-(cost_sun + buy_fee_sun)))
                    await ctx.db.split_to_community_reserves(ctx.guild_id, "SUN", to_raw(buy_fee_sun), sun_usd_rate)
                else:
                    # For buy-all, cap at exact raw balance to prevent float→Decimal overshoot
                    _buy_wallet_delta = to_raw(-(cost_usd + buy_fee))
                    if _buying_all:
                        _buy_wallet_delta = -min(-_buy_wallet_delta, _fresh_wallet_raw)
                    await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, _buy_wallet_delta)
                    await ctx.db.split_to_community_reserves(ctx.guild_id, "USD", to_raw(buy_fee))
                # Credit buyer with tokens after contract deductions
                new_holding = await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, to_raw(qty_after_contract))
                # Burn tokens: reduce circulating supply. Built-in tokens
                # live in crypto_prices, custom guild tokens in guild_tokens
                # -- mirror services/swap.py and dispatch by token type so
                # built-in burns aren't silently dropped on the floor.
                if _buy_burned > 0:
                    if symbol in Config.TOKENS:
                        await ctx.db.update_builtin_circulating_supply(
                            ctx.guild_id, symbol, to_raw(-_buy_burned),
                        )
                    else:
                        await ctx.db.update_circulating_supply(
                            ctx.guild_id, symbol, to_raw(-_buy_burned),
                        )
                # Transfer fee goes to community reserves
                if _buy_fee_tokens > 0:
                    _xfer_fee_usd = _buy_fee_tokens * float(price_row["price"])
                    await ctx.db.split_to_community_reserves(ctx.guild_id, "USD", to_raw(_xfer_fee_usd))
                await ctx.db.update_price(symbol, ctx.guild_id, _eff_price_buy)
                # Immediately extend the current 1-minute candle so the chart
                # reflects the impact without waiting up to PRICE_TICK_SECONDS
                # for the drift task.  upsert_candle takes GREATEST/LEAST on
                # high/low and overwrites close, so this is safe to stack with
                # subsequent drift ticks.
                await ctx.db.upsert_candle(
                    ctx.guild_id, f"{symbol}USD", _minute_ts(),
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
                await ctx.db.add_trade_volume(ctx.guild_id, f"{symbol}USD", to_raw(cost_usd))
        except Exception:
            if _depeg_reservation_ts is not None:
                _cancel_depeg_reservation(ctx.author.id, ctx.guild_id, symbol, _depeg_reservation_ts)
            raise

        # Realign pools + the drift_task's in-memory candle-open cache so the
        # next drift tick builds on the new regime instead of the pre-trade price.
        self._last_price[(ctx.guild_id, f"{symbol}USD")] = _eff_price_buy
        await ctx.bot.bus.publish("prices_updated", guild=ctx.guild)

        set_trade_cooldown(ctx.author.id, ctx.guild_id, symbol)
        _record_user_swap_volume(ctx.author.id, ctx.guild_id, cost_usd)
        _buy_vault_net = _VAULT_NET_MAP.get(all_tokens.get(symbol, {}).get("network", ""))
        if _buy_vault_net and cost_usd > 0:
            try:
                await credit_vault_volume(ctx.db, ctx.guild_id, _buy_vault_net, cost_usd, bot=ctx.bot)
            except Exception:
                pass  # never let vault update block a trade
        await ctx.bot.bus.publish(
            "trade", guild=ctx.guild, user=ctx.author,
            action="BUY", symbol=symbol, amount=qty,
            price=_eff_price_buy, total=cost_usd, tx_hash=tx_hash,
        )
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "buy", cost_usd, symbol=symbol, amount=qty)
        if cost_usd >= 5000:
            try:
                await ctx.db.log_server_event(
                    ctx.guild_id, ctx.channel.id, ctx.author.id,
                    "big_buy",
                    f"{ctx.author.display_name} bought {qty:,.4f} {symbol} for {fmt_usd(cost_usd)}",
                    cost_usd,
                )
            except Exception:
                pass

        _b = (
            card(f"✅ Bought {Config.currency_label(symbol)}", color=C_BUY)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("🪙 Received",     f"**{qty_after_contract:,.6f} {symbol}**",  True)
            .field("💵 Spent",        payment_str,                               True)
            .field("📈 Fill Price",   f"`${_eff_price_buy:,.4f}`\n📊 Slippage: `+{impact*100:.3f}%`", True)
        )
        if _buy_burned > 0:
            _b.field("🔥 Burned", f"{_buy_burned:,.6f} {symbol} ({_buy_burn_rate*100:.1f}%)", True)
        if _buy_fee_tokens > 0:
            _b.field("📋 Contract Fee", f"{_buy_fee_tokens:,.6f} {symbol} ({_buy_xfer_fee*100:.1f}%)", True)
        if pay_with_sun:
            _sun_fee_usd_res = buy_fee_sun / 2.0 * sun_usd_rate if sun_usd_rate > 0 else 0.0
            _b.field("🏦 Fee",
                f"`{buy_fee_sun:,.6f} SUN`\n↳ ${_sun_fee_usd_res:,.4f} → Vault",
                True)
        else:
            _buy_fee_usd_res = buy_fee / 2.0
            _fee_value = f"`${buy_fee:,.2f}`\n↳ ${_buy_fee_usd_res:,.2f} → Vault"
            if _buy_fee_rebate_amt > 0:
                _fee_value += (
                    f"\n↳ 🐾 Buddy: -{_buy_fee_rebate_pct*100:.1f}% "
                    f"(-${_buy_fee_rebate_amt:,.2f})"
                )
            _b.field("🏦 Fee", _fee_value, True)
        _b.field("💰 Now Holding",  f"**{to_human(new_holding):,.6f} {symbol}**", True)
        result_embed = _b.build()
        set_tx(result_embed, ctx.guild.id, tx_hash)
        if auto_confirm:
            await ctx.reply(embed=result_embed, mention_author=False)
        else:
            from core.framework.links import sanitize_embed
            sanitize_embed(result_embed)
            await conf_msg.edit(embed=result_embed, view=None)  # type: ignore[reportUnboundVariable]

    # ══════════════════════════════════════════════════════════════════════════
    #  sell everything helper
    # ══════════════════════════════════════════════════════════════════════════

    async def _sell_everything(self, ctx: DiscoContext, *, flags: str = "") -> None:
        """Sell all sellable CeFi holdings for USD with a single confirmation."""
        fl = flags.lower()
        auto_confirm = "yes" in fl or "-y" in fl

        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        holdings = await ctx.db.get_holdings(ctx.author.id, ctx.guild_id)

        # Filter to sellable tokens with balance > 0
        sellable: list[tuple[str, float, float]] = []  # (symbol, amount, price)
        total_gross = 0.0
        for h in holdings:
            sym = h["symbol"]
            amt = to_human(h["amount"])
            if amt <= 0 or sym not in Config.BUYABLE_WITH_USD:
                continue
            if await ctx.db.is_token_disabled(ctx.guild_id, sym):
                continue
            price_row = await ctx.db.get_price(sym, ctx.guild_id)
            if not price_row or float(price_row["price"]) <= 0:
                continue
            price = float(price_row["price"])
            gross = price * amt
            sellable.append((sym, amt, price))
            total_gross += gross

        if not sellable:
            await ctx.reply_error(f"You have no sellable holdings.\nDirect `{ctx.prefix}trade sell` works for: " + ", ".join(sorted(Config.BUYABLE_WITH_USD)))
            return

        _fee_cfg = await ctx.db.guilds.get_fee_config(ctx.guild_id)
        total_fees = 0.0
        total_impact = 0.0
        lines = []
        # Pre-compute per-token impact + effective revenue using the same
        # formula as single-token .sell (around trade.py:2289). Previously
        # this handler charged only the platform fee and used SPOT price,
        # so users could dump whale-sized bags on .sell everything and
        # sidestep the slippage a sequence of single .sell commands would
        # have eaten. Impact is per-token (circ_supply is per-token), so
        # selling multiple tokens stays independent.
        priced: list[tuple[str, float, float, float, float, float, float]] = []
        for sym, amt, price in sellable:
            gross = price * amt
            tcfg = all_tokens.get(sym, {})
            circ_supply = to_human(tcfg.get("circulating_supply") or 0)
            mkt_cap = price * circ_supply
            impact = gross / Config.PRICE_IMPACT_DIVISOR
            if mkt_cap > 0 and gross > 0.001 * mkt_cap:
                mc_ratio = gross / mkt_cap
                impact = impact * min(1.0 + mc_ratio * 2.0, 5.0)
            impact = min(impact, 0.95)
            eff_price = max(1e-9, price * (1 - impact))
            eff_revenue = amt * eff_price
            fee = max(_fee_cfg["platform_fee_min"],
                      min(_fee_cfg["platform_fee_max"], eff_revenue * _fee_cfg["platform_fee_pct"]))
            total_fees   += fee
            total_impact += (gross - eff_revenue)
            emoji = tcfg.get("emoji", "")
            impact_str = f"  *(impact {impact*100:.2f}%)*" if impact >= 0.001 else ""
            lines.append(
                f"{emoji}**{sym}**: {amt:,.4f} @ ${price:,.4f} "
                f"-> **${eff_revenue:,.2f}**{impact_str}"
            )
            priced.append((sym, amt, price, eff_price, eff_revenue, fee, impact))

        total_gross = sum(p * a for _, a, p, *_ in priced)
        total_eff_revenue = sum(er for *_, er, _, _ in priced)
        total_net = total_eff_revenue - total_fees
        desc = (
            "Selling **all** holdings for USD:\n\n"
            + "\n".join(lines)
            + f"\n\n**Gross @ spot:** ${total_gross:,.2f}\n"
            f"**Price impact:** -${total_impact:,.2f}\n"
            f"**Platform fee:** -${total_fees:,.2f}\n"
            f"**You receive:** ${total_net:,.2f}"
        )

        if not auto_confirm:
            confirm_embed = (
                card("💰 Sell Everything", description=desc, color=C_AMBER)
                .footer(f"Expires {fmt_ts(int(time.time() + 30))}  ·  Use `yes` to skip confirmation")
                .build()
            )
            view = ConfirmView(ctx.author.id, timeout=30)
            msg = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
            view.message = msg
            confirmed = await view.wait_result()
            if confirmed is not True:
                try:
                    await msg.edit(embed=card("💰 Sell Cancelled", color=C_NEUTRAL).build(), view=None)
                except Exception:
                    pass
                return

        # Execute all sells. The priced tuple carries the slippage-adjusted
        # numbers the user saw in the preview, so actual fills match the
        # confirmation down to the penny (modulo balance drift between
        # pre-check and execute, which is capped via `actual`).
        from services.bottleneck import (
            apply_bottleneck, realized_sell_gain_raw, CreditKind,
        )
        sold_lines = []
        total_received = 0.0
        total_bn_drag = 0.0
        total_bn_boost = 0.0
        for sym, pre_amt, spot_price, eff_price, pre_eff_rev, pre_fee, pre_impact in priced:
            holding = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, sym)
            hold_raw = int(holding["amount"]) if holding else 0
            actual = to_human(hold_raw)
            if actual <= 0:
                continue
            amt = pre_amt
            if actual < amt:
                amt = actual

            # Re-compute at live spot. Keeps slippage correct even if
            # another sell in the same batch (or a swap from another
            # user) moved the oracle between preview and execute.
            eff_revenue = amt * eff_price
            fee = max(_fee_cfg["platform_fee_min"],
                      min(_fee_cfg["platform_fee_max"], eff_revenue * _fee_cfg["platform_fee_pct"]))
            if fee >= eff_revenue:
                # Minimum fee would zero out the sale; skip this token
                # (matches single-sell's "trade too small" guard).
                continue
            net_rev = eff_revenue - fee
            # Wealth Bottleneck on the realized USD gain only.
            _gain_raw_se = await realized_sell_gain_raw(
                ctx.db, uid=ctx.author.id, gid=ctx.guild_id, symbol=sym,
                sell_qty_raw=abs(int(to_raw(amt))), sell_price_usd=eff_price,
            )
            if _gain_raw_se > 0:
                _bn_se = await apply_bottleneck(
                    ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
                    gross_raw=_gain_raw_se, kind=CreditKind.TRADE_GAIN,
                )
                _delta_h = (_bn_se.boost_wallet_raw - _bn_se.drag_usd_raw) / 10**18
                net_rev += _delta_h
                total_bn_drag += _bn_se.drag_usd_raw / 10**18
                total_bn_boost += _bn_se.boost_wallet_raw / 10**18
            sell_delta_raw = -min(to_raw(amt), hold_raw)
            if sell_delta_raw == 0:
                continue
            async with ctx.db.atomic():
                await ctx.db.update_holding(ctx.author.id, ctx.guild_id, sym, sell_delta_raw)
                await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(net_rev))
                await ctx.db.split_to_community_reserves(ctx.guild_id, "USD", to_raw(fee))
                # Move the oracle so the next seller eats fresh slippage
                # instead of getting stale pre-impact price.
                await ctx.db.update_price(sym, ctx.guild_id, eff_price)
                await ctx.db.log_tx(
                    ctx.guild_id, ctx.author.id, "SELL",
                    symbol_in=sym, amount_in=to_raw(amt),
                    symbol_out="USD", amount_out=to_raw(eff_revenue),
                    price_at=eff_price, network="usd",
                )
                await ctx.db.add_trade_volume(ctx.guild_id, f"{sym}USD", to_raw(eff_revenue))
            _sv_net = _VAULT_NET_MAP.get(all_tokens.get(sym, {}).get("network", ""))
            if _sv_net and eff_revenue > 0:
                try:
                    await credit_vault_volume(ctx.db, ctx.guild_id, _sv_net, eff_revenue, bot=ctx.bot)
                except Exception:
                    pass
            total_received += net_rev
            tcfg = all_tokens.get(sym, {})
            emoji = tcfg.get("emoji", "")
            impact_note = f", impact {pre_impact*100:.2f}%" if pre_impact >= 0.001 else ""
            sold_lines.append(
                f"{emoji}**{sym}**: {amt:,.4f} -> **${net_rev:,.2f}** "
                f"(fee: ${fee:,.2f}{impact_note})"
            )

        user = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        new_wallet = to_human(user["wallet"]) if user else 0.0
        _bn_summary = ""
        if total_bn_drag > 0:
            _bn_summary = f"⚖️ Bottleneck: -${total_bn_drag:,.2f} to community pool"
        elif total_bn_boost > 0:
            _bn_summary = f"⚖️ Bottleneck: +${total_bn_boost:,.2f} from community pool"
        _eb = (
            card("💰 Sold Everything", color=C_SELL)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .description("\n".join(sold_lines) if sold_lines else "Nothing sold.")
            .field("💵 Total Received", f"**${total_received:,.2f}**", True)
            .field("💰 New Balance", f"**${new_wallet:,.2f}**", True)
        )
        if _bn_summary:
            _eb.footer(_bn_summary)
        result_embed = _eb.build()
        if not auto_confirm:
            try:
                await msg.edit(embed=result_embed, view=None)  # type: ignore[reportUnboundVariable]
            except Exception:
                await ctx.reply(embed=result_embed, mention_author=False)
        else:
            await ctx.reply(embed=result_embed, mention_author=False)

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade sell
    # ══════════════════════════════════════════════════════════════════════════

    @trade.command(name="sell")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def sell(self, ctx: DiscoContext, arg1: str, arg2: str = "", *, flags: str = "") -> None:
        """Sell coins/stablecoins for USD. Accepts 'SYM amount' or 'amount SYM'.
        Use $<amount> to specify a USD amount (e.g. '.sell ARC $500' sells $500 worth of ARC).
        Use 'trade sell everything' to sell all sellable holdings.
        Flags: yes to skip confirmation.
        Only network coins and stablecoins can be sold for USD. For other tokens, use .swap."""
        if arg1.lower() == "everything" and not arg2:
            await self._sell_everything(ctx, flags=flags)
            return
        if not arg2:
            await ctx.reply_error(f"Usage: `{ctx.prefix}trade sell <SYMBOL> <amount>` or `{ctx.prefix}trade sell <amount> <SYMBOL>`")
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
        _net_key2 = _NET_SHORT.get(_tok_net, "")
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
                f"Direct `{ctx.prefix}trade sell` is available for: {', '.join(sorted(Config.BUYABLE_WITH_USD))}."
            )
            return

        # ── Per-symbol anti-sandwich cooldown ────────────────────────────────
        if (_cd_sell := check_trade_cooldown(ctx.author.id, ctx.guild_id, symbol)) > 0:
            await ctx.reply_cooldown(_cd_sell)
            return

        holding = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, symbol)
        available = to_human(holding["amount"]) if holding else 0.0

        price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        if not price_row:
            await ctx.db.seed_prices(ctx.guild_id)
            price_row = await ctx.db.get_price(symbol, ctx.guild_id)
        if not price_row:
            await ctx.reply_error("Price data unavailable.")
            return

        amount_str = str(amount_str)
        _selling_all = amount_str.lower() == "all"
        if _selling_all:
            amt = available
        else:
            # Check for $-prefixed USD amount (e.g. "$500" = sell $500 worth)
            _usd_mode = amount_str.startswith("$")
            _raw = amount_str.lstrip("$")
            try:
                _parsed = float(_raw)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `all`, or `$<usd>` (e.g. `$500`).")
                return
            if not math.isfinite(_parsed):
                await ctx.reply_error("Amount must be a finite number.")
                return
            if _usd_mode:
                # Convert USD amount to token quantity
                amt = _parsed / float(price_row["price"]) if price_row["price"] > 0 else 0.0
            else:
                amt = _parsed

        if amt <= 0 or available == 0:
            await ctx.reply_error(f"You have no **{symbol}** to sell.")
            return
        if amt > available:
            await ctx.reply_error(f"You only have **{available:.8g} {symbol}**.")
            return

        _fee_cfg = await ctx.db.guilds.get_fee_config(ctx.guild_id)
        revenue = float(price_row["price"]) * amt
        # Buddy fee rebate is applied BEFORE the clamp; see buy path for
        # the same pattern.
        _raw_sell_fee = revenue * _fee_cfg["platform_fee_pct"]
        _sell_fee_rebate_pct = 0.0
        _sell_fee_rebate_amt = 0.0
        try:
            from services.buddy_bonus import buddy_bonus
            _buddy_mult = await buddy_bonus(ctx.db, ctx.guild_id, ctx.author.id, lane="trade")
            if _buddy_mult > 1.0:
                _pre_rebate = _raw_sell_fee
                _raw_sell_fee = _raw_sell_fee / _buddy_mult
                _sell_fee_rebate_pct = 1.0 - (1.0 / _buddy_mult)
                _sell_fee_rebate_amt = _pre_rebate - _raw_sell_fee
        except Exception:
            pass  # buddy subsystem must never break trade
        sell_fee = max(_fee_cfg["platform_fee_min"],
                       min(_fee_cfg["platform_fee_max"], _raw_sell_fee))
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
            _circ_supply_est_sell = to_human(all_tokens.get(symbol, {}).get("circulating_supply") or 0)
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

        # Volume limit check (shared with swap service)
        _sell_usd_val = revenue  # revenue = price * qty
        allowed, remaining = _check_user_swap_volume(ctx.author.id, ctx.guild_id, _sell_usd_val)
        if not allowed:
            await ctx.reply_error(
                f"Hourly volume limit reached. Remaining: **`{fmt_usd(remaining)}`**. "
                f"Limit: `{fmt_usd(Config.USER_SWAP_HOURLY_LIMIT_USD)}`/hour."
            )
            return

        # ── Re-check holding after confirmation ─────────────────────────────
        fresh_holding = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, symbol)
        fresh_raw_amount = int(fresh_holding["amount"]) if fresh_holding else 0
        fresh_available = to_human(fresh_raw_amount)
        if _selling_all:
            # Re-resolve from fresh balance to avoid float round-trip precision loss
            amt = fresh_available
        if amt > fresh_available + 1e-9:
            await ctx.reply_error(
                f"Balance changed since confirmation. You now have **{fresh_available:,.6f} {symbol}** "
                f"but tried to sell **{amt:,.6f}**."
            )
            return

        # ── Token contract enforcement (burn + transfer fee on sell) ────────────
        _sell_contract = await ctx.db.get_token_contract(ctx.guild_id, symbol)
        _sell_params = _sell_contract if isinstance(_sell_contract, dict) else {}
        if isinstance(_sell_params.get("params"), str):
            import json as _json_sell
            _sell_params = _json_sell.loads(_sell_params["params"])
        _builtin_sell_burn = Config.TOKENS.get(symbol, {}).get("burn_rate", 0.0)
        _sell_burn_rate = _sell_params.get("burn_rate", 0.0) or _builtin_sell_burn
        _sell_xfer_fee = _sell_params.get("transfer_fee", 0.0)
        _sell_burned = amt * _sell_burn_rate
        _sell_fee_tokens = amt * _sell_xfer_fee
        # Burned tokens and fee tokens reduce the effective sell amount
        effective_sell_amt = amt - _sell_burned - _sell_fee_tokens
        # Recalculate revenue based on effective sell amount
        if effective_sell_amt < amt:
            revenue = effective_sell_amt * float(price_row["price"])
            net_revenue = revenue - sell_fee

        # ── Execute trade atomically ─────────────────────────────────────────
        _sell_cur_price = float(price_row["price"])
        impact = revenue / Config.PRICE_IMPACT_DIVISOR
        circ_supply_sell = to_human(all_tokens.get(symbol, {}).get("circulating_supply") or 0)
        mkt_cap_sell = _sell_cur_price * circ_supply_sell
        if mkt_cap_sell > 0 and revenue > 0.001 * mkt_cap_sell:
            mc_ratio_sell = revenue / mkt_cap_sell
            mc_mult_sell = min(1.0 + mc_ratio_sell * 2.0, 5.0)
            impact = impact * mc_mult_sell
        impact = min(impact, 0.95)  # never wipe more than 95% of price in one sell
        _eff_price_sell = max(1e-9, _sell_cur_price * (1 - impact))
        eff_revenue = effective_sell_amt * _eff_price_sell
        net_revenue = eff_revenue - sell_fee

        # ── Wealth Bottleneck on the realized USD profit ────────────────
        # Estimate realized gain via avg buy price from the user's recent
        # BUY history; apply the bottleneck only to the positive portion.
        # Loss/breakeven sells (no positive gain) skip the bottleneck.
        from services.bottleneck import (
            apply_bottleneck, realized_sell_gain_raw, CreditKind,
        )
        _sell_qty_raw = abs(int(to_raw(amt)))
        _gain_raw = await realized_sell_gain_raw(
            ctx.db, uid=ctx.author.id, gid=ctx.guild_id, symbol=symbol,
            sell_qty_raw=_sell_qty_raw, sell_price_usd=_eff_price_sell,
        )
        _trade_bn = None
        if _gain_raw > 0:
            _trade_bn = await apply_bottleneck(
                ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
                gross_raw=_gain_raw, kind=CreditKind.TRADE_GAIN,
            )
            # Apply the bottleneck delta to net_revenue (wallet credit):
            # subtract drag, add boost. Both are USD-raw so the unit math
            # works without any further conversion.
            _bn_delta_raw = _trade_bn.boost_wallet_raw - _trade_bn.drag_usd_raw
            net_revenue = float(net_revenue) + (_bn_delta_raw / 10**18)
        async with ctx.db.atomic():
            # Re-read the holding INSIDE the transaction so another command
            # (swap, PvP, validator rewards, etc.) that commits between the
            # pre-confirm read and the update cannot produce a stale
            # -fresh_raw_amount that the DB guard rejects as "insufficient".
            _tx_holding = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, symbol)
            _tx_raw_amount = int(_tx_holding["amount"]) if _tx_holding else 0
            if _selling_all:
                # Settle against whatever is actually there at tx time.
                _sell_delta_raw = -_tx_raw_amount
            else:
                # Cap the deduction at the live balance to absorb any small
                # external debit that happened during the confirm window.
                _sell_delta_raw = -min(to_raw(amt), _tx_raw_amount)
            if _sell_delta_raw == 0:
                await ctx.reply_error(f"You no longer have any **{symbol}** to sell.")
                return
            await ctx.db.update_holding(ctx.author.id, ctx.guild_id, symbol, _sell_delta_raw)
            new_wallet = await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(net_revenue))
            await ctx.db.split_to_community_reserves(ctx.guild_id, "USD", to_raw(sell_fee))
            # Burn tokens: reduce circulating supply. Built-in tokens live
            # in crypto_prices, custom guild tokens in guild_tokens -- mirror
            # services/swap.py and dispatch by token type so built-in burns
            # aren't silently dropped on the floor.
            if _sell_burned > 0:
                if symbol in Config.TOKENS:
                    await ctx.db.update_builtin_circulating_supply(
                        ctx.guild_id, symbol, to_raw(-_sell_burned),
                    )
                else:
                    await ctx.db.update_circulating_supply(
                        ctx.guild_id, symbol, to_raw(-_sell_burned),
                    )
            # Transfer fee tokens go to community reserves
            if _sell_fee_tokens > 0:
                _sell_fee_usd = _sell_fee_tokens * float(price_row["price"])
                await ctx.db.split_to_community_reserves(ctx.guild_id, "USD", to_raw(_sell_fee_usd))
            await ctx.db.update_price(symbol, ctx.guild_id, _eff_price_sell)
            # Extend the current 1-minute candle immediately so the chart moves
            # without a 15s drift-task delay (see buy path for rationale).
            await ctx.db.upsert_candle(
                ctx.guild_id, f"{symbol}USD", _minute_ts(),
                open_=_sell_cur_price,
                high=max(_sell_cur_price, _eff_price_sell),
                low=min(_sell_cur_price, _eff_price_sell),
                close=_eff_price_sell,
                volume_delta=eff_revenue,
            )
            tx_hash = await ctx.db.log_tx(
                ctx.guild_id, ctx.author.id, "SELL",
                symbol_in=symbol, amount_in=to_raw(amt),
                symbol_out="USD", amount_out=to_raw(eff_revenue),
                price_at=_eff_price_sell,
                network="usd",
            )
            await ctx.db.add_trade_volume(ctx.guild_id, f"{symbol}USD", to_raw(eff_revenue))

        # Realign pools + drift-task's candle-open cache for next tick.
        self._last_price[(ctx.guild_id, f"{symbol}USD")] = _eff_price_sell
        await ctx.bot.bus.publish("prices_updated", guild=ctx.guild)

        set_trade_cooldown(ctx.author.id, ctx.guild_id, symbol)
        _record_user_swap_volume(ctx.author.id, ctx.guild_id, _sell_usd_val)
        _sell_vault_net = _VAULT_NET_MAP.get(all_tokens.get(symbol, {}).get("network", ""))
        if _sell_vault_net and eff_revenue > 0:
            try:
                await credit_vault_volume(ctx.db, ctx.guild_id, _sell_vault_net, eff_revenue, bot=ctx.bot)
            except Exception:
                pass  # never let vault update block a trade
        await ctx.bot.bus.publish(
            "trade", guild=ctx.guild, user=ctx.author,
            action="SELL", symbol=symbol, amount=amt,
            price=_eff_price_sell, total=eff_revenue, tx_hash=tx_hash,
        )
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "sell", eff_revenue, symbol=symbol, amount=amt)
        if eff_revenue >= 5000:
            try:
                await ctx.db.log_server_event(
                    ctx.guild_id, ctx.channel.id, ctx.author.id,
                    "big_sell",
                    f"{ctx.author.display_name} sold {amt:,.4f} {symbol} for {fmt_usd(eff_revenue)}",
                    eff_revenue,
                )
            except Exception:
                pass

        _sell_usd_res = sell_fee / 2.0
        _sell_fee_value = f"`${sell_fee:,.2f}`\n↳ ${_sell_usd_res:,.2f} → Vault"
        if _sell_fee_rebate_amt > 0:
            _sell_fee_value += (
                f"\n↳ 🐾 Buddy: -{_sell_fee_rebate_pct*100:.1f}% "
                f"(-${_sell_fee_rebate_amt:,.2f})"
            )
        _sell_eb = (
            card(f"💰 Sold {Config.currency_label(symbol)}", color=C_SELL)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("🪙 Sold",         f"**{amt:.4f} {symbol}**",              True)
            .field("💵 Received",     f"**${net_revenue:,.4f} USD**",          True)
            .field("📈 Fill Price",   f"`${_eff_price_sell:.4f}`\n📊 Slippage: `-{impact*100:.3f}%`", True)
            .field("🏦 Fee",          _sell_fee_value,                        True)
            .field("💰 New Balance",  f"**${to_human(new_wallet):,.2f}**",               True)
        )
        if _sell_burned > 0:
            _sell_eb.field("🔥 Burned", f"{_sell_burned:,.6f} {symbol} ({_sell_burn_rate*100:.1f}%)", True)
        if _sell_fee_tokens > 0:
            _sell_eb.field("📋 Contract Fee", f"{_sell_fee_tokens:,.6f} {symbol} ({_sell_xfer_fee*100:.1f}%)", True)
        result_embed = _sell_eb.build()
        from core.framework.ui import fmt_bottleneck as _fmt_bn
        _bn_foot = _fmt_bn(_trade_bn) if _trade_bn else ""
        _foot_extra = f"slippage: -{impact*100:.3f}%"
        if _bn_foot:
            _foot_extra = f"{_foot_extra}  -  {_bn_foot}"
        set_tx(result_embed, ctx.guild.id, tx_hash, _foot_extra)
        if auto_confirm:
            await ctx.reply(embed=result_embed, mention_author=False)
        else:
            from core.framework.links import sanitize_embed
            sanitize_embed(result_embed)
            await conf_msg.edit(embed=result_embed, view=None)  # type: ignore[reportUnboundVariable]

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade portfolio
    # ══════════════════════════════════════════════════════════════════════════

    @trade.command(name="portfolio", aliases=["port", "holdings"])
    @guild_only
    @no_bots
    @ensure_registered
    async def portfolio(self, ctx: DiscoContext) -> None:
        """Show your crypto holdings and current value."""
        holdings = await ctx.db.get_holdings(ctx.author.id, ctx.guild_id)
        if not holdings:
            await ctx.reply_error_action(
                f"You have no crypto holdings. Use `{ctx.prefix}trade buy SYMBOL amount` to get started.",
                "Buy Crypto",
                "buy",
            )
            return

        # First pass: compute prices and total value for percentage calculations
        all_tokens_cfg = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        holding_data: list[dict] = []
        total_value = 0.0
        for h in holdings:
            symbol = h["symbol"]
            if symbol in _DIRECT_BUY_TOKENS:
                price_row = await ctx.db.get_price(symbol, ctx.guild_id)
                usd_price = float(price_row["price"]) if price_row else 0.0
                price_source = "oracle"
            else:
                usd_price = await self._derive_usd_price(symbol, ctx.guild_id) or 0.0
                if usd_price:
                    price_source = "pool"
                else:
                    price_row = await ctx.db.get_price(symbol, ctx.guild_id)
                    usd_price = float(price_row["price"]) if price_row else 0.0
                    price_source = "oracle"
            amt_h = to_human(h["amount"])
            value = usd_price * amt_h
            total_value += value
            tcfg = all_tokens_cfg.get(symbol, {})
            holding_data.append({
                "symbol": symbol,
                "amount": amt_h,
                "usd_price": usd_price,
                "value": value,
                "price_source": price_source,
                "network": tcfg.get("network") or "Other",
                "emoji": tcfg.get("emoji") or "●",
            })

        # Group by network for dashboard layout
        by_net: dict[str, list[dict]] = {}
        for hd in holding_data:
            by_net.setdefault(hd["network"], []).append(hd)

        user_row = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        usd_bal = to_human(user_row["wallet"]) if user_row else 0.0

        _b = (
            card(
                "💼 Portfolio Dashboard",
                description=(
                    f"**Total Holdings Value: ${total_value:,.4f}**\n"
                    f"💵 USD Balance: **${usd_bal:,.2f}**  ·  "
                    f"📊 {len(holding_data)} asset{'s' if len(holding_data) != 1 else ''} across {len(by_net)} network{'s' if len(by_net) != 1 else ''}"
                ),
                color=C_INFO,
            )
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        )

        for net in sorted(by_net.keys()):
            net_holdings = by_net[net]
            _b.field("\u200b", f"** -  {net}  - **", True)
            for hd in sorted(net_holdings, key=lambda x: x["value"], reverse=True):
                pct_of_port = (hd["value"] / total_value * 100) if total_value > 0 else 0.0
                src_tag = "🏊" if hd["price_source"] == "pool" else "🔮"
                _b.field(
                    f"{hd['emoji']} {hd['symbol']}",
                    (
                        f"`{hd['amount']:,.4f}` · **${hd['value']:,.4f}**\n"
                        f"{src_tag} `${hd['usd_price']:,.4f}` · {pct_of_port:.1f}% of portfolio"
                    ),
                    True,
                )

        embed = (
            _b
            .field("💰 Total Holdings", f"**${total_value:,.4f}**", True)
            .field("💵 USD Cash", f"**${usd_bal:,.2f}**", True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade info (tokeninfo)
    # ══════════════════════════════════════════════════════════════════════════

    @trade.command(name="info", aliases=["ti", "token", "tokeninfo"])
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
            await ctx.reply_error(f"Token **{symbol}** not found. Use `{ctx.prefix}trade prices` to see all tokens.")
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
            pool_val = to_human(pool["reserve_a"]) * pa_usd + to_human(pool["reserve_b"]) * pb_usd
            lp_usd += pool_val
            pair_str = f"{ta}/{tb}"
            if pair_str not in pool_pairs:
                pool_pairs.append(pair_str)

        # Build embed
        color = C_BUY if pct_chg >= 0 else C_SELL
        _b = (
            card(
                f"{emoji} {name}  ({symbol})",
                description=f"💵 **${price:,.6f}**  {arrow} **{pct_chg:+.2f}%**  ·  {network}",
                color=color,
            )
            .field("🌐 Network",   network,   True)
            .field("⚙️ Consensus", consensus, True)
            .field("📈 24h High",  f"`${day_high:,.6f}`", True)
            .field("📉 24h Low",   f"`${day_low:,.6f}`",  True)
        )
        if start_p:
            _b.field("🏁 Start Price", f"`${start_p:,.6f}`", True)
        if daily_vol:
            _b.field("📊 Daily Vol", f"`{daily_vol*100:.1f}%`", True)

        # Supply & market cap
        token_row = all_tokens.get(symbol, {})
        circ_supply = to_human(token_row.get("circulating_supply") or 0)
        # Resolve max supply: guild token → contract params → Config.TOKENS
        cfg_max = to_human((tcfg.get("max_supply") if tcfg else None) or 0)
        max_sup_tok = to_human(token_row.get("max_supply") or 0) or to_human(max_supply or 0) or cfg_max or 0

        if circ_supply > 0:
            mkt_cap = price * circ_supply
            supply_pct = f" ({circ_supply / max_sup_tok * 100:.1f}% of max)" if max_sup_tok > 0 else ""
            _b.field("💎 Market Cap",
                f"**${mkt_cap:,.2f}**",
                True)
            _b.field("🔄 Circulating",
                f"`{circ_supply:,.0f} {symbol}`{supply_pct}",
                True)
            if max_sup_tok > 0:
                _b.field("🔒 Max Supply", f"`{max_sup_tok:,.0f} {symbol}`", True)
        elif max_sup_tok > 0:
            # No circulating data yet  -  still show max supply from config
            _b.field("🔄 Circulating", "*No data yet*", True)
            _b.field("🔒 Max Supply", f"`{max_sup_tok:,.0f} {symbol}`", True)

        # Contract section
        if fee_rate or burn_rate or max_supply:
            contract_lines = []
            if fee_rate:
                contract_lines.append(f"• 💸 Transfer fee: **{fee_rate*100:.2f}%**")
            if burn_rate:
                contract_lines.append(f"• 🔥 Burn rate: **{burn_rate*100:.2f}%**")
            if max_supply:
                contract_lines.append(f"• 🔒 Max supply: **{max_supply:,.0f}**")
            _b.field("⚙️ Contract Rules", "\n".join(contract_lines), False)
        else:
            _b.field("⚙️ Contract", "No rules set  ·  *uncapped, no fees*", False)

        # LP liquidity
        if lp_usd > 0:
            pairs_str = " · ".join(pool_pairs[:4])
            _b.field("💧 LP Liquidity", f"**${lp_usd:,.2f}**\n`{pairs_str}`", False)
        else:
            _b.field("💧 LP Liquidity", "*Not pooled*", False)

        embed = _b.footer("📈 .trade for market overview  ·  ⚙️ .admin contract to set rules").build()
        await ctx.reply(embed=embed, mention_author=False)

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade chart
    # ══════════════════════════════════════════════════════════════════════════

    @trade.command(name="chart", aliases=["c"])
    @guild_only
    async def chart(self, ctx: DiscoContext, pair: str, timeframe: str = "1h", *, args: str = "") -> None:
        """Generate a price chart with technical indicators.

        Usage: $chart PAIR [timeframe] [indicators/flags...]

          Pairs: ARCUSD, MTAUSD, DSCUSD, VTRARC (any pool pair).
          Timeframes: 1m 5m 15m 1h 4h 1d
          Indicators: rsi macd bb vol vwap obv adx stoch atr supertrend
                      psar ichimoku donchian keltner pivots roc wpr cci
                      mfi mom ema20 sma50 ema200 wma50 trend all
          Style flags: wide tall light dark minimal
                       line area candles heikinashi log
          Overlay symbols: compare:MTA  (sentinel overlay normalised to 100)
                           in:ARC       (re-quote price series in ARC terms)

        Examples
            ,chart ARCUSD 4h macd rsi vwap
            ,chart MTAUSD 1d ichimoku supertrend wide
            ,chart DSCUSD 1h compare:MTA compare:ARC wide
            ,chart AAVEUSD 1h all light
            ,chart SUNUSD 4h in:MTA heikinashi
        """
        if not _HAS_PLAYWRIGHT:
            await ctx.reply_error(
                "`playwright` is not installed on this host. Run "
                "`uv pip install --system 'playwright>=1.40.0' && playwright install chromium` "
                "(or rebuild the Docker image) and try again."
            )
            return

        pair = pair.upper()
        timeframe = timeframe.lower()

        if timeframe not in _TIMEFRAMES:
            await ctx.reply_error(
                f"Unknown timeframe `{timeframe}`. Valid: {', '.join(_TIMEFRAMES)}"
            )
            return

        _guild_tokens = await ctx.db.get_guild_tokens(ctx.guild_id)
        _guild_syms = frozenset(
            t["symbol"].upper() for t in _guild_tokens if t.get("symbol")
        )
        tokens = _parse_pair(pair, extra_tokens=_guild_syms)
        if not tokens:
            await ctx.reply_error(
                f"Can't parse pair `{pair}`. Example: `ARCUSD`, `MTAUSD`, "
                "`DSCUSD`, `CATUSD`."
            )
            return
        token_a, token_b = tokens

        if token_b == "USD":
            candle_sym = f"{token_a}USD"
        else:
            _pool_id_ab, ca, cb = ctx.db.make_pool_id(token_a, token_b)
            candle_sym = f"{ca}{cb}"

        tf_sec = _TIMEFRAMES[timeframe]
        need_base = 500 * tf_sec // 60
        since_ts = int(time.time()) - need_base * 60
        raw_candles = await ctx.db.get_candles(
            ctx.guild_id, candle_sym, since_ts, limit=need_base,
        )
        if len(raw_candles) < 2:
            await ctx.reply_error(
                f"Not enough price history for **{pair}** yet. "
                "Prices update every 5 minutes  -  wait a bit and try again."
            )
            return

        agg = _aggregate(raw_candles, tf_sec)
        if len(agg) < 2:
            await ctx.reply_error(
                "Not enough aggregated candles for this timeframe yet."
            )
            return

        layout, compare_syms, quote_in, clean_inds = parse_chart_args(
            args.split(),
            _VALID_TOKENS | _guild_syms,
            primary=token_a,
        )

        # ── Convert the chart to be denominated in another token if the
        # user asked. We divide each OHLC value by the corresponding
        # close of the quote token's USD candles. ────────────────────
        if quote_in and quote_in != "USD" and token_b == "USD":
            quote_raw = await ctx.db.get_candles(
                ctx.guild_id, f"{quote_in}USD", since_ts, limit=need_base,
            )
            quote_agg = _aggregate(quote_raw, tf_sec)
            qmap = {c["ts"]: float(c["close"] or 1.0) for c in quote_agg}
            converted = []
            for c in agg:
                q = qmap.get(c["ts"])
                if not q or q <= 0:
                    continue
                converted.append({
                    "ts": c["ts"],
                    "open":  c["open"]  / q,
                    "high":  c["high"]  / q,
                    "low":   c["low"]   / q,
                    "close": c["close"] / q,
                    "volume": c.get("volume", 0.0),
                })
            if len(converted) >= 2:
                agg = converted
                token_b = quote_in

        # ── Comparison overlays -- fetch the DB candles for each compare
        # symbol and normalise to 100 at the first matching timestamp. ─
        times = [c["ts"] for c in agg]
        comparisons: list[dict] = []
        for sym in compare_syms[:3]:
            cmp_raw = await ctx.db.get_candles(
                ctx.guild_id, f"{sym}USD", since_ts, limit=need_base,
            )
            cmp_agg = _aggregate(cmp_raw, tf_sec)
            if len(cmp_agg) < 2:
                continue
            cmp_map = {c["ts"]: float(c["close"] or 0.0) for c in cmp_agg}
            anchor = None
            pts: list[dict] = []
            for t in times:
                v = cmp_map.get(t)
                if v is None or v <= 0:
                    continue
                if anchor is None:
                    anchor = v
                pts.append({"time": t, "value": 100.0 * v / anchor})
            if pts:
                comparisons.append({"symbol": sym, "points": pts})

        base_norm: list[dict] = []
        if comparisons:
            closes = [c["close"] for c in agg]
            anchor = next((c for c in closes if c), closes[0] if closes else 0.0)
            if anchor:
                base_norm = [
                    {"time": t, "value": 100.0 * c / anchor}
                    for t, c in zip(times, closes) if c
                ]

        async with ctx.typing():
            png_bytes, stats = await build_chart_png(
                agg,
                layout=layout,
                clean_inds=clean_inds,
                tf_seconds=tf_sec,
                pair=f"{token_a}/{token_b}",
                timeframe=timeframe,
                comparisons=comparisons,
                base_norm=base_norm,
                quoted_in=quote_in or token_b,
            )

        footer = build_footer_chips(
            compare_syms=compare_syms,
            quote_in=quote_in,
            layout=layout,
            clean_inds=clean_inds,
        )

        pct = stats["pct_change"]
        delta_arrow = "▲" if pct >= 0 else "▼"
        desc = (
            f"💵 Close `{stats['close']:,.6f}`  "
            f"{delta_arrow} **{pct:+.2f}%**  ·  "
            f"H `{stats['high']:,.4f}`  L `{stats['low']:,.4f}`"
        )
        embed = (
            card(
                f"📊 {token_a}/{token_b} · {timeframe.upper()}",
                description=desc, color=C_CHART_BG,
            )
            .image("attachment://chart.png")
            .footer(footer)
            .build()
        )
        file = discord.File(io.BytesIO(png_bytes), filename="chart.png")
        await ctx.reply(embed=embed, file=file, mention_author=False)

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade swap
    # ══════════════════════════════════════════════════════════════════════════

    # ── swap everything ───────────────────────────────────────────────────

    async def _swap_everything(self, ctx: DiscoContext) -> None:
        """Swap all DeFi tokens to their network's native coin."""
        uid, gid = ctx.author.id, ctx.guild_id
        all_tokens = await ctx.db.get_all_tokens_for_guild(gid)
        all_wallet = await ctx.db.get_all_wallet_holdings(uid, gid)

        # Build list of (symbol, amount, network_short, network_coin, pool_id, ca, cb)
        swappable: list[dict] = []
        for h in all_wallet:
            sym = h["symbol"]
            amt = to_human(h.get("amount", 0) or 0)
            net_short = h.get("network", "")
            if amt <= 0 or not net_short:
                continue
            # Find full network name from token config
            tok_cfg = all_tokens.get(sym, {})
            net_full = tok_cfg.get("network", "")
            net_coin = Config.NETWORK_COINS.get(net_full, "")
            if not net_coin or sym == net_coin:
                continue  # already the network coin
            # Check pool exists
            pool_id, ca, cb = ctx.db.make_pool_id(sym, net_coin)
            pool = await ctx.db.get_pool(pool_id, gid)
            if not pool or float(pool["total_lp"]) <= 0:
                continue
            swappable.append({
                "sym": sym, "amt": amt, "net_short": net_short,
                "net_coin": net_coin, "net_full": net_full,
                "pool_id": pool_id, "ca": ca, "cb": cb,
            })

        if not swappable:
            await ctx.reply_error(
                "No swappable DeFi tokens found.\n"
                "Tokens must have an AMM pool with their network coin."
            )
            return

        # Build preview  -  Liqstone owners get a per-swap fee discount.
        # Each preview line simulates against the running reserves so users
        # see the ACTUAL cumulative slippage when they dump multiple tokens
        # into the same coin pool (previously every line priced off the
        # pristine pre-batch reserves and the last swaps in the chain got
        # a nasty surprise at execute time).
        _lq_discount = await _liqstone_swap_fee_discount(ctx.db, uid, gid)
        _ch_discount = await _chimerastone_swap_fee_discount(ctx.db, uid, gid)
        effective_fee = _apply_liqstone_discount(
            _DEFAULT_SWAP_FEE, _lq_discount + _ch_discount,
        )
        _sim_reserves: dict[str, tuple[float, float]] = {}  # pool_id -> (res_ca, res_cb)
        lines = []
        total_fee_usd = 0.0
        for s in swappable:
            pool = await ctx.db.get_pool(s["pool_id"], gid)
            if s["pool_id"] not in _sim_reserves:
                _sim_reserves[s["pool_id"]] = (
                    to_human(pool["reserve_a"]),
                    to_human(pool["reserve_b"]),
                )
            res_a, res_b = _sim_reserves[s["pool_id"]]
            if s["sym"] == s["ca"]:
                reserve_in, reserve_out = res_a, res_b
            else:
                reserve_in, reserve_out = res_b, res_a
            fee_amt_in = s["amt"] * effective_fee
            ain = s["amt"] - fee_amt_in
            est_out = reserve_out * ain / (reserve_in + ain) if (reserve_in + ain) > 0 else 0
            # Update running simulation so the NEXT swap into this pool
            # sees the drained reserves.
            if s["sym"] == s["ca"]:
                _sim_reserves[s["pool_id"]] = (res_a + s["amt"], res_b - est_out)
            else:
                _sim_reserves[s["pool_id"]] = (res_a - est_out, res_b + s["amt"])
            # Value the fee in USD at current oracle for the transparency line.
            _pin = await ctx.db.get_price(s["sym"], gid)
            total_fee_usd += fee_amt_in * (float(_pin["price"]) if _pin else 0.0)
            emoji = all_tokens.get(s["sym"], {}).get("emoji", "")
            lines.append(
                f"{emoji}**{s['sym']}**: {s['amt']:,.4f} -> ~{est_out:,.4f} {s['net_coin']}"
            )

        _fee_line = (
            f"*Liqstone discount active: effective fee `{effective_fee*100:.3g}%` "
            f"(-`{_lq_discount*100:.3g}%`).*"
            if _lq_discount > 0
            else f"*{effective_fee*100:.3g}% swap fee per trade, paid to LPs. "
                 f"Price impact already baked into the estimates above.*"
        )
        desc = (
            "Swapping **all** DeFi tokens to their network coin:\n\n"
            + "\n".join(lines)
            + f"\n\n**Total swap fee:** ~${total_fee_usd:,.2f}\n"
            + _fee_line
        )
        confirm_embed = (
            card("🔄 Swap Everything", description=desc, color=C_AMBER)
            .footer("Confirm within 30 seconds")
            .build()
        )
        view = ConfirmView(ctx.author.id, timeout=30)
        msg = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
        view.message = msg
        confirmed = await view.wait_result()
        if confirmed is not True:
            try:
                await msg.edit(embed=card("🔄 Swap Cancelled", color=C_NEUTRAL).build(), view=None)
            except Exception:
                pass
            return

        # Execute swaps
        result_lines = []
        total_received: dict[str, float] = {}  # net_coin -> total
        for s in swappable:
            try:
                # Re-check balance
                wh = await ctx.db.get_wallet_holding(uid, gid, s["net_short"], s["sym"])
                actual = to_human(wh["amount"]) if wh else 0.0
                if actual <= 0:
                    continue
                amt = min(s["amt"], actual)

                # Recalculate with current pool state
                pool = await ctx.db.get_pool(s["pool_id"], gid)
                if not pool or pool["total_lp"] <= 0:
                    result_lines.append(f"**{s['sym']}**: skipped (pool empty)")
                    continue

                if s["sym"] == s["ca"]:
                    reserve_in = to_human(pool["reserve_a"])
                    reserve_out = to_human(pool["reserve_b"])
                else:
                    reserve_in = to_human(pool["reserve_b"])
                    reserve_out = to_human(pool["reserve_a"])

                # Use the discounted fee already resolved above for preview  -
                # keeps preview and execution in sync for Liqstone holders.
                fee = effective_fee
                ain_with_fee = amt * (1 - fee)
                amount_out = reserve_out * ain_with_fee / (reserve_in + ain_with_fee)

                if amount_out <= 0:
                    result_lines.append(f"**{s['sym']}**: skipped (zero output)")
                    continue

                # Execute atomically
                async with ctx.db.atomic():
                    await ctx.db.update_wallet_holding(uid, gid, s["net_short"], s["sym"], to_raw(-amt))
                    await ctx.db.update_wallet_holding(uid, gid, s["net_short"], s["net_coin"], to_raw(amount_out))

                    # Update pool reserves
                    if s["sym"] == s["ca"]:
                        await ctx.db.update_pool_reserves(
                            s["pool_id"], gid,
                            to_raw(reserve_in + amt), to_raw(reserve_out - amount_out), pool["total_lp"]
                        )
                    else:
                        await ctx.db.update_pool_reserves(
                            s["pool_id"], gid,
                            to_raw(reserve_out - amount_out), to_raw(reserve_in + amt), pool["total_lp"]
                        )

                # Log tx
                net_prefix = s["net_short"]
                try:
                    await ctx.db.log_tx(
                        gid, uid, "SWAP",
                        symbol_in=s["sym"], amount_in=to_raw(amt),
                        symbol_out=s["net_coin"], amount_out=to_raw(amount_out),
                        network=net_prefix,
                    )
                except Exception:
                    pass

                # Vault volume credit
                _pin = await ctx.db.get_price(s["sym"], gid)
                _vol_usd = amt * (float(_pin["price"]) if _pin else 0.0)
                if _vol_usd > 0:
                    try:
                        await ctx.db.add_trade_volume(gid, f"{s['sym']}USD", to_raw(_vol_usd))
                        _vnet = _VAULT_NET_MAP.get(s["net_full"])
                        if _vnet:
                            await credit_vault_volume(ctx.db, gid, _vnet, _vol_usd, bot=ctx.bot)
                    except Exception:
                        pass

                total_received[s["net_coin"]] = total_received.get(s["net_coin"], 0.0) + amount_out
                result_lines.append(f"**{s['sym']}**: {amt:,.4f} -> {amount_out:,.4f} {s['net_coin']}")
            except Exception as exc:
                result_lines.append(f"**{s['sym']}**: failed ({str(exc)[:50]})")

        # Summary
        summary_parts = []
        for coin, total in total_received.items():
            emoji = all_tokens.get(coin, {}).get("emoji", "")
            summary_parts.append(f"{emoji}**{total:,.4f} {coin}**")

        result_desc = "\n".join(result_lines) or "Nothing swapped."
        if summary_parts:
            result_desc += "\n\n**Total received:** " + " + ".join(summary_parts)

        result_embed = (
            card("🔄 Swap Everything - Complete", color=C_SUCCESS)
            .description(result_desc)
            .build()
        )
        try:
            await msg.edit(embed=result_embed, view=None)
        except Exception:
            await ctx.reply(embed=result_embed, mention_author=False)

    # ── swap ───────────────────────────────────────────────────────────────

    @trade.command(name="swap")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def swap(self, ctx: DiscoContext, token_in: str, token_out: str = "", amount_in: str = "", *, flags: str = "") -> None:
        """Swap tokens through an AMM pool. Usage: .swap TOKEN_IN TOKEN_OUT amount
        amount can be a number, 'all', or '$<usd>' (e.g. '$100').  Flags: yes to skip confirmation.
        Swaps default to intra-network -- cross-network pairs use .sell then .buy
        unless a player at a job rank with can_create_pool has deployed a
        direct pool via 'trade pool create', in which case that pool is used.
        SUN cannot be swapped - use 'trade buy'/'trade sell' for SUN.
        Use 'trade swap everything' to swap all DeFi tokens to their network coin."""
        if token_in.lower() == "everything" and not token_out:
            await self._swap_everything(ctx)
            return
        if not token_out or not amount_in:
            await ctx.reply_error(f"Usage: `{ctx.prefix}trade swap <TOKEN_IN> <TOKEN_OUT> <amount|all>`\nOr `{ctx.prefix}trade swap everything`")
            return
        token_in, token_out = token_in.upper(), token_out.upper()
        fl = flags.lower()
        auto_confirm = "yes" in fl or "-y" in fl

        # Parse min flag for slippage protection
        min_amount_out = 0.0
        min_match = re.search(r'min\s+([\d.]+)', fl) if flags else None
        if min_match:
            min_amount_out = float(min_match.group(1))

        # Cross-network restriction
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        net_in  = all_tokens.get(token_in,  {}).get("network", "")
        net_out = all_tokens.get(token_out, {}).get("network", "")

        # SUN swaps now route through the SUN/USD fallback pool seeded by
        # seed_pools (Sun Network has no stablecoin, so USD is the pairing).
        # Swap routing falls through to the standard AMM path below.

        # ── Earn-only tokens: MOON has explicit swap carve-outs ──────────────
        # MOON is emitted by the Lunar Mint. Swaps in either direction are
        # blocked unless the pair is on the carve-out list mirrored from
        # services/swap.py:
        #
        #   * mMTA / mSUN (Config.MOON_SWAPPABLE_TOKENS) -- bidirectional
        #   * a player-deployed token whose contract was minted with
        #     ``moon_swappable: True`` (set by ``token deploy``) -- bidirectional
        #   * a Moon Network group token -- one-way (MOON -> GROUP only) so
        #     the Lunar Mint stays the single legitimate path for minting
        #     MOON against a group token.
        #
        # LURE / REEL never appear on the carve-out so the fishing firewall
        # is unaffected.
        def _is_moon_group_token(sym: str) -> bool:
            meta = all_tokens.get(sym, {})
            return (
                meta.get("token_type") == "group"
                and meta.get("network") == "Moon Network"
            )

        moon_pair_other = ""
        if "MOON" in (token_in, token_out):
            moon_pair_other = token_out if token_in == "MOON" else token_in
        moon_bidirectional = bool(moon_pair_other) and await _is_moon_swappable_pair(
            ctx.db, ctx.guild_id, "MOON", moon_pair_other, all_tokens=all_tokens,
        )

        # BUD has its own bidirectional carve-out list (REEL / RUNE / MOON /
        # FREN). Same shape as the MOON branch -- the EARN_ONLY firewall
        # blocks every other path so the Buddy Market + Buddy Shop stay
        # the only legitimate way to acquire BUD outside FREN stake-yield.
        bud_pair_other = ""
        if "BUD" in (token_in, token_out):
            bud_pair_other = token_out if token_in == "BUD" else token_in
        bud_bidirectional = bool(bud_pair_other) and await _is_bud_swappable_pair(
            ctx.db, ctx.guild_id, "BUD", bud_pair_other, all_tokens=all_tokens,
        )

        if token_out in Config.EARN_ONLY_TOKENS:
            if token_out == "MOON" and moon_bidirectional:
                pass  # MMTA / MSUN / flagged deployed tokens may swap into MOON
            elif token_out == "BUD" and bud_bidirectional:
                pass  # FREN / REEL / RUNE / MOON / HRV may swap into BUD
            else:
                hint = ""
                if token_out == "MOON":
                    hint = (
                        f"\nThe only ways to get MOON are: stake a group token "
                        f"into the Lunar Mint (`{ctx.prefix}moon stake <GROUP_TOKEN> "
                        f"<amount>`), or swap from mMTA / mSUN / a moon-swappable "
                        f"deployed token."
                    )
                elif token_out == "BUD":
                    hint = (
                        f"\nBUD can be acquired by FREN stake-yield (`{ctx.prefix}buddy "
                        f"stake fren <amount>`), by burn-swapping FREN / REEL / RUNE / "
                        f"MOON / HRV against BUD, or via the auto-swap on the Buddy Market."
                    )
                await ctx.reply_error(
                    f"**{token_out}** is an earn-only token and cannot be acquired "
                    f"through this pair.{hint}"
                )
                return
        if (
            token_in in Config.EARN_ONLY_TOKENS
            and not _is_moon_group_token(token_out)
            and not moon_bidirectional
            and not bud_bidirectional
        ):
            hint = ""
            if token_in == "MOON":
                hint = (
                    f"\nMOON can only be swapped OUT into a Moon Network group "
                    f"token (CAT, COOK, FEM, ...), mMTA, mSUN, or a moon-swappable "
                    f"deployed token. It cannot be swapped for USD, stablecoins, "
                    f"or unrelated network coins."
                )
            elif token_in == "BUD":
                hint = (
                    f"\nBUD can only be swapped OUT into REEL, RUNE, MOON, or "
                    f"FREN. Use `{ctx.prefix}buddy cashout <amount>` to convert "
                    f"BUD -> USD via the Buddy Market auto-swap (slippage applies)."
                )
            elif token_in == "FREN":
                hint = (
                    f"\nFREN can only be swapped OUT into BUD. Stake FREN with "
                    f"`{ctx.prefix}buddy stake fren <amount>` to earn BUD passively."
                )
            await ctx.reply_error(
                f"**{token_in}** is an earn-only token and cannot be swapped "
                f"through this pair.{hint}"
            )
            return

        # ── Admin halts ───────────────────────────────────────────────────────
        for _sym, _net_name in ((token_in, net_in), (token_out, net_out)):
            if await ctx.db.is_token_disabled(ctx.guild_id, _sym):
                await ctx.reply_error(f"**{_sym}** trading is currently disabled by an admin.")
                return
            _nk = _NET_SHORT.get(_net_name, "")
            if _nk and await ctx.db.is_network_halted(ctx.guild_id, _nk):
                await ctx.reply_error(f"The **{_net_name}** is currently halted by an admin. Transactions are paused.")
                return

        # Cross-network swaps are blocked for unrelated tokens, but several
        # carve-outs apply:
        #   * vault-pair pools (group token / mining-chain coin, e.g.
        #     COOK/MTA or FEM/SUN) are legitimately cross-network -- the
        #     group token lives on the bridged "Moon Network" while the
        #     paired coin lives on its own PoW chain;
        #   * MOON bidirectional carve-outs (MMTA, MSUN);
        #   * any pair that has an explicitly-deployed pool. A player who
        #     reaches a job rank with ``can_create_pool`` can deploy a
        #     cross-network pool via ``trade pool create``; once that
        #     pool exists it IS the authorization, so the swap should
        #     transact through it instead of refusing.
        _tt_in  = all_tokens.get(token_in,  {}).get("token_type", "")
        _tt_out = all_tokens.get(token_out, {}).get("token_type", "")
        _group_involved = (_tt_in == "group") or (_tt_out == "group")

        pool_id, ca, cb = ctx.db.make_pool_id(token_in, token_out)
        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)

        if (
            net_in and net_out and net_in != net_out
            and "USD" not in (token_in, token_out)
            and not _group_involved
            and not moon_bidirectional
            and not pool
        ):
            await ctx.reply_error(
                f"Cross-network swaps are not supported.\n"
                f"**{token_in}** is on {net_in}, **{token_out}** is on {net_out}.\n"
                f"Use `{ctx.prefix}trade sell {token_in} all` → `{ctx.prefix}trade buy {token_out}` to switch networks.\n"
                f"If you reach a job rank that can deploy pools, "
                f"`{ctx.prefix}trade pool create {token_in} {token_out}` opens a direct venue."
            )
            return

        # Stablecoin ↔ USD swaps not allowed  -  use .buy / .sell instead
        stablecoins = set(Config.NETWORK_STABLECOIN.values())
        if (token_in in stablecoins and token_out == "USD") or \
           (token_out in stablecoins and token_in == "USD"):
            direction = f"{ctx.prefix}trade sell {token_in} all" if token_out == "USD" else f"{ctx.prefix}trade buy {token_out}"
            await ctx.reply_error(
                f"Stablecoins can't be swapped directly with USD  -  the pool isn't for that.\n"
                f"Use **`{direction}`** instead."
            )
            return

        # Cross-stablecoin swaps not allowed
        if token_in in stablecoins and token_out in stablecoins:
            await ctx.reply_error(
                f"Swapping between stablecoins is not supported.\n"
                f"Use `{ctx.prefix}trade sell {token_in}` and `{ctx.prefix}trade buy {token_out}` instead."
            )
            return

        if not pool:
            # Smart hint: if the user tried to swap a native PoW coin
            # directly against a group token (the most common 404 now that
            # group tokens pair against MMTA / MSUN instead of MTA / SUN),
            # tell them to wrap / unwrap -- not to go reshuffle their
            # mining group.
            from constants.moons import (
                WRAPPED_FOR_NATIVE as _WFN,
                NATIVE_FOR_WRAPPED as _NFW,
            )
            _meta_in  = all_tokens.get(token_in,  {})
            _meta_out = all_tokens.get(token_out, {})
            _in_is_group  = _meta_in.get("token_type")  == "group"
            _out_is_group = _meta_out.get("token_type") == "group"

            if token_in in _WFN and _out_is_group:
                wrapped = _WFN[token_in]
                await ctx.reply_error(
                    f"No direct **{token_in}/{token_out}** pool exists. "
                    f"{token_out} trades against **{wrapped}** on Moon Network.\n"
                    f"Wrap first, then swap:\n"
                    f"> `{ctx.prefix}moon wrap {token_in.lower()} <amount>`\n"
                    f"> `{ctx.prefix}trade swap {wrapped} {token_out} <amount>`"
                )
                return
            if _in_is_group and token_out in _WFN:
                wrapped = _WFN[token_out]
                await ctx.reply_error(
                    f"No direct **{token_in}/{token_out}** pool exists. "
                    f"{token_in} trades against **{wrapped}**; unwrap after to "
                    f"get native {token_out}.\n"
                    f"> `{ctx.prefix}trade swap {token_in} {wrapped} <amount>`\n"
                    f"> `{ctx.prefix}moon unwrap {wrapped.lower()} <amount>`"
                )
                return
            if token_in in _NFW and _out_is_group:
                # Already using the wrapper but the specific pool is
                # somehow missing -- advise the pool listing rather than
                # unwrapping.
                pass

            await ctx.reply_error(
                f"No pool for **{token_in}/{token_out}** pair.\n"
                f"Use `{ctx.prefix}trade pool list` to browse available pools."
            )
            return
        if pool.get("vault_locked"):
            await ctx.reply_error(
                f"The **{token_in}/{token_out}** pool is a vault-locked group token pool "
                f"and cannot be traded."
            )
            return

        # Handle 'all' amount  -  gas is paid in network coin so "all" sends the full token_in balance
        _is_all = amount_in.lower() in {"all", "everything", "max", "full", "entire", "total"}
        _cached_price_a_row = None  # may be populated during $<usd> parsing to avoid duplicate DB fetch
        # Track the raw balance so "all" paths compare raw-int to raw-int and
        # never hit the "have 100 need 100" float round-trip bug.
        _all_balance_raw = 0
        if _is_all:
            if token_in == "USD":
                fresh = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
                _all_balance_raw = int(fresh["wallet"]) if fresh else 0
            else:
                _swap_net_short = _NET_SHORT.get(net_in, "")
                if _swap_net_short:
                    _wh = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, _swap_net_short, token_in)
                else:
                    _wh = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, token_in)
                _all_balance_raw = int(_wh["amount"]) if _wh else 0
            amount_val = to_human(_all_balance_raw)
        else:
            # Check for $-prefixed USD amount (e.g. "$100" = swap $100 worth)
            amount_str = str(amount_in)
            _usd_mode = amount_str.startswith("$")
            _raw = amount_str.lstrip("$")
            try:
                _parsed = float(_raw)
            except ValueError:
                await ctx.reply_error("Amount must be a number, `all`, or `$<usd>` (e.g. `$100`).")
                return
            if _usd_mode:
                # Convert USD amount to token_in quantity
                if token_in == "USD":
                    amount_val = _parsed
                    _cached_price_a_row = None  # no token price needed for USD→USD
                else:
                    _cached_price_a_row = await ctx.db.get_price(token_in, ctx.guild_id)
                    if not _cached_price_a_row or _cached_price_a_row["price"] <= 0:
                        await ctx.reply_error(f"Price data unavailable for **{token_in}**.")
                        return
                    amount_val = _parsed / float(_cached_price_a_row["price"])
            else:
                amount_val = _parsed
                _cached_price_a_row = None  # will be fetched below

        if amount_val <= 0:
            await ctx.reply_error("Amount must be positive.")
            return
        amount_in_float = amount_val

        if token_in == ca:
            reserve_in  = to_human(pool["reserve_a"])
            reserve_out = to_human(pool["reserve_b"])
        else:
            reserve_in  = to_human(pool["reserve_b"])
            reserve_out = to_human(pool["reserve_a"])

        if reserve_in <= 0 or reserve_out <= 0:
            await ctx.reply_error("Pool has no liquidity.")
            return

        # Dynamic swap fraction based on pool depth
        # Reuse price_row already fetched during $<usd> amount parsing when available
        price_a_row = _cached_price_a_row if _cached_price_a_row is not None else await ctx.db.get_price(token_in, ctx.guild_id)
        price_b_row = await ctx.db.get_price(token_out, ctx.guild_id)
        _p_in = float(price_a_row["price"]) if price_a_row else 0.0
        _p_out = float(price_b_row["price"]) if price_b_row else 0.0
        pool_tvl = reserve_in * _p_in + reserve_out * _p_out
        if pool_tvl < Config.LOW_LIQUIDITY_THRESHOLD:
            effective_max_fraction = Config.LOW_LIQUIDITY_SWAP_FRACTION
        else:
            effective_max_fraction = Config.MAX_SWAP_FRACTION
        max_in = reserve_in * effective_max_fraction

        # Per-user hourly volume limit
        swap_usd_value = amount_in_float * _p_in if _p_in > 0 else amount_in_float

        # ── Micro-swap anti-exploit ───────────────────────────────────────────
        # Validators can exploit the lockstone XP system by spamming tiny swaps
        # to artificially inflate the mempool, creating more blocks to confirm
        # and earn XP per block. Enforce a minimum swap value and slash any
        # registered validator caught doing it.
        # The `> 0` guard skips tokens with no price data (_p_in=0) so missing
        # price data doesn't incorrectly trigger the micro-swap rejection.
        if 0 < swap_usd_value < Config.MICRO_SWAP_MIN_USD:
            # Check if the user is a registered PoS validator for this network
            _swap_network = net_in or net_out
            if _swap_network:
                _validator = await ctx.db.get_pos_validator(ctx.author.id, ctx.guild_id, _swap_network)
                if _validator and _validator.get("is_active"):
                    # Slash the validator for attempting to exploit micro-swaps
                    _slash = await ctx.db.slash_pos_validator(
                        ctx.author.id, ctx.guild_id, _swap_network,
                        Config.MICRO_SWAP_VALIDATOR_SLASH_RATE,
                    )
                    _slashed_amt = _slash.get("slashed_amount", 0)
                    _deactivated = _slash.get("deactivated", False)
                    from constants.validators import MAX_SLASH_COUNT
                    _slash_count  = _slash.get("slash_count", MAX_SLASH_COUNT)
                    _warn = (
                        f"⚠️ **Validator exploit detected**  -  micro-swaps (< ${Config.MICRO_SWAP_MIN_USD:,.2f}) "
                        f"are prohibited for validators.\n"
                        f"Your **{_swap_network}** validator was slashed **{Config.MICRO_SWAP_VALIDATOR_SLASH_RATE:.0%}** "
                        f"({_slashed_amt:,.4f} tokens burned). Slash #{_slash_count}/{MAX_SLASH_COUNT}."
                    )
                    if _deactivated:
                        _warn += f"\n🚫 Validator **deactivated** after {MAX_SLASH_COUNT} slashes."
                        # Refund all delegators  -  mirror the rejection path in validators.py
                        _dn = _NET_SHORT.get(_swap_network, "")
                        _deleg_rows = await ctx.db.wipe_delegations_for_validator(
                            ctx.author.id, ctx.guild_id, _swap_network
                        )
                        for _d in _deleg_rows:
                            if _dn:
                                await ctx.db.update_wallet_holding(
                                    _d["delegator_id"], ctx.guild_id, _dn,
                                    _d["token"], _d["amount"]
                                )
                            else:
                                await ctx.db.update_holding(
                                    _d["delegator_id"], ctx.guild_id,
                                    _d["token"], _d["amount"]
                                )
                            # DM delegator
                            _del_member = ctx.guild.get_member(_d["delegator_id"])
                            if _del_member:
                                try:
                                    _del_embed = (
                                        card("⛔ Validator Deactivated  -  Delegation Refunded", color=C_WARNING)
                                        .field("Network",  _swap_network,                              True)
                                        .field("Refunded", f"{_d['amount']:,.6f} {_d['token']}",       True)
                                        .field("Reason",   f"Validator auto-deactivated after {_slash_count} slashes (micro-swap exploit).", False)
                                        .footer("Your funds have been returned to your wallet.")
                                        .build()
                                    )
                                    await _del_member.send(embed=_del_embed)
                                except discord.HTTPException:
                                    pass
                        # Publish bus event so trades.py sends the DM to validator
                        await ctx.bot.bus.publish(
                            "pos_validator_slashed",
                            guild=ctx.guild,
                            validator_user_id=ctx.author.id,
                            network=_swap_network,
                            slash_result=_slash,
                            reason="micro_swap_exploit",
                            action_type="swap",
                        )
                    await ctx.reply_error(_warn)
                    return
            await ctx.reply_error(
                f"Swap amount is too small  -  minimum is **${Config.MICRO_SWAP_MIN_USD:,.2f}** USD equivalent "
                f"to prevent mempool spam. Your swap is worth ~**${swap_usd_value:,.4f}**."
            )
            return

        allowed, remaining = _check_user_swap_volume(ctx.author.id, ctx.guild_id, swap_usd_value)
        if not allowed:
            await ctx.reply_error(
                f"Hourly swap volume limit reached. Remaining: **`{fmt_usd(remaining)}`**. "
                f"Limit: `{fmt_usd(Config.USER_SWAP_HOURLY_LIMIT_USD)}`/hour."
            )
            return

        # Per-pool swap cooldown: prevents the spam-max-swap exploit where a
        # user repeatedly fires `swap mta tok max` because each call only
        # checks N% of CURRENT reserves and the loop drains far more than
        # the intended single-swap limit. Reserve atomically (check + record
        # under a lock) so two parallel commands cannot both pass before
        # either records.  Each early-return path below releases the slot
        # via _cancel_pool_swap_reservation so an aborted swap doesn't lock
        # the player out for the full cooldown.
        _cd_ok, _cd_left = await _reserve_pool_swap(ctx.author.id, ctx.guild_id, pool_id)
        if not _cd_ok:
            await ctx.reply_cooldown(_cd_left)
            return

        if amount_in_float > max_in:
            _cancel_pool_swap_reservation(ctx.author.id, ctx.guild_id, pool_id)
            await ctx.reply_error(
                f"Swap too large. Maximum is **`{fmt_token(max_in, token_in)}`** "
                f"({effective_max_fraction*100:.0f}% of pool depth). Split into smaller swaps."
            )
            return

        # Compute output before confirmation so we can show preview.
        # Liqstone owners get -0.1% swap fee per level (see items_config.py).
        # Chimerastone owners stack another -0.1% per level on top -- both
        # sum into one discount which is then capped at 90% of base fee
        # by apply_liqstone_discount so the effective fee never zeros.
        _lq_discount = await _liqstone_swap_fee_discount(ctx.db, ctx.author.id, ctx.guild_id)
        _ch_discount = await _chimerastone_swap_fee_discount(ctx.db, ctx.author.id, ctx.guild_id)
        fee = _apply_liqstone_discount(_DEFAULT_SWAP_FEE, _lq_discount + _ch_discount)
        amount_in_with_fee = amount_in_float * (1 - fee)
        amount_out = reserve_out * amount_in_with_fee / (reserve_in + amount_in_with_fee)

        spot_before  = reserve_out / reserve_in
        exec_price   = amount_out / amount_in_float if amount_in_float > 0 else 0.0
        price_impact = max(0.0, (spot_before - exec_price) / spot_before) if spot_before > 0 else 0.0

        # Slippage protection (--min flag)
        if min_amount_out > 0 and amount_out < min_amount_out:
            _cancel_pool_swap_reservation(ctx.author.id, ctx.guild_id, pool_id)
            await ctx.reply_error(
                f"Slippage protection: output `{fmt_token(amount_out, token_out)}` "
                f"is below your minimum `{fmt_token(min_amount_out, token_out)}`."
            )
            return

        # USD swaps are always instant (off-chain leg). Only token↔token swaps are
        # queued for validator processing when validators are active on the network.
        use_mempool = False
        gas_fee = 0.0
        gas_coin = ""
        gas_emoji = ""
        gas_price = "medium"
        platform_fee = 0.0
        total_gas_cost = 0.0
        swap_network = ""
        if "USD" not in (token_in, token_out) and (net_in or net_out):
            swap_network = net_in or net_out
            all_v = await ctx.db.get_pos_validators_for_network(ctx.guild_id, swap_network)
            active_validators = [v for v in all_v if v["is_active"]]
            # Sun Network uses PoW miners instead of PoS validators
            has_pow_miners = False
            if swap_network == "Sun Network" and not active_validators:
                all_rigs = await ctx.db.get_all_guild_rigs(ctx.guild_id)
                has_pow_miners = any(r["quantity"] > 0 for r in all_rigs)
            if active_validators or has_pow_miners:
                use_mempool = True
                # Calculate gas fee
                gas_price = "medium"
                flags_lower = flags.lower()
                if "gas high" in flags_lower or "high" in flags_lower:
                    gas_price = "high"
                elif "gas low" in flags_lower or "low" in flags_lower:
                    gas_price = "low"
                from cogs.validators import gas_fee_for_network
                gas_coin, gas_fee = await gas_fee_for_network(ctx.db, ctx.guild_id, "swap", gas_price, swap_network)
                # Apex Mastery: Slipstream Trades (utility.gas_cut)
                # trims the per-swap gas fee.
                try:
                    from services import mastery as _mastery_g
                    _mp = await _mastery_g.passives(
                        ctx.db, ctx.author.id, ctx.guild_id,
                    )
                    _g_cut = float(_mp.get("utility.gas_cut") or 0.0)
                    if _g_cut > 0 and gas_fee > 0:
                        gas_fee = gas_fee * max(0.0, 1.0 - _g_cut)
                except Exception:
                    pass
                gas_cfg = Config.TOKENS.get(gas_coin, {})
                gas_emoji = gas_cfg.get("emoji", "●")
                # Platform fee = % of swap value in gas coin terms
                if gas_coin == token_out:
                    _swap_val_gas = amount_out
                elif gas_coin == token_in:
                    _swap_val_gas = amount_in_float
                else:
                    # Convert via USD prices
                    _gc_row = await ctx.db.get_price(gas_coin, ctx.guild_id)
                    _gc_price = float(_gc_row["price"]) if _gc_row else 0.0
                    _swap_val_gas = (swap_usd_value / _gc_price) if _gc_price > 0 else amount_out
                platform_fee = _swap_val_gas * _SWAP_PLATFORM_FEE_PCT
                total_gas_cost = gas_fee + platform_fee
                # Check gas balance
                _swap_gas_net = _NET_SHORT.get(swap_network, "")
                if _swap_gas_net:
                    gas_h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, _swap_gas_net, gas_coin)
                else:
                    gas_h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, gas_coin)
                gas_balance = to_human(gas_h["amount"]) if gas_h else 0.0
                if gas_balance < total_gas_cost:
                    _cancel_pool_swap_reservation(ctx.author.id, ctx.guild_id, pool_id)
                    await ctx.reply_error(
                        f"Need **`{fmt_gas(total_gas_cost, gas_coin, gas_emoji)}`** for gas + platform fees.\n"
                        f"Gas: `{fmt_gas(gas_fee, gas_coin, gas_emoji)}`, Platform: `{fmt_gas(platform_fee, gas_coin, gas_emoji)}`\n"
                        f"Your balance: **`{fmt_gas(gas_balance, gas_coin, gas_emoji)}`**"
                    )
                    return

                # CRITICAL: When user said "all" and the gas coin is the same
                # token being swapped, reduce swap amount to leave room for gas.
                # Otherwise the full balance + gas fee would overdraw the wallet.
                if _is_all and gas_coin == token_in and total_gas_cost > 0:
                    amount_in_float -= total_gas_cost
                    if amount_in_float <= 0:
                        _cancel_pool_swap_reservation(ctx.author.id, ctx.guild_id, pool_id)
                        await ctx.reply_error(
                            f"Your entire **{token_in}** balance would be consumed by gas fees "
                            f"(`{fmt_gas(total_gas_cost, gas_coin, gas_emoji)}`). Nothing left to swap."
                        )
                        return
                    # Recalculate swap output with reduced input
                    amount_after_fee = amount_in_float * (1.0 - fee)
                    amount_out = (reserve_out * amount_after_fee) / (reserve_in + amount_after_fee)
                    price_impact = amount_in_float / (reserve_in + amount_in_float)

        # Confirmation view
        conf_msg = None
        if not auto_confirm:
            _swap_banner, _swap_color_override = slippage_banner(price_impact)
            desc = (
                f"{_swap_banner}"
                f"Send **`{fmt_token(amount_in_float, token_in)}`**\n"
                f"Receive ≈ **`{fmt_token(amount_out, token_out)}`**\n"
                f"Rate: 1 {token_in} = {fmt_token(amount_out/amount_in_float, token_out)}\n"
                f"📊 **Price impact:** `{fmt_pct(-price_impact*100)}`  |  "
                f"Pool fee: {fee*100:.1f}% (`{fmt_token(amount_in_float*fee, token_in)}`)"
            )
            if total_gas_cost > 0:
                _vault_cut = platform_fee / 2.0
                desc += (
                    f"\n**Fees:** {gas_emoji}{gas_coin}"
                    f"\n• Total: **`{fmt_gas(total_gas_cost, gas_coin, gas_emoji)}`**"
                    f"\n• Gas: `{fmt_gas(gas_fee, gas_coin, gas_emoji)}`"
                    f"\n• Platform: `{fmt_gas(platform_fee, gas_coin, gas_emoji)}`"
                    f"\n• 🏛️ Vault: `{fmt_gas(_vault_cut, gas_coin, gas_emoji)}`"
                )
            desc += f"\n\nExpires {fmt_ts(int(time.time() + 30))}  ·  Use `yes` to skip."
            conf_embed = (
                card(
                    "🔄 Confirm Swap",
                    description=desc,
                    color=_swap_color_override if _swap_color_override is not None else C_AMBER,
                )
                .build()
            )
            view = ConfirmTradeView(ctx.author.id)
            conf_msg = await ctx.reply(embed=conf_embed, view=view, mention_author=False)
            await view.wait()
            if not view.confirmed:
                _cancel_pool_swap_reservation(ctx.author.id, ctx.guild_id, pool_id)
                await conf_msg.edit(
                    embed=card("", description="Swap cancelled.", color=C_NEUTRAL).build(),
                    view=None,
                )
                return

        if use_mempool:
            # Automatic 2% slippage tolerance for mempool swaps if user didn't specify --min
            if min_amount_out <= 0:
                min_amount_out = amount_out * 0.98

            _mp_net = _NET_SHORT.get(swap_network, "")
            # token_in's own network may differ from the swap/gas network on
            # vault-pair pools (e.g. CAT/MTA: CAT lives on Moon Network but
            # the gas + mempool run on Moneta Chain). Lock the deposit
            # from token_in's own wallet, not the gas chain's.
            #
            # Built-in safety: for built-in tokens (MOON, mMTA, mSUN, etc.)
            # the wallet network is anchored to ``Config.TOKENS[sym].network``
            # so a corrupted or missing ``net_in`` lookup never debits from
            # the wrong wallet. This was a real footgun on TOKEN/MOON swaps:
            # if ``net_in`` ever slipped to the deployed-token's network the
            # MOON debit would query ``wallet_holdings(network='arc', symbol='MOON')``
            # and report "0 MOON in your DeFi wallet" even though the user
            # had a thousand MOON sitting on Moon Network.
            _tok_in_net = _resolve_token_wallet_net(token_in, net_in) or _mp_net
            # Lock token_in from sender now
            if _tok_in_net:
                h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, _tok_in_net, token_in)
            else:
                h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, token_in)
            # Raw-integer balance check: "all" passed float round-tripping that
            # could produce a sub-LSB shortfall ("have 100 need 100"). Compare
            # raw -> raw and treat "all" as "entire raw balance minus gas".
            h_raw = int(h["amount"]) if h else 0
            gas_cost_raw = to_raw(total_gas_cost) if gas_coin == token_in else 0
            if _is_all:
                amount_in_raw = max(0, h_raw - gas_cost_raw)
            else:
                amount_in_raw = to_raw(amount_in_float)
            needed_raw = amount_in_raw + gas_cost_raw
            if not h or needed_raw > h_raw:
                _cancel_pool_swap_reservation(ctx.author.id, ctx.guild_id, pool_id)
                await ctx.reply_error(
                    f"Insufficient **{token_in}** balance in your DeFi wallet. "
                    f"Have **{fmt_token(to_human(h_raw), token_in)}**, need "
                    f"**{fmt_token(to_human(needed_raw), token_in)}**."
                )
                return
            # Keep the float copy consistent with the raw-int path so fee /
            # slippage math downstream uses the same (possibly gas-reserved)
            # quantity.
            amount_in_float = to_human(amount_in_raw)
            if _tok_in_net:
                deduct_raw = min(amount_in_raw, h_raw)
                await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, _tok_in_net, token_in, -deduct_raw)
                # Gas is always paid on the gas chain, regardless of where
                # token_in lives. Deduct from the gas-chain wallet_holdings.
                if _mp_net:
                    await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, _mp_net, gas_coin, to_raw(-total_gas_cost))
                else:
                    await ctx.db.update_holding(ctx.author.id, ctx.guild_id, gas_coin, to_raw(-total_gas_cost))
            else:
                deduct_raw = min(amount_in_raw, h_raw)
                await ctx.db.update_holding(ctx.author.id, ctx.guild_id, token_in, -deduct_raw)
                await ctx.db.update_holding(ctx.author.id, ctx.guild_id, gas_coin, to_raw(-total_gas_cost))
            if platform_fee > 0:
                _vault_cut = platform_fee / 2.0
                try:
                    await ctx.db.split_to_community_reserves(
                        ctx.guild_id, gas_coin, to_raw(_vault_cut)
                    )
                    await deposit_to_vault(ctx.db, ctx.guild_id, _mp_net, _vault_cut, bot=ctx.bot)
                except Exception as _fee_exc:
                    log.warning(
                        "Vault fee deposit failed for guild %s swap (fee=%.6f %s): %s  -  swap proceeds.",
                        ctx.guild_id, _vault_cut, gas_coin, _fee_exc,
                    )

            action_id = await ctx.db.add_to_mempool(
                guild_id=ctx.guild_id,
                user_id=ctx.author.id,
                network=swap_network,
                action_type="swap",
                payload={
                    "token_in": token_in,
                    "token_out": token_out,
                    "amount_in": amount_in_float,
                    "pool_id": pool_id,
                    "min_amount_out": min_amount_out,
                    # Per-token networks so the block-time executor credits
                    # token_out to its own wallet (Moon Network group tokens
                    # must not land in the mining chain's wallet_holdings).
                    "net_in":  net_in,
                    "net_out": net_out,
                },
                gas_price=gas_price,
                gas_fee=to_raw(total_gas_cost),
            )

            tier_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}[gas_price]
            _mp_vault_cut = platform_fee / 2.0
            pending_embed = (
                card("⏳ Swap Queued", description="Your transaction is locked in the mempool and will execute at the next block.", color=C_AMBER)
                .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                .field("📤 Sending",   f"`{fmt_token(amount_in_float, token_in)}`",  True)
                .field("📥 Receiving", f"≈`{fmt_token(amount_out, token_out)}`",     True)
                .field("🌐 Network",   swap_network,                                  True)
                .field("⛽ Gas Tier",  f"{tier_emoji} **{gas_price.title()}**",       True)
                .field(
                    "💸 Gas + Platform Fee",
                    (
                        f"**Total: `{fmt_gas(total_gas_cost, gas_coin, gas_emoji)}`**\n"
                        f"• ⛽ Gas: `{fmt_gas(gas_fee, gas_coin, gas_emoji)}`\n"
                        f"• 🏦 Platform: `{fmt_gas(platform_fee, gas_coin, gas_emoji)}`\n"
                        f"• 🏛️ Vault: `{fmt_gas(_mp_vault_cut, gas_coin, gas_emoji)}`"
                    ),
                    False,
                )
                .footer(
                    f"Mempool #{action_id}  •  Tokens locked until confirmed  •  Next block in ~120s\n"
                    f"Note: final rate is set at block time  -  slippage may differ slightly."
                )
                .build()
            )
            if conf_msg:
                from core.framework.links import sanitize_embed
                sanitize_embed(pending_embed)
                await conf_msg.edit(embed=pending_embed, view=None)
            else:
                await ctx.reply(embed=pending_embed, mention_author=False)
            _record_user_swap_volume(ctx.author.id, ctx.guild_id, swap_usd_value)
            # Chimerastone XP: one ,swap action = one grant, regardless
            # of mempool vs instant settlement. Best-effort.
            try:
                from services import themed_stones as _ts
                await _ts.grant_chimerastone_xp(
                    ctx.db, int(ctx.author.id), int(ctx.guild_id),
                    swapped=True, bot=ctx.bot, guild=ctx.guild,
                )
            except Exception:
                log.debug("chimerastone xp grant failed", exc_info=True)
            # Per-pool cooldown was already committed by _reserve_pool_swap above.
            return

        # ── INSTANT PATH ─────────────────────────────────────────────────────
        # Each side of a vault-pair pool can live on a different network
        # (e.g. CAT on Moon Network paired with MTA on Moneta Chain), so
        # debit token_in from its own wallet and credit token_out to its own
        # wallet. A single shared network would mis-credit the output and
        # create duplicate-wallet rows (the bug that had group tokens
        # appearing on Moneta Chain in the DeFi wallet embed).
        _swap_fallback_net = _NET_SHORT.get(swap_network, "")
        # Built-in safety: same reason as the mempool branch above -- a
        # built-in token's wallet network is anchored to Config.TOKENS so
        # MOON / mMTA / mSUN never get debited / credited from the wrong
        # wallet on a TOKEN/MOON swap.
        _inst_net_in  = _resolve_token_wallet_net(token_in,  net_in)  or _swap_fallback_net
        _inst_net_out = _resolve_token_wallet_net(token_out, net_out) or _swap_fallback_net
        ok = await self._debit(ctx, token_in, amount_in_float, _inst_net_in)
        if not ok:
            _cancel_pool_swap_reservation(ctx.author.id, ctx.guild_id, pool_id)
            if conf_msg:
                from core.framework.links import sanitize_embed
                embed = card("", description="Insufficient balance.", color=C_ERROR).build()
                sanitize_embed(embed)
                await conf_msg.edit(embed=embed, view=None)
            return
        await self._credit(ctx, token_out, amount_out, _inst_net_out)

        if token_in == ca:
            await ctx.db.update_pool_reserves(
                pool_id, ctx.guild_id,
                to_raw(reserve_in + amount_in_float), to_raw(reserve_out - amount_out), pool["total_lp"]
            )
        else:
            await ctx.db.update_pool_reserves(
                pool_id, ctx.guild_id,
                to_raw(reserve_out - amount_out), to_raw(reserve_in + amount_in_float), pool["total_lp"]
            )

        _record_user_swap_volume(ctx.author.id, ctx.guild_id, swap_usd_value)
        # Chimerastone XP: one ,swap action = one grant on the instant
        # settle path too. Best-effort -- a stone hiccup never rolls
        # back the swap.
        try:
            from services import themed_stones as _ts
            await _ts.grant_chimerastone_xp(
                ctx.db, int(ctx.author.id), int(ctx.guild_id),
                swapped=True, bot=ctx.bot, guild=ctx.guild,
            )
        except Exception:
            log.debug("chimerastone xp grant failed", exc_info=True)
        # Per-pool cooldown was already committed by _reserve_pool_swap above.

        # VIP job fee rebate: pool keeps full fee, but user gets a fraction back
        job = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_cfg     = Config.JOBS.get(job["job_id"], {})
        rebate_rate = job_cfg.get("perks", {}).get("swap_fee", 0.0)
        rebate      = 0.0
        if rebate_rate > 0:
            rebate = amount_in_float * fee * rebate_rate
            if token_in == "USD":
                await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(rebate))
            else:
                await ctx.db.update_holding(ctx.author.id, ctx.guild_id, token_in, to_raw(rebate))

        # Determine network for tx hash prefix
        net_in_short = _NET_SHORT.get(net_in, "") if net_in else ""

        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "SWAP",
            symbol_in=token_in, amount_in=to_raw(amount_in_float),
            symbol_out=token_out, amount_out=to_raw(amount_out),
            price_at=amount_out / amount_in_float if amount_in_float else None,
            network=net_in_short,
            gas_fee=to_raw(gas_fee) if gas_fee > 0 else 0,
            gas_coin=gas_coin if gas_fee > 0 else "",
        )
        # Record swap volume in USD for both sides
        _pin = await ctx.db.get_price(token_in, ctx.guild_id)
        _swap_vol_usd = amount_in_float * (float(_pin["price"]) if _pin else 0.0)
        if _swap_vol_usd > 0:
            await ctx.db.add_trade_volume(ctx.guild_id, f"{token_in}USD", to_raw(_swap_vol_usd))
            await ctx.db.add_trade_volume(ctx.guild_id, f"{token_out}USD", to_raw(_swap_vol_usd))
            # Credit vault for both token sides (de-duped by checking both networks)
            _seen_vault_nets: set[str] = set()
            for _vtok in (token_in, token_out):
                _vnet = _VAULT_NET_MAP.get(all_tokens.get(_vtok, {}).get("network", ""))
                if _vnet and _vnet not in _seen_vault_nets:
                    _seen_vault_nets.add(_vnet)
                    try:
                        await credit_vault_volume(ctx.db, ctx.guild_id, _vnet, _swap_vol_usd, bot=ctx.bot)
                    except Exception:
                        pass  # never let vault update block a swap

        # Push the swap into the oracle + chart so slippage / impact is
        # actually visible (mirrors what .buy and .sell already do). Without
        # this, swaps silently mutate pool reserves and the drift loop's
        # TWAP arbitrage ends up the only thing the chart ever sees, which
        # made MOON / mMTA / mSUN pairs look frozen even during heavy use.
        await _swap_oracle_nudge(
            ctx.db, ctx.guild_id, token_in, token_out, price_impact, _swap_vol_usd,
        )
        # Realign drift loop's in-memory candle-open cache so the next tick
        # builds on the post-swap regime instead of the pre-swap price.
        for _sym in (token_in, token_out):
            _sym_meta = Config.TOKENS.get(_sym, {})
            if _sym in ("USD",) or _sym_meta.get("stablecoin") or _sym_meta.get("peg_to"):
                continue
            _r = await ctx.db.get_price(_sym, ctx.guild_id)
            if _r:
                self._last_price[(ctx.guild_id, f"{_sym}USD")] = float(_r["price"])

        # Do NOT pass channel= here  -  the command already replies to the user
        # directly (via conf_msg.edit or ctx.reply). Passing channel would cause
        # _on_swap_trade to send a second embed to the same channel.
        await ctx.bot.bus.publish(
            "swap_trade",
            guild=ctx.guild, user=ctx.author,
            token_in=token_in, amount_in=amount_in_float,
            token_out=token_out, amount_out=amount_out,
            pool_id=pool_id, price_impact=price_impact, tx_hash=tx_hash,
            gas_fee=gas_fee, gas_coin=gas_coin,
        )
        _usd = max(
            await _whale.usd_value_of(ctx.bot, token_in, amount_in_float, ctx.guild_id),
            await _whale.usd_value_of(ctx.bot, token_out, amount_out, ctx.guild_id),
        )
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "swap", _usd, symbol_in=token_in, symbol_out=token_out, amount_in=amount_in_float, amount_out=amount_out)

        color = C_SELL if price_impact > _SLIPPAGE_WARN else C_TEAL
        fee_amount = amount_in_float * fee
        _desc = "⚠️ **High price impact!** Consider splitting into smaller trades." if price_impact > _SLIPPAGE_WARN else None
        _b = card("🔄 Swap Complete", description=_desc, color=color)
        _b.field("📤 Sent",         f"**`{fmt_token(amount_in_float, token_in)}`**",  True)
        _b.field("📥 Received",     f"**`{fmt_token(amount_out, token_out)}`**",       True)
        _b.field("💱 Rate",         f"`1 {token_in} = {fmt_token(amount_out/amount_in_float, token_out)}`\n📊 Impact: {fmt_pct(price_impact*100)}", True)
        _fee_parts = [f"`{fee*100:.2g}%` ({fmt_token(fee_amount, token_in)})"]
        if _lq_discount > 0:
            _fee_parts.append(f"🌊 Liqstone: `-{_lq_discount*100:.2g}%`")
        if rebate > 0:
            _fee_parts.append(f"🎁 Rebate: +`{fmt_token(rebate, token_in)}`")
        _b.field("💧 Pool Fee",     "\n".join(_fee_parts), True)
        if gas_fee > 0:
            tier_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(gas_price, "🟡")
            _b.field("⛽ Gas Fee", f"**`{fmt_gas(gas_fee, gas_coin, tier_emoji)}`**", True)
        result_embed = _b.build()
        set_tx(result_embed, ctx.guild_id, tx_hash)
        if conf_msg:
            try:
                from core.framework.links import LinkManager
                LinkManager().process_embed(result_embed)
            except Exception:
                pass
            await conf_msg.edit(embed=result_embed, view=None)
        else:
            await ctx.reply(embed=result_embed, mention_author=False)

        # ── Auto-create wallet if user received tokens on a new network ────
        _out_net_short = _NET_SHORT.get(net_out, "")
        if _out_net_short and net_out:
            try:
                if not await ctx.db.has_defi_wallet(ctx.author.id, ctx.guild_id, _out_net_short):
                    _new_addr = await ctx.db.create_wallet_address(
                        ctx.author.id, ctx.guild_id,
                        label=None, is_temp=False,
                        network=net_out, address_prefix=_out_net_short,
                    )
                    await ctx.db.log_tx(
                        ctx.guild_id, ctx.author.id, "WALLET_CREATE",
                        symbol_in="WALLET", amount_in=0,
                        network=_out_net_short,
                    )
                    from core.framework.utils import ActionSuggestionView
                    _w_embed = (
                        card(f"🆕 {net_out} Wallet Created", color=C_INFO)
                        .description(
                            f"Since this is your first **{token_out}** swap, "
                            f"a **{net_out}** wallet was automatically created for you!\n"
                            f"Address: `{_new_addr}`"
                        )
                        .footer("View all your wallets with the button below")
                        .build()
                    )
                    _w_view = ActionSuggestionView(ctx, "📋 View My Wallets", "wallet list")
                    await ctx.send(embed=_w_embed, view=_w_view)
            except Exception:
                pass  # best-effort  -  don't block the swap

    # ══════════════════════════════════════════════════════════════════════════
    #  /trade pool subgroup
    # ══════════════════════════════════════════════════════════════════════════

    @trade.group(name="pool", invoke_without_command=True)
    @guild_only
    async def pool(self, ctx: DiscoContext) -> None:
        """Liquidity pool commands. Subcommands: list, create, add, remove, price"""
        if await suggest_subcommand(ctx, self.pool):
            return
        await ctx.send_help(ctx.command)

    # ── Pool list helpers ─────────────────────────────────────────────────────

    # Network-coin -> network-name mapping. When a pool pairs a custom
    # token against a known network coin (MOON, SUN, MTA, ARC, DSC),
    # the pool *belongs* to that coin's network regardless of the
    # custom token's own ``network`` field. Without this, a graduated
    # Disc.Fun token (which always lives on Discoin Network) paired
    # against MOON would have its MOON pair classified as Discoin
    # Network because the pool tokens are alphabetised and the custom
    # token usually sorts before MOON.
    _NETWORK_COINS: dict[str, str] = {
        "MOON": "Moon Network",
        "SUN":  "Sun Network",
        "MTA":  "Moneta Chain",
        "ARC":  "Arcadia Network",
        "DSC":  "Discoin Network",
    }

    @classmethod
    def _pool_network_label(cls, p: dict, all_tokens_cfg: dict) -> str:
        # First, prefer the network-coin side -- a TOKEN/MOON pool is
        # part of the Moon Network economy, never the custom token's
        # home network.
        for sym in (p["token_a"], p["token_b"]):
            mapped = cls._NETWORK_COINS.get(str(sym).upper())
            if mapped:
                return mapped
        # No network coin in the pair -- fall back to whichever side
        # has a configured network.
        for sym in (p["token_a"], p["token_b"]):
            net = all_tokens_cfg.get(sym, {}).get("network", "")
            if net:
                return net
        return "Other"

    async def _build_buddy_pool_rows(
        self, db: "Any", guild_id: int, *, sample_usd: float = 100.0,
    ) -> list[dict]:
        """Probe the Buddy Network's burn-swap pairs and return one
        synthetic-pool row per legal pair.

        Every BUD partner is bidirectional now; depth per side =
        oracle x circulating supply, identical to what ``,buddy pools``
        quotes. The (currently empty) ``BUD_ONEWAY_IN_TOKENS`` set is
        still unioned in so a future earn-only carve-out slots back in
        without touching this builder.
        """
        from core.config import Config as _Cfg
        from services import buddy_economy as _bes
        try:
            partners_bi = sorted(
                (set(_Cfg.BUD_SWAPPABLE_TOKENS) | set(_Cfg.BUD_ONEWAY_IN_TOKENS))
                - {"BUD"}
            )
        except Exception:
            return []
        bud_oracle = await _bes._oracle_price(db, guild_id, "BUD")
        if bud_oracle <= 0:
            return []
        bud_supply = await _bes._supply_human(db, guild_id, "BUD")
        bud_sample_raw = to_raw(sample_usd / bud_oracle)
        out: list[dict] = []
        for sym in partners_bi:
            try:
                q_out = await _bes.quote_burn_swap(
                    db, guild_id, None, "BUD", sym, bud_sample_raw,
                )
                partner_oracle = q_out.out_oracle
                if partner_oracle <= 0:
                    continue
                partner_sample_raw = to_raw(sample_usd / partner_oracle)
                q_in = await _bes.quote_burn_swap(
                    db, guild_id, None, sym, "BUD", partner_sample_raw,
                )
            except Exception:
                continue
            partner_supply = q_out.out_supply
            out.append({
                "kind": "buddy",
                "token_a": "BUD", "token_b": sym,
                "spot_rate": float(q_out.spot_rate),
                "a_depth_usd": float(bud_oracle * bud_supply),
                "b_depth_usd": float(partner_oracle * partner_supply),
                "bud_depth_usd": float(bud_oracle * bud_supply),
                "partner_depth_usd": float(partner_oracle * partner_supply),
                "slip_a_pct": float(q_out.slippage_pct),
                "slip_b_pct": float(q_in.slippage_pct),
            })
        return out

    async def _build_buddy_pool_embed(
        self,
        buddy_rows: list[dict],
        sample_usd: float,
        total_tvl_all: float,
    ) -> discord.Embed:
        """Synthetic Buddy Network page for ``,trade pool list``.

        Buddy swaps don't use the AMM ``pools`` table -- they're
        oracle+supply burn-swaps -- so we render them with a dedicated
        embed that mirrors the layout used by ``,buddy pools``: spot
        rate, per-side depth (oracle x circulating supply), and the
        per-pair sample slippage in each direction. Field values are
        chunked into successive ``Pairs (cont)`` slots so this page
        never trips Discord's 1024-char field cap.
        """
        net_tvl = 0.0
        rows: list[str] = []
        for r in buddy_rows:
            ta, tb = str(r.get("token_a") or ""), str(r.get("token_b") or "")
            bud_depth = float(r.get("bud_depth_usd") or 0.0)
            partner_depth = float(r.get("partner_depth_usd") or 0.0)
            net_tvl += bud_depth + partner_depth
            spot = float(r.get("spot_rate") or 0.0)
            spot_str = f"{spot:.4g}" if spot < 1_000 else f"{spot:,.2f}"
            slip_a = float(r.get("slip_a_pct") or 0.0) * 100.0
            slip_b = float(r.get("slip_b_pct") or 0.0) * 100.0
            depth_line = (
                f"-# depth: {ta} **{fmt_usd(r.get('a_depth_usd') or 0.0)}** / "
                f"{tb} **{fmt_usd(r.get('b_depth_usd') or 0.0)}**"
            )
            slip_line = (
                f"-# slip @ {fmt_usd(sample_usd)}: "
                f"{ta}->{tb} **{slip_a:.2f}%**, "
                f"{tb}->{ta} **{slip_b:.2f}%**"
            )
            rows.append(
                f"`{ta}/{tb}`  ·  1 {ta} = {spot_str} {tb}\n"
                f"{depth_line}\n{slip_line}"
            )
        builder = card(
            f"\U0001F300 Liquidity Pools  -  Buddy Network",
            description=(
                "Buddy-network swaps are synthetic burn-swaps "
                "(oracle x circulating supply). No LP shares; depth "
                "scales with per-token supply, not deposits. Use "
                "`.buddy quote` / `.buddy convert` to size and execute."
            ),
            color=C_TEAL,
        )
        # Chunk pair rows so we never blow the 1024-char per-field cap.
        idx = 0
        buf = ""
        for row in rows:
            sep = "\n\n" if buf else ""
            if buf and len(buf) + len(sep) + len(row) > 1000:
                builder.field(
                    "Pairs" if idx == 0 else "Pairs (cont)",
                    buf, False,
                )
                buf = row
                idx += 1
            else:
                buf += sep + row
        if buf:
            builder.field(
                "Pairs" if idx == 0 else "Pairs (cont)",
                buf, False,
            )
        if not rows:
            builder.description(
                "*No buddy-swap pools have live oracle prices yet.*"
            )
        builder.footer(
            f"{len(rows)} pairs  ·  Network depth ≈ {fmt_usd(net_tvl)}  ·  "
            f"Total Platform TVL ≈ {fmt_usd(total_tvl_all)}"
        )
        return builder.build()

    async def _build_pool_embed(
        self,
        pools: list[dict],
        user_lp_map: dict,
        price_cache: dict,
        network_label: str,
        total_tvl_all: float,
        moon_swap_map: dict[str, bool] | None = None,
        all_tokens_cfg: dict | None = None,
    ) -> discord.Embed:
        """Build a single embed showing pools for a given network.

        ``moon_swap_map`` is keyed by pool_id; True means the MOON pair is
        bidirectionally swappable (mMTA, mSUN, or a flagged player-deployed
        token). Pools missing from the map default to one-way / N-A. The
        Moon Network page surfaces this distinction so players can tell at
        a glance which pools they can route into MOON through.
        """
        rows = []
        net_tvl = 0.0
        is_moon_page = network_label == "Moon Network"
        moon_swap_map = moon_swap_map or {}
        all_tokens_cfg = all_tokens_cfg or {}
        for p in pools:
            ta, tb = p["token_a"], p["token_b"]
            ra, rb = to_human(p["reserve_a"]), to_human(p["reserve_b"])
            price = rb / ra if ra > 0 else 0.0
            pa_usd = price_cache.get(ta, 1.0 if ta == "USD" else 0.0)
            pb_usd = price_cache.get(tb, 1.0 if tb == "USD" else 0.0)
            tvl = ra * pa_usd + rb * pb_usd
            net_tvl += tvl

            lp_mark = ""
            if p["pool_id"] in user_lp_map:
                _, pct = user_lp_map[p["pool_id"]]
                lp_mark = f" 🟢{pct:.1f}%"

            # Moon Network page only: tag each pool's swap orientation so a
            # player can see at a glance which pairs are bidirectional vs
            # MOON-out only vs not involving MOON at all.
            swap_tag = ""
            if is_moon_page:
                if "MOON" in (ta, tb):
                    other = tb if ta == "MOON" else ta
                    if moon_swap_map.get(p["pool_id"]):
                        swap_tag = "  🔁 swappable"
                    else:
                        meta = all_tokens_cfg.get(other, {})
                        if (
                            meta.get("token_type") == "group"
                            and meta.get("network") == "Moon Network"
                        ):
                            swap_tag = "  ➡️ MOON→only (Lunar Mint exit)"
                        else:
                            swap_tag = "  🔒 not swappable"
                else:
                    swap_tag = "  🔁 swappable"

            tvl_str   = fmt_usd(tvl) if tvl > 0 else " - "
            price_str = f"{price:.4g}" if price < 1_000 else f"{price:,.2f}"
            rows.append(
                f"`{ta}/{tb}`{lp_mark}{swap_tag}  ·  1 {ta} = {price_str} {tb}  ·  TVL {tvl_str}"
            )

        footer = (
            f"{len(pools)} pools  ·  Network TVL ≈ {fmt_usd(net_tvl)}"
            f"  ·  Total Platform TVL ≈ {fmt_usd(total_tvl_all)}"
            f"  ·  🟢 = you have LP"
        )
        if is_moon_page:
            footer += "  ·  🔁 = bidirectional  ·  ➡️ = MOON→only"

        _DESC_CAP = 4000  # safe margin under Discord's 4096-char description limit
        desc_full = "\n".join(rows)
        if len(desc_full) > _DESC_CAP:
            # Trim rows until the joined text fits, then note truncation.
            while rows and len("\n".join(rows)) > _DESC_CAP - 30:
                rows.pop()
            desc_full = "\n".join(rows) + f"\n*...and {len(pools) - len(rows)} more*"
        e = (
            card(
                f"🌊 Liquidity Pools  -  {network_label}",
                description=desc_full if desc_full else "*No pools on this network.*",
                color=C_TEAL,
            )
            .footer(footer)
            .build()
        )
        return e

    @pool.command(name="list", aliases=["ls"])
    @guild_only
    async def pool_list(self, ctx: DiscoContext, *, query: str = "") -> None:
        """List liquidity pools. Use the dropdown to select a network.
        Filter by network: `.pool list arc`
        Filter by pair: `.pool list ethusdc` or `.pool list arc/usdc`
        Shows anonymous LP provider count and breakdown."""
        pools = await ctx.db.get_all_pools(ctx.guild_id) or []
        all_tokens_cfg = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)

        # Synthetic Buddy Network pools -- BUD <-> partner burn-swaps
        # don't live in the AMM ``pools`` table, so we compute them on
        # the fly (oracle x circulating supply for depth, sample-sized
        # slippage probe for the listing). Built once here and threaded
        # through the network dropdown alongside real pool networks.
        sample_usd = 100.0
        buddy_rows = await self._build_buddy_pool_rows(
            ctx.db, ctx.guild_id, sample_usd=sample_usd,
        )

        if not pools and not buddy_rows:
            await ctx.reply_error("No pools exist yet.")
            return

        # ── Handle query filter: network name or pair ──
        q = query.strip().lower().replace("/", "")
        _NET_FILTER_MAP = {
            "arc": "Arcadia Network", "arcadia": "Arcadia Network",
            "sun": "Sun Network",
            "mta": "Moneta Chain", "moneta": "Moneta Chain",
            "dsc": "Discoin Network", "discoin": "Discoin Network",
            "bud": "Buddy Network", "buddy": "Buddy Network",
        }

        filtered_pool: dict | None = None  # specific pool detail view
        filter_net: str | None = None

        if q:
            # Check network filter first
            if q in _NET_FILTER_MAP:
                filter_net = _NET_FILTER_MAP[q]
            else:
                # Try as a pair (e.g. "ethusdc" or "ethaave")
                # Try all combinations of splitting the query into two token symbols
                matched = None
                q_upper = q.upper()
                for p in pools:
                    pair_concat = (p["token_a"] + p["token_b"]).upper()
                    pair_concat_rev = (p["token_b"] + p["token_a"]).upper()
                    if q_upper == pair_concat or q_upper == pair_concat_rev:
                        matched = p
                        break
                if matched:
                    filtered_pool = matched
                else:
                    # Try partial match on network full name
                    for net_name in set(self._pool_network_label(p, all_tokens_cfg) for p in pools):
                        if q in net_name.lower():
                            filter_net = net_name
                            break

        # ── Specific pool detail with anonymous LP providers ──
        if filtered_pool:
            p = filtered_pool
            price_cache: dict[str, float] = {"USD": 1.0}
            for sym in (p["token_a"], p["token_b"]):
                r = await ctx.db.get_price(sym, ctx.guild_id)
                if r:
                    price_cache[sym] = float(r["price"])

            tvl_a = to_human(p["reserve_a"]) * price_cache.get(p["token_a"], 0)
            tvl_b = to_human(p["reserve_b"]) * price_cache.get(p["token_b"], 0)
            tvl = tvl_a + tvl_b

            # Get all LP positions for this pool (anonymous)
            lp_positions = await ctx.db.get_pool_lp_positions(p["pool_id"], ctx.guild_id)
            lp_lines = []
            total_lp_h = to_human(p["total_lp"])
            for i, lp in enumerate(sorted(lp_positions, key=lambda x: float(x["lp_shares"]), reverse=True)):
                shares = to_human(lp["lp_shares"])
                if shares <= 0:
                    continue
                share_pct = shares / total_lp_h * 100 if total_lp_h > 0 else 0
                frac = shares / total_lp_h if total_lp_h > 0 else 0
                val = frac * tvl
                # Anonymous: Provider #1, Provider #2, etc
                bar_len = 10
                filled = int(share_pct / 100 * bar_len)
                bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
                lp_lines.append(f"`[{bar}]` Provider #{i+1}  -  {share_pct:.1f}% ({fmt_usd(val)})")

            desc = (
                f"**{p['token_a']}/{p['token_b']}**\n\n"
                f"**Reserves:** {fmt_token(to_human(p['reserve_a']), p['token_a'])} / {fmt_token(to_human(p['reserve_b']), p['token_b'])}\n"
                f"**TVL:** {fmt_usd(tvl)}\n"
                f"**Total LP Shares:** {to_human(p['total_lp']):,.4f}\n"
                f"**Providers:** {len(lp_lines)}\n\n"
            )
            if lp_lines:
                desc += "**Liquidity Providers (anonymous):**\n" + "\n".join(lp_lines[:20])
            else:
                desc += "*No liquidity providers.*"

            embed = (
                card(f"🌊 Pool Detail  -  {p['token_a']}/{p['token_b']}", description=desc, color=C_TEAL)
                .footer("LP providers are shown anonymously  ·  Use .pool add to provide liquidity")
                .build()
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        # Collect user LP positions
        user_lp_map: dict[str, tuple[float, float]] = {}
        for p in pools:
            lp_pos = await ctx.db.get_user_lp(ctx.author.id, ctx.guild_id, p["pool_id"])
            if lp_pos and p["total_lp"] > 0 and lp_pos["lp_shares"] > 0:
                share_pct = lp_pos["lp_shares"] / p["total_lp"] * 100
                user_lp_map[p["pool_id"]] = (lp_pos["lp_shares"], share_pct)

        # Collect oracle prices
        price_cache: dict[str, float] = {"USD": 1.0}
        for sym in set(t for p in pools for t in (p["token_a"], p["token_b"])):
            r = await ctx.db.get_price(sym, ctx.guild_id)
            if r:
                price_cache[sym] = float(r["price"])

        # Pre-compute MOON-pair swappability so the Moon Network pool page
        # can tag each pool with 🔁 (bidirectional) or ➡️ (MOON->only) without
        # making N extra DB calls per render. Only MOON-paired pools matter;
        # everything else stays unannotated.
        moon_swap_map: dict[str, bool] = {}
        for p in pools:
            if "MOON" in (p["token_a"], p["token_b"]):
                other = p["token_b"] if p["token_a"] == "MOON" else p["token_a"]
                moon_swap_map[p["pool_id"]] = await _is_moon_swappable_pair(
                    ctx.db, ctx.guild_id, "MOON", other, all_tokens=all_tokens_cfg,
                )

        # Group by network
        by_net: dict[str, list[dict]] = {}
        for p in pools:
            net = self._pool_network_label(p, all_tokens_cfg)
            by_net.setdefault(net, []).append(p)

        # Compute total TVL across all real pools
        def _tvl(p: dict) -> float:
            ta, tb = p["token_a"], p["token_b"]
            return (to_human(p["reserve_a"]) * price_cache.get(ta, 0.0)
                    + to_human(p["reserve_b"]) * price_cache.get(tb, 0.0))

        total_tvl = sum(_tvl(p) for p in pools)
        # Buddy network depth folds into the platform-wide TVL line so a
        # player's "what's my LP doing globally" mental model still adds
        # up. Buddy depth = oracle x circulating supply per side.
        buddy_total_depth = sum(
            float(r.get("a_depth_usd") or 0.0) + float(r.get("b_depth_usd") or 0.0)
            for r in (buddy_rows or [])
        )
        total_tvl_all = total_tvl + buddy_total_depth

        # Inject the synthetic Buddy Network bucket into the dropdown.
        if buddy_rows:
            by_net["Buddy Network"] = buddy_rows

        networks = sorted(by_net.keys())

        # If filtering by network, show only that network
        if filter_net and filter_net in by_net:
            if filter_net == "Buddy Network":
                embed = await self._build_buddy_pool_embed(
                    buddy_rows, sample_usd=sample_usd,
                    total_tvl_all=total_tvl_all,
                )
                await ctx.reply(embed=embed, mention_author=False)
                return
            net_pools = by_net[filter_net]
            embed = await self._build_pool_embed(
                net_pools, user_lp_map, price_cache, filter_net, total_tvl_all,
                moon_swap_map=moon_swap_map, all_tokens_cfg=all_tokens_cfg,
            )
            # Add LP provider counts
            lp_counts = []
            for p in net_pools:
                lp_positions = await ctx.db.get_pool_lp_positions(p["pool_id"], ctx.guild_id)
                active_lps = [lp for lp in lp_positions if float(lp["lp_shares"]) > 0]
                lp_counts.append(f"**{p['token_a']}/{p['token_b']}**: {len(active_lps)} provider(s)")
            if lp_counts:
                _FIELD_CAP = 1000  # safe margin under Discord's 1024-char field limit
                chunk: list[str] = []
                chunk_len = 0
                first_chunk = True
                for _line in lp_counts:
                    _line_len = len(_line) + 1  # +1 for joining newline
                    if chunk and chunk_len + _line_len > _FIELD_CAP:
                        embed.add_field(
                            name="Anonymous LP Providers" if first_chunk else "​",
                            value="\n".join(chunk),
                            inline=False,
                        )
                        chunk = []
                        chunk_len = 0
                        first_chunk = False
                    chunk.append(_line)
                    chunk_len += _line_len
                if chunk:
                    embed.add_field(
                        name="Anonymous LP Providers" if first_chunk else "​",
                        value="\n".join(chunk),
                        inline=False,
                    )
            await ctx.reply(embed=embed, mention_author=False)
            return

        # Start with first network
        first_net = networks[0]

        if first_net == "Buddy Network":
            initial_embed = await self._build_buddy_pool_embed(
                buddy_rows, sample_usd=sample_usd,
                total_tvl_all=total_tvl_all,
            )
        else:
            initial_embed = await self._build_pool_embed(
                by_net[first_net], user_lp_map, price_cache, first_net, total_tvl_all,
                moon_swap_map=moon_swap_map, all_tokens_cfg=all_tokens_cfg,
            )

        # Build network dropdown
        _NET_EMOJIS = {
            "Sun Network": "☀", "Moneta Chain": "🔸", "Arcadia Network": "🔷",
            "Discoin Network": "🪙", "Moon Network": "\U0001F315",
            "Buddy Network": "\U0001F436", "Other": "🌐",
        }

        # Need a reference to self for the callback
        _self = self

        class PoolNetworkSelect(discord.ui.Select):
            def __init__(self_inner) -> None:
                options = [
                    discord.SelectOption(
                        label=net,
                        value=net,
                        emoji=_NET_EMOJIS.get(net, "🌐"),
                        description=(
                            f"{len(by_net[net])} synthetic pair(s)"
                            if net == "Buddy Network"
                            else f"{len(by_net[net])} pool(s)"
                        ),
                        default=(net == first_net),
                    )
                    for net in networks
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
                if selected == "Buddy Network":
                    embed = await _self._build_buddy_pool_embed(
                        buddy_rows, sample_usd=sample_usd,
                        total_tvl_all=total_tvl_all,
                    )
                else:
                    embed = await _self._build_pool_embed(
                        by_net[selected], user_lp_map, price_cache, selected, total_tvl_all,
                        moon_swap_map=moon_swap_map, all_tokens_cfg=all_tokens_cfg,
                    )
                await interaction.response.edit_message(embed=embed, view=view)

        class PoolListView(discord.ui.View):
            def __init__(self_inner) -> None:
                super().__init__(timeout=120)
                self_inner.add_item(PoolNetworkSelect())

            async def on_timeout(self_inner) -> None:
                try:
                    for item in self_inner.children:
                        item.disabled = True
                    await msg.edit(view=self_inner)
                except Exception:
                    pass

        view = PoolListView()
        msg = await ctx.reply(embed=initial_embed, view=view, mention_author=False)

    @pool.command(name="create")
    @guild_only
    async def pool_create(self, ctx: DiscoContext, token_a: str, token_b: str) -> None:
        """Create a new liquidity pool with treasury seed liquidity at oracle ratio.
        Requires manage_guild permission OR ANON_FOUNDER job rank."""
        token_a, token_b = token_a.upper(), token_b.upper()
        valid = await self._valid_tokens(ctx.guild_id)
        if token_a not in valid or token_b not in valid:
            await ctx.reply_error(f"Invalid token(s). Valid: {', '.join(sorted(valid))}")
            return
        if token_a == token_b:
            await ctx.reply_error("Tokens must be different.")
            return

        # Earn-only tokens (MOON) may only live in pools paired with Moon
        # Network group tokens. A MOON/USD or MOON/ARC pool would be a
        # back door into MOON since LP providers could deposit MOON on one
        # side and effectively sell it for cash. Keep MOON inside its
        # native ecosystem.
        _all_tokens_for_pool = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)

        def _is_moon_group(sym: str) -> bool:
            meta = _all_tokens_for_pool.get(sym, {})
            return (
                meta.get("token_type") == "group"
                and meta.get("network") == "Moon Network"
            )

        earn_sym = other_sym = None
        if token_a in Config.EARN_ONLY_TOKENS:
            earn_sym, other_sym = token_a, token_b
        elif token_b in Config.EARN_ONLY_TOKENS:
            earn_sym, other_sym = token_b, token_a
        if earn_sym and not _is_moon_group(other_sym):
            await ctx.reply_error(
                f"**{earn_sym}** pools can only be paired with Moon Network "
                f"group tokens. **{other_sym}** is not one."
            )
            return

        is_mod = ctx.author.guild_permissions.manage_guild
        if not is_mod:
            job = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
            job_cfg = Config.JOBS.get(job["job_id"], {})
            can_create = job_cfg.get("perks", {}).get("can_create_pool", False)
            if not can_create:
                await ctx.reply_error("You need `Manage Guild` permission or **Exploiter** job tier to create pools.")
                return

        pool_id, ca, cb = ctx.db.make_pool_id(token_a, token_b)
        existing = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if existing:
            await ctx.reply_error(f"Pool **{pool_id}** already exists.")
            return

        # Seed at $500k per side using oracle ratio
        seed_a = _TREASURY_SEED
        seed_b = _TREASURY_SEED

        # Determine which token is the "base" and which is the quote (USD or stablecoin)
        quote_syms = {"USD"} | set(Config.NETWORK_STABLECOIN.values())
        if cb in quote_syms:
            price_row = await ctx.db.get_price(ca, ctx.guild_id)
            if price_row and price_row["price"] > 0:
                seed_a = _TREASURY_SEED / float(price_row["price"])
                seed_b = _TREASURY_SEED
        elif ca in quote_syms:
            price_row = await ctx.db.get_price(cb, ctx.guild_id)
            if price_row and price_row["price"] > 0:
                seed_a = _TREASURY_SEED
                seed_b = _TREASURY_SEED / float(price_row["price"])

        await ctx.db.create_pool(pool_id, ctx.guild_id, ca, cb, seed_a, seed_b)
        await ctx.reply_success(
            f"Created pool **{ca} / {cb}** seeded with:\n"
            f"**{fmt_token(seed_a, ca)}** ≈ $500,000 / **{fmt_token(seed_b, cb)}** ≈ $500,000",
            title="🌊 Pool Created",
        )

    # ── pool add (addlp) ────────────────────────────────────────────────────

    @pool.command(name="add", aliases=["addlp"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def addlp(self, ctx: DiscoContext, token_a: str, token_b: str, amount_a_raw: str, amount_b_raw: str) -> None:
        """Add liquidity to a pool. Usage: $addlp TOKEN_A TOKEN_B amount_a|all amount_b|all"""
        token_a, token_b = token_a.upper(), token_b.upper()
        pool_id, ca, cb = ctx.db.make_pool_id(token_a, token_b)
        swapped = token_a != ca
        if swapped:
            amount_a_raw, amount_b_raw = amount_b_raw, amount_a_raw

        # Resolve "all" to user's full balance of that token. Each token
        # is looked up on ITS OWN network -- a CHEF/MOON pool reads
        # CHEF from Arcadia and MOON from Moon Network. Built-ins
        # anchor to Config.TOKENS via _resolve_token_wallet_net so the
        # lookup is robust to a stale all_tokens merge.
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        pool_network_pre = all_tokens.get(ca, {}).get("network") or all_tokens.get(cb, {}).get("network") or ""
        _pool_net_short_pre = _NET_SHORT.get(pool_network_pre, "")

        async def _resolve_amount(raw: str, symbol: str) -> float | None:
            if raw.lower() == "all":
                if symbol == "USD":
                    u = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
                    return to_human(u["wallet"]) if u else 0.0
                tok_net_full = all_tokens.get(symbol, {}).get("network", "")
                tok_net_short = (
                    _resolve_token_wallet_net(symbol, tok_net_full)
                    or _pool_net_short_pre
                )
                if tok_net_short:
                    h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, tok_net_short, symbol)
                    return to_human(h["amount"]) if h else 0.0
                h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, symbol)
                return to_human(h["amount"]) if h else 0.0
            try:
                return float(raw)
            except ValueError:
                return None

        amount_a = await _resolve_amount(amount_a_raw, ca)
        amount_b = await _resolve_amount(amount_b_raw, cb)

        if amount_a is None or amount_b is None:
            await ctx.reply_error("Amounts must be numbers or `all`.")
            return
        if not math.isfinite(amount_a) or not math.isfinite(amount_b):
            await ctx.reply_error("Amounts must be finite numbers.")
            return
        if amount_a <= 0 or amount_b <= 0:
            await ctx.reply_error("Amounts must be positive. (If you used `all`, you may have 0 of that token.)")
            return

        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if not pool:
            await ctx.reply_error(f"Pool **{ca}/{cb}** doesn't exist. Ask an admin to create it.")
            return
        if pool.get("vault_locked"):
            await ctx.reply_error(
                f"**{ca}/{cb}** is a vault-locked group token pool. "
                "Players cannot add or remove liquidity."
            )
            return

        # Raw-int reserves for exact LP mint math; human floats for display.
        _pool_ra_raw = int(pool["reserve_a"])
        _pool_rb_raw = int(pool["reserve_b"])
        _pool_lp_raw = int(pool["total_lp"])
        _pool_ra_h = to_human(_pool_ra_raw)
        _pool_rb_h = to_human(_pool_rb_raw)
        _pool_lp_h = to_human(_pool_lp_raw)

        def _compute_lp_mint_raw(a_h: float, b_h: float) -> int:
            """LP mint amount in raw int space.

            Empty pool: ``sqrt(a_raw * b_raw)`` (geometric mean, which stays
            in raw scale because ``sqrt(SCALE**2) == SCALE``).
            Existing pool: ``total_lp_raw * min(share_a, share_b)`` where the
            share is computed as an integer fraction of the reserves.
            """
            a_raw = to_raw(a_h)
            b_raw = to_raw(b_h)
            if _pool_lp_raw == 0:
                return math.isqrt(a_raw * b_raw)
            if _pool_ra_raw <= 0 or _pool_rb_raw <= 0:
                return 0
            # share_a_raw = a_raw * SCALE / _pool_ra_raw (as a SCALE-scaled fraction)
            # lp_mint_raw = _pool_lp_raw * min(share_a_raw, share_b_raw) / SCALE
            # Rewrite to a single floor division on the smaller side:
            mint_from_a = _pool_lp_raw * a_raw // _pool_ra_raw
            mint_from_b = _pool_lp_raw * b_raw // _pool_rb_raw
            return min(mint_from_a, mint_from_b)

        lp_mint_raw = _compute_lp_mint_raw(amount_a, amount_b)
        lp_mint = to_human(lp_mint_raw)

        if lp_mint_raw <= 0:
            await ctx.reply_error("Calculated LP shares too small. Try a larger amount.")
            return

        # Charge gas if validators are active on this pool's network
        from cogs.validators import gas_fee_for_network
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        pool_network = all_tokens.get(ca, {}).get("network") or all_tokens.get(cb, {}).get("network") or ""
        gas_fee = 0.0
        gas_coin = ""
        gas_em = ""
        if pool_network:
            active_v = [v for v in await ctx.db.get_pos_validators_for_network(ctx.guild_id, pool_network) if v["is_active"]]
            if active_v:
                gas_coin, gas_fee = await gas_fee_for_network(ctx.db, ctx.guild_id, "addlp", "medium", pool_network)
                gas_cfg = Config.TOKENS.get(gas_coin, {})
                gas_em = gas_cfg.get("emoji", "●")
                gas_h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, _NET_SHORT.get(pool_network, ""), gas_coin)
                gas_bal = to_human(gas_h["amount"]) if gas_h else 0.0
                if gas_bal < gas_fee:
                    await ctx.reply_error(f"Need **`{fmt_gas(gas_fee, gas_coin, gas_em)}`** for gas. You have **`{fmt_gas(gas_bal, gas_coin, gas_em)}`**.")
                    return

                # When "all" was used and gas coin matches one of the LP
                # tokens, reduce the LP amount so gas doesn't overdraw.
                if gas_fee > 0:
                    if amount_a_raw.lower() == "all" and gas_coin == ca:
                        amount_a -= gas_fee
                        if amount_a <= 0:
                            await ctx.reply_error(f"Your entire **{ca}** balance would be consumed by gas. Nothing left to deposit.")
                            return
                    if amount_b_raw.lower() == "all" and gas_coin == cb:
                        amount_b -= gas_fee
                        if amount_b <= 0:
                            await ctx.reply_error(f"Your entire **{cb}** balance would be consumed by gas. Nothing left to deposit.")
                            return
                    # Recalculate LP mint with adjusted amounts (raw int math)
                    lp_mint_raw = _compute_lp_mint_raw(amount_a, amount_b)
                    lp_mint = to_human(lp_mint_raw)
                    if lp_mint_raw <= 0:
                        await ctx.reply_error("Calculated LP shares too small after gas reservation.")
                        return

        # LP concentration cap
        _existing_lp_pos = await ctx.db.get_user_lp(ctx.author.id, ctx.guild_id, pool_id)
        _existing_shares = to_human(_existing_lp_pos["lp_shares"]) if _existing_lp_pos else 0.0
        resulting_shares = _existing_shares + lp_mint
        if _pool_lp_h + lp_mint > 0:
            concentration = resulting_shares / (_pool_lp_h + lp_mint)
            if concentration > Config.LP_MAX_CONCENTRATION:
                await ctx.reply_error(
                    f"LP concentration limit: you would own {concentration*100:.1f}% of this pool "
                    f"(max {Config.LP_MAX_CONCENTRATION*100:.0f}%). Split across pools or wait for others to add."
                )
                return

        # ── Estimate pool share and earnings ──────────────────────────────────
        new_total_lp   = _pool_lp_h + lp_mint
        pool_share_pct = (lp_mint / new_total_lp * 100) if new_total_lp > 0 else 100.0
        pa_usd = price_cache_lp = 0.0
        pb_usd = 0.0
        try:
            pr_a = await ctx.db.get_price(ca, ctx.guild_id)
            pr_b = await ctx.db.get_price(cb, ctx.guild_id)
            pa_usd = float(pr_a["price"]) if pr_a else (1.0 if ca in ("USD", "USDC", "DSD") else 0.0)
            pb_usd = float(pr_b["price"]) if pr_b else (1.0 if cb in ("USD", "USDC", "DSD") else 0.0)
        except Exception:
            pass
        tvl_after = (_pool_ra_h + amount_a) * pa_usd + (_pool_rb_h + amount_b) * pb_usd
        est_daily_fee_pool = tvl_after * 0.05 * _DEFAULT_SWAP_FEE
        est_daily_earn = est_daily_fee_pool * (pool_share_pct / 100.0)

        # ── Confirmation ─────────────────────────────────────────────────────
        _b = (
            card(f"⚠️ Confirm Add Liquidity  -  {ca}/{cb}", description="Review before depositing.", color=C_AMBER)
            .field(f"Deposit {ca}", f"**`{fmt_token(amount_a, ca)}`**", True)
            .field(f"Deposit {cb}", f"**`{fmt_token(amount_b, cb)}`**", True)
            .field("LP Shares",     f"**`{fmt_token(lp_mint, 'LP')}`**", True)
            .field("Pool Share",    f"**{fmt_pct(pool_share_pct)}**",    True)
        )
        if est_daily_earn > 0:
            _b.field("Est. Daily Earn", f"**≈ `{fmt_usd(est_daily_earn)}`**", True)
        if gas_fee > 0:
            _b.field("Gas Fee", f"**`{fmt_gas(gas_fee, gas_coin, gas_em)}`**", True)
        conf_embed = _b.footer("Estimates based on 5% daily pool volume turnover at 0.3% swap fee.").build()
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(embed=conf_embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await conf_msg.edit(view=None)
        if not confirmed:
            cancel = card("", description="Liquidity deposit cancelled.", color=C_NEUTRAL).build()
            await conf_msg.edit(embed=cancel)
            return

        _pool_net = _NET_SHORT.get(pool_network, "")
        # Each side of a cross-network pool (CHEF on Arcadia / MOON on
        # Moon Network, or any TOKEN/MOON pair where the deployer is on
        # a different chain) lives in its own wallet_holdings row keyed
        # by its OWN network. Using ``_pool_net`` for both sides used to
        # debit MOON from network='arc' on CHEF/MOON and report
        # "0 MOON in your DeFi wallet" even with 1k MOON sitting on
        # Moon Network. Resolve each side independently, anchoring
        # built-ins to Config.TOKENS so the lookup is robust to a stale
        # ``all_tokens`` merge.
        _net_a = _resolve_token_wallet_net(
            ca, all_tokens.get(ca, {}).get("network", "")
        ) or _pool_net
        _net_b = _resolve_token_wallet_net(
            cb, all_tokens.get(cb, {}).get("network", "")
        ) or _pool_net
        if gas_fee > 0:
            if _pool_net:
                await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, _pool_net, gas_coin, to_raw(-gas_fee))
            else:
                await ctx.db.update_holding(ctx.author.id, ctx.guild_id, gas_coin, to_raw(-gas_fee))

        ok_a = await self._debit(ctx, ca, amount_a, _net_a)
        if not ok_a:
            if gas_fee > 0:
                if _pool_net:
                    await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, _pool_net, gas_coin, to_raw(gas_fee))
                else:
                    await ctx.db.update_holding(ctx.author.id, ctx.guild_id, gas_coin, to_raw(gas_fee))
            return
        ok_b = await self._debit(ctx, cb, amount_b, _net_b)
        if not ok_b:
            await self._credit(ctx, ca, amount_a, _net_a)
            if gas_fee > 0:
                if _pool_net:
                    await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, _pool_net, gas_coin, to_raw(gas_fee))
                else:
                    await ctx.db.update_holding(ctx.author.id, ctx.guild_id, gas_coin, to_raw(gas_fee))
            return

        # Raw-int pool reserve update so the reserves column stays exact.
        amount_a_raw_i = to_raw(amount_a)
        amount_b_raw_i = to_raw(amount_b)
        new_res_a_raw = _pool_ra_raw + amount_a_raw_i
        new_res_b_raw = _pool_rb_raw + amount_b_raw_i
        new_total_raw = _pool_lp_raw + lp_mint_raw
        new_res_a_h = to_human(new_res_a_raw)
        new_res_b_h = to_human(new_res_b_raw)
        new_total_h = to_human(new_total_raw)

        await ctx.db.update_pool_reserves(pool_id, ctx.guild_id, new_res_a_raw, new_res_b_raw, new_total_raw)
        total_user_lp = await ctx.db.update_lp_position(ctx.author.id, ctx.guild_id, pool_id, lp_mint_raw)

        res_a_per_lp = new_res_a_h / new_total_h if new_total_h > 0 else 0
        res_b_per_lp = new_res_b_h / new_total_h if new_total_h > 0 else 0
        await ctx.db.upsert_lp_snapshot(ctx.author.id, ctx.guild_id, pool_id, to_raw(res_a_per_lp), to_raw(res_b_per_lp))

        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "ADDLP",
            symbol_in=f"{ca}/{cb}", amount_in=lp_mint_raw,
            network=_NET_SHORT.get(pool_network, ""),
            gas_fee=to_raw(gas_fee) if gas_fee > 0 else 0, gas_coin=gas_coin,
        )
        await ctx.bot.bus.publish("lp_added", guild=ctx.guild, user=ctx.author,
            pool_id=pool_id, lp_minted=lp_mint, tx_hash=tx_hash,
            amount_a=amount_a, amount_b=amount_b, token_a=ca, token_b=cb,
            gas_fee=gas_fee, gas_coin=gas_coin)
        _usd_a = await _whale.usd_value_of(ctx.bot, ca, amount_a, ctx.guild_id)
        _usd_b = await _whale.usd_value_of(ctx.bot, cb, amount_b, ctx.guild_id)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "addlp", _usd_a + _usd_b, symbol_in=ca, symbol_out=cb, amount_in=amount_a, amount_out=amount_b)

        # Grant lockstone XP for LP provision (scales with USD value like staking)
        try:
            from cogs.stake import cap_xp
            _lp_usd = _usd_a + _usd_b
            if _lp_usd > 0:
                _ls = await ctx.db.get_lockstone(ctx.author.id, ctx.guild_id)
                if _ls:
                    _LS_CFG = Config.SHOP_ITEMS.get("lockstone", {})
                    base_xp = float(_LS_CFG.get("xp_per_tick", 10))
                    xp_ref = float(Config.XP_STAKE_REFERENCE_USD) if hasattr(Config, "XP_STAKE_REFERENCE_USD") else 1000.0
                    xp_scale = min(float(getattr(Config, "XP_SCALE_MAX", 5.0)), _lp_usd / xp_ref)
                    xp_gain = base_xp * xp_scale
                    xp_result = await ctx.db.add_lockstone_xp(ctx.author.id, ctx.guild_id, xp_gain)
                    if xp_result:
                        live_xp, live_level = xp_result
                        capped_xp = cap_xp(live_xp, live_level, _LS_CFG)
                        if capped_xp < live_xp:
                            await ctx.db.update_lockstone_xp(ctx.author.id, ctx.guild_id, capped_xp, live_level)
        except Exception:
            pass  # non-critical  -  don't break LP flow for XP issues

        _b = (
            card("🌊 Liquidity Added", description=f"Pool **{ca}/{cb}**  -  deposit confirmed", color=C_TEAL)
            .field(f"📥 Deposited {ca}", f"**`{fmt_token(amount_a, ca)}`**",        True)
            .field(f"📥 Deposited {cb}", f"**`{fmt_token(amount_b, cb)}`**",        True)
            .field("🪙 LP Shares Minted", f"**`{fmt_token(lp_mint, 'LP')}`**",                  True)
            .field("💼 Your Total LP",    f"**`{fmt_token(total_user_lp, 'LP')}`**", True)
        )
        if gas_fee > 0:
            _b.field("⛽ Gas Fee", f"**`{fmt_gas(gas_fee, gas_coin, gas_em)}`**", True)
        embed = _b.build()
        set_tx(embed, ctx.guild_id, tx_hash)
        await ctx.reply(embed=embed, mention_author=False)

    # ── pool remove everything helper ─────────────────────────────────────

    async def _remove_all_lp(self, ctx: DiscoContext) -> None:
        """Remove all LP positions from all pools."""
        uid, gid = ctx.author.id, ctx.guild_id
        pools = await ctx.db.get_all_pools(gid)
        if not pools:
            await ctx.reply_error("No pools exist.")
            return

        # Find all user LP positions (skip locked ones). Carry the raw shares
        # alongside the human value -- the DB guard compares raw amounts and
        # to_raw(to_human(raw)) can overshoot by a few base units near float64
        # precision limits, which would reject an "all" removal spuriously.
        positions: list[tuple[dict, float, float, int]] = []  # (pool, shares, share_pct, shares_raw)
        lp_locked: list[str] = []
        now_ts = time.time()
        for p in pools:
            lp_pos = await ctx.db.get_user_lp(uid, gid, p["pool_id"])
            if lp_pos and to_human(lp_pos["lp_shares"]) > 0 and to_human(p["total_lp"]) > 0:
                _added_raw = lp_pos.get("added_at")
                added_at = _added_raw.timestamp() if hasattr(_added_raw, "timestamp") else float(_added_raw or 0)
                if added_at > 0 and now_ts - added_at < Config.LP_LOCK_SECONDS:
                    remaining_lock = int(Config.LP_LOCK_SECONDS - (now_ts - added_at))
                    lp_locked.append(f"**{p['token_a']}/{p['token_b']}**: locked ({remaining_lock // 60}m remaining)")
                    continue
                # Opt-in time-lock check (same semantics as single-pool removelp):
                # an active lock makes the position ineligible for .pool remove everything.
                cur_tier = int(lp_pos.get("lock_tier") or 0)
                _lu = lp_pos.get("locked_until")
                _lu_ts = _lu.timestamp() if hasattr(_lu, "timestamp") else float(_lu or 0)
                if cur_tier > 0 and _lu_ts and now_ts < _lu_ts:
                    remaining = int(_lu_ts - now_ts)
                    days_left = remaining // 86400
                    hrs_left  = (remaining % 86400) // 3600
                    tier_lbl = Config.LP_LOCK_TIERS.get(cur_tier, {}).get("label", f"tier {cur_tier}")
                    lp_locked.append(
                        f"**{p['token_a']}/{p['token_b']}**: **{tier_lbl}** time-lock "
                        f"({days_left}d {hrs_left}h remaining)"
                    )
                    continue
                shares_raw_user = int(lp_pos["lp_shares"])
                shares = to_human(shares_raw_user)
                pct = shares / to_human(p["total_lp"]) * 100
                positions.append((p, shares, pct, shares_raw_user))

        if not positions:
            lock_str = "\n".join(lp_locked) if lp_locked else ""
            await ctx.reply_error(f"You have no eligible LP positions to remove.{chr(10) + lock_str if lock_str else ''}")
            return

        # Build preview with gas info
        from cogs.validators import gas_fee_for_network
        all_tokens = await ctx.db.get_all_tokens_for_guild(gid)
        price_cache: dict[str, float] = {"USD": 1.0}
        for p, shares, pct, _shares_raw in positions:
            for sym in (p["token_a"], p["token_b"]):
                if sym not in price_cache:
                    r = await ctx.db.get_price(sym, gid)
                    price_cache[sym] = float(r["price"]) if r else 0.0

        # Pre-calculate gas for each position
        gas_info: list[dict] = []
        total_gas_by_coin: dict[str, float] = {}
        for p, shares, pct, _shares_raw in positions:
            ca, cb = p["token_a"], p["token_b"]
            pool_network = all_tokens.get(ca, {}).get("network") or all_tokens.get(cb, {}).get("network") or ""
            g_fee = 0.0
            g_coin = ""
            g_emoji = ""
            net_short = _NET_SHORT.get(pool_network, "")
            if pool_network:
                active_v = [v for v in await ctx.db.get_pos_validators_for_network(gid, pool_network) if v["is_active"]]
                if active_v:
                    g_coin, g_fee = await gas_fee_for_network(ctx.db, gid, "removelp", "medium", pool_network)
                    g_cfg = Config.TOKENS.get(g_coin, {})
                    g_emoji = g_cfg.get("emoji", "●")
            gas_info.append({"fee": g_fee, "coin": g_coin, "emoji": g_emoji, "network": pool_network, "net_short": net_short})
            if g_fee > 0:
                total_gas_by_coin[g_coin] = total_gas_by_coin.get(g_coin, 0.0) + g_fee

        lines = []
        total_value = 0.0
        for (p, shares, pct, _shares_raw_user), gi in zip(positions, gas_info):
            # LP exit preview in raw int space; convert to human only for display.
            _tlp_raw = int(p["total_lp"])
            _shares_raw = _shares_raw_user
            if _tlp_raw > 0:
                out_a_raw = int(p["reserve_a"]) * _shares_raw // _tlp_raw
                out_b_raw = int(p["reserve_b"]) * _shares_raw // _tlp_raw
            else:
                out_a_raw = out_b_raw = 0
            out_a = to_human(out_a_raw)
            out_b = to_human(out_b_raw)
            val = out_a * price_cache.get(p["token_a"], 0) + out_b * price_cache.get(p["token_b"], 0)
            total_value += val
            line = (
                f"**{p['token_a']}/{p['token_b']}**: {fmt_token(shares, 'LP')} ({pct:.1f}%) "
                f"→ {fmt_token(out_a, p['token_a'])} + {fmt_token(out_b, p['token_b'])} ≈ {fmt_usd(val)}"
            )
            if gi["fee"] > 0:
                line += f"  ⛽ {fmt_gas(gi['fee'], gi['coin'], gi['emoji'])}"
            lines.append(line)

        desc = (
            f"Removing **all** LP positions:\n\n"
            + "\n".join(lines)
            + f"\n\n**Total estimated value:** {fmt_usd(total_value)}"
        )
        if total_gas_by_coin:
            gas_summary = " + ".join(f"**{fmt_gas(v, k, Config.TOKENS.get(k, {}).get('emoji', ''))}**" for k, v in total_gas_by_coin.items())
            desc += f"\n⛽ **Total gas:** {gas_summary}"
        if lp_locked:
            desc += "\n\n**Still locked:**\n" + "\n".join(lp_locked)

        confirm_embed = card("🌊 Remove All Liquidity", description=desc, color=C_AMBER).footer("Confirm within 30 seconds").build()
        view = ConfirmView(ctx.author.id, timeout=30)
        msg = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
        view.message = msg
        confirmed = await view.wait_result()
        if confirmed is not True:
            try:
                await msg.edit(embed=card("🌊 Removal Cancelled", color=C_NEUTRAL).build(), view=None)
            except Exception:
                pass
            return

        # Execute removals with gas
        result_lines = []
        total_gas_paid: dict[str, float] = {}
        for (p, shares, pct, shares_raw_user), gi in zip(positions, gas_info):
            pool_id = p["pool_id"]
            ca, cb = p["token_a"], p["token_b"]
            g_fee = gi["fee"]
            g_coin = gi["coin"]
            g_emoji = gi["emoji"]
            net_short = gi["net_short"]
            try:
                # Check gas balance
                if g_fee > 0:
                    gas_h = (
                        await ctx.db.get_wallet_holding(uid, gid, net_short, g_coin) if net_short
                        else await ctx.db.get_holding(uid, gid, g_coin)
                    )
                    gas_bal = to_human(gas_h["amount"]) if gas_h else 0.0
                    if gas_bal < g_fee:
                        result_lines.append(f"**{ca}/{cb}**: skipped (need {fmt_gas(g_fee, g_coin, g_emoji)} gas)")
                        continue

                pool = await ctx.db.get_pool(pool_id, gid)
                pool_total_lp_raw = int(pool["total_lp"]) if pool else 0
                if not pool or pool_total_lp_raw <= 0:
                    result_lines.append(f"**{ca}/{cb}**: skipped (pool empty)")
                    continue
                _ptlp_h = to_human(pool_total_lp_raw)
                # Re-read the user's LP shares and use the raw value verbatim
                # for "all" removals, capped at the current live balance. Avoids
                # the float round-trip overshoot that produced spurious
                # "Insufficient LP shares" errors on exact-balance exits.
                _live_lp = await ctx.db.get_user_lp(uid, gid, pool_id)
                _live_raw = int(_live_lp["lp_shares"]) if _live_lp else 0
                shares_raw = min(shares_raw_user, _live_raw)
                if shares_raw <= 0:
                    result_lines.append(f"**{ca}/{cb}**: skipped (no shares held)")
                    continue
                reserve_a_raw = int(pool["reserve_a"])
                reserve_b_raw = int(pool["reserve_b"])

                # Whale cap re-check: skip removal if it would push another LP > cap.
                # Compare in raw int space so a near-max concentration is not
                # misjudged by a float rounding.
                _post_total_raw = pool_total_lp_raw - shares_raw
                _whale_block = False
                if _post_total_raw > 0:
                    _all_lps = await ctx.db.get_pool_lp_positions(str(pool_id), gid)
                    _cap_num = int(Config.LP_MAX_CONCENTRATION * 10**9)
                    _cap_den = 10**9
                    for _lpp in _all_lps:
                        if int(_lpp.get("user_id", 0)) == uid:
                            continue
                        _other_raw = int(_lpp.get("lp_shares", 0) or 0)
                        if _other_raw <= 0:
                            continue
                        # _other_raw / _post_total_raw > cap  <=>
                        #   _other_raw * cap_den > _post_total_raw * cap_num
                        if _other_raw * _cap_den > _post_total_raw * _cap_num:
                            _whale_block = True
                            break
                if _whale_block:
                    result_lines.append(
                        f"**{ca}/{cb}**: skipped (would push another LP above "
                        f"{Config.LP_MAX_CONCENTRATION*100:.0f}% whale cap)"
                    )
                    continue

                # LP exit math in raw int space:
                #   out_a_raw = reserve_a_raw * shares_raw / pool_total_lp_raw
                # then subtract from reserves exactly so no float rounding drift
                # accumulates in the pool row.
                out_a_raw = reserve_a_raw * shares_raw // pool_total_lp_raw
                out_b_raw = reserve_b_raw * shares_raw // pool_total_lp_raw
                out_a = to_human(out_a_raw)
                out_b = to_human(out_b_raw)

                await ctx.db.update_lp_position(uid, gid, pool_id, -shares_raw)
                await ctx.db.update_pool_reserves(
                    pool_id, gid,
                    reserve_a_raw - out_a_raw,
                    reserve_b_raw - out_b_raw,
                    pool_total_lp_raw - shares_raw,
                )
                await self._credit(ctx, ca, out_a, net_short)
                await self._credit(ctx, cb, out_b, net_short)

                # Deduct gas
                if g_fee > 0:
                    if net_short:
                        await ctx.db.update_wallet_holding(uid, gid, net_short, g_coin, to_raw(-g_fee))
                    else:
                        await ctx.db.update_holding(uid, gid, g_coin, to_raw(-g_fee))
                    total_gas_paid[g_coin] = total_gas_paid.get(g_coin, 0.0) + g_fee

                # Log transaction (mirrors single removelp)
                tx_hash = await ctx.db.log_tx(
                    gid, uid, "REMOVELP",
                    symbol_in=f"{ca}/{cb}", amount_in=to_raw(shares),
                    network=net_short,
                    gas_fee=to_raw(g_fee) if g_fee > 0 else 0, gas_coin=g_coin,
                )

                # Contribute gas to network vault for server level
                if g_fee > 0:
                    if net_short:
                        await deposit_to_vault(ctx.db, gid, net_short, g_fee, bot=ctx.bot)

                net_label = f" [{gi['network']}]" if gi.get("network") else ""
                line = f"**{ca}/{cb}**{net_label}: +{fmt_token(out_a, ca)} +{fmt_token(out_b, cb)}"
                if g_fee > 0:
                    line += f"  ⛽ -{fmt_gas(g_fee, g_coin, g_emoji)}"
                result_lines.append(line)
            except Exception as exc:
                result_lines.append(f"**{ca}/{cb}**: failed ({str(exc)[:50]})")

        result_desc = "\n".join(result_lines) or "Nothing removed."
        if total_gas_paid:
            gas_total_str = " + ".join(f"**{fmt_gas(v, k, Config.TOKENS.get(k, {}).get('emoji', ''))}**" for k, v in total_gas_paid.items())
            result_desc += f"\n\n⛽ **Total gas paid:** {gas_total_str}"

        result_embed = (
            card("🌊 Remove All Liquidity - Complete", color=C_TEAL)
            .description(result_desc)
            .build()
        )
        try:
            await msg.edit(embed=result_embed, view=None)
        except Exception:
            await ctx.reply(embed=result_embed, mention_author=False)

    # ── pool lock / unlock (time-lock boost) ────────────────────────────────

    @pool.command(name="lock")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def pool_lock(
        self, ctx: DiscoContext, token_a: str, token_b: str, days: int,
    ) -> None:
        """Time-lock an LP position for a Liqstone-XP multiplier.

        Usage: `.pool lock TOKEN_A TOKEN_B 7|30|90`. Longer locks boost
        harder. Extending a lock is allowed (pick a higher tier or the
        same one to restart the timer); downgrading is not.
        """
        token_a, token_b = token_a.upper(), token_b.upper()
        pool_id, ca, cb = ctx.db.make_pool_id(token_a, token_b)

        # Map days -> tier via Config.LP_LOCK_TIERS so the mapping stays
        # in one place.
        tier = next(
            (t for t, cfg in Config.LP_LOCK_TIERS.items() if int(cfg["days"]) == int(days)),
            None,
        )
        if tier is None:
            valid = ", ".join(str(cfg["days"]) for cfg in Config.LP_LOCK_TIERS.values())
            await ctx.reply_error(f"Lock duration must be one of: **{valid}** days.")
            return

        lp_pos = await ctx.db.get_user_lp(ctx.author.id, ctx.guild_id, pool_id)
        if not lp_pos or int(lp_pos.get("lp_shares") or 0) <= 0:
            await ctx.reply_error(f"You have no LP position in **{ca}/{cb}**.")
            return

        cur_tier = int(lp_pos.get("lock_tier") or 0)
        if cur_tier > tier:
            cur_label = Config.LP_LOCK_TIERS[cur_tier]["label"]
            await ctx.reply_error(
                f"Your existing **{cur_label}** lock is stronger than **{days}d**. "
                f"Either pick a longer duration or `.pool unlock {ca} {cb}` first "
                f"(costs {Config.LP_EARLY_UNLOCK_BURN*100:.0f}% of your LP shares)."
            )
            return

        tier_cfg = Config.LP_LOCK_TIERS[tier]
        import datetime as _dt
        locked_until = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=int(tier_cfg["days"]))
        await ctx.db.set_lp_lock(
            ctx.author.id, ctx.guild_id, pool_id, tier, locked_until,
        )

        xp_mult = float(tier_cfg["xp_mult"])
        embed = card(
            f"🔒 LP Locked  -  {ca}/{cb}",
            description=(
                f"Locked **{lp_pos.h('lp_shares'):,.4f}** LP for **{tier_cfg['label']}**.\n"
                f"Liqstone XP on this position: **{xp_mult:.2f}x** while locked.\n"
                f"Unlocks **{fmt_ts(locked_until.timestamp())}**.\n"
                f"Early unlock burns **{Config.LP_EARLY_UNLOCK_BURN*100:.0f}%** of your shares."
            ),
            color=C_PURPLE,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @pool.command(name="unlock")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3)
    async def pool_unlock(
        self, ctx: DiscoContext, token_a: str, token_b: str,
    ) -> None:
        """Break an LP lock early. Burns LP_EARLY_UNLOCK_BURN of your shares."""
        token_a, token_b = token_a.upper(), token_b.upper()
        pool_id, ca, cb = ctx.db.make_pool_id(token_a, token_b)

        lp_pos = await ctx.db.get_user_lp(ctx.author.id, ctx.guild_id, pool_id)
        if not lp_pos or int(lp_pos.get("lp_shares") or 0) <= 0:
            await ctx.reply_error(f"You have no LP position in **{ca}/{cb}**.")
            return

        cur_tier = int(lp_pos.get("lock_tier") or 0)
        if cur_tier == 0:
            await ctx.reply_error(f"Your **{ca}/{cb}** LP is not locked.")
            return

        _lu = lp_pos.get("locked_until")
        _lu_ts = _lu.timestamp() if hasattr(_lu, "timestamp") else float(_lu or 0)
        if _lu_ts and time.time() >= _lu_ts:
            # Lock already expired naturally -- clear without penalty.
            await ctx.db.clear_lp_lock(ctx.author.id, ctx.guild_id, pool_id)
            await ctx.reply_success(
                f"Lock on **{ca}/{cb}** had already expired; cleared with no penalty.",
                title="✅ Unlocked",
            )
            return

        # Compute share burn in raw int space so pool.total_lp stays exact.
        shares_raw = int(lp_pos.get("lp_shares") or 0)
        # int(shares * burn_pct) rounds toward zero, which is the defender-
        # friendly direction (user loses slightly less than 10% at rounding
        # boundaries). Acceptable.
        burn_raw = int(shares_raw * Config.LP_EARLY_UNLOCK_BURN)
        burn_h = to_human(burn_raw)

        confirmed = await ctx.confirm(
            f"Break **{ca}/{cb}** lock early?\n\n"
            f"This burns **{burn_h:,.4f}** LP (`{Config.LP_EARLY_UNLOCK_BURN*100:.0f}%` "
            f"of your position) -- other LPs in the pool gain value, and the lock resets "
            f"to tier 0. The remaining **{to_human(shares_raw - burn_raw):,.4f}** LP stays "
            f"in the pool and is removable normally.",
        )
        if not confirmed:
            return

        # Wrap the burn + lock-clear in a single atomic block so a DB hiccup
        # between steps can never leave a user with shares burned but the
        # lock still active (or the inverse: lock cleared without the burn).
        try:
            async with ctx.db.atomic():
                if burn_raw > 0:
                    pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
                    if not pool:
                        await ctx.reply_error("Pool disappeared mid-transaction. Try again.")
                        return
                    new_total_raw = max(0, int(pool["total_lp"]) - burn_raw)
                    # Burn = remove shares from user AND pool.total_lp,
                    # reserves untouched. Other LPs' fractional claim grows
                    # as a result.
                    await ctx.db.update_lp_position(
                        ctx.author.id, ctx.guild_id, pool_id, -burn_raw,
                    )
                    await ctx.db.update_pool_reserves(
                        pool_id, ctx.guild_id,
                        int(pool["reserve_a"]), int(pool["reserve_b"]), new_total_raw,
                    )
                await ctx.db.clear_lp_lock(ctx.author.id, ctx.guild_id, pool_id)
        except Exception:
            log.exception("pool_unlock: atomic burn+clear failed for %s %s", ctx.author.id, pool_id)
            await ctx.reply_error("Unlock failed due to a storage error. Your position is unchanged; please try again.")
            return

        embed = card(
            f"🗝 Lock Broken  -  {ca}/{cb}",
            description=(
                f"Burned **{burn_h:,.4f}** LP. Lock cleared.\n"
                f"You can `.pool remove {ca} {cb} <shares|all>` now."
            ),
            color=C_WARNING,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── pool remove (removelp) ─────────────────────────────────────────────

    @pool.command(name="remove", aliases=["removelp", "rmlp"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(5)
    async def removelp(self, ctx: DiscoContext, token_a: str, token_b: str = "", lp_shares: str = "") -> None:
        """Remove liquidity from a pool. Usage: $removelp TOKEN_A TOKEN_B shares|all
        Or: $pool remove everything  -  remove all LP from all pools."""
        if token_a.lower() == "everything" and not token_b:
            await self._remove_all_lp(ctx)
            return
        if not token_b or not lp_shares:
            await ctx.reply_error("Usage: `.pool remove <TOKEN_A> <TOKEN_B> <shares|all>`\nOr `.pool remove everything`")
            return
        token_a, token_b = token_a.upper(), token_b.upper()
        pool_id, ca, cb = ctx.db.make_pool_id(token_a, token_b)

        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if not pool:
            await ctx.reply_error(f"Pool **{ca}/{cb}** doesn't exist.")
            return
        if pool.get("vault_locked"):
            await ctx.reply_error(
                f"**{ca}/{cb}** is a vault-locked group token pool. "
                "Players cannot remove liquidity."
            )
            return

        lp_pos      = await ctx.db.get_user_lp(ctx.author.id, ctx.guild_id, pool_id)
        # Keep the raw (DB-scale) balance too; to_human->to_raw round-trip can
        # overshoot by a few base units at float64 precision, which makes the
        # DB-side `lp_shares + delta >= 0` guard reject an "all" removal with
        # "Insufficient LP shares". Using raw directly for "all" avoids this.
        user_shares_raw = int(lp_pos["lp_shares"]) if lp_pos else 0
        user_shares = to_human(user_shares_raw)

        _all_shares = lp_shares.lower() == "all"
        if _all_shares:
            shares = user_shares
        else:
            try:
                shares = float(lp_shares)
            except ValueError:
                await ctx.reply_error("Shares must be a number or `all`.")
                return
            if not math.isfinite(shares):
                await ctx.reply_error("Shares must be a finite number.")
                return

        if shares <= 0 or user_shares == 0:
            await ctx.reply_error("You have no LP shares in this pool.")
            return
        if shares > user_shares:
            await ctx.reply_error(f"You only have **`{fmt_token(user_shares, 'LP')}`** LP shares.")
            return
        _pool_total_lp_h = to_human(pool["total_lp"])
        if _pool_total_lp_h <= 0:
            await ctx.reply_error("Pool has no liquidity.")
            return

        # Charge gas if validators are active on this pool's network
        from cogs.validators import gas_fee_for_network
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        pool_network = all_tokens.get(ca, {}).get("network") or all_tokens.get(cb, {}).get("network") or ""
        gas_fee = 0.0
        gas_coin = ""
        gas_em = ""
        if pool_network:
            active_v = [v for v in await ctx.db.get_pos_validators_for_network(ctx.guild_id, pool_network) if v["is_active"]]
            if active_v:
                gas_coin, gas_fee = await gas_fee_for_network(ctx.db, ctx.guild_id, "removelp", "medium", pool_network)
                gas_cfg = Config.TOKENS.get(gas_coin, {})
                gas_em = gas_cfg.get("emoji", "●")
                _rmlp_net = _NET_SHORT.get(pool_network, "")
                if _rmlp_net:
                    gas_h = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, _rmlp_net, gas_coin)
                else:
                    gas_h = await ctx.db.get_holding(ctx.author.id, ctx.guild_id, gas_coin)
                gas_bal = to_human(gas_h["amount"]) if gas_h else 0.0
                if gas_bal < gas_fee:
                    await ctx.reply_error(f"Need **`{fmt_gas(gas_fee, gas_coin, gas_em)}`** for gas. You have **`{fmt_gas(gas_bal, gas_coin, gas_em)}`**.")
                    return

        # LP lock period check
        if lp_pos:
            _added_raw = lp_pos.get("added_at")
            added_at = (
                _added_raw.timestamp() if hasattr(_added_raw, "timestamp") else float(_added_raw or 0)
            )
            if added_at > 0 and time.time() - added_at < Config.LP_LOCK_SECONDS:
                remaining_lock = int(Config.LP_LOCK_SECONDS - (time.time() - added_at))
                await ctx.reply_error(
                    f"LP locked for {remaining_lock // 60} more minutes. "
                    f"Minimum hold: {Config.LP_LOCK_SECONDS // 3600}h after adding."
                )
                return

            # Opt-in time-lock (separate from the 2h anti-churn gate above).
            # An active lock blocks removal entirely -- user must call
            # `.pool unlock` to burn LP_EARLY_UNLOCK_BURN of their shares.
            cur_tier = int(lp_pos.get("lock_tier") or 0)
            _lu = lp_pos.get("locked_until")
            _lu_ts = _lu.timestamp() if hasattr(_lu, "timestamp") else float(_lu or 0)
            if cur_tier > 0 and _lu_ts and time.time() < _lu_ts:
                remaining = int(_lu_ts - time.time())
                days_left = remaining // 86400
                hrs_left  = (remaining % 86400) // 3600
                tier_lbl = Config.LP_LOCK_TIERS.get(cur_tier, {}).get("label", f"tier {cur_tier}")
                await ctx.reply_error(
                    f"This position has an active **{tier_lbl}** lock "
                    f"({days_left}d {hrs_left}h remaining). Use "
                    f"`.pool unlock {token_a} {token_b}` to break it "
                    f"(costs {Config.LP_EARLY_UNLOCK_BURN*100:.0f}% of your LP shares)."
                )
                return

        # Large removal throttle
        frac = shares / _pool_total_lp_h if _pool_total_lp_h > 0 else 1.0
        if frac > Config.LP_LARGE_REMOVAL_THRESHOLD:
            throttle_key = (ctx.author.id, ctx.guild_id, str(pool["pool_id"]))
            last_removal = _user_large_lp_removal.get(throttle_key, 0.0)
            if time.time() - last_removal < Config.LP_LARGE_REMOVAL_COOLDOWN:
                remaining_cd = int(Config.LP_LARGE_REMOVAL_COOLDOWN - (time.time() - last_removal))
                await ctx.reply_error(
                    f"Large LP removal cooldown: wait {remaining_cd // 60} more minutes. "
                    f"Split into smaller removals or wait."
                )
                return

        # LP whale-cap re-check on removal: shrinking total_lp can push another
        # holder above LP_MAX_CONCENTRATION. Block the removal if it would
        # leave any *other* LP above the cap so the whale can't be trapped
        # passively by someone else exiting first.
        _post_total_lp = _pool_total_lp_h - shares
        if _post_total_lp > 0:
            _all_lps = await ctx.db.get_pool_lp_positions(str(pool["pool_id"]), ctx.guild_id)
            for _lpp in _all_lps:
                if int(_lpp.get("user_id", 0)) == ctx.author.id:
                    continue
                _other_h = _lpp.h("lp_shares")
                if _other_h <= 0:
                    continue
                _other_post = _other_h / _post_total_lp
                if _other_post > Config.LP_MAX_CONCENTRATION:
                    _max_safe = max(
                        0.0,
                        _pool_total_lp_h - (_other_h / Config.LP_MAX_CONCENTRATION),
                    )
                    _max_safe_str = (
                        f"At most **`{fmt_token(_max_safe, 'LP')}`** can be removed right now."
                        if _max_safe > 0 else
                        "No removals possible until another LP reduces their position."
                    )
                    await ctx.reply_error(
                        f"This removal would push another liquidity provider to "
                        f"**{_other_post*100:.1f}%** of the pool, above the "
                        f"{Config.LP_MAX_CONCENTRATION*100:.0f}% whale cap. {_max_safe_str}"
                    )
                    return

        # LP exit math in raw int space so pool reserves stay exact and the
        # confirm/execute phases see the same numbers (no float rounding drift).
        pool_total_lp_raw = int(pool["total_lp"])
        reserve_a_raw = int(pool["reserve_a"])
        reserve_b_raw = int(pool["reserve_b"])
        # For "all" removals use the raw LP balance verbatim; otherwise convert
        # the human-entered number and cap it at the actual raw balance so a
        # float round-trip cannot produce a deduction larger than what is held.
        if _all_shares:
            shares_raw = user_shares_raw
        else:
            shares_raw = min(to_raw(shares), user_shares_raw)
        out_a_raw = reserve_a_raw * shares_raw // pool_total_lp_raw if pool_total_lp_raw > 0 else 0
        out_b_raw = reserve_b_raw * shares_raw // pool_total_lp_raw if pool_total_lp_raw > 0 else 0
        out_a = to_human(out_a_raw)
        out_b = to_human(out_b_raw)
        frac  = shares / _pool_total_lp_h if _pool_total_lp_h > 0 else 0.0

        # ── Estimate remaining earnings after removal ─────────────────────────
        remaining_shares  = user_shares - shares
        remaining_pct     = (remaining_shares / (_pool_total_lp_h - shares) * 100) if (_pool_total_lp_h - shares) > 0 else 0.0
        current_share_pct = (shares / _pool_total_lp_h * 100) if _pool_total_lp_h > 0 else 0.0

        # ── Confirmation ─────────────────────────────────────────────────────
        _b = (
            card(
                f"⚠️ Confirm Remove Liquidity  -  {ca}/{cb}",
                description="Review before withdrawing. You will stop earning fees on removed shares.",
                color=C_AMBER,
            )
            .field("LP Shares Burned", f"**`{fmt_token(shares, 'LP')}`**",   True)
            .field("Pool Share",        f"**{fmt_pct(current_share_pct)}**",  True)
            .field(f"Receive {ca}",    f"**`{fmt_token(out_a, ca)}`**",       True)
            .field(f"Receive {cb}",    f"**`{fmt_token(out_b, cb)}`**",       True)
        )
        if remaining_shares > 0:
            _b.field("Remaining Shares", f"**`{fmt_token(remaining_shares, 'LP')}`** ({fmt_pct(remaining_pct)})", True)
        if gas_fee > 0:
            _b.field("Gas Fee", f"**`{fmt_gas(gas_fee, gas_coin, gas_em)}`**", True)
        conf_embed = _b.footer("Removing liquidity stops fee earnings on the withdrawn shares.").build()
        view = ConfirmView(ctx.author.id)
        conf_msg = await ctx.reply(embed=conf_embed, view=view, mention_author=False)
        confirmed = await view.wait_result()
        await conf_msg.edit(view=None)
        if not confirmed:
            cancel = card("", description="Liquidity removal cancelled.", color=C_NEUTRAL).build()
            await conf_msg.edit(embed=cancel)
            return

        _rmlp_net = _NET_SHORT.get(pool_network, "")
        # Same per-token fix as the addlp branch: each side of a
        # cross-network pool gets credited to its OWN wallet network.
        # Using ``_rmlp_net`` for both sides on a CHEF/MOON withdrawal
        # used to drop MOON into network='arc' wallet_holdings, which
        # then disappeared from the user's Moon Network DeFi view.
        _rm_net_a = _resolve_token_wallet_net(
            ca, all_tokens.get(ca, {}).get("network", "")
        ) or _rmlp_net
        _rm_net_b = _resolve_token_wallet_net(
            cb, all_tokens.get(cb, {}).get("network", "")
        ) or _rmlp_net
        if gas_fee > 0:
            if _rmlp_net:
                await ctx.db.update_wallet_holding(ctx.author.id, ctx.guild_id, _rmlp_net, gas_coin, to_raw(-gas_fee))
            else:
                await ctx.db.update_holding(ctx.author.id, ctx.guild_id, gas_coin, to_raw(-gas_fee))

        await ctx.db.update_lp_position(ctx.author.id, ctx.guild_id, pool_id, -shares_raw)
        await ctx.db.update_pool_reserves(
            pool_id, ctx.guild_id,
            reserve_a_raw - out_a_raw,
            reserve_b_raw - out_b_raw,
            pool_total_lp_raw - shares_raw,
        )
        await self._credit(ctx, ca, out_a, _rm_net_a)
        await self._credit(ctx, cb, out_b, _rm_net_b)

        if frac > Config.LP_LARGE_REMOVAL_THRESHOLD:
            _user_large_lp_removal[(ctx.author.id, ctx.guild_id, str(pool["pool_id"]))] = time.time()

        if user_shares - shares <= 0:
            await ctx.db.delete_lp_snapshot(ctx.author.id, ctx.guild_id, pool_id)

        tx_hash = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "REMOVELP",
            symbol_in=f"{ca}/{cb}", amount_in=to_raw(shares),
            network=_NET_SHORT.get(pool_network, ""),
            gas_fee=to_raw(gas_fee) if gas_fee > 0 else 0, gas_coin=gas_coin,
        )
        await ctx.bot.bus.publish("lp_removed", guild=ctx.guild, user=ctx.author,
            pool_id=pool_id, lp_burned=shares, tx_hash=tx_hash,
            amount_a=out_a, amount_b=out_b, token_a=ca, token_b=cb,
            gas_fee=gas_fee, gas_coin=gas_coin)
        _usd_a = await _whale.usd_value_of(ctx.bot, ca, out_a, ctx.guild_id)
        _usd_b = await _whale.usd_value_of(ctx.bot, cb, out_b, ctx.guild_id)
        await _whale.check(ctx.bot, ctx.guild, ctx.author.id, "removelp", _usd_a + _usd_b, symbol_in=ca, symbol_out=cb, amount_in=out_a, amount_out=out_b)

        _b = (
            card("🌊 Liquidity Removed", description=f"Pool **{ca}/{cb}**  -  withdrawal complete", color=C_WARNING)
            .field(f"📤 Received {ca}",  f"**`{fmt_token(out_a, ca)}`**",       True)
            .field(f"📤 Received {cb}",  f"**`{fmt_token(out_b, cb)}`**",       True)
            .field("🔥 LP Burned",       f"**`{fmt_token(shares, 'LP')}`**",    True)
        )
        if gas_fee > 0:
            _b.field("⛽ Gas Fee", f"**`{fmt_gas(gas_fee, gas_coin, gas_em)}`**", True)
        embed = _b.build()
        set_tx(embed, ctx.guild_id, tx_hash)
        await ctx.reply(embed=embed, mention_author=False)

    # ── pool price ────────────────────────────────────────────────────────────

    @pool.command(name="price")
    @guild_only
    async def pool_price(self, ctx: DiscoContext, pair: str) -> None:
        """Get the price for a token or pair. Usage: .price ARC  or  .price ARCUSD  or  .price ARCDSC
        If a single symbol is given (e.g. .price ARC), defaults to showing USD price."""
        pair = pair.upper()
        valid = await self._valid_tokens(ctx.guild_id)
        all_syms = sorted(valid, key=len, reverse=True)

        # If pair is a bare symbol (not a compound like ARCUSD), default to SYMUSD
        if pair in valid:
            token_a, token_b = pair, "USD"
        else:
            token_a = token_b = None
            for sym in all_syms:
                if pair.startswith(sym) and pair[len(sym):] in valid:
                    token_a = sym
                    token_b = pair[len(sym):]
                    break
            if not token_a or not token_b:
                # Last resort: check if it's a custom token with just an oracle price
                all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
                if pair in all_tokens:
                    token_a, token_b = pair, "USD"
                else:
                    await ctx.reply_error(
                        f"Can't parse pair `{pair}`. Try `.price {pair}` (single token) or `.price {pair}USD`."
                    )
                    return

        if token_b == "USD":
            all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
            row = await ctx.db.get_price(token_a, ctx.guild_id)
            if row:
                pct = (float(row["price"]) - float(row["open_price"])) / float(row["open_price"]) * 100 if row["open_price"] else 0
                tok_info = all_tokens.get(token_a, {})
                arrow_p = "▲" if pct >= 0 else "▼"
                _b = (
                    card(
                        f"{Config.currency_label(token_a, detail=True)} / USD",
                        description=f"💵 **{fmt_usd(row['price'])}**  {arrow_p} **{pct:+.2f}%**",
                        color=C_BUY if pct >= 0 else C_SELL,
                    )
                    .field("📈 24h High",  f"`{fmt_usd(row['day_high'])}`",  True)
                    .field("📉 24h Low",   f"`{fmt_usd(row['day_low'])}`",   True)
                    .field("📊 24h Change", f"{'📈' if pct >= 0 else '📉'} **{fmt_pct(pct)}**", True)
                )
                if tok_info.get("network"):
                    _b.field("🌐 Network", tok_info["network"], True)
                embed = _b.build()
                await ctx.reply(embed=embed, mention_author=False)
                return
            await ctx.reply_error(f"No price data for `{token_a}`.")
            return

        pool_id, ca, cb = ctx.db.make_pool_id(token_a, token_b)
        pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if not pool or pool["reserve_a"] <= 0:
            await ctx.reply_error(f"No pool found for **{token_a}/{token_b}**.")
            return

        _ra_h = to_human(pool["reserve_a"])
        _rb_h = to_human(pool["reserve_b"])
        derived = _rb_h / _ra_h if _ra_h > 0 and ca == token_a else (_ra_h / _rb_h if _rb_h > 0 else 0)
        k = _ra_h * _rb_h
        embed = (
            card(
                f"🌊 {token_a} / {token_b} · AMM Pool",
                description=f"💱 **1 {token_a} = `{fmt_token(derived, token_b)}`**",
                color=C_TEAL,
            )
            .field(f"📦 {ca} Reserve",  f"`{fmt_token(_ra_h, ca)}`",               True)
            .field(f"📦 {cb} Reserve",  f"`{fmt_token(_rb_h, cb)}`",               True)
            .field("🔢 Pool Depth (k)", f"`{k:,.2f}`",                                         True)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ══════════════════════════════════════════════════════════════════════════
    #  Backward-compat prefix stubs
    # ══════════════════════════════════════════════════════════════════════════

    @commands.command(name="swap", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def _swap_compat(self, ctx: DiscoContext, token_in: str = "", token_out: str = "", amount_in: str = "", *, flags: str = "") -> None:
        await ctx.invoke(self.swap, token_in=token_in, token_out=token_out, amount_in=amount_in, flags=flags)

    @commands.command(name="chart", hidden=True, aliases=["c"])
    async def _chart_compat(
        self, ctx: DiscoContext,
        pair: str = "", timeframe: str = "1h", *, args: str = "",
    ) -> None:
        await ctx.invoke(self.chart, pair=pair, timeframe=timeframe, args=args)

    @commands.command(name="portfolio", hidden=True, aliases=["port", "holdings"])
    @guild_only
    @no_bots
    @ensure_registered
    async def _portfolio_compat(self, ctx: DiscoContext) -> None:
        await ctx.invoke(self.portfolio)

    @commands.command(name="tokeninfo", hidden=True, aliases=["ti"])
    @guild_only
    async def _tokeninfo_compat(self, ctx: DiscoContext, symbol: str = "") -> None:
        if not symbol:
            await ctx.reply_error("Usage: `,tokeninfo <symbol>`  -  e.g. `,tokeninfo MTA`")
            return
        await ctx.invoke(self.tokeninfo, symbol=symbol)


    # ── Top-level LP dashboard ─────────────────────────────────────────────

    @commands.command(name="mylp", aliases=["mypool", "mypools", "myliquidity"])
    @guild_only
    @no_bots
    @ensure_registered
    async def mylp(self, ctx: DiscoContext) -> None:
        """Show your LP positions with current value, gain since deposit, and liqstone bonus."""
        uid, gid = ctx.author.id, ctx.guild_id
        positions = await ctx.db.get_user_lp_positions(uid, gid)
        if not positions:
            await ctx.reply_error("You have no active LP positions.")
            return

        liqstone = await ctx.db.get_liqstone(uid, gid)
        lq_bonus = _liqstone_stat(liqstone, "lp_reward_bonus")

        # Pre-fetch user-created-token symbols so each row can flag
        # whether it earns the user-LP work bonus + Liqstone XP multiplier.
        from services.liquidity import user_created_token_symbols
        from services.lp_yield import estimate_position_apr
        user_syms = await user_created_token_symbols(ctx.db, gid)

        # Build the per-position fields first, accumulate the totals, then
        # chunk fields across multiple embeds. A single embed could blow
        # Discord's 6000-char ceiling for users with many positions.
        position_fields: list[tuple[str, str]] = []
        total_usd = 0.0
        total_gain = 0.0
        total_user_usd = 0.0
        total_proj_daily = 0.0

        for lp in positions:
            if lp["total_lp"] <= 0:
                continue
            frac = float(lp["lp_shares"]) / float(lp["total_lp"])
            val_a = lp.h("reserve_a") * frac
            val_b = lp.h("reserve_b") * frac
            ta, tb = lp["token_a"], lp["token_b"]

            p_a = await ctx.db.get_price(ta, gid)
            p_b = await ctx.db.get_price(tb, gid)
            price_a = float(p_a["price"]) if p_a else 0.0
            price_b = float(p_b["price"]) if p_b else 0.0
            usd_val = val_a * price_a + val_b * price_b
            total_usd += usd_val

            snap = await ctx.db.get_lp_snapshot(uid, gid, lp["pool_id"])
            gain_a = gain_b = 0.0
            if snap:
                cur_a_per_lp = float(lp["reserve_a"]) / float(lp["total_lp"])
                cur_b_per_lp = float(lp["reserve_b"]) / float(lp["total_lp"])
                gain_a = max(0.0, (cur_a_per_lp - float(snap["entry_res_a_per_lp"])) * lp.h("lp_shares"))
                gain_b = max(0.0, (cur_b_per_lp - float(snap["entry_res_b_per_lp"])) * lp.h("lp_shares"))
            fees_usd_base = gain_a * price_a + gain_b * price_b
            fees_usd = fees_usd_base * (1.0 + lq_bonus)
            yield_paid_h = to_human(int(lp.get("yield_paid_usd_raw") or 0))
            gain_usd = fees_usd + yield_paid_h
            total_gain += gain_usd

            share_pct = frac * 100
            gain_pct = (gain_usd / max(usd_val - gain_usd, 1e-9)) * 100 if usd_val > 0 else 0.0
            gain_breakdown_bits: list[str] = []
            if fees_usd > 0:
                gain_breakdown_bits.append(f"fees {fmt_usd(fees_usd)}")
            if yield_paid_h > 0:
                gain_breakdown_bits.append(f"yield {fmt_usd(yield_paid_h)}")
            gain_breakdown = (
                f" ({' + '.join(gain_breakdown_bits)})"
                if gain_breakdown_bits else ""
            )
            gain_str = fmt_bonus(
                f"+{fmt_usd(gain_usd)} ({fmt_pct(gain_pct)}){gain_breakdown}",
                lq_bonus, "Liqstone",
            )
            since_str = f"  ·  Since {fmt_ts(lp['added_at'])}" if lp.get("added_at") else ""

            lock_line = ""
            _cur_tier = int(lp.get("lock_tier") or 0)
            _lu = lp.get("locked_until")
            _lu_ts = _lu.timestamp() if hasattr(_lu, "timestamp") else 0
            if _cur_tier > 0 and _lu_ts and time.time() < _lu_ts:
                _tier_cfg = Config.LP_LOCK_TIERS.get(_cur_tier, {})
                _xp_mult  = float(_tier_cfg.get("xp_mult", 1.0))
                _remaining = int(_lu_ts - time.time())
                _dleft = _remaining // 86400
                _hleft = (_remaining % 86400) // 3600
                lock_line = (
                    f"\n🔒 **{_tier_cfg.get('label', '?')}** lock  ·  "
                    f"**{_xp_mult:.2f}x** Liqstone XP  ·  "
                    f"{_dleft}d {_hleft}h left"
                )

            is_user_lp = bool(user_syms) and (ta in user_syms or tb in user_syms)
            title_prefix = "🌊 " if is_user_lp else "💧 "
            user_line = ""
            if is_user_lp:
                total_user_usd += usd_val
                user_line = (
                    f"\n🌊 User-token LP  ·  **{Config.USER_LP_LIQSTONE_MULT:.2f}x** "
                    f"Liqstone XP  ·  counts toward work/daily bonus"
                )

            yield_apr = estimate_position_apr(lp, user_syms)
            proj_daily_usd = usd_val * yield_apr / 365.0
            total_proj_daily += proj_daily_usd
            yield_paid_line = (
                f"  ·  Earned so far: **{fmt_usd(yield_paid_h)}**"
                if yield_paid_h > 0 else ""
            )
            yield_line = (
                f"\n💸 Yield: **{yield_apr*100:.1f}% APR**  ·  "
                f"≈ **{fmt_usd(proj_daily_usd)}**/day"
                f"{yield_paid_line}"
            )

            field_value = (
                f"Value: **{fmt_usd(usd_val)}**  ·  Gain: **{gain_str}**\n"
                f"{lp.h('lp_shares'):,.4f} LP ({share_pct:.1f}% of pool){since_str}\n"
                f"{val_a:,.4f} {ta} + {val_b:,.4f} {tb}"
                f"{yield_line}"
                f"{lock_line}"
                f"{user_line}"
            )
            # Discord field-value cap is 1024 chars. Truncate defensively
            # so a particularly verbose row never trips the per-field cap.
            if len(field_value) > 1020:
                field_value = field_value[:1017] + "..."
            position_fields.append((f"{title_prefix}{ta}/{tb}", field_value))

        total_gain_pct = (total_gain / max(total_usd - total_gain, 1e-9)) * 100 if total_usd > 0 else 0.0
        gain_note = f"  |  Gained: {fmt_usd(total_gain)} ({fmt_pct(total_gain_pct)})"
        lq_note = f"  |  💎 Liqstone +{int(lq_bonus * 100)}% LP rewards" if lq_bonus else ""

        ulp_note = ""
        if total_user_usd > 0:
            from services.liquidity import user_lp_work_bonus_pct
            _ulp_pct = user_lp_work_bonus_pct(total_user_usd)
            ulp_note = (
                f"  |  🌊 User LP ${total_user_usd:,.0f} -> "
                f"+{_ulp_pct*100:.1f}% work/daily"
            )

        lifetime_yield_raw = await ctx.db.get_total_lp_yield_earned(uid, gid)
        lifetime_yield_h = to_human(lifetime_yield_raw)
        yield_summary_value = (
            f"You earn yield every {int(Config.LP_YIELD_TICK_HOURS)}h on every active LP position. "
            f"Lock positions for higher rates: 7d=+30%, 30d=+75%, 90d=+150%."
            f"\n💸 LP Yield: **{fmt_usd(total_proj_daily)}**/day projected  ·  "
            f"Lifetime: **{fmt_usd(lifetime_yield_h)}**"
        )
        footer_text = f"Total LP value: {fmt_usd(total_usd)}{gain_note}{lq_note}{ulp_note}"

        # Chunk position fields across embeds. Discord limits: 6000 chars
        # per embed, 25 fields per embed, 1024 chars per field. We pack
        # fields greedily under a 5000-char body budget (leaves headroom
        # for title + description + footer + per-field name overhead).
        FIELDS_BODY_BUDGET = 5000
        FIELDS_PER_EMBED_MAX = 20  # leaves room for the summary field

        def _emit_embed(
            chunk: list[tuple[str, str]],
            *,
            page_no: int,
            page_total: int,
            include_summary: bool,
        ) -> "discord.Embed":
            title = "💧 My LP Positions"
            if page_total > 1:
                title += f"  ({page_no}/{page_total})"
            eb = (
                card(title, color=C_TEAL)
                .author(
                    ctx.author.display_name,
                    icon_url=ctx.author.display_avatar.url,
                )
            )
            for nm, val in chunk:
                eb.field(nm, val, False)
            if include_summary:
                eb.field("📈 Yield Summary", yield_summary_value, False)
            eb.footer(footer_text)
            return eb.build()

        # Greedy pack into pages.
        pages_payload: list[list[tuple[str, str]]] = []
        cur: list[tuple[str, str]] = []
        cur_len = 0
        for nm, val in position_fields:
            entry_len = len(nm) + len(val) + 8  # cushion for field formatting
            if cur and (
                cur_len + entry_len > FIELDS_BODY_BUDGET
                or len(cur) >= FIELDS_PER_EMBED_MAX
            ):
                pages_payload.append(cur)
                cur = []
                cur_len = 0
            cur.append((nm, val))
            cur_len += entry_len
        if cur:
            pages_payload.append(cur)
        if not pages_payload:
            pages_payload = [[]]

        page_total = len(pages_payload)
        embeds = [
            _emit_embed(
                chunk,
                page_no=i + 1,
                page_total=page_total,
                include_summary=(i == page_total - 1),
            )
            for i, chunk in enumerate(pages_payload)
        ]
        if len(embeds) == 1:
            await ctx.reply(embed=embeds[0], mention_author=False)
        else:
            await ctx.paginate(embeds)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Trade(bot))
