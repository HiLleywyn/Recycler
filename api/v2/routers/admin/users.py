"""Admin user management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import NotFoundError
from api.v2.schemas.admin import GiveRequest, SetBalanceRequest, TakeRequest
from api.v2.schemas.common import SuccessResponse
from api.v2.routers.admin._helpers import audit_log

router = APIRouter()


async def _ensure_user(db, uid: int, gid: int) -> None:
    """Raise NotFoundError if user does not exist in this guild."""
    row = await db.fetchrow(
        "SELECT user_id FROM users WHERE user_id = $1 AND guild_id = $2",
        uid, gid,
    )
    if not row:
        raise NotFoundError("User not found in this guild.")


@router.post("/users/{user_id}/give", response_model=SuccessResponse, summary="Give USD to user")
async def give_user(
    user_id: int,
    body: GiveRequest,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Give USD to a user's wallet."""
    gid = int(admin["guild_id"])
    await _ensure_user(db, user_id, gid)

    await db.execute(
        "UPDATE users SET wallet = wallet + $3 WHERE user_id = $1 AND guild_id = $2",
        user_id, gid, body.amount,
    )
    await audit_log(db, gid, int(admin["user_id"]), "give_user",
                    {"target": str(user_id), "amount": body.amount})
    return SuccessResponse(message=f"Gave {body.amount} USD to user {user_id}.")


@router.post("/users/{user_id}/take", response_model=SuccessResponse, summary="Take USD from user")
async def take_user(
    user_id: int,
    body: TakeRequest,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Take USD from a user's wallet."""
    gid = int(admin["guild_id"])
    await _ensure_user(db, user_id, gid)

    await db.execute(
        "UPDATE users SET wallet = GREATEST(wallet - $3, 0) WHERE user_id = $1 AND guild_id = $2",
        user_id, gid, body.amount,
    )
    await audit_log(db, gid, int(admin["user_id"]), "take_user",
                    {"target": str(user_id), "amount": body.amount})
    return SuccessResponse(message=f"Took {body.amount} USD from user {user_id}.")


@router.post("/users/{user_id}/set-balance", response_model=SuccessResponse, summary="Set user balance")
async def set_user_balance(
    user_id: int,
    body: SetBalanceRequest,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Set a user's wallet and/or bank balance."""
    gid = int(admin["guild_id"])
    await _ensure_user(db, user_id, gid)

    if body.wallet is not None:
        await db.execute(
            "UPDATE users SET wallet = $3 WHERE user_id = $1 AND guild_id = $2",
            user_id, gid, body.wallet,
        )
    if body.bank is not None:
        await db.execute(
            "UPDATE users SET bank = $3 WHERE user_id = $1 AND guild_id = $2",
            user_id, gid, body.bank,
        )
    details = {}
    if body.wallet is not None:
        details["wallet"] = body.wallet
    if body.bank is not None:
        details["bank"] = body.bank
    await audit_log(db, gid, int(admin["user_id"]), "set_balance",
                    {"target": str(user_id), **details})
    return SuccessResponse(message=f"Balance set for user {user_id}.")


@router.post("/users/{user_id}/reset", response_model=SuccessResponse, summary="Reset user")
async def reset_user(
    user_id: int,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Reset a user's economy data (wallet, bank, holdings, loans, etc.)."""
    gid = int(admin["guild_id"])
    await _ensure_user(db, user_id, gid)

    # Single source of truth: the comprehensive table list lives in
    # ``database/users.py::reset_user``. The dashboard endpoint used to
    # duplicate a small subset of that delete loop and silently fell
    # behind every time a new V3 economy (safety module, gamba, disc.fun,
    # farming, crafting, etc.) added user-scoped tables.
    await db.reset_user(user_id, gid)
    await audit_log(db, gid, int(admin["user_id"]), "reset_user",
                    {"target": str(user_id)})
    return SuccessResponse(message=f"User {user_id} reset to defaults.")


@router.post("/reset-server", response_model=SuccessResponse, summary="Reset entire server")
async def reset_server(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Reset the entire server's economy data. Dangerous operation."""
    gid = int(admin["guild_id"])

    async with db.transaction():
        # Reset all users in this guild
        await db.execute(
            "UPDATE users SET wallet = 100.0, bank = 0, daily_streak = 0 WHERE guild_id = $1",
            gid,
        )
        # Clear guild-scoped tables
        for table in (
            "crypto_holdings", "wallet_holdings", "loans",
            "savings_deposits", "stakes", "lp_positions", "lp_snapshots",
            "hashstones", "lockstones", "vaultstones",
            "user_profiles", "user_badges", "pnl_snapshots",
            "mining_rigs", "rig_chain_assignments",
        ):
            await db.execute(f"DELETE FROM {table} WHERE guild_id = $1", gid)

        await audit_log(db, gid, int(admin["user_id"]), "reset_server", {})
    return SuccessResponse(message="Server economy data reset.")
