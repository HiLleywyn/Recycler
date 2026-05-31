"""Helpers shared between the migrated ``$`` handlers.

Lifted out of :mod:`cogs.realmarket` to keep the cog file slim. Pure
functions only -- no DB or HTTP. Anything stateful goes through the
cog instance the handlers receive.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import discord

from core.framework.chart import (
    _adx,
    _atr,
    _bb,
    _ema,
    _macd,
    _rsi,
    default_layout,
)
from core.framework.embed import card
from core.framework.ui import (
    C_BEAR,
    C_BULL,
    C_GOLD,
    C_INFO,
    C_VOLATILE,
    fmt_pct,
    fmt_usd,
)
from services.pattern_scout import PatternMatch, lore as pattern_lore


_FOOTER_BRAND = "📡 Live · CoinGecko"
_LIVE_PREFIX = "[LIVE]"
_NEWS_FIELD_CHAR_CAP = 1024

# Timeframe -> seconds (must match SUPPORTED_TIMEFRAMES upstream).
_TF_SECONDS: dict[str, int] = {
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
    "4h":  14400,
    "1d":  86400,
}

# Tokens shaped like a timeframe (12h, 2d, 1w, ...). Used to detect when a
# user typed something timeframe-ish but unsupported.
_TF_SHAPE = re.compile(r"^\d+\s*[mhdw]$", re.IGNORECASE)
# Mention pattern for #channel parsing on $channels subcommands.
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
# Cap compare overlays so the legend stays readable.
_MAX_COMPARES = 3


def _fmt_big_usd(value: float | None) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000_000_000:
        return f"${v / 1_000_000_000_000:,.2f}T"
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:,.2f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:,.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:,.2f}K"
    return fmt_usd(v)


def _fmt_price_usd(value: float | None) -> str:
    """Price formatter that adapts decimals to magnitude. Sub-cent meme
    tokens need higher precision than the 2-decimal ``fmt_usd`` default.
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v == 0:
        return "$0.00"
    av = abs(v)
    if av >= 1:
        return f"${v:,.2f}"
    if av >= 0.01:
        return f"${v:,.4f}"
    if av >= 0.0001:
        return f"${v:,.6f}"
    return f"${v:,.8f}"


def _heat_emoji(pct: float | None) -> str:
    if pct is None:
        return "⚫"
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return "⚫"
    if v >= 5:
        return "🟩"
    if v > 0:
        return "🟢"
    if v <= -5:
        return "🟥"
    if v < 0:
        return "🔴"
    return "⚫"


def _fng_color(value: int) -> int:
    if value <= 25:
        return C_BEAR
    if value <= 45:
        return C_VOLATILE
    if value <= 55:
        return C_INFO
    if value <= 75:
        return C_BULL
    return C_GOLD


def _fng_emoji(value: int) -> str:
    if value <= 25:
        return "😱"
    if value <= 45:
        return "😟"
    if value <= 55:
        return "😐"
    if value <= 75:
        return "😀"
    return "🤑"


def _fmt_supply(value: float | None) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000_000_000:
        return f"{v / 1_000_000_000_000:,.2f}T"
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:,.2f}B"
    if v >= 1_000_000:
        return f"{v / 1_000_000:,.2f}M"
    if v >= 1_000:
        return f"{v:,.0f}"
    return f"{v:,.4f}"


