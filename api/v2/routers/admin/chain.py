"""Admin chain & supply management  -  mirrors Discord .admin chain / .admin supply."""
from __future__ import annotations


from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import NotFoundError, ValidationError
from api.v2.schemas.common import SuccessResponse

from core.config import Config

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class ChainStatus(BaseModel):
    symbol: str
    name: str
    block_height: int
    difficulty: float
    total_hashrate: float
    current_reward: float
    warmup_blocks: int
    solo_share_cap: float
    initial_difficulty: float
    target_block_time: int
    electricity_rate: float


class ChainSetRequest(BaseModel):
    key: str = Field(..., description="Config key to set.")
    value: float = Field(..., description="New value.")


class SupplyInfo(BaseModel):
    symbol: str
    circulating_supply: float
    max_supply: float | None = None
    pct_of_max: float | None = None


# ── Chain status ──────────────────────────────────────────────────────────────

@router.get("/chain", response_model=list[ChainStatus], summary="Get all PoW chain status")
async def get_chains(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    gid = int(admin["guild_id"])
    results = []
    for symbol, cfg in Config.POW_NETWORKS.items():
        row = await db.fetchrow(
            "SELECT * FROM pow_network_state WHERE guild_id = $1 AND chain_symbol = $2",
            gid, symbol,
        )
        if not row:
            continue
        results.append(ChainStatus(
            symbol=symbol,
            name=cfg.get("name", symbol),
            block_height=row["block_height"],
            difficulty=float(row.get("difficulty") or cfg["initial_difficulty"]),
            total_hashrate=float(row.get("total_hashrate", 0)),
            current_reward=float(row.get("current_reward", 0)),
            warmup_blocks=cfg.get("warmup_blocks", 0),
            solo_share_cap=cfg.get("solo_share_cap", 1.0),
            initial_difficulty=cfg["initial_difficulty"],
            target_block_time=cfg.get("target_block_time", 600),
            electricity_rate=cfg.get("electricity_rate", 0),
        ))
    return results


# ── Chain config set ──────────────────────────────────────────────────────────

_ALLOWED_KEYS = {
    "warmup_blocks", "solo_share_cap", "initial_difficulty", "initial_reward",
    "electricity_rate", "electricity_scaling", "target_block_time", "max_group_share",
}

# Validation ranges for chain config values  -  same rules as Discord bot
_KEY_VALIDATION: dict[str, tuple[float, float]] = {
    "warmup_blocks":       (0, 100_000),
    "solo_share_cap":      (0.01, 1.0),
    "initial_difficulty":  (1.0, 1e18),
    "initial_reward":      (0.00000001, 1_000_000),
    "electricity_rate":    (0.0, 10.0),
    "electricity_scaling": (1.0, 2.0),
    "target_block_time":   (60, 86_400),       # 1 minute to 24 hours
    "max_group_share":     (0.01, 1.0),
}


@router.patch("/chain/{symbol}", response_model=SuccessResponse, summary="Set chain config value")
async def set_chain_config(
    symbol: str,
    body: ChainSetRequest,
    admin: dict = Depends(require_admin),
):
    symbol = symbol.upper()
    cfg = Config.POW_NETWORKS.get(symbol)
    if not cfg:
        raise NotFoundError(f"Unknown chain {symbol}.")
    if body.key not in _ALLOWED_KEYS:
        raise ValidationError(f"Unknown key '{body.key}'. Valid: {', '.join(sorted(_ALLOWED_KEYS))}")
    parsed = int(body.value) if body.key in ("warmup_blocks", "target_block_time") else float(body.value)
    # Enforce validation ranges to prevent misconfiguration
    bounds = _KEY_VALIDATION.get(body.key)
    if bounds:
        lo, hi = bounds
        if parsed < lo or parsed > hi:
            raise ValidationError(f"'{body.key}' must be between {lo} and {hi} (got {parsed}).")
    cfg[body.key] = parsed
    return SuccessResponse(message=f"{symbol} {body.key} set to {parsed}.")


# ── Chain reset ───────────────────────────────────────────────────────────────

@router.post("/chain/{symbol}/reset", response_model=SuccessResponse, summary="Reset chain to block 0")
async def reset_chain(
    symbol: str,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Reset a chain to block 0. Resets difficulty, height, supply. Preserves player balances."""
    symbol = symbol.upper()
    cfg = Config.POW_NETWORKS.get(symbol)
    if not cfg:
        raise NotFoundError(f"Unknown chain {symbol}.")
    gid = int(admin["guild_id"])

    token_cfg = Config.TOKENS.get(symbol, {})
    max_sup = token_cfg.get("max_supply", 0)
    initial_supply = max_sup * 0.5 if max_sup else 0.0

    await db.execute(
        """UPDATE pow_network_state
           SET block_height = 0, total_hashrate = 0, current_reward = $3,
               difficulty = $4, last_block_ts = now(), last_retarget_height = 0,
               last_retarget_ts = now()
           WHERE guild_id = $1 AND chain_symbol = $2""",
        gid, symbol, cfg.get("initial_reward", 1.0), cfg.get("initial_difficulty", 60000.0),
    )
    await db.execute(
        "UPDATE crypto_prices SET circulating_supply = $3 WHERE guild_id = $1 AND symbol = $2",
        gid, symbol, initial_supply,
    )
    await db.execute(
        "UPDATE guild_tokens SET circulating_supply = $3 WHERE guild_id = $1 AND symbol = $2",
        gid, symbol, initial_supply,
    )
    await db.execute(
        "DELETE FROM chain_blocks WHERE guild_id = $1 AND network = $2",
        gid, symbol.lower(),
    )
    return SuccessResponse(
        message=f"{symbol} reset to block 0. Difficulty: {cfg.get('initial_difficulty', 60000.0):,.0f}. "
                f"Supply: {initial_supply:,.0f} (initial). Player balances untouched."
    )


# ── Supply check ──────────────────────────────────────────────────────────────

@router.get("/supply", response_model=list[SupplyInfo], summary="Get token supply info")
async def get_supply(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    gid = int(admin["guild_id"])
    rows = await db.fetch(
        "SELECT symbol, circulating_supply FROM crypto_prices WHERE guild_id = $1 ORDER BY symbol",
        gid,
    )
    gt_rows = await db.fetch(
        "SELECT symbol, circulating_supply, max_supply FROM guild_tokens WHERE guild_id = $1 ORDER BY symbol",
        gid,
    )
    gt_map = {r["symbol"]: r for r in gt_rows}

    results = []
    for r in rows:
        sym = r["symbol"]
        circ = float(r["circulating_supply"])
        tok_cfg = Config.TOKENS.get(sym, {})
        max_sup = (
            float(gt_map[sym]["max_supply"])
            if sym in gt_map and gt_map[sym].get("max_supply") is not None
            else tok_cfg.get("max_supply")
        )
        pct = (circ / max_sup * 100) if max_sup and max_sup > 0 else None
        results.append(SupplyInfo(
            symbol=sym,
            circulating_supply=circ,
            max_supply=max_sup,
            pct_of_max=round(pct, 2) if pct is not None else None,
        ))
    return results


# ── Supply reset ──────────────────────────────────────────────────────────────

@router.post("/supply/{symbol}/reset", response_model=SuccessResponse, summary="Reset token supply and wipe balances")
async def reset_supply(
    symbol: str,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Reset circulating supply to initial value AND wipe all player balances of this token."""
    symbol = symbol.upper()
    token_cfg = Config.TOKENS.get(symbol, {})
    max_sup = token_cfg.get("max_supply", 0)
    initial_supply = max_sup * 0.5 if max_sup else 0.0
    gid = int(admin["guild_id"])

    await db.execute(
        "UPDATE crypto_prices SET circulating_supply = $3 WHERE guild_id = $1 AND symbol = $2",
        gid, symbol, initial_supply,
    )
    await db.execute(
        "UPDATE guild_tokens SET circulating_supply = $3 WHERE guild_id = $1 AND symbol = $2",
        gid, symbol, initial_supply,
    )
    await db.execute("DELETE FROM crypto_holdings WHERE guild_id = $1 AND symbol = $2", gid, symbol)
    await db.execute("DELETE FROM wallet_holdings WHERE guild_id = $1 AND symbol = $2", gid, symbol)
    await db.execute("DELETE FROM stakes WHERE guild_id = $1 AND symbol = $2", gid, symbol)

    return SuccessResponse(
        message=f"{symbol} supply reset to {initial_supply:,.0f} (initial). "
                f"All player holdings, wallet holdings, and stakes wiped."
    )
