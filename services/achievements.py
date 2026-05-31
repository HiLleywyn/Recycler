"""services/achievements.py - grant, progress, and event fan-in for achievements.

Single source of truth for awarding badges. Read the catalog from
achievements_config.py, increment a per-user, per-trigger counter on each
bus event, and grant any badge whose threshold is met on the new counter
value.

Public API
----------
``sync_catalog(db)``
    Upsert every entry from achievements_config.ACHIEVEMENTS into the
    ``badges`` table. Called once at cog load so the DB catalog stays
    aligned with the Python definitions (and the HTTP API returns the
    same thing the bot displays).

``bump(bot, user_id, guild_id, trigger, amount=1)``
    Increment the counter for ``(user_id, guild_id, trigger)`` and award
    any achievements whose threshold is newly crossed. Pays the reward,
    writes a ``BADGE_REWARD`` transaction, and publishes ``badge_earned``.

``grant(bot, user_id, guild_id, badge_id)``
    Low-level grant path used by ``bump`` and by any caller that wants to
    award a badge unconditionally. Idempotent: a second grant for the
    same user + badge is a no-op.

``user_badges(db, user_id, guild_id)``
    Return a list of ``{badge_id, name, icon, earned_at, ...}`` dicts for
    every badge the user has earned, joined against the catalog.

``attach_listeners(bot)``
    Subscribe this service to the bus events that drive achievements. Called
    from ``cogs/achievements.py`` on cog load.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import discord

from core.framework.scale import to_raw

import configs.achievements_config as _catalog

log = logging.getLogger(__name__)

# Per-(uid, gid) throttle for net-worth recomputes. Value is the last epoch
# timestamp we recomputed; a fresh compute is skipped if it's younger than
# _NW_TTL seconds. Keeps monetary events from spawning expensive bulk
# queries on every tick.
_NW_TTL: float = 60.0
_nw_last: dict[tuple[int, int], float] = {}


# ── Catalog sync ─────────────────────────────────────────────────────────────

async def sync_catalog(db) -> None:
    """Upsert every catalog entry into the ``badges`` table.

    Safe to re-run on every startup: rows are matched on ``badge_id`` and
    updated in place so edits to name/description/icon/reward in
    ``achievements_config.py`` propagate without a manual migration.
    """
    for a in _catalog.ACHIEVEMENTS:
        await db.execute(
            """
            INSERT INTO badges
                (badge_id, name, description, icon, category,
                 requirement, reward_usd, sort_order, secret)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
            ON CONFLICT (badge_id) DO UPDATE SET
                name        = EXCLUDED.name,
                description = EXCLUDED.description,
                icon        = EXCLUDED.icon,
                category    = EXCLUDED.category,
                requirement = EXCLUDED.requirement,
                reward_usd  = EXCLUDED.reward_usd,
                sort_order  = EXCLUDED.sort_order,
                secret      = EXCLUDED.secret
            """,
            a["badge_id"],
            a["name"],
            a.get("description", ""),
            a.get("icon", ""),
            a.get("category", ""),
            json.dumps(a.get("requirement", {})),
            float(a.get("reward_usd", 0.0)),
            int(a.get("sort_order", 0)),
            bool(a.get("secret", False)),
        )
    log.info("Synced %d achievement catalog entries.", len(_catalog.ACHIEVEMENTS))


# ── Grant / progress ─────────────────────────────────────────────────────────

async def grant(bot, user_id: int, guild_id: int, badge_id: str) -> bool:
    """Award ``badge_id`` to the user. Returns True on a new grant, False if
    already earned.

    Pays the catalog reward to the user's wallet and writes a BADGE_REWARD
    transaction so it shows up in history. Publishes ``badge_earned`` for
    the feed subscribers + API.
    """
    entry = _catalog.get(badge_id)
    if entry is None:
        log.warning("grant(): unknown badge_id %r", badge_id)
        return False

    db = bot.db
    # Chat XP can fire for users who haven't registered yet (the only
    # touch point is reading messages -- no command was run, no users
    # row was created via ensure_registered). Without this seeding step
    # the user_badges INSERT below trips fk_user_badges_user. Cheap
    # ON CONFLICT DO NOTHING via ensure_user.
    try:
        await db.ensure_user(user_id, guild_id)
    except Exception:
        log.debug(
            "grant(): ensure_user failed uid=%s gid=%s",
            user_id, guild_id, exc_info=True,
        )
    # INSERT ... ON CONFLICT RETURNING earned_at tells us whether this is a
    # new grant or a repeat: on conflict, no row is returned.
    row = await db.fetch_one(
        """
        INSERT INTO user_badges (user_id, guild_id, badge_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, guild_id, badge_id) DO NOTHING
        RETURNING earned_at
        """,
        user_id, guild_id, badge_id,
    )
    if row is None:
        return False

    reward = float(entry.get("reward_usd", 0.0) or 0.0)
    tx_hash = ""
    if reward > 0:
        try:
            await db.update_wallet(user_id, guild_id, to_raw(reward))
            tx_hash = await db.log_tx(
                guild_id, user_id, "BADGE_REWARD",
                symbol_out="USD", amount_out=to_raw(reward),
                network="usd",
            )
        except Exception as exc:
            log.exception("Failed to pay badge reward for %s: %s", badge_id, exc)

    guild = bot.get_guild(guild_id)
    try:
        await bot.bus.publish(
            "badge_earned",
            guild=guild,
            user_id=user_id,
            badge_id=badge_id,
            name=entry["name"],
            icon=entry.get("icon", ""),
            reward_usd=reward,
            tx_hash=tx_hash,
        )
    except Exception as exc:
        log.error("badge_earned publish failed: %s", exc)

    # Auto-grant any cosmetics gated on this badge id via
    # ``unlock: "achievement:<badge_id>"`` in cosmetics_config. Lets us
    # tie a title / sigil / frame / banner to an existing achievement
    # without writing a parallel grant pipeline -- the cosmetic just
    # lands in the player's inventory the moment they earn the badge.
    try:
        from services import cosmetics as _cos
        for item_path in _cos.cosmetics_for_achievement(badge_id):
            try:
                await _cos.grant(db, user_id, item_path, source=f"achievement:{badge_id}")
            except Exception:
                log.debug(
                    "achievement %s: cosmetic auto-grant failed for %s",
                    badge_id, item_path, exc_info=True,
                )
    except Exception:
        log.debug("achievement %s: cosmetics module unavailable", badge_id, exc_info=True)

    await _dm_badge(bot, user_id, entry, reward)
    return True


async def revoke(db, user_id: int, guild_id: int, badge_id: str) -> bool:
    """Delete a user_badges row. Returns True if a row was deleted.

    Does NOT refund the reward_usd that was paid on grant -- revocation is
    a moderation tool, not a rollback. If a full rollback is needed, pair
    this with an ``,admin take`` on the wallet.
    """
    status = await db.execute(
        "DELETE FROM user_badges "
        "WHERE user_id = $1 AND guild_id = $2 AND badge_id = $3",
        user_id, guild_id, badge_id,
    )
    # asyncpg returns "DELETE <n>" on successful execute.
    return isinstance(status, str) and status.startswith("DELETE ") and status != "DELETE 0"


async def _dm_badge(bot, user_id: int, entry: dict, reward: float) -> None:
    """Best-effort DM letting the user know they earned a badge."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        return
    if user is None:
        return
    from core.framework.embed import card
    from core.framework.ui import C_GOLD
    title = f"{entry.get('icon', '')} Achievement Unlocked: {entry['name']}"
    body = entry.get("description", "")
    if reward > 0:
        body = f"{body}\n\nReward: **${reward:,.2f}**"
    embed = card(title, description=body, color=C_GOLD).build()
    try:
        await user.send(embed=embed)
    except discord.HTTPException:
        pass


