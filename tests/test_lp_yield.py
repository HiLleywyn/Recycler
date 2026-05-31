from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import Config
from core.framework.scale import to_raw
from services.lp_yield import (
    estimate_position_apr,
    tick_lp_yield_for_guild,
)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _row(**kw) -> dict:
    # Default last_yield_at to ~1 hour ago so the tick pays exactly one
    # period of yield (any drift from `time.time()` is < 1ms in tests).
    base = {
        "user_id": 1,
        "pool_id": "ARC-USDC",
        "lp_shares": to_raw(10.0),
        "lock_tier": 0,
        "locked_until": None,
        "last_yield_at": _now() - _dt.timedelta(hours=1),
        "token_a": "ARC",
        "token_b": "USDC",
        "reserve_a": to_raw(100.0),
        "reserve_b": to_raw(200_000.0),
        "total_lp": to_raw(100.0),
        "is_group_pool": False,
        "vault_locked": False,
    }
    base.update(kw)
    return base


def test_estimate_position_apr_unlocked() -> None:
    apr = estimate_position_apr(_row(), set())
    assert apr == pytest.approx(Config.LP_YIELD_APR)


def test_estimate_position_apr_locked_tier_2() -> None:
    apr = estimate_position_apr(
        _row(lock_tier=2, locked_until=_dt.datetime(2099, 1, 1, tzinfo=_dt.timezone.utc)),
        set(),
    )
    expected = Config.LP_YIELD_APR * Config.LP_YIELD_LOCK_BONUS[2]
    assert apr == pytest.approx(expected)


def test_estimate_position_apr_user_token_pool() -> None:
    apr = estimate_position_apr(_row(token_a="DOGE"), {"DOGE"})
    expected = Config.LP_YIELD_APR * Config.LP_YIELD_USER_TOKEN_BONUS
    assert apr == pytest.approx(expected)


def test_estimate_position_apr_group_pool_stacks() -> None:
    apr = estimate_position_apr(
        _row(is_group_pool=True, lock_tier=3,
             locked_until=_dt.datetime(2099, 1, 1, tzinfo=_dt.timezone.utc),
             token_a="GRP1", token_b="GRP2"),
        {"GRP1", "GRP2"},
    )
    expected = (
        Config.LP_YIELD_APR
        * Config.LP_YIELD_LOCK_BONUS[3]
        * Config.LP_YIELD_USER_TOKEN_BONUS
        * Config.LP_YIELD_GROUP_POOL_BONUS
    )
    assert apr == pytest.approx(expected)


def test_estimate_position_apr_lapsed_lock_pays_unlocked_rate() -> None:
    """A lock_tier > 0 with locked_until in the past should not earn the bonus."""
    apr = estimate_position_apr(
        _row(lock_tier=3, locked_until=_dt.datetime(1999, 1, 1, tzinfo=_dt.timezone.utc)),
        set(),
    )
    assert apr == pytest.approx(Config.LP_YIELD_APR)


@pytest.mark.asyncio
async def test_tick_lp_yield_pays_user_position() -> None:
    """One unlocked position worth $200,000 should pay base APR / hours-per-year."""
    db = MagicMock()
    db.fetch_all = AsyncMock(side_effect=[[_row()], []])
    db.get_price = AsyncMock(side_effect=[
        {"price": 1000.0},  # ARC
        {"price": 1.0},     # USDC
    ])
    db.get_guild_tokens = AsyncMock(return_value=[])
    db.update_wallet = AsyncMock()
    db.execute = AsyncMock()
    db.log_tx = AsyncMock(return_value="hash")

    class _Atomic:
        async def __aenter__(self_inner):
            return self_inner
        async def __aexit__(self_inner, *exc):
            return False
    db.atomic = MagicMock(return_value=_Atomic())

    result = await tick_lp_yield_for_guild(db, guild_id=42)

    assert result.user_payouts == 1
    assert result.group_payouts == 0
    # Pool reserves: 100 ARC @ $1k + 200,000 USDC @ $1 = $300k TVL.
    # Position owns 10/100 = 10% = $30,000 USD value.
    # Base APR 30% over 1h ≈ $30,000 * 0.30 / 8760 = $1.03.
    assert result.total_user_usd == pytest.approx(
        30_000 * Config.LP_YIELD_APR / (24 * 365), rel=5e-3
    )
    db.update_wallet.assert_awaited_once()
    db.log_tx.assert_awaited_once()


@pytest.mark.asyncio
async def test_tick_lp_yield_skips_dust_positions() -> None:
    """Position worth less than LP_YIELD_MIN_USD must not pay or write."""
    db = MagicMock()
    dust = _row(lp_shares=to_raw(0.0001))  # 0.0001/100 of $400k pool = $0.40
    db.fetch_all = AsyncMock(side_effect=[[dust], []])
    db.get_price = AsyncMock(side_effect=[
        {"price": 1000.0},
        {"price": 1.0},
    ])
    db.get_guild_tokens = AsyncMock(return_value=[])
    db.update_wallet = AsyncMock()
    db.log_tx = AsyncMock()

    result = await tick_lp_yield_for_guild(db, guild_id=42)
    assert result.user_payouts == 0
    assert result.skipped_min_value == 1
    db.update_wallet.assert_not_awaited()
    db.log_tx.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_lp_yield_pays_group_to_reserve() -> None:
    """Group LP positions credit reserve_usd, not a user wallet."""
    db = MagicMock()
    grp = _row()
    grp.pop("user_id")
    grp["group_id"] = "g1"
    grp["is_group_pool"] = True
    db.fetch_all = AsyncMock(side_effect=[[], [grp]])
    db.get_price = AsyncMock(side_effect=[
        {"price": 1000.0},
        {"price": 1.0},
    ])
    db.get_guild_tokens = AsyncMock(return_value=[])
    db.add_group_reserve_usd = AsyncMock()
    db.execute = AsyncMock()

    class _Atomic:
        async def __aenter__(self_inner):
            return self_inner
        async def __aexit__(self_inner, *exc):
            return False
    db.atomic = MagicMock(return_value=_Atomic())

    result = await tick_lp_yield_for_guild(db, guild_id=42)
    assert result.group_payouts == 1
    db.add_group_reserve_usd.assert_awaited_once()
    # Position owns 10% of $300k = $30,000. Group pool 2x bonus stacks on top.
    expected = 30_000 * Config.LP_YIELD_APR * Config.LP_YIELD_GROUP_POOL_BONUS / (24 * 365)
    assert result.total_group_usd == pytest.approx(expected, rel=5e-3)
