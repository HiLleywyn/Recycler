"""V3 Pillar 2 service: Apex Mastery state machine + passive lookup.

Public surface:

    add_mastery(db, uid, gid, track, xp)
    unlock_node(db, uid, gid, node_id)
    reset_tree(db, uid, gid)            -- paid wipe
    mastery_summary(db, uid, gid)
    passives(db, uid, gid)              -- {effect_key: cumulative_value}
    apply(passives, key, default=0.0)   -- pure helper for consumers

Effects are applied at READ time by each consumer (savings APR, daily
bonus, fishing catch rate, etc.) -- the service writes no per-event
state changes outside of XP / node unlocks.

Per CLAUDE.md: net worth is queried from services/net_worth.py;
monetary writes go through core/framework/scale.to_raw. No re-deriving.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.config import Config
from core.framework.scale import to_raw
from configs.mastery_config import (
    NODES_BY_ID,
    TRACKS,
    TRACK_MAX_LEVEL,
    level_for_xp,
    points_for_level,
    xp_for_level,
)

log = logging.getLogger(__name__)


@dataclass
class MasteryAdd:
    """Side-effects of an XP grant."""
    track: str
    xp_before: int
    xp_after: int
    level_before: int
    level_after: int
    points_granted: int = 0
    leveled_up: bool = False


@dataclass
class MasterySummary:
    """Read-only roll-up shown by ``,mastery``."""
    tracks: dict[str, dict] = field(default_factory=dict)
    points_available: int = 0
    points_spent: int = 0
    unlocked: set[str] = field(default_factory=set)
    resets_used: int = 0


# ── Internals ──────────────────────────────────────────────────────────
async def _ensure_track_row(db, gid: int, uid: int, track: str) -> dict:
    row = await db.fetch_one(
        "SELECT * FROM user_mastery WHERE guild_id=$1 AND user_id=$2 AND track=$3",
        gid, uid, track,
    )
    if row:
        return dict(row)
    await db.execute(
        "INSERT INTO user_mastery (guild_id, user_id, track) "
        "VALUES ($1, $2, $3)",
        gid, uid, track,
    )
    return {"guild_id": gid, "user_id": uid, "track": track, "xp": 0, "level": 1}


async def _ensure_meta_row(db, gid: int, uid: int) -> dict:
    row = await db.fetch_one(
        "SELECT * FROM user_mastery_meta WHERE guild_id=$1 AND user_id=$2",
        gid, uid,
    )
    if row:
        return dict(row)
    await db.execute(
        "INSERT INTO user_mastery_meta (guild_id, user_id) VALUES ($1, $2)",
        gid, uid,
    )
    return {
        "guild_id": gid, "user_id": uid,
        "points_spent": 0, "points_available": 0, "resets_used": 0,
    }


# ── Public: XP grant (called by every minigame end-of-action) ─────────
async def add_mastery(
    db, uid: int, gid: int, track: str, xp: int,
) -> MasteryAdd:
    """Grant ``xp`` to a track. Idempotent on DB failure (returns no-op).

    The minigame call sites pass small per-action XP (10-200 typical).
    A level-up grants ``mastery_config.points_for_level`` minus the
    previous-level value, which is then bankable in
    ``user_mastery_meta.points_available``.
    """
    if track not in TRACKS or xp <= 0:
        return MasteryAdd(
            track=track, xp_before=0, xp_after=0,
            level_before=1, level_after=1,
        )
    try:
        row = await _ensure_track_row(db, gid, uid, track)
        xp_before = int(row.get("xp") or 0)
        lvl_before = int(row.get("level") or 1)
        xp_after = xp_before + int(xp)
        lvl_after = min(TRACK_MAX_LEVEL, level_for_xp(xp_after))
        await db.execute(
            "UPDATE user_mastery SET xp=$4, level=$5, updated_at=now() "
            "WHERE guild_id=$1 AND user_id=$2 AND track=$3",
            gid, uid, track, xp_after, lvl_after,
        )
        granted = 0
        if lvl_after > lvl_before:
            pts_before = points_for_level(lvl_before)
            pts_after = points_for_level(lvl_after)
            granted = max(0, pts_after - pts_before)
            if granted > 0:
                await _ensure_meta_row(db, gid, uid)
                await db.execute(
                    "UPDATE user_mastery_meta "
                    "SET points_available = points_available + $3 "
                    "WHERE guild_id=$1 AND user_id=$2",
                    gid, uid, granted,
                )
        return MasteryAdd(
            track=track,
            xp_before=xp_before, xp_after=xp_after,
            level_before=lvl_before, level_after=lvl_after,
            points_granted=granted,
            leveled_up=lvl_after > lvl_before,
        )
    except Exception:
        log.exception(
            "mastery: add_mastery failed gid=%s uid=%s track=%s",
            gid, uid, track,
        )
        return MasteryAdd(
            track=track, xp_before=0, xp_after=0,
            level_before=1, level_after=1,
        )


# ── Public: node unlock ────────────────────────────────────────────────
async def unlock_node(db, uid: int, gid: int, node_id: str) -> tuple[bool, str]:
    """Spend mastery points to unlock a node.

    Returns ``(ok, message)``. Failure cases:
      - unknown node id
      - prereqs not satisfied
      - not enough points
      - already unlocked
    """
    node = NODES_BY_ID.get(node_id)
    if not node:
        return False, f"Unknown node `{node_id}`."
    meta = await _ensure_meta_row(db, gid, uid)
    # Already unlocked?
    existing = await db.fetch_one(
        "SELECT 1 FROM user_mastery_nodes "
        "WHERE guild_id=$1 AND user_id=$2 AND node_id=$3",
        gid, uid, node_id,
    )
    if existing:
        return False, f"`{node['name']}` is already unlocked."
    # Prereqs
    for pre in node.get("prereqs", []):
        ok = await db.fetch_one(
            "SELECT 1 FROM user_mastery_nodes "
            "WHERE guild_id=$1 AND user_id=$2 AND node_id=$3",
            gid, uid, pre,
        )
        if not ok:
            pre_name = NODES_BY_ID.get(pre, {}).get("name", pre)
            return False, f"Locked: requires `{pre_name}` first."
    cost = int(node["cost"])
    if int(meta.get("points_available") or 0) < cost:
        return False, (
            f"Need {cost} point{'s' if cost != 1 else ''}; "
            f"you have {meta.get('points_available')}."
        )
    try:
        async with db.atomic():
            await db.execute(
                "INSERT INTO user_mastery_nodes (guild_id, user_id, node_id) "
                "VALUES ($1, $2, $3)",
                gid, uid, node_id,
            )
            await db.execute(
                "UPDATE user_mastery_meta "
                "SET points_available = points_available - $3, "
                "    points_spent = points_spent + $3 "
                "WHERE guild_id=$1 AND user_id=$2",
                gid, uid, cost,
            )
    except Exception:
        log.exception(
            "mastery: unlock failed gid=%s uid=%s node=%s", gid, uid, node_id,
        )
        return False, "Unlock failed -- try again."
    return True, f"Unlocked **{node['name']}** ({node['description']})."


# ── Public: paid reset ─────────────────────────────────────────────────
async def reset_tree(db, uid: int, gid: int) -> tuple[bool, str, float]:
    """Refund every spent node. Cost doubles each reset (mirrors delve/buddy)."""
    meta = await _ensure_meta_row(db, gid, uid)
    used = int(meta.get("resets_used") or 0)
    base = float(getattr(Config, "MASTERY_RESET_BASE_USD", 25_000.0))
    cost_usd = base * (2 ** used)
    try:
        user = await db.get_user(uid, gid)
    except Exception:
        return False, "User lookup failed.", cost_usd
    wallet = int(user.get("wallet") or 0) if user else 0
    bank = int(user.get("bank") or 0) if user else 0
    cost_raw = to_raw(cost_usd)
    if wallet + bank < cost_raw:
        return False, f"Reset costs ${cost_usd:,.2f}; you can't cover it.", cost_usd

    # Refund every node's cost to points_available.
    rows = await db.fetch_all(
        "SELECT node_id FROM user_mastery_nodes "
        "WHERE guild_id=$1 AND user_id=$2",
        gid, uid,
    )
    refunded = sum(int(NODES_BY_ID.get(r["node_id"], {}).get("cost", 0)) for r in rows)
    try:
        async with db.atomic():
            # Debit cost across wallet -> bank fallback.
            from_wallet = min(wallet, cost_raw)
            from_bank = cost_raw - from_wallet
            if from_wallet > 0:
                await db.update_wallet(uid, gid, -from_wallet)
            if from_bank > 0:
                await db.execute(
                    "UPDATE users SET bank = bank - $3 "
                    "WHERE user_id=$1 AND guild_id=$2",
                    uid, gid, from_bank,
                )
            await db.execute(
                "DELETE FROM user_mastery_nodes WHERE guild_id=$1 AND user_id=$2",
                gid, uid,
            )
            await db.execute(
                "UPDATE user_mastery_meta SET "
                "  points_available = points_available + $3, "
                "  points_spent = 0, "
                "  resets_used = resets_used + 1, "
                "  last_reset_at = now() "
                "WHERE guild_id=$1 AND user_id=$2",
                gid, uid, refunded,
            )
    except Exception:
        log.exception(
            "mastery: reset failed gid=%s uid=%s", gid, uid,
        )
        return False, "Reset failed -- try again.", cost_usd
    return True, (
        f"Reset complete. Refunded **{refunded}** points; "
        f"charged **${cost_usd:,.2f}**."
    ), cost_usd


# ── Public: summary ────────────────────────────────────────────────────
async def mastery_summary(db, uid: int, gid: int) -> MasterySummary:
    out = MasterySummary()
    try:
        tracks_rows = await db.fetch_all(
            "SELECT * FROM user_mastery WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
        for r in tracks_rows:
            t = str(r["track"])
            xp = int(r.get("xp") or 0)
            lvl = int(r.get("level") or 1)
            next_thr = xp_for_level(lvl + 1)
            this_thr = xp_for_level(lvl)
            denom = max(1, next_thr - this_thr)
            progress = max(0.0, min(1.0, (xp - this_thr) / denom))
            out.tracks[t] = {
                "level": lvl, "xp": xp,
                "next_threshold": next_thr,
                "progress": progress,
            }
        meta = await _ensure_meta_row(db, gid, uid)
        out.points_available = int(meta.get("points_available") or 0)
        out.points_spent = int(meta.get("points_spent") or 0)
        out.resets_used = int(meta.get("resets_used") or 0)
        node_rows = await db.fetch_all(
            "SELECT node_id FROM user_mastery_nodes "
            "WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
        out.unlocked = {str(r["node_id"]) for r in node_rows}
    except Exception:
        log.exception("mastery: summary failed gid=%s uid=%s", gid, uid)
    return out


# ── Public: passive effect lookup ──────────────────────────────────────
async def passives(db, uid: int, gid: int) -> dict[str, float]:
    """Return ``{effect_key: cumulative_value}`` for the user.

    Each consumer reads ONE key (e.g. `econ.daily_bonus`) and applies
    it via ``apply()``. The dict is intentionally flat so callers
    don't need to know the node tree.
    """
    out: dict[str, float] = {}
    try:
        rows = await db.fetch_all(
            "SELECT node_id FROM user_mastery_nodes "
            "WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
    except Exception:
        return out
    for r in rows:
        node = NODES_BY_ID.get(str(r["node_id"]))
        if not node:
            continue
        key = node.get("effect_key")
        val = float(node.get("effect_value", 0.0))
        if not key:
            continue
        out[key] = out.get(key, 0.0) + val
    return out


def apply(passives_dict: dict[str, float], key: str, default: float = 0.0) -> float:
    """Read a single effect value from the passives dict.

    Pure helper so consumers don't trip over a missing key. Default
    is the no-effect value (typically 0 for additive bonuses, 1 for
    multiplicative scalars -- pass whatever fits the math at the
    call site).
    """
    return float(passives_dict.get(key, default))


# ── Single-call consumer helper ────────────────────────────────────────
# Most consumer call sites are one-liners: read the passive, modulate
# one value, return. ``apply_passive`` collapses the three-step
# (passives() -> apply() -> multiply) pattern into one await so each
# consumer site is a single line. Modes:
#     "add" -- base + bonus       (used for additive odds: drop chance, crit, etc.)
#     "mul" -- base * (1 + bonus) (used for multiplicative payouts: daily, yield)
#     "cut" -- base * (1 - bonus) (used for fee / cooldown reductions)
#     "flat"-- bonus directly     (used for boolean / integer flags like extra_slot)
#
# Bonus is read from a single effect_key in ``mastery_config.NODES``.
# All four modes clamp at sane bounds (cuts can't push base below 0,
# muls can't go negative).

async def apply_passive(
    db,
    uid: int,
    gid: int,
    key: str,
    base: float,
    *,
    mode: str = "mul",
) -> float:
    """Apply the mastery passive ``key`` to ``base`` and return the result.

    Reads the player's full passives dict on each call. This is a single
    SELECT per consumer per action; the cost is bounded and consumers
    don't have to thread the dict through their function signatures.
    Callers that need many passives in a row should read passives()
    directly and use ``apply()`` repeatedly to avoid re-querying.
    """
    p = await passives(db, int(uid), int(gid))
    bonus = float(p.get(str(key), 0.0))
    if bonus == 0.0:
        return float(base)
    if mode == "add":
        return float(base) + bonus
    if mode == "mul":
        return float(base) * max(0.0, 1.0 + bonus)
    if mode == "cut":
        return float(base) * max(0.0, 1.0 - bonus)
    if mode == "flat":
        return float(bonus)
    # Fallback: treat unknown mode as "mul"
    return float(base) * max(0.0, 1.0 + bonus)


def apply_to_base(
    passives_dict: dict[str, float],
    key: str,
    base: float,
    *,
    mode: str = "mul",
) -> float:
    """Sync mirror of ``apply_passive`` when the caller already has the dict.

    Use this inside a tight loop where ``passives()`` has already been
    read once. Saves a DB round-trip per call.
    """
    bonus = float((passives_dict or {}).get(str(key), 0.0))
    if bonus == 0.0:
        return float(base)
    if mode == "add":
        return float(base) + bonus
    if mode == "mul":
        return float(base) * max(0.0, 1.0 + bonus)
    if mode == "cut":
        return float(base) * max(0.0, 1.0 - bonus)
    if mode == "flat":
        return float(bonus)
    return float(base) * max(0.0, 1.0 + bonus)


# ── Per-action XP grant helper ─────────────────────────────────────────
# Every minigame end-of-action calls add_mastery with a track + xp. The
# xp is derived from the USD value of that action (cashout value, steal
# value, paid wager, etc.). Centralising the USD -> XP formula keeps
# every track on the same balance curve and gives us ONE place to tune
# when the level pace is off.
#
# Player report tuning the previous /10 (raider was /5) divisor: a single
# Full Protocol Heist that stole $50k produced 10k XP which is past
# level 20 in one action -- "Raider L27 ... I've hardly delved at all".
# Cranked the divisor to /50 and added a per-action cap so one whale-tier
# action can't single-shot mid-tier. New shape:
#   $50 cashout    ->    1 XP
#   $500 cashout   ->   10 XP
#   $5k cashout    ->  100 XP
#   $50k cashout   -> 1000 XP
#   $100k cashout  -> 1500 XP (capped)
# Level curve unchanged: L10 ~ 1,675 XP, L20 ~ 8.8k, L50 ~ 627k. So a
# new player who plays a few rounds lands at L3-L6, not L27.

USD_PER_XP: float = 50.0
"""Dollars of in-game USD value per 1 XP. Tune to slow/speed levelling."""

XP_PER_ACTION_CAP: int = 1500
"""Max XP a single minigame action can grant, regardless of USD scale."""


def xp_for_action(usd_value: float, *, multiplier: float = 1.0) -> int:
    """Map a USD-denominated minigame action to a per-action XP grant.

    One formula, one place to tune. ``multiplier`` lets a specific track
    be marginally more or less generous than the global curve (default
    1.0 keeps everyone on the same scale). Returns an integer clamped
    to ``[1, XP_PER_ACTION_CAP]``; 0 USD returns 0 so a no-op action
    doesn't drip XP.
    """
    try:
        usd = float(usd_value)
    except (TypeError, ValueError):
        return 0
    if usd <= 0.0:
        return 0
    raw = int(round(usd * float(multiplier) / USD_PER_XP))
    return max(1, min(XP_PER_ACTION_CAP, raw))


# ── Per-micro-action bus listeners ─────────────────────────────────────
# The previous design only granted track XP at the END of a minigame
# loop (delve cashout, fish reel cashout, farm harvest cashout, craft
# forge cashout). Players reasonably expect EVERY catch / harvest /
# craft / kill to nudge the relevant track up -- otherwise fishing all
# afternoon shows no fisher progress until you cashout, and a player
# who never cashes out an aborted run gets nothing despite the play.
#
# The listeners below subscribe to the same bus events the achievement
# engine already uses (services.achievements.attach_listeners) and
# grant a small flat XP per micro-action. Cashout still does the big
# USD-scaled grant via xp_for_action -- micro-actions are the
# "fishing-in-the-background feels alive" baseline.

# Small flat XP grants per micro-action (much smaller than a cashout)
# so an active player who fishes 100 times gets 100 * 5 = 500 XP
# (~ L4) from micro-actions alone, and the bigger cashout grant still
# accounts for the majority of progress on a serious play session.
_MICRO_XP: dict[str, tuple[str, int]] = {
    # event_name : (track, xp)
    "fish_caught":            ("fisher",    5),
    "fish_legendary":         ("fisher",   25),
    "farm_harvest":           ("farmer",    5),
    "farm_legendary_harvest": ("farmer",   25),
    "farm_pest_kill":         ("farmer",    3),
    "farm_boss_pest_kill":    ("farmer",   15),
    "craft_made":             ("crafter",   5),
    "craft_legendary":        ("crafter",  25),
    "delve_kill":             ("delver",    5),
    "delve_boss_kill":        ("delver",   25),
    "delve_wild_battle_won":  ("delver",   10),
    "delve_clear_run":        ("delver",   15),
    "trade_executed":         ("trader",    3),
    "swap_executed":          ("trader",    3),
    "gamble_win":             ("gambler",   5),
    "buddy_battle_win":       ("tamer",     5),
    "buddy_arena_won":        ("tamer",    10),
    "buddy_adopted":          ("tamer",    10),
    "exploit_win":            ("raider",    5),
    "stake_created":          ("validator", 5),
    "stake_reward":           ("validator", 3),
    "validator_registered":   ("validator", 25),
    "sage_correct":           ("sage_scholar", 5),
    "sage_streak_10":         ("sage_scholar", 25),
    "sage_streak_25":         ("sage_scholar", 75),
}


def attach_listeners(bot) -> None:
    """Subscribe per-micro-action mastery XP grants to the event bus.

    Mirrors ``services.achievements.attach_listeners`` -- same events,
    same dispatch shape, separate small XP grant for each. Failures
    inside a handler are swallowed (log + continue) so a single broken
    event never stops the whole bus.

    Idempotent at the framework.bus level: re-attaching is safe.
    """
    bus = bot.bus

    def _extract_ids(kw: dict) -> tuple[int | None, int | None]:
        # Mirror achievements.py: accept user= or user_id=, guild= or
        # guild_id=, with discord.Member / discord.Guild objects too.
        u = kw.get("user_id")
        if u is None:
            obj = kw.get("user")
            u = getattr(obj, "id", obj) if obj is not None else None
        g = kw.get("guild_id")
        if g is None:
            obj = kw.get("guild")
            g = getattr(obj, "id", obj) if obj is not None else None
        try:
            return (int(u) if u is not None else None,
                    int(g) if g is not None else None)
        except (TypeError, ValueError):
            return None, None

    def _make_handler(track: str, xp: int):
        async def _handler(**kw) -> None:
            uid, gid = _extract_ids(kw)
            if uid is None or gid is None:
                return
            try:
                await add_mastery(bot.db, uid, gid, track, xp)
            except Exception:
                log.debug(
                    "mastery: micro-xp grant failed event=%s uid=%s gid=%s",
                    kw.get("__event__", "?"), uid, gid, exc_info=True,
                )
        return _handler

    for event_name, (track, xp) in _MICRO_XP.items():
        try:
            bus.subscribe(event_name, _make_handler(track, xp))
        except Exception:
            log.warning(
                "mastery: subscribe failed for %s", event_name, exc_info=True,
            )
