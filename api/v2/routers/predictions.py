"""Prediction market endpoints  -  markets, bets, summaries."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from core.framework.scale import to_human
from api.v2.dependencies import get_current_user, get_optional_user, get_db, require_module
from api.v2.exceptions import NotFoundError
from api.v2.utils import to_iso

router = APIRouter(prefix="/predictions", tags=["predictions"])


@router.get(
    "/summary",
    summary="Prediction market summary stats",
)
async def predictions_summary(
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    guild_id = int(user["guild_id"]) if user else None
    if not guild_id:
        return {"active_markets": 0, "total_pool": 0.0, "user_active_bets": 0}

    markets_row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM prediction_markets WHERE guild_id = $1 AND status = 'open'",
        guild_id,
    )
    active_markets = int(markets_row["cnt"]) if markets_row else 0

    pool_row = await db.fetchrow(
        "SELECT COALESCE(SUM(total_pool), 0) AS total FROM prediction_markets WHERE guild_id = $1 AND status = 'open'",
        guild_id,
    )
    total_pool = float(pool_row["total"]) if pool_row else 0.0

    user_bets = 0
    if user:
        bets_row = await db.fetchrow(
            """SELECT COUNT(*) AS cnt FROM prediction_bets pb
               JOIN prediction_markets pm ON pm.id = pb.market_id
               WHERE pb.user_id = $1 AND pm.guild_id = $2 AND pm.status = 'open'""",
            int(user["user_id"]),
            guild_id,
        )
        user_bets = int(bets_row["cnt"]) if bets_row else 0

    return {
        "active_markets": active_markets,
        "total_pool": round(total_pool, 2),
        "user_active_bets": user_bets,
    }


@router.get(
    "/markets",
    summary="List open prediction markets",
)
async def list_markets(
    status: str = Query("open", description="Filter by status: open, closed, resolved"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    guild_id = int(user["guild_id"]) if user else None
    if not guild_id:
        return {"markets": [], "total": 0}

    rows = await db.fetch(
        """SELECT id, question, description, options, total_pool,
                  status, created_by, created_at, end_time, resolved_at, resolved_option
           FROM prediction_markets
           WHERE guild_id = $1 AND status = $2
           ORDER BY created_at DESC
           LIMIT $3 OFFSET $4""",
        guild_id,
        status,
        limit,
        offset,
    )

    total_row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM prediction_markets WHERE guild_id = $1 AND status = $2",
        guild_id,
        status,
    )

    markets = []
    for r in rows:
        bet_counts = await db.fetch(
            """SELECT option, COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total_amount
               FROM prediction_bets
               WHERE market_id = $1
               GROUP BY option""",
            r["id"],
        )
        options_raw = r["options"]
        if isinstance(options_raw, str):
            options_list = json.loads(options_raw)
        else:
            options_list = list(options_raw) if options_raw else []

        # Map option text back to index for frontend compatibility
        option_to_idx = {opt: i for i, opt in enumerate(options_list)}
        option_stats = {}
        for bc in bet_counts:
            idx = option_to_idx.get(bc["option"], -1)
            if idx >= 0:
                option_stats[idx] = {
                    "bets": int(bc["cnt"]),
                    "amount": float(bc["total_amount"]),
                }

        # resolved_option is TEXT; convert to index for frontend
        resolved_idx = option_to_idx.get(r["resolved_option"]) if r["resolved_option"] else None

        markets.append({
            "id": r["id"],
            "question": r["question"],
            "description": r["description"],
            "options": options_list,
            "option_stats": option_stats,
            "pool_amount": to_human(int(r["total_pool"])) if r["total_pool"] else 0.0,
            "status": r["status"],
            "created_by": str(r["created_by"]),
            "created_at": to_iso(r["created_at"]),
            "closes_at": to_iso(r["end_time"]),
            "resolved_at": to_iso(r["resolved_at"]),
            "winning_option": resolved_idx,
        })

    return {
        "markets": markets,
        "total": int(total_row["cnt"]) if total_row else 0,
    }


@router.get(
    "/market/{market_id}",
    summary="Market details",
)
async def market_detail(
    market_id: int,
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
):
    guild_id = int(user["guild_id"]) if user else None
    if not guild_id:
        raise NotFoundError("Guild context required.")

    row = await db.fetchrow(
        "SELECT * FROM prediction_markets WHERE id = $1 AND guild_id = $2",
        market_id,
        guild_id,
    )
    if not row:
        raise NotFoundError("Market not found.")

    bet_counts = await db.fetch(
        """SELECT option, COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total_amount
           FROM prediction_bets
           WHERE market_id = $1
           GROUP BY option""",
        market_id,
    )

    options_raw = row["options"]
    if isinstance(options_raw, str):
        options_list = json.loads(options_raw)
    else:
        options_list = list(options_raw) if options_raw else []

    option_to_idx = {opt: i for i, opt in enumerate(options_list)}
    option_stats = {}
    for bc in bet_counts:
        idx = option_to_idx.get(bc["option"], -1)
        if idx >= 0:
            option_stats[idx] = {
                "bets": int(bc["cnt"]),
                "amount": float(bc["total_amount"]),
            }

    resolved_idx = option_to_idx.get(row["resolved_option"]) if row["resolved_option"] else None

    total_bets_row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM prediction_bets WHERE market_id = $1",
        market_id,
    )

    return {
        "id": row["id"],
        "question": row["question"],
        "description": row["description"],
        "options": options_list,
        "option_stats": option_stats,
        "pool_amount": to_human(int(row["total_pool"])) if row["total_pool"] else 0.0,
        "status": row["status"],
        "created_by": str(row["created_by"]),
        "created_at": to_iso(row["created_at"]),
        "closes_at": to_iso(row["end_time"]),
        "resolved_at": to_iso(row["resolved_at"]),
        "winning_option": resolved_idx,
        "total_bets": int(total_bets_row["cnt"]) if total_bets_row else 0,
    }


@router.get(
    "/my",
    summary="User's prediction bets",
    dependencies=[require_module("predictions")],
)
async def my_bets(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    rows = await db.fetch(
        """SELECT pb.id, pb.market_id, pb.option, pb.amount, pb.placed_at,
                  pm.question, pm.options, pm.status, pm.resolved_option, pm.total_pool
           FROM prediction_bets pb
           JOIN prediction_markets pm ON pm.id = pb.market_id
           WHERE pb.user_id = $1 AND pm.guild_id = $2
           ORDER BY pb.placed_at DESC
           LIMIT $3 OFFSET $4""",
        user_id,
        guild_id,
        limit,
        offset,
    )

    total_row = await db.fetchrow(
        """SELECT COUNT(*) AS cnt FROM prediction_bets pb
           JOIN prediction_markets pm ON pm.id = pb.market_id
           WHERE pb.user_id = $1 AND pm.guild_id = $2""",
        user_id,
        guild_id,
    )

    bets = []
    for r in rows:
        options_raw = r["options"]
        if isinstance(options_raw, str):
            options_list = json.loads(options_raw)
        else:
            options_list = list(options_raw) if options_raw else []

        option_text = r["option"]
        option_idx = options_list.index(option_text) if option_text in options_list else -1
        won = r["status"] == "resolved" and r["resolved_option"] == option_text

        bets.append({
            "id": r["id"],
            "market_id": r["market_id"],
            "question": r["question"],
            "option_index": option_idx,
            "option_label": option_text,
            "amount": to_human(int(r["amount"] or 0)),
            "placed_at": to_iso(r["placed_at"]),
            "market_status": r["status"],
            "won": won,
        })

    return {
        "bets": bets,
        "total": int(total_row["cnt"]) if total_row else 0,
    }
