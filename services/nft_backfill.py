"""
services/nft_backfill.py  -  One-shot Phase 1 mint backfill.

Walks every existing inventory table (cc_buddies, user_fishing.*,
user_farming.*, user_dungeon.*, user_crafting.*, every stone table,
shop_items rows on users) and mints one item_instances token per
unit -- so a stack of 50 COPPER becomes 50 token rows, a buddy gets
one token, a held egg gets one token, and so on.

Idempotent:
  * Each kind tracks its completion via a marker row in
    ``nft_backfill_state``. Re-running the backfill skips kinds that
    already finished.
  * Within a kind, the fetch path filters out source rows that already
    have an item_instances row (matched on source_table + source_id).

Called at startup right after :func:`services.nft_bootstrap.deploy_all_contracts`.

The backfill is intentionally cog-by-cog so a partial failure (one
inventory shape misbehaves) doesn't block the rest. Phase 2 will wire
the runtime create paths so this backfill is a one-time historical
sweep, not a recurring job.
"""
from __future__ import annotations

import logging
from typing import Any

from services import items as _items

log = logging.getLogger(__name__)


# Marker table -- created on first run. We don't ship a SQL migration for
# this because the table is purely a checkpointing aid for the Python
# backfill; keeping it here keeps the migration file count smaller and
# the schema cleaner (no migration churn for an internal artefact).
_INIT_SQL = """
CREATE TABLE IF NOT EXISTS nft_backfill_state (
    kind           TEXT PRIMARY KEY,
    completed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rows_minted    BIGINT      NOT NULL DEFAULT 0,
    notes          TEXT
);
"""


async def _ensure_state_table(db: Any) -> None:
    await db.execute(_INIT_SQL)


async def _is_done(db: Any, kind: str) -> bool:
    return bool(await db.fetch_val(
        "SELECT 1 FROM nft_backfill_state WHERE kind = $1",
        str(kind),
    ))


async def _mark_done(db: Any, kind: str, *, rows: int, notes: str = "") -> None:
    await db.execute(
        "INSERT INTO nft_backfill_state (kind, rows_minted, notes) "
        "VALUES ($1, $2, $3) ON CONFLICT (kind) DO UPDATE SET "
        "  completed_at = NOW(), rows_minted = EXCLUDED.rows_minted, "
        "  notes = EXCLUDED.notes",
        str(kind), int(rows), str(notes or ""),
    )


async def _already_minted(
    db: Any, source_table: str, source_id: str,
) -> bool:
    """Idempotency guard: was a token already minted for this exact
    source row? Used by every backfill loop so a partial-failure rerun
    doesn't double-mint.
    """
    return bool(await db.fetch_val(
        "SELECT 1 FROM item_instances "
        "WHERE source_table = $1 AND source_id = $2 LIMIT 1",
        str(source_table), str(source_id),
    ))


async def _link_pre_phase1_tokens(db: Any) -> int:
    """Retro-fit contract_id + unit_index onto existing item_instances
    rows that pre-date Phase 1 (lazily minted by services/auction.py
    before the NFT layer).

    Picks a contract by (kind, metadata->>'species' / metadata->>'ref' /
    catalog_key fallback). Allocates a fresh unit_index off the
    contract's monotonic counter. Idempotent: rows that already have a
    contract_id are skipped.
    """
    rows = await db.fetch_all(
        """
        SELECT token_id, kind, metadata
          FROM item_instances
         WHERE contract_id IS NULL
        """,
    )
    n = 0
    for r in rows or []:
        kind = str(r.get("kind") or "").lower()
        if not kind:
            continue
        md = r.get("metadata") or {}
        if isinstance(md, str):
            try:
                import json as _json
                md = _json.loads(md)
            except Exception:
                md = {}
        # Try several keys to find the catalog match. Priority order
        # mirrors how the auction-house lock helpers stamp metadata.
        candidates = [
            md.get("species"),                       # buddies, eggs
            md.get("fish_key"),                      # fish entries
            md.get("ref"),                           # fungibles (ore, etc.)
            md.get("name"),                          # fallback
            md.get("catalog_key"),                   # explicit
        ]
        catalog_key = next(
            (str(c).lower() for c in candidates if c),
            "",
        )
        if not catalog_key:
            continue
        addr = _items.contract_address(kind, catalog_key)
        contract = await _items.get_contract(db, address=addr)
        if not contract:
            continue
        cid = int(contract["contract_id"])
        # Allocate next unit_index off the contract counter.
        last = await db.fetch_val(
            "SELECT COALESCE(MAX(unit_index), 0) FROM item_instances "
            "WHERE contract_id = $1",
            cid,
        )
        new_idx = int(last or 0) + 1
        await db.execute(
            """
            UPDATE item_instances
               SET contract_id = $2,
                   unit_index  = $3,
                   minted_at   = COALESCE(minted_at, NOW()),
                   mint_source = COALESCE(mint_source, 'phase1.link'),
                   updated_at  = NOW()
             WHERE token_id = $1
            """,
            str(r["token_id"]), cid, new_idx,
        )
        n += 1
    return n


