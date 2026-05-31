"""Admin operations: halts, pools, blocks, backup, health, audit log."""
from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Depends, Query

from core.framework.scale import to_human, to_raw
from api.v2.dependencies import get_db, get_redis, require_admin
from api.v2.exceptions import NotFoundError, ValidationError
from api.v2.schemas.admin import (
    AuditLogEntry,
    BackupInfo,
    BlockBundleRequest,
    BlockRejectRequest,
    BlockStatus,
    HaltInfo,
    HaltRequest,
    HealthInfo,
)
from api.v2.schemas.common import PaginatedResponse, SuccessResponse
from api.v2.routers.admin._helpers import audit_log
from api.v2.utils import to_iso
from api.v2.ws.manager import manager as ws_manager

router = APIRouter()

_start_time = time.time()


# ---- Halts -----------------------------------------------------------------

@router.get("/halts", response_model=list[HaltInfo], summary="List network halt statuses")
async def list_halts(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return halt status of all networks."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow(
        "SELECT halted_networks FROM guild_settings WHERE guild_id = $1", gid,
    )
    halted_str = row["halted_networks"] if row else ""
    halted_set = set(h.strip() for h in halted_str.split(",") if h.strip())

    networks = await db.fetch(
        "SELECT network_name FROM guild_networks WHERE guild_id = $1 ORDER BY network_name",
        gid,
    )
    return [
        HaltInfo(network=n["network_name"], halted=n["network_name"] in halted_set)
        for n in networks
    ]


