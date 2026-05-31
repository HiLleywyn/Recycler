"""Shared arg-parser for ``$`` commands.

The dispatcher hands every handler the raw arg string. This helper
peels off the symbol, an optional timeframe, the ``ai`` modifier
sentinel, and any leftover flags so handlers don't all reinvent the same
parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from services.market.timeframes import canonical_tf


@dataclass(slots=True)
class DollarArgs:
    symbol: str = ""
    timeframe: str | None = None   # canonical code or None
    ai: bool = False
    flags: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    raw: str = ""


def parse_dollar_args(
    raw: str,
    *,
    known_flags: Iterable[str] = (),
) -> DollarArgs:
    """Parse ``$cmd SYMBOL [TIMEFRAME] [ai] [flags...]``.

    - First non-flag token is the symbol.
    - First token after the symbol that matches :func:`canonical_tf` is
      the timeframe.
    - The literal ``ai`` token anywhere flips ``ai=True``.
    - Tokens in ``known_flags`` go to ``.flags``; the rest go to
      ``.extra``.
    """
    out = DollarArgs(raw=(raw or "").strip())
    tokens = [t for t in (raw or "").split() if t]
    if not tokens:
        return out

    known = {f.lower() for f in known_flags}

    # Symbol = first non-flag non-ai non-tf token.
    consumed_symbol = False
    for idx, tok in enumerate(tokens):
        if not consumed_symbol:
            if tok.lower() == "ai":
                out.ai = True
                continue
            if canonical_tf(tok):
                # Timeframe before symbol -- unusual but supported.
                out.timeframe = canonical_tf(tok)
                continue
            out.symbol = tok
            consumed_symbol = True
            continue
        # Past the symbol now: timeframe / ai / flags / extras.
        low = tok.lower()
        if low == "ai":
            out.ai = True
            continue
        if out.timeframe is None:
            tf = canonical_tf(tok)
            if tf is not None:
                out.timeframe = tf
                continue
        if low in known:
            out.flags.append(low)
        else:
            out.extra.append(tok)
    return out