# ─── Per-kind backfills ─────────────────────────────────────────────────────


async def _backfill_buddies(db: Any) -> int:
    """One token per cc_buddies row (status owned/storage/auction).

    The unit_index allocator inside mint_unit is contract-scoped, so we
    just mint per row; concurrency is fine because there's only one
    backfill caller running.
    """
    rows = await db.fetch_all(
        """
        SELECT b.id, b.guild_id, b.owner_user_id, b.species,
               b.rarity_tier, b.gender, b.level, b.xp, b.status
          FROM cc_buddies b
         WHERE b.status IN ('owned', 'storage', 'auction')
           AND NOT EXISTS (
               SELECT 1 FROM item_instances ii
                WHERE ii.source_table = 'cc_buddies'
                  AND ii.source_id    = b.id::text
           )
         ORDER BY b.id
        """,
    )
    n = 0
    for r in rows or []:
        species = str((r.get("species") or "")).lower()
        if not species:
            continue
        addr = _items.contract_address("buddy", species)
        try:
            await _items.mint_unit(
                db,
                guild_id=int(r["guild_id"]),
                contract_address=addr,
                owner_user_id=(
                    int(r["owner_user_id"]) if r.get("owner_user_id") else None
                ),
                metadata={
                    "species":     species,
                    "rarity_tier": int(r.get("rarity_tier") or 1),
                    "gender":      str(r.get("gender") or "").upper(),
                    "level":       int(r.get("level") or 1),
                    "xp":          int(r.get("xp") or 0),
                    "buddy_id":    int(r["id"]),
                },
                mint_source="backfill.buddies",
                source_table="cc_buddies",
                source_id=int(r["id"]),
            )
            n += 1
        except Exception:
            log.exception("backfill buddy %s failed", r.get("id"))
    return n


async def _backfill_held_eggs(db: Any) -> int:
    """One token per entry in user_fishing.held_eggs JSONB list.

    held_eggs is a list of {"species", "rarity_tier", "rolled_at", ...}
    objects. We use a per-(user, species, list_index) source_id so
    backfill is reproducible.
    """
    rows = await db.fetch_all(
        """
        SELECT guild_id, user_id, held_eggs
          FROM user_fishing
         WHERE jsonb_typeof(held_eggs) = 'array'
           AND jsonb_array_length(held_eggs) > 0
        """,
    )
    n = 0
    for r in rows or []:
        held = r.get("held_eggs") or []
        if isinstance(held, str):
            try:
                import json as _json
                held = _json.loads(held)
            except Exception:
                continue
        for i, egg in enumerate(held or []):
            species = str((egg or {}).get("species") or "").lower()
            if not species:
                continue
            addr = _items.contract_address("egg", species)
            src_id = f"{int(r['user_id'])}:{species}:{i}:{egg.get('rolled_at') or ''}"
            if await _already_minted(db, "user_fishing.held_eggs", src_id):
                continue
            try:
                await _items.mint_unit(
                    db,
                    guild_id=int(r["guild_id"]),
                    contract_address=addr,
                    owner_user_id=int(r["user_id"]),
                    metadata={
                        "species":     species,
                        "rarity_tier": int((egg or {}).get("rarity_tier") or 1),
                        "rolled_at":   str((egg or {}).get("rolled_at") or ""),
                        "from":        str((egg or {}).get("from") or ""),
                    },
                    mint_source="backfill.held_eggs",
                    source_table="user_fishing.held_eggs",
                    source_id=src_id,
                )
                n += 1
            except Exception as e:
                log.error(
                    "backfill held_egg failed gid=%s uid=%s i=%s: %r",
                    r.get("guild_id"), r.get("user_id"), i, e,
                    exc_info=True,
                )
    return n


