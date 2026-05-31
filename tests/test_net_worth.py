"""Tests for services/net_worth.py  -  portfolio net-worth computation."""
from __future__ import annotations

import pytest

from core.framework.scale import to_raw
from services.net_worth import NetWorthResult, compute_net_worth
from tests.conftest import GUILD_ID, USER_ID, MockDB


# ── NetWorthResult data class ──────────────────────────────────────────────────

class TestNetWorthResult:
    def test_total_property_sums_assets(self):
        nw = NetWorthResult(wallet=1000.0, bank=500.0, cefi_crypto=200.0)
        assert nw.total == pytest.approx(1700.0)

    def test_total_subtracts_liabilities(self):
        nw = NetWorthResult(wallet=1000.0, loan_liability=200.0)
        assert nw.total == pytest.approx(800.0)

    def test_total_all_components(self):
        nw = NetWorthResult(
            wallet=1000.0,
            bank=500.0,
            cefi_crypto=200.0,
            defi_wallet=150.0,
            stake_value=100.0,
            pos_stake_value=50.0,
            lp_value=75.0,
            rig_value=300.0,
            delegation_value=25.0,
            savings_value=400.0,
            items_value=20.0,
            loan_liability=300.0,
        )
        expected = (
            1000 + 500 + 200 + 150 + 100 + 50 + 75 + 300 + 25 + 400 + 20
            - 300
        )
        assert nw.total == pytest.approx(expected, abs=0.01)

    def test_total_zero_for_empty_result(self):
        assert NetWorthResult().total == pytest.approx(0.0)

    def test_total_is_rounded_to_cents(self):
        nw = NetWorthResult(wallet=0.123456789)
        # total property rounds to 2 decimal places
        assert nw.total == pytest.approx(0.12, abs=0.001)

    def test_default_holdings_empty(self):
        nw = NetWorthResult()
        assert nw.holdings == []
        assert nw.wallet_holdings == []
        assert nw.stakes == []


# ── compute_net_worth ─────────────────────────────────────────────────────────

class TestComputeNetWorth:
    def _make_db(
        self,
        wallet: int = 0,
        bank: int = 0,
    ) -> MockDB:
        db = MockDB()
        db.users[(USER_ID, GUILD_ID)] = {"wallet": wallet, "bank": bank}
        return db

    @pytest.mark.asyncio
    async def test_no_user_returns_empty_result(self):
        db = MockDB()  # No user row
        result = await compute_net_worth(USER_ID, GUILD_ID, db)
        assert result.total == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_wallet_and_bank(self):
        db = self._make_db(wallet=to_raw(500), bank=to_raw(250))
        result = await compute_net_worth(USER_ID, GUILD_ID, db)
        assert result.wallet == pytest.approx(500.0)
        assert result.bank == pytest.approx(250.0)
        assert result.total >= 750.0

    @pytest.mark.asyncio
    async def test_cefi_holdings_counted(self):
        db = self._make_db()
        db.holdings[(USER_ID, GUILD_ID, "ARC")] = {"amount": to_raw(2), "symbol": "ARC"}
        db.prices[("ARC", GUILD_ID)] = {"price": 1500.0}
        result = await compute_net_worth(USER_ID, GUILD_ID, db)
        assert result.cefi_crypto == pytest.approx(3000.0)

    @pytest.mark.asyncio
    async def test_cefi_holdings_no_price(self):
        """Holdings with no price data are counted as $0."""
        db = self._make_db()
        db.holdings[(USER_ID, GUILD_ID, "UNKNOWN")] = {"amount": to_raw(5), "symbol": "UNKNOWN"}
        # No price row for UNKNOWN
        result = await compute_net_worth(USER_ID, GUILD_ID, db)
        assert result.cefi_crypto == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_defi_wallet_holdings_counted(self):
        db = self._make_db()
        db.wallet_holdings[(USER_ID, GUILD_ID, "arc", "ARC")] = {
            "amount": to_raw(1), "symbol": "ARC"
        }
        db.prices[("ARC", GUILD_ID)] = {"price": 2000.0}
        result = await compute_net_worth(USER_ID, GUILD_ID, db)
        assert result.defi_wallet == pytest.approx(2000.0)

    @pytest.mark.asyncio
    async def test_total_includes_all_assets(self):
        db = self._make_db(wallet=to_raw(100), bank=to_raw(50))
        db.holdings[(USER_ID, GUILD_ID, "ARC")] = {"amount": to_raw(1), "symbol": "ARC"}
        db.prices[("ARC", GUILD_ID)] = {"price": 500.0}
        result = await compute_net_worth(USER_ID, GUILD_ID, db)
        assert result.total == pytest.approx(650.0)

    @pytest.mark.asyncio
    async def test_multiple_holdings(self):
        db = self._make_db()
        db.holdings[(USER_ID, GUILD_ID, "ARC")] = {"amount": to_raw(1), "symbol": "ARC"}
        db.holdings[(USER_ID, GUILD_ID, "MTA")] = {"amount": to_raw(0.1), "symbol": "MTA"}
        db.prices[("ARC", GUILD_ID)] = {"price": 2000.0}
        db.prices[("MTA", GUILD_ID)] = {"price": 30000.0}
        result = await compute_net_worth(USER_ID, GUILD_ID, db)
        assert result.cefi_crypto == pytest.approx(2000.0 + 3000.0)

    @pytest.mark.asyncio
    async def test_holdings_detail_populated(self):
        db = self._make_db()
        db.holdings[(USER_ID, GUILD_ID, "ARC")] = {"amount": to_raw(2), "symbol": "ARC"}
        db.prices[("ARC", GUILD_ID)] = {"price": 1000.0}
        result = await compute_net_worth(USER_ID, GUILD_ID, db)
        assert len(result.holdings) == 1
        holding = result.holdings[0]
        assert holding["symbol"] == "ARC"
        assert holding["amount"] == to_raw(2)
        assert holding["usd_value"] == pytest.approx(2000.0)
