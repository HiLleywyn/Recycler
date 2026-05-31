"""Live-market data layer for the $-prefixed real-crypto commands.

Wraps the CoinGecko free public API (https://www.coingecko.com/en/api) so the
``$chart`` / ``$info`` commands in :mod:`cogs.realmarket` can fetch OHLC
candles, market overviews, top headlines, and best-effort holder data without
touching the simulated ``price_candles`` table.

All responses are Redis-cached using the bot's existing
``bot.bus._redis`` client (same pattern as :meth:`cogs.groups.Groups._cache_get`).
A persistent ``aiohttp.ClientSession`` is reused across requests for
keep-alive; failures retry with exponential backoff on 429 and 5xx.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from core.config import Config

log = logging.getLogger(__name__)


class RealMarketError(Exception):
    """Surfaced to user code when CoinGecko is unreachable, misconfigured,
    or returns an unrecoverable error after retries."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# Timeframe -> (CoinGecko endpoint, days window, native granularity in seconds)
#
# CoinGecko exposes two relevant endpoints on the free tier:
#
#   /coins/{id}/ohlc?days=N
#     days=1     -> 30 min candles
#     days=7..30 -> 4 hour candles
#     days=90+   -> 4 day candles
#
#   /coins/{id}/market_chart?days=N
#     days=1     -> ~5 min price points
#     days=2..90 -> hourly price points
#     days >90   -> daily price points
#
# For sub-hourly timeframes (5m, 15m, 30m) we pull the 5-min price stream
# from market_chart and synthesise OHLC candles by bucketing. For 1h, 4h,
# and 1d we use the native ohlc endpoint. Anything finer than 5m is not
# available on the free tier and is rejected upstream with a clear error.
_TF_NATIVE: dict[str, tuple[str, int, int]] = {
    # endpoint, days, bucket seconds
    "5m":  ("market_chart",  1, 300),
    "15m": ("market_chart",  1, 900),
    "30m": ("market_chart",  1, 1800),
    "1h":  ("ohlc",          7, 3600),
    "4h":  ("ohlc",         30, 14400),
    "1d":  ("ohlc",        365, 86400),
}

SUPPORTED_TIMEFRAMES: tuple[str, ...] = tuple(_TF_NATIVE.keys())


