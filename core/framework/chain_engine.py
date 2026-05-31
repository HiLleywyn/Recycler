"""
core/framework/chain_engine.py  -  Core chain execution engine for the Discoin NLP pipeline.

Provides data models for multi-step transaction plans, an amount resolver
that converts dynamic expressions (all, rest, half, ...) into concrete
values at execution time, a chain executor that drives step-by-step
execution with retries, and a scheduler for delayed chains.

Usage:
    from core.framework.chain_engine import (
        ChainPlan, ChainStep, ChainExecutor, ChainScheduler,
        ActionType, StepStatus, describe_step, format_duration,
    )
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.framework.amount_parser import AmountSpec

log = logging.getLogger("discoin.chain_engine")


# ═══════════════════════════════════════════════════════════════════════════
# Enumerations
# ═══════════════════════════════════════════════════════════════════════════

class ChainOperator(str, enum.Enum):
    """Operator linking two chain steps together.

    Each operator controls how the engine proceeds after a step finishes.

    Operators:
        SEQ  (>)    -  Sequential: next runs only if previous succeeded.
        AND  (&&)   -  Strict AND: identical to SEQ but explicit.
        FIRE (;)    -  Fire-and-forget: next runs regardless of outcome.
        OR   (||)   -  Fallback OR: next runs only if previous *failed*.
        PIPE (|)    -  Pipe: like SEQ but injects previous result into next.
        PARA (+)    -  Parallel: next step runs concurrently with previous.
    """

    SEQ  = ">"
    AND  = "&&"
    FIRE = ";"
    OR   = "||"
    PIPE = "|"
    PARA = "+"


class StepStatus(str, enum.Enum):
    """Lifecycle state of a single chain step."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


class ActionType(str, enum.Enum):
    """Every discrete action the NLP engine can resolve to."""

    # ── Trading ────────────────────────────────────────────────────────
    BUY = "buy"
    SELL = "sell"
    SWAP = "swap"
    TRANSFER = "transfer"

    # ── Movement ───────────────────────────────────────────────────────
    MOVE = "move"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"

    # ── Staking ────────────────────────────────────────────────────────
    STAKE = "stake"
    UNSTAKE = "unstake"
    DELEGATE = "delegate"
    UNDELEGATE = "undelegate"

    # ── DeFi ───────────────────────────────────────────────────────────
    ADD_LP = "add_lp"
    REMOVE_LP = "remove_lp"
    SAVE_DEPOSIT = "save_deposit"
    SAVE_WITHDRAW = "save_withdraw"

    # ── Mining / Shop ──────────────────────────────────────────────────
    BUY_RIG = "buy_rig"
    SHOP_BUY = "shop_buy"

    # ── Games ──────────────────────────────────────────────────────────
    PLAY_GAME = "play_game"

    # ── Read-only / Utility ────────────────────────────────────────────
    QUERY = "query"
    CREATE_WALLET = "create_wallet"
    SET_NOTIFICATION = "set_notification"

    # ── Passthrough (bot commands not handled by NLP) ─────────────────
    PASSTHROUGH = "passthrough"


# ═══════════════════════════════════════════════════════════════════════════
# Data models
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ChainStep:
    """A single atomic action inside a chain plan."""

    action: ActionType
    symbol: str | None = None           # token symbol, e.g. "ARC"
    amount: AmountSpec | None = None    # parsed amount expression
    target: str | None = None           # recipient, validator, pool, game name, etc.
    confidence: float = 0.0             # 0.0 - 1.0, how confident the parser is
    params: dict[str, Any] = field(default_factory=dict)
    source_text: str = ""
    depends_on: list[int] = field(default_factory=list)
    operator: ChainOperator = ChainOperator.SEQ   # how this step is linked to the *next*
    status: StepStatus = StepStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int = 0
    expected_outcome: str = ""
    parallel_group: int | None = None             # steps with the same group run concurrently

    # ── Convenience properties ──────────────────────────────────────────

    @property
    def finished(self) -> bool:
        return self.status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED)

    @property
    def ok(self) -> bool:
        return self.status == StepStatus.SUCCEEDED

    @property
    def is_mutating(self) -> bool:
        return self.action != ActionType.QUERY


