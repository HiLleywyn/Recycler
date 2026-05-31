"""Transactions repository (PostgreSQL)  -  ledger, tx history, chain block tagging."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_MAX_AMOUNT = 10**33  # sanity cap: 1e15 human tokens * 10^18 scale = 10^33 raw units

# Gambling stats cutoff: the "payout recorded as post-game wallet balance"
# bug was fixed in c87928c on 2026-04-12 15:59:11 UTC. Rows before that may
# record the entire wallet balance in amount_out, which turns into multi-
# billion dollar "Worst Loss" entries and poisons every aggregate stat.
# Filter those out at query time instead of deleting (preserves the raw
# ledger but hides the noise). Gambling has no max bet, so a pure amount
# threshold would clip legitimate whale games -- a timestamp cutoff is the
# only clean line.
_GAMBLE_STATS_CUTOFF = "2026-04-12 16:00:00+00"


def _sanitize_amount(value: int | None, field: str) -> int | None:
    """Clamp and validate a transaction amount (raw scaled integer). Caps extremes."""
    if value is None:
        return None
    if not isinstance(value, int):
        raise TypeError(
            f"log_tx {field} must be a raw-scaled int, got {type(value).__name__}={value!r}. "
            "Call to_raw() before passing human-scale floats to the ledger."
        )
    if value < 0:
        log.error("log_tx received negative %s=%r - clamping to 0", field, value)
        return 0
    if value > _MAX_AMOUNT:
        log.error("log_tx received unreasonably large %s=%r - clamping to cap", field, value)
        return _MAX_AMOUNT
    return value

from core.config import Config
from .base import PgBaseRepo


class PgTransactionsRepo(PgBaseRepo):

    def _make_tx_hash(
        self, guild_id: int, user_id: int | None, tx_type: str,
        amount_in: int | None, amount_out: int | None, ts: float
    ) -> str:
        nonce = secrets.token_hex(8)  # 64-bit random nonce prevents same-millisecond collisions
        raw = json.dumps([guild_id, user_id, tx_type, amount_in, amount_out, f"{ts:.3f}", nonce])
        return hashlib.sha256(f"{Config.TX_SALT}:{raw}".encode()).hexdigest()  # full 64-char hash

    async def log_tx(
        self, guild_id: int, user_id: int | None, tx_type: str,
        symbol_in: str | None = None, amount_in: int | None = None,
        symbol_out: str | None = None, amount_out: int | None = None,
        price_at: float | None = None,
        network: str = "",   # optional network prefix e.g. "sun", "arc", "sol", "bnb"
        gas_fee: int = 0,
        gas_coin: str = "",
    ) -> str:
        amount_in  = _sanitize_amount(amount_in,  "amount_in")
        amount_out = _sanitize_amount(amount_out, "amount_out")
        if price_at is not None and not math.isfinite(price_at):
            log.error("log_tx received non-finite price_at=%r for %s - setting None", price_at, tx_type)
            price_at = None

        ts = time.time()
        raw_hash = self._make_tx_hash(guild_id, user_id, tx_type, amount_in, amount_out, ts)
        prefix = f"{network.lower()}:" if network else ""
        tx_hash = prefix + raw_hash
        ts_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        await self.execute(
            """INSERT INTO transactions
               (tx_hash, guild_id, user_id, tx_type, symbol_in, amount_in, symbol_out, amount_out, price_at, gas_fee, gas_coin, ts)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
               ON CONFLICT DO NOTHING""",
            tx_hash, guild_id, user_id, tx_type, symbol_in, amount_in,
            symbol_out, amount_out, price_at, gas_fee or 0, gas_coin or "", ts_dt,
        )
        return tx_hash

    async def get_tx(self, tx_hash: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM transactions WHERE tx_hash=$1", tx_hash
        )

    async def get_user_tx_history(self, user_id: int, guild_id: int, limit: int = 20) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM transactions WHERE user_id=$1 AND guild_id=$2 ORDER BY ts DESC LIMIT $3",
            user_id, guild_id, limit,
        )

    async def get_guild_tx_history(self, guild_id: int, limit: int = 50) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM transactions WHERE guild_id=$1 ORDER BY ts DESC LIMIT $2",
            guild_id, limit,
        )

    async def get_pending_txns_since(self, guild_id: int, since_ts: float, network: str | None = None) -> list[dict]:
        """All transactions since since_ts that haven't been assigned a block yet.
        If network is provided, filters to transactions whose tx_hash starts with '<network>:'."""
        since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
        if network:
            return await self.fetch_all(
                "SELECT tx_hash, ts FROM transactions WHERE guild_id=$1 AND ts > $2 AND block_num IS NULL "
                "AND tx_hash LIKE $3 ORDER BY ts ASC",
                guild_id, since_dt, f"{network}:%",
            )
        return await self.fetch_all(
            "SELECT tx_hash, ts FROM transactions WHERE guild_id=$1 AND ts > $2 AND block_num IS NULL ORDER BY ts ASC",
            guild_id, since_dt,
        )

    async def tag_transactions_with_block(
        self, guild_id: int, block_num: int, since_ts: float,
        network: str | None = None, tx_hashes: list[str] | None = None
    ) -> int:
        """Tag specific transactions with the given block_num.

        If tx_hashes is provided, tags ONLY those exact hashes (precise  -  avoids
        tagging transactions that arrive during bundling and stealing them from the
        next block). Falls back to the time-range query when tx_hashes is None."""
        if tx_hashes:
            # Tag only the exact set we bundled  -  no race window
            placeholders = ",".join(f"${i}" for i in range(3, 3 + len(tx_hashes)))
            status = await self.execute(
                f"UPDATE transactions SET block_num=$1 WHERE guild_id=$2 AND tx_hash IN ({placeholders})",
                block_num, guild_id, *tx_hashes,
            )
        elif network:
            since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
            status = await self.execute(
                "UPDATE transactions SET block_num=$1 WHERE guild_id=$2 AND ts > $3 AND block_num IS NULL "
                "AND tx_hash LIKE $4",
                block_num, guild_id, since_dt, f"{network}:%",
            )
        else:
            since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
            status = await self.execute(
                "UPDATE transactions SET block_num=$1 WHERE guild_id=$2 AND ts > $3 AND block_num IS NULL",
                block_num, guild_id, since_dt,
            )
        return self._row_count(status)

    async def get_transaction(self, guild_id: int, tx_hash: str) -> dict | None:
        """Fetch a single transaction by its hash (exact match)."""
        return await self.fetch_one(
            "SELECT * FROM transactions WHERE guild_id=$1 AND tx_hash=$2",
            guild_id, tx_hash,
        )

    async def get_gambling_stats(
        self, user_id: int, guild_id: int, *,
        since: datetime | None = None,
        game_type: str | None = None,
    ) -> list[dict]:
        """Return per-game gambling statistics for a user.

        Each row: game (str), total_games, wins, losses, net_pnl, total_wagered,
        win_rate (0-1), biggest_win, biggest_loss.

        Parameters
        ----------
        since: Only include transactions on or after this timestamp.
        game_type: Filter to a single game (e.g. "COINFLIP", "DICE").
        """
        clauses = [
            "user_id=$1", "guild_id=$2", "tx_type LIKE 'GAMBLE_%'",
            # Skip pre-payout-fix rows (see _GAMBLE_STATS_CUTOFF above).
            f"ts >= '{_GAMBLE_STATS_CUTOFF}'::timestamptz",
        ]
        params: list = [user_id, guild_id]
        idx = 3
        if since is not None:
            clauses.append(f"ts >= ${idx}")
            params.append(since)
            idx += 1
        if game_type is not None:
            clauses.append(f"tx_type = ${idx}")
            params.append(f"GAMBLE_{game_type.upper()}")
            idx += 1
        where = " AND ".join(clauses)
        rows = await self.fetch_all(
            f"""
            SELECT
                REPLACE(tx_type, 'GAMBLE_', '') AS game,
                COUNT(*)                        AS total_games,
                SUM(CASE WHEN (amount_out - amount_in) > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN (amount_out - amount_in) <= 0 THEN 1 ELSE 0 END) AS losses,
                SUM(amount_out - amount_in)     AS net_pnl,
                SUM(amount_in)                  AS total_wagered,
                MAX(CASE WHEN (amount_out - amount_in) > 0 THEN (amount_out - amount_in) ELSE 0 END) AS biggest_win,
                MIN(CASE WHEN (amount_out - amount_in) <= 0 THEN (amount_out - amount_in) ELSE 0 END) AS biggest_loss
            FROM transactions
            WHERE {where}
            GROUP BY tx_type
            ORDER BY total_games DESC
            """,
            *params,
        )
        result = []
        for d in rows:
            total = d.get("total_games") or 0
            wins  = d.get("wins") or 0
            d["net_pnl"] = int(d["net_pnl"]) if d.get("net_pnl") is not None else 0
            d["total_wagered"] = int(d["total_wagered"]) if d.get("total_wagered") is not None else 0
            d["biggest_win"] = int(d["biggest_win"]) if d.get("biggest_win") is not None else 0
            d["biggest_loss"] = int(d["biggest_loss"]) if d.get("biggest_loss") is not None else 0
            d["win_rate"] = wins / total if total > 0 else 0.0
            result.append(d)
        return result

    async def get_guild_gambling_stats(self, guild_id: int) -> list[dict]:
        """Return server-wide gambling statistics per game (all users combined)."""
        rows = await self.fetch_all(
            f"""
            SELECT
                REPLACE(tx_type, 'GAMBLE_', '') AS game,
                COUNT(*)                        AS total_games,
                COUNT(DISTINCT user_id)         AS unique_players,
                SUM(CASE WHEN (amount_out - amount_in) > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(amount_out - amount_in)     AS net_pnl,
                SUM(amount_in)                  AS total_wagered,
                MAX(CASE WHEN (amount_out - amount_in) > 0 THEN (amount_out - amount_in) ELSE 0 END) AS biggest_win
            FROM transactions
            WHERE guild_id=$1 AND tx_type LIKE 'GAMBLE_%'
              AND ts >= '{_GAMBLE_STATS_CUTOFF}'::timestamptz
            GROUP BY tx_type
            ORDER BY total_wagered DESC
            """,
            guild_id,
        )
        result = []
        for d in rows:
            total = d.get("total_games") or 0
            wins  = d.get("wins") or 0
            d["net_pnl"] = int(d["net_pnl"]) if d.get("net_pnl") is not None else 0
            d["total_wagered"] = int(d["total_wagered"]) if d.get("total_wagered") is not None else 0
            d["biggest_win"] = int(d["biggest_win"]) if d.get("biggest_win") is not None else 0
            d["win_rate"] = wins / total if total > 0 else 0.0
            result.append(d)
        return result

    async def get_explorer_summary(self, guild_id: int) -> dict:
        """Return aggregated explorer data: recent transfers, large trades, top wallets, etc."""
        # Recent transfers
        recent_transfers = await self.fetch_all(
            """SELECT * FROM transactions WHERE guild_id=$1 AND tx_type IN ('TRANSFER','SEND','token_send')
               ORDER BY ts DESC LIMIT 10""",
            guild_id,
        )

        # Largest trades by USD value
        large_trades = await self.fetch_all(
            """SELECT * FROM transactions
               WHERE guild_id=$1 AND tx_type IN ('BUY','SELL','SWAP')
                 AND amount_in IS NOT NULL
               ORDER BY amount_in DESC LIMIT 10""",
            guild_id,
        )

        # Top wallets by wallet+bank
        large_wallets = await self.fetch_all(
            "SELECT user_id, wallet, bank, (wallet+bank) as total FROM users "
            "WHERE guild_id=$1 ORDER BY total DESC LIMIT 10",
            guild_id,
        )

        # New tokens (custom tokens ordered by created_at desc)
        new_tokens = await self.fetch_all(
            "SELECT * FROM guild_tokens WHERE guild_id=$1 ORDER BY created_at DESC LIMIT 10",
            guild_id,
        )

        # Token stats (24h volume from candles if available)
        token_stats = await self.fetch_all(
            """SELECT symbol_out as symbol, SUM(amount_in) as volume_24h,
                      COUNT(*) as trade_count
               FROM transactions
               WHERE guild_id=$1 AND tx_type IN ('BUY','SELL')
                 AND ts > now() - INTERVAL '1 day'
               GROUP BY symbol_out""",
            guild_id,
        )
        # Wrap Decimal aggregates to int
        for row in token_stats:
            if row.get("volume_24h") is not None:
                row["volume_24h"] = int(row["volume_24h"])

        return {
            "recent_transfers": recent_transfers,
            "large_trades": large_trades,
            "large_wallets": large_wallets,
            "new_tokens": new_tokens,
            "token_stats": token_stats,
        }

    async def get_work_today(self, user_id: int, guild_id: int, since_ts: float) -> int:
        """Return total work earnings (raw scaled int) for this user since since_ts."""
        since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
        row = await self.fetch_one(
            """SELECT COALESCE(SUM(amount_out), 0) as total
               FROM transactions
               WHERE user_id=$1 AND guild_id=$2 AND tx_type='WORK' AND ts >= $3""",
            user_id, guild_id, since_dt,
        )
        return int(row["total"]) if row else 0

    async def get_total_staking_earned(self, user_id: int, guild_id: int) -> int:
        """Return total lifetime staking rewards earned (raw scaled int) by this user."""
        row = await self.fetch_one(
            """SELECT COALESCE(SUM(amount_out), 0) as total
               FROM transactions
               WHERE user_id=$1 AND guild_id=$2 AND tx_type='STAKE_REWARD'""",
            user_id, guild_id,
        )
        return int(row["total"]) if row else 0

    async def get_total_lp_yield_earned(self, user_id: int, guild_id: int) -> int:
        """Return total lifetime LP yield rewards earned (raw scaled int) by this user."""
        row = await self.fetch_one(
            """SELECT COALESCE(SUM(amount_out), 0) as total
               FROM transactions
               WHERE user_id=$1 AND guild_id=$2 AND tx_type='LP_YIELD'""",
            user_id, guild_id,
        )
        return int(row["total"]) if row else 0

    # ── Leaderboards & history (game_results / transactions) ─────────────

    async def get_gambling_leaderboard(
        self, guild_id: int, limit: int = 200, *,
        since: datetime | None = None,
        game_type: str | None = None,
        user_ids: list[int] | None = None,
    ) -> list[dict]:
        """Top gamblers by net P&L from transaction history.

        Parameters
        ----------
        since: Only include transactions on or after this timestamp.
        game_type: Filter to a single game (e.g. "COINFLIP").
        user_ids: Restrict to these users (for group leaderboards).
        """
        clauses = [
            "guild_id = $1", "tx_type LIKE 'GAMBLE_%'",
            f"ts >= '{_GAMBLE_STATS_CUTOFF}'::timestamptz",
        ]
        params: list = [guild_id]
        idx = 2
        if since is not None:
            clauses.append(f"ts >= ${idx}")
            params.append(since)
            idx += 1
        if game_type is not None:
            clauses.append(f"tx_type = ${idx}")
            params.append(f"GAMBLE_{game_type.upper()}")
            idx += 1
        if user_ids:
            placeholders = ", ".join(f"${idx + i}" for i in range(len(user_ids)))
            clauses.append(f"user_id IN ({placeholders})")
            params.extend(user_ids)
            idx += len(user_ids)
        where = " AND ".join(clauses)
        params.append(limit)
        return await self.fetch_all(
            f"""SELECT user_id,
                      COALESCE(SUM(amount_out - amount_in), 0) AS net_pnl,
                      COUNT(*) AS total_games,
                      SUM(CASE WHEN (amount_out - amount_in) > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(amount_in) AS total_wagered
               FROM transactions
               WHERE {where}
               GROUP BY user_id
               HAVING COUNT(*) > 0
               ORDER BY net_pnl DESC
               LIMIT ${idx}""",
            *params,
        )

    async def get_work_leaderboard(self, guild_id: int, limit: int = 50) -> list[dict]:
        """Top workers by number of shifts completed."""
        return await self.fetch_all(
            """SELECT user_id, work_count, total_earned
               FROM user_jobs
               WHERE guild_id = $1 AND work_count > 0
               ORDER BY work_count DESC
               LIMIT $2""",
            guild_id, limit,
        )

    async def get_group_gambling_stats(
        self, guild_id: int, user_ids: list[int], *,
        since: datetime | None = None,
        game_type: str | None = None,
    ) -> list[dict]:
        """Aggregate gambling stats across multiple users (for group view).

        Returns per-game stats aggregated across all provided user_ids.
        """
        clauses = [
            "guild_id = $1", "tx_type LIKE 'GAMBLE_%'",
            f"ts >= '{_GAMBLE_STATS_CUTOFF}'::timestamptz",
        ]
        params: list = [guild_id]
        idx = 2
        placeholders = ", ".join(f"${idx + i}" for i in range(len(user_ids)))
        clauses.append(f"user_id IN ({placeholders})")
        params.extend(user_ids)
        idx += len(user_ids)
        if since is not None:
            clauses.append(f"ts >= ${idx}")
            params.append(since)
            idx += 1
        if game_type is not None:
            clauses.append(f"tx_type = ${idx}")
            params.append(f"GAMBLE_{game_type.upper()}")
            idx += 1
        where = " AND ".join(clauses)
        rows = await self.fetch_all(
            f"""
            SELECT
                REPLACE(tx_type, 'GAMBLE_', '') AS game,
                COUNT(*)                        AS total_games,
                COUNT(DISTINCT user_id)         AS unique_players,
                SUM(CASE WHEN (amount_out - amount_in) > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN (amount_out - amount_in) <= 0 THEN 1 ELSE 0 END) AS losses,
                SUM(amount_out - amount_in)     AS net_pnl,
                SUM(amount_in)                  AS total_wagered,
                MAX(CASE WHEN (amount_out - amount_in) > 0 THEN (amount_out - amount_in) ELSE 0 END) AS biggest_win,
                MIN(CASE WHEN (amount_out - amount_in) <= 0 THEN (amount_out - amount_in) ELSE 0 END) AS biggest_loss
            FROM transactions
            WHERE {where}
            GROUP BY tx_type
            ORDER BY total_games DESC
            """,
            *params,
        )
        result = []
        for d in rows:
            total = d.get("total_games") or 0
            wins  = d.get("wins") or 0
            d["net_pnl"] = int(d["net_pnl"]) if d.get("net_pnl") is not None else 0
            d["total_wagered"] = int(d["total_wagered"]) if d.get("total_wagered") is not None else 0
            d["biggest_win"] = int(d["biggest_win"]) if d.get("biggest_win") is not None else 0
            d["biggest_loss"] = int(d["biggest_loss"]) if d.get("biggest_loss") is not None else 0
            d["win_rate"] = wins / total if total > 0 else 0.0
            result.append(d)
        return result

    async def get_user_trade_history(
        self, user_id: int, guild_id: int, limit: int = 25, tx_type: str | None = None,
    ) -> list[dict]:
        """Recent trades for a user. Filters to BUY/SELL/SWAP by default."""
        if tx_type and tx_type.upper() in ("BUY", "SELL", "SWAP"):
            return await self.fetch_all(
                """SELECT tx_hash, tx_type, symbol_in, amount_in, symbol_out,
                          amount_out, price_at, gas_fee, gas_coin, block_num, ts
                   FROM transactions
                   WHERE user_id = $1 AND guild_id = $2 AND tx_type = $3
                   ORDER BY ts DESC LIMIT $4""",
                user_id, guild_id, tx_type.upper(), limit,
            )
        return await self.fetch_all(
            """SELECT tx_hash, tx_type, symbol_in, amount_in, symbol_out,
                      amount_out, price_at, gas_fee, gas_coin, block_num, ts
               FROM transactions
               WHERE user_id = $1 AND guild_id = $2
                 AND tx_type IN ('BUY', 'SELL', 'SWAP')
               ORDER BY ts DESC LIMIT $3""",
            user_id, guild_id, limit,
        )

    async def get_economy_snapshot(self, guild_id: int) -> dict:
        """Server-wide aggregate stats for the economy dashboard.
        Note: queries users and loans tables (cross-repo) for completeness."""
        ts_24h = datetime.fromtimestamp(time.time() - 86400, tz=timezone.utc)

        # User / wallet / bank totals
        user_row = await self.fetch_one(
            """SELECT COUNT(*)       AS user_count,
                      COALESCE(SUM(wallet), 0) AS total_wallet,
                      COALESCE(SUM(bank), 0)   AS total_bank
               FROM users WHERE guild_id = $1""",
            guild_id,
        )

        # 24h trade volume
        trade_row = await self.fetch_one(
            """SELECT COUNT(*)                    AS trade_count_24h,
                      COALESCE(SUM(amount_in), 0) AS volume_usd_24h
               FROM transactions
               WHERE guild_id = $1 AND tx_type IN ('BUY', 'SELL', 'SWAP')
                 AND ts >= $2""",
            guild_id, ts_24h,
        )

        # Active loans
        loan_row = await self.fetch_one(
            """SELECT COUNT(*)                        AS active_loans,
                      COALESCE(SUM(outstanding), 0)   AS total_outstanding
               FROM loans
               WHERE guild_id = $1 AND outstanding > 0""",
            guild_id,
        )

        # 24h gambling activity. Live gameplay logs to ``transactions`` with
        # ``tx_type LIKE 'GAMBLE_*'`` (cogs/play.py:1081); the older
        # ``game_results`` table is only written by the v2 API and isn't
        # the source of truth, so reading from it always reported zero.
        # ``amount_in`` on gamble rows is the player's bet (raw NUMERIC).
        game_row = await self.fetch_one(
            """SELECT COUNT(*)                    AS games_24h,
                      COALESCE(SUM(amount_in), 0) AS wagered_24h
               FROM transactions
               WHERE guild_id = $1 AND tx_type LIKE 'GAMBLE_%'
                 AND ts >= $2""",
            guild_id, ts_24h,
        )

        return {
            "user_count": (user_row or {}).get("user_count", 0),
            "total_wallet": int((user_row or {}).get("total_wallet", 0)),
            "total_bank": int((user_row or {}).get("total_bank", 0)),
            "trade_count_24h": (trade_row or {}).get("trade_count_24h", 0),
            "volume_usd_24h": int((trade_row or {}).get("volume_usd_24h", 0)),
            "active_loans": (loan_row or {}).get("active_loans", 0),
            "total_outstanding": int((loan_row or {}).get("total_outstanding", 0)),
            "games_24h": (game_row or {}).get("games_24h", 0),
            "wagered_24h": int((game_row or {}).get("wagered_24h", 0)),
        }

    async def get_gambling_history(
        self, user_id: int, guild_id: int, limit: int = 20,
        game_type: str | None = None, *, since: datetime | None = None,
    ) -> list[dict]:
        """Recent game results for a user, optionally filtered by game type and time."""
        clauses = ["user_id = $1", "guild_id = $2"]
        params: list = [user_id, guild_id]
        idx = 3
        if game_type:
            clauses.append(f"game_type = ${idx}")
            params.append(game_type)
            idx += 1
        if since is not None:
            clauses.append(f"played_at >= ${idx}")
            params.append(since)
            idx += 1
        clauses_str = " AND ".join(clauses)
        params.append(limit)
        return await self.fetch_all(
            f"""SELECT game_type, bet_amount, payout, profit, multiplier,
                      result_data, played_at
               FROM game_results
               WHERE {clauses_str}
               ORDER BY played_at DESC LIMIT ${idx}""",
            *params,
        )

    async def get_gambling_streaks(
        self, user_id: int, guild_id: int, *,
        since: datetime | None = None,
        game_type: str | None = None,
    ) -> dict:
        """Compute win/loss streaks from recent game results."""
        clauses = ["user_id = $1", "guild_id = $2"]
        params: list = [user_id, guild_id]
        idx = 3
        if game_type:
            clauses.append(f"game_type = ${idx}")
            params.append(game_type)
            idx += 1
        if since is not None:
            clauses.append(f"played_at >= ${idx}")
            params.append(since)
            idx += 1
        where = " AND ".join(clauses)
        rows = await self.fetch_all(
            f"""SELECT profit FROM game_results
               WHERE {where}
               ORDER BY played_at DESC LIMIT 200""",
            *params,
        )

        current_streak = 0
        current_type = "none"
        best_win = 0
        best_loss = 0

        # Filter out pushes (profit == 0) -- they don't affect streaks
        rows = [r for r in rows if int(r["profit"]) != 0]

        if rows:
            # Determine current streak from the most recent game
            first_type = "win" if int(rows[0]["profit"]) > 0 else "loss"
            current_type = first_type
            for r in rows:
                is_win = int(r["profit"]) > 0
                if (is_win and first_type == "win") or (not is_win and first_type == "loss"):
                    current_streak += 1
                else:
                    break

            # Scan all rows for best streaks
            streak = 0
            prev_win = None
            for r in rows:
                is_win = int(r["profit"]) > 0
                if is_win == prev_win:
                    streak += 1
                else:
                    streak = 1
                    prev_win = is_win
                if is_win:
                    best_win = max(best_win, streak)
                else:
                    best_loss = max(best_loss, streak)

        return {
            "current_streak": current_streak,
            "current_type": current_type,
            "best_win_streak": best_win,
            "best_loss_streak": best_loss,
        }
