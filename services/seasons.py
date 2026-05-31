"""services/seasons.py - guild-scoped season lifecycle + season pass.

A season is a bounded competition window with a prize pool. At start,
the guild has a single active row in ``seasons``. At end (manual or when
``ends_at`` passes), the service snapshots the leaderboard, pays out
rewards to the top N, and writes one ``season_entries`` row per payee.

The season pass rides on top of an active season. Bus events grant XP
into ``season_xp`` scoped by season_id; tier claims land in
``season_tier_claims``. Tier math + XP source weights live in
seasonpass_config.py (single source of truth).

Metrics
-------
Four leaderboard metrics are supported. Three of them measure activity
during the season (volume, trades, pass_xp) and are ranged by
``season.started_at``. ``net_worth`` is a pure snapshot of current
wealth at evaluation time.

    ``net_worth`` (USD)  - services/net_worth.compute_bulk_net_worth.
    ``volume``    (USD)  - sum of USD-denominated trade amounts in
                           range. Token-to-token swaps without a USD
                           leg contribute zero.
    ``trades``    (#)    - count of BUY/SELL/SWAP/ADDLP/REMOVELP
                           transactions in range.
    ``pass_xp``   (XP)   - season_xp.xp directly. Rewards grinders.

Adding a metric: implement ``_metric_<name>``, register it in
``METRICS``, and widen the CHECK constraint via a migration.

Public API
----------
Lifecycle:
    ``start``, ``get_active``, ``get_season``, ``entries``,
    ``end_season``, ``check_expired``, ``fetch_standings``.
Season pass:
    ``grant_xp``, ``get_xp``, ``claim_tier``, ``claimed_tiers``,
    ``top_xp``, ``total_pass_payout``, ``attach_pass_listeners``.
Metrics:
    ``METRICS``, ``metric_label``, ``metric_unit``, ``format_metric``.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

from core.framework.scale import to_human, to_raw
from core.framework.ui import FormatKit
from services import net_worth as _nw

import configs.seasonpass_config as _pass_cfg

log = logging.getLogger(__name__)


def _multipliers(season: dict | None) -> dict[str, float]:
    """Decode the active season's xp_multipliers field as a dict.

    asyncpg may return JSONB as a decoded dict or as a JSON string
    depending on codec registration; this normalizes both.
    """
    if season is None:
        return {}
    raw = season.get("xp_multipliers") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): float(v) for k, v in raw.items()}


# ── Metric registry ──────────────────────────────────────────────────────────

METRICS: tuple[str, ...] = ("net_worth", "volume", "trades", "pass_xp", "buddy_wins")

_METRIC_LABELS: dict[str, str] = {
    "net_worth":  "Net Worth",
    "volume":     "Trade Volume",
    "trades":     "Trade Count",
    "pass_xp":    "Season Pass XP",
    "buddy_wins": "Buddy Battle Wins",
}

_METRIC_UNITS: dict[str, str] = {
    "net_worth":  "usd",
    "volume":     "usd",
    "trades":     "count",
    "pass_xp":    "xp",
    "buddy_wins": "wins",
}


def metric_description(metric: str) -> str:
    """One-line explanation of what a metric measures. Used by ,season help."""
    return {
        "net_worth":  "Current total net worth (wallet + bank + positions).",
        "volume":     "USD volume traded since the season started.",
        "trades":     "Number of trades executed since the season started.",
        "pass_xp":    "Season pass XP earned during this season.",
        "buddy_wins": "Buddy battles won during this season.",
    }.get(metric, "")


def metric_label(metric: str) -> str:
    """Human-readable label for a metric key ('net_worth' -> 'Net Worth')."""
    return _METRIC_LABELS.get(metric, metric.replace("_", " ").title())


def metric_unit(metric: str) -> str:
    """Unit tag: 'usd', 'count', or 'xp'. Drives format_metric()."""
    return _METRIC_UNITS.get(metric, "count")


def format_metric(metric: str, value: float) -> str:
    """Render ``value`` with the unit-appropriate formatter."""
    unit = metric_unit(metric)
    if unit == "usd":
        return FormatKit.usd(float(value))
    if unit == "xp":
        return f"{int(value):,} XP"
    if unit == "wins":
        return f"{int(value):,} wins"
    return f"{int(value):,}"


# ── Reward distribution ──────────────────────────────────────────────────────
#
# Top N ranks get a cut of the prize pool in a simple geometric falloff:
# 1st gets ~40%, 2nd ~24%, 3rd ~14%, 4th ~9%, 5th ~5%, then a long tail.
# Normalized to sum to 1.0 so the pool is fully paid out regardless of N.
_PAYOUT_WEIGHTS: tuple[float, ...] = (
    0.40, 0.24, 0.14, 0.09, 0.06, 0.03, 0.02, 0.01, 0.006, 0.004,
)
MAX_PAYOUT_RANKS: int = len(_PAYOUT_WEIGHTS)


def _compute_rewards(prize_pool_usd: float, n_ranked: int) -> list[float]:
    """Return a list of reward amounts for ranks 1..min(n_ranked, MAX_PAYOUT_RANKS)."""
    if prize_pool_usd <= 0 or n_ranked <= 0:
        return []
    take = min(n_ranked, MAX_PAYOUT_RANKS)
    weights = list(_PAYOUT_WEIGHTS[:take])
    total_w = sum(weights)
    if total_w <= 0:
        return [0.0] * take
    return [prize_pool_usd * (w / total_w) for w in weights]


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def start(
    db, guild_id: int, name: str, metric: str,
    prize_pool_usd: float, duration_days: int,
    theme: str = "classic",
) -> dict | None:
    """Create an active season for ``guild_id``.

    Returns the new season row, or ``None`` if one is already active
    (the caller is expected to end the existing season first). ``theme``
    stores a label on the season and copies its multiplier map from
    seasonpass_config.THEMES into xp_multipliers so grant_xp can read
    it directly.
    """
    if await get_active(db, guild_id) is not None:
        return None
    ends_at = _dt.datetime.utcnow() + _dt.timedelta(days=int(duration_days))
    multipliers = _pass_cfg.theme_multipliers(theme)
    row = await db.fetch_one(
        """
        INSERT INTO seasons
            (guild_id, name, metric, prize_pool_usd, ends_at,
             theme, xp_multipliers)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        RETURNING season_id, guild_id, name, metric, prize_pool_usd,
                  started_at, ends_at, finalized_at, status,
                  theme, xp_multipliers
        """,
        guild_id, name, metric, float(prize_pool_usd), ends_at,
        theme, json.dumps(multipliers),
    )
    return row


async def set_theme(db, guild_id: int, theme: str) -> dict | None:
    """Swap the active season's theme (and multipliers). Returns the
    updated row, or ``None`` if no active season exists. Does NOT reset
    XP already earned -- the multiplier only applies to future grants.
    """
    active = await get_active(db, guild_id)
    if active is None:
        return None
    multipliers = _pass_cfg.theme_multipliers(theme)
    return await db.fetch_one(
        """
        UPDATE seasons
           SET theme = $1, xp_multipliers = $2::jsonb
         WHERE season_id = $3
         RETURNING season_id, guild_id, name, metric, prize_pool_usd,
                   started_at, ends_at, finalized_at, status,
                   theme, xp_multipliers
        """,
        theme, json.dumps(multipliers), int(active["season_id"]),
    )


async def get_active(db, guild_id: int) -> dict | None:
    return await db.fetch_one(
        """
        SELECT season_id, guild_id, name, metric, prize_pool_usd,
               started_at, ends_at, finalized_at, status,
               theme, xp_multipliers
        FROM seasons
        WHERE guild_id = $1 AND status = 'active'
        """,
        guild_id,
    )


async def get_season(db, season_id: int) -> dict | None:
    return await db.fetch_one(
        """
        SELECT season_id, guild_id, name, metric, prize_pool_usd,
               started_at, ends_at, finalized_at, status,
               theme, xp_multipliers
        FROM seasons WHERE season_id = $1
        """,
        season_id,
    )


async def entries(db, season_id: int, limit: int = 10) -> list[dict]:
    """Return the top ``limit`` entries for a finalized season."""
    return await db.fetch_all(
        """
        SELECT user_id, final_rank, metric_value, reward_usd
        FROM season_entries
        WHERE season_id = $1
        ORDER BY final_rank
        LIMIT $2
        """,
        season_id, int(limit),
    )


# ── Finalization ─────────────────────────────────────────────────────────────

_TRADE_TX_TYPES: tuple[str, ...] = (
    "BUY", "SELL", "SWAP", "ADDLP", "REMOVELP",
)


async def _metric_net_worth(db, season: dict) -> list[tuple[int, float]]:
    values = await _nw.compute_bulk_net_worth(int(season["guild_id"]), db)
    return sorted(values.items(), key=lambda kv: kv[1], reverse=True)


async def _metric_volume(db, season: dict) -> list[tuple[int, float]]:
    """Sum USD-side trade amounts per user since season.started_at.

    amount_in when symbol_in='USD' (buys); amount_out when symbol_out='USD'
    (sells). Token-to-token swaps with no USD leg score 0.
    """
    rows = await db.fetch_all(
        f"""
        SELECT user_id,
               SUM(CASE
                     WHEN symbol_in  = 'USD' THEN amount_in
                     WHEN symbol_out = 'USD' THEN amount_out
                     ELSE 0
                   END) AS vol_raw
        FROM transactions
        WHERE guild_id = $1
          AND user_id IS NOT NULL
          AND ts >= to_timestamp($2)
          AND tx_type IN ({','.join(f"'{t}'" for t in _TRADE_TX_TYPES)})
        GROUP BY user_id
        HAVING SUM(CASE
                     WHEN symbol_in  = 'USD' THEN amount_in
                     WHEN symbol_out = 'USD' THEN amount_out
                     ELSE 0
                   END) > 0
        ORDER BY vol_raw DESC
        """,
        int(season["guild_id"]), float(season["started_at"]),
    )
    return [
        (int(r["user_id"]), float(to_human(int(r["vol_raw"] or 0))))
        for r in (rows or [])
    ]


async def _metric_trades(db, season: dict) -> list[tuple[int, float]]:
    rows = await db.fetch_all(
        f"""
        SELECT user_id, COUNT(*) AS n
        FROM transactions
        WHERE guild_id = $1
          AND user_id IS NOT NULL
          AND ts >= to_timestamp($2)
          AND tx_type IN ({','.join(f"'{t}'" for t in _TRADE_TX_TYPES)})
        GROUP BY user_id
        ORDER BY n DESC
        """,
        int(season["guild_id"]), float(season["started_at"]),
    )
    return [(int(r["user_id"]), float(r["n"])) for r in (rows or [])]


async def _metric_pass_xp(db, season: dict) -> list[tuple[int, float]]:
    rows = await db.fetch_all(
        """
        SELECT user_id, xp
        FROM season_xp
        WHERE season_id = $1
        ORDER BY xp DESC
        """,
        int(season["season_id"]),
    )
    return [(int(r["user_id"]), float(r["xp"])) for r in (rows or [])]


async def _metric_counter(
    db, season: dict, counter: str,
) -> list[tuple[int, float]]:
    """Generic loader for any season_counters-backed metric."""
    rows = await db.fetch_all(
        """
        SELECT user_id, value
        FROM season_counters
        WHERE season_id = $1 AND counter = $2
        ORDER BY value DESC
        """,
        int(season["season_id"]), counter,
    )
    return [(int(r["user_id"]), float(r["value"])) for r in (rows or [])]


async def _metric_buddy_wins(db, season: dict) -> list[tuple[int, float]]:
    return await _metric_counter(db, season, "buddy_wins")


_METRIC_FNS = {
    "net_worth":  _metric_net_worth,
    "volume":     _metric_volume,
    "trades":     _metric_trades,
    "pass_xp":    _metric_pass_xp,
    "buddy_wins": _metric_buddy_wins,
}


async def fetch_standings(
    db, season: dict, limit: int | None = None,
) -> list[tuple[int, float]]:
    """Dispatch the season's metric to the right query and return a sorted
    ``[(user_id, value)]`` list. ``limit`` truncates the result when set.
    """
    fn = _METRIC_FNS.get(season["metric"])
    if fn is None:
        raise ValueError(f"unknown metric {season['metric']!r}")
    out = await fn(db, season)
    if limit is not None:
        out = out[:int(limit)]
    return out


async def _collect_metric(
    db, guild_id: int, metric: str,
) -> list[tuple[int, float]]:
    """Legacy shim kept for ``end_season``. Dispatches via ``fetch_standings``
    using a minimal season-shaped dict so existing callers keep working.
    """
    # end_season() passes full season row; this path is only hit by tests /
    # external callers constructing a bare (guild_id, metric) pair.
    stub = {
        "guild_id": guild_id,
        "metric": metric,
        "started_at": _dt.datetime.fromtimestamp(0, tz=_dt.timezone.utc),
        "season_id": 0,
    }
    return await fetch_standings(db, stub)


async def end_season(bot, season_id: int) -> dict:
    """Finalize a season: snapshot the leaderboard, pay rewards, mark done.

    Returns a summary dict: {season, winners: [(uid, rank, value, reward)], ...}
    Safe to call once; raises if the season is already finalized.
    """
    db = bot.db
    season = await get_season(db, season_id)
    if season is None:
        raise ValueError(f"season {season_id} not found")
    if season["status"] != "active":
        raise ValueError(f"season {season_id} is not active")

    gid = int(season["guild_id"])
    ranked = await fetch_standings(db, season)
    rewards = _compute_rewards(float(season["prize_pool_usd"]), len(ranked))

    winners: list[tuple[int, int, float, float]] = []
    for i, (uid, value) in enumerate(ranked[:MAX_PAYOUT_RANKS]):
        rank = i + 1
        reward = rewards[i] if i < len(rewards) else 0.0
        await db.execute(
            """
            INSERT INTO season_entries
                (season_id, user_id, guild_id, final_rank, metric_value, reward_usd)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (season_id, user_id) DO UPDATE SET
                final_rank   = EXCLUDED.final_rank,
                metric_value = EXCLUDED.metric_value,
                reward_usd   = EXCLUDED.reward_usd
            """,
            season_id, uid, gid, rank, float(value), float(reward),
        )
        if reward > 0:
            try:
                await db.update_wallet(uid, gid, to_raw(reward))
                await db.log_tx(
                    gid, uid, "SEASON_REWARD",
                    symbol_out="USD", amount_out=to_raw(reward),
                    network="usd",
                )
            except Exception as exc:
                log.exception("season reward payout failed for %s: %s", uid, exc)
        winners.append((uid, rank, float(value), float(reward)))

    await db.execute(
        """
        UPDATE seasons
           SET status = 'finalized', finalized_at = NOW()
         WHERE season_id = $1
        """,
        season_id,
    )
    try:
        await bot.bus.publish(
            "season_ended",
            guild=bot.get_guild(gid),
            season_id=season_id,
            name=season["name"],
            winners=[
                {"user_id": u, "rank": r, "value": v, "reward_usd": rw}
                for (u, r, v, rw) in winners
            ],
        )
    except Exception as exc:
        log.error("season_ended publish failed: %s", exc)
    return {"season": season, "winners": winners}


async def check_expired(bot) -> list[int]:
    """Finalize every season whose ends_at has passed. Returns season_ids ended."""
    db = bot.db
    rows = await db.fetch_all(
        """
        SELECT season_id FROM seasons
        WHERE status = 'active' AND ends_at <= NOW()
        """,
    )
    ended: list[int] = []
    for r in (rows or []):
        try:
            await end_season(bot, int(r["season_id"]))
            ended.append(int(r["season_id"]))
        except Exception as exc:
            log.exception("end_season failed for %s: %s", r["season_id"], exc)
    return ended


# ── Season pass ──────────────────────────────────────────────────────────────

async def grant_xp(
    bot, user_id: int, guild_id: int, xp: int,
    *, event_name: str | None = None,
) -> int:
    """Add ``xp`` to the user's pass progress in the active season.

    Returns the new total XP (0 if no active season). Safe to call with
    ``xp <= 0``; negative or zero values are dropped without a DB write.

    When ``event_name`` is provided, the active season's theme multiplier
    for that event is applied before insertion. Listeners attached via
    ``attach_pass_listeners`` always pass the event_name; other callers
    can omit it and get the raw xp value.
    """
    if xp <= 0 or not user_id or not guild_id:
        return 0
    db = bot.db
    active = await get_active(db, guild_id)
    if active is None:
        return 0
    effective = int(xp)
    if event_name:
        mult = _multipliers(active).get(event_name, 1.0)
        if mult != 1.0:
            effective = max(1, int(round(xp * mult)))
    row = await db.fetch_one(
        """
        INSERT INTO season_xp (season_id, user_id, guild_id, xp)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (season_id, user_id) DO UPDATE
            SET xp = season_xp.xp + EXCLUDED.xp,
                updated_at = NOW()
        RETURNING xp
        """,
        int(active["season_id"]), user_id, guild_id, effective,
    )
    return int(row["xp"]) if row else 0


async def get_xp(db, season_id: int, user_id: int) -> int:
    val = await db.fetch_val(
        "SELECT xp FROM season_xp WHERE season_id = $1 AND user_id = $2",
        int(season_id), int(user_id),
    )
    return int(val or 0)


async def top_xp(
    db, season_id: int, limit: int = 10,
) -> list[dict]:
    """Return ``[{user_id, xp}]`` ordered by highest XP first."""
    rows = await db.fetch_all(
        """
        SELECT user_id, xp
        FROM season_xp
        WHERE season_id = $1
        ORDER BY xp DESC
        LIMIT $2
        """,
        int(season_id), int(limit),
    )
    return rows or []


async def grant_counter(
    bot, user_id: int, guild_id: int, counter: str, amount: int = 1,
) -> int:
    """Increment a per-season activity counter and return the new value.

    No-op when no season is active -- listeners can stay wired 24/7 and
    only accumulate when there's an active race. Separate from pass XP
    (which lives in its own table) because counters can be arbitrarily
    many and sparsely populated per user.
    """
    if amount <= 0 or not user_id or not guild_id or not counter:
        return 0
    db = bot.db
    active = await get_active(db, guild_id)
    if active is None:
        return 0
    row = await db.fetch_one(
        """
        INSERT INTO season_counters (season_id, user_id, guild_id, counter, value)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (season_id, user_id, counter) DO UPDATE
            SET value = season_counters.value + EXCLUDED.value,
                updated_at = NOW()
        RETURNING value
        """,
        int(active["season_id"]), user_id, guild_id, counter, int(amount),
    )
    return int(row["value"]) if row else 0


async def total_pass_payout(db, season_id: int) -> float:
    """Return the sum of reward_usd already claimed from the pass this season."""
    val = await db.fetch_val(
        "SELECT COALESCE(SUM(reward_usd), 0) FROM season_tier_claims "
        "WHERE season_id = $1",
        int(season_id),
    )
    return float(val or 0.0)


async def claimed_tiers(db, season_id: int, user_id: int) -> set[int]:
    rows = await db.fetch_all(
        "SELECT tier FROM season_tier_claims "
        "WHERE season_id = $1 AND user_id = $2",
        int(season_id), int(user_id),
    )
    return {int(r["tier"]) for r in (rows or [])}


async def claim_tier(
    bot, season_id: int, user_id: int, guild_id: int, tier: int,
) -> tuple[bool, str, float]:
    """Claim a single tier reward. Returns (ok, message, reward_usd).

    Validates: the tier is within range, the user has earned it (their
    season XP is at or past the tier threshold), and the tier has not
    already been claimed for this season.
    """
    if tier < 1 or tier > _pass_cfg.MAX_TIER:
        return False, f"Tier must be between 1 and {_pass_cfg.MAX_TIER}.", 0.0
    db = bot.db
    xp = await get_xp(db, season_id, user_id)
    unlocked = _pass_cfg.tier_for_xp(xp)
    if tier > unlocked:
        needed = _pass_cfg.xp_for_tier(tier)
        return False, f"You need {needed - xp:,} more XP to unlock tier {tier}.", 0.0

    reward = _pass_cfg.tier_reward(tier)
    inserted = await db.fetch_one(
        """
        INSERT INTO season_tier_claims
            (season_id, user_id, guild_id, tier, reward_usd)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (season_id, user_id, tier) DO NOTHING
        RETURNING tier
        """,
        int(season_id), user_id, guild_id, int(tier), float(reward),
    )
    if inserted is None:
        return False, f"Tier {tier} was already claimed.", 0.0

    if reward > 0:
        try:
            await db.update_wallet(user_id, guild_id, to_raw(reward))
            await db.log_tx(
                guild_id, user_id, "SEASON_PASS_REWARD",
                symbol_out="USD", amount_out=to_raw(reward),
                network="usd",
            )
        except Exception as exc:
            log.exception("pass reward payout failed uid=%s tier=%s: %s",
                          user_id, tier, exc)
    try:
        await bot.bus.publish(
            "season_tier_claimed",
            guild=bot.get_guild(guild_id),
            user_id=user_id,
            season_id=season_id,
            tier=tier,
            reward_usd=reward,
        )
    except Exception as exc:
        log.error("season_tier_claimed publish failed: %s", exc)
    return True, "", reward


# ── Pass bus listeners ───────────────────────────────────────────────────────

def _extract_uid(user: Any) -> int | None:
    if user is None:
        return None
    if isinstance(user, int):
        return user
    return int(getattr(user, "id", 0) or 0) or None


def _extract_gid(guild: Any, fallback: Any = None) -> int | None:
    if guild is None:
        guild = fallback
    if guild is None:
        return None
    if isinstance(guild, int):
        return guild
    return int(getattr(guild, "id", 0) or 0) or None


def attach_pass_listeners(bot) -> None:
    """Subscribe the season pass to every XP-granting bus event.

    Grants are no-ops when no season is active, so this can stay wired
    all the time; no need to detach on end_season.
    """
    bus = bot.bus

    def _simple(event_name: str, xp: int):
        async def _cb(**kw) -> None:
            uid = _extract_uid(kw.get("user") or kw.get("user_id"))
            gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
            if not (uid and gid):
                return
            try:
                await grant_xp(bot, uid, gid, xp, event_name=event_name)
            except Exception as exc:
                log.error("pass xp grant failed on %s: %s", event_name, exc)
        return _cb

    async def _on_pow_tick(**kw) -> None:
        payouts = kw.get("payouts") or []
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not gid:
            return
        xp = _pass_cfg.XP_EVENTS.get("block_mined", 0)
        if xp <= 0:
            return
        if isinstance(payouts, dict):
            ids = list(payouts.keys())
        else:
            ids = []
            for p in payouts:
                if isinstance(p, dict):
                    ids.append(p.get("user_id") or p.get("uid"))
                elif isinstance(p, (list, tuple)) and p:
                    ids.append(p[0])
        for raw in ids:
            uid = _extract_uid(raw)
            if uid:
                await grant_xp(bot, uid, gid, xp, event_name="block_mined")

    async def _on_gamble(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await grant_xp(bot, uid, gid,
                       _pass_cfg.XP_EVENTS.get("gamble_play", 0),
                       event_name="gamble_play")
        if kw.get("won") or float(kw.get("delta", 0) or 0) > 0:
            await grant_xp(bot, uid, gid,
                           _pass_cfg.XP_EVENTS.get("gamble_win", 0),
                           event_name="gamble_win")

    async def _on_exploit(**kw) -> None:
        # cogs/eat_the_rich.py uses attacker= (not user=/user_id=).
        uid = _extract_uid(
            kw.get("user") or kw.get("user_id") or kw.get("attacker")
        )
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await grant_xp(bot, uid, gid,
                       _pass_cfg.XP_EVENTS.get("exploit_run", 0),
                       event_name="exploit_run")
        if kw.get("won") or kw.get("success"):
            await grant_xp(bot, uid, gid,
                           _pass_cfg.XP_EVENTS.get("exploit_win", 0),
                           event_name="exploit_win")

    async def _on_ape(**kw) -> None:
        # ,ape uses gamble_play / gamble_win XP rates.
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await grant_xp(bot, uid, gid,
                       _pass_cfg.XP_EVENTS.get("gamble_play", 0),
                       event_name="gamble_play")
        if float(kw.get("net", 0) or 0) > 0:
            await grant_xp(bot, uid, gid,
                           _pass_cfg.XP_EVENTS.get("gamble_win", 0),
                           event_name="gamble_win")

    async def _on_mining_tick_complete(**kw) -> None:
        # SUN/MTA PoW path. Credit every paid miner with block_mined XP.
        summary = kw.get("summary") or {}
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not gid:
            return
        xp = _pass_cfg.XP_EVENTS.get("block_mined", 0)
        if xp <= 0:
            return
        ids: set[int] = set()
        for entry in summary.get("solo_payouts") or []:
            if isinstance(entry, (list, tuple)) and entry:
                uid = _extract_uid(entry[0])
                if uid:
                    ids.add(uid)
        for raw in summary.get("pool_miner_ids") or []:
            uid = _extract_uid(raw)
            if uid:
                ids.add(uid)
        for grp in summary.get("groups") or []:
            for m in (grp.get("members") or []):
                if isinstance(m, (list, tuple)) and m:
                    uid = _extract_uid(m[0])
                    if uid:
                        ids.add(uid)
        for uid in ids:
            await grant_xp(bot, uid, gid, xp, event_name="block_mined")

    # Direct 1:1 event -> xp mappings, skipping the fan-out events handled
    # above so we don't double-grant on the same activity.
    _direct = {
        "work_completed",
        "daily_claimed",
        "trade",
        "trade_executed",
        "swap_trade",
        "swap_executed",
        "staked",
        "lp_added",
        "deposit",
        "buddy_adopted",
        "buddy_battle_win",
        "validator_registered",
        "drop_claimed",
        "fish_caught",
        "fish_legendary",
        "fish_buddy_egg",
    }
    for event in _direct:
        xp = _pass_cfg.XP_EVENTS.get(event, 0)
        if xp > 0:
            bus.subscribe(event, _simple(event, xp))

    bus.subscribe("pow_mining_tick", _on_pow_tick)
    bus.subscribe("mining_tick_complete", _on_mining_tick_complete)
    bus.subscribe("gamble_result", _on_gamble)
    bus.subscribe("exploit_completed", _on_exploit)
    bus.subscribe("ape_completed", _on_ape)

    # Per-season counters used by non-XP metrics (buddy_wins so far).
    async def _on_buddy_win_counter(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        try:
            await grant_counter(bot, uid, gid, "buddy_wins", 1)
        except Exception as exc:
            log.error("buddy_wins counter failed: %s", exc)

    bus.subscribe("buddy_battle_win", _on_buddy_win_counter)