@dataclass
class ChainPlan:
    """An ordered list of steps to execute, optionally after a delay."""

    steps: list[ChainStep] = field(default_factory=list)
    delay_seconds: float = 0.0
    user_id: int = 0
    guild_id: int = 0
    channel_id: int = 0
    message_id: int = 0
    created_at: float = field(default_factory=time.time)
    confirmed: bool = False
    source: str = ""          # "intent_map", "ai_think", "manual"
    raw_text: str = ""        # original user input
    requires_confirmation: bool = True
    started_at: float = 0.0
    finished_at: float = 0.0

    # ── Convenience helpers ─────────────────────────────────────────────

    @property
    def all_succeeded(self) -> bool:
        return all(s.status == StepStatus.SUCCEEDED for s in self.steps)

    @property
    def has_failure(self) -> bool:
        return any(s.status == StepStatus.FAILED for s in self.steps)

    @property
    def pending_steps(self) -> list[ChainStep]:
        return [s for s in self.steps if s.status == StepStatus.PENDING]

    @property
    def is_mutating(self) -> bool:
        return any(s.is_mutating for s in self.steps)

    @property
    def min_confidence(self) -> float:
        if not self.steps:
            return 0.0
        return min(s.confidence for s in self.steps)

    @property
    def succeeded_count(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.SUCCEEDED)

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.FAILED)

    @property
    def all_failed(self) -> bool:
        return all(s.status == StepStatus.FAILED for s in self.steps)

    @property
    def summary(self) -> str:
        ok = sum(1 for s in self.steps if s.ok)
        total = len(self.steps)
        return f"{ok}/{total} steps succeeded"


# ═══════════════════════════════════════════════════════════════════════════
# Amount resolution
# ═══════════════════════════════════════════════════════════════════════════

