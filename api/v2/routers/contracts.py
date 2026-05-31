"""Contracts router  -  4 endpoints."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_current_user, get_db, require_module
from api.v2.exceptions import NotFoundError, UnauthorizedError
from api.v2.schemas.contracts import (
    ContractDetail,
    ContractEvent,
    ContractSummary,
    TokenContractInfo,
)
from api.v2.utils import to_iso

router = APIRouter(prefix="/contracts", tags=["contracts"], dependencies=[require_module("validators")])


def _safe_json(value: Any, default: Any = None) -> Any:
    """Return parsed JSON for string values, the value itself otherwise.

    Falls back to *default* when the value is ``None`` or the string contains
    invalid JSON, preventing 500 errors from malformed legacy rows.
    """
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return default
    return value


def _require_guild(user: dict) -> int:
    """Pull guild_id from the JWT payload or 401. Every contracts query is
    guild-scoped  -  smart_contracts/contract_events/token_contracts all carry
    a guild_id column and contracts in one server must never leak into another.
    """
    gid = user.get("guild_id") if user else None
    if gid is None:
        raise UnauthorizedError("Token has no guild_id; re-login required.")
    return int(gid)


@router.get("", response_model=list[ContractSummary], summary="List all contracts")
async def list_contracts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return all deployed smart contracts for the caller's guild."""
    guild_id = _require_guild(user)
    rows = await db.fetch(
        """
        SELECT address, name, network, type, owner_id, is_paused,
               call_count, deployed_at, description
        FROM smart_contracts
        WHERE guild_id = $1
        ORDER BY deployed_at DESC
        LIMIT $2 OFFSET $3
        """,
        guild_id, limit, offset,
    )
    return [
        ContractSummary(
            address=r["address"],
            name=r["name"],
            network=r["network"],
            type=r["type"],
            owner_id=str(r["owner_id"]),
            is_paused=r["is_paused"],
            call_count=r["call_count"],
            deployed_at=to_iso(r["deployed_at"]),
            description=r["description"],
        )
        for r in rows
    ]


@router.get("/token-contracts", response_model=list[TokenContractInfo], summary="Token contract params")
async def list_token_contracts(
    db=Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return token contract parameters (fees, burns, etc.) for the caller's guild."""
    guild_id = _require_guild(user)
    rows = await db.fetch(
        "SELECT symbol, params, created_at, updated_at FROM token_contracts "
        "WHERE guild_id = $1 ORDER BY symbol",
        guild_id,
    )
    return [
        TokenContractInfo(
            symbol=r["symbol"],
            params=_safe_json(r["params"], {}),
            created_at=to_iso(r["created_at"]),
            updated_at=to_iso(r["updated_at"]),
        )
        for r in rows
    ]


@router.get("/{address}", response_model=ContractDetail, summary="Contract details")
async def get_contract(
    address: str,
    db=Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return full details for a specific contract including state and recent events."""
    guild_id = _require_guild(user)
    row = await db.fetchrow(
        """
        SELECT address, name, network, type, owner_id, is_paused,
               call_count, deployed_at, description, definition, state
        FROM smart_contracts
        WHERE address = $1 AND guild_id = $2
        """,
        address, guild_id,
    )
    if not row:
        raise NotFoundError("Contract not found.")

    events_rows = await db.fetch(
        """
        SELECT id, event, data, block_id, ts
        FROM contract_events
        WHERE address = $1 AND guild_id = $2
        ORDER BY ts DESC
        LIMIT 20
        """,
        address, guild_id,
    )
    events = [
        ContractEvent(
            id=e["id"],
            event=e["event"],
            data=_safe_json(e["data"], {}),
            block_id=e["block_id"],
            ts=to_iso(e["ts"]),
        )
        for e in events_rows
    ]

    return ContractDetail(
        address=row["address"],
        name=row["name"],
        network=row["network"],
        type=row["type"],
        owner_id=str(row["owner_id"]),
        is_paused=row["is_paused"],
        call_count=row["call_count"],
        deployed_at=to_iso(row["deployed_at"]),
        description=row["description"],
        definition=_safe_json(row["definition"], {}),
        state=_safe_json(row["state"], {}),
        recent_events=events,
    )


@router.get("/{address}/events", response_model=list[ContractEvent], summary="Contract events")
async def get_contract_events(
    address: str,
    limit: int = Query(50, ge=1, le=500),
    db=Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return the event log for a specific contract scoped to the caller's guild."""
    guild_id = _require_guild(user)
    rows = await db.fetch(
        """
        SELECT id, event, data, block_id, ts
        FROM contract_events
        WHERE address = $1 AND guild_id = $2
        ORDER BY ts DESC
        LIMIT $3
        """,
        address, guild_id, limit,
    )
    return [
        ContractEvent(
            id=r["id"],
            event=r["event"],
            data=_safe_json(r["data"], {}),
            block_id=r["block_id"],
            ts=to_iso(r["ts"]),
        )
        for r in rows
    ]
