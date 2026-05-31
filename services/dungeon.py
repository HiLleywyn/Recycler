"""
services/dungeon.py  -  Delve crawler state + economy.

The cog (cogs/dungeon.py) drives the user-facing flow (animations,
buttons, embeds). Everything that touches the DB or the loot model
lives here so the cog stays presentation-only.

Public API (chunked across this module):
    ensure_state(db, gid, uid)                         -> dict
    list_state(db, gid, uid)                           -> dict
    set_class(db, gid, uid, class_key)                 -> dict
    start_run(db, gid, uid)                            -> RunResult
    end_run(db, gid, uid, outcome)                     -> dict
    advance_room(db, gid, uid)                         -> RoomEvent
    descend(db, gid, uid)                              -> RoomEvent
    resolve_attack(db, gid, uid, mode='attack')        -> CombatResult
    resolve_flee(db, gid, uid)                         -> CombatResult
    attempt_capture(db, gid, uid, charm=False)         -> CaptureResult
    use_consumable(db, gid, uid, key)                  -> ConsumableResult
    mine_ore(db, gid, uid)                             -> MineResult
    buy_item(db, gid, uid, kind, key)                  -> BuyResult
    equip_item(db, gid, uid, kind, key)                -> dict
    list_party(db, gid, uid)                           -> list[dict]
    set_active_buddy(db, gid, uid, party_id)           -> dict | None
    release_buddy(db, gid, uid, party_id)              -> bool
    burn_ore_for_rune(db, gid, uid, ore_sym, amt_raw)  -> BurnResult
    stake_ore(db, gid, uid, ore_sym, amt_raw)          -> StakeResult
    unstake_ore(db, gid, uid, ore_sym, amt_raw)        -> StakeResult
    claim_stake_yield(db, gid, uid)                    -> StakeResult
    accrued_stake_yield(db, gid, uid)                  -> int
    cashout_rune(db, gid, uid, amt_raw)                -> CashoutResult
    get_top_delvers(db, gid, limit)                    -> list[dict]

Conventions:
    -- Monetary deltas are passed to the DB as raw-scaled NUMERIC(36,0)
       via core.framework.scale.to_raw / to_human.
    -- Every public function returns plain dicts so the cog never
       imports asyncpg.Record.
    -- Time comparisons happen DB-side via EXTRACT(EPOCH ...).
    -- Burns / mints update crypto_prices via the same helpers .buy /
       .sell / fishing burns use, so the chart agrees with everyone.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import random
import time as _time
from dataclasses import dataclass, field
from typing import Any

from core.framework.scale import to_human, to_raw

import configs.dungeon_config as dc

log = logging.getLogger(__name__)


# Mirror Config.PRICE_IMPACT_MAX so dungeon ore moves cap at the same
# magnitude .buy / .sell / fishing burns cap at. Loaded lazily so a test
# patching Config.PRICE_IMPACT_MAX picks up at call time.
def _price_impact_max() -> float:
    from core.config import Config
    return float(getattr(Config, "PRICE_IMPACT_MAX", 0.40))


# ============================================================================
# JSONB normalizers (asyncpg returns raw JSON strings here)
# ============================================================================

_JSONB_DICT_COLS: tuple[str, ...] = (
    "consumables", "weapons_owned", "armor_owned",
    "current_mob_state", "current_room_payload",
    "player_buffs",
    "relics_owned",
    "junk_inventory",
)


def _as_dict(value: Any) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _normalize_state(row: Any) -> dict:
    if row is None:
        return {}
    out = dict(row)
    for col in _JSONB_DICT_COLS:
        if col in out:
            out[col] = _as_dict(out.get(col))
    return out


def _json(payload: Any) -> str:
    """Serialise a payload for a JSONB DB write."""
    return json.dumps(payload or {}, separators=(",", ":"))


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class RunResult:
    run_id: int
    floor: int
    room: int
    state: dict


@dataclass
class RoomEvent:
    """Outcome of advance_room / descend."""
    room_type: str           # 'mob' | 'ore' | 'shrine' | 'stairs' | 'chest' | 'wild_battle' | 'empty' | 'boss'
    floor: int
    room: int
    payload: dict = field(default_factory=dict)
    mob_state: dict | None = None
    descended: bool = False
    cleared_floor: bool = False
    # Set when ``room_type == 'wild_battle'``: the synthesised wild buddy
    # opponent. Cog passes this into services.buddy_battle.Fighter.from_row
    # to run the interactive battle, then calls back into resolve_wild_battle
    # with the engine result.
    wild_buddy: dict | None = None


@dataclass
class WildBattleResolution:
    """Outcome of resolve_wild_battle."""
    won: bool
    captured: bool
    rune_reward_raw: int                  # RUNE minted on win (0 on loss)
    ore_symbol: str | None                # COPPER / SILVER / GOLD on win, None on loss
    ore_reward_raw: int                   # ore minted on win (0 on loss)
    captured_buddy_row: dict | None       # cc_buddies row when captured
    new_won_total: int                    # lifetime wild_battles_won AFTER this row
    new_lost_total: int                   # lifetime wild_battles_lost AFTER this row
    new_captured_total: int               # lifetime wild_buddies_captured AFTER this row
    buddy_xp_awarded: int = 0             # XP added to the active buddy on win
    fighter_buddy_id: int | None = None   # cc_buddies.id of the buddy that fought


@dataclass
class CombatResult:
    outcome: str             # 'continue' | 'mob_dead' | 'player_dead' | 'fled' | 'failed_flee'
    log: list[str] = field(default_factory=list)
    mob_state: dict | None = None
    player_hp: int = 0
    player_max_hp: int = 0
    mob_xp: int = 0
    rune_drop_human: float = 0.0
    ore_drop_symbol: str | None = None
    ore_drop_qty_human: float = 0.0
    leveled_up: bool = False
    new_level: int = 1
    boss_kill: bool = False
    mini_boss_kill: bool = False
    captured: bool = False  # set by capture path
    party_id: int | None = None
    junk_drop_key: str | None = None  # secondary salvage / mat / usable drop
    # Boss / mini-boss loot table drop (separate from junk_drop_key).
    # awarded_kind is one of "weapon" / "armor" / "junk" -- "junk"
    # appears when the rolled gear was already owned and we fell back.
    loot_drop_key: str | None = None
    loot_drop_kind: str | None = None


@dataclass
class CaptureResult:
    success: bool
    chance: float
    party_id: int | None = None
    mob_key: str | None = None
    log: list[str] = field(default_factory=list)


@dataclass
class ConsumableResult:
    key: str
    kind: str                # 'heal' | 'escape' | 'charm' | 'mine_boost' | 'lure'
    consumed: bool
    detail: str = ""
    player_hp: int = 0
    player_max_hp: int = 0


@dataclass
class MineResult:
    ore_symbol: str
    qty_human: float
    qty_raw: int
    oracle_before: float
    oracle_after: float
    impact_pct: float
    junk_drop_key: str | None = None


@dataclass
class ChestResult:
    rune_amount:  float
    relic_key:    str | None = None
    junk_drop_key: str | None = None
    # Shrine-debt buff multiplier consumed on this open (e.g. 2.0 means
    # the chest payout was doubled). 0.0 / None when no debt was active
    # so the receipt path can decide whether to show a "Shrine debt
    # paid off!" line.
    shrine_debt_mult: float = 0.0


@dataclass
class ShrineResult:
    outcome_key:   str
    boon_name:     str
    blurb:         str
    hp_delta:      int = 0           # final HP - starting HP (positive heal, negative cost)
    rune_credited: float = 0.0
    buff_key:      str | None = None
    buff_value:    float = 0.0
    buff_duration: int = 0
    relic_key:     str | None = None


@dataclass
class RelicEquipResult:
    equipped_key:  str | None
    previous_key:  str | None


@dataclass
class CurseSetResult:
    curse_key:     str | None
    previous_key:  str | None


@dataclass
class BuyResult:
    kind: str               # 'weapon' | 'armor' | 'consumable'
    key: str
    price_rune_human: float
    impact_pct: float
    oracle_before: float
    oracle_after: float


@dataclass
class BurnResult:
    """ORE -> RUNE burn receipt."""
    ore_symbol: str
    ore_burned_raw: int
    rune_minted_raw: int
    ore_oracle_before: float
    ore_oracle_after: float
    rune_oracle_before: float
    rune_oracle_after: float
    price_impact_pct: float
    lp_reward_usd: float = 0.0


@dataclass
class StakeResult:
    ore_symbol: str
    staked_raw: int
    delta_raw: int
    rune_yield_paid_raw: int
    pending_rune_raw: int


@dataclass
class CashoutResult:
    rune_burned_raw: int
    usd_credited_raw: int
    rune_oracle_before: float
    rune_oracle_after: float
    price_impact_pct: float
    revenue_usd: float = 0.0
    lp_reward_usd: float = 0.0


# ============================================================================
# State helpers
# ============================================================================

async def ensure_state(db: Any, guild_id: int, user_id: int) -> dict:
    """Insert a default user_dungeon row on first touch and return it."""
    await db.execute(
        """
        INSERT INTO user_dungeon (guild_id, user_id, last_action_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (guild_id, user_id) DO NOTHING
        """,
        guild_id, user_id,
    )
    row = await db.fetch_one(
        "SELECT * FROM user_dungeon WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    return _normalize_state(row)


async def list_state(db: Any, guild_id: int, user_id: int) -> dict:
    """Return a dict of the user's dungeon row, or {} if no row yet."""
    row = await db.fetch_one(
        "SELECT * FROM user_dungeon WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    return _normalize_state(row)


async def set_class(db: Any, guild_id: int, user_id: int, class_key: str) -> dict:
    """Set the player's class for the first time.

    Subsequent class changes go through ``reroll_class`` which charges a
    USD fee + tracks the cooldown. ``set_class`` snaps the player's
    starter weapon/armor (per ``CLASSES[class_key].starter_weapon`` /
    ``starter_armor``) into both their ``equipped_*`` columns and their
    owned-inventory JSONB so the starter gear is actually equippable
    after the migration adds new types (an Archer who has never owned a
    bow needs ``short_bow`` minted into ``weapons_owned`` immediately).
    """
    meta = dc.class_meta(class_key)
    if not meta:
        raise ValueError(f"Unknown class: {class_key!r}")
    state = await ensure_state(db, guild_id, user_id)
    if state.get("class_key"):
        raise ValueError(
            f"You are already a {state['class_key']}. "
            f"Use `,delve reroll {class_key}` to change classes."
        )
    hp_max = max(1, int(round(dc.STARTING_HP * float(meta["hp_mult"]))))
    starter_w = dc.starter_weapon_for_class(class_key)
    starter_a = dc.starter_armor_for_class(class_key)

    weapons = dict(state.get("weapons_owned") or {})
    armors  = dict(state.get("armor_owned")   or {})
    weapons.setdefault(starter_w, 1)
    armors.setdefault(starter_a, 1)

    await db.execute(
        """
        UPDATE user_dungeon
           SET class_key       = $3,
               hp_max          = $4,
               current_hp      = $4,
               equipped_weapon = $5,
               equipped_armor  = $6,
               weapons_owned   = $7::jsonb,
               armor_owned     = $8::jsonb,
               last_action_at  = NOW(),
               updated_at      = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, class_key, hp_max,
        starter_w, starter_a, _json(weapons), _json(armors),
    )
    return await list_state(db, guild_id, user_id)


def player_combat_stats(state: dict) -> dict:
    """Compute (atk, def, spd, int, hp_max) from class + level + gear + stat allocs.

    Stat allocations (`hp_alloc / atk_alloc / spd_alloc / int_alloc`) are
    folded in here so the value the panel shows ALWAYS matches what the
    combat engine uses next swing. SPD is hard-capped at 0.95 so a maxed
    Vigor build can never trigger guaranteed first-strike via overflow.

    Returned dict also carries class skill metadata + the equipped
    weapon's attack_kind so resolve_attack can branch ranged-vs-melee
    without re-reading the weapon catalog.
    """
    class_key = state.get("class_key") or "warrior"
    cmeta = dc.class_meta(class_key) or dc.CLASSES["warrior"]
    level = max(1, int(state.get("level") or 1))

    weapon = dc.weapon_meta(state.get("equipped_weapon") or "rusty_dagger") or {}
    armor  = dc.armor_meta(state.get("equipped_armor")   or "cloth_tunic")    or {}

    hp_alloc  = max(0, int(state.get("hp_alloc")  or 0))
    atk_alloc = max(0, int(state.get("atk_alloc") or 0))
    spd_alloc = max(0, int(state.get("spd_alloc") or 0))
    int_alloc = max(0, int(state.get("int_alloc") or 0))

    # Rarity-scaled (and BASE_STAT_FACTOR-weakened) gear bonuses replace
    # the raw catalog atk_bonus / def_bonus reads. Single source of truth
    # via dc.effective_*_bonus so the bag panel and combat engine never
    # disagree about what the gear is actually worth.
    weapon_atk = dc.effective_atk_bonus(weapon)
    armor_def  = dc.effective_def_bonus(armor)
    weapon_affixes = dc.item_affixes(weapon)
    armor_affixes  = dc.item_affixes(armor)

    atk = (
        float(cmeta["atk_base"]) + level * 0.6
        + float(weapon_atk)
        + atk_alloc * dc.STAT_POINT_ATK_BONUS
    )
    df  = float(cmeta["def_base"]) + level * 0.3 + float(armor_def)
    spd = (
        float(cmeta["spd_base"]) + level * 0.005
        + spd_alloc * dc.STAT_POINT_SPD_BONUS
    )
    # Crit affix on weapon -> additive crit chance bump applied below in
    # resolve_attack via attacker_spd. Surfaced here too for the panel.
    crit_affix = float(weapon_affixes.get("crit_pct") or 0.0)
    int_stat = (
        float(cmeta.get("int_base", 0)) + level * 0.3
        + int_alloc * dc.STAT_POINT_INT_BONUS
    )

    # HP affix (typically armor.affixes.hp_pct) inflates hp_max.
    hp_affix_pct = float(armor_affixes.get("hp_pct") or 0.0) + float(weapon_affixes.get("hp_pct") or 0.0)
    hp_max = max(1, int(round(
        dc.STARTING_HP * float(cmeta["hp_mult"])
        + level * dc.HP_PER_LEVEL
        + hp_alloc * dc.STAT_POINT_HP_BONUS
    )))
    if hp_affix_pct > 0:
        hp_max = max(1, int(round(hp_max * (1.0 + hp_affix_pct))))
    # Equipped relic (passive, persistent across runs). Multipliers fold in
    # here so the panel and the combat engine see one truth. Effects that
    # don't apply to a stat (lifesteal, mine_yield, rune_drop) are read at
    # their own call sites via dc.relic_effect().
    relic_key = state.get("equipped_relic") or None
    hp_max = max(1, int(round(hp_max * dc.relic_effect(relic_key, "hp_max_mult", 1.0))))
    spd = spd + dc.relic_effect(relic_key, "spd_bonus", 0.0)
    return {
        "atk": atk, "def": df, "spd": min(0.95, spd),
        "int": int_stat,
        "hp_max": hp_max, "level": level,
        "class_key": class_key, "skill_key": cmeta["skill_key"],
        "skill_name": cmeta["skill_name"], "skill_mult": float(cmeta["skill_mult"]),
        "skill_auto_crit": bool(cmeta["skill_auto_crit"]),
        "skill_cd": int(cmeta["skill_cd"]),
        "skill_kind": str(cmeta.get("skill_kind") or "melee"),
        "weapon_key": str(state.get("equipped_weapon") or "rusty_dagger"),
        "weapon_type": str(weapon.get("weapon_type") or ""),
        "attack_kind": str(weapon.get("attack_kind") or "melee"),
        "weapon_ammo_key": weapon.get("ammo_key") or None,
        "hp_alloc": hp_alloc, "atk_alloc": atk_alloc,
        "spd_alloc": spd_alloc, "int_alloc": int_alloc,
        "relic_key": relic_key,
        # Affix bundle for resolve_attack to read per-swing.
        "weapon_rarity": dc.item_rarity(weapon),
        "armor_rarity":  dc.item_rarity(armor),
        "weapon_affixes": weapon_affixes,
        "armor_affixes":  armor_affixes,
        "crit_affix": crit_affix,
    }


# ============================================================================
# Run lifecycle
# ============================================================================

async def start_run(db: Any, guild_id: int, user_id: int) -> RunResult:
    """Start a new dungeon run. Refuses if a run is already active or HP is 0."""
    state = await ensure_state(db, guild_id, user_id)
    if not state.get("class_key"):
        raise ValueError("Pick a class first with `,delve class warrior|mage|rogue`.")
    if state.get("run_id"):
        raise ValueError("You're already mid-delve. Use `,delve` to view your room.")
    if int(state.get("current_hp") or 0) <= 0:
        raise ValueError("You're at 0 HP. Rest at the surface first (`,delve rest`).")

    row = await db.fetch_one(
        """
        INSERT INTO dungeon_runs (guild_id, user_id, class_key)
        VALUES ($1, $2, $3)
        RETURNING run_id
        """,
        guild_id, user_id, state["class_key"],
    )
    run_id = int(row["run_id"])

    await db.execute(
        """
        UPDATE user_dungeon
           SET run_id              = $3,
               current_floor       = 1,
               current_room        = 0,
               current_room_type   = NULL,
               current_mob_state   = NULL,
               current_room_payload = NULL,
               total_runs          = total_runs + 1,
               last_run_started_at = NOW(),
               last_action_at      = NOW(),
               updated_at          = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, run_id,
    )
    refreshed = await list_state(db, guild_id, user_id)
    return RunResult(run_id=run_id, floor=1, room=0, state=refreshed)


async def end_run(
    db: Any, guild_id: int, user_id: int, outcome: str,
) -> dict:
    """Close out the active run. Idempotent (no-op when no active run)."""
    if outcome not in ("cleared", "died", "fled", "rest"):
        raise ValueError(f"bad outcome: {outcome!r}")
    state = await list_state(db, guild_id, user_id)
    run_id = int(state.get("run_id") or 0)
    if not run_id:
        return state
    deepest = max(int(state.get("deepest_floor") or 0), int(state.get("current_floor") or 0))
    await db.execute(
        """
        UPDATE dungeon_runs
           SET ended_at      = NOW(),
               outcome       = $3,
               deepest_floor = GREATEST(deepest_floor, $4)
         WHERE run_id = $1 AND guild_id = $2 AND ended_at IS NULL
        """,
        run_id, guild_id, outcome, deepest,
    )
    new_hp = int(state.get("current_hp") or 0)
    if outcome in ("rest", "died"):
        new_hp = int(state.get("hp_max") or dc.STARTING_HP)
    # Cursed-run accounting: bump the lifetime counter when a cursed run
    # ends in 'rest' (the player walked away alive). Death just clears
    # the curse with no credit. Always clear the curse so the next run
    # starts fresh -- curses are explicitly opt-in per run.
    had_curse = bool(state.get("run_curse"))
    bump_curses = 1 if (had_curse and outcome == "rest") else 0
    await db.execute(
        """
        UPDATE user_dungeon
           SET run_id              = NULL,
               current_floor       = 0,
               current_room        = 0,
               current_room_type   = NULL,
               current_mob_state   = NULL,
               current_room_payload = NULL,
               current_hp          = $3,
               deepest_floor       = GREATEST(deepest_floor, $4),
               last_run_ended_at   = NOW(),
               last_action_at      = NOW(),
               skill_cd_remaining  = 0,
               run_curse           = NULL,
               total_curses_completed = total_curses_completed + $5,
               updated_at          = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, new_hp, deepest, int(bump_curses),
    )
    return await list_state(db, guild_id, user_id)


# ============================================================================
# Room generation
# ============================================================================

# Non-boss room weights. Tuned so combat dominates ("battle heavy"):
_ROOM_WEIGHTS: dict[str, int] = {
    "mob":         55,
    "ore":         22,
    "shrine":       6,
    "chest":        5,
    "wild_battle":  0,   # spawn handled out-of-band; see advance_room
    "empty":       12,
}


def _roll_room_type(rng: random.Random, *, force_stairs: bool, force_boss: bool) -> str:
    if force_boss:
        return "boss"
    if force_stairs:
        return "stairs"
    keys = list(_ROOM_WEIGHTS.keys())
    weights = [_ROOM_WEIGHTS[k] for k in keys]
    total = sum(weights)
    r = rng.random() * total
    acc = 0
    for k, w in zip(keys, weights):
        acc += w
        if r <= acc:
            return k
    return keys[-1]


def _build_mob_state(
    mob_key: str, depth: int,
    *,
    boss: bool = False,
    mini_boss: bool = False,
    curse_key: str | None = None,
) -> dict:
    """Construct a fresh mob_state dict scaled by floor depth.

    ``curse_key`` is the active run curse (if any). Curse multipliers fold
    into HP / ATK at spawn time so combat math never has to special-case
    cursed mobs at the swing site. ``mini_boss=True`` looks the mob up in
    MINI_BOSSES instead of MOBS so the mini-boss key space is fully
    distinct from regular mobs.
    """
    if mini_boss:
        meta = dc.mini_boss_meta(mob_key) or {}
    else:
        meta = dc.mob_meta(mob_key) or {}
    depth_scale = 1.0 + 0.07 * max(0, depth - 1)
    hp_mult  = dc.curse_mult(curse_key, "mob_hp_mult", 1.0)
    dmg_mult = dc.curse_mult(curse_key, "mob_dmg_mult", 1.0)
    hp = max(1, int(round(float(meta.get("hp_base", 10)) * depth_scale * hp_mult)))
    atk = float(meta.get("atk_base", 1)) * depth_scale * dmg_mult
    df  = float(meta.get("def_base", 0)) * depth_scale
    return {
        "key": mob_key,
        "hp": hp,
        "max_hp": hp,
        "atk": atk,
        "def": df,
        "spd": float(meta.get("spd_base", 0.5)),
        "tier": int(meta.get("tier", 1)),
        "boss": bool(boss),
        "mini_boss": bool(mini_boss),
        "stunned_turns": 0,
        "curse_key": curse_key,
    }


async def advance_room(db: Any, guild_id: int, user_id: int) -> RoomEvent:
    """Move to the next room of the current floor.

    Refuses if the player is mid-combat or has no active run. The very
    last room of a non-boss floor forces a stairs roll; boss floors
    force a boss room as the last one.
    """
    state = await list_state(db, guild_id, user_id)
    if not state.get("run_id"):
        raise ValueError("No active run. Use `,delve start`.")
    if int(state.get("current_hp") or 0) <= 0:
        raise ValueError("You're at 0 HP. End the run with `,delve rest`.")
    if state.get("current_mob_state"):
        raise ValueError("You're mid-combat. Finish the fight first.")

    floor = int(state.get("current_floor") or 1)
    room = int(state.get("current_room") or 0) + 1
    fmeta = dc.floor_meta(floor)
    rooms_total = int(fmeta.get("rooms") or 5)
    is_boss_floor = bool(fmeta.get("boss"))
    rng = random.Random()

    # Boss-floor handling. The boss is always the final room
    # (room == rooms_total). Once the player advances PAST that final
    # room (room > rooms_total) the boss has been defeated, so force
    # stairs to send them to the next floor instead of looping the
    # boss spawn forever. resolve_combat also flips current_room_type
    # to 'stairs' on a boss kill so the player usually never reaches
    # this branch -- it's a safety net for in-progress runs that
    # predate that fix.
    force_stairs = (
        ((not is_boss_floor) and (room >= rooms_total))
        or (is_boss_floor and room > rooms_total)
    )
    force_boss   = is_boss_floor and (room == rooms_total)
    rt = _roll_room_type(rng, force_stairs=force_stairs, force_boss=force_boss)

    # Wild-buddy overlay. Boss/stairs always take priority -- a wild buddy
    # on the boss room would block progression -- but for any other rolled
    # type (mob/ore/shrine/chest/empty) we re-roll against
    # ``wild_battle_chance(floor)``. Mirrors the way fishing's wild battle
    # overrides a normal cast outcome on a separate dice roll.
    payload: dict = {}
    mob_state: dict | None = None
    wild_buddy: dict | None = None
    # Active attractor doubles the per-room wild-battle spawn chance.
    # The flag is stamped onto the wild_buddy payload below so the cog
    # can render a magnet badge on the encounter prompt.
    base_wild_chance = dc.wild_battle_chance(floor)
    eff_wild_chance = base_wild_chance
    attractor_on = False
    try:
        from services.buddy_economy import attractor_active as _att
        if await _att(db, int(guild_id), int(user_id)):
            attractor_on = True
            eff_wild_chance = min(1.0, base_wild_chance * 2.0)
    except Exception:
        log.debug("delve attractor probe failed", exc_info=True)
    if rt not in ("boss", "stairs") and rng.random() < eff_wild_chance:
        rt = "wild_battle"

    curse_key = state.get("run_curse") or None
    if rt == "mob":
        # Mini-boss roll: a fraction of mob rooms upgrade to a named
        # mini-boss with stronger stats and a guaranteed loot-table drop.
        # Skips boss floors (the boss room slot is reserved for the
        # main boss) and stays inside the MINI_BOSS_MIN/MAX_FLOOR window.
        if dc.should_spawn_mini_boss(floor, is_boss_floor, rng):
            mb_key = dc.pick_mini_boss_for_floor(floor, rng)
            if mb_key:
                mob_state = _build_mob_state(
                    mb_key, floor,
                    mini_boss=True, curse_key=curse_key,
                )
        if mob_state is None:
            mob_key = dc.pick_mob_for_floor(floor, rng) or "goblin"
            mob_state = _build_mob_state(mob_key, floor, curse_key=curse_key)
    elif rt == "boss":
        boss_key = str(fmeta.get("boss") or "ogre_lord")
        mob_state = _build_mob_state(boss_key, floor, boss=True, curse_key=curse_key)
    elif rt == "ore":
        ore_sym = dc.pick_ore_for_floor(floor, rng) or dc.COPPER_SYMBOL
        qty = dc.mine_qty_roll(floor, ore_sym, rng)
        payload = {"ore_symbol": ore_sym, "ore_qty": qty}
    elif rt == "chest":
        # Chests give a small RUNE roll on use. Resolved client-side via
        # ,delve open which reads payload.rune_amount.
        payload = {"rune_amount": round(rng.uniform(1.0, 8.0) * (1 + 0.10 * floor), 2)}
    elif rt == "wild_battle":
        # Stash the synth opponent in the room payload. The cog reads it
        # back when the player engages and feeds it into Fighter.from_row.
        # Storing it on current_room_payload (rather than current_mob_state)
        # keeps the regular combat resolver from accidentally swinging at it.
        wild_buddy = dc.roll_wild_battle(floor)
        if attractor_on:
            wild_buddy = {**wild_buddy, "attractor_pulled": True}
        payload = {"wild_buddy": wild_buddy}

    await db.execute(
        """
        UPDATE user_dungeon
           SET current_room        = $3,
               current_room_type   = $4,
               current_mob_state   = $5::jsonb,
               current_room_payload = $6::jsonb,
               last_action_at      = NOW(),
               updated_at          = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, room, rt,
        _json(mob_state) if mob_state else None,
        _json(payload) if payload else None,
    )
    return RoomEvent(
        room_type=rt, floor=floor, room=room,
        payload=payload, mob_state=mob_state,
        wild_buddy=wild_buddy,
    )


async def descend(db: Any, guild_id: int, user_id: int) -> RoomEvent:
    """Take the stairs to the next floor. Requires current_room_type == 'stairs'."""
    state = await list_state(db, guild_id, user_id)
    if state.get("current_room_type") != "stairs":
        raise ValueError("No stairs in this room.")
    floor = int(state.get("current_floor") or 1)
    if floor >= dc.MAX_FLOOR:
        raise ValueError("You've reached the deepest floor.")
    new_floor = floor + 1
    await db.execute(
        """
        UPDATE user_dungeon
           SET current_floor       = $3,
               current_room        = 0,
               current_room_type   = NULL,
               current_mob_state   = NULL,
               current_room_payload = NULL,
               deepest_floor       = GREATEST(deepest_floor, $3),
               last_action_at      = NOW(),
               updated_at          = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, new_floor,
    )
    await db.execute(
        """
        UPDATE dungeon_runs
           SET floors_cleared = floors_cleared + 1,
               deepest_floor  = GREATEST(deepest_floor, $3)
         WHERE run_id = $1 AND guild_id = $2
        """,
        int(state.get("run_id") or 0), guild_id, new_floor,
    )
    return RoomEvent(
        room_type="empty", floor=new_floor, room=0,
        descended=True, cleared_floor=True,
    )


# ============================================================================
# Combat helpers
# ============================================================================

def _crit_chance(spd: float) -> float:
    return dc.CRIT_BASE + dc.CRIT_SPD_SCALE * float(spd)


def _swing_damage(
    atk: float, df: float, *, mult: float, force_crit: bool, attacker_spd: float,
    rng: random.Random,
) -> tuple[int, bool]:
    """Standard ATK vs DEF swing. Returns (damage, is_crit)."""
    base = max(1.0, float(atk) * float(mult)) - float(df) * 0.5
    base = max(1.0, base) * rng.uniform(0.85, 1.15)
    is_crit = force_crit or (rng.random() < _crit_chance(attacker_spd))
    if is_crit:
        base *= dc.CRIT_MULT
    return max(1, int(round(base))), is_crit


async def _award_kill(
    db: Any, guild_id: int, user_id: int,
    mob_state: dict, *, captured: bool,
) -> tuple[int, bool, int]:
    """Persist XP + level-up + kill log. Returns (xp_gained, leveled_up, new_level)."""
    mob_key = str(mob_state.get("key") or "")
    meta = dc.mob_meta(mob_key) or {}
    xp_gain = int(meta.get("xp", 10))
    state = await list_state(db, guild_id, user_id)
    cur_xp = int(state.get("xp") or 0)
    new_xp = cur_xp + xp_gain
    new_level = dc.level_from_xp(new_xp)
    cur_level = int(state.get("level") or 1)
    leveled = new_level > cur_level
    new_hp_max = max(1, int(round(
        dc.STARTING_HP * float((dc.class_meta(state.get("class_key") or "warrior") or {}).get("hp_mult", 1.0))
        + new_level * dc.HP_PER_LEVEL
        + max(0, int(state.get("hp_alloc") or 0)) * dc.STAT_POINT_HP_BONUS
    )))
    cur_hp = int(state.get("current_hp") or 0)
    bumped_hp = cur_hp + (new_hp_max - int(state.get("hp_max") or new_hp_max)) if leveled else cur_hp
    bumped_hp = min(new_hp_max, max(0, bumped_hp))

    await db.execute(
        """
        UPDATE user_dungeon
           SET xp           = $3,
               level        = $4,
               hp_max       = $5,
               current_hp   = $6,
               total_kills  = total_kills + 1,
               total_captures = total_captures + $7,
               bosses_slain = bosses_slain + $8,
               last_action_at = NOW(),
               updated_at   = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, new_xp, new_level, new_hp_max, bumped_hp,
        1 if captured else 0,
        1 if (mob_state.get("boss") and not captured) else 0,
    )
    await db.execute(
        """
        INSERT INTO dungeon_kills (guild_id, user_id, mob_key, mob_tier, floor, captured)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        guild_id, user_id, mob_key,
        int(meta.get("tier", 1)),
        int(state.get("current_floor") or 1),
        bool(captured),
    )
    await db.execute(
        """
        UPDATE dungeon_runs
           SET mobs_killed = mobs_killed + $3,
               captures    = captures    + $4
         WHERE run_id = $1 AND guild_id = $2
        """,
        int(state.get("run_id") or 0), guild_id,
        0 if captured else 1,
        1 if captured else 0,
    )
    # Themed Cryptstone XP: kills, boss kills, and captures all level the
    # owner's Cryptstone if they have one. Best-effort per the
    # themed_stones contract.
    try:
        from services import themed_stones as _ts
        await _ts.grant_cryptstone_xp(
            db, user_id, guild_id,
            kills=0 if captured else 1,
            captures=1 if captured else 0,
            bosses=1 if (mob_state.get("boss") and not captured) else 0,
        )
    except Exception:
        log.debug(
            "dungeon: themed_stones.grant_cryptstone_xp kill failed",
            exc_info=True,
        )
    return xp_gain, leveled, new_level


# ============================================================================
# Combat: attack / skill / flee
# ============================================================================

def _ammo_consume(consumables: dict, ammo_key: str, want: int) -> tuple[int, float]:
    """Burn up to ``want`` units of ``ammo_key`` from ``consumables`` (mutated in place).

    Returns ``(burned, dmg_mult)`` where ``dmg_mult`` is the per-shot
    damage multiplier this ammo grants (1.0 default; broadhead +25% etc).
    If less than ``want`` ammo is available the burn is partial; the
    caller is responsible for deciding how to handle missing shots
    (typically: scale the swing's damage by OUT_OF_AMMO_DAMAGE_MULT).
    """
    have = int(consumables.get(ammo_key) or 0)
    if have <= 0:
        return 0, 1.0
    take = min(have, max(0, int(want)))
    if take <= 0:
        return 0, 1.0
    consumables[ammo_key] = have - take
    if consumables[ammo_key] <= 0:
        consumables.pop(ammo_key, None)
    meta = dc.consumable_meta(ammo_key) or {}
    return take, float(meta.get("ammo_dmg_mult") or 1.0)


def _tick_player_buffs(buffs: dict) -> dict:
    """Decrement each buff's duration by 1 round; drop expired entries.

    Returns the same dict for caller-side serialisation. Buff metadata
    (`value`, `source`) is preserved so the engine can re-read the
    magnitude of e.g. a thorn aura's reflect % across rounds.
    """
    out: dict = {}
    for name, payload in (buffs or {}).items():
        if not isinstance(payload, dict):
            continue
        dur = int(payload.get("duration") or 0) - 1
        if dur > 0:
            out[name] = {**payload, "duration": dur}
    return out


_ABILITY_CD_PREFIX: str = "_ability_cd_"


def _ability_cd_remaining(buffs: dict, ability_key: str) -> int:
    """Return the remaining cooldown rounds for an ability stored in buffs."""
    payload = buffs.get(_ABILITY_CD_PREFIX + ability_key)
    if not isinstance(payload, dict):
        return 0
    # ``duration`` is what _tick_player_buffs decrements every round;
    # when it hits 0 the buffs entry is removed and the ability is ready.
    return max(0, int(payload.get("duration") or 0))


def _set_ability_cd(buffs: dict, ability_key: str, cd: int) -> None:
    """Stamp a fresh cooldown into buffs. ``cd`` is the rounds-to-ready value.

    The +1 fudge compensates for ``_tick_player_buffs`` decrementing the
    duration at the end of THIS round; without it a CD-3 ability would
    only block re-fire for 2 rounds.
    """
    cd = max(0, int(cd))
    if cd <= 0:
        buffs.pop(_ABILITY_CD_PREFIX + ability_key, None)
        return
    buffs[_ABILITY_CD_PREFIX + ability_key] = {"duration": cd + 1, "value": 0}


async def resolve_attack(
    db: Any, guild_id: int, user_id: int, *,
    mode: str = "attack",
    ability_key: str | None = None,
) -> CombatResult:
    """One round of combat.

    ``mode`` is one of:
      * ``"attack"``    -- basic weapon swing, no ability fired.
      * ``"skill"``     -- legacy alias; resolves to the player class's
                            primary ability (first entry in
                            ``CLASS_ABILITIES``). Kept so older code
                            paths (`,delve skill`, single Skill button)
                            keep working without code changes.
      * ``"ability"``   -- caller provides ``ability_key``; one of the
                            three abilities returned by
                            ``dc.class_abilities(class_key)``.

    Turn order:
      * Ranged weapons (bow / crossbow) take their first swing BEFORE the
        mob regardless of SPD (kiting).
      * Melee falls back to: player first iff player.spd >= mob.spd.

    Ammo:
      * Ranged basic attack burns AMMO_PER_RANGED_SWING ammo.
      * Ranged abilities burn ``ABILITIES[<key>]['ammo_cost']`` ammo.
      * Out-of-ammo ranged swings deal OUT_OF_AMMO_DAMAGE_MULT damage
        (improvised throwing -- a soft tax, not a hard lockout).

    Buffs (live in `player_buffs` JSONB on user_dungeon):
      * `marked_target` -- next N attacks against the active mob auto-crit.
      * `volley_charged` -- next basic ranged swing fires 3 shots.
      * `thorn_aura` -- mob melee swing reflects N% damage back.
      * `wildshape` -- +50% ATK and heal 5% max HP per turn.
      * `regen` -- +10% max HP per turn (regrowth_brew).
      * `sanctuary` -- mob damage halved.
      * ``_ability_cd_<key>`` -- per-ability cooldown (auto-decrements).
    """
    if mode not in ("attack", "skill", "ability"):
        raise ValueError(f"bad mode: {mode!r}")
    state = await list_state(db, guild_id, user_id)
    mob = dict(state.get("current_mob_state") or {})
    if not mob:
        raise ValueError("Nothing to fight in this room.")
    if int(state.get("current_hp") or 0) <= 0:
        raise ValueError("You're at 0 HP. Rest first.")

    pstats = player_combat_stats(state)
    skill_cd = int(state.get("skill_cd_remaining") or 0)
    consumables = dict(state.get("consumables") or {})
    buffs = dict(state.get("player_buffs") or {})

    rng = random.Random()
    log_lines: list[str] = []

    is_ranged   = (str(pstats.get("attack_kind") or "melee") == "ranged")
    ammo_key    = pstats.get("weapon_ammo_key") or None
    class_key   = str(pstats.get("class_key") or "")
    skill_key   = str(pstats.get("skill_key") or "")
    skill_kind  = str(pstats.get("skill_kind") or "melee")
    int_stat    = float(pstats.get("int") or 0.0)

    # Resolve mode="skill" (legacy) into mode="ability" with the player's
    # primary ability so the dispatch below has a single code path.
    primary_ability = (dc.class_abilities(class_key) or (skill_key,))[0]
    if mode == "skill":
        mode = "ability"
        ability_key = ability_key or primary_ability
    if mode == "ability" and not ability_key:
        ability_key = primary_ability

    # --- Decide the swing count + per-shot multiplier for this round -----
    # Abilities can fan out (Volley = 3 shots at 0.7x each), do a single
    # high-multiplier hit, or skip the attack swing entirely (Regrowth).
    swing_count   = 1
    swing_mult    = 1.0
    # Apex Mastery: Sharp Edge I/II (combat.dungeon_dmg) scales every
    # swing this round. Read once at round start so abilities that
    # override swing_mult later (Volley etc.) still pick up the bonus
    # via the mult passed into _swing_damage.
    try:
        from services import mastery as _mastery_d
        _mp = await _mastery_d.passives(db, user_id, guild_id)
        _dmg_bonus = float(_mp.get("combat.dungeon_dmg") or 0.0)
        if _dmg_bonus > 0:
            swing_mult *= (1.0 + _dmg_bonus)
    except Exception:
        log.debug("combat.dungeon_dmg passive read failed", exc_info=True)
    force_crit    = False
    crit_bonus    = dc.RANGED_CRIT_BONUS if is_ranged else 0.0
    new_skill_cd  = max(0, skill_cd - 1)
    skill_self_hp_pct = 0.0
    ability_stun_rounds = 0
    ability_mark_rounds = 0
    ability_def_pierce  = 0.0
    ability_lifesteal   = 0.0
    ammo_per_swing    = dc.AMMO_PER_RANGED_SWING

    # Volley pre-cast buff (from scroll_volley) upgrades the next basic
    # ranged swing to 3 shots without burning the class skill cooldown.
    volley_charged = ("volley_charged" in buffs) and is_ranged
    if mode == "attack" and volley_charged:
        swing_count = 3
        swing_mult  = 0.75
        log_lines.append("**Volley charge** primed -- 3 shots loose.")
        buffs.pop("volley_charged", None)

    if mode == "ability":
        ameta = dc.ability_meta(ability_key) or {}
        if not ameta:
            raise ValueError(f"Unknown ability: {ability_key!r}")
        # Authorization: ability must belong to the player's class.
        if ability_key not in dc.class_abilities(class_key):
            raise ValueError(
                f"**{ameta.get('name', ability_key)}** isn't one of your "
                f"class abilities."
            )
        # Cooldown gate. Primary ability also reads / writes the legacy
        # ``skill_cd_remaining`` column so old `,delve skill` callers
        # still see the same number.
        is_primary = (ability_key == primary_ability)
        if is_primary and skill_cd > 0:
            raise ValueError(f"**{ameta.get('name', ability_key)}** is on cooldown ({skill_cd} round(s) left).")
        cd_left = _ability_cd_remaining(buffs, ability_key)
        if cd_left > 0 and not is_primary:
            raise ValueError(f"**{ameta.get('name', ability_key)}** is on cooldown ({cd_left} round(s) left).")

        akind = str(ameta.get("kind") or skill_kind)
        if akind == "ranged" and not is_ranged:
            raise ValueError(
                f"**{ameta.get('name', ability_key)}** needs a ranged weapon "
                f"(bow / crossbow)."
            )
        # Set legacy skill_cd column for the primary ability so the
        # render path's "Skill: cooldown 3 rounds" indicator stays in
        # sync without reading the new buffs map.
        ab_cd = int(ameta.get("cd") or 0)
        if is_primary:
            new_skill_cd = ab_cd
        _set_ability_cd(buffs, ability_key, ab_cd)

        # Apply ability properties.
        swing_mult = float(ameta.get("mult") or 1.0)
        swing_count = dc.ability_swings(ameta)
        force_crit  = bool(ameta.get("auto_crit"))
        if ameta.get("crit_bonus"):
            crit_bonus += float(ameta["crit_bonus"])
        ability_stun_rounds = int(ameta.get("stun_rounds") or 0)
        ability_mark_rounds = int(ameta.get("mark_rounds") or 0)
        ability_def_pierce  = float(ameta.get("def_pierce_pct") or 0.0)
        ability_lifesteal   = float(ameta.get("lifesteal_pct") or 0.0)
        skill_self_hp_pct   = float(ameta.get("heal_pct") or 0.0)

        if akind == "ranged":
            ammo_per_swing = int(ameta.get("ammo_cost") or dc.AMMO_PER_RANGED_SWING)
            if swing_count > 1 and ammo_per_swing > 1 and ammo_per_swing < swing_count:
                # Multi-swing volley-style: ammo_cost is total burned;
                # account for the stagger so we burn extra on shot 0
                # and 1-per-shot after that (mirrors the legacy volley
                # path).
                pass
        # Spell / self-target: skip attack swings entirely.
        if str(ameta.get("target") or "mob") == "self":
            swing_count = 0

        # Flavour line for the kill log.
        kind_tag = "spell" if akind == "spell" else "shot" if akind == "ranged" else "strike"
        if swing_count == 0:
            log_lines.append(
                f"You channel **{ameta.get('name', ability_key)}** -- focusing on yourself."
            )
        elif swing_count > 1:
            log_lines.append(
                f"You unleash **{ameta.get('name', ability_key)}** -- "
                f"{swing_count} {kind_tag}s at x{swing_mult:.2f} damage each."
            )
        else:
            log_lines.append(
                f"You unleash **{ameta.get('name', ability_key)}** -- "
                f"x{swing_mult:.2f} damage."
                + (" *(auto-crit)*" if force_crit else "")
            )
        # Marked-target stamp: future hits within ``mark_rounds``
        # auto-crit. Stored in player_buffs for the existing
        # marked_target consumer.
        if ability_mark_rounds > 0:
            buffs["marked_target"] = {
                "value": 1.0,
                "duration": ability_mark_rounds,
            }

    # Wildshape buff (from wildshape_potion or skill aftermath) boosts
    # ATK + slow-heals each round it ticks.
    wild_atk_mult = 1.0
    # Surface the active per-swing buffs in the round log so the player
    # actually SEES why their numbers look big -- otherwise wildshape /
    # shrine boons silently bake into ATK and the round just looks lucky.
    buff_notes: list[str] = []
    if "wildshape" in buffs:
        wval = float(buffs["wildshape"].get("value") or 0.50)
        wild_atk_mult *= 1.0 + wval
        buff_notes.append(f"Wildshape +{int(round(wval * 100))}% ATK")
    # Shrine atk buff: a flat ATK% boost on every swing for its duration.
    # Stacks multiplicatively with wildshape so a praying druid is scary.
    if "shrine_atk" in buffs:
        sval = float(buffs["shrine_atk"].get("value") or 0.0)
        wild_atk_mult *= 1.0 + sval
        buff_notes.append(f"Shrine ATK +{int(round(sval * 100))}%")
    if "shrine_spd" in buffs:
        spd_val = float(buffs["shrine_spd"].get("value") or 0.0)
        buff_notes.append(f"Shrine SPD +{int(round(spd_val * 100))}%")
    if "sanctuary" in buffs:
        buff_notes.append("Sanctuary halves incoming")
    if "thorn_aura" in buffs:
        tval = float(buffs["thorn_aura"].get("value") or 0.30)
        buff_notes.append(f"Thorn aura reflects {int(round(tval * 100))}%")
    if "regen" in buffs:
        rval = float(buffs["regen"].get("value") or 0.10)
        buff_notes.append(f"Regen +{int(round(rval * 100))}% HP/round")
    if buff_notes:
        log_lines.append("  -# Buffs: " + ", ".join(buff_notes) + ".")

    # Mark target buff: next N swings auto-crit; consumed on each hit.
    marked_remaining = int((buffs.get("marked_target") or {}).get("duration") or 0)

    # Effective per-swing ATK + spell-vs-physical scaling. Spell-kind
    # abilities add INT to the ATK base so caster classes' allocations
    # matter. ``ability_kind`` is the ability's own kind (falls back to
    # the class skill_kind when the player chose mode="attack").
    if mode == "ability":
        ability_kind = str((dc.ability_meta(ability_key) or {}).get("kind") or skill_kind)
    else:
        ability_kind = "ranged" if is_ranged else "melee"
    eff_atk = float(pstats["atk"]) * wild_atk_mult
    if ability_kind == "spell" and mode == "ability":
        eff_atk += int_stat * 1.2

    # ── Weapon affix damage modifiers ──────────────────────────────────
    # phys_dmg_pct boosts every swing for physical / ranged attacks;
    # spell_dmg_pct boosts only spell-kind ability swings.
    # vs_undead_atk_pct adds when the active mob carries the "undead"
    # tag (skeleton, ghoul, lich, wraith, banshee, lich_acolyte).
    weapon_affixes = pstats.get("weapon_affixes") or {}
    armor_affixes  = pstats.get("armor_affixes")  or {}
    is_spell_swing = (ability_kind == "spell" and mode == "ability")
    if not is_spell_swing:
        phys_pct = float(weapon_affixes.get("phys_dmg_pct") or 0.0)
        if phys_pct:
            eff_atk *= (1.0 + phys_pct)
    else:
        spell_pct = float(weapon_affixes.get("spell_dmg_pct") or 0.0)
        if spell_pct:
            eff_atk *= (1.0 + spell_pct)
    mob_tags = tuple((dc.mob_meta(mob.get("key")) or {}).get("tags") or ())
    if "undead" in mob_tags:
        vs_atk = float(weapon_affixes.get("vs_undead_atk_pct") or 0.0)
        if vs_atk:
            eff_atk *= (1.0 + vs_atk)
    # Crit affix adds to the per-swing crit roll. Folded into attacker_spd
    # below since _swing_damage's crit chance reads attacker_spd.
    crit_bonus += float(pstats.get("crit_affix") or 0.0)
    # Shrine spd buff: bumps the SPD passed into _swing_damage so the
    # crit_bonus + first-strike calc both benefit. Read once here so the
    # nested attack helper below sees the buffed value.
    if "shrine_spd" in buffs:
        # mutate via a local copy of pstats so the rest of the function
        # sees the buffed SPD without polluting the snapshot dict
        pstats = {**pstats, "spd": min(0.95, float(pstats["spd"]) + float(buffs["shrine_spd"].get("value") or 0.0))}

    # Ranged retaliation cut: mob's first counter only deals
    # RANGED_RETALIATION_MULT damage when the player opened the round
    # with a ranged shot (kiting). Single-round flag.
    ranged_first_round = is_ranged and (
        mode == "attack" or ability_kind == "ranged"
    )

    # --- Player swings ---------------------------------------------------
    def _do_player_attack() -> int:
        """Run swing_count player swings; return total damage dealt."""
        nonlocal marked_remaining
        total = 0
        for shot_i in range(swing_count):
            if mob["hp"] <= 0 or state["current_hp"] <= 0:
                break
            mult = swing_mult
            # Per-ammo damage multiplier (broadhead / piercing tweak).
            if is_ranged and ammo_key:
                burned, dmg_mult = _ammo_consume(consumables, ammo_key, ammo_per_swing if shot_i == 0 else 1)
                if burned <= 0:
                    mult *= dc.OUT_OF_AMMO_DAMAGE_MULT
                    if shot_i == 0:
                        log_lines.append(
                            f"  -# Out of ammo -- improvised shot at "
                            f"{int(dc.OUT_OF_AMMO_DAMAGE_MULT * 100)}% damage."
                        )
                else:
                    mult *= dmg_mult
            mark_used = (marked_remaining > 0)
            this_force_crit = force_crit or mark_used
            # Ability def_pierce: shave a fraction of mob's defence for
            # this swing (Piercing Shot leaves only 50% of def in play).
            mob_def = float(mob.get("def") or 0) * (1.0 - ability_def_pierce)
            dmg, crit = _swing_damage(
                eff_atk, mob_def,
                mult=mult,
                force_crit=this_force_crit,
                attacker_spd=pstats["spd"] + crit_bonus,
                rng=rng,
            )
            mob["hp"] = max(0, int(mob["hp"]) - dmg)
            total += dmg
            tag = "  **CRIT!**" if crit else ""
            # Marked-target stamp: spell out that the auto-crit came from
            # the buff and show how many marks remain so the player can
            # plan whether to burn a heavy ability before they fade.
            if mark_used:
                marks_left_after = marked_remaining - 1
                if marks_left_after > 0:
                    tag += f" *(marked, {marks_left_after} left)*"
                else:
                    tag += " *(marked, last)*"
            verb = "shoot" if is_ranged else "hit"
            log_lines.append(
                f"You {verb} **{(dc.mob_meta(mob['key']) or {}).get('name', mob['key'])}** "
                f"for **{dmg}**{tag}."
            )
            # Ability stun: applied AFTER the hit lands so the mob's
            # next counter-swing eats it.
            if ability_stun_rounds > 0 and mob["hp"] > 0:
                mob["stunned_turns"] = max(
                    int(mob.get("stunned_turns") or 0), ability_stun_rounds,
                )
            if marked_remaining > 0:
                marked_remaining -= 1
            # Buddy assist (per swing): chance for a fractional follow-up.
            if state.get("active_buddy_id") and rng.random() < dc.BUDDY_ASSIST_TURN_CHANCE:
                assist = max(1, int(round(dmg * dc.BUDDY_ASSIST_DAMAGE_FRACTION)))
                mob["hp"] = max(0, mob["hp"] - assist)
                total += assist
                log_lines.append(f"  + Your buddy chips in for **{assist}**.")
        return total

    def _do_mob_swing() -> int:
        """Run the mob's counter-attack; return mitigated damage applied."""
        if mob["hp"] <= 0 or state["current_hp"] <= 0:
            return 0
        if int(mob.get("stunned_turns") or 0) > 0:
            mob["stunned_turns"] = max(0, int(mob["stunned_turns"]) - 1)
            log_lines.append(
                f"  {(dc.mob_meta(mob['key']) or {}).get('name', mob['key'])} is stunned."
            )
            return 0
        dmg, crit = _swing_damage(
            float(mob.get("atk") or 1), 0.0,
            mult=1.0, force_crit=False,
            attacker_spd=float(mob.get("spd") or 0.5), rng=rng,
        )
        mitigated = max(1, int(round(dmg - pstats["def"] * 0.5)))
        # Sanctuary -> halve. Track the pre-mitigation value so the
        # combat log can show "X soaked by Sanctuary" instead of the
        # halving happening invisibly.
        sanctuary_saved = 0
        if "sanctuary" in buffs:
            pre_sanctuary = mitigated
            mitigated = max(1, int(round(mitigated * 0.5)))
            sanctuary_saved = max(0, pre_sanctuary - mitigated)
        # Ranged-first round: opener takes a reduced counter.
        if ranged_first_round:
            mitigated = max(1, int(round(mitigated * dc.RANGED_RETALIATION_MULT)))
        # Armor affix: vs_undead_def_pct cuts damage taken from undead-tagged mobs.
        if "undead" in mob_tags:
            vs_def = float(armor_affixes.get("vs_undead_def_pct") or 0.0)
            if vs_def:
                mitigated = max(1, int(round(mitigated * (1.0 - vs_def))))
        state["current_hp"] = max(0, int(state["current_hp"]) - mitigated)
        tag = "  *crit*" if crit else ""
        log_lines.append(
            f"{(dc.mob_meta(mob['key']) or {}).get('name', mob['key'])} hits "
            f"you for **{mitigated}**{tag}."
        )
        if sanctuary_saved > 0:
            log_lines.append(
                f"  + Sanctuary soaks **{sanctuary_saved}** damage."
            )
        # Thorn aura -> reflect a fraction back.
        if "thorn_aura" in buffs:
            reflect = max(1, int(round(mitigated * float(buffs["thorn_aura"].get("value") or 0.30))))
            mob["hp"] = max(0, mob["hp"] - reflect)
            log_lines.append(f"  + Thorn aura reflects **{reflect}** back.")
        return mitigated

    # Turn-order: ranged player ALWAYS strikes first; otherwise SPD ties.
    player_first = dc.RANGED_FIRST_STRIKE if is_ranged else (
        pstats["spd"] >= float(mob.get("spd") or 0.5)
    )
    if player_first:
        _do_player_attack()
        _do_mob_swing()
    else:
        _do_mob_swing()
        _do_player_attack()

    # --- Apply post-swing self-heals + regen tick ------------------------
    hp_max = int(state.get("hp_max") or dc.STARTING_HP)
    if skill_self_hp_pct > 0 and state["current_hp"] > 0:
        heal = max(1, int(round(hp_max * skill_self_hp_pct)))
        state["current_hp"] = min(hp_max, int(state["current_hp"]) + heal)
        log_lines.append(f"  + Wildshape regrows **{heal}** HP.")
    if "wildshape" in buffs and state["current_hp"] > 0:
        heal = max(1, int(round(hp_max * 0.05)))
        state["current_hp"] = min(hp_max, int(state["current_hp"]) + heal)
        log_lines.append(f"  + Beast-form heals **{heal}** HP.")
    if "regen" in buffs and state["current_hp"] > 0:
        pct = float(buffs["regen"].get("value") or 0.10)
        heal = max(1, int(round(hp_max * pct)))
        state["current_hp"] = min(hp_max, int(state["current_hp"]) + heal)
        log_lines.append(f"  + Regrowth heals **{heal}** HP.")

    # --- Tick + persist buffs --------------------------------------------
    if marked_remaining > 0 and "marked_target" in buffs:
        # Sync the (potentially decremented) mark counter back into duration.
        buffs["marked_target"] = {**buffs["marked_target"], "duration": marked_remaining}
    elif "marked_target" in buffs and marked_remaining <= 0:
        buffs.pop("marked_target", None)
    buffs = _tick_player_buffs(buffs)

    # --- Persist HP + skill cooldown + mob state + ammo + buffs ----------
    outcome = "continue"
    mob_state_json: dict | None = mob
    boss_kill = False
    mini_boss_kill = False
    loot_drop_key: str | None = None
    loot_drop_kind: str | None = None
    rune_drop_human = 0.0
    ore_drop_sym: str | None = None
    ore_drop_qty = 0.0
    xp_gain = 0
    leveled = False
    new_level = pstats["level"]
    junk_drop_key: str | None = None

    if state["current_hp"] <= 0 and mob["hp"] <= 0:
        outcome = "player_dead"
        mob_state_json = None
    elif mob["hp"] <= 0:
        outcome = "mob_dead"
        meta = dc.mob_meta(mob["key"]) or {}
        rune_drop_human = float(meta.get("rune_drop") or 0)
        ore_drop_sym = meta.get("ore_drop")
        ore_drop_qty = float(meta.get("ore_qty") or 0.0)
        boss_kill = bool(mob.get("boss"))
        mini_boss_kill = bool(mob.get("mini_boss"))
        # Curse + relic kicker on kill drops. Stacks multiplicatively
        # with each other and with mining boosts at credit time.
        rune_drop_human *= dc.curse_mult(state.get("run_curse"), "rune_mult", 1.0)
        rune_drop_human *= dc.relic_effect(
            state.get("equipped_relic"), "rune_drop_mult", 1.0,
        )
        ore_drop_qty *= dc.curse_mult(state.get("run_curse"), "ore_mult", 1.0)
        ore_drop_qty *= dc.relic_effect(
            state.get("equipped_relic"), "mine_yield_mult", 1.0,
        )
        # Apex Mastery: Treasure Hunter (luck.dungeon_loot) scales
        # rune + ore drops on every kill. Reuses the passives dict
        # read above for combat.dungeon_dmg (cached in closure).
        try:
            from services import mastery as _mastery_l
            _mp = await _mastery_l.passives(db, user_id, guild_id)
            _loot_bonus = float(_mp.get("luck.dungeon_loot") or 0.0)
            if _loot_bonus > 0:
                rune_drop_human *= (1.0 + _loot_bonus)
                ore_drop_qty   *= (1.0 + _loot_bonus)
        except Exception:
            pass
        # Vampire fang lifesteal: heal a fraction of total damage dealt
        # this round. Cheap proxy: scale on mob's max_hp since exact dmg
        # numbers aren't aggregated here. Stacks additively with the
        # weapon-affix lifesteal so a Vampire Fang relic + lifesteal
        # weapon both contribute.
        lifesteal = dc.relic_effect(state.get("equipped_relic"), "lifesteal_pct", 0.0)
        lifesteal += float(weapon_affixes.get("lifesteal_pct") or 0.0)
        # One-shot ability lifesteal stacks on top (Poison Strike,
        # Dragonfang Dirk-style proc abilities).
        lifesteal += float(ability_lifesteal or 0.0)
        if lifesteal > 0 and state["current_hp"] > 0:
            hp_max_now = int(state.get("hp_max") or dc.STARTING_HP)
            heal = max(1, int(round(int(mob.get("max_hp") or 0) * lifesteal)))
            state["current_hp"] = min(hp_max_now, int(state["current_hp"]) + heal)
            log_lines.append(f"  + Lifesteal drinks back **{heal}** HP.")
        xp_gain, leveled, new_level = await _award_kill(
            db, guild_id, user_id, mob, captured=False,
        )
        mob_state_json = None
        # Secondary junk drop on the kill (salvage / craft mat / usable).
        # Floor-gated + weighted in dc.roll_junk_drop; logged for the
        # combat reply and persisted via _credit_junk.
        floor_for_drop = int(state.get("current_floor") or 1)
        junk_drop_key = dc.roll_junk_drop(floor_for_drop, rng, source="combat")
        if junk_drop_key:
            await _credit_junk(db, guild_id, user_id, junk_drop_key)
            jmeta = dc.junk_meta(junk_drop_key) or {}
            log_lines.append(
                f"  + Loot: {jmeta.get('emoji', '')} **{jmeta.get('name', junk_drop_key)}** "
                f"({str(jmeta.get('kind', '')).title()})"
            )
        # Boss / mini-boss loot table roll. Independent of junk_drop_key
        # so a boss kill can pay BOTH a salvage breadcrumb AND a piece
        # of rare gear in the same encounter -- a hard-fought win
        # should always feel celebratory.
        loot_pool = None
        if boss_kill:
            loot_pool = dc.boss_loot_pool(mob["key"]) or dc.mini_boss_loot_pool("deep")
        elif mini_boss_kill:
            mb_meta = dc.mini_boss_meta(mob["key"]) or {}
            loot_pool = dc.mini_boss_loot_pool(str(mb_meta.get("loot_pool") or ""))
        if loot_pool:
            picked = dc.roll_loot_table(loot_pool, rng)
            if picked:
                pkey, pkind = picked
                awarded_key, awarded_kind = await _credit_loot_drop(
                    db, guild_id, user_id, pkey, pkind,
                )
                loot_drop_key, loot_drop_kind = awarded_key, awarded_kind
                # Build a flavour line referencing the awarded item's
                # actual rarity (post-fallback) so the kill reply
                # shows "+ rare iron_shortsword" not the rolled key.
                catalog = (
                    dc.WEAPONS if awarded_kind == "weapon"
                    else dc.ARMOR if awarded_kind == "armor"
                    else dc.JUNK
                )
                ameta = catalog.get(awarded_key) or {}
                rarity = dc.item_rarity(ameta)
                rdot = dc.rarity_dot(rarity)
                em = ameta.get("emoji", "")
                nm = ameta.get("name", awarded_key)
                tag = "Boss drop" if boss_kill else "Mini-boss drop"
                log_lines.append(
                    f"  + {tag}: {rdot} {em} **{nm}** "
                    f"*({dc.rarity_label(rarity)} {awarded_kind})*"
                )
    elif state["current_hp"] <= 0:
        outcome = "player_dead"
        mob_state_json = None

    new_room_type_sql = (
        "CASE WHEN $5::jsonb IS NOT NULL THEN current_room_type "
        "WHEN $6 THEN 'stairs' ELSE 'empty' END"
    )
    await db.execute(
        f"""
        UPDATE user_dungeon
           SET current_hp         = $3,
               skill_cd_remaining = $4,
               current_mob_state  = $5::jsonb,
               current_room_type  = {new_room_type_sql},
               consumables        = $7::jsonb,
               player_buffs       = $8::jsonb,
               last_action_at     = NOW(),
               updated_at         = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(state["current_hp"]), int(new_skill_cd),
        _json(mob_state_json) if mob_state_json else None,
        bool(boss_kill),
        _json(consumables), _json(buffs),
    )
    return CombatResult(
        outcome=outcome, log=log_lines,
        mob_state=mob_state_json,
        player_hp=int(state["current_hp"]), player_max_hp=int(state.get("hp_max") or 0),
        mob_xp=xp_gain, rune_drop_human=rune_drop_human,
        ore_drop_symbol=ore_drop_sym, ore_drop_qty_human=ore_drop_qty,
        leveled_up=leveled, new_level=new_level,
        boss_kill=boss_kill,
        mini_boss_kill=mini_boss_kill,
        junk_drop_key=junk_drop_key,
        loot_drop_key=loot_drop_key,
        loot_drop_kind=loot_drop_kind,
    )


async def resolve_flee(db: Any, guild_id: int, user_id: int) -> CombatResult:
    """Try to flee combat. Costs FLEE_HP_PENALTY_PCT of max HP on success."""
    state = await list_state(db, guild_id, user_id)
    mob = dict(state.get("current_mob_state") or {})
    if not mob:
        raise ValueError("Nothing to flee from.")
    if mob.get("boss"):
        raise ValueError("You can't flee from a boss.")
    rng = random.Random()
    success = rng.random() < dc.FLEE_BASE_CHANCE
    log_lines: list[str] = []
    if success:
        hp_max = int(state.get("hp_max") or dc.STARTING_HP)
        penalty = max(1, int(round(hp_max * dc.FLEE_HP_PENALTY_PCT)))
        new_hp = max(0, int(state.get("current_hp") or 0) - penalty)
        await db.execute(
            """
            UPDATE user_dungeon
               SET current_hp = $3,
                   current_mob_state = NULL,
                   current_room_type = 'empty',
                   last_action_at = NOW(),
                   updated_at = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id, user_id, new_hp,
        )
        log_lines.append(f"You bolt and lose **{penalty}** HP catching your breath.")
        return CombatResult(
            outcome="fled", log=log_lines, mob_state=None,
            player_hp=new_hp, player_max_hp=hp_max,
        )
    log_lines.append("You can't shake them. The fight continues.")
    return CombatResult(
        outcome="failed_flee", log=log_lines, mob_state=mob,
        player_hp=int(state.get("current_hp") or 0),
        player_max_hp=int(state.get("hp_max") or 0),
    )


# ============================================================================
# Capture
# ============================================================================

async def attempt_capture(
    db: Any, guild_id: int, user_id: int, *, charm: bool = False,
) -> CaptureResult:
    """Try to tame the active mob. Requires HP <= CAPTURE_HP_THRESHOLD * max_hp.

    On success, inserts a dungeon_party row, ends combat, awards capture XP.
    Bosses cannot be captured.
    """
    state = await list_state(db, guild_id, user_id)
    mob = dict(state.get("current_mob_state") or {})
    if not mob:
        raise ValueError("Nothing to capture.")
    if mob.get("boss"):
        raise ValueError("Bosses cannot be captured.")

    party_count = await db.fetch_val(
        """
        SELECT COUNT(*)::int FROM dungeon_party
         WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned'
        """,
        guild_id, user_id,
    )
    if int(party_count or 0) >= dc.MAX_PARTY_SIZE:
        raise ValueError(
            f"Party is full ({dc.MAX_PARTY_SIZE}). Release a buddy first."
        )

    max_hp = max(1, int(mob.get("max_hp") or 1))
    hp_pct = max(0.0, int(mob.get("hp") or 0) / max_hp)
    chance = dc.capture_chance(str(mob.get("key") or ""), hp_pct, charm)
    if chance <= 0:
        raise ValueError(
            f"This mob is too healthy. Bring it under "
            f"{int(dc.CAPTURE_HP_THRESHOLD * 100)}% HP first."
        )

    rng = random.Random()
    success = rng.random() < chance
    log_lines: list[str] = []
    party_id: int | None = None
    if success:
        meta = dc.mob_meta(mob["key"]) or {}
        row = await db.fetch_one(
            """
            INSERT INTO dungeon_party (
                guild_id, owner_user_id, species_key, name,
                captured_floor
            )
            VALUES ($1, $2, $3, $4, $5)
            RETURNING party_id
            """,
            guild_id, user_id, str(mob["key"]),
            str(meta.get("name") or mob["key"]).title(),
            int(state.get("current_floor") or 1),
        )
        party_id = int(row["party_id"])
        await _award_kill(db, guild_id, user_id, mob, captured=True)
        await db.execute(
            """
            UPDATE user_dungeon
               SET current_mob_state  = NULL,
                   current_room_type  = 'empty',
                   last_action_at     = NOW(),
                   updated_at         = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id, user_id,
        )
        log_lines.append(
            f"Tamed! {(meta.get('emoji') or '')} **{meta.get('name') or mob['key']}** "
            f"joins your party."
        )
        return CaptureResult(
            success=True, chance=chance, party_id=party_id,
            mob_key=str(mob["key"]), log=log_lines,
        )
    log_lines.append(
        f"It thrashes free ({int(chance * 100)}% chance). The fight resumes."
    )
    return CaptureResult(
        success=False, chance=chance,
        mob_key=str(mob.get("key") or ""), log=log_lines,
    )


async def list_party(
    db: Any, guild_id: int, user_id: int, *, owned_only: bool = True,
) -> list[dict]:
    where_status = "AND status = 'owned'" if owned_only else ""
    rows = await db.fetch_all(
        f"""
        SELECT * FROM dungeon_party
         WHERE guild_id = $1 AND owner_user_id = $2 {where_status}
         ORDER BY captured_at DESC
        """,
        guild_id, user_id,
    )
    return [dict(r) for r in (rows or [])]


async def set_active_buddy(
    db: Any, guild_id: int, user_id: int, party_id: int | None,
) -> dict | None:
    """Activate a captured buddy as the player's combat assistant. None to clear."""
    if party_id is not None:
        row = await db.fetch_one(
            """
            SELECT party_id FROM dungeon_party
             WHERE party_id = $1 AND guild_id = $2 AND owner_user_id = $3
               AND status = 'owned'
            """,
            int(party_id), guild_id, user_id,
        )
        if row is None:
            raise ValueError("That buddy is not in your party.")
    await db.execute(
        """
        UPDATE user_dungeon
           SET active_buddy_id = $3,
               last_action_at  = NOW(),
               updated_at      = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, party_id,
    )
    if party_id is None:
        return None
    return await db.fetch_one(
        "SELECT * FROM dungeon_party WHERE party_id = $1",
        int(party_id),
    )


async def release_buddy(
    db: Any, guild_id: int, user_id: int, party_id: int,
) -> bool:
    row = await db.fetch_one(
        """
        UPDATE dungeon_party
           SET status = 'released',
               released_at = NOW()
         WHERE party_id = $1 AND guild_id = $2 AND owner_user_id = $3
           AND status = 'owned'
         RETURNING party_id
        """,
        int(party_id), guild_id, user_id,
    )
    if row is None:
        return False
    await db.execute(
        """
        UPDATE user_dungeon
           SET active_buddy_id = CASE WHEN active_buddy_id = $3
                                       THEN NULL ELSE active_buddy_id END,
               last_action_at  = NOW(),
               updated_at      = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(party_id),
    )
    return True


# ============================================================================
# Oracle / burn-candle / LP fan-out helpers (mirrors services/fishing.py)
# ============================================================================

async def _oracle_price(db: Any, guild_id: int, symbol: str) -> float:
    row = await db.fetch_one(
        "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), symbol.upper(),
    )
    if row and row.get("price") is not None:
        return float(row["price"])
    from core.config import Config
    return float(Config.TOKENS.get(symbol.upper(), {}).get("start_price", 1.0))


def _minute_ts() -> int:
    return int(_time.time()) // 60 * 60


async def _write_burn_candle(
    db: Any, guild_id: int, symbol: str,
    oracle_before: float, oracle_after: float, volume_usd: float,
) -> None:
    try:
        await db.upsert_candle(
            int(guild_id), f"{symbol.upper()}USD", _minute_ts(),
            open_=float(oracle_before),
            high=max(float(oracle_before), float(oracle_after)),
            low=min(float(oracle_before), float(oracle_after)),
            close=float(oracle_after),
            volume_delta=float(max(0.0, volume_usd)),
        )
    except Exception:
        log.exception(
            "dungeon burn candle update failed gid=%s sym=%s",
            guild_id, symbol,
        )


async def _distribute_burn_lp_reward(
    db: Any, guild_id: int, symbol: str, fee_usd: float,
) -> float:
    """Pay a USD reward to LP holders of pools containing ``symbol``.

    Mirrors services/fishing._distribute_burn_lp_reward exactly. No-op
    when no LP positions hold ``symbol`` (the common case for the
    EARN_ONLY tokens until someone seeds a manual pool).
    """
    if fee_usd <= 0:
        return 0.0
    sym = symbol.upper()
    rows = await db.fetch_all(
        """
        SELECT lp.user_id, lp.pool_id, lp.lp_shares, p.total_lp
          FROM lp_positions lp
          JOIN pools p
            ON p.pool_id = lp.pool_id
           AND p.guild_id = lp.guild_id
         WHERE lp.guild_id = $1
           AND lp.lp_shares > 0
           AND COALESCE(p.vault_locked, FALSE) = FALSE
           AND (p.token_a = $2 OR p.token_b = $2)
        """,
        int(guild_id), sym,
    )
    if not rows:
        return 0.0
    weights: list[tuple[int, float]] = []
    total_weight = 0.0
    for r in rows:
        total_lp = int(r.get("total_lp") or 0)
        shares = int(r.get("lp_shares") or 0)
        if total_lp <= 0 or shares <= 0:
            continue
        w = shares / total_lp
        weights.append((int(r["user_id"]), w))
        total_weight += w
    if total_weight <= 0:
        return 0.0
    paid_total = 0.0
    for uid, w in weights:
        payout_usd = fee_usd * (w / total_weight)
        payout_raw = to_raw(payout_usd)
        if payout_raw <= 0:
            continue
        try:
            async with db.atomic():
                await db.update_wallet(uid, int(guild_id), int(payout_raw))
                await db.log_tx(
                    int(guild_id), uid, "LP_BURN_REWARD",
                    symbol_in=sym,
                    symbol_out="USD", amount_out=int(payout_raw),
                    network="usd",
                )
            paid_total += payout_usd
        except Exception:
            log.exception(
                "dungeon lp burn reward credit failed gid=%s uid=%s sym=%s usd=%.6f",
                guild_id, uid, sym, payout_usd,
            )
    return paid_total


def _price_impact(usd_value: float, oracle: float, supply_human: float) -> float:
    """Same impact formula cogs/trade.py / fishing burns use."""
    from core.config import Config
    impact = usd_value / float(Config.PRICE_IMPACT_DIVISOR)
    market_cap = max(0.0, oracle * supply_human)
    if market_cap > 0 and usd_value > 0.001 * market_cap:
        mc_ratio = usd_value / market_cap
        impact *= min(1.0 + mc_ratio * 2.0, 5.0)
    return min(impact, _price_impact_max())


async def _supply_human(db: Any, guild_id: int, symbol: str) -> float:
    row = await db.fetch_one(
        "SELECT circulating_supply FROM crypto_prices "
        "WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), symbol,
    )
    return to_human(int((row or {}).get("circulating_supply") or 0))


async def _apply_mint_oracle_drop(
    db: Any, guild_id: int, symbol: str, mint_amount_human: float,
) -> tuple[float, float, float]:
    """Drop ``symbol`` oracle by the standard impact for a mint of size N.

    Returns (oracle_before, oracle_after, impact_pct). Best-effort: a
    chart-write hiccup never aborts the upstream credit. update_wallet_holding
    on the user side has already moved circulating_supply by the time this
    is called.
    """
    if mint_amount_human <= 0:
        return 0.0, 0.0, 0.0
    try:
        oracle_before = await _oracle_price(db, guild_id, symbol)
        if oracle_before <= 0:
            return 0.0, 0.0, 0.0
        usd_value = float(mint_amount_human) * oracle_before
        supply = await _supply_human(db, guild_id, symbol)
        impact = _price_impact(usd_value, oracle_before, supply)
        oracle_after = max(1e-9, oracle_before * (1.0 - impact))
        await db.update_price(symbol, guild_id, oracle_after)
        await _write_burn_candle(db, guild_id, symbol, oracle_before, oracle_after, usd_value)
        return float(oracle_before), float(oracle_after), float(impact)
    except Exception:
        log.exception("dungeon mint oracle drop failed gid=%s sym=%s", guild_id, symbol)
        return 0.0, 0.0, 0.0


# ============================================================================
# Mining (ore mint) + boss/mob ore drop credit
# ============================================================================

_LIFETIME_COL_BY_ORE: dict[str, str] = {
    dc.COPPER_SYMBOL: "total_copper_mined_raw",
    dc.SILVER_SYMBOL: "total_silver_mined_raw",
    dc.GOLD_SYMBOL:   "total_gold_mined_raw",
}

_RUN_COL_BY_ORE: dict[str, str] = {
    dc.COPPER_SYMBOL: "copper_mined_raw",
    dc.SILVER_SYMBOL: "silver_mined_raw",
    dc.GOLD_SYMBOL:   "gold_mined_raw",
}


async def _credit_junk(
    db: Any, guild_id: int, user_id: int, junk_key: str,
) -> None:
    """Increment a player's junk counter dict atomically.

    Uses Postgres' ``jsonb_set`` so the existing JSONB shape stays a
    flat ``{"key": qty, ...}`` counter without round-tripping through
    Python -- mirrors the way relics_owned and consumables get bumped
    elsewhere in this module.
    """
    if not junk_key:
        return
    await db.execute(
        """
        UPDATE user_dungeon
           SET junk_inventory = jsonb_set(
                   COALESCE(junk_inventory, '{}'::jsonb),
                   ARRAY[$3::text],
                   to_jsonb(
                       COALESCE((junk_inventory->>$3)::int, 0) + 1
                   )
               ),
               total_junk_collected = total_junk_collected + 1,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, str(junk_key),
    )


async def _credit_loot_drop(
    db: Any, guild_id: int, user_id: int,
    item_key: str, kind: str,
) -> tuple[str, str]:
    """Credit a boss / mini-boss loot table drop to the right inventory.

    ``kind`` is ``"weapon"``, ``"armor"`` or ``"junk"``. Weapon/armor
    drops the player already owns fall back to a high-tier junk drop
    (so the kill always feels rewarded). Returns ``(awarded_key,
    awarded_kind)`` -- the kind may be ``"junk"`` after a fallback.
    """
    if kind == "junk":
        await _credit_junk(db, guild_id, user_id, item_key)
        return item_key, "junk"
    state = await ensure_state(db, guild_id, user_id)
    if kind == "weapon":
        owned = _as_dict(state.get("weapons_owned"))
        if item_key in owned:
            fallback = dc.loot_fallback_junk(random.Random())
            await _credit_junk(db, guild_id, user_id, fallback)
            return fallback, "junk"
        owned[item_key] = 1
        await db.execute(
            "UPDATE user_dungeon SET weapons_owned = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(owned),
        )
        return item_key, "weapon"
    if kind == "armor":
        owned = _as_dict(state.get("armor_owned"))
        if item_key in owned:
            fallback = dc.loot_fallback_junk(random.Random())
            await _credit_junk(db, guild_id, user_id, fallback)
            return fallback, "junk"
        owned[item_key] = 1
        await db.execute(
            "UPDATE user_dungeon SET armor_owned = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(owned),
        )
        return item_key, "armor"
    raise ValueError(f"bad loot kind: {kind!r}")


async def _credit_ore(
    db: Any, guild_id: int, user_id: int,
    ore_symbol: str, qty_human: float, run_id: int,
) -> int:
    """Mint ``qty_human`` of ``ore_symbol`` to the user, update lifetime + run
    totals, drop the ore oracle by the standard impact. Returns raw qty credited.
    """
    if ore_symbol not in dc.ORE_SYMBOLS:
        raise ValueError(f"Not an ore: {ore_symbol!r}")
    qty_raw = to_raw(max(0.0, float(qty_human)))
    if qty_raw <= 0:
        return 0
    await db.update_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, ore_symbol, int(qty_raw),
    )
    lifetime_col = _LIFETIME_COL_BY_ORE[ore_symbol]
    await db.execute(
        f"""
        UPDATE user_dungeon
           SET {lifetime_col} = {lifetime_col} + $3::numeric,
               last_action_at = NOW(),
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(qty_raw),
    )
    if run_id:
        run_col = _RUN_COL_BY_ORE[ore_symbol]
        await db.execute(
            f"""
            UPDATE dungeon_runs
               SET {run_col} = {run_col} + $3::numeric
             WHERE run_id = $1 AND guild_id = $2
            """,
            int(run_id), guild_id, int(qty_raw),
        )
    await _apply_mint_oracle_drop(db, guild_id, ore_symbol, qty_human)
    return int(qty_raw)


async def _credit_rune(
    db: Any, guild_id: int, user_id: int, rune_human: float, run_id: int,
) -> int:
    """Mint RUNE drop (boss kills, treasure). Mirrors _credit_ore."""
    rune_raw = to_raw(max(0.0, float(rune_human)))
    if rune_raw <= 0:
        return 0
    await db.update_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL, int(rune_raw),
    )
    await db.execute(
        """
        UPDATE user_dungeon
           SET total_rune_earned_raw = total_rune_earned_raw + $3::numeric,
               last_action_at = NOW(),
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(rune_raw),
    )
    if run_id:
        await db.execute(
            """
            UPDATE dungeon_runs
               SET rune_earned_raw = rune_earned_raw + $3::numeric
             WHERE run_id = $1 AND guild_id = $2
            """,
            int(run_id), guild_id, int(rune_raw),
        )
    await _apply_mint_oracle_drop(db, guild_id, dc.RUNE_SYMBOL, rune_human)
    return int(rune_raw)


async def mine_ore(db: Any, guild_id: int, user_id: int) -> MineResult:
    """Mine the ore vein in the current room.

    Consumes the best ``mine_boost`` consumable held (by ``value``) and applies
    its multiplier to the ore yield. Supports any consumable with
    ``kind == "mine_boost"`` (pickaxe_oil, diamond_pickaxe, ...).
    """
    state = await list_state(db, guild_id, user_id)
    if state.get("current_room_type") != "ore":
        raise ValueError("No ore vein in this room.")
    payload = dict(state.get("current_room_payload") or {})
    ore_sym = str(payload.get("ore_symbol") or dc.COPPER_SYMBOL)
    qty = float(payload.get("ore_qty") or 0.0)
    if qty <= 0:
        raise ValueError("This vein is dry.")
    consumables = dict(state.get("consumables") or {})

    boost_key: str | None = None
    boost_value: float = 0.0
    for ckey, ccount in consumables.items():
        try:
            if int(ccount) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        meta = dc.consumable_meta(str(ckey)) or {}
        if str(meta.get("kind")) != "mine_boost":
            continue
        val = float(meta.get("value") or 0.0)
        if val > boost_value:
            boost_value = val
            boost_key = str(ckey)

    if boost_key is not None and boost_value > 0:
        qty *= 1.0 + boost_value
        consumables[boost_key] = int(consumables[boost_key]) - 1
        if consumables[boost_key] <= 0:
            consumables.pop(boost_key, None)
        # NFT layer: burn one of the consumed token. Best-effort.
        try:
            from services import items as _items
            await _items.consume_one(
                db,
                guild_id=guild_id, user_id=user_id,
                contract_address=_items.contract_address("consumable", boost_key),
                reason="dungeon.mine",
            )
        except Exception:
            log.debug(
                "nft %s burn sync failed gid=%s uid=%s",
                boost_key, guild_id, user_id, exc_info=True,
            )

    # Equipped relic (Miner's Charm, Godslayer's Eye, ...) and the active
    # run curse both modify ore yield. Stack multiplicatively with each
    # other and with the pickaxe-oil boost above.
    qty *= dc.relic_effect(state.get("equipped_relic"), "mine_yield_mult", 1.0)
    qty *= dc.curse_mult(state.get("run_curse"), "ore_mult", 1.0)

    oracle_before = await _oracle_price(db, guild_id, ore_sym)
    # Active-buddy delve-lane bonus inflates the ore yield. Sits on top
    # of any pickaxe consumable boost above; signature-lane Cave-types
    # (cobble / glitch / robo) can stack this into a real edge by mid
    # level. Best-effort.
    try:
        from services.buddy_bonus import buddy_bonus as _bb
        qty *= await _bb(db, guild_id, user_id, lane="delve")
    except Exception:
        log.debug("dungeon mine buddy_bonus failed", exc_info=True)

    qty_raw = await _credit_ore(
        db, guild_id, user_id, ore_sym, qty,
        int(state.get("run_id") or 0),
    )
    oracle_after = await _oracle_price(db, guild_id, ore_sym)
    impact = 0.0 if oracle_before <= 0 else max(0.0, (oracle_before - oracle_after) / oracle_before)

    await db.execute(
        """
        UPDATE user_dungeon
           SET current_room_type = 'empty',
               current_room_payload = NULL,
               consumables = $3::jsonb,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(consumables),
    )
    # Themed Cryptstone XP: each successful mine action levels the
    # owner's Cryptstone if they have one.
    try:
        from services import themed_stones as _ts
        await _ts.grant_cryptstone_xp(db, user_id, guild_id, mines=1)
    except Exception:
        log.debug(
            "dungeon: themed_stones.grant_cryptstone_xp mine failed",
            exc_info=True,
        )
    # Junk drop on the mine action -- lower base chance than combat or
    # chest opens (players mine more often) but the same catalog.
    floor_for_drop = int(state.get("current_floor") or 1)
    junk_key = dc.roll_junk_drop(floor_for_drop, random.Random(), source="mine")
    if junk_key:
        await _credit_junk(db, guild_id, user_id, junk_key)
    return MineResult(
        ore_symbol=ore_sym,
        qty_human=float(qty),
        qty_raw=int(qty_raw),
        oracle_before=float(oracle_before),
        oracle_after=float(oracle_after),
        impact_pct=float(impact),
        junk_drop_key=junk_key,
    )


# ============================================================================
# Token economy: ORE -> RUNE burn-swap, RUNE -> USD cashout
# ============================================================================

async def get_ore_wallet_raw(
    db: Any, guild_id: int, user_id: int, ore_symbol: str,
) -> int:
    row = await db.get_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, ore_symbol,
    )
    return int((row or {}).get("amount") or 0)


async def get_rune_wallet_raw(db: Any, guild_id: int, user_id: int) -> int:
    row = await db.get_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL,
    )
    return int((row or {}).get("amount") or 0)


async def burn_ore_for_rune(
    db: Any, guild_id: int, user_id: int,
    ore_symbol: str, ore_amount_raw: int,
) -> BurnResult:
    """Burn ore, mint RUNE. Mirrors fishing.burn_lure_for_reel.

    Conversion preserves USD value at the live oracle on each side: the
    user gets RUNE worth the burnt ore minus the standard slippage.
    """
    if ore_symbol not in dc.ORE_SYMBOLS:
        raise ValueError(f"Not an ore: {ore_symbol!r}")
    if ore_amount_raw <= 0:
        raise ValueError("Amount must be positive.")
    held = await get_ore_wallet_raw(db, guild_id, user_id, ore_symbol)
    if held < int(ore_amount_raw):
        raise ValueError(
            f"You only have {to_human(held):,.4f} {ore_symbol}."
        )

    ore_oracle_before = await _oracle_price(db, guild_id, ore_symbol)
    rune_oracle_before = await _oracle_price(db, guild_id, dc.RUNE_SYMBOL)
    if ore_oracle_before <= 0 or rune_oracle_before <= 0:
        raise ValueError("Oracle price is zero -- try again in a moment.")

    ore_human = to_human(int(ore_amount_raw))
    usd_value = ore_human * ore_oracle_before

    ore_supply  = await _supply_human(db, guild_id, ore_symbol)
    rune_supply = await _supply_human(db, guild_id, dc.RUNE_SYMBOL)
    ore_impact  = _price_impact(usd_value, ore_oracle_before,  ore_supply)
    rune_impact = _price_impact(usd_value, rune_oracle_before, rune_supply)

    eff_rune_price = rune_oracle_before * (1.0 + rune_impact / 2.0)
    rune_minted_human = usd_value / max(1e-12, eff_rune_price)
    rune_minted_raw = to_raw(rune_minted_human)
    if rune_minted_raw <= 0:
        raise ValueError("Burn produces zero RUNE -- raise the amount.")

    await db.update_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, ore_symbol, -int(ore_amount_raw),
    )
    try:
        await db.update_wallet_holding(
            user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL, int(rune_minted_raw),
        )
    except Exception:
        try:
            await db.update_wallet_holding(
                user_id, guild_id, dc.CRYPT_NETWORK_SHORT, ore_symbol, int(ore_amount_raw),
            )
        except Exception:
            log.exception(
                "burn_ore_for_rune: refund failed uid=%s gid=%s sym=%s amt=%s",
                user_id, guild_id, ore_symbol, ore_amount_raw,
            )
        raise

    ore_oracle_after  = max(1e-9, ore_oracle_before  * (1.0 - ore_impact))
    rune_oracle_after = max(1e-9, rune_oracle_before * (1.0 + rune_impact))
    try:
        await db.update_price(ore_symbol,    guild_id, ore_oracle_after)
        await db.update_price(dc.RUNE_SYMBOL, guild_id, rune_oracle_after)
    except Exception:
        log.exception(
            "burn_ore_for_rune: oracle update failed gid=%s sym=%s -- chart "
            "will lag until the next drift tick", guild_id, ore_symbol,
        )
    await _write_burn_candle(db, guild_id, ore_symbol,
                             ore_oracle_before, ore_oracle_after, usd_value)
    await _write_burn_candle(db, guild_id, dc.RUNE_SYMBOL,
                             rune_oracle_before, rune_oracle_after, usd_value)

    fee_usd = usd_value * (int(dc.ORE_BURN_LP_REWARD_BPS) / 10_000.0)
    lp_paid = 0.0
    if fee_usd > 0:
        lp_paid += await _distribute_burn_lp_reward(db, guild_id, ore_symbol,    fee_usd / 2.0)
        lp_paid += await _distribute_burn_lp_reward(db, guild_id, dc.RUNE_SYMBOL, fee_usd / 2.0)

    await db.execute(
        """
        UPDATE user_dungeon
           SET total_rune_earned_raw = total_rune_earned_raw + $3::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(rune_minted_raw),
    )
    return BurnResult(
        ore_symbol=ore_symbol,
        ore_burned_raw=int(ore_amount_raw),
        rune_minted_raw=int(rune_minted_raw),
        ore_oracle_before=float(ore_oracle_before),
        ore_oracle_after=float(ore_oracle_after),
        rune_oracle_before=float(rune_oracle_before),
        rune_oracle_after=float(rune_oracle_after),
        price_impact_pct=float(max(ore_impact, rune_impact)),
        lp_reward_usd=float(lp_paid),
    )


async def cashout_rune(
    db: Any, guild_id: int, user_id: int, rune_amount_raw: int,
) -> CashoutResult:
    """Burn RUNE, push the RUNE oracle DOWN, credit the user's USD wallet.
    Mirrors fishing.cashout_reel exactly."""
    if rune_amount_raw <= 0:
        raise ValueError("Amount must be positive.")
    held = await get_rune_wallet_raw(db, guild_id, user_id)
    if held < int(rune_amount_raw):
        raise ValueError(f"You only have {to_human(held):,.4f} RUNE.")

    oracle_before = await _oracle_price(db, guild_id, dc.RUNE_SYMBOL)
    if oracle_before <= 0:
        raise ValueError("RUNE oracle price is currently zero -- try again later.")

    rune_human = to_human(int(rune_amount_raw))
    revenue_usd = rune_human * oracle_before
    supply = await _supply_human(db, guild_id, dc.RUNE_SYMBOL)
    impact = _price_impact(revenue_usd, oracle_before, supply)

    eff_price = oracle_before * (1.0 - impact / 2.0)
    usd_credit_human = rune_human * eff_price

    # Group Industry bonus: members of a group with a dungeon-bonus
    # upgrade (Delve Bastion / Guild Market / Master Industries) earn
    # the bonus on every cashout, anywhere.
    try:
        from services.group_reserve import member_activity_bonus
        _dungeon_bonus = await member_activity_bonus(db, guild_id, user_id, "dungeon")
    except Exception:
        log.debug("group dungeon bonus probe failed", exc_info=True)
        _dungeon_bonus = 0.0
    if _dungeon_bonus > 0:
        usd_credit_human *= (1.0 + _dungeon_bonus)

    usd_credit_raw = to_raw(usd_credit_human)

    await db.update_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL, -int(rune_amount_raw),
    )
    if usd_credit_raw > 0:
        try:
            await db.update_wallet(user_id, guild_id, int(usd_credit_raw))
        except Exception:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL,
                    int(rune_amount_raw),
                )
            except Exception:
                log.exception(
                    "cashout_rune: refund failed uid=%s gid=%s amt=%s",
                    user_id, guild_id, rune_amount_raw,
                )
            raise

    oracle_after = max(1e-9, oracle_before * (1.0 - impact))
    try:
        await db.update_price(dc.RUNE_SYMBOL, guild_id, oracle_after)
    except Exception:
        log.exception(
            "cashout_rune: oracle update failed gid=%s -- chart will lag",
            guild_id,
        )
    await _write_burn_candle(
        db, guild_id, dc.RUNE_SYMBOL, oracle_before, oracle_after, revenue_usd,
    )
    fee_usd = revenue_usd * (int(dc.RUNE_CASHOUT_LP_REWARD_BPS) / 10_000.0)
    lp_paid = 0.0
    if fee_usd > 0:
        lp_paid = await _distribute_burn_lp_reward(
            db, guild_id, dc.RUNE_SYMBOL, fee_usd,
        )

    # Group reserve tribute: system-funded grant on the gross USD value
    # of the cashout. The user's payout is unaffected.
    try:
        from services.group_reserve import tribute_from_activity
        await tribute_from_activity(
            db, guild_id, user_id, float(revenue_usd), "dungeon",
        )
    except Exception:
        log.debug("group dungeon tribute failed", exc_info=True)

    await db.execute(
        """
        UPDATE user_dungeon
           SET total_usd_cashout_raw = total_usd_cashout_raw + $3::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(usd_credit_raw),
    )
    return CashoutResult(
        rune_burned_raw=int(rune_amount_raw),
        usd_credited_raw=int(usd_credit_raw),
        rune_oracle_before=float(oracle_before),
        rune_oracle_after=float(oracle_after),
        price_impact_pct=float(impact),
        revenue_usd=float(revenue_usd),
        lp_reward_usd=float(lp_paid),
    )


# ============================================================================
# Ore staking -> RUNE yield (mirrors fishing's LURE -> REEL stake)
# ============================================================================

_STAKE_COL_BY_ORE: dict[str, str] = {
    dc.COPPER_SYMBOL: "copper_staked_raw",
    dc.SILVER_SYMBOL: "silver_staked_raw",
    dc.GOLD_SYMBOL:   "gold_staked_raw",
}


def _accrue_pending_total(state: dict) -> int:
    """Compute fresh accrued RUNE (raw) across all ore stakes since last_at."""
    last_at = state.get("last_stake_yield_at")
    if not last_at:
        return 0
    if isinstance(last_at, _dt.datetime):
        last_ts = last_at.timestamp()
    else:
        last_ts = float(last_at)
    elapsed = max(0, int(_time.time() - last_ts))
    if elapsed <= 0:
        return 0
    one = to_raw(1.0)
    total_accrued = 0
    for sym in dc.ORE_SYMBOLS:
        staked_raw = int(state.get(_STAKE_COL_BY_ORE[sym]) or 0)
        if staked_raw <= 0:
            continue
        rate_raw = to_raw(dc.ORE_STAKE_RUNE_PER_DAY[sym])
        accrued = (staked_raw * rate_raw * elapsed) // (one * 86400)
        total_accrued += int(accrued)
    return int(total_accrued)


async def accrued_stake_yield(db: Any, guild_id: int, user_id: int) -> int:
    state = await ensure_state(db, guild_id, user_id)
    pending = int(state.get("rune_yield_pending_raw") or 0)
    return pending + _accrue_pending_total(state)


async def stake_ore(
    db: Any, guild_id: int, user_id: int,
    ore_symbol: str, ore_amount_raw: int,
) -> StakeResult:
    """Move ore from wallet -> stake. Crystallises pending RUNE yield first."""
    if ore_symbol not in dc.ORE_SYMBOLS:
        raise ValueError(f"Not an ore: {ore_symbol!r}")
    if ore_amount_raw <= 0:
        raise ValueError("Amount must be positive.")
    state = await ensure_state(db, guild_id, user_id)
    pending = int(state.get("rune_yield_pending_raw") or 0)
    fresh = _accrue_pending_total(state)
    new_pending = pending + fresh

    await db.update_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, ore_symbol, -int(ore_amount_raw),
    )
    col = _STAKE_COL_BY_ORE[ore_symbol]
    cur_staked = int(state.get(col) or 0)
    new_staked = cur_staked + int(ore_amount_raw)
    await db.execute(
        f"""
        UPDATE user_dungeon
           SET {col} = $3::numeric,
               rune_yield_pending_raw = $4::numeric,
               last_stake_yield_at = NOW(),
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_staked), int(new_pending),
    )
    return StakeResult(
        ore_symbol=ore_symbol,
        staked_raw=int(new_staked),
        delta_raw=int(ore_amount_raw),
        rune_yield_paid_raw=0,
        pending_rune_raw=int(new_pending),
    )


async def claim_stake_yield(
    db: Any, guild_id: int, user_id: int,
) -> StakeResult:
    """Pay accrued RUNE to wallet. Stake stays locked. Resets accrual clock."""
    state = await ensure_state(db, guild_id, user_id)
    pending = int(state.get("rune_yield_pending_raw") or 0)
    fresh = _accrue_pending_total(state)
    payout = pending + fresh
    if payout <= 0:
        raise ValueError(
            "No RUNE has accrued yet. Try again after some time has passed."
        )
    await db.update_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL, int(payout),
    )
    # Apply the standard mint-style oracle drop on RUNE so a stake-yield
    # claim moves the chart the same way fresh RUNE drops from the dungeon
    # do. Best-effort: a chart-write hiccup never aborts the upstream
    # credit (the user has already been paid).
    await _apply_mint_oracle_drop(
        db, guild_id, dc.RUNE_SYMBOL, to_human(int(payout)),
    )
    await db.execute(
        """
        UPDATE user_dungeon
           SET rune_yield_pending_raw = 0,
               last_stake_yield_at = NOW(),
               total_rune_earned_raw = total_rune_earned_raw + $3::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(payout),
    )
    # Total stake across the three ores; report whichever is largest as
    # the canonical "staked" number on the receipt for display.
    largest = max(
        int(state.get("copper_staked_raw") or 0),
        int(state.get("silver_staked_raw") or 0),
        int(state.get("gold_staked_raw")   or 0),
    )
    return StakeResult(
        ore_symbol="*",
        staked_raw=int(largest),
        delta_raw=0,
        rune_yield_paid_raw=int(payout),
        pending_rune_raw=0,
    )


