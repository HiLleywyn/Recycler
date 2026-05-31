"""core/framework/ai/diagnose_ai.py - AI-assisted investigation for the diagnose command.

Safety model:
  safe      - pure SELECT on non-sensitive tables, single confirm
  sensitive - SELECT touching user financial/personal data, double confirm
  blocked   - anything that writes, drops, or executes code, no confirmation

When an OpenRouter key is configured the investigator uses function/tool calling
so the AI can request context (guild settings, task loops, beta features, etc.)
as well as SQL queries.  When only Ollama is available it falls back to a
JSON-prompt approach that supports SQL queries only.

ask_investigator() returns a dict with one of these shapes:
  {"done": True,  "reasoning": str,  "tool_calls": None, "query": None}
  {"done": False, "tool_calls": [...], "reasoning": None, "query": None}  # tool-call mode
  {"done": False, "query": str,       "reasoning": str,  "tool_calls": None}  # JSON fallback
"""
from __future__ import annotations

import json
import logging
import re

from core.config import Config

log = logging.getLogger(__name__)

# ── SQL safety ─────────────────────────────────────────────────────────────────

_BLOCKED_KEYWORDS: frozenset[str] = frozenset({
    "insert", "update", "delete", "drop", "create", "alter", "truncate",
    "execute", "copy", "grant", "revoke", "notify", "listen", "call", "do",
    "pg_terminate_backend", "pg_cancel_backend", "pg_read_file",
    "pg_write_file", "pg_ls_dir", "pg_exec",
})

# Tables whose data is considered sensitive -- double confirm required
_SENSITIVE_TABLES: frozenset[str] = frozenset({
    "users", "user_prefs", "transactions", "wallet_holdings",
    "crypto_holdings", "bank_transactions", "savings_accounts",
    "defi_positions", "api_keys",
})

_MAX_QUERY_LEN = 1000
_AUTO_LIMIT = 50


def classify_query(sql: str) -> str:
    """Return 'safe', 'sensitive', or 'blocked'."""
    stripped = sql.strip().rstrip(";")
    if not re.match(r"^\s*select\b", stripped, re.IGNORECASE):
        return "blocked"
    if ";" in stripped:
        return "blocked"
    lower = stripped.lower()
    for kw in _BLOCKED_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", lower):
            return "blocked"
    for tbl in _SENSITIVE_TABLES:
        if re.search(r"\b" + re.escape(tbl) + r"\b", lower):
            return "sensitive"
    return "safe"


def enforce_limit(sql: str) -> str:
    """Append LIMIT _AUTO_LIMIT if the query has none."""
    stripped = sql.strip().rstrip(";")
    if re.search(r"\blimit\s+\d+", stripped, re.IGNORECASE):
        return stripped
    return f"{stripped} LIMIT {_AUTO_LIMIT}"


# ── Tool definitions (OpenAI function-calling format) ─────────────────────────

DIAG_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_sql_query",
            "description": (
                "Run a read-only SQL SELECT on the Discoin PostgreSQL database. "
                "The user must confirm the query before it runs. "
                "Use information_schema, pg_catalog, or specific bot tables. "
                "Always include a LIMIT clause."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A SQL SELECT query. No semicolons in the middle. Must start with SELECT.",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_guild_settings",
            "description": "Fetch this guild's configuration (channels, modules, prefix, etc.).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_loaded_cogs",
            "description": "List all currently loaded Discord bot extensions/cogs and whether they loaded successfully.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_loops",
            "description": "Get the running/failed/stopped status of all background task loops.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_beta_features",
            "description": "List all beta features enabled for this guild.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "conclude",
            "description": "Conclude the investigation with a summary once you have enough context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Summary of findings and any recommended actions.",
                    }
                },
                "required": ["summary"],
            },
        },
    },
]

# ── System prompt (tool-call mode) ─────────────────────────────────────────────

_TOOL_SYSTEM_PROMPT = (
    "You are a diagnostic assistant for the Discoin Discord economy bot. "
    "You have been given the results of an automated health check that found failures or warnings. "
    "Use the provided tools to investigate the root cause. "
    "Start by calling context tools (get_guild_settings, list_loaded_cogs, get_task_loops, get_beta_features) "
    "to build a picture before jumping to SQL. "
    "Only call run_sql_query when the context tools are insufficient. "
    "Call conclude when you have a confident assessment. "
    "Make one or two tool calls per round - do not flood with calls."
)

# ── System prompt (JSON fallback for Ollama) ───────────────────────────────────