class AmountResolver:
    """Resolve dynamic :class:`AmountSpec` values into concrete floats at
    execution time using live database state.

    Resolution rules:
        - **is_all**:  full balance of the relevant symbol
        - **is_rest**: full balance minus amounts consumed by prior steps
        - **is_fraction**: ``fraction_value * full_balance``
        - **depends_on_step**: output amount from that step's result
        - **already resolved**: returned as-is
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    # ── Public API ──────────────────────────────────────────────────────

    async def resolve(
        self,
        spec: AmountSpec,
        plan: ChainPlan,
        step_idx: int,
    ) -> float:
        """Resolve *spec* to a concrete float.

        Raises :class:`ValueError` if resolution fails (e.g. zero balance
        when ``is_all`` is set).
        """
        # Already a concrete number with no dynamic flags
        if spec.resolved is not None and not spec.needs_resolution:
            return spec.resolved

        step = plan.steps[step_idx]
        symbol = self._symbol_for_step(step)
        # For move actions, the balance source is the "from"/"source" param,
        # not the generic "location" key.  Map "wallet" to "defi_wallet" so
        # _get_balance queries DeFi wallet_holdings instead of CeFi holdings.
        if step.action == ActionType.MOVE:
            raw_loc = (
                step.params.get("from")
                or step.params.get("source")
                or step.params.get("location", "bank")
            )
            location = "defi_wallet" if raw_loc == "wallet" else raw_loc
        else:
            location = step.params.get("location", "wallet")

        # Dependent on a prior step's output
        if spec.depends_on_step is not None:
            return self._resolve_from_prior_step(spec, plan)

        balance = await self._get_balance(plan.guild_id, plan.user_id, symbol, location)

        if spec.is_all:
            if balance <= 0:
                raise ValueError(f"Cannot use 'all' \u2014 {symbol} balance is 0")
            spec.resolved = balance
            return balance

        if spec.is_rest:
            consumed = self._consumed_by_prior_steps(plan, step_idx, symbol)
            remaining = balance - consumed
            if remaining <= 0:
                raise ValueError(
                    f"Cannot use 'rest' \u2014 {symbol} balance ({balance:,.4f}) "
                    f"minus prior steps ({consumed:,.4f}) leaves nothing"
                )
            spec.resolved = remaining
            return remaining

        if spec.is_fraction and spec.fraction_value is not None:
            amount = spec.fraction_value * balance
            if amount <= 0:
                raise ValueError(
                    f"Fraction {spec.fraction_value} of {symbol} balance "
                    f"({balance:,.4f}) is zero"
                )
            spec.resolved = amount
            return amount

        # Fallback: spec has a resolved value after all
        if spec.resolved is not None:
            return spec.resolved

        raise ValueError(f"Cannot resolve amount spec: {spec.raw!r}")

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _symbol_for_step(step: ChainStep) -> str:
        """Extract the primary symbol from a step's fields and params."""
        # Check the direct field first
        if step.symbol:
            return step.symbol.upper()
        # Then check params
        for key in ("symbol", "token", "symbol_in", "token_in", "coin"):
            if key in step.params:
                return str(step.params[key]).upper()
        # For move/transfer actions the symbol may be "DIS" (the base currency)
        if step.action in (ActionType.MOVE, ActionType.DEPOSIT, ActionType.WITHDRAW):
            return step.params.get("currency", "DIS").upper()
        return "DIS"

    @staticmethod
    def _resolve_from_prior_step(spec: AmountSpec, plan: ChainPlan) -> float:
        """Pull the output amount from a prior step's result dict."""
        dep_idx = spec.depends_on_step
        if dep_idx is None or dep_idx < 0 or dep_idx >= len(plan.steps):
            raise ValueError(f"Invalid depends_on_step index: {dep_idx}")
        dep_step = plan.steps[dep_idx]
        if dep_step.result is None:
            raise ValueError(
                f"Step {dep_idx} has no result yet \u2014 "
                f"cannot resolve dependent amount"
            )
        for key in ("amount_out", "amount", "received", "output"):
            if key in dep_step.result:
                value = float(dep_step.result[key])
                spec.resolved = value
                return value
        raise ValueError(
            f"Step {dep_idx} result has no recognisable output amount"
        )

    async def _get_balance(
        self,
        guild_id: int,
        user_id: int,
        symbol: str,
        location: str = "wallet",
    ) -> float:
        """Fetch the user's balance for *symbol* as a human-readable float.

        All balance columns (wallet, bank, holdings, wallet_holdings) are
        stored as raw NUMERIC(36,0) integers scaled by 10**18. Callers of
        this resolver expect a human-readable float (so ``is_all`` sets the
        spec.resolved to a value the service layer interprets as a token
        quantity), so we convert via ``to_human`` here. Returning the raw
        scaled value would silently pass 10**20-scale amounts downstream and
        blow up balance checks.
        """
        from core.framework.scale import to_human

        if symbol == "DIS" or symbol == "":
            user = await self.db.users.get_user(user_id, guild_id)
            if user is None:
                return 0.0
            if location == "bank":
                return to_human(int(user.get("bank", 0) or 0))
            return to_human(int(user.get("wallet", 0) or 0))

        if location == "defi_wallet":
            all_wh = await self.db.users.get_all_wallet_holdings(user_id, guild_id)
            total_raw = sum(
                int(h.get("amount", 0) or 0)
                for h in all_wh
                if h.get("symbol", "").upper() == symbol
            )
            return to_human(total_raw)

        holding = await self.db.users.get_holding(user_id, guild_id, symbol)
        if holding is None:
            return 0.0
        return to_human(int(holding.get("amount", 0) or 0))

    @staticmethod
    def _consumed_by_prior_steps(
        plan: ChainPlan,
        current_idx: int,
        symbol: str,
    ) -> float:
        """Sum amounts consumed by steps prior to *current_idx* that
        operate on the same *symbol*."""
        total = 0.0
        for i, step in enumerate(plan.steps):
            if i >= current_idx:
                break
            if step.status != StepStatus.SUCCEEDED:
                continue
            if step.amount is None or step.amount.resolved is None:
                continue
            step_symbol = AmountResolver._symbol_for_step(step)
            if step_symbol == symbol:
                total += step.amount.resolved
        return total


# ═══════════════════════════════════════════════════════════════════════════
# Step execution
# ═══════════════════════════════════════════════════════════════════════════

MAX_RETRIES: int = 1


