"""core/framework/ai/heal_ai.py  -  AI client for heal analysis.

Provides a single entry point, complete_heal(), that routes to the right
provider using per-guild settings (heal_ai_backend / heal_ai_model /
heal_ai_base_url), falling back to the global TOOLS_BACKEND / TOOLS_MODEL
config if none are set.

Per-guild overrides are managed via .admin ai heal <subcommand>.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.config import Config

if TYPE_CHECKING:
    from database.database import Database

log = logging.getLogger("discoin.heal_ai")

_HEAL_SYSTEM_PROMPT = (
    "You are a Discoin server health analyst. "
    "You will be given the output of a bot health diagnostic. "
    "Identify the most critical issues, explain what each one means in plain language, "
    "and give specific actionable steps to resolve them. "
    "Be concise  -  use bullet points, no preamble. "
    "If everything is healthy, say so in one sentence."
)


async def get_heal_ai_config(db: "Database", guild_id: int) -> dict:
    """Return the heal AI provider config for a guild.

    Keys:
      backend    -  "openrouter" | "ollama"
      model      -  model identifier string, or "" to use the global default
      base_url   -  custom base URL for Ollama (overrides OLLAMA_BASE_URL env var)
    """
    settings = await db.get_guild_settings(guild_id)
    # Explicit heal_ai_model wins; fall through to the "code" category guild
    # default (set via ,ai model set code), then the global TOOLS_MODEL env var.
    _heal_model = settings.get("heal_ai_model") or ""
    if not _heal_model:
        try:
            from core.framework.ai.models import get_guild_default as _ai_get_guild_default
            _code_pick = await _ai_get_guild_default(db, guild_id, "code")
            if _code_pick and _code_pick.model:
                _heal_model = _code_pick.model
        except Exception:
            pass
    return {
        "backend":  settings.get("heal_ai_backend")  or Config.TOOLS_BACKEND or "openrouter",
        "model":    _heal_model or Config.TOOLS_MODEL or "",
        "base_url": settings.get("heal_ai_base_url") or "",
    }


async def complete_heal(
    health_report: str,
    config: dict,
) -> str | None:
    """Run an AI analysis of *health_report* using the provider in *config*.

    Args:
        health_report: Plain-text serialisation of the health check results.
        config: Dict from get_heal_ai_config()  -  backend, model, base_url.

    Returns:
        AI-generated analysis string, or None on failure.
    """
    messages = [
        {"role": "system", "content": _HEAL_SYSTEM_PROMPT},
        {"role": "user",   "content": f"Health report:\n\n{health_report}"},
    ]

    backend  = config.get("backend", "openrouter").lower()
    model    = config.get("model") or None
    base_url = config.get("base_url", "").strip()

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
                        max_tokens=500,
                        temperature=0.4,
                    )
                finally:
                    os.environ["OLLAMA_BASE_URL"] = old
            return await complete_ollama(
                messages,
                model=model or "llama3.2",
                max_tokens=500,
                temperature=0.4,
            )
        else:
            from core.framework.ai.client import complete
            return await complete(
                messages,
                model=model or None,
                max_tokens=500,
                temperature=0.4,
            )
    except Exception:
        log.exception("Heal AI completion failed (backend=%s)", backend)
        return None


def build_health_report(sections: list[tuple[str, str]]) -> str:
    """Serialise the (name, content) tuples from a health check into plain text."""
    lines: list[str] = []
    for name, content in sections:
        # Strip Discord markdown (*, **, `, <#id>) for the AI
        import re
        clean = re.sub(r"<#\d+>", "#channel", content)
        clean = re.sub(r"[`*_~]", "", clean)
        lines.append(f"[{name}]\n{clean}")
    return "\n\n".join(lines)
