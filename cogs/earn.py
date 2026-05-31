from __future__ import annotations

import asyncio
import logging
import random
import time

log = logging.getLogger(__name__)

import discord
from discord.ext import commands

from core.config import Config
from cogs.shop import _item_stat, _liqstone_stat, _lockstone_stat, _vaultstone_stat
from core.framework.ai import complete as ai_complete
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, module_allowed, no_bots
from services.net_worth import compute_net_worth
from services.active_players import get_random_active_players
from cogs.social_context import mark_hot_channel
from core.framework.fuzzy import suggest_subcommand
from core.framework.cooldowns import user_cooldown
from core.framework.ui import C_AMBER, C_BLACK, C_BUY, C_ERROR, C_GOLD, C_INFO, C_PINK, C_PURPLE, C_SELL, C_SUCCESS, C_TEAL, FormatKit, fmt_ts, fmt_usd
from core.framework.scale import to_raw, to_human

# ── Daily constants ───────────────────────────────────────────────────────────
_48H = 48 * 3600

# ── Streak → work cooldown reduction tiers ────────────────────────────────────
# Each tuple is (min_streak, multiplier). Checked highest-first.
# Max reduction: 30% at streak 180+. Never reduces below _WORK_CD_MIN seconds.
_STREAK_WORK_TIERS: list[tuple[int, float]] = [
    (180, 0.70),  # 6 months+  → 30% faster
    (90,  0.75),  # 3 months+  → 25% faster
    (60,  0.80),  # 2 months+  → 20% faster
    (30,  0.85),  # 1 month+   → 15% faster
    (14,  0.90),  # 2 weeks+   → 10% faster
    (7,   0.95),  # 1 week+    →  5% faster
]
_WORK_CD_MIN = 60  # floor: no cooldown ever drops below 1 minute


def _streak_work_multiplier(streak: int) -> float:
    """Return the work cooldown multiplier for a given daily streak."""
    for threshold, mult in _STREAK_WORK_TIERS:
        if streak >= threshold:
            return mult
    return 1.0

# ── Daily flavor lines ──────────────────────────────────────────────────────
# These show as a one-liner in the daily reward embed for personality.
# Supports {amount} substitution at runtime.
_DAILY_FLAVORS: list[str] = [
    "The protocol you used once in 2021 to swap $5 of DOGE quietly dropped a retroactive airdrop. You checked your wallet on a whim and found it waiting. You claimed **{amount}**.",
    "J. Powell signed the executive order. The printers are running. Some of that fresh liquidity somehow routed directly to your staking address. You claimed **{amount}**.",
    "You forgot you had funds locked in a yield aggregator that was supposed to be a rug. Turns out it was just poorly managed. Your rewards finally vested. You claimed **{amount}**.",
    "A new Layer 2 decided to retroactively reward anyone who ever paid over $100 in gas fees. Your 2022 suffering has finally been monetized. You claimed **{amount}**.",
    "The DAO treasury passed Proposal #69 after three months of governance theater. The 'Sustainability Fund' has been distributed to bagholders. You claimed **{amount}**.",
    "You were a victim of a hack three years ago. A recovery fund just cleared your claim after the longest application process in human history. You claimed **{amount}**.",
    "The points you've been farming by locking up your liquidity were finally converted into a tradable token. It's actually worth something today. You claimed **{amount}**.",
    "You checked your hidden folder on OpenSea and found an airdrop that wasn't a drainer for once. Somehow it's a Blue Chip in the making. You claimed **{amount}**.",
    "Your staked ARC earned its daily interest, proving that 'doing absolutely nothing' remains the most profitable strategy in this market. You claimed **{amount}**.",
    "A paper wallet in the back of your desk drawer contained a Moneta fork you had completely forgotten about. It's surprisingly liquid. You claimed **{amount}**.",
    "The 'Fair Launch' protocol you used once on a slow Tuesday decided to retroactively reward early adopters. You are briefly in the 1%. You claimed **{amount}**.",
    "The Celsius recovery fund distributed another tranche. It barely covers the emotional damage, but it's something. You claimed **{amount}**.",
    "A governance vote from 2022 you forgot you participated in finally resolved. The winning side got a treasury cut. You were on the winning side. You claimed **{amount}**.",
    "The DEX you used for three trades in 2021 launched a token. You qualified for the airdrop. Every gas fee you suffered back then was an investment. You claimed **{amount}**.",
    "You clicked 'harvest' on a yield farm you'd written off completely. The APY was real this time. The compound interest hit quietly. You claimed **{amount}**.",
    "The protocol recovered 40% of the stolen funds from the 2023 exploit and distributed them pro rata. You got your share. You claimed **{amount}**.",
    "The on-chain data shows you bridged to this chain before it was cool. The retrospective airdrop landed in your wallet at 3 AM. You claimed **{amount}**.",
    "You set up auto-stake on a validator eighteen months ago and forgot about it. The rewards have been silently compounding ever since. You claimed **{amount}**.",
    "The meme you posted in the project Discord in 2021 was used in their official marketing without credit. They settled the 'misunderstanding' quietly. You claimed **{amount}**.",
    "The bear market filter found you still present, still building, still holding. The early-adopter bonus distribution rewarded your endurance. You claimed **{amount}**.",
]

_DAILY_STREAK_FLAVORS: list[str] = [
    "The streak is alive and the compounding interest is starting to show. Every consecutive claim builds your position. You claimed **{amount}**.",
    "Back-to-back claims. The algorithm recognizes your consistency. Others slept. You showed up. You claimed **{amount}**.",
    "Diamond hands on the daily grind. The streak multiplier is doing its thing. The market can't rug your discipline. You claimed **{amount}**.",
    "Streak warriors don't take days off. Your pathological commitment to this ritual continues to pay out. You claimed **{amount}**.",
    "The chain remains unbroken. Your dedication compounds quietly in the background while everyone else resets. You claimed **{amount}**.",
]

_DAILY_MAX_STREAK_FLAVORS: list[str] = [
    "Max streak achieved. You have logged in every single day. The blockchain respects this level of pathological dedication. You claimed **{amount}**.",
    "Peak daily. Peak rewards. You've done the one thing Do Kwon said was impossible: maintained a stable peg  -  of discipline. You claimed **{amount}**.",
    "A full year of daily claims. Not even Mt. Gox had this kind of uptime. You are a category unto yourself. You claimed **{amount}**.",
    "Max streak. The bot notifies you, but you're already there. You've transcended reminders. You are the reminder. You claimed **{amount}**.",
    "They said diamond hands were for holding tokens. You applied them to a daily check-in instead. Certified degen. You claimed **{amount}**.",
]

# ── Random surprise events ──────────────────────────────────────────────────
# Rare positive surprises that occasionally fire on .work and .daily.
# Designed to feel like a windfall (free money / free consumable) with no
# downside. Combined chance is small so they don't distort the economy.
_WORK_SURPRISE_CHANCE = 0.025
_DAILY_SURPRISE_CHANCE = 0.030

# Weighted pool of surprise kinds (must sum to ~1.0).
_SURPRISE_KINDS: list[tuple[str, float]] = [
    ("treasure",        0.45),
    ("jackpot",         0.25),
    ("validator_guard", 0.18),
    ("yield_guard",     0.12),
]

_SURPRISE_TREASURE_FLAVORS: list[str] = [
    "A duffel bag fell off an armored MetaMask truck and rolled to your feet. Nobody saw. Finders keepers.",
    "You opened a draft email from 2021 titled 'in case of bull market'. Past you had hidden cold-storage keys inside.",
    "An NFT you minted as a joke three years ago just got listed in a museum collection. You sold the floor copy.",
    "A pseudonymous benefactor zapped your wallet from a mixer. The memo just said 'gm'.",
    "You found a forgotten paper wallet behind a bookshelf. It had real coins on it for once.",
    "The exchange you used in 2021 finally released funds it 'temporarily froze for compliance review'.",
    "A protocol you reported to an auditor paid out the bounty. Whistleblower money hits different.",
    "You won a Twitter giveaway you don't remember entering. The DM was real this time.",
    "An old hardware wallet you forgot about turned out to hold a forgotten allocation. The seed phrase was on a sticky note.",
    "A retroactive airdrop snapshot landed on a wallet you tagged as 'burner' three years ago. Turns out it was real.",
]

_SURPRISE_JACKPOT_FLAVORS: list[str] = [
    "JACKPOT! A flash-loan glitch in a protocol you don't even use rebated triple your session into your wallet by mistake.",
    "JACKPOT! A retroactive airdrop caught your address right before it would've been pruned. Triple payout.",
    "JACKPOT! The DAO treasury overpaid the contributor pool by exactly 200%. The transaction is irreversible.",
    "JACKPOT! An MEV bot front-ran a front-running bot front-running you. You got the spread three times over.",
    "JACKPOT! A typo in the smart contract sent rewards to the wrong address. That address was yours. Nobody noticed.",
    "JACKPOT! The bridge fee rebate program had a bug that compounded your refund. You triple-claimed before they patched it.",
]

_SURPRISE_VGUARD_FLAVORS: list[str] = [
    "A passing validator slipped a free Validator Guard into your inventory. 'Just in case,' they said.",
    "You found a Validator Guard taped under a hardware wallet at a hackathon. It still works.",
    "A node operator in Discord airdropped you a Validator Guard for being one of the 'real ones'.",
    "An old friend from the 2017 cycle handed you a Validator Guard. 'You'll need this eventually,' they said.",
    "A retired staker's estate sale included a single sealed Validator Guard. The auction house overlooked it.",
]

_SURPRISE_YGUARD_FLAVORS: list[str] = [
    "A retired DeFi farmer left you their last Yield Guard in their will. Touching, in its own way.",
    "You noticed a Yield Guard sitting in a public faucet contract. You claimed it before the bots did.",
    "A grateful protocol shipped you a Yield Guard for stress-testing their edge cases (by losing money).",
    "You found a Yield Guard in the swag bag of a conference you don't remember attending.",
    "A grateful auditor sent you a Yield Guard after you accidentally surfaced a bug by losing money to it.",
]


async def _roll_surprise(
    base_amount: float,
    db,
    uid: int,
    gid: int,
    *,
    chance: float,
) -> dict | None:
    """Maybe roll a positive surprise event. Returns a dict on hit, else None.

    Result dict shape:
      kind:        "treasure" | "jackpot" | "validator_guard" | "yield_guard"
      flat_bonus:  USD added to payout (treasure only, else 0.0)
      multiplier:  multiplier applied to payout (jackpot only, else 1.0)
      title:       short field title with emoji
      flavor:      one-line flavor description
    """
    if random.random() >= chance:
        return None

    kinds, weights = zip(*_SURPRISE_KINDS)
    kind = random.choices(kinds, weights=weights, k=1)[0]

    if kind == "treasure":
        # 2x-5x current earnings, with a $50 floor so reduced sessions
        # (e.g. capped or risky-loss path) still feel like a real surprise.
        flat = max(round(base_amount * random.uniform(2.0, 5.0), 2), 50.0)
        return {
            "kind":       "treasure",
            "flat_bonus": flat,
            "multiplier": 1.0,
            "title":      "🎁 Treasure Chest!",
            "flavor":     random.choice(_SURPRISE_TREASURE_FLAVORS),
        }
    if kind == "jackpot":
        return {
            "kind":       "jackpot",
            "flat_bonus": 0.0,
            "multiplier": 3.0,
            "title":      "🎰 JACKPOT!",
            "flavor":     random.choice(_SURPRISE_JACKPOT_FLAVORS),
        }
    if kind == "validator_guard":
        try:
            await db.add_validator_guard(uid, gid, 1)
        except Exception:
            log.exception("surprise: validator_guard grant failed uid=%s gid=%s", uid, gid)
            return None
        return {
            "kind":       "validator_guard",
            "flat_bonus": 0.0,
            "multiplier": 1.0,
            "title":      "🛡️ Free Validator Guard!",
            "flavor":     random.choice(_SURPRISE_VGUARD_FLAVORS),
        }
    if kind == "yield_guard":
        try:
            await db.add_yield_guard(uid, gid, 1)
        except Exception:
            log.exception("surprise: yield_guard grant failed uid=%s gid=%s", uid, gid)
            return None
        return {
            "kind":       "yield_guard",
            "flat_bonus": 0.0,
            "multiplier": 1.0,
            "title":      "🌾 Free Yield Guard!",
            "flavor":     random.choice(_SURPRISE_YGUARD_FLAVORS),
        }
    return None

# ── AI flavor cache ──────────────────────────────────────────────────────────
# Uses Redis when available (shared across processes), falls back to in-memory.
_ai_flavor_cache: dict[str, tuple[float, str]] = {}  # in-memory fallback
_AI_FLAVOR_TTL = 300  # 5 minutes
_FLAVOR_REDIS_PREFIX = "discoin:ai_flavor:"
_flavor_redis = None  # set by Earn cog __init__


async def _get_ai_flavor(job_id: str, job_title: str, amount_str: str) -> str | None:
    """Return AI-generated work flavor text, cached per job tier for 5 min."""
    # Try Redis first
    if _flavor_redis is not None:
        try:
            cached = await _flavor_redis.get(f"{_FLAVOR_REDIS_PREFIX}{job_id}")
            if cached is not None:
                return cached.replace("{amount}", amount_str)
        except Exception:
            pass  # fall through to in-memory

    # In-memory fallback
    now = time.time()
    mem_cached = _ai_flavor_cache.get(job_id)
    if mem_cached and now < mem_cached[0]:
        return mem_cached[1].replace("{amount}", amount_str)

    result = await ai_complete(
        [
            {"role": "system", "content":
                f"You are writing a 1-sentence degen crypto work story for a Discord economy game. "
                f"The player's job is '{job_title}'. Write a funny, absurd story about what they did to earn money. "
                "Include the placeholder {{amount}} exactly once where the earnings appear. Max 25 words. No quotes."},
            {"role": "user", "content": "Generate a new work story."},
        ],
        max_tokens=60,
        temperature=1.1,
    )
    if result and "{amount}" in result:
        # Store in Redis
        if _flavor_redis is not None:
            try:
                await _flavor_redis.setex(
                    f"{_FLAVOR_REDIS_PREFIX}{job_id}",
                    _AI_FLAVOR_TTL,
                    result,
                )
            except Exception:
                pass
        # Also keep in-memory as fallback
        _ai_flavor_cache[job_id] = (now + _AI_FLAVOR_TTL, result)
        return result.replace("{amount}", amount_str)
    return None


# ── Social AI flavor (multi-player stories) ──────────────────────────────────
# Generates AI flavor text that mentions the player AND other active players.
_SOCIAL_FLAVOR_CACHE: dict[str, tuple[float, str]] = {}
_SOCIAL_FLAVOR_TTL = 180  # 3 min  -  shorter to keep stories varied
_SOCIAL_REDIS_PREFIX = "discoin:social_flavor:"

_SOCIAL_PROMPTS: dict[str, str] = {
    "work_earn": (
        "Write a 1-2 sentence degen crypto work story for a Discord economy game. "
        "The player's job is '{job_title}'. "
        "The story MUST mention @{player} by name and at least one of these other active players: {others}. "
        "The other players are bystanders, accomplices, co-workers, or rivals in the story. "
        "Include the placeholder {{amount}} exactly once where the earnings appear. "
        "Max 40 words. No quotes. Crypto/degen humor. Use @name format for all player references."
    ),
    "ape_rugged": (
        "Write a 1-2 sentence degen crypto story about @{player} getting rugged on a shitcoin ape. "
        "Mention at least one of these other active players: {others}  -  maybe they warned them, laughed, "
        "or were somehow involved. Include {{amount}} once for the loss. Max 40 words. No quotes."
    ),
    "ape_break_even": (
        "Write a 1-2 sentence story about @{player} barely breaking even on an ape. "
        "Mention at least one of: {others}  -  maybe they watched, shook their head, or had a comment. "
        "Include {{amount}} once. Max 40 words. No quotes."
    ),
    "ape_moon": (
        "Write a 1-2 sentence story about @{player} hitting a moon on an ape. "
        "Mention at least one of: {others}  -  maybe they're jealous, missed the trade, or were along for the ride. "
        "Include {{amount}} once for the payout. Max 40 words. No quotes. Triumphant tone."
    ),
    "ape_legendary": (
        "Write a 1-2 sentence EPIC story about @{player} hitting a legendary ape return. "
        "Mention at least one of: {others}  -  maybe they're in disbelief, rage-sold early, or are screenshotting the PnL. "
        "Include {{amount}} once. Max 40 words. No quotes. Legendary energy."
    ),
    "ape_ascended": (
        "Write a 1-2 sentence TRANSCENDENT story about @{player} hitting an ascended 100x ape. "
        "Mention at least one of: {others}  -  pure chaos, disbelief, salt, tears. "
        "Include {{amount}} once. Max 40 words. No quotes. God-tier energy."
    ),
    "ape_drained": (
        "Write a 1-2 sentence devastating story about @{player} getting their entire wallet drained on a scam ape. "
        "Mention at least one of: {others}  -  maybe they tried to warn them, or are now roasting them. "
        "Include {{amount}} once for the loss. Max 40 words. No quotes. Brutal."
    ),
    "beg_nothing": (
        "Write a 1-2 sentence funny story about @{player} begging for crypto and getting nothing. "
        "Mention at least one of: {others}  -  maybe they walked past, ignored them, or made a snarky comment. "
        "Max 35 words. No quotes. No amount placeholder."
    ),
    "beg_small": (
        "Write a 1-2 sentence story about @{player} begging and receiving a tiny amount of crypto. "
        "Mention at least one of: {others}  -  maybe they tossed them some dust out of pity. "
        "Include {{amount}} once. Max 35 words. No quotes."
    ),
    "beg_medium": (
        "Write a 1-2 sentence story about @{player} begging and actually receiving a decent amount. "
        "Mention at least one of: {others}  -  maybe they were the generous donor or an impressed bystander. "
        "Include {{amount}} once. Max 35 words. No quotes."
    ),
    "beg_jackpot": (
        "Write a 1-2 sentence HYPE story about @{player} begging and hitting a jackpot. "
        "Mention at least one of: {others}  -  maybe they're furious, jealous, or demanding their cut. "
        "Include {{amount}} once. Max 40 words. No quotes. Big energy."
    ),
    "beg_catastrophe": (
        "Write a 1-2 sentence devastating story about @{player} getting scammed while begging  -  lost almost everything. "
        "Mention at least one of: {others}  -  maybe they're horrified, saw it coming, or are offering condolences. "
        "Include {{amount}} once for the loss. Max 40 words. No quotes. Dark."
    ),
    "daily_claim": (
        "Write a 1-2 sentence story about @{player} claiming their daily reward. "
        "Mention at least one of: {others}  -  maybe they forgot their daily, are jealous of the streak, "
        "or have a comment about the grind. Include {{amount}} once. Max 35 words. No quotes."
    ),
    "daily_streak": (
        "Write a 1-2 sentence story about @{player} continuing their impressive daily streak. "
        "Mention at least one of: {others}  -  maybe they broke their own streak, or are in awe of the discipline. "
        "Include {{amount}} once. Max 35 words. No quotes."
    ),
    "daily_max_streak": (
        "Write a 1-2 sentence story about @{player} at MAX daily streak  -  peak dedication. "
        "Mention at least one of: {others}  -  they're either inspired or deeply concerned about this person's habits. "
        "Include {{amount}} once. Max 35 words. No quotes."
    ),
}


