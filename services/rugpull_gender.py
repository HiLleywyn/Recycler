"""Gender inference for the King/Queen of Rugs minigame.

A player wins the rug pull -> we need to decide whether to award the
``RUGPULL_ROLE_ID`` (King of Rugs) or ``RUGPULL_QUEEN_ROLE_ID`` (Queen of Rugs).

Resolution order, in this order, stopping at the first hit:

1. Manual override stored in ``rugpull_gender`` with ``source='manual'``
   (set via ``,ruggender`` -- the player's word always wins).
2. Cached auto-detect stored in ``rugpull_gender`` with ``source='auto'``
   (we don't want to spam the AI on every rugpull).
3. Heuristic: Discord display-name / username -- a small hand-maintained list
   of overtly gendered tokens / pronouns catches the easy cases without any
   API cost.
4. AI fallback (OpenRouter via ``core.framework.ai.complete``). We send the
   member's display name, username, optionally pronouns scraped from their
   bio if available, and ask for one of ``male`` / ``female`` / ``unknown``.
   Anything that isn't a confident male/female read defaults to **male**
   (the role behaviour mirrors the legacy King of Rugs and the user has
   only configured two role slots).

The cached result is written back to the DB so subsequent reigns reuse it.
"""
from __future__ import annotations

import logging
import re

import discord

from core.config import Config
from core.framework.ai import complete as ai_complete


log = logging.getLogger(__name__)


# Gender bucket the rug minigame supports.
Gender = str  # 'male' | 'female'


# ── Heuristic dictionaries ───────────────────────────────────────────────────
# Tokens that strongly imply a gender when they appear as a whole word inside
# a display name or username. Kept intentionally small -- false positives are
# worse than misses here because the player can always /ruggender themselves.

_FEMALE_TOKENS: frozenset[str] = frozenset({
    "she", "her", "hers", "queen", "princess", "lady", "girl", "girly",
    "miss", "mrs", "ms", "madame", "madam", "mama", "mom", "mommy",
    "sister", "sis", "witch", "fairy", "goddess", "diva", "duchess",
    "empress", "femme", "female",
})

_MALE_TOKENS: frozenset[str] = frozenset({
    "he", "him", "his", "king", "prince", "lord", "boy", "guy", "dude",
    "mr", "sir", "papa", "dad", "daddy", "brother", "bro", "wizard",
    "god", "duke", "emperor", "male", "man",
})

_TOKEN_SPLIT_RE = re.compile(r"[^a-z]+")


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT_RE.split(text.lower()) if t]


def _heuristic_guess(*names: str) -> Gender | None:
    """Return 'male'/'female' if any name contains a strong gender token, else None."""
    female_hits = 0
    male_hits = 0
    for name in names:
        if not name:
            continue
        for tok in _tokenize(name):
            if tok in _FEMALE_TOKENS:
                female_hits += 1
            elif tok in _MALE_TOKENS:
                male_hits += 1
    if female_hits > male_hits:
        return "female"
    if male_hits > female_hits:
        return "male"
    return None


# ── DB helpers ───────────────────────────────────────────────────────────────

async def _fetch_stored(db, user_id: int, guild_id: int) -> dict | None:
    return await db.fetch_one(
        "SELECT gender, source FROM rugpull_gender WHERE user_id=$1 AND guild_id=$2",
        user_id, guild_id,
    )


async def _store(db, user_id: int, guild_id: int, gender: Gender, source: str) -> None:
    await db.execute(
        """INSERT INTO rugpull_gender (user_id, guild_id, gender, source, updated_at)
           VALUES ($1, $2, $3, $4, now())
           ON CONFLICT (user_id, guild_id) DO UPDATE SET
               gender     = excluded.gender,
               source     = excluded.source,
               updated_at = now()""",
        user_id, guild_id, gender, source,
    )


# ── AI fallback ──────────────────────────────────────────────────────────────

