"""Savings router  -  5 endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api.v2.dependencies import get_current_user, get_db, get_orm_db, require_module
from api.v2.exceptions import InsufficientBalanceError, ValidationError
from api.v2.utils import to_iso
from api.v2.schemas.savings import (
    ReserveBalance,
    SavingsActionResult,
    SavingsDepositRequest,
    SavingsPool,
    SavingsPosition,
    SavingsWithdrawRequest,
)

from constants.economy import BASE_DEPOSIT_APY, BASE_BORROW_APY
from core.framework.scale import to_human
from services.savings import deposit_savings, withdraw_savings

router = APIRouter(prefix="/savings", tags=["savings"], dependencies=[require_module("savings")])


@router.get("/pools", response_model=list[SavingsPool], summary="List savings pools")
async def list_savings_pools(db=Depends(get_db)):
    """Return savings pool statistics including utilization and APY."""
    # savings_deposits.amount, loans.outstanding are raw NUMERIC(36,0) * 10**18;
    # convert to human for the API response.
    rows = await db.fetch(
        """
        SELECT sd.symbol,
               COALESCE(SUM(sd.amount), 0) AS total_deposits
        FROM savings_deposits sd
        GROUP BY sd.symbol
        """
    )
    pools = []
    seen_symbols = set()
    for r in rows:
        total_dep = to_human(int(r["total_deposits"] or 0))
        symbol = r["symbol"]
        seen_symbols.add(symbol.upper())
        total_borrowed = 0.0
        if symbol.upper() == "USD":
            brow = await db.fetchrow("SELECT COALESCE(SUM(outstanding), 0) AS total FROM loans")
            total_borrowed = to_human(int(brow["total"] or 0)) if brow else 0.0

        utilization = (total_borrowed / total_dep * 100) if total_dep > 0 else 0.0
        deposit_apy = BASE_DEPOSIT_APY * (1 + utilization / 100)
        borrow_apy = BASE_BORROW_APY * (1 + utilization / 100)

        pools.append(SavingsPool(
            symbol=symbol,
            total_deposits=total_dep,
            total_borrowed=total_borrowed,
            utilization_pct=round(utilization, 2),
            deposit_apy=round(deposit_apy, 4),
            borrow_apy=round(borrow_apy, 4),
        ))

    # Ensure USD pool always appears (even with 0 deposits)
    for sym in ("USD",):
        if sym not in seen_symbols:
            pools.append(SavingsPool(
                symbol=sym,
                total_deposits=0.0,
                total_borrowed=0.0,
                utilization_pct=0.0,
                deposit_apy=round(BASE_DEPOSIT_APY, 4),
                borrow_apy=round(BASE_BORROW_APY, 4),
            ))

    return pools


@router.get("/reserve", response_model=list[ReserveBalance], summary="Community reserve balances")
async def get_reserve(db=Depends(get_db)):
    """Return community reserve balances (treasury)."""
    rows = await db.fetch(
        "SELECT guild_id, balance FROM guild_treasury ORDER BY guild_id"
    )
    return [
        ReserveBalance(symbol="USD", balance=to_human(int(r["balance"] or 0)))
        for r in rows
    ]


@router.post("/deposit", response_model=SavingsActionResult, summary="Deposit into savings")
async def deposit_savings_endpoint(
    body: SavingsDepositRequest,
    user: dict = Depends(get_current_user),
    orm_db=Depends(get_orm_db),
):
    """Deposit USD or SUN into savings to earn interest.

    Delegates to :func:`services.savings.deposit_savings`, which handles
    raw-int scaling, atomic debit/credit, and balance validation. The raw SQL
    path that lived here previously wrote human-scale floats directly into
    raw ``NUMERIC(36,0)`` columns, silently corrupting balances.
    """
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    symbol = body.asset.upper()
    amount = body.amount

    if symbol not in ("USD", "SUN"):
        raise ValidationError("Asset must be 'usd' or 'sun'.")

    result = await deposit_savings(orm_db, gid, uid, symbol, amount)
    if not result.success:
        err = result.error.lower()
        if "insufficient" in err:
            raise InsufficientBalanceError(result.error)
        raise ValidationError(result.error)

    return SavingsActionResult(
        success=True,
        message=f"Deposited {amount} {symbol} into savings.",
        symbol=result.symbol,
        amount=result.amount,
        new_balance=result.new_savings_balance,
    )


@router.post("/withdraw", response_model=SavingsActionResult, summary="Withdraw from savings")
async def withdraw_savings_endpoint(
    body: SavingsWithdrawRequest,
    user: dict = Depends(get_current_user),
    orm_db=Depends(get_orm_db),
):
    """Withdraw from savings back to wallet (USD) or holdings (SUN).

    Delegates to :func:`services.savings.withdraw_savings` for the same
    raw-scaling reasons documented on ``deposit_savings_endpoint``.
    """
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    symbol = body.asset.upper()
    amount = body.amount

    if symbol not in ("USD", "SUN"):
        raise ValidationError("Asset must be 'usd' or 'sun'.")

    result = await withdraw_savings(orm_db, gid, uid, symbol, amount)
    if not result.success:
        err = result.error.lower()
        if "insufficient" in err:
            raise InsufficientBalanceError(result.error)
        raise ValidationError(result.error)

    return SavingsActionResult(
        success=True,
        message=f"Withdrew {amount} {symbol} from savings.",
        symbol=result.symbol,
        amount=result.amount,
        new_balance=result.new_savings_balance,
    )


@router.get("/my-positions", response_model=list[SavingsPosition], summary="My savings positions")
async def my_savings_positions(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the authenticated user's savings positions."""
    rows = await db.fetch(
        """
        SELECT symbol, amount, last_interest, created_at
        FROM savings_deposits
        WHERE user_id = $1 AND guild_id = $2 AND amount > 0
        ORDER BY symbol
        """,
        int(user["user_id"]),
        int(user["guild_id"]),
    )
    return [
        SavingsPosition(
            symbol=r["symbol"],
            amount=to_human(int(r["amount"] or 0)),
            interest_earned=0.0,  # computed at interest-accrual time
            apy=BASE_DEPOSIT_APY,
            last_interest=to_iso(r["last_interest"]),
            created_at=to_iso(r["created_at"]),
        )
        for r in rows
    ]
