"""services/giphy.py -- GIPHY GIF search for Disco AI chat replies."""
from __future__ import annotations

import logging
import random
import re
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_SESSION: aiohttp.ClientSession | None = None

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "i", "you", "he", "she",
    "it", "we", "they", "me", "him", "her", "us", "them", "my", "your",
    "his", "its", "our", "their", "what", "which", "who", "whom", "this",
    "that", "these", "those", "am", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "as", "into", "about", "like", "so", "but",
    "and", "or", "not", "no", "yes", "just", "also", "very", "really",
    "up", "out", "how", "when", "where", "why", "if", "then", "than",
    "there", "here", "now", "only", "after", "before", "get", "got",
    "let", "go", "see", "know", "think", "say", "make", "use",
    "s", "t", "re", "ll", "ve", "d", "m",
})

# Short user phrases mapped to better GIPHY search terms
_PHRASE_MAP: dict[str, str] = {
    "lol": "laughing",
    "lmao": "laughing hysterically",
    "haha": "laughing",
    "rofl": "rolling on floor laughing",
    "omg": "oh my god reaction",
    "wtf": "shocked reaction",
    "wow": "amazed wow reaction",
    "nice": "nice reaction",
    "cool": "cool reaction",
    "noice": "very nice reaction",
    "bruh": "bruh moment",
    "pog": "poggers excited",
    "poggers": "excited celebration",
    "gg": "good game celebration",
    "rip": "rip sad",
    "oof": "oof yikes",
    "based": "epic reaction",
    "cringe": "cringe reaction",
    "sus": "suspicious hmm",
    "lets go": "lets go celebration",
    "lesgo": "lets go celebration",
    "congrats": "congratulations celebration",
    "gratz": "congratulations",
    "grats": "congratulations",
    "thanks": "thank you",
    "ty": "thank you",
    "thx": "thank you",
    "bye": "goodbye wave",
    "hi": "hello wave",
    "hello": "hello greeting wave",
    "help": "help me please",
    "yikes": "yikes reaction",
    "facepalm": "facepalm",
    "smh": "shaking head disappointed",
    "ez": "easy win",
    "gg ez": "easy win celebration",
}


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8),
        )
    return _SESSION


def should_send_gif() -> bool:
    """Return True with probability GIPHY_GIF_PROBABILITY when key is configured."""
    from core.config import Config
    prob = float(Config.GIPHY_GIF_PROBABILITY)
    if prob <= 0.0 or not Config.GIPHY_API_KEY:
        return False
    return random.random() < prob


def pick_gif_query(user_msg: str, ai_reply: str) -> str:
    """Derive a 2-4 word GIPHY search query from the conversation context.

    Checks the user message first (shorter, more direct intent). Maps known
    slang/short phrases to richer queries, then falls back to keyword
    extraction from the AI reply.
    """
    msg = (user_msg or "").strip()
    msg = re.sub(r"<@!?\d+>", "", msg).strip()
    msg_lower = msg.lower().rstrip("!?.")

    # Exact or whole-message phrase match
    if msg_lower in _PHRASE_MAP:
        return _PHRASE_MAP[msg_lower]
    for phrase, mapped in _PHRASE_MAP.items():
        if re.fullmatch(rf"[^\w]*{re.escape(phrase)}[^\w]*", msg_lower):
            return mapped

    # Extract meaningful words - prefer user message, fall back to AI reply
    for source in (msg, (ai_reply or "")[:200]):
        tokens = [w.lower() for w in re.findall(r"[a-zA-Z]{3,}", source)]
        meaningful = [w for w in tokens if w not in _STOPWORDS]
        if len(meaningful) >= 2:
            return " ".join(meaningful[:3])

    words = msg.split()[:4]
    return " ".join(words)[:50] if words else "reaction"


async def search_gif(query: str, *, rating: str = "") -> str | None:
    """Search GIPHY and return a random result's direct GIF URL, or None.

    Uses the GIPHY public search endpoint. Returns an animated .gif media
    URL that Discord renders inline when posted as a plain message.
    """
    from core.config import Config
    key = Config.GIPHY_API_KEY
    if not key or not query.strip():
        return None
    effective_rating = rating or Config.GIPHY_GIF_RATING or "g"
    try:
        sess = await _get_session()
        async with sess.get(
            "https://api.giphy.com/v1/gifs/search",
            params={
                "api_key": key,
                "q": query,
                "limit": 10,
                "rating": effective_rating,
                "lang": "en",
            },
        ) as resp:
            if resp.status != 200:
                log.debug("[giphy] HTTP %s for %r", resp.status, query)
                return None
            payload: dict[str, Any] = await resp.json()
            results = payload.get("data") or []
            if not results:
                log.debug("[giphy] no results for %r", query)
                return None
            gif = random.choice(results)
            images = gif.get("images") or {}
            orig = images.get("original") or {}
            url = orig.get("url") or gif.get("url") or gif.get("embed_url")
            return url
    except Exception as exc:
        log.debug("[giphy] search failed for %r: %s", query, exc)
        return None