async def bump(
    bot,
    user_id: int,
    guild_id: int,
    trigger: str,
    amount: int = 1,
) -> list[str]:
    """Increment ``trigger`` counter for the user and grant any newly-met
    achievements. Returns the list of newly-granted ``badge_id`` values.

    Safe to call for every event; does nothing when no catalog entry uses
    the trigger. Only counter-based catalog entries (requirement.count)
    are considered; threshold entries flow through ``check_threshold``.
    """
    matches = [
        e for e in _catalog.by_trigger(trigger)
        if "count" in e.get("requirement", {})
    ]
    if not matches:
        return []
    if not user_id or not guild_id:
        return []

    db = bot.db
    row = await db.fetch_one(
        """
        INSERT INTO achievement_progress (user_id, guild_id, trigger, counter)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, guild_id, trigger) DO UPDATE
            SET counter = achievement_progress.counter + EXCLUDED.counter,
                updated_at = NOW()
        RETURNING counter
        """,
        user_id, guild_id, trigger, int(amount),
    )
    new_count = int(row["counter"]) if row else 0

    granted: list[str] = []
    for entry in matches:
        target = int(entry["requirement"].get("count", 1))
        if new_count >= target:
            if await grant(bot, user_id, guild_id, entry["badge_id"]):
                granted.append(entry["badge_id"])
    return granted


