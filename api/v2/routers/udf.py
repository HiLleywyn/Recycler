"""TradingView UDF (Universal Data Feed) -- live endpoint.

Implements the TradingView Charting Library UDF protocol so external
TradingView clients (anything that speaks the UDF datafeed spec) can
pull live OHLC + symbol-search data from this bot's existing provider
stack -- CoinGecko for crypto, Yahoo Finance for equities / ETFs /
forex / commodities / indices, DexScreener for DEX pairs.

Public endpoints (mount at ``/api/v2/udf``):

  GET /config              - datafeed capability descriptor
  GET /symbols?symbol=AAPL - one-symbol metadata
  GET /search?query=MTA&limit=10 - fuzzy symbol search
  GET /history?symbol=AAPL&resolution=D&from=<unix>&to=<unix>
                           - candle history; UDF response shape

UDF spec reference:
  https://www.tradingview.com/charting-library-docs/latest/connecting_data/UDF/

Notes
-----

- No TradingView Datafeed license is required to run this endpoint. The
  Charting Library is a separate product; this is just the data plane.
- Recursion guard: this endpoint pulls data via the market router but
  explicitly excludes the ``TradingViewProvider`` from its fan-out so
  pointing ``TRADINGVIEW_UDF_URL`` at this URL doesn't create a loop.
- All responses are CORS-open (``Access-Control-Allow-Origin: *``) so a
  browser-side Charting Library can read them without a proxy.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from services.market.base import AssetClass, ResolvedSymbol
from services.market.router import get_router
from services.market.timeframes import canonical_tf

log = logging.getLogger(__name__)

router = APIRouter(prefix="/udf", tags=["udf"])


# ── TradingView resolution mapping ──────────────────────────────────────
#
# UDF resolutions are quoted as bare strings -- minutes as integers,
# daily/weekly/monthly as the special tokens "D"/"W"/"M". We map them to
# the canonical timeframe codes our router understands.

_TV_TO_TF: dict[str, str] = {
    "1":   "1m",
    "3":   "3m",
    "5":   "5m",
    "15":  "15m",
    "30":  "30m",
    "45":  "45m",
    "60":  "1h",
    "120": "2h",
    "240": "4h",
    "360": "6h",
    "480": "8h",
    "720": "12h",
    "D":   "1d",
    "1D":  "1d",
    "3D":  "3d",
    "W":   "1w",
    "1W":  "1w",
    "M":   "1mo",
    "1M":  "1mo",
    "3M":  "3mo",
    "6M":  "6mo",
    "12M": "1y",
}

_TV_SUPPORTED_RESOLUTIONS: tuple[str, ...] = tuple(_TV_TO_TF.keys())


def _cors(payload: Any, *, status: int = 200) -> Response:
    """Wrap a JSON payload with CORS headers so browser charting libs
    can read it directly."""
    return JSONResponse(
        content=payload,
        status_code=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Cache-Control": "no-store",
        },
    )


def _no_data() -> dict[str, Any]:
    return {"s": "no_data"}


def _error(msg: str) -> dict[str, Any]:
    return {"s": "error", "errmsg": msg}


def _exchange_for(ac: AssetClass) -> str:
    return {
        AssetClass.CRYPTO:    "Crypto",
        AssetClass.DEX:       "DEX",
        AssetClass.EQUITY:    "Equity",
        AssetClass.ETF:       "ETF",
        AssetClass.FOREX:     "FX",
        AssetClass.COMMODITY: "Commodity",
        AssetClass.INDEX:     "Index",
        AssetClass.PERP:      "Perp",
        AssetClass.ORACLE:    "Oracle",
    }.get(ac, "Market")


def _type_for(ac: AssetClass) -> str:
    return {
        AssetClass.CRYPTO:    "crypto",
        AssetClass.DEX:       "crypto",
        AssetClass.EQUITY:    "stock",
        AssetClass.ETF:       "etf",
        AssetClass.FOREX:     "forex",
        AssetClass.COMMODITY: "commodity",
        AssetClass.INDEX:     "index",
        AssetClass.PERP:      "perp",
    }.get(ac, "stock")


def _symbol_descriptor(resolved: ResolvedSymbol) -> dict[str, Any]:
    """UDF symbol-info payload."""
    return {
        "name": resolved.symbol,
        "full_name": f"{_exchange_for(resolved.asset_class)}:{resolved.symbol}",
        "description": resolved.name,
        "exchange": _exchange_for(resolved.asset_class),
        "listed_exchange": _exchange_for(resolved.asset_class),
        "type": _type_for(resolved.asset_class),
        "session": "24x7",
        "timezone": "Etc/UTC",
        "ticker": resolved.symbol,
        "minmov": 1,
        "pricescale": 10000,
        "has_intraday": True,
        "has_seconds": False,
        "has_daily": True,
        "has_weekly_and_monthly": True,
        "supported_resolutions": list(_TV_SUPPORTED_RESOLUTIONS),
        "volume_precision": 0,
        "data_status": "streaming",
        "currency_code": "USD",
    }


# ── endpoints ────────────────────────────────────────────────────────────

@router.options("/{path:path}")
async def cors_preflight(path: str) -> Response:
    return _cors({})


@router.get("/config")
async def udf_config() -> Response:
    """UDF capability descriptor consumed by the Charting Library on
    initial datafeed handshake."""
    return _cors({
        "supports_search": True,
        "supports_group_request": False,
        "supports_marks": False,
        "supports_timescale_marks": False,
        "supports_time": True,
        "supports_streaming": False,  # we're a pull feed; no WS yet
        "exchanges": [
            {"value": "", "name": "All", "desc": "All exchanges"},
            {"value": "Crypto",    "name": "Crypto",    "desc": "Crypto spot via CoinGecko"},
            {"value": "DEX",       "name": "DEX",       "desc": "DEX pairs via DexScreener"},
            {"value": "Equity",    "name": "Equity",    "desc": "Equities via Yahoo Finance"},
            {"value": "ETF",       "name": "ETF",       "desc": "ETFs via Yahoo Finance"},
            {"value": "FX",        "name": "FX",        "desc": "Forex via Yahoo Finance"},
            {"value": "Commodity", "name": "Commodity", "desc": "Commodities via Yahoo"},
            {"value": "Index",     "name": "Index",     "desc": "Indices via Yahoo"},
        ],
        "symbols_types": [
            {"name": "All",       "value": ""},
            {"name": "Crypto",    "value": "crypto"},
            {"name": "Equity",    "value": "stock"},
            {"name": "ETF",       "value": "etf"},
            {"name": "Forex",     "value": "forex"},
            {"name": "Commodity", "value": "commodity"},
            {"name": "Index",     "value": "index"},
        ],
        "supported_resolutions": list(_TV_SUPPORTED_RESOLUTIONS),
    })


@router.get("/time")
async def udf_time() -> Response:
    """UDF server-time. Returned as a plain integer."""
    return _cors(int(time.time()))


@router.get("/symbols")
async def udf_symbols(request: Request) -> Response:
    sym = (request.query_params.get("symbol") or "").strip()
    if not sym:
        return _cors(_error("symbol required"), status=400)
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        return _cors(_error("market router unavailable"), status=503)
    try:
        resolved = await get_router(bot).resolve(sym)
    except Exception:
        log.exception("[udf] resolve crashed")
        return _cors(_error("resolver failed"), status=502)
    if resolved is None:
        return _cors(_error("unknown_symbol"), status=404)
    return _cors(_symbol_descriptor(resolved))


@router.get("/search")
async def udf_search(request: Request) -> Response:
    """Fuzzy symbol search. Resolves the query through the router and
    returns the single best match; the Charting Library tolerates
    short hit lists fine. All params come off ``query_params`` so
    unknown / extra params don't 422."""
    qp = request.query_params
    q = (qp.get("query") or "").strip()
    try:
        limit = max(1, int(qp.get("limit") or 30))
    except (TypeError, ValueError):
        limit = 30
    if not q:
        return _cors([])
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        return _cors([])
    try:
        resolved = await get_router(bot).resolve(q)
    except Exception:
        log.exception("[udf] search resolve crashed")
        return _cors([])
    if resolved is None:
        return _cors([])
    descriptor = _symbol_descriptor(resolved)
    item = {
        "symbol": descriptor["name"],
        "full_name": descriptor["full_name"],
        "description": descriptor["description"],
        "exchange": descriptor["exchange"],
        "ticker": descriptor["ticker"],
        "type": descriptor["type"],
    }
    return _cors([item][:limit])