async def _get_social_ai_flavor(
    command: str,
    outcome: str,
    player_name: str,
    other_players: list[str],
    amount_str: str,
    *,
    job_title: str = "",
) -> str | None:
    """Generate AI flavor text that mentions the player and other active players.

    Falls back to None if AI is unavailable or the response doesn't validate.
    The caller should fall back to hardcoded flavor when this returns None.
    """
    if not other_players:
        return None

    prompt_key = f"{command}_{outcome}"
    template = _SOCIAL_PROMPTS.get(prompt_key)
    if not template:
        return None

    others_str = ", ".join(f"@{n}" for n in other_players)
    system_msg = template.format(
        player=player_name,
        others=others_str,
        job_title=job_title,
    )

    cache_key = f"{prompt_key}:{player_name}"

    # Check in-memory cache
    now = time.time()
    mem_cached = _SOCIAL_FLAVOR_CACHE.get(cache_key)
    if mem_cached and now < mem_cached[0]:
        cached_text = mem_cached[1]
        # Re-substitute {amount} (may differ per call)
        if "{amount}" in cached_text:
            return cached_text.replace("{amount}", amount_str)
        return cached_text

    result = await ai_complete(
        [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": "Generate."},
        ],
        max_tokens=80,
        temperature=1.1,
    )
    if not result:
        return None

    # Validate: for outcomes that need an amount, check for {amount} placeholder
    needs_amount = outcome not in ("nothing",)
    if needs_amount and "{amount}" not in result:
        return None

    # Cache the raw result (with {amount} placeholder intact)
    _SOCIAL_FLAVOR_CACHE[cache_key] = (now + _SOCIAL_FLAVOR_TTL, result)

    if "{amount}" in result:
        return result.replace("{amount}", amount_str)
    return result


# ── Work flavor texts ─────────────────────────────────────────────────────────
# (template, company)  -  {company} and {amount} are substituted at runtime
_WORK_FLAVORS: list[tuple[str, str]] = [
    # Homeless tier
    ("You held a sign outside the {company} office reading 'Will audit for food'. Someone gave you **{amount}**.", "Coinbase HQ"),
    ("You found an old USB drive in a park. One file: a wallet.dat. Balance: **{amount}**. Password: 'password'.", "Lost & Found"),
    ("You begged for testnet tokens on the {company} faucet so many times they just sent you mainnet. Earned **{amount}**.", "ChainLink Faucet"),
    ("You slept outside the {company} conference and scalped the last badge for **{amount}**.", "ARCDenver"),
    # Airdrop Farmer tier
    ("You botted 47 wallets to farm the {company} airdrop. Sybil check failed on all of them. Somehow still earned **{amount}**.", "Jito Foundation"),
    ("You spent 6 hours filling out Galxe quests for {company}. Reward: **{amount}**. Minus gas fees you're basically even.", "some L2"),
    ("You RT'd the {company} tweet, replied 'LFG', and connected your empty wallet. They paid you **{amount}** anyway.", "random launchpad"),
    ("You copy-pasted your wallet address into 47 airdrop forms. One of them was real. Earned **{amount}**.", "AnonymousChain"),
    ("The {company} team asked for KYC. You said no. They airdropped you **{amount}** anyway out of respect.", "Permissionless Protocol"),
    # Larper tier
    ("You posted a fake screenshot of your {company} portfolio in the alpha chat. Someone paid **{amount}** for your 'strategy'.", "Alpha Calls DAO"),
    ("You put 'DeFi OG since 2021' in your {company} bio. Whale mistook you for someone real. Tipped **{amount}**.", "CT Influencer Fund"),
    ("You borrowed a friend's Ledger for the {company} photoshoot. The CNBC segment paid **{amount}**.", "Crypto Media Group"),
    ("You attended a {company} Twitter Space and typed 'great alpha' 12 times. Speaker tipped **{amount}**.", "Alpha Radio"),
    # Whitelist Farmer tier
    ("You grinded {company} Discord for 3 weeks to hit OG role. Sold the WL spot for **{amount}**.", "NFT Mint DAO"),
    ("You set 14 alarms to catch the {company} whitelist form. Submitted in 0.3 seconds. Sold access for **{amount}**.", "OversubscribedNFT"),
    ("You ran a {company} referral campaign with 200 fake accounts. Collected **{amount}** in referral bonus.", "ReferralMax Protocol"),
    ("You found the {company} early-access code in plain HTML. Registered 50 accounts. Profit: **{amount}**.", "Beta Access Labs"),
    # Shitcoin Trencher tier
    ("You sniped the {company} launch block with a MEV bot. Dumped on retail for **{amount}**.", "PumpFun Launchpad"),
    ("You bought the {company} dip at -80% and sold at -75%. Still up **{amount}** from the bottom tick.", "MicroCapGems"),
    ("Your {company} play: buy, tweet 'lowkey bullish', sell into your own volume. Gross: **{amount}**.", "CT Shiller Fund"),
    ("You held {company} through 4 rugs and 1 real project. The real one paid **{amount}**.", "LotteryToken"),
    ("You found a {company} contract with a honeypot modifier. Reverse-engineered the exit. Took **{amount}**.", "MemeCoinTech"),
    # Discord Mod tier
    ("You muted 23 users for saying 'wen token' in the {company} server. Paycheck: **{amount}**.", "CryptoDAO"),
    ("You copy-pasted the {company} roadmap into Discord for the 5th time today. Nobody read it. Earned **{amount}**.", "Sun Protocol Foundation"),
    ("Someone asked about refunds in {company}'s Discord. You deleted it, banned them, and earned **{amount}**.", "Rugpull DAO"),
    ("You issued 14 timeout warnings for 'FUD' in the {company} server. Morale is low. Your wallet: +**{amount}**.", "BullishOnly Community"),
    ("You told 30 people 'the team is building, no alpha yet' in the {company} server. Paid **{amount}** for the cover story.", "Stealth Mode Labs"),
    # DeFi Degen tier
    ("You ape'd into a {company} farm at 40,000% APY. It's now 12%. Still earned **{amount}** before the IL hit.", "YieldFarm X"),
    ("You provided liquidity on {company}. You're down 34% from IL but earned **{amount}** in fees. Classic.", "UniSwap V99"),
    ("You deployed a {company} position at 5× leverage. Got liquidated at 4×. Somehow walked away with **{amount}**.", "Perp Protocol"),
    ("You found a {company} vault with 0 TVL and 2000% APY. You were the first in. Made **{amount}** before others noticed.", "HiddenYield Finance"),
    ("You bridged funds to {company} three times because the first two bridges were 'slow'. Pocketed **{amount}** in arb.", "OmniChain Bridge"),
    # Trader tier
    ("You spotted the {company} divergence at 4am. Set the limit. Went to sleep. Woke up +**{amount}**.", "Perpetuals Exchange"),
    ("You front-ran a {company} listing announcement. In and out in 11 seconds. Net: **{amount}**.", "Tier-1 CEX"),
    ("You wrote a {company} options strategy in a notebook at 2am. It worked. Earned **{amount}**.", "On-Chain Options"),
    ("You read the {company} on-chain flow before the pump. Sized in. Took profit. **{amount}** captured.", "Whale Tracker"),
    ("Your {company} funding rate arbitrage closed cleanly. Both legs filled. Pocketed **{amount}** with zero delta.", "Perp Arb Desk"),
    # Course Seller tier
    ("You recorded a {company} trading course in one weekend. Sold 200 copies at $99. Earned **{amount}** net.", "Alpha Academy"),
    ("You ran a {company} 'paid alpha' group for 3 months. 80% of calls were wrong. Subscribers renewed. Profit: **{amount}**.", "VIP Signals"),
    ("You packaged your {company} losing trades into a 'lessons learned' thread. Sold the ebook for **{amount}**.", "Degen University"),
    ("You hosted a {company} 'how I made 10× in 6 months' webinar. Didn't mention the 8 losses. Earned **{amount}** in signups.", "CT Mentor"),
    # Liquidity Baron tier
    ("Your {company} validator was slashed 0.01 ARC for missing attestations. You still netted **{amount}** this session.", "Arcadia L1"),
    ("You spun up 4 {company} validators at 3am. Two are offline. You're asleep. Earned **{amount}** from the live two.", "Lido Finance"),
    ("You patched your {company} node just before a hardfork. Others didn't. Earned **{amount}** plus their missed rewards.", "SUN Network"),
    ("Your {company} validator uptime is 99.97%. You earned **{amount}** and sent an unsolicited thread about it.", "EigenLayer Restaking"),
    ("You MEV-boosted 3 {company} blocks in one session. Kept the tips. Passed the base to stakers. Net: **{amount}**.", "Flashbots Relay"),
    # Protocol Dev tier
    ("You audited the {company} contracts. Found a critical bug. Got paid **{amount}** in vested tokens with a 4yr cliff.", "Rugpull Labs"),
    ("You deployed a {company} contract and immediately found a reentrancy bug. In prod. Earned **{amount}** fixing it.", "DeFi Anon"),
    ("You wrote the {company} tokenomics doc. 60% to team. You kept a straight face. Earned **{amount}**.", "Moon Capital"),
    ("You submitted a {company} governance proposal to reduce fees. It passed. You earned **{amount}** in bribes to vote no.", "Governance Wars DAO"),
    ("You forked {company}, patched the vulnerability the original team ignored, and absorbed their TVL. Net: **{amount}**.", "ForkFi"),
    # Exploiter tier
    ("You found a {company} read-only reentrancy in prod. Disclosed responsibly. Bug bounty: **{amount}**.", "ImmuneFi"),
    ("You drained the {company} price oracle using a flash loan. Governance passed a fund recovery. You kept **{amount}** as the 'finder's fee'.", "Vulnerable Protocol"),
    ("You deployed a {company} sandwich bot and caught a $2M MEV opportunity. Took **{amount}** after gas.", "Dark Forest"),
    ("You anonymously published the {company} exploit PoC. The team patched in 4 hours. White-hat reward: **{amount}**.", "Security Research DAO"),
    ("You traced the {company} hack back to a dev's leaked private key. Negotiated the return. Kept 10%: **{amount}**.", "Chain Forensics Inc"),
    # Extra variety  -  cross-tier
    ("You accidentally sent your resume to the {company} team instead of your wallet address. They hired you on the spot. Salary advance: **{amount}**.", "Anonymous DeFi"),
    ("You submitted a PR to {company} fixing a typo in their whitepaper. They paid **{amount}** and thanked you in a blog post.", "Arcadia Foundation"),
    ("Your {company} meme went viral. The team bought it as an NFT. You earned **{amount}**.", "Meme Capital"),
    ("You told {company} their token logo looked like a potato. They agreed. Redesign consulting fee: **{amount}**.", "BrandChain"),
    ("You set up a {company} node on a Raspberry Pi held together with tape. Uptime: 100%. Earned **{amount}**.", "Home Node Labs"),
    ("The {company} founder called you at 3am to debug a production issue. You charged **{amount}** per minute.", "Crisis Engineering"),
    ("You joined a {company} hackathon solo. Built a working product. Team of 5 got second place. You won **{amount}**.", "ARCGlobal"),
    ("You accidentally triggered a {company} liquidation cascade. On a testnet. The devs were so scared they paid you **{amount}** to not do it on mainnet.", "Stress Test DAO"),
    ("The {company} discord had a trivia night. You knew every answer. Prize: **{amount}**.", "Alpha Quizzers"),
    ("You proof-read the {company} terms of service. Nobody else had ever read them. They paid **{amount}** for your notes.", "Legal DAO"),
    # ── New crypto-noir additions ─────────────────────────────────────────────
    ("You spent fourteen hours moderating the {company} Discord, banning anyone who asked 'wen token?'. The dev emerged from his 'meditation retreat' and tipped you from the marketing wallet. You earned **{amount}**.", "Stealth Launch DAO"),
    ("You successfully shilled a dead {company} project to your extended family at a holiday dinner. Your uncle bought the top. The foundation sent you a referral bonus regardless. You earned **{amount}**.", "Community Growth DAO"),
    ("You became the 'Community Lead' for {company}, a protocol whose founder is a GPT-4 persona with a LinkedIn. You banned FUD, deleted audits, and submitted your invoice. You earned **{amount}**.", "Anon Dev Protocol"),
    ("The bear market got cold enough that you started ghostwriting 'Intro to Web3' content for the {company} blog. You stared into the void. The direct deposit didn't lie. You earned **{amount}**.", "Web3 Content Collective"),
    ("You farmed {company} Zealy tasks for six straight months, retweeting every cryptic one-word post from a stealth protocol. The Engagement Reward materialized from the void. You earned **{amount}**.", "Points Season Foundation"),
    ("You're the last human in the {company} Telegram. The dev paid you a 'Loyalty Bonus' to stop you from leaving him alone with 50,000 bots. You earned **{amount}**.", "Ghost Chain Labs"),
    ("You spent the night on CT convincing retail that {company} had 'strong fundamentals' ahead of a whale exit. The referral commission cleared at dawn. You earned **{amount}**.", "Exit Liquidity Partners"),
    ("You wrote the {company} tokenomics paper in a weekend: 60% tokens to team, 20% 'ecosystem development'. You sent the invoice before the launch. You earned **{amount}**.", "Tokenomics Consulting DAO"),
    ("You attended seventeen {company} Twitter Spaces and typed 'incredible alpha, thank you ser' in every single one. The project lead noticed your 'engagement' and tipped you. You earned **{amount}**.", "Vibes Only Protocol"),
    ("The {company} founder called you at 3 AM in a production panic. You fixed the critical bug in twelve minutes. You billed for six hours. Nobody checked. You earned **{amount}**.", "On-Call Engineering DAO"),
]

# ── Interactive risk/reward work view ─────────────────────────────────────────

