"""Tests for the staking heat system, LP time-lock tiers, and user-created-
token LP bonus -- the three mechanics added on the claude/general-improvements
branch. Kept in one file because they cover a coherent economy slice.

All tests are pure (no DB, no bot) and exercise the helpers directly so
regressions show up fast without a Postgres container.
"""
from __future__ import annotations

import random

from configs.buddies_config import (
    RARITY_ROLL_WEIGHTS,
    RARITY_TIERS,
    roll_rarity,
)
from core.config import Config
from cogs.stake import (
    _advance_heat,
    _format_heat,
    _roll_validator_event,
    _HEAT_DELTA_HOT,
    _HEAT_DELTA_COLD,
    _HEAT_REWARD_TILT,
    _STAKE_HOT_MULT,
)
from services.liquidity import (
    user_lp_work_bonus_pct,
)


# =============================================================================
# Validator heat
# =============================================================================

class TestValidatorHeat:
    def test_neutral_stays_neutral_without_event(self) -> None:
        assert _advance_heat(0.0, None) == 0.0

    def test_hot_event_pushes_heat_up(self) -> None:
        assert _advance_heat(0.0, "HOT") == _HEAT_DELTA_HOT

    def test_cold_event_pushes_heat_down(self) -> None:
        assert _advance_heat(0.0, "COLD") == _HEAT_DELTA_COLD

    def test_consecutive_hots_cap_at_positive_one(self) -> None:
        h = 0.0
        for _ in range(30):
            h = _advance_heat(h, "HOT")
        assert h == 1.0

    def test_consecutive_colds_cap_at_negative_one(self) -> None:
        h = 0.0
        for _ in range(30):
            h = _advance_heat(h, "COLD")
        assert h == -1.0

    def test_decay_monotonic_toward_zero_from_positive(self) -> None:
        h = 1.0
        prev = h
        for _ in range(10):
            h = _advance_heat(h, None)
            assert 0.0 <= h < prev
            prev = h

    def test_decay_monotonic_toward_zero_from_negative(self) -> None:
        h = -1.0
        prev = h
        for _ in range(10):
            h = _advance_heat(h, None)
            assert prev < h <= 0.0
            prev = h

    def test_decay_half_life_around_eight_ticks(self) -> None:
        # 0.92^8 is ~0.51 -- heat should roughly halve in 8 ticks.
        h = 1.0
        for _ in range(8):
            h = _advance_heat(h, None)
        assert 0.45 <= h <= 0.55

    def test_reward_tilt_bounds_match_config(self) -> None:
        # At heat=+1, tilt is +_HEAT_REWARD_TILT. At heat=-1, it's the mirror.
        tilt_hi = 1.0 + 1.0 * _HEAT_REWARD_TILT
        tilt_lo = 1.0 + -1.0 * _HEAT_REWARD_TILT
        assert tilt_hi == 1.0 + _HEAT_REWARD_TILT
        assert tilt_lo == 1.0 - _HEAT_REWARD_TILT
        # Sanity: a HOT tick on a fully hot validator clears the stated
        # "2x * 1.15 = 2.3x" design ceiling.
        assert abs(_STAKE_HOT_MULT * tilt_hi - 2.30) < 1e-9

    def test_format_heat_returns_string_with_bar(self) -> None:
        for val in (-1.0, -0.5, 0.0, 0.5, 1.0):
            s = _format_heat(val)
            assert isinstance(s, str) and "■" in s
            assert f"{val:+.2f}" in s

    def test_roll_event_distribution_matches_config(self) -> None:
        rng = random.Random(12345)
        # Patch module-level RNG for this test only.
        import cogs.stake as _stake
        _orig = _stake.random
        _stake.random = rng
        try:
            buckets = {"HOT": 0, "COLD": 0, None: 0}
            for _ in range(50_000):
                _, tag = _roll_validator_event()
                buckets[tag] += 1
        finally:
            _stake.random = _orig
        # ~5% each, ~90% normal. Allow 2% wiggle for RNG variance at 50k samples.
        assert 0.030 < buckets["HOT"] / 50_000 < 0.070
        assert 0.030 < buckets["COLD"] / 50_000 < 0.070
        assert 0.870 < buckets[None] / 50_000 < 0.930


# =============================================================================
# User-created-token LP work/daily bonus
# =============================================================================

