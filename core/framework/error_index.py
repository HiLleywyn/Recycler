"""
core/framework/error_index.py  -  Error indexer and diagnostic engine for chain execution.

Records every error that occurs during chain execution, indexes them by
category, and provides a diagnostic API that can:
  - Search for specific error types
  - Explain what happened and why
  - Suggest what should have happened given the context

Usage:
    from core.framework.error_index import ErrorIndex, ErrorEntry

    index = ErrorIndex()
    index.record(entry)

    # Search and diagnose
    results = index.search("wallet")
    diagnosis = index.diagnose(entry)
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from core.framework.chain_engine import ChainStep, ChainPlan, describe_step


# ═══════════════════════════════════════════════════════════════════════════
# Error categories
# ═══════════════════════════════════════════════════════════════════════════

class ErrorCategory(str, Enum):
    """Broad categories for chain execution errors."""

    INSUFFICIENT_FUNDS = "insufficient_funds"
    MISSING_WALLET = "missing_wallet"
    TOKEN_NOT_FOUND = "token_not_found"
    NETWORK_HALTED = "network_halted"
    RATE_LIMITED = "rate_limited"
    VALIDATOR_NOT_FOUND = "validator_not_found"
    POOL_NOT_FOUND = "pool_not_found"
    PERMISSION_DENIED = "permission_denied"
    PARSE_ERROR = "parse_error"
    UNKNOWN = "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# Error entry
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ErrorEntry:
    """A single recorded error from chain execution."""

    error_message: str
    category: ErrorCategory
    step: ChainStep | None = None
    plan: ChainPlan | None = None
    user_id: int = 0
    guild_id: int = 0
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False
    resolution: str = ""

    @property
    def step_description(self) -> str:
        if self.step:
            return describe_step(self.step)
        return "unknown step"

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp


# ═══════════════════════════════════════════════════════════════════════════
# Error classification
# ═══════════════════════════════════════════════════════════════════════════

_CATEGORY_PATTERNS: list[tuple[ErrorCategory, list[str]]] = [
    (ErrorCategory.INSUFFICIENT_FUNDS, [
        "insufficient", "not enough", "balance is 0", "only have",
        "need .* but", "can't afford",
    ]),
    (ErrorCategory.MISSING_WALLET, [
        "no wallet", "wallet not found", "not registered", "no .* wallet",
    ]),
    (ErrorCategory.TOKEN_NOT_FOUND, [
        "unknown token", "token not found", "invalid token", "unknown symbol",
    ]),
    (ErrorCategory.NETWORK_HALTED, [
        "halted", "suspended", "paused", "network .* halted",
    ]),
    (ErrorCategory.RATE_LIMITED, [
        "rate limit", "too fast", "cooldown", "try again in",
    ]),
    (ErrorCategory.VALIDATOR_NOT_FOUND, [
        "validator.*not found", "unknown validator",
    ]),
    (ErrorCategory.POOL_NOT_FOUND, [
        "pool not found", "no pool", "no liquidity pool",
    ]),
    (ErrorCategory.PERMISSION_DENIED, [
        "permission", "not allowed", "can't use", "required role",
    ]),
    (ErrorCategory.PARSE_ERROR, [
        "could not parse", "invalid", "usage:", "could not identify",
    ]),
]


def classify_error(error_msg: str) -> ErrorCategory:
    """Classify an error message into a category."""
    lower = error_msg.lower()
    for category, patterns in _CATEGORY_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, lower):
                return category
    return ErrorCategory.UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════
# Diagnosis engine
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Diagnosis:
    """Structured explanation of what went wrong and how to fix it."""

    error: ErrorEntry
    what_happened: str
    why: str
    suggestion: str
    auto_recoverable: bool = False
    recovery_action: str = ""      # Command or action to fix it


# Diagnosis templates keyed by category
_DIAGNOSIS_TEMPLATES: dict[ErrorCategory, dict[str, str]] = {
    ErrorCategory.INSUFFICIENT_FUNDS: {
        "what": "The {action} failed because you don't have enough {resource} to complete it.",
        "why": "Your balance is lower than the amount required. This can happen if a prior step consumed funds, prices moved, or you miscalculated.",
        "suggestion": "Check your balance with `.bal` or `.portfolio`, then retry with a smaller amount or use `all` to use your full balance.",
    },
    ErrorCategory.MISSING_WALLET: {
        "what": "The {action} failed because you don't have a {network} wallet set up.",
        "why": "Some tokens live on specific blockchain networks. To hold or move them, you need a wallet for that network.",
        "suggestion": "Create the wallet with `.wallet create {network}`, then retry the command.",
        "recoverable": "true",
        "recovery": "wallet create {network}",
    },
    ErrorCategory.TOKEN_NOT_FOUND: {
        "what": "The token symbol wasn't recognized.",
        "why": "The symbol may be misspelled, or the token might not be listed on this server. Check `.crypto` for available tokens.",
        "suggestion": "Double-check the symbol. Common tokens: MTA, ARC, SOL, SUN. Use `.crypto` to see the full list.",
    },
    ErrorCategory.NETWORK_HALTED: {
        "what": "The {action} was blocked because the {network} network is currently halted.",
        "why": "An admin has paused transactions on this network, usually for maintenance or emergency reasons.",
        "suggestion": "Wait for the network to be unhalted, or contact a server admin.",
    },
    ErrorCategory.RATE_LIMITED: {
        "what": "You're sending commands too fast.",
        "why": "Commands have cooldowns to prevent abuse. The system needs a moment between actions.",
        "suggestion": "Wait a few seconds and try again. Chain commands handle cooldowns automatically.",
        "recoverable": "true",
    },
    ErrorCategory.VALIDATOR_NOT_FOUND: {
        "what": "The validator you specified doesn't exist.",
        "why": "The validator name may be misspelled or hasn't been registered yet.",
        "suggestion": "Use `.stake validators` to see available validators, then retry with the correct name.",
    },
    ErrorCategory.POOL_NOT_FOUND: {
        "what": "No liquidity pool exists for this token pair.",
        "why": "Swaps require an active liquidity pool. The pool may not have been created yet for this pair.",
        "suggestion": "Check available pools with `.pool list`, or try swapping through a common pair like USDC.",
    },
    ErrorCategory.PERMISSION_DENIED: {
        "what": "You don't have permission to use this command.",
        "why": "The server has role-based restrictions on certain commands.",
        "suggestion": "Ask a server admin to grant you the required role.",
    },
    ErrorCategory.PARSE_ERROR: {
        "what": "The command couldn't be understood.",
        "why": "The syntax was too far from what the parser expects. This could be a severe typo or missing arguments.",
        "suggestion": "Check the command format. Example: `.buy 10 MTA`, `.move all ARC b w`, `.sell $500 SOL`.",
    },
    ErrorCategory.UNKNOWN: {
        "what": "An unexpected error occurred during {action}.",
        "why": "This might be a bug or an edge case the system doesn't handle yet.",
        "suggestion": "Try the command again. If it persists, report it with `.report`.",
    },
}


def diagnose(entry: ErrorEntry) -> Diagnosis:
    """Generate a structured diagnosis for an error entry."""
    template = _DIAGNOSIS_TEMPLATES.get(entry.category, _DIAGNOSIS_TEMPLATES[ErrorCategory.UNKNOWN])

    # Build context for template substitution
    action = entry.step.action.value if entry.step else "command"
    symbol = (entry.step.symbol or "") if entry.step else ""

    # Try to extract network from the error or step
    network = ""
    if entry.step and entry.step.symbol:
        from core.config import Config
        tok = Config.TOKENS.get(entry.step.symbol.upper(), {})
        network = tok.get("network", "")

    resource = symbol or "funds"

    ctx = {
        "action": action,
        "symbol": symbol,
        "network": network or "the target",
        "resource": resource,
    }

    what = template["what"].format(**ctx)
    why = template["why"].format(**ctx)
    suggestion = template["suggestion"].format(**ctx)
    auto_recoverable = template.get("recoverable", "false") == "true"
    recovery = template.get("recovery", "").format(**ctx)

    return Diagnosis(
        error=entry,
        what_happened=what,
        why=why,
        suggestion=suggestion,
        auto_recoverable=auto_recoverable,
        recovery_action=recovery,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Error Index
# ═══════════════════════════════════════════════════════════════════════════

class ErrorIndex:
    """In-memory index of chain execution errors.

    Stores errors per-guild, searchable by keyword, category, user, or time.
    Keeps a rolling window (default: last 500 errors per guild).
    """

    def __init__(self, max_per_guild: int = 500) -> None:
        self.max_per_guild = max_per_guild
        self._entries: dict[int, list[ErrorEntry]] = defaultdict(list)

    # ── Recording ───────────────────────────────────────────────────────

    def record(
        self,
        error_message: str,
        *,
        step: ChainStep | None = None,
        plan: ChainPlan | None = None,
        user_id: int = 0,
        guild_id: int = 0,
    ) -> ErrorEntry:
        """Record an error and return the entry."""
        category = classify_error(error_message)
        entry = ErrorEntry(
            error_message=error_message,
            category=category,
            step=step,
            plan=plan,
            user_id=user_id,
            guild_id=guild_id,
        )
        entries = self._entries[guild_id]
        entries.append(entry)
        # Trim to max
        if len(entries) > self.max_per_guild:
            self._entries[guild_id] = entries[-self.max_per_guild:]
        return entry

    # ── Searching ───────────────────────────────────────────────────────

    def search(
        self,
        guild_id: int,
        *,
        keyword: str = "",
        category: ErrorCategory | None = None,
        user_id: int = 0,
        limit: int = 10,
    ) -> list[ErrorEntry]:
        """Search errors by keyword, category, and/or user."""
        entries = self._entries.get(guild_id, [])
        results: list[ErrorEntry] = []

        for entry in reversed(entries):  # newest first
            if keyword and keyword.lower() not in entry.error_message.lower():
                # Also check step description
                if keyword.lower() not in entry.step_description.lower():
                    continue
            if category and entry.category != category:
                continue
            if user_id and entry.user_id != user_id:
                continue
            results.append(entry)
            if len(results) >= limit:
                break

        return results

    def diagnose_latest(
        self,
        guild_id: int,
        *,
        user_id: int = 0,
        keyword: str = "",
    ) -> Diagnosis | None:
        """Find the most recent matching error and diagnose it."""
        results = self.search(guild_id, keyword=keyword, user_id=user_id, limit=1)
        if not results:
            return None
        return diagnose(results[0])

    def stats(self, guild_id: int) -> dict[str, int]:
        """Return error count by category for a guild."""
        entries = self._entries.get(guild_id, [])
        counts: dict[str, int] = {}
        for entry in entries:
            key = entry.category.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def clear(self, guild_id: int) -> int:
        """Clear all errors for a guild. Returns count cleared."""
        count = len(self._entries.get(guild_id, []))
        self._entries.pop(guild_id, None)
        return count
