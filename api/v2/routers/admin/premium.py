"""
Premium subscription admin endpoints.

Bot-owner-only management surface for the per-guild premium feature gate.
Mirrors what ,admin premium grant/revoke/list/status/sync/link do from
Discord, exposed for the dashboard so the operator can manage paid
guilds without leaving the browser.

All write paths route through ``services/entitlements.py``, which means
every mutation here also produces a ``staff_audit_log`` row in the host
guild (no second audit pass needed in this file).

Auth: ``require_bot_owner`` -- guild admins can NOT use these endpoints
(the whole point of the gate is that the server owner pays).

Endpoints:

    GET  /api/v2/admin/premium/guilds              -- list all rows
    GET  /api/v2/admin/premium/guilds/{guild_id}   -- single row + status
    POST /api/v2/admin/premium/guilds/{guild_id}/grant   -- grant premium
    POST /api/v2/admin/premium/guilds/{guild_id}/revoke  -- revoke
    POST /api/v2/admin/premium/guilds/{guild_id}/gift    -- gift + try-notify
    POST /api/v2/admin/premium/guilds/{guild_id}/link    -- attach PayPal
    POST /api/v2/admin/premium/guilds/{guild_id}/sync    -- refresh from PayPal
    POST /api/v2/admin/premium/expire                    -- run sweep now
    GET  /api/v2/admin/premium/webhooks                  -- recent events
    GET  /api/v2/admin/premium/features                  -- the FEATURES dict
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api.v2.dependencies import get_orm_db, require_bot_owner
from api.v2.exceptions import NotFoundError, ValidationError
from services import entitlements
from services.paypal import (
    PayPalError, paypal_client, parse_iso8601_to_epoch,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/premium", tags=["admin-premium"])


# ── request models ────────────────────────────────────────────────────


class GrantRequest(BaseModel):
    days: int | None = Field(None, ge=1, le=3650, description="Days to grant (omit for indefinite)")
    notes: str | None = Field(None, max_length=1000)


class GiftRequest(BaseModel):
    days: int = Field(..., ge=1, le=3650)
    message: str | None = Field(None, max_length=1000, description="Personal note shown to recipient")


class RevokeRequest(BaseModel):
    reason: str | None = Field(None, max_length=500)


class LinkRequest(BaseModel):
    subscription_id: str = Field(..., min_length=1, max_length=64)


# ── response models ───────────────────────────────────────────────────


class PremiumStatusOut(BaseModel):
    guild_id: int
    is_premium: bool
    source: str
    tier: str
    status: str
    expires_at: float | None
    current_period_end: float | None
    subscriber_user_id: int | None
    paypal_subscription_id: str | None
    notes: str | None
    started_at: float | None


def _status_to_out(gid: int, s: entitlements.PremiumStatus) -> PremiumStatusOut:
    return PremiumStatusOut(
        guild_id=int(gid),
        is_premium=s.is_premium,
        source=s.source,
        tier=s.tier,
        status=s.status,
        expires_at=s.expires_at,
        current_period_end=s.current_period_end,
        subscriber_user_id=s.subscriber_user_id,
        paypal_subscription_id=s.paypal_subscription_id,
        notes=s.notes,
        started_at=s.started_at,
    )


# ── reads ─────────────────────────────────────────────────────────────


@router.get("/guilds", summary="List every premium guild")
async def list_premium_guilds(
    user: dict = Depends(require_bot_owner),
    db = Depends(get_orm_db),
):
    return await entitlements.list_premium_guilds(db)


@router.get("/guilds/{guild_id}", response_model=PremiumStatusOut,
            summary="Single guild's premium status")
async def get_premium_status(
    guild_id: int,
    user: dict = Depends(require_bot_owner),
    db = Depends(get_orm_db),
):
    s = await entitlements.get_status(int(guild_id), db)
    return _status_to_out(guild_id, s)


@router.get("/features", summary="The full premium feature catalog")
async def list_features(user: dict = Depends(require_bot_owner)):
    """Returns the same dict that ``,premium features`` shows in chat."""
    return entitlements.PREMIUM_FEATURES


@router.get("/webhooks", summary="Recent PayPal webhook events")
async def list_webhooks(
    limit: int = Query(50, ge=1, le=500),
    only_unprocessed: bool = Query(False),
    user: dict = Depends(require_bot_owner),
    db = Depends(get_orm_db),
):
    """Recent rows from the ``paypal_webhook_events`` ledger.

    Useful for diagnosing missed activations -- if PayPal fired the webhook
    but processed_at is NULL or error is set, the event is in the queue and
    the operator can re-run it via ``POST .../guilds/{gid}/sync``.
    """
    if only_unprocessed:
        sql = (
            "SELECT event_id, event_type, resource_id, error, "
            "       EXTRACT(EPOCH FROM received_at)  AS received_epoch, "
            "       EXTRACT(EPOCH FROM processed_at) AS processed_epoch "
            "FROM paypal_webhook_events WHERE processed_at IS NULL "
            "ORDER BY received_at DESC LIMIT $1"
        )
    else:
        sql = (
            "SELECT event_id, event_type, resource_id, error, "
            "       EXTRACT(EPOCH FROM received_at)  AS received_epoch, "
            "       EXTRACT(EPOCH FROM processed_at) AS processed_epoch "
            "FROM paypal_webhook_events ORDER BY received_at DESC LIMIT $1"
        )
    rows = await db.fetch_all(sql, int(limit))
    return rows


# ── writes ────────────────────────────────────────────────────────────


@router.post("/guilds/{guild_id}/grant", response_model=PremiumStatusOut,
             summary="Grant premium to a guild")
async def grant(
    guild_id: int,
    body: GrantRequest,
    user: dict = Depends(require_bot_owner),
    db = Depends(get_orm_db),
):
    try:
        s = await entitlements.grant_premium(
            int(guild_id), db,
            days=body.days, granted_by=int(user.get("user_id", 0)),
            source="admin", notes=body.notes,
        )
    except ValueError as exc:
        raise ValidationError(str(exc))
    return _status_to_out(guild_id, s)


@router.post("/guilds/{guild_id}/gift", response_model=PremiumStatusOut,
             summary="Gift premium to a guild (audited as premium.gift)")
async def gift(
    guild_id: int,
    body: GiftRequest,
    user: dict = Depends(require_bot_owner),
    db = Depends(get_orm_db),
):
    """Same write as grant, but tagged with source='gift' so the recipient
    sees it as a gift in ``,premium status`` and the audit feed groups it
    separately from administrative grants. The Discord-side ``,admin
    premium gift`` ALSO sends a celebratory embed in the recipient guild;
    this endpoint does NOT (the caller is HTTP, not connected to Discord
    directly), so trigger the chat notification by running the Discord
    command if you need it."""
    try:
        s = await entitlements.grant_premium(
            int(guild_id), db,
            days=body.days, granted_by=int(user.get("user_id", 0)),
            source="gift", notes=body.message,
        )
    except ValueError as exc:
        raise ValidationError(str(exc))
    return _status_to_out(guild_id, s)


@router.post("/guilds/{guild_id}/revoke", response_model=PremiumStatusOut,
             summary="Revoke premium for a guild")
async def revoke(
    guild_id: int,
    body: RevokeRequest,
    user: dict = Depends(require_bot_owner),
    db = Depends(get_orm_db),
):
    s = await entitlements.revoke_premium(
        int(guild_id), db,
        revoked_by=int(user.get("user_id", 0)), reason=body.reason,
    )
    return _status_to_out(guild_id, s)


@router.post("/guilds/{guild_id}/link", response_model=PremiumStatusOut,
             summary="Manually attach a PayPal subscription to a guild")
async def link_subscription(
    guild_id: int,
    body: LinkRequest,
    user: dict = Depends(require_bot_owner),
    db = Depends(get_orm_db),
):
    """Pulls live state from PayPal and writes it through entitlements.
    Use when a webhook delivery was dropped."""
    client = paypal_client()
    if not client.configured:
        raise ValidationError("PayPal is not configured on this instance.")
    try:
        sub = await client.get_subscription(body.subscription_id)
    except PayPalError as exc:
        raise ValidationError(f"PayPal lookup failed: {exc}")
    paypal_status = (sub.get("status") or "").upper()
    status = {
        "ACTIVE":    "active", "APPROVED":  "active",
        "SUSPENDED": "suspended", "CANCELLED": "cancelled", "EXPIRED": "expired",
    }.get(paypal_status, "active")
    billing_info = sub.get("billing_info") or {}
    period_end = parse_iso8601_to_epoch(billing_info.get("next_billing_time"))
    expires_at = period_end
    if status not in ("active", "cancelled"):
        import time as _time
        expires_at = _time.time() - 1.0
    s = await entitlements.link_paypal_subscription(
        int(guild_id), db,
        subscription_id=body.subscription_id,
        plan_id=sub.get("plan_id"),
        subscriber_user_id=int(user.get("user_id", 0)) or None,
        status=status,
        current_period_end_epoch=period_end,
        expires_at_epoch=expires_at,
    )
    return _status_to_out(guild_id, s)


@router.post("/guilds/{guild_id}/sync", response_model=PremiumStatusOut,
             summary="Re-fetch this guild's PayPal subscription state")
async def sync_subscription(
    guild_id: int,
    user: dict = Depends(require_bot_owner),
    db = Depends(get_orm_db),
):
    """Re-pull this guild's PayPal subscription state and reconcile.
    404 if the guild has no PayPal subscription on file."""
    cur = await entitlements.get_status(int(guild_id), db)
    if not cur.paypal_subscription_id:
        raise NotFoundError("Guild has no PayPal subscription linked.")
    return await link_subscription(
        guild_id,
        LinkRequest(subscription_id=cur.paypal_subscription_id),
        user=user, db=db,
    )


@router.post("/expire", summary="Run the overdue-expiry sweep now")
async def expire_now(
    user: dict = Depends(require_bot_owner),
    db = Depends(get_orm_db),
):
    n = await entitlements.expire_overdue(db)
    return {"swept": n}
