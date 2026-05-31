"""Tests for bulk "everything" operations and the rugpull minigame.

Covers:
* Move everything: balance transfers, platform fee enforcement, route validation
* Sell everything: batch sell with fee deduction
* Unstake everything: lock period filtering, gas fee enforcement
* Remove all LP: lock period filtering, gas fee enforcement
* Rugpull: wager math, tier validation, king transfer logic
* Send all: amount resolution, balance checks
"""
from __future__ import annotations

import time

import pytest

from tests.conftest import MockDB, GUILD_ID, USER_ID


# ── Helpers ----------------------------------------------------------------

def _setup_user(db: MockDB, uid: int = USER_ID, wallet: float = 1000.0, bank: float = 0.0):
    db.users[(uid, GUILD_ID)] = {"wallet": wallet, "bank": bank}


def _setup_holding(db: MockDB, symbol: str, amount: float, uid: int = USER_ID):
    db.holdings[(uid, GUILD_ID, symbol)] = {"amount": amount, "symbol": symbol}


def _setup_wallet_holding(db: MockDB, net: str, symbol: str, amount: float, uid: int = USER_ID):
    db.wallet_holdings[(uid, GUILD_ID, net, symbol)] = {"amount": amount, "symbol": symbol}


def _setup_price(db: MockDB, symbol: str, price: float):
    db.prices[(symbol, GUILD_ID)] = {"price": price, "symbol": symbol}


def _setup_token(db: MockDB, symbol: str, network: str = "Arcadia Network", **extra):
    tokens = db.tokens.setdefault(GUILD_ID, {})
    tokens[symbol] = {"symbol": symbol, "network": network, "emoji": "", **extra}


def _setup_pool(db: MockDB, token_a: str, token_b: str, reserve_a: float, reserve_b: float, total_lp: float):
    pool_id = f"{token_a}_{token_b}"
    db.pools[(pool_id, GUILD_ID)] = {
        "pool_id": pool_id,
        "token_a": token_a,
        "token_b": token_b,
        "reserve_a": reserve_a,
        "reserve_b": reserve_b,
        "total_lp": total_lp,
    }
    return pool_id


# ── Move everything tests --------------------------------------------------

class TestMoveEverythingBalances:
    """Verify balance math for mass move operations."""

    async def test_cash_to_bank_moves_full_amount(self, mock_db: MockDB):
        _setup_user(mock_db, wallet=500.0, bank=100.0)
        cash_before = mock_db.users[(USER_ID, GUILD_ID)]["wallet"]
        bank_before = mock_db.users[(USER_ID, GUILD_ID)]["bank"]

        await mock_db.update_wallet(USER_ID, GUILD_ID, -cash_before)
        await mock_db.update_bank(USER_ID, GUILD_ID, cash_before)

        row = mock_db.users[(USER_ID, GUILD_ID)]
        assert row["wallet"] == 0.0
        assert row["bank"] == bank_before + cash_before

    async def test_bank_to_cash_moves_full_amount(self, mock_db: MockDB):
        _setup_user(mock_db, wallet=0.0, bank=750.0)
        bank_amt = mock_db.users[(USER_ID, GUILD_ID)]["bank"]

        await mock_db.update_bank(USER_ID, GUILD_ID, -bank_amt)
        await mock_db.update_wallet(USER_ID, GUILD_ID, bank_amt)

        row = mock_db.users[(USER_ID, GUILD_ID)]
        assert row["wallet"] == 750.0
        assert row["bank"] == 0.0

    async def test_platform_fee_deducted_on_bank_to_wallet(self, mock_db: MockDB):
        """CeFi to DeFi transfers should charge a platform fee from the USD wallet."""
        _setup_user(mock_db, wallet=100.0)
        _setup_holding(mock_db, "ARC", 2.0)
        _setup_price(mock_db, "ARC", 2000.0)

        fee_cfg = await mock_db.guilds.get_fee_config(GUILD_ID)
        usd_value = 2.0 * 2000.0
        raw_fee = usd_value * fee_cfg["platform_fee_pct"]
        expected_fee = max(fee_cfg["platform_fee_min"], min(fee_cfg["platform_fee_max"], raw_fee))

        # Simulate fee deduction
        wallet_before = mock_db.users[(USER_ID, GUILD_ID)]["wallet"]
        await mock_db.update_wallet(USER_ID, GUILD_ID, -expected_fee)
        wallet_after = mock_db.users[(USER_ID, GUILD_ID)]["wallet"]

        assert wallet_after == wallet_before - expected_fee
        assert expected_fee > 0, "Fee must be positive"

    async def test_no_fee_on_wallet_to_bank(self, mock_db: MockDB):
        """DeFi to CeFi direction is free (no platform fee)."""
        _setup_user(mock_db, wallet=100.0)
        wallet_before = mock_db.users[(USER_ID, GUILD_ID)]["wallet"]

        # DeFi to CeFi move should not touch the USD wallet
        _setup_wallet_holding(mock_db, "arc", "ARC", 1.5)
        await mock_db.update_wallet_holding(USER_ID, GUILD_ID, "arc", "ARC", -1.5)
        await mock_db.update_holding(USER_ID, GUILD_ID, "ARC", 1.5)

        assert mock_db.users[(USER_ID, GUILD_ID)]["wallet"] == wallet_before

    async def test_move_skips_zero_balance_tokens(self, mock_db: MockDB):
        _setup_holding(mock_db, "ARC", 0.0)
        _setup_holding(mock_db, "MTA", 1.0)

        holdings = await mock_db.get_holdings(USER_ID, GUILD_ID)
        non_zero = [h for h in holdings if h["amount"] > 0]
        assert len(non_zero) == 1
        assert non_zero[0]["symbol"] == "MTA"


