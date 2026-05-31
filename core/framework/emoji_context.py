"""core/framework/emoji_context.py - Emoji meaning context, reaction triggers, and guild emoji support.

Provides:
  - EMOJI_MEANINGS: static table of unicode emoji -> (category, plain-English meaning)
  - SERVER_EMOJI_HINTS: maps common custom emoji name substrings to categories
  - detect_reaction_category(content): analyse message text and return a reaction category
  - pick_emoji(guild, category): return the best emoji for a category (server > unicode fallback)
  - get_emoji_description(emoji_str): human-readable meaning for context logging
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

# ── Category constants ────────────────────────────────────────────────────────

CAT_LOSS       = "loss"        # rekt, liquidated, rug
CAT_WIN        = "win"         # jackpot, moon, profit
CAT_HYPE       = "hype"        # lets go, lfg, bullish
CAT_FRUSTRATION= "frustration" # ugh, bruh, wtf
CAT_LAUGH      = "laugh"       # lmao, lol, kek
CAT_GG         = "gg"          # gg, well played
CAT_SALT       = "salt"        # salty, cope, seething
CAT_SHOCK      = "shock"       # omg, no way, wtf
CAT_VIBE       = "vibe"        # gm, chill, wagmi
CAT_SARCASM    = "sarcasm"     # sure, totally, uh huh
CAT_SUPPORT    = "support"     # you good?, hang in there
CAT_CELEBRATE  = "celebrate"   # congrats, birthday, anniversary
CAT_DEGENERATE = "degenerate"  # ape in, yolo, degen move

# ── Unicode emoji -> (category, meaning) ─────────────────────────────────────

EMOJI_MEANINGS: dict[str, tuple[str, str]] = {
    # Loss / rekt
    "💀": (CAT_LOSS,        "dead, completely rekt"),
    "🪦": (CAT_LOSS,        "rip, buried, gone forever"),
    "😭": (CAT_LOSS,        "crying, devastated by a loss"),
    "📉": (CAT_LOSS,        "price going down, portfolio down"),
    "🩸": (CAT_LOSS,        "bleeding out, heavy loss"),
    "⚰️": (CAT_LOSS,        "coffin, dead trade"),
    "🔴": (CAT_LOSS,        "red, in the negative"),
    "😢": (CAT_LOSS,        "sad, small loss or disappointment"),
    "😩": (CAT_FRUSTRATION, "exhausted and frustrated"),
    "🤡": (CAT_LOSS,        "clown behavior, bad trade decision"),
    # Win / moon
    "🚀": (CAT_WIN,         "mooning, pumping, going up"),
    "🌙": (CAT_WIN,         "to the moon, late night gain"),
    "💰": (CAT_WIN,         "money bag, big profit"),
    "🤑": (CAT_WIN,         "money face, very profitable"),
    "📈": (CAT_WIN,         "price going up, portfolio pumping"),
    "🔥": (CAT_HYPE,        "fire, insane, something is hot"),
    "✅": (CAT_WIN,         "confirmed, success"),
    "💎": (CAT_WIN,         "diamond hands, holding strong"),
    "👑": (CAT_WIN,         "king, won the top spot"),
    "🏆": (CAT_CELEBRATE,   "trophy, first place win"),
    "💯": (CAT_WIN,         "100%, absolutely correct or based"),
    # Hype / bullish
    "⚡": (CAT_HYPE,        "electric, energy, fast gains"),
    "🎯": (CAT_HYPE,        "on target, perfect call"),
    "🦁": (CAT_HYPE,        "bold, strong, bullish energy"),
    "🐂": (CAT_HYPE,        "bull, bullish market"),
    "💪": (CAT_HYPE,        "strong, power move"),
    # Frustration / anger
    "😤": (CAT_FRUSTRATION, "huffing, frustrated, salty"),
    "🤬": (CAT_FRUSTRATION, "very angry, cursing"),
    "😠": (CAT_FRUSTRATION, "angry, annoyed"),
    "🤦": (CAT_FRUSTRATION, "facepalm, unbelievable mistake"),
    "🫠": (CAT_FRUSTRATION, "melting, done, gave up"),
    # Salt / cope
    "🧂": (CAT_SALT,        "salty, heavy salt, coping"),
    "😒": (CAT_SALT,        "unimpressed, low-key salty"),
    "🙄": (CAT_SALT,        "eye roll, sure whatever"),
    # Laugh / kek
    "😂": (CAT_LAUGH,       "laughing out loud"),
    "🤣": (CAT_LAUGH,       "rolling on the floor laughing"),
    "😆": (CAT_LAUGH,       "grinning, amused"),
    "👀": (CAT_SHOCK,       "eyes, watching closely, caught something"),
    # GG
    "🫡": (CAT_GG,          "salute, respect, well played"),
    "🤝": (CAT_GG,          "handshake, deal, mutual respect"),
    "👏": (CAT_GG,          "clapping, applause"),
    "🎉": (CAT_CELEBRATE,   "party, celebration, congrats"),
    # Shock
    "😱": (CAT_SHOCK,       "screaming, shocked, jaw dropped"),
    "😮": (CAT_SHOCK,       "mouth open, surprised"),
    "🫢": (CAT_SHOCK,       "gasping, cant believe it"),
    "‼️": (CAT_SHOCK,       "double exclamation, major event"),
    # Vibe / gm
    "🌅": (CAT_VIBE,        "sunrise, good morning, new day"),
    "☕": (CAT_VIBE,        "coffee, morning routine"),
    "😴": (CAT_VIBE,        "sleeping, went to bed, afk"),
    "🙏": (CAT_VIBE,        "praying, please, hoping"),
    "❤️": (CAT_SUPPORT,     "heart, warmth, genuine care"),
    "🫂": (CAT_SUPPORT,     "hug, support, its going to be okay"),
    # Sarcasm / irony
    "😏": (CAT_SARCASM,     "smirk, knowing look, told you so"),
    "🤭": (CAT_SARCASM,     "hand over mouth, suppressing a laugh"),
    # Degenerate
    "🎰": (CAT_DEGENERATE,  "gambling, slots, aping in"),
    "🪙": (CAT_DEGENERATE,  "coin, crypto, on-chain"),
    "🐸": (CAT_DEGENERATE,  "frog, degen culture"),
    "🦍": (CAT_DEGENERATE,  "ape, aping in, degen move"),
    "🌊": (CAT_LOSS,        "got wrecked by a wave, swept away"),
}

# ── Server emoji name hints -> category ───────────────────────────────────────
# These match substrings in custom guild emoji names (case-insensitive).

SERVER_EMOJI_HINTS: list[tuple[str, str]] = [
    # Loss / rekt
    ("rip", CAT_LOSS), ("rekt", CAT_LOSS), ("dead", CAT_LOSS), ("ded", CAT_LOSS),
    ("peepo_sad", CAT_LOSS), ("pepehands", CAT_LOSS), ("feelsbad", CAT_LOSS),
    ("bleed", CAT_LOSS), ("noooo", CAT_LOSS), ("wasted", CAT_LOSS),
    ("liquidat", CAT_LOSS), ("rug", CAT_LOSS), ("bankrupt", CAT_LOSS),
    ("skull", CAT_LOSS), ("coffin", CAT_LOSS), ("cry", CAT_LOSS),
    # Win / hype
    ("goat", CAT_WIN), ("pog", CAT_HYPE), ("pogchamp", CAT_HYPE),
    ("hypers", CAT_HYPE), ("hype", CAT_HYPE), ("let", CAT_HYPE),
    ("gg", CAT_GG), ("ez", CAT_GG), ("clap", CAT_GG),
    ("chad", CAT_WIN), ("based", CAT_WIN), ("dub", CAT_WIN),
    ("moon", CAT_WIN), ("rocket", CAT_WIN), ("pump", CAT_WIN),
    ("rich", CAT_WIN), ("money", CAT_WIN), ("moneybag", CAT_WIN),
    # Laugh
    ("kek", CAT_LAUGH), ("lul", CAT_LAUGH), ("lmao", CAT_LAUGH),
    ("xd", CAT_LAUGH), ("pepega", CAT_LAUGH), ("monka", CAT_LAUGH),
    # Frustration
    ("angry", CAT_FRUSTRATION), ("mad", CAT_FRUSTRATION),
    ("facepalm", CAT_FRUSTRATION), ("bruh", CAT_FRUSTRATION),
    ("copium", CAT_SALT), ("salt", CAT_SALT), ("cope", CAT_SALT),
    # Shock
    ("omg", CAT_SHOCK), ("shock", CAT_SHOCK), ("wtf", CAT_SHOCK),
    # Degen
    ("degen", CAT_DEGENERATE), ("ape", CAT_DEGENERATE), ("yolo", CAT_DEGENERATE),
    ("gamble", CAT_DEGENERATE), ("casino", CAT_DEGENERATE),
    # Vibe
    ("gm", CAT_VIBE), ("comfy", CAT_VIBE), ("chill", CAT_VIBE),
    ("wave", CAT_VIBE), ("hello", CAT_VIBE), ("hi", CAT_VIBE),
]

# ── Fallback unicode pools per category ──────────────────────────────────────

_UNICODE_POOL: dict[str, list[str]] = {
    CAT_LOSS:        ["💀", "🪦", "😭", "📉", "🤡", "😢"],
    CAT_WIN:         ["🚀", "💰", "🤑", "📈", "💎", "👑"],
    CAT_HYPE:        ["🔥", "⚡", "💪", "🎯", "🦁"],
    CAT_FRUSTRATION: ["😤", "🤦", "🫠", "😩", "🤬"],
    CAT_LAUGH:       ["😂", "🤣", "😆", "💀"],
    CAT_GG:          ["🫡", "🤝", "👏", "💯"],
    CAT_SALT:        ["🧂", "😒", "🙄"],
    CAT_SHOCK:       ["😱", "😮", "🫢", "👀"],
    CAT_VIBE:        ["🌅", "☕", "🙏", "😊"],
    CAT_SARCASM:     ["😏", "🤭", "🙄"],
    CAT_SUPPORT:     ["🫂", "❤️", "🙏"],
    CAT_CELEBRATE:   ["🎉", "🏆", "🎊", "💯"],
    CAT_DEGENERATE:  ["🦍", "🎰", "🐸", "🪙"],
}

# ── Content pattern -> category triggers ─────────────────────────────────────

_TRIGGER_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Loss triggers
    (re.compile(r"\b(f+u+c+k+|ffs|wtff|rugged|liquidat|rekt|ngmi|rip|im\s*done|lost\s*(it|all|everything)|gone|wiped|blew\s*up|buried|dead|nooo+|damn\s*it|ggs\s*i\s*lose)\b", re.I), CAT_LOSS),
    # Win / moon
    (re.compile(r"\b(lets?\s*go+|lfg|mooning?|pumping|hit\s*it|jackpot|won|clean|dub|profit|in\s*profit|i\s*(won|made|hit)|nice\s*hit|gg\s*ez|goated)\b", re.I), CAT_WIN),
    # Hype
    (re.compile(r"\b(hype|based|chad|poggers?|pog|fire|insane|crazy\s*good|bullish|send\s*it)\b", re.I), CAT_HYPE),
    # Frustration
    (re.compile(r"\b(bruh+|ugh+|smh|why\s*tho|this\s*is\s*(so\s*)?(dumb|stupid|bs)|unbelievable|facepalm|tf\s*is\s*this)\b", re.I), CAT_FRUSTRATION),
    # Laugh
    (re.compile(r"\b(lmao+|lol+|lmfao|kek+|haha+|hehe+|dead\s*xd|i\s*(cant|can't)|im\s*crying)\b", re.I), CAT_LAUGH),
    # Salt
    (re.compile(r"\b(copium|cope|salty|seething|whatever|skill\s*issue|rigged|not\s*fair|unfair)\b", re.I), CAT_SALT),
    # Shock
    (re.compile(r"\b(omg+|oh\s*my|no\s*way|wait\s*what|holy\s*(shit|moly|cow)|bro\s*what|what\s*the)\b", re.I), CAT_SHOCK),
    # Vibe / gm
    (re.compile(r"^(gm|gn|good\s*morning|good\s*night|wagmi|woke\s*up)\b", re.I), CAT_VIBE),
    # GG
    (re.compile(r"^(gg+|good\s*game|wp|well\s*played|ez\s*clap)\b", re.I), CAT_GG),
    # Degenerate
    (re.compile(r"\b(aping?\s*in|yolo|degen|all\s*in|send\s*it|no\s*thoughts?\s*head\s*empty|goblin\s*mode)\b", re.I), CAT_DEGENERATE),
]


def detect_reaction_category(content: str) -> str | None:
    """Return the most relevant reaction category for a message, or None if no match."""
    for pattern, category in _TRIGGER_PATTERNS:
        if pattern.search(content):
            return category
    return None


def pick_emoji(guild: "discord.Guild | None", category: str, *, seed: int | None = None) -> str:
    """Return the best emoji for a category.

    Prefers server custom emojis whose names hint at the category,
    then falls back to the unicode pool.
    """
    import random as _rng
    rng = _rng.Random(seed) if seed is not None else _rng

    if guild is not None:
        candidates: list[str] = []
        for emoji in guild.emojis:
            name_lower = emoji.name.lower()
            for hint, cat in SERVER_EMOJI_HINTS:
                if cat == category and hint in name_lower:
                    candidates.append(str(emoji))
                    break
        if candidates:
            return rng.choice(candidates)

    pool = _UNICODE_POOL.get(category, ["❓"])
    return rng.choice(pool)


def get_emoji_description(emoji_str: str) -> str:
    """Return a plain-English description for context logging."""
    meaning = EMOJI_MEANINGS.get(emoji_str)
    if meaning:
        return f"{emoji_str} ({meaning[1]})"
    # Try to identify as a custom emoji
    if emoji_str.startswith("<") and ":" in emoji_str:
        name = emoji_str.split(":")[1] if ":" in emoji_str else emoji_str
        return f"server emoji :{name}:"
    return emoji_str


async def build_guild_emoji_context(
    guild: "discord.Guild | None", *, limit: int = 40, db=None,
) -> str:
    """Build a system-prompt block listing the guild's custom emojis.

    The AI is shown each emoji in its raw `<:name:id>` / `<a:name:id>` form so
    it can paste the exact markup into a reply and Discord will render it.

    When a per-guild indexed meaning exists in ``guild_emoji_meanings`` (see
    migration 0107 + :mod:`core.framework.emoji_index`), its nuanced description
    is surfaced alongside the raw markup so the chat model can match the
    emoji to the conversation with real context -- not just the static
    substring hint from :data:`SERVER_EMOJI_HINTS`. The substring hint is
    used as a fallback when no DB entry exists yet.

    Returns an empty string when the guild is missing, has no custom emojis,
    or all emojis are unavailable (e.g. a lapsed boost tier).
    """
    if guild is None:
        return ""
    try:
        emojis = list(guild.emojis)
    except Exception:
        return ""
    if not emojis:
        return ""

    # Pull indexed meanings in one round-trip (if we have a DB handle).
    meanings_by_id: dict[int, dict] = {}
    if db is not None:
        try:
            rows = await db.get_all_emoji_meanings(guild.id)
            meanings_by_id = {int(r["emoji_id"]): r for r in rows}
        except Exception:
            meanings_by_id = {}

    lines: list[str] = []
    for emoji in emojis:
        if len(lines) >= limit:
            break
        try:
            # Skip unavailable emojis (lost boost tier, etc.) so we don't
            # tell the model to use markup Discord will render as broken.
            if getattr(emoji, "available", True) is False:
                continue
            raw = str(emoji)  # "<:name:id>" or "<a:name:id>"
            meaning = meanings_by_id.get(int(emoji.id))
            if meaning:
                desc = str(meaning.get("description") or "").strip()
                cat = meaning.get("category") or ""
                tag = f"[{cat}] " if cat else ""
                if desc:
                    lines.append(f"- {raw} {tag}{desc}")
                    continue
            # No indexed meaning yet -- fall back to the static substring hint.
            name_lower = emoji.name.lower()
            hint_cat: str | None = None
            for hint, cat in SERVER_EMOJI_HINTS:
                if hint in name_lower:
                    hint_cat = cat
                    break
            if hint_cat:
                lines.append(f"- {raw} [{hint_cat}]")
            else:
                lines.append(f"- {raw}")
        except Exception:
            continue

    if not lines:
        return ""

    return (
        "SERVER CUSTOM EMOJIS - these are available for you to use in "
        "replies. Each line shows ONE emoji's literal markup followed by "
        "an optional bracketed tone tag ([loss]/[win]/[hype]/[laugh]/[gg]/"
        "[shock]/[vibe]/[salt]/[frustration]/[degenerate]) and, when "
        "indexed, a short description of how this server uses it -- pay "
        "attention to the description, it captures nuance the tone tag "
        "alone cannot.\n"
        "HOW TO USE AN EMOJI: copy the markup from the list verbatim into "
        "your reply, with no surrounding characters. The markup must be "
        "naked text -- never wrap it in backticks, single quotes, or a "
        "code block. Discord only renders the emoji image when the "
        "markup is plain text; inside `code spans` it shows as the "
        "literal characters. Same for the closing `>` -- that single "
        "character is what tells Discord the markup is complete.\n"
        "Rules: at most 1-2 emojis per reply, only when they actually "
        "fit the vibe (celebrating a win, reacting to a rug, teasing a "
        "bad trade, gm, gg, etc.). Do NOT spam, do NOT invent emoji "
        "IDs, do NOT use an emoji that doesn't appear in the list "
        "below. Prefer a server emoji over a unicode emoji when one "
        "clearly fits, otherwise skip it.\n"
        "CRITICAL EMOJI MARKUP RULE: Custom emoji markup is a single "
        "atomic token ending in `>`. NEVER end a reply mid-emoji. If "
        "you are near your output limit and unsure you can fit the "
        "full markup including the closing `>`, skip the emoji "
        "entirely. A broken markup without `>` renders as literal "
        "text and looks terrible.\n"
        "Place emojis EARLY in the reply when you use them, not as the "
        "very last characters, so truncation risk is minimal.\n"
        + "\n".join(lines)
    )
