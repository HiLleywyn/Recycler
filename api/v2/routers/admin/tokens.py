"""Admin token management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import ValidationError
from api.v2.schemas.admin import SetPriceRequest, TokenCreate, TokenInfo
from api.v2.schemas.common import SuccessResponse
from api.v2.routers.admin._helpers import audit_log
from api.v2.utils import to_iso

router = APIRouter()


@router.get("/tokens", response_model=list[TokenInfo], summary="List tokens")
async def list_tokens(
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Return all tokens configured for this guild."""
    gid = int(admin["guild_id"])
    rows = await db.fetch(
        "SELECT symbol, name, emoji, consensus, network, start_price, daily_vol, "
        "max_supply, decimals, tx_fee_rate, gas_fee, created_at "
        "FROM guild_tokens WHERE guild_id = $1 ORDER BY symbol",
        gid,
    )
    return [
        TokenInfo(
            symbol=r["symbol"],
            name=r["name"],
            emoji=r["emoji"],
            consensus=r["consensus"],
            network=r["network"],
            start_price=float(r["start_price"]),
            daily_vol=float(r["daily_vol"]),
            max_supply=int(r["max_supply"]) if r["max_supply"] is not None else None,
            decimals=int(r["decimals"]),
            tx_fee_rate=float(r["tx_fee_rate"]),
            gas_fee=float(r["gas_fee"]),
            created_at=to_iso(r["created_at"]),
        )
        for r in rows
    ]


@router.post("/tokens", response_model=SuccessResponse, summary="Create token")
async def create_token(
    body: TokenCreate,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Create a new token for this guild.

    All token parameters are configurable: price, volatility, supply cap,
    decimal precision, per-transfer fee rate, and base gas cost.
    Stablecoins (stablecoin=True) have their daily_vol forced to 0.
    """
    gid = int(admin["guild_id"])
    symbol = body.symbol.upper()

    existing = await db.fetchrow(
        "SELECT symbol FROM guild_tokens WHERE guild_id = $1 AND symbol = $2",
        gid, symbol,
    )
    if existing:
        raise ValidationError(f"Token {symbol} already exists.")

    # Stablecoins must have 0 volatility regardless of what was submitted
    effective_vol = 0.0 if body.stablecoin else body.daily_vol

    await db.execute(
        """
        INSERT INTO guild_tokens
            (guild_id, symbol, name, emoji, consensus, network,
             start_price, daily_vol, max_supply, decimals, tx_fee_rate, gas_fee)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        gid, symbol, body.name, body.emoji, body.consensus,
        body.network, body.start_price, effective_vol,
        body.max_supply, body.decimals, body.tx_fee_rate, body.gas_fee,
    )
    # Initialize price oracle entry
    await db.execute(
        """
        INSERT INTO crypto_prices (symbol, guild_id, price, open_price, day_high, day_low)
        VALUES ($1, $2, $3, $3, $3, $3)
        ON CONFLICT (symbol, guild_id) DO NOTHING
        """,
        symbol, gid, body.start_price,
    )
    await audit_log(db, gid, int(admin["user_id"]), "create_token", {
        "symbol": symbol, "name": body.name, "price": body.start_price,
        "max_supply": body.max_supply, "decimals": body.decimals,
        "tx_fee_rate": body.tx_fee_rate, "gas_fee": body.gas_fee,
    })
    return SuccessResponse(message=f"Token {symbol} created.")


@router.delete("/tokens", response_model=SuccessResponse, summary="Delete token")
async def delete_token(
    symbol: str = Query(..., description="Token symbol to delete"),
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Delete a token from this guild."""
    gid = int(admin["guild_id"])
    await db.execute(
        "DELETE FROM guild_tokens WHERE guild_id = $1 AND symbol = $2",
        gid, symbol.upper(),
    )
    await audit_log(db, gid, int(admin["user_id"]), "delete_token", {"symbol": symbol.upper()})
    return SuccessResponse(message=f"Token {symbol.upper()} deleted.")


@router.post("/tokens/{symbol}/set-price", response_model=SuccessResponse, summary="Set token price")
async def set_token_price(
    symbol: str,
    body: SetPriceRequest,
    admin: dict = Depends(require_admin),
    db=Depends(get_db),
):
    """Manually set a token's current price."""
    gid = int(admin["guild_id"])
    await db.execute(
        """
        UPDATE crypto_prices SET price = $3, updated_at = now()
        WHERE symbol = $1 AND guild_id = $2
        """,
        symbol.upper(), gid, body.price,
    )
    await audit_log(db, gid, int(admin["user_id"]), "set_price",
                    {"symbol": symbol.upper(), "price": body.price})
    return SuccessResponse(message=f"Price of {symbol.upper()} set to {body.price}.")
