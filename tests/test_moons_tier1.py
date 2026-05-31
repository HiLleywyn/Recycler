"""Unit tests for the Lunar Mint (Slice 1 of the Moons economy).

Covers the `_tick_row` emission path in `cogs.moons.Moons` and the
`upsert_lunar_stake` warmup-preserving behavior in `database.moons`.

The command handlers (`.moon stake` / `unstake` / `info`) are not tested
here because they require a real discord.py context; their internal logic
delegates to the repo methods and `_tick_row`, which ARE covered.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import Config
from core.framework.scale import to_raw
from constants.moons import (
    MOON_SYMBOL,
    MOON_NETWORK_SHORT,
    MOON_EMISSION_RATE,
    MOON_TWAP_WINDOW,
    GROUP_ACTIVITY_BONUS_MAX,
    PER_USER_DAILY_MOON_CAP,
    PER_GUILD_DAILY_MOON_CAP,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

FIXED_NOW = 1_700_000_000.0
GID = 111_000_000
UID = 222_000_000
SYM = "CAT"


def _make_db(
    *,
    twap: float = 1.0,
    spot: float = 10.0,
    miners: int = 3,
    blocks: int = 2,
    user_mined: float = 0.0,
    guild_mined: float = 0.0,
    circulating: float = 0.0,
) -> MagicMock:
    """Build a MagicMock db with the surface the tick path touches.

    Defaults place the stake under full warmup with full activity bonus so
    every knob is "live"; tests override specific values to isolate each
    guard.
    """
    db = MagicMock()
    db.get_twap = AsyncMock(return_value=(twap, 0.0))
    db.get_price = AsyncMock(return_value={
        "price": spot, "circulating_supply": circulating,
        "h": lambda col: circulating if col == "circulating_supply" else 0.0,
    })
    db.get_group_activity_for_token = AsyncMock(return_value=(miners, blocks))
    db.get_user_moon_minted_recent = AsyncMock(return_value=user_mined)
    db.get_guild_moon_minted_recent = AsyncMock(return_value=guild_mined)
    db.update_wallet_holding = AsyncMock()
    db.execute = AsyncMock()
    db.record_lunar_earnings = AsyncMock()
    db.log_tx = AsyncMock()
    db.atomic = lambda: _AsyncNoop()
    # Slice 3 vault-level bonus reads the Moon Network vault row. Default to
    # None so vault_level = 0 (baseline multiplier 1.0) and every pre-Slice-3
    # assertion stays byte-for-byte identical.
    db.fetch_one = AsyncMock(return_value=None)
    return db


class _AsyncNoop:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _make_row(stake_h: float, *, age_secs: float) -> dict:
    """A lunar_stakes row with ``stake_h`` MOON (scaled to raw) staked
    ``age_secs`` ago."""
    return {
        "user_id": UID,
        "symbol": SYM,
        "amount": to_raw(stake_h),
        "staked_at": FIXED_NOW - age_secs,
    }


def _make_cog_instance(db: MagicMock):
    """Instantiate Moons without running the background loop.

    `Moons.__init__` calls `self.lunar_tick.start()` which needs an event
    loop; patching `tasks.loop` at import time is more fragile than just
    mocking the ``start``/``cancel`` methods on the bound task.
    """
    from cogs.moons import Moons
    bot = MagicMock()
    bot.db = db
    # wait_until_ready is awaited by the task-loop's before_loop hook; if it
    # is a plain MagicMock the background task errors and pytest surfaces a
    # "Task exception was never retrieved" warning that clutters the run.
    bot.wait_until_ready = AsyncMock()
    with patch.object(Moons.lunar_tick, "start"), \
         patch.object(Moons.lunar_tick, "cancel"):
        cog = Moons(bot)
    return cog


# ── _tick_row emission tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tick_row_emits_at_full_warmup_full_activity():
    """$100 stake, 13h old (full warmup), 3 miners + 2 blocks (full bonus).

    expected = 100 * 0.008 / 24 * 1.0 * (1 + 0.25) = 0.04166... MOON
    """
    db = _make_db(twap=1.0, miners=3, blocks=2)
    cog = _make_cog_instance(db)

    row = _make_row(stake_h=100.0, age_secs=13 * 3600)
    headroom_h = float(Config.TOKENS[MOON_SYMBOL]["max_supply"])

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_row(db, GID, row, FIXED_NOW, headroom_h)

    db.update_wallet_holding.assert_called_once()
    args = db.update_wallet_holding.call_args.args
    assert args[0] == UID
    assert args[1] == GID
    assert args[2] == MOON_NETWORK_SHORT
    assert args[3] == MOON_SYMBOL
    expected_raw = to_raw(100.0 * MOON_EMISSION_RATE / 24.0 * 1.0 * (1 + GROUP_ACTIVITY_BONUS_MAX))
    assert args[4] == expected_raw

    # Circulating supply bumped and tx logged with LUNAR_MINT type.
    db.execute.assert_called_once()
    db.record_lunar_earnings.assert_called_once()
    db.log_tx.assert_called_once()
    assert db.log_tx.call_args.args[2] == "LUNAR_MINT"
    assert db.log_tx.call_args.kwargs.get("network") == MOON_NETWORK_SHORT


@pytest.mark.asyncio
async def test_tick_row_warmup_half_at_6h():
    """Stake 6h old -> warmup = 0.5 of Config.STAKING_WARMUP_SECONDS (12h).

    expected = 100 * 0.008 / 24 * 0.5 * 1.25
    """
    db = _make_db(twap=1.0, miners=3, blocks=2)
    cog = _make_cog_instance(db)

    row = _make_row(stake_h=100.0, age_secs=6 * 3600)
    await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    expected = 100.0 * MOON_EMISSION_RATE / 24.0 * 0.5 * (1 + GROUP_ACTIVITY_BONUS_MAX)
    args = db.update_wallet_holding.call_args.args
    assert args[4] == to_raw(expected)


@pytest.mark.asyncio
async def test_tick_row_no_activity_bonus_for_dead_group():
    """Zero miners / zero blocks -> activity_mult = 1.0 (no bonus)."""
    db = _make_db(twap=1.0, miners=0, blocks=0)
    cog = _make_cog_instance(db)

    row = _make_row(stake_h=100.0, age_secs=13 * 3600)
    await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    expected = 100.0 * MOON_EMISSION_RATE / 24.0 * 1.0 * 1.0
    args = db.update_wallet_holding.call_args.args
    assert args[4] == to_raw(expected)


@pytest.mark.asyncio
async def test_tick_row_uses_twap_not_spot():
    """A pumped spot price must not leak into the emission valuation.

    TWAP=1.0 (dead token average), spot=10.0 (whale pumped). Emission
    should value the stake at $100 (1.0 * 100), NOT $1000.
    """
    db = _make_db(twap=1.0, spot=10.0, miners=3, blocks=2)
    cog = _make_cog_instance(db)

    row = _make_row(stake_h=100.0, age_secs=13 * 3600)
    await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    # Candles are written as "{SYM}USD" by the drift loop (see
    # cogs/trade.py :: _drift_guild), so the TWAP lookup must use the
    # USD-denominated key. Passing the bare symbol was returning 0 and
    # forcing the spot-price fallback, which defeated the whale-pump
    # guard this test was written to enforce.
    db.get_twap.assert_called_with(f"{SYM}USD", GID, window=MOON_TWAP_WINDOW)
    expected = 100.0 * MOON_EMISSION_RATE / 24.0 * 1.0 * (1 + GROUP_ACTIVITY_BONUS_MAX)
    args = db.update_wallet_holding.call_args.args
    assert args[4] == to_raw(expected)


@pytest.mark.asyncio
async def test_tick_row_respects_per_user_cap():
    """User already minted (cap - 0.5) today. Emission must clip to 0.5 MOON.

    Uses 0.5 (exactly representable in float) rather than 0.1 so the cap
    remainder doesn't pick up IEEE-754 rounding noise when scaled to 1e18.
    """
    db = _make_db(
        twap=1.0, miners=3, blocks=2,
        user_mined=PER_USER_DAILY_MOON_CAP - 0.5,
    )
    cog = _make_cog_instance(db)

    # Large stake so the uncapped emission would blow past the remainder.
    row = _make_row(stake_h=1_000_000.0, age_secs=13 * 3600)
    await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    args = db.update_wallet_holding.call_args.args
    assert args[4] == to_raw(0.5)


@pytest.mark.asyncio
async def test_tick_row_respects_per_guild_cap():
    """Guild already minted (cap - 0.5) today -> emission clipped to 0.5."""
    db = _make_db(
        twap=1.0, miners=3, blocks=2,
        guild_mined=PER_GUILD_DAILY_MOON_CAP - 0.5,
    )
    cog = _make_cog_instance(db)

    row = _make_row(stake_h=1_000_000.0, age_secs=13 * 3600)
    await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    args = db.update_wallet_holding.call_args.args
    assert args[4] == to_raw(0.5)


@pytest.mark.asyncio
async def test_tick_row_respects_max_supply_headroom():
    """headroom=0.01 -> emission clipped to 0.01 regardless of stake size."""
    db = _make_db(twap=1.0, miners=3, blocks=2)
    cog = _make_cog_instance(db)

    row = _make_row(stake_h=1_000_000.0, age_secs=13 * 3600)
    await cog._tick_row(db, GID, row, FIXED_NOW, headroom_h=0.01)

    args = db.update_wallet_holding.call_args.args
    assert args[4] == to_raw(0.01)


@pytest.mark.asyncio
async def test_tick_row_falls_back_to_spot_when_twap_unavailable():
    """TWAP=0 (no candle history) -> fall back to spot price so a freshly
    launched or dormant group token still mints MOON at its seeded
    crypto_prices value. Pump risk is bounded by per-user / per-guild /
    max-supply caps.

    With twap=0 and spot=10.0, $100 stake values at $1000 -> expected
    emission = 1000 * 0.008 / 24 * 1.25 MOON.
    """
    db = _make_db(twap=0.0, spot=10.0, miners=3, blocks=2)
    cog = _make_cog_instance(db)

    row = _make_row(stake_h=100.0, age_secs=13 * 3600)
    await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    expected = 1000.0 * MOON_EMISSION_RATE / 24.0 * 1.0 * (1 + GROUP_ACTIVITY_BONUS_MAX)
    args = db.update_wallet_holding.call_args.args
    assert args[4] == to_raw(expected)


@pytest.mark.asyncio
async def test_tick_row_skips_when_no_price_at_all():
    """TWAP=0 AND get_price returns None -> skip emission, do not credit
    against $0. Only happens for a token with no price row seeded, which
    should be unreachable for a legitimate group token but we still guard."""
    db = _make_db(twap=0.0, miners=3, blocks=2)
    db.get_price = AsyncMock(return_value=None)
    cog = _make_cog_instance(db)

    row = _make_row(stake_h=100.0, age_secs=13 * 3600)
    await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    db.update_wallet_holding.assert_not_called()
    db.execute.assert_not_called()
    db.log_tx.assert_not_called()


@pytest.mark.asyncio
async def test_tick_row_skips_when_stake_is_zero():
    """Empty stake row -> early return, no DB writes."""
    db = _make_db()
    cog = _make_cog_instance(db)

    row = _make_row(stake_h=0.0, age_secs=13 * 3600)
    await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    db.get_twap.assert_not_called()
    db.update_wallet_holding.assert_not_called()


# ── Repo: upsert preserves staked_at (anti-cycling) ──────────────────────────

@pytest.mark.asyncio
async def test_upsert_lunar_stake_preserves_staked_at_via_sql():
    """The upsert SQL must NOT set staked_at on conflict, so a warmed stake
    cannot be reset-and-top-up to game the 12h ramp."""
    from database.moons import PgMoonsRepo

    repo = PgMoonsRepo.__new__(PgMoonsRepo)
    repo.fetch_one = AsyncMock(return_value={
        "user_id": UID, "guild_id": GID, "symbol": SYM, "amount": 10_000,
    })

    await repo.upsert_lunar_stake(UID, GID, SYM, 1_000)

    sql = repo.fetch_one.call_args.args[0]
    # ON CONFLICT DO UPDATE must only touch amount, never staked_at.
    assert "ON CONFLICT" in sql
    assert "staked_at" not in sql.split("ON CONFLICT")[1]
