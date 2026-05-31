"""Tests for services/trade.py  -  buy and sell service layer using a mock DB."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from core.framework.scale import to_raw, to_human
from services.trade import execute_buy, execute_sell, _trade_cd
from services.swap import check_user_swap_volume, record_user_swap_volume, _user_swap_locks, _user_swap_volume
from tests.conftest import GUILD_ID, USER_ID, MockDB


@pytest.fixture(autouse=True)
def _reset_swap_volume_state():
    _user_swap_volume.clear()
    _user_swap_locks.clear()
    _trade_cd.clear()
    yield
    _user_swap_volume.clear()
    _user_swap_locks.clear()
    _trade_cd.clear()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_db_for_buy(
    symbol: str = "ARC",
    price: float = 2000.0,
    wallet: float = 100_000.0,
    fee_pct: float = 0.005,
) -> MockDB:
    db = MockDB()
    db.tokens[GUILD_ID] = {symbol: {"network": "Arcadia Network"}}
    db.prices[(symbol, GUILD_ID)] = {"price": price}
    db.prices[("SUN", GUILD_ID)] = {"price": 1.0}
    db.users[(USER_ID, GUILD_ID)] = {"wallet": to_raw(wallet), "bank": 0}
    db.guilds.fee_config = {
        "platform_fee_pct": fee_pct,
        "platform_fee_min": 0.01,
        "platform_fee_max": 50.0,
    }
    return db


def _make_db_for_sell(
    symbol: str = "ARC",
    price: float = 2000.0,
    token_balance: float = 5.0,
    wallet: float = 0.0,
) -> MockDB:
    db = _make_db_for_buy(symbol=symbol, price=price, wallet=wallet)
    db.holdings[(USER_ID, GUILD_ID, symbol)] = {"amount": to_raw(token_balance), "symbol": symbol}
    return db


# ── execute_buy ────────────────────────────────────────────────────────────────

class TestExecuteBuy:
    @pytest.mark.asyncio
    async def test_successful_buy(self):
        from core.config import Config
        db = _make_db_for_buy()
        result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert result.success
        # amount is slippage-adjusted: eff_amount = cost_usd / (price * (1 + impact))
        cost_usd = 2000.0 * 1.0
        impact = cost_usd / Config.PRICE_IMPACT_DIVISOR
        expected_amount = cost_usd / (2000.0 * (1 + impact))
        assert result.amount == pytest.approx(expected_amount, rel=1e-5)
        assert result.cost > 0

    @pytest.mark.asyncio
    async def test_buy_deducts_wallet(self):
        db = _make_db_for_buy(price=2000.0, wallet=100_000.0)
        await execute_buy(db, GUILD_ID, USER_ID, "ARC", 1.0)
        user = await db.get_user(USER_ID, GUILD_ID)
        # Cost = 2000 + fee; wallet should be reduced
        assert to_human(user["wallet"]) < 100_000.0

    @pytest.mark.asyncio
    async def test_buy_credits_holding(self):
        from core.config import Config
        db = _make_db_for_buy()
        await execute_buy(db, GUILD_ID, USER_ID, "ARC", 2.0)
        holding = await db.get_holding(USER_ID, GUILD_ID, "ARC")
        cost_usd = 2000.0 * 2.0
        impact = cost_usd / Config.PRICE_IMPACT_DIVISOR
        expected = cost_usd / (2000.0 * (1 + impact))
        assert to_human(holding["amount"]) == pytest.approx(expected, rel=1e-5)

    @pytest.mark.asyncio
    async def test_zero_amount_fails(self):
        db = _make_db_for_buy()
        result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", 0.0)
        assert not result.success
        assert "positive" in result.error.lower()

    @pytest.mark.asyncio
    async def test_negative_amount_fails(self):
        db = _make_db_for_buy()
        result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", -5.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_unknown_token_fails(self):
        db = _make_db_for_buy()
        result = await execute_buy(db, GUILD_ID, USER_ID, "ZZZZ", 1.0)
        assert not result.success
        assert "ZZZZ" in result.error

    @pytest.mark.asyncio
    async def test_usd_not_buyable(self):
        db = _make_db_for_buy()
        result = await execute_buy(db, GUILD_ID, USER_ID, "USD", 1.0)
        assert not result.success
        assert "base currency" in result.error.lower()

    @pytest.mark.asyncio
    async def test_insufficient_balance(self):
        db = _make_db_for_buy(price=2000.0, wallet=1.0)  # Only $1
        result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success
        assert "insufficient" in result.error.lower()

    @pytest.mark.asyncio
    async def test_disabled_token_fails(self):
        db = _make_db_for_buy()
        db.disabled_tokens.add((GUILD_ID, "ARC"))
        result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success
        assert "disabled" in result.error.lower()

    @pytest.mark.asyncio
    async def test_halted_network_fails(self):
        db = _make_db_for_buy()
        db.halted_networks.add((GUILD_ID, "arc"))
        result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success
        assert "halted" in result.error.lower()

    @pytest.mark.asyncio
    async def test_price_impact_too_large(self):
        """Buying an astronomically large amount should hit the 50% impact cap."""
        db = _make_db_for_buy(price=1.0, wallet=10_000_000_000.0)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "services.trade.reserve_user_swap_volume",
                AsyncMock(return_value=(True, 9_999_999.0, 123.0)),
            )
            result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", 3_000_000.0)
        assert not result.success
        assert "impact" in result.error.lower()

    @pytest.mark.asyncio
    async def test_case_insensitive_symbol(self):
        db = _make_db_for_buy()
        result = await execute_buy(db, GUILD_ID, USER_ID, "arc", 1.0)
        assert result.success

    @pytest.mark.asyncio
    async def test_fee_deducted_from_wallet(self):
        db = _make_db_for_buy(price=1000.0, wallet=50_000.0, fee_pct=0.01)
        result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert result.success
        assert result.fee > 0

    @pytest.mark.asyncio
    async def test_non_buyable_token_fails(self):
        """Tokens not in BUYABLE_WITH_USD should be rejected with a clear message."""
        db = MockDB()
        db.tokens[GUILD_ID] = {"LINK": {"network": "Arcadia Network"}}
        db.prices[("LINK", GUILD_ID)] = {"price": 15.0}
        db.users[(USER_ID, GUILD_ID)] = {"wallet": 100_000.0, "bank": 0.0}
        result = await execute_buy(db, GUILD_ID, USER_ID, "LINK", 1.0)
        assert not result.success
        assert "cannot be purchased" in result.error.lower() or "direct" in result.error.lower()

    @pytest.mark.asyncio
    async def test_buy_rejects_when_hourly_volume_is_exhausted(self):
        from core.config import Config

        db = _make_db_for_buy()
        record_user_swap_volume(USER_ID, GUILD_ID, Config.USER_SWAP_HOURLY_LIMIT_USD)

        result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success
        assert "hourly volume limit" in result.error.lower()

    @pytest.mark.asyncio
    async def test_failed_buy_releases_reserved_volume(self):
        db = _make_db_for_buy()

        async def _boom(*args, **kwargs):
            raise RuntimeError("db boom")

        db.update_wallet = _boom
        result = await execute_buy(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success

        allowed, _ = check_user_swap_volume(USER_ID, GUILD_ID, 2000.0)
        assert allowed is True


# ── execute_sell ──────────────────────────────────────────────────────────────

class TestExecuteSell:
    @pytest.mark.asyncio
    async def test_successful_sell(self):
        db = _make_db_for_sell(token_balance=5.0)
        result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert result.success
        assert result.amount == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_sell_credits_wallet(self):
        db = _make_db_for_sell(price=2000.0, token_balance=5.0, wallet=0.0)
        await execute_sell(db, GUILD_ID, USER_ID, "ARC", 1.0)
        user = await db.get_user(USER_ID, GUILD_ID)
        assert to_human(user["wallet"]) > 0

    @pytest.mark.asyncio
    async def test_sell_deducts_holding(self):
        db = _make_db_for_sell(token_balance=5.0)
        await execute_sell(db, GUILD_ID, USER_ID, "ARC", 2.0)
        holding = await db.get_holding(USER_ID, GUILD_ID, "ARC")
        assert to_human(holding["amount"]) == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_zero_amount_fails(self):
        db = _make_db_for_sell(token_balance=5.0)
        result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 0.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_no_holding_fails(self):
        db = _make_db_for_sell(token_balance=0.0)
        result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success
        assert "no arc" in result.error.lower() or "have no" in result.error.lower()

    @pytest.mark.asyncio
    async def test_oversell_fails(self):
        db = _make_db_for_sell(token_balance=1.0)
        result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 5.0)
        assert not result.success
        assert "insufficient" in result.error.lower()

    @pytest.mark.asyncio
    async def test_unknown_token_fails(self):
        db = _make_db_for_sell()
        result = await execute_sell(db, GUILD_ID, USER_ID, "ZZZZ", 1.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_disabled_token_fails(self):
        db = _make_db_for_sell(token_balance=5.0)
        db.disabled_tokens.add((GUILD_ID, "ARC"))
        result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success
        assert "disabled" in result.error.lower()

    @pytest.mark.asyncio
    async def test_halted_network_fails(self):
        db = _make_db_for_sell(token_balance=5.0)
        db.halted_networks.add((GUILD_ID, "arc"))
        result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_price_impact_too_large(self):
        """Selling an amount that causes > 50% price impact must be rejected."""
        db = _make_db_for_sell(price=1.0, token_balance=10_000_000.0, wallet=0.0)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "services.trade.reserve_user_swap_volume",
                AsyncMock(return_value=(True, 9_999_999.0, 123.0)),
            )
            result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 3_000_000.0)
        assert not result.success
        assert "impact" in result.error.lower()

    @pytest.mark.asyncio
    async def test_sell_moves_oracle_down(self):
        """V3 Pillar 8: sells now move the chart.

        Pre-V3 ``execute_sell`` rebalanced the user's position but never
        touched the oracle, so the chart stayed flat even on large dumps.
        V3 routes both legs of every market order through
        ``services.swap.apply_trade_oracle_impact`` with a symmetric
        ``_SWAP_ORACLE_NUDGE_CAP``-clamped move. Slippage is still applied
        to the user's fill price; the chart impact is layered on top.
        """
        from services.swap import _SWAP_ORACLE_NUDGE_CAP
        db = _make_db_for_sell(price=2000.0, token_balance=5.0)
        result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert result.success
        price_row = await db.get_price("ARC", GUILD_ID)
        new_price = float(price_row["price"])
        # Oracle moved DOWN (sell pressure), bounded by the nudge cap.
        assert new_price < 2000.0
        assert new_price >= 2000.0 * (1.0 - _SWAP_ORACLE_NUDGE_CAP)
        # User received less than spot revenue (slippage deducted from fill price)
        assert result.cost < 2000.0

    @pytest.mark.asyncio
    async def test_sell_net_revenue_positive(self):
        db = _make_db_for_sell(price=2000.0, token_balance=5.0, wallet=0.0)
        result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert result.success
        # cost returned as 'cost' field in sell = revenue after fee
        user = await db.get_user(USER_ID, GUILD_ID)
        assert to_human(user["wallet"]) > 0

    @pytest.mark.asyncio
    async def test_failed_sell_releases_reserved_volume(self):
        db = _make_db_for_sell(token_balance=5.0)

        async def _boom(*args, **kwargs):
            raise RuntimeError("db boom")

        db.update_holding = _boom
        result = await execute_sell(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success

        allowed, _ = check_user_swap_volume(USER_ID, GUILD_ID, 2000.0)
        assert allowed is True