class ChainExecutor:
    """Drive a :class:`ChainPlan` to completion, step by step.

    For each step the executor:

    1. Resolves the amount via :class:`AmountResolver`
    2. Pre-validates via StepValidator
    3. Executes via ServiceBridge
    4. Post-validates the result
    5. On error: retries once, then marks the step as failed
    6. Calls *on_progress* after each step completes

    Supports advanced chain operators (>;&&||+|) for conditional,
    parallel, and piped execution flows.
    """

    def __init__(
        self,
        db: Any,
        plan: ChainPlan,
        on_progress: Callable[[ChainPlan, int], Any] | None = None,
        *,
        bridge: Any | None = None,
        validator: Any | None = None,
    ) -> None:
        self.db = db
        self.plan = plan
        self.on_progress = on_progress
        self.resolver = AmountResolver(db)
        self._bridge = bridge      # ServiceBridge instance
        self._validator = validator  # StepValidator instance

    async def _pre_validate(self, step: ChainStep, plan: ChainPlan) -> tuple[bool, str]:
        if self._validator is not None:
            return await self._validator.pre_validate(step, plan)
        return True, ""

    async def _post_validate(self, step: ChainStep, plan: ChainPlan) -> tuple[bool, str]:
        if self._validator is not None:
            return await self._validator.post_validate(step, plan)
        return True, ""

    async def _execute_action(self, step: ChainStep, plan: ChainPlan) -> dict[str, Any]:
        if self._bridge is not None:
            return await self._bridge.execute(step, plan)
        # Fallback stub for testing
        return {
            "ok": True,
            "type": "text",
            "action": step.action.value,
            "amount": step.amount.resolved if step.amount else 0.0,
            "content": f"{describe_step(step)} \u2014 executed",
            "params": step.params,
        }

    # ── Main loop ───────────────────────────────────────────────────────

    async def execute(self) -> ChainPlan:
        """Execute every step in the plan, respecting chain operators.

        Operator semantics between step N and step N+1:
            >  / &&   -  continue only if step N succeeded
            ;         -  always continue regardless of outcome
            ||        -  continue only if step N *failed*
            |         -  continue if step N succeeded; inject result into N+1
            +         -  run steps in the same parallel group concurrently
        """
        self.plan.started_at = time.time()

        idx = 0
        while idx < len(self.plan.steps):
            step = self.plan.steps[idx]
            if step.finished:
                idx += 1
                continue

            # ── Parallel group: gather contiguous + steps ────────────
            if step.parallel_group is not None:
                group_id = step.parallel_group
                group_indices = [
                    i for i, s in enumerate(self.plan.steps)
                    if s.parallel_group == group_id and not s.finished
                ]
                await self._run_parallel_group(group_indices)
                idx = max(group_indices) + 1
                continue

            # ── Check dependencies ──────────────────────────────────
            if not self._deps_satisfied(idx):
                step.status = StepStatus.SKIPPED
                step.error = "Skipped: dependency failed"
                await self._notify_progress(idx)
                idx += 1
                continue

            # ── Pipe: inject prior result ────────────────────────────
            prev_op = self.plan.steps[idx - 1].operator if idx > 0 else None
            if prev_op == ChainOperator.PIPE and idx > 0:
                prev_step = self.plan.steps[idx - 1]
                if prev_step.result:
                    step.params["_piped"] = prev_step.result

            # ── Run the step ────────────────────────────────────────
            await self._run_step(idx, step)
            await self._notify_progress(idx)

            # ── Decide whether to continue ──────────────────────────
            if idx + 1 < len(self.plan.steps):
                next_step = self.plan.steps[idx + 1]
                should_continue = self._should_continue(step, next_step)
                if not should_continue:
                    self._skip_remaining(idx + 1)
                    break

            idx += 1

        self.plan.finished_at = time.time()
        return self.plan

    def _should_continue(self, current: ChainStep, next_step: ChainStep) -> bool:
        """Decide if the chain should proceed to *next_step* based on the
        operator that links *current* → *next_step*.

        The operator stored on the *current* step describes how to transition
        to the next.
        """
        op = current.operator

        if op in (ChainOperator.SEQ, ChainOperator.AND, ChainOperator.PIPE):
            return current.status == StepStatus.SUCCEEDED

        if op == ChainOperator.FIRE:
            return True  # always continue

        if op == ChainOperator.OR:
            return current.status == StepStatus.FAILED

        if op == ChainOperator.PARA:
            return True  # parallel groups handled separately

        return current.status == StepStatus.SUCCEEDED

    async def _run_parallel_group(self, indices: list[int]) -> None:
        """Execute a group of steps concurrently via asyncio.gather."""
        async def _wrapped(i: int) -> None:
            step = self.plan.steps[i]
            step.status = StepStatus.RUNNING
            await self._run_step(i, step)
            await self._notify_progress(i)

        await asyncio.gather(*[_wrapped(i) for i in indices])

    # ── Step runner ─────────────────────────────────────────────────────

    async def _run_step(self, idx: int, step: ChainStep) -> None:
        """Run a single step with retry logic."""
        step.status = StepStatus.RUNNING

        for attempt in range(1 + MAX_RETRIES):
            try:
                # 1. Resolve amount
                if step.amount is not None and step.amount.needs_resolution:
                    await self.resolver.resolve(step.amount, self.plan, idx)

                # 2. Pre-validate
                valid, reason = await self._pre_validate(step, self.plan)
                if not valid:
                    raise ValueError(f"Pre-validation failed: {reason}")

                # 3. Execute
                result = await self._execute_action(step, self.plan)

                # 4. Post-validate
                step.result = result
                valid, reason = await self._post_validate(step, self.plan)
                if not valid:
                    log.warning("Post-validation failed for step %d: %s", idx, reason)

                # Success
                step.status = StepStatus.SUCCEEDED
                return

            except Exception as exc:
                step.retry_count = attempt
                step.error = str(exc)
                log.warning(
                    "Chain step %d (%s) attempt %d failed: %s",
                    idx, step.action.value, attempt + 1, exc,
                )

                if attempt < MAX_RETRIES:
                    step.status = StepStatus.RETRYING
                    await asyncio.sleep(0.5)  # brief pause before retry
                else:
                    step.status = StepStatus.FAILED

    # ── Helpers ─────────────────────────────────────────────────────────

    def _deps_satisfied(self, idx: int) -> bool:
        """Return True if all dependencies of step *idx* have succeeded."""
        step = self.plan.steps[idx]
        for dep_idx in step.depends_on:
            if dep_idx < 0 or dep_idx >= len(self.plan.steps):
                return False
            if self.plan.steps[dep_idx].status != StepStatus.SUCCEEDED:
                return False
        return True

    def _skip_remaining(self, from_idx: int) -> None:
        """Mark all steps from *from_idx* onward as SKIPPED."""
        for step in self.plan.steps[from_idx:]:
            if not step.finished:
                step.status = StepStatus.SKIPPED
                step.error = "Skipped: prior step failed"

    async def _notify_progress(self, step_idx: int) -> None:
        """Invoke the progress callback if one was provided."""
        if self.on_progress is None:
            return
        try:
            result = self.on_progress(self.plan, step_idx)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("on_progress callback error for step %d", step_idx)


