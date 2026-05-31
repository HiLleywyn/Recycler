"""
services/ai_memory.py  -  AI memory refresh and user context assembly.

Responsibilities
----------------
build_user_context()       - assemble layered trait context + normalized reaction
                             ratios + text memory into a structured prompt block.
refresh_user_memory()      - delta-aware refresh: includes existing memory in the
                             summarization prompt so the AI updates rather than
                             overwrites, preventing summarization drift.
log_tool_activations()     - record which tools a message activated for a user,
                             feeding both legacy tool_memory and the trait engine.
batch_refresh_guild()      - refresh stale memories for all users in a guild.
run_post_message_tasks()   - fire-and-forget background tasks run after every
                             AI chat turn: tone ingest, count-based refresh,
                             behavior-shift refresh (with cooldown), trait prune.

Refresh policy
--------------
  Stale if last_refreshed_at > REFRESH_AFTER_HOURS ago (batch_refresh_guild).
  Also triggered after REFRESH_AFTER_MSGS new messages (count-based).
  Also triggered on behavior shift detection with a 30-min cooldown.
  After refresh, conversation rows beyond KEEP_AFTER_REFRESH are pruned.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import re as _re
import time

log = logging.getLogger(__name__)

# How many hours before a memory is considered stale
REFRESH_AFTER_HOURS = 4
# How many conversation messages between count-based refresh triggers
REFRESH_AFTER_MSGS = 40
# How many conversation rows to keep after pruning
KEEP_AFTER_REFRESH = 40
# Max stored memory string length
_MEMORY_MAX_CHARS = 1600
# Minimum seconds between shift-triggered refreshes for the same (uid, gid).
# Count-based refreshes ignore this.
_SHIFT_REFRESH_COOLDOWN = 1800

# Module-level state for the shift-refresh cooldown. Previously lived on the
# Chat cog; moved here so every entrypoint (,ask / reply / mention) can share
# a single cooldown instead of each path tracking its own.
_last_shift_refresh: dict[tuple[int, int], float] = {}

# Passive-learning bookkeeping. Tracks the last time we ran the extraction
# LLM call for each user, and a per-user turn counter so we only extract
# every Nth turn. These dicts are pruned alongside _last_shift_refresh by
# ``prune_shift_cooldown`` (extended in this module).
_last_passive_extract: dict[int, float] = {}
_passive_turn_counter: dict[int, int] = {}

# Cap on per-user passive traits. Prevents a single user with very chatty
# behavior from accumulating an unbounded fact list. The trait prune step
# (services/ai_traits.prune_user_traits) already enforces per-layer caps
# but doesn't know about source; this is an extra ceiling specifically
# for the passive-chat source.
_MAX_PASSIVE_TRAITS_PER_USER = 200

# Trait keys must look like `<event_type>.<subtype>` per the existing
# pipeline. Sanitize the LLM output to avoid SQL surprises; we keep
# letters, digits, dots, underscores and dashes.
_TRAIT_KEY_RE = _re.compile(r"^[a-z0-9][a-z0-9_\-.]{0,63}$")


async def build_user_context(db, user_id: int, guild_id: int, display_name: str = "") -> str:
    """Assemble a context block for injection into the AI system prompt.

    Pulls (in parallel):
      - Persistent text memory summary
      - Layered traits (stable / volatile / interaction) from ai_user_traits
      - Top reaction categories from ai_reaction_memory for normalized ratios
      - Active buddy summary (species, level, mood, battle record) so the
        AI knows whether the user has a buddy and can reference it naturally

    Returns a structured USER PROFILE block, or empty string if nothing is known.
    """
    from services.ai_traits import build_trait_context, build_reaction_ratios

    try:
        memory, reaction_rows, all_traits, buddy_block = await asyncio.gather(
            db.get_ai_user_memory(user_id, guild_id),
            db.get_ai_reaction_memory(user_id, guild_id, limit=8),
            db.get_ai_traits(user_id, guild_id, min_confidence=0.1, limit=30),
            _build_buddy_block(db, user_id, guild_id),
        )
    except Exception:
        log.debug("build_user_context: DB fetch failed uid=%s gid=%s", user_id, guild_id)
        return ""

    # Split traits by layer
    stable = [t for t in all_traits if t["layer"] == "stable"]
    volatile = [t for t in all_traits if t["layer"] == "volatile"]
    interaction = [t for t in all_traits if t["layer"] == "interaction"]

    parts: list[str] = []
    name_label = display_name or f"<@{user_id}>"

    # Structured trait block (takes precedence when available)
    trait_block = build_trait_context(stable, volatile, interaction)
    if trait_block:
        parts.append(trait_block)

    # Reaction ratios (normalized, not raw counts)
    rx_ratios = build_reaction_ratios(reaction_rows)
    if rx_ratios:
        parts.append(f"Reaction Ratios: {rx_ratios}")

    # Persistent text memory as a grounding summary
    if memory:
        parts.append(f"Memory ({name_label}): {memory[:_MEMORY_MAX_CHARS]}")

    if buddy_block:
        parts.append(buddy_block)

    return "\n".join(parts)


async def _build_buddy_block(db, user_id: int, guild_id: int) -> str:
    """Short ACTIVE BUDDY line describing the user's current pet, if any.

    Safe: any DB failure (e.g. buddy schema not deployed) returns ''.
    Returns a one-line summary the AI can reference naturally when the
    user brings up their buddy, or when the conversation drifts toward
    pets / the `,buddy` command family.
    """
    try:
        row = await db.fetch_one(
            "SELECT name, species, level, hunger, happiness, energy, "
            "       wins, losses, battle_count, rarity_tier "
            "FROM cc_buddies "
            "WHERE guild_id = $1 AND owner_user_id = $2 "
            "  AND status = 'owned' AND is_active "
            "LIMIT 1",
            guild_id, user_id,
        )
        total = await db.fetch_val(
            "SELECT COUNT(*) FROM cc_buddies "
            "WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned'",
            guild_id, user_id,
        )
    except Exception:
        return ""

    if not row:
        return ""

    name = str(row.get("name") or "?")
    species = str(row.get("species") or "?")
    lvl = int(row.get("level") or 1)
    hunger = int(row.get("hunger") or 0)
    happy = int(row.get("happiness") or 0)
    energy = int(row.get("energy") or 0)
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    fought = int(row.get("battle_count") or (wins + losses))
    count = int(total or 1)

    record = f", battle record {wins}-{losses} ({fought} fought)" if fought else ""
    collection = f", {count} buddy/buddies in their collection" if count > 1 else ""
    return (
        f"ACTIVE BUDDY: {name} the {species}, Lv. {lvl}, "
        f"hunger {hunger}/100, happiness {happy}/100, energy {energy}/100"
        f"{record}{collection}"
    )


async def refresh_user_memory(
    db,
    user_id: int,
    guild_id: int,
    ai_complete_fn,
    display_name: str = "",
) -> str | None:
    """Delta-aware summarization of recent conversations into a memory entry.

    Unlike a naive overwrite, this prompt includes the EXISTING memory so the
    AI can merge new insights without discarding what was already known. This
    prevents summarization drift: stable traits don't evaporate on each refresh.

    Returns the updated memory string, or None if nothing changed or the call
    failed.
    """
    try:
        history, existing_memory = await asyncio.gather(
            db.get_ai_conversation(user_id, guild_id, limit=60),
            db.get_ai_user_memory(user_id, guild_id),
        )
    except Exception:
        log.warning("refresh_user_memory: failed to load history uid=%s", user_id)
        return None

    if not history:
        return None

    # Build a compact conversation digest
    lines: list[str] = []
    for msg in history[-40:]:
        role = msg.get("role", "user")
        content = str(msg.get("content", ""))[:300]
        label = "Player" if role == "user" else "Bot"
        lines.append(f"{label}: {content}")
    digest = "\n".join(lines)

    name_label = display_name or "this player"

    # Include existing memory so the AI updates rather than overwrites
    existing_section = ""
    if existing_memory and existing_memory.strip():
        existing_section = (
            f"\nExisting memory (keep what's still true, update what changed):\n"
            f"{existing_memory[:800]}\n"
        )

    prompt = (
        f"Update what you know about {name_label} based on recent conversation.\n"
        f"Write max 2 sentences. Focus on: game interests, tone/style, "
        f"recurring questions, notable behavior. Second person only ('They mine a lot').\n"
        f"Merge with existing knowledge - do not drop confirmed facts.\n"
        f"If nothing meaningful changed, reply: NONE\n"
        f"{existing_section}\n"
        f"Recent conversation:\n{digest}"
    )

    try:
        result = await ai_complete_fn(
            [{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.3,
        )
    except Exception:
        log.warning("refresh_user_memory: AI call failed uid=%s", user_id)
        return None

    if not result or result.strip().upper() == "NONE":
        return None

    new_memory = result.strip()[:_MEMORY_MAX_CHARS]

    # Skip write if the new memory is nearly identical to the existing one
    # (prevents unnecessary DB churn and timestamp bumps)
    if existing_memory and _similarity_ratio(new_memory, existing_memory) > 0.85:
        log.debug("refresh_user_memory: no meaningful delta, skipping write uid=%s", user_id)
        return None

    try:
        await db.set_ai_user_memory(user_id, guild_id, new_memory)
        pruned = await db.prune_ai_conversations(user_id, guild_id, keep=KEEP_AFTER_REFRESH)
        log.debug(
            "Memory refreshed uid=%s gid=%s pruned=%d: %.80s",
            user_id, guild_id, pruned, new_memory,
        )
    except Exception:
        log.warning("refresh_user_memory: DB write failed uid=%s", user_id, exc_info=True)
        return None

    return new_memory


def _similarity_ratio(a: str, b: str) -> float:
    """Rough token-overlap similarity between two strings (0.0 to 1.0).

    Uses word-set Jaccard similarity. Quick and allocation-cheap.
    """
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union else 0.0


async def log_tool_activations(db, user_id: int, guild_id: int, tool_keys: list[str]) -> None:
    """Record tool activations for a user in both the legacy and trait systems.

    Fire-and-forget - errors are suppressed so this never blocks the response path.
    """
    from services.ai_traits import ingest_tool_use

    for key in tool_keys:
        try:
            await db.log_ai_tool_use(user_id, guild_id, key)
        except Exception:
            log.debug("log_tool_activations: log_ai_tool_use failed tool=%s uid=%s", key, user_id)
        try:
            await ingest_tool_use(db, user_id, guild_id, key)
        except Exception:
            log.debug("log_tool_activations: ingest_tool_use failed tool=%s uid=%s", key, user_id)


async def batch_refresh_guild(
    db,
    guild_id: int,
    ai_complete_fn,
    stale_hours: int = REFRESH_AFTER_HOURS,
) -> int:
    """Refresh stale memories for all users in a guild who need it.

    Returns the count of users whose memory was successfully refreshed.
    """
    try:
        rows = await db.get_users_needing_memory_refresh(
            guild_id, stale_hours=stale_hours, limit=50
        )
    except Exception:
        log.debug("batch_refresh_guild: could not fetch stale users gid=%s", guild_id)
        return 0

    refreshed = 0
    for row in rows:
        uid = row["user_id"]
        try:
            result = await refresh_user_memory(db, uid, guild_id, ai_complete_fn)
            if result:
                refreshed += 1
        except Exception:
            log.debug("batch_refresh_guild: failed for uid=%s", uid, exc_info=True)

    if refreshed:
        log.info("batch_refresh_guild: refreshed %d memories gid=%s", refreshed, guild_id)
    return refreshed


async def run_post_message_tasks(
    db,
    *,
    user_id: int,
    guild_id: int,
    display_name: str,
    content: str,
    ai_complete_fn,
    assistant_reply: str = "",
) -> None:
    """Background tasks fired after every AI chat turn (non-blocking).

    Runs tone ingest, count-based memory refresh, behavior-shift memory
    refresh (with module-level cooldown), low-signal trait pruning, and
    (when ``Config.AI_AUTO_LEARN_ENABLED`` is true and the per-user rate
    limit allows) passive trait extraction from the (user_msg,
    assistant_reply) pair. All errors are swallowed so the caller's
    response path is never blocked by a background-task failure.

    Previously lived as a private method on the Chat cog. Lifted into the
    service layer so the ``,ask`` command, help-cog reply handler, and
    mention handler can all run the same post-turn housekeeping without
    duplicating state (the shift cooldown map is module-level, shared by
    every caller).

    ``assistant_reply`` is optional so older callers that don't have the
    reply text on hand still work; passive extraction is simply skipped
    in that case.
    """
    from core.config import Config
    from services.ai_traits import ingest_message_tone, prune_user_traits

    # The opt-out check is cheap and shared with the passive-extract path
    # below; cache it so we don't hit the DB twice for the same turn.
    try:
        opted_out = await db.is_ai_opted_out(user_id, guild_id)
    except Exception:
        opted_out = False

    try:
        await ingest_message_tone(db, user_id, guild_id, content)
    except Exception:
        log.debug("run_post_message_tasks: ingest_message_tone failed uid=%s", user_id)

    try:
        count = await db.get_ai_conversation_count(user_id, guild_id)
    except Exception:
        return

    count_trigger = count > 0 and count % REFRESH_AFTER_MSGS == 0

    shift_trigger = False
    if not count_trigger:
        key = (user_id, guild_id)
        now = time.time()
        last_shift = _last_shift_refresh.get(key, 0.0)
        if now - last_shift >= _SHIFT_REFRESH_COOLDOWN:
            try:
                from services.ai_traits import detect_behavior_shift
                if await detect_behavior_shift(db, user_id, guild_id):
                    shift_trigger = True
                    _last_shift_refresh[key] = now
                    log.debug("Behavior shift refresh uid=%s gid=%s", user_id, guild_id)
            except Exception:
                pass

    if count_trigger or shift_trigger:
        try:
            await refresh_user_memory(
                db, user_id, guild_id, ai_complete_fn, display_name=display_name
            )
        except Exception:
            log.debug("run_post_message_tasks: refresh failed uid=%s", user_id, exc_info=True)

    if count > 0 and count % 40 == 0:
        try:
            await prune_user_traits(db, user_id, guild_id)
        except Exception:
            log.debug("run_post_message_tasks: prune failed uid=%s", user_id)

    # Passive trait extraction. Gated behind the global flag and a
    # per-user rate limit so we never spam an LLM call on every message
    # in a busy guild. Skipped entirely for opted-out users.
    if (
        not opted_out
        and bool(getattr(Config, "AI_AUTO_LEARN_ENABLED", False))
        and assistant_reply
        and _passive_extract_due(user_id)
    ):
        try:
            await extract_turn_signals(
                db, user_id, guild_id, content, assistant_reply, ai_complete_fn,
            )
        except Exception:
            log.debug(
                "run_post_message_tasks: extract_turn_signals failed uid=%s",
                user_id, exc_info=True,
            )


def _passive_extract_due(user_id: int) -> bool:
    """Return True iff the per-user rate limit lets us run extraction.

    Two gates:

      * Every ``AI_AUTO_LEARN_EVERY_N_TURNS`` messages from a given user
        triggers the call -- the rest are skipped to keep token cost
        proportional to actual learning value.
      * A floor of ``AI_AUTO_LEARN_MIN_INTERVAL_S`` seconds between
        extractions for the same user guarantees the LLM call rate stays
        bounded even if a user is hyper-active.

    Both counters are in-memory; a bot restart resets them which is fine
    since the worst case is we run one extra extraction per user.
    """
    from core.config import Config

    every = max(1, int(getattr(Config, "AI_AUTO_LEARN_EVERY_N_TURNS", 3)))
    n = _passive_turn_counter.get(user_id, 0) + 1
    _passive_turn_counter[user_id] = n
    if n % every != 0:
        return False
    min_gap = float(getattr(Config, "AI_AUTO_LEARN_MIN_INTERVAL_S", 600))
    now = time.time()
    last = _last_passive_extract.get(user_id, 0.0)
    if (now - last) < min_gap:
        return False
    _last_passive_extract[user_id] = now
    return True


async def extract_turn_signals(
    db,
    user_id: int,
    guild_id: int,
    user_msg: str,
    assistant_msg: str,
    ai_complete_fn,
) -> int:
    """Pull candidate trait signals from one chat turn and upsert them.

    Runs a cheap LLM call against the (user_msg, assistant_msg) pair and
    expects a JSON array of ``{key, value, polarity, weight}`` entries.
    Each entry is upserted into ``ai_user_traits`` with ``source=
    "passive_chat"`` and a confidence seed of 0.3 -- the existing decay
    math purges one-shot misreads within days while repeated signals
    promote to ``stable`` through the standard sample_size threshold.

    Returns the number of signals successfully upserted. Skips entirely
    if either side is shorter than 20 chars (no useful signal), if the
    Ollama queue already has user-facing waiters (don't steal capacity
    from chat), or if the user is already at the per-user passive cap.
    """
    from core.framework.ai.client import chat_queue
    from services.ai_traits import _ingest_signal

    if len(user_msg) < 20 or len(assistant_msg) < 20:
        return 0

    # Don't push background extraction load when chat is already waiting
    # for Ollama -- it would steal from the same queue lane.
    try:
        ollama_stats = chat_queue.stats("ollama")
        if ollama_stats and ollama_stats[0].waiting > 0:
            return 0
    except Exception:
        pass

    # Per-user passive-trait cap.
    try:
        existing = await db.fetch_val(
            "SELECT COUNT(*) FROM ai_user_traits "
            "WHERE user_id=$1 AND guild_id=$2 AND source='passive_chat'",
            user_id, guild_id,
        )
        if int(existing or 0) >= _MAX_PASSIVE_TRAITS_PER_USER:
            return 0
    except Exception:
        # If the count fails we err on the side of writing (caller's
        # bounded retry + decay will keep things sane).
        pass

    prompt = (
        "Extract up to 5 STABLE preferences or traits about the USER from "
        "the conversation snippet below. Output ONLY a JSON array; no prose.\n\n"
        "Each item: {\"key\": \"<lowercase.snake.case>\", \"polarity\": "
        "\"positive|negative|neutral\", \"weight\": <0.3-1.5>}\n\n"
        "Skip surface-level mood; capture LASTING traits (preferences, "
        "play style, interests, recurring intents). Keys must be 3-32 "
        "chars, alphanum + dot + underscore + dash. Examples: "
        "\"interest.defi\", \"style.terse\", \"preference.no_emoji\".\n\n"
        f"USER: {user_msg[:600]}\n"
        f"ASSISTANT: {assistant_msg[:600]}\n\n"
        "JSON array (no commentary):"
    )
    try:
        result = await ai_complete_fn(
            [{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.2,
        )
    except Exception:
        return 0

    if not result:
        return 0

    # Lift the first JSON array out of the model response so a chatty
    # model that adds explanatory prose around the JSON doesn't trip us.
    text = result.strip()
    if text.startswith("```"):
        text = text.strip("`")
        # Drop a leading "json" language tag if present.
        nl = text.find("\n")
        if nl > 0 and text[:nl].strip().lower() in {"json", ""}:
            text = text[nl + 1:]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return 0
    try:
        entries = _json.loads(text[start:end + 1])
    except Exception:
        return 0
    if not isinstance(entries, list):
        return 0

    written = 0
    for entry in entries[:8]:
        if not isinstance(entry, dict):
            continue
        raw_key = str(entry.get("key") or "").strip().lower()
        if not raw_key or not _TRAIT_KEY_RE.match(raw_key):
            continue
        # Split into event_type / subtype so the existing
        # _ingest_signal path works unchanged. If the key has no dot,
        # bucket it under "passive.<subtype>".
        if "." in raw_key:
            event_type, _, subtype = raw_key.partition(".")
        else:
            event_type, subtype = "passive", raw_key
        polarity = str(entry.get("polarity") or "neutral").lower()
        try:
            weight = max(0.3, min(1.5, float(entry.get("weight") or 0.6)))
        except (TypeError, ValueError):
            weight = 0.6
        # Negative polarity dampens the signal so contradicting evidence
        # cancels out instead of accumulating in the same trait.
        if polarity == "negative":
            weight *= -0.5
            if weight == 0:
                continue
        try:
            await _ingest_signal(
                db, user_id, guild_id, event_type, subtype,
                source="passive_chat",
                confidence_seed=0.3,
                signal_weight_override=weight,
            )
            written += 1
        except Exception:
            log.debug(
                "extract_turn_signals: ingest failed uid=%s key=%s",
                user_id, raw_key,
            )
    if written:
        log.debug(
            "extract_turn_signals: %d signals upserted uid=%s gid=%s",
            written, user_id, guild_id,
        )
    return written


def prune_shift_cooldown(max_age: float = _SHIFT_REFRESH_COOLDOWN) -> None:
    """Evict stale shift-refresh + passive-extract cooldown entries.

    Called from the Chat cog's background refresh loop to keep the
    module-level dicts bounded. Entries older than ``max_age`` seconds
    are dropped from both the shift-refresh and passive-extract caches.
    The passive turn counter doesn't have a timestamp so it's truncated
    when it grows past a soft cap; counters reset cleanly because
    extraction triggers are modular.
    """
    now = time.time()
    stale = [k for k, v in _last_shift_refresh.items() if now - v >= max_age]
    for k in stale:
        _last_shift_refresh.pop(k, None)
    stale_passive = [
        uid for uid, v in _last_passive_extract.items() if now - v >= max_age
    ]
    for uid in stale_passive:
        _last_passive_extract.pop(uid, None)
    # Cap the turn counter dict to prevent unbounded growth on a long-
    # running bot. Drop the oldest entries (by insertion order, which is
    # essentially LRU under dict's ordering guarantee).
    if len(_passive_turn_counter) > 10000:
        for uid in list(_passive_turn_counter.keys())[:5000]:
            _passive_turn_counter.pop(uid, None)
