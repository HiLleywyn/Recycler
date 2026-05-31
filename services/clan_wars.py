"""V3 Pillar 3: Clan Wars service.

Public surface every cog uses:
    record(db, gid, user_id, group_id, node_id, points)
        -- single-line hook from cogs/eat_the_rich.py / chain_group.py / etc.
    queue_group(db, gid, group_id, entry_raw)
    pair_queue(db, gid) -> list[(group_a, group_b)]
    create_match(db, gid, group_a, group_b, *, entry_raw, duration_days=7)
    settle_finished(db, gid)
    active_match(db, gid, group_id) -> match dict or None

The full match lifecycle is:
    1) groups call ``queue_group`` paying an entry fee from treasury
    2) ``pair_queue`` matches similar-NW groups
    3) ``create_match`` opens the 12-node board and a 7-day window
    4) cog hooks record contributions into ``clan_war_contributions``
    5) ``settle_finished`` resolves winners + splits the pool
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone


log = logging.getLogger(__name__)


# The fixed 12-node board. Each one ties to a single economic activity.
NODES: list[dict] = [
    {"id": "mine",      "label": "Mine",       "source": "chain_group.mine"},
    {"id": "vault",     "label": "Vault",      "source": "vault.deposit"},
    {"id": "bazaar",    "label": "Bazaar",     "source": "auction.sale"},
    {"id": "forge",     "label": "Forge",      "source": "crafting.craft"},
    {"id": "lighthouse","label": "Lighthouse", "source": "trade.swap"},
    {"id": "reef",      "label": "Reef",       "source": "fishing.legendary"},
    {"id": "grove",     "label": "Grove",      "source": "farming.harvest"},
    {"id": "crypt",     "label": "Crypt",      "source": "dungeon.clear"},
    {"id": "spire",     "label": "Spire",      "source": "validator.block"},
    {"id": "orchard",   "label": "Orchard",    "source": "liquidity.add"},
    {"id": "forum",     "label": "Forum",      "source": "governance.vote"},
    {"id": "apex",      "label": "Apex",       "source": "exploit.win",
     "weight": 3.0},
]
NODE_IDS = [n["id"] for n in NODES]
NODE_BY_ID = {n["id"]: n for n in NODES}


async def queue_group(
    db, guild_id: int, group_id: int, entry_raw: int,
) -> tuple[bool, str]:
    """Add a group to the matchmaking queue with their entry fee."""
    try:
        await db.execute(
            "INSERT INTO clan_war_queue (guild_id, group_id, entry_paid_raw) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, group_id) DO NOTHING",
            guild_id, group_id, max(0, int(entry_raw)),
        )
        return True, "Queued."
    except Exception:
        log.exception("clan_wars: queue failed gid=%s group=%s", guild_id, group_id)
        return False, "Queue failed."


async def queued(db, guild_id: int) -> list[dict]:
    try:
        rows = await db.fetch_all(
            "SELECT group_id, queued_at, entry_paid_raw FROM clan_war_queue "
            "WHERE guild_id = $1 ORDER BY queued_at ASC",
            guild_id,
        )
        return [dict(r) for r in rows]
    except Exception:
        return []


async def pair_queue(db, guild_id: int) -> list[tuple[int, int, int]]:
    """Pair queued groups for matchmaking.

    Returns ``[(group_a, group_b, pooled_entry_raw)]`` for each pair
    that should now open a match. The caller is responsible for
    actually creating the match rows and dequeuing the groups.

    Simple algorithm: FIFO-order pair-by-pair so the queue stays small
    and operators can verify pairings deterministically. Net-worth-
    matched matching is a future improvement.
    """
    rows = await queued(db, guild_id)
    out: list[tuple[int, int, int]] = []
    while len(rows) >= 2:
        a = rows.pop(0)
        b = rows.pop(0)
        pooled = int(a.get("entry_paid_raw") or 0) + int(b.get("entry_paid_raw") or 0)
        out.append((int(a["group_id"]), int(b["group_id"]), pooled))
    return out


async def create_match(
    db, guild_id: int, group_a: int, group_b: int,
    *, entry_raw: int, duration_days: int = 7,
) -> int | None:
    """Open a fresh match row + 12 zero-score node rows. Returns match_id."""
    try:
        ends = datetime.now(timezone.utc) + timedelta(days=duration_days)
        row = await db.fetch_one(
            "INSERT INTO clan_war_matches "
            "(guild_id, group_a_id, group_b_id, ends_at, entry_pool_raw) "
            "VALUES ($1, $2, $3, $4, $5) "
            "RETURNING id",
            guild_id, group_a, group_b, ends, max(0, int(entry_raw)),
        )
        if not row:
            return None
        match_id = int(row["id"])
        for node in NODES:
            await db.execute(
                "INSERT INTO clan_war_nodes (match_id, node_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                match_id, node["id"],
            )
        # Dequeue the participants
        await db.execute(
            "DELETE FROM clan_war_queue WHERE guild_id = $1 "
            "AND group_id = ANY($2::BIGINT[])",
            guild_id, [group_a, group_b],
        )
        return match_id
    except Exception:
        log.exception(
            "clan_wars: create_match failed gid=%s a=%s b=%s",
            guild_id, group_a, group_b,
        )
        return None


async def active_match(db, guild_id: int, group_id: int) -> dict | None:
    """Return the live match for a given group, or None."""
    try:
        row = await db.fetch_one(
            "SELECT * FROM clan_war_matches "
            "WHERE guild_id = $1 AND status = 'live' "
            "AND (group_a_id = $2 OR group_b_id = $2) "
            "ORDER BY started_at DESC LIMIT 1",
            guild_id, group_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


async def get_match(db, match_id: int) -> dict | None:
    try:
        row = await db.fetch_one(
            "SELECT * FROM clan_war_matches WHERE id = $1", match_id,
        )
        return dict(row) if row else None
    except Exception:
        return None


async def node_scores(db, match_id: int) -> list[dict]:
    try:
        rows = await db.fetch_all(
            "SELECT * FROM clan_war_nodes WHERE match_id = $1",
            match_id,
        )
        return [dict(r) for r in rows]
    except Exception:
        return []


async def record(
    db, guild_id: int, user_id: int, group_id: int,
    node_id: str, points: int,
) -> bool:
    """One-line hook called by every event-producing cog.

    Finds the user's active match in this guild and adds ``points`` to
    the appropriate side of the node scoreboard. No-ops cleanly when
    the user's group has no active match (so the call is safe to wire
    into every cog without a per-cog check).
    """
    if node_id not in NODE_BY_ID or points <= 0:
        return False
    match = await active_match(db, guild_id, group_id)
    if not match:
        return False
    weight = float(NODE_BY_ID[node_id].get("weight", 1.0))
    weighted = max(1, int(points * weight))
    side = "a" if int(match["group_a_id"]) == int(group_id) else "b"
    try:
        async with db.atomic():
            await db.execute(
                f"UPDATE clan_war_nodes SET {side}_score = {side}_score + $3 "
                f"WHERE match_id = $1 AND node_id = $2",
                int(match["id"]), node_id, weighted,
            )
            await db.execute(
                "INSERT INTO clan_war_contributions "
                "(match_id, user_id, group_id, node_id, points) "
                "VALUES ($1, $2, $3, $4, $5)",
                int(match["id"]), user_id, group_id, node_id, weighted,
            )
        return True
    except Exception:
        log.exception(
            "clan_wars: record failed gid=%s match=%s node=%s",
            guild_id, match.get("id"), node_id,
        )
        return False


async def scoreline(db, match_id: int) -> dict:
    """Return ``{a_nodes, b_nodes, a_total, b_total, tied}`` for a match."""
    rows = await node_scores(db, match_id)
    a_nodes = b_nodes = 0
    a_total = b_total = 0
    for r in rows:
        a, b = int(r["a_score"] or 0), int(r["b_score"] or 0)
        a_total += a
        b_total += b
        if a > b:
            a_nodes += 1
        elif b > a:
            b_nodes += 1
    return {
        "a_nodes": a_nodes, "b_nodes": b_nodes,
        "a_total": a_total, "b_total": b_total,
        "tied": a_nodes == b_nodes,
    }


async def settle_finished(db, guild_id: int) -> int:
    """Settle any match whose window has closed. Returns count settled."""
    try:
        rows = await db.fetch_all(
            "SELECT id, group_a_id, group_b_id, entry_pool_raw "
            "FROM clan_war_matches "
            "WHERE guild_id = $1 AND status = 'live' AND ends_at <= now()",
            guild_id,
        )
    except Exception:
        return 0
    settled = 0
    for row in rows:
        match_id = int(row["id"])
        sl = await scoreline(db, match_id)
        winner: int | None = None
        if sl["a_nodes"] > sl["b_nodes"]:
            winner = int(row["group_a_id"])
        elif sl["b_nodes"] > sl["a_nodes"]:
            winner = int(row["group_b_id"])
        # Pool split: winner 80%, loser 20% (consolation). Tied -> 50/50.
        pool = int(row.get("entry_pool_raw") or 0)
        try:
            async with db.atomic():
                await db.execute(
                    "UPDATE clan_war_matches "
                    "SET status = 'settled', settled_at = now(), winner_group = $2 "
                    "WHERE id = $1",
                    match_id, winner,
                )
                if pool > 0:
                    if winner is None:
                        # Tie -- split evenly
                        half = pool // 2
                        for gid in (int(row["group_a_id"]), int(row["group_b_id"])):
                            await _credit_group_treasury(db, guild_id, gid, half)
                    else:
                        loser = (
                            int(row["group_b_id"])
                            if winner == int(row["group_a_id"])
                            else int(row["group_a_id"])
                        )
                        await _credit_group_treasury(db, guild_id, winner, int(pool * 0.80))
                        await _credit_group_treasury(db, guild_id, loser, int(pool * 0.20))
            settled += 1
        except Exception:
            log.exception("clan_wars: settle failed match=%s", match_id)
            continue
    return settled


async def _credit_group_treasury(
    db, guild_id: int, group_id: int, amount_raw: int,
) -> None:
    """Best-effort: credit ``amount_raw`` to a group's treasury.

    Falls back to a noop if the group treasury table isn't loaded
    (i.e. dev environment without the groups cog).
    """
    if amount_raw <= 0:
        return
    try:
        await db.execute(
            "UPDATE groups SET treasury_raw = treasury_raw + $3 "
            "WHERE guild_id = $1 AND group_id = $2",
            guild_id, group_id, int(amount_raw),
        )
    except Exception:
        log.debug(
            "clan_wars: treasury credit skipped gid=%s group=%s",
            guild_id, group_id,
            exc_info=True,
        )
