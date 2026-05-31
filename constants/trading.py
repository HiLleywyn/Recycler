"""
Trading constants  -  swap fees, slippage, price impact, precision.
"""
from __future__ import annotations

DEFAULT_SWAP_FEE: float = 0.01
PLATFORM_FEE_RATIO: float = 0.1
SWAP_PLATFORM_FEE_PCT: float = 0.001   # 0.1% of swap value (in gas coin terms)
ARB_FEE: float = 0.003
SLIPPAGE_WARN: float = 0.15
PRICE_FLOOR: float = 0.001
PRICE_IMPACT_DIVISOR: float = 2_500_000.0
DEFAULT_FEE_PCT: float = 0.005
DEFAULT_FEE_MIN: float = 0.01
DEFAULT_FEE_MAX: float = 500.0
USD_PRECISION: int = 2
TOKEN_PRECISION: int = 8
MIN_TRADE_USD: float = 0.01
QUOTE_EXPIRY_SECS: int = 5
