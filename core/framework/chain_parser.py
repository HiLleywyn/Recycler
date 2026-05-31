"""
core/framework/chain_parser.py  -  Parse command chains from user input.

Chains are sequences of commands linked by operators:

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
    .buy MTA + .buy ARC + .buy SOL > .move all b w
    .buy 100 MTA && .stake all MTA cosmosval1

The parser:
  1. Splits on operator tokens (&&, ||, +, |, ;, >)
  2. Tags each step with its outgoing operator
  3. Strips optional leading prefix (. or $)
  4. Feeds each segment to CommandParser
  5. Assigns parallel groups for + operators
  6. Wires up dependencies
  7. Returns a ChainPlan

Usage:
    from core.framework.chain_parser import ChainSyntaxParser

    parser = ChainSyntaxParser(known_tokens={"MTA", "ARC"}, prefix=".")
    plan = parser.parse(
        raw=".buy 10 MTA > .move all mta b w",
        user_id=123, guild_id=456, channel_id=789, message_id=1,
    )
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from core.framework.chain_engine import ChainOperator, ChainPlan, ChainStep
from core.framework.command_parser import CommandParser, ParseResult

# Default cooldown between chain links (seconds)
CHAIN_LINK_COOLDOWN: float = 1.0

# ── Operator token mapping ────────────────────────────────────────────────

# Order matters: longest tokens first so "&&" matches before "&", "||" before "|"
_OPERATOR_TOKENS: list[tuple[str, ChainOperator]] = [
    ("&&", ChainOperator.AND),
    ("||", ChainOperator.OR),
    ("+",  ChainOperator.PARA),
    ("|",  ChainOperator.PIPE),
    (";",  ChainOperator.FIRE),
    (">",  ChainOperator.SEQ),
]

# Build a regex that matches any operator token, longest-first.
# Capture the operator so we can tag each segment.
_OP_PATTERN = "|".join(re.escape(tok) for tok, _ in _OPERATOR_TOKENS)
_SPLIT_RE = re.compile(rf"\s*({_OP_PATTERN})\s*")

# Reverse lookup: token string → ChainOperator
_TOKEN_TO_OP: dict[str, ChainOperator] = {tok: op for tok, op in _OPERATOR_TOKENS}

# For is_chain detection  -  does the text contain any operator?
_HAS_OPERATOR_RE = re.compile(rf"(?:{_OP_PATTERN})")


@dataclass
class _Segment:
    """A parsed segment: the command text plus the operator that follows it."""

    text: str
    operator: ChainOperator  # the operator *after* this segment (SEQ for the last one)


class ChainSyntaxParser:
    """Parse operator-delimited command chains into a :class:`ChainPlan`."""

    def __init__(
        self,
        known_tokens: set[str] | None = None,
        prefix: str = ".",
    ) -> None:
        self.cmd_parser = CommandParser(known_tokens=known_tokens)
        self.prefix = prefix

    def parse(
        self,
        raw: str,
        *,
        user_id: int = 0,
        guild_id: int = 0,
        channel_id: int = 0,
        message_id: int = 0,
    ) -> ChainPlan | ParseResult:
        """Parse a full chain string into a :class:`ChainPlan`.

        Returns a :class:`ChainPlan` on success, or a :class:`ParseResult`
        with an error if the first segment fails to parse.
        """
        segments = self._split_chain(raw)

        if not segments:
            return ParseResult(error="Empty chain", raw=raw)

        steps: list[ChainStep] = []
        errors: list[str] = []
        parallel_group_counter = 0
        in_parallel_group = False

        for i, seg in enumerate(segments):
            result = self.cmd_parser.parse(seg.text)

            if result.step is None:
                # First link must succeed; later links get recorded as errors
                if i == 0:
                    return result
                errors.append(f"Link {i + 1}: {result.error or 'parse error'}")
                continue

            step = result.step
            step.operator = seg.operator

            # ── Parallel grouping ───────────────────────────────────
            # If the *previous* segment's operator was +, this step is
            # in the same parallel group.
            if i > 0 and segments[i - 1].operator == ChainOperator.PARA:
                if not in_parallel_group:
                    # Start a new parallel group, retroactively tag the
                    # previous step too.
                    in_parallel_group = True
                    if steps:
                        steps[-1].parallel_group = parallel_group_counter
                step.parallel_group = parallel_group_counter
            else:
                if in_parallel_group:
                    # We just exited a parallel group
                    parallel_group_counter += 1
                    in_parallel_group = False

            # ── Dependency wiring ────────────────────────────────────
            # For non-parallel steps, depend on the previous step.
            # For parallel steps, all members depend on the step *before*
            # the group (if any).
            if step.parallel_group is not None:
                # Find the step before this parallel group
                first_in_group = next(
                    (j for j in range(len(steps) - 1, -1, -1)
                     if steps[j].parallel_group != step.parallel_group),
                    None,
                )
                if first_in_group is not None:
                    step.depends_on = [first_in_group]
            elif i > 0 and steps:
                prev_op = segments[i - 1].operator
                if prev_op == ChainOperator.OR:
                    # OR: depend on previous but only fires when it fails
                    step.depends_on = [len(steps) - 1]
                elif prev_op == ChainOperator.FIRE:
                    # FIRE: no hard dependency  -  always runs
                    step.depends_on = []
                else:
                    step.depends_on = [len(steps) - 1]

            steps.append(step)

        # Close any trailing parallel group
        if in_parallel_group:
            parallel_group_counter += 1

        if not steps:
            return ParseResult(error="No valid commands in chain", raw=raw)

        plan = ChainPlan(
            steps=steps,
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            created_at=time.time(),
            source="chain_parser",
            raw_text=raw,
            requires_confirmation=True,
        )

        return plan

    def is_chain(self, text: str) -> bool:
        """Return True if text contains chain syntax (more than one segment)."""
        return len(self._split_chain(text)) > 1

    def _split_chain(self, raw: str) -> list[_Segment]:
        """Split raw input on operator tokens, returning tagged segments."""
        parts = _SPLIT_RE.split(raw.strip())

        # parts alternates: [text, op, text, op, ..., text]
        segments: list[_Segment] = []
        i = 0
        while i < len(parts):
            text = parts[i].strip()
            if not text:
                i += 1
                continue

            # Strip leading prefix if present (. or > or $)
            if text.startswith(self.prefix):
                text = text[len(self.prefix):].strip()
            elif text.startswith(">") or text.startswith("$"):
                text = text[1:].strip()

            # The operator after this text (if any)
            op = ChainOperator.SEQ  # default for last segment
            if i + 1 < len(parts) and parts[i + 1] in _TOKEN_TO_OP:
                op = _TOKEN_TO_OP[parts[i + 1]]
                i += 2  # skip op token
            else:
                i += 1

            if text:
                # Skip single-character non-command prefixes that users might accidentally include
                # e.g., ".n move all dsc bank wallet" where "n" is not a valid command
                if len(text) == 1 and text.isalpha() and text.lower() not in (
                    "b", "c", "v", "w",  # storage aliases
                ):
                    # This is likely a typo/prefix error, skip this segment
                    # but log a warning for debugging
                    import logging
                    logging.getLogger("discoin.chain_parser").warning(
                        f"Skipping single-character segment '{text}' in chain: {raw[:50]}"
                    )
                    continue
                segments.append(_Segment(text=text, operator=op))

        return segments