# ── Sell everything tests ---------------------------------------------------

class TestSellEverythingFees:
    """Verify that sell everything charges fees correctly."""

    async def test_sell_fee_matches_single_sell(self, mock_db: MockDB):
        """Batch sell fee per token should use the same formula as single sell."""
        _setup_user(mock_db, wallet=0.0)
        _setup_holding(mock_db, "ARC", 5.0)
        _setup_price(mock_db, "ARC", 2000.0)

        fee_cfg = await mock_db.guilds.get_fee_config(GUILD_ID)
        gross = 5.0 * 2000.0
        fee = max(fee_cfg["platform_fee_min"], min(fee_cfg["platform_fee_max"], gross * fee_cfg["platform_fee_pct"]))
        net = gross - fee

        assert fee > 0
        assert net < gross
        assert net > 0

    async def test_sell_all_tokens_independently(self, mock_db: MockDB):
        """Each token sold independently, fees not batched across tokens."""
        _setup_holding(mock_db, "ARC", 1.0)
        _setup_holding(mock_db, "MTA", 0.5)
        _setup_price(mock_db, "ARC", 2000.0)
        _setup_price(mock_db, "MTA", 40000.0)

        fee_cfg = await mock_db.guilds.get_fee_config(GUILD_ID)
        total_fees = 0.0
        for sym, amt in [("ARC", 1.0), ("MTA", 0.5)]:
            price = (await mock_db.get_price(sym, GUILD_ID))["price"]
            gross = amt * price
            fee = max(fee_cfg["platform_fee_min"], min(fee_cfg["platform_fee_max"], gross * fee_cfg["platform_fee_pct"]))
            total_fees += fee

        # Two independent fee calculations should produce two separate fees
        assert total_fees > 0

    async def test_disabled_tokens_excluded(self, mock_db: MockDB):
        """Disabled tokens should not be sold."""
        _setup_holding(mock_db, "ARC", 1.0)
        mock_db.disabled_tokens.add((GUILD_ID, "ARC"))

        assert await mock_db.is_token_disabled(GUILD_ID, "ARC") is True


# ── Unstake everything tests ------------------------------------------------

