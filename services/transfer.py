"""Transfer service layer  -  shared by Discord commands and web API.

Validates and executes wallet-to-wallet USD transfers between users.
No Discord dependencies.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from core.framework.scale import to_human, to_raw


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class TransferResult:
    success: bool
    tx_hash: str = ""
    amount: float = 0.0
    new_balance: float = 0.0
    error: str = ""


# ── Core function ────────────────────────────────────────────────────────────

async def execute_transfer(
    db,
    guild_id: int,
    sender_id: int,
    recipient_id: int,
    amount: float,
) -> TransferResult:
    """Validate and execute a wallet-to-wallet USD transfer.

    ``amount`` is human-scale; the DB stores wallet balances as raw
    ``NUMERIC(36,0)`` scaled by ``10**18`` so we convert via ``to_raw``
    before writing.

    Returns a TransferResult with success/failure info and the sender's
    new wallet balance on success.
    """
    # ── Validation ────────────────────────────────────────────────────────
    if sender_id == recipient_id:
        return TransferResult(success=False, error="You cannot transfer to yourself.")

    if amount <= 0:
        return TransferResult(success=False, error="Transfer amount must be positive.")

    if not math.isfinite(amount):
        return TransferResult(success=False, error="Transfer amount must be a finite number.")

    # ── Ensure recipient exists ───────────────────────────────────────────
    await db.ensure_user(recipient_id, guild_id)

    # ── Check sender balance (raw int comparison  -  exact, no float loss) ─
    amount_raw = to_raw(amount)
    sender = await db.get_user(sender_id, guild_id)
    sender_wallet_raw = int(sender["wallet"]) if sender else 0
    if sender_wallet_raw < amount_raw:
        have = to_human(sender_wallet_raw)
        return TransferResult(
            success=False,
            error=f"Insufficient balance (have ${have:,.2f}, need ${amount:,.2f}).",
        )

    # ── Execute atomic transfer ───────────────────────────────────────────
    try:
        tx_hash = await db.transfer_wallet(guild_id, sender_id, recipient_id, amount_raw)
    except ValueError as e:
        return TransferResult(success=False, error=str(e))

    # Fetch sender's updated balance
    sender_after = await db.get_user(sender_id, guild_id)
    new_balance = to_human(int(sender_after["wallet"])) if sender_after else 0.0

    return TransferResult(
        success=True,
        tx_hash=tx_hash,
        amount=amount,
        new_balance=new_balance,
    )
