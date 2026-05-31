"""configs/eatchain_config.py  -  EatChain expansion tuning.

EatChain rebrands the "Eat the Rich" minigame as a satirical simulated
Layer-2 DeFi ecosystem. This module holds every NEW tuning value, the rank
ladder, the XP/level math, the $EAT token economy, the new-tactic costs and
cooldowns, the cosmetic titles, and all the flavor-text banks.

The ORIGINAL theft engine still reads its tuning from ``core.config.Config``
(``Config.EAT_*`` and ``Config.EAT_TACTICS``) -- those constants are shared
with cogs/help.py and the agent eat tool, so they stay where they are. This
module is strictly additive: it never duplicates a value that already lives
in ``Config``.
"""
from __future__ import annotations

import math
from typing import Final

# Raw monetary scale -- every amount column is NUMERIC(36,0) scaled by 10**18.
_S: Final[int] = 10 ** 18

# ── The $EAT token ─────────────────────────────────────────────────────────
# $EAT is an EARN-ONLY reward token (registered in Config.TOKENS and
# Config.EARN_ONLY_TOKENS). Liquid $EAT lives in wallet_holdings on the
# `eat` network; staked $EAT lives in exploit_stats.eat_staked.
EAT_SYMBOL: Final[str] = "EAT"
EAT_NETWORK: Final[str] = "eat"          # wallet_holdings.network short key
EAT_EMOJI: Final[str] = "\U0001F37D"     # fork-and-knife plate

# ── Mempool snipe -- the new random-target core loop ───────────────────────
SNIPE_COOLDOWN: Final[int] = 90          # ,eat snipe -- reduced vs the 120s group cd
SNIPE_CANDIDATE_POOL: Final[int] = 10    # snipe picks at random from the N nearest richer actives

# ── ,eat nibble -- quick, tiny, instant low-stakes eat ─────────────────────
NIBBLE_COOLDOWN: Final[int] = 45
NIBBLE_COST: Final[int] = int(25 * _S)
NIBBLE_STEAL_PCT: Final[float] = 0.005   # 0.5% of the target wallet
NIBBLE_SUCCESS: Final[float] = 0.72
NIBBLE_MAX_STEAL: Final[int] = int(5_000 * _S)
NIBBLE_FAIL_PENALTY_PCT: Final[float] = 0.30

# ── ,eat feast -- Lv100 multi-snipe ────────────────────────────────────────
FEAST_COOLDOWN: Final[int] = 3600
FEAST_COST: Final[int] = int(100_000 * _S)
FEAST_TARGETS: Final[int] = 3            # snipes the top-N wealthiest actives at once
FEAST_SUCCESS: Final[float] = 0.55       # per-target roll
FEAST_STEAL_PCT: Final[float] = 0.04     # of each target's wallet

# ── ,eat rug -- pull your own liquidity ────────────────────────────────────
RUG_COOLDOWN: Final[int] = 7200
RUG_EXIT_BONUS_PCT: Final[float] = 0.15  # bonus $EAT minted on top of the unstaked principal
RUG_VULN_DURATION: Final[int] = 7200     # 2h window where you are wide open
RUG_VULN_ODDS_BONUS: Final[float] = 0.30 # +30% attacker odds vs a rug-vulnerable target

# ── ,eat chew -- digest a recent winning eat ───────────────────────────────
CHEW_WINDOW: Final[int] = 600            # seconds after a win you may still chew it
CHEW_BONUS_EAT_PCT: Final[float] = 0.25  # bonus $EAT = 25% of the eat's reward
CHEW_BONUS_XP: Final[int] = 15

# ── ,eat insurance -- charge-based eat blocker ─────────────────────────────
INSURANCE_COOLDOWN: Final[int] = 1800
INSURANCE_PREMIUM_EAT: Final[int] = int(40 * _S)  # $EAT per charge
INSURANCE_MAX_CHARGES: Final[int] = 3
INSURANCE_DURATION: Final[int] = 86400            # charges expire after 24h

# ── ,eat audit -- cheap recon ──────────────────────────────────────────────
AUDIT_COOLDOWN: Final[int] = 60
AUDIT_COST_EAT: Final[int] = int(5 * _S)

