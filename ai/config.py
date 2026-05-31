"""DiscoAI typed settings.

Pared-down version of the original backend settings: we no longer ship a
local inference path, so everything related to model selection / dtype /
HF cache / adapter dir is gone.  What's left governs the memory sidecar
(short-term Redis turns, rate limiting, passive-learning toggle) and the
internal API endpoint the tool handlers call back into.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class DiscoAISettings(BaseModel):
    """Runtime settings for the DiscoAI memory + tool sidecar."""

    short_term_turns: int = Field(12, ge=1, le=64)
    short_term_ttl_s: int = Field(3600, ge=60, le=86400)
    passive_learning: bool = Field(False)
    rate_limit_per_user_per_min: int = Field(8, ge=1, le=120)
    api_base_url: str = Field("http://127.0.0.1:8080", description="Discoin FastAPI base URL")

    @classmethod
    def from_config(cls) -> "DiscoAISettings":
        """Build settings from the global Config object."""
        from core.config import Config

        return cls(
            short_term_turns=Config.DISCOAI_SHORT_TERM_TURNS,
            short_term_ttl_s=Config.DISCOAI_SHORT_TERM_TTL_S,
            passive_learning=Config.DISCOAI_PASSIVE_LEARNING,
            rate_limit_per_user_per_min=Config.DISCOAI_RATE_LIMIT_PER_USER_PER_MIN,
            api_base_url=Config.DISCOAI_API_BASE_URL,
        )
