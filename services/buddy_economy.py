"""
services/buddy_economy.py  -  Buddy Network FREN stake + BUD economy.

Mirrors the surface area of services/fishing.py's LURE/REEL economy and
services/dungeon.py's ORE/RUNE economy:

  * FREN -> stake -> BUD yield   (passive accrual on a DB-side clock)
  * BUD <-> {FREN, REEL, RUNE, MOON} burn-swap with slippage on both
    oracles (matching the existing fishing/dungeon mint+burn pattern)
  * BUD -> USD cashout via burn at oracle minus impact (only off-ramp)
  * Buddy Shop sinks: battle slot, storage slot, egg storage,
    battle attractor. Each burns BUD with the standard burn impact +
    LP-reward fan-out so a USD-rich whale can't grief the BUD oracle
    by spamming purchases.

Public API (chunked across this module):
    ensure_state(db, gid, uid)                  -> dict
    list_state(db, gid, uid)                    -> dict
    accrued_yield(db, gid, uid)                 -> int  (raw BUD)
    stake_fren(db, gid, uid, fren_amount_raw)   -> StakeResult
    unstake_fren(db, gid, uid, fren_amount_raw) -> StakeResult
    claim_yield(db, gid, uid)                   -> StakeResult
    burn_for_bud(db, gid, uid, sym, amt_raw)    -> BurnResult  (any BUD partner -> BUD)
    burn_bud_for(db, gid, uid, sym, amt_raw)    -> BurnResult  (BUD -> any BUD partner)
    cashout_bud(db, gid, uid, amt_raw)          -> CashoutResult  (BUD -> USD)
    purchase_battle_slot(db, gid, uid)          -> SlotResult
    purchase_storage_slot(db, gid, uid)         -> SlotResult
    purchase_egg_storage(db, gid, uid)          -> SlotResult
    purchase_nest_slot(db, gid, uid)            -> SlotResult
    purchase_attractor(db, gid, uid)            -> AttractorResult
    user_max_battle_slots(db, gid, uid)         -> int  (status='owned' cap)
    user_max_storage_slots(db, gid, uid)        -> int  (status='stored' cap)
    user_max_egg_storage(db, gid, uid)          -> int  (egg_storage row cap)
    user_max_nest_slots(db, gid, uid)           -> int  (nest cap)
    capture_destination(db, gid, uid)           -> 'battle' | 'storage' | None
    attractor_active(db, gid, uid)              -> bool
"""
from __future__ import annotations

import datetime as _dt
import logging
import time as _time
from dataclasses import dataclass
from typing import Any

from constants.ui import (
    C_TIER_BRONZE, C_TIER_DIAMOND, C_TIER_GOLD,
    C_TIER_PLATINUM, C_TIER_SILVER,
)
from core.framework.scale import to_human, to_raw

log = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────
# Buddy Network short code + token symbols.
BUD_NETWORK_SHORT: str = "bud"
BUD_SYMBOL:        str = "BUD"

# Buddy Battle Token. Earn-only on Buddy Network; minted by every wild
# / arena / PvP battle win across the bot. Listed in
# Config.BUD_SWAPPABLE_TOKENS so burn_for_bud / burn_bud_for handle it
# generically alongside FREN / REEL / RUNE / MOON / HRV / INGOT / GBC.
BBT_SYMBOL:        str = "BBT"
FREN_SYMBOL:       str = "FREN"

# FREN stake yield. 1 FREN -> 0.01 BUD per day at base, so a player
# with 10,000 FREN staked accrues ~100 BUD/day. Tuned in line with
# fishing's LURE_STAKE_REEL_PER_DAY (0.01) so the staking grind feels
# similar across earn-economy networks.
FREN_STAKE_BUD_PER_DAY: float = 0.01

# LP-reward fee bps split out of every BUD burn (mirrors fishing's
# GEAR_BURN_LP_REWARD_BPS / dungeon's ORE_BURN_LP_REWARD_BPS).
BUD_BURN_LP_REWARD_BPS:    int = 100
BUD_CASHOUT_LP_REWARD_BPS: int = 100

# Buddy Shop pricing. Flat BUD amounts so listed prices never drift
# under a player's feet as the BUD/USD oracle moves. The shop panel
# still annotates the USD equivalent off the live oracle so players
# see what each purchase is worth, but the gate is purely BUD-held.
#
# Three distinct purchasable capacities (per buddies_config):
#   * battle slot   -- expensive, hard-capped at +7 (3 base, 10 max)
#   * storage slot  -- mid-priced, +10 buddies per upgrade, +9 upgrades
#                      (10 base, 100 max)
#   * egg storage   -- cheap, +50 eggs per upgrade, +19 upgrades
#                      (50 base, 1000 max)
#
# Battle is rare and player-power-affecting so the price ladder is the
# steepest; storage is collection-flex; egg storage is largely a QoL
# overflow buffer for fishing/wild-battle drops.
BATTLE_SLOT_PRICE_BUD:   float = 25_000.0
STORAGE_SLOT_PRICE_BUD:  float =  5_000.0
EGG_STORAGE_PRICE_BUD:   float =  2_500.0
NEST_SLOT_PRICE_BUD:     float = 10_000.0

# Battle attractor (1-hour timed buff) -- doubles a user's escape-event
# roll rate while active.
ATTRACTOR_PRICE_BUD:   float = 250.0
ATTRACTOR_DURATION_S:  int   = 3600
ATTRACTOR_BUFF_MULT:   float = 2.0

# USD-named aliases preserved so the net_worth compute path keeps
# valuing every purchased upgrade. Each *_USD constant just mirrors
# the flat-BUD price; the USD readout in the shop UI is computed off
# the live BUD oracle on render.
BATTLE_SLOT_PRICE_USD:  float = BATTLE_SLOT_PRICE_BUD
STORAGE_SLOT_PRICE_USD: float = STORAGE_SLOT_PRICE_BUD
EGG_STORAGE_PRICE_USD:  float = EGG_STORAGE_PRICE_BUD
NEST_SLOT_PRICE_USD:    float = NEST_SLOT_PRICE_BUD
ATTRACTOR_PRICE_USD:    float = ATTRACTOR_PRICE_BUD


@dataclass
class StakeResult:
    """Snapshot returned by every stake / unstake / claim call.

    Both FREN and BBT positions sit on user_buddy_economy and accrue
    BUD yield at the same per-day rate, so the result dataclass carries
    both stakes back to the cog. The ``*_delta_raw`` fields are signed
    (+ on stake, - on unstake, 0 on claim).
    """
    fren_staked_raw: int
    fren_delta_raw: int
    bud_yield_paid_raw: int
    pending_bud_raw: int
    bbt_staked_raw: int = 0
    bbt_delta_raw: int = 0


@dataclass
class BurnResult:
    sym_in: str
    sym_out: str
    amount_in_raw: int
    amount_out_raw: int
    in_oracle_before: float
    in_oracle_after: float
    out_oracle_before: float
    out_oracle_after: float
    price_impact_pct: float
    lp_reward_usd: float = 0.0


@dataclass
class SwapQuote:
    """Read-only preview of a ``,buddy convert`` burn-swap.

    Mirrors what :func:`_generic_burn_swap` would compute, without mutating
    wallet balances or oracle prices. Powers the ``,buddy quote`` command
    so players can see the rate / synthetic-pool depth / impact / slippage
    before paying any tokens.
    """
    sym_in: str
    sym_out: str
    amount_in_raw: int
    amount_in_human: float
    amount_out_raw: int
    amount_out_human: float
    in_oracle: float
    out_oracle: float
    in_oracle_after: float
    out_oracle_after: float
    in_supply: float
    out_supply: float
    in_pool_usd: float
    out_pool_usd: float
    in_impact_pct: float
    out_impact_pct: float
    price_impact_pct: float
    spot_rate: float
    effective_rate: float
    slippage_pct: float
    usd_value: float
    lp_reward_usd: float


@dataclass
class CashoutResult:
    bud_burned_raw: int
    usd_credited_raw: int
    bud_oracle_before: float
    bud_oracle_after: float
    price_impact_pct: float
    revenue_usd: float = 0.0
    lp_reward_usd: float = 0.0


@dataclass
class SlotResult:
    new_slot_count: int
    bud_burned_raw: int
    bud_oracle_before: float
    bud_oracle_after: float
    price_impact_pct: float


@dataclass
class AttractorResult:
    expires_at: _dt.datetime
    bud_burned_raw: int
    bud_oracle_before: float
    bud_oracle_after: float
    price_impact_pct: float


# ── State helpers ───────────────────────────────────────────────────────────


