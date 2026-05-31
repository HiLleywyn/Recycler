"""
cogs/command_chain.py  -  Advanced command chain processing with operator-aware execution.

Intercepts messages that contain chain syntax, parses them into a ChainPlan,
displays a confirmation embed, executes step-by-step with operator-specific
semantics, and provides interactive error recovery when a link breaks.

Chain operators:
    >    -  sequential: next runs only if previous succeeded
    &&   -  strict AND: same as > but explicit
    ;    -  fire-and-forget: next runs regardless of outcome
    ||   -  fallback OR: next runs only if previous *failed*
    |    -  pipe: like > but injects previous result into next step
    +    -  parallel: run adjacent steps concurrently

Syntax examples:
    .buy 10 MTA > .move all mta b w
    .sell half ARC ; .deposit all
    .buy 10 MTA || .buy 10 ARC
    .sell MTA | .buy ARC
    .buy MTA + .buy ARC + .buy DSC > .move all b w
    .buy 100 MTA > .stake all MTA cosmosval1 in 5m

Error recovery flow:
    1. Chain stops at the failed link
    2. Error is diagnosed and displayed with context
    3. A recovery button appears (e.g. "Create Wallet")
    4. On resolution: "Resolved. Continuing..." and the chain resumes
"""
from __future__ import annotations

import asyncio
import copy
import time

import discord
from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.chain_engine import (
    ActionType,
    ChainOperator,
    ChainPlan,
    ChainStep,
    StepStatus,
    describe_step,
    describe_result,
    format_duration,
)
from core.framework.chain_parser import ChainSyntaxParser, CHAIN_LINK_COOLDOWN
from core.framework.command_parser import ParseResult
from core.framework.delay_parser import parse_delay
from core.framework.error_index import ErrorIndex, classify_error, ErrorCategory
from core.framework.embed import card
from core.framework.service_bridge import ServiceBridge
from core.framework.step_validator import StepValidator
from core.framework.ui import C_ERROR, C_INFO, C_NAVY, C_SUCCESS, C_NEUTRAL


# ── Status icons ─────────────────────────────────────────────────────────

_ICONS = {
    StepStatus.PENDING:   "⬜",
    StepStatus.RUNNING:   "🔄",
    StepStatus.SUCCEEDED: "✅",
    StepStatus.FAILED:    "❌",
    StepStatus.SKIPPED:   "⏭️",
    StepStatus.RETRYING:  "🔁",
}

_OP_LABELS: dict[ChainOperator, str] = {
    ChainOperator.SEQ:  ">",
    ChainOperator.AND:  "&&",
    ChainOperator.FIRE: ";",
    ChainOperator.OR:   "||",
    ChainOperator.PIPE: "|",
    ChainOperator.PARA: "+",
}


