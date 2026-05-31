"""Tests for services/transfer.py  -  wallet-to-wallet USD transfer service."""
from __future__ import annotations

import math

import pytest

from core.framework.scale import to_raw
from services.transfer import execute_transfer
from tests.conftest import GUILD_ID, USER_ID, OTHER_USER_ID, MockDB


def _make_db(sender_wallet: float = 500.0, recipient_wallet: float = 0.0) -> MockDB:
    # Wallet column is raw NUMERIC(36,0) * 10**18 in production; mirror that.
    db = MockDB()
    db.users[(USER_ID, GUILD_ID)] = {"wallet": to_raw(sender_wallet), "bank": 0}
    db.users[(OTHER_USER_ID, GUILD_ID)] = {"wallet": to_raw(recipient_wallet), "bank": 0}
    return db


# ── Successful transfers ───────────────────────────────────────────────────────

class TestExecuteTransferSuccess:
    @pytest.mark.asyncio
    async def test_basic_transfer(self):
        db = _make_db(sender_wallet=200.0, recipient_wallet=0.0)
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 100.0)
        assert result.success
        assert result.amount == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_sender_debited(self):
        db = _make_db(sender_wallet=500.0)
        await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 200.0)
        sender = await db.get_user(USER_ID, GUILD_ID)
        assert sender["wallet"] == to_raw(300.0)

    @pytest.mark.asyncio
    async def test_recipient_credited(self):
        db = _make_db(sender_wallet=500.0, recipient_wallet=50.0)
        await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 100.0)
        recip = await db.get_user(OTHER_USER_ID, GUILD_ID)
        assert recip["wallet"] == to_raw(150.0)

    @pytest.mark.asyncio
    async def test_new_balance_returned(self):
        db = _make_db(sender_wallet=300.0)
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 100.0)
        assert result.new_balance == pytest.approx(200.0)

    @pytest.mark.asyncio
    async def test_tx_hash_returned(self):
        db = _make_db()
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 50.0)
        assert result.tx_hash  # non-empty string

    @pytest.mark.asyncio
    async def test_exact_balance_transfer(self):
        """Transferring the sender's exact balance should succeed."""
        db = _make_db(sender_wallet=100.0)
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 100.0)
        assert result.success
        sender = await db.get_user(USER_ID, GUILD_ID)
        assert sender["wallet"] == 0

    @pytest.mark.asyncio
    async def test_recipient_auto_created(self):
        """Transfer to a user that doesn't exist yet should create their account."""
        db = MockDB()
        db.users[(USER_ID, GUILD_ID)] = {"wallet": to_raw(500.0), "bank": 0}
        NEW_USER_ID = 999_999_999
        result = await execute_transfer(db, GUILD_ID, USER_ID, NEW_USER_ID, 50.0)
        assert result.success
        new_user = await db.get_user(NEW_USER_ID, GUILD_ID)
        assert new_user is not None

    @pytest.mark.asyncio
    async def test_small_decimal_amount(self):
        db = _make_db(sender_wallet=10.0)
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 0.000001)
        assert result.success


# ── Validation failures ────────────────────────────────────────────────────────

class TestExecuteTransferFailures:
    @pytest.mark.asyncio
    async def test_self_transfer_fails(self):
        db = _make_db()
        result = await execute_transfer(db, GUILD_ID, USER_ID, USER_ID, 100.0)
        assert not result.success
        assert "yourself" in result.error.lower()

    @pytest.mark.asyncio
    async def test_zero_amount_fails(self):
        db = _make_db()
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 0.0)
        assert not result.success
        assert "positive" in result.error.lower()

    @pytest.mark.asyncio
    async def test_negative_amount_fails(self):
        db = _make_db()
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, -50.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_insufficient_balance_fails(self):
        db = _make_db(sender_wallet=50.0)
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 100.0)
        assert not result.success
        assert "insufficient" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_sender_account_fails(self):
        db = MockDB()
        # Sender has no account at all
        db.users[(OTHER_USER_ID, GUILD_ID)] = {"wallet": 0, "bank": 0}
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 50.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_inf_amount_fails(self):
        db = _make_db()
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, math.inf)
        assert not result.success
        assert "finite" in result.error.lower()

    @pytest.mark.asyncio
    async def test_nan_amount_fails(self):
        db = _make_db()
        result = await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, float("nan"))
        assert not result.success

    @pytest.mark.asyncio
    async def test_over_balance_does_not_modify_state(self):
        """A failed transfer must not change any balances."""
        db = _make_db(sender_wallet=50.0, recipient_wallet=10.0)
        await execute_transfer(db, GUILD_ID, USER_ID, OTHER_USER_ID, 100.0)
        sender = await db.get_user(USER_ID, GUILD_ID)
        recip = await db.get_user(OTHER_USER_ID, GUILD_ID)
        assert sender["wallet"] == to_raw(50.0)
        assert recip["wallet"] == to_raw(10.0)
