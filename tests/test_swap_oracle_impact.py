"""Tests for the V3 Pillar 8 swap-impact oracle fix.

Pre-V3, ``services.trade.buy`` and ``services.trade.sell`` rebalanced
the user position but never touched ``crypto_prices`` or ``candles``,
so the chart stayed flat even on large market orders. V3 funnels both
buys and sells through ``services.swap.apply_trade_oracle_impact`` which
shares the candle/oracle write path with the AMM swap nudge.

These tests use a tiny fake ``db`` so we don't need a Postgres harness
in CI -- they pin the curve, the direction symmetry, the cap, and the
"don't nudge stablecoins" rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.config import Config
from services.swap import (
    _SWAP_ORACLE_NUDGE_CAP,
    apply_trade_oracle_impact,
    trade_oracle_impact_for_usd,
)


@dataclass
class _FakeDB:
    prices: dict[str, float] = field(default_factory=dict)
    updates: list[tuple[str, float]] = field(default_factory=list)
    candles: list[dict] = field(default_factory=list)

    async def get_price(self, sym: str, gid: int) -> dict | None:
        if sym not in self.prices:
            return None
        return {"price": self.prices[sym]}

    async def update_price(self, sym: str, gid: int, new_price: float) -> None:
        self.prices[sym] = new_price
        self.updates.append((sym, new_price))

    async def upsert_candle(
        self, gid: int, pair: str, minute: int, *,
        open_: float, high: float, low: float, close: float, volume_delta: float,
    ) -> None:
        self.candles.append({
            "pair": pair, "open": open_, "high": high, "low": low,
            "close": close, "volume": volume_delta,
        })


def test_impact_for_usd_is_clamped_at_cap() -> None:
    # Way past the cap: a $1B trade still only moves the oracle by the
    # cap fraction -- the drift loop + follow-on trades take it the rest
    # of the way.
    huge = trade_oracle_impact_for_usd(1_000_000_000.0)
    assert huge == _SWAP_ORACLE_NUDGE_CAP


def test_impact_for_usd_curve_matches_trade_slippage() -> None:
    # Sub-cap value follows the same divisor curve trade.py uses for
    # user-facing slippage, so chart impact and fill impact stay in sync.
    val = 100.0
    expected = val / Config.PRICE_IMPACT_DIVISOR
    got = trade_oracle_impact_for_usd(val)
    if expected >= _SWAP_ORACLE_NUDGE_CAP:
        assert got == _SWAP_ORACLE_NUDGE_CAP
    else:
        assert got == pytest.approx(expected, rel=1e-6)


def test_impact_for_usd_rejects_nonpositive() -> None:
    assert trade_oracle_impact_for_usd(0.0) == 0.0
    assert trade_oracle_impact_for_usd(-50.0) == 0.0


@pytest.mark.asyncio
async def test_buy_pushes_oracle_up_and_writes_candle() -> None:
    db = _FakeDB(prices={"MTA": 100.0})
    await apply_trade_oracle_impact(
        db, guild_id=1, sym="MTA", usd_value=1000.0, direction=+1,
    )
    assert db.updates, "oracle should have been updated on a buy"
    sym, new_price = db.updates[-1]
    assert sym == "MTA"
    assert new_price > 100.0, "buy should push price up"
    assert db.candles, "candle row should have been written"
    candle = db.candles[-1]
    assert candle["pair"] == "MTAUSD"
    assert candle["close"] > candle["open"]
    assert candle["volume"] == 1000.0


@pytest.mark.asyncio
async def test_sell_pushes_oracle_down_and_writes_candle() -> None:
    db = _FakeDB(prices={"MTA": 100.0})
    await apply_trade_oracle_impact(
        db, guild_id=1, sym="MTA", usd_value=1000.0, direction=-1,
    )
    sym, new_price = db.updates[-1]
    assert sym == "MTA"
    assert new_price < 100.0, "sell should push price down"
    candle = db.candles[-1]
    assert candle["close"] < candle["open"]


@pytest.mark.asyncio
async def test_buy_then_sell_same_size_is_symmetric() -> None:
    # A buy followed by a same-size sell should move the oracle in
    # opposite directions by the same magnitude. They don't perfectly
    # cancel because the multiplicative nature means a +x% then -x% nets
    # to about -x^2%, but the magnitudes per leg are equal.
    db = _FakeDB(prices={"MTA": 100.0})
    await apply_trade_oracle_impact(
        db, guild_id=1, sym="MTA", usd_value=500.0, direction=+1,
    )
    after_buy = db.prices["MTA"]
    await apply_trade_oracle_impact(
        db, guild_id=1, sym="MTA", usd_value=500.0, direction=-1,
    )
    after_sell = db.prices["MTA"]
    # The buy moved it up some fraction f; the sell takes it back down
    # by the same fraction -- net is (1+f)(1-f) = 1 - f^2 which is
    # slightly below 100. So we should land just under start.
    assert after_buy > 100.0
    assert after_sell < after_buy
    assert abs(after_sell - 100.0) / 100.0 < 0.001


@pytest.mark.asyncio
async def test_stablecoin_is_skipped() -> None:
    db = _FakeDB(prices={"USD": 1.0, "USDC": 1.0, "DSD": 1.0})
    for sym in ("USD", "USDC", "DSD"):
        await apply_trade_oracle_impact(
            db, guild_id=1, sym=sym, usd_value=1000.0, direction=+1,
        )
    assert not db.updates, "stablecoins must not be nudged"
    assert not db.candles


@pytest.mark.asyncio
async def test_missing_oracle_row_is_a_noop() -> None:
    db = _FakeDB(prices={})  # no row for MTA
    await apply_trade_oracle_impact(
        db, guild_id=1, sym="MTA", usd_value=1000.0, direction=+1,
    )
    assert not db.updates
    assert not db.candles


@pytest.mark.asyncio
async def test_zero_usd_value_is_a_noop() -> None:
    db = _FakeDB(prices={"MTA": 100.0})
    await apply_trade_oracle_impact(
        db, guild_id=1, sym="MTA", usd_value=0.0, direction=+1,
    )
    assert not db.updates
