"""Swap service layer  -  shared by Discord commands and web API.

Encapsulates all AMM swap logic: validation, constant-product math,
volume limits, and execution. No Discord dependencies.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field

from core.config import Config
from core.framework.scale import to_human, to_raw

# ── Constants ────────────────────────────────────────────────────────────────
from constants.trading import DEFAULT_SWAP_FEE, SWAP_PLATFORM_FEE_PCT, SLIPPAGE_WARN
from constants.validators import NET_SHORT

log = logging.getLogger("discoin.swap")

_STABLE_QUOTE = frozenset({"USD", "USDC", "DSD"})  # stablecoins treated as $1 in arb/impact math


async def is_bud_swappable_pair(
    db, guild_id: int, token_a: str, token_b: str, all_tokens: dict | None = None,
) -> bool:
    """True if this BUD pair allows bidirectional AMM swaps.

    Mirrors :func:`is_moon_swappable_pair` for the Buddy Network. Both
    BUD and FREN are in ``Config.EARN_ONLY_TOKENS``, so every swap is
    blocked by default. The carve-out lets players bridge to/from the
    other earn-economy networks (REEL on Lure, RUNE on Crypt, MOON on
    Moon) and to FREN on the same network without going through the
    USD AMM.

    Direction-agnostic; callers run their own guard so a non-BUD pair
    returns False fast.
    """
    del db, guild_id, all_tokens  # signature parity with is_moon_swappable_pair
    a, b = token_a.upper(), token_b.upper()
    if "BUD" not in (a, b):
        return False
    other = b if a == "BUD" else a
    return other in Config.BUD_SWAPPABLE_TOKENS


async def is_moon_swappable_pair(
    db, guild_id: int, token_a: str, token_b: str, all_tokens: dict | None = None,
) -> bool:
    """True if this MOON pair allows bidirectional AMM swaps.

    The general earn-only firewall blocks every swap into MOON. The two
    legitimate carve-outs are:

    * Built-in tokens listed in ``Config.MOON_SWAPPABLE_TOKENS`` (mMTA, mSUN
      -- the wrapped Moon-Network coins).
    * Player-deployed tokens whose contract was minted with
      ``params["moon_swappable"] = True`` (set by ``token deploy`` whenever
      a TOKEN/MOON pool is auto-seeded alongside the founder's chosen pool).

    Group tokens on Moon Network are deliberately excluded so the legacy
    one-way ``MOON -> GROUP`` semantics keep the Lunar Mint as the only
    minting venue against a group token.

    The helper ignores swap direction -- callers wrap it in their own
    direction guard so a non-MOON pair returns False short-circuit.
    """
    a, b = token_a.upper(), token_b.upper()
    if "MOON" not in (a, b):
        return False
    other = b if a == "MOON" else a
    if other in Config.MOON_SWAPPABLE_TOKENS:
        return True
    if all_tokens is None:
        try:
            all_tokens = await db.get_all_tokens_for_guild(guild_id)
        except Exception:
            all_tokens = {}
    # Group tokens on Moon Network keep the legacy MOON -> GROUP one-way
    # path; never treat them as bidirectional even if they somehow get
    # tagged with the contract flag.
    meta = all_tokens.get(other, {}) if all_tokens else {}
    if meta.get("token_type") == "group" and meta.get("network") == "Moon Network":
        return False
    try:
        contract = await db.get_token_contract(guild_id, other)
    except Exception:
        contract = {}
    return bool(contract.get("moon_swappable"))


async def liqstone_swap_fee_discount(db, user_id: int, guild_id: int) -> float:
    """Return the fractional swap-fee discount a user earns from their Liqstone.

    Pulled here rather than cogs/shop.py so the API-facing swap service does
    not import Discord code. Caps the discount at the item's base fee so the
    effective fee never goes below zero.
    """
    try:
        get_liq = getattr(db, "get_liqstone", None)
        if get_liq is None:
            return 0.0
        liq = await get_liq(user_id, guild_id)
    except Exception:
        return 0.0
    if not liq:
        return 0.0
    cfg = Config.SHOP_ITEMS.get("liqstone", {})
    per_level = float(cfg.get("stats", {}).get("swap_fee_discount", 0.0))
    if per_level <= 0.0:
        return 0.0
    level = int(liq.get("level") or 0)
    if level <= 0:
        return 0.0
    discount = per_level * level
    # Cap to leave at least 10% of the base fee intact (no zero-fee swaps).
    max_discount = max(0.0, DEFAULT_SWAP_FEE * 0.9)
    return min(discount, max_discount)


def apply_liqstone_discount(base_fee: float, discount: float) -> float:
    """Return the effective swap fee after the Liqstone discount is applied."""
    eff = base_fee - float(discount or 0.0)
    return max(0.0, eff)


async def chimerastone_swap_fee_discount(db, user_id: int, guild_id: int) -> float:
    """Return the fractional fee discount a user earns from their Chimerastone.

    Stacks ADDITIVELY on top of the Liqstone discount: the swap path
    sums both, then clamps the combined discount so the effective fee
    never falls below 10% of the base. Mirrors ``liqstone_swap_fee_discount``
    so the trade cog can apply both with one ``apply_liqstone_discount``
    call, e.g. ``apply_liqstone_discount(base, lq + ch)``.
    """
    try:
        get_ch = getattr(db, "get_chimerastone", None)
        if get_ch is None:
            return 0.0
        ch = await get_ch(user_id, guild_id)
    except Exception:
        return 0.0
    if not ch:
        return 0.0
    cfg = Config.SHOP_ITEMS.get("chimerastone", {})
    per_level = float(cfg.get("stats", {}).get("swap_fee_bonus", 0.0))
    if per_level <= 0.0:
        return 0.0
    level = int(ch.get("level") or 0)
    if level <= 0:
        return 0.0
    discount = per_level * level
    # Same 10%-floor cap the liqstone helper enforces, so even a max
    # Chimerastone + max Liqstone leaves the swap fee non-zero.
    max_discount = max(0.0, DEFAULT_SWAP_FEE * 0.9)
    return min(discount, max_discount)

# ── Anti-drain state (in-memory, resets on restart) ──────────────────────────
# Entries are (reservation_id, wall_ts, usd_value).  The integer reservation_id
# from a monotone counter is used as the cancellation key so that two
# reservations made within the same time.time() tick can be distinguished.
_user_swap_volume: dict[tuple[int, int], list[tuple[int, float, float]]] = {}
_user_swap_locks: dict[tuple[int, int], asyncio.Lock] = {}
_swap_reservation_seq: itertools.count = itertools.count(1)

# Per-(user, pool) last-swap timestamp. Prevents the "spam max swap" exploit
# where a player repeatedly fires `swap mta tok max` because each call only
# checks 15%-of-CURRENT-reserves and never enforces a delay between calls.
_user_pool_last_swap: dict[tuple[int, int, str], float] = {}
_user_pool_swap_locks: dict[tuple[int, int, str], asyncio.Lock] = {}
USER_POOL_SWAP_COOLDOWN: int = 30  # seconds between swaps in the same pool

# ── Depeg buy tracking (in-memory, rolling 24-hour window) ───────────────────
# Keyed by (user_id, guild_id, symbol) → list of (reservation_id, wall_ts, usd_value).
# Resets on bot restart (acceptable  -  depeg protection is a soft throttle).
_depeg_buy_volume: dict[tuple[int, int, str], list[tuple[int, float, float]]] = {}
# Per-key locks prevent concurrent buys from both passing the cap before either records.
_depeg_buy_locks: dict[tuple[int, int, str], asyncio.Lock] = {}
_depeg_reservation_seq: itertools.count = itertools.count(1)


def check_user_swap_volume(user_id: int, guild_id: int, usd_value: float) -> tuple[bool, float]:
    """Check if user is within hourly swap volume limit. Returns (allowed, remaining).

    .. note::
        This is a **non-atomic, advisory** helper used by the quote path to
        provide early feedback.  The execution path calls
        :func:`reserve_user_swap_volume` for the authoritative check; a quote
        showing remaining allowance may be stale if another swap completes
        between quote generation and execution.
    """
    key = (user_id, guild_id)
    now = time.time()
    cutoff = now - 3600
    entries = _user_swap_volume.get(key, [])
    entries = [e for e in entries if e[1] > cutoff]
    _user_swap_volume[key] = entries
    total = sum(e[2] for e in entries)
    remaining = Config.USER_SWAP_HOURLY_LIMIT_USD - total
    return usd_value <= remaining, max(0.0, remaining)


def record_user_swap_volume(user_id: int, guild_id: int, usd_value: float) -> None:
    """Record a swap volume entry."""
    key = (user_id, guild_id)
    if key not in _user_swap_volume:
        _user_swap_volume[key] = []
    _user_swap_volume[key].append((next(_swap_reservation_seq), time.time(), usd_value))


async def reserve_user_swap_volume(
    user_id: int, guild_id: int, usd_value: float
) -> tuple[bool, float, int | None]:
    """Atomically check and reserve user hourly swap volume.

    Returns ``(allowed, remaining_usd, reservation_id)``.
    On success the reservation is already recorded and should be cancelled only if
    the downstream trade/swap fails.
    """
    key = (user_id, guild_id)
    if key not in _user_swap_locks:
        _user_swap_locks[key] = asyncio.Lock()
    async with _user_swap_locks[key]:
        now = time.time()
        cutoff = now - 3600
        entries = _user_swap_volume.get(key, [])
        entries = [e for e in entries if e[1] > cutoff]
        total = sum(e[2] for e in entries)
        remaining = Config.USER_SWAP_HOURLY_LIMIT_USD - total
        if usd_value > remaining:
            _user_swap_volume[key] = entries
            return False, max(0.0, remaining), None
        res_id = next(_swap_reservation_seq)
        entries.append((res_id, now, usd_value))
        _user_swap_volume[key] = entries
        return True, max(0.0, remaining - usd_value), res_id


def cancel_user_swap_reservation(user_id: int, guild_id: int, reservation_id: int) -> None:
    """Release a previously reserved user swap-volume slot.

    Safe to call more than once for the same reservation.
    """
    key = (user_id, guild_id)
    entries = _user_swap_volume.get(key)
    if not entries:
        return
    for i, (rid, _, _) in enumerate(entries):
        if rid == reservation_id:
            del entries[i]
            break


def check_pool_swap_cooldown(user_id: int, guild_id: int, pool_id: str) -> tuple[bool, int]:
    """Check if user is allowed to swap in this pool right now.

    Returns ``(allowed, seconds_remaining)``. The cooldown stops the
    "spam swap max" exploit where each call only checks N% of current
    reserves and a fast loop drains far more than the intended limit.

    .. note::
        This is a **non-atomic, advisory** helper.  Production callers must
        use :func:`reserve_pool_swap` for the authoritative check; concurrent
        commands can otherwise both pass this check before either records.
    """
    key = (user_id, guild_id, pool_id)
    last = _user_pool_last_swap.get(key, 0.0)
    now = time.time()
    elapsed = now - last
    if elapsed < USER_POOL_SWAP_COOLDOWN:
        return False, max(1, int(USER_POOL_SWAP_COOLDOWN - elapsed))
    return True, 0


def record_pool_swap(user_id: int, guild_id: int, pool_id: str) -> None:
    """Record a successful swap for cooldown tracking.

    .. note::
        Use :func:`reserve_pool_swap` in production handlers; it combines
        check + record atomically under a lock so two parallel swap commands
        cannot both bypass the per-pool cooldown.
    """
    _user_pool_last_swap[(user_id, guild_id, pool_id)] = time.time()


async def reserve_pool_swap(
    user_id: int, guild_id: int, pool_id: str
) -> tuple[bool, int]:
    """Atomically check **and** record the per-pool swap cooldown.

    Returns ``(allowed, seconds_remaining)``.  On ``allowed=True`` the
    timestamp is committed immediately under an :class:`asyncio.Lock`, so
    two concurrent swap commands cannot both pass the check before either
    records.  Callers that abort before executing the swap (e.g. user
    cancels the confirmation prompt) must release the slot via
    :func:`cancel_pool_swap_reservation` so the player isn't locked out
    for the full cooldown after a no-op.
    """
    key = (user_id, guild_id, pool_id)
    if key not in _user_pool_swap_locks:
        _user_pool_swap_locks[key] = asyncio.Lock()
    async with _user_pool_swap_locks[key]:
        now = time.time()
        last = _user_pool_last_swap.get(key, 0.0)
        elapsed = now - last
        if elapsed < USER_POOL_SWAP_COOLDOWN:
            return False, max(1, int(USER_POOL_SWAP_COOLDOWN - elapsed))
        _user_pool_last_swap[key] = now
        return True, 0


def cancel_pool_swap_reservation(
    user_id: int, guild_id: int, pool_id: str
) -> None:
    """Release a previously reserved pool swap slot (call on swap abort).

    Safe to call repeatedly; only clears the entry if it was set in the
    current cooldown window.
    """
    _user_pool_last_swap.pop((user_id, guild_id, pool_id), None)


def _minute_ts() -> int:
    """Floor the current epoch second to the start of the current minute.

    Mirrors ``cogs.trade._minute_ts`` so the swap service can extend the
    same 1-minute candle row that ``buy`` / ``sell`` and the drift loop
    write to. Kept local to avoid pulling a Discord cog into the API
    layer.
    """
    return int(time.time()) // 60 * 60


# Cap how far a single swap can move the oracle in either direction. The AMM
# constant-product math already bounds price impact at the dynamic max-swap
# fraction (~15% of pool depth on a normal pool), but a low-liquidity group
# pool can synthesise a >50% impact on a single trade -- propagating that
# straight into the oracle would yank the chart off-screen and trip the
# next drift tick's circuit-breaker. 5% per trade leaves room for whales
# to actually move the chart while keeping the snap bounded; subsequent
# trades or the drift loop pull it the rest of the way.
_SWAP_ORACLE_NUDGE_CAP: float = 0.025


async def apply_swap_oracle_nudge(
    db, guild_id: int,
    token_in: str, token_out: str,
    price_impact: float, swap_usd_value: float,
) -> None:
    """Push both legs of a swap into the price oracle and the chart candles.

    .buy and .sell both call ``update_price`` + ``upsert_candle`` so the
    chart reflects the trade within the same minute it happened. Swaps
    historically only mutated pool reserves and let the drift loop
    arbitrage the oracle back into line a few ticks later, which made
    swap-only pairs (mMTA/MOON, mSUN/MOON, the new player-deployed
    TOKEN/MOON pools) look static on the chart even when players were
    actively trading.

    The nudge is symmetric: ``token_out`` (bought) goes up and
    ``token_in`` (sold) goes down by the same fraction so the implied
    cross-rate moves with the AMM. We damp the impact by half before
    splitting so the geometric mean of the two oracles is roughly
    preserved -- the AMM has already taken its constant-product cut, no
    need to double-count it on the chart.

    Stablecoins, USD, and pegged wrappers (mMTA, mSUN -- their oracle is
    clamped each tick by the drift loop's peg_band logic) are skipped on
    the assumption that the existing peg machinery will dominate any
    nudge we apply here.
    """
    if price_impact <= 0:
        return
    nudge = min(price_impact, _SWAP_ORACLE_NUDGE_CAP) * 0.5
    if nudge <= 0:
        return

    for sym, direction in ((token_in, -1.0), (token_out, +1.0)):
        await _nudge_symbol_oracle(
            db, guild_id, sym, direction=direction,
            nudge_fraction=nudge, volume_usd=swap_usd_value,
        )


async def _nudge_symbol_oracle(
    db, guild_id: int, sym: str,
    *, direction: float, nudge_fraction: float, volume_usd: float,
) -> None:
    """Move a single symbol's oracle + write a candle row.

    Shared by ``apply_swap_oracle_nudge`` (AMM path) and
    ``apply_trade_oracle_impact`` (market-maker path) so the chart
    reacts identically to both. Stablecoins and pegged wrappers are
    skipped because their oracle is clamped by the drift loop each
    tick.

    Failures are swallowed and logged; the drift loop reconciles on
    the next tick so a temporary DB hiccup never breaks the trade
    flow that called us.
    """
    if nudge_fraction <= 0:
        return
    if sym in _STABLE_QUOTE:
        return
    meta = Config.TOKENS.get(sym, {})
    if meta.get("stablecoin") or meta.get("peg_to"):
        return
    try:
        row = await db.get_price(sym, guild_id)
    except Exception:
        return
    if not row:
        return
    old_price = float(row["price"])
    if old_price <= 0:
        return
    new_price = max(1e-15, old_price * (1.0 + direction * nudge_fraction))
    minute = _minute_ts()
    try:
        await db.update_price(sym, guild_id, new_price)
        await db.upsert_candle(
            guild_id, f"{sym}USD", minute,
            open_=old_price,
            high=max(old_price, new_price),
            low=min(old_price, new_price),
            close=new_price,
            volume_delta=volume_usd,
        )
    except Exception as exc:
        log.warning(
            "oracle nudge failed sym=%s gid=%s: %s  -  "
            "drift loop will reconcile next tick.",
            sym, guild_id, exc,
        )


def trade_oracle_impact_for_usd(usd_value: float) -> float:
    """Translate a market-order USD size into an oracle nudge fraction.

    Uses the same ``Config.PRICE_IMPACT_DIVISOR`` curve that
    ``services.trade.buy`` / ``sell`` apply as user-visible slippage,
    then clamps to ``_SWAP_ORACLE_NUDGE_CAP`` so a single market order
    cannot yank the chart off-screen.

    Public so the test suite can pin the curve and the API can mirror
    the bot's chart impact.
    """
    if usd_value <= 0:
        return 0.0
    raw = usd_value / max(1.0, float(Config.PRICE_IMPACT_DIVISOR))
    return float(min(raw, _SWAP_ORACLE_NUDGE_CAP))


async def apply_trade_oracle_impact(
    db, guild_id: int, sym: str, *, usd_value: float, direction: int,
) -> None:
    """Push a market-maker buy/sell into the oracle + candle row.

    ``services.trade.buy`` / ``sell`` historically rebalanced the user's
    position but never touched ``crypto_prices`` or ``candles``, so the
    chart stayed flat even on large trades and every TA-driven game
    (predictions, gamba trends, market events) lied about the state of
    the market.

    ``direction`` is ``+1`` for a buy (price up) and ``-1`` for a sell
    (price down). The nudge fraction is symmetric for both legs of the
    same dollar size so an instant buy-then-sell cancels out at the
    chart level.

    Stablecoins, pegged wrappers, and missing oracle rows are skipped
    (same rules as ``apply_swap_oracle_nudge``). The 1-minute candle row
    is upserted so a flurry of buys inside the same minute aggregates
    into one OHLC bar.
    """
    nudge = trade_oracle_impact_for_usd(usd_value)
    if nudge <= 0:
        return
    await _nudge_symbol_oracle(
        db, guild_id, sym, direction=float(direction),
        nudge_fraction=nudge, volume_usd=usd_value,
    )


def is_depeg(price: float, ath: float) -> bool:
    """Return True if *price* is below the depeg threshold relative to *ath*.

    A token is considered depegged when its current price has fallen below
    ``Config.DEPEG_THRESHOLD`` × ATH (e.g. 30% of the all-time high).
    If ATH is unknown (0), depeg mode is never triggered.
    """
    if ath <= 0:
        return False
    return price < ath * Config.DEPEG_THRESHOLD


def check_depeg_buy(
    user_id: int, guild_id: int, symbol: str, usd_value: float
) -> tuple[bool, float]:
    """Check whether a buy is within the per-user 24-hour depeg buy cap.

    Returns ``(allowed, remaining_usd)``.  Only call this when the token is
    already confirmed to be in depeg mode (``is_depeg()`` == True).

    .. note::
        This is a **non-atomic** helper, safe for tests and single-threaded
        inspection.  Use :func:`reserve_depeg_buy` in production command
        handlers to prevent concurrent buys from both passing the cap.
    """
    key = (user_id, guild_id, symbol)
    now = time.time()
    cutoff = now - 86400  # rolling 24-hour window
    entries = _depeg_buy_volume.get(key, [])
    entries = [e for e in entries if e[1] > cutoff]
    _depeg_buy_volume[key] = entries
    total = sum(e[2] for e in entries)
    remaining = Config.DEPEG_DAILY_BUY_USD - total
    return usd_value <= remaining, max(0.0, remaining)


def record_depeg_buy(user_id: int, guild_id: int, symbol: str, usd_value: float) -> None:
    """Record a depeg-mode buy against the 24-hour cap.

    .. note::
        Use :func:`reserve_depeg_buy` in production command handlers instead;
        it combines check + record atomically under a lock.
    """
    key = (user_id, guild_id, symbol)
    if key not in _depeg_buy_volume:
        _depeg_buy_volume[key] = []
    _depeg_buy_volume[key].append((next(_depeg_reservation_seq), time.time(), usd_value))


async def reserve_depeg_buy(
    user_id: int, guild_id: int, symbol: str, usd_value: float
) -> tuple[bool, float, int | None]:
    """Atomically check **and** reserve a depeg buy slot.

    Returns ``(allowed, remaining_usd, reservation_id)``.

    If ``allowed`` is ``True`` the amount is immediately appended to the
    rolling 24-hour volume dict under an :class:`asyncio.Lock`, preventing
    concurrent buy commands from both passing the cap before either records.

    On trade failure (e.g. DB error after this call) the caller must release
    the reserved allowance via
    ``cancel_depeg_reservation(user_id, guild_id, symbol, reservation_id)``.
    On trade success the reservation stands  -  no separate record call is needed.

    If ``allowed`` is ``False``, ``reservation_id`` is ``None`` and no slot was
    consumed.
    """
    key = (user_id, guild_id, symbol)
    if key not in _depeg_buy_locks:
        _depeg_buy_locks[key] = asyncio.Lock()
    async with _depeg_buy_locks[key]:
        now = time.time()
        cutoff = now - 86400
        entries = _depeg_buy_volume.get(key, [])
        entries = [e for e in entries if e[1] > cutoff]
        total = sum(e[2] for e in entries)
        remaining = Config.DEPEG_DAILY_BUY_USD - total
        if usd_value > remaining:
            _depeg_buy_volume[key] = entries
            return False, max(0.0, remaining), None
        # Reserve immediately  -  also acts as the final record on trade success.
        res_id = next(_depeg_reservation_seq)
        entries.append((res_id, now, usd_value))
        _depeg_buy_volume[key] = entries
        return True, max(0.0, remaining - usd_value), res_id


def cancel_depeg_reservation(
    user_id: int, guild_id: int, symbol: str, reservation_id: int
) -> None:
    """Release a previously reserved depeg buy slot (call on trade failure).

    Removes the entry recorded at ``reservation_id`` so the allowance is
    returned to the user.  Safe to call more than once for the same token
    (extra calls are silently ignored).
    """
    key = (user_id, guild_id, symbol)
    entries = _depeg_buy_volume.get(key)
    if not entries:
        return
    for i, (rid, _, _) in enumerate(entries):
        if rid == reservation_id:
            del entries[i]
            break


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class SwapQuote:
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float
    fee: float
    fee_amount: float
    price_impact: float
    spot_price: float
    exec_price: float
    pool_id: str
    canon_a: str
    canon_b: str
    reserve_in: float
    reserve_out: float
    use_mempool: bool = False
    gas_fee: float = 0.0
    gas_coin: str = ""
    gas_emoji: str = ""
    platform_fee: float = 0.0
    total_gas_cost: float = 0.0
    gas_price: str = "medium"
    network: str = ""
    net_short: str = ""
    min_amount_out: float = 0.0
    swap_usd_value: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class SwapResult:
    success: bool
    tx_hash: str | None = None
    mempool_id: int | None = None
    amount_out: float = 0.0
    rebate: float = 0.0
    error: str | None = None


# ── Core functions ───────────────────────────────────────────────────────────

async def compute_swap_quote(
    db,
    guild_id: int,
    user_id: int,
    token_in: str,
    token_out: str,
    amount_in: float,
    gas_price: str = "medium",
    min_amount_out: float = 0.0,
) -> SwapQuote | str:
    """Compute a swap quote with full validation. Returns SwapQuote or error string."""
    token_in, token_out = token_in.upper(), token_out.upper()

    if token_in == token_out:
        return "Cannot swap a token for itself."

    # Token existence + network lookup
    all_tokens = await db.get_all_tokens_for_guild(guild_id)
    if token_in not in all_tokens and token_in != "USD":
        return f"Unknown token: {token_in}"
    if token_out not in all_tokens and token_out != "USD":
        return f"Unknown token: {token_out}"

    net_in = all_tokens.get(token_in, {}).get("network", "")
    net_out = all_tokens.get(token_out, {}).get("network", "")

    # SUN restriction - block direct SUN swaps unless pairing with a group token.
    # Group tokens on Sun Network use SUN as their AMM pairing asset, so that is valid.
    if "SUN" in (token_in, token_out):
        other = token_out if token_in == "SUN" else token_in
        if all_tokens.get(other, {}).get("token_type") != "group":
            return "SUN cannot be swapped. Use buy/sell for SUN instead."

    # Earn-only tokens: one-way out only. Mirrors the guard in cogs/trade.py
    # so API / agent-tool callers that invoke this service directly get the
    # same rejection instead of a "no pool for pair" leak.
    #
    # MOON is the canonical earn-only token. Swaps in either direction are
    # blocked unless the pair is on the explicit carve-out list:
    #
    #   * built-in mMTA / mSUN (Config.MOON_SWAPPABLE_TOKENS) -- bidirectional
    #   * a player-deployed token whose contract was minted with
    #     ``moon_swappable: True`` (set by ``token deploy``) -- bidirectional
    #   * a Moon Network group token -- one-way (MOON -> GROUP only) so the
    #     Lunar Mint stays the single legitimate path to mint MOON against
    #     a group token.
    #
    # LURE / REEL stay fully closed: they never appear in MOON_SWAPPABLE_TOKENS
    # and their contracts are never flagged, so the original firewall holds.
    def _is_moon_group_token(sym: str) -> bool:
        meta = all_tokens.get(sym, {})
        return (
            meta.get("token_type") == "group"
            and meta.get("network") == "Moon Network"
        )

    moon_pair_other = ""
    if "MOON" in (token_in, token_out):
        moon_pair_other = token_out if token_in == "MOON" else token_in
    moon_bidirectional = bool(moon_pair_other) and await is_moon_swappable_pair(
        db, guild_id, "MOON", moon_pair_other, all_tokens=all_tokens,
    )

    if token_out in Config.EARN_ONLY_TOKENS:
        if token_out == "MOON":
            if not moon_bidirectional:
                return (
                    "MOON cannot be acquired through this pair. Stake a group "
                    "token into the Lunar Mint to earn MOON "
                    "(.moon stake <GROUP> <amt>), or swap from mMTA / mSUN / a "
                    "moon-swappable deployed token."
                )
        else:
            return f"{token_out} is earn-only and cannot be acquired via swap."
    if (
        token_in in Config.EARN_ONLY_TOKENS
        and not _is_moon_group_token(token_out)
        and not moon_bidirectional
    ):
        if token_in == "MOON":
            return (
                "MOON can only be swapped OUT into a Moon Network group token "
                "(CAT, COOK, FEM, ...), mMTA, mSUN, or a moon-swappable "
                "deployed token. It cannot be swapped for USD, stablecoins, "
                "or unrelated network coins."
            )
        return f"{token_in} can only be swapped into a Moon Network group token."

    # Admin halts  -  canonical full-name → short-code map lives in core.framework.network.
    from core.framework.network import FULL_TO_SHORT as _NET_KEY
    for _sym, _net_name in ((token_in, net_in), (token_out, net_out)):
        if await db.is_token_disabled(guild_id, _sym):
            return f"{_sym} trading is currently disabled by an admin."
        _nk = _NET_KEY.get(_net_name, "")
        if _nk and await db.is_network_halted(guild_id, _nk):
            return f"The {_net_name} is currently halted by an admin."

    # Cross-network restriction with carve-outs. MOON pools are explicitly
    # cross-network for bridged pairs (a deployed token on Arcadia / Discoin
    # paired with MOON on Moon Network); the bidirectional carve-out has to
    # pass this gate the same way vault-pair pools do in cogs/trade.py.
    # Any pair with an explicitly-deployed pool (created via
    # ``trade pool create`` by a player at a job rank with
    # ``can_create_pool``) is also allowed -- the pool itself is the
    # authorization, so the swap transacts through it.
    pool_id, ca, cb = db.make_pool_id(token_in, token_out)
    pool = await db.get_pool(pool_id, guild_id)

    if (
        net_in and net_out and net_in != net_out
        and "USD" not in (token_in, token_out)
        and not moon_bidirectional
        and not pool
    ):
        return f"Cross-network swaps not supported. {token_in} is on {net_in}, {token_out} is on {net_out}."

    # Stablecoin restrictions
    stablecoins = set(Config.NETWORK_STABLECOIN.values())
    if (token_in in stablecoins and token_out == "USD") or (token_out in stablecoins and token_in == "USD"):
        return "Stablecoins can't be swapped directly with USD. Use buy/sell instead."
    if token_in in stablecoins and token_out in stablecoins:
        return "Swapping between stablecoins is not supported."

    if not pool:
        return f"No pool for {token_in}/{token_out} pair."

    if amount_in <= 0:
        return "Amount must be positive."

    # Pool reserves are stored as raw NUMERIC(36,0) scaled by 10**18; convert
    # to human-scale floats once here so the AMM math, TVL check, and max-in
    # check all operate in the same space.
    reserve_a_h = to_human(int(pool["reserve_a"]))
    reserve_b_h = to_human(int(pool["reserve_b"]))
    if token_in == ca:
        reserve_in, reserve_out = reserve_a_h, reserve_b_h
    else:
        reserve_in, reserve_out = reserve_b_h, reserve_a_h

    if reserve_in <= 0 or reserve_out <= 0:
        return "Pool has no liquidity."

    # Dynamic swap fraction
    price_a_row = await db.get_price(token_in, guild_id)
    price_b_row = await db.get_price(token_out, guild_id)
    p_in = float(price_a_row["price"]) if price_a_row else 0.0
    p_out = float(price_b_row["price"]) if price_b_row else 0.0
    pool_tvl = reserve_in * p_in + reserve_out * p_out
    if pool_tvl < Config.LOW_LIQUIDITY_THRESHOLD:
        effective_max_fraction = Config.LOW_LIQUIDITY_SWAP_FRACTION
    else:
        effective_max_fraction = Config.MAX_SWAP_FRACTION
    max_in = reserve_in * effective_max_fraction

    # Volume limit
    swap_usd_value = amount_in * p_in if p_in > 0 else amount_in
    allowed, remaining = check_user_swap_volume(user_id, guild_id, swap_usd_value)
    if not allowed:
        return f"Hourly swap volume limit reached. Remaining: ${remaining:,.2f}. Limit: ${Config.USER_SWAP_HOURLY_LIMIT_USD:,.0f}/hour."

    if amount_in > max_in:
        return f"Swap too large. Maximum is {max_in:,.6f} {token_in} ({effective_max_fraction*100:.0f}% of pool depth)."

    # AMM math
    # Apply the caller's Liqstone swap-fee discount. Scales with Liqstone level
    # via items_config.py ("swap_fee_discount" per level), capped so fee stays
    # above zero. Liqstones held by either party of the pool don't affect
    # anyone else's swap  -  the discount is strictly per-caller.
    _lq_discount = await liqstone_swap_fee_discount(db, user_id, guild_id)
    fee = apply_liqstone_discount(DEFAULT_SWAP_FEE, _lq_discount)
    amount_in_with_fee = amount_in * (1 - fee)
    amount_out = reserve_out * amount_in_with_fee / (reserve_in + amount_in_with_fee)
    spot_price = reserve_out / reserve_in
    exec_price = amount_out / amount_in if amount_in > 0 else 0.0
    price_impact = max(0.0, (spot_price - exec_price) / spot_price) if spot_price > 0 else 0.0

    # Slippage check
    if min_amount_out > 0 and amount_out < min_amount_out:
        return f"Slippage protection: output {amount_out:.6f} {token_out} is below minimum {min_amount_out:.6f} {token_out}."

    warnings = []
    if price_impact > SLIPPAGE_WARN:
        warnings.append(f"High price impact: {price_impact*100:.2f}%")

    # Mempool determination
    use_mempool = False
    gas_fee_val = 0.0
    gas_coin_val = ""
    gas_emoji_val = ""
    platform_fee_val = 0.0
    total_gas_cost_val = 0.0
    swap_network = ""
    net_short = ""

    if "USD" not in (token_in, token_out) and (net_in or net_out):
        swap_network = net_in or net_out
        net_short = NET_SHORT.get(swap_network, "")
        all_v = await db.get_pos_validators_for_network(guild_id, swap_network)
        active_validators = [v for v in all_v if v["is_active"]]
        has_pow_miners = False
        if swap_network == "Sun Network" and not active_validators:
            all_rigs = await db.get_all_guild_rigs(guild_id)
            has_pow_miners = any(r["quantity"] > 0 for r in all_rigs)
        if active_validators or has_pow_miners:
            use_mempool = True
            from cogs.validators import gas_fee_for_network
            gas_coin_val, gas_fee_val = await gas_fee_for_network(db, guild_id, "swap", gas_price, swap_network)
            gas_cfg = Config.TOKENS.get(gas_coin_val, {})
            gas_emoji_val = gas_cfg.get("emoji", "")
            # Platform fee = % of swap value in gas coin terms
            if gas_coin_val == token_out:
                _swap_val_gas = amount_out
            elif gas_coin_val == token_in:
                _swap_val_gas = amount_in
            else:
                _gc_row = await db.get_price(gas_coin_val, guild_id)
                _gc_price = float(_gc_row["price"]) if _gc_row else 0.0
                _swap_val_gas = (swap_usd_value / _gc_price) if _gc_price > 0 else amount_out
            platform_fee_val = _swap_val_gas * SWAP_PLATFORM_FEE_PCT
            total_gas_cost_val = gas_fee_val + platform_fee_val

            # Check gas balance (DB column is raw scaled int)
            if net_short:
                gas_h = await db.get_wallet_holding(user_id, guild_id, net_short, gas_coin_val)
            else:
                gas_h = await db.get_holding(user_id, guild_id, gas_coin_val)
            gas_balance_h = to_human(int(gas_h["amount"])) if gas_h else 0.0
            if gas_balance_h < total_gas_cost_val:
                return (
                    f"Insufficient gas. Need {total_gas_cost_val:.8f} {gas_coin_val}, "
                    f"have {gas_balance_h:.8f}."
                )

    return SwapQuote(
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        amount_out=amount_out,
        fee=fee,
        fee_amount=amount_in * fee,
        price_impact=price_impact,
        spot_price=spot_price,
        exec_price=exec_price,
        pool_id=pool_id,
        canon_a=ca,
        canon_b=cb,
        reserve_in=reserve_in,
        reserve_out=reserve_out,
        use_mempool=use_mempool,
        gas_fee=gas_fee_val,
        gas_coin=gas_coin_val,
        gas_emoji=gas_emoji_val,
        platform_fee=platform_fee_val,
        total_gas_cost=total_gas_cost_val,
        gas_price=gas_price,
        network=swap_network,
        net_short=net_short,
        min_amount_out=min_amount_out,
        swap_usd_value=swap_usd_value,
        warnings=warnings,
    )


async def execute_swap(db, guild_id: int, user_id: int, quote: SwapQuote) -> SwapResult:
    """Execute a pre-computed swap quote against the database."""
    reservation_id: int | None = None
    try:
        if quote.use_mempool:
            # Auto 2% slippage tolerance if not set
            min_out = quote.min_amount_out
            if min_out <= 0:
                min_out = quote.amount_out * 0.98

            net = quote.net_short
            # Raw scaled amounts for DB writes (balance columns are NUMERIC(36,0)
            # scaled by 10**18; raw-int math avoids IEEE-754 loss).
            amount_in_raw = to_raw(quote.amount_in)
            amount_out_raw = to_raw(quote.amount_out)
            total_gas_cost_raw = to_raw(quote.total_gas_cost)

            # Lock token_in from sender  -  check for combined amount when gas_coin == token_in
            if net:
                h = await db.get_wallet_holding(user_id, guild_id, net, quote.token_in)
            else:
                h = await db.get_holding(user_id, guild_id, quote.token_in)
            total_needed_raw = amount_in_raw
            total_needed_h = quote.amount_in
            if quote.gas_coin == quote.token_in:
                total_needed_raw += total_gas_cost_raw
                total_needed_h += quote.total_gas_cost
            if not h or int(h["amount"]) < total_needed_raw:
                return SwapResult(
                    success=False,
                    error=(
                        f"Insufficient {quote.token_in}. "
                        f"Need {total_needed_h:.6f} (swap + gas)."
                    ),
                )

            allowed, remaining, reservation_id = await reserve_user_swap_volume(
                user_id, guild_id, quote.swap_usd_value,
            )
            if not allowed:
                return SwapResult(
                    success=False,
                    error=(
                        f"Hourly swap volume limit reached. Remaining: ${remaining:,.2f}. "
                        f"Limit: ${Config.USER_SWAP_HOURLY_LIMIT_USD:,.0f}/hour."
                    ),
                )

            # Wrap debit + mempool insert in atomic transaction so tokens
            # are never lost if the mempool insert fails.
            async with db.atomic():
                if net:
                    await db.update_wallet_holding(user_id, guild_id, net, quote.token_in, -amount_in_raw)
                    await db.update_wallet_holding(user_id, guild_id, net, quote.gas_coin, -total_gas_cost_raw)
                else:
                    await db.update_holding(user_id, guild_id, quote.token_in, -amount_in_raw)
                    await db.update_holding(user_id, guild_id, quote.gas_coin, -total_gas_cost_raw)

                if quote.platform_fee > 0:
                    gas_usd_row = await db.get_price(quote.gas_coin, guild_id)
                    gas_usd = float(gas_usd_row["price"]) if gas_usd_row else 0.0
                    await db.split_to_community_reserves(
                        guild_id, "USD", to_raw(quote.platform_fee * gas_usd),
                    )

                action_id = await db.add_to_mempool(
                    guild_id=guild_id,
                    user_id=user_id,
                    network=quote.network,
                    action_type="swap",
                    payload={
                        "token_in": quote.token_in,
                        "token_out": quote.token_out,
                        "amount_in": quote.amount_in,
                        "pool_id": quote.pool_id,
                        "min_amount_out": min_out,
                    },
                    gas_price=quote.gas_price,
                    gas_fee=quote.total_gas_cost,
                )
            return SwapResult(success=True, mempool_id=action_id, amount_out=quote.amount_out)

        # ── INSTANT PATH (atomic  -  rollback on any failure) ────────────────
        net = quote.net_short

        # Fetch actual pool total_lp (C2 fix: was passing 0). total_lp is
        # stored as a raw scaled int (NUMERIC(36,0) * 10**18).
        pool_now = await db.get_pool(quote.pool_id, guild_id)
        actual_total_lp_raw = int(pool_now["total_lp"]) if pool_now else 0
        net_in_short = NET_SHORT.get(quote.network, "") if quote.network else ""

        # Raw scaled amounts for DB writes.
        amount_in_raw = to_raw(quote.amount_in)
        amount_out_raw = to_raw(quote.amount_out)
        fee_amount_raw = to_raw(quote.fee_amount)

        allowed, remaining, reservation_id = await reserve_user_swap_volume(
            user_id, guild_id, quote.swap_usd_value,
        )
        if not allowed:
            return SwapResult(
                success=False,
                error=(
                    f"Hourly swap volume limit reached. Remaining: ${remaining:,.2f}. "
                    f"Limit: ${Config.USER_SWAP_HOURLY_LIMIT_USD:,.0f}/hour."
                ),
            )

        async with db.atomic():
            # Debit input  -  compare raw-to-raw so there is no float loss.
            if quote.token_in == "USD":
                user = await db.get_user(user_id, guild_id)
                if not user or int(user["wallet"]) < amount_in_raw:
                    raise ValueError(f"Insufficient USD balance.")
                await db.update_wallet(user_id, guild_id, -amount_in_raw)
            elif net:
                h = await db.get_wallet_holding(user_id, guild_id, net, quote.token_in)
                if not h or int(h["amount"]) < amount_in_raw:
                    raise ValueError(f"Insufficient {quote.token_in} balance.")
                await db.update_wallet_holding(user_id, guild_id, net, quote.token_in, -amount_in_raw)
            else:
                h = await db.get_holding(user_id, guild_id, quote.token_in)
                if not h or int(h["amount"]) < amount_in_raw:
                    raise ValueError(f"Insufficient {quote.token_in} balance.")
                await db.update_holding(user_id, guild_id, quote.token_in, -amount_in_raw)

            # Credit output
            if quote.token_out == "USD":
                await db.update_wallet(user_id, guild_id, amount_out_raw)
            elif net:
                await db.update_wallet_holding(user_id, guild_id, net, quote.token_out, amount_out_raw)
            else:
                await db.update_holding(user_id, guild_id, quote.token_out, amount_out_raw)

            # Update pool reserves with fee burn (25% of fees permanently removed).
            # All reserve math stays in raw int space.
            reserve_in_raw = to_raw(quote.reserve_in)
            reserve_out_raw = to_raw(quote.reserve_out)
            new_in_raw = reserve_in_raw + amount_in_raw
            new_out_raw = reserve_out_raw - amount_out_raw
            burn_raw = 0
            if Config.FEE_BURN_FRACTION > 0:
                # Config.FEE_BURN_FRACTION is a float (e.g. 0.25). Stay in int
                # space by routing through to_raw on the computed fraction.
                burn_raw = to_raw(quote.fee_amount * Config.FEE_BURN_FRACTION)
                new_in_raw -= burn_raw  # burn from input side of pool
                # Track the burn in circulating supply so total accounting stays correct
                if quote.token_in in Config.TOKENS:
                    await db.update_builtin_circulating_supply(
                        guild_id, quote.token_in, -burn_raw,
                    )
                else:
                    await db.update_circulating_supply(
                        guild_id, quote.token_in, -burn_raw,
                    )

            # Sanity-check: constant-product k must not decrease after fee burn.
            # A decrease would mean the AMM is leaking value (misconfigured burn
            # fraction, rounding bug, etc.).  Log a warning but allow the swap to
            # proceed so users are not unexpectedly blocked.
            _k_before = reserve_in_raw * reserve_out_raw
            _k_after = new_in_raw * new_out_raw
            if _k_before > 0 and _k_after < _k_before * (1 - 1e-6):
                log.warning(
                    "AMM k-invariant violated: k_before=%d k_after=%d pool=%s "
                    "burn_frac=%.4f  -  investigate FEE_BURN_FRACTION config.",
                    _k_before, _k_after, quote.pool_id, Config.FEE_BURN_FRACTION,
                )

            if quote.token_in == quote.canon_a:
                await db.update_pool_reserves(
                    quote.pool_id, guild_id, new_in_raw, new_out_raw, actual_total_lp_raw,
                )
            else:
                await db.update_pool_reserves(
                    quote.pool_id, guild_id, new_out_raw, new_in_raw, actual_total_lp_raw,
                )

            # Bump the pool's rolling recent-volume counter so the LP
            # yield tick can decay the bootstrap incentive as trades
            # actually flow through the pool. Best-effort: a failure here
            # cannot roll back the swap (the user already got their
            # token_out), so we log and move on.
            try:
                _swap_usd = float(getattr(quote, "swap_usd_value", 0.0) or 0.0)
                if _swap_usd > 0:
                    await db.execute(
                        "UPDATE pools SET recent_volume_usd_raw "
                        "= recent_volume_usd_raw + $1::numeric "
                        "WHERE pool_id=$2 AND guild_id=$3",
                        to_raw(_swap_usd), quote.pool_id, guild_id,
                    )
            except Exception:
                log.debug(
                    "swap: bootstrap volume bump failed pool=%s",
                    quote.pool_id, exc_info=True,
                )

            # Job fee rebate  -  inside atomic transaction so it either succeeds
            # with the swap or rolls back entirely (was previously outside,
            # risking a failed rebate after a successful swap).
            rebate = 0.0
            try:
                job = await db.get_user_job(user_id, guild_id)
                job_cfg = Config.JOBS.get(job["job_id"], {})
                rebate_rate = job_cfg.get("perks", {}).get("swap_fee", 0.0)
                if rebate_rate > 0:
                    rebate = quote.amount_in * quote.fee * rebate_rate
                    rebate_raw = to_raw(rebate)
                    if quote.token_in == "USD":
                        await db.update_wallet(user_id, guild_id, rebate_raw)
                    elif net:
                        await db.update_wallet_holding(user_id, guild_id, net, quote.token_in, rebate_raw)
                    else:
                        await db.update_holding(user_id, guild_id, quote.token_in, rebate_raw)
            except Exception as _rebate_exc:
                import logging as _log
                _log.getLogger("discoin.swap").warning(
                    "Fee rebate credit failed for user=%s guild=%s: %s  -  swap proceeds without rebate.",
                    user_id, guild_id, _rebate_exc,
                )
                rebate = 0.0  # fail closed  -  swap already executed

            # Log transaction inside the same atomic block so accounting and
            # ledger state cannot diverge. ``log_tx`` already enforces raw ints.
            tx_hash = await db.log_tx(
                guild_id, user_id, "SWAP",
                symbol_in=quote.token_in, amount_in=amount_in_raw,
                symbol_out=quote.token_out, amount_out=amount_out_raw,
                price_at=quote.exec_price,
                network=net_in_short,
                gas_fee=to_raw(quote.gas_fee) if quote.gas_fee > 0 else 0,
                gas_coin=quote.gas_coin if quote.gas_fee > 0 else "",
            )

        # Nudge the oracle + chart so swap activity (slippage / impact) shows
        # up the same way .buy and .sell do. Outside the atomic block: a
        # failed price update must not roll back the swap itself.
        await apply_swap_oracle_nudge(
            db, guild_id,
            quote.token_in, quote.token_out,
            quote.price_impact, quote.swap_usd_value,
        )

        return SwapResult(success=True, tx_hash=tx_hash, amount_out=quote.amount_out, rebate=rebate)

    except Exception as e:
        if reservation_id is not None:
            cancel_user_swap_reservation(user_id, guild_id, reservation_id)
        return SwapResult(success=False, error=str(e))
