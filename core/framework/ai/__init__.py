"""Modular AI integration for Discoin."""
from .client import close_client, complete, complete_default, complete_tools
from .models import (
    Category,
    ModelOption,
    TOOL_CATEGORIES,
    all_categories,
    catalog_for,
    category,
    clear_guild_default,
    get_guild_default,
    is_vision_capable_slug,
    list_guild_defaults,
    resolve_model,
    set_guild_default,
)
from .safety import is_injection_attempt, looks_like_acrostic, sanitize_context_snippet, sanitize_input, sanitize_output, strip_links
from .quota import cancel_ai_quota_reservation, check_ai_quota, reserve_ai_quota, reset_ai_quota_state

__all__ = [
    "Category",
    "ModelOption",
    "TOOL_CATEGORIES",
    "all_categories",
    "cancel_ai_quota_reservation",
    "catalog_for",
    "category",
    "check_ai_quota",
    "clear_guild_default",
    "close_client",
    "complete",
    "complete_default",
    "complete_tools",
    "get_guild_default",
    "is_injection_attempt",
    "is_vision_capable_slug",
    "list_guild_defaults",
    "looks_like_acrostic",
    "reset_ai_quota_state",
    "reserve_ai_quota",
    "resolve_model",
    "sanitize_context_snippet",
    "sanitize_input",
    "sanitize_output",
    "set_guild_default",
    "strip_links",
]
