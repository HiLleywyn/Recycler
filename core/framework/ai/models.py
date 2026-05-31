"""
core/framework/ai/models.py -- recommended models per tool category and the
selectable default resolver for the ``,ai`` config menu.

Why this exists
---------------
Different AI jobs have wildly different sweet-spots. Using ``gpt-4o`` for
flavor text wastes money; using ``flux-schnell`` for a tool-calling loop
doesn't even make sense. Hard-coding one env var per job also scales poorly
once the list of jobs grows beyond chat/tools/vision/image.

This module:

1. Names the categories Discoin cares about (see ``TOOL_CATEGORIES``).
2. Ships a curated shortlist of the currently-best models from OpenRouter
   and Ollama for each category, so an admin can pick from a dropdown
   instead of pasting provider strings.
3. Reads and writes the per-guild default from ``ai_model_defaults`` with
   a clean fallback to the env-level ``Config`` so unset categories keep
   behaving like they did before this module landed.

The registry is intentionally data, not code. Any cog / service that wants
to honour the admin's selection calls :func:`resolve_model` with a
category, backend, and fallback model. The AI client layer does the rest.

Updating the registry
---------------------
Edit ``_CATALOG`` below. Each entry is (provider, model_id, label).
``label`` is what the dropdown shows to operators; keep it short so the
Discord select menu renders it without truncation. Put the strongest
default first in each list -- the config menu starts the cursor there.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from core.config import Config

log = logging.getLogger("discoin.ai.models")


# ── Tool categories ───────────────────────────────────────────────────────────
#
# Every category maps to one of the jobs Discoin routes AI traffic through.
# The short key is used as the row key in ``ai_model_defaults`` and as the
# CLI argument for ``,ai model set <category> ...``.
#
# Wiring status (guild override actually consumed at runtime):
#   chat         -- wired: cogs/help.py _run_ai_chat via resolve_model
#   tools        -- wired: core/framework/agent_tools/ai_bridge._resolve_tools_pick
#   vision       -- wired: core/framework/agent_tools/tools/vision.describe_image
#   image        -- wired: core/framework/agent_tools/tools/image_gen.generate
#   search       -- wired: core/framework/agent_tools/tools/data.web_search
#   code         -- wired: core/framework/ai/heal_ai.get_heal_ai_config (fallback after heal_ai_model)
#   reason       -- wired: ai_bridge final-pass model when risk.* tools run
#   automation   -- wired: ai_bridge final-pass model when automation.* tools run
#   defi         -- wired: ai_bridge final-pass model when defi.* tools run
#   economy_sim  -- wired: ai_bridge final-pass model when economy_sim.* tools run

@dataclass(frozen=True)
class Category:
    key: str
    label: str
    summary: str
    default_env: tuple[str, str]
        # (provider, env_model_attr_name) -- which Config field backs this
        # category when no per-guild override is set.


TOOL_CATEGORIES: tuple[Category, ...] = (
    Category(
        key="chat",
        label="Chat / Persona",
        summary="User-facing ,ask chat and persona replies. Cheap, fast, friendly.",
        default_env=("openrouter", "OPENROUTER_MODEL"),
    ),
    Category(
        key="tools",
        label="Tool Calling",
        summary="Agent tool loop (wallet, market, alerts, vision dispatch).",
        default_env=("openrouter", "OPENROUTER_MODEL"),
    ),
    Category(
        key="vision",
        label="Vision / Image Describe",
        summary="vision.describe_image -- OCR, chart reading, meme captions.",
        default_env=("ollama", "VISION_MODEL"),
    ),
    Category(
        key="image",
        label="Image Generation",
        summary="image.generate agent tool -- flux / sdxl / DALL-E class.",
        default_env=("openrouter", "IMAGE_GEN_MODEL"),
    ),
    Category(
        key="search",
        label="Web Search",
        summary="data.web_search -- live-web routed queries with citations.",
        default_env=("openrouter", "SEARCH_MODEL"),
    ),
    Category(
        key="code",
        label="Code / Diagnose",
        summary=".health analyze + heal AI -- diffs, stack traces, SQL.",
        default_env=("openrouter", "TOOLS_MODEL"),
    ),
    Category(
        key="reason",
        label="Reasoning / Risk",
        summary="risk.analyze + deep economic reasoning. Slower, smarter.",
        default_env=("openrouter", "TOOLS_MODEL"),
    ),
    Category(
        key="automation",
        label="Automation / Workflow",
        summary="Task queue, triggers, chains -- autonomous background agent.",
        default_env=("openrouter", "TOOLS_MODEL"),
    ),
    Category(
        key="defi",
        label="Crypto / DeFi Execution",
        summary="Swap / stake / LP planning and execution routing.",
        default_env=("openrouter", "TOOLS_MODEL"),
    ),
    Category(
        key="economy_sim",
        label="Game Economy Simulation",
        summary="Simulated economy manipulation, price shock modelling.",
        default_env=("openrouter", "TOOLS_MODEL"),
    ),
)


_CATEGORIES_BY_KEY: dict[str, Category] = {c.key: c for c in TOOL_CATEGORIES}


def category(key: str) -> Category | None:
    return _CATEGORIES_BY_KEY.get(key.lower())


# ── Curated catalog ──────────────────────────────────────────────────────────
#
# Keep this list short and strong. The first entry of each (provider, cat)
# pair is the recommended default shown at the top of the dropdown. All
# model ids are literal strings passed to the OpenRouter / Ollama APIs.
#
# These picks were chosen for: cost-efficiency on user-facing chat, strong
# tool-calling on agent loops, multimodal support on vision, and cheap
# strong generations on image. Update the list when newer models land.

@dataclass(frozen=True)
class ModelOption:
    provider: str           # "openrouter" | "ollama"
    model: str              # model id passed to the provider
    label: str              # short UI label for the dropdown

    def key(self) -> str:
        return f"{self.provider}:{self.model}"


_CATALOG: dict[str, list[ModelOption]] = {
    "chat": [
        ModelOption("openrouter", "google/gemini-2.5-flash",              "Gemini 2.5 Flash  -  cheap, fast, multimodal"),
        ModelOption("openrouter", "anthropic/claude-haiku-4-5",           "Claude Haiku 4.5  -  fast + sharp"),
        ModelOption("openrouter", "anthropic/claude-3-5-haiku-20241022",  "Claude 3.5 Haiku  -  reliable fallback"),
        ModelOption("openrouter", "openai/gpt-4o-mini",                   "GPT-4o mini  -  safe fallback"),
        ModelOption("openrouter", "meta-llama/llama-3.3-70b-instruct",    "Llama 3.3 70B  -  open, solid"),
        ModelOption("ollama",     "gemma3:27b",                           "Local gemma3:27b  -  strong local chat"),
        ModelOption("ollama",     "llama3.2",                             "Local llama3.2  -  lightweight fallback"),
    ],
    # NOTE: tool calling requires a model with strong structured-output ability.
    # Small models (< 7B) frequently misfire tool schemas and force the full
    # 3-iteration loop, tripling latency. Prefer Gemini 2.5 Flash or Haiku 4.5
    # for the best speed/quality tradeoff. Do NOT use 4B-class models here.
    # OpenRouter uses no-date aliases for Claude 4.x (e.g. claude-haiku-4-5),
    # NOT the date-versioned Anthropic API strings (claude-haiku-4-5-20251001).
    "tools": [
        ModelOption("openrouter", "google/gemini-2.5-flash",              "Gemini 2.5 Flash  -  fastest good tool-caller"),
        ModelOption("openrouter", "anthropic/claude-haiku-4-5",           "Claude Haiku 4.5  -  fast + accurate tools"),
        ModelOption("openrouter", "anthropic/claude-3-5-haiku-20241022",  "Claude 3.5 Haiku  -  reliable tool-caller"),
        ModelOption("openrouter", "openai/gpt-4o-mini",                   "GPT-4o mini  -  cheap structured calls"),
        ModelOption("openrouter", "mistralai/mistral-large",              "Mistral Large  -  EU host, strong tools"),
        ModelOption("openrouter", "google/gemini-2.5-pro",                "Gemini 2.5 Pro  -  long context + tools"),
        ModelOption("ollama",     "gemma3:27b",                           "Local gemma3:27b  -  best local tool calling"),
        ModelOption("ollama",     "llama3.1:70b",                         "Local llama3.1:70b  -  deep local tools"),
    ],
    "vision": [
        ModelOption("openrouter", "google/gemini-2.5-flash",              "Gemini 2.5 Flash  -  fast multimodal"),
        ModelOption("openrouter", "anthropic/claude-haiku-4-5",           "Claude Haiku 4.5  -  cheap vision"),
        ModelOption("openrouter", "anthropic/claude-3-5-haiku-20241022",  "Claude 3.5 Haiku  -  reliable vision"),
        ModelOption("openrouter", "openai/gpt-4o-mini",                   "GPT-4o mini  -  reliable vision"),
        ModelOption("ollama",     "gemma3:27b",                           "Local gemma3:27b  -  multimodal"),
        ModelOption("ollama",     "llava:13b",                            "Local llava:13b  -  classic vision"),
    ],
    "image": [
        ModelOption("openrouter", "black-forest-labs/flux-schnell",       "FLUX schnell  -  fast, cheap"),
        ModelOption("openrouter", "black-forest-labs/flux-1.1-pro",       "FLUX 1.1 Pro  -  sharper output"),
        ModelOption("openrouter", "stabilityai/stable-diffusion-3-5-large","SD 3.5 Large  -  open SDXL-class"),
        ModelOption("openrouter", "openai/dall-e-3",                      "DALL-E 3  -  prompt fidelity"),
    ],
    # NOTE: search model only applies to AI-summary backends (openrouter,
    # perplexity, ollama).  Raw-engine backends ignore SEARCH_MODEL:
    #   SEARCH_BACKEND=ddg                    no key required, no model
    #   SEARCH_BACKEND=brave + BRAVE_SEARCH_API_KEY
    #   SEARCH_BACKEND=perplexity + PERPLEXITY_API_KEY (model = SEARCH_MODEL)
    #   SEARCH_BACKEND=openrouter + OPENROUTER_API_KEY (model = SEARCH_MODEL)
    #   SEARCH_BACKEND=ollama + OLLAMA_BASE_URL (model = SEARCH_MODEL)
    "search": [
        ModelOption("openrouter", "perplexity/sonar",                     "Perplexity Sonar (via OpenRouter)  -  fast"),
        ModelOption("openrouter", "perplexity/sonar-pro",                 "Perplexity Sonar Pro (via OpenRouter)"),
        ModelOption("openrouter", "perplexity/sonar-reasoning",           "Perplexity Reasoning (via OpenRouter)  -  CoT"),
        ModelOption("openrouter", "google/gemini-2.5-flash",              "Gemini 2.5 Flash (via OpenRouter)  -  web-grounded"),
    ],
    "code": [
        ModelOption("openrouter", "anthropic/claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet  -  top-tier codegen"),
        ModelOption("openrouter", "anthropic/claude-3-5-haiku-20241022",  "Claude 3.5 Haiku  -  fast code fixes"),
        ModelOption("openrouter", "qwen/qwen-2.5-coder-32b-instruct",     "Qwen2.5-Coder 32B  -  code specialist"),
        ModelOption("openrouter", "deepseek/deepseek-coder-v2",           "DeepSeek Coder v2  -  cheap + strong"),
        ModelOption("ollama",     "qwen2.5-coder:32b",                    "Local qwen2.5-coder:32b"),
        ModelOption("ollama",     "codellama:34b",                        "Local codellama:34b"),
    ],
    "reason": [
        ModelOption("openrouter", "deepseek/deepseek-r1",                 "DeepSeek R1  -  open reasoning"),
        ModelOption("openrouter", "anthropic/claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet  -  strong + fast"),
        ModelOption("openrouter", "openai/o1-mini",                       "o1-mini  -  reasoning, cheap"),
        ModelOption("openrouter", "openai/o1",                            "o1  -  reasoning, expensive"),
        ModelOption("ollama",     "deepseek-r1:70b",                      "Local deepseek-r1:70b"),
    ],
    "automation": [
        ModelOption("openrouter", "google/gemini-2.5-flash",              "Gemini 2.5 Flash  -  fast autonomous loops"),
        ModelOption("openrouter", "anthropic/claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet  -  best agent"),
        ModelOption("openrouter", "anthropic/claude-3-5-haiku-20241022",  "Claude 3.5 Haiku  -  cheap automation"),
        ModelOption("openrouter", "openai/gpt-4o",                        "GPT-4o  -  strong agent"),
        ModelOption("openrouter", "mistralai/mistral-large",              "Mistral Large  -  EU agent"),
        ModelOption("ollama",     "llama3.1:70b",                         "Local llama3.1:70b"),
    ],
    "defi": [
        ModelOption("openrouter", "anthropic/claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet  -  sharpest math"),
        ModelOption("openrouter", "google/gemini-2.5-flash",              "Gemini 2.5 Flash  -  fast DeFi planner"),
        ModelOption("openrouter", "deepseek/deepseek-r1",                 "DeepSeek R1  -  reasoning router"),
        ModelOption("openrouter", "openai/gpt-4o",                        "GPT-4o  -  reliable planner"),
        ModelOption("ollama",     "llama3.1:70b",                         "Local llama3.1:70b"),
    ],
    "economy_sim": [
        ModelOption("openrouter", "deepseek/deepseek-r1",                 "DeepSeek R1  -  long-horizon sim"),
        ModelOption("openrouter", "anthropic/claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet  -  balanced"),
        ModelOption("openrouter", "openai/o1-mini",                       "o1-mini  -  cheap reasoner"),
        ModelOption("ollama",     "deepseek-r1:70b",                      "Local deepseek-r1:70b"),
    ],
}


def catalog_for(category_key: str) -> list[ModelOption]:
    """Return the curated option list for a category (empty if unknown)."""
    return list(_CATALOG.get(category_key.lower(), ()))


def all_categories() -> Iterable[Category]:
    return TOOL_CATEGORIES


# ── Vision-capability heuristic ──────────────────────────────────────────────
#
# Substrings that strongly indicate a model accepts image inputs. Used by the
# vision tool to decide whether a guild-picked model should be tried at all,
# and by the model picker to warn admins who set ``vision`` to a text-only
# slug. Matching is case-insensitive substring -- the slug only has to
# CONTAIN one of these hints, so e.g. ``openai/gpt-4o-mini-2024-07-18``
# matches ``gpt-4o``.
#
# Keep this list curated -- false positives (text-only models flagged as
# vision-capable) cause the same silent-failure mode the heuristic exists
# to prevent. False negatives just mean the guild pick is deprioritised
# and the fallback chain takes over, which is a benign outcome.
_VISION_CAPABLE_HINTS: tuple[str, ...] = (
    # OpenAI multimodal lineage
    "gpt-4o", "gpt-4-vision", "gpt-4-turbo",
    "gpt-4.1", "gpt-4.5", "gpt-5",
    "o1", "o3", "o4",
    "chatgpt-4o",
    # Anthropic (Claude 3 onward all accept images)
    "claude-3", "claude-haiku-3", "claude-haiku-4", "claude-haiku-5",
    "claude-sonnet-3", "claude-sonnet-4", "claude-sonnet-5",
    "claude-opus-3", "claude-opus-4",
    # Google Gemini (1.5+ all multimodal)
    "gemini",
    # Open-weights vision specialists
    "llava", "pixtral", "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3-vl",
    "llama-3.2-vision", "llama3.2-vision", "llama-4-vision",
    "minicpm-v", "minicpm-llama3-v",
    "phi-4-multimodal", "phi-3-vision", "phi-3.5-vision",
    "internvl", "cogvlm", "moondream",
    # Mistral multimodal
    "pixtral", "mistral-medium-3",
    # Kimi multimodal (k2.6 has vision per upstream docs)
    "kimi-vl", "kimi-k2.6", "kimi-k3",
    # Ollama tagged Gemma 3 is multimodal at all parameter sizes;
    # the OpenRouter "google/gemma-3-*-it" routes are text-only and
    # intentionally NOT matched here.
    "gemma3:",
    # Generic fallback hints (when providers ship explicit suffixes)
    "-vision", "-multimodal", "-vl-", "/vl-",
)


def is_vision_capable_slug(model: str | None) -> bool:
    """Heuristic: does this model slug look like it accepts images?

    True means the slug matches a curated multimodal hint. False means
    "no signal" -- the slug might still work in practice (exotic
    providers, new releases) but the vision tool will deprioritise it
    so a text-only model can't silently swallow the only vision attempt.
    """
    if not model:
        return False
    s = model.lower()
    return any(hint in s for hint in _VISION_CAPABLE_HINTS)


# ── Per-guild override storage ────────────────────────────────────────────────

async def get_guild_default(
    db: Any, guild_id: int, category_key: str,
) -> ModelOption | None:
    """Return the admin-selected model for ``category_key`` in this guild,
    or ``None`` if the guild has not overridden the env default."""
    row = await db.fetch_one(
        "SELECT provider, model FROM ai_model_defaults "
        "WHERE guild_id=$1 AND category=$2",
        int(guild_id), category_key.lower(),
    )
    if not row:
        return None
    return ModelOption(
        provider=str(row["provider"]),
        model=str(row["model"]),
        label=f"{row['provider']}:{row['model']}",
    )


async def set_guild_default(
    db: Any, guild_id: int, category_key: str,
    provider: str, model: str, updated_by: int | None,
) -> None:
    """Upsert the admin selection for ``category_key``."""
    await db.execute(
        """
        INSERT INTO ai_model_defaults
            (guild_id, category, provider, model, updated_by, updated_at)
        VALUES ($1,$2,$3,$4,$5,NOW())
        ON CONFLICT (guild_id, category) DO UPDATE
          SET provider=EXCLUDED.provider,
              model=EXCLUDED.model,
              updated_by=EXCLUDED.updated_by,
              updated_at=NOW()
        """,
        int(guild_id), category_key.lower(),
        provider.lower(), model.strip(),
        int(updated_by) if updated_by is not None else None,
    )


async def clear_guild_default(db: Any, guild_id: int, category_key: str) -> bool:
    res = await db.execute(
        "DELETE FROM ai_model_defaults WHERE guild_id=$1 AND category=$2",
        int(guild_id), category_key.lower(),
    )
    return "DELETE 1" in str(res)


async def list_guild_defaults(db: Any, guild_id: int) -> dict[str, ModelOption]:
    rows = await db.fetch_all(
        "SELECT category, provider, model FROM ai_model_defaults WHERE guild_id=$1",
        int(guild_id),
    )
    return {
        str(r["category"]): ModelOption(
            provider=str(r["provider"]),
            model=str(r["model"]),
            label=f"{r['provider']}:{r['model']}",
        )
        for r in rows
    }


# ── Resolution ────────────────────────────────────────────────────────────────

def _env_default_for(cat: Category) -> ModelOption:
    provider, attr = cat.default_env
    model = str(getattr(Config, attr, "") or "")
    return ModelOption(provider=provider, model=model, label=f"env:{attr}")


async def resolve_model(
    db: Any, guild_id: int | None, category_key: str,
) -> ModelOption:
    """Resolve ``(provider, model)`` for a category.

    Order of precedence (env-wins policy):
        1. Env default declared in :class:`Category.default_env`, when set.
        2. Per-guild row in ``ai_model_defaults`` (Discord ``,ai model set``).

    The operator's environment is the canonical source of truth: the env
    var is set in Railway/Docker by the bot operator and should not be
    silently overridden by per-guild Discord picks. Guild picks only
    take effect when the operator has NOT set the env var (empty or
    unset), so a guild can still self-serve where the operator has
    intentionally left a hole.

    Callers that don't have a db handle can pass ``db=None`` and get the
    env default, which is what things like startup-time bootstrapping do.
    """
    cat = category(category_key)
    if cat is None:
        raise ValueError(f"unknown ai category: {category_key!r}")
    env = _env_default_for(cat)
    if env.model:
        return env
    if db is not None and guild_id:
        picked = await get_guild_default(db, guild_id, cat.key)
        if picked is not None and picked.model:
            return picked
    return env
