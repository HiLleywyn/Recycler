"""
core/framework/delay_parser.py  -  Time delay parser for scheduled command chains.

Extracts phrases like "in 5 minutes", "after 30 seconds", "wait 2 days"
from natural language text.  Returns the delay in seconds and the input
text with the delay phrase removed.

Usage:
    from core.framework.delay_parser import parse_delay

    delay, cleaned = parse_delay("buy 100 MTA in 5 minutes")
    assert delay == 300.0
    assert cleaned == "buy 100 MTA"
"""
from __future__ import annotations

import re

MAX_DELAY: int = 7 * 24 * 3600  # 1 week in seconds

TIME_UNITS: dict[str, int] = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
}

# Build a regex alternation from longest unit names first so that
# "minutes" matches before "min" or "m".
_UNIT_PATTERN = "|".join(
    re.escape(u) for u in sorted(TIME_UNITS, key=len, reverse=True)
)

# Matches patterns like:
#   "in 5 minutes"
#   "after 30 seconds"
#   "wait 2 days"
#   "in 1.5 hours"
#   "after 0.5h"
_DELAY_RE = re.compile(
    rf"(?:^|\s)"                          # start of string or whitespace
    rf"(?:in|after|wait)\s+"              # trigger word
    rf"(\d+(?:\.\d+)?)\s*"               # number (int or float)
    rf"({_UNIT_PATTERN})"                 # time unit
    rf"(?:\s|$)",                          # end of string or whitespace
    re.IGNORECASE,
)

# Secondary pattern: bare trailing "5m", "30s", "2h" at end of input
_BARE_DELAY_RE = re.compile(
    rf"(?:^|\s)"
    rf"(\d+(?:\.\d+)?)\s*"
    rf"({_UNIT_PATTERN})"
    rf"\s*$",
    re.IGNORECASE,
)


def parse_delay(text: str) -> tuple[float, str]:
    """Extract a time delay from *text*.

    Scans for phrases like "in 5 minutes", "after 30 seconds", or a bare
    trailing duration like "2h".  If found, returns the delay in seconds
    (capped at :data:`MAX_DELAY`) and the text with the delay phrase
    stripped out and whitespace normalised.

    Returns ``(0.0, text)`` unchanged if no delay phrase is detected.
    """
    match = _DELAY_RE.search(text)
    if not match:
        # Try the bare trailing pattern as a fallback
        match = _BARE_DELAY_RE.search(text)

    if not match:
        return 0.0, text

    value = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = TIME_UNITS.get(unit, 0)

    delay = value * multiplier
    delay = min(delay, float(MAX_DELAY))

    # Remove the matched span from the text and normalise whitespace
    cleaned = text[: match.start()] + " " + text[match.end() :]
    cleaned = " ".join(cleaned.split()).strip()

    return delay, cleaned
