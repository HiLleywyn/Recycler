"""
Tests for services/entitlements.py -- the per-guild premium gate.

Covers:
  - Host-guild override never hits the DB
  - is_premium / get_status for missing rows
  - Active row with future expiry -> premium
  - Active row past expiry -> not premium
  - Cancelled row -> not premium (no period-end grace)
  - PayPal subscription_id lookup
  - Decorator gate fires + reply_premium_required called once
  - Decorator gate passes for premium guilds (no reply)
  - PremiumCog.cog_check raises PremiumGateFailure for non-premium
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from core.config import Config
from services import entitlements
from core.framework.premium import (
    PremiumCog,
    PremiumGateFailure,
    premium_required,
)


HOST_GID = 999_999_999
PAID_GID = 100_000_001
FREE_GID = 100_000_002


@pytest.fixture(autouse=True)
def _force_host_guild(monkeypatch):
    """Force Config.HOST_GUILD_ID for every test in this file. We can't
    rely on env-var-at-import-time because pytest may have imported config
    earlier (cached value) -- monkeypatch the class attribute instead."""
    monkeypatch.setattr(Config, "HOST_GUILD_ID", HOST_GID)


class _RowDB:
    """Tiny in-memory stand-in for the bot's Database class.

    Stores guild_premium rows as plain dicts and answers the queries
    entitlements.py actually issues. Captures executed statements so
    tests can assert on grant/revoke side effects."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.host_db_hits = 0  # bumped if host-guild path ever hits DB
        self._executed: list[tuple[str, tuple]] = []

    async def fetch_one(self, query: str, *args):
        # paypal lookup goes first -- its arg is a sub_id string, never an int.
        if "WHERE paypal_subscription_id" in query:
            sub = args[0]
            for gid, row in self.rows.items():
                if row.get("paypal_subscription_id") == sub:
                    return {"guild_id": gid, "status": row.get("status")}
            return None
        if "FROM guild_premium" in query and "$1" in query:
            gid = int(args[0])
            if gid == HOST_GID:
                # Host should never reach here; entitlements.is_premium
                # short-circuits before any DB call.
                self.host_db_hits += 1
            row = self.rows.get(gid)
            if not row:
                return None
            # Mirror the column projection the real query returns.
            if "exp_epoch" in query and "started_epoch" not in query:
                return {
                    "status": row.get("status"),
                    "exp_epoch": row.get("expires_at"),
                }
            return {
                "tier": row.get("tier", "premium"),
                "status": row.get("status"),
                "source": row.get("source", "admin"),
                "subscriber_user_id": row.get("subscriber_user_id"),
                "paypal_subscription_id": row.get("paypal_subscription_id"),
                "notes": row.get("notes"),
                "started_epoch": row.get("started_at"),
                "exp_epoch": row.get("expires_at"),
                "period_end_epoch": row.get("current_period_end"),
            }
        return None

    async def fetch_all(self, query: str, *args):
        rows = []
        for gid, row in self.rows.items():
            rows.append({
                "guild_id": gid,
                **row,
                "started_epoch": row.get("started_at"),
                "exp_epoch": row.get("expires_at"),
                "period_end_epoch": row.get("current_period_end"),
            })
        return rows

    async def execute(self, query: str, *args):
        self._executed.append((query, args))
        # Crude statement router for the few INSERT/UPDATE shapes used.
        if "INSERT INTO guild_premium" in query and "ON CONFLICT" in query:
            # grant_premium signature: ($1 gid, $2 tier, $3 source, $4 granted_by,
            # $5 notes, $6 seconds-or-null)
            if len(args) >= 6 and "EXCLUDED.tier" in query:
                gid = int(args[0])
                seconds = args[5]
                exp = (time.time() + float(seconds)) if seconds is not None else None
                self.rows[gid] = {
                    "tier": args[1],
                    "status": "active",
                    "source": args[2],
                    "granted_by": args[3],
                    "notes": args[4],
                    "started_at": time.time(),
                    "expires_at": exp,
                    "current_period_end": None,
                    "paypal_subscription_id": None,
                }
                return "INSERT 0 1"
            # link_paypal_subscription signature: ($1 gid, $2 status,
            # $3 subscriber, $4 sub_id, $5 plan_id, $6 period_end_epoch,
            # $7 expires_at_epoch)
            if len(args) >= 7:
                gid = int(args[0])
                self.rows[gid] = {
                    "tier": "premium",
                    "status": args[1],
                    "source": "paypal",
                    "subscriber_user_id": args[2],
                    "paypal_subscription_id": args[3],
                    "paypal_plan_id": args[4],
                    "current_period_end": args[5],
                    "expires_at": args[6],
                    "started_at": time.time(),
                }
                return "INSERT 0 1"
        if "UPDATE guild_premium SET" in query and "cancelled" in query.lower():
            gid = int(args[0])
            if gid in self.rows:
                self.rows[gid]["status"] = "cancelled"
                self.rows[gid]["expires_at"] = time.time()
                self.rows[gid]["notes"] = args[1] if len(args) > 1 else None
            return "UPDATE 1"
        if "UPDATE guild_premium SET" in query and "expired" in query.lower():
            n = 0
            now = time.time()
            for row in self.rows.values():
                if (row.get("status") == "active"
                        and row.get("expires_at") is not None
                        and row["expires_at"] < now):
                    row["status"] = "expired"
                    n += 1
            return f"UPDATE {n}"
        return "OK"


