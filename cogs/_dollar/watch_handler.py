"""``$watch`` -- personal market watchlist + alerts.

Subcommands:

- ``$watch`` / ``$watch list``       -- show entries
- ``$watch add SYMBOL [PRICE] [above|below]``
- ``$watch remove SYMBOL`` / ``$watch rm SYMBOL``
- ``$watch clear``

A row with ``target_price`` set is an active alert; the background
worker (``services/market/watch_worker.py``, wired in cogs/realmarket.py)
fires once per trigger. A row with no target is a passive watchlist
entry.
"""

from __future__ import annotations

import logging
from typing import Any

from core.config import Config
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_INFO, C_WARNING, fmt_usd

from services.market.router import get_router

log = logging.getLogger(__name__)


async def handle_watch(ctx: DiscoContext, raw_args: str) -> None:
    parts = (raw_args or "").split()
    sub = (parts[0] or "").lower() if parts else "list"
    rest = parts[1:]

    if sub in ("list", "ls", "show", ""):
        await _list_entries(ctx)
        return
    if sub in ("add", "a", "+"):
        await _add_entry(ctx, rest)
        return
    if sub in ("remove", "rm", "delete", "del", "-"):
        await _remove_entry(ctx, rest)
        return
    if sub in ("clear", "purge", "reset"):
        await _clear_entries(ctx)
        return

    # Treat bare ``$watch MTA`` as ``$watch add MTA``.
    await _add_entry(ctx, parts)


async def _list_entries(ctx: DiscoContext) -> None:
    try:
        rows = await ctx.db.fetch_all(
            "SELECT id, symbol, asset_class, target_price, direction, "
            "       triggered_at, created_at "
            "FROM market_watchlist "
            "WHERE user_id = $1 AND guild_id = $2 "
            "ORDER BY created_at DESC LIMIT 25",
            ctx.author.id, ctx.guild_id,
        )
    except Exception:
        log.exception("$watch list failed")
        await ctx.reply_error("Watchlist storage isn't responding right now.")
        return

    if not rows:
        await ctx.reply(
            embed=card(
                "👁️ $watch · empty",
                description=(
                    "You have no watch entries yet.\n\n"
                    "Add one with `$watch add MTA` (passive) or "
                    "`$watch add MTA 75000 above` (active alert)."
                ),
                color=C_INFO,
            ).build(),
            mention_author=False,
        )
        return

    embed = card("👁️ $watch · your list", color=C_INFO)
    for r in rows[:24]:
        sym = r.get("symbol") or "?"
        ac = r.get("asset_class") or "crypto"
        target = r.get("target_price")
        direction = r.get("direction") or ""
        if target is not None and direction:
            line = f"alert: **{direction}** {fmt_usd(float(target))}"
        else:
            line = "passive"
        if r.get("triggered_at"):
            line += " · ✅ triggered"
        embed.field(f"{sym} ({ac})", line, True)
    embed.footer(f"$watch · {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")
    await ctx.reply(embed=embed.build(), mention_author=False)


