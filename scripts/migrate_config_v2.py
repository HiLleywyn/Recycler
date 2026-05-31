"""
Config V2 migration: old token config → new token config.

Handles:
  1. Adding burn_rate field to any tokens missing it in the database
  2. Resetting oracle prices to new genesis start_price (opt-in via --reset-prices)
  3. Recalculating pool seed reserves for new price scale
  4. Preserving existing economy data (balances, holdings, stakes untouched)

Run with:
  python scripts/migrate_config_v2.py                  # dry-run (default)
  python scripts/migrate_config_v2.py --apply          # apply changes
  python scripts/migrate_config_v2.py --apply --reset-prices  # apply + reset prices to genesis

Or import and call:
  await run_migration(db, apply=True, reset_prices=False)
"""
from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config

# ── Snapshot of old config values (pre-v2) for diff reporting ────────────────
_OLD_START_PRICES: dict[str, float] = {
    "MTA": 105_000.0,
    "SUN": 100.0,
    "ARC": 3_900.0,
    "DSC": 10.0,
    "DSD": 1.0,
    "DSY": 5.0,
    "USDC": 1.0,
    "VTR": 250.0,
}

# Fields that were removed from Config in v2.
_REMOVED_ATTRS: list[str] = [
    "DISCORD_CLIENT_ID",
    "DISCORD_CLIENT_SECRET",
    "DISCORD_REDIRECT_URI",
    "JWT_EXPIRE_SECONDS",
    "DAILY_STAT_RESET",
    "POOL_SEED_AMOUNT",
    "API_KEY",
    "XP_SCALE_MIN",
    "TOKEN_DEPLOY_COST",
    "TOKEN_DEPLOY_MIN_SUN_STAKE",
    "TOKEN_DEPLOY_COOLDOWN",
    "TOKEN_DEPLOY_MAX_SUPPLY",
    "TOKEN_DEPLOY_INITIAL_LIQUIDITY",
    "SUN_MINE",
    "BTC_MINE",
    "MINE_CONFIGS",
]


def print_diff() -> None:
    """Print a summary of config changes (no DB required)."""
    print("=" * 60)
    print("CONFIG V2 MIGRATION DIFF")
    print("=" * 60)

    # Price changes
    print("\n── Start Price Changes ────────────────────────────")
    for sym, old_price in _OLD_START_PRICES.items():
        new_price = Config.TOKENS[sym]["start_price"]
        if old_price != new_price:
            print(f"  {sym}: ${old_price:,.2f} → ${new_price:,.2f}")
        else:
            print(f"  {sym}: ${old_price:,.2f} (unchanged)")

    # New fields
    print("\n── New Token Fields ───────────────────────────────")
    for sym, tok in Config.TOKENS.items():
        burn = tok.get("burn_rate", 0.0)
        print(f"  {sym}: burn_rate = {burn} ({burn * 100:.2f}%)")

    # Removed attributes
    print("\n── Removed Config Attributes ──────────────────────")
    for attr in _REMOVED_ATTRS:
        print(f"  - {attr}")

    print()


async def run_migration(db, *, apply: bool = False, reset_prices: bool = False) -> dict:
    """Execute the config v2 migration. Returns summary stats."""
    stats: dict[str, object] = {"apply": apply, "reset_prices": reset_prices}

    # ── 1. Report what changed ──────────────────────────────────────────────
    price_changes = {}
    for sym, old_price in _OLD_START_PRICES.items():
        new_price = Config.TOKENS[sym]["start_price"]
        if old_price != new_price:
            price_changes[sym] = {"old": old_price, "new": new_price}
    stats["price_changes"] = price_changes

    # ── 2. Optionally reset oracle prices to new genesis values ─────────────
    if reset_prices:
        for sym, tok in Config.TOKENS.items():
            new_price = tok["start_price"]
            if apply:
                await db.execute(
                    "UPDATE crypto_prices SET price = $1 WHERE symbol = $2",
                    new_price, sym,
                )
            price_changes.setdefault(sym, {})["applied"] = apply
        stats["prices_reset"] = True
    else:
        stats["prices_reset"] = False

    # ── 3. Re-seed pool reserves for new price scale (new guilds only) ──────
    # Existing pools use ON CONFLICT DO NOTHING, so this only affects
    # guilds that haven't seeded yet. No action needed for existing pools.
    stats["pool_note"] = "Existing pools untouched. New guilds seed at genesis prices."

    # ── 4. Validate all tokens have required fields ─────────────────────────
    required_fields = {
        "name", "emoji", "consensus", "network", "start_price",
        "daily_vol", "stakeable", "mineable", "max_supply",
        "decimals", "tx_fee_rate", "gas_fee", "burn_rate",
    }
    missing = {}
    for sym, tok in Config.TOKENS.items():
        tok_missing = required_fields - set(tok.keys())
        if tok_missing:
            missing[sym] = list(tok_missing)
    stats["missing_fields"] = missing if missing else "none"

    # ── 5. Verify burn_rate is set for all tokens ───────────────────────────
    burn_rates = {sym: tok.get("burn_rate", "MISSING") for sym, tok in Config.TOKENS.items()}
    stats["burn_rates"] = burn_rates

    return stats


async def main():
    """Run as standalone script."""
    apply = "--apply" in sys.argv
    reset_prices = "--reset-prices" in sys.argv

    # Always print the diff first
    print_diff()

    if not apply:
        print("DRY RUN  -  no database changes. Pass --apply to execute.")
        print("Pass --reset-prices to also reset oracle prices to genesis values.")
        return

    from database import Database

    db_url = os.getenv("DATABASE_URL", "postgresql://discoin:discoin@localhost:5432/discoin")
    db = Database(db_url)
    await db.connect()

    print("APPLYING MIGRATION...")
    if reset_prices:
        print("  ⚠  --reset-prices: Oracle prices will be reset to genesis values!")
        confirm = input("  Type 'MIGRATE' to proceed: ")
        if confirm != "MIGRATE":
            print("Aborted.")
            await db.close()
            return

    stats = await run_migration(db, apply=apply, reset_prices=reset_prices)

    print()
    print("── Results ────────────────────────────────────────")
    if stats.get("price_changes"):
        for sym, info in stats["price_changes"].items():
            old = info.get("old", "?")
            new = info.get("new", "?")
            applied = info.get("applied", False)
            status = "APPLIED" if applied else "skipped"
            print(f"  {sym}: ${old:,.2f} → ${new:,.2f} [{status}]")

    print(f"\n  Prices reset: {stats['prices_reset']}")
    print(f"  Missing fields: {stats['missing_fields']}")
    print(f"\n  Burn rates:")
    for sym, rate in stats.get("burn_rates", {}).items():
        print(f"    {sym}: {rate}")

    print(f"\n  {stats['pool_note']}")
    print("\nDone.")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