# ═══════════════════════════════════════════════════════════════════════════
# Delayed execution scheduler
# ═══════════════════════════════════════════════════════════════════════════

class ChainScheduler:
    """Manage delayed chain execution via asyncio tasks.

    Each scheduled chain is tracked by its ``message_id`` so that users
    can cancel pending chains before they fire.
    """

    def __init__(self) -> None:
        self._pending: dict[int, asyncio.Task[None]] = {}  # message_id -> task

    async def schedule(
        self,
        plan: ChainPlan,
        execute_fn: Callable[[ChainPlan], Any],
    ) -> None:
        """Schedule *plan* for execution after its ``delay_seconds``.

        If ``delay_seconds`` is zero the plan is executed immediately.
        """
        if plan.delay_seconds <= 0:
            result = execute_fn(plan)
            if asyncio.iscoroutine(result):
                await result
            return

        task = asyncio.create_task(
            self._delayed_run(plan, execute_fn),
            name=f"chain-delay-{plan.message_id}",
        )
        self._pending[plan.message_id] = task
        task.add_done_callback(lambda _t: self._pending.pop(plan.message_id, None))

    def cancel(self, message_id: int) -> bool:
        """Cancel a pending chain by message ID.

        Returns True if a pending chain was found and cancelled.
        """
        task = self._pending.pop(message_id, None)
        if task is None:
            return False
        task.cancel()
        return True

    @property
    def pending_count(self) -> int:
        """Number of chains waiting to fire."""
        return len(self._pending)

    def pending_message_ids(self) -> list[int]:
        """List message IDs of all pending chains."""
        return list(self._pending.keys())

    # ── Internal ────────────────────────────────────────────────────────

    async def _delayed_run(
        self,
        plan: ChainPlan,
        execute_fn: Callable[[ChainPlan], Any],
    ) -> None:
        """Wait for the delay period then execute the plan."""
        try:
            await asyncio.sleep(plan.delay_seconds)
            result = execute_fn(plan)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            log.info(
                "Delayed chain %d cancelled (user_id=%d)",
                plan.message_id, plan.user_id,
            )
        except Exception:
            log.exception(
                "Delayed chain %d failed (user_id=%d)",
                plan.message_id, plan.user_id,
            )


# ═══════════════════════════════════════════════════════════════════════════
# Human-readable formatting helpers
# ═══════════════════════════════════════════════════════════════════════════

