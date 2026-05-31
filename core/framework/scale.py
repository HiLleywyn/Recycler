"""
core/framework/scale.py  -  Scaled-integer arithmetic for Discoin.

All monetary amounts are stored and calculated as raw integers (x10^18).
Only convert to human-readable decimal at the UI display boundary.

    SCALE = 10**18

    # Arithmetic: always in raw int space
    fee_raw = amount_raw * fee_num // fee_den   # exact, no float error
    out_raw = amount_raw - fee_raw

    # Display only:
    fmt_usd(to_human(wallet_raw))
    fmt_token(to_human(amount_raw), "ARC")
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN

SCALE: int = 10 ** 18
"""Canonical scale factor: 1 human unit = SCALE raw units."""

_SCALE_D: Decimal = Decimal(SCALE)
_SCALE_F: float = float(SCALE)


def to_raw(human: float | int | str) -> int:
    """Convert a human-readable amount to a raw scaled integer.

    Accepts float, int, or numeric string.  Uses Decimal arithmetic to
    avoid IEEE-754 float precision loss (e.g. ``0.1 * 10**18`` would be
    off by 1 using plain float).  Float inputs are converted via ``str``
    first so that e.g. ``to_raw(0.1)`` gives the same result as
    ``to_raw("0.1")``.

    Examples::

        to_raw(1)       -> 1_000_000_000_000_000_000
        to_raw(0.5)     -> 500_000_000_000_000_000
        to_raw("1.5")   -> 1_500_000_000_000_000_000
        to_raw(0)       -> 0
    """
    if isinstance(human, int):
        return human * SCALE
    if isinstance(human, str):
        human = human.replace(",", "").strip()
        if not human:
            return 0
    return int((Decimal(str(human)) * _SCALE_D).to_integral_value(rounding=ROUND_HALF_EVEN))


def to_human(raw: int | float) -> float:
    """Convert a raw scaled integer to a human-readable float.

    This is ONLY for display/UI.  All arithmetic must stay in raw int space.

    Examples::

        to_human(1_000_000_000_000_000_000)  -> 1.0
        to_human(500_000_000_000_000_000)    -> 0.5
        to_human(0)                          -> 0.0
    """
    return int(raw) / _SCALE_F


def require_raw(value, field: str = "amount") -> int:
    """Assert *value* is a raw-scaled int and return it unchanged.

    Use at the boundary of any DB mutation that writes to a raw
    ``NUMERIC(36,0)`` balance column (wallet, bank, holdings, savings,
    stakes, etc).  Accepting a ``float`` here is almost always a scaling
    bug: ``50.0`` would be stored as raw ``50`` (= ``$0.00...05`` after
    ``to_human``) instead of ``50 * 10**18``.

    ``bool`` is rejected explicitly because ``isinstance(True, int)`` is
    ``True`` in Python and truthy flags accidentally passed as amounts
    would otherwise sneak through.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{field} must be a raw-scaled int (e.g. to_raw(50)), "
            f"got {type(value).__name__}={value!r}. "
            "Call to_raw() at the service/API boundary before writing to "
            "raw NUMERIC(36,0) columns."
        )
    return value
