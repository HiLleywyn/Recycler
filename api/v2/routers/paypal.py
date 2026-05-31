"""
api/v2/routers/paypal.py  -  PayPal Subscriptions webhooks for Discoin Premium.

ONE endpoint: ``POST /api/v2/paypal/webhook``. PayPal hits it for every
subscription lifecycle event. We:

    1. Verify the signature against PayPal's verify-webhook-signature
       endpoint (PAYPAL_WEBHOOK_ID required).
    2. Insert the event into ``paypal_webhook_events`` for idempotency
       and audit. PayPal retries failed deliveries, so the event_id PK
       is what makes re-delivery safe.
    3. Translate the event into a ``link_paypal_subscription`` call so
       the right guild gets unlocked / cancelled / expired.

We rely on the ``custom_id`` field set when the subscription was created
to know which guild the subscription belongs to. ``custom_id`` is just
``str(guild_id)`` set by ,premium subscribe.

Failures are logged and re-raised so PayPal retries -- the only safe
behaviour, otherwise a brief outage means missed activations.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from api.v2.dependencies import get_orm_db
from api.v2.exceptions import UnauthorizedError, ValidationError
from services import entitlements
from services.paypal import paypal_client, parse_iso8601_to_epoch

log = logging.getLogger(__name__)

router = APIRouter(prefix="/paypal", tags=["paypal"])


# Map PayPal subscription event_type -> our internal status. Anything
# not in this dict is logged and ignored.
_STATUS_MAP: dict[str, str] = {
    "BILLING.SUBSCRIPTION.CREATED":   "active",   # actually APPROVAL_PENDING; we only flip on activation
    "BILLING.SUBSCRIPTION.ACTIVATED": "active",
    "BILLING.SUBSCRIPTION.UPDATED":   "active",
    "BILLING.SUBSCRIPTION.RE-ACTIVATED": "active",
    "BILLING.SUBSCRIPTION.CANCELLED": "cancelled",
    "BILLING.SUBSCRIPTION.EXPIRED":   "expired",
    "BILLING.SUBSCRIPTION.SUSPENDED": "suspended",
    "BILLING.SUBSCRIPTION.PAYMENT.FAILED": "suspended",
}


def _extract_guild_id(resource: dict[str, Any]) -> Optional[int]:
    """Pull guild_id out of the subscription resource. We set custom_id =
    str(guild_id) at creation time, so it should always be present."""
    cid = resource.get("custom_id")
    if not cid:
        return None
    try:
        return int(cid)
    except (TypeError, ValueError):
        log.warning("paypal webhook: non-int custom_id=%r", cid)
        return None


def _extract_subscriber_user_id(resource: dict[str, Any]) -> Optional[int]:
    """Subscriber's Discord user_id, if we set it. PayPal returns this in
    custom_id sometimes too -- but we keep custom_id reserved for guild_id.
    Discord user_id is currently surfaced via the subscriber.payer_id
    being a PayPal payer id, NOT a Discord one. So this returns None
    unless a future change adds it."""
    return None


@router.post("/webhook", summary="PayPal Subscriptions webhook")
async def paypal_webhook(
    request: Request,
    db = Depends(get_orm_db),
):
    """Receive PayPal webhooks. Signature-verified + idempotent."""
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8", errors="replace")
    headers = dict(request.headers)

    # ── 1. signature ─────────────────────────────────────────────
    client = paypal_client()
    if not client.configured:
        # Bot was started without PayPal env vars. Reject so the operator
        # notices instead of silently dropping subscription state.
        log.error("paypal webhook hit but PayPal client not configured")
        raise UnauthorizedError("PayPal is not configured on this instance.")
    verified = await client.verify_webhook(headers, body_str)
    if not verified:
        log.warning("paypal webhook: signature verification FAILED")
        raise UnauthorizedError("Invalid PayPal webhook signature.")

    # ── 2. parse + idempotency ───────────────────────────────────
    try:
        event = json.loads(body_str)
    except json.JSONDecodeError:
        raise ValidationError("Body is not valid JSON.")

    event_id = event.get("id") or ""
    event_type = event.get("event_type") or ""
    resource = event.get("resource") or {}
    resource_id = resource.get("id") or ""
    if not event_id or not event_type:
        raise ValidationError("Webhook payload missing id / event_type.")

    existing = await db.fetch_one(
        "SELECT processed_at FROM paypal_webhook_events WHERE event_id = $1",
        event_id,
    )
    if existing and existing.get("processed_at"):
        log.info("paypal webhook %s already processed -- skipping", event_id)
        return {"status": "already_processed", "event_id": event_id}

    await db.execute(
        """
        INSERT INTO paypal_webhook_events (event_id, event_type, resource_id, payload)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id, event_type, resource_id, body_str,
    )

    # ── 3. dispatch ──────────────────────────────────────────────
    try:
        await _handle_event(event_type, resource, db)
    except Exception as exc:
        log.exception("paypal webhook handler failed event=%s type=%s",
                      event_id, event_type)
        await db.execute(
            "UPDATE paypal_webhook_events SET error = $2 WHERE event_id = $1",
            event_id, str(exc)[:500],
        )
        # Re-raise so PayPal retries. The row stays unprocessed so the
        # next attempt repeats the dispatch (handler is idempotent).
        raise

    await db.execute(
        "UPDATE paypal_webhook_events SET processed_at = NOW(), error = NULL "
        "WHERE event_id = $1",
        event_id,
    )
    return {"status": "ok", "event_id": event_id, "event_type": event_type}


