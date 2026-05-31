"""Canonical network-key normalization for Discoin.

Networks in Discoin have two spellings:

- **Full name**, e.g. ``"Arcadia Network"`` or ``"Discoin Network"``. This is
  the form stored in ``Config.TOKENS[sym]["network"]``, ``Config.NETWORK_STABLECOIN``,
  validator rows, and any user-facing embed text.
- **Short code**, e.g. ``"arc"`` or ``"dsc"``. This is the form stored in
  ``wallet_addresses`` / ``wallet_holdings`` / ``transactions`` as the
  lowercase ``network`` column and tx hash prefix.

Mixing the two or hardcoding the wrong key has already caused at least one
production bug (see migration ``0050_fix_discoin_network_key_and_rescale_staked``
which had to UPDATE every ``wallet_holdings`` / ``transactions`` row where the
``network`` column had been written as ``"discoin"`` instead of ``"dsc"``). The
rule codified in ``CLAUDE.md`` is "``_STABLE_NETWORK["DSD"]`` must resolve to
``"dsc"`` (use ``_NET_NORMALIZE``)". This module is the single source of truth
for those helpers  -  every cog and service that needs to normalize a network
reference should import from here instead of rolling its own table.

Usage::

    from core.framework.network import (
        FULL_TO_SHORT,       # {"Arcadia Network": "arc", ...}
        SHORT_TO_FULL,       # {"arc": "Arcadia Network", ...}
        STABLE_NETWORK,      # {"USDC": "arc", "DSD": "dsc", ...}
        normalize_short,     # "Arcadia Network" / "arc" / "arcadia" → "arc"
        normalize_full,      # "arc" / "arcadia" / "Arcadia Network" → "Arcadia Network"
        stable_display,      # "`DSD`, `USDC`"
        stable_emoji,        # "💵"
    )
"""
from __future__ import annotations

from constants.validators import NET_SHORT as _NET_SHORT_CANON


# ── Canonical maps ───────────────────────────────────────────────────────────

#: Full network name → short code. Pulled from :mod:`constants.validators` so
#: there is exactly one authoritative source. Do not define a second copy.
FULL_TO_SHORT: dict[str, str] = dict(_NET_SHORT_CANON)

#: Short code → full network name. Derived from :data:`FULL_TO_SHORT`.
SHORT_TO_FULL: dict[str, str] = {v: k for k, v in FULL_TO_SHORT.items()}

#: Alias table used by :func:`normalize_short` and :func:`normalize_full` to
#: accept user input in a variety of spellings. Keys are lowercased,
#: stripped. Values are the canonical short code.
#:
#: Only the short code and the lowercase prefix of the full name are needed;
#: the full canonical name is handled separately by :func:`normalize_short`.
_ALIAS_TO_SHORT: dict[str, str] = {
    "sun": "sun", "sun network": "sun",
    "mta": "mta", "moneta": "mta", "moneta chain": "mta",
    "arc": "arc", "arcadia": "arc", "arcadia network": "arc",
    "dsc": "dsc", "discoin": "dsc", "discoin network": "dsc",
    "moon": "moon", "moon network": "moon",
    # Legacy aliases from when this network was called "Group Network".
    "grp": "moon", "group": "moon", "group network": "moon",
    # Lure Network -- fishing-only earn economy.
    "lur": "lur", "lure": "lur", "lure network": "lur",
    # Crypt Network -- dungeon-only earn economy.
    "cry": "cry", "crypt": "cry", "crypt network": "cry",
    # Buddy Network -- companion economy.
    "bud": "bud", "buddy": "bud", "buddy network": "bud",
    # Harvest Network -- minigame economy.
    "har":              "har",
    "harvest":          "har",
    "harvest network":  "har",
    # Forge Network -- crafting economy (INGOT earn-only, FORGE coin, FGD stablecoin).
    "fge":              "fge",
    "forge":            "fge",
    "forge network":    "fge",
    # Gamba Network -- gambling economy (GBC coin + 8 game tokens, all earn-only).
    "gam":              "gam",
    "gamba":            "gam",
    "gamba network":    "gam",
    # Sage Network -- crypto learn-and-earn economy (SAGE coin + EDU game token).
    "sag":              "sag",
    "sage":             "sag",
    "sage network":     "sag",
}


