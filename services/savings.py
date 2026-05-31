"""Savings service layer  -  shared by Discord commands and web API.

Handles deposits and withdrawals for the savings account system.
Only USD and SUN are supported. No Discord dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.framework.scale import to_human, to_raw

# ── Constants ────────────────────────────────────────────────────────────────
SUPPORTED_SAVINGS_SYMBOLS = frozenset({"USD", "SUN"})


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class SavingsResult:
    success: bool
    amount: float = 0.0
    new_savings_balance: float = 0.0
    symbol: str = ""
    error: str = ""


# ── Core functions ───────────────────────────────────────────────────────────

async def deposit_savings(
    db,
    guild_id: int,
    user_id: int,
    symbol: str,
    amount: float,
) -> SavingsResult:
    """Deposit funds from wallet (USD) or holdings (SUN) into savings.

    ``amount`` is human-scale (e.g. ``50.0`` for $50).  All DB writes are
    converted to raw scaled integers via ``to_raw``.

    Returns a SavingsResult with the new savings balance on success.
    """
    symbol = symbol.upper()

    # ── Validation ────────────────────────────────────────────────────────
    if symbol not in SUPPORTED_SAVINGS_SYMBOLS:
        return SavingsResult(
            success=False,
            error=f"Only {', '.join(sorted(SUPPORTED_SAVINGS_SYMBOLS))} are supported for savings.",
        )

    if amount <= 0:
        return SavingsResult(success=False, error="Deposit amount must be positive.")

    # ── Check source balance ──────────────────────────────────────────────
    # DB columns are raw NUMERIC(36,0); compare in raw int space so we do
    # not leak float precision into the check.
    amount_raw = to_raw(amount)
    if symbol == "USD":
        user = await db.get_user(user_id, guild_id)
        balance_raw = int(user["wallet"]) if user else 0
    else:  # SUN
        holding = await db.get_holding(user_id, guild_id, symbol)
        balance_raw = int(holding["amount"]) if holding else 0

    if balance_raw < amount_raw:
        return SavingsResult(
            success=False,
            error=(
                f"Insufficient {symbol} balance "
                f"(have {to_human(balance_raw):,.6f}, need {amount:,.6f})."
            ),
        )

    # ── Execute deposit atomically ────────────────────────────────────────
    try:
        async with db.atomic():
            # Debit from source FIRST  -  if this fails, savings isn't credited
            if symbol == "USD":
                await db.update_wallet(user_id, guild_id, -amount_raw)
            else:  # SUN
                await db.update_holding(user_id, guild_id, symbol, -amount_raw)

            await db.savings_deposit(user_id, guild_id, symbol, amount_raw)
    except Exception as e:
        return SavingsResult(success=False, error=str(e))

    # Fetch updated savings balance
    deposit_row = await db.get_savings_deposit(user_id, guild_id, symbol)
    new_balance = to_human(int(deposit_row["amount"])) if deposit_row else 0.0

    return SavingsResult(
        success=True,
        amount=amount,
        new_savings_balance=new_balance,
        symbol=symbol,
    )


async def withdraw_savings(
    db,
    guild_id: int,
    user_id: int,
    symbol: str,
    amount: float,
) -> SavingsResult:
    """Withdraw funds from savings back to wallet (USD) or holdings (SUN).

    ``amount`` is human-scale; all DB writes are converted to raw via ``to_raw``.

    Returns a SavingsResult with the new savings balance on success.
    """
    symbol = symbol.upper()

    # ── Validation ────────────────────────────────────────────────────────
    if symbol not in SUPPORTED_SAVINGS_SYMBOLS:
        return SavingsResult(
            success=False,
            error=f"Only {', '.join(sorted(SUPPORTED_SAVINGS_SYMBOLS))} are supported for savings.",
        )

    if amount <= 0:
        return SavingsResult(success=False, error="Withdrawal amount must be positive.")

    # ── Check savings balance ─────────────────────────────────────────────
    amount_raw = to_raw(amount)
    deposit_row = await db.get_savings_deposit(user_id, guild_id, symbol)
    savings_balance_raw = int(deposit_row["amount"]) if deposit_row else 0

    if savings_balance_raw < amount_raw:
        return SavingsResult(
            success=False,
            error=(
                f"Insufficient savings balance "
                f"(have {to_human(savings_balance_raw):,.6f} {symbol}, need {amount:,.6f})."
            ),
        )

    # ── Execute withdrawal atomically ─────────────────────────────────────
    try:
        async with db.atomic():
            # Debit savings FIRST  -  if this fails, destination isn't credited
            await db.savings_withdraw(user_id, guild_id, symbol, amount_raw)

            # Credit to destination
            if symbol == "USD":
                await db.update_wallet(user_id, guild_id, amount_raw)
            else:  # SUN
                await db.update_holding(user_id, guild_id, symbol, amount_raw)
    except Exception as e:
        return SavingsResult(success=False, error=str(e))

    # Fetch updated savings balance
    deposit_row = await db.get_savings_deposit(user_id, guild_id, symbol)
    new_balance = to_human(int(deposit_row["amount"])) if deposit_row else 0.0

    return SavingsResult(
        success=True,
        amount=amount,
        new_savings_balance=new_balance,
        symbol=symbol,
    )