async def _backfill_count_jsonb(
    db: Any,
    *,
    table: str,
    column: str,
    kind: str,
    mint_source: str,
    fallback_kinds: tuple[str, ...] = (),
) -> int:
    """Generic backfill for JSONB count maps -- mints N tokens per
    {key: count} entry. Used by bait, junk, crops, weapons, armor,
    consumables, crafted.

    The source_id is per-(uid, key, n) so each minted unit has a unique
    backfill marker; the unit_index counter on the contract still
    allocates the on-chain serial.

    ``fallback_kinds`` lets a column whose entries straddle multiple
    contract kinds resolve them all. Example: ``user_dungeon.consumables``
    holds both pure dungeon consumables (kind='consumable') AND crafted
    outputs that route to consumables via ``apply: consum/*`` in
    crafting recipes (kind='crafted'). Without a fallback the second
    set crashes with "unknown contract `consumable.anglers_paste`".
    """
    rows = await db.fetch_all(
        f"""
        SELECT guild_id, user_id, {column} AS inv
          FROM {table}
         WHERE jsonb_typeof({column}) = 'object'
        """,
    )
    n = 0
    for r in rows or []:
        inv = r.get("inv") or {}
        if isinstance(inv, str):
            try:
                import json as _json
                inv = _json.loads(inv)
            except Exception:
                continue
        for key, cnt in (inv or {}).items():
            try:
                cnt_int = int(cnt or 0)
            except (TypeError, ValueError):
                continue
            if cnt_int <= 0:
                continue

            # Resolve to the FIRST kind whose contract exists. Try the
            # primary kind first, then each fallback in order. Cached
            # per key so a 50-item inventory doesn't issue 50 lookups.
            addr: str | None = None
            for try_kind in (kind, *fallback_kinds):
                cand_addr = _items.contract_address(try_kind, str(key))
                if await _items.get_contract(db, address=cand_addr):
                    addr = cand_addr
                    break
            if addr is None:
                # Demoted from WARNING to DEBUG: missing contracts for
                # specific item keys (e.g. crafted fertilizers, dungeon
                # junk subkinds) are an expected no-op when those items
                # aren't NFT-eligible by current catalog policy. The
                # per-label "X -> N rows" summary below still logs at
                # INFO so operators see the aggregate.
                log.debug(
                    "backfill %s: no contract for %s.%s "
                    "(also tried %s) (skipping)",
                    mint_source, kind, key,
                    ", ".join(f"{k}.{key}" for k in fallback_kinds) or "no fallbacks",
                )
                # Don't ``break`` here -- the next inventory key might
                # resolve fine. The original code broke out of the
                # whole user's inventory on the first unknown contract,
                # which masked working keys behind a single bad one.
                continue
            for unit_n in range(cnt_int):
                src_id = f"{int(r['user_id'])}:{key}:{unit_n}"
                if await _already_minted(
                    db, f"{table}.{column}", src_id,
                ):
                    continue
                try:
                    await _items.mint_unit(
                        db,
                        guild_id=int(r["guild_id"]),
                        contract_address=addr,
                        owner_user_id=int(r["user_id"]),
                        metadata={"catalog_key": str(key)},
                        mint_source=mint_source,
                        source_table=f"{table}.{column}",
                        source_id=src_id,
                    )
                    n += 1
                except Exception as e:
                    log.error(
                        "backfill %s failed gid=%s uid=%s key=%s addr=%s: %r",
                        mint_source, r.get("guild_id"), r.get("user_id"),
                        key, addr, e, exc_info=True,
                    )
                    break
    return n


