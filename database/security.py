"""
database/security.py  -  PostgreSQL repository for security system persistence.

Handles CRUD for security_events, security_enforcements, security_profiles,
and security_audit tables.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from database.base import PgBaseRepo

log = logging.getLogger("discoin.database.security")


class SecurityRepository(PgBaseRepo):
    """Persistent storage for security events, enforcements, and audit logs."""

    # ── Security Events ──────────────────────────────────────────────────────

    async def create_security_event(
        self,
        guild_id: int,
        user_id: int,
        event_type: str,
        severity: str,
        score_delta: float,
        details: dict,
        source: str = "system",
    ) -> int:
        """Insert a security event and return its ID."""
        return await self.fetch_val(
            """INSERT INTO security_events
               (guild_id, user_id, event_type, severity, score_delta, details, source)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id""",
            guild_id, user_id, event_type, severity, score_delta,
            json.dumps(details, default=str), source,
        )

    async def get_security_events(
        self,
        guild_id: int,
        limit: int = 50,
        offset: int = 0,
        user_id: int | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        since: datetime | None = None,
    ) -> list[dict]:
        """Query security events with optional filters."""
        conditions = ["guild_id = $1"]
        params: list[Any] = [guild_id]
        idx = 2

        if user_id is not None:
            conditions.append(f"user_id = ${idx}")
            params.append(user_id)
            idx += 1
        if event_type:
            conditions.append(f"event_type = ${idx}")
            params.append(event_type)
            idx += 1
        if severity:
            conditions.append(f"severity = ${idx}")
            params.append(severity)
            idx += 1
        if since:
            conditions.append(f"created_at >= ${idx}")
            params.append(since)
            idx += 1

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        return await self.fetch_all(
            f"""SELECT id, guild_id, user_id, event_type, severity,
                       score_delta, details, source, created_at
                FROM security_events
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}""",
            *params,
        )

    async def count_security_events(
        self,
        guild_id: int,
        since: datetime | None = None,
    ) -> int:
        """Count security events, optionally since a datetime."""
        if since:
            return await self.fetch_val(
                "SELECT COUNT(*) FROM security_events WHERE guild_id = $1 AND created_at >= $2",
                guild_id, since,
            )
        return await self.fetch_val(
            "SELECT COUNT(*) FROM security_events WHERE guild_id = $1",
            guild_id,
        )

    async def get_events_by_type(self, guild_id: int, since: datetime) -> list[dict]:
        """Aggregate event counts by type."""
        return await self.fetch_all(
            """SELECT event_type, severity, COUNT(*) as count
               FROM security_events
               WHERE guild_id = $1 AND created_at >= $2
               GROUP BY event_type, severity
               ORDER BY count DESC""",
            guild_id, since,
        )

    # ── Enforcements ─────────────────────────────────────────────────────────

    async def create_enforcement(self, record) -> int:
        """Insert an enforcement record and return its ID."""
        return await self.fetch_val(
            """INSERT INTO security_enforcements
               (guild_id, user_id, action_type, scope, reason, enacted_by,
                expires_at, details)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               RETURNING id""",
            record.guild_id,
            record.user_id,
            record.action_type.value if hasattr(record.action_type, 'value') else record.action_type,
            record.scope,
            record.reason,
            record.enacted_by,
            datetime.fromtimestamp(record.expires_at, tz=timezone.utc) if record.expires_at else None,
            json.dumps(record.details, default=str) if record.details else None,
        )

    async def lift_all_enforcements(self, lifted_by: str = "startup") -> int:
        """Lift all active enforcements across every guild.

        Used for the one-time startup lock clear. Returns number of rows updated.
        """
        status = await self.execute(
            """UPDATE security_enforcements
               SET lifted_at = now(), lifted_by = $1
               WHERE lifted_at IS NULL""",
            lifted_by,
        )
        return self._row_count(status)

    async def lift_enforcement(
        self, guild_id: int, user_id: int, lifted_by: str,
    ) -> bool:
        """Lift the most recent active enforcement for a user."""
        status = await self.execute(
            """UPDATE security_enforcements
               SET lifted_at = now(), lifted_by = $3
               WHERE guild_id = $1 AND user_id = $2 AND lifted_at IS NULL""",
            guild_id, user_id, lifted_by,
        )
        return self._row_count(status) > 0

    async def get_active_enforcements(
        self, guild_id: int, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        """Get all active enforcements for a guild."""
        return await self.fetch_all(
            """SELECT id, guild_id, user_id, action_type, scope, reason,
                      enacted_by, expires_at, details, created_at
               FROM security_enforcements
               WHERE guild_id = $1
                 AND lifted_at IS NULL
                 AND (expires_at IS NULL OR expires_at > now())
               ORDER BY created_at DESC
               LIMIT $2 OFFSET $3""",
            guild_id, limit, offset,
        )

    async def count_active_enforcements(self, guild_id: int) -> int:
        return await self.fetch_val(
            """SELECT COUNT(*) FROM security_enforcements
               WHERE guild_id = $1
                 AND lifted_at IS NULL
                 AND (expires_at IS NULL OR expires_at > now())""",
            guild_id,
        )

    async def get_user_enforcements(
        self, guild_id: int, user_id: int, include_expired: bool = False,
    ) -> list[dict]:
        """Get enforcements for a specific user."""
        if include_expired:
            return await self.fetch_all(
                """SELECT * FROM security_enforcements
                   WHERE guild_id = $1 AND user_id = $2
                   ORDER BY created_at DESC LIMIT 20""",
                guild_id, user_id,
            )
        return await self.fetch_all(
            """SELECT * FROM security_enforcements
               WHERE guild_id = $1 AND user_id = $2
                 AND lifted_at IS NULL
                 AND (expires_at IS NULL OR expires_at > now())
               ORDER BY created_at DESC""",
            guild_id, user_id,
        )

    # ── Security Profiles ────────────────────────────────────────────────────

    async def upsert_profile(
        self,
        user_id: int,
        guild_id: int,
        threat_score: float,
        total_flags: int,
        risk_level: str,
        baseline: dict,
        known_ips: list[str],
        last_flagged: datetime | None = None,
    ) -> None:
        """Create or update a security profile."""
        await self.execute(
            """INSERT INTO security_profiles
               (user_id, guild_id, threat_score, total_flags, last_flagged,
                baseline, known_ips, risk_level)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (user_id, guild_id) DO UPDATE SET
                   threat_score = $3,
                   total_flags = $4,
                   last_flagged = COALESCE($5, security_profiles.last_flagged),
                   baseline = $6,
                   known_ips = $7,
                   risk_level = $8""",
            user_id, guild_id, threat_score, total_flags,
            last_flagged,
            json.dumps(baseline, default=str),
            json.dumps(known_ips),
            risk_level,
        )

    async def get_profile(self, guild_id: int, user_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM security_profiles WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )

    async def get_flagged_users(self, guild_id: int) -> list[dict]:
        """Get users with elevated or higher risk level."""
        return await self.fetch_all(
            """SELECT * FROM security_profiles
               WHERE guild_id = $1 AND risk_level != 'normal'
               ORDER BY threat_score DESC""",
            guild_id,
        )

    async def count_flagged_users(self, guild_id: int) -> int:
        return await self.fetch_val(
            "SELECT COUNT(*) FROM security_profiles WHERE guild_id = $1 AND risk_level != 'normal'",
            guild_id,
        )

    # ── Security Audit ───────────────────────────────────────────────────────

    async def create_security_audit(
        self,
        guild_id: int,
        admin_id: int,
        action: str,
        target_user: int | None = None,
        details: dict | None = None,
    ) -> int:
        return await self.fetch_val(
            """INSERT INTO security_audit
               (guild_id, admin_id, action, target_user, details)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING id""",
            guild_id, admin_id, action, target_user,
            json.dumps(details, default=str) if details else None,
        )

    async def get_security_audit(
        self, guild_id: int, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        return await self.fetch_all(
            """SELECT id, guild_id, admin_id, action, target_user, details, created_at
               FROM security_audit
               WHERE guild_id = $1
               ORDER BY created_at DESC
               LIMIT $2 OFFSET $3""",
            guild_id, limit, offset,
        )

    # ── Security Exemptions ──────────────────────────────────────────────────

    async def add_exempt(
        self,
        guild_id: int,
        target_type: str,
        target_id: int,
        granted_by: int,
        notes: str | None = None,
    ) -> int:
        """Add or update a security exemption for a user or role."""
        return await self.fetch_val(
            """INSERT INTO security_exempt_users (guild_id, target_type, target_id, granted_by, notes)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (guild_id, target_type, target_id) DO UPDATE
                   SET granted_by = $4, notes = $5
               RETURNING id""",
            guild_id, target_type, target_id, granted_by, notes,
        )

    async def remove_exempt(
        self,
        guild_id: int,
        target_type: str,
        target_id: int,
    ) -> bool:
        """Remove a security exemption. Returns True if a row was deleted."""
        status = await self.execute(
            "DELETE FROM security_exempt_users WHERE guild_id=$1 AND target_type=$2 AND target_id=$3",
            guild_id, target_type, target_id,
        )
        return self._row_count(status) > 0

    async def get_exemptions(self, guild_id: int) -> list[dict]:
        """List all active exemptions for a guild."""
        return await self.fetch_all(
            "SELECT * FROM security_exempt_users WHERE guild_id=$1 ORDER BY created_at DESC",
            guild_id,
        )

    async def is_exempt(
        self,
        guild_id: int,
        user_id: int,
        role_ids: list[int],
    ) -> bool:
        """Return True if the user or any of their roles is in the exemption list."""
        row = await self.fetch_one(
            """SELECT 1 FROM security_exempt_users
               WHERE guild_id = $1
                 AND (
                     (target_type = 'user' AND target_id = $2)
                     OR (target_type = 'role' AND target_id = ANY($3))
                 )
               LIMIT 1""",
            guild_id, user_id, role_ids,
        )
        return row is not None

    # ── Statistics ───────────────────────────────────────────────────────────

    async def get_stats(self, guild_id: int, since: datetime) -> dict:
        """Get aggregate security statistics."""
        rows = await self.fetch_all(
            """SELECT
                   event_type,
                   severity,
                   COUNT(*) as count,
                   AVG(score_delta) as avg_score
               FROM security_events
               WHERE guild_id = $1 AND created_at >= $2
               GROUP BY event_type, severity
               ORDER BY count DESC""",
            guild_id, since,
        )

        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for r in rows:
            by_type[r["event_type"]] = by_type.get(r["event_type"], 0) + r["count"]
            by_severity[r["severity"]] = by_severity.get(r["severity"], 0) + r["count"]

        total = sum(by_type.values())
        active = await self.count_active_enforcements(guild_id)
        flagged = await self.count_flagged_users(guild_id)

        return {
            "total_events": total,
            "events_by_type": by_type,
            "events_by_severity": by_severity,
            "active_enforcements": active,
            "flagged_users": flagged,
        }
