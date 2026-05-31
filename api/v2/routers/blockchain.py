"""Blockchain router -- on-chain data and transaction endpoints for Discoin v2."""
from __future__ import annotations

import json
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_current_user, get_db, get_optional_user
from core.framework.scale import to_human
from api.v2.exceptions import NotFoundError
from api.v2.schemas.blockchain import (
    BlockInfo,
    ExplorerSummary,
    MempoolEntry,
    TransactionInfo,
)

router = APIRouter(prefix="/blockchain", tags=["blockchain"])


# ---------------------------------------------------------------------------
# 1. GET /blockchain/blocks
# ---------------------------------------------------------------------------

@router.get("/blocks", response_model=list[BlockInfo], summary="List chain blocks")
async def list_blocks(
    network: str | None = Query(None, description="Filter by network."),
    status: str | None = Query(None, description="Filter by status (pending, confirmed)."),
    limit: int = Query(20, ge=1, le=100, description="Number of blocks."),
    offset: int = Query(0, ge=0, description="Offset."),
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List blockchain blocks with optional filters for network and status."""
    gid = int(user["guild_id"])
    params: list[Any] = [gid]
    filters = ""

    if network:
        params.append(network)
        filters += f" AND network = ${len(params)}"
    if status:
        params.append(status)
        filters += f" AND status = ${len(params)}"

    params.append(limit)
    params.append(offset)

    rows = await conn.fetch(
        f"""SELECT block_num, network, status, tx_count, block_hash, miner_id, ts
            FROM chain_blocks
            WHERE guild_id = $1 {filters}
            ORDER BY block_num DESC
            LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
        *params,
    )

    return [
        BlockInfo(
            block_num=r["block_num"],
            network=r["network"],
            status=r["status"],
            tx_count=r["tx_count"],
            block_hash=r["block_hash"],
            miner_id=r["miner_id"],
            ts=r["ts"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 2. GET /blockchain/blocks/{block_num}
# ---------------------------------------------------------------------------

@router.get("/blocks/{block_num}", response_model=BlockInfo, summary="Get single block")
async def get_block(
    block_num: int,
    network: str = Query("", description="Network name (empty for default)."),
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get details for a single block by block number."""
    gid = int(user["guild_id"])
    row = await conn.fetchrow(
        """SELECT block_num, network, status, tx_count, block_hash, miner_id, ts
           FROM chain_blocks
           WHERE guild_id = $1 AND block_num = $2 AND network = $3""",
        gid, block_num, network,
    )
    if not row:
        raise NotFoundError(f"Block #{block_num} not found.")

    return BlockInfo(
        block_num=row["block_num"],
        network=row["network"],
        status=row["status"],
        tx_count=row["tx_count"],
        block_hash=row["block_hash"],
        miner_id=row["miner_id"],
        ts=row["ts"],
    )


# ---------------------------------------------------------------------------
# 3. GET /blockchain/blocks/{block_num}/txs
# ---------------------------------------------------------------------------

@router.get("/blocks/{block_num}/txs", response_model=list[TransactionInfo], summary="Block transactions")
async def block_transactions(
    block_num: int,
    limit: int = Query(50, ge=1, le=200, description="Number of transactions."),
    offset: int = Query(0, ge=0, description="Offset."),
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List all transactions included in a specific block."""
    gid = int(user["guild_id"])
    rows = await conn.fetch(
        """SELECT tx_hash, tx_type, user_id, symbol_in, amount_in, symbol_out, amount_out,
                  gas_fee, block_num, ts
           FROM transactions
           WHERE guild_id = $1 AND block_num = $2
           ORDER BY ts DESC
           LIMIT $3 OFFSET $4""",
        gid, block_num, limit, offset,
    )

    return [
        TransactionInfo(
            tx_hash=r["tx_hash"],
            tx_type=r["tx_type"],
            user_id=r["user_id"],
            symbol_in=r["symbol_in"],
            amount_in=to_human(int(r["amount_in"])) if r["amount_in"] is not None else None,
            symbol_out=r["symbol_out"],
            amount_out=to_human(int(r["amount_out"])) if r["amount_out"] is not None else None,
            gas_fee=to_human(int(r["gas_fee"])),
            block_num=r["block_num"],
            ts=r["ts"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 4. GET /blockchain/transactions
# ---------------------------------------------------------------------------

@router.get("/transactions", response_model=list[TransactionInfo], summary="Recent transactions")
async def list_transactions(
    tx_type: str | None = Query(None, description="Filter by transaction type."),
    user_id: int | None = Query(None, description="Filter by user ID."),
    symbol: str | None = Query(None, description="Filter by token symbol."),
    limit: int = Query(20, ge=1, le=100, description="Number of transactions."),
    offset: int = Query(0, ge=0, description="Offset."),
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List recent transactions with optional filters."""
    gid = int(user["guild_id"])
    params: list[Any] = [gid]
    filters = ""

    if tx_type:
        params.append(tx_type)
        filters += f" AND tx_type = ${len(params)}"
    if user_id is not None:
        params.append(user_id)
        filters += f" AND user_id = ${len(params)}"
    if symbol:
        params.append(symbol)
        filters += f" AND (symbol_in = ${len(params)} OR symbol_out = ${len(params)})"

    params.append(limit)
    params.append(offset)

    rows = await conn.fetch(
        f"""SELECT tx_hash, tx_type, user_id, symbol_in, amount_in, symbol_out, amount_out,
                   gas_fee, block_num, ts
            FROM transactions
            WHERE guild_id = $1 {filters}
            ORDER BY ts DESC
            LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
        *params,
    )

    return [
        TransactionInfo(
            tx_hash=r["tx_hash"],
            tx_type=r["tx_type"],
            user_id=r["user_id"],
            symbol_in=r["symbol_in"],
            amount_in=to_human(int(r["amount_in"])) if r["amount_in"] is not None else None,
            symbol_out=r["symbol_out"],
            amount_out=to_human(int(r["amount_out"])) if r["amount_out"] is not None else None,
            gas_fee=to_human(int(r["gas_fee"])),
            block_num=r["block_num"],
            ts=r["ts"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 5. GET /blockchain/transactions/{tx_hash}
# ---------------------------------------------------------------------------

@router.get("/transactions/{tx_hash}", response_model=TransactionInfo, summary="Get transaction")
async def get_transaction(
    tx_hash: str,
    conn: asyncpg.Connection = Depends(get_db),
):
    """Get details for a single transaction by its hash."""
    r = await conn.fetchrow(
        """SELECT tx_hash, tx_type, user_id, symbol_in, amount_in, symbol_out, amount_out,
                  gas_fee, block_num, ts
           FROM transactions
           WHERE tx_hash = $1""",
        tx_hash,
    )
    if not r:
        raise NotFoundError("Transaction not found.")

    return TransactionInfo(
        tx_hash=r["tx_hash"],
        tx_type=r["tx_type"],
        user_id=r["user_id"],
        symbol_in=r["symbol_in"],
        amount_in=to_human(int(r["amount_in"])) if r["amount_in"] is not None else None,
        symbol_out=r["symbol_out"],
        amount_out=to_human(int(r["amount_out"])) if r["amount_out"] is not None else None,
        gas_fee=to_human(int(r["gas_fee"])),
        block_num=r["block_num"],
        ts=r["ts"],
    )


# ---------------------------------------------------------------------------
# 6. GET /blockchain/mempool
# ---------------------------------------------------------------------------

@router.get("/mempool", response_model=list[MempoolEntry], summary="Pending transactions (mempool)")
async def list_mempool(
    network: str | None = Query(None, description="Filter by network."),
    limit: int = Query(50, ge=1, le=200, description="Number of entries."),
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List pending transactions in the mempool, ordered by gas fee (priority)."""
    gid = int(user["guild_id"])
    params: list[Any] = [gid]
    net_filter = ""
    if network:
        params.append(network)
        net_filter = f"AND network = ${len(params)}"

    params.append(limit)

    rows = await conn.fetch(
        f"""SELECT id, action_type, user_id, network, payload, gas_fee, gas_price, status, submitted_at
            FROM mempool
            WHERE guild_id = $1 AND status = 'pending' {net_filter}
            ORDER BY gas_fee DESC, submitted_at ASC
            LIMIT ${len(params)}""",
        *params,
    )

    results = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        elif payload is None:
            payload = {}

        # Extract symbol/amount from payload for display
        symbol = payload.get("symbol", payload.get("symbol_in", ""))
        amount = float(payload.get("amount", payload.get("amount_in", 0)))

        results.append(MempoolEntry(
            id=r["id"],
            tx_type=r["action_type"],
            user_id=r["user_id"],
            network=r["network"],
            symbol=symbol,
            amount=amount,
            gas_fee=to_human(int(r["gas_fee"])),
            gas_price=r["gas_price"],
            status=r["status"],
            ts=r["submitted_at"],
        ))
    return results


# ---------------------------------------------------------------------------
# 7. GET /blockchain/explorer-summary
# ---------------------------------------------------------------------------

@router.get("/explorer-summary", response_model=ExplorerSummary, summary="Blockchain explorer overview")
async def explorer_summary(
    conn: asyncpg.Connection = Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    """Get a high-level overview of the blockchain for the explorer page."""
    if user and user.get("guild_id"):
        gid = int(user["guild_id"])
    else:
        row = await conn.fetchrow("SELECT guild_id FROM chain_blocks LIMIT 1")
        gid = int(row["guild_id"]) if row else 0

    # Total blocks
    total_blocks = await conn.fetchval(
        "SELECT COUNT(*) FROM chain_blocks WHERE guild_id = $1",
        gid,
    ) or 0

    # Total transactions
    total_transactions = await conn.fetchval(
        "SELECT COUNT(*) FROM transactions WHERE guild_id = $1",
        gid,
    ) or 0

    # Total unique users (addresses)
    total_addresses = await conn.fetchval(
        "SELECT COUNT(DISTINCT user_id) FROM transactions WHERE guild_id = $1 AND user_id IS NOT NULL",
        gid,
    ) or 0

    # Active networks
    network_rows = await conn.fetch(
        "SELECT DISTINCT network FROM chain_blocks WHERE guild_id = $1 ORDER BY network",
        gid,
    )
    networks = [r["network"] for r in network_rows if r["network"]]

    # Mempool size
    mempool_size = await conn.fetchval(
        "SELECT COUNT(*) FROM mempool WHERE guild_id = $1 AND status = 'pending'",
        gid,
    ) or 0

    return ExplorerSummary(
        total_blocks=total_blocks,
        total_transactions=total_transactions,
        total_addresses=total_addresses,
        networks=networks,
        mempool_size=mempool_size,
    )