# ── ,eat burn -- burn $EAT for a timed odds buff ───────────────────────────
BURN_MIN: Final[int] = int(10 * _S)
BURN_ODDS_PER_UNIT: Final[float] = 0.0015   # +0.15% odds per 1 $EAT burned
BURN_MAX_BONUS: Final[float] = 0.15         # buff caps at +15%
BURN_BUFF_DURATION: Final[int] = 900        # buff stays armed for 15 min

# ── $EAT staking ───────────────────────────────────────────────────────────
STAKE_MIN: Final[int] = int(10 * _S)
# Hourly yield rate indexed by rank tier 0..5 -- higher rank validators earn more.
STAKE_HOURLY_APY: Final[tuple] = (
    0.00015, 0.00020, 0.00026, 0.00033, 0.00042, 0.00055,
)
YIELD_TICK_SECONDS: Final[int] = 3600

# ── $EAT block rewards (earned by eating) ──────────────────────────────────
EAT_REWARD_PER_1K_USD: Final[float] = 1.0   # $EAT minted per $1,000 of gross devoured
EAT_REWARD_GAP_MULT: Final[float] = 0.12    # extra multiplier per 1x of wealth gap
EAT_REWARD_MIN: Final[int] = int(1 * _S)
EAT_REWARD_MAX: Final[int] = int(250_000 * _S)
EAT_COMBO_BONUS_PCT: Final[float] = 0.50    # +50% $EAT when both prep AND cook were spent
NIBBLE_EAT_REWARD: Final[int] = int(1 * _S)
GM_TIP: Final[int] = int(1 * _S)            # the ,eat gm easter-egg tip

# ── Progression -- the Eat Ladder ──────────────────────────────────────────
XP_CURVE_BASE: Final[int] = 50              # XP for level L from zero == base*L*(L-1)/2
MAX_LEVEL: Final[int] = 100
XP_PER_EAT_BASE: Final[int] = 20            # flat XP for a winning eat
XP_GAP_MULT: Final[float] = 8.0             # extra XP per 1x of wealth gap
XP_COMBO_BONUS: Final[int] = 25             # extra XP when prep+cook were both used
XP_DEFEND_BONUS: Final[int] = 12            # XP for successfully repelling an eat
XP_NIBBLE: Final[int] = 4                   # XP for a winning nibble
XP_PER_VETERAN_WIN: Final[int] = 60         # migration backfill: XP per legacy heists_won

# ── Rank ladder (MEV / validator theme) ────────────────────────────────────
# Ordered ascending by min_level. rank_for_level() picks the highest match.
RANKS: Final[tuple] = (
    {"tier": 0, "min_level": 1,
     "name": "Mempool Peasant",   "emoji": "\U0001FAB1",
     "perk": "Scraping the mempool floor. Basic tactics only."},
    {"tier": 1, "min_level": 10,
     "name": "Sandwich Bot",      "emoji": "\U0001F96A",
     "perk": "+5% base odds on every eat."},
    {"tier": 2, "min_level": 25,
     "name": "MEV Searcher",      "emoji": "\U0001F50D",
     "perk": "Unlocks `,eat bite` and `,eat audit`."},
    {"tier": 3, "min_level": 50,
     "name": "Liquidation Engine", "emoji": "⚙️",
     "perk": "-25% on all stake and attack costs."},
    {"tier": 4, "min_level": 75,
     "name": "Block Builder",     "emoji": "\U0001F9F1",
     "perk": "Most Wanted -- snipes lock onto bigger fish."},
    {"tier": 5, "min_level": 100,
     "name": "Apex Validator",    "emoji": "\U0001F451",
     "perk": "Unlocks `,eat feast` and the prestige title slot."},
)

PERK_ODDS_BONUS: Final[float] = 0.05        # tier >= 1
PERK_COST_MULT: Final[float] = 0.75         # tier >= 3 (multiply costs by this)


