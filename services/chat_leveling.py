"""
services/chat_leveling.py  -  Chat-XP math, persistence, and Discord role sync.

The per-guild XP curve mirrors the classic MEE6 progression by default:
``xp_for_level_up(n) = quad * n^2 + lin * n + base`` with (5, 50, 100).  A
guild admin can tune the three coefficients via ``chat_level_config`` so the
progression tracks whatever bot the server is migrating from.  ``xp`` in
``chat_levels`` is the CUMULATIVE total -- never the within-level residual --
so importing from another bot only needs their total-XP value (or a level we
can invert into total XP).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from database.database import PgDatabase

log = logging.getLogger(__name__)


# ── Config dataclass ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LevelingConfig:
    """Per-guild leveling configuration."""

    enabled: bool = False
    xp_min: int = 15
    xp_max: int = 25
    cooldown_seconds: int = 60
    min_chars: int = 4
    announce_channel: int | None = None
    dm_levelup: bool = False
    stack_roles: bool = True
    curve_quad: int = 5
    curve_lin: int = 50
    curve_base: int = 100
    streak_max_days: int = 10
    streak_pct_per_day: int = 1


_DEFAULT_CFG = LevelingConfig()


# ── Curve math ──────────────────────────────────────────────────────────────

def xp_for_level_up(level: int, cfg: LevelingConfig = _DEFAULT_CFG) -> int:
    """XP required to advance from *level* to *level + 1*."""
    if level < 0:
        level = 0
    return cfg.curve_quad * level * level + cfg.curve_lin * level + cfg.curve_base


def total_xp_for_level(level: int, cfg: LevelingConfig = _DEFAULT_CFG) -> int:
    """Cumulative XP needed to reach *level* from level 0."""
    if level <= 0:
        return 0
    total = 0
    for n in range(level):
        total += xp_for_level_up(n, cfg)
    return total


def level_from_total_xp(total_xp: int, cfg: LevelingConfig = _DEFAULT_CFG) -> int:
    """Highest level reachable with *total_xp* cumulative XP."""
    if total_xp <= 0:
        return 0
    level = 0
    needed = 0
    while True:
        step = xp_for_level_up(level, cfg)
        if needed + step > total_xp:
            return level
        needed += step
        level += 1


def progress_to_next(total_xp: int, cfg: LevelingConfig = _DEFAULT_CFG) -> tuple[int, int, int]:
    """Return (current_level, xp_into_level, xp_needed_for_next_level)."""
    level = level_from_total_xp(total_xp, cfg)
    floor_xp = total_xp_for_level(level, cfg)
    needed = xp_for_level_up(level, cfg)
    return level, total_xp - floor_xp, needed


# ── Config load / save ──────────────────────────────────────────────────────

async def get_config(db: "PgDatabase", guild_id: int) -> LevelingConfig:
    row = await db.fetch_one(
        "SELECT enabled, xp_min, xp_max, cooldown_seconds, min_chars, "
        "announce_channel, dm_levelup, stack_roles, curve_quad, curve_lin, curve_base, "
        "streak_max_days, streak_pct_per_day "
        "FROM chat_level_config WHERE guild_id=$1",
        guild_id,
    )
    if not row:
        return _DEFAULT_CFG
    return LevelingConfig(
        enabled=bool(row["enabled"]),
        xp_min=int(row["xp_min"]),
        xp_max=int(row["xp_max"]),
        cooldown_seconds=int(row["cooldown_seconds"]),
        min_chars=int(row["min_chars"]),
        announce_channel=int(row["announce_channel"]) if row.get("announce_channel") else None,
        dm_levelup=bool(row["dm_levelup"]),
        stack_roles=bool(row["stack_roles"]),
        curve_quad=int(row["curve_quad"]),
        curve_lin=int(row["curve_lin"]),
        curve_base=int(row["curve_base"]),
        streak_max_days=int(row["streak_max_days"]),
        streak_pct_per_day=int(row["streak_pct_per_day"]),
    )


_ALLOWED_CFG_COLS = frozenset({
    "enabled", "xp_min", "xp_max", "cooldown_seconds", "min_chars",
    "announce_channel", "dm_levelup", "stack_roles",
    "curve_quad", "curve_lin", "curve_base",
    "streak_max_days", "streak_pct_per_day",
})


async def set_config_field(db: "PgDatabase", guild_id: int, column: str, value) -> None:
    """Upsert a single config column for a guild. Raises on unknown column."""
    if column not in _ALLOWED_CFG_COLS:
        raise ValueError(f"Unknown leveling config column: {column}")
    await db.execute(
        f"INSERT INTO chat_level_config (guild_id, {column}) VALUES ($1, $2) "
        f"ON CONFLICT (guild_id) DO UPDATE SET {column}=EXCLUDED.{column}, updated_at=NOW()",
        guild_id, value,
    )


# ── Per-user state ──────────────────────────────────────────────────────────

async def get_user(db: "PgDatabase", guild_id: int, user_id: int) -> dict:
    """Return the user's leveling row, inserting a zero row if absent."""
    row = await db.fetch_one(
        "SELECT xp, level, total_messages, last_message_at, streak_days, "
        "last_active_date, display_name "
        "FROM chat_levels WHERE guild_id=$1 AND user_id=$2",
        guild_id, user_id,
    )
    if row:
        return dict(row)
    return {
        "xp": 0, "level": 0, "total_messages": 0, "last_message_at": None,
        "streak_days": 0, "last_active_date": None, "display_name": None,
    }


