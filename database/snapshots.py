"""database/snapshots.py - Economy snapshot repo for rollback support."""
from __future__ import annotations

import json
import logging
import math
from decimal import Decimal
from datetime import datetime

from core.config import Config
from .base import PgBaseRepo

log = logging.getLogger(__name__)

# All stone table names
_STONE_TABLES = ("hashstones", "lockstones", "vaultstones", "liqstones")


def _ensure_list(val: object) -> list:
    """Decode a JSONB column that may have been double-encoded as a string."""
    if isinstance(val, str):
        val = json.loads(val)
    return val if isinstance(val, list) else []


def _safe_float(value, field: str, default: float = 0.0) -> float:
    """Convert snapshot JSON value to a safe non-negative finite float."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        log.warning("restore: non-numeric %s=%r, using %s", field, value, default)
        return default
    if not math.isfinite(v):
        log.warning("restore: non-finite %s=%r, using %s", field, value, default)
        return default
    if v < 0:
        log.warning("restore: negative %s=%r, clamping to 0", field, v)
        return 0.0
    return v


class PgSnapshotsRepo(PgBaseRepo):

    async def take_snapshot(self, guild_id: int) -> int:
        """Capture current economy state for a guild. Returns the snapshot id."""
        wallets = await self.fetch_all(
            "SELECT user_id, wallet, bank FROM users WHERE guild_id=$1",
            guild_id,
        )
        crypto_holdings = await self.fetch_all(
            "SELECT user_id, symbol, amount FROM crypto_holdings WHERE guild_id=$1 AND amount > 0",
            guild_id,
        )
        wallet_holdings = await self.fetch_all(
            "SELECT user_id, network, symbol, amount FROM wallet_holdings WHERE guild_id=$1 AND amount > 0",
            guild_id,
        )
        prices = await self.fetch_all(
            "SELECT symbol, price, circulating_supply, ath FROM crypto_prices WHERE guild_id=$1",
            guild_id,
        )
        pools = await self.fetch_all(
            "SELECT pool_id, token_a, token_b, reserve_a, reserve_b, total_lp FROM pools WHERE guild_id=$1",
            guild_id,
        )

        # Capture all stone tables with table name tagged so restore knows which table
        all_stones: list[dict] = []
        for table in _STONE_TABLES:
            rows = await self.fetch_all(
                f"SELECT user_id, level, xp, staked_amount FROM {table} WHERE guild_id=$1",  # noqa: S608
                guild_id,
            )
            for r in rows:
                all_stones.append({"table": table, **dict(r)})

        lp_positions = await self.fetch_all(
            "SELECT user_id, pool_id, lp_shares FROM lp_positions WHERE guild_id=$1",
            guild_id,
        )

        def _to_plain(rows: list) -> list:
            """Convert PgRow/Record objects to plain dicts with JSON-safe types."""
            return [
                {k: float(v) if isinstance(v, Decimal) else v for k, v in (dict(r) if not isinstance(r, dict) else r).items()}
                for r in rows
            ]

        row = await self.fetch_one(
            """INSERT INTO economy_snapshots
               (guild_id, wallets, crypto_holdings, wallet_holdings, prices, pools, stones, lp_positions)
               VALUES ($1, $2::jsonb, $3::jsonb, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb)
               RETURNING id""",
            guild_id,
            json.dumps(_to_plain(wallets)),
            json.dumps(_to_plain(crypto_holdings)),
            json.dumps(_to_plain(wallet_holdings)),
            json.dumps(_to_plain(prices)),
            json.dumps(_to_plain(pools)),
            json.dumps(_to_plain(all_stones)),
            json.dumps(_to_plain(lp_positions)),
        )
        snapshot_id = row["id"]

        # Prune old snapshots beyond retention limit
        await self.execute(
            """DELETE FROM economy_snapshots
               WHERE guild_id=$1 AND id NOT IN (
                   SELECT id FROM economy_snapshots
                   WHERE guild_id=$1
                   ORDER BY taken_at DESC
                   LIMIT $2
               )""",
            guild_id, Config.SNAPSHOT_KEEP,
        )

        return snapshot_id

    async def list_snapshots(self, guild_id: int, limit: int = 10) -> list[dict]:
        """Return recent snapshots (id + taken_at only, no payload)."""
        return await self.fetch_all(
            """SELECT id, taken_at,
                      jsonb_array_length(wallets)          AS user_count,
                      jsonb_array_length(prices)           AS price_count,
                      jsonb_array_length(pools)            AS pool_count
               FROM economy_snapshots
               WHERE guild_id=$1
               ORDER BY taken_at DESC
               LIMIT $2""",
            guild_id, limit,
        )

    async def get_nearest_snapshot(self, guild_id: int, target_ts: datetime) -> dict | None:
        """Return the snapshot closest in time to target_ts (full payload)."""
        return await self.fetch_one(
            """SELECT * FROM economy_snapshots
               WHERE guild_id=$1
               ORDER BY ABS(EXTRACT(EPOCH FROM (taken_at - $2)))
               LIMIT 1""",
            guild_id, target_ts,
        )

    async def get_snapshot(self, guild_id: int, snapshot_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM economy_snapshots WHERE id=$1 AND guild_id=$2",
            snapshot_id, guild_id,
        )

    async def restore_snapshot(self, guild_id: int, snapshot_id: int) -> dict:
        """
        Atomically restore economy state from a snapshot.
        Covers wallets, all token holdings, prices, pools, stones, and LP positions
        so no asset purchased between snapshot and rollback can be double-kept.
        Returns a summary dict with counts of rows restored.
        """
        snap = await self.get_snapshot(guild_id, snapshot_id)
        if not snap:
            raise ValueError(f"Snapshot {snapshot_id} not found for guild {guild_id}")

        wallets:         list[dict] = _ensure_list(snap["wallets"])
        crypto_holdings: list[dict] = _ensure_list(snap["crypto_holdings"])
        wallet_holdings: list[dict] = _ensure_list(snap["wallet_holdings"])
        prices:          list[dict] = _ensure_list(snap["prices"])
        pools:           list[dict] = _ensure_list(snap["pools"])
        stones:          list[dict] = _ensure_list(snap.get("stones"))
        lp_pos:          list[dict] = _ensure_list(snap.get("lp_positions"))

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Lock all economy tables exclusively before touching any row.
                # Any concurrent spend that hasn't committed yet will block here
                # and execute after the restore completes (with the restored balance).
                # Any spend that committed before we lock gets overwritten below.
                await conn.execute(
                    """LOCK TABLE
                           users,
                           wallet_holdings,
                           crypto_holdings,
                           crypto_prices,
                           pools,
                           hashstones, lockstones, vaultstones, liqstones,
                           lp_positions
                       IN EXCLUSIVE MODE"""
                )

                # ── Wallets ────────────────────────────────────────────────
                for row in wallets:
                    await conn.execute(
                        "UPDATE users SET wallet=$1, bank=$2 WHERE user_id=$3 AND guild_id=$4",
                        _safe_float(row["wallet"], "wallet"),
                        _safe_float(row["bank"], "bank"),
                        int(row["user_id"]), guild_id,
                    )

                # ── CeFi crypto holdings ───────────────────────────────────
                await conn.execute(
                    "DELETE FROM crypto_holdings WHERE guild_id=$1", guild_id
                )
                if crypto_holdings:
                    await conn.executemany(
                        """INSERT INTO crypto_holdings (user_id, guild_id, symbol, amount)
                           VALUES ($1, $2, $3, $4)""",
                        [
                            (int(r["user_id"]), guild_id, r["symbol"], _safe_float(r["amount"], "crypto_amount"))
                            for r in crypto_holdings
                        ],
                    )

                # ── DeFi wallet holdings ───────────────────────────────────
                await conn.execute(
                    "DELETE FROM wallet_holdings WHERE guild_id=$1", guild_id
                )
                if wallet_holdings:
                    await conn.executemany(
                        """INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
                           VALUES ($1, $2, $3, $4, $5)""",
                        [
                            (int(r["user_id"]), guild_id, r["network"], r["symbol"], _safe_float(r["amount"], "wallet_amount"))
                            for r in wallet_holdings
                        ],
                    )

                # ── Prices + circulating supply ────────────────────────────
                for row in prices:
                    _price = _safe_float(row["price"], "price", default=0.0001)
                    await conn.execute(
                        """UPDATE crypto_prices
                           SET price=$1, circulating_supply=$2, ath=$3
                           WHERE symbol=$4 AND guild_id=$5""",
                        _price,
                        _safe_float(row["circulating_supply"], "circulating_supply"),
                        max(_price, _safe_float(row["ath"], "ath", default=_price)),
                        row["symbol"], guild_id,
                    )

                # ── Pool reserves ──────────────────────────────────────────
                for row in pools:
                    await conn.execute(
                        """UPDATE pools
                           SET reserve_a=$1, reserve_b=$2, total_lp=$3
                           WHERE pool_id=$4 AND guild_id=$5""",
                        _safe_float(row["reserve_a"], "reserve_a"),
                        _safe_float(row["reserve_b"], "reserve_b"),
                        _safe_float(row["total_lp"], "total_lp"),
                        row["pool_id"], guild_id,
                    )

                # ── Stones (all tables) ────────────────────────────────────
                # Delete all stones for each table, then restore snapshot state.
                # This prevents keeping a stone purchased between snapshot and rollback
                # while also getting the wallet refunded.
                for table in _STONE_TABLES:
                    await conn.execute(
                        f"DELETE FROM {table} WHERE guild_id=$1",  # noqa: S608
                        guild_id,
                    )
                table_rows: dict[str, list] = {t: [] for t in _STONE_TABLES}
                for s in stones:
                    t = s.get("table")
                    if t in table_rows:
                        table_rows[t].append(s)
                for table, rows in table_rows.items():
                    if rows:
                        await conn.executemany(
                            f"""INSERT INTO {table} (user_id, guild_id, level, xp, staked_amount)
                                VALUES ($1, $2, $3, $4, $5)""",  # noqa: S608
                            [
                                (
                                    int(r["user_id"]), guild_id,
                                    max(1, int(r["level"] or 1)),
                                    _safe_float(r["xp"], "xp"),
                                    _safe_float(r["staked_amount"], "staked_amount"),
                                )
                                for r in rows
                            ],
                        )

                # ── LP positions ───────────────────────────────────────────
                await conn.execute(
                    "DELETE FROM lp_positions WHERE guild_id=$1", guild_id
                )
                if lp_pos:
                    await conn.executemany(
                        """INSERT INTO lp_positions (user_id, guild_id, pool_id, lp_shares)
                           VALUES ($1, $2, $3, $4)""",
                        [
                            (int(r["user_id"]), guild_id, r["pool_id"], _safe_float(r["lp_shares"], "lp_shares"))
                            for r in lp_pos
                        ],
                    )

        return {
            "snapshot_id":     snapshot_id,
            "taken_at":        snap["taken_at"],
            "users_restored":  len(wallets),
            "prices_restored": len(prices),
            "pools_restored":  len(pools),
            "stones_restored": len(stones),
            "lp_restored":     len(lp_pos),
        }
