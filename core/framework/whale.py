"""Whale alert helper  -  checks transaction USD value against the guild threshold
and publishes a ``whale_alert`` event when exceeded."""
from __future__ import annotations

import discord

from core.config import Config
from core.framework.scale import to_human as _h

# Action labels understood by the whale_alert handler in trades.py
ACTIONS = {"swap", "buy", "sell", "transfer", "stake", "unstake",
           "addlp", "removelp", "gamble", "deposit", "withdraw",
           "loan", "liquidation", "send", "mining"}


async def check(
    bot,
    guild: discord.Guild,
    user_id: int,
    action: str,
    usd_value: float,
    *,
    symbol: str = "",
    symbol_in: str = "",
    symbol_out: str = "",
    network: str = "",
    amount: float = 0.0,
    amount_in: float = 0.0,
    amount_out: float = 0.0,
) -> None:
    """Publish a whale_alert event if *usd_value* meets the guild threshold.

    Call this right after every ``bus.publish`` for value-bearing transactions.
    The function is intentionally fire-and-forget safe (never raises).
    """
    if usd_value <= 0:
        return
    try:
        settings = await bot.db.get_guild_settings(guild.id)
        threshold_raw = settings.get("whale_alert_threshold") or Config.WHALE_ALERT_THRESHOLD_USD
        # Guard for legacy values: the old admin whalethreshold command stored the
        # threshold as a plain integer (e.g. 1000 for $1,000) instead of raw-scaled
        # (1000 * 10^18).  Even $1 in raw format is 10^18, so any stored value
        # below 10^15 is certainly a legacy human-readable amount -- use it directly.
        if threshold_raw < 10 ** 15:
            threshold = float(threshold_raw)
        else:
            threshold = _h(threshold_raw)
        if usd_value < threshold:
            return
        kwargs: dict = {}
        if symbol:
            kwargs["symbol"] = symbol
        if symbol_in:
            kwargs["symbol_in"] = symbol_in
        if symbol_out:
            kwargs["symbol_out"] = symbol_out
        if network:
            kwargs["network"] = network
        if amount:
            kwargs["amount"] = amount
        if amount_in:
            kwargs["amount_in"] = amount_in
        if amount_out:
            kwargs["amount_out"] = amount_out
        await bot.bus.publish(
            "whale_alert",
            guild=guild,
            user_id=user_id,
            action=action,
            usd_value=usd_value,
            **kwargs,
        )
    except Exception:
        pass


async def usd_value_of(bot, symbol: str, amount: float, guild_id: int) -> float:
    """Return the approximate USD value of *amount* of *symbol*. Returns 0.0 on failure."""
    if symbol == "USD":
        return abs(amount)
    try:
        row = await bot.db.get_price(symbol, guild_id)
        if row and row["price"] > 0:
            return abs(amount * float(row["price"]))
    except Exception:
        pass
    return 0.0