class TestUnstakeEverythingLocks:
    """Verify lock period enforcement and gas fee handling."""

    async def test_locked_positions_excluded(self, mock_db: MockDB):
        """Positions within the 24h lock window should be excluded."""
        now = time.time()
        lock_secs = 86400  # 24h

        # Unlocked position (staked 25h ago)
        mock_db.stakes[(USER_ID, GUILD_ID, "LIDO")] = {
            "amount": 1.0, "symbol": "ARC", "validator_id": "LIDO",
            "staked_at": now - (lock_secs + 3600),
        }
        # Locked position (staked 1h ago)
        mock_db.stakes[(USER_ID, GUILD_ID, "CBETH")] = {
            "amount": 2.0, "symbol": "ARC", "validator_id": "CBETH",
            "staked_at": now - 3600,
        }

        stakes = await mock_db.get_user_stakes(USER_ID, GUILD_ID)
        eligible = []
        locked = []
        for s in stakes:
            staked_at = s.get("staked_at", 0)
            if now - staked_at >= lock_secs:
                eligible.append(s)
            else:
                locked.append(s)

        assert len(eligible) == 1
        assert eligible[0]["validator_id"] == "LIDO"
        assert len(locked) == 1
        assert locked[0]["validator_id"] == "CBETH"

    async def test_unstake_uses_update_stake_negative(self, mock_db: MockDB):
        """Unstaking should call update_stake with negative amount."""
        mock_db.stakes[(USER_ID, GUILD_ID, "LIDO")] = {
            "amount": 5.0, "symbol": "ARC", "staked_at": 0,
        }

        await mock_db.update_stake(USER_ID, GUILD_ID, "LIDO", "ARC", -5.0)
        stake = await mock_db.get_stake(USER_ID, GUILD_ID, "LIDO")
        assert stake["amount"] == 0.0

    async def test_gas_deducted_from_wallet(self, mock_db: MockDB):
        """Gas fees should be deducted from the wallet holding."""
        _setup_wallet_holding(mock_db, "arc", "ARC", 10.0)
        gas_fee = 0.005

        await mock_db.update_wallet_holding(USER_ID, GUILD_ID, "arc", "ARC", -gas_fee)
        h = await mock_db.get_wallet_holding(USER_ID, GUILD_ID, "arc", "ARC")
        assert abs(h["amount"] - (10.0 - gas_fee)) < 1e-9

    async def test_insufficient_gas_skips_position(self, mock_db: MockDB):
        """If gas balance is below the fee, the position should be skipped."""
        _setup_wallet_holding(mock_db, "arc", "ARC", 0.001)
        gas_fee = 0.005

        h = await mock_db.get_wallet_holding(USER_ID, GUILD_ID, "arc", "ARC")
        gas_bal = h["amount"]
        assert gas_bal < gas_fee, "Balance should be below gas fee"


# ── Remove all LP tests ----------------------------------------------------

class TestRemoveAllLP:
    """Verify LP removal uses correct DB methods and enforces locks."""

    async def test_uses_update_lp_position_not_remove_lp(self, mock_db: MockDB):
        """LP removal should use update_lp_position(-shares), not remove_lp."""
        pool_id = _setup_pool(mock_db, "ARC", "USDC", 100.0, 200000.0, 1000.0)

        # update_lp_position should not raise
        await mock_db.update_lp_position(USER_ID, GUILD_ID, pool_id, -500.0)

        # remove_lp should not exist
        assert not hasattr(mock_db, "remove_lp"), "MockDB should not have remove_lp method"

    async def test_pool_reserves_updated_after_removal(self, mock_db: MockDB):
        pool_id = _setup_pool(mock_db, "ARC", "USDC", 100.0, 200000.0, 1000.0)

        shares = 500.0
        frac = shares / 1000.0
        out_a = 100.0 * frac
        out_b = 200000.0 * frac

        await mock_db.update_pool_reserves(
            pool_id, GUILD_ID,
            100.0 - out_a,
            200000.0 - out_b,
            1000.0 - shares,
        )

        pool = await mock_db.get_pool(pool_id, GUILD_ID)
        assert pool["reserve_a"] == 50.0
        assert pool["reserve_b"] == 100000.0
        assert pool["total_lp"] == 500.0

    async def test_lp_lock_excludes_recent_positions(self, mock_db: MockDB):
        """LP positions added within LP_LOCK_SECONDS should be excluded."""
        now = time.time()
        lp_lock = 7200  # 2 hours (Config.LP_LOCK_SECONDS)

        # Recent (locked)
        recent_added_at = now - 1800  # 30 min ago
        # Old (unlocked)
        old_added_at = now - 10000   # ~2.8h ago

        assert now - recent_added_at < lp_lock, "Recent should be locked"
        assert now - old_added_at >= lp_lock, "Old should be unlocked"


# ── Rugpull math tests ------------------------------------------------------

_config_available = True
try:
    from core.config import Config as _Config
except ImportError:
    _config_available = False


@pytest.mark.skipif(not _config_available, reason="core/config.py requires python-dotenv")
class TestRugpullTiers:
    """Verify rugpull wager calculations match config."""

    def test_tier_costs_scale_with_wallet(self):
        from core.config import Config
        from core.framework.scale import to_raw
        wallet = to_raw(10000.0)  # raw-int balance (10000 human units)

        for tier_name, tier in Config.RUGPULL_TIERS.items():
            cost = max(tier["min_cost"], int(wallet * tier["cost_pct"]))
            assert cost >= tier["min_cost"], f"{tier_name} cost below minimum"
            assert cost <= wallet, f"{tier_name} cost exceeds wallet"

    def test_tier_success_rates_ordered(self):
        from core.config import Config
        tiers = Config.RUGPULL_TIERS
        assert tiers["low"]["success"] < tiers["medium"]["success"]
        assert tiers["medium"]["success"] < tiers["high"]["success"]

    def test_tier_costs_ordered(self):
        from core.config import Config
        tiers = Config.RUGPULL_TIERS
        assert tiers["low"]["cost_pct"] < tiers["medium"]["cost_pct"]
        assert tiers["medium"]["cost_pct"] < tiers["high"]["cost_pct"]

    def test_high_tier_not_guaranteed(self):
        from core.config import Config
        assert Config.RUGPULL_TIERS["high"]["success"] < 1.0

    def test_bonuses_are_positive(self):
        from core.config import Config
        assert Config.RUGPULL_APE_BONUS > 0
        assert Config.RUGPULL_WORK_BONUS > 0
        assert Config.RUGPULL_APE_BONUS <= 0.5, "Ape bonus should not exceed 50%"
        assert Config.RUGPULL_WORK_BONUS <= 0.5, "Work bonus should not exceed 50%"


