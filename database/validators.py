"""Validators repository  -  NPC validators, PoS validators, mempool, treasury, base fees (PostgreSQL)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from core.config import Config
from .base import PgBaseRepo


def _to_dt(value) -> datetime:
    """Ensure a value is a timezone-aware datetime for TIMESTAMPTZ columns."""
    if isinstance(value, datetime):
        return value
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


class PgValidatorsRepo(PgBaseRepo):

    # ── NPC Validators ────────────────────────────────────────────────────

    # Renamed validator IDs: old → new.  Stakes on old IDs are migrated
    # to the new IDs so players keep their staked positions.
    _VALIDATOR_RENAMES: dict[str, str] = {
        "EIGEN":  "EIGENV",   # EigenLayer ARC
    }

    async def seed_validators(self, guild_id: int) -> None:
        async with self.transaction() as conn:
            for vid, data in Config.VALIDATORS.items():
                await conn.execute(
                    """INSERT INTO validators
                       (validator_id, guild_id, name, emoji, network, uptime_rate, reward_rate, slash_rate)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                       ON CONFLICT (validator_id, guild_id) DO UPDATE SET
                           name=EXCLUDED.name,
                           emoji=EXCLUDED.emoji,
                           network=EXCLUDED.network,
                           uptime_rate=EXCLUDED.uptime_rate,
                           reward_rate=EXCLUDED.reward_rate,
                           slash_rate=EXCLUDED.slash_rate""",
                    vid, guild_id, data["name"], data["emoji"], data.get("network", ""),
                    data["uptime_rate"], data["reward_rate"], data["slash_rate"],
                )
            # Migrate stakes from renamed validators before orphan recovery
            await self._migrate_renamed_stakes(guild_id, conn=conn)
            # Before removing stale validators, refund any orphaned stakes to players
            await self._recover_orphaned_stakes_txn(guild_id, conn=conn)
            # Remove validators no longer defined in config
            valid_ids = list(Config.VALIDATORS.keys())
            placeholders = ",".join(f"${i}" for i in range(2, 2 + len(valid_ids)))
            await conn.execute(
                f"DELETE FROM validators WHERE guild_id=$1 AND validator_id NOT IN ({placeholders})",
                guild_id, *valid_ids,
            )

    async def _migrate_renamed_stakes(self, guild_id: int, *, conn=None) -> None:
        """Move stakes from old (renamed) validator IDs to their new IDs.

        For each old→new mapping, any stake rows on the old ID are merged into
        the new validator.  If a user already has a stake on the new validator,
        the amounts are summed; otherwise the row is simply re-pointed.
        """
        _conn = conn

        async def _run(c):
            for old_vid, new_vid in self._VALIDATOR_RENAMES.items():
                # Find stakes still referencing the old validator ID
                old_stakes = await c.fetch(
                    "SELECT user_id, symbol, amount FROM stakes "
                    "WHERE validator_id=$1 AND guild_id=$2 AND amount > 0",
                    old_vid, guild_id,
                )

                if not old_stakes:
                    continue

                for s in old_stakes:
                    uid, symbol, amount = s["user_id"], s["symbol"], s["amount"]
                    # Ensure a row exists on the new validator
                    await c.execute(
                        "INSERT INTO stakes (user_id, guild_id, validator_id, symbol) "
                        "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
                        uid, guild_id, new_vid, symbol,
                    )
                    # Add the old amount to the new validator stake
                    await c.execute(
                        "UPDATE stakes SET amount = amount + $1 "
                        "WHERE user_id=$2 AND guild_id=$3 AND validator_id=$4 AND symbol=$5",
                        amount, uid, guild_id, new_vid, symbol,
                    )
                    # Zero out the old stake row
                    await c.execute(
                        "UPDATE stakes SET amount = 0 "
                        "WHERE user_id=$1 AND guild_id=$2 AND validator_id=$3 AND symbol=$4",
                        uid, guild_id, old_vid, symbol,
                    )

        if _conn is not None:
            await _run(_conn)
        else:
            async with self.transaction() as c:
                await _run(c)

    async def recover_orphaned_stakes(self, guild_id: int) -> list[dict]:
        """Find stakes whose validator_id no longer exists in the validators table
        and refund the amounts to each player's DeFi wallet (or CeFi holdings as fallback).

        Returns a list of recovery records for logging/auditing.
        """
        async with self.transaction() as conn:
            return await self._recover_orphaned_stakes_txn(guild_id, conn=conn)

    async def _recover_orphaned_stakes_txn(self, guild_id: int, *, conn) -> list[dict]:
        """Inner implementation  -  runs inside an existing transaction connection."""
        # Stakes that reference a validator not in the validators table
        orphaned_rows = await conn.fetch(
            """SELECT s.*
               FROM stakes s
               LEFT JOIN validators v ON s.validator_id=v.validator_id AND s.guild_id=v.guild_id
               WHERE s.guild_id=$1 AND s.amount > 0 AND v.validator_id IS NULL""",
            guild_id,
        )

        orphaned = [dict(r) for r in orphaned_rows]

        if not orphaned:
            return []

        # Build a mapping: validator_id → network (from Config, last known good)
        _vid_to_net = {vid: cfg.get("network", "") for vid, cfg in Config.VALIDATORS.items()}

        # Also build network → short code mapping
        _NET_SHORT = {
            "Sun Network":      "sun",
            "Moneta Chain":  "mta",
            "Arcadia Network": "arc",
            "Discoin Network":  "dsc",
        }

        recovered = []
        for stake in orphaned:
            uid     = stake["user_id"]
            vid     = stake["validator_id"]
            symbol  = stake["symbol"]
            amount  = stake["amount"]

            # Determine network from config (best guess) or symbol heuristics
            network = _vid_to_net.get(vid, "")
            net_short = _NET_SHORT.get(network, "")

            if net_short:
                # Credit to DeFi wallet
                await conn.execute(
                    "INSERT INTO wallet_holdings (user_id, guild_id, network, symbol) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                    uid, guild_id, net_short, symbol,
                )
                await conn.execute(
                    "UPDATE wallet_holdings SET amount = amount + $1 WHERE user_id=$2 AND guild_id=$3 AND network=$4 AND symbol=$5",
                    amount, uid, guild_id, net_short, symbol,
                )
            else:
                # Fallback: credit to CeFi holdings
                await conn.execute(
                    "INSERT INTO crypto_holdings (user_id, guild_id, symbol) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                    uid, guild_id, symbol,
                )
                await conn.execute(
                    "UPDATE crypto_holdings SET amount = amount + $1 WHERE user_id=$2 AND guild_id=$3 AND symbol=$4",
                    amount, uid, guild_id, symbol,
                )

            # Zero out the orphaned stake
            await conn.execute(
                "UPDATE stakes SET amount=0 WHERE user_id=$1 AND guild_id=$2 AND validator_id=$3 AND symbol=$4",
                uid, guild_id, vid, symbol,
            )
            recovered.append({
                "user_id": uid, "validator_id": vid, "symbol": symbol,
                "amount": amount, "credited_to": net_short or "cefi",
            })

        return recovered

    async def get_validators(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM validators WHERE guild_id=$1 ORDER BY reward_rate DESC",
            guild_id,
        )

    async def get_validator(self, validator_id: str, guild_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM validators WHERE validator_id=$1 AND guild_id=$2",
            validator_id, guild_id,
        )

    # ── Stakes ────────────────────────────────────────────────────────────

    async def update_stake(
        self, user_id: int, guild_id: int, validator_id: str, symbol: str, delta: float
    ) -> float:
        await self.execute(
            """INSERT INTO stakes (user_id, guild_id, validator_id, symbol)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT DO NOTHING""",
            user_id, guild_id, validator_id, symbol,
        )
        row = await self.fetch_one(
            "UPDATE stakes SET "
            "amount = CASE WHEN amount + $1 < 0 AND amount + $1 > -0.01 THEN 0 ELSE amount + $1 END, "
            # Only stamp staked_at when creating a brand-new position (amount was 0).
            # Top-ups keep the original timestamp; each deposit gets its own lock via
            # stake_batches instead.
            "staked_at = CASE WHEN amount = 0 AND $1 > 0 THEN now() ELSE staked_at END, "
            "session_earned = CASE "
            "  WHEN (CASE WHEN amount + $1 < 0 AND amount + $1 > -0.01 THEN 0 ELSE amount + $1 END) = 0 THEN 0 "
            "  ELSE session_earned END "
            "WHERE user_id=$2 AND guild_id=$3 AND validator_id=$4 AND symbol=$5 AND amount + $6 >= -0.01 "
            "RETURNING amount",
            delta, user_id, guild_id, validator_id, symbol, delta,
        )
        if row is None:
            raise ValueError(f"Insufficient stake (need {-delta:.4f})")
        # Track supply change (slashing burns tokens  -  negative delta decreases supply)
        if delta != 0:
            await self.execute(
                "UPDATE crypto_prices SET circulating_supply = GREATEST(0, circulating_supply + $1) "
                "WHERE guild_id = $2 AND symbol = $3",
                delta, guild_id, symbol,
            )
            await self.execute(
                "UPDATE guild_tokens SET circulating_supply = GREATEST(0, circulating_supply + $1) "
                "WHERE guild_id = $2 AND symbol = $3",
                delta, guild_id, symbol,
            )
        return row["amount"]

    async def get_user_stakes(self, user_id: int, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            """SELECT s.*, v.name, v.emoji, v.reward_rate, v.uptime_rate, v.slash_rate, v.network
               FROM stakes s
               JOIN validators v ON s.validator_id=v.validator_id AND s.guild_id=v.guild_id
               WHERE s.user_id=$1 AND s.guild_id=$2 AND s.amount > 0""",
            user_id, guild_id,
        )

    async def get_all_guild_stakes(self, guild_id: int) -> list[dict]:
        """All stakes across all users in a guild  -  used for bulk net worth computation."""
        return await self.fetch_all(
            "SELECT user_id, symbol, SUM(amount) AS amount FROM stakes WHERE guild_id=$1 AND amount > 0 GROUP BY user_id, symbol",
            guild_id,
        )

    # ── Stake batches (per-deposit lock tracking) ─────────────────────────

    async def insert_stake_batch(
        self, user_id: int, guild_id: int, validator_id: str, symbol: str, amount: float
    ) -> None:
        """Record a new stake deposit as its own lock batch."""
        await self.execute(
            "INSERT INTO stake_batches (user_id, guild_id, validator_id, symbol, amount) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id, guild_id, validator_id, symbol, amount,
        )

    async def get_stake_batches(
        self, user_id: int, guild_id: int, validator_id: str
    ) -> list[dict]:
        """Return all non-zero batches for a position, oldest first."""
        return await self.fetch_all(
            "SELECT * FROM stake_batches "
            "WHERE user_id=$1 AND guild_id=$2 AND validator_id=$3 AND amount > 0 "
            "ORDER BY staked_at ASC",
            user_id, guild_id, validator_id,
        )

    async def consume_stake_batches(
        self, user_id: int, guild_id: int, validator_id: str, amount: float, lock_secs: float
    ) -> tuple[float, list[dict]]:
        """Consume up to *amount* from the oldest unlocked batches (FIFO).

        Returns ``(consumed, still_locked)`` where *consumed* is how much was
        actually deducted from batches and *still_locked* is the list of batch
        rows that are not yet past their lock period.

        Any portion of the stake that has no corresponding batch (e.g. auto-
        compounded rewards) is treated as freely unlocked.
        """
        import time as _time
        now = _time.time()
        batches = await self.get_stake_batches(user_id, guild_id, validator_id)

        # No batches  -  pre-migration stake or auto-compounded-only position;
        # treat entire amount as freely unlocked (backward compat).
        if not batches:
            return amount, []

        still_locked: list[dict] = []
        remaining = amount
        for b in batches:
            _sa = b.get("staked_at")
            staked_at = _sa.timestamp() if hasattr(_sa, "timestamp") else float(_sa or 0)
            if staked_at + lock_secs > now:
                still_locked.append(b)
                continue
            # Unlocked  -  consume as much as needed from this batch
            take = min(float(b["amount"]), remaining)
            if take <= 0:
                break
            new_amt = float(b["amount"]) - take
            if new_amt < 0.000001:
                await self.execute(
                    "DELETE FROM stake_batches WHERE id=$1", b["id"]
                )
            else:
                await self.execute(
                    "UPDATE stake_batches SET amount=$1 WHERE id=$2", new_amt, b["id"]
                )
            remaining -= take
            if remaining <= 0:
                break

        consumed = amount - max(remaining, 0.0)
        return consumed, still_locked

    async def get_stakes_for_validator(self, validator_id: str, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            """SELECT * FROM stakes
               WHERE validator_id=$1 AND guild_id=$2 AND amount > 0""",
            validator_id, guild_id,
        )

    async def create_validator(
        self, validator_id: str, guild_id: int, name: str, network: str,
        uptime_rate: float, reward_rate: float, slash_rate: float, emoji: str = "🌐",
    ) -> None:
        await self.execute(
            """INSERT INTO validators
               (validator_id, guild_id, name, emoji, network, uptime_rate, reward_rate, slash_rate)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT(validator_id, guild_id) DO UPDATE SET
                   name=EXCLUDED.name, emoji=EXCLUDED.emoji, network=EXCLUDED.network,
                   uptime_rate=EXCLUDED.uptime_rate, reward_rate=EXCLUDED.reward_rate,
                   slash_rate=EXCLUDED.slash_rate""",
            validator_id, guild_id, name, emoji, network, uptime_rate, reward_rate, slash_rate,
        )

    async def update_validator_field(
        self, validator_id: str, guild_id: int, field: str, value
    ) -> None:
        _ALLOWED = {"name", "emoji", "network", "uptime_rate", "reward_rate", "slash_rate", "heat"}
        if field not in _ALLOWED:
            raise ValueError(f"Unknown validator field: {field}")
        await self.execute(
            f"UPDATE validators SET {field}=$1 WHERE validator_id=$2 AND guild_id=$3",
            value, validator_id, guild_id,
        )

    async def update_validator_heat(
        self, validator_id: str, guild_id: int, heat: float
    ) -> None:
        """Persist a validator's new heat value, clamped to [-1, 1] defensively."""
        clamped = max(-1.0, min(1.0, float(heat)))
        await self.execute(
            "UPDATE validators SET heat=$1 WHERE validator_id=$2 AND guild_id=$3",
            clamped, validator_id, guild_id,
        )

    async def delete_validator(self, validator_id: str, guild_id: int) -> None:
        await self.execute(
            "DELETE FROM validators WHERE validator_id=$1 AND guild_id=$2",
            validator_id, guild_id,
        )

    async def clear_user_stakes(self, user_id: int, guild_id: int) -> list[dict]:
        """Remove all stakes for user; returns list of {validator_id, symbol, amount} for refunds."""
        rows = await self.fetch_all(
            "SELECT validator_id, symbol, amount FROM stakes WHERE user_id=$1 AND guild_id=$2 AND amount > 0",
            user_id, guild_id,
        )
        await self.execute(
            "DELETE FROM stakes WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        return rows

    async def clear_validator_stakes(self, validator_id: str, guild_id: int) -> list[dict]:
        """Remove all stakes on a validator; returns list of {user_id, symbol, amount} for refunds."""
        rows = await self.fetch_all(
            "SELECT user_id, symbol, amount FROM stakes WHERE validator_id=$1 AND guild_id=$2 AND amount > 0",
            validator_id, guild_id,
        )
        await self.execute(
            "DELETE FROM stakes WHERE validator_id=$1 AND guild_id=$2",
            validator_id, guild_id,
        )
        return rows

    # ── PoS Validators (player-driven) ──────────────────────────────────────────

    async def create_pos_validator(
        self,
        user_id: int,
        guild_id: int,
        network: str,
        stake_token: str,
        stake_amount: float,
        lock_until: float,
    ) -> None:
        await self.execute(
            """INSERT INTO pos_validators
               (user_id, guild_id, network, stake_token, stake_amount, stake_locked_until, is_active)
               VALUES ($1, $2, $3, $4, $5, $6, TRUE)
               ON CONFLICT(user_id, guild_id, network) DO UPDATE SET
                   stake_amount=EXCLUDED.stake_amount,
                   stake_locked_until=GREATEST(pos_validators.stake_locked_until, EXCLUDED.stake_locked_until),
                   is_active=TRUE""",
            user_id, guild_id, network, stake_token, stake_amount, _to_dt(lock_until),
        )

    async def get_pos_validator(
        self, user_id: int, guild_id: int, network: str
    ) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM pos_validators WHERE user_id=$1 AND guild_id=$2 AND network=$3",
            user_id, guild_id, network,
        )

    async def get_pos_validators(self, guild_id: int) -> list[dict]:
        """All pos_validators for a guild (active and inactive)."""
        return await self.fetch_all(
            "SELECT * FROM pos_validators WHERE guild_id=$1 ORDER BY stake_amount DESC",
            guild_id,
        )

    async def get_pos_validators_for_user(self, user_id: int, guild_id: int) -> list[dict]:
        """All pos_validators registered by a player across all networks."""
        return await self.fetch_all(
            "SELECT * FROM pos_validators WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def get_pos_validators_for_network(self, guild_id: int, network: str) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM pos_validators WHERE guild_id=$1 AND network=$2 ORDER BY stake_amount DESC",
            guild_id, network,
        )

    async def get_user_pos_validators(self, user_id: int, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM pos_validators WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def update_pos_validator_stake(
        self, user_id: int, guild_id: int, network: str, delta: float
    ) -> float:
        row = await self.fetch_one(
            "UPDATE pos_validators SET stake_amount = CASE WHEN stake_amount + $1 < 0 AND stake_amount + $1 > -0.01 THEN 0 ELSE stake_amount + $1 END "
            "WHERE user_id=$2 AND guild_id=$3 AND network=$4 AND stake_amount + $5 >= -0.01 "
            "RETURNING stake_amount",
            delta, user_id, guild_id, network, delta,
        )
        if row is None:
            raise ValueError(f"Insufficient stake (need {-delta:.4f})")
        return row["stake_amount"]

    async def set_commission_rate(self, user_id: int, guild_id: int, network: str, rate: float) -> None:
        """Set the validator's commission rate and record the change timestamp for cooldown."""
        await self.execute(
            "UPDATE pos_validators SET commission_rate=$1, last_commission_change=now() "
            "WHERE user_id=$2 AND guild_id=$3 AND network=$4",
            rate, user_id, guild_id, network,
        )

    async def reactivate_pos_validator(self, user_id: int, guild_id: int, network: str) -> None:
        await self.execute(
            "UPDATE pos_validators SET is_active=TRUE, slash_count=0 WHERE user_id=$1 AND guild_id=$2 AND network=$3",
            user_id, guild_id, network,
        )

    async def deactivate_pos_validator(self, user_id: int, guild_id: int, network: str) -> None:
        await self.execute(
            "UPDATE pos_validators SET is_active=FALSE, stake_amount=0 WHERE user_id=$1 AND guild_id=$2 AND network=$3",
            user_id, guild_id, network,
        )

    async def slash_pos_validator(
        self, user_id: int, guild_id: int, network: str, slash_rate: float
    ) -> dict:
        """Slash a validator's stake. Returns {slashed_amount, new_stake, slash_count}."""
        v = await self.get_pos_validator(user_id, guild_id, network)
        if not v:
            return {}
        slash_amount = float(v["stake_amount"]) * slash_rate
        new_stake = max(0.0, float(v["stake_amount"]) - slash_amount)
        new_slash_count = v["slash_count"] + 1
        is_active = True if new_slash_count < 5 else False  # auto-deactivate at 5 slashes (raised from 3)
        await self.execute(
            """UPDATE pos_validators
               SET stake_amount=$1, slash_count=$2, is_active=$3
               WHERE user_id=$4 AND guild_id=$5 AND network=$6""",
            new_stake, new_slash_count, is_active, user_id, guild_id, network,
        )
        # Track supply burn from slashing
        stake_token = v.get("stake_token", "")
        if slash_amount > 0 and stake_token:
            await self.execute(
                "UPDATE crypto_prices SET circulating_supply = GREATEST(0, circulating_supply - $1) "
                "WHERE guild_id = $2 AND symbol = $3",
                slash_amount, guild_id, stake_token,
            )
            await self.execute(
                "UPDATE guild_tokens SET circulating_supply = GREATEST(0, circulating_supply - $1) "
                "WHERE guild_id = $2 AND symbol = $3",
                slash_amount, guild_id, stake_token,
            )
        return {
            "slashed_amount": slash_amount,
            "new_stake": new_stake,
            "slash_count": new_slash_count,
            "deactivated": not is_active,
        }

    async def increment_validator_blocks(
        self, user_id: int, guild_id: int, reward: float
    ) -> None:
        await self.execute(
            """UPDATE pos_validators
               SET total_blocks_validated = total_blocks_validated + 1,
                   total_rewards_earned   = total_rewards_earned + $1
               WHERE user_id=$2 AND guild_id=$3""",
            reward, user_id, guild_id,
        )

    async def decay_validator_slashes(self, guild_id: int, decay_secs: int) -> None:
        """Decay slash_count by 1 for validators whose last update was > decay_secs ago.

        This gives validators a path to recovery instead of permanent deactivation.
        Uses updated_at as the timestamp  -  each slash triggers an UPDATE which refreshes it,
        and this decay also triggers an UPDATE, so decay happens at most once per window.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - decay_secs
        cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        await self.execute(
            """UPDATE pos_validators
               SET slash_count = GREATEST(slash_count - 1, 0),
                   is_active = CASE WHEN slash_count - 1 < 5 AND stake_amount > 0 THEN true ELSE is_active END
               WHERE guild_id = $1
                 AND slash_count > 0
                 AND updated_at < $2""",
            guild_id, cutoff_dt,
        )

    # ── Delegations ──────────────────────────────────────────────────────────────

    async def create_or_add_delegation(
        self,
        delegator_id: int,
        validator_user_id: int,
        guild_id: int,
        network: str,
        token: str,
        amount: float,
        lock_until: float,
    ) -> float:
        """Upsert a delegation row, adding to existing amount. Returns new total amount."""
        await self.execute(
            """INSERT INTO pos_delegations
               (delegator_id, validator_user_id, guild_id, network, token, amount, locked_until)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT(delegator_id, validator_user_id, guild_id, network) DO UPDATE SET
                   amount       = pos_delegations.amount + EXCLUDED.amount,
                   locked_until = GREATEST(pos_delegations.locked_until, EXCLUDED.locked_until)""",
            delegator_id, validator_user_id, guild_id, network, token, amount, _to_dt(lock_until),
        )
        row = await self.fetch_one(
            "SELECT amount FROM pos_delegations WHERE delegator_id=$1 AND validator_user_id=$2 AND guild_id=$3 AND network=$4",
            delegator_id, validator_user_id, guild_id, network,
        )
        return row["amount"] if row else amount

    async def remove_delegation(
        self,
        delegator_id: int,
        validator_user_id: int,
        guild_id: int,
        network: str,
        amount: float,
    ) -> float:
        """Subtract amount from delegation. Resets session_earned on full exit. Returns new amount."""
        row = await self.fetch_one(
            """UPDATE pos_delegations SET amount = amount - $1,
               session_earned = CASE WHEN amount - $1 <= 0 THEN 0 ELSE session_earned END
               WHERE delegator_id=$2 AND validator_user_id=$3 AND guild_id=$4 AND network=$5
               AND amount - $6 >= 0
               RETURNING amount""",
            amount, delegator_id, validator_user_id, guild_id, network, amount,
        )
        if row is None:
            raise ValueError(f"Insufficient delegation (need {amount:.4f})")
        return row["amount"]

    async def get_delegation(
        self, delegator_id: int, validator_user_id: int, guild_id: int, network: str
    ) -> dict | None:
        return await self.fetch_one(
            """SELECT * FROM pos_delegations
               WHERE delegator_id=$1 AND validator_user_id=$2 AND guild_id=$3 AND network=$4""",
            delegator_id, validator_user_id, guild_id, network,
        )

    async def get_delegations_for_validator(
        self, validator_user_id: int, guild_id: int, network: str
    ) -> list[dict]:
        """All active delegations for a validator (used at block time for reward split)."""
        return await self.fetch_all(
            """SELECT * FROM pos_delegations
               WHERE validator_user_id=$1 AND guild_id=$2 AND network=$3 AND amount > 0""",
            validator_user_id, guild_id, network,
        )

    async def get_user_delegations(self, delegator_id: int, guild_id: int) -> list[dict]:
        """All active delegations made by a user."""
        return await self.fetch_all(
            "SELECT * FROM pos_delegations WHERE delegator_id=$1 AND guild_id=$2 AND amount > 0",
            delegator_id, guild_id,
        )

    async def get_all_guild_delegations(self, guild_id: int) -> list[dict]:
        """All active delegations across all users in a guild (bulk, for leaderboard)."""
        return await self.fetch_all(
            "SELECT delegator_id AS user_id, token, amount FROM pos_delegations "
            "WHERE guild_id=$1 AND amount > 0",
            guild_id,
        )

    async def get_total_delegated_stake(
        self, validator_user_id: int, guild_id: int, network: str
    ) -> float:
        """Sum of all active delegations to a validator on a network."""
        val = await self.fetch_val(
            """SELECT COALESCE(SUM(amount), 0.0) FROM pos_delegations
               WHERE validator_user_id=$1 AND guild_id=$2 AND network=$3 AND amount > 0""",
            validator_user_id, guild_id, network,
        )
        return val if val is not None else 0.0

    async def wipe_delegations_for_validator(
        self, validator_user_id: int, guild_id: int, network: str
    ) -> list[dict]:
        """Zero out all delegations for a validator. Returns the rows before zeroing (for refunds)."""
        rows = await self.fetch_all(
            """SELECT * FROM pos_delegations
               WHERE validator_user_id=$1 AND guild_id=$2 AND network=$3 AND amount > 0""",
            validator_user_id, guild_id, network,
        )
        if rows:
            await self.execute(
                """UPDATE pos_delegations SET amount=0
                   WHERE validator_user_id=$1 AND guild_id=$2 AND network=$3""",
                validator_user_id, guild_id, network,
            )
        return rows

    async def slash_pos_delegations(
        self, validator_user_id: int, guild_id: int, network: str, slash_rate: float
    ) -> list[dict]:
        """Pro-rata slash all delegators at slash_rate. Returns list of {delegator_id, slashed_amount, new_amount}."""
        rows = await self.get_delegations_for_validator(validator_user_id, guild_id, network)
        results = []
        for d in rows:
            slash_amount = float(d["amount"]) * slash_rate
            new_amount = max(0.0, float(d["amount"]) - slash_amount)
            await self.execute(
                """UPDATE pos_delegations SET amount=$1
                   WHERE delegator_id=$2 AND validator_user_id=$3 AND guild_id=$4 AND network=$5""",
                new_amount, d["delegator_id"], validator_user_id, guild_id, network,
            )
            results.append({
                "delegator_id": d["delegator_id"],
                "slashed_amount": slash_amount,
                "new_amount": new_amount,
                "token": d["token"],
            })
        # Track total delegation slash burn in supply
        total_slashed = sum(r["slashed_amount"] for r in results)
        token = results[0]["token"] if results else ""
        if total_slashed > 0 and token:
            await self.execute(
                "UPDATE crypto_prices SET circulating_supply = GREATEST(0, circulating_supply - $1) "
                "WHERE guild_id = $2 AND symbol = $3",
                total_slashed, guild_id, token,
            )
            await self.execute(
                "UPDATE guild_tokens SET circulating_supply = GREATEST(0, circulating_supply - $1) "
                "WHERE guild_id = $2 AND symbol = $3",
                total_slashed, guild_id, token,
            )
        return results

    async def increment_delegation_earned(
        self,
        delegator_id: int,
        validator_user_id: int,
        guild_id: int,
        network: str,
        amount: float,
    ) -> None:
        """Accumulate earnings for a delegator row (both session and lifetime)."""
        await self.execute(
            """UPDATE pos_delegations SET total_earned = total_earned + $1, session_earned = session_earned + $1
               WHERE delegator_id=$2 AND validator_user_id=$3 AND guild_id=$4 AND network=$5""",
            amount, delegator_id, validator_user_id, guild_id, network,
        )

    # ── Mempool ─────────────────────────────────────────────────────────────────

    async def add_to_mempool(
        self,
        guild_id: int,
        user_id: int,
        network: str,
        action_type: str,
        payload: dict,
        gas_price: str,
        gas_fee: float,
    ) -> int:
        mempool_id = await self.fetch_val(
            """INSERT INTO mempool
               (guild_id, network, user_id, action_type, payload, gas_price, gas_fee, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
               RETURNING id""",
            guild_id, network, user_id, action_type, json.dumps(payload), gas_price, gas_fee,
        )

        # Log to session file
        try:
            from core.framework import session_log as _sl
            sl = _sl.get()
            if sl is not None:
                # Build a brief payload summary (token pair, amount, etc.)
                extra_parts = []
                for key in ("token_in", "token_out", "symbol", "amount_in", "amount", "amount_usd"):
                    if key in payload:
                        extra_parts.append(f"{key}={payload[key]}")
                # Determine gas coin from network
                _COINS = {
                    "Sun Network":      "SUN",
                    "Moneta Chain":  "MTA",
                    "Arcadia Network": "ARC",
                    "Discoin Network":  "DSC",
                }
                gas_coin = _COINS.get(network, "?")
                sl.mempool_submit(
                    guild_name=f"guild:{guild_id}",
                    user_id=user_id,
                    network=network,
                    action_type=action_type,
                    mempool_id=mempool_id,
                    gas_fee=gas_fee,
                    gas_coin=gas_coin,
                    extra=" ".join(extra_parts),
                )
        except Exception as _log_exc:
            import logging as _logging
            _logging.getLogger("discoin.validators").debug(
                "submit_to_mempool: non-critical logging step failed: %s", _log_exc
            )

        return mempool_id

    async def get_pending_mempool(
        self, guild_id: int, network: str | None, limit: int = 50
    ) -> list[dict]:
        if network:
            return await self.fetch_all(
                """SELECT * FROM mempool
                   WHERE guild_id=$1 AND network=$2 AND status='pending'
                   ORDER BY gas_fee DESC, submitted_at ASC
                   LIMIT $3""",
                guild_id, network, limit,
            )
        else:
            return await self.fetch_all(
                """SELECT * FROM mempool
                   WHERE guild_id=$1 AND status='pending'
                   ORDER BY gas_fee DESC, submitted_at ASC
                   LIMIT $2""",
                guild_id, limit,
            )

    async def resolve_mempool_action(
        self, action_id: int, status: str, block_id: int
    ) -> bool:
        """Mark a mempool action as confirmed or rejected, linking it to a validator block.

        Returns True if the row was updated (was still pending), False if it was
        already resolved  -  guards against double-processing in multi-process deploys.
        """
        result = await self.execute(
            "UPDATE mempool SET status=$1, block_id=$2 WHERE id=$3 AND status='pending'",
            status, block_id, action_id,
        )
        return self._row_count(result) > 0

    async def get_mempool_action(self, action_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM mempool WHERE id=$1", action_id,
        )

    async def cancel_mempool_action(self, action_id: int, user_id: int) -> bool:
        """Cancel a pending mempool action. Returns True if cancelled."""
        row = await self.fetch_one(
            "SELECT * FROM mempool WHERE id=$1 AND user_id=$2 AND status='pending'",
            action_id, user_id,
        )
        if not row:
            return False
        await self.execute(
            "UPDATE mempool SET status='cancelled' WHERE id=$1", action_id,
        )
        return True

    # ── Validator Blocks ─────────────────────────────────────────────────────────

    async def create_validator_block(
        self, guild_id: int, network: str, validator_id: int
    ) -> int:
        return await self.fetch_val(
            """INSERT INTO validator_blocks (guild_id, network, validator_id, status)
               VALUES ($1, $2, $3, 'processing')
               RETURNING id""",
            guild_id, network, validator_id,
        )

    async def confirm_validator_block(
        self,
        block_id: int,
        total_gas: float,
        validator_reward: float,
        treasury_cut: float,
    ) -> None:
        now = datetime.now(timezone.utc)
        await self.execute(
            """UPDATE validator_blocks SET
               status='confirmed',
               total_gas_collected=$1,
               validator_reward=$2,
               treasury_cut=$3,
               action_count=(SELECT COUNT(*) FROM mempool WHERE block_id=$4),
               confirmed_at=$5
               WHERE id=$6""",
            total_gas, validator_reward, treasury_cut, block_id, now, block_id,
        )

    async def get_recent_validator_blocks(
        self, guild_id: int, network: str | None = None, limit: int = 10
    ) -> list[dict]:
        if network:
            return await self.fetch_all(
                """SELECT * FROM validator_blocks WHERE guild_id=$1 AND network=$2 AND status='confirmed'
                   ORDER BY confirmed_at DESC LIMIT $3""",
                guild_id, network, limit,
            )
        return await self.fetch_all(
            """SELECT * FROM validator_blocks WHERE guild_id=$1 AND status='confirmed'
               ORDER BY confirmed_at DESC LIMIT $2""",
            guild_id, limit,
        )

    async def get_validator_block(self, block_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM validator_blocks WHERE id=$1", block_id,
        )

    # ── Guild Treasury ───────────────────────────────────────────────────────────

    async def add_to_treasury(self, guild_id: int, amount: float) -> float:
        now = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO guild_treasury (guild_id, balance, updated_at) VALUES ($1, $2, $3)
               ON CONFLICT(guild_id) DO UPDATE SET
                   balance = guild_treasury.balance + EXCLUDED.balance,
                   updated_at = EXCLUDED.updated_at""",
            guild_id, amount, now,
        )
        val = await self.fetch_val(
            "SELECT balance FROM guild_treasury WHERE guild_id=$1", guild_id,
        )
        return val if val is not None else 0.0

    async def get_treasury(self, guild_id: int) -> float:
        val = await self.fetch_val(
            "SELECT balance FROM guild_treasury WHERE guild_id=$1", guild_id,
        )
        return val if val is not None else 0.0

    # ── Network Vaults (per-network server progression) ─────────────────────────

    async def add_to_vault(self, guild_id: int, network: str, amount: float) -> dict:
        """Deposit fees into a network vault. Returns {balance, level}."""
        now = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO network_vaults (guild_id, network, balance, updated_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT(guild_id, network) DO UPDATE SET
                   balance = network_vaults.balance + EXCLUDED.balance,
                   updated_at = EXCLUDED.updated_at""",
            guild_id, network.lower(), amount, now,
        )
        row = await self.fetch_one(
            "SELECT balance, level FROM network_vaults WHERE guild_id=$1 AND network=$2",
            guild_id, network.lower(),
        )
        return {"balance": float(row["balance"]) if row else 0.0,
                "level": int(row["level"]) if row else 0}

    async def get_vault(self, guild_id: int, network: str) -> dict:
        """Get a single network vault. Returns {balance, level}."""
        row = await self.fetch_one(
            "SELECT balance, level FROM network_vaults WHERE guild_id=$1 AND network=$2",
            guild_id, network.lower(),
        )
        if not row:
            return {"balance": 0.0, "level": 0}
        return {"balance": float(row["balance"]), "level": int(row["level"])}

    async def get_all_vaults(self, guild_id: int) -> list[dict]:
        """Get all network vaults for a guild."""
        rows = await self.fetch_all(
            "SELECT network, balance, level FROM network_vaults WHERE guild_id=$1 ORDER BY network",
            guild_id,
        )
        return [{"network": r["network"], "balance": float(r["balance"]),
                 "level": int(r["level"])} for r in (rows or [])]

    async def set_vault_level(self, guild_id: int, network: str, level: int) -> None:
        """Update the level for a network vault."""
        await self.execute(
            """INSERT INTO network_vaults (guild_id, network, level)
               VALUES ($1, $2, $3)
               ON CONFLICT(guild_id, network) DO UPDATE SET level = GREATEST(network_vaults.level, EXCLUDED.level)""",
            guild_id, network.lower(), level,
        )

    # ── Network Base Fees (EIP-1559) ─────────────────────────────────────────────

    async def get_base_fee(self, guild_id: int, network: str) -> float:
        """Get current base fee for a guild+network (human-readable), or initial if not set."""
        from cogs.validators import INITIAL_BASE_GAS
        from core.framework.scale import to_human
        row = await self.fetch_one(
            "SELECT base_fee FROM network_base_fees WHERE guild_id=$1 AND network=$2",
            guild_id, network,
        )
        if row:
            return to_human(int(row["base_fee"]))
        return INITIAL_BASE_GAS.get(network, INITIAL_BASE_GAS["Sun Network"])

    async def set_base_fee(self, guild_id: int, network: str, base_fee: float) -> None:
        """Set the base fee for a guild+network (accepts human-readable float, stores raw-scaled)."""
        from core.framework.scale import to_raw
        now = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO network_base_fees (guild_id, network, base_fee, updated_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT(guild_id, network) DO UPDATE SET
                   base_fee = EXCLUDED.base_fee,
                   updated_at = EXCLUDED.updated_at""",
            guild_id, network, to_raw(base_fee), now,
        )
