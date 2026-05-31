"""core/framework/ai/report_ai.py  -  AI realness analysis for player reports.

Mirrors core/framework/ai/heal_ai.py exactly: a single ``complete_report_diagnosis``
entry point that routes to OpenRouter or Ollama using the same per-guild
``heal_ai_*`` settings (one provider config powers every AI feature on the
server -- no separate column for every analyser).

Used by ,admin reports diagnose <id>: feed in the report text plus a few
cheap signals (account age, report count, length, status) and return a
short verdict on whether the report looks real or trolled / spam / AI-
generated.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database.database import Database

log = logging.getLogger("discoin.report_ai")

_REPORT_SYSTEM_PROMPT = (
    "You are a Discoin moderator triaging player-submitted bug reports and "
    "suggestions. You will be given the report text plus signals about the "
    "submitter (account age, prior report count, etc). Return a SHORT "
    "verdict in this exact shape:\n"
    "\n"
    "Verdict: <real | likely_real | suspicious | likely_fake | spam>\n"
    "Confidence: <low | medium | high>\n"
    "Reasoning: <2-3 sentences, no preamble>\n"
    "Recommended action: <accept | investigate | reject>\n"
    "\n"
    "Lean toward 'real' when the report describes specific commands, "
    "errors, numbers, or reproducible steps. Flag 'suspicious' or 'fake' "
    "for vague complaints, copy-paste templates, repeated identical text, "
    "or content that looks AI-generated (overly polished, lists of "
    "bullet points with no specifics, generic phrases). Do not moralise. "
    "Do not suggest punishments."
)


def _redact(text: str, *, max_chars: int = 4000) -> str:
    """Strip Discord markdown / mentions and clamp length so the prompt
    stays cheap. Mirrors the helper in heal_ai.build_health_report.
    """
    import re
    s = re.sub(r"<@!?\d+>", "@user", text or "")
    s = re.sub(r"<#\d+>", "#channel", s)
    s = re.sub(r"<@&\d+>", "@role", s)
    if len(s) > max_chars:
        s = s[:max_chars] + "\n[...truncated]"
    return s


def build_report_prompt(report: dict, signals: dict) -> str:
    """Turn a report row + submitter signals into the user-message body.

    ``report`` is the row from the ``reports`` table. ``signals`` carries
    cheap-to-compute reputation hints (account_age_days, prior_report_count,
    submitter_message_length, etc.) -- the AI gets to weight them itself.
    """
    lines: list[str] = []
    lines.append(f"Report ID: {report.get('id')}")
    lines.append(f"Category:  {report.get('category')}")
    lines.append(f"Status:    {report.get('status')}")
    lines.append(f"Tags:      {report.get('tags') or '(none)'}")
    lines.append("")
    lines.append("Submitter signals:")
    for k, v in (signals or {}).items():
        lines.append(f"  - {k}: {v}")
    lines.append("")
    lines.append("Report text:")
    lines.append(_redact(str(report.get("message") or "")))
    if report.get("admin_note"):
        lines.append("")
        lines.append("Existing admin note:")
        lines.append(_redact(str(report.get("admin_note") or ""), max_chars=600))
    return "\n".join(lines)


async def complete_report_diagnosis(
    report: dict,
    signals: dict,
    config: dict,
) -> str | None:
    """Run an AI realness check on a single report.

    Args:
        report: row from the ``reports`` table (id / category / message / ...).
        signals: dict of cheap reputation hints (see build_report_prompt).
        config: provider config from ``heal_ai.get_heal_ai_config`` -- we
            reuse the same per-guild backend / model settings so the admin
            doesn't have to configure a separate provider for each AI
            feature.

    Returns:
        AI-generated verdict string (already formatted per the system
        prompt above), or None on failure.
    """
    body = build_report_prompt(report, signals)
    messages = [
        {"role": "system", "content": _REPORT_SYSTEM_PROMPT},
        {"role": "user",   "content": body},
    ]
    backend  = (config.get("backend") or "openrouter").lower()
    model    = config.get("model") or None
    base_url = (config.get("base_url") or "").strip()

    try:
        if backend == "ollama":
            from core.framework.ai.client import complete_ollama
            import os
            if base_url:
                old = os.environ.get("OLLAMA_BASE_URL", "")
                os.environ["OLLAMA_BASE_URL"] = base_url
                try:
                    return await complete_ollama(
                        messages,
                        model=model or "llama3.2",
                        max_tokens=300,
                        temperature=0.2,
                    )
                finally:
                    os.environ["OLLAMA_BASE_URL"] = old
            return await complete_ollama(
                messages,
                model=model or "llama3.2",
                max_tokens=300,
                temperature=0.2,
            )
        from core.framework.ai.client import complete
        return await complete(
            messages,
            model=model or None,
            max_tokens=300,
            temperature=0.2,
        )
    except Exception:
        log.exception("Report AI completion failed (backend=%s)", backend)
        return None


async def gather_signals(db: "Database", guild_id: int, report: dict) -> dict:
    """Cheap reputation signals fed into the realness prompt.

    Account-creation date isn't on the ``users`` table, so we derive a
    proxy from the user's first appearance in the bot DB -- close enough
    for triage. Prior report count is a direct ``SELECT COUNT(*)``. All
    queries swallow exceptions to keep the diagnose command running on
    a corrupt row.
    """
    user_id = int(report.get("user_id") or 0)
    out: dict = {
        "report_message_length": len(str(report.get("message") or "")),
    }
    try:
        prior = await db.fetch_val(
            "SELECT COUNT(*)::int FROM reports "
            "WHERE guild_id = $1 AND user_id = $2 AND id <> $3",
            int(guild_id), user_id, int(report.get("id") or 0),
        )
        out["prior_report_count"] = int(prior or 0)
    except Exception:
        out["prior_report_count"] = "?"
    try:
        first_seen = await db.fetch_val(
            "SELECT MIN(created_at) FROM users "
            "WHERE user_id = $1 AND guild_id = $2",
            user_id, int(guild_id),
        )
        if first_seen is not None:
            import datetime as _dt
            now = _dt.datetime.now(tz=_dt.timezone.utc)
            if hasattr(first_seen, "tzinfo"):
                fs = first_seen if first_seen.tzinfo else first_seen.replace(tzinfo=_dt.timezone.utc)
                age_days = (now - fs).days
                out["account_age_days_in_db"] = max(0, int(age_days))
        else:
            out["account_age_days_in_db"] = "?"
    except Exception:
        out["account_age_days_in_db"] = "?"
    try:
        identical = await db.fetch_val(
            "SELECT COUNT(*)::int FROM reports "
            "WHERE guild_id = $1 AND message = $2 AND id <> $3",
            int(guild_id), str(report.get("message") or ""), int(report.get("id") or 0),
        )
        out["identical_text_in_other_reports"] = int(identical or 0)
    except Exception:
        out["identical_text_in_other_reports"] = "?"
    return out
