"""Unit tests for the Moon Pool (Tier 2) distribution path and the
vault-level emission bonus (Slice 3) on `cogs.moons.Moons`.

Slice 2 adds `_tick_distribute_moon_pool(guild)` which drips
`network_vaults.distributable_balance` pro-rata to MOON stakers in DSD on
the Discoin Network (`dsc`). Slice 3 multiplies per-row emission in
`_tick_row` by `(1 + VAULT_LEVEL_EMISSION_BONUS * level)` capped at
`VAULT_LEVEL_EMISSION_BONUS_MAX`.

Both slices land via parallel agents; tests here reference new repo
methods (`get_moon_stakes_for_guild`, `get_moon_pool_total_raw`,
`get_moon_vault_distributable`, `drain_moon_vault_distributable`,
`record_moon_earnings`) that will exist by the time the suite runs.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.framework.scale import to_raw
from constants.moons import (
    MOON_SYMBOL,
    MOON_NETWORK_SHORT,
    MOON_EMISSION_RATE,
    GROUP_ACTIVITY_BONUS_MAX,
    HOURLY_DRIP_FRACTION,
    MOON_POOL_YIELD_BASKET,
    VAULT_LEVEL_EMISSION_BONUS,
    VAULT_LEVEL_EMISSION_BONUS_MAX,
)


# -- Helpers ------------------------------------------------------------------

FIXED_NOW = 1_700_000_000.0
GID = 111_000_000
UID_A = 222_000_001
UID_B = 222_000_002
SYM = "CAT"


class _AsyncNoop:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _make_cog_instance(db: MagicMock):
    """Instantiate Moons without booting the background tick loop.

    Mirrors the helper in `test_moons_tier1.py`: `lunar_tick.start()` needs a
    running loop, so we patch both `start`/`cancel` on the bound task.
    """
    from cogs.moons import Moons
    bot = MagicMock()
    bot.db = db
    bot.wait_until_ready = AsyncMock()
    with patch.object(Moons.lunar_tick, "start"), \
         patch.object(Moons.lunar_tick, "cancel"):
        cog = Moons(bot)
    return cog


def _make_pool_db(
    *,
    distributable: float = 168.0,
    stakes: list[dict] | None = None,
    total_raw: int | None = None,
) -> MagicMock:
    """Build a MagicMock db shaped for the Moon Pool distribution path.

    Defaults seed a 168.0 DSD distributable balance so `drip = 1.0 USD/hour`
    (168 * 1/168 = 1.0 exactly). Stakes default to None so each test can
    supply its own warmup mix.
    """
    db = MagicMock()
    if stakes is None:
        stakes = []
    if total_raw is None:
        total_raw = sum(int(s.get("amount", 0) or 0) for s in stakes)

    db.get_moon_vault_distributable = AsyncMock(return_value=distributable)
    db.get_moon_stakes_for_guild = AsyncMock(return_value=stakes)
    db.get_moon_pool_total_raw = AsyncMock(return_value=total_raw)
    db.drain_moon_vault_distributable = AsyncMock()
    db.update_wallet_holding = AsyncMock()
    db.record_moon_earnings = AsyncMock()
    db.log_tx = AsyncMock()
    db.execute = AsyncMock()
    db.atomic = lambda: _AsyncNoop()
    # Every basket symbol prices at $1 so per-slot USD and per-slot token
    # amounts coincide. Tests assert on the USD totals not per-symbol math.
    db.get_price = AsyncMock(return_value={"price": 1.0})
    return db


def _moon_stake_row(uid: int, amount_h: float, *, age_secs: float) -> dict:
    """A moon_stakes row with ``amount_h`` MOON (scaled raw) staked
    ``age_secs`` ago."""
    return {
        "user_id": uid,
        "amount": to_raw(amount_h),
        "staked_at": FIXED_NOW - age_secs,
    }


def _make_tick_db(
    *,
    twap: float = 1.0,
    miners: int = 3,
    blocks: int = 2,
    vault_level: int | None = 0,
) -> MagicMock:
    """MagicMock db for Slice 3 `_tick_row` vault-level bonus tests.

    `vault_level=None` simulates a guild with no network_vaults row.
    """
    db = MagicMock()
    db.get_twap = AsyncMock(return_value=(twap, 0.0))
    db.get_group_activity_for_token = AsyncMock(return_value=(miners, blocks))
    db.get_user_moon_minted_recent = AsyncMock(return_value=0.0)
    db.get_guild_moon_minted_recent = AsyncMock(return_value=0.0)
    db.update_wallet_holding = AsyncMock()
    db.execute = AsyncMock()
    db.record_lunar_earnings = AsyncMock()
    db.log_tx = AsyncMock()
    db.atomic = lambda: _AsyncNoop()

    if vault_level is None:
        db.fetch_one = AsyncMock(return_value=None)
    else:
        db.fetch_one = AsyncMock(return_value={"level": vault_level})
    return db


def _tick_row(stake_h: float, *, age_secs: float) -> dict:
    """Lunar_stakes row for Slice 3 tests (note: Slice 3 runs inside the
    existing `_tick_row`, which reads `lunar_stakes` rows)."""
    return {
        "user_id": UID_A,
        "symbol": SYM,
        "amount": to_raw(stake_h),
        "staked_at": FIXED_NOW - age_secs,
    }


# -- A. Moon Pool distribution ------------------------------------------------

@pytest.mark.asyncio
async def test_pool_drips_pro_rata_to_stakers():
    """Distributable sized so this tick drips exactly 1.0 USD, split 1:3 across
    two stakers, paid as a basket of MTA / ARC / DSC / SUN (each $1 in the
    mock). Asserts behaviour: every staker receives one credit per basket
    symbol; per-staker USD total matches pro-rata share; vault drains by the
    full drip.
    """
    stakes = [
        _moon_stake_row(UID_A, 100.0, age_secs=13 * 3600),
        _moon_stake_row(UID_B, 300.0, age_secs=13 * 3600),
    ]
    db = _make_pool_db(distributable=1.0 / HOURLY_DRIP_FRACTION, stakes=stakes)
    cog = _make_cog_instance(db)

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_distribute_moon_pool(MagicMock(id=GID))

    basket_size = len(MOON_POOL_YIELD_BASKET)
    expected_symbols = {sym for sym, _ in MOON_POOL_YIELD_BASKET}

    # Each staker gets one credit per basket slot.
    assert db.update_wallet_holding.await_count == basket_size * 2

    per_user_totals_raw: dict[int, int] = {}
    per_user_symbols: dict[int, set[str]] = {}
    for call in db.update_wallet_holding.await_args_list:
        uid, gid_arg, net_short, sym, delta_raw = call.args
        assert gid_arg == GID
        assert sym in expected_symbols
        per_user_totals_raw[uid] = per_user_totals_raw.get(uid, 0) + delta_raw
        per_user_symbols.setdefault(uid, set()).add(sym)

    # Each user received every basket symbol exactly once.
    for uid in (UID_A, UID_B):
        assert per_user_symbols[uid] == expected_symbols

    # Pro-rata USD totals (each $1 mock price collapses raw == USD-raw).
    assert abs(per_user_totals_raw[UID_A] - to_raw(0.25)) <= basket_size
    assert abs(per_user_totals_raw[UID_B] - to_raw(0.75)) <= basket_size

    # Vault drained once, by the full USD drip value.
    db.drain_moon_vault_distributable.assert_awaited_once()
    drained = db.drain_moon_vault_distributable.await_args.args[1]
    assert abs(drained - 1.0) <= 1e-9

    # Each credit generates its own MOON_POOL_YIELD tx.
    assert db.log_tx.await_count == basket_size * 2
    for call in db.log_tx.await_args_list:
        assert call.args[2] == "MOON_POOL_YIELD"


@pytest.mark.asyncio
async def test_pool_respects_warmup():
    """Half-warmed staker gets half its pro-rata share; fully-warmed gets all.
    6h old => warmup 0.5, 13h old => warmup 1.0. Drain totals the actually-
    paid USD, not the full drip, because the unwarmed slice is left in the
    vault for next tick.
    """
    stakes = [
        _moon_stake_row(UID_A, 100.0, age_secs=6 * 3600),    # warmup 0.5
        _moon_stake_row(UID_B, 300.0, age_secs=13 * 3600),   # warmup 1.0
    ]
    db = _make_pool_db(distributable=1.0 / HOURLY_DRIP_FRACTION, stakes=stakes)
    cog = _make_cog_instance(db)

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_distribute_moon_pool(MagicMock(id=GID))

    basket_size = len(MOON_POOL_YIELD_BASKET)

    # Sum raw credits per user across every basket symbol.
    per_user_totals_raw: dict[int, int] = {}
    for call in db.update_wallet_holding.await_args_list:
        uid = call.args[0]
        per_user_totals_raw[uid] = per_user_totals_raw.get(uid, 0) + call.args[4]

    # Half-warmed A: 0.125 USD. Full-warmed B: 0.75 USD. Basket prices are $1
    # so the USD total collapses onto the raw-credit total.
    assert abs(per_user_totals_raw[UID_A] - to_raw(0.125)) <= basket_size
    assert abs(per_user_totals_raw[UID_B] - to_raw(0.75)) <= basket_size

    # Drain matches actual USD payout sum (0.875), not the full 1.0 drip.
    drained = db.drain_moon_vault_distributable.await_args.args[1]
    assert abs(drained - 0.875) <= 1e-9


@pytest.mark.asyncio
async def test_pool_skips_when_distributable_is_zero():
    """Nothing to drip -> no wallet credits, no drain call."""
    stakes = [_moon_stake_row(UID_A, 100.0, age_secs=13 * 3600)]
    db = _make_pool_db(distributable=0.0, stakes=stakes)
    cog = _make_cog_instance(db)

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_distribute_moon_pool(MagicMock(id=GID))

    db.update_wallet_holding.assert_not_called()
    db.drain_moon_vault_distributable.assert_not_called()
    db.log_tx.assert_not_called()


@pytest.mark.asyncio
async def test_pool_skips_when_no_stakers():
    """No stakers -> vault distributable left untouched, no credits."""
    db = _make_pool_db(distributable=168.0, stakes=[])
    cog = _make_cog_instance(db)

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_distribute_moon_pool(MagicMock(id=GID))

    db.update_wallet_holding.assert_not_called()
    db.drain_moon_vault_distributable.assert_not_called()
    db.log_tx.assert_not_called()


@pytest.mark.asyncio
async def test_pool_skips_basket_symbol_without_price():
    """If a basket symbol has no live price this tick, it's dropped from the
    payout and its USD share is redistributed across the remaining symbols.
    The USD total paid to each staker still equals their pro-rata slice of
    the drip; only the per-symbol split narrows.
    """
    stakes = [_moon_stake_row(UID_A, 100.0, age_secs=13 * 3600)]
    db = _make_pool_db(distributable=1.0 / HOURLY_DRIP_FRACTION, stakes=stakes)

    # Mock get_price to return a live $1 price for every basket symbol EXCEPT
    # the first one (which has price=0 so it's dropped from the basket).
    dead_symbol = MOON_POOL_YIELD_BASKET[0][0]

    async def _price(sym, gid):
        if sym == dead_symbol:
            return {"price": 0.0}
        return {"price": 1.0}

    db.get_price = AsyncMock(side_effect=_price)
    cog = _make_cog_instance(db)

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_distribute_moon_pool(MagicMock(id=GID))

    paid_symbols = {call.args[3] for call in db.update_wallet_holding.await_args_list}
    assert dead_symbol not in paid_symbols
    # The other basket symbols still got paid.
    assert paid_symbols == {sym for sym, _ in MOON_POOL_YIELD_BASKET[1:]}

    # User still received the full drip (1.0 USD) since only the split changed.
    # Tolerance accounts for float64 representation of non-terminating fractions
    # like 1/3 when the basket shrinks from 4 to 3 legs: each to_raw call can
    # shed up to ~300 raw units, times basket_size legs.
    total_raw = sum(call.args[4] for call in db.update_wallet_holding.await_args_list)
    assert abs(total_raw - to_raw(1.0)) <= len(MOON_POOL_YIELD_BASKET) * 500


# -- B. Vault-level emission bonus in _tick_row (Slice 3) ---------------------

@pytest.mark.asyncio
async def test_tick_row_vault_level_zero_is_baseline():
    """Level 0 (or missing) -> multiplier 1.0. Slice 3 must not change the
    Slice 1 baseline for fresh guilds."""
    db = _make_tick_db(vault_level=0)
    cog = _make_cog_instance(db)

    row = _tick_row(stake_h=100.0, age_secs=13 * 3600)

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    baseline = 100.0 * MOON_EMISSION_RATE / 24.0 * 1.0 * (1 + GROUP_ACTIVITY_BONUS_MAX)
    args = db.update_wallet_holding.call_args.args
    assert args[4] == to_raw(baseline)


@pytest.mark.asyncio
async def test_tick_row_vault_level_5_applies_10_percent_bonus():
    """Level 5 -> multiplier 1 + 0.02*5 = 1.10."""
    db = _make_tick_db(vault_level=5)
    cog = _make_cog_instance(db)

    row = _tick_row(stake_h=100.0, age_secs=13 * 3600)

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    baseline = 100.0 * MOON_EMISSION_RATE / 24.0 * 1.0 * (1 + GROUP_ACTIVITY_BONUS_MAX)
    multiplier = 1.0 + VAULT_LEVEL_EMISSION_BONUS * 5
    expected = baseline * multiplier
    args = db.update_wallet_holding.call_args.args
    assert abs(args[4] - to_raw(expected)) <= 1


@pytest.mark.asyncio
async def test_tick_row_vault_level_bonus_clamped_at_max():
    """A level well past the linear ceiling must clamp at
    VAULT_LEVEL_EMISSION_BONUS_MAX so runaway mature vaults cannot blow past
    the emission budget. Pick a level that exceeds the clamp regardless of
    how the per-level rate is tuned."""
    # Enough levels to overshoot the clamp even if VAULT_LEVEL_EMISSION_BONUS
    # is tuned down in the future.
    level = int(VAULT_LEVEL_EMISSION_BONUS_MAX / VAULT_LEVEL_EMISSION_BONUS) + 5
    db = _make_tick_db(vault_level=level)
    cog = _make_cog_instance(db)

    row = _tick_row(stake_h=100.0, age_secs=13 * 3600)

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    baseline = 100.0 * MOON_EMISSION_RATE / 24.0 * 1.0 * (1 + GROUP_ACTIVITY_BONUS_MAX)
    multiplier = 1.0 + min(
        VAULT_LEVEL_EMISSION_BONUS_MAX,
        VAULT_LEVEL_EMISSION_BONUS * level,
    )
    assert multiplier == pytest.approx(1.0 + VAULT_LEVEL_EMISSION_BONUS_MAX)
    expected = baseline * multiplier
    args = db.update_wallet_holding.call_args.args
    assert abs(args[4] - to_raw(expected)) <= 1


# -- C. Autocompound (Lunar Mint MOON -> Moon Pool on the same tick) ---------


def _dispatch_fetch_one(vault_level: int, autocompound: bool):
    """fetch_one side_effect that routes the autocompound query to the
    configured bool and every other query to the vault-level row the
    existing _tick_row tests expect.
    """
    async def _side_effect(sql, *args):
        if "moon_autocompound" in sql:
            return {"moon_autocompound": autocompound} if autocompound else None
        return {"level": vault_level}
    return _side_effect


@pytest.mark.asyncio
async def test_tick_row_autocompound_off_credits_wallet():
    """Baseline: autocompound off => earned MOON lands in wallet via
    update_wallet_holding, no stake mutation."""
    db = _make_tick_db(vault_level=0)
    db.fetch_one = AsyncMock(side_effect=_dispatch_fetch_one(0, False))
    db.upsert_moon_stake = AsyncMock()
    cog = _make_cog_instance(db)

    row = _tick_row(stake_h=100.0, age_secs=13 * 3600)
    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await cog._tick_row(db, GID, row, FIXED_NOW, 1e18)

    db.update_wallet_holding.assert_awaited_once()
    wargs = db.update_wallet_holding.call_args.args
    assert wargs[2] == MOON_NETWORK_SHORT and wargs[3] == MOON_SYMBOL
    db.upsert_moon_stake.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_row_autocompound_on_upserts_stake():
    """Autocompound on => earned MOON goes straight into moon_stakes via
    upsert_moon_stake, wallet is untouched. The delta upserted must match
    what the wallet path would have credited so users see identical totals
    regardless of toggle position."""
    # Baseline path: capture what a no-autocompound tick credits.
    baseline_db = _make_tick_db(vault_level=0)
    baseline_db.fetch_one = AsyncMock(side_effect=_dispatch_fetch_one(0, False))
    baseline_db.upsert_moon_stake = AsyncMock()
    baseline_cog = _make_cog_instance(baseline_db)

    row = _tick_row(stake_h=100.0, age_secs=13 * 3600)
    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await baseline_cog._tick_row(baseline_db, GID, row, FIXED_NOW, 1e18)
    expected_raw = baseline_db.update_wallet_holding.call_args.args[4]

    # Autocompound path: same inputs, flag on.
    ac_db = _make_tick_db(vault_level=0)
    ac_db.fetch_one = AsyncMock(side_effect=_dispatch_fetch_one(0, True))
    ac_db.upsert_moon_stake = AsyncMock()
    ac_cog = _make_cog_instance(ac_db)

    with patch("cogs.moons.time.time", return_value=FIXED_NOW):
        await ac_cog._tick_row(ac_db, GID, row, FIXED_NOW, 1e18)

    ac_db.update_wallet_holding.assert_not_awaited()
    ac_db.upsert_moon_stake.assert_awaited_once()
    ac_delta = ac_db.upsert_moon_stake.call_args.args[2]
    assert ac_delta == expected_raw, (
        f"autocompound delta {ac_delta} diverges from wallet path {expected_raw}"
    )

    # MOON circulating supply is still incremented in both paths so the
    # autocompound toggle doesn't silently stop MOON inflation accounting.
    for db in (baseline_db, ac_db):
        supply_updates = [
            c for c in db.execute.await_args_list
            if "circulating_supply" in c.args[0] and "MOON" in c.args[0]
        ]
        assert supply_updates, "MOON circulating_supply update missing"


# -- D. ,moon burn rounding + dust ------------------------------------------


def _make_burn_ctx(
    *,
    moon_price: float,
    moon_balance_h: float,
    group_tokens: dict[str, float],
) -> MagicMock:
    """Build a mock DiscoContext for moon_burn. ``group_tokens`` maps
    symbol -> price. Each symbol gets a priced row and a guild_tokens-style
    meta entry marked as token_type='group' on Moon Network."""
    ctx = MagicMock()
    ctx.author = MagicMock(id=UID_A, bot=False, display_name="tester")
    ctx.guild = MagicMock(id=GID)
    ctx.guild_id = GID
    ctx.prefix = "."

    db = MagicMock()
    db.atomic = lambda: _AsyncNoop()
    # ensure_registered middleware calls this before the command body runs.
    db.ensure_user = AsyncMock(return_value={"user_id": UID_A, "guild_id": GID})

    db.get_wallet_holding = AsyncMock(return_value={"amount": to_raw(moon_balance_h)})

    async def _price(sym, gid):
        if sym == MOON_SYMBOL:
            return {"price": moon_price}
        if sym in group_tokens:
            return {"price": group_tokens[sym]}
        return None

    db.get_price = AsyncMock(side_effect=_price)

    tokens_meta = {
        sym: {"token_type": "group", "network": "Moon Network", "emoji": ""}
        for sym in group_tokens
    }
    db.get_all_tokens_for_guild = AsyncMock(return_value=tokens_meta)
    db.update_wallet_holding = AsyncMock()
    db.update_price = AsyncMock()
    db.add_trade_volume = AsyncMock()
    db.execute = AsyncMock()
    db.log_tx = AsyncMock()
    ctx.db = db

    ctx.bot = MagicMock()
    ctx.bot.bus = MagicMock()
    ctx.bot.bus.publish = AsyncMock()

    ctx.reply = AsyncMock()
    ctx.reply_error = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_burn_rounding_stability_many_small_vs_one_large():
    """Burning 100 MOON in one shot versus 100 x 1 MOON must produce the
    same total token credits per group symbol within a small per-burn
    rounding tolerance. Probes whether dust drops silently at small slice
    sizes."""
    from cogs.moons import Moons

    group_tokens = {"CAT": 2.0, "COOK": 5.0, "FEM": 10.0}

    # One 100-MOON burn.
    ctx_big = _make_burn_ctx(
        moon_price=1.0, moon_balance_h=100.0, group_tokens=group_tokens,
    )
    cog = _make_cog_instance(MagicMock())
    cog.bot = MagicMock()
    await Moons.moon_burn.callback(cog, ctx_big, "100")

    big_credits: dict[str, int] = {}
    for call in ctx_big.db.update_wallet_holding.await_args_list:
        sym = call.args[3]
        if sym == MOON_SYMBOL:
            continue
        big_credits[sym] = big_credits.get(sym, 0) + call.args[4]

    # 100 consecutive 1-MOON burns. Rebalance the wallet mock between burns so
    # the deduction check keeps passing.
    small_credits: dict[str, int] = {}
    remaining_h = 100.0
    for _ in range(100):
        ctx_small = _make_burn_ctx(
            moon_price=1.0, moon_balance_h=remaining_h,
            group_tokens=group_tokens,
        )
        await Moons.moon_burn.callback(cog, ctx_small, "1")
        remaining_h -= 1.0
        for call in ctx_small.db.update_wallet_holding.await_args_list:
            sym = call.args[3]
            if sym == MOON_SYMBOL:
                continue
            small_credits[sym] = small_credits.get(sym, 0) + call.args[4]

    assert set(big_credits.keys()) == set(small_credits.keys())
    # AMM slippage now applies to ,moon burn (sell-side on MOON + buy-side on
    # each group token), so a single big burn intentionally gets a worse fill
    # than 100 small ones -- that's the whole point of the price-impact
    # rework. Bound the drift to "still roughly the same ballpark" (under 1%
    # of credited total), which keeps the rounding/dust regression catch
    # while allowing the deliberate slippage delta. A real silent-loss bug
    # (percent-level dust drop) would still trip the bound easily.
    for sym, big_total in big_credits.items():
        drift = abs(small_credits[sym] - big_total)
        assert drift <= big_total * 0.01, (
            f"{sym}: small-burns total {small_credits[sym]} "
            f"vs big burn {big_total} drifted {drift} raw "
            f"(> 1% of {big_total})"
        )
        # Small burns must end up with at least as much as the big burn,
        # because individually their per-trade impact is ~100x smaller.
        assert small_credits[sym] >= big_total, (
            f"{sym}: small burns ({small_credits[sym]}) should beat or match "
            f"a single big burn ({big_total}) -- AMM slippage scales with "
            f"trade size, so sequential small burns ought to win."
        )


@pytest.mark.asyncio
async def test_burn_dust_slice_drops_cleanly_on_extreme_price():
    """A group token priced at $1M receives a raw=0 slice when the per-
    slot USD is tiny. The burn must drop that slot (no zero-amount credit,
    no crash) and still succeed on the other slots."""
    from cogs.moons import Moons

    # $0.01 burn split 2 ways = $0.005 per slot.
    #   WHALE @ $1,000,000: 0.005 / 1_000_000 = 5e-9 -> to_raw(5e-9) == 5 raw
    #     (actually non-zero at 18 dec, so pick a bigger price).
    #   Use $1e20 so to_raw(0.005 / 1e20) truncates to 0.
    group_tokens = {"CAT": 1.0, "WHALE": 1e20}
    ctx = _make_burn_ctx(
        moon_price=1.0, moon_balance_h=0.01,
        group_tokens=group_tokens,
    )
    cog = _make_cog_instance(MagicMock())
    cog.bot = MagicMock()

    await Moons.moon_burn.callback(cog, ctx, "0.01")

    ctx.reply_error.assert_not_awaited()  # didn't bail out
    ctx.reply.assert_awaited_once()       # success path

    credited_syms = {
        call.args[3]
        for call in ctx.db.update_wallet_holding.await_args_list
        if call.args[3] != MOON_SYMBOL
    }
    assert "CAT" in credited_syms
    assert "WHALE" not in credited_syms, (
        "dust slice rounded to zero should be dropped, not credited as 0"
    )
