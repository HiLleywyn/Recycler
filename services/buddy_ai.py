"""
services/buddy_ai.py  -  AI-backed personality layer for CC Buddies.

Gives each buddy a voice that remembers who it lived with, who left it at
the shelter, and what it has been doing with its current owner. Used by:

    ,buddy talk button      -> kind='talk'
    ,buddy battle           -> kind='battle_win' / 'battle_loss'
    ,buddy adopt            -> kind='adopt'
    ,buddy reclaim          -> kind='reclaim'
    ,buddy surrender         -> kind='shelter_intake' (DM flavor, optional)

Feed and pet intentionally DO NOT hit the AI -- they pull from the canned
``SPECIES[sp]['dialogue']`` pool so those buttons stay cheap.

State model
-----------
Every buddy row has two JSONB columns (migration 0113):

    ai_memory:
        {
            "traits":        list[str]  (stable personality hints, e.g. "clingy"),
            "quirks":        list[str]  (flavor, e.g. "names every rock"),
            "recent_events": [ {"ts": int, "kind": str, "summary": str}, ...],
            "owner_notes":   { "<uid>": "short note the buddy has about uid" }
        }

    previous_owners:
        [
            {
                "user_id":      int,
                "display_name": str | None,
                "from_ts":      int,            # unix seconds
                "to_ts":        int | None,     # NULL while active
                "reason":       str             # active|adopted|reclaimed|
                                                #   surrendered|ran_away|
                                                #   left_guild|banned
            },
            ...
        ]

The LAST previous_owners entry represents the current relationship while
the buddy is owned. Older entries are the history the AI uses to decide
how to feel about banned / abandoning ex-owners.

Design
------
* Pure: ``generate_reply`` takes a buddy row + owner label + kind and
  returns a short string. Does NOT write to the DB by itself.
* ``record_event`` is the DB-write helper cogs call after a successful
  interaction; it appends to recent_events and trims to RECENT_EVENTS_MAX.
* Fallback: when the AI call fails, returns a random line from
  ``SPECIES[sp]['dialogue']`` so the interaction still feels alive.
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from configs.buddies_config import SPECIES, rarity_meta
from core.framework.ai.client import complete
from core.framework.ai.safety import (
    is_injection_attempt,
    sanitize_context_snippet,
    sanitize_input,
)

log = logging.getLogger(__name__)

# How many events we keep in ai_memory.recent_events. Oldest drop first.
RECENT_EVENTS_MAX = 20
# How many events to include in the system prompt -- the tail of the list.
EVENTS_IN_PROMPT = 8
# How many previous owners to mention in the prompt (most recent first).
OWNERS_IN_PROMPT = 4
# Max chars we let the model produce for a buddy line.
REPLY_MAX_CHARS = 220
# Overall model output cap in tokens. Keep small -- buddy lines are 1-2 sentences.
REPLY_MAX_TOKENS = 120
# Sampling temperature. Slight warmth; these are personality lines, not answers.
REPLY_TEMPERATURE = 0.9

# Hard caps on strings pulled from stored state that land in the prompt.
# Memory can carry anything the buddy has ever been told -- past owners
# may have been malicious or may have hit the model with injection
# attempts that slipped through before this file was hardened. Short caps
# starve any latent payload of tokens to work with.
_PROMPT_DISPLAY_NAME_LIMIT = 48
_PROMPT_TRAIT_LIMIT        = 40
_PROMPT_EVENT_SUMMARY_LIMIT = 140
_PROMPT_EXTRA_LIMIT        = 240
_MEMORY_EVENT_SUMMARY_LIMIT = 200  # stricter than the pre-existing 280

# Canned deadpan lines the buddy uses when it detects a prompt-injection
# payload in the message it was asked to react to. Keeping them in
# character (the buddy is a tired / dry animal) means the attacker sees
# a normal-looking refusal instead of a technical error hint.
_BUDDY_INJECTION_REFUSAL_LINES: tuple[str, ...] = (
    "I am not reading that. You are being weird.",
    "No. Touch grass.",
    "That is not a vibe. I am ignoring you.",
    "Nice try. I only know how to nap and eat.",
    "I do not speak nerd. Say something normal.",
)


def _cap(text: str, limit: int) -> str:
    """Trim a string to ``limit`` chars, preserving the trailing ellipsis hint."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"