def normalize_short(key: str | None) -> str:
    """Return the canonical short code for a network key.

    Accepts the short code (``"arc"``), the full network name
    (``"Arcadia Network"``), or any lowercase alias (``"arcadia"``,
    ``"moneta chain"``). Returns ``""`` for unknown / empty input so the
    caller can fall through without a ``KeyError``.
    """
    if not key:
        return ""
    s = str(key).strip()
    if not s:
        return ""
    # Fast path: exact full-name match.
    if s in FULL_TO_SHORT:
        return FULL_TO_SHORT[s]
    # Alias path (lowercase).
    return _ALIAS_TO_SHORT.get(s.lower(), "")


def normalize_full(key: str | None) -> str:
    """Return the canonical full network name for a network key.

    Accepts the short code (``"arc"``), the full network name
    (``"Arcadia Network"``), or any lowercase alias (``"arcadia"``,
    ``"moneta chain"``). Returns ``""`` for unknown / empty input.
    """
    short = normalize_short(key)
    return SHORT_TO_FULL.get(short, "")


# ── Stablecoin-per-network (derived from Config.TOKENS) ──────────────────────
# Built lazily on first access so we don't import ``config`` at module-import
# time (which would pull in the entire settings tree before anything else is
# ready). Tests can still patch ``Config.TOKENS`` without the cache going
# stale because every public helper calls ``_build_stable_network()`` if the
# module-level cache is empty.

_STABLE_NETWORK_CACHE: dict[str, str] = {}


def _build_stable_network() -> dict[str, str]:
    """Compute the stablecoin → short network-code mapping from Config.TOKENS.

    Example result::

        {"DSD": "dsc", "USDC": "arc"}

    The mapping is used by the shop and savings paths to decide which
    wallet-holdings row a payment lives in.
    """
    from core.config import Config  # local import to avoid cycles
    out: dict[str, str] = {}
    for sym, tok in Config.TOKENS.items():
        if not tok.get("stablecoin"):
            continue
        net_full = tok.get("network", "")
        short = normalize_short(net_full)
        if short:
            out[sym] = short
    return out


def _get_stable_network() -> dict[str, str]:
    global _STABLE_NETWORK_CACHE
    if not _STABLE_NETWORK_CACHE:
        _STABLE_NETWORK_CACHE = _build_stable_network()
    return _STABLE_NETWORK_CACHE


class _StableNetworkView:
    """Dict-like lazy proxy over :func:`_build_stable_network`.

    Exposed as :data:`STABLE_NETWORK` so callers can ``from core.framework.network
    import STABLE_NETWORK`` and use it like a plain dict (``[...]``, ``.get``,
    iteration, ``in``), without paying the ``Config`` import at module-load
    time.
    """
    __slots__ = ()

    def _m(self) -> dict[str, str]:
        return _get_stable_network()

    def __getitem__(self, key: str) -> str:
        return self._m()[key]

    def get(self, key: str, default=None):
        return self._m().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._m()

    def __iter__(self):
        return iter(self._m())

    def __len__(self) -> int:
        return len(self._m())

    def keys(self):
        return self._m().keys()

    def values(self):
        return self._m().values()

    def items(self):
        return self._m().items()


#: ``{stablecoin_symbol: short_network_code}`` for every token in
#: ``Config.TOKENS`` with ``stablecoin=True``. Lazily built on first access.
STABLE_NETWORK: _StableNetworkView = _StableNetworkView()


def stable_network_short(symbol: str) -> str:
    """Return the short network code that hosts ``symbol``, or ``""`` if
    ``symbol`` is not a configured stablecoin.
    """
    return _get_stable_network().get((symbol or "").upper(), "")


def stable_display() -> str:
    """Return a human-readable list of accepted stablecoins, sorted.

    Example::

        "`DSD`, `USDC`"
    """
    return ", ".join(f"`{s}`" for s in sorted(_get_stable_network()))


def stable_emoji(symbol: str) -> str:
    """Return the display emoji for a stablecoin symbol, or ``"💵"``."""
    from core.config import Config  # local import  -  same reason as above
    return Config.TOKENS.get(symbol, {}).get("emoji", "💵")
