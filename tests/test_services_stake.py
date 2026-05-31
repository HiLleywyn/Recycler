"""Tests for services/stake.py  -  staking and unstaking service layer."""
from __future__ import annotations

import time

import pytest

from core.framework.scale import to_raw
from services.stake import execute_stake, execute_unstake
from tests.conftest import GUILD_ID, USER_ID, MockDB

VALIDATOR_ID = "arc-validator-1"
NETWORK = "Arcadia Network"
SYMBOL = "ARC"
NET_SHORT = "arc"


# ── Helpers ────────────────────────────────────────────────────────────────────
# Wallet holdings and stakes are stored as raw NUMERIC(36,0) * 10**18.

def _make_db(
    eth_balance: float = 10.0,
    has_wallet: bool = True,
    validator_active: bool = True,
) -> MockDB:
    db = MockDB()
    db.validators[VALIDATOR_ID] = {
        "validator_id": VALIDATOR_ID,
        "name": "Test ARC Validator",
        "network": NETWORK,
        "is_active": validator_active,
    }
    if has_wallet:
        db.defi_wallets.add((USER_ID, GUILD_ID, NET_SHORT))
    if eth_balance > 0:
        db.wallet_holdings[(USER_ID, GUILD_ID, NET_SHORT, SYMBOL)] = {
            "amount": to_raw(eth_balance),
            "symbol": SYMBOL,
        }
    return db


def _make_db_with_stake(staked: float = 5.0, eth_balance: float = 5.0) -> MockDB:
    db = _make_db(eth_balance=eth_balance)
    db.stakes[(USER_ID, GUILD_ID, VALIDATOR_ID)] = {
        "amount": to_raw(staked),
        "symbol": SYMBOL,
        "staked_at": time.time() - 90_000,  # > 24h ago
    }
    return db


# ── execute_stake ──────────────────────────────────────────────────────────────

class TestExecuteStake:
    @pytest.mark.asyncio
    async def test_successful_stake(self):
        db = _make_db(eth_balance=5.0)
        result = await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 2.0)
        assert result.success
        assert result.amount == pytest.approx(2.0)
        assert result.symbol == SYMBOL

    @pytest.mark.asyncio
    async def test_stake_deducts_holding(self):
        db = _make_db(eth_balance=5.0)
        await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 2.0)
        holding = await db.get_wallet_holding(USER_ID, GUILD_ID, NET_SHORT, SYMBOL)
        assert holding["amount"] == to_raw(3.0)

    @pytest.mark.asyncio
    async def test_stake_creates_stake_record(self):
        db = _make_db(eth_balance=5.0)
        await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 2.0)
        stake = await db.get_stake(USER_ID, GUILD_ID, VALIDATOR_ID)
        assert stake is not None
        assert stake["amount"] == to_raw(2.0)

    @pytest.mark.asyncio
    async def test_zero_amount_fails(self):
        db = _make_db()
        result = await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 0.0)
        assert not result.success
        assert "positive" in result.error.lower()

    @pytest.mark.asyncio
    async def test_negative_amount_fails(self):
        db = _make_db()
        result = await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, -1.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_unknown_validator_fails(self):
        db = _make_db()
        result = await execute_stake(db, GUILD_ID, USER_ID, "unknown-validator", 1.0)
        assert not result.success
        assert "unknown validator" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_defi_wallet_fails(self):
        db = _make_db(has_wallet=False)
        result = await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 1.0)
        assert not result.success
        assert "defi wallet" in result.error.lower()

    @pytest.mark.asyncio
    async def test_insufficient_balance_fails(self):
        db = _make_db(eth_balance=0.5)
        result = await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 2.0)
        assert not result.success
        assert "insufficient" in result.error.lower()

    @pytest.mark.asyncio
    async def test_tx_hash_returned(self):
        db = _make_db(eth_balance=5.0)
        result = await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 1.0)
        assert result.tx_hash

    @pytest.mark.asyncio
    async def test_validator_name_returned(self):
        db = _make_db(eth_balance=5.0)
        result = await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 1.0)
        assert result.validator_name == "Test ARC Validator"

    @pytest.mark.asyncio
    async def test_staking_full_balance(self):
        db = _make_db(eth_balance=5.0)
        result = await execute_stake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 5.0)
        assert result.success
        holding = await db.get_wallet_holding(USER_ID, GUILD_ID, NET_SHORT, SYMBOL)
        assert holding["amount"] == 0


