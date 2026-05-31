"""services/quests.py - daily + weekly quest assignment, progress, and claim.

Flow
----
1. ``ensure_assigned(db, uid, gid, period)`` - on first view for the
   current period_key, pick N random templates from the period pool and
   insert one row per slot. Idempotent: subsequent calls return the
   existing rows.
2. Bus listeners subscribe to the same trigger names as
   services/achievements.py and call ``progress_trigger`` to increment
   any active unclaimed quest whose trigger matches.
3. ``claim(bot, uid, gid, period, period_key, slot)`` - validate the
   slot is complete and unclaimed, mark it claimed, and pay the reward.

The trigger surface is identical to the achievements service to avoid
two parallel event fan-ins. That means a single bus event can tick both
a quest and an achievement; both writes are idempotent so order and
retries are safe.
"""
from __future__ import annotations

import datetime as _dt
import logging
import random
from typing import Any

from core.framework.scale import to_raw

import configs.quests_config as _catalog

log = logging.getLogger(__name__)


# ── Period keys ──────────────────────────────────────────────────────────────

def _today_utc() -> _dt.date:
    return _dt.datetime.utcnow().date()


def period_key(period: str, when: _dt.datetime | None = None) -> str:
    """Return the period-key string for ``period`` at ``when`` (default now).

    daily  -> 'YYYY-MM-DD' (UTC)
    weekly -> 'YYYY-Www' (ISO, UTC)
    """
    when = when or _dt.datetime.utcnow()
    if period == "daily":
        return when.strftime("%Y-%m-%d")
    if period == "weekly":
        y, w, _ = when.isocalendar()
        return f"{y}-W{w:02d}"
    raise ValueError(f"unknown period {period!r}")


def slots_for(period: str) -> int:
    if period == "daily":
        return _catalog.DAILY_SLOTS
    if period == "weekly":
        return _catalog.WEEKLY_SLOTS
    raise ValueError(f"unknown period {period!r}")


# ── Reward scaling by player progression ─────────────────────────────────────
#
# Catalog reward_usd is the BASE payout for a brand-new account. Active
# players who've climbed the chat-XP curve get more $$ for the same
# work, so a level 30 trader doesn't see the same $200 as a level 1
# alt for "Execute 8 trades today". The multiplier is multiplicative on
# the base + a small flat offset so even level 0 still pays catalog.
#
# Curve (per chat_levels.level):
#   Lv. 0  -> 1.00x (catalog)
#   Lv. 5  -> 1.25x
#   Lv. 10 -> 1.50x
#   Lv. 25 -> 2.25x
#   Lv. 50 -> 3.50x
#   Lv. 100+ -> capped at 6.00x
#
# Tunable from one constant so a future seasonal nudge is one edit.
_QUEST_REWARD_LEVEL_BONUS_PER_LEVEL: float = 0.05   # +5% per level
_QUEST_REWARD_LEVEL_MULT_CAP:        float = 6.0    # hard cap so whales don't explode


async def _player_level_for_quests(db, user_id: int, guild_id: int) -> int:
    """Best-effort lookup of the player's chat-leveling level.

    The chat_levels table is the closest thing this codebase has to a
    canonical "user level" per guild. Returns 0 on any miss so a
    fresh / unmessaged player just sees catalog rewards.
    """
    try:
        lvl = await db.fetch_val(
            "SELECT level FROM chat_levels "
            "WHERE guild_id = $1 AND user_id = $2",
            int(guild_id), int(user_id),
        )
    except Exception:
        return 0
    return max(0, int(lvl or 0))


def _scaled_reward_usd(base_usd: float, level: int) -> float:
    """Return ``base_usd`` scaled by the player's level multiplier.

    Linear in level, capped at ``_QUEST_REWARD_LEVEL_MULT_CAP``.
    """
    mult = 1.0 + max(0, int(level)) * _QUEST_REWARD_LEVEL_BONUS_PER_LEVEL
    mult = min(mult, _QUEST_REWARD_LEVEL_MULT_CAP)
    return round(float(base_usd) * mult, 2)


# ── Assignment ───────────────────────────────────────────────────────────────

