"""Prediction markets repository (PostgreSQL)."""
from __future__ import annotations

import json
from datetime import datetime

from .base import PgBaseRepo


class PgPredictionsRepo(PgBaseRepo):

    # -- Markets --

    async def create_market(
        self, guild_id: int, question: str, description: str,
        category: str, options: list[str], end_time: datetime,
        created_by: int, prize_pool: float = 0.0,
    ) -> dict:
        return await self.fetch_one(
            "INSERT INTO prediction_markets"
            " (guild_id, question, description, category, options, end_time, created_by, prize_pool, total_pool)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8)"
            " RETURNING *",
            guild_id, question, description, category,
            json.dumps(options), end_time, created_by, prize_pool,
        )

    async def update_market_pool(self, market_id: int, prize_pool: float) -> bool:
        """Update the prize pool of an existing market, adjusting total_pool accordingly."""
        row = await self.fetch_one(
            "SELECT prize_pool, total_pool FROM prediction_markets WHERE id = $1", market_id
        )
        if not row:
            return False
        old_prize = float(row["prize_pool"] or 0)
        old_total = float(row["total_pool"] or 0)
        new_total = max(0.0, old_total - old_prize + prize_pool)
        await self.execute(
            "UPDATE prediction_markets SET prize_pool=$1, total_pool=$2 WHERE id=$3",
            prize_pool, new_total, market_id,
        )
        return True

    async def update_market_end_time(self, market_id: int, new_end_time: datetime) -> bool:
        status = await self.execute(
            "UPDATE prediction_markets SET end_time=$1 WHERE id=$2 AND status='open'",
            new_end_time, market_id,
        )
        return self._row_count(status) > 0

    async def get_market(self, market_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM prediction_markets WHERE id = $1", market_id,
        )

    async def get_open_markets(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM prediction_markets"
            " WHERE guild_id = $1 AND status = 'open'"
            " ORDER BY end_time ASC",
            guild_id,
        )

    async def get_all_markets(self, guild_id: int, limit: int = 20) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM prediction_markets"
            " WHERE guild_id = $1"
            " ORDER BY created_at DESC LIMIT $2",
            guild_id, limit,
        )

    async def close_market(self, market_id: int) -> bool:
        status = await self.execute(
            "UPDATE prediction_markets SET status = 'closed' WHERE id = $1 AND status = 'open'",
            market_id,
        )
        return self._row_count(status) > 0

    async def resolve_market(self, market_id: int, winning_option: str) -> bool:
        status = await self.execute(
            "UPDATE prediction_markets"
            " SET status = 'resolved', resolved_option = $2, resolved_at = now()"
            " WHERE id = $1 AND status IN ('open', 'closed')",
            market_id, winning_option,
        )
        return self._row_count(status) > 0

    async def cancel_market(self, market_id: int) -> bool:
        status = await self.execute(
            "UPDATE prediction_markets SET status = 'cancelled' WHERE id = $1 AND status IN ('open', 'closed')",
            market_id,
        )
        return self._row_count(status) > 0

    # -- Bets --

    async def place_bet(
        self, guild_id: int, market_id: int, user_id: int,
        option: str, amount: float,
    ) -> dict:
        async with self.transaction() as conn:
            bet = await conn.fetchrow(
                "INSERT INTO prediction_bets (guild_id, market_id, user_id, option, amount)"
                " VALUES ($1,$2,$3,$4,$5)"
                " RETURNING *",
                guild_id, market_id, user_id, option, amount,
            )
            await conn.execute(
                "UPDATE prediction_markets SET total_pool = total_pool + $1 WHERE id = $2",
                amount, market_id,
            )
            from core.database import _row
            return _row(bet)

    async def get_market_bets(self, market_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM prediction_bets WHERE market_id = $1 ORDER BY placed_at",
            market_id,
        )

    async def get_user_bets(self, user_id: int, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT b.*, m.question, m.status, m.resolved_option"
            " FROM prediction_bets b"
            " JOIN prediction_markets m ON b.market_id = m.id"
            " WHERE b.user_id = $1 AND b.guild_id = $2"
            " ORDER BY b.placed_at DESC",
            user_id, guild_id,
        )

    async def get_market_pools(self, market_id: int) -> dict[str, float]:
        """Return {option: total_amount} for each option in the market."""
        rows = await self.fetch_all(
            "SELECT option, COALESCE(SUM(amount), 0) AS total"
            " FROM prediction_bets WHERE market_id = $1"
            " GROUP BY option",
            market_id,
        )
        return {r["option"]: float(r["total"]) for r in rows}

    async def get_winning_bets(self, market_id: int, winning_option: str) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM prediction_bets WHERE market_id = $1 AND option = $2",
            market_id, winning_option,
        )

    async def get_all_bets_for_market(self, market_id: int) -> list[dict]:
        """Get all bets for refund purposes (cancellation)."""
        return await self.fetch_all(
            "SELECT * FROM prediction_bets WHERE market_id = $1",
            market_id,
        )
