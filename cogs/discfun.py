"""cogs/discfun.py  -  Disc.Fun proto-token launchpad (Pump.fun replica).

`,fun` is the public-facing command. Anyone can deploy a "proto token" on
the Discoin Network with a flat DFUN fee (no Protocol Dev tier gate). Each
proto trades against a virtual-DFUN bonding curve. Once the curve has
collected the graduation threshold of real DFUN, the proto is promoted to
a full guild token with a deep SYMBOL/DFUN pool plus a SYMBOL/DSC bridge,
and becomes swappable on the regular AMM.

Trade-off vs ,token deploy:

  ,token deploy   -- Protocol Dev tier gate. Deployer picks supply, price,
                     volatility, burn rate, transfer fee, network. Pays
                     gas in the native coin.
  ,fun deploy     -- No tier gate. Symbol / name / emoji only. Everything
                     else is fixed by ``Config.DISCFUN``. Cheap flat DFUN
                     fee. Curve graduates into a real token automatically.

Commands:
    ,fun                            -- group help / quick-start
    ,fun deploy SYMBOL "Name" 🚀    -- launch a proto token (interactive on partial)
    ,fun list [hot|new|progress|mcap]  -- browse active protos with sort
    ,fun info SYMBOL                -- live curve + buy/sell buttons
    ,fun buy SYMBOL <dfun>          -- buy proto tokens with DFUN
    ,fun sell SYMBOL <amount|all|%>  -- sell proto tokens back to the curve
    ,fun bag                        -- your active proto positions with PnL
    ,fun grads                      -- recently graduated tokens
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import discord
from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.fuzzy import suggest_subcommand
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.scale import SCALE, to_human, to_raw
from core.framework.ui import (
    C_AMBER, C_GOLD, C_INFO, C_NAVY, C_PURPLE, C_SUCCESS, ConfirmView, InputModal,
    fmt_token, fmt_ts,
)
from services import discfun as _df

log = logging.getLogger(__name__)

# Chart rendering -- mirrors cogs/trade.py's pipeline. Optional; if
# playwright isn't installed the chart command degrades to a graceful
# error instead of breaking the cog load.
try:
    from playwright.async_api import async_playwright as _async_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

_PROTO_TEMPLATE_PATH = Path(__file__).parent.parent / "charts" / "template_discfun.html"


async def _render_proto_chart(html_path: str) -> bytes:
    """Open the Disc.Fun chart HTML in headless Chromium, screenshot to PNG.

    Mirrors ``cogs.trade._render_chart`` exactly so the rendering pipeline
    is identical -- the only diff is the source template (purple/pink
    palette instead of green/red) so a viewer can immediately distinguish
    a proto chart from a regular AMM chart.
    """
    async with _async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1200, "height": 700})
        await page.goto(f"file:///{html_path}")
        await page.wait_for_timeout(500)
        screenshot = await page.screenshot(type="png")
        await browser.close()
    return screenshot


def _ema_simple(values: list[float], n: int) -> list[float | None]:
    """EMA helper sized for the proto chart's default overlay."""
    if not values or n <= 0:
        return []
    k = 2 / (n + 1)
    out: list[float | None] = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


# ── Parsing ─────────────────────────────────────────────────────────────────

_DEPLOY_RE = re.compile(
    r"""\A
        \s*(?P<symbol>[A-Za-z0-9]{1,8})\s+
        (?:"(?P<name_q>[^"]{1,32})"|(?P<name_p>[^\s"]{1,32}))
        # Emoji slot: a Discord custom-emoji mention <:name:id> /
        # <a:name:id> takes ~25-60 chars, so ``\S{1,4}`` was too tight
        # and silently dropped them. Allow up to 80 non-space chars and
        # let validate_emoji() decide what's acceptable.
        (?:\s+(?P<emoji>\S{1,80}))?
        \s*\Z
    """,
    re.VERBOSE,
)

_EDIT_RE = re.compile(
    r"""\A
        \s*(?P<symbol>[A-Za-z0-9]{1,8})
        (?:\s+(?:"(?P<name_q>[^"]{1,32})"|(?P<name_p>[^\s"]{1,32}\b)))?
        (?:\s+(?P<emoji>\S{1,80}))?
        \s*\Z
    """,
    re.VERBOSE,
)

_VALID_SORTS = {"new", "hot", "progress", "mcap"}


# ── Formatting helpers ──────────────────────────────────────────────────────

def _qsym() -> str:
    return _df.quote_symbol()


def _qem() -> str:
    return _df.quote_emoji()


_SUB_DIGITS = "₀₁₂₃₄₅₆₇₈₉"


def _subscript_int(n: int) -> str:
    return "".join(_SUB_DIGITS[int(c)] for c in str(n))


def _fmt_decimal_smart(value: float) -> str:
    """Render a positive float with no scientific notation.

    Mirrors pump.fun's subscript-zero notation for very small numbers so
    e.g. ``0.000001442`` renders as ``0.0₅1442`` (5 leading zeros + 4
    significant digits). Larger values use thousands-separated decimals
    with the precision sized to the magnitude:

        >= 1            -> 4 dp           ``1,234.5678``
        >= 1e-2         -> 6 dp           ``0.123456``
        >= 1e-4         -> 8 dp           ``0.00012345``
        else            -> subscript zero ``0.0₅1442``
    """
    if value <= 0:
        return "0"
    if value >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if value >= 1e-2:
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if value >= 1e-4:
        return f"{value:.8f}".rstrip("0").rstrip(".")
    # Tiny: count leading zeros, then 4 significant digits.
    s = f"{value:.20f}"
    _, _, frac = s.partition(".")
    zeros = 0
    for ch in frac:
        if ch == "0":
            zeros += 1
        else:
            break
    sig = (frac[zeros:zeros + 4]).rstrip("0") or "0"
    if zeros >= 4:
        return f"0.0{_subscript_int(zeros)}{sig}"
    return f"0.{'0' * zeros}{sig}"


def _short_num(value: float) -> str:
    """Compact magnitude string: 1.23M / 4.56k / 12.34 / 0.0₅14."""
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}k"
    if value >= 1:
        return f"{value:,.2f}"
    return _fmt_decimal_smart(value)


def _fmt_usd(amount: float) -> str:
    """USD with smart decimal width -- handles tiny values without 0.00."""
    if amount <= 0:
        return "$0.00"
    if amount >= 1:
        return f"${amount:,.2f}"
    if amount >= 0.01:
        return f"${amount:.4f}"
    return f"${_fmt_decimal_smart(amount)}"


def _fmt_proto_price(
    price_q_per_tok: float, *, dfun_usd: float = 0.0,
) -> str:
    """Render a curve spot price; appends a USD equiv when ``dfun_usd>0``."""
    qsym = _qsym()
    if price_q_per_tok <= 0:
        return f"0 {qsym}"
    base = f"{_fmt_decimal_smart(price_q_per_tok)} {qsym}"
    if dfun_usd > 0:
        return f"{base}  ≈  {_fmt_usd(price_q_per_tok * dfun_usd)}"
    return base


def _fmt_quote(amount: float) -> str:
    qem = _qem()
    qsym = _qsym()
    return f"{qem} {amount:,.4f} {qsym}".strip()


def _progress_bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(1.0, pct))
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled)


def _mcap_str(mcap_quote_raw: int, *, dfun_usd: float = 0.0) -> str:
    h = to_human(mcap_quote_raw)
    base = f"{_short_num(h)} {_qsym()}"
    if dfun_usd > 0 and h > 0:
        return f"{base} ({_fmt_usd(h * dfun_usd)})"
    return base


async def _live_dfun_usd(db, guild_id: int) -> float:
    """Live DFUN/USD rate from crypto_prices, fallback to genesis price."""
    try:
        row = await db.fetch_one(
            "SELECT price FROM crypto_prices WHERE symbol='DFUN' AND guild_id=$1",
            guild_id,
        )
    except Exception:
        row = None
    if row and row.get("price"):
        try:
            return float(row["price"])
        except (TypeError, ValueError):
            pass
    return float(Config.TOKENS.get("DFUN", {}).get("start_price", 0.10) or 0.10)


def _proto_summary_line(p, *, idx: int | None = None, dfun_usd: float = 0.0) -> str:
    """Compact, attractive one-row summary for list views."""
    real = p.h("real_quote_collected")
    grad = p.h("graduation_quote")
    pct = (real / grad) if grad else 0.0
    v_q = int(p["virtual_quote"])
    v_t = int(p["virtual_token"])
    spot = (v_q / v_t) if v_t else 0.0
    mcap_raw = _df.market_cap_raw(v_q, v_t, int(p["total_supply"]))
    rank = f"`#{idx:>2}` " if idx is not None else ""
    head = f"{rank}{p['emoji']} **{p['symbol']}**  -  {p['name']}"
    bar = _progress_bar(pct, width=14)
    body = (
        f"`{bar}` `{pct*100:5.1f}%`\n"
        f"  💰 `{_fmt_proto_price(spot, dfun_usd=dfun_usd)}` · "
        f"📊 mcap `{_mcap_str(mcap_raw, dfun_usd=dfun_usd)}` · "
        f"👥 `{int(p['holder_count'])}` · "
        f"🔁 `{int(p['trade_count'])}`"
    )
    return f"{head}\n{body}"


def _parse_amount(raw: str) -> float | None:
    """Parse a numeric amount with optional ``k`` / ``m`` suffix. None on failure."""
    raw = raw.strip().lower().replace(",", "").replace("_", "")
    if not raw:
        return None
    mult = 1.0
    if raw.endswith("k"):
        mult, raw = 1_000.0, raw[:-1]
    elif raw.endswith("m"):
        mult, raw = 1_000_000.0, raw[:-1]
    elif raw.endswith("b"):
        mult, raw = 1_000_000_000.0, raw[:-1]
    try:
        return float(raw) * mult
    except ValueError:
        return None


# ── Embed builders ──────────────────────────────────────────────────────────

def _build_info_embed(
    ctx: DiscoContext, proto, *,
    viewer_holding_raw: int = 0, dfun_usd: float = 0.0,
) -> discord.Embed:
    """The interactive `,fun info` panel. Used by the cog command and the
    button-driven Refresh callback."""
    qsym = _qsym()
    qem = _qem()
    sym = proto["symbol"]
    v_q = int(proto["virtual_quote"])
    v_t = int(proto["virtual_token"])
    real = int(proto["real_quote_collected"])
    grad = int(proto["graduation_quote"])
    circ = int(proto["tokens_in_circulation"])
    total = int(proto["total_supply"])
    curve_supply = int(proto["curve_supply"])
    spot = (v_q / v_t) if v_t else 0.0
    pct = _df.progress_pct(real, grad)
    mcap_raw = _df.market_cap_raw(v_q, v_t, total)
    graduated = bool(proto["graduated"])

    creator = ctx.guild.get_member(int(proto["creator_id"])) if ctx.guild else None
    creator_name = creator.display_name if creator else f"User {proto['creator_id']}"

    title = f"{proto['emoji']} {sym}  -  {proto['name']}"
    color = C_GOLD if graduated else C_INFO

    embed = card(title, color=color)

    if graduated:
        embed.description(
            f"🎓 **Graduated** -- now a full Discoin Network token.\n"
            f"`{ctx.prefix}buy {sym}` / `{ctx.prefix}sell {sym}` to trade on the AMM."
        )
    else:
        bar = _progress_bar(pct, width=22)
        # Headline panel -- big bar + the two numbers that matter.
        embed.description(
            f"**Bonding curve  -  {pct*100:5.1f}% to graduation**\n"
            f"```{bar}```\n"
            f"`{to_human(real):,.2f}` / `{to_human(grad):,.0f}` {qsym} collected"
        )

    # ── Top fields (price, mcap, supply, etc) ──────────────────────────────
    embed.field("Price", _fmt_proto_price(spot, dfun_usd=dfun_usd), True)
    embed.field("Market Cap", _mcap_str(mcap_raw, dfun_usd=dfun_usd), True)
    embed.field("Holders", f"{int(proto['holder_count']):,}", True)
    embed.field(
        "Sold on Curve",
        f"`{to_human(circ):,.0f}` / `{to_human(curve_supply):,.0f}` ({(circ/curve_supply*100 if curve_supply else 0):.1f}%)",
        True,
    )
    embed.field("Lifetime Volume", f"{to_human(int(proto['volume_quote'])):,.2f} {qsym}", True)
    embed.field("Trades", f"{int(proto['trade_count']):,}", True)
    embed.field("Total Supply", f"`{to_human(total):,.0f}`", True)
    embed.field("Trade Fee", f"{int(proto['trade_fee_bps']) / 100:.2f}%", True)
    embed.field("Creator", creator_name, True)

    if graduated:
        embed.field("Graduated", fmt_ts(proto["graduated_at"]), True)
    else:
        embed.field("Created", fmt_ts(proto["created_at"]), True)
        # Inactivity countdown -- shows when the proto would auto-destroy
        # if no one buys. Refreshed by every buy via last_buy_at = NOW().
        last_buy = proto.get("last_buy_at")
        try:
            last_buy_f = float(last_buy) if last_buy is not None else None
        except (TypeError, ValueError):
            last_buy_f = None
        if last_buy_f is not None:
            elapsed = max(0, int(time.time()) - int(last_buy_f))
            remaining = max(0, _df.INACTIVITY_DESTROY_SECS - elapsed)
            days = remaining // 86400
            hours = (remaining % 86400) // 3600
            embed.field(
                "Auto-destroy",
                (
                    f"`{days}d {hours}h` if no buys"
                    if remaining > 0
                    else "**Sweeping next tick** -- buy now to save it"
                ),
                True,
            )

    # ── Your position ──────────────────────────────────────────────────────
    if viewer_holding_raw > 0 and not graduated:
        held = to_human(viewer_holding_raw)
        value_q = held * spot
        embed.field(
            f"Your Bag",
            f"`{held:,.4f}` {sym} · ≈ {qem} `{value_q:,.4f}` {qsym}",
            False,
        )

    if graduated:
        embed.footer(f"Trade on the AMM: {ctx.prefix}buy {sym} / {ctx.prefix}sell {sym}")
    else:
        embed.footer(
            f"{qem} Quote currency: {qsym} · "
            f"Quick-buy buttons below · {ctx.prefix}fun info {sym} to refresh"
        )
    return embed


