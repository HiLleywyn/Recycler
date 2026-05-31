"""
core/framework/content_filter.py  -  Lightweight content sanitiser for user-set text.

Strips URLs, Discord invite links, @everyone/@here mentions, and known
scam/exploit patterns from player-provided strings (group names, descriptions,
tags, image URLs, etc.).

This runs synchronously and does NOT call AI  -  it's a fast regex-based gate.
The AI scam classifier in cogs/moderation.py handles full message analysis;
this module handles display-time sanitisation of stored user content.

Usage:
    from core.framework.content_filter import sanitize_text, is_safe_url, sanitize_display

    clean = sanitize_text("My Cool Group https://scam.link")
    # => "My Cool Group"

    safe = is_safe_url("https://i.imgur.com/abc.png")
    # => True

    display = sanitize_display("@everyone FREE CRYPTO https://evil.com")
    # => "@​everyone FREE CRYPTO"  (zero-width space breaks mention, URL stripped)
"""
from __future__ import annotations

import re

# ═══════════════════════════════════════════════════════════════════════════
# Patterns
# ═══════════════════════════════════════════════════════════════════════════

# Any URL (http/https/ftp/discord invite links)
_URL_RE = re.compile(
    r"https?://\S+"
    r"|discord\.gg/\S+"
    r"|discord\.com/invite/\S+"
    r"|discordapp\.com/invite/\S+",
    re.IGNORECASE,
)

# Discord invite patterns (even without full URL)
_INVITE_RE = re.compile(
    r"discord\.gg/\S+"
    r"|discord\.com/invite/\S+"
    r"|discordapp\.com/invite/\S+",
    re.IGNORECASE,
)

# @everyone / @here mentions
_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)

# Discord entity mentions: <@userid>, <@!userid>, <@&roleid>, <#channelid>
_ENTITY_MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>")

# Raw snowflake IDs (17-20 digit numbers that look like Discord IDs)
_RAW_SNOWFLAKE_RE = re.compile(r"\b\d{17,20}\b")

# Common scam/phishing keywords (case-insensitive)
_SCAM_KEYWORDS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"free\s*(crypto|nft|airdrop|giveaway|money|moneta|arc)",
        r"(claim|redeem)\s*(your|free|now)",
        r"seed\s*phrase",
        r"private\s*key",
        r"connect\s*(your\s*)?wallet",
        r"verify\s*(your\s*)?(account|wallet)",
        r"limited\s*time\s*(offer|only)",
        r"send\s*\d+.*get\s*\d+.*back",
        r"double\s*your\s*(money|crypto|coins|tokens)",
    ]
]

# Allowed image URL domains (for group images, etc.)
_SAFE_IMAGE_DOMAINS: frozenset[str] = frozenset({
    "i.imgur.com", "imgur.com",
    "cdn.discordapp.com", "media.discordapp.net",
    "i.redd.it", "preview.redd.it",
    "pbs.twimg.com",
    "upload.wikimedia.org",
    "raw.githubusercontent.com",
})


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def sanitize_text(text: str, *, allow_urls: bool = False) -> str:
    """Remove URLs, invite links, and @mentions from user-provided text.

    Returns cleaned text with multiple spaces collapsed.
    If *allow_urls* is True, only invite links are stripped (not all URLs).
    """
    if not text:
        return text

    # Strip invite links always
    result = _INVITE_RE.sub("", text)

    # Strip all URLs unless explicitly allowed
    if not allow_urls:
        result = _URL_RE.sub("", result)

    # Defang @everyone/@here by inserting a zero-width space
    result = _MENTION_RE.sub("@\u200b" + r"\1", result)

    # Collapse whitespace
    result = re.sub(r"\s+", " ", result).strip()
    return result


def has_scam_patterns(text: str) -> bool:
    """Return True if text matches known scam/phishing patterns."""
    if not text:
        return False
    for pattern in _SCAM_KEYWORDS:
        if pattern.search(text):
            return True
    return False


def is_safe_url(url: str) -> bool:
    """Check if a URL is from a known safe image hosting domain."""
    if not url:
        return False
    url_lower = url.lower().strip()
    if not url_lower.startswith(("http://", "https://")):
        return False
    # Extract domain
    try:
        # Remove protocol
        after_proto = url_lower.split("://", 1)[1]
        domain = after_proto.split("/", 1)[0].split(":", 1)[0]
        return domain in _SAFE_IMAGE_DOMAINS
    except (IndexError, ValueError):
        return False


def sanitize_display(text: str) -> str:
    """Sanitize text for safe display in embeds/messages.

    More aggressive than sanitize_text: also strips scam keywords
    and replaces them with [blocked].
    """
    if not text:
        return text

    result = sanitize_text(text)

    for pattern in _SCAM_KEYWORDS:
        result = pattern.sub("[blocked]", result)

    return result


def has_discord_entities(text: str) -> bool:
    """Return True if text contains Discord mentions (<@id>, <@&id>, <#id>) or raw snowflake IDs."""
    if not text:
        return False
    if _ENTITY_MENTION_RE.search(text):
        return True
    if _RAW_SNOWFLAKE_RE.search(text):
        return True
    return False


def validate_group_name(name: str) -> tuple[bool, str]:
    """Validate a group name. Returns (ok, error_message)."""
    if not name or not name.strip():
        return False, "Name cannot be empty."
    if len(name) > 32:
        return False, "Name must be 32 characters or fewer."

    cleaned = sanitize_text(name)
    if cleaned != name.strip():
        return False, "Name cannot contain URLs or invite links."

    if has_scam_patterns(name):
        return False, "Name contains blocked content."

    if _MENTION_RE.search(name):
        return False, "Name cannot contain @everyone or @here."

    if has_discord_entities(name):
        return False, "Name cannot contain user mentions, role mentions, channels, or IDs."

    return True, ""


def validate_group_description(desc: str) -> tuple[bool, str]:
    """Validate a group description. Returns (ok, error_message)."""
    if len(desc) > 200:
        return False, "Description must be 200 characters or fewer."

    if has_scam_patterns(desc):
        return False, "Description contains blocked content."

    # Block Discord mentions and raw IDs
    if has_discord_entities(desc):
        return False, "Description cannot contain user mentions, role mentions, channels, or IDs."

    # Block URLs in descriptions
    if _URL_RE.search(desc):
        return False, "Description cannot contain URLs or links."

    return True, ""


def make_group_token(group_name: str, tag: str) -> tuple[str, str]:
    """Derive a token symbol and display name from a group tag and name.

    Returns ``(symbol, token_name)`` where:
      - ``symbol``     is the tag uppercased, stripped to <=4 alphanumeric chars
      - ``token_name`` is "<group_name> Token" truncated to 50 chars

    The caller is responsible for checking uniqueness before registering.
    """
    import re as _re
    # Strip any non-alphanumeric characters and take first 4
    clean_sym = _re.sub(r"[^A-Z0-9]", "", tag.upper())[:4]
    if not clean_sym:
        # Fallback: first letters of each word in the group name
        words = group_name.split()
        clean_sym = "".join(w[0].upper() for w in words if w)[:4]
    token_name = f"{group_name.strip()} Token"[:50]
    return clean_sym, token_name


def validate_image_url(url: str) -> tuple[bool, str]:
    """Validate an image URL. Returns (ok, error_message)."""
    if not url:
        return True, ""

    if not is_safe_url(url):
        domains = ", ".join(sorted(_SAFE_IMAGE_DOMAINS)[:5])
        return False, f"Image URL must be from a trusted host ({domains}, ...)."

    return True, ""
