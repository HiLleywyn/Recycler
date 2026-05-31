"""
services/entitlements.py  -  Per-guild premium gating for Discoin.

Discoin runs as a single shared bot that any guild can invite. The
trading economy, gambling, bank/profile, and basic buddy management
are free everywhere. Cost-heavy or compute-heavy features (AI, fishing,
crafting, delves, expeditions, buddy battles/breeding/market) are gated
behind a per-guild premium subscription. Server owners pay via PayPal;
the host guild (Config.HOST_GUILD_ID) is auto-unlocked.

This module is the ONE source of truth for "is this guild premium?"
Every gate -- decorator, command, API route -- routes through ``is_premium``
or ``get_status``.

Two write paths:
- ``grant_premium``  -- admin / host-side, optional days
- ``link_paypal_subscription`` -- PayPal webhook, sets period_end + sub_id

Background sweep: ``expire_overdue`` flips ``status = 'expired'`` for any
row past its ``expires_at``. Run it from a tasks loop or on bot startup.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from core.config import Config

log = logging.getLogger(__name__)

# Feature keys must match what @premium_required("...") and the gating
# command surfaces use. Keep the set tight -- adding entries here changes
# what ,premium info advertises as paid.
PREMIUM_FEATURES: dict[str, str] = {
    "ai":              "AI chat (,ask, @mentions, replies), DiscoAI, agents, plugins, web search",
    "fishing":         "Fishing -- casts, baits, fish market",
    "farming":         "Farming -- plots, crops, seasons, HRV/SEED",
    "crafting":        "Crafting -- forge, specialties, gear apply",
    "dungeon":         "Delves -- dungeon runs, classes, room loot",
    "expeditions":     "Expeditions -- buddy treks for resources",
    "buddy_battle":    "Buddy battles & arena ladder",
    "buddy_breeding":  "Buddy nest, daycare, egg deposit/withdraw/hatch",
    "buddy_market":    "Buddy auction house, listings, gifting, egg market",
    "buddy_talk":      "Buddy AI chat (talk to your buddy)",
}


@dataclass
class PremiumStatus:
    """Snapshot of a guild's premium state. ``is_premium`` is the only
    field that should drive a gate decision; the rest are for display."""

    is_premium: bool
    source: str            # 'host' | 'admin' | 'paypal' | 'none'
    tier: str              # 'premium' (room for tiers later)
    status: str            # 'active' | 'cancelled' | 'expired' | 'suspended' | 'none'
    expires_at: Optional[float]            # epoch float or None
    current_period_end: Optional[float]    # epoch float or None
    subscriber_user_id: Optional[int]
    paypal_subscription_id: Optional[str]
    notes: Optional[str]
    started_at: Optional[float]


def is_host_guild(gid: int) -> bool:
    """True if ``gid`` is the host guild OR a configured dev guild.

    Both treatments are identical: every premium feature unlocked, no
    DB row required, gated commands always available. Dev guilds are
    declared in ``Config.DEV_GUILD_IDS`` (comma-separated env var or
    the baked-in default) so the operator can ship without paying
    premium to themselves on staging / personal servers.
    """
    gid_i = int(gid)
    if Config.HOST_GUILD_ID and gid_i == int(Config.HOST_GUILD_ID):
        return True
    if gid_i in Config.DEV_GUILD_IDS:
        return True
    return False


# Backwards-compatible alias used inside this module.
_is_host = is_host_guild


async def is_premium(gid: int, db) -> bool:
    """Hot-path check used by every gated command. Cheap single-row read."""
    if _is_host(gid):
        return True
    row = await db.fetch_one(
        "SELECT status, "
        "       EXTRACT(EPOCH FROM expires_at) AS exp_epoch "
        "FROM guild_premium WHERE guild_id = $1",
        int(gid),
    )
    if not row:
        return False
    if row.get("status") != "active":
        # 'cancelled' is intentionally excluded here -- once the user hits
        # cancel we treat them as non-premium immediately. If you want the
        # period-end grace behaviour instead, change to:
        #     if row.status not in ('active', 'cancelled'): return False
        return False
    exp = row.get("exp_epoch")
    if exp is not None and exp < time.time():
        return False
    return True


async def get_status(gid: int, db) -> PremiumStatus:
    """Full status read for ,premium status / admin views."""
    if _is_host(gid):
        return PremiumStatus(
            is_premium=True,
            source="host",
            tier="premium",
            status="active",
            expires_at=None,
            current_period_end=None,
            subscriber_user_id=None,
            paypal_subscription_id=None,
            notes="Host guild -- auto-unlocked",
            started_at=None,
        )
    row = await db.fetch_one(
        "SELECT tier, status, source, "
        "       subscriber_user_id, paypal_subscription_id, notes, "
        "       EXTRACT(EPOCH FROM started_at)         AS started_epoch, "
        "       EXTRACT(EPOCH FROM expires_at)         AS exp_epoch, "
        "       EXTRACT(EPOCH FROM current_period_end) AS period_end_epoch "
        "FROM guild_premium WHERE guild_id = $1",
        int(gid),
    )
    if not row:
        return PremiumStatus(
            is_premium=False, source="none", tier="free", status="none",
            expires_at=None, current_period_end=None,
            subscriber_user_id=None, paypal_subscription_id=None,
            notes=None, started_at=None,
        )
    exp = row.get("exp_epoch")
    active = row.get("status") == "active" and (exp is None or exp >= time.time())
    return PremiumStatus(
        is_premium=bool(active),
        source=row.get("source") or "none",
        tier=row.get("tier") or "premium",
        status=row.get("status") or "none",
        expires_at=exp,
        current_period_end=row.get("period_end_epoch"),
        subscriber_user_id=row.get("subscriber_user_id"),
        paypal_subscription_id=row.get("paypal_subscription_id"),
        notes=row.get("notes"),
        started_at=row.get("started_epoch"),
    )


async def _audit(
    db,
    *,
    action: str,
    actor_id: int | None,
    target_guild_id: int,
    severity: str = "info",
    details: str = "",
    extra: dict | None = None,
) -> None:
    """Emit a staff_audit_log row to the host guild's audit feed.

    Premium events affect the *target* guild but the bot owner reads the
    feed from the host guild, so that's where we write. ``target_guild_id``
    is captured both as ``staff_audit_log.target_id`` and inside the JSONB
    metadata for downstream queries. Silent on failure -- audit is
    best-effort, never blocks the actual entitlement write.
    """
    if not Config.HOST_GUILD_ID:
        return
    try:
        from core.framework.staff_audit import (
            log_staff_action, SCOPE_ADMIN,
        )
        meta = dict(extra or {})
        meta.setdefault("guild_id", int(target_guild_id))
        await log_staff_action(
            db,
            scope=SCOPE_ADMIN,
            guild_id=int(Config.HOST_GUILD_ID),
            actor_id=int(actor_id) if actor_id is not None else 0,
            target_id=int(target_guild_id),
            action=action,
            severity=severity,
            details=details,
            metadata=meta,
        )
    except Exception:
        log.debug("entitlements._audit failed", exc_info=True)


async def grant_premium(
    gid: int,
    db,
    *,
    days: Optional[int] = None,
    granted_by: Optional[int] = None,
    source: str = "admin",
    tier: str = "premium",
    notes: Optional[str] = None,
) -> PremiumStatus:
    """Admin / host-side grant. ``days=None`` -> indefinite (no expiry).

    Idempotent: re-running on an existing row extends and reactivates it.
    Pass ``days`` as the interval in days (cast to seconds and applied with
    ``$6 * INTERVAL '1 second'`` so we can keep the query parameterised).
    """
    if days is not None and days <= 0:
        raise ValueError("days must be > 0 or None")
    seconds = int(days) * 86400 if days is not None else None
    await db.execute(
        """
        INSERT INTO guild_premium
            (guild_id, tier, status, source, granted_by, notes,
             started_at, expires_at, updated_at)
        VALUES
            ($1, $2, 'active', $3, $4, $5,
             NOW(),
             CASE WHEN $6::BIGINT IS NULL
                  THEN NULL
                  ELSE NOW() + ($6 * INTERVAL '1 second')
             END,
             NOW())
        ON CONFLICT (guild_id) DO UPDATE SET
            tier         = EXCLUDED.tier,
            status       = 'active',
            source       = EXCLUDED.source,
            granted_by   = EXCLUDED.granted_by,
            notes        = COALESCE(EXCLUDED.notes, guild_premium.notes),
            expires_at   = CASE
                WHEN $6::BIGINT IS NULL THEN NULL
                ELSE GREATEST(
                    COALESCE(guild_premium.expires_at, NOW()),
                    NOW()
                ) + ($6 * INTERVAL '1 second')
            END,
            cancelled_at = NULL,
            updated_at   = NOW()
        """,
        int(gid), tier, source,
        int(granted_by) if granted_by is not None else None,
        notes,
        seconds,
    )
    log.info(
        "premium.grant guild=%s days=%s source=%s by=%s",
        gid, days, source, granted_by,
    )
    await _audit(
        db,
        action=f"premium.{source}" if source in ("gift",) else "premium.grant",
        actor_id=granted_by,
        target_guild_id=int(gid),
        severity="warn",
        details=(
            f"Granted {days}d to guild {gid}" if days
            else f"Granted indefinite premium to guild {gid}"
        ),
        extra={"days": days, "source": source, "tier": tier, "notes": notes},
    )
    return await get_status(gid, db)


async def revoke_premium(
    gid: int,
    db,
    *,
    revoked_by: Optional[int] = None,
    reason: Optional[str] = None,
) -> PremiumStatus:
    """Hard revoke -- flips status to 'cancelled' and clears expires_at.

    Use ``link_paypal_subscription`` with status='cancelled' if the source
    of truth is a PayPal cancellation; this method is for admin overrides.
    """
    note = f"Revoked by {revoked_by}: {reason}" if reason else f"Revoked by {revoked_by}"
    await db.execute(
        """
        UPDATE guild_premium SET
            status       = 'cancelled',
            cancelled_at = NOW(),
            expires_at   = NOW(),
            notes        = $2,
            updated_at   = NOW()
        WHERE guild_id = $1
        """,
        int(gid), note,
    )
    log.info("premium.revoke guild=%s by=%s reason=%s", gid, revoked_by, reason)
    await _audit(
        db,
        action="premium.revoke",
        actor_id=revoked_by,
        target_guild_id=int(gid),
        severity="danger",
        details=f"Revoked premium for guild {gid}" + (f": {reason}" if reason else ""),
        extra={"reason": reason},
    )
    return await get_status(gid, db)


async def list_premium_guilds(db) -> list[dict[str, Any]]:
    """Owner-facing: every row in guild_premium, newest first."""
    return await db.fetch_all(
        """
        SELECT guild_id, tier, status, source, subscriber_user_id,
               paypal_subscription_id,
               EXTRACT(EPOCH FROM started_at)         AS started_epoch,
               EXTRACT(EPOCH FROM expires_at)         AS exp_epoch,
               EXTRACT(EPOCH FROM current_period_end) AS period_end_epoch,
               granted_by, notes
        FROM guild_premium
        ORDER BY updated_at DESC
        """
    )


async def link_paypal_subscription(
    gid: int,
    db,
    *,
    subscription_id: str,
    plan_id: Optional[str],
    subscriber_user_id: Optional[int],
    status: str,
    current_period_end_epoch: Optional[float],
    expires_at_epoch: Optional[float],
) -> PremiumStatus:
    """PayPal webhook write path. Only ever called from
    api.v2.routers.paypal_webhook after signature verification."""
    valid = {"active", "cancelled", "expired", "suspended"}
    if status not in valid:
        raise ValueError(f"invalid status {status!r}")
    await db.execute(
        """
        INSERT INTO guild_premium
            (guild_id, tier, status, source,
             subscriber_user_id, paypal_subscription_id, paypal_plan_id,
             current_period_end, expires_at, started_at, updated_at)
        VALUES
            ($1, 'premium', $2, 'paypal',
             $3, $4, $5,
             to_timestamp($6), to_timestamp($7), NOW(), NOW())
        ON CONFLICT (guild_id) DO UPDATE SET
            status                 = EXCLUDED.status,
            source                 = 'paypal',
            subscriber_user_id     = COALESCE(EXCLUDED.subscriber_user_id,
                                              guild_premium.subscriber_user_id),
            paypal_subscription_id = EXCLUDED.paypal_subscription_id,
            paypal_plan_id         = COALESCE(EXCLUDED.paypal_plan_id,
                                              guild_premium.paypal_plan_id),
            current_period_end     = COALESCE(EXCLUDED.current_period_end,
                                              guild_premium.current_period_end),
            expires_at             = COALESCE(EXCLUDED.expires_at,
                                              guild_premium.expires_at),
            cancelled_at           = CASE WHEN EXCLUDED.status = 'cancelled'
                                          THEN NOW() ELSE guild_premium.cancelled_at END,
            updated_at             = NOW()
        """,
        int(gid), status,
        int(subscriber_user_id) if subscriber_user_id is not None else None,
        subscription_id, plan_id,
        current_period_end_epoch, expires_at_epoch,
    )
    log.info(
        "premium.paypal guild=%s sub=%s status=%s period_end=%s",
        gid, subscription_id, status, current_period_end_epoch,
    )
    sev = (
        "danger" if status in ("cancelled", "expired", "suspended")
        else "info"
    )
    await _audit(
        db,
        action=f"premium.paypal_{status}",
        actor_id=subscriber_user_id,
        target_guild_id=int(gid),
        severity=sev,
        details=(
            f"PayPal subscription {subscription_id} -> {status} for guild {gid}"
        ),
        extra={
            "subscription_id": subscription_id,
            "plan_id": plan_id,
            "status": status,
            "subscriber_user_id": subscriber_user_id,
            "current_period_end_epoch": current_period_end_epoch,
        },
    )
    return await get_status(gid, db)


async def find_by_paypal_subscription(sub_id: str, db) -> Optional[dict[str, Any]]:
    """Webhook helper: look up the guild a PayPal subscription is attached to."""
    return await db.fetch_one(
        "SELECT guild_id, status FROM guild_premium "
        "WHERE paypal_subscription_id = $1",
        sub_id,
    )


async def expire_overdue(db) -> int:
    """Background sweep -- flip rows past expires_at to 'expired'.
    Returns the number of rows affected. Safe to run from a tasks loop."""
    status = await db.execute(
        """
        UPDATE guild_premium SET
            status     = 'expired',
            updated_at = NOW()
        WHERE status     = 'active'
          AND expires_at IS NOT NULL
          AND expires_at < NOW()
        """
    )
    # asyncpg execute() returns "UPDATE N"
    try:
        n = int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        n = 0
    if n:
        log.info("premium.expire_overdue swept=%s", n)
        # Single audit row per sweep so we don't spam the feed when a
        # batch of subscriptions all roll over at once.
        await _audit(
            db,
            action="premium.expire_sweep",
            actor_id=None,
            target_guild_id=0,  # sweep is multi-guild
            severity="info",
            details=f"Expired {n} overdue subscription(s)",
            extra={"swept": n},
        )
    return n