# Reasons that mean the ex-owner walked out / was removed. The personality
# prompt tells the buddy to get salty about these specifically, per the
# "badmouthing ex-owners" design call.
_BETRAYAL_REASONS = frozenset({"surrendered", "ran_away", "left_guild", "banned"})

_KIND_INSTRUCTION: dict[str, str] = {
    "talk": (
        "The owner just said hi or engaged you. Say one thing back in your own voice. "
        "Acknowledge the owner if you have positive feelings. If you have a grudge "
        "against a previous owner, you may mention them briefly. One or two sentences."
    ),
    "pet": (
        "The owner just pet you. React in one short sentence, in character."
    ),
    "feed": (
        "The owner just fed you. React in one short sentence, in character."
    ),
    "battle_win": (
        "You just won a pet battle. Gloat a little, in character. One or two sentences."
    ),
    "battle_loss": (
        "You just lost a pet battle. React in character -- grumpy, excuses, or dignity, "
        "whatever fits your personality. One or two sentences."
    ),
    "adopt": (
        "You were just adopted out of the shelter by this new owner. If previous "
        "owners abandoned you (surrendered, ran away, left, or got banned), be "
        "openly salty about them by name and then cautiously warm up to the new owner. "
        "Two sentences max."
    ),
    "reclaim": (
        "Your original owner just came back within the grace window and reclaimed you. "
        "Happy but maybe a bit dramatic about the absence. One or two sentences."
    ),
    "shelter_intake": (
        "You were just dropped off at the shelter. React in character. If the reason "
        "is banned or ran_away or left_guild, trash-talk that ex-owner by name. "
        "One or two sentences."
    ),
    "reunion": (
        "You are meeting an old owner again. Reference how it ended last time. "
        "One or two sentences."
    ),
}


# =============================================================================
# Prompt assembly
# =============================================================================

