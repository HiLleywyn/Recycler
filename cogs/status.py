# cogs/status.py
"""Player-facing .status command -- shows bot service health without sensitive info.

Displays green/yellow/red per system with verifiable proof each system is working
(last block, last trade, last validator tick, etc.). No IPs, no DB stats, no errors.
Three pages: Overview, System Health, Economy Snapshot.
"""
from __future__ import annotations

import time

import discord
from discord.ext import commands

from core.config import Config
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.heartbeat import (
    get_all as _get_all_heartbeats,
    get_all_intervals as _get_all_intervals,
    stale_tasks,
)
from core.framework.middleware import guild_only
from core.framework.scale import to_human as _h
from core.framework.ui import send_paginated, C_INFO, FormatKit


def _age_str(secs: float) -> str:
    """Human-readable age string."""
    if secs < 60:
        return f"{secs:.0f}s ago"
    if secs < 3600:
        return f"{secs / 60:.0f}m ago"
    return f"{secs / 3600:.1f}h ago"


def _status_icon(age: float | None, healthy_max: float = 300) -> str:
    """Return status icon based on age in seconds. None = never."""
    if age is None:
        return "\U0001F534"  # red
    if age < healthy_max:
        return "\U0001F7E2"  # green
    if age < healthy_max * 2:
        return "\U0001F7E1"  # yellow
    return "\U0001F534"  # red


def _pct_change(current: float, open_price: float) -> str:
    """Format daily % change from open to current."""
    if not open_price or open_price == 0:
        return "--"
    pct = ((current - open_price) / open_price) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


