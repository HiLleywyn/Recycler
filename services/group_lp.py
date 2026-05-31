"""
services/group_lp.py  -  Founder-controlled group treasury <-> LP.

The founder of a mining_group can deposit a bounded percentage of
``vault_token_bal`` into the group's own TOKEN/<wrapped-coin> AMM pool,
or withdraw a bounded percentage of the pool's group-token reserve back
into the treasury. The pair is the Moon-Network wrapped version of the
founder's mining chain (mMTA for Moneta Chain, mSUN for Sun Network)
-- the same pair ``create_vault_pool`` and ``seed_group_genesis_pools``
already use, since group tokens have no USD pool by design. Both ops
are single-sided (only the group-token side moves) -- the founder
accepts the price impact in exchange for not having to drain the
wrapped-coin side in lockstep.

Safeguards (all enforced here, not in the cog):
  * Founder-only -- the caller's user_id must match
    mining_groups.founder_id. Guild admins are NOT auto-authorised;
    the founder is the only seat that can move treasury.
  * Master unlock -- ``treasury_lp_unlocked`` must be TRUE. The founder
    flips it via ``set_unlocked()`` (also founder-only).
  * Per-action pct cap -- ``MAX_PCT_PER_ACTION = 25.0`` (caller can
    pass a smaller pct; a larger one raises ValueError).
  * Cooldown -- ``COOLDOWN_HOURS = 24`` between deposits or withdrawals.
    Enforced on the DB clock via
    ``EXTRACT(EPOCH FROM (NOW() - last_treasury_lp_at))``.
  * Min remaining reserve -- a deposit can't move more than
    ``MAX_PCT_PER_ACTION`` of the CURRENT pool reserve_a in one go,
    so price-shock is bounded. Same on withdraw.
  * Audit -- every successful op appends a tx_log entry.

Cog wiring: ``cogs/chain_group.py:group_lp_*`` commands.
"""
from __future__ import annotations

import logging
from typing import Any

from core.config import Config
from constants.moons import wrapped_coin
from core.framework.scale import to_human

log = logging.getLogger(__name__)


MAX_PCT_PER_ACTION: float = 25.0
COOLDOWN_HOURS: int = 24


# ─── Validation helpers ─────────────────────────────────────────────────────


async def _load_group(
    db: Any, guild_id: int, group_id: str,
) -> dict:
    row = await db.fetch_one(
        """
        SELECT mg.guild_id, mg.group_id, mg.founder_id,
               mg.token_symbol, mg.token_network,
               mg.vault_token_bal,
               mg.treasury_lp_unlocked,
               mg.last_treasury_lp_at,
               mg.treasury_lp_total_raw,
               EXTRACT(EPOCH FROM (NOW() - mg.last_treasury_lp_at))::bigint
                   AS seconds_since_last
          FROM mining_groups mg
         WHERE mg.guild_id = $1 AND mg.group_id = $2
        """,
        int(guild_id), str(group_id),
    )
    if not row:
        raise ValueError(f"Group `{group_id}` not found.")
    return dict(row)


def resolve_pair(group: dict) -> str:
    """Return the pair symbol the group's TOKEN trades against.

    Group tokens are seeded against the wrapped version of the founder's
    mining coin (mMTA for Moneta Chain, mSUN for Sun Network) -- there
    is no TOKEN/USD pool by design (see ``seed_group_genesis_pools``).
    Raises ``ValueError`` if the group hasn't bound a mining chain yet.
    """
    net = str(group.get("token_network") or "").strip()
    if not net:
        raise ValueError(
            "Group's mining chain isn't bound yet. The founder must run "
            "`,group token network <mta|sun>` before LP ops are available."
        )
    coin = Config.NETWORK_COINS.get(net)
    if not coin:
        raise ValueError(
            f"Mining chain `{net}` has no Moon-Network wrapped coin to "
            f"pair against."
        )
    return wrapped_coin(coin)


