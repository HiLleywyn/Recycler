"""Pools repository  -  AMM liquidity pools and LP positions (PostgreSQL)."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from core.config import Config
from .base import PgBaseRepo


class PgPoolsRepo(PgBaseRepo):

    @staticmethod
    def make_pool_id(token_a: str, token_b: str) -> tuple[str, str, str]:
        """Returns (pool_id, canonical_a, canonical_b) with tokens in alphabetical order."""
        a, b = sorted([token_a.upper(), token_b.upper()])
        return f"{a}-{b}", a, b

    async def get_pool(self, pool_id: str, guild_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM pools WHERE pool_id=$1 AND guild_id=$2",
            pool_id, guild_id,
        )

    async def get_all_pools(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM pools WHERE guild_id=$1 ORDER BY pool_id",
            guild_id,
        )

    async def create_pool(
        self, pool_id: str, guild_id: int, token_a: str, token_b: str,
        reserve_a: float, reserve_b: float
    ) -> dict:
        lp = math.sqrt(reserve_a * reserve_b)
        await self.execute(
            """INSERT INTO pools
               (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT DO NOTHING""",
            pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, lp,
        )
        return await self.get_pool(pool_id, guild_id)

    async def create_group_pool(
        self, pool_id: str, guild_id: int, token_a: str, token_b: str,
    ) -> dict:
        """Create an empty group partnership pool (no initial liquidity).
        vault_locked is left FALSE so founders can add LP normally."""
        await self.execute(
            """INSERT INTO pools
               (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp, is_group_pool)
               VALUES ($1, $2, $3, $4, 0, 0, 0, TRUE)
               ON CONFLICT DO NOTHING""",
            pool_id, guild_id, token_a, token_b,
        )
        return await self.get_pool(pool_id, guild_id)

    async def seed_pools(self, guild_id: int) -> None:
        """Seed TOKEN/stablecoin and COIN/TOKEN AMM pools.
        Each token on a network gets:
          1. TOKEN/STABLECOIN pool  (e.g. LINK/USDC)
          2. COIN/TOKEN pool        (e.g. ARC/LINK)  -  for direct intra-network trades
        Stablecoins get TOKEN/USD pools for .buy/.sell.
        Uses INSERT ... ON CONFLICT DO NOTHING  -  safe to call on every startup."""
        seed_usd = Config.POOL_SEED_STABLECOIN  # default $10,000

        async with self.transaction() as conn:
            for symbol, data in Config.TOKENS.items():
                # Earn-only tokens (MOON, LURE, REEL, ...) deliberately get
                # no auto-seeded AMM pools. They are acquired exclusively
                # through their native earn mechanism and have one-way exits
                # handled by their owning service (cogs/moons.py for MOON,
                # services/fishing.py for LURE/REEL). Auto-seeding a USD or
                # stablecoin pool would let users dodge the earn loop.
                if symbol in Config.EARN_ONLY_TOKENS:
                    continue
                if data.get("stablecoin"):
                    # Stablecoin/USD pool for .buy/.sell
                    price = data["start_price"]
                    pool_id, a, b = self.make_pool_id(symbol, "USD")
                    reserve_a = seed_usd / price if a == symbol else seed_usd
                    reserve_b = seed_usd if a == symbol else seed_usd / price
                    lp = math.sqrt(reserve_a * reserve_b)
                    await conn.execute(
                        """INSERT INTO pools
                           (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
                           VALUES ($1, $2, $3, $4, $5, $6, $7)
                           ON CONFLICT DO NOTHING""",
                        pool_id, guild_id, a, b, reserve_a, reserve_b, lp,
                    )
                    continue

                network = data.get("network", "")
                stablecoin = Config.NETWORK_STABLECOIN.get(network)
                coin = Config.NETWORK_COINS.get(network)  # e.g. ARC, DSC, SUN, MTA
                price = data["start_price"]

                if stablecoin:
                    # TOKEN/STABLECOIN (e.g. LINK/USDC)
                    pool_id, a, b = self.make_pool_id(symbol, stablecoin)
                    reserve_a = seed_usd / price if a == symbol else seed_usd
                    reserve_b = seed_usd if a == symbol else seed_usd / price
                    lp = math.sqrt(reserve_a * reserve_b)
                    await conn.execute(
                        """INSERT INTO pools
                           (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
                           VALUES ($1, $2, $3, $4, $5, $6, $7)
                           ON CONFLICT DO NOTHING""",
                        pool_id, guild_id, a, b, reserve_a, reserve_b, lp,
                    )

                if coin and coin != symbol:
                    # COIN/TOKEN within network (e.g. ARC/LINK)  -  enables direct coin->token swaps
                    coin_price = Config.TOKENS.get(coin, {}).get("start_price", 1.0)
                    pool_id, a, b = self.make_pool_id(coin, symbol)
                    # Both sides seeded at equal USD value
                    coin_reserve = seed_usd / coin_price
                    token_reserve = seed_usd / price
                    reserve_a = coin_reserve if a == coin else token_reserve
                    reserve_b = token_reserve if a == coin else coin_reserve
                    lp = math.sqrt(reserve_a * reserve_b)
                    await conn.execute(
                        """INSERT INTO pools
                           (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
                           VALUES ($1, $2, $3, $4, $5, $6, $7)
                           ON CONFLICT DO NOTHING""",
                        pool_id, guild_id, a, b, reserve_a, reserve_b, lp,
                    )

                if not stablecoin and (not coin or coin == symbol):
                    # Fallback: TOKEN/USD. Covers PoW network coins (SUN, MTA)
                    # which ARE their network's coin and have no stablecoin,
                    # plus any token on a network without both.
                    pool_id, a, b = self.make_pool_id(symbol, "USD")
                    reserve_a = seed_usd / price if a == symbol else seed_usd
                    reserve_b = seed_usd if a == symbol else seed_usd / price
                    lp = math.sqrt(reserve_a * reserve_b)
                    await conn.execute(
                        """INSERT INTO pools
                           (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
                           VALUES ($1, $2, $3, $4, $5, $6, $7)
                           ON CONFLICT DO NOTHING""",
                        pool_id, guild_id, a, b, reserve_a, reserve_b, lp,
                    )

            # ── Intra-network TOKEN/TOKEN pools ───────────────────────────────────
            # Group tradeable (non-stablecoin) tokens by network, then seed
            # all pairwise combinations so users can swap directly within a network.
            by_network: dict[str, list[tuple[str, float]]] = {}
            for sym, data in Config.TOKENS.items():
                if data.get("stablecoin"):
                    continue
                net = data.get("network", "")
                if net:
                    by_network.setdefault(net, []).append((sym, data["start_price"]))

            token_token_seed = seed_usd * 0.4  # 40% of base seed per side for token/token pairs
            for net, tokens in by_network.items():
                for i in range(len(tokens)):
                    sym_a, price_a = tokens[i]
                    for j in range(i + 1, len(tokens)):
                        sym_b, price_b = tokens[j]
                        pool_id, ca, cb = self.make_pool_id(sym_a, sym_b)
                        res_a = token_token_seed / price_a
                        res_b = token_token_seed / price_b
                        reserve_a = res_a if ca == sym_a else res_b
                        reserve_b = res_b if ca == sym_a else res_a
                        lp = math.sqrt(reserve_a * reserve_b)
                        await conn.execute(
                            """INSERT INTO pools
                               (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
                               VALUES ($1, $2, $3, $4, $5, $6, $7)
                               ON CONFLICT DO NOTHING""",
                            pool_id, guild_id, ca, cb, reserve_a, reserve_b, lp,
                        )

            # ── Backfill genesis pools for EXISTING group tokens ──────────
            # New groups get their TOKEN/DSD + TOKEN/MOON pools seeded at
            # creation time (cogs/groups.py::_seed_group_token_genesis_pools).
            # Groups that predate that feature were left with no tradeable
            # pool at all. Call the same helper for every guild_tokens row
            # so every existing group token becomes tradeable on the next
            # bot startup; ON CONFLICT DO NOTHING makes it safe to re-run.
            existing_group_tokens = await conn.fetch(
                "SELECT symbol FROM guild_tokens WHERE guild_id=$1 AND token_type='group'",
                guild_id,
            )
            for _gt in existing_group_tokens:
                try:
                    await self.seed_group_genesis_pools(guild_id, _gt["symbol"])
                except Exception:
                    # Logged by the helper; backfill is best-effort.
                    pass

            # ── Bidirectional MOON pairs (Config.MOON_SWAPPABLE_TOKENS) ───
            # mMTA and mSUN are the two carve-out built-in tokens that may
            # swap into AND out of MOON (services/swap.py::is_moon_swappable_pair).
            # Seed their MOON pools at startup so players have a venue the
            # moment the bot boots; ON CONFLICT DO NOTHING keeps it safe.
            moon_meta = Config.TOKENS.get("MOON", {})
            moon_price = float(moon_meta.get("start_price") or 0.0)
            for _wsym in Config.MOON_SWAPPABLE_TOKENS:
                _wmeta = Config.TOKENS.get(_wsym, {})
                _wprice = float(_wmeta.get("start_price") or 0.0)
                if _wprice <= 0 or moon_price <= 0:
                    continue
                pool_id, ca, cb = self.make_pool_id(_wsym, "MOON")
                _w_reserve = seed_usd / _wprice
                _moon_reserve = seed_usd / moon_price
                reserve_a = _w_reserve if ca == _wsym else _moon_reserve
                reserve_b = _moon_reserve if ca == _wsym else _w_reserve
                lp = math.sqrt(reserve_a * reserve_b)
                await conn.execute(
                    """INSERT INTO pools
                       (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT DO NOTHING""",
                    pool_id, guild_id, ca, cb, reserve_a, reserve_b, lp,
                )

            # ── Bidirectional HRV pairs (Config.HRV_SWAPPABLE_TOKENS) ────
            # REEL, RUNE, and BUD are the carve-out built-in tokens that may
            # swap into AND out of HRV (services/farming.py owns the HRV->USD
            # one-way burn cashout; no HRV/USD AMM pool is seeded here).
            # ON CONFLICT DO NOTHING keeps re-runs safe.
            hrv_meta = Config.TOKENS.get("HRV", {})
            hrv_price = float(hrv_meta.get("start_price") or 0.0)
            for _hsym in Config.HRV_SWAPPABLE_TOKENS:
                _hmeta = Config.TOKENS.get(_hsym, {})
                _hprice = float(_hmeta.get("start_price") or 0.0)
                if _hprice <= 0 or hrv_price <= 0:
                    continue
                pool_id, ca, cb = self.make_pool_id(_hsym, "HRV")
                _h_reserve = seed_usd / _hprice
                _hrv_reserve = seed_usd / hrv_price
                reserve_a = _h_reserve if ca == _hsym else _hrv_reserve
                reserve_b = _hrv_reserve if ca == _hsym else _h_reserve
                lp = math.sqrt(reserve_a * reserve_b)
                await conn.execute(
                    """INSERT INTO pools
                       (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT DO NOTHING""",
                    pool_id, guild_id, ca, cb, reserve_a, reserve_b, lp,
                )

            # ── Bidirectional FORGE pairs (Config.FORGE_SWAPPABLE_TOKENS) ──
            # REEL, RUNE, BUD, HRV, and INGOT are the carve-out built-in tokens
            # that may swap into AND out of FORGE (services/crafting.py owns
            # the FORGE->USD one-way burn cashout; no FORGE/USD AMM pool is
            # seeded here -- FGD/USD is the stable on-ramp for the network).
            # ON CONFLICT DO NOTHING keeps re-runs safe.
            forge_meta = Config.TOKENS.get("FORGE", {})
            forge_price = float(forge_meta.get("start_price") or 0.0)
            for _fsym in Config.FORGE_SWAPPABLE_TOKENS:
                _fmeta = Config.TOKENS.get(_fsym, {})
                _fprice = float(_fmeta.get("start_price") or 0.0)
                if _fprice <= 0 or forge_price <= 0:
                    continue
                pool_id, ca, cb = self.make_pool_id(_fsym, "FORGE")
                _f_reserve = seed_usd / _fprice
                _forge_reserve = seed_usd / forge_price
                reserve_a = _f_reserve if ca == _fsym else _forge_reserve
                reserve_b = _forge_reserve if ca == _fsym else _f_reserve
                lp = math.sqrt(reserve_a * reserve_b)
                await conn.execute(
                    """INSERT INTO pools
                       (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT DO NOTHING""",
                    pool_id, guild_id, ca, cb, reserve_a, reserve_b, lp,
                )

            # ── Migration: opt EVERY existing player-deployed token into a
            #              bidirectional TOKEN/MOON pool ──────────────────────
            # ERC-20 tokens deployed before the moon-swappable carve-out
            # shipped never had params["moon_swappable"] set. Back-fill
            # the flag in place on every contract whose type is ERC-20 so
            # an old deploy gets the same TOKEN/MOON pool a fresh deploy
            # would have. NFT collections (type=ERC-721, stored under
            # symbol "NFT:..."), admin-installed tokens (no deployer),
            # and contracts that already have the flag are left alone --
            # the UPDATE is idempotent and safe to re-run every boot.
            await conn.execute(
                """UPDATE token_contracts
                      SET params = params || '{"moon_swappable": true}'::jsonb
                    WHERE guild_id = $1
                      AND params->>'type' = 'ERC-20'
                      AND params ? 'deployer'
                      AND COALESCE((params->>'moon_swappable')::bool, FALSE) = FALSE""",
                guild_id,
            )

            # ── Seed / re-seed TOKEN/MOON pools for moon-swappable tokens ───
            # Includes the contracts we just flagged in the migration above
            # plus any new deploys done after this feature shipped. The
            # helper bails out cleanly on a pool that already has LP, so
            # founder-provided liquidity is never overwritten.
            moon_swappable_deployed = await conn.fetch(
                """SELECT symbol FROM token_contracts
                   WHERE guild_id=$1 AND (params->>'moon_swappable')::bool = TRUE""",
                guild_id,
            )
            for _row in moon_swappable_deployed:
                try:
                    await self.seed_moon_swap_pool(guild_id, _row["symbol"])
                except Exception:
                    pass

    async def seed_group_genesis_pools(self, guild_id: int, token_sym: str) -> None:
        """Seed trading pools for a user-created group token.

        Every group token gets three system-minted pools at creation (and
        as a backfill on boot for existing groups). The pair set mirrors
        the wrapped-token DeFi pattern so trading routes through real
        Moon Network assets instead of a DSD shortcut:

          * TOKEN / mMTA   -- wrapped Moneta, the main MTA-chain entry
          * TOKEN / mSUN   -- wrapped Sun,     the main SUN-chain entry
          * TOKEN / MOON   -- Lunar Mint off-ramp (one-way: MOON -> TOKEN,
                              because EARN_ONLY_TOKENS blocks TOKEN -> MOON)

        Reserves on both sides come from thin air (no circulating_supply
        effect anywhere) and each pool is sized at
        Config.GROUP_TOKEN_GENESIS_SEED_USD per side. ON CONFLICT DO NOTHING
        protects founder-provided LP so reseeding is safe to re-run.
        """
        from core.framework.scale import SCALE as _SCALE
        seed_usd = float(Config.GROUP_TOKEN_GENESIS_SEED_USD)
        if seed_usd <= 0:
            return
        seed_raw = int(seed_usd * _SCALE)

        tok_price_row = await self.fetch_one(
            "SELECT price FROM crypto_prices WHERE symbol=$1 AND guild_id=$2",
            token_sym, guild_id,
        )
        tok_price = float(tok_price_row["price"]) if tok_price_row else 0.01

        # Wrapped coins first so the entire pair set swings through
        # Moon-Network-native tokens; MOON last so its one-way nature is
        # clear in the reading order.
        for pair_sym in ("mMTA", "mSUN", "MOON"):
            pair_price_row = await self.fetch_one(
                "SELECT price FROM crypto_prices WHERE symbol=$1 AND guild_id=$2",
                pair_sym, guild_id,
            )
            pair_price = float(pair_price_row["price"]) if pair_price_row else 0.0
            if pair_price <= 0 or tok_price <= 0:
                continue

            pool_id, ca, cb = self.make_pool_id(token_sym, pair_sym)
            existing = await self.get_pool(pool_id, guild_id)
            if existing and int(existing.get("total_lp") or 0) > 0:
                continue

            tok_reserve_raw  = int(seed_raw / tok_price)
            pair_reserve_raw = int(seed_raw / pair_price)
            reserve_a = tok_reserve_raw  if ca == token_sym else pair_reserve_raw
            reserve_b = pair_reserve_raw if ca == token_sym else tok_reserve_raw
            lp = int(math.sqrt(reserve_a * reserve_b))

            await self.execute(
                """INSERT INTO pools
                   (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT DO NOTHING""",
                pool_id, guild_id, ca, cb, reserve_a, reserve_b, lp,
            )

    async def seed_moon_swap_pool(self, guild_id: int, token_sym: str) -> None:
        """Seed a TOKEN/MOON pool for a player-deployed moon-swappable token.

        Called from ``cogs/nfts.py::token_deploy`` whenever a deployer asks
        for a MOON pair alongside their stablecoin pool, and from
        ``seed_pools`` on every bot startup so old deploys that predate
        this feature still get their pool. Sized like a regular
        stablecoin pool (POOL_SEED_STABLECOIN, raw-scaled by 10^18) so the
        depth and slippage curve match every other auto-seeded pair.
        ON CONFLICT DO NOTHING protects founder-provided LP, making the
        helper safe to re-run.
        """
        seed_usd = Config.POOL_SEED_STABLECOIN  # already raw-scaled
        if seed_usd <= 0:
            return
        moon_price_row = await self.fetch_one(
            "SELECT price FROM crypto_prices WHERE symbol=$1 AND guild_id=$2",
            "MOON", guild_id,
        )
        moon_price = float(moon_price_row["price"]) if moon_price_row else 0.0
        tok_price_row = await self.fetch_one(
            "SELECT price FROM crypto_prices WHERE symbol=$1 AND guild_id=$2",
            token_sym, guild_id,
        )
        tok_price = float(tok_price_row["price"]) if tok_price_row else 0.0
        if moon_price <= 0 or tok_price <= 0:
            return

        pool_id, ca, cb = self.make_pool_id(token_sym, "MOON")
        existing = await self.get_pool(pool_id, guild_id)
        if existing and float(existing.get("total_lp") or 0) > 0:
            return

        tok_reserve = seed_usd / tok_price
        moon_reserve = seed_usd / moon_price
        reserve_a = tok_reserve if ca == token_sym else moon_reserve
        reserve_b = moon_reserve if ca == token_sym else tok_reserve
        lp = math.sqrt(reserve_a * reserve_b)
        await self.execute(
            """INSERT INTO pools
               (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT DO NOTHING""",
            pool_id, guild_id, ca, cb, reserve_a, reserve_b, lp,
        )

    async def update_pool_reserves(
        self, pool_id: str, guild_id: int,
        new_a: float, new_b: float, new_lp: float
    ) -> None:
        await self.execute(
            "UPDATE pools SET reserve_a=$1, reserve_b=$2, total_lp=$3 WHERE pool_id=$4 AND guild_id=$5",
            new_a, new_b, new_lp, pool_id, guild_id,
        )

    async def get_user_lp(self, user_id: int, guild_id: int, pool_id: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM lp_positions WHERE user_id=$1 AND guild_id=$2 AND pool_id=$3",
            user_id, guild_id, pool_id,
        )

    async def set_lp_lock(
        self, user_id: int, guild_id: int, pool_id: str,
        tier: int, locked_until,
    ) -> None:
        """Activate (or extend) an LP time-lock on one position.

        ``tier`` must be 1..3 and ``locked_until`` is a timezone-aware
        datetime (enforced by the CHECK constraint lp_positions_lock_tier_pair_chk).
        """
        await self.execute(
            "UPDATE lp_positions SET lock_tier=$1, locked_until=$2 "
            "WHERE user_id=$3 AND guild_id=$4 AND pool_id=$5",
            int(tier), locked_until, user_id, guild_id, pool_id,
        )

    async def clear_lp_lock(self, user_id: int, guild_id: int, pool_id: str) -> None:
        """Reset a position's lock to tier 0 (no active lock)."""
        await self.execute(
            "UPDATE lp_positions SET lock_tier=0, locked_until=NULL "
            "WHERE user_id=$1 AND guild_id=$2 AND pool_id=$3",
            user_id, guild_id, pool_id,
        )

    async def update_lp_position(
        self, user_id: int, guild_id: int, pool_id: str, delta: float
    ) -> float:
        now = datetime.now(timezone.utc)
        await self.execute(
            """INSERT INTO lp_positions (user_id, guild_id, pool_id, added_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT DO NOTHING""",
            user_id, guild_id, pool_id, now,
        )
        # When adding LP (delta > 0), refresh the added_at timestamp
        if delta > 0:
            row = await self.fetch_one(
                "UPDATE lp_positions SET lp_shares = lp_shares + $1, added_at = $2 "
                "WHERE user_id=$3 AND guild_id=$4 AND pool_id=$5 AND lp_shares + $6 >= 0 "
                "RETURNING lp_shares",
                delta, now, user_id, guild_id, pool_id, delta,
            )
        else:
            row = await self.fetch_one(
                "UPDATE lp_positions SET lp_shares = lp_shares + $1 "
                "WHERE user_id=$2 AND guild_id=$3 AND pool_id=$4 AND lp_shares + $5 >= 0 "
                "RETURNING lp_shares",
                delta, user_id, guild_id, pool_id, delta,
            )
        if row is None:
            raise ValueError(f"Insufficient LP shares (need {-delta:.4f})")
        return row.h("lp_shares")

    async def get_user_lp_positions(self, user_id: int, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            """SELECT lp.*, p.token_a, p.token_b, p.reserve_a, p.reserve_b, p.total_lp,
                      p.is_group_pool, p.vault_locked
               FROM lp_positions lp
               JOIN pools p ON lp.pool_id=p.pool_id AND lp.guild_id=p.guild_id
               WHERE lp.user_id=$1 AND lp.guild_id=$2 AND lp.lp_shares > 0""",
            user_id, guild_id,
        )

    async def get_all_guild_lp_positions(self, guild_id: int) -> list[dict]:
        """All LP positions across all users in a guild  -  used for bulk net worth computation."""
        return await self.fetch_all(
            """SELECT lp.user_id, lp.lp_shares, p.token_a, p.token_b, p.reserve_a, p.reserve_b, p.total_lp
               FROM lp_positions lp
               JOIN pools p ON lp.pool_id=p.pool_id AND lp.guild_id=p.guild_id
               WHERE lp.guild_id=$1 AND lp.lp_shares > 0""",
            guild_id,
        )

    async def upsert_lp_snapshot(
        self, user_id: int, guild_id: int, pool_id: str,
        res_a_per_lp: float, res_b_per_lp: float,
    ) -> None:
        await self.execute(
            """INSERT INTO lp_snapshots (user_id, guild_id, pool_id, entry_res_a_per_lp, entry_res_b_per_lp)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT(user_id, guild_id, pool_id) DO UPDATE SET
                   entry_res_a_per_lp = excluded.entry_res_a_per_lp,
                   entry_res_b_per_lp = excluded.entry_res_b_per_lp""",
            user_id, guild_id, pool_id, res_a_per_lp, res_b_per_lp,
        )

    async def get_lp_snapshot(self, user_id: int, guild_id: int, pool_id: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM lp_snapshots WHERE user_id=$1 AND guild_id=$2 AND pool_id=$3",
            user_id, guild_id, pool_id,
        )

    async def delete_lp_snapshot(self, user_id: int, guild_id: int, pool_id: str) -> None:
        await self.execute(
            "DELETE FROM lp_snapshots WHERE user_id=$1 AND guild_id=$2 AND pool_id=$3",
            user_id, guild_id, pool_id,
        )

    async def get_pool_lp_positions(self, pool_id: str, guild_id: int) -> list[dict]:
        """All LP positions for a given pool (used for snapshot refresh after arb)."""
        return await self.fetch_all(
            "SELECT * FROM lp_positions WHERE pool_id=$1 AND guild_id=$2 AND lp_shares > 0",
            pool_id, guild_id,
        )

    async def delete_pool(self, pool_id: str, guild_id: int) -> dict | None:
        """Delete a pool and all associated LP positions/snapshots."""
        pool = await self.get_pool(pool_id, guild_id)
        if pool:
            async with self.transaction() as conn:
                await conn.execute(
                    "DELETE FROM pools WHERE pool_id=$1 AND guild_id=$2", pool_id, guild_id
                )
                await conn.execute(
                    "DELETE FROM lp_positions WHERE pool_id=$1 AND guild_id=$2", pool_id, guild_id
                )
                await conn.execute(
                    "DELETE FROM lp_snapshots WHERE pool_id=$1 AND guild_id=$2", pool_id, guild_id
                )
                await conn.execute(
                    "DELETE FROM group_lp_positions WHERE pool_id=$1 AND guild_id=$2",
                    pool_id, guild_id,
                )
        return pool

    async def create_vault_pool(
        self, guild_id: int, token_sym: str, network_sym: str,
        token_price: float, network_price: float,
    ) -> dict:
        """Create (or fetch) the vault-locked LP for a group token / PoW coin pair.

        Seed size is Config.GROUP_VAULT_POOL_SEED_USD per side at current
        oracle prices, so the treasury panel shows real-looking liquidity
        instead of the old 42-cent dust pool. Pool stays vault_locked so
        players can't swap against it -- the reserves are an accounting
        anchor for the group's mining vault, not a tradeable venue.
        """
        from core.framework.scale import to_raw as _tr
        pool_id, ca, cb = self.make_pool_id(token_sym, network_sym)
        existing = await self.get_pool(pool_id, guild_id)
        if existing:
            return existing

        seed_usd = float(Config.GROUP_VAULT_POOL_SEED_USD)
        tp = max(float(token_price), 1e-12)
        np = max(float(network_price), 1e-12)
        # Human amounts: $seed_usd worth of each side at current prices.
        ra_token   = seed_usd / tp
        rb_network = seed_usd / np
        if ca == token_sym.upper():
            ra, rb = ra_token, rb_network
        else:
            ra, rb = rb_network, ra_token
        lp = math.sqrt(ra * rb)
        await self.execute(
            """INSERT INTO pools
               (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp, vault_locked)
               VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
               ON CONFLICT DO NOTHING""",
            pool_id, guild_id, ca, cb, _tr(ra), _tr(rb), _tr(lp),
        )
        return await self.get_pool(pool_id, guild_id)

    async def vault_add_to_pool(
        self, guild_id: int, token_sym: str, network_sym: str,
        token_delta: float, network_delta: float,
    ) -> dict | None:
        """Atomically add ``token_delta`` and ``network_delta`` to the vault LP reserves.

        Creates the pool if it does not exist yet (first mint from a new group token).
        Returns the updated pool row, or None if both deltas are zero.
        """
        from core.framework.scale import to_raw as _tr
        if token_delta <= 0 and network_delta <= 0:
            return None
        pool_id, ca, cb = self.make_pool_id(token_sym, network_sym)
        if ca == token_sym.upper():
            da, db = _tr(token_delta), _tr(network_delta)
        else:
            da, db = _tr(network_delta), _tr(token_delta)
        row = await self.fetch_one(
            """INSERT INTO pools (pool_id, guild_id, token_a, token_b, reserve_a, reserve_b, total_lp)
               VALUES ($5, $6, $7, $8, $1, $2, sqrt($1::NUMERIC * $2::NUMERIC))
               ON CONFLICT (pool_id, guild_id) DO UPDATE
               SET reserve_a = pools.reserve_a + $3,
                   reserve_b = pools.reserve_b + $4,
                   total_lp  = sqrt((pools.reserve_a + $3::NUMERIC) * (pools.reserve_b + $4::NUMERIC))
               RETURNING *""",
            da, db, da, db, pool_id, guild_id, ca, cb,
        )
        return dict(row) if row else None

    async def vault_remove_from_pool(
        self, guild_id: int, token_sym: str, network_sym: str,
        token_delta: float, network_delta: float,
    ) -> dict | None:
        """Remove ``token_delta`` / ``network_delta`` from vault LP, clamped to >= 0."""
        from core.framework.scale import to_raw as _tr
        pool_id, ca, cb = self.make_pool_id(token_sym, network_sym)
        if ca == token_sym.upper():
            da, db = _tr(token_delta), _tr(network_delta)
        else:
            da, db = _tr(network_delta), _tr(token_delta)
        row = await self.fetch_one(
            """UPDATE pools
               SET reserve_a = GREATEST(0, reserve_a - $1::NUMERIC),
                   reserve_b = GREATEST(0, reserve_b - $2::NUMERIC),
                   total_lp  = GREATEST(0, sqrt(GREATEST(0, reserve_a - $3::NUMERIC) * GREATEST(0, reserve_b - $4::NUMERIC)))
               WHERE pool_id=$5 AND guild_id=$6
               RETURNING *""",
            da, db, da, db, pool_id, guild_id,
        )
        return dict(row) if row else None

    async def seed_group_pool(
        self,
        guild_id: int,
        pool_id: str,
        amount_a: float,
        amount_b: float,
        group_a_id: str,
        group_b_id: str,
        *,
        cost_basis_usd_per_side: float = 0.0,
    ) -> dict | None:
        """Seed an empty group pool with initial liquidity from both groups.

        Both groups contribute equal USD value so the LP is split 50/50.
        LP shares are recorded in group_lp_positions (not lp_positions) so
        they count toward pool total_lp without appearing as any user's holding.

        ``cost_basis_usd_per_side`` is the USD value each group is
        contributing -- written to ``cost_basis_usd_raw`` so a later
        ``,group pool harvest`` removes only the gain over basis
        instead of burning the full position. Defaults to 0 for legacy
        callers; the partnership-acceptance path always passes the
        real per-side seed USD it computed.

        Returns the updated pool row, or None if the pool doesn't exist.
        """
        from core.framework.scale import to_raw as _tr
        from datetime import datetime, timezone

        pool = await self.get_pool(pool_id, guild_id)
        if not pool:
            return None

        lp_total = math.sqrt(amount_a * amount_b)
        lp_each = lp_total / 2.0  # 50/50 because both sides contributed equal USD

        ra_raw = _tr(amount_a)
        rb_raw = _tr(amount_b)
        lp_raw_total = _tr(lp_total)
        lp_raw_each = _tr(lp_each)
        cost_basis_each_raw = _tr(max(0.0, float(cost_basis_usd_per_side)))
        now = datetime.now(timezone.utc)

        async with self.transaction() as conn:
            # Add initial reserves and LP to the pool
            await conn.execute(
                """UPDATE pools
                   SET reserve_a = reserve_a + $1,
                       reserve_b = reserve_b + $2,
                       total_lp  = total_lp  + $3
                   WHERE pool_id=$4 AND guild_id=$5""",
                ra_raw, rb_raw, lp_raw_total, pool_id, guild_id,
            )
            # Create LP positions for both groups (50% each)
            for gid in (group_a_id, group_b_id):
                await conn.execute(
                    """INSERT INTO group_lp_positions
                           (guild_id, group_id, pool_id, lp_shares,
                            cost_basis_usd_raw, seeded_at)
                       VALUES ($1, $2, $3, $4::NUMERIC, $6::NUMERIC, $5)
                       ON CONFLICT (group_id, guild_id, pool_id) DO UPDATE
                           SET lp_shares          = group_lp_positions.lp_shares
                                                  + $4::NUMERIC,
                               cost_basis_usd_raw = group_lp_positions.cost_basis_usd_raw
                                                  + $6::NUMERIC""",
                    guild_id, gid, pool_id, lp_raw_each, now,
                    cost_basis_each_raw,
                )
        return await self.get_pool(pool_id, guild_id)

    async def harvest_group_lp_fees_only(
        self,
        guild_id: int,
        pool_id: str,
        group_id: str,
        price_a_usd: float,
        price_b_usd: float,
    ) -> tuple[float, float, int, int]:
        """Harvest only the FEE EARNINGS portion of a group LP position.

        Computes the position's current USD value at ``price_a_usd`` /
        ``price_b_usd`` and compares it against ``cost_basis_usd_raw``.
        The fraction f = (current_value - cost_basis) / current_value
        of the position's LP shares is removed pro-rata from the pool
        (so the underlying reserve_a / reserve_b withdrawn equals the
        fee gain in token terms), the principal stays in the pool to
        keep earning, and ``cost_basis_usd_raw`` is left untouched so
        future fees compound from the same baseline.

        Also resets ``last_yield_at = NOW()`` so the per-tick passive
        LP-yield sweep doesn't double-pay over the just-harvested
        window. ``last_harvest_at`` is set by the cog (cooldown gate).

        Returns (out_a_h, out_b_h, harvested_lp_raw,
        remaining_lp_raw). When the position is at-or-below cost basis
        nothing is withdrawn and the helper returns (0, 0, 0,
        existing_lp_shares) so the caller can give a clean "no fees
        accrued yet" message instead of erroring.
        """
        from core.framework.scale import to_human as _th

        pool = await self.get_pool(pool_id, guild_id)
        if not pool:
            raise ValueError("Pool not found")
        total_lp_raw = int(pool["total_lp"])
        if total_lp_raw <= 0:
            raise ValueError("Pool has no liquidity to harvest")
        glp = await self.fetch_one(
            "SELECT lp_shares, cost_basis_usd_raw FROM group_lp_positions "
            "WHERE guild_id=$1 AND group_id=$2 AND pool_id=$3",
            guild_id, group_id, pool_id,
        )
        if not glp:
            raise ValueError("Group LP position not found")
        lp_shares_raw = int(glp.get("lp_shares") or 0)
        if lp_shares_raw <= 0:
            return 0.0, 0.0, 0, 0
        cost_basis_raw = int(glp.get("cost_basis_usd_raw") or 0)

        reserve_a_raw = int(pool["reserve_a"])
        reserve_b_raw = int(pool["reserve_b"])
        # Position's current pro-rata share of pool reserves.
        share_a_raw = reserve_a_raw * lp_shares_raw // total_lp_raw
        share_b_raw = reserve_b_raw * lp_shares_raw // total_lp_raw
        current_value_usd = (
            _th(share_a_raw) * float(price_a_usd)
            + _th(share_b_raw) * float(price_b_usd)
        )
        cost_basis_usd = _th(cost_basis_raw)
        gain_usd = current_value_usd - cost_basis_usd
        if gain_usd <= 0 or current_value_usd <= 0:
            # No fees yet. Don't touch the position.
            return 0.0, 0.0, 0, lp_shares_raw

        # Fraction of THIS position to burn (preserves principal exactly).
        frac = gain_usd / current_value_usd
        # Clamp 0 < frac <= 1 in case of rounding noise.
        frac = max(0.0, min(1.0, frac))
        harvested_lp_raw = int(lp_shares_raw * frac)
        if harvested_lp_raw <= 0:
            return 0.0, 0.0, 0, lp_shares_raw
        # Reserves withdrawn = same fraction of the position's pool share
        # so the remaining LP retains exactly the principal value.
        out_a_raw = reserve_a_raw * harvested_lp_raw // total_lp_raw
        out_b_raw = reserve_b_raw * harvested_lp_raw // total_lp_raw
        new_total_lp = total_lp_raw - harvested_lp_raw
        new_ra = reserve_a_raw - out_a_raw
        new_rb = reserve_b_raw - out_b_raw
        new_lp_shares = lp_shares_raw - harvested_lp_raw

        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE pools SET reserve_a=$1, reserve_b=$2, total_lp=$3
                   WHERE pool_id=$4 AND guild_id=$5""",
                max(0, new_ra), max(0, new_rb),
                max(0, new_total_lp), pool_id, guild_id,
            )
            await conn.execute(
                """UPDATE group_lp_positions
                   SET lp_shares    = GREATEST(0, lp_shares - $1::NUMERIC),
                       last_yield_at = NOW()
                   WHERE guild_id=$2 AND group_id=$3 AND pool_id=$4""",
                harvested_lp_raw, guild_id, group_id, pool_id,
            )
        return (
            _th(out_a_raw), _th(out_b_raw),
            harvested_lp_raw, max(0, new_lp_shares),
        )

    async def deposit_group_lp_from_reserve(
        self,
        guild_id: int,
        pool_id: str,
        group_id: str,
        usd_amount: float,
        price_a_usd: float,
        price_b_usd: float,
    ) -> tuple[float, float, int]:
        """Add LP to a group position by spending ``usd_amount`` of reserve.

        Recovery / top-up path: pulls ``usd_amount`` from the group's
        ``reserve_usd`` and contributes it to the pool, half-and-half
        in USD value across the two sides. The pool's current ratio is
        absorbed at the spot reserve ratio (no slippage modelling --
        this is treasury -> LP, not a swap), and ``lp_shares`` +
        ``cost_basis_usd_raw`` both bump by the contribution so
        subsequent harvests treat the full deposit as principal.

        Returns (added_a_h, added_b_h, added_lp_raw). Raises
        ValueError if the pool / position is missing, prices are zero
        on either side, or the contribution would mint zero LP.
        """
        from core.framework.scale import to_human as _th, to_raw as _tr

        if usd_amount <= 0:
            raise ValueError("USD amount must be positive.")
        if price_a_usd <= 0 or price_b_usd <= 0:
            raise ValueError(
                "Token prices must be set on both sides "
                "(use ,admin setprice if missing)."
            )
        pool = await self.get_pool(pool_id, guild_id)
        if not pool:
            raise ValueError("Pool not found")
        total_lp_raw = int(pool["total_lp"])
        reserve_a_raw = int(pool["reserve_a"])
        reserve_b_raw = int(pool["reserve_b"])
        if total_lp_raw <= 0 or reserve_a_raw <= 0 or reserve_b_raw <= 0:
            raise ValueError(
                "Pool has no live liquidity to deposit into. "
                "Re-seed the partnership first."
            )

        # Half-and-half in USD; convert each side to its token amount
        # at the spot price. With pool-spot-equivalent prices these
        # land at the pool's exact reserve ratio.
        half_usd = usd_amount / 2.0
        add_a_h = half_usd / price_a_usd
        add_b_h = half_usd / price_b_usd
        add_a_raw = _tr(add_a_h)
        add_b_raw = _tr(add_b_h)
        if add_a_raw <= 0 or add_b_raw <= 0:
            raise ValueError("Contribution too small to mint LP.")

        # Mint LP in proportion to the side that scales smaller (so we
        # never over-mint when prices drift slightly off-pool-ratio).
        mint_from_a = total_lp_raw * add_a_raw // reserve_a_raw
        mint_from_b = total_lp_raw * add_b_raw // reserve_b_raw
        delta_lp_raw = min(mint_from_a, mint_from_b)
        if delta_lp_raw <= 0:
            raise ValueError("Contribution too small to mint LP.")

        usd_raw = _tr(usd_amount)
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE pools SET
                       reserve_a = reserve_a + $1::NUMERIC,
                       reserve_b = reserve_b + $2::NUMERIC,
                       total_lp  = total_lp  + $3::NUMERIC
                   WHERE pool_id=$4 AND guild_id=$5""",
                add_a_raw, add_b_raw, delta_lp_raw, pool_id, guild_id,
            )
            await conn.execute(
                """INSERT INTO group_lp_positions (
                       guild_id, group_id, pool_id, lp_shares,
                       cost_basis_usd_raw, seeded_at, last_yield_at
                   )
                   VALUES ($1, $2, $3, $4::NUMERIC, $5::NUMERIC, NOW(), NOW())
                   ON CONFLICT (group_id, guild_id, pool_id) DO UPDATE
                       SET lp_shares          = group_lp_positions.lp_shares
                                              + EXCLUDED.lp_shares,
                           cost_basis_usd_raw = group_lp_positions.cost_basis_usd_raw
                                              + EXCLUDED.cost_basis_usd_raw,
                           last_yield_at      = NOW()""",
                guild_id, group_id, pool_id,
                delta_lp_raw, usd_raw,
            )
        return _th(add_a_raw), _th(add_b_raw), delta_lp_raw

    async def rebalance_pool(self, pool_id: str, guild_id: int, new_price: float) -> None:
        """Set pool reserves so price = new_price while preserving k = reserve_a * reserve_b."""
        pool = await self.get_pool(pool_id, guild_id)
        if not pool:
            return
        k = float(pool["reserve_a"]) * float(pool["reserve_b"])
        if k <= 0 or new_price <= 0:
            return  # Cannot rebalance an empty pool -- no reserves to redistribute
        new_a = math.sqrt(k / new_price)
        if new_a <= 0:
            return
        new_b = k / new_a
        await self.update_pool_reserves(pool_id, guild_id, new_a, new_b, pool["total_lp"])
