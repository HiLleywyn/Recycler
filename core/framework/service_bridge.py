"""Service Bridge  -  executes chain actions against the service layer.

Bridges the chain engine to existing Discoin service functions (trade, swap,
stake, transfer, savings, liquidity) and direct DB operations for actions
that don't yet have a dedicated service module.

Each handler receives a ChainStep and ChainPlan and returns a structured dict
describing the result.  Handlers catch service-level errors and re-raise with
descriptive messages so callers get clean failure info.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from core.config import Config
from core.framework.chain_engine import ActionType, ChainStep, ChainPlan

log = logging.getLogger("discoin.bridge")

# ---------------------------------------------------------------------------
# Network name → short-code mapping.
# This mirrors the bank cog's _FULL_TO_SHORT table.  Kept here to avoid a
# circular import between the framework layer and cogs.  If network names
# change, update both locations (or extract to core/config.py in a future pass).
# ---------------------------------------------------------------------------

_NET_FULL_TO_SHORT: dict[str, str] = {
    "Sun Network": "sun",
    "Moneta Chain": "mta",
    "Arcadia Network": "arc",
    "Discoin Network": "dsc",
}


# ---------------------------------------------------------------------------
# Service imports  -  wrapped in try/except so missing modules don't crash init
# ---------------------------------------------------------------------------

try:
    from services.trade import execute_buy, execute_sell
except ImportError:
    execute_buy = None  # type: ignore[assignment]
    execute_sell = None  # type: ignore[assignment]
    log.warning("[bridge] services.trade not available")

try:
    from services.swap import compute_swap_quote, execute_swap
except ImportError:
    compute_swap_quote = None  # type: ignore[assignment]
    execute_swap = None  # type: ignore[assignment]
    log.warning("[bridge] services.swap not available")

try:
    from services.stake import execute_stake, execute_unstake
except ImportError:
    execute_stake = None  # type: ignore[assignment]
    execute_unstake = None  # type: ignore[assignment]
    log.warning("[bridge] services.stake not available")

try:
    from services.transfer import execute_transfer
except ImportError:
    execute_transfer = None  # type: ignore[assignment]
    log.warning("[bridge] services.transfer not available")

try:
    from services.savings import deposit_savings, withdraw_savings
except ImportError:
    deposit_savings = None  # type: ignore[assignment]
    withdraw_savings = None  # type: ignore[assignment]
    log.warning("[bridge] services.savings not available")

try:
    from services.liquidity import add_liquidity, remove_liquidity
except ImportError:
    add_liquidity = None  # type: ignore[assignment]
    remove_liquidity = None  # type: ignore[assignment]
    log.warning("[bridge] services.liquidity not available")


# ---------------------------------------------------------------------------
# Amount resolution helper
# ---------------------------------------------------------------------------

def _resolve_amount(step: ChainStep) -> float:
    """Resolve the concrete float amount from a ChainStep.

    Raises ValueError if the amount cannot be resolved to a positive number.
    """
    if step.amount is None:
        raise ValueError("No amount specified.")
    if step.amount.resolved is not None:
        val = step.amount.resolved
    elif step.amount.is_all or step.amount.is_rest:
        # Caller should have resolved these before reaching the bridge.
        raise ValueError(
            f"Dynamic amount '{step.amount.raw}' must be resolved before execution."
        )
    elif step.amount.is_fraction:
        raise ValueError(
            f"Fraction amount '{step.amount.raw}' must be resolved before execution."
        )
    else:
        raise ValueError(f"Cannot resolve amount: '{step.amount.raw}'")
    if val <= 0:
        raise ValueError("Amount must be positive.")
    return val


# ---------------------------------------------------------------------------
# ServiceBridge
# ---------------------------------------------------------------------------

class ServiceBridge:
    """Execute chain steps by dispatching to the appropriate service function."""

    def __init__(self, db):
        self.db = db

    # ── Dispatch table (class-level) ──────────────────────────────────────
    # Populated after all handler methods are defined.
    _HANDLERS: dict[ActionType, Callable[..., Awaitable[dict[str, Any]]]] = {}

    async def execute(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Execute a chain step.  Returns a structured result dict.

        Raises ValueError if no handler exists for the action.
        """
        handler = self._HANDLERS.get(step.action)
        if not handler:
            raise ValueError(f"No handler for action: {step.action.name}")
        return await handler(self, step, plan)

    # ══════════════════════════════════════════════════════════════════════
    # TRADING handlers
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_buy(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Buy tokens with USD via services.trade.execute_buy."""
        if execute_buy is None:
            raise RuntimeError("Trade service (execute_buy) is not available.")

        symbol = (step.symbol or "").upper()
        if not symbol:
            raise ValueError("No token symbol specified for buy.")

        amount = _resolve_amount(step)

        result = await execute_buy(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            symbol=symbol,
            amount=amount,
        )

        if not result.success:
            raise ValueError(f"Buy failed: {result.error}")

        return {
            "type": "trade",
            "action": "buy",
            "tx_hash": result.tx_hash,
            "symbol": symbol,
            "amount": result.amount,
            "cost": result.cost,
            "fee": result.fee,
            "new_price": result.new_price,
            "new_balance": result.new_balance,
        }

    async def _handle_sell(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Sell tokens for USD via services.trade.execute_sell."""
        if execute_sell is None:
            raise RuntimeError("Trade service (execute_sell) is not available.")

        symbol = (step.symbol or "").upper()
        if not symbol:
            raise ValueError("No token symbol specified for sell.")

        amount = _resolve_amount(step)

        result = await execute_sell(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            symbol=symbol,
            amount=amount,
        )

        if not result.success:
            raise ValueError(f"Sell failed: {result.error}")

        return {
            "type": "trade",
            "action": "sell",
            "tx_hash": result.tx_hash,
            "symbol": symbol,
            "amount": result.amount,
            "revenue": result.cost,   # TradeResult.cost = revenue for sells
            "fee": result.fee,
            "new_price": result.new_price,
            "new_balance": result.new_balance,
        }

    async def _handle_swap(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Swap one token for another via services.swap."""
        if compute_swap_quote is None or execute_swap is None:
            raise RuntimeError("Swap service is not available.")

        token_in = (step.symbol or step.params.get("token_in", "")).upper()
        token_out = (step.target or step.params.get("token_out", "")).upper()
        if not token_in:
            raise ValueError("No input token specified for swap.")
        if not token_out:
            raise ValueError("No output token specified for swap.")

        amount = _resolve_amount(step)

        # Compute quote first
        quote = await compute_swap_quote(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount,
        )

        # compute_swap_quote returns a string on error, SwapQuote on success
        if isinstance(quote, str):
            raise ValueError(f"Swap quote failed: {quote}")

        # Execute the swap
        result = await execute_swap(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            quote=quote,
        )

        if not result.success:
            raise ValueError(f"Swap failed: {result.error}")

        return {
            "type": "trade",
            "action": "swap",
            "tx_hash": result.tx_hash,
            "mempool_id": result.mempool_id,
            "token_in": token_in,
            "token_out": token_out,
            "amount_in": quote.amount_in,
            "amount_out": result.amount_out,
            "price": quote.exec_price,
            "rebate": result.rebate,
        }

    async def _handle_transfer(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Transfer USD to another user via services.transfer.execute_transfer."""
        if execute_transfer is None:
            raise RuntimeError("Transfer service is not available.")

        recipient_id = step.params.get("recipient_id")
        if not recipient_id:
            # Try to parse from target (could be a mention or ID)
            if step.target:
                try:
                    recipient_id = int(step.target.strip("<@!>"))
                except (ValueError, TypeError):
                    raise ValueError(f"Invalid recipient: {step.target}")
            else:
                raise ValueError("No recipient specified for transfer.")

        amount = _resolve_amount(step)

        result = await execute_transfer(
            db=self.db,
            guild_id=plan.guild_id,
            sender_id=plan.user_id,
            recipient_id=int(recipient_id),
            amount=amount,
        )

        if not result.success:
            raise ValueError(f"Transfer failed: {result.error}")

        return {
            "type": "trade",
            "action": "transfer",
            "tx_hash": result.tx_hash,
            "amount": result.amount,
            "new_balance": result.new_balance,
        }

    # ══════════════════════════════════════════════════════════════════════
    # STAKING handlers
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_stake(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Stake tokens on a validator via services.stake.execute_stake."""
        if execute_stake is None:
            raise RuntimeError("Stake service is not available.")

        validator_id = step.target or step.params.get("validator_id", "")
        if not validator_id:
            raise ValueError("No validator specified for staking.")

        amount = _resolve_amount(step)

        result = await execute_stake(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            validator_id=validator_id,
            amount=amount,
        )

        if not result.success:
            raise ValueError(f"Stake failed: {result.error}")

        return {
            "type": "staking",
            "action": "stake",
            "tx_hash": result.tx_hash,
            "amount": result.amount,
            "validator": result.validator_name,
            "symbol": result.symbol,
        }

    async def _handle_unstake(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Unstake tokens from a validator via services.stake.execute_unstake."""
        if execute_unstake is None:
            raise RuntimeError("Stake service (execute_unstake) is not available.")

        validator_id = step.target or step.params.get("validator_id", "")
        if not validator_id:
            raise ValueError("No validator specified for unstaking.")

        amount = _resolve_amount(step)

        result = await execute_unstake(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            validator_id=validator_id,
            amount=amount,
        )

        if not result.success:
            raise ValueError(f"Unstake failed: {result.error}")

        return {
            "type": "staking",
            "action": "unstake",
            "tx_hash": result.tx_hash,
            "amount_unstaked": result.amount_unstaked,
            "amount_received": result.amount_received,
            "penalty": result.penalty,
            "symbol": result.symbol,
        }

    async def _handle_delegate(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Delegate tokens to a validator.

        Delegates use the same stake service  -  the distinction is semantic
        in the NLP layer.  The chain DB tracks delegation separately.
        """
        if execute_stake is None:
            raise RuntimeError("Stake service is not available for delegation.")

        validator_id = step.target or step.params.get("validator_id", "")
        if not validator_id:
            raise ValueError("No validator specified for delegation.")

        amount = _resolve_amount(step)

        result = await execute_stake(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            validator_id=validator_id,
            amount=amount,
        )

        if not result.success:
            raise ValueError(f"Delegation failed: {result.error}")

        return {
            "type": "staking",
            "action": "delegate",
            "tx_hash": result.tx_hash,
            "amount": result.amount,
            "validator": result.validator_name,
            "symbol": result.symbol,
        }

    async def _handle_undelegate(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Undelegate tokens from a validator."""
        if execute_unstake is None:
            raise RuntimeError("Stake service is not available for undelegation.")

        validator_id = step.target or step.params.get("validator_id", "")
        if not validator_id:
            raise ValueError("No validator specified for undelegation.")

        amount = _resolve_amount(step)

        result = await execute_unstake(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            validator_id=validator_id,
            amount=amount,
        )

        if not result.success:
            raise ValueError(f"Undelegation failed: {result.error}")

        return {
            "type": "staking",
            "action": "undelegate",
            "tx_hash": result.tx_hash,
            "amount_unstaked": result.amount_unstaked,
            "amount_received": result.amount_received,
            "penalty": result.penalty,
            "symbol": result.symbol,
        }

    # ══════════════════════════════════════════════════════════════════════
    # DeFi handlers (Savings, Liquidity, Lending)
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_save_deposit(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Deposit to savings via services.savings.deposit_savings."""
        if deposit_savings is None:
            raise RuntimeError("Savings service (deposit_savings) is not available.")

        symbol = (step.symbol or "USD").upper()
        amount = _resolve_amount(step)

        result = await deposit_savings(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            symbol=symbol,
            amount=amount,
        )

        if not result.success:
            raise ValueError(f"Savings deposit failed: {result.error}")

        return {
            "type": "savings",
            "action": "deposit",
            "amount": result.amount,
            "symbol": result.symbol,
            "new_savings_balance": result.new_savings_balance,
        }

    async def _handle_save_withdraw(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Withdraw from savings via services.savings.withdraw_savings."""
        if withdraw_savings is None:
            raise RuntimeError("Savings service (withdraw_savings) is not available.")

        symbol = (step.symbol or "USD").upper()
        amount = _resolve_amount(step)

        result = await withdraw_savings(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            symbol=symbol,
            amount=amount,
        )

        if not result.success:
            raise ValueError(f"Savings withdrawal failed: {result.error}")

        return {
            "type": "savings",
            "action": "withdraw",
            "amount": result.amount,
            "symbol": result.symbol,
            "new_savings_balance": result.new_savings_balance,
        }

    async def _handle_add_lp(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Add liquidity to a pool via services.liquidity.add_liquidity."""
        if add_liquidity is None:
            raise RuntimeError("Liquidity service (add_liquidity) is not available.")

        token_a = (step.symbol or step.params.get("token_a", "")).upper()
        token_b = (step.target or step.params.get("token_b", "")).upper()
        if not token_a:
            raise ValueError("No first token specified for add liquidity.")
        if not token_b:
            raise ValueError("No second token specified for add liquidity.")

        amount_a = _resolve_amount(step)
        amount_b = step.params.get("amount_b", 0.0)

        result = await add_liquidity(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            token_a=token_a,
            token_b=token_b,
            amount_a=amount_a,
            amount_b=float(amount_b),
        )

        if not result.success:
            raise ValueError(f"Add liquidity failed: {result.error}")

        return {
            "type": "liquidity",
            "action": "add",
            "tx_hash": result.tx_hash,
            "token_a": result.token_a,
            "token_b": result.token_b,
            "amount_a": result.amount_a,
            "amount_b": result.amount_b,
            "lp_tokens": result.lp_tokens,
        }

    async def _handle_remove_lp(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Remove liquidity from a pool via services.liquidity.remove_liquidity."""
        if remove_liquidity is None:
            raise RuntimeError("Liquidity service (remove_liquidity) is not available.")

        token_a = (step.symbol or step.params.get("token_a", "")).upper()
        token_b = (step.target or step.params.get("token_b", "")).upper()
        if not token_a:
            raise ValueError("No first token specified for remove liquidity.")
        if not token_b:
            raise ValueError("No second token specified for remove liquidity.")

        # For remove_liquidity, the amount is a share percentage (0-100)
        share_pct = _resolve_amount(step)

        result = await remove_liquidity(
            db=self.db,
            guild_id=plan.guild_id,
            user_id=plan.user_id,
            token_a=token_a,
            token_b=token_b,
            share_pct=share_pct,
        )

        if not result.success:
            raise ValueError(f"Remove liquidity failed: {result.error}")

        return {
            "type": "liquidity",
            "action": "remove",
            "tx_hash": result.tx_hash,
            "token_a": result.token_a,
            "token_b": result.token_b,
            "amount_a": result.amount_a,
            "amount_b": result.amount_b,
            "lp_tokens_burned": result.lp_tokens,
        }

    # ══════════════════════════════════════════════════════════════════════
    # MOVEMENT handler
    # ══════════════════════════════════════════════════════════════════════

    async def _resolve_token_network(self, symbol: str, guild_id: int) -> str:
        """Try to resolve the network short-code for a token via guild token config."""
        try:
            all_tokens = await self.db.get_all_tokens_for_guild(guild_id)
            tok_info = all_tokens.get(symbol, {})
            tok_net = tok_info.get("network", "")
            return _NET_FULL_TO_SHORT.get(tok_net, "")
        except Exception:
            return ""

    async def _handle_move(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Move tokens between CeFi (bank) and DeFi (wallet) locations.

        Direction is derived from the 'from'/'to' params set by the command parser
        (e.g. from=wallet to=bank → to_bank).  If 'direction' is explicitly provided
        it takes precedence.  An explicit 'network' param can override auto-resolution.
        """
        symbol = (step.symbol or "").upper()
        if not symbol:
            raise ValueError("No token symbol specified for move.")

        amount = _resolve_amount(step)

        # Derive direction from from/to params when not explicitly set
        _bank_locs = frozenset({"bank", "b", "vault", "v"})
        from_loc = step.params.get("from", step.params.get("source", "")).lower()
        to_loc = step.params.get("to", step.params.get("destination", "")).lower()
        if step.params.get("direction"):
            direction = step.params["direction"]
        elif to_loc in _bank_locs:
            direction = "to_bank"
        else:
            direction = "to_wallet"  # default: wallet / DeFi side

        network = step.params.get("network", "")

        try:
            if direction == "to_wallet":
                # CeFi holdings / bank → DeFi wallet_holdings
                if symbol == "USD":
                    user = await self.db.get_user(plan.user_id, plan.guild_id)
                    balance = float(user["wallet"]) if user else 0.0
                    if balance < amount:
                        raise ValueError(f"Insufficient USD. Have ${balance:,.2f}.")
                    async with self.db.atomic():
                        await self.db.update_wallet(plan.user_id, plan.guild_id, -amount)
                        if network:
                            await self.db.update_wallet_holding(
                                plan.user_id, plan.guild_id, network, symbol, amount
                            )
                        else:
                            await self.db.update_holding(
                                plan.user_id, plan.guild_id, symbol, amount
                            )
                else:
                    # Resolve network if not provided  -  required to credit DeFi wallet_holding
                    if not network:
                        network = await self._resolve_token_network(symbol, plan.guild_id)
                    if not network:
                        raise ValueError(
                            f"Cannot move {symbol} to the DeFi wallet without a network. "
                            f"Use `,bank move <amount> {symbol} bank wallet` instead, or specify the "
                            f"network (e.g. `arc`, `sun`)."
                        )
                    # Require a DeFi wallet address for the target network
                    if not await self.db.has_defi_wallet(plan.user_id, plan.guild_id, network):
                        raise ValueError(
                            f"No {network.upper()} DeFi wallet. "
                            f"Create one first with `,wallet create {network}`."
                        )
                    holding = await self.db.get_holding(plan.user_id, plan.guild_id, symbol)
                    balance = float(holding["amount"]) if holding else 0.0
                    if balance < amount:
                        raise ValueError(f"Insufficient {symbol}. Have {balance:,.6f}.")
                    # Platform fee is deducted from the crypto amount itself (crypto-native).
                    _fee_cfg = await self.db.guilds.get_fee_config(plan.guild_id)
                    price_row = await self.db.get_price(symbol, plan.guild_id)
                    _price = float(price_row["price"]) if price_row else 0.0
                    usd_val = amount * _price
                    raw_fee = usd_val * _fee_cfg["platform_fee_pct"]
                    platform_fee = max(
                        _fee_cfg["platform_fee_min"],
                        min(_fee_cfg["platform_fee_max"], raw_fee),
                    )
                    fee_in_crypto = platform_fee / _price if _price > 0 else 0.0
                    if fee_in_crypto >= amount:
                        raise ValueError(
                            f"Move too small  -  minimum fee (${_fee_cfg['platform_fee_min']:,.2f}) "
                            f"exceeds the value of {amount:,.6f} {symbol} (${usd_val:,.4f})."
                        )
                    net_amount = amount - fee_in_crypto
                    async with self.db.atomic():
                        await self.db.update_holding(plan.user_id, plan.guild_id, symbol, -amount)
                        await self.db.update_wallet_holding(
                            plan.user_id, plan.guild_id, network, symbol, net_amount
                        )
                        if platform_fee > 0:
                            await self.db.split_to_community_reserves(
                                plan.guild_id, "USD", platform_fee
                            )

            elif direction == "to_bank":
                # DeFi wallet_holdings → CeFi holdings / bank
                if symbol == "USD":
                    if network:
                        holding = await self.db.get_wallet_holding(
                            plan.user_id, plan.guild_id, network, symbol
                        )
                        balance = float(holding["amount"]) if holding else 0.0
                        if balance < amount:
                            raise ValueError(f"Insufficient USD in DeFi wallet. Have ${balance:,.2f}.")
                        async with self.db.atomic():
                            await self.db.update_wallet_holding(
                                plan.user_id, plan.guild_id, network, symbol, -amount
                            )
                            await self.db.update_wallet(plan.user_id, plan.guild_id, amount)
                    else:
                        user = await self.db.get_user(plan.user_id, plan.guild_id)
                        balance = float(user["wallet"]) if user else 0.0
                        if balance < amount:
                            raise ValueError(f"Insufficient USD. Have ${balance:,.2f}.")
                        await self.db.deposit_to_bank(plan.user_id, plan.guild_id, amount)
                else:
                    # Resolve network if not provided
                    if not network:
                        network = await self._resolve_token_network(symbol, plan.guild_id)
                    if not network:
                        raise ValueError(
                            f"Cannot move {symbol} to bank without a network. "
                            f"Use `,bank move {symbol} wallet bank` instead, or specify the "
                            f"network (e.g. `arc`, `sun`)."
                        )
                    holding = await self.db.get_wallet_holding(
                        plan.user_id, plan.guild_id, network, symbol
                    )
                    balance = float(holding["amount"]) if holding else 0.0
                    if balance < amount:
                        raise ValueError(
                            f"Insufficient {symbol} in DeFi wallet. Have {balance:,.6f}."
                        )
                    async with self.db.atomic():
                        await self.db.update_wallet_holding(
                            plan.user_id, plan.guild_id, network, symbol, -amount
                        )
                        await self.db.update_holding(plan.user_id, plan.guild_id, symbol, amount)
            else:
                raise ValueError(f"Unknown move direction: {direction}")

            result: dict[str, Any] = {
                "type": "movement",
                "action": "move",
                "symbol": symbol,
                "amount": amount,
                "direction": direction,
                "network": network,
            }
            if direction == "to_wallet" and symbol != "USD":
                result["platform_fee"] = platform_fee
                result["fee_coin"] = "USD"
            return result
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Move failed: {exc}") from exc

    # ══════════════════════════════════════════════════════════════════════
    # DEPOSIT / WITHDRAW handlers (bank ↔ wallet)
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_deposit(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Deposit USD from wallet into bank."""
        amount = _resolve_amount(step)

        try:
            new_wallet, new_bank = await self.db.deposit_to_bank(
                plan.user_id, plan.guild_id, amount,
            )
            return {
                "type": "bank",
                "action": "deposit",
                "symbol": "USD",
                "amount": amount,
                "new_wallet": new_wallet,
                "new_bank": new_bank,
            }
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Deposit failed: {exc}") from exc

    async def _handle_withdraw(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Withdraw USD from bank into wallet."""
        amount = _resolve_amount(step)

        try:
            new_wallet, new_bank = await self.db.withdraw_from_bank(
                plan.user_id, plan.guild_id, amount,
            )
            return {
                "type": "bank",
                "action": "withdraw",
                "symbol": "USD",
                "amount": amount,
                "new_wallet": new_wallet,
                "new_bank": new_bank,
            }
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Withdraw failed: {exc}") from exc

    # ══════════════════════════════════════════════════════════════════════
    # SHOP handler
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_shop_buy(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Buy a shop item  -  direct DB operations."""
        item_name = (step.target or step.symbol or step.params.get("item", "")).lower()
        if not item_name:
            raise ValueError("No item specified for shop purchase.")

        quantity = 1
        if step.amount and step.amount.resolved:
            quantity = max(1, int(step.amount.resolved))

        try:
            # Look up item in the shop table
            item = await self.db.get_shop_item(plan.guild_id, item_name)
            if not item:
                raise ValueError(f"Unknown shop item: {item_name}")

            cost = item["price"] * quantity

            # Check balance
            user = await self.db.get_user(plan.user_id, plan.guild_id)
            wallet = user["wallet"] if user else 0.0
            if wallet < cost:
                raise ValueError(
                    f"Insufficient balance. Need ${cost:,.2f} but have ${wallet:,.2f}."
                )

            async with self.db.atomic():
                await self.db.update_wallet(plan.user_id, plan.guild_id, -cost)
                await self.db.add_inventory_item(
                    plan.user_id, plan.guild_id, item["item_id"], quantity
                )

            return {
                "type": "shop",
                "action": "buy",
                "item": item_name,
                "quantity": quantity,
                "cost": cost,
            }
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Shop purchase failed: {exc}") from exc

    # ══════════════════════════════════════════════════════════════════════
    # GAME handler
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_play_game(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Play a casino game  -  direct DB call to game result."""
        game_name = (step.target or step.params.get("game", "")).lower()
        if not game_name:
            raise ValueError("No game specified.")

        bet_amount = 0.0
        if step.amount and step.amount.resolved:
            bet_amount = step.amount.resolved

        # Return structured game request for the game engine to handle
        return {
            "type": "game",
            "action": "play",
            "game": game_name,
            "bet": bet_amount,
            "params": step.params,
            "guild_id": plan.guild_id,
            "user_id": plan.user_id,
        }

    # ══════════════════════════════════════════════════════════════════════
    # WALLET handler
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_create_wallet(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Create a DeFi wallet  -  insert into wallet_addresses table."""
        network = (step.target or step.params.get("network", "")).strip()
        if not network:
            raise ValueError("No network specified for wallet creation.")

        try:
            # Check if wallet already exists
            has = await self.db.has_defi_wallet(plan.user_id, plan.guild_id, network)
            if has:
                return {
                    "type": "wallet",
                    "action": "create",
                    "network": network,
                    "already_exists": True,
                    "content": f"You already have a wallet on the {network} network.",
                }

            await self.db.create_defi_wallet(plan.user_id, plan.guild_id, network)

            return {
                "type": "wallet",
                "action": "create",
                "network": network,
                "already_exists": False,
                "content": f"Created new DeFi wallet on the {network} network.",
            }
        except Exception as exc:
            raise ValueError(f"Wallet creation failed: {exc}") from exc

    # ══════════════════════════════════════════════════════════════════════
    # QUERY handler (read-only)
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_query(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Handle read-only queries.  Returns structured data without modifying state.

        The query sub-type is determined from step.params["intent_id"] or
        step.params["query_type"], which the intent map or AI think engine sets.
        """
        intent_id = step.params.get("intent_id", "")
        query_type = step.params.get("query_type", intent_id)

        # Dispatch to sub-handlers based on query domain
        if query_type.startswith("portfolio.") or query_type in ("balance", "holdings"):
            return await self._query_portfolio(step, plan, query_type)
        elif query_type.startswith("market."):
            return await self._query_market(step, plan, query_type)
        elif query_type.startswith("staking.") or query_type.startswith("mining."):
            return await self._query_staking_mining(step, plan, query_type)
        elif query_type.startswith("blockchain."):
            return await self._query_blockchain(step, plan, query_type)
        elif query_type.startswith("stats."):
            return await self._query_stats(step, plan, query_type)
        else:
            # Generic pass-through  -  let the caller handle rendering
            return {
                "type": "query",
                "query_type": query_type,
                "intent_id": intent_id,
                "guild_id": plan.guild_id,
                "user_id": plan.user_id,
                "params": step.params,
            }

    # ── Query sub-handlers ────────────────────────────────────────────────

    async def _query_portfolio(
        self, step: ChainStep, plan: ChainPlan, query_type: str,
    ) -> dict[str, Any]:
        """Fetch portfolio-related data."""
        try:
            user = await self.db.get_user(plan.user_id, plan.guild_id)
            wallet = user["wallet"] if user else 0.0

            if query_type in ("portfolio.summary", "balance"):
                return {"type": "query", "query_type": query_type, "data": {"wallet": wallet}}

            if query_type in ("portfolio.holdings", "holdings"):
                holdings = await self.db.get_all_holdings(plan.user_id, plan.guild_id)
                return {"type": "query", "query_type": query_type, "data": {"wallet": wallet, "holdings": holdings}}

            if query_type == "portfolio.stakes":
                stakes = await self.db.get_user_stakes(plan.user_id, plan.guild_id)
                return {"type": "query", "query_type": query_type, "data": {"stakes": stakes}}

            if query_type == "portfolio.savings":
                deposits = await self.db.get_user_savings(plan.user_id, plan.guild_id)
                return {"type": "query", "query_type": query_type, "data": {"savings": deposits}}

            if query_type == "portfolio.loans":
                loans = await self.db.get_user_loans(plan.user_id, plan.guild_id)
                return {"type": "query", "query_type": query_type, "data": {"loans": loans}}

        except Exception as exc:
            log.warning("[bridge] Portfolio query error: %s", exc)

        return {"type": "query", "query_type": query_type, "guild_id": plan.guild_id, "user_id": plan.user_id}

    async def _query_market(
        self, step: ChainStep, plan: ChainPlan, query_type: str,
    ) -> dict[str, Any]:
        """Fetch market-related data."""
        try:
            if query_type == "market.price_single":
                symbol = (step.symbol or step.params.get("symbol", "")).upper()
                if symbol:
                    price_row = await self.db.get_price(symbol, plan.guild_id)
                    if price_row:
                        return {
                            "type": "query",
                            "query_type": query_type,
                            "data": {"symbol": symbol, "price": float(price_row["price"])},
                        }

            if query_type == "market.prices":
                all_tokens = await self.db.get_all_tokens_for_guild(plan.guild_id)
                prices = {}
                for sym in all_tokens:
                    row = await self.db.get_price(sym, plan.guild_id)
                    if row:
                        prices[sym] = float(row["price"])
                return {"type": "query", "query_type": query_type, "data": {"prices": prices}}

        except Exception as exc:
            log.warning("[bridge] Market query error: %s", exc)

        return {"type": "query", "query_type": query_type, "guild_id": plan.guild_id, "params": step.params}

    async def _query_staking_mining(
        self, step: ChainStep, plan: ChainPlan, query_type: str,
    ) -> dict[str, Any]:
        """Fetch staking/mining related data."""
        return {
            "type": "query",
            "query_type": query_type,
            "guild_id": plan.guild_id,
            "user_id": plan.user_id,
            "params": step.params,
        }

    async def _query_blockchain(
        self, step: ChainStep, plan: ChainPlan, query_type: str,
    ) -> dict[str, Any]:
        """Fetch blockchain/explorer data."""
        return {
            "type": "query",
            "query_type": query_type,
            "guild_id": plan.guild_id,
            "params": step.params,
        }

    async def _query_stats(
        self, step: ChainStep, plan: ChainPlan, query_type: str,
    ) -> dict[str, Any]:
        """Fetch stats/leaderboard data."""
        return {
            "type": "query",
            "query_type": query_type,
            "guild_id": plan.guild_id,
            "user_id": plan.user_id,
            "params": step.params,
        }


    # ══════════════════════════════════════════════════════════════════════
    # BUY RIG handler
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_buy_rig(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Buy a mining rig from the chain mine shop via Config catalog + DB update."""
        rig_type = (step.target or step.params.get("rig_type", "basic")).upper()
        quantity = 1
        if step.amount and step.amount.resolved:
            quantity = max(1, int(step.amount.resolved))

        try:
            # Look up rig price in the static config catalog (same as chain_group.py:mine_buy)
            rig = Config.MINING_RIGS.get(rig_type)
            if not rig:
                raise ValueError(
                    f"Unknown rig type: {rig_type!r}. "
                    f"Valid rigs: {', '.join(Config.MINING_RIGS)}"
                )

            cost = rig["price"] * quantity

            user = await self.db.get_user(plan.user_id, plan.guild_id)
            wallet = user["wallet"] if user else 0.0
            if wallet < cost:
                raise ValueError(
                    f"Insufficient balance. Need ${cost:,.2f} but have ${wallet:,.2f}."
                )

            async with self.db.atomic():
                await self.db.update_wallet(plan.user_id, plan.guild_id, -cost)
                await self.db.update_rig(plan.user_id, plan.guild_id, rig_type, quantity)

            return {
                "type": "mining",
                "action": "buy_rig",
                "rig_type": rig_type,
                "quantity": quantity,
                "cost": cost,
            }
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Buy rig failed: {exc}") from exc

    # ══════════════════════════════════════════════════════════════════════
    # SET NOTIFICATION handler
    # ══════════════════════════════════════════════════════════════════════

    # Map user-facing notification type names to user_prefs boolean columns.
    _NOTIF_TYPE_TO_PREF: dict[str, str] = {
        "mining":       "dm_mining",
        "transfer":     "dm_transfer",
        "transfers":    "dm_transfer",
        "validator":    "dm_validator",
        "validators":   "dm_validator",
        "staking":      "dm_staking",
        "stake":        "dm_staking",
        "unstake":      "dm_staking",
        "whale":        "dm_whale_alerts",
        "whale_alert":  "dm_whale_alerts",
        "whale_alerts": "dm_whale_alerts",
        "price_alert":  "dm_whale_alerts",
        "price":        "dm_whale_alerts",
        "pump":         "dm_whale_alerts",
        "drop":         "dm_whale_alerts",
        "nft":          "dm_nft",
        "predictions":  "dm_predictions",
        "prediction":   "dm_predictions",
        "events":       "dm_events",
        "event":        "dm_events",
        "ape":          "dm_ape",
        "itemlevelup":  "dm_itemlevelup",
        "levelup":      "dm_itemlevelup",
    }

    async def _handle_set_notification(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        """Set a user notification preference via DB."""
        notif_type = (step.target or step.params.get("notification_type", "events")).lower()
        symbol = (step.symbol or step.params.get("symbol", "")).upper()
        value = step.params.get("value", "on").lower()
        enabled = value in ("on", "enable", "true", "1", "yes", "all")

        pref_col = self._NOTIF_TYPE_TO_PREF.get(notif_type)
        if not pref_col:
            raise ValueError(
                f"Unknown notification type: {notif_type!r}. "
                f"Valid types: {', '.join(sorted(self._NOTIF_TYPE_TO_PREF))}"
            )

        try:
            await self.db.set_user_pref(
                user_id=plan.user_id,
                guild_id=plan.guild_id,
                column=pref_col,
                value=enabled,
            )
            return {
                "type": "notification",
                "action": "set",
                "notification_type": notif_type,
                "pref_column": pref_col,
                "symbol": symbol,
                "enabled": enabled,
            }
        except Exception as exc:
            raise ValueError(f"Set notification failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Wire up the dispatch table
# ---------------------------------------------------------------------------

ServiceBridge._HANDLERS = {
    ActionType.BUY:           ServiceBridge._handle_buy,
    ActionType.SELL:          ServiceBridge._handle_sell,
    ActionType.SWAP:          ServiceBridge._handle_swap,
    ActionType.TRANSFER:      ServiceBridge._handle_transfer,
    ActionType.STAKE:         ServiceBridge._handle_stake,
    ActionType.UNSTAKE:       ServiceBridge._handle_unstake,
    ActionType.DELEGATE:      ServiceBridge._handle_delegate,
    ActionType.UNDELEGATE:    ServiceBridge._handle_undelegate,
    ActionType.ADD_LP:        ServiceBridge._handle_add_lp,
    ActionType.REMOVE_LP:     ServiceBridge._handle_remove_lp,
    ActionType.SAVE_DEPOSIT:  ServiceBridge._handle_save_deposit,
    ActionType.SAVE_WITHDRAW: ServiceBridge._handle_save_withdraw,
    ActionType.MOVE:          ServiceBridge._handle_move,
    ActionType.DEPOSIT:       ServiceBridge._handle_deposit,
    ActionType.WITHDRAW:      ServiceBridge._handle_withdraw,
    ActionType.SHOP_BUY:          ServiceBridge._handle_shop_buy,
    ActionType.PLAY_GAME:         ServiceBridge._handle_play_game,
    ActionType.BUY_RIG:           ServiceBridge._handle_buy_rig,
    ActionType.CREATE_WALLET:     ServiceBridge._handle_create_wallet,
    ActionType.SET_NOTIFICATION:  ServiceBridge._handle_set_notification,
    ActionType.QUERY:             ServiceBridge._handle_query,
}
