"""LP yield service -- pays inflation-style USD rewards to LP providers hourly.

Rationale: with ~40 active players the swap-fee accrual alone yields essentially
zero per-share income, so providing LP is unprofitable in practice and the rest
of the economy stalls (no liquidity = no swaps = no fees). This service pays a
guaranteed USD yield to every active LP position based on its USD value at
oracle prices, with multipliers for time-locked positions, user-created-token
pools, and cross-group partnership pools. Fees still accrue in pool reserves
the same way -- this just adds a baseline reward so LP is meaningful without
relying on swap volume.

Pays out hourly via Trade.lp_yield_tick. Each payout is logged as an LP_YIELD
transaction so it shows up in the user's income history.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.config import Config
from core.framework.scale import to_human, to_raw
from services.liquidity import user_created_token_symbols

log = logging.getLogger(__name__)


@dataclass
class LPYieldTickResult:
    """Aggregate stats from a single guild tick. Logged for diagnostics."""
    user_payouts: int = 0
    group_payouts: int = 0
    total_user_usd: float = 0.0
    total_group_usd: float = 0.0
    skipped_no_price: int = 0
    skipped_min_value: int = 0
    skipped_capped: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "LPYieldTickResult") -> None:
        self.user_payouts += other.user_payouts
        self.group_payouts += other.group_payouts
        self.total_user_usd += other.total_user_usd
        self.total_group_usd += other.total_group_usd
        self.skipped_no_price += other.skipped_no_price
        self.skipped_min_value += other.skipped_min_value
        self.skipped_capped += other.skipped_capped
        self.errors.extend(other.errors)


def _hours_since(epoch_or_dt) -> float:
    """Return elapsed hours between a stored timestamp and now."""
    import time as _time
    if epoch_or_dt is None:
        return float(Config.LP_YIELD_TICK_HOURS)
    if hasattr(epoch_or_dt, "timestamp"):
        ts = epoch_or_dt.timestamp()
    else:
        ts = float(epoch_or_dt)
    return max(0.0, (_time.time() - ts) / 3600.0)


def _lock_multiplier(lock_tier: int, locked_until) -> float:
    """Resolve the active lock-tier multiplier, decaying lapsed locks to 1.0."""
    import time as _time
    tier = int(lock_tier or 0)
    if tier <= 0:
        return 1.0
    if locked_until is not None:
        lu_ts = (
            locked_until.timestamp()
            if hasattr(locked_until, "timestamp")
            else float(locked_until)
        )
        if lu_ts > 0 and _time.time() >= lu_ts:
            return 1.0  # lock expired -- pay the unlocked rate
    return float(Config.LP_YIELD_LOCK_BONUS.get(tier, 1.0))


async def _position_usd_value(
    db, lp_row: dict, price_cache: dict[str, float], guild_id: int,
) -> float:
    """USD value of an LP position: pro-rata share of pool reserves at oracle prices."""
    total_lp_raw = int(lp_row.get("total_lp") or 0)
    lp_shares_raw = int(lp_row.get("lp_shares") or 0)
    if total_lp_raw <= 0 or lp_shares_raw <= 0:
        return 0.0
    frac = lp_shares_raw / total_lp_raw
    val_a = to_human(int(lp_row["reserve_a"])) * frac
    val_b = to_human(int(lp_row["reserve_b"])) * frac
    ta, tb = lp_row["token_a"], lp_row["token_b"]
    for sym in (ta, tb):
        if sym not in price_cache:
            pr = await db.get_price(sym, guild_id)
            price_cache[sym] = float(pr["price"]) if pr else 0.0
    return val_a * price_cache[ta] + val_b * price_cache[tb]


async def _pool_tvl_usd(
    db, lp_row: dict, price_cache: dict[str, float], guild_id: int,
) -> float:
    """Total USD value sitting in the pool right now (independent of share)."""
    val_a = to_human(int(lp_row["reserve_a"]))
    val_b = to_human(int(lp_row["reserve_b"]))
    ta, tb = lp_row["token_a"], lp_row["token_b"]
    for sym in (ta, tb):
        if sym not in price_cache:
            pr = await db.get_price(sym, guild_id)
            price_cache[sym] = float(pr["price"]) if pr else 0.0
    return val_a * price_cache[ta] + val_b * price_cache[tb]


def bootstrap_multiplier(tvl_usd: float, recent_vol_usd: float) -> float:
    """LP bootstrap-incentive multiplier for a pool.

    Returns 1.0 (no bonus) once the pool's TVL OR recent volume crosses
    its threshold; ramps up to ``LP_BOOTSTRAP_MAX_BONUS`` for an empty,
    untraded pool. Both factors must be in the bonus zone for the full
    multiplier so a whaled-but-untraded pool decays to no-bonus along
    the TVL axis even while volume is zero.
    """
    max_bonus = float(Config.LP_BOOTSTRAP_MAX_BONUS)
    if max_bonus <= 1.0:
        return 1.0
    tvl_thresh = max(1.0, float(Config.LP_BOOTSTRAP_TVL_THRESHOLD_USD))
    vol_thresh = max(1.0, float(Config.LP_BOOTSTRAP_VOLUME_THRESHOLD_USD))
    tvl_factor = max(0.0, 1.0 - max(0.0, float(tvl_usd))    / tvl_thresh)
    vol_factor = max(0.0, 1.0 - max(0.0, float(recent_vol_usd)) / vol_thresh)
    decay      = tvl_factor * vol_factor
    return 1.0 + (max_bonus - 1.0) * decay


async def tick_lp_yield_for_guild(db, guild_id: int) -> LPYieldTickResult:
    """Run one hourly LP yield distribution for a single guild.

    Pays USD yield to every user LP position and credits group LP positions
    to their group reserve_usd. Built-in seeded reserves (system-minted) are
    not paid -- only positions actually held by users or groups.
    """
    result = LPYieldTickResult()

    base_apr = float(Config.LP_YIELD_APR)
    if base_apr <= 0:
        return result
    tick_hours = max(0.0001, float(Config.LP_YIELD_TICK_HOURS))
    base_per_tick = base_apr * (tick_hours / (24.0 * 365.0))
    min_usd = float(Config.LP_YIELD_MIN_USD)
    max_usd = float(Config.LP_YIELD_MAX_PER_TICK_USD)
    user_token_bonus = float(Config.LP_YIELD_USER_TOKEN_BONUS)
    group_pool_bonus = float(Config.LP_YIELD_GROUP_POOL_BONUS)

    user_syms = await user_created_token_symbols(db, guild_id)
    price_cache: dict[str, float] = {"USD": 1.0}

    # ── User LP yield ─────────────────────────────────────────────────────
    user_positions = await db.fetch_all(
        """SELECT lp.user_id, lp.pool_id, lp.lp_shares, lp.lock_tier, lp.locked_until,
                  lp.last_yield_at,
                  p.token_a, p.token_b, p.reserve_a, p.reserve_b, p.total_lp,
                  p.is_group_pool, p.vault_locked,
                  COALESCE(p.recent_volume_usd_raw, 0) AS recent_volume_usd_raw
           FROM lp_positions lp
           JOIN pools p ON lp.pool_id=p.pool_id AND lp.guild_id=p.guild_id
           WHERE lp.guild_id=$1 AND lp.lp_shares > 0 AND COALESCE(p.vault_locked, FALSE)=FALSE""",
        guild_id,
    )

    for lp in user_positions:
        try:
            usd_val = await _position_usd_value(db, lp, price_cache, guild_id)
            if usd_val <= 0:
                result.skipped_no_price += 1
                continue
            if usd_val < min_usd:
                result.skipped_min_value += 1
                continue

            elapsed = _hours_since(lp.get("last_yield_at"))
            # Cap elapsed at a single day so a long downtime can't pay 30 days
            # of accrued yield in one tick (which would obliterate the income
            # cap and dump a wave of inflation into the economy at once).
            elapsed = min(elapsed, 24.0)
            if elapsed <= 0:
                continue

            mult = _lock_multiplier(lp.get("lock_tier"), lp.get("locked_until"))
            ta, tb = lp["token_a"], lp["token_b"]
            if user_syms and (ta in user_syms or tb in user_syms):
                mult *= user_token_bonus
            if lp.get("is_group_pool"):
                mult *= group_pool_bonus

            # Pool bootstrap incentive: extra yield on low-TVL / low-volume
            # pools so the first seeders get rewarded for planting capital
            # before swap fees alone make LP worthwhile. Decays on both
            # axes, so the bonus tapers as TVL grows AND as trades start
            # flowing through the pool.
            try:
                pool_tvl = await _pool_tvl_usd(db, lp, price_cache, guild_id)
                recent_vol = to_human(int(lp.get("recent_volume_usd_raw") or 0))
                mult *= bootstrap_multiplier(pool_tvl, recent_vol)
            except Exception:
                log.debug(
                    "lp_yield: bootstrap mult failed pool=%s",
                    lp.get("pool_id"), exc_info=True,
                )

            payout_usd = usd_val * base_per_tick * mult * (elapsed / tick_hours)
            if payout_usd > max_usd:
                payout_usd = max_usd
                result.skipped_capped += 1
            if payout_usd <= 0:
                continue

            # Wealth Bottleneck: LP yield is paid in USD, so the gross
            # raw amount goes straight through apply_bottleneck. Drag
            # feeds the per-guild pool; boost is sourced from it.
            uid = int(lp["user_id"])
            payout_raw = to_raw(payout_usd)
            try:
                from services.bottleneck import apply_bottleneck, CreditKind
                bn_lp = await apply_bottleneck(
                    db, uid=uid, gid=guild_id,
                    gross_raw=int(payout_raw),
                    kind=CreditKind.LP_YIELD,
                )
                effective_raw = bn_lp.total_to_wallet_raw
            except Exception:
                # Bottleneck must never break LP yield itself.
                log.debug("lp_yield: bottleneck failed", exc_info=True)
                effective_raw = int(payout_raw)
            if effective_raw <= 0:
                continue
            async with db.atomic():
                await db.update_wallet(uid, guild_id, int(effective_raw))
                await db.execute(
                    "UPDATE lp_positions "
                    "SET last_yield_at = NOW(), "
                    "    yield_paid_usd_raw = "
                    "        COALESCE(yield_paid_usd_raw, 0) + $4::numeric "
                    "WHERE user_id=$1 AND guild_id=$2 AND pool_id=$3",
                    uid, guild_id, lp["pool_id"], int(effective_raw),
                )
                await db.log_tx(
                    guild_id, uid, "LP_YIELD",
                    symbol_out="USD", amount_out=int(effective_raw),
                    network="usd",
                )
            result.user_payouts += 1
            result.total_user_usd += to_human(int(effective_raw))
        except Exception as exc:
            log.exception(
                "lp_yield: user payout failed gid=%s uid=%s pool=%s",
                guild_id, lp.get("user_id"), lp.get("pool_id"),
            )
            result.errors.append(f"user {lp.get('user_id')}/{lp.get('pool_id')}: {exc}")

    # ── Group LP yield (paid to group reserve_usd, no per-user log_tx) ────
    group_positions = await db.fetch_all(
        """SELECT glp.group_id, glp.pool_id, glp.lp_shares, glp.last_yield_at,
                  p.token_a, p.token_b, p.reserve_a, p.reserve_b, p.total_lp,
                  p.is_group_pool, p.vault_locked,
                  COALESCE(p.recent_volume_usd_raw, 0) AS recent_volume_usd_raw
           FROM group_lp_positions glp
           JOIN pools p ON glp.pool_id=p.pool_id AND glp.guild_id=p.guild_id
           WHERE glp.guild_id=$1 AND glp.lp_shares > 0 AND COALESCE(p.vault_locked, FALSE)=FALSE""",
        guild_id,
    )

    for glp in group_positions:
        try:
            usd_val = await _position_usd_value(db, glp, price_cache, guild_id)
            if usd_val <= 0:
                result.skipped_no_price += 1
                continue
            if usd_val < min_usd:
                result.skipped_min_value += 1
                continue

            elapsed = min(_hours_since(glp.get("last_yield_at")), 24.0)
            if elapsed <= 0:
                continue

            mult = 1.0
            ta, tb = glp["token_a"], glp["token_b"]
            if user_syms and (ta in user_syms or tb in user_syms):
                mult *= user_token_bonus
            if glp.get("is_group_pool"):
                mult *= group_pool_bonus

            try:
                pool_tvl = await _pool_tvl_usd(db, glp, price_cache, guild_id)
                recent_vol = to_human(int(glp.get("recent_volume_usd_raw") or 0))
                mult *= bootstrap_multiplier(pool_tvl, recent_vol)
            except Exception:
                log.debug(
                    "lp_yield: bootstrap mult failed pool=%s",
                    glp.get("pool_id"), exc_info=True,
                )

            payout_usd = usd_val * base_per_tick * mult * (elapsed / tick_hours)
            if payout_usd > max_usd:
                payout_usd = max_usd
                result.skipped_capped += 1
            if payout_usd <= 0:
                continue

            gid_str = str(glp["group_id"])
            payout_raw = to_raw(payout_usd)
            async with db.atomic():
                await db.add_group_reserve_usd(guild_id, gid_str, payout_usd)
                await db.execute(
                    "UPDATE group_lp_positions "
                    "SET last_yield_at = NOW(), "
                    "    yield_paid_usd_raw = "
                    "        COALESCE(yield_paid_usd_raw, 0) + $4::numeric "
                    "WHERE guild_id=$1 AND group_id=$2 AND pool_id=$3",
                    guild_id, gid_str, glp["pool_id"], int(payout_raw),
                )
            result.group_payouts += 1
            result.total_group_usd += payout_usd
        except Exception as exc:
            log.exception(
                "lp_yield: group payout failed gid=%s group=%s pool=%s",
                guild_id, glp.get("group_id"), glp.get("pool_id"),
            )
            result.errors.append(f"group {glp.get('group_id')}/{glp.get('pool_id')}: {exc}")

    # Per-tick decay on the rolling pool-volume counter so a once-busy
    # pool that goes quiet eases back into bootstrap-bonus territory
    # over time. Multiplicative decay so the half-life is fixed regardless
    # of how big the counter got.
    try:
        decay = float(Config.LP_BOOTSTRAP_VOLUME_DECAY_PER_TICK)
        if 0.0 < decay < 1.0:
            keep = max(0.0, 1.0 - decay)
            await db.execute(
                "UPDATE pools "
                "SET recent_volume_usd_raw = "
                "    (recent_volume_usd_raw::NUMERIC * $1)::NUMERIC(36,0), "
                "    recent_volume_window_at = NOW() "
                "WHERE guild_id = $2 "
                "  AND recent_volume_usd_raw > 0",
                keep, guild_id,
            )
    except Exception:
        log.debug(
            "lp_yield: bootstrap volume decay failed gid=%s",
            guild_id, exc_info=True,
        )

    return result


def estimate_position_apr(lp_row: dict, user_syms: set[str]) -> float:
    """Effective LP-yield APR for one position (for `,mylp` display).

    Combines base APR with lock multiplier, user-token bonus, and group-pool
    bonus. The number is "what fraction of this position's USD value will
    accumulate as yield over a year, ignoring compounding", which is exactly
    the input to the user's mental model when they see "X% APR on $Y".
    """
    apr = float(Config.LP_YIELD_APR)
    mult = _lock_multiplier(lp_row.get("lock_tier"), lp_row.get("locked_until"))
    ta, tb = lp_row.get("token_a"), lp_row.get("token_b")
    if user_syms and (ta in user_syms or tb in user_syms):
        mult *= float(Config.LP_YIELD_USER_TOKEN_BONUS)
    if lp_row.get("is_group_pool"):
        mult *= float(Config.LP_YIELD_GROUP_POOL_BONUS)
    return apr * mult
