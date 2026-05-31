"""Tests for services/liquidity.py  -  add_liquidity and remove_liquidity."""
from __future__ import annotations

import math
from unittest.mock import AsyncMock

import pytest

from core.framework.scale import to_raw
from services.liquidity import add_liquidity, remove_liquidity
from tests.conftest import GUILD_ID, USER_ID, MockDB

POOL_ID = "DSC_USD"
TOKEN_A = "DSC"
TOKEN_B = "USD"


# ── Helpers ────────────────────────────────────────────────────────────────────
# Pool reserves, LP shares, wallet balances, and holdings are all stored as raw
# NUMERIC(36,0) scaled by 10**18 in production; mirror that in the mock.

def _make_pool(
    db: MockDB,
    reserve_a: float = 1000.0,
    reserve_b: float = 2000.0,
    total_lp: float = 100.0,
) -> None:
    """Register a DSC/USD pool in the MockDB."""
    db.pools[(POOL_ID, GUILD_ID)] = {
        "pool_id": POOL_ID,
        "token_a": "DSC",
        "token_b": "USD",
        "reserve_a": to_raw(reserve_a),
        "reserve_b": to_raw(reserve_b),
        "total_lp": to_raw(total_lp),
    }


def _set_user_lp(db: MockDB, lp_shares: float) -> None:
    """Monkeypatch db.get_user_lp to return a position with given shares."""
    db.get_user_lp = AsyncMock(return_value={"lp_shares": to_raw(lp_shares)})


def _set_balances(db: MockDB, dsc: float = 0.0, usd: float = 0.0) -> None:
    """Set user DSC holding and USD wallet balance."""
    if dsc > 0:
        db.holdings[(USER_ID, GUILD_ID, "DSC")] = {"amount": to_raw(dsc), "symbol": "DSC"}
    db.users[(USER_ID, GUILD_ID)] = {"wallet": to_raw(usd), "bank": 0}


# ── add_liquidity ──────────────────────────────────────────────────────────────

class TestAddLiquidity:
    @pytest.mark.asyncio
    async def test_same_token_rejected(self, mock_db):
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, "DSC", "DSC", 10.0)
        assert not result.success
        assert "same token" in result.error.lower()

    @pytest.mark.asyncio
    async def test_zero_amount_rejected(self, mock_db):
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 0.0)
        assert not result.success
        assert "positive" in result.error.lower()

    @pytest.mark.asyncio
    async def test_negative_amount_rejected(self, mock_db):
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, -5.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_no_pool_returns_error(self, mock_db):
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 10.0)
        assert not result.success
        assert "no pool" in result.error.lower()

    @pytest.mark.asyncio
    async def test_empty_pool_requires_both_amounts(self, mock_db):
        _make_pool(mock_db, reserve_a=0.0, reserve_b=0.0, total_lp=0.0)
        _set_balances(mock_db, dsc=100.0, usd=0.0)
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 10.0, amount_b=0.0)
        assert not result.success
        assert "both amounts" in result.error.lower()

    @pytest.mark.asyncio
    async def test_initial_lp_geometric_mean(self, mock_db):
        """Empty pool: LP minted = sqrt(amount_a * amount_b)."""
        _make_pool(mock_db, reserve_a=0.0, reserve_b=0.0, total_lp=0.0)
        _set_balances(mock_db, dsc=100.0, usd=400.0)
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 100.0, amount_b=400.0)
        assert result.success
        expected_lp = math.sqrt(100.0 * 400.0)
        assert result.lp_tokens == pytest.approx(expected_lp)

    @pytest.mark.asyncio
    async def test_existing_pool_ratio_enforced(self, mock_db):
        """For an existing pool with ratio 1:2, providing 50 DSC should require 100 USD."""
        _make_pool(mock_db, reserve_a=1000.0, reserve_b=2000.0, total_lp=100.0)
        _set_balances(mock_db, dsc=50.0, usd=200.0)
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 50.0, amount_b=999.0)
        assert result.success
        # amount_b is overridden to 50 * (2000/1000) = 100
        assert result.amount_b == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_insufficient_token_a_fails(self, mock_db):
        _make_pool(mock_db)
        _set_balances(mock_db, dsc=5.0, usd=500.0)
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 10.0)
        assert not result.success
        assert "insufficient" in result.error.lower()
        assert "DSC" in result.error

    @pytest.mark.asyncio
    async def test_insufficient_token_b_fails(self, mock_db):
        _make_pool(mock_db, reserve_a=1000.0, reserve_b=2000.0, total_lp=100.0)
        # Enough DSC, but ratio requires 100 USD and user only has 50
        _set_balances(mock_db, dsc=50.0, usd=50.0)
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 50.0)
        assert not result.success
        assert "insufficient" in result.error.lower()
        assert "USD" in result.error

    @pytest.mark.asyncio
    async def test_successful_add_debits_user(self, mock_db):
        _make_pool(mock_db, reserve_a=1000.0, reserve_b=2000.0, total_lp=100.0)
        _set_balances(mock_db, dsc=50.0, usd=200.0)
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 50.0)
        assert result.success
        dsc_holding = await mock_db.get_holding(USER_ID, GUILD_ID, "DSC")
        usd_user = await mock_db.get_user(USER_ID, GUILD_ID)
        assert dsc_holding["amount"] == 0
        assert usd_user["wallet"] == to_raw(200.0 - 100.0)

    @pytest.mark.asyncio
    async def test_pool_reserves_updated(self, mock_db):
        _make_pool(mock_db, reserve_a=1000.0, reserve_b=2000.0, total_lp=100.0)
        _set_balances(mock_db, dsc=50.0, usd=200.0)
        await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 50.0)
        pool = await mock_db.get_pool(POOL_ID, GUILD_ID)
        assert pool["reserve_a"] == to_raw(1050.0)
        assert pool["reserve_b"] == to_raw(2100.0)

    @pytest.mark.asyncio
    async def test_lp_proportional_to_existing(self, mock_db):
        """Adding 10% of reserve_a to an existing pool mints 10% of total_lp."""
        _make_pool(mock_db, reserve_a=1000.0, reserve_b=2000.0, total_lp=100.0)
        _set_balances(mock_db, dsc=100.0, usd=500.0)
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 100.0)
        assert result.success
        # 100 / 1000 = 10% of pool → 10% of 100 LP = 10 LP
        assert result.lp_tokens == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_tx_hash_returned(self, mock_db):
        _make_pool(mock_db, reserve_a=0.0, reserve_b=0.0, total_lp=0.0)
        _set_balances(mock_db, dsc=100.0, usd=200.0)
        result = await add_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 100.0, amount_b=200.0)
        assert result.success
        assert result.tx_hash


