"""
core/framework/emoji_index.py -- per-guild custom emoji meaning indexer.

The chat system prompt previously only showed custom emojis with a static
tone tag derived from substring-matching the emoji name (see
core/framework/emoji_context.py :: SERVER_EMOJI_HINTS). That's too shallow for
the model to catch the nuance of a particular server's emoji palette, so
it ends up giving generic explanations.

This module produces a richer per-emoji description by combining:

  1. A vision pass on the emoji image itself (expression, colours, subject).
  2. Recent usage snippets -- short text samples around each time the emoji
     was used in chat, pulled from guild_emoji_usage.
  3. A short synthesis pass that folds both signals into a single ~30 word
     description plus a tone category.

The result is stored in guild_emoji_meanings (see migration 0107) and
preferred over the static hint in build_guild_emoji_context().

Callers:
  - cogs/ai.py ,ai emojis index / set       (manual triggers)
  - cogs/ai.py background refresh task      (14-day staleness)
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING

import aiohttp

from core.config import Config
from core.framework.ai import complete as ai_complete
from core.framework.ai import sanitize_output
from core.framework.ai.client import complete_ollama_vision

if TYPE_CHECKING:
    import discord

    from database.database import PgDatabase

log = logging.getLogger(__name__)

# Categories the chat system prompt already understands -- keep in sync with
# core/framework/emoji_context.py so the tone tag rendered alongside the emoji
# stays a known token for the model.
_CATEGORIES = (
    "loss", "win", "hype", "frustration", "laugh", "gg",
    "salt", "shock", "vibe", "sarcasm", "support", "celebrate",
    "degenerate",
)

# Fresh entries (updated less than this many days ago) are skipped when the
# indexer is run without --force. Matches the 14-day refresh cadence.
DEFAULT_MAX_AGE_DAYS = 14

# Vision prompt -- intentionally focused on the "vibe" not a pixel-level
# description. The synthesis step turns this into something usable by the
# chat model.
_VISION_PROMPT = (
    "This is a small custom Discord emoji (usually ~128px). Describe the "
    "expression, pose, or subject and the emotional vibe it conveys in one "
    "or two short sentences. Do not describe individual pixels, colours, or "
    "compression artefacts. Focus on what a human sending this emoji would "
    "be feeling. If the image is a letter, symbol, object, or meme face, "
    "say so plainly."
)

_MAX_USAGE_SAMPLES = 15
_USAGE_WINDOW_DAYS = 30

_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
_MAX_IMAGE_BYTES = 2 * 1024 * 1024  # emojis are tiny; 2 MiB is generous


async def _download_emoji_bytes(url: str) -> tuple[bytes, str] | None:
    """Fetch the emoji PNG/GIF. Returns (bytes, mime) or None on failure."""
    try:
        async with aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT) as sess:
            async with sess.get(
                url, headers={"User-Agent": "Discoin-EmojiIndex/1.0"},
            ) as r:
                if r.status != 200:
                    return None
                mime = (r.headers.get("Content-Type") or "image/png").split(";")[0].strip().lower()
                if mime == "image/jpg":
                    mime = "image/jpeg"
                body = await r.content.read(_MAX_IMAGE_BYTES + 1)
                if not body or len(body) > _MAX_IMAGE_BYTES:
                    return None
                return body, mime
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.info("[emoji_index] download failed for %s: %s", url, exc)
        return None
    except Exception:
        log.exception("[emoji_index] unexpected download error for %s", url)
        return None


async def describe_emoji_image(emoji: "discord.Emoji") -> str | None:
    """Run the vision model against the emoji image.

    Tries the configured vision backend (Ollama by default) first and falls
    back to OpenRouter when Ollama is down or unreachable -- the same
    pattern as ``vision.describe_image``. Returns the description string
    or None if every path failed.
    """
    url = str(getattr(emoji, "url", "") or "")
    if not url:
        return None
    downloaded = await _download_emoji_bytes(url)
    if downloaded is None:
        return None
    body, mime = downloaded
    b64 = base64.b64encode(body).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"

    backend = (Config.VISION_BACKEND or "ollama").lower()
    description: str | None = None

    if backend == "ollama":
        try:
            description = await complete_ollama_vision(
                prompt=_VISION_PROMPT,
                image_data_uri=data_uri,
                max_tokens=120,
            )
        except Exception:
            log.warning(
                "[emoji_index] ollama crashed for :%s:, falling back to OpenRouter",
                getattr(emoji, "name", "?"),
            )

    if not description:
        try:
            fallback_msgs = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ]
            description = await ai_complete(
                fallback_msgs, max_tokens=160, temperature=0.2,
            )
        except Exception:
            log.warning(
                "[emoji_index] OpenRouter vision also failed for :%s:",
                getattr(emoji, "name", "?"),
            )

    return description or None


def _format_usage_block(usage_rows: list[dict]) -> str:
    """Render usage snippets for the synthesis prompt.

    Each row is `{user_id, snippet, ts}` from guild_emoji_usage. The user_id
    is dropped from the prompt -- the model only needs the text context.
    """
    if not usage_rows:
        return "(no recent usage samples available)"
    lines: list[str] = []
    for r in usage_rows[:_MAX_USAGE_SAMPLES]:
        text = str(r.get("snippet") or "").strip()
        if not text:
            continue
        lines.append(f"- {text}")
    if not lines:
        return "(no recent usage samples available)"
    return "\n".join(lines)


def _parse_synthesis(raw: str) -> tuple[str, str | None]:
    """Pull a description + category out of the synthesis model's reply.

    The model is asked to emit two lines:
        CATEGORY: <one of _CATEGORIES or ->
        DESCRIPTION: <one to two sentence blurb>
    Any deviation is tolerated -- worst case we fall back to the whole
    string as the description and leave category NULL.
    """
    category: str | None = None
    description = ""
    for line in raw.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("category:"):
            val = stripped.split(":", 1)[1].strip().strip("`*_").lower()
            if val in _CATEGORIES:
                category = val
        elif lower.startswith("description:"):
            description = stripped.split(":", 1)[1].strip()
    if not description:
        description = raw.strip()
    description = description.strip().strip("`*_")
    # Keep the description compact so the final system-prompt block stays
    # under Discord embed / model context limits even on big palettes.
    if len(description) > 220:
        description = description[:217].rstrip() + "..."
    return description, category


async def _synthesize_meaning(
    emoji_name: str,
    vision_desc: str | None,
    usage_rows: list[dict],
) -> tuple[str, str | None]:
    """Combine vision + usage signal into a final (description, category)."""
    cats = ", ".join(_CATEGORIES)
    vision_block = vision_desc.strip() if vision_desc else "(vision unavailable)"
    usage_block = _format_usage_block(usage_rows)

    prompt = (
        "You are cataloguing a Discord server's custom emoji palette so a "
        "chat AI can pick the right one when replying. Produce a compact "
        "nuanced meaning for ONE emoji based on two signals:\n\n"
        f"EMOJI NAME: :{emoji_name}:\n\n"
        f"VISION DESCRIPTION (what the image shows):\n{vision_block}\n\n"
        f"RECENT USAGE SAMPLES (snippets of messages where this emoji appeared):\n{usage_block}\n\n"
        "Respond with exactly two lines in this format:\n"
        f"CATEGORY: <one of: {cats}, or - if none fit>\n"
        "DESCRIPTION: <one or two sentences, under 200 characters, capturing the vibe, "
        "tone, and how this server seems to use it. Reference the usage pattern when "
        "it's distinctive; otherwise describe the emoji's feeling plainly.>"
    )

    raw = await ai_complete(
        [{"role": "user", "content": prompt}],
        max_tokens=180,
        temperature=0.3,
    )
    if not raw:
        # Fall back to the vision description alone so we still have SOMETHING
        # useful in the DB instead of a blank row.
        fallback = (vision_desc or f"custom server emoji :{emoji_name}:").strip()
        return sanitize_output(fallback)[:220], None
    return _parse_synthesis(sanitize_output(str(raw)))


class _VisionBreaker:
    """Tiny per-batch circuit breaker for the vision backend.

    When the backend is down every emoji produces a failed request, spamming
    logs and wasting network attempts. After a run of consecutive failures
    we flip the breaker and stop calling vision for the remainder of the
    batch -- synthesis falls back to name + usage samples alone, which is
    still better than blocking on a dead endpoint.
    """

    __slots__ = ("_consecutive_fails", "_threshold", "_tripped")

    def __init__(self, threshold: int = 3) -> None:
        self._consecutive_fails = 0
        self._threshold = threshold
        self._tripped = False

    @property
    def tripped(self) -> bool:
        return self._tripped

    def record(self, ok: bool) -> None:
        if ok:
            self._consecutive_fails = 0
            return
        self._consecutive_fails += 1
        if self._consecutive_fails >= self._threshold:
            self._tripped = True


async def index_emoji(
    db: "PgDatabase", guild_id: int, emoji: "discord.Emoji",
    *, source: str = "vision", breaker: _VisionBreaker | None = None,
) -> dict | None:
    """Index one emoji: download, vision, synthesis, upsert.

    Returns the stored row as a dict, or None if indexing failed before a
    row could be written. The function is resilient: a vision failure still
    produces a usage-only synthesis, and a total LLM failure falls back to
    the emoji name as description.
    """
    emoji_id = int(getattr(emoji, "id", 0) or 0)
    emoji_name = str(getattr(emoji, "name", "") or "unknown")
    animated = bool(getattr(emoji, "animated", False))
    if emoji_id <= 0:
        return None

    if breaker is not None and breaker.tripped:
        vision_task = None
    else:
        vision_task = asyncio.create_task(describe_emoji_image(emoji))
    usage_task = asyncio.create_task(
        db.get_recent_emoji_usage(
            guild_id, emoji_id, limit=_MAX_USAGE_SAMPLES, days=_USAGE_WINDOW_DAYS,
        )
    )
    if vision_task is not None:
        vision_desc, usage_rows = await asyncio.gather(vision_task, usage_task)
        if breaker is not None:
            breaker.record(bool(vision_desc))
    else:
        vision_desc = None
        usage_rows = await usage_task

    description, category = await _synthesize_meaning(emoji_name, vision_desc, usage_rows or [])

    await db.upsert_emoji_meaning(
        guild_id, emoji_id, emoji_name, description,
        animated=animated, category=category, source=source,
    )
    return await db.get_emoji_meaning(guild_id, emoji_id)


async def index_guild(
    db: "PgDatabase",
    guild: "discord.Guild",
    *,
    force: bool = False,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    concurrency: int = 3,
) -> dict:
    """Index every custom emoji on a guild.

    ``force=True`` re-indexes every emoji; otherwise entries newer than
    ``max_age_days`` are skipped. Missing emojis (rows in the DB whose
    emoji_id no longer exists on the guild) are deleted so the chat prompt
    doesn't reference broken markup.

    Uses a small semaphore so a server with hundreds of emojis doesn't
    slam the vision backend.
    """
    emojis = [e for e in guild.emojis if getattr(e, "available", True)]
    emoji_ids = {int(e.id) for e in emojis}

    existing = await db.get_all_emoji_meanings(guild.id)
    existing_by_id: dict[int, dict] = {int(r["emoji_id"]): r for r in existing}

    # Remove rows for emojis that no longer exist on the guild.
    pruned = 0
    for row_id in list(existing_by_id.keys()):
        if row_id not in emoji_ids:
            await db.delete_emoji_meaning(guild.id, row_id)
            existing_by_id.pop(row_id, None)
            pruned += 1

    if not force:
        stale_ids = set(await db.get_stale_emoji_meaning_ids(
            guild.id, max_age_days=max_age_days,
        ))
    else:
        stale_ids = emoji_ids

    sem = asyncio.Semaphore(max(1, concurrency))
    breaker = _VisionBreaker(threshold=3)
    indexed = 0
    skipped = 0
    failed = 0

    async def _run(e):
        nonlocal indexed, failed
        async with sem:
            try:
                row = await index_emoji(db, guild.id, e, breaker=breaker)
                if row is None:
                    failed += 1
                else:
                    indexed += 1
            except Exception:
                log.exception("[emoji_index] failed to index :%s:", getattr(e, "name", "?"))
                failed += 1

    tasks: list[asyncio.Task] = []
    for e in emojis:
        eid = int(e.id)
        if not force and eid in existing_by_id and eid not in stale_ids:
            skipped += 1
            continue
        tasks.append(asyncio.create_task(_run(e)))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=False)

    return {
        "total": len(emojis),
        "indexed": indexed,
        "skipped": skipped,
        "failed": failed,
        "pruned": pruned,
        "vision_down": breaker.tripped,
    }


__all__ = (
    "DEFAULT_MAX_AGE_DAYS",
    "describe_emoji_image",
    "index_emoji",
    "index_guild",
)