async def add_xp(
    db: "PgDatabase", guild_id: int, user_id: int, amount: int,
    cfg: LevelingConfig, *, display_name: str | None = None,
) -> tuple[int, int, int, int]:
    """Atomically credit *amount* XP to a user.

    Returns (old_level, new_level, new_total_xp, new_total_messages).
    If *display_name* is provided, it's cached on the row so leaderboards
    can render a readable name for users who've left the guild.
    """
    async with db.atomic() as conn:
        row = await conn.fetchrow(
            "SELECT xp, level FROM chat_levels "
            "WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
            guild_id, user_id,
        )
        old_xp = int(row["xp"]) if row else 0
        old_level = int(row["level"]) if row else 0
        new_xp = old_xp + max(0, int(amount))
        new_level = level_from_total_xp(new_xp, cfg)
        row2 = await conn.fetchrow(
            "INSERT INTO chat_levels (guild_id, user_id, xp, level, total_messages, "
            "last_message_at, display_name, updated_at) "
            "VALUES ($1, $2, $3, $4, 1, NOW(), $5, NOW()) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
            "xp=EXCLUDED.xp, level=EXCLUDED.level, "
            "total_messages=chat_levels.total_messages + 1, "
            "last_message_at=NOW(), "
            "display_name=COALESCE(EXCLUDED.display_name, chat_levels.display_name), "
            "updated_at=NOW() "
            "RETURNING total_messages",
            guild_id, user_id, new_xp, new_level, display_name,
        )
        total_msgs = int(row2["total_messages"]) if row2 else 1
    return old_level, new_level, new_xp, total_msgs


async def apply_streak(
    db: "PgDatabase", guild_id: int, user_id: int, cfg: LevelingConfig,
) -> tuple[int, float]:
    """Bump/maintain the user's daily streak and return (streak_days, xp_multiplier).

    Same-day chat leaves the streak unchanged.  A gap of exactly one calendar
    day extends the streak.  A gap of two or more days resets to 1.
    """
    import datetime as _dt
    today = _dt.date.today()

    async with db.atomic() as conn:
        row = await conn.fetchrow(
            "SELECT streak_days, last_active_date FROM chat_levels "
            "WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
            guild_id, user_id,
        )
        if row is None or row["last_active_date"] is None:
            new_streak = 1
        else:
            last = row["last_active_date"]
            if hasattr(last, "date"):
                last = last.date()
            prev_streak = int(row["streak_days"] or 0)
            if last == today:
                new_streak = max(1, prev_streak)
            elif last == today - _dt.timedelta(days=1):
                new_streak = prev_streak + 1
            else:
                new_streak = 1

        await conn.execute(
            "INSERT INTO chat_levels (guild_id, user_id, streak_days, last_active_date, updated_at) "
            "VALUES ($1, $2, $3, $4, NOW()) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
            "streak_days=EXCLUDED.streak_days, "
            "last_active_date=EXCLUDED.last_active_date, "
            "updated_at=NOW()",
            guild_id, user_id, new_streak, today,
        )

    cap = max(0, int(cfg.streak_max_days))
    pct = max(0, int(cfg.streak_pct_per_day))
    capped = min(new_streak, cap)
    multiplier = 1.0 + (capped * pct) / 100.0
    return new_streak, multiplier


