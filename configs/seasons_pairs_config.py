"""seasons_pairs_config.py  -  themed pair templates for auto seasons + challenges.

Each pair bundles a ``Season`` (with theme, metric, default name) and a
list of ``Challenge`` templates that go with it. When the bot is in
auto-rotation mode (see ``guild_settings.auto_seasons_enabled``), the
next pair from this list is started in lockstep -- one season + N
challenges -- so the meta of the week is coherent across both surfaces.
The cursor ``guild_settings.auto_seasons_pair_idx`` walks the list mod
``len(PAIRS)`` so guilds keep cycling indefinitely.

Edit this file to add / remove / re-tune pairs. Keep each pair's
challenges thematically aligned with its season theme (a buddy-themed
season should ship buddy-themed challenges, not gambling ones) so a
player who engages with the season pass and the challenges sees the
same call-to-action twice.

Constraints:
    * Theme keys must exist in ``seasonpass_config.THEMES``.
    * Metric keys must be in ``services/seasons.METRICS``.
    * Challenge triggers must be in ``services/challenges.TRIGGERS``.
    * ``challenge_count`` is a sanity check -- the auto-rotation refuses
      to start a pair with the wrong number of challenge templates.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── Defaults ───────────────────────────────────────────────────────────────
# Used when a guild hasn't overridden ``auto_seasons_*`` columns.
DEFAULT_DURATION_DAYS: int = 7
DEFAULT_SEASON_POOL_USD: float = 10_000_000.0
# Per-challenge default; 5 challenges per pair -> $5,000,000 total
# challenge pool / pair when the admin hasn't set a per-guild override.
DEFAULT_CHALLENGE_POOL_USD: float = 1_000_000.0

# Hard guardrails so a config typo can't drain the treasury or schedule
# an "infinite" season. Pool ceiling is generous enough to fit the new
# $10M season default and any reasonable admin override on top.
MIN_DURATION_DAYS: int = 1
MAX_DURATION_DAYS: int = 30
MIN_POOL_USD: float = 0.0
MAX_POOL_USD: float = 100_000_000.0
CHALLENGES_PER_PAIR: int = 5


@dataclass(frozen=True)
class ChallengeTemplate:
    """One challenge inside a pair.

    ``target`` is the bot-wide goal (NOT per-user). ``pool_weight`` is
    the slice of the per-pair challenge pool this challenge gets,
    normalised across the pair so 5 weights of 1.0 each split the pool
    evenly. Set ``pool_weight=2.0`` to give one challenge double the
    pool of the others. ``trigger`` must be in ``services/challenges.TRIGGERS``.
    """
    name: str
    trigger: str
    target: int
    description: str = ""
    pool_weight: float = 1.0


@dataclass(frozen=True)
class SeasonTemplate:
    """One season inside a pair.

    ``metric`` must be in ``services/seasons.METRICS``. ``theme`` must
    be in ``seasonpass_config.THEMES``.
    """
    name: str
    theme: str
    metric: str = "net_worth"


@dataclass(frozen=True)
class Pair:
    """A themed (Season + N challenges) bundle. Shipped together by
    ``services/auto_seasons.start_next_pair``.
    """
    key: str
    season: SeasonTemplate
    challenges: list[ChallengeTemplate] = field(default_factory=list)


# ── Pair templates ─────────────────────────────────────────────────────────
# Order matters: ``auto_seasons_pair_idx`` walks this list, so the first
# pair is what brand-new guilds see when they flip auto-rotation on.

PAIRS: list[Pair] = [
    Pair(
        key="buddy_brawls",
        season=SeasonTemplate(
            name="Buddy Brawls Week",
            theme="buddy_brawls",
            metric="buddy_wins",
        ),
        challenges=[
            ChallengeTemplate(
                name="Battle Royale",
                trigger="buddy_battle_win",
                target=500,
                description="Win 500 buddy battles as a server.",
            ),
            ChallengeTemplate(
                name="Adopt the Pack",
                trigger="buddy_adopted",
                target=100,
                description="Adopt 100 new buddies across the guild.",
            ),
            ChallengeTemplate(
                name="Arena Spawns",
                trigger="buddy_arena_spawn",
                target=200,
                description="Trigger 200 arena fights server-wide.",
            ),
            ChallengeTemplate(
                name="Arena Champions",
                trigger="buddy_arena_won",
                target=150,
                description="Win 150 arena fights as a guild.",
            ),
            ChallengeTemplate(
                name="Wild Captures",
                trigger="fish_wild_buddy_captured",
                target=50,
                description="Capture 50 wild buddies via the fishing minigame.",
                pool_weight=2.0,
            ),
        ],
    ),
    Pair(
        key="trading_frenzy",
        season=SeasonTemplate(
            name="Trading Frenzy Week",
            theme="trading_frenzy",
            metric="volume",
        ),
        challenges=[
            ChallengeTemplate(
                name="Order Flow",
                trigger="trade_executed",
                target=2_000,
                description="Push 2,000 BUY/SELL trades through the market.",
            ),
            ChallengeTemplate(
                name="Swap City",
                trigger="swap_executed",
                target=1_000,
                description="Settle 1,000 AMM swaps across the guild.",
            ),
            ChallengeTemplate(
                name="LP Builders",
                trigger="lp_added",
                target=300,
                description="Open 300 new LP positions guild-wide.",
            ),
            ChallengeTemplate(
                name="Stones Levelled",
                trigger="stone_leveled",
                target=200,
                description="Level up 200 stones during the week.",
            ),
            ChallengeTemplate(
                name="Validator Push",
                trigger="validator_registered",
                target=20,
                description="Spin up 20 new validators across the guild.",
                pool_weight=2.0,
            ),
        ],
    ),
    Pair(
        key="mining_madness",
        season=SeasonTemplate(
            name="Mining Madness Week",
            theme="mining_madness",
            metric="pass_xp",
        ),
        challenges=[
            ChallengeTemplate(
                name="Block Bonanza",
                trigger="block_mined",
                target=5_000,
                description="Mine 5,000 blocks as a server.",
                pool_weight=2.0,
            ),
            ChallengeTemplate(
                name="Stake Up",
                trigger="stake_created",
                target=200,
                description="Open 200 new stakes guild-wide.",
            ),
            ChallengeTemplate(
                name="Validators Online",
                trigger="validator_registered",
                target=30,
                description="Spin up 30 validators this week.",
            ),
            ChallengeTemplate(
                name="Daily Devotion",
                trigger="daily_claimed",
                target=300,
                description="Claim ,daily 300 times across the guild.",
            ),
            ChallengeTemplate(
                name="Workforce",
                trigger="work_completed",
                target=2_000,
                description="Knock out 2,000 ,work shifts.",
            ),
        ],
    ),
    Pair(
        key="fishing_frenzy",
        season=SeasonTemplate(
            name="Fishing Frenzy Week",
            theme="fishing_frenzy",
            metric="pass_xp",
        ),
        challenges=[
            ChallengeTemplate(
                name="Cast Marathon",
                trigger="fish_caught",
                target=5_000,
                description="Land 5,000 fish across the guild.",
            ),
            ChallengeTemplate(
                name="Legendary Hunt",
                trigger="fish_legendary",
                target=50,
                description="Land 50 legendary catches.",
                pool_weight=2.0,
            ),
            ChallengeTemplate(
                name="LURE Liquidity",
                trigger="fish_lure_swap",
                target=200,
                description="Swap LURE 200 times this week.",
            ),
            ChallengeTemplate(
                name="REEL Cashouts",
                trigger="fish_reel_cashout",
                target=100,
                description="Cash out REEL 100 times.",
            ),
            ChallengeTemplate(
                name="Wild Battles",
                trigger="fish_wild_battle_won",
                target=150,
                description="Win 150 wild-buddy fishing battles.",
            ),
        ],
    ),
    Pair(
        key="yield_szn",
        season=SeasonTemplate(
            name="Yield Season Week",
            theme="yield_szn",
            metric="net_worth",
        ),
        challenges=[
            ChallengeTemplate(
                name="LP Tide",
                trigger="lp_added",
                target=400,
                description="Open 400 new LP positions guild-wide.",
            ),
            ChallengeTemplate(
                name="Stake Stack",
                trigger="stake_created",
                target=300,
                description="Open 300 new stakes this week.",
            ),
            ChallengeTemplate(
                name="Bank Run",
                trigger="bank_deposit",
                target=500,
                description="Make 500 ,bank deposit calls.",
            ),
            ChallengeTemplate(
                name="Validator Push",
                trigger="validator_registered",
                target=25,
                description="Register 25 new validators.",
            ),
            ChallengeTemplate(
                name="Stones Levelled",
                trigger="stone_leveled",
                target=150,
                description="Level up 150 stones this week.",
                pool_weight=2.0,
            ),
        ],
    ),
    Pair(
        key="risk_takers",
        season=SeasonTemplate(
            name="Risk Takers Week",
            theme="risk_takers",
            metric="pass_xp",
        ),
        challenges=[
            ChallengeTemplate(
                name="Spin Spree",
                trigger="gamble_play",
                target=2_000,
                description="Play 2,000 gambling rounds.",
            ),
            ChallengeTemplate(
                name="House Wins",
                trigger="gamble_win",
                target=400,
                description="Land 400 gambling wins.",
                pool_weight=2.0,
            ),
            ChallengeTemplate(
                name="Eat Attempts",
                trigger="exploit_run",
                target=300,
                description="Attempt 300 ,eat runs.",
            ),
            ChallengeTemplate(
                name="Rich Devoured",
                trigger="exploit_win",
                target=150,
                description="Eat 150 rich players.",
            ),
            ChallengeTemplate(
                name="Daily Fix",
                trigger="daily_claimed",
                target=300,
                description="Claim ,daily 300 times across the guild.",
            ),
        ],
    ),
    Pair(
        key="wild_season",
        season=SeasonTemplate(
            name="Wild Season Week",
            theme="wild_season",
            metric="buddy_wins",
        ),
        challenges=[
            ChallengeTemplate(
                name="Spawn Surge",
                trigger="fish_wild_battle_spawn",
                target=300,
                description="Trigger 300 wild-buddy spawns from fishing.",
            ),
            ChallengeTemplate(
                name="Wild Wins",
                trigger="fish_wild_battle_won",
                target=200,
                description="Win 200 wild-buddy fishing battles.",
            ),
            ChallengeTemplate(
                name="Delve Spawns",
                trigger="delve_wild_battle_spawn",
                target=200,
                description="Trigger 200 wild buddy spawns in the dungeon.",
            ),
            ChallengeTemplate(
                name="Delve Wins",
                trigger="delve_wild_battle_won",
                target=150,
                description="Win 150 delve wild buddy battles.",
            ),
            ChallengeTemplate(
                name="Wild Captures",
                trigger="fish_wild_buddy_captured",
                target=75,
                description="Capture 75 wild buddies (fishing).",
                pool_weight=2.0,
            ),
        ],
    ),
]


# ── Helpers ────────────────────────────────────────────────────────────────

def pair_count() -> int:
    return len(PAIRS)


def get_pair(idx: int) -> Pair:
    """Return the pair at ``idx`` (mod len(PAIRS)). Raises if no pairs."""
    if not PAIRS:
        raise ValueError("seasons_pairs_config.PAIRS is empty")
    return PAIRS[int(idx) % len(PAIRS)]


def next_idx(idx: int) -> int:
    """Cursor advancement helper. Wraps around to 0."""
    if not PAIRS:
        return 0
    return (int(idx) + 1) % len(PAIRS)


def clamp_days(days: int | None) -> int:
    """Clamp a stored / requested day count into the safe range."""
    if days is None:
        return DEFAULT_DURATION_DAYS
    return max(MIN_DURATION_DAYS, min(MAX_DURATION_DAYS, int(days)))


def clamp_pool(pool: float | None, default: float) -> float:
    """Clamp a stored / requested pool USD into the safe range. ``None``
    falls back to the supplied default rather than zero so admins who
    never set a pool still get a meaningful prize.
    """
    if pool is None:
        return float(default)
    return max(MIN_POOL_USD, min(MAX_POOL_USD, float(pool)))


def split_pool(pair: Pair, total_usd: float) -> list[float]:
    """Split ``total_usd`` across the pair's challenges by ``pool_weight``.

    Returns a list aligned with ``pair.challenges`` so caller can zip
    them. Equal weights divide evenly; weight 2.0 doubles that
    challenge's slice, etc.
    """
    if total_usd <= 0 or not pair.challenges:
        return [0.0] * len(pair.challenges)
    weights = [max(0.0, float(c.pool_weight)) for c in pair.challenges]
    sw = sum(weights) or 1.0
    return [total_usd * (w / sw) for w in weights]


# Sanity check: every pair must ship exactly CHALLENGES_PER_PAIR challenges
# so the rotation logic doesn't have to special-case partial pairs.
def _validate() -> None:
    for p in PAIRS:
        if len(p.challenges) != CHALLENGES_PER_PAIR:
            raise ValueError(
                f"seasons_pairs_config.PAIRS[{p.key!r}]: "
                f"expected {CHALLENGES_PER_PAIR} challenges, "
                f"got {len(p.challenges)}"
            )


_validate()
