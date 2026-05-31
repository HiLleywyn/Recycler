"""services/sage.py  -  Sage Network economy + game logic.

The Sage Network is a closed earn-only economy attached to a three-game
educational quiz surface (,pattern / ,gauge / ,tknom). Each correct answer
mints a small SAGE drip (10% of the reward) and EDU game token (90%),
mirroring the fishing LURE/REEL split. Players stake EDU to drip more
SAGE; SAGE -> USD via burn cashout closes the loop. No stablecoin --
shop items are priced in SAGE, the only USD off-ramp is ``,sage cashout``.

This module owns:

* Game session lifecycle (`start_session` / `clear_session` / `has_active`)
  -- the DB row backs the AI mid-game refusal in `cogs/disco_ai.py`.
* Round resolution (`resolve_round`) -- credits SAGE + EDU on a correct
  answer, applies sage-level payout multiplier.
* Run summary (`finalise_run`) -- records the run in `sage_runs` for the
  leaderboard, bumps mastery/quest event hooks.
* Stake / unstake / claim of EDU (mirrors services/gamba.py shape).
* SAGE -> USD burn cashout (mirrors services/gamba.cashout_gbc).
* Leaderboard reads for the three games.

Per CLAUDE.md: all DB timestamps via DB clock, raw monetary columns
held as scaled int via core.framework.scale, embed formatting via
core.framework.ui.
"""
from __future__ import annotations

import datetime as _dt
import logging
import random
import time as _time
from dataclasses import dataclass
from typing import Any

import configs.sage_config as sc
from core.config import Config
from core.framework.scale import to_human, to_raw

log = logging.getLogger(__name__)


# ── Constants pulled out of Config ─────────────────────────────────────
SAGE_NETWORK: str = "Sage Network"
SAGE_NETWORK_SHORT: str = Config.SAGE_NETWORK_SHORT
SAGE_SYMBOL: str = Config.SAGE_COIN
EDU_SYMBOL: str = Config.SAGE_GAME_TOKEN_SYM
STAKE_SAGE_PER_DAY: float = float(Config.SAGE_STAKE_RATE_PER_DAY)
REWARD_USD_BASE: float = float(Config.SAGE_REWARD_USD_BASE)
REWARD_ROUND_MULT: float = float(Config.SAGE_REWARD_ROUND_MULT)
REWARD_MAX_ROUND_MULT: float = float(Config.SAGE_REWARD_MAX_ROUND_MULT)
COIN_SHARE: float = float(Config.SAGE_COIN_SHARE)
TOKEN_SHARE: float = float(Config.SAGE_TOKEN_SHARE)
CASHOUT_LP_REWARD_BPS: int = int(Config.SAGE_CASHOUT_LP_REWARD_BPS)


# ============================================================================
# Round reward math
# ============================================================================

def round_reward_usd(round_index: int) -> float:
    """USD-equivalent reward for getting round ``round_index`` correct.

    Round 1 = base, scales linearly per round up to MAX_ROUND_MULT.
    """
    mult = 1.0 + max(0, int(round_index) - 1) * REWARD_ROUND_MULT
    mult = min(mult, REWARD_MAX_ROUND_MULT)
    return REWARD_USD_BASE * mult


def split_reward(usd_value: float) -> tuple[float, float]:
    """Split a USD reward into (SAGE_human, EDU_human).

    SAGE_human represents the network coin amount denominated in SAGE
    units (priced 1 SAGE = $1.00 at genesis). EDU_human represents the
    game token (priced $0.10 at genesis -- so 9 EDU per $0.90 reward).
    Both default to the token's start_price for the conversion since
    that keeps the math deterministic vs the live oracle.
    """
    sage_usd = usd_value * COIN_SHARE
    edu_usd = usd_value * TOKEN_SHARE
    sage_price = float(Config.TOKENS.get(SAGE_SYMBOL, {}).get("start_price", 1.0) or 1.0)
    edu_price = float(Config.TOKENS.get(EDU_SYMBOL, {}).get("start_price", 0.10) or 0.10)
    sage_amt = sage_usd / max(0.0001, sage_price)
    edu_amt = edu_usd / max(0.0001, edu_price)
    return sage_amt, edu_amt