async def _handle_event(event_type: str, resource: dict[str, Any], db) -> None:
    """Translate a PayPal subscription event into entitlement state."""

    # PAYMENT.SALE.COMPLETED is fired on each successful renewal payment.
    # We don't get the full subscription resource here, just billing_agreement_id
    # which equals the subscription_id. Refresh from PayPal to pick up the
    # new next_billing_time.
    if event_type == "PAYMENT.SALE.COMPLETED":
        sub_id = resource.get("billing_agreement_id") or ""
        if not sub_id:
            log.info("paypal sale.completed without billing_agreement_id -- ignoring")
            return
        await _refresh_from_paypal(sub_id, db)
        return

    if event_type not in _STATUS_MAP:
        log.info("paypal webhook: ignoring event_type=%s", event_type)
        return

    sub_id = resource.get("id") or ""
    if not sub_id:
        log.warning("paypal webhook: %s missing resource.id", event_type)
        return

    gid = _extract_guild_id(resource)
    if gid is None:
        # Fallback: maybe we already have the row keyed by sub_id from a
        # prior event. Look it up so we can still update status.
        existing = await entitlements.find_by_paypal_subscription(sub_id, db)
        if not existing:
            log.warning(
                "paypal webhook: %s sub=%s has no custom_id and no existing row -- skipping",
                event_type, sub_id,
            )
            return
        gid = int(existing["guild_id"])

    # CREATED fires before the user has approved + paid. Don't activate
    # until ACTIVATED arrives -- otherwise we'd unlock for a free trial
    # period that never gets paid.
    if event_type == "BILLING.SUBSCRIPTION.CREATED":
        log.info("paypal sub created (pending approval) gid=%s sub=%s", gid, sub_id)
        return

    status = _STATUS_MAP[event_type]
    plan_id = resource.get("plan_id")
    billing_info = resource.get("billing_info") or {}
    next_billing_iso = billing_info.get("next_billing_time")
    period_end_epoch = parse_iso8601_to_epoch(next_billing_iso)

    # On cancel / expired / suspended we set expires_at = period end so
    # the user keeps access through what they already paid for. PayPal
    # API returns ``billing_info.final_payment_time`` or similar, but
    # next_billing_time is reliable across event types.
    if status == "active":
        expires_epoch = period_end_epoch  # active row expires at end of paid period
    elif status == "cancelled":
        # Honour remaining paid period if PayPal told us.
        expires_epoch = period_end_epoch
    else:
        # 'expired' / 'suspended' -> revoke immediately by setting expires
        # to the past. The is_premium check rejects rows past expires_at.
        expires_epoch = parse_iso8601_to_epoch(
            (billing_info.get("last_payment") or {}).get("time")
        )
        if expires_epoch is None:
            import time
            expires_epoch = time.time() - 1.0

    await entitlements.link_paypal_subscription(
        gid, db,
        subscription_id=sub_id,
        plan_id=plan_id,
        subscriber_user_id=_extract_subscriber_user_id(resource),
        status=status,
        current_period_end_epoch=period_end_epoch,
        expires_at_epoch=expires_epoch,
    )


async def _refresh_from_paypal(sub_id: str, db) -> None:
    """Pull the live subscription state from PayPal and write it through.
    Used on PAYMENT.SALE.COMPLETED so renewals extend the period."""
    client = paypal_client()
    sub = await client.get_subscription(sub_id)
    gid = _extract_guild_id(sub)
    if gid is None:
        existing = await entitlements.find_by_paypal_subscription(sub_id, db)
        if not existing:
            log.warning("paypal refresh: sub=%s has no guild link -- skipping", sub_id)
            return
        gid = int(existing["guild_id"])

    paypal_status = (sub.get("status") or "").upper()
    # PayPal: APPROVAL_PENDING / APPROVED / ACTIVE / SUSPENDED / CANCELLED / EXPIRED
    status = {
        "ACTIVE":    "active",
        "APPROVED":  "active",
        "SUSPENDED": "suspended",
        "CANCELLED": "cancelled",
        "EXPIRED":   "expired",
    }.get(paypal_status, "active")
    billing_info = sub.get("billing_info") or {}
    period_end_epoch = parse_iso8601_to_epoch(billing_info.get("next_billing_time"))
    expires_epoch = period_end_epoch
    if status not in ("active", "cancelled"):
        import time
        expires_epoch = time.time() - 1.0

    await entitlements.link_paypal_subscription(
        gid, db,
        subscription_id=sub_id,
        plan_id=sub.get("plan_id"),
        subscriber_user_id=None,
        status=status,
        current_period_end_epoch=period_end_epoch,
        expires_at_epoch=expires_epoch,
    )