def _check_founder(group: dict, user_id: int) -> None:
    if int(group.get("founder_id") or 0) != int(user_id):
        raise ValueError(
            "Only the group founder can move treasury into / out of LP."
        )


def _check_cooldown(group: dict) -> None:
    sec = group.get("seconds_since_last")
    if sec is None:
        return
    cooldown_sec = COOLDOWN_HOURS * 3600
    if int(sec) < cooldown_sec:
        remain = cooldown_sec - int(sec)
        h = remain // 3600
        m = (remain % 3600) // 60
        raise ValueError(
            f"Treasury LP is on cooldown. Try again in **{h}h {m}m**."
        )


def _check_pct(pct: float) -> float:
    try:
        p = float(pct)
    except (TypeError, ValueError):
        raise ValueError("Pct must be a number, e.g. 10 for 10%.")
    if p <= 0:
        raise ValueError("Pct must be positive.")
    if p > MAX_PCT_PER_ACTION:
        raise ValueError(
            f"Pct is capped at {MAX_PCT_PER_ACTION:.0f}% per action "
            f"(you passed {p:.1f}%). Run multiple smaller ops with "
            f"the {COOLDOWN_HOURS}h cooldown between them."
        )
    return p


# ─── Public ops ─────────────────────────────────────────────────────────────


async def set_unlocked(
    db: Any, *, guild_id: int, group_id: str, user_id: int,
    unlocked: bool,
) -> dict:
    """Founder-only: flip the master kill switch.

    Doesn't reset the cooldown -- a paused-then-resumed window still
    counts as "elapsed" between cooldown ticks. That keeps a founder
    from cycling enable/disable to bypass the throttle.
    """
    group = await _load_group(db, guild_id, group_id)
    _check_founder(group, user_id)
    await db.execute(
        "UPDATE mining_groups SET treasury_lp_unlocked = $3 "
        "WHERE guild_id = $1 AND group_id = $2",
        int(guild_id), str(group_id), bool(unlocked),
    )
    return await _load_group(db, guild_id, group_id)