@pytest.fixture
def db() -> _RowDB:
    return _RowDB()


# ── host-guild override ───────────────────────────────────────────────


async def test_host_guild_is_premium_without_db(db: _RowDB) -> None:
    assert Config.HOST_GUILD_ID == HOST_GID
    assert await entitlements.is_premium(HOST_GID, db) is True
    assert db.host_db_hits == 0, "host-guild check leaked to the DB"


async def test_host_guild_status(db: _RowDB) -> None:
    s = await entitlements.get_status(HOST_GID, db)
    assert s.is_premium is True
    assert s.source == "host"
    assert s.expires_at is None


async def test_is_host_guild_helper() -> None:
    assert entitlements.is_host_guild(HOST_GID) is True
    assert entitlements.is_host_guild(FREE_GID) is False


# ── plain reads ───────────────────────────────────────────────────────


async def test_missing_row_is_not_premium(db: _RowDB) -> None:
    assert await entitlements.is_premium(FREE_GID, db) is False
    s = await entitlements.get_status(FREE_GID, db)
    assert s.is_premium is False
    assert s.source == "none"


async def test_active_row_with_future_expiry_is_premium(db: _RowDB) -> None:
    db.rows[PAID_GID] = {
        "tier": "premium", "status": "active", "source": "admin",
        "expires_at": time.time() + 3600,
    }
    assert await entitlements.is_premium(PAID_GID, db) is True


async def test_active_row_past_expiry_is_not_premium(db: _RowDB) -> None:
    db.rows[PAID_GID] = {
        "tier": "premium", "status": "active", "source": "admin",
        "expires_at": time.time() - 60,
    }
    assert await entitlements.is_premium(PAID_GID, db) is False


async def test_cancelled_row_is_not_premium(db: _RowDB) -> None:
    db.rows[PAID_GID] = {
        "tier": "premium", "status": "cancelled", "source": "paypal",
        "expires_at": time.time() + 3600,
    }
    assert await entitlements.is_premium(PAID_GID, db) is False


async def test_active_row_with_no_expiry_is_premium(db: _RowDB) -> None:
    db.rows[PAID_GID] = {
        "tier": "premium", "status": "active", "source": "admin",
        "expires_at": None,
    }
    assert await entitlements.is_premium(PAID_GID, db) is True


# ── grant / revoke ────────────────────────────────────────────────────


async def test_grant_premium_with_days_sets_expiry(db: _RowDB) -> None:
    s = await entitlements.grant_premium(PAID_GID, db, days=30, granted_by=42)
    assert s.is_premium is True
    assert s.source == "admin"
    # Expires roughly 30 days out.
    assert s.expires_at is not None
    delta = s.expires_at - time.time()
    assert 30 * 86400 - 60 < delta < 30 * 86400 + 60


async def test_grant_premium_without_days_is_indefinite(db: _RowDB) -> None:
    s = await entitlements.grant_premium(PAID_GID, db, days=None, granted_by=42)
    assert s.is_premium is True
    assert s.expires_at is None


async def test_grant_rejects_zero_or_negative_days(db: _RowDB) -> None:
    with pytest.raises(ValueError):
        await entitlements.grant_premium(PAID_GID, db, days=0)
    with pytest.raises(ValueError):
        await entitlements.grant_premium(PAID_GID, db, days=-7)