def xp_for_level(level: int) -> int:
    """Total XP required to reach ``level`` from scratch."""
    level = max(1, int(level))
    return int(XP_CURVE_BASE * level * (level - 1) // 2)


def level_for_xp(xp: float) -> int:
    """Eat-ladder level for a total XP figure. Standard Discoin curve."""
    if xp <= 0 or XP_CURVE_BASE <= 0:
        return 1
    level = int((1 + math.sqrt(1 + 8 * float(xp) / XP_CURVE_BASE)) / 2)
    return max(1, min(level, MAX_LEVEL))


def rank_for_level(level: int) -> dict:
    """Return the rank dict for an eat-ladder level."""
    level = max(1, int(level))
    chosen = RANKS[0]
    for rank in RANKS:
        if level >= rank["min_level"]:
            chosen = rank
    return chosen


def tier_for_level(level: int) -> int:
    return int(rank_for_level(level)["tier"])


def perk_unlocked(level: int, perk: str) -> bool:
    """Perk gate. perk in {'odds', 'bite', 'cheap', 'mostwanted', 'feast'}."""
    tier = tier_for_level(level)
    return {
        "odds": tier >= 1,
        "bite": tier >= 2,
        "cheap": tier >= 3,
        "mostwanted": tier >= 4,
        "feast": tier >= 5,
    }.get(perk, False)


def odds_bonus_for_level(level: int) -> float:
    """Flat success-odds bonus granted by rank."""
    return PERK_ODDS_BONUS if perk_unlocked(level, "odds") else 0.0


def cost_mult_for_level(level: int) -> float:
    """Multiplier applied to stake/attack costs (rank discount)."""
    return PERK_COST_MULT if perk_unlocked(level, "cheap") else 1.0


def stake_hourly_apy(level: int) -> float:
    return STAKE_HOURLY_APY[tier_for_level(level)]


def xp_for_eat(steal_usd: float, gap: float, combo: bool) -> int:
    """XP awarded for a winning eat."""
    xp = XP_PER_EAT_BASE + XP_GAP_MULT * max(0.0, gap - 1.0)
    if combo:
        xp += XP_COMBO_BONUS
    # A token reward for actually devouring something sizable.
    xp += min(120.0, math.log10(max(10.0, steal_usd)) * 6.0)
    return max(1, int(xp))


def eat_reward(steal_usd: float, gap: float, combo: bool) -> int:
    """Raw $EAT minted for a winning eat."""
    units = (steal_usd / 1000.0) * EAT_REWARD_PER_1K_USD
    units *= 1.0 + EAT_REWARD_GAP_MULT * max(0.0, gap - 1.0)
    raw = int(units * _S)
    if combo:
        raw = int(raw * (1.0 + EAT_COMBO_BONUS_PCT))
    return max(EAT_REWARD_MIN, min(raw, EAT_REWARD_MAX))


# ── Cosmetic titles ────────────────────────────────────────────────────────
# Unlock conditions are evaluated against an exploit_stats row by
# unlocked_titles(). "The Silent Devourer" suppresses your name in the
# public ,eat history feed.
TITLES: Final[dict] = {
    "fresh_meat": {
        "name": "Fresh Meat", "emoji": "\U0001F416",
        "desc": "Everyone starts on the menu.",
    },
    "silent_devourer": {
        "name": "The Silent Devourer", "emoji": "\U0001F92B",
        "desc": "Win 50 eats. Your name is hidden in the public history feed.",
    },
    "diamond_hands": {
        "name": "Diamond Hands", "emoji": "\U0001F48E",
        "desc": "Repel 25 attacks.",
    },
    "rug_architect": {
        "name": "Rug Architect", "emoji": "\U0001F9F6",
        "desc": "Pull 10 rugs.",
    },
    "mt_gox_survivor": {
        "name": "Mt. Gox Survivor", "emoji": "\U0001F480",
        "desc": "Be hunted 100 times and live to tell it.",
    },
    "exit_liquidity": {
        "name": "Exit Liquidity", "emoji": "\U0001F4C9",
        "desc": "Lose more than you have ever devoured. We don't talk about it.",
    },
    "apex": {
        "name": "Apex Validator", "emoji": "\U0001F451",
        "desc": "Reach level 100.",
    },
}


def unlocked_titles(stats: dict | None, level: int) -> list[str]:
    """Title ids a player has earned, given their exploit_stats row + level."""
    out = ["fresh_meat"]
    if not stats:
        return out
    won = int(stats.get("heists_won") or 0)
    defended = int(stats.get("times_defended") or 0)
    targeted = int(stats.get("times_targeted") or 0)
    rugs = int(stats.get("rugs_pulled") or 0)
    stolen = float(stats.get("total_stolen") or 0)
    lost = float(stats.get("total_lost") or 0)
    if won >= 50:
        out.append("silent_devourer")
    if defended >= 25:
        out.append("diamond_hands")
    if rugs >= 10:
        out.append("rug_architect")
    if targeted >= 100:
        out.append("mt_gox_survivor")
    if lost > stolen and lost > 0:
        out.append("exit_liquidity")
    if level >= MAX_LEVEL:
        out.append("apex")
    return out


# ── Flavor banks ───────────────────────────────────────────────────────────

SNIPE_SCAN_FLAVORS: Final[tuple] = (
    "Scanning mempool liquidity... pending transactions sorted by fee...",
    "Spinning up the searcher bot. Indexing fat wallets...",
    "Front-running the block. Looking for someone slow and rich...",
    "Sandwiching the mempool. A juicy target is mid-transaction...",
    "Querying the EatChain indexer for over-collateralised whales...",
)

# Win flavor woven through crypto-culture parody.
CRYPTO_WIN_FLAVORS: Final[tuple] = (
    "{target}'s funds were *technically* fine -- right up until they weren't.",
    "You lent {target}'s liquidity to yourself. For safekeeping. wagmi.",
    "{target} said 'have fun staying poor'. You took the funds. And the poverty.",
    "The peg held, briefly. {target}'s bag did not.",
    "{target} bought the top. You bought {target}'s bottom.",
    "Number went down -- for {target}. You front-ran the whole block.",
    "{target}'s cold storage was lukewarm at best. Clean extraction.",
)

CRYPTO_FAIL_FLAVORS: Final[tuple] = (
    "{target}'s funds were 'lent out for liquidity' before you arrived. **{penalty}** gone.",
    "Your transaction reverted. {target}'s multisig held. Gas wasted: **{penalty}**.",
    "{target} rugged YOU first. Down **{penalty}** and feeling ngmi.",
    "Slippage ate your trade. {target} walks; you eat a **{penalty}** loss.",
    "{target}'s validator slashed your attempt. **{penalty}** down the drain.",
    "MEV bots got there before you. {target} is fine; you are out **{penalty}**.",
)

NIBBLE_FLAVORS: Final[tuple] = (
    "You take a polite little nibble of {target}'s wallet.",
    "Just a taste. {target} barely feels the dust leave.",
    "A dainty bite. The mempool calls it 'micro-MEV'.",
    "You skim a crumb off {target}. Every gwei counts.",
)

NIBBLE_SPAM_FLAVORS: Final[tuple] = (
    "You are genuinely just grazing at this point.",
    "Nibble, nibble, nibble. Maybe try a real meal sometime?",
    "The mempool is starting to call you 'the snacker'.",
    "This is less 'apex predator' and more 'office fridge raider'.",
)

RUG_FLAVORS: Final[tuple] = (
    "You yank every dollar of liquidity out of your own pool. Classic.",
    "Anonymous founder energy: you pulled the rug on yourself and cashed out.",
    "The pool is drained, the Discord is on fire, and your wallet is fat.",
    "You exit-scammed your own validator. The on-chain sleuths are already tweeting.",
)

GM_LINES: Final[tuple] = (
    "gm. wagmi. here is a crumb of $EAT for vibes.",
    "gm ser. the mempool is bullish. have some $EAT.",
    "gm. ngmi is a state of mind. $EAT is a state of wallet.",
    "gm. few understand. fewer eat. here, eat.",
)

# Rare positive event -- rolled before an eat. Big odds boost + flavor.
RARE_EVENT_CHANCE: Final[float] = 0.005     # ~1 in 200
RARE_EVENT_ODDS_BONUS: Final[float] = 0.40
RARE_EVENT_FLAVORS: Final[tuple] = (
    "\U0001F984 **Vitalik is in the mempool.** The block bends in your favour.",
    "\U0001F388 **A testnet faucet glitched mainnet.** Free alpha -- strike now.",
    "\U0001F3DB️ **The SEC is distracted.** Regulatory clear skies. Eat freely.",
)

# Ultra-rare flavor-only jackpot line on a big win.
LEGENDARY_CHANCE: Final[float] = 0.001      # ~1 in 1000
LEGENDARY_FLAVOR: Final[str] = (
    "\U0001F4C0 **You cracked open a Satoshi-era wallet.** The chain goes quiet. "
    "Somewhere, a cypherpunk sheds a single tear."
)