async def unstake_ore(
    db: Any, guild_id: int, user_id: int,
    ore_symbol: str, ore_amount_raw: int,
) -> StakeResult:
    """Move ore from stake -> wallet. Crystallises and pays accrued RUNE."""
    if ore_symbol not in dc.ORE_SYMBOLS:
        raise ValueError(f"Not an ore: {ore_symbol!r}")
    state = await ensure_state(db, guild_id, user_id)
    col = _STAKE_COL_BY_ORE[ore_symbol]
    cur_staked = int(state.get(col) or 0)
    pending = int(state.get("rune_yield_pending_raw") or 0)
    fresh = _accrue_pending_total(state)
    payout = pending + fresh
    requested = max(0, int(ore_amount_raw))
    if cur_staked <= 0 or requested <= 0:
        raise ValueError(f"You have no {ore_symbol} staked.")
    actual = min(requested, cur_staked)
    new_staked = cur_staked - actual

    await db.update_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, ore_symbol, int(actual),
    )
    # Returning ore to circulating supply behaves like a mint on the
    # chart -- supply just expanded, so the oracle drops by the standard
    # impact formula. Mirrors how mining ore moves the oracle.
    await _apply_mint_oracle_drop(
        db, guild_id, ore_symbol, to_human(int(actual)),
    )
    if payout > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL, int(payout),
            )
            await _apply_mint_oracle_drop(
                db, guild_id, dc.RUNE_SYMBOL, to_human(int(payout)),
            )
        except Exception:
            log.exception(
                "unstake_ore: RUNE payout failed uid=%s gid=%s sym=%s",
                user_id, guild_id, ore_symbol,
            )
            payout = 0
    await db.execute(
        f"""
        UPDATE user_dungeon
           SET {col} = $3::numeric,
               rune_yield_pending_raw = 0,
               last_stake_yield_at = NOW(),
               total_rune_earned_raw = total_rune_earned_raw + $4::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_staked), int(payout),
    )
    return StakeResult(
        ore_symbol=ore_symbol,
        staked_raw=int(new_staked),
        delta_raw=-int(actual),
        rune_yield_paid_raw=int(payout),
        pending_rune_raw=0,
    )


# ============================================================================
# Items: buy / equip / use consumable
# ============================================================================

async def buy_item(
    db: Any, guild_id: int, user_id: int, kind: str, key: str,
    *, qty: int = 1,
) -> BuyResult:
    """Buy a weapon, armor, or consumable for RUNE. Spending applies the
    same burn-style oracle drop fishing's gear spend uses.

    ``qty`` is the number of consumable units to purchase in one go
    (weapons + armor are always qty=1). Each unit's pack_size (e.g.
    20 arrows per Arrow Bundle) is honored: buying 1 Arrow Bundle now
    grants 20 arrows, buying 2 grants 40, etc. Total RUNE spend is
    ``price_rune * qty``.
    """
    if kind not in ("weapon", "armor", "consumable"):
        raise ValueError(f"bad kind: {kind!r}")
    qty = max(1, int(qty))
    if kind in ("weapon", "armor"):
        qty = 1   # weapons + armor are unique-owned, no stacking
    catalog = (
        dc.WEAPONS if kind == "weapon"
        else dc.ARMOR if kind == "armor"
        else dc.CONSUMABLES
    )
    meta = catalog.get(key)
    if not meta:
        raise ValueError(f"Unknown {kind}: {key!r}")
    if meta.get("delve_only"):
        raise ValueError(
            f"**{meta.get('name') or key}** can only be found inside the "
            f"delve -- chests, mini-bosses, and bosses drop it."
        )
    unit_price = float(meta.get("price_rune") or 0.0)
    if unit_price <= 0 and kind in ("weapon", "armor"):
        raise ValueError("That gear is starter-tier (already owned).")
    price = unit_price * qty

    state = await ensure_state(db, guild_id, user_id)
    if kind == "weapon":
        owned = dict(state.get("weapons_owned") or {})
        if key in owned:
            raise ValueError("You already own that weapon.")
    elif kind == "armor":
        owned = dict(state.get("armor_owned") or {})
        if key in owned:
            raise ValueError("You already own that armor.")

    rune_oracle_before = await _oracle_price(db, guild_id, dc.RUNE_SYMBOL)
    price_raw = to_raw(price)
    held = await get_rune_wallet_raw(db, guild_id, user_id)
    if held < int(price_raw):
        raise ValueError(
            f"Not enough RUNE: need {price:,.4f}, have {to_human(held):,.4f}."
        )

    # Burn the RUNE (negative delta on EARN_ONLY token decrements supply).
    await db.update_wallet_holding(
        user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL, -int(price_raw),
    )

    # Add the item to inventory.
    if kind == "weapon":
        owned = dict(state.get("weapons_owned") or {})
        owned[key] = 1
        await db.execute(
            "UPDATE user_dungeon SET weapons_owned = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(owned),
        )
    elif kind == "armor":
        owned = dict(state.get("armor_owned") or {})
        owned[key] = 1
        await db.execute(
            "UPDATE user_dungeon SET armor_owned = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(owned),
        )
    else:
        cons = dict(state.get("consumables") or {})
        # Honor pack_size on ammo bundles + similar bulk consumables.
        # Without this, "Arrow Bundle (20 arrows)" was granting 1 arrow
        # instead of 20.
        pack_size = max(1, int(meta.get("pack_size") or 1))
        granted_units = pack_size * qty
        cons[key] = int(cons.get(key, 0)) + granted_units
        await db.execute(
            "UPDATE user_dungeon SET consumables = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(cons),
        )

    # NFT layer sync: mint one token per purchased unit. Best-effort.
    try:
        from services import items as _items
        await _items.mint_unit(
            db,
            guild_id=guild_id,
            contract_address=_items.contract_address(kind, str(key)),
            owner_user_id=user_id,
            metadata={"catalog_key": str(key), "purchased_for_rune": float(price)},
            mint_source=f"dungeon.buy.{kind}",
            source_table=(
                "user_dungeon.weapons_owned" if kind == "weapon"
                else "user_dungeon.armor_owned" if kind == "armor"
                else "user_dungeon.consumables"
            ),
            source_id=f"{user_id}:{key}:{int(__import__('time').time())}",
        )
    except Exception:
        log.debug(
            "nft dungeon buy mint sync failed gid=%s uid=%s key=%s",
            guild_id, user_id, key, exc_info=True,
        )

    # Burn-style oracle drop on RUNE for the spent supply.
    ob, oa, impact = await _apply_mint_oracle_drop(
        db, guild_id, dc.RUNE_SYMBOL, 0.0,
    ) if False else (rune_oracle_before, rune_oracle_before, 0.0)
    # Spending tokens = burn (deflationary). Apply the impact + LP reward
    # the same way fishing's gear-spend does.
    try:
        oracle_before = rune_oracle_before
        usd_value = price * oracle_before
        supply = await _supply_human(db, guild_id, dc.RUNE_SYMBOL)
        impact = _price_impact(usd_value, oracle_before, supply)
        oracle_after = max(1e-9, oracle_before * (1.0 - impact))
        await db.update_price(dc.RUNE_SYMBOL, guild_id, oracle_after)
        await _write_burn_candle(
            db, guild_id, dc.RUNE_SYMBOL,
            oracle_before, oracle_after, usd_value,
        )
        fee_usd = usd_value * (int(dc.ORE_BURN_LP_REWARD_BPS) / 10_000.0)
        if fee_usd > 0:
            await _distribute_burn_lp_reward(db, guild_id, dc.RUNE_SYMBOL, fee_usd)
        ob, oa = float(oracle_before), float(oracle_after)
    except Exception:
        log.exception("buy_item burn-effect failed gid=%s key=%s", guild_id, key)

    return BuyResult(
        kind=kind, key=key, price_rune_human=float(price),
        impact_pct=float(impact), oracle_before=float(ob), oracle_after=float(oa),
    )


async def equip_item(
    db: Any, guild_id: int, user_id: int, kind: str, key: str,
) -> dict:
    """Equip a weapon or armor the user already owns.

    Enforces class compatibility via ``dc.weapon_allowed_for_class`` /
    ``dc.armor_allowed_for_class`` so an Archer can't suddenly wear plate
    or a Mage can't wield a longsword. Mismatches raise ValueError with a
    human-readable hint pointing at the class's allowed types.
    """
    if kind not in ("weapon", "armor"):
        raise ValueError(f"bad kind: {kind!r}")
    state = await ensure_state(db, guild_id, user_id)
    class_key = state.get("class_key") or ""
    if not class_key:
        raise ValueError("Pick a class first with `,delve class <warrior|mage|rogue|archer|druid>`.")
    if kind == "weapon":
        if key not in (state.get("weapons_owned") or {}):
            raise ValueError("You don't own that weapon.")
        wmeta = dc.weapon_meta(key)
        if not wmeta:
            raise ValueError(f"Unknown weapon: {key!r}")
        if not dc.weapon_allowed_for_class(key, class_key):
            wt = str(wmeta.get("weapon_type") or "?")
            allowed = ", ".join(dc.class_weapon_types(class_key)) or "none"
            raise ValueError(
                f"A {dc.class_meta(class_key).get('name', class_key)} can't wield "
                f"a **{wmeta.get('name', key)}** ({wt}). Allowed: {allowed}."
            )
        await db.execute(
            "UPDATE user_dungeon SET equipped_weapon = $3, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, key,
        )
    else:
        if key not in (state.get("armor_owned") or {}):
            raise ValueError("You don't own that armor.")
        ameta = dc.armor_meta(key)
        if not ameta:
            raise ValueError(f"Unknown armor: {key!r}")
        if not dc.armor_allowed_for_class(key, class_key):
            at = str(ameta.get("armor_type") or "?")
            allowed = ", ".join(dc.class_armor_types(class_key)) or "none"
            raise ValueError(
                f"A {dc.class_meta(class_key).get('name', class_key)} can't wear "
                f"**{ameta.get('name', key)}** ({at} armor). Allowed: {allowed}."
            )
        await db.execute(
            "UPDATE user_dungeon SET equipped_armor = $3, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, key,
        )
    return await list_state(db, guild_id, user_id)


# ============================================================================
# Shrine pray
# ============================================================================

async def pray_at_shrine(
    db: Any, guild_id: int, user_id: int,
) -> ShrineResult:
    """Activate the shrine in the current room. Rolls a random boon from
    SHRINE_OUTCOME_WEIGHTS and applies it to the player. Clears the room
    so the shrine can't be re-prayed; bumps total_shrines_visited."""
    state = await list_state(db, guild_id, user_id)
    if state.get("current_room_type") != "shrine":
        raise ValueError("No shrine here. Use `,delve next` to keep delving.")
    floor = int(state.get("current_floor") or 1)
    rng = random.Random()
    outcome = dc.roll_shrine_outcome(rng)
    boon = dc.shrine_meta(outcome) or {}
    kind = str(boon.get("kind") or "")
    hp_delta = 0
    rune_credited = 0.0
    buff_key: str | None = None
    buff_value = 0.0
    buff_duration = 0
    relic_key: str | None = None

    cur_hp = int(state.get("current_hp") or 0)
    hp_max = int(state.get("hp_max") or dc.STARTING_HP)
    new_hp = cur_hp
    buffs = dict(state.get("player_buffs") or {})
    relics_owned = _as_dict(state.get("relics_owned"))

    if kind == "heal_full":
        new_hp = hp_max
        hp_delta = new_hp - cur_hp
    elif kind == "rune":
        lo = float(boon.get("amount_min") or 5.0)
        hi = float(boon.get("amount_max") or 30.0)
        depth_scale = float(boon.get("depth_scale") or 0.0)
        rune_credited = round(rng.uniform(lo, hi) * (1.0 + depth_scale * floor), 2)
        if rune_credited > 0:
            await _credit_rune(
                db, guild_id, user_id, rune_credited,
                int(state.get("run_id") or 0),
            )
    elif kind == "buff":
        buff_key = str(boon.get("buff_key") or "shrine_atk")
        buff_value = float(boon.get("value") or 0.20)
        buff_duration = int(boon.get("duration") or 5)
        buffs[buff_key] = {
            "value":    buff_value,
            "duration": buff_duration,
        }
    elif kind == "relic":
        relic_key = dc.roll_relic(max(int(boon.get("min_floor") or 1), floor + 5), rng)
        # If the depth-gated roll missed, fall back to a guaranteed
        # common relic so the gift always feels real.
        if not relic_key:
            common_pool = [k for k, v in dc.RELICS.items() if v.get("rarity") == "common"]
            if common_pool:
                relic_key = rng.choice(common_pool)
        if relic_key:
            relics_owned[relic_key] = int(relics_owned.get(relic_key, 0) or 0) + 1
    elif kind == "curse":
        hp_cost_pct = float(boon.get("hp_cost_pct") or 0.25)
        cost = max(1, int(round(hp_max * hp_cost_pct)))
        new_hp = max(1, cur_hp - cost)
        hp_delta = new_hp - cur_hp
        # Stamp a "shrine_debt" buff that mining / chest paths can read
        # for a one-shot kicker. Mirrors the marked_target shape.
        buffs["shrine_debt"] = {"value": 2.0, "duration": 3}

    await db.execute(
        """
        UPDATE user_dungeon
           SET current_hp        = $3,
               current_room_type = 'empty',
               current_room_payload = NULL,
               player_buffs      = $4::jsonb,
               relics_owned      = $5::jsonb,
               total_shrines_visited = total_shrines_visited + 1,
               last_action_at    = NOW(),
               updated_at        = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_hp), _json(buffs), _json(relics_owned),
    )
    return ShrineResult(
        outcome_key=outcome,
        boon_name=str(boon.get("name") or outcome.replace("_", " ").title()),
        blurb=str(boon.get("blurb") or ""),
        hp_delta=int(hp_delta),
        rune_credited=float(rune_credited),
        buff_key=buff_key,
        buff_value=float(buff_value),
        buff_duration=int(buff_duration),
        relic_key=relic_key,
    )


# ============================================================================
# Relic equip / list
# ============================================================================

async def equip_relic(
    db: Any, guild_id: int, user_id: int, key: str | None,
) -> RelicEquipResult:
    """Equip a relic the user owns, or unequip with ``key=None``."""
    state = await ensure_state(db, guild_id, user_id)
    previous = state.get("equipped_relic") or None
    if key is None or str(key).lower() in ("none", "off", "clear", "remove"):
        await db.execute(
            "UPDATE user_dungeon SET equipped_relic = NULL, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        return RelicEquipResult(equipped_key=None, previous_key=previous)
    target = str(key).lower()
    if not dc.relic_meta(target):
        raise ValueError(f"Unknown relic: {key!r}")
    relics_owned = _as_dict(state.get("relics_owned"))
    if int(relics_owned.get(target, 0) or 0) <= 0:
        raise ValueError(
            f"You don't own a {dc.relic_meta(target).get('name', target)}. "
            f"Open chests on deeper floors to find one."
        )
    await db.execute(
        "UPDATE user_dungeon SET equipped_relic = $3, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, target,
    )
    return RelicEquipResult(equipped_key=target, previous_key=previous)


async def list_relics(
    db: Any, guild_id: int, user_id: int,
) -> tuple[dict, str | None]:
    """Return ``(relics_owned, equipped_key)`` for the player."""
    state = await ensure_state(db, guild_id, user_id)
    return _as_dict(state.get("relics_owned")), state.get("equipped_relic") or None


# ============================================================================
# Cursed runs: opt-in modifier toggled before start_run
# ============================================================================

async def set_run_curse(
    db: Any, guild_id: int, user_id: int, key: str | None,
) -> CurseSetResult:
    """Toggle the active run curse. Refuses while a run is in progress so
    the modifier can't change mid-floor; clear with ``key=None`` when the
    player wants to opt out before they `,delve start`."""
    state = await ensure_state(db, guild_id, user_id)
    if state.get("run_id"):
        raise ValueError(
            "You're already mid-delve. `,delve rest` first, then set a curse."
        )
    previous = state.get("run_curse") or None
    if key is None or str(key).lower() in ("none", "off", "clear", "remove"):
        await db.execute(
            "UPDATE user_dungeon SET run_curse = NULL, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        return CurseSetResult(curse_key=None, previous_key=previous)
    target = str(key).lower()
    if not dc.curse_meta(target):
        raise ValueError(f"Unknown curse: {key!r}")
    await db.execute(
        "UPDATE user_dungeon SET run_curse = $3, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, target,
    )
    return CurseSetResult(curse_key=target, previous_key=previous)


async def use_junk_item(
    db: Any, guild_id: int, user_id: int, junk_key: str,
) -> ConsumableResult:
    """Apply a usable junk item's effect (heal / escape / buff / ammo).

    Salvage and craft mats raise -- only ``kind == 'usable'`` entries
    can be used. Heal applies the same in-place HP bump the consumable
    path uses; escape mirrors smoke-bomb / scroll_escape; buff_crit
    drops a ``shrine_atk`` -shaped buff into player_buffs (so the
    combat resolver picks it up without new wiring); ammo refills
    consumables[ammo_key]. Burns one unit on success.
    """
    meta = dc.junk_meta(junk_key)
    if not meta:
        raise ValueError(f"Unknown junk item: {junk_key!r}")
    kind = str(meta.get("kind") or "salvage")
    if kind != "usable":
        raise ValueError(
            f"{meta.get('name', junk_key)} can't be used -- sell it with "
            f"`,delve sell junk` instead."
        )
    state = await ensure_state(db, guild_id, user_id)
    junk_inv = _as_dict(state.get("junk_inventory"))
    have = int(junk_inv.get(junk_key, 0) or 0)
    if have <= 0:
        raise ValueError(f"You don't have any {meta.get('name', junk_key)}.")
    use_kind = str(meta.get("use_kind") or "")
    use_value = float(meta.get("use_value") or 0.0)
    detail = ""
    new_hp = int(state.get("current_hp") or 0)
    hp_max = int(state.get("hp_max") or dc.STARTING_HP)
    consumables = dict(state.get("consumables") or {})
    buffs = dict(state.get("player_buffs") or {})

    if use_kind == "heal":
        # Famine curse blocks healing here too -- consistent with
        # use_consumable's potion gate.
        if dc.curse_mult(state.get("run_curse"), "block_potions", 0.0) > 0:
            raise ValueError(
                "The Famine curse forbids healing items. Sell or save it."
            )
        amt = max(1, int(round(hp_max * use_value)))
        new_hp = min(hp_max, new_hp + amt)
        detail = f"Healed for {amt} HP."
    elif use_kind == "escape":
        if not state.get("current_mob_state"):
            raise ValueError("Nothing to escape from.")
        await db.execute(
            "UPDATE user_dungeon "
            "SET current_mob_state = NULL, current_room_type = 'empty', "
            "    updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        detail = "You vanish in a puff of white smoke."
    elif use_kind == "buff_crit":
        # Stash as a shrine_atk-style buff so the combat resolver picks
        # it up via the existing buff path. Field name ``shrine_spd`` is
        # what gates the SPD bonus; we re-purpose it as a generic
        # crit/SPD bump for the duration window.
        dur = int(meta.get("use_duration") or 5)
        buffs["shrine_spd"] = {"value": float(use_value), "duration": dur}
        await db.execute(
            "UPDATE user_dungeon SET player_buffs = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(buffs),
        )
        detail = f"+{int(use_value * 100)}% effective speed/crit for {dur} rounds."
    elif use_kind == "ammo":
        ammo_key = str(meta.get("ammo_key") or "")
        if not ammo_key:
            raise ValueError("Junk usable has no ammo_key.")
        qty = int(use_value)
        consumables[ammo_key] = int(consumables.get(ammo_key, 0) or 0) + qty
        await db.execute(
            "UPDATE user_dungeon SET consumables = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(consumables),
        )
        detail = f"+{qty} {ammo_key.replace('_', ' ')}."
    else:
        raise ValueError(f"Unknown junk use_kind: {use_kind!r}")

    junk_inv[junk_key] = have - 1
    if junk_inv[junk_key] <= 0:
        junk_inv.pop(junk_key, None)
    await db.execute(
        "UPDATE user_dungeon SET junk_inventory = $3::jsonb, "
        "current_hp = $4, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(junk_inv), int(new_hp),
    )
    return ConsumableResult(
        key=junk_key, kind=str(use_kind), consumed=True, detail=detail,
        player_hp=new_hp, player_max_hp=hp_max,
    )


async def sell_junk(
    db: Any, guild_id: int, user_id: int,
    key: str | None = None,
    *,
    salvage_only: bool = False,
) -> tuple[float, dict]:
    """Burn the player's junk inventory for RUNE.

    ``key`` sells one specific junk item (``salvage`` / ``mat`` /
    ``usable`` -- usable items can also be sold for their salvage
    rune value rather than used). ``None`` sells the player's
    junk inventory at once.

    When ``salvage_only=True`` the bulk sell path skips ``mat`` and
    ``usable`` kinds entirely. This is the policy for the unified
    ``,delve sell all`` command -- only "true junk" gets dumped, while
    crafting mats and usable charms / potions stay in the bag for the
    player to spend or sell individually.

    Returns ``(total_rune, sold_dict)``.
    """
    state = await ensure_state(db, guild_id, user_id)
    junk_inv = _as_dict(state.get("junk_inventory"))
    if not junk_inv:
        raise ValueError("Your junk inventory is empty.")
    sold: dict[str, int] = {}
    if key:
        k = str(key).lower()
        if int(junk_inv.get(k, 0) or 0) <= 0:
            raise ValueError(f"You don't have any {k}.")
        sold[k] = int(junk_inv.pop(k))
    else:
        for k, qty in list(junk_inv.items()):
            if int(qty or 0) <= 0:
                continue
            if salvage_only:
                kind = str((dc.junk_meta(k) or {}).get("kind") or "salvage")
                if kind != "salvage":
                    continue
            sold[k] = int(qty)
            junk_inv.pop(k, None)
        if salvage_only and not sold:
            raise ValueError(
                "No salvage to sell -- your junk is all crafting mats and "
                "usables. Sell those one at a time with "
                "`,delve junk sell <key>`."
            )
    total_rune_human = 0.0
    for k, qty in sold.items():
        meta = dc.junk_meta(k) or {}
        # Rarity-multiplied salvage rate so a "rare" Glowing Crystal
        # stack pays out more than the same count of common torn cloth.
        rune_per = float(dc.effective_salvage_rune(meta))
        total_rune_human += rune_per * qty
    if total_rune_human > 0:
        await db.update_wallet_holding(
            user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL,
            int(to_raw(total_rune_human)),
        )
    await db.execute(
        "UPDATE user_dungeon SET junk_inventory = $3::jsonb, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(junk_inv),
    )
    return float(total_rune_human), sold


_GEAR_SELL_RATE: float = 0.50


async def sell_gear(
    db: Any, guild_id: int, user_id: int,
    kind: str, key: str,
) -> tuple[float, str]:
    """Sell an owned weapon or armor back for 50% of its RUNE price.

    Returns ``(rune_refunded_human, item_name)``.
    Raises ``ValueError`` on bad input or if the item is currently equipped.
    """
    if kind == "weapon":
        catalog = dc.WEAPONS
        inv_col = "weapons_owned"
        equip_col = "equipped_weapon"
    elif kind == "armor":
        catalog = dc.ARMOR
        inv_col = "armor_owned"
        equip_col = "equipped_armor"
    else:
        raise ValueError(f"Can only sell weapons or armor, not {kind!r}.")

    meta = catalog.get(key)
    if not meta:
        raise ValueError(f"Unknown {kind}: {key!r}")

    # Single source of truth for the sell value: ``gear_sell_value``
    # handles both shop-bought (50% of price_rune) and delve-only
    # drops (synthesised from tier + rarity since they have no
    # listed price).
    refund = float(dc.gear_sell_value(meta))
    if refund <= 0:
        raise ValueError(f"**{meta['name']}** is a starter item and has no sell value.")

    state = await ensure_state(db, guild_id, user_id)
    owned = _as_dict(state.get(inv_col))
    if not owned.get(key):
        raise ValueError(f"You don't own **{meta['name']}**.")

    if str(state.get(equip_col) or "") == key:
        raise ValueError(
            f"**{meta['name']}** is currently equipped. Swap to a different "
            f"{kind} first, then sell."
        )

    refund_raw = int(to_raw(refund))

    owned.pop(key, None)
    await db.execute(
        f"UPDATE user_dungeon SET {inv_col} = $3::jsonb, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(owned),
    )
    if refund_raw > 0:
        await db.update_wallet_holding(
            user_id, guild_id, dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL, refund_raw,
        )
    return refund, str(meta.get("name") or key)


async def use_consumable(
    db: Any, guild_id: int, user_id: int, key: str,
) -> ConsumableResult:
    """Apply a consumable's effect. Heals adjust HP; escape ends combat;
    charm / mine_boost / lure stay banked in the consumables JSONB until
    consumed by the action they affect."""
    meta = dc.consumable_meta(key)
    if not meta:
        raise ValueError(f"Unknown consumable: {key!r}")
    state = await ensure_state(db, guild_id, user_id)
    cons = dict(state.get("consumables") or {})
    qty = int(cons.get(key) or 0)
    if qty <= 0:
        raise ValueError(f"You don't have any {meta['name']}.")
    kind = str(meta["kind"])
    detail = ""
    new_hp = int(state.get("current_hp") or 0)
    hp_max = int(state.get("hp_max") or dc.STARTING_HP)
    # Mutable copy of player_buffs so the heal / damage branches can
    # stamp their own 2-round cooldowns ("potion_cd" / "scroll_cd")
    # before we land them in the final UPDATE. _tick_player_buffs in
    # resolve_combat decrements duration each round so the cooldown
    # naturally lifts after two combat actions without any extra
    # bookkeeping in this module.
    buffs = dict(state.get("player_buffs") or {})

    # Famine curse: blocks healing potions outright. Other consumable
    # kinds (charms, oils, scrolls, ammo) still work because the curse
    # only restricts in-combat HP recovery.
    if kind == "heal" and dc.curse_mult(state.get("run_curse"), "block_potions", 0.0) > 0:
        raise ValueError(
            "The Famine curse forbids potions. Heal naturally or break the curse first."
        )

    # 2-round potion / scroll cooldowns: prevents spamming heals or
    # damage scrolls back-to-back during combat. The CD ticks down
    # alongside other buffs in resolve_combat's _tick_player_buffs.
    if kind == "heal":
        cur_cd = int((buffs.get("potion_cd") or {}).get("duration") or 0)
        if cur_cd > 0:
            raise ValueError(
                f"Potions are on cooldown for **{cur_cd}** more round"
                f"{'s' if cur_cd != 1 else ''}."
            )
    if kind == "damage":
        cur_cd = int((buffs.get("scroll_cd") or {}).get("duration") or 0)
        if cur_cd > 0:
            raise ValueError(
                f"Attack scrolls are on cooldown for **{cur_cd}** more round"
                f"{'s' if cur_cd != 1 else ''}."
            )

    if kind == "heal":
        amt = max(1, int(round(hp_max * float(meta["value"]))))
        new_hp = min(hp_max, new_hp + amt)
        detail = f"Healed for {amt} HP."
        # Stamp the 2-round potion CD. _tick_player_buffs runs at the
        # end of every combat action (resolve_attack), so duration=2
        # forces two attack/skill/flee actions before the next heal
        # is allowed.
        buffs["potion_cd"] = {
            "duration": 2, "value": 0.0, "source": str(key),
        }
    elif kind == "escape":
        if not state.get("current_mob_state"):
            raise ValueError("Nothing to escape from.")
        await db.execute(
            "UPDATE user_dungeon "
            "SET current_mob_state = NULL, current_room_type = 'empty', "
            "    updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        detail = "You vanish in a puff of parchment smoke."
    elif kind in ("charm", "mine_boost", "lure", "revive"):
        # Banked items: they stay in cons until consumed by the appropriate
        # action (capture / mine / advance_room / fatal-hit auto-revive).
        # Only bookkeep on the consumables count here -- the "use" is when
        # the action fires.
        if kind == "revive":
            detail = (
                f"{meta['name']} primed -- on KO you'll auto-revive at "
                f"{int(float(meta.get('value') or 0.5) * 100)}% HP."
            )
        else:
            detail = f"{meta['name']} primed for next action."
        # Don't decrement: the action itself decrements when it fires.
        return ConsumableResult(
            key=key, kind=kind, consumed=False, detail=detail,
            player_hp=new_hp, player_max_hp=hp_max,
        )
    elif kind == "damage":
        # Damage scroll: deals ``value * player_atk`` to the active mob.
        # Uses the player's current effective ATK so a maxed Mage hits
        # WAY harder than a fresh Warrior. The mob still gets to swing
        # afterwards if it survives -- scrolls are a tempo tool, not an
        # auto-win, except for ``scroll_apocalypse`` / ``scroll_unmake``
        # which are usually overkill at the floors they're priced for.
        if not state.get("current_mob_state"):
            raise ValueError("Nothing to target.")
        # Stamp the 2-round scroll CD up-front so a kill that returns
        # early below still leaves the cooldown in place. _tick_player_buffs
        # in resolve_combat decrements duration each combat action.
        buffs["scroll_cd"] = {
            "duration": 2, "value": 0.0, "source": str(key),
        }
        mob = dict(state.get("current_mob_state") or {})
        pstats = player_combat_stats(state)
        mult = float(meta.get("value") or 1.0)
        # Damage formula: player ATK * mult, mob def 0.5 mitigation
        # mirrors the regular swing math so damage feels consistent.
        raw_dmg = float(pstats["atk"]) * mult
        mitigated = max(1, int(round(raw_dmg - float(mob.get("def") or 0) * 0.5)))
        mob["hp"] = max(0, int(mob.get("hp") or 0) - mitigated)
        mob_meta = dc.mob_meta(str(mob.get("key") or "")) or {}
        mob_name = mob_meta.get("name") or mob.get("key") or "the mob"
        if mob["hp"] <= 0:
            # The scroll killed the mob -- mirror the kill bookkeeping
            # path used by resolve_attack so XP / drops / oracle moves
            # all fire. credit_combat_drops + _award_kill is intentionally
            # scoped to the same place to keep the math single-source.
            xp_gain, leveled, new_level = await _award_kill(
                db, guild_id, user_id, mob, captured=False,
            )
            mob_meta_full = dc.mob_meta(str(mob.get("key") or "")) or {}
            res = CombatResult(
                outcome="mob_dead", log=[
                    f"You unfurl the {meta['name']} -- {mob_name} takes "
                    f"**{mitigated}** damage and is destroyed!"
                ],
                mob_state=None,
                player_hp=int(state.get("current_hp") or 0),
                player_max_hp=int(state.get("hp_max") or 0),
                mob_xp=xp_gain,
                rune_drop_human=float(mob_meta_full.get("rune_drop") or 0),
                ore_drop_symbol=mob_meta_full.get("ore_drop"),
                ore_drop_qty_human=float(mob_meta_full.get("ore_qty") or 0.0),
                leveled_up=leveled, new_level=new_level,
                boss_kill=bool(mob.get("boss")),
            )
            await credit_combat_drops(db, guild_id, user_id, res)
            await db.execute(
                "UPDATE user_dungeon "
                "SET current_mob_state = NULL, current_room_type = 'empty', "
                "    updated_at = NOW() "
                "WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id,
            )
            detail = (
                f"✨ {meta['name']} hits **{mob_name}** for "
                f"**{mitigated}** dmg -- mob destroyed!"
            )
        else:
            await db.execute(
                "UPDATE user_dungeon "
                "SET current_mob_state = $3::jsonb, "
                "    updated_at = NOW() "
                "WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id, _json(mob),
            )
            detail = (
                f"✨ {meta['name']} hits **{mob_name}** for "
                f"**{mitigated}** dmg ({mob['hp']}/{mob.get('max_hp', '?')} HP left)."
            )
    elif kind == "buff":
        # Apply / refresh a player buff with a duration. Buffs are
        # additive across types but the same buff name overwrites with
        # the longer duration so a stacked re-cast doesn't clip itself.
        buff_name = str(meta.get("buff") or "")
        if not buff_name:
            raise ValueError(f"{meta['name']} has no buff name in catalog.")
        buffs = dict(state.get("player_buffs") or {})
        existing_dur = int((buffs.get(buff_name) or {}).get("duration") or 0)
        new_dur = max(existing_dur, int(meta.get("duration_rounds") or 1))
        buffs[buff_name] = {
            "duration": new_dur,
            "value": float(meta.get("value") or 0.0),
            "source": str(key),
        }
        await db.execute(
            "UPDATE user_dungeon SET player_buffs = $3::jsonb, "
            "    updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(buffs),
        )
        detail = (
            f"{meta['name']} active for **{new_dur}** round(s) -- "
            f"{meta.get('blurb', '')}"
        )
    elif kind == "regen":
        buffs = dict(state.get("player_buffs") or {})
        new_dur = max(
            int((buffs.get("regen") or {}).get("duration") or 0),
            int(meta.get("duration_rounds") or 1),
        )
        buffs["regen"] = {
            "duration": new_dur,
            "value": float(meta.get("value") or 0.10),
            "source": str(key),
        }
        await db.execute(
            "UPDATE user_dungeon SET player_buffs = $3::jsonb, "
            "    updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(buffs),
        )
        detail = (
            f"{meta['name']} active -- regen "
            f"{int(float(meta.get('value') or 0.10) * 100)}% HP / round "
            f"for **{new_dur}** round(s)."
        )
    elif kind == "skill_reset":
        # Clears the class-skill cooldown immediately.
        await db.execute(
            "UPDATE user_dungeon SET skill_cd_remaining = 0, "
            "    updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        detail = f"{meta['name']} clears your class-skill cooldown."
    elif kind == "ammo":
        # Ammo is consumed automatically per ranged swing inside
        # resolve_attack -- direct ,delve use on it is a no-op error
        # (would silently delete a stack). Tell the player to just go
        # shoot something.
        raise ValueError(
            f"{meta['name']} is auto-consumed when you fire a ranged shot. "
            f"You don't need to ,delve use it."
        )
    else:
        raise ValueError(f"Unknown consumable kind: {kind!r}")

    cons[key] = qty - 1
    if cons[key] <= 0:
        cons.pop(key, None)
    # player_buffs is in the final UPDATE so the heal / damage branches'
    # potion_cd / scroll_cd cooldowns persist alongside the consumables
    # bookkeeping. Branches that already wrote buffs themselves (buff /
    # regen / skill_reset) pass through unchanged because they mutated
    # the same ``buffs`` dict before reaching this point.
    await db.execute(
        """
        UPDATE user_dungeon
           SET consumables  = $3::jsonb,
               current_hp   = $4,
               player_buffs = $5::jsonb,
               last_action_at = NOW(),
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(cons), int(new_hp), _json(buffs),
    )
    return ConsumableResult(
        key=key, kind=kind, consumed=True, detail=detail,
        player_hp=new_hp, player_max_hp=hp_max,
    )