# ============================================================================
# Active-session lock  -  drives the AI mid-game refusal
# ============================================================================

async def has_active(db: Any, guild_id: int, user_id: int) -> bool:
    """Return True if the user has an open Sage game in progress.

    The lock auto-expires after 5 minutes so a dropped run never leaves
    the AI permanently muted -- if the player abandons a game and comes
    back later, the AI helps them again.
    """
    try:
        row = await db.fetch_one(
            """
            SELECT EXTRACT(EPOCH FROM (NOW() - started_at))::int AS age_s
              FROM sage_active
             WHERE guild_id = $1 AND user_id = $2
            """,
            int(guild_id), int(user_id),
        )
    except Exception:
        # Table missing (migration hasn't run yet) -- no-op rather than crash.
        return False
    if not row:
        return False
    age = int(row.get("age_s") or 0)
    if age > 300:
        await clear_session(db, guild_id, user_id)
        return False
    return True


async def start_session(
    db: Any, guild_id: int, user_id: int, game: str,
) -> None:
    """Mark the user as mid-game for ``game``. Idempotent."""
    if game not in sc.GAMES:
        return
    try:
        await db.execute(
            """
            INSERT INTO sage_active (user_id, guild_id, game, started_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (user_id, guild_id) DO UPDATE
                SET game = EXCLUDED.game, started_at = NOW()
            """,
            int(user_id), int(guild_id), game,
        )
    except Exception:
        log.debug("sage.start_session: %s", game, exc_info=True)


async def clear_session(db: Any, guild_id: int, user_id: int) -> None:
    """Remove the active-game lock. Always safe to call."""
    try:
        await db.execute(
            "DELETE FROM sage_active WHERE user_id=$1 AND guild_id=$2",
            int(user_id), int(guild_id),
        )
    except Exception:
        log.debug("sage.clear_session", exc_info=True)


def random_refusal(rng: random.Random | None = None) -> str:
    """Return a random Disco quip used when the AI declines to help mid-game."""
    r = rng or random
    return r.choice(sc.AI_REFUSAL_QUIPS)


# ============================================================================
# User progression  -  sage XP / level
# ============================================================================

@dataclass
class SageProgress:
    user_id: int
    guild_id: int
    sage_xp: int
    sage_level: int
    lifetime_correct: int
    lifetime_runs: int
    best_pattern_streak: int
    best_gauge_streak: int
    best_tknom_streak: int
    best_cycle_streak: int
    total_sage_earned_raw: int
    total_edu_earned_raw: int


async def ensure_user(db: Any, guild_id: int, user_id: int) -> dict:
    """Insert an empty user_sage row if missing, return the current row."""
    await db.execute(
        """
        INSERT INTO user_sage (user_id, guild_id)
        VALUES ($1, $2)
        ON CONFLICT (user_id, guild_id) DO NOTHING
        """,
        int(user_id), int(guild_id),
    )
    row = await db.fetch_one(
        "SELECT * FROM user_sage WHERE user_id=$1 AND guild_id=$2",
        int(user_id), int(guild_id),
    )
    return dict(row or {})


async def get_progress(db: Any, guild_id: int, user_id: int) -> SageProgress:
    row = await ensure_user(db, guild_id, user_id)
    xp = int(row.get("sage_xp") or 0)
    return SageProgress(
        user_id=int(user_id),
        guild_id=int(guild_id),
        sage_xp=xp,
        sage_level=sc.sage_level_from_xp(xp),
        lifetime_correct=int(row.get("lifetime_correct") or 0),
        lifetime_runs=int(row.get("lifetime_runs") or 0),
        best_pattern_streak=int(row.get("best_pattern_streak") or 0),
        best_gauge_streak=int(row.get("best_gauge_streak") or 0),
        best_tknom_streak=int(row.get("best_tknom_streak") or 0),
        best_cycle_streak=int(row.get("best_cycle_streak") or 0),
        total_sage_earned_raw=int(row.get("total_sage_earned_raw") or 0),
        total_edu_earned_raw=int(row.get("total_edu_earned_raw") or 0),
    )


