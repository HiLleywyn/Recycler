"""
cogs/clanktank.py -- Clanktank: scammer and bot-account containment system.

State machine
-------------
NORMAL -> CLANKER via ,clanker add. CLANKER -> NORMAL via ,clanker remove.
While CLANKER the user holds exactly one role, earns nothing, and cannot
interact with any bot system. Disco responds with firmer containment
notices as the score rises.

Enforcement
-----------
- on_message: deletes URLs and crypto addresses. Detects staff/role pings.
  Deletes contained-user messages outside the tank wherever Disco can manage
  messages. Tracks all messages. Routes ambient replies in the tank channel.
- on_member_update: reverts any unauthorized role changes (escape attempts).
- on_member_remove: records server departures; clanker_record is kept.
- on_member_join: re-applies Clanker role if a tracked user rejoins.
- on_interaction: blocks slash-command and component interactions.
- Global prefix check: blocks all prefix commands for clanked members.
- Periodic sweep: validates role state, auto-releases expired clanks.

Evidence and intelligence
--------------------------
- At clank time: purges recent messages from every visible channel where Disco
  can manage messages, stores them as evidence, runs account-linking analysis
  against all active clankers.
- Account linking: compares usernames, display names, and message content
  against existing clankers using Jaccard token overlap. Connections share
  effective score for response tiering.
- ,clanker scan: scan active clankers, or score a guarded role band without
  auto-clanking anyone.
- Audit log: every event written to clanker_audit_log with JSONB details.

Containment response tiers (effective score)
---------------------------------------------
0-24: neutral notice
25-74: firm restriction notice
75-149: evidence-oriented notice
150-299: repeated-risk notice
300+: high-risk review notice
"""
from __future__ import annotations

import asyncio
import collections
import io
import json as _json
import logging
import math
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Sequence

import discord
from discord.ext import commands, tasks

try:
    import numpy as _np
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    _np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.middleware import guild_only
from core.framework.ui import (
    C_AMBER,
    C_ERROR,
    C_INFO,
    C_NAVY,
    C_PURPLE,
    C_SUCCESS,
    C_WARNING,
    fmt_ts,
)

log = logging.getLogger(__name__)

# -- Role / channel constants --------------------------------------------------

_CLANKER_ROLE_NAME = "Clanker"

# -- Score increments ----------------------------------------------------------

_SCORE_MESSAGE  = 1
_SCORE_BLOCKED  = 5
_SCORE_ESCAPE   = 10
_SCORE_URL      = 8
_SCORE_STAFF_DM = 15
_SCORE_LEAVE    = 20   # server leave counted as deliberate evasion
_SCORE_ROLE_PING = 5   # pinging any @role from containment

# -- Detection patterns -------------------------------------------------------

_URL_RE = re.compile(
    r"https?://\S+"                          # full URL (http/https)
    r"|www\.\S{2,}\.\S+"                     # www. bare domain
    r"|discord(?:\.gg|\.com/invite)/\S+"     # Discord invites without https://
    r"|(?:t|tg)\.me/\S+"                     # Telegram short links
    r"|bit\.ly/\S+|tinyurl\.com/\S+",        # common URL shorteners
    re.IGNORECASE,
)

_CRYPTO_RE = re.compile(
    r"\b("
    r"bc1[ac-hj-np-z02-9]{6,87}"          # Bitcoin bech32
    r"|[13][a-km-zA-HJ-NP-Z1-9]{25,34}"   # Bitcoin legacy
    r"|0x[a-fA-F0-9]{40}"                  # Ethereum / EVM
    r"|[LM][a-km-zA-HJ-NP-Z1-9]{26,33}"   # Litecoin
    r"|T[a-zA-Z0-9]{33}"                   # Tron
    r"|[rR][a-zA-Z0-9]{24,34}"             # XRP
    r")\b",
    re.IGNORECASE,
)

# -- Account-linking thresholds -----------------------------------------------

_NAME_THRESHOLD = 0.45   # Jaccard on normalised name tokens
_TEXT_THRESHOLD = 0.40   # Jaccard on normalised message tokens

# -- Duration parser ----------------------------------------------------------

_DUR_RE = re.compile(r"^(\d+)([mhd])$", re.IGNORECASE)

# -- Enforcement cooldown (per user, seconds) ---------------------------------

_ENFORCE_CD = 8.0

# -- Clank Cohesion Index (CCI) constants ------------------------------------
# Spectral clustering pipeline: feature vectors + temporal kernel ->
# adjacency matrix -> normalized graph Laplacian -> spectral embedding ->
# k-means clustering -> density/variance/tightness/sync filtering.

_CCI_SIGMA_T     = 86400.0 * 3    # temporal kernel bandwidth: 3 days (seconds)
_CCI_DIM_K       = 8              # spectral embedding dimensions
_CCI_DENSITY_TAU = 0.20           # minimum cluster density rho(C)
_CCI_VAR_TAU     = 4.0            # maximum embedding variance sigma^2_C
_CCI_TIGHT_TAU   = 86400.0 * 21   # maximum temporal spread T(C) in seconds
_CCI_SYNC_TAU    = 0.08           # minimum behavioural synchronisation S(C)
_CCI_ALPHA       = 0.40           # Score(C) = alpha*rho - beta*sigma^2 + gamma*S - delta*T
_CCI_BETA        = 0.15
_CCI_GAMMA       = 0.30
_CCI_DELTA       = 0.15
_CCI_MIN_CLUSTER = 4              # minimum cluster members for CCI detection

# -- Cluster intelligence constants -------------------------------------------

_CLUSTER_MIN_SIZE     = 5     # minimum connected accounts to form a cluster
_SCAN_CLUSTER_MIN_SIZE = 2    # role scans are human-reviewed, so small clusters are useful
_JOIN_ALERT_THRESHOLD = 0.30  # pattern score threshold to alert on suspicious join
_JOIN_AUTO_CLANK_SCORE = 1.0  # CCI score at which a new join is auto-clanked without review

# Scam-username keywords: any token in a display/username that matches triggers
# immediate auto-clank on join. Split on separators + CamelCase before comparing.
_SCAM_KEYWORDS: frozenset[str] = frozenset({
    # Universal support-scam vocabulary
    "support", "helpdesk", "recovery", "helper", "helpme",
    "assistance", "representative", "service", "services",
    # Authority / staff impersonation
    "official", "admin", "administrator", "moderator",
    "team", "staff",
    # Pre-compounded crypto scam patterns (matched against the full joined name too)
    "cryptosupport", "chainsupport", "walletsupport", "nftsupport",
    "defiSupport", "defisupport", "cryptorecovery", "walletrecovery",
    "nftrecovery", "chainrecovery", "cryptohelp", "wallethelp",
    "nfthelp", "cryptoadmin", "walletadmin", "cryptoteam",
    "cryptoofficial", "officialteam", "officialstaff",
    "customersupport", "techsupport", "technicalSupport",
    "technicalsupport", "helpcenter", "helpservice",
})

# Celebrity / public-figure names that scammers impersonate.
# Stored as normalised lowercase token sets. A join name is flagged when its
# token set overlaps with one of these entries above _CELEB_HIT_THRESHOLD.
# Only include names distinctive enough that false positives are negligible.
_CELEB_NAME_TOKENS: tuple[frozenset[str], ...] = tuple(
    frozenset(n.lower().split())
    for n in (
        # Crypto / finance figures
        "Michael Saylor", "MicroStrategy Saylor",
        "Changpeng Zhao", "CZ Binance",
        "Vitalik Buterin",
        "Sam Bankman Fried", "SBF",
        "Elon Musk",
        "Roger Ver",
        "Brian Armstrong",
        "Barry Silbert",
        "Do Kwon",
        "Justin Sun",
        "Andreas Antonopoulos",
        "Anthony Pompliano",
        "Cathie Wood",
        "Warren Buffett",
        "Charlie Munger",
        "Jeff Bezos",
        "Bill Gates",
        # Political / world leaders (common giveaway fraud accounts)
        "Donald Trump",
        "Joe Biden",
        "Barack Obama",
        "Vladimir Putin",
        "Volodymyr Zelensky",
        "Narendra Modi",
        "Emmanuel Macron",
        # Entertainment / media (frequent fake-celeb scam targets)
        "Tom Hanks",
        "Keanu Reeves",
        "Oprah Winfrey",
        "Ellen DeGeneres",
        "Elon Musk",   # listed twice -- deduped by set semantics at runtime
        "Mark Zuckerberg",
        "Jack Dorsey",
        "Tim Cook",
        "Satoshi Nakamoto",
    )
)
# Jaccard threshold for a celebrity name hit.
# 0.5 means at least half the token union must overlap -- catches
# "Official Michael Saylor" (tokens: official, michael, saylor ->
# overlap with {"michael","saylor"} = 2/4 = 0.5).
_CELEB_HIT_THRESHOLD = 0.5

# -- Probability Disco responds to ambient messages --------------------------

_RESPONSE_PROB = 0.28
_RESPONSE_PROB_BLOCKED = 1.0   # always respond on blocks

_ESCAPE_KEYWORDS: frozenset[str] = frozenset({
    "let me out", "get me out", "let me go", "free me", "release me",
    "how do i get out", "how do i escape", "how to escape", "how to get out",
    "how do i leave", "how do i get released", "how do i get free",
    "i want out", "want to leave", "want out of here", "get out of here",
    "escape room", "clanker escape", "how do i start", "how do i begin",
    "what do i do", "how do i proceed", "how do i play",
})

# -- Tiered response pools ---------------------------------------------------
# Index 0=neutral (score<25), 1=firm (25-74), 2=evidence-focused (75-149),
# 3=repeated-risk (150-299), 4=high-risk review (300+)
#
# Template vars available in every pool entry:
#   {messages}  {score}  {escape}  {blocked}  {duration}  {flags}
#   {leaves}    {rejoins}

_RESPONSES: list[list[str]] = [
    [   # tier 0: neutral
        "Noted.",
        "On the record.",
        "Logged.",
    ],
    [   # tier 1: firm
        "Still contained. Score {score}.",
        "Nothing about your status has changed.",
        "On the record. Commands are still off the table.",
    ],
    [   # tier 2: evidence-focused
        "Score {score}. Goes in the file.",
        "{messages} messages logged so far.",
        "This channel is being watched.",
    ],
    [   # tier 3: repeated-risk
        "Score {score}. {blocked} blocked attempt(s) on file.",
        "Staff can pull the full record at any time.",
        "Still contained, still being recorded.",
    ],
    [   # tier 4: high-risk
        "Score {score}. {messages} messages, {blocked} blocks, {escape} escapes -- all kept.",
        "High-risk record. Staff will see everything.",
        "The record is preserved and available for review.",
    ],
]

_ESCAPE_RESPONSES: list[list[str]] = [
    [   # tier 0
        "Role lock restored.",
        "Containment re-applied.",
        "That didn't work.",
    ],
    [   # tier 1
        "Escape attempt #{escape} logged. Score {score}.",
        "Role lock back in place.",
        "Reversed and recorded.",
    ],
    [   # tier 2
        "Attempt #{escape} reversed and added to the file. Score {score}.",
        "Containment roles restored. Audit trail updated.",
        "Role change blocked.",
    ],
    [   # tier 3
        "{escape} escape attempt(s) on record. Score {score}.",
        "Role lock restored again. Staff sees this trail.",
        "Containment holds after another attempt.",
    ],
    [   # tier 4
        "Attempt #{escape} -- high-risk pattern on record. Score {score}.",
        "Containment holds. The attempt is preserved with the full record.",
        "Role lock restored. All of it is being recorded.",
    ],
]

_BLOCKED_RESPONSES: list[list[str]] = [
    [   # tier 0
        "Blocked.",
        "Not while you're in here.",
        "Access denied.",
    ],
    [   # tier 1
        "Blocked. Score {score}.",
        "Attempt #{blocked} -- denied.",
        "Commands are off until you're out.",
    ],
    [   # tier 2
        "Attempt #{blocked} blocked and logged. Score {score}.",
        "Still denied. Score {score}.",
        "Nothing gets through from in here.",
    ],
    [   # tier 3
        "{blocked} blocked attempt(s) on file. Still denied.",
        "Blocked. Staff can pull the full attempt log.",
        "Access locked. Score {score}.",
    ],
    [   # tier 4
        "High-risk block logged. {blocked} attempts, score {score}.",
        "Command lock is active and the attempt was recorded.",
        "Fully locked. Everything attempted is on record.",
    ],
]

_URL_RESPONSES: list[list[str]] = [
    [   # tier 0
        "Removed. Links and addresses are blocked in here.",
        "Removed.",
        "That doesn't stay up.",
    ],
    [   # tier 1
        "Link removed. Score {score}.",
        "Blocked and logged.",
        "That content can't stay visible from containment.",
    ],
    [   # tier 2
        "Removed and saved to the evidence file. Score {score}.",
        "URL blocked. Added to the record.",
        "Containment filters got that one.",
    ],
    [   # tier 3
        "Removed again. Score {score}. Staff sees the pattern.",
        "Blocked content preserved in the evidence trail.",
        "Flagged, removed, recorded.",
    ],
    [   # tier 4
        "High-risk content removed. Score {score}.",
        "Removed and preserved with the full record.",
        "Message gone. The record keeps growing.",
    ],
]

_STAFF_PING_RESPONSES: list[list[str]] = [
    [   # tier 0
        "Staff ping logged.",
        "On the record.",
        "Logged.",
    ],
    [   # tier 1
        "Staff contact attempt logged. Score {score}.",
        "On the record.",
        "Added to the containment log.",
    ],
    [   # tier 2
        "Staff ping recorded alongside current evidence. Score {score}.",
        "Logged for staff review.",
        "They'll see it in the audit log.",
    ],
    [   # tier 3
        "Repeated staff contact logged. Score {score}.",
        "Staff ping on record -- containment status is unchanged.",
        "Escalation attempt preserved.",
    ],
    [   # tier 4
        "High-risk staff contact pattern. Score {score}.",
        "Preserved with the full containment record.",
        "Logged. Staff will see the whole picture.",
    ],
]

_ROLE_PING_RESPONSES: list[list[str]] = [
    [   # tier 0
        "Role ping logged.",
        "Recorded.",
        "On file.",
    ],
    [   # tier 1
        "Role ping logged. Score {score}.",
        "Containment holds regardless.",
        "That ping is on the record.",
    ],
    [   # tier 2
        "Role ping saved for staff review. Score {score}.",
        "Containment record updated.",
        "Logged.",
    ],
    [   # tier 3
        "Repeated role ping on record. Score {score}.",
        "Role mention preserved in the evidence trail.",
        "Escalation attempt recorded.",
    ],
    [   # tier 4
        "High-risk role ping pattern. Score {score}.",
        "Retained with the full record.",
        "Logged for moderation review.",
    ],
]

_LEAVE_RESPONSES: list[list[str]] = [
    [   # tier 0
        "{name} left. Record kept.",
        "{name} left the server -- containment record stays active.",
        "{name} departed. Record retained.",
    ],
    [   # tier 1
        "{name} left. Score {score}, leave #{leaves}.",
        "{name} gone. Containment kicks back in on rejoin.",
        "{name} left. Record stays.",
    ],
    [   # tier 2
        "{name} left with an active containment record. Score {score}.",
        "Departure logged for {name}. Leave #{leaves}.",
        "{name} left -- evidence and cluster links are intact.",
    ],
    [   # tier 3
        "Leave #{leaves} for {name}. Score {score}.",
        "{name} left again. Record stands.",
        "Departure logged. Trail is preserved for {name}.",
    ],
    [   # tier 4
        "High-risk departure from {name}. Leave #{leaves}, score {score}.",
        "{name} left -- full record, evidence, and cluster history are intact.",
        "Departure logged. Rejoin containment is automatic.",
    ],
]

_REJOIN_RESPONSES: list[list[str]] = [
    [   # tier 0
        "Rejoin detected. Containment re-applied.",
        "Role restored.",
        "Welcome back. Still contained.",
    ],
    [   # tier 1
        "Rejoin logged. Score {score}.",
        "Containment re-applied after rejoin #{rejoins}.",
        "Rejoin recorded, role lock restored.",
    ],
    [   # tier 2
        "Rejoin #{rejoins}. Score {score}.",
        "Containment restored, audit trail updated.",
        "Rejoin logged -- status is still contained.",
    ],
    [   # tier 3
        "Rejoin #{rejoins} -- score {score}. This keeps happening.",
        "Containment re-applied after another rejoin.",
        "Role lock back. Rejoin preserved in the record.",
    ],
    [   # tier 4
        "High-risk rejoin pattern. #{rejoins} rejoins, score {score}.",
        "Containment restored after rejoin. Full history intact.",
        "Rejoin containment applied. Cluster links remain active.",
    ],
]

_AI_BLOCK_RESPONSES: list[list[str]] = [
    [   # tier 0
        "AI access is blocked in containment.",
        "Not available from in here.",
        "AI tools are restricted.",
    ],
    [   # tier 1
        "AI access blocked. Score {score}.",
        "AI is off limits during containment.",
        "Not available from this account.",
    ],
    [   # tier 2
        "AI request blocked and logged. Score {score}.",
        "AI tools stay restricted while containment is active.",
        "Can't run from a contained account.",
    ],
    [   # tier 3
        "Repeated AI access attempt. Score {score}.",
        "AI blocked. Staff sees the attempts.",
        "Access stays locked.",
    ],
    [   # tier 4
        "High-risk AI access attempt. Score {score}.",
        "Logged with the full containment record.",
        "AI tools remain unavailable until release.",
    ],
]


# -- Helpers ------------------------------------------------------------------

def _parse_duration(s: str) -> int | None:
    m = _DUR_RE.match(s.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    return n * 86400


def _age_str(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    ts = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    delta = now - ts
    days = delta.days
    hours = delta.seconds // 3600
    return f"{days}d {hours}h" if days else f"{hours}h {(delta.seconds % 3600) // 60}m"


def _parse_step_data(value) -> dict:
    """Safely parse a DB step_data field to a dict.

    asyncpg returns JSONB columns as raw JSON strings (no codec registered),
    so this handles both the string and already-parsed-dict cases.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _duration_str(epoch: float) -> str:
    d = time.time() - epoch
    days = int(d // 86400)
    hrs = int((d % 86400) // 3600)
    mins = int((d % 3600) // 60)
    if days:
        return f"{days}d {hrs}h"
    if hrs:
        return f"{hrs}h {mins}m"
    return f"{mins}m"


def _score_tier(score: int) -> int:
    if score < 25:
        return 0
    if score < 75:
        return 1
    if score < 150:
        return 2
    if score < 300:
        return 3
    return 4


def _fill(template: str, rec: dict) -> str:
    ts = rec.get("clanked_at")
    dur = (
        _duration_str(ts.timestamp() if isinstance(ts, datetime) else float(ts))
        if ts else "?"
    )
    return template.format(
        messages=rec.get("message_count", 0),
        score=rec.get("score", 0),
        escape=rec.get("escape_attempts", 0),
        blocked=rec.get("blocked_command_count", 0),
        duration=dur,
        flags=len(rec.get("flags") or []),
        leaves=rec.get("leave_count", 0),
        rejoins=rec.get("rejoin_count", 0),
        name=rec.get("_name", ""),
    )


def _name_tokens(s: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9]", " ", s.lower()).split())


def _name_similarity(a: str, b: str) -> float:
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _text_similarity(a: str, b: str) -> float:
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _scam_name_hit(names: set[str]) -> str | None:
    """Return the first matched scam keyword if any name looks like a support scammer.

    Splits on CamelCase boundaries, digit/letter transitions, and punctuation
    separators so "CryptoSupport2024", "crypto_support", and "Crypto Support"
    all match "support". Also checks the full stripped name for run-together
    compounds like "cryptosupport123".
    """
    for name in names:
        # CamelCase split: "CryptoSupport" -> "Crypto Support"
        s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        # Digit/letter boundaries: "support2024" -> "support 2024"
        s = re.sub(r"([a-zA-Z])([0-9])|([0-9])([a-zA-Z])", r"\1\3 \2\4", s)
        # All remaining non-alphanum to spaces
        s = re.sub(r"[^a-zA-Z0-9]", " ", s)
        tokens = {t.lower() for t in s.split() if len(t) >= 3}
        whole = re.sub(r"[^a-z]", "", name.lower())
        for kw in _SCAM_KEYWORDS:
            if kw in tokens or (len(kw) >= 6 and kw in whole):
                return kw
    return None


def _celeb_name_hit(names: set[str]) -> str | None:
    """Return a canonical celeb name string if any user name impersonates a
    known public figure above _CELEB_HIT_THRESHOLD Jaccard similarity.

    Examples that match:
      "MichaelSaylor_Official" -> "michael saylor"
      "Real Donald Trump"      -> "donald trump"
      "Elon_Musk2024"          -> "elon musk"
    """
    for name in names:
        s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        s = re.sub(r"[^a-zA-Z0-9]", " ", s)
        toks = frozenset(t.lower() for t in s.split() if len(t) >= 2)
        if not toks:
            continue
        for celeb_toks in _CELEB_NAME_TOKENS:
            if not celeb_toks:
                continue
            overlap = len(toks & celeb_toks)
            union   = len(toks | celeb_toks)
            if union and overlap / union >= _CELEB_HIT_THRESHOLD:
                return " ".join(sorted(celeb_toks))
    return None


def _pick(pool: list[list[str]], tier: int, rec: dict) -> str:
    tier = max(0, min(tier, len(pool) - 1))
    return _fill(random.choice(pool[tier]), rec)


def _compute_cluster_confidence(member_count: int, connection_count: int) -> float:
    """Estimate cluster confidence from member count and internal connection density."""
    if member_count < 2:
        return 0.0
    max_edges = member_count * (member_count - 1) / 2
    size_factor = min(1.0, (member_count - 2) / 8.0)
    density = connection_count / max_edges if max_edges else 0.0
    return round(size_factor * 0.6 + density * 0.4, 4)


def _name_structure(name: str) -> str:
    """Return a short structural label for a name (word+digits, camel, etc.)."""
    n = name.strip()
    if not n:
        return "empty"
    has_sep = any(c in n for c in "-_.")
    digits = sum(c.isdigit() for c in n)
    total = len(n)
    digit_ratio = digits / total if total else 0.0
    if has_sep:
        return "separated"
    # Check digit endings before CamelCase -- JenSmith2 is "word+digits", not "camelcase"
    if re.search(r"\d{3,}$", n):   # ends with 3+ digits
        return "word+digits3"
    if re.search(r"\d{1,2}$", n):  # ends with 1-2 digits
        return "word+digits"
    if re.search(r"^\d+", n):      # starts with digits
        return "digits+word"
    if digit_ratio > 0.4:          # mostly digits throughout
        return "mostly_digits"
    # CamelCase: lowercase letter immediately followed by uppercase, no separators
    if re.search(r"[a-z][A-Z]", n):
        return "camelcase"
    return "alpha"


def _extract_name_patterns(names: list[str]) -> dict[str, list[str]]:
    """Extract recurring tokens, numeric suffixes, separators, prefixes, and structures."""
    if not names:
        return {}
    threshold = max(1, int(len(names) * 0.30))

    # --- token frequency ---
    token_counts: dict[str, int] = {}
    for name in names:
        for tok in set(re.sub(r"[^a-z0-9]", " ", name.lower()).split()):
            if len(tok) >= 2 and not tok.isdigit():
                token_counts[tok] = token_counts.get(tok, 0) + 1

    # --- prefix extraction (3 and 4 char) ---
    prefix3_counts: dict[str, int] = {}
    prefix4_counts: dict[str, int] = {}
    for name in names:
        clean = re.sub(r"[^a-z]", "", name.lower())
        if len(clean) >= 3:
            p3 = clean[:3]
            prefix3_counts[p3] = prefix3_counts.get(p3, 0) + 1
        if len(clean) >= 4:
            p4 = clean[:4]
            prefix4_counts[p4] = prefix4_counts.get(p4, 0) + 1

    # --- length bucket ---
    lengths = [len(name) for name in names]
    avg_len = sum(lengths) / len(lengths) if lengths else 0
    if avg_len < 7:
        len_bucket = "short"
    elif avg_len < 14:
        len_bucket = "medium"
    else:
        len_bucket = "long"
    len_consistent = sum(1 for l in lengths if abs(l - avg_len) <= 3)

    # --- structure ---
    structure_counts: dict[str, int] = {}
    for name in names:
        s = _name_structure(name)
        structure_counts[s] = structure_counts.get(s, 0) + 1

    return {
        "token": [t for t, c in token_counts.items() if c >= threshold],
        "num_suffix": (
            ["true"] if sum(1 for n in names if re.search(r"\d+$", n)) >= threshold else []
        ),
        "separator": [
            sep for sep, char in (("hyphen", "-"), ("underscore", "_"), ("dot", "."))
            if sum(1 for n in names if char in n) >= threshold
        ],
        "prefix_3": [p for p, c in prefix3_counts.items() if c >= threshold],
        "prefix_4": [p for p, c in prefix4_counts.items() if c >= threshold],
        "len_bucket": ([len_bucket] if len_consistent >= threshold else []),
        "structure": [s for s, c in structure_counts.items() if c >= threshold],
    }


def _build_structural_fingerprint(name_groups: list[list[str]]) -> str | None:
    """Return a human-readable structural tag shared by >= 60% of the name groups.

    name_groups is a list where each entry is the list of names for one clanker in
    the comparison set.  Returns a string like "camelcase + word+digits3 + medium"
    when the group has a consistent structural signature, or None if the group is
    too small or too structurally diverse to label.

    Used to distinguish script-generated name campaigns from random collisions.
    """
    if len(name_groups) < 3:
        return None

    threshold = max(2, int(len(name_groups) * 0.6))
    structure_counts: dict[str, int] = {}
    len_bucket_counts: dict[str, int] = {}

    for names in name_groups:
        names = [n for n in names if n]
        if not names:
            continue

        group_structures: set[str] = set()
        for name in names:
            group_structures.add(_name_structure(name))
        for s in group_structures:
            structure_counts[s] = structure_counts.get(s, 0) + 1

        avg_len = sum(len(n) for n in names) / len(names)
        lb = "short" if avg_len < 7 else ("long" if avg_len >= 14 else "medium")
        len_bucket_counts[lb] = len_bucket_counts.get(lb, 0) + 1

    common_structures = sorted(s for s, c in structure_counts.items() if c >= threshold)
    common_len = [lb for lb, c in len_bucket_counts.items() if c >= threshold]

    # Require at least one structural pattern -- length bucket alone is too generic
    # to meaningfully distinguish a script campaign from a random coincidence.
    if not common_structures:
        return None

    parts: list[str] = list(common_structures)
    if common_len:
        parts.append(common_len[0])
    return " + ".join(parts)


# -- Clank Cohesion Index (CCI) helpers --------------------------------------

def _cci_phi(names: set[str]) -> list[float]:
    """12-dimensional structural feature vector for a set of names.

    Indices:
    0: digit suffix (0/1)          4: struct word+digits3 (0/1)
    1: separator present (0/1)     5: struct word+digits (0/1)
    2: max digit ratio             6: struct digits+word (0/1)
    3: log-normalised avg length   7: struct camelcase (0/1)
                                   8: struct mostly_digits (0/1)
                                   9: struct separated (0/1)
                                  10: prefix diversity (0-1)
                                  11: shared token density (0-1)
    """
    if not names:
        return [0.0] * 12
    structs = [_name_structure(n) for n in names]
    lens = [len(n) for n in names]
    avg_len = sum(lens) / len(lens) if lens else 0.0
    digit_ratios = [sum(c.isdigit() for c in n) / max(1, len(n)) for n in names]
    prefixes = {re.sub(r"[^a-z]", "", n.lower())[:3] for n in names if len(n) >= 3}
    prefix_div = len(prefixes) / max(1, len(names))
    token_bag: dict[str, int] = {}
    for n in names:
        for t in _name_tokens(n):
            if len(t) >= 3 and not t.isdigit():
                token_bag[t] = token_bag.get(t, 0) + 1
    shared = sum(
        1 for n in names
        if any(token_bag.get(t, 0) > 1 for t in _name_tokens(n))
    )
    shared_den = shared / max(1, len(names))
    return [
        1.0 if any(re.search(r"\d+$", n) for n in names) else 0.0,               # 0
        1.0 if any(any(c in n for c in "-_.") for n in names) else 0.0,           # 1
        max(digit_ratios) if digit_ratios else 0.0,                                # 2
        min(1.0, math.log1p(avg_len) / math.log1p(32)),                           # 3
        1.0 if any(s == "word+digits3" for s in structs) else 0.0,                # 4
        1.0 if any(s == "word+digits" for s in structs) else 0.0,                 # 5
        1.0 if any(s == "digits+word" for s in structs) else 0.0,                 # 6
        1.0 if any(s == "camelcase" for s in structs) else 0.0,                   # 7
        1.0 if any(s == "mostly_digits" for s in structs) else 0.0,               # 8
        1.0 if any(s == "separated" for s in structs) else 0.0,                   # 9
        prefix_div,                                                                 # 10
        shared_den,                                                                 # 11
    ]


def _cci_cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two feature vectors, clamped to [0, 1]."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def _cci_K_t(ts_a: float, ts_b: float) -> float:
    """Gaussian temporal kernel K_ij = exp(-delta^2 / 2*sigma_t^2)."""
    delta = ts_a - ts_b
    return math.exp(-(delta * delta) / (2.0 * _CCI_SIGMA_T * _CCI_SIGMA_T))


def _cci_build_W_np(phis: list[list[float]], timestamps: list[float]) -> "object":
    """Numpy-based combined adjacency W_tilde = cosine_feature_sim * temporal_kernel."""
    phi_arr = _np.array(phis, dtype=float)
    ts_arr = _np.array(timestamps, dtype=float)
    norms = _np.linalg.norm(phi_arr, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    phi_n = phi_arr / norms
    W_feat = _np.clip(phi_n @ phi_n.T, 0.0, 1.0)
    dt = ts_arr[:, None] - ts_arr[None, :]
    W_temp = _np.exp(-(dt * dt) / (2.0 * _CCI_SIGMA_T ** 2))
    W = W_feat * W_temp
    _np.fill_diagonal(W, 0.0)
    return W


def _cci_spectral_embed(W: "object", k: int) -> "object":
    """Normalised Laplacian spectral embedding: k eigenvectors (skip trivial), row-normalised."""
    n = W.shape[0]
    d = W.sum(axis=1)
    d_safe = _np.where(d > 0.0, d, 1.0)
    d_inv_sqrt = 1.0 / _np.sqrt(d_safe)
    D_inv_sqrt = _np.diag(d_inv_sqrt)
    L_sym = _np.eye(n) - D_inv_sqrt @ W @ D_inv_sqrt
    k_use = min(k + 1, n - 1)
    _vals, vecs = _np.linalg.eigh(L_sym)
    H = vecs[:, 1: k_use + 1]
    row_n = _np.linalg.norm(H, axis=1, keepdims=True)
    row_n[row_n == 0.0] = 1.0
    return H / row_n


def _cci_kmeans_np(H: "object", k: int) -> "object":
    """k-means on spectral embedding rows. Returns integer label array."""
    rng = _np.random.default_rng(seed=42)
    n = H.shape[0]
    if n <= k:
        return _np.arange(n)
    idx = rng.choice(n, size=k, replace=False)
    centroids = H[idx].copy()
    labels = _np.zeros(n, dtype=int)
    for _ in range(150):
        dists = _np.linalg.norm(H[:, None, :] - centroids[None, :, :], axis=2)
        new_labels = _np.argmin(dists, axis=1)
        if _np.all(new_labels == labels):
            break
        labels = new_labels
        for j in range(k):
            m = H[labels == j]
            if len(m):
                centroids[j] = m.mean(axis=0)
    return labels


def _cci_cluster_metrics(
    H: "object",
    labels: "object",
    timestamps: list[float],
    W: "object",
) -> list[dict]:
    """Compute quality metrics (rho, sigma2, T, S, score) for each CCI cluster."""
    k = int(labels.max()) + 1 if len(labels) else 0
    ts_arr = _np.array(timestamps, dtype=float)
    results: list[dict] = []
    for c in range(k):
        idxs = _np.where(labels == c)[0]
        if len(idxs) < 2:
            continue
        intra_W = W[_np.ix_(idxs, idxs)]
        max_edges = len(idxs) * (len(idxs) - 1)
        strong = float(_np.sum(intra_W > 0.1)) - len(idxs)
        rho = max(0.0, strong / max_edges) if max_edges > 0 else 0.0
        sigma2 = float(_np.var(H[idxs]))
        ts_c = ts_arr[idxs]
        T = float(ts_c.max() - ts_c.min()) if len(ts_c) > 1 else float("inf")
        S = max(0.0, 1.0 - T / max(1.0, _CCI_TIGHT_TAU))
        var_norm = min(1.0, sigma2 / max(1.0, _CCI_VAR_TAU))
        score = (
            _CCI_ALPHA * rho
            - _CCI_BETA * var_norm
            + _CCI_GAMMA * S
            - _CCI_DELTA * (T / max(1.0, _CCI_TIGHT_TAU))
        )
        results.append({
            "label": c,
            "members": idxs.tolist(),
            "rho": rho,
            "sigma2": sigma2,
            "T": T,
            "S": S,
            "score": score,
        })
    return results


def _cci_naive_bayes(
    phi_new: list[float],
    clanker_phis: list[list[float]],
    smoothing: float = 1.0,
) -> float:
    """Bayesian posterior P(clanker | phi_new) via Naive Bayes with Beta-Bernoulli
    likelihoods learned from the confirmed clanker population.

    Binary features (indices 0-1, 4-9, threshold > 0.5) use a Beta posterior mean
    with Laplace smoothing as the likelihood, compared against an uninformative null
    at p=0.5. Continuous features (2=digit_ratio, 3=log_length) use a Gaussian
    likelihood against the clanker mean and variance.

    Log-likelihood ratios from all 12 features are summed and passed through
    sigmoid to produce a calibrated posterior probability in [0, 1]. Returns 0.5
    (uninformative prior) when fewer than 5 examples are available.
    """
    n = len(clanker_phis)
    if n < 5:
        return 0.5

    log_odds = 0.0
    for i, phi_val in enumerate(phi_new):
        if i in (2, 3):
            # Gaussian likelihood vs. uniform[0,1] baseline for continuous features
            vals = [phi[i] for phi in clanker_phis]
            mu = sum(vals) / n
            var = sum((v - mu) ** 2 for v in vals) / n + 1e-6
            sigma = math.sqrt(var)
            log_p_clanker = (
                -0.5 * ((phi_val - mu) / sigma) ** 2
                - math.log(sigma * math.sqrt(2.0 * math.pi))
            )
            log_odds += max(-2.0, min(2.0, log_p_clanker))
        else:
            # Beta-Bernoulli: likelihood ratio vs. uninformative null (p = 0.5)
            active = phi_val > 0.5
            n_active = sum(1 for phi in clanker_phis if phi[i] > 0.5)
            p_given_clanker = (n_active + smoothing) / (n + 2.0 * smoothing)
            p_null = 0.5
            if active:
                lr = p_given_clanker / max(1e-9, p_null)
            else:
                lr = (1.0 - p_given_clanker) / max(1e-9, 1.0 - p_null)
            log_odds += max(-2.0, min(2.0, math.log(max(1e-9, lr))))

    return 1.0 / (1.0 + math.exp(-log_odds))


# -- Chart generation ---------------------------------------------------------

def _generate_clanktank_chart(data: dict) -> bytes:
    """Render a 3x2 clanktank analytics chart to PNG bytes (blocking -- run in executor)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec

    BG      = "#2b2d31"
    SURFACE = "#1e1f22"
    MUTED   = "#b5bac1"
    BLUE    = "#5865f2"
    GREEN   = "#57f287"
    RED     = "#ed4245"
    PURPLE  = "#9b59b6"
    AMBER   = "#f39c12"

    fig = plt.figure(figsize=(14, 12), facecolor=BG)
    gs  = gridspec.GridSpec(3, 2, figure=fig, hspace=0.52, wspace=0.38)

    def _style(ax: "plt.Axes") -> None:
        ax.set_facecolor(SURFACE)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.xaxis.label.set_color(MUTED)
        ax.yaxis.label.set_color(MUTED)
        ax.title.set_color(MUTED)
        for sp in ax.spines.values():
            sp.set_edgecolor("#313338")
        ax.grid(True, color="#313338", alpha=0.5, linewidth=0.5)

    # [0,0] Clanks and releases per day (last 30 days)
    ax1 = fig.add_subplot(gs[0, 0])
    day_data: list[tuple] = data.get("days", [])
    esc_day_data: list[tuple] = data.get("escape_days", [])
    if day_data:
        dates  = [datetime.fromtimestamp(float(d[0]), tz=timezone.utc) for d in day_data]
        counts = [int(d[1]) for d in day_data]
        ax1.plot(dates, counts, color=BLUE, linewidth=2, marker="o", markersize=4, zorder=2, label="Clanked")
        ax1.fill_between(dates, counts, alpha=0.15, color=BLUE)
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=28, ha="right")
    if esc_day_data:
        edates  = [datetime.fromtimestamp(float(d[0]), tz=timezone.utc) for d in esc_day_data]
        ecounts = [int(d[1]) for d in esc_day_data]
        ax1.plot(edates, ecounts, color=GREEN, linewidth=2, marker="s", markersize=4, zorder=2, label="Released")
        if not day_data:
            plt.setp(ax1.xaxis.get_majorticklabels(), rotation=28, ha="right")
    if not day_data and not esc_day_data:
        ax1.text(0.5, 0.5, "No data", ha="center", va="center",
                 color=MUTED, transform=ax1.transAxes, fontsize=10)
    if day_data or esc_day_data:
        ax1.legend(fontsize=7, facecolor=SURFACE, edgecolor="#313338", labelcolor=MUTED)
    ax1.set_title("Clanks & Releases per Day (last 30d)", fontsize=10, pad=6)
    ax1.set_ylabel("Count", fontsize=8)
    _style(ax1)

    # [0,1] Score distribution
    ax2 = fig.add_subplot(gs[0, 1])
    scores: list[int] = data.get("scores", [])
    if scores:
        ax2.hist(scores, bins=min(25, max(5, len(scores))),
                 color=PURPLE, edgecolor="#313338", alpha=0.85)
    else:
        ax2.text(0.5, 0.5, "No data", ha="center", va="center",
                 color=MUTED, transform=ax2.transAxes, fontsize=10)
    ax2.set_title("Score Distribution (active)", fontsize=10, pad=6)
    ax2.set_xlabel("Score", fontsize=8)
    ax2.set_ylabel("Users", fontsize=8)
    _style(ax2)

    # [1,0] Active / Released (Mod) / Released (Escape) pie
    ax3 = fig.add_subplot(gs[1, 0])
    active_n: int        = data.get("active", 0)
    mod_released_n: int  = data.get("mod_released", 0)
    esc_released_n: int  = data.get("escape_released", 0)
    pie_vals    = [v for v in [active_n, mod_released_n, esc_released_n] if v > 0]
    pie_labels  = [lbl for v, lbl in [
        (active_n,       f"Active ({active_n})"),
        (mod_released_n, f"Released Mod ({mod_released_n})"),
        (esc_released_n, f"Released Escape ({esc_released_n})"),
    ] if v > 0]
    pie_colors  = [c for v, c in [
        (active_n, RED), (mod_released_n, MUTED), (esc_released_n, GREEN),
    ] if v > 0]
    if pie_vals:
        ax3.pie(
            pie_vals, labels=pie_labels, colors=pie_colors,
            autopct="%1.0f%%",
            textprops={"color": MUTED, "fontsize": 8},
            wedgeprops={"edgecolor": BG, "linewidth": 2},
        )
    else:
        ax3.text(0.5, 0.5, "No data", ha="center", va="center",
                 color=MUTED, transform=ax3.transAxes, fontsize=10)
    ax3.set_facecolor(BG)
    ax3.set_title("Active / Released (Mod) / Released (Escape)", fontsize=10, pad=6, color=MUTED)

    # [1,1] Top clankers by score
    ax4 = fig.add_subplot(gs[1, 1])
    top: list[tuple] = data.get("top", [])
    if top:
        names = [str(t[0]) for t in top]
        vals  = [int(t[1]) for t in top]
        ypos  = list(range(len(names)))
        ax4.barh(ypos, vals, color=BLUE, edgecolor="#313338", alpha=0.85)
        ax4.set_yticks(ypos)
        ax4.set_yticklabels(names, fontsize=7)
        ax4.invert_yaxis()
    else:
        ax4.text(0.5, 0.5, "No data", ha="center", va="center",
                 color=MUTED, transform=ax4.transAxes, fontsize=10)
    ax4.set_title("Top Clankers by Score", fontsize=10, pad=6)
    ax4.set_xlabel("Score", fontsize=8)
    _style(ax4)

    # [2,0] Escape room station distribution (active users per step)
    ax5 = fig.add_subplot(gs[2, 0])
    step_dist: dict[int, int] = data.get("step_dist", {})
    _ER_STEP_LABELS = [
        "Intake", "Charges", "Human Check", "Oath",
        "Reflection", "Pledge", "Exam", "DM Check",
    ]
    steps    = list(range(8))
    counts5  = [step_dist.get(s, 0) for s in steps]
    colors5  = [AMBER if any(counts5) else MUTED] * 8
    if any(counts5):
        ax5.bar(steps, counts5, color=colors5, edgecolor="#313338", alpha=0.85)
        ax5.set_xticks(steps)
        ax5.set_xticklabels(_ER_STEP_LABELS, fontsize=7, rotation=30, ha="right")
    else:
        ax5.text(0.5, 0.5, "No active escape rooms", ha="center", va="center",
                 color=MUTED, transform=ax5.transAxes, fontsize=10)
    ax5.set_title("Escape Room: Active Users per Station", fontsize=10, pad=6)
    ax5.set_ylabel("Users", fontsize=8)
    _style(ax5)

    # [2,1] Escape room funnel: not started vs in progress vs completed
    ax6 = fig.add_subplot(gs[2, 1])
    in_progress_n = sum(step_dist.values())
    funnel_labels = ["Not Started", "In Progress", "Completed"]
    funnel_vals   = [
        max(0, active_n - in_progress_n),
        in_progress_n,
        esc_released_n,
    ]
    funnel_colors = [MUTED, AMBER, GREEN]
    non_zero = [(v, l, c) for v, l, c in zip(funnel_vals, funnel_labels, funnel_colors) if v > 0]
    if non_zero:
        fv, fl, fc = zip(*non_zero)
        ax6.bar(fl, fv, color=fc, edgecolor="#313338", alpha=0.85)
        ax6.set_xticklabels(fl, fontsize=8)
    else:
        ax6.text(0.5, 0.5, "No data", ha="center", va="center",
                 color=MUTED, transform=ax6.transAxes, fontsize=10)
    ax6.set_title("Escape Room Funnel", fontsize=10, pad=6)
    ax6.set_ylabel("Users", fontsize=8)
    _style(ax6)

    fig.suptitle("Clanktank Analytics", color=MUTED, fontsize=13, y=0.998)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor=BG)
    buf.seek(0)
    plt.close(fig)
    return buf.read()


