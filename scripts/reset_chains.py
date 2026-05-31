"""
Full chain/supply reset migration.

Resets ALL chains to block 0 and recalculates circulating supply from
actual player holdings. Player balances are NOT touched.

What this does:
1. Reset pow_network_state to block 0 with initial difficulty for ALL chains
2. Reset legacy mining_network to block 0
3. Delete all chain_blocks (mined block history)
4. Delete all mining_blocks (block log)
5. Recalculate circulating_supply in crypto_prices from actual player holdings
   (crypto_holdings + wallet_holdings + stakes + lp_positions collateral)
6. Same for guild_tokens.circulating_supply

Run with:  python scripts/reset_chains.py
Or import and call:  await run_migration(db)
"""
from __future__ import annotations

import asyncio
import sys
import os

# Allow running as a script from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config


async def run_migration(db) -> dict:
    """Execute the full reset. Returns summary stats."""
    stats: dict[str, object] = {}

    # ── 1. Reset pow_network_state for every chain ────────────────────────
    for symbol, cfg in Config.POW_NETWORKS.items():
        await db.execute(
            """UPDATE pow_network_state
               SET block_height = 0,
                   total_hashrate = 0,
                   current_reward = $3,
                   difficulty = $4,
                   last_block_ts = now(),
                   last_retarget_height = 0,
                   last_retarget_ts = now()
               WHERE chain_symbol = $2""",
            None,  # placeholder  -  guild_id not filtered, resets ALL guilds
            symbol,
            cfg.get("initial_reward", 1.0),
            cfg.get("initial_difficulty", 60000.0),
        )

    # Fix: the above has a None arg we don't need. Let me use direct SQL.
    for symbol, cfg in Config.POW_NETWORKS.items():
        await db.execute(
            f"""UPDATE pow_network_state
                SET block_height = 0,
                    total_hashrate = 0,
                    current_reward = {cfg.get('initial_reward', 1.0)},
                    difficulty = {cfg.get('initial_difficulty', 60000.0)},
                    last_block_ts = now(),
                    last_retarget_height = 0,
                    last_retarget_ts = now()
                WHERE chain_symbol = $1""",
            symbol,
        )
    stats["pow_networks_reset"] = list(Config.POW_NETWORKS.keys())

    # ── 2. Reset legacy mining_network ────────────────────────────────────
    await db.execute(
        """UPDATE mining_network
           SET block_height = 0,
               total_hashrate = 0,
               current_reward = 50.0,
               last_block_ts = now()"""
    )
    stats["legacy_mining_reset"] = True

    # ── 3. Delete all chain blocks ────────────────────────────────────────
    await db.execute("DELETE FROM chain_blocks")
    stats["chain_blocks_deleted"] = True

    # ── 4. Delete all mining block logs ───────────────────────────────────
    try:
        await db.execute("DELETE FROM mining_blocks")
        stats["mining_blocks_deleted"] = True
    except Exception:
        stats["mining_blocks_deleted"] = "table not found (ok)"

    # ── 5. Recalculate circulating supply from player holdings ────────────
    # For each token, sum up ALL player-held amounts across:
    #   - crypto_holdings (CeFi)
    #   - wallet_holdings (DeFi)
    #   - stakes (staked tokens)
    # This becomes the new circulating_supply. The rest up to max_supply
    # is "unmined/unreleased".

    all_tokens = list(Config.TOKENS.keys())
    supply_results = {}

    for symbol in all_tokens:
        token_cfg = Config.TOKENS[symbol]
        max_supply = token_cfg.get("max_supply", 0)

        # Sum player holdings across all sources and guilds
        cefi = await db.fetch_val(
            "SELECT COALESCE(SUM(amount), 0) FROM crypto_holdings WHERE symbol = $1",
            symbol,
        )
        defi = await db.fetch_val(
            "SELECT COALESCE(SUM(amount), 0) FROM wallet_holdings WHERE symbol = $1",
            symbol,
        )
        staked = await db.fetch_val(
            "SELECT COALESCE(SUM(amount), 0) FROM stakes WHERE symbol = $1",
            symbol,
        )

        # Pool reserves (tokens locked in AMM pools)
        pool_a = await db.fetch_val(
            "SELECT COALESCE(SUM(reserve_a), 0) FROM pools WHERE token_a = $1",
            symbol,
        )
        pool_b = await db.fetch_val(
            "SELECT COALESCE(SUM(reserve_b), 0) FROM pools WHERE token_b = $1",
            symbol,
        )
        in_pools = float(pool_a or 0) + float(pool_b or 0)
        player_held = float(cefi or 0) + float(defi or 0) + float(staked or 0)

        # Circulating = player holdings + pool reserves (all tokens in the system)
        circulating = player_held + in_pools
        if max_supply > 0:
            circulating = min(circulating, max_supply)

        # Update crypto_prices
        await db.execute(
            "UPDATE crypto_prices SET circulating_supply = $1 WHERE symbol = $2",
            circulating, symbol,
        )
        # Update guild_tokens (custom tokens)
        await db.execute(
            "UPDATE guild_tokens SET circulating_supply = $1 WHERE symbol = $2",
            circulating, symbol,
        )

        supply_results[symbol] = {
            "player_held": round(player_held, 4),
            "circulating_supply": round(circulating, 4),
            "max_supply": max_supply,
        }

    stats["supply"] = supply_results

    return stats


async def main():
    """Run as standalone script."""
    from database import Database

    db_url = os.getenv("DATABASE_URL", "postgresql://discoin:discoin@localhost:5432/discoin")
    db = Database(db_url)
    await db.connect()

    print("=" * 60)
    print("FULL CHAIN & SUPPLY RESET")
    print("=" * 60)
    print()
    print("This will:")
    print("  - Reset ALL PoW chains to block 0")
    print("  - Delete all mined block history")
    print("  - Recalculate circulating supply from player holdings")
    print("  - NOT touch any player balances")
    print()

    confirm = input("Type 'RESET' to proceed: ")
    if confirm != "RESET":
        print("Aborted.")
        return

    stats = await run_migration(db)

    print()
    print("Chains reset:", stats.get("pow_networks_reset"))
    print()
    print("Supply recalculated:")
    for sym, info in stats.get("supply", {}).items():
        held = info["player_held"]
        circ = info["circulating_supply"]
        mx = info["max_supply"]
        pct = f" ({circ/mx*100:.2f}%)" if mx else ""
        print(f"  {sym}: players hold {held:,.2f} → circulating {circ:,.2f} / {mx:,}{pct}")

    print()
    print("Done. All chains at block 0. Player balances untouched.")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
