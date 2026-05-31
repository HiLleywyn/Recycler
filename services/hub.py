"""
services/hub.py  -  Daily hub aggregator + login-streak service.

Backs the ``,today`` panel:

* ``claim_daily_bonus`` -- once per UTC day. Bumps the user's streak,
  credits a USD-equivalent reward into the wallet, and returns the
  streak result for the cog to render.
* ``streak_status`` -- read-only "did I claim today?" + streak counters
  for the panel header.
* ``hub_summary`` -- one-shot stitched dict the cog renders without
  re-querying the world: net worth, top quests, ready-to-claim hints,
  streak, season tier.

Streak math:
    Day N grants ``base * min(N, cap) + bonus_at_step(N)`` USD where
    ``base = 0.50`` and ``cap = 30``. Milestones at days 7 / 14 / 30
    add a flat bonus on top. Past 30 days the streak holds at +30 (so
    perpetual logins keep paying without runaway inflation).

All DB clocks are server-side; the date is computed via ``CURRENT_DATE
AT TIME ZONE 'UTC'`` so container drift is irrelevant.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any

from core.framework.scale import to_raw

log = logging.getLogger(__name__)


# Reward schedule. Tweak in one place; both the claim path and the
# preview-row in the panel header read these constants.
_BASE_REWARD_USD = 0.50
_STREAK_CAP = 30
_MILESTONE_BONUS_USD = {
    7:  3.00,
    14: 7.00,
    30: 25.00,
}


def _scaled_reward_usd(streak: int) -> tuple[float, float]:
    """Return ``(base_reward_usd, milestone_bonus_usd)`` for ``streak``.

    ``streak`` is the post-claim count (1 on the first day). Past
    ``_STREAK_CAP`` the base reward saturates so a 90-day streak pays
    the same as a 30-day streak; milestone bonuses only fire on the
    exact day they trigger.
    """
    base = _BASE_REWARD_USD * float(min(int(streak), _STREAK_CAP))
    bonus = float(_MILESTONE_BONUS_USD.get(int(streak), 0.0))
    return base, bonus


def reward_preview_usd(streak_after: int) -> float:
    """Total USD a claim would grant if ``streak_after`` were achieved."""
    base, bonus = _scaled_reward_usd(int(streak_after))
    return base + bonus


@dataclass(slots=True)
class StreakResult:
    new_streak: int
    longest: int
    reward_usd: float
    base_usd: float
    bonus_usd: float
    is_milestone: bool
    already_claimed: bool


@dataclass(slots=True)
class StreakStatus:
    current_streak: int
    longest_streak: int
    claimed_today: bool
    last_claim_utc: _dt.date | None
    total_claims: int


async def streak_status(
    db: Any, user_id: int, guild_id: int,
) -> StreakStatus:
    """Read-only streak view, sourced from the earn cog's columns.

    The earn cog (``,daily``) is the canonical daily-cooldown gate --
    ``users.last_daily`` (TIMESTAMPTZ) is the claim stamp and
    ``users.daily_streak`` (INTEGER) is the consecutive-day counter.
    The hub used to maintain a parallel ``user_daily_streak`` table
    which made ``,today`` and ``,daily`` two separate dailies; this
    reader now points at the same columns the earn cog writes so the
    two surfaces always agree.

    ``claimed_today`` is True when ``last_daily`` is within the last
    ``Config.DAILY_COOLDOWN`` seconds.
    """
    try:
        from core.config import Config
        cooldown_s = float(Config.DAILY_COOLDOWN)
    except Exception:
        cooldown_s = 86400.0     # 24h fallback

    row = await db.fetch_one(
        """
        SELECT daily_streak,
               last_daily,
               EXTRACT(EPOCH FROM (NOW() - last_daily))::bigint AS elapsed
          FROM users
         WHERE user_id = $1 AND guild_id = $2
        """,
        int(user_id), int(guild_id),
    )
    if not row:
        return StreakStatus(0, 0, False, None, 0)
    streak = int(row.get("daily_streak") or 0)
    last = row.get("last_daily")
    elapsed = row.get("elapsed")
    claimed_today = bool(
        elapsed is not None and int(elapsed) < int(cooldown_s)
    )
    last_date: _dt.date | None = None
    if isinstance(last, _dt.datetime):
        last_date = last.date()
    elif isinstance(last, _dt.date):
        last_date = last
    return StreakStatus(
        current_streak=streak,
        longest_streak=streak,    # legacy parity (longest_streak isn't tracked in users)
        claimed_today=claimed_today,
        last_claim_utc=last_date,
        total_claims=0,
    )


async def claim_daily_bonus(
    db: Any, user_id: int, guild_id: int,
) -> StreakResult:
    """Claim the once-per-UTC-day bonus.

    Streak progression:
    * First-ever claim   -> streak = 1.
    * Claim on next day  -> streak += 1.
    * Skipped a day      -> streak = 1 (broke and restarted today).
    * Same-day re-claim  -> ``already_claimed=True``, no credit.

    Always returns within one DB transaction so concurrent ``,today``
    spam can't double-credit.
    """
    today = _dt.datetime.utcnow().date()
    yesterday = today - _dt.timedelta(days=1)

    async with db.atomic():
        row = await db.fetch_one(
            """
            SELECT current_streak, longest_streak, last_claim_utc, total_claims
              FROM user_daily_streak
             WHERE user_id = $1 AND guild_id = $2
             FOR UPDATE
            """,
            int(user_id), int(guild_id),
        )
        last = row.get("last_claim_utc") if row else None
        cur_streak = int((row or {}).get("current_streak") or 0)
        longest = int((row or {}).get("longest_streak") or 0)
        total_claims = int((row or {}).get("total_claims") or 0)

        if last and last == today:
            return StreakResult(
                new_streak=cur_streak,
                longest=longest,
                reward_usd=0.0,
                base_usd=0.0,
                bonus_usd=0.0,
                is_milestone=False,
                already_claimed=True,
            )

        if last == yesterday and cur_streak >= 1:
            new_streak = cur_streak + 1
        else:
            new_streak = 1
        new_longest = max(longest, new_streak)

        base_usd, bonus_usd = _scaled_reward_usd(new_streak)
        reward_usd = base_usd + bonus_usd
        is_milestone = bonus_usd > 0

        # Wallet credit. Wallet stores USD-pegged stable as the
        # canonical liquid balance, so a flat USD amount is correct.
        await db.update_wallet(
            int(user_id), int(guild_id), to_raw(reward_usd),
        )

        await db.execute(
            """
            INSERT INTO user_daily_streak (
                user_id, guild_id,
                current_streak, longest_streak,
                last_claim_utc, total_claims,
                total_reward_usd_raw,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::numeric, NOW())
            ON CONFLICT (user_id, guild_id) DO UPDATE SET
                current_streak       = EXCLUDED.current_streak,
                longest_streak       = EXCLUDED.longest_streak,
                last_claim_utc       = EXCLUDED.last_claim_utc,
                total_claims         = user_daily_streak.total_claims + 1,
                total_reward_usd_raw = user_daily_streak.total_reward_usd_raw
                                       + EXCLUDED.total_reward_usd_raw,
                updated_at           = NOW()
            """,
            int(user_id), int(guild_id),
            int(new_streak), int(new_longest),
            today, int(total_claims) + 1,
            str(int(to_raw(reward_usd))),
        )

    return StreakResult(
        new_streak=int(new_streak),
        longest=int(new_longest),
        reward_usd=float(reward_usd),
        base_usd=float(base_usd),
        bonus_usd=float(bonus_usd),
        is_milestone=is_milestone,
        already_claimed=False,
    )


# ---------------------------------------------------------------------------
# Hub summary -- one-stop fetch for the ,today panel.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HubSummary:
    streak:           StreakStatus
    next_reward_usd:  float
    net_worth_usd:    float
    wallet_usd:       float
    bank_usd:         float
    quests_total:     int
    quests_ready:     int
    quests_top:       list[dict]
    challenges_active: int
    ready_hints:      list[str]
    # Quick-collect counts. The hub view uses these to enable / disable
    # one-shot action buttons without re-querying inside callbacks.
    eggs_held:        int = 0
    daycare_ready:    int = 0
    expeditions_ready:   int = 0
    expeditions_running: int = 0
    plots_ripe:       int = 0
    traps_placed:     int = 0
    daily_earn_available: bool = False
    work_available:   bool = False
    ah_active:        int = 0
    # Unspent stat-point counters surfaced on the today panel so points
    # that landed via level-ups don't sit forever. ``buddies_unspent`` sums
    # across every owned + active buddy on the player's roster.
    delve_unspent_stats:   int = 0
    buddies_unspent_stats: int = 0
    delve_class_key:       str = ""
    # Daily-loop probes for the new farming + delve features. Booleans
    # keep the home view's quick-collect button logic uniform with the
    # existing ``daily_earn_available`` / ``work_available`` flags.
    forage_ready:           bool = False
    beachcomb_ready:        bool = False
    scavenge_ready:         bool = False
    contract_actionable:    bool = False
    delve_shrine_in_room:   bool = False
    delve_chest_in_room:    bool = False
    # Compact calendar feed (top 5 live + upcoming items). Full grid
    # behind the Calendar button on the unified today panel.
    calendar_items:        list = field(default_factory=list)


async def hub_summary(
    db: Any, user_id: int, guild_id: int,
) -> HubSummary:
    """Aggregate every panel-side number in one shot.

    Pulls:
    * Streak status + the reward the next claim WILL grant.
    * Net worth via the canonical ``services.net_worth`` helper.
    * Quest progress (top 3 unclaimed) via ``services.quests``.
    * Active guild challenge count via ``services.challenges``.
    * Per-cog "ready" hints (eggs ready to hatch, plots ripe, AH
      sales settled, etc.) sourced from light targeted queries.

    Each helper call is wrapped in try/except: a busted subsystem
    degrades the panel (its row goes missing) rather than 500-ing
    the whole hub.
    """
    sr = await streak_status(db, user_id, guild_id)
    next_streak = sr.current_streak + 1 if not sr.claimed_today else sr.current_streak
    next_reward = (
        reward_preview_usd(next_streak) if not sr.claimed_today else 0.0
    )

    net_total = wallet_h = bank_h = 0.0
    try:
        from services import net_worth as _nw
        result = await _nw.compute_net_worth(int(user_id), int(guild_id), db)
        net_total = float(result.total or 0.0)
        wallet_h = float(getattr(result, "wallet", 0.0) or 0.0)
        bank_h = float(getattr(result, "bank", 0.0) or 0.0)
    except Exception:
        log.debug("hub: net worth fetch failed", exc_info=True)

    quests_top: list[dict] = []
    quests_total = quests_ready = 0
    try:
        from services import quests as _q
        rows = await _q.current_for_user(db, int(user_id), int(guild_id))
        flat: list[dict] = []
        for period in ("daily", "weekly"):
            for r in rows.get(period, []) or []:
                flat.append({**dict(r), "_period": period})
        quests_total = len(flat)
        for r in flat:
            if not r.get("claimed") and int(r.get("progress") or 0) >= int(r.get("target") or 0):
                quests_ready += 1
        # Top 3: unclaimed, prefer ready then highest %, then daily-first.
        def _key(r: dict) -> tuple:
            target = max(1, int(r.get("target") or 0))
            prog = int(r.get("progress") or 0)
            ready = 1 if (not r.get("claimed") and prog >= target) else 0
            pct = prog / target
            period_rank = 0 if r.get("_period") == "daily" else 1
            return (-ready, -pct, period_rank)
        quests_top = sorted(
            (r for r in flat if not r.get("claimed")),
            key=_key,
        )[:3]
    except Exception:
        log.debug("hub: quest fetch failed", exc_info=True)

    challenges_active = 0
    try:
        from services import challenges as _c
        active = await _c.list_active(db, int(guild_id))
        challenges_active = len(active or [])
    except Exception:
        log.debug("hub: challenges fetch failed", exc_info=True)

    ready_hints, ready_counts = await _ready_to_claim_hints(
        db, int(user_id), int(guild_id),
    )

    delve_unspent, delve_class = await _delve_unspent_stats(db, int(user_id), int(guild_id))
    buddies_unspent = await _buddies_unspent_stats(db, int(user_id), int(guild_id))
    calendar_items: list = []
    try:
        from services import calendar as _cal
        calendar_items = await _cal.list_calendar(
            db, int(guild_id),
        )
    except Exception:
        log.debug("hub: calendar fetch failed", exc_info=True)
    # Unspent delve / buddy stat points are NOT prepended into ready_hints
    # anymore -- the embed builder renders them in a dedicated "Stat
    # points" field, so prepending here just duplicates the same line
    # under "Ready right now". Keep the feed capped for mobile.
    ready_hints = ready_hints[:6]

    return HubSummary(
        streak=sr,
        next_reward_usd=next_reward,
        net_worth_usd=net_total,
        wallet_usd=wallet_h,
        bank_usd=bank_h,
        quests_total=quests_total,
        quests_ready=quests_ready,
        quests_top=quests_top,
        challenges_active=challenges_active,
        ready_hints=ready_hints,
        delve_unspent_stats=int(delve_unspent),
        buddies_unspent_stats=int(buddies_unspent),
        delve_class_key=str(delve_class or ""),
        calendar_items=list(calendar_items or []),
        eggs_held=int(ready_counts.get("eggs_held") or 0),
        daycare_ready=int(ready_counts.get("daycare_ready") or 0),
        expeditions_running=int(ready_counts.get("expeditions_running") or 0),
        expeditions_ready=int(ready_counts.get("expeditions_ready") or 0),
        plots_ripe=int(ready_counts.get("plots_ripe") or 0),
        traps_placed=int(ready_counts.get("traps_placed") or 0),
        daily_earn_available=bool(ready_counts.get("daily_earn_available")),
        work_available=bool(ready_counts.get("work_available")),
        ah_active=int(ready_counts.get("ah_active") or 0),
        forage_ready=bool(ready_counts.get("forage_ready")),
        beachcomb_ready=bool(ready_counts.get("beachcomb_ready")),
        scavenge_ready=bool(ready_counts.get("scavenge_ready")),
        contract_actionable=bool(ready_counts.get("contract_actionable")),
        delve_shrine_in_room=bool(ready_counts.get("delve_shrine_in_room")),
        delve_chest_in_room=bool(ready_counts.get("delve_chest_in_room")),
    )


async def _delve_unspent_stats(
    db: Any, user_id: int, guild_id: int,
) -> tuple[int, str]:
    """Return ``(unspent_points, class_key)`` for the player's delve row.

    Returns ``(0, "")`` if the player hasn't picked a class yet so the
    today panel doesn't pester them about a system they haven't engaged
    with. Wrapped in try/except in the caller so a missing migration
    doesn't 500 the whole hub.
    """
    try:
        row = await db.fetch_one(
            """
            SELECT class_key, level, hp_alloc, atk_alloc, spd_alloc, int_alloc
              FROM user_dungeon
             WHERE user_id = $1 AND guild_id = $2
            """,
            int(user_id), int(guild_id),
        )
    except Exception:
        log.debug("hub.delve_unspent: probe failed", exc_info=True)
        return 0, ""
    if not row or not row.get("class_key"):
        return 0, ""
    try:
        import configs.dungeon_config as _dc
        avail = _dc.stat_points_available(
            int(row.get("level") or 1),
            int(row.get("hp_alloc")  or 0),
            int(row.get("atk_alloc") or 0),
            int(row.get("spd_alloc") or 0),
            int(row.get("int_alloc") or 0),
        )
    except Exception:
        log.debug("hub.delve_unspent: stat_points_available failed", exc_info=True)
        return 0, ""
    return int(avail), str(row.get("class_key") or "")


async def _buddies_unspent_stats(
    db: Any, user_id: int, guild_id: int,
) -> int:
    """Sum unspent stat points across every owned buddy.

    Mirrors ``cogs/buddy.py:_alloc_summary`` (level * STAT_POINTS_PER_LEVEL
    minus the three alloc counters) but folded into a single SUM query
    so the today panel doesn't need to hydrate a buddy roster.
    """
    try:
        from configs.buddies_config import STAT_POINTS_PER_LEVEL as _PTS
    except Exception:
        _PTS = 1
    try:
        row = await db.fetch_one(
            """
            SELECT COALESCE(SUM(GREATEST(
                0,
                level * $3 - (hp_alloc + atk_alloc + spd_alloc)
            )), 0) AS unspent
              FROM cc_buddies
             WHERE owner_user_id = $1 AND guild_id = $2 AND status = 'owned'
            """,
            int(user_id), int(guild_id), int(_PTS),
        )
    except Exception:
        log.debug("hub.buddies_unspent: probe failed", exc_info=True)
        return 0
    return int((row or {}).get("unspent") or 0)


async def _ready_to_claim_hints(
    db: Any, user_id: int, guild_id: int,
) -> tuple[list[str], dict[str, int]]:
    """Probe every "claimable right now" surface in one shot.

    Returns ``(hints, counts)`` where hints are ordered short lines for
    the panel feed (top 5 surface up) and counts feed the hub view's
    quick-collect button enabled-state without re-querying. Each probe
    is wrapped in try/except so a busted subsystem just leaves its row
    out of the panel rather than 500-ing the whole hub.

    Probes:
    * eggs held (``,fish egg``)
    * daycare ready (egg_ready_at <= NOW())
    * expeditions ready (ends_at <= NOW()) + still-running fallback hint
    * ripe farm plots (jsonb_array_elements + ready_at <= NOW())
    * placed crab traps (count placed; soak time per trap is checked
      inside ``,fish trap collect``)
    * hungry / sad / tired active buddy (any stat below 30)
    * AH active listings
    * ``,daily`` earn cog 24h check-in
    """
    hints: list[str] = []
    counts: dict[str, int] = {}

    # Eggs ready to hatch (held_eggs JSONB array on user_fishing).
    try:
        eggs_row = await db.fetch_one(
            """
            SELECT COALESCE(jsonb_array_length(held_eggs), 0) AS n
              FROM user_fishing
             WHERE user_id = $1 AND guild_id = $2
            """,
            int(user_id), int(guild_id),
        )
        n = int((eggs_row or {}).get("n") or 0)
        counts["eggs_held"] = n
        if n > 0:
            hints.append(
                f"\U0001F423 **{n} egg{'s' if n != 1 else ''}** held -- "
                f"`,fish egg` to hatch / sell / list."
            )
    except Exception:
        log.debug("hub.ready: eggs probe failed", exc_info=True)

    # Daycare incubation finished.
    try:
        dc_row = await db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM cc_buddy_daycare
             WHERE user_id = $1 AND guild_id = $2
               AND egg_collected = FALSE
               AND egg_ready_at <= NOW()
            """,
            int(user_id), int(guild_id),
        )
        n = int((dc_row or {}).get("n") or 0)
        counts["daycare_ready"] = n
        if n > 0:
            hints.append(
                f"\U0001FAB9 **{n} nest egg{'s' if n != 1 else ''}** "
                f"ready to collect -- `,buddy nest collect`."
            )
    except Exception:
        log.debug("hub.ready: nest probe failed", exc_info=True)

    # Buddy expeditions: split ready vs still-running on the DB clock so
    # the hub hint matches what ,expedition shows. Ready ones go into the
    # "Ready right now" feed; still-running are tracked separately so the
    # caller can render them under an Activity rollup -- they are NOT
    # claimable yet and shouldn't pollute the ready feed.
    try:
        exp_row = await db.fetch_one(
            """
            SELECT
                COUNT(*) FILTER (WHERE ends_at <= NOW()) AS ready,
                COUNT(*) FILTER (WHERE ends_at >  NOW()) AS running
              FROM buddy_expeditions
             WHERE user_id = $1 AND guild_id = $2
               AND status = 'running'
            """,
            int(user_id), int(guild_id),
        )
        ready = int((exp_row or {}).get("ready") or 0)
        running = int((exp_row or {}).get("running") or 0)
        counts["expeditions_ready"] = ready
        counts["expeditions_running"] = running
        if ready > 0:
            hints.append(
                f"\U0001F392 **{ready} expedition"
                f"{'s' if ready != 1 else ''}** ready -- "
                f"`,expedition collect`."
            )
    except Exception:
        log.debug("hub.ready: expedition probe failed", exc_info=True)

    # Ripe farm plots. Counts only ones where ready_at has passed -- the
    # JSONB ISO timestamp casts cleanly to TIMESTAMPTZ so the comparison
    # runs server-side (no Python clock skew). State 'growing' covers
    # the standard path; 'ready' catches the soft state some code paths
    # set after the timer hits.
    try:
        plot_row = await db.fetch_one(
            """
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN (p->>'state') IN ('growing', 'ready')
                         AND (p->>'ready_at') IS NOT NULL
                         AND (p->>'ready_at')::timestamptz <= NOW()
                        THEN 1 ELSE 0
                    END
                ), 0) AS ripe
              FROM user_farming uf
              CROSS JOIN LATERAL jsonb_array_elements(uf.plots) AS p
             WHERE uf.user_id = $1 AND uf.guild_id = $2
            """,
            int(user_id), int(guild_id),
        )
        ripe = int((plot_row or {}).get("ripe") or 0)
        counts["plots_ripe"] = ripe
        if ripe > 0:
            hints.append(
                f"\U0001F33E **{ripe} plot{'s' if ripe != 1 else ''}** ripe -- "
                f"`,farm` to harvest."
            )
    except Exception:
        log.debug("hub.ready: plots probe failed", exc_info=True)

    # Placed crab traps. Pull the JSONB array client-side and check each
    # trap's ``placed_at + soak_seconds <= NOW()`` against the live
    # fishing_config. Previously we just counted "any placed trap" and
    # told the player to run ``,fish trap`` -- both of which were wrong.
    # Now ``,today`` only mentions traps that are actually ready and
    # points at ``,fish trap collect``.
    try:
        trap_row = await db.fetch_one(
            """
            SELECT placed_crab_traps
              FROM user_fishing
             WHERE user_id = $1 AND guild_id = $2
            """,
            int(user_id), int(guild_id),
        )
        placed = (trap_row or {}).get("placed_crab_traps") or []
        if isinstance(placed, str):
            try:
                import json as _json
                placed = _json.loads(placed)
            except Exception:
                placed = []
        try:
            import datetime as _dt2
            import configs.fishing_config as _fc
            now = _dt2.datetime.now(_dt2.timezone.utc)
            ready = 0
            for t in placed or []:
                if not isinstance(t, dict):
                    continue
                key = str(t.get("key") or "")
                placed_at_iso = str(t.get("placed_at") or "")
                if not key or not placed_at_iso:
                    continue
                meta = _fc.crab_trap_meta(key) or {}
                soak = int(meta.get("soak_seconds") or 0)
                if soak <= 0:
                    continue
                try:
                    placed_at = _dt2.datetime.fromisoformat(placed_at_iso)
                except Exception:
                    continue
                if placed_at + _dt2.timedelta(seconds=soak) <= now:
                    ready += 1
        except Exception:
            ready = 0
        counts["traps_placed"] = int(ready)
        if ready > 0:
            hints.append(
                f"\U0001F980 **{ready} trap{'s' if ready != 1 else ''}** "
                f"ready to haul -- `,fish trap collect`."
            )
    except Exception:
        log.debug("hub.ready: traps probe failed", exc_info=True)

    # Hungry / sad / tired active buddy.
    try:
        bud_row = await db.fetch_one(
            """
            SELECT name, hunger, happiness, energy
              FROM cc_buddies
             WHERE guild_id = $1 AND owner_user_id = $2
               AND status = 'owned' AND is_active
             LIMIT 1
            """,
            int(guild_id), int(user_id),
        )
        if bud_row:
            mood_low = []
            if int(bud_row.get("hunger") or 100) < 30:
                mood_low.append("hungry")
            if int(bud_row.get("happiness") or 100) < 30:
                mood_low.append("sad")
            if int(bud_row.get("energy") or 100) < 30:
                mood_low.append("tired")
            if mood_low:
                hints.append(
                    f"\U0001F436 **{bud_row.get('name') or 'Your buddy'}** is "
                    f"{', '.join(mood_low)} -- `,buddy` to feed / pet / talk."
                )
    except Exception:
        log.debug("hub.ready: buddy mood probe failed", exc_info=True)

    # Empty farm plots -- prompt the player to plant.
    try:
        empty_row = await db.fetch_one(
            """
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN COALESCE(p->>'state', 'empty') = 'empty'
                        THEN 1 ELSE 0
                    END
                ), 0) AS empty_n
              FROM user_farming uf
              CROSS JOIN LATERAL jsonb_array_elements(uf.plots) AS p
             WHERE uf.user_id = $1 AND uf.guild_id = $2
            """,
            int(user_id), int(guild_id),
        )
        empty = int((empty_row or {}).get("empty_n") or 0)
        if empty > 0:
            hints.append(
                f"\U0001F331 **{empty} empty plot"
                f"{'s' if empty != 1 else ''}** -- `,farm` to plant."
            )
    except Exception:
        log.debug("hub.ready: farm-empty probe failed", exc_info=True)

    # ,work and ,daily 24h check-ins. users.last_work / last_daily are
    # the canonical cooldown columns the earn cog reads + writes.
    try:
        from core.config import Config
        try:
            work_cd = float(Config.WORK_COOLDOWN)
        except Exception:
            work_cd = 3600.0
        try:
            daily_cd = float(Config.DAILY_COOLDOWN)
        except Exception:
            daily_cd = 86400.0

        cd_row = await db.fetch_one(
            """
            SELECT
                EXTRACT(EPOCH FROM (NOW() - last_work))::bigint  AS work_elapsed,
                EXTRACT(EPOCH FROM (NOW() - last_daily))::bigint AS daily_elapsed
              FROM users
             WHERE user_id = $1 AND guild_id = $2
            """,
            int(user_id), int(guild_id),
        )
        if cd_row:
            we = cd_row.get("work_elapsed")
            de = cd_row.get("daily_elapsed")
            work_avail = (we is None) or (int(we) >= int(work_cd))
            daily_avail = (de is None) or (int(de) >= int(daily_cd))
            counts["daily_earn_available"] = 1 if daily_avail else 0
            counts["work_available"] = 1 if work_avail else 0
            if daily_avail:
                hints.append(
                    "\U0001F4B5 Your **,daily** check-in is ready."
                )
            if work_avail:
                hints.append(
                    "\U0001F4BC Your **,work** shift is ready."
                )
    except Exception:
        log.debug("hub.ready: work/daily probe failed", exc_info=True)

    # AH active listings live in their own informational line, NOT in the
    # "ready to claim" feed -- they're not actionable in the same sense
    # (no claim step on a sold listing; cancellation is optional).
    # Emit a low-priority hint at the tail so the page still surfaces
    # the count for at-a-glance scanning.
    try:
        ah_n = int(await db.fetch_val(
            """
            SELECT COUNT(*) FROM auction_listings
             WHERE seller_user_id = $1 AND guild_id = $2
               AND status = 'active'
            """,
            int(user_id), int(guild_id),
        ) or 0)
        counts["ah_active"] = ah_n
    except Exception:
        log.debug("hub.ready: ah probe failed", exc_info=True)

    # Farm forage cooldown probe. Cooldown lives on the DB clock as
    # last_forage_at; flagging "ready" the instant the wait elapses
    # mirrors the in-cog ,farm forage gate so today/start agree.
    try:
        import configs.farming_config as _fc
        forage_row = await db.fetch_one(
            """
            SELECT
                CASE
                    WHEN last_forage_at IS NULL THEN 1
                    WHEN EXTRACT(EPOCH FROM (NOW() - last_forage_at))::INTEGER >= $3
                        THEN 1
                    ELSE 0
                END AS ready
              FROM user_farming
             WHERE user_id = $1 AND guild_id = $2
            """,
            int(user_id), int(guild_id), int(_fc.FORAGE_COOLDOWN_S),
        )
        if forage_row and int(forage_row.get("ready") or 0) > 0:
            counts["forage_ready"] = 1
            hints.append("\U0001F33F **Forage** ready -- `,farm forage`.")
    except Exception:
        log.debug("hub.ready: forage probe failed", exc_info=True)

    # Fish beachcomb cooldown probe. Same DB-clock pattern as forage --
    # surface the home-tab Beachcomb button the instant the wait elapses.
    try:
        import configs.fishing_config as _fic
        bc_row = await db.fetch_one(
            """
            SELECT
                CASE
                    WHEN last_beachcomb_at IS NULL THEN 1
                    WHEN EXTRACT(EPOCH FROM (NOW() - last_beachcomb_at))::INTEGER >= $3
                        THEN 1
                    ELSE 0
                END AS ready
              FROM user_fishing
             WHERE user_id = $1 AND guild_id = $2
            """,
            int(user_id), int(guild_id), int(_fic.BEACHCOMB_COOLDOWN_S),
        )
        if bc_row and int(bc_row.get("ready") or 0) > 0:
            counts["beachcomb_ready"] = 1
            hints.append("\U0001F3DD **Beachcomb** ready -- `,fish beachcomb`.")
    except Exception:
        log.debug("hub.ready: beachcomb probe failed", exc_info=True)

    # Delve scavenge cooldown probe. Hidden during an active run because
    # the in-cog command refuses mid-floor; checking run_id here keeps
    # the home-tab button in lockstep with the cog's gate.
    try:
        import configs.dungeon_config as _dcc
        sc_row = await db.fetch_one(
            """
            SELECT
                CASE
                    WHEN run_id IS NOT NULL THEN 0
                    WHEN last_scavenge_at IS NULL THEN 1
                    WHEN EXTRACT(EPOCH FROM (NOW() - last_scavenge_at))::INTEGER >= $3
                        THEN 1
                    ELSE 0
                END AS ready
              FROM user_dungeon
             WHERE user_id = $1 AND guild_id = $2
            """,
            int(user_id), int(guild_id), int(_dcc.SCAVENGE_COOLDOWN_S),
        )
        if sc_row and int(sc_row.get("ready") or 0) > 0:
            counts["scavenge_ready"] = 1
            hints.append("\U0001F50E **Scavenge** ready -- `,delve scavenge`.")
    except Exception:
        log.debug("hub.ready: scavenge probe failed", exc_info=True)

    # Daily contract turn-in probe. Surfaces only when today's contract
    # is set, NOT yet completed, AND the player has at least one of the
    # required crop in their bag (so the prompt is actionable, not noise).
    try:
        ctc_row = await db.fetch_one(
            """
            SELECT daily_contract, crop_inventory
              FROM user_farming
             WHERE user_id = $1 AND guild_id = $2
            """,
            int(user_id), int(guild_id),
        )
        if ctc_row:
            import json as _json
            contract = ctc_row.get("daily_contract")
            if isinstance(contract, str):
                try:
                    contract = _json.loads(contract) if contract else {}
                except Exception:
                    contract = {}
            contract = contract or {}
            crops = ctc_row.get("crop_inventory") or {}
            if isinstance(crops, str):
                try:
                    crops = _json.loads(crops) if crops else {}
                except Exception:
                    crops = {}
            ck = str(contract.get("crop_key") or "")
            required = int(contract.get("qty_required") or 0)
            delivered = int(contract.get("qty_delivered") or 0)
            have = int((crops or {}).get(ck, 0) or 0)
            if (
                ck and not contract.get("completed")
                and have > 0 and delivered < required
            ):
                counts["contract_actionable"] = 1
                hints.append(
                    f"\U0001F4E6 Daily contract: deliver **{ck}** -- `,farm contract turnin`."
                )
    except Exception:
        log.debug("hub.ready: contract probe failed", exc_info=True)

    # Delve shrine / chest in current room. Gives the home tab two
    # high-signal one-shot buttons when the player walks back into the
    # panel mid-run (e.g. after AFK).
    try:
        rt_row = await db.fetch_one(
            """
            SELECT current_room_type, run_id
              FROM user_dungeon
             WHERE user_id = $1 AND guild_id = $2
            """,
            int(user_id), int(guild_id),
        )
        if rt_row and rt_row.get("run_id"):
            rt = str(rt_row.get("current_room_type") or "")
            if rt == "shrine":
                counts["delve_shrine_in_room"] = 1
                hints.append("\U0001F64F Shrine awaiting -- `,delve pray`.")
            elif rt == "chest":
                counts["delve_chest_in_room"] = 1
                hints.append("\U0001F4B0 Chest awaiting -- `,delve open`.")
    except Exception:
        log.debug("hub.ready: delve room probe failed", exc_info=True)

    return hints[:6], counts