async def ensure_assigned(
    db, user_id: int, guild_id: int, period: str,
) -> list[dict]:
    """Ensure the user has quests for the current ``period`` and return them.

    On first call for a new period_key this inserts N rows picked at random
    (without replacement) from the pool. Subsequent calls just read back
    the same rows so the quest set is stable for the whole period.
    """
    key = period_key(period)
    rows = await db.fetch_all(
        """
        SELECT user_id, guild_id, period, period_key, slot, quest_id,
               progress, target, reward_usd, claimed, assigned_at, claimed_at
        FROM user_quests
        WHERE user_id = $1 AND guild_id = $2
          AND period = $3 AND period_key = $4
        ORDER BY slot
        """,
        user_id, guild_id, period, key,
    )
    if rows:
        return rows

    pool = _catalog.by_period(period)
    if not pool:
        log.warning("No quest templates for period=%r", period)
        return []
    n = min(slots_for(period), len(pool))
    picks = random.sample(pool, n)

    # Scale rewards by player level. The level read happens ONCE per
    # ensure_assigned call (not per pick) so an active player and a
    # brand-new alt with the same catalog targets see proportional but
    # different payouts. Stored on the row so a level-up between
    # assignment and claim doesn't change the contract -- player sees
    # the reward they signed up for.
    player_level = await _player_level_for_quests(db, user_id, guild_id)

    for slot, tmpl in enumerate(picks):
        scaled_reward = _scaled_reward_usd(
            float(tmpl["reward_usd"]), player_level,
        )
        await db.execute(
            """
            INSERT INTO user_quests
                (user_id, guild_id, period, period_key, slot, quest_id,
                 progress, target, reward_usd)
            VALUES ($1, $2, $3, $4, $5, $6, 0, $7, $8)
            ON CONFLICT (user_id, guild_id, period, period_key, slot)
            DO NOTHING
            """,
            user_id, guild_id, period, key, slot,
            tmpl["quest_id"], int(tmpl["target"]), float(scaled_reward),
        )

    return await db.fetch_all(
        """
        SELECT user_id, guild_id, period, period_key, slot, quest_id,
               progress, target, reward_usd, claimed, assigned_at, claimed_at
        FROM user_quests
        WHERE user_id = $1 AND guild_id = $2
          AND period = $3 AND period_key = $4
        ORDER BY slot
        """,
        user_id, guild_id, period, key,
    )


async def current_for_user(
    db, user_id: int, guild_id: int,
) -> dict[str, list[dict]]:
    """Return {'daily': [...], 'weekly': [...]} for the current period keys."""
    out: dict[str, list[dict]] = {}
    for period in ("daily", "weekly"):
        out[period] = await ensure_assigned(db, user_id, guild_id, period)
    return out


# ── Progress ─────────────────────────────────────────────────────────────────

async def progress_trigger(
    db, user_id: int, guild_id: int, trigger: str, amount: int = 1,
) -> list[dict]:
    """Increment progress on every current, unclaimed quest whose trigger
    matches ``trigger`` for this user. Returns the list of quest rows that
    transitioned from incomplete to complete on this increment.

    The underlying UPDATE is scoped by period_key so it only touches the
    user's CURRENT quests, never stale rows from prior periods.
    """
    tmpls = _catalog.by_trigger(trigger)
    if not tmpls:
        return []

    # Auto-assign today's quests if the user hasn't opened ,quests yet.
    # Without this the UPDATE below matches zero rows when a user does
    # activity BEFORE viewing their quest card for the current period,
    # and the first N activities silently go uncounted. ensure_assigned
    # is idempotent + cheap when rows already exist (single indexed
    # SELECT and an early return), so it's safe on every event.
    for period in {tmpl["period"] for tmpl in tmpls}:
        try:
            await ensure_assigned(db, user_id, guild_id, period)
        except Exception as exc:
            log.error("quest ensure_assigned failed (%s): %s", period, exc)

    now_keys = {p: period_key(p) for p in ("daily", "weekly")}
    completed: list[dict] = []

    for tmpl in tmpls:
        key = now_keys.get(tmpl["period"])
        if key is None:
            continue
        # Increment and flag crossings in a single round trip.
        row = await db.fetch_one(
            """
            UPDATE user_quests
               SET progress = LEAST(target, progress + $5)
             WHERE user_id = $1 AND guild_id = $2
               AND period = $3 AND period_key = $4
               AND quest_id = $6 AND NOT claimed
               AND progress < target
            RETURNING slot, quest_id, progress, target, reward_usd
            """,
            user_id, guild_id, tmpl["period"], key,
            int(amount), tmpl["quest_id"],
        )
        if row and int(row["progress"]) >= int(row["target"]):
            completed.append({
                "slot": int(row["slot"]),
                "period": tmpl["period"],
                "period_key": key,
                "quest_id": row["quest_id"],
                "reward_usd": float(row["reward_usd"]),
            })
    return completed