async def check_threshold(
    bot,
    user_id: int,
    guild_id: int,
    trigger: str,
    value: float,
) -> list[str]:
    """Grant any threshold-based achievement whose bar ``value`` crosses.

    Used for state-shaped triggers (``chat_level_up`` with ``level``,
    ``net_worth`` with ``threshold``) where the requirement is "hit this
    number at least once" rather than "do this N times". Reads the
    requirement key from the catalog entry so a single helper serves both.
    """
    if not user_id or not guild_id:
        return []
    granted: list[str] = []
    for entry in _catalog.by_trigger(trigger):
        req = entry.get("requirement", {})
        bar = req.get("threshold")
        if bar is None:
            bar = req.get("level")
        if bar is None:
            continue
        if float(value) >= float(bar):
            if await grant(bot, user_id, guild_id, entry["badge_id"]):
                granted.append(entry["badge_id"])
    return granted


# ── Retroactive backfill ─────────────────────────────────────────────────────
#
# Reconstructs achievement_progress counters from historical data in the
# transactions table, then runs the normal grant pipeline. Intended for:
#   1. Rolling out achievements on a server that already has activity.
#   2. Repairing counters after a bug or DB restore.
#
# Triggers covered by backfill (those with a clean tx_type mapping):
#   work_completed, daily_claimed, trade_executed, swap_executed,
#   stake_created, lp_added, bank_deposit, stone_leveled.
# Threshold triggers covered:
#   net_worth (recomputed from current state).
# Not covered (no durable source): buddy_adopted, buddy_battle_win,
# exploit_run, exploit_win, chat_level_up. Those accumulate going forward.

_BACKFILL_TX_MAP: dict[str, tuple[str, ...]] = {
    "work_completed":  ("WORK",),
    "daily_claimed":   ("DAILY",),
    "trade_executed":  ("BUY", "SELL", "SWAP"),
    "swap_executed":   ("SWAP",),
    "stake_created":   ("STAKE",),
    "lp_added":        ("ADDLP",),
    "bank_deposit":    ("DEPOSIT",),
    "stone_leveled":   ("STONE_LEVELUP",),
}


