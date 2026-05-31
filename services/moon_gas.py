"""
services/moon_gas.py  -  MOON Network gas.

Single source of truth for the per-action MOON fee charged on every Moon
Network interaction (unwrap, swap, stake, unstake, claim, pool add/remove,
burn). ``wrap`` is intentionally free -- it is the network on-ramp.

Every charged fee is split: MOON_GAS_BURN_PCT is burned (the wallet debit
itself removes it from circulating supply), and the remainder is converted
to USD at the MOON oracle and added to the Moon Network vault's
distributable bucket, where it is dripped to Moon Pool stakers as yield.

Cogs charge gas inside the action's ``db.atomic()`` block so a failed
action never leaves a fee charged, and surface ``gas_field()`` on the
result embed so the player always sees what gas cost and what was burned.
"""
from __future__ import annotations

from dataclasses import dataclass

from constants.moons import (
    MOON_GAS_BURN_PCT,
    MOON_GAS_COSTS,
    MOON_GAS_DEFAULT,
    MOON_NETWORK_SHORT,
    MOON_SYMBOL,
)
from core.framework.scale import to_human, to_raw

_MOON_EMOJI = "\U0001F315"  # full moon


@dataclass
class GasResult:
    """Outcome of a gas charge. ``ok`` is False only when the player could
    not afford the fee (nothing was debited in that case)."""
    action: str
    cost: float        # MOON charged (human units)
    burned: float      # MOON destroyed (human units)
    to_vault: float    # MOON routed to the vault as yield (human units)
    charged: bool      # True when a non-zero fee was actually debited
    ok: bool = True

    @property
    def free(self) -> bool:
        return self.cost <= 0.0


def gas_cost(action: str) -> float:
    """Flat MOON gas cost for an action name (human units). 0 == free."""
    return MOON_GAS_COSTS.get(action, MOON_GAS_DEFAULT)


async def moon_balance(db, guild_id: int, user_id: int) -> float:
    """The user's liquid MOON balance (human units)."""
    holding = await db.get_wallet_holding(
        user_id, guild_id, MOON_NETWORK_SHORT, MOON_SYMBOL,
    )
    return to_human(int(holding["amount"])) if holding else 0.0


async def can_afford_gas(db, guild_id: int, user_id: int, action: str) -> bool:
    """Pre-check used before the atomic block so the cog can show a clean
    'you need N MOON for gas' error instead of rolling a transaction back."""
    cost = gas_cost(action)
    if cost <= 0.0:
        return True
    return await moon_balance(db, guild_id, user_id) >= cost


async def charge_gas(db, guild_id: int, user_id: int, action: str) -> GasResult:
    """Charge the MOON gas fee for ``action``.

    Debits the user's MOON wallet (which also removes the fee from MOON
    circulating supply), then routes the vault share to the Moon Network
    distributable bucket. Call inside the action's ``db.atomic()`` block.

    Returns ``ok=False`` WITHOUT debiting when the fee is non-zero and the
    player cannot cover it -- the caller must abort.
    """
    cost = gas_cost(action)
    if cost <= 0.0:
        return GasResult(action, 0.0, 0.0, 0.0, charged=False, ok=True)

    cost_raw = to_raw(cost)
    holding = await db.get_wallet_holding(
        user_id, guild_id, MOON_NETWORK_SHORT, MOON_SYMBOL,
    )
    bal_raw = int(holding["amount"]) if holding else 0
    if bal_raw < cost_raw:
        return GasResult(action, cost, 0.0, 0.0, charged=False, ok=False)

    # Debit MOON. update_wallet_holding decrements MOON circulating supply,
    # so the whole fee leaves circulation at this step.
    await db.update_wallet_holding(
        user_id, guild_id, MOON_NETWORK_SHORT, MOON_SYMBOL, -cost_raw,
    )

    burned_raw = int(cost_raw * MOON_GAS_BURN_PCT)
    vault_raw = cost_raw - burned_raw

    # Vault share: value the MOON at oracle and add to the distributable
    # bucket so it drips to Moon Pool stakers.
    if vault_raw > 0:
        price_row = await db.get_price(MOON_SYMBOL, guild_id)
        moon_price = float(price_row["price"]) if price_row else 0.0
        vault_usd = to_human(vault_raw) * moon_price
        if vault_usd > 0:
            await db.add_moon_vault_distributable(guild_id, vault_usd)

    return GasResult(
        action, cost, to_human(burned_raw), to_human(vault_raw),
        charged=True, ok=True,
    )


def gas_line(result: GasResult) -> str:
    """One-line gas summary for an embed description."""
    if result.free:
        return f"{_MOON_EMOJI} Gas: free (network on-ramp)"
    return (
        f"{_MOON_EMOJI} Gas: {result.cost:.4f} MOON "
        f"({result.burned:.4f} burned, {result.to_vault:.4f} to vault)"
    )


def gas_field(result: GasResult) -> tuple[str, str, bool]:
    """``(name, value, inline)`` tuple for a ``card().field(*gas_field(r))``."""
    if result.free:
        return (f"{_MOON_EMOJI} Gas", "Free (on-ramp)", True)
    return (
        f"{_MOON_EMOJI} Gas",
        f"{result.cost:.4f} MOON\n{result.burned:.4f} burned",
        True,
    )