async def _backfill_fish_inventory(db: Any) -> int:
    """user_fishing.fish_inventory is a {fish_key: [{lbs, ts}, ...]} dict.

    Each list entry is one caught fish, so each gets its own token
    with the lbs/ts captured into metadata.
    """
    rows = await db.fetch_all(
        """
        SELECT guild_id, user_id, fish_inventory
          FROM user_fishing
         WHERE jsonb_typeof(fish_inventory) = 'object'
        """,
    )
    n = 0
    for r in rows or []:
        inv = r.get("fish_inventory") or {}
        if isinstance(inv, str):
            try:
                import json as _json
                inv = _json.loads(inv)
            except Exception:
                continue
        for fish_key, entries in (inv or {}).items():
            if not isinstance(entries, list):
                continue
            addr = _items.contract_address("fish", str(fish_key))
            for i, entry in enumerate(entries):
                src_id = f"{int(r['user_id'])}:{fish_key}:{i}:{(entry or {}).get('ts') or ''}"
                if await _already_minted(
                    db, "user_fishing.fish_inventory", src_id,
                ):
                    continue
                try:
                    await _items.mint_unit(
                        db,
                        guild_id=int(r["guild_id"]),
                        contract_address=addr,
                        owner_user_id=int(r["user_id"]),
                        metadata={
                            "fish_key": str(fish_key),
                            "lbs":      float((entry or {}).get("lbs") or 0.0),
                            "ts":       int((entry or {}).get("ts") or 0),
                        },
                        mint_source="backfill.fish_inventory",
                        source_table="user_fishing.fish_inventory",
                        source_id=src_id,
                    )
                    n += 1
                except ValueError as e:
                    log.warning(
                        "backfill fish: no contract for fish.%s (%r) (skipping)",
                        fish_key, e,
                    )
                    break
                except Exception as e:
                    log.error(
                        "backfill fish failed gid=%s uid=%s key=%s: %r",
                        r.get("guild_id"), r.get("user_id"), fish_key, e,
                        exc_info=True,
                    )
    return n


async def _backfill_ore(db: Any) -> int:
    """Ore (COPPER / SILVER / GOLD) currently lives in wallet_holdings as
    fungible token symbols, not as scalar columns on user_dungeon. Until
    the fungible-vs-NFT design decision lands (do we want each ore unit
    minted as its own NFT, or treat ore as a currency in wallets like
    LURE/HRV?), this backfill is a no-op. The ``ore`` contracts deployed
    by the bootstrap are kept ready so a future runtime mint can attach
    to them once the call is made.
    """
    return 0


# ─── Top-level orchestrator ─────────────────────────────────────────────────


