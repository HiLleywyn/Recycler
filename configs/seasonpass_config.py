"""Season season pass config - XP sources + tier rewards.

The pass runs in lockstep with an active season. Bus events grant XP
(see ``XP_EVENTS``); tiers are crossed at fixed XP thresholds derived
from ``TIER_XP_COST``; each tier has a USD reward pulled from
``tier_reward()``.

No separate catalog table: the DB only stores per-user XP and the
per-tier claim ledger. Editing this file changes the pass everywhere.
"""
from __future__ import annotations

# XP cost to reach each tier. Linear so the curve is easy to reason
# about: hitting tier N requires ``N * TIER_XP_COST`` cumulative XP.
TIER_XP_COST: int = 1000

# Maximum claimable tier. Beyond this, extra XP accumulates but grants
# no new rewards (used for leaderboard bragging rights).
MAX_TIER: int = 30

# Per-event XP grants. Keys are bus-event labels; values are XP per
# event. Keep values small -- the dominant retention signal is the
# frequency of activity, not the per-event size.
XP_EVENTS: dict[str, int] = {
    "work_completed":      20,
    "daily_claimed":      100,
    "trade":               10,
    "trade_executed":      10,   # HTTP-API path
    "swap_trade":          15,
    "swap_executed":       15,   # HTTP-API path
    "block_mined":         15,   # fanned out from pow_mining_tick payouts
    "staked":              30,
    "lp_added":            50,
    "deposit":             10,
    "gamble_play":          5,   # small so spamming the slots isn't the meta
    "gamble_win":          15,
    "exploit_run":         10,
    "exploit_win":         40,
    "buddy_adopted":       75,
    "buddy_battle_win":    25,
    "validator_registered": 150,
    "drop_claimed":        10,
    "fish_caught":          5,
    "fish_legendary":      75,
    "fish_buddy_egg":     100,
    # Lure Network economy events. Worth slightly more than a basic
    # catch since each one represents an active token decision (not just
    # spamming ,fish), but capped so cycling swap/stake/cashout cannot
    # outrun the catch loop as the dominant XP source.
    "fish_lure_swap":      15,
    "fish_lure_stake":     20,
    "fish_reel_cashout":   25,
    # Wild-buddy battles. Spawning is the player's reward for fishing in
    # deep water; winning is a real PvE accomplishment so it pays a lot;
    # capturing is the rarest outcome and gets the biggest XP grant.
    "fish_wild_battle_spawn":   20,
    "fish_wild_battle_won":     60,
    "fish_wild_battle_lost":    10,
    "fish_wild_buddy_captured": 200,
}