_ACTION_VERBS: dict[ActionType, str] = {
    ActionType.BUY: "Buy",
    ActionType.SELL: "Sell",
    ActionType.SWAP: "Swap",
    ActionType.TRANSFER: "Transfer",
    ActionType.MOVE: "Move",
    ActionType.DEPOSIT: "Deposit",
    ActionType.WITHDRAW: "Withdraw",
    ActionType.STAKE: "Stake",
    ActionType.UNSTAKE: "Unstake",
    ActionType.DELEGATE: "Delegate",
    ActionType.UNDELEGATE: "Undelegate",
    ActionType.ADD_LP: "Add liquidity for",
    ActionType.REMOVE_LP: "Remove liquidity from",
    ActionType.SAVE_DEPOSIT: "Deposit into savings",
    ActionType.SAVE_WITHDRAW: "Withdraw from savings",
    ActionType.BUY_RIG: "Buy mining rig",
    ActionType.SHOP_BUY: "Buy from shop",
    ActionType.PLAY_GAME: "Play",
    ActionType.QUERY: "Check",
    ActionType.CREATE_WALLET: "Create wallet",
    ActionType.SET_NOTIFICATION: "Set notification",
    ActionType.PASSTHROUGH: "Run",
}

_PAST_VERBS: dict[ActionType, str] = {
    ActionType.BUY: "Bought",
    ActionType.SELL: "Sold",
    ActionType.SWAP: "Swapped",
    ActionType.TRANSFER: "Transferred",
    ActionType.MOVE: "Moved",
    ActionType.DEPOSIT: "Deposited",
    ActionType.WITHDRAW: "Withdrew",
    ActionType.STAKE: "Staked",
    ActionType.UNSTAKE: "Unstaked",
    ActionType.DELEGATE: "Delegated",
    ActionType.UNDELEGATE: "Undelegated",
    ActionType.ADD_LP: "Added liquidity",
    ActionType.REMOVE_LP: "Removed liquidity",
    ActionType.SAVE_DEPOSIT: "Saved",
    ActionType.SAVE_WITHDRAW: "Withdrew savings",
    ActionType.BUY_RIG: "Bought rig",
    ActionType.SHOP_BUY: "Bought item",
    ActionType.PLAY_GAME: "Played",
    ActionType.QUERY: "Checked",
    ActionType.CREATE_WALLET: "Created wallet",
    ActionType.SET_NOTIFICATION: "Set notification",
    ActionType.PASSTHROUGH: "Ran",
}


def _format_amount(spec: AmountSpec | None) -> str:
    """Render an amount spec as a short readable string."""
    if spec is None:
        return ""
    if spec.resolved is not None:
        if spec.is_usd:
            return f"${spec.resolved:,.2f}"
        return f"{spec.resolved:,.4f}".rstrip("0").rstrip(".")
    if spec.is_all:
        return "all"
    if spec.is_rest:
        return "the rest"
    if spec.is_fraction and spec.fraction_value is not None:
        pct = spec.fraction_value * 100
        if pct == int(pct):
            return f"{int(pct)}%"
        return f"{pct:.1f}%"
    return spec.raw


