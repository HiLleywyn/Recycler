"""Users repository (PostgreSQL)  -  wallets, holdings, jobs, loans, savings, addresses."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from core.config import Config
from core.framework.scale import require_raw, to_human, to_raw
from .base import PgBaseRepo

log = logging.getLogger(__name__)


def _to_dt(value) -> datetime:
    """Ensure a value is a timezone-aware datetime for TIMESTAMPTZ columns.
    Accepts datetime (returned as-is) or numeric epoch (converted)."""
    if isinstance(value, datetime):
        return value
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


class PgUsersRepo(PgBaseRepo):
    _SHOP_TABLES = {
        "hashstone": "hashstones",
        "lockstone": "lockstones",
        "vaultstone": "vaultstones",
        "liqstone": "liqstones",
        # Themed leveled stones for the four minigame surfaces.
        "tidestone":  "tidestones",
        "heartstone": "heartstones",
        "cryptstone": "cryptstones",
        "bloodstone": "bloodstones",
        "bloomstone": "bloomstones",
        "validator_guard": "validator_guard_inventory",
        "yield_guard": "yield_guard_inventory",
    }

    # ── Users ──────────────────────────────────────────────────────────────

    async def ensure_user(self, user_id: int, guild_id: int, username: str = "") -> dict:
        # ``last_activity = now()`` on every call. This is the
        # canonical "user just touched the bot" timestamp:
        # ``ensure_registered`` (core/framework/middleware.py) calls
        # ensure_user on every command invocation, so any command
        # bump-stamps the player as active. Without this, the column
        # stayed NULL forever (the schema has no DEFAULT) and the
        # wealth-equalizer UBI eligibility filter
        # ``WHERE last_activity > now() - make_interval(days => 7)``
        # rejected every user (NULL > anything is NULL i.e. false),
        # which manifested as "0 people received UBI" on every cycle.
        await self.execute(
            "INSERT INTO users (user_id, guild_id, username, last_activity) "
            "VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (user_id, guild_id) DO UPDATE SET last_activity = now()",
            user_id, guild_id, username,
        )
        # Keep username in sync if provided and changed
        if username:
            await self.execute(
                "UPDATE users SET username = $3 WHERE user_id = $1 AND guild_id = $2 AND username != $3",
                user_id, guild_id, username,
            )
        return await self.fetch_one(
            "SELECT * FROM users WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def get_user(self, user_id: int, guild_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM users WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def update_wallet(self, user_id: int, guild_id: int, delta: int) -> int:
        delta = require_raw(delta, "update_wallet delta")
        await self.ensure_user(user_id, guild_id)
        row = await self.fetch_val(
            "UPDATE users SET wallet = wallet + $1 WHERE user_id=$2 AND guild_id=$3 AND (wallet + $4) >= 0"
            " RETURNING wallet",
            delta, user_id, guild_id, delta,
        )
        if row is None:
            raise ValueError(f"Insufficient wallet balance (need ${to_human(abs(delta)):,.2f})")
        return int(row)

    async def update_bank(self, user_id: int, guild_id: int, delta: int) -> int:
        delta = require_raw(delta, "update_bank delta")
        await self.ensure_user(user_id, guild_id)
        row = await self.fetch_val(
            "UPDATE users SET bank = bank + $1 WHERE user_id=$2 AND guild_id=$3 AND (bank + $4) >= 0"
            " RETURNING bank",
            delta, user_id, guild_id, delta,
        )
        if row is None:
            raise ValueError(f"Insufficient bank balance (need ${to_human(abs(delta)):,.2f})")
        return int(row)

    async def deduct_liquid(self, user_id: int, guild_id: int, amount: int) -> None:
        """Atomically deduct *amount* from wallet+bank combined.

        Drains wallet first; bank covers any remainder.
        Raises ValueError if wallet+bank < amount.
        """
        amount = require_raw(amount, "deduct_liquid amount")
        await self.ensure_user(user_id, guild_id)
        row = await self.fetch_one(
            """UPDATE users
               SET wallet = GREATEST(0, wallet - $1),
                   bank   = bank - GREATEST(0, $1 - wallet)
               WHERE user_id=$2 AND guild_id=$3
                 AND (wallet + bank) >= $4
               RETURNING wallet, bank""",
            amount, user_id, guild_id, amount,
        )
        if row is None:
            raise ValueError(f"Insufficient liquid balance (need ${to_human(amount):,.2f})")

    async def deduct_liquid_in_conn(
        self, conn, user_id: int, guild_id: int, amount: int
    ) -> None:
        """Same as ``deduct_liquid`` but runs against a caller-provided asyncpg
        connection so it participates in an existing transaction. Use this
        whenever the deduction must succeed or fail atomically with a
        downstream INSERT/UPDATE -- e.g. paying for a hatch and writing the
        cc_buddies row in the same transaction.

        Caller is responsible for ensuring the ``users`` row exists (use
        the ``@ensure_registered`` decorator or call ``ensure_user`` first);
        we deliberately skip it here to avoid acquiring a second pool
        connection mid-transaction.
        """
        amount = require_raw(amount, "deduct_liquid amount")
        row = await conn.fetchrow(
            """UPDATE users
               SET wallet = GREATEST(0, wallet - $1),
                   bank   = bank - GREATEST(0, $1 - wallet)
               WHERE user_id=$2 AND guild_id=$3
                 AND (wallet + bank) >= $4
               RETURNING wallet, bank""",
            amount, user_id, guild_id, amount,
        )
        if row is None:
            raise ValueError(f"Insufficient liquid balance (need ${to_human(amount):,.2f})")

    async def deposit_to_bank(self, user_id: int, guild_id: int, amount: int) -> tuple[int, int]:
        """Atomically move *amount* from wallet to bank.

        Returns (new_wallet, new_bank).
        Raises ValueError if wallet balance is insufficient.
        """
        amount = require_raw(amount, "deposit_to_bank amount")
        if amount <= 0:
            raise ValueError("Deposit amount must be positive.")
        await self.ensure_user(user_id, guild_id)
        row = await self.fetch_one(
            "UPDATE users SET wallet = wallet - $1, bank = bank + $1"
            " WHERE user_id=$2 AND guild_id=$3 AND wallet >= $4"
            " RETURNING wallet, bank",
            amount, user_id, guild_id, amount,
        )
        if row is None:
            raise ValueError(f"Insufficient wallet balance (need ${to_human(amount):,.2f})")
        return int(row["wallet"]), int(row["bank"])

    async def withdraw_from_bank(self, user_id: int, guild_id: int, amount: int) -> tuple[int, int]:
        """Atomically move *amount* from bank to wallet.

        Returns (new_wallet, new_bank).
        Raises ValueError if bank balance is insufficient.
        """
        amount = require_raw(amount, "withdraw_from_bank amount")
        if amount <= 0:
            raise ValueError("Withdrawal amount must be positive.")
        await self.ensure_user(user_id, guild_id)
        row = await self.fetch_one(
            "UPDATE users SET bank = bank - $1, wallet = wallet + $1"
            " WHERE user_id=$2 AND guild_id=$3 AND bank >= $4"
            " RETURNING wallet, bank",
            amount, user_id, guild_id, amount,
        )
        if row is None:
            raise ValueError(f"Insufficient bank balance (need ${to_human(amount):,.2f})")
        return int(row["wallet"]), int(row["bank"])

    async def set_cooldown(self, user_id: int, guild_id: int, field: str) -> None:
        allowed = {"last_daily", "last_work", "last_activity"}
        if field not in allowed:
            raise ValueError(f"Unknown cooldown field: {field}")
        await self.execute(
            f"UPDATE users SET {field}=$1 WHERE user_id=$2 AND guild_id=$3",
            datetime.now(timezone.utc), user_id, guild_id,
        )

    async def update_streak(self, user_id: int, guild_id: int, streak: int, last_daily) -> None:
        await self.execute(
            "UPDATE users SET daily_streak=$1, last_daily=$2 WHERE user_id=$3 AND guild_id=$4",
            streak, _to_dt(last_daily), user_id, guild_id,
        )

    async def get_leaderboard(
        self, guild_id: int, limit: int = 10, exclude_user_id: int | None = None
    ) -> list[dict]:
        """Return top users by wallet+bank, optionally excluding a specific user (e.g. bot)."""
        if exclude_user_id is not None:
            return await self.fetch_all(
                "SELECT * FROM users WHERE guild_id=$1 AND user_id != $2 "
                "ORDER BY (wallet + bank) DESC LIMIT $3",
                guild_id, exclude_user_id, limit,
            )
        return await self.fetch_all(
            "SELECT * FROM users WHERE guild_id=$1 ORDER BY (wallet + bank) DESC LIMIT $2",
            guild_id, limit,
        )

    async def get_trading_leaderboard(self, guild_id: int, limit: int = 200) -> list[dict]:
        """Top traders by realized P&L, computed from transaction history."""
        return await self.fetch_all(
            """SELECT user_id,
                      COALESCE(SUM(CASE WHEN tx_type = 'SELL' THEN amount_out ELSE 0 END), 0)
                      - COALESCE(SUM(CASE WHEN tx_type = 'BUY'  THEN amount_in  ELSE 0 END), 0)
                          AS realized_pnl,
                      COUNT(*) AS total_trades
               FROM transactions
               WHERE guild_id = $1 AND tx_type IN ('BUY', 'SELL')
               GROUP BY user_id
               HAVING COUNT(*) > 0
               ORDER BY realized_pnl DESC
               LIMIT $2""",
            guild_id, limit,
        )

    async def get_all_guild_users(self, guild_id: int, exclude_user_id: int | None = None) -> list[dict]:
        """All users in a guild  -  used for bulk net worth computation."""
        if exclude_user_id is not None:
            return await self.fetch_all(
                "SELECT * FROM users WHERE guild_id=$1 AND user_id != $2",
                guild_id, exclude_user_id,
            )
        return await self.fetch_all(
            "SELECT * FROM users WHERE guild_id=$1", guild_id,
        )

    async def get_all_guild_crypto_holdings(self, guild_id: int) -> list[dict]:
        """All CeFi crypto holdings for a guild (bulk net worth)."""
        return await self.fetch_all(
            "SELECT user_id, symbol, amount FROM crypto_holdings WHERE guild_id=$1 AND amount > 0",
            guild_id,
        )

    async def get_all_guild_wallet_holdings(self, guild_id: int) -> list[dict]:
        """All DeFi wallet holdings for a guild (bulk net worth)."""
        return await self.fetch_all(
            "SELECT user_id, symbol, amount FROM wallet_holdings WHERE guild_id=$1 AND amount > 0",
            guild_id,
        )

    async def get_leaderboard_by_token(self, guild_id: int, symbol: str, limit: int = 50) -> list[dict]:
        """Top holders of a specific token, combining CeFi + DeFi + staked holdings."""
        return await self.fetch_all(
            """SELECT user_id, SUM(amount) AS amount FROM (
                   SELECT user_id, amount FROM crypto_holdings WHERE guild_id=$1 AND symbol=$2 AND amount > 0
                   UNION ALL
                   SELECT user_id, amount FROM wallet_holdings WHERE guild_id=$3 AND symbol=$4 AND amount > 0
                   UNION ALL
                   SELECT user_id, amount FROM stakes WHERE guild_id=$5 AND symbol=$6 AND amount > 0
               ) sub GROUP BY user_id ORDER BY amount DESC LIMIT $7""",
            guild_id, symbol, guild_id, symbol, guild_id, symbol, limit,
        )

    # ── Crypto Holdings ────────────────────────────────────────────────────

    async def get_holding(self, user_id: int, guild_id: int, symbol: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM crypto_holdings WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol,
        )

    async def get_holdings(self, user_id: int, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM crypto_holdings WHERE user_id=$1 AND guild_id=$2 AND amount > 0 ORDER BY symbol",
            user_id, guild_id,
        )

    async def _clamp_mint_delta(self, guild_id: int, symbol: str, delta: int) -> int:
        """Cap a positive (mint) delta so circulating_supply cannot exceed
        max_supply.  Burns / debits (delta <= 0) pass through unchanged.

        Built-in tokens read max_supply from Config.TOKENS (human units);
        custom guild tokens read it from the guild_tokens table (raw scaled).
        Tokens without a configured cap are uncapped.  Wrapped coins (those
        with a ``peg_to`` in config, e.g. MMTA/MSUN) are also uncapped.
        Returns 0 when the token is already at or above its cap, and logs a
        warning whenever the requested mint is reduced."""
        if delta <= 0:
            return delta
        cfg = Config.TOKENS.get(symbol)
        if cfg is not None:
            # Wrapped coins (MMTA/MSUN) are 1:1 collateral-backed IOUs: each
            # unit is minted by `.moon wrap` burning an equal unit of native
            # coin. Their supply is bounded by that locked collateral, never
            # by a fixed cap, so the mint must not be clamped. Clamping it
            # lets `.moon wrap` burn the native coin without minting the
            # wrapper -- silently breaking the 1:1 peg and destroying funds.
            if cfg.get("peg_to"):
                return delta
            max_h = cfg.get("max_supply") or 0
            max_raw = to_raw(max_h) if max_h else 0
        else:
            gt_max = await self.fetch_val(
                "SELECT max_supply FROM guild_tokens WHERE guild_id=$1 AND symbol=$2",
                guild_id, symbol,
            )
            max_raw = int(gt_max or 0)
        if max_raw <= 0:
            return delta
        current_raw = int(await self.fetch_val(
            "SELECT COALESCE(circulating_supply, 0) FROM crypto_prices "
            "WHERE guild_id=$1 AND symbol=$2",
            guild_id, symbol,
        ) or 0)
        headroom = max_raw - current_raw
        if headroom <= 0:
            log.warning(
                "Mint blocked at supply cap: %s on guild %s "
                "(current=%s max=%s requested=%s)",
                symbol, guild_id, current_raw, max_raw, delta,
            )
            return 0
        if delta > headroom:
            log.warning(
                "Mint clamped at supply cap: %s on guild %s "
                "(requested=%s minted=%s)",
                symbol, guild_id, delta, headroom,
            )
            return headroom
        return delta

    async def update_holding(self, user_id: int, guild_id: int, symbol: str, delta: int) -> int:
        delta = require_raw(delta, "update_holding delta")
        if delta > 0:
            delta = await self._clamp_mint_delta(guild_id, symbol, delta)
        await self.execute(
            "INSERT INTO crypto_holdings (user_id, guild_id, symbol) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            user_id, guild_id, symbol,
        )
        row = await self.fetch_val(
            "UPDATE crypto_holdings SET amount = amount + $1"
            " WHERE user_id=$2 AND guild_id=$3 AND symbol=$4 AND (amount + $5) >= 0"
            " RETURNING amount",
            delta, user_id, guild_id, symbol, delta,
        )
        if row is None:
            raise ValueError(f"Insufficient {symbol} holdings (need {to_human(-delta):.4f})")
        # Track circulating supply change (built-in tokens in crypto_prices,
        # custom tokens in guild_tokens  -  try both, ignore if row doesn't exist)
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
        return int(row)

    # ── Admin Helpers ──────────────────────────────────────────────────────

    async def set_wallet(self, user_id: int, guild_id: int, amount: int) -> None:
        await self.ensure_user(user_id, guild_id)
        await self.execute(
            "UPDATE users SET wallet=$1 WHERE user_id=$2 AND guild_id=$3",
            max(0, amount), user_id, guild_id,
        )

    async def set_bank(self, user_id: int, guild_id: int, amount: int) -> None:
        await self.ensure_user(user_id, guild_id)
        await self.execute(
            "UPDATE users SET bank=$1 WHERE user_id=$2 AND guild_id=$3",
            max(0, amount), user_id, guild_id,
        )

    async def set_holding(self, user_id: int, guild_id: int, symbol: str, amount: int) -> None:
        await self.execute(
            "INSERT INTO crypto_holdings (user_id, guild_id, symbol) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            user_id, guild_id, symbol,
        )
        await self.execute(
            "UPDATE crypto_holdings SET amount=$1 WHERE user_id=$2 AND guild_id=$3 AND symbol=$4",
            max(0, amount), user_id, guild_id, symbol,
        )

    async def reset_user(self, user_id: int, guild_id: int) -> None:
        """Wipe all economy data for one user in one guild.

        Must cover every table that contributes to ``compute_net_worth``
        in ``services/net_worth.py`` -- if a category shows up on
        ``,balance`` and isn't deleted here, ``,admin reset user``
        silently keeps that value live.
        """
        async with self.transaction() as conn:
            # Sum all token holdings before deleting  -  adjust circulating supply.
            # Every table shaped (user_id, guild_id, symbol, amount) where
            # ``amount`` is raw token units against the main supply ledger
            # (crypto_prices / guild_tokens). Game-state stake columns like
            # user_fishing.lure_staked_raw are NOT in this list because they
            # represent tokens already removed from circulating_supply at
            # stake time -- mirrors the pre-existing convention.
            _supply_tables = [
                ("crypto_holdings",      "symbol", "amount", "user_id"),
                ("wallet_holdings",      "symbol", "amount", "user_id"),
                ("stakes",               "symbol", "amount", "user_id"),
                ("safety_module_stakes", "symbol", "amount", "user_id"),
                ("gamba_stakes",         "symbol", "amount", "user_id"),
                ("lunar_stakes",         "symbol", "amount", "user_id"),
            ]
            to_deduct: dict[str, int] = {}
            for tbl, sym_col, amt_col, uid_col in _supply_tables:
                try:
                    rows = await conn.fetch(
                        f"SELECT {sym_col}, SUM({amt_col}) as total FROM {tbl} "
                        f"WHERE {uid_col} = $1 AND guild_id = $2 GROUP BY {sym_col}",
                        user_id, guild_id,
                    )
                except Exception:
                    # Optional tables (gamba/safety/moon) may not exist on
                    # older DB snapshots that haven't run the relevant
                    # migration yet -- skip silently like the bulk-NW path.
                    continue
                for r in rows:
                    to_deduct[r[sym_col]] = to_deduct.get(r[sym_col], 0) + int(r["total"])
            # pos_delegations uses delegator_id + token (not symbol)
            del_rows = await conn.fetch(
                "SELECT token as symbol, SUM(amount) as total FROM pos_delegations "
                "WHERE delegator_id = $1 AND guild_id = $2 GROUP BY token",
                user_id, guild_id,
            )
            for r in del_rows:
                to_deduct[r["symbol"]] = to_deduct.get(r["symbol"], 0) + int(r["total"])
            # moon_stakes is MOON-only (no ``symbol`` column).
            try:
                moon_row = await conn.fetchrow(
                    "SELECT amount FROM moon_stakes WHERE user_id=$1 AND guild_id=$2",
                    user_id, guild_id,
                )
                if moon_row and int(moon_row["amount"] or 0) > 0:
                    to_deduct["MOON"] = to_deduct.get("MOON", 0) + int(moon_row["amount"])
            except Exception:
                pass
            for sym, amt in to_deduct.items():
                if amt > 0:
                    await conn.execute(
                        "UPDATE crypto_prices SET circulating_supply = GREATEST(0, circulating_supply - $1) "
                        "WHERE guild_id = $2 AND symbol = $3", amt, guild_id, sym,
                    )
                    await conn.execute(
                        "UPDATE guild_tokens SET circulating_supply = GREATEST(0, circulating_supply - $1) "
                        "WHERE guild_id = $2 AND symbol = $3", amt, guild_id, sym,
                    )

            # Tables keyed on (user_id, guild_id). Optional tables (newer
            # migrations) are wrapped in a per-table try/except so a stale
            # DB snapshot never aborts the reset.
            for table in [
                "users",
                "crypto_holdings",
                "wallet_holdings",
                "stakes",
                "loans",
                "savings_deposits",
                "mining_rigs",
                "mining_pool_members",
                "mining_group_members",
                "user_mining_config",
                "lp_positions",
                "lp_snapshots",
                "user_jobs",
                "wallet_addresses",
                "pos_validators",
                "game_sessions",
                # Stones (primary + themed + meta).
                "hashstones",
                "lockstones",
                "vaultstones",
                "gambastones",
                "liqstones",
                "tidestones",
                "heartstones",
                "cryptstones",
                "bloodstones",
                "bloomstones",
                "gavelstones",
                "anvilstones",
                "chimerastones",
                # Consumables.
                "validator_guard_inventory",
                "yield_guard_inventory",
                # Game-state economies (fishing / delve / buddy / farming / crafting).
                "user_fishing",
                "fishing_catches",
                "user_dungeon",
                "dungeon_runs",
                "dungeon_kills",
                "user_buddy_economy",
                "cc_buddy_hatches",
                "user_farming",
                "farming_harvests",
                "farming_pest_battles",
                "user_crafting",
                "crafting_logs",
                # Safety Module + Disc.Fun + Gamba + Moon Network.
                "safety_module_stakes",
                "discfun_stakes",
                "proto_token_holdings",
                "proto_token_trades",
                "gamba_stakes",
                "gamba_chess_stats",
                "gamba_checkers_stats",
                "gamba_consumables",
                "lunar_stakes",
                "moon_stakes",
                # transactions must be wiped too; otherwise the per-job
                # WORK_DAILY_CAP query (get_work_today) keeps counting the
                # user's pre-reset earnings and ,work stays capped.
                "transactions",
            ]:
                try:
                    await conn.execute(
                        f"DELETE FROM {table} WHERE user_id=$1 AND guild_id=$2",
                        user_id, guild_id,
                    )
                except Exception:
                    # Table may not exist yet on stale DB snapshots.
                    continue

            # Tables keyed on non-standard user columns.
            # pos_delegations uses delegator_id
            await conn.execute(
                "DELETE FROM pos_delegations WHERE delegator_id=$1 AND guild_id=$2",
                user_id, guild_id,
            )
            # NFTs use owner_id (nft_listings CASCADEs off nfts.id).
            try:
                await conn.execute(
                    "DELETE FROM nfts WHERE owner_id=$1 AND guild_id=$2",
                    user_id, guild_id,
                )
            except Exception:
                pass
            # cc_buddies (Buddy Network) + dungeon_party use owner_user_id.
            for tbl in ("cc_buddies", "dungeon_party"):
                try:
                    await conn.execute(
                        f"DELETE FROM {tbl} WHERE owner_user_id=$1 AND guild_id=$2",
                        user_id, guild_id,
                    )
                except Exception:
                    continue
            # If user was a group founder, disband their group
            owned = await conn.fetch(
                "SELECT group_id FROM mining_groups WHERE founder_id=$1 AND guild_id=$2",
                user_id, guild_id,
            )
            for row in owned:
                await conn.execute(
                    "DELETE FROM mining_group_members WHERE group_id=$1 AND guild_id=$2",
                    row["group_id"], guild_id,
                )
                await conn.execute(
                    "DELETE FROM mining_groups WHERE group_id=$1 AND guild_id=$2",
                    row["group_id"], guild_id,
                )

    # ── User Jobs ──────────────────────────────────────────────────────────

    async def get_user_job(self, user_id: int, guild_id: int) -> dict:
        await self.execute(
            "INSERT INTO user_jobs (user_id, guild_id, job_id) VALUES ($1, $2, 'HOMELESS') ON CONFLICT DO NOTHING",
            user_id, guild_id,
        )
        return await self.fetch_one(
            "SELECT * FROM user_jobs WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def update_job(
        self, user_id: int, guild_id: int,
        job_id: str, work_count: int, total_earned: int
    ) -> None:
        await self.execute(
            """UPDATE user_jobs SET job_id=$1, work_count=$2, total_earned=$3
               WHERE user_id=$4 AND guild_id=$5""",
            job_id, work_count, total_earned, user_id, guild_id,
        )

    # ── User Preferences (DM toggles) ────────────────────────────────────────────
    #
    # Every DM notification defaults to OFF -- players opt in via
    # ,notify <kind> on. Migration 0208_notify_default_off.sql also
    # flips every existing row to FALSE so already-registered players
    # stop getting DMs immediately on first deploy.
    _PREF_DEFAULTS = {
        "dm_mining": 0, "dm_transfer": 0, "dm_validator": 0, "dm_staking": 0,
        "dm_itemlevelup": 0, "dm_whale_alerts": 0, "dm_2fa": 0,
        "dm_nft": 0, "dm_predictions": 0, "dm_events": 0, "dm_ape": 0,
        "dm_autolevelup": 0,
    }

    async def get_user_prefs(self, user_id: int, guild_id: int) -> dict:
        row = await self.fetch_one(
            "SELECT * FROM user_prefs WHERE user_id=$1 AND guild_id=$2", user_id, guild_id,
        )
        if row:
            result = dict(row)
            for key, default in self._PREF_DEFAULTS.items():
                if result.get(key) is None:
                    result[key] = default
            return result
        return {"user_id": user_id, "guild_id": guild_id, **self._PREF_DEFAULTS}

    async def set_user_pref(self, user_id: int, guild_id: int, column: str, value: int | bool) -> None:
        _ALLOWED = {"dm_mining", "dm_transfer", "dm_validator", "dm_staking", "dm_itemlevelup", "dm_whale_alerts", "dm_2fa", "dm_nft", "dm_predictions", "dm_events", "dm_ape", "dm_autolevelup"}
        if column not in _ALLOWED:
            raise ValueError(f"Unknown pref: {column}")
        await self.execute(
            f"INSERT INTO user_prefs (user_id, guild_id, {column}) VALUES ($1, $2, $3) "
            f"ON CONFLICT(user_id, guild_id) DO UPDATE SET {column}=EXCLUDED.{column}",
            user_id, guild_id, bool(value),
        )

    # ── Per-network notification mutes ──────────────────────────────────

    _MUTED_NET_COLS = {
        "mining": "muted_networks_mining",
        "staking": "muted_networks_staking",
        "validator": "muted_networks_validator",
        "whale": "muted_networks_whale",
    }

    async def get_muted_networks(self, user_id: int, guild_id: int, category: str) -> set[str]:
        """Return the set of muted network short names for a notification category."""
        col = self._MUTED_NET_COLS.get(category)
        if not col:
            return set()
        prefs = await self.get_user_prefs(user_id, guild_id)
        raw = prefs.get(col, "") or ""
        return {n.strip().lower() for n in raw.split(",") if n.strip()}

    async def set_muted_networks(self, user_id: int, guild_id: int, category: str, networks: set[str]) -> None:
        """Replace the muted networks for a notification category."""
        col = self._MUTED_NET_COLS.get(category)
        if not col:
            raise ValueError(f"Unknown mute category: {category}")
        val = ",".join(sorted(networks))
        await self.execute(
            f"INSERT INTO user_prefs (user_id, guild_id, {col}) VALUES ($1, $2, $3) "
            f"ON CONFLICT(user_id, guild_id) DO UPDATE SET {col}=EXCLUDED.{col}",
            user_id, guild_id, val,
        )

    async def toggle_muted_network(self, user_id: int, guild_id: int, category: str, network: str) -> bool:
        """Toggle a network mute. Returns True if now muted, False if unmuted."""
        muted = await self.get_muted_networks(user_id, guild_id, category)
        net = network.strip().lower()
        if net in muted:
            muted.discard(net)
            result = False
        else:
            muted.add(net)
            result = True
        await self.set_muted_networks(user_id, guild_id, category, muted)
        return result

    # ── Profile Customization ────────────────────────────────────────────

    async def set_profile_field(self, user_id: int, guild_id: int, field: str, value) -> None:
        _ALLOWED = {"profile_bio", "profile_title", "profile_color", "profile_banner_url"}
        if field not in _ALLOWED:
            raise ValueError(f"Unknown profile field: {field}")
        await self.execute(
            f"UPDATE users SET {field}=$1 WHERE user_id=$2 AND guild_id=$3",
            value, user_id, guild_id,
        )

    async def get_profile(self, user_id: int, guild_id: int) -> dict | None:
        """Get user profile with customization fields."""
        row = await self.fetch_one(
            "SELECT * FROM users WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        return dict(row) if row else None

    # ── Loans ──────────────────────────────────────────────────────────────

    async def get_loan(self, user_id: int, guild_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM loans WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def upsert_loan(
        self, user_id: int, guild_id: int,
        principal: int, outstanding: int, collateral: int, last_interest,
    ) -> None:
        await self.execute(
            """INSERT INTO loans (user_id, guild_id, principal, outstanding, collateral, last_interest)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT(user_id, guild_id) DO UPDATE SET
                   outstanding=EXCLUDED.outstanding,
                   last_interest=EXCLUDED.last_interest""",
            user_id, guild_id, principal, outstanding, collateral, _to_dt(last_interest),
        )

    async def delete_loan(self, user_id: int, guild_id: int) -> None:
        await self.execute(
            "DELETE FROM loans WHERE user_id=$1 AND guild_id=$2", user_id, guild_id,
        )

    async def get_all_loans(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM loans WHERE guild_id=$1", guild_id,
        )

    # ── Savings Deposits ────────────────────────────────────────────────────

    async def get_savings_deposit(self, user_id: int, guild_id: int, symbol: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM savings_deposits WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol,
        )

    async def savings_deposit(self, user_id: int, guild_id: int, symbol: str, amount: int) -> int:
        """Increment a savings deposit and return the new balance."""
        amount = require_raw(amount, "savings_deposit amount")
        if amount <= 0:
            raise ValueError("Deposit amount must be positive.")
        row = await self.fetch_one(
            """INSERT INTO savings_deposits (user_id, guild_id, symbol, amount, last_interest)
               VALUES ($1, $2, $3, $4, now())
               ON CONFLICT(user_id, guild_id, symbol) DO UPDATE SET
                   amount = savings_deposits.amount + EXCLUDED.amount
               RETURNING amount""",
            user_id, guild_id, symbol.upper(), amount,
        )
        return int(row["amount"]) if row else 0

    async def savings_withdraw(self, user_id: int, guild_id: int, symbol: str, amount: int) -> int:
        """Decrement a savings deposit and return the new balance."""
        amount = require_raw(amount, "savings_withdraw amount")
        if amount <= 0:
            raise ValueError("Withdrawal amount must be positive.")
        symbol = symbol.upper()
        row = await self.fetch_one(
            """UPDATE savings_deposits
               SET amount = amount - $4
               WHERE user_id=$1 AND guild_id=$2 AND symbol=$3 AND amount >= $4
               RETURNING amount""",
            user_id, guild_id, symbol, amount,
        )
        if row is None:
            raise ValueError(
                f"Insufficient savings balance (need {to_human(amount):,.6f} {symbol})."
            )
        return int(row["amount"])

    async def upsert_savings_deposit(
        self, user_id: int, guild_id: int, symbol: str, amount: int, last_interest,
    ) -> None:
        await self.execute(
            """INSERT INTO savings_deposits (user_id, guild_id, symbol, amount, last_interest)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT(user_id, guild_id, symbol) DO UPDATE SET
                   amount=EXCLUDED.amount,
                   last_interest=EXCLUDED.last_interest""",
            user_id, guild_id, symbol, amount, _to_dt(last_interest),
        )

    async def apply_savings_interest(
        self, user_id: int, guild_id: int, symbol: str, multiplier: float, now,
    ) -> int | None:
        """Atomically multiply the current deposit balance by multiplier (compound interest step).

        The multiplier is computed in Python from elapsed time and bonus factors  -  both are
        independent of the deposit amount, so computing them from a snapshot is correct.
        Applying via ROUND(amount * multiplier) in SQL means the live DB value is used, not the
        stale snapshot, so concurrent withdrawals between the tick read and this write are safe.
        Returns the new raw int amount, or None if the deposit no longer exists / amount <= 0.
        """
        row = await self.fetch_one(
            """UPDATE savings_deposits
               SET amount = ROUND(amount * $4::NUMERIC), last_interest = $5
               WHERE user_id=$1 AND guild_id=$2 AND symbol=$3 AND amount > 0
               RETURNING amount""",
            user_id, guild_id, symbol, multiplier, _to_dt(now),
        )
        return int(row["amount"]) if row else None

    async def apply_loan_interest(
        self, user_id: int, guild_id: int, multiplier: float, now,
    ) -> tuple[int, int] | None:
        """Atomically multiply outstanding by multiplier (compound interest step).

        Returns (new_outstanding, collateral) from the live row, or None if the loan
        was repaid between the tick's initial read and this update.
        """
        row = await self.fetch_one(
            """UPDATE loans
               SET outstanding = ROUND(outstanding * $3::NUMERIC), last_interest = $4
               WHERE user_id=$1 AND guild_id=$2 AND outstanding > 0
               RETURNING outstanding, collateral""",
            user_id, guild_id, multiplier, _to_dt(now),
        )
        return (int(row["outstanding"]), int(row["collateral"])) if row else None

    async def add_to_community_reserve(self, guild_id: int, symbol: str, amount: int) -> None:
        """Route a share of protocol fees into the community savings reserve (user_id=0).
        The reserve participates in the savings pool like any normal deposit, increasing
        total liquidity and improving borrowing availability for all users."""
        from core.config import Config
        uid = Config.COMMUNITY_RESERVE_USER_ID
        await self.ensure_user(uid, guild_id)
        await self.execute(
            """INSERT INTO savings_deposits (user_id, guild_id, symbol, amount, last_interest)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT(user_id, guild_id, symbol) DO UPDATE SET
                   amount = savings_deposits.amount + EXCLUDED.amount,
                   last_interest = EXCLUDED.last_interest""",
            uid, guild_id, symbol, amount, datetime.now(timezone.utc),
        )

    async def split_to_community_reserves(
        self, guild_id: int, symbol: str, amount: int, sun_price: float = 0.0
    ) -> None:
        """Route half of a protocol fee into the USD community reserve.

        Half of ``amount`` is deposited into the USD reserve.
        If ``symbol`` is "SUN", the amount is converted to USD first using ``sun_price``.
        """
        half = amount // 2
        if symbol == "USD":
            await self.add_to_community_reserve(guild_id, "USD", half)
        elif symbol == "SUN":
            if sun_price > 0:
                await self.add_to_community_reserve(guild_id, "USD", round(half * sun_price))

    async def delete_savings_deposit(self, user_id: int, guild_id: int, symbol: str) -> None:
        await self.execute(
            "DELETE FROM savings_deposits WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol,
        )

    async def get_all_savings_deposits(self, guild_id: int, symbol: str) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM savings_deposits WHERE guild_id=$1 AND symbol=$2",
            guild_id, symbol,
        )

    async def get_user_savings(self, user_id: int, guild_id: int) -> list[dict]:
        """Compatibility helper: return all positive savings deposits for a user."""
        return await self.fetch_all(
            """SELECT symbol, amount, last_interest, created_at
               FROM savings_deposits
               WHERE user_id=$1 AND guild_id=$2 AND amount > 0
               ORDER BY symbol""",
            user_id, guild_id,
        )

    async def get_savings_totals(self, guild_id: int, symbol: str) -> tuple[int, int]:
        """Return (total_supply, total_borrowed) raw ints for utilization rate calculation.

        symbol must be 'USD' or 'SUN'.
        """
        dep_row = await self.fetch_one(
            "SELECT COALESCE(SUM(amount), 0) as total FROM savings_deposits WHERE guild_id=$1 AND symbol=$2",
            guild_id, symbol,
        )
        explicit_deposits = int(dep_row["total"]) if dep_row else 0

        if symbol == "USD":
            total_supply = explicit_deposits
            bor_row = await self.fetch_one(
                "SELECT COALESCE(SUM(outstanding), 0) as total FROM loans WHERE guild_id=$1",
                guild_id,
            )
            total_borrowed = int(bor_row["total"]) if bor_row else 0
        else:  # SUN
            total_supply = explicit_deposits
            total_borrowed = 0

        return total_supply, total_borrowed

    # ── Safety Module Stakes ────────────────────────────────────────────────

    async def get_sm_stake(self, user_id: int, guild_id: int, symbol: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM safety_module_stakes WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol.upper(),
        )

    async def upsert_sm_stake(
        self,
        user_id: int,
        guild_id: int,
        symbol: str,
        amount: int,
        last_yield,
        staked_at=None,
        cooldown_at=None,
    ) -> None:
        await self.execute(
            """INSERT INTO safety_module_stakes
                   (user_id, guild_id, symbol, amount, last_yield, staked_at, cooldown_at)
               VALUES ($1, $2, $3, $4, $5, COALESCE($6, NOW()), $7)
               ON CONFLICT (user_id, guild_id, symbol) DO UPDATE SET
                   amount=EXCLUDED.amount,
                   last_yield=EXCLUDED.last_yield,
                   cooldown_at=EXCLUDED.cooldown_at""",
            user_id, guild_id, symbol.upper(), amount,
            _to_dt(last_yield), _to_dt(staked_at) if staked_at else None, _to_dt(cooldown_at) if cooldown_at else None,
        )

    async def set_sm_auto_compound(
        self, user_id: int, guild_id: int, symbol: str, enabled: bool,
    ) -> None:
        await self.execute(
            "UPDATE safety_module_stakes SET auto_compound=$4 "
            "WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol.upper(), enabled,
        )

    async def delete_sm_stake(self, user_id: int, guild_id: int, symbol: str) -> None:
        await self.execute(
            "DELETE FROM safety_module_stakes WHERE user_id=$1 AND guild_id=$2 AND symbol=$3",
            user_id, guild_id, symbol.upper(),
        )

    async def get_all_sm_stakes(self, guild_id: int, symbol: str) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM safety_module_stakes WHERE guild_id=$1 AND symbol=$2 AND amount > 0",
            guild_id, symbol.upper(),
        )

    async def get_sm_total_staked(self, guild_id: int, symbol: str) -> int:
        row = await self.fetch_one(
            "SELECT COALESCE(SUM(amount), 0) as total FROM safety_module_stakes WHERE guild_id=$1 AND symbol=$2",
            guild_id, symbol.upper(),
        )
        return int(row["total"]) if row else 0

    # ── Wallet Addresses ────────────────────────────────────────────────────

    async def create_wallet_address(
        self,
        user_id: int,
        guild_id: int,
        label: str | None = None,
        is_temp: bool = False,
        network: str = "",
        address_prefix: str = "",
    ) -> str:
        import secrets
        if address_prefix:
            address = f"{address_prefix.lower()}:{secrets.token_hex(12)}"
        else:
            address = "0x" + secrets.token_hex(20)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=86400) if is_temp else None
        await self.execute(
            """INSERT INTO wallet_addresses (address, user_id, guild_id, label, is_temp, expires_at, created_at, network)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            address, user_id, guild_id, label, is_temp, expires_at, now, network,
        )
        return address

    async def get_wallet_address(self, address: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM wallet_addresses WHERE address=$1", address,
        )

    async def get_user_addresses(self, user_id: int, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM wallet_addresses WHERE user_id=$1 AND guild_id=$2 ORDER BY created_at DESC",
            user_id, guild_id,
        )

    async def has_defi_wallet(self, user_id: int, guild_id: int, net_short: str) -> bool:
        """Check if a user has a DeFi wallet address on a given network prefix."""
        row = await self.fetch_one(
            """SELECT 1 FROM wallet_addresses
               WHERE user_id = $1 AND guild_id = $2
                 AND LOWER(address) LIKE $3 || ':%'
               LIMIT 1""",
            user_id, guild_id, net_short.lower(),
        )
        return row is not None

    async def get_defi_wallet_address(self, user_id: int, guild_id: int, net_short: str) -> dict | None:
        """Return the wallet address row for a user on a given network prefix."""
        return await self.fetch_one(
            """SELECT * FROM wallet_addresses
               WHERE user_id = $1 AND guild_id = $2
                 AND LOWER(address) LIKE $3 || ':%'
               ORDER BY created_at DESC LIMIT 1""",
            user_id, guild_id, net_short.lower(),
        )

    async def delete_wallet_address(self, address: str, user_id: int) -> bool:
        row = await self.fetch_one(
            "SELECT 1 FROM wallet_addresses WHERE address=$1 AND user_id=$2",
            address, user_id,
        )
        if not row:
            return False
        await self.execute(
            "DELETE FROM wallet_addresses WHERE address=$1 AND user_id=$2",
            address, user_id,
        )
        return True

    async def cleanup_expired_addresses(self) -> None:
        await self.execute(
            "DELETE FROM wallet_addresses WHERE is_temp = TRUE AND expires_at IS NOT NULL AND expires_at < now()",
        )

    # ── DeFi Wallet Holdings ───────────────────────────────────────────────

    _NET_FULL = {
            "sun":  "Sun Network",      "sun network":  "Sun Network",
            "mta":  "Moneta Chain",  "moneta":      "Moneta Chain",  "moneta chain": "Moneta Chain",
            "arc":  "Arcadia Network", "arcadia":     "Arcadia Network", "arcadia network": "Arcadia Network",
            "dsc":  "Discoin Network",  "discoin":      "Discoin Network",  "discoin network": "Discoin Network",
            "gam":  "Gamba Network",    "gamba":        "Gamba Network",    "gamba network":   "Gamba Network",
        }

    async def get_wallet_holding(
        self, user_id: int, guild_id: int, net_short: str, symbol: str
    ) -> dict | None:
        """Get a single DeFi wallet holding for a user on a network."""
        return await self.fetch_one(
            """SELECT * FROM wallet_holdings
               WHERE user_id = $1 AND guild_id = $2
                 AND network = $3 AND symbol = $4""",
            user_id, guild_id, net_short.lower(), symbol.upper(),
        )

    async def update_wallet_holding(
        self, user_id: int, guild_id: int, net_short: str, symbol: str, delta: int
    ) -> int:
        """Atomically adjust a DeFi wallet holding by delta. Creates row if needed.
        Returns the new amount (raw scaled int). Raises ValueError if deduction would go negative.

        Positive deltas (mints) are clamped to the token's max_supply via
        ``_clamp_mint_delta`` -- once a token hits its cap, the player's
        balance is not increased and the function returns the existing amount."""
        delta = require_raw(delta, "update_wallet_holding delta")
        sym = symbol.upper()
        if delta > 0:
            delta = await self._clamp_mint_delta(guild_id, sym, delta)
        if delta < 0:
            row = await self.fetch_one(
                """UPDATE wallet_holdings
                   SET amount = amount + $5
                   WHERE user_id = $1 AND guild_id = $2
                     AND network = $3 AND symbol = $4
                     AND amount + $5 >= 0
                   RETURNING amount""",
                user_id, guild_id, net_short.lower(), sym, delta,
            )
            if row is None:
                raise ValueError(f"Insufficient {sym} balance.")
            new_amount = int(row["amount"])
        elif delta > 0:
            row = await self.fetch_one(
                """INSERT INTO wallet_holdings (user_id, guild_id, network, symbol, amount)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (user_id, guild_id, network, symbol)
                   DO UPDATE SET amount = wallet_holdings.amount + EXCLUDED.amount
                   RETURNING amount""",
                user_id, guild_id, net_short.lower(), sym, delta,
            )
            new_amount = int(row["amount"]) if row else 0
        else:
            # delta clamped to 0 (cap reached) OR caller passed 0 explicitly.
            # Return the existing balance without inserting a placeholder row.
            existing = await self.fetch_val(
                "SELECT amount FROM wallet_holdings "
                "WHERE user_id=$1 AND guild_id=$2 AND network=$3 AND symbol=$4",
                user_id, guild_id, net_short.lower(), sym,
            )
            new_amount = int(existing or 0)
        # Track circulating supply change
        if delta != 0:
            await self.execute(
                "UPDATE crypto_prices SET circulating_supply = GREATEST(0, circulating_supply + $1) "
                "WHERE guild_id = $2 AND symbol = $3",
                delta, guild_id, sym,
            )
            await self.execute(
                "UPDATE guild_tokens SET circulating_supply = GREATEST(0, circulating_supply + $1) "
                "WHERE guild_id = $2 AND symbol = $3",
                delta, guild_id, sym,
            )
        return new_amount

    async def get_wallet_holdings_for_network(
        self, user_id: int, guild_id: int, net_short: str
    ) -> list[dict]:
        """All DeFi holdings for a user on a specific network."""
        return await self.fetch_all(
            """SELECT * FROM wallet_holdings
               WHERE user_id = $1 AND guild_id = $2 AND network = $3 AND amount > 0
               ORDER BY symbol""",
            user_id, guild_id, net_short.lower(),
        )

    async def get_all_wallet_holdings(
        self, user_id: int, guild_id: int
    ) -> list[dict]:
        """All DeFi holdings for a user across all networks."""
        return await self.fetch_all(
            """SELECT * FROM wallet_holdings
               WHERE user_id = $1 AND guild_id = $2 AND amount > 0
               ORDER BY network, symbol""",
            user_id, guild_id,
        )

    async def get_shop_item(self, guild_id: int, item_name: str) -> dict | None:
        """Compatibility helper for shop purchases during the migration."""
        del guild_id
        return self._get_shop_item_meta(item_name)

    def _shop_table_for(self, item_id: str) -> str:
        table = self._SHOP_TABLES.get(item_id.lower().strip())
        if not table:
            raise ValueError(f"Unknown shop item: {item_id}")
        return table

    async def add_inventory_item(
        self, user_id: int, guild_id: int, item_id: str, quantity: int = 1
    ) -> None:
        """Compatibility helper for item inventory writes used by the service bridge."""
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
        item = self._get_shop_item_meta(item_id)
        if not item or not item.get("table"):
            raise ValueError(f"Unknown shop item: {item_id}")

        key = item["item_id"]
        if quantity != 1:
            raise ValueError(f"{item['name']} can only be purchased one at a time.")
        table = self._shop_table_for(key)
        exists = await self.fetch_one(
            f"SELECT 1 FROM {table} WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        if exists:
            raise ValueError(f"You already own a {item['name']}.")
        await self.execute(
            f"INSERT INTO {table} (user_id, guild_id) VALUES ($1, $2)",
            user_id, guild_id,
        )

    # ── Shop Item Meta ─────────────────────────────────────────────────────

    def _get_shop_item_meta(self, item_id: str) -> dict | None:
        """Return enriched metadata for a shop item from items_config."""
        from configs.items_config import SHOP_ITEMS
        key = item_id.lower().strip()
        # Try direct key match first, then search by name
        cfg = SHOP_ITEMS.get(key)
        if cfg is None:
            for k, v in SHOP_ITEMS.items():
                if v.get("name", "").lower() == key:
                    cfg = v
                    key = k
                    break
        if cfg is None:
            return None
        table = self._SHOP_TABLES.get(key)
        return {**cfg, "item_id": key, "table": table}

    # ── Generic Leveled-Stone CRUD ───────────────────────────────────────
    # All leveled stones (hashstone, lockstone, vaultstone, liqstone, etc.)
    # share the same table structure: (user_id, guild_id, level, xp, staked_amount, acquired_at).
    # These generic helpers avoid duplicating SQL for each stone type.

    async def _get_stone(self, table: str, user_id: int, guild_id: int) -> dict | None:
        return await self.fetch_one(
            f"SELECT * FROM {table} WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def _create_stone(self, table: str, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self.execute(
            f"INSERT INTO {table} (user_id, guild_id, staked_amount, lp_currency) VALUES ($1, $2, $3, $4)",
            user_id, guild_id, staked_amount, lp_currency,
        )

    async def _delete_stone(self, table: str, user_id: int, guild_id: int) -> None:
        await self.execute(
            f"DELETE FROM {table} WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def _transfer_stone(self, table: str, from_id: int, to_id: int, guild_id: int) -> None:
        await self.execute(
            f"UPDATE {table} SET user_id=$1 WHERE user_id=$2 AND guild_id=$3",
            to_id, from_id, guild_id,
        )

    async def _update_stone_xp(self, table: str, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self.execute(
            f"UPDATE {table} SET xp=$1, level=$2 WHERE user_id=$3 AND guild_id=$4",
            new_xp, new_level, user_id, guild_id,
        )

    async def _add_stone_xp_delta(self, table: str, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        """Atomically add xp_gain to the stone's current XP.

        Returns (live_xp, live_level) from the post-update row, or None if the stone
        doesn't exist. Using a delta-add prevents concurrent XP grants (mining, staking,
        savings ticks) from overwriting each other.
        """
        row = await self.fetch_one(
            f"UPDATE {table} SET xp = xp + $1 WHERE user_id=$2 AND guild_id=$3 RETURNING xp, level",
            xp_gain, user_id, guild_id,
        )
        return (float(row["xp"]), int(row["level"])) if row else None

    async def _add_stone_staked(self, table: str, user_id: int, guild_id: int, amount: int) -> None:
        await self.execute(
            f"UPDATE {table} SET staked_amount = staked_amount + $1 WHERE user_id=$2 AND guild_id=$3",
            amount, user_id, guild_id,
        )

    async def _get_all_guild_stones(self, table: str, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            f"SELECT * FROM {table} WHERE guild_id=$1", guild_id,
        )

    # ── Hashstone ────────────────────────────────────────────────────────

    async def get_hashstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("hashstones", user_id, guild_id)

    async def create_hashstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self._create_stone("hashstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_hashstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("hashstones", user_id, guild_id)

    async def transfer_hashstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("hashstones", from_id, to_id, guild_id)

    async def update_hashstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("hashstones", user_id, guild_id, new_xp, new_level)

    async def add_hashstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("hashstones", user_id, guild_id, xp_gain)

    async def add_hashstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("hashstones", user_id, guild_id, amount)

    async def get_all_guild_hashstones(self, guild_id: int) -> list[dict]:
        """All hashstones in a guild  -  used for bulk net worth computation."""
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM hashstones WHERE guild_id=$1",
            guild_id,
        )

    async def get_all_guild_lockstones(self, guild_id: int) -> list[dict]:
        """All lockstones in a guild  -  used for bulk net worth computation."""
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM lockstones WHERE guild_id=$1",
            guild_id,
        )

    async def get_all_guild_vaultstones(self, guild_id: int) -> list[dict]:
        """All vaultstones in a guild  -  used for bulk net worth computation."""
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM vaultstones WHERE guild_id=$1",
            guild_id,
        )

    async def get_all_guild_validator_guards(self, guild_id: int) -> list[dict]:
        """All validator guard counts per user - used for bulk net worth computation."""
        return await self.fetch_all(
            "SELECT user_id, count FROM validator_guard_inventory WHERE guild_id=$1",
            guild_id,
        )

    async def get_all_guild_yield_guards(self, guild_id: int) -> list[dict]:
        """All yield guard counts per user - used for bulk net worth computation."""
        return await self.fetch_all(
            "SELECT user_id, count FROM yield_guard_inventory WHERE guild_id=$1",
            guild_id,
        )

    # ── Cosmetic consumables ─────────────────────────────────────────────

    async def get_cosmetic_count(
        self, user_id: int, guild_id: int, item_key: str,
    ) -> int:
        val = await self.fetch_val(
            "SELECT COALESCE((cosmetics->>$3)::int, 0) "
            "FROM users WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id, item_key,
        )
        return int(val or 0)

    async def get_cosmetics(self, user_id: int, guild_id: int) -> dict:
        val = await self.fetch_val(
            "SELECT cosmetics FROM users WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        try:
            import json
            return json.loads(val) if isinstance(val, str) else (dict(val) if val else {})
        except Exception:
            return {}

    async def add_cosmetic(
        self, user_id: int, guild_id: int, item_key: str, qty: int,
    ) -> int:
        val = await self.fetch_val(
            "UPDATE users "
            "SET cosmetics = jsonb_set("
            "  cosmetics, "
            "  ARRAY[$3], "
            "  to_jsonb(COALESCE((cosmetics->>$3)::int, 0) + $4)"
            ") "
            "WHERE user_id=$1 AND guild_id=$2 "
            "RETURNING (cosmetics->>$3)::int",
            user_id, guild_id, item_key, qty,
        )
        return int(val or 0)

    async def remove_cosmetic(
        self, user_id: int, guild_id: int, item_key: str,
    ) -> bool:
        """Remove one cosmetic item. Returns True if successfully consumed."""
        current = await self.get_cosmetic_count(user_id, guild_id, item_key)
        if current <= 0:
            return False
        new_count = current - 1
        if new_count == 0:
            await self.execute(
                "UPDATE users SET cosmetics = cosmetics - $3 "
                "WHERE user_id=$1 AND guild_id=$2",
                user_id, guild_id, item_key,
            )
        else:
            await self.execute(
                "UPDATE users "
                "SET cosmetics = jsonb_set(cosmetics, ARRAY[$3], to_jsonb($4::int)) "
                "WHERE user_id=$1 AND guild_id=$2",
                user_id, guild_id, item_key, new_count,
            )
        return True

    # ── Lockstone ────────────────────────────────────────────────────────

    async def get_lockstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("lockstones", user_id, guild_id)

    async def create_lockstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self._create_stone("lockstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_lockstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("lockstones", user_id, guild_id)

    async def transfer_lockstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("lockstones", from_id, to_id, guild_id)

    async def update_lockstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("lockstones", user_id, guild_id, new_xp, new_level)

    async def add_lockstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("lockstones", user_id, guild_id, xp_gain)

    async def add_lockstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("lockstones", user_id, guild_id, amount)

    # ── Vaultstone ───────────────────────────────────────────────────────

    async def get_vaultstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("vaultstones", user_id, guild_id)

    async def create_vaultstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self._create_stone("vaultstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_vaultstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("vaultstones", user_id, guild_id)

    async def transfer_vaultstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("vaultstones", from_id, to_id, guild_id)

    async def update_vaultstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("vaultstones", user_id, guild_id, new_xp, new_level)

    async def add_vaultstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("vaultstones", user_id, guild_id, xp_gain)

    async def add_vaultstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("vaultstones", user_id, guild_id, amount)

    # ── Liqstone ─────────────────────────────────────────────────────────

    async def get_liqstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("liqstones", user_id, guild_id)

    async def create_liqstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self._create_stone("liqstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_liqstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("liqstones", user_id, guild_id)

    async def transfer_liqstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("liqstones", from_id, to_id, guild_id)

    async def update_liqstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("liqstones", user_id, guild_id, new_xp, new_level)

    async def add_liqstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("liqstones", user_id, guild_id, xp_gain)

    async def add_liqstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("liqstones", user_id, guild_id, amount)

    async def get_all_guild_liqstones(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM liqstones WHERE guild_id=$1",
            guild_id,
        )

    # ── Tidestone (fishing) ──────────────────────────────────────────────
    async def get_tidestone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("tidestones", user_id, guild_id)

    async def create_tidestone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self._create_stone("tidestones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_tidestone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("tidestones", user_id, guild_id)

    async def transfer_tidestone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("tidestones", from_id, to_id, guild_id)

    async def update_tidestone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("tidestones", user_id, guild_id, new_xp, new_level)

    async def add_tidestone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("tidestones", user_id, guild_id, xp_gain)

    async def add_tidestone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("tidestones", user_id, guild_id, amount)

    async def get_all_guild_tidestones(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM tidestones WHERE guild_id=$1",
            guild_id,
        )

    # ── Heartstone (buddy) ───────────────────────────────────────────────
    async def get_heartstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("heartstones", user_id, guild_id)

    async def create_heartstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self._create_stone("heartstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_heartstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("heartstones", user_id, guild_id)

    async def transfer_heartstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("heartstones", from_id, to_id, guild_id)

    async def update_heartstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("heartstones", user_id, guild_id, new_xp, new_level)

    async def add_heartstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("heartstones", user_id, guild_id, xp_gain)

    async def add_heartstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("heartstones", user_id, guild_id, amount)

    async def get_all_guild_heartstones(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM heartstones WHERE guild_id=$1",
            guild_id,
        )

    # ── Cryptstone (dungeon) ─────────────────────────────────────────────
    async def get_cryptstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("cryptstones", user_id, guild_id)

    async def create_cryptstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self._create_stone("cryptstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_cryptstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("cryptstones", user_id, guild_id)

    async def transfer_cryptstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("cryptstones", from_id, to_id, guild_id)

    async def update_cryptstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("cryptstones", user_id, guild_id, new_xp, new_level)

    async def add_cryptstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("cryptstones", user_id, guild_id, xp_gain)

    async def add_cryptstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("cryptstones", user_id, guild_id, amount)

    async def get_all_guild_cryptstones(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM cryptstones WHERE guild_id=$1",
            guild_id,
        )

    # ── Bloodstone (buddy battles) ───────────────────────────────────────
    async def get_bloodstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("bloodstones", user_id, guild_id)

    async def create_bloodstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self._create_stone("bloodstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_bloodstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("bloodstones", user_id, guild_id)

    async def transfer_bloodstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("bloodstones", from_id, to_id, guild_id)

    async def update_bloodstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("bloodstones", user_id, guild_id, new_xp, new_level)

    async def add_bloodstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("bloodstones", user_id, guild_id, xp_gain)

    async def add_bloodstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("bloodstones", user_id, guild_id, amount)

    async def get_all_guild_bloodstones(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM bloodstones WHERE guild_id=$1",
            guild_id,
        )

    # ── Bloomstone (farming) ─────────────────────────────────────────────
    async def get_bloomstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("bloomstones", user_id, guild_id)

    async def create_bloomstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "DSD") -> None:
        await self._create_stone("bloomstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_bloomstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("bloomstones", user_id, guild_id)

    async def transfer_bloomstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("bloomstones", from_id, to_id, guild_id)

    async def update_bloomstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("bloomstones", user_id, guild_id, new_xp, new_level)

    async def add_bloomstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("bloomstones", user_id, guild_id, xp_gain)

    async def add_bloomstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("bloomstones", user_id, guild_id, amount)

    async def get_all_guild_bloomstones(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM bloomstones WHERE guild_id=$1",
            guild_id,
        )

    # ── Gavelstone (auction house) ───────────────────────────────────────
    async def get_gavelstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("gavelstones", user_id, guild_id)

    async def create_gavelstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "USD") -> None:
        await self._create_stone("gavelstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_gavelstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("gavelstones", user_id, guild_id)

    async def transfer_gavelstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("gavelstones", from_id, to_id, guild_id)

    async def update_gavelstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("gavelstones", user_id, guild_id, new_xp, new_level)

    async def add_gavelstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("gavelstones", user_id, guild_id, xp_gain)

    async def add_gavelstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("gavelstones", user_id, guild_id, amount)

    async def get_all_guild_gavelstones(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM gavelstones WHERE guild_id=$1",
            guild_id,
        )

    # ── Anvilstone (crafting) ────────────────────────────────────────────
    async def get_anvilstone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("anvilstones", user_id, guild_id)

    async def create_anvilstone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "USD") -> None:
        await self._create_stone("anvilstones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_anvilstone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("anvilstones", user_id, guild_id)

    async def transfer_anvilstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("anvilstones", from_id, to_id, guild_id)

    async def update_anvilstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("anvilstones", user_id, guild_id, new_xp, new_level)

    async def add_anvilstone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("anvilstones", user_id, guild_id, xp_gain)

    async def add_anvilstone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("anvilstones", user_id, guild_id, amount)

    async def get_all_guild_anvilstones(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM anvilstones WHERE guild_id=$1",
            guild_id,
        )

    # ── Chimerastone (AMM swap) ──────────────────────────────────────────
    async def get_chimerastone(self, user_id: int, guild_id: int) -> dict | None:
        return await self._get_stone("chimerastones", user_id, guild_id)

    async def create_chimerastone(self, user_id: int, guild_id: int, staked_amount: int, lp_currency: str = "USD") -> None:
        await self._create_stone("chimerastones", user_id, guild_id, staked_amount, lp_currency)

    async def delete_chimerastone(self, user_id: int, guild_id: int) -> None:
        await self._delete_stone("chimerastones", user_id, guild_id)

    async def transfer_chimerastone(self, from_id: int, to_id: int, guild_id: int) -> None:
        await self._transfer_stone("chimerastones", from_id, to_id, guild_id)

    async def update_chimerastone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        await self._update_stone_xp("chimerastones", user_id, guild_id, new_xp, new_level)

    async def add_chimerastone_xp(self, user_id: int, guild_id: int, xp_gain: float) -> tuple[float, int] | None:
        return await self._add_stone_xp_delta("chimerastones", user_id, guild_id, xp_gain)

    async def add_chimerastone_staked(self, user_id: int, guild_id: int, amount: int) -> None:
        await self._add_stone_staked("chimerastones", user_id, guild_id, amount)

    async def get_all_guild_chimerastones(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT user_id, staked_amount FROM chimerastones WHERE guild_id=$1",
            guild_id,
        )

    # ── Validator Guard (consumable) ─────────────────────────────────────

    async def get_validator_guard_count(self, user_id: int, guild_id: int) -> int:
        row = await self.fetch_one(
            "SELECT count FROM validator_guard_inventory WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        return int(row["count"]) if row else 0

    async def add_validator_guard(self, user_id: int, guild_id: int, quantity: int = 1) -> int:
        row = await self.fetch_one(
            """INSERT INTO validator_guard_inventory (user_id, guild_id, count)
               VALUES ($1, $2, $3)
               ON CONFLICT(user_id, guild_id) DO UPDATE
               SET count = validator_guard_inventory.count + EXCLUDED.count
               RETURNING count""",
            user_id, guild_id, quantity,
        )
        return int(row["count"]) if row else 0

    async def use_validator_guard(self, user_id: int, guild_id: int) -> bool:
        """Consume one validator guard. Returns False if none available."""
        updated = await self.fetch_one(
            """UPDATE validator_guard_inventory SET count = count - 1
               WHERE user_id=$1 AND guild_id=$2 AND count > 0
               RETURNING count""",
            user_id, guild_id,
        )
        if updated is not None:
            try:
                from services import items as _items
                await _items.consume_one(
                    self, guild_id=guild_id, user_id=user_id,
                    contract_address=_items.contract_address("shop", "validator_guard"),
                    reason="shop.activate.validator_guard",
                )
            except Exception:
                log.debug(
                    "nft validator_guard burn sync failed gid=%s uid=%s",
                    guild_id, user_id, exc_info=True,
                )
        return updated is not None

    # ── Yield Guard (consumable) ─────────────────────────────────────────

    async def get_yield_guard_count(self, user_id: int, guild_id: int) -> int:
        row = await self.fetch_one(
            "SELECT count FROM yield_guard_inventory WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        return int(row["count"]) if row else 0

    async def add_yield_guard(self, user_id: int, guild_id: int, quantity: int = 1) -> int:
        row = await self.fetch_one(
            """INSERT INTO yield_guard_inventory (user_id, guild_id, count)
               VALUES ($1, $2, $3)
               ON CONFLICT(user_id, guild_id) DO UPDATE
               SET count = yield_guard_inventory.count + EXCLUDED.count
               RETURNING count""",
            user_id, guild_id, quantity,
        )
        return int(row["count"]) if row else 0

    async def use_yield_guard(self, user_id: int, guild_id: int) -> bool:
        """Consume one yield guard. Returns False if none available."""
        updated = await self.fetch_one(
            """UPDATE yield_guard_inventory SET count = count - 1
               WHERE user_id=$1 AND guild_id=$2 AND count > 0
               RETURNING count""",
            user_id, guild_id,
        )
        if updated is not None:
            try:
                from services import items as _items
                await _items.consume_one(
                    self, guild_id=guild_id, user_id=user_id,
                    contract_address=_items.contract_address("shop", "yield_guard"),
                    reason="shop.activate.yield_guard",
                )
            except Exception:
                log.debug(
                    "nft yield_guard burn sync failed gid=%s uid=%s",
                    guild_id, user_id, exc_info=True,
                )
        return updated is not None

    # ── Group Upgrades ───────────────────────────────────────────────────

    async def get_group_upgrades(self, guild_id: int, group_id: str) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM group_upgrades WHERE guild_id=$1 AND group_id=$2",
            guild_id, group_id,
        )

    async def has_group_upgrade(self, guild_id: int, group_id: str, upgrade_id: str) -> bool:
        row = await self.fetch_one(
            "SELECT 1 FROM group_upgrades WHERE guild_id=$1 AND group_id=$2 AND upgrade_id=$3",
            guild_id, group_id, upgrade_id,
        )
        return row is not None

    async def add_group_upgrade(self, guild_id: int, group_id: str, upgrade_id: str) -> None:
        await self.execute(
            """INSERT INTO group_upgrades (guild_id, group_id, upgrade_id)
               VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
            guild_id, group_id, upgrade_id,
        )

    async def remove_group_upgrade(self, guild_id: int, group_id: str, upgrade_id: str) -> None:
        await self.execute(
            "DELETE FROM group_upgrades WHERE guild_id=$1 AND group_id=$2 AND upgrade_id=$3",
            guild_id, group_id, upgrade_id,
        )

    # ── Bulk item queries (for net worth, profiles, etc.) ────────────────

    async def get_all_user_items(self, user_id: int, guild_id: int) -> dict:
        """Return all item data for a user in one call."""
        return {
            "hashstone": await self.get_hashstone(user_id, guild_id),
            "lockstone": await self.get_lockstone(user_id, guild_id),
            "vaultstone": await self.get_vaultstone(user_id, guild_id),
            "liqstone": await self.get_liqstone(user_id, guild_id),
            "validator_guard_count": await self.get_validator_guard_count(user_id, guild_id),
            "yield_guard_count": await self.get_yield_guard_count(user_id, guild_id),
        }

    async def get_all_guild_items(self, guild_id: int) -> dict:
        """Return aggregate item counts for a guild."""
        result = {}
        for stone_name, table in [("hashstones", "hashstones"), ("lockstones", "lockstones"),
                                   ("vaultstones", "vaultstones"), ("liqstones", "liqstones")]:
            rows = await self.fetch_one(
                f"SELECT COUNT(*) as cnt, COALESCE(SUM(staked_amount), 0) as total_staked FROM {table} WHERE guild_id=$1",
                guild_id,
            )
            result[stone_name] = {"count": rows["cnt"] if rows else 0, "total_staked": float(rows["total_staked"]) if rows else 0.0}
        return result

    async def get_user_loans(self, user_id: int, guild_id: int) -> list[dict]:
        """Return active USD loan positions."""
        results: list[dict] = []
        usd_loan = await self.fetch_one(
            """SELECT principal, outstanding, collateral, last_interest, created_at
               FROM loans
               WHERE user_id=$1 AND guild_id=$2 AND outstanding > 0""",
            user_id, guild_id,
        )
        if usd_loan:
            results.append({**usd_loan, "loan_type": "usd"})
        return results

    # ── AI Conversation & Memory ───────────────────────────────────────────

    async def get_ai_conversation(
        self, user_id: int, guild_id: int, limit: int = 10, history_key: str = "default"
    ) -> list[dict]:
        """Return recent AI conversation history for a user, scoped by agent history_key."""
        rows = await self.fetch_all(
            "SELECT role, content FROM ai_conversations "
            "WHERE user_id=$1 AND guild_id=$2 AND history_key=$3 ORDER BY ts DESC LIMIT $4",
            user_id, guild_id, history_key, limit,
        )
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def get_thread_conversation(
        self, guild_id: int, history_key: str, limit: int = 24
    ) -> list[dict]:
        """Return a shared thread conversation (every speaker), scoped by history_key.

        Unlike ``get_ai_conversation`` this does NOT filter by user_id -- a
        thread is a shared space where anyone can talk to the bot, so the
        transcript is the full set of rows under the thread's history_key.
        """
        rows = await self.fetch_all(
            "SELECT role, content FROM ai_conversations "
            "WHERE guild_id=$1 AND history_key=$2 ORDER BY ts DESC LIMIT $3",
            guild_id, history_key, limit,
        )
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def save_ai_message(
        self, user_id: int, guild_id: int, role: str, content: str, history_key: str = "default"
    ) -> None:
        """Save a single AI conversation message, scoped by agent history_key."""
        await self.execute(
            "INSERT INTO ai_conversations (user_id, guild_id, role, content, history_key) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id, guild_id, role, content, history_key,
        )

    async def clear_ai_conversation(self, user_id: int, guild_id: int) -> int:
        """Delete all AI conversation history for a single user in a guild."""
        result = await self.execute(
            "DELETE FROM ai_conversations WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

    async def clear_all_ai_conversations(self, guild_id: int) -> int:
        """Delete all AI conversation history for every user in a guild."""
        result = await self.execute(
            "DELETE FROM ai_conversations WHERE guild_id=$1",
            guild_id,
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

    # ── AI context opt-out ───────────────────────────────────────────────────
    #
    # Users who opt out have their conversations / memory / traits / social
    # context capture skipped throughout the AI stack. Reading this flag is a
    # single-row primary-key lookup; it's called on every AI turn so it needs
    # to stay fast.

    async def is_ai_opted_out(self, user_id: int, guild_id: int) -> bool:
        row = await self.fetch_one(
            "SELECT 1 FROM ai_opt_outs WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        return row is not None

    async def set_ai_opt_out(self, user_id: int, guild_id: int) -> None:
        await self.execute(
            "INSERT INTO ai_opt_outs (user_id, guild_id) VALUES ($1, $2) "
            "ON CONFLICT (user_id, guild_id) DO NOTHING",
            user_id, guild_id,
        )
        # Purge anything we already learned so the opt-out is retroactive.
        # Same wipe path the ,ai recontext command uses -- single source of
        # truth so the two stay aligned.
        await self.wipe_ai_user_state(user_id, guild_id)

    async def wipe_ai_user_state(self, user_id: int, guild_id: int) -> dict[str, int]:
        """Drop every per-user AI learning row in this guild.

        Wipes the conversation history, the one-line memory summary, the
        layered traits, the reaction-category counters, the tool-use memory,
        and any DiscoAI long-term facts scoped to this user (both the
        guild-specific ``user:<uid>:guild:<gid>`` scope and the global
        ``user:<uid>`` scope). Server-wide context (events, channel feed,
        guild facts) is left intact.

        Used by ``set_ai_opt_out`` for retroactive opt-out and by the
        ``,ai recontext`` command when a user wants a clean slate without
        opting out of future tracking. Returns ``{table: rows_deleted}``
        so callers can tell the user what actually changed.
        """
        deleted: dict[str, int] = {}

        async def _del(table: str, sql: str, *args) -> None:
            try:
                status = await self.execute(sql, *args)
                # asyncpg returns "DELETE n"
                try:
                    deleted[table] = int(status.split()[-1])
                except (AttributeError, ValueError, IndexError):
                    deleted[table] = 0
            except Exception:
                deleted[table] = 0  # table may not exist on older DBs

        await _del(
            "ai_conversations",
            "DELETE FROM ai_conversations WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        await _del(
            "ai_user_memory",
            "DELETE FROM ai_user_memory WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        for table in ("ai_user_traits", "ai_reaction_memory", "ai_tool_memory"):
            await _del(
                table,
                f"DELETE FROM {table} WHERE user_id=$1 AND guild_id=$2",
                user_id, guild_id,
            )
        # disco_facts is scoped by string. Both `user:<uid>` (global) and
        # `user:<uid>:guild:<gid>` (this-guild) entries are this user's
        # personal facts, so a LIKE on the prefix covers both. Guild-wide
        # facts (`guild:<gid>`) and lore facts (`lore:...`) are deliberately
        # left alone.
        await _del(
            "disco_facts",
            "DELETE FROM disco_facts WHERE scope LIKE $1",
            f"user:{user_id}%",
        )
        return deleted

    async def wipe_ai_guild_state(self, guild_id: int) -> dict[str, int]:
        """Drop EVERY AI memory row tied to this guild.

        This is the nuclear option behind ``,ai recontext server``: wipes
        every user's conversations, memory, traits, reaction counters, and
        tool memory in this guild; clears the recent server-events drama
        feed and channel-context (reactions / edits / deletes / banter)
        feed; and drops both ``user:<uid>:guild:<gid>`` and ``guild:<gid>``
        scoped facts + episodes from DiscoAI's long-term store. Global
        ``user:<uid>`` facts (cross-guild) and ``lore:`` facts are left
        alone -- those aren't this guild's to delete.

        Configuration tables (``ai_custom_prompts``, ``ai_chat_channels``,
        ``ai_model_defaults``, etc.) are intentionally NOT touched -- a
        memory wipe should not silently undo guild settings.
        """
        deleted: dict[str, int] = {}

        async def _del(table: str, sql: str, *args) -> None:
            try:
                status = await self.execute(sql, *args)
                try:
                    deleted[table] = int(status.split()[-1])
                except (AttributeError, ValueError, IndexError):
                    deleted[table] = 0
            except Exception:
                deleted[table] = 0

        # Per-user learning tables -- wipe every user's row in this guild
        # in one shot rather than looping through members.
        await _del(
            "ai_conversations",
            "DELETE FROM ai_conversations WHERE guild_id=$1",
            guild_id,
        )
        await _del(
            "ai_user_memory",
            "DELETE FROM ai_user_memory WHERE guild_id=$1",
            guild_id,
        )
        for table in ("ai_user_traits", "ai_reaction_memory", "ai_tool_memory"):
            await _del(
                table,
                f"DELETE FROM {table} WHERE guild_id=$1",
                guild_id,
            )

        # Drama / banter feeds the AI grazes from on every turn.
        await _del(
            "server_events",
            "DELETE FROM server_events WHERE guild_id=$1",
            guild_id,
        )
        await _del(
            "channel_context",
            "DELETE FROM channel_context WHERE guild_id=$1",
            guild_id,
        )

        # DiscoAI long-term store: drop facts whose scope is THIS guild's.
        # Two scope shapes carry guild-bound data:
        #   * user:<uid>:guild:<gid>   -> per-user facts in this guild
        #   * guild:<gid>              -> guild-wide facts
        # Cross-guild user facts (user:<uid> with no :guild:) are left
        # alone since they belong to the user, not the guild.
        await _del(
            "disco_facts.user_in_guild",
            "DELETE FROM disco_facts WHERE scope LIKE $1",
            f"user:%:guild:{guild_id}",
        )
        await _del(
            "disco_facts.guild",
            "DELETE FROM disco_facts WHERE scope LIKE $1",
            f"guild:{guild_id}%",
        )
        await _del(
            "disco_episodes.user_in_guild",
            "DELETE FROM disco_episodes WHERE scope LIKE $1",
            f"user:%:guild:{guild_id}",
        )
        await _del(
            "disco_episodes.guild",
            "DELETE FROM disco_episodes WHERE scope LIKE $1",
            f"guild:{guild_id}%",
        )
        return deleted

    async def wipe_ai_channel_context(
        self, guild_id: int, channel_id: int,
    ) -> int:
        """Drop the recent reactions / edits / deletes / banter feed for ONE channel.

        Lighter cousin of :meth:`wipe_ai_guild_state` -- when the AI is
        looping on something specific that happened in this room and the
        admin doesn't want to torch the entire server's drama. Returns
        the row count.
        """
        try:
            status = await self.execute(
                "DELETE FROM channel_context WHERE guild_id=$1 AND channel_id=$2",
                guild_id, channel_id,
            )
            try:
                return int(status.split()[-1])
            except (AttributeError, ValueError, IndexError):
                return 0
        except Exception:
            return 0

    async def clear_ai_opt_out(self, user_id: int, guild_id: int) -> None:
        await self.execute(
            "DELETE FROM ai_opt_outs WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )

    async def get_ai_user_memory(self, user_id: int, guild_id: int) -> str | None:
        """Return persistent AI memory for a user, or None."""
        row = await self.fetch_one(
            "SELECT memory FROM ai_user_memory WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        return row["memory"] if row else None

    async def get_ai_memories_for_users(self, guild_id: int, user_ids: list[int]) -> dict[int, str]:
        """Return {user_id: memory} for multiple users at once."""
        if not user_ids:
            return {}
        rows = await self.fetch_all(
            "SELECT user_id, memory FROM ai_user_memory"
            " WHERE guild_id=$1 AND user_id = ANY($2::bigint[])",
            guild_id, user_ids,
        )
        return {r["user_id"]: r["memory"] for r in rows if r.get("memory")}

    async def set_ai_user_memory(self, user_id: int, guild_id: int, memory: str) -> None:
        """Upsert persistent AI memory for a user and record the refresh timestamp."""
        await self.execute(
            """INSERT INTO ai_user_memory (user_id, guild_id, memory, updated_at, last_refreshed_at, refresh_count)
               VALUES ($1, $2, $3, now(), now(), 1)
               ON CONFLICT (user_id, guild_id) DO UPDATE SET
                   memory = $3, updated_at = now(),
                   last_refreshed_at = now(),
                   refresh_count = ai_user_memory.refresh_count + 1""",
            user_id, guild_id, memory,
        )

    async def clear_ai_user_memory(self, user_id: int, guild_id: int) -> int:
        """Wipe the persistent AI memory row for one user/guild pair.

        Used by the self-serve ``.ai forget`` command so users can recover
        from corrupt memory (e.g. a stale '$0 net worth' claim that the AI
        keeps echoing). Returns the number of rows deleted.
        """
        status = await self.execute(
            "DELETE FROM ai_user_memory WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        # asyncpg returns "DELETE <n>" on success; parse the count.
        try:
            return int(str(status).split()[-1])
        except (ValueError, IndexError):
            return 0

    # ── Disco command group ──────────────────────────────────────────────────
    #
    # Per-user reply-mode preference and personally bookmarked Disco answers.
    # Backing tables live in migration 0283_disco_command_group.sql.

    async def get_disco_reply_mode(self, user_id: int, guild_id: int) -> str:
        """Return 'thread' or 'chat' -- how Disco answers this member.

        'thread' is the native behaviour everyone keeps until they explicitly
        switch with ,disco chat.
        """
        row = await self.fetch_one(
            "SELECT reply_mode FROM disco_user_prefs WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        mode = (row or {}).get("reply_mode")
        return "chat" if mode == "chat" else "thread"

    async def set_disco_reply_mode(self, user_id: int, guild_id: int, mode: str) -> None:
        """Persist a member's Disco reply-mode preference ('thread' | 'chat')."""
        mode = "chat" if mode == "chat" else "thread"
        await self.execute(
            """INSERT INTO disco_user_prefs (user_id, guild_id, reply_mode, updated_at)
               VALUES ($1, $2, $3, now())
               ON CONFLICT (user_id, guild_id) DO UPDATE SET
                   reply_mode = $3, updated_at = now()""",
            user_id, guild_id, mode,
        )

    async def add_disco_saved_message(
        self,
        user_id: int,
        guild_id: int,
        channel_id: int,
        disco_message_id: int,
        trigger_message_id: int | None,
        prompt_text: str,
        response_text: str,
        jump_url: str | None,
    ) -> bool:
        """Bookmark a Disco answer for a member. Returns False if already saved."""
        status = await self.execute(
            """INSERT INTO disco_saved_messages
                   (user_id, guild_id, channel_id, disco_message_id,
                    trigger_message_id, prompt_text, response_text, jump_url)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (user_id, guild_id, disco_message_id) DO NOTHING""",
            user_id, guild_id, channel_id, disco_message_id,
            trigger_message_id, prompt_text, response_text, jump_url,
        )
        return isinstance(status, str) and status.endswith(" 1")

    async def list_disco_saved_messages(
        self, user_id: int, guild_id: int,
    ) -> list[dict]:
        """Return a member's saved Disco answers, oldest first (index 0 = oldest)."""
        return await self.fetch_all(
            """SELECT id, channel_id, disco_message_id, trigger_message_id,
                      prompt_text, response_text, jump_url, saved_at
               FROM disco_saved_messages
               WHERE user_id=$1 AND guild_id=$2
               ORDER BY saved_at ASC, id ASC""",
            user_id, guild_id,
        )

    async def is_disco_message_saved(
        self, user_id: int, guild_id: int, disco_message_id: int,
    ) -> bool:
        """True when the member already bookmarked this Disco message."""
        row = await self.fetch_one(
            "SELECT 1 FROM disco_saved_messages "
            "WHERE user_id=$1 AND guild_id=$2 AND disco_message_id=$3",
            user_id, guild_id, disco_message_id,
        )
        return row is not None

    async def delete_disco_saved_message(
        self, user_id: int, guild_id: int, row_id: int,
    ) -> bool:
        """Remove one bookmarked Disco answer by its row id (owner-scoped)."""
        status = await self.execute(
            "DELETE FROM disco_saved_messages "
            "WHERE id=$1 AND user_id=$2 AND guild_id=$3",
            row_id, user_id, guild_id,
        )
        return isinstance(status, str) and status.endswith(" 1")

    async def get_ai_conversation_count(self, user_id: int, guild_id: int) -> int:
        """Return total stored messages for a user's conversation history."""
        val = await self.fetch_val(
            "SELECT COUNT(*) FROM ai_conversations WHERE user_id=$1 AND guild_id=$2",
            user_id, guild_id,
        )
        return int(val or 0)

    async def prune_ai_conversations(
        self, user_id: int, guild_id: int, keep: int = 20, history_key: str = "default"
    ) -> int:
        """Delete old conversation messages beyond the most recent *keep* entries.

        Returns the number of rows deleted.
        """
        result = await self.execute(
            """DELETE FROM ai_conversations
               WHERE user_id=$1 AND guild_id=$2 AND history_key=$3
                 AND id NOT IN (
                     SELECT id FROM ai_conversations
                     WHERE user_id=$1 AND guild_id=$2 AND history_key=$3
                     ORDER BY ts DESC LIMIT $4
                 )""",
            user_id, guild_id, history_key, keep,
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

    # ── Tool memory ───────────────────────────────────────────────────────────

    async def log_ai_tool_use(self, user_id: int, guild_id: int, tool_key: str) -> None:
        """Record or increment a tool activation for a user."""
        await self.execute(
            """INSERT INTO ai_tool_memory (user_id, guild_id, tool_key, use_count, last_used)
               VALUES ($1, $2, $3, 1, now())
               ON CONFLICT (user_id, guild_id, tool_key) DO UPDATE SET
                   use_count = ai_tool_memory.use_count + 1,
                   last_used = now()""",
            user_id, guild_id, tool_key,
        )

    async def get_ai_tool_memory(self, user_id: int, guild_id: int, limit: int = 5) -> list[dict]:
        """Return the top tools (by use count) for a user."""
        return await self.fetch_all(
            "SELECT tool_key, use_count, last_used FROM ai_tool_memory "
            "WHERE user_id=$1 AND guild_id=$2 ORDER BY use_count DESC LIMIT $3",
            user_id, guild_id, limit,
        )

    # ── Reaction memory ───────────────────────────────────────────────────────

    async def log_ai_reaction_memory(self, user_id: int, guild_id: int, category: str) -> None:
        """Record or increment an emoji reaction category for a user."""
        await self.execute(
            """INSERT INTO ai_reaction_memory (user_id, guild_id, category, use_count, last_used)
               VALUES ($1, $2, $3, 1, now())
               ON CONFLICT (user_id, guild_id, category) DO UPDATE SET
                   use_count = ai_reaction_memory.use_count + 1,
                   last_used = now()""",
            user_id, guild_id, category,
        )

    async def get_ai_reaction_memory(self, user_id: int, guild_id: int, limit: int = 5) -> list[dict]:
        """Return the top emoji categories (by use count) for a user."""
        return await self.fetch_all(
            "SELECT category, use_count FROM ai_reaction_memory "
            "WHERE user_id=$1 AND guild_id=$2 ORDER BY use_count DESC LIMIT $3",
            user_id, guild_id, limit,
        )

    # ── Trait engine (ai_user_traits) ─────────────────────────────────────────

    async def upsert_ai_trait(
        self,
        user_id: int,
        guild_id: int,
        trait_key: str,
        signal_weight: float,
        lambda_val: float = 0.00005,
        k_val: float = 10.0,
        stable_sample: int = 20,
        stable_conf: float = 0.7,
        *,
        source: str = "event",
        confidence_seed: float | None = None,
    ) -> None:
        """Insert or update a trait with DB-side time-decay and confidence scoring.

        On insert: weight = signal_weight, confidence = ``confidence_seed`` if
                   provided else 1 - exp(-1/k_val), layer = 'volatile'.
        On update: weight decays by exp(-lambda * age_seconds) then adds signal_weight;
                   confidence increments; layer is 'stable' once thresholds are met, else
                   'volatile' (since the trait was just observed).

        ``source`` is recorded in the row so passive-chat extracted traits
        can be queried apart from tone / reaction / behavior signals.
        ``confidence_seed`` lets passive-chat traits land at a lower seed
        (e.g. 0.3) than directly-observed signals, so a one-shot
        misextraction decays out quickly while a repeated signal still
        promotes to stable through the existing math.

        The 'interaction' layer is a read-time concept only: get_ai_traits computes it for
        traits that were once volatile but haven't been observed in the last hour.
        """
        # COALESCE the seed against the natural formula so callers who
        # pass None get the original behavior; an explicit seed (capped
        # in [0, 1]) overrides only the insert path. Updates always use
        # the canonical formula based on sample_size.
        if confidence_seed is None:
            seed_val: float | None = None
        else:
            seed_val = max(0.0, min(1.0, float(confidence_seed)))
        await self.execute(
            """
            INSERT INTO ai_user_traits
                (user_id, guild_id, trait_key, weight, confidence, sample_size, layer, source)
            VALUES ($1, $2, $3, $4,
                    COALESCE($9::float, 1.0 - exp(-1.0 / $6::float)),
                    1, 'volatile', $10)
            ON CONFLICT (user_id, guild_id, trait_key) DO UPDATE SET
                weight = ai_user_traits.weight
                         * exp(-$5::float
                               * EXTRACT(EPOCH FROM (NOW() - ai_user_traits.last_observed_at)))
                         + $4::float,
                sample_size = ai_user_traits.sample_size + 1,
                confidence  = 1.0 - exp(
                                  -(ai_user_traits.sample_size + 1)::float / $6::float
                              ),
                layer = CASE
                    WHEN ai_user_traits.sample_size + 1 >= $7
                         AND (1.0 - exp(-(ai_user_traits.sample_size + 1)::float / $6::float))
                             >= $8::float
                      THEN 'stable'
                    ELSE 'volatile'
                END,
                last_observed_at = NOW()
            """,
            user_id, guild_id, trait_key, signal_weight,
            lambda_val, k_val, stable_sample, stable_conf,
            seed_val, source,
        )

    async def dampen_ai_trait(
        self, user_id: int, guild_id: int, trait_key: str, factor: float = 0.8
    ) -> None:
        """Multiply a trait's weight by factor (used for contradiction dampening)."""
        await self.execute(
            "UPDATE ai_user_traits SET weight = weight * $4 "
            "WHERE user_id=$1 AND guild_id=$2 AND trait_key=$3",
            user_id, guild_id, trait_key, factor,
        )

    async def get_ai_traits(
        self,
        user_id: int,
        guild_id: int,
        layer: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 20,
    ) -> list[dict]:
        """Return traits for a user, optionally filtered by effective layer.

        The stored layer column is 'stable' or 'volatile' (set at upsert time).
        The effective layer is computed at read time:
          stable      - sample_size >= 20 AND confidence >= 0.7
          volatile    - last_observed_at within the last hour
          interaction - was observed before but not recently and not yet stable
        Filtering by layer= uses the computed effective layer.
        """
        _LAYER_EXPR = (
            "CASE "
            "  WHEN sample_size >= 20 AND confidence >= 0.7 THEN 'stable' "
            "  WHEN last_observed_at >= NOW() - INTERVAL '1 hour' THEN 'volatile' "
            "  ELSE 'interaction' "
            "END"
        )
        if layer:
            return await self.fetch_all(
                f"SELECT trait_key, trait_value, ({_LAYER_EXPR}) AS layer, "
                "       confidence, weight, sample_size "
                "FROM ai_user_traits "
                f"WHERE user_id=$1 AND guild_id=$2 AND ({_LAYER_EXPR}) = $3 "
                "  AND confidence >= $4 "
                "ORDER BY weight DESC LIMIT $5",
                user_id, guild_id, layer, min_confidence, limit,
            )
        return await self.fetch_all(
            f"SELECT trait_key, trait_value, ({_LAYER_EXPR}) AS layer, "
            "       confidence, weight, sample_size "
            "FROM ai_user_traits "
            "WHERE user_id=$1 AND guild_id=$2 AND confidence >= $3 "
            "ORDER BY weight DESC LIMIT $4",
            user_id, guild_id, min_confidence, limit,
        )

    async def prune_ai_traits(
        self,
        user_id: int,
        guild_id: int,
        min_confidence: float = 0.15,
        min_weight: float = 0.05,
    ) -> int:
        """Delete traits below the given confidence or weight thresholds.

        Returns the number of rows deleted.
        """
        result = await self.execute(
            "DELETE FROM ai_user_traits "
            "WHERE user_id=$1 AND guild_id=$2 AND (confidence < $3 OR weight < $4)",
            user_id, guild_id, min_confidence, min_weight,
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

    async def cap_ai_traits_layer(
        self, user_id: int, guild_id: int, layer: str, cap: int
    ) -> int:
        """Keep only the top *cap* traits by weight for the given layer.

        Returns the number of rows deleted.
        """
        result = await self.execute(
            """DELETE FROM ai_user_traits
               WHERE user_id=$1 AND guild_id=$2 AND layer=$3
                 AND trait_key NOT IN (
                     SELECT trait_key FROM ai_user_traits
                     WHERE user_id=$1 AND guild_id=$2 AND layer=$3
                     ORDER BY weight DESC LIMIT $4
                 )""",
            user_id, guild_id, layer, cap,
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

    # ── Raw event log (ai_user_events) ────────────────────────────────────────

    async def log_ai_event(
        self, user_id: int, guild_id: int, event_type: str, event_subtype: str
    ) -> None:
        """Append a raw signal event to the event log."""
        await self.execute(
            "INSERT INTO ai_user_events (user_id, guild_id, event_type, event_subtype) "
            "VALUES ($1, $2, $3, $4)",
            user_id, guild_id, event_type, event_subtype,
        )

    async def get_ai_event_distribution(
        self, user_id: int, guild_id: int, window_secs: int = 3600
    ) -> dict[str, int]:
        """Return event_subtype -> count for events within the last window_secs.

        Uses DB-side clock to avoid container/DB skew.
        """
        rows = await self.fetch_all(
            "SELECT event_subtype, COUNT(*) AS cnt FROM ai_user_events "
            "WHERE user_id=$1 AND guild_id=$2 "
            "  AND created_at >= NOW() - make_interval(secs => $3) "
            "GROUP BY event_subtype",
            user_id, guild_id, window_secs,
        )
        return {r["event_subtype"]: int(r["cnt"]) for r in rows}

    async def get_ai_baseline_event_distribution(
        self, user_id: int, guild_id: int
    ) -> dict[str, int]:
        """Return rolling-baseline event_subtype -> count for a user.

        Because ai_user_events is pruned to ~200 rows per user, this reflects
        the most recent ~200 signals, not a true all-time history.
        """
        rows = await self.fetch_all(
            "SELECT event_subtype, COUNT(*) AS cnt FROM ai_user_events "
            "WHERE user_id=$1 AND guild_id=$2 GROUP BY event_subtype",
            user_id, guild_id,
        )
        return {r["event_subtype"]: int(r["cnt"]) for r in rows}

    async def prune_ai_events(
        self, user_id: int, guild_id: int, keep: int = 200
    ) -> int:
        """Keep only the most recent *keep* events per user. Returns rows deleted."""
        result = await self.execute(
            """DELETE FROM ai_user_events
               WHERE user_id=$1 AND guild_id=$2
                 AND id NOT IN (
                     SELECT id FROM ai_user_events
                     WHERE user_id=$1 AND guild_id=$2
                     ORDER BY created_at DESC LIMIT $3
                 )""",
            user_id, guild_id, keep,
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

    async def get_users_needing_memory_refresh(
        self, guild_id: int, stale_hours: int = 4, limit: int = 50
    ) -> list[dict]:
        """Return users whose memory is stale (older than stale_hours) or never refreshed,
        and who have at least one conversation message."""
        return await self.fetch_all(
            """SELECT DISTINCT c.user_id
               FROM ai_conversations c
               LEFT JOIN ai_user_memory m ON m.user_id = c.user_id AND m.guild_id = c.guild_id
               WHERE c.guild_id = $1
                 AND (
                     m.last_refreshed_at IS NULL
                     OR m.last_refreshed_at < now() - make_interval(hours => $2)
                 )
               LIMIT $3""",
            guild_id, stale_hours, limit,
        )

    # ── Active players ────────────────────────────────────────────────────────

    async def get_active_players(
        self, guild_id: int, days: int = 90, limit: int = 50,
    ) -> list[dict]:
        """Return users active within the last *days* days (by last_activity)."""
        return await self.fetch_all(
            "SELECT user_id FROM users "
            "WHERE guild_id = $1 AND last_activity > now() - make_interval(days => $2) "
            "ORDER BY last_activity DESC LIMIT $3",
            guild_id, days, limit,
        )

    # ── Server events (notable moments) ───────────────────────────────────────

    async def log_server_event(
        self,
        guild_id: int,
        channel_id: int | None,
        user_id: int,
        event_type: str,
        summary: str,
        amount: float = 0,
        metadata: dict | None = None,
    ) -> None:
        """Record a notable server event (catastrophe, jackpot, rugpull, etc.)."""
        import json as _json
        await self.execute(
            "INSERT INTO server_events (guild_id, channel_id, user_id, event_type, summary, amount, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            guild_id, channel_id, user_id, event_type, summary, amount,
            _json.dumps(metadata or {}),
        )

    async def get_recent_server_events(
        self, guild_id: int, limit: int = 20,
    ) -> list[dict]:
        """Fetch the most recent notable events for a guild."""
        try:
            return await self.fetch_all(
                "SELECT user_id, event_type, summary, amount, ts "
                "FROM server_events WHERE guild_id = $1 ORDER BY ts DESC LIMIT $2",
                guild_id, limit,
            )
        except Exception:
            return []

    async def get_user_server_events(
        self, user_id: int, guild_id: int, limit: int = 10,
    ) -> list[dict]:
        """Fetch recent notable events for a specific user."""
        return await self.fetch_all(
            "SELECT event_type, summary, amount, ts "
            "FROM server_events WHERE user_id = $1 AND guild_id = $2 ORDER BY ts DESC LIMIT $3",
            user_id, guild_id, limit,
        )

    # ── Channel context (social interactions) ─────────────────────────────────

    async def log_channel_context(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        event_type: str,
        content: str = "",
        target_user_id: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Record a social interaction (reaction, edit, delete, reply, etc.)."""
        import json as _json
        await self.execute(
            "INSERT INTO channel_context "
            "(guild_id, channel_id, user_id, event_type, content, target_user_id, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            guild_id, channel_id, user_id, event_type, content, target_user_id,
            _json.dumps(metadata or {}),
        )

    async def get_recent_channel_context(
        self, guild_id: int, channel_id: int, limit: int = 20,
    ) -> list[dict]:
        """Fetch recent social interactions for a channel."""
        return await self.fetch_all(
            "SELECT user_id, event_type, content, target_user_id, ts "
            "FROM channel_context "
            "WHERE guild_id = $1 AND channel_id = $2 ORDER BY ts DESC LIMIT $3",
            guild_id, channel_id, limit,
        )

    async def prune_old_channel_context(self, days: int = 30) -> int:
        """Delete channel context entries older than *days* days. Returns rows deleted."""
        row = await self.fetch_one(
            "WITH deleted AS ("
            "  DELETE FROM channel_context WHERE ts < now() - make_interval(days => $1) RETURNING 1"
            ") SELECT count(*) AS cnt FROM deleted",
            days,
        )
        return int(row["cnt"]) if row else 0