# ── remove_liquidity ───────────────────────────────────────────────────────────

class TestRemoveLiquidity:
    @pytest.mark.asyncio
    async def test_invalid_share_pct_zero(self, mock_db):
        result = await remove_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 0.0)
        assert not result.success
        assert "percentage" in result.error.lower() or "0" in result.error

    @pytest.mark.asyncio
    async def test_invalid_share_pct_over_100(self, mock_db):
        result = await remove_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 101.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_no_pool_returns_error(self, mock_db):
        result = await remove_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 50.0)
        assert not result.success
        assert "no pool" in result.error.lower()

    @pytest.mark.asyncio
    async def test_empty_pool_returns_error(self, mock_db):
        _make_pool(mock_db, reserve_a=0.0, reserve_b=0.0, total_lp=0.0)
        _set_user_lp(mock_db, lp_shares=10.0)
        result = await remove_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 50.0)
        assert not result.success
        assert "no liquidity" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_lp_position_fails(self, mock_db):
        _make_pool(mock_db)
        # get_user_lp returns None by default → user_lp = 0
        result = await remove_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 50.0)
        assert not result.success
        assert "no lp position" in result.error.lower()

    @pytest.mark.asyncio
    async def test_successful_remove_credits_user(self, mock_db):
        """Removing 50% of 10 LP from a 1000/2000 pool with 100 total LP
        returns 5% of reserves: 50 DSC + 100 USD."""
        _make_pool(mock_db, reserve_a=1000.0, reserve_b=2000.0, total_lp=100.0)
        _set_user_lp(mock_db, lp_shares=10.0)
        _set_balances(mock_db, dsc=0.0, usd=0.0)
        result = await remove_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 50.0)
        assert result.success
        # user holds 10 LP, removing 50% = 5 LP out of 100 total = 5% of reserves
        dsc_holding = await mock_db.get_holding(USER_ID, GUILD_ID, "DSC")
        usd_user = await mock_db.get_user(USER_ID, GUILD_ID)
        assert dsc_holding["amount"] == to_raw(50.0)   # 5% of 1000
        assert usd_user["wallet"] == to_raw(100.0)     # 5% of 2000

    @pytest.mark.asyncio
    async def test_pool_reserves_reduced(self, mock_db):
        _make_pool(mock_db, reserve_a=1000.0, reserve_b=2000.0, total_lp=100.0)
        _set_user_lp(mock_db, lp_shares=10.0)
        _set_balances(mock_db, usd=0.0)
        await remove_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 50.0)
        pool = await mock_db.get_pool(POOL_ID, GUILD_ID)
        assert pool["reserve_a"] == to_raw(950.0)
        assert pool["reserve_b"] == to_raw(1900.0)

    @pytest.mark.asyncio
    async def test_full_withdrawal(self, mock_db):
        """100% share_pct removes the entire user LP position."""
        _make_pool(mock_db, reserve_a=1000.0, reserve_b=2000.0, total_lp=100.0)
        _set_user_lp(mock_db, lp_shares=100.0)
        _set_balances(mock_db, usd=0.0)
        result = await remove_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 100.0)
        assert result.success
        assert result.lp_tokens == pytest.approx(100.0)
        pool = await mock_db.get_pool(POOL_ID, GUILD_ID)
        assert pool["reserve_a"] == 0
        assert pool["reserve_b"] == 0

    @pytest.mark.asyncio
    async def test_tx_hash_returned(self, mock_db):
        _make_pool(mock_db)
        _set_user_lp(mock_db, lp_shares=10.0)
        _set_balances(mock_db, usd=0.0)
        result = await remove_liquidity(mock_db, GUILD_ID, USER_ID, TOKEN_A, TOKEN_B, 25.0)
        assert result.success
        assert result.tx_hash