def describe_step(step: ChainStep) -> str:
    """Return a human-readable one-line description of a chain step.

    Examples::

        "Buy 100 MTA"
        "Swap 50% ARC -> USDC"
        "Stake all ARC with validator1"
        "Add liquidity 100 ARC / USDC"
        "Play coinflip 50 USD"
        "Create wallet arc"
    """
    # Passthrough: just show the raw command text
    if step.action == ActionType.PASSTHROUGH:
        return f"`{step.source_text}`" if step.source_text else "Run command"

    verb = _ACTION_VERBS.get(step.action, step.action.value.replace("_", " ").title())
    parts: list[str] = [verb]

    amount_str = _format_amount(step.amount)

    # ── Liquidity pool: show token pair ──────────────────────────────
    if step.action in (ActionType.ADD_LP, ActionType.REMOVE_LP):
        token_a = step.symbol or step.params.get("token_a") or ""
        token_b = step.target or step.params.get("token_b") or ""
        if amount_str:
            parts.append(amount_str)
        if token_a and token_b:
            parts.append(f"{token_a.upper()} / {token_b.upper()}")
        elif token_a:
            parts.append(token_a.upper())
        return " ".join(parts)

    # ── Play game: show game name and bet ─────────────────────────────
    if step.action == ActionType.PLAY_GAME:
        game = step.target or step.params.get("game") or "game"
        if amount_str:
            parts.append(amount_str)
        token = step.symbol or step.params.get("token") or "USD"
        parts.append(f"{token.upper()} on {game}")
        return " ".join(parts)

    # ── Create wallet: show network ───────────────────────────────────
    if step.action == ActionType.CREATE_WALLET:
        network = step.target or step.params.get("network") or ""
        if network:
            parts.append(network)
        return " ".join(parts)

    # ── Set notification: show type and symbol ────────────────────────
    if step.action == ActionType.SET_NOTIFICATION:
        notif_type = step.target or step.params.get("notification_type") or ""
        value = step.params.get("value", "on")
        sym = step.symbol or step.params.get("symbol") or ""
        if notif_type:
            parts.append(notif_type)
        if sym:
            parts.append(sym.upper())
        parts.append(f"({value})")
        return " ".join(parts)

    # ── Buy rig: show rig type ────────────────────────────────────────
    if step.action == ActionType.BUY_RIG:
        rig_type = step.target or step.params.get("rig_type") or "basic"
        if amount_str:
            parts.append(f"{amount_str}x")
        parts.append(rig_type)
        return " ".join(parts)

    # ── Shop buy: show item ───────────────────────────────────────────
    if step.action == ActionType.SHOP_BUY:
        item = step.target or step.params.get("item") or ""
        if amount_str:
            parts.append(f"{amount_str}x")
        if item:
            parts.append(item)
        return " ".join(parts)

    # ── Query: show what's being queried ──────────────────────────────
    if step.action == ActionType.QUERY:
        sym = step.symbol or ""
        query_type = step.params.get("query_type", "")
        if sym:
            parts.append(f"{sym.upper()} price" if "price" in query_type else sym.upper())
        elif query_type:
            parts.append(query_type.replace(".", " ").replace("_", " "))
        return " ".join(parts)

    # ── Default path ──────────────────────────────────────────────────
    if amount_str:
        parts.append(amount_str)

    sym = step.symbol or step.params.get("symbol") or step.params.get("token") or ""
    if sym:
        parts.append(str(sym).upper())

    # Swap: show arrow
    symbol_out = step.params.get("symbol_out") or step.params.get("token_out")
    if symbol_out:
        parts.append("->")
        parts.append(str(symbol_out).upper())

    # Move/transfer: show source -> destination
    src = step.params.get("from") or step.params.get("source")
    dst = step.target or step.params.get("to") or step.params.get("destination")
    if src and dst:
        parts.append(f"from {src} to {dst}")
    elif dst and step.action not in (ActionType.STAKE, ActionType.UNSTAKE,
                                     ActionType.DELEGATE, ActionType.UNDELEGATE):
        parts.append(f"to {dst}")

    # Validator for staking/delegation
    validator = step.params.get("validator") or step.params.get("validator_name")
    if not validator and step.action in (ActionType.STAKE, ActionType.UNSTAKE,
                                          ActionType.DELEGATE, ActionType.UNDELEGATE):
        validator = step.target
    if validator:
        parts.append(f"with {validator}")

    # Savings: show symbol
    if step.action in (ActionType.SAVE_DEPOSIT, ActionType.SAVE_WITHDRAW):
        pass  # symbol already appended above

    return " ".join(parts)