def _parse_iso_to_epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        return int(dt.datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def _pct_chip(pct: Any) -> str:
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return "—"
    arrow = "▲" if v >= 0 else "▼"
    return f"{arrow} {fmt_pct(v)}"


def _summarize_indicators(candles: list[dict]) -> tuple[str, str]:
    """Compact indicator readout from recent OHLC. Returns
    ``(indicator_field_value, indicator_short_summary)``."""
    if len(candles) < 30:
        return ("Not enough history for indicators.", "n/a")
    highs  = [c["high"] for c in candles]
    lows   = [c["low"]  for c in candles]
    closes = [c["close"] for c in candles]
    rsi = _rsi(closes, 14)
    rsi_v = next((x for x in reversed(rsi) if x is not None), None)
    macd_line, sig_line, hist = _macd(closes)
    macd_v = macd_line[-1] if macd_line else None
    sig_v  = sig_line[-1]  if sig_line  else None
    hist_v = hist[-1]      if hist      else None
    ema20 = _ema(closes, 20)[-1] if closes else None
    ema50 = _ema(closes, 50)[-1] if len(closes) >= 50 else None
    upper, mid, lower = _bb(closes)
    bb_upper = next((x for x in reversed(upper) if x is not None), None)
    bb_lower = next((x for x in reversed(lower) if x is not None), None)
    adx_series, _p, _m = _adx(highs, lows, closes, 14)
    adx_v = next((x for x in reversed(adx_series) if x is not None), None)
    atr_v = next((x for x in reversed(_atr(highs, lows, closes, 14)) if x is not None), None)

    last = closes[-1]
    cross_label = "—"
    if ema20 is not None and ema50 is not None:
        cross_label = "bull (EMA20 > EMA50)" if ema20 > ema50 else "bear (EMA20 < EMA50)"

    bb_pos = "—"
    if bb_upper and bb_lower and bb_upper > bb_lower:
        rel = (last - bb_lower) / (bb_upper - bb_lower) * 100.0
        bb_pos = f"{rel:.0f}% of band"

    rsi_label = "n/a"
    if rsi_v is not None:
        if rsi_v >= 70:
            rsi_label = f"{rsi_v:.1f} (overbought)"
        elif rsi_v <= 30:
            rsi_label = f"{rsi_v:.1f} (oversold)"
        else:
            rsi_label = f"{rsi_v:.1f}"

    macd_label = "n/a"
    if macd_v is not None and sig_v is not None and hist_v is not None:
        direction = "bullish" if hist_v >= 0 else "bearish"
        macd_label = f"{macd_v:+.4f} / sig {sig_v:+.4f} ({direction})"

    adx_label = "n/a"
    if adx_v is not None:
        adx_label = f"{adx_v:.1f} (strong trend)" if adx_v >= 25 else f"{adx_v:.1f} (weak / ranging)"

    atr_label = "n/a" if atr_v is None else f"{atr_v:,.4f}"

    field_value = (
        f"• RSI(14): `{rsi_label}`\n"
        f"• MACD: `{macd_label}`\n"
        f"• EMA20 vs EMA50: `{cross_label}`\n"
        f"• BB %: `{bb_pos}`\n"
        f"• ADX(14): `{adx_label}`\n"
        f"• ATR(14): `{atr_label}`"
    )
    short = f"RSI {rsi_label.split(' ')[0]} · {cross_label.split(' ', 1)[0]}"
    return field_value, short


# ── pattern-scout rendering helpers ──────────────────────────────────────

_BIAS_TITLE_EMOJI = {"bullish": "📈", "bearish": "📉", "neutral": "📊"}
_BIAS_COLOR = {"bullish": C_BULL, "bearish": C_BEAR, "neutral": C_VOLATILE}
_STATUS_EMOJI = {
    "Forming":   "📊",
    "Confirmed": "✅",
    "Breakout":  "🚀",
    "Breakdown": "🩸",
}


def _pattern_emoji(match: PatternMatch) -> str:
    if "flag" in match.key:
        return "🚩"
    if "wedge" in match.key:
        return "📐"
    if "triangle" in match.key:
        return "🔺"
    if "head_shoulders" in match.key:
        return "👤"
    if "double" in match.key:
        return "♊"
    return "🔍"


def _default_scan_layout() -> dict:
    layout = default_layout()
    layout["view"] = "wide"
    return layout


def _fmt_volume_chip(value: float) -> str:
    if value <= 0:
        return "—"
    if value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:,.2f}T"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:,.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:,.2f}K"
    return f"{value:,.2f}"


def _build_context_line(ctx_data: dict) -> str:
    move_arrow = "▲" if ctx_data.get("move_pct", 0) >= 0 else "▼"
    parts = [
        f"{move_arrow} Window move: **{ctx_data['move_pct']:+.2f}%** "
        f"over {ctx_data['window_bars']} bars",
        f"📐 Range: `{ctx_data['range_pct']:.2f}%`  ·  "
        f"Volatility: `{ctx_data['volatility_pct']:.2f}%/bar`",
    ]
    if ctx_data.get("has_volume"):
        trend = ctx_data.get("volume_trend_pct", 0.0)
        trend_arrow = "▲" if trend >= 0 else "▼"
        parts.append(
            f"📦 Volume: `{_fmt_volume_chip(ctx_data['volume_total'])}` total "
            f"({_fmt_volume_chip(ctx_data['volume_avg'])}/bar) · "
            f"{trend_arrow} **{trend:+.1f}%** late-window vs early"
        )
    return "\n".join(parts)


def _build_scan_embed(
    match: PatternMatch, *, pair: str, timeframe: str,
    context: dict | None = None,
) -> discord.Embed:
    """Pattern-alert embed used by ``$scan``."""
    title_emoji = _BIAS_TITLE_EMOJI.get(match.bias, "📊")
    color = _BIAS_COLOR.get(match.bias, C_VOLATILE)

    pattern_emoji = _pattern_emoji(match)
    extra_chips = "  ·  ".join(
        f"{label}: {value}" for label, value in match.stats.items()
    )
    pattern_line = f"{pattern_emoji} **{match.name}**  ·  `{timeframe}`"
    if extra_chips:
        pattern_line += f"  ·  {extra_chips}"

    status_emoji = _STATUS_EMOJI.get(match.status, "📊")
    status_line = (
        f"{status_emoji} Status: **{match.status}**  ·  "
        f"Confidence: **{match.confidence:.0f}%**  ·  "
        f"Bias: **{match.bias.title()}**"
    )
    rs_line = (
        f"🔴 Resistance: **{match.resistance_touches}**  |  "
        f"🟢 Support: **{match.support_touches}**"
    )

    description = (
        f"{match.description} "
        + ("Pattern currently forming and worth watching. 👀"
           if match.status == "Forming"
           else f"Pattern is **{match.status.lower()}** -- act on the signal accordingly.")
        + "\n\n"
        + pattern_line + "\n"
        + rs_line + "\n"
        + status_line + "\n\n"
        + "*DYOR | Not financial advice.*"
    )

    builder = card(
        f"{title_emoji} {match.name} spotted on {pair} ({timeframe.upper()})",
        description=description,
        color=color,
    )

    blurb = pattern_lore(match.key)
    if blurb:
        builder = builder.field("📖 What this means", blurb, False)

    if context:
        builder = builder.field(
            "📈 Market context", _build_context_line(context), False,
        )

    return (
        builder
        .image("attachment://scan.png")
        .footer(_FOOTER_BRAND)
        .build()
    )