# ── Claim ────────────────────────────────────────────────────────────────────

async def claim(
    bot, user_id: int, guild_id: int,
    period: str, slot: int,
) -> tuple[bool, str, float]:
    """Claim a completed quest slot. Returns (ok, message, reward_usd)."""
    db = bot.db
    key = period_key(period)
    row = await db.fetch_one(
        """
        SELECT quest_id, progress, target, reward_usd, claimed
        FROM user_quests
        WHERE user_id = $1 AND guild_id = $2
          AND period = $3 AND period_key = $4 AND slot = $5
        """,
        user_id, guild_id, period, key, slot,
    )
    if row is None:
        return False, "Quest not found.", 0.0
    if row["claimed"]:
        return False, "Already claimed.", 0.0
    if int(row["progress"]) < int(row["target"]):
        return False, "Quest not complete yet.", 0.0

    reward = float(row["reward_usd"] or 0.0)
    tx_hash = ""
    if reward > 0:
        await db.update_wallet(user_id, guild_id, to_raw(reward))
        tx_hash = await db.log_tx(
            guild_id, user_id, "QUEST_REWARD",
            symbol_out="USD", amount_out=to_raw(reward),
            network="usd",
        )

    await db.execute(
        """
        UPDATE user_quests
           SET claimed = TRUE, claimed_at = NOW()
         WHERE user_id = $1 AND guild_id = $2
           AND period = $3 AND period_key = $4 AND slot = $5
        """,
        user_id, guild_id, period, key, slot,
    )
    try:
        await bot.bus.publish(
            "quest_claimed",
            guild=bot.get_guild(guild_id),
            user_id=user_id,
            quest_id=row["quest_id"],
            period=period,
            reward_usd=reward,
            tx_hash=tx_hash,
        )
    except Exception as exc:
        log.error("quest_claimed publish failed: %s", exc)
    return True, "", reward


# ── Bus listeners ────────────────────────────────────────────────────────────

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


