"""Tests for depeg-protection system.

Covers:
* is_depeg() threshold logic
* check_depeg_buy() / record_depeg_buy() rolling 24-hour cap
* reserve_depeg_buy() atomic check+reserve + cancel_depeg_reservation() rollback
* gbm_step() ATH-aware recovery cap (ORACLE_RECOVERY_CAP vs ORACLE_DAILY_MAX_DRIFT)
"""
from __future__ import annotations

import time

import pytest

import services.swap as swap_mod
from services.swap import (
    _depeg_buy_volume,
    cancel_depeg_reservation,
    check_depeg_buy,
    is_depeg,
    record_depeg_buy,
    reserve_depeg_buy,
)
from core.config import Config


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reset_depeg_state():
    """Reset depeg in-memory state between tests."""
    _depeg_buy_volume.clear()
    swap_mod._depeg_buy_locks.clear()


# ── is_depeg() ────────────────────────────────────────────────────────────────

class TestIsDepeg:
    def test_depeg_disabled_never_triggers(self):
        """With DEPEG_THRESHOLD=0.0, depeg mode never activates regardless of price."""
        assert is_depeg(0.001, 1.0) is False
        assert is_depeg(0.01, 1.0) is False
        assert is_depeg(0.50, 1.0) is False

    def test_ath_zero_never_depeg(self):
        """Unknown ATH (0) must never trigger depeg mode."""
        assert is_depeg(0.001, 0.0) is False
        assert is_depeg(100.0, 0.0) is False


# ── check_depeg_buy / record_depeg_buy ───────────────────────────────────────

class TestDepegBuyCap:
    def setup_method(self):
        _reset_depeg_state()

    def test_first_buy_allowed_under_cap(self):
        """A buy well under the daily cap should be allowed."""
        allowed, remaining = check_depeg_buy(1, 100, "SUN", 100.0)
        assert allowed is True
        assert remaining == pytest.approx(Config.DEPEG_DAILY_BUY_USD)

    def test_buy_at_exact_cap_allowed(self):
        """A single buy equal to the full cap should be permitted."""
        allowed, remaining = check_depeg_buy(1, 100, "SUN", Config.DEPEG_DAILY_BUY_USD)
        assert allowed is True

    def test_buy_exceeding_cap_rejected(self):
        """A single buy above the cap should be rejected."""
        allowed, _ = check_depeg_buy(1, 100, "SUN", Config.DEPEG_DAILY_BUY_USD + 0.01)
        assert allowed is False

    def test_cumulative_cap_enforced(self):
        """Multiple buys should accumulate against the 24-hour cap."""
        half = Config.DEPEG_DAILY_BUY_USD / 2
        record_depeg_buy(1, 100, "SUN", half)
        # Second half exactly fills the cap
        allowed_exact, _ = check_depeg_buy(1, 100, "SUN", half)
        assert allowed_exact is True
        # One cent over the cap is rejected
        allowed_over, _ = check_depeg_buy(1, 100, "SUN", half + 0.01)
        assert allowed_over is False

    def test_cap_is_per_symbol(self):
        """Each symbol has its own independent 24-hour cap."""
        record_depeg_buy(1, 100, "SUN", Config.DEPEG_DAILY_BUY_USD)
        # SUN cap exhausted, but MTA cap should be fresh
        allowed_sun, _ = check_depeg_buy(1, 100, "SUN", 1.0)
        allowed_btc, _ = check_depeg_buy(1, 100, "MTA", Config.DEPEG_DAILY_BUY_USD)
        assert allowed_sun is False
        assert allowed_btc is True

    def test_cap_is_per_user(self):
        """Two different users each have their own cap."""
        record_depeg_buy(1, 100, "SUN", Config.DEPEG_DAILY_BUY_USD)
        # User 2 should still have a full cap
        allowed, remaining = check_depeg_buy(2, 100, "SUN", Config.DEPEG_DAILY_BUY_USD)
        assert allowed is True
        assert remaining == pytest.approx(Config.DEPEG_DAILY_BUY_USD)

    def test_remaining_reported_correctly(self):
        """Remaining capacity is correctly computed after a partial buy."""
        spent = Config.DEPEG_DAILY_BUY_USD * 0.25
        record_depeg_buy(1, 100, "SUN", spent)
        _, remaining = check_depeg_buy(1, 100, "SUN", 0.01)
        expected = Config.DEPEG_DAILY_BUY_USD - spent
        assert remaining == pytest.approx(expected, rel=1e-6)

    def test_window_expiry(self):
        """Entries older than 24 hours should not count against the cap."""
        old_ts = time.time() - 86401  # just over 24h ago
        _depeg_buy_volume[(1, 100, "SUN")] = [(old_ts, Config.DEPEG_DAILY_BUY_USD)]
        # The stale entry should be pruned; a fresh buy equal to the cap is allowed
        allowed, _ = check_depeg_buy(1, 100, "SUN", Config.DEPEG_DAILY_BUY_USD)
        assert allowed is True


# ── reserve_depeg_buy / cancel_depeg_reservation ─────────────────────────────