class TestUserLpWorkBonus:
    def test_zero_exposure_gives_zero_bonus(self) -> None:
        assert user_lp_work_bonus_pct(0.0) == 0.0
        assert user_lp_work_bonus_pct(-100.0) == 0.0  # guard against negatives

    def test_linear_ramp_below_cap(self) -> None:
        # With default PER_USD=1e-5, $1000 LP == +1% bonus.
        bonus = user_lp_work_bonus_pct(1_000.0)
        assert abs(bonus - 0.01) < 1e-9

    def test_cap_enforced(self) -> None:
        big = user_lp_work_bonus_pct(1_000_000.0)
        assert big == Config.USER_LP_WORK_BONUS_CAP

    def test_cap_matches_scale(self) -> None:
        # At the saturation threshold exactly, bonus == cap.
        threshold = Config.USER_LP_WORK_BONUS_CAP / Config.USER_LP_WORK_BONUS_PER_USD
        assert user_lp_work_bonus_pct(threshold) == Config.USER_LP_WORK_BONUS_CAP
        # Just below, bonus < cap.
        assert user_lp_work_bonus_pct(threshold - 100) < Config.USER_LP_WORK_BONUS_CAP


# =============================================================================
# LP lock tier config
# =============================================================================

class TestLpLockTiers:
    def test_three_tiers_defined(self) -> None:
        assert set(Config.LP_LOCK_TIERS.keys()) == {1, 2, 3}

    def test_tier_durations_monotonic(self) -> None:
        days = [Config.LP_LOCK_TIERS[t]["days"] for t in (1, 2, 3)]
        assert days == sorted(days)

    def test_tier_xp_multipliers_monotonic(self) -> None:
        mults = [Config.LP_LOCK_TIERS[t]["xp_mult"] for t in (1, 2, 3)]
        assert mults == sorted(mults)
        # All tier multipliers must improve over the baseline.
        assert all(m > 1.0 for m in mults)

    def test_early_unlock_burn_within_sane_bounds(self) -> None:
        assert 0 < Config.LP_EARLY_UNLOCK_BURN < 1

    def test_stacking_90d_plus_user_token_matches_design(self) -> None:
        # 4.0x lock * 1.3x user-token = 5.2x -- the stated degen ceiling.
        top_lock = Config.LP_LOCK_TIERS[3]["xp_mult"]
        stack = top_lock * Config.USER_LP_LIQSTONE_MULT
        assert abs(stack - 5.2) < 1e-6


# =============================================================================
# Buddy rarity decoupling (regression guards for the rarity rework)
# =============================================================================

class TestBuddyRarityDecoupling:
    def test_species_no_longer_carry_rarity(self) -> None:
        from configs.buddies_config import SPECIES
        for name, meta in SPECIES.items():
            assert "rarity" not in meta, f"{name} still has a species-level rarity"

    def test_rarity_of_is_deleted(self) -> None:
        import configs.buddies_config as bc
        assert not hasattr(bc, "rarity_of"), "rarity_of should be removed"

    def test_roll_rarity_returns_valid_tier(self) -> None:
        for _ in range(200):
            tier = roll_rarity()
            assert tier in RARITY_TIERS

    def test_rarity_weights_sum_positive(self) -> None:
        assert sum(RARITY_ROLL_WEIGHTS.values()) > 0

    def test_all_tiers_have_ability_mult(self) -> None:
        for tier, meta in RARITY_TIERS.items():
            assert "ability_mult" in meta, f"tier {tier} missing ability_mult"
            assert meta["ability_mult"] >= 1.0


# =============================================================================
# Job-tier rename + dead-perk removal
# =============================================================================

class TestJobTierCleanup:
    def test_validator_op_renamed_to_liquidity_baron(self) -> None:
        assert Config.JOBS["VALIDATOR_OP"]["title"] == "Liquidity Baron"

    def test_dead_validator_perk_removed(self) -> None:
        # can_deploy_validator was display-only; it's gone now.
        for tier_id, cfg in Config.JOBS.items():
            assert "can_deploy_validator" not in cfg.get("perks", {}), (
                f"{tier_id} still carries the dead can_deploy_validator perk"
            )

    def test_real_perks_retained(self) -> None:
        # can_deploy_token (Protocol Dev / Exploiter) and can_create_pool
        # (Exploiter) ARE checked in gameplay -- they must stay.
        assert Config.JOBS["PROTOCOL_DEV"]["perks"].get("can_deploy_token") is True
        assert Config.JOBS["EXPLOITER"]["perks"].get("can_deploy_token") is True
        assert Config.JOBS["EXPLOITER"]["perks"].get("can_create_pool") is True


# =============================================================================
# Dead-stat removal guard
# =============================================================================

class TestDeadStatsRemoved:
    def test_gamble_edge_reduc_not_on_any_item(self) -> None:
        for key, cfg in Config.SHOP_ITEMS.items():
            stats = cfg.get("stats", {})
            assert "gamble_edge_reduc" not in stats, (
                f"shop item {key} still carries the dead gamble_edge_reduc stat"
            )