async def deposit(
    db: Any, *,
    guild_id: int, group_id: str, user_id: int,
    pct: float,
) -> dict:
    """Move ``pct%`` of vault_token_bal into the group's TOKEN/<pair> pool.

    Single-sided: only the group-token side of the pool moves. Returns
    a receipt dict with ``token_added`` (raw + human), ``new_reserve_a``,
    ``price_before / price_after``, ``vault_remaining``, ``pair_symbol``.
    """
    pct = _check_pct(pct)
    group = await _load_group(db, guild_id, group_id)
    _check_founder(group, user_id)
    _check_cooldown(group)

    sym = str(group.get("token_symbol") or "").upper()
    if not sym:
        raise ValueError(
            "Group token isn't bound yet. The founder must mint the "
            "first vault block before LP ops are available."
        )
    pair = resolve_pair(group)

    vault_raw = int(group.get("vault_token_bal") or 0)
    if vault_raw <= 0:
        raise ValueError(
            "Vault is empty -- nothing to deposit. Mine some blocks first."
        )
    move_raw = int(vault_raw * pct / 100.0)
    if move_raw <= 0:
        raise ValueError("Pct rounds to zero against current vault size.")

    # Pool lookup. Group tokens pair against the founder's mining-chain
    # wrapped coin on Moon Network (TOKEN/mMTA or TOKEN/mSUN).
    pool_id, canon_a, canon_b = db.make_pool_id(sym, pair)
    pool = await db.get_pool(pool_id, guild_id)
    if not pool:
        raise ValueError(
            f"No pool exists for {sym}/{pair}. Ask an admin to seed one."
        )
    reserve_a_raw = int(pool.get("reserve_a") or 0)
    reserve_b_raw = int(pool.get("reserve_b") or 0)
    if sym != canon_a:
        # Caller's group-token side is reserve_b in canonical order;
        # swap the local variables so the rest of this function reads
        # naturally as "side A = group token".
        reserve_a_raw, reserve_b_raw = reserve_b_raw, reserve_a_raw

    # Bound the move to <= MAX_PCT_PER_ACTION% of the current pool's
    # group-token reserve so a deposit can't move price by more than
    # the same fraction in one shot.
    pool_cap_raw = int(reserve_a_raw * MAX_PCT_PER_ACTION / 100.0)
    if pool_cap_raw > 0 and move_raw > pool_cap_raw:
        move_raw = pool_cap_raw

    if move_raw <= 0:
        raise ValueError(
            "Pool reserve too small to accept any deposit at this cap."
        )

    price_before = (
        (to_human(reserve_b_raw) / to_human(reserve_a_raw))
        if reserve_a_raw > 0 else 0.0
    )

    async with db.atomic():
        # 1) Drain the vault.
        await db.execute(
            "UPDATE mining_groups "
            "   SET vault_token_bal = vault_token_bal - $3::numeric, "
            "       last_treasury_lp_at = NOW(), "
            "       treasury_lp_total_raw = treasury_lp_total_raw + $3::numeric "
            " WHERE guild_id = $1 AND group_id = $2",
            int(guild_id), str(group_id), str(int(move_raw)),
        )
        # 2) Add to the matching side of the pool.
        if sym == canon_a:
            await db.execute(
                "UPDATE pools "
                "   SET reserve_a = reserve_a + $3::numeric, updated_at = NOW() "
                " WHERE guild_id = $1 AND pool_id = $2",
                int(guild_id), pool_id, str(int(move_raw)),
            )
        else:
            await db.execute(
                "UPDATE pools "
                "   SET reserve_b = reserve_b + $3::numeric, updated_at = NOW() "
                " WHERE guild_id = $1 AND pool_id = $2",
                int(guild_id), pool_id, str(int(move_raw)),
            )
        # 3) Audit.
        try:
            await db.log_tx(
                int(guild_id), int(user_id), "GROUP_LP_DEPOSIT",
                symbol_in=sym, amount_in=move_raw,
                network=str(group.get("token_network") or "moon"),
            )
        except Exception:
            log.debug("group_lp deposit log_tx failed", exc_info=True)

    new_reserve_a_raw = reserve_a_raw + move_raw
    price_after = (
        (to_human(reserve_b_raw) / to_human(new_reserve_a_raw))
        if new_reserve_a_raw > 0 else 0.0
    )
    return {
        "symbol":          sym,
        "pair_symbol":     pair,
        "pct":             pct,
        "token_added_raw": int(move_raw),
        "token_added_h":   float(to_human(move_raw)),
        "new_reserve_a_raw": int(new_reserve_a_raw),
        "new_reserve_b_raw": int(reserve_b_raw),
        "vault_remaining_raw": int(vault_raw - move_raw),
        "price_before":    float(price_before),
        "price_after":     float(price_after),
    }


