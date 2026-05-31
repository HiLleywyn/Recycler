"""Reports repository  -  persistent ticket/report system (PostgreSQL)."""
from __future__ import annotations

import datetime
from datetime import timezone

from .base import PgBaseRepo

VALID_CATEGORIES = {"bugs", "suggestions", "users", "other"}
VALID_STATUSES = {"open", "accepted", "rejected", "in_progress", "resolved", "closed"}


class PgReportsRepo(PgBaseRepo):

    async def create_report(self, guild_id: int, user_id: int, category: str, message: str) -> dict:
        return await self.fetch_one(
            "INSERT INTO reports (guild_id, user_id, category, message, status)"
            " VALUES ($1, $2, $3, $4, 'open')"
            " RETURNING *",
            guild_id, user_id, category, message,
        )

    async def create_bounty_report(
        self, guild_id: int, user_id: int, category: str, message: str, bounty_id: int
    ) -> dict:
        """Create a report explicitly linked to a specific bounty."""
        return await self.fetch_one(
            "INSERT INTO reports (guild_id, user_id, category, message, status, bounty_id)"
            " VALUES ($1, $2, $3, $4, 'open', $5)"
            " RETURNING *",
            guild_id, user_id, category, message, bounty_id,
        )

    async def get_report(self, report_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM reports WHERE id = $1", report_id,
        )

    async def get_user_latest_report(self, user_id: int, guild_id: int) -> dict | None:
        """Get the most recent report by a user in a guild."""
        return await self.fetch_one(
            "SELECT * FROM reports WHERE user_id = $1 AND guild_id = $2"
            " ORDER BY created_at DESC LIMIT 1",
            user_id, guild_id,
        )

    async def get_user_open_reports(self, user_id: int, guild_id: int) -> list[dict]:
        """Get all open reports by a user in a guild."""
        return await self.fetch_all(
            "SELECT * FROM reports WHERE user_id = $1 AND guild_id = $2 AND status = 'open'"
            " ORDER BY created_at DESC",
            user_id, guild_id,
        )

    async def update_report_message(self, report_id: int, message: str) -> dict | None:
        """Update the message of an existing report."""
        await self.execute(
            "UPDATE reports SET message = $1, updated_at = now() WHERE id = $2",
            message, report_id,
        )
        return await self.get_report(report_id)

    async def update_status(
        self, report_id: int, status: str, admin_note: str | None = None
    ) -> dict | None:
        if admin_note is not None:
            await self.execute(
                "UPDATE reports SET status = $1, admin_note = $2, updated_at = now() WHERE id = $3",
                status, admin_note, report_id,
            )
        else:
            await self.execute(
                "UPDATE reports SET status = $1, updated_at = now() WHERE id = $2",
                status, report_id,
            )
        return await self.get_report(report_id)

    async def set_tags(self, report_id: int, tags: list[str]) -> dict | None:
        """Set admin tags on a report. Replaces any existing tags."""
        tag_str = ",".join(t.strip() for t in tags if t.strip())
        await self.execute(
            "UPDATE reports SET tags = $1, updated_at = now() WHERE id = $2",
            tag_str, report_id,
        )
        return await self.get_report(report_id)

    async def set_dm_message_id(self, report_id: int, message_id: int) -> None:
        await self.execute(
            "UPDATE reports SET dm_message_id = $1 WHERE id = $2",
            message_id, report_id,
        )

    async def get_open_reports(self, guild_id: int | None = None) -> list[dict]:
        if guild_id is not None:
            return await self.fetch_all(
                "SELECT * FROM reports WHERE guild_id = $1 AND status NOT IN ('closed', 'rejected')"
                " ORDER BY id ASC",
                guild_id,
            )
        return await self.fetch_all(
            "SELECT * FROM reports WHERE status NOT IN ('closed', 'rejected')"
            " ORDER BY id ASC",
        )

    async def get_reports_filtered(
        self,
        guild_id: int | None = None,
        category: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Get reports with optional guild / category / status filters.

        ``guild_id`` defaults to None for legacy callers that intentionally
        wanted cross-guild aggregation, but every admin-facing call site
        should pass it -- without it the row set spans every guild the bot
        has ever served.
        """
        clauses: list[str] = []
        params: list = []
        idx = 1
        if guild_id is not None:
            clauses.append(f"guild_id = ${idx}")
            params.append(int(guild_id))
            idx += 1
        if category:
            clauses.append(f"category = ${idx}")
            params.append(category)
            idx += 1
        if status:
            clauses.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return await self.fetch_all(
            f"SELECT * FROM reports{where} ORDER BY id DESC", *params,
        )

    async def get_reports_by_user(self, user_id: int) -> list[dict]:
        """Get all reports submitted by a specific user."""
        return await self.fetch_all(
            "SELECT * FROM reports WHERE user_id = $1 ORDER BY id DESC",
            user_id,
        )

    async def get_public_reports(
        self, guild_id: int, category: str | None = None, limit: int = 50,
    ) -> list[dict]:
        """Fetch reports in public categories (bugs, suggestions) only."""
        public = ("bugs", "suggestions")
        if category and category in public:
            return await self.fetch_all(
                "SELECT * FROM reports WHERE guild_id = $1 AND category = $2 ORDER BY id DESC LIMIT $3",
                guild_id, category, limit,
            )
        return await self.fetch_all(
            "SELECT * FROM reports WHERE guild_id = $1 AND category IN ($2, $3) ORDER BY id DESC LIMIT $4",
            guild_id, *public, limit,
        )

    async def delete_report(self, report_id: int) -> bool:
        """Delete a single report by ID. Returns True if deleted."""
        status = await self.execute("DELETE FROM reports WHERE id = $1", report_id)
        return self._row_count(status) > 0

    async def delete_reports_by_guild(self, guild_id: int) -> int:
        """Delete all reports for a guild. Returns count deleted."""
        status = await self.execute("DELETE FROM reports WHERE guild_id = $1", guild_id)
        return self._row_count(status)

    async def delete_reports_filtered(
        self, guild_id: int, category: str | None = None, status: str | None = None,
    ) -> int:
        """Delete reports matching filter. Returns count deleted."""
        clauses = [f"guild_id = $1"]
        params: list = [guild_id]
        idx = 2
        if category:
            clauses.append(f"category = ${idx}")
            params.append(category)
            idx += 1
        if status:
            clauses.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        where = " AND ".join(clauses)
        result = await self.execute(f"DELETE FROM reports WHERE {where}", *params)
        return self._row_count(result)

    async def get_report_summary(self, guild_id: int) -> dict:
        """Return a summary of open (non-terminal) reports grouped by category and status.

        Returns: {
            "total": int,
            "by_category": {"bugs": int, ...},
            "by_status": {"open": int, ...},
            "by_category_status": {("bugs", "open"): int, ...},
        }
        """
        rows = await self.fetch_all(
            "SELECT category, status, COUNT(*) as cnt FROM reports "
            "WHERE guild_id = $1 AND status NOT IN ('closed', 'rejected') "
            "GROUP BY category, status",
            guild_id,
        )
        total = 0
        by_cat: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_cat_status: dict[tuple[str, str], int] = {}
        for r in rows:
            cnt = r["cnt"]
            total += cnt
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + cnt
            by_status[r["status"]] = by_status.get(r["status"], 0) + cnt
            by_cat_status[(r["category"], r["status"])] = cnt
        return {
            "total": total,
            "by_category": by_cat,
            "by_status": by_status,
            "by_category_status": by_cat_status,
        }

    # ── Rewards ────────────────────────────────────────────────────────────

    async def set_reward(self, report_id: int, amount: float) -> dict | None:
        """Set the reward amount on a report."""
        await self.execute(
            "UPDATE reports SET reward_amount = $1, updated_at = now() WHERE id = $2",
            amount, report_id,
        )
        return await self.get_report(report_id)

    # ── Bounties ──────────────────────────────────────────────────────────

    async def create_bounty(
        self, guild_id: int, created_by: int, title: str,
        description: str, category: str, reward_amount: float,
        max_claims: int = 0,
    ) -> dict:
        return await self.fetch_one(
            "INSERT INTO bounties (guild_id, created_by, title, description, category, reward_amount, max_claims)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
            guild_id, created_by, title, description, category, reward_amount, max_claims,
        )

    async def get_active_bounties(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM bounties WHERE guild_id = $1 AND is_active = TRUE ORDER BY created_at DESC",
            guild_id,
        )

    async def get_bounty(self, bounty_id: int) -> dict | None:
        return await self.fetch_one("SELECT * FROM bounties WHERE id = $1", bounty_id)

    async def edit_bounty(self, bounty_id: int, *, title: str | None = None, description: str | None = None, reward_amount: float | None = None) -> dict | None:
        """Edit bounty fields. Only updates fields that are not None."""
        updates: list[str] = []
        params: list = []
        idx = 1
        if title is not None:
            updates.append(f"title = ${idx}"); params.append(title); idx += 1
        if description is not None:
            updates.append(f"description = ${idx}"); params.append(description); idx += 1
        if reward_amount is not None:
            updates.append(f"reward_amount = ${idx}"); params.append(reward_amount); idx += 1
        if not updates:
            return await self.get_bounty(bounty_id)
        params.append(bounty_id)
        await self.execute(
            f"UPDATE bounties SET {', '.join(updates)} WHERE id = ${idx}",
            *params,
        )
        return await self.get_bounty(bounty_id)

    async def close_bounty(self, bounty_id: int) -> dict | None:
        await self.execute(
            "UPDATE bounties SET is_active = FALSE, closed_at = now() WHERE id = $1",
            bounty_id,
        )
        return await self.get_bounty(bounty_id)

    async def get_bounty_reports(self, bounty_id: int) -> list[dict]:
        """Get all reports explicitly linked to a bounty (via bounty_id column)."""
        return await self.fetch_all(
            "SELECT * FROM reports WHERE bounty_id = $1 ORDER BY created_at DESC",
            bounty_id,
        )

    async def get_qualifying_bounty_reports(self, bounty_id: int) -> list[dict]:
        """Get bounty reports that qualify for reward payout: accepted, in_progress, or closed."""
        return await self.fetch_all(
            "SELECT * FROM reports WHERE bounty_id = $1 AND status IN ('accepted', 'in_progress', 'closed')"
            " ORDER BY created_at ASC",
            bounty_id,
        )

    async def close_linked_bounty_reports(self, bounty_id: int) -> int:
        """Close all open/accepted/in_progress reports linked to a bounty. Returns count closed."""
        result = await self.execute(
            "UPDATE reports SET status = 'closed', updated_at = now()"
            " WHERE bounty_id = $1 AND status NOT IN ('closed', 'rejected', 'resolved')",
            bounty_id,
        )
        return self._row_count(result)

    async def increment_bounty_claims(self, bounty_id: int) -> dict | None:
        """Increment the claims counter. Auto-closes if max_claims reached."""
        await self.execute(
            "UPDATE bounties SET claims = claims + 1 WHERE id = $1", bounty_id,
        )
        bounty = await self.get_bounty(bounty_id)
        if bounty and bounty["max_claims"] > 0 and bounty["claims"] >= bounty["max_claims"]:
            await self.execute(
                "UPDATE bounties SET is_active = FALSE, closed_at = now() WHERE id = $1",
                bounty_id,
            )
            return await self.get_bounty(bounty_id)
        return bounty

    async def get_matching_bounty(self, guild_id: int, category: str) -> dict | None:
        """Get the highest-value active bounty matching a report's category."""
        return await self.fetch_one(
            "SELECT * FROM bounties WHERE guild_id = $1 AND category = $2 AND is_active = TRUE"
            " ORDER BY reward_amount DESC LIMIT 1",
            guild_id, category,
        )

    async def count_reports_filtered(
        self, guild_id: int, category: str | None = None, status: str | None = None,
    ) -> int:
        """Count reports matching filter (for confirmation previews)."""
        clauses = [f"guild_id = $1"]
        params: list = [guild_id]
        idx = 2
        if category:
            clauses.append(f"category = ${idx}")
            params.append(category)
            idx += 1
        if status:
            clauses.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        where = " AND ".join(clauses)
        return await self.fetch_val(
            f"SELECT COUNT(*) FROM reports WHERE {where}", *params,
        )

    # ── DM Cleanup ────────────────────────────────────────────────────────

    async def get_stale_closed_report_dms(self, older_than_days: int) -> list[dict]:
        """Return closed/rejected reports that still have dm_message_id and were closed > N days ago."""
        cutoff = datetime.datetime.now(timezone.utc) - datetime.timedelta(days=older_than_days)
        return await self.fetch_all(
            "SELECT id, dm_message_id FROM reports"
            " WHERE status IN ('closed', 'rejected') AND dm_message_id IS NOT NULL"
            " AND updated_at < $1",
            cutoff,
        )

    async def clear_dm_message_id(self, report_id: int) -> None:
        """Clear the stored admin DM message ID (after the message has been deleted)."""
        await self.execute(
            "UPDATE reports SET dm_message_id = NULL WHERE id = $1",
            report_id,
        )

    # ── Leaderboards ─────────────────────────────────────────────────────

    async def get_report_leaderboard(self, guild_id: int, limit: int = 15) -> list[dict]:
        """Top reporters by accepted/resolved report count."""
        return await self.fetch_all(
            """SELECT user_id,
                      COUNT(*) AS total_reports,
                      SUM(CASE WHEN status IN ('accepted', 'in_progress', 'resolved', 'closed') THEN 1 ELSE 0 END) AS accepted,
                      COALESCE(SUM(reward_amount), 0) AS total_rewarded
               FROM reports
               WHERE guild_id = $1
               GROUP BY user_id
               HAVING COUNT(*) > 0
               ORDER BY accepted DESC, total_reports DESC
               LIMIT $2""",
            guild_id, limit,
        )

    async def get_bugbounty_leaderboard(self, guild_id: int, limit: int = 15) -> list[dict]:
        """Top bug bounty hunters by bounty reward earnings."""
        return await self.fetch_all(
            """SELECT r.user_id,
                      COUNT(*) AS bounty_reports,
                      COALESCE(SUM(r.reward_amount), 0) AS total_earned
               FROM reports r
               WHERE r.guild_id = $1 AND r.bounty_id IS NOT NULL
                 AND r.status IN ('accepted', 'in_progress', 'resolved', 'closed')
               GROUP BY r.user_id
               HAVING COUNT(*) > 0
               ORDER BY total_earned DESC, bounty_reports DESC
               LIMIT $2""",
            guild_id, limit,
        )

    async def get_all_reports_since(self, guild_id: int, since: "datetime.datetime") -> list[dict]:
        """Get all reports created since a timestamp (for daily summary with full text)."""
        return await self.fetch_all(
            "SELECT * FROM reports WHERE guild_id = $1 AND created_at >= $2 ORDER BY created_at DESC",
            guild_id, since,
        )

    # ── Auto-fix queue ────────────────────────────────────────────────────────
    #
    # Per-report row tracking the ,admin reports autofix lifecycle. Read +
    # write helpers below; the worker loop and the Open PR / Discard
    # buttons in cogs/report.py call these to keep the row in sync with
    # what's actually happening.

    async def queue_autofix(
        self, report_id: int, guild_id: int, requested_by: int,
    ) -> dict | None:
        """Insert a fresh queued row, or RESET an existing terminal row
        (failed / unfixable / discarded / pr_open) back to queued.

        ``proposed`` and ``generating`` rows are left alone -- the
        worker is mid-flight on those and we don't want to step on it.
        Returns the resulting row, or None if the row already exists in
        a non-terminal state (caller should treat that as "already
        queued, no-op").
        """
        return await self.fetch_one(
            """
            INSERT INTO report_autofix_queue
                (report_id, guild_id, requested_by, status, requested_at, updated_at)
            VALUES ($1, $2, $3, 'queued', NOW(), NOW())
            ON CONFLICT (report_id) DO UPDATE
                SET status = 'queued',
                    requested_by = EXCLUDED.requested_by,
                    requested_at = NOW(),
                    updated_at   = NOW(),
                    proposed_path = NULL,
                    proposed_lines = NULL,
                    pr_url = NULL,
                    pr_number = NULL,
                    last_error = NULL
                WHERE report_autofix_queue.status IN
                    ('failed','unfixable','discarded','pr_open')
            RETURNING *
            """,
            int(report_id), int(guild_id), int(requested_by),
        )

    async def get_autofix_entry(self, report_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM report_autofix_queue WHERE report_id = $1",
            int(report_id),
        )

    async def list_autofix_entries(
        self, guild_id: int, *, status: str | None = None, limit: int = 100,
    ) -> list[dict]:
        if status:
            return await self.fetch_all(
                "SELECT * FROM report_autofix_queue "
                "WHERE guild_id = $1 AND status = $2 "
                "ORDER BY updated_at DESC LIMIT $3",
                int(guild_id), str(status), int(limit),
            )
        return await self.fetch_all(
            "SELECT * FROM report_autofix_queue "
            "WHERE guild_id = $1 "
            "ORDER BY updated_at DESC LIMIT $2",
            int(guild_id), int(limit),
        )

    async def autofix_status_counts(self, guild_id: int) -> dict[str, int]:
        rows = await self.fetch_all(
            "SELECT status, COUNT(*)::int AS n "
            "FROM report_autofix_queue WHERE guild_id = $1 "
            "GROUP BY status",
            int(guild_id),
        )
        return {str(r["status"]): int(r["n"]) for r in (rows or [])}

    async def claim_next_queued_autofix(
        self, guild_id: int,
    ) -> dict | None:
        """Atomically transition the oldest queued row to ``generating``.

        Used by the worker loop; the SKIP LOCKED + UPDATE chain ensures
        two concurrent workers can't claim the same row. Returns the
        row that was claimed, or None if the queue is empty.
        """
        return await self.fetch_one(
            """
            UPDATE report_autofix_queue
               SET status = 'generating', updated_at = NOW()
             WHERE report_id = (
                 SELECT report_id FROM report_autofix_queue
                  WHERE guild_id = $1 AND status = 'queued'
                  ORDER BY requested_at ASC
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED
             )
            RETURNING *
            """,
            int(guild_id),
        )

    async def update_autofix_status(
        self, report_id: int, status: str,
        *,
        proposed_path: str | None = None,
        proposed_lines: int | None = None,
        issue_url: str | None = None,
        issue_number: int | None = None,
        pr_url: str | None = None,
        pr_number: int | None = None,
        last_error: str | None = None,
    ) -> dict | None:
        """Patch a single row. Only fields explicitly provided are
        updated; pass None for anything you want to leave alone.
        """
        sets = ["status = $2", "updated_at = NOW()"]
        params: list = [int(report_id), str(status)]
        idx = 3
        for col, val in (
            ("proposed_path",  proposed_path),
            ("proposed_lines", proposed_lines),
            ("issue_url",      issue_url),
            ("issue_number",   issue_number),
            ("pr_url",         pr_url),
            ("pr_number",      pr_number),
            ("last_error",     last_error),
        ):
            if val is not None:
                sets.append(f"{col} = ${idx}")
                params.append(val)
                idx += 1
        sql = (
            f"UPDATE report_autofix_queue SET {', '.join(sets)} "
            f"WHERE report_id = $1 RETURNING *"
        )
        return await self.fetch_one(sql, *params)

    async def reset_stale_generating_autofixes(
        self, guild_id: int, older_than_seconds: int = 600,
    ) -> int:
        """Bot crashed mid-LLM-call? The row sits in ``generating``
        forever. This helper -- called from cog_load + once per worker
        tick -- flips anything stuck > ``older_than_seconds`` back to
        ``queued`` so the next worker pass picks it up.
        """
        result = await self.execute(
            "UPDATE report_autofix_queue "
            "SET status = 'queued', updated_at = NOW() "
            "WHERE guild_id = $1 AND status = 'generating' "
            "AND updated_at < NOW() - ($2 || ' seconds')::interval",
            int(guild_id), int(older_than_seconds),
        )
        return self._row_count(result)

    async def reset_in_memory_proposed_autofixes(self, guild_id: int) -> int:
        """On bot restart, any ``proposed`` rows lost their in-memory
        patch text. Flip them back to ``queued`` so the worker
        regenerates. Called once from cog_load.
        """
        result = await self.execute(
            "UPDATE report_autofix_queue "
            "SET status = 'queued', updated_at = NOW() "
            "WHERE guild_id = $1 AND status = 'proposed'",
            int(guild_id),
        )
        return self._row_count(result)

    async def clear_terminal_autofixes(self, guild_id: int) -> int:
        """Drop terminal rows (failed / unfixable / discarded / pr_open).
        Used by ,admin reports queue clear. Returns count deleted.
        """
        result = await self.execute(
            "DELETE FROM report_autofix_queue "
            "WHERE guild_id = $1 AND status IN "
            "('failed','unfixable','discarded','pr_open')",
            int(guild_id),
        )
        return self._row_count(result)

    async def cancel_autofix(
        self, report_id: int, guild_id: int, *, reason: str = "",
    ) -> dict | None:
        """Flip one non-terminal row to ``discarded``. Returns the
        updated row, or None if the row didn't exist or was already in
        a terminal state (admin sees a 'nothing to cancel' note then).
        """
        return await self.fetch_one(
            """
            UPDATE report_autofix_queue
               SET status = 'discarded',
                   last_error = COALESCE(NULLIF($3, ''), last_error),
                   updated_at = NOW()
             WHERE report_id = $1 AND guild_id = $2
               AND status IN ('queued','generating','proposed')
            RETURNING *
            """,
            int(report_id), int(guild_id),
            reason or "Cancelled by admin via ,admin reports queue cancel.",
        )

    async def count_reports_older_than(
        self, guild_id: int, days: int,
        *, status_in: tuple[str, ...] | None = None,
    ) -> int:
        """How many reports in ``guild_id`` are older than ``days`` days
        and currently in one of ``status_in`` (default: every non-
        terminal status). Used by the close-old subcommand to preview
        the count before the destructive write.
        """
        statuses = tuple(status_in) if status_in else (
            "open", "accepted", "in_progress", "resolved",
        )
        # Build the cutoff in Python so asyncpg's type inference doesn't
        # complain about the ``$N || ' days'`` concat that the
        # interval-cast version triggered ('expected str, got int').
        cutoff = datetime.datetime.now(timezone.utc) - datetime.timedelta(days=int(days))
        return await self.fetch_val(
            "SELECT COUNT(*)::int FROM reports "
            "WHERE guild_id = $1 "
            "AND created_at < $2 "
            "AND status = ANY($3::text[])",
            int(guild_id), cutoff, list(statuses),
        )

    async def bulk_close_reports_older_than(
        self, guild_id: int, days: int,
        *,
        status_in: tuple[str, ...] | None = None,
        admin_note: str = "",
    ) -> list[dict]:
        """Set status='closed' on every report in ``guild_id`` older than
        ``days`` and currently in one of ``status_in``. Returns the
        affected rows so the caller can DM the reporters / update feed.
        """
        statuses = tuple(status_in) if status_in else (
            "open", "accepted", "in_progress", "resolved",
        )
        cutoff = datetime.datetime.now(timezone.utc) - datetime.timedelta(days=int(days))
        return await self.fetch_all(
            """
            UPDATE reports
               SET status = 'closed',
                   admin_note = COALESCE(NULLIF($4, ''), admin_note),
                   updated_at = NOW()
             WHERE guild_id = $1
               AND created_at < $2
               AND status = ANY($3::text[])
            RETURNING *
            """,
            int(guild_id), cutoff, list(statuses),
            admin_note or "",
        )

    async def cancel_active_autofixes(
        self, guild_id: int, *, reason: str = "",
    ) -> list[dict]:
        """Flip every non-terminal row in ``guild_id`` to ``discarded``.
        Returns the list of rows that were affected so the caller can
        DM each report's recipient + drop the in-memory patch.
        """
        return await self.fetch_all(
            """
            UPDATE report_autofix_queue
               SET status = 'discarded',
                   last_error = COALESCE(NULLIF($2, ''), last_error),
                   updated_at = NOW()
             WHERE guild_id = $1
               AND status IN ('queued','generating','proposed')
            RETURNING *
            """,
            int(guild_id),
            reason or "Bulk-cancelled by admin via ,admin reports queue cancel.",
        )
