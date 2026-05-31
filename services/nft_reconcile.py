"""
services/nft_reconcile.py  -  JSONB <-> NFT drift reconciliation.

Compares the per-unit NFT counts in ``item_instances`` against the
canonical JSONB / scalar inventories for every user in a guild.
Returns a per-user drift report so an admin can verify the NFT
shadow layer is keeping up with the source of truth before flipping
reads.

Each row in the report is::

    {
        "user_id":     <int>,
        "kind":        <str>,            # buddy / egg / fish / bait / ...
        "catalog_key": <str>,            # 'worm' / 'wecco' / 'bronze_sword'
        "jsonb_count": <int>,            # canonical inventory count
        "nft_count":   <int>,            # unburned tokens owned
        "drift":       jsonb - nft,      # negative => NFT layer is ahead
    }

Empty drift report = NFT shadow is in sync; safe to flip reads.

Public API:
    reconcile_guild(db, guild_id) -> dict
        Returns ``{"drifts": [...], "summary": {...}}`` where summary
        is a count of drift rows by kind + a total absolute-drift
        sum so the admin can see at-a-glance how far off things are.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


async def _jsonb_counts_buddies(db: Any, guild_id: int) -> dict:
    """Per-user (species -> count) from cc_buddies (status owned/storage/auction)."""
    rows = await db.fetch_all(
        """
        SELECT owner_user_id AS uid, LOWER(species) AS species, COUNT(*) AS n
          FROM cc_buddies
         WHERE guild_id = $1
           AND status IN ('owned', 'storage', 'auction')
           AND owner_user_id IS NOT NULL
         GROUP BY owner_user_id, LOWER(species)
        """,
        guild_id,
    )
    out: dict = {}
    for r in rows or []:
        out.setdefault(int(r["uid"]), {})[str(r["species"])] = int(r["n"])
    return out


async def _jsonb_counts_held_eggs(db: Any, guild_id: int) -> dict:
    """Per-user (species -> count) from user_fishing.held_eggs JSONB list."""
    rows = await db.fetch_all(
        """
        SELECT user_id AS uid, held_eggs
          FROM user_fishing
         WHERE guild_id = $1
           AND jsonb_typeof(held_eggs) = 'array'
        """,
        guild_id,
    )
    out: dict = {}
    for r in rows or []:
        held = r.get("held_eggs") or []
        if isinstance(held, str):
            try:
                held = json.loads(held)
            except Exception:
                continue
        bucket: dict = out.setdefault(int(r["uid"]), {})
        for e in held or []:
            sp = str((e or {}).get("species") or "").lower()
            if not sp:
                continue
            bucket[sp] = bucket.get(sp, 0) + 1
    return out


async def _jsonb_counts_fish(db: Any, guild_id: int) -> dict:
    """Per-user (fish_key -> count) from user_fishing.fish_inventory."""
    rows = await db.fetch_all(
        """
        SELECT user_id AS uid, fish_inventory
          FROM user_fishing
         WHERE guild_id = $1
           AND jsonb_typeof(fish_inventory) = 'object'
        """,
        guild_id,
    )
    out: dict = {}
    for r in rows or []:
        inv = r.get("fish_inventory") or {}
        if isinstance(inv, str):
            try:
                inv = json.loads(inv)
            except Exception:
                continue
        bucket: dict = out.setdefault(int(r["uid"]), {})
        for k, entries in (inv or {}).items():
            if isinstance(entries, list):
                bucket[str(k)] = bucket.get(str(k), 0) + len(entries)
    return out


async def _jsonb_counts_count_map(
    db: Any, guild_id: int, table: str, column: str,
) -> dict:
    """Per-user (key -> count) from a JSONB count map.

    Used for bait/junk/crops/weapons/armor/consumables/crafted.
    """
    rows = await db.fetch_all(
        f"""
        SELECT user_id AS uid, {column} AS inv
          FROM {table}
         WHERE guild_id = $1
           AND jsonb_typeof({column}) = 'object'
        """,
        guild_id,
    )
    out: dict = {}
    for r in rows or []:
        inv = r.get("inv") or {}
        if isinstance(inv, str):
            try:
                inv = json.loads(inv)
            except Exception:
                continue
        bucket: dict = out.setdefault(int(r["uid"]), {})
        for k, v in (inv or {}).items():
            try:
                cnt = int(v or 0)
            except (TypeError, ValueError):
                cnt = 0
            if cnt > 0:
                bucket[str(k)] = bucket.get(str(k), 0) + cnt
    return out


async def _nft_counts_for_kind(
    db: Any, guild_id: int, kind: str,
) -> dict:
    """Per-user (catalog_key -> count) of unburned tokens for a kind.

    Joins item_instances against item_contracts to get the catalog_key.
    """
    rows = await db.fetch_all(
        """
        SELECT ii.owner_user_id AS uid,
               LOWER(ic.catalog_key) AS k,
               COUNT(*)              AS n
          FROM item_instances ii
          JOIN item_contracts ic
            ON ic.contract_id = ii.contract_id
         WHERE ii.guild_id    = $1
           AND ic.kind        = $2
           AND ii.burned_at   IS NULL
           AND ii.owner_user_id IS NOT NULL
         GROUP BY ii.owner_user_id, LOWER(ic.catalog_key)
        """,
        guild_id, kind,
    )
    out: dict = {}
    for r in rows or []:
        out.setdefault(int(r["uid"]), {})[str(r["k"])] = int(r["n"])
    return out


def _diff(jsonb_map: dict, nft_map: dict, kind: str) -> list[dict]:
    """Diff two per-user (key -> count) maps. Returns drift rows where
    jsonb != nft. Includes rows that exist on only one side.
    """
    rows: list[dict] = []
    uids = set(jsonb_map.keys()) | set(nft_map.keys())
    for uid in uids:
        j = jsonb_map.get(uid) or {}
        n = nft_map.get(uid) or {}
        keys = set(j.keys()) | set(n.keys())
        for k in keys:
            jc = int(j.get(k) or 0)
            nc = int(n.get(k) or 0)
            if jc != nc:
                rows.append({
                    "user_id":     uid,
                    "kind":        kind,
                    "catalog_key": k,
                    "jsonb_count": jc,
                    "nft_count":   nc,
                    "drift":       jc - nc,
                })
    return rows


async def reconcile_guild(db: Any, guild_id: int) -> dict:
    """Walk every inventory shape and return a drift report.

    Returns ``{"drifts": [...], "summary": {...}}``. Empty drifts =
    NFT layer is in sync with JSONB; safe to flip reads.
    """
    plan = (
        ("buddy",      _jsonb_counts_buddies),
        ("egg",        _jsonb_counts_held_eggs),
        ("fish",       _jsonb_counts_fish),
        ("bait",       lambda db, gid: _jsonb_counts_count_map(
            db, gid, "user_fishing", "bait_inventory")),
        ("junk",       lambda db, gid: _jsonb_counts_count_map(
            db, gid, "user_fishing", "junk_inventory")),
        ("crop",       lambda db, gid: _jsonb_counts_count_map(
            db, gid, "user_farming", "crop_inventory")),
        ("weapon",     lambda db, gid: _jsonb_counts_count_map(
            db, gid, "user_dungeon", "weapons_owned")),
        ("armor",      lambda db, gid: _jsonb_counts_count_map(
            db, gid, "user_dungeon", "armor_owned")),
        ("consumable", lambda db, gid: _jsonb_counts_count_map(
            db, gid, "user_dungeon", "consumables")),
        ("crafted",    lambda db, gid: _jsonb_counts_count_map(
            db, gid, "user_crafting", "crafted_inventory")),
    )

    all_drifts: list[dict] = []
    summary: dict = {"by_kind": {}, "total_abs_drift": 0, "rows": 0}

    for kind, jsonb_fn in plan:
        try:
            jsonb_map = await jsonb_fn(db, guild_id)
        except Exception:
            log.exception("reconcile_guild: jsonb fetch failed kind=%s", kind)
            continue
        try:
            nft_map = await _nft_counts_for_kind(db, guild_id, kind)
        except Exception:
            log.exception("reconcile_guild: nft fetch failed kind=%s", kind)
            continue
        drifts = _diff(jsonb_map, nft_map, kind)
        if drifts:
            summary["by_kind"][kind] = len(drifts)
            for d in drifts:
                summary["total_abs_drift"] += abs(int(d["drift"]))
            all_drifts.extend(drifts)

    summary["rows"] = len(all_drifts)
    return {"drifts": all_drifts, "summary": summary}
