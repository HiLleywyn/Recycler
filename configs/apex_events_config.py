"""V3 Pillar 6: Apex Events catalogue.

Declarative. Each event has:
    id              stable string id
    name            display name
    flavour         short story line
    duration_secs   how long it runs once triggered
    rarity          info / warning / volatile / catastrophe (drives color)
    modifiers       {modifier_key: float_value}
    weight          relative likelihood per roll

The roller (services.apex_events.try_roll) picks one weighted-random
event per guild per tick. Cooldowns are enforced by the active-window
check in services so the same event can't double-roll while live.
"""
from __future__ import annotations


EVENTS: dict[str, dict] = {
    "solar_flare": {
        "name": "Solar Flare",
        "flavour": (
            "Mining rigs spin faster under the geomagnetic surge, but "
            "deep-sea sonar interferes with the fishing fleet."
        ),
        "duration_secs": 3600,        # 1 hour
        "rarity": "volatile",
        "modifiers": {
            "mining.hashrate":   1.50,
            "fishing.catch_rate": 0.80,
            "dungeon.mob_damage": 1.25,
        },
        "weight": 8,
    },
    "blood_moon": {
        "name": "Blood Moon",
        "flavour": (
            "PvP raid cooldowns shorten and gamba payouts climb, but "
            "savings interest cools as risk-on dominates."
        ),
        "duration_secs": 86400,       # 24 hours
        "rarity": "warning",
        "modifiers": {
            "exploit.cooldown":  0.50,
            "gamba.payout":      1.15,
            "savings.apr":       0.90,
        },
        "weight": 3,
    },
    "harvest_bloom": {
        "name": "Harvest Bloom",
        "flavour": (
            "Crops grow at twice the usual pace; LP fees waive on "
            "the major commodity pairs while the bloom lasts."
        ),
        "duration_secs": 43200,       # 12 hours
        "rarity": "info",
        "modifiers": {
            "farming.yield":     1.40,
            "lp.fee":            0.50,
            "auction.cut":       1.00,
        },
        "weight": 6,
    },
    "vault_tremor": {
        "name": "Vault Tremor",
        "flavour": (
            "Network vaults shed 1% of their stake; in exchange every "
            "mastery XP event pays triple."
        ),
        "duration_secs": 1800,        # 30 minutes
        "rarity": "catastrophe",
        "modifiers": {
            "vault.drain_pct":   0.01,
            "mastery.xp_mult":   3.00,
        },
        "weight": 2,
    },
    "deep_liquidity": {
        "name": "Deep Liquidity Wave",
        "flavour": (
            "Pool depths surge across every AMM; large trades suffer "
            "less slippage and LP yield ticks pay 25% more."
        ),
        "duration_secs": 21600,       # 6 hours
        "rarity": "info",
        "modifiers": {
            "swap.impact_mult":  0.75,
            "lp.yield_bonus":    1.25,
        },
        "weight": 5,
    },
    "raider_dawn": {
        "name": "Raider Dawn",
        "flavour": (
            "Exploit raids do double damage but defenders catch double "
            "the rewards on a successful block."
        ),
        "duration_secs": 7200,        # 2 hours
        "rarity": "warning",
        "modifiers": {
            "exploit.damage":    2.00,
            "exploit.defense_reward": 2.00,
        },
        "weight": 4,
    },
    "silent_market": {
        "name": "Silent Market",
        "flavour": (
            "Traders go quiet -- the chart drifts less than usual, "
            "swap impact softens, but auction listings pay 10% more."
        ),
        "duration_secs": 14400,       # 4 hours
        "rarity": "info",
        "modifiers": {
            "trade.drift_mult":  0.50,
            "auction.cut":       0.90,
        },
        "weight": 5,
    },
}


def total_weight() -> int:
    return sum(e["weight"] for e in EVENTS.values())