async def spend_stat_points(
    db: Any, guild_id: int, user_id: int,
    *, hp: int = 0, atk: int = 0, spd: int = 0, int_: int = 0,
) -> dict:
    """Allocate (hp, atk, spd, int_) stat points against the user's pool.

    Refuses if the requested spend exceeds the unspent points
    (level * STAT_POINTS_PER_LEVEL minus current allocations). All four
    counters are sticky -- they persist across class reroll, equip
    change, and run lifecycle. Per-point payoffs live in
    ``dungeon_config.STAT_POINT_*_BONUS``.
    """
    hp = max(0, int(hp or 0))
    atk = max(0, int(atk or 0))
    spd = max(0, int(spd or 0))
    int_ = max(0, int(int_ or 0))
    total = hp + atk + spd + int_
    if total <= 0:
        raise ValueError("Pass at least one positive amount.")

    state = await ensure_state(db, guild_id, user_id)
    avail = dc.stat_points_available(
        int(state.get("level") or 1),
        int(state.get("hp_alloc") or 0),
        int(state.get("atk_alloc") or 0),
        int(state.get("spd_alloc") or 0),
        int(state.get("int_alloc") or 0),
    )
    if total > avail:
        raise ValueError(
            f"You only have {avail} unspent point(s) -- you tried to spend {total}."
        )

    # Recompute hp_max first so a Hardiness spend grows the player's
    # max + heals them by the same delta (mirrors ,buddy upgrade).
    cmeta = dc.class_meta(state.get("class_key") or "warrior") or dc.CLASSES["warrior"]
    new_hp_alloc = int(state.get("hp_alloc") or 0) + hp
    new_hp_max = max(1, int(round(
        dc.STARTING_HP * float(cmeta["hp_mult"])
        + int(state.get("level") or 1) * dc.HP_PER_LEVEL
        + new_hp_alloc * dc.STAT_POINT_HP_BONUS
    )))
    cur_hp = int(state.get("current_hp") or 0)
    grew_hp_by = new_hp_max - int(state.get("hp_max") or new_hp_max)
    new_cur_hp = max(0, min(new_hp_max, cur_hp + max(0, grew_hp_by)))

    await db.execute(
        """
        UPDATE user_dungeon
           SET hp_alloc  = hp_alloc  + $3,
               atk_alloc = atk_alloc + $4,
               spd_alloc = spd_alloc + $5,
               int_alloc = int_alloc + $6,
               hp_max    = $7,
               current_hp = $8,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, hp, atk, spd, int_, int(new_hp_max), int(new_cur_hp),
    )
    return await list_state(db, guild_id, user_id)


async def respec_stat_points(
    db: Any, guild_id: int, user_id: int,
) -> tuple[dict, float, int]:
    """Refund every spent stat point back to the available pool for USD.

    Zeroes ``hp_alloc / atk_alloc / spd_alloc / int_alloc`` so the player
    can reallocate from scratch via ``,delve upgrade``. Cost doubles per
    respec on the same delver (``dc.respec_cost_usd``). Charges wallet
    first then bank, refusing if combined liquid balance is short.
    Refused mid-run so a mid-fight stat panic can't be papered over with
    a respec.

    Returns ``(new_state, cost_usd, refunded_points)``.
    """
    state = await ensure_state(db, guild_id, user_id)
    if state.get("run_id"):
        raise ValueError(
            "Finish or `,delve rest` first -- can't respec mid-run."
        )

    hp_a = int(state.get("hp_alloc") or 0)
    at_a = int(state.get("atk_alloc") or 0)
    sp_a = int(state.get("spd_alloc") or 0)
    in_a = int(state.get("int_alloc") or 0)
    refunded = hp_a + at_a + sp_a + in_a
    if refunded <= 0:
        raise ValueError(
            "You have no spent stat points to refund yet -- "
            "earn some by levelling, then `,delve upgrade` to spend them."
        )

    respecs_used = int(state.get("stat_respecs_used") or 0)
    cost_usd = dc.respec_cost_usd(respecs_used)
    cost_raw = to_raw(cost_usd)

    # Charge wallet then bank atomically; refuse if combined is short.
    paid = await db.fetch_val(
        """
        UPDATE users
           SET wallet = GREATEST(0, wallet - $1),
               bank   = bank - GREATEST(0, $1 - wallet)
         WHERE user_id = $2 AND guild_id = $3
           AND (wallet + bank) >= $1
        RETURNING 1
        """,
        cost_raw, user_id, guild_id,
    )
    if not paid:
        raise ValueError(
            f"Respec #{respecs_used + 1} costs **${cost_usd:,.2f}** "
            f"(wallet + bank). You don't have enough."
        )

    # Recompute hp_max from level + class base (no hp_alloc contribution
    # any more) so the refund snaps the player's max back to baseline.
    # Clamp current_hp to the new (smaller) max.
    cmeta = dc.class_meta(state.get("class_key") or "warrior") or dc.CLASSES["warrior"]
    new_hp_max = max(1, int(round(
        dc.STARTING_HP * float(cmeta["hp_mult"])
        + int(state.get("level") or 1) * dc.HP_PER_LEVEL
    )))
    cur_hp = min(int(state.get("current_hp") or 0), new_hp_max)

    await db.execute(
        """
        UPDATE user_dungeon
           SET hp_alloc          = 0,
               atk_alloc         = 0,
               spd_alloc         = 0,
               int_alloc         = 0,
               hp_max             = $3,
               current_hp         = $4,
               stat_respecs_used = stat_respecs_used + 1,
               updated_at         = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_hp_max), int(cur_hp),
    )
    new_state = await list_state(db, guild_id, user_id)
    return new_state, float(cost_usd), int(refunded)