class WorkChoiceView(discord.ui.View):
    """Presented ~10% of the time. Player chooses safe payout or gamble for 2× / 0.
    Only the initiating user can interact with the buttons."""

    def __init__(self, safe_amt: float, risky_amt: float,
                 author_id: int, token: str = "USD") -> None:
        super().__init__(timeout=30.0)
        self.choice: str | None = None
        self.safe_amt = safe_amt
        self.risky_amt = risky_amt
        self.author_id = author_id
        self.token = token
        self.message: discord.Message | None = None  # set after send

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the user who triggered the work command can respond."""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This isn't your work prompt!", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Take the safe payout", style=discord.ButtonStyle.secondary)
    async def safe(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.choice = "safe"
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Risk it for 2x (or nothing)", style=discord.ButtonStyle.danger)
    async def risky(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.choice = "risky"
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        self.choice = "safe"  # default to safe on timeout
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
        self.stop()


class WorkInfoView(discord.ui.View):
    """Persistent info button posted under every ,work reply.

    Tapping the button surfaces an ephemeral breakdown of every
    multiplicative modifier the work payout flows through (stones,
    user-created LP, buddy companion, group hall, rugpull crown) plus
    the progressive tax band and per-job daily cap. Centralised here
    so the explanation never drifts from the live values in
    ``_do_work`` (cogs/earn.py:_do_work). Times-out long, so the
    button stays clickable across a normal play session; the per-user
    interaction_check keeps it scoped to the original earner.
    """

    def __init__(self, *, author_id: int, ctx: DiscoContext) -> None:
        super().__init__(timeout=900.0)
        self.author_id = int(author_id)
        self.ctx = ctx
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This panel belongs to whoever ran `,work`.", ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            try:
                item.disabled = True  # type: ignore[attr-defined]
            except Exception:
                pass
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(
        label="What boosts my pay?", emoji="ℹ️",
        style=discord.ButtonStyle.secondary,
    )
    async def info_btn(
        self, interaction: discord.Interaction, _btn: discord.ui.Button,
    ) -> None:
        ctx = self.ctx
        prefix = ctx.prefix or ","
        # Live values so the explanation matches what the player would
        # actually earn right now -- LP cap from config, tax band from
        # config, stone bonus summed off the four primary stones.
        try:
            from services.liquidity import (
                user_created_lp_value_usd,
                user_lp_work_bonus_pct,
            )
            _lp_usd = await user_created_lp_value_usd(
                ctx.db, ctx.author.id, ctx.guild_id,
            )
            _lp_pct = user_lp_work_bonus_pct(_lp_usd)
        except Exception:
            _lp_usd, _lp_pct = 0.0, 0.0
        try:
            _lp_cap_usd = float(getattr(Config, "USER_LP_WORK_BONUS_CAP", 0.0))
        except Exception:
            _lp_cap_usd = 0.0
        try:
            _tax_threshold_h = to_human(Config.WORK_PROGRESSIVE_TAX_THRESHOLD)
            _tax_rate = float(Config.WORK_PROGRESSIVE_TAX_RATE)
        except Exception:
            _tax_threshold_h, _tax_rate = 0.0, 0.0

        e = (
            card("\U0001F4B5 Work payout breakdown", color=C_INFO)
            .description(
                "Your work payout is a base roll multiplied by every "
                "bonus you qualify for, then taxed if it lands above "
                "the high-earner threshold. Here's everything that "
                "moves the number on a `,work` receipt:"
            )
            .field(
                "\U0001F30A User-created LP",
                (
                    f"Pool you've added liquidity into for any "
                    f"user-created token (group tokens, tier-11 "
                    f"deploys, admin-added). Bonus is **USD-priced** "
                    f"so thin positions can't game it.\n"
                    f"-# Your LP: **{fmt_usd(_lp_usd)}**  ->  "
                    f"+**{_lp_pct*100:.2f}%** work bonus"
                    + (f"  (cap {fmt_usd(_lp_cap_usd)})" if _lp_cap_usd > 0 else "")
                ),
                False,
            )
            .field(
                "\U0001F4B0 Progressive tax",
                (
                    f"Earnings above **{fmt_usd(_tax_threshold_h)}** in a "
                    f"single session are taxed at **{_tax_rate*100:.0f}%** "
                    f"(burned, not redistributed). Stones/LP/buddy bonuses "
                    f"all apply BEFORE the tax check, so big rolls hit it "
                    f"first."
                ),
                False,
            )
            .field(
                "\U0001F48E Stones",
                (
                    "Hashstone, Lockstone, Vaultstone, Liqstone, plus "
                    "the meta stones (Gavel/Anvil/Chimera) all stack "
                    "their `work_daily_bonus` multiplicatively. Level "
                    "them up at `" + prefix + "shop`."
                ),
                False,
            )
            .field(
                "\U0001F43E Buddy companion",
                (
                    "Your active buddy adds a small `work` lane bonus "
                    "(capped). Train via `" + prefix + "buddy`."
                ),
                True,
            )
            .field(
                "\U0001F3DB️ Group hall",
                (
                    "Inside a Group Hall with the Gilded Arch upgrade, "
                    "every work session gets an extra `%` on top of the "
                    "normal multipliers. See `" + prefix + "group`."
                ),
                True,
            )
            .field(
                "\U0001F451 Rugpull King",
                (
                    f"The reigning Rugpull King earns "
                    f"+**{Config.RUGPULL_WORK_BONUS*100:.0f}%** base "
                    f"work income, scaling up to **+15%** after a 24h "
                    f"reign. See `" + prefix + "rugpull`."
                ),
                True,
            )
            .field(
                "⏱ Cooldown + caps",
                (
                    "Each job has its own work cooldown; a daily "
                    "streak of consecutive logins shaves up to 30% off "
                    "(`" + prefix + "daily`). Per-job WORK_DAILY_CAP "
                    "limits how much you can earn from a single job in "
                    "24h -- payouts get clamped if you hit it."
                ),
                False,
            )
            .footer(
                "Order of ops: base roll -> guild mult -> buddy -> stones -> "
                "user LP -> rugpull -> hall -> daily cap -> progressive tax."
            )
            .build()
        )
        await interaction.response.send_message(embed=e, ephemeral=True)


class _ApeConfirmView(discord.ui.View):
    """Confirmation prompt before aping in. Shows odds and potential gains/losses."""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=30.0)
        self.choice: str | None = None
        self.author_id = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your ape.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🦍 Ape In", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.choice = "confirm"
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Nevermind", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.choice = "cancel"
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        self.choice = "cancel"
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
        self.stop()


def _next_job_id(current: str) -> str | None:
    order = Config.JOB_ORDER
    idx = order.index(current) if current in order else 0
    if idx + 1 < len(order):
        return order[idx + 1]
    return None


# Endgame ``title_flair`` perk -> render text. Each post-EXPLOITER job
# carries one of these so the perks block reads differently per tier
# instead of repeating "deploy NFTs" / "create pools" four times in a
# row. The flair itself is decorative -- the real differentiation comes
# from the varied core bonuses + ``ape_bonus`` which IS wired below.
_JOB_TITLE_FLAIR: dict[str, str] = {
    "shield":     "\U0001F6E1️ Bug-bounty negotiator -- protocols owe you favors",
    "megaphone":  "\U0001F4E2 Cartel pump network -- 47k members on standby",
    "sequencer":  "⚙️ Sequencer privileges -- you order the L2 mempool",
    "genesis":    "\U0001F4DC Authored the genesis block -- mythical status",
}


def _render_perk_lines(perks: dict, *, compact: bool = False) -> list[str]:
    """Build a uniform perk-display list for both ,earn job and ,earn jobs.

    ``compact=True`` swaps to the shorter dot-separated form used by the
    paginated ladder; ``compact=False`` returns the verbose multi-line
    form used on a single user's job panel.
    """
    parts: list[str] = []
    sep_label = lambda label, val: (
        f"{label} {val}" if compact else f"{label}: **{val}**"
    )
    if "daily_bonus" in perks:
        v = f"+{perks['daily_bonus']*100:.0f}%"
        parts.append(sep_label("\U0001F5D3️", v) if compact
                     else f"\U0001F5D3️ Daily bonus: **{v}**")
    if "swap_fee" in perks:
        v = f"{perks['swap_fee']*100:.2f}%"
        parts.append(sep_label("\U0001F504", v) if compact
                     else f"\U0001F504 Swap fee: **{v}**")
    if "stake_bonus" in perks:
        v = f"+{perks['stake_bonus']*100:.0f}%"
        parts.append(sep_label("\U0001F4C8", v) if compact
                     else f"\U0001F4C8 Stake bonus: **{v}**")
    if "mining_bonus" in perks:
        v = f"+{perks['mining_bonus']*100:.0f}%"
        parts.append(sep_label("⛏️", v) if compact
                     else f"⛏️ Mining bonus: **{v}**")
    if "interest_bonus" in perks:
        v = f"+{perks['interest_bonus']*100:.0f}%"
        parts.append(sep_label("\U0001F3E6", v) if compact
                     else f"\U0001F3E6 Savings APY bonus: **{v}**")
    if "ape_bonus" in perks and float(perks["ape_bonus"] or 0) > 0:
        v = f"+{perks['ape_bonus']*100:.0f}%"
        parts.append(sep_label("\U0001F98D", v) if compact
                     else f"\U0001F98D Ape payout bonus: **{v}**")
    flair = perks.get("title_flair")
    if flair and flair in _JOB_TITLE_FLAIR:
        parts.append(_JOB_TITLE_FLAIR[flair])
    if perks.get("can_deploy_token"):
        parts.append("\U0001F3A8 deploy NFT collections" if compact
                     else "\U0001F3A8 Can deploy NFT collections")
    if perks.get("can_create_pool"):
        parts.append("\U0001F30A create pools" if compact
                     else "\U0001F30A Can create AMM pools")
    return parts


# ══════════════════════════════════════════════════════════════════════════════
#  Earn cog  -  merges Work + Daily under /earn group
# ══════════════════════════════════════════════════════════════════════════════

class Earn(commands.Cog):

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        # Per-user lock keyed by (user_id, guild_id) to prevent concurrent spam
        self._work_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._daily_locks: dict[tuple[int, int], asyncio.Lock] = {}
        # Inject Redis client for AI flavor cache
        global _flavor_redis
        if hasattr(bot, "bus") and bot.bus.is_connected:
            _flavor_redis = bot.bus._redis

    async def cog_check(self, ctx) -> bool:
        if ctx.guild:
            sub = ctx.invoked_subcommand or ctx.command
            cmd_name = sub.qualified_name if sub else ""
            if "ape" in cmd_name or "degen" in cmd_name or "yolo" in cmd_name:
                if not await module_allowed(ctx, "ape"):
                    raise commands.CheckFailure("The **ape** module is disabled on this server.")
            elif "daily" in cmd_name:
                if not await module_allowed(ctx, "daily"):
                    raise commands.CheckFailure("The **daily** module is disabled on this server.")
            else:
                if not await module_allowed(ctx, "work"):
                    raise commands.CheckFailure("The **work** module is disabled on this server.")
        return True

    # ── /earn (no subcommand)  -  show available earning commands ───────────────

    @commands.hybrid_group(name="earn", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def earn(self, ctx: DiscoContext) -> None:
        """Show available earning commands."""
        if await suggest_subcommand(ctx, self.earn):
            return
        embed = (
            card(
                "Earning Commands",
                description=(
                    "Use these commands to earn coins:\n\n"
                    "**`/earn work`** - work to earn coins (15-min cooldown)\n"
                    "**`/earn daily`** - claim your daily reward (24-hr cooldown, streak bonuses)\n"
                    "**`/earn ape`** - ape into a random shitcoin ($50 entry, high risk/reward)\n"
                    "**`/earn job`** - view your current job and stats\n"
                    "**`/earn jobs`** - see all job tiers and requirements\n"
                    "**`/earn promote`** - promote to the next job tier"
                ),
                color=C_TEAL,
            )
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .build()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── /earn work ────────────────────────────────────────────────────────────

    @earn.command(name="work")
    @guild_only
    @no_bots
    @ensure_registered
    async def work(self, ctx: DiscoContext) -> None:
        """Work to earn coins (cooldown). Pay scales with your job level."""
        # Prevent concurrent invocations from the same user (spam exploit fix)
        lock_key = (ctx.author.id, ctx.guild_id)
        lock = self._work_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You're already working! Wait for your current session to finish.")
            return

        async with lock:
            await self._do_work(ctx)

    async def _do_work(self, ctx: DiscoContext) -> None:
        """Inner work logic  -  called with user lock held."""
        row = ctx.user_row
        now = time.time()
        _lw = row["last_work"]
        last = _lw.timestamp() if hasattr(_lw, 'timestamp') else (_lw or 0.0)
        elapsed = now - last

        job = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_cfg = Config.JOBS.get(job["job_id"], Config.JOBS["HOMELESS"])

        # Per-job cooldown (falls back to global WORK_COOLDOWN)
        work_cd = job_cfg.get("work_cooldown", Config.WORK_COOLDOWN)

        # Reduce cooldown based on daily streak (tiered, capped at 30% reduction)
        streak_mult = _streak_work_multiplier(row["daily_streak"])
        if streak_mult < 1.0:
            work_cd = max(_WORK_CD_MIN, int(work_cd * streak_mult))

        if elapsed < work_cd:
            remaining = work_cd - elapsed
            m, s = divmod(int(remaining), 60)
            streak = row["daily_streak"]
            bonus_note = f" *(day {streak} streak: {int((1 - streak_mult) * 100)}% faster)*" if streak_mult < 1.0 else ""
            await ctx.reply_error(f"Still on cooldown. Come back in **{m}m {s}s**.{bonus_note}")
            return

        # Guard: if the bot can't respond in this channel, don't consume the cooldown.
        # Prevents the user from being silently stuck on cooldown after running
        # work in a channel where the bot has no send_messages permission.
        if ctx.guild and ctx.channel:
            perms = ctx.channel.permissions_for(ctx.guild.me)
            if not perms.send_messages:
                bot_chs = await ctx.db.get_bot_channels(ctx.guild_id)
                hint = ""
                if bot_chs:
                    mentions = " ".join(f"<#{c}>" for c in bot_chs[:3])
                    hint = f" Try one of the bot channels: {mentions}"
                try:
                    await ctx.author.send(
                        f"I can't send messages in <#{ctx.channel.id}>, so your `.work` was cancelled.{hint}"
                    )
                except Exception:
                    pass
                return

        # Set cooldown IMMEDIATELY so a queued duplicate can't pass the check.
        # (Previously set at the end of work logic, leaving a window where a
        # second queued command could also pass the cooldown check.)
        await ctx.db.set_cooldown(ctx.author.id, ctx.guild_id, "last_work")

        earn_min_raw, earn_max_raw = job_cfg["earn"]
        earn_min = to_human(earn_min_raw)
        earn_max = to_human(earn_max_raw)

        base_amount = round(random.uniform(earn_min, earn_max), 2)

        # Guild work multiplier (admin-configurable)
        _guild_settings = await ctx.db.get_guild_settings(ctx.guild_id)
        _work_mult = float(_guild_settings.get("work_multiplier") or 1.0)
        if _work_mult != 1.0:
            base_amount = round(base_amount * _work_mult, 2)

        # Buddy companion multiplier (capped at ~2% per spec).
        _buddy_pct = 0.0
        _buddy_added = 0.0
        try:
            from services.buddy_bonus import buddy_bonus
            _buddy_mult = await buddy_bonus(ctx.db, ctx.guild_id, ctx.author.id, lane="work")
            if _buddy_mult > 1.0:
                _buddy_pct = _buddy_mult - 1.0
                _pre_buddy = base_amount
                base_amount = round(base_amount * _buddy_mult, 2)
                _buddy_added = base_amount - _pre_buddy
        except Exception:
            pass  # buddy subsystem must never break earn

        # Apply stone level bonuses before interactive choice (so both branches benefit).
        # All four stones contribute work_daily_bonus -- the shop advertises it on
        # every stone (see cogs/shop.py:_leveled_field calls for each), so every
        # stone must actually apply here too or the value the shop promises is a lie.
        hashstone   = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
        lockstone  = await ctx.db.get_lockstone(ctx.author.id, ctx.guild_id)
        vaultstone = await ctx.db.get_vaultstone(ctx.author.id, ctx.guild_id)
        liqstone   = await ctx.db.get_liqstone(ctx.author.id, ctx.guild_id)
        stone_bonus = (
            _item_stat(hashstone, "work_daily_bonus")
            + _lockstone_stat(lockstone, "work_daily_bonus")
            + _vaultstone_stat(vaultstone, "work_daily_bonus")
            + _liqstone_stat(liqstone, "work_daily_bonus")
        )
        # Meta-economy stones (Gavelstone / Anvilstone / Chimerastone)
        # all advertise work_daily_bonus too. Sum them here so the value
        # the shop promises actually shows up in /work earnings.
        for _meta_key in ("gavelstone", "anvilstone", "chimerastone"):
            try:
                _meta_row = await getattr(ctx.db, f"get_{_meta_key}")(
                    ctx.author.id, ctx.guild_id,
                )
            except Exception:
                _meta_row = None
            if not _meta_row:
                continue
            _meta_per = float(
                Config.SHOP_ITEMS.get(_meta_key, {}).get("stats", {})
                .get("work_daily_bonus", 0.0)
            )
            stone_bonus += _meta_per * int(_meta_row.get("level") or 0)
        stone_added = 0.0
        if stone_bonus > 0:
            pre_stone = base_amount
            base_amount = round(base_amount * (1.0 + stone_bonus), 2)
            stone_added = base_amount - pre_stone

        # User-created-token LP bonus: USD-priced so thin positions don't
        # game it, capped by Config.USER_LP_WORK_BONUS_CAP. Applied
        # multiplicatively on top of stones so bonuses compound cleanly.
        # "User-created" covers every guild_tokens row -- group tokens,
        # tier-11 deploys, and admin-added tokens alike.
        _user_lp_bonus = 0.0
        _user_lp_added = 0.0
        _user_lp_usd   = 0.0
        try:
            from services.liquidity import user_created_lp_value_usd, user_lp_work_bonus_pct
            _user_lp_usd = await user_created_lp_value_usd(
                ctx.db, ctx.author.id, ctx.guild_id,
            )
            _user_lp_bonus = user_lp_work_bonus_pct(_user_lp_usd)
        except Exception:
            log.exception("work: user-LP bonus lookup failed; dropping to 0")
        if _user_lp_bonus > 0:
            _pre_ulp = base_amount
            base_amount = round(base_amount * (1.0 + _user_lp_bonus), 2)
            _user_lp_added = base_amount - _pre_ulp

        # Rugpull King bonus: scaling work income (+5% base, up to +15% after 24h reign)
        _rug_bonus = 0.0
        if Config.RUGPULL_WORK_BONUS > 0:
            try:
                from cogs.rugpull import has_rugpull_role, _get_king, _compute_reign_perks
                if isinstance(ctx.author, discord.Member) and await has_rugpull_role(ctx.author):
                    _rug_king = await _get_king(ctx.db, ctx.guild_id)
                    if _rug_king and _rug_king["user_id"] == ctx.author.id:
                        _work_bonus, _ = _compute_reign_perks(_rug_king)
                    else:
                        _work_bonus = Config.RUGPULL_WORK_BONUS
                    _rug_pre = base_amount
                    base_amount = round(base_amount * (1.0 + _work_bonus), 2)
                    _rug_bonus = base_amount - _rug_pre
            except Exception:
                pass

        # ~10% chance of interactive risk/reward choice
        interactive_msg = None
        _risky_original_amt: float = 0.0  # set when risky=won; corrected below if capped
        _outcome_claimed_amt: float = base_amount  # amount the flavor text was written about
        if random.random() < 0.10:
            risky_amt = round(base_amount * 2, 2)
            template, company = random.choice(_WORK_FLAVORS)
            fallback_preview = template.format(company=company, amount=fmt_usd(base_amount))
            ai_flags = await ctx.db.get_ai_flags(ctx.guild_id)
            if ai_flags["flavor"] and Config.OPENROUTER_API_KEY:
                _others = await get_random_active_players(
                    ctx.guild, ctx.db, exclude_user_id=ctx.author.id, count=2,
                )
                social_preview = await _get_social_ai_flavor(
                    "work", "earn", ctx.author.display_name, _others,
                    fmt_usd(base_amount), job_title=job_cfg["title"],
                )
                flavor_preview = social_preview or await _get_ai_flavor(
                    job["job_id"], job_cfg["title"], fmt_usd(base_amount),
                ) or fallback_preview
            else:
                flavor_preview = fallback_preview

            view = WorkChoiceView(safe_amt=base_amount, risky_amt=risky_amt, author_id=ctx.author.id)
            _expires_at = int(time.time() + 30)
            prompt_embed = (
                card(
                    None,
                    description=(
                        f"{flavor_preview}\n\n"
                        f"**Side hustle opportunity:** Take **${base_amount:,.2f}** guaranteed, "
                        f"or gamble for **${risky_amt:,.2f}** (50/50 chance of nothing).\n\n"
                        f"Expires {fmt_ts(int(_expires_at))}"
                    ),
                    color=C_AMBER,
                )
                .author(f"{job_cfg['title']}", icon_url=ctx.author.display_avatar.url)
                .build()
            )
            interactive_msg = await ctx.reply(embed=prompt_embed, view=view, mention_author=False)
            view.message = interactive_msg

            await view.wait()

            if view.choice == "risky":
                won = random.random() < 0.5
                amount = risky_amt if won else 0.0
                if won:
                    _risky_original_amt = risky_amt
                    _outcome_claimed_amt = risky_amt
                else:
                    _outcome_claimed_amt = 0.0
                outcome_str = (
                    f"You risked it and **won ${risky_amt:,.2f}**!" if won
                    else "You risked it and **got nothing**. Degen moment."
                )
            else:
                amount = base_amount
                outcome_str = f"Safe play. Took the **${base_amount:,.2f}**."
        else:
            amount = base_amount
            template, company = random.choice(_WORK_FLAVORS)
            fallback_str = template.format(company=company, amount=fmt_usd(amount))
            ai_flags = await ctx.db.get_ai_flags(ctx.guild_id)
            if ai_flags["flavor"] and Config.OPENROUTER_API_KEY:
                # Try social flavor (with other player names) first
                _others = await get_random_active_players(
                    ctx.guild, ctx.db, exclude_user_id=ctx.author.id, count=2,
                )
                social = await _get_social_ai_flavor(
                    "work", "earn", ctx.author.display_name, _others,
                    fmt_usd(amount), job_title=job_cfg["title"],
                )
                outcome_str = social or await _get_ai_flavor(
                    job["job_id"], job_cfg["title"], fmt_usd(amount),
                ) or fallback_str
            else:
                outcome_str = fallback_str

        # Hall bonus: extra % when work is used inside a Group Hall with Gilded Arch
        _hall_work_pct = getattr(ctx, "hall_bonus", {}).get("work", 0.0)
        _hall_work_added = 0.0
        if _hall_work_pct > 0 and amount > 0:
            pre_hall = amount
            amount = round(amount * (1.0 + _hall_work_pct), 2)
            _hall_work_added = amount - pre_hall

        # Daily work income cap: prevent session-grinding from flooding money supply
        if amount > 0 and Config.WORK_DAILY_CAP:
            job_id = job["job_id"]
            daily_cap_h = to_human(Config.WORK_DAILY_CAP.get(job_id, 0))
            if daily_cap_h > 0:
                try:
                    today_start = int(time.time() // 86400) * 86400
                    today_earned_h = to_human(await ctx.db.get_work_today(ctx.author.id, ctx.guild_id, since_ts=today_start))
                    if today_earned_h + amount > daily_cap_h:
                        amount = max(0.0, daily_cap_h - today_earned_h)
                        amount = round(amount, 2)
                except Exception:
                    log.exception("work daily cap check failed for user %s guild %s", ctx.author.id, ctx.guild_id)

        # Progressive tax on high earnings
        work_tax = 0.0
        _tax_threshold_h = to_human(Config.WORK_PROGRESSIVE_TAX_THRESHOLD)
        if amount > _tax_threshold_h:
            excess = amount - _tax_threshold_h
            work_tax = excess * Config.WORK_PROGRESSIVE_TAX_RATE
            amount -= work_tax  # tax is burned (not redistributed)
            amount = round(amount, 2)

        # Apex Mastery: Overtime Hustle (econ.work_bonus) scales the
        # post-tax payout by the cumulative passive value from the
        # economy branch. No effect if the user has never unlocked the
        # node (passive returns 0 -> base unchanged).
        if amount > 0:
            try:
                from services import mastery as _m
                amount = round(
                    await _m.apply_passive(
                        ctx.db, ctx.author.id, ctx.guild_id,
                        "econ.work_bonus", amount, mode="mul",
                    ),
                    2,
                )
            except Exception:
                log.debug(
                    "econ.work_bonus passive read failed",
                    exc_info=True,
                )

        # Random surprise event (rare positive bonus). Only fires when there's
        # a real session to attach it to (skipped on cap-zeroed payouts so a
        # jackpot multiplier doesn't trivially round to nothing).
        surprise = None
        if amount > 0:
            surprise = await _roll_surprise(
                amount, ctx.db, ctx.author.id, ctx.guild_id,
                chance=_WORK_SURPRISE_CHANCE,
            )
            if surprise:
                if surprise["kind"] == "jackpot":
                    amount = round(amount * surprise["multiplier"], 2)
                elif surprise["kind"] == "treasure":
                    amount = round(amount + surprise["flat_bonus"], 2)

        # Wealth Bottleneck: scale this credit by the player's leaderboard
        # rank. Top of the LB has gains throttled, bottom gets a USD boost
        # from the same per-guild pool. See services.bottleneck.
        _bn_result = None
        if amount > 0:
            from services.bottleneck import apply_bottleneck, CreditKind
            _gross_raw = to_raw(amount)
            _bn_result = await apply_bottleneck(
                ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
                gross_raw=_gross_raw, kind=CreditKind.WORK,
            )
            new_wallet_raw = await ctx.db.update_wallet(
                ctx.author.id, ctx.guild_id, _bn_result.total_to_wallet_raw,
            )
            # Replace the embed's "amount" with the post-bottleneck net so
            # every downstream field (Earned line, payout PNG, log_tx) sees
            # the same number the wallet just received.
            amount = round(
                (_bn_result.net_credit_raw + _bn_result.boost_wallet_raw) / 10**18,
                2,
            )
        else:
            _gross_raw = 0
            new_wallet_raw = row["wallet"]

        work_tx = ""
        if amount > 0:
            work_tx = await ctx.db.log_tx(
                ctx.guild_id, ctx.author.id, "WORK",
                symbol_out="USD", amount_out=to_raw(amount),
                network="usd",
            )
            await ctx.bot.bus.publish("work_completed", guild=ctx.guild,
                user=ctx.author, amount=amount, job_title=job_cfg["title"],
                tx_hash=work_tx)

        # Update job stats (count the session regardless of zero payout)
        new_work_count = job["work_count"] + 1
        new_earned_raw = job["total_earned"] + to_raw(amount)
        await ctx.db.update_job(ctx.author.id, ctx.guild_id, job["job_id"], new_work_count, new_earned_raw)

        cooldown_str = f"{work_cd // 60}m"

        # If capping/scaling reduced the payout below what the flavor text claimed,
        # fix the description so the embed color, earned field, and text all agree.
        # Only the per-job WORK_DAILY_CAP can zero out a payout now -- the
        # aggregate daily income cap is gone.
        if _risky_original_amt > 0 and amount < _risky_original_amt:
            # Risky-win path specifically
            if amount == 0:
                outcome_str = (
                    f"You risked it and technically **won ${_risky_original_amt:,.2f}**... "
                    f"but you've maxed today's pay for this job. **$0 paid out.**"
                )
            else:
                outcome_str = (
                    f"You risked it and **won**! Today's job cap limited the payout to "
                    f"**${amount:,.2f}** (from ${_risky_original_amt:,.2f})."
                )
        elif amount == 0 and _outcome_claimed_amt > 0:
            # Regular work path: flavor claimed an amount but cap reduced to $0
            outcome_str = (
                f"You've maxed today's pay for this job. **$0 paid out** this session."
            )

        _b = (
            card(None, description=outcome_str, color=C_TEAL if amount > 0 else C_ERROR)
            .author(f"💼 {job_cfg['title']}", icon_url=ctx.author.display_avatar.url)
        )

        _earned_parts = []
        if amount > 0:
            _earned_parts.append(f"**+${amount:,.2f}**")
        else:
            _earned_parts.append("**$0.00** (degen moment)")
        if _rug_bonus > 0 and amount > 0:
            _earned_parts.append(f"👑 +{Config.RUGPULL_WORK_BONUS*100:.0f}% King (+${_rug_bonus:,.2f})")
        if stone_bonus > 0 and amount > 0:
            _earned_parts.append(f"💎 +{stone_bonus*100:.0f}% (+${stone_added:,.2f})")
        if _user_lp_added > 0 and amount > 0:
            _earned_parts.append(
                f"🌊 +{_user_lp_bonus*100:.1f}% User LP "
                f"(${_user_lp_usd:,.0f}, +${_user_lp_added:,.2f})"
            )
        if _buddy_added > 0 and amount > 0:
            _earned_parts.append(f"🐾 +{_buddy_pct*100:.1f}% Buddy (+${_buddy_added:,.2f})")
        if _hall_work_added > 0 and amount > 0:
            _earned_parts.append(f"🏛️ +{_hall_work_pct*100:.0f}% Hall (+${_hall_work_added:,.2f})")
        if work_tax > 0:
            _earned_parts.append(f"🏛️ Tax: -${work_tax:,.2f}")
        if _bn_result is not None and not _bn_result.skipped:
            _bn_drag = float(_bn_result.drag_usd_raw) / 10**18
            _bn_boost = float(_bn_result.boost_wallet_raw) / 10**18
            if _bn_drag > 0:
                _earned_parts.append(
                    f"⚖️ Bottleneck x{_bn_result.multiplier:.2f}: -${_bn_drag:,.2f} to pool"
                )
            elif _bn_boost > 0:
                _earned_parts.append(
                    f"⚖️ Bottleneck x{_bn_result.multiplier:.2f}: +${_bn_boost:,.2f} from pool"
                )
        _b.field("💵 Earned", "\n".join(_earned_parts), True)
        _b.field("📊 Sessions", f"**{new_work_count}**", True)
        _b.field("💰 Wallet", f"**${to_human(new_wallet_raw):,.2f}**", True)

        if surprise:
            if surprise["kind"] == "jackpot":
                detail = f"\n**Bonus: x{surprise['multiplier']:.0f} payout**"
            elif surprise["kind"] == "treasure":
                detail = f"\n**Bonus: +${surprise['flat_bonus']:,.2f}**"
            else:
                detail = "\n*Added to your inventory.*"
            _b.field(surprise["title"], f"_{surprise['flavor']}_{detail}", False)

        # Check promotion eligibility (use full net worth across all assets)
        next_id = _next_job_id(job["job_id"])
        promo_ready = False
        if next_id:
            next_cfg = Config.JOBS[next_id]
            _nw = await compute_net_worth(ctx.author.id, ctx.guild_id, ctx.db)
            net_worth = _nw.total
            if new_work_count >= next_cfg["min_work"] and net_worth >= next_cfg["min_wealth"]:
                promo_ready = True

        if promo_ready:
            _b.field(
                "\u200b",
                f"🎉 **PROMOTION AVAILABLE** - you qualify for **{next_cfg['title']}**!\n"
                f"Run `/earn promote` to rank up.",
                False,
            )

        _b.footer(f"⏱ Next work in {cooldown_str}"
                  + (" • ⬆️ Promotion Available!" if promo_ready else ""))
        result_embed = _b.build()

        # V3: Pillow work-receipt card, themed to the player's equipped
        # cosmetics so it matches their ,level / ,profile look.
        _work_file = None
        try:
            from services.payout_render import render_payout_card
            from services import cosmetics as _cos
            import io as _io
            _avatar_bytes = None
            if ctx.author.display_avatar:
                try:
                    _avatar_bytes = await ctx.author.display_avatar.read()
                except Exception:
                    pass
            _equipped = {}
            try:
                _equipped = await _cos.equipped(ctx.db, ctx.author.id)
            except Exception:
                pass
            _gross_h = to_human(_gross_raw) if amount > 0 else 0.0
            _tax_h = (
                max(0.0, float(_bn_result.drag_usd_raw) / 10**18)
                if (amount > 0 and _bn_result is not None) else 0.0
            )
            _bonus_h = (
                max(0.0, float(_bn_result.boost_wallet_raw) / 10**18)
                if (amount > 0 and _bn_result is not None) else 0.0
            )
            _bonuses_w: list[tuple[str, str]] = []
            if _bn_result is not None and not _bn_result.skipped:
                _bn_drag_h = float(_bn_result.drag_usd_raw) / 10**18
                _bn_boost_h = float(_bn_result.boost_wallet_raw) / 10**18
                if _bn_drag_h > 0:
                    _bonuses_w.append((
                        "Bottleneck",
                        f"-${_bn_drag_h:,.2f} (x{_bn_result.multiplier:.2f})",
                    ))
                elif _bn_boost_h > 0:
                    _bonuses_w.append((
                        "Bottleneck",
                        f"+${_bn_boost_h:,.2f} (x{_bn_result.multiplier:.2f})",
                    ))
            _png = render_payout_card(
                user_name=ctx.author.display_name,
                avatar_bytes=_avatar_bytes,
                title="Work Session",
                # user_jobs has no `level` column -- the old card faked
                # ``Level {job.get('level', 1)}`` which always rendered
                # ``Level 1`` regardless of actual work_count. Use the
                # real ``work_count`` as the session number on both
                # subtitle and badge so a player with 50 sessions sees
                # "Session 50" / "S50" instead of a phantom "Lv 1".
                subtitle=(
                    f"{job_cfg.get('title', 'Job')}  -  "
                    f"Session {int(job.get('work_count', 0))}"
                ),
                badge_text=f"S{int(job.get('work_count', 0))}",
                badge_color=C_PURPLE,
                accent_color=C_AMBER,
                reward_usd=float(amount),
                gross_usd=float(_gross_h),
                tax_usd=float(_tax_h),
                bonus_usd=float(_bonus_h),
                bonuses=_bonuses_w,
                new_wallet_usd=to_human(int(new_wallet_raw)),
                footer="V3 Work",
                equipped=_equipped,
            )
            _work_file = discord.File(_io.BytesIO(_png), filename="work.png")
            result_embed.set_image(url="attachment://work.png")
        except Exception:
            log.debug("work: PNG render failed; sending embed only", exc_info=True)

        # For interactive sessions, edit the original message; otherwise send fresh.
        # Either path attaches the WorkInfoView so the player gets a
        # tappable "what boosts my pay?" affordance under every receipt
        # without having to memorise the formulas.
        info_view = WorkInfoView(author_id=ctx.author.id, ctx=ctx)
        if interactive_msg is not None:
            try:
                from core.framework.links import sanitize_embed

                sanitize_embed(result_embed)
                _edit_kwargs = {"embed": result_embed, "view": info_view}
                if _work_file is not None:
                    _edit_kwargs["attachments"] = [_work_file]
                await interactive_msg.edit(**_edit_kwargs)
                info_view.message = interactive_msg
            except Exception:
                _kw = {"embed": result_embed, "view": info_view,
                       "mention_author": False}
                if _work_file is not None:
                    _kw["file"] = _work_file
                sent = await ctx.reply(**_kw)
                info_view.message = sent
        else:
            _kw = {"embed": result_embed, "view": info_view,
                   "mention_author": False}
            if _work_file is not None:
                _kw["file"] = _work_file
            sent = await ctx.reply(**_kw)
            info_view.message = sent

    # ── /earn ape ─────────────────────────────────────────────────────────────

    # Base cost scales with job tier  -  whales risk more, newcomers risk less.
    # Multipliers keyed by job_id; missing IDs default to 1.0.
    _APE_BASE_COST = 50.0
    _APE_JOB_MULTIPLIERS: dict[str, float] = {
        "HOMELESS":            0.4,    # $20
        "TWITTER_SHILL":       0.5,    # $25
        "AIRDROP_FARMER":      0.6,    # $30
        "POAP_HUNTER":         0.7,    # $35
        "LARPER":              0.8,    # $40
        "WHITELIST_FARMER":    1.0,    # $50
        "NFT_FLIPPER":         1.5,    # $75
        "SHITCOIN_TRENCHER":   2.0,    # $100
        "DISCORD_MOD":         4.0,    # $200
        "CT_INFLUENCER":       6.0,    # $300
        "DEFI_DEGEN":          8.0,    # $400
        "YIELD_FARMER":       12.0,    # $600
        "TRADER":             16.0,    # $800
        "MEV_SEARCHER":       22.0,    # $1,100
        "COURSE_SELLER":      30.0,    # $1,500
        "ANALYST":            42.0,    # $2,100
        "VALIDATOR_OP":       60.0,    # $3,000
        "VC_PARTNER":         88.0,    # $4,400
        "PROTOCOL_DEV":      120.0,    # $6,000
        "EXPLOITER":         250.0,    # $12,500
        "WHITE_HAT":         400.0,    # $20,000
        "CARTEL_BOSS":       600.0,    # $30,000
        "L2_FOUNDER":        900.0,    # $45,000
        "SATOSHI":         1_500.0,    # $75,000
    }

    # Supports {amount} substitution  -  inject the entry cost (loss) or reward at runtime.
    _APE_FAIL = [
        "You ignored the 'Unverified Contract' warning because the logo was a cute Shiba Inu. The dev pulled liquidity before the launch tweet finished loading. You lost **{amount}**.",
        "$LUNA was 'mathematically sound' and you bought the dip all the way to zero. Do Kwon sends his regards from a Montenegrin prison cell. You lost **{amount}**.",
        "The roadmap promised a 'Metaverse Gaming Hub'. What they built was a bridge to the founder's offshore account. The website is now a 404. You lost **{amount}**.",
        "You trusted an audit done by a firm with a cool-looking shield logo. The firm was the dev's second Twitter account. You lost **{amount}**.",
        "Bitconnect 2.0 promised 3% daily returns. You really thought Carlos Matos wouldn't let you down twice. Wassa-wassa-wassa-wasted. You lost **{amount}**.",
        "You forgot to revoke permissions on a 'Free Mint' site that looked like a 2005 Geocities page. Your wallet drained faster than an FTX balance sheet. You lost **{amount}**.",
        "Liquidity was locked for '100 years' but the dev found a backdoor to mint a quadrillion tokens. The chart looks like a cliff face. You lost **{amount}**.",
        "You followed a KOL's '1000x Gem' call. He was dumping his allocation into your buy order in real time. You lost **{amount}**.",
        "You stored savings in a Celsius-adjacent yield protocol because the APY promised 'financial freedom'. The only thing free now is your empty wallet. You lost **{amount}**.",
        "You diamond-handed a memecoin through a 90% drawdown, convinced it was a healthy correction. It wasn't a correction. It was a eulogy. You lost **{amount}**.",
        "The bridge you used to move funds got hacked by a state-sponsored group. The 'Emergency Pause' happened five seconds after your transaction confirmed. You lost **{amount}**.",
        "The token had a rug delay mechanism built into the contract. You discovered this after the delay expired. You lost **{amount}**.",
        "You aped into a coin named $SAFU. You should have recognized the hubris in that branding. You lost **{amount}**.",
        "The whitepaper was a single page: a lambo jpeg and the phrase 'trust the vision'. You trusted the vision. You lost **{amount}**.",
        "The 'doxxed team' turned out to be AI-generated face swaps over stock photos. You found this out post-launch. You lost **{amount}**.",
        "You bought the top so precisely that the chart appears to have been waiting for you. Textbook market timing. You lost **{amount}**.",
        "The contract had a hidden mint function only the dev could call. He called it twice before the Telegram deleted itself. You lost **{amount}**.",
        "You aped in because the Telegram had 50,000 members. Forty-nine thousand nine hundred and ninety-seven were bots. You lost **{amount}**.",
        "The token name was $TRUST. You trusted it. The irony will accompany you for a while. You lost **{amount}**.",
        "Website: one page, one countdown timer. The countdown reached zero. So did the price. You lost **{amount}**.",
        "You spent $300 in gas to buy a token worth $4. The gas fee was the real product all along. You lost **{amount}**.",
        "The devs doxxed themselves mid-rug. Bold move. The doxx turned out to be deepfakes. You lost **{amount}**.",
        "Token supply: 1 trillion. Circulating: 999 billion. Dev wallet: 999 billion. You lost **{amount}**.",
        "The chart formed a perfect head-and-shoulders. Your buy was the right shoulder. You lost **{amount}**.",
        "Community voted on the roadmap. Step 1: dump. There was no Step 2 on the agenda. You lost **{amount}**.",
    ]

    _APE_BREAK_EVEN = [
        "Token pumped 2× then dumped back to earth. You got out in the two-second window between. Called it 'risk management'. You walked away with **{amount}**.",
        "Not a rug, not a moon. Just... mid. Like a centralized stablecoin that actually stayed stable. You walked away with **{amount}**.",
        "You sold at breakeven and watched it pump 400% without you. Emotional damage: immeasurable. Financial damage: minimal. You walked away with **{amount}**.",
        "Chart went sideways for six hours. You rage-sold at cost. It dumped 80% three minutes later. Accidental genius. You walked away with **{amount}**.",
        "Dev launched V2 after V1 dumped 70%. You sold V1 into V2 hype. Breakeven was the highest it ever got. You walked away with **{amount}**.",
        "You timed the bottom perfectly and sold twelve seconds too early. The 8× pump was for someone else. You walked away with **{amount}**.",
        "Token had real utility. Nobody cared. You held for three weeks waiting for the market to notice. It didn't. You walked away with **{amount}**.",
        "The coin pumped 12× while you were sleeping. It dumped 12× before your alarm went off. Net: flat. You walked away with **{amount}**.",
        "Made back your gas fees and nothing else. In this economy, a moral victory is the only kind. You walked away with **{amount}**.",
        "You entered with conviction and exited with mild dignity. Could have been worse. Could have been Bitconnect. You walked away with **{amount}**.",
    ]

    _APE_WIN = [
        "You found a gem in a Discord with twelve members. All twelve of you are now planning early retirement. You walked away with **{amount}**.",
        "CT said the token was dead. You bought the dead cat bounce. The cat had nine lives and used all of them. You walked away with **{amount}**.",
        "Stealth launch. No influencers. No pre-sale. No insider allocation. Pure degen conviction that paid off. You walked away with **{amount}**.",
        "Some KOL called it a '10x gem' ten minutes after you bought. You were the alpha leak. He was your exit liquidity. You walked away with **{amount}**.",
        "You read the contract source before aping. It was clean. You sized in heavy. Due diligence worked for once. You walked away with **{amount}**.",
        "The token name was misspelled. The gains were not. You walked away with **{amount}**.",
        "The dev actually delivered the roadmap on schedule. First time anyone in this space has done that. You were positioned correctly. You walked away with **{amount}**.",
        "You bought the dip on a 'dead' token that turned out to be hibernating, not dead. Diamond hands, black P&L. You walked away with **{amount}**.",
        "Early to the next Stratum. In before the CT threads, before the influencers, before the normies. You walked away with **{amount}**.",
        "You were the whale all along. They just didn't have your on-chain data to realize it. You walked away with **{amount}**.",
        "A 100× on a dog coin nobody had heard of. You are the alpha. CT is now DMing you for calls. You walked away with **{amount}**.",
        "The community took over after the dev ragequit. You held through the chaos. Turns out the community could actually build. You walked away with **{amount}**.",
        "You market-bought at the absolute bottom tick. The chart now looks like your initials. You walked away with **{amount}**.",
    ]

    _APE_LEGENDARY = [
        "Fifty-ex. You found the next Shiba Inu before the Shiba Inu crowd found it. Generational wealth, at least on paper. You walked away with **{amount}**.",
        "The token was a community joke. The gains were not. You held while everyone else paper-handed. You walked away with **{amount}**.",
        "CT is screenshotting your PnL. You have notifications off. You're already positioned in the next one. You walked away with **{amount}**.",
        "The dev ragequit and the community took over and then they actually delivered. You held through all of it. You walked away with **{amount}**.",
        "You market-bought at the literal bottom tick. The chart has your name on it now. Technically. You walked away with **{amount}**.",
        "A documentary crew wants to interview you about this trade. You declined. You're already in the next position. You walked away with **{amount}**.",
        "You had a 50× on a coin with a typo in its name. The gains were spelled correctly. You walked away with **{amount}**.",
    ]

    _APE_ASCENDED = [
        "One hundred times. This wasn't luck. The universe bent its probability function in your direction and you were ready for it. You walked away with **{amount}**.",
        "The token was four minutes old and you're already calculating retirement. They'll write case studies about this entry. You walked away with **{amount}**.",
        "Your PnL screenshot just crashed Crypto Twitter. Three infrastructure providers went down simultaneously. You did this. You walked away with **{amount}**.",
        "They will study this trade in behavioral economics classes as 'irrational conviction that turned out to be correct.' You are the textbook. You walked away with **{amount}**.",
    ]

    _APE_DRAINED = [
        "You signed a transaction you didn't read. Your entire DeFi wallet got drained. All of it. Entry cost was the least of your losses today. You paid **{amount}** to learn this lesson.",
        "That 'free mint' link was a wallet drainer. Every token across every chain  -  gone. Your entry fee was a rounding error in the damage. You paid **{amount}** to find out.",
        "BITCONNEEEEECT! Hey hey hey! Your DeFi wallets? Wasa wasa wasa... emptied. The entry was just the cover charge. You paid **{amount}** to get in the door.",
        "You approved unlimited spend on a contract called `TotallyNotADrainer.sol`. Narrator: it was. The ape entry was the least of it. You paid **{amount}** for the privilege.",
        "Clipboard malware swapped your address. You sent every DeFi token to a stranger's wallet. Your ape entry was just the opening act. You paid **{amount}** and then some.",
        "A 'Uniswap airdrop' popped up. You connected your wallet. They disconnected your funds. Entry fee was a rounding error. You paid **{amount}** to find out.",
        "You clicked 'Revoke Approvals' on a phishing site. It approved everything instead. The ape entry barely registered in the carnage. You paid **{amount}** to get started.",
        "Someone DMed you 'ser your wallet is at risk.' They were right  -  because you clicked their link. The entry fee was nothing compared to what followed. You paid **{amount}**.",
        "An NFT appeared in your wallet. You tried to list it. The hidden contract drained everything. Your ape bet was just the appetizer. You paid **{amount}** for the full meal.",
        "You pasted your seed phrase into a 'wallet migration tool.' It migrated your funds to someone else's wallet. The entry cost was the least of it. You paid **{amount}**.",
    ]

    # Payout ranges as multiples of entry cost
    # EV ≈ 0.72x  -  the house wins long-term, but big hits keep it exciting
    _APE_PAYOUTS = {
        #                (min_mult, max_mult)
        "drained":      (0.0,    0.0),
        "rugged":       (0.0,    0.0),
        "break_even":   (0.8,    1.5),
        "moon":         (5.0,   12.0),
        "legendary":    (15.0,  30.0),
        "ascended":     (50.0, 100.0),
    }

    @earn.command(name="ape", aliases=["degen", "yolo"])
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(300)
    async def ape(self, ctx: DiscoContext) -> None:
        """Ape into a random shitcoin. Cost scales with job tier. High risk, high reward."""
        # Scale cost by job tier
        job = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_id = job.get("job_id", "HOMELESS") if job else "HOMELESS"
        job_mult = self._APE_JOB_MULTIPLIERS.get(job_id, 1.0)

        ape_cost = round(self._APE_BASE_COST * job_mult, 2)

        _ape_gs = await ctx.db.get_guild_settings(ctx.guild_id)
        _ape_mult = float(_ape_gs.get("ape_multiplier") or 1.0)

        user = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        wallet = user.h("wallet")

        if wallet < ape_cost:
            ctx.command.reset_cooldown(ctx)
            await ctx.reply_error(
                f"You need **${ape_cost:,.0f}** to ape in, but you only have **${wallet:,.2f}**.\n"
                f"Go `{ctx.prefix}work` until you can afford to degen."
            )
            return

        # Build odds preview
        payouts = self._APE_PAYOUTS
        # Check King of Rugs status before building preview; scale ape bonus by reign duration
        _is_rug_king = False
        _ape_bonus_rate = Config.RUGPULL_APE_BONUS
        if Config.RUGPULL_APE_BONUS > 0:
            try:
                from cogs.rugpull import has_rugpull_role, _get_king, _compute_reign_perks
                if isinstance(ctx.author, discord.Member) and await has_rugpull_role(ctx.author):
                    _is_rug_king = True
                    _rug_king_row = await _get_king(ctx.db, ctx.guild_id)
                    if _rug_king_row and _rug_king_row["user_id"] == ctx.author.id:
                        _, _ape_bonus_rate = _compute_reign_perks(_rug_king_row)
            except Exception:
                pass

        # Endgame jobs (CARTEL_BOSS, L2_FOUNDER, SATOSHI, ...) carry an
        # ``ape_bonus`` perk that compounds on top of the King-of-Rugs
        # bonus. Wired here so each endgame tier gets a distinct, real
        # impact instead of just decorative perk text.
        job_cfg_for_ape = Config.JOBS.get(job_id, Config.JOBS["HOMELESS"])
        _job_ape_bonus = float(job_cfg_for_ape.get("perks", {}).get("ape_bonus", 0.0) or 0.0)

        _bonus_mult = (1.0 + _ape_bonus_rate if _is_rug_king else 1.0) * (1.0 + _job_ape_bonus)
        preview = (
            f"**Entry cost: ${ape_cost:,.0f}**\n"
            f"```\n"
            f"84.00%  Rugged         $0 (lose ${ape_cost:,.0f})\n"
            f" 9.49%  Break even     ${ape_cost * payouts['break_even'][0] * _bonus_mult:,.0f} - ${ape_cost * payouts['break_even'][1] * _bonus_mult:,.0f}\n"
            f" 4.50%  Moon           ${ape_cost * payouts['moon'][0] * _bonus_mult:,.0f} - ${ape_cost * payouts['moon'][1] * _bonus_mult:,.0f}\n"
            f" 1.00%  Legendary      ${ape_cost * payouts['legendary'][0] * _bonus_mult:,.0f} - ${ape_cost * payouts['legendary'][1] * _bonus_mult:,.0f}\n"
            f" 1.00%  Wallet Drain   $0 + all DeFi holdings lost\n"
            f" 0.01%  Ascended       ${ape_cost * payouts['ascended'][0] * _bonus_mult:,.0f} - ${ape_cost * payouts['ascended'][1] * _bonus_mult:,.0f}\n"
            f"```"
        )
        if _is_rug_king:
            preview += f"\n👑 **King of Rugs** - all payouts boosted by **+{_ape_bonus_rate*100:.1f}%**"
        if _job_ape_bonus > 0:
            preview += (
                f"\n💼 **{job_cfg_for_ape['title']}** perk - "
                f"all payouts boosted by **+{_job_ape_bonus*100:.0f}%**"
            )
        confirm_embed = (
            card("🦍 Ape Confirmation", description=preview, color=C_AMBER)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .footer("You have 30 seconds to decide")
            .build()
        )

        view = _ApeConfirmView(ctx.author.id)
        msg = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
        view.message = msg

        await view.wait()

        if view.choice != "confirm":
            ctx.command.reset_cooldown(ctx)
            cancel_embed = card("🦍 Ape Cancelled", description="Smart move... or cowardice? Only the chart knows.", color=C_AMBER).build()
            try:
                await msg.edit(embed=cancel_embed, view=None)
            except Exception:
                pass
            return

        # Re-check wallet balance (someone could have spent between confirm and now)
        user = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        wallet = user.h("wallet")
        if wallet < ape_cost:
            await msg.edit(embed=card("🦍 Ape Failed", description="You no longer have enough funds.", color=C_SELL).build(), view=None)
            return

        # Deduct entry cost
        await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, to_raw(-ape_cost))

        # Roll outcome  -  EV ≈ 0.72x entry cost (house edge ~28%)
        outcome = random.choices(
            ["rugged", "break_even", "moon", "legendary", "drained", "ascended"],
            weights=[8400, 949, 450, 100, 100, 1],
            k=1,
        )[0]

        lo, hi = payouts[outcome]

        # Fetch active players for social AI flavor
        ai_flags = await ctx.db.get_ai_flags(ctx.guild_id)
        _ape_others: list[str] = []
        if ai_flags["flavor"] and Config.OPENROUTER_API_KEY:
            _ape_others = await get_random_active_players(
                ctx.guild, ctx.db, exclude_user_id=ctx.author.id, count=2,
            )

        _ape_outcome_map = {
            "rugged": "rugged", "break_even": "break_even", "moon": "moon",
            "legendary": "legendary", "drained": "drained", "ascended": "ascended",
        }

        if outcome == "rugged":
            msg_text = random.choice(self._APE_FAIL)
            reward = 0.0
            color = C_SELL
            title = "🪤 Rugged"
        elif outcome == "break_even":
            msg_text = random.choice(self._APE_BREAK_EVEN)
            reward = round(random.uniform(lo, hi) * ape_cost * _ape_mult, 2)
            color = C_AMBER
            title = "😐 Break Even"
        elif outcome == "moon":
            msg_text = random.choice(self._APE_WIN)
            reward = round(random.uniform(lo, hi) * ape_cost * _ape_mult, 2)
            color = C_BUY
            title = "🚀 Moon"
        elif outcome == "legendary":
            msg_text = random.choice(self._APE_LEGENDARY)
            reward = round(random.uniform(lo, hi) * ape_cost * _ape_mult, 2)
            color = C_GOLD
            title = "👑 Legendary Ape"
        elif outcome == "drained":
            msg_text = random.choice(self._APE_DRAINED)
            reward = 0.0
            color = C_BLACK
            title = "🚨 WALLET DRAINED"
        else:  # ascended
            msg_text = random.choice(self._APE_ASCENDED)
            reward = round(random.uniform(lo, hi) * ape_cost * _ape_mult, 2)
            color = C_PINK
            title = "✦ Ascended"

        # Try social AI flavor (falls back to hardcoded above)
        if _ape_others:
            _ape_amt = fmt_usd(ape_cost) if outcome in ("rugged", "drained") else fmt_usd(reward)
            _social_ape = await _get_social_ai_flavor(
                "ape", _ape_outcome_map[outcome],
                ctx.author.display_name, _ape_others, _ape_amt,
            )
            if _social_ape:
                msg_text = _social_ape

        # Rugpull King bonus: scaling ape payout bonus
        _ape_rug_bonus = 0.0
        if reward > 0 and _is_rug_king:
            _ape_rug_bonus = round(reward * _ape_bonus_rate, 2)
            reward = reward + _ape_rug_bonus

        # Job-tier ape bonus (CARTEL_BOSS, L2_FOUNDER, SATOSHI, ...) -- applies
        # AFTER the rug-king bonus so a Cartel-Boss-King stacks both.
        _ape_job_bonus_amt = 0.0
        if reward > 0 and _job_ape_bonus > 0:
            _ape_job_bonus_amt = round(reward * _job_ape_bonus, 2)
            reward = reward + _ape_job_bonus_amt

        # Credit reward (if any). Wealth Bottleneck scales gains by leaderboard
        # rank: top of LB has reward throttled, bottom gets a USD top-up from
        # the per-guild pool. See services.bottleneck.
        _ape_bn = None
        if reward > 0:
            from services.bottleneck import apply_bottleneck, CreditKind
            _ape_bn = await apply_bottleneck(
                ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
                gross_raw=to_raw(reward), kind=CreditKind.APE,
            )
            reward = round(_ape_bn.total_to_wallet_raw / 10**18, 2)
            new_wallet_raw = await ctx.db.update_wallet(
                ctx.author.id, ctx.guild_id, _ape_bn.total_to_wallet_raw,
            )
            new_wallet = to_human(new_wallet_raw)
        else:
            new_wallet = wallet - ape_cost

        # Drain all DeFi wallet holdings on "drained" outcome
        defi_losses: list[str] = []
        if outcome == "drained":
            all_defi = await ctx.db.get_all_wallet_holdings(ctx.author.id, ctx.guild_id)
            for h in all_defi:
                amount_raw = int(h.get("amount", 0) or 0)
                if amount_raw <= 0:
                    continue
                amount_human = to_human(amount_raw)
                sym = h["symbol"]
                net_short = h["network"]
                try:
                    await ctx.db.update_wallet_holding(
                        ctx.author.id, ctx.guild_id, net_short, sym, -amount_raw,
                    )
                    price_row = await ctx.db.get_price(sym, ctx.guild_id)
                    usd_val = amount_human * float(price_row["price"]) if price_row else 0.0
                    defi_losses.append(f"{sym} ({net_short}): -{amount_human:,.6f} (~${usd_val:,.2f})")
                except ValueError:
                    pass

        net = reward - ape_cost

        # Substitute {amount} in flavor text: losses show entry cost, wins show payout
        # (social AI flavor may already have amount substituted  -  skip if no placeholder)
        if "{amount}" in msg_text:
            if outcome in ("rugged", "drained"):
                msg_text = msg_text.format(amount=fmt_usd(ape_cost))
            else:
                msg_text = msg_text.format(amount=fmt_usd(reward))

        _b = (
            card(title, description=msg_text, color=color)
            .author(f"🦍 {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        )
        if reward > 0:
            _payout_str = f"**${reward:,.2f}**"
            if _ape_rug_bonus > 0:
                _payout_str += f"\n👑 +${_ape_rug_bonus:,.2f} King bonus"
            _b.field("💰 Payout", _payout_str, True)
        if outcome == "drained" and defi_losses:
            _b.field("🚨 DeFi Losses", "\n".join(defi_losses[:10]), False)
        _b.field("📊 P&L", f"**{'+' if net >= 0 else ''}{FormatKit.usd(net)}**", True)
        _b.field("💵 Wallet", f"**${new_wallet:,.2f}**", True)
        from core.framework.ui import fmt_bottleneck as _fmt_bn
        _ape_bn_foot = _fmt_bn(_ape_bn) if _ape_bn else ""
        _ape_footer = f"Entry: ${ape_cost:,.0f} | {ctx.prefix}ape to degen again"
        if _ape_bn_foot:
            _ape_footer = f"{_ape_footer} | {_ape_bn_foot}"
        _b.footer(_ape_footer)

        result_embed = _b.build()
        # V3: Pillow ape result card, cosmetic-themed.
        _ape_file = None
        try:
            from services.payout_render import render_payout_card
            from services import cosmetics as _cos
            from core.framework.ui import C_SUCCESS, C_ERROR, C_PINK
            import io as _io
            _avatar_bytes = None
            if ctx.author.display_avatar:
                try:
                    _avatar_bytes = await ctx.author.display_avatar.read()
                except Exception:
                    pass
            _equipped = {}
            try:
                _equipped = await _cos.equipped(ctx.db, ctx.author.id)
            except Exception:
                pass
            _won = outcome not in ("rugged", "drained") and reward > 0
            _bonuses_a: list[tuple[str, str]] = []
            if _ape_rug_bonus > 0:
                _bonuses_a.append(("King bonus", f"+${_ape_rug_bonus:,.2f}"))
            if _job_ape_bonus > 0 and reward > 0:
                _bonuses_a.append((
                    "Job ape perk", f"+{_job_ape_bonus*100:.0f}%",
                ))
            _bonuses_a.append(("Outcome", outcome.upper()))
            _png = render_payout_card(
                user_name=ctx.author.display_name,
                avatar_bytes=_avatar_bytes,
                title="APED",
                subtitle=f"YOLO ${ape_cost:,.0f}",
                badge_text=("WIN" if _won else "LOSS"),
                badge_color=(C_SUCCESS if _won else C_ERROR),
                accent_color=C_PINK,
                reward_usd=float(net),
                gross_usd=float(reward),
                tax_usd=0.0,
                bonus_usd=float(_ape_rug_bonus),
                bonuses=_bonuses_a,
                new_wallet_usd=float(new_wallet),
                footer="V3 Ape",
                equipped=_equipped,
            )
            _ape_file = discord.File(_io.BytesIO(_png), filename="ape.png")
            result_embed.set_image(url="attachment://ape.png")
        except Exception:
            log.debug("ape: PNG render failed; sending embed only", exc_info=True)

        try:
            _ek = {"embed": result_embed, "view": None}
            if _ape_file is not None:
                _ek["attachments"] = [_ape_file]
            await msg.edit(**_ek)
        except Exception:
            try:
                _sk = {"embed": result_embed}
                if _ape_file is not None:
                    _sk["file"] = _ape_file
                await ctx.send(**_sk)
            except Exception:
                try:
                    await ctx.author.send(
                        content="Your ape result (could not post in channel):",
                        embed=result_embed,
                    )
                except Exception:
                    pass

        # Publish event for notifications
        await ctx.bot.bus.publish("ape_completed", guild=ctx.guild,
            user=ctx.author, outcome=outcome, payout=reward,
            entry_cost=ape_cost, net=net)

        # Post to ape feed channel (if configured) for wins
        if net > 0:
            try:
                settings = await ctx.db.get_guild_settings(ctx.guild_id)
                ch_id = settings.get("ape_channel") if settings else None
                if ch_id:
                    ch = ctx.guild.get_channel(int(ch_id))
                    if ch and hasattr(ch, "send"):
                        await ch.send(embed=result_embed)
            except Exception:
                pass

        # DM notification for big wins (moon+)
        if outcome in ("moon", "legendary", "ascended"):
            try:
                prefs = await ctx.db.get_user_prefs(ctx.author.id, ctx.guild_id)
                if prefs.get("dm_ape", False):
                    await ctx.author.send(
                        content=f"🦍 **Ape win on {ctx.guild.name}!**",
                        embed=result_embed,
                    )
            except Exception:
                pass

        # Log notable ape outcomes as server events for AI gossip
        _ape_event_types = {
            "drained": ("drain", f"{ctx.author.display_name} got wallet-drained on an ape - lost ${ape_cost:,.2f} entry + all DeFi"),
            "ascended": ("ascended", f"{ctx.author.display_name} hit an ASCENDED 100x ape - walked away with ${reward:,.2f}"),
            "legendary": ("legendary", f"{ctx.author.display_name} hit a legendary ape - walked away with ${reward:,.2f}"),
        }
        if outcome in _ape_event_types:
            _evt_type, _evt_summary = _ape_event_types[outcome]
            try:
                await ctx.db.log_server_event(
                    ctx.guild_id, ctx.channel.id, ctx.author.id,
                    _evt_type, _evt_summary,
                    abs(net),
                    {"command": "ape", "outcome": outcome},
                )
                mark_hot_channel(ctx.guild_id, ctx.channel.id)
            except Exception:
                pass

            # Autonomous bot reaction
            try:
                _social_cog = ctx.bot.get_cog("SocialContext")
                if _social_cog and msg:
                    asyncio.create_task(_social_cog.react_to_event(ctx.channel, msg, _evt_type))
            except Exception:
                pass

    # ── /earn daily ───────────────────────────────────────────────────────────

    @earn.command(name="daily")
    @guild_only
    @no_bots
    @ensure_registered
    async def daily(self, ctx: DiscoContext) -> None:
        """Claim your daily reward (24-hour cooldown, streak bonuses)."""
        lock_key = (ctx.author.id, ctx.guild_id)
        lock = self._daily_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("Already processing your daily  -  wait a moment.")
            return
        async with lock:
            await self._do_daily(ctx)

    async def _do_daily(self, ctx: DiscoContext) -> None:
        row = ctx.user_row
        now = time.time()
        _ld = row["last_daily"]
        last = _ld.timestamp() if hasattr(_ld, 'timestamp') else (_ld or 0.0)
        elapsed = now - last

        if elapsed < Config.DAILY_COOLDOWN:
            remaining = Config.DAILY_COOLDOWN - elapsed
            h, rem = divmod(int(remaining), 3600)
            m = rem // 60
            await ctx.reply_error(f"Come back in **{h}h {m}m** for your next daily reward.")
            return

        # Set cooldown IMMEDIATELY so a queued duplicate can't pass the check.
        await ctx.db.set_cooldown(ctx.author.id, ctx.guild_id, "last_daily")

        # Streak logic
        streak = row["daily_streak"]
        if elapsed < _48H:
            streak = min(streak + 1, Config.DAILY_MAX_STREAK)
        else:
            streak = 1  # reset; starting a new streak

        base_reward = to_human(Config.DAILY_AMOUNT) + (streak - 1) * to_human(Config.DAILY_STREAK_BONUS)

        # Guild daily multiplier (admin-configurable)
        _daily_settings = await ctx.db.get_guild_settings(ctx.guild_id)
        _daily_mult = float(_daily_settings.get("daily_multiplier") or 1.0)
        if _daily_mult != 1.0:
            base_reward = round(base_reward * _daily_mult, 2)

        # Wealth-scaling lives in the Wealth Bottleneck now (applied below)
        # so this block intentionally leaves the base reward untouched. The
        # bottleneck call further down is the single point where leaderboard
        # rank reshapes the payout.

        # Apply daily_bonus job perk
        job = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_cfg = Config.JOBS.get(job["job_id"], Config.JOBS["HOMELESS"])
        daily_bonus = job_cfg.get("perks", {}).get("daily_bonus", 0.0)
        reward = round(base_reward * (1 + daily_bonus), 2)

        # Buddy companion multiplier (work lane also multiplies daily payouts).
        _buddy_pct = 0.0
        _buddy_added = 0.0
        try:
            from services.buddy_bonus import buddy_bonus
            _buddy_mult = await buddy_bonus(ctx.db, ctx.guild_id, ctx.author.id, lane="work")
            if _buddy_mult > 1.0:
                _buddy_pct = _buddy_mult - 1.0
                _pre_buddy = reward
                reward = round(reward * _buddy_mult, 2)
                _buddy_added = reward - _pre_buddy
        except Exception:
            pass  # buddy subsystem must never break daily

        # Apply stone level bonuses -- all four stones contribute work_daily_bonus
        # (see the matching block in .work; keep them in sync so one command
        # doesn't silently drop a bonus the other credits).
        hashstone   = await ctx.db.get_hashstone(ctx.author.id, ctx.guild_id)
        lockstone  = await ctx.db.get_lockstone(ctx.author.id, ctx.guild_id)
        vaultstone = await ctx.db.get_vaultstone(ctx.author.id, ctx.guild_id)
        liqstone   = await ctx.db.get_liqstone(ctx.author.id, ctx.guild_id)
        stone_bonus = (
            _item_stat(hashstone, "work_daily_bonus")
            + _lockstone_stat(lockstone, "work_daily_bonus")
            + _vaultstone_stat(vaultstone, "work_daily_bonus")
            + _liqstone_stat(liqstone, "work_daily_bonus")
        )
        # Meta-economy stones (Gavelstone / Anvilstone / Chimerastone)
        # also advertise work_daily_bonus -- mirror the .work block so
        # /daily credits the same bonuses /work does.
        for _meta_key in ("gavelstone", "anvilstone", "chimerastone"):
            try:
                _meta_row = await getattr(ctx.db, f"get_{_meta_key}")(
                    ctx.author.id, ctx.guild_id,
                )
            except Exception:
                _meta_row = None
            if not _meta_row:
                continue
            _meta_per = float(
                Config.SHOP_ITEMS.get(_meta_key, {}).get("stats", {})
                .get("work_daily_bonus", 0.0)
            )
            stone_bonus += _meta_per * int(_meta_row.get("level") or 0)
        stone_added = 0.0
        if stone_bonus > 0:
            pre_stone = reward
            reward = round(reward * (1.0 + stone_bonus), 2)
            stone_added = reward - pre_stone

        # User-created-token LP bonus: same USD-priced, capped tilt as .work.
        _user_lp_bonus = 0.0
        _user_lp_added = 0.0
        _user_lp_usd   = 0.0
        try:
            from services.liquidity import user_created_lp_value_usd, user_lp_work_bonus_pct
            _user_lp_usd = await user_created_lp_value_usd(
                ctx.db, ctx.author.id, ctx.guild_id,
            )
            _user_lp_bonus = user_lp_work_bonus_pct(_user_lp_usd)
        except Exception:
            log.exception("daily: user-LP bonus lookup failed; dropping to 0")
        if _user_lp_bonus > 0:
            _pre_ulp = reward
            reward = round(reward * (1.0 + _user_lp_bonus), 2)
            _user_lp_added = reward - _pre_ulp

        # Hall bonus: extra % when daily is used inside a Group Hall with Trophy Wall
        hall_daily_pct = getattr(ctx, "hall_bonus", {}).get("daily", 0.0)
        hall_daily_added = 0.0
        if hall_daily_pct > 0:
            pre_hall = reward
            reward = round(reward * (1.0 + hall_daily_pct), 2)
            hall_daily_added = reward - pre_hall

        # Apex Mastery: Reliable Returns (econ.daily_bonus) scales the
        # daily payout multiplicatively by the cumulative passive. The
        # Reliable Returns I + II nodes stack additively in the node
        # tree (5% + 10% = 0.15) so a fully unlocked path gives +15%.
        if reward > 0:
            try:
                from services import mastery as _m
                reward = round(
                    await _m.apply_passive(
                        ctx.db, ctx.author.id, ctx.guild_id,
                        "econ.daily_bonus", reward, mode="mul",
                    ),
                    2,
                )
            except Exception:
                log.debug(
                    "econ.daily_bonus passive read failed",
                    exc_info=True,
                )

        # Random surprise event (rare positive bonus). Mirrors the .work hook
        # so daily check-ins also occasionally hit a windfall or free guard.
        daily_surprise = await _roll_surprise(
            reward, ctx.db, ctx.author.id, ctx.guild_id,
            chance=_DAILY_SURPRISE_CHANCE,
        )
        if daily_surprise:
            if daily_surprise["kind"] == "jackpot":
                reward = round(reward * daily_surprise["multiplier"], 2)
            elif daily_surprise["kind"] == "treasure":
                reward = round(reward + daily_surprise["flat_bonus"], 2)

        # Wealth Bottleneck: rank-based scaling on the credit.
        from services.bottleneck import apply_bottleneck, CreditKind
        _gross_raw = to_raw(reward)
        _bn_result = await apply_bottleneck(
            ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
            gross_raw=_gross_raw, kind=CreditKind.DAILY,
        )
        # Reframe ``reward`` so every downstream embed/PNG sees the actual
        # net the player just received (gross - drag + boost).
        reward = round(_bn_result.total_to_wallet_raw / 10**18, 2)
        new_wallet_raw = await ctx.db.update_wallet(
            ctx.author.id, ctx.guild_id, _bn_result.total_to_wallet_raw,
        )
        from datetime import datetime, timezone as _tz
        await ctx.db.update_streak(ctx.author.id, ctx.guild_id, streak, datetime.now(_tz.utc))
        daily_tx = await ctx.db.log_tx(
            ctx.guild_id, ctx.author.id, "DAILY",
            symbol_out="USD", amount_out=_gross_raw,
            network="usd",
        )
        await ctx.bot.bus.publish("daily_claimed", guild=ctx.guild,
            user=ctx.author, amount=reward, streak=streak, tx_hash=daily_tx)

        bonus_parts = []
        _bn_drag_h = float(_bn_result.drag_usd_raw) / 10**18 if _bn_result and not _bn_result.skipped else 0.0
        _bn_boost_h = float(_bn_result.boost_wallet_raw) / 10**18 if _bn_result and not _bn_result.skipped else 0.0
        if _bn_drag_h > 0:
            bonus_parts.append(
                f"  ↳ ⚖️ Bottleneck: x{_bn_result.multiplier:.2f} (-${_bn_drag_h:,.2f} to pool)"
            )
        elif _bn_boost_h > 0:
            bonus_parts.append(
                f"  ↳ ⚖️ Bottleneck: x{_bn_result.multiplier:.2f} (+${_bn_boost_h:,.2f} from pool)"
            )
        if daily_bonus > 0:
            bonus_parts.append(f"  ↳ 💼 Job perk: +{daily_bonus*100:.0f}%")
        if stone_bonus > 0:
            bonus_parts.append(f"  ↳ 💎 Stone: +{stone_bonus*100:.0f}% (+${stone_added:,.2f})")
        if _user_lp_added > 0:
            bonus_parts.append(
                f"  ↳ 🌊 User LP: +{_user_lp_bonus*100:.1f}% "
                f"(${_user_lp_usd:,.0f}, +${_user_lp_added:,.2f})"
            )
        if _buddy_added > 0:
            bonus_parts.append(f"  ↳ 🐾 Buddy: +{_buddy_pct*100:.1f}% (+${_buddy_added:,.2f})")
        if hall_daily_added > 0:
            bonus_parts.append(f"  ↳ 🏛️ Hall: +{hall_daily_pct*100:.0f}% (+${hall_daily_added:,.2f})")
        bonus_str = ("\n" + "\n".join(bonus_parts)) if bonus_parts else ""

        streak_bar = FormatKit.bar(streak, Config.DAILY_MAX_STREAK, width=10, show_pct=False)
        if streak < Config.DAILY_MAX_STREAK:
            next_bonus_val = to_human(Config.DAILY_AMOUNT) + streak * to_human(Config.DAILY_STREAK_BONUS)
            footer_text = f"Tomorrow's base reward: ${next_bonus_val:,.2f}  -  don't break the chain!"
        else:
            footer_text = "Max streak reached! Every daily from here is peak rewards."

        # Pick flavor text based on streak state and substitute {amount}
        if streak >= Config.DAILY_MAX_STREAK:
            daily_flavor = random.choice(_DAILY_MAX_STREAK_FLAVORS).format(amount=fmt_usd(reward))
            _daily_outcome = "max_streak"
        elif streak >= 3:
            daily_flavor = random.choice(_DAILY_STREAK_FLAVORS).format(amount=fmt_usd(reward))
            _daily_outcome = "streak"
        else:
            daily_flavor = random.choice(_DAILY_FLAVORS).format(amount=fmt_usd(reward))
            _daily_outcome = "claim"

        # Try social AI flavor with other player names
        _daily_ai = await ctx.db.get_ai_flags(ctx.guild_id)
        if _daily_ai["flavor"] and Config.OPENROUTER_API_KEY:
            _daily_others = await get_random_active_players(
                ctx.guild, ctx.db, exclude_user_id=ctx.author.id, count=2,
            )
            if _daily_others:
                _social_daily = await _get_social_ai_flavor(
                    "daily", _daily_outcome, ctx.author.display_name,
                    _daily_others, fmt_usd(reward),
                )
                if _social_daily:
                    daily_flavor = _social_daily

        _daily_b = (
            card("🗓️ Daily Reward", description=f"_{daily_flavor}_", color=C_INFO)
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field(
                "💰 Reward",
                f"**+${reward:,.2f}**{bonus_str}",
                True,
            )
            .field(
                "🔥 Streak",
                f"`{streak_bar}`\n**{streak}** / {Config.DAILY_MAX_STREAK} days",
                True,
            )
            .field(
                "💼 Wallet",
                f"**${to_human(new_wallet_raw):,.2f}**",
                True,
            )
        )
        if daily_surprise:
            if daily_surprise["kind"] == "jackpot":
                _surprise_detail = f"\n**Bonus: x{daily_surprise['multiplier']:.0f} payout**"
            elif daily_surprise["kind"] == "treasure":
                _surprise_detail = f"\n**Bonus: +${daily_surprise['flat_bonus']:,.2f}**"
            else:
                _surprise_detail = "\n*Added to your inventory.*"
            _daily_b.field(
                daily_surprise["title"],
                f"_{daily_surprise['flavor']}_{_surprise_detail}",
                False,
            )
        embed = _daily_b.footer(footer_text).build()
        # V3: Pillow daily-claim receipt card alongside the embed.
        try:
            from services.payout_render import render_daily_card
            import io as _io
            _bonuses: list[tuple[str, str]] = []
            if _bn_drag_h > 0:
                _bonuses.append(("Bottleneck", f"-${_bn_drag_h:,.2f} (x{_bn_result.multiplier:.2f})"))
            elif _bn_boost_h > 0:
                _bonuses.append(("Bottleneck", f"+${_bn_boost_h:,.2f} (x{_bn_result.multiplier:.2f})"))
            if daily_bonus > 0:
                _bonuses.append(("Job perk", f"+{daily_bonus*100:.0f}%"))
            if stone_bonus > 0:
                _bonuses.append((
                    "Stone bonus",
                    f"+{stone_bonus*100:.0f}% (+${stone_added:,.2f})",
                ))
            if _user_lp_added > 0:
                _bonuses.append((
                    "User LP",
                    f"+{_user_lp_bonus*100:.1f}% (+${_user_lp_added:,.2f})",
                ))
            if _buddy_added > 0:
                _bonuses.append((
                    "Buddy",
                    f"+{_buddy_pct*100:.1f}% (+${_buddy_added:,.2f})",
                ))
            if hall_daily_added > 0:
                _bonuses.append((
                    "Hall",
                    f"+{hall_daily_pct*100:.0f}% (+${hall_daily_added:,.2f})",
                ))
            _avatar_bytes = None
            if ctx.author.display_avatar:
                try:
                    _avatar_bytes = await ctx.author.display_avatar.read()
                except Exception:
                    pass
            _equipped = {}
            try:
                from services import cosmetics as _cos
                _equipped = await _cos.equipped(ctx.db, ctx.author.id)
            except Exception:
                pass
            _gross_human = to_human(_gross_raw)
            _tax_human = max(0.0, float(_bn_result.drag_usd_raw) / 10**18)
            _bonus_human = max(0.0, float(_bn_result.boost_wallet_raw) / 10**18)
            _png = render_daily_card(
                user_name=ctx.author.display_name,
                avatar_bytes=_avatar_bytes,
                streak_days=streak,
                reward_usd=reward,
                gross_usd=_gross_human,
                tax_usd=_tax_human,
                bonus_usd=_bonus_human,
                bonuses=_bonuses,
                new_wallet_usd=to_human(int(new_wallet_raw)),
                equipped=_equipped,
            )
            _file = discord.File(_io.BytesIO(_png), filename="daily.png")
            embed.set_image(url="attachment://daily.png")
            await ctx.reply(embed=embed, file=_file, mention_author=False)
            return
        except Exception:
            log.debug("daily: PNG render failed; sending embed only", exc_info=True)
        await ctx.reply(embed=embed, mention_author=False)

    # ── /earn job ─────────────────────────────────────────────────────────────

    @earn.command(name="job")
    @guild_only
    @no_bots
    @ensure_registered
    async def job(self, ctx: DiscoContext) -> None:
        """Show your current job and stats."""
        job = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_cfg = Config.JOBS.get(job["job_id"], Config.JOBS["HOMELESS"])
        perks = job_cfg.get("perks", {})

        _earn_min, _earn_max = to_human(job_cfg["earn"][0]), to_human(job_cfg["earn"][1])
        _b = card(
            f"💼 {job_cfg['title']}",
            description=f"_{job_cfg.get('description', '')}_",
            color=C_PURPLE,
        )
        _b.author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        _b.field("💵 Pay Range",    f"**${_earn_min:,.0f} - ${_earn_max:,.0f}** / session", True)
        _b.field("📊 Sessions",     f"**{job['work_count']}** completed",          True)
        _b.field("💰 Total Earned", f"**${to_human(job['total_earned']):,.2f}**",             True)

        if perks:
            perk_lines = _render_perk_lines(perks, compact=False)
            _b.field("🎁 Perks", "\n".join(perk_lines) if perk_lines else "None", False)

        next_id = _next_job_id(job["job_id"])
        if next_id:
            next_cfg = Config.JOBS[next_id]
            net_worth = (await compute_net_worth(ctx.author.id, ctx.guild_id, ctx.db)).total
            need_sessions = max(0, next_cfg["min_work"] - job["work_count"])
            need_wealth = max(0.0, next_cfg["min_wealth"] - net_worth)
            reqs = []
            if need_sessions > 0:
                reqs.append(f"**{need_sessions}** more sessions")
            if need_wealth > 0:
                reqs.append(f"**${need_wealth:,.0f}** more net worth")

            progress_bar = FormatKit.bar(
                job["work_count"], next_cfg["min_work"], width=10, show_pct=False
            )
            progress_line = f"`{progress_bar}`  {job['work_count']}/{next_cfg['min_work']} sessions"
            if not reqs:
                status = f"{progress_line}\n✅ **Ready to promote!** Run `/earn promote`"
            else:
                status = f"{progress_line}\n⏳ Still need: {' + '.join(reqs)}"

            _b.field(f"⬆️ Next Rank: {next_cfg['title']}", status, False)
        embed = _b.build()
        await ctx.reply(embed=embed, mention_author=False)

    # ── /earn jobs ────────────────────────────────────────────────────────────

    @earn.command(name="jobs")
    @guild_only
    async def jobs(self, ctx: DiscoContext) -> None:
        """Show all job tiers with requirements and perks."""
        # Job ladder is 24 tiers -- paginate so each page stays under
        # Discord's 25-field embed cap and ~6000-char total budget.
        per_page = 12
        order = list(Config.JOB_ORDER)
        pages: list[discord.Embed] = []
        total_pages = (len(order) + per_page - 1) // per_page
        for page_idx in range(total_pages):
            chunk = order[page_idx * per_page : (page_idx + 1) * per_page]
            _b = card(
                f"📋 Job Ladder ({page_idx + 1}/{total_pages})",
                description="All tiers from entry-level grind to full degen operator.",
                color=C_PURPLE,
            )
            for i, job_id in enumerate(chunk):
                cfg = Config.JOBS[job_id]
                earn_min, earn_max = to_human(cfg["earn"][0]), to_human(cfg["earn"][1])
                perks = cfg.get("perks", {})
                perk_parts = _render_perk_lines(perks, compact=True)
                perk_str = "  ·  ".join(perk_parts) if perk_parts else "None"
                tier_num = page_idx * per_page + i + 1
                value = (
                    f"**💵 ${earn_min:,.0f} - ${earn_max:,.0f}** / session\n"
                    f"🔒 Req: **{cfg['min_work']}** sessions · "
                    f"**${cfg['min_wealth']:,.0f}** net worth\n"
                    f"🎁 {perk_str}\n"
                    f"───────────────"
                )
                _b.field(f"#{tier_num}  -  {cfg['title']}", value, True)
            pages.append(_b.build())
        if len(pages) == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
            return
        await ctx.paginate(pages)

    # ── /earn promote ─────────────────────────────────────────────────────────

    @earn.command(name="promote")
    @guild_only
    @no_bots
    @ensure_registered
    async def promote(self, ctx: DiscoContext) -> None:
        """Promote to the next job tier if you meet the requirements."""
        job = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        next_id = _next_job_id(job["job_id"])
        if not next_id:
            top_id = Config.JOB_ORDER[-1]
            top_title = Config.JOBS.get(top_id, {}).get("title", top_id.title())
            await ctx.reply_error(
                f"You're already at the top tier: **{top_title}**. Impressive."
            )
            return

        next_cfg = Config.JOBS[next_id]

        if job["work_count"] < next_cfg["min_work"]:
            needed = next_cfg["min_work"] - job["work_count"]
            await ctx.reply_error(
                f"Need **{needed}** more work sessions to qualify for **{next_cfg['title']}**."
            )
            return
        net_worth = (await compute_net_worth(ctx.author.id, ctx.guild_id, ctx.db)).total
        if net_worth < next_cfg["min_wealth"]:
            needed = next_cfg["min_wealth"] - net_worth
            await ctx.reply_error(
                f"Need **${needed:,.2f}** more net worth for **{next_cfg['title']}**."
            )
            return

        old_id = job["job_id"]
        old_cfg = Config.JOBS.get(old_id, Config.JOBS["HOMELESS"])
        await ctx.db.update_job(ctx.author.id, ctx.guild_id, next_id, job["work_count"], job["total_earned"])
        await ctx.bot.bus.publish(
            "promoted",
            guild=ctx.guild, user=ctx.author,
            old_job=old_id, new_job=next_id,
        )
        earn_min, earn_max = to_human(next_cfg["earn"][0]), to_human(next_cfg["earn"][1])
        old_min, old_max = to_human(old_cfg["earn"][0]), to_human(old_cfg["earn"][1])

        new_perks = next_cfg.get("perks", {})
        perk_lines = _render_perk_lines(new_perks, compact=False)

        promo_embed = (
            card(
                "⬆️ Promotion!",
                description=(
                    f"_{next_cfg.get('description', '')}_"
                ),
                color=C_SUCCESS,
            )
            .author(ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            .field("📤 Previous Rank", f"**{old_cfg['title']}**\n${old_min:,.0f} - ${old_max:,.0f} / session", True)
            .field("📥 New Rank", f"**{next_cfg['title']}**\n${earn_min:,.0f} - ${earn_max:,.0f} / session", True)
        )
        if perk_lines:
            promo_embed.field("🎁 Perks", "\n".join(perk_lines), True)
        promo_embed.footer("Keep working sessions to reach the next tier.")
        await ctx.reply(embed=promo_embed.build(), mention_author=False)

    # ── Backward-compatible prefix-only aliases ───────────────────────────────

    @commands.command(name="work", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def _work_alias(self, ctx: DiscoContext) -> None:
        """Backward-compatible alias: $work -> /earn work"""
        lock_key = (ctx.author.id, ctx.guild_id)
        lock = self._work_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("You're already working! Wait for your current session to finish.")
            return
        async with lock:
            await self._do_work(ctx)

    @commands.command(name="daily", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def _daily_alias(self, ctx: DiscoContext) -> None:
        """Backward-compatible alias: $daily -> /earn daily"""
        lock_key = (ctx.author.id, ctx.guild_id)
        lock = self._daily_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await ctx.reply_error("Already processing your daily  -  wait a moment.")
            return
        async with lock:
            await self._do_daily(ctx)

    @commands.command(name="job", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def _job_alias(self, ctx: DiscoContext) -> None:
        """Backward-compatible alias: $job -> /earn job"""
        await self.job(ctx)

    @commands.command(name="jobs", hidden=True)
    @guild_only
    async def _jobs_alias(self, ctx: DiscoContext) -> None:
        """Backward-compatible alias: $jobs -> /earn jobs"""
        await self.jobs(ctx)

    @commands.command(name="promote", hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    async def _promote_alias(self, ctx: DiscoContext) -> None:
        """Backward-compatible alias: $promote -> /earn promote"""
        await self.promote(ctx)

    @commands.command(name="ape", aliases=["degen", "yolo"], hidden=True)
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(300)
    async def _ape_alias(self, ctx: DiscoContext) -> None:
        """Backward-compatible alias: $ape -> /earn ape"""
        await self.ape(ctx)

    # ─── $beg ─────────────────────────────────────────────────────────────

    # Outcome weights (must sum to 1.0)
    #   70%    -  nothing happens (flavour text, no gain/loss)
    #   18%    -  small gain: $0.01 - $20
    #    7%    -  medium gain: $0.01 - $500
    #    2.5%  -  jackpot: $500 - $50,000
    #    2.5%  -  catastrophe: lose ~90% of CeFi USD + crypto value
    _BEG_NOTHING  = 0.70
    _BEG_SMALL    = 0.18
    _BEG_MEDIUM   = 0.07
    _BEG_JACKPOT  = 0.025
    # remaining 0.005 = catastrophe

    # Supports {amount} substitution for gain/loss tiers; nothing lines have no amount.
    _BEG_NOTHING_LINES = [
        "You post your wallet address in a billionaire's replies with a typed-out sob story. He muted you from space.",
        "A passing whale glances at your QR code and keeps scrolling. His gas fees cost more than your net worth.",
        "You begged in a Telegram VC while a whale was mid-sentence. You got muted by the admin before the wallet moved.",
        "Someone throws a governance token at you. It's from a rug that died in 2022. You take it anyway.",
        "'I only tip in airdrops,' says the man whose last airdrop is worth $0.0003.",
        "You hold up a digital sign outside the Coinbase listing committee. Nobody inside can see it. You're on Discord.",
        "A crypto influencer reposts your begging thread with 'Not financial advice' and zero satoshis attached.",
        "The 'random wallet dust' airdrop you received is worth exactly $0.00. It costs $8 in gas to find out.",
        "An 'anonymous donor' DMs you about a 'seed phrase recovery'. You recognize the scam. You lose nothing but your afternoon.",
        "You entered a 'free crypto' Twitter giveaway requiring a retweet, a follow, and your wallet address. The account deleted itself.",
        "'Will shill for gas' gets zero traction in a bear market. There are ten thousand signs exactly like yours.",
        "The faucet you've been farming all day gives testnet tokens. They are, to be clear, not mainnet tokens.",
        "A Discord Mod tells you begging violates the community guidelines and issues a 48-hour ban.",
        "The DAO voted 'No' on your emergency grant proposal 99.4% to 0.6%. The 0.6% was you, voting for yourself.",
        "You've been refreshing the faucet for four hours. It ran dry three hours ago. The website just shows a spinner.",
    ]

    _BEG_SMALL_LINES = [
        "A developer feeling guilty about a 2022 rug dusted random wallets from the proceeds. You were on the list. You received **{amount}**.",
        "You posted your SOL address in the replies of a memecoin launch. Someone who made 400× decided to pay it forward. You received **{amount}**.",
        "The 'Pay It Forward' bot randomly selected your wallet from a list of a million addresses. No idea why. You received **{amount}**.",
        "A stranger in a Telegram VC heard you begging and threw you some dust  -  not out of charity, but to make you stop talking. You received **{amount}**.",
        "You found an old airdrop portal nobody had revisited. The snapshot was years ago but the claim was still live. You received **{amount}**.",
        "A whale accidentally sent a micro-transaction to the wrong address. That address was yours. You received **{amount}**.",
    ]

    _BEG_MEDIUM_LINES = [
        "A VC lurking on a public DAO call got impressed by your questions and dropped a 'talent acquisition tip' to your wallet. You received **{amount}**.",
        "You wrote a 20-tweet thread about how 'the tech is the future', tagging every fund you could find. One tipped you to make you stop. You received **{amount}**.",
        "A 'Crypto Guru' you messaged about mentorship sent you a mystery token he's quietly accumulating. It's actually up today. You received **{amount}**.",
        "You joined a 'Free Crypto' YouTube stream that was a two-year-old loop. A viewer felt bad for your comments and sent a tip. You received **{amount}**.",
        "You've been camping in a major DAO's governance forum for weeks. Someone passed a 'community morale grant' that covered your address. You received **{amount}**.",
        "A whale doing wallet audits for tax purposes accidentally included your address in their 'charitable donation' column. You received **{amount}**.",
    ]

    _BEG_JACKPOT_LINES = [
        "You posted your wallet address under a billionaire's tweet with a Mt. Gox survivor story. Someone verified it and sent funds. You received **{amount}**.",
        "A 'Crypto Angel' with a documented history of random large transfers saw your address floating on CT and made a decision. You received **{amount}**.",
        "You were scavenging 2021 airdrop portals and found one nobody had claimed. The eligibility requirements were absurd enough that you qualified. You received **{amount}**.",
        "The DAO treasury accidentally over-distributed during a snapshot dispute. The unclaimed surplus went to the last address on the list. That was you. You received **{amount}**.",
        "An anonymous donor going through a 'crypto karma' cleanse picked your address from a public begging thread. Their generosity is your windfall. You received **{amount}**.",
        "You entered a whale's 'Diamond Hands Challenge' as a joke. You were the only participant still holding after 90 days. The prize pool was uncontested. You received **{amount}**.",
    ]

    _BEG_CATASTROPHE_LINES = [
        "You connected your wallet to a 'faucet' that was actually a drainer. The site looked identical to the real one. You lost **{amount}**.",
        "A 'random wallet audit' DM convinced you to 'verify' your holdings through their portal. You lost **{amount}** before the tab finished loading.",
        "You clicked an airdrop link from someone whose profile picture matched a dev you trusted. You lost **{amount}**.",
        "While begging in a public channel, someone sent you an NFT. You tried to list it. The hidden contract drained your wallet. You lost **{amount}**.",
        "The 'Decentralized Charity' you donated to had a 99% 'platform fee'. What felt like generosity was extraction. You lost **{amount}**.",
        "Clipboard malware you picked up from a phishing Discord swapped your address mid-transaction. The funds went to a wallet you've never seen. You lost **{amount}**.",
    ]

    @commands.command(name="beg")
    @guild_only
    @no_bots
    @ensure_registered
    @user_cooldown(3600)  # 1 hour, halved by multiplier → 30min effective
    async def beg(self, ctx: DiscoContext) -> None:
        """Beg on the streets. Usually nothing happens.
        But sometimes... you hit the jackpot. Or lose almost everything.

        Affects CeFi only (USD wallet + bank + crypto holdings). 1 hour cooldown.
        """
        roll = random.random()

        _beg_settings = await ctx.db.get_guild_settings(ctx.guild_id)
        _beg_mult = float(_beg_settings.get("beg_multiplier") or 1.0)

        # Fetch active players for social AI flavor (shared across all beg outcomes)
        _beg_ai = await ctx.db.get_ai_flags(ctx.guild_id)
        _beg_others: list[str] = []
        if _beg_ai["flavor"] and Config.OPENROUTER_API_KEY:
            _beg_others = await get_random_active_players(
                ctx.guild, ctx.db, exclude_user_id=ctx.author.id, count=2,
            )

        if roll < self._BEG_NOTHING:
            # ── Nothing happens ──
            line = random.choice(self._BEG_NOTHING_LINES)
            if _beg_others:
                _social = await _get_social_ai_flavor(
                    "beg", "nothing", ctx.author.display_name, _beg_others, "",
                )
                if _social:
                    line = _social
            embed = card("🫳 Begging...", description=line, color=C_AMBER)
            embed.footer("Nothing gained. Nothing lost. Try again in an hour.")
            await ctx.reply(embed=embed.build(), mention_author=False)
            return

        user = await ctx.db.get_user(ctx.author.id, ctx.guild_id)
        wallet = user.h("wallet")
        bank = user.h("bank")

        # V3: Pillow beg-receipt card themed to equipped cosmetics.
        # The helper is called by every gain/loss branch below right
        # before its ctx.reply so the result is one consistent visual.
        async def _beg_png(
            *, badge: str, badge_color: int, accent: int,
            outcome_label: str, delta_usd: float,
        ):
            try:
                from services.payout_render import render_payout_card
                from services import cosmetics as _cos
                import io as _io
                _avatar_bytes = None
                if ctx.author.display_avatar:
                    try:
                        _avatar_bytes = await ctx.author.display_avatar.read()
                    except Exception:
                        pass
                _equipped = {}
                try:
                    _equipped = await _cos.equipped(ctx.db, ctx.author.id)
                except Exception:
                    pass
                _png = render_payout_card(
                    user_name=ctx.author.display_name,
                    avatar_bytes=_avatar_bytes,
                    title="Begging",
                    subtitle=outcome_label,
                    badge_text=badge,
                    badge_color=badge_color,
                    accent_color=accent,
                    reward_usd=float(delta_usd),
                    gross_usd=float(max(0.0, delta_usd)),
                    tax_usd=0.0,
                    bonus_usd=0.0,
                    bonuses=[("Outcome", outcome_label)],
                    new_wallet_usd=float(wallet + delta_usd),
                    footer="V3 Beg",
                    equipped=_equipped,
                )
                return discord.File(_io.BytesIO(_png), filename="beg.png")
            except Exception:
                log.debug("beg: PNG render failed", exc_info=True)
                return None

        _threshold_small  = self._BEG_NOTHING + self._BEG_SMALL
        _threshold_medium = _threshold_small + self._BEG_MEDIUM
        _threshold_jackpot = _threshold_medium + self._BEG_JACKPOT

        if roll < _threshold_small:
            # ── Small gain: $0.01 - $20 ──
            gain = round(random.uniform(0.01, 20.00) * _beg_mult, 2)
            from services.bottleneck import apply_bottleneck, CreditKind
            _bn = await apply_bottleneck(
                ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
                gross_raw=to_raw(gain), kind=CreditKind.BEG,
            )
            gain = round(_bn.total_to_wallet_raw / 10**18, 2)
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, _bn.total_to_wallet_raw)
            line = random.choice(self._BEG_SMALL_LINES).format(amount=fmt_usd(gain))
            if _beg_others:
                _social = await _get_social_ai_flavor(
                    "beg", "small", ctx.author.display_name, _beg_others, fmt_usd(gain),
                )
                if _social:
                    line = _social
            embed = card("🫳 Begging...", color=C_AMBER)
            embed.description(line)
            from core.framework.ui import fmt_bottleneck as _fmt_bn
            _bn_foot = _fmt_bn(_bn)
            embed.footer(("Every cent counts." if not _bn_foot else f"Every cent counts. - {_bn_foot}"))
            from core.framework.ui import C_INFO as _C_INFO
            _emb = embed.build()
            _f = await _beg_png(
                badge="SMALL", badge_color=_C_INFO, accent=C_AMBER,
                outcome_label="A small kindness.", delta_usd=gain,
            )
            if _f is not None:
                _emb.set_image(url="attachment://beg.png")
                await ctx.reply(embed=_emb, file=_f, mention_author=False)
            else:
                await ctx.reply(embed=_emb, mention_author=False)
            return

        if roll < _threshold_medium:
            # ── Medium gain: $0.01 - $500 ──
            gain = round(random.uniform(0.01, 500.00) * _beg_mult, 2)
            from services.bottleneck import apply_bottleneck, CreditKind
            _bn = await apply_bottleneck(
                ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
                gross_raw=to_raw(gain), kind=CreditKind.BEG,
            )
            gain = round(_bn.total_to_wallet_raw / 10**18, 2)
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, _bn.total_to_wallet_raw)
            line = random.choice(self._BEG_MEDIUM_LINES).format(amount=fmt_usd(gain))
            if _beg_others:
                _social = await _get_social_ai_flavor(
                    "beg", "medium", ctx.author.display_name, _beg_others, fmt_usd(gain),
                )
                if _social:
                    line = _social
            embed = card("🫳 Begging...", color=C_AMBER)
            embed.description(line)
            from core.framework.ui import fmt_bottleneck as _fmt_bn
            _bn_foot = _fmt_bn(_bn)
            embed.footer(("Not bad for a beggar." if not _bn_foot else f"Not bad for a beggar. - {_bn_foot}"))
            from core.framework.ui import C_PURPLE as _C_PURPLE
            _emb = embed.build()
            _f = await _beg_png(
                badge="MED", badge_color=_C_PURPLE, accent=C_AMBER,
                outcome_label="A solid haul.", delta_usd=gain,
            )
            if _f is not None:
                _emb.set_image(url="attachment://beg.png")
                await ctx.reply(embed=_emb, file=_f, mention_author=False)
            else:
                await ctx.reply(embed=_emb, mention_author=False)
            return

        if roll < _threshold_jackpot:
            # ── Jackpot: gain $500 - $50,000 ──
            gain = round(random.uniform(500, 50_000) * _beg_mult, 2)
            from services.bottleneck import apply_bottleneck, CreditKind
            _bn = await apply_bottleneck(
                ctx.db, uid=ctx.author.id, gid=ctx.guild_id,
                gross_raw=to_raw(gain), kind=CreditKind.BEG,
            )
            gain = round(_bn.total_to_wallet_raw / 10**18, 2)
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, _bn.total_to_wallet_raw)

            line = random.choice(self._BEG_JACKPOT_LINES).format(amount=fmt_usd(gain))
            if _beg_others:
                _social = await _get_social_ai_flavor(
                    "beg", "jackpot", ctx.author.display_name, _beg_others, fmt_usd(gain),
                )
                if _social:
                    line = _social
            embed = card("🫳 Begging... JACKPOT!", color=C_BUY)
            embed.description(line)
            embed.field("Wallet", f"{fmt_usd(wallet)} -> {fmt_usd(wallet + gain)}", True)
            from core.framework.ui import fmt_bottleneck as _fmt_bn
            _bn_foot = _fmt_bn(_bn)
            embed.footer(("Sometimes the streets pay off." if not _bn_foot else f"Sometimes the streets pay off. - {_bn_foot}"))
            from core.framework.ui import C_SUCCESS as _C_SUCCESS
            _emb_jp = embed.build()
            _fjp = await _beg_png(
                badge="JACKPOT", badge_color=_C_SUCCESS, accent=C_GOLD,
                outcome_label="JACKPOT.", delta_usd=gain,
            )
            if _fjp is not None:
                _emb_jp.set_image(url="attachment://beg.png")
                _jp_msg = await ctx.reply(
                    embed=_emb_jp, file=_fjp, mention_author=False,
                )
            else:
                _jp_msg = await ctx.reply(embed=_emb_jp, mention_author=False)

            # Log jackpot as a server event
            try:
                await ctx.db.log_server_event(
                    ctx.guild_id, ctx.channel.id, ctx.author.id,
                    "jackpot",
                    f"{ctx.author.display_name} hit a beg jackpot - received ${gain:,.2f}",
                    gain,
                    {"command": "beg"},
                )
                mark_hot_channel(ctx.guild_id, ctx.channel.id)
            except Exception:
                pass

            # Autonomous bot reaction to jackpot
            try:
                _social_cog = ctx.bot.get_cog("SocialContext")
                if _social_cog and _jp_msg:
                    asyncio.create_task(_social_cog.react_to_event(ctx.channel, _jp_msg, "jackpot"))
            except Exception:
                pass
            return

        # ── Catastrophe: lose ~90% of all CeFi ──
        _catastrophe_template = random.choice(self._BEG_CATASTROPHE_LINES)
        drain_pct = random.uniform(0.85, 0.95)  # lose 85-95%

        losses = []
        total_usd_lost = 0.0

        # Drain wallet
        if wallet > 0.01:
            wallet_loss = round(wallet * drain_pct, 2)
            await ctx.db.update_wallet(ctx.author.id, ctx.guild_id, -to_raw(wallet_loss))
            losses.append(f"Wallet: -${wallet_loss:,.2f}")
            total_usd_lost += wallet_loss

        # Drain bank
        if bank > 0.01:
            bank_loss = round(bank * drain_pct, 2)
            await ctx.db.update_bank(ctx.author.id, ctx.guild_id, -to_raw(bank_loss))
            losses.append(f"Bank: -${bank_loss:,.2f}")
            total_usd_lost += bank_loss

        # Drain CeFi crypto holdings
        holdings = await ctx.db.get_holdings(ctx.author.id, ctx.guild_id)
        for h in holdings:
            amount = h.h("amount")
            if amount <= 0:
                continue
            sym = h["symbol"]
            loss_amount = round(amount * drain_pct, 8)
            try:
                await ctx.db.update_holding(ctx.author.id, ctx.guild_id, sym, -to_raw(loss_amount))
                # Convert to USD for display
                price_row = await ctx.db.get_price(sym, ctx.guild_id)
                usd_val = loss_amount * float(price_row["price"]) if price_row else 0.0
                token_cfg = Config.TOKENS.get(sym, {})
                emoji = token_cfg.get("emoji", "●")
                losses.append(f"{emoji} {sym}: -{loss_amount:,.6f} (~${usd_val:,.2f})")
                total_usd_lost += usd_val
            except ValueError:
                pass

        line = _catastrophe_template.format(amount=fmt_usd(total_usd_lost))
        if _beg_others:
            _social = await _get_social_ai_flavor(
                "beg", "catastrophe", ctx.author.display_name, _beg_others,
                fmt_usd(total_usd_lost),
            )
            if _social:
                line = _social
        embed = card("🫳 Begging... CATASTROPHE!", color=C_ERROR)
        embed.description(line)
        if losses:
            embed.field("Losses", "\n".join(losses[:10]), False)
        pct_display = round(drain_pct * 100, 0)
        embed.footer(f"Lost {pct_display:.0f}% of your CeFi assets. DeFi wallets untouched.")
        _emb_cat = embed.build()
        _fcat = await _beg_png(
            badge="LOSS", badge_color=C_ERROR, accent=C_ERROR,
            outcome_label=f"Lost {pct_display:.0f}% of CeFi assets.",
            delta_usd=-float(total_usd_lost),
        )
        if _fcat is not None:
            _emb_cat.set_image(url="attachment://beg.png")
            _cat_msg = await ctx.reply(
                embed=_emb_cat, file=_fcat, mention_author=False,
            )
        else:
            _cat_msg = await ctx.reply(embed=_emb_cat, mention_author=False)

        # Log catastrophe as a server event for AI gossip
        try:
            await ctx.db.log_server_event(
                ctx.guild_id, ctx.channel.id, ctx.author.id,
                "catastrophe",
                f"{ctx.author.display_name} got drained on a beg - lost ${total_usd_lost:,.2f}",
                total_usd_lost,
                {"command": "beg", "drain_pct": round(drain_pct, 3)},
            )
            mark_hot_channel(ctx.guild_id, ctx.channel.id)
        except Exception:
            pass

        # Autonomous bot reaction to the catastrophe
        try:
            _social_cog = ctx.bot.get_cog("SocialContext")
            if _social_cog and _cat_msg:
                asyncio.create_task(_social_cog.react_to_event(ctx.channel, _cat_msg, "catastrophe"))
        except Exception:
            pass



# ── /exploit flavor text ──────────────────────────────────────────────────────
# Flavor text for the /exploit command (social engineering, phishing, smart-contract heists).
# Supports {amount} substitution at runtime.
_EXPLOIT_FLAVORS: list[str] = [
    "You noticed a logic error in an unverified contract on a ghost chain. You didn't report it. You filed it under 'finders keepers'. You stole **{amount}**.",
    "You built a fake 'Revoke.cash' phishing site and waited for panic-sellers to connect their wallets during a market crash. The fishing was excellent today. You stole **{amount}**.",
    "You convinced a 'Boomer' investor that his seed phrase needed to be 'synchronized with the blockchain' via your Google Form. He fell for it immediately. You stole **{amount}**.",
    "You deployed a sandwich bot on a low-cap DEX and caught a whale mid-transaction. The attack was textbook. The profit was not. You stole **{amount}**.",
    "You hijacked a forgotten 2017-era project's Twitter account and posted a 'surprise airdrop' link. The clicks poured in. The wallets opened. You stole **{amount}**.",
    "You found a discarded Ledger box in a dumpster with the recovery sheet still inside. Someone's cold storage just got a lot warmer. You stole **{amount}**.",
    "You spent three weeks social engineering a Discord Mod into clicking a 'PDF' that was a session-token stealer. Admin rights acquired. Treasury drained. You stole **{amount}**.",
    "You exploited a re-entrancy bug in a 'revolutionary' yield farm before their audit was even submitted. Code is law and you were the legislature today. You stole **{amount}**.",
    "You deployed a honeypot token with a 99% sell tax. Everyone could buy but nobody could sell  -  except for you. The exit was clean. You stole **{amount}**.",
    "You DM'd a whale pretending to be MetaMask Support and convinced them to 'verify' their seed phrase on a Google Form. It actually worked. You stole **{amount}**.",
]


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Earn(bot))