class CommandChain(commands.Cog):
    """Chain command processing with interactive error recovery."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self.error_index = ErrorIndex()
        self._active_chains: dict[int, ChainPlan] = {}  # message_id -> plan
        self._scheduler = None  # lazy init ChainScheduler

    # ════════════════════════════════════════════════════════════════════
    #  Message listener  -  intercept chain syntax
    # ════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Detect chain syntax in messages and process them."""
        if message.author.bot or not message.content:
            return

        content = message.content

        # Cheapest possible pre-filter: skip messages with no chain operators at all.
        # This eliminates the vast majority of normal chat messages with zero DB calls.
        if not any(op in content for op in (">", ";", "&&", "||")):
            return

        # Fetch guild prefix (1 DB call, only for messages that look chain-like)
        prefix = Config.PREFIX
        if message.guild and hasattr(self.bot, "db"):
            try:
                settings = await self.bot.db.get_guild_settings(message.guild.id)
                if settings.get("prefix"):
                    prefix = settings["prefix"]
            except Exception:
                pass

        if not content.startswith(prefix):
            return

        after_prefix = content[len(prefix):]

        # Only process if there's a chain operator after the prefix
        if not any(op in after_prefix for op in (">", ";", "|", "&&", "||", "+")):
            return

        # Command chaining is a beta feature  -  check access only after confirming this looks like a chain
        if message.guild:
            from core.framework.middleware import check_beta_access
            member = message.author if isinstance(message.author, discord.Member) else message.guild.get_member(message.author.id)
            if not await check_beta_access(self.bot, message.guild, member, "command_chains"):
                return

        # Build parser with known tokens
        known_tokens = set(Config.TOKENS.keys())
        if message.guild and hasattr(self.bot, "db"):
            try:
                guild_tokens = await self.bot.db.get_all_tokens_for_guild(message.guild.id)
                known_tokens = set(guild_tokens.keys())
            except Exception:
                pass

        parser = ChainSyntaxParser(known_tokens=known_tokens, prefix=prefix)

        # Check it's actually a chain (not just a comparison operator in chat)
        if not parser.is_chain(after_prefix):
            return

        # Extract delay before parsing (e.g. "buy 10 MTA > move all b w in 5m")
        delay, cleaned_input = parse_delay(after_prefix)

        result = parser.parse(
            cleaned_input,
            user_id=message.author.id,
            guild_id=message.guild.id if message.guild else 0,
            channel_id=message.channel.id,
            message_id=message.id,
        )

        if isinstance(result, ParseResult):
            # Parse error on the first link
            _b = card("❌ Chain Error", description=result.error or "Could not parse chain.").color(C_ERROR)
            if result.suggestion:
                _b.field("Did you mean?", f"`{result.suggestion}`")
            await message.reply(embed=_b.build(), mention_author=False)
            return

        plan: ChainPlan = result
        plan.delay_seconds = delay

        # Show confirmation and execute
        await self._confirm_and_execute(message, plan, prefix)

    # ════════════════════════════════════════════════════════════════════
    #  Confirmation UI
    # ════════════════════════════════════════════════════════════════════

    async def _confirm_and_execute(
        self, message: discord.Message, plan: ChainPlan, prefix: str
    ) -> None:
        """Show a chain preview and execute on confirmation."""
        title = "⛓️ Command Chain"
        if plan.delay_seconds > 0:
            title += f" (scheduled: {format_duration(plan.delay_seconds)})"

        embed = self._build_plan_embed(plan, title=title)
        view = _ChainConfirmView(message.author.id)

        conf_msg = await message.reply(embed=embed, view=view, mention_author=False)
        confirmed = await view.wait_result()

        if not confirmed:
            cancel_embed = card("", description="Chain cancelled.").color(C_NEUTRAL).build()
            await conf_msg.edit(embed=cancel_embed, view=None)
            return

        # Handle delayed execution
        if plan.delay_seconds > 0:
            from core.framework.chain_engine import ChainScheduler

            if self._scheduler is None:
                self._scheduler = ChainScheduler()

            countdown_embed = (
                card("⏱️ Chain Scheduled",
                     description=f"Executing in **{format_duration(plan.delay_seconds)}**...")
                .color(C_INFO)
                .build()
            )
            await conf_msg.edit(embed=countdown_embed, view=None)

            async def _delayed_exec(p: ChainPlan) -> None:
                await self._execute_chain(message, conf_msg, p, prefix)

            await self._scheduler.schedule(plan, _delayed_exec)
            return

        # Execute immediately
        await self._execute_chain(message, conf_msg, plan, prefix)

    # ════════════════════════════════════════════════════════════════════
    #  Chain execution
    # ════════════════════════════════════════════════════════════════════

    async def _execute_chain(
        self,
        original_msg: discord.Message,
        status_msg: discord.Message,
        plan: ChainPlan,
        prefix: str,
    ) -> None:
        """Execute a chain plan step-by-step, respecting operators."""
        self._active_chains[original_msg.id] = plan
        plan.started_at = time.time()

        # Build ServiceBridge and StepValidator if DB is available
        bridge = None
        validator = None
        if hasattr(self.bot, "db"):
            bridge = ServiceBridge(self.bot.db)
            validator = StepValidator(self.bot.db)

        try:
            idx = 0
            while idx < len(plan.steps):
                step = plan.steps[idx]
                if step.finished:
                    idx += 1
                    continue

                # ── Parallel group: gather concurrent steps ──────────
                if step.parallel_group is not None:
                    group_id = step.parallel_group
                    group_indices = [
                        i for i, s in enumerate(plan.steps)
                        if s.parallel_group == group_id and not s.finished
                    ]

                    # Update all to running
                    for gi in group_indices:
                        plan.steps[gi].status = StepStatus.RUNNING
                    await self._update_status(
                        status_msg, plan,
                        f"Running {len(group_indices)} steps in parallel..."
                    )

                    # Execute concurrently  -  each step gets its own DB connection
                    async def _run_one(i: int) -> bool:
                        # Create fresh bridge/validator for each parallel step to avoid connection sharing
                        step_bridge = None
                        step_validator = None
                        if hasattr(self.bot, "db"):
                            step_bridge = ServiceBridge(self.bot.db)
                            step_validator = StepValidator(self.bot.db)
                        return await self._execute_one_step(
                            original_msg, status_msg, plan, i, prefix,
                            bridge=step_bridge, validator=step_validator,
                        )

                    results = await asyncio.gather(
                        *[_run_one(i) for i in group_indices],
                        return_exceptions=True,
                    )

                    # Check results
                    all_ok = all(
                        r is True for r in results if not isinstance(r, Exception)
                    )
                    for gi, r in zip(group_indices, results):
                        if isinstance(r, Exception):
                            plan.steps[gi].status = StepStatus.FAILED
                            plan.steps[gi].error = str(r)

                    await self._update_status(
                        status_msg, plan,
                        f"Parallel group {'complete' if all_ok else 'finished with errors'}"
                    )

                    idx = max(group_indices) + 1

                    # Cooldown before next step
                    if idx < len(plan.steps):
                        await asyncio.sleep(CHAIN_LINK_COOLDOWN)
                    continue

                # ── Single step execution ────────────────────────────
                step.status = StepStatus.RUNNING
                await self._update_status(status_msg, plan, f"Running step {idx + 1}...")

                success = await self._execute_one_step(
                    original_msg, status_msg, plan, idx, prefix,
                    bridge=bridge, validator=validator,
                )

                if not success:
                    # Decide based on operator whether to continue
                    if idx + 1 < len(plan.steps):
                        should_continue = self._should_continue_after(plan, idx)
                        if not should_continue:
                            for remaining in plan.steps[idx + 1:]:
                                if not remaining.finished:
                                    remaining.status = StepStatus.SKIPPED
                                    remaining.error = "Skipped: prior step failed"
                            await self._update_status(status_msg, plan, "Chain halted")
                            break
                else:
                    # For || (OR) operator: if step succeeded, skip the fallback
                    if idx + 1 < len(plan.steps):
                        if step.operator == ChainOperator.OR:
                            # Skip the next step since current succeeded
                            plan.steps[idx + 1].status = StepStatus.SKIPPED
                            plan.steps[idx + 1].error = "Skipped: fallback not needed"

                # Cooldown between links
                if idx < len(plan.steps) - 1:
                    await asyncio.sleep(CHAIN_LINK_COOLDOWN)

                idx += 1

            plan.finished_at = time.time()
            elapsed = plan.finished_at - plan.started_at

            # Final status
            if plan.all_succeeded:
                final_title = "✅ Chain Complete"
                final_color = C_SUCCESS
                footer = f"All {len(plan.steps)} steps succeeded in {elapsed:.1f}s"
            elif plan.has_failure:
                final_title = "⚠️ Chain Halted"
                final_color = C_ERROR
                footer = f"{plan.summary}  -  halted after {elapsed:.1f}s"
            else:
                final_title = "⛓️ Chain Finished"
                final_color = C_INFO
                footer = f"{plan.summary} in {elapsed:.1f}s"

            embed = self._build_plan_embed(plan, title=final_title, color=final_color)
            embed.set_footer(text=footer)
            await status_msg.edit(embed=embed, view=None)

        finally:
            self._active_chains.pop(original_msg.id, None)

    async def _execute_one_step(
        self,
        original_msg: discord.Message,
        status_msg: discord.Message,
        plan: ChainPlan,
        idx: int,
        prefix: str,
        *,
        bridge: ServiceBridge | None = None,
        validator: StepValidator | None = None,
    ) -> bool:
        """Execute a single step, trying ServiceBridge first, falling back to command replay.

        Returns True on success, False on failure.
        """
        step = plan.steps[idx]

        # ── Pipe: inject prior result data ──────────────────────────
        if idx > 0 and plan.steps[idx - 1].operator == ChainOperator.PIPE:
            prev = plan.steps[idx - 1]
            if prev.result:
                step.params["_piped"] = prev.result
                # Auto-resolve amount from piped output
                if step.amount is None or (step.amount.needs_resolution and step.amount.depends_on_step is not None):
                    for key in ("amount_out", "amount", "received", "output"):
                        if key in prev.result:
                            from core.framework.amount_parser import AmountSpec
                            step.amount = AmountSpec(
                                raw=f"piped:{key}",
                                resolved=float(prev.result[key]),
                            )
                            break

        # ── Passthrough: always use command replay, skip ServiceBridge ──
        if step.action == ActionType.PASSTHROUGH:
            success = await self._execute_step_via_command(original_msg, step, prefix)
            if success:
                step.status = StepStatus.SUCCEEDED
                step.result = {"ok": True}
                await self._update_status(status_msg, plan, f"Step {idx + 1} complete")
            else:
                step.status = StepStatus.FAILED
                self._record_error(step, plan)
                await self._update_status(status_msg, plan, f"Step {idx + 1} failed")
            return success

        # ── Try ServiceBridge first ─────────────────────────────────
        if bridge is not None:
            try:
                # Pre-validate
                if validator:
                    valid, reason = await validator.pre_validate(step, plan)
                    if not valid:
                        step.status = StepStatus.FAILED
                        step.error = reason
                        self._record_error(step, plan)
                        await self._update_status(status_msg, plan, f"Step {idx + 1} failed validation")
                        return False

                # Resolve dynamic amounts
                if step.amount and step.amount.needs_resolution:
                    from core.framework.chain_engine import AmountResolver
                    resolver = AmountResolver(self.bot.db)
                    await resolver.resolve(step.amount, plan, idx)

                # Execute via ServiceBridge
                result = await bridge.execute(step, plan)
                step.status = StepStatus.SUCCEEDED
                step.result = result
                await self._update_status(status_msg, plan, f"Step {idx + 1} complete")

                # Post-validate
                if validator:
                    valid, reason = await validator.post_validate(step, plan)
                    if not valid:
                        log_msg = f"Post-validation warning for step {idx + 1}: {reason}"
                        # Don't fail the step, just log
                        import logging
                        logging.getLogger("discoin.chain").warning(log_msg)

                return True

            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = str(exc)
                self._record_error(step, plan)

                # Attempt interactive recovery
                recovered = await self._interactive_recovery(
                    original_msg, status_msg, plan, step, idx, prefix
                )

                if recovered:
                    step.status = StepStatus.SUCCEEDED
                    step.result = {"ok": True, "recovered": True}
                    await self._update_status(
                        status_msg, plan, f"Step {idx + 1} recovered  -  continuing..."
                    )
                    return True

                await self._update_status(status_msg, plan, f"Step {idx + 1} failed")
                return False

        # ── Fallback: replay as bot command ─────────────────────────
        success = await self._execute_step_via_command(original_msg, step, prefix)

        if success:
            step.status = StepStatus.SUCCEEDED
            step.result = {"ok": True}
            await self._update_status(status_msg, plan, f"Step {idx + 1} complete")
        else:
            step.status = StepStatus.FAILED
            self._record_error(step, plan)

            recovered = await self._interactive_recovery(
                original_msg, status_msg, plan, step, idx, prefix
            )
            if recovered:
                step.status = StepStatus.SUCCEEDED
                step.result = {"ok": True, "recovered": True}
                await self._update_status(
                    status_msg, plan, f"Step {idx + 1} recovered  -  continuing..."
                )
                return True

            await self._update_status(status_msg, plan, f"Step {idx + 1} failed")

        return success

    def _record_error(self, step: ChainStep, plan: ChainPlan) -> None:
        """Record a step failure in both the chain error index and the bot error tracker."""
        self.error_index.record(
            step.error or "Unknown error",
            step=step,
            plan=plan,
            user_id=plan.user_id,
            guild_id=plan.guild_id,
        )
        # Also record in the unified bot error tracker
        if hasattr(self.bot, "errors"):
            from core.framework.error_tracker import ErrorSource, Severity
            self.bot.errors.record(
                ErrorSource.CMDCHAIN,
                step.error or "Unknown chain step error",
                severity=Severity.MEDIUM,
                guild_id=plan.guild_id,
                user_id=plan.user_id,
                command=f"chain:{step.action.value}",
                module="command_chain",
                error_type="ChainStepFailure",
                context={
                    "step_action": step.action.value,
                    "step_symbol": step.symbol or "",
                    "raw_text": plan.raw_text[:200],
                },
            )

    @staticmethod
    def _should_continue_after(plan: ChainPlan, idx: int) -> bool:
        """Decide whether to continue after a failed step based on operator."""
        step = plan.steps[idx]
        op = step.operator

        if op == ChainOperator.FIRE:
            return True  # ; always continues
        if op == ChainOperator.OR:
            return True  # || continues on failure (that's its purpose)
        # > && | all require success
        return step.status == StepStatus.SUCCEEDED

    async def _execute_step_via_command(
        self,
        original_msg: discord.Message,
        step: ChainStep,
        prefix: str,
    ) -> bool:
        """Execute a chain step by replaying it as a bot command.

        Constructs the canonical command string from the step's parsed data
        and processes it through the bot's command system.
        """
        cmd_str = self._step_to_command(step)
        if not cmd_str:
            step.error = "Could not convert step to command"
            return False

        # Create a fake message with the command content
        new_msg = copy.copy(original_msg)
        object.__setattr__(new_msg, "content", f"{prefix}{cmd_str}")
        # Mark as chain-initiated so cooldown handler can skip it.
        # _bypass_cooldown / _chain_step are not in Message.__slots__,
        # so write them into the instance __dict__ directly.
        new_msg.__dict__["_bypass_cooldown"] = True
        new_msg.__dict__["_chain_step"] = True

        try:
            await self.bot.process_commands(new_msg)
            return True
        except Exception as exc:
            step.error = str(exc)
            return False

    def _step_to_command(self, step: ChainStep) -> str | None:
        """Convert a ChainStep back into a command string."""
        action = step.action

        if action == ActionType.PASSTHROUGH:
            return step.params.get("cmd") or step.source_text or None

        if action == ActionType.BUY:
            amt = self._format_step_amount(step)
            return f"trade buy {step.symbol or ''} {amt}".strip()

        if action == ActionType.SELL:
            amt = self._format_step_amount(step)
            return f"trade sell {step.symbol or ''} {amt}".strip()

        if action == ActionType.MOVE:
            amt = self._format_step_amount(step)
            src = step.params.get("from", step.params.get("source", ""))
            dst = step.params.get("to", step.params.get("destination", ""))
            sym = step.symbol or ""
            return f"bank move {amt} {sym} {src} {dst}".strip()

        if action == ActionType.SWAP:
            amt = self._format_step_amount(step)
            sym_out = step.params.get("symbol_out", step.params.get("token_out", ""))
            return f"trade swap {step.symbol or ''} {sym_out} {amt}".strip()

        if action == ActionType.DEPOSIT:
            amt = self._format_step_amount(step)
            return f"bank deposit {amt}".strip()

        if action == ActionType.WITHDRAW:
            amt = self._format_step_amount(step)
            return f"bank withdraw {amt}".strip()

        if action == ActionType.STAKE:
            amt = self._format_step_amount(step)
            validator = step.target or step.params.get("validator", "")
            return f"stake farm {step.symbol or ''} {validator} {amt}".strip()

        if action == ActionType.UNSTAKE:
            amt = self._format_step_amount(step)
            validator = step.target or step.params.get("validator", "")
            return f"stake unstake {validator} {amt}".strip()

        if action == ActionType.DELEGATE:
            amt = self._format_step_amount(step)
            validator = step.target or step.params.get("validator", "")
            return f"stake farm {step.symbol or ''} {validator} {amt}".strip()

        if action == ActionType.UNDELEGATE:
            amt = self._format_step_amount(step)
            validator = step.target or step.params.get("validator", "")
            return f"stake unstake {validator} {amt}".strip()

        if action == ActionType.TRANSFER:
            amt = self._format_step_amount(step)
            target = step.target or ""
            return f"bank transfer {target} {amt}".strip()

        if action == ActionType.ADD_LP:
            amt = self._format_step_amount(step)
            token_a = step.symbol or step.params.get("token_a", "")
            token_b = step.target or step.params.get("token_b", "")
            return f"trade pool add {token_a} {token_b} {amt}".strip()

        if action == ActionType.REMOVE_LP:
            amt = self._format_step_amount(step)
            token_a = step.symbol or step.params.get("token_a", "")
            token_b = step.target or step.params.get("token_b", "")
            return f"trade pool remove {token_a} {token_b} {amt}".strip()

        if action == ActionType.SAVE_DEPOSIT:
            amt = self._format_step_amount(step)
            sym = step.symbol or "USD"
            # bank savings deposit <amount> (USD savings) or bank savings deposit <symbol> <amount>
            if sym == "USD":
                return f"bank savings deposit {amt}".strip()
            return f"bank savings deposit {sym} {amt}".strip()

        if action == ActionType.SAVE_WITHDRAW:
            amt = self._format_step_amount(step)
            sym = step.symbol or "USD"
            if sym == "USD":
                return f"bank savings withdraw {amt}".strip()
            return f"bank savings withdraw {sym} {amt}".strip()

        if action == ActionType.BUY_RIG:
            rig_type = step.target or step.params.get("rig_type", "basic")
            qty = self._format_step_amount(step)
            return f"chain mine buy {rig_type} {qty}".strip()

        if action == ActionType.SHOP_BUY:
            item = step.target or step.params.get("item", "")
            qty = self._format_step_amount(step)
            if qty:
                return f"shop buy {item} {qty}".strip()
            return f"shop buy {item}".strip()

        if action == ActionType.PLAY_GAME:
            game = step.target or step.params.get("game", "coinflip")
            amt = self._format_step_amount(step)
            token = step.symbol or step.params.get("token", "USD")
            side = step.params.get("side", "")
            parts = [game, amt, token]
            if side:
                parts.append(side)
            return " ".join(p for p in parts if p).strip()

        if action == ActionType.CREATE_WALLET:
            network = step.target or step.params.get("network", "")
            return f"wallet create {network}".strip()

        if action == ActionType.SET_NOTIFICATION:
            notif_type = step.target or step.params.get("notification_type", "price")
            sym = step.symbol or step.params.get("symbol", "")
            value = step.params.get("value", "on")
            if sym:
                return f"notify {notif_type} {sym} {value}".strip()
            return f"notify {notif_type} {value}".strip()

        if action == ActionType.QUERY:
            # Queries fall back to passthrough using source text
            return step.source_text or None

        return None

    @staticmethod
    def _format_step_amount(step: ChainStep) -> str:
        """Format a step's amount for command replay."""
        if step.amount is None:
            return ""
        if step.amount.is_all:
            return "all"
        if step.amount.is_rest:
            return "all"  # "rest" → "all" for replay (balance already reduced)
        if step.amount.is_usd and step.amount.resolved is not None:
            return f"${step.amount.resolved:g}"
        if step.amount.resolved is not None:
            return f"{step.amount.resolved:g}"
        return step.amount.raw

    # ════════════════════════════════════════════════════════════════════
    #  Interactive error recovery
    # ════════════════════════════════════════════════════════════════════

    async def _interactive_recovery(
        self,
        original_msg: discord.Message,
        status_msg: discord.Message,
        plan: ChainPlan,
        step: ChainStep,
        step_idx: int,
        prefix: str,
    ) -> bool:
        """Attempt interactive error recovery for a failed step.

        Shows the error diagnosis with a recovery button. If the user
        clicks it and recovery succeeds, the step is retried.
        """
        from core.framework.error_index import ErrorEntry, diagnose as diag_fn

        entry = ErrorEntry(
            error_message=step.error or "Unknown error",
            category=classify_error(step.error or ""),
            step=step,
            plan=plan,
            user_id=plan.user_id,
            guild_id=plan.guild_id,
        )
        diagnosis = diag_fn(entry)

        # Build error embed
        embed = (
            card(f"⛓️💥 Chain halted at step {step_idx + 1}", color=C_ERROR)
            .field("Step", f"`{describe_step(step)}`", False)
            .field("What happened", diagnosis.what_happened, False)
            .field("Why", diagnosis.why, False)
            .field("Suggestion", diagnosis.suggestion, False)
            .build()
        )

        if not diagnosis.auto_recoverable:
            embed.set_footer(text="This error requires manual intervention.")
            await status_msg.edit(embed=embed, view=None)
            return False

        # Show recovery button
        view = _RecoveryView(
            author_id=original_msg.author.id,
            recovery_label=self._recovery_label(entry.category),
            recovery_action=diagnosis.recovery_action,
        )

        await status_msg.edit(embed=embed, view=view)
        action_taken = await view.wait_result()

        if not action_taken:
            return False

        # Execute the recovery action
        if diagnosis.recovery_action:
            recovery_msg = copy.copy(original_msg)
            object.__setattr__(recovery_msg, "content", f"{prefix}{diagnosis.recovery_action}")
            object.__setattr__(recovery_msg, "_bypass_cooldown", True)
            try:
                await self.bot.process_commands(recovery_msg)
                await asyncio.sleep(0.5)
            except Exception:
                return False

        # Show "Resolved. Continuing..."
        resolve_embed = card("", description="✅ **Resolved. Continuing...**", color=C_SUCCESS).build()
        await status_msg.edit(embed=resolve_embed, view=None)
        await asyncio.sleep(1.0)

        # Retry the step
        step.status = StepStatus.PENDING
        step.error = None
        success = await self._execute_step_via_command(original_msg, step, prefix)
        return success

    @staticmethod
    def _recovery_label(category: ErrorCategory) -> str:
        """Get a user-friendly button label for a recovery action."""
        labels = {
            ErrorCategory.MISSING_WALLET: "Create Wallet",
            ErrorCategory.RATE_LIMITED: "Retry",
            ErrorCategory.VALIDATOR_NOT_FOUND: "Search Validators",
        }
        return labels.get(category, "Fix & Retry")

    # ════════════════════════════════════════════════════════════════════
    #  Status embed builder
    # ════════════════════════════════════════════════════════════════════

    def _build_plan_embed(
        self,
        plan: ChainPlan,
        title: str = "⛓️ Command Chain",
        color: int = C_NAVY,
    ) -> discord.Embed:
        """Build a status embed showing all steps, operators, and states."""
        lines: list[str] = []
        total_gas = 0.0
        total_fees = 0.0
        
        for i, step in enumerate(plan.steps):
            icon = _ICONS.get(step.status, "⬜")
            desc = describe_step(step)

            # Show parallel group indicator with animation effect
            para_tag = " ⚡" if step.parallel_group is not None else ""

            # Add progress bar for running steps
            if step.status == StepStatus.RUNNING:
                line = f"{icon} **{i + 1}.** {desc}{para_tag} 🔄"
            elif step.status == StepStatus.RETRYING:
                line = f"{icon} **{i + 1}.** {desc}{para_tag} 🔁 ({step.retry_count + 1})"
            else:
                line = f"{icon} **{i + 1}.** {desc}{para_tag}"

            if step.status == StepStatus.SUCCEEDED:
                result_desc = describe_result(step)
                line += f"\n-# ✨ {result_desc}"
                
                # Accumulate gas and fees for summary
                if step.result:
                    gas = step.result.get("gas")
                    if gas is not None:
                        total_gas += float(gas)
                    fee = step.result.get("fee") or step.result.get("platform_fee")
                    if fee is not None:
                        total_fees += float(fee)
                        
            elif step.status == StepStatus.FAILED and step.error:
                line += f"\n-# 💥 {step.error[:80]}"
            elif step.status == StepStatus.SKIPPED:
                line += "\n-# ⏭️ Skipped"
            elif step.status == StepStatus.RUNNING:
                line += "\n-# 🔄 *Executing...*"

            lines.append(line)

            # Show non-default operators between steps with visual connector
            if i < len(plan.steps) - 1:
                op_label = _OP_LABELS.get(step.operator, ">")
                if op_label != ">":
                    lines.append(f"-# ↳ `{op_label}`")

        # Build footer from active operators with totals
        parts = [f"{len(plan.steps)} step{'s' if len(plan.steps) != 1 else ''}"]
        ops_used = {s.operator for s in plan.steps[:-1]} if len(plan.steps) > 1 else set()
        if ChainOperator.PARA in ops_used:
            parts.append("parallel")
        if ChainOperator.PIPE in ops_used:
            parts.append("piped")
        if ChainOperator.OR in ops_used:
            parts.append("fallback")
        if ChainOperator.FIRE in ops_used:
            parts.append("fire-and-forget")
        parts.append("1s cooldown")
        
        # Add gas and fee totals if any
        if total_gas > 0:
            parts.append(f"⛽ {total_gas:,.4f}")
        if total_fees > 0:
            parts.append(f"💸 ${total_fees:,.2f}")

        return (
            card(title, description="\n".join(lines))
            .color(color)
            .footer(" · ".join(parts))
            .build()
        )

    async def _update_status(
        self,
        msg: discord.Message,
        plan: ChainPlan,
        footer: str,
    ) -> None:
        """Update the status embed on an existing message."""
        if plan.has_failure:
            color = C_ERROR
        elif plan.all_succeeded:
            color = C_SUCCESS
        else:
            color = C_NAVY

        embed = self._build_plan_embed(plan, title="⛓️ Executing Chain...", color=color)
        embed.set_footer(text=footer)
        try:
            await msg.edit(embed=embed, view=None)
        except Exception:
            pass

    # Note: The `.errors` command has been moved to `admin errors` subgroup
    # in cogs/admin.py for unified error tracking across all bot subsystems.