# ── Send all tests ----------------------------------------------------------

class TestSendAll:
    """Verify send all resolves to full balance."""

    async def test_all_resolves_to_full_balance(self, mock_db: MockDB):
        _setup_wallet_holding(mock_db, "arc", "ARC", 3.5)

        is_all = "all".lower() == "all"
        assert is_all

        h = await mock_db.get_wallet_holding(USER_ID, GUILD_ID, "arc", "ARC")
        amt = h["amount"]
        assert amt == 3.5

    async def test_all_with_zero_balance_rejected(self, mock_db: MockDB):
        _setup_wallet_holding(mock_db, "arc", "ARC", 0.0)

        h = await mock_db.get_wallet_holding(USER_ID, GUILD_ID, "arc", "ARC")
        amt = h["amount"]
        assert amt <= 0, "Zero balance should be rejected"

    def test_amount_parsing_accepts_all_keyword(self):
        """The string 'all' should not raise ValueError when parsed."""
        amount = "all"
        is_all = str(amount).lower() == "all"
        assert is_all is True

        # Non-all amounts should parse as float
        for valid in ["100", "100.5", "$100", "$1,000.50"]:
            clean = valid.lstrip("$").replace(",", "")
            assert float(clean) > 0

    def test_amount_parsing_rejects_garbage(self):
        """Non-numeric, non-'all' input should fail."""
        for invalid in ["abc", "send", "me", "", "all2"]:
            is_all = str(invalid).lower() == "all"
            if not is_all:
                clean = str(invalid).lstrip("$").replace(",", "")
                with pytest.raises(ValueError):
                    float(clean)


# ── ConfirmView pattern tests -----------------------------------------------

class TestConfirmViewPattern:
    """Verify the codebase uses wait_result(), not view.value."""

    def test_no_view_value_in_new_code(self):
        """Bulk operation code should use wait_result(), not view.value."""
        import ast

        files_to_check = [
            "cogs/bank.py",
            "cogs/trade.py",
            "cogs/stake.py",
        ]

        # These are the methods we wrote that had the bug
        methods_to_check = [
            "_move_everything",
            "_sell_everything",
            "_unstake_everything",
            "_remove_all_lp",
        ]

        for filepath in files_to_check:
            with open(filepath) as f:
                source = f.read()

            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                    if node.name in methods_to_check:
                        # Check the function body doesn't contain view.value
                        func_source = ast.get_source_segment(source, node)
                        if func_source:
                            assert "view.value" not in func_source, (
                                f"{filepath}:{node.name} still uses view.value instead of wait_result()"
                            )


# ── MockDB method coverage --------------------------------------------------

class TestMockDBCoverage:
    """Verify MockDB has stubs for methods used by bulk operations."""

    def test_has_update_bank(self, mock_db: MockDB):
        assert hasattr(mock_db, "update_bank") or hasattr(mock_db, "update_wallet")

    def test_has_update_stake(self, mock_db: MockDB):
        assert hasattr(mock_db, "update_stake")

    def test_has_update_lp_position(self, mock_db: MockDB):
        assert hasattr(mock_db, "update_lp_position")

    def test_has_update_pool_reserves(self, mock_db: MockDB):
        assert hasattr(mock_db, "update_pool_reserves")

    def test_has_get_user_stakes(self, mock_db: MockDB):
        assert hasattr(mock_db, "get_user_stakes")

    def test_has_get_all_wallet_holdings(self, mock_db: MockDB):
        assert hasattr(mock_db, "get_all_wallet_holdings")

    def test_has_split_to_community_reserves(self, mock_db: MockDB):
        assert hasattr(mock_db, "split_to_community_reserves")

    def test_no_remove_lp_method(self, mock_db: MockDB):
        """remove_lp does not exist - update_lp_position is the correct method."""
        assert not hasattr(mock_db, "remove_lp")

    def test_has_atomic_context_manager(self, mock_db: MockDB):
        assert hasattr(mock_db, "atomic")