async def ensure_state(db: Any, guild_id: int, user_id: int) -> dict:
    """Insert a default user_buddy_economy row on first touch and return it."""
    await db.execute(
        """
        INSERT INTO user_buddy_economy (guild_id, user_id)
        VALUES ($1, $2)
        ON CONFLICT (guild_id, user_id) DO NOTHING
        """,
        guild_id, user_id,
    )
    row = await db.fetch_one(
        "SELECT * FROM user_buddy_economy WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    return dict(row) if row else {}


async def list_state(db: Any, guild_id: int, user_id: int) -> dict:
    row = await db.fetch_one(
        "SELECT * FROM user_buddy_economy WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    return dict(row) if row else {}


def _accrue_pending(state: dict) -> int:
    """Return fresh BUD (raw) accrued since ``last_yield_at`` on staked FREN+BBT.

    Both tokens earn BUD at the same per-day rate so a player can split
    stake across FREN (the buddy-loop currency) and BBT (the cross-game
    battle token). Total staked = fren_staked_raw + bbt_staked_raw.
    """
    last_at = state.get("last_yield_at")
    if not last_at:
        return 0
    if isinstance(last_at, _dt.datetime):
        last_ts = last_at.timestamp()
    else:
        last_ts = float(last_at)
    elapsed = max(0, int(_time.time() - last_ts))
    if elapsed <= 0:
        return 0
    staked_raw = (
        int(state.get("fren_staked_raw") or 0)
        + int(state.get("bbt_staked_raw") or 0)
    )
    if staked_raw <= 0:
        return 0
    one = to_raw(1.0)
    rate_raw = to_raw(FREN_STAKE_BUD_PER_DAY)
    accrued = (staked_raw * rate_raw * elapsed) // (one * 86400)
    return int(accrued)


async def accrued_yield(db: Any, guild_id: int, user_id: int) -> int:
    state = await ensure_state(db, guild_id, user_id)
    pending = int(state.get("bud_yield_pending_raw") or 0)
    return pending + _accrue_pending(state)


async def user_max_battle_slots(db: Any, guild_id: int, user_id: int) -> int:
    """Effective cap on status='owned' (battle-active) buddies.

    Base from buddies_config.BATTLE_SLOTS_BASE, plus every battle-slot
    upgrade purchased through ``,buddy shop`` (one each, max 7 = 10
    total). Battle-slot buddies decay, can be promoted active, and
    are eligible for arena / wild battles.
    """
    from configs.buddies_config import BATTLE_SLOTS_BASE, BATTLE_SLOTS_MAX_PURCHASED
    state = await ensure_state(db, guild_id, user_id)
    extra = int(state.get("battle_slots_purchased") or 0)
    extra = min(int(BATTLE_SLOTS_MAX_PURCHASED), max(0, extra))
    return int(BATTLE_SLOTS_BASE) + extra


async def user_max_storage_slots(db: Any, guild_id: int, user_id: int) -> int:
    """Effective cap on status='stored' buddies.

    Base from buddies_config.STORAGE_SLOTS_BASE, plus every storage
    upgrade purchased (each adds STORAGE_SLOTS_PER_UPGRADE rows; max
    9 upgrades = 100 total). Stored buddies are frozen -- no decay,
    no fights, no is_active competition.
    """
    from configs.buddies_config import (
        STORAGE_SLOTS_BASE, STORAGE_SLOTS_MAX_PURCHASED,
        STORAGE_SLOTS_PER_UPGRADE,
    )
    state = await ensure_state(db, guild_id, user_id)
    extra_upgrades = int(state.get("storage_slots_purchased") or 0)
    extra_upgrades = min(int(STORAGE_SLOTS_MAX_PURCHASED), max(0, extra_upgrades))
    return int(STORAGE_SLOTS_BASE) + extra_upgrades * int(STORAGE_SLOTS_PER_UPGRADE)


async def user_max_nest_slots(db: Any, guild_id: int, user_id: int) -> int:
    """Effective cap on simultaneous active nest (incubation) rows.

    Base from buddies_config.NEST_SLOTS_BASE, plus every nest-slot
    upgrade purchased (one per upgrade, max 9 = 10 total). Each slot
    holds one active cc_buddy_daycare row.
    """
    from configs.buddies_config import NEST_SLOTS_BASE, NEST_SLOTS_MAX_PURCHASED
    state = await ensure_state(db, guild_id, user_id)
    extra = int(state.get("nest_slots_purchased") or 0)
    extra = min(int(NEST_SLOTS_MAX_PURCHASED), max(0, extra))
    return int(NEST_SLOTS_BASE) + extra


async def user_max_egg_storage(db: Any, guild_id: int, user_id: int) -> int:
    """Effective cap on rows in user_buddy_economy.egg_storage.

    Base from buddies_config.EGG_STORAGE_BASE, plus every egg-storage
    upgrade (each adds EGG_STORAGE_PER_UPGRADE rows; max 19 upgrades
    = 1000 total). Held eggs sit on user_fishing.held_eggs and are
    capped separately at EGG_HELD_HARD_CAP (10, not upgradable).
    """
    from configs.buddies_config import (
        EGG_STORAGE_BASE, EGG_STORAGE_MAX_PURCHASED,
        EGG_STORAGE_PER_UPGRADE,
    )
    state = await ensure_state(db, guild_id, user_id)
    extra_upgrades = int(state.get("egg_storage_slots_purchased") or 0)
    extra_upgrades = min(int(EGG_STORAGE_MAX_PURCHASED), max(0, extra_upgrades))
    return int(EGG_STORAGE_BASE) + extra_upgrades * int(EGG_STORAGE_PER_UPGRADE)


# Back-compat shim. Older capture sites called user_max_buddies() to
# decide whether the shelter had room. The semantic split into
# battle/storage routes happens via capture_destination(); leaving the
# old name pointed at the battle cap so any straggler that hasn't been
# migrated still gets the historical "owned cap" answer rather than
# blowing up at import time.
user_max_buddies = user_max_battle_slots


async def capture_destination(
    db: Any, guild_id: int, user_id: int,
) -> str | None:
    """Return where a fresh capture should land for this user.

    * 'battle'  -- player has an open battle slot (status='owned' rows
                   < user_max_battle_slots). Caller should INSERT with
                   status='owned' and is_active=FALSE.
    * 'storage' -- battle is full but storage has room
                   (status='stored' rows < user_max_storage_slots).
                   Caller should INSERT with status='stored' and
                   is_active=FALSE.
    * None      -- both surfaces full. Caller refuses the capture
                   (or, on fishing wild battles, the egg overflow path
                   takes over).

    Battle is always preferred so a player who has the room gets an
    immediately-usable buddy rather than one stuck in storage.
    """
    battle_cap = await user_max_battle_slots(db, guild_id, user_id)
    storage_cap = await user_max_storage_slots(db, guild_id, user_id)
    counts = await db.fetch_one(
        """
        SELECT
          COALESCE(SUM(CASE WHEN status = 'owned'  THEN 1 ELSE 0 END), 0)::int
            AS owned_count,
          COALESCE(SUM(CASE WHEN status = 'stored' THEN 1 ELSE 0 END), 0)::int
            AS stored_count
          FROM cc_buddies
         WHERE guild_id = $1 AND owner_user_id = $2
        """,
        int(guild_id), int(user_id),
    )
    owned = int((counts or {}).get("owned_count") or 0)
    stored = int((counts or {}).get("stored_count") or 0)
    if owned < int(battle_cap):
        return "battle"
    if stored < int(storage_cap):
        return "storage"
    return None


async def attractor_active(db: Any, guild_id: int, user_id: int) -> bool:
    """True if the user has an unexpired battle attractor."""
    row = await db.fetch_one(
        """
        SELECT 1 FROM user_buddy_economy
         WHERE guild_id = $1 AND user_id = $2
           AND attractor_until IS NOT NULL
           AND attractor_until > NOW()
        LIMIT 1
        """,
        guild_id, user_id,
    )
    return row is not None


# ── Wallet helpers ──────────────────────────────────────────────────────────


async def _wallet_raw(db: Any, gid: int, uid: int, sym: str) -> int:
    """Look up the user's raw balance for ``sym``.

    Every BUD partner lives in ``wallet_holdings`` keyed by network short
    -- migration 0235 brought the Gamba Network into the same shape as
    the rest of the earn-only networks, so a single dispatch resolves
    every supported burn-swap symbol.
    """
    from core.config import Config
    net_full = Config.TOKENS.get(sym, {}).get("network", "")
    from core.framework.network import normalize_short
    short = normalize_short(net_full)
    if not short:
        return 0
    row = await db.get_wallet_holding(uid, gid, short, sym)
    return int((row or {}).get("amount") or 0)


async def get_bud_wallet_raw(db: Any, gid: int, uid: int) -> int:
    return await _wallet_raw(db, gid, uid, BUD_SYMBOL)


async def get_fren_wallet_raw(db: Any, gid: int, uid: int) -> int:
    return await _wallet_raw(db, gid, uid, FREN_SYMBOL)


async def get_bbt_wallet_raw(db: Any, gid: int, uid: int) -> int:
    """Read the user's BBT (Buddy Battle Token) balance, raw scaled.
    BBT lives on Buddy Network, same as BUD/FREN.
    """
    return await _wallet_raw(db, gid, uid, BBT_SYMBOL)


async def mint_bbt_reward(
    db: Any, guild_id: int, user_id: int, amount_human: float,
    *, source: str = "battle",
) -> int:
    """Credit the user with freshly-minted BBT for a battle win.

    Pure mint: increments wallet_holdings + circulating_supply via
    update_wallet_holding (which writes both atomically). No oracle
    drop is applied -- BBT is the universal battle reward and minting
    on every win would push the oracle to zero. The price-discovery
    happens through ``burn_for_bud`` / ``cashout_bbt`` paths instead,
    same shape as fishing's REEL stake-yield mint.

    ``source`` is logged for analytics (e.g. 'fish_wild', 'delve_wild',
    'farm_wild', 'arena', 'buddy_pvp'). Returns raw amount credited.
    """
    if amount_human <= 0:
        return 0
    raw = to_raw(float(amount_human))
    if raw <= 0:
        return 0
    try:
        await db.update_wallet_holding(
            user_id, guild_id, BUD_NETWORK_SHORT, BBT_SYMBOL, int(raw),
        )
    except Exception:
        log.exception(
            "mint_bbt_reward failed uid=%s gid=%s src=%s amt=%.4f",
            user_id, guild_id, source, amount_human,
        )
        return 0
    return int(raw)


async def mint_bud_reward(
    db: Any, guild_id: int, user_id: int, amount_human: float,
    *, source: str = "battle",
) -> tuple[int, float, float]:
    """Credit the user with freshly-minted BUD for a battle win.

    Mirrors ``mint_bbt_reward`` but for BUD and ALSO applies the standard
    mint-impact oracle drop (BUD is price-discovered, BBT is universal
    reward-only). Returns ``(raw_credited, oracle_before, oracle_after)``
    so callers can show the oracle hit alongside the credit.

    ``source`` is logged for analytics (e.g. ``"zone_plains_gate"``,
    ``"zone_boss"``). Best-effort -- a wallet write that fails returns
    (0, 0, 0) and is logged but never raises.
    """
    if amount_human <= 0:
        return 0, 0.0, 0.0
    raw = to_raw(float(amount_human))
    if raw <= 0:
        return 0, 0.0, 0.0
    try:
        await db.update_wallet_holding(
            user_id, guild_id, BUD_NETWORK_SHORT, BUD_SYMBOL, int(raw),
        )
        oracle_before, oracle_after, _ = await _apply_mint_oracle_drop(
            db, int(guild_id), BUD_SYMBOL, float(amount_human),
        )
    except Exception:
        log.exception(
            "mint_bud_reward failed uid=%s gid=%s src=%s amt=%.4f",
            user_id, guild_id, source, amount_human,
        )
        return 0, 0.0, 0.0
    return int(raw), float(oracle_before), float(oracle_after)


async def cashout_bbt(
    db: Any, guild_id: int, user_id: int, bbt_amount_raw: int,
) -> CashoutResult:
    """Burn BBT, credit USD at oracle minus impact. Mirrors cashout_bud
    exactly -- same slippage formula, same LP-reward fan-out, same
    oracle-drop write -- just for BBT instead of BUD.
    """
    if bbt_amount_raw <= 0:
        raise ValueError("Amount must be positive.")
    held = await get_bbt_wallet_raw(db, guild_id, user_id)
    if held < int(bbt_amount_raw):
        raise ValueError(f"You only have {to_human(held):,.4f} BBT.")

    oracle_before = await _oracle_price(db, guild_id, BBT_SYMBOL)
    if oracle_before <= 0:
        raise ValueError("BBT oracle price is currently zero -- try again later.")

    bbt_human = to_human(int(bbt_amount_raw))
    revenue_usd = bbt_human * oracle_before
    supply = await _supply_human(db, guild_id, BBT_SYMBOL)
    impact = _price_impact(revenue_usd, oracle_before, supply)

    eff_price = oracle_before * (1.0 - impact / 2.0)
    usd_credit_human = bbt_human * eff_price
    usd_credit_raw = to_raw(usd_credit_human)

    await db.update_wallet_holding(
        user_id, guild_id, BUD_NETWORK_SHORT, BBT_SYMBOL, -int(bbt_amount_raw),
    )
    if usd_credit_raw > 0:
        try:
            await db.update_wallet(user_id, guild_id, int(usd_credit_raw))
        except Exception:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id, BUD_NETWORK_SHORT, BBT_SYMBOL,
                    int(bbt_amount_raw),
                )
            except Exception:
                log.exception(
                    "cashout_bbt: refund failed uid=%s gid=%s amt=%s",
                    user_id, guild_id, bbt_amount_raw,
                )
            raise

    oracle_after = max(1e-9, oracle_before * (1.0 - impact))
    try:
        await db.update_price(BBT_SYMBOL, guild_id, oracle_after)
    except Exception:
        log.exception("cashout_bbt: oracle update failed gid=%s", guild_id)
    await _write_burn_candle(
        db, guild_id, BBT_SYMBOL, oracle_before, oracle_after, revenue_usd,
    )
    fee_usd = revenue_usd * (int(BUD_CASHOUT_LP_REWARD_BPS) / 10_000.0)
    if fee_usd > 0:
        await _distribute_burn_lp_reward(db, guild_id, BBT_SYMBOL, fee_usd)
    return CashoutResult(
        bud_burned_raw=int(bbt_amount_raw),
        usd_credited_raw=int(usd_credit_raw),
        bud_oracle_before=float(oracle_before),
        bud_oracle_after=float(oracle_after),
        impact_pct=float(impact),
    )


# ── Oracle / burn-impact helpers (ports of services/dungeon.py's pattern) ──


def _price_impact_max() -> float:
    from core.config import Config
    return float(getattr(Config, "PRICE_IMPACT_MAX", 0.40))


async def _oracle_price(db: Any, guild_id: int, symbol: str) -> float:
    row = await db.fetch_one(
        "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), symbol.upper(),
    )
    if row and row.get("price") is not None:
        return float(row["price"])
    from core.config import Config
    return float(Config.TOKENS.get(symbol.upper(), {}).get("start_price", 1.0))


def _minute_ts() -> int:
    return int(_time.time()) // 60 * 60


async def _supply_human(db: Any, guild_id: int, symbol: str) -> float:
    row = await db.fetch_one(
        "SELECT circulating_supply FROM crypto_prices "
        "WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), symbol,
    )
    return to_human(int((row or {}).get("circulating_supply") or 0))


def _price_impact(usd_value: float, oracle: float, supply_human: float) -> float:
    """Same impact formula .buy / .sell / fishing / dungeon use."""
    from core.config import Config
    impact = usd_value / float(Config.PRICE_IMPACT_DIVISOR)
    market_cap = max(0.0, oracle * supply_human)
    if market_cap > 0 and usd_value > 0.001 * market_cap:
        mc_ratio = usd_value / market_cap
        impact *= min(1.0 + mc_ratio * 2.0, 5.0)
    return min(impact, _price_impact_max())


async def _write_burn_candle(
    db: Any, guild_id: int, symbol: str,
    oracle_before: float, oracle_after: float, volume_usd: float,
) -> None:
    try:
        await db.upsert_candle(
            int(guild_id), f"{symbol.upper()}USD", _minute_ts(),
            open_=float(oracle_before),
            high=max(float(oracle_before), float(oracle_after)),
            low=min(float(oracle_before), float(oracle_after)),
            close=float(oracle_after),
            volume_delta=float(max(0.0, volume_usd)),
        )
    except Exception:
        log.exception("buddy economy candle write failed gid=%s sym=%s", guild_id, symbol)


async def _distribute_burn_lp_reward(
    db: Any, guild_id: int, symbol: str, fee_usd: float,
) -> float:
    """Pay a USD slice to LP holders of any pool containing ``symbol``.
    Mirrors services.fishing._distribute_burn_lp_reward exactly.
    """
    if fee_usd <= 0:
        return 0.0
    sym = symbol.upper()
    rows = await db.fetch_all(
        """
        SELECT lp.user_id, lp.pool_id, lp.lp_shares, p.total_lp
          FROM lp_positions lp
          JOIN pools p
            ON p.pool_id = lp.pool_id
           AND p.guild_id = lp.guild_id
         WHERE lp.guild_id = $1
           AND lp.lp_shares > 0
           AND COALESCE(p.vault_locked, FALSE) = FALSE
           AND (p.token_a = $2 OR p.token_b = $2)
        """,
        int(guild_id), sym,
    )
    if not rows:
        return 0.0
    weights: list[tuple[int, float]] = []
    total_weight = 0.0
    for r in rows:
        total_lp = int(r.get("total_lp") or 0)
        shares = int(r.get("lp_shares") or 0)
        if total_lp <= 0 or shares <= 0:
            continue
        w = shares / total_lp
        weights.append((int(r["user_id"]), w))
        total_weight += w
    if total_weight <= 0:
        return 0.0
    paid = 0.0
    for uid, w in weights:
        payout_usd = fee_usd * (w / total_weight)
        payout_raw = to_raw(payout_usd)
        if payout_raw <= 0:
            continue
        try:
            async with db.atomic():
                await db.update_wallet(uid, int(guild_id), int(payout_raw))
                await db.log_tx(
                    int(guild_id), uid, "LP_BURN_REWARD",
                    symbol_in=sym,
                    symbol_out="USD", amount_out=int(payout_raw),
                    network="usd",
                )
            paid += payout_usd
        except Exception:
            log.exception(
                "buddy economy LP reward credit failed gid=%s uid=%s sym=%s usd=%.6f",
                guild_id, uid, sym, payout_usd,
            )
    return paid


async def _apply_mint_oracle_drop(
    db: Any, guild_id: int, symbol: str, mint_amount_human: float,
) -> tuple[float, float, float]:
    """Drop ``symbol`` oracle by the standard impact for a mint of size N.

    Returns (oracle_before, oracle_after, impact_pct). Best-effort: a
    chart-write hiccup never aborts the upstream credit.
    """
    if mint_amount_human <= 0:
        return 0.0, 0.0, 0.0
    try:
        oracle_before = await _oracle_price(db, guild_id, symbol)
        if oracle_before <= 0:
            return 0.0, 0.0, 0.0
        usd_value = float(mint_amount_human) * oracle_before
        supply = await _supply_human(db, guild_id, symbol)
        impact = _price_impact(usd_value, oracle_before, supply)
        oracle_after = max(1e-9, oracle_before * (1.0 - impact))
        await db.update_price(symbol, guild_id, oracle_after)
        await _write_burn_candle(db, guild_id, symbol, oracle_before, oracle_after, usd_value)
        return float(oracle_before), float(oracle_after), float(impact)
    except Exception:
        log.exception("buddy economy mint impact failed gid=%s sym=%s", guild_id, symbol)
        return 0.0, 0.0, 0.0


async def _apply_burn_oracle_drop(
    db: Any, guild_id: int, symbol: str, burn_amount_human: float, lp_reward_bps: int,
) -> tuple[float, float, float, float]:
    """Drop ``symbol`` oracle on a burn (negative supply delta) and pay LP.

    Returns (oracle_before, oracle_after, impact_pct, lp_paid_usd).
    """
    if burn_amount_human <= 0:
        return 0.0, 0.0, 0.0, 0.0
    try:
        oracle_before = await _oracle_price(db, guild_id, symbol)
        if oracle_before <= 0:
            return 0.0, 0.0, 0.0, 0.0
        usd_value = float(burn_amount_human) * oracle_before
        supply = await _supply_human(db, guild_id, symbol)
        impact = _price_impact(usd_value, oracle_before, supply)
        # Burns are deflationary -- supply just contracted, so the oracle
        # rises in proportion. Mirrors fishing's burn behavior.
        oracle_after = max(1e-9, oracle_before * (1.0 - impact))
        # NOTE: a sell-side burn (token leaves circulation via update_wallet
        # with a negative delta) presses the oracle down because the burn
        # is treated as "supply walked off the chain into the void", which
        # the AMM reads as more of the token chasing the same buyers --
        # same direction the .sell oracle move uses. The sign convention
        # matches services/fishing.cashout_reel and services/dungeon.cashout_rune.
        await db.update_price(symbol, guild_id, oracle_after)
        await _write_burn_candle(db, guild_id, symbol, oracle_before, oracle_after, usd_value)
        fee_usd = usd_value * (int(lp_reward_bps) / 10_000.0)
        lp_paid = 0.0
        if fee_usd > 0:
            lp_paid = await _distribute_burn_lp_reward(db, guild_id, symbol, fee_usd)
        return float(oracle_before), float(oracle_after), float(impact), float(lp_paid)
    except Exception:
        log.exception("buddy economy burn impact failed gid=%s sym=%s", guild_id, symbol)
        return 0.0, 0.0, 0.0, 0.0


# ── FREN staking -> BUD yield ──────────────────────────────────────────────


_STAKE_SYMBOLS: tuple[str, ...] = ("FREN", "BBT")


def _stake_column(symbol: str) -> str:
    sym = (symbol or "").upper()
    if sym == "FREN":
        return "fren_staked_raw"
    if sym == "BBT":
        return "bbt_staked_raw"
    raise ValueError(f"{symbol!r} can't be staked on the Buddy Network. Accepted: FREN, BBT.")


async def stake_token(
    db: Any, guild_id: int, user_id: int,
    *, symbol: str, amount_raw: int,
) -> StakeResult:
    """Move ``symbol`` (FREN or BBT) from wallet -> stake.

    Both stakes share the same BUD-per-day rate; combined stake powers
    the yield clock. Crystallises any pending BUD yield first so the
    new balance starts a fresh accrual period.
    """
    sym = (symbol or "").upper()
    column = _stake_column(sym)
    if amount_raw <= 0:
        raise ValueError("Amount must be positive.")
    state = await ensure_state(db, guild_id, user_id)
    pending = int(state.get("bud_yield_pending_raw") or 0)
    fresh = _accrue_pending(state)
    new_pending = pending + fresh

    await db.update_wallet_holding(
        user_id, guild_id, BUD_NETWORK_SHORT, sym, -int(amount_raw),
    )
    cur_staked = int(state.get(column) or 0)
    new_staked = cur_staked + int(amount_raw)
    await db.execute(
        f"""
        UPDATE user_buddy_economy
           SET {column} = $3::numeric,
               bud_yield_pending_raw = $4::numeric,
               last_yield_at = NOW(),
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_staked), int(new_pending),
    )
    new_state = await ensure_state(db, guild_id, user_id)
    return StakeResult(
        fren_staked_raw=int(new_state.get("fren_staked_raw") or 0),
        fren_delta_raw=int(amount_raw) if sym == "FREN" else 0,
        bud_yield_paid_raw=0,
        pending_bud_raw=int(new_pending),
        bbt_staked_raw=int(new_state.get("bbt_staked_raw") or 0),
        bbt_delta_raw=int(amount_raw) if sym == "BBT" else 0,
    )


async def stake_fren(
    db: Any, guild_id: int, user_id: int, fren_amount_raw: int,
) -> StakeResult:
    """Back-compat wrapper: stake FREN on the Buddy Network."""
    return await stake_token(
        db, guild_id, user_id, symbol="FREN", amount_raw=int(fren_amount_raw),
    )


async def stake_bbt(
    db: Any, guild_id: int, user_id: int, bbt_amount_raw: int,
) -> StakeResult:
    """Stake BBT on the Buddy Network. Same per-day rate as FREN."""
    return await stake_token(
        db, guild_id, user_id, symbol="BBT", amount_raw=int(bbt_amount_raw),
    )


async def claim_yield(
    db: Any, guild_id: int, user_id: int,
) -> StakeResult:
    """Pay accrued BUD yield to wallet. Stake stays locked; clock resets."""
    state = await ensure_state(db, guild_id, user_id)
    pending = int(state.get("bud_yield_pending_raw") or 0)
    fresh = _accrue_pending(state)
    payout = pending + fresh
    if payout <= 0:
        raise ValueError("No BUD has accrued yet. Try again after some time has passed.")
    await db.update_wallet_holding(
        user_id, guild_id, BUD_NETWORK_SHORT, BUD_SYMBOL, int(payout),
    )
    await _apply_mint_oracle_drop(db, guild_id, BUD_SYMBOL, to_human(int(payout)))
    await db.execute(
        """
        UPDATE user_buddy_economy
           SET bud_yield_pending_raw = 0,
               last_yield_at = NOW(),
               total_bud_earned_raw = total_bud_earned_raw + $3::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(payout),
    )
    return StakeResult(
        fren_staked_raw=int(state.get("fren_staked_raw") or 0),
        fren_delta_raw=0,
        bud_yield_paid_raw=int(payout),
        pending_bud_raw=0,
        bbt_staked_raw=int(state.get("bbt_staked_raw") or 0),
        bbt_delta_raw=0,
    )


async def unstake_token(
    db: Any, guild_id: int, user_id: int,
    *, symbol: str, amount_raw: int,
) -> StakeResult:
    """Move ``symbol`` (FREN or BBT) from stake -> wallet.

    Pays accrued BUD on the same call so the player always gets their
    yield when they unstake. The BUD payout is computed against the
    full FREN+BBT stake, then the chosen symbol is unstaked at the end.
    """
    sym = (symbol or "").upper()
    column = _stake_column(sym)
    state = await ensure_state(db, guild_id, user_id)
    cur_staked = int(state.get(column) or 0)
    pending = int(state.get("bud_yield_pending_raw") or 0)
    fresh = _accrue_pending(state)
    payout = pending + fresh
    requested = max(0, int(amount_raw))
    if cur_staked <= 0 or requested <= 0:
        raise ValueError(f"You have no {sym} staked.")
    actual = min(requested, cur_staked)
    new_staked = cur_staked - actual

    await db.update_wallet_holding(
        user_id, guild_id, BUD_NETWORK_SHORT, sym, int(actual),
    )
    await _apply_mint_oracle_drop(db, guild_id, sym, to_human(int(actual)))
    if payout > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id, BUD_NETWORK_SHORT, BUD_SYMBOL, int(payout),
            )
            await _apply_mint_oracle_drop(db, guild_id, BUD_SYMBOL, to_human(int(payout)))
        except Exception:
            log.exception(
                "unstake_token: BUD payout failed uid=%s gid=%s sym=%s",
                user_id, guild_id, sym,
            )
            payout = 0
    await db.execute(
        f"""
        UPDATE user_buddy_economy
           SET {column} = $3::numeric,
               bud_yield_pending_raw = 0,
               last_yield_at = NOW(),
               total_bud_earned_raw = total_bud_earned_raw + $4::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_staked), int(payout),
    )
    new_state = await ensure_state(db, guild_id, user_id)
    return StakeResult(
        fren_staked_raw=int(new_state.get("fren_staked_raw") or 0),
        fren_delta_raw=-int(actual) if sym == "FREN" else 0,
        bud_yield_paid_raw=int(payout),
        pending_bud_raw=0,
        bbt_staked_raw=int(new_state.get("bbt_staked_raw") or 0),
        bbt_delta_raw=-int(actual) if sym == "BBT" else 0,
    )


async def unstake_fren(
    db: Any, guild_id: int, user_id: int, fren_amount_raw: int,
) -> StakeResult:
    """Back-compat wrapper: unstake FREN on the Buddy Network."""
    return await unstake_token(
        db, guild_id, user_id, symbol="FREN", amount_raw=int(fren_amount_raw),
    )


async def unstake_bbt(
    db: Any, guild_id: int, user_id: int, bbt_amount_raw: int,
) -> StakeResult:
    """Unstake BBT on the Buddy Network."""
    return await unstake_token(
        db, guild_id, user_id, symbol="BBT", amount_raw=int(bbt_amount_raw),
    )


# ── Burn-swaps: BUD ↔ {FREN, REEL, RUNE, MOON} ─────────────────────────────


_PARTNER_NETWORK_BY_SYM: dict[str, str] = {
    "FREN": "bud",
    "REEL": "lur",
    "RUNE": "cry",
    "MOON": "moon",
    "HRV":  "har",
    "BBT":  "bud",
    # INGOT lives on Forge Network -- short code matches
    # crafting_config.FORGE_NETWORK_SHORT. Now bidirectional with BUD.
    "INGOT": "fge",
    # Gamba Network tokens. All nine (GBC + the eight game tokens) live
    # in wallet_holdings on the "gam" network short, same as every other
    # partner -- migration 0235_gamba_to_wallet_holdings moved them out
    # of crypto_holdings so ,wallet list surfaces every gamba balance.
    "GBC":    "gam",
    "GAMBIT": "gam",
    "CROWN":  "gam",
    "VEIN":   "gam",
    "PIP":    "gam",
    "EDGE":   "gam",
    "ACE":    "gam",
    "NOIR":   "gam",
    "CHERRY": "gam",
    # Sage Network coin -- short matches Config.SAGE_NETWORK_SHORT.
    # Bidirectional with BUD via the same earn-only firewall pattern as
    # INGOT / GBC. EDU is intentionally excluded; see core/config.py comment.
    "SAGE":   "sag",
}


def _partner_net_short(sym: str) -> str:
    return _PARTNER_NETWORK_BY_SYM.get(sym.upper(), "")


async def quote_burn_swap(
    db: Any, guild_id: int, user_id: int | None,
    sym_in: str, sym_out: str, amount_in_raw: int,
) -> SwapQuote:
    """Read-only preview of a BUD <-> partner burn-swap.

    Computes the same numbers :func:`_generic_burn_swap` uses to settle a
    real swap (oracle prices, supplies, per-side impact, effective output,
    LP fee), but does not touch wallets or oracles. Used by the
    ``,buddy quote`` command.
    """
    if amount_in_raw <= 0:
        raise ValueError("Amount must be positive.")
    sym_in, sym_out = sym_in.upper(), sym_out.upper()
    from core.config import Config as _Cfg
    legal_out = frozenset(_Cfg.BUD_SWAPPABLE_TOKENS) | {"BUD"}
    legal_in  = legal_out | frozenset(_Cfg.BUD_ONEWAY_IN_TOKENS)
    if sym_in not in legal_in:
        raise ValueError(f"Not a Buddy-network burn-swap input: {sym_in}")
    if sym_out not in legal_out:
        raise ValueError(f"Not a Buddy-network burn-swap output: {sym_out}")
    if sym_in == sym_out:
        raise ValueError("Cannot swap a token for itself.")
    if "BUD" not in (sym_in, sym_out):
        raise ValueError("Buddy swaps must touch BUD on at least one side.")

    in_oracle  = await _oracle_price(db, guild_id, sym_in)
    out_oracle = await _oracle_price(db, guild_id, sym_out)
    if in_oracle <= 0 or out_oracle <= 0:
        raise ValueError("Oracle price is currently zero -- try again in a moment.")

    in_human = to_human(int(amount_in_raw))
    usd_value = in_human * in_oracle

    in_supply  = await _supply_human(db, guild_id, sym_in)
    out_supply = await _supply_human(db, guild_id, sym_out)
    in_impact  = _price_impact(usd_value, in_oracle,  in_supply)
    out_impact = _price_impact(usd_value, out_oracle, out_supply)

    eff_out_price = out_oracle * (1.0 + out_impact / 2.0)
    out_human = usd_value / max(1e-12, eff_out_price)
    out_raw = to_raw(out_human)

    in_oracle_after  = max(1e-9, in_oracle  * (1.0 - in_impact))
    out_oracle_after = max(1e-9, out_oracle * (1.0 + out_impact))

    spot_rate = in_oracle / out_oracle if out_oracle > 0 else 0.0
    effective_rate = (out_human / in_human) if in_human > 0 else 0.0
    slippage_pct = (
        max(0.0, (spot_rate - effective_rate) / spot_rate)
        if spot_rate > 0 else 0.0
    )

    fee_usd = usd_value * (int(BUD_BURN_LP_REWARD_BPS) / 10_000.0)

    return SwapQuote(
        sym_in=sym_in, sym_out=sym_out,
        amount_in_raw=int(amount_in_raw),
        amount_in_human=float(in_human),
        amount_out_raw=int(out_raw),
        amount_out_human=float(out_human),
        in_oracle=float(in_oracle), out_oracle=float(out_oracle),
        in_oracle_after=float(in_oracle_after),
        out_oracle_after=float(out_oracle_after),
        in_supply=float(in_supply), out_supply=float(out_supply),
        in_pool_usd=float(in_oracle * in_supply),
        out_pool_usd=float(out_oracle * out_supply),
        in_impact_pct=float(in_impact),
        out_impact_pct=float(out_impact),
        price_impact_pct=float(max(in_impact, out_impact)),
        spot_rate=float(spot_rate),
        effective_rate=float(effective_rate),
        slippage_pct=float(slippage_pct),
        usd_value=float(usd_value),
        lp_reward_usd=float(fee_usd),
    )


async def _generic_burn_swap(
    db: Any, guild_id: int, user_id: int,
    sym_in: str, sym_out: str, amount_in_raw: int,
) -> BurnResult:
    """Burn ``sym_in`` and mint ``sym_out`` at preserved USD value.

    Used by both directions of the BUD <-> partner swaps. The user-facing
    helpers wrap this with the right symbol pair so the swap surface
    stays clean. Slippage moves both oracles by the standard impact;
    LP-reward fee fans out across both sides' pools.
    """
    if amount_in_raw <= 0:
        raise ValueError("Amount must be positive.")
    sym_in, sym_out = sym_in.upper(), sym_out.upper()
    from core.config import Config as _Cfg
    legal_out = frozenset(_Cfg.BUD_SWAPPABLE_TOKENS) | {"BUD"}
    legal_in  = legal_out | frozenset(_Cfg.BUD_ONEWAY_IN_TOKENS)
    if sym_in not in legal_in:
        raise ValueError(f"Not a Buddy-network burn-swap input: {sym_in}")
    if sym_out not in legal_out:
        # Future earn-only tokens added to BUD_ONEWAY_IN_TOKENS would be
        # burnable INTO BUD only -- guarded here so a future carve-out
        # slots in without re-engineering the swap surface.
        raise ValueError(f"Not a Buddy-network burn-swap output: {sym_out}")
    if sym_in == sym_out:
        raise ValueError("Cannot swap a token for itself.")

    in_net  = _partner_net_short(sym_in)  or BUD_NETWORK_SHORT if sym_in  in ("BUD",) else _partner_net_short(sym_in)
    out_net = _partner_net_short(sym_out) or BUD_NETWORK_SHORT if sym_out in ("BUD",) else _partner_net_short(sym_out)
    if not in_net or not out_net:
        raise ValueError(f"Unknown wallet network for {sym_in} / {sym_out}.")

    held = await _wallet_raw(db, guild_id, user_id, sym_in)
    if held < int(amount_in_raw):
        raise ValueError(f"You only have {to_human(held):,.4f} {sym_in}.")

    in_oracle  = await _oracle_price(db, guild_id, sym_in)
    out_oracle = await _oracle_price(db, guild_id, sym_out)
    if in_oracle <= 0 or out_oracle <= 0:
        raise ValueError("Oracle price is currently zero -- try again in a moment.")

    in_human = to_human(int(amount_in_raw))
    usd_value = in_human * in_oracle

    in_supply  = await _supply_human(db, guild_id, sym_in)
    out_supply = await _supply_human(db, guild_id, sym_out)
    in_impact  = _price_impact(usd_value, in_oracle,  in_supply)
    out_impact = _price_impact(usd_value, out_oracle, out_supply)

    eff_out_price = out_oracle * (1.0 + out_impact / 2.0)
    out_human = usd_value / max(1e-12, eff_out_price)
    out_raw = to_raw(out_human)
    if out_raw <= 0:
        raise ValueError(f"Burn produces zero {sym_out} -- raise the amount.")

    # Wallet writes ride wallet_holdings keyed by network short -- every
    # partner (BUD, FREN, REEL, RUNE, MOON, HRV, INGOT, BBT, GBC + the
    # eight Gamba game tokens) now lives in the DeFi wallet. update_wallet_holding
    # also keeps crypto_prices.circulating_supply in sync.
    async def _debit(sym: str, net_short: str, amt: int) -> None:
        await db.update_wallet_holding(user_id, guild_id, net_short, sym, -int(amt))

    async def _credit(sym: str, net_short: str, amt: int) -> None:
        await db.update_wallet_holding(user_id, guild_id, net_short, sym, int(amt))

    await _debit(sym_in, in_net, int(amount_in_raw))
    try:
        await _credit(sym_out, out_net, int(out_raw))
    except Exception:
        try:
            await _credit(sym_in, in_net, int(amount_in_raw))
        except Exception:
            log.exception("buddy burn-swap: refund failed uid=%s gid=%s pair=%s/%s",
                          user_id, guild_id, sym_in, sym_out)
        raise

    in_oracle_after  = max(1e-9, in_oracle  * (1.0 - in_impact))
    out_oracle_after = max(1e-9, out_oracle * (1.0 + out_impact))
    try:
        await db.update_price(sym_in,  guild_id, in_oracle_after)
        await db.update_price(sym_out, guild_id, out_oracle_after)
    except Exception:
        log.exception("buddy burn-swap: oracle update failed gid=%s pair=%s/%s",
                      guild_id, sym_in, sym_out)
    await _write_burn_candle(db, guild_id, sym_in,  in_oracle,  in_oracle_after,  usd_value)
    await _write_burn_candle(db, guild_id, sym_out, out_oracle, out_oracle_after, usd_value)

    fee_usd = usd_value * (int(BUD_BURN_LP_REWARD_BPS) / 10_000.0)
    lp_paid = 0.0
    if fee_usd > 0:
        lp_paid += await _distribute_burn_lp_reward(db, guild_id, sym_in,  fee_usd / 2.0)
        lp_paid += await _distribute_burn_lp_reward(db, guild_id, sym_out, fee_usd / 2.0)

    if sym_out == BUD_SYMBOL:
        await db.execute(
            """
            UPDATE user_buddy_economy
               SET total_bud_earned_raw = total_bud_earned_raw + $3::numeric,
                   updated_at = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id, user_id, int(out_raw),
        )
    if sym_in == BUD_SYMBOL:
        await db.execute(
            """
            UPDATE user_buddy_economy
               SET total_bud_burned_raw = total_bud_burned_raw + $3::numeric,
                   updated_at = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id, user_id, int(amount_in_raw),
        )

    return BurnResult(
        sym_in=sym_in, sym_out=sym_out,
        amount_in_raw=int(amount_in_raw),
        amount_out_raw=int(out_raw),
        in_oracle_before=float(in_oracle),
        in_oracle_after=float(in_oracle_after),
        out_oracle_before=float(out_oracle),
        out_oracle_after=float(out_oracle_after),
        price_impact_pct=float(max(in_impact, out_impact)),
        lp_reward_usd=float(lp_paid),
    )


async def burn_for_bud(
    db: Any, guild_id: int, user_id: int, sym_in: str, amount_in_raw: int,
) -> BurnResult:
    """Burn any partner in ``Config.BUD_SWAPPABLE_TOKENS`` for fresh BUD."""
    return await _generic_burn_swap(db, guild_id, user_id, sym_in, BUD_SYMBOL, amount_in_raw)


async def burn_bud_for(
    db: Any, guild_id: int, user_id: int, sym_out: str, bud_amount_raw: int,
) -> BurnResult:
    """Burn BUD for any partner in ``Config.BUD_SWAPPABLE_TOKENS``."""
    return await _generic_burn_swap(db, guild_id, user_id, BUD_SYMBOL, sym_out, bud_amount_raw)


async def cashout_bud(
    db: Any, guild_id: int, user_id: int, bud_amount_raw: int,
) -> CashoutResult:
    """Burn BUD, credit USD at oracle minus impact. Mirrors cashout_reel / cashout_rune."""
    if bud_amount_raw <= 0:
        raise ValueError("Amount must be positive.")
    held = await get_bud_wallet_raw(db, guild_id, user_id)
    if held < int(bud_amount_raw):
        raise ValueError(f"You only have {to_human(held):,.4f} BUD.")

    oracle_before = await _oracle_price(db, guild_id, BUD_SYMBOL)
    if oracle_before <= 0:
        raise ValueError("BUD oracle price is currently zero -- try again later.")

    bud_human = to_human(int(bud_amount_raw))
    revenue_usd = bud_human * oracle_before
    supply = await _supply_human(db, guild_id, BUD_SYMBOL)
    impact = _price_impact(revenue_usd, oracle_before, supply)

    eff_price = oracle_before * (1.0 - impact / 2.0)
    usd_credit_human = bud_human * eff_price
    usd_credit_raw = to_raw(usd_credit_human)

    await db.update_wallet_holding(
        user_id, guild_id, BUD_NETWORK_SHORT, BUD_SYMBOL, -int(bud_amount_raw),
    )
    if usd_credit_raw > 0:
        try:
            await db.update_wallet(user_id, guild_id, int(usd_credit_raw))
        except Exception:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id, BUD_NETWORK_SHORT, BUD_SYMBOL,
                    int(bud_amount_raw),
                )
            except Exception:
                log.exception(
                    "cashout_bud: refund failed uid=%s gid=%s amt=%s",
                    user_id, guild_id, bud_amount_raw,
                )
            raise

    oracle_after = max(1e-9, oracle_before * (1.0 - impact))
    try:
        await db.update_price(BUD_SYMBOL, guild_id, oracle_after)
    except Exception:
        log.exception("cashout_bud: oracle update failed gid=%s -- chart will lag", guild_id)
    await _write_burn_candle(db, guild_id, BUD_SYMBOL, oracle_before, oracle_after, revenue_usd)
    fee_usd = revenue_usd * (int(BUD_CASHOUT_LP_REWARD_BPS) / 10_000.0)
    lp_paid = 0.0
    if fee_usd > 0:
        lp_paid = await _distribute_burn_lp_reward(db, guild_id, BUD_SYMBOL, fee_usd)
    await db.execute(
        """
        UPDATE user_buddy_economy
           SET total_bud_burned_raw = total_bud_burned_raw + $3::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(bud_amount_raw),
    )
    return CashoutResult(
        bud_burned_raw=int(bud_amount_raw),
        usd_credited_raw=int(usd_credit_raw),
        bud_oracle_before=float(oracle_before),
        bud_oracle_after=float(oracle_after),
        price_impact_pct=float(impact),
        revenue_usd=float(revenue_usd),
        lp_reward_usd=float(lp_paid),
    )