async def run_backfill(db: Any, *, force: bool = False) -> dict[str, int]:
    """Run every per-kind backfill.

    Per-row idempotency (NOT EXISTS query filters + ``_already_minted``
    checks) is the actual guard against double-mints, so each scan is
    safe to repeat. The ``nft_backfill_state`` rows still get written
    for observability + as a marker for "this kind has been swept at
    least once," but they are NOT used to short-circuit subsequent
    runs -- otherwise a buggy first boot that marked a kind "done"
    with 0 rows would permanently lock the backfill out.

    ``force=True`` is a no-op today; the flag is kept for the admin
    command's call signature and to make future "force re-link" or
    "force re-walk-burned" semantics easy to add.

    Returns a per-kind row count summary. Best-effort: a failure in
    one kind is logged + skipped so the rest still progress.
    """
    del force  # see docstring -- accepted, currently unused
    await _ensure_state_table(db)
    summary: dict[str, int] = {}

    # Step 0: link any pre-Phase-1 item_instances rows (e.g. lazy
    # auction-house mints) to a contract_id + unit_index so the NFT
    # layer is internally consistent before we add new mints. Always
    # safe to re-run -- the helper already filters out rows that
    # already have a contract_id.
    try:
        n = await _link_pre_phase1_tokens(db)
        await _mark_done(db, "_link_existing", rows=n)
        summary["_link_existing"] = n
        if n:
            log.info("nft_backfill: linked %s pre-phase-1 tokens", n)
    except Exception:
        log.exception("nft_backfill: _link_existing aborted")

    # Ore is fungible (wallet_holdings + oracle prices); no NFT layer.
    # _backfill_ore stays defined as a no-op for callers but is not run.
    plan = (
        ("buddies",      _backfill_buddies),
        ("held_eggs",    _backfill_held_eggs),
        ("fish",         _backfill_fish_inventory),
    )

    for kind_label, fn in plan:
        try:
            n = await fn(db)
            await _mark_done(db, kind_label, rows=n)
            if n:
                summary[kind_label] = n
                log.info("nft_backfill: %s -> %s rows", kind_label, n)
        except Exception:
            log.exception("nft_backfill: %s aborted", kind_label)
            summary[kind_label] = -1

    # Count-map JSONB inventories. Each runs through the generic helper
    # with a (table, column, kind, fallback_kinds) tuple. Fallback kinds
    # are tried in order when the primary kind has no contract for a
    # catalog_key -- this covers cross-catalog routes like crafted
    # outputs that route into the dungeon consumables column via
    # ``apply: consum/<key>`` in crafting_config.CRAFT_ITEMS.
    count_plan = (
        ("bait",        "user_fishing", "bait_inventory",      "bait",
         "backfill.bait", ("crafted",)),
        ("junk",        "user_fishing", "junk_inventory",      "junk",
         "backfill.junk", ()),
        ("crops",       "user_farming", "crop_inventory",      "crop",
         "backfill.crops", ()),
        ("weapons",     "user_dungeon", "weapons_owned",       "weapon",
         "backfill.weapons", ()),
        ("armor",       "user_dungeon", "armor_owned",         "armor",
         "backfill.armor", ()),
        # Crafted-as-consumable: anglers_paste, expedition_ration, and
        # every other ``apply: consum/*`` recipe land here under their
        # crafted-kind contract; fall back to ``crafted`` when the
        # primary ``consumable`` lookup misses.
        ("consumables", "user_dungeon", "consumables",         "consumable",
         "backfill.consumables", ("crafted",)),
        ("crafted",     "user_crafting", "crafted_inventory",  "crafted",
         "backfill.crafted", ()),
        # Originally missing: dungeon JUNK column (mats / salvage like
        # enchanted_thread) shares the table+column shape with fishing
        # junk but a different catalog. Same for RELICS. Both kinds
        # got contracts deployed in nft_bootstrap from the same fix
        # round; this backfill mints tokens for every existing JSONB
        # count so legacy inventories are AH-listable too.
        ("dungeon_junk", "user_dungeon", "junk_inventory",      "junk",
         "backfill.dungeon_junk", ()),
        ("relics",       "user_dungeon", "relics_owned",        "relic",
         "backfill.relics", ()),
        # Crafted fertilizers route to user_farming.fertilizer_inventory
        # via ``apply: fert/*`` recipes. Same crafted-as-X bridge as
        # consumables above.
        ("fertilizers", "user_farming", "fertilizer_inventory", "crafted",
         "backfill.fertilizers", ()),
    )
    for label, table, column, kind, src, fallback_kinds in count_plan:
        try:
            n = await _backfill_count_jsonb(
                db, table=table, column=column,
                kind=kind, mint_source=src,
                fallback_kinds=fallback_kinds,
            )
            await _mark_done(db, label, rows=n)
            if n:
                summary[label] = n
            log.info("nft_backfill: %s -> %s rows", label, n)
        except Exception:
            log.exception("nft_backfill: %s aborted", label)
            summary[label] = -1

    log.info("nft_backfill complete: %s", summary)
    return summary
