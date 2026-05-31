"""
core/framework/amount_parser.py  -  Parse fractional and dynamic amounts from natural language.

Handles expressions like "5", "all", "half", "1/4", "$100", "2.5k", "the rest",
"everything", "three quarters", etc. and converts them into structured AmountSpec
objects for use in the NLP chain engine.

Usage:
    from core.framework.amount_parser import parse_amount, AmountSpec

    spec = parse_amount("half")
    assert spec.is_fraction and spec.fraction_value == 0.5

    spec = parse_amount("$2.5k")
    assert spec.is_usd and spec.resolved == 2500.0
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class AmountSpec:
    """Structured representation of a parsed amount expression."""

    raw: str                              # Original text: "5", "all", "half", "1/4", "$100"
    resolved: float | None = None         # Concrete value (set at parse or execution time)
    is_fraction: bool = False
    fraction_value: float | None = None   # 0.5, 0.25, etc.
    is_rest: bool = False                 # "rest", "remaining", "what's left"
    is_all: bool = False                  # "all", "everything", "entire"
    is_usd: bool = False                  # "$100", "100 dollars"
    depends_on_step: int | None = None    # Step index for dynamic resolution

    @property
    def needs_resolution(self) -> bool:
        """True if this spec cannot produce a concrete value without context."""
        return self.resolved is None and (
            self.is_all or self.is_rest or self.is_fraction or self.depends_on_step is not None
        )


# ── Fraction lookup table ───────────────────────────────────────────────────

FRACTION_MAP: dict[str, float] = {
    "half": 0.5, "a half": 0.5, "1/2": 0.5, "one half": 0.5,
    "quarter": 0.25, "a quarter": 0.25, "1/4": 0.25, "one quarter": 0.25,
    "three quarters": 0.75, "3/4": 0.75, "three fourths": 0.75,
    "third": 1 / 3, "a third": 1 / 3, "1/3": 1 / 3, "one third": 1 / 3,
    "two thirds": 2 / 3, "2/3": 2 / 3,
    "eighth": 0.125, "an eighth": 0.125, "1/8": 0.125, "one eighth": 0.125,
    "three eighths": 0.375, "3/8": 0.375,
    "tenth": 0.1, "a tenth": 0.1, "1/10": 0.1, "one tenth": 0.1,
    "fifth": 0.2, "a fifth": 0.2, "1/5": 0.2, "one fifth": 0.2,
    "two fifths": 0.4, "2/5": 0.4,
    "three fifths": 0.6, "3/5": 0.6,
    "four fifths": 0.8, "4/5": 0.8,
}

ALL_KEYWORDS: frozenset[str] = frozenset({
    "all", "everything", "entire", "max", "full", "total",
})

REST_KEYWORDS: frozenset[str] = frozenset({
    "rest", "remaining", "remainder", "leftover",
    "what's left", "whats left", "the rest",
})

# ── Numeric emoji map ───────────────────────────────────────────────────────
#
# Lets users type amounts as emoji -- e.g. ",dice 💯" for 100 or
# ",dice 1️⃣0️⃣0️⃣" for 100. Keycap digits are 3-codepoint sequences
# (digit + U+FE0F + U+20E3); we translate the full sequence so leftover
# variation selectors don't break downstream regexes. Unknown emojis are
# left intact so the normal numeric parse fails with the usual error.

_EMOJI_NUMERIC_MAP: dict[str, str] = {
    "0️⃣": "0",  # 0-keycap
    "1️⃣": "1",  # 1-keycap
    "2️⃣": "2",  # 2-keycap
    "3️⃣": "3",  # 3-keycap
    "4️⃣": "4",  # 4-keycap
    "5️⃣": "5",  # 5-keycap
    "6️⃣": "6",  # 6-keycap
    "7️⃣": "7",  # 7-keycap
    "8️⃣": "8",  # 8-keycap
    "9️⃣": "9",  # 9-keycap
    "\U0001f51f": "10",    # keycap-ten
    "\U0001f4af": "100",   # hundred-points
}

# Zero-width modifiers that may linger after partial emoji input. Stripping
# them before parsing keeps stray VS16 / ZWJ / keycap glyphs from breaking
# the numeric regexes.
_ZW_MODIFIERS = ("️", "︎", "‍", "⃣")


def translate_emoji_amount(text: str) -> str:
    """Translate numeric emojis in an amount string to their digit form.

    Recognised: 0️⃣-9️⃣, 🔟, 💯. Unknown emojis are passed through unchanged
    and will fail the downstream numeric parse, which callers surface as the
    normal "amount must be a number" error -- so this is purely additive and
    safe for any input.
    """
    if not text or text.isascii():
        return text
    for emoji, digits in _EMOJI_NUMERIC_MAP.items():
        if emoji in text:
            text = text.replace(emoji, digits)
    for ch in _ZW_MODIFIERS:
        if ch in text:
            text = text.replace(ch, "")
    return text


# ── Suffix multiplier map ───────────────────────────────────────────────────

_SUFFIX_MAP: dict[str, int] = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

# ── Compiled regexes ────────────────────────────────────────────────────────

# Generic fraction regex: 1/2, 3/4, 7/8, etc.
_GENERIC_FRACTION_RE = re.compile(r"^(\d+)\s*/\s*(\d+)$")

# USD amount: $100, $1.5k, $1,000, 100 dollars, 100 usd, 50 bucks
_USD_RE = re.compile(
    r"^\$\s*([\d,.]+[kmb]?)"           # $100, $1.5k, $1,000
    r"|^([\d,.]+)\s*(?:dollars?|usd|bucks?)$",  # 100 dollars, 100 usd
    re.IGNORECASE,
)

# Plain number: 5, 100.5, 1,000
_NUMBER_RE = re.compile(r"^[\d,.]+$")

# Suffixed number: 1k, 2.5m, 100b
_SUFFIX_RE = re.compile(r"^([\d,.]+)\s*([kmb])$", re.IGNORECASE)


# ── Internal helpers ────────────────────────────────────────────────────────

def _clean_number(s: str) -> float:
    """Remove commas and convert to float.  '1,000.5' -> 1000.5"""
    return float(s.replace(",", ""))


def _apply_suffix(base: str, suffix: str) -> float:
    """Apply a k/m/b suffix to a numeric string."""
    return _clean_number(base) * _SUFFIX_MAP[suffix.lower()]


def _parse_usd_value(raw_match: str) -> float:
    """Parse the numeric part of a USD expression, handling optional k/m/b suffix."""
    raw_match = raw_match.strip()
    suffix_m = _SUFFIX_RE.match(raw_match)
    if suffix_m:
        return _apply_suffix(suffix_m.group(1), suffix_m.group(2))
    return _clean_number(raw_match)


# ── Public API ──────────────────────────────────────────────────────────────

def parse_amount(text: str) -> AmountSpec:
    """Parse an amount expression from natural language.

    Handles, in priority order:
      1. Named fractions ("half", "three quarters", "a third")
      2. Generic fraction notation ("3/4", "7/8")
      3. "all"/"everything" keywords
      4. "rest"/"remaining" keywords
      5. USD amounts ("$100", "100 dollars", "$2.5k")
      6. Suffixed numbers ("1k", "2.5m")
      7. Plain numbers ("5", "100.5", "1,000")

    Returns an AmountSpec with the appropriate flags set.  For concrete
    values (USD, suffixed, plain numbers), ``resolved`` is populated
    immediately.  For dynamic expressions (all, rest, fraction), resolution
    is deferred to execution time via AmountResolver.
    """
    raw = text.strip()
    translated = translate_emoji_amount(raw)
    normalised = translated.lower().strip()

    # 1. Named fractions  -  check the lookup table first
    if normalised in FRACTION_MAP:
        return AmountSpec(
            raw=raw,
            is_fraction=True,
            fraction_value=FRACTION_MAP[normalised],
        )

    # 2. Generic fraction notation (e.g. "7/8", "3/16")
    frac_m = _GENERIC_FRACTION_RE.match(normalised)
    if frac_m:
        numerator = int(frac_m.group(1))
        denominator = int(frac_m.group(2))
        if denominator == 0:
            raise ValueError(f"Division by zero in fraction: {raw}")
        value = numerator / denominator
        # If >= 1 this is a concrete amount, not a fraction-of-balance
        if value < 1.0:
            return AmountSpec(
                raw=raw,
                is_fraction=True,
                fraction_value=value,
            )
        # Treat e.g. "5/2" = 2.5 as a resolved plain number
        return AmountSpec(raw=raw, resolved=value)

    # 3. "all" / "everything" keywords
    if normalised in ALL_KEYWORDS:
        return AmountSpec(raw=raw, is_all=True)

    # 4. "rest" / "remaining" keywords
    if normalised in REST_KEYWORDS:
        return AmountSpec(raw=raw, is_rest=True)

    # 5. USD amounts  -  "$100", "$1.5k", "100 dollars", "100 usd"
    usd_m = _USD_RE.match(normalised)
    if usd_m:
        # group(1) matches the $-prefixed form, group(2) matches the word form
        num_str = usd_m.group(1) or usd_m.group(2)
        return AmountSpec(
            raw=raw,
            resolved=_parse_usd_value(num_str),
            is_usd=True,
        )

    # 6. Suffixed numbers  -  "1k", "2.5m", "100b"
    suffix_m = _SUFFIX_RE.match(normalised)
    if suffix_m:
        return AmountSpec(
            raw=raw,
            resolved=_apply_suffix(suffix_m.group(1), suffix_m.group(2)),
        )

    # 7. Plain numbers  -  "5", "100.5", "1,000"
    if _NUMBER_RE.match(normalised):
        return AmountSpec(raw=raw, resolved=_clean_number(normalised))

    # Unable to parse  -  return the raw text with no resolution
    return AmountSpec(raw=raw)