async def set_user_level(
    db: "PgDatabase", guild_id: int, user_id: int, level: int,
    cfg: LevelingConfig, *, reset_messages: bool = False,
    display_name: str | None = None,
) -> int:
    """Set a user's level directly, computing total XP from the curve.

    Returns the new total XP.  Used by the CSV importer when the source data
    only provides a level (no XP).  If *display_name* is provided, it's
    cached on the row.
    """
    level = max(0, int(level))
    total_xp = total_xp_for_level(level, cfg)
    if reset_messages:
        await db.execute(
            "INSERT INTO chat_levels "
            "(guild_id, user_id, xp, level, total_messages, display_name, updated_at) "
            "VALUES ($1, $2, $3, $4, 0, $5, NOW()) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
            "xp=EXCLUDED.xp, level=EXCLUDED.level, total_messages=0, "
            "display_name=COALESCE(EXCLUDED.display_name, chat_levels.display_name), "
            "updated_at=NOW()",
            guild_id, user_id, total_xp, level, display_name,
        )
    else:
        await db.execute(
            "INSERT INTO chat_levels "
            "(guild_id, user_id, xp, level, total_messages, display_name, updated_at) "
            "VALUES ($1, $2, $3, $4, 0, $5, NOW()) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
            "xp=EXCLUDED.xp, level=EXCLUDED.level, "
            "display_name=COALESCE(EXCLUDED.display_name, chat_levels.display_name), "
            "updated_at=NOW()",
            guild_id, user_id, total_xp, level, display_name,
        )
    return total_xp


async def set_user_streak(
    db: "PgDatabase", guild_id: int, user_id: int,
    streak_days: int, last_active_date,
) -> None:
    """Set a user's streak_days and last_active_date directly.

    ``last_active_date`` may be a ``datetime.date``, ``datetime.datetime``, or
    None.  ``streak_days`` is clamped to >= 0.
    """
    import datetime as _dt
    streak_days = max(0, int(streak_days))
    if isinstance(last_active_date, _dt.datetime):
        last_active_date = last_active_date.date()
    await db.execute(
        "INSERT INTO chat_levels (guild_id, user_id, streak_days, last_active_date, updated_at) "
        "VALUES ($1, $2, $3, $4, NOW()) "
        "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
        "streak_days=EXCLUDED.streak_days, "
        "last_active_date=EXCLUDED.last_active_date, "
        "updated_at=NOW()",
        guild_id, user_id, streak_days, last_active_date,
    )


async def set_user_xp(
    db: "PgDatabase", guild_id: int, user_id: int, total_xp: int,
    cfg: LevelingConfig, *, display_name: str | None = None,
) -> int:
    """Set a user's cumulative XP directly.  Returns the derived level."""
    total_xp = max(0, int(total_xp))
    new_level = level_from_total_xp(total_xp, cfg)
    await db.execute(
        "INSERT INTO chat_levels "
        "(guild_id, user_id, xp, level, total_messages, display_name, updated_at) "
        "VALUES ($1, $2, $3, $4, 0, $5, NOW()) "
        "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
        "xp=EXCLUDED.xp, level=EXCLUDED.level, "
        "display_name=COALESCE(EXCLUDED.display_name, chat_levels.display_name), "
        "updated_at=NOW()",
        guild_id, user_id, total_xp, new_level, display_name,
    )
    return new_level