def tier_for_xp(xp: int) -> int:
    """Return the tier number earned for ``xp`` cumulative pass XP.

    Capped at ``MAX_TIER``. Tier 0 means "no tier unlocked yet".
    """
    if xp <= 0 or TIER_XP_COST <= 0:
        return 0
    return min(MAX_TIER, xp // TIER_XP_COST)


def xp_for_tier(tier: int) -> int:
    """Cumulative XP required to reach ``tier``. ``tier=0`` returns 0."""
    return max(0, int(tier) * TIER_XP_COST)


def tier_reward(tier: int) -> float:
    """USD reward for crossing ``tier``.

    Simple schedule: base of $100 at tier 1, +$50 per tier, with a small
    bonus every 5 tiers (+$250) and a bigger milestone at every 10
    (+$1000). The final tier (30) pays an extra $5000 capstone.
    """
    if tier < 1 or tier > MAX_TIER:
        return 0.0
    reward = 100.0 + (tier - 1) * 50.0
    if tier % 5 == 0:
        reward += 250.0
    if tier % 10 == 0:
        reward += 1000.0
    if tier == MAX_TIER:
        reward += 5000.0
    return reward


def total_pool() -> float:
    """Sum of every tier reward. Useful for showcase + season intro embeds."""
    return sum(tier_reward(t) for t in range(1, MAX_TIER + 1))


# ── Themes ───────────────────────────────────────────────────────────────────
#
# A theme flips per-event XP multipliers for the whole season. The keys of
# each theme's dict match bus-event names (the same strings XP_EVENTS uses).
# Unlisted events use the default 1.0x.
#
# Add a theme: drop a new entry in THEMES. Admins pick one via
# ,season theme <name>. The values are stored on seasons.xp_multipliers
# (JSONB), so the active season just reads the dict directly.

THEMES: dict[str, dict[str, float]] = {
    # No boosts. Seasons start as 'classic' by default.
    "classic": {},

    # Mining-focused boost. Both payout paths are boosted so a player
    # grinding on any chain sees the multiplier.
    "mining_madness": {
        "block_mined": 3.0,
    },

    # Trading + swap-focused. Covers Discord and API-sourced trades.
    "trading_frenzy": {
        "trade":          2.5,
        "trade_executed": 2.5,
        "swap_trade":     2.5,
        "swap_executed":  2.5,
    },

    # Buddy-focused. Pairs well with a buddy_wins-metric season.
    "buddy_brawls": {
        "buddy_battle_win": 3.0,
        "buddy_adopted":    2.0,
    },

    # Gambling + exploit risk-takers.
    "risk_takers": {
        "gamble_play": 2.0,
        "gamble_win":  3.0,
        "exploit_run": 2.0,
        "exploit_win": 3.0,
    },

    # DeFi-heavy: staking, LP, savings.
    "yield_szn": {
        "lp_added":  2.5,
        "staked":    2.5,
        "deposit":   2.0,
        "validator_registered": 2.0,
    },

    # Everything x1.5. A "sprint" theme for shorter seasons.
    "double_up": {
        k: 1.5 for k in XP_EVENTS
    },

    # Fishing-focused. Pairs well with rare-fish leaderboards.
    "fishing_frenzy": {
        "fish_caught":     2.5,
        "fish_legendary":  3.0,
        "fish_buddy_egg":  2.0,
    },

    # Lure-Network theme. Boosts the swap / stake / cashout loop so a
    # season can deliberately push players to engage with the economy
    # rather than just spamming casts.
    "tide_season": {
        "fish_caught":        1.5,
        "fish_legendary":     2.0,
        "fish_buddy_egg":     1.5,
        "fish_lure_swap":     3.0,
        "fish_lure_stake":    3.0,
        "fish_reel_cashout":  3.0,
    },

    # Bestiary-themed: amplifies the wild-battle loop without buffing
    # vanilla casts, so a season can deliberately push players into the
    # fight-the-aquatic-buddy meta.
    "wild_season": {
        "fish_caught":              1.2,
        "fish_wild_battle_spawn":   3.0,
        "fish_wild_battle_won":     4.0,
        "fish_wild_buddy_captured": 5.0,
    },
}


def theme_names() -> list[str]:
    """Stable sorted list of theme keys for help text + validation."""
    return sorted(THEMES.keys())


def theme_multipliers(name: str) -> dict[str, float]:
    """Return a fresh copy of the multiplier map for ``name``.

    Unknown themes return an empty dict (classic).
    """
    return dict(THEMES.get(name, {}))


def theme_summary(name: str) -> str:
    """One-line human description of a theme's boosted events."""
    m = THEMES.get(name, {})
    if not m:
        return "Standard XP, no boosts."
    parts = []
    # Group events by multiplier so "x2.5 on trade/swap" collapses neatly.
    by_mult: dict[float, list[str]] = {}
    for ev, mult in m.items():
        by_mult.setdefault(float(mult), []).append(ev)
    for mult in sorted(by_mult.keys(), reverse=True):
        evs = ", ".join(sorted(by_mult[mult]))
        parts.append(f"{mult:.1f}x on {evs}")
    return "; ".join(parts)