# ============================================================================
# Round resolution
# ============================================================================

@dataclass
class RoundResult:
    correct: bool
    sage_credited_raw: int
    edu_credited_raw: int
    xp_gained: int
    new_total_xp: int
    new_level: int
    leveled_up: bool


async def resolve_round(
    db: Any, guild_id: int, user_id: int, game: str,
    *, correct: bool, round_index: int, compound: bool = False,
    xp_mult: float = 1.0,
) -> RoundResult:
    """Resolve a single round. On a correct answer, mint SAGE + EDU and
    bump the user's sage XP. On a wrong answer, no mint and no XP.

    ``compound=True`` applies ``sc.COMPOUND_REWARD_MULT`` to both the USD
    payout and the XP gain. Used by Pattern Lab's spliced compound rounds
    where the player had to identify both halves correctly to earn the round.

    ``xp_mult`` is an extra XP multiplier from a consumed Scholar's Draft
    shop item (1.0 = no item). It scales only the XP gain, never the SAGE /
    EDU mint, so it cannot inflate the token supply.
    """
    state = await ensure_user(db, guild_id, user_id)
    cur_xp = int(state.get("sage_xp") or 0)
    cur_level = sc.sage_level_from_xp(cur_xp)

    sage_raw = 0
    edu_raw = 0
    xp_gain = 0

    if correct:
        usd_value = round_reward_usd(round_index)
        # Level-scaled payout (same shape as fishing/farming).
        usd_value *= sc.sage_level_payout_mult(cur_level)
        # Compound rounds (both halves correct) pay a bonus multiplier.
        if compound:
            usd_value *= float(sc.COMPOUND_REWARD_MULT)
        sage_h, edu_h = split_reward(usd_value)
        sage_raw = int(to_raw(sage_h))
        edu_raw = int(to_raw(edu_h))
        xp_gain = int(sc.SAGE_XP_PER_CORRECT)
        if compound:
            xp_gain = int(round(xp_gain * float(sc.COMPOUND_REWARD_MULT)))
        if xp_mult and xp_mult != 1.0:
            xp_gain = int(round(xp_gain * float(xp_mult)))

        # Mint into the Sage Network wallet.
        if sage_raw > 0:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id,
                    SAGE_NETWORK_SHORT, SAGE_SYMBOL, sage_raw,
                )
            except Exception:
                log.exception("sage.resolve_round: SAGE mint failed")
                sage_raw = 0
        if edu_raw > 0:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id,
                    SAGE_NETWORK_SHORT, EDU_SYMBOL, edu_raw,
                )
            except Exception:
                log.exception("sage.resolve_round: EDU mint failed")
                edu_raw = 0

    new_xp = cur_xp + xp_gain
    new_level = sc.sage_level_from_xp(new_xp)
    await db.execute(
        """
        UPDATE user_sage
           SET sage_xp                = $3,
               sage_level             = $4,
               total_sage_earned_raw  = total_sage_earned_raw + $5::numeric,
               total_edu_earned_raw   = total_edu_earned_raw + $6::numeric,
               lifetime_correct       = lifetime_correct + CASE WHEN $7 THEN 1 ELSE 0 END,
               updated_at             = NOW()
         WHERE user_id = $1 AND guild_id = $2
        """,
        int(user_id), int(guild_id),
        int(new_xp), int(new_level),
        int(sage_raw), int(edu_raw),
        bool(correct),
    )
    return RoundResult(
        correct=bool(correct),
        sage_credited_raw=int(sage_raw),
        edu_credited_raw=int(edu_raw),
        xp_gained=int(xp_gain),
        new_total_xp=int(new_xp),
        new_level=int(new_level),
        leveled_up=int(new_level) > int(cur_level),
    )


