"""V3 API surface.

Consolidated router for the V3 systems so a single ``include_router``
mounts mastery / clan_wars / cosmetics / apex_events / inbox / bottleneck /
render endpoints. Splitting into one-file-per-system added registry
churn without buying anything -- every endpoint here is a thin read
against the V3 service layer.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from api.v2.dependencies import get_current_user, get_db
from api.v2.exceptions import NotFoundError, ValidationError


router = APIRouter(prefix="/v3", tags=["v3"])


# ── Mastery ────────────────────────────────────────────────────────────
@router.get("/mastery/{uid}")
async def mastery_for(
    uid: int, gid: int,
    db: Any = Depends(get_db),
) -> dict:
    """Return the user's mastery summary in a given guild."""
    from services import mastery as svc
    summary = await svc.mastery_summary(db, uid, gid)
    return {
        "tracks": summary.tracks,
        "points_available": summary.points_available,
        "points_spent": summary.points_spent,
        "unlocked": sorted(summary.unlocked),
        "resets_used": summary.resets_used,
    }


@router.post("/mastery/{uid}/unlock")
async def mastery_unlock(
    uid: int, gid: int, node_id: str,
    db: Any = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """Unlock a node for the caller (must match ``uid``)."""
    if int(getattr(user, "id", 0)) != int(uid):
        raise ValidationError("uid mismatch")
    from services import mastery as svc
    ok, msg = await svc.unlock_node(db, uid, gid, node_id)
    if not ok:
        raise ValidationError(msg)
    return {"ok": True, "message": msg}


# ── Cosmetics ──────────────────────────────────────────────────────────
@router.get("/cosmetics/{uid}")
async def cosmetics_for(uid: int, db: Any = Depends(get_db)) -> dict:
    from services import cosmetics as svc
    return {
        "owned": await svc.list_owned(db, uid),
        "equipped": await svc.equipped(db, uid),
        "inventory": await svc.inventory(db, uid),
    }


@router.post("/cosmetics/{uid}/equip")
async def cosmetics_equip(
    uid: int, slot: str, item_id: str,
    db: Any = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    if int(getattr(user, "id", 0)) != int(uid):
        raise ValidationError("uid mismatch")
    from services import cosmetics as svc
    ok, msg = await svc.equip(db, uid, slot, item_id)
    if not ok:
        raise ValidationError(msg)
    return {"ok": True, "message": msg}


# ── Inbox ──────────────────────────────────────────────────────────────
@router.get("/inbox/{uid}")
async def inbox_for(uid: int, db: Any = Depends(get_db)) -> dict:
    from services import inbox as svc
    msgs = await svc.recent(db, uid, limit=50)
    unread = await svc.unread_count(db, uid)
    return {"unread": unread, "messages": msgs}


@router.post("/inbox/{uid}/read")
async def inbox_read(
    uid: int, msg_id: int,
    db: Any = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    if int(getattr(user, "id", 0)) != int(uid):
        raise ValidationError("uid mismatch")
    from services import inbox as svc
    await svc.read(db, uid, msg_id)
    return {"ok": True}


# ── Apex Events ────────────────────────────────────────────────────────
@router.get("/events/apex/active")
async def apex_active(gid: int, db: Any = Depends(get_db)) -> dict:
    from services import apex_events as svc
    rows = await svc.active(db, gid)
    return {"active": rows}


@router.get("/events/apex/history")
async def apex_history(gid: int, db: Any = Depends(get_db)) -> dict:
    rows = await db.fetch_all(
        "SELECT event_id, started_at, ended_at, modifiers "
        "FROM apex_events_history WHERE guild_id = $1 "
        "ORDER BY started_at DESC LIMIT 25",
        gid,
    )
    return {"history": [dict(r) for r in rows]}


# ── Clan Wars ──────────────────────────────────────────────────────────
@router.get("/wars/active")
async def wars_active(gid: int, db: Any = Depends(get_db)) -> dict:
    rows = await db.fetch_all(
        "SELECT * FROM clan_war_matches WHERE guild_id = $1 AND status = 'live'",
        gid,
    )
    return {"active": [dict(r) for r in rows]}


@router.get("/wars/{match_id}")
async def war_detail(match_id: int, db: Any = Depends(get_db)) -> dict:
    from services import clan_wars as svc
    match = await svc.get_match(db, match_id)
    if not match:
        raise NotFoundError(f"Match {match_id} not found")
    return {
        "match": match,
        "nodes": await svc.node_scores(db, match_id),
        "scoreline": await svc.scoreline(db, match_id),
    }


# ── Wealth Bottleneck (rank-based gain throttle + inline UBI) ──────────
@router.get("/bottleneck/curve")
async def bottleneck_curve_endpoint() -> dict:
    """Return the active multiplier curve so the dashboard can render it."""
    from core.config import Config
    from services.bottleneck import BOTTLENECK_DEFAULT_CURVE, percentile_label
    raw = list(getattr(Config, "BOTTLENECK_CURVE", BOTTLENECK_DEFAULT_CURVE))
    return {
        "curve": [
            {
                "percentile": float(p),
                "multiplier": float(m),
                "label": percentile_label(float(p)),
            }
            for p, m in raw
        ],
        "min_holders": int(getattr(Config, "BOTTLENECK_MIN_HOLDERS", 5)),
        "max_boost_multiple_of_gross": float(getattr(
            Config, "BOTTLENECK_MAX_BOOST_MULTIPLE_OF_GROSS", 1.0,
        )),
    }


@router.get("/bottleneck/pool/{gid}")
async def bottleneck_pool_endpoint(gid: int, db: Any = Depends(get_db)) -> dict:
    """Per-guild USD-stable bottleneck pool snapshot."""
    from services.bottleneck import get_pool_state
    return await get_pool_state(db, gid)


@router.get("/bottleneck/{uid}")
async def bottleneck_for_user(
    uid: int, gid: int, days: int = 7, db: Any = Depends(get_db),
) -> dict:
    """User-side breakdown: percentile, multiplier, recent drag/boost totals."""
    from services.bottleneck import (
        bottleneck_multiplier, get_user_history, lookup_percentile,
        percentile_label,
    )
    pctile, nw_usd, n = await lookup_percentile(db, uid=uid, gid=gid)
    mult = bottleneck_multiplier(pctile)
    hist = await get_user_history(db, uid=uid, gid=gid, days=days)
    return {
        "uid": uid,
        "gid": gid,
        "percentile": pctile,
        "label": percentile_label(pctile),
        "multiplier": mult,
        "net_worth_usd": nw_usd,
        "n_holders": n,
        "history": hist,
    }


# ── Render endpoints (so the frontend can show identical PNGs) ─────────
@router.get("/render/profile/{uid}.png")
async def render_profile(uid: int, gid: int, db: Any = Depends(get_db)) -> Response:
    from services import cosmetics, mastery
    from services.profile_render import render_profile_card
    eq = await cosmetics.equipped(db, uid)
    try:
        from services.net_worth import compute_net_worth
        nw = await compute_net_worth(uid, gid, db)
        nw_total = float(nw.total)
    except Exception:
        nw_total = 0.0
    ms = await mastery.mastery_summary(db, uid, gid)
    png = render_profile_card(
        user_name=f"User {uid}",
        equipped=eq,
        net_worth_usd=nw_total,
        mastery_summary={
            "tracks": ms.tracks,
            "unlocked_count": len(ms.unlocked),
        },
    )
    return Response(content=png, media_type="image/png")


@router.get("/render/mastery/{uid}.png")
async def render_mastery(uid: int, gid: int, db: Any = Depends(get_db)) -> Response:
    from services import mastery as svc
    from services.mastery_render import render_mastery_board
    summary = await svc.mastery_summary(db, uid, gid)
    png = render_mastery_board(summary, display_name=f"User {uid}")
    return Response(content=png, media_type="image/png")


@router.get("/render/war/{match_id}.png")
async def render_war(match_id: int, db: Any = Depends(get_db)) -> Response:
    from datetime import datetime, timezone
    from services import clan_wars as svc
    from services.war_render import render_war_map
    match = await svc.get_match(db, match_id)
    if not match:
        raise NotFoundError(f"Match {match_id} not found")
    ends = match["ends_at"]
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    rem = int((ends - datetime.now(timezone.utc)).total_seconds())
    nodes = await svc.node_scores(db, match_id)
    png = render_war_map(
        match, nodes,
        group_a_name=f"Group {match['group_a_id']}",
        group_b_name=f"Group {match['group_b_id']}",
        time_remaining_sec=max(0, rem),
    )
    return Response(content=png, media_type="image/png")