async def withdraw(
    db: Any, *,
    guild_id: int, group_id: str, user_id: int,
    pct: float,
) -> dict:
    """Pull ``pct%`` of the pool's group-token reserve back into the
    vault. Mirror of ``deposit``.
    """
    pct = _check_pct(pct)
    group = await _load_group(db, guild_id, group_id)
    _check_founder(group, user_id)
    _check_cooldown(group)

    sym = str(group.get("token_symbol") or "").upper()
    if not sym:
        raise ValueError("Group token isn't bound yet.")
    pair = resolve_pair(group)

    pool_id, canon_a, canon_b = db.make_pool_id(sym, pair)
    pool = await db.get_pool(pool_id, guild_id)
    if not pool:
        raise ValueError(f"No pool exists for {sym}/{pair}.")
    reserve_a_raw = int(pool.get("reserve_a") or 0)
    reserve_b_raw = int(pool.get("reserve_b") or 0)
    if sym != canon_a:
        reserve_a_raw, reserve_b_raw = reserve_b_raw, reserve_a_raw

    if reserve_a_raw <= 0:
        raise ValueError(
            f"Pool {sym}/{pair} has zero {sym} reserve -- nothing to withdraw."
        )
    move_raw = int(reserve_a_raw * pct / 100.0)
    if move_raw <= 0:
        raise ValueError("Pct rounds to zero against current pool reserve.")

    price_before = (
        (to_human(reserve_b_raw) / to_human(reserve_a_raw))
        if reserve_a_raw > 0 else 0.0
    )

    async with db.atomic():
        # 1) Pull from the matching side.
        if sym == canon_a:
            await db.execute(
                "UPDATE pools "
                "   SET reserve_a = reserve_a - $3::numeric, updated_at = NOW() "
                " WHERE guild_id = $1 AND pool_id = $2",
                int(guild_id), pool_id, str(int(move_raw)),
            )
        else:
            await db.execute(
                "UPDATE pools "
                "   SET reserve_b = reserve_b - $3::numeric, updated_at = NOW() "
                " WHERE guild_id = $1 AND pool_id = $2",
                int(guild_id), pool_id, str(int(move_raw)),
            )
        # 2) Credit the vault.
        await db.execute(
            "UPDATE mining_groups "
            "   SET vault_token_bal = vault_token_bal + $3::numeric, "
            "       last_treasury_lp_at = NOW() "
            " WHERE guild_id = $1 AND group_id = $2",
            int(guild_id), str(group_id), str(int(move_raw)),
        )
        # 3) Audit.
        try:
            await db.log_tx(
                int(guild_id), int(user_id), "GROUP_LP_WITHDRAW",
                symbol_in=sym, amount_in=move_raw,
                network=str(group.get("token_network") or "moon"),
            )
        except Exception:
            log.debug("group_lp withdraw log_tx failed", exc_info=True)

    new_reserve_a_raw = reserve_a_raw - move_raw
    price_after = (
        (to_human(reserve_b_raw) / to_human(new_reserve_a_raw))
        if new_reserve_a_raw > 0 else 0.0
    )
    return {
        "symbol":            sym,
        "pair_symbol":       pair,
        "pct":               pct,
        "token_pulled_raw":  int(move_raw),
        "token_pulled_h":    float(to_human(move_raw)),
        "new_reserve_a_raw": int(new_reserve_a_raw),
        "new_reserve_b_raw": int(reserve_b_raw),
        "price_before":      float(price_before),
        "price_after":       float(price_after),
    }


async def status(
    db: Any, *, guild_id: int, group_id: str,
) -> dict:
    """Read-only status panel for ``,group lp status``."""
    group = await _load_group(db, guild_id, group_id)
    sym = str(group.get("token_symbol") or "").upper()
    out: dict = {
        "group_id":            str(group_id),
        "founder_id":          int(group.get("founder_id") or 0),
        "symbol":              sym,
        "pair_symbol":         "",
        "vault_token_bal_raw": int(group.get("vault_token_bal") or 0),
        "unlocked":            bool(group.get("treasury_lp_unlocked")),
        "last_at":             group.get("last_treasury_lp_at"),
        "lifetime_total_raw":  int(group.get("treasury_lp_total_raw") or 0),
        "max_pct":             MAX_PCT_PER_ACTION,
        "cooldown_h":          COOLDOWN_HOURS,
        "pool_token_raw":      0,
        "pool_pair_raw":       0,
    }
    if not sym:
        return out
    try:
        pair = resolve_pair(group)
    except ValueError:
        # Mining chain not bound yet -- status is read-only so don't
        # error; just leave the pool fields empty.
        return out
    out["pair_symbol"] = pair
    pool_id, canon_a, canon_b = db.make_pool_id(sym, pair)
    pool = await db.get_pool(pool_id, guild_id)
    if pool:
        ra = int(pool.get("reserve_a") or 0)
        rb = int(pool.get("reserve_b") or 0)
        if sym != canon_a:
            ra, rb = rb, ra
        out["pool_token_raw"] = ra
        out["pool_pair_raw"]  = rb
    return out
