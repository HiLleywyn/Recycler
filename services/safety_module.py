"""Safety Module service layer -- VTR/DSY staking, unstake cooldown, and yield distribution.

Mirrors Vantor V2 Safety Module mechanics:
  - Stake VTR -> earn USDC yield from protocol fees
  - Stake DSY  -> earn DSD yield  from protocol fees
  - 24h unstake cooldown before withdrawal is permitted
  - 10% of staked amount burned during a shortfall (market crisis) event

APY is emission-based and variable: a fixed USD pool is emitted per day to all
stakers, so early stakers with low TVL can earn up to 10,000% APY while yield
compresses as total staked grows -- identical to Cetus-style DeFi staking.

Auto-compound: when a position has auto_compound=True, the hourly staking_tick
re-stakes accrued yield back into the position (increasing the staked amount)
instead of paying it out to the DeFi wallet. Manual ,claim also respects the
flag: compound positions receive more stake, wallet positions receive yield token.

No Discord dependencies -- all logic is pure service/DB layer.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from core.config import Config
from core.framework.scale import to_human, to_raw

_SM = Config.SAFETY_MODULE


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class SMResult:
    success: bool
    amount: float = 0.0
    symbol: str = ""
    error: str = ""


# ── Rate helpers ─────────────────────────────────────────────────────────────

def sm_current_daily_rate(total_staked_usd: float, cfg: dict) -> float:
    """Effective daily yield rate given current total staked USD.

    Formula: emission_usd_per_day / total_staked_usd, capped at
    max_apy_pct / 365 / 100 and floored at min_apy_pct / 365 / 100.
    When TVL is near-zero the max cap kicks in; as TVL grows the rate
    compresses but never drops below the min floor.
    """
    emission  = float(cfg.get("emission_usd_per_day", 50000.0))
    max_daily = float(cfg.get("max_apy_pct", 10000.0)) / 365.0 / 100.0
    min_daily = float(cfg.get("min_apy_pct", 50.0))   / 365.0 / 100.0
    if total_staked_usd <= 0:
        return max_daily
    return max(min(emission / total_staked_usd, max_daily), min_daily)


def sm_current_apy_pct(total_staked_usd: float, cfg: dict) -> float:
    """Current effective APY percentage given total staked USD."""
    return sm_current_daily_rate(total_staked_usd, cfg) * 365.0 * 100.0


# ── Helpers ──────────────────────────────────────────────────────────────────

def sm_cfg(symbol: str) -> dict:
    """Return the SAFETY_MODULE config block for the given symbol (VTR or DSY)."""
    return _SM[symbol.upper()]


def cooldown_remaining(row: dict) -> float:
    """Seconds remaining on unstake cooldown; 0.0 if no cooldown or already expired."""
    if not row or not row.get("cooldown_at"):
        return 0.0
    cd = row["cooldown_at"]
    ts = cd.timestamp() if hasattr(cd, "timestamp") else float(cd)
    cfg = _SM.get(row["symbol"].upper(), {})
    elapsed = time.time() - ts
    remaining = cfg.get("cooldown_secs", 86400) - elapsed
    return max(remaining, 0.0)


# ── Stake ─────────────────────────────────────────────────────────────────────

async def stake_sm(
    db,
    guild_id: int,
    user_id: int,
    symbol: str,
    amount: float,
    *,
    amount_raw: int | None = None,
) -> SMResult:
    """Move `amount` of VTR/DSY from the user's DeFi wallet into the Safety Module.

    Pass ``amount_raw`` to bypass the human-float -> raw conversion when the
    caller already has the canonical raw integer (e.g. the ``all``/``max``
    path in ``cogs/stake._sm_deposit``). The float roundtrip
    ``raw -> to_human -> to_raw`` can drift by a handful of raw units for
    balances whose human value isn't an exact float, which manifested as
    "have 100 VTR, need 100 VTR" off-by-1s on full-balance stakes."""
    symbol = symbol.upper()
    if symbol not in _SM:
        return SMResult(success=False, symbol=symbol, error=f"{symbol} is not a Safety Module token.")

    cfg = _SM[symbol]
    net = cfg["network"]

    if amount < cfg["min_stake"]:
        return SMResult(
            success=False, symbol=symbol,
            error=f"Minimum stake is {cfg['min_stake']:g} {symbol}.",
        )

    if amount_raw is None:
        amount_raw = to_raw(amount)
    else:
        amount_raw = int(amount_raw)
        amount = to_human(amount_raw)

    has_wallet = await db.has_defi_wallet(user_id, guild_id, net)
    if not has_wallet:
        network_full = "Arcadia Network" if net == "arc" else "Discoin Network"
        return SMResult(
            success=False, symbol=symbol,
            error=f"You need a DeFi wallet on {network_full} to stake {symbol}.",
        )

    holding = await db.get_wallet_holding(user_id, guild_id, net, symbol)
    bal_raw = int(holding["amount"]) if holding else 0
    if bal_raw < amount_raw:
        return SMResult(
            success=False, symbol=symbol,
            error=f"Insufficient {symbol} balance (have {to_human(bal_raw):,.4f}, need {amount:,.4f}).",
        )

    existing = await db.get_sm_stake(user_id, guild_id, symbol)

    async with db.atomic():
        await db.update_wallet_holding(user_id, guild_id, net, symbol, -amount_raw)
        new_amount = amount_raw + (int(existing["amount"]) if existing else 0)
        await db.upsert_sm_stake(
            user_id, guild_id, symbol,
            amount=new_amount,
            last_yield=time.time(),
            staked_at=time.time() if not existing else None,
            cooldown_at=None,
        )

    return SMResult(success=True, symbol=symbol, amount=amount)


# ── Start unstake cooldown ───────────────────────────────────────────────────

async def begin_unstake(
    db,
    guild_id: int,
    user_id: int,
    symbol: str,
) -> SMResult:
    """Start the unstake cooldown. User must call withdraw_sm after cooldown_secs."""
    symbol = symbol.upper()
    row = await db.get_sm_stake(user_id, guild_id, symbol)
    if not row or int(row["amount"]) == 0:
        return SMResult(success=False, symbol=symbol, error=f"You have no {symbol} staked.")

    if row.get("cooldown_at"):
        remaining = cooldown_remaining(row)
        if remaining > 0:
            hrs = remaining / 3600
            return SMResult(
                success=False, symbol=symbol,
                error=f"Cooldown already active -- {hrs:.1f}h remaining.",
            )

    await db.upsert_sm_stake(
        user_id, guild_id, symbol,
        amount=int(row["amount"]),
        last_yield=row["last_yield"],
        cooldown_at=time.time(),
    )
    return SMResult(success=True, symbol=symbol, amount=to_human(int(row["amount"])))


# ── Withdraw after cooldown ──────────────────────────────────────────────────

async def withdraw_sm(
    db,
    guild_id: int,
    user_id: int,
    symbol: str,
) -> SMResult:
    """Return staked tokens to the DeFi wallet after the cooldown has elapsed."""
    symbol = symbol.upper()
    cfg = _SM[symbol]
    net = cfg["network"]

    row = await db.get_sm_stake(user_id, guild_id, symbol)
    if not row or int(row["amount"]) == 0:
        return SMResult(success=False, symbol=symbol, error=f"You have no {symbol} staked.")

    if not row.get("cooldown_at"):
        return SMResult(
            success=False, symbol=symbol,
            error=f"Start the unstake cooldown first with `,{symbol.lower()} unstake`.",
        )

    remaining = cooldown_remaining(row)
    if remaining > 0:
        hrs = remaining / 3600
        return SMResult(
            success=False, symbol=symbol,
            error=f"Cooldown not done yet -- {hrs:.1f}h remaining.",
        )

    amount_raw = int(row["amount"])

    async with db.atomic():
        await db.update_wallet_holding(user_id, guild_id, net, symbol, amount_raw)
        await db.delete_sm_stake(user_id, guild_id, symbol)

    return SMResult(success=True, symbol=symbol, amount=to_human(amount_raw))


# ── Yield tick ───────────────────────────────────────────────────────────────

async def apply_sm_yield(
    db,
    guild_id: int,
    symbol: str,
    *,
    user_id: int | None = None,
    auto_compound_only: bool = False,
) -> list[dict]:
    """Distribute yield for all (or one) Safety Module stakers for `symbol`.

    ``user_id``           -- when set, only processes that single staker
                            (used by the manual ,claim command).
    ``auto_compound_only`` -- when True, skips positions that have
                            auto_compound=False (used by the hourly tick so
                            non-compound stakers aren't paid without asking).

    Returns a list of result dicts with keys:
      - user_id, auto_compound (bool)
      - yield_amount_raw + yield_token  (auto_compound=False -- wallet pay)
      - compounded_raw   + yield_token  (auto_compound=True  -- restake)
    """
    cfg        = _SM[symbol.upper()]
    net        = cfg["network"]
    yield_token = cfg["yield_token"]

    rows = await db.get_all_sm_stakes(guild_id, symbol)
    results: list[dict] = []
    now = time.time()

    if not rows:
        return results

    # Fetch prices once -- all stakers share the same oracle for this round.
    price_row = await db.fetch_one(
        "SELECT price FROM crypto_prices WHERE guild_id=$1 AND symbol=$2",
        guild_id, symbol.upper(),
    )
    if not price_row:
        return results
    token_price_usd = float(price_row["price"])
    if token_price_usd <= 0:
        return results

    if yield_token in ("USDC", "DSD", "USD"):
        yield_price_usd = 1.0
    else:
        yp_row = await db.fetch_one(
            "SELECT price FROM crypto_prices WHERE guild_id=$1 AND symbol=$2",
            guild_id, yield_token,
        )
        yield_price_usd = float(yp_row["price"]) if yp_row else 1.0

    # Compute the dynamic daily rate from total active TVL.
    active_staked_raw = sum(
        int(r["amount"]) for r in rows
        if not r.get("cooldown_at") and int(r.get("amount", 0)) > 0
    )
    total_staked_usd = to_human(active_staked_raw) * token_price_usd
    daily_rate = sm_current_daily_rate(total_staked_usd, cfg)

    for row in rows:
        if user_id is not None and row["user_id"] != user_id:
            continue
        if row.get("cooldown_at"):
            continue  # yield paused during cooldown

        is_auto = bool(row.get("auto_compound", False))
        if auto_compound_only and not is_auto:
            continue

        ly = row["last_yield"]
        last_yield_ts = ly.timestamp() if hasattr(ly, "timestamp") else float(ly)
        elapsed_days = (now - last_yield_ts) / 86400.0
        if elapsed_days <= 0:
            continue

        staked_raw = int(row["amount"])
        staked_usd = to_human(staked_raw) * token_price_usd
        yield_usd  = staked_usd * daily_rate * elapsed_days
        if yield_usd <= 0:
            continue

        if is_auto:
            # Re-stake yield as more staked token.
            compound_h   = yield_usd / token_price_usd
            compound_raw = to_raw(compound_h)
            if compound_raw <= 0:
                continue
            async with db.atomic():
                await db.upsert_sm_stake(
                    row["user_id"], guild_id, symbol.upper(),
                    amount=staked_raw + compound_raw,
                    last_yield=now,
                    cooldown_at=row.get("cooldown_at"),
                )
            results.append({
                "user_id":        row["user_id"],
                "compounded_raw": compound_raw,
                "yield_token":    symbol.upper(),
                "auto_compound":  True,
            })
        else:
            # Pay yield in yield_token to the DeFi wallet.
            yield_amount_h   = yield_usd / yield_price_usd if yield_price_usd > 0 else 0.0
            yield_raw        = to_raw(yield_amount_h)
            if yield_raw <= 0:
                continue
            async with db.atomic():
                await db.update_wallet_holding(
                    row["user_id"], guild_id, net, yield_token, yield_raw,
                )
                await db.upsert_sm_stake(
                    row["user_id"], guild_id, symbol.upper(),
                    amount=staked_raw,
                    last_yield=now,
                    cooldown_at=row.get("cooldown_at"),
                )
            results.append({
                "user_id":          row["user_id"],
                "yield_amount_raw": yield_raw,
                "yield_token":      yield_token,
                "auto_compound":    False,
            })

    return results


# ── Shortfall slash (called by market event engine) ──────────────────────────

async def apply_shortfall_slash(
    db,
    guild_id: int,
    symbol: str,
) -> dict:
    """Slash cfg['slash_rate'] of all SM stakes for `symbol`.

    Returns {slashed_count, total_burned_raw}.
    """
    cfg = _SM[symbol.upper()]
    slash_rate = cfg["slash_rate"]

    rows = await db.get_all_sm_stakes(guild_id, symbol)
    total_burned = 0
    slashed_count = 0

    for row in rows:
        staked_raw = int(row["amount"])
        if staked_raw == 0:
            continue
        burn_raw = int(staked_raw * slash_rate)
        new_raw = staked_raw - burn_raw
        await db.upsert_sm_stake(
            row["user_id"], guild_id, symbol.upper(),
            amount=new_raw,
            last_yield=row["last_yield"],
            cooldown_at=row.get("cooldown_at"),
        )
        total_burned += burn_raw
        slashed_count += 1

    return {"slashed_count": slashed_count, "total_burned_raw": total_burned}
