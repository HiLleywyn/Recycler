"""Lending router  -  9 endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from core.framework.scale import to_human
from api.v2.dependencies import get_current_user, get_db, require_module
from api.v2.exceptions import InsufficientBalanceError, NotFoundError, ValidationError
from api.v2.utils import to_iso
from api.v2.schemas.lending import (
    AddCollateralRequest,
    BorrowRequest,
    LendingStats,
    LoanActionResult,
    LoanPublic,
    MyLoan,
    RepayRequest,
)

from constants.economy import MIN_COLLATERAL_RATIO

router = APIRouter(prefix="/lending", tags=["lending"], dependencies=[require_module("lending")])


@router.get("/stats", response_model=LendingStats, summary="Lending protocol stats")
async def lending_stats(user: dict = Depends(get_current_user), db=Depends(get_db)):
    """Return aggregate lending protocol statistics (guild-scoped)."""
    gid = int(user["guild_id"])
    usd = await db.fetchrow(
        """
        SELECT COUNT(*)::int AS cnt,
               COALESCE(SUM(outstanding), 0) AS total_borrowed,
               COALESCE(SUM(collateral), 0) AS total_collateral
        FROM loans
        WHERE outstanding > 0 AND guild_id = $1
        """,
        gid,
    )
    total_collateral = float(usd["total_collateral"])
    total_borrowed = float(usd["total_borrowed"])
    avg_ratio = (total_collateral / total_borrowed) if total_borrowed > 0 else 0.0

    return LendingStats(
        total_borrowed=total_borrowed,
        total_collateral=total_collateral,
        active_loans=usd["cnt"],
        avg_collateral_ratio=round(avg_ratio, 2),
    )


@router.get("/loans", response_model=list[LoanPublic], summary="Active USD loans")
async def list_loans(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return active USD loans (guild-scoped)."""
    gid = int(user["guild_id"])
    rows = await db.fetch(
        """
        SELECT user_id, principal, outstanding, collateral, created_at
        FROM loans
        WHERE outstanding > 0 AND guild_id = $3
        ORDER BY outstanding DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset, gid,
    )
    return [
        LoanPublic(
            user_id=str(r["user_id"]),
            principal=to_human(int(r["principal"] or 0)),
            outstanding=to_human(int(r["outstanding"] or 0)),
            collateral=to_human(int(r["collateral"] or 0)),
            collateral_ratio=round(
                to_human(int(r["collateral"] or 0)) / to_human(int(r["outstanding"] or 0)), 2
            ) if (r["outstanding"] or 0) > 0 else 0.0,
            created_at=to_iso(r["created_at"]),
        )
        for r in rows
    ]


@router.post("/borrow", response_model=LoanActionResult, summary="Borrow USD")
async def borrow_usd(
    body: BorrowRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Take out a USD loan by locking collateral."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])

    if body.collateral < body.amount * MIN_COLLATERAL_RATIO:
        raise ValidationError(
            f"Collateral must be at least {MIN_COLLATERAL_RATIO}x the borrow amount."
        )

    # Check collateral balance (wallet)
    urow = await db.fetchrow(
        "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
        uid, gid,
    )
    if not urow or float(urow["wallet"]) < body.collateral:
        raise InsufficientBalanceError("Insufficient wallet balance for collateral.")

    # Check no existing loan
    existing = await db.fetchrow(
        "SELECT outstanding FROM loans WHERE user_id = $1 AND guild_id = $2",
        uid, gid,
    )
    if existing and float(existing["outstanding"]) > 0:
        raise ValidationError("You already have an active loan. Repay it first.")

    # Lock collateral, give loan  -  RETURNING ensures deduction succeeded
    deducted = await db.fetchrow(
        "UPDATE users SET wallet = wallet - $3 WHERE user_id = $1 AND guild_id = $2 AND wallet >= $3 RETURNING wallet",
        uid, gid, body.collateral,
    )
    if deducted is None:
        raise InsufficientBalanceError("Insufficient USD balance for collateral.")
    await db.execute(
        """
        INSERT INTO loans (user_id, guild_id, principal, outstanding, collateral)
        VALUES ($1, $2, $3, $3, $4)
        ON CONFLICT (user_id, guild_id)
        DO UPDATE SET principal = $3, outstanding = $3, collateral = $4, last_interest = now()
        """,
        uid, gid, body.amount, body.collateral,
    )
    # Credit borrowed USD
    await db.execute(
        "UPDATE users SET wallet = wallet + $3 WHERE user_id = $1 AND guild_id = $2",
        uid, gid, body.amount,
    )

    return LoanActionResult(
        success=True,
        message=f"Borrowed {body.amount} USD with {body.collateral} collateral.",
        outstanding=body.amount,
        collateral=body.collateral,
    )


@router.post("/repay", response_model=LoanActionResult, summary="Repay loan")
async def repay_loan(
    body: RepayRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Repay part or all of an active USD loan."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])

    loan = await db.fetchrow(
        "SELECT outstanding, collateral FROM loans WHERE user_id = $1 AND guild_id = $2",
        uid, gid,
    )
    if not loan or float(loan["outstanding"]) <= 0:
        raise NotFoundError("No active loan found.")

    amount = min(body.amount, float(loan["outstanding"]))

    urow = await db.fetchrow(
        "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
        uid, gid,
    )
    if not urow or float(urow["wallet"]) < amount:
        raise InsufficientBalanceError("Insufficient balance to repay.")

    deducted = await db.fetchrow(
        "UPDATE users SET wallet = wallet - $3 WHERE user_id = $1 AND guild_id = $2 AND wallet >= $3 RETURNING wallet",
        uid, gid, amount,
    )
    if deducted is None:
        raise InsufficientBalanceError("Insufficient balance to repay.")
    new_outstanding = float(loan["outstanding"]) - amount
    collateral_return = 0.0

    if new_outstanding <= 0:
        # Fully repaid: return collateral
        collateral_return = float(loan["collateral"])
        await db.execute(
            "UPDATE users SET wallet = wallet + $3 WHERE user_id = $1 AND guild_id = $2",
            uid, gid, collateral_return,
        )
        await db.execute(
            "UPDATE loans SET outstanding = 0, collateral = 0 WHERE user_id = $1 AND guild_id = $2",
            uid, gid,
        )
    else:
        await db.execute(
            "UPDATE loans SET outstanding = $3 WHERE user_id = $1 AND guild_id = $2",
            uid, gid, new_outstanding,
        )

    return LoanActionResult(
        success=True,
        message=f"Repaid {amount} USD." + (f" Collateral of {collateral_return} returned." if collateral_return else ""),
        outstanding=max(new_outstanding, 0),
        collateral=float(loan["collateral"]) if new_outstanding > 0 else 0.0,
    )


@router.post("/add-collateral", response_model=LoanActionResult, summary="Add collateral")
async def add_collateral(
    body: AddCollateralRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Add additional collateral to an existing loan."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])

    loan = await db.fetchrow(
        "SELECT outstanding, collateral FROM loans WHERE user_id = $1 AND guild_id = $2",
        uid, gid,
    )
    if not loan or float(loan["outstanding"]) <= 0:
        raise NotFoundError("No active loan found.")

    urow = await db.fetchrow(
        "SELECT wallet FROM users WHERE user_id = $1 AND guild_id = $2",
        uid, gid,
    )
    if not urow or float(urow["wallet"]) < body.amount:
        raise InsufficientBalanceError("Insufficient wallet balance.")

    deducted = await db.fetchrow(
        "UPDATE users SET wallet = wallet - $3 WHERE user_id = $1 AND guild_id = $2 AND wallet >= $3 RETURNING wallet",
        uid, gid, body.amount,
    )
    if deducted is None:
        raise InsufficientBalanceError("Insufficient wallet balance.")
    await db.execute(
        "UPDATE loans SET collateral = collateral + $3 WHERE user_id = $1 AND guild_id = $2",
        uid, gid, body.amount,
    )

    new_collateral = float(loan["collateral"]) + body.amount
    return LoanActionResult(
        success=True,
        message=f"Added {body.amount} collateral.",
        outstanding=float(loan["outstanding"]),
        collateral=new_collateral,
    )


@router.get("/my-loans", response_model=list[MyLoan], summary="My active loans")
async def my_loans(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the authenticated user's active loans (USD and SUN)."""
    uid = int(user["user_id"])
    gid = int(user["guild_id"])
    result: list[MyLoan] = []

    usd_loan = await db.fetchrow(
        "SELECT principal, outstanding, collateral, last_interest, created_at "
        "FROM loans WHERE user_id = $1 AND guild_id = $2 AND outstanding > 0",
        uid, gid,
    )
    if usd_loan:
        o = to_human(int(usd_loan["outstanding"] or 0))
        c = to_human(int(usd_loan["collateral"] or 0))
        result.append(MyLoan(
            loan_type="usd",
            principal=to_human(int(usd_loan["principal"] or 0)),
            outstanding=o,
            collateral=c,
            collateral_ratio=round(c / o, 2) if o > 0 else 0.0,
            last_interest=to_iso(usd_loan["last_interest"]),
            created_at=to_iso(usd_loan["created_at"]),
        ))

    return result
