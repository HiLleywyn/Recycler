"""Mining repository (PostgreSQL)  -  rigs, pool, network, blocks, groups, chain blocks."""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Any

from core.config import Config
from .base import PgBaseRepo


class PgMiningRepo(PgBaseRepo):

    # ── Mining Rigs ────────────────────────────────────────────────────────

    async def get_user_rigs(self, user_id: int, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM mining_rigs WHERE user_id=$1 AND guild_id=$2 AND quantity > 0",
            user_id, guild_id,
        )

    async def update_rig(self, user_id: int, guild_id: int, rig_id: str, delta: int) -> int:
        await self.execute(
            """INSERT INTO mining_rigs (user_id, guild_id, rig_id)
               VALUES ($1, $2, $3)
               ON CONFLICT DO NOTHING""",
            user_id, guild_id, rig_id,
        )
        row = await self.fetch_one(
            """UPDATE mining_rigs SET quantity = quantity + $1
               WHERE user_id=$2 AND guild_id=$3 AND rig_id=$4 AND quantity + $1 >= 0
               RETURNING quantity""",
            delta, user_id, guild_id, rig_id,
        )
        if row is None:
            raise ValueError(f"Not enough rigs (need {-delta})")
        return row["quantity"]

    async def get_user_total_hashrate(self, user_id: int, guild_id: int) -> float:
        """Total SUN-chain hashrate for a user (excludes MTA-assigned rigs)."""
        rigs = await self.get_user_rigs(user_id, guild_id)
        total = 0.0
        for r in rigs:
            rig_cfg = Config.MINING_RIGS.get(r["rig_id"])
            if rig_cfg:
                sun_qty = r.get("quantity", 0)
                total += rig_cfg["hashrate"] * sun_qty
        return total

    async def get_all_guild_rigs(self, guild_id: int) -> list[dict]:
        """All rig rows across all users in guild, for network hashrate calc."""
        return await self.fetch_all(
            "SELECT * FROM mining_rigs WHERE guild_id=$1 AND quantity > 0",
            guild_id,
        )

    # ── Mining Pool Members ────────────────────────────────────────────────

    async def is_pool_miner(self, user_id: int, guild_id: int) -> bool:
        row = await self.fetch_one(
            "SELECT 1 FROM mining_pool_members WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        return row is not None

    async def set_mining_mode(self, user_id: int, guild_id: int, pool: bool) -> None:
        if pool:
            await self.execute(
                """INSERT INTO mining_pool_members (user_id, guild_id)
                   VALUES ($1, $2)
                   ON CONFLICT DO NOTHING""",
                user_id, guild_id,
            )
        else:
            await self.execute(
                "DELETE FROM mining_pool_members WHERE user_id=$1 AND guild_id=$2",
                user_id, guild_id,
            )

    async def get_pool_miners(self, guild_id: int) -> list[int]:
        rows = await self.fetch_all(
            """SELECT user_id FROM mining_pool_members
               WHERE guild_id=$1
               AND user_id NOT IN (
                   SELECT user_id FROM user_mining_config
                   WHERE guild_id=$1 AND mode='group'
               )""",
            guild_id,
        )
        return [r["user_id"] for r in rows]

    async def get_solo_miners(self, guild_id: int) -> list[int]:
        """Users with rigs in solo mode.

        Solo = has rigs AND not in mining_pool_members AND not in group mode.
        Uses mining_pool_members as the single source of truth for pool membership
        so that users whose user_mining_config.mode drifts out of sync with the pool
        table are never silently orphaned from all mining payouts.
        """
        rows = await self.fetch_all(
            """SELECT DISTINCT mr.user_id FROM mining_rigs mr
               WHERE mr.guild_id=$1 AND mr.quantity > 0
               AND mr.user_id NOT IN (
                   SELECT user_id FROM mining_pool_members WHERE guild_id=$1
               )
               AND mr.user_id NOT IN (
                   SELECT user_id FROM user_mining_config WHERE guild_id=$1 AND mode='group'
               )""",
            guild_id,
        )
        return [r["user_id"] for r in rows]

    async def fix_orphaned_group_miners(self, guild_id: int) -> int:
        """Repair users who have mode='group' in user_mining_config but are not
        in any mining group.  These users are orphaned  -  they contribute no
        hashrate in the lottery because get_group_miners returns them but
        get_user_mining_group returns None, so they are silently skipped.

        Resets orphaned users to 'pool' mode and re-syncs mining_pool_members.
        Returns the number of rows fixed.
        """
        orphaned = await self.fetch_all(
            """SELECT umc.user_id FROM user_mining_config umc
               WHERE umc.guild_id = $1 AND umc.mode = 'group'
                 AND NOT EXISTS (
                     SELECT 1 FROM mining_group_members mgm
                     WHERE mgm.user_id = umc.user_id AND mgm.guild_id = $1
                 )""",
            guild_id,
        )
        if not orphaned:
            return 0
        for row in orphaned:
            uid = row["user_id"]
            await self.execute(
                "UPDATE user_mining_config SET mode='pool' WHERE user_id=$1 AND guild_id=$2",
                uid, guild_id,
            )
            await self.execute(
                """INSERT INTO mining_pool_members (user_id, guild_id)
                   VALUES ($1, $2) ON CONFLICT DO NOTHING""",
                uid, guild_id,
            )
        return len(orphaned)

    # ── Modular PoW Network State (unified for all chains) ────────────────────

    async def seed_pow_network(self, guild_id: int, chain_symbol: str) -> None:
        """Seed a pow_network_state row for a chain if it doesn't exist yet."""
        cfg = Config.POW_NETWORKS.get(chain_symbol, {})
        await self.execute(
            """INSERT INTO pow_network_state
               (guild_id, chain_symbol, block_height, total_hashrate, current_reward,
                last_block_ts, difficulty, last_retarget_height)
               VALUES ($1, $2, 0, 0.0, $3, now(), $4, 0)
               ON CONFLICT DO NOTHING""",
            guild_id, chain_symbol,
            cfg.get("initial_reward", 1.0),
            cfg.get("initial_difficulty", 1_000_000.0),
        )

    async def get_pow_network(self, guild_id: int, chain_symbol: str) -> dict | None:
        """Return pow_network_state row for (guild, chain), seeding it if missing."""
        row = await self.fetch_one(
            "SELECT * FROM pow_network_state WHERE guild_id=$1 AND chain_symbol=$2",
            guild_id, chain_symbol,
        )
        if row:
            return row
        # Not seeded yet  -  seed and return
        await self.seed_pow_network(guild_id, chain_symbol)
        return await self.fetch_one(
            "SELECT * FROM pow_network_state WHERE guild_id=$1 AND chain_symbol=$2",
            guild_id, chain_symbol,
        )

    async def get_all_guild_pow_networks(self, guild_id: int) -> list[dict]:
        """Return all pow_network_state rows for a guild."""
        return await self.fetch_all(
            "SELECT * FROM pow_network_state WHERE guild_id=$1", guild_id,
        )

    async def update_pow_network(
        self, guild_id: int, chain_symbol: str,
        block_height: int, total_hashrate: float,
        current_reward: float, last_block_ts: float,
    ) -> None:
        ts_dt = datetime.fromtimestamp(last_block_ts, tz=timezone.utc)
        await self.execute(
            """UPDATE pow_network_state
               SET block_height=$1, total_hashrate=$2, current_reward=$3, last_block_ts=$4
               WHERE guild_id=$5 AND chain_symbol=$6""",
            block_height, total_hashrate, current_reward, ts_dt,
            guild_id, chain_symbol,
        )

    async def update_pow_network_difficulty(
        self, guild_id: int, chain_symbol: str,
        difficulty: float, last_retarget_ts: float, last_retarget_height: int,
    ) -> None:
        ts_dt = datetime.fromtimestamp(last_retarget_ts, tz=timezone.utc)
        await self.execute(
            """UPDATE pow_network_state
               SET difficulty=$1, last_retarget_ts=$2, last_retarget_height=$3
               WHERE guild_id=$4 AND chain_symbol=$5""",
            difficulty, ts_dt, last_retarget_height, guild_id, chain_symbol,
        )

    # ── Modular rig chain assignments ──────────────────────────────────────────

    async def get_user_chain_rigs(self, user_id: int, guild_id: int, chain_symbol: str) -> list[dict]:
        """Return rig_chain_assignments rows for a user on a specific chain."""
        return await self.fetch_all(
            """SELECT * FROM rig_chain_assignments
               WHERE user_id=$1 AND guild_id=$2 AND chain_symbol=$3 AND quantity > 0""",
            user_id, guild_id, chain_symbol,
        )

    async def get_all_guild_chain_rigs(self, guild_id: int, chain_symbol: str) -> list[dict]:
        """Return all rig_chain_assignments for a guild on a specific chain."""
        return await self.fetch_all(
            """SELECT * FROM rig_chain_assignments
               WHERE guild_id=$1 AND chain_symbol=$2 AND quantity > 0""",
            guild_id, chain_symbol,
        )

    async def get_user_all_chain_rigs(self, user_id: int, guild_id: int) -> list[dict]:
        """Return all rig assignments for a user across all chains."""
        return await self.fetch_all(
            """SELECT * FROM rig_chain_assignments
               WHERE user_id=$1 AND guild_id=$2 AND quantity > 0""",
            user_id, guild_id,
        )

    async def assign_rig_to_chain(
        self, user_id: int, guild_id: int, rig_id: str,
        from_symbol: str, to_symbol: str, qty: int,
    ) -> None:
        """Move qty rigs from from_symbol to to_symbol. Atomic: both updates or neither."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        async with self.transaction() as conn:
            # Deduct from source chain
            row = await conn.fetchrow(
                """UPDATE rig_chain_assignments
                   SET quantity = quantity - $1
                   WHERE user_id=$2 AND guild_id=$3 AND rig_id=$4 AND chain_symbol=$5 AND quantity >= $1
                   RETURNING quantity""",
                qty, user_id, guild_id, rig_id, from_symbol,
            )
            if row is None:
                raise ValueError(f"Not enough {rig_id} rigs assigned to {from_symbol} (need {qty})")
            # Add to destination chain
            await conn.execute(
                """INSERT INTO rig_chain_assignments (user_id, guild_id, rig_id, chain_symbol, quantity)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT(user_id, guild_id, rig_id, chain_symbol)
                   DO UPDATE SET quantity = rig_chain_assignments.quantity + EXCLUDED.quantity""",
                user_id, guild_id, rig_id, to_symbol, qty,
            )

    # ── Chain-switch cooldown ─────────────────────────────────────────────

    async def get_last_chain_switch(self, user_id: int, guild_id: int):
        """Get timestamp of user's last chain switch (or None)."""
        row = await self.fetch_one(
            "SELECT last_chain_switch FROM user_mining_config WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        return row["last_chain_switch"] if row else None

    async def record_chain_switch(self, user_id: int, guild_id: int) -> None:
        """Record a chain switch timestamp for cooldown enforcement."""
        await self.execute(
            """INSERT INTO user_mining_config (user_id, guild_id, last_chain_switch)
               VALUES ($1, $2, now())
               ON CONFLICT (user_id, guild_id)
               DO UPDATE SET last_chain_switch = now()""",
            user_id, guild_id,
        )

    async def set_rig_chain_quantity(
        self, user_id: int, guild_id: int, rig_id: str, chain_symbol: str, quantity: int
    ) -> None:
        """Upsert the quantity for a rig-chain assignment (used during rig purchases)."""
        await self.execute(
            """INSERT INTO rig_chain_assignments (user_id, guild_id, rig_id, chain_symbol, quantity)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT(user_id, guild_id, rig_id, chain_symbol)
               DO UPDATE SET quantity = rig_chain_assignments.quantity + EXCLUDED.quantity""",
            user_id, guild_id, rig_id, chain_symbol, quantity,
        )

    async def remove_rig_chain_quantity(
        self, user_id: int, guild_id: int, rig_id: str, chain_symbol: str, quantity: int
    ) -> None:
        """Remove qty rigs from a chain assignment (used during rig sells)."""
        row = await self.fetch_one(
            """UPDATE rig_chain_assignments
               SET quantity = quantity - $1
               WHERE user_id=$2 AND guild_id=$3 AND rig_id=$4 AND chain_symbol=$5 AND quantity >= $1
               RETURNING quantity""",
            quantity, user_id, guild_id, rig_id, chain_symbol,
        )
        if row is None:
            raise ValueError(f"Not enough {rig_id} rigs on {chain_symbol} to remove {quantity}")

    async def get_user_chain_hashrate(self, user_id: int, guild_id: int, chain_symbol: str) -> float:
        """Total hashrate for a user on a specific chain."""
        rigs = await self.get_user_chain_rigs(user_id, guild_id, chain_symbol)
        total = 0.0
        for r in rigs:
            rig_cfg = Config.MINING_RIGS.get(r["rig_id"])
            if rig_cfg:
                total += rig_cfg["hashrate"] * r["quantity"]
        return total

    async def backfill_chain_assignments(self, guild_id: int) -> int:
        """Ensure every mining_rigs row has a matching rig_chain_assignments entry.

        Rigs without any chain assignment are auto-assigned to SUN (the default).
        Returns the number of rows backfilled.
        """
        rows = await self.fetch_all(
            """SELECT mr.user_id, mr.guild_id, mr.rig_id, mr.quantity
               FROM mining_rigs mr
               WHERE mr.guild_id = $1 AND mr.quantity > 0
                 AND NOT EXISTS (
                     SELECT 1 FROM rig_chain_assignments rca
                     WHERE rca.user_id = mr.user_id
                       AND rca.guild_id = mr.guild_id
                       AND rca.rig_id = mr.rig_id
                 )""",
            guild_id,
        )
        for r in rows:
            await self.execute(
                """INSERT INTO rig_chain_assignments
                       (user_id, guild_id, rig_id, chain_symbol, quantity)
                   VALUES ($1, $2, $3, 'SUN', $4)
                   ON CONFLICT (user_id, guild_id, rig_id, chain_symbol)
                   DO NOTHING""",
                r["user_id"], r["guild_id"], r["rig_id"], r["quantity"],
            )
        return len(rows)

    # ── Mining Network ─────────────────────────────────────────────────────

    async def seed_network(self, guild_id: int) -> None:
        await self.execute(
            """INSERT INTO mining_network (guild_id)
               VALUES ($1)
               ON CONFLICT DO NOTHING""",
            guild_id,
        )

    async def get_network(self, guild_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM mining_network WHERE guild_id=$1", guild_id,
        )

    # Alias used by cogs/mining.py
    async def get_mining_network(self, guild_id: int) -> dict | None:
        return await self.get_network(guild_id)

    async def update_network(
        self, guild_id: int, block_height: int,
        total_hashrate: float, current_reward: float, last_block_ts: float
    ) -> None:
        ts_dt = datetime.fromtimestamp(last_block_ts, tz=timezone.utc)
        await self.execute(
            """UPDATE mining_network
               SET block_height=$1, total_hashrate=$2, current_reward=$3, last_block_ts=$4
               WHERE guild_id=$5""",
            block_height, total_hashrate, current_reward, ts_dt, guild_id,
        )

    async def update_network_difficulty(
        self, guild_id: int, difficulty: float,
        last_retarget_ts: float, last_retarget_height: int
    ) -> None:
        # PG mining_network table lacks difficulty columns; store on pow_network_state for SUN
        ts_dt = datetime.fromtimestamp(last_retarget_ts, tz=timezone.utc)
        await self.execute(
            """UPDATE pow_network_state
               SET difficulty=$1, last_retarget_ts=$2, last_retarget_height=$3
               WHERE guild_id=$4 AND chain_symbol='SUN'""",
            difficulty, ts_dt, last_retarget_height, guild_id,
        )

    # ── MTA Mining Network (mapped to pow_network_state) ──────────────────

    async def seed_btc_network(self, guild_id: int) -> None:
        await self.seed_pow_network(guild_id, 'MTA')

    async def get_btc_network(self, guild_id: int) -> dict | None:
        return await self.get_pow_network(guild_id, 'MTA')

    async def update_btc_network(
        self, guild_id: int, block_height: int,
        total_hashrate: float, current_reward: float, last_block_ts: float
    ) -> None:
        await self.update_pow_network(guild_id, 'MTA', block_height, total_hashrate, current_reward, last_block_ts)

    async def update_btc_network_difficulty(
        self, guild_id: int, difficulty: float,
        last_retarget_ts: float, last_retarget_height: int
    ) -> None:
        await self.update_pow_network_difficulty(guild_id, 'MTA', difficulty, last_retarget_ts, last_retarget_height)

    # ── MTA Rig Assignment (mapped to rig_chain_assignments) ──────────────

    async def get_user_rigs_btc(self, user_id: int, guild_id: int) -> list[dict]:
        """Get rigs assigned to MTA mining."""
        return await self.fetch_all(
            """SELECT * FROM rig_chain_assignments
               WHERE user_id=$1 AND guild_id=$2 AND chain_symbol='MTA' AND quantity > 0""",
            user_id, guild_id,
        )

    async def get_all_guild_rigs_btc(self, guild_id: int) -> list[dict]:
        """All MTA-assigned rigs across all users in guild."""
        return await self.fetch_all(
            """SELECT * FROM rig_chain_assignments
               WHERE guild_id=$1 AND chain_symbol='MTA' AND quantity > 0""",
            guild_id,
        )

    async def assign_rig_to_btc(self, user_id: int, guild_id: int, rig_id: str, qty: int) -> None:
        """Reassign qty rigs between SUN and MTA mining. Positive = SUN->MTA, negative = MTA->SUN."""
        if qty > 0:
            await self.assign_rig_to_chain(user_id, guild_id, rig_id, 'SUN', 'MTA', qty)
        else:
            await self.assign_rig_to_chain(user_id, guild_id, rig_id, 'MTA', 'SUN', -qty)

    async def get_user_btc_hashrate(self, user_id: int, guild_id: int) -> float:
        """Total hashrate of rigs assigned to MTA mining for a user."""
        return await self.get_user_chain_hashrate(user_id, guild_id, 'MTA')

    # ── Mining Blocks ──────────────────────────────────────────────────────

    async def log_block(
        self, guild_id: int, block_height: int,
        miner_id: int | None, reward: float, total_hashrate: float,
        symbol: str = "SUN",
    ) -> bool:
        """Insert a mining block. Returns False if a block at this height already exists (idempotent)."""
        # Use INSERT ... ON CONFLICT DO NOTHING to avoid race conditions
        ts_dt = datetime.fromtimestamp(time.time(), tz=timezone.utc)
        result = await self.fetch_one(
            """INSERT INTO mining_blocks
               (guild_id, block_height, block_ts, miner_id, reward, total_hashrate, symbol)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            guild_id, block_height, ts_dt, miner_id, reward, total_hashrate, symbol,
        )
        return result is not None

    async def get_recent_blocks(self, guild_id: int, limit: int = 10) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM mining_blocks WHERE guild_id=$1 ORDER BY block_ts DESC LIMIT $2",
            guild_id, limit,
        )

    # ── Mining Groups ───────────────────────────────────────────────────────

    async def create_mining_group(
        self, guild_id: int, name: str, founder_id: int
    ) -> dict:
        group_id = secrets.token_hex(4).upper()
        now_dt = datetime.now(timezone.utc)
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO mining_groups (group_id, guild_id, name, founder_id, created_at)
                   VALUES ($1, $2, $3, $4, $5)""",
                group_id, guild_id, name, founder_id, now_dt,
            )
            # Auto-join founder
            await conn.execute(
                """INSERT INTO mining_group_members (user_id, guild_id, group_id, joined_at)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT(user_id, guild_id)
                   DO UPDATE SET group_id=EXCLUDED.group_id, joined_at=EXCLUDED.joined_at""",
                founder_id, guild_id, group_id, now_dt,
            )
        return await self.get_mining_group(guild_id, group_id=group_id)

    async def get_mining_group(
        self, guild_id: int, group_id: str | None = None, name: str | None = None
    ) -> dict | None:
        if group_id:
            return await self.fetch_one(
                "SELECT * FROM mining_groups WHERE guild_id=$1 AND group_id=$2",
                guild_id, group_id,
            )
        if name:
            return await self.fetch_one(
                "SELECT * FROM mining_groups WHERE guild_id=$1 AND LOWER(name)=LOWER($2)",
                guild_id, name,
            )
        return None

    async def get_all_mining_groups(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM mining_groups WHERE guild_id=$1 ORDER BY created_at ASC",
            guild_id,
        )

    async def get_user_mining_group(self, user_id: int, guild_id: int) -> dict | None:
        return await self.fetch_one(
            """SELECT mg.* FROM mining_group_members mgm
               JOIN mining_groups mg ON mgm.group_id=mg.group_id AND mgm.guild_id=mg.guild_id
               WHERE mgm.user_id=$1 AND mgm.guild_id=$2""",
            user_id, guild_id,
        )

    async def join_mining_group(self, user_id: int, guild_id: int, group_id: str) -> None:
        now_dt = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO mining_group_members (user_id, guild_id, group_id, joined_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT(user_id, guild_id)
               DO UPDATE SET group_id=EXCLUDED.group_id, joined_at=EXCLUDED.joined_at""",
            user_id, guild_id, group_id, now_dt,
        )
        # Set mining mode to 'group' so get_group_miners() returns this user
        await self.set_user_mining_mode(user_id, guild_id, "group")

    async def leave_mining_group(self, user_id: int, guild_id: int) -> None:
        await self.execute(
            "DELETE FROM mining_group_members WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        # Reset mining mode back to pool so the user isn't orphaned
        # as a group miner with no group in the mining tick.
        await self.execute(
            """UPDATE user_mining_config SET mode='pool'
               WHERE user_id=$1 AND guild_id=$2 AND mode='group'""",
            user_id, guild_id,
        )

    async def get_group_members(self, guild_id: int, group_id: str) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM mining_group_members WHERE guild_id=$1 AND group_id=$2 ORDER BY joined_at ASC",
            guild_id, group_id,
        )

    async def disband_mining_group(self, guild_id: int, group_id: str) -> None:
        # Reset all members' mining mode back to pool before deleting
        await self.execute(
            """UPDATE user_mining_config SET mode='pool'
               WHERE guild_id=$1 AND mode='group' AND user_id IN (
                   SELECT user_id FROM mining_group_members
                   WHERE guild_id=$1 AND group_id=$2
               )""",
            guild_id, group_id,
        )
        await self.execute(
            "DELETE FROM mining_group_members WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )
        await self.execute(
            "DELETE FROM group_invites WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )
        await self.execute(
            "DELETE FROM mining_groups WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )

    async def kick_from_group(self, user_id: int, guild_id: int) -> None:
        await self.execute(
            "DELETE FROM mining_group_members WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        # Reset mining mode back to pool (same as leave)
        await self.execute(
            """UPDATE user_mining_config SET mode='pool'
               WHERE user_id=$1 AND guild_id=$2 AND mode='group'""",
            user_id, guild_id,
        )

    async def update_mining_group(self, guild_id: int, group_id: str, **fields: Any) -> None:
        """Update mutable group fields: description, tag, image_url, weight_mode."""
        _ALLOWED = {"description", "tag", "image_url", "weight_mode"}
        for col, val in fields.items():
            if col not in _ALLOWED:
                continue
            await self.execute(
                f"UPDATE mining_groups SET {col}=$1 WHERE guild_id=$2 AND group_id=$3",
                val, guild_id, group_id,
            )

    async def transfer_mining_group(
        self, guild_id: int, group_id: str, new_founder_id: int,
    ) -> None:
        """Reassign the founder of a mining group to ``new_founder_id``.

        Wrapped in a transaction so the ownership row, the auto-join row
        for the new owner, and any pending proposal cleanup land together.
        The old founder stays in the members table; if the caller wants
        them removed they can run ``,group kick`` afterwards.
        """
        now_dt = datetime.now(timezone.utc)
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE mining_groups SET founder_id=$1 "
                "WHERE guild_id=$2 AND group_id=$3",
                new_founder_id, guild_id, group_id,
            )
            await conn.execute(
                """INSERT INTO mining_group_members
                       (user_id, guild_id, group_id, joined_at)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT(user_id, guild_id)
                   DO UPDATE SET group_id=EXCLUDED.group_id""",
                new_founder_id, guild_id, group_id, now_dt,
            )
            # Drop any pending proposal -- the transfer is final.
            await conn.execute(
                "DELETE FROM group_transfer_proposals "
                "WHERE guild_id=$1 AND group_id=$2",
                guild_id, group_id,
            )

    async def create_group_transfer_proposal(
        self, guild_id: int, group_id: str,
        from_user_id: int, to_user_id: int,
    ) -> dict | None:
        """Insert a pending transfer proposal. Returns the row, or ``None``
        if one already exists (the founder must cancel it before opening
        a new one, enforced by the UNIQUE (guild_id, group_id) index).
        """
        return await self.fetch_one(
            """
            INSERT INTO group_transfer_proposals
                (guild_id, group_id, from_user_id, to_user_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, group_id) DO NOTHING
            RETURNING *
            """,
            guild_id, group_id, from_user_id, to_user_id,
        )

    async def get_group_transfer_proposal(
        self, guild_id: int, group_id: str,
    ) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM group_transfer_proposals "
            "WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )

    async def get_group_transfer_proposals_for_user(
        self, guild_id: int, user_id: int,
    ) -> list[dict]:
        """All pending proposals targeting ``user_id`` in ``guild_id``.

        Used by ``,group transfer accept`` so the target can accept the
        single open proposal without naming the group_id.
        """
        return await self.fetch_all(
            "SELECT * FROM group_transfer_proposals "
            "WHERE guild_id=$1 AND to_user_id=$2 ORDER BY created_at ASC",
            guild_id, user_id,
        )

    async def delete_group_transfer_proposal(
        self, guild_id: int, group_id: str,
    ) -> None:
        await self.execute(
            "DELETE FROM group_transfer_proposals "
            "WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )

    async def set_group_member_weight(
        self, guild_id: int, group_id: str, user_id: int, weight: float
    ) -> None:
        await self.execute(
            """INSERT INTO mining_group_weights (guild_id, group_id, user_id, weight)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT(guild_id, group_id, user_id) DO UPDATE SET weight=EXCLUDED.weight""",
            guild_id, group_id, user_id, weight,
        )

    async def get_group_weights(self, guild_id: int, group_id: str) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM mining_group_weights WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )

    async def update_mining_group_fields(self, guild_id: int, group_id: str, **fields) -> None:
        """Update any mutable mining group fields, including new ones."""
        _ALLOWED = {
            "name", "description", "tag", "image_url", "weight_mode",
            "is_public", "reserve_pct", "reserve_usd", "renamed_at",
        }
        for col, val in fields.items():
            if col not in _ALLOWED:
                continue
            await self.execute(
                f"UPDATE mining_groups SET {col}=$1 WHERE guild_id=$2 AND group_id=$3",
                val, guild_id, group_id,
            )

    async def add_group_reserve_usd(self, guild_id: int, group_id: str, delta_usd: float) -> float:
        """Atomically add USD-equivalent value to group reserve. Any PoW coin converted at current price."""
        from core.framework.scale import to_raw as _tr
        row = await self.fetch_one(
            """UPDATE mining_groups SET reserve_usd = reserve_usd + $1
               WHERE guild_id=$2 AND group_id=$3
               RETURNING reserve_usd""",
            _tr(delta_usd), guild_id, group_id,
        )
        return row.h("reserve_usd") if row else 0.0

    async def spend_group_reserve_usd(self, guild_id: int, group_id: str, amount_usd: float) -> bool:
        """Deduct USD amount from group reserve. Returns False if insufficient balance."""
        row = await self.fetch_one(
            """UPDATE mining_groups SET reserve_usd = reserve_usd - $1
               WHERE guild_id=$2 AND group_id=$3 AND reserve_usd >= $1
               RETURNING reserve_usd""",
            amount_usd, guild_id, group_id,
        )
        return row is not None

    async def add_group_reserve_btc(self, guild_id: int, group_id: str, delta_btc: float) -> float:
        """Accrue (or deduct when negative) MTA in the group's MTA reserve.

        ``delta_btc`` is a human-unit MTA float (e.g. 0.5 = 0.5 MTA).
        Converts to raw NUMERIC(36,0) before writing.
        """
        from core.framework.scale import to_raw as _tr
        raw = _tr(delta_btc)
        row = await self.fetch_one(
            """UPDATE mining_groups SET reserve_btc = reserve_btc + $1
               WHERE guild_id=$2 AND group_id=$3
               RETURNING reserve_btc""",
            raw, guild_id, group_id,
        )
        return row.h("reserve_btc") if row else 0.0

    async def set_group_token_network(
        self, guild_id: int, group_id: str, token_symbol: str, token_network: str
    ) -> None:
        """Bind a group token to a PoW network (e.g. 'Sun Network' / 'Moneta Chain')."""
        await self.execute(
            "UPDATE mining_groups SET token_symbol=$1, token_network=$2 "
            "WHERE guild_id=$3 AND group_id=$4",
            token_symbol, token_network, guild_id, group_id,
        )

    # ── Group mine-chain bulk switch ─────────────────────────────────────────

    async def group_bulk_move_rigs_to_chain(
        self, guild_id: int, group_id: str, chain_symbol: str
    ) -> dict[int, int]:
        """Atomically move ALL non-target rig assignments for every group member to chain_symbol.

        Returns {user_id: total_rigs_on_target_chain} for members that have rigs.
        """
        async with self.transaction() as conn:
            # Step 1: Upsert all non-target rigs into target chain (sum per user+rig)
            await conn.execute(
                """
                INSERT INTO rig_chain_assignments (user_id, guild_id, rig_id, chain_symbol, quantity)
                SELECT rca.user_id, rca.guild_id, rca.rig_id, $3, SUM(rca.quantity)
                FROM rig_chain_assignments rca
                JOIN mining_group_members mgm ON mgm.user_id = rca.user_id AND mgm.guild_id = rca.guild_id
                WHERE rca.guild_id = $1 AND mgm.group_id = $2
                  AND rca.chain_symbol != $3 AND rca.quantity > 0
                GROUP BY rca.user_id, rca.guild_id, rca.rig_id
                ON CONFLICT (user_id, guild_id, rig_id, chain_symbol)
                DO UPDATE SET quantity = rig_chain_assignments.quantity + EXCLUDED.quantity
                """,
                guild_id, group_id, chain_symbol,
            )
            # Step 2: Zero out all non-target chain assignments for these members
            await conn.execute(
                """
                UPDATE rig_chain_assignments rca
                SET quantity = 0
                FROM mining_group_members mgm
                WHERE mgm.guild_id = rca.guild_id AND mgm.user_id = rca.user_id
                  AND mgm.group_id = $2 AND rca.guild_id = $1
                  AND rca.chain_symbol != $3
                """,
                guild_id, group_id, chain_symbol,
            )

        # Fetch summary: total rigs per member on the target chain
        rows = await self.fetch_all(
            """
            SELECT rca.user_id, SUM(rca.quantity) AS total_rigs
            FROM rig_chain_assignments rca
            JOIN mining_group_members mgm ON mgm.user_id = rca.user_id AND mgm.guild_id = rca.guild_id
            WHERE rca.guild_id = $1 AND mgm.group_id = $2
              AND rca.chain_symbol = $3 AND rca.quantity > 0
            GROUP BY rca.user_id
            """,
            guild_id, group_id, chain_symbol,
        )
        return {int(r["user_id"]): int(r["total_rigs"]) for r in rows}

    async def record_group_mine_switch(self, guild_id: int, group_id: str) -> None:
        """Record the timestamp of a group mine chain switch."""
        await self.execute(
            "UPDATE mining_groups SET mine_switched_at = now() WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )

    async def get_group_mine_switched_at(self, guild_id: int, group_id: str):
        """Fetch mine_switched_at epoch float (or None) for the group."""
        row = await self.fetch_one(
            "SELECT mine_switched_at FROM mining_groups WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )
        return row["mine_switched_at"] if row else None

    async def mint_vault_tokens(
        self, guild_id: int, group_id: str, amount: float
    ) -> float:
        """Add ``amount`` tokens to the group vault. Returns new vault balance."""
        row = await self.fetch_one(
            """UPDATE mining_groups SET vault_token_bal = vault_token_bal + $1
               WHERE guild_id=$2 AND group_id=$3
               RETURNING vault_token_bal""",
            amount, guild_id, group_id,
        )
        return float(row["vault_token_bal"]) if row else 0.0

    async def deduct_group_vault_tokens(
        self, guild_id: int, group_id: str, amount: float
    ) -> bool:
        """Deduct ``amount`` tokens from the group vault (human-unit float).

        Uses a conditional UPDATE so the balance can never go negative.
        Returns True on success, False if insufficient balance.
        """
        row = await self.fetch_one(
            """UPDATE mining_groups
               SET vault_token_bal = vault_token_bal - $1
               WHERE guild_id=$2 AND group_id=$3 AND vault_token_bal >= $1
               RETURNING vault_token_bal""",
            amount, guild_id, group_id,
        )
        return row is not None

    # ── Group LP positions (cross-group token pools) ─────────────────────────

    async def get_group_lp_position(
        self, guild_id: int, group_id: str, pool_id: str
    ) -> dict | None:
        """Fetch a group's LP position for a specific pool."""
        return await self.fetch_one(
            "SELECT * FROM group_lp_positions WHERE guild_id=$1 AND group_id=$2 AND pool_id=$3",
            guild_id, group_id, pool_id,
        )

    async def get_group_lp_positions_for_pool(
        self, guild_id: int, pool_id: str
    ) -> list[dict]:
        """All groups that hold LP in a given pool."""
        return await self.fetch_all(
            """SELECT glp.*, mg.name AS group_name, mg.token_symbol
               FROM group_lp_positions glp
               JOIN mining_groups mg ON mg.group_id=glp.group_id AND mg.guild_id=glp.guild_id
               WHERE glp.guild_id=$1 AND glp.pool_id=$2 AND glp.lp_shares > 0""",
            guild_id, pool_id,
        )

    async def update_group_lp_position(
        self, guild_id: int, group_id: str, pool_id: str, delta_raw: int
    ) -> None:
        """Upsert a group LP position, adjusting lp_shares by ``delta_raw`` (raw int).

        On insert sets seeded_at to now. On update only adjusts lp_shares.
        Balance cannot go below 0 (enforced by GREATEST).
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO group_lp_positions (guild_id, group_id, pool_id, lp_shares, seeded_at)
               VALUES ($1, $2, $3, GREATEST(0, $4::NUMERIC), $5)
               ON CONFLICT (group_id, guild_id, pool_id) DO UPDATE
               SET lp_shares = GREATEST(0, group_lp_positions.lp_shares + $4::NUMERIC)""",
            guild_id, group_id, pool_id, delta_raw, now,
        )

    async def set_group_lp_harvest_time(
        self, guild_id: int, group_id: str, pool_id: str
    ) -> None:
        """Record the current timestamp as the last harvest for cooldown enforcement."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        await self.execute(
            """UPDATE group_lp_positions SET last_harvest_at=$1
               WHERE guild_id=$2 AND group_id=$3 AND pool_id=$4""",
            now, guild_id, group_id, pool_id,
        )

    # ── Group Invites ────────────────────────────────────────────────────────

    async def create_group_invite(
        self, guild_id: int, group_id: str, invitee_id: int, invited_by: int
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO group_invites
               (guild_id, group_id, invitee_id, invited_by, created_at)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT(guild_id, group_id, invitee_id)
               DO UPDATE SET invited_by=EXCLUDED.invited_by, created_at=EXCLUDED.created_at""",
            guild_id, group_id, invitee_id, invited_by, now_dt,
        )

    async def get_group_invite(
        self, guild_id: int, group_id: str, invitee_id: int
    ) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM group_invites WHERE guild_id=$1 AND group_id=$2 AND invitee_id=$3",
            guild_id, group_id, invitee_id,
        )

    async def delete_group_invite(
        self, guild_id: int, group_id: str, invitee_id: int
    ) -> None:
        await self.execute(
            "DELETE FROM group_invites WHERE guild_id=$1 AND group_id=$2 AND invitee_id=$3",
            guild_id, group_id, invitee_id,
        )

    async def get_pending_invites_for_user(self, user_id: int, guild_id: int) -> list[dict]:
        """All pending guild invites for a user, joined with group info."""
        return await self.fetch_all(
            """SELECT gi.*, mg.name as group_name, mg.founder_id
               FROM group_invites gi
               JOIN mining_groups mg ON gi.group_id=mg.group_id AND gi.guild_id=mg.guild_id
               WHERE gi.invitee_id=$1 AND gi.guild_id=$2
               ORDER BY gi.created_at DESC""",
            user_id, guild_id,
        )

    async def get_all_pending_invites(self) -> list[dict]:
        """All pending invites across all guilds (for persistent view registration)."""
        return await self.fetch_all(
            "SELECT guild_id, group_id FROM group_invites",
        )

    # ── Group Pool Proposals ─────────────────────────────────────────────────

    async def create_group_pool_proposal(
        self,
        guild_id: int,
        proposer_group: str,
        target_group: str,
        proposed_by: int,
        token_a: str,
        token_b: str,
    ) -> int:
        """Insert a proposal and return its id."""
        from datetime import datetime, timezone
        row = await self.fetch_one(
            """INSERT INTO group_pool_proposals
               (guild_id, proposer_group, target_group, proposed_by, token_a, token_b, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (guild_id, proposer_group, target_group)
               DO UPDATE SET proposed_by=EXCLUDED.proposed_by,
                             token_a=EXCLUDED.token_a,
                             token_b=EXCLUDED.token_b,
                             created_at=EXCLUDED.created_at
               RETURNING id""",
            guild_id, proposer_group, target_group, proposed_by, token_a, token_b,
            datetime.now(timezone.utc),
        )
        return row["id"]

    async def get_group_pool_proposal(self, proposal_id: int, guild_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM group_pool_proposals WHERE id=$1 AND guild_id=$2",
            proposal_id, guild_id,
        )

    async def get_incoming_pool_proposals(self, guild_id: int, target_group: str) -> list[dict]:
        """Proposals waiting on this group to accept."""
        return await self.fetch_all(
            """SELECT p.*, mg.name AS proposer_name
               FROM group_pool_proposals p
               JOIN mining_groups mg ON mg.group_id=p.proposer_group AND mg.guild_id=p.guild_id
               WHERE p.guild_id=$1 AND p.target_group=$2
               ORDER BY p.created_at DESC""",
            guild_id, target_group,
        )

    async def get_outgoing_pool_proposals(self, guild_id: int, proposer_group: str) -> list[dict]:
        """Proposals this group has sent and are still pending."""
        return await self.fetch_all(
            """SELECT p.*, mg.name AS target_name
               FROM group_pool_proposals p
               JOIN mining_groups mg ON mg.group_id=p.target_group AND mg.guild_id=p.guild_id
               WHERE p.guild_id=$1 AND p.proposer_group=$2
               ORDER BY p.created_at DESC""",
            guild_id, proposer_group,
        )

    async def delete_group_pool_proposal(self, proposal_id: int, guild_id: int) -> None:
        await self.execute(
            "DELETE FROM group_pool_proposals WHERE id=$1 AND guild_id=$2",
            proposal_id, guild_id,
        )

    async def get_all_pending_pool_proposals(self) -> list[dict]:
        """All pending proposals across all guilds (for persistent view registration)."""
        return await self.fetch_all(
            "SELECT id, guild_id, target_group FROM group_pool_proposals",
        )

    # ── Group Upgrades ───────────────────────────────────────────────────────

    async def get_group_upgrades(self, guild_id: int, group_id: str) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM group_upgrades WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )

    async def add_group_upgrade(
        self, guild_id: int, group_id: str, upgrade_id: str
    ) -> None:
        now_dt = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO group_upgrades (guild_id, group_id, upgrade_id, purchased_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT DO NOTHING""",
            guild_id, group_id, upgrade_id, now_dt,
        )

    # ── User Mining Config (solo / pool / group) ─────────────────────────────

    async def get_user_mining_config(self, user_id: int, guild_id: int) -> dict:
        """Return the user's mining config row, defaulting to pool mode."""
        row = await self.fetch_one(
            "SELECT * FROM user_mining_config WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        if row:
            return row
        # If not found, check legacy mining_pool_members table for backwards compat
        is_pool = await self.is_pool_miner(user_id, guild_id)
        return {"user_id": user_id, "guild_id": guild_id, "mode": "pool" if is_pool else "solo"}

    async def set_user_mining_mode(self, user_id: int, guild_id: int, mode: str) -> None:
        """Set user mining mode: 'solo', 'pool', or 'group'."""
        await self.execute(
            """INSERT INTO user_mining_config (user_id, guild_id, mode) VALUES ($1, $2, $3)
               ON CONFLICT(user_id, guild_id) DO UPDATE SET mode=EXCLUDED.mode""",
            user_id, guild_id, mode,
        )
        # Keep legacy table in sync for backward compat
        if mode == "pool":
            await self.execute(
                """INSERT INTO mining_pool_members (user_id, guild_id)
                   VALUES ($1, $2)
                   ON CONFLICT DO NOTHING""",
                user_id, guild_id,
            )
        else:
            await self.execute(
                "DELETE FROM mining_pool_members WHERE user_id=$1 AND guild_id=$2",
                user_id, guild_id,
            )

    async def get_group_miners(self, guild_id: int) -> list[int]:
        """Return user_ids that are currently in 'group' mining mode."""
        rows = await self.fetch_all(
            "SELECT user_id FROM user_mining_config WHERE guild_id=$1 AND mode='group'",
            guild_id,
        )
        return [r["user_id"] for r in rows]

    async def get_mining_mode_counts(self, guild_id: int) -> dict[str, int]:
        """Return counts of miners per mode for display in block embeds."""
        solo_count = len(await self.get_solo_miners(guild_id))
        pool_count = len(await self.get_pool_miners(guild_id))
        group_count = len(await self.get_group_miners(guild_id))
        return {"solo": solo_count, "pool": pool_count, "group": group_count}

    # ── Chain Blocks ────────────────────────────────────────────────────────

    async def create_chain_block(
        self, guild_id: int, block_num: int, block_hash: str, tx_count: int, network: str = ""
    ) -> None:
        ts_dt = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO chain_blocks (block_num, guild_id, block_hash, tx_count, ts, network)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT DO NOTHING""",
            block_num, guild_id, block_hash, tx_count, ts_dt, network,
        )

    async def get_oldest_pending_chain_blocks(self, guild_id: int, limit: int = 1, network: str | None = None) -> list[dict]:
        """Return the oldest pending (unmined) chain blocks, FIFO order."""
        if network:
            return await self.fetch_all(
                """SELECT * FROM chain_blocks
                   WHERE guild_id=$1 AND status='pending' AND network=$2
                   ORDER BY block_num ASC LIMIT $3""",
                guild_id, network, limit,
            )
        return await self.fetch_all(
            """SELECT * FROM chain_blocks
               WHERE guild_id=$1 AND status='pending'
               ORDER BY block_num ASC LIMIT $2""",
            guild_id, limit,
        )

    async def mine_chain_block(self, guild_id: int, block_num: int, miner_id: int | None, network: str = "sun") -> None:
        """Mark a pending chain block as mined."""
        now_dt = datetime.now(timezone.utc)
        await self.execute(
            """UPDATE chain_blocks SET status='mined', miner_id=$1, mined_at=$2
               WHERE guild_id=$3 AND network=$4 AND block_num=$5""",
            miner_id, now_dt, guild_id, network, block_num,
        )

    async def create_and_mine_chain_block(
        self, guild_id: int, block_num: int, block_hash: str, miner_id: int | None, network: str = "sun"
    ) -> None:
        """Create a chain block already in mined state (fallback when no pending block exists)."""
        now_dt = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO chain_blocks (block_num, guild_id, block_hash, tx_count, ts, status, miner_id, mined_at, network)
               VALUES ($1, $2, $3, 0, $4, 'mined', $5, $6, $7)
               ON CONFLICT DO NOTHING""",
            block_num, guild_id, block_hash, now_dt, miner_id, now_dt, network,
        )

    async def get_latest_chain_block(self, guild_id: int, network: str | None = None) -> dict | None:
        if network:
            return await self.fetch_one(
                "SELECT * FROM chain_blocks WHERE guild_id=$1 AND network=$2 ORDER BY block_num DESC LIMIT 1",
                guild_id, network,
            )
        return await self.fetch_one(
            "SELECT * FROM chain_blocks WHERE guild_id=$1 ORDER BY block_num DESC LIMIT 1",
            guild_id,
        )

    async def get_chain_block(self, guild_id: int, block_num: int, network: str | None = None) -> dict | None:
        if network:
            return await self.fetch_one(
                "SELECT * FROM chain_blocks WHERE guild_id=$1 AND block_num=$2 AND network=$3",
                guild_id, block_num, network,
            )
        return await self.fetch_one(
            "SELECT * FROM chain_blocks WHERE guild_id=$1 AND block_num=$2",
            guild_id, block_num,
        )

    async def get_recent_chain_blocks(self, guild_id: int, limit: int = 10, network: str | None = None) -> list[dict]:
        if network:
            return await self.fetch_all(
                "SELECT * FROM chain_blocks WHERE guild_id=$1 AND network=$2 ORDER BY block_num DESC LIMIT $3",
                guild_id, network, limit,
            )
        return await self.fetch_all(
            "SELECT * FROM chain_blocks WHERE guild_id=$1 ORDER BY block_num DESC LIMIT $2",
            guild_id, limit,
        )

    async def get_chain_block_by_hash(self, guild_id: int, block_hash: str) -> dict | None:
        """Fetch a chain (ledger) block by its hash."""
        return await self.fetch_one(
            "SELECT * FROM chain_blocks WHERE guild_id=$1 AND block_hash=$2",
            guild_id, block_hash,
        )

    async def get_chain_block_txns(
        self, guild_id: int, block_num: int, limit: int = 20, network: str | None = None
    ) -> list[dict]:
        """Fetch all transactions included in a specific chain block, optionally filtered by network."""
        if network:
            return await self.fetch_all(
                """SELECT * FROM transactions
                   WHERE guild_id=$1 AND block_num=$2 AND tx_hash LIKE $3
                   ORDER BY ts ASC LIMIT $4""",
                guild_id, block_num, f"{network}:%", limit,
            )
        return await self.fetch_all(
            "SELECT * FROM transactions WHERE guild_id=$1 AND block_num=$2 ORDER BY ts ASC LIMIT $3",
            guild_id, block_num, limit,
        )
