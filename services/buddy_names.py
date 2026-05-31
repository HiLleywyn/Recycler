"""
services/buddy_names.py  -  Server-side name generator + validator for CC Buddies.

Names must NEVER be generated client-side (the spec requires server-side
generation to avoid inconsistencies, and to keep sanitization in one place).

Public API:
    generate_name(species, db, guild_id)  -> str          (async)
    validate_rename(name)                 -> (ok, error)
"""
from __future__ import annotations

import logging
import random
import re
from typing import Any

from configs.buddies_config import (
    NAME_MAX_LEN,
    NAME_MIN_LEN,
    NAME_SUFFIX_MAX,
    NAME_SUFFIX_MIN,
    SPECIES,
)
from core.framework.content_filter import sanitize_text

log = logging.getLogger(__name__)

# ASCII printables only. Matches the project's no-unicode-dash rule and
# blocks anything that could mess up embed rendering.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-.']{0,%d}$" % (NAME_MAX_LEN - 1))


async def generate_name(species: str, db: Any, guild_id: int) -> str:
    """Produce a unique-enough, flavor-appropriate name for a freshly hatched buddy.

    Strategy:
      1. Pick the species' name pool and shuffle it.
      2. For each candidate, check if an *active* (non-shelter) buddy in this
         guild already has that name. First miss wins.
      3. If every base name is taken, append a 2-digit suffix and retry until
         one is unused.
      4. Last resort (vanishingly rare): return "<species>-<nnnn>".

    The lookup uses a lowercase compare so "Zenny" and "zenny" collide.
    """
    meta = SPECIES.get(species)
    if meta is None:
        # Caller passed an unknown species -- degrade gracefully.
        return f"{species[:12]}-{random.randint(10, 99)}"

    pool: list[str] = list(meta.get("name_pool") or [species.title()])
    random.shuffle(pool)

    # Base pass.
    for candidate in pool:
        if await _is_name_free(db, guild_id, candidate):
            return candidate

    # Suffix pass (up to a few hundred tries total; suffix pool is 90 wide).
    for candidate in pool:
        for _ in range(12):
            suffix = random.randint(NAME_SUFFIX_MIN, NAME_SUFFIX_MAX)
            proposed = f"{candidate}-{suffix}"
            if len(proposed) > NAME_MAX_LEN:
                proposed = proposed[:NAME_MAX_LEN]
            if await _is_name_free(db, guild_id, proposed):
                return proposed

    # Cosmically unlucky fallback.
    return f"{species[:10]}-{random.randint(1000, 9999)}"


async def _is_name_free(db: Any, guild_id: int, name: str) -> bool:
    try:
        row = await db.fetch_one(
            "SELECT 1 FROM cc_buddies "
            "WHERE guild_id = $1 AND status = 'owned' AND LOWER(name) = LOWER($2) "
            "LIMIT 1",
            guild_id, name,
        )
    except Exception:
        log.debug("_is_name_free: lookup failed gid=%s name=%s", guild_id, name, exc_info=True)
        # Treat DB errors as "free" -- better to hatch with a possibly-duplicate
        # name than to loop forever.
        return True
    return row is None


def validate_rename(name: str) -> tuple[bool, str]:
    """Validate a user-supplied new name.

    Returns (ok, error_message). ``error_message`` is empty on success.
    """
    if name is None:
        return False, "Name cannot be empty."
    stripped = name.strip()
    if not stripped:
        return False, "Name cannot be empty."

    cleaned = sanitize_text(stripped)
    if cleaned != stripped:
        return False, "Name cannot contain URLs, invite links, or @mentions."

    if len(cleaned) < NAME_MIN_LEN:
        return False, f"Name must be at least {NAME_MIN_LEN} character."
    if len(cleaned) > NAME_MAX_LEN:
        return False, f"Name must be {NAME_MAX_LEN} characters or fewer."

    if not _NAME_RE.match(cleaned):
        return (
            False,
            "Name must start with a letter or digit and use only "
            "letters, digits, spaces, underscores, hyphens, periods, or apostrophes.",
        )
    return True, ""


def pick_hatch_species() -> str:
    """Weighted-random species pick for a fresh hatch."""
    keys: list[str] = list(SPECIES.keys())
    weights: list[int] = [int(SPECIES[k].get("weight", 1)) for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]