# ============================================================================
# Run summary  -  records to sage_runs for leaderboards
# ============================================================================

@dataclass
class RunResult:
    run_id: int
    game: str
    score: int
    best_for_user: bool
    total_sage_raw: int
    total_edu_raw: int


async def finalise_run(
    db: Any, guild_id: int, user_id: int, game: str,
    *, score: int, total_sage_raw: int, total_edu_raw: int,
) -> RunResult:
    """Record a finished run into ``sage_runs`` and update bests."""
    score = max(0, int(score))
    field = {
        sc.GAME_PATTERN: "best_pattern_streak",
        sc.GAME_GAUGE:   "best_gauge_streak",
        sc.GAME_TKNOM:   "best_tknom_streak",
        sc.GAME_CYCLE:   "best_cycle_streak",
    }.get(game)
    if field is None:
        raise ValueError(f"Unknown sage game: {game}")

    run_id = await db.fetch_val(
        """
        INSERT INTO sage_runs
            (user_id, guild_id, game, score, sage_earned_raw, edu_earned_raw, ended_at)
        VALUES ($1, $2, $3, $4, $5::numeric, $6::numeric, NOW())
        RETURNING run_id
        """,
        int(user_id), int(guild_id), game, int(score),
        int(total_sage_raw), int(total_edu_raw),
    )

    cur_best = await db.fetch_val(
        f"SELECT {field} FROM user_sage WHERE user_id=$1 AND guild_id=$2",
        int(user_id), int(guild_id),
    )
    cur_best_v = int(cur_best or 0)
    is_best = score > cur_best_v
    if is_best:
        await db.execute(
            f"UPDATE user_sage SET {field} = $3, updated_at = NOW() "
            "WHERE user_id = $1 AND guild_id = $2",
            int(user_id), int(guild_id), int(score),
        )
    await db.execute(
        "UPDATE user_sage SET lifetime_runs = lifetime_runs + 1 "
        "WHERE user_id = $1 AND guild_id = $2",
        int(user_id), int(guild_id),
    )
    await clear_session(db, guild_id, user_id)

    # Emit a mastery/quest event so cross-game systems can react. Use
    # the bus if available; otherwise no-op so a missing infra dep
    # doesn't break the cog.
    try:
        from services.mastery import add_mastery
        await add_mastery(db, int(user_id), int(guild_id), "sage_scholar", int(score) * 2)
    except Exception:
        log.debug("sage.finalise_run: mastery add skipped", exc_info=True)

    return RunResult(
        run_id=int(run_id or 0),
        game=game,
        score=int(score),
        best_for_user=bool(is_best),
        total_sage_raw=int(total_sage_raw),
        total_edu_raw=int(total_edu_raw),
    )


# ============================================================================
# Leaderboards
# ============================================================================

async def top_runs(
    db: Any, guild_id: int, game: str, *, limit: int = 10,
) -> list[dict]:
    """Top scores for a single Sage game, best score per user."""
    if game not in sc.GAMES:
        raise ValueError(f"Unknown sage game: {game}")
    rows = await db.fetch_all(
        """
        SELECT user_id, MAX(score) AS best,
               MAX(ended_at) AS last_at
          FROM sage_runs
         WHERE guild_id = $1 AND game = $2
         GROUP BY user_id
         ORDER BY best DESC, last_at ASC
         LIMIT $3
        """,
        int(guild_id), game, int(limit),
    )
    return [dict(r) for r in (rows or [])]


async def top_levels(
    db: Any, guild_id: int, *, limit: int = 10,
) -> list[dict]:
    """Top Sage-network players by level, then XP, then earliest registered."""
    rows = await db.fetch_all(
        """
        SELECT user_id, sage_level, sage_xp,
               lifetime_correct, lifetime_runs, created_at
          FROM user_sage
         WHERE guild_id = $1
         ORDER BY sage_level DESC, sage_xp DESC, created_at ASC
         LIMIT $2
        """,
        int(guild_id), int(limit),
    )
    return [dict(r) for r in (rows or [])]


