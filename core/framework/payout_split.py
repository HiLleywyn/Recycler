"""V3 payout balancer: every game reward is split 10/90 by USD value
between the network coin and the network yield token.

Background: prior to V3 each minigame rolled its coin and token amounts
independently. Because the two sides ran on different oracle prices,
a single event could mint $6,309 of LURE and $0.07 of REEL -- the
human-amount split was nowhere near the USD-value split, and once a
coin's price drifted the ratio drifted with it.

This module provides one helper that callers invoke right before the
``update_wallet_holding`` writes. It reads the current oracle for both
sides, computes the total USD value of what the caller was about to
mint, and re-allocates it as 10% USD into the coin + 90% USD into the
yield token. Callers keep their existing reward-generation logic; this
is purely a normalization pass at the mint boundary.

Public surface:
    await rebalance_to_split(db, gid, coin_sym, token_sym,
                             coin_human, token_human) -> (coin, token)

    await split_from_usd(db, gid, coin_sym, token_sym, total_usd)
        -> (coin_human, token_human)

Both functions return floats in human units (not raw). The caller is
responsible for ``to_raw`` + ``update_wallet_holding``.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# The protocol-wide split. Coin = currency, Token = yield.
DEFAULT_COIN_PCT: float = 0.10
DEFAULT_TOKEN_PCT: float = 0.90


async def _oracle_price(db: Any, sym: str, gid: int) -> float:
    """Best-effort oracle read. Returns 0.0 on miss (caller handles)."""
    try:
        row = await db.get_price(sym, gid)
        if row is None:
            return 0.0
        return float(row.get("price") or 0.0)
    except Exception:
        log.debug("payout_split: oracle read failed sym=%s gid=%s",
                  sym, gid, exc_info=True)
        return 0.0


async def rebalance_to_split(
    db: Any,
    gid: int,
    coin_sym: str,
    token_sym: str,
    coin_human: float,
    token_human: float,
    *,
    coin_pct: float = DEFAULT_COIN_PCT,
    token_pct: float = DEFAULT_TOKEN_PCT,
) -> tuple[float, float]:
    """Take pre-V3 ``(coin, token)`` human amounts and re-allocate them
    so the resulting USD value lands at ``coin_pct / token_pct`` of the
    same total. If either oracle is unavailable, the input is returned
    unchanged so a broken oracle never silently zeros a payout.
    """
    coin_oracle = await _oracle_price(db, coin_sym, gid)
    token_oracle = await _oracle_price(db, token_sym, gid)
    if coin_oracle <= 0 or token_oracle <= 0:
        return float(coin_human), float(token_human)
    total_usd = (float(coin_human) * coin_oracle) + (float(token_human) * token_oracle)
    if total_usd <= 0:
        return 0.0, 0.0
    coin_new = (total_usd * coin_pct) / coin_oracle
    token_new = (total_usd * token_pct) / token_oracle
    return coin_new, token_new


async def split_from_usd(
    db: Any,
    gid: int,
    coin_sym: str,
    token_sym: str,
    total_usd: float,
    *,
    coin_pct: float = DEFAULT_COIN_PCT,
    token_pct: float = DEFAULT_TOKEN_PCT,
) -> tuple[float, float]:
    """Fresh payout: convert a USD payout into ``(coin_human, token_human)``
    at the 10/90 split using the current oracle prices.
    """
    coin_oracle = await _oracle_price(db, coin_sym, gid)
    token_oracle = await _oracle_price(db, token_sym, gid)
    if total_usd <= 0:
        return 0.0, 0.0
    coin_new = (
        (total_usd * coin_pct) / coin_oracle if coin_oracle > 0 else 0.0
    )
    token_new = (
        (total_usd * token_pct) / token_oracle if token_oracle > 0 else 0.0
    )
    return coin_new, token_new
