"""Amount resolution helpers shared across cogs.

Centralises "all" amount handling so every command that supports "all"
uses the same maths and never produces precision drift (the infamous
"you have 100 but need 100" bug).

The canonical pattern for any "all" path is:

    1. Fetch the raw balance as an integer (NUMERIC(36,0), already x10**18).
    2. If amount_str is "all": use the raw balance DIRECTLY. Do not round-trip
       through to_human -> to_raw (that loses precision).
    3. For numeric/"$X" amounts: parse via parse_user_amount() and convert to
       raw via to_raw() once at the boundary.
    4. Compare raw ints to raw ints. Only convert to_human at display time.

Use resolve_all_spend_raw() for "spend entire balance including fee" flows
(buy, savings deposit, etc.).  It solves cost + fee = balance using integer
math so the user's wallet is always emptied exactly, never over or under.
"""
from __future__ import annotations

from decimal import Decimal

from core.framework.scale import SCALE, to_raw


def resolve_all_spend(
    balance: float,
    fee_pct: float = 0.0,
    fee_min: float = 0.0,
    fee_max: float = float("inf"),
) -> tuple[float, float]:
    """Return (cost, fee) for spending an entire float balance including fees.

    Solves: cost + clamp(cost * fee_pct, fee_min, fee_max) = balance

    Two-pass iteration handles the non-linear clamp, and an explicit
    safety clamp ensures cost + fee <= balance even under float rounding.

    Prefer :func:`resolve_all_spend_raw` for new code so balance checks stay
    in exact integer space.
    """
    if balance <= 0:
        return 0.0, 0.0
    if fee_pct <= 0 and fee_min <= 0:
        return balance, 0.0

    est = balance / (1.0 + fee_pct) if fee_pct > 0 else balance
    fee = max(fee_min, min(fee_max, est * fee_pct))
    cost = max(0.0, balance - fee)

    fee = max(fee_min, min(fee_max, cost * fee_pct))
    cost = max(0.0, balance - fee)

    overshoot = cost + fee - balance
    if overshoot > 0:
        cost = max(0.0, cost - overshoot)

    return cost, fee


def resolve_all_spend_raw(
    balance_raw: int,
    fee_pct: float = 0.0,
    fee_min_raw: int = 0,
    fee_max_raw: int | None = None,
) -> tuple[int, int]:
    """Raw-integer variant of :func:`resolve_all_spend`.

    Returns ``(cost_raw, fee_raw)`` such that ``cost_raw + fee_raw == balance_raw``
    exactly when fees are zero, and ``cost_raw + fee_raw <= balance_raw`` (never
    exceeds) when fees are non-zero.  All arithmetic is done in Decimal / int
    space so there is no float round-trip.

    fee_pct is still a float (it's a config value and the absolute values of
    typical fees - 1%, 5% - fit in float precision without loss at the scale we
    care about).  fee_min_raw / fee_max_raw are raw ints.
    """
    if balance_raw <= 0:
        return 0, 0
    if fee_max_raw is None:
        fee_max_raw = balance_raw  # no cap beyond the balance itself

    if fee_pct <= 0 and fee_min_raw <= 0:
        return balance_raw, 0

    fee_pct_d = Decimal(str(fee_pct))

    def _clamp(f: int) -> int:
        return max(fee_min_raw, min(fee_max_raw, f))

    # First pass: estimate ignoring the clamp
    if fee_pct > 0:
        est_cost = int(Decimal(balance_raw) / (Decimal(1) + fee_pct_d))
    else:
        est_cost = balance_raw
    est_fee = int(Decimal(est_cost) * fee_pct_d)
    fee_raw = _clamp(est_fee)
    cost_raw = max(0, balance_raw - fee_raw)

    # Second pass: refine using the actual (clamped) fee
    fee_raw = _clamp(int(Decimal(cost_raw) * fee_pct_d))
    cost_raw = max(0, balance_raw - fee_raw)

    # Hard clamp: never exceed balance
    if cost_raw + fee_raw > balance_raw:
        cost_raw = max(0, balance_raw - fee_raw)

    return cost_raw, fee_raw


def parse_user_amount(
    raw: str,
    balance_raw: int = 0,
    price_usd: float | None = None,
) -> tuple[int, bool, bool]:
    """Parse a user-supplied amount string into a raw-scaled integer.

    Handles every amount form the bot accepts in a single place:
      * ``"all"`` / ``"everything"`` / ``"max"`` / ``"full"``: returns
        ``balance_raw`` unchanged and sets ``is_all=True``.
      * ``"$100"`` / ``"$1.5k"``: USD amount. If ``price_usd`` is given, the
        quantity is ``usd / price_usd``; otherwise the USD value itself is
        returned (for USD-native flows).  ``is_usd=True``.
      * ``"1k"`` / ``"2.5m"`` / ``"100b"``: suffixed number.
      * Plain numbers with optional commas (``"1,234.56"``).
      * Named fractions and ``N/M`` fractions resolve against ``balance_raw``
        (``"half"`` -> ``balance_raw // 2``).

    Returns ``(amount_raw, is_all, is_usd)``.  Raises ``ValueError`` if the
    string can't be parsed.  The caller should clamp ``amount_raw`` against
    the relevant balance themselves for numeric inputs  -  only ``is_all`` is
    guaranteed to equal the balance exactly.
    """
    import re

    from core.framework.amount_parser import ALL_KEYWORDS, FRACTION_MAP, REST_KEYWORDS

    _frac_re = re.compile(r"^(\d+)\s*/\s*(\d+)$")

    text = (raw or "").strip()
    if not text:
        raise ValueError("Empty amount.")
    lower = text.lower()

    if lower in ALL_KEYWORDS or lower in REST_KEYWORDS:
        return balance_raw, True, False

    if lower in FRACTION_MAP:
        frac = FRACTION_MAP[lower]
        return int(Decimal(balance_raw) * Decimal(str(frac))), False, False

    m = _frac_re.match(lower)
    if m:
        num = int(m.group(1))
        den = int(m.group(2))
        if den == 0:
            raise ValueError("Division by zero in fraction.")
        frac = Decimal(num) / Decimal(den)
        if frac < 1:
            return int(Decimal(balance_raw) * frac), False, False
        return to_raw(float(frac)), False, False

    usd_mode = text.startswith("$")
    body = text.lstrip("$").replace(",", "").strip()

    if not usd_mode and body.lower().endswith(("dollars", "dollar", "usd", "bucks", "buck")):
        usd_mode = True
        for tail in ("dollars", "dollar", "bucks", "buck", "usd"):
            if body.lower().endswith(tail):
                body = body[: -len(tail)].strip()
                break

    suffix_mult = 1
    if body and body[-1].lower() in ("k", "m", "b"):
        suffix_mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[body[-1].lower()]
        body = body[:-1]

    try:
        value = Decimal(body) * suffix_mult
    except Exception as exc:
        raise ValueError(f"Not a valid amount: {raw!r}") from exc

    if usd_mode and price_usd is not None and price_usd > 0:
        qty = value / Decimal(str(price_usd))
        return int(qty * Decimal(SCALE)), False, True

    return int(value * Decimal(SCALE)), False, usd_mode


def is_all_token(text: str) -> bool:
    """Return True when *text* is an "everything" keyword.

    Centralises the check so no cog forgets a synonym.  Mirrors
    :data:`core.framework.amount_parser.ALL_KEYWORDS` plus ``everything``.
    """
    from core.framework.amount_parser import ALL_KEYWORDS, REST_KEYWORDS

    if not text:
        return False
    return text.strip().lower() in (ALL_KEYWORDS | REST_KEYWORDS)
