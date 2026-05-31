"""Tests for AMM economics in services/swap.py.

Covers:
* Constant-product AMM math (amount_out formula)
* Price impact calculation
* User hourly swap-volume limits
* execute_swap execution path (fee burn, balance changes)
"""
from __future__ import annotations


import pytest

from services.swap import (
    DEFAULT_SWAP_FEE,
    cancel_user_swap_reservation,
    check_user_swap_volume,
    record_user_swap_volume,
    reserve_user_swap_volume,
    _user_swap_locks,
    _user_swap_volume,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reset_state():
    """Reset global in-memory state between tests."""
    _user_swap_volume.clear()
    _user_swap_locks.clear()


def amm_out(amount_in: float, reserve_in: float, reserve_out: float, fee: float = DEFAULT_SWAP_FEE) -> float:
    """Replicate the constant-product AMM formula used in compute_swap_quote."""
    amount_in_with_fee = amount_in * (1 - fee)
    return reserve_out * amount_in_with_fee / (reserve_in + amount_in_with_fee)


# ── Constant-product AMM math ─────────────────────────────────────────────────

class TestAMMFormula:
    def test_basic_swap_out_positive(self):
        out = amm_out(10, 1000, 1000)
        assert out > 0

    def test_symmetric_pool_price_close_to_spot(self):
        """In a deep pool, small trade exec price should be close to spot price (within fee)."""
        amount_in = 1.0
        reserve = 100_000.0
        out = amm_out(amount_in, reserve, reserve)
        exec_price = out / amount_in
        spot_price = 1.0  # equal reserves → spot = 1
        # The deviation is roughly the fee (1%) plus a tiny price impact
        assert abs(exec_price - spot_price) / spot_price < DEFAULT_SWAP_FEE + 0.002

    def test_price_impact_increases_with_trade_size(self):
        reserve = 10_000.0
        out_small = amm_out(1, reserve, reserve)
        out_large = amm_out(1000, reserve, reserve)
        exec_small = out_small / 1
        exec_large = out_large / 1000
        assert exec_large < exec_small, "Larger trades must have worse execution price"

    def test_constant_product_invariant(self):
        """After a swap, k = reserve_in * reserve_out must not increase."""
        reserve_in, reserve_out = 5000.0, 10000.0
        amount_in = 100.0
        k_before = reserve_in * reserve_out
        out = amm_out(amount_in, reserve_in, reserve_out)
        new_reserve_in = reserve_in + amount_in
        new_reserve_out = reserve_out - out
        k_after = new_reserve_in * new_reserve_out
        # k should be >= k_before due to fees collected
        assert k_after >= k_before * (1 - 1e-9)

    def test_zero_amount_in_gives_zero_out(self):
        assert amm_out(0, 1000, 1000) == 0.0

    def test_output_less_than_reserve(self):
        """The AMM can never drain more than 100% of the output reserve."""
        out = amm_out(1_000_000, 1000, 1000)
        assert out < 1000.0

    def test_fee_reduces_output(self):
        out_with_fee = amm_out(100, 10000, 10000, fee=0.003)
        out_no_fee = amm_out(100, 10000, 10000, fee=0.0)
        assert out_with_fee < out_no_fee

    def test_price_impact_formula(self):
        """price_impact = max(0, (spot - exec) / spot)"""
        reserve_in, reserve_out = 1000.0, 1000.0
        amount_in = 50.0
        out = amm_out(amount_in, reserve_in, reserve_out)
        spot = reserve_out / reserve_in  # = 1.0
        exec_price = out / amount_in
        expected_impact = max(0.0, (spot - exec_price) / spot)
        assert expected_impact > 0

    def test_asymmetric_pool_spot_price(self):
        """ARC/USD pool: 1000 ARC at $2000 each → reserve_usd = 2_000_000."""
        reserve_eth = 1000.0
        reserve_usd = 2_000_000.0
        # Buying 1 ARC: spot price = reserve_usd / reserve_eth = 2000
        spot = reserve_usd / reserve_eth
        assert spot == pytest.approx(2000.0)


# ── User hourly swap-volume limits ─────────────────────────────────────────────

class TestUserSwapVolume:
    def setup_method(self):
        _reset_state()

    def test_fresh_user_allowed(self):
        allowed, remaining = check_user_swap_volume(1, 1, 100.0)
        assert allowed is True

    def test_zero_usd_allowed(self):
        allowed, _ = check_user_swap_volume(1, 1, 0.0)
        assert allowed is True

    def test_record_then_check(self):
        uid, gid = 10, 20
        # Consume most of the limit
        from core.config import Config
        limit = Config.USER_SWAP_HOURLY_LIMIT_USD
        record_user_swap_volume(uid, gid, limit - 1.0)
        # Remaining = limit - recorded = 1.0; a swap of 0.5 is within that
        allowed, remaining = check_user_swap_volume(uid, gid, 0.5)
        assert allowed is True
        # remaining reflects the available capacity BEFORE this potential swap
        assert remaining == pytest.approx(1.0, abs=0.01)

    def test_over_limit_rejected(self):
        uid, gid = 11, 21
        from core.config import Config
        limit = Config.USER_SWAP_HOURLY_LIMIT_USD
        record_user_swap_volume(uid, gid, limit)
        allowed, remaining = check_user_swap_volume(uid, gid, 1.0)
        assert allowed is False
        assert remaining == pytest.approx(0.0)

    def test_different_users_independent(self):
        from core.config import Config
        limit = Config.USER_SWAP_HOURLY_LIMIT_USD
        record_user_swap_volume(100, 1, limit)
        # Different user should still be allowed
        allowed, _ = check_user_swap_volume(200, 1, limit)
        assert allowed is True

    def test_different_guilds_independent(self):
        from core.config import Config
        limit = Config.USER_SWAP_HOURLY_LIMIT_USD
        record_user_swap_volume(1, 100, limit)
        # Same user in different guild should still be allowed
        allowed, _ = check_user_swap_volume(1, 200, limit)
        assert allowed is True

    def test_old_entries_expire(self):
        """Entries older than 1 hour must not count against the limit."""
        uid, gid = 50, 50
        from core.config import Config
        limit = Config.USER_SWAP_HOURLY_LIMIT_USD
        # Inject a stale entry directly
        import time
        key = (uid, gid)
        _user_swap_volume[key] = [(0, time.time() - 3601, limit)]
        # The stale record should be ignored
        allowed, remaining = check_user_swap_volume(uid, gid, limit)
        assert allowed is True
        assert remaining == pytest.approx(limit, rel=1e-3)

    @pytest.mark.asyncio
    async def test_reserve_then_cancel_releases_capacity(self):
        uid, gid = 77, 88
        allowed, remaining, ts = await reserve_user_swap_volume(uid, gid, 123.0)
        assert allowed is True
        assert ts is not None
        allowed_after_reserve, _ = check_user_swap_volume(uid, gid, remaining + 0.01)
        assert allowed_after_reserve is False

        cancel_user_swap_reservation(uid, gid, ts)
        allowed_after_cancel, _ = check_user_swap_volume(uid, gid, 123.0)
        assert allowed_after_cancel is True


# ── execute_swap ──────────────────────────────────────────────────────────────

from unittest.mock import AsyncMock

from core.framework.scale import to_raw
from services.swap import SwapQuote, execute_swap
from tests.conftest import GUILD_ID, USER_ID, MockDB


def _make_quote(
    token_in: str = "MYTOKEN",
    token_out: str = "USD",
    amount_in: float = 100.0,
    amount_out: float = 95.0,
    fee: float = 0.003,
    fee_amount: float = 0.3,
    reserve_in: float = 10_000.0,
    reserve_out: float = 10_000.0,
    pool_id: str = "MYTOKEN_USD",
    canon_a: str = "MYTOKEN",
) -> SwapQuote:
    # SwapQuote fields are human-scale floats; execute_swap converts to raw at
    # the DB write boundary via to_raw().
    return SwapQuote(
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        amount_out=amount_out,
        fee=fee,
        fee_amount=fee_amount,
        price_impact=0.001,
        spot_price=1.0,
        exec_price=0.95,
        pool_id=pool_id,
        canon_a=canon_a,
        canon_b=token_out,
        reserve_in=reserve_in,
        reserve_out=reserve_out,
        use_mempool=False,
        net_short="",
        network="",
        swap_usd_value=100.0,
    )


def _make_swap_db(token_in: str = "MYTOKEN", amount_in: float = 100.0) -> MockDB:
    db = MockDB()
    # Pool reserves and holdings are raw NUMERIC(36,0) * 10**18 in production.
    db.pools[("MYTOKEN_USD", GUILD_ID)] = {
        "pool_id": "MYTOKEN_USD",
        "reserve_a": to_raw(10_000.0),
        "reserve_b": to_raw(10_000.0),
        "total_lp": to_raw(500.0),
    }
    # User has enough token_in
    db.holdings[(USER_ID, GUILD_ID, token_in.upper())] = {
        "amount": to_raw(amount_in + 10), "symbol": token_in.upper(),
    }
    db.users[(USER_ID, GUILD_ID)] = {"wallet": 0, "bank": 0}
    return db


class TestExecuteSwap:
    def setup_method(self):
        _reset_state()

    @pytest.mark.asyncio
    async def test_execute_swap_debits_input(self):
        db = _make_swap_db()
        quote = _make_quote()
        result = await execute_swap(db, GUILD_ID, USER_ID, quote)
        assert result.success, f"swap failed: {result.error}"
        holding = await db.get_holding(USER_ID, GUILD_ID, "MYTOKEN")
        # started with amount_in + 10 = 110; debited 100 → 10 remaining
        assert holding["amount"] == to_raw(10.0)

    @pytest.mark.asyncio
    async def test_execute_swap_credits_output(self):
        db = _make_swap_db()
        quote = _make_quote()
        result = await execute_swap(db, GUILD_ID, USER_ID, quote)
        assert result.success
        user = await db.get_user(USER_ID, GUILD_ID)
        assert user["wallet"] == to_raw(quote.amount_out)

    @pytest.mark.asyncio
    async def test_fee_burn_reduces_pool_reserve_in(self):
        """Pool reserve_in = reserve_in + amount_in - burn_amount."""
        from core.config import Config
        db = _make_swap_db()
        quote = _make_quote()
        await execute_swap(db, GUILD_ID, USER_ID, quote)
        pool = await db.get_pool("MYTOKEN_USD", GUILD_ID)
        burn = quote.fee_amount * Config.FEE_BURN_FRACTION
        expected_reserve_a_h = quote.reserve_in + quote.amount_in - burn
        assert pool["reserve_a"] == pytest.approx(to_raw(expected_reserve_a_h), rel=1e-9)

    @pytest.mark.asyncio
    async def test_fee_burn_calls_update_circulating_supply(self, monkeypatch):
        """Custom (non-builtin) token burn → update_circulating_supply is called.

        FEE_BURN_FRACTION defaults to 0 so all swap fees accrue to LPs; force a
        non-zero burn here so the test exercises the burn code path end-to-end.
        """
        from core.config import Config
        monkeypatch.setattr(Config, "FEE_BURN_FRACTION", 0.10)
        db = _make_swap_db()
        db.update_circulating_supply = AsyncMock()
        quote = _make_quote(token_in="MYTOKEN")
        await execute_swap(db, GUILD_ID, USER_ID, quote)
        expected_burn_raw = -to_raw(quote.fee_amount * Config.FEE_BURN_FRACTION)
        db.update_circulating_supply.assert_called_once_with(
            GUILD_ID, "MYTOKEN", expected_burn_raw,
        )

    @pytest.mark.asyncio
    async def test_fee_burn_calls_update_builtin_circulating_supply(self, monkeypatch):
        """Built-in token burn (e.g. ARC) → update_builtin_circulating_supply is called.

        FEE_BURN_FRACTION defaults to 0 so all swap fees accrue to LPs; force a
        non-zero burn here so the test exercises the burn code path end-to-end.
        """
        from core.config import Config
        monkeypatch.setattr(Config, "FEE_BURN_FRACTION", 0.10)
        db = MockDB()
        db.pools[("ETH_USD", GUILD_ID)] = {
            "pool_id": "ETH_USD",
            "reserve_a": to_raw(10_000.0),
            "reserve_b": to_raw(10_000.0),
            "total_lp": to_raw(500.0),
        }
        db.holdings[(USER_ID, GUILD_ID, "ARC")] = {"amount": to_raw(110.0), "symbol": "ARC"}
        db.users[(USER_ID, GUILD_ID)] = {"wallet": 0, "bank": 0}
        db.update_builtin_circulating_supply = AsyncMock()

        quote = _make_quote(token_in="ARC", token_out="USD", pool_id="ETH_USD", canon_a="ARC")
        await execute_swap(db, GUILD_ID, USER_ID, quote)
        expected_burn_raw = -to_raw(quote.fee_amount * Config.FEE_BURN_FRACTION)
        db.update_builtin_circulating_supply.assert_called_once_with(
            GUILD_ID, "ARC", expected_burn_raw,
        )


# ── Liqstone swap-fee discount ───────────────────────────────────────────────
# The discount is defined in items_config.py as `stats.swap_fee_discount` per
# level (0.001 = -0.1% per level). These tests pin the helper behaviour so a
# regression (e.g. discount not applied, cap removed) breaks loudly.

from services.swap import apply_liqstone_discount, liqstone_swap_fee_discount


class TestLiqstoneDiscount:

    def test_apply_discount_subtracts_and_clamps(self):
        # Normal case: 0.003 fee, 0.001 discount → 0.002
        assert apply_liqstone_discount(0.003, 0.001) == pytest.approx(0.002)
        # Floor at zero so fee can't go negative from an oversized discount.
        assert apply_liqstone_discount(0.003, 0.01) == 0.0
        # None / zero discount returns base fee unchanged.
        assert apply_liqstone_discount(0.003, 0.0) == pytest.approx(0.003)

    @pytest.mark.asyncio
    async def test_discount_zero_without_liqstone(self):
        class _NoLiqDB:
            async def get_liqstone(self, uid, gid): return None
        assert await liqstone_swap_fee_discount(_NoLiqDB(), 1, 1) == 0.0

    @pytest.mark.asyncio
    async def test_discount_scales_with_level(self):
        class _LiqDB:
            def __init__(self, level): self.level = level
            async def get_liqstone(self, uid, gid):
                return {"level": self.level}

        # items_config: swap_fee_discount = 0.001 per level, capped by helper at
        # 90% of DEFAULT_SWAP_FEE so the effective fee stays above zero.
        d1 = await liqstone_swap_fee_discount(_LiqDB(1), 1, 1)
        d5 = await liqstone_swap_fee_discount(_LiqDB(5), 1, 1)
        assert d1 == pytest.approx(0.001)
        assert d5 == pytest.approx(0.005)
        # At very high levels the raw discount exceeds the cap.
        d_high = await liqstone_swap_fee_discount(_LiqDB(99), 1, 1)
        assert d_high == pytest.approx(DEFAULT_SWAP_FEE * 0.9)

    @pytest.mark.asyncio
    async def test_discount_caps_below_base_fee(self):
        """At max level the raw discount (0.1) would exceed the 0.003 base fee.
        The helper caps the returned discount so effective fee stays >0."""
        class _LiqDB:
            async def get_liqstone(self, uid, gid): return {"level": 100}
        d = await liqstone_swap_fee_discount(_LiqDB(), 1, 1)
        assert d < DEFAULT_SWAP_FEE  # room left for a positive effective fee
        assert apply_liqstone_discount(DEFAULT_SWAP_FEE, d) > 0.0

    @pytest.mark.asyncio
    async def test_discount_swallows_db_errors(self):
        """A broken/missing get_liqstone must not take down swaps."""
        class _BrokenDB:
            async def get_liqstone(self, uid, gid):
                raise RuntimeError("db down")
        assert await liqstone_swap_fee_discount(_BrokenDB(), 1, 1) == 0.0

        class _NoAttrDB: pass
        assert await liqstone_swap_fee_discount(_NoAttrDB(), 1, 1) == 0.0