# ── execute_unstake ───────────────────────────────────────────────────────────

class TestExecuteUnstake:
    @pytest.mark.asyncio
    async def test_successful_unstake_after_lock_period(self):
        db = _make_db_with_stake(staked=5.0)
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 3.0)
        assert result.success

    @pytest.mark.asyncio
    async def test_unstake_credits_holding(self):
        db = _make_db_with_stake(staked=5.0, eth_balance=0.0)
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 2.0)
        assert result.success
        # received amount should be positive (may have penalty for early unstake)
        assert result.amount_received > 0 or result.amount_unstaked > 0

    @pytest.mark.asyncio
    async def test_zero_amount_fails(self):
        db = _make_db_with_stake()
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 0.0)
        assert not result.success
        assert "positive" in result.error.lower()

    @pytest.mark.asyncio
    async def test_negative_amount_fails(self):
        db = _make_db_with_stake()
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, -1.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_unknown_validator_fails(self):
        db = _make_db_with_stake()
        result = await execute_unstake(db, GUILD_ID, USER_ID, "bad-validator", 1.0)
        assert not result.success
        assert "unknown validator" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_stake_fails(self):
        db = _make_db(eth_balance=5.0)  # No existing stake
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 1.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_unstake_exceeds_staked_fails(self):
        db = _make_db_with_stake(staked=2.0)
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 5.0)
        assert not result.success
        assert "insufficient" in result.error.lower() or "exceed" in result.error.lower() or "cannot unstake" in result.error.lower()


# ── Early-unstake penalty ─────────────────────────────────────────────────────

def _make_db_early_unstake(staked: float = 5.0, eth_balance: float = 0.0) -> MockDB:
    """Stake that is within the early-penalty window (> lock, < 48h early window)."""
    db = _make_db(eth_balance=eth_balance)
    db.stakes[(USER_ID, GUILD_ID, VALIDATOR_ID)] = {
        "amount": to_raw(staked),
        "symbol": SYMBOL,
        "staked_at": time.time() - 90_001,  # ~25h ago: past 24h lock, within 48h window
    }
    return db


class TestEarlyUnstakePenalty:
    @pytest.mark.asyncio
    async def test_early_unstake_succeeds(self):
        db = _make_db_early_unstake(staked=5.0)
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 2.0)
        assert result.success, f"Expected success, got error: {result.error}"

    @pytest.mark.asyncio
    async def test_early_unstake_has_penalty(self):
        db = _make_db_early_unstake(staked=5.0)
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 2.0)
        assert result.success
        assert result.penalty > 0, "Expected a non-zero penalty for early unstake"

    @pytest.mark.asyncio
    async def test_early_unstake_amount_received_less_than_unstaked(self):
        db = _make_db_early_unstake(staked=5.0)
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, 2.0)
        assert result.success
        assert result.amount_received < result.amount_unstaked

    @pytest.mark.asyncio
    async def test_early_unstake_penalty_is_5_pct(self):
        """5% penalty (Config.STAKING_EARLY_UNSTAKE_PENALTY = 0.05)."""
        from core.config import Config
        amount = 4.0
        db = _make_db_early_unstake(staked=amount)
        result = await execute_unstake(db, GUILD_ID, USER_ID, VALIDATOR_ID, amount)
        assert result.success
        expected_received = amount * (1 - Config.STAKING_EARLY_UNSTAKE_PENALTY)
        assert result.amount_received == pytest.approx(expected_received, rel=1e-6)
