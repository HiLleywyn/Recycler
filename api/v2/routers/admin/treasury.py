"""Admin treasury management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.framework.scale import to_human, to_raw
from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import InsufficientBalanceError, ValidationError
from api.v2.schemas.admin import TreasuryAction, TreasuryInfo
from api.v2.schemas.common import SuccessResponse
from api.v2.routers.admin._helpers import audit_log

router = APIRouter()


@router.get("/treasury", response_model=TreasuryInfo, summary="Get treasury balance")
async def get_treasury(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return the guild treasury balance."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow(
        "SELECT balance FROM guild_treasury WHERE guild_id = $1", gid,
    )
    return TreasuryInfo(balance=to_human(int(row["balance"])) if row else 0.0)


@router.post("/treasury", response_model=SuccessResponse, summary="Treasury action")
async def treasury_action(
    body: TreasuryAction,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Give from treasury to a user or drain treasury."""
    gid = int(admin["guild_id"])

    if body.action == "give":
        if not body.target_user_id:
            raise ValidationError("target_user_id required for 'give' action.")

        target = int(body.target_user_id)
        amount_raw = to_raw(body.amount)
        async with db.transaction():
            # Check treasury balance (balance is NUMERIC(36,0) raw)
            row = await db.fetchrow(
                "SELECT balance FROM guild_treasury WHERE guild_id = $1", gid,
            )
            balance_human = to_human(int(row["balance"])) if row else 0.0
            if balance_human < body.amount:
                raise InsufficientBalanceError("Insufficient treasury balance.")

            # Deduct from treasury (raw units)
            await db.execute(
                "UPDATE guild_treasury SET balance = balance - $2 WHERE guild_id = $1",
                gid, amount_raw,
            )
            # Give to user (users.wallet is NUMERIC(36,0) raw)
            await db.execute(
                "UPDATE users SET wallet = wallet + $3 WHERE user_id = $1 AND guild_id = $2",
                target, gid, amount_raw,
            )
            await audit_log(db, gid, int(admin["user_id"]), "treasury_give",
                            {"target": body.target_user_id, "amount": body.amount})
        return SuccessResponse(message=f"Gave {body.amount} USD from treasury to user {target}.")

    elif body.action == "drain":
        amount_raw = to_raw(body.amount)
        async with db.transaction():
            await db.execute(
                "UPDATE guild_treasury SET balance = GREATEST(balance - $2, 0) WHERE guild_id = $1",
                gid, amount_raw,
            )
            await audit_log(db, gid, int(admin["user_id"]), "treasury_drain",
                            {"amount": body.amount})
        return SuccessResponse(message=f"Drained {body.amount} USD from treasury.")

    else:
        raise ValidationError("Action must be 'give' or 'drain'.")
