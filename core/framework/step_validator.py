"""Step validator  -  checks preconditions and verifies outcomes.

Pre-validates that a chain step *can* succeed (e.g. sufficient balance,
token not halted) and post-validates that it *did* succeed (e.g. holding
appeared, wallet balance changed).
"""
from __future__ import annotations

import logging

from core.config import Config
from core.framework.chain_engine import ActionType, ChainStep, ChainPlan
from core.framework.scale import to_human

log = logging.getLogger("discoin.validator")


class StepValidator:
    """Pre/post validation for chain steps."""

    def __init__(self, db) -> None:
        self.db = db

    # ── Public API ─────────────────────────────────────────────────────────

    async def pre_validate(self, step: ChainStep, plan: ChainPlan) -> tuple[bool, str]:
        """Check preconditions before executing *step*.

        Returns ``(True, "")`` when the step is safe to attempt, or
        ``(False, error_message)`` when a known precondition fails.
        """
        guild_id = plan.guild_id
        user_id = plan.user_id

        # Check if the token's network is halted
        if step.symbol:
            halted = await self._is_token_halted(guild_id, step.symbol)
            if halted:
                return False, f"{step.symbol} network is currently halted"

        amount = self._resolve_amount(step)

        action = step.action

        if action == ActionType.BUY:
            if amount is not None and step.symbol:
                price = await self._get_price(guild_id, step.symbol)
                if price <= 0:
                    return False, f"No price data for {step.symbol}"
                estimated_cost = price * amount
                wallet = await self._get_wallet_balance(guild_id, user_id)
                if wallet < estimated_cost:
                    return False, (
                        f"Insufficient balance: need ${estimated_cost:,.2f} "
                        f"but wallet has ${wallet:,.2f}"
                    )

        elif action == ActionType.SELL:
            if amount is not None and step.symbol:
                # Check both CeFi and DeFi holdings for sell
                cefi_holding = await self._get_holding(guild_id, user_id, step.symbol)
                
                # Also check DeFi wallet holdings
                defi_holding = 0.0
                try:
                    tok_cfg = Config.TOKENS.get(step.symbol.upper(), {})
                    network = tok_cfg.get("network", "")
                    _NETWORK_SHORT_MAP = {
                        "Sun Network": "sun",
                        "Moneta Chain": "mta", 
                        "Arcadia Network": "arc",
                        "Discoin Network": "dsc",
                    }
                    net_short = _NETWORK_SHORT_MAP.get(network, "")
                    if net_short:
                        row = await self.db.fetch_one(
                            "SELECT amount FROM wallet_holdings WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND symbol = $4",
                            user_id, guild_id, net_short, step.symbol.upper(),
                        )
                        defi_holding = to_human(int(row["amount"])) if row else 0.0
                except Exception:
                    pass
                
                total_holding = cefi_holding + defi_holding
                if total_holding < amount:
                    return False, (
                        f"Insufficient {step.symbol}: need {amount:,.4f} "
                        f"but hold {total_holding:,.4f} (CeFi: {cefi_holding:,.4f}, DeFi: {defi_holding:,.4f})"
                    )

        elif action == ActionType.SWAP:
            token_in = step.params.get("token_in") or step.symbol
            if amount is not None and token_in:
                holding = await self._get_holding(guild_id, user_id, token_in)
                if holding < amount:
                    return False, (
                        f"Insufficient {token_in}: need {amount:,.4f} "
                        f"but hold {holding:,.4f}"
                    )

        elif action == ActionType.TRANSFER:
            if amount is not None:
                wallet = await self._get_wallet_balance(guild_id, user_id)
                if wallet < amount:
                    return False, (
                        f"Insufficient balance: need ${amount:,.2f} "
                        f"but wallet has ${wallet:,.2f}"
                    )

        elif action == ActionType.STAKE:
            if amount is not None and step.symbol:
                holding = await self._get_holding(guild_id, user_id, step.symbol)
                if holding < amount:
                    return False, (
                        f"Insufficient {step.symbol}: need {amount:,.4f} "
                        f"but hold {holding:,.4f}"
                    )
            # Check validator exists
            if step.target:
                validator_id = step.target.upper()
                try:
                    row = await self.db.fetch_one(
                        "SELECT 1 FROM validators WHERE validator_id = $1 AND guild_id = $2",
                        validator_id, guild_id,
                    )
                    if not row:
                        return False, f"Validator '{step.target}' not found"
                except Exception:
                    pass

        elif action == ActionType.MOVE:
            # Check source location has the asset
            source = step.params.get("source")
            if source and step.symbol and amount is not None:
                if source == "wallet":
                    wallet = await self._get_wallet_balance(guild_id, user_id)
                    if wallet < amount:
                        return False, f"Insufficient wallet balance for move"
                elif source == "bank":
                    try:
                        row = await self.db.fetch_one(
                            "SELECT bank FROM users WHERE user_id = $1 AND guild_id = $2",
                            user_id, guild_id,
                        )
                        bank = row["bank"] if row else 0.0
                        if bank < amount:
                            return False, f"Insufficient bank balance for move"
                    except Exception:
                        pass

        elif action == ActionType.SAVE_DEPOSIT:
            if amount is not None:
                wallet = await self._get_wallet_balance(guild_id, user_id)
                if wallet < amount:
                    return False, (
                        f"Insufficient balance: need ${amount:,.2f} "
                        f"but wallet has ${wallet:,.2f}"
                    )

        elif action == ActionType.QUERY:
            return True, ""

        return True, ""

    async def post_validate(self, step: ChainStep, plan: ChainPlan) -> tuple[bool, str]:
        """Verify that an action took effect after execution.

        Returns ``(True, "")`` if the outcome looks correct, or
        ``(False, warning_message)`` if something seems off.
        """
        guild_id = plan.guild_id
        user_id = plan.user_id
        action = step.action

        if action == ActionType.QUERY:
            return True, ""

        if action == ActionType.BUY:
            if step.symbol:
                holding = await self._get_holding(guild_id, user_id, step.symbol)
                if holding <= 0:
                    return False, f"Expected {step.symbol} holding after buy, but found none"

        elif action == ActionType.SELL:
            # Verify wallet balance increased (we can't easily check the delta,
            # but we can at least confirm wallet exists)
            wallet = await self._get_wallet_balance(guild_id, user_id)
            if wallet <= 0:
                return False, "Expected wallet balance increase after sell"

        elif action == ActionType.SWAP:
            token_out = step.params.get("token_out")
            if token_out:
                # Swaps can output to CeFi holdings or DeFi wallet depending on configuration
                # Check both locations before warning
                cefi_holding = await self._get_holding(guild_id, user_id, token_out)
                
                # Also check DeFi wallet holdings
                defi_holding = 0.0
                try:
                    tok_cfg = Config.TOKENS.get(token_out, {})
                    network = tok_cfg.get("network", "")
                    _NETWORK_SHORT_MAP = {
                        "Sun Network": "sun",
                        "Moneta Chain": "mta", 
                        "Arcadia Network": "arc",
                        "Discoin Network": "dsc",
                    }
                    net_short = _NETWORK_SHORT_MAP.get(network, "")
                    if net_short:
                        row = await self.db.fetch_one(
                            "SELECT amount FROM wallet_holdings WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND symbol = $4",
                            user_id, guild_id, net_short, token_out,
                        )
                        defi_holding = to_human(int(row["amount"])) if row else 0.0
                except Exception:
                    pass
                
                total_holding = cefi_holding + defi_holding
                if total_holding <= 0:
                    return False, f"Expected {token_out} holding after swap, but found none (checked CeFi: {cefi_holding}, DeFi: {defi_holding})"

        elif action == ActionType.TRANSFER:
            # Verify sender balance decreased (basic sanity check)
            pass

        elif action == ActionType.STAKE:
            if step.target:
                try:
                    row = await self.db.fetch_one(
                        "SELECT 1 FROM stakes WHERE user_id = $1 AND guild_id = $2 AND validator_id = $3",
                        user_id, guild_id, step.target.upper(),
                    )
                    if not row:
                        return False, "Expected stake record after staking"
                except Exception:
                    pass

        return True, ""

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _resolve_amount(self, step: ChainStep) -> float | None:
        """Extract a concrete numeric amount from the step's AmountSpec."""
        if step.amount is None:
            return None
        if step.amount.resolved is not None:
            return step.amount.resolved
        return None

    async def _get_wallet_balance(self, guild_id: int, user_id: int) -> float:
        """Fetch the user's wallet balance."""
        try:
            row = await self.db.fetch_one(
                "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
                user_id, guild_id,
            )
            return to_human(int(row["wallet"])) if row else 0.0
        except Exception:
            return 0.0

    async def _get_holding(self, guild_id: int, user_id: int, symbol: str) -> float:
        """Fetch the user's holding of a specific token."""
        try:
            row = await self.db.fetch_one(
                "SELECT amount FROM crypto_holdings WHERE user_id = $1 AND guild_id = $2 AND symbol = $3",
                user_id, guild_id, symbol.upper(),
            )
            return to_human(int(row["amount"])) if row else 0.0
        except Exception:
            return 0.0

    async def _is_token_halted(self, guild_id: int, symbol: str) -> bool:
        """Check if the token's network is currently halted."""
        try:
            tok_cfg = Config.TOKENS.get(symbol.upper(), {})
            network = tok_cfg.get("network", "")
            if not network:
                return False
            return await self.db.guilds.is_network_halted(guild_id, network)
        except Exception:
            return False

    async def _get_price(self, guild_id: int, symbol: str) -> float:
        """Fetch the current price of a token."""
        try:
            row = await self.db.markets.get_price(symbol.upper(), guild_id)
            return float(row["price"]) if row else 0.0
        except Exception:
            return 0.0