async def recompute_levels(
    db: "PgDatabase", guild_id: int, cfg: LevelingConfig,
    *, batch_size: int = 500,
) -> tuple[int, int]:
    """Recalculate every user's ``level`` column from their stored ``xp``.

    Useful after the curve coefficients change (or after a CSV import from a
    source system whose curve differed from the defaults).  Returns
    ``(updated, unchanged)``.
    """
    rows = await db.fetch_all(
        "SELECT user_id, xp, level FROM chat_levels WHERE guild_id=$1",
        guild_id,
    )
    updates: list[tuple[int, int, int]] = []
    unchanged = 0
    for r in rows:
        uid = int(r["user_id"])
        xp = int(r["xp"] or 0)
        current = int(r["level"] or 0)
        target = level_from_total_xp(xp, cfg)
        if target != current:
            updates.append((guild_id, uid, target))
        else:
            unchanged += 1

    if not updates:
        return 0, unchanged

    async with db.atomic() as conn:
        for i in range(0, len(updates), batch_size):
            chunk = updates[i : i + batch_size]
            await conn.executemany(
                "UPDATE chat_levels SET level=$3, updated_at=NOW() "
                "WHERE guild_id=$1 AND user_id=$2",
                chunk,
            )
    return len(updates), unchanged


async def get_leaderboard(
    db: "PgDatabase", guild_id: int, limit: int = 10, offset: int = 0,
) -> list[dict]:
    return await db.fetch_all(
        "SELECT user_id, xp, level, total_messages, display_name "
        "FROM chat_levels WHERE guild_id=$1 "
        "ORDER BY xp DESC LIMIT $2 OFFSET $3",
        guild_id, limit, offset,
    )


async def get_user_rank(db: "PgDatabase", guild_id: int, user_id: int) -> int | None:
    """Return the user's 1-indexed leaderboard position, or None if no row."""
    row = await db.fetch_one(
        "SELECT 1 + (SELECT COUNT(*) FROM chat_levels c2 "
        "WHERE c2.guild_id=$1 AND c2.xp > c1.xp) AS rank "
        "FROM chat_levels c1 WHERE c1.guild_id=$1 AND c1.user_id=$2",
        guild_id, user_id,
    )
    if not row:
        return None
    return int(row["rank"])


# ── Ranks (level -> title) ──────────────────────────────────────────────────

async def get_ranks(db: "PgDatabase", guild_id: int) -> list[tuple[int, str]]:
    """Return [(level, rank_name), ...] sorted by level ascending."""
    rows = await db.fetch_all(
        "SELECT level, rank_name FROM chat_level_ranks "
        "WHERE guild_id=$1 ORDER BY level ASC",
        guild_id,
    )
    return [(int(r["level"]), str(r["rank_name"])) for r in rows]


def rank_for_level(level: int, ranks: list[tuple[int, str]]) -> str | None:
    """Return the highest-level rank name whose threshold is <= *level*."""
    best: str | None = None
    for threshold, name in ranks:
        if threshold <= level:
            best = name
        else:
            break
    return best


async def set_rank(db: "PgDatabase", guild_id: int, level: int, name: str) -> None:
    await db.execute(
        "INSERT INTO chat_level_ranks (guild_id, level, rank_name) VALUES ($1, $2, $3) "
        "ON CONFLICT (guild_id, level) DO UPDATE SET rank_name=EXCLUDED.rank_name",
        guild_id, level, name,
    )


async def delete_rank(db: "PgDatabase", guild_id: int, level: int) -> bool:
    status = await db.execute(
        "DELETE FROM chat_level_ranks WHERE guild_id=$1 AND level=$2",
        guild_id, level,
    )
    try:
        return int(status.split()[-1]) > 0
    except (ValueError, IndexError):
        return False


# ── Role rewards (level -> role_id) ─────────────────────────────────────────