def attach_listeners(bot) -> None:
    """Subscribe quest progress to the same bus events achievements use."""
    bus = bot.bus
    db = bot.db

    def _bumper(trigger: str):
        async def _cb(**kw) -> None:
            uid = _extract_uid(kw.get("user") or kw.get("user_id"))
            gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
            if not (uid and gid):
                return
            try:
                await progress_trigger(db, uid, gid, trigger)
            except Exception as exc:
                log.error("quest progress %s failed: %s", trigger, exc)
        return _cb

    async def _on_pow_tick(**kw) -> None:
        payouts = kw.get("payouts") or []
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not gid:
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
                try:
                    await progress_trigger(db, uid, gid, "block_mined")
                except Exception as exc:
                    log.error("quest block_mined failed: %s", exc)

    async def _on_gamble(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await progress_trigger(db, uid, gid, "gamble_play")
        if kw.get("won") or float(kw.get("delta", 0) or 0) > 0:
            await progress_trigger(db, uid, gid, "gamble_win")

    async def _on_exploit(**kw) -> None:
        # cogs/eat_the_rich.py publishes with attacker= (not user=/user_id=).
        uid = _extract_uid(
            kw.get("user") or kw.get("user_id") or kw.get("attacker")
        )
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await progress_trigger(db, uid, gid, "exploit_run")
        if kw.get("won") or kw.get("success"):
            await progress_trigger(db, uid, gid, "exploit_win")

    async def _on_ape(**kw) -> None:
        # ,ape routes through gamble triggers so Feeling Lucky / weekly
        # gamble quests count apes.
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await progress_trigger(db, uid, gid, "gamble_play")
        if float(kw.get("net", 0) or 0) > 0:
            await progress_trigger(db, uid, gid, "gamble_win")

    async def _on_mining_tick_complete(**kw) -> None:
        # SUN/MTA PoW path. See achievements._on_mining_tick_complete for
        # payload shape notes.
        summary = kw.get("summary") or {}
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not gid:
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
            try:
                await progress_trigger(db, uid, gid, "block_mined")
            except Exception as exc:
                log.error("quest block_mined (SUN) failed: %s", exc)

    async def _on_chat_level_up(**kw) -> None:
        # Any level-up counts as +1 progress for chat_level_up quests --
        # quests want "did you ding a level today", not "what level are you".
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        try:
            await progress_trigger(db, uid, gid, "chat_level_up")
        except Exception as exc:
            log.error("quest chat_level_up failed: %s", exc)

    bus.subscribe("work_completed", _bumper("work_completed"))
    bus.subscribe("daily_claimed", _bumper("daily_claimed"))
    bus.subscribe("trade", _bumper("trade_executed"))
    bus.subscribe("trade_executed", _bumper("trade_executed"))
    bus.subscribe("swap_trade", _bumper("swap_executed"))
    bus.subscribe("swap_executed", _bumper("swap_executed"))
    bus.subscribe("staked", _bumper("stake_created"))
    bus.subscribe("lp_added", _bumper("lp_added"))
    bus.subscribe("deposit", _bumper("bank_deposit"))
    bus.subscribe("buddy_adopted", _bumper("buddy_adopted"))
    bus.subscribe("buddy_battle_win", _bumper("buddy_battle_win"))
    bus.subscribe("buddy_battle_loss", _bumper("buddy_battle_loss"))
    bus.subscribe("buddy_arena_spawn", _bumper("buddy_arena_spawn"))
    bus.subscribe("buddy_arena_won", _bumper("buddy_arena_won"))
    bus.subscribe("buddy_arena_lost", _bumper("buddy_arena_lost"))
    bus.subscribe("validator_registered", _bumper("validator_registered"))
    bus.subscribe("chat_level_up", _on_chat_level_up)
    bus.subscribe("pow_mining_tick", _on_pow_tick)
    bus.subscribe("mining_tick_complete", _on_mining_tick_complete)
    bus.subscribe("gamble_result", _on_gamble)
    bus.subscribe("exploit_completed", _on_exploit)
    bus.subscribe("ape_completed", _on_ape)
    bus.subscribe("buddy_stored", _bumper("buddy_stored"))
    bus.subscribe(
        "daycare_egg_collected", _bumper("daycare_egg_collected"),
    )
    bus.subscribe("specialty_level_up", _bumper("specialty_level_up"))
    # Auction-house triggers so a future quest pool can ask "list 3
    # items today" / "buy something this week" without re-plumbing.
    bus.subscribe("ah_listing_created", _bumper("ah_listing_created"))
    bus.subscribe("ah_sale_settled", _bumper("ah_sale_settled"))
    bus.subscribe("ah_purchase_settled", _bumper("ah_purchase_settled"))

    # Expeditions: every send/collect ticks the generic counter; collect
    # also fans out a per-destination trigger so a quest pool can ask
    # "complete a Reef expedition this week" without a custom handler.
    async def _on_expedition_collected(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        try:
            await progress_trigger(db, uid, gid, "expedition_collected")
            dest = str(kw.get("destination") or "").lower()
            if dest:
                await progress_trigger(
                    db, uid, gid, f"expedition_collected_{dest}",
                )
        except Exception as exc:
            log.error("quest expedition_collected failed: %s", exc)

    bus.subscribe("expedition_started", _bumper("expedition_started"))
    bus.subscribe("expedition_collected", _on_expedition_collected)
