"""
Network vault constants  -  level thresholds, fee rates, display config.

Each network has a vault that accumulates a small cut of transaction fees.
When the vault balance crosses a threshold, the server gains a level for
that network.  Levels are server-wide progression checkpoints.
"""
from __future__ import annotations

# Fraction of the treasury_cut_pct that goes to network vaults.
# e.g. if treasury_cut_pct = 0.10 (10%), vault gets 0.05 * 0.10 = 0.005 (0.5%).
VAULT_FEE_FRACTION: float = 0.05

# Fraction of raw trade volume (in USD) credited to the relevant network vault.
# 0.1% of every buy/sell/swap goes toward that network's progression.
VAULT_VOLUME_FRACTION: float = 0.001

# Maps token network names (from Config.TOKENS["network"]) to vault keys.
NETWORK_TO_VAULT: dict[str, str] = {
    "Sun Network":      "sun",
    "Moneta Chain":  "mta",
    "Arcadia Network": "arc",
    "Discoin Network":  "dsc",
    "Moon Network":     "moon",
}

# Level thresholds per network (cumulative USD value in vault).
# Level N requires LEVEL_THRESHOLDS[network][N-1] total USD.
# Once balance >= threshold, server gains that level.

# SUN gets heavy shop fee deposits so it needs higher thresholds.
# ARC/MTA/DSC rely mainly on trade volume (0.1%) so lower thresholds.

_SUN_THRESHOLDS: list[float] = [
    10.0,        # Level 1
    50.0,        # Level 2
    150.0,       # Level 3
    400.0,       # Level 4
    1_000.0,     # Level 5
    2_500.0,     # Level 6
    5_000.0,     # Level 7
    10_000.0,    # Level 8
    25_000.0,    # Level 9
    50_000.0,    # Level 10
    100_000.0,   # Level 11
    250_000.0,   # Level 12
    500_000.0,   # Level 13
    1_000_000.0, # Level 14
    5_000_000.0, # Level 15
]

_DEFI_THRESHOLDS: list[float] = [
    0.50,        # Level 1
    2.0,         # Level 2
    8.0,         # Level 3
    25.0,        # Level 4
    75.0,        # Level 5
    200.0,       # Level 6
    500.0,       # Level 7
    1_500.0,     # Level 8
    4_000.0,     # Level 9
    10_000.0,    # Level 10
    25_000.0,    # Level 11
    60_000.0,    # Level 12
    150_000.0,   # Level 13
    400_000.0,   # Level 14
    1_000_000.0, # Level 15
]

LEVEL_THRESHOLDS: dict[str, list[float]] = {
    "sun":  _SUN_THRESHOLDS,
    "mta":  _DEFI_THRESHOLDS,
    "arc":  _DEFI_THRESHOLDS,
    "dsc":  _DEFI_THRESHOLDS,
    "moon": _DEFI_THRESHOLDS,
}

MAX_LEVEL: int = len(_SUN_THRESHOLDS)

# Display config per network
VAULT_DISPLAY: dict[str, dict] = {
    "sun":  {"name": "Sun Network",     "emoji": "\u2600\ufe0f", "color": 0xFFAA00},
    "mta":  {"name": "Moneta Chain",  "emoji": "🟡",       "color": 0xF7931A},
    "arc":  {"name": "Arcadia Network", "emoji": "🔵",       "color": 0x627EEA},
    "dsc":  {"name": "Discoin Network",  "emoji": "\U0001fa99",   "color": 0x5865F2},
    "moon": {"name": "Moon Network",     "emoji": "\U0001f315",   "color": 0x9B59B6},
}

ALL_VAULT_NETWORKS: tuple[str, ...] = ("sun", "mta", "arc", "dsc", "moon")


def level_for_balance(network: str, balance: float) -> int:
    """Return the level achieved for a given vault balance."""
    thresholds = LEVEL_THRESHOLDS.get(network, _SUN_THRESHOLDS)
    level = 0
    for threshold in thresholds:
        if balance >= threshold:
            level += 1
        else:
            break
    return level


def next_threshold(network: str, current_level: int) -> float | None:
    """Return the USD threshold for the next level, or None if maxed."""
    thresholds = LEVEL_THRESHOLDS.get(network, _SUN_THRESHOLDS)
    if current_level >= len(thresholds):
        return None
    return thresholds[current_level]


def progress_pct(network: str, balance: float, current_level: int) -> float:
    """Return 0.0 - 1.0 progress toward the next level."""
    nxt = next_threshold(network, current_level)
    if nxt is None:
        return 1.0
    prev = LEVEL_THRESHOLDS.get(network, _SUN_THRESHOLDS)[current_level - 1] if current_level > 0 else 0.0
    span = nxt - prev
    if span <= 0:
        return 1.0
    return min(1.0, max(0.0, (balance - prev) / span))
