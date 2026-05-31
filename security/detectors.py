"""
security/detectors.py  -  Threat detection functions.

Each detector receives the SecurityEvent plus cached context (recent events,
profile, correlation data) and returns a list of ThreatDetection objects
(empty list = no threat detected).

Detectors are pure analysis  -  they do NOT enforce anything.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from security.config import (
    LOOKBACK_SECONDS,
    INCOME_VELOCITY_LIMIT,
    GAMBLING_VELOCITY_LIMIT,
    WASH_TRADE_MIN_CYCLES,
    TRANSFER_RING_MIN,
    LP_CHURN_MIN,
    TX_FLOOD_LIMIT,
    AUTH_FAILURE_LIMIT,
    SESSION_IP_CHANGE_WINDOW,
    API_REQUEST_FLOOD_LIMIT,
    COMMAND_FLOOD_LIMIT,
    IDENTICAL_COMMAND_LIMIT,
    CORRELATION_EVENT_MIN,
    FLASH_LOAN_WINDOW,
    ORACLE_MANIPULATION_TRADES,
    WHALE_CONCENTRATION_LIMIT,
    SCORE_WEIGHTS,
)
from security.models import (
    SecurityEvent,
    ThreatDetection,
    Severity,
)
from security.redis_cache import SecurityRedisCache

log = logging.getLogger("discoin.security.detectors")

# tx_type classifications
_INCOME_TYPES = {"WORK", "DAILY", "MINING", "STAKE_REWARD", "VALIDATOR_REWARD",
                 "MINE_REWARD", "SAVINGS_INTEREST", "LP_YIELD"}
_GAMBLE_TYPES_PREFIX = "GAMBLE"
_TRADE_TYPES = {"BUY", "SELL", "SWAP"}
_TRANSFER_TYPES = {"TRANSFER", "SEND", "token_send"}
_LP_TYPES = {"ADD_LP", "REMOVE_LP", "LP_ADD", "LP_REMOVE", "lp_added", "lp_removed"}
_LOAN_TYPES = {"LOAN_BORROW", "LOAN_REPAY", "LENDING_BORROW", "LENDING_REPAY"}


class ThreatDetectors:
    """Collection of all threat detection logic."""

    def __init__(self, cache: SecurityRedisCache) -> None:
        self.cache = cache

    async def run_all(
        self,
        event: SecurityEvent,
        recent_events: list[dict],
        profile: dict | None,
        correlation: dict,
    ) -> list[ThreatDetection]:
        """Run all applicable detectors and collect results."""
        detections: list[ThreatDetection] = []

        # Economy detectors  -  run on financial events
        if event.tx_type or event.event_type in ("trade", "transfer", "gamble", "earn",
                                                   "stake", "pool", "loan", "mine"):
            detections.extend(self._detect_income_velocity(recent_events))
            detections.extend(self._detect_gambling_abuse(recent_events))
            detections.extend(self._detect_wash_trading(recent_events))
            detections.extend(self._detect_transfer_rings(recent_events))
            detections.extend(self._detect_lp_manipulation(recent_events))
            detections.extend(self._detect_tx_flood(recent_events))
            detections.extend(self._detect_defi_exploit(recent_events))

        # API detectors
        if event.source.value == "api":
            detections.extend(await self._detect_api_abuse(event))
            detections.extend(await self._detect_session_anomaly(event))

        # Bot detectors
        if event.source.value == "bot" and event.command:
            detections.extend(await self._detect_command_flood(event))

        # Cross-platform detector
        detections.extend(self._detect_cross_platform_abuse(event, correlation))

        # Privilege escalation  -  always check
        detections.extend(self._detect_privilege_escalation(event))

        # Transaction integrity  -  on financial events
        if event.amount_usd is not None:
            detections.extend(self._detect_transaction_integrity(event, recent_events))

        # Whale concentration
        detections.extend(self._detect_whale_concentration(recent_events))

        return detections

    # ── Economy Detectors ────────────────────────────────────────────────────

    def _detect_income_velocity(self, recent_events: list[dict]) -> list[ThreatDetection]:
        income_events = [e for e in recent_events
                         if e.get("tx_type", "") in _INCOME_TYPES]
        if len(income_events) <= INCOME_VELOCITY_LIMIT:
            return []

        total_earned = sum(float(e.get("amount_usd", 0) or 0) for e in income_events)
        types_seen = set(e.get("tx_type", "") for e in income_events)

        return [ThreatDetection(
            detector="income_velocity",
            severity=Severity.MEDIUM if len(income_events) < INCOME_VELOCITY_LIMIT * 2 else Severity.HIGH,
            score_delta=SCORE_WEIGHTS["income_velocity"],
            description=(
                f"Abnormal income velocity: {len(income_events)} income transactions "
                f"in {LOOKBACK_SECONDS // 60}min (${total_earned:,.2f} earned). "
                f"Types: {', '.join(types_seen)}"
            ),
            details={
                "count": len(income_events),
                "total_earned": total_earned,
                "types": list(types_seen),
                "limit": INCOME_VELOCITY_LIMIT,
            },
        )]

    def _detect_gambling_abuse(self, recent_events: list[dict]) -> list[ThreatDetection]:
        gamble_events = [e for e in recent_events
                         if (e.get("tx_type", "") or "").startswith(_GAMBLE_TYPES_PREFIX)
                         or e.get("event_type") == "gamble"]
        if len(gamble_events) <= GAMBLING_VELOCITY_LIMIT:
            return []

        total_wagered = sum(float(e.get("amount_usd", 0) or 0) for e in gamble_events)

        return [ThreatDetection(
            detector="gambling_abuse",
            severity=Severity.MEDIUM if len(gamble_events) < GAMBLING_VELOCITY_LIMIT * 2 else Severity.HIGH,
            score_delta=SCORE_WEIGHTS["gambling_abuse"],
            description=(
                f"Inhuman gambling velocity: {len(gamble_events)} games in "
                f"{LOOKBACK_SECONDS // 60}min (${total_wagered:,.2f} wagered)"
            ),
            details={
                "count": len(gamble_events),
                "total_wagered": total_wagered,
                "limit": GAMBLING_VELOCITY_LIMIT,
            },
        )]

    def _detect_wash_trading(self, recent_events: list[dict]) -> list[ThreatDetection]:
        trade_events = [e for e in recent_events
                        if e.get("tx_type", "") in _TRADE_TYPES
                        or e.get("event_type") == "trade"]
        if len(trade_events) < WASH_TRADE_MIN_CYCLES:
            return []

        buys: dict[str, int] = defaultdict(int)
        sells: dict[str, int] = defaultdict(int)
        for t in trade_events:
            tx_type = t.get("tx_type", "")
            symbol = t.get("symbol", "") or t.get("symbol_in", "") or t.get("symbol_out", "")
            if not symbol:
                continue
            if tx_type == "BUY":
                buys[symbol] += 1
            elif tx_type == "SELL":
                sells[symbol] += 1
            elif tx_type == "SWAP":
                # Count swaps as both buy and sell of different tokens
                sym_in = t.get("symbol_in", "")
                sym_out = t.get("symbol_out", "")
                if sym_in:
                    sells[sym_in] += 1
                if sym_out:
                    buys[sym_out] += 1

        detections: list[ThreatDetection] = []
        for sym in set(buys) & set(sells):
            cycles = min(buys[sym], sells[sym])
            if cycles >= WASH_TRADE_MIN_CYCLES // 2:
                detections.append(ThreatDetection(
                    detector="wash_trading",
                    severity=Severity.HIGH,
                    score_delta=SCORE_WEIGHTS["wash_trading"],
                    description=(
                        f"Wash trading detected: {sym} bought {buys[sym]}x "
                        f"and sold {sells[sym]}x in {LOOKBACK_SECONDS // 60}min"
                    ),
                    details={
                        "symbol": sym,
                        "buys": buys[sym],
                        "sells": sells[sym],
                        "cycles": cycles,
                    },
                ))
        return detections

    def _detect_transfer_rings(self, recent_events: list[dict]) -> list[ThreatDetection]:
        transfer_events = [e for e in recent_events
                           if e.get("tx_type", "") in _TRANSFER_TYPES
                           or e.get("event_type") == "transfer"]
        if len(transfer_events) <= TRANSFER_RING_MIN:
            return []

        total_moved = sum(float(e.get("amount_usd", 0) or 0) for e in transfer_events)
        # Check for recipient patterns
        recipients: dict[str, int] = defaultdict(int)
        for t in transfer_events:
            to_user = t.get("to_user_id") or t.get("details", {}).get("to_user_id")
            if to_user:
                recipients[str(to_user)] += 1

        return [ThreatDetection(
            detector="transfer_rings",
            severity=Severity.HIGH if total_moved > 50000 else Severity.MEDIUM,
            score_delta=SCORE_WEIGHTS["transfer_rings"],
            description=(
                f"Rapid fund movement: {len(transfer_events)} transfers in "
                f"{LOOKBACK_SECONDS // 60}min (${total_moved:,.2f} moved)"
            ),
            details={
                "count": len(transfer_events),
                "total_moved": total_moved,
                "unique_recipients": len(recipients),
                "recipient_counts": dict(recipients),
            },
        )]

    def _detect_lp_manipulation(self, recent_events: list[dict]) -> list[ThreatDetection]:
        lp_events = [e for e in recent_events
                     if e.get("tx_type", "") in _LP_TYPES
                     or e.get("event_type") in ("pool", "lp_added", "lp_removed")]
        if len(lp_events) < LP_CHURN_MIN:
            return []

        adds = sum(1 for e in lp_events
                   if e.get("tx_type", "") in ("ADD_LP", "LP_ADD", "lp_added")
                   or e.get("event_type") == "lp_added")
        removes = sum(1 for e in lp_events
                      if e.get("tx_type", "") in ("REMOVE_LP", "LP_REMOVE", "lp_removed")
                      or e.get("event_type") == "lp_removed")

        if adds < 2 or removes < 2:
            return []

        return [ThreatDetection(
            detector="lp_manipulation",
            severity=Severity.HIGH,
            score_delta=SCORE_WEIGHTS["lp_manipulation"],
            description=(
                f"LP churn detected: {adds} adds + {removes} removes in "
                f"{LOOKBACK_SECONDS // 60}min (possible sandwich / fee extraction)"
            ),
            details={"adds": adds, "removes": removes, "total": len(lp_events)},
        )]

    def _detect_tx_flood(self, recent_events: list[dict]) -> list[ThreatDetection]:
        if len(recent_events) <= TX_FLOOD_LIMIT:
            return []

        return [ThreatDetection(
            detector="tx_flood",
            severity=Severity.MEDIUM,
            score_delta=SCORE_WEIGHTS["tx_flood"],
            description=(
                f"Transaction flood: {len(recent_events)} total events in "
                f"{LOOKBACK_SECONDS // 60}min"
            ),
            details={"count": len(recent_events), "limit": TX_FLOOD_LIMIT},
        )]

    # ── DeFi Exploit Detectors ───────────────────────────────────────────────

    def _detect_defi_exploit(self, recent_events: list[dict]) -> list[ThreatDetection]:
        detections: list[ThreatDetection] = []

        # Flash-loan pattern: borrow → large trade → repay in quick succession
        loan_borrows = [e for e in recent_events if e.get("tx_type", "") in ("LOAN_BORROW", "LENDING_BORROW")]
        loan_repays = [e for e in recent_events if e.get("tx_type", "") in ("LOAN_REPAY", "LENDING_REPAY")]
        trades = [e for e in recent_events if e.get("tx_type", "") in _TRADE_TYPES]

        for borrow in loan_borrows:
            borrow_ts = float(borrow.get("timestamp", 0))
            # Find a trade and repay within the flash loan window
            has_trade = any(
                abs(float(t.get("timestamp", 0)) - borrow_ts) < FLASH_LOAN_WINDOW
                for t in trades
            )
            has_repay = any(
                0 < float(r.get("timestamp", 0)) - borrow_ts < FLASH_LOAN_WINDOW
                for r in loan_repays
            )
            if has_trade and has_repay:
                detections.append(ThreatDetection(
                    detector="defi_exploit",
                    severity=Severity.CRITICAL,
                    score_delta=SCORE_WEIGHTS["defi_exploit"],
                    description=(
                        f"Flash-loan pattern: borrow → trade → repay within "
                        f"{FLASH_LOAN_WINDOW}s window"
                    ),
                    details={"pattern": "flash_loan", "window": FLASH_LOAN_WINDOW},
                ))
                break  # One detection per scan is enough

        # Oracle manipulation: rapid same-token trades to move price
        if len(trades) >= ORACLE_MANIPULATION_TRADES:
            symbol_counts: dict[str, int] = defaultdict(int)
            for t in trades:
                sym = t.get("symbol", "") or t.get("symbol_in", "") or t.get("symbol_out", "")
                if sym:
                    symbol_counts[sym] += 1
            for sym, count in symbol_counts.items():
                if count >= ORACLE_MANIPULATION_TRADES:
                    detections.append(ThreatDetection(
                        detector="defi_exploit",
                        severity=Severity.HIGH,
                        score_delta=SCORE_WEIGHTS["defi_exploit"],
                        description=(
                            f"Possible oracle manipulation: {count} trades of {sym} "
                            f"in {LOOKBACK_SECONDS // 60}min"
                        ),
                        details={"pattern": "oracle_manipulation", "symbol": sym, "count": count},
                    ))

        return detections

    # ── API Detectors ────────────────────────────────────────────────────────

    async def _detect_api_abuse(self, event: SecurityEvent) -> list[ThreatDetection]:
        detections: list[ThreatDetection] = []

        # Auth failure tracking (brute force / credential stuffing)
        if event.event_type == "auth_failure" and event.ip_address:
            count = await self.cache.record_auth_failure(event.ip_address)
            if count > AUTH_FAILURE_LIMIT:
                detections.append(ThreatDetection(
                    detector="api_abuse",
                    severity=Severity.HIGH,
                    score_delta=SCORE_WEIGHTS["api_abuse"],
                    description=(
                        f"Brute force detected: {count} auth failures from "
                        f"IP {event.ip_address} in 5min"
                    ),
                    details={
                        "pattern": "brute_force",
                        "ip": event.ip_address,
                        "failure_count": count,
                    },
                ))

        # API request flood (per user)
        if event.endpoint:
            req_count = await self.cache.record_api_request(
                event.guild_id, event.user_id, event.endpoint,
            )
            if req_count > API_REQUEST_FLOOD_LIMIT:
                detections.append(ThreatDetection(
                    detector="api_abuse",
                    severity=Severity.MEDIUM,
                    score_delta=SCORE_WEIGHTS["api_abuse"] * 0.5,
                    description=(
                        f"API flood: {req_count} requests in 60s from user {event.user_id}"
                    ),
                    details={
                        "pattern": "api_flood",
                        "request_count": req_count,
                        "endpoint": event.endpoint,
                    },
                ))

        return detections

    async def _detect_session_anomaly(self, event: SecurityEvent) -> list[ThreatDetection]:
        """Detect suspicious session behavior: IP changes, fingerprint drift."""
        if not event.ip_address:
            return []

        detections: list[ThreatDetection] = []

        # Check profile for known IPs
        profile = await self.cache.get_profile(event.guild_id, event.user_id)
        if profile:
            known_ips = profile.get("known_ips", [])
            if known_ips and event.ip_address not in known_ips:
                # New IP  -  check if there was activity from a different IP very recently
                recent = await self.cache.get_recent_events(
                    event.guild_id, event.user_id, window_seconds=SESSION_IP_CHANGE_WINDOW,
                )
                recent_ips = set(e.get("ip_address") for e in recent if e.get("ip_address"))
                recent_ips.discard(event.ip_address)

                if recent_ips:
                    detections.append(ThreatDetection(
                        detector="session_anomaly",
                        severity=Severity.MEDIUM,
                        score_delta=SCORE_WEIGHTS["session_anomaly"],
                        description=(
                            f"IP change mid-session: {', '.join(recent_ips)} → "
                            f"{event.ip_address} within {SESSION_IP_CHANGE_WINDOW}s"
                        ),
                        details={
                            "pattern": "ip_change",
                            "previous_ips": list(recent_ips),
                            "new_ip": event.ip_address,
                        },
                    ))

        # User agent fingerprint check
        if event.user_agent:
            token_hash = event.details.get("token_hash", "")
            if token_hash:
                stored_fp = await self.cache.get_session_fingerprint(token_hash)
                if stored_fp and stored_fp.get("user_agent") != event.user_agent:
                    detections.append(ThreatDetection(
                        detector="session_anomaly",
                        severity=Severity.HIGH,
                        score_delta=SCORE_WEIGHTS["session_anomaly"] * 1.5,
                        description=(
                            f"Session fingerprint mismatch: user agent changed "
                            f"for token {token_hash[:8]}..."
                        ),
                        details={
                            "pattern": "fingerprint_drift",
                            "stored_ua": stored_fp.get("user_agent", "")[:80],
                            "current_ua": event.user_agent[:80],
                        },
                    ))

        return detections

    # ── Bot Detectors ────────────────────────────────────────────────────────

    async def _detect_command_flood(self, event: SecurityEvent) -> list[ThreatDetection]:
        detections: list[ThreatDetection] = []

        total_cmds, identical_cmds = await self.cache.record_command(
            event.guild_id, event.user_id, event.command or "",
        )

        if total_cmds > COMMAND_FLOOD_LIMIT:
            detections.append(ThreatDetection(
                detector="command_flood",
                severity=Severity.MEDIUM,
                score_delta=SCORE_WEIGHTS["command_flood"],
                description=(
                    f"Command flood: {total_cmds} commands in 60s"
                ),
                details={
                    "total_commands": total_cmds,
                    "limit": COMMAND_FLOOD_LIMIT,
                },
            ))

        if identical_cmds > IDENTICAL_COMMAND_LIMIT:
            detections.append(ThreatDetection(
                detector="command_flood",
                severity=Severity.MEDIUM,
                score_delta=SCORE_WEIGHTS["command_flood"] * 0.8,
                description=(
                    f"Macro-like pattern: '{event.command}' repeated {identical_cmds}x in 60s"
                ),
                details={
                    "pattern": "identical_command",
                    "command": event.command,
                    "count": identical_cmds,
                    "limit": IDENTICAL_COMMAND_LIMIT,
                },
            ))

        return detections

    # ── Cross-Platform Detector ──────────────────────────────────────────────

    def _detect_cross_platform_abuse(
        self, event: SecurityEvent, correlation: dict,
    ) -> list[ThreatDetection]:
        bot_events = correlation.get("bot_events", 0)
        api_events = correlation.get("api_events", 0)
        total = bot_events + api_events

        if total < CORRELATION_EVENT_MIN:
            return []
        if bot_events == 0 or api_events == 0:
            return []  # Only flag if BOTH platforms are active

        return [ThreatDetection(
            detector="cross_platform_abuse",
            severity=Severity.HIGH,
            score_delta=SCORE_WEIGHTS["cross_platform_abuse"],
            description=(
                f"Suspicious cross-platform activity: {bot_events} bot events + "
                f"{api_events} API events in 5min window"
            ),
            details={
                "bot_events": bot_events,
                "api_events": api_events,
                "total": total,
            },
        )]

    # ── Privilege Escalation ─────────────────────────────────────────────────

    def _detect_privilege_escalation(self, event: SecurityEvent) -> list[ThreatDetection]:
        """Detect attempts to access admin endpoints or forge permissions."""
        details = event.details

        # Check for admin endpoint access without admin status
        is_admin_endpoint = (
            event.endpoint and "/admin/" in event.endpoint
        )
        is_admin = details.get("is_admin", False)

        if is_admin_endpoint and not is_admin:
            return [ThreatDetection(
                detector="privilege_escalation",
                severity=Severity.CRITICAL,
                score_delta=SCORE_WEIGHTS["privilege_escalation"],
                description=(
                    f"Unauthorized admin access attempt: {event.endpoint}"
                ),
                details={
                    "endpoint": event.endpoint,
                    "method": details.get("method", ""),
                },
            )]

        # Check for guild_id mismatch (forged claims)
        claimed_guild = details.get("claimed_guild_id")
        actual_guild = details.get("actual_guild_id")
        if claimed_guild and actual_guild and str(claimed_guild) != str(actual_guild):
            return [ThreatDetection(
                detector="privilege_escalation",
                severity=Severity.CRITICAL,
                score_delta=SCORE_WEIGHTS["privilege_escalation"],
                description=(
                    f"Guild ID mismatch: claimed {claimed_guild}, actual {actual_guild}"
                ),
                details={
                    "claimed_guild_id": claimed_guild,
                    "actual_guild_id": actual_guild,
                },
            )]

        return []

    # ── Transaction Integrity ────────────────────────────────────────────────

    def _detect_transaction_integrity(
        self, event: SecurityEvent, recent_events: list[dict],
    ) -> list[ThreatDetection]:
        """Detect potential double-spend or balance manipulation patterns."""
        detections: list[ThreatDetection] = []

        # Check for rapid identical transactions (same type, same amount)
        if event.amount_usd and event.tx_type:
            identical_recent = [
                e for e in recent_events
                if (e.get("tx_type") == event.tx_type
                    and abs(float(e.get("amount_usd", 0) or 0) - event.amount_usd) < 0.01
                    and abs(float(e.get("timestamp", 0)) - event.timestamp) < 2.0)  # within 2 seconds
            ]
            if len(identical_recent) >= 3:
                detections.append(ThreatDetection(
                    detector="transaction_integrity",
                    severity=Severity.CRITICAL,
                    score_delta=SCORE_WEIGHTS["transaction_integrity"],
                    description=(
                        f"Possible double-spend: {len(identical_recent)} identical "
                        f"{event.tx_type} transactions of ${event.amount_usd:,.2f} "
                        f"within 2 seconds"
                    ),
                    details={
                        "tx_type": event.tx_type,
                        "amount": event.amount_usd,
                        "identical_count": len(identical_recent),
                    },
                ))

        # Check for negative amount attempts (should be caught by DB constraints,
        # but flagging the attempt itself is valuable)
        if event.amount_usd is not None and event.amount_usd < 0:
            detections.append(ThreatDetection(
                detector="transaction_integrity",
                severity=Severity.CRITICAL,
                score_delta=SCORE_WEIGHTS["transaction_integrity"],
                description="Negative amount in transaction  -  potential exploit attempt",
                details={"amount": event.amount_usd, "tx_type": event.tx_type},
            ))

        return detections

    # ── Whale Concentration ──────────────────────────────────────────────────

    def _detect_whale_concentration(self, recent_events: list[dict]) -> list[ThreatDetection]:
        """Detect whale-sized action concentration."""
        whale_events = [
            e for e in recent_events
            if float(e.get("amount_usd", 0) or 0) >= 50000  # matches WHALE_ALERT_THRESHOLD_USD
        ]
        if len(whale_events) < WHALE_CONCENTRATION_LIMIT:
            return []

        total_usd = sum(float(e.get("amount_usd", 0) or 0) for e in whale_events)
        actions = set(e.get("tx_type", "") or e.get("event_type", "") for e in whale_events)

        return [ThreatDetection(
            detector="whale_concentration",
            severity=Severity.HIGH,
            score_delta=SCORE_WEIGHTS.get("whale_concentration", 15.0),
            description=(
                f"Whale concentration: {len(whale_events)} large transactions "
                f"totalling ${total_usd:,.2f} in {LOOKBACK_SECONDS // 60}min"
            ),
            details={
                "count": len(whale_events),
                "total_usd": total_usd,
                "actions": list(actions),
            },
        )]
