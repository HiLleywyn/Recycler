"""Network vault endpoints  -  server progression levels."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.v2.dependencies import get_current_user, get_db
from api.v2.exceptions import NotFoundError
from constants.ui import C_GRAY
from constants.vaults import (
    VAULT_DISPLAY,
    ALL_VAULT_NETWORKS,
    LEVEL_THRESHOLDS,
    MAX_LEVEL,
    level_for_balance,
    next_threshold,
    progress_pct,
)

router = APIRouter(prefix="/vaults", tags=["vaults"])


@router.get("/{guild_id}", summary="Get all vault levels for a guild")
async def get_vaults(
    guild_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    conn=Depends(get_db),
) -> dict[str, Any]:
    rows = await conn.fetch(
        "SELECT network, balance, level FROM network_vaults WHERE guild_id = $1",
        guild_id,
    )
    vault_map = {r["network"]: {"balance": float(r["balance"]), "level": r["level"]} for r in rows}

    result = []
    for net in ALL_VAULT_NETWORKS:
        v = vault_map.get(net, {"balance": 0.0, "level": 0})
        bal = v["balance"]
        lvl = level_for_balance(net, bal)
        nxt = next_threshold(net, lvl)
        pct = progress_pct(net, bal, lvl)
        disp = VAULT_DISPLAY.get(net, {})
        result.append({
            "network": net,
            "display_name": disp.get("name", net.upper()),
            "emoji": disp.get("emoji", ""),
            "color": disp.get("color", C_GRAY),
            "level": lvl,
            "max_level": MAX_LEVEL,
            "balance": bal,
            "next_threshold": nxt,
            "progress": round(pct, 4),
        })

    return {"guild_id": guild_id, "vaults": result}


@router.get("/{guild_id}/{network}", summary="Get single vault detail for a guild")
async def get_vault(
    guild_id: int,
    network: str,
    user: dict[str, Any] = Depends(get_current_user),
    conn=Depends(get_db),
) -> dict[str, Any]:
    network = network.lower()
    if network not in ALL_VAULT_NETWORKS:
        raise NotFoundError(f"Unknown network: {network}")

    row = await conn.fetchrow(
        "SELECT balance, level FROM network_vaults WHERE guild_id = $1 AND network = $2",
        guild_id, network,
    )
    bal = float(row["balance"]) if row else 0.0
    lvl = level_for_balance(network, bal)
    nxt = next_threshold(network, lvl)
    pct = progress_pct(network, bal, lvl)
    disp = VAULT_DISPLAY.get(network, {})
    thresholds = LEVEL_THRESHOLDS.get(network, [])

    return {
        "guild_id": guild_id,
        "network": network,
        "display_name": disp.get("name", network.upper()),
        "emoji": disp.get("emoji", ""),
        "color": disp.get("color", C_GRAY),
        "level": lvl,
        "max_level": MAX_LEVEL,
        "balance": bal,
        "next_threshold": nxt,
        "progress": round(pct, 4),
        "thresholds": thresholds,
    }
