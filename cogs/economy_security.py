"""
Economy Security Monitor
========================

Passive background analysis of player behavior to detect abusive patterns.
Two detection modes:
  1. **Periodic scan**  -  Every 2 minutes, queries the transaction ledger
  2. **Real-time events**  -  Subscribes to whale_alert, game_result, and other
     bus events for instant detection

DMs REPORT_TARGET_USER_ID with AI-generated summaries.  Does NOT mute,
timeout, or penalize players.  Observation and reporting only.

Detections:
  1. Income velocity     -  Earning far more than normal in a short window
  2. Gambling velocity    -  Playing games at inhuman speed / volume
  3. Wash trading         -  Buy-then-sell (or swap loops) with no economic purpose
  4. Transfer rings       -  Rapid fund movement from one user
  5. Rapid LP churn       -  Add/remove liquidity repeatedly (sandwich-style)
  6. Whale concentration  -  Single user dominating transaction volume
  7. Repeat offender      -  User who has been flagged multiple times
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.ai import complete as ai_complete
from core.framework.embed import card
from core.framework.heartbeat import pulse, register_interval
from core.framework.scale import to_human as _h
from core.framework.ui import C_WARNING, fmt_usd

if TYPE_CHECKING:
    from core.framework.bot import Discoin

# ── Thresholds ────────────────────────────────────────────────────────────────
from constants.security import (
    SCAN_INTERVAL_SECONDS as _SCAN_INTERVAL,
    LOOKBACK_SECONDS as _LOOKBACK,
    INCOME_VELOCITY_LIMIT as _INCOME_TX_LIMIT,
    GAMBLING_VELOCITY_LIMIT as _GAMBLING_TX_LIMIT,
    WASH_TRADE_MIN_CYCLES as _WASH_TRADE_MIN,
    TRANSFER_RING_MIN as _TRANSFER_RING_MIN,
    LP_CHURN_MIN as _LP_CHURN_MIN,
    REPEAT_OFFENDER_LIMIT as _REPEAT_OFFENDER_LIMIT,
    WHALE_CONCENTRATION_LIMIT as _WHALE_CONCENTRATION_LIMIT,
    ALERT_COOLDOWN_SECONDS as _ALERT_COOLDOWN,
)

# Whale alert tracking  -  accumulates whale-sized actions per user per window
_whale_actions: dict[tuple[int, int], list[dict]] = {}  # (uid, gid) → [{action, usd, ts}, ...]

# Track how many times each user has been flagged (in-memory, per session)
_flag_counts: dict[tuple[int, int], int] = {}  # (user_id, guild_id) → count
# Cooldown so we don't spam the admin with the same user repeatedly
_last_alert: dict[tuple[int, int], float] = {}  # (user_id, guild_id) → timestamp

# ── AI prompt for generating the alert summary ────────────────────────────────
_ALERT_SYSTEM = (
    "You are Discoin's economy security monitor. You've detected suspicious "
    "player behavior in a Discord economy game. Write a SHORT (2-4 sentences) "
    "DM alert for the server owner. Be direct, slightly witty, and specific "
    "about what the player did. Include the detection type and key numbers. "
    "If this is a repeat offender, mention how many times they've been flagged. "
    "If whale alerts were involved, note the scale of their transactions. "
    "Do NOT use markdown. Do NOT use emojis. Keep it professional but with personality."
)


class EconomySecurity(commands.Cog):
    """Passive economy abuse detection  -  observes and reports, never punishes."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self.security_scan.start()
        register_interval("security_scan", _SCAN_INTERVAL)

        # Subscribe to real-time bus events for instant detection
        bot.bus.subscribe("whale_alert", self._on_whale_alert)
        bot.bus.subscribe("game_result", self._on_game_result)

    def cog_unload(self) -> None:
        self.security_scan.cancel()

    # ── Real-time event handlers ──────────────────────────────────────────────

    async def _on_whale_alert(self, **kwargs) -> None:
        """Track whale-sized transactions per user for concentration detection."""
        guild = kwargs.get("guild")
        user_id = kwargs.get("user_id")
        if not guild or not user_id:
            return

        key = (user_id, guild.id)
        entry = {
            "action": kwargs.get("action", "unknown"),
            "usd_value": kwargs.get("usd_value", 0),
            "symbol": kwargs.get("symbol", kwargs.get("symbol_in", "")),
            "ts": time.time(),
        }

        if key not in _whale_actions:
            _whale_actions[key] = []

        # Prune old entries
        now = time.time()
        _whale_actions[key] = [
            w for w in _whale_actions[key] if now - w["ts"] < _LOOKBACK
        ]
        _whale_actions[key].append(entry)

        # Immediate check: too many whale-tier actions in the window?
        if len(_whale_actions[key]) >= _WHALE_CONCENTRATION_LIMIT:
            total_usd = sum(w["usd_value"] for w in _whale_actions[key])
            actions = ", ".join(set(w["action"] for w in _whale_actions[key]))
            alert = (
                f"WHALE_CONCENTRATION: {len(_whale_actions[key])} whale-tier "
                f"actions in {_LOOKBACK // 60}min ({fmt_usd(total_usd)} total). "
                f"Actions: {actions}"
            )
            await self._send_alert(guild, user_id, [alert])

    async def _on_game_result(self, **kwargs) -> None:
        """Placeholder for real-time game pattern analysis.
        The periodic scan handles most gambling detection; this catches
        edge cases like a sudden massive win streak."""
        pass  # Future: real-time win-streak or payout anomaly detection

    # ── Background scanner ────────────────────────────────────────────────────

    @tasks.loop(seconds=_SCAN_INTERVAL)
    async def security_scan(self) -> None:
        """Scan all guilds for suspicious transaction patterns."""
        for guild in self.bot.guilds:
            try:
                await self._scan_guild(guild)
            except Exception:
                pass  # never crash the loop
        pulse("security_scan")

    @security_scan.before_loop
    async def _before_scan(self) -> None:
        await self.bot.wait_until_ready()

    async def _scan_guild(self, guild: discord.Guild) -> None:
        """Analyze recent transactions for one guild."""
        since = time.time() - _LOOKBACK

        # Fetch recent transactions (last 5 minutes)
        rows = await self.bot.db.fetch_all(
            """SELECT user_id, tx_type, symbol_in, symbol_out,
                      amount_in, amount_out, ts
               FROM transactions
               WHERE guild_id = $1 AND ts > to_timestamp($2)
               ORDER BY ts DESC
               LIMIT 500""",
            guild.id, since,
        )
        if not rows:
            return

        # Group by user
        by_user: dict[int, list[dict]] = defaultdict(list)
        for r in rows:
            if r["user_id"]:
                by_user[r["user_id"]].append(r)

        for user_id, txs in by_user.items():
            alerts = self._analyze_user(user_id, guild.id, txs)
            if alerts:
                await self._send_alert(guild, user_id, alerts)

        # Clean up stale whale tracking entries
        now = time.time()
        stale = [k for k, v in _whale_actions.items() if not v or now - v[-1]["ts"] > _LOOKBACK * 2]
        for k in stale:
            _whale_actions.pop(k, None)

    # ── Detection logic ───────────────────────────────────────────────────────

    def _analyze_user(
        self, user_id: int, guild_id: int, txs: list[dict]
    ) -> list[str]:
        """Run all detectors on a user's recent transactions. Returns alert strings."""
        alerts: list[str] = []

        # Categorize transactions
        income_txs = [t for t in txs if t["tx_type"] in (
            "WORK", "DAILY", "MINING", "STAKE_REWARD", "VALIDATOR_REWARD", "LP_YIELD",
        )]
        gamble_txs = [t for t in txs if t["tx_type"].startswith("GAMBLE")]
        trade_txs = [t for t in txs if t["tx_type"] in ("BUY", "SELL", "SWAP")]
        transfer_txs = [t for t in txs if t["tx_type"] in ("TRANSFER", "SEND")]
        lp_txs = [t for t in txs if t["tx_type"] in ("ADD_LP", "REMOVE_LP")]

        # 1. Income velocity
        # `transactions.amount_out` is NUMERIC(36,0) scaled by 10**18 --
        # summing it raw produces "$1,015,941,903,741,947,871,232.00 earned"
        # alerts that look like an exploit but are just the wrong unit.
        # `_h` (core.framework.scale.to_human) converts to real dollars before
        # the sum so the alert text reflects what the player actually got.
        if len(income_txs) > _INCOME_TX_LIMIT:
            total_earned = sum(float(_h(t.get("amount_out") or 0)) for t in income_txs)
            alerts.append(
                f"INCOME_VELOCITY: {len(income_txs)} income transactions in "
                f"{_LOOKBACK // 60}min ({fmt_usd(total_earned)} earned). Types: "
                + ", ".join(set(t["tx_type"] for t in income_txs))
            )

        # 2. Gambling velocity
        if len(gamble_txs) > _GAMBLING_TX_LIMIT:
            total_wagered = sum(float(_h(t.get("amount_in") or 0)) for t in gamble_txs)
            alerts.append(
                f"GAMBLING_VELOCITY: {len(gamble_txs)} games in "
                f"{_LOOKBACK // 60}min ({fmt_usd(total_wagered)} wagered)"
            )

        # 3. Wash trading  -  same token bought and sold repeatedly
        if len(trade_txs) >= _WASH_TRADE_MIN:
            symbols_in: dict[str, int] = defaultdict(int)
            symbols_out: dict[str, int] = defaultdict(int)
            for t in trade_txs:
                if t["symbol_in"]:
                    symbols_in[t["symbol_in"]] += 1
                if t["symbol_out"]:
                    symbols_out[t["symbol_out"]] += 1
            for sym in set(symbols_in) & set(symbols_out):
                cycles = min(symbols_in[sym], symbols_out[sym])
                if cycles >= _WASH_TRADE_MIN // 2:
                    alerts.append(
                        f"WASH_TRADE: {sym} bought {symbols_out[sym]}x "
                        f"and sold {symbols_in[sym]}x in {_LOOKBACK // 60}min"
                    )

        # 4. Transfer volume (potential ring / money laundering)
        if len(transfer_txs) > _TRANSFER_RING_MIN:
            total_moved = sum(float(_h(t.get("amount_out") or 0)) for t in transfer_txs)
            alerts.append(
                f"TRANSFER_VOLUME: {len(transfer_txs)} transfers in "
                f"{_LOOKBACK // 60}min ({fmt_usd(total_moved)} moved)"
            )

        # 5. LP churn
        if len(lp_txs) >= _LP_CHURN_MIN:
            adds = sum(1 for t in lp_txs if t["tx_type"] == "ADD_LP")
            removes = sum(1 for t in lp_txs if t["tx_type"] == "REMOVE_LP")
            if adds >= 2 and removes >= 2:
                alerts.append(
                    f"LP_CHURN: {adds} adds + {removes} removes in "
                    f"{_LOOKBACK // 60}min (possible sandwich / fee extraction)"
                )

        # 6. Overall transaction flood (any type)
        if len(txs) > 80:
            alerts.append(
                f"TX_FLOOD: {len(txs)} total transactions in {_LOOKBACK // 60}min "
                f"(unusually high activity across all features)"
            )

        return alerts

    # ── Alert delivery ────────────────────────────────────────────────────────

    async def _send_alert(
        self, guild: discord.Guild, user_id: int, alerts: list[str]
    ) -> None:
        """DM the REPORT_TARGET_USER_ID with an AI-generated alert summary."""
        # Respect the per-guild security toggle
        try:
            gs = await self.bot.db.get_guild_settings(guild.id)
            if gs and gs.get("module_security") is False:
                return
        except Exception:
            pass

        key = (user_id, guild.id)

        # Cooldown: don't spam for the same user
        if time.time() - _last_alert.get(key, 0) < _ALERT_COOLDOWN:
            return
        _last_alert[key] = time.time()

        # Track repeat offenders
        _flag_counts[key] = _flag_counts.get(key, 0) + 1
        flag_count = _flag_counts[key]

        repeat_note = ""
        if flag_count >= _REPEAT_OFFENDER_LIMIT:
            repeat_note = f"\nREPEAT OFFENDER: flagged {flag_count} times this session."

        # Include whale history if any
        whale_note = ""
        whale_entries = _whale_actions.get(key, [])
        if whale_entries:
            total_whale = sum(w["usd_value"] for w in whale_entries)
            whale_note = (
                f"\nWhale alert history: {len(whale_entries)} large transactions "
                f"totalling {fmt_usd(total_whale)} in the current window."
            )

        # Build context for AI
        alert_text = "\n".join(f"- {a}" for a in alerts)
        context = (
            f"Player: <@{user_id}> (ID: {user_id})\n"
            f"Server: {guild.name}\n"
            f"Detections:\n{alert_text}"
            f"{whale_note}"
            f"{repeat_note}"
        )

        # Generate AI summary
        summary = None
        try:
            summary = await ai_complete(
                [
                    {"role": "system", "content": _ALERT_SYSTEM},
                    {"role": "user", "content": context},
                ],
                max_tokens=150,
                temperature=0.7,
            )
        except Exception:
            pass

        if not summary:
            summary = (
                f"Suspicious activity detected for user {user_id} in {guild.name}:\n"
                f"{alert_text}{whale_note}{repeat_note}"
            )

        # Send DM to report target
        target_id = Config.REPORT_TARGET_USER_ID
        if not target_id:
            return

        try:
            target = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
            _b = (
                card("Economy Security Alert", description=summary.strip(), color=C_WARNING)
                .field("Player", f"<@{user_id}> (`{user_id}`)", True)
                .field("Server", f"{guild.name} (`{guild.id}`)", True)
            )
            if flag_count > 1:
                _b.field(
                    "Flags This Session",
                    f"**{flag_count}**" + (
                        " -- REPEAT" if flag_count >= _REPEAT_OFFENDER_LIMIT else ""
                    ),
                    True,
                )
            _b.field("Raw Detections", alert_text[:1024], False)
            if whale_note:
                _b.field("Whale Activity", whale_note.strip()[:1024], False)
            embed = _b.footer("Economy Security Monitor | observation only, no action taken").build()
            await target.send(embed=embed)
        except Exception:
            pass

        # Publish to bus so dashboard / other systems can react
        try:
            await self.bot.bus.publish(
                "security_alert",
                guild=guild,
                user_id=user_id,
                alerts=alerts,
                flag_count=flag_count,
            )
        except Exception:
            pass

        # Feed into the unified security engine (if available)
        engine = getattr(self.bot, "security_engine", None)
        if engine and engine.is_running:
            try:
                from security.models import SecurityEvent, EventSource
                for alert_text in alerts:
                    # Determine event type from alert prefix
                    event_type = "economy_abuse"
                    if "INCOME" in alert_text:
                        event_type = "earn"
                    elif "GAMBLING" in alert_text:
                        event_type = "gamble"
                    elif "WASH" in alert_text:
                        event_type = "trade"
                    elif "TRANSFER" in alert_text:
                        event_type = "transfer"
                    elif "LP" in alert_text:
                        event_type = "pool"
                    elif "WHALE" in alert_text:
                        event_type = "trade"

                    event = SecurityEvent(
                        guild_id=guild.id,
                        user_id=user_id,
                        event_type=event_type,
                        source=EventSource.BOT,
                        details={"legacy_alert": alert_text, "flag_count": flag_count},
                    )
                    await engine.process_event(event)
            except Exception:
                pass


async def setup(bot: Discoin) -> None:
    await bot.add_cog(EconomySecurity(bot))