class Status(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.command(name="status")
    @guild_only
    async def status(self, ctx: DiscoContext) -> None:
        """Live system health -- 3 pages: overview, services, economy."""
        db = ctx.db
        guild_id = ctx.guild_id
        now = time.time()
        heartbeats = _get_all_heartbeats()

        pages: list[discord.Embed] = []

        # ==================================================================
        # PAGE 1: Overview
        # ==================================================================
        uptime_secs = now - getattr(self.bot, "_start_time", now)
        lat = self.bot.latency * 1000

        p1 = card("Status -- Overview", color=C_INFO)

        # Connection & uptime
        p1.field(
            "Connection",
            f"Latency: **{lat:.0f}ms** | Uptime: **{FormatKit.time_ago(int(uptime_secs))}**",
            False,
        )

        # Server stats
        users = await db.get_all_guild_users(guild_id)
        prices = await db.get_all_prices(guild_id)
        pools = await db.get_all_pools(guild_id)
        p1.field(
            "Server Stats",
            f"Players: **{len(users or [])}** | Tokens: **{len(prices or [])}** | Pools: **{len(pools or [])}**",
            False,
        )

        # Last trade
        txs = await db.get_guild_tx_history(guild_id, limit=1)
        if txs:
            tx = txs[0]
            tx_ts = tx.get("ts")
            if tx_ts:
                if hasattr(tx_ts, "timestamp"):
                    tx_age = int(now - tx_ts.timestamp())
                else:
                    tx_age = 0
                ago = FormatKit.time_ago(tx_age) if tx_age > 0 else "just now"
            else:
                ago = "unknown"
            token = tx.get("symbol_in") or tx.get("symbol_out") or "?"
            p1.field(
                "Last Trade",
                f"Type: **{tx.get('tx_type', '?')}** | Token: **{token}** | {ago}",
                False,
            )
        else:
            p1.field("Last Trade", "No trades recorded yet", False)

        # Sample token prices (top 6)
        if prices:
            price_lines = []
            for row in (prices or [])[:6]:
                sym = row["symbol"]
                tok_cfg = Config.TOKENS.get(sym, {})
                emoji = tok_cfg.get("emoji", "")
                p = float(row["price"])
                op = float(row.get("open_price") or p)
                change = _pct_change(p, op)
                price_lines.append(f"{emoji} **{sym}** ${p:,.4f} ({change})")
            p1.field("Token Prices", "\n".join(price_lines), False)

        p1.footer("Page 1/3 -- Overview")
        pages.append(p1.build())

        # ==================================================================
        # PAGE 2: System Health
        # ==================================================================
        p2 = card("Status -- System Health", color=C_INFO)

        intervals = _get_all_intervals()

        # Order matches the player's mental model: prices → trading economy →
        # chain/mining → staking/validators → savings/loans → security → drops.
        # Healthy thresholds are derived from the registered interval where
        # available (3x interval, with a sensible floor) so the status icon
        # always matches the actual loop cadence rather than a hardcoded guess.
        _SERVICES: list[tuple[str, str, float]] = [
            ("price_drift_trade", "Price Engine",      120),
            ("lp_yield",          "LP Yield Payouts",  7200),
            ("hourly_summary",    "Hourly Summary",    7200),
            ("mining_tick",       "PoW Mining",        360),
            ("chain_tick",        "Chain Blocks",      Config.CHAIN_BLOCK_INTERVAL * 3),
            ("keeper_loop",       "Mempool Keeper",    300),
            ("staking_tick",      "PoS Staking",       7200),
            ("validator_tick",    "PoW Validators",    7200),
            ("pos_validator_tick","PoS Validators",    7200),
            ("savings_interest",  "Savings Interest",  3600),
            ("loan_interest",     "Loan Interest",     3600),
            ("security_scan",     "Security Monitor",  600),
            ("rugpull_integrity", "Rugpull Integrity", 1800),
            ("faucet",            "Faucet Drops",      Config.AUTO_DROP_INTERVAL * 3),
            ("lunar_tick",        "Lunar Cycle",       7200),
            ("season_expiry",     "Season Expiry",     7200),
            ("challenge_expiry",  "Challenge Expiry",  7200),
            ("backup",            "Backup",            Config.BACKUP_INTERVAL_HOURS * 3600 * 2),
        ]

        svc_lines = []
        for hb_key, label, healthy_max in _SERVICES:
            # Prefer the registered interval (interval × 3, floor 5m) so the
            # icon reflects each loop's actual cadence and not a stale guess.
            registered = intervals.get(hb_key)
            threshold = max(registered * 3, 300) if registered else healthy_max
            age = (now - heartbeats[hb_key]) if hb_key in heartbeats else None
            icon = _status_icon(age, threshold)
            detail = _age_str(age) if age is not None else "no data"
            svc_lines.append(f"{icon} **{label}** -- {detail}")

        # Market events status (non-heartbeat, DB-driven)
        ev_state = await db.get_guild_event(guild_id)
        settings = await db.get_guild_settings(guild_id)
        events_on = settings.get("module_events", True) if settings else True
        if not events_on:
            svc_lines.append(f"\U0001F534 **Market Events** -- module disabled")
        elif ev_state and ev_state.get("current_event"):
            from cogs.events import MARKET_EVENTS
            ek = ev_state["current_event"]
            ev = MARKET_EVENTS.get(ek, {})
            expires = ev_state.get("event_expires_at")
            remaining = 0
            if expires is not None:
                exp_ts = expires.timestamp() if hasattr(expires, "timestamp") else float(expires)
                remaining = max(0, int(exp_ts - now))
            m, s = divmod(remaining, 60)
            svc_lines.append(f"\U0001F7E2 **Market Events** -- {ev.get('emoji', '')} {ev.get('title', ek)} ({m}m {s}s left)")
        else:
            svc_lines.append(f"\U0001F7E2 **Market Events** -- idle (no active event)")

        p2.description("\n".join(svc_lines))

        stale = stale_tasks(max_age=600)
        if stale:
            p2.footer(f"Page 2/3 -- {len(stale)} task(s) may be delayed")
        else:
            p2.footer("Page 2/3 -- All background tasks running")

        pages.append(p2.build())

        # ==================================================================
        # PAGE 3: Economy Snapshot
        # ==================================================================
        p3 = card("Status -- Economy Snapshot", color=C_INFO)

        # PoW chain info
        pow_chains = await db.get_all_guild_pow_networks(guild_id)
        if pow_chains:
            chain_lines = []
            for ch in pow_chains:
                sym = ch.get("chain_symbol", "?")
                height = ch.get("block_height", 0)
                diff = float(ch.get("difficulty", 0))
                hr = float(ch.get("total_hashrate", 0))
                chain_lines.append(
                    f"**{sym}** -- Block #{height:,} | Diff {diff:,.0f} | HR {hr:,.1f}"
                )
            p3.field("PoW Chains", "\n".join(chain_lines), False)
        else:
            p3.field("PoW Chains", "No networks initialized", False)

        # Validators
        pow_validators = await db.get_validators(guild_id)
        pos_validators = await db.get_pos_validators(guild_id)
        pow_active = len([v for v in (pow_validators or []) if v.get("is_active")])
        pos_active = len([v for v in (pos_validators or []) if v.get("is_active")])
        # stake_amount is NUMERIC(36,0) scaled by 10**18; descale before display
        # so totals show real dollars instead of quintillions.
        total_staked = sum(_h(v.get("stake_amount", 0)) for v in (pos_validators or []))
        p3.field(
            "Validators",
            f"PoW active: **{pow_active}** | PoS active: **{pos_active}** | Total staked: **${total_staked:,.2f}**",
            False,
        )

        # Pool TVL
        if pools:
            total_tvl = 0.0
            price_map = {r["symbol"]: float(r["price"]) for r in (prices or [])}
            for pool in pools:
                # reserve_a / reserve_b are NUMERIC(36,0) scaled by 10**18
                ra = _h(pool.get("reserve_a", 0))
                rb = _h(pool.get("reserve_b", 0))
                pa = price_map.get(pool.get("token_a", ""), 0)
                pb = price_map.get(pool.get("token_b", ""), 0)
                total_tvl += ra * pa + rb * pb
            p3.field(
                "Pool TVL",
                f"**{len(pools)}** pools | Total: **${total_tvl:,.2f}**",
                False,
            )
        else:
            p3.field("Pool TVL", "No pools active", False)

        # Events config summary
        if settings:
            disabled_ev = settings.get("disabled_events", "")
            disabled_count = len(list(filter(None, disabled_ev.split(",")))) if disabled_ev else 0
            freq = float(settings.get("event_frequency") or 0.0005)
            freq_label = "off" if freq == 0 else f"{freq:.4f}/tick"
            ev_status = "enabled" if events_on else "disabled"
            p3.field(
                "Events Config",
                f"Module: **{ev_status}** | Frequency: **{freq_label}** | Disabled types: **{disabled_count}**",
                False,
            )

        # Progression activity: small rollup across the achievement,
        # streak, season, and challenge tables so a glance at ,status
        # shows how alive the server is beyond raw economy numbers.
        try:
            from services.progression import guild_totals
            _g = await guild_totals(db, guild_id)
            p3.field(
                "Progression Activity",
                f"Active streaks: **{_g['active_streaks']}** | "
                f"Badges earned: **{_g['total_badges_earned']}** | "
                f"Active seasons: **{_g['active_seasons']}** | "
                f"Active challenges: **{_g['active_challenges']}**",
                False,
            )
        except Exception:
            pass

        p3.footer("Page 3/3 -- Economy Snapshot")
        pages.append(p3.build())

        await send_paginated(ctx, pages)


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Status(bot))