# ============================================================================
# Sage Shop  -  SAGE-priced consumables
# ============================================================================

async def get_items(db: Any, guild_id: int, user_id: int) -> dict[str, int]:
    """Return the user's owned shop consumables as ``{item_key: qty}``."""
    try:
        rows = await db.fetch_all(
            """
            SELECT item_key, qty FROM sage_items
             WHERE user_id = $1 AND guild_id = $2 AND qty > 0
            """,
            int(user_id), int(guild_id),
        )
    except Exception:
        # Table missing (migration not yet applied) -- treat as empty.
        return {}
    return {r["item_key"]: int(r["qty"]) for r in (rows or [])}


async def buy_item(
    db: Any, guild_id: int, user_id: int, item_key: str, qty: int = 1,
) -> float:
    """Buy ``qty`` of a shop item, burning SAGE from the user's wallet.

    Returns the SAGE cost. Raises ValueError on a bad item, non-positive
    quantity, or insufficient SAGE balance.
    """
    item = sc.SAGE_SHOP_ITEMS.get(item_key)
    if item is None:
        raise ValueError(f"Unknown shop item: `{item_key}`.")
    qty = int(qty)
    if qty <= 0:
        raise ValueError("Quantity must be a positive whole number.")
    cost_sage = float(item["price_sage"]) * qty
    cost_raw = int(to_raw(cost_sage))
    if cost_raw <= 0:
        raise ValueError("Quantity too small.")
    held = await get_sage_wallet_raw(db, guild_id, user_id)
    if held < cost_raw:
        raise ValueError(
            f"That costs {cost_sage:,.4f} SAGE -- you only have "
            f"{to_human(held):,.4f} SAGE."
        )
    async with db.atomic():
        await db.update_wallet_holding(
            int(user_id), int(guild_id),
            SAGE_NETWORK_SHORT, SAGE_SYMBOL, -cost_raw,
        )
        await db.execute(
            """
            INSERT INTO sage_items (user_id, guild_id, item_key, qty, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_id, guild_id, item_key) DO UPDATE
                SET qty = sage_items.qty + EXCLUDED.qty, updated_at = NOW()
            """,
            int(user_id), int(guild_id), item_key, qty,
        )
    return cost_sage


async def consume_item(
    db: Any, guild_id: int, user_id: int, item_key: str, n: int = 1,
) -> bool:
    """Decrement an owned shop item by ``n``. Returns True if stock allowed it.

    Atomic: the conditional UPDATE only fires when ``qty >= n``, so a
    concurrent run can never drive the balance negative.
    """
    try:
        status = await db.execute(
            """
            UPDATE sage_items SET qty = qty - $4, updated_at = NOW()
             WHERE user_id = $1 AND guild_id = $2 AND item_key = $3 AND qty >= $4
            """,
            int(user_id), int(guild_id), item_key, int(n),
        )
    except Exception:
        log.debug("sage.consume_item: %s", item_key, exc_info=True)
        return False
    # asyncpg returns a status tag like "UPDATE 1" / "UPDATE 0".
    tail = (str(status or "").strip().split() or ["0"])[-1]
    return tail.isdigit() and int(tail) > 0


async def apply_run_perks(
    db: Any, guild_id: int, user_id: int,
) -> dict:
    """Consume the run-start shop consumables and return the active perks.

    time_crystal / insight_lens / scholar_draft are spent here (one each).
    second_wind is NOT spent here -- it is consumed lazily in the cog only
    if it actually saves a wrong answer, so a clean run never burns it.
    """
    perks: dict = {
        "bonus_time":    0,
        "fewer_options": False,
        "xp_mult":       1.0,
        "consumed":      [],
    }
    if await consume_item(db, guild_id, user_id, "time_crystal", 1):
        perks["bonus_time"] = int(sc.SAGE_TIME_CRYSTAL_BONUS_S)
        perks["consumed"].append("time_crystal")
    if await consume_item(db, guild_id, user_id, "insight_lens", 1):
        perks["fewer_options"] = True
        perks["consumed"].append("insight_lens")
    if await consume_item(db, guild_id, user_id, "scholar_draft", 1):
        perks["xp_mult"] = float(sc.SAGE_SCHOLAR_DRAFT_XP_MULT)
        perks["consumed"].append("scholar_draft")
    return perks