# ════════════════════════════════════════════════════════════════════════
#  Discord UI Views
# ════════════════════════════════════════════════════════════════════════

class _ChainConfirmView(discord.ui.View):
    """Confirm/cancel a command chain before execution."""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=30.0)
        self._author_id = author_id
        self._result: bool | None = None
        self._event = asyncio.Event()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message("Not your chain.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Execute", style=discord.ButtonStyle.success, emoji="⛓️")
    async def execute(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self._result = True
        self._event.set()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self._result = False
        self._event.set()
        self.stop()

    async def on_timeout(self) -> None:
        self._result = False
        self._event.set()
        self.stop()

    async def wait_result(self) -> bool:
        await self._event.wait()
        return self._result or False


class _RecoveryView(discord.ui.View):
    """Interactive recovery button for a failed chain step."""

    def __init__(
        self,
        author_id: int,
        recovery_label: str,
        recovery_action: str,
    ) -> None:
        super().__init__(timeout=60.0)
        self._author_id = author_id
        self._recovery_action = recovery_action
        self._result: bool | None = None
        self._event = asyncio.Event()

        # Dynamic button label
        fix_btn = discord.ui.Button(
            label=recovery_label,
            style=discord.ButtonStyle.primary,
            emoji="🔧",
        )
        fix_btn.callback = self._fix_callback
        self.add_item(fix_btn)

        skip_btn = discord.ui.Button(
            label="Skip & Stop",
            style=discord.ButtonStyle.secondary,
        )
        skip_btn.callback = self._skip_callback
        self.add_item(skip_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message("Not your chain.", ephemeral=True)
            return False
        return True

    async def _fix_callback(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self._result = True
        self._event.set()
        self.stop()

    async def _skip_callback(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self._result = False
        self._event.set()
        self.stop()

    async def on_timeout(self) -> None:
        self._result = False
        self._event.set()
        self.stop()

    async def wait_result(self) -> bool:
        await self._event.wait()
        return self._result or False


async def setup(bot: Discoin) -> None:
    await bot.add_cog(CommandChain(bot))