@router.post("/halts", response_model=SuccessResponse, summary="Halt or resume network")
async def toggle_halt(
    body: HaltRequest,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Halt or resume a network. Toggles the current state."""
    gid = int(admin["guild_id"])
    row = await db.fetchrow(
        "SELECT halted_networks FROM guild_settings WHERE guild_id = $1", gid,
    )
    halted_str = row["halted_networks"] if row else ""
    halted_set = set(h.strip() for h in halted_str.split(",") if h.strip())

    if body.network in halted_set:
        halted_set.discard(body.network)
        action = "resumed"
    else:
        halted_set.add(body.network)
        action = "halted"

    new_str = ",".join(sorted(halted_set))
    await db.execute(
        "UPDATE guild_settings SET halted_networks = $2 WHERE guild_id = $1",
        gid, new_str,
    )
    await audit_log(db, gid, int(admin["user_id"]), f"network_{action}",
                    {"network": body.network})
    return SuccessResponse(message=f"Network {body.network} {action}.")


# ---- Pool management -------------------------------------------------------

@router.post("/pools/{pool_id}/remove", response_model=SuccessResponse, summary="Remove pool")
async def remove_pool(
    pool_id: str,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Remove a liquidity pool."""
    gid = int(admin["guild_id"])
    await db.execute(
        "DELETE FROM pools WHERE pool_id = $1 AND guild_id = $2",
        pool_id, gid,
    )
    await db.execute(
        "DELETE FROM lp_positions WHERE pool_id = $1 AND guild_id = $2",
        pool_id, gid,
    )
    await audit_log(db, gid, int(admin["user_id"]), "remove_pool", {"pool_id": pool_id})
    return SuccessResponse(message=f"Pool {pool_id} removed.")


@router.post("/pools/{pool_id}/rebalance", response_model=SuccessResponse, summary="Rebalance pool")
async def rebalance_pool(
    pool_id: str,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Rebalance a liquidity pool's reserves based on current prices."""
    gid = int(admin["guild_id"])
    pool = await db.fetchrow(
        "SELECT token_a, token_b, reserve_a, reserve_b FROM pools "
        "WHERE pool_id = $1 AND guild_id = $2",
        pool_id, gid,
    )
    if not pool:
        raise NotFoundError("Pool not found.")

    # Get current prices for both tokens
    price_a = await db.fetchval(
        "SELECT price FROM crypto_prices WHERE symbol = $1 AND guild_id = $2",
        pool["token_a"], gid,
    )
    price_b = await db.fetchval(
        "SELECT price FROM crypto_prices WHERE symbol = $1 AND guild_id = $2",
        pool["token_b"], gid,
    )

    if not price_a or not price_b:
        raise ValidationError("Cannot rebalance: missing price data.")

    # Rebalance to equal value on both sides (work in human scale)
    ra_h = to_human(int(pool["reserve_a"] or 0))
    rb_h = to_human(int(pool["reserve_b"] or 0))
    total_value = ra_h * float(price_a) + rb_h * float(price_b)
    new_reserve_a = (total_value / 2) / float(price_a)
    new_reserve_b = (total_value / 2) / float(price_b)

    await db.execute(
        "UPDATE pools SET reserve_a = $3, reserve_b = $4 WHERE pool_id = $1 AND guild_id = $2",
        pool_id, gid, to_raw(new_reserve_a), to_raw(new_reserve_b),
    )
    await audit_log(db, gid, int(admin["user_id"]), "rebalance_pool",
                    {"pool_id": pool_id, "reserve_a": new_reserve_a, "reserve_b": new_reserve_b})
    return SuccessResponse(message=f"Pool {pool_id} rebalanced.")


# ---- Block pipeline --------------------------------------------------------

@router.post("/blocks/bundle", response_model=SuccessResponse, summary="Bundle transactions into block")
async def bundle_block(
    body: BlockBundleRequest,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Bundle pending mempool transactions into a new block."""
    gid = int(admin["guild_id"])
    pending_count = await db.fetchval(
        "SELECT COUNT(*) FROM mempool WHERE guild_id = $1 AND network = $2 AND status = 'pending'",
        gid, body.network,
    )
    if not pending_count:
        return SuccessResponse(message="No pending transactions to bundle.")

    # Get next block number
    max_block = await db.fetchval(
        "SELECT COALESCE(MAX(block_num), 0) FROM chain_blocks WHERE guild_id = $1 AND network = $2",
        gid, body.network,
    )
    new_block = (max_block or 0) + 1
    block_hash = f"0x{uuid.uuid4().hex[:16]}"

    await db.execute(
        """
        INSERT INTO chain_blocks (guild_id, network, block_num, block_hash, tx_count, status)
        VALUES ($1, $2, $3, $4, $5, 'confirmed')
        """,
        gid, body.network, new_block, block_hash, pending_count,
    )
    # Mark mempool items
    await db.execute(
        "UPDATE mempool SET status = 'confirmed' WHERE guild_id = $1 AND network = $2 AND status = 'pending'",
        gid, body.network,
    )
    await audit_log(db, gid, int(admin["user_id"]), "bundle_block",
                    {"network": body.network, "block_num": new_block, "tx_count": pending_count})
    return SuccessResponse(message=f"Block #{new_block} created with {pending_count} transactions.")


@router.post("/blocks/reject", response_model=SuccessResponse, summary="Reject a block")
async def reject_block(
    body: BlockRejectRequest,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Reject and roll back a pending block."""
    gid = int(admin["guild_id"])
    await db.execute(
        "UPDATE chain_blocks SET status = 'rejected' "
        "WHERE guild_id = $1 AND network = $2 AND block_num = $3",
        gid, body.network, body.block_num,
    )
    await audit_log(db, gid, int(admin["user_id"]), "reject_block",
                    {"network": body.network, "block_num": body.block_num})
    return SuccessResponse(message=f"Block #{body.block_num} rejected.")


@router.get("/blocks/status", response_model=list[BlockStatus], summary="Block pipeline status")
async def block_status(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return the status of each network's block pipeline."""
    gid = int(admin["guild_id"])
    networks = await db.fetch(
        "SELECT network_name FROM guild_networks WHERE guild_id = $1",
        gid,
    )
    result = []
    for n in networks:
        name = n["network_name"]
        pending = await db.fetchval(
            "SELECT COUNT(*) FROM mempool WHERE guild_id = $1 AND network = $2 AND status = 'pending'",
            gid, name,
        )
        latest = await db.fetchval(
            "SELECT COALESCE(MAX(block_num), 0) FROM chain_blocks WHERE guild_id = $1 AND network = $2",
            gid, name,
        )
        result.append(BlockStatus(
            network=name,
            pending_txs=pending or 0,
            latest_block=latest or 0,
            status="ok",
        ))
    return result


# ---- Backup ----------------------------------------------------------------

@router.post("/backup", response_model=SuccessResponse, summary="Create backup")
async def create_backup(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Create a server data backup (metadata entry)."""
    gid = int(admin["guild_id"])
    backup_id = f"backup-{uuid.uuid4().hex[:8]}"
    await audit_log(db, gid, int(admin["user_id"]), "create_backup", {"backup_id": backup_id})
    return SuccessResponse(message=f"Backup {backup_id} created.")


@router.get("/backup", response_model=list[BackupInfo], summary="List backups")
async def list_backups(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """List available backups from audit log."""
    gid = int(admin["guild_id"])
    rows = await db.fetch(
        """
        SELECT details->>'backup_id' AS backup_id, created_at
        FROM audit_log
        WHERE guild_id = $1 AND action = 'create_backup'
        ORDER BY created_at DESC
        LIMIT 20
        """,
        gid,
    )
    return [
        BackupInfo(
            id=r["backup_id"] or "unknown",
            created_at=to_iso(r["created_at"]),
            size_bytes=0,
        )
        for r in rows
    ]


@router.delete("/backup", response_model=SuccessResponse, summary="Delete backup")
async def delete_backup(
    backup_id: str = Query(..., description="Backup ID to delete"),
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Delete a backup entry."""
    gid = int(admin["guild_id"])
    await audit_log(db, gid, int(admin["user_id"]), "delete_backup", {"backup_id": backup_id})
    return SuccessResponse(message=f"Backup {backup_id} deleted.")


# ---- Health ----------------------------------------------------------------

@router.get("/health", response_model=HealthInfo, summary="Admin health check")
async def admin_health(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
    redis=Depends(get_redis),
):
    """Return system health information."""
    db_ok = False
    try:
        await db.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        pass

    redis_ok = False
    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        pass

    return HealthInfo(
        db_connected=db_ok,
        redis_connected=redis_ok,
        active_ws_connections=ws_manager.active_connections,
        uptime_seconds=round(time.time() - _start_time, 1),
    )


# ---- Audit log -------------------------------------------------------------

@router.get("/audit-log", response_model=PaginatedResponse, summary="Audit log")
async def get_audit_log(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: str | None = Query(None, description="Filter by action type"),
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return paginated admin audit log."""
    gid = int(admin["guild_id"])

    where = "WHERE guild_id = $1"
    params: list = [gid]
    if action:
        where += " AND action = $2"
        params.append(action)

    total = await db.fetchval(
        f"SELECT COUNT(*) FROM audit_log {where}", *params,
    )
    rows = await db.fetch(
        f"""
        SELECT id, admin_user_id, action, details, created_at
        FROM audit_log
        {where}
        ORDER BY created_at DESC
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """,
        *params, limit, offset,
    )
    items = [
        AuditLogEntry(
            id=r["id"],
            admin_user_id=str(r["admin_user_id"]),
            action=r["action"],
            details=json.loads(r["details"]) if isinstance(r["details"], str) else r["details"],
            created_at=to_iso(r["created_at"]),
        )
        for r in rows
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)