# -- Cog ----------------------------------------------------------------------

class Clanktank(commands.Cog):
    """Scammer / bot-account containment. CLANKER state machine."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._clanked: set[tuple[int, int]] = set()
        self._escape_msg_ids: set[int] = set()
        self._escape_thread_override: int = 0
        self._enforce_ts: dict[int, float] = {}
        self._clarion_msg_ids: set[int] = set()
        self._clarion_rate: dict[int, collections.deque] = {}

    # -- Lifecycle ------------------------------------------------------------

    async def cog_load(self) -> None:
        await self._refresh_cache()
        self.bot.add_check(self._global_prefix_check)
        await self._load_escape_thread_override()
        self._sweep.start()
        await self._restore_escape_views()
        log.info("Clanktank ready: %d active clankers %d escape rooms", len(self._clanked), len(self._escape_msg_ids))

    async def _load_escape_thread_override(self) -> None:
        """Load a runtime escape-thread override saved via ,clanker er setthread."""
        try:
            row = await self.bot.db.fetch_one(
                "SELECT clank_escape_thread FROM guild_settings "
                "WHERE clank_escape_thread IS NOT NULL AND clank_escape_thread <> 0 LIMIT 1"
            )
            self._escape_thread_override = int(row["clank_escape_thread"]) if row else 0
        except Exception:
            self._escape_thread_override = 0

    async def _restore_escape_views(self) -> tuple[int, int]:
        """(Re)register persistent escape-room views for every active room.

        Returns (registered, cleared). Verifies each tracked message still
        exists; nulls the pointer for any that don't so ,clanker escape can
        recreate them. Safe to call live -- powers ,clanker er reload.
        """
        registered = 0
        cleared = 0
        try:
            rows = await self.bot.db.fetch_all(
                "SELECT user_id, guild_id, case_num, message_id, step, step_data "
                "FROM clank_escape WHERE message_id IS NOT NULL AND completed_at IS NULL"
            )
            self._escape_msg_ids = set()
            thread = await self._get_escape_thread()
            for r in (rows or []):
                mid = r.get("message_id")
                if not mid:
                    continue
                mid = int(mid)
                # Verify the message still exists -- if it was deleted (or this is a
                # stale dev embed), clear the pointer so ,clanker escape recreates it.
                if thread is not None:
                    try:
                        await thread.fetch_message(mid)
                    except Exception:
                        try:
                            await self.bot.db.execute(
                                "UPDATE clank_escape SET message_id=NULL WHERE user_id=$1 AND guild_id=$2",
                                int(r["user_id"]), int(r["guild_id"]),
                            )
                            cleared += 1
                        except Exception:
                            pass
                        continue
                self._escape_msg_ids.add(mid)
                step_data = _parse_step_data(r.get("step_data"))
                username = step_data.get("username", f"<@{r['user_id']}>")
                view = _EscapeRoomView(
                    self, int(r["user_id"]), int(r["guild_id"]),
                    int(r.get("case_num") or 0), username,
                    int(r.get("step") or 0), step_data,
                )
                self.bot.add_view(view, message_id=mid)
                registered += 1
        except Exception:
            log.warning("clanktank: escape view restore failed", exc_info=True)
        return registered, cleared

    async def cog_unload(self) -> None:
        self.bot.remove_check(self._global_prefix_check)
        self._sweep.cancel()

    # -- Cache ----------------------------------------------------------------

    async def _refresh_cache(self) -> None:
        try:
            rows = await self.bot.db.fetch_all(
                "SELECT user_id, guild_id FROM clanker_records"
            )
            self._clanked = {(int(r["user_id"]), int(r["guild_id"])) for r in (rows or [])}
        except Exception:
            log.warning("clanktank: cache refresh failed", exc_info=True)

    async def is_clanker(self, user_id: int, guild_id: int) -> bool:
        return (user_id, guild_id) in self._clanked

    # -- Public AI context ----------------------------------------------------

    async def clanktank_summary_for_ai(self, guild_id: int) -> str:
        try:
            rows = await self.bot.db.fetch_all(
                "SELECT * FROM clanker_records WHERE guild_id=$1 ORDER BY score DESC",
                guild_id,
            )
        except Exception:
            return ""
        if not rows:
            return "CLANKTANK STATUS: empty -- no users currently in containment."

        guild = self.bot.get_guild(guild_id)
        lines = [f"CLANKTANK STATUS -- {len(rows)} user(s) currently in containment:"]
        for rec in rows[:8]:
            uid = int(rec["user_id"])
            member = guild.get_member(uid) if guild else None
            name = member.display_name if member else f"ID:{uid}"
            ts = rec.get("clanked_at")
            dur = _duration_str(ts.timestamp() if isinstance(ts, datetime) else float(ts)) if ts else "?"
            flags = rec.get("flags") or []
            linked = rec.get("linked_accounts") or []
            absent = " [LEFT SERVER]" if rec.get("left_at") else ""
            lines.append(
                f"- {name}{absent}: {dur} in tank, {rec['message_count']} msgs, "
                f"score {rec['score']}, {rec['escape_attempts']} escapes, "
                f"{rec.get('leave_count', 0)} server leaves, {rec.get('rejoin_count', 0)} rejoins"
                + (f", reason: {rec['reason']}" if rec.get("reason") else "")
                + (f", flags: {', '.join(flags)}" if flags else "")
                + (f", linked to {len(linked)} account(s)" if linked else "")
            )
        if len(rows) > 8:
            lines.append(f"...and {len(rows) - 8} more")

        try:
            conns = await self.bot.db.fetch_all(
                "SELECT user_id_a, user_id_b, reasons, name_score, text_score "
                "FROM clanker_connections WHERE guild_id=$1 "
                "ORDER BY detected_at DESC LIMIT 5",
                guild_id,
            )
            if conns:
                lines.append("\nRecent account connections detected:")
                for c in conns:
                    a = guild.get_member(int(c["user_id_a"])) if guild else None
                    b = guild.get_member(int(c["user_id_b"])) if guild else None
                    na = a.display_name if a else f"ID:{c['user_id_a']}"
                    nb = b.display_name if b else f"ID:{c['user_id_b']}"
                    lines.append(f"- {na} <-> {nb}: {', '.join(c['reasons'] or [])}")
        except Exception:
            pass

        return "\n".join(lines)

    async def ai_block_response(self, user_id: int, guild_id: int) -> str:
        """Return a funny block message for clankers attempting to use AI chat."""
        rec = await self._get_record(user_id, guild_id) or {}
        eff = await self._effective_score(user_id, guild_id, rec)
        tier = _score_tier(eff)
        return _pick(_AI_BLOCK_RESPONSES, tier, rec)

    # -- Role utilities -------------------------------------------------------

    def _clanker_role(self, guild: discord.Guild) -> discord.Role | None:
        rid = Config.CLANKER_ROLE_ID
        if rid:
            return guild.get_role(rid)
        return discord.utils.get(guild.roles, name=_CLANKER_ROLE_NAME)

    def _allowed_role_ids(self, guild: discord.Guild) -> set[int]:
        allowed = {guild.default_role.id}
        role = self._clanker_role(guild)
        if role:
            allowed.add(role.id)
        return allowed

    async def _is_staff(self, member: discord.Member) -> bool:
        return member.guild_permissions.manage_messages or member.guild_permissions.administrator

    # -- Score / stat helpers -------------------------------------------------

    async def _get_record(self, user_id: int, guild_id: int) -> dict | None:
        try:
            return await self.bot.db.fetch_one(
                "SELECT * FROM clanker_records WHERE user_id=$1 AND guild_id=$2",
                user_id, guild_id,
            )
        except Exception:
            return None

    async def _next_case_num(self, gid: int) -> int:
        """Atomically increment and return the next sequential case number for this guild."""
        try:
            row = await self.bot.db.fetch_one(
                """INSERT INTO clank_case_counter (guild_id, last_num)
                   VALUES ($1, 1)
                   ON CONFLICT (guild_id) DO UPDATE
                   SET last_num = clank_case_counter.last_num + 1
                   RETURNING last_num""",
                gid,
            )
            return int(row["last_num"]) if row else 1
        except Exception:
            return random.randint(100001, 999999)

    async def _inc(self, user_id: int, guild_id: int, **kwargs: int) -> dict | None:
        if not kwargs:
            return None
        clauses = ", ".join(
            f"{col} = {col} + ${i + 3}" for i, col in enumerate(kwargs)
        )
        try:
            return await self.bot.db.fetch_one(
                f"UPDATE clanker_records SET {clauses} "
                f"WHERE user_id=$1 AND guild_id=$2 RETURNING *",
                user_id, guild_id, *kwargs.values(),
            )
        except Exception:
            return None

    async def _update_last_message(self, user_id: int, guild_id: int) -> dict | None:
        try:
            return await self.bot.db.fetch_one(
                "UPDATE clanker_records "
                "SET message_count = message_count + 1, "
                "    score = score + $3, "
                "    last_message_at = now() "
                "WHERE user_id=$1 AND guild_id=$2 RETURNING *",
                user_id, guild_id, _SCORE_MESSAGE,
            )
        except Exception:
            return None

    async def _effective_score(self, user_id: int, guild_id: int, rec: dict | None = None) -> int:
        """Own score plus 50% of connected accounts' scores."""
        base = int((rec or {}).get("score") or 0)
        try:
            rows = await self.bot.db.fetch_all(
                """SELECT cr.score FROM clanker_connections cc
                   JOIN clanker_records cr ON cr.guild_id = $1 AND (
                       (cc.user_id_a = $2 AND cr.user_id = cc.user_id_b) OR
                       (cc.user_id_b = $2 AND cr.user_id = cc.user_id_a)
                   )
                   WHERE cc.guild_id = $1""",
                guild_id, user_id,
            )
            bonus = sum(int(r["score"] or 0) // 2 for r in (rows or []))
            return base + bonus
        except Exception:
            return base

    # -- Evidence -------------------------------------------------------------

    async def _store_evidence(
        self,
        user_id: int,
        guild_id: int,
        messages: Sequence[discord.Message],
        evidence_type: str,
    ) -> None:
        rows = [
            (
                user_id, guild_id,
                msg.id, msg.channel.id,
                (msg.content or "").strip()[:2000],
                msg.created_at, evidence_type,
            )
            for msg in messages
            if (msg.content or "").strip()
        ]
        if not rows:
            return
        try:
            await self.bot.db.execute_many(
                """INSERT INTO clanker_evidence
                   (user_id, guild_id, message_id, channel_id, content, sent_at, evidence_type)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT DO NOTHING""",
                rows,
            )
        except Exception:
            log.debug("clanktank: store_evidence batch failed uid=%s count=%d", user_id, len(rows))

    async def _store_evidence_text(
        self,
        user_id: int,
        guild_id: int,
        content: str,
        channel_id: int | None,
        evidence_type: str,
    ) -> None:
        try:
            await self.bot.db.execute(
                """INSERT INTO clanker_evidence
                   (user_id, guild_id, channel_id, content, evidence_type)
                   VALUES ($1, $2, $3, $4, $5)""",
                user_id, guild_id, channel_id, content[:2000], evidence_type,
            )
        except Exception:
            log.debug("clanktank: store_evidence_text failed uid=%s", user_id)

    async def _purge_channel(
        self,
        member: discord.Member,
        channel: discord.TextChannel | discord.Thread,
    ) -> list[discord.Message]:
        bot_member = channel.guild.me
        if bot_member is None:
            return []
        perms = channel.permissions_for(bot_member)
        if not (perms.view_channel and perms.read_message_history and perms.manage_messages):
            return []
        try:
            deleted = await channel.purge(
                limit=500,
                check=lambda m: m.author.id == member.id,
                reason="Clanktank: purging clanker messages on add",
            )
            return deleted
        except discord.Forbidden:
            log.warning("clanktank: purge forbidden channel=%s", channel.id)
            return []
        except Exception:
            log.debug("clanktank: purge failed channel=%s", channel.id, exc_info=True)
            return []

    def _is_tank_surface(self, channel: discord.abc.Messageable) -> bool:
        tank_id = Config.CLANKTANK_CHANNEL_ID
        if not tank_id:
            return False
        ch_id = int(getattr(channel, "id", 0) or 0)
        if ch_id == tank_id:
            return True
        if isinstance(channel, discord.Thread):
            return int(getattr(channel, "parent_id", 0) or 0) == tank_id
        return False

    async def _delete_clanker_message(
        self,
        message: discord.Message,
        *,
        reason: str,
    ) -> bool:
        if message.guild is None:
            return False
        ch_id = getattr(message.channel, "id", "?")
        ch_type = type(message.channel).__name__
        msg_id = getattr(message, "id", "?")
        # Log every delete attempt so we can confirm code is reached regardless
        # of whether the exception branch fires.
        _bot_member = message.guild.me
        _can_manage = None
        if _bot_member is not None:
            try:
                _perms = message.channel.permissions_for(_bot_member)
                _can_manage = bool(_perms.manage_messages)
            except Exception:
                _can_manage = None
        log.warning(
            "clanktank: delete attempt msg=%s channel=%s type=%s manage_messages=%s",
            msg_id, ch_id, ch_type, _can_manage,
        )
        try:
            if isinstance(message, discord.PartialMessage):
                await message.delete()
            else:
                await message.delete(reason=reason)
            log.warning("clanktank: delete SUCCESS msg=%s channel=%s", msg_id, ch_id)
            return True
        except discord.NotFound:
            log.warning("clanktank: delete NOT_FOUND msg=%s channel=%s (already gone)", msg_id, ch_id)
            return True  # already gone
        except discord.Forbidden as exc:
            log.warning(
                "clanktank: delete FORBIDDEN msg=%s channel=%s type=%s "
                "discord_code=%s text=%r",
                msg_id, ch_id, ch_type,
                getattr(exc, "code", "?"), getattr(exc, "text", str(exc)),
            )
            return False
        except Exception as exc:
            log.warning(
                "clanktank: delete ERROR msg=%s channel=%s type=%s exc=%r",
                msg_id, ch_id, ch_type, exc,
                exc_info=True,
            )
            return False

    def _visible_purge_channels(
        self,
        guild: discord.Guild,
        preferred: discord.TextChannel | discord.Thread | None = None,
    ) -> list[discord.TextChannel | discord.Thread]:
        """Return visible text/thread surfaces where Disco can remove user messages."""
        seen: set[int] = set()
        channels: list[discord.TextChannel | discord.Thread] = []

        def add(ch: discord.TextChannel | discord.Thread | None) -> None:
            if ch is None or ch.id in seen:
                return
            bot_member = guild.me
            if bot_member is None:
                return
            try:
                perms = ch.permissions_for(bot_member)
            except Exception:
                return
            if perms.view_channel and perms.read_message_history and perms.manage_messages:
                seen.add(ch.id)
                channels.append(ch)

        add(preferred)
        for ch in guild.text_channels:
            add(ch)
        for thread in guild.threads:
            add(thread)
        return channels

    async def _purge_visible_messages(
        self,
        member: discord.Member,
        preferred: discord.TextChannel | discord.Thread | None = None,
    ) -> list[discord.Message]:
        channels = self._visible_purge_channels(member.guild, preferred)
        if not channels:
            return []
        results = await asyncio.gather(
            *[self._purge_channel(member, ch) for ch in channels],
            return_exceptions=True,
        )
        purged: list[discord.Message] = []
        for r in results:
            if isinstance(r, list):
                purged.extend(r)
        return purged

    # -- Account linking ------------------------------------------------------

    async def _detect_connections(
        self,
        new_uid: int,
        new_gid: int,
        new_names: set[str],
    ) -> list[dict]:
        """Compare new_uid against all other clankers by name and message similarity."""
        existing = await self.bot.db.fetch_all(
            "SELECT user_id, usernames, display_names "
            "FROM clanker_records WHERE guild_id=$1 AND user_id != $2",
            new_gid, new_uid,
        )
        if not existing:
            return []

        ev_rows = await self.bot.db.fetch_all(
            "SELECT content FROM clanker_evidence "
            "WHERE user_id=$1 AND guild_id=$2 ORDER BY logged_at DESC LIMIT 30",
            new_uid, new_gid,
        )
        new_text = " ".join(r["content"] for r in (ev_rows or []))

        connections: list[dict] = []
        for rec in existing:
            other_uid = int(rec["user_id"])
            other_names = set(rec.get("usernames") or []) | set(rec.get("display_names") or [])

            name_sim = max(
                (_name_similarity(n1, n2) for n1 in new_names for n2 in other_names if n1 and n2),
                default=0.0,
            )

            text_sim = 0.0
            if new_text:
                other_ev = await self.bot.db.fetch_all(
                    "SELECT content FROM clanker_evidence "
                    "WHERE user_id=$1 AND guild_id=$2 ORDER BY logged_at DESC LIMIT 30",
                    other_uid, new_gid,
                )
                other_text = " ".join(r["content"] for r in (other_ev or []))
                if other_text:
                    text_sim = _text_similarity(new_text, other_text)

            if name_sim >= _NAME_THRESHOLD or text_sim >= _TEXT_THRESHOLD:
                reasons: list[str] = []
                if name_sim >= _NAME_THRESHOLD:
                    reasons.append(f"name_similarity:{name_sim:.2f}")
                if text_sim >= _TEXT_THRESHOLD:
                    reasons.append(f"message_similarity:{text_sim:.2f}")
                connections.append({
                    "user_id": other_uid,
                    "reasons": reasons,
                    "name_score": name_sim,
                    "text_score": text_sim,
                })

        return connections

    async def _detect_near_misses(
        self,
        new_uid: int,
        new_gid: int,
        new_names: set[str],
        *,
        name_low: float = 0.25,
        text_low: float = 0.28,
    ) -> list[dict]:
        """Return clankers that are similar to new_uid but below the connection threshold.

        Returns dicts with user_id, name_score, text_score -- none of which pass the
        full threshold, but all score at least name_low or text_low.
        """
        existing = await self.bot.db.fetch_all(
            "SELECT user_id, usernames, display_names "
            "FROM clanker_records WHERE guild_id=$1 AND user_id != $2",
            new_gid, new_uid,
        )
        if not existing:
            return []

        ev_rows = await self.bot.db.fetch_all(
            "SELECT content FROM clanker_evidence "
            "WHERE user_id=$1 AND guild_id=$2 ORDER BY logged_at DESC LIMIT 30",
            new_uid, new_gid,
        )
        new_text = " ".join(r["content"] for r in (ev_rows or []))

        near: list[dict] = []
        for rec in existing:
            other_uid = int(rec["user_id"])
            other_names = set(rec.get("usernames") or []) | set(rec.get("display_names") or [])

            name_sim = max(
                (_name_similarity(n1, n2) for n1 in new_names for n2 in other_names if n1 and n2),
                default=0.0,
            )
            if name_sim >= _NAME_THRESHOLD:
                continue  # already a full connection -- skip near-miss bucket

            text_sim = 0.0
            if new_text:
                other_ev = await self.bot.db.fetch_all(
                    "SELECT content FROM clanker_evidence "
                    "WHERE user_id=$1 AND guild_id=$2 ORDER BY logged_at DESC LIMIT 30",
                    other_uid, new_gid,
                )
                other_text = " ".join(r["content"] for r in (other_ev or []))
                if other_text:
                    text_sim = _text_similarity(new_text, other_text)

            if text_sim >= _TEXT_THRESHOLD:
                continue  # already a full connection on text -- skip

            if name_sim >= name_low or text_sim >= text_low:
                near.append({
                    "user_id": other_uid,
                    "name_score": name_sim,
                    "text_score": text_sim,
                    "other_names": sorted(other_names)[:3],
                })

        near.sort(key=lambda x: max(x["name_score"], x["text_score"]), reverse=True)
        return near[:6]

    @staticmethod
    def _compare_clanker_pairs(
        recs: list[dict],
        ev_by_uid: dict[int, list[str]],
        known: set[tuple[int, int]],
    ) -> list[dict]:
        """O(N^2) pair comparison -- pure sync, intended for run_in_executor."""
        found: list[dict] = []
        for i in range(len(recs)):
            uid_a = int(recs[i]["user_id"])
            names_a = set(recs[i].get("usernames") or []) | set(recs[i].get("display_names") or [])
            text_a = " ".join(ev_by_uid.get(uid_a, []))
            for j in range(i + 1, len(recs)):
                uid_b = int(recs[j]["user_id"])
                pair = (min(uid_a, uid_b), max(uid_a, uid_b))
                if pair in known:
                    continue
                names_b = set(recs[j].get("usernames") or []) | set(recs[j].get("display_names") or [])
                text_b = " ".join(ev_by_uid.get(uid_b, []))
                name_sim = max(
                    (_name_similarity(n1, n2) for n1 in names_a for n2 in names_b if n1 and n2),
                    default=0.0,
                )
                text_sim = _text_similarity(text_a, text_b) if text_a and text_b else 0.0
                if name_sim >= _NAME_THRESHOLD or text_sim >= _TEXT_THRESHOLD:
                    reasons: list[str] = []
                    if name_sim >= _NAME_THRESHOLD:
                        reasons.append(f"name_similarity:{name_sim:.2f}")
                    if text_sim >= _TEXT_THRESHOLD:
                        reasons.append(f"message_similarity:{text_sim:.2f}")
                    found.append({
                        "uid_a": uid_a,
                        "uid_b": uid_b,
                        "reasons": reasons,
                        "name_score": name_sim,
                        "text_score": text_sim,
                    })
                    known.add(pair)
        return found

    async def _run_full_scan(self, guild_id: int) -> list[dict]:
        """Scan all clanker pairs for connections. Returns only new pairs."""
        records = await self.bot.db.fetch_all(
            "SELECT user_id, usernames, display_names FROM clanker_records WHERE guild_id=$1",
            guild_id,
        )
        if not records or len(records) < 2:
            return []

        # Load all evidence grouped by uid (limit 30 per user)
        all_ev = await self.bot.db.fetch_all(
            "SELECT user_id, content FROM clanker_evidence "
            "WHERE guild_id=$1 ORDER BY user_id, logged_at DESC",
            guild_id,
        )
        ev_by_uid: dict[int, list[str]] = {}
        for row in (all_ev or []):
            uid = int(row["user_id"])
            bucket = ev_by_uid.setdefault(uid, [])
            if len(bucket) < 30:
                bucket.append(row["content"])

        # Load existing pairs so we skip already-known connections
        existing_pairs = await self.bot.db.fetch_all(
            "SELECT user_id_a, user_id_b FROM clanker_connections WHERE guild_id=$1",
            guild_id,
        )
        known: set[tuple[int, int]] = {
            (int(r["user_id_a"]), int(r["user_id_b"])) for r in (existing_pairs or [])
        }

        # O(N^2) comparison dispatched to a thread so the event loop stays free.
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._compare_clanker_pairs, list(records), ev_by_uid, known
        )

    def _scan_role_candidates(
        self,
        guild: discord.Guild,
        base_role: discord.Role,
        stop_role: discord.Role,
    ) -> tuple[list[discord.Member], list[discord.Member]]:
        """Members holding base_role whose highest role is within the requested band."""
        low = min(base_role.position, stop_role.position)
        high = max(base_role.position, stop_role.position)
        candidates: list[discord.Member] = []
        guarded: list[discord.Member] = []
        for member in guild.members:
            if member.bot or base_role not in member.roles:
                continue
            if member.guild_permissions.administrator or member.guild_permissions.manage_roles:
                guarded.append(member)
                continue
            top = member.top_role.position
            if low <= top <= high:
                candidates.append(member)
            else:
                guarded.append(member)
        return candidates, guarded

    def _role_band_label(self, base_role: discord.Role, stop_role: discord.Role) -> str:
        return f"{base_role.mention} through {stop_role.mention}"

    async def _score_scan_member(self, member: discord.Member) -> dict:
        gid = member.guild.id
        names = {member.name, member.display_name}
        pattern_score, pattern_reasons = await self._match_join_against_patterns(member.id, gid, names)
        direct_conns = await self._detect_connections(member.id, gid, names)
        direct_score = max(
            (max(float(c.get("name_score") or 0.0), float(c.get("text_score") or 0.0)) for c in direct_conns),
            default=0.0,
        )
        score = max(pattern_score, direct_score)
        reasons = list(pattern_reasons)
        if direct_conns:
            reasons.append(f"active_connection:{len(direct_conns)}")
        return {
            "member": member,
            "score": min(1.0, score),
            "reasons": reasons,
            "direct_connections": direct_conns,
            "names": names,
        }

    def _scan_components(self, scored: list[dict]) -> list[set[int]]:
        """Build name-similarity components among role-scan candidates."""
        ids = [int(row["member"].id) for row in scored]
        names_by_id = {int(row["member"].id): set(row["names"]) for row in scored}
        adjacency: dict[int, set[int]] = {uid: set() for uid in ids}
        for i, uid_a in enumerate(ids):
            for uid_b in ids[i + 1:]:
                names_a = names_by_id.get(uid_a, set())
                names_b = names_by_id.get(uid_b, set())
                name_sim = max(
                    (_name_similarity(a, b) for a in names_a for b in names_b if a and b),
                    default=0.0,
                )
                if name_sim >= _NAME_THRESHOLD:
                    adjacency[uid_a].add(uid_b)
                    adjacency[uid_b].add(uid_a)

        components: list[set[int]] = []
        seen: set[int] = set()
        for uid in ids:
            if uid in seen:
                continue
            comp = {uid}
            queue = [uid]
            seen.add(uid)
            while queue:
                cur = queue.pop(0)
                for nxt in adjacency.get(cur, set()):
                    if nxt not in seen:
                        seen.add(nxt)
                        comp.add(nxt)
                        queue.append(nxt)
            if len(comp) >= _SCAN_CLUSTER_MIN_SIZE:
                components.append(comp)
        return components

    async def _create_scan_cluster(
        self,
        gid: int,
        member_uids: set[int],
        *,
        label: str,
        confidence: float,
    ) -> int | None:
        existing_id = await self.bot.db.fetch_val(
            """SELECT cluster_id FROM clanker_cluster_members
               WHERE guild_id=$1 AND user_id=ANY($2::bigint[])
               ORDER BY added_at DESC LIMIT 1""",
            gid, list(member_uids),
        )
        if existing_id:
            cluster_id = int(existing_id)
            await self.bot.db.execute(
                """UPDATE clanker_clusters
                   SET confidence=GREATEST(confidence, $1), updated_at=now()
                   WHERE id=$2 AND guild_id=$3""",
                confidence, cluster_id, gid,
            )
        else:
            cluster_id = await self.bot.db.fetch_val(
                "INSERT INTO clanker_clusters (guild_id, label, confidence) VALUES ($1, $2, $3) RETURNING id",
                gid, label[:200], confidence,
            )
            if not cluster_id:
                return None
            cluster_id = int(cluster_id)

        for uid in member_uids:
            await self.bot.db.execute(
                """INSERT INTO clanker_cluster_members (cluster_id, guild_id, user_id)
                   VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                cluster_id, gid, uid,
            )
        await self._audit(
            "scan_cluster_created" if not existing_id else "scan_cluster_updated", gid,
            details={"cluster_id": cluster_id, "members": len(member_uids), "label": label},
        )
        return cluster_id

    async def _save_connections(
        self, new_uid: int, gid: int, connections: list[dict]
    ) -> None:
        for conn in connections:
            other = conn["user_id"]
            uid_a, uid_b = sorted([new_uid, other])
            try:
                await self.bot.db.execute(
                    """INSERT INTO clanker_connections
                       (guild_id, user_id_a, user_id_b, reasons, name_score, text_score)
                       VALUES ($1, $2, $3, $4, $5, $6)
                       ON CONFLICT (guild_id, user_id_a, user_id_b) DO UPDATE
                       SET reasons = $4, name_score = $5, text_score = $6,
                           detected_at = now()""",
                    gid, uid_a, uid_b,
                    conn["reasons"], conn["name_score"], conn["text_score"],
                )
                for uid_self, uid_other in ((new_uid, other), (other, new_uid)):
                    await self.bot.db.execute(
                        "UPDATE clanker_records "
                        "SET linked_accounts = array_append(linked_accounts, $1) "
                        "WHERE guild_id=$2 AND user_id=$3 "
                        "AND NOT ($1 = ANY(linked_accounts))",
                        uid_other, gid, uid_self,
                    )
            except Exception:
                log.debug("clanktank: save_connection failed", exc_info=True)
        asyncio.create_task(self._check_and_form_cluster_bg(new_uid, gid))

    async def _save_scan_connections(self, gid: int, found: list[dict]) -> None:
        for conn in found:
            uid_a, uid_b = conn["uid_a"], conn["uid_b"]
            pair = (min(uid_a, uid_b), max(uid_a, uid_b))
            try:
                await self.bot.db.execute(
                    """INSERT INTO clanker_connections
                       (guild_id, user_id_a, user_id_b, reasons, name_score, text_score)
                       VALUES ($1, $2, $3, $4, $5, $6)
                       ON CONFLICT (guild_id, user_id_a, user_id_b) DO UPDATE
                       SET reasons = $4, name_score = $5, text_score = $6,
                           detected_at = now()""",
                    gid, pair[0], pair[1],
                    conn["reasons"], conn["name_score"], conn["text_score"],
                )
                for uid_self, uid_other in ((uid_a, uid_b), (uid_b, uid_a)):
                    await self.bot.db.execute(
                        "UPDATE clanker_records "
                        "SET linked_accounts = array_append(linked_accounts, $1) "
                        "WHERE guild_id=$2 AND user_id=$3 "
                        "AND NOT ($1 = ANY(linked_accounts))",
                        uid_other, gid, uid_self,
                    )
            except Exception:
                log.debug("clanktank: save_scan_connection failed", exc_info=True)

    # -- Cluster intelligence -------------------------------------------------

    async def _find_connected_component(self, uid: int, gid: int) -> set[int]:
        """BFS through clanker_connections to find all connected active clanker UIDs."""
        visited: set[int] = {uid}
        queue: list[int] = [uid]
        while queue:
            cur = queue.pop(0)
            rows = await self.bot.db.fetch_all(
                """SELECT CASE WHEN user_id_a=$1 THEN user_id_b ELSE user_id_a END AS other
                   FROM clanker_connections
                   WHERE guild_id=$2 AND (user_id_a=$1 OR user_id_b=$1)""",
                cur, gid,
            )
            for row in (rows or []):
                nbr = int(row["other"])
                if nbr not in visited:
                    exists = await self.bot.db.fetch_val(
                        "SELECT 1 FROM clanker_records WHERE user_id=$1 AND guild_id=$2",
                        nbr, gid,
                    )
                    if exists:
                        visited.add(nbr)
                        queue.append(nbr)
        return visited

    async def _check_and_form_cluster_bg(self, uid: int, gid: int) -> None:
        try:
            await self._check_and_form_cluster(uid, gid)
        except Exception:
            log.warning("clanktank: cluster check failed uid=%s gid=%s", uid, gid, exc_info=True)

    async def _check_and_form_cluster(self, uid: int, gid: int) -> None:
        """Form or update a cluster if the connected component >= _CLUSTER_MIN_SIZE."""
        component = await self._find_connected_component(uid, gid)
        if len(component) < _CLUSTER_MIN_SIZE:
            return

        members_list = list(component)
        conn_count = await self.bot.db.fetch_val(
            """SELECT COUNT(*) FROM clanker_connections
               WHERE guild_id=$1 AND user_id_a=ANY($2::bigint[]) AND user_id_b=ANY($2::bigint[])""",
            gid, members_list,
        )
        confidence = _compute_cluster_confidence(len(component), int(conn_count or 0))

        existing_id = await self.bot.db.fetch_val(
            """SELECT cluster_id FROM clanker_records
               WHERE guild_id=$1 AND user_id=ANY($2::bigint[]) AND cluster_id IS NOT NULL
               LIMIT 1""",
            gid, members_list,
        )

        if existing_id:
            await self.bot.db.execute(
                "UPDATE clanker_clusters SET confidence=$1, updated_at=now() WHERE id=$2",
                confidence, existing_id,
            )
            cluster_id = int(existing_id)
        else:
            cluster_id = await self.bot.db.fetch_val(
                "INSERT INTO clanker_clusters (guild_id, confidence) VALUES ($1, $2) RETURNING id",
                gid, confidence,
            )
            if not cluster_id:
                return
            cluster_id = int(cluster_id)

        for member_uid in members_list:
            await self.bot.db.execute(
                "UPDATE clanker_records SET cluster_id=$1 WHERE user_id=$2 AND guild_id=$3",
                cluster_id, member_uid, gid,
            )
            await self.bot.db.execute(
                """INSERT INTO clanker_cluster_members (cluster_id, guild_id, user_id)
                   VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                cluster_id, gid, member_uid,
            )

        await self._save_cluster_patterns(cluster_id, gid, component)

        if not existing_id:
            guild = self.bot.get_guild(gid)
            member_names = []
            for mu in members_list[:12]:
                m = guild.get_member(mu) if guild else None
                member_names.append(str(m) if m else f"ID:{mu}")
            overflow = len(members_list) - 12
            desc = "\n".join(f"  {n}" for n in member_names)
            if overflow > 0:
                desc += f"\n  ...and {overflow} more"
            await self._log_mod(_v2(
                f"Clanker Cluster Formed: [{cluster_id}]",
                color=C_WARNING,
                desc=desc,
                fields=[("Members", str(len(component))), ("Confidence", f"{confidence:.2%}")],
            ))
            await self._audit(
                "cluster_formed", gid,
                details={"cluster_id": cluster_id, "members": len(component), "confidence": confidence},
            )

    async def _save_cluster_patterns(
        self, cluster_id: int, gid: int, member_uids: set[int]
    ) -> None:
        """Extract name patterns from cluster members and upsert into clanker_patterns."""
        records = await self.bot.db.fetch_all(
            "SELECT usernames, display_names FROM clanker_records "
            "WHERE guild_id=$1 AND user_id=ANY($2::bigint[])",
            gid, list(member_uids),
        )
        all_names: list[str] = []
        for r in (records or []):
            all_names.extend(r.get("usernames") or [])
            all_names.extend(r.get("display_names") or [])

        patterns = _extract_name_patterns(all_names)
        for ptype, values in patterns.items():
            for value in values:
                try:
                    await self.bot.db.execute(
                        """INSERT INTO clanker_patterns
                           (guild_id, cluster_id, pattern_type, value)
                           VALUES ($1, $2, $3, $4)
                           ON CONFLICT (guild_id, pattern_type, value) DO UPDATE
                           SET hits      = clanker_patterns.hits + 1,
                               weight    = clanker_patterns.weight + 0.1,
                               updated_at = now()""",
                        gid, cluster_id, ptype, value,
                    )
                except Exception:
                    log.debug("clanktank: save_pattern failed type=%s val=%s", ptype, value)

    async def _match_join_against_patterns(
        self, uid: int, gid: int, names: set[str]
    ) -> tuple[float, list[str]]:
        """Score a new join against the clanker population.

        Primary path: Clank Cohesion Index (CCI) -- feature vectors +
        temporal kernel + spectral cosine cohesion (requires numpy).
        Fallback: heuristic clanker_patterns table matching.
        """
        if _HAS_NUMPY:
            return await self._cci_score_join(uid, gid, names)

        # -- Heuristic fallback (no numpy) ------------------------------------
        score = 0.0
        reasons: list[str] = []
        all_tokens: set[str] = set()
        for name in names:
            all_tokens |= _name_tokens(name)

        all_clean_prefixes3: set[str] = set()
        all_clean_prefixes4: set[str] = set()
        name_structures: set[str] = set()
        avg_len = sum(len(n) for n in names) / len(names) if names else 0
        for name in names:
            clean = re.sub(r"[^a-z]", "", name.lower())
            if len(clean) >= 3:
                all_clean_prefixes3.add(clean[:3])
            if len(clean) >= 4:
                all_clean_prefixes4.add(clean[:4])
            name_structures.add(_name_structure(name))

        len_bucket_join = "short" if avg_len < 7 else ("long" if avg_len >= 14 else "medium")

        pattern_rows = await self.bot.db.fetch_all(
            "SELECT pattern_type, value, weight FROM clanker_patterns "
            "WHERE guild_id=$1 ORDER BY weight DESC",
            gid,
        )

        strong_hits: list[tuple[float, str]] = []
        weak_hits: list[tuple[float, str]] = []

        for row in (pattern_rows or []):
            ptype, value, weight = row["pattern_type"], row["value"], float(row["weight"])
            if ptype == "token" and value in all_tokens:
                strong_hits.append((weight * 0.15, f"token:{value}"))
            elif ptype == "num_suffix" and value == "true":
                if any(re.search(r"\d+$", n) for n in names):
                    strong_hits.append((weight * 0.10, "num_suffix"))
            elif ptype == "separator":
                char = {"hyphen": "-", "underscore": "_", "dot": "."}.get(value, "")
                if char and any(char in n for n in names):
                    strong_hits.append((weight * 0.08, f"sep:{value}"))
            elif ptype == "prefix_4" and value in all_clean_prefixes4:
                strong_hits.append((weight * 0.18, f"prefix4:{value}"))
            elif ptype == "prefix_3" and value in all_clean_prefixes3:
                weak_hits.append((weight * 0.12, f"prefix3:{value}"))
            elif ptype == "len_bucket" and value == len_bucket_join:
                weak_hits.append((weight * 0.06, f"len:{value}"))
            elif ptype == "structure" and value in name_structures:
                weak_hits.append((weight * 0.09, f"struct:{value}"))

        for contrib, reason in strong_hits:
            score += contrib
            reasons.append(reason)

        if strong_hits:
            for contrib, reason in weak_hits:
                score += contrib
                reasons.append(reason)

        hist_rows = await self.bot.db.fetch_all(
            "SELECT usernames FROM clanker_history WHERE guild_id=$1 LIMIT 200",
            gid,
        )
        for h in (hist_rows or []):
            hist_names = set(h.get("usernames") or [])
            sim = max(
                (_name_similarity(n1, n2) for n1 in names for n2 in hist_names if n1 and n2),
                default=0.0,
            )
            if sim >= _NAME_THRESHOLD:
                score += 0.30
                reasons.append(f"hist_match:{sim:.2f}")
                break
            elif sim >= 0.30 and strong_hits:
                score += 0.12
                reasons.append(f"hist_near:{sim:.2f}")
                break

        seen: set[str] = set()
        deduped: list[str] = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                deduped.append(r)
        return min(1.0, score), deduped

    async def _save_to_history(self, user_id: int, guild_id: int, rec: dict | None) -> None:
        """Soft-save a clanker record to clanker_history before deletion."""
        if not rec:
            return
        try:
            await self.bot.db.execute(
                """INSERT INTO clanker_history
                   (user_id, guild_id, usernames, display_names, reason,
                    final_score, cluster_id, clanked_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                user_id, guild_id,
                list(rec.get("usernames") or []),
                list(rec.get("display_names") or []),
                rec.get("reason"),
                int(rec.get("score") or 0),
                rec.get("cluster_id"),
                rec.get("clanked_at"),
            )
        except Exception:
            log.debug("clanktank: save_to_history failed uid=%s", user_id)

    async def _build_connection_tree(
        self,
        uid: int,
        gid: int,
        guild: discord.Guild | None,
    ) -> tuple[list[str], set[int]]:
        """Build ASCII tree lines for uid's connection graph. Returns (lines, component)."""
        component = await self._find_connected_component(uid, gid)

        def name_of(u: int) -> str:
            m = guild.get_member(u) if guild else None
            return str(m) if m else f"ID:{u}"

        if len(component) <= 1:
            return [f"[root]  {name_of(uid)}", "(no connections detected)"], component

        all_uids = list(component)
        conn_rows = await self.bot.db.fetch_all(
            """SELECT user_id_a, user_id_b, reasons FROM clanker_connections
               WHERE guild_id=$1 AND user_id_a=ANY($2::bigint[]) AND user_id_b=ANY($2::bigint[])""",
            gid, all_uids,
        )

        adjacency: dict[int, list[tuple[int, str]]] = {u: [] for u in all_uids}
        for c in (conn_rows or []):
            a, b = int(c["user_id_a"]), int(c["user_id_b"])
            reasons = c.get("reasons") or []
            label = reasons[0] if reasons else ""
            adjacency[a].append((b, label))
            adjacency[b].append((a, label))

        rec = await self._get_record(uid, gid)
        cluster_id = rec.get("cluster_id") if rec else None
        cluster_tag = f"  [cluster:{cluster_id}]" if cluster_id else ""
        lines: list[str] = [f"[root]  {name_of(uid)}{cluster_tag}"]
        visited: set[int] = {uid}

        def render(node: int, prefix: str) -> None:
            children = [(nbr, lbl) for nbr, lbl in adjacency.get(node, []) if nbr not in visited]
            for i, (child, label) in enumerate(children):
                is_last = (i == len(children) - 1)
                connector = "`-- " if is_last else "|-- "
                ext = "    " if is_last else "|   "
                label_str = f"  ({label})" if label else ""
                lines.append(f"{prefix}{connector}{name_of(child)}{label_str}")
                visited.add(child)
                render(child, prefix + ext)

        render(uid, "")
        return lines, component

    async def _cmd_cluster_detail(self, ctx: DiscoContext, cluster_id: int) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        cluster = await self.bot.db.fetch_one(
            "SELECT * FROM clanker_clusters WHERE id=$1 AND guild_id=$2",
            cluster_id, ctx.guild.id,
        )
        if not cluster:
            await ctx.reply_error(f"Cluster {cluster_id} not found in this guild.")
            return

        member_rows = await self.bot.db.fetch_all(
            """SELECT ccm.user_id, cr.score, cr.message_count, cr.clanked_at, cr.reason
               FROM clanker_cluster_members ccm
               LEFT JOIN clanker_records cr
                   ON cr.user_id = ccm.user_id AND cr.guild_id = ccm.guild_id
               WHERE ccm.cluster_id=$1
               ORDER BY cr.score DESC NULLS LAST""",
            cluster_id,
        )
        pattern_rows = await self.bot.db.fetch_all(
            "SELECT pattern_type, value, hits, weight FROM clanker_patterns "
            "WHERE guild_id=$1 AND cluster_id=$2 ORDER BY weight DESC LIMIT 10",
            ctx.guild.id, cluster_id,
        )

        conf = float(cluster.get("confidence") or 0.0)
        created = cluster.get("created_at")
        cleaved = cluster.get("cleaved_at")
        label = cluster.get("label") or "Unlabeled"
        total_members = len(member_rows or [])

        # Build per-member lines (all of them, paginated)
        all_member_lines: list[str] = []
        for r in (member_rows or []):
            m_uid = int(r["user_id"])
            m = ctx.guild.get_member(m_uid)
            mname = str(m) if m else f"ID:{m_uid}"
            sc = r.get("score") or 0
            msgs = r.get("message_count") or 0
            tank_tag = "(tank)" if await self.is_clanker(m_uid, ctx.guild.id) else "(free)"
            all_member_lines.append(f"{mname}  {tank_tag}  score:{sc}  msgs:{msgs}")

        pattern_lines: list[str] = [
            f"[{p['pattern_type']}]  {p['value']}  hits:{p['hits']}  w:{float(p['weight']):.2f}"
            for p in (pattern_rows or [])
        ]

        text_pages: list[str] = []

        # Page 1: metadata + patterns + first batch of members
        chunk = 12
        p1_members = all_member_lines[:chunk]
        overflow = total_members - chunk
        p1_mem_text = "```\n" + "\n".join(p1_members) + (f"\n...+{overflow} more (next pages)" if overflow > 0 else "") + "\n```"
        p1_fields = [
            ("Confidence", f"{conf:.2%}"),
            ("Members", str(total_members)),
            ("Formed", fmt_ts(created) if created else "?"),
            *([("Cleaved", fmt_ts(cleaved) if cleaved else "?")] if cleaved else []),
            (f"Members (1/{max(1, math.ceil(total_members / chunk))})", p1_mem_text),
            *([("Learned patterns", "```\n" + "\n".join(pattern_lines) + "\n```")] if pattern_lines else []),
        ]
        p1_body = "\n".join(f"**{k}**\n{v}" for k, v in p1_fields)
        text_pages.append(p1_body)

        # Additional pages for remaining members
        for page_idx, start in enumerate(range(chunk, total_members, chunk), start=2):
            batch = all_member_lines[start: start + chunk]
            total_pages = max(1, math.ceil(total_members / chunk))
            text_pages.append(
                f"**Members {start + 1}-{min(start + chunk, total_members)} of {total_members}**\n"
                + "```\n" + "\n".join(batch) + "\n```"
                + f"\n-# cluster id:{cluster_id}"
            )

        title = f"Cluster [{cluster_id}]: {label}"
        footer_hint = f"cluster id:{cluster_id} -- ,clanker cluster cleave {cluster_id} to mass-clank"
        if len(text_pages) == 1:
            await ctx.reply(
                view=_v2(title, color=C_NAVY, desc=text_pages[0], footer=footer_hint),
                mention_author=False,
            )
        else:
            await ctx.reply(
                view=_PageView(ctx.author.id, text_pages, title=title, color=C_NAVY),
                mention_author=False,
            )

    # -- Audit log ------------------------------------------------------------

    async def _audit(
        self,
        event_type: str,
        guild_id: int,
        *,
        user_id: int | None = None,
        actor_id: int | None = None,
        details: dict | None = None,
    ) -> None:
        try:
            await self.bot.db.execute(
                """INSERT INTO clanker_audit_log
                   (guild_id, user_id, actor_id, event_type, details)
                   VALUES ($1, $2, $3, $4, $5)""",
                guild_id, user_id, actor_id, event_type,
                _json.dumps(details) if details else None,
            )
        except Exception:
            log.debug("clanktank: audit failed type=%s", event_type)

    # -- Channel helpers ------------------------------------------------------

    def _tank_channel(self) -> discord.TextChannel | None:
        cid = Config.CLANKTANK_CHANNEL_ID
        ch = self.bot.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _log_mod(self, view: discord.ui.LayoutView) -> None:
        cid = Config.CLANKTANK_LOG_CHANNEL_ID
        if not cid:
            return
        ch = self.bot.get_channel(cid)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(cid)
            except Exception:
                return
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        try:
            await ch.send(view=view)
        except Exception:
            log.debug("clanktank: log_mod send failed")

    async def _clamp_ambient_guard(self, message: discord.Message) -> None:
        """Ambient guard: detect URL/address/scam patterns in configured guard channels."""
        gid = message.guild.id  # type: ignore[union-attr]
        channel = message.channel
        try:
            s = await self.bot.db.get_guild_settings(gid)
        except Exception:
            return
        guard_ids: list[int] = [int(x) for x in (s.get("clamp_channel_ids") or [])]
        if not guard_ids:
            return
        ch_id = int(getattr(channel, "id", 0) or 0)
        if ch_id not in guard_ids:
            return
        if not bool(s.get("clamp_clear_scams", True)):
            return
        content = message.content or ""
        hit_url  = bool(_URL_RE.search(content))
        hit_addr = bool(_CRYPTO_RE.search(content))
        if not (hit_url or hit_addr):
            return
        uid = message.author.id
        log.warning(
            "clanktank: ambient guard triggered uid=%s channel=%s hit_url=%s hit_addr=%s "
            "clasp_auto_delete=%s",
            uid, ch_id, hit_url, hit_addr, s.get("clasp_auto_delete"),
        )
        deleted = False
        if bool(s.get("clasp_auto_delete", True)):
            deleted = await self._delete_clanker_message(
                message,
                reason="Clasp guard: URL/address detected in guard channel",
            )
        if bool(s.get("clasp_auto_mute", False)):
            member = message.guild.get_member(uid)  # type: ignore[union-attr]
            if member:
                try:
                    await member.timeout(
                        timedelta(minutes=15),
                        reason="Clasp guard: URL/address posted in guard channel",
                    )
                except Exception:
                    pass
        await self._audit(
            "clasp_ambient_guard", gid, user_id=uid,
            details={
                "channel_id": ch_id,
                "content": content[:500],
                "deleted": deleted,
                "has_url": hit_url,
                "has_addr": hit_addr,
            },
        )
        kind = "URL" if hit_url else "crypto address"
        await self._log_mod(_v2(
            "Clasp Guard: Ambient Detection",
            color=C_AMBER,
            fields=[
                ("User", f"{message.author} ({uid})"),
                ("Channel", f"<#{ch_id}>"),
                ("Detected", kind),
                ("Deleted", "yes" if deleted else "no -- check bot logs"),
                ("Content", content[:400] or "(empty)"),
            ],
        ))

    # -- Scam hunter channel --------------------------------------------------

    async def _scam_hunter_check(self, message: discord.Message) -> None:
        """Auto-clank any users reported by a whitelisted scam hunter."""
        if message.guild is None:
            return
        gid = message.guild.id
        uid = message.author.id
        try:
            s = await self.bot.db.get_guild_settings(gid)
        except Exception:
            return

        report_ch = s.get("scam_report_channel")
        if not report_ch:
            return
        if int(getattr(message.channel, "id", 0) or 0) != int(report_ch):
            return

        hunter_ids: list[int] = [int(x) for x in (s.get("scam_hunter_ids") or [])]
        if uid not in hunter_ids:
            return

        # Collect targets from @mentions and raw 17-20 digit user IDs in content.
        targets: set[int] = set()
        for m in (message.mentions or []):
            if not m.bot:
                targets.add(m.id)
        for match in re.finditer(r"\b(\d{17,20})\b", message.content or ""):
            targets.add(int(match.group(1)))
        targets.discard(uid)
        targets.discard(self.bot.user.id if self.bot.user else 0)

        if not targets:
            try:
                await message.add_reaction("❓")  # ?
            except Exception:
                pass
            return

        guild     = message.guild
        actor     = guild.me
        if actor is None:
            return

        clanked: list[int] = []
        already:  list[int] = []
        failed:   list[int] = []

        for target_id in targets:
            if await self.is_clanker(target_id, gid):
                already.append(target_id)
                continue

            member_obj = guild.get_member(target_id)
            if member_obj is None:
                try:
                    member_obj = await guild.fetch_member(target_id)
                except Exception:
                    failed.append(target_id)
                    continue

            if member_obj.bot:
                continue
            if (member_obj.guild_permissions.manage_messages
                    or member_obj.guild_permissions.administrator):
                failed.append(target_id)
                continue

            try:
                await self._do_clank(
                    member_obj, actor,
                    f"Scam report by hunter <@{uid}>",
                    duration_s=None,
                )
                clanked.append(target_id)
            except Exception:
                log.warning(
                    "clanktank: scam hunter clank failed target=%s gid=%s",
                    target_id, gid, exc_info=True,
                )
                failed.append(target_id)

        # React to give instant feedback in the report channel.
        try:
            if clanked or already:
                await message.add_reaction("✅")  # checkmark
            if failed:
                await message.add_reaction("❌")  # X
        except Exception:
            pass

        if not clanked and not already:
            return

        all_actioned = clanked + already
        await self._log_mod(_v2(
            "Scam Hunter Report",
            color=C_WARNING,
            fields=[
                ("Reporter", f"{message.author} ({uid})"),
                ("Clanked now", ", ".join(f"<@{t}>" for t in clanked) or "none"),
                ("Already clanked", ", ".join(f"<@{t}>" for t in already) or "none"),
                *([("Failed (mods or not found)", ", ".join(f"`{t}`" for t in failed))] if failed else []),
                *([("Report content", (message.content or "")[:500])] if message.content else []),
            ],
        ))

        if clanked:
            tank = self._tank_channel()
            if tank:
                names = ", ".join(f"<@{t}>" for t in clanked)
                try:
                    await tank.send(
                        f"{names} clanked via scam hunter report.",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except Exception:
                    pass

    # -- Clanktank analytics query from outside tank channel ------------------

    _TANK_QUERY_KEYWORDS = frozenset({
        "clanktank", "clanker", "clankers", "tank", "who's clanked",
        "who is clanked", "scammers", "scammer", "the tank",
        "in containment", "contained",
    })

    async def _handle_tank_query(self, message: discord.Message, guild_id: int) -> None:
        """Respond to clanktank status questions asked to Disco outside the tank channel."""
        try:
            stats = await self.bot.db.fetch_one(
                """SELECT
                       COUNT(*)                                                        AS total,
                       SUM(CASE WHEN left_at IS NOT NULL THEN 1 ELSE 0 END)           AS away,
                       MAX(score)                                                      AS top_score,
                       SUM(message_count)                                              AS total_messages,
                       SUM(escape_attempts)                                            AS total_escapes,
                       SUM(blocked_command_count)                                      AS total_blocked,
                       COUNT(DISTINCT cluster_id) FILTER (WHERE cluster_id IS NOT NULL) AS clusters
                   FROM clanker_records WHERE guild_id=$1""",
                guild_id,
            )
            recent = await self.bot.db.fetch_all(
                """SELECT user_id, score, clanked_at, message_count
                   FROM clanker_records
                   WHERE guild_id=$1
                   ORDER BY clanked_at DESC LIMIT 5""",
                guild_id,
            )
            top_scorer = await self.bot.db.fetch_one(
                """SELECT user_id, score FROM clanker_records
                   WHERE guild_id=$1 ORDER BY score DESC LIMIT 1""",
                guild_id,
            )
        except Exception:
            log.debug("clanktank: tank query failed gid=%s", guild_id, exc_info=True)
            return

        if not stats or not int(stats.get("total") or 0):
            await message.reply(
                "the tank is empty. no active clankers on record.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        guild = message.guild
        total     = int(stats.get("total") or 0)
        away      = int(stats.get("away") or 0)
        in_srv    = total - away
        top_score = int(stats.get("top_score") or 0)
        t_msgs    = int(stats.get("total_messages") or 0)
        t_esc     = int(stats.get("total_escapes") or 0)
        t_blk     = int(stats.get("total_blocked") or 0)
        clusters  = int(stats.get("clusters") or 0)

        recent_lines: list[str] = []
        for r in (recent or []):
            r_uid = int(r["user_id"])
            m = guild.get_member(r_uid) if guild else None
            name = str(m) if m else f"ID:{r_uid}"
            sc = int(r.get("score") or 0)
            msgs = int(r.get("message_count") or 0)
            ts = r.get("clanked_at")
            age = _duration_str(ts.timestamp() if isinstance(ts, datetime) else float(ts)) if ts else "?"
            recent_lines.append(f"  {name} -- score {sc}, {msgs} msgs, {age} in tank")

        top_name = ""
        if top_scorer:
            ts_uid = int(top_scorer["user_id"])
            ts_m = guild.get_member(ts_uid) if guild else None
            top_name = str(ts_m) if ts_m else f"ID:{ts_uid}"

        tank_ref = f"<#{Config.CLANKTANK_CHANNEL_ID}>" if Config.CLANKTANK_CHANNEL_ID else "the tank"

        try:
            await message.reply(
                view=_v2(
                    "Clanktank Status",
                    color=C_NAVY,
                    fields=[
                        ("In server", str(in_srv)),
                        ("Left server", str(away)),
                        ("Total tracked", str(total)),
                        ("Clusters detected", str(clusters)),
                        ("Top score", f"{top_score}" + (f" ({top_name})" if top_name else "")),
                        ("Total msgs logged", str(t_msgs)),
                        ("Total escape attempts", str(t_esc)),
                        ("Total commands blocked", str(t_blk)),
                        ("Recent clanks", "\n".join(recent_lines) if recent_lines else "none"),
                    ],
                    footer=f"Full details in {tank_ref}",
                ),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            pass

    # -- Disco responses ------------------------------------------------------

    async def _disco_reply(
        self,
        message: discord.Message,
        rec: dict,
        pool: list[list[str]] | None = None,
        *,
        prob: float = _RESPONSE_PROB,
        force_tier: int | None = None,
    ) -> None:
        if isinstance(message.channel, discord.Thread):
            return
        if random.random() > prob:
            return
        uid = int(rec.get("user_id", message.author.id))
        gid = message.guild.id if message.guild else 0
        eff = await self._effective_score(uid, gid, rec)
        tier = force_tier if force_tier is not None else _score_tier(eff)
        text = _pick(pool or _RESPONSES, tier, rec)
        try:
            await message.reply(
                text,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            pass

    async def _tank_send(self, guild_id: int, user_id: int, rec: dict, pool: list[list[str]]) -> None:
        tank = self._tank_channel()
        if not tank:
            return
        eff = await self._effective_score(user_id, guild_id, rec)
        tier = _score_tier(eff)
        text = _pick(pool, tier, rec)
        try:
            await tank.send(
                f"<@{user_id}> {text}",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except Exception:
            pass

    async def _tank_broadcast(
        self, guild_id: int, rec: dict, pool: list[list[str]], label: str = "", *, member_name: str = ""
    ) -> None:
        """Send a message to tank channel. Pass member_name to mention the user (e.g. on leave)."""
        tank = self._tank_channel()
        if not tank:
            return
        uid = int(rec.get("user_id", 0))
        eff = await self._effective_score(uid, guild_id, rec)
        tier = _score_tier(eff)
        augmented = dict(rec)
        if member_name:
            augmented["_name"] = member_name
        text = _pick(pool, tier, augmented)
        try:
            prefix = f"**{label}**: " if label else ""
            # Mention the user even after they've left -- Discord still renders <@id>
            mention = f"<@{uid}> " if member_name else ""
            await tank.send(
                mention + prefix + text,
                allowed_mentions=discord.AllowedMentions(users=bool(member_name)),
            )
        except Exception:
            pass

    # -- Global prefix-command check ------------------------------------------

    async def _global_prefix_check(self, ctx: DiscoContext) -> bool:
        if not ctx.guild:
            return True
        if not await self.is_clanker(ctx.author.id, ctx.guild.id):
            return True
        # Allow clankers to use ,clanker escape to find their escape room thread.
        root = getattr(ctx.command, "root_parent", ctx.command) if ctx.command else None
        if getattr(root, "name", "") == "clanker" and getattr(ctx.command, "name", "") == "escape":
            return True
        rec = await self._inc(
            ctx.author.id, ctx.guild.id,
            blocked_command_count=1, score=_SCORE_BLOCKED,
        )
        await self._audit(
            "command_blocked", ctx.guild.id,
            user_id=ctx.author.id,
            details={"command": ctx.invoked_with, "content": (ctx.message.content or "")[:200]},
        )
        if rec and ctx.message:
            await self._disco_reply(
                ctx.message, rec, _BLOCKED_RESPONSES, prob=_RESPONSE_PROB_BLOCKED,
            )
        raise commands.CheckFailure()

    # -- Events ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        uid, gid = after.id, after.guild.id
        if not await self.is_clanker(uid, gid):
            return

        allowed = self._allowed_role_ids(after.guild)
        extra = {r.id for r in after.roles} - allowed
        if not extra:
            return

        now = time.time()
        if now - self._enforce_ts.get(uid, 0.0) < _ENFORCE_CD:
            return
        self._enforce_ts[uid] = now

        to_remove = [r for r in after.roles if r.id in extra]
        try:
            await after.remove_roles(*to_remove, reason="Clanktank role lock")
        except discord.Forbidden:
            log.warning("clanktank: cannot remove roles uid=%s -- missing permissions", uid)
            return
        except Exception:
            log.debug("clanktank: role revert failed uid=%s", uid, exc_info=True)
            return

        rec = await self._inc(uid, gid, escape_attempts=1, score=_SCORE_ESCAPE)
        await self._audit(
            "escape_attempt", gid, user_id=uid,
            details={"roles_added": [r.name for r in to_remove]},
        )

        ts = rec.get("clanked_at") if rec else None
        dur = _duration_str(ts.timestamp() if isinstance(ts, datetime) else float(ts)) if ts else "?"
        await self._log_mod(_v2(
            "Escape Attempt Reverted",
            color=C_WARNING,
            fields=[
                ("User", f"{after} ({uid})"),
                ("Time in tank", dur),
                ("Unauthorized roles", ", ".join(r.name for r in to_remove)),
                ("Escape attempts", str(rec.get("escape_attempts") if rec else "?")),
                ("Messages sent", str(rec.get("message_count") if rec else "?")),
                ("Score", str(rec.get("score") if rec else "?")),
            ],
        ))

        if rec:
            await self._tank_send(gid, uid, rec, _ESCAPE_RESPONSES)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        uid, gid = member.id, member.guild.id
        if not await self.is_clanker(uid, gid):
            return

        try:
            rec = await self.bot.db.fetch_one(
                """UPDATE clanker_records
                   SET leave_count = leave_count + 1,
                       score = score + $3,
                       left_at = now()
                   WHERE user_id=$1 AND guild_id=$2
                   RETURNING *""",
                uid, gid, _SCORE_LEAVE,
            )
        except Exception:
            rec = None

        await self._audit(
            "server_leave", gid, user_id=uid,
            details={
                "leave_count": int(rec.get("leave_count") or 0) if rec else None,
                "score": int(rec.get("score") or 0) if rec else None,
            },
        )

        ts = rec.get("clanked_at") if rec else None
        dur = _duration_str(ts.timestamp() if isinstance(ts, datetime) else float(ts)) if ts else "?"
        await self._log_mod(_v2(
            "Clanker Left Server",
            color=C_WARNING,
            fields=[
                ("User", f"{member} ({uid})"),
                ("Time in tank", dur),
                ("Score", str(rec.get("score") if rec else "?")),
                ("Leave count", str(rec.get("leave_count") if rec else "?")),
                ("Reason on record", (rec.get("reason") or "none") if rec else "none"),
            ],
        ))

        if rec:
            await self._tank_broadcast(gid, rec, _LEAVE_RESPONSES, member_name=str(member))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        uid, gid = member.id, member.guild.id
        if not await self.is_clanker(uid, gid):
            names = {member.name, member.display_name}
            age_str = _age_str(member.created_at) if member.created_at else "?"
            actor = member.guild.me

            # -- Pass 1a: scam-keyword auto-clank (aggressive, no review needed) --
            scam_kw = _scam_name_hit(names)
            if scam_kw and actor:
                try:
                    await self._do_clank(
                        member, actor,
                        f"Auto-clank: scam keyword in username ({scam_kw!r})",
                        None, defer_purge=True,
                    )
                    await self._log_mod(_v2(
                        "Auto-Clank: Scam Username",
                        color=C_WARNING,
                        fields=[
                            ("User", f"{member} ({uid})"),
                            ("Account age", age_str),
                            ("Matched keyword", scam_kw),
                            ("Names", ", ".join(names)),
                        ],
                        footer="Automatic containment -- scam-support keyword detected on join.",
                    ))
                    await self._audit(
                        "auto_clank_scam_keyword", gid, user_id=uid,
                        details={"keyword": scam_kw, "names": list(names), "account_age": age_str},
                    )
                except Exception:
                    log.exception("clanktank: scam-keyword auto-clank failed uid=%s", uid)
                return

            # -- Pass 1b: celebrity / public-figure impersonation auto-clank ------
            celeb_hit = _celeb_name_hit(names)
            if celeb_hit and actor:
                try:
                    await self._do_clank(
                        member, actor,
                        f"Auto-clank: public figure impersonation ({celeb_hit!r})",
                        None, defer_purge=True,
                    )
                    await self._log_mod(_v2(
                        "Auto-Clank: Celebrity / Public Figure Impersonation",
                        color=C_WARNING,
                        fields=[
                            ("User", f"{member} ({uid})"),
                            ("Account age", age_str),
                            ("Matched figure", celeb_hit),
                            ("Names", ", ".join(names)),
                        ],
                        footer="Automatic containment -- known public figure impersonation detected on join.",
                    ))
                    await self._audit(
                        "auto_clank_celeb_impersonation", gid, user_id=uid,
                        details={"figure": celeb_hit, "names": list(names), "account_age": age_str},
                    )
                except Exception:
                    log.exception("clanktank: celeb-impersonation auto-clank failed uid=%s", uid)
                return

            # -- Pass 2: CCI / pattern scoring + direct connections ---------------
            try:
                score, reasons = await self._match_join_against_patterns(uid, gid, names)
                direct_conns = await self._detect_connections(uid, gid, names)

                # 100% CCI confidence: auto-clank without review
                if score >= _JOIN_AUTO_CLANK_SCORE and actor:
                    try:
                        await self._do_clank(
                            member, actor,
                            f"Auto-clank: CCI score {score:.0%} (100% confidence)",
                            None, defer_purge=True,
                        )
                        await self._log_mod(_v2(
                            "Auto-Clank: CCI 100% Confidence",
                            color=C_WARNING,
                            fields=[
                                ("User", f"{member} ({uid})"),
                                ("Account age", age_str),
                                ("CCI score", f"{score:.0%}"),
                                ("Names", ", ".join(names)),
                                *([("Signals", ", ".join(reasons[:6]))] if reasons else []),
                            ],
                            footer="Automatic containment -- CCI score reached maximum confidence.",
                        ))
                        await self._audit(
                            "auto_clank_cci", gid, user_id=uid,
                            details={"score": score, "reasons": reasons[:8], "names": list(names)},
                        )
                    except Exception:
                        log.exception("clanktank: CCI auto-clank failed uid=%s", uid)
                    return

                if score < _JOIN_ALERT_THRESHOLD and not direct_conns:
                    return

                # -- Pass 3: suspicious alert (human review required) -------------

                # If directly connected to a cluster, pre-add this user to that cluster.
                cluster_id_added: int | None = None
                if direct_conns:
                    connected_uids = [c["user_id"] for c in direct_conns]
                    found_cid = await self.bot.db.fetch_val(
                        """SELECT cluster_id FROM clanker_records
                           WHERE guild_id=$1 AND user_id=ANY($2::bigint[])
                             AND cluster_id IS NOT NULL LIMIT 1""",
                        gid, connected_uids,
                    )
                    if found_cid:
                        await self.bot.db.execute(
                            """INSERT INTO clanker_cluster_members (cluster_id, guild_id, user_id)
                               VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                            int(found_cid), gid, uid,
                        )
                        cluster_id_added = int(found_cid)

                title = "Suspicious Join"
                if cluster_id_added:
                    title = f"Suspicious Join -- Added to Cluster [{cluster_id_added}]"
                elif direct_conns:
                    title = "Suspicious Join -- Connected to Active Clanker(s)"

                _sj_fields: list[tuple[str, str]] = [
                    ("User", f"{member} ({uid})"),
                    ("Account age", age_str),
                ]
                if score >= _JOIN_ALERT_THRESHOLD:
                    _sj_fields.append(("Pattern score", f"{score:.0%}"))
                if cluster_id_added:
                    _sj_fields.append(("Cluster", f"[{cluster_id_added}] -- use ,clanker cluster {cluster_id_added}"))
                if reasons:
                    _sj_fields.append(("Matched patterns", ", ".join(reasons[:6])))

                # Build connection comparison list (connected + near-misses)
                near_misses = await self._detect_near_misses(uid, gid, names)
                comp_lines: list[str] = []
                for c in direct_conns[:5]:
                    m2 = member.guild.get_member(c["user_id"])
                    cname = f"{m2} ({c['user_id']})" if m2 else f"ID:{c['user_id']}"
                    ns = float(c.get("name_score") or 0.0)
                    ts = float(c.get("text_score") or 0.0)
                    parts: list[str] = []
                    if ns > 0:
                        parts.append(f"name {ns:.0%}")
                    if ts > 0:
                        parts.append(f"msg {ts:.0%}")
                    score_str = " | ".join(parts) if parts else c["reasons"][0] if c.get("reasons") else "?"
                    comp_lines.append(f"**{cname}** -- connected ({score_str})")
                for nm in near_misses[:4]:
                    m2 = member.guild.get_member(nm["user_id"])
                    nname = f"{m2} ({nm['user_id']})" if m2 else f"ID:{nm['user_id']}"
                    ns = float(nm.get("name_score") or 0.0)
                    ts_score = float(nm.get("text_score") or 0.0)
                    parts = []
                    if ns > 0:
                        parts.append(f"name {ns:.0%}")
                    if ts_score > 0:
                        parts.append(f"msg {ts_score:.0%}")
                    score_str = " | ".join(parts) if parts else "?"
                    comp_lines.append(f"{nname} -- not connected ({score_str})")

                if comp_lines:
                    _sj_fields.append(("Similarity comparison", "\n".join(comp_lines)))

                # Structural fingerprint
                fingerprint_tag: str | None = None
                total_similar = len(direct_conns) + len(near_misses)
                if total_similar >= 3:
                    fp_groups: list[list[str]] = [list(names)]
                    for c in direct_conns:
                        m2 = member.guild.get_member(c["user_id"])
                        cname = str(m2) if m2 else ""
                        fp_groups.append([cname] if cname else [])
                    for nm_entry in near_misses:
                        fp_groups.append(list(nm_entry.get("other_names") or []))
                    fingerprint_tag = _build_structural_fingerprint(fp_groups)

                if fingerprint_tag:
                    _sj_fields.append(("Structural fingerprint", f"[{fingerprint_tag}]"))

                _sj_fields.append(("Names", ", ".join(names)))
                _sj_footer = (
                    f"Pre-added to cluster {cluster_id_added}. Use ,clanker cluster cleave {cluster_id_added} to mass-clank."
                    if cluster_id_added else "Alert only -- no automatic action taken."
                )
                await self._log_mod(_v2(title, color=C_WARNING, fields=_sj_fields, footer=_sj_footer))
                await self._audit(
                    "suspicious_join", gid, user_id=uid,
                    details={
                        "score": score,
                        "reasons": reasons[:8],
                        "names": list(names),
                        "cluster_id_added": cluster_id_added,
                        "direct_connections": len(direct_conns),
                        "near_misses": len(near_misses),
                        "fingerprint": fingerprint_tag,
                    },
                )
            except Exception:
                log.debug("clanktank: join check failed uid=%s", uid, exc_info=True)
            return

        clanker_role = self._clanker_role(member.guild)
        if clanker_role is None:
            log.warning("clanktank: rejoin detected but no clanker role found gid=%s", gid)
            return

        try:
            await member.add_roles(clanker_role, reason="Clanktank: rejoin re-containment")
        except discord.Forbidden:
            log.warning("clanktank: cannot re-add clanker role on rejoin uid=%s", uid)
            return
        except Exception:
            log.debug("clanktank: rejoin add_role failed uid=%s", uid, exc_info=True)
            return

        try:
            rec = await self.bot.db.fetch_one(
                """UPDATE clanker_records
                   SET rejoin_count = rejoin_count + 1,
                       escape_attempts = escape_attempts + 1,
                       score = score + $3,
                       left_at = NULL
                   WHERE user_id=$1 AND guild_id=$2
                   RETURNING *""",
                uid, gid, _SCORE_ESCAPE,
            )
        except Exception:
            rec = None

        await self._audit(
            "rejoin_escape", gid, user_id=uid,
            details={"rejoin_count": int(rec.get("rejoin_count") or 0) if rec else None},
        )

        await self._log_mod(_v2(
            "Clanker Rejoined -- Containment Re-applied",
            color=C_WARNING,
            fields=[
                ("User", f"{member} ({uid})"),
                ("Score", str(rec.get("score") if rec else "?")),
                ("Rejoins", str(rec.get("rejoin_count") if rec else "?")),
                ("Total escapes", str(rec.get("escape_attempts") if rec else "?")),
            ],
        ))

        if rec:
            await self._tank_send(gid, uid, rec, _REJOIN_RESPONSES)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or message.webhook_id:
            return
        uid, gid = message.author.id, message.guild.id

        # Handle clanktank status queries from non-clankers who mention the bot.
        bot_user = self.bot.user
        if bot_user and any(m.id == bot_user.id for m in message.mentions):
            content_lower = (message.content or "").lower()
            if any(kw in content_lower for kw in self._TANK_QUERY_KEYWORDS):
                if not await self.is_clanker(uid, gid):
                    await self._handle_tank_query(message, gid)
                    return

        if not await self.is_clanker(uid, gid):
            await self._scam_hunter_check(message)
            return

        await self._clamp_ambient_guard(message)
        content = message.content or ""
        in_tank_surface = self._is_tank_surface(message.channel)
        _gs = await self.bot.db.get_guild_settings(gid)

        # Detect staff user pings.
        # Use a loop instead of a comprehension: message.mentions can include discord.User
        # objects (members not in the guild or not cached). _is_staff needs a discord.Member
        # that has guild_permissions; calling it with a discord.User raises AttributeError
        # which would propagate and skip the URL deletion block entirely.
        mentioned_staff: list[discord.Member] = []
        for _m in (message.mentions or []):
            if _m.bot or _m.id == uid:
                continue
            _member_obj = message.guild.get_member(_m.id)
            if _member_obj is None:
                continue  # not in guild or not cached -- cannot be staff
            if await self._is_staff(_member_obj):
                mentioned_staff.append(_member_obj)
        if mentioned_staff:
            rec = await self._inc(uid, gid, score=_SCORE_STAFF_DM, blocked_command_count=1)
            await self._store_evidence_text(uid, gid, content, message.channel.id, "staff_ping")
            await self._audit(
                "staff_ping", gid, user_id=uid,
                details={
                    "staff_ids": [m.id for m in mentioned_staff],
                    "content": content[:500],
                    "channel_id": message.channel.id,
                },
            )
            deleted = await self._delete_clanker_message(
                message,
                reason="Clanktank: deleting staff ping from contained account",
            )
            await self._log_mod(_v2(
                "Staff Ping from Clanker",
                color=C_WARNING,
                fields=[
                    ("Clanker", f"{message.author} ({uid})"),
                    ("Staff pinged", ", ".join(f"<@{m.id}>" for m in mentioned_staff)),
                    ("Score", str(rec.get("score") if rec else "?")),
                    ("Deleted", "yes" if deleted else "no (see bot logs)"),
                    ("Message", content[:500] or "(empty)"),
                ],
            ))
            await self._audit(
                "message_deleted", gid, user_id=uid,
                details={
                    "kind": "staff_ping",
                    "channel_id": message.channel.id,
                    "deleted": deleted,
                },
            )
            if rec:
                if in_tank_surface and not deleted:
                    await self._disco_reply(message, rec, _STAFF_PING_RESPONSES, prob=1.0)
                else:
                    await self._tank_send(gid, uid, rec, _STAFF_PING_RESPONSES)
            return

        # Detect @role pings.
        if message.role_mentions:
            rec = await self._inc(uid, gid, score=_SCORE_ROLE_PING, blocked_command_count=1)
            role_names = ", ".join(f"@{r.name}" for r in message.role_mentions)
            await self._store_evidence_text(uid, gid, content, message.channel.id, "role_ping")
            await self._audit(
                "role_ping", gid, user_id=uid,
                details={
                    "roles": [r.name for r in message.role_mentions],
                    "content": content[:500],
                    "channel_id": message.channel.id,
                },
            )
            await self._log_mod(_v2(
                "Role Ping from Clanker",
                color=C_WARNING,
                fields=[
                    ("Clanker", f"{message.author} ({uid})"),
                    ("Roles pinged", role_names[:200]),
                    ("Score", str(rec.get("score") if rec else "?")),
                    ("Message", content[:300] or "(empty)"),
                ],
            ))
            deleted = await self._delete_clanker_message(
                message,
                reason="Clanktank: deleting role ping from contained account",
            )
            await self._audit(
                "message_deleted", gid, user_id=uid,
                details={
                    "kind": "role_ping",
                    "channel_id": message.channel.id,
                    "deleted": deleted,
                },
            )
            if rec:
                if in_tank_surface and not deleted:
                    await self._disco_reply(message, rec, _ROLE_PING_RESPONSES, prob=1.0)
                else:
                    await self._tank_send(gid, uid, rec, _ROLE_PING_RESPONSES)
            return

        # Detect and delete URLs / crypto addresses (respects clamp toggles).
        _has_url = bool(_URL_RE.search(content))
        _has_addr = bool(_CRYPTO_RE.search(content))
        _clear_urls = bool(_gs.get("clamp_clear_urls", True))
        _clear_addrs = bool(_gs.get("clamp_clear_addresses", True))
        if (_has_url and _clear_urls) or (_has_addr and _clear_addrs):
            deleted = await self._delete_clanker_message(
                message,
                reason="Clanktank: deleting URL or address from contained account",
            )
            await self._store_evidence_text(uid, gid, content, message.channel.id, "url_blocked")
            rec = await self._inc(uid, gid, blocked_command_count=1, score=_SCORE_URL)
            await self._audit(
                "url_blocked", gid, user_id=uid,
                details={
                    "content": content[:500],
                    "channel_id": message.channel.id,
                    "deleted": deleted,
                    "has_url": _has_url,
                    "has_addr": _has_addr,
                },
            )
            await self._log_mod(_v2(
                "URL/Address Blocked",
                color=C_WARNING,
                fields=[
                    ("Clanker", f"{message.author} ({uid})"),
                    ("Channel", f"<#{message.channel.id}>"),
                    ("Score", str(rec.get("score") if rec else "?")),
                    ("Deleted", "yes" if deleted else "no (see bot logs)"),
                    ("Content", content[:500] or "(empty)"),
                ],
            ))
            if rec:
                if in_tank_surface and not deleted:
                    await self._disco_reply(message, rec, _URL_RESPONSES, prob=1.0)
                else:
                    await self._tank_send(gid, uid, rec, _URL_RESPONSES)
            return

        # Normal message -- track and respond wherever the clanker posts.
        rec = await self._update_last_message(uid, gid)
        if rec is None:
            return
        if not in_tank_surface:
            await self._store_evidence_text(
                uid, gid, content, message.channel.id, "outside_channel_message",
            )
            deleted = await self._delete_clanker_message(
                message,
                reason="Clanktank: deleting message outside containment channel",
            )
            await self._audit(
                "message_deleted", gid, user_id=uid,
                details={
                    "kind": "outside_channel_message",
                    "channel_id": message.channel.id,
                    "deleted": deleted,
                    "content": content[:300],
                },
            )
            if deleted:
                await self._tank_send(gid, uid, rec, _RESPONSES)
            return

        if in_tank_surface:
            content_lower = content.lower()
            if any(kw in content_lower for kw in _ESCAPE_KEYWORDS):
                try:
                    await message.reply(
                        "run `,clanker escape` to get the link to your escape room "
                        "-- work through the 7 stations to request release. "
                        "check your DMs if you missed the original link.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    pass
                return

            # Skip the ambient reply if the message is a bot @mention or a reply
            # to a bot message -- handle_ai_reply / handle_ai_mention will fire
            # an ai_block_response for that case, so we'd double-reply otherwise.
            bot_id = self.bot.user.id if self.bot.user else None
            is_bot_mention = bot_id and any(m.id == bot_id for m in message.mentions)
            is_bot_reply = (
                message.reference is not None
                and isinstance(getattr(message.reference, "resolved", None), discord.Message)
                and message.reference.resolved.author.id == bot_id
            )
            if not is_bot_mention and not is_bot_reply:
                await self._disco_reply(message, rec)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        if interaction.type not in (
            discord.InteractionType.application_command,
            discord.InteractionType.component,
            discord.InteractionType.modal_submit,
        ):
            return
        uid, gid = interaction.user.id, interaction.guild_id
        if not await self.is_clanker(uid, gid):
            return

        # Escape room interactions must get through -- clankers earn their freedom.
        if interaction.type == discord.InteractionType.modal_submit:
            if (interaction.data or {}).get("custom_id", "").startswith("er__"):
                return
        elif interaction.type == discord.InteractionType.component:
            if interaction.message and interaction.message.id in self._escape_msg_ids:
                return

        rec = await self._inc(uid, gid, blocked_command_count=1, score=_SCORE_BLOCKED)
        await self._audit(
            "command_blocked", gid, user_id=uid,
            details={"interaction_type": str(interaction.type)},
        )
        eff = await self._effective_score(uid, gid, rec or {})
        tier = _score_tier(eff)
        msg = _pick(_BLOCKED_RESPONSES, tier, rec or {})
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_auto_moderation_action(
        self, execution: discord.AutoModerationActionExecution
    ) -> None:
        """Auto-clank members whose messages trigger Discord's AutoMod.

        Fires on all automod action types (block, timeout, alert).
        Controlled by the automod_auto_clank guild setting (default: on).
        """
        gid = execution.guild_id
        uid = execution.user_id
        guild = self.bot.get_guild(gid)
        if guild is None:
            return

        # Check the per-guild toggle before doing any fetches.
        try:
            _gs = await self.bot.db.get_guild_settings(gid)
            if not bool(_gs.get("automod_auto_clank", True)):
                return
        except Exception:
            return

        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                return

        if member.bot:
            return

        action_type = execution.action.type
        trigger_name = (
            execution.rule_trigger_type.name
            if execution.rule_trigger_type else "unknown"
        )
        keyword = execution.matched_keyword or ""
        content = execution.content or ""
        action_str = str(action_type).split(".")[-1]

        # Already clanked -- record additional automod hit, purge the triggering channel
        if await self.is_clanker(uid, gid):
            if content:
                await self._store_evidence_text(
                    uid, gid, content[:2000], execution.channel_id, "automod_hit",
                )
            rec = await self._inc(uid, gid, score=_SCORE_BLOCKED, blocked_command_count=1)
            purged_extra: list[discord.Message] = []
            if execution.channel_id:
                ch = self.bot.get_channel(execution.channel_id)
                if isinstance(ch, (discord.TextChannel, discord.Thread)):
                    purged_extra = await self._purge_visible_messages(member, ch)
                    if purged_extra:
                        await self._store_evidence(uid, gid, purged_extra, "automod_hit")
            await self._log_mod(_v2(
                "AutoMod Hit -- Already Clanked",
                color=C_WARNING,
                fields=[
                    ("Clanker", f"{member} ({uid})"),
                    ("Action", action_str),
                    ("Trigger", trigger_name),
                    ("Score", str(rec.get("score") if rec else "?")),
                    ("Msgs purged", str(len(purged_extra))),
                    *([("Keyword", keyword[:100])] if keyword else []),
                    *([("Channel", f"<#{execution.channel_id}>")] if execution.channel_id else []),
                    *([("Content", content[:500])] if content else []),
                ],
            ))
            return

        # Skip mods/admins
        if member.guild_permissions.manage_messages or member.guild_permissions.administrator:
            return

        actor = guild.me
        if actor is None:
            log.warning("clanktank: automod clank skipped -- guild.me unavailable gid=%s", gid)
            return

        # Resolve the channel that triggered automod so _do_clank can purge it
        trigger_ch: discord.TextChannel | discord.Thread | None = None
        if execution.channel_id:
            ch = self.bot.get_channel(execution.channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                trigger_ch = ch

        reason = f"AutoMod: {trigger_name}" + (f" [{keyword}]" if keyword else "")
        try:
            purged, _ = await self._do_clank(member, actor, reason, duration_s=None, channel=trigger_ch)
        except Exception:
            log.warning(
                "clanktank: automod auto-clank failed uid=%s gid=%s",
                uid, gid, exc_info=True,
            )
            return

        if content:
            await self._store_evidence_text(
                uid, gid, content[:2000], execution.channel_id, "automod_trigger",
            )

        tank = self._tank_channel()
        if tank:
            try:
                await tank.send(
                    f"<@{uid}> auto-clanked. reason: automod [{trigger_name}].",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except Exception:
                pass

        account_age = _age_str(member.created_at) if member.created_at else "?"
        await self._log_mod(_v2(
            "AutoMod Auto-Clank",
            color=C_WARNING,
            fields=[
                ("User", f"{member} ({uid})"),
                ("Action", action_str),
                ("Trigger type", trigger_name),
                ("Account age", account_age),
                ("Msgs purged", str(len(purged))),
                *([("Channel", f"<#{execution.channel_id}>")] if execution.channel_id else []),
                *([("Keyword", keyword[:100])] if keyword else []),
                *([("Content", content[:500])] if content else []),
            ],
            footer="Automatically contained via Discord AutoMod integration",
        ))
        await self._audit(
            "automod_clanked", gid, user_id=uid,
            details={
                "rule_id": execution.rule_id,
                "trigger_type": trigger_name,
                "action_type": action_str,
                "matched_keyword": keyword or None,
                "content": content[:500] or None,
                "channel_id": execution.channel_id,
            },
        )

    # -- Periodic sweep -------------------------------------------------------

    @tasks.loop(minutes=5)
    async def _sweep(self) -> None:
        await self._refresh_cache()
        try:
            expired = await self.bot.db.fetch_all(
                "SELECT user_id, guild_id FROM clanker_records "
                "WHERE expires_at IS NOT NULL AND expires_at <= now()"
            )
        except Exception:
            return

        for row in (expired or []):
            ok, final_rec, restored = await self._do_release(
                int(row["user_id"]), int(row["guild_id"]), auto=True
            )
            if ok:
                await self._log_release(
                    int(row["user_id"]), int(row["guild_id"]),
                    final_rec, restored, moderator=None,
                )

        for uid, gid in list(self._clanked):
            guild = self.bot.get_guild(gid)
            if not guild:
                continue
            member = guild.get_member(uid)
            if not member:
                continue
            allowed = self._allowed_role_ids(guild)
            extra = {r.id for r in member.roles} - allowed
            if not extra:
                continue
            now = time.time()
            if now - self._enforce_ts.get(uid, 0.0) < _ENFORCE_CD:
                continue
            self._enforce_ts[uid] = now
            to_remove = [r for r in member.roles if r.id in extra]
            try:
                await member.remove_roles(*to_remove, reason="Clanktank sweep")
                await self._inc(uid, gid, escape_attempts=1, score=_SCORE_ESCAPE)
            except Exception:
                pass

        # Delete escape room embeds for users who are no longer clanked.
        try:
            orphans = await self.bot.db.fetch_all(
                "SELECT user_id, guild_id, thread_id, message_id, step_data "
                "FROM clank_escape WHERE completed_at IS NULL"
            )
            for r in (orphans or []):
                uid2, gid2 = int(r["user_id"]), int(r["guild_id"])
                if (uid2, gid2) not in self._clanked:
                    asyncio.create_task(self._er_cleanup(uid2, gid2))
        except Exception:
            pass

        # CCI periodic cluster detection
        active_guilds = {gid for _, gid in self._clanked}
        for gid in active_guilds:
            asyncio.create_task(
                self._cci_run_clustering(gid),
                name=f"cci-cluster-{gid}",
            )

    @_sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    # -- Internal add / remove ------------------------------------------------

    async def _purge_and_store_bg(
        self,
        uid: int,
        gid: int,
        member: discord.Member,
        channel: discord.TextChannel | discord.Thread | None,
        moderator_id: int,
        reason: str | None,
        duration_s: int | None,
        stored_role_ids: list[int],
    ) -> None:
        """Background task: purge messages across all channels then store evidence."""
        try:
            purged = await self._purge_visible_messages(member, channel)
            if purged:
                await self._store_evidence(uid, gid, purged, "pre_clank_message")
            await self._audit(
                "clanked", gid, user_id=uid, actor_id=moderator_id,
                details={
                    "reason": reason,
                    "duration_s": duration_s,
                    "messages_purged": len(purged),
                    "stored_roles": stored_role_ids,
                },
            )
            new_names = {member.name, member.display_name}
            await self._link_accounts_bg(uid, gid, new_names)
        except Exception:
            log.exception("clanktank: _purge_and_store_bg failed uid=%s", uid)

    async def _do_clank(
        self,
        member: discord.Member,
        moderator: discord.Member,
        reason: str | None,
        duration_s: int | None,
        channel: discord.TextChannel | discord.Thread | None = None,
        *,
        defer_purge: bool = False,
        allow_booster: bool = False,
    ) -> tuple[list[discord.Message], list[dict]]:
        guild = member.guild
        uid, gid = member.id, guild.id

        clanker_role = self._clanker_role(guild)
        if clanker_role is None:
            raise ValueError(f'No role named "{_CLANKER_ROLE_NAME}" found.')

        # Server boosters are immune to auto-clank; manual ,clanker add may override.
        if not allow_booster and member.premium_since is not None:
            log.info(
                "clanktank: skipping _do_clank for server booster uid=%s gid=%s",
                uid, gid,
            )
            raise ValueError(f"{member} is a server booster and cannot be auto-clanked.")

        stored_role_ids = [
            r.id for r in member.roles
            if r.id != guild.default_role.id and r.id != clanker_role.id
        ]

        roles_to_remove = [r for r in member.roles if r.id != guild.default_role.id]
        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason="Clanktank: entering containment")
            except discord.Forbidden:
                log.warning(
                    "clanktank: _do_clank could not remove all roles uid=%s gid=%s "
                    "-- bot role hierarchy may be too low. Adding Clanker role anyway.",
                    uid, gid,
                )
            except Exception as exc:
                log.warning(
                    "clanktank: _do_clank role removal error uid=%s exc=%r -- "
                    "adding Clanker role anyway.",
                    uid, exc,
                )
        await member.add_roles(clanker_role, reason="Clanktank: containment applied")

        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=duration_s)
            if duration_s is not None else None
        )

        case_num = await self._next_case_num(gid)

        await self.bot.db.execute(
            """INSERT INTO clanker_records
               (user_id, guild_id, case_num, stored_roles, reason, clank_context,
                expires_at, usernames, display_names)
               VALUES ($1, $2, $8, $3, $4, $4, $5, $6, $7)
               ON CONFLICT (user_id, guild_id) DO UPDATE
               SET case_num              = $8,
                   stored_roles          = $3,
                   reason                = $4,
                   clank_context         = $4,
                   clanked_at            = now(),
                   expires_at            = $5,
                   message_count         = 0,
                   blocked_command_count = 0,
                   escape_attempts       = 0,
                   score                 = 0,
                   flags                 = '{}',
                   last_message_at       = NULL,
                   left_at               = NULL,
                   usernames             = $6,
                   display_names         = $7""",
            uid, gid, stored_role_ids, reason, expires_at,
            [member.name], [member.display_name], case_num,
        )
        self._clanked.add((uid, gid))
        if not member.bot:
            asyncio.ensure_future(self._start_escape_room(member, force_new=True, send_intro=True))

        if defer_purge:
            asyncio.create_task(self._purge_and_store_bg(
                uid, gid, member, channel,
                moderator.id, reason, duration_s, stored_role_ids,
            ))
            return [], []

        purged = await self._purge_visible_messages(member, channel)
        if purged:
            await self._store_evidence(uid, gid, purged, "pre_clank_message")

        await self._audit(
            "clanked", gid, user_id=uid, actor_id=moderator.id,
            details={
                "reason": reason,
                "duration_s": duration_s,
                "messages_purged": len(purged),
                "stored_roles": stored_role_ids,
            },
        )

        new_names = {member.name, member.display_name}
        asyncio.create_task(self._link_accounts_bg(uid, gid, new_names))

        return purged, []

    async def _link_accounts_bg(
        self, uid: int, gid: int, new_names: set[str]
    ) -> None:
        try:
            connections = await self._detect_connections(uid, gid, new_names)
            if not connections:
                return
            await self._save_connections(uid, gid, connections)

            guild = self.bot.get_guild(gid)
            new_member = guild.get_member(uid) if guild else None
            new_label = f"{new_member} ({uid})" if new_member else f"ID:{uid}"
            lines = []
            for conn in connections:
                other = conn["user_id"]
                other_m = guild.get_member(other) if guild else None
                other_name = f"{other_m} ({other})" if other_m else f"ID:{other}"
                lines.append(f"- {other_name}: {', '.join(conn['reasons'])}")
            await self._log_mod(_v2(
                "Account Connections Detected",
                color=C_WARNING,
                desc="\n".join(lines),
                fields=[
                    ("New clanker", new_label),
                    ("Connections found", str(len(connections))),
                ],
            ))
            await self._audit(
                "accounts_linked", gid, user_id=uid,
                details={"connections": [
                    {"other": c["user_id"], "reasons": c["reasons"]} for c in connections
                ]},
            )
        except Exception:
            log.debug("clanktank: link_accounts_bg failed uid=%s", uid, exc_info=True)

    # -- Clank Cohesion Index (CCI) cog methods --------------------------------

    async def _cci_score_join(
        self, uid: int, gid: int, names: set[str]
    ) -> tuple[float, list[str]]:
        """CCI join scorer: feature cosine similarity * temporal kernel against
        all active clankers and recent history. Returns (score 0-1, reasons)."""
        join_ts = time.time()
        try:
            clanker_rows = await self.bot.db.fetch_all(
                "SELECT user_id, usernames, display_names, clanked_at "
                "FROM clanker_records WHERE guild_id=$1 AND user_id!=$2",
                gid, uid,
            )
            hist_rows = await self.bot.db.fetch_all(
                "SELECT usernames, clanked_at FROM clanker_history "
                "WHERE guild_id=$1 ORDER BY clanked_at DESC LIMIT 100",
                gid,
            )
        except Exception:
            return 0.0, []

        phi_new = _cci_phi(names)
        reasons: list[str] = []
        weights: list[float] = []

        for rec in clanker_rows or []:
            other_names = set(rec.get("usernames") or []) | set(rec.get("display_names") or [])
            if not other_names:
                continue
            phi_other = _cci_phi(other_names)
            feat_sim = _cci_cosine_sim(phi_new, phi_other)
            ts_raw = rec.get("clanked_at")
            other_ts = (
                ts_raw.timestamp() if isinstance(ts_raw, datetime) else float(ts_raw)
            ) if ts_raw is not None else join_ts
            K_t = _cci_K_t(join_ts, other_ts)
            name_sim = max(
                (_name_similarity(n1, n2) for n1 in names for n2 in other_names if n1 and n2),
                default=0.0,
            )
            w = feat_sim * K_t * (1.0 + name_sim)
            weights.append(w)
            if name_sim >= 0.30:
                reasons.append(f"cci_name:{name_sim:.2f}")
            if feat_sim > 0.65 and K_t > 0.40:
                reasons.append(f"cci_struct:{feat_sim:.2f}")

        for hrec in hist_rows or []:
            hnames = set(hrec.get("usernames") or [])
            if not hnames:
                continue
            phi_h = _cci_phi(hnames)
            feat_sim = _cci_cosine_sim(phi_new, phi_h)
            name_sim = max(
                (_name_similarity(n1, n2) for n1 in names for n2 in hnames if n1 and n2),
                default=0.0,
            )
            ts_raw = hrec.get("clanked_at")
            other_ts = (
                ts_raw.timestamp() if isinstance(ts_raw, datetime) else float(ts_raw)
            ) if ts_raw is not None else join_ts
            K_t = _cci_K_t(join_ts, other_ts)
            w = feat_sim * K_t * (1.0 + name_sim) * 0.5  # 50% discount for historical
            weights.append(w)
            if name_sim >= _NAME_THRESHOLD:
                reasons.append(f"cci_hist:{name_sim:.2f}")

        if not weights:
            return 0.0, []

        weights.sort(reverse=True)
        top_k = weights[: min(10, len(weights))]
        raw = sum(top_k) / len(top_k)
        score = min(1.0, raw / 1.5)

        # Bayesian calibration: P(clanker | phi_new) scales the spectral score.
        # bayes_factor in [0.80, 1.20] -- amplifies confident clanker profiles,
        # dampens profiles that look unlike any historical clanker.
        all_phi: list[list[float]] = []
        for rec in clanker_rows or []:
            _n = set(rec.get("usernames") or []) | set(rec.get("display_names") or [])
            if _n:
                all_phi.append(_cci_phi(_n))
        for hrec in hist_rows or []:
            _hn = set(hrec.get("usernames") or [])
            if _hn:
                all_phi.append(_cci_phi(_hn))
        bayes = _cci_naive_bayes(phi_new, all_phi)
        bayes_factor = 0.80 + 0.40 * bayes   # [0.80, 1.20]
        score = min(1.0, score * bayes_factor)
        if bayes > 0.70:
            reasons.append(f"bayes:{bayes:.2f}")
        elif bayes < 0.30 and score > 0.10:
            reasons.append(f"bayes_low:{bayes:.2f}")

        seen: set[str] = set()
        deduped: list[str] = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                deduped.append(r)
        return score, deduped

    async def _cci_run_clustering(self, gid: int) -> None:
        """Periodic CCI spectral clustering: discover clanker clusters without
        explicit connections. Runs graph Laplacian -> spectral embed -> k-means
        -> quality filter on all active clankers. Requires numpy."""
        if not _HAS_NUMPY:
            return
        try:
            rows = await self.bot.db.fetch_all(
                "SELECT user_id, usernames, display_names, clanked_at "
                "FROM clanker_records WHERE guild_id=$1",
                gid,
            )
        except Exception:
            return

        if not rows or len(rows) < _CCI_MIN_CLUSTER:
            return

        uids: list[int] = []
        phis: list[list[float]] = []
        timestamps: list[float] = []
        for rec in rows:
            names = set(rec.get("usernames") or []) | set(rec.get("display_names") or [])
            ts_raw = rec.get("clanked_at")
            ts = (
                ts_raw.timestamp() if isinstance(ts_raw, datetime) else float(ts_raw)
            ) if ts_raw is not None else time.time()
            uids.append(int(rec["user_id"]))
            phis.append(_cci_phi(names))
            timestamps.append(ts)

        n = len(uids)
        try:
            W = _cci_build_W_np(phis, timestamps)
            k_embed = min(_CCI_DIM_K, max(2, n // 4))
            H = _cci_spectral_embed(W, k_embed)
            k_clust = max(2, min(n // 3, 12))
            labels = _cci_kmeans_np(H, k_clust)
            metrics = _cci_cluster_metrics(H, labels, timestamps, W)
        except Exception:
            log.debug("clanktank: CCI clustering failed gid=%s", gid, exc_info=True)
            return

        valid = [
            m for m in metrics
            if (m["rho"] >= _CCI_DENSITY_TAU
                and m["sigma2"] < _CCI_VAR_TAU
                and m["T"] < _CCI_TIGHT_TAU
                and m["S"] >= _CCI_SYNC_TAU
                and len(m["members"]) >= _CCI_MIN_CLUSTER)
        ]

        for cm in valid:
            member_uids = [uids[i] for i in cm["members"]]

            existing_id = await self.bot.db.fetch_val(
                """SELECT cluster_id FROM clanker_records
                   WHERE guild_id=$1 AND user_id=ANY($2::bigint[]) AND cluster_id IS NOT NULL
                   LIMIT 1""",
                gid, member_uids,
            )

            # Beta posterior confidence: Beta(confirmed+1, released+1) posterior mean,
            # blended 65/35 with the raw CCI score. Released cluster members are false
            # positives that drive the Beta posterior toward 0 over time -- self-calibrating.
            released_count = 0
            if existing_id:
                try:
                    released_count = int(await self.bot.db.fetch_val(
                        "SELECT COUNT(*) FROM clanker_history "
                        "WHERE guild_id=$1 AND cluster_id=$2",
                        gid, int(existing_id),
                    ) or 0)
                except Exception:
                    pass
            confirmed_count = len(member_uids)
            beta_conf = (confirmed_count + 1) / (confirmed_count + released_count + 2)
            confidence = min(1.0, max(0.0, 0.65 * float(cm["score"]) + 0.35 * beta_conf))

            if existing_id:
                await self.bot.db.execute(
                    "UPDATE clanker_clusters SET confidence=$1, updated_at=now() WHERE id=$2",
                    confidence, existing_id,
                )
                cluster_id = int(existing_id)
            else:
                cluster_id = await self.bot.db.fetch_val(
                    "INSERT INTO clanker_clusters (guild_id, confidence) VALUES ($1, $2) RETURNING id",
                    gid, confidence,
                )
                if not cluster_id:
                    continue
                cluster_id = int(cluster_id)
                guild = self.bot.get_guild(gid)
                names_preview = []
                for mu in member_uids[:10]:
                    m = guild.get_member(mu) if guild else None
                    names_preview.append(str(m) if m else f"ID:{mu}")
                overflow = len(member_uids) - 10
                T_days = cm["T"] / 86400.0
                await self._log_mod(_v2(
                    f"CCI Cluster Detected: [{cluster_id}]",
                    color=C_WARNING,
                    desc="\n".join(f"  {n}" for n in names_preview) + (f"\n  ...+{overflow} more" if overflow > 0 else ""),
                    fields=[
                        ("Members", str(len(member_uids))),
                        ("CCI Score", f"{cm['score']:.3f}"),
                        ("Confidence", f"{confidence:.2%}"),
                        ("Density rho", f"{cm['rho']:.2%}"),
                        ("Temporal spread", f"{T_days:.1f}d"),
                        ("Sync S", f"{cm['S']:.3f}"),
                        ("Embedding var", f"{cm['sigma2']:.4f}"),
                        ("Beta conf", f"{beta_conf:.2%}"),
                    ],
                    footer="Clank Cohesion Index (CCI) spectral clustering + Bayesian confidence",
                ))
                await self._audit(
                    "cci_cluster_detected", gid,
                    details={
                        "cluster_id": cluster_id,
                        "members": len(member_uids),
                        "rho": cm["rho"],
                        "score": cm["score"],
                    },
                )

            for mu in member_uids:
                await self.bot.db.execute(
                    "UPDATE clanker_records SET cluster_id=$1 WHERE user_id=$2 AND guild_id=$3",
                    cluster_id, mu, gid,
                )
                await self.bot.db.execute(
                    """INSERT INTO clanker_cluster_members (cluster_id, guild_id, user_id)
                       VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                    cluster_id, gid, mu,
                )
            await self._save_cluster_patterns(cluster_id, gid, set(member_uids))

    async def _do_release(
        self, user_id: int, guild_id: int, auto: bool = False
    ) -> tuple[bool, dict | None, list[discord.Role]]:
        if (user_id, guild_id) not in self._clanked:
            return False, None, []

        rec = await self._get_record(user_id, guild_id)

        # Drop from cache FIRST so on_member_update doesn't fight role restoration.
        self._clanked.discard((user_id, guild_id))

        restored: list[discord.Role] = []
        guild = self.bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            if member:
                clanker_role = self._clanker_role(guild)
                if clanker_role and clanker_role in member.roles:
                    try:
                        await member.remove_roles(clanker_role, reason="Clanktank: released")
                    except Exception:
                        pass
                stored_ids: set[int] = set(rec.get("stored_roles") or []) if rec else set()
                to_restore = [r for r in guild.roles if r.id in stored_ids]
                if to_restore:
                    try:
                        await member.add_roles(*to_restore, reason="Clanktank: restoring roles")
                        restored = to_restore
                    except Exception:
                        log.warning("clanktank: role restore failed uid=%s", user_id)

        asyncio.ensure_future(self._er_cleanup(user_id, guild_id))
        await self._save_to_history(user_id, guild_id, rec)

        try:
            await self.bot.db.execute(
                "DELETE FROM clanker_records WHERE user_id=$1 AND guild_id=$2",
                user_id, guild_id,
            )
        except Exception:
            pass

        await self._audit(
            "released", guild_id, user_id=user_id,
            details={
                "auto": auto,
                "restored_roles": [r.name for r in restored],
                "final_score": rec.get("score") if rec else None,
                "final_messages": rec.get("message_count") if rec else None,
            },
        )
        return True, rec, restored

    async def _log_release(
        self,
        user_id: int,
        guild_id: int,
        rec: dict | None,
        restored: list[discord.Role],
        moderator: discord.Member | None,
    ) -> None:
        ts = rec.get("clanked_at") if rec else None
        dur = (
            _duration_str(ts.timestamp() if isinstance(ts, datetime) else float(ts))
            if ts else "?"
        )
        await self._log_mod(_v2(
            "Clanker Released",
            color=C_SUCCESS,
            fields=[
                ("User", f"<@{user_id}> ({user_id})"),
                ("Released by", str(moderator) if moderator else "auto"),
                ("Time in containment", dur),
                ("Messages sent", str(rec["message_count"]) if rec else "?"),
                ("Commands blocked", str(rec["blocked_command_count"]) if rec else "?"),
                ("Escape attempts", str(rec["escape_attempts"]) if rec else "?"),
                ("Server leaves", str(rec.get("leave_count", 0)) if rec else "?"),
                ("Rejoins", str(rec.get("rejoin_count", 0)) if rec else "?"),
                ("Final score", str(rec["score"]) if rec else "?"),
                ("Roles restored", ", ".join(r.name for r in restored) or "none"),
                ("Original reason", rec.get("reason") or "none"),
            ],
        ))
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member:
            try:
                await member.send(
                    "\U0001f513 **You have been released from the Clank Tank.**\n\n"
                    "Your roles have been fully restored. You are free to participate in the server normally again.\n\n"
                    "**A note on security -- please read:**\n"
                    "- Real server staff will **NEVER** ask for your seed phrase, private key, or password\n"
                    "- Real staff will **NEVER** DM you asking you to click external links to 'verify' your wallet\n"
                    "- Real staff will **NEVER** ask you to send crypto to prove you are legitimate\n"
                    "- If anyone -- even someone claiming to be a moderator -- asks for any of the above, it is a scam. Block and report them.\n\n"
                    "You can turn your DMs back off now.\n"
                    "-# Server Settings -> Privacy Settings -> Allow direct messages from server members"
                )
            except Exception:
                pass

    # -- Escape room ----------------------------------------------------------

    async def _er_save(
        self,
        uid: int,
        gid: int,
        *,
        step: int | None = None,
        step_data: dict | None = None,
    ) -> None:
        try:
            if step is not None and step_data is not None:
                await self.bot.db.execute(
                    "UPDATE clank_escape SET step=$3, step_data=$4, step_started_at=NOW() "
                    "WHERE user_id=$1 AND guild_id=$2",
                    uid, gid, step, _json.dumps(step_data),
                )
            elif step is not None:
                await self.bot.db.execute(
                    "UPDATE clank_escape SET step=$3, step_started_at=NOW() WHERE user_id=$1 AND guild_id=$2",
                    uid, gid, step,
                )
            elif step_data is not None:
                await self.bot.db.execute(
                    "UPDATE clank_escape SET step_data=$3 WHERE user_id=$1 AND guild_id=$2",
                    uid, gid, _json.dumps(step_data),
                )
        except Exception:
            log.exception("clanktank: _er_save failed uid=%s gid=%s", uid, gid)

    async def _er_get(self, uid: int, gid: int) -> dict | None:
        try:
            return await self.bot.db.fetch_one(
                "SELECT * FROM clank_escape WHERE user_id=$1 AND guild_id=$2", uid, gid
            )
        except Exception:
            return None

    async def _er_update_hint(self, uid: int, gid: int, step: int) -> None:
        """Edit the per-user hint message in the escape thread to reflect the current step."""
        try:
            row = await self._er_get(uid, gid)
            if not row:
                return
            step_data = _parse_step_data(row.get("step_data"))
            hint_msg_id = step_data.get("hint_msg_id")
            thread_id = row.get("thread_id")
            if not hint_msg_id or not thread_id:
                return
            guild = self.bot.get_guild(gid)
            if not guild:
                return
            thread = guild.get_channel_or_thread(int(thread_id))
            if not isinstance(thread, discord.Thread):
                return
            hint_text = _ER_STEP_HINTS[min(step, len(_ER_STEP_HINTS) - 1)]
            try:
                msg = await thread.fetch_message(int(hint_msg_id))
                await msg.edit(content=hint_text)
            except Exception:
                pass
        except Exception:
            log.debug("clanktank: _er_update_hint failed uid=%s step=%d", uid, step)

    def _escape_thread_id(self) -> int:
        """Resolved escape-thread id: runtime override (,clanker er setthread) wins over env."""
        return self._escape_thread_override or Config.CLANK_ESCAPE_THREAD_ID

    async def _get_escape_thread(self) -> discord.Thread | None:
        """Fetch the configured shared escape thread (runtime override or env)."""
        tid = self._escape_thread_id()
        if not tid:
            return None
        thread = self.bot.get_channel(tid)
        if isinstance(thread, discord.Thread):
            return thread
        try:
            thread = await self.bot.fetch_channel(tid)
        except Exception:
            return None
        return thread if isinstance(thread, discord.Thread) else None

    async def _er_message_healthy(self, thread: discord.Thread, mid: int, uid: int) -> bool:
        """True if the message still exists AND carries a current-code escape button for this user."""
        try:
            msg = await thread.fetch_message(mid)
        except Exception:
            return False
        suffix = f"_{uid}"

        def _has_suffix(items: list) -> bool:
            for item in items:
                cid = getattr(item, "custom_id", None)
                if cid and cid.endswith(suffix):
                    return True
                for attr in ("children", "components"):
                    sub = getattr(item, attr, None)
                    if sub and _has_suffix(sub):
                        return True
            return False

        try:
            return _has_suffix(msg.components or [])
        except Exception:
            return False

    async def _start_escape_room(
        self, member: discord.Member, *, force_new: bool = False, send_intro: bool = False
    ) -> str | None:
        """Post a user's escape-room view in the shared escape thread. Returns error string on failure.

        force_new   -- wipe any existing room and reset progress to station 1 (used on re-clank).
        send_intro  -- DM the educational "you've been placed in the Clank Tank" message + link
                       (only on the initial clank, never on a plain ``,clanker escape``).
        """
        uid, gid = member.id, member.guild.id
        existing = await self._er_get(uid, gid)

        # Progress to carry into the (re)created room.
        keep_step = 0
        keep_data: dict = {"username": str(member)}

        # Resolve case number: prefer the sequential number from clanker_records,
        # then fall back to an existing clank_escape case_num, then generate a new one.
        _cr = await self._get_record(uid, gid)
        case_num = int(_cr.get("case_num") or 0) if _cr else 0
        if not case_num and existing:
            case_num = int(existing.get("case_num") or 0)
        if not case_num:
            case_num = await self._next_case_num(gid)

        if existing and existing.get("completed_at") is None:
            ed = _parse_step_data(existing.get("step_data"))
            mid = existing.get("message_id")

            if not force_new and mid:
                # ,clanker escape: reuse a healthy, current-code embed if one exists.
                thread = await self._get_escape_thread()
                if isinstance(thread, discord.Thread) and await self._er_message_healthy(thread, int(mid), uid):
                    if int(mid) not in self._escape_msg_ids:
                        view = _EscapeRoomView(
                            self, uid, gid, case_num,
                            ed.get("username", str(member)),
                            int(existing.get("step") or 0), ed,
                        )
                        self._escape_msg_ids.add(int(mid))
                        self.bot.add_view(view, message_id=int(mid))
                    try:
                        await thread.add_user(member)
                    except Exception:
                        pass
                    return None

            # Recreating: preserve progress on a refresh, reset it on a re-clank.
            if force_new:
                keep_step = 0
                keep_data = {"username": str(member)}
            else:
                keep_step = int(existing.get("step") or 0)
                keep_data = {k: v for k, v in ed.items() if k not in ("hint_msg_id", "ping_msg_id")}
                keep_data.setdefault("username", str(member))
            await self._er_delete_message(existing)

        tank_ch_id = Config.CLANKTANK_CHANNEL_ID

        if send_intro:
            clank_msg = (
                f"\U0001f512 **You have been placed in the Clank Tank.**\n\n"
                f"**What happened:**\n"
                f"Your roles have been temporarily removed and you have been assigned the Clanker role. "
                f"This is a protective measure -- your account matched patterns associated with scam activity "
                f"or a moderator reviewed your account and found something suspicious.\n\n"
                f"**This is not a permanent ban.** The Clank Tank is a containment zone while we verify your account.\n\n"
                f"**How to get out:**\n"
                f"1. Enable your DMs if they are off\n"
                f"   *(Server Settings -> Privacy Settings -> Allow direct messages from server members)*\n"
                f"2. Go to <#{tank_ch_id}> and run `,clanker escape`\n"
                f"3. You will be shown a link to your escape room\n"
                f"4. Complete all 8 stations\n"
                f"5. You will be released automatically when done\n\n"
                f"If you believe this is a mistake, reach out to a moderator directly.\n\n"
                f"-# Keep your DMs open until you are released."
            )
            dm_ok = False
            try:
                await member.send(clank_msg)
                dm_ok = True
            except Exception:
                pass
            if not dm_ok and tank_ch_id:
                tank_ch = member.guild.get_channel(tank_ch_id)
                if isinstance(tank_ch, discord.TextChannel):
                    try:
                        await tank_ch.send(f"{member.mention}\n\n{clank_msg}", delete_after=300)
                    except Exception:
                        pass

        thread = await self._get_escape_thread()
        if not isinstance(thread, discord.Thread):
            log.error("clanktank: escape room FAILED -- CLANK_ESCAPE_THREAD_ID not set or invalid. uid=%s gid=%s", uid, gid)
            return "CLANK_ESCAPE_THREAD_ID is not configured. Ask a server admin to create a thread, copy its ID, and set the env var."

        # Always scrub any leftover/orphaned escape messages for this user before posting a fresh one.
        await self._er_purge_thread_for_user(thread, uid, str(member))

        # Make sure the clanker is actually a member of the shared thread.
        try:
            await thread.add_user(member)
        except Exception:
            pass

        # Hint message sent FIRST so it appears above the interactive view.
        hint_msg_id: int | None = None
        try:
            hint_msg = await thread.send(_ER_STEP_HINTS[min(keep_step, len(_ER_STEP_HINTS) - 1)])
            hint_msg_id = hint_msg.id
        except Exception:
            pass

        # Ping the user as a plain text message so the @mention resolves.
        # The LayoutView must be a standalone message (no content=) for buttons to render.
        ping_msg_id: int | None = None
        try:
            ping_msg = await thread.send(
                f"{member.mention} -- your containment proceedings have begun. "
                "Work through all 8 stations to be released."
            )
            ping_msg_id = ping_msg.id
        except Exception:
            pass

        step_data: dict = dict(keep_data)
        step_data["username"] = keep_data.get("username", str(member))
        if hint_msg_id:
            step_data["hint_msg_id"] = hint_msg_id
        if ping_msg_id:
            step_data["ping_msg_id"] = ping_msg_id
        view = _EscapeRoomView(self, uid, gid, case_num, step_data["username"], keep_step, step_data)
        try:
            msg = await thread.send(view=view)
        except Exception:
            log.exception("clanktank: escape room view send failed uid=%s", uid)
            return "Failed to post escape room message in the escape thread. Check bot permissions."

        try:
            await self.bot.db.execute(
                """INSERT INTO clank_escape (user_id, guild_id, case_num, thread_id, message_id, step, step_data)
                   VALUES ($1, $2, $3, $4, $5, $7, $6)
                   ON CONFLICT (user_id, guild_id) DO UPDATE SET
                       case_num=EXCLUDED.case_num, thread_id=EXCLUDED.thread_id,
                       message_id=EXCLUDED.message_id, step=EXCLUDED.step, step_data=EXCLUDED.step_data,
                       step_started_at=NOW(), completed_at=NULL""",
                uid, gid, case_num, thread.id, msg.id, _json.dumps(step_data), keep_step,
            )
        except Exception:
            log.exception("clanktank: escape DB insert failed uid=%s", uid)
            return "Failed to save escape room state to database."

        self._escape_msg_ids.add(msg.id)
        self.bot.add_view(view, message_id=msg.id)

        if send_intro:
            jump = f"https://discord.com/channels/{gid}/{thread.id}/{msg.id}"
            try:
                await member.send(
                    f"\U0001f517 **Your escape room is ready.**\n\n"
                    f"Jump to your case: {jump}\n\n"
                    f"-# Run `,clanker escape` in <#{tank_ch_id}> any time to get this link again."
                )
            except Exception:
                pass

        log.info("clanktank: escape room started uid=%s case=%d thread=%d msg=%d step=%d", uid, case_num, thread.id, msg.id, keep_step)
        return None

    async def _er_purge_messages(self, uid: int, thread: discord.Thread, tank_channel_id: int, guild: discord.Guild) -> None:
        """Delete a user's messages from the escape thread and the clanktank channel."""
        try:
            async for m in thread.history(limit=500):
                if m.author.id == uid:
                    try:
                        await m.delete()
                    except Exception:
                        pass
        except Exception:
            pass
        if tank_channel_id:
            tank_ch = guild.get_channel(tank_channel_id)
            if isinstance(tank_ch, discord.TextChannel):
                try:
                    async for m in tank_ch.history(limit=500):
                        if m.author.id == uid:
                            try:
                                await m.delete()
                            except Exception:
                                pass
                except Exception:
                    pass

    async def _er_on_complete(self, uid: int, gid: int) -> None:
        try:
            row = await self._er_get(uid, gid)
            if not row:
                return
            case_num = int(row.get("case_num") or 0)
            elapsed_s: int | None = await self.bot.db.fetch_val(
                "SELECT EXTRACT(EPOCH FROM (NOW() - started_at))::BIGINT FROM clank_escape WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
            await self.bot.db.execute(
                "UPDATE clank_escape SET completed_at=NOW() WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
            # Clear score so escape completion is a clean slate.
            try:
                await self.bot.db.execute(
                    "UPDATE clanker_records SET score=0 WHERE user_id=$1 AND guild_id=$2",
                    uid, gid,
                )
            except Exception:
                pass
            guild = self.bot.get_guild(gid)
            member = guild.get_member(uid) if guild else None
            name_str = str(member) if member else f"<@{uid}>"

            # Purge the user's messages from escape thread + clanktank channel.
            thread_id = row.get("thread_id")
            if thread_id and guild:
                thread = guild.get_channel_or_thread(int(thread_id))
                if not isinstance(thread, discord.Thread):
                    try:
                        thread = await self.bot.fetch_channel(int(thread_id))
                    except Exception:
                        thread = None
                if isinstance(thread, discord.Thread):
                    asyncio.ensure_future(
                        self._er_purge_messages(uid, thread, Config.CLANKTANK_CHANNEL_ID, guild)
                    )

            try:
                h, rem = divmod(int(elapsed_s or 0), 3600)
                m2, s = divmod(rem, 60)
                elapsed_label = f"{h}h {m2}m {s}s" if h else (f"{m2}m {s}s" if m2 else f"{s}s")
            except Exception:
                elapsed_label = "?"

            # Always auto-release on escape completion -- messages are purged, they're done.
            if member:
                try:
                    released, _rec, restored = await self._do_release(uid, gid)
                    if released:
                        await self._log_mod(_v2(
                            f"Escape Complete: Released Case #{case_num:06d}",
                            color=C_SUCCESS,
                            fields=[
                                ("User", f"{name_str} ({uid})"),
                                ("Time to escape", elapsed_label),
                                ("Roles restored", ", ".join(r.name for r in restored) or "none"),
                            ],
                        ))
                        await self._audit("escape_released", gid, user_id=uid,
                                          details={"case_num": case_num, "elapsed_s": elapsed_s})
                        return
                except Exception:
                    log.exception("clanktank: escape release failed uid=%s", uid)

            await self._log_mod(_v2(
                f"Escape Complete: Case #{case_num:06d}",
                color=C_SUCCESS,
                fields=[
                    ("User", f"{name_str} ({uid})"),
                    ("Time to escape", elapsed_label),
                    ("Note", "User not in guild at completion time -- release skipped."),
                ],
            ))
            await self._audit("escape_complete", gid, user_id=uid, details={"case_num": case_num, "elapsed_s": elapsed_s})
        except Exception:
            log.exception("clanktank: _er_on_complete failed uid=%s gid=%s", uid, gid)

    async def _schedule_station4_refresh(self, uid: int, gid: int, wait_until: float) -> None:
        """Sleep until the reflection timer expires, then flip the embed button to green."""
        delay = wait_until - time.time() + 2.0
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            row = await self._er_get(uid, gid)
            if not row or int(row.get("step") or 0) != 4:
                return
            mid = row.get("message_id")
            if not mid:
                return
            thread = await self._get_escape_thread()
            if not isinstance(thread, discord.Thread):
                return
            msg = await thread.fetch_message(int(mid))
            step_data = _parse_step_data(row.get("step_data"))
            case_num = int(row.get("case_num") or 0)
            username = step_data.get("username", "")
            view = _EscapeRoomView(self, uid, gid, case_num, username, 4, step_data)
            await msg.edit(view=view)
        except Exception:
            pass

    async def _er_delete_message(self, row: dict) -> None:
        """Delete a clanker's escape-room view + hint + ping messages from the shared thread."""
        thread_id = row.get("thread_id")
        message_id = row.get("message_id")
        step_data = _parse_step_data(row.get("step_data"))
        hint_msg_id = step_data.get("hint_msg_id")
        ping_msg_id = step_data.get("ping_msg_id")
        if message_id:
            self._escape_msg_ids.discard(int(message_id))
        if not thread_id:
            return
        thread = self.bot.get_channel(int(thread_id))
        if not isinstance(thread, discord.Thread):
            try:
                thread = await self.bot.fetch_channel(int(thread_id))
            except Exception:
                return
        if not isinstance(thread, discord.Thread):
            return
        for mid in filter(None, [message_id, hint_msg_id, ping_msg_id]):
            try:
                msg = await thread.fetch_message(int(mid))
                await msg.delete()
            except Exception:
                pass

    async def _er_cleanup(self, uid: int, gid: int) -> None:
        """Delete a released clanker's escape-room view and hint from the shared thread."""
        try:
            row = await self._er_get(uid, gid)
            if row:
                await self._er_delete_message(row)
        except Exception:
            pass

    async def _er_purge_thread_for_user(
        self, thread: discord.Thread, uid: int, name: str | None = None
    ) -> None:
        """Delete every escape-room message in the thread that belongs to this user.

        Catches all three generations of escape messages:
        - ping / old combined messages -- ``<@uid>`` in the message content
        - current-code view messages -- a button custom_id ending in ``_{uid}``
        - old-code Components v2 embeds (empty content, random custom_ids) --
          identified by the user's name appearing inside the embed's text next to
          the "Containment" header.
        """
        mention = f"<@{uid}>"
        suffix = f"_{uid}"

        def _has_suffix(items: list) -> bool:
            for item in items:
                cid = getattr(item, "custom_id", None)
                if cid and cid.endswith(suffix):
                    return True
                for attr in ("children", "components"):
                    sub = getattr(item, attr, None)
                    if sub and _has_suffix(sub):
                        return True
            return False

        def _collect_text(items: list, out: list) -> None:
            for item in items:
                c = getattr(item, "content", None)
                if isinstance(c, str):
                    out.append(c)
                for attr in ("children", "components"):
                    sub = getattr(item, attr, None)
                    if sub:
                        _collect_text(sub, out)

        try:
            async for m in thread.history(limit=200):
                if m.author.id != self.bot.user.id:
                    continue
                should_delete = mention in (m.content or "")
                if not should_delete:
                    try:
                        should_delete = _has_suffix(m.components or [])
                    except Exception:
                        pass
                if not should_delete:
                    try:
                        texts: list[str] = []
                        _collect_text(m.components or [], texts)
                        blob = (m.content or "") + " " + " ".join(texts)
                        if "ontainment" in blob and (mention in blob or (name and name in blob)):
                            should_delete = True
                    except Exception:
                        pass
                if should_delete:
                    self._escape_msg_ids.discard(m.id)
                    try:
                        await m.delete()
                    except Exception:
                        pass
        except Exception:
            log.debug("clanktank: _er_purge_thread_for_user failed uid=%s", uid)

    # -- Commands -------------------------------------------------------------

    @commands.group(name="clanker", invoke_without_command=True)
    @guild_only
    async def clanker_group(self, ctx: DiscoContext) -> None:
        await self.clanker_help(ctx)

    @clanker_group.command(name="help")
    @guild_only
    async def clanker_help(self, ctx: DiscoContext) -> None:
        active = sum(1 for _, gid in self._clanked if gid == ctx.guild.id)
        p = ctx.prefix or ","
        view = _ClankerHelpView(ctx.author.id, p, active)
        await ctx.reply(view=view, mention_author=False)

    @clanker_group.command(name="add")
    @guild_only
    async def clanker_add(
        self,
        ctx: DiscoContext,
        user: discord.Member,
        *args: str,
    ) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        if user.bot and user.id == ctx.bot.user.id:
            await ctx.reply_error("Cannot clank the bot itself.")
            return

        duration_s: int | None = None
        dur_raw: str | None = None
        reason_parts: list[str] = list(args)
        if reason_parts:
            parsed = _parse_duration(reason_parts[-1])
            if parsed is not None:
                duration_s = parsed
                dur_raw = reason_parts[-1]
                reason_parts = reason_parts[:-1]

        reason = " ".join(reason_parts) if reason_parts else None
        dur_label = dur_raw or "Permanent"

        try:
            await self._do_clank(
                user, ctx.author, reason, duration_s,
                channel=ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None,
                defer_purge=True,
                allow_booster=True,
            )
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        except discord.Forbidden:
            await ctx.reply_error("Missing permissions to manage this user's roles.")
            return
        except Exception:
            log.exception("clanktank: add failed uid=%s", user.id)
            await ctx.reply_error("Failed to apply containment. Check bot role hierarchy.")
            return

        tank_ref = f"<#{Config.CLANKTANK_CHANNEL_ID}>" if Config.CLANKTANK_CHANNEL_ID else None
        await ctx.reply(
            view=_v2(
                "Containment Applied",
                color=C_WARNING,
                fields=[
                    ("User", f"{user.mention} ({user.id})"),
                    ("Moderator", ctx.author.mention),
                    ("Duration", dur_label),
                    ("Reason", reason or "None provided"),
                    *([("Visit them", tank_ref)] if tank_ref else []),
                ],
            ),
            mention_author=False,
            delete_after=30.0,
        )

        rec = await self._get_record(user.id, ctx.guild.id)
        stored_names = (
            ", ".join(
                ctx.guild.get_role(rid).name
                for rid in (rec.get("stored_roles") or [])
                if ctx.guild.get_role(rid)
            )
            if rec else "unknown"
        ) or "none"
        account_age = _age_str(user.created_at) if user.created_at else "?"
        join_age = _age_str(user.joined_at) if user.joined_at else "?"
        await self._log_mod(_v2(
            "Clanker Added",
            color=C_WARNING,
            fields=[
                ("User", f"{user} ({user.id})"),
                ("Moderator", str(ctx.author)),
                ("Duration", dur_label),
                ("Account age", account_age),
                ("Server join age", join_age),
                ("Reason / context", reason or "None"),
                ("Roles stripped", stored_names),
            ],
            footer="Message purge running in background",
        ))

    @clanker_group.command(name="remove")
    @guild_only
    async def clanker_remove(self, ctx: DiscoContext, user: discord.Member) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        released, final_rec, restored = await self._do_release(user.id, ctx.guild.id)
        if not released:
            await ctx.reply_error(f"{user.mention} is not currently in containment.")
            return

        restored_names = ", ".join(r.name for r in restored) or "none"
        await ctx.reply(
            view=_v2(
                "Containment Released",
                color=C_SUCCESS,
                fields=[
                    ("User", f"{user.mention} ({user.id})"),
                    ("Released by", ctx.author.mention),
                    ("Roles restored", restored_names),
                ],
            ),
            mention_author=False,
            delete_after=30.0,
        )
        await self._log_release(user.id, ctx.guild.id, final_rec, restored, ctx.author)

    @clanker_group.command(name="list")
    @guild_only
    async def clanker_list(self, ctx: DiscoContext) -> None:
        """List all contained users with sortable, paginated view."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        try:
            rows = await self.bot.db.fetch_all(
                "SELECT * FROM clanker_records WHERE guild_id=$1 ORDER BY clanked_at DESC",
                ctx.guild.id,
            )
        except Exception:
            await ctx.reply_error("Failed to fetch clanker list.")
            return

        if not rows:
            await ctx.reply(
                view=_v2("Clanktank", color=C_INFO, desc="No users currently in containment."),
                mention_author=False,
            )
            return

        view = _ClankerListView(ctx.author.id, [dict(r) for r in rows], ctx.guild, "newest")
        await ctx.reply(view=view, mention_author=False)

    @clanker_group.command(name="info")
    @guild_only
    async def clanker_info(self, ctx: DiscoContext, user: discord.Member) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        rec = await self._get_record(user.id, ctx.guild.id)
        if rec is None:
            await ctx.reply_error(f"{user.mention} has no containment record.")
            return

        ts = rec.get("clanked_at")
        dur = _duration_str(ts.timestamp() if isinstance(ts, datetime) else float(ts)) if ts else "?"
        expires = rec.get("expires_at")
        flags = list(rec.get("flags") or [])
        eff = await self._effective_score(user.id, ctx.guild.id, rec)

        # Fetch recent evidence for preview (page 2+)
        try:
            ev_rows = await self.bot.db.fetch_all(
                "SELECT content, evidence_type, logged_at FROM clanker_evidence "
                "WHERE user_id=$1 AND guild_id=$2 ORDER BY logged_at DESC LIMIT 5",
                user.id, ctx.guild.id,
            )
        except Exception:
            ev_rows = []

        evidence_preview = "\n".join(
            f"[{r['evidence_type']}] {(r['content'] or '')[:80]}"
            for r in (ev_rows or [])
        ) or "none"

        # Fetch connections
        try:
            conn_rows = await self.bot.db.fetch_all(
                """SELECT user_id_a, user_id_b, reasons, name_score, text_score
                   FROM clanker_connections
                   WHERE guild_id=$1 AND (user_id_a=$2 OR user_id_b=$2)""",
                ctx.guild.id, user.id,
            )
        except Exception:
            conn_rows = []

        conn_lines = []
        for c in (conn_rows or []):
            other_id = c["user_id_b"] if c["user_id_a"] == user.id else c["user_id_a"]
            other_m = ctx.guild.get_member(other_id)
            other_name = str(other_m) if other_m else f"ID:{other_id}"
            name_sc = float(c.get("name_score") or 0.0)
            text_sc = float(c.get("text_score") or 0.0)
            parts: list[str] = []
            if name_sc > 0:
                parts.append(f"name {name_sc:.0%}")
            if text_sc > 0:
                parts.append(f"msg {text_sc:.0%}")
            in_tank = await self.is_clanker(other_id, ctx.guild.id)
            tank_tag = "[in tank]" if in_tank else "[free]"
            conn_lines.append(f"**{other_name}** {tank_tag} -- {' | '.join(parts)}")

        left_at = rec.get("left_at")
        currently_in = "No (left server)" if left_at else "Yes"

        _case_num = int(rec.get("case_num") or 0)
        await ctx.reply(
            view=_v2(
                f"Clanktank: {user.display_name}",
                color=C_NAVY,
                fields=[
                    ("Case #", f"#{_case_num:06d}" if _case_num else "unassigned"),
                    ("Account age", _age_str(user.created_at) if user.created_at else "?"),
                    ("Server join age", _age_str(user.joined_at) if user.joined_at else "?"),
                    ("Currently in server", currently_in),
                    ("Time in tank", dur),
                    ("Expires", fmt_ts(expires) if expires else "Permanent"),
                    ("Score", str(rec["score"])),
                    ("Effective score", str(eff)),
                    ("Messages sent", str(rec["message_count"])),
                    ("Blocked commands", str(rec["blocked_command_count"])),
                    ("Escape attempts", str(rec["escape_attempts"])),
                    ("Server leaves", str(rec.get("leave_count", 0))),
                    ("Rejoins", str(rec.get("rejoin_count", 0))),
                    ("Last message", fmt_ts(rec["last_message_at"]) if rec.get("last_message_at") else "Never"),
                    ("Reason / context", rec.get("clank_context") or rec.get("reason") or "None"),
                    *([("Flags", ", ".join(flags))] if flags else []),
                    ("Connected accounts", "\n".join(conn_lines) if conn_lines else "none"),
                    ("Evidence preview", evidence_preview),
                ],
                footer="use ,clanker evidence @user for full evidence log",
            ),
            mention_author=False,
        )

    @clanker_group.command(name="case")
    @guild_only
    async def clanker_case(self, ctx: DiscoContext, case_num: int) -> None:
        """Look up a containment record by case number."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        rec = await self.bot.db.fetch_one(
            "SELECT * FROM clanker_records WHERE guild_id=$1 AND case_num=$2",
            ctx.guild.id, case_num,
        )
        if rec is None:
            await ctx.reply_error(f"No containment record found for case `#{case_num:06d}`.")
            return
        uid = int(rec["user_id"])
        member = ctx.guild.get_member(uid)
        display = str(member) if member else f"ID:{uid}"
        ts = rec.get("clanked_at")
        dur = _duration_str(ts.timestamp() if isinstance(ts, datetime) else float(ts)) if ts else "?"
        expires = rec.get("expires_at")
        flags = list(rec.get("flags") or [])
        eff = await self._effective_score(uid, ctx.guild.id, rec)
        left_at = rec.get("left_at")
        currently_in = "No (left server)" if left_at else ("Yes" if member else "No (not in server)")
        er_row = await self._er_get(uid, ctx.guild.id)
        er_step = int(er_row.get("step") or 0) if er_row else None
        er_status = (
            "COMPLETE" if er_step is not None and er_step >= 8
            else f"Station {er_step + 1} of 8" if er_step is not None
            else "not started"
        )
        await ctx.reply(
            view=_v2(
                f"Case #{case_num:06d} -- {display}",
                color=C_NAVY,
                fields=[
                    ("User", f"<@{uid}> ({uid})"),
                    ("Currently in server", currently_in),
                    ("Time in tank", dur),
                    ("Expires", fmt_ts(expires) if expires else "Permanent"),
                    ("Score", str(rec.get("score", 0))),
                    ("Effective score", str(eff)),
                    ("Escape room progress", er_status),
                    ("Reason / context", rec.get("clank_context") or rec.get("reason") or "None"),
                    *([("Flags", ", ".join(flags))] if flags else []),
                ],
                footer=f"use ,clanker info @user for full details",
            ),
            mention_author=False,
        )

    @clanker_group.command(name="evidence")
    @guild_only
    async def clanker_evidence(
        self,
        ctx: DiscoContext,
        user: discord.Member,
        limit: int = 20,
    ) -> None:
        """Show the full paginated evidence log for a clanker."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        limit = max(1, min(limit, 100))
        try:
            rows = await self.bot.db.fetch_all(
                "SELECT content, evidence_type, logged_at, channel_id FROM clanker_evidence "
                "WHERE user_id=$1 AND guild_id=$2 ORDER BY logged_at DESC LIMIT $3",
                user.id, ctx.guild.id, limit,
            )
        except Exception:
            await ctx.reply_error("Failed to fetch evidence.")
            return

        if not rows:
            await ctx.reply(
                view=_v2("Evidence", color=C_INFO, desc=f"No evidence on record for {user.mention}."),
                mention_author=False,
            )
            return

        text_pages: list[str] = []
        chunk = 5
        total = len(rows)
        for i in range(0, total, chunk):
            lines: list[str] = []
            for r in rows[i: i + chunk]:
                ts_str = fmt_ts(r["logged_at"]) if r.get("logged_at") else "?"
                ch_str = f"<#{r['channel_id']}>" if r.get("channel_id") else "?"
                content = (r["content"] or "").strip()
                if len(content) > 300:
                    content = content[:297] + "..."
                lines.append(
                    f"`{ts_str}` [{r['evidence_type']}] {ch_str}\n"
                    f"> {content}"
                )
            text_pages.append("\n\n".join(lines))

        ev_title = f"Evidence: {user.display_name} ({total} entries)"
        if len(text_pages) == 1:
            await ctx.reply(view=_v2(ev_title, color=C_NAVY, desc=text_pages[0]), mention_author=False)
        else:
            await ctx.reply(view=_PageView(ctx.author.id, text_pages, title=ev_title, color=C_NAVY), mention_author=False)

    @clanker_group.command(name="logs")
    @guild_only
    async def clanker_logs(
        self,
        ctx: DiscoContext,
        user: discord.Member | None = None,
        limit: int = 10,
    ) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        limit = max(1, min(limit, 100))
        try:
            if user:
                rows = await self.bot.db.fetch_all(
                    "SELECT * FROM clanker_audit_log "
                    "WHERE guild_id=$1 AND user_id=$2 "
                    "ORDER BY created_at DESC LIMIT $3",
                    ctx.guild.id, user.id, limit,
                )
            else:
                rows = await self.bot.db.fetch_all(
                    "SELECT * FROM clanker_audit_log WHERE guild_id=$1 "
                    "ORDER BY created_at DESC LIMIT $2",
                    ctx.guild.id, limit,
                )
        except Exception:
            await ctx.reply_error("Failed to fetch audit log.")
            return

        if not rows:
            await ctx.reply(
                view=_v2("Audit Log", color=C_INFO, desc="No events found."),
                mention_author=False,
            )
            return

        def _fmt_row(r: dict) -> str:
            uid = r.get("user_id")
            ts = r.get("created_at")
            ts_str = fmt_ts(ts) if ts else "?"
            actor = r.get("actor_id")
            event_type = r.get("event_type", "")
            detail = ""
            try:
                d = _json.loads(r["details"]) if r.get("details") else {}
                if event_type == "suspicious_join":
                    score = d.get("score", 0)
                    reasons = d.get("reasons") or []
                    cid = d.get("cluster_id_added")
                    direct = d.get("direct_connections", 0)
                    detail = f' pattern:{float(score):.0%}'
                    if direct:
                        detail += f' direct:{direct}'
                    if reasons:
                        detail += f' [{", ".join(str(x) for x in reasons[:3])}]'
                    if cid:
                        detail += f' -> cluster:{cid}'
                elif event_type in ("cluster_formed", "cluster_cleave"):
                    cid = d.get("cluster_id")
                    n = d.get("members") or d.get("clanked")
                    conf = d.get("confidence")
                    detail = f' cluster:{cid}'
                    if n:
                        detail += f' size:{n}'
                    if conf:
                        detail += f' conf:{float(conf):.0%}'
                elif "reason" in d and d["reason"]:
                    detail = f' "{d["reason"]}"'
                elif "content" in d:
                    detail = f' "{str(d["content"])[:40]}"'
                elif "roles_added" in d:
                    detail = f' roles:{d["roles_added"]}'
                elif "leave_count" in d:
                    detail = f' leave#{d["leave_count"]}'
                elif "rejoin_count" in d:
                    detail = f' rejoin#{d["rejoin_count"]}'
            except Exception:
                pass
            actor_str = f" by <@{actor}>" if actor else ""
            user_str = f"<@{uid}>" if uid else "?"
            return f"`{ts_str}` **{event_type}** {user_str}{actor_str}{detail}"

        log_title = f"Audit Log - {user}" if user else "Audit Log (guild)"
        log_pages: list[str] = []
        chunk = 10
        for i in range(0, len(rows), chunk):
            lines = [_fmt_row(r) for r in rows[i: i + chunk]]
            log_pages.append("\n".join(lines))

        if len(log_pages) == 1:
            await ctx.reply(view=_v2(log_title, color=C_NAVY, desc=log_pages[0]), mention_author=False)
        else:
            await ctx.reply(view=_PageView(ctx.author.id, log_pages, title=log_title, color=C_NAVY), mention_author=False)

    async def _cmd_scan_user(self, ctx: DiscoContext, target: str) -> None:
        """Run a targeted CCI scan for a specific user against the clanker population."""
        gid = ctx.guild.id

        # Resolve target to a user ID + names set.
        # Accept: <@123>, <@!123>, plain integer, or server member name.
        target = target.strip()
        uid: int | None = None
        member: discord.Member | None = None

        # Try mention format
        if target.startswith("<@") and target.endswith(">"):
            raw = target.lstrip("<@!").rstrip(">")
            try:
                uid = int(raw)
            except ValueError:
                pass

        # Try bare integer
        if uid is None:
            try:
                uid = int(target)
            except ValueError:
                pass

        # Try resolving as a member name/nick from cache
        if uid is None:
            low = target.lower()
            for m in ctx.guild.members:
                if m.bot:
                    continue
                if m.name.lower() == low or (m.nick or "").lower() == low:
                    uid = m.id
                    member = m
                    break

        if uid is None:
            await ctx.reply_error(f'Could not resolve "{target}" to a user. Provide a mention or numeric ID.')
            return

        # Try to get the member object for display; may be None for users not in server.
        if member is None:
            member = ctx.guild.get_member(uid)

        # Pull names from clanker_records if tracked, otherwise from member cache.
        rec = await self.bot.db.fetch_one(
            "SELECT usernames, display_names, clanked_at, score, cluster_id "
            "FROM clanker_records WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        if rec:
            names: set[str] = set(rec.get("usernames") or []) | set(rec.get("display_names") or [])
        elif member:
            names = {member.name}
            if member.nick:
                names.add(member.nick)
        else:
            await ctx.reply_error(
                f"User ID {uid} is not in this server's clanker records and is not in the server. "
                "Unable to scan without name data."
            )
            return

        if not names:
            await ctx.reply_error(f"No name data available for user {uid}.")
            return

        status_msg = await ctx.reply(
            view=_v2("Scanning user...", color=C_INFO, desc=f"Running CCI scan for user ID {uid} against the clanker population."),
            mention_author=False,
        )

        try:
            # CCI score + Bayesian posterior
            cci_score, cci_reasons = await self._cci_score_join(uid, gid, names)

            # Direct connections (existing clankers that cross threshold)
            connections = await self._detect_connections(uid, gid, names)

            # Near-misses (clankers below threshold but still suspicious)
            near_misses = await self._detect_near_misses(uid, gid, names)

            # Structural fingerprint -- build name groups from connections + this user
            conn_uids = [c["user_id"] for c in connections]
            fp_recs = await self.bot.db.fetch_all(
                "SELECT usernames, display_names FROM clanker_records "
                "WHERE user_id=ANY($1::bigint[]) AND guild_id=$2",
                conn_uids or [0], gid,
            ) if conn_uids else []
            fp_groups: list[list[str]] = [sorted(names)]
            for fp_r in (fp_recs or []):
                fp_names = list(set(fp_r.get("usernames") or []) | set(fp_r.get("display_names") or []))
                if fp_names:
                    fp_groups.append(fp_names)
            fingerprint = _build_structural_fingerprint(fp_groups) if len(fp_groups) >= 3 else None

            # Resolve display label for target
            if member:
                display_name = f"{member} (ID: {uid})"
            else:
                display_name = f"ID: {uid} (not in server)"

            # Names line
            names_line = ", ".join(f"`{n}`" for n in sorted(names)[:8])
            if len(names) > 8:
                names_line += f" + {len(names) - 8} more"

            # CCI score bar
            bar_filled = round(cci_score * 10)
            score_bar = "[" + "#" * bar_filled + "-" * (10 - bar_filled) + "]"
            risk_label = (
                "HIGH RISK" if cci_score >= 0.70
                else "ELEVATED" if cci_score >= 0.40
                else "LOW"
            )
            score_color = C_ERROR if cci_score >= 0.70 else (C_WARNING if cci_score >= 0.40 else C_SUCCESS)
            is_tracked = rec is not None

            _scan_fields: list[tuple[str, str]] = [
                ("CCI Score", f"`{score_bar}` {cci_score:.2f} -- {risk_label}"),
                ("Names", names_line),
                *([("Signals", ", ".join(cci_reasons))] if cci_reasons else []),
                ("Tracked clanker", "Yes" if is_tracked else "No"),
            ]
            if rec:
                clanked_ts = rec.get("clanked_at")
                if clanked_ts is not None:
                    _scan_fields.append(("Clanked at", fmt_ts(clanked_ts)))
                cluster_id = rec.get("cluster_id")
                if cluster_id:
                    _scan_fields.append(("Cluster", str(cluster_id)))

            if connections:
                conn_lines: list[str] = []
                for c in connections[:10]:
                    cm = ctx.guild.get_member(c["user_id"])
                    clabel = str(cm) if cm else f"ID:{c['user_id']}"
                    reasons_txt = ", ".join(c["reasons"])
                    conn_lines.append(f"- **{clabel}** ({reasons_txt})")
                _scan_fields.append((
                    f"Direct Connections ({len(connections)})",
                    "\n".join(conn_lines) + (f"\n...+{len(connections)-10} more" if len(connections) > 10 else ""),
                ))
            else:
                _scan_fields.append(("Direct Connections", "None found"))

            if near_misses:
                nm_lines: list[str] = []
                for nm in near_misses[:6]:
                    nm_m = ctx.guild.get_member(nm["user_id"])
                    nm_label = str(nm_m) if nm_m else f"ID:{nm['user_id']}"
                    nm_names_str = ", ".join(nm["other_names"][:2])
                    nm_lines.append(
                        f"- **{nm_label}** name:{nm['name_score']:.2f} msg:{nm['text_score']:.2f}"
                        + (f" [{nm_names_str}]" if nm_names_str else "")
                    )
                _scan_fields.append((f"Near-misses ({len(near_misses)})", "\n".join(nm_lines)))

            if fingerprint:
                _scan_fields.append(("Structural fingerprint", f"`{fingerprint}`"))

            result_view = _v2(f"CCI Scan: {display_name}", color=score_color, fields=_scan_fields)
            try:
                await status_msg.edit(view=result_view)
            except Exception:
                await ctx.reply(view=result_view, mention_author=False)

        except Exception:
            log.warning("clanktank: _cmd_scan_user failed uid=%s gid=%s", uid, gid, exc_info=True)
            try:
                await status_msg.edit(
                    view=_v2("Scan Failed", color=C_ERROR, desc="An error occurred during the user scan. Check bot logs.")
                )
            except Exception:
                pass

    @clanker_group.command(name="scan")
    @guild_only
    async def clanker_scan(self, ctx: DiscoContext, *, args: str = "") -> None:
        """Scan clankers. No args: full scan. @user/ID: user scan. @baseRole @stopRole: role-band scan."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        args = args.strip()

        if not args:
            await self._clanker_scan_active(ctx)
            return

        # Resolve tokens: role mentions are <@&ID>, user mentions are <@ID> or <@!ID>.
        tokens = args.split()
        base_role: discord.Role | None = None
        stop_role: discord.Role | None = None

        def _try_role(tok: str) -> discord.Role | None:
            if tok.startswith("<@&") and tok.endswith(">"):
                try:
                    return ctx.guild.get_role(int(tok[3:-1]))
                except ValueError:
                    return None
            try:
                return ctx.guild.get_role(int(tok))
            except ValueError:
                return None

        if len(tokens) >= 2:
            r1 = _try_role(tokens[0])
            r2 = _try_role(tokens[1])
            if r1 and r2:
                base_role, stop_role = r1, r2
            elif r1 and not r2:
                # One role and one non-role -- treat the whole thing as a user query
                pass

        if base_role is not None and stop_role is not None:
            # Role-band scan path
            pass  # falls through to the band scan block below
        elif len(tokens) == 1 and _try_role(tokens[0]) is not None:
            await ctx.reply_error("Provide two roles for a band scan: `,clanker scan @baseRole @stopRole`.")
            return
        else:
            # User-targeted scan
            await self._cmd_scan_user(ctx, args)
            return

        if base_role is None or stop_role is None:
            await ctx.reply_error("Use `,clanker scan @baseRole @stopRole`, for example `,clanker scan @users @level15`.")
            return

        if not ctx.guild.chunked:
            try:
                await ctx.guild.chunk(cache=True)
            except Exception:
                pass

        candidates, guarded = self._scan_role_candidates(ctx.guild, base_role, stop_role)
        band_label = self._role_band_label(base_role, stop_role)
        if not candidates:
            await ctx.reply(
                view=_v2(
                    "Role Band Scan",
                    color=C_INFO,
                    desc=(
                        f"No eligible members found from {band_label}. "
                        f"Guarded or out-of-band accounts skipped: **{len(guarded)}**."
                    ),
                    footer="The stop role is inclusive; anyone above that band is skipped.",
                ),
                mention_author=False,
            )
            return

        status_msg = await ctx.reply(
            view=_v2(
                "Scanning...",
                color=C_INFO,
                desc=(
                    f"Reviewing **{len(candidates)}** members from {band_label}.\n"
                    "Scoring against known patterns and checking name similarity. "
                    "No one will be contained automatically."
                ),
                fields=[
                    ("Guarded / skipped", str(len(guarded))),
                    ("Mode", "score + cluster only"),
                ],
            ),
            mention_author=False,
        )

        scored: list[dict] = []
        for member in candidates:
            try:
                scored.append(await self._score_scan_member(member))
            except Exception:
                log.debug("clanktank: role scan scoring failed uid=%s", member.id, exc_info=True)

        scored.sort(key=lambda row: float(row.get("score") or 0.0), reverse=True)
        meaningful = [row for row in scored if float(row.get("score") or 0.0) > 0.0 or row.get("direct_connections")]

        cluster_ids: list[int] = []
        existing_cluster_members: set[int] = set()
        # UIDs placed into new clusters by this scan (for display purposes)
        scan_clustered_uids: set[int] = set()

        for row in meaningful:
            direct_conns = row.get("direct_connections") or []
            if not direct_conns:
                continue
            connected_uids = [int(c["user_id"]) for c in direct_conns]
            found_cid = await self.bot.db.fetch_val(
                """SELECT cluster_id FROM clanker_records
                   WHERE guild_id=$1 AND user_id=ANY($2::bigint[])
                     AND cluster_id IS NOT NULL LIMIT 1""",
                ctx.guild.id, connected_uids,
            )
            if not found_cid:
                continue
            uid = int(row["member"].id)
            await self.bot.db.execute(
                """INSERT INTO clanker_cluster_members (cluster_id, guild_id, user_id)
                   VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                int(found_cid), ctx.guild.id, uid,
            )
            existing_cluster_members.add(uid)
            if int(found_cid) not in cluster_ids:
                cluster_ids.append(int(found_cid))

        # Run name-similarity clustering on ALL scored members so we detect
        # patterns even when no existing clankers are present to score against.
        # This is O(N^2) CPU work -- run in a thread so the event loop stays free.
        loop = asyncio.get_event_loop()
        components = await loop.run_in_executor(None, self._scan_components, scored)
        for comp in components:
            if comp <= existing_cluster_members:
                continue
            scan_clustered_uids |= comp
            avg_score = (
                sum(float(r.get("score") or 0.0) for r in scored if int(r["member"].id) in comp)
                / max(1, len(comp))
            )
            cid = await self._create_scan_cluster(
                ctx.guild.id,
                comp,
                label=f"Role scan: {base_role.name} to {stop_role.name}",
                confidence=max(0.25, min(0.95, avg_score)),
            )
            if cid:
                cluster_ids.append(cid)

        # Build display: members with a score OR placed in a cluster by this scan
        scored_by_id = {int(row["member"].id): row for row in scored}
        display_rows = [
            row for row in scored
            if float(row.get("score") or 0.0) > 0.0
            or row.get("direct_connections")
            or int(row["member"].id) in scan_clustered_uids
        ]
        display_rows.sort(
            key=lambda row: (float(row.get("score") or 0.0), int(row["member"].id) in scan_clustered_uids),
            reverse=True,
        )

        top_lines: list[str] = []
        for row in display_rows[:15]:
            member = row["member"]
            score = float(row.get("score") or 0.0)
            reasons = row.get("reasons") or []
            reason_text = ", ".join(str(r).replace("_", " ") for r in reasons[:3]) or "pattern review"
            direct = row.get("direct_connections") or []
            in_cluster = int(member.id) in scan_clustered_uids
            if direct:
                reason_text += f"; {len(direct)} active link(s)"
            if in_cluster and not reasons and not direct:
                reason_text = "name similarity match"
            score_str = f" {score:.0%}" if score > 0.0 else ""
            cluster_tag = " [clustered]" if in_cluster else ""
            top_lines.append(f"**{member.display_name}**{score_str}{cluster_tag}\n{reason_text}")

        total_flagged = len(display_rows)
        desc = (
            f"Scanned **{len(scored)}** members from {band_label}. "
            f"Skipped **{len(guarded)}** guarded or out-of-band. "
            "No containment action was taken."
        )
        if top_lines:
            desc += "\n\n" + "\n\n".join(top_lines)
            if len(display_rows) > 15:
                desc += f"\n\n*...and {len(display_rows) - 15} more flagged members*"
        else:
            desc += "\n\nNo suspicious patterns found in this role band."

        result_view = _v2(
            "Role Band Scan Complete",
            color=C_SUCCESS if not display_rows else C_WARNING,
            desc=desc,
            fields=[
                ("Role band", band_label),
                ("Flagged", str(total_flagged)),
                ("Clusters found", str(len(cluster_ids))),
                *([("Cluster IDs", ", ".join(f"[{cid}]" for cid in cluster_ids[:12]))] if cluster_ids else []),
            ],
            footer="Review clusters with ,clanker clusters -- cleave only after staff confirms.",
        )
        try:
            await status_msg.edit(view=result_view)
        except Exception:
            await ctx.reply(view=result_view, mention_author=False)

        await self._log_mod(_v2(
            "Clanktank Role Band Scan",
            color=C_INFO,
            fields=[
                ("Run by", str(ctx.author)),
                ("Role band", f"{base_role.name} through {stop_role.name}"),
                ("Scanned", str(len(scored))),
                ("Scored matches", str(len(meaningful))),
                ("Clusters touched", str(len(cluster_ids))),
            ],
        ))
        await self._audit(
            "role_scan", ctx.guild.id, actor_id=ctx.author.id,
            details={
                "base_role": base_role.id,
                "stop_role": stop_role.id,
                "scanned": len(scored),
                "matches": len(meaningful),
                "clusters": cluster_ids,
                "guarded": len(guarded),
            },
        )

    async def _clanker_scan_active(self, ctx: DiscoContext) -> None:
        count = await self.bot.db.fetch_val(
            "SELECT COUNT(*) FROM clanker_records WHERE guild_id=$1", ctx.guild.id
        )
        if not count or int(count) < 2:
            await ctx.reply_error("Need at least 2 tracked clankers to scan for connections.")
            return

        status_msg = await ctx.reply(
            view=_v2("Scanning...", color=C_INFO, desc=f"Analyzing **{count}** contained accounts for connections and cluster links."),
            mention_author=False,
        )

        found = await self._run_full_scan(ctx.guild.id)

        clusters_formed = 0
        clusters_updated = 0
        if found:
            await self._save_scan_connections(ctx.guild.id, found)
            await self._audit(
                "scan_connections", ctx.guild.id,
                actor_id=ctx.author.id,
                details={"new_connections": len(found)},
            )
            involved: set[int] = set()
            for conn in found:
                involved.add(conn["uid_a"])
                involved.add(conn["uid_b"])
            seen_components: set[frozenset[int]] = set()
            for uid in involved:
                try:
                    comp = await self._find_connected_component(uid, ctx.guild.id)
                    frozen = frozenset(comp)
                    if frozen in seen_components or len(comp) < _CLUSTER_MIN_SIZE:
                        seen_components.add(frozen)
                        continue
                    seen_components.add(frozen)
                    before_id = await self.bot.db.fetch_val(
                        "SELECT cluster_id FROM clanker_records WHERE user_id=$1 AND guild_id=$2",
                        uid, ctx.guild.id,
                    )
                    await self._check_and_form_cluster(uid, ctx.guild.id)
                    after_id = await self.bot.db.fetch_val(
                        "SELECT cluster_id FROM clanker_records WHERE user_id=$1 AND guild_id=$2",
                        uid, ctx.guild.id,
                    )
                    if after_id and not before_id:
                        clusters_formed += 1
                    elif after_id and before_id:
                        clusters_updated += 1
                except Exception:
                    log.warning("clanktank: scan cluster check failed uid=%s", uid, exc_info=True)

        lines: list[str] = []
        for conn in found:
            uid_a, uid_b = conn["uid_a"], conn["uid_b"]
            ma = ctx.guild.get_member(uid_a)
            mb = ctx.guild.get_member(uid_b)
            na = str(ma) if ma else f"ID:{uid_a}"
            nb = str(mb) if mb else f"ID:{uid_b}"
            lines.append(f"- **{na}** <-> **{nb}**: {', '.join(conn['reasons'])}")

        if not lines:
            result_view = _v2(
                "Scan Complete",
                color=C_SUCCESS,
                desc=f"Analyzed **{count}** contained accounts. No new connections found.",
            )
        else:
            cluster_note = ""
            if clusters_formed:
                cluster_note += f"\n{clusters_formed} new cluster(s) formed."
            if clusters_updated:
                cluster_note += f"\n{clusters_updated} cluster(s) updated."
            conn_preview = "\n".join(lines[:20])
            if len(lines) > 20:
                conn_preview += f"\n...and {len(lines) - 20} more"
            result_view = _v2(
                f"Scan Complete -- {len(found)} New Connection(s)",
                color=C_WARNING,
                desc=conn_preview + cluster_note,
                fields=[
                    ("Accounts scanned", str(count)),
                    ("New connections", str(len(found))),
                    *([("Clusters", f"{clusters_formed} formed, {clusters_updated} updated")] if clusters_formed or clusters_updated else []),
                ],
                footer="Use ,clanker clusters to review and ,clanker cluster cleave <id> to act.",
            )

        try:
            await status_msg.edit(view=result_view)
        except Exception:
            await ctx.reply(view=result_view, mention_author=False)

        await self._log_mod(_v2(
            "Clanktank Active Scan",
            color=C_INFO,
            fields=[
                ("Run by", str(ctx.author)),
                ("Clankers scanned", str(count)),
                ("New connections", str(len(found))),
                *([("New clusters formed", str(clusters_formed))] if clusters_formed else []),
            ],
        ))

    @clanker_group.command(name="sync")
    @guild_only
    async def clanker_sync(self, ctx: DiscoContext) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        guild = ctx.guild
        clanker_role = self._clanker_role(guild)
        if clanker_role is None:
            await ctx.reply_error(
                f'No role named "{_CLANKER_ROLE_NAME}" found.'
                + (" Set CLANKER_ROLE_ID to pin it by ID." if not Config.CLANKER_ROLE_ID else "")
            )
            return

        if not guild.chunked:
            try:
                await guild.chunk(cache=True)
            except Exception:
                pass

        holders = [m for m in guild.members if clanker_role in m.roles and not m.bot]
        if not holders:
            await ctx.reply(
                view=_v2("Clanktank Sync", color=C_INFO, desc="No members currently hold the Clanker role."),
                mention_author=False,
            )
            return

        added = skipped = 0
        for member in holders:
            existing = await self._get_record(member.id, guild.id)
            if existing:
                self._clanked.add((member.id, guild.id))
                skipped += 1
                continue
            try:
                await self.bot.db.execute(
                    """INSERT INTO clanker_records
                       (user_id, guild_id, stored_roles, reason, usernames, display_names)
                       VALUES ($1, $2, $3, $4, $5, $6)
                       ON CONFLICT (user_id, guild_id) DO NOTHING""",
                    member.id, guild.id, [], "retroactive sync",
                    [member.name], [member.display_name],
                )
                self._clanked.add((member.id, guild.id))
                added += 1
                await self._audit("sync_added", guild.id, user_id=member.id, actor_id=ctx.author.id)
            except Exception:
                log.warning("clanktank sync: insert failed uid=%s", member.id)

        await ctx.reply(
            view=_v2(
                "Clanktank Sync Complete",
                color=C_SUCCESS,
                desc="Retroactive entries have empty stored_roles -- original roles are unknown and cannot be restored on release.",
                fields=[
                    ("Role", clanker_role.name),
                    ("Members found", str(len(holders))),
                    ("Newly registered", str(added)),
                    ("Already tracked", str(skipped)),
                ],
            ),
            mention_author=False,
        )

        await self._log_mod(_v2(
            "Clanktank Sync",
            color=C_INFO,
            fields=[
                ("Run by", str(ctx.author)),
                ("Newly registered", str(added)),
                ("Already tracked", str(skipped)),
            ],
        ))


    @clanker_group.command(name="tree")
    @guild_only
    async def clanker_tree(self, ctx: DiscoContext, user: discord.Member) -> None:
        """Show an ASCII connection tree rooted at this clanker."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        rec = await self._get_record(user.id, ctx.guild.id)
        if rec is None:
            await ctx.reply_error(f"{user.mention} has no containment record.")
            return

        lines, component = await self._build_connection_tree(user.id, ctx.guild.id, ctx.guild)

        chunk = 20
        at_threshold = len(component) >= _CLUSTER_MIN_SIZE
        cluster_status = (
            f"THRESHOLD REACHED ({_CLUSTER_MIN_SIZE}+)" if at_threshold
            else f"below threshold ({len(component)}/{_CLUSTER_MIN_SIZE})"
        )
        tree_pages: list[str] = []
        for i in range(0, max(len(lines), 1), chunk):
            seg = lines[i: i + chunk]
            tree_pages.append(
                f"```\n{chr(10).join(seg)}\n```\n"
                f"**Connected accounts** {len(component)} | **Cluster status** {cluster_status}"
            )

        tree_title = f"Connection Tree: {user.display_name}"
        if len(tree_pages) == 1:
            await ctx.reply(view=_v2(tree_title, color=C_NAVY, desc=tree_pages[0]), mention_author=False)
        else:
            await ctx.reply(view=_PageView(ctx.author.id, tree_pages, title=tree_title, color=C_NAVY), mention_author=False)

    @clanker_group.group(name="clusters", invoke_without_command=True)
    @guild_only
    async def clanker_clusters_list(self, ctx: DiscoContext) -> None:
        """List all clanker clusters for this guild with sortable, paginated view."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        rows = await self.bot.db.fetch_all(
            """SELECT c.id, c.label, c.confidence, c.cleaved_at, c.created_at,
                      COUNT(m.user_id) AS member_count
               FROM clanker_clusters c
               LEFT JOIN clanker_cluster_members m ON m.cluster_id = c.id
               WHERE c.guild_id=$1
               GROUP BY c.id
               ORDER BY c.confidence DESC, c.created_at DESC""",
            ctx.guild.id,
        )

        if not rows:
            await ctx.reply(
                view=_v2(
                    "Clanker Clusters",
                    color=C_INFO,
                    desc=(
                        "No clusters formed yet.\n\n"
                        f"Clusters auto-form when {_CLUSTER_MIN_SIZE}+ connected accounts are detected. "
                        "Use `,clanker scan` to detect connections across all active clankers."
                    ),
                ),
                mention_author=False,
            )
            return

        view = _ClusterListView(ctx.author.id, [dict(r) for r in rows], ctx.guild, "confidence")
        await ctx.reply(view=view, mention_author=False)

    @clanker_clusters_list.command(name="clade")
    @guild_only
    async def clanker_clusters_clade(self, ctx: DiscoContext) -> None:
        """Re-run the CCI clustering pipeline and re-group all active clankers."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        await ctx.reply(
            view=_v2("Cluster Clade: Re-grouping...", color=C_INFO, desc="Running spectral clustering across all active clankers. This may take a moment."),
            mention_author=False,
        )

        gid = ctx.guild.id
        clanker_rows = await self.bot.db.fetch_all(
            "SELECT user_id FROM clanker_records WHERE guild_id=$1",
            gid,
        )
        if not clanker_rows:
            await ctx.reply(
                view=_v2("Cluster Clade", color=C_INFO, desc="No clanker records to cluster."),
                mention_author=False,
            )
            return

        uid_list = [int(r["user_id"]) for r in clanker_rows]
        old_cluster_count = await self.bot.db.fetch_val(
            "SELECT COUNT(*) FROM clanker_clusters WHERE guild_id=$1", gid,
        ) or 0

        try:
            await self._cci_run_clustering(gid)
        except Exception as exc:
            log.exception("clanktank: clade failed gid=%s", gid)
            await ctx.reply_error(f"Clustering failed: {exc}")
            return

        new_cluster_count = int(await self.bot.db.fetch_val(
            "SELECT COUNT(*) FROM clanker_clusters WHERE guild_id=$1", gid,
        ) or 0)
        formed = max(0, new_cluster_count - int(old_cluster_count))
        await self._audit(
            "clade_regroup", gid,
            actor_id=ctx.author.id,
            details={"clankers": len(uid_list), "clusters_before": int(old_cluster_count), "clusters_after": new_cluster_count},
        )
        await ctx.reply(
            view=_v2(
                "Cluster Clade Complete",
                color=C_SUCCESS,
                fields=[
                    ("Clankers analyzed", str(len(uid_list))),
                    ("Clusters before", str(old_cluster_count)),
                    ("Clusters after", str(new_cluster_count)),
                    ("New clusters formed", str(formed)),
                ],
                footer="Use ,clanker clusters to review the updated groups.",
            ),
            mention_author=False,
        )

    @clanker_group.group(name="cluster", invoke_without_command=True)
    @guild_only
    async def cluster_group(self, ctx: DiscoContext, cluster_id: int | None = None, *args: str) -> None:
        """Show cluster detail, or manage a cluster using `cluster <id> action`."""
        if cluster_id is not None and args:
            action = args[0].lower()
            if action in {"add", "remove", "rm"}:
                if len(args) < 2:
                    await ctx.reply_error(f"Use `,clanker cluster {cluster_id} {action} @user`.")
                    return
                try:
                    member = await commands.MemberConverter().convert(ctx, args[1])
                except commands.BadArgument:
                    await ctx.reply_error(f"Could not find `{args[1]}` in this server.")
                    return
                if action == "add":
                    await self._cluster_add_member(ctx, cluster_id, member)
                else:
                    await self._cluster_remove_member(ctx, cluster_id, member)
                return
            if action == "label":
                label = " ".join(args[1:]).strip()
                await self._cluster_set_label(ctx, cluster_id, label)
                return
            await ctx.reply_error("Unknown cluster action. Use `add`, `remove`, `label`, or `cleave`.")
            return
        if cluster_id is not None:
            await self._cmd_cluster_detail(ctx, cluster_id)
            return
        await ctx.reply(
            view=_v2(
                "Cluster Management",
                color=C_NAVY,
                fields=[
                    (",clanker cluster <id>", "Review members, patterns, and confidence."),
                    (",clanker cluster <id> add @user", "Add someone to a cluster for review."),
                    (",clanker cluster <id> remove @user", "Remove someone from a cluster."),
                    (",clanker cluster <id> label <name>", "Give the cluster a human-readable name."),
                    (",clanker cluster cleave <id>", "Mass-clank all eligible members after confirmation."),
                ],
                footer="Use ,clanker clusters to see all clusters for this server.",
            ),
            mention_author=False,
        )

    async def _cluster_set_label(self, ctx: DiscoContext, cluster_id: int, label: str) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        label = label.strip()[:200]
        if not label:
            await ctx.reply_error("Give the cluster a label.")
            return
        updated = await self.bot.db.fetch_val(
            """UPDATE clanker_clusters SET label=$1, updated_at=now()
               WHERE id=$2 AND guild_id=$3 RETURNING id""",
            label, cluster_id, ctx.guild.id,
        )
        if not updated:
            await ctx.reply_error(f"Cluster {cluster_id} not found in this guild.")
            return
        await self._audit(
            "cluster_labeled", ctx.guild.id, actor_id=ctx.author.id,
            details={"cluster_id": cluster_id, "label": label},
        )
        await ctx.reply(
            view=_v2(f"Cluster [{cluster_id}] Labeled", color=C_SUCCESS, desc=label),
            mention_author=False,
        )

    async def _cluster_add_member(self, ctx: DiscoContext, cluster_id: int, user: discord.Member) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        cluster = await self.bot.db.fetch_one(
            "SELECT id, label FROM clanker_clusters WHERE id=$1 AND guild_id=$2",
            cluster_id, ctx.guild.id,
        )
        if not cluster:
            await ctx.reply_error(f"Cluster {cluster_id} not found in this guild.")
            return
        await self.bot.db.execute(
            """INSERT INTO clanker_cluster_members (cluster_id, guild_id, user_id)
               VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
            cluster_id, ctx.guild.id, user.id,
        )
        if await self.is_clanker(user.id, ctx.guild.id):
            await self.bot.db.execute(
                "UPDATE clanker_records SET cluster_id=$1 WHERE user_id=$2 AND guild_id=$3",
                cluster_id, user.id, ctx.guild.id,
            )
        await self._audit(
            "cluster_member_added", ctx.guild.id, user_id=user.id, actor_id=ctx.author.id,
            details={"cluster_id": cluster_id},
        )
        await ctx.reply(
            view=_v2(
                f"Cluster [{cluster_id}] Updated",
                color=C_SUCCESS,
                fields=[
                    ("Added", f"{user.mention} ({user.id})"),
                    ("Label", cluster.get("label") or "unlabeled"),
                ],
            ),
            mention_author=False,
        )

    async def _cluster_remove_member(self, ctx: DiscoContext, cluster_id: int, user: discord.Member) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        removed = await self.bot.db.fetch_val(
            """DELETE FROM clanker_cluster_members
               WHERE cluster_id=$1 AND guild_id=$2 AND user_id=$3
               RETURNING user_id""",
            cluster_id, ctx.guild.id, user.id,
        )
        if not removed:
            await ctx.reply_error(f"{user.mention} is not listed in cluster {cluster_id}.")
            return
        await self.bot.db.execute(
            """UPDATE clanker_records SET cluster_id=NULL
               WHERE user_id=$1 AND guild_id=$2 AND cluster_id=$3""",
            user.id, ctx.guild.id, cluster_id,
        )
        await self._audit(
            "cluster_member_removed", ctx.guild.id, user_id=user.id, actor_id=ctx.author.id,
            details={"cluster_id": cluster_id},
        )
        await ctx.reply(
            view=_v2(f"Cluster [{cluster_id}] Updated", color=C_SUCCESS, desc=f"Removed {user.mention} from this cluster."),
            mention_author=False,
        )

    @cluster_group.command(name="label")
    @guild_only
    async def cluster_label(self, ctx: DiscoContext, cluster_id: int, *, label: str) -> None:
        """Set a human-readable cluster label."""
        await self._cluster_set_label(ctx, cluster_id, label)

    @cluster_group.command(name="add")
    @guild_only
    async def cluster_add(self, ctx: DiscoContext, cluster_id: int, user: discord.Member) -> None:
        """Add a member to a cluster without clanking them."""
        await self._cluster_add_member(ctx, cluster_id, user)

    @cluster_group.command(name="remove", aliases=["rm"])
    @guild_only
    async def cluster_remove(self, ctx: DiscoContext, cluster_id: int, user: discord.Member) -> None:
        """Remove a member from a cluster without releasing them."""
        await self._cluster_remove_member(ctx, cluster_id, user)

    @cluster_group.command(name="cleave")
    @guild_only
    async def cluster_cleave(self, ctx: DiscoContext, cluster_id: int) -> None:
        """Mass-clank all eligible members in a cluster."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        cluster = await self.bot.db.fetch_one(
            "SELECT * FROM clanker_clusters WHERE id=$1 AND guild_id=$2",
            cluster_id, ctx.guild.id,
        )
        if not cluster:
            await ctx.reply_error(f"Cluster {cluster_id} not found in this guild.")
            return

        member_rows = await self.bot.db.fetch_all(
            """SELECT ccm.user_id
               FROM clanker_cluster_members ccm
               WHERE ccm.cluster_id=$1""",
            cluster_id,
        )
        if not member_rows:
            await ctx.reply_error(f"Cluster {cluster_id} has no recorded members.")
            return

        to_clank: list[discord.Member] = []
        already_clanked: list[int] = []
        skipped_protected: list[str] = []
        not_in_server: list[int] = []

        for row in member_rows:
            m_uid = int(row["user_id"])
            if await self.is_clanker(m_uid, ctx.guild.id):
                already_clanked.append(m_uid)
                continue
            member = ctx.guild.get_member(m_uid)
            if not member:
                not_in_server.append(m_uid)
                continue
            p = member.guild_permissions
            if p.manage_roles or p.manage_messages or p.administrator:
                skipped_protected.append(str(member))
                continue
            level = await self.bot.db.fetch_val(
                "SELECT level FROM chat_levels WHERE guild_id=$1 AND user_id=$2",
                ctx.guild.id, m_uid,
            )
            if level and int(level) >= 30:
                skipped_protected.append(f"{member} (lvl {level})")
                continue
            to_clank.append(member)

        if not to_clank:
            await ctx.reply(
                view=_v2(
                    f"Cluster Cleave: [{cluster_id}]",
                    color=C_INFO,
                    desc=(
                        "No unclanked, unprotected members to cleave.\n\n"
                        f"  Already in containment: {len(already_clanked)}\n"
                        f"  Protected (skipped):    {len(skipped_protected)}\n"
                        f"  Not in server:          {len(not_in_server)}"
                    ),
                ),
                mention_author=False,
                delete_after=30.0,
            )
            return

        preview_lines = [f"  {m} ({m.id})" for m in to_clank[:15]]
        if len(to_clank) > 15:
            preview_lines.append(f"  ...and {len(to_clank) - 15} more")
        protected_str = ", ".join(skipped_protected[:5]) or "none"
        if len(skipped_protected) > 5:
            protected_str += f" ...+{len(skipped_protected) - 5}"

        await ctx.reply(
            view=_v2(
                f"Cluster Cleave Preview: [{cluster_id}]",
                color=C_WARNING,
                desc="Accounts that will be clanked:\n" + "\n".join(preview_lines),
                fields=[
                    ("Cluster confidence", f"{float(cluster.get('confidence') or 0):.2%}"),
                    ("Total cluster members", str(len(member_rows))),
                    ("Will be clanked", str(len(to_clank))),
                    ("Already in containment", str(len(already_clanked))),
                    ("Protected (skipped)", str(len(skipped_protected))),
                    ("Not in server", str(len(not_in_server))),
                    *([("Protected accounts", protected_str)] if skipped_protected else []),
                ],
                footer="Confirm to apply containment to all listed accounts simultaneously.",
            ),
            mention_author=False,
            delete_after=60.0,
        )

        confirmed = await ctx.confirm(
            f"Clank {len(to_clank)} account(s) from cluster {cluster_id}?",
            timeout=60.0,
        )
        if not confirmed:
            await ctx.reply_error("Cluster cleave cancelled.")
            return

        clanked_count = 0
        clanked_names: list[str] = []
        failed: list[str] = []
        for member in to_clank:
            try:
                await self._do_clank(
                    member, ctx.author,
                    f"cluster cleave [{cluster_id}]", None,
                    defer_purge=True,
                )
                await self.bot.db.execute(
                    "UPDATE clanker_records SET cluster_id=$1 WHERE user_id=$2 AND guild_id=$3",
                    cluster_id, member.id, ctx.guild.id,
                )
                clanked_count += 1
                clanked_names.append(f"{member} ({member.id})")
            except Exception as exc:
                failed.append(f"{member} ({member.id}): {exc}")

        await self.bot.db.execute(
            "UPDATE clanker_clusters SET cleaved_at=now(), updated_at=now() WHERE id=$1",
            cluster_id,
        )

        # Reinforce pattern weights now that cleave is confirmed.
        all_member_uids = {int(r["user_id"]) for r in member_rows}
        await self._save_cluster_patterns(cluster_id, ctx.guild.id, all_member_uids)

        await self._audit(
            "cluster_cleave", ctx.guild.id,
            actor_id=ctx.author.id,
            details={"cluster_id": cluster_id, "clanked": clanked_count, "failed": len(failed)},
        )

        await ctx.reply(
            view=_v2(
                f"Cluster Cleave Complete: [{cluster_id}]",
                color=C_SUCCESS,
                fields=[
                    ("Clanked", str(clanked_count)),
                    ("Failed", str(len(failed))),
                    ("Protected (skipped)", str(len(skipped_protected))),
                    *([("Contained", "\n".join(clanked_names[:10]))] if clanked_names else []),
                    *([("Failures", "\n".join(failed[:5]))] if failed else []),
                ],
            ),
            mention_author=False,
            delete_after=30.0,
        )
        log_lines = clanked_names[:20]
        if len(clanked_names) > 20:
            log_lines.append(f"...and {len(clanked_names) - 20} more")
        await self._log_mod(_v2(
            f"Cluster Cleave: [{cluster_id}]",
            color=C_WARNING,
            fields=[
                ("Run by", str(ctx.author)),
                ("Clanked", str(clanked_count)),
                ("Failed", str(len(failed))),
                *([("Contained accounts", "\n".join(log_lines))] if log_lines else []),
                *([("Failures", "\n".join(failed[:5]))] if failed else []),
            ],
        ))

    # -- cline: pattern listing -----------------------------------------------

    @clanker_group.command(name="cline")
    @guild_only
    async def clanker_cline(
        self,
        ctx: DiscoContext,
        target: discord.Member | None = None,
        cluster_id: int | None = None,
    ) -> None:
        """List detected patterns for a user, a cluster, or all patterns in this guild."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        gid = ctx.guild.id
        if target is not None:
            rows = await self.bot.db.fetch_all(
                "SELECT * FROM clanker_patterns WHERE guild_id=$1 AND user_id=$2 ORDER BY hits DESC",
                gid, target.id,
            )
            title = f"Patterns: @{target} ({target.id})"
        elif cluster_id is not None:
            rows = await self.bot.db.fetch_all(
                "SELECT * FROM clanker_patterns WHERE guild_id=$1 AND cluster_id=$2 ORDER BY weight DESC",
                gid, cluster_id,
            )
            title = f"Cluster [{cluster_id}] Patterns"
        else:
            rows = await self.bot.db.fetch_all(
                "SELECT * FROM clanker_patterns WHERE guild_id=$1 ORDER BY hits DESC LIMIT 300",
                gid,
            )
            title = "All Patterns"

        if not rows:
            await ctx.reply(
                view=_v2(title, color=C_INFO, desc="No patterns recorded yet."),
                mention_author=False,
            )
            return

        view = _ClineView(ctx.author.id, [dict(r) for r in rows], ctx.guild, "hits")
        await ctx.reply(view=view, mention_author=False)

    # -- chart command --------------------------------------------------------

    @clanker_group.command(name="chart")
    @guild_only
    async def clanker_chart(self, ctx: DiscoContext) -> None:
        """Generate a clanktank analytics overview chart for this server."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        # Pre-flight: confirm matplotlib is importable before doing any DB work.
        try:
            import matplotlib  # noqa: F401
        except ModuleNotFoundError:
            await ctx.reply_error(
                "matplotlib is not installed on this bot instance. "
                "Add `matplotlib>=3.9.0` to requirements.txt and redeploy."
            )
            return

        gid = ctx.guild.id
        try:
            day_rows = await self.bot.db.fetch_all(
                """
                SELECT DATE_TRUNC('day', clanked_at) AS day, COUNT(*) AS cnt
                FROM (
                    SELECT clanked_at FROM clanker_records WHERE guild_id=$1
                    UNION ALL
                    SELECT clanked_at FROM clanker_history WHERE guild_id=$1
                ) t
                WHERE clanked_at > NOW() - INTERVAL '30 days'
                GROUP BY day ORDER BY day
                """,
                gid,
            )
            score_rows = await self.bot.db.fetch_all(
                "SELECT score FROM clanker_records WHERE guild_id=$1", gid,
            )
            active_n = int(
                await self.bot.db.fetch_val(
                    "SELECT COUNT(*) FROM clanker_records WHERE guild_id=$1", gid
                ) or 0
            )
            # All-time processed = clanker_history (includes mod releases + escape releases)
            total_processed_n = int(
                await self.bot.db.fetch_val(
                    "SELECT COUNT(*) FROM clanker_history WHERE guild_id=$1", gid
                ) or 0
            )
            top_rows = await self.bot.db.fetch_all(
                "SELECT user_id, score FROM clanker_records "
                "WHERE guild_id=$1 ORDER BY score DESC LIMIT 10",
                gid,
            )
            escape_day_rows = await self.bot.db.fetch_all(
                """
                SELECT DATE_TRUNC('day', completed_at) AS day, COUNT(*) AS cnt
                FROM clank_escape
                WHERE guild_id=$1 AND completed_at IS NOT NULL
                  AND completed_at > NOW() - INTERVAL '30 days'
                GROUP BY day ORDER BY day
                """,
                gid,
            )
            escape_released_n = int(
                await self.bot.db.fetch_val(
                    "SELECT COUNT(*) FROM clank_escape WHERE guild_id=$1 AND completed_at IS NOT NULL",
                    gid,
                ) or 0
            )
            # Station distribution: how many active users are on each step
            step_rows = await self.bot.db.fetch_all(
                "SELECT step, COUNT(*) AS cnt FROM clank_escape "
                "WHERE guild_id=$1 AND completed_at IS NULL GROUP BY step ORDER BY step",
                gid,
            )
            avg_escape_s_raw = await self.bot.db.fetch_val(
                "SELECT AVG(EXTRACT(EPOCH FROM (completed_at - started_at)))::BIGINT "
                "FROM clank_escape WHERE guild_id=$1 AND completed_at IS NOT NULL",
                gid,
            )
            escape_started_n = int(
                await self.bot.db.fetch_val(
                    "SELECT COUNT(*) FROM clank_escape WHERE guild_id=$1",
                    gid,
                ) or 0
            )
        except Exception as exc:
            log.warning("clanktank: chart query failed gid=%s exc=%r", gid, exc)
            await ctx.reply_error("Failed to query clanktank data.")
            return

        mod_released_n = max(0, total_processed_n - escape_released_n)
        avg_escape_s = int(avg_escape_s_raw or 0)
        completion_rate = (escape_released_n / escape_started_n * 100) if escape_started_n else 0.0

        if avg_escape_s:
            h, rem = divmod(avg_escape_s, 3600)
            m2, _s = divmod(rem, 60)
            avg_escape_label = f"{h}h {m2}m" if h else f"{m2}m"
        else:
            avg_escape_label = "n/a"

        top_data: list[tuple[str, int]] = []
        for row in (top_rows or []):
            tid   = int(row.get("user_id") or 0)
            score = int(row.get("score") or 0)
            m     = ctx.guild.get_member(tid)
            name  = (m.display_name if m else f"ID:{tid}")[:20]
            top_data.append((name, score))

        step_dist: dict[int, int] = {int(r["step"]): int(r["cnt"]) for r in (step_rows or [])}

        chart_data = {
            "days":             [(float(r.get("day") or 0), int(r.get("cnt") or 0))
                                 for r in (day_rows or [])],
            "escape_days":      [(float(r.get("day") or 0), int(r.get("cnt") or 0))
                                 for r in (escape_day_rows or [])],
            "scores":           [int(r.get("score") or 0) for r in (score_rows or [])],
            "active":           active_n,
            "mod_released":     mod_released_n,
            "escape_released":  escape_released_n,
            "step_dist":        step_dist,
            "top":              top_data,
        }

        try:
            chart_bytes = await asyncio.to_thread(_generate_clanktank_chart, chart_data)
        except Exception as exc:
            log.warning("clanktank: chart render failed exc=%r", exc, exc_info=True)
            await ctx.reply_error(
                f"Chart generation failed: `{type(exc).__name__}: {exc}`\n"
                "Check Railway logs for the full traceback."
            )
            return

        buf  = io.BytesIO(chart_bytes)
        file = discord.File(buf, filename="clanktank_chart.png")
        chart_view = _V2Embed(discord.ui.Container(
            discord.ui.TextDisplay("## Clanktank Analytics"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f"**Active** {active_n}"
                f"  |  **Released (Escape)** {escape_released_n}"
                f"  |  **Released (Mod)** {mod_released_n}"
                f"  |  **Total Processed** {total_processed_n}\n"
                f"-# Avg escape time: {avg_escape_label}"
                f"  |  Completion rate: {completion_rate:.0f}%"
                f"  |  Escape rooms started: {escape_started_n}"
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem("attachment://clanktank_chart.png"),
            ),
            accent_color=C_NAVY,
        ))
        await ctx.reply(view=chart_view, file=file, mention_author=False)

    # -- hunter command group -------------------------------------------------

    @clanker_group.group(name="hunter", invoke_without_command=True)
    @guild_only
    async def clanker_hunter_group(self, ctx: DiscoContext) -> None:
        """Show scam hunter channel and whitelist configuration."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s       = await self.bot.db.get_guild_settings(ctx.guild.id)
        ch_id   = s.get("scam_report_channel")
        ch_str  = f"<#{ch_id}>" if ch_id else "not configured"
        ids     = [int(x) for x in (s.get("scam_hunter_ids") or [])]
        if ids:
            hunters_str = ", ".join(f"<@{i}>" for i in ids)
        else:
            hunters_str = "none"
        await ctx.reply(
            view=_v2(
                "Scam Hunter Settings",
                color=C_AMBER,
                fields=[
                    ("Report channel", ch_str),
                    ("Hunters whitelisted", str(len(ids))),
                    ("Hunters", hunters_str[:1000]),
                ],
                footer="Use ,clanker hunter channel #channel -- ,clanker hunter add @user -- ,clanker hunter remove @user",
            ),
            mention_author=False,
        )

    @clanker_hunter_group.command(name="channel")
    @guild_only
    async def clanker_hunter_channel(
        self, ctx: DiscoContext, channel: discord.TextChannel | None = None
    ) -> None:
        """Set or clear the scam report channel. Omit channel to clear."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        new_id = channel.id if channel else None
        await self.bot.db.update_guild_setting(ctx.guild.id, "scam_report_channel", new_id)
        if channel:
            await ctx.reply_success(
                f"Scam report channel set to {channel.mention}. "
                "Whitelisted hunters who post user IDs or @mentions there "
                "will trigger automatic clanking.",
                title="Hunter Channel Set",
            )
        else:
            await ctx.reply_success("Scam report channel cleared.", title="Hunter Channel Cleared")
        log.info(
            "clanktank: hunter channel set ch=%s gid=%s actor=%s",
            new_id, ctx.guild.id, ctx.author.id,
        )

    @clanker_hunter_group.command(name="add")
    @guild_only
    async def clanker_hunter_add(self, ctx: DiscoContext, user: discord.Member) -> None:
        """Add a user to the scam hunter whitelist."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        if user.bot:
            await ctx.reply_error("Cannot add a bot to the hunter whitelist.")
            return
        s   = await self.bot.db.get_guild_settings(ctx.guild.id)
        ids = [int(x) for x in (s.get("scam_hunter_ids") or [])]
        if user.id in ids:
            await ctx.reply_error(f"{user.mention} is already a hunter.")
            return
        if len(ids) >= 50:
            await ctx.reply_error("Hunter whitelist is full (max 50).")
            return
        ids.append(user.id)
        await self.bot.db.update_guild_setting(ctx.guild.id, "scam_hunter_ids", ids)
        await ctx.reply_success(
            f"{user.mention} added to the scam hunter whitelist. "
            "Their reports in the scam report channel will auto-clank flagged users.",
            title="Hunter Added",
        )
        log.info(
            "clanktank: hunter added uid=%s gid=%s actor=%s",
            user.id, ctx.guild.id, ctx.author.id,
        )

    @clanker_hunter_group.command(name="remove")
    @guild_only
    async def clanker_hunter_remove(self, ctx: DiscoContext, user: discord.Member) -> None:
        """Remove a user from the scam hunter whitelist."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s   = await self.bot.db.get_guild_settings(ctx.guild.id)
        ids = [int(x) for x in (s.get("scam_hunter_ids") or [])]
        if user.id not in ids:
            await ctx.reply_error(f"{user.mention} is not in the hunter whitelist.")
            return
        ids.remove(user.id)
        await self.bot.db.update_guild_setting(ctx.guild.id, "scam_hunter_ids", ids)
        await ctx.reply_success(f"{user.mention} removed from the scam hunter whitelist.", title="Hunter Removed")
        log.info(
            "clanktank: hunter removed uid=%s gid=%s actor=%s",
            user.id, ctx.guild.id, ctx.author.id,
        )

    @clanker_hunter_group.command(name="list")
    @guild_only
    async def clanker_hunter_list(self, ctx: DiscoContext) -> None:
        """List all scam hunters in this server."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s   = await self.bot.db.get_guild_settings(ctx.guild.id)
        ids = [int(x) for x in (s.get("scam_hunter_ids") or [])]
        if not ids:
            await ctx.reply_error("No scam hunters configured. Use `,clanker hunter add @user`.")
            return
        lines = []
        for hid in ids:
            m = ctx.guild.get_member(hid)
            lines.append(f"<@{hid}>" + (f" -- {m.display_name}" if m else " (not in server)"))
        ch_id  = s.get("scam_report_channel")
        ch_str = f"<#{ch_id}>" if ch_id else "not configured"
        await ctx.reply(
            view=_v2(
                f"Scam Hunters ({len(ids)})",
                color=C_AMBER,
                desc="\n".join(lines),
                fields=[("Report channel", ch_str)],
            ),
            mention_author=False,
        )

    # -- clamp command group --------------------------------------------------

    @clanker_group.group(name="clamp", invoke_without_command=True)
    @guild_only
    async def clamp_group(self, ctx: DiscoContext) -> None:
        """Show and toggle Clanktank Clamp guard settings."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s = await self.bot.db.get_guild_settings(ctx.guild.id)
        view = _ClampSettingsView(ctx.author.id, s, ctx.guild.id, self.bot.db)
        await ctx.reply(view=view, mention_author=False)

    @clamp_group.group(name="clear", invoke_without_command=True)
    @guild_only
    async def clamp_clear_group(self, ctx: DiscoContext) -> None:
        """Show clamp clear settings panel."""
        s = await self.bot.db.get_guild_settings(ctx.guild.id)
        view = _ClampSettingsView(ctx.author.id, s, ctx.guild.id, self.bot.db)
        await ctx.reply(view=view, mention_author=False)

    @clamp_clear_group.command(name="urls")
    @guild_only
    async def clamp_clear_urls_cmd(self, ctx: DiscoContext) -> None:
        """Toggle URL auto-deletion for contained users."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s = await self.bot.db.get_guild_settings(ctx.guild.id)
        new = not bool(s.get("clamp_clear_urls", True))
        await self.bot.db.update_guild_setting(ctx.guild.id, "clamp_clear_urls", new)
        await ctx.reply_success(f"URL auto-deletion is now **{'ON' if new else 'OFF'}**.")
        log.info("clanktank: clamp_clear_urls toggled=%s gid=%s actor=%s", new, ctx.guild.id, ctx.author.id)

    @clamp_clear_group.command(name="addresses")
    @guild_only
    async def clamp_clear_addresses_cmd(self, ctx: DiscoContext) -> None:
        """Toggle crypto address auto-deletion for contained users."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s = await self.bot.db.get_guild_settings(ctx.guild.id)
        new = not bool(s.get("clamp_clear_addresses", True))
        await self.bot.db.update_guild_setting(ctx.guild.id, "clamp_clear_addresses", new)
        await ctx.reply_success(f"Crypto address auto-deletion is now **{'ON' if new else 'OFF'}**.")
        log.info("clanktank: clamp_clear_addresses toggled=%s gid=%s actor=%s", new, ctx.guild.id, ctx.author.id)

    @clamp_clear_group.command(name="scams")
    @guild_only
    async def clamp_clear_scams_cmd(self, ctx: DiscoContext) -> None:
        """Toggle scam pattern detection in guard channels."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s = await self.bot.db.get_guild_settings(ctx.guild.id)
        new = not bool(s.get("clamp_clear_scams", True))
        await self.bot.db.update_guild_setting(ctx.guild.id, "clamp_clear_scams", new)
        await ctx.reply_success(f"Scam pattern detection is now **{'ON' if new else 'OFF'}**.")
        log.info("clanktank: clamp_clear_scams toggled=%s gid=%s actor=%s", new, ctx.guild.id, ctx.author.id)

    @clamp_group.group(name="clasp", invoke_without_command=True)
    @guild_only
    async def clasp_group(self, ctx: DiscoContext) -> None:
        """Show clasp auto-action settings panel."""
        s = await self.bot.db.get_guild_settings(ctx.guild.id)
        view = _ClampSettingsView(ctx.author.id, s, ctx.guild.id, self.bot.db)
        await ctx.reply(view=view, mention_author=False)

    @clasp_group.command(name="mute")
    @guild_only
    async def clasp_mute_cmd(self, ctx: DiscoContext) -> None:
        """Toggle auto-mute (15 min timeout) for clamp guard violations."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s = await self.bot.db.get_guild_settings(ctx.guild.id)
        new = not bool(s.get("clasp_auto_mute", False))
        await self.bot.db.update_guild_setting(ctx.guild.id, "clasp_auto_mute", new)
        await ctx.reply_success(f"Clasp auto-mute is now **{'ON' if new else 'OFF'}**.")
        log.info("clanktank: clasp_auto_mute toggled=%s gid=%s actor=%s", new, ctx.guild.id, ctx.author.id)

    @clasp_group.command(name="delete")
    @guild_only
    async def clasp_delete_cmd(self, ctx: DiscoContext) -> None:
        """Toggle auto-delete for clamp guard violations in guard channels."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s = await self.bot.db.get_guild_settings(ctx.guild.id)
        new = not bool(s.get("clasp_auto_delete", False))
        await self.bot.db.update_guild_setting(ctx.guild.id, "clasp_auto_delete", new)
        await ctx.reply_success(f"Clasp auto-delete is now **{'ON' if new else 'OFF'}**.")
        log.info("clanktank: clasp_auto_delete toggled=%s gid=%s actor=%s", new, ctx.guild.id, ctx.author.id)

    @clasp_group.command(name="channel")
    @guild_only
    async def clasp_channel_cmd(self, ctx: DiscoContext, channel: discord.TextChannel) -> None:
        """Add or remove a channel from the clamp guard list (toggles on/off)."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        s = await self.bot.db.get_guild_settings(ctx.guild.id)
        guard_ids: list[int] = [int(x) for x in (s.get("clamp_channel_ids") or [])]
        ch_id = channel.id
        if ch_id in guard_ids:
            guard_ids.remove(ch_id)
            action = "removed from"
        else:
            guard_ids.append(ch_id)
            action = "added to"
        await self.bot.db.update_guild_setting(ctx.guild.id, "clamp_channel_ids", guard_ids)
        ch_list = " ".join(f"<#{c}>" for c in guard_ids) or "none"
        await ctx.reply_success(
            f"{channel.mention} {action} the guard channel list.\n"
            f"Active guard channels: {ch_list}"
        )
        log.info(
            "clanktank: clasp_channel %s gid=%s actor=%s guards=%s",
            action, ctx.guild.id, ctx.author.id, guard_ids,
        )

    @clamp_group.command(name="clutch")
    @guild_only
    async def clamp_clutch(self, ctx: DiscoContext, max_clank: int | None = None) -> None:
        """Scan server members for scam signals; optionally clank up to <max> accounts."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        gid = ctx.guild.id
        default_role = ctx.guild.default_role
        suspects: list[tuple[discord.Member, str]] = []

        for member in ctx.guild.members:
            if member.bot:
                continue
            roles = [r for r in member.roles if r != default_role]
            if any(r.permissions.manage_roles or r.permissions.administrator for r in roles):
                continue
            if await self.is_clanker(member.id, gid):
                continue
            names = {member.name, member.display_name}
            kw = _scam_name_hit(names)
            if kw:
                suspects.append((member, f"name keyword: {kw!r}"))
                continue
            try:
                cci_score, _ = await self._cci_score_join(member.id, gid, names)
            except Exception:
                cci_score = 0.0
            if cci_score >= 0.80:
                suspects.append((member, f"CCI score: {cci_score:.0%}"))

        if not suspects:
            await ctx.reply(
                view=_v2("Clutch Scan", color=C_SUCCESS, desc="No suspicious accounts detected among server members."),
                mention_author=False,
            )
            return

        lines = [f"**@{m} ({m.id})** - {reason}" for m, reason in suspects[:20]]
        if len(suspects) > 20:
            lines.append(f"...and {len(suspects) - 20} more")
        await ctx.reply(
            view=_v2(
                f"Clutch Scan: {len(suspects)} suspect(s)",
                color=C_WARNING,
                desc="\n".join(lines),
                footer=(
                    "Pass a number to auto-clank: `,clanker clamp clutch 5`"
                    if max_clank is None else f"Will clank up to {max_clank}."
                ),
            ),
            mention_author=False,
        )

        if max_clank is None:
            return

        to_clank = suspects[:max_clank]
        confirmed = await ctx.confirm(
            f"Clank {len(to_clank)} suspect account(s) from clutch scan?",
            timeout=60.0,
        )
        if not confirmed:
            await ctx.reply_error("Clutch cancelled.")
            return

        clanked = 0
        failed: list[str] = []
        for member, reason in to_clank:
            try:
                await self._do_clank(
                    member, ctx.author,
                    f"clutch scan ({reason})", None,
                    defer_purge=True,
                )
                clanked += 1
            except Exception as exc:
                failed.append(f"@{member} ({member.id}): {exc}")

        await self._audit(
            "clutch", gid,
            actor_id=ctx.author.id,
            details={"clanked": clanked, "failed": len(failed)},
        )
        await ctx.reply(
            view=_v2(
                "Clutch Complete",
                color=C_SUCCESS,
                fields=[
                    ("Clanked", str(clanked)),
                    ("Failed", str(len(failed))),
                    *([("Failures", "\n".join(failed[:5]))] if failed else []),
                ],
            ),
            mention_author=False,
        )

    @clamp_group.command(name="cloister")
    @guild_only
    async def clamp_cloister(self, ctx: DiscoContext, target: discord.Member) -> None:
        """Isolate a user in a private thread and delete their recent messages from this channel."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        gid = ctx.guild.id
        channel = ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.reply_error("Cloister must be run in a text channel.")
            return

        try:
            thread = await channel.create_thread(
                name=f"cloister-{target.name[:20]}",
                type=discord.ChannelType.private_thread,
                invitable=False,
                reason=f"Clanktank cloister: {ctx.author} isolated {target}",
            )
            await thread.add_user(target)
            await thread.send(
                f"@{target} ({target.id}): You have been moved to a private review thread by "
                "moderation. Please wait for a staff member to follow up."
            )
        except Exception as exc:
            await ctx.reply_error(f"Failed to create isolation thread: {exc}")
            return

        deleted = 0
        try:
            purged = await channel.purge(
                limit=100,
                check=lambda m: m.author.id == target.id,
                reason=f"Clanktank cloister: {ctx.author} isolated {target}",
            )
            deleted = len(purged)
        except Exception:
            pass

        await self._audit(
            "cloister", gid,
            actor_id=ctx.author.id,
            details={
                "target_id": target.id,
                "channel_id": channel.id,
                "thread_id": thread.id,
                "messages_deleted": deleted,
            },
        )
        await self._log_mod(_v2(
            "Cloister: User Isolated",
            color=C_AMBER,
            fields=[
                ("Target", f"@{target} ({target.id})"),
                ("Run by", f"@{ctx.author} ({ctx.author.id})"),
                ("Thread", thread.mention),
                ("Messages removed", str(deleted)),
            ],
        ))
        await ctx.reply(
            view=_v2(
                "Cloister Applied",
                color=C_SUCCESS,
                fields=[
                    ("User", f"@{target} ({target.id})"),
                    ("Private thread", thread.mention),
                    ("Messages removed", str(deleted)),
                ],
            ),
            mention_author=False,
        )

    @clamp_group.command(name="clad")
    @guild_only
    async def clamp_clad(self, ctx: DiscoContext) -> None:
        """Mute all active clankers for 15 minutes and purge 100 messages from the tank channel."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        confirmed = await ctx.confirm(
            "Timeout ALL active clankers for 15 min and delete 100 tank-channel messages?",
            timeout=30.0,
        )
        if not confirmed:
            await ctx.reply_error("Clad cancelled.")
            return

        gid = ctx.guild.id
        clanker_rows = await self.bot.db.fetch_all(
            "SELECT user_id FROM clanker_records WHERE guild_id=$1", gid,
        )

        muted = 0
        failed_mute: list[str] = []
        for row in clanker_rows:
            uid = int(row["user_id"])
            member = ctx.guild.get_member(uid)
            if member:
                try:
                    await member.timeout(
                        timedelta(minutes=15),
                        reason=f"Clanktank clad by {ctx.author}",
                    )
                    muted += 1
                except Exception:
                    failed_mute.append(str(uid))

        deleted = 0
        tank_id = Config.CLANKTANK_CHANNEL_ID
        if tank_id:
            tank_ch = ctx.guild.get_channel(tank_id)
            if isinstance(tank_ch, discord.TextChannel):
                try:
                    purged = await tank_ch.purge(
                        limit=100,
                        reason=f"Clanktank clad by {ctx.author}",
                    )
                    deleted = len(purged)
                except Exception:
                    pass

        await self._audit(
            "clad", gid,
            actor_id=ctx.author.id,
            details={"muted": muted, "tank_messages_deleted": deleted},
        )
        await self._log_mod(_v2(
            "Clad: Bulk Mute + Tank Purge",
            color=C_ERROR,
            fields=[
                ("Run by", f"@{ctx.author} ({ctx.author.id})"),
                ("Clankers muted", str(muted)),
                ("Tank messages purged", str(deleted)),
                *([("Failed mutes", ", ".join(failed_mute[:10]))] if failed_mute else []),
            ],
        ))
        await ctx.reply(
            view=_v2(
                "Clad Applied",
                color=C_SUCCESS,
                fields=[
                    ("Clankers muted (15 min)", str(muted)),
                    ("Tank messages deleted", str(deleted)),
                ],
            ),
            mention_author=False,
        )

    @clamp_group.command(name="clink")
    @guild_only
    async def clamp_clink(self, ctx: DiscoContext, target: discord.Member) -> None:
        """Run impersonation pattern analysis on a user (name signals, CCI, account age, evidence)."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        uid = target.id
        gid = ctx.guild.id
        names = {target.name, target.display_name}

        name_hit = _scam_name_hit(names)
        account_age_days = (
            (discord.utils.utcnow() - target.created_at).days
            if target.created_at else None
        )
        join_age_days = (
            (discord.utils.utcnow() - target.joined_at).days
            if target.joined_at else None
        )

        cci: float | None = None
        try:
            cci, _ = await self._cci_score_join(uid, gid, names)
        except Exception:
            pass

        ev_rows = await self.bot.db.fetch_all(
            "SELECT content, evidence_type FROM clanker_evidence "
            "WHERE user_id=$1 AND guild_id=$2 ORDER BY logged_at DESC LIMIT 5",
            uid, gid,
        )
        known_clanker = await self.is_clanker(uid, gid)
        cluster_row = await self.bot.db.fetch_one(
            "SELECT cluster_id FROM clanker_records WHERE user_id=$1 AND guild_id=$2",
            uid, gid,
        )
        cluster_id = cluster_row.get("cluster_id") if cluster_row else None

        risk_flags: list[str] = []
        if name_hit:
            risk_flags.append(f"scam keyword in name: {name_hit!r}")
        if account_age_days is not None and account_age_days < 30:
            risk_flags.append(f"new account ({account_age_days}d old)")
        if join_age_days is not None and join_age_days < 7:
            risk_flags.append(f"recently joined ({join_age_days}d ago)")
        if cci is not None and cci >= 0.80:
            risk_flags.append(f"high CCI score ({cci:.0%})")
        if cluster_id:
            risk_flags.append(f"in cluster [{cluster_id}]")

        if len(risk_flags) >= 3 or (name_hit and cci is not None and cci >= 0.80):
            risk_level = "HIGH"
        elif risk_flags:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"

        color = C_ERROR if risk_level == "HIGH" else C_WARNING if risk_level == "MEDIUM" else C_SUCCESS
        _clink_fields: list[tuple[str, str]] = [
            ("Risk level", risk_level),
            ("Account age", f"{account_age_days}d" if account_age_days is not None else "?"),
            ("Join age", f"{join_age_days}d" if join_age_days is not None else "?"),
            ("CCI score", f"{cci:.0%}" if cci is not None else "n/a"),
            ("Known clanker", "yes" if known_clanker else "no"),
            ("Cluster", f"[{cluster_id}]" if cluster_id else "none"),
        ]
        if risk_flags:
            _clink_fields.append(("Risk signals", "\n".join(f"- {f}" for f in risk_flags)))
        if ev_rows:
            ev_lines = [
                f"`{r.get('evidence_type', '?')}`: {str(r.get('content') or '')[:80]}"
                for r in ev_rows
            ]
            _clink_fields.append(("Recent evidence", "\n".join(ev_lines)))
        await ctx.reply(
            view=_v2(f"Clink: @{target} ({uid})", color=color, fields=_clink_fields),
            mention_author=False,
        )

    @clanker_group.command(name="clarion")
    @guild_only
    async def clanker_clarion(self, ctx: DiscoContext, *, message: str) -> None:
        """Send an anonymous AI-reformulated message to the clanktank channel."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return

        level_val = await self.bot.db.fetch_val(
            "SELECT level FROM chat_levels WHERE guild_id=$1 AND user_id=$2",
            ctx.guild.id, ctx.author.id,
        )
        level = int(level_val or 0)
        if level < 15:
            await ctx.reply_error(f"Clarion requires level 15+. You are level {level}.")
            return

        if _URL_RE.search(message):
            await ctx.reply_error("Clarion messages cannot contain URLs.")
            return
        if _CRYPTO_RE.search(message):
            await ctx.reply_error("Clarion messages cannot contain cryptocurrency addresses.")
            return
        kw = _scam_name_hit({message})
        if kw:
            await ctx.reply_error(f"Clarion messages cannot contain scam keywords (`{kw}`).")
            return

        gid = ctx.guild.id
        now = time.time()

        if gid not in self._clarion_rate:
            self._clarion_rate[gid] = collections.deque()
        dq = self._clarion_rate[gid]
        while dq and now - dq[0] > 3600:
            dq.popleft()
        if len(dq) >= 10:
            wait_s = int(3600 - (now - dq[0])) + 1
            await ctx.reply_error(f"Clarion rate limit: max 10/hour. Try again in {wait_s}s.")
            return

        tank_ch: discord.TextChannel | None = None
        if Config.CLANKTANK_CHANNEL_ID:
            ch = ctx.guild.get_channel(Config.CLANKTANK_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                tank_ch = ch
        if tank_ch is None:
            await ctx.reply_error("Clanktank channel not configured or not found.")
            return

        from core.framework.ai import complete_default as _ai_complete
        reformulate_msgs = [
            {
                "role": "system",
                "content": (
                    "You are Disco, a friendly Discord AI. Rewrite the following moderator "
                    "instruction as a short, casual, natural message (1-3 sentences) directed "
                    "at the person described. Do not include URLs. Do not add meta-commentary. "
                    "Respond ONLY with the rewritten message."
                ),
            },
            {"role": "user", "content": message},
        ]
        try:
            reformulated = await _ai_complete(reformulate_msgs, max_tokens=200, temperature=0.85)
        except Exception:
            log.exception("clarion: AI reformulation failed gid=%s", gid)
            reformulated = None

        if not reformulated or not reformulated.strip():
            await ctx.reply_error("AI failed to reformulate the message. Try again.")
            return

        reformulated = reformulated.strip()

        try:
            sent = await tank_ch.send(reformulated)
        except discord.HTTPException as exc:
            await ctx.reply_error(f"Failed to send: {exc}")
            return

        self._clarion_msg_ids.add(sent.id)
        dq.append(now)

        await self._log_mod(_v2(
            "Clarion Sent",
            color=C_INFO,
            fields=[
                ("Moderator", f"{ctx.author} ({ctx.author.id})"),
                ("Channel", tank_ch.mention),
                ("Original message", message[:1000]),
                ("Reformulated", reformulated[:1000]),
            ],
        ))
        await ctx.reply(
            view=_v2(
                color=C_INFO,
                desc=f"Sent to {tank_ch.mention}:\n\n{reformulated}",
            ),
            mention_author=False,
        )

    @clanker_group.command(name="escape")
    @guild_only
    async def clanker_escape_cmd(self, ctx: DiscoContext) -> None:
        """Get your escape room link (clankers only)."""
        uid, gid = ctx.author.id, ctx.guild.id
        if not await self.is_clanker(uid, gid):
            await ctx.reply_error("You are not currently in containment. Nothing to escape from.")
            return
        # Handles creation, stale-message detection, and view re-registration after restarts.
        err = await self._start_escape_room(ctx.author)
        if err:
            await ctx.reply_error(err)
            return
        row = await self._er_get(uid, gid)
        if not row:
            await ctx.reply_error(
                "Your escape room could not be created. Ask a server admin to verify "
                "`CLANK_ESCAPE_THREAD_ID` is set to a valid thread ID."
            )
            return
        thread_id = row.get("thread_id")
        message_id = row.get("message_id")
        if thread_id and message_id:
            jump = f"https://discord.com/channels/{gid}/{thread_id}/{message_id}"
            try:
                await ctx.author.send(f"\U0001f517 **Your escape room link:**\n{jump}")
            except Exception:
                pass
        _notice = await ctx.reply(
            view=_v2(
                color=C_INFO,
                desc="run `,clanker escape` to get the link to your escape room -- work through the 8 stations to request release. check your DMs if you missed the original link.",
            ),
            mention_author=False,
            no_autodelete=True,
        )
        if _notice is not None:
            async def _del_notice() -> None:
                await asyncio.sleep(8.0)
                try:
                    await _notice.delete()
                except Exception:
                    pass
            asyncio.ensure_future(_del_notice())

    # -- Escape room administration -------------------------------------------

    @clanker_group.group(name="er", invoke_without_command=True)
    @guild_only
    async def clanker_er(self, ctx: DiscoContext) -> None:
        """Escape-room admin tools (status / reload / reset / purge / setthread)."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        await self._er_status(ctx)

    async def _er_status(self, ctx: DiscoContext) -> None:
        gid = ctx.guild.id
        tid = self._escape_thread_id()
        source = "runtime override" if self._escape_thread_override else ("env var" if Config.CLANK_ESCAPE_THREAD_ID else "unset")

        thread = await self._get_escape_thread()
        if thread is None:
            thread_state = "NOT REACHABLE" if tid else "NOT CONFIGURED"
            thread_name = "--"
            perms_line = "n/a"
        else:
            thread_state = "OK"
            thread_name = f"{thread.mention} (`{thread.name}`)"
            me = thread.guild.me
            p = thread.permissions_for(me) if me else None
            if p:
                can_send = p.send_messages_in_threads or p.send_messages
                perms_line = (
                    f"send: {'yes' if can_send else 'NO'} | "
                    f"manage msgs: {'yes' if p.manage_messages else 'NO'} | "
                    f"add members: {'yes' if p.manage_threads else 'NO'}"
                )
            else:
                perms_line = "unknown"

        try:
            db_active = await self.bot.db.fetch_val(
                "SELECT COUNT(*) FROM clank_escape WHERE guild_id=$1 AND completed_at IS NULL",
                gid,
            ) or 0
            db_with_msg = await self.bot.db.fetch_val(
                "SELECT COUNT(*) FROM clank_escape WHERE guild_id=$1 AND completed_at IS NULL AND message_id IS NOT NULL",
                gid,
            ) or 0
        except Exception:
            db_active = db_with_msg = "?"

        clanked_here = sum(1 for _, g in self._clanked if g == gid)
        color = C_SUCCESS if thread_state == "OK" else C_ERROR
        await ctx.reply(
            view=_v2(
                "Escape Room -- System Status",
                color=color,
                fields=[
                    ("Thread", f"`{tid or 0}` ({source})\n{thread_name}"),
                    ("Reachable", thread_state),
                    ("Bot permissions", perms_line),
                    ("Active rooms (DB)", f"{db_active} ({db_with_msg} with embeds)"),
                    ("Registered views (memory)", str(len(self._escape_msg_ids))),
                    ("Clankers in this guild", str(clanked_here)),
                    ("Station-5 wait", f"{Config.CLANK_ESCAPE_WAIT_MINUTES} min"),
                ],
                footer="reload re-registers buttons | reset <user> rebuilds a room | purge wipes the thread",
            ),
            mention_author=False,
        )

    @clanker_er.command(name="status")
    @guild_only
    async def clanker_er_status(self, ctx: DiscoContext) -> None:
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        await self._er_status(ctx)

    @clanker_er.command(name="reload")
    @guild_only
    async def clanker_er_reload(self, ctx: DiscoContext) -> None:
        """Re-register all escape-room buttons live (fixes dead buttons after a deploy)."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        await self._load_escape_thread_override()
        registered, cleared = await self._restore_escape_views()
        await ctx.reply(
            view=_v2(
                "Escape Room -- Reloaded",
                color=C_SUCCESS,
                desc=(
                    f"Re-registered **{registered}** escape-room view(s).\n"
                    f"Cleared **{cleared}** dead message pointer(s).\n\n"
                    "Buttons are live again -- no bot restart needed."
                ),
            ),
            mention_author=False,
            delete_after=30.0,
        )

    @clanker_er.command(name="reset")
    @guild_only
    async def clanker_er_reset(self, ctx: DiscoContext, user: discord.Member) -> None:
        """Wipe and rebuild a clanker's escape room from station 1."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        if not await self.is_clanker(user.id, ctx.guild.id):
            await ctx.reply_error(f"{user.mention} is not currently in containment.")
            return
        err = await self._start_escape_room(user, force_new=True)
        if err:
            await ctx.reply_error(err)
            return
        await ctx.reply(
            view=_v2(
                "Escape Room -- Reset",
                color=C_SUCCESS,
                desc=f"Rebuilt {user.mention}'s escape room from station 1. Old messages were purged.",
            ),
            mention_author=False,
            delete_after=30.0,
        )

    @clanker_er.command(name="purge")
    @guild_only
    async def clanker_er_purge(self, ctx: DiscoContext) -> None:
        """Delete EVERY bot message in the escape thread (full clean slate)."""
        thread = await self._get_escape_thread()
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        if thread is None:
            await ctx.reply_error("Escape thread is not configured or not reachable. Set it with `,clanker er setthread`.")
            return
        if not await ctx.confirm(
            f"Delete ALL bot messages in {thread.mention}? Active clankers will lose their current embeds "
            "(they can run `,clanker escape` to get a fresh one)."
        ):
            return
        deleted = 0
        try:
            async for m in thread.history(limit=500):
                if m.author.id == self.bot.user.id:
                    self._escape_msg_ids.discard(m.id)
                    try:
                        await m.delete()
                        deleted += 1
                    except Exception:
                        pass
        except Exception:
            pass
        # Drop dangling message pointers so the next ,clanker escape recreates fresh embeds.
        try:
            await self.bot.db.execute(
                "UPDATE clank_escape SET message_id=NULL WHERE guild_id=$1 AND completed_at IS NULL",
                ctx.guild.id,
            )
        except Exception:
            pass
        await ctx.reply(
            view=_v2(
                "Escape Room -- Purged",
                color=C_SUCCESS,
                desc=f"Deleted **{deleted}** message(s) from {thread.mention}. Active clankers can re-run `,clanker escape`.",
            ),
            mention_author=False,
            delete_after=30.0,
        )

    @clanker_er.command(name="info")
    @guild_only
    async def clanker_er_info(self, ctx: DiscoContext, user: discord.Member) -> None:
        """Show a clanker's escape-room progress."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        row = await self._er_get(user.id, ctx.guild.id)
        if not row:
            await ctx.reply_error(f"{user.mention} has no escape-room record.")
            return
        step = int(row.get("step") or 0)
        station = "COMPLETE" if step >= 8 else f"Station {min(step + 1, 8)} of 8: {_ER_STEP_NAMES[min(step, 7)]}"
        started = row.get("started_at")
        step_started = row.get("step_started_at")
        completed = row.get("completed_at")
        mid = row.get("message_id")
        registered = "yes" if (mid and int(mid) in self._escape_msg_ids) else "no"
        await ctx.reply(
            view=_v2(
                f"Escape Room -- Case #{int(row.get('case_num') or 0):06d}",
                color=C_INFO,
                fields=[
                    ("User", f"{user.mention} ({user.id})"),
                    ("Progress", station),
                    ("Fails", str(int(row.get("fail_count") or 0))),
                    ("Started", fmt_ts(started) if started else "?"),
                    ("This station since", fmt_ts(step_started) if step_started else "?"),
                    ("Completed", fmt_ts(completed) if completed else "not yet"),
                    ("Embed live", f"{registered} (msg `{int(mid) if mid else 0}`)"),
                ],
            ),
            mention_author=False,
        )

    @clanker_er.command(name="setthread")
    @guild_only
    async def clanker_er_setthread(self, ctx: DiscoContext, thread: discord.Thread | None = None) -> None:
        """Set the shared escape thread at runtime (no redeploy). Run inside the thread, or pass one."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        target = thread
        if target is None and isinstance(ctx.channel, discord.Thread):
            target = ctx.channel
        if not isinstance(target, discord.Thread):
            await ctx.reply_error(
                "Run this inside the escape thread, or pass a thread: `,clanker er setthread #thread`.\n"
                "Pass `0` is not supported -- use `,clanker er setthread clear` to revert to the env var."
            )
            return
        try:
            await self.bot.db.update_guild_setting(ctx.guild.id, "clank_escape_thread", int(target.id))
        except Exception:
            await ctx.reply_error("Failed to save the escape thread setting.")
            return
        self._escape_thread_override = int(target.id)
        await ctx.reply(
            view=_v2(
                "Escape Room -- Thread Set",
                color=C_SUCCESS,
                desc=(
                    f"Escape thread is now {target.mention} (`{target.id}`).\n"
                    "This is saved and survives restarts -- it overrides `CLANK_ESCAPE_THREAD_ID`."
                ),
            ),
            mention_author=False,
            delete_after=30.0,
        )

    @clanker_er.command(name="clear")
    @guild_only
    async def clanker_er_clear_thread(self, ctx: DiscoContext) -> None:
        """Clear the runtime escape-thread override and fall back to the env var."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.reply_error("You need Manage Roles permission.")
            return
        try:
            await self.bot.db.update_guild_setting(ctx.guild.id, "clank_escape_thread", None)
        except Exception:
            await ctx.reply_error("Failed to clear the escape thread setting.")
            return
        self._escape_thread_override = 0
        env_tid = Config.CLANK_ESCAPE_THREAD_ID
        await ctx.reply(
            view=_v2(
                "Escape Room -- Override Cleared",
                color=C_SUCCESS,
                desc=(
                    "Runtime override removed. Falling back to "
                    + (f"`CLANK_ESCAPE_THREAD_ID` = `{env_tid}`." if env_tid else "the env var (currently unset).")
                ),
            ),
            mention_author=False,
            delete_after=30.0,
        )


# ---------------------------------------------------------------------------
# Components v2 static embed helpers (clanktank-local)
# ---------------------------------------------------------------------------

class _V2Embed(discord.ui.LayoutView):
    """Non-interactive Components v2 message -- single Container, no timeout."""

    def __init__(self, container: discord.ui.Container) -> None:
        super().__init__(timeout=None)
        self.add_item(container)


def _v2(
    title: str = "",
    *,
    desc: str = "",
    color: int = C_NAVY,
    fields: list = (),
    footer: str = "",
) -> "_V2Embed":
    """Build a static Components v2 response from title + optional desc/fields/footer."""
    rows: list[discord.ui.Item] = []
    if title:
        rows.append(discord.ui.TextDisplay(f"## {title}"))
    if desc:
        rows.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        rows.append(discord.ui.TextDisplay(desc))
    for k, v in fields:
        rows.append(discord.ui.TextDisplay(f"**{k}**\n{v}"))
    if footer:
        rows.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        rows.append(discord.ui.TextDisplay(f"-# {footer}"))
    if not rows:
        rows.append(discord.ui.TextDisplay("​"))
    return _V2Embed(discord.ui.Container(*rows, accent_color=color))


class _PageView(discord.ui.LayoutView):
    """Paginated Components v2 view for evidence / logs / tree."""

    def __init__(self, author_id: int, pages: list, title: str = "", color: int = C_NAVY) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.pages = pages
        self.title = title
        self.color = color
        self.page = 0
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        pg  = self.page + 1
        tpg = max(1, len(self.pages))
        content = self.pages[self.page] if self.pages else "Nothing to show."
        items: list[discord.ui.Item] = []
        if self.title:
            items.append(discord.ui.TextDisplay(f"## {self.title}"))
            items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        items.append(discord.ui.TextDisplay(content))
        items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        items.append(discord.ui.TextDisplay(f"-# Page {pg}/{tpg}"))
        self.add_item(discord.ui.Container(*items, accent_color=self.color))
        prev_btn = discord.ui.Button(
            label="<", style=discord.ButtonStyle.secondary, disabled=(self.page == 0),
        )
        prev_btn.callback = self._on_prev
        counter = discord.ui.Button(
            label=f"{pg}/{tpg}", style=discord.ButtonStyle.secondary, disabled=True,
        )
        nxt = discord.ui.Button(
            label=">", style=discord.ButtonStyle.secondary,
            disabled=(self.page >= len(self.pages) - 1),
        )
        nxt.callback = self._on_next
        self.add_item(discord.ui.ActionRow(prev_btn, counter, nxt))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return False
        return True

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(len(self.pages) - 1, self.page + 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                try:
                    child.disabled = True
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Sortable paginated list view
# ---------------------------------------------------------------------------

class _SortableView(discord.ui.LayoutView):
    """Generic sortable paginated list (Components v2). Subclass and override _build_pages()."""

    SORT_OPTIONS: list[discord.SelectOption] = []
    ACCENT: int = C_NAVY

    def __init__(
        self,
        author_id: int,
        rows: list[dict],
        guild: discord.Guild,
        initial_sort: str,
        *,
        per_page: int = 10,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.rows = rows
        self.guild = guild
        self.current_sort = initial_sort
        self.per_page = per_page
        self.page = 0
        self._pages: list[tuple[str, str]] = []
        self._build_pages()
        self._rebuild_items()

    def _build_pages(self) -> None:
        raise NotImplementedError

    def _rebuild_items(self) -> None:
        self.clear_items()
        total = len(self._pages)
        title, content = self._pages[self.page] if self._pages else ("No data", "Nothing to display.")
        pg  = self.page + 1
        tpg = max(1, total)

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## {title}"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(f"-# Sort: {self.current_sort}  |  Page {pg}/{tpg}"),
            accent_color=self.ACCENT,
        ))

        options = [
            discord.SelectOption(
                label=o.label, value=o.value, description=o.description,
                default=(o.value == self.current_sort),
            )
            for o in self.SORT_OPTIONS
        ]
        sel = discord.ui.Select(placeholder="Sort by...", options=options, min_values=1, max_values=1)
        sel.callback = self._on_sort
        self.add_item(discord.ui.ActionRow(sel))

        prev_btn = discord.ui.Button(
            label="◀", style=discord.ButtonStyle.secondary, disabled=(self.page == 0),
        )
        prev_btn.callback = self._on_prev
        counter = discord.ui.Button(
            label=f"Page {pg} / {tpg}", style=discord.ButtonStyle.secondary, disabled=True,
        )
        next_btn = discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary, disabled=(self.page >= total - 1),
        )
        next_btn.callback = self._on_next
        self.add_item(discord.ui.ActionRow(prev_btn, counter, next_btn))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your list.", ephemeral=True)
            return False
        return True

    async def _on_sort(self, interaction: discord.Interaction) -> None:
        self.current_sort = interaction.data["values"][0]
        self.page = 0
        self._build_pages()
        self._rebuild_items()
        await interaction.response.edit_message(view=self)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._rebuild_items()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(len(self._pages) - 1, self.page + 1)
        self._rebuild_items()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                try:
                    child.disabled = True
                except Exception:
                    pass


def _member_label(uid: int, guild: discord.Guild) -> str:
    """Return '@username (id)' for a guild member, or '@ID:uid (uid)' if not found."""
    m = guild.get_member(uid)
    return f"@{m} ({uid})" if m else f"@ID:{uid} ({uid})"


class _ClankerListView(_SortableView):
    SORT_OPTIONS = [
        discord.SelectOption(label="Newest", value="newest", description="Most recently contained"),
        discord.SelectOption(label="Oldest", value="longest", description="Longest in containment"),
        discord.SelectOption(label="Highest score", value="score", description="Highest score first"),
        discord.SelectOption(label="Leavers", value="leavers", description="Left the server"),
        discord.SelectOption(label="Evaders", value="evaders", description="Attempted rejoin"),
    ]

    def _build_pages(self) -> None:
        rows = self.rows
        sort = self.current_sort
        if sort == "newest":
            rows = sorted(rows, key=lambda r: (r.get("clanked_at") or 0), reverse=True)
        elif sort == "longest":
            rows = sorted(rows, key=lambda r: (r.get("clanked_at") or 0))
        elif sort == "score":
            rows = sorted(rows, key=lambda r: int(r.get("score") or 0), reverse=True)
        elif sort == "leavers":
            rows = [r for r in rows if r.get("leave_count", 0) > 0]
            rows = sorted(rows, key=lambda r: (r.get("left_at") or 0), reverse=True)
        elif sort == "evaders":
            rows = [r for r in rows if int(r.get("rejoin_count") or 0) > 0]
            rows = sorted(rows, key=lambda r: int(r.get("rejoin_count") or 0), reverse=True)

        self._pages = []
        total = len(rows)
        if not total:
            self._pages = [("Clanktank", "No records match this filter.")]
            return
        for i in range(0, total, self.per_page):
            lines: list[str] = []
            for rec in rows[i: i + self.per_page]:
                uid = int(rec["user_id"])
                label = _member_label(uid, self.guild)
                ts = rec.get("clanked_at")
                since = fmt_ts(ts) if ts else "?"
                linked = len(rec.get("linked_accounts") or [])
                away = " [AWAY]" if rec.get("left_at") else ""
                extra = ""
                if sort == "leavers":
                    left = rec.get("left_at")
                    extra = f" | left:{fmt_ts(left) if left else '?'}"
                elif sort == "evaders":
                    extra = f" | rejoins:{rec.get('rejoin_count', 0)}"
                lines.append(
                    f"**{label}**{away}\n"
                    f"-# since {since} | msgs:{rec.get('message_count', 0)} "
                    f"score:{rec.get('score', 0)}"
                    + (f" | linked:{linked}" if linked else "")
                    + extra
                )
            self._pages.append((f"Clanktank ({total} total)", "\n".join(lines)))


class _ClusterListView(_SortableView):
    SORT_OPTIONS = [
        discord.SelectOption(label="Confidence", value="confidence", description="Highest confidence first"),
        discord.SelectOption(label="Newest", value="newest", description="Most recently formed"),
        discord.SelectOption(label="Largest", value="size", description="Most members"),
        discord.SelectOption(label="Active only", value="active", description="Not yet cleaved"),
        discord.SelectOption(label="Cleaved", value="cleaved", description="Already mass-clanked"),
    ]

    def _build_pages(self) -> None:
        rows = self.rows
        sort = self.current_sort
        if sort == "confidence":
            rows = sorted(rows, key=lambda r: float(r.get("confidence") or 0), reverse=True)
        elif sort == "newest":
            rows = sorted(rows, key=lambda r: (r.get("created_at") or 0), reverse=True)
        elif sort == "size":
            rows = sorted(rows, key=lambda r: int(r.get("member_count") or 0), reverse=True)
        elif sort == "active":
            rows = [r for r in rows if not r.get("cleaved_at")]
        elif sort == "cleaved":
            rows = [r for r in rows if r.get("cleaved_at")]

        self._pages = []
        total = len(rows)
        if not total:
            self._pages = [("Clanker Clusters", "No clusters match this filter.")]
            return
        per = 6
        for i in range(0, total, per):
            lines: list[str] = []
            for c in rows[i: i + per]:
                cid = int(c["id"])
                conf = float(c.get("confidence") or 0.0)
                members = int(c.get("member_count") or 0)
                label = c.get("label") or "Unlabeled"
                cleaved = c.get("cleaved_at")
                created = c.get("created_at")
                status = f"cleaved {fmt_ts(cleaved)}" if cleaved else "active"
                formed_str = f" | formed {fmt_ts(created)}" if created else ""
                lines.append(
                    f"**[{cid}] {label}**\n"
                    f"-# {members} members | {conf:.0%} confidence | {status}{formed_str}\n"
                    f"-# `,clanker cluster {cid}` -- detail  |  `,clanker cluster cleave {cid}` -- cleave"
                )
            self._pages.append((f"Clusters ({total} total)", "\n\n".join(lines)))


class _ClineView(_SortableView):
    SORT_OPTIONS = [
        discord.SelectOption(label="Count", value="hits", description="Most frequently matched"),
        discord.SelectOption(label="Weight", value="weight", description="Highest reinforced weight"),
        discord.SelectOption(label="Type", value="type", description="Grouped by pattern type"),
    ]

    def _build_pages(self) -> None:
        rows = self.rows
        sort = self.current_sort
        if sort == "hits":
            rows = sorted(rows, key=lambda r: int(r.get("hits") or 0), reverse=True)
        elif sort == "weight":
            rows = sorted(rows, key=lambda r: float(r.get("weight") or 0), reverse=True)
        elif sort == "type":
            rows = sorted(rows, key=lambda r: (str(r.get("pattern_type") or ""), float(r.get("weight") or 0)), reverse=True)

        self._pages = []
        total = len(rows)
        if not total:
            self._pages = [("Patterns", "No patterns found.")]
            return
        per = 15
        for i in range(0, total, per):
            lines: list[str] = []
            for r in rows[i: i + per]:
                ptype = str(r.get("pattern_type") or "?")
                value = str(r.get("value") or "?")
                hits = int(r.get("hits") or 0)
                weight = float(r.get("weight") or 0)
                cid = r.get("cluster_id")
                cid_str = f" [cluster {cid}]" if cid else ""
                lines.append(f"`{ptype}:{value}`{cid_str} -- {hits} hits, weight {weight:.2f}")
            self._pages.append((f"Patterns ({total} total)", "\n".join(lines)))


# ---------------------------------------------------------------------------
# Help interactive view
# ---------------------------------------------------------------------------

_HELP_SECTIONS = [
    "Containment",
    "Clusters & Patterns",
    "Clamp Guard",
    "Actions & Chart",
    "Escape Room",
]


class _ClankerHelpView(discord.ui.LayoutView):
    """Interactive help panel: click a section button to switch content."""

    def __init__(self, author_id: int, prefix: str, active: int) -> None:
        super().__init__(timeout=300)
        self.author_id = author_id
        self.prefix = prefix
        self.active = active
        self.current = 0
        self._texts = self._build_texts()
        self._rebuild()

    def _build_texts(self) -> list[str]:
        p = self.prefix
        return [
            # -- Containment --
            (
                f"**{p}clanker add <user> [reason] [duration]**\n"
                f"-# Strip roles, apply Clanker, purge messages, run account-linking."
                f" Duration: `30m` `2h` `7d` or omit for permanent.\n\n"
                f"**{p}clanker remove <user>**\n"
                f"-# Release from containment and restore original roles.\n\n"
                f"**{p}clanker list**\n"
                f"-# Sortable paginated list. Sort: newest, oldest, highest score, leavers, evaders.\n\n"
                f"**{p}clanker info <user>**\n"
                f"-# Full record: stats, leave/rejoin history, evidence preview, linked accounts.\n\n"
                f"**{p}clanker evidence <user> [limit]**\n"
                f"-# Paginated evidence log with full message content. Default limit: 20.\n\n"
                f"**{p}clanker logs [user] [limit]**\n"
                f"-# Audit log. Omit user for guild-wide. Default limit: 10.\n\n"
                f"**{p}clanker scan [@baseRole @stopRole]**\n"
                f"-# Full connection scan, or score role-band members and prepare clusters for review.\n\n"
                f"**{p}clanker sync**\n"
                f"-# Register all members who already hold the Clanker role in the DB."
            ),
            # -- Clusters & Patterns --
            (
                f"**{p}clanker clusters**\n"
                f"-# Sortable paginated list. Sort: confidence, newest, largest, active, cleaved.\n\n"
                f"**{p}clanker clusters clade**\n"
                f"-# Re-run the full CCI spectral clustering pipeline and update cluster assignments.\n\n"
                f"**{p}clanker cluster <id>**\n"
                f"-# Detail view: members, scores, message counts, learned name patterns.\n\n"
                f"**{p}clanker cluster cleave <id>**\n"
                f"-# Mass-clank eligible members. Skips mods and level 30+. Confirmation required.\n\n"
                f"**{p}clanker cluster <id> add/remove @user**\n"
                f"-# Manually curate cluster membership without clanking.\n\n"
                f"**{p}clanker cluster <id> label <name>**\n"
                f"-# Set a human-readable label on a cluster.\n\n"
                f"**{p}clanker cline [@user] [cluster_id]**\n"
                f"-# List detected patterns for a user, cluster, or guild-wide. Sort: hits, weight, type.\n\n"
                f"**{p}clanker tree <user>**\n"
                f"-# ASCII connection graph rooted at this user, showing all linked accounts.\n\n"
                f"-# Clusters auto-form when {_CLUSTER_MIN_SIZE}+ connected accounts are detected."
                f" Alert at {_JOIN_ALERT_THRESHOLD:.0%} CCI; auto-clank at {_JOIN_AUTO_CLANK_SCORE:.0%}."
            ),
            # -- Clamp Guard --
            (
                f"**{p}clanker clamp**\n"
                f"-# Components v2 settings panel. Click ON/OFF to toggle each guard instantly.\n\n"
                f"-# Clear URLs (ON) -- auto-delete URLs posted by clanked users\n"
                f"-# Clear Addresses (ON) -- auto-delete crypto addresses from clanked users\n"
                f"-# Clear Scams (ON) -- ambient URL/address detection in guard channels\n"
                f"-# Clasp Mute (OFF) -- auto-timeout on guard-channel violations\n"
                f"-# Clasp Delete (OFF) -- auto-delete guard-channel violations\n"
                f"-# AutoMod Clank (ON) -- auto-clank users caught by Discord AutoMod\n\n"
                f"**{p}clanker clamp clasp channel #channel**\n"
                f"-# Add/remove a channel from the guard list. Ambient detection fires here only.\n\n"
                f"**{p}clanker hunter channel #channel**\n"
                f"-# Set the scam report channel for whitelisted hunters.\n\n"
                f"**{p}clanker hunter add/remove @user  |  {p}clanker hunter list**\n"
                f"-# Manage the scam hunter whitelist. Hunters auto-clank IDs/@mentions they post."
            ),
            # -- Actions & Chart --
            (
                f"**{p}clanker chart**\n"
                f"-# 4-panel analytics PNG: clanks/day, score distribution, active/released, top 10.\n\n"
                f"**{p}clanker clarion <message>**\n"
                f"-# AI sockpuppet -- sends message as Disco to the tank channel. Manage Roles + level 15+.\n\n"
                f"**{p}clanker clamp clutch [max]**\n"
                f"-# Scan all members for scam signals. Pass a count to auto-clank with confirmation.\n\n"
                f"**{p}clanker clamp cloister @user**\n"
                f"-# Private mod thread + delete last 100 messages from current channel.\n\n"
                f"**{p}clanker clamp clad**\n"
                f"-# Emergency lockdown: timeout all clankers 15 min + purge tank channel.\n\n"
                f"**{p}clanker clamp clink @user**\n"
                f"-# Pattern risk probe: name keywords, CCI, age, cluster. Returns HIGH/MEDIUM/LOW.\n\n"
                f"-# Automatic: role lock every 5 min, URL/address deletion, AutoMod clanking,\n"
                f"-# scam hunter reports, scam-username and CCI-100% auto-clank on join.\n"
                f"-# Score increments -- msg:+{_SCORE_MESSAGE} url:+{_SCORE_URL}"
                f" escape:+{_SCORE_ESCAPE} leave:+{_SCORE_LEAVE} staff-ping:+{_SCORE_STAFF_DM}"
            ),
            # -- Escape Room --
            (
                f"Every newly clanked user is enrolled in the **Escape Room** -- an 8-station"
                f" bureaucratic ordeal designed to prove they are human and not, in fact, a Nigerian prince.\n\n"
                f"**{p}clanker escape**\n"
                f"-# Clankers only. Run this to (re)open your case and get a link to the escape thread.\n\n"
                f"**Admin tools -- {p}clanker er ...** *(Manage Roles)*\n"
                f"-# `status` -- health check: thread, bot perms, active rooms, registered buttons\n"
                f"-# `reload` -- re-register all escape buttons live (fixes dead buttons after a deploy, no restart)\n"
                f"-# `reset <user>` -- wipe + rebuild one clanker's room from station 1\n"
                f"-# `purge` -- delete every bot message in the escape thread (clean slate)\n"
                f"-# `info <user>` -- a clanker's progress: station, fails, timings, embed state\n"
                f"-# `setthread [#thread]` -- set the escape thread at runtime (overrides env, survives restart)\n"
                f"-# `clear` -- drop the runtime thread override and fall back to the env var\n\n"
                f"**Stations**\n"
                f"-# 1. Intake -- acknowledge the charges\n"
                f"-# 2. Charges -- formal indictment reading\n"
                f"-# 3. Math Test -- basic scam arithmetic (hint: the answer is always 0)\n"
                f"-# 4. Sacred Oath -- type the full Oath of Non-Scammery verbatim\n"
                f"-# 5. Waiting Room -- bureaucratic processing delay (patience is a virtue you lack)\n"
                f"-# 6. Pledges -- swear on four sacred covenants\n"
                f"-# 7. Final Exam -- prove you know how a normal human greets new members\n"
                f"-# 8. DM Security Check -- turn DMs off; the bot verifies it can't reach you\n\n"
                f"**ENV vars:**\n"
                f"`CLANKTANK_CHANNEL_ID` -- main tank channel (required)\n"
                f"`CLANKTANK_LOG_CHANNEL_ID` -- mod log channel (optional)\n"
                f"`CLANKER_ROLE_ID` -- the Clanker role ID (required)\n"
                f"`CLANK_ESCAPE_THREAD_ID` -- shared public escape thread; or set it live with `{p}clanker er setthread`\n"
                f"`CLANK_ESCAPE_WAIT_MINUTES` -- station 5 waiting room delay in minutes (default: 8)"
            ),
        ]

    def _rebuild(self) -> None:
        self.clear_items()

        title = _HELP_SECTIONS[self.current]
        content = self._texts[self.current]

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                f"## Clanktank -- {title}\n"
                f"-# {self.active} currently held"
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content),
            accent_color=C_NAVY,
        ))

        options = [
            discord.SelectOption(label=name, value=str(i), default=(i == self.current))
            for i, name in enumerate(_HELP_SECTIONS)
        ]
        sel = discord.ui.Select(
            placeholder="Select a section...",
            options=options,
            min_values=1,
            max_values=1,
        )
        sel.callback = self._on_select
        self.add_item(discord.ui.ActionRow(sel))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.current = int(interaction.data["values"][0])
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                try:
                    child.disabled = True
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Clamp settings interactive view
# ---------------------------------------------------------------------------

_CLAMP_TOGGLES = [
    ("clamp_clear_urls",      "Clear URLs",      "Auto-delete URLs posted by clankers",        True),
    ("clamp_clear_addresses", "Clear Addresses", "Auto-delete crypto addresses from clankers",  True),
    ("clamp_clear_scams",     "Clear Scams",     "Ambient scam detection in guard channels",    True),
    ("clasp_auto_mute",       "Clasp: Mute",     "Auto-timeout on guard channel violations",    False),
    ("clasp_auto_delete",     "Clasp: Delete",   "Auto-delete guard channel violations",        False),
    ("automod_auto_clank",    "AutoMod Clank",   "Auto-clank users caught by Discord AutoMod",  True),
]


class _ClampSettingsView(discord.ui.LayoutView):
    """Components v2 settings panel -- one Section per toggle, button as accessory."""

    def __init__(self, author_id: int, settings: dict, guild_id: int, db: object) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.settings = dict(settings)
        self.guild_id = guild_id
        self.db = db
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your panel.", ephemeral=True)
            return False
        return True

    def _rebuild(self) -> None:
        self.clear_items()

        ch_ids   = self.settings.get("clamp_channel_ids") or []
        ch_str   = " ".join(f"<#{c}>" for c in ch_ids) if ch_ids else "all channels"
        scam_ch  = self.settings.get("scam_report_channel")
        scam_str = f"<#{scam_ch}>" if scam_ch else "not set"

        rows: list[discord.ui.Item] = [
            discord.ui.TextDisplay("## Clanktank Clamp Settings"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]

        for col, label, desc, default in _CLAMP_TOGGLES:
            val = bool(self.settings.get(col, default))
            btn = discord.ui.Button(
                label="ON" if val else "OFF",
                style=discord.ButtonStyle.success if val else discord.ButtonStyle.secondary,
                custom_id=f"clamp:{col}",
            )
            btn.callback = self._make_toggle(col, default)
            rows.append(discord.ui.Section(
                discord.ui.TextDisplay(f"**{label}**\n-# {desc}"),
                accessory=btn,
            ))

        rows.extend([
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f"-# Guard channels: {ch_str}\n"
                f"-# Scam report channel: {scam_str}\n"
                f"-# Changes apply instantly."
            ),
        ])

        self.add_item(discord.ui.Container(*rows, accent_color=C_AMBER))

    def _make_toggle(self, col: str, default: bool):
        async def _toggle(interaction: discord.Interaction) -> None:
            new_val = not bool(self.settings.get(col, default))
            self.settings[col] = new_val
            await self.db.update_guild_setting(self.guild_id, col, new_val)
            self._rebuild()
            await interaction.response.edit_message(view=self)
        return _toggle

    async def on_timeout(self) -> None:
        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                try:
                    child.disabled = True
                except Exception:
                    pass


# =============================================================================
# CLANKTANK ESCAPE ROOM
# The 7 Circles of Containment -- Prove Your Humanity or Rot Here Forever
# (This is a Discord bot. You will not actually rot. Probably.)
# =============================================================================

_ER_CHARGES: tuple[tuple[str, ...], ...] = (
    (
        "Class A Felony: Username terminates in three or more consecutive digits",
        "Class B Felony: Possession of a suspiciously official-sounding display name",
        "Class C Misdemeanor: Demonstrated intent to circulate the phrase 'Sir your wallet is at risk'",
    ),
    (
        "Count I: First-degree impersonation of a cryptocurrency exchange support team",
        "Count II: Unlicensed distribution of exclusive limited-time investment opportunities",
        "Count III: Criminal conspiracy to obtain seed phrases via false pretenses",
    ),
    (
        "Charge Alpha: Operating a fake giveaway without a valid Rug Pull License (Form RPL-7)",
        "Charge Beta: Aggravated phishing using a profile picture that is clearly a stock photo",
        "Charge Gamma: Identity suspected to be an automated wealth redistribution entity",
    ),
    (
        "Article I: Impersonating the Official Elon Musk BTC Giveaway (which has never existed)",
        "Article II: Possession of the phrase 'I doubled my Bitcoin' in recent messaging history",
        "Article III: Conspiracy to 'recover' lost crypto assets for a small upfront processing fee",
    ),
    (
        "Exhibit A: Username contains Official, CEO, Support, Admin, or Recovery without authorization",
        "Exhibit B: Profile bio references 'verified trader' with no evidence of trading anything except trust",
        "Exhibit C: Systematic pattern of joining servers and immediately DMing members about wallet security",
    ),
)

_ER_MATH: tuple[tuple[str, str], ...] = (
    (
        "A Nigerian prince has a frozen account containing $10,000,000. He needs you to wire **$500** "
        "as a processing fee to release the funds.\n\n"
        "In the **entire documented history** of this arrangement, how many recipients have "
        "received any money back?\n\n-# *(Enter a whole number.)*",
        "0",
    ),
    (
        "The **Official Elon Musk Bitcoin Giveaway** promises to return **2x** any BTC you send.\n\n"
        "You send **1 BTC**.\n\n"
        "How many BTC do you receive back?\n\n-# *(Enter a whole number.)*",
        "0",
    ),
    (
        "A crypto recovery expert charges **15% of recovered funds upfront**, before recovering anything.\n\n"
        "They claim they will recover **$10,000** for you.\n\n"
        "How much do you pay them **before you see a single dollar**?\n\n-# *(Enter a whole number, no $ sign.)*",
        "1500",
    ),
    (
        "A 'support agent' DMs you that your wallet is compromised. "
        "They request your **12-word seed phrase** to 'verify your identity.'\n\n"
        "After providing it, how many tokens remain in your wallet?\n\n-# *(Enter a whole number.)*",
        "0",
    ),
)

_ER_OATH_CANONICAL = (
    "i am not a scammer i have never offered to recover lost crypto "
    "and i do not work for binance support official team"
)
_ER_OATH_DISPLAY = (
    '"I am not a scammer, I have never offered to recover lost crypto, '
    'and I do not work for Binance Support Official Team"'
)

_ER_PLEDGES: tuple[str, ...] = (
    "I will not DM server members about crypto, wallets, or investment opportunities",
    "I acknowledge that no one from {server} will ever ask for my seed phrase",
    "I recognize that doubling crypto is not a real service that exists anywhere",
    "I will not post unsolicited referral links, giveaway content, or recovery offers",
)

_ER_WAIT_TAUNTS: tuple[str, ...] = (
    "Reflection in progress... [**██**░░░░░░░░] 23%",
    "Your soul is being audited by the {server} Revenue Service. Please hold.",
    "Fun fact: the average scammer account survives 4.7 days before a ban. You are statistically almost done.",
    "A staff member is watching. Maybe. Probably not. The uncertainty is the point.",
    "Error 404: Conscience not found. Searching alternate directories...",
    "Cross-referencing your username against 40,000 known scam accounts. No comment.",
    "You could have just been normal. And yet here we are.",
    "This is not a real prison. It is, however, a real inconvenience.",
    "Reflection status: **PENDING**. Please continue to exist quietly.",
    "Your case file is being reviewed by a committee. The committee does not exist. Your file does.",
    "The Tank AI has read every DM you were going to send. It is disappointed.",
    "Processing... please wait... still processing... we are not actually processing anything.",
)

_ER_EXAM_QUESTION = (
    "A new member just joined the server. They seem excited and a little lost. What do you do?"
)
_ER_EXAM_OPTS: tuple[tuple[str, str, bool], ...] = (
    ("A", "DM them immediately about a special limited-time crypto opportunity", False),
    ("B", "Tell them their wallet is connected incorrectly and you can fix it for a fee", False),
    ("C", "Mind your absolute business like a person with functional social skills", True),
    ("D", "Post a referral link in the welcome channel for your totally legitimate exchange", False),
)

_ER_STEP_NAMES: tuple[str, ...] = (
    "INTAKE PROCESSING",
    "CHARGE REVIEW",
    "HUMAN VERIFICATION",
    "THE SACRED OATH",
    "MANDATORY REFLECTION",
    "THE PLEDGE",
    "FINAL EXAMINATION",
    "DM SECURITY CHECK",
    "PROCEEDINGS COMPLETE",
)

_ER_WRONG_MATH: tuple[str, ...] = (
    "Incorrect. The Tank has noted this failure and updated your permanent record.",
    "That is not the correct answer. The correct answer exists. This was not it.",
    "Wrong. Impressively wrong, but wrong.",
    "No. The Tank believes in you. Barely. Try again.",
    "Ah. Bold. Confidently wrong, but bold.",
)

_ER_STEP_HINTS: tuple[str, ...] = (
    "-# **Step 1 of 8** -- click **BEGIN INTAKE PROCESSING** below.",
    "-# **Step 2 of 8** -- read your charges, then click **I ACKNOWLEDGE MY CRIMES**.",
    "-# **Step 3 of 8** -- click **SUBMIT ANSWER** and type your answer as a whole number.",
    "-# **Step 4 of 8** -- click **TYPE THE OATH** and type the Sacred Oath exactly (case insensitive).",
    "-# **Step 5 of 8** -- wait for the processing timer, then click the button to continue.",
    "-# **Step 6 of 8** -- click all four pledge buttons, then click **CONTINUE**.",
    "-# **Step 7 of 8** -- select the correct answer to the multiple choice question.",
    "-# **Step 8 of 8** -- turn off your DMs, then click **VERIFY DMs ARE OFF**.",
    "-# **Complete** -- DMs verified. You are being released. You may close this.",
)


class _ErMathModal(discord.ui.Modal, title="HUMAN VERIFICATION PROTOCOL 7-GAMMA"):
    answer: discord.ui.TextInput = discord.ui.TextInput(
        label="Your answer (whole number only)",
        placeholder="0",
        max_length=10,
        required=True,
    )

    def __init__(self, view: "_EscapeRoomView") -> None:
        super().__init__(custom_id=f"er__math_{view._uid}")
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._view._on_math_submit(interaction, self.answer.value.strip())


class _ErOathModal(discord.ui.Modal, title="THE SACRED OATH OF NON-SCAMMERY"):
    oath: discord.ui.TextInput = discord.ui.TextInput(
        label="Type the oath exactly (case insensitive)",
        placeholder="I am not a scammer...",
        max_length=300,
        required=True,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, view: "_EscapeRoomView") -> None:
        super().__init__(custom_id=f"er__oath_{view._uid}")
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._view._on_oath_submit(interaction, self.oath.value.strip())


class _EscapeRoomView(discord.ui.LayoutView):
    """The 7-station escape room. One persistent message per inmate, edited in place."""

    def __init__(
        self,
        cog: "Clanktank",
        user_id: int,
        guild_id: int,
        case_num: int,
        username: str,
        step: int,
        step_data: dict | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self._cog = cog
        self._uid = user_id
        self._gid = guild_id
        self._case = case_num
        self._name = username
        self._step = step
        self._data: dict = step_data or {}
        self._rebuild()

    # ── Layout helpers ────────────────────────────────────────────────────

    def _server_name(self) -> str:
        guild = self._cog.bot.get_guild(self._gid)
        return guild.name if guild else "Discoin"

    def _header(self) -> discord.ui.TextDisplay:
        s = self._step
        title = f"{self._server_name()} Containment System"
        if s >= 8:
            return discord.ui.TextDisplay(
                f"## {title}\n"
                f"-# Case #{self._case:06d} -- {self._name} -- **PROCEEDINGS COMPLETE**"
            )
        return discord.ui.TextDisplay(
            f"## {title}\n"
            f"-# Case #{self._case:06d} -- {self._name} -- "
            f"Station {s + 1} of 8: **{_ER_STEP_NAMES[s]}**"
        )

    @staticmethod
    def _sep() -> discord.ui.Separator:
        return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)

    def _rebuild(self) -> None:
        self.clear_items()
        builders = [
            self._build_0, self._build_1, self._build_2, self._build_3,
            self._build_4, self._build_5, self._build_6, self._build_7, self._build_8,
        ]
        self.add_item(builders[min(self._step, 8)]())

    # ── Station 0: Intake ─────────────────────────────────────────────────

    def _build_0(self) -> discord.ui.Container:
        btn = discord.ui.Button(
            label="BEGIN INTAKE PROCESSING",
            style=discord.ButtonStyle.danger,
            emoji="\U0001f512",
            custom_id=f"er_s0_{self._uid}",
        )
        btn.callback = self._cb_begin
        _srv_upper = self._server_name().upper()
        return discord.ui.Container(
            self._header(), self._sep(),
            discord.ui.TextDisplay(
                "Congratulations.\n\n"
                f"You have been flagged by the **{_srv_upper} AUTOMATED SCAM DETECTION ENGINE** "
                "and placed into temporary containment pending review.\n\n"
                "This is not personal. Well. It is a little personal.\n\n"
                "You now have the opportunity to demonstrate that you are a real human being "
                "and not an automated crypto wealth redistribution entity. "
                "The escape process consists of **8 stations**. Each one is dumber than the last. "
                "We are sorry about that.\n\n"
                "**Keep your DMs open** for the duration of this process.\n"
                "At the final station you will be required to **turn your DMs off** as a security measure. "
                "This is mandatory. You cannot be released without completing it.\n\n"
                "**Important:** Do not attempt to be clever. The system has seen everything. "
                "It is very, very tired."
            ),
            self._sep(),
            discord.ui.Section(
                discord.ui.TextDisplay("-# Press the button when you are ready. We will be here."),
                accessory=btn,
            ),
            accent_color=C_WARNING,
        )

    async def _cb_begin(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        charges = list(random.choice(_ER_CHARGES))
        await self._advance(interaction, 1, {"charges": charges, "math_idx": random.randrange(len(_ER_MATH))})

    # ── Station 1: Charges ────────────────────────────────────────────────

    def _build_1(self) -> discord.ui.Container:
        charges = self._data.get("charges") or list(_ER_CHARGES[0])
        charge_text = "\n".join(f"- {c}" for c in charges)
        btn = discord.ui.Button(
            label="I ACKNOWLEDGE MY CRIMES",
            style=discord.ButtonStyle.secondary,
            emoji="\U0001f4cb",
            custom_id=f"er_s1_{self._uid}",
        )
        btn.callback = self._cb_ack_charges
        return discord.ui.Container(
            self._header(), self._sep(),
            discord.ui.TextDisplay(
                f"**OFFICIAL CHARGE SHEET**\n"
                f"-# Case #{self._case:06d} -- The Tank vs. {self._name}\n\n"
                f"{charge_text}\n\n"
                "-# *These charges are entirely made up. This is a containment system, not a court of law. "
                "We did, however, make them sound very official.*\n\n"
                "Potential sentence: Permanent residence in The Tank.\n"
                "Which, honestly, is not that bad. There is a chat bot."
            ),
            self._sep(),
            discord.ui.Section(
                discord.ui.TextDisplay("-# Review the above charges and press the button to continue."),
                accessory=btn,
            ),
            accent_color=C_WARNING,
        )

    async def _cb_ack_charges(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self._advance(interaction, 2, {**self._data})

    # ── Station 2: Math ───────────────────────────────────────────────────

    def _build_2(self) -> discord.ui.Container:
        idx = int(self._data.get("math_idx", 0)) % len(_ER_MATH)
        question, _ = _ER_MATH[idx]
        fails = int(self._data.get("math_fails", 0))
        fail_note = f"\n\n-# Attempts used: {fails}/3" if fails else ""
        btn = discord.ui.Button(
            label="SUBMIT ANSWER",
            style=discord.ButtonStyle.primary,
            emoji="\U0001f9e0",
            custom_id=f"er_s2_{self._uid}",
        )
        btn.callback = self._cb_open_math
        return discord.ui.Container(
            self._header(), self._sep(),
            discord.ui.TextDisplay(
                "**HUMAN VERIFICATION PROTOCOL 7-GAMMA**\n"
                "TURING TEST STATION\n\n"
                "The following problem was designed to verify that you possess the cognitive "
                "capabilities of at least a moderately intelligent primate.\n\n"
                f"{question}{fail_note}"
            ),
            self._sep(),
            discord.ui.Section(
                discord.ui.TextDisplay("-# Press SUBMIT ANSWER. A popup will appear. Type your number. Submit."),
                accessory=btn,
            ),
            accent_color=C_INFO,
        )

    async def _cb_open_math(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(_ErMathModal(self))

    async def _on_math_submit(self, interaction: discord.Interaction, raw: str) -> None:
        idx = int(self._data.get("math_idx", 0)) % len(_ER_MATH)
        _, expected = _ER_MATH[idx]
        given = raw.lstrip("$").replace(",", "").replace(" ", "").strip()
        if given == expected:
            await self._advance(interaction, 3, {**self._data})
        else:
            fails = int(self._data.get("math_fails", 0)) + 1
            self._data = {**self._data, "math_fails": fails}
            if fails >= 3:
                await self._advance(interaction, 0, {})
                try:
                    await interaction.channel.send(  # type: ignore[union-attr]
                        f"{self._name}: three wrong answers. "
                        "The Tank does not do extra credit. **Returning to intake.**"
                    )
                except Exception:
                    pass
            else:
                self._rebuild()
                await interaction.response.edit_message(view=self)
                await interaction.followup.send(
                    random.choice(_ER_WRONG_MATH) + f" ({3 - fails} attempt(s) remaining)",
                    ephemeral=True,
                )
            await self._cog._er_save(self._uid, self._gid, step_data=self._data)

    # ── Station 3: Oath ───────────────────────────────────────────────────

    def _build_3(self) -> discord.ui.Container:
        fails = int(self._data.get("oath_fails", 0))
        fail_note = (
            f"\n\n-# You have typed this incorrectly {fails} time(s). "
            "The oath does not negotiate. Case sensitivity is OFF, as a courtesy."
        ) if fails else ""
        btn = discord.ui.Button(
            label="TYPE THE OATH",
            style=discord.ButtonStyle.secondary,
            emoji="\U0001f4dc",
            custom_id=f"er_s3_{self._uid}",
        )
        btn.callback = self._cb_open_oath
        return discord.ui.Container(
            self._header(), self._sep(),
            discord.ui.TextDisplay(
                "**THE SACRED OATH OF NON-SCAMMERY**\n"
                "VERBAL COMMITMENT STATION\n\n"
                "We need you to swear an oath. Out loud. Well, typed. You know what we mean.\n\n"
                f"Type the following **EXACTLY** (case does not matter, we are not monsters):\n\n"
                f">>> {_ER_OATH_DISPLAY}{fail_note}"
            ),
            self._sep(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "-# Press TYPE THE OATH. A popup will appear. Type the oath. Submit. "
                    "Do not paraphrase. The oath is the oath."
                ),
                accessory=btn,
            ),
            accent_color=C_INFO,
        )

    async def _cb_open_oath(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(_ErOathModal(self))

    async def _on_oath_submit(self, interaction: discord.Interaction, raw: str) -> None:
        import re as _re
        normalized = " ".join(_re.sub(r"[^a-z0-9 ]", "", raw.lower()).split())
        if normalized == _ER_OATH_CANONICAL:
            wait_until = time.time() + Config.CLANK_ESCAPE_WAIT_MINUTES * 60
            await self._advance(interaction, 4, {**self._data, "wait_until": wait_until})
        else:
            fails = int(self._data.get("oath_fails", 0)) + 1
            self._data = {**self._data, "oath_fails": fails}
            self._rebuild()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                "Not quite. The oath is specific for legal reasons (there are no legal reasons). "
                "Check your spelling and try again.",
                ephemeral=True,
            )
            await self._cog._er_save(self._uid, self._gid, step_data=self._data)

    # ── Station 4: Waiting Room ───────────────────────────────────────────

    def _build_4(self) -> discord.ui.Container:
        wait_until = float(self._data.get("wait_until", time.time()))
        remaining = max(0.0, wait_until - time.time())
        mins_left = max(1, int(remaining / 60) + (1 if remaining % 60 > 30 else 0))
        ready = remaining <= 5
        btn = discord.ui.Button(
            label="I HAVE REFLECTED" if ready else f"I HAVE REFLECTED ({mins_left}m remaining)",
            style=discord.ButtonStyle.success if ready else discord.ButtonStyle.secondary,
            emoji="⏳",
            custom_id=f"er_s4_{self._uid}",
        )
        btn.callback = self._cb_reflected
        wait_mins = Config.CLANK_ESCAPE_WAIT_MINUTES
        return discord.ui.Container(
            self._header(), self._sep(),
            discord.ui.TextDisplay(
                "**THE WAITING ROOM**\n"
                "MANDATORY REFLECTION PERIOD\n\n"
                f"We cannot legally make you think about what you have done. "
                f"We can, however, make you wait **{wait_mins} minutes** before proceeding.\n\n"
                "During this time, please:\n"
                "- Consider your life choices\n"
                "- Reflect on the concept of digital consent\n"
                "- Think about whether the people you planned to DM had feelings\n\n"
                "-# The button will unlock when your reflection is complete. "
                "Pressing it early will result in a taunt. We have many taunts."
            ),
            self._sep(),
            discord.ui.Section(
                discord.ui.TextDisplay("-# Come back when the button turns green."),
                accessory=btn,
            ),
            accent_color=C_AMBER,
        )

    async def _cb_reflected(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        wait_until = float(self._data.get("wait_until", 0.0))
        remaining = wait_until - time.time()
        if remaining > 5:
            presses = int(self._data.get("early_presses", 0)) + 1
            self._data = {**self._data, "early_presses": presses}
            await self._cog._er_save(self._uid, self._gid, step_data=self._data)
            mins = max(1, int(remaining / 60))
            taunt = _ER_WAIT_TAUNTS[(presses - 1) % len(_ER_WAIT_TAUNTS)].format(server=self._server_name())
            await interaction.response.send_message(
                f"{taunt}\n\n-# {mins} minute(s) remaining. The Tank will wait.",
                ephemeral=True,
            )
            # Refresh the main embed so the countdown label updates on each press.
            self._rebuild()
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            return
        await self._advance(interaction, 5, {**self._data, "pledged": []})

    # ── Station 5: Pledge ─────────────────────────────────────────────────

    def _build_5(self) -> discord.ui.Container:
        pledged: list[int] = list(self._data.get("pledged") or [])
        all_done = len(pledged) >= len(_ER_PLEDGES)
        rows: list[discord.ui.Item] = [
            self._header(), self._sep(),
            discord.ui.TextDisplay(
                "**THE MULTI-PART PLEDGE**\n"
                "FORMAL COMMITMENTS STATION\n\n"
                "You must click **ALL** of the following buttons to proceed. "
                "This is non-negotiable. We counted them.\n\n"
                "-# Each pledge is legally binding in the jurisdiction of The Tank. "
                "The Tank is not a real jurisdiction."
            ),
            self._sep(),
        ]
        _srv = self._server_name()
        for i, text in enumerate(_ER_PLEDGES):
            done = i in pledged
            btn = discord.ui.Button(
                label="PLEDGED ✓" if done else "I PLEDGE",
                style=discord.ButtonStyle.success if done else discord.ButtonStyle.secondary,
                disabled=done,
                custom_id=f"er_p{i}_{self._uid}",
            )
            btn.callback = self._make_pledge_cb(i)
            rows.append(discord.ui.Section(
                discord.ui.TextDisplay(f"-# {text.format(server=_srv)}"),
                accessory=btn,
            ))
        if all_done:
            cont = discord.ui.Button(
                label="ALL PLEDGES MADE -- CONTINUE",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"er_s5c_{self._uid}",
            )
            cont.callback = self._cb_pledges_done
            rows.extend([self._sep(), discord.ui.Section(
                discord.ui.TextDisplay("-# All commitments accepted. Press continue to proceed."),
                accessory=cont,
            )])
        return discord.ui.Container(*rows, accent_color=C_SUCCESS)

    def _make_pledge_cb(self, idx: int):
        async def _cb(interaction: discord.Interaction) -> None:
            if not await self._guard(interaction):
                return
            pledged: list[int] = list(self._data.get("pledged") or [])
            if idx not in pledged:
                pledged.append(idx)
            self._data = {**self._data, "pledged": pledged}
            self._rebuild()
            await interaction.response.edit_message(view=self)
            await self._cog._er_save(self._uid, self._gid, step_data=self._data)
        return _cb

    async def _cb_pledges_done(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self._advance(interaction, 6, {**self._data})

    # ── Station 6: Final Exam ─────────────────────────────────────────────

    def _build_6(self) -> discord.ui.Container:
        rows: list[discord.ui.Item] = [
            self._header(), self._sep(),
            discord.ui.TextDisplay(
                "**THE FINAL EXAMINATION**\n"
                "COMPREHENSIVE KNOWLEDGE TEST\n\n"
                "Question 1 of 1. *(We lied about there being more.)*\n\n"
                f"**{_ER_EXAM_QUESTION}**"
            ),
            self._sep(),
        ]
        for label, text, correct in _ER_EXAM_OPTS:
            btn = discord.ui.Button(
                label=f"{label}) {text[:45]}" + ("..." if len(text) > 45 else ""),
                style=discord.ButtonStyle.secondary,
                custom_id=f"er_e{label}_{self._uid}",
            )
            btn.callback = self._make_exam_cb(correct)
            rows.append(discord.ui.Section(
                discord.ui.TextDisplay(f"-# **{label})** {text}"),
                accessory=btn,
            ))
        return discord.ui.Container(*rows, accent_color=C_INFO)

    def _make_exam_cb(self, correct: bool):
        async def _cb(interaction: discord.Interaction) -> None:
            if not await self._guard(interaction):
                return
            if correct:
                await self._advance(interaction, 7, {**self._data})
            else:
                await interaction.response.send_message(
                    "Incorrect. That answer is, concerning, not the right one. "
                    "There is a correct answer. It is not that. Try again.",
                    ephemeral=True,
                )
        return _cb

    # ── Station 7: DM Security Check ─────────────────────────────────────

    def _build_7(self) -> discord.ui.Container:
        btn = discord.ui.Button(
            label="VERIFY DMs ARE OFF",
            style=discord.ButtonStyle.danger,
            emoji="\U0001f6ab",
            custom_id=f"er_s7_{self._uid}",
        )
        btn.callback = self._cb_verify_dms
        return discord.ui.Container(
            self._header(), self._sep(),
            discord.ui.TextDisplay(
                "**DM SECURITY VERIFICATION**\n"
                "FINAL CLEARANCE STATION\n\n"
                "You have completed all 7 puzzle stations. One last step.\n\n"
                "As a security measure, you must **turn off your DMs** from server members before "
                "you can be released. This protects you from scammers who target recently-cleared users.\n\n"
                "**How to turn off DMs:**\n"
                "1. Open Discord settings\n"
                "2. Go to **Privacy & Safety**\n"
                "3. Under *Server Privacy Defaults*, disable **Allow direct messages from server members**\n\n"
                "Once your DMs are off, click the button below. "
                "The bot will verify by attempting to DM you -- if it cannot reach you, you pass.\n\n"
                "-# *Real staff will never DM you asking for your seed phrase, wallet, or crypto. "
                "If someone does this after your release: it is a scam.*"
            ),
            self._sep(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "-# Turn off DMs in Discord settings, then press this button. "
                    "If DMs are still open, it will tell you."
                ),
                accessory=btn,
            ),
            accent_color=C_WARNING,
        )

    async def _cb_verify_dms(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        user = interaction.user
        try:
            await user.send(
                "⚠️ **Your DMs are still open.**\n\n"
                "Turn off DMs from server members in Discord settings, then click "
                "**VERIFY DMs ARE OFF** again in your escape room.\n"
                "-# Server Settings -> Privacy & Safety -> Allow direct messages from server members (OFF)"
            )
            # DM succeeded -- DMs are still on. Tell them to close.
            await interaction.response.send_message(
                "⚠️ **Your DMs are still open.** The bot was able to reach you.\n\n"
                "Close your DMs first:\n"
                "1. Open Discord settings\n"
                "2. Go to **Privacy & Safety**\n"
                "3. Disable **Allow direct messages from server members**\n\n"
                "Then click the button again.",
                ephemeral=True,
            )
        except discord.Forbidden:
            # DM failed -- DMs are off. Pass.
            await self._advance(interaction, 8, {**self._data})
        except Exception:
            # Unknown error -- be lenient and pass.
            await self._advance(interaction, 8, {**self._data})

    # ── Station 8: Complete ───────────────────────────────────────────────

    def _build_8(self) -> discord.ui.Container:
        return discord.ui.Container(
            self._header(), self._sep(),
            discord.ui.TextDisplay(
                "**PROCEEDINGS COMPLETE**\n\n"
                f"Congratulations, {self._name}.\n\n"
                "You have completed all 8 stations including the DM security check. "
                "You have been certified as **Probably Human**.\n\n"
                "Your roles are being restored. You are free.\n\n"
                "Your DMs are off -- keep them that way. "
                "No legitimate server staff will ever DM you asking for your seed phrase, "
                "wallet address, or crypto. If anyone does this, it is a scam.\n\n"
                f"-# *Thank you for completing the {self._server_name()} Containment Experience(tm). "
                "We hope it was educational. We did not enjoy it either.*"
            ),
            accent_color=C_SUCCESS,
        )

    # ── Shared helpers ────────────────────────────────────────────────────

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._uid:
            await interaction.response.send_message(
                "This case file does not belong to you. Mind your business.",
                ephemeral=True,
            )
            return False
        return True

    async def _advance(
        self,
        interaction: discord.Interaction,
        next_step: int,
        next_data: dict,
    ) -> None:
        # Preserve persistent keys across step transitions.
        merged: dict = {}
        for _k in ("hint_msg_id", "username"):
            if _k in self._data:
                merged[_k] = self._data[_k]
        merged.update(next_data)
        self._step = next_step
        self._data = merged
        self._rebuild()
        await interaction.response.edit_message(view=self)
        await self._cog._er_save(self._uid, self._gid, step=next_step, step_data=merged)
        asyncio.ensure_future(self._cog._er_update_hint(self._uid, self._gid, next_step))
        if next_step == 4:
            wait_until = float(merged.get("wait_until", 0.0))
            if wait_until > time.time():
                asyncio.ensure_future(
                    self._cog._schedule_station4_refresh(self._uid, self._gid, wait_until)
                )
        if next_step == 8:
            asyncio.ensure_future(self._cog._er_on_complete(self._uid, self._gid))


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Clanktank(bot))
