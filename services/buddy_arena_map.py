"""services/buddy_arena_map.py -- Buddy Arena Map travel state machine.

Public surface (all async, all DB-touching):

    get_progress(db, gid, uid)            -> dict
    travel(db, gid, uid, zone_id)         -> TravelResult
    on_zone_cleared(db, gid, uid, zone_id, rounds_remaining)
                                          -> ClearResult
    start_tournament(db, gid, uid)        -> TournamentStart
    advance_tournament(db, gid, uid)      -> TournamentAdvance

Helpers (sync, pure):

    zone_for(zone_id)                     -> dict
    neighbors_of(zone_id)                 -> list[str]
    can_travel(progress, target_zone,
               active_buddy_level,
               mastery_skip)              -> tuple[bool, str]
    region_complete(progress, region)     -> bool
    tournament_ready(progress)            -> bool

The arena map sits on top of the existing battle engine
(services/buddy_battle.py) -- this module only manages the travel
cursor, region unlocks, and tournament bracket state. The cog calls
``start_zone_battle`` -> existing battle engine -> ``on_zone_cleared``.

Per CLAUDE.md: DB-side clocks for cooldown comparisons; raw monetary
columns use ``row.h()`` (not used here -- there are no monetary cols
on cc_buddy_map_progress); reuse buddies_config catalog without
re-deriving zone geometry.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from configs.buddies_config import (
    ARENA_REGIONS,
    ARENA_ZONES,
    TOURNAMENT_BRACKET,
    TRAVEL_COOLDOWN_S,
    ZONE_BATTLE_COOLDOWN_S,
)

log = logging.getLogger(__name__)


_TOURNAMENT_HUB_ZONE = "champion_hall"
_FIRST_REGION_KEY = "plains"


# Map battle rewards are paid in BUD + BBT, NOT DSD. Each zone carries
# a ``reward_usd`` curve marker in buddies_config (the historical
# hand-tuned progression scale: 250 at Plains Gate -> 25_000 at
# Champion Hall). These constants convert that marker into the BUD /
# BBT human amounts credited on clear, so the further a player has
# travelled the more they earn. Plains Gate first clear:
# ~2.5 BUD + ~15 BBT (a hair above the arena Lv 1 baseline);
# Tide Amphitheatre boss first clear: ~145 BUD + ~820 BBT.
ZONE_BUD_PER_CURVE: float = 0.010
ZONE_BBT_PER_CURVE: float = 0.060
ZONE_REPEAT_CLEAR_FRACTION: float = 0.25     # 25% on repeat clears
ZONE_BOSS_BUD_BONUS: float = 25.0            # flat BUD cherry on boss clears
ZONE_BOSS_BBT_BONUS: float = 100.0           # flat BBT cherry on boss clears


def zone_rewards_human(
    zone: dict, *, first_clear: bool,
) -> tuple[float, float]:
    """Return (bud_human, bbt_human) for clearing ``zone``.

    Boss zones get a flat bonus on top of the curve. Repeat clears pay
    ``ZONE_REPEAT_CLEAR_FRACTION`` of the first-clear amount (the boss
    bonus is also scaled down on repeats so a re-killed boss still
    rewards more than a re-killed non-boss).
    """
    curve = float(zone.get("reward_usd") or 0)
    bud = curve * ZONE_BUD_PER_CURVE
    bbt = curve * ZONE_BBT_PER_CURVE
    if zone.get("boss"):
        bud += ZONE_BOSS_BUD_BONUS
        bbt += ZONE_BOSS_BBT_BONUS
    if not first_clear:
        bud *= ZONE_REPEAT_CLEAR_FRACTION
        bbt *= ZONE_REPEAT_CLEAR_FRACTION
    return round(bud, 4), round(bbt, 4)


# ── Result dataclasses ─────────────────────────────────────────────────

@dataclass(slots=True)
class TravelResult:
    """Outcome of a ``travel`` call."""
    ok:           bool
    reason:       str
    new_zone_id:  str
    cooldown_s:   float = 0.0          # remaining cooldown if ok=False
    skipped:      int = 0              # zones skipped via mastery (combat.zone_travel)


@dataclass(slots=True)
class ClearResult:
    """Outcome of ``on_zone_cleared``.

    Rewards are in BUD + BBT, NOT DSD. The map metagame is part of the
    Buddy Network token loop -- every clear mints both Buddy Network
    tokens directly into the player's wallet. No DSD is ever credited
    on a zone clear.
    """
    zone_id:           str
    first_clear:       bool
    region_completed:  str | None      # region key if this clear flipped a region boss
    tournament_unlocked: bool
    item_drop:         str | None      # consumable key dropped, or None
    bud_reward_human:  float           # BUD credited to wallet on this clear
    bbt_reward_human:  float           # BBT credited to wallet on this clear
    bud_reward_raw:    int             # raw scaled, for receipt embeds
    bbt_reward_raw:    int             # raw scaled, for receipt embeds
    best_score:        int


@dataclass(slots=True)
class TournamentStart:
    ok:     bool
    reason: str
    round:  int


@dataclass(slots=True)
class TournamentAdvance:
    """Result of advancing one round of the tournament bracket."""
    round:           int                # round just resolved (1-4)
    final:           bool               # True if this was the championship match
    label:           str                # "Quarterfinal", "Semifinal", ...
    level_bonus:     int
    reward_usd:      int
    reward_item:     str
    champion:        bool               # True if the player just won the championship


# ── Sync helpers ───────────────────────────────────────────────────────

def zone_for(zone_id: str) -> dict:
    """Return the zone catalogue dict for ``zone_id``, or {} if unknown."""
    return ARENA_ZONES.get(str(zone_id or "").strip(), {})


def neighbors_of(zone_id: str) -> list[str]:
    """Return the directed neighbour list for ``zone_id``.

    Empty list when the zone is unknown or has no outgoing edges
    (e.g. the tournament hub).
    """
    return list(zone_for(zone_id).get("neighbors") or [])


def can_travel(
    progress: dict,
    target_zone: str,
    *,
    active_buddy_level: int,
    mastery_skip: int = 0,
) -> tuple[bool, str]:
    """Pure check: can the user travel from current zone to target?

    Returns (ok, reason). Reason is empty on success or a human-readable
    short reason on failure. Mastery_skip > 0 allows skipping one hop
    in the neighbours graph (combat.zone_travel passive).
    """
    cur = str(progress.get("current_zone_id") or "")
    tgt = str(target_zone or "")
    if not zone_for(tgt):
        return False, f"`{tgt}` is not a known zone."

    # Direct neighbour OR one-hop-away when mastery_skip is set.
    if tgt in neighbors_of(cur):
        pass
    elif mastery_skip > 0 and any(
        tgt in neighbors_of(hop) for hop in neighbors_of(cur)
    ):
        pass
    else:
        return False, "You can only travel to a neighbouring zone."

    z = zone_for(tgt)
    if z.get("hidden") and tgt not in (progress.get("cleared_zones") or []) and \
            tgt not in _unlocked_hidden_zones(progress):
        return False, "That zone is hidden -- find the unlock first."

    tier_min = int(z.get("tier_min") or 1)
    if int(active_buddy_level) < tier_min:
        return False, f"Your active buddy must be at least L{tier_min}."

    # Tournament hub gate: all 3 region bosses cleared
    if tgt == _TOURNAMENT_HUB_ZONE and not tournament_ready(progress):
        return False, "Clear the three region bosses first."

    return True, ""


def _unlocked_hidden_zones(progress: dict) -> set[str]:
    """Hidden zones the player has unlocked via passive triggers.

    For now: Moonlit Pool unlocks once Sharp Eye (luck.rare_catch) is
    bought -- the gate is checked by the caller via mastery passives.
    Ember Grove is auto-unlocked when the player clears any plains
    region zone (so they hear about the optional fork).
    """
    out: set[str] = set()
    cleared = set(progress.get("cleared_zones") or [])
    if cleared & {"plains_gate", "grassy_meadow", "windmill_lane"}:
        out.add("ember_grove")
    # moonlit_pool is gated by mastery -- callers should pass it through
    # explicitly when the player owns the luck.rare_catch node.
    return out


def region_complete(progress: dict, region: str) -> bool:
    """True when the player has cleared the boss zone of ``region``."""
    r = ARENA_REGIONS.get(str(region or ""))
    if not r:
        return False
    boss = r.get("boss_zone")
    return bool(boss and boss in (progress.get("cleared_zones") or []))


_TOURNAMENT_QUALIFYING_REGIONS: tuple[str, ...] = ("plains", "stone", "tide")


def tournament_ready(progress: dict) -> bool:
    """True when the 3 qualifying region bosses have been cleared.

    Forest + Volcano are post-launch expansion regions and not required
    for tournament entry -- they extend the long tail of the campaign
    instead of gating the championship bracket.
    """
    return all(
        region_complete(progress, k)
        for k in _TOURNAMENT_QUALIFYING_REGIONS
    )


def tournament_round_meta(round_idx: int) -> dict:
    """Return the bracket entry for ``round_idx`` (1-4). {} if out of range."""
    for entry in TOURNAMENT_BRACKET:
        if int(entry["round"]) == int(round_idx):
            return dict(entry)
    return {}


# ── DB-side state machine ──────────────────────────────────────────────

async def _ensure_progress(db, gid: int, uid: int) -> dict:
    """Return the cc_buddy_map_progress row, inserting defaults on first touch."""
    row = await db.fetch_one(
        "SELECT * FROM cc_buddy_map_progress "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if row:
        return dict(row)
    # Seed map_seed with a per-user RNG so route variation feels personal
    # but is reproducible for a given user across reads.
    seed = random.randint(1, 10**9)
    await db.execute(
        "INSERT INTO cc_buddy_map_progress "
        "(guild_id, user_id, map_seed) "
        "VALUES ($1, $2, $3) "
        "ON CONFLICT DO NOTHING",
        int(gid), int(uid), int(seed),
    )
    row = await db.fetch_one(
        "SELECT * FROM cc_buddy_map_progress "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    return dict(row) if row else {}


async def get_progress(db, gid: int, uid: int) -> dict:
    """Public read of progress; seeds defaults if missing."""
    return await _ensure_progress(db, int(gid), int(uid))


async def travel(
    db,
    gid: int,
    uid: int,
    target_zone: str,
    *,
    active_buddy_level: int,
    mastery_skip: int = 0,
    can_use_hidden: set[str] | None = None,
) -> TravelResult:
    """Move the cursor to ``target_zone`` if allowed.

    ``can_use_hidden`` is the set of hidden zone ids the player has
    unlocked via mastery (e.g. {"moonlit_pool"} when they own
    luck.rare_catch). The caller fills this from mastery_summary.
    """
    progress = await _ensure_progress(db, int(gid), int(uid))

    # Pre-flight: cooldown via DB clock
    cd_row = await db.fetch_one(
        "SELECT EXTRACT(EPOCH FROM (NOW() - last_travel_at)) AS dt "
        "FROM cc_buddy_map_progress "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    dt = float((cd_row or {}).get("dt") or 0.0)
    if dt and dt < TRAVEL_COOLDOWN_S:
        remaining = max(0.0, TRAVEL_COOLDOWN_S - dt)
        return TravelResult(
            ok=False, reason="Travel is on cooldown.",
            new_zone_id=str(progress.get("current_zone_id") or ""),
            cooldown_s=remaining,
        )

    # Splice the hidden-unlock set into the progress dict so can_travel
    # can see it without changing its signature
    progress_view = dict(progress)
    progress_view["_extra_hidden"] = list(can_use_hidden or set())

    ok, reason = can_travel(
        progress_view, target_zone,
        active_buddy_level=int(active_buddy_level),
        mastery_skip=int(mastery_skip),
    )
    # If hidden gate blocked it but the caller pre-cleared it, retry
    if not ok and "hidden" in reason and target_zone in (can_use_hidden or set()):
        ok, reason = True, ""
    if not ok:
        return TravelResult(
            ok=False, reason=reason,
            new_zone_id=str(progress.get("current_zone_id") or ""),
        )

    # Detect mastery skip use (target is 2 hops away)
    skipped = 0
    if target_zone not in neighbors_of(str(progress.get("current_zone_id") or "")):
        skipped = 1

    await db.execute(
        "UPDATE cc_buddy_map_progress "
        "SET current_zone_id = $3, last_travel_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), str(target_zone),
    )
    return TravelResult(
        ok=True, reason="", new_zone_id=str(target_zone), skipped=int(skipped),
    )


async def can_start_zone_battle(db, gid: int, uid: int) -> tuple[bool, float]:
    """Return (ok, remaining_cooldown_s). Cooldown is DB-side."""
    row = await db.fetch_one(
        "SELECT EXTRACT(EPOCH FROM (NOW() - last_zone_battle_at)) AS dt "
        "FROM cc_buddy_map_progress "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    dt = float((row or {}).get("dt") or 0.0)
    if dt and dt < ZONE_BATTLE_COOLDOWN_S:
        return False, max(0.0, ZONE_BATTLE_COOLDOWN_S - dt)
    return True, 0.0


async def mark_zone_battle(db, gid: int, uid: int) -> None:
    """Stamp last_zone_battle_at so the cooldown DB clock advances."""
    await db.execute(
        "UPDATE cc_buddy_map_progress "
        "SET last_zone_battle_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )


async def on_zone_cleared(
    db,
    gid: int,
    uid: int,
    zone_id: str,
    *,
    rounds_remaining: int,
    zone_drop_bonus: float = 0.0,
    is_boss_clear: bool = False,
) -> ClearResult:
    """Record a successful clear of ``zone_id``.

    - Inserts / updates cc_buddy_zone_trophies (best_score = max rounds_remaining).
    - Appends to cc_buddy_map_progress.cleared_zones once the zone has
      been cleared enough times to meet its ``clear_target`` (default 3
      for normal zones, 1 for boss zones). Boss zones also require
      ``is_boss_clear`` -- a wild-encounter win at a boss zone never
      advances region unlocks.
    - If the zone is a region boss, appends the next region to region_unlocks
      and flips tournament_state -> qualified when all three are done.
    - Rolls for an item drop (zone.item_drop) with mastery bonus.

    Returns a ClearResult describing the side effects so the cog can
    show a per-clear embed without a second DB round-trip.
    """
    progress = await _ensure_progress(db, int(gid), int(uid))
    z = zone_for(zone_id)
    cleared = set(progress.get("cleared_zones") or [])
    # Boss zones may only be "first cleared" by an explicit boss fight.
    # A wild encounter at a boss zone earns rewards + clear count but
    # never advances the region.
    is_boss_zone = bool(z.get("boss"))
    if is_boss_zone and not is_boss_clear:
        first_clear = False
    else:
        # Normal zones become "fully cleared" once clear_count reaches
        # the zone's clear_target (default 3). Until then every win
        # rolls drops and rewards, but neighbours stay locked beyond
        # the existing graph rules.
        clear_target = int(z.get("clear_target") or (1 if is_boss_zone else 3))
        existing = await db.fetch_val(
            "SELECT clear_count FROM cc_buddy_zone_trophies "
            "WHERE guild_id = $1 AND user_id = $2 AND zone_id = $3",
            int(gid), int(uid), str(zone_id),
        )
        prior_count = int(existing or 0)
        first_clear = (
            zone_id not in cleared
            and (prior_count + 1) >= clear_target
        )

    # Update trophy ledger
    await db.execute(
        "INSERT INTO cc_buddy_zone_trophies "
        "(guild_id, user_id, zone_id, best_score, clear_count) "
        "VALUES ($1, $2, $3, $4, 1) "
        "ON CONFLICT (guild_id, user_id, zone_id) DO UPDATE "
        "SET best_score = GREATEST(cc_buddy_zone_trophies.best_score, EXCLUDED.best_score), "
        "    clear_count = cc_buddy_zone_trophies.clear_count + 1, "
        "    cleared_at  = NOW()",
        int(gid), int(uid), str(zone_id), int(rounds_remaining),
    )

    region_completed: str | None = None
    tournament_unlocked = False
    if first_clear:
        cleared.add(zone_id)
        new_cleared = sorted(cleared)
        if z.get("boss"):
            region_completed = str(z.get("region") or "")
            # Append next-region unlock
            unlocks = list(progress.get("region_unlocks") or [])
            for r_key in ARENA_REGIONS.keys():
                if r_key not in unlocks:
                    unlocks.append(r_key)
                    break
            await db.execute(
                "UPDATE cc_buddy_map_progress "
                "SET cleared_zones = $3::text[], region_unlocks = $4::text[] "
                "WHERE guild_id = $1 AND user_id = $2",
                int(gid), int(uid), new_cleared, unlocks,
            )
        else:
            await db.execute(
                "UPDATE cc_buddy_map_progress "
                "SET cleared_zones = $3::text[] "
                "WHERE guild_id = $1 AND user_id = $2",
                int(gid), int(uid), new_cleared,
            )
        # Re-read so the tournament gate check sees the freshly-cleared zone
        progress = await _ensure_progress(db, int(gid), int(uid))
        if tournament_ready(progress) and progress.get("tournament_state") == "locked":
            await db.execute(
                "UPDATE cc_buddy_map_progress "
                "SET tournament_state = 'qualified' "
                "WHERE guild_id = $1 AND user_id = $2",
                int(gid), int(uid),
            )
            tournament_unlocked = True

    # Item drop roll (zone.item_drop is the candidate; only one item / clear)
    item_drop = _roll_item_drop(z, first_clear, float(zone_drop_bonus))
    if item_drop:
        await _grant_battle_item(db, int(gid), int(uid), item_drop, qty=1)

    # Credit BUD + BBT directly to the Buddy Network wallet. BUD goes
    # through the standard mint-impact path so heavy farming pulls the
    # oracle down (same shape as arena wins); BBT mints clean (no
    # oracle drop) because price discovery for BBT happens through
    # cashout / burn-for-bud, not arena/zone mints. Best-effort: a
    # mint failure logs and zeroes out that side of the reward.
    bud_h, bbt_h = zone_rewards_human(z, first_clear=bool(first_clear))
    bud_raw = 0
    bbt_raw = 0
    if bud_h > 0 or bbt_h > 0:
        from services import buddy_economy as _be
        if bud_h > 0:
            bud_raw, _ob, _oa = await _be.mint_bud_reward(
                db, int(gid), int(uid), float(bud_h),
                source=f"zone_{zone_id}{'_boss' if z.get('boss') else ''}",
            )
        if bbt_h > 0:
            bbt_raw = await _be.mint_bbt_reward(
                db, int(gid), int(uid), float(bbt_h),
                source=f"zone_{zone_id}{'_boss' if z.get('boss') else ''}",
            )

    return ClearResult(
        zone_id=str(zone_id),
        first_clear=bool(first_clear),
        region_completed=region_completed,
        tournament_unlocked=bool(tournament_unlocked),
        item_drop=item_drop,
        bud_reward_human=float(bud_h),
        bbt_reward_human=float(bbt_h),
        bud_reward_raw=int(bud_raw),
        bbt_reward_raw=int(bbt_raw),
        best_score=int(rounds_remaining),
    )


def _roll_item_drop(z: dict, first_clear: bool, bonus: float) -> str | None:
    """Decide whether the clear awards the zone's signature consumable.

    Probability: 100% on first clear, 30% on repeat clears, modulated by
    ``bonus`` (mastery luck.zone_drops adds to the chance).
    """
    candidate = str(z.get("item_drop") or "")
    if not candidate:
        return None
    p = 1.0 if first_clear else 0.30
    p = max(0.0, min(1.0, p + float(bonus or 0.0)))
    return candidate if random.random() < p else None


async def _grant_battle_item(
    db, gid: int, uid: int, item_key: str, *, qty: int,
) -> None:
    """Deposit ``qty`` of ``item_key`` into user_buddy_economy.battle_inventory.

    Reuses the JSONB column the crafting apply path writes to so the
    in-battle dropdown can read both crafted + drop-acquired items
    from one source of truth.
    """
    await db.execute(
        "INSERT INTO user_buddy_economy (guild_id, user_id) "
        "VALUES ($1, $2) ON CONFLICT DO NOTHING",
        int(gid), int(uid),
    )
    row = await db.fetch_one(
        "SELECT battle_inventory FROM user_buddy_economy "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    inv = dict(row.get("battle_inventory") or {}) if row else {}
    if not isinstance(inv, dict):
        inv = {}
    inv[item_key] = int(inv.get(item_key) or 0) + int(qty)
    import json as _json
    await db.execute(
        "UPDATE user_buddy_economy SET battle_inventory = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json.dumps(inv),
    )


# ── Tournament bracket ────────────────────────────────────────────────

async def start_tournament(db, gid: int, uid: int) -> TournamentStart:
    """Transition tournament_state qualified -> in_progress, round 1.

    Idempotent if already in_progress (returns the current round).
    """
    progress = await _ensure_progress(db, int(gid), int(uid))
    state = str(progress.get("tournament_state") or "locked")
    if state == "locked":
        return TournamentStart(
            ok=False,
            reason="Clear the three region bosses first.",
            round=0,
        )
    if state == "in_progress":
        return TournamentStart(
            ok=True, reason="Resuming bracket.",
            round=int(progress.get("tournament_round") or 1),
        )
    if state == "champion":
        # Allow re-entry; reset round to 1 for a victory lap
        await db.execute(
            "UPDATE cc_buddy_map_progress "
            "SET tournament_state = 'in_progress', tournament_round = 1 "
            "WHERE guild_id = $1 AND user_id = $2",
            int(gid), int(uid),
        )
        return TournamentStart(ok=True, reason="Champion's run.", round=1)
    # qualified -> in_progress
    await db.execute(
        "UPDATE cc_buddy_map_progress "
        "SET tournament_state = 'in_progress', tournament_round = 1 "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    await db.execute(
        "INSERT INTO cc_buddy_tournament_runs "
        "(guild_id, user_id, final_round, outcome) "
        "VALUES ($1, $2, 0, 'in_progress')",
        int(gid), int(uid),
    )
    return TournamentStart(ok=True, reason="", round=1)


async def advance_tournament(
    db, gid: int, uid: int, *, victory: bool,
) -> TournamentAdvance:
    """Resolve one round of the bracket.

    On victory: bump tournament_round; if that was round 4, flip state
    to ``champion`` and increment champion_count.
    On defeat: reset round to 0, state to ``qualified`` (re-runnable),
    and append an eliminated row to cc_buddy_tournament_runs.
    """
    progress = await _ensure_progress(db, int(gid), int(uid))
    cur_round = int(progress.get("tournament_round") or 1)
    meta = tournament_round_meta(cur_round)

    if not victory:
        await db.execute(
            "UPDATE cc_buddy_map_progress "
            "SET tournament_state = 'qualified', tournament_round = 0 "
            "WHERE guild_id = $1 AND user_id = $2",
            int(gid), int(uid),
        )
        await db.execute(
            "UPDATE cc_buddy_tournament_runs "
            "SET final_round = $3, outcome = 'eliminated', ended_at = NOW() "
            "WHERE id = ("
            "  SELECT id FROM cc_buddy_tournament_runs "
            "  WHERE guild_id = $1 AND user_id = $2 AND outcome = 'in_progress' "
            "  ORDER BY started_at DESC LIMIT 1"
            ")",
            int(gid), int(uid), int(cur_round),
        )
        return TournamentAdvance(
            round=cur_round, final=(cur_round >= 4),
            label=str(meta.get("label") or ""),
            level_bonus=int(meta.get("level_bonus") or 0),
            reward_usd=0, reward_item="",
            champion=False,
        )

    # Victory path
    final = cur_round >= 4
    if final:
        # Champion!
        await db.execute(
            "UPDATE cc_buddy_map_progress "
            "SET tournament_state = 'champion', tournament_round = 0, "
            "    champion_count = champion_count + 1 "
            "WHERE guild_id = $1 AND user_id = $2",
            int(gid), int(uid),
        )
        await db.execute(
            "UPDATE cc_buddy_tournament_runs "
            "SET final_round = 4, outcome = 'champion', ended_at = NOW() "
            "WHERE id = ("
            "  SELECT id FROM cc_buddy_tournament_runs "
            "  WHERE guild_id = $1 AND user_id = $2 AND outcome = 'in_progress' "
            "  ORDER BY started_at DESC LIMIT 1"
            ")",
            int(gid), int(uid),
        )
    else:
        await db.execute(
            "UPDATE cc_buddy_map_progress "
            "SET tournament_round = $3 "
            "WHERE guild_id = $1 AND user_id = $2",
            int(gid), int(uid), int(cur_round + 1),
        )

    return TournamentAdvance(
        round=cur_round, final=final,
        label=str(meta.get("label") or ""),
        level_bonus=int(meta.get("level_bonus") or 0),
        reward_usd=int(meta.get("reward_usd") or 0),
        reward_item=str(meta.get("reward_item") or ""),
        champion=bool(final),
    )


# ── Read helpers (cog-facing) ──────────────────────────────────────────

async def list_unlocked_zones(db, gid: int, uid: int) -> list[str]:
    """Zone ids the player has either cleared or has neighbour access to."""
    progress = await _ensure_progress(db, int(gid), int(uid))
    out: set[str] = set(progress.get("cleared_zones") or [])
    cur = str(progress.get("current_zone_id") or "")
    out.add(cur)
    for n in neighbors_of(cur):
        out.add(n)
    return sorted(out)


async def battle_inventory(db, gid: int, uid: int) -> dict[str, int]:
    """Read user_buddy_economy.battle_inventory as a plain dict.

    Used by the in-battle dropdown to populate options.
    """
    row = await db.fetch_one(
        "SELECT battle_inventory FROM user_buddy_economy "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        return {}
    raw = row.get("battle_inventory") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v or 0) for k, v in raw.items() if int(v or 0) > 0}


async def consume_battle_item(
    db, gid: int, uid: int, item_key: str, *, qty: int = 1,
) -> bool:
    """Decrement one or more uses of ``item_key`` from the inventory.

    Returns False if the user does not have enough (without mutating).
    """
    import json as _json
    row = await db.fetch_one(
        "SELECT battle_inventory FROM user_buddy_economy "
        "WHERE guild_id = $1 AND user_id = $2 FOR UPDATE",
        int(gid), int(uid),
    )
    inv = dict(row.get("battle_inventory") or {}) if row else {}
    if not isinstance(inv, dict):
        inv = {}
    have = int(inv.get(item_key) or 0)
    if have < int(qty):
        return False
    new = have - int(qty)
    if new > 0:
        inv[item_key] = new
    else:
        inv.pop(item_key, None)
    await db.execute(
        "UPDATE user_buddy_economy SET battle_inventory = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json.dumps(inv),
    )
    return True
