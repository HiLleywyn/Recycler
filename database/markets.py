"""Markets repository (PostgreSQL)  -  prices, candles, tokens, networks."""
from __future__ import annotations

from datetime import datetime, timezone

from core.config import Config
from .base import PgBaseRepo


class PgMarketsRepo(PgBaseRepo):

    # ── Crypto Prices ──────────────────────────────────────────────────────

    async def seed_prices(self, guild_id: int) -> None:
        # Seed all tokens in TOKENS (includes stablecoins USDC/DSD)
        for symbol, data in Config.TOKENS.items():
            p = data["start_price"]
            await self.execute(
                """INSERT INTO crypto_prices
                   (symbol, guild_id, price, open_price, day_high, day_low, ath)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT DO NOTHING""",
                symbol, guild_id, p, p, p, p, p,
            )
            # Initialize circulating_supply to 50% of max_supply for built-in tokens
            max_sup = data.get("max_supply")
            if max_sup:
                await self.execute(
                    """UPDATE crypto_prices
                       SET circulating_supply = $1
                       WHERE symbol=$2 AND guild_id=$3 AND circulating_supply=0""",
                    max_sup * 0.5, symbol, guild_id,
                )

    async def get_price(self, symbol: str, guild_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM crypto_prices WHERE symbol=$1 AND guild_id=$2",
            symbol, guild_id,
        )

    async def get_all_prices(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM crypto_prices WHERE guild_id=$1 ORDER BY symbol",
            guild_id,
        )

    async def update_price(self, symbol: str, guild_id: int, new_price: float) -> None:
        # Stablecoins are hard-pegged to $1  -  reject any external price change.
        # This covers buy/sell price impact, MM nudges, and manual setprice.
        tok_cfg = Config.TOKENS.get(symbol, {})
        if tok_cfg.get("stablecoin") or tok_cfg.get("consensus") == "Fiat":
            new_price = tok_cfg.get("start_price", 1.0)

        # Re-anchor open_price when a write punches outside the daily drift band
        # (e.g. a whale-sized trade impact). Without this, the drift tick's daily
        # circuit breaker in gbm_step would snap the oracle right back to
        # open*(1 ± max_drift) within one PRICE_TICK_SECONDS, undoing the trade.
        # Drift-tick writes are pre-clamped by gbm_step itself, so they never
        # trigger this branch  -  only out-of-band writes rebase the anchor.
        row = await self.fetch_one(
            "SELECT open_price FROM crypto_prices WHERE symbol=$1 AND guild_id=$2",
            symbol, guild_id,
        )
        open_p = float(row["open_price"]) if row and row.get("open_price") else 0.0
        cap = float(Config.ORACLE_DAILY_MAX_DRIFT)
        if open_p > 0 and (
            new_price > open_p * (1.0 + cap) or new_price < open_p * (1.0 - cap)
        ):
            new_open = float(new_price)
        else:
            new_open = open_p

        await self.execute(
            """UPDATE crypto_prices
               SET price=$1,
                   open_price = $4,
                   day_high = GREATEST(day_high, $1),
                   day_low  = LEAST(day_low, $1),
                   ath      = GREATEST(COALESCE(ath, 0), $1)
               WHERE symbol=$2 AND guild_id=$3""",
            new_price, symbol, guild_id, new_open,
        )

    # ── Admin price events (pump scheduler) ───────────────────────────────
    # Backing store for ``cogs/trade.py::_admin_price_events``. Persisted so
    # a deploy or restart in the middle of a pump doesn't silently freeze
    # the chart -- ``Trade.cog_load`` rehydrates the in-memory dict from
    # this table on startup and ``drift_task`` deletes finished events.

    async def upsert_admin_price_event(
        self, guild_id: int, symbol: str, ev: dict,
    ) -> None:
        await self.execute(
            """INSERT INTO admin_price_events
               (guild_id, symbol, pattern, magnitude_pct, seed,
                start_price, start_ts, end_ts)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (guild_id, symbol) DO UPDATE SET
                   pattern       = EXCLUDED.pattern,
                   magnitude_pct = EXCLUDED.magnitude_pct,
                   seed          = EXCLUDED.seed,
                   start_price   = EXCLUDED.start_price,
                   start_ts      = EXCLUDED.start_ts,
                   end_ts        = EXCLUDED.end_ts""",
            guild_id, symbol, ev["pattern"], float(ev["magnitude_pct"]),
            int(ev["seed"]), float(ev["start_price"]),
            float(ev["start_ts"]), float(ev["end_ts"]),
        )

    async def delete_admin_price_event(self, guild_id: int, symbol: str) -> None:
        await self.execute(
            "DELETE FROM admin_price_events WHERE guild_id=$1 AND symbol=$2",
            guild_id, symbol,
        )

    async def load_admin_price_events(self) -> list[dict]:
        return await self.fetch_all("SELECT * FROM admin_price_events")

    async def reset_daily_stats(self, guild_id: int) -> None:
        await self.execute(
            """UPDATE crypto_prices
               SET open_price=price, day_high=price, day_low=price
               WHERE guild_id=$1""",
            guild_id,
        )

    async def reset_daily_prices(self, guild_id: int) -> None:
        """Reset open_price, day_high, day_low to current price at UTC midnight."""
        await self.execute(
            """UPDATE crypto_prices
               SET open_price=price, day_high=price, day_low=price
               WHERE guild_id=$1""",
            guild_id,
        )

    # ── Price Candles ──────────────────────────────────────────────────────

    async def upsert_candle(
        self, guild_id: int, symbol: str, ts: int,
        open_: float, high: float, low: float, close: float,
        volume_delta: float = 0.0,
    ) -> None:
        """INSERT a new candle or UPDATE high/low/close/volume of existing."""
        ts_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        await self.execute(
            """INSERT INTO price_candles (guild_id, symbol, ts, open, high, low, close, volume)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT(guild_id, symbol, ts) DO UPDATE SET
                   high   = GREATEST(price_candles.high, EXCLUDED.high),
                   low    = LEAST(price_candles.low, EXCLUDED.low),
                   close  = EXCLUDED.close,
                   volume = price_candles.volume + EXCLUDED.volume""",
            guild_id, symbol, ts_dt, open_, high, low, close, volume_delta,
        )

    async def add_trade_volume(
        self, guild_id: int, symbol: str, volume_usd: float,
    ) -> None:
        """Add trade volume (in USD) to the current 1-minute candle without touching OHLC."""
        if volume_usd <= 0:
            return
        import time as _time
        ts = int(_time.time()) // 60 * 60  # round to current minute
        ts_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        await self.execute(
            """UPDATE price_candles SET volume = volume + $1
               WHERE guild_id=$2 AND symbol=$3 AND ts=$4""",
            volume_usd, guild_id, symbol, ts_dt,
        )

    async def get_candles(self, guild_id: int, symbol: str, since_ts: int, limit: int = 500) -> list[dict]:
        since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
        return await self.fetch_all(
            """SELECT * FROM price_candles
               WHERE guild_id=$1 AND symbol=$2 AND ts >= $3
               ORDER BY ts ASC LIMIT $4""",
            guild_id, symbol, since_dt, limit,
        )

    async def get_latest_candle(self, guild_id: int, symbol: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM price_candles WHERE guild_id=$1 AND symbol=$2 ORDER BY ts DESC LIMIT 1",
            guild_id, symbol,
        )

    async def get_twap(self, symbol: str, guild_id: int, window: int = 80) -> tuple[float, float]:
        """Return (twap, stddev) from the last `window` candle closes.

        Used by the oracle for TWAP-anchored pricing and Bollinger regime caps.
        Returns (0.0, 0.0) if insufficient data.
        """
        rows = await self.fetch_all(
            "SELECT close FROM price_candles WHERE guild_id=$1 AND symbol=$2 ORDER BY ts DESC LIMIT $3",
            guild_id, symbol, window,
        )
        if not rows or len(rows) < 2:
            return 0.0, 0.0
        closes = [float(r["close"]) for r in rows]
        n = len(closes)
        mean = sum(closes) / n
        variance = sum((c - mean) ** 2 for c in closes) / n
        stddev = variance ** 0.5
        return mean, stddev

    async def get_all_twaps(self, guild_id: int, window: int = 80) -> dict[str, tuple[float, float]]:
        """Return {symbol: (twap, stddev)} for all symbols in one query.

        Replaces per-token get_twap calls in the price drift loop  -  reduces
        N sequential queries to a single batch query per guild per tick.
        Returns only symbols with >= 2 candles (insufficient data excluded).
        """
        rows = await self.fetch_all(
            """
            SELECT symbol,
                   AVG(close)          AS twap,
                   STDDEV_POP(close)   AS stddev,
                   COUNT(*)            AS n
            FROM (
                SELECT symbol, close,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
                FROM price_candles
                WHERE guild_id = $1
            ) sub
            WHERE rn <= $2
            GROUP BY symbol
            HAVING COUNT(*) >= 2
            """,
            guild_id, window,
        )
        return {
            r["symbol"]: (float(r["twap"]), float(r["stddev"] or 0.0))
            for r in rows
        }

    # ── Dynamic Guild Tokens ───────────────────────────────────────────────

    async def get_all_tokens_for_guild(self, guild_id: int) -> dict:
        """Returns merged dict of Config.TOKENS + guild custom tokens.
        Built-in tokens always take precedence  -  custom entries cannot override them."""
        merged = dict(Config.TOKENS)
        guild_token_rows = await self.fetch_all(
            "SELECT * FROM guild_tokens WHERE guild_id=$1", guild_id,
        )
        for t in guild_token_rows:
            sym = t["symbol"]
            if sym in Config.TOKENS:
                # Never let a guild_tokens row shadow a built-in token
                continue
            merged[sym] = {
                "name": t["name"], "emoji": t["emoji"],
                "consensus": t["consensus"], "network": t["network"],
                "start_price": t["start_price"], "daily_vol": t["daily_vol"],
                "token_type": t.get("token_type", "utility"),
                "max_supply": t.get("max_supply"),
                "circulating_supply": t.get("circulating_supply", 0.0),
                "vault_locked": bool(t.get("vault_locked", False)),
            }
        return merged

    async def update_circulating_supply(self, guild_id: int, symbol: str, delta: float) -> float:
        """Atomically increment (positive) or decrement (negative) circulating supply.
        Returns the new value. Only applies to custom guild tokens."""
        await self.execute(
            "UPDATE guild_tokens SET circulating_supply = GREATEST(0, circulating_supply + $1) "
            "WHERE guild_id=$2 AND symbol=$3",
            delta, guild_id, symbol,
        )
        row = await self.fetch_one(
            "SELECT circulating_supply FROM guild_tokens WHERE guild_id=$1 AND symbol=$2",
            guild_id, symbol,
        )
        return float(row["circulating_supply"]) if row else 0.0

    async def update_builtin_circulating_supply(
        self, guild_id: int, symbol: str, delta: float
    ) -> float:
        """Atomically update circulating_supply in crypto_prices (built-in tokens like SUN).
        Returns the new circulating supply. Clamps to >= 0."""
        await self.execute(
            """UPDATE crypto_prices
               SET circulating_supply = GREATEST(0, circulating_supply + $1)
               WHERE symbol=$2 AND guild_id=$3""",
            delta, symbol, guild_id,
        )
        row = await self.fetch_one(
            "SELECT circulating_supply FROM crypto_prices WHERE symbol=$1 AND guild_id=$2",
            symbol, guild_id,
        )
        return float(row["circulating_supply"]) if row else 0.0

    async def get_token_network(self, guild_id: int, symbol: str) -> str | None:
        """Return the network name for a custom token, or None if not found."""
        row = await self.fetch_one(
            "SELECT network FROM guild_tokens WHERE guild_id=$1 AND symbol=$2",
            guild_id, symbol,
        )
        return row["network"] if row else None

    async def get_network_accepted_tokens(self, guild_id: int, network: str) -> list[str]:
        """Return symbols accepted by wallets on a given network (built-in + custom)."""
        # Built-in tokens for this network
        built_in = [
            sym for sym, t in Config.TOKENS.items()
            if t.get("network") == network
        ]
        # Custom tokens registered for this network
        rows = await self.fetch_all(
            "SELECT symbol FROM network_accepted_tokens WHERE guild_id=$1 AND network=$2",
            guild_id, network,
        )
        custom = [r["symbol"] for r in rows]
        return list(dict.fromkeys(built_in + custom))  # preserve order, dedupe

    async def add_token_to_network_wallet(
        self, guild_id: int, network: str, symbol: str
    ) -> None:
        """Register a custom token as accepted by a network's wallets."""
        await self.execute(
            """INSERT INTO network_accepted_tokens (guild_id, network, symbol)
               VALUES ($1,$2,$3)
               ON CONFLICT DO NOTHING""",
            guild_id, network, symbol,
        )