async def test_revoke_premium_flips_to_cancelled(db: _RowDB) -> None:
    await entitlements.grant_premium(PAID_GID, db, days=30)
    s = await entitlements.revoke_premium(PAID_GID, db, revoked_by=1, reason="abuse")
    assert s.is_premium is False
    assert s.status == "cancelled"


# ── PayPal lookup ─────────────────────────────────────────────────────


async def test_find_by_paypal_subscription(db: _RowDB) -> None:
    db.rows[PAID_GID] = {
        "status": "active", "paypal_subscription_id": "I-ABC123",
    }
    found = await entitlements.find_by_paypal_subscription("I-ABC123", db)
    assert found is not None
    assert found["guild_id"] == PAID_GID

    missing = await entitlements.find_by_paypal_subscription("nope", db)
    assert missing is None


async def test_link_paypal_subscription(db: _RowDB) -> None:
    end = time.time() + 30 * 86400
    s = await entitlements.link_paypal_subscription(
        PAID_GID, db,
        subscription_id="I-ABC123",
        plan_id="P-PLAN",
        subscriber_user_id=12345,
        status="active",
        current_period_end_epoch=end,
        expires_at_epoch=end,
    )
    assert s.is_premium is True
    assert s.source == "paypal"
    assert s.paypal_subscription_id == "I-ABC123"


async def test_link_paypal_rejects_invalid_status(db: _RowDB) -> None:
    with pytest.raises(ValueError):
        await entitlements.link_paypal_subscription(
            PAID_GID, db,
            subscription_id="I-X", plan_id=None, subscriber_user_id=None,
            status="bogus",
            current_period_end_epoch=None, expires_at_epoch=None,
        )


# ── decorator behaviour ───────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, gid: int, db: _RowDB) -> None:
        self.guild_id = gid
        self.db = db
        self.replied: list[str] = []

    async def reply_premium_required(self, feature: str) -> None:
        self.replied.append(feature)


async def test_decorator_blocks_non_premium(db: _RowDB) -> None:
    ran = []

    @premium_required("fishing")
    async def cmd(ctx):
        ran.append(True)

    ctx = _FakeCtx(FREE_GID, db)
    result = await cmd(ctx)
    assert result is None
    assert ran == []
    assert ctx.replied == ["fishing"]


async def test_decorator_passes_for_premium(db: _RowDB) -> None:
    db.rows[PAID_GID] = {
        "tier": "premium", "status": "active", "source": "admin",
        "expires_at": time.time() + 3600,
    }
    ran = []

    @premium_required("fishing")
    async def cmd(ctx):
        ran.append(True)
        return "OK"

    ctx = _FakeCtx(PAID_GID, db)
    result = await cmd(ctx)
    assert result == "OK"
    assert ran == [True]
    assert ctx.replied == []


async def test_decorator_supports_methods(db: _RowDB) -> None:
    class Cog:
        @premium_required("ai")
        async def cmd(self, ctx):
            return "ran"

    ctx = _FakeCtx(FREE_GID, db)
    result = await Cog().cmd(ctx)
    assert result is None
    assert ctx.replied == ["ai"]


async def test_decorator_fails_open_on_db_error(db: _RowDB) -> None:
    """If the DB blows up we never want to lock everyone out -- the gate
    fails open so paid features keep working through transient errors."""

    class BrokenDB:
        async def fetch_one(self, *a, **kw):
            raise RuntimeError("db down")

    ran = []

    @premium_required("ai")
    async def cmd(ctx):
        ran.append(True)
        return "OK"

    ctx = _FakeCtx(FREE_GID, BrokenDB())
    result = await cmd(ctx)
    assert result == "OK"


# ── PremiumCog ────────────────────────────────────────────────────────


async def test_premium_cog_check_raises_for_non_premium(db: _RowDB) -> None:
    class MyCog(PremiumCog):
        __premium_feature__ = "ai"

    cog = MyCog()
    ctx = _FakeCtx(FREE_GID, db)
    with pytest.raises(PremiumGateFailure) as excinfo:
        await cog.cog_check(ctx)
    assert excinfo.value.feature_key == "ai"


async def test_premium_cog_check_passes_for_premium(db: _RowDB) -> None:
    db.rows[PAID_GID] = {
        "tier": "premium", "status": "active", "source": "admin",
        "expires_at": None,
    }

    class MyCog(PremiumCog):
        __premium_feature__ = "ai"

    cog = MyCog()
    ctx = _FakeCtx(PAID_GID, db)
    assert await cog.cog_check(ctx) is True