# ── Interactive view: buy / sell / refresh / holders / trades ──────────────

class FunPanelView(discord.ui.View):
    """Sticky button panel for `,fun info`. Anyone in the channel can press."""

    def __init__(self, cog: "DiscFun", ctx: DiscoContext, proto_id: int) -> None:
        super().__init__(timeout=180.0)
        self.cog = cog
        self.ctx = ctx
        self.proto_id = proto_id
        self._build_buttons()

    def _build_buttons(self) -> None:
        chips = list(Config.DISCFUN.get("quickbuy_chips", [1.0, 5.0, 25.0, 100.0]))
        # Row 0: quick-buy chips (DFUN amounts)
        for amt in chips[:5]:
            label = f"+{amt:g} {_qsym()}"
            self.add_item(_QuickBuyButton(self, amt, label))
        # Row 1: primary trade actions (max 5 per row).
        self.add_item(_BuyCustomButton(self))
        self.add_item(_SellButton(self))
        self.add_item(_ChartButton(self))
        self.add_item(_RefreshButton(self))
        # Row 2: secondary lookups.
        self.add_item(_HoldersButton(self))
        self.add_item(_TradesButton(self))

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        # Best-effort -- the original message may have been deleted.
        try:
            if hasattr(self, "message") and self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


class _QuickBuyButton(discord.ui.Button):
    def __init__(self, panel: FunPanelView, amount_dfun: float, label: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.success,
            label=label, row=0,
            custom_id=f"fun_qb_{amount_dfun}",
        )
        self._panel = panel
        self.amount_dfun = amount_dfun

    async def callback(self, inter: discord.Interaction) -> None:
        await self._panel.cog._handle_buy_button(inter, self._panel, self.amount_dfun)


class _BuyCustomButton(discord.ui.Button):
    def __init__(self, panel: FunPanelView) -> None:
        super().__init__(
            style=discord.ButtonStyle.success, label="🟢 Buy…", row=1,
            custom_id="fun_buy_custom",
        )
        self._panel = panel

    async def callback(self, inter: discord.Interaction) -> None:
        modal = InputModal(
            title=f"Buy {self._panel.proto_id}",
            label=f"Amount in {_qsym()}",
            placeholder="e.g. 12.5",
            max_length=20,
        )
        await inter.response.send_modal(modal)
        await modal.wait()
        if not modal.value:
            return
        amt = _parse_amount(modal.value)
        if amt is None or amt <= 0:
            await inter.followup.send("Invalid amount.", ephemeral=True)
            return
        await self._panel.cog._handle_buy_button(inter, self._panel, amt, deferred=True)


class _SellButton(discord.ui.Button):
    def __init__(self, panel: FunPanelView) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger, label="🔴 Sell…", row=1,
            custom_id="fun_sell",
        )
        self._panel = panel

    async def callback(self, inter: discord.Interaction) -> None:
        modal = InputModal(
            title="Sell",
            label="Amount (number, all, or %)",
            placeholder="e.g. 100000  /  all  /  50%",
            max_length=20,
        )
        await inter.response.send_modal(modal)
        await modal.wait()
        if not modal.value:
            return
        await self._panel.cog._handle_sell_button(
            inter, self._panel, modal.value, deferred=True,
        )


class _RefreshButton(discord.ui.Button):
    def __init__(self, panel: FunPanelView) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary, label="🔄 Refresh", row=1,
            custom_id="fun_refresh",
        )
        self._panel = panel

    async def callback(self, inter: discord.Interaction) -> None:
        proto = await _df.get_proto_by_id(self._panel.cog.bot.db, self._panel.proto_id)
        if proto is None:
            await inter.response.send_message("Proto vanished.", ephemeral=True)
            return
        viewer_holding = await _df.get_user_proto_holding(
            self._panel.cog.bot.db, self._panel.proto_id, inter.user.id,
        )
        ctx = self._panel.ctx
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        embed = _build_info_embed(
            ctx, proto, viewer_holding_raw=viewer_holding, dfun_usd=dfun_usd,
        )
        # Keep the panel alive but disable the view if graduated.
        view = self._panel if not proto["graduated"] else None
        await inter.response.edit_message(embed=embed, view=view)


class _ChartButton(discord.ui.Button):
    def __init__(self, panel: FunPanelView) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary, label="📊 Chart", row=1,
            custom_id="fun_chart",
        )
        self._panel = panel

    async def callback(self, inter: discord.Interaction) -> None:
        await inter.response.defer(ephemeral=False, thinking=True)
        cog = self._panel.cog
        ctx = self._panel.ctx
        proto = await _df.get_proto_by_id(ctx.db, self._panel.proto_id)
        if proto is None:
            await inter.followup.send("Proto vanished.", ephemeral=True)
            return
        await cog._send_proto_chart(ctx, proto, "5m", reply_target=inter)


class _HoldersButton(discord.ui.Button):
    def __init__(self, panel: FunPanelView) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary, label="👥 Holders", row=2,
            custom_id="fun_holders",
        )
        self._panel = panel

    async def callback(self, inter: discord.Interaction) -> None:
        await self._panel.cog._show_holders(inter, self._panel.proto_id)


class _TradesButton(discord.ui.Button):
    def __init__(self, panel: FunPanelView) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary, label="📜 Trades", row=2,
            custom_id="fun_trades",
        )
        self._panel = panel

    async def callback(self, inter: discord.Interaction) -> None:
        await self._panel.cog._show_trades(inter, self._panel.proto_id)


# ── Cog ─────────────────────────────────────────────────────────────────────

