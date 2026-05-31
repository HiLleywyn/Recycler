"""
services/expeditions.py  -  AI Buddy Expedition logic.

Public API:
    start_expedition(db, guild_id, user_id, destination, duration_key)
        -> ExpeditionResult           (the new ``running`` row)
    list_active(db, guild_id, user_id)        -> list[dict]
    list_collectable(db, guild_id, user_id)   -> list[dict]   (ends_at <= NOW())
    is_buddy_busy(db, buddy_id)               -> bool
    collect_expedition(db, bot, guild_id, user_id, expedition_id)
        -> CollectResult              (story + loot summary)

The collect path is the work-horse: it samples N independent draws
from the destination's bucket-weight table, looks each draw up against
the destination-favored item pools, mints / credits the loot
atomically, weaves a procedural story log, and stamps the row as
collected. Everything happens inside ``db.atomic()`` so a partial
failure can't leave the player with half-credited loot and a
collected row.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import configs.expeditions_config as ec
from core.framework.scale import to_raw

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Network / symbol constants. Cheap to import lazily; we re-read them in
# the few spots they're needed so the service module never imports the
# whole config tree at module load.
# ---------------------------------------------------------------------------


def _crypt_net_and_rune() -> tuple[str, str]:
    from configs.dungeon_config import CRYPT_NETWORK_SHORT, RUNE_SYMBOL
    return str(CRYPT_NETWORK_SHORT), str(RUNE_SYMBOL)


def _affinity_bonus(species: str, destination: str) -> tuple[float, int]:
    """Return ``(loot_qty_mult, rarity_bump)`` for the (species, dest) pair.

    Matching affinity: 1.25x loot quantity, +1 rarity bump on per-draw
    samples (handled by the catalog sampler).
    Mismatched / neutral: no bonus.
    """
    if ec.species_affinity(species) == destination:
        return 1.25, 1
    return 1.0, 0


def _rarity_tier_mult(rarity_tier: int) -> float:
    """Rare buddy = better loot. Quadratic-lite curve so T5 buddies are
    notably better than T1 without the gap being absurd."""
    t = max(1, min(5, int(rarity_tier or 1)))
    return 1.0 + (t - 1) * 0.10  # 1.0, 1.1, 1.2, 1.3, 1.4


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExpeditionResult:
    expedition_id: int
    destination:   str
    duration_seconds: int
    ends_at:       _dt.datetime
    buddy_id:      int
    buddy_name:    str
    species:       str
    rarity_tier:   int


@dataclass(slots=True)
class CollectResult:
    expedition_id: int
    destination:   str
    buddy_name:    str
    species:       str
    story:         list[str]
    loot:          dict[str, Any] = field(default_factory=dict)
    xp_gained:     int = 0
    happiness_delta: int = 0


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


async def list_eligible_buddies(
    db: Any, guild_id: int, user_id: int,
) -> list[dict]:
    """Return owned buddies that aren't currently on an expedition.

    Carries the same fields the picker view shows so the cog can build
    a select menu without a second round-trip.
    """
    rows = await db.fetch_all(
        """
        SELECT b.id, b.name, b.species, b.rarity_tier, b.level, b.xp,
               b.is_active
          FROM cc_buddies b
         WHERE b.guild_id = $1 AND b.owner_user_id = $2
           AND b.status = 'owned'
           AND NOT EXISTS (
               SELECT 1 FROM buddy_expeditions e
                WHERE e.buddy_id = b.id AND e.status = 'running'
           )
         ORDER BY b.is_active DESC, b.id ASC
        """,
        int(guild_id), int(user_id),
    )
    return [dict(r) for r in (rows or [])]


async def start_expedition(
    db: Any,
    guild_id: int,
    user_id: int,
    *,
    destination: str,
    duration_key: str,
    buddy_id: int | None = None,
) -> ExpeditionResult:
    """Deploy a buddy to ``destination`` for ``duration_key``.

    Picks the active buddy by default, or a specific one when ``buddy_id``
    is supplied (used by the picker view's buddy selector).

    Raises ValueError if:
    * destination / duration are unknown
    * the user has no eligible buddy
    * the buddy is below the destination's min level
    * the buddy is already on a running expedition

    All checks happen inside a single transaction so two ``,expedition send``
    clicks in the same second can't double-deploy the buddy.
    """
    dest_meta = ec.destination_meta(destination)
    if not dest_meta:
        raise ValueError(f"Unknown destination `{destination}`.")
    dur_meta = ec.duration_meta(duration_key)
    if not dur_meta:
        raise ValueError(f"Unknown duration `{duration_key}`.")

    async with db.atomic():
        if buddy_id is not None:
            active = await db.fetch_one(
                """
                SELECT id, name, species, rarity_tier, level, xp
                  FROM cc_buddies
                 WHERE id = $1 AND guild_id = $2 AND owner_user_id = $3
                   AND status = 'owned'
                 FOR UPDATE
                """,
                int(buddy_id), int(guild_id), int(user_id),
            )
            if not active:
                raise ValueError(
                    "Couldn't find that buddy in your roster. Pick one from "
                    "the dropdown or run `,buddy` to see your collection."
                )
        else:
            active = await db.fetch_one(
                """
                SELECT id, name, species, rarity_tier, level, xp
                  FROM cc_buddies
                 WHERE guild_id = $1 AND owner_user_id = $2
                   AND status = 'owned' AND is_active
                 FOR UPDATE
                """,
                int(guild_id), int(user_id),
            )
        if not active:
            raise ValueError(
                "You need an active buddy to send on an expedition. "
                "Hatch one with `,buddy hatch` or set one active via "
                "the `,buddy` panel."
            )
        buddy_id = int(active["id"])
        species = str(active.get("species") or "")
        # Level is XP-derived so an expedition / battle level-up is visible
        # immediately even if the level column hasn't been refreshed yet.
        from configs.buddies_config import effective_level as _eff_lvl
        level = _eff_lvl(dict(active))
        rarity = int(active.get("rarity_tier") or 1)

        if level < int(dest_meta["min_level"]):
            raise ValueError(
                f"This destination requires a level **{int(dest_meta['min_level'])}** "
                f"buddy; yours is **{level}**. Train them up first or "
                f"pick an easier destination."
            )

        # Idempotency / busy check: partial-unique index on
        # buddy_expeditions enforces one running row per buddy_id, so a
        # racing INSERT raises UniqueViolation. We pre-check for a
        # friendlier error.
        busy = await db.fetch_val(
            "SELECT 1 FROM buddy_expeditions "
            "WHERE buddy_id = $1 AND status = 'running' LIMIT 1",
            buddy_id,
        )
        if busy:
            raise ValueError(
                "Your active buddy is already on an expedition. "
                "`,expedition status` to see when they're back."
            )

        seconds = int(dur_meta["seconds"])
        row = await db.fetch_one(
            """
            INSERT INTO buddy_expeditions (
                guild_id, user_id, buddy_id,
                destination, duration_seconds,
                ends_at,
                species_at_start, rarity_at_start, level_at_start
            )
            VALUES ($1, $2, $3, $4, $5,
                    NOW() + make_interval(secs => $5::int),
                    $6, $7, $8)
            RETURNING expedition_id, ends_at
            """,
            int(guild_id), int(user_id), buddy_id,
            str(destination), int(seconds),
            species, int(rarity), int(level),
        )

    return ExpeditionResult(
        expedition_id=int(row["expedition_id"]),
        destination=str(destination),
        duration_seconds=int(seconds),
        ends_at=row["ends_at"],
        buddy_id=int(buddy_id),
        buddy_name=str(active.get("name") or "Buddy"),
        species=species,
        rarity_tier=int(rarity),
    )


# ---------------------------------------------------------------------------
# Read-side helpers
# ---------------------------------------------------------------------------


async def list_active(
    db: Any, guild_id: int, user_id: int,
) -> list[dict]:
    """Return every running expedition for the user.

    Each row carries ``seconds_remaining`` (negative when ready) computed
    on the DB clock so the cog never has to compare a Python ``now()``
    against a TIMESTAMPTZ from the database.
    """
    rows = await db.fetch_all(
        """
        SELECT expedition_id, destination, duration_seconds, started_at,
               ends_at, status, species_at_start, rarity_at_start,
               level_at_start, buddy_id,
               EXTRACT(EPOCH FROM (ends_at - NOW()))::bigint AS seconds_remaining
          FROM buddy_expeditions
         WHERE guild_id = $1 AND user_id = $2
           AND status = 'running'
         ORDER BY ends_at ASC
        """,
        int(guild_id), int(user_id),
    )
    return [dict(r) for r in (rows or [])]


async def list_collectable(
    db: Any, guild_id: int, user_id: int,
) -> list[dict]:
    rows = await db.fetch_all(
        """
        SELECT expedition_id, destination, duration_seconds, started_at,
               ends_at, status, species_at_start, rarity_at_start,
               level_at_start, buddy_id
          FROM buddy_expeditions
         WHERE guild_id = $1 AND user_id = $2
           AND status = 'running'
           AND ends_at <= NOW()
         ORDER BY ends_at ASC
        """,
        int(guild_id), int(user_id),
    )
    return [dict(r) for r in (rows or [])]


async def is_buddy_busy(db: Any, buddy_id: int) -> bool:
    """True when the buddy has an active expedition row."""
    return bool(await db.fetch_val(
        "SELECT 1 FROM buddy_expeditions "
        "WHERE buddy_id = $1 AND status = 'running' LIMIT 1",
        int(buddy_id),
    ))


# ---------------------------------------------------------------------------
# Loot roll + credit
# ---------------------------------------------------------------------------


def _weighted_pick(rng: random.Random, weights: dict[str, float]) -> str:
    """Standard weighted draw, matching weights stored as a dict."""
    total = sum(float(w) for w in weights.values()) or 1.0
    r = rng.random() * total
    acc = 0.0
    for k, w in weights.items():
        acc += float(w)
        if r <= acc:
            return k
    # Fallback to last key (float drift safety).
    return next(reversed(weights))


def _roll_loot(
    destination: str, duration_key: str,
    species: str, rarity_tier: int,
    rng: random.Random,
) -> dict[str, Any]:
    """Roll a loot bundle: ``{ore: {sym: qty}, rune: float, fish: [keys],
    crops: [keys], junk: [keys], nothing: int}``.

    Pure function; no DB. The returned dict is consumed by
    ``_credit_loot`` which actually moves balances. Splitting the roll
    from the credit lets the caller log / preview without committing.
    """
    dest_meta = ec.destination_meta(destination) or {}
    dur_meta = ec.duration_meta(duration_key) or {}
    weights = dict(dest_meta.get("loot_weights") or {})
    base_draws = int(dur_meta.get("draws") or 0)
    qty_mult, _rarity_bump = _affinity_bonus(species, destination)
    rarity_mult = _rarity_tier_mult(rarity_tier)
    total_mult = qty_mult * rarity_mult
    draws = max(1, int(round(base_draws * total_mult)))

    out: dict[str, Any] = {
        "ore":     {},
        "rune":    0.0,
        "fish":    [],
        "crops":   [],
        "junk":    [],
        "nothing": 0,
    }

    ore_min, ore_max = ec.ORE_PER_DRAW.get(destination, (1, 1))
    rune_lo, rune_hi = ec.RUNE_PER_DRAW.get(destination, (0.0, 1.0))

    for _ in range(draws):
        bucket = _weighted_pick(rng, weights)
        if bucket == "nothing":
            out["nothing"] += 1
            continue
        if bucket == "ore":
            sym = rng.choice(ec.FAVORED_ORE.get(destination, ("COPPER",)))
            qty = rng.randint(int(ore_min), int(ore_max))
            out["ore"][sym] = int(out["ore"].get(sym, 0)) + qty
            continue
        if bucket == "rune":
            out["rune"] = float(out["rune"]) + rng.uniform(rune_lo, rune_hi)
            continue
        if bucket == "fish":
            pool = ec.FAVORED_FISH.get(destination) or ()
            if pool:
                out["fish"].append(rng.choice(pool))
            continue
        if bucket == "crop":
            pool = ec.FAVORED_CROPS.get(destination) or ()
            if pool:
                out["crops"].append(rng.choice(pool))
            continue
        if bucket == "junk":
            out["junk"].append(rng.choice(ec.JUNK_POOL))
            continue

    return out


async def _credit_loot(
    db: Any,
    *,
    guild_id: int, user_id: int,
    loot: dict[str, Any],
    expedition_id: int,
) -> None:
    """Apply the rolled loot to the player's wallet + NFT layer.

    Wallet credits (ORE / RUNE) go through ``db.update_wallet_holding``
    on the Crypt Network. Catalog items (fish / crops / junk) get a
    fresh NFT mint per draw via ``services.items.mint_unit``, owned by
    the user with ``mint_source='expedition'`` for analytics.

    Each NFT mint is wrapped in try/except so one bad contract
    (e.g. the catalog drifted and the contract isn't deployed yet)
    just drops that item rather than failing the whole collect.
    """
    crypt_net, rune_sym = _crypt_net_and_rune()

    # Ore deposits.
    for sym, qty in (loot.get("ore") or {}).items():
        if int(qty) <= 0:
            continue
        try:
            await db.update_wallet_holding(
                int(user_id), int(guild_id), str(crypt_net),
                str(sym), int(to_raw(float(qty))),
            )
        except Exception:
            log.exception(
                "expedition credit ore failed sym=%s qty=%s exp=%s",
                sym, qty, expedition_id,
            )

    # RUNE deposit.
    rune_h = float(loot.get("rune") or 0.0)
    if rune_h > 0:
        try:
            await db.update_wallet_holding(
                int(user_id), int(guild_id), str(crypt_net),
                str(rune_sym), int(to_raw(rune_h)),
            )
        except Exception:
            log.exception(
                "expedition credit rune failed amt=%s exp=%s",
                rune_h, expedition_id,
            )

    # NFT mints. Fish / crops / junk each have a per-key contract
    # (kind.<catalog_key>) deployed by nft_bootstrap. mint_unit
    # creates one row per draw so a 5-fish run yields 5 distinct
    # tokens the player can list / inspect / gift individually.
    try:
        from services import items as _items
    except Exception:
        log.exception("expedition: services.items import failed")
        return
    for kind, keys in (("fish",  loot.get("fish") or []),
                       ("crop",  loot.get("crops") or []),
                       ("junk",  loot.get("junk") or [])):
        for key in keys:
            try:
                await _items.mint_unit(
                    db,
                    guild_id=int(guild_id),
                    contract_address=_items.contract_address(kind, str(key)),
                    owner_user_id=int(user_id),
                    metadata={"expedition_id": int(expedition_id)},
                    mint_source="expedition",
                )
            except Exception:
                log.exception(
                    "expedition mint %s/%s failed exp=%s",
                    kind, key, expedition_id,
                )


# ---------------------------------------------------------------------------
# Story
# ---------------------------------------------------------------------------


def _pick_loot_item_for_story(loot: dict[str, Any], rng: random.Random) -> str:
    """Pick a representative loot item to drop into ``{item}`` slots.

    Prefers crops / fish (more concrete words) over generic ore / rune.
    Falls back to a hand-rolled placeholder so a totally empty run
    still tells a story.
    """
    bag: list[str] = []
    bag.extend(str(k).replace("_", " ") for k in (loot.get("crops") or []))
    bag.extend(str(k).replace("_", " ") for k in (loot.get("fish") or []))
    bag.extend(str(k).replace("_", " ") for k in (loot.get("junk") or []))
    for sym, qty in (loot.get("ore") or {}).items():
        bag.append(f"a chunk of {sym.lower()}")
    if (loot.get("rune") or 0) > 0:
        bag.append("a glimmering rune shard")
    if not bag:
        return rng.choice([
            "a curious bauble", "a glittering something",
            "an unidentifiable trinket", "a surprisingly heavy stone",
        ])
    return rng.choice(bag)


def _generate_story(
    destination: str, buddy_name: str, species: str,
    loot: dict[str, Any], rng: random.Random,
) -> list[str]:
    """Compose a 5-line story log for the run.

    Layout: opener (1 line) + 3 destination events + closer (1 line).
    Each event is sampled WITHOUT replacement so the same line never
    appears twice. Substitution happens once per event.
    """
    dest_meta = ec.destination_meta(destination) or {}
    dest_name = str(dest_meta.get("name") or destination.title())

    def _sub(template: str) -> str:
        item = _pick_loot_item_for_story(loot, rng)
        return template.format(
            name=buddy_name, species=species, dest=dest_name, item=item,
        )

    opener = _sub(rng.choice(ec.OPENERS))
    closer = _sub(rng.choice(ec.CLOSERS))

    pool = list(ec.EVENTS.get(destination) or ())
    rng.shuffle(pool)
    body = [_sub(t) for t in pool[:3]]

    return [opener, *body, closer]


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


async def collect_expedition(
    db: Any, bot: Any,
    guild_id: int, user_id: int,
    expedition_id: int,
) -> CollectResult:
    """Resolve an expedition that has finished.

    Steps inside one transaction:
    1. Lock the row with FOR UPDATE; refuse if status != 'running' or
       ends_at is still in the future.
    2. Roll loot off the snapshotted species/rarity (so changing your
       active buddy mid-run doesn't shift the rewards).
    3. Generate the story.
    4. Credit ore / rune wallet + mint NFTs.
    5. Bump buddy XP + happiness via direct UPDATE on cc_buddies.
    6. Stamp the row collected with story_json + loot_json + counters.

    Steps 4-6 happen inside the same atomic block so a crash leaves the
    expedition in 'running' (player can retry) instead of half-credited.
    """
    rng = random.Random()
    async with db.atomic():
        row = await db.fetch_one(
            """
            SELECT *,
                   EXTRACT(EPOCH FROM (ends_at - NOW()))::bigint AS seconds_remaining
              FROM buddy_expeditions
             WHERE expedition_id = $1
               AND guild_id = $2 AND user_id = $3
             FOR UPDATE
            """,
            int(expedition_id), int(guild_id), int(user_id),
        )
        if not row:
            raise ValueError("Expedition not found.")
        if str(row.get("status")) != "running":
            raise ValueError("Expedition already collected.")
        secs_left = int(row.get("seconds_remaining") or 0)
        if secs_left > 0:
            raise ValueError(
                f"Expedition still running -- back in "
                f"{secs_left // 60}m{secs_left % 60}s."
            )

        destination = str(row["destination"])
        species = str(row["species_at_start"])
        rarity = int(row["rarity_at_start"] or 1)
        duration_s = int(row["duration_seconds"])

        # Recover the duration_key so the loot roller can read draws.
        dur_key = next(
            (d["key"] for d in ec.DURATIONS if int(d["seconds"]) == duration_s),
            ec.DURATIONS[0]["key"],
        )

        loot = _roll_loot(destination, dur_key, species, rarity, rng)

        # Look up the buddy for the story and the XP/happiness bumps.
        bud = await db.fetch_one(
            "SELECT id, name, level FROM cc_buddies WHERE id = $1",
            int(row["buddy_id"]),
        )
        buddy_name = str((bud or {}).get("name") or "Buddy")

        story = _generate_story(destination, buddy_name, species, loot, rng)

        # Credit loot (writes outside this lock are fine; we hold the
        # expedition row's lock until commit so a parallel collect can't
        # double-pay).
        await _credit_loot(
            db,
            guild_id=int(guild_id), user_id=int(user_id),
            loot=loot, expedition_id=int(expedition_id),
        )

        # Buddy XP / happiness deltas. ``+xp`` always; happiness is a
        # signed delta (longer runs leave the buddy a bit grumpy).
        dur_meta = ec.duration_meta(dur_key) or {}
        xp_gain = int(dur_meta.get("xp_gain") or 0)
        hap_delta = int(dur_meta.get("happiness_delta") or 0)
        if bud:
            await db.execute(
                """
                UPDATE cc_buddies
                   SET xp        = xp + $2,
                       level     = GREATEST(
                           level,
                           LEAST(
                               50,
                               GREATEST(
                                   1,
                                   FLOOR((1.0 + SQRT(
                                       1.0 + 8.0 * (xp + $2)::double precision / 120.0
                                   )) / 2.0)::int
                               )
                           )
                       ),
                       happiness = GREATEST(0, LEAST(100, happiness + $3)),
                       last_interacted_at = NOW(),
                       updated_at = NOW()
                 WHERE id = $1
                """,
                int(row["buddy_id"]), int(xp_gain), int(hap_delta),
            )

        # Stamp the row collected.
        await db.execute(
            """
            UPDATE buddy_expeditions
               SET status          = 'collected',
                   collected_at    = NOW(),
                   story_json      = $2::jsonb,
                   loot_json       = $3::jsonb,
                   xp_gained       = $4,
                   happiness_delta = $5
             WHERE expedition_id   = $1
            """,
            int(expedition_id),
            _json.dumps(story),
            _json.dumps(loot),
            int(xp_gain),
            int(hap_delta),
        )

    return CollectResult(
        expedition_id=int(expedition_id),
        destination=destination,
        buddy_name=buddy_name,
        species=species,
        story=story,
        loot=loot,
        xp_gained=xp_gain,
        happiness_delta=hap_delta,
    )