async def backfill_user(bot, user_id: int, guild_id: int) -> dict:
    """Rebuild counters for one user and grant any earned achievements.

    Returns a summary dict: {'updated': [trigger...], 'granted': [badge_id...]}.
    Idempotent: existing badges are not re-granted and rewards are not
    re-paid (grant() uses INSERT ON CONFLICT DO NOTHING).
    """
    db = bot.db
    updated: list[str] = []
    granted_all: list[str] = []

    # Register trigger has no clean tx_type mapping (WALLET_CREATE isn't
    # reliably logged for users registered before we started tracking it).
    # Presence in the users table is itself proof of registration, so we
    # bump the counter unconditionally when the user row exists.
    user_exists = await db.fetch_val(
        "SELECT 1 FROM users WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    if user_exists:
        await db.execute(
            """
            INSERT INTO achievement_progress (user_id, guild_id, trigger, counter)
            VALUES ($1, $2, 'register', 1)
            ON CONFLICT (user_id, guild_id, trigger) DO UPDATE
                SET counter = GREATEST(achievement_progress.counter, 1),
                    updated_at = NOW()
            """,
            user_id, guild_id,
        )
        updated.append("register")
        for entry in _catalog.by_trigger("register"):
            target = int(entry.get("requirement", {}).get("count", 0))
            if target and 1 >= target:
                if await grant(bot, user_id, guild_id, entry["badge_id"]):
                    granted_all.append(entry["badge_id"])

    for trigger, tx_types in _BACKFILL_TX_MAP.items():
        count = await db.fetch_val(
            f"""
            SELECT COUNT(*) FROM transactions
            WHERE guild_id = $1 AND user_id = $2
              AND tx_type IN ({','.join(f"'{t}'" for t in tx_types)})
            """,
            guild_id, user_id,
        )
        count = int(count or 0)
        if count <= 0:
            continue
        await db.execute(
            """
            INSERT INTO achievement_progress (user_id, guild_id, trigger, counter)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, guild_id, trigger) DO UPDATE
                SET counter = GREATEST(achievement_progress.counter, EXCLUDED.counter),
                    updated_at = NOW()
            """,
            user_id, guild_id, trigger, count,
        )
        updated.append(trigger)
        for entry in _catalog.by_trigger(trigger):
            target = int(entry.get("requirement", {}).get("count", 0))
            if target and count >= target:
                if await grant(bot, user_id, guild_id, entry["badge_id"]):
                    granted_all.append(entry["badge_id"])

    # Threshold triggers: net_worth.
    try:
        # Force-bypass the _NW_TTL cache so a manual backfill always refreshes.
        _nw_last.pop((user_id, guild_id), None)
        granted_nw = await check_net_worth_milestones(bot, user_id, guild_id)
        granted_all.extend(granted_nw)
    except Exception as exc:
        log.debug("backfill net worth check failed uid=%s: %s", user_id, exc)

    return {"updated": updated, "granted": granted_all}


async def backfill_guild(bot, guild_id: int) -> dict:
    """Run backfill across every registered user in a guild.

    Returns: {'users': int, 'granted_total': int, 'by_badge': {badge_id: n}}.
    """
    db = bot.db
    users = await db.fetch_all(
        "SELECT user_id FROM users WHERE guild_id = $1",
        guild_id,
    )
    by_badge: dict[str, int] = {}
    total = 0
    for row in (users or []):
        uid = int(row["user_id"])
        try:
            summary = await backfill_user(bot, uid, guild_id)
        except Exception as exc:
            log.exception("backfill failed for uid=%s: %s", uid, exc)
            continue
        for bid in summary.get("granted", []):
            by_badge[bid] = by_badge.get(bid, 0) + 1
            total += 1
    return {
        "users": len(users or []),
        "granted_total": total,
        "by_badge": by_badge,
    }


async def check_net_worth_milestones(bot, user_id: int, guild_id: int) -> list[str]:
    """Recompute ``user_id``'s net worth (throttled) and grant milestones.

    Call after any event that moves a user's balance. Skips the compute
    entirely when all net_worth-triggered badges are already earned, so
    maxed users pay zero cost on every monetary event.
    """
    from services import net_worth as _nw
    nw_entries = _catalog.by_trigger("net_worth")
    if not nw_entries:
        return []
    ids = {e["badge_id"] for e in nw_entries}
    earned = await earned_ids(bot.db, user_id, guild_id)
    if ids.issubset(earned):
        return []

    key = (user_id, guild_id)
    now = time.time()
    last = _nw_last.get(key, 0.0)
    if now - last < _NW_TTL:
        return []
    _nw_last[key] = now
    try:
        result = await _nw.compute_net_worth(user_id, guild_id, bot.db)
        value = float(getattr(result, "total", 0.0) or 0.0)
    except Exception as exc:
        log.debug("net worth compute failed uid=%s: %s", user_id, exc)
        return []
    return await check_threshold(bot, user_id, guild_id, "net_worth", value)


# ── Read helpers ─────────────────────────────────────────────────────────────

async def user_badges(db, user_id: int, guild_id: int) -> list[dict]:
    """Return all badges earned by a user, joined against the catalog."""
    rows = await db.fetch_all(
        """
        SELECT ub.badge_id, ub.earned_at,
               b.name, b.description, b.icon, b.category, b.reward_usd
        FROM user_badges ub
        JOIN badges b ON b.badge_id = ub.badge_id
        WHERE ub.user_id = $1 AND ub.guild_id = $2
        ORDER BY b.category, b.sort_order
        """,
        user_id, guild_id,
    )
    return rows or []


async def earned_ids(db, user_id: int, guild_id: int) -> set[str]:
    """Return the set of badge_ids already earned by the user."""
    rows = await db.fetch_all(
        "SELECT badge_id FROM user_badges WHERE user_id = $1 AND guild_id = $2",
        user_id, guild_id,
    )
    return {r["badge_id"] for r in (rows or [])}


async def progress_for(
    db, user_id: int, guild_id: int, trigger: str,
) -> int:
    """Return the current counter for ``trigger`` (0 if never incremented)."""
    val = await db.fetch_val(
        "SELECT counter FROM achievement_progress "
        "WHERE user_id = $1 AND guild_id = $2 AND trigger = $3",
        user_id, guild_id, trigger,
    )
    return int(val or 0)


# ── Bus listeners ────────────────────────────────────────────────────────────

def _extract_uid(user: Any) -> int | None:
    """Normalize a ``user=`` kwarg to an int user id."""
    if user is None:
        return None
    if isinstance(user, int):
        return user
    return int(getattr(user, "id", 0) or 0) or None


def _extract_gid(guild: Any, fallback: Any = None) -> int | None:
    """Normalize a ``guild=`` or ``guild_id=`` kwarg to an int guild id."""
    if guild is None:
        guild = fallback
    if guild is None:
        return None
    if isinstance(guild, int):
        return guild
    return int(getattr(guild, "id", 0) or 0) or None


def attach_listeners(bot) -> None:
    """Subscribe the achievement engine to every relevant bus event.

    Adding a new achievement usually means adding a handler below (or
    reusing one that already bumps the right trigger).
    """
    bus = bot.bus

    async def _on_wallet_created(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "register")

    async def _on_work(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "work_completed")

    async def _on_daily(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await bump(bot, uid, gid, "daily_claimed")
        # Streak update is idempotent-per-day and feeds the streak_* badges.
        try:
            from services import streaks as _streaks
            summary = await _streaks.update_on_claim(bot.db, uid, gid)
            await check_threshold(
                bot, uid, gid, "daily_streak", int(summary["current"])
            )
        except Exception as exc:
            log.error("streak update failed uid=%s: %s", uid, exc)

    async def _on_trade(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "trade_executed")

    async def _on_swap(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "swap_executed")

    async def _on_pow_tick(**kw) -> None:
        # payouts can be a list of (uid, amt) tuples or a {uid: amt} dict.
        # Either way, count one "block_mined" credit per payee per tick.
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
                await bump(bot, uid, gid, "block_mined")

    async def _on_staked(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "stake_created")

    async def _on_validator_reward(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "stake_reward")

    async def _on_lp_added(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "lp_added")

    async def _on_deposit(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "bank_deposit")

    async def _on_gamble(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await bump(bot, uid, gid, "gamble_play")
        if kw.get("won") or float(kw.get("delta", 0) or 0) > 0:
            await bump(bot, uid, gid, "gamble_win")

    async def _on_exploit(**kw) -> None:
        # cogs/eat_the_rich.py publishes with attacker= (not user= / user_id=).
        # Fall back through all three so we catch either shape.
        uid = _extract_uid(
            kw.get("user") or kw.get("user_id") or kw.get("attacker")
        )
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await bump(bot, uid, gid, "exploit_run")
        if kw.get("won") or kw.get("success"):
            await bump(bot, uid, gid, "exploit_win")

    async def _on_ape(**kw) -> None:
        # cogs/earn.py ,ape is gambling-adjacent: risky spin, win/lose
        # outcome. Route it through the gamble_* triggers so the Degen /
        # Lucky Streak achievements count apes.
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await bump(bot, uid, gid, "gamble_play")
        if float(kw.get("net", 0) or 0) > 0:
            await bump(bot, uid, gid, "gamble_win")

    async def _on_mining_tick_complete(**kw) -> None:
        # cogs/chain_group.py fires this for the SUN/MTA PoW path; its
        # payload is a nested tick_summary rather than a flat payouts
        # list. Iterate solo_payouts (tuples), pool_miner_ids (added in
        # the same commit as this listener), and groups[].members.
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
            await bump(bot, uid, gid, "block_mined")

    async def _on_buddy_adopted(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "buddy_adopted")

    async def _on_buddy_battle_win(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "buddy_battle_win")

    async def _on_buddy_battle_loss(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "buddy_battle_loss")

    async def _on_buddy_arena_won(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "buddy_arena_won")

    async def _on_buddy_arena_lost(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "buddy_arena_lost")

    async def _on_buddy_arena_spawn(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "buddy_arena_spawn")

    async def _on_validator_registered(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "validator_registered")

    async def _on_chat_level_up(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        level = int(kw.get("new_level") or kw.get("level") or 0)
        if uid and gid and level:
            await check_threshold(bot, uid, gid, "chat_level_up", level)

    async def _on_monetary(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await check_net_worth_milestones(bot, uid, gid)

    async def _on_stone_leveled(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "stone_leveled")

    async def _on_buddy_stored(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "buddy_stored")
        # Storage-count threshold check: pass the running stored count
        # so the "10 stored at once" badge fires when the count crosses 10.
        try:
            n = int(kw.get("stored_count") or 0)
        except (TypeError, ValueError):
            n = 0
        if uid and gid and n > 0:
            await check_threshold(
                bot, uid, gid, "buddy_storage_count", n,
            )

    async def _on_daycare_egg_collected(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "daycare_egg_collected")

    async def _on_specialty_level(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        spec = str(kw.get("specialty") or "").lower()
        try:
            level = int(kw.get("new_level") or kw.get("level") or 0)
        except (TypeError, ValueError):
            level = 0
        if not (uid and gid and spec and level > 0):
            return
        await check_threshold(
            bot, uid, gid, f"specialty_level_{spec}", level,
        )
        # Renaissance: every specialty at level >= 10. Cheap one-shot
        # query against user_crafting; fires the badge once on any
        # specialty bump that brings the floor up to 10.
        if level >= 10:
            try:
                row = await bot.db.fetch_one(
                    "SELECT smithing_level, alchemy_level, cooking_level, "
                    "       fletching_level, tinkering_level "
                    "FROM user_crafting "
                    "WHERE guild_id = $1 AND user_id = $2",
                    gid, uid,
                )
                if row and all(
                    int(row.get(f"{s}_level") or 1) >= 10
                    for s in (
                        "smithing", "alchemy", "cooking",
                        "fletching", "tinkering",
                    )
                ):
                    await bump(bot, uid, gid, "specialty_renaissance")
            except Exception:
                log.debug(
                    "specialty_renaissance check failed", exc_info=True,
                )

    async def _on_wild_zone(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        try:
            count = int(kw.get("zone_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if uid and gid and count > 0:
            await check_threshold(
                bot, uid, gid, "wild_zone_diversity", count,
            )

    bus.subscribe("wallet_created", _on_wallet_created)
    bus.subscribe("work_completed", _on_work)
    bus.subscribe("daily_claimed", _on_daily)
    bus.subscribe("trade", _on_trade)
    bus.subscribe("trade_executed", _on_trade)
    bus.subscribe("swap_trade", _on_swap)
    bus.subscribe("swap_executed", _on_swap)
    bus.subscribe("pow_mining_tick", _on_pow_tick)
    bus.subscribe("staked", _on_staked)
    bus.subscribe("validator_reward", _on_validator_reward)
    bus.subscribe("lp_added", _on_lp_added)
    bus.subscribe("deposit", _on_deposit)
    bus.subscribe("gamble_result", _on_gamble)
    bus.subscribe("exploit_completed", _on_exploit)
    bus.subscribe("ape_completed", _on_ape)
    bus.subscribe("mining_tick_complete", _on_mining_tick_complete)
    bus.subscribe("buddy_adopted", _on_buddy_adopted)
    bus.subscribe("buddy_battle_win", _on_buddy_battle_win)
    bus.subscribe("buddy_battle_loss", _on_buddy_battle_loss)
    bus.subscribe("buddy_arena_spawn", _on_buddy_arena_spawn)
    bus.subscribe("buddy_arena_won", _on_buddy_arena_won)
    bus.subscribe("buddy_arena_lost", _on_buddy_arena_lost)
    bus.subscribe("validator_registered", _on_validator_registered)
    bus.subscribe("chat_level_up", _on_chat_level_up)
    bus.subscribe("stone_leveled", _on_stone_leveled)
    bus.subscribe("buddy_stored", _on_buddy_stored)
    bus.subscribe("daycare_egg_collected", _on_daycare_egg_collected)
    bus.subscribe("specialty_level_up", _on_specialty_level)
    bus.subscribe("wild_zone_visited", _on_wild_zone)

    # ── Auction House ────────────────────────────────────────────────────
    async def _on_ah_listing(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "ah_listing_created")

    async def _on_ah_sale(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "ah_sale_settled")

    async def _on_ah_purchase(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "ah_purchase_settled")

    async def _on_ah_cross_currency(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "ah_cross_currency_buy")

    bus.subscribe("ah_listing_created", _on_ah_listing)
    bus.subscribe("ah_sale_settled", _on_ah_sale)
    bus.subscribe("ah_purchase_settled", _on_ah_purchase)
    bus.subscribe("ah_cross_currency_buy", _on_ah_cross_currency)

    # Expeditions: every collect bumps the generic + per-destination
    # trigger so the catalog can gate badges on "send 50 expeditions"
    # AND "complete a Ruins expedition" without re-plumbing.
    async def _on_expedition_started(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if uid and gid:
            await bump(bot, uid, gid, "expedition_started")

    async def _on_expedition_collected(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await bump(bot, uid, gid, "expedition_collected")
        dest = str(kw.get("destination") or "").lower()
        if dest:
            await bump(bot, uid, gid, f"expedition_collected_{dest}")

    bus.subscribe("expedition_started", _on_expedition_started)
    bus.subscribe("expedition_collected", _on_expedition_collected)

    # Net worth milestones: recompute (throttled) after any event that moves
    # a balance. Cheap once the user has earned both net_worth badges.
    for event in ("deposit", "withdraw", "transfer", "trade", "trade_executed",
                  "swap_trade", "swap_executed", "validator_reward",
                  "stake_reward", "daily_claimed", "work_completed",
                  "drop_claimed", "gamble_result"):
        bus.subscribe(event, _on_monetary)