class RealMarketClient:
    """Singleton-style CoinGecko client. One instance per bot is fine."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.base: str = Config.REAL_MARKET_API_BASE.rstrip("/")
        self.api_key: str = Config.COINGECKO_API_KEY or ""
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=Config.REAL_MARKET_HTTP_TIMEOUT)
                self._session = aiohttp.ClientSession(timeout=timeout)
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Redis cache helpers (same pattern as cogs/groups.py:1257) ──

    def _redis(self):
        return getattr(getattr(self.bot, "bus", None), "_redis", None)

    async def _cache_get(self, key: str) -> Any | None:
        r = self._redis()
        if r is None:
            return None
        try:
            raw = await r.get(key)
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode()
            return json.loads(raw)
        except Exception:
            return None

    async def _cache_set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        r = self._redis()
        if r is None:
            return
        try:
            await r.setex(key, ttl, json.dumps(value))
        except Exception:
            pass

    # ── HTTP with retry / backoff ─────────────────────────────────

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        sess = await self._get_session()
        url = f"{self.base}{path}"
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-cg-pro-api-key"] = self.api_key

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with sess.get(url, params=params, headers=headers) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        log.warning(
                            "coingecko %s -> %s (attempt %d/3)",
                            path, resp.status, attempt + 1,
                        )
                        last_exc = RealMarketError(
                            f"CoinGecko {path} -> {resp.status}",
                            status=resp.status,
                        )
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        log.warning("coingecko %s -> %s: %s", path, resp.status, body)
                        raise RealMarketError(
                            f"CoinGecko {path} -> {resp.status}",
                            status=resp.status,
                        )
                    return await resp.json()
            except aiohttp.ClientError as exc:
                log.warning("coingecko %s network error (attempt %d/3): %s",
                            path, attempt + 1, exc)
                last_exc = exc
                await asyncio.sleep(2 ** attempt)
                continue
        if isinstance(last_exc, RealMarketError):
            raise last_exc
        raise RealMarketError(f"CoinGecko {path} failed after 3 attempts")

    # ── public methods ────────────────────────────────────────────

    async def resolve_symbol(self, symbol: str) -> dict | None:
        """Resolve a user ticker (e.g. ``MTA``, ``mta``, ``moneta``) to a
        CoinGecko coin record ``{"id": ..., "symbol": ..., "name": ...,
        "thumb": ..., "market_cap_rank": ...}`` or None if no match.

        Disambiguates colliding tickers by picking the highest market-cap
        rank (lowest numeric rank). Cached for a day per symbol.
        """
        s = symbol.strip().lower()
        if not s:
            return None

        key = f"realmarket:sym:{s}"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached or None

        try:
            data = await self._get("/search", params={"query": s})
        except RealMarketError:
            return None

        coins = data.get("coins") or []
        # Prefer exact symbol match (case-insensitive), then exact id match,
        # then highest market-cap rank.
        def _rank(c: dict) -> tuple[int, int]:
            sym_match = 0 if (c.get("symbol", "").lower() == s) else 1
            id_match  = 0 if (c.get("id", "").lower() == s) else 1
            mcap_rank = c.get("market_cap_rank") or 10**9
            return (sym_match + id_match, mcap_rank)

        coins.sort(key=_rank)
        match = coins[0] if coins else None
        if match:
            record = {
                "id": match.get("id"),
                "symbol": (match.get("symbol") or "").upper(),
                "name": match.get("name") or match.get("id"),
                "thumb": match.get("thumb") or "",
                "market_cap_rank": match.get("market_cap_rank"),
            }
            await self._cache_set(key, record, Config.REAL_MARKET_CACHE_TTL_SYMBOL)
            return record

        # Cache the negative result for a shorter window (5 min) so typos
        # don't hammer the API.
        await self._cache_set(key, {}, 300)
        return None

    async def get_ohlc(self, coin_id: str, timeframe: str) -> list[dict]:
        """Return aggregated candles in ``[{"ts", "open", "high", "low",
        "close", "volume"}, ...]`` shape -- the same shape
        :func:`core.framework.chart._aggregate` consumes.

        ``timeframe`` must be one of :data:`SUPPORTED_TIMEFRAMES`. For
        1h/4h/1d we hit ``/ohlc`` directly; for 5m/15m/30m we pull the
        5-minute price stream from ``/market_chart`` and synthesise OHLC
        candles by bucketing into ``tf_seconds`` buckets. Volume is 0.0
        because neither free-tier endpoint exposes it.
        """
        if timeframe not in _TF_NATIVE:
            raise RealMarketError(
                f"timeframe {timeframe!r} not supported "
                f"(use {', '.join(SUPPORTED_TIMEFRAMES)})"
            )

        endpoint, days, tf_sec = _TF_NATIVE[timeframe]
        key = f"realmarket:ohlc:{coin_id}:{timeframe}"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached

        if endpoint == "ohlc":
            raw = await self._get(
                f"/coins/{coin_id}/ohlc",
                params={"vs_currency": "usd", "days": days},
            )
            if not isinstance(raw, list):
                raise RealMarketError(f"unexpected OHLC payload for {coin_id}")
            candles: list[dict] = []
            for entry in raw:
                if not isinstance(entry, list) or len(entry) < 5:
                    continue
                ts_ms, o, h, l, c = entry[:5]
                candles.append({
                    "ts": int(ts_ms) // 1000,
                    "open":  float(o),
                    "high":  float(h),
                    "low":   float(l),
                    "close": float(c),
                    "volume": 0.0,
                })
        else:
            # /market_chart returns {prices: [[ts_ms, price], ...]} with
            # ~5-minute resolution on days=1. Bucket into tf_sec windows.
            raw = await self._get(
                f"/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": days},
            )
            if not isinstance(raw, dict):
                raise RealMarketError(f"unexpected market_chart payload for {coin_id}")
            prices = raw.get("prices") or []
            candles = self._synthesize_candles(prices, tf_sec)

        await self._cache_set(key, candles, Config.REAL_MARKET_CACHE_TTL_OHLC)
        return candles

    @staticmethod
    def _synthesize_candles(price_points: list, tf_sec: int) -> list[dict]:
        """Bucket [[ts_ms, price], ...] into OHLC candles. Open is the first
        point of the bucket, close is the last, high/low are the extrema.
        Volume is not available from this endpoint so we leave it at 0.0."""
        buckets: dict[int, dict] = {}
        for entry in price_points:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            try:
                ts = int(entry[0]) // 1000
                price = float(entry[1])
            except (TypeError, ValueError):
                continue
            bucket_ts = (ts // tf_sec) * tf_sec
            b = buckets.get(bucket_ts)
            if b is None:
                buckets[bucket_ts] = {
                    "ts":    bucket_ts,
                    "open":  price,
                    "high":  price,
                    "low":   price,
                    "close": price,
                    "volume": 0.0,
                }
            else:
                if price > b["high"]:
                    b["high"] = price
                if price < b["low"]:
                    b["low"] = price
                b["close"] = price
        return sorted(buckets.values(), key=lambda x: x["ts"])

    async def get_overview(self, coin_id: str) -> dict:
        """Return the CoinGecko ``/coins/{id}`` payload, trimmed to the
        fields used by ``$info``."""
        key = f"realmarket:over:{coin_id}"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached

        raw = await self._get(
            f"/coins/{coin_id}",
            params={
                "localization": "false",
                "tickers": "false",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
        )
        # Trim down to the fields $info actually reads so the cached blob
        # stays small.
        md = (raw.get("market_data") or {}) if isinstance(raw, dict) else {}
        out = {
            "id":     raw.get("id", coin_id),
            "symbol": (raw.get("symbol") or "").upper(),
            "name":   raw.get("name") or raw.get("id") or coin_id,
            "image":  ((raw.get("image") or {}).get("small") or ""),
            "market_cap_rank": raw.get("market_cap_rank"),
            "categories": raw.get("categories") or [],
            "platforms":  raw.get("platforms") or {},
            "market_data": {
                "current_price":   (md.get("current_price") or {}),
                "high_24h":        (md.get("high_24h") or {}),
                "low_24h":         (md.get("low_24h") or {}),
                "total_volume":    (md.get("total_volume") or {}),
                "market_cap":      (md.get("market_cap") or {}),
                "fully_diluted_valuation": (md.get("fully_diluted_valuation") or {}),
                "circulating_supply": md.get("circulating_supply"),
                "total_supply":       md.get("total_supply"),
                "max_supply":         md.get("max_supply"),
                "ath":              (md.get("ath") or {}),
                "ath_date":         (md.get("ath_date") or {}),
                "atl":              (md.get("atl") or {}),
                "atl_date":         (md.get("atl_date") or {}),
                "price_change_percentage_1h_in_currency":  (md.get("price_change_percentage_1h_in_currency") or {}),
                "price_change_percentage_24h_in_currency": (md.get("price_change_percentage_24h_in_currency") or {}),
                "price_change_percentage_7d_in_currency":  (md.get("price_change_percentage_7d_in_currency") or {}),
                "price_change_percentage_30d_in_currency": (md.get("price_change_percentage_30d_in_currency") or {}),
            },
        }
        await self._cache_set(key, out, Config.REAL_MARKET_CACHE_TTL_OVIEW)
        return out

    async def get_news(self, coin_name: str, coin_symbol: str, limit: int = 3) -> list[dict]:
        """Top recent headlines mentioning the coin (best-effort).

        CoinGecko's /news endpoint returns a global feed; we filter locally
        for entries whose title or description mentions the coin name or
        symbol (case-insensitive). Cached for ``REAL_MARKET_CACHE_TTL_NEWS``.
        """
        key = f"realmarket:news:{coin_symbol.lower()}"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached[:limit]

        try:
            raw = await self._get("/news")
        except RealMarketError:
            return []

        items = []
        needles = {coin_name.lower(), coin_symbol.lower()}
        for entry in (raw.get("data") or []) if isinstance(raw, dict) else []:
            title = (entry.get("title") or "").strip()
            desc = (entry.get("description") or "").strip()
            url  = (entry.get("url") or "").strip()
            if not (title and url):
                continue
            hay = (title + " " + desc).lower()
            if not any(n and n in hay for n in needles):
                continue
            items.append({
                "title": title,
                "url": url,
                "source": ((entry.get("news_site") or "")).strip(),
                "ts": int(entry.get("updated_at") or 0),
            })
            if len(items) >= limit * 2:  # cache a few extras
                break

        await self._cache_set(key, items, Config.REAL_MARKET_CACHE_TTL_NEWS)
        return items[:limit]

    async def get_top_tickers(self, coin_id: str, limit: int = 5) -> list[dict]:
        """Top exchange tickers for ``coin_id``, sorted by 24h USD volume.

        CoinGecko's free tier doesn't expose per-coin holder distributions,
        so the closest proxy for "whale activity" is the venues where the
        biggest USD flows are happening. Each entry has ``exchange``,
        ``pair``, ``volume_usd``, ``price_usd``, and ``trust_score``.
        Stale and anomaly tickers are filtered out.
        """
        key = f"realmarket:tickers:{coin_id}"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached[:limit]
        try:
            raw = await self._get(
                f"/coins/{coin_id}/tickers",
                params={"include_exchange_logo": "false", "depth": "false"},
            )
        except RealMarketError:
            return []
        tickers = (raw or {}).get("tickers") or [] if isinstance(raw, dict) else []
        out: list[dict] = []
        for t in tickers:
            if t.get("is_stale") or t.get("is_anomaly"):
                continue
            market = t.get("market") or {}
            cv = t.get("converted_volume") or {}
            usd_vol = cv.get("usd")
            try:
                usd_vol = float(usd_vol or 0.0)
            except (TypeError, ValueError):
                continue
            if usd_vol <= 0:
                continue
            cl = t.get("converted_last") or {}
            try:
                price_usd = float(cl.get("usd") or 0.0)
            except (TypeError, ValueError):
                price_usd = 0.0
            out.append({
                "exchange":    market.get("name") or "Unknown",
                "base":        (t.get("base") or "").upper(),
                "target":      (t.get("target") or "").upper(),
                "volume_usd":  usd_vol,
                "price_usd":   price_usd,
                "trust_score": t.get("trust_score") or "",
            })
        out.sort(key=lambda x: x["volume_usd"], reverse=True)
        # Dedupe by exchange so a single CEX with multiple pairs (MTA/USDT,
        # MTA/USDC, MTA/USD) doesn't take all five slots -- combine its
        # volume across pairs and keep the highest-volume pair as the label.
        seen: dict[str, dict] = {}
        for entry in out:
            ex = entry["exchange"]
            keep = seen.get(ex)
            if keep is None:
                seen[ex] = dict(entry)
            else:
                keep["volume_usd"] += entry["volume_usd"]
        deduped = sorted(seen.values(), key=lambda x: x["volume_usd"], reverse=True)
        await self._cache_set(key, deduped, Config.REAL_MARKET_CACHE_TTL_TICKERS)
        return deduped[:limit]

    async def get_global(self) -> dict:
        """Global crypto market summary: total market cap (USD), 24h volume,
        MTA/ARC dominance, active cryptocurrencies + market count, 24h
        market-cap change %. Mirrors CoinGecko's ``/global`` payload."""
        key = "realmarket:global"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached
        raw = await self._get("/global")
        data = (raw or {}).get("data") or {} if isinstance(raw, dict) else {}
        out = {
            "active_cryptocurrencies": data.get("active_cryptocurrencies"),
            "markets": data.get("markets"),
            "total_market_cap_usd": (data.get("total_market_cap") or {}).get("usd"),
            "total_volume_usd":     (data.get("total_volume") or {}).get("usd"),
            "market_cap_percentage": data.get("market_cap_percentage") or {},
            "market_cap_change_pct_24h_usd": data.get("market_cap_change_percentage_24h_usd"),
            "updated_at": data.get("updated_at"),
        }
        await self._cache_set(key, out, Config.REAL_MARKET_CACHE_TTL_GLOBAL)
        return out

    async def get_markets(
        self, *, order: str = "market_cap_desc", per_page: int = 25, page: int = 1,
    ) -> list[dict]:
        """List of coins from ``/coins/markets`` sorted by ``order``.

        Supported orders (per CoinGecko): ``market_cap_desc``,
        ``market_cap_asc``, ``volume_desc``, ``volume_asc``,
        ``id_asc``, ``id_desc``. For top gainers / losers we pull the
        market_cap_desc top-250 and re-sort locally on
        ``price_change_percentage_24h`` because CoinGecko's free tier
        doesn't expose a 24h-change sort.
        """
        per_page = max(1, min(int(per_page), 250))
        page = max(1, int(page))
        key = f"realmarket:markets:{order}:{per_page}:{page}"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached
        raw = await self._get(
            "/coins/markets",
            params={
                "vs_currency": "usd",
                "order": order,
                "per_page": per_page,
                "page": page,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            },
        )
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for c in raw:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            out.append({
                "id": c.get("id"),
                "symbol": (c.get("symbol") or "").upper(),
                "name": c.get("name") or c.get("id"),
                "image": c.get("image") or "",
                "price": c.get("current_price"),
                "market_cap": c.get("market_cap"),
                "market_cap_rank": c.get("market_cap_rank"),
                "fdv": c.get("fully_diluted_valuation"),
                "total_volume": c.get("total_volume"),
                "high_24h": c.get("high_24h"),
                "low_24h":  c.get("low_24h"),
                "pct_1h":   c.get("price_change_percentage_1h_in_currency"),
                "pct_24h":  c.get("price_change_percentage_24h_in_currency"),
                "pct_7d":   c.get("price_change_percentage_7d_in_currency"),
                "ath": c.get("ath"),
                "ath_change_pct": c.get("ath_change_percentage"),
            })
        await self._cache_set(key, out, Config.REAL_MARKET_CACHE_TTL_MARKETS)
        return out

    async def get_trending(self) -> list[dict]:
        """Top 7-15 trending coins on CoinGecko (most searched in 24h)."""
        key = "realmarket:trending"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached
        raw = await self._get("/search/trending")
        coins = (raw or {}).get("coins") or [] if isinstance(raw, dict) else []
        out: list[dict] = []
        for c in coins:
            item = c.get("item") if isinstance(c, dict) else None
            if not isinstance(item, dict):
                continue
            data = item.get("data") or {}
            out.append({
                "id":     item.get("id"),
                "symbol": (item.get("symbol") or "").upper(),
                "name":   item.get("name") or item.get("id"),
                "thumb":  item.get("thumb") or item.get("small") or "",
                "market_cap_rank": item.get("market_cap_rank"),
                "price_btc": item.get("price_btc"),
                "price_usd": data.get("price"),
                "pct_24h":   ((data.get("price_change_percentage_24h") or {}).get("usd")
                              if isinstance(data.get("price_change_percentage_24h"), dict)
                              else None),
                "market_cap": data.get("market_cap"),
                "total_volume": data.get("total_volume"),
            })
        await self._cache_set(key, out, Config.REAL_MARKET_CACHE_TTL_TRENDING)
        return out

    async def get_fear_greed(self) -> dict | None:
        """Crypto Fear & Greed Index from alternative.me (free public API).

        Returns ``{"value", "classification", "timestamp",
        "yesterday_value", "week_ago_value", "month_ago_value"}`` or
        ``None`` if the API is unreachable. Different host from CoinGecko
        so it uses its own request path.
        """
        key = "realmarket:feargreed"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached
        sess = await self._get_session()
        url = Config.REAL_MARKET_FNG_BASE
        try:
            async with sess.get(url, params={"limit": "31"}) as resp:
                if resp.status != 200:
                    log.warning("fng api -> %s", resp.status)
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("fng api network error: %s", exc)
            return None
        arr = (data or {}).get("data") or []
        if not arr:
            return None
        today = arr[0]
        try:
            out = {
                "value": int(today.get("value", 0)),
                "classification": today.get("value_classification") or "",
                "timestamp": int(today.get("timestamp") or 0),
                "yesterday_value": (int(arr[1]["value"]) if len(arr) > 1 else None),
                "week_ago_value":  (int(arr[7]["value"]) if len(arr) > 7 else None),
                "month_ago_value": (int(arr[30]["value"]) if len(arr) > 30 else None),
            }
        except (TypeError, ValueError, KeyError):
            return None
        await self._cache_set(key, out, Config.REAL_MARKET_CACHE_TTL_FNG)
        return out

    async def get_simple_price(self, ids: list[str], vs: str = "usd") -> dict[str, float]:
        """Return a flat ``{coin_id: price_in_vs}`` map. Used by ``$convert``.

        Uses CoinGecko's ``/simple/price`` which is cheap and supports
        multiple ids in one request.
        """
        if not ids:
            return {}
        joined = ",".join(sorted({i.lower() for i in ids if i}))
        key = f"realmarket:simple:{vs.lower()}:{joined}"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached
        raw = await self._get(
            "/simple/price",
            params={"ids": joined, "vs_currencies": vs.lower()},
        )
        out: dict[str, float] = {}
        if isinstance(raw, dict):
            for cid, payload in raw.items():
                if not isinstance(payload, dict):
                    continue
                try:
                    out[cid] = float(payload.get(vs.lower()) or 0.0)
                except (TypeError, ValueError):
                    continue
        await self._cache_set(key, out, Config.REAL_MARKET_CACHE_TTL_OVIEW)
        return out
