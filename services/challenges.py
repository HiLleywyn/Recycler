"""services/challenges.py - guild-wide collective challenges.

A challenge is a server-level goal: "collectively hit N of X by deadline
Y, split the reward pool if you succeed." Progress ticks on every
qualifying bus event, split between a global counter on the challenge
row and a per-user contribution row. Completion pays out proportionally.

Flow
----
1. Admin runs ,admin challenge start (or services/challenges.start()).
2. A row goes into guild_challenges with status='active'. The unique
   index enforces "one active per (guild, trigger)".
3. Listeners attached via attach_listeners() subscribe to the standard
   bus events. Each event finds the matching active challenge for the
   guild + trigger and increments both the global progress and the
   user's contribution (atomic upsert).
4. When progress crosses the target, succeed() is called: status flips
   to 'succeeded', reward_pool_usd is split proportional to contributions
   among every contributor, the wallets are credited, and a
   ``challenge_succeeded`` bus event fires.
5. An expiry loop (cogs/challenges.py) flips unfinished challenges to
   'failed' once ends_at passes. No payout on failure.

Public API
----------
``start(db, ...)``, ``get_active_by_trigger(db, gid, trigger)``,
``list_active(db, gid)``, ``list_history(db, gid, limit)``,
``progress_trigger(bot, uid, gid, trigger, amount)``, ``succeed(bot, cid)``,
``check_expired(bot)``, ``attach_listeners(bot)``.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from core.framework.scale import to_raw

log = logging.getLogger(__name__)

# Valid triggers. Mirrors the logical labels used by quests/achievements
# so a single bus event naturally fans into all three systems.
TRIGGERS: tuple[str, ...] = (
    "work_completed",
    "daily_claimed",
    "trade_executed",
    "swap_executed",
    "block_mined",
    "stake_created",
    "lp_added",
    "bank_deposit",
    "buddy_adopted",
    "buddy_battle_win",
    "buddy_battle_loss",
    "buddy_arena_spawn",
    "buddy_arena_won",
    "buddy_arena_lost",
    "gamble_play",
    "gamble_win",
    "exploit_run",
    "exploit_win",
    "stone_leveled",
    "validator_registered",
    "fish_caught",
    "fish_legendary",
    # Lure Network economy events. Published by services/fishing.py from
    # swap_lure_to_reel / stake_lure / cashout_reel so quests / challenges
    # / achievements can all consume them.
    "fish_lure_swap",
    "fish_lure_stake",
    "fish_reel_cashout",
    # Wild-buddy battle triggers from fishing. Spawn fires per cast that
    # rolls a wild encounter; win/loss/capture fire after the PvE fight
    # resolves via services.fishing.resolve_wild_battle.
    "fish_wild_battle_spawn",
    "fish_wild_battle_won",
    "fish_wild_battle_lost",
    "fish_wild_buddy_captured",
    # Forge Network (crafting) triggers. Published by cogs/crafting.py via
    # the per-cog _fan_out helper. craft_made fires for every successful
    # ,craft make (amount = qty produced); craft_legendary is the legendary-
    # rarity subset; craft_applied fires once per ,craft apply call;
    # craft_ingot_swap and craft_forge_cashout count INGOT->FORGE burns and
    # FORGE->USD cashouts respectively.
    "craft_made",
    "craft_legendary",
    "craft_applied",
    "craft_ingot_swap",
    "craft_forge_cashout",
    # Dungeon wild-buddy battle triggers. Published by cogs/dungeon.py
    # via _fan_out from the ,delve battle command after the engine
    # resolves. Mirrors the fish_wild_battle_* family on the fishing side.
    "delve_wild_battle_spawn",
    "delve_wild_battle_won",
    "delve_wild_battle_lost",
    "delve_wild_buddy_captured",
)


def trigger_label(trigger: str) -> str:
    return {
        "work_completed":      "Work shifts",
        "daily_claimed":       "Daily claims",
        "trade_executed":      "Trades",
        "swap_executed":       "Swaps",
        "block_mined":         "Mining payouts",
        "stake_created":       "Stakes opened",
        "lp_added":            "LP added",
        "bank_deposit":        "Bank deposits",
        "buddy_adopted":       "Buddy adoptions",
        "buddy_battle_win":    "Buddy battle wins",
        "buddy_battle_loss":   "Buddy battle losses",
        "buddy_arena_spawn":   "Arena fights entered",
        "buddy_arena_won":     "Arena fights won",
        "buddy_arena_lost":    "Arena fights lost",
        "gamble_play":         "Games played",
        "gamble_win":          "Games won",
        "exploit_run":         "Eat attempts",
        "exploit_win":         "Rich players eaten",
        "stone_leveled":       "Stone level-ups",
        "validator_registered": "Validators registered",
        "fish_caught":         "Fish caught",
        "fish_legendary":      "Legendary fish landed",
        "craft_made":          "Items crafted",
        "craft_legendary":     "Legendary items crafted",
        "craft_applied":       "Crafts applied to games",
        "craft_ingot_swap":    "INGOT -> FORGE burns",
        "craft_forge_cashout": "FORGE -> USD cashouts",
        "delve_wild_battle_spawn":  "Wild buddies encountered (delve)",
        "delve_wild_battle_won":    "Wild battles won (delve)",
        "delve_wild_battle_lost":   "Wild battles lost (delve)",
        "delve_wild_buddy_captured": "Wild buddies captured (delve)",
    }.get(trigger, trigger.replace("_", " ").title())


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def start(
    db, guild_id: int, name: str, trigger: str, target: int,
    reward_pool_usd: float, duration_days: int, description: str = "",
) -> dict | None:
    """Create an active challenge. Returns the row, or ``None`` if one
    already exists for (guild, trigger).

    The unique index drops the INSERT silently on conflict so we can
    detect collisions with ON CONFLICT DO NOTHING and a RETURNING clause.
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")
    if target <= 0 or duration_days <= 0 or reward_pool_usd < 0:
        raise ValueError("target/duration must be positive; pool non-negative")
    ends_at = _dt.datetime.utcnow() + _dt.timedelta(days=int(duration_days))
    row = await db.fetch_one(
        """
        INSERT INTO guild_challenges
            (guild_id, name, description, trigger, target,
             reward_pool_usd, ends_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING challenge_id, guild_id, name, description, trigger,
                  target, progress, reward_pool_usd,
                  started_at, ends_at, completed_at, status
        """,
        guild_id, name, description, trigger, int(target),
        float(reward_pool_usd), ends_at,
    )
    return row


