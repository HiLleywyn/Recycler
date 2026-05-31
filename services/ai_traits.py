"""
services/ai_traits.py - Layered user trait engine with time-decay and confidence scoring.

Signal pipeline
---------------
1. ingest_message_tone(db, uid, gid, content)  -- detect tone from chat text
2. ingest_reaction(db, uid, gid, category)     -- emoji category -> trait signal
3. ingest_tool_use(db, uid, gid, tool_key)     -- tool activation -> trait signal
All three call _ingest_signal() which logs the event and upserts the trait.

Trait math (all DB-side to avoid container/DB clock skew)
---------------------------------------------------------
  weight     = old_weight * exp(-LAMBDA * age_seconds) + signal_weight
  confidence = 1 - exp(-sample_size / K)
  layer      = 'stable'      when sample_size >= STABLE_SAMPLE AND confidence >= STABLE_CONF
             = 'volatile'    when last_observed_at within 1 hour (computed at read time)
             = 'interaction' last_observed_at older than 1 hour, not yet stable (read time)

Contradiction handling
----------------------
Defined conflict pairs share signal space. When trait A receives a signal,
its conflicting counterpart B has its weight multiplied by 0.8 (dampened).

Behavior shift detection
------------------------
detect_behavior_shift() compares the event subtype distribution for the last
hour against the rolling baseline (~200-event window). Returns True if total
variation distance exceeds 0.5 or a novel dominant signal appears.

Context injection
-----------------
build_trait_context(stable, volatile, interaction) -> structured prompt block.
build_reaction_ratios(reaction_rows)               -> normalized ratios string.

Pruning
-------
prune_user_traits() removes entries below confidence/weight thresholds and
enforces per-layer caps. Called as a background task by chat.py.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# ── Decay and confidence constants ───────────────────────────────────────────

# exp(-LAMBDA * age) gives ~0.84 weight remaining after 1 hour (3600s),
# ~0.17 after 6 hours - old signals fade but do not vanish instantly.
_LAMBDA: float = 0.00005

# confidence = 1 - exp(-n / K). At n=10: ~0.63, n=20: ~0.86, n=50: ~0.99
_K: float = 10.0

# Thresholds for promoting a trait to the 'stable' layer
_STABLE_SAMPLE: int = 20
_STABLE_CONF: float = 0.7

# Age in seconds within which a trait counts as 'volatile' (1 hour)
_RECENT_WINDOW: int = 3600

# ── Pruning thresholds ────────────────────────────────────────────────────────

_PRUNE_MIN_CONF: float = 0.15
_PRUNE_MIN_WEIGHT: float = 0.05

# Maximum traits kept per layer after pruning
_LAYER_CAPS: dict[str, int] = {"stable": 10, "volatile": 5, "interaction": 5}

# ── Signal weights ────────────────────────────────────────────────────────────

_SIGNAL_WEIGHTS: dict[str, float] = {
    # Tone signals
    "humor":      1.2,
    "analytical": 1.3,
    "chaos":      1.1,
    "serious":    1.0,
    "casual":     0.9,
    # Reaction categories
    "positive":   1.0,
    "negative":   1.0,
    "loss":       1.0,
    "win":        1.0,
    "hype":       1.1,
    "frustration":1.2,
    "salt":       1.0,
    "laugh":      1.2,
    "gg":         0.9,
    "shock":      1.0,
    "vibe":       0.9,
    "support":    0.9,
    # Behavior signals (tool use)
    "trading":    1.5,
    "staking":    1.2,
    "mining":     1.2,
    "defi":       1.3,
    "lending":    1.1,
    "nft":        1.0,
    "gambling":   1.2,
}

# ── Contradiction pairs ───────────────────────────────────────────────────────
# When either key receives a signal, its counterpart is dampened by 0.8.

_CONFLICT_PAIRS: list[tuple[str, str]] = [
    ("tone.analytical", "tone.chaos"),
    ("tone.serious",    "tone.humor"),
    ("tone.serious",    "tone.casual"),
]

# ── Message tone detection ────────────────────────────────────────────────────

_HUMOR_RE = re.compile(
    r"\b(lol|lmao|lmfao|kek|bruh|fr\s+fr|no\s+cap|based|cope|seethe|ngl|kekw)\b"
    r"|[😂💀🤣😆😅😹🫠]",
    re.IGNORECASE,
)
_ANALYTICAL_RE = re.compile(
    r"^(how|why|what|when|where|does|can|should|is it|explain|tell me|what is|what are)\b",
    re.IGNORECASE,
)
_CHAOS_RE = re.compile(r"[!]{2,}|[?]{2,}|[A-Z]{5,}")


def detect_message_signals(content: str) -> list[str]:
    """Detect tone signals from a message string.

    Returns a list of subtype strings (e.g. ['humor', 'analytical']).
    May return an empty list if no strong signal is detected.
    """
    stripped = content.strip()
    signals: list[str] = []

    if _HUMOR_RE.search(stripped):
        signals.append("humor")

    if _ANALYTICAL_RE.match(stripped) or len(stripped) > 120:
        signals.append("analytical")

    # Chaos: short AND contains multi-punct OR all-caps run
    if len(stripped) < 35 and _CHAOS_RE.search(stripped):
        signals.append("chaos")
    elif (
        len(stripped) > 70
        and "humor" not in signals
        and not any(w in stripped.lower() for w in ("lol", "lmao", "bruh", "kek"))
    ):
        signals.append("serious")

    return signals


# ── Public ingestion API ──────────────────────────────────────────────────────


async def ingest_message_tone(db, user_id: int, guild_id: int, content: str) -> None:
    """Detect tone signals from a user message and update traits.

    Designed to run as a fire-and-forget background task - all errors are
    suppressed so this never affects the chat response path.
    """
    try:
        signals = detect_message_signals(content)
        for subtype in signals:
            await _ingest_signal(db, user_id, guild_id, "tone", subtype)
    except Exception:
        log.debug("ingest_message_tone: failed uid=%s", user_id, exc_info=True)


async def ingest_reaction(db, user_id: int, guild_id: int, category: str) -> None:
    """Process an emoji reaction category into a trait signal."""
    try:
        await _ingest_signal(db, user_id, guild_id, "reaction", category)
    except Exception:
        log.debug("ingest_reaction: failed uid=%s cat=%s", user_id, category)


async def ingest_tool_use(db, user_id: int, guild_id: int, tool_key: str) -> None:
    """Process a tool activation into a behavior trait signal."""
    try:
        await _ingest_signal(db, user_id, guild_id, "behavior", tool_key)
    except Exception:
        log.debug("ingest_tool_use: failed uid=%s tool=%s", user_id, tool_key)


# ── Internal signal pipeline ──────────────────────────────────────────────────


async def _ingest_signal(
    db, user_id: int, guild_id: int, event_type: str, event_subtype: str,
    *, source: str = "event", confidence_seed: float | None = None,
    signal_weight_override: float | None = None,
) -> None:
    """Core signal ingestion: log event + upsert trait with time-decay.

    ``source`` is recorded on the trait row so passive-chat extractions can
    be queried apart from the existing tone / reaction / behavior signals.
    ``confidence_seed`` overrides the insert-time confidence (used for
    low-confidence passive extractions which should decay out if not
    reinforced). ``signal_weight_override`` lets the caller bypass the
    static ``_SIGNAL_WEIGHTS`` table for novel trait keys.
    """
    if signal_weight_override is not None:
        signal_weight = float(signal_weight_override)
    else:
        signal_weight = _SIGNAL_WEIGHTS.get(event_subtype, 1.0)
    trait_key = f"{event_type}.{event_subtype}"

    try:
        await db.log_ai_event(user_id, guild_id, event_type, event_subtype)
    except Exception:
        log.debug("_ingest_signal: log_ai_event failed uid=%s key=%s", user_id, trait_key)

    try:
        await db.upsert_ai_trait(
            user_id, guild_id, trait_key, signal_weight,
            lambda_val=_LAMBDA,
            k_val=_K,
            stable_sample=_STABLE_SAMPLE,
            stable_conf=_STABLE_CONF,
            source=source,
            confidence_seed=confidence_seed,
        )
    except Exception:
        log.debug("_ingest_signal: upsert failed uid=%s key=%s", user_id, trait_key)
        return

    await _handle_contradictions(db, user_id, guild_id, trait_key)


async def _handle_contradictions(
    db, user_id: int, guild_id: int, new_trait_key: str
) -> None:
    """Dampen the weight of conflicting traits when a new signal arrives."""
    for key_a, key_b in _CONFLICT_PAIRS:
        if new_trait_key == key_a:
            conflicting = key_b
        elif new_trait_key == key_b:
            conflicting = key_a
        else:
            continue
        try:
            await db.dampen_ai_trait(user_id, guild_id, conflicting, factor=0.8)
        except Exception:
            log.debug(
                "_handle_contradictions: dampen failed uid=%s key=%s",
                user_id, conflicting,
            )


# ── Pruning ───────────────────────────────────────────────────────────────────


async def prune_user_traits(db, user_id: int, guild_id: int) -> None:
    """Remove low-signal traits, enforce per-layer caps, and prune old events.

    Safe to call as a background task - all errors are suppressed.
    """
    try:
        await db.prune_ai_traits(user_id, guild_id, _PRUNE_MIN_CONF, _PRUNE_MIN_WEIGHT)
    except Exception:
        log.debug("prune_user_traits: prune_ai_traits failed uid=%s", user_id)
        return

    for layer, cap in _LAYER_CAPS.items():
        try:
            await db.cap_ai_traits_layer(user_id, guild_id, layer, cap)
        except Exception:
            log.debug("prune_user_traits: cap failed uid=%s layer=%s", user_id, layer)

    try:
        await db.prune_ai_events(user_id, guild_id, keep=200)
    except Exception:
        log.debug("prune_user_traits: prune_ai_events failed uid=%s", user_id)


# ── Behavior shift detection ──────────────────────────────────────────────────


async def detect_behavior_shift(db, user_id: int, guild_id: int) -> bool:
    """Return True when recent behavior (last hour) diverges from baseline.

    Comparison uses total variation distance (TVD > 0.5 threshold) across event
    subtype distributions. Requires at least 5 recent events to avoid false
    positives on cold starts.

    Note: the baseline is the last ~200 logged events (rolling), not all-time,
    because ai_user_events is pruned to 200 rows per user.
    """
    try:
        recent = await db.get_ai_event_distribution(user_id, guild_id, window_secs=3600)
        baseline = await db.get_ai_baseline_event_distribution(user_id, guild_id)
    except Exception:
        return False

    if not recent or not baseline:
        return False

    total_recent = sum(recent.values())
    if total_recent < 5:
        return False

    total_baseline = sum(baseline.values())
    if total_baseline == 0:
        return False

    recent_norm = {k: v / total_recent for k, v in recent.items()}
    baseline_norm = {k: v / total_baseline for k, v in baseline.items()}

    # Novel dominant signal: top recent subtype has low baseline representation
    top_recent = max(recent_norm, key=lambda k: recent_norm[k])
    if baseline_norm.get(top_recent, 0.0) < 0.15 and recent_norm[top_recent] > 0.4:
        return True

    # Total variation distance: half the L1 norm of the difference
    all_keys = set(recent_norm) | set(baseline_norm)
    tvd = sum(
        abs(recent_norm.get(k, 0.0) - baseline_norm.get(k, 0.0))
        for k in all_keys
    ) / 2.0
    return tvd > 0.5


# ── Context builders ──────────────────────────────────────────────────────────


def build_trait_context(
    stable: list[dict],
    volatile: list[dict],
    interaction: list[dict],
) -> str:
    """Return a structured USER PROFILE prompt block from layered trait rows.

    Each row is expected to have 'trait_key' and 'confidence' fields.
    Returns empty string when no traits are available.
    """
    if not stable and not volatile and not interaction:
        return ""

    lines: list[str] = ["USER PROFILE:"]

    if stable:
        parts = []
        for t in stable[:5]:
            key = t["trait_key"].split(".", 1)[-1]
            conf = float(t.get("confidence", 0.0))
            parts.append(f"{key} (conf:{conf:.2f})")
        lines.append(f"Stable Traits: {', '.join(parts)}")

    if volatile:
        keys = [t["trait_key"].split(".", 1)[-1] for t in volatile[:3]]
        lines.append(f"Current Behavior: {', '.join(keys)}")

    if interaction:
        keys = [t["trait_key"].split(".", 1)[-1] for t in interaction[:3]]
        lines.append(f"Interaction Style: {', '.join(keys)}")

    lines.append(
        "Guidance: Match user tone. "
        "Stable traits carry more weight than volatile ones. "
        "Recent behavior overrides stale patterns."
    )
    return "\n".join(lines)


def build_reaction_ratios(reaction_rows: list[dict]) -> str:
    """Compute normalized reaction ratios from raw count rows.

    Each row must have 'category' and 'use_count' keys.
    Returns a compact string like 'laugh:45%, win:28%' or empty string.
    """
    if not reaction_rows:
        return ""
    total = sum(r.get("use_count", 0) for r in reaction_rows)
    if total == 0:
        return ""
    parts = []
    for r in sorted(reaction_rows, key=lambda x: x.get("use_count", 0), reverse=True)[:4]:
        ratio = r.get("use_count", 0) / total
        if ratio >= 0.1:
            parts.append(f"{r['category']}:{ratio:.0%}")
    return ", ".join(parts)