@router.get("/history")
async def udf_history(request: Request) -> Response:
    """UDF candle history.

    Returns the column-major payload the Charting Library expects:
    ``{"s": "ok", "t": [...], "o": [...], "h": [...], "l": [...],
       "c": [...], "v": [...]}``.

    Out-of-range / unknown-symbol returns ``{"s": "no_data"}``.

    All params come off ``request.query_params`` (not the route
    signature) for two reasons: ``from`` is a reserved Python keyword
    so FastAPI can't bind it to a function arg, and accepting unknown
    extra params via the signature would trip Pydantic's 422
    validator.
    """
    qp = request.query_params
    sym = (qp.get("symbol") or "").strip()
    res = (qp.get("resolution") or "").strip()
    if not sym or not res:
        return _cors(_error("symbol and resolution required"), status=400)

    tf = _TV_TO_TF.get(res) or canonical_tf(res)
    if not tf:
        return _cors(_error(f"resolution {res!r} unsupported"), status=400)

    try:
        from_ts = int(qp.get("from") or 0)
        to_ts = int(qp.get("to") or 0)
    except (TypeError, ValueError):
        return _cors(_error("from / to must be unix timestamps"), status=400)

    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        return _cors(_error("market router unavailable"), status=503)

    router_obj = get_router(bot)
    try:
        resolved = await router_obj.resolve(sym)
    except Exception:
        log.exception("[udf] history resolve crashed")
        return _cors(_error("resolver failed"), status=502)
    if resolved is None:
        return _cors(_no_data())

    # Recursion guard: when the TradingView provider is configured to
    # point back at this very endpoint, we MUST short-circuit it for
    # the duration of THIS request -- otherwise router.ohlc fans out
    # to TradingView which calls /history which fans out to TradingView
    # which calls /history...
    #
    # We snapshot the provider's full health entry, switch it to a
    # transient DISABLED state, run the inner fetch, and restore the
    # original entry verbatim in ``finally``. Using ``mark_success`` to
    # un-disable would NOT work because the health table deliberately
    # never auto-clears the DISABLED flag (it's reserved for "missing
    # key / config" cases that need an operator action).
    tv = router_obj.registry.get("tradingview")
    tv_saved: Any = None
    if tv is not None:
        health = router_obj.registry.health
        entry = health.get("tradingview")
        tv_saved = {
            "status": entry.status,
            "reason": entry.reason,
            "consecutive_failures": entry.consecutive_failures,
            "last_failure_ts": entry.last_failure_ts,
            "last_success_ts": entry.last_success_ts,
            "cool_off_until": entry.cool_off_until,
        }
        # Direct mutation instead of mark_disabled() so we don't
        # touch the underlying class invariants the health table
        # otherwise enforces.
        from services.market.health import HealthStatus  # local import
        entry.status = HealthStatus.DISABLED
        entry.reason = "skipped inside /api/v2/udf to avoid loop"

    try:
        candles, _provider = await router_obj.ohlc(resolved, tf)
    except Exception as exc:
        log.warning("[udf] ohlc failed for %s %s: %s", sym, tf, exc)
        return _cors(_no_data())
    finally:
        if tv is not None and tv_saved is not None:
            entry = router_obj.registry.health.get("tradingview")
            entry.status = tv_saved["status"]
            entry.reason = tv_saved["reason"]
            entry.consecutive_failures = tv_saved["consecutive_failures"]
            entry.last_failure_ts = tv_saved["last_failure_ts"]
            entry.last_success_ts = tv_saved["last_success_ts"]
            entry.cool_off_until = tv_saved["cool_off_until"]

    if not candles:
        return _cors(_no_data())

    # Trim to the requested window; UDF expects ascending-time order.
    candles = [c for c in candles if (not from_ts or c.ts >= from_ts) and (not to_ts or c.ts <= to_ts)]
    if not candles:
        return _cors(_no_data())
    candles.sort(key=lambda c: c.ts)

    return _cors({
        "s": "ok",
        "t": [int(c.ts) for c in candles],
        "o": [float(c.open) for c in candles],
        "h": [float(c.high) for c in candles],
        "l": [float(c.low) for c in candles],
        "c": [float(c.close) for c in candles],
        "v": [float(c.volume or 0.0) for c in candles],
    })