# =============================================================================
# Bot startup + pool-seeding defaults
# =============================================================================

class TestPoolSeedingDefaults:
    def test_auto_seed_pools_defaults_true(self) -> None:
        # Without an env override, the bot must seed pools on every startup
        # so new networks (Arcadia tokens, group-token additions, etc.) don't
        # wait on an admin flipping a .env flag to become swappable.
        import os
        # Read effective value only when the env var isn't forcing it -- a
        # test environment might have legitimately set it to False.
        if "AUTO_SEED_POOLS" in os.environ:
            return  # skip -- env override makes this test non-deterministic
        assert Config.AUTO_SEED_POOLS is True

    def test_group_token_genesis_seed_positive(self) -> None:
        # A group-token genesis seed of 0 would leave the pool empty and
        # regression back to the original "no liquidity" bug.
        assert Config.GROUP_TOKEN_GENESIS_SEED_USD > 0

    def test_group_vault_pool_seed_meaningful(self) -> None:
        # Regression guard: create_vault_pool used to seed dust (~42 cents).
        # The new config must keep the pool readable as real liquidity.
        assert Config.GROUP_VAULT_POOL_SEED_USD >= 1_000


# =============================================================================
# Moon Network wrapped coins (MMTA / MSUN)
# =============================================================================

class TestMoonWrappedCoins:
    def test_wrapped_coin_map_symmetric(self) -> None:
        from constants.moons import (
            WRAPPED_FOR_NATIVE,
            NATIVE_FOR_WRAPPED,
            wrapped_coin,
            native_coin_for_wrapped,
        )
        # Every native has a wrapper and the inverse maps round-trip.
        assert WRAPPED_FOR_NATIVE == {"MTA": "MMTA", "SUN": "MSUN"}
        for native, wrapped in WRAPPED_FOR_NATIVE.items():
            assert wrapped_coin(native) == wrapped
            assert native_coin_for_wrapped(wrapped) == native
            assert NATIVE_FOR_WRAPPED[wrapped] == native

    def test_wrapped_coins_registered(self) -> None:
        # Each wrapped symbol must exist in Config.TOKENS, sit on Moon
        # Network, and NOT be earn-only or USD-buyable -- acquisition goes
        # exclusively through the .moon wrap command.
        for wrapped in ("MMTA", "MSUN"):
            assert wrapped in Config.TOKENS, f"{wrapped} missing from Config.TOKENS"
            meta = Config.TOKENS[wrapped]
            assert meta["network"] == "Moon Network"
            assert meta.get("stakeable") is False
            assert meta.get("mineable") is False
            assert wrapped not in Config.EARN_ONLY_TOKENS
            assert wrapped not in Config.BUYABLE_WITH_USD

    def test_wrapped_price_mirrors_native_at_genesis(self) -> None:
        # Not a hard peg, but the genesis price of each wrapper should
        # match its underlying so the first trades don't instantly
        # arbitrage to zero.
        assert Config.TOKENS["MMTA"]["start_price"] == Config.TOKENS["MTA"]["start_price"]
        assert Config.TOKENS["MSUN"]["start_price"] == Config.TOKENS["SUN"]["start_price"]

    def test_wrapped_coins_have_no_burn(self) -> None:
        # Any burn on a 1:1 wrapper breaks the peg -- wrap/unwrap would
        # silently drain user balances. Guard against regressions.
        assert Config.TOKENS["MMTA"]["burn_rate"] == 0.0
        assert Config.TOKENS["MSUN"]["burn_rate"] == 0.0

    def test_wrapped_coins_anchored_to_native(self) -> None:
        # Each wrapper declares peg_to + peg_band so the price-drift tick
        # clamps it near the underlying. Without these fields the wrapper
        # would drift independently via GBM and eventually desync entirely
        # from its native counterpart.
        for wrapped, native in (("MMTA", "MTA"), ("MSUN", "SUN")):
            meta = Config.TOKENS[wrapped]
            assert meta.get("peg_to") == native, (
                f"{wrapped} must declare peg_to={native!r} so the oracle anchors it"
            )
            band = float(meta.get("peg_band") or 0)
            assert 0 < band <= 0.05, (
                f"{wrapped} peg_band must be a small positive fraction (got {band})"
            )

    def test_wrapped_coins_low_native_vol(self) -> None:
        # daily_vol still matters because gbm_step runs BEFORE the peg
        # clamp. Keeping it low means the wrapper's chart is smooth inside
        # the band instead of sawtoothing between the clamp edges.
        assert Config.TOKENS["MMTA"]["daily_vol"] <= 0.02
        assert Config.TOKENS["MSUN"]["daily_vol"] <= 0.02