async def get(db, challenge_id: int) -> dict | None:
    return await db.fetch_one(
        """
        SELECT challenge_id, guild_id, name, description, trigger,
               target, progress, reward_pool_usd,
               started_at, ends_at, completed_at, status
        FROM guild_challenges WHERE challenge_id = $1
        """,
        int(challenge_id),
    )


async def get_active_by_trigger(
    db, guild_id: int, trigger: str,
) -> dict | None:
    return await db.fetch_one(
        """
        SELECT challenge_id, guild_id, name, description, trigger,
               target, progress, reward_pool_usd,
               started_at, ends_at, completed_at, status
        FROM guild_challenges
        WHERE guild_id = $1 AND trigger = $2 AND status = 'active'
        """,
        guild_id, trigger,
    )


async def list_active(db, guild_id: int) -> list[dict]:
    rows = await db.fetch_all(
        """
        SELECT challenge_id, guild_id, name, description, trigger,
               target, progress, reward_pool_usd,
               started_at, ends_at, completed_at, status
        FROM guild_challenges
        WHERE guild_id = $1 AND status = 'active'
        ORDER BY ends_at
        """,
        guild_id,
    )
    return rows or []


async def list_history(db, guild_id: int, limit: int = 10) -> list[dict]:
    rows = await db.fetch_all(
        """
        SELECT challenge_id, guild_id, name, trigger, target, progress,
               reward_pool_usd, started_at, ends_at, completed_at, status
        FROM guild_challenges
        WHERE guild_id = $1 AND status <> 'active'
        ORDER BY completed_at DESC NULLS LAST, ends_at DESC
        LIMIT $2
        """,
        guild_id, int(limit),
    )
    return rows or []


async def top_contributors(
    db, challenge_id: int, limit: int = 10,
) -> list[dict]:
    rows = await db.fetch_all(
        """
        SELECT user_id, contribution, reward_paid
        FROM guild_challenge_contributions
        WHERE challenge_id = $1
        ORDER BY contribution DESC
        LIMIT $2
        """,
        int(challenge_id), int(limit),
    )
    return rows or []


# ── Progress + success ───────────────────────────────────────────────────────

