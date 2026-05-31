"""
core/framework/staff_audit.py -- unified staff audit log helpers and feed embeds.

Every in-game staff surface (``,admin``, ``,drs``, ``,dev``, ``,ai``)
writes a row here whenever a privileged action is taken. The per-surface audit
feed commands (e.g. ``,admin audit``) read back from this table
filtered by ``scope`` so each group only sees its own feed.

Why a single table
------------------
Per-surface tables fragment queries, miss cross-surface context ("who flagged
the user right before admin banned them?"), and turn every feature addition
into a schema migration. One wide audit row with an opaque ``scope`` column
handles every case and lets us render a pretty unified feed with the same
embed builder for all four admin cogs.

Severity
--------
``info``   -- read-only / non-destructive (e.g. DRS profile lookup).
``warn``   -- noticeable effect (e.g. issue warning, purge 20 messages).
``danger`` -- destructive / high-value (e.g. reset server, give 1B USD).

Callers pick severity based on the command they're logging. The audit feed
uses severity to pick an icon + color.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("discoin.staff_audit")


# ── Canonical scope keys ──────────────────────────────────────────────────────

SCOPE_ADMIN = "admin"
SCOPE_MOD = "mod"
SCOPE_DRS = "drs"
SCOPE_DEV = "dev"
SCOPE_AI = "ai"

_VALID_SCOPES: frozenset[str] = frozenset({
    SCOPE_ADMIN, SCOPE_MOD, SCOPE_DRS, SCOPE_DEV, SCOPE_AI,
})

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_DANGER = "danger"

_VALID_SEVERITIES: frozenset[str] = frozenset({
    SEVERITY_INFO, SEVERITY_WARN, SEVERITY_DANGER,
})


@dataclass
class StaffAuditEntry:
    """Parsed staff_audit_log row for display helpers."""
    id: int
    guild_id: int
    scope: str
    actor_id: int
    action: str
    target_id: int | None
    severity: str
    details: str
    metadata: dict
    created_at: Any

    @classmethod
    def from_row(cls, row: dict) -> "StaffAuditEntry":
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return cls(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            scope=str(row["scope"]),
            actor_id=int(row["actor_id"]),
            action=str(row["action"]),
            target_id=int(row["target_id"]) if row.get("target_id") else None,
            severity=str(row.get("severity") or SEVERITY_INFO),
            details=str(row.get("details") or ""),
            metadata=meta,
            created_at=row.get("created_at"),
        )


# ── Writing ───────────────────────────────────────────────────────────────────

async def log_staff_action(
    db: Any,
    *,
    scope: str,
    guild_id: int,
    actor_id: int,
    action: str,
    target_id: int | None = None,
    severity: str = SEVERITY_INFO,
    details: str = "",
    metadata: dict | None = None,
) -> None:
    """Insert a single staff audit row. Never raises -- logs on failure."""
    scope_l = (scope or "").lower()
    if scope_l not in _VALID_SCOPES:
        log.warning("[staff_audit] ignoring write with unknown scope=%r", scope)
        return
    sev_l = (severity or SEVERITY_INFO).lower()
    if sev_l not in _VALID_SEVERITIES:
        sev_l = SEVERITY_INFO
    try:
        await db.execute(
            """
            INSERT INTO staff_audit_log
                (guild_id, scope, actor_id, action, target_id,
                 severity, details, metadata, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,NOW())
            """,
            int(guild_id),
            scope_l,
            int(actor_id),
            str(action)[:100],
            int(target_id) if target_id is not None else None,
            sev_l,
            str(details)[:1000],
            json.dumps(metadata or {}, default=str),
        )
    except Exception:
        log.exception("[staff_audit] insert failed scope=%s action=%s", scope, action)


# ── Reading ───────────────────────────────────────────────────────────────────

async def recent_staff_actions(
    db: Any,
    *,
    guild_id: int,
    scope: str | None = None,
    actor_id: int | None = None,
    target_id: int | None = None,
    limit: int = 50,
) -> list[StaffAuditEntry]:
    """Return a page of recent staff audit rows filtered by the given fields."""
    limit = max(1, min(int(limit), 250))
    clauses = ["guild_id = $1"]
    args: list[Any] = [int(guild_id)]
    if scope:
        args.append(scope.lower())
        clauses.append(f"scope = ${len(args)}")
    if actor_id:
        args.append(int(actor_id))
        clauses.append(f"actor_id = ${len(args)}")
    if target_id:
        args.append(int(target_id))
        clauses.append(f"target_id = ${len(args)}")
    args.append(limit)
    query = (
        "SELECT * FROM staff_audit_log WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY created_at DESC LIMIT ${len(args)}"
    )
    rows = await db.fetch_all(query, *args)
    return [StaffAuditEntry.from_row(r) for r in rows]


# ── Pretty embed feed ─────────────────────────────────────────────────────────

_SEVERITY_ICON: dict[str, str] = {
    SEVERITY_INFO:   "🟢",
    SEVERITY_WARN:   "🟡",
    SEVERITY_DANGER: "🔴",
}

_SCOPE_ICON: dict[str, str] = {
    SCOPE_ADMIN: "🛠",
    SCOPE_MOD:   "🛡",
    SCOPE_DRS:   "🕵",
    SCOPE_DEV:   "🔧",
    SCOPE_AI:    "🤖",
}

_SCOPE_TITLE: dict[str, str] = {
    SCOPE_ADMIN: "Admin Audit Feed",
    SCOPE_MOD:   "Moderation Audit Feed",
    SCOPE_DRS:   "DRS Audit Feed",
    SCOPE_DEV:   "Developer Audit Feed",
    SCOPE_AI:    "AI Audit Feed",
}


def build_audit_embeds(
    entries: list[StaffAuditEntry],
    *,
    scope: str | None,
    guild,
    per_page: int = 8,
) -> list:
    """Render a list of ``StaffAuditEntry`` rows into paginated card embeds.

    Uses the framework ``card`` builder and the shared color constants so
    the output matches every other admin embed in the bot. ``guild`` is
    the ``discord.Guild`` the rows belong to; it's used to resolve actor
    and target display names.
    """
    # Local imports so the helper file stays framework-side with no cogs dep.
    from core.framework.embed import card
    from core.framework.ui import C_AMBER, C_ERROR, C_INFO, C_NAVY, fmt_ts

    scope_key = (scope or "").lower() or None
    title = (
        _SCOPE_TITLE.get(scope_key or "", "Staff Audit Feed")
        if scope_key else "Staff Audit Feed"
    )
    icon = _SCOPE_ICON.get(scope_key or "", "📜")

    if not entries:
        empty = (
            card(f"{icon} {title}", color=C_NAVY)
            .description("No audit entries recorded yet.")
            .footer("Use the per-surface audit command to log actions here.")
            .build()
        )
        return [empty]

    pages: list = []
    total = len(entries)
    chunks = [entries[i:i + per_page] for i in range(0, total, per_page)]
    total_pages = len(chunks)

    for idx, chunk in enumerate(chunks, 1):
        worst = max(
            (_severity_rank(e.severity) for e in chunk),
            default=0,
        )
        color = (
            C_ERROR if worst >= 2
            else (C_AMBER if worst >= 1 else C_INFO)
        )
        b = card(f"{icon} {title}", color=color)
        for entry in chunk:
            sev_icon = _SEVERITY_ICON.get(entry.severity, "ℹ️")
            actor_name = _resolve_name(guild, entry.actor_id) or f"<{entry.actor_id}>"
            target_str = ""
            if entry.target_id:
                target_name = _resolve_name(guild, entry.target_id) or str(entry.target_id)
                target_str = f" → **{target_name}**"
            ts_str = fmt_ts(entry.created_at)
            header = (
                f"{sev_icon} `{entry.action}` · **{actor_name}**{target_str} · `{ts_str}`"
            )
            body_parts: list[str] = []
            if entry.details:
                body_parts.append(entry.details[:500])
            if entry.metadata:
                meta_preview = ", ".join(
                    f"{k}=`{str(v)[:40]}`" for k, v in list(entry.metadata.items())[:3]
                )
                if meta_preview:
                    body_parts.append(f"*{meta_preview}*")
            body = "\n".join(body_parts) or "_(no details)_"
            b.field(header[:256], body[:1024], False)
        b.footer(
            f"Page {idx}/{total_pages} · {total} entr"
            + ("y" if total == 1 else "ies")
        )
        pages.append(b.build())
    return pages


def _severity_rank(sev: str) -> int:
    return {
        SEVERITY_INFO: 0,
        SEVERITY_WARN: 1,
        SEVERITY_DANGER: 2,
    }.get((sev or "").lower(), 0)


def _resolve_name(guild, user_id: int) -> str | None:
    if guild is None:
        return None
    member = guild.get_member(int(user_id))
    if member is not None:
        return member.display_name
    return None
