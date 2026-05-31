"""DiscoAI memory + learning sidecar.

This package does NOT ship a chat model.  Generation stays on the existing
`core.framework.ai` (OpenRouter) pipeline.  What lives here is the memory and
feedback infrastructure that plugs into that pipeline:

    MemoryService       -- short-term Redis turns + long-term Postgres facts
                           + episodic summaries (tables: disco_facts,
                           disco_episodes; migration 0123_disco_ai.sql)
    ToolRegistry        -- generic tool-registration helper (decorator API,
                           as_openai_tools() output) that any backend can
                           consume.  Shipped with a default tool set for
                           the Discoin FastAPI surface + memory read/write.
    TrainingLogger      -- append-only capture of every chat turn (table:
                           disco_training_turns) plus 👍/👎 feedback,
                           available for future curation / fine-tune runs
                           without adding a runtime inference dependency.
    DiscoAISettings     -- typed settings loaded from Config (env).

Scope helpers (`user_scope`, `guild_scope`, `lore_scope`) give the caller a
consistent namespacing convention for Postgres rows.
"""
from __future__ import annotations

from ai.config import DiscoAISettings
from ai.memory import (
    Episode,
    Fact,
    MemoryService,
    Turn,
    guild_scope,
    lore_scope,
    user_scope,
)
from ai.tools import ToolRegistry, build_default_registry
from ai.training_logger import TrainingLogger, TrainingTurn

__all__ = [
    "DiscoAISettings",
    "Episode",
    "Fact",
    "MemoryService",
    "ToolRegistry",
    "TrainingLogger",
    "TrainingTurn",
    "Turn",
    "build_default_registry",
    "guild_scope",
    "lore_scope",
    "user_scope",
]
