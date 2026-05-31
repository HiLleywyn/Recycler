"""
Game constants  -  mines, slots, anti-bot thresholds.
"""
from __future__ import annotations

MINES_TOTAL_TILES: int = 24
MINES_DEFAULT_BOMBS: int = 3
MINES_MIN_BOMBS: int = 1
MINES_MAX_BOMBS: int = 20
MINES_HOUSE_EDGE: float = 0.05
MINES_TIMEOUT_SECS: float = 120.0
SLOT_THREE_OF_A_KIND_PAYOUT: float = 5.0
SLOT_PAIR_PAYOUT: float = 1.5
CROSS_GAME_WINDOW: int = 300
CROSS_GAME_LIMIT: int = 40

# ── Coinflip streak mode ─────────────────────────────────────────────────────
CF_STREAK_MIN: int = 2
CF_STREAK_MAX: int = 10

# ── Coinflip double-or-nothing mode ──────────────────────────────────────────
CF_DON_MAX_ROUNDS: int = 10
CF_DON_TIMEOUT: float = 30.0

# ── Dice over/under/range mode ───────────────────────────────────────────────
DICE_ROLL_SIZE: int = 100
DICE_OVER_MIN: int = 2
DICE_OVER_MAX: int = 98
DICE_UNDER_MIN: int = 3
DICE_UNDER_MAX: int = 99
DICE_RANGE_MIN_SIZE: int = 1
DICE_RANGE_MAX_SIZE: int = 98

# ── Coinflip trio mode (3 coins, predict exact pattern) ──────────────────────
CF_TRIO_COUNT: int = 3

# ── Coinflip rainbow mode (5 coins, predict heads count) ─────────────────────
CF_RAINBOW_COUNT: int = 5
CF_RAINBOW_PICK_MIN: int = 0
CF_RAINBOW_PICK_MAX: int = 5

# ── Dice exact mode (pick one number out of 100) ─────────────────────────────
DICE_EXACT_MIN: int = 1
DICE_EXACT_MAX: int = 100

# ── Dice ladder mode (N strictly-ascending rolls) ────────────────────────────
DICE_LADDER_MIN: int = 2
DICE_LADDER_MAX: int = 5

# ── Animation timing (seconds per frame) ─────────────────────────────────────
GAME_ANIM_FRAME_DELAY: float = 0.25
GAME_ANIM_STEP_DELAY: float = 0.35