def describe_result(step: ChainStep) -> str:
    """Return a human-readable one-line summary of a step's outcome.

    Examples::

        "Bought 100 MTA @ $45,000"
        "Failed: Insufficient balance"
        "Skipped: prior step failed"
    """
    if step.status == StepStatus.FAILED:
        return f"Failed: {step.error or 'unknown error'}"

    if step.status == StepStatus.SKIPPED:
        return f"Skipped: {step.error or 'dependency not met'}"

    if step.status == StepStatus.PENDING:
        return "Pending"

    if step.status == StepStatus.RUNNING:
        return "Running..."

    if step.status == StepStatus.RETRYING:
        return f"Retrying (attempt {step.retry_count + 1})..."

    # SUCCEEDED
    if step.result is None:
        return "Succeeded"

    # Build a useful summary from the result
    parts: list[str] = []
    verb = _PAST_VERBS.get(step.action, "Completed")
    parts.append(verb)

    # ── Liquidity pool results ────────────────────────────────────────
    if step.action in (ActionType.ADD_LP, ActionType.REMOVE_LP):
        token_a = step.result.get("token_a", step.symbol or "")
        token_b = step.result.get("token_b", step.target or "")
        amt_a = step.result.get("amount_a")
        amt_b = step.result.get("amount_b")
        lp = step.result.get("lp_tokens") or step.result.get("lp_tokens_burned")
        if amt_a and token_a:
            parts.append(f"{float(amt_a):,.4f}".rstrip("0").rstrip("."))
            parts.append(str(token_a).upper())
        if amt_b and token_b:
            parts.append("+")
            parts.append(f"{float(amt_b):,.4f}".rstrip("0").rstrip("."))
            parts.append(str(token_b).upper())
        if lp:
            parts.append(f"({float(lp):,.4f}".rstrip("0").rstrip(".") + " LP)")
        return " ".join(parts)

    # ── Savings results ───────────────────────────────────────────────
    if step.action in (ActionType.SAVE_DEPOSIT, ActionType.SAVE_WITHDRAW):
        amt = step.result.get("amount")
        sym = step.result.get("symbol") or step.symbol or "USD"
        balance = step.result.get("new_savings_balance")
        if amt:
            parts.append(f"{float(amt):,.4f}".rstrip("0").rstrip("."))
        if sym:
            parts.append(str(sym).upper())
        if balance is not None:
            parts.append(f"(savings: {float(balance):,.2f})")
        return " ".join(parts)

    # ── Game results ──────────────────────────────────────────────────
    if step.action == ActionType.PLAY_GAME:
        game = step.result.get("game") or step.params.get("game") or "game"
        parts.append(game)
        return " ".join(parts)

    # ── Wallet creation ───────────────────────────────────────────────
    if step.action == ActionType.CREATE_WALLET:
        network = step.result.get("network") or step.target or ""
        already = step.result.get("already_exists", False)
        if network:
            parts.append(network)
        if already:
            parts.append("(already existed)")
        return " ".join(parts)

    # ── Shop buy ──────────────────────────────────────────────────────
    if step.action == ActionType.SHOP_BUY:
        item = step.result.get("item") or step.target or ""
        qty = step.result.get("quantity", 1)
        cost = step.result.get("cost")
        if qty and qty > 1:
            parts.append(f"{qty}x")
        if item:
            parts.append(item)
        if cost:
            parts.append(f"for ${float(cost):,.2f}")
        return " ".join(parts)

    # ── Buy rig ───────────────────────────────────────────────────────
    if step.action == ActionType.BUY_RIG:
        rig = step.result.get("rig_type") or step.target or "rig"
        parts.append(rig)
        return " ".join(parts)

    # ── Default path ──────────────────────────────────────────────────
    amount = step.result.get("amount") or step.result.get("amount_out")
    if amount is not None:
        parts.append(f"{float(amount):,.4f}".rstrip("0").rstrip("."))

    sym = step.symbol or step.params.get("symbol") or step.params.get("token") or ""
    if sym:
        parts.append(str(sym).upper())

    price = step.result.get("price") or step.result.get("price_at")
    if price is not None:
        parts.append(f"@ ${float(price):,.2f}")

    tx_hash = step.result.get("tx_hash")
    if tx_hash:
        short_hash = str(tx_hash)[:8]
        parts.append(f"(tx:{short_hash})")

    # ── Gas and fee details ──────────────────────────────────────────
    gas = step.result.get("gas")
    if gas is not None:
        gas_coin = step.result.get("gas_coin", "")
        parts.append(f"⛽{float(gas):,.4f}".rstrip("0").rstrip(".") + (f" {gas_coin}" if gas_coin else ""))

    fee = step.result.get("fee") or step.result.get("platform_fee")
    if fee is not None:
        fee_coin = step.result.get("fee_coin", "USD")
        parts.append(f"💸${float(fee):,.2f}" if fee_coin == "USD" else f"💸{float(fee):,.4f} {fee_coin}")

    reserve = step.result.get("reserve_amount")
    if reserve is not None:
        parts.append(f"🏦→${float(reserve):,.2f}")

    return " ".join(parts)


def format_duration(seconds: float) -> str:
    """Convert *seconds* into a human-friendly duration string.

    Examples::

        format_duration(90)      -> "1 minute 30 seconds"
        format_duration(3661)    -> "1 hour 1 minute"
        format_duration(86400)   -> "1 day"
        format_duration(0)       -> "0 seconds"
    """
    if seconds <= 0:
        return "0 seconds"

    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []

    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    # Only show seconds if the total is under 1 hour, or if nothing else matched
    if secs and total < 3600:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    elif not parts:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")

    return " ".join(parts)