# ============================================================================
# EDU staking  -  mirrors gamba_stakes shape, scoped to a single token
# ============================================================================

@dataclass
class StakeRow:
    user_id: int
    guild_id: int
    staked_raw: int
    pending_yield_raw: int
    total_claimed_raw: int
    yield_paid_raw: int = 0


def _accrue_pending(staked_raw: int, last_at: Any) -> int:
    """Lazy accrual: SAGE-yield raw owed since ``last_at`` for this stake."""
    if staked_raw <= 0 or not last_at:
        return 0
    if isinstance(last_at, _dt.datetime):
        last_ts = last_at.timestamp()
    else:
        last_ts = float(last_at)
    now_ts = float(_time.time())
    elapsed = max(0, int(now_ts - last_ts))
    if elapsed <= 0:
        return 0
    rate_raw = to_raw(STAKE_SAGE_PER_DAY)
    return int((staked_raw * rate_raw * elapsed) // (to_raw(1.0) * 86400))


async def _ensure_stake_row(db: Any, gid: int, uid: int) -> dict:
    await db.execute(
        """
        INSERT INTO sage_stakes (user_id, guild_id)
        VALUES ($1, $2)
        ON CONFLICT (user_id, guild_id) DO NOTHING
        """,
        int(uid), int(gid),
    )
    row = await db.fetch_one(
        "SELECT * FROM sage_stakes WHERE user_id=$1 AND guild_id=$2",
        int(uid), int(gid),
    )
    return dict(row or {})


async def get_stake(db: Any, gid: int, uid: int) -> StakeRow:
    row = await _ensure_stake_row(db, gid, uid)
    return StakeRow(
        user_id=int(uid), guild_id=int(gid),
        staked_raw=int(row.get("amount") or 0),
        pending_yield_raw=int(row.get("pending_yield_raw") or 0),
        total_claimed_raw=int(row.get("total_claimed") or 0),
    )


async def accrued_yield(db: Any, gid: int, uid: int) -> int:
    row = await _ensure_stake_row(db, gid, uid)
    fresh = _accrue_pending(int(row.get("amount") or 0), row.get("last_accrue"))
    return int(row.get("pending_yield_raw") or 0) + fresh


async def stake(db: Any, gid: int, uid: int, amount_raw: int) -> StakeRow:
    """Move EDU from wallet -> stake. Crystallises pending yield first."""
    if amount_raw <= 0:
        raise ValueError("Amount must be positive.")
    row = await _ensure_stake_row(db, gid, uid)
    cur_staked = int(row.get("amount") or 0)
    pending = int(row.get("pending_yield_raw") or 0)
    fresh = _accrue_pending(cur_staked, row.get("last_accrue"))
    new_pending = pending + fresh
    # Burn EDU from wallet (will raise if insufficient).
    await db.update_wallet_holding(
        int(uid), int(gid), SAGE_NETWORK_SHORT, EDU_SYMBOL, -int(amount_raw),
    )
    new_staked = cur_staked + int(amount_raw)
    await db.execute(
        """
        UPDATE sage_stakes
           SET amount            = $3::numeric,
               pending_yield_raw = $4::numeric,
               last_accrue       = NOW()
         WHERE user_id = $1 AND guild_id = $2
        """,
        int(uid), int(gid), int(new_staked), int(new_pending),
    )
    return StakeRow(
        user_id=int(uid), guild_id=int(gid),
        staked_raw=int(new_staked), pending_yield_raw=int(new_pending),
        total_claimed_raw=int(row.get("total_claimed") or 0),
    )


async def unstake(db: Any, gid: int, uid: int, amount_raw: int) -> StakeRow:
    """Move EDU from stake -> wallet. Auto-claims pending SAGE yield."""
    requested = max(0, int(amount_raw))
    row = await _ensure_stake_row(db, gid, uid)
    cur_staked = int(row.get("amount") or 0)
    pending = int(row.get("pending_yield_raw") or 0)
    fresh = _accrue_pending(cur_staked, row.get("last_accrue"))
    payout = pending + fresh
    if cur_staked <= 0 or requested <= 0:
        raise ValueError("You have no EDU staked.")
    actual = min(requested, cur_staked)
    new_staked = cur_staked - actual
    await db.update_wallet_holding(
        int(uid), int(gid), SAGE_NETWORK_SHORT, EDU_SYMBOL, int(actual),
    )
    if payout > 0:
        try:
            await db.update_wallet_holding(
                int(uid), int(gid), SAGE_NETWORK_SHORT, SAGE_SYMBOL, int(payout),
            )
        except Exception:
            log.exception("sage.unstake: yield credit failed")
            payout = 0
    await db.execute(
        """
        UPDATE sage_stakes
           SET amount            = $3::numeric,
               pending_yield_raw = 0,
               total_claimed     = total_claimed + $4::numeric,
               last_accrue       = NOW()
         WHERE user_id = $1 AND guild_id = $2
        """,
        int(uid), int(gid), int(new_staked), int(payout),
    )
    return StakeRow(
        user_id=int(uid), guild_id=int(gid),
        staked_raw=int(new_staked), pending_yield_raw=0,
        total_claimed_raw=int(row.get("total_claimed") or 0) + int(payout),
        yield_paid_raw=int(payout),
    )


async def claim(db: Any, gid: int, uid: int) -> int:
    """Pay out accrued SAGE yield. Returns raw paid."""
    row = await _ensure_stake_row(db, gid, uid)
    cur_staked = int(row.get("amount") or 0)
    pending = int(row.get("pending_yield_raw") or 0)
    fresh = _accrue_pending(cur_staked, row.get("last_accrue"))
    payout = pending + fresh
    if payout <= 0:
        raise ValueError("No yield accrued yet.")
    try:
        await db.update_wallet_holding(
            int(uid), int(gid), SAGE_NETWORK_SHORT, SAGE_SYMBOL, int(payout),
        )
    except Exception:
        log.exception("sage.claim: yield credit failed")
        raise ValueError("Failed to credit yield. Try again shortly.")
    await db.execute(
        """
        UPDATE sage_stakes
           SET pending_yield_raw = 0,
               total_claimed     = total_claimed + $3::numeric,
               last_accrue       = NOW()
         WHERE user_id = $1 AND guild_id = $2
        """,
        int(uid), int(gid), int(payout),
    )
    return int(payout)


def effective_apy_pct() -> float:
    """Headline APY for the SAGE drip on an EDU stake at parity prices."""
    return float(STAKE_SAGE_PER_DAY * 365.0 * 100.0)


# ============================================================================
# SAGE -> USD burn cashout  -  mirrors services/gamba.cashout_gbc
# ============================================================================

def _price_impact_max() -> float:
    return float(getattr(Config, "PRICE_IMPACT_MAX", 0.40))


async def _oracle_price_db(db: Any, gid: int, symbol: str) -> float:
    row = await db.fetch_one(
        "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
        int(gid), symbol.upper(),
    )
    if row and row.get("price") is not None:
        return float(row["price"])
    return float(Config.TOKENS.get(symbol.upper(), {}).get("start_price", 1.0))


async def _supply_human(db: Any, gid: int, symbol: str) -> float:
    row = await db.fetch_one(
        "SELECT circulating_supply FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
        int(gid), symbol.upper(),
    )
    return to_human(int((row or {}).get("circulating_supply") or 0))


def _price_impact(usd_value: float, oracle: float, supply_human: float) -> float:
    impact = usd_value / float(Config.PRICE_IMPACT_DIVISOR)
    market_cap = max(0.0, oracle * supply_human)
    if market_cap > 0 and usd_value > 0.001 * market_cap:
        mc_ratio = usd_value / market_cap
        impact *= min(1.0 + mc_ratio * 2.0, 5.0)
    return min(impact, _price_impact_max())


@dataclass
class CashoutResult:
    sage_burned_raw: int
    usd_credited_raw: int
    sage_oracle_before: float
    sage_oracle_after: float
    price_impact_pct: float
    revenue_usd: float


async def get_sage_wallet_raw(db: Any, gid: int, uid: int) -> int:
    row = await db.get_wallet_holding(
        int(uid), int(gid), SAGE_NETWORK_SHORT, SAGE_SYMBOL,
    )
    return int((row or {}).get("amount") or 0)


async def get_edu_wallet_raw(db: Any, gid: int, uid: int) -> int:
    row = await db.get_wallet_holding(
        int(uid), int(gid), SAGE_NETWORK_SHORT, EDU_SYMBOL,
    )
    return int((row or {}).get("amount") or 0)


async def cashout_sage(
    db: Any, gid: int, uid: int, amount_raw: int,
) -> CashoutResult:
    """Burn SAGE, push the SAGE oracle DOWN, credit the user's USD wallet."""
    if amount_raw <= 0:
        raise ValueError("Amount must be positive.")
    held = await get_sage_wallet_raw(db, gid, uid)
    if held < int(amount_raw):
        raise ValueError(f"You only have {to_human(held):,.4f} SAGE.")

    oracle_before = await _oracle_price_db(db, gid, SAGE_SYMBOL)
    if oracle_before <= 0:
        raise ValueError("SAGE oracle price is currently zero -- try again later.")

    sage_h = to_human(int(amount_raw))
    revenue_usd = sage_h * oracle_before
    supply_h = await _supply_human(db, gid, SAGE_SYMBOL)
    impact = _price_impact(revenue_usd, oracle_before, supply_h)
    eff_price = oracle_before * (1.0 - impact / 2.0)
    usd_credit_h = sage_h * eff_price
    usd_credit_raw = to_raw(usd_credit_h)
    if usd_credit_raw <= 0:
        raise ValueError("Amount too small -- USD credit would round to zero.")

    async with db.atomic():
        await db.update_wallet_holding(
            int(uid), int(gid),
            SAGE_NETWORK_SHORT, SAGE_SYMBOL, -int(amount_raw),
        )
        await db.update_wallet(int(uid), int(gid), int(usd_credit_raw))

    oracle_after = max(1e-9, oracle_before * (1.0 - impact))
    try:
        await db.update_price(SAGE_SYMBOL, int(gid), oracle_after)
    except Exception:
        log.exception("sage cashout: oracle update failed -- chart will lag")

    return CashoutResult(
        sage_burned_raw=int(amount_raw),
        usd_credited_raw=int(usd_credit_raw),
        sage_oracle_before=float(oracle_before),
        sage_oracle_after=float(oracle_after),
        price_impact_pct=float(impact),
        revenue_usd=float(revenue_usd),
    )


__all__ = [
    "SAGE_NETWORK", "SAGE_NETWORK_SHORT", "SAGE_SYMBOL", "EDU_SYMBOL",
    "STAKE_SAGE_PER_DAY", "REWARD_USD_BASE", "COIN_SHARE", "TOKEN_SHARE",
    "SageProgress", "RoundResult", "RunResult", "StakeRow", "CashoutResult",
    "round_reward_usd", "split_reward",
    "has_active", "start_session", "clear_session", "random_refusal",
    "ensure_user", "get_progress",
    "resolve_round", "finalise_run", "top_runs", "top_levels",
    "get_items", "buy_item", "consume_item", "apply_run_perks",
    "get_stake", "accrued_yield", "stake", "unstake", "claim",
    "effective_apy_pct",
    "get_sage_wallet_raw", "get_edu_wallet_raw", "cashout_sage",
]
