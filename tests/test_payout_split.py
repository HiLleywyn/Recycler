"""V3 payout split helper tests."""
from __future__ import annotations

import pytest

from core.framework.payout_split import (
    DEFAULT_COIN_PCT,
    DEFAULT_TOKEN_PCT,
    rebalance_to_split,
    split_from_usd,
)


class _MockDB:
    def __init__(self, prices: dict[tuple[str, int], float]) -> None:
        self._prices = {k: {"price": v} for k, v in prices.items()}

    async def get_price(self, sym: str, gid: int):
        return self._prices.get((sym.upper(), gid))


@pytest.mark.asyncio
async def test_rebalance_lopsided_input_to_target_ratio() -> None:
    """The bug the user reported in production: $6309 LURE + $0.07 REEL."""
    # LURE @ $0.50, REEL @ $50 -> human amounts ~12618 LURE, ~0.0014 REEL.
    db = _MockDB({("LURE", 1): 0.5, ("REEL", 1): 50.0})
    coin, token = await rebalance_to_split(
        db, 1, "LURE", "REEL", 12618.0, 0.0014,
    )
    total_usd_new = coin * 0.5 + token * 50.0
    coin_usd = coin * 0.5
    token_usd = token * 50.0
    # The total USD value is preserved.
    assert total_usd_new == pytest.approx(12618.0 * 0.5 + 0.0014 * 50.0)
    # 10/90 split is now exact (within float).
    assert coin_usd == pytest.approx(total_usd_new * 0.10)
    assert token_usd == pytest.approx(total_usd_new * 0.90)


@pytest.mark.asyncio
async def test_split_from_usd() -> None:
    """Fresh payout: $1000 -> $100 LURE, $900 REEL at oracle prices."""
    db = _MockDB({("LURE", 1): 0.5, ("REEL", 1): 50.0})
    coin, token = await split_from_usd(db, 1, "LURE", "REEL", 1000.0)
    assert coin * 0.5 == pytest.approx(100.0)
    assert token * 50.0 == pytest.approx(900.0)


@pytest.mark.asyncio
async def test_rebalance_returns_input_when_oracle_missing() -> None:
    """A broken oracle should never silently zero a payout."""
    db = _MockDB({})
    coin, token = await rebalance_to_split(
        db, 1, "LURE", "REEL", 100.0, 5.0,
    )
    assert coin == 100.0
    assert token == 5.0


@pytest.mark.asyncio
async def test_rebalance_zero_input() -> None:
    db = _MockDB({("LURE", 1): 1.0, ("REEL", 1): 1.0})
    coin, token = await rebalance_to_split(db, 1, "LURE", "REEL", 0.0, 0.0)
    assert coin == 0.0
    assert token == 0.0


@pytest.mark.asyncio
async def test_rebalance_one_sided_input() -> None:
    """When only the coin is minted, the rebalance pushes 90% into
    the token side, which matches the user's "always 10/90" rule."""
    db = _MockDB({("INGOT", 1): 2.0, ("FORGE", 1): 4.0})
    coin, token = await rebalance_to_split(
        db, 1, "INGOT", "FORGE", 500.0, 0.0,
    )
    total_in_usd = 500.0 * 2.0
    assert coin * 2.0 == pytest.approx(total_in_usd * 0.10)
    assert token * 4.0 == pytest.approx(total_in_usd * 0.90)


def test_default_ratios_sum_to_one() -> None:
    assert DEFAULT_COIN_PCT + DEFAULT_TOKEN_PCT == pytest.approx(1.0)
    assert DEFAULT_COIN_PCT == 0.10
    assert DEFAULT_TOKEN_PCT == 0.90