async def reroll_class(
    db: Any, guild_id: int, user_id: int, new_class: str,
) -> dict:
    """Switch the player's class. Charges a USD wallet fee + bumps counter.

    Cost ramps geometrically per prior reroll
    (``class_reroll_cost_usd``). Cooldown gate is enforced server-side
    via EXTRACT(EPOCH FROM (NOW() - last_class_reroll_at)) so container
    clock skew can't be used to bypass it. Reroll is refused mid-run
    (the run state is wiped on completion only).

    On success: starter weapon/armor for the new class is minted into
    the inventory + equipped; stat-point allocations are PRESERVED so
    a Vigor-stacked Rogue can carry that build to Archer cleanly.
    """
    new_class = (new_class or "").strip().lower()
    if new_class not in dc.CLASSES:
        raise ValueError(f"Unknown class: {new_class!r}")

    state = await ensure_state(db, guild_id, user_id)
    cur_class = str(state.get("class_key") or "")
    if not cur_class:
        raise ValueError("Pick a class first with `,delve class <name>`.")
    if cur_class == new_class:
        raise ValueError(f"You are already a {dc.CLASSES[new_class]['name']}.")
    if state.get("run_id"):
        raise ValueError(
            "Finish or `,delve rest` first -- can't reroll mid-run."
        )

    # Cooldown gate (DB-side clock).
    elapsed = await db.fetch_val(
        "SELECT EXTRACT(EPOCH FROM (NOW() - last_class_reroll_at))::bigint "
        "FROM user_dungeon WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    cd = int(dc.CLASS_REROLL_COOLDOWN_S)
    if elapsed is not None and int(elapsed) < cd:
        wait = cd - int(elapsed)
        raise ValueError(
            f"Reroll cooldown: try again in {wait // 3600}h "
            f"{(wait % 3600) // 60}m."
        )

    rerolls_used = int(state.get("class_rerolls_used") or 0)
    cost_usd = dc.class_reroll_cost_usd(rerolls_used)
    cost_raw = to_raw(cost_usd)

    # Charge wallet (then bank as fallback) atomically; refuse if broke.
    paid = await db.fetch_val(
        """
        UPDATE users
           SET wallet = GREATEST(0, wallet - $1),
               bank   = bank - GREATEST(0, $1 - wallet)
         WHERE user_id = $2 AND guild_id = $3
           AND (wallet + bank) >= $1
        RETURNING 1
        """,
        cost_raw, user_id, guild_id,
    )
    if not paid:
        raise ValueError(
            f"Reroll #{rerolls_used + 1} costs **${cost_usd:,.2f}** "
            f"(wallet + bank). You don't have enough."
        )

    # Apply the reroll: snap starter gear, reset skill cd + buffs,
    # bump rerolls_used, stamp the cooldown clock.
    starter_w = dc.starter_weapon_for_class(new_class)
    starter_a = dc.starter_armor_for_class(new_class)
    weapons = dict(state.get("weapons_owned") or {})
    armors  = dict(state.get("armor_owned")   or {})
    weapons.setdefault(starter_w, 1)
    armors.setdefault(starter_a, 1)

    cmeta_new = dc.class_meta(new_class)
    new_hp_alloc = int(state.get("hp_alloc") or 0)
    new_hp_max = max(1, int(round(
        dc.STARTING_HP * float(cmeta_new["hp_mult"])
        + int(state.get("level") or 1) * dc.HP_PER_LEVEL
        + new_hp_alloc * dc.STAT_POINT_HP_BONUS
    )))
    cur_hp = min(int(state.get("current_hp") or 0), new_hp_max)

    await db.execute(
        """
        UPDATE user_dungeon
           SET class_key            = $3,
               equipped_weapon      = $4,
               equipped_armor       = $5,
               weapons_owned        = $6::jsonb,
               armor_owned          = $7::jsonb,
               hp_max               = $8,
               current_hp           = $9,
               skill_cd_remaining   = 0,
               player_buffs         = '{}'::jsonb,
               class_rerolls_used   = class_rerolls_used + 1,
               last_class_reroll_at = NOW(),
               updated_at           = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, new_class,
        starter_w, starter_a, _json(weapons), _json(armors),
        int(new_hp_max), int(cur_hp),
    )
    return await list_state(db, guild_id, user_id)


async def open_chest(db: Any, guild_id: int, user_id: int) -> ChestResult:
    """Pop the current chest payload (mints RUNE, possibly drops a relic)
    and clear the room."""
    state = await list_state(db, guild_id, user_id)
    if state.get("current_room_type") != "chest":
        raise ValueError("No chest in this room.")
    payload = dict(state.get("current_room_payload") or {})
    rune_amt = float(payload.get("rune_amount") or 0.0)
    floor = int(state.get("current_floor") or 1)
    # Curse + relic multipliers fold in before crediting so leaderboards
    # and oracle moves see the post-modifier amount.
    rune_amt *= dc.curse_mult(state.get("run_curse"), "chest_mult", 1.0)
    rune_amt *= dc.relic_effect(state.get("equipped_relic"), "rune_drop_mult", 1.0)
    # Shrine-debt buff: a one-shot kicker on the next chest opened. The
    # buff is set by ``pray_at_shrine`` for the curse outcome and pays
    # off here so the cracked-promise narrative actually rewards the
    # cost. Consumes the buff regardless of the multiplier so a player
    # never quietly carries it across runs.
    buffs = dict(state.get("player_buffs") or {})
    shrine_debt_mult = 0.0
    if "shrine_debt" in buffs:
        shrine_debt_mult = float(buffs["shrine_debt"].get("value") or 1.0)
        rune_amt *= shrine_debt_mult
        buffs.pop("shrine_debt", None)
        await db.execute(
            "UPDATE user_dungeon SET player_buffs = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(buffs),
        )
    if rune_amt > 0:
        await _credit_rune(
            db, guild_id, user_id, rune_amt,
            int(state.get("run_id") or 0),
        )
    # Relic drop roll: deeper floors have a non-trivial shot at adding a
    # passive item to the player's relics_owned inventory. Drops persist
    # across runs and stack so a duplicate just bumps the count.
    rng_chest = random.Random()
    relic_key = dc.roll_relic(floor, rng_chest)
    relics_owned = _as_dict(state.get("relics_owned"))
    if relic_key:
        relics_owned[relic_key] = int(relics_owned.get(relic_key, 0) or 0) + 1
        await db.execute(
            "UPDATE user_dungeon SET relics_owned = $3::jsonb, updated_at = NOW() "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(relics_owned),
        )
    # Junk drop roll on chest open. Independent of the relic check so a
    # chest can drop both a relic and a salvage breadcrumb -- chests
    # already feel celebratory; this just means the celebration always
    # leaves you with SOMETHING to sell or use.
    junk_key = dc.roll_junk_drop(floor, rng_chest, source="chest")
    if junk_key:
        await _credit_junk(db, guild_id, user_id, junk_key)
    await db.execute(
        "UPDATE user_dungeon SET current_room_type = 'empty', "
        "current_room_payload = NULL, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    return ChestResult(
        rune_amount=float(rune_amt),
        relic_key=relic_key,
        junk_drop_key=junk_key,
        shrine_debt_mult=shrine_debt_mult,
    )


# ============================================================================
# Leaderboards + lookups
# ============================================================================

async def get_top_delvers(db: Any, guild_id: int, limit: int = 10) -> list[dict]:
    rows = await db.fetch_all(
        """
        SELECT user_id, class_key, level, deepest_floor, total_kills,
               total_captures, bosses_slain
          FROM user_dungeon
         WHERE guild_id = $1 AND deepest_floor > 0
         ORDER BY deepest_floor DESC, level DESC, total_kills DESC
         LIMIT $2
        """,
        guild_id, int(limit),
    )
    return [dict(r) for r in (rows or [])]


async def get_user_runs(
    db: Any, guild_id: int, user_id: int, limit: int = 5,
) -> list[dict]:
    rows = await db.fetch_all(
        """
        SELECT * FROM dungeon_runs
         WHERE guild_id = $1 AND user_id = $2
         ORDER BY started_at DESC
         LIMIT $3
        """,
        guild_id, user_id, int(limit),
    )
    return [dict(r) for r in (rows or [])]


async def credit_combat_drops(
    db: Any, guild_id: int, user_id: int, result: CombatResult,
) -> None:
    """Convenience: after a winning round, credit ore/RUNE drops (mints
    that move the relevant oracle the same way mining does)."""
    state = await list_state(db, guild_id, user_id)
    run_id = int(state.get("run_id") or 0)
    if result.ore_drop_symbol and result.ore_drop_qty_human > 0:
        await _credit_ore(
            db, guild_id, user_id,
            result.ore_drop_symbol, result.ore_drop_qty_human, run_id,
        )
    if result.rune_drop_human > 0:
        await _credit_rune(
            db, guild_id, user_id, result.rune_drop_human, run_id,
        )


# ============================================================================
# Wild-buddy battles (Delve crawler)
# ============================================================================
#
# advance_room may roll a 'wild_battle' room and stash the synth opponent
# in current_room_payload['wild_buddy']. The cog runs the interactive
# battle (services.buddy_battle.Fighter.from_row vs the player's active
# CC buddy) and calls back here with the engine result.
#
# Win path mints RUNE + a bonus ore drop (mirrors services.fishing's
# wild-battle LURE+REEL credit) and rolls a capture chance. Loss path
# is a counter bump only -- no penalty, no payout.

async def resolve_wild_battle(
    db: Any, guild_id: int, user_id: int,
    *, won: bool,
    floor: int,
    opponent_species: str | None = None,
    opponent_level: int = 1,
    opponent_rarity_tier: int = 1,
    bonus_pct: float = 0.0,
    skip_capture_roll: bool = False,
) -> WildBattleResolution:
    """Persist the outcome of a wild-buddy battle in a delve.

    ``won=False`` just bumps the loss counter -- no penalty, no payout.
    ``won=True`` rolls a RUNE reward via ``dc.wild_battle_rune_reward``
    plus a bonus ore drop via ``dc.wild_battle_ore_reward``, credits each
    via the standard ``_credit_rune`` / ``_credit_ore`` helpers (so the
    oracle drop and lifetime counters stay uniform with mining), rolls a
    capture chance, and -- on capture -- inserts a fresh cc_buddies row
    for the wild buddy at the OPPONENT'S level + rarity. Capture respects
    ``MAX_OWNED_BUDDIES`` and falls through cleanly when the shelter is
    full (player keeps RUNE + ore + counters; only the buddy is missed).

    ``bonus_pct`` is a decimal multiplier added to BOTH rewards
    (0.30 == "+30%") so the cog can pay extra for a clean, fast fight.

    ``skip_capture_roll`` -- pass True when the caller has already done
    a manual capture (e.g. the explicit Capture button on the wild-
    buddy battle view). Skips the auto-roll AND the cc_buddies insert
    so the player isn't double-credited.
    """
    state = await list_state(db, guild_id, user_id)
    run_id = int(state.get("run_id") or 0)

    captured = False
    captured_buddy_row: dict | None = None
    rune_reward_raw = 0
    ore_symbol: str | None = None
    ore_reward_raw = 0
    buddy_xp_awarded = 0
    fighter_buddy_id: int | None = None
    bonus_mult = 1.0 + max(0.0, float(bonus_pct))

    if won:
        # RUNE credit. Reuses _credit_rune so the post-mint oracle drop is
        # consistent with mob/boss kill drops; no separate slippage path.
        rune_human = dc.wild_battle_rune_reward(int(floor)) * bonus_mult
        if rune_human > 0:
            try:
                rune_reward_raw = await _credit_rune(
                    db, guild_id, user_id, float(rune_human), run_id,
                )
            except Exception:
                log.exception(
                    "resolve_wild_battle: RUNE reward credit failed "
                    "uid=%s gid=%s amt=%s", user_id, guild_id, rune_human,
                )
                rune_reward_raw = 0

        # Ore kicker. Independent try/except so an ore credit failure
        # doesn't roll back the RUNE the user already saw.
        ore_sym, ore_qty = dc.wild_battle_ore_reward(int(floor))
        ore_qty *= bonus_mult
        if ore_qty > 0 and ore_sym:
            try:
                ore_reward_raw = await _credit_ore(
                    db, guild_id, user_id, ore_sym, float(ore_qty), run_id,
                )
                ore_symbol = ore_sym
            except Exception:
                log.exception(
                    "resolve_wild_battle: ore kicker credit failed "
                    "uid=%s gid=%s sym=%s qty=%s",
                    user_id, guild_id, ore_sym, ore_qty,
                )
                ore_reward_raw = 0

        # BBT (Buddy Battle Token) -- universal battle reward, same
        # mint shape as fishing's wild-battle path. Scales with floor
        # depth and the clean-fight bonus_mult. Best-effort additive
        # to RUNE/ore: a BBT mint failure doesn't roll back the
        # primary rewards the user already saw.
        try:
            from services import buddy_economy as _bes
            bbt_amount = (1.0 + 0.5 * max(0, int(floor) - 1)) * bonus_mult
            await _bes.mint_bbt_reward(
                db, guild_id, user_id, float(bbt_amount), source="delve_wild",
            )
        except Exception:
            log.exception(
                "resolve_wild_battle: BBT mint failed uid=%s gid=%s",
                user_id, guild_id,
            )

        # Active-buddy XP. The buddy who actually fought the wild encounter
        # used to walk away with nothing on its row -- only the player got
        # RUNE / ore / BBT. Now we credit the active buddy's xp column via
        # the canonical award_battle_xp helper so the level / mood / panel
        # progression stays aligned with PvP and the chat-XP path.
        try:
            from services.buddy_battle import award_battle_xp as _award_bxp
            from configs.dungeon_config import wild_battle_xp_reward as _wbxp
            active = await db.fetch_one(
                "SELECT id FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 "
                "  AND status = 'owned' AND is_active "
                "LIMIT 1",
                int(guild_id), int(user_id),
            )
            if active and int(active.get("id") or 0) > 0:
                # Battle-lane multiplier on top of base XP. Lets a
                # signature-lane combat buddy (Pyper / Draclet / Blazer
                # via rarity-extras) actually feel like a fighter.
                try:
                    from services.buddy_bonus import buddy_bonus as _bb
                    battle_mult = await _bb(
                        db, guild_id, user_id, lane="battle",
                    )
                except Exception:
                    battle_mult = 1.0
                xp_award = int(round(
                    _wbxp(int(floor), int(opponent_rarity_tier or 1))
                    * bonus_mult * battle_mult
                ))
                if xp_award > 0:
                    await _award_bxp(
                        db, int(guild_id),
                        int(user_id), int(active["id"]),
                        int(xp_award),
                    )
                    buddy_xp_awarded = xp_award
                    fighter_buddy_id = int(active["id"])
        except Exception:
            log.exception(
                "resolve_wild_battle: buddy XP credit failed uid=%s gid=%s",
                user_id, guild_id,
            )

        # Capture roll. Uses the OPPONENT'S species / level / rarity so
        # the wild buddy you just beat IS the buddy you capture. Skipped
        # when the caller already manually captured via the explicit
        # Capture button (otherwise we'd double-insert cc_buddies).
        if (
            not skip_capture_roll
            and random.random() < dc.WILD_BATTLE_CAPTURE_CHANCE
            and opponent_species
        ):
            try:
                count = await db.fetch_val(
                    """
                    SELECT COUNT(*) FROM cc_buddies
                     WHERE guild_id = $1 AND owner_user_id = $2
                       AND status = 'owned'
                    """,
                    guild_id, user_id,
                )
                from services.buddy_economy import (
                    capture_destination as _dest,
                )
                _capture_dest = await _dest(db, guild_id, user_id)
                if _capture_dest is not None:
                    capture_status = (
                        "owned" if _capture_dest == "battle" else "stored"
                    )
                    species_capture = str(opponent_species)
                    try:
                        from services.buddy_names import generate_name
                        new_name = await generate_name(
                            species_capture, db, guild_id,
                        )
                    except Exception:
                        new_name = species_capture.title()
                    try:
                        await db.execute(
                            """
                            INSERT INTO cc_buddy_hatches
                              (guild_id, user_id, first_species)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (guild_id, user_id) DO NOTHING
                            """,
                            guild_id, user_id, species_capture,
                        )
                    except Exception:
                        log.exception(
                            "resolve_wild_battle: cc_buddy_hatches insert failed "
                            "uid=%s gid=%s", user_id, guild_id,
                        )
                    from configs.buddies_config import (
                        roll_gender as _roll_gender,
                        xp_for_level as _xp_for_level,
                    )
                    _cap_level = int(max(1, opponent_level))
                    new_row = await db.fetch_one(
                        """
                        INSERT INTO cc_buddies
                            (guild_id, owner_user_id, species, name,
                             status, is_active, rarity_tier, level, xp,
                             gender)
                        VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7, $8, $9)
                        RETURNING *
                        """,
                        guild_id, user_id, species_capture, new_name,
                        str(capture_status),
                        int(max(1, opponent_rarity_tier)),
                        _cap_level,
                        int(_xp_for_level(_cap_level)),
                        _roll_gender(),
                    )
                    if new_row:
                        captured = True
                        captured_buddy_row = dict(new_row)
                        # NFT layer: mint a buddy token. Best-effort.
                        try:
                            from services import items as _items
                            await _items.mint_unit(
                                db,
                                guild_id=guild_id,
                                contract_address=_items.contract_address(
                                    "buddy", str(species_capture).lower(),
                                ),
                                owner_user_id=user_id,
                                metadata={
                                    "species":     str(species_capture),
                                    "rarity_tier": int(max(1, opponent_rarity_tier)),
                                    "level":       int(max(1, opponent_level)),
                                    "gender":      str(new_row.get("gender") or "").upper(),
                                    "buddy_id":    int(new_row["id"]),
                                    "name":        str(new_name),
                                },
                                mint_source="dungeon.wild_capture",
                                source_table="cc_buddies",
                                source_id=int(new_row["id"]),
                            )
                        except Exception:
                            log.debug(
                                "nft dungeon capture mint sync failed "
                                "gid=%s uid=%s", guild_id, user_id,
                                exc_info=True,
                            )
            except Exception:
                log.exception(
                    "resolve_wild_battle: capture insert failed "
                    "uid=%s gid=%s", user_id, guild_id,
                )

    # Counter bump. Single UPDATE returns the post-write totals so the
    # cog can print "Wild battle wins: N" without an extra read.
    row = await db.fetch_one(
        """
        UPDATE user_dungeon
           SET wild_battles_won      = wild_battles_won
                                     + (CASE WHEN $3 THEN 1 ELSE 0 END),
               wild_battles_lost     = wild_battles_lost
                                     + (CASE WHEN $3 THEN 0 ELSE 1 END),
               wild_buddies_captured = wild_buddies_captured
                                     + (CASE WHEN $4 THEN 1 ELSE 0 END),
               current_mob_state     = NULL,
               current_room_payload  = NULL,
               current_room_type     = 'empty',
               last_action_at        = NOW(),
               updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        RETURNING wild_battles_won, wild_battles_lost, wild_buddies_captured
        """,
        guild_id, user_id, bool(won), bool(captured),
    )
    new_won = int((row or {}).get("wild_battles_won") or 0)
    new_lost = int((row or {}).get("wild_battles_lost") or 0)
    new_cap = int((row or {}).get("wild_buddies_captured") or 0)

    return WildBattleResolution(
        won=bool(won),
        captured=bool(captured),
        rune_reward_raw=int(rune_reward_raw),
        ore_symbol=ore_symbol,
        ore_reward_raw=int(ore_reward_raw),
        captured_buddy_row=captured_buddy_row,
        new_won_total=new_won,
        new_lost_total=new_lost,
        new_captured_total=new_cap,
        buddy_xp_awarded=int(buddy_xp_awarded),
        fighter_buddy_id=fighter_buddy_id,
    )


# ============================================================================
# Scavenge (free 10-min wander between runs; mirrors farm forage)
# ============================================================================
# Free roll outside an active delve: small RUNE / ORE purses, occasional
# dungeon consumables (potions/scrolls), and on a rare jackpot drops a
# pulsing relic shard straight into the player's relics_owned bag. No
# inputs consumed, 10-minute DB-clock cooldown.

@dataclass
class ScavengeResult:
    outcome_key:        str
    label:              str
    rune_credited:      float = 0.0
    ore_credited:       float = 0.0
    ore_symbol:         str | None = None
    consumables_added:  list[tuple[str, int]] = field(default_factory=list)
    scrolls_added:      list[tuple[str, int]] = field(default_factory=list)
    relic_added:        tuple[str, int] | None = None


_SCAVENGE_LABELS: dict[str, str] = {
    "rune_purse_small":  "Small Rune Purse",
    "rune_purse_big":    "Heavy Rune Purse",
    "ore_pile_small":    "Loose Ore Pile",
    "ore_pile_big":      "Heavy Ore Vein",
    "consumable_cache":  "Consumable Cache",
    "scroll_find":       "Sealed Scroll",
    "relic_shard":       "RELIC SHARD",
    "empty":             "Just Bones",
}


async def scavenge(
    db: Any, guild_id: int, user_id: int,
) -> ScavengeResult:
    """Wander the surface ruins once. Free roll, 10-minute cooldown.

    Cooldown enforced via DB-side clock on user_dungeon.last_scavenge_at
    so Python now() vs Postgres TIMESTAMPTZ never drift. Stamps the
    cooldown + bumps total_scavenges in the same UPDATE that credits the
    loot, so a transient crash never lets a player double-roll.

    Refuses to run while the player is mid-delve (run_id IS NOT NULL) --
    scavenging is an out-of-run wander, not an in-run shortcut.
    """
    state = await ensure_state(db, guild_id, user_id)

    if state.get("run_id"):
        raise ValueError(
            "You're mid-delve. Use `,delve rest` to exit to the surface first. "
            "(In combat? `,delve flee` escapes the fight -- then `,delve rest` to leave.)"
        )

    cd_row = await db.fetch_one(
        """
        SELECT
            CASE
                WHEN last_scavenge_at IS NULL THEN 0
                ELSE EXTRACT(EPOCH FROM (NOW() - last_scavenge_at))::INTEGER
            END AS elapsed_s
          FROM user_dungeon
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
    )
    elapsed_s = int((cd_row or {}).get("elapsed_s") or 0)
    if elapsed_s > 0 and elapsed_s < int(dc.SCAVENGE_COOLDOWN_S):
        wait = int(dc.SCAVENGE_COOLDOWN_S - elapsed_s)
        raise ValueError(
            f"You're still catching your breath  -  scavenge again in **{wait}s**."
        )

    rng = random.Random()
    outcome = dc.roll_scavenge_outcome(rng)
    label = _SCAVENGE_LABELS.get(outcome, outcome.replace("_", " ").title())

    consumables = _as_dict(state.get("consumables"))
    relics_owned = _as_dict(state.get("relics_owned"))

    rune_credited = 0.0
    ore_credited = 0.0
    ore_symbol: str | None = None
    consumables_added: list[tuple[str, int]] = []
    scrolls_added:     list[tuple[str, int]] = []
    relic_added: tuple[str, int] | None = None

    # Delve-level payout multiplier: scavenge gains scale with the
    # player's delve level so a Lv 40 scavenger doesn't pull the same
    # handful of RUNE as a fresh Lv 1 (mirrors farming/fishing forage).
    _delve_lvl = int(state.get("level") or 1)
    _lvl_mult = dc.level_payout_mult(_delve_lvl)
    if outcome in ("rune_purse_small", "rune_purse_big"):
        lo, hi = dc.SCAVENGE_PAYOUTS[outcome]
        rune_credited = round(rng.uniform(lo, hi) * _lvl_mult, 2)
    elif outcome in ("ore_pile_small", "ore_pile_big"):
        lo, hi = dc.SCAVENGE_PAYOUTS[outcome]
        ore_credited = round(rng.uniform(lo, hi) * _lvl_mult, 2)
        # Pick the ore symbol weighted toward COPPER for small piles and
        # SILVER/GOLD for the heavy pile -- mirrors the run mining table
        # without giving free-roll players a guaranteed gold drop.
        if outcome == "ore_pile_big":
            ore_symbol = rng.choices(
                list(dc.ORE_SYMBOLS), weights=(20, 35, 45), k=1,
            )[0]
        else:
            ore_symbol = rng.choices(
                list(dc.ORE_SYMBOLS), weights=(70, 25, 5), k=1,
            )[0]
    elif outcome == "consumable_cache":
        pool = list(dc.SCAVENGE_CONSUMABLE_POOL)
        picks = rng.sample(pool, k=min(dc.SCAVENGE_CONSUMABLE_PICKS, len(pool)))
        qty_lo, qty_hi = dc.SCAVENGE_CONSUMABLE_QTY
        for key in picks:
            qty = rng.randint(qty_lo, qty_hi)
            consumables[key] = int(consumables.get(key, 0) or 0) + qty
            consumables_added.append((key, qty))
    elif outcome == "scroll_find":
        key = rng.choice(dc.SCAVENGE_SCROLL_POOL)
        qty_lo, qty_hi = dc.SCAVENGE_SCROLL_QTY
        qty = rng.randint(qty_lo, qty_hi)
        consumables[key] = int(consumables.get(key, 0) or 0) + qty
        scrolls_added.append((key, qty))
    elif outcome == "relic_shard":
        # Weighted pick from the existing RELICS catalog, biased to the
        # cheaper tiers so the free-roll jackpot doesn't trivialise the
        # actual run-loot payout.
        relic_pool = [k for k, v in dc.RELICS.items() if v.get("rarity") in ("common", "uncommon")]
        if relic_pool:
            relic_key = rng.choice(relic_pool)
            qty_lo, qty_hi = dc.SCAVENGE_RELIC_QTY
            qty = rng.randint(qty_lo, qty_hi)
            relics_owned[relic_key] = int(relics_owned.get(relic_key, 0) or 0) + qty
            relic_added = (relic_key, qty)
        else:
            # No relic candidates -- consolation RUNE drop, mirrors the
            # ancient_relic decay branch in fishing.dig_treasure_map.
            lo, hi = dc.SCAVENGE_PAYOUTS["rune_purse_big"]
            rune_credited = round(rng.uniform(lo, hi), 2)
            label = "Empty Cache (relic decayed -- RUNE consolation)"

    raw_rune = to_raw(rune_credited) if rune_credited > 0 else 0
    raw_ore  = to_raw(ore_credited)  if ore_credited  > 0 else 0

    await db.execute(
        """
        UPDATE user_dungeon SET
            consumables       = $3::jsonb,
            relics_owned      = $4::jsonb,
            total_scavenges   = total_scavenges + 1,
            last_scavenge_at  = NOW(),
            updated_at        = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
        _json(consumables), _json(relics_owned),
    )

    if raw_rune > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                dc.CRYPT_NETWORK_SHORT, dc.RUNE_SYMBOL,
                int(raw_rune),
            )
        except Exception:
            log.exception("scavenge: RUNE credit failed uid=%s gid=%s amt=%s",
                          user_id, guild_id, raw_rune)
            rune_credited = 0.0
    if raw_ore > 0 and ore_symbol:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                dc.CRYPT_NETWORK_SHORT, ore_symbol,
                int(raw_ore),
            )
        except Exception:
            log.exception("scavenge: %s credit failed uid=%s gid=%s amt=%s",
                          ore_symbol, user_id, guild_id, raw_ore)
            ore_credited = 0.0
            ore_symbol = None

    return ScavengeResult(
        outcome_key=outcome,
        label=label,
        rune_credited=float(rune_credited),
        ore_credited=float(ore_credited),
        ore_symbol=ore_symbol,
        consumables_added=consumables_added,
        scrolls_added=scrolls_added,
        relic_added=relic_added,
    )
