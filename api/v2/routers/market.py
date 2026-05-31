"""Market data endpoints  -  prices, candles, tickers, tokens, and networks."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.v2.dependencies import get_current_user, get_db, get_optional_user
from api.v2.exceptions import NotFoundError
from api.v2.schemas.market import (
    CandleData,
    GainersLosers,
    MarketOverview,
    NetworkInfo,
    PriceData,
    TickerData,
    TokenMetadata,
)

router = APIRouter(prefix="/market", tags=["market"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _change_pct(price: float, open_price: float) -> float:
    """Calculate 24-hour change percentage."""
    if open_price == 0:
        return 0.0
    return round(((price - open_price) / open_price) * 100, 4)


def _market_cap(price: float, supply: float) -> float:
    return round(price * supply, 2)


# ---------------------------------------------------------------------------
# GET /market/prices  -  all token prices with 24h stats
# ---------------------------------------------------------------------------

@router.get(
    "/prices",
    response_model=list[PriceData],
    summary="Get all token prices",
)
async def list_prices(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[PriceData]:
    """Return current price data for every token in the guild."""
    guild_id = int(user["guild_id"])

    from core.config import Config

    rows = await db.fetch(
        """
        SELECT cp.symbol, cp.price, cp.open_price, cp.day_high, cp.day_low,
               cp.circulating_supply, gt.network, gt.max_supply as gt_max_supply
        FROM crypto_prices cp
        LEFT JOIN guild_tokens gt
            ON gt.guild_id = cp.guild_id AND gt.symbol = cp.symbol
        WHERE cp.guild_id = $1
        ORDER BY cp.symbol
        """,
        guild_id,
    )

    results: list[PriceData] = []
    for r in rows:
        price = float(r["price"])
        open_price = float(r["open_price"])
        supply = float(r["circulating_supply"])
        sym = r["symbol"]
        # Resolve name and max_supply from Config.TOKENS (built-in) or guild_tokens
        token_cfg = Config.TOKENS.get(sym, {})
        name = token_cfg.get("name", sym)
        max_supply = (
            float(r["gt_max_supply"]) if r.get("gt_max_supply") is not None
            else token_cfg.get("max_supply")
        )
        results.append(
            PriceData(
                symbol=sym,
                name=name,
                price=price,
                open_price=open_price,
                high_24h=float(r["day_high"]),
                low_24h=float(r["day_low"]),
                change_24h_pct=_change_pct(price, open_price),
                market_cap=_market_cap(price, supply),
                circulating_supply=supply,
                max_supply=max_supply,
                volume_24h=0.0,  # populated below if candle data exists
                network=r["network"],
                buyable_usd=sym in Config.BUYABLE_WITH_USD,
                swappable=token_cfg.get("consensus") is not None or sym in ("USDC", "DSD"),
                stakeable=bool(token_cfg.get("stakeable")),
            )
        )

    # Bulk-fetch 24h volume from candles
    vol_rows = await db.fetch(
        """
        SELECT symbol, COALESCE(SUM(volume), 0) AS vol
        FROM price_candles
        WHERE guild_id = $1 AND ts > now() - INTERVAL '24 hours'
        GROUP BY symbol
        """,
        guild_id,
    )
    vol_map = {vr["symbol"]: float(vr["vol"]) for vr in vol_rows}
    for item in results:
        item.volume_24h = vol_map.get(item.symbol, 0.0)

    return results


# ---------------------------------------------------------------------------
# GET /market/prices/{symbol}  -  single token
# ---------------------------------------------------------------------------

@router.get(
    "/prices/{symbol}",
    response_model=PriceData,
    summary="Get price for a single token",
)
async def get_price(
    symbol: str,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> PriceData:
    """Return price data for a single token by symbol."""
    guild_id = int(user["guild_id"])
    symbol = symbol.upper()

    row = await db.fetchrow(
        """
        SELECT cp.symbol, cp.price, cp.open_price, cp.day_high, cp.day_low,
               cp.circulating_supply, gt.network
        FROM crypto_prices cp
        LEFT JOIN guild_tokens gt
            ON gt.guild_id = cp.guild_id AND gt.symbol = cp.symbol
        WHERE cp.guild_id = $1 AND cp.symbol = $2
        """,
        guild_id,
        symbol,
    )
    if not row:
        raise NotFoundError(f"Token '{symbol}' not found.")

    price = float(row["price"])
    open_price = float(row["open_price"])
    supply = float(row["circulating_supply"])

    vol_row = await db.fetchrow(
        """
        SELECT COALESCE(SUM(volume), 0) AS vol
        FROM price_candles
        WHERE guild_id = $1 AND symbol = $2 AND ts > now() - INTERVAL '24 hours'
        """,
        guild_id,
        symbol,
    )
    volume = float(vol_row["vol"]) if vol_row else 0.0

    return PriceData(
        symbol=row["symbol"],
        price=price,
        open_price=open_price,
        high_24h=float(row["day_high"]),
        low_24h=float(row["day_low"]),
        change_24h_pct=_change_pct(price, open_price),
        market_cap=_market_cap(price, supply),
        circulating_supply=supply,
        volume_24h=volume,
        network=row["network"],
    )


# ---------------------------------------------------------------------------
# GET /market/candles/{symbol}  -  OHLCV candles
# ---------------------------------------------------------------------------

@router.get(
    "/candles/{symbol}",
    response_model=list[CandleData],
    summary="Get OHLCV candles for a token",
)
async def get_candles(
    symbol: str,
    tf: str = Query("1h", description="Time-frame (e.g. 1h, 4h, 1d)."),
    limit: int = Query(100, ge=1, le=1000, description="Max number of candles."),
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[CandleData]:
    """Return OHLCV candle data for a token.

    The ``tf`` query parameter accepts common time-frames such as ``1h``,
    ``4h``, or ``1d``.  The candles are returned newest-first up to ``limit``.
    """
    guild_id = int(user["guild_id"])
    symbol = symbol.upper()

    # Map tf shorthand to PostgreSQL interval for bucketing
    tf_map = {
        "1m": "1 minute",
        "5m": "5 minutes",
        "15m": "15 minutes",
        "30m": "30 minutes",
        "1h": "1 hour",
        "4h": "4 hours",
        "1d": "1 day",
    }
    pg_interval = tf_map.get(tf, "1 hour")

    # Use date_bin for proper interval bucketing (PostgreSQL 14+)
    rows = await db.fetch(
        f"""
        SELECT
            date_bin('{pg_interval}'::interval, ts, '2020-01-01'::timestamptz) AS bucket,
            (array_agg(open ORDER BY ts ASC))[1]   AS open,
            MAX(high)                               AS high,
            MIN(low)                                AS low,
            (array_agg(close ORDER BY ts DESC))[1]  AS close,
            COALESCE(SUM(volume), 0)                AS volume
        FROM price_candles
        WHERE guild_id = $1 AND symbol = $2
        GROUP BY bucket
        ORDER BY bucket DESC
        LIMIT $3
        """,
        guild_id,
        symbol,
        limit,
    )

    return [
        CandleData(
            time=int(r["bucket"].timestamp()),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /market/tickers  -  compact ticker list
# ---------------------------------------------------------------------------

@router.get(
    "/tickers",
    response_model=list[TickerData],
    summary="Get compact ticker list",
)
async def list_tickers(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[TickerData]:
    """Return a compact list of symbol + price + 24h change for every token."""
    guild_id = int(user["guild_id"])

    rows = await db.fetch(
        """
        SELECT symbol, price, open_price
        FROM crypto_prices
        WHERE guild_id = $1
        ORDER BY symbol
        """,
        guild_id,
    )

    from core.config import Config
    results = []
    for r in rows:
        sym = r["symbol"]
        tcfg = Config.TOKENS.get(sym, {})
        results.append(TickerData(
            symbol=sym,
            price=float(r["price"]),
            change_24h_pct=_change_pct(float(r["price"]), float(r["open_price"])),
            buyable_usd=sym in Config.BUYABLE_WITH_USD,
            swappable=tcfg.get("consensus") is not None or sym in ("USDC", "DSD"),
            stakeable=bool(tcfg.get("stakeable")),
        ))
    return results


# ---------------------------------------------------------------------------
# GET /market/overview  -  total market stats
# ---------------------------------------------------------------------------

@router.get(
    "/overview",
    response_model=MarketOverview,
    summary="Get market overview",
)
async def market_overview(
    db=Depends(get_db),
    user: dict[str, Any] | None = Depends(get_optional_user),
) -> MarketOverview:
    """Return aggregate market statistics: total market cap, volume, counts,
    and the top gainers/losers.

    Works without authentication  -  uses the first available guild when no
    auth token is provided.
    """
    if user and user.get("guild_id"):
        guild_id = int(user["guild_id"])
    else:
        # Fallback: pick the first guild that has price data
        row = await db.fetchrow("SELECT guild_id FROM crypto_prices LIMIT 1")
        guild_id = int(row["guild_id"]) if row else 0

    rows = await db.fetch(
        """
        SELECT symbol, price, open_price, circulating_supply
        FROM crypto_prices
        WHERE guild_id = $1
        """,
        guild_id,
    )

    total_cap = 0.0
    tickers: list[TickerData] = []
    for r in rows:
        price = float(r["price"])
        open_price = float(r["open_price"])
        supply = float(r["circulating_supply"])
        total_cap += price * supply
        tickers.append(
            TickerData(
                symbol=r["symbol"],
                price=price,
                change_24h_pct=_change_pct(price, open_price),
            )
        )

    # Volume
    vol_row = await db.fetchrow(
        """
        SELECT COALESCE(SUM(volume), 0) AS vol
        FROM price_candles
        WHERE guild_id = $1 AND ts > now() - INTERVAL '24 hours'
        """,
        guild_id,
    )
    total_volume = float(vol_row["vol"]) if vol_row else 0.0

    # Gainers and losers
    sorted_up = sorted(tickers, key=lambda t: t.change_24h_pct, reverse=True)
    sorted_down = sorted(tickers, key=lambda t: t.change_24h_pct)

    return MarketOverview(
        total_market_cap=round(total_cap, 2),
        total_volume_24h=total_volume,
        total_tokens=len(rows),
        top_gainers=sorted_up[:5],
        top_losers=sorted_down[:5],
    )


# ---------------------------------------------------------------------------
# GET /market/gainers-losers  -  top 5 each
# ---------------------------------------------------------------------------

@router.get(
    "/gainers-losers",
    response_model=GainersLosers,
    summary="Get top gainers and losers",
)
async def gainers_losers(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> GainersLosers:
    """Return the top 5 gainers and top 5 losers by 24-hour price change."""
    guild_id = int(user["guild_id"])

    rows = await db.fetch(
        """
        SELECT symbol, price, open_price
        FROM crypto_prices
        WHERE guild_id = $1
        """,
        guild_id,
    )

    tickers = [
        TickerData(
            symbol=r["symbol"],
            price=float(r["price"]),
            change_24h_pct=_change_pct(float(r["price"]), float(r["open_price"])),
        )
        for r in rows
    ]

    sorted_up = sorted(tickers, key=lambda t: t.change_24h_pct, reverse=True)
    sorted_down = sorted(tickers, key=lambda t: t.change_24h_pct)

    return GainersLosers(
        gainers=sorted_up[:5],
        losers=sorted_down[:5],
    )


# ---------------------------------------------------------------------------
# GET /market/tokens  -  all tokens grouped by network
# ---------------------------------------------------------------------------

@router.get(
    "/tokens",
    response_model=list[TokenMetadata],
    summary="List all tokens",
)
async def list_tokens(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[TokenMetadata]:
    """Return metadata for every token in the guild, grouped by network."""
    guild_id = int(user["guild_id"])

    rows = await db.fetch(
        """
        SELECT symbol, name, network, consensus
        FROM guild_tokens
        WHERE guild_id = $1
        ORDER BY network NULLS LAST, symbol
        """,
        guild_id,
    )

    return [
        TokenMetadata(
            symbol=r["symbol"],
            name=r["name"],
            network=r["network"],
            consensus=r["consensus"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /market/tokens/{symbol}  -  single token metadata
# ---------------------------------------------------------------------------

@router.get(
    "/tokens/{symbol}",
    response_model=TokenMetadata,
    summary="Get token metadata",
)
async def get_token_metadata(
    symbol: str,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> TokenMetadata:
    """Return metadata for a single token by symbol."""
    guild_id = int(user["guild_id"])
    symbol = symbol.upper()

    row = await db.fetchrow(
        """
        SELECT symbol, name, network, consensus
        FROM guild_tokens
        WHERE guild_id = $1 AND symbol = $2
        """,
        guild_id,
        symbol,
    )
    if not row:
        raise NotFoundError(f"Token '{symbol}' not found.")

    return TokenMetadata(
        symbol=row["symbol"],
        name=row["name"],
        network=row["network"],
        consensus=row["consensus"],
    )


# ---------------------------------------------------------------------------
# GET /market/networks  -  network list
# ---------------------------------------------------------------------------

@router.get(
    "/networks",
    response_model=list[NetworkInfo],
    summary="List blockchain networks",
)
async def list_networks(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[NetworkInfo]:
    """Return information about each blockchain network in the guild."""
    guild_id = int(user["guild_id"])

    rows = await db.fetch(
        """
        SELECT gn.network_name, gn.stake_token,
               COUNT(gt.symbol) AS token_count
        FROM guild_networks gn
        LEFT JOIN guild_tokens gt
            ON gt.guild_id = gn.guild_id AND gt.network = gn.network_name
        WHERE gn.guild_id = $1
        GROUP BY gn.network_name, gn.stake_token
        ORDER BY gn.network_name
        """,
        guild_id,
    )

    results: list[NetworkInfo] = []
    for r in rows:
        # Sum staked value for the network
        stake_row = await db.fetchrow(
            """
            SELECT COALESCE(SUM(pv.stake_amount), 0) AS total_staked
            FROM pos_validators pv
            WHERE pv.guild_id = $1 AND pv.network = $2
            """,
            guild_id,
            r["network_name"],
        )
        total_staked = float(stake_row["total_staked"]) if stake_row else 0.0

        results.append(
            NetworkInfo(
                name=r["network_name"],
                total_tokens=int(r["token_count"]),
                total_staked=total_staked,
            )
        )

    return results