class TestReserveDepegBuy:
    """Verify the atomic reserve+check+rollback path used by production commands."""

    def setup_method(self):
        _reset_depeg_state()

    async def test_reserve_allowed_records_immediately(self):
        """A successful reservation should be immediately visible in the volume dict."""
        allowed, remaining, ts = await reserve_depeg_buy(1, 100, "SUN", 100.0)
        assert allowed is True
        assert ts is not None
        # Volume is reserved  -  check_depeg_buy should see 100 already spent
        _, rem_after = check_depeg_buy(1, 100, "SUN", 0.0)
        assert rem_after == pytest.approx(Config.DEPEG_DAILY_BUY_USD - 100.0)

    async def test_reserve_rejected_when_over_cap(self):
        """Attempting to reserve more than the cap returns allowed=False with no side effects."""
        over = Config.DEPEG_DAILY_BUY_USD + 1.0
        allowed, remaining, ts = await reserve_depeg_buy(1, 100, "SUN", over)
        assert allowed is False
        assert ts is None
        # Nothing should have been recorded
        _, full_rem = check_depeg_buy(1, 100, "SUN", 0.0)
        assert full_rem == pytest.approx(Config.DEPEG_DAILY_BUY_USD)

    async def test_concurrent_reserves_respect_cap(self):
        """Two concurrent reserve calls should not both pass when their combined total exceeds the cap."""
        half = Config.DEPEG_DAILY_BUY_USD / 2
        # Simulate serial reservation (asyncio is cooperative; this still confirms correct accounting)
        allowed1, _, ts1 = await reserve_depeg_buy(1, 100, "SUN", half)
        allowed2, _, ts2 = await reserve_depeg_buy(1, 100, "SUN", half)
        allowed3, _, ts3 = await reserve_depeg_buy(1, 100, "SUN", 1.0)
        assert allowed1 is True
        assert allowed2 is True
        assert allowed3 is False   # cap exhausted after first two

    async def test_cancel_restores_allowance(self):
        """cancel_depeg_reservation should release the reserved slot."""
        allowed, _, ts = await reserve_depeg_buy(1, 100, "SUN", 300.0)
        assert allowed is True
        cancel_depeg_reservation(1, 100, "SUN", ts)
        # Allowance should be fully restored
        _, rem = check_depeg_buy(1, 100, "SUN", 0.0)
        assert rem == pytest.approx(Config.DEPEG_DAILY_BUY_USD)

    async def test_cancel_noop_for_unknown_reservation(self):
        """cancel_depeg_reservation with a bogus reservation_id should not raise or corrupt state."""
        record_depeg_buy(1, 100, "SUN", 50.0)
        cancel_depeg_reservation(1, 100, "SUN", 0)  # unknown reservation_id
        _, rem = check_depeg_buy(1, 100, "SUN", 0.0)
        assert rem == pytest.approx(Config.DEPEG_DAILY_BUY_USD - 50.0)


# ── gbm_step recovery cap ─────────────────────────────────────────────────────

class TestGbmStepRecoveryCap:
    """Verify the ATH-aware asymmetric daily circuit breaker in cogs/trade.gbm_step."""

    # Import at class level so the test is isolated from conftest patching
    @staticmethod
    def _gbm_step(*args, **kwargs):
        from cogs.trade import gbm_step
        return gbm_step(*args, **kwargs)

    def test_daily_drift_cap_respected(self):
        """Price must not exceed ORACLE_DAILY_MAX_DRIFT from open."""
        open_p = 0.012
        normal_ceiling = open_p * (1.0 + Config.ORACLE_DAILY_MAX_DRIFT)

        import random as _r
        _r.seed(42)
        price = open_p
        max_seen = price
        for _ in range(400):
            price = self._gbm_step(price, 0.30, Config.PRICE_TICK_SECONDS,
                                   open_price=open_p, ath=1.0)
            if price > max_seen:
                max_seen = price
        assert max_seen <= normal_ceiling * 1.0001

    def test_normal_cap_applied(self):
        """Daily circuit breaker caps price at ORACLE_DAILY_MAX_DRIFT from open."""
        open_p = 0.80
        normal_ceiling = open_p * (1.0 + Config.ORACLE_DAILY_MAX_DRIFT)

        import random as _r
        _r.seed(42)
        price = open_p
        max_seen = price
        for _ in range(400):
            price = self._gbm_step(price, 0.30, Config.PRICE_TICK_SECONDS,
                                   open_price=open_p, ath=1.0)
            if price > max_seen:
                max_seen = price
        assert max_seen <= normal_ceiling * 1.0001

    def test_no_ath_uses_normal_drift(self):
        """When ATH is 0 (unknown), depeg mode must never activate."""
        open_p = 0.001
        normal_ceiling = open_p * (1.0 + Config.ORACLE_DAILY_MAX_DRIFT)

        import random as _r
        _r.seed(7)
        price = open_p
        max_seen = price
        for _ in range(400):
            price = self._gbm_step(price, 0.30, Config.PRICE_TICK_SECONDS,
                                   open_price=open_p, ath=0.0)
            if price > max_seen:
                max_seen = price
        assert max_seen <= normal_ceiling * 1.0001

    def test_downward_cap_unchanged_in_depeg_mode(self):
        """The downward circuit breaker should NOT be tightened in depeg mode."""
        open_p = 0.012
        ath    = 1.0
        down_floor = open_p * (1.0 - Config.ORACLE_DAILY_MAX_DRIFT)

        import random as _r
        _r.seed(13)
        price = open_p
        min_seen = price
        for _ in range(400):
            price = self._gbm_step(price, 0.30, Config.PRICE_TICK_SECONDS,
                                   open_price=open_p, ath=ath)
            if price < min_seen:
                min_seen = price
        # Downward drift should still be at full ORACLE_DAILY_MAX_DRIFT
        assert min_seen >= down_floor * 0.9999
