"""Tests for services/savings.py  -  savings deposit and withdrawal service layer."""
from __future__ import annotations

import pytest

from core.framework.scale import to_raw
from services.savings import deposit_savings, withdraw_savings, SUPPORTED_SAVINGS_SYMBOLS
from tests.conftest import GUILD_ID, USER_ID, MockDB


# ── Helpers ────────────────────────────────────────────────────────────────────
# Balance columns are raw NUMERIC(36,0) scaled by 10**18 in production, so the
# MockDB is populated with ``to_raw(human_amount)`` values to match.

def _make_db(
    usd_wallet: float = 1000.0,
    sun_holding: float = 0.0,
    usd_savings: float = 0.0,
) -> MockDB:
    db = MockDB()
    db.users[(USER_ID, GUILD_ID)] = {"wallet": to_raw(usd_wallet), "bank": 0}
    if sun_holding > 0:
        db.holdings[(USER_ID, GUILD_ID, "SUN")] = {"amount": to_raw(sun_holding), "symbol": "SUN"}
    if usd_savings > 0:
        db.savings[(USER_ID, GUILD_ID, "USD")] = {"amount": to_raw(usd_savings), "symbol": "USD"}
    return db


# ── SUPPORTED_SAVINGS_SYMBOLS constant ────────────────────────────────────────

class TestSupportedSavingsSymbols:
    def test_usd_supported(self):
        assert "USD" in SUPPORTED_SAVINGS_SYMBOLS

    def test_sun_supported(self):
        assert "SUN" in SUPPORTED_SAVINGS_SYMBOLS

    def test_eth_not_supported(self):
        assert "ARC" not in SUPPORTED_SAVINGS_SYMBOLS


# ── deposit_savings ───────────────────────────────────────────────────────────

class TestDepositSavings:
    @pytest.mark.asyncio
    async def test_usd_deposit_success(self):
        db = _make_db(usd_wallet=500.0)
        result = await deposit_savings(db, GUILD_ID, USER_ID, "USD", 100.0)
        assert result.success
        assert result.amount == pytest.approx(100.0)
        assert result.symbol == "USD"

    @pytest.mark.asyncio
    async def test_usd_deducted_from_wallet(self):
        db = _make_db(usd_wallet=500.0)
        await deposit_savings(db, GUILD_ID, USER_ID, "USD", 200.0)
        user = await db.get_user(USER_ID, GUILD_ID)
        assert user["wallet"] == to_raw(300.0)

    @pytest.mark.asyncio
    async def test_usd_credited_to_savings(self):
        db = _make_db(usd_wallet=500.0)
        result = await deposit_savings(db, GUILD_ID, USER_ID, "USD", 100.0)
        assert result.new_savings_balance == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_sun_deposit_success(self):
        db = _make_db(sun_holding=50.0)
        result = await deposit_savings(db, GUILD_ID, USER_ID, "SUN", 10.0)
        assert result.success
        assert result.symbol == "SUN"

    @pytest.mark.asyncio
    async def test_sun_deducted_from_holding(self):
        db = _make_db(sun_holding=50.0)
        await deposit_savings(db, GUILD_ID, USER_ID, "SUN", 10.0)
        holding = await db.get_holding(USER_ID, GUILD_ID, "SUN")
        assert holding["amount"] == to_raw(40.0)

    @pytest.mark.asyncio
    async def test_cumulative_deposits(self):
        db = _make_db(usd_wallet=1000.0)
        await deposit_savings(db, GUILD_ID, USER_ID, "USD", 100.0)
        result = await deposit_savings(db, GUILD_ID, USER_ID, "USD", 50.0)
        assert result.new_savings_balance == pytest.approx(150.0)

    @pytest.mark.asyncio
    async def test_unsupported_symbol_fails(self):
        db = _make_db()
        result = await deposit_savings(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success
        assert "supported" in result.error.lower() or "only" in result.error.lower()

    @pytest.mark.asyncio
    async def test_zero_amount_fails(self):
        db = _make_db()
        result = await deposit_savings(db, GUILD_ID, USER_ID, "USD", 0.0)
        assert not result.success
        assert "positive" in result.error.lower()

    @pytest.mark.asyncio
    async def test_negative_amount_fails(self):
        db = _make_db()
        result = await deposit_savings(db, GUILD_ID, USER_ID, "USD", -100.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_insufficient_usd_balance_fails(self):
        db = _make_db(usd_wallet=50.0)
        result = await deposit_savings(db, GUILD_ID, USER_ID, "USD", 100.0)
        assert not result.success
        assert "insufficient" in result.error.lower()

    @pytest.mark.asyncio
    async def test_insufficient_sun_balance_fails(self):
        db = _make_db(sun_holding=1.0)
        result = await deposit_savings(db, GUILD_ID, USER_ID, "SUN", 5.0)
        assert not result.success
        assert "insufficient" in result.error.lower()

    @pytest.mark.asyncio
    async def test_case_insensitive_symbol(self):
        db = _make_db(usd_wallet=200.0)
        result = await deposit_savings(db, GUILD_ID, USER_ID, "usd", 50.0)
        assert result.success

    @pytest.mark.asyncio
    async def test_no_user_account_insufficient(self):
        db = MockDB()  # No user row
        result = await deposit_savings(db, GUILD_ID, USER_ID, "USD", 100.0)
        assert not result.success
        assert "insufficient" in result.error.lower()


# ── withdraw_savings ──────────────────────────────────────────────────────────

class TestWithdrawSavings:
    @pytest.mark.asyncio
    async def test_usd_withdrawal_success(self):
        db = _make_db(usd_wallet=0.0, usd_savings=200.0)
        result = await withdraw_savings(db, GUILD_ID, USER_ID, "USD", 100.0)
        assert result.success
        assert result.amount == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_usd_credited_to_wallet(self):
        db = _make_db(usd_wallet=50.0, usd_savings=200.0)
        await withdraw_savings(db, GUILD_ID, USER_ID, "USD", 100.0)
        user = await db.get_user(USER_ID, GUILD_ID)
        assert user["wallet"] == to_raw(150.0)

    @pytest.mark.asyncio
    async def test_usd_deducted_from_savings(self):
        db = _make_db(usd_savings=200.0)
        result = await withdraw_savings(db, GUILD_ID, USER_ID, "USD", 80.0)
        assert result.new_savings_balance == pytest.approx(120.0)

    @pytest.mark.asyncio
    async def test_zero_amount_fails(self):
        db = _make_db(usd_savings=100.0)
        result = await withdraw_savings(db, GUILD_ID, USER_ID, "USD", 0.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_negative_amount_fails(self):
        db = _make_db(usd_savings=100.0)
        result = await withdraw_savings(db, GUILD_ID, USER_ID, "USD", -50.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_insufficient_savings_fails(self):
        db = _make_db(usd_savings=50.0)
        result = await withdraw_savings(db, GUILD_ID, USER_ID, "USD", 100.0)
        assert not result.success
        assert "insufficient" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_savings_account_fails(self):
        db = _make_db()  # No savings
        result = await withdraw_savings(db, GUILD_ID, USER_ID, "USD", 50.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_unsupported_symbol_fails(self):
        db = _make_db()
        result = await withdraw_savings(db, GUILD_ID, USER_ID, "ARC", 1.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_exact_balance_withdrawal(self):
        db = _make_db(usd_savings=100.0)
        result = await withdraw_savings(db, GUILD_ID, USER_ID, "USD", 100.0)
        assert result.success
        assert result.new_savings_balance == pytest.approx(0.0)
