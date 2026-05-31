"""
services/rate_model.py  -  Vantor V2-style utilization kink interest rate model.

Rate math is kept in a standalone module so both lending.py and savings.py
can import it without circular dependencies.

The model:
  utilization = total_borrowed / total_deposits          (0.0  -  1.0)

  Below the optimal utilization (kink):
    borrow_rate_daily = base_rate + slope1 * (utilization / optimal_utilization)

  Above the optimal utilization (steep slope):
    borrow_rate_daily = base_rate + slope1
                      + slope2 * ((utilization - optimal) / (1 - optimal))

  savings_rate_daily = borrow_rate_daily * utilization * (1 - reserve_factor)

Default calibration (Config.SAVINGS_RATE_MODEL):
  - 0 % util  → borrow 0.05 %/day,  savings  0.0165 %/day (floor)
  - 50% util  → borrow 0.14 %/day,  savings  0.0606 %/day
  - 80% util  → borrow 0.20 %/day,  savings  0.1360 %/day (kink)
  - 90% util  → borrow 0.95 %/day,  savings  0.7268 %/day
  - 100% util → borrow 1.70 %/day,  savings  1.4450 %/day (extreme)
"""
from __future__ import annotations

from core.config import Config

_M = Config.SAVINGS_RATE_MODEL


def compute_rates(
    total_deposits: float,
    total_borrowed: float,
    *,
    model: dict | None = None,
) -> tuple[float, float, float]:
    """Return (borrow_rate_daily, savings_rate_daily, utilization).

    When there are no deposits the protocol has no pool to draw from; fall back
    to the legacy fixed rate (Config.LENDING.DAILY_RATE = 2 %/day) and 0 %
    savings so existing loans aren't disrupted.
    """
    m = model or _M

    if total_deposits <= 0:
        # No savings pool exists yet  -  use the legacy fixed borrowing rate
        legacy = Config.LENDING["DAILY_RATE"]
        return legacy, 0.0, 0.0

    utilization = min(total_borrowed / total_deposits, 1.0)
    opt = m["optimal_utilization"]

    if utilization <= opt:
        borrow_daily = m["base_rate"] + m["slope1"] * (utilization / opt)
    else:
        excess = (utilization - opt) / max(1.0 - opt, 1e-9)
        borrow_daily = m["base_rate"] + m["slope1"] + m["slope2"] * excess

    savings_daily = max(
        m.get("base_savings_rate", 0.0),
        borrow_daily * utilization * (1.0 - m["reserve_factor"]),
    )

    return borrow_daily, savings_daily, utilization


def utilization_str(utilization: float) -> str:
    """Human-readable utilization label."""
    if utilization >= 0.95:
        return f"🔴 {utilization:.1%}"
    if utilization >= 0.80:
        return f"🟡 {utilization:.1%}"
    return f"🟢 {utilization:.1%}"
