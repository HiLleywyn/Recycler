"""Tests for PoS validator alignment fixes."""
from cogs.validators import MAX_SLASH_COUNT


class TestSlashConstant:
    def test_max_slash_count_is_five(self):
        """MAX_SLASH_COUNT must be 5  -  the value the DB deactivates at."""
        assert MAX_SLASH_COUNT == 5

    def test_slash_display_uses_constant(self):
        """Slash display strings must reference MAX_SLASH_COUNT, not a hardcoded 3."""
        import inspect
        import cogs.validators as v_mod
        source = inspect.getsource(v_mod)
        # The embed at line ~1534 must NOT contain "/3 slashes" or "/3 slash"
        assert "/3 slashes" not in source, (
            "cogs/validators.py still has a hardcoded '/3 slashes' string  -  "
            "update to use MAX_SLASH_COUNT"
        )

    def test_trades_slash_display(self):
        import inspect
        import cogs.trades as t_mod
        source = inspect.getsource(t_mod)
        assert '"/3"' not in source and "'/3'" not in source and "/3\"" not in source, (
            "cogs/trades.py still has hardcoded '/3' slash strings"
        )

    def test_help_text_says_five(self):
        import inspect
        import cogs.help as h_mod
        source = inspect.getsource(h_mod)
        assert "3 slashes = auto-deactivated" not in source, (
            "cogs/help.py still says '3 slashes = auto-deactivated'"
        )


class TestGasSplitDefault:
    def test_treasury_cut_default_is_ten_pct(self):
        """get_fee_config default for treasury_cut_pct must be 0.10, not 0.05."""
        import inspect
        import database.guilds as guilds_mod
        source = inspect.getsource(guilds_mod)
        # The fallback in get_fee_config must say 0.10, not 0.05
        assert "else 0.05" not in source, (
            "database/guilds.py still has treasury_cut_pct default of 0.05  -  "
            "change to 0.10 to match VALIDATOR_REWARD/TREASURY_CUT constants"
        )

    def test_validators_docstring_correct(self):
        """cogs/validators.py module docstring must describe 90% to validator."""
        import cogs.validators as v_mod
        doc = v_mod.__doc__ or ""
        assert "10% to validator" not in doc, (
            "validators.py docstring still says '10% to validator'  -  "
            "should say '90% to validator'"
        )


class TestMicroSwapDeactivationRefund:
    """When a micro-swap slash deactivates a validator, delegators must be refunded."""

    def test_trade_deactivation_block_calls_wipe(self):
        """After _deactivated=True from micro-swap slash, wipe_delegations must be called."""
        import inspect
        import cogs.trade as trade_mod
        source = inspect.getsource(trade_mod)
        # The deactivation block must include wipe_delegations_for_validator
        assert "wipe_delegations_for_validator" in source, (
            "cogs/trade.py does not call wipe_delegations_for_validator on "
            "micro-swap deactivation  -  delegators will not be refunded"
        )

    def test_trade_deactivation_publishes_bus_event(self):
        """Deactivation in trade.py must publish pos_validator_slashed so trades.py DMs fire."""
        import inspect
        import cogs.trade as trade_mod
        source = inspect.getsource(trade_mod)
        assert "pos_validator_slashed" in source, (
            "cogs/trade.py does not publish pos_validator_slashed bus event on "
            "micro-swap deactivation  -  validator operator DM will not fire"
        )


class TestDeactivationDMs:
    def test_deactivation_refund_block_can_dm_delegators(self):
        """The deactivation refund block in validators.py must collect delegator IDs
        so the bus event can carry them for DM dispatch."""
        import inspect
        import cogs.validators as v_mod
        source = inspect.getsource(v_mod)
        # After wipe_delegations_for_validator, the code should publish an event
        # that includes delegator info (either inline DM or via bus)
        # We check that delegator_ids or delegation_rows appears in the deactivation block
        assert "delegator_id" in source, (
            "cogs/validators.py deactivation path does not reference delegator_id  -  "
            "delegator DMs cannot fire"
        )


class TestSendMempoolRouting:
    def test_send_checks_for_validators(self):
        """The .send command must query active validators before executing."""
        import inspect
        import cogs.bank as bank_mod
        source = inspect.getsource(bank_mod)
        assert "get_pos_validators_for_network" in source, (
            "cogs/bank.py .send command does not check for active validators  -  "
            "sends will always execute instantly even when validators are active"
        )

    def test_send_calls_add_to_mempool(self):
        """The .send command must submit to mempool when validators are active."""
        import inspect
        import cogs.bank as bank_mod
        source = inspect.getsource(bank_mod)
        assert "add_to_mempool" in source, (
            "cogs/bank.py .send command does not call add_to_mempool"
        )