# ── Buddy Shop: slot purchase + battle-attractor ───────────────────────────


async def _bud_cost_for_usd(db: Any, guild_id: int, usd_amount: float) -> int:
    """How many raw BUD does ``usd_amount`` cost at the live oracle?

    Uses the live oracle so the BUD denomination of every shop item
    moves with the chart (exactly what the user asked for: shop purchases
    feel slippery the same way fishing/dungeon swaps do).
    """
    oracle = await _oracle_price(db, guild_id, BUD_SYMBOL)
    if oracle <= 0:
        raise ValueError("BUD oracle price is currently zero -- try again later.")
    return to_raw(usd_amount / oracle)


async def _purchase_capacity_upgrade(
    db: Any, guild_id: int, user_id: int,
    *, column: str, max_purchased: int, price_bud: float, label: str,
) -> SlotResult:
    """Shared BUD-burn upgrade path for battle / storage / egg-storage slots.

    Each of the three purchasable buddy capacities ticks a single
    integer column on user_buddy_economy. Same per-purchase guard
    (cap not yet reached + BUD wallet covers the flat price), same
    LP-reward fan-out, same SlotResult receipt.
    """
    state = await ensure_state(db, guild_id, user_id)
    cur = int(state.get(column) or 0)
    if cur >= int(max_purchased):
        raise ValueError(
            f"{label} upgrade cap reached "
            f"({int(max_purchased)} purchased -- already at maximum)."
        )
    cost_raw = to_raw(float(price_bud))
    held = await get_bud_wallet_raw(db, guild_id, user_id)
    if held < cost_raw:
        raise ValueError(
            f"Not enough BUD: need {to_human(cost_raw):,.4f}, "
            f"have {to_human(held):,.4f}."
        )

    await db.update_wallet_holding(
        user_id, guild_id, BUD_NETWORK_SHORT, BUD_SYMBOL, -int(cost_raw),
    )
    cost_human = to_human(int(cost_raw))
    ob, oa, impact, _lp = await _apply_burn_oracle_drop(
        db, guild_id, BUD_SYMBOL, cost_human, BUD_BURN_LP_REWARD_BPS,
    )
    await db.execute(
        f"""
        UPDATE user_buddy_economy
           SET {column} = {column} + 1,
               total_bud_burned_raw = total_bud_burned_raw + $3::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(cost_raw),
    )
    return SlotResult(
        new_slot_count=cur + 1,
        bud_burned_raw=int(cost_raw),
        bud_oracle_before=float(ob),
        bud_oracle_after=float(oa),
        price_impact_pct=float(impact),
    )


async def purchase_battle_slot(
    db: Any, guild_id: int, user_id: int,
) -> SlotResult:
    """Burn BUD for one extra status='owned' (battle) slot.

    Capped at BATTLE_SLOTS_MAX_PURCHASED upgrades (10 effective max
    once the 3-slot base is included).
    """
    from configs.buddies_config import BATTLE_SLOTS_MAX_PURCHASED
    return await _purchase_capacity_upgrade(
        db, guild_id, user_id,
        column="battle_slots_purchased",
        max_purchased=int(BATTLE_SLOTS_MAX_PURCHASED),
        price_bud=float(BATTLE_SLOT_PRICE_BUD),
        label="Battle slot",
    )


async def purchase_storage_slot(
    db: Any, guild_id: int, user_id: int,
) -> SlotResult:
    """Burn BUD for one storage upgrade (+10 storage rows per upgrade).

    Capped at STORAGE_SLOTS_MAX_PURCHASED upgrades (100 effective max
    once the 10-row base is included).
    """
    from configs.buddies_config import STORAGE_SLOTS_MAX_PURCHASED
    return await _purchase_capacity_upgrade(
        db, guild_id, user_id,
        column="storage_slots_purchased",
        max_purchased=int(STORAGE_SLOTS_MAX_PURCHASED),
        price_bud=float(STORAGE_SLOT_PRICE_BUD),
        label="Storage slot",
    )


async def purchase_egg_storage(
    db: Any, guild_id: int, user_id: int,
) -> SlotResult:
    """Burn BUD for one egg-storage upgrade (+50 egg rows per upgrade).

    Capped at EGG_STORAGE_MAX_PURCHASED upgrades (1000 effective max
    once the 50-row base is included).
    """
    from configs.buddies_config import EGG_STORAGE_MAX_PURCHASED
    return await _purchase_capacity_upgrade(
        db, guild_id, user_id,
        column="egg_storage_slots_purchased",
        max_purchased=int(EGG_STORAGE_MAX_PURCHASED),
        price_bud=float(EGG_STORAGE_PRICE_BUD),
        label="Egg storage",
    )


async def purchase_nest_slot(
    db: Any, guild_id: int, user_id: int,
) -> SlotResult:
    """Burn BUD for one extra nest (incubation) slot.

    Capped at NEST_SLOTS_MAX_PURCHASED upgrades (10 effective max once
    the 1-slot base is included).
    """
    from configs.buddies_config import NEST_SLOTS_MAX_PURCHASED
    return await _purchase_capacity_upgrade(
        db, guild_id, user_id,
        column="nest_slots_purchased",
        max_purchased=int(NEST_SLOTS_MAX_PURCHASED),
        price_bud=float(NEST_SLOT_PRICE_BUD),
        label="Nest slot",
    )


async def purchase_attractor(db: Any, guild_id: int, user_id: int) -> AttractorResult:
    """Burn BUD for a 1-hour buddy battle attractor.

    Stacks with an active timer by extending its expiry instead of
    replacing it (so a player can topup mid-buff without losing time).
    """
    # Flat BUD price -- ATTRACTOR_PRICE_BUD per hour, no oracle dependency.
    cost_raw = to_raw(ATTRACTOR_PRICE_BUD)
    held = await get_bud_wallet_raw(db, guild_id, user_id)
    if held < cost_raw:
        raise ValueError(
            f"Not enough BUD: need {to_human(cost_raw):,.4f}, "
            f"have {to_human(held):,.4f}."
        )

    await db.update_wallet_holding(
        user_id, guild_id, BUD_NETWORK_SHORT, BUD_SYMBOL, -int(cost_raw),
    )
    cost_human = to_human(int(cost_raw))
    ob, oa, impact, _lp = await _apply_burn_oracle_drop(
        db, guild_id, BUD_SYMBOL, cost_human, BUD_BURN_LP_REWARD_BPS,
    )
    # Atomic upsert: extend an active timer instead of replacing so a
    # player who buys two attractors back-to-back gets two hours, not
    # one. ``GREATEST(NOW(), attractor_until)`` is the trick.
    row = await db.fetch_one(
        f"""
        UPDATE user_buddy_economy
           SET attractor_until = GREATEST(
                   COALESCE(attractor_until, NOW()),
                   NOW()
               ) + INTERVAL '{int(ATTRACTOR_DURATION_S)} seconds',
               total_bud_burned_raw = total_bud_burned_raw + $3::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
         RETURNING attractor_until
        """,
        guild_id, user_id, int(cost_raw),
    )
    new_until = (row or {}).get("attractor_until")
    if not isinstance(new_until, _dt.datetime):
        new_until = _dt.datetime.utcnow() + _dt.timedelta(seconds=ATTRACTOR_DURATION_S)
    return AttractorResult(
        expires_at=new_until,
        bud_burned_raw=int(cost_raw),
        bud_oracle_before=float(ob),
        bud_oracle_after=float(oa),
        price_impact_pct=float(impact),
    )


# ── USD -> BUD auto-swap (used by ,buddy market for buyers paying in USD) ──


@dataclass
class AutoBuyResult:
    """Receipt for ``auto_buy_bud_for_market``.

    ``usd_paid_raw`` and ``bud_minted_raw`` are zero when no top-up was
    needed (the buyer already had enough BUD), so callers can append
    the impact line conditionally.
    """
    bud_held_after_raw: int
    usd_paid_raw: int
    bud_minted_raw: int
    bud_oracle_before: float
    bud_oracle_after: float
    price_impact_pct: float


async def auto_buy_bud_for_market(
    db: Any, guild_id: int, user_id: int, bud_needed_raw: int,
) -> AutoBuyResult:
    """Ensure the user has at least ``bud_needed_raw`` BUD; top up via USD.

    Used by services.buddy_market on a buy: the listing is denominated
    in BUD, but buyers can pay in USD. If the buyer already has enough
    BUD this is a no-op. Otherwise we mint the shortfall at the live
    BUD/USD oracle (with the standard mint-style impact applied -- the
    chart moves), debit the buyer's USD wallet, and credit the buyer's
    BUD wallet so the rest of the market path can proceed unchanged.

    Raises:
        ValueError on insufficient USD wallet, or on a zero/missing
        BUD oracle.
    """
    if bud_needed_raw <= 0:
        return AutoBuyResult(
            bud_held_after_raw=int(await get_bud_wallet_raw(db, guild_id, user_id)),
            usd_paid_raw=0, bud_minted_raw=0,
            bud_oracle_before=0.0, bud_oracle_after=0.0,
            price_impact_pct=0.0,
        )
    held = await get_bud_wallet_raw(db, guild_id, user_id)
    shortfall_raw = max(0, int(bud_needed_raw) - int(held))
    if shortfall_raw <= 0:
        return AutoBuyResult(
            bud_held_after_raw=int(held),
            usd_paid_raw=0, bud_minted_raw=0,
            bud_oracle_before=0.0, bud_oracle_after=0.0,
            price_impact_pct=0.0,
        )

    oracle_before = await _oracle_price(db, guild_id, BUD_SYMBOL)
    if oracle_before <= 0:
        raise ValueError("BUD oracle is not seeded yet on this server.")

    shortfall_human = to_human(shortfall_raw)
    supply = await _supply_human(db, guild_id, BUD_SYMBOL)
    impact = _price_impact(shortfall_human * oracle_before, oracle_before, supply)
    # Mint pricing: buyer pays at the AVERAGE of pre- and post-mint
    # oracle, exactly like fishing burn-swap on the LURE side.
    eff_price = oracle_before * (1.0 + impact / 2.0)
    usd_cost_human = shortfall_human * eff_price
    usd_cost_raw = to_raw(usd_cost_human)

    user_row = await db.get_user(int(user_id), int(guild_id))
    if not user_row or int(user_row.get("wallet") or 0) < usd_cost_raw:
        have_h = to_human(int((user_row or {}).get("wallet") or 0))
        raise ValueError(
            f"Need an extra {to_human(shortfall_raw):,.4f} BUD "
            f"(~ ${usd_cost_human:,.2f}) but you only have "
            f"${have_h:,.2f}."
        )

    # Debit USD, mint BUD, push the BUD oracle UP (mint pressure on
    # the auto-buy raises the oracle, mirroring the fishing burn-swap
    # mint-side oracle bump on REEL).
    await db.update_wallet(int(user_id), int(guild_id), -int(usd_cost_raw))
    await db.update_wallet_holding(
        int(user_id), int(guild_id), BUD_NETWORK_SHORT, BUD_SYMBOL,
        int(shortfall_raw),
    )
    oracle_after = max(1e-9, oracle_before * (1.0 + impact))
    try:
        await db.update_price(BUD_SYMBOL, int(guild_id), oracle_after)
    except Exception:
        log.exception(
            "auto_buy_bud_for_market: oracle update failed gid=%s",
            guild_id,
        )
    await _write_burn_candle(
        db, int(guild_id), BUD_SYMBOL,
        oracle_before, oracle_after, shortfall_human * oracle_before,
    )
    fee_usd = (shortfall_human * oracle_before) * (int(BUD_BURN_LP_REWARD_BPS) / 10_000.0)
    if fee_usd > 0:
        await _distribute_burn_lp_reward(db, int(guild_id), BUD_SYMBOL, fee_usd)

    held_after = await get_bud_wallet_raw(db, guild_id, user_id)
    return AutoBuyResult(
        bud_held_after_raw=int(held_after),
        usd_paid_raw=int(usd_cost_raw),
        bud_minted_raw=int(shortfall_raw),
        bud_oracle_before=float(oracle_before),
        bud_oracle_after=float(oracle_after),
        price_impact_pct=float(impact),
    )


# =============================================================================
# Buddy arena (PvE BUD + BBT mint surface)
# =============================================================================
# Players send their active CC buddy into a level-matched arena fight against
# an AI opponent. Win mints BUD into their wallet (subject to the standard
# mint-impact oracle drop) and BBT (universal battle token); loss is a counter
# bump only. Cooldown is enforced DB-side via last_arena_at so container/DB
# clock skew can't fast-forward it. Arena pays the network's stake-yield
# token (BUD) plus BBT -- never FREN, since FREN is the buddy-interaction
# loop currency (talk/feed/pet) and the two reward surfaces stay separate.

# Reward sizing. BBT is the headline arena reward (cross-game battle
# token, deflationary, bigger drop), BUD is a small drip on top (the
# Buddy Network's stake-yield token, used to keep arena participation
# wired into the FREN-stake loop). A Lv1 win mints ~ARENA_BBT_REWARD_BASE
# of BBT and a much smaller ~ARENA_BUD_REWARD_BASE of BUD; both scale
# per buddy level, and both are capped so a Lv50 fighter can't print
# arbitrary tokens. Tuned in line with wild_battle_rune_reward + LURE
# rewards so an arena win pays comparably to a clean fishing wild battle.
ARENA_BBT_REWARD_BASE: float       = 25.0
ARENA_BBT_REWARD_PER_LEVEL: float  = 2.5
ARENA_BBT_REWARD_MAX: float        = 250.0
ARENA_BUD_REWARD_BASE: float       = 2.5
ARENA_BUD_REWARD_PER_LEVEL: float  = 0.25
ARENA_BUD_REWARD_MAX: float        = 25.0

# Active-buddy XP per arena win. Mirrors the per-level scaling shape of
# the BUD/BBT rewards so a deep grind on the arena progresses the buddy's
# panel level alongside the player's tier. Conservative ceiling so chat
# / craft / expedition / wild-battle XP all stay relevant.
ARENA_XP_REWARD_BASE: int          = 30
ARENA_XP_REWARD_PER_LEVEL: float   = 2.0
ARENA_XP_REWARD_MAX: int           = 250

# Per-user cooldown (seconds). Tight enough that arena keeps an active player
# busy, loose enough that grinding is paced -- mirrors fishing's wild-battle
# cadence per cast.
ARENA_COOLDOWN_S: int              = 60

# Win-streak reward curve. Every consecutive arena win bumps the player's
# arena_streak by 1; a loss resets it to 0. The streak grants an additive
# bonus to BUD/BBT/XP equal to ARENA_STREAK_BONUS_PER_WIN per win above 1,
# capped at ARENA_STREAK_BONUS_MAX. So at +12 wins the player is at the
# +60% cap; below 2 wins the bonus is 0. Stacks additively with the
# clean-fight bonus and modifier reward bonus, and multiplicatively
# under the tier multiplier (same shape as the rest of the arena math).
ARENA_STREAK_BONUS_PER_WIN: float  = 0.05
ARENA_STREAK_BONUS_MAX: float      = 0.60
ARENA_STREAK_MILESTONES: tuple[int, ...] = (3, 5, 10, 20, 50)

# Daily arena boss. One attempt per UTC day per user, gated on the DB
# clock via last_arena_boss_at. The boss is the player's active-buddy
# level + ARENA_BOSS_LEVEL_BUMP, with HP / ATK scaled by the multipliers
# below to make it a real fight. Win pays ARENA_BOSS_PAYOUT_MULT * the
# normal arena BUD/BBT/XP for that level, plus a fixed flat cherry on top
# so first-time players see a meaningful payout. Loss is a counter bump,
# burns the daily attempt, and applies the standard arena cooldown.
ARENA_BOSS_LEVEL_BUMP: int         = 5
ARENA_BOSS_HP_MULT: float          = 1.60
ARENA_BOSS_ATK_MULT: float         = 1.25
ARENA_BOSS_PAYOUT_MULT: float      = 4.0
ARENA_BOSS_BBT_BONUS: float        = 100.0
ARENA_BOSS_BUD_BONUS: float        = 10.0
ARENA_BOSS_COOLDOWN_S: int         = 24 * 3600  # 24h sliding window

# Arena tier ladder. Tier is derived from lifetime arena_wins on
# user_buddy_economy and applied as a multiplier on the BUD reward formula
# so high-tier players keep climbing without the base table needing per-tier
# fields. Each entry is (key, label, emoji, min_wins, bud_mult, color_hex).
# Diamond is the cap -- the multiplier ladder stops there to keep the
# economy bounded.
ARENA_TIERS: tuple[tuple[str, str, str, int, float, int], ...] = (
    ("bronze",   "Bronze",   "\U0001F949", 0,   1.00, C_TIER_BRONZE),
    ("silver",   "Silver",   "\U0001F948", 10,  1.25, C_TIER_SILVER),
    ("gold",     "Gold",     "\U0001F947", 50,  1.60, C_TIER_GOLD),
    ("platinum", "Platinum", "\U0001F4A0", 150, 2.00, C_TIER_PLATINUM),
    ("diamond",  "Diamond",  "\U0001F48E", 500, 3.00, C_TIER_DIAMOND),
)


def arena_tier_for_wins(arena_wins: int) -> dict:
    """Return the tier dict for a given lifetime arena_wins count.

    Returns the highest tier whose ``min_wins`` is <= ``arena_wins``.
    Falls back to Bronze (the floor) for negative / zero counts.
    """
    chosen = ARENA_TIERS[0]
    for tier in ARENA_TIERS:
        if int(arena_wins) >= int(tier[3]):
            chosen = tier
    return {
        "key":        chosen[0],
        "label":      chosen[1],
        "emoji":      chosen[2],
        "min_wins":   int(chosen[3]),
        "bud_mult":   float(chosen[4]),
        "color_hex":  int(chosen[5]),
    }


def arena_next_tier(arena_wins: int) -> dict | None:
    """Return the NEXT tier above the player's current one, or None at cap."""
    for tier in ARENA_TIERS:
        if int(arena_wins) < int(tier[3]):
            return {
                "key":        tier[0],
                "label":      tier[1],
                "emoji":      tier[2],
                "min_wins":   int(tier[3]),
                "bud_mult":   float(tier[4]),
                "color_hex":  int(tier[5]),
            }
    return None


@dataclass
class ArenaResolution:
    """Outcome of resolve_arena_battle."""
    won: bool
    bud_reward_raw: int              # BUD minted on win (0 on loss)
    bbt_reward_raw: int              # BBT minted on win (universal battle token, 0 on loss)
    bud_oracle_before: float         # 0 on loss / when oracle is unavailable
    bud_oracle_after: float
    new_arena_wins: int              # lifetime arena_wins AFTER this row
    new_arena_losses: int            # lifetime arena_losses AFTER this row
    new_total_bud_earned_raw: int    # cumulative BUD won via arena AFTER this row
    tier_before: dict                # tier dict at the START of this fight
    tier_after: dict                 # tier dict AFTER this fight (may differ)
    tier_promoted: bool              # True iff tier_after.min_wins > tier_before.min_wins
    tier_bud_mult_applied: float     # multiplier the win paid out at (1.0 on loss)
    buddy_xp_awarded: int = 0        # XP added to the active buddy on win (0 on loss)
    fighter_buddy_id: int | None = None   # cc_buddies.id that received the XP
    streak_before: int = 0           # consecutive-win count at start of fight
    streak_after: int  = 0           # consecutive-win count after this fight
    best_streak_after: int = 0       # lifetime best streak after this fight
    streak_bonus_applied: float = 0.0  # additive decimal applied to BUD/BBT/XP
    streak_milestone: int | None = None  # set if this win crosses a milestone
    is_boss: bool = False            # True for boss battles
    boss_payout_mult: float = 1.0    # boss payout multiplier (1.0 for normal arena)
    modifier_key: str = "none"       # key of the arena modifier active for this fight
    modifier_reward_bonus: float = 0.0  # decimal bonus from the modifier (additive)


def arena_bud_reward(level: int) -> float:
    """BUD minted on a Lv ``level`` arena win (small drip alongside BBT).

    BBT is the headline reward; BUD is the buddy-network drip on top.
    Clamped to the network max so a Lv50 fighter can't print arbitrary BUD.
    """
    base = ARENA_BUD_REWARD_BASE + max(0, int(level) - 1) * ARENA_BUD_REWARD_PER_LEVEL
    return float(min(ARENA_BUD_REWARD_MAX, base))


def arena_xp_reward(level: int) -> int:
    """Active-buddy XP earned on a Lv ``level`` arena win.

    Linear in level then clamped at ``ARENA_XP_REWARD_MAX``; tier
    multiplier is applied at the call site (same shape as BUD / BBT).
    """
    base = ARENA_XP_REWARD_BASE + max(0, int(level) - 1) * ARENA_XP_REWARD_PER_LEVEL
    return int(min(ARENA_XP_REWARD_MAX, max(1, int(round(base)))))


def arena_bbt_reward(level: int) -> float:
    """BBT minted on a Lv ``level`` arena win (the headline arena reward).

    Same shape as :func:`arena_bud_reward` but scaled to the bigger BBT
    purse so BBT is the dominant token an arena win mints. Clamped to
    the network max.
    """
    base = ARENA_BBT_REWARD_BASE + max(0, int(level) - 1) * ARENA_BBT_REWARD_PER_LEVEL
    return float(min(ARENA_BBT_REWARD_MAX, base))


async def arena_cooldown_remaining_s(
    db: Any, guild_id: int, user_id: int,
) -> float:
    """Seconds left on the per-user arena cooldown, 0 if ready.

    DB-side clock per the project's "no Python clocks for cooldowns" rule.
    """
    state = await ensure_state(db, guild_id, user_id)
    if not state.get("last_arena_at"):
        return 0.0
    elapsed = await db.fetch_val(
        "SELECT EXTRACT(EPOCH FROM (NOW() - last_arena_at))::float "
        "FROM user_buddy_economy WHERE guild_id = $1 AND user_id = $2",
        int(guild_id), int(user_id),
    )
    if elapsed is None:
        return 0.0
    remaining = float(ARENA_COOLDOWN_S) - float(elapsed)
    return max(0.0, remaining)


async def arena_boss_cooldown_remaining_s(
    db: Any, guild_id: int, user_id: int,
) -> float:
    """Seconds left on the per-user daily boss cooldown, 0 if ready.

    Same DB-clock pattern as :func:`arena_cooldown_remaining_s`. Returns
    0.0 if the player has never fought a boss yet (NULL last_arena_boss_at).
    """
    state = await ensure_state(db, guild_id, user_id)
    if not state.get("last_arena_boss_at"):
        return 0.0
    elapsed = await db.fetch_val(
        "SELECT EXTRACT(EPOCH FROM (NOW() - last_arena_boss_at))::float "
        "FROM user_buddy_economy WHERE guild_id = $1 AND user_id = $2",
        int(guild_id), int(user_id),
    )
    if elapsed is None:
        return 0.0
    remaining = float(ARENA_BOSS_COOLDOWN_S) - float(elapsed)
    return max(0.0, remaining)


def arena_streak_bonus(streak: int) -> float:
    """Decimal bonus from a current streak. 0.0 below 2 wins, capped at +60%.

    Linear in the number of wins past the first, since the first win is
    the streak's seed (it's not "consecutive" until the second win).
    """
    above = max(0, int(streak) - 1)
    return min(ARENA_STREAK_BONUS_MAX, above * ARENA_STREAK_BONUS_PER_WIN)


def _hit_streak_milestone(prev: int, new: int) -> int | None:
    """Return the highest milestone reached when the streak went prev -> new."""
    crossed = [m for m in ARENA_STREAK_MILESTONES if prev < m <= new]
    return max(crossed) if crossed else None


async def resolve_arena_battle(
    db: Any, guild_id: int, user_id: int,
    *, won: bool,
    winner_level: int,
    bonus_pct: float = 0.0,
    is_boss: bool = False,
    modifier_key: str = "none",
    modifier_reward_bonus: float = 0.0,
) -> ArenaResolution:
    """Persist the outcome of an arena fight.

    Win mints BUD scaled by ``winner_level`` + ``bonus_pct`` + ``modifier_reward_bonus``
    + the player's streak bonus + the player's arena tier multiplier, then
    applies the standard mint-impact oracle drop. BBT is also minted as the
    universal battle token. Loss is a counter bump and resets the streak.

    ``bonus_pct``, ``modifier_reward_bonus`` and the streak bonus all stack
    *additively* into a single (1 + sum) factor, then the tier multiplier
    stacks *multiplicatively* on top -- same shape the existing arena math
    already used.

    Boss fights (``is_boss=True``) add an extra ``ARENA_BOSS_PAYOUT_MULT``
    factor on every reward, count toward separate boss W/L counters, and
    stamp ``last_arena_boss_at`` for the daily cooldown gate.
    """
    state = await ensure_state(db, guild_id, user_id)
    pre_wins = int(state.get("arena_wins") or 0)
    pre_streak = int(state.get("arena_streak") or 0)
    pre_best_streak = int(state.get("arena_best_streak") or 0)
    tier_before = arena_tier_for_wins(pre_wins)
    streak_for_bonus = pre_streak  # bonus applies to the win we're about to log

    bud_reward_raw = 0
    bbt_reward_raw = 0
    oracle_before = 0.0
    oracle_after = 0.0
    tier_mult = float(tier_before["bud_mult"]) if won else 1.0
    buddy_xp_awarded = 0
    fighter_buddy_id: int | None = None

    streak_bonus = arena_streak_bonus(streak_for_bonus) if won else 0.0
    mod_bonus = max(0.0, float(modifier_reward_bonus)) if won else 0.0
    boss_mult = float(ARENA_BOSS_PAYOUT_MULT) if (won and is_boss) else 1.0

    additive_bonus = (
        max(0.0, float(bonus_pct))
        + streak_bonus
        + mod_bonus
    )

    if won:
        base_human = arena_bud_reward(int(winner_level))
        bud_human = (
            base_human
            * (1.0 + additive_bonus)
            * float(tier_before["bud_mult"])
            * boss_mult
        )
        if is_boss:
            bud_human += ARENA_BOSS_BUD_BONUS
        if bud_human > 0:
            payout_raw = to_raw(float(bud_human))
            try:
                await db.update_wallet_holding(
                    int(user_id), int(guild_id), BUD_NETWORK_SHORT,
                    BUD_SYMBOL, int(payout_raw),
                )
                oracle_before, oracle_after, _ = await _apply_mint_oracle_drop(
                    db, int(guild_id), BUD_SYMBOL, float(bud_human),
                )
                bud_reward_raw = int(payout_raw)
            except Exception:
                log.exception(
                    "resolve_arena_battle: BUD reward credit failed "
                    "uid=%s gid=%s amt=%s", user_id, guild_id, bud_human,
                )

        # BBT (Buddy Battle Token) is the headline arena reward -- much
        # larger than the BUD drip. Scales by winner_level + bonus_pct +
        # tier multiplier the same way BUD does. Best-effort additive:
        # a BBT mint failure can't roll back the BUD payout the user
        # already saw. Mirrors fishing's wild-battle BBT credit and the
        # farm wild-battle path so every battle in the bot pays BBT
        # alongside its native drip.
        try:
            bbt_human = (
                arena_bbt_reward(int(winner_level))
                * (1.0 + additive_bonus)
                * float(tier_before["bud_mult"])
                * boss_mult
            )
            if is_boss:
                bbt_human += ARENA_BOSS_BBT_BONUS
            bbt_reward_raw = await mint_bbt_reward(
                db, int(guild_id), int(user_id), float(bbt_human),
                source="arena_boss" if is_boss else "arena",
            )
        except Exception:
            log.exception(
                "resolve_arena_battle: BBT mint failed uid=%s gid=%s",
                user_id, guild_id,
            )

        # Active-buddy XP. Mirrors the wild-battle XP credit in
        # services.dungeon / services.fishing -- the buddy who fought
        # actually progresses on its own row, not just the player's
        # arena_wins counter. Tier multiplier + clean-fight bonus
        # stack the same way they do for BUD / BBT.
        try:
            from services.buddy_battle import award_battle_xp as _award_bxp
            active = await db.fetch_one(
                "SELECT id FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 "
                "  AND status = 'owned' AND is_active "
                "LIMIT 1",
                int(guild_id), int(user_id),
            )
            if active and int(active.get("id") or 0) > 0:
                # Battle-lane multiplier on top of base XP. Same shape
                # as the wild-battle XP credit in the cog services.
                try:
                    from services.buddy_bonus import buddy_bonus as _bb
                    battle_mult = await _bb(
                        db, guild_id, user_id, lane="battle",
                    )
                except Exception:
                    battle_mult = 1.0
                xp_award = int(round(
                    arena_xp_reward(int(winner_level))
                    * (1.0 + additive_bonus)
                    * float(tier_before["bud_mult"])
                    * battle_mult
                    * boss_mult
                ))
                if xp_award > 0:
                    await _award_bxp(
                        db, int(guild_id),
                        int(user_id), int(active["id"]),
                        int(xp_award),
                    )
                    buddy_xp_awarded = xp_award
                    fighter_buddy_id = int(active["id"])
        except Exception:
            log.exception(
                "resolve_arena_battle: buddy XP credit failed uid=%s gid=%s",
                user_id, guild_id,
            )

    # Boss fights have their own counter pair + the daily-cooldown
    # timestamp. Normal arena fights bump the lifetime arena_wins/losses
    # and the streak columns. Both branches still update last_arena_at
    # so the per-fight cooldown applies even after a boss attempt.
    if is_boss:
        row = await db.fetch_one(
            """
            UPDATE user_buddy_economy
               SET arena_boss_wins   = arena_boss_wins
                                     + (CASE WHEN $3 THEN 1 ELSE 0 END),
                   arena_boss_losses = arena_boss_losses
                                     + (CASE WHEN $3 THEN 0 ELSE 1 END),
                   arena_bud_earned_raw = arena_bud_earned_raw + $4::numeric,
                   last_arena_boss_at   = NOW(),
                   last_arena_at        = NOW(),
                   updated_at           = NOW()
             WHERE guild_id = $1 AND user_id = $2
            RETURNING arena_wins, arena_losses, arena_bud_earned_raw,
                     arena_streak, arena_best_streak,
                     arena_boss_wins, arena_boss_losses
            """,
            int(guild_id), int(user_id), bool(won), int(bud_reward_raw),
        )
        # Boss fights don't count toward the standard arena_streak. They
        # have their own boss W/L surface so a streak grinder on the
        # daily boss can't pad the leaderboard. New_streak / new_best
        # carry forward unchanged.
        new_streak = pre_streak
        new_best = pre_best_streak
    else:
        # CASE expression handles the streak: win => streak + 1, loss => 0.
        # arena_best_streak is GREATEST(prev, new) so a fresh personal best
        # is captured the same row.
        row = await db.fetch_one(
            """
            UPDATE user_buddy_economy
               SET arena_wins              = arena_wins
                                           + (CASE WHEN $3 THEN 1 ELSE 0 END),
                   arena_losses            = arena_losses
                                           + (CASE WHEN $3 THEN 0 ELSE 1 END),
                   arena_bud_earned_raw    = arena_bud_earned_raw + $4::numeric,
                   arena_streak            = (CASE WHEN $3
                                                THEN arena_streak + 1
                                                ELSE 0 END),
                   arena_best_streak       = GREATEST(
                                                arena_best_streak,
                                                CASE WHEN $3
                                                     THEN arena_streak + 1
                                                     ELSE 0 END
                                             ),
                   last_arena_at           = NOW(),
                   updated_at              = NOW()
             WHERE guild_id = $1 AND user_id = $2
            RETURNING arena_wins, arena_losses, arena_bud_earned_raw,
                     arena_streak, arena_best_streak,
                     arena_boss_wins, arena_boss_losses
            """,
            int(guild_id), int(user_id), bool(won), int(bud_reward_raw),
        )
        new_streak = int((row or {}).get("arena_streak") or 0)
        new_best = int((row or {}).get("arena_best_streak") or 0)

    new_wins = int((row or {}).get("arena_wins") or 0)
    new_losses = int((row or {}).get("arena_losses") or 0)
    new_total = int((row or {}).get("arena_bud_earned_raw") or 0)
    tier_after = arena_tier_for_wins(new_wins)
    promoted = bool(tier_after["min_wins"] > tier_before["min_wins"])
    milestone = _hit_streak_milestone(pre_streak, new_streak) if won else None

    return ArenaResolution(
        won=bool(won),
        bud_reward_raw=int(bud_reward_raw),
        bbt_reward_raw=int(bbt_reward_raw),
        bud_oracle_before=float(oracle_before),
        bud_oracle_after=float(oracle_after),
        new_arena_wins=new_wins,
        new_arena_losses=new_losses,
        new_total_bud_earned_raw=new_total,
        tier_before=tier_before,
        tier_after=tier_after,
        tier_promoted=promoted,
        tier_bud_mult_applied=tier_mult,
        buddy_xp_awarded=int(buddy_xp_awarded),
        fighter_buddy_id=fighter_buddy_id,
        streak_before=int(pre_streak),
        streak_after=int(new_streak),
        best_streak_after=int(new_best),
        streak_bonus_applied=float(streak_bonus),
        streak_milestone=milestone,
        is_boss=bool(is_boss),
        boss_payout_mult=float(boss_mult),
        modifier_key=str(modifier_key),
        modifier_reward_bonus=float(mod_bonus),
    )


async def list_arena_leaderboard(
    db: Any, guild_id: int, *, limit: int = 10,
) -> list[dict]:
    """Top players in this guild ranked by lifetime arena_wins.

    Pulls (user_id, arena_wins, arena_losses, arena_bud_earned_raw,
    arena_streak, arena_best_streak, arena_boss_wins) so the leaderboard
    panel can show W/L, total earnings, current/best streak, and boss
    kills without a per-row follow-up query.
    """
    rows = await db.fetch_all(
        """
        SELECT user_id, arena_wins, arena_losses, arena_bud_earned_raw,
               arena_streak, arena_best_streak,
               arena_boss_wins, arena_boss_losses
          FROM user_buddy_economy
         WHERE guild_id = $1 AND arena_wins > 0
         ORDER BY arena_wins DESC, arena_bud_earned_raw DESC
         LIMIT $2
        """,
        int(guild_id), int(limit),
    )
    return list(rows or [])


async def list_arena_streak_leaderboard(
    db: Any, guild_id: int, *, limit: int = 10,
) -> list[dict]:
    """Top players ranked by lifetime best arena win-streak.

    Different ordering surface than list_arena_leaderboard so a player who
    can stack consistent win streaks gets a separate spotlight from a
    pure-volume grinder. Tie-breaker is current streak then total wins.
    """
    rows = await db.fetch_all(
        """
        SELECT user_id, arena_wins, arena_losses, arena_bud_earned_raw,
               arena_streak, arena_best_streak,
               arena_boss_wins, arena_boss_losses
          FROM user_buddy_economy
         WHERE guild_id = $1 AND arena_best_streak > 0
         ORDER BY arena_best_streak DESC,
                  arena_streak DESC,
                  arena_wins DESC
         LIMIT $2
        """,
        int(guild_id), int(limit),
    )
    return list(rows or [])


__all__ = (
    "BUD_NETWORK_SHORT", "BUD_SYMBOL", "FREN_SYMBOL",
    "FREN_STAKE_BUD_PER_DAY", "BUD_BURN_LP_REWARD_BPS",
    "BUD_CASHOUT_LP_REWARD_BPS", "ATTRACTOR_BUFF_MULT",
    "BATTLE_SLOT_PRICE_BUD", "STORAGE_SLOT_PRICE_BUD",
    "EGG_STORAGE_PRICE_BUD", "NEST_SLOT_PRICE_BUD",
    "BATTLE_SLOT_PRICE_USD", "STORAGE_SLOT_PRICE_USD",
    "EGG_STORAGE_PRICE_USD", "NEST_SLOT_PRICE_USD",
    "ATTRACTOR_PRICE_USD",
    "ATTRACTOR_DURATION_S",
    "StakeResult", "BurnResult", "CashoutResult", "SwapQuote",
    "SlotResult", "AttractorResult",
    "ensure_state", "list_state",
    "accrued_yield",
    "user_max_buddies",
    "user_max_battle_slots", "user_max_storage_slots",
    "user_max_egg_storage", "user_max_nest_slots",
    "capture_destination",
    "attractor_active",
    "get_bud_wallet_raw", "get_fren_wallet_raw",
    "stake_fren", "unstake_fren", "claim_yield",
    "burn_for_bud", "burn_bud_for", "quote_burn_swap", "cashout_bud",
    "purchase_battle_slot", "purchase_storage_slot",
    "purchase_egg_storage", "purchase_nest_slot",
    "purchase_attractor",
    "AutoBuyResult", "auto_buy_bud_for_market",
    "ArenaResolution", "arena_bud_reward", "arena_bbt_reward",
    "arena_cooldown_remaining_s",
    "arena_boss_cooldown_remaining_s",
    "arena_streak_bonus",
    "resolve_arena_battle",
    "list_arena_leaderboard", "list_arena_streak_leaderboard",
    "arena_tier_for_wins", "arena_next_tier",
    "ARENA_BUD_REWARD_BASE", "ARENA_BUD_REWARD_PER_LEVEL",
    "ARENA_BUD_REWARD_MAX",
    "ARENA_BBT_REWARD_BASE", "ARENA_BBT_REWARD_PER_LEVEL",
    "ARENA_BBT_REWARD_MAX",
    "ARENA_COOLDOWN_S", "ARENA_TIERS",
    "ARENA_STREAK_BONUS_PER_WIN", "ARENA_STREAK_BONUS_MAX",
    "ARENA_STREAK_MILESTONES",
    "ARENA_BOSS_LEVEL_BUMP", "ARENA_BOSS_HP_MULT", "ARENA_BOSS_ATK_MULT",
    "ARENA_BOSS_PAYOUT_MULT", "ARENA_BOSS_BBT_BONUS",
    "ARENA_BOSS_BUD_BONUS", "ARENA_BOSS_COOLDOWN_S",
    "mint_bud_reward", "mint_bbt_reward",
)