async def get_role_rewards(db: "PgDatabase", guild_id: int) -> list[tuple[int, int]]:
    """Return [(level, role_id), ...] sorted by level ascending."""
    rows = await db.fetch_all(
        "SELECT level, role_id FROM chat_level_roles "
        "WHERE guild_id=$1 ORDER BY level ASC, role_id ASC",
        guild_id,
    )
    return [(int(r["level"]), int(r["role_id"])) for r in rows]


async def add_role_reward(db: "PgDatabase", guild_id: int, level: int, role_id: int) -> None:
    await db.execute(
        "INSERT INTO chat_level_roles (guild_id, level, role_id) VALUES ($1, $2, $3) "
        "ON CONFLICT DO NOTHING",
        guild_id, level, role_id,
    )


async def remove_role_reward(
    db: "PgDatabase", guild_id: int, level: int, role_id: int | None = None,
) -> int:
    if role_id is None:
        status = await db.execute(
            "DELETE FROM chat_level_roles WHERE guild_id=$1 AND level=$2",
            guild_id, level,
        )
    else:
        status = await db.execute(
            "DELETE FROM chat_level_roles WHERE guild_id=$1 AND level=$2 AND role_id=$3",
            guild_id, level, role_id,
        )
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def sync_member_roles(
    member: discord.Member, level: int,
    rewards: list[tuple[int, int]], stack: bool,
) -> tuple[list[int], list[int]]:
    """Apply level-based role rewards to *member*.

    Additive-only semantics: we **never** strip a reward role the member
    already holds, even if they technically haven't earned it via chat -- the
    role may have been granted manually, imported, or carried over from a
    previous bot. Stripping those without consent is destructive.

    ``stack=True`` -- add every earned reward the member doesn't already have.
    ``stack=False`` -- add the highest-tier earned reward, and remove any
    STRICTLY LOWER-tier reward roles the member currently holds (classic
    "replace with higher rank" behaviour). Reward roles ABOVE the member's
    earned tier are never touched.

    Returns (added_role_ids, removed_role_ids).
    """
    if not rewards:
        return [], []

    earned_pairs: list[tuple[int, int]] = [(lvl, rid) for lvl, rid in rewards if lvl <= level]
    earned_ids: set[int] = {rid for _, rid in earned_pairs}

    current_role_ids = {r.id for r in member.roles}
    guild = member.guild
    me = guild.me
    bot_top = me.top_role if me else None

    def _manageable(role: discord.Role) -> bool:
        if role.managed or role.is_default():
            return False
        if bot_top is None:
            return False
        return role < bot_top

    to_add_ids: set[int] = set()
    to_remove_ids: set[int] = set()

    if stack:
        to_add_ids = earned_ids - current_role_ids
    else:
        if earned_pairs:
            max_lvl = max(lvl for lvl, _ in earned_pairs)
            top_tier_ids = {rid for lvl, rid in earned_pairs if lvl == max_lvl}
            lower_tier_ids = {rid for lvl, rid in earned_pairs if lvl < max_lvl}
            to_add_ids = top_tier_ids - current_role_ids
            # Only strip lower-tier EARNED rewards -- never higher-tier ones,
            # which the member may hold legitimately from a manual grant.
            to_remove_ids = lower_tier_ids & current_role_ids

    to_add: list[discord.Role] = []
    for rid in to_add_ids:
        r = guild.get_role(rid)
        if r and _manageable(r):
            to_add.append(r)

    to_remove: list[discord.Role] = []
    for rid in to_remove_ids:
        r = guild.get_role(rid)
        if r and _manageable(r):
            to_remove.append(r)

    added: list[int] = []
    removed: list[int] = []
    if to_add:
        try:
            await member.add_roles(*to_add, reason="Chat leveling role reward")
            added = [r.id for r in to_add]
        except discord.HTTPException as exc:
            log.debug("sync_member_roles: add_roles failed gid=%s uid=%s: %s",
                      guild.id, member.id, exc)
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Chat leveling role reassignment")
            removed = [r.id for r in to_remove]
        except discord.HTTPException as exc:
            log.debug("sync_member_roles: remove_roles failed gid=%s uid=%s: %s",
                      guild.id, member.id, exc)
    return added, removed