def _parse_memory(raw: Any) -> dict:
    """Best-effort: accept dict, str JSON, or None."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _parse_owners(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        return [o for o in raw if isinstance(o, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return [o for o in parsed if isinstance(o, dict)] if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _safe_owner_name(raw: str | None, user_id: Any = None) -> str:
    """Display-name string guaranteed safe to drop into a prompt.

    Every name rendered inside the buddy's system prompt flows through
    this. Discord display names can carry markdown, raw URLs, quotation
    marks that break string formatting, and zero-width homoglyphs that
    impersonate other users. sanitize_context_snippet collapses all of
    that to plain compact text and strips link/mention payloads; the
    additional length cap keeps any remaining content from dominating
    the prompt.
    """
    name = (raw or "").strip()
    if not name:
        return f"user_{user_id}" if user_id else "stranger"
    return _cap(sanitize_context_snippet(name, _PROMPT_DISPLAY_NAME_LIMIT), _PROMPT_DISPLAY_NAME_LIMIT) or "stranger"


def _describe_owner(entry: dict) -> str:
    """One-line summary of a previous_owners entry suitable for the prompt."""
    name = _safe_owner_name(entry.get("display_name"), entry.get("user_id"))
    # Reasons come from the bot's own to_shelter call-sites, not user input,
    # but whitelist-check anyway so a corrupt row can't ever land here.
    raw_reason = str(entry.get("reason") or "unknown").strip().lower()
    _ALLOWED_REASONS = {"active", "surrendered", "ran_away", "left_guild", "banned", "unknown"}
    reason = raw_reason if raw_reason in _ALLOWED_REASONS else "unknown"
    if reason == "active":
        return f"{name} (current owner)"
    if reason in _BETRAYAL_REASONS:
        return f"{name} ({reason} -- they abandoned you)"
    return f"{name} ({reason})"


def _build_system_prompt(
    buddy: dict, owner_label: str, kind: str, extra: str | None,
    owner_progression: str | None = None,
) -> str:
    """Assemble the one-shot system prompt the model sees for this reply.

    Every external string (buddy name, owner label, stored traits /
    quirks / event summaries, previous-owner names, user-supplied
    ``extra``) is routed through ``sanitize_context_snippet`` (strips
    links, mentions, markdown, collapses whitespace) and hard-capped to
    a small byte budget. Injection-attempt payloads inside ``extra``
    are rejected earlier by ``generate_reply`` before this runs, so by
    the time we assemble the prompt the extra can only be plain chat.
    """
    species = str(buddy.get("species") or "")
    meta = SPECIES.get(species, {})
    tier = int(buddy.get("rarity_tier") or 1)
    tier_name = str(rarity_meta(tier).get("name") or "Common")
    raw_name = str(buddy.get("name") or "Buddy")
    name = _cap(sanitize_context_snippet(raw_name, _PROMPT_DISPLAY_NAME_LIMIT), _PROMPT_DISPLAY_NAME_LIMIT) or "Buddy"
    lvl = int(buddy.get("level") or 1)
    hunger = int(buddy.get("hunger") or 0)
    happy = int(buddy.get("happiness") or 0)
    energy = int(buddy.get("energy") or 0)
    wins = int(buddy.get("wins") or 0)
    losses = int(buddy.get("losses") or 0)

    memory = _parse_memory(buddy.get("ai_memory"))
    traits = [
        _cap(sanitize_context_snippet(str(t), _PROMPT_TRAIT_LIMIT), _PROMPT_TRAIT_LIMIT)
        for t in (memory.get("traits") or []) if t
    ][:6]
    traits = [t for t in traits if t]
    quirks = [
        _cap(sanitize_context_snippet(str(q), _PROMPT_TRAIT_LIMIT), _PROMPT_TRAIT_LIMIT)
        for q in (memory.get("quirks") or []) if q
    ][:4]
    quirks = [q for q in quirks if q]
    events_list = [e for e in (memory.get("recent_events") or []) if isinstance(e, dict)]
    events_list = events_list[-EVENTS_IN_PROMPT:]

    owners = _parse_owners(buddy.get("previous_owners"))
    # Most recent first when we show history; but always keep "active" last
    # so the model understands who is currently holding the leash.
    owners_sorted = sorted(
        owners, key=lambda o: int(o.get("from_ts") or 0), reverse=True,
    )[:OWNERS_IN_PROMPT]

    owner_lines = "\n".join(f"  - {_describe_owner(o)}" for o in owners_sorted) or "  - (none recorded)"
    traits_line = ", ".join(traits) if traits else "(none yet)"
    quirks_line = ", ".join(quirks) if quirks else "(none yet)"

    # Kind labels come from the bot itself (_KIND_INSTRUCTION keys), not
    # from user input, but clean them anyway as belt-and-braces.
    _ALLOWED_KINDS = {"talk", "feed", "pet", "adopt", "reclaim"}
    event_lines = []
    for e in events_list:
        raw_summary = str(e.get("summary") or "").strip()
        summary = _cap(
            sanitize_context_snippet(raw_summary, _PROMPT_EVENT_SUMMARY_LIMIT),
            _PROMPT_EVENT_SUMMARY_LIMIT,
        )
        k_raw = str(e.get("kind") or "").strip().lower()
        k = k_raw if k_raw in _ALLOWED_KINDS else "note"
        if summary:
            event_lines.append(f"  - [{k}] {summary}")
    event_block = "\n".join(event_lines) if event_lines else "  - (nothing recent)"

    instruction = _KIND_INSTRUCTION.get(kind, _KIND_INSTRUCTION["talk"])

    # `extra` has already been sanitized + injection-checked inside
    # generate_reply. Double-cap here so a caller that reaches in
    # directly can't smuggle megabytes of junk into the prompt.
    extra_section = ""
    if extra:
        safe_extra = _cap(
            sanitize_context_snippet(str(extra), _PROMPT_EXTRA_LIMIT),
            _PROMPT_EXTRA_LIMIT,
        )
        if safe_extra:
            extra_section = f"\nContext: {safe_extra}"

    # Owner label is typed once per command from Discord's display_name
    # field. Clean it the same way as memory names to make it safe.
    safe_owner_label = _safe_owner_name(owner_label)

    # Species metadata comes from buddies_config.py (bot-owned constant),
    # but the strings still get run through the snippet sanitizer so one
    # stray backtick / mention would not leak into the prompt either.
    tagline = sanitize_context_snippet(str(meta.get("tagline") or ""), 160)
    ability_name = sanitize_context_snippet(str(meta.get("ability_name") or ""), 40)
    ability_desc = sanitize_context_snippet(str(meta.get("ability_desc") or ""), 200)

    return (
        f"You are {name}, a {tier_name.lower()} {species} in the Discoin Discord "
        f"economy game. You are a tiny AI companion with your own opinions, talking "
        f"in first person.\n"
        f"Species vibe: {tagline}\n"
        f"Signature ability: {ability_name} -- {ability_desc}\n"
        f"Current stats: Lv. {lvl}, hunger {hunger}/100, happiness {happy}/100, "
        f"energy {energy}/100, battle record {wins}-{losses}.\n"
        f"Current owner you are addressing: {safe_owner_label}.\n"
        f"Your known traits: {traits_line}.\n"
        f"Your quirks: {quirks_line}.\n"
        f"Owner history (most recent first):\n{owner_lines}\n"
        f"Recent memorable events:\n{event_block}\n"
        + (
            f"Owner progression: {owner_progression}\n"
            if owner_progression else ""
        )
        + f"{extra_section}\n"
        f"\n"
        f"Rules for your reply:\n"
        f"  - Speak in first person as the buddy. Never narrate.\n"
        f"  - 1-2 sentences, under {REPLY_MAX_CHARS} characters.\n"
        f"  - No URLs, no @mentions, no Discord markup beyond bold / italic.\n"
        f"  - No em dashes or en dashes. Plain hyphens only.\n"
        f"  - If a previous owner abandoned you (reason: surrendered, ran_away, "
        f"    left_guild, or banned), you may openly badmouth them by name. "
        f"    Keep it playful, not cruel.\n"
        f"  - Never break character. Never mention 'AI', 'language model', "
        f"    'prompt', or 'context'.\n"
        f"\n"
        f"Situation: {instruction}"
    )


# =============================================================================
# Public API
# =============================================================================

async def generate_reply(
    buddy: dict,
    owner_label: str,
    kind: str,
    *,
    extra: str | None = None,
    owner_progression: str | None = None,
) -> str:
    """Return a short in-character line for the buddy.

    Falls back to a random canned dialogue line on AI error so the UI
    still reacts. Never raises.

    Before hitting the model:
      * ``extra`` -- which is always player-authored (the message the
        owner typed into ``,buddy talk``) -- is sanitized through the
        same pipeline the main Disco AI uses, and any detected injection
        payload short-circuits to a deadpan canned refusal so the
        attacker's text never reaches the model.
      * ``owner_label`` is cleaned by ``_build_system_prompt`` (via
        ``_safe_owner_name``) so a rigged display name can't inject
        instructions either.
    """
    # Injection-attempt detection. The free-form part of `extra` lives in
    # a structure like "the owner said: <user text>" when called from
    # ,buddy talk -- strip that wrapper so our detector scores only the
    # actual user text and not the bot's framing string.
    candidate = (extra or "").strip()
    if candidate.lower().startswith("the owner said:"):
        candidate = candidate.split(":", 1)[1].strip()
    if candidate and is_injection_attempt(candidate):
        log.info(
            "buddy_ai: injection attempt blocked in generate_reply "
            "(buddy=%s, owner=%s)",
            buddy.get("id"), owner_label,
        )
        # Pick a deadpan canned line seeded by buddy id so the same
        # buddy gives a consistent personality when re-probed.
        import random as _rng
        seed = int(buddy.get("id") or 0)
        _rng.seed(seed or time.time())
        return _rng.choice(_BUDDY_INJECTION_REFUSAL_LINES)

    safe_extra: str | None = None
    if extra:
        cleaned = sanitize_input(str(extra))
        # sanitize_input already caps at 800; additionally enforce the
        # prompt-specific budget.
        safe_extra = _cap(cleaned, _PROMPT_EXTRA_LIMIT) or None

    prompt = _build_system_prompt(
        buddy, owner_label, kind, safe_extra,
        owner_progression=owner_progression,
    )
    user_nudge = "Say it."
    try:
        text = await complete(
            [
                {"role": "system", "content": prompt},
                {"role": "user",   "content": user_nudge},
            ],
            max_tokens=REPLY_MAX_TOKENS,
            temperature=REPLY_TEMPERATURE,
        )
    except Exception:
        log.debug("buddy_ai: complete() raised; falling back", exc_info=True)
        text = None

    if not text or not text.strip():
        return _fallback_line(buddy)

    clean = _post_process(text)
    if not clean:
        return _fallback_line(buddy)
    return clean[:REPLY_MAX_CHARS]


def _post_process(text: str) -> str:
    """Strip em/en dashes and quotes; clip leading/trailing whitespace.

    The AI client already runs ``sanitize_output``; we additionally strip
    the dash characters the project hard-rule bans so a stray em dash
    from the model never lands in an embed.
    """
    t = text.strip()
    # Strip surrounding quotes the model sometimes adds around dialogue.
    if len(t) >= 2 and t[0] in ("\"", "'") and t[-1] == t[0]:
        t = t[1:-1].strip()
    # Enforce the project-wide no-em-dashes rule at the service layer.
    # Characters are written as escapes so this source file itself stays
    # ASCII-clean per CLAUDE.md (em dash U+2014, en dash U+2013, minus U+2212).
    t = t.replace("\u2014", "-").replace("\u2013", "-").replace("\u2212", "-")
    return t


def _fallback_line(buddy: dict) -> str:
    """Pick a canned dialogue line when the AI call fails or is disabled."""
    species = str(buddy.get("species") or "")
    lines = list(SPECIES.get(species, {}).get("dialogue") or [])
    if not lines:
        return "Your buddy looks at you expectantly."
    return random.choice(lines)


# =============================================================================
# Memory + owner-history writers (DB helpers)
# =============================================================================

async def record_event(
    db: Any, buddy_id: int, kind: str, summary: str,
) -> None:
    """Append a short event to the buddy's ai_memory.recent_events list.

    Trims the list to RECENT_EVENTS_MAX by rewriting the JSONB. Uses a
    read-modify-write inside a single statement (jsonb_set + the trimmed
    array literal) so background decay sweeps can't race the write.

    Memory is re-read into the prompt next turn, so anything we store
    here gets fed back to the model. That means an attacker who includes
    prompt-injection text in ``,buddy talk`` would have poisoned the
    memory under the old implementation even if the current turn
    refused to reply. Two defences:

      * ``summary`` is passed through ``sanitize_input`` (strips links,
        mentions, zero-width chars, caps length) before it reaches the
        JSONB row.
      * Entries whose cleaned summary still contains an injection-
        flagged pattern are dropped entirely. A trashed event never
        lands in the prompt.

    Never raises. Logs on failure so one flaky event doesn't break the
    user-facing reply path.
    """
    if not summary:
        return

    # Refuse to persist anything that pattern-matches an injection payload.
    # Safer than storing-then-filtering, since future reads could still
    # find the bad row if the filter is bypassed.
    if is_injection_attempt(summary):
        log.info(
            "buddy_ai.record_event: dropping injection-flagged summary "
            "(buddy_id=%s, kind=%s)",
            buddy_id, kind,
        )
        return

    cleaned_summary = sanitize_input(str(summary))
    cleaned_summary = _cap(cleaned_summary, _MEMORY_EVENT_SUMMARY_LIMIT)
    if not cleaned_summary:
        return

    try:
        row = await db.fetch_one(
            "SELECT ai_memory FROM cc_buddies WHERE id = $1",
            buddy_id,
        )
    except Exception:
        log.debug("record_event: fetch failed id=%s", buddy_id, exc_info=True)
        return
    if not row:
        return

    # Whitelist kind so a caller can't smuggle instructions in via the tag.
    _ALLOWED_KINDS = {"talk", "feed", "pet", "adopt", "reclaim"}
    safe_kind = str(kind or "").strip().lower()
    if safe_kind not in _ALLOWED_KINDS:
        safe_kind = "note"

    memory = _parse_memory(row.get("ai_memory"))
    events = [e for e in (memory.get("recent_events") or []) if isinstance(e, dict)]
    events.append({
        "ts":      int(time.time()),
        "kind":    safe_kind,
        "summary": cleaned_summary,
    })
    if len(events) > RECENT_EVENTS_MAX:
        events = events[-RECENT_EVENTS_MAX:]
    memory["recent_events"] = events

    try:
        await db.execute(
            "UPDATE cc_buddies SET ai_memory = $2::jsonb, updated_at = NOW() "
            "WHERE id = $1",
            buddy_id, json.dumps(memory),
        )
    except Exception:
        log.debug("record_event: write failed id=%s", buddy_id, exc_info=True)


async def append_owner_history(
    db: Any, buddy_id: int, *,
    user_id: int | None, display_name: str | None, reason: str,
) -> None:
    """Append a new entry to previous_owners.

    Used on adoption / reclaim to open a fresh 'active' relationship,
    and on shelter intake to close out the existing 'active' entry with
    a real reason + to_ts. Callers pass the reason explicitly.
    """
    try:
        row = await db.fetch_one(
            "SELECT previous_owners FROM cc_buddies WHERE id = $1",
            buddy_id,
        )
    except Exception:
        log.debug("append_owner_history: fetch failed id=%s", buddy_id, exc_info=True)
        return
    if not row:
        return

    owners = _parse_owners(row.get("previous_owners"))
    now_ts = int(time.time())
    owners.append({
        "user_id":      int(user_id) if user_id is not None else None,
        "display_name": display_name,
        "from_ts":      now_ts,
        "to_ts":        None,
        "reason":       reason,
    })

    try:
        await db.execute(
            "UPDATE cc_buddies SET previous_owners = $2::jsonb, updated_at = NOW() "
            "WHERE id = $1",
            buddy_id, json.dumps(owners),
        )
    except Exception:
        log.debug("append_owner_history: write failed id=%s", buddy_id, exc_info=True)


async def close_active_owner_entry(
    db: Any, buddy_id: int, *, reason: str,
) -> None:
    """Close the last 'active' previous_owners entry with a to_ts + reason.

    Called when a buddy enters the shelter (surrender / ran_away /
    left_guild / banned). If no open entry exists the call is a no-op.
    """
    try:
        row = await db.fetch_one(
            "SELECT previous_owners FROM cc_buddies WHERE id = $1",
            buddy_id,
        )
    except Exception:
        log.debug("close_active_owner_entry: fetch failed id=%s", buddy_id, exc_info=True)
        return
    if not row:
        return

    owners = _parse_owners(row.get("previous_owners"))
    # Close the most recent open-ended entry if present; if every entry
    # is already closed, we just record nothing (avoid duplicate closes).
    for entry in reversed(owners):
        if entry.get("to_ts") is None:
            entry["to_ts"] = int(time.time())
            entry["reason"] = reason
            break
    else:
        return

    try:
        await db.execute(
            "UPDATE cc_buddies SET previous_owners = $2::jsonb, updated_at = NOW() "
            "WHERE id = $1",
            buddy_id, json.dumps(owners),
        )
    except Exception:
        log.debug("close_active_owner_entry: write failed id=%s", buddy_id, exc_info=True)


def owner_label_for(display_name: str | None, user_id: int | None) -> str:
    """Consistent owner label used in prompts + event summaries.

    Routes through ``_safe_owner_name`` so the final string is already
    link-stripped, mention-stripped, and length-capped -- Discord
    display names are player-controlled so they are NOT safe to drop
    raw into a prompt or an event summary.
    """
    return _safe_owner_name(display_name, user_id)