async def progress_trigger(
    bot, user_id: int, guild_id: int, trigger: str, amount: int = 1,
) -> dict | None:
    """Increment the active challenge for (guild, trigger) by ``amount``.

    Returns the updated challenge row if one existed, else ``None``.
    If this bump crosses the target, ``succeed()`` is fired off
    asynchronously so the caller doesn't block on reward distribution.
    """
    if amount <= 0 or not user_id or not guild_id:
        return None
    db = bot.db
    # UPDATE + RETURNING so we do everything in one round trip. The
    # WHERE clause ensures we only touch the active row.
    row = await db.fetch_one(
        """
        UPDATE guild_challenges
           SET progress = progress + $3
         WHERE guild_id = $1 AND trigger = $2 AND status = 'active'
        RETURNING challenge_id, target, progress, reward_pool_usd
        """,
        guild_id, trigger, int(amount),
    )
    if row is None:
        return None
    cid = int(row["challenge_id"])
    await db.execute(
        """
        INSERT INTO guild_challenge_contributions
            (challenge_id, user_id, guild_id, contribution)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (challenge_id, user_id) DO UPDATE
            SET contribution = guild_challenge_contributions.contribution
                               + EXCLUDED.contribution,
                updated_at = NOW()
        """,
        cid, user_id, guild_id, int(amount),
    )
    if int(row["progress"]) >= int(row["target"]):
        try:
            await succeed(bot, cid)
        except Exception as exc:
            log.exception("challenge succeed failed cid=%s: %s", cid, exc)
    return row


async def succeed(bot, challenge_id: int) -> dict:
    """Finalize a challenge as succeeded, pay out contributors pro rata.

    Safe to call twice: the first UPDATE sets status='succeeded'
    atomically, and the second call will find no row to update and
    return the already-finalized row.
    """
    db = bot.db
    # Atomic flip; only succeed if currently active to avoid races.
    row = await db.fetch_one(
        """
        UPDATE guild_challenges
           SET status = 'succeeded', completed_at = NOW()
         WHERE challenge_id = $1 AND status = 'active'
        RETURNING challenge_id, guild_id, name, trigger, target, progress,
                  reward_pool_usd, started_at, ends_at, completed_at, status
        """,
        int(challenge_id),
    )
    if row is None:
        existing = await get(db, challenge_id)
        return existing or {}
    gid = int(row["guild_id"])
    pool = float(row["reward_pool_usd"])

    contribs = await db.fetch_all(
        """
        SELECT user_id, contribution
        FROM guild_challenge_contributions
        WHERE challenge_id = $1 AND contribution > 0
        """,
        int(challenge_id),
    )
    total = sum(int(c["contribution"]) for c in (contribs or []))
    paid: list[tuple[int, float]] = []
    if total > 0 and pool > 0 and contribs:
        for c in contribs:
            uid = int(c["user_id"])
            share = int(c["contribution"]) / total
            reward = pool * share
            if reward <= 0:
                continue
            try:
                await db.update_wallet(uid, gid, to_raw(reward))
                await db.log_tx(
                    gid, uid, "CHALLENGE_REWARD",
                    symbol_out="USD", amount_out=to_raw(reward),
                    network="usd",
                )
                await db.execute(
                    """
                    UPDATE guild_challenge_contributions
                       SET reward_paid = $1, updated_at = NOW()
                     WHERE challenge_id = $2 AND user_id = $3
                    """,
                    float(reward), int(challenge_id), uid,
                )
                paid.append((uid, reward))
            except Exception as exc:
                log.exception(
                    "challenge payout failed cid=%s uid=%s: %s",
                    challenge_id, uid, exc,
                )
    try:
        await bot.bus.publish(
            "challenge_succeeded",
            guild=bot.get_guild(gid),
            challenge_id=challenge_id,
            name=row["name"],
            trigger=row["trigger"],
            target=int(row["target"]),
            reward_pool_usd=pool,
            paid=[{"user_id": u, "reward_usd": r} for u, r in paid],
        )
    except Exception as exc:
        log.error("challenge_succeeded publish failed: %s", exc)
    return {**row, "paid": paid}


async def fail(db, challenge_id: int) -> bool:
    """Flip an active challenge to failed. No payout."""
    status = await db.execute(
        """
        UPDATE guild_challenges
           SET status = 'failed', completed_at = NOW()
         WHERE challenge_id = $1 AND status = 'active'
        """,
        int(challenge_id),
    )
    return isinstance(status, str) and status.startswith("UPDATE ") and status != "UPDATE 0"