async def _add_entry(ctx: DiscoContext, tokens: list[str]) -> None:
    if not tokens:
        await ctx.reply_error_hint(
            "Tell me what to watch.",
            hint=(
                "`$watch add SYMBOL`                  -- passive entry\n"
                "`$watch add SYMBOL PRICE above`      -- alert when ≥ price\n"
                "`$watch add SYMBOL PRICE below`      -- alert when ≤ price"
            ),
            command_name="$watch",
        )
        return

    cap = int(getattr(Config, "MARKET_WATCH_MAX_PER_USER", 20))
    try:
        existing = await ctx.db.fetch_val(
            "SELECT COUNT(*) FROM market_watchlist "
            "WHERE user_id = $1 AND guild_id = $2",
            ctx.author.id, ctx.guild_id,
        )
    except Exception:
        existing = 0
    if existing and int(existing) >= cap:
        await ctx.reply_error(
            f"You've hit the watchlist limit ({cap}). "
            "Remove one with `$watch remove SYMBOL` first.",
        )
        return

    symbol_raw = tokens[0]
    price: float | None = None
    direction: str | None = None

    if len(tokens) >= 2:
        try:
            price = float(tokens[1].replace(",", "").replace("$", ""))
        except ValueError:
            price = None
    if len(tokens) >= 3:
        d = tokens[2].lower()
        if d in ("above", "over", "up", "≥", ">="):
            direction = "above"
        elif d in ("below", "under", "down", "≤", "<="):
            direction = "below"

    if price is not None and direction is None:
        await ctx.reply_error_hint(
            "You set a price but didn't tell me which side to trigger on.",
            hint="Add `above` or `below` after the price.",
            command_name="$watch",
        )
        return

    router = get_router(ctx.bot)
    try:
        resolved = await router.resolve(symbol_raw)
    except Exception:
        resolved = None
    if resolved is None:
        await ctx.reply_error(f"Couldn't resolve `{symbol_raw}`.")
        return

    try:
        await ctx.db.execute(
            "INSERT INTO market_watchlist "
            "  (user_id, guild_id, symbol, asset_class, target_price, "
            "   direction, notify_channel) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (user_id, guild_id, symbol, target_price, direction) "
            "DO NOTHING",
            ctx.author.id, ctx.guild_id,
            resolved.symbol, resolved.asset_class.value,
            price, direction,
            ctx.channel.id if ctx.channel else None,
        )
    except Exception:
        log.exception("$watch add failed")
        await ctx.reply_error("Watchlist storage isn't responding right now.")
        return

    if price is not None and direction:
        await ctx.reply_success(
            f"Watching **{resolved.symbol}** -- alert when "
            f"{direction} {fmt_usd(price)}.",
            title="✅ $watch",
        )
    else:
        await ctx.reply_success(
            f"Watching **{resolved.symbol}** (passive entry, no alert).",
            title="✅ $watch",
        )


async def _remove_entry(ctx: DiscoContext, tokens: list[str]) -> None:
    if not tokens:
        await ctx.reply_error_hint(
            "Tell me which symbol to remove.",
            hint="`$watch remove MTA`",
            command_name="$watch",
        )
        return
    sym = tokens[0].upper()
    try:
        result = await ctx.db.execute(
            "DELETE FROM market_watchlist "
            "WHERE user_id = $1 AND guild_id = $2 AND symbol = $3",
            ctx.author.id, ctx.guild_id, sym,
        )
    except Exception:
        log.exception("$watch remove failed")
        await ctx.reply_error("Watchlist storage isn't responding right now.")
        return
    affected = _affected(result)
    if affected:
        await ctx.reply_success(f"Removed **{sym}** from your watchlist.", title="✅ $watch")
    else:
        await ctx.reply(
            embed=card(
                "👁️ $watch",
                description=f"No entries for `{sym}` to remove.",
                color=C_WARNING,
            ).build(),
            mention_author=False,
        )


async def _clear_entries(ctx: DiscoContext) -> None:
    ok = await ctx.confirm(
        "Clear your entire `$watch` list? This can't be undone.",
        timeout=20.0,
    )
    if not ok:
        return
    try:
        await ctx.db.execute(
            "DELETE FROM market_watchlist "
            "WHERE user_id = $1 AND guild_id = $2",
            ctx.author.id, ctx.guild_id,
        )
    except Exception:
        log.exception("$watch clear failed")
        await ctx.reply_error("Watchlist storage isn't responding right now.")
        return
    await ctx.reply_success("Cleared your watchlist.", title="✅ $watch")


def _affected(result: Any) -> int:
    """asyncpg returns the command tag string; parse the trailing int."""
    if isinstance(result, str):
        parts = result.rsplit(" ", 1)
        try:
            return int(parts[-1])
        except (ValueError, IndexError):
            return 0
    if isinstance(result, int):
        return result
    return 0