_JSON_SYSTEM_PROMPT = (
    "You are a PostgreSQL database analyst for the Discoin Discord bot. "
    "You are given diagnostic check results that may contain failures or warnings. "
    "Your job is to propose a single SQL SELECT query to investigate the root cause. "
    "You must respond with ONLY a JSON object - no markdown, no commentary outside the JSON.\n\n"
    "Response format:\n"
    '  {"reasoning": "one or two sentence explanation", "query": "SELECT ...", "done": false}\n'
    "Or if investigation is complete:\n"
    '  {"reasoning": "summary of findings", "query": null, "done": true}\n\n'
    "Rules:\n"
    "- Only use SELECT statements\n"
    "- No semicolons in the middle\n"
    "- Prefer information_schema, pg_catalog, or specific diagnostic tables\n"
    "- Always include a LIMIT clause\n"
    "- If a previous query already resolved the issue, set done=true\n"
    "- Maximum 3 follow-up rounds before setting done=true"
)


async def get_diagnose_ai_config(db, guild_id: int) -> dict:
    """Return AI config for the diagnose investigation, reusing heal AI settings."""
    try:
        settings = await db.get_guild_settings(guild_id)
    except Exception:
        settings = {}
    return {
        "backend":  settings.get("heal_ai_backend")  or Config.TOOLS_BACKEND or "openrouter",
        "model":    settings.get("heal_ai_model")    or Config.TOOLS_MODEL   or "",
        "base_url": settings.get("heal_ai_base_url") or "",
    }


async def ask_investigator(
    diag_summary: str,
    history: list[dict],
    config: dict,
) -> dict | None:
    """Ask the AI for the next investigation step.

    ``history`` entries have one of these shapes:
      {"query": str, "result": str}              -- SQL round (both modes)
      {"tool": str, "args": dict, "result": str} -- context tool round (tool-call mode)

    Returns a dict with keys: reasoning, query (str|None), done (bool),
    tool_calls (list|None).  Returns None on AI error.
    """
    backend = config.get("backend", "openrouter").lower()
    use_tools = backend != "ollama" and bool(Config.OPENROUTER_API_KEY)

    # Build user content
    user_content = f"Diagnostic results:\n\n{diag_summary}"
    if history:
        prev_parts = []
        for i, h in enumerate(history):
            if "query" in h:
                prev_parts.append(f"Round {i+1} SQL:\n{h['query']}\nResult:\n{h['result']}")
            else:
                tool_name = h.get("tool", "tool")
                prev_parts.append(f"Round {i+1} tool={tool_name}:\n{h.get('result', '')}")
        user_content += "\n\nPrevious rounds:\n\n" + "\n\n".join(prev_parts)

    if use_tools:
        return await _ask_with_tools(user_content, config)
    return await _ask_json_fallback(user_content, config)


async def _ask_with_tools(user_content: str, config: dict) -> dict | None:
    """Use OpenRouter function calling."""
    from core.framework.ai.client import complete_with_tool_calls

    messages = [
        {"role": "system", "content": _TOOL_SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]
    model = config.get("model") or None

    try:
        text, tool_calls = await complete_with_tool_calls(
            messages, DIAG_TOOLS, model=model, max_tokens=512, temperature=0.2,
        )
    except Exception as exc:
        log.warning("diagnose_ai: tool-call AI failed: %s", exc)
        return None

    if tool_calls:
        # Parse each tool call's arguments JSON
        parsed_calls = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {}
            parsed_calls.append({
                "id":   tc.get("id", ""),
                "name": fn.get("name", ""),
                "args": args,
            })
        return {"done": False, "reasoning": None, "query": None, "tool_calls": parsed_calls}

    if text:
        return {"done": True, "reasoning": text, "query": None, "tool_calls": None}

    return None


async def _ask_json_fallback(user_content: str, config: dict) -> dict | None:
    """Ollama / no-key fallback: prompt-based JSON response."""
    messages = [
        {"role": "system", "content": _JSON_SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]
    backend  = config.get("backend", "openrouter").lower()
    model    = config.get("model") or None
    base_url = config.get("base_url", "").strip()

    raw: str | None = None
    try:
        if backend == "ollama":
            from core.framework.ai.client import complete_ollama
            import os
            if base_url:
                old = os.environ.get("OLLAMA_BASE_URL", "")
                os.environ["OLLAMA_BASE_URL"] = base_url
                try:
                    raw = await complete_ollama(messages, model=model or "llama3.2", max_tokens=512, temperature=0.2)
                finally:
                    os.environ["OLLAMA_BASE_URL"] = old
            else:
                raw = await complete_ollama(messages, model=model or "llama3.2", max_tokens=512, temperature=0.2)
        else:
            from core.framework.ai.client import complete
            raw = await complete(messages, model=model, max_tokens=512, temperature=0.2)
    except Exception as exc:
        log.warning("diagnose_ai: JSON-fallback AI failed: %s", exc)
        return None

    if not raw:
        return None

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()

    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
        parsed.setdefault("reasoning", "")
        parsed.setdefault("query", None)
        parsed.setdefault("done", False)
        parsed["tool_calls"] = None
        return parsed
    except (json.JSONDecodeError, Exception) as exc:
        log.warning("diagnose_ai: failed to parse JSON fallback: %s | raw: %.200s", exc, raw)
        return None