async def check_expired(bot) -> list[int]:
    """Fail every active challenge past its deadline. Returns ids failed."""
    db = bot.db
    rows = await db.fetch_all(
        """
        SELECT challenge_id, guild_id FROM guild_challenges
        WHERE status = 'active' AND ends_at <= NOW()
        """,
    )
    failed: list[int] = []
    for r in (rows or []):
        cid = int(r["challenge_id"])
        gid = int(r["guild_id"])
        try:
            if await fail(db, cid):
                failed.append(cid)
                try:
                    await bot.bus.publish(
                        "challenge_failed",
                        guild=bot.get_guild(gid),
                        challenge_id=cid,
                    )
                except Exception as pub_exc:
                    log.error("challenge_failed publish failed: %s", pub_exc)
        except Exception as exc:
            log.exception("challenge fail failed cid=%s: %s", cid, exc)
    return failed


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
    """Subscribe every supported trigger to progress_trigger.

    Handlers are no-ops when no matching active challenge exists, so
    attaching is cheap and can stay wired all the time.
    """
    bus = bot.bus

    def _simple(trigger: str):
        async def _cb(**kw) -> None:
            uid = _extract_uid(kw.get("user") or kw.get("user_id"))
            gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
            if not (uid and gid):
                return
            try:
                await progress_trigger(bot, uid, gid, trigger)
            except Exception as exc:
                log.error("challenge progress %s failed: %s", trigger, exc)
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
                    await progress_trigger(bot, uid, gid, "block_mined")
                except Exception as exc:
                    log.error("challenge block_mined failed: %s", exc)

    async def _on_gamble(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await progress_trigger(bot, uid, gid, "gamble_play")
        if kw.get("won") or float(kw.get("delta", 0) or 0) > 0:
            await progress_trigger(bot, uid, gid, "gamble_win")

    async def _on_exploit(**kw) -> None:
        # cogs/eat_the_rich.py uses attacker= (not user=/user_id=).
        uid = _extract_uid(
            kw.get("user") or kw.get("user_id") or kw.get("attacker")
        )
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await progress_trigger(bot, uid, gid, "exploit_run")
        if kw.get("won") or kw.get("success"):
            await progress_trigger(bot, uid, gid, "exploit_win")

    async def _on_ape(**kw) -> None:
        uid = _extract_uid(kw.get("user") or kw.get("user_id"))
        gid = _extract_gid(kw.get("guild") or kw.get("guild_id"))
        if not (uid and gid):
            return
        await progress_trigger(bot, uid, gid, "gamble_play")
        if float(kw.get("net", 0) or 0) > 0:
            await progress_trigger(bot, uid, gid, "gamble_win")

    async def _on_mining_tick_complete(**kw) -> None:
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
                await progress_trigger(bot, uid, gid, "block_mined")
            except Exception as exc:
                log.error("challenge block_mined (SUN) failed: %s", exc)

    # Event -> trigger map for 1:1 events.
    _direct = {
        "work_completed":       "work_completed",
        "daily_claimed":        "daily_claimed",
        "trade":                "trade_executed",
        "trade_executed":       "trade_executed",
        "swap_trade":           "swap_executed",
        "swap_executed":        "swap_executed",
        "staked":               "stake_created",
        "lp_added":             "lp_added",
        "deposit":              "bank_deposit",
        "buddy_adopted":        "buddy_adopted",
        "buddy_battle_win":     "buddy_battle_win",
        "buddy_battle_loss":    "buddy_battle_loss",
        "buddy_arena_spawn":    "buddy_arena_spawn",
        "buddy_arena_won":      "buddy_arena_won",
        "buddy_arena_lost":     "buddy_arena_lost",
        "validator_registered": "validator_registered",
        "stone_leveled":        "stone_leveled",
    }
    for event, trigger in _direct.items():
        bus.subscribe(event, _simple(trigger))

    bus.subscribe("pow_mining_tick", _on_pow_tick)
    bus.subscribe("mining_tick_complete", _on_mining_tick_complete)
    bus.subscribe("gamble_result", _on_gamble)
    bus.subscribe("exploit_completed", _on_exploit)
    bus.subscribe("ape_completed", _on_ape)