class _StakeOverviewView(discord.ui.View):
    """Quick-action buttons under the ,fun stake overview embed.

    Mirrors the pattern used by ``,farm stake`` / ``,fish stake``: the
    overview is informational, but a one-click "list my stakes" /
    "claim all" / "stake everything" saves the user from having to
    retype the next command. Anyone in the channel can press;
    interactions re-issue the canonical command on behalf of the
    pressing user (so each press shows a fresh embed for them).
    """

    def __init__(self, cog: "DiscFun", ctx: DiscoContext) -> None:
        super().__init__(timeout=180.0)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.button(label="My Stakes", style=discord.ButtonStyle.primary, emoji="🔒")
    async def btn_stakes(
        self, inter: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await inter.response.defer(ephemeral=False)
        shim = _ButtonCtxShim(self.ctx, inter)
        await self.cog.fun_stakes.callback(self.cog, shim)

    @discord.ui.button(label="Claim All", style=discord.ButtonStyle.success, emoji="💰")
    async def btn_claim_all(
        self, inter: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await inter.response.defer(ephemeral=False)
        shim = _ButtonCtxShim(self.ctx, inter)
        await self.cog.fun_claim.callback(self.cog, shim, symbol=None)

    @discord.ui.button(label="Stake Everything", style=discord.ButtonStyle.secondary, emoji="🎰")
    async def btn_stake_everything(
        self, inter: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await inter.response.defer(ephemeral=False)
        shim = _ButtonCtxShim(self.ctx, inter)
        await self.cog.fun_stake.callback(
            self.cog, shim, symbol="everything", amount=None,
        )


class _StakeActionsView(discord.ui.View):
    """Buttons under a single-symbol stake receipt: claim / unstake / autocompound."""

    def __init__(self, cog: "DiscFun", ctx: DiscoContext, symbol: str) -> None:
        super().__init__(timeout=180.0)
        self.cog = cog
        self.ctx = ctx
        self.symbol = symbol.upper()

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="💰")
    async def btn_claim(
        self, inter: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await inter.response.defer(ephemeral=False)
        shim = _ButtonCtxShim(self.ctx, inter)
        await self.cog.fun_claim.callback(self.cog, shim, symbol=self.symbol)

    @discord.ui.button(label="Unstake All", style=discord.ButtonStyle.danger, emoji="🔓")
    async def btn_unstake(
        self, inter: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await inter.response.defer(ephemeral=False)
        shim = _ButtonCtxShim(self.ctx, inter)
        await self.cog.fun_unstake.callback(
            self.cog, shim, symbol=self.symbol, amount="all",
        )

    @discord.ui.button(label="Toggle Auto-Compound", style=discord.ButtonStyle.secondary, emoji="🔁")
    async def btn_autocompound(
        self, inter: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        await inter.response.defer(ephemeral=False)
        shim = _ButtonCtxShim(self.ctx, inter)
        await self.cog.fun_autocompound.callback(
            self.cog, shim, symbol=self.symbol, choice="toggle",
        )


class DiscFun(commands.Cog):
    """Disc.Fun  -  proto-token launchpad with a virtual bonding curve."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._inactivity_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        """Start the inactivity sweep loop when the cog comes online."""
        if self._inactivity_task is None or self._inactivity_task.done():
            self._inactivity_task = asyncio.create_task(self._inactivity_loop())

    async def cog_unload(self) -> None:
        if self._inactivity_task and not self._inactivity_task.done():
            self._inactivity_task.cancel()

    async def _inactivity_loop(self) -> None:
        """Run ``sweep_inactive_protos`` every hour.

        Initial 60s grace so a fresh process boot doesn't slam the DB on
        startup if the bot was offline through the threshold. Logs every
        destroyed proto for audit purposes; balances disappear on their
        own via ``ON DELETE CASCADE`` on proto_token_holdings.
        """
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
        while True:
            try:
                rows = await _df.sweep_inactive_protos(self.bot.db)
                for r in rows:
                    log.warning(
                        "Disc.Fun proto destroyed for inactivity: gid=%s "
                        "symbol=%s name=%s creator=%s last_buy_at=%s",
                        r.get("guild_id"), r.get("symbol"), r.get("name"),
                        r.get("creator_id"), r.get("last_buy_at"),
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Disc.Fun inactivity sweep iteration crashed")
            try:
                await asyncio.sleep(_df.INACTIVITY_SWEEP_INTERVAL_S)
            except asyncio.CancelledError:
                return

    # ── Group ──────────────────────────────────────────────────────────────

    @commands.hybrid_group(
        name="fun", aliases=["df", "discfun"],
        invoke_without_command=True, with_app_command=False,
    )
    @guild_only
    async def fun(self, ctx: DiscoContext) -> None:
        """Disc.Fun launchpad -- deploy and trade proto tokens."""
        if await suggest_subcommand(ctx, self.fun):
            return
        await self._send_overview(ctx)

    async def _send_overview(self, ctx: DiscoContext) -> None:
        cfg = Config.DISCFUN
        qsym = _qsym()
        qem = _qem()
        deploy_fee = float(cfg["deploy_fee"])
        active = await _df.list_active_protos(ctx.db, ctx.guild_id, limit=5, sort="hot")
        recent = await _df.list_recent_graduates(ctx.db, ctx.guild_id, limit=3)
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)

        embed = (
            card("🎢 Disc.Fun -- Token Launchpad", color=C_PURPLE)
            .description(
                f"Launch a token in seconds, trade on a virtual bonding curve, "
                f"graduate to a real {qem} {qsym}/SYMBOL pool once **"
                f"{cfg['graduation_quote']:,.0f} {qsym}** is collected.\n\n"
                f"**Quote currency:** {qem} `{qsym}` (Discoin Network)\n"
                f"**Deploy fee:** `{deploy_fee:,.0f} {qsym}`\n"
                f"**Trade fee:** `{cfg['trade_fee_bps'] / 100:.2f}%` per buy/sell\n"
                f"**Total supply:** `{cfg['total_supply']:,}` "
                f"(`{cfg['curve_supply']:,}` on the curve, "
                f"rest seeds the LP at graduation)"
            )
        )

        cmd_block = (
            f"`{ctx.prefix}fun deploy SYMBOL \"Name\" 🚀`  -  launch a proto "
            f"(custom Discord emojis welcome)\n"
            f"`{ctx.prefix}fun edit SYMBOL \"New Name\" [emoji]`  -  rename / "
            f"re-emoji your proto (creator only, costs 2x deploy fee)\n"
            f"`{ctx.prefix}fun list [hot|new|progress|mcap]`  -  browse\n"
            f"`{ctx.prefix}fun info SYMBOL`  -  live panel + buy/sell buttons\n"
            f"`{ctx.prefix}fun chart SYMBOL [1m|5m|15m|1h|4h|1d]`  -  candles\n"
            f"`{ctx.prefix}fun buy SYMBOL <{qsym}>`  -  market buy\n"
            f"`{ctx.prefix}fun sell SYMBOL <amt|all|%>`  -  market sell\n"
            f"`{ctx.prefix}fun bag`  -  your active positions\n"
            f"`{ctx.prefix}fun grads`  -  recent graduations"
        )
        embed.field("Commands", cmd_block, False)
        apy_pct = await _df.current_staking_apy_pct(ctx.db, ctx.guild_id)
        min_apy = float(cfg.get("staking_min_apy_pct", 0.0))
        max_apy = float(cfg.get("staking_max_apy_pct", 0.0))
        stake_block = (
            f"`{ctx.prefix}fun stake SYMBOL <amt|all>`  -  lock graduated "
            f"tokens for **{apy_pct:,.0f}%** live APY paid in `DFUN` "
            f"(variable, floor {min_apy:,.0f}% / cap {max_apy:,.0f}%)\n"
            f"`{ctx.prefix}fun stake everything`  -  stake every graduated "
            f"token you hold\n"
            f"`{ctx.prefix}fun stakes`  -  list your active stakes + pending "
            f"yield\n"
            f"`{ctx.prefix}fun claim [SYMBOL]`  -  harvest pending DFUN yield\n"
            f"`{ctx.prefix}fun autocompound SYMBOL [on|off]`  -  reinvest "
            f"yield as more of `SYMBOL` (no AMM round-trip, no slippage)\n"
            f"`{ctx.prefix}fun unstake SYMBOL <amt|all>`  -  withdraw the "
            f"staked balance"
        )
        embed.field("Staking (graduated tokens)", stake_block, False)

        if active:
            embed.field(
                "🔥 Hot right now",
                "\n\n".join(
                    _proto_summary_line(p, idx=i+1, dfun_usd=dfun_usd)
                    for i, p in enumerate(active)
                ),
                False,
            )
        else:
            embed.field(
                "No active protos yet",
                f"Be the first: `{ctx.prefix}fun deploy SYMBOL \"Name\" 🚀`",
                False,
            )

        if recent:
            grad_lines = [
                f"{r['emoji']} **{r['symbol']}** -- {r['name']} (graduated {fmt_ts(r['graduated_at'])})"
                for r in recent
            ]
            embed.field("🎓 Recent graduates", "\n".join(grad_lines), False)

        embed.footer(
            f"Need full control over supply/price/fees? Use {ctx.prefix}token deploy "
            f"(Protocol Dev tier required)."
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    # ── Deploy ─────────────────────────────────────────────────────────────

    @fun.command(name="deploy", aliases=["create", "launch"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_deploy(self, ctx: DiscoContext, *, raw: str = "") -> None:
        """Launch a proto token on the Disc.Fun bonding curve.

        Usage: {prefix}fun deploy SYMBOL "Token Name" 🚀
        Example: {prefix}fun deploy MEME "My Meme Coin" 🐸
        """
        cfg = Config.DISCFUN
        qsym = _qsym()
        if not raw.strip():
            await self._send_deploy_help(ctx)
            return

        m = _DEPLOY_RE.match(raw)
        if not m:
            await ctx.reply_error_hint(
                "Couldn't parse your deploy command.",
                hint=f"{ctx.prefix}fun deploy MEME \"My Meme Coin\" 🐸",
                command_name="fun deploy",
            )
            return

        symbol = m.group("symbol").upper()
        name = (m.group("name_q") or m.group("name_p") or "").strip()
        emoji = (m.group("emoji") or cfg["default_emoji"]).strip()

        for err in (
            _df.validate_symbol(symbol),
            _df.validate_name(name),
            _df.validate_emoji(emoji),
        ):
            if err:
                await ctx.reply_error(err)
                return

        deploy_fee = float(cfg["deploy_fee"])
        deploy_fee_raw = to_raw(deploy_fee)
        held = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, "dsc", qsym)
        bal_raw = int(held["amount"]) if held else 0
        if bal_raw < deploy_fee_raw:
            await ctx.reply_error_hint(
                f"Deploying a proto costs **{_fmt_quote(deploy_fee)}**. "
                f"Your DSC-network {qsym} balance is `{to_human(bal_raw):,.4f}`.",
                hint=f"{ctx.prefix}buy {qsym}",
                command_name="fun deploy",
            )
            return

        # 1-deploy-per-user-per-guild-per-24h gate. Surface the limit BEFORE
        # the confirm dialog so a player who hits it doesn't waste a prompt
        # round-trip; the service-layer guard is the source of truth and
        # would still reject a racing duplicate.
        cooldown_secs = await _df.deploy_cooldown_remaining(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        if cooldown_secs > 0:
            hrs, rem = divmod(cooldown_secs, 3600)
            mins = rem // 60
            await ctx.reply_error(
                f"You can only deploy one Disc.Fun token per day. "
                f"Try again in **{hrs}h {mins}m**."
            )
            return

        confirm = ConfirmView(ctx.author.id)
        body = (
            f"## Deploy **{emoji} {symbol}**  -  *{name}*\n"
            f"Quote: {_qem()} `{qsym}` · Network: Discoin Network\n\n"
            f"**Cost** · `{deploy_fee:,.0f} {qsym}` (flat fee)\n"
            f"**Total Supply** · `{cfg['total_supply']:,}` (fixed)\n"
            f"**Curve Supply** · `{cfg['curve_supply']:,}` "
            f"(remaining `{cfg['total_supply'] - cfg['curve_supply']:,}` seeds the LP at graduation)\n"
            f"**Trade Fee** · `{cfg['trade_fee_bps'] / 100:.2f}%` per buy & sell\n"
            f"**Graduation Threshold** · `{cfg['graduation_quote']:,.0f} {qsym}`\n\n"
            f"On graduation, **{symbol}** becomes a full ERC-20 with two pools:\n"
            f"  - **{symbol}/{qsym}** (deep, native) seeded with collected {qsym} + 90% of LP supply\n"
            f"  - **{symbol}/DSC** (bridge) seeded with the remaining 10% so it routes against DSC\n"
            f"  - LP shares are **locked** -- liquidity is permanent."
        )
        msg = await ctx.reply(body, view=confirm, mention_author=False)
        if not await confirm.wait_result():
            await msg.edit(content="Deploy cancelled.", view=None)
            return

        try:
            proto = await _df.deploy_proto_token(
                ctx.db,
                guild_id=ctx.guild_id,
                creator_id=ctx.author.id,
                symbol=symbol,
                name=name,
                emoji=emoji,
            )
        except ValueError as exc:
            await msg.edit(content=None, embed=card(
                "Deploy Failed", description=f"❌  {exc}", color=C_AMBER,
            ).build(), view=None)
            return
        except Exception:
            log.exception("fun deploy failed for sym=%s gid=%s", symbol, ctx.guild_id)
            await msg.edit(content=None, embed=card(
                "Deploy Failed",
                description="❌  Unexpected error. The deploy fee was not charged.",
                color=C_AMBER,
            ).build(), view=None)
            return

        spot = float(proto["virtual_quote"]) / float(proto["virtual_token"])
        embed = (
            card(f"🚀 {emoji} {symbol} -- LIVE", color=C_SUCCESS)
            .description(
                f"**{name}** is now trading on Disc.Fun.\n\n"
                f"Hit a quick-buy button or use `{ctx.prefix}fun info {symbol}` "
                f"for the full panel."
            )
            .field("Start Price", _fmt_proto_price(spot), True)
            .field("Curve Supply", f"`{cfg['curve_supply']:,}`", True)
            .field("Graduation @", f"`{cfg['graduation_quote']:,.0f} {qsym}`", True)
            .footer(
                f"Quote: {qsym} · {cfg['trade_fee_bps'] / 100:.2f}% trade fee · "
                f"{cfg['curve_supply']:,} on curve"
            )
        )
        view = FunPanelView(self, ctx, int(proto["proto_id"]))
        sent = await msg.edit(content=None, embed=embed.build(), view=view)
        view.message = sent

    # ── Edit ───────────────────────────────────────────────────────────────

    # ── Admin subgroup ─────────────────────────────────────────────────────

    @fun.group(name="admin", invoke_without_command=True)
    @guild_only
    @commands.has_guild_permissions(manage_guild=True)
    async def fun_admin(self, ctx: DiscoContext) -> None:
        """Manage Disc.Fun protos as a server admin (Manage Server perm).

        Subcommands:
            ,fun admin list                       -- every proto in this guild
            ,fun admin rename SYMBOL "New Name"   -- free, bypasses creator-only
            ,fun admin emoji  SYMBOL <emoji>      -- free, bypasses creator-only
            ,fun admin destroy SYMBOL [reason]    -- erase a non-graduated proto
                                                    + every holder balance
            ,fun admin extend  SYMBOL <days>      -- push the inactivity timer
                                                    forward by N days
            ,fun admin sweep                      -- run the inactivity sweep now
        """
        if ctx.invoked_subcommand is not None:
            return
        prefix = ctx.prefix
        await ctx.reply(
            "**Disc.Fun admin commands**\n"
            f"`{prefix}fun admin list`  -  every proto in this guild\n"
            f"`{prefix}fun admin rename SYMBOL \"New Name\"`  -  free rename\n"
            f"`{prefix}fun admin emoji SYMBOL <emoji>`  -  free emoji change\n"
            f"`{prefix}fun admin destroy SYMBOL [reason]`  -  erase a "
            f"non-graduated proto + every holder balance\n"
            f"`{prefix}fun admin extend SYMBOL <days>`  -  push the inactivity "
            f"timer forward by N days\n"
            f"`{prefix}fun admin sweep`  -  run the inactivity sweep now",
            mention_author=False,
        )

    @fun_admin.command(name="list", aliases=["ls"])
    @guild_only
    @commands.has_guild_permissions(manage_guild=True)
    async def fun_admin_list(self, ctx: DiscoContext) -> None:
        """List every proto on this guild with creator + status + age."""
        rows = await ctx.db.fetch_all(
            "SELECT proto_id, symbol, name, emoji, creator_id, graduated, "
            "       created_at, last_buy_at, real_quote_collected, "
            "       graduation_quote, holder_count "
            "FROM proto_tokens WHERE guild_id=$1 "
            "ORDER BY graduated, created_at DESC",
            ctx.guild_id,
        )
        if not rows:
            await ctx.reply_error("No Disc.Fun protos exist on this server.")
            return
        lines: list[str] = []
        now_ts = int(time.time())
        for r in rows:
            sym = str(r["symbol"])
            graduated = bool(r["graduated"])
            holder_count = int(r.get("holder_count") or 0)
            real = int(r.get("real_quote_collected") or 0)
            grad = int(r.get("graduation_quote") or 1)
            pct = (real / grad * 100.0) if grad else 0.0
            try:
                last_buy = float(r.get("last_buy_at") or 0)
            except (TypeError, ValueError):
                last_buy = 0.0
            stale_hours = max(0, (now_ts - int(last_buy)) // 3600) if last_buy else 0
            badge = "🎓 graduated" if graduated else f"🛒 {pct:.1f}% to grad"
            lines.append(
                f"{r['emoji']} **{sym}** -- {r['name']}\n"
                f"-# id `{r['proto_id']}` · creator <@{r['creator_id']}> · "
                f"holders {holder_count} · {badge}\n"
                f"-# last_buy {stale_hours}h ago · created {fmt_ts(r['created_at'])}"
            )
        embed = card(
            f"🛠 Disc.Fun Protos ({len(rows)})", color=C_NAVY,
        ).description("\n\n".join(lines[:25]))
        if len(rows) > 25:
            embed.footer(f"+{len(rows) - 25} more (showing first 25)")
        await ctx.reply(embed=embed.build(), mention_author=False)

    @fun_admin.command(name="rename")
    @guild_only
    @commands.has_guild_permissions(manage_guild=True)
    async def fun_admin_rename(
        self, ctx: DiscoContext, symbol: str, *, new_name: str,
    ) -> None:
        """Rename a proto. No fee, bypasses creator-only check."""
        sym = symbol.upper().lstrip("`").rstrip("`")
        new_name = (new_name or "").strip().strip('"').strip("'")
        if not new_name:
            await ctx.reply_error("Pass a new name.")
            return
        err = _df.validate_name(new_name)
        if err:
            await ctx.reply_error(err)
            return
        proto = await _df.get_proto_by_symbol(ctx.db, ctx.guild_id, sym)
        if proto is None:
            await ctx.reply_error(f"No Disc.Fun proto `{sym}` on this server.")
            return
        if bool(proto.get("graduated")):
            await ctx.reply_error(
                f"`{sym}` has graduated -- it's a regular guild token now. "
                f"Use the standard token-admin tools to rename it."
            )
            return
        await ctx.db.execute(
            "UPDATE proto_tokens SET name=$3 "
            "WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, sym, new_name,
        )
        await ctx.reply_success(
            f"`{sym}` renamed to **{new_name}**.",
            title="Admin: Renamed",
        )

    @fun_admin.command(name="emoji")
    @guild_only
    @commands.has_guild_permissions(manage_guild=True)
    async def fun_admin_emoji(
        self, ctx: DiscoContext, symbol: str, *, new_emoji: str,
    ) -> None:
        """Change a proto's emoji. No fee, bypasses creator-only check."""
        sym = symbol.upper().lstrip("`").rstrip("`")
        new_emoji = (new_emoji or "").strip()
        err = _df.validate_emoji(new_emoji)
        if err:
            await ctx.reply_error(err)
            return
        proto = await _df.get_proto_by_symbol(ctx.db, ctx.guild_id, sym)
        if proto is None:
            await ctx.reply_error(f"No Disc.Fun proto `{sym}` on this server.")
            return
        if bool(proto.get("graduated")):
            await ctx.reply_error(
                f"`{sym}` has graduated -- emoji is locked on the regular token now."
            )
            return
        await ctx.db.execute(
            "UPDATE proto_tokens SET emoji=$3 "
            "WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, sym, new_emoji,
        )
        await ctx.reply_success(
            f"`{sym}` emoji changed to {new_emoji}.",
            title="Admin: Emoji Updated",
        )

    @fun_admin.command(name="destroy", aliases=["delete", "rug", "rm"])
    @guild_only
    @commands.has_guild_permissions(manage_guild=True)
    async def fun_admin_destroy(
        self, ctx: DiscoContext, symbol: str, *, reason: str = "",
    ) -> None:
        """Permanently delete a non-graduated proto and zero every holder balance.

        Idempotent on already-graduated protos (refused -- those are
        regular tokens managed elsewhere) and on missing symbols. The
        DELETE cascades to ``proto_token_holdings`` and
        ``proto_token_trades`` so balances disappear on their own.
        """
        sym = symbol.upper().lstrip("`").rstrip("`")
        proto = await _df.get_proto_by_symbol(ctx.db, ctx.guild_id, sym)
        if proto is None:
            await ctx.reply_error(f"No Disc.Fun proto `{sym}` on this server.")
            return
        if bool(proto.get("graduated")):
            await ctx.reply_error(
                f"`{sym}` has graduated. Disc.Fun no longer manages it -- "
                f"use the standard token-admin tools to disable it."
            )
            return
        holder_count = int(proto.get("holder_count") or 0)
        body = (
            f"## Destroy **{proto['emoji']} {sym}** ?\n"
            f"**{proto['name']}** -- proto id `{proto['proto_id']}`\n"
            f"Holders: **{holder_count}** "
            f"(every balance will be zeroed)\n"
            f"Reason: {reason or '*(none given)*'}\n\n"
            f"This is **permanent**. The symbol becomes available for "
            f"a fresh deploy."
        )
        confirmed = await ctx.confirm(body, timeout=30.0)
        if not confirmed:
            await ctx.reply_error("Destroy cancelled.")
            return
        try:
            await ctx.db.execute(
                "DELETE FROM proto_tokens "
                "WHERE guild_id=$1 AND symbol=$2 AND graduated=FALSE",
                ctx.guild_id, sym,
            )
        except Exception as exc:
            log.exception(
                "fun admin destroy failed gid=%s sym=%s", ctx.guild_id, sym,
            )
            await ctx.reply_error(f"Destroy failed: {exc}")
            return
        log.warning(
            "Disc.Fun admin destroy: gid=%s sym=%s by uid=%s reason=%s holders=%s",
            ctx.guild_id, sym, ctx.author.id, reason or "(none)", holder_count,
        )
        await ctx.reply_success(
            f"**{sym}** destroyed. {holder_count} holder balance"
            f"{'s' if holder_count != 1 else ''} wiped. "
            f"Reason: {reason or '*(none)*'}",
            title="Admin: Proto Destroyed",
        )

    @fun_admin.command(name="extend")
    @guild_only
    @commands.has_guild_permissions(manage_guild=True)
    async def fun_admin_extend(
        self, ctx: DiscoContext, symbol: str, days: int,
    ) -> None:
        """Push a proto's last_buy_at forward by N days, delaying auto-destroy."""
        sym = symbol.upper().lstrip("`").rstrip("`")
        if days <= 0:
            await ctx.reply_error("Days must be positive.")
            return
        if days > 30:
            await ctx.reply_error("Cap on extension is 30 days at a time.")
            return
        proto = await _df.get_proto_by_symbol(ctx.db, ctx.guild_id, sym)
        if proto is None:
            await ctx.reply_error(f"No Disc.Fun proto `{sym}` on this server.")
            return
        if bool(proto.get("graduated")):
            await ctx.reply_error(
                f"`{sym}` has graduated -- the inactivity rule no longer applies."
            )
            return
        await ctx.db.execute(
            "UPDATE proto_tokens "
            "   SET last_buy_at = GREATEST(last_buy_at, NOW()) "
            "                       + ($3::int * INTERVAL '1 day') "
            " WHERE guild_id=$1 AND symbol=$2",
            ctx.guild_id, sym, int(days),
        )
        await ctx.reply_success(
            f"`{sym}` inactivity timer pushed forward by **{days}** day"
            f"{'s' if days != 1 else ''}. New auto-destroy clock starts now.",
            title="Admin: Timer Extended",
        )

    @fun_admin.command(name="sweep")
    @guild_only
    @commands.has_guild_permissions(manage_guild=True)
    async def fun_admin_sweep(self, ctx: DiscoContext) -> None:
        """Run the inactivity sweep now instead of waiting for the hourly tick."""
        rows = await _df.sweep_inactive_protos(ctx.db)
        if not rows:
            await ctx.reply_success(
                "No protos eligible for inactivity sweep right now.",
                title="Admin: Sweep Complete",
            )
            return
        lines = [
            f"·  {r.get('emoji', '')} **{r['symbol']}** "
            f"({r['name']}) -- creator <@{r['creator_id']}>"
            for r in rows
        ]
        embed = card(
            f"🧹 Sweep Complete -- {len(rows)} destroyed",
            color=C_AMBER,
        ).description(
            "Every listed proto has been deleted along with every "
            "holder balance and trade history.\n\n" + "\n".join(lines)
        )
        log.warning(
            "Disc.Fun manual sweep by uid=%s in gid=%s wiped %s protos",
            ctx.author.id, ctx.guild_id, len(rows),
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @fun.command(name="edit", aliases=["rename", "modify"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_edit(self, ctx: DiscoContext, *, raw: str = "") -> None:
        """Edit a proto's name and/or emoji (creator only, costs 2x deploy fee).

        Usage:
          {prefix}fun edit SYMBOL "New Name" [emoji]
          {prefix}fun edit SYMBOL [emoji]              -- emoji-only
          {prefix}fun edit SYMBOL "New Name"           -- name-only

        Once a proto graduates it becomes a regular guild token and this
        command refuses -- post-graduation metadata is locked.
        """
        cfg = Config.DISCFUN
        qsym = _qsym()
        edit_fee = float(cfg["deploy_fee"]) * _df.EDIT_FEE_MULTIPLIER
        if not raw.strip():
            await ctx.reply(
                "**Edit a Disc.Fun proto's name / emoji** (creator only, "
                f"locked once graduated).\n"
                "```\n"
                f"{ctx.prefix}fun edit SYMBOL \"New Name\" [emoji]\n"
                "```\n"
                f"**Cost:** `{edit_fee:,.0f} {qsym}` (2x the deploy fee).\n"
                "Pass either a unicode glyph or a Discord custom emoji "
                "(`<:name:id>` / `<a:name:id>`).",
                mention_author=False,
            )
            return

        m = _EDIT_RE.match(raw)
        if not m:
            await ctx.reply_error_hint(
                "Couldn't parse your edit command.",
                hint=f"{ctx.prefix}fun edit MEME \"My Meme Coin\" 🐸",
                command_name="fun edit",
            )
            return

        symbol = m.group("symbol").upper()
        new_name = m.group("name_q") or m.group("name_p")
        if new_name is not None:
            new_name = new_name.strip() or None
        new_emoji = m.group("emoji")
        if new_emoji is not None:
            new_emoji = new_emoji.strip() or None
        if new_name is None and new_emoji is None:
            await ctx.reply_error_hint(
                "Pass at least a new name or a new emoji.",
                hint=f"{ctx.prefix}fun edit {symbol} \"Better Name\" 🐸",
                command_name="fun edit",
            )
            return

        for err in (
            _df.validate_symbol(symbol),
            _df.validate_name(new_name) if new_name is not None else None,
            _df.validate_emoji(new_emoji) if new_emoji is not None else None,
        ):
            if err and not err.startswith("Symbol `"):
                # validate_symbol's "reserved or already exists" trips for
                # any deployed token; for edit we WANT it to exist, so
                # only forward the format errors.
                await ctx.reply_error(err)
                return

        proto = await _df.get_proto_by_symbol(ctx.db, ctx.guild_id, symbol)
        if proto is None:
            await ctx.reply_error(
                f"No Disc.Fun proto `{symbol}` on this server."
            )
            return
        if int(proto.get("creator_id") or 0) != int(ctx.author.id):
            await ctx.reply_error(
                f"Only the original deployer of `{symbol}` can edit it."
            )
            return
        if bool(proto.get("graduated")):
            await ctx.reply_error(
                f"`{symbol}` has already graduated -- it's a regular "
                f"token now and Disc.Fun no longer manages its metadata."
            )
            return

        cur_name = str(proto.get("name") or "")
        cur_emoji = str(proto.get("emoji") or "")
        next_name = new_name if new_name is not None else cur_name
        next_emoji = new_emoji if new_emoji is not None else cur_emoji
        bits: list[str] = []
        if new_name is not None and new_name != cur_name:
            bits.append(f"name `{cur_name}` → `{next_name}`")
        if new_emoji is not None and new_emoji != cur_emoji:
            bits.append(f"emoji {cur_emoji} → {next_emoji}")
        if not bits:
            await ctx.reply_error(
                "Nothing to change -- the new values match the current ones."
            )
            return

        confirm = ConfirmView(ctx.author.id)
        body = (
            f"## Edit **{cur_emoji} {symbol}**\n"
            + "\n".join(f"·  {b}" for b in bits) + "\n\n"
            f"**Cost:** `{edit_fee:,.0f} {qsym}` (2x deploy fee, burned)."
        )
        msg = await ctx.reply(body, view=confirm, mention_author=False)
        if not await confirm.wait_result():
            await msg.edit(content="Edit cancelled.", view=None)
            return

        try:
            row = await _df.edit_proto_token(
                ctx.db,
                guild_id=ctx.guild_id,
                user_id=ctx.author.id,
                symbol=symbol,
                new_name=new_name,
                new_emoji=new_emoji,
            )
        except ValueError as exc:
            await msg.edit(content=None, embed=card(
                "Edit Failed", description=f"❌  {exc}", color=C_AMBER,
            ).build(), view=None)
            return
        except Exception:
            log.exception(
                "fun edit failed for sym=%s gid=%s", symbol, ctx.guild_id,
            )
            await msg.edit(content=None, embed=card(
                "Edit Failed",
                description="❌  Unexpected error. Try again in a moment.",
                color=C_AMBER,
            ).build(), view=None)
            return

        embed = (
            card(
                f"✏️ Updated {row['emoji']} {row['symbol']}",
                color=C_SUCCESS,
            )
            .description(
                f"**{row['name']}**\n"
                + "\n".join(f"·  {b}" for b in bits)
            )
            .footer(
                f"Charged {edit_fee:,.0f} {qsym}. "
                f"Use {ctx.prefix}fun info {symbol} for the live panel."
            )
        )
        await msg.edit(content=None, embed=embed.build(), view=None)

    # ── Stake overview ─────────────────────────────────────────────────────
    async def _send_stake_overview(self, ctx: DiscoContext) -> None:
        """Show a staking landing page, like ,farm stake / ,fish stake.

        Renders live APY, the user's current positions in DFUN + USD,
        the guild-wide TVL, and quick-action buttons. The buttons
        re-issue the most common follow-ups (`,fun stakes`, `,fun
        claim`, `,fun stake everything`) so first-time users don't have
        to type a separate command to take action.
        """
        cfg = Config.DISCFUN
        emission = float(cfg.get("staking_emission_dfun_per_day", 0.0))
        min_apy = float(cfg.get("staking_min_apy_pct", 0.0))
        max_apy = float(cfg.get("staking_max_apy_pct", 0.0))
        tvl_dfun = await _df.total_staked_dfun_value(ctx.db, ctx.guild_id)
        apy_pct = await _df.current_staking_apy_pct(
            ctx.db, ctx.guild_id, total_dfun_override=tvl_dfun,
        )
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        # User-side aggregates (read-only, no accrue write).
        user_stakes = await _df.list_user_stakes(
            ctx.db, ctx.guild_id, ctx.author.id, accrue=False,
        )
        user_value_dfun = 0.0
        user_pending_dfun = 0.0
        for r in user_stakes:
            sym = str(r.get("symbol") or "")
            amt = int(r.get("amount") or 0)
            spot = await _df._amm_spot_dfun(ctx.db, ctx.guild_id, sym)
            user_value_dfun += (amt / SCALE) * spot
            user_pending_dfun += int(r.get("pending_dfun") or 0) / SCALE

        tvl_usd = tvl_dfun * dfun_usd if dfun_usd > 0 else 0.0
        user_usd = user_value_dfun * dfun_usd if dfun_usd > 0 else 0.0
        pending_usd = user_pending_dfun * dfun_usd if dfun_usd > 0 else 0.0

        def _line_dfun_usd(amount_dfun: float, amount_usd: float) -> str:
            if dfun_usd > 0:
                return f"`{amount_dfun:,.4f} DFUN` ({_fmt_usd(amount_usd)})"
            return f"`{amount_dfun:,.4f} DFUN`"

        embed = (
            card("🔒 Disc.Fun Staking", color=C_PURPLE)
            .description(
                "Lock graduated Disc.Fun tokens to earn **DFUN yield**. "
                "APY is emission-based and variable -- a fixed daily DFUN "
                "pool is split across every staker in the server, so "
                "early stakers earn near the cap while the rate "
                "compresses as TVL grows.\n"
                f"\n**Live APY:** `{apy_pct:,.1f}%`  (cap `{max_apy:,.0f}%` / "
                f"floor `{min_apy:,.0f}%`)\n"
                f"**Daily emission:** `{emission:,.0f} DFUN/day` "
                f"shared across all stakers"
            )
            .field(
                "Server TVL",
                _line_dfun_usd(tvl_dfun, tvl_usd),
                True,
            )
            .field(
                "Your Position",
                _line_dfun_usd(user_value_dfun, user_usd)
                + (
                    f"\n({len(user_stakes)} stake"
                    f"{'s' if len(user_stakes) != 1 else ''})"
                    if user_stakes else ""
                ),
                True,
            )
            .field(
                "Your Pending",
                _line_dfun_usd(user_pending_dfun, pending_usd),
                True,
            )
            .field(
                "Stake",
                f"`{ctx.prefix}fun stake SYMBOL <amt|all>`\n"
                f"`{ctx.prefix}fun stake everything`  -  full bag",
                False,
            )
            .field(
                "Manage",
                f"`{ctx.prefix}fun stakes`  -  list positions\n"
                f"`{ctx.prefix}fun claim [SYMBOL]`  -  sweep pending DFUN\n"
                f"`{ctx.prefix}fun autocompound SYMBOL [on|off]`\n"
                f"`{ctx.prefix}fun unstake SYMBOL <amt|all>`",
                False,
            )
            .footer(
                f"DFUN spot ≈ {_fmt_usd(dfun_usd)} per token"
                if dfun_usd > 0 else
                "DFUN/USD oracle unavailable -- USD values hidden."
            )
        )
        await ctx.reply(
            embed=embed.build(), mention_author=False,
            view=_StakeOverviewView(self, ctx),
        )

    async def _send_deploy_help(self, ctx: DiscoContext) -> None:
        cfg = Config.DISCFUN
        qsym = _qsym()
        edit_fee = float(cfg["deploy_fee"]) * _df.EDIT_FEE_MULTIPLIER
        inactivity_days = _df.INACTIVITY_DESTROY_SECS // 86400
        usage = (
            "**Launch a proto token on the Disc.Fun bonding curve.**\n"
            "```\n"
            f"{ctx.prefix}fun deploy SYMBOL \"Token Name\" EMOJI\n"
            "```\n"
            f"**Cost:** `{cfg['deploy_fee']:,.0f} {qsym}` (flat, paid in {qsym} on Discoin Network)\n"
            f"**Limit:** 1 deploy per user per server per 24 hours\n"
            f"**Emoji:** unicode glyph or a Discord custom emoji "
            f"(`<:name:id>` / `<a:name:id>`) -- pick from the picker and "
            f"Discord will paste it for you.\n"
            f"**Edit later:** `{ctx.prefix}fun edit SYMBOL \"New Name\" "
            f"emoji` for `{edit_fee:,.0f} {qsym}` (creator only, locked "
            f"once graduated).\n"
            f"**Use it or lose it:** if no one buys for **{inactivity_days} "
            f"days**, the proto is destroyed and every holder's balance "
            f"is wiped. Sells don't refresh the timer -- only buys.\n"
            f"**Locked-in defaults:** total `{cfg['total_supply']:,}`, "
            f"curve `{cfg['curve_supply']:,}`, "
            f"trade fee `{cfg['trade_fee_bps'] / 100:.2f}%`, "
            f"graduation @ `{cfg['graduation_quote']:,.0f} {qsym}`.\n\n"
            "Need full control over supply, price and fees? Use "
            f"`{ctx.prefix}token deploy` (Protocol Dev tier required) instead."
        )
        await ctx.reply(usage, mention_author=False)

    # ── List / info ────────────────────────────────────────────────────────

    @fun.command(name="list", aliases=["ls", "active", "browse"])
    @guild_only
    async def fun_list(self, ctx: DiscoContext, sort: str = "hot") -> None:
        """Show active protos. Sort = hot | new | progress | mcap."""
        sort = sort.lower()
        if sort not in _VALID_SORTS:
            await ctx.reply_error(
                f"Sort must be one of: `{'`, `'.join(sorted(_VALID_SORTS))}`."
            )
            return
        rows = await _df.list_active_protos(ctx.db, ctx.guild_id, limit=12, sort=sort)
        sort_label = {
            "hot": "🔥 Hot (volume)",
            "new": "🆕 New",
            "progress": "📈 Near graduation",
            "mcap": "💎 Top market cap",
        }[sort]

        if not rows:
            embed = card(
                "🎢 Disc.Fun -- No Active Protos",
                description=(
                    f"Nobody has launched a proto token here yet.\n"
                    f"Be the first: `{ctx.prefix}fun deploy SYMBOL \"Name\" 🚀`"
                ),
                color=C_NAVY,
            )
            await ctx.reply(embed=embed.build(), mention_author=False)
            return

        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        body = "\n\n".join(
            _proto_summary_line(r, idx=i+1, dfun_usd=dfun_usd)
            for i, r in enumerate(rows)
        )
        embed = (
            card(f"🎢 Disc.Fun -- {sort_label}", color=C_GOLD)
            .description(body)
            .footer(
                f"Open the live panel: {ctx.prefix}fun info <symbol>  ·  "
                f"Other sorts: hot / new / progress / mcap"
            )
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @fun.command(name="info", aliases=["view", "card"])
    @guild_only
    async def fun_info(self, ctx: DiscoContext, symbol: str = None) -> None:
        """Show curve state + graduation progress + buy/sell buttons."""
        if not symbol:
            await ctx.reply_error(f"Usage: `{ctx.prefix}fun info SYMBOL`")
            return
        sym = symbol.upper()
        proto = await _df.get_proto_by_symbol(ctx.db, ctx.guild_id, sym)
        if proto is None:
            await ctx.reply_error(f"No proto token `{sym}` on this server.")
            return

        viewer_holding = await _df.get_user_proto_holding(
            ctx.db, int(proto["proto_id"]), ctx.author.id,
        )
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        embed = _build_info_embed(
            ctx, proto, viewer_holding_raw=viewer_holding, dfun_usd=dfun_usd,
        )
        view = None if proto["graduated"] else FunPanelView(self, ctx, int(proto["proto_id"]))
        msg = await ctx.reply(embed=embed.build(), view=view, mention_author=False)
        if view is not None:
            view.message = msg

    @fun.command(name="chart", aliases=["c", "candles"])
    @guild_only
    async def fun_chart(
        self, ctx: DiscoContext, symbol: str = None, timeframe: str = "5m",
    ) -> None:
        """Render a candlestick chart of a proto's bonding-curve trades.

        Usage: {prefix}fun chart SYMBOL [timeframe]
        Timeframes: 1m 5m 15m 1h 4h 1d
        """
        if not symbol:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}fun chart SYMBOL [timeframe]`"
            )
            return
        sym = symbol.upper()
        tf = timeframe.lower()
        if tf not in _df.CHART_TIMEFRAMES:
            await ctx.reply_error(
                f"Unknown timeframe `{tf}`. Valid: "
                f"{', '.join(_df.CHART_TIMEFRAMES)}"
            )
            return
        proto = await _df.get_proto_by_symbol(ctx.db, ctx.guild_id, sym)
        if proto is None:
            await ctx.reply_error(f"No proto token `{sym}` on this server.")
            return
        await self._send_proto_chart(ctx, proto, tf)

    async def _send_proto_chart(
        self, ctx: DiscoContext, proto, tf: str, *,
        reply_target=None,
    ) -> None:
        """Build + render + send the Disc.Fun-themed chart for a proto."""
        if not _HAS_PLAYWRIGHT:
            err = (
                "Charts need playwright to render. Install it on the host with "
                "`uv pip install --system 'playwright>=1.40.0' && "
                "playwright install chromium`."
            )
            target = reply_target if reply_target is not None else ctx
            if hasattr(target, "reply_error"):
                await target.reply_error(err)
            else:
                await target.followup.send(f"❌ {err}", ephemeral=True)
            return

        sym = proto["symbol"]
        tf_secs = _df.CHART_TIMEFRAMES[tf]
        # Pull a generous window so 1m candles still get coverage on a busy
        # token, while 1d candles get historical depth.
        since_ts = int(time.time()) - max(tf_secs * 500, 86400 * 7)
        proto_id = int(proto["proto_id"])
        trades = await _df.fetch_proto_trades_for_candles(ctx.db, proto_id, since_ts)
        candles = _df.build_proto_candles(trades, tf_secs)

        # Genesis pad + trailing live candle so a fresh proto still renders.
        origin = _df.synthetic_origin_candle(proto)
        if origin is not None and (not candles or candles[0]["ts"] > origin["ts"]):
            candles = [origin, *candles]
        live = _df.current_spot_candle(proto, int(time.time()))
        if live is not None:
            if candles and live["ts"] // tf_secs == candles[-1]["ts"] // tf_secs:
                # Same bucket -> extend the last candle's wicks/close to live.
                last = candles[-1]
                last["high"]  = max(last["high"], live["close"])
                last["low"]   = min(last["low"],  live["close"])
                last["close"] = live["close"]
            else:
                candles.append({**live, "ts": (int(time.time()) // tf_secs) * tf_secs})

        if len(candles) < 2:
            await ctx.reply_error(
                f"Not enough trade history for **{sym}** yet. "
                f"Make a trade to seed the chart, or wait for activity."
            )
            return

        # Volume series (post-aggregation) for the sub-panel.
        vol_series = [
            {"time": c["ts"], "value": c.get("volume", 0.0)} for c in candles
        ]
        # Default EMA20 overlay.
        closes = [c["close"] for c in candles]
        ema = _ema_simple(closes, 20)
        ema_series = [
            {"time": c["ts"], "value": v}
            for c, v in zip(candles, ema) if v is not None
        ]

        chart_data: dict = {
            "pair": f"{sym}/DFUN",
            "tf":   tf.upper(),
            "candles": _df.candles_to_lwc(candles),
            "indicators": {
                "ema": {"EMA20": ema_series},
                "vol": vol_series,
            },
            "grad_price": _df.graduation_price_dfun(proto),
        }

        try:
            tmpl = _PROTO_TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError:
            await ctx.reply_error("Disc.Fun chart template missing on host.")
            return
        injection = f"<script>window.CHART_DATA = {json.dumps(chart_data)};</script>"
        html_content = tmpl.replace("</head>", injection + "\n</head>")

        async with ctx.typing():
            with tempfile.NamedTemporaryFile(
                suffix=".html", delete=False, mode="w", encoding="utf-8",
            ) as f:
                f.write(html_content)
                tmp_path = f.name
            try:
                png_bytes = await _render_proto_chart(tmp_path.replace("\\", "/"))
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        embed = (
            card(
                f"🎢 {proto['emoji']} {sym}/DFUN  ·  {tf.upper()}",
                color=C_PURPLE,
            )
            .image("attachment://discfun_chart.png")
            .footer(
                f"Disc.Fun bonding curve · "
                f"graduation @ {Config.DISCFUN['graduation_quote']:,.0f} DFUN"
            )
        )
        file = discord.File(io.BytesIO(png_bytes), filename="discfun_chart.png")
        target = reply_target if reply_target is not None else ctx
        if hasattr(target, "reply"):
            await target.reply(embed=embed.build(), file=file, mention_author=False)
        else:
            await target.followup.send(
                embed=embed.build(), file=file, ephemeral=False,
            )

    @fun.command(name="grads", aliases=["graduates", "graduated"])
    @guild_only
    async def fun_grads(self, ctx: DiscoContext) -> None:
        """Show recently graduated proto tokens."""
        rows = await _df.list_recent_graduates(ctx.db, ctx.guild_id, limit=10)
        if not rows:
            embed = card(
                "Disc.Fun -- No Graduations Yet",
                description=(
                    f"No proto has hit the {Config.DISCFUN['graduation_quote']:,.0f} "
                    f"{_qsym()} threshold yet.\n"
                    f"Push one over the line: `{ctx.prefix}fun list progress`"
                ),
                color=C_NAVY,
            )
            await ctx.reply(embed=embed.build(), mention_author=False)
            return
        lines = []
        for r in rows:
            lines.append(
                f"{r['emoji']} **{r['symbol']}** -- {r['name']}\n"
                f"  Trade: `{ctx.prefix}buy {r['symbol']}` · "
                f"Graduated {fmt_ts(r['graduated_at'])}"
            )
        embed = (
            card("🎓 Disc.Fun -- Graduates", color=C_PURPLE)
            .description("\n\n".join(lines))
            .footer(f"Each graduate has a {_qsym()}/SYMBOL pool and a DSC bridge.")
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    # ── Buy / sell ─────────────────────────────────────────────────────────

    @fun.command(name="buy", aliases=["ape", "long"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_buy(
        self, ctx: DiscoContext, symbol: str = None, *, amount: str = None,
    ) -> None:
        """Buy proto tokens with DFUN.

        Usage: {prefix}fun buy SYMBOL <amount|all|50%>
        Examples: {prefix}fun buy MEME 100   {prefix}fun buy MEME 1k   {prefix}fun buy MEME all
        """
        if not symbol or not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}fun buy SYMBOL <{_qsym()}_amount|all|%>`"
            )
            return
        sym = symbol.upper()
        proto = await _df.get_proto_by_symbol(ctx.db, ctx.guild_id, sym)
        if proto is None:
            await ctx.reply_error(f"No proto token `{sym}` on this server.")
            return
        if proto["graduated"]:
            await ctx.reply_error(
                f"`{sym}` has graduated. Trade with `{ctx.prefix}buy {sym}`."
            )
            return

        # Parse amount: number, "all"/"max" (full DFUN balance), or "X%".
        held = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, "dsc", _qsym())
        bal_h = to_human(int(held["amount"])) if held else 0.0
        amt_str = amount.strip().lower().replace(",", "")
        if amt_str in ("all", "max"):
            amt = bal_h
        elif amt_str.endswith("%"):
            try:
                pct = float(amt_str[:-1])
            except ValueError:
                await ctx.reply_error("Invalid percentage.")
                return
            if pct <= 0 or pct > 100:
                await ctx.reply_error("Percentage must be 0-100.")
                return
            amt = bal_h * (pct / 100.0)
        else:
            parsed = _parse_amount(amt_str)
            if parsed is None or parsed <= 0:
                await ctx.reply_error("Amount must be a positive number, `all`, or `X%`.")
                return
            amt = parsed
        if amt <= 0:
            await ctx.reply_error(
                f"You don't hold any {_qsym()} to spend. Buy some first: "
                f"`{ctx.prefix}buy {_qsym()}`."
            )
            return
        await self._execute_buy(ctx, proto, amt, reply_target=ctx)

    @fun.command(name="sell", aliases=["dump", "short"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_sell(
        self, ctx: DiscoContext, symbol: str = None, *, amount: str = None,
    ) -> None:
        """Sell proto tokens back to the curve.

        Usage: {prefix}fun sell SYMBOL <amount|all|50%>
        """
        if not symbol or not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}fun sell SYMBOL <amount|all|%>`"
            )
            return
        sym = symbol.upper()
        proto = await _df.get_proto_by_symbol(ctx.db, ctx.guild_id, sym)
        if proto is None:
            await ctx.reply_error(f"No proto token `{sym}` on this server.")
            return
        if proto["graduated"]:
            await ctx.reply_error(
                f"`{sym}` has graduated. Use `{ctx.prefix}sell {sym}` instead."
            )
            return
        await self._execute_sell(ctx, proto, amount, reply_target=ctx)

    # ── Bag ────────────────────────────────────────────────────────────────

    @fun.command(name="bag", aliases=["holdings", "portfolio", "positions"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_bag(self, ctx: DiscoContext) -> None:
        """List your active proto-token positions with live PnL."""
        rows = await _df.list_user_proto_holdings(
            ctx.db, ctx.guild_id, ctx.author.id,
        )
        active = [r for r in rows if not r["graduated"]]
        if not active:
            embed = card(
                "🎒 Your Disc.Fun Bag",
                description=(
                    f"You don't hold any active proto tokens.\n"
                    f"Browse with `{ctx.prefix}fun list` or launch your own with "
                    f"`{ctx.prefix}fun deploy SYMBOL \"Name\" 🚀`."
                ),
                color=C_NAVY,
            )
            await ctx.reply(embed=embed.build(), mention_author=False)
            return

        qsym = _qsym()
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        lines = []
        total_value = 0.0
        total_cost = 0.0
        for r in active:
            held = r.h("amount")
            cost = r.h("cost_basis")
            v_q = int(r["virtual_quote"])
            v_t = int(r["virtual_token"])
            spot = (v_q / v_t) if v_t else 0.0
            value = held * spot
            pnl = value - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
            arrow = "🟢" if pnl >= 0 else "🔴"
            total_value += value
            total_cost += cost
            value_usd_str = (
                f" ≈ {_fmt_usd(value * dfun_usd)}" if dfun_usd > 0 else ""
            )
            lines.append(
                f"{r['emoji']} **{r['symbol']}**  -  `{held:,.4f}` @ `{_fmt_proto_price(spot, dfun_usd=dfun_usd)}`\n"
                f"  ≈ `{value:,.4f} {qsym}`{value_usd_str} · cost `{cost:,.4f}` · "
                f"{arrow} `{pnl:+,.4f} {qsym}` (`{pnl_pct:+.2f}%`)"
            )

        total_pnl = total_value - total_cost
        total_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
        total_arrow = "🟢" if total_pnl >= 0 else "🔴"
        usd_suffix = f" ≈ {_fmt_usd(total_value * dfun_usd)}" if dfun_usd > 0 else ""
        cost_usd_suffix = f" ≈ {_fmt_usd(total_cost * dfun_usd)}" if dfun_usd > 0 else ""
        pnl_usd_suffix = f" ≈ {_fmt_usd(total_pnl * dfun_usd)}" if dfun_usd > 0 and total_pnl != 0 else ""
        embed = (
            card("🎒 Your Disc.Fun Bag", color=C_GOLD)
            .description("\n\n".join(lines))
            .field(
                "Bag Total",
                f"Value `{total_value:,.4f} {qsym}`{usd_suffix}\n"
                f"Cost `{total_cost:,.4f} {qsym}`{cost_usd_suffix}\n"
                f"{total_arrow} PnL `{total_pnl:+,.4f} {qsym}` "
                f"(`{total_pct:+.2f}%`){pnl_usd_suffix}",
                False,
            )
            .footer(f"{ctx.prefix}fun sell SYMBOL <amt|all|%> to dump · "
                    f"{ctx.prefix}fun info SYMBOL for live panel")
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    # ── Staking (graduated tokens -> DFUN yield) ──────────────────────────

    @fun.command(name="stake")
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_stake(
        self, ctx: DiscoContext, symbol: str = None, *, amount: str = None,
    ) -> None:
        """Stake graduated Disc.Fun tokens to earn DFUN yield.

        Usage:
          {prefix}fun stake                         -- staking overview
          {prefix}fun stake SYMBOL <amt|all>
          {prefix}fun stake everything              -- stake every graduated token you hold
        """
        cfg = Config.DISCFUN
        if not symbol:
            await self._send_stake_overview(ctx)
            return
        # Live APY drives both the receipt embed and the
        # _stake_everything path so the value displayed matches what
        # accruals will actually use.
        apy_pct = await _df.current_staking_apy_pct(ctx.db, ctx.guild_id)
        sym_lower = symbol.lower()
        if sym_lower in ("everything", "all", "*"):
            await self._stake_everything(ctx, apy_pct)
            return
        if not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}fun stake {symbol.upper()} <amt|all>`"
            )
            return
        sym = symbol.upper()
        # Overload: ``,fun stake SYM autocompound [on|off]`` toggles the flag
        # on the existing position, mirroring the Safety Module's
        # ``,stake vtr autocompound`` pattern.
        if amount.strip().lower().split()[0] in ("autocompound", "ac", "compound"):
            parts = amount.strip().lower().split()
            choice = parts[1] if len(parts) > 1 else None
            await self._toggle_autocompound(ctx, sym, choice)
            return
        # Resolve amount.
        held = await ctx.db.get_wallet_holding(ctx.author.id, ctx.guild_id, "dsc", sym)
        held_raw = int(held["amount"]) if held else 0
        if held_raw <= 0:
            await ctx.reply_error(f"You don't hold any `{sym}` to stake.")
            return
        amt_str = amount.strip().lower().replace(",", "")
        if amt_str in ("all", "max"):
            amount_raw = held_raw
        else:
            n = _parse_amount(amt_str)
            if n is None or n <= 0:
                await ctx.reply_error("Stake amount must be positive.")
                return
            amount_raw = to_raw(n)
            if amount_raw > held_raw:
                await ctx.reply_error(
                    f"You only hold `{to_human(held_raw):,.4f}` {sym}."
                )
                return

        try:
            staked, row = await _df.stake_token(
                ctx.db, guild_id=ctx.guild_id, user_id=ctx.author.id,
                symbol=sym, amount_raw=amount_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        spot_dfun = await _df._amm_spot_dfun(ctx.db, ctx.guild_id, sym)
        value_dfun = to_human(int(row["amount"])) * spot_dfun
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        position_usd = value_dfun * dfun_usd if dfun_usd > 0 else 0.0
        staked_value_dfun = to_human(staked) * spot_dfun
        staked_value_usd = staked_value_dfun * dfun_usd if dfun_usd > 0 else 0.0
        embed = (
            card(f"🔒 Staked {sym}", color=C_SUCCESS)
            .field(
                "Just Staked",
                f"`{to_human(staked):,.4f}` {sym}\n"
                + (
                    f"≈ `{staked_value_dfun:,.4f} DFUN` ({_fmt_usd(staked_value_usd)})"
                    if dfun_usd > 0 else
                    f"≈ `{staked_value_dfun:,.4f} DFUN`"
                ),
                True,
            )
            .field(
                "Position",
                f"`{to_human(int(row['amount'])):,.4f}` {sym}\n"
                + (
                    f"≈ `{value_dfun:,.4f} DFUN` ({_fmt_usd(position_usd)})"
                    if dfun_usd > 0 else
                    f"≈ `{value_dfun:,.4f} DFUN`"
                ),
                True,
            )
            .field(
                "APY (live, variable)",
                f"`{apy_pct:,.1f}%` -- compresses as guild-wide TVL grows",
                True,
            )
            .footer(
                f"🔁 auto-compound: {ctx.prefix}fun autocompound {sym} on  ·  "
                f"{ctx.prefix}fun claim {sym}  ·  "
                f"{ctx.prefix}fun unstake {sym} <amt|all>"
            )
        )
        await ctx.reply(
            embed=embed.build(), mention_author=False,
            view=_StakeActionsView(self, ctx, sym),
        )

    async def _toggle_autocompound(
        self, ctx: DiscoContext, sym: str, choice: str | None,
    ) -> None:
        """Flip the auto_compound flag on an existing stake row."""
        existing = await ctx.db.fetch_one(
            "SELECT auto_compound, amount FROM discfun_stakes "
            "WHERE guild_id=$1 AND user_id=$2 AND symbol=$3",
            ctx.guild_id, ctx.author.id, sym,
        )
        if existing is None or int(existing.get("amount") or 0) <= 0:
            await ctx.reply_error(
                f"You don't have any `{sym}` staked. Stake some first: "
                f"`{ctx.prefix}fun stake {sym} all`."
            )
            return
        cur = bool(existing.get("auto_compound"))
        if choice in (None, "toggle"):
            new_val = not cur
        elif choice in ("on", "true", "enable", "1", "yes"):
            new_val = True
        elif choice in ("off", "false", "disable", "0", "no"):
            new_val = False
        else:
            await ctx.reply_error("Use `on`, `off`, or `toggle`.")
            return
        if new_val == cur:
            state = "ON" if cur else "OFF"
            await ctx.reply_error(f"Autocompound on `{sym}` is already **{state}**.")
            return
        try:
            await _df.set_autocompound(
                ctx.db, guild_id=ctx.guild_id, user_id=ctx.author.id,
                symbol=sym, enabled=new_val,
            )
        except Exception as exc:
            log.exception("set_autocompound failed sym=%s", sym)
            await ctx.reply_error(f"Couldn't toggle: {exc}")
            return
        if new_val:
            msg = (
                f"🔁 **Autocompound ON** for `{sym}`.\n"
                f"Future yield will be virtually swapped DFUN→{sym} at spot "
                f"and added back to your stake position. No AMM round-trip "
                f"means no slippage on the compound step."
            )
            color = C_SUCCESS
        else:
            msg = (
                f"⚙️ **Autocompound OFF** for `{sym}`.\n"
                f"Future yield accrues as DFUN in `pending` -- claim with "
                f"`{ctx.prefix}fun claim {sym}`."
            )
            color = C_INFO
        embed = card(
            title=f"Autocompound {'ON' if new_val else 'OFF'}",
            description=msg, color=color,
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @fun.command(name="autocompound", aliases=["ac", "compound"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_autocompound(
        self, ctx: DiscoContext, symbol: str = None, choice: str = None,
    ) -> None:
        """Toggle autocompound on a stake (alias for `,fun stake SYM autocompound`).

        Usage: {prefix}fun autocompound SYMBOL [on|off|toggle]
        """
        if not symbol:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}fun autocompound SYMBOL [on|off|toggle]`"
            )
            return
        await self._toggle_autocompound(
            ctx, symbol.upper(), (choice or "").strip().lower() or None,
        )

    async def _stake_everything(self, ctx: DiscoContext, apy_pct: float) -> None:
        """Stake the full balance of every graduated Disc.Fun token the user holds."""
        rows = await _df.list_graduated_holdings(ctx.db, ctx.guild_id, ctx.author.id)
        if not rows:
            await ctx.reply_error(
                "You don't hold any graduated Disc.Fun tokens to stake. "
                f"Buy some on the curve first: `{ctx.prefix}fun list`."
            )
            return
        results: list[str] = []
        total_value_dfun = 0.0
        failed = 0
        for r in rows:
            sym = str(r["symbol"])
            amt_raw = int(r["amount"] or 0)
            if amt_raw <= 0:
                continue
            try:
                staked, srow = await _df.stake_token(
                    ctx.db, guild_id=ctx.guild_id, user_id=ctx.author.id,
                    symbol=sym, amount_raw=amt_raw,
                )
            except Exception as exc:
                failed += 1
                log.debug("stake everything: skipping %s -- %s", sym, exc)
                continue
            spot = await _df._amm_spot_dfun(ctx.db, ctx.guild_id, sym)
            staked_h = to_human(staked)
            value_dfun = staked_h * spot
            total_value_dfun += value_dfun
            results.append(
                f"{r['emoji']} **{sym}**  ·  staked `{staked_h:,.4f}`  ≈ `{value_dfun:,.4f} DFUN`"
            )
        if not results:
            await ctx.reply_error("Nothing to stake.")
            return
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        usd_suffix = f" ≈ {_fmt_usd(total_value_dfun * dfun_usd)}" if dfun_usd > 0 else ""
        embed = (
            card("🔒 Staked Everything", color=C_SUCCESS)
            .description("\n".join(results))
            .field(
                "Total Position Value",
                f"`{total_value_dfun:,.4f} DFUN`{usd_suffix}",
                True,
            )
            .field(
                "APY (live, variable)",
                f"`{apy_pct:,.1f}%` -- compresses as TVL grows",
                True,
            )
            .footer(
                f"{failed} skipped · "
                f"{ctx.prefix}fun stakes to track  ·  "
                f"{ctx.prefix}fun claim to sweep yield"
            )
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @fun.command(name="unstake", aliases=["withdraw"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_unstake(
        self, ctx: DiscoContext, symbol: str = None, *, amount: str = None,
    ) -> None:
        """Unstake graduated tokens (auto-claims accrued DFUN yield).

        Usage: {prefix}fun unstake SYMBOL <amt|all>
        """
        if not symbol or not amount:
            await ctx.reply_error(
                f"Usage: `{ctx.prefix}fun unstake SYMBOL <amt|all>`"
            )
            return
        sym = symbol.upper()
        rows = await _df.list_user_stakes(
            ctx.db, ctx.guild_id, ctx.author.id, accrue=False,
        )
        existing = next((r for r in rows if str(r["symbol"]) == sym), None)
        if existing is None:
            await ctx.reply_error(f"You don't have any `{sym}` staked.")
            return
        staked_raw = int(existing["amount"] or 0)
        amt_str = amount.strip().lower().replace(",", "")
        if amt_str in ("all", "max"):
            amount_raw = staked_raw
        elif amt_str.endswith("%"):
            try:
                pct = float(amt_str[:-1])
            except ValueError:
                await ctx.reply_error("Invalid percentage.")
                return
            if pct <= 0 or pct > 100:
                await ctx.reply_error("Percentage must be 0-100.")
                return
            amount_raw = (staked_raw * int(pct * 1000)) // 100_000
        else:
            n = _parse_amount(amt_str)
            if n is None or n <= 0:
                await ctx.reply_error("Unstake amount must be positive.")
                return
            amount_raw = to_raw(n)
        if amount_raw <= 0 or amount_raw > staked_raw:
            await ctx.reply_error(
                f"You only have `{to_human(staked_raw):,.4f}` {sym} staked."
            )
            return

        try:
            unstaked, claimed, _row = await _df.unstake_token(
                ctx.db, guild_id=ctx.guild_id, user_id=ctx.author.id,
                symbol=sym, amount_raw=amount_raw,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return

        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        claimed_h = to_human(claimed)
        claim_str = f"`{claimed_h:,.4f} DFUN`"
        if dfun_usd > 0 and claimed_h > 0:
            claim_str += f"  ({_fmt_usd(claimed_h * dfun_usd)})"
        embed = (
            card(f"🔓 Unstaked {sym}", color=C_AMBER)
            .field("Withdrew", f"`{to_human(unstaked):,.4f}` {sym}", True)
            .field("Yield Claimed", claim_str, True)
            .footer(
                f"Tokens are back in your DSC-network DeFi wallet. "
                f"{ctx.prefix}fun stakes to see what's still staked."
            )
        )
        await ctx.reply(embed=embed.build(), mention_author=False)

    @fun.command(name="claim", aliases=["harvest"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_claim(self, ctx: DiscoContext, symbol: str = None) -> None:
        """Claim accrued DFUN yield without unstaking.

        Usage: {prefix}fun claim [SYMBOL]    (no symbol = claim everything)
        """
        if not symbol:
            claimed = await _df.claim_all_stakes(
                ctx.db, guild_id=ctx.guild_id, user_id=ctx.author.id,
            )
            if claimed <= 0:
                await ctx.reply_error("Nothing to claim yet. Patience -- yield accrues per-second.")
                return
            dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
            claimed_h = to_human(claimed)
            usd_suffix = f"  ({_fmt_usd(claimed_h * dfun_usd)})" if dfun_usd > 0 else ""
            await ctx.reply_success(
                f"Claimed `{claimed_h:,.4f} DFUN`{usd_suffix} across all stakes.",
                title="🌾 Yield Claimed",
            )
            return
        sym = symbol.upper()
        try:
            claimed = await _df.claim_stake(
                ctx.db, guild_id=ctx.guild_id, user_id=ctx.author.id, symbol=sym,
            )
        except Exception as exc:
            log.exception("fun claim failed for sym=%s", sym)
            await ctx.reply_error(f"Couldn't claim `{sym}`: {exc}")
            return
        if claimed <= 0:
            await ctx.reply_error(
                f"No DFUN yield pending on `{sym}`. (Or you don't have any staked.)"
            )
            return
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        claimed_h = to_human(claimed)
        usd_suffix = f"  ({_fmt_usd(claimed_h * dfun_usd)})" if dfun_usd > 0 else ""
        await ctx.reply_success(
            f"Claimed `{claimed_h:,.4f} DFUN`{usd_suffix} from `{sym}` stake.",
            title="🌾 Yield Claimed",
        )

    @fun.command(name="stakes", aliases=["staked"])
    @guild_only
    @no_bots
    @ensure_registered
    async def fun_stakes(self, ctx: DiscoContext) -> None:
        """Show your active Disc.Fun stakes with live yield."""
        rows = await _df.list_user_stakes(
            ctx.db, ctx.guild_id, ctx.author.id, accrue=True,
        )
        if not rows:
            embed = card(
                "🔒 Your Disc.Fun Stakes",
                description=(
                    f"You're not staking any graduated Disc.Fun tokens yet.\n"
                    f"Stake a position to earn DFUN yield: "
                    f"`{ctx.prefix}fun stake SYMBOL all`."
                ),
                color=C_NAVY,
            )
            await ctx.reply(embed=embed.build(), mention_author=False)
            return

        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        apy_pct = await _df.current_staking_apy_pct(ctx.db, ctx.guild_id)
        lines = []
        total_value_dfun = 0.0
        total_pending_dfun = 0.0
        total_claimed_dfun = 0.0
        for r in rows:
            sym = str(r["symbol"])
            staked_h = r.h("amount")
            pending_h = r.h("pending_dfun")
            claimed_h = r.h("total_claimed")
            compounded_h = r.h("total_compounded")
            is_ac = bool(r.get("auto_compound"))
            spot = await _df._amm_spot_dfun(ctx.db, ctx.guild_id, sym)
            value_dfun = staked_h * spot
            total_value_dfun += value_dfun
            total_pending_dfun += pending_h
            total_claimed_dfun += claimed_h
            ac_badge = "🔁 **AUTO**" if is_ac else "⚙️ manual"
            pending_str = f"`{pending_h:,.4f} DFUN`"
            if dfun_usd > 0 and pending_h > 0:
                pending_str += f"  ({_fmt_usd(pending_h * dfun_usd)})"
            if is_ac:
                tail = (
                    f"  🪴 lifetime auto-restaked `{compounded_h:,.4f} {sym}`  ·  "
                    f"lifetime claimed `{claimed_h:,.4f} DFUN`"
                )
            else:
                tail = (
                    f"  🌾 pending {pending_str}  ·  "
                    f"lifetime claimed `{claimed_h:,.4f} DFUN`"
                )
            lines.append(
                f"**{sym}** {ac_badge}  ·  staked `{staked_h:,.4f}` "
                f"≈ `{value_dfun:,.4f} DFUN`\n{tail}"
            )

        usd_total = total_value_dfun * dfun_usd if dfun_usd > 0 else 0.0
        usd_pending = total_pending_dfun * dfun_usd if dfun_usd > 0 else 0.0
        embed = (
            card(
                f"🔒 Your Disc.Fun Stakes  ·  live APY {apy_pct:,.1f}% (variable)",
                color=C_GOLD,
            )
            .description("\n\n".join(lines))
            .field(
                "Total Staked",
                f"`{total_value_dfun:,.4f} DFUN`"
                + (f"  ≈ {_fmt_usd(usd_total)}" if usd_total > 0 else ""),
                True,
            )
            .field(
                "Pending Yield",
                f"`{total_pending_dfun:,.4f} DFUN`"
                + (f"  ≈ {_fmt_usd(usd_pending)}" if usd_pending > 0 else ""),
                True,
            )
            .field(
                "Lifetime Claimed",
                f"`{total_claimed_dfun:,.4f} DFUN`",
                True,
            )
            .footer(
                f"{ctx.prefix}fun claim to sweep all yield  ·  "
                f"{ctx.prefix}fun unstake SYM <amt|all>  ·  "
                f"{ctx.prefix}fun autocompound SYM to toggle 🔁"
            )
        )
        await ctx.reply(
            embed=embed.build(), mention_author=False,
            view=_StakeOverviewView(self, ctx),
        )

    # ── Shared trade execution (used by command + buttons) ────────────────

    async def _execute_buy(
        self,
        ctx: DiscoContext,
        proto,
        amount_q: float,
        *,
        reply_target,
        view: FunPanelView | None = None,
    ) -> None:
        """Run a buy and post (or reply with) a result embed."""
        sym = proto["symbol"]
        qsym = _qsym()
        requested_raw = to_raw(amount_q)
        # ``,fun buy SYM all`` round-trips raw -> float -> raw via
        # to_human / to_raw, and float64 can't represent every 18-decimal
        # raw integer exactly. The reconverted requested_raw therefore
        # sometimes lands one or two raw units ABOVE the player's actual
        # holding, which makes update_wallet_holding refuse the deduction
        # with "Insufficient DFUN balance" even though the player asked
        # to spend "all". Clamp at the raw boundary against the live
        # holding so a 100% spend always lands inside the available
        # balance regardless of float quirks. The panel quick-buy chips
        # benefit from the same guard for free.
        try:
            held = await ctx.db.get_wallet_holding(
                ctx.author.id, ctx.guild_id, "dsc", qsym,
            )
            held_raw = int(held["amount"]) if held else 0
        except Exception:
            held_raw = 0
        if requested_raw > held_raw:
            requested_raw = held_raw
        if requested_raw <= 0:
            err = (
                f"You don't hold any {qsym} to spend. Buy some first: "
                f"`{ctx.prefix}buy {qsym}`."
            )
            if hasattr(reply_target, "reply_error"):
                await reply_target.reply_error(err)
            else:
                await reply_target.followup.send(f"❌ {err}", ephemeral=True)
            return
        try:
            quote, updated, graduated_now, used_raw = await _df.buy_proto_token(
                ctx.db,
                guild_id=ctx.guild_id,
                user_id=ctx.author.id,
                proto_id=int(proto["proto_id"]),
                quote_in_raw=requested_raw,
            )
        except ValueError as exc:
            if hasattr(reply_target, "reply_error"):
                await reply_target.reply_error(str(exc))
            else:
                await reply_target.followup.send(f"❌ {exc}", ephemeral=True)
            return

        tokens_h = to_human(quote.tokens_out_raw)
        fee_h = to_human(quote.fee_quote_raw)
        actual_paid_h = to_human(int(used_raw))
        refund_raw = int(requested_raw) - int(used_raw)
        refunded_h = to_human(max(0, refund_raw))
        cap_clamped = refund_raw > 0
        new_spot = float(quote.new_virtual_quote) / float(quote.new_virtual_token)
        pct = _df.progress_pct(
            int(updated["real_quote_collected"]), int(updated["graduation_quote"]),
        )
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)
        avg_fill = (actual_paid_h - fee_h) / max(tokens_h, 1e-12)

        def _q(amt: float) -> str:
            base = _fmt_quote(amt)
            return f"{base} ({_fmt_usd(amt * dfun_usd)})" if dfun_usd > 0 and amt > 0 else base

        embed = (
            card(f"🟢 Bought {sym}", color=C_SUCCESS)
            .field(
                "Bought",
                fmt_token(tokens_h, sym, emoji=str(proto["emoji"])),
                True,
            )
            .field("Paid", _q(actual_paid_h), True)
            .field("Fee", _q(fee_h), True)
            .field("New Price", _fmt_proto_price(new_spot, dfun_usd=dfun_usd), True)
            .field("Progress", f"`{_progress_bar(pct, width=10)}` {pct*100:.2f}%", True)
            .field(
                "Avg Fill",
                _fmt_proto_price(avg_fill, dfun_usd=dfun_usd),
                True,
            )
        )
        if cap_clamped:
            embed.field(
                "Refunded",
                f"{_q(refunded_h)} -- only this much fit in the remaining "
                f"curve supply.",
                False,
            )
        if graduated_now:
            embed.description(
                f"🎓 **{sym} just graduated!** Your balance is now in your "
                f"Discoin Network DeFi wallet. Trade with `{ctx.prefix}buy {sym}`."
            )
            embed.color(C_GOLD)
        # Send result.
        if hasattr(reply_target, "reply"):
            await reply_target.reply(embed=embed.build(), mention_author=False)
        else:
            # Discord interaction follow-up.
            await reply_target.followup.send(embed=embed.build(), ephemeral=False)
        # Refresh the live panel if there is one.
        if view is not None and not graduated_now:
            try:
                fresh = await _df.get_proto_by_id(ctx.db, view.proto_id)
                if fresh:
                    held = await _df.get_user_proto_holding(
                        ctx.db, view.proto_id, ctx.author.id,
                    )
                    panel = _build_info_embed(ctx, fresh, viewer_holding_raw=held, dfun_usd=dfun_usd)
                    if view.message:
                        await view.message.edit(embed=panel)
            except Exception:
                log.debug("buy: failed to refresh panel", exc_info=True)

    async def _execute_sell(
        self,
        ctx: DiscoContext,
        proto,
        amount_str: str,
        *,
        reply_target,
        view: FunPanelView | None = None,
    ) -> None:
        sym = proto["symbol"]
        qsym = _qsym()
        proto_id = int(proto["proto_id"])
        held_raw = await _df.get_user_proto_holding(ctx.db, proto_id, ctx.author.id)
        if held_raw <= 0:
            err = f"You don't own any `{sym}`."
            if hasattr(reply_target, "reply_error"):
                await reply_target.reply_error(err)
            else:
                await reply_target.followup.send(f"❌ {err}", ephemeral=True)
            return

        amt_str = amount_str.strip().lower().replace(",", "")
        if amt_str in ("all", "max"):
            tokens_in_raw = held_raw
        elif amt_str.endswith("%"):
            try:
                pct = float(amt_str[:-1])
            except ValueError:
                await self._send_err(reply_target, "Invalid percentage.")
                return
            if pct <= 0 or pct > 100:
                await self._send_err(reply_target, "Percentage must be 0-100.")
                return
            tokens_in_raw = (held_raw * int(pct * 1000)) // 100_000
            if tokens_in_raw <= 0:
                await self._send_err(reply_target, "Sell amount rounded to zero.")
                return
        else:
            n = _parse_amount(amt_str)
            if n is None or n <= 0:
                await self._send_err(reply_target, "Amount must be positive.")
                return
            tokens_in_raw = to_raw(n)

        try:
            quote, _updated = await _df.sell_proto_token(
                ctx.db,
                guild_id=ctx.guild_id,
                user_id=ctx.author.id,
                proto_id=proto_id,
                tokens_in_raw=tokens_in_raw,
            )
        except ValueError as exc:
            await self._send_err(reply_target, str(exc))
            return

        sold_h = to_human(tokens_in_raw)
        net_h = to_human(quote.quote_out_raw)
        fee_h = to_human(quote.fee_quote_raw)
        new_spot = float(quote.new_virtual_quote) / float(quote.new_virtual_token)
        dfun_usd = await _live_dfun_usd(ctx.db, ctx.guild_id)

        def _q(amt: float) -> str:
            base = _fmt_quote(amt)
            return f"{base} ({_fmt_usd(amt * dfun_usd)})" if dfun_usd > 0 and amt > 0 else base

        embed = (
            card(f"🔴 Sold {sym}", color=C_AMBER)
            .field(
                "Sold",
                fmt_token(sold_h, sym, emoji=str(proto["emoji"])),
                True,
            )
            .field("Received", _q(net_h), True)
            .field("Fee", _q(fee_h), True)
            .field("New Price", _fmt_proto_price(new_spot, dfun_usd=dfun_usd), True)
            .field(
                "Avg Fill",
                _fmt_proto_price((net_h + fee_h) / max(sold_h, 1e-12), dfun_usd=dfun_usd),
                True,
            )
        )
        if hasattr(reply_target, "reply"):
            await reply_target.reply(embed=embed.build(), mention_author=False)
        else:
            await reply_target.followup.send(embed=embed.build(), ephemeral=False)
        if view is not None:
            try:
                fresh = await _df.get_proto_by_id(ctx.db, view.proto_id)
                if fresh:
                    held = await _df.get_user_proto_holding(
                        ctx.db, view.proto_id, ctx.author.id,
                    )
                    panel = _build_info_embed(ctx, fresh, viewer_holding_raw=held, dfun_usd=dfun_usd)
                    if view.message:
                        await view.message.edit(embed=panel)
            except Exception:
                log.debug("sell: failed to refresh panel", exc_info=True)

    @staticmethod
    async def _send_err(target, message: str) -> None:
        if hasattr(target, "reply_error"):
            await target.reply_error(message)
        else:
            try:
                await target.followup.send(f"❌ {message}", ephemeral=True)
            except Exception:
                pass

    # ── Button handlers ────────────────────────────────────────────────────

    async def _handle_buy_button(
        self,
        inter: discord.Interaction,
        view: FunPanelView,
        amount_q: float,
        *,
        deferred: bool = False,
    ) -> None:
        if not deferred:
            await inter.response.defer(ephemeral=False, thinking=False)
        ctx = view.ctx
        # The buyer is whoever pressed the button -- not the original ctx
        # author, so let anyone in the channel ape in.
        ctx_for_user = _ButtonCtxShim(ctx, inter.user)
        proto = await _df.get_proto_by_id(ctx.db, view.proto_id)
        if proto is None:
            await inter.followup.send("Proto vanished.", ephemeral=True)
            return
        if proto["graduated"]:
            await inter.followup.send(
                f"`{proto['symbol']}` has already graduated. Use {ctx.prefix}buy {proto['symbol']}.",
                ephemeral=True,
            )
            return
        await self._execute_buy(ctx_for_user, proto, amount_q, reply_target=inter, view=view)

    async def _handle_sell_button(
        self,
        inter: discord.Interaction,
        view: FunPanelView,
        amount_str: str,
        *,
        deferred: bool = False,
    ) -> None:
        if not deferred:
            await inter.response.defer(ephemeral=False, thinking=False)
        ctx = view.ctx
        ctx_for_user = _ButtonCtxShim(ctx, inter.user)
        proto = await _df.get_proto_by_id(ctx.db, view.proto_id)
        if proto is None:
            await inter.followup.send("Proto vanished.", ephemeral=True)
            return
        if proto["graduated"]:
            await inter.followup.send(
                f"`{proto['symbol']}` has graduated. Use {ctx.prefix}sell {proto['symbol']}.",
                ephemeral=True,
            )
            return
        await self._execute_sell(ctx_for_user, proto, amount_str, reply_target=inter, view=view)

    async def _show_holders(self, inter: discord.Interaction, proto_id: int) -> None:
        rows = await _df.list_top_holders(self.bot.db, proto_id, limit=10)
        proto = await _df.get_proto_by_id(self.bot.db, proto_id)
        if not rows or proto is None:
            await inter.response.send_message("No holders yet.", ephemeral=True)
            return
        sym = proto["symbol"]
        circ = int(proto["tokens_in_circulation"]) or 1
        lines = []
        for i, h in enumerate(rows, 1):
            held = int(h["amount"])
            share = held / circ * 100
            lines.append(
                f"`#{i:>2}` <@{int(h['user_id'])}>  -  "
                f"`{to_human(held):,.4f}` {sym} ({share:.2f}%)"
            )
        embed = (
            card(f"👥 {proto['emoji']} {sym} -- Top Holders", color=C_INFO)
            .description("\n".join(lines))
            .footer(f"Total holders: {int(proto['holder_count'])}")
        )
        await inter.response.send_message(
            embed=embed.build(), ephemeral=True,
        )

    async def _show_trades(self, inter: discord.Interaction, proto_id: int) -> None:
        rows = await _df.list_recent_trades(self.bot.db, proto_id, limit=10)
        proto = await _df.get_proto_by_id(self.bot.db, proto_id)
        if not rows or proto is None:
            await inter.response.send_message("No trades yet.", ephemeral=True)
            return
        sym = proto["symbol"]
        qsym = str(proto["quote_symbol"])
        lines = []
        for t in rows:
            arrow = "🟢" if t["side"] == "buy" else "🔴"
            q_h = to_human(int(t["quote_amount"]))
            tok_h = to_human(int(t["token_amount"]))
            lines.append(
                f"{arrow} <@{int(t['user_id'])}> "
                f"{t['side']} `{tok_h:,.0f}` {sym} for `{q_h:,.4f}` {qsym} "
                f"({fmt_ts(t['created_at'])})"
            )
        embed = (
            card(f"📜 {proto['emoji']} {sym} -- Recent Trades", color=C_INFO)
            .description("\n".join(lines))
            .footer(f"Lifetime: {int(proto['trade_count'])} trades · "
                    f"{to_human(int(proto['volume_quote'])):,.2f} {qsym} volume")
        )
        await inter.response.send_message(embed=embed.build(), ephemeral=True)


class _ButtonCtxShim:
    """Minimal ctx-like wrapper that swaps in the button presser as the author.

    Lets ``_execute_buy`` / ``_execute_sell`` and the staking command
    callbacks work for both prefix-command callers (real DiscoContext)
    and button presses (any guild member). Reply methods route through
    the interaction's followup so receipts post to the same channel
    after the View handler has already responded.

    Intentionally NOT using ``__slots__`` so the framework middleware
    (``@ensure_registered`` etc.) can write side-channel attributes
    like ``ctx.user_row`` onto the shim the same way it does on a
    real ``DiscoContext``. The earlier slotted version raised
    ``AttributeError: '_ButtonCtxShim' object has no attribute
    'user_row'`` the moment a re-invoked staking command hit the
    register-check middleware.
    """

    def __init__(
        self,
        ctx: DiscoContext,
        user_or_inter,
    ) -> None:
        # Backwards compatible: existing call sites pass ``inter.user``;
        # the new staking buttons pass the full ``Interaction`` so reply
        # helpers below can dispatch via ``inter.followup``.
        self._ctx = ctx
        if isinstance(user_or_inter, discord.Interaction):
            self._inter = user_or_inter
            self._user = user_or_inter.user
        else:
            self._inter = None
            self._user = user_or_inter

    @property
    def db(self):
        return self._ctx.db

    @property
    def bot(self):
        return self._ctx.bot

    @property
    def guild(self):
        return self._ctx.guild

    @property
    def guild_id(self) -> int:
        return self._ctx.guild_id

    @property
    def author(self) -> discord.User:
        return self._user

    @property
    def prefix(self) -> str:
        return self._ctx.prefix

    @property
    def channel(self):
        return self._ctx.channel

    # ── Reply helpers ──────────────────────────────────────────────────
    # When the shim was created from an Interaction we route through
    # ``inter.followup`` (the View must have already deferred). When it
    # was created from a User (legacy path) we fall back to the original
    # ctx so receipts still post even if reply_target wasn't threaded.

    async def _send(self, **kwargs) -> None:
        if self._inter is not None:
            kwargs.pop("mention_author", None)
            await self._inter.followup.send(**kwargs)
            return
        await self._ctx.reply(**kwargs)

    async def reply(self, content=None, **kwargs):
        if content is not None and "content" not in kwargs:
            kwargs["content"] = content
        await self._send(**kwargs)

    async def reply_error(self, msg: str) -> None:
        from core.framework.embed import card as _card
        from core.framework.ui import C_ERROR as _C_ERROR
        await self._send(
            embed=_card("Error", description=msg, color=_C_ERROR).build(),
        )

    async def reply_success(self, msg: str, title: str = "Success") -> None:
        from core.framework.embed import card as _card
        from core.framework.ui import C_SUCCESS as _C_SUCCESS
        await self._send(
            embed=_card(title, description=msg, color=_C_SUCCESS).build(),
        )

    async def reply_error_hint(
        self, msg: str, hint: str = "", command_name: str = "",
    ) -> None:
        body = msg + (f"\n\n💡 {hint}" if hint else "")
        await self.reply_error(body)

    async def send_embed(self, embed) -> None:
        await self._send(embed=embed)

    async def confirm(self, prompt: str, timeout: float = 30.0) -> bool:
        # Buttons always confirm -- they're already an explicit user action.
        return True

    async def get_guild_prefix(self) -> str:
        return self._ctx.prefix


# ── Setup ───────────────────────────────────────────────────────────────────

async def setup(bot: Discoin) -> None:
    await bot.add_cog(DiscFun(bot))
