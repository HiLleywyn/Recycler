"""Moons (MOON) economy repository.

Backs the Lunar Mint (Slice 1): players stake a group token into a row of
``lunar_stakes`` and earn MOON on the hourly tick in :mod:`cogs.moons`. Slice
2 (MOON -> DSD real yield via the Moon Pool) will live here too when it
lands.

Migration ``0104_moons_economy.sql`` creates the table; this repo only
reads/writes it.
"""
from __future__ import annotations

from .base import PgBaseRepo


class PgMoonsRepo(PgBaseRepo):

    # ── Lunar Mint (Tier 1) ───────────────────────────────────────────────

    async def get_lunar_stake(
        self, user_id: int, guild_id: int, symbol: str,
    ) -> dict | None:
        """Return the lunar-stake row for (user, guild, symbol) or None."""
        return await self.fetch_one(
            "SELECT * FROM lunar_stakes "
            "WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol.upper(),
        )

    async def get_lunar_stakes_for_user(
        self, user_id: int, guild_id: int,
    ) -> list[dict]:
        """Every active lunar-stake position for a user in a guild."""
        return await self.fetch_all(
            "SELECT * FROM lunar_stakes "
            "WHERE user_id=$1 AND guild_id=$2 AND amount > 0 "
            "ORDER BY staked_at ASC",
            user_id, guild_id,
        )

    async def get_lunar_stakes_for_guild(self, guild_id: int) -> list[dict]:
        """Every active lunar-stake row for a guild. Used by the tick loop."""
        return await self.fetch_all(
            "SELECT * FROM lunar_stakes WHERE guild_id=$1 AND amount > 0",
            guild_id,
        )

    async def upsert_lunar_stake(
        self, user_id: int, guild_id: int, symbol: str, delta_raw: int,
    ) -> dict:
        """Add ``delta_raw`` to the user's lunar stake of ``symbol``.

        On first stake (row absent) the ``staked_at`` timestamp is set to NOW
        so the warmup curve starts fresh. On subsequent adds we keep the
        original ``staked_at`` -- adding to an already-warmed position must
        not reset the warmup, otherwise a user could game the ramp by
        top-up/unstake cycling.
        """
        return await self.fetch_one(
            "INSERT INTO lunar_stakes (user_id, guild_id, symbol, amount) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT(user_id, guild_id, symbol) DO UPDATE SET "
            "    amount = lunar_stakes.amount + EXCLUDED.amount "
            "RETURNING *",
            user_id, guild_id, symbol.upper(), delta_raw,
        )

    async def subtract_lunar_stake(
        self, user_id: int, guild_id: int, symbol: str, amount_raw: int,
    ) -> int:
        """Deduct ``amount_raw`` from a lunar stake. Returns the new amount.

        Clamped at 0; callers must validate the amount against the current
        balance before calling. Does NOT delete empty rows -- keeping the
        row preserves ``total_earned`` history for the profile display.
        """
        row = await self.fetch_one(
            "UPDATE lunar_stakes "
            "SET amount = GREATEST(0, amount - $1) "
            "WHERE user_id=$2 AND guild_id=$3 AND symbol=$4 "
            "RETURNING amount",
            amount_raw, user_id, guild_id, symbol.upper(),
        )
        return int(row["amount"]) if row else 0

    async def record_lunar_earnings(
        self, user_id: int, guild_id: int, symbol: str, earned_h: float,
    ) -> None:
        """Bump session/total earned counters on a lunar-stake row.

        Called by the tick loop after crediting MOON to the wallet.
        Mirrors the ``stakes.session_earned / total_earned`` pattern in
        ``cogs/stake.py``. Both columns are human-scale NUMERIC(28,8).
        """
        await self.execute(
            "UPDATE lunar_stakes "
            "SET session_earned = session_earned + $1, "
            "    total_earned   = total_earned   + $1 "
            "WHERE user_id=$2 AND guild_id=$3 AND symbol=$4",
            earned_h, user_id, guild_id, symbol.upper(),
        )

    async def reset_lunar_session(
        self, user_id: int, guild_id: int, symbol: str,
    ) -> None:
        """Zero out ``session_earned`` when the user fully exits a position."""
        await self.execute(
            "UPDATE lunar_stakes SET session_earned = 0 "
            "WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol.upper(),
        )

    async def get_guild_moon_minted_recent(
        self, guild_id: int, since_epoch: float,
    ) -> float:
        """Sum of MOON credited to any user in ``guild_id`` since ``since_epoch``.

        Reads from the transactions table where tx_type='LUNAR_MINT'. Used
        by the hourly tick to enforce ``PER_GUILD_DAILY_MOON_CAP``.
        Returns human-scale float.
        """
        from datetime import datetime, timezone
        since_dt = datetime.fromtimestamp(since_epoch, tz=timezone.utc)
        row = await self.fetch_one(
            "SELECT COALESCE(SUM(amount_out), 0) AS total "
            "FROM transactions "
            "WHERE guild_id=$1 AND tx_type='LUNAR_MINT' AND ts >= $2",
            guild_id, since_dt,
        )
        if not row:
            return 0.0
        return float(row.h("total"))

    async def get_user_moon_minted_recent(
        self, user_id: int, guild_id: int, since_epoch: float,
    ) -> float:
        """Sum of MOON credited to ``user_id`` in ``guild_id`` since
        ``since_epoch``. Enforces ``PER_USER_DAILY_MOON_CAP``. Human scale."""
        from datetime import datetime, timezone
        since_dt = datetime.fromtimestamp(since_epoch, tz=timezone.utc)
        row = await self.fetch_one(
            "SELECT COALESCE(SUM(amount_out), 0) AS total "
            "FROM transactions "
            "WHERE user_id=$1 AND guild_id=$2 AND tx_type='LUNAR_MINT' AND ts >= $3",
            user_id, guild_id, since_dt,
        )
        if not row:
            return 0.0
        return float(row.h("total"))

    # ── Group activity (for the emission multiplier) ──────────────────────

    async def get_group_activity_for_token(
        self, guild_id: int, symbol: str, window_secs: int,
    ) -> tuple[int, int]:
        """Return (distinct_miner_count, blocks_won) for the group that
        issued ``symbol`` over the last ``window_secs``.

        Used by the Lunar Mint emission formula to scale the activity
        bonus: zombie groups (1 miner or 0 recent blocks) earn no bonus,
        active groups (>= GROUP_ACTIVITY_MIN_MINERS and
        GROUP_ACTIVITY_MIN_BLOCKS) earn the full GROUP_ACTIVITY_BONUS_MAX.

        Returns (0, 0) if no group owns this symbol (token_type != 'group',
        or no mining_groups row references it).
        """
        from datetime import datetime, timezone
        import time
        since_dt = datetime.fromtimestamp(time.time() - window_secs, tz=timezone.utc)
        row = await self.fetch_one(
            "SELECT mg.group_id FROM mining_groups mg "
            "WHERE mg.guild_id=$1 AND mg.token_symbol=$2 LIMIT 1",
            guild_id, symbol.upper(),
        )
        if not row:
            return (0, 0)
        group_id = row["group_id"]
        # Blocks won by the group's current members in the window.
        # ``mining_blocks`` stores only ``miner_id`` (not group_id), so we
        # join against current ``mining_group_members`` to attribute blocks
        # to the group. Uses block_ts (not mined_at).
        act = await self.fetch_one(
            "SELECT COUNT(*) AS blocks, COUNT(DISTINCT mb.miner_id) AS miners "
            "FROM mining_blocks mb "
            "JOIN mining_group_members mgm "
            "  ON mgm.user_id = mb.miner_id "
            " AND mgm.guild_id = mb.guild_id "
            "WHERE mb.guild_id = $1 "
            "  AND mgm.group_id = $2 "
            "  AND mb.block_ts >= $3",
            guild_id, group_id, since_dt,
        )
        if not act:
            return (0, 0)
        return (int(act["miners"] or 0), int(act["blocks"] or 0))

    # ── Moon Pool (Tier 2) ─────────────────────────────────────────────

    async def get_moon_stake(self, user_id: int, guild_id: int) -> dict | None:
        """Return the Moon Pool position for (user, guild) or None."""
        return await self.fetch_one(
            "SELECT * FROM moon_stakes WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def get_moon_stakes_for_guild(self, guild_id: int) -> list[dict]:
        """Every active Moon Pool position for a guild. Used by the distribution tick."""
        return await self.fetch_all(
            "SELECT * FROM moon_stakes WHERE guild_id=$1 AND amount > 0",
            guild_id,
        )

    async def upsert_moon_stake(self, user_id: int, guild_id: int, delta_raw: int) -> dict:
        """Add ``delta_raw`` MOON to the user's Moon Pool position. Keeps
        ``staked_at`` on top-ups so the 12h warmup can't be reset and farmed."""
        return await self.fetch_one(
            "INSERT INTO moon_stakes (user_id, guild_id, amount) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT(user_id, guild_id) DO UPDATE SET "
            "    amount = moon_stakes.amount + EXCLUDED.amount "
            "RETURNING *",
            user_id, guild_id, delta_raw,
        )

    async def subtract_moon_stake(self, user_id: int, guild_id: int, amount_raw: int) -> int:
        """Deduct ``amount_raw`` MOON from the pool. Returns the new balance."""
        row = await self.fetch_one(
            "UPDATE moon_stakes "
            "SET amount = GREATEST(0, amount - $1) "
            "WHERE user_id=$2 AND guild_id=$3 "
            "RETURNING amount",
            amount_raw, user_id, guild_id,
        )
        return int(row["amount"]) if row else 0

    async def record_moon_earnings(self, user_id: int, guild_id: int, earned_h: float) -> None:
        """Bump session/total earned counters on a Moon Pool position."""
        await self.execute(
            "UPDATE moon_stakes "
            "SET session_earned = session_earned + $1, "
            "    total_earned   = total_earned   + $1 "
            "WHERE user_id=$2 AND guild_id=$3",
            earned_h, user_id, guild_id,
        )

    async def get_moon_pool_total_raw(self, guild_id: int) -> int:
        """Total MOON staked in the guild's Moon Pool. Raw scale."""
        row = await self.fetch_one(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM moon_stakes WHERE guild_id=$1",
            guild_id,
        )
        return int(row["total"]) if row else 0

    # ── Moon Network vault (distribution source) ──────────────────────

    async def get_moon_vault_distributable(self, guild_id: int) -> float:
        """Human-scale USD sitting in the Moon Network vault's distributable
        bucket, waiting to be dripped to MOON stakers."""
        row = await self.fetch_one(
            "SELECT distributable_balance FROM network_vaults "
            "WHERE guild_id=$1 AND network='moon'",
            guild_id,
        )
        return float(row["distributable_balance"]) if row else 0.0

    async def add_moon_vault_distributable(self, guild_id: int, delta_h: float) -> None:
        """Bump the distributable bucket on the Moon Network vault row. Creates
        the row if missing so the first post-deploy swap does not silently
        drop its share."""
        await self.execute(
            "INSERT INTO network_vaults (guild_id, network, balance, distributable_balance) "
            "VALUES ($1, 'moon', 0, $2) "
            "ON CONFLICT (guild_id, network) DO UPDATE SET "
            "    distributable_balance = network_vaults.distributable_balance + EXCLUDED.distributable_balance",
            guild_id, delta_h,
        )

    async def drain_moon_vault_distributable(self, guild_id: int, amount_h: float) -> None:
        """Remove ``amount_h`` USD from the distributable bucket after a
        distribution tick pays stakers. Clamps at 0."""
        await self.execute(
            "UPDATE network_vaults "
            "SET distributable_balance = GREATEST(0, distributable_balance - $1), "
            "    last_moon_distributed_at = now() "
            "WHERE guild_id=$2 AND network='moon'",
            amount_h, guild_id,
        )

    # ── Wrapped-asset staking (Tier 3: mMTA / mSUN dual-yield) ────────────

    async def get_wrapped_stake(
        self, user_id: int, guild_id: int, symbol: str,
    ) -> dict | None:
        """Return the wrapped-stake row for (user, guild, symbol) or None."""
        return await self.fetch_one(
            "SELECT * FROM moon_wrapped_stakes "
            "WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol.upper(),
        )

    async def get_wrapped_stakes_for_user(
        self, user_id: int, guild_id: int,
    ) -> list[dict]:
        """Every wrapped-stake position for a user (incl. claimable-but-empty)."""
        return await self.fetch_all(
            "SELECT * FROM moon_wrapped_stakes "
            "WHERE user_id=$1 AND guild_id=$2 "
            "  AND (amount > 0 OR pending_self > 0 OR pending_moon > 0) "
            "ORDER BY symbol ASC",
            user_id, guild_id,
        )

    async def get_wrapped_stakes_for_guild(self, guild_id: int) -> list[dict]:
        """Every active wrapped-stake row for a guild. Used by the tick loop."""
        return await self.fetch_all(
            "SELECT * FROM moon_wrapped_stakes WHERE guild_id=$1 AND amount > 0",
            guild_id,
        )

    async def upsert_wrapped_stake(
        self, user_id: int, guild_id: int, symbol: str, delta_raw: int,
    ) -> dict:
        """Add ``delta_raw`` to a wrapped stake. Keeps ``staked_at`` on top-ups
        so the warmup ramp cannot be reset by top-up/unstake cycling. Callers
        must accrue pending rewards up to NOW before topping up."""
        return await self.fetch_one(
            "INSERT INTO moon_wrapped_stakes (user_id, guild_id, symbol, amount) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id, guild_id, symbol) DO UPDATE SET "
            "    amount = moon_wrapped_stakes.amount + EXCLUDED.amount "
            "RETURNING *",
            user_id, guild_id, symbol.upper(), delta_raw,
        )

    async def subtract_wrapped_stake(
        self, user_id: int, guild_id: int, symbol: str, amount_raw: int,
    ) -> int:
        """Deduct ``amount_raw`` from a wrapped stake. Returns the new amount,
        clamped at 0. Does not delete the row (preserves pending + history)."""
        row = await self.fetch_one(
            "UPDATE moon_wrapped_stakes "
            "SET amount = GREATEST(0, amount - $1) "
            "WHERE user_id=$2 AND guild_id=$3 AND symbol=$4 "
            "RETURNING amount",
            amount_raw, user_id, guild_id, symbol.upper(),
        )
        return int(row["amount"]) if row else 0

    async def accrue_wrapped_pending(
        self, user_id: int, guild_id: int, symbol: str, *,
        self_raw: int, moon_raw: int, earned_usd: float,
    ) -> None:
        """Tick hook: add accrued yield to the pending buckets and advance the
        ``last_accrued_at`` cursor. ``earned_usd`` bumps the session/total
        counters (USD-valued, for display)."""
        await self.execute(
            "UPDATE moon_wrapped_stakes SET "
            "    pending_self   = pending_self   + $1, "
            "    pending_moon   = pending_moon   + $2, "
            "    session_earned = session_earned + $3, "
            "    total_earned   = total_earned   + $3, "
            "    last_accrued_at = now() "
            "WHERE user_id=$4 AND guild_id=$5 AND symbol=$6",
            self_raw, moon_raw, earned_usd, user_id, guild_id, symbol.upper(),
        )

    async def touch_wrapped_accrual(
        self, user_id: int, guild_id: int, symbol: str,
    ) -> None:
        """Advance ``last_accrued_at`` to NOW without crediting (used when an
        accrual computes to zero so the cursor still moves forward)."""
        await self.execute(
            "UPDATE moon_wrapped_stakes SET last_accrued_at = now() "
            "WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol.upper(),
        )

    async def claim_wrapped_pending(
        self, user_id: int, guild_id: int, symbol: str,
    ) -> tuple[int, int]:
        """Zero a position's pending buckets and return what was claimed as
        ``(self_raw, moon_raw)``. Atomic: the CTE snapshots the pre-update
        values so a concurrent accrual cannot be silently dropped."""
        row = await self.fetch_one(
            "WITH cur AS ("
            "  SELECT pending_self, pending_moon FROM moon_wrapped_stakes "
            "  WHERE user_id=$1 AND guild_id=$2 AND symbol=$3"
            ") "
            "UPDATE moon_wrapped_stakes SET pending_self = 0, pending_moon = 0 "
            "WHERE user_id=$1 AND guild_id=$2 AND symbol=$3 "
            "RETURNING (SELECT pending_self FROM cur) AS claimed_self, "
            "          (SELECT pending_moon FROM cur) AS claimed_moon",
            user_id, guild_id, symbol.upper(),
        )
        if not row:
            return (0, 0)
        return (int(row["claimed_self"] or 0), int(row["claimed_moon"] or 0))

    # ── MOON supply ───────────────────────────────────────────────────────

    async def adjust_moon_circulating(self, guild_id: int, delta_raw: int) -> None:
        """Adjust MOON ``circulating_supply`` by ``delta_raw`` (negative burns,
        positive emits). Clamped at 0."""
        await self.execute(
            "UPDATE crypto_prices "
            "SET circulating_supply = GREATEST(0, circulating_supply + $1) "
            "WHERE symbol='MOON' AND guild_id=$2",
            delta_raw, guild_id,
        )