# ── expire_overdue ────────────────────────────────────────────────────


async def test_expire_overdue_flips_overdue_rows(db: _RowDB) -> None:
    db.rows[PAID_GID] = {
        "tier": "premium", "status": "active", "source": "admin",
        "expires_at": time.time() - 10,
    }
    db.rows[PAID_GID + 1] = {
        "tier": "premium", "status": "active", "source": "admin",
        "expires_at": time.time() + 3600,
    }
    n = await entitlements.expire_overdue(db)
    assert n == 1
    assert db.rows[PAID_GID]["status"] == "expired"
    assert db.rows[PAID_GID + 1]["status"] == "active"


# ── audit emission ────────────────────────────────────────────────────


async def test_grant_emits_staff_audit(monkeypatch, db: _RowDB) -> None:
    """grant_premium should emit a premium.grant audit row to the host
    guild's audit feed via core.framework.staff_audit.log_staff_action."""
    captured: list[dict] = []

    async def fake_log(_db, *, scope, guild_id, actor_id, action,
                       target_id=None, severity, details, metadata):
        captured.append({
            "scope": scope, "guild_id": guild_id, "actor_id": actor_id,
            "action": action, "target_id": target_id, "severity": severity,
            "details": details, "metadata": metadata,
        })

    monkeypatch.setattr("core.framework.staff_audit.log_staff_action", fake_log)
    await entitlements.grant_premium(PAID_GID, db, days=7, granted_by=42)
    assert len(captured) == 1
    row = captured[0]
    assert row["scope"] == "admin"
    assert row["guild_id"] == HOST_GID
    assert row["target_id"] == PAID_GID
    assert row["action"] == "premium.grant"
    assert row["severity"] == "warn"
    assert row["metadata"]["days"] == 7


async def test_gift_source_emits_premium_gift_action(monkeypatch, db: _RowDB) -> None:
    captured: list[str] = []

    async def fake_log(_db, *, action, **_kw):
        captured.append(action)

    monkeypatch.setattr("core.framework.staff_audit.log_staff_action", fake_log)
    await entitlements.grant_premium(
        PAID_GID, db, days=30, granted_by=42, source="gift",
    )
    assert captured == ["premium.gift"]


async def test_revoke_emits_danger_severity(monkeypatch, db: _RowDB) -> None:
    captured: list[dict] = []

    async def fake_log(_db, *, severity, action, **_kw):
        captured.append({"severity": severity, "action": action})

    monkeypatch.setattr("core.framework.staff_audit.log_staff_action", fake_log)
    await entitlements.grant_premium(PAID_GID, db, days=30)
    await entitlements.revoke_premium(PAID_GID, db, revoked_by=1, reason="abuse")
    assert {"severity": "danger", "action": "premium.revoke"} in captured


async def test_paypal_link_audits_with_status_action(monkeypatch, db: _RowDB) -> None:
    captured: list[str] = []

    async def fake_log(_db, *, action, **_kw):
        captured.append(action)

    monkeypatch.setattr("core.framework.staff_audit.log_staff_action", fake_log)
    await entitlements.link_paypal_subscription(
        PAID_GID, db,
        subscription_id="I-X", plan_id="P-X", subscriber_user_id=99,
        status="active",
        current_period_end_epoch=time.time() + 86400,
        expires_at_epoch=time.time() + 86400,
    )
    await entitlements.link_paypal_subscription(
        PAID_GID, db,
        subscription_id="I-X", plan_id="P-X", subscriber_user_id=99,
        status="cancelled",
        current_period_end_epoch=None, expires_at_epoch=time.time() + 1,
    )
    assert "premium.paypal_active" in captured
    assert "premium.paypal_cancelled" in captured


async def test_audit_skipped_when_no_host_guild(monkeypatch, db: _RowDB) -> None:
    """Audit is best-effort -- if HOST_GUILD_ID is unset (operator hasn't
    configured the host yet) we just skip the audit instead of failing."""
    monkeypatch.setattr(Config, "HOST_GUILD_ID", 0)
    captured: list = []

    async def fake_log(*a, **kw):
        captured.append((a, kw))

    monkeypatch.setattr("core.framework.staff_audit.log_staff_action", fake_log)
    await entitlements.grant_premium(PAID_GID, db, days=7)
    assert captured == []