_AI_SYSTEM = (
    "You classify a Discord user's likely gender from public profile signals "
    "(display name, username, pronouns). Reply with exactly one lowercase "
    "word from this set: male, female, unknown. No punctuation, no quotes, "
    "no explanation. Pick 'unknown' if the signals are ambiguous."
)


async def _ai_guess(display_name: str, username: str) -> Gender | None:
    """Ask the AI to classify, return 'male'/'female' or None on failure/ambiguous."""
    if not Config.OPENROUTER_API_KEY:
        return None
    prompt = (
        f"Display name: {display_name or '(none)'}\n"
        f"Username: {username or '(none)'}\n"
        "Classify."
    )
    try:
        reply = await ai_complete(
            [
                {"role": "system", "content": _AI_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4,
            temperature=0.0,
            kind="system",
        )
    except Exception:
        log.exception("rugpull_gender AI lookup failed")
        return None
    if not reply:
        return None
    cleaned = reply.strip().lower().strip(".,!?\"'")
    if cleaned in ("male", "female"):
        return cleaned
    return None


# ── Public API ───────────────────────────────────────────────────────────────

async def resolve_gender(
    db,
    member: discord.Member,
    guild_id: int,
    *,
    allow_ai: bool = True,
) -> Gender:
    """Resolve a Discord member to ``'male'`` or ``'female'``.

    Looks up manual overrides, then the auto-detect cache, then runs the
    heuristic, and finally falls back to AI. The result is cached back to
    ``rugpull_gender`` so the next call is a single SELECT. Defaults to
    ``'male'`` when nothing else produces a confident answer (the King of
    Rugs role is the legacy behaviour).
    """
    stored = await _fetch_stored(db, member.id, guild_id)
    if stored:
        return stored["gender"]

    guess = _heuristic_guess(member.display_name, member.name)
    if guess is not None:
        await _store(db, member.id, guild_id, guess, "auto")
        return guess

    if allow_ai:
        ai = await _ai_guess(member.display_name, member.name)
        if ai is not None:
            await _store(db, member.id, guild_id, ai, "auto")
            return ai

    # Default fall-through: legacy King of Rugs behaviour.
    await _store(db, member.id, guild_id, "male", "auto")
    return "male"


async def set_manual_gender(
    db,
    user_id: int,
    guild_id: int,
    gender: Gender,
) -> None:
    """Pin a player's gender (manual override that beats every auto-detect)."""
    if gender not in ("male", "female"):
        raise ValueError(f"gender must be 'male' or 'female', got {gender!r}")
    await _store(db, user_id, guild_id, gender, "manual")


async def get_stored_gender(db, user_id: int, guild_id: int) -> dict | None:
    """Return the cached row (``{'gender': ..., 'source': ...}``) or ``None``."""
    return await _fetch_stored(db, user_id, guild_id)


def monarch_role_id(gender: Gender) -> int:
    """Return the configured monarch role id for a gender, falling back to King."""
    if gender == "female" and Config.RUGPULL_QUEEN_ROLE_ID:
        return Config.RUGPULL_QUEEN_ROLE_ID
    return Config.RUGPULL_ROLE_ID


def monarch_role_ids() -> tuple[int, ...]:
    """Return all configured monarch role IDs (zero-filtered) -- King + Queen."""
    return tuple(
        rid for rid in (Config.RUGPULL_ROLE_ID, Config.RUGPULL_QUEEN_ROLE_ID) if rid
    )


def monarch_title(gender: Gender) -> str:
    """Return ``'Queen of Rugs'`` or ``'King of Rugs'`` for the given gender."""
    if gender == "female":
        return "Queen of Rugs"
    return "King of Rugs"


def monarch_pronoun(gender: Gender, case: str = "subject") -> str:
    """Return the pronoun for a given gender and grammatical case."""
    if gender == "female":
        return {"subject": "she", "object": "her", "possessive": "her"}.get(case, "she")
    return {"subject": "he", "object": "him", "possessive": "his"}.get(case, "he")
