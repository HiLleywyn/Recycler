"""
services/buddy_battle.py  -  Pet-battle engine for the CC Buddy system.

Turn-based duels between two owned+active buddies. Every number is derived
from stats that already live on cc_buddies -- no new schema, no new
columns. The caller (cogs/buddy.py) is responsible for:
    - confirming mutual consent (challenge + accept view)
    - enforcing the per-user cooldown
    - awarding the winner's active buddy the XP this engine returns
    - rendering the returned BattleLog into an embed

The engine itself is pure -- it takes two dict rows and produces a log +
winner + xp reward. It does not touch the database, which keeps the
system easy to unit test and makes it safe to dry-run for previews.

Formula summary (see buddies_config.py for the tunables):
    HP  = (tier.hp_base  + level * 3)  * (0.5 + 0.5 * hunger/100)
    ATK = (tier.atk_base + level * 0.8)* (0.5 + 0.5 * happiness/100)
    SPD = 0.5 + 0.5 * energy/100
    crit_chance = BATTLE_CRIT_BASE + BATTLE_CRIT_SPD_SCALE * SPD
    crit_mult   = BATTLE_CRIT_MULT
    dmg_roll    = ATK * uniform(0.85, 1.15) * (crit_mult if crit else 1.0)

Species abilities (buddies_config SPECIES[*]['ability_key']) layer on top
and are the *only* way raw level/stats don't monotonically decide the
winner -- a rare low-level buddy with a clutch ability can beat a common
higher-level one.

Ability magnitude scales with rarity via ``RARITY_TIERS[tier]["ability_mult"]``
(see buddies_config.py): a Legendary buddy's Lucky Paw dodges more often
than a Common buddy's, a Legendary Pincer Grip stuns more often, and so
on. Fixed-turn abilities (Chatterbox, Rain Dance) stay chunky; only
percentage-based effects scale.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

from configs.buddies_config import (
    ABILITY_PROGRESSION,
    BATTLE_CRIT_BASE,
    BATTLE_CRIT_MULT,
    BATTLE_HEAL_SOFT_CAP_PCT,
    BATTLE_MAX_ROUNDS,
    BATTLE_REGEN_HP_CAP_PCT,
    BATTLE_USD_MAX,
    BATTLE_USD_MIN,
    BATTLE_USD_SCALE,
    BATTLE_XP_MIN,
    BATTLE_XP_SCALE,
    BATTLE_CRIT_SPD_SCALE,
    SECONDARY_ABILITY_LEVEL,
    SPECIES,
    STAT_POINT_ATK_BONUS,
    STAT_POINT_HP_BONUS,
    STAT_POINT_SPD_BONUS,
    TERTIARY_ABILITY_LEVEL,
    rarity_meta,
)

log = logging.getLogger(__name__)


# =============================================================================
# Fighter state
# =============================================================================

@dataclass
class Fighter:
    """Per-battle mutable state for one buddy.

    Lives for the length of a single ``run_battle`` call. Every field is
    derived from the source row at build time, so the engine is pure and
    the caller's ``dict`` row is never mutated.
    """
    id: int
    owner_id: int
    species: str
    name: str
    emoji: str
    level: int
    tier: int
    tier_name: str

    max_hp: int
    hp: int
    atk: float
    spd: float

    ability_key: str
    ability_name: str
    ability_mult: float                 # rarity-driven magnitude scalar (Common 1.0 .. Legendary 1.8)
    boss_zone_id: str = ""              # set when this fighter was tamed from a boss zone
                                        # (drives portrait overlay + ability override)

    # Per-battle ability state. Defaults = ability off / not yet used.
    atk_mult: float = 1.0              # mutable ATK multiplier (wolf rage, shrimp ink on foe)
    dmg_taken_mult: float = 1.0        # 1.0 = normal; 0.8 for crab hard shell (scaled by rarity)
    dodge_chance: float = 0.0          # cobble lucky paw (scaled by rarity)
    first_strike_pending: bool = False  # fox: force-crit once
    double_strike: bool = False         # octopus: two 65% hits per attack
    poison_proc_chance: float = 0.0     # pyper: chance to poison on hit (scaled by rarity)
    damage_reroll_chance: float = 0.0   # glitch: reroll damage chance (scaled by rarity)
    stun_proc_chance: float = 0.0       # lobster: stun chance on hit (scaled by rarity)
    low_hp_rage_pending: bool = False   # wolf: buff arms at <50% HP
    low_hp_rage_bonus: float = 0.50     # wolf ATK buff magnitude (scaled by rarity)
    ink_used: bool = False              # shrimp: one-shot debuff
    ink_atk_reduction: float = 0.20     # shrimp ATK-cut magnitude (scaled by rarity)
    rain_used: bool = False              # nimbus: one-shot 2-turn skip
    preen_used: bool = False              # wecco: one-shot clutch heal + atk buff
    preen_heal_pct: float = 0.60        # wecco heal target % (scaled by rarity)
    preen_atk_bonus: float = 0.15       # wecco ATK buff magnitude (scaled by rarity)

    # Phase-2 ability state (new species)
    lifesteal_pct: float = 0.0          # blazer: heal % of damage dealt back
    counter_chance: float = 0.0         # thornling: chance to counter when hit
    counter_pct: float = 0.50           # thornling: counter dmg as fraction of own ATK
    execute_thresh: float = 0.0         # draclet: execute trigger HP fraction
    execute_bonus_pct: float = 0.0      # draclet: bonus dmg multiplier below threshold
    regen_pct: float = 0.0              # gloomer / verdant: heal fraction of max HP per round
    atk_up_per_stack: float = 0.0       # robo / verdant: ATK fraction gain per stack
    atk_up_stacks: int = 0              # robo / verdant: current ATK-up stacks
    atk_up_max_stacks: int = 3          # robo / verdant: maximum ATK-up stacks
    atk_up_every_n_rounds: int = 3      # robo (3) / verdant (1) cadence

    # New species abilities (Phase 3)
    reflect_pct: float = 0.0            # tortuga / phantom: % damage reflected back
    static_proc_chance: float = 0.0     # jolt: chance to discharge bonus shock damage
    static_bonus_mult: float = 0.50     # jolt: bonus shock damage multiplier
    phase_dodge_chance: float = 0.0     # phantom: dodge that ALSO reflects on success
    ambush_pending: bool = False        # mimik: first hit is guaranteed crit

    # Level-gated secondary / tertiary ability flags + state.
    # Primed in _prime_ability() based on level + ABILITY_PROGRESSION.
    crit_chance_bonus: float = 0.0      # lucky_crit: extra crit %
    crit_mult_bonus: float = 0.0        # battle_focus: bumps BATTLE_CRIT_MULT
    second_wind_used: bool = False      # second_wind: one-shot heal flag
    second_wind_pending: bool = False   # second_wind: armed when secondary unlocks
    killing_blow_thresh: float = 0.0    # killing_blow: extra execute threshold
    killing_blow_bonus_pct: float = 0.0 # killing_blow: extra execute multiplier
    berserker_pending: bool = False     # berserker: low-HP rage 2nd unlock flag
    berserker_thresh: float = 0.40      # berserker: HP % trigger
    berserker_bonus: float = 0.25       # berserker: ATK % buff on trigger
    affinity_bonus: float = 0.0         # elemental_affinity: scales primary magnitude

    # Healing soft-cap tracker. Every heal increments this; once it
    # crosses BATTLE_HEAL_SOFT_CAP_PCT * starting_max_hp, further heals
    # are halved. Re-set to 0 on construction.
    heal_total: int = 0                 # total HP healed by this fighter so far

    # Victim-side status trackers mutated by the opponent's abilities.
    poison_turns: int = 0               # per-fighter poison timer
    stunned_turns: int = 0              # skip this many turns (1 = next turn)
    ability_log: list[str] = field(default_factory=list)

    # Level used to gate secondary / tertiary ability primes. Stored on
    # the fighter so renderers can show "Lv 15 unlock active" etc.
    secondary_active: bool = False
    tertiary_active: bool = False

    # Carried over from gear -- consumed by the interactive view setup.
    start_stamina_bonus: int = 0

    @classmethod
    def from_row(cls, row: dict) -> "Fighter":
        species = str(row.get("species") or "")
        meta = SPECIES.get(species, {})
        tier = int(row.get("rarity_tier") or 1)
        tmeta = rarity_meta(tier)

        level = max(1, int(row.get("level") or 1))
        hunger    = max(0, min(100, int(row.get("hunger") or 0)))
        happiness = max(0, min(100, int(row.get("happiness") or 0)))
        energy    = max(0, min(100, int(row.get("energy") or 0)))

        hp_alloc  = max(0, int(row.get("hp_alloc")  or 0))
        atk_alloc = max(0, int(row.get("atk_alloc") or 0))
        spd_alloc = max(0, int(row.get("spd_alloc") or 0))

        hp_mult   = 0.5 + 0.5 * (hunger / 100.0)
        atk_mood  = 0.5 + 0.5 * (happiness / 100.0)
        spd       = min(1.0, 0.5 + 0.5 * (energy / 100.0) + spd_alloc * STAT_POINT_SPD_BONUS)

        hp_base  = int(tmeta["hp_base"])  + level * 3   + hp_alloc  * STAT_POINT_HP_BONUS
        atk_base = int(tmeta["atk_base"]) + level * 0.8 + atk_alloc * STAT_POINT_ATK_BONUS

        # Equipped gear bonuses (charm slot only -- accessories are
        # cosmetic). Read each known stat_bonus key from
        # buddy_gear_config.BUDDY_GEAR.<charm>['stat_bonus']. Unknown
        # keys are ignored so adding a new charm with a non-battle
        # bonus (expedition_loot_pct etc.) doesn't blow up the engine.
        gear_atk_pct = 0.0
        gear_hp_pct = 0.0
        gear_spd_flat = 0.0
        gear_crit_chance = 0.0
        gear_dr_pct = 0.0
        gear_lifesteal = 0.0
        gear_regen = 0.0
        gear_reflect = 0.0
        gear_start_stam = 0
        try:
            from configs.buddy_gear_config import gear_meta as _gear_meta
            gear = row.get("gear") or {}
            if isinstance(gear, str):
                import json as _json
                try:
                    gear = _json.loads(gear) or {}
                except Exception:
                    gear = {}
            charm_key = str((gear or {}).get("charm") or "")
            charm_meta = _gear_meta(charm_key) if charm_key else None
            if charm_meta:
                sb = charm_meta.get("stat_bonus") or {}
                gear_atk_pct     = float(sb.get("atk_pct")          or 0.0)
                gear_hp_pct      = float(sb.get("hp_pct")           or 0.0)
                gear_spd_flat    = float(sb.get("spd_flat")         or 0.0)
                gear_crit_chance = float(sb.get("crit_chance_pct")  or 0.0)
                gear_dr_pct      = float(sb.get("dr_pct")           or 0.0)
                gear_lifesteal   = float(sb.get("lifesteal_pct")    or 0.0)
                gear_regen       = float(sb.get("regen_pct")        or 0.0)
                gear_reflect     = float(sb.get("reflect_pct")      or 0.0)
                gear_start_stam  = int(sb.get("start_stamina")      or 0)
        except Exception:
            log.debug("buddy_battle: gear lookup failed", exc_info=True)

        spd = min(1.0, spd + gear_spd_flat)
        max_hp = int(round(hp_base * hp_mult * (1.0 + gear_hp_pct)))
        atk    = float(atk_base) * atk_mood * (1.0 + gear_atk_pct)

        # Boss-tamed buddies (row.boss_zone_id is set) override the
        # species' default ability with the variant's named ability so
        # a captured Meadow King brings "Royal Fury" into battle, not
        # the wolf's generic "Pack Howl". Lookup tolerates a missing
        # variant table (e.g. orphaned row whose zone was renamed).
        boss_zid = str(row.get("boss_zone_id") or "")
        ability_key  = str(meta.get("ability_key") or "")
        ability_name = str(meta.get("ability_name") or "")
        if boss_zid:
            try:
                from configs.buddies_config import boss_variant as _bv
                bv = _bv(boss_zid)
                if bv.get("ability_key"):
                    ability_key  = str(bv["ability_key"])
                    ability_name = str(bv.get("ability_name") or ability_name)
            except Exception:
                pass

        f = cls(
            id=int(row.get("id") or 0),
            owner_id=int(row.get("owner_user_id") or 0),
            species=species,
            name=str(row.get("name") or species.title()),
            emoji=str(meta.get("emoji") or ""),
            level=level,
            tier=tier,
            tier_name=str(tmeta.get("name") or "Common"),
            max_hp=max(1, max_hp),
            hp=max(1, max_hp),
            atk=atk,
            spd=spd,
            ability_key=ability_key,
            ability_name=ability_name,
            ability_mult=float(tmeta.get("ability_mult") or 1.0),
            boss_zone_id=boss_zid,
        )
        _prime_ability(f)
        # Layer charm stat bonuses ON TOP of the primed ability values.
        # Charms stack additively with abilities (a Bloomheart + gloomer
        # gets both regens, etc.) so building around your buddy's
        # archetype with the right charm actually pays off.
        if gear_crit_chance > 0:
            f.crit_chance_bonus += gear_crit_chance
        if gear_dr_pct > 0:
            f.dmg_taken_mult *= max(0.50, 1.0 - gear_dr_pct)
        if gear_lifesteal > 0:
            f.lifesteal_pct = min(0.45, f.lifesteal_pct + gear_lifesteal)
        if gear_regen > 0:
            f.regen_pct = min(0.10, f.regen_pct + gear_regen)
        if gear_reflect > 0:
            f.reflect_pct = min(0.45, f.reflect_pct + gear_reflect)
        if gear_start_stam > 0:
            f.start_stamina_bonus = gear_start_stam
        return f


def _prime_ability(f: Fighter) -> None:
    """Apply the passive / trigger setup for a fighter's species ability.

    Ability magnitudes are scaled by ``f.ability_mult`` (which comes from
    the buddy's rarity tier): a Legendary buddy's Lucky Paw dodges more
    often than a Common one's, a Legendary Hard Shell mitigates more
    damage, and so on. One-off active abilities (ink_cloud, rain_dance)
    just record that they haven't been used yet; the engine consumes them
    at the right moment, using the scaled magnitudes stored here.

    Level-gated secondary (Lv 15+) and tertiary (Lv 30+) abilities from
    ``ABILITY_PROGRESSION`` are layered on top after the primary. They
    use the same ability_mult so rarity scaling matches.
    """
    _prime_primary(f, f.ability_key, f.ability_mult)
    plan = ABILITY_PROGRESSION.get(f.species, {}) or {}
    if f.level >= SECONDARY_ABILITY_LEVEL:
        sec_key = str(plan.get("sec") or "")
        if sec_key:
            _prime_secondary(f, sec_key, f.ability_mult)
            f.secondary_active = True
    if f.level >= TERTIARY_ABILITY_LEVEL:
        ter_key = str(plan.get("ter") or "")
        if ter_key:
            _prime_secondary(f, ter_key, f.ability_mult)
            f.tertiary_active = True
    # Apply elemental_affinity AFTER both passives have primed -- it
    # boosts the magnitudes of the primary ability triggers (preen,
    # poison %, dodge %, etc.) so it has to read the already-primed
    # values and bump them.
    if f.affinity_bonus > 0:
        _apply_affinity_bonus(f)


def _prime_primary(f: Fighter, key: str, m: float) -> None:
    """Set up the species' primary ability fields. Pulled out of
    _prime_ability so the same logic also powers the level-gated
    affinity_bonus path (it needs to know which knobs to scale)."""
    if key == "dodge_20":
        f.dodge_chance = min(0.45, 0.20 * m)
    elif key == "first_strike":
        f.first_strike_pending = True
    elif key == "damage_reduction_20":
        f.dmg_taken_mult = max(0.50, 1.0 - min(0.50, 0.20 * m))
    elif key == "low_hp_rage":
        f.low_hp_rage_pending = True
        f.low_hp_rage_bonus = 0.50 * m
    elif key == "ink_atk_debuff_20":
        f.ink_used = False
        f.ink_atk_reduction = min(0.45, 0.20 * m)
    elif key == "damage_reroll":
        f.damage_reroll_chance = min(0.45, 0.20 * m)
    elif key == "double_strike":
        f.double_strike = True
    elif key == "stun_15":
        f.stun_proc_chance = min(0.35, 0.15 * m)
    elif key == "rain_skip_2":
        f.rain_used = False
    elif key == "poison_bite":
        f.poison_proc_chance = min(0.50, 0.25 * m)
    elif key == "preen_heal":
        f.preen_used = False
        # Rebalance: heal target 0.60 -> 0.50 base, ATK buff 0.15 -> 0.12.
        # Trigger window unchanged at 35% HP threshold.
        f.preen_heal_pct  = min(0.85, 0.50 * m)
        f.preen_atk_bonus = 0.12 * m
    elif key == "lifesteal_20":
        # Rebalance: 0.20 -> 0.15 base.
        f.lifesteal_pct = min(0.35, 0.15 * m)
    elif key == "counter_25":
        f.counter_chance = min(0.50, 0.25 * m)
        f.counter_pct = 0.50
    elif key == "execute_30":
        f.execute_thresh = 0.30
        f.execute_bonus_pct = min(2.0, 0.80 * m)
    elif key == "regen_3pct":
        # Rebalance: 0.03 -> 0.02 per round (also gated by 75% HP cap
        # in _tick_regen so the buddy can't sit at full health regen-
        # padding for free).
        f.regen_pct = min(0.08, 0.02 * m)
    elif key == "atk_up_3rounds":
        f.atk_up_per_stack = 0.15 * m
        f.atk_up_max_stacks = 3
        f.atk_up_every_n_rounds = 3
    # ── new species (Phase 3) ───────────────────────────────────────────
    elif key == "fortress_shell":
        # Tortuga: -15% damage taken AND reflects 12% of every incoming hit.
        f.dmg_taken_mult = max(0.55, 1.0 - min(0.45, 0.15 * m))
        f.reflect_pct = min(0.30, 0.12 * m)
    elif key == "static_shock":
        # Jolt: chance per hit to discharge a 50% bonus dmg shock.
        f.static_proc_chance = min(0.55, 0.30 * m)
        f.static_bonus_mult = 0.50 * m
    elif key == "phase_shift":
        # Phantom: 30% dodge that ALSO reflects 25% of would-have-been damage.
        f.dodge_chance = min(0.45, 0.30 * m)
        f.phase_dodge_chance = f.dodge_chance
        f.reflect_pct = min(0.40, 0.25 * m)
    elif key == "photo_synth":
        # Verdant: small per-round regen (capped at 75% HP via _tick_regen)
        # PLUS slow ATK ramp every round (4 stacks max).
        f.regen_pct = min(0.05, 0.015 * m)
        f.atk_up_per_stack = 0.06 * m
        f.atk_up_max_stacks = 4
        f.atk_up_every_n_rounds = 1
    elif key == "ambush_strike":
        # Mimik: first hit guaranteed crit + permanent +20% crit chance.
        f.ambush_pending = True
        f.first_strike_pending = True
        f.crit_chance_bonus += min(0.30, 0.20 * m)
    # extra_turn_every_3 (zenny): handled in the round loop directly.


def _prime_secondary(f: Fighter, key: str, m: float) -> None:
    """Apply a level-gated passive (secondary @ Lv 15, tertiary @ Lv 30).

    These layer on top of the primary ability and never replace it. Each
    key here corresponds to a row in buddies_config.ABILITY_KIT. Effects
    are tuned to be meaningful but never overshadow the species' primary.
    """
    if key == "sharp_claws":
        # +10% ATK, scaled by rarity. Stacks with low_hp_rage / preen.
        f.atk_mult *= 1.0 + (0.10 * m)
    elif key == "tough_hide":
        # -10% damage taken, multiplicative with shells.
        f.dmg_taken_mult *= max(0.50, 1.0 - 0.10 * m)
    elif key == "evasive":
        # +10% dodge, capped at 50% so legendaries don't auto-win.
        f.dodge_chance = min(0.55, f.dodge_chance + 0.10 * m)
    elif key == "lucky_crit":
        # +10% flat crit chance bonus.
        f.crit_chance_bonus += 0.10 * m
    elif key == "swift_recovery":
        # +1% max HP regen per round, additive with primary regen. Still
        # gated by BATTLE_REGEN_HP_CAP_PCT inside _tick_regen.
        f.regen_pct += 0.01 * m
    elif key == "battle_focus":
        # Crit multiplier 1.80 -> 2.10 (additive +0.30 base, scaled).
        f.crit_mult_bonus += 0.30 * m
    elif key == "iron_will":
        # Further -15% damage taken, multiplicative with everything else.
        f.dmg_taken_mult *= max(0.50, 1.0 - 0.15 * m)
    elif key == "second_wind":
        # Arms a one-shot mid-fight heal at <30% HP.
        f.second_wind_pending = True
        f.second_wind_used = False
    elif key == "killing_blow":
        # Adds an execute window even if the species didn't have one.
        # Stacks ADDITIVELY with execute_30 (draclet) for a real finisher.
        f.killing_blow_thresh = 0.25
        f.killing_blow_bonus_pct = 0.50 * m
    elif key == "berserker":
        # Stronger version of low_hp_rage that triggers earlier (40% HP)
        # and stacks with it. Tracked separately so a wolf-with-berserker
        # can fire BOTH (~40% then ~50%) for a comeback build.
        f.berserker_pending = True
        f.berserker_thresh = 0.40
        f.berserker_bonus = 0.25 * m
    elif key == "elemental_affinity":
        # Bumps the primary ability's magnitude after both have primed.
        # Stored as a delta so _apply_affinity_bonus knows what to scale.
        f.affinity_bonus = 0.15 * m


def _apply_affinity_bonus(f: Fighter) -> None:
    """Re-scale the primary ability magnitudes after secondary/tertiary
    have primed. Multiplicative on the already-rarity-scaled values so
    a Legendary glitch with elemental_affinity does even higher dmg-reroll
    chance (capped at the primary's own ceiling).
    """
    boost = 1.0 + f.affinity_bonus
    if f.dodge_chance > 0:
        f.dodge_chance = min(0.55, f.dodge_chance * boost)
    if f.dmg_taken_mult < 1.0:
        # mitigation = 1 - dmg_taken_mult; bump it then re-floor at 0.50.
        mit = (1.0 - f.dmg_taken_mult) * boost
        f.dmg_taken_mult = max(0.50, 1.0 - mit)
    if f.poison_proc_chance > 0:
        f.poison_proc_chance = min(0.60, f.poison_proc_chance * boost)
    if f.stun_proc_chance > 0:
        f.stun_proc_chance = min(0.45, f.stun_proc_chance * boost)
    if f.damage_reroll_chance > 0:
        f.damage_reroll_chance = min(0.55, f.damage_reroll_chance * boost)
    if f.lifesteal_pct > 0:
        f.lifesteal_pct = min(0.40, f.lifesteal_pct * boost)
    if f.counter_chance > 0:
        f.counter_chance = min(0.55, f.counter_chance * boost)
    if f.regen_pct > 0:
        f.regen_pct = min(0.10, f.regen_pct * boost)
    if f.reflect_pct > 0:
        f.reflect_pct = min(0.45, f.reflect_pct * boost)
    if f.static_proc_chance > 0:
        f.static_proc_chance = min(0.65, f.static_proc_chance * boost)


# =============================================================================
# Battle result
# =============================================================================

@dataclass
class BattleResult:
    winner: Fighter | None      # None for a draw / timeout
    loser:  Fighter | None
    rounds: int
    xp_award: int               # XP granted to the winner's buddy (0 on draw)
    usd_award: float            # USD credited to the winning OWNER (0 on draw)
    log: list[str]              # human-readable play-by-play lines


# =============================================================================
# Engine
# =============================================================================

def _crit_chance(f: Fighter) -> float:
    """Base + spd scaling + level-gated lucky_crit bonus."""
    return BATTLE_CRIT_BASE + BATTLE_CRIT_SPD_SCALE * f.spd + f.crit_chance_bonus


def _crit_mult(f: Fighter) -> float:
    """Base crit multiplier + battle_focus bonus from level-gated unlocks."""
    return BATTLE_CRIT_MULT + f.crit_mult_bonus


def _roll_damage(attacker: Fighter, defender: Fighter, *, force_crit: bool) -> tuple[int, bool]:
    """Compute raw damage (pre-mitigation) for one hit. Returns (dmg, is_crit)."""
    base = attacker.atk * attacker.atk_mult
    roll = base * random.uniform(0.85, 1.15)

    # Glitch: reroll once and keep the higher.
    if attacker.damage_reroll_chance > 0 and random.random() < attacker.damage_reroll_chance:
        alt = base * random.uniform(0.85, 1.15)
        roll = max(roll, alt)

    is_crit = force_crit or random.random() < _crit_chance(attacker)
    if is_crit:
        roll *= _crit_mult(attacker)

    # Killing Blow (level-gated tertiary): +50% damage when defender HP
    # is below the killing_blow threshold. Stacks ADDITIVELY with
    # draclet's execute_30 (so a Lv 30 draclet with killing_blow gets
    # both bonuses below 25% HP).
    if attacker.killing_blow_bonus_pct > 0 and defender.hp > 0:
        if defender.hp / max(1, defender.max_hp) < attacker.killing_blow_thresh:
            roll *= 1.0 + attacker.killing_blow_bonus_pct

    return max(1, int(round(roll))), is_crit


def _heal_fighter(f: Fighter, amount: int) -> int:
    """Apply ``amount`` healing with the diminishing-returns soft cap.

    Once ``f.heal_total`` exceeds ``BATTLE_HEAL_SOFT_CAP_PCT * max_hp``,
    further heals are halved. Always heals at least 1 HP to keep flavor
    text honest. Returns the actual HP gained (post-cap).
    """
    if amount <= 0 or f.hp <= 0:
        return 0
    cap = int(BATTLE_HEAL_SOFT_CAP_PCT * f.max_hp)
    if f.heal_total >= cap:
        amount = max(1, amount // 2)
    old = f.hp
    f.hp = min(f.max_hp, f.hp + amount)
    actual = f.hp - old
    f.heal_total += actual
    return actual


def _apply_hit(
    attacker: Fighter, defender: Fighter, dmg: int, is_crit: bool,
    log_lines: list[str], *, hit_label: str = "hits",
) -> None:
    """Apply ``dmg`` to ``defender`` with ``dmg_taken_mult`` mitigation."""
    mitigated = max(1, int(round(dmg * defender.dmg_taken_mult)))
    defender.hp = max(0, defender.hp - mitigated)
    crit_tag = "  **CRIT!**" if is_crit else ""
    mit_tag = ""
    if defender.dmg_taken_mult < 1.0:
        mit_tag = f"  *(-{int((1 - defender.dmg_taken_mult) * 100)}%, armor)*"
    log_lines.append(
        f"  {attacker.emoji} {attacker.name} {hit_label} for "
        f"**{mitigated}** dmg{crit_tag}{mit_tag}  "
        f"({defender.name}: {defender.hp}/{defender.max_hp})"
    )

    # Jolt static_shock: chance to discharge bonus damage on every hit.
    if (attacker.static_proc_chance > 0
            and defender.hp > 0
            and random.random() < attacker.static_proc_chance):
        bonus_raw = mitigated * attacker.static_bonus_mult
        bonus = max(1, int(round(bonus_raw)))
        defender.hp = max(0, defender.hp - bonus)
        log_lines.append(
            f"  ⚡ **Static Shock** -- arcs for **{bonus}** bonus dmg "
            f"({defender.name}: {defender.hp}/{defender.max_hp})."
        )

    # Tortuga / Phantom reflect: defender bounces a fraction of damage
    # back at the attacker. Fires on the dealt-damage event so it scales
    # with the actual hit, not the raw roll.
    if (defender.reflect_pct > 0 and defender.hp > 0 and attacker.hp > 0
            and mitigated > 0):
        reflected = max(1, int(round(mitigated * defender.reflect_pct)))
        attacker.hp = max(0, attacker.hp - reflected)
        log_lines.append(
            f"  \U0001F6E1️ {defender.emoji} **{defender.name}** reflects "
            f"**{reflected}** dmg back ({attacker.name}: "
            f"{attacker.hp}/{attacker.max_hp})."
        )

    # Blazer lifesteal: attacker heals a % of damage actually delivered.
    if attacker.lifesteal_pct > 0 and mitigated > 0 and attacker.hp > 0:
        steal = max(1, int(round(mitigated * attacker.lifesteal_pct)))
        actual = _heal_fighter(attacker, steal)
        if actual > 0:
            log_lines.append(
                f"  {attacker.emoji} **Flame Drain** siphons **{actual}** HP "
                f"({attacker.name}: {attacker.hp}/{attacker.max_hp})."
            )

    # Wolf: low-HP rage arms the ATK buff once, permanently.
    if (
        defender.low_hp_rage_pending
        and defender.hp > 0
        and defender.hp * 2 < defender.max_hp
    ):
        defender.atk_mult *= 1.0 + defender.low_hp_rage_bonus
        defender.low_hp_rage_pending = False
        log_lines.append(
            f"  {defender.emoji} {defender.name} howls -- **Pack Howl** flips on "
            f"(ATK +{int(round(defender.low_hp_rage_bonus * 100))}%)."
        )

    # Berserker (level-gated tertiary): triggers earlier than wolf rage
    # and stacks with it. Fires once permanently on the defender side.
    if (
        defender.berserker_pending
        and defender.hp > 0
        and defender.hp / max(1, defender.max_hp) < defender.berserker_thresh
    ):
        defender.atk_mult *= 1.0 + defender.berserker_bonus
        defender.berserker_pending = False
        log_lines.append(
            f"  \U0001F525 {defender.emoji} {defender.name} goes **berserk** "
            f"(ATK +{int(round(defender.berserker_bonus * 100))}%)."
        )

    # Wecco: preen heals once when pushed below 30% HP, also buffs ATK.
    # Trigger threshold raised slightly (35% -> 30%) as part of healer
    # rebalance so it triggers later -- compensated by Lv 30 second_wind
    # which gives a second clutch heal when it unlocks.
    if (
        defender.ability_key == "preen_heal"
        and not defender.preen_used
        and defender.hp > 0
        and defender.hp * 100 < defender.max_hp * 30
    ):
        defender.preen_used = True
        heal_to = int(round(defender.max_hp * defender.preen_heal_pct))
        if heal_to > defender.hp:
            actual = _heal_fighter(defender, heal_to - defender.hp)
        else:
            actual = 0
        defender.atk_mult *= 1.0 + defender.preen_atk_bonus
        log_lines.append(
            f"  {defender.emoji} {defender.name} preens -- **Preen** triggers, "
            f"healed to {defender.hp}/{defender.max_hp} and ATK "
            f"+{int(round(defender.preen_atk_bonus * 100))}%."
        )

    # Second Wind (level-gated): a one-shot clutch heal at <30% HP.
    # Independent from preen_heal so a Lv 30 wecco can fire BOTH once
    # each, but the heal soft-cap means total sustain is bounded.
    if (
        defender.second_wind_pending
        and not defender.second_wind_used
        and defender.hp > 0
        and defender.hp / max(1, defender.max_hp) < 0.30
    ):
        defender.second_wind_used = True
        heal_amt = max(1, int(round(defender.max_hp * 0.25)))
        actual = _heal_fighter(defender, heal_amt)
        log_lines.append(
            f"  \U0001F4A8 {defender.emoji} **Second Wind** -- {defender.name} "
            f"recovers **{actual}** HP "
            f"({defender.hp}/{defender.max_hp})."
        )


def _attack(attacker: Fighter, defender: Fighter, log_lines: list[str]) -> None:
    """Resolve one full attack action from ``attacker`` against ``defender``.

    Honors dodge / first-strike / double-strike / poison-proc / stun-proc.
    Mutates both fighters' HP / status trackers.
    """
    # Defender dodge first -- full avoid, no status procs. Phantom's
    # phase_shift is a dodge that ALSO bounces a fraction of the
    # would-have-been damage back, so it reads as both an avoid and a
    # counter (see reflect_pct branch below).
    if defender.dodge_chance > 0 and random.random() < defender.dodge_chance:
        # Phase-shift reflect: a portion of the avoided damage hits back.
        if defender.phase_dodge_chance > 0 and defender.reflect_pct > 0 and attacker.hp > 0:
            # Use raw damage estimate so reflect packs a punch even on a dodge.
            est = max(1, int(round(attacker.atk * attacker.atk_mult)))
            reflected = max(1, int(round(est * defender.reflect_pct)))
            attacker.hp = max(0, attacker.hp - reflected)
            log_lines.append(
                f"  {attacker.emoji} {attacker.name} swings -- "
                f"{defender.emoji} **{defender.name}** phases through and "
                f"reflects **{reflected}** dmg back ({attacker.name}: "
                f"{attacker.hp}/{attacker.max_hp})."
            )
        else:
            dodge_label = "Phase Shift" if defender.ability_key == "phase_shift" else "Lucky Paw"
            log_lines.append(
                f"  {attacker.emoji} {attacker.name} swings -- "
                f"{defender.emoji} **{defender.name}** sidesteps via **{dodge_label}**!"
            )
        return

    force_crit = attacker.first_strike_pending
    if force_crit:
        attacker.first_strike_pending = False
        # Mimik's Ambush is the same mechanic with different flavor; the
        # ambush_pending flag also carries the +20% crit chance bonus
        # which was already added at prime time.
        if attacker.ambush_pending:
            attacker.ambush_pending = False
            log_lines.append(
                f"  \U0001F4E6 {attacker.emoji} **Ambush!** {attacker.name} "
                f"springs from cover for a guaranteed crit."
            )
        else:
            label = "First Strike"
            log_lines.append(
                f"  {attacker.emoji} {attacker.name} opens with **{label}** -- guaranteed crit."
            )

    if attacker.double_strike:
        # Octopus: two 65% hits so net ~130%. Each hit rolls crits /
        # procs independently, making it feel flashy.
        for hit_num in (1, 2):
            dmg, is_crit = _roll_damage(
                attacker, defender,
                force_crit=force_crit and hit_num == 1,
            )
            dmg = max(1, int(round(dmg * 0.65)))
            # Draclet execute: bonus on low-HP targets.
            if attacker.execute_thresh > 0 and defender.hp > 0:
                if defender.hp / max(1, defender.max_hp) < attacker.execute_thresh:
                    exec_bonus = max(1, int(round(dmg * attacker.execute_bonus_pct)))
                    dmg += exec_bonus
                    log_lines.append(
                        f"  {attacker.emoji} **Death Grip** execute triggers -- "
                        f"+{exec_bonus} on arm #{hit_num}!"
                    )
            _apply_hit(
                attacker, defender, dmg, is_crit, log_lines,
                hit_label=f"arm #{hit_num} hits",
            )
            _maybe_proc_on_hit(attacker, defender, log_lines)
            if defender.hp <= 0:
                return
    else:
        dmg, is_crit = _roll_damage(attacker, defender, force_crit=force_crit)
        # Draclet execute: extra damage when enemy is nearly dead.
        if attacker.execute_thresh > 0 and defender.hp > 0:
            if defender.hp / max(1, defender.max_hp) < attacker.execute_thresh:
                exec_bonus = max(1, int(round(dmg * attacker.execute_bonus_pct)))
                dmg += exec_bonus
                log_lines.append(
                    f"  {attacker.emoji} **Death Grip** execute -- "
                    f"+{exec_bonus} bonus "
                    f"({int(defender.hp / max(1, defender.max_hp) * 100)}% HP)!"
                )
        _apply_hit(attacker, defender, dmg, is_crit, log_lines)
        _maybe_proc_on_hit(attacker, defender, log_lines)


def _maybe_proc_on_hit(
    attacker: Fighter, defender: Fighter, log_lines: list[str],
) -> None:
    """Roll poison / stun side-effects from the attacker's ability."""
    if defender.hp <= 0:
        return
    if attacker.poison_proc_chance > 0 and defender.poison_turns == 0:
        if random.random() < attacker.poison_proc_chance:
            defender.poison_turns = 3
            log_lines.append(
                f"  {attacker.emoji} **Poison Fang** sinks in -- "
                f"{defender.name} is poisoned for 3 turns."
            )
    if attacker.stun_proc_chance > 0 and defender.stunned_turns == 0:
        if random.random() < attacker.stun_proc_chance:
            defender.stunned_turns = 1
            log_lines.append(
                f"  {attacker.emoji} **Pincer Grip** clamps down -- "
                f"{defender.name} is stunned and will skip their next turn."
            )
    # Thornling counter: defender retaliates immediately for a portion of their ATK.
    if defender.counter_chance > 0 and defender.hp > 0 and attacker.hp > 0:
        if random.random() < defender.counter_chance:
            c_dmg = max(1, int(round(
                defender.atk * defender.atk_mult * defender.counter_pct
            )))
            attacker.hp = max(0, attacker.hp - c_dmg)
            log_lines.append(
                f"  {defender.emoji} **Prickle Back** -- {defender.name} "
                f"retaliates for **{c_dmg}** dmg "
                f"({attacker.name}: {attacker.hp}/{attacker.max_hp})."
            )
            # Let wolf/wecco triggers fire on the counter target too.
            if (
                attacker.low_hp_rage_pending
                and attacker.hp > 0
                and attacker.hp * 2 < attacker.max_hp
            ):
                attacker.atk_mult *= 1.0 + attacker.low_hp_rage_bonus
                attacker.low_hp_rage_pending = False
                log_lines.append(
                    f"  {attacker.emoji} {attacker.name} howls -- "
                    f"**Pack Howl** flips on!"
                )


def _maybe_fire_pre_turn_ability(
    attacker: Fighter, defender: Fighter, log_lines: list[str],
) -> None:
    """One-shot active abilities that trigger at the start of the fighter's turn."""
    if attacker.ability_key == "ink_atk_debuff_20" and not attacker.ink_used:
        attacker.ink_used = True
        defender.atk_mult *= 1.0 - attacker.ink_atk_reduction
        log_lines.append(
            f"  {attacker.emoji} **Ink Cloud** bursts -- "
            f"{defender.name}'s ATK is cut by "
            f"{int(round(attacker.ink_atk_reduction * 100))}% for the rest of the battle."
        )
    elif attacker.ability_key == "rain_skip_2" and not attacker.rain_used:
        attacker.rain_used = True
        defender.stunned_turns = max(defender.stunned_turns, 2)
        log_lines.append(
            f"  {attacker.emoji} **Rain Dance** -- a torrential downpour pins "
            f"{defender.name} for 2 turns."
        )


def _tick_poison(f: Fighter, log_lines: list[str]) -> None:
    """End-of-round poison damage, if any, on ``f``."""
    if f.hp <= 0 or f.poison_turns <= 0:
        return
    dmg = max(1, int(round(f.max_hp * 0.05)))
    f.hp = max(0, f.hp - dmg)
    f.poison_turns -= 1
    log_lines.append(
        f"  {f.emoji} {f.name} takes **{dmg}** poison damage "
        f"({f.hp}/{f.max_hp}, {f.poison_turns} turn(s) left)."
    )


def _tick_regen(f: Fighter, log_lines: list[str]) -> None:
    """End-of-round regen heal for gloomer's Lunar Regen + verdant's
    Photo Synth + the swift_recovery secondary unlock.

    Capped at BATTLE_REGEN_HP_CAP_PCT of max_hp -- once the fighter is
    above that threshold the tick is a no-op so regen buddies have to
    take damage for healing to do anything (no free top-up at full HP).
    """
    if f.hp <= 0 or f.regen_pct <= 0:
        return
    if f.hp / max(1, f.max_hp) >= BATTLE_REGEN_HP_CAP_PCT:
        return
    heal = max(1, int(round(f.max_hp * f.regen_pct)))
    actual = _heal_fighter(f, heal)
    if actual <= 0:
        return
    label = "Lunar Regen" if f.ability_key == "regen_3pct" else (
        "Photo Synth" if f.ability_key == "photo_synth" else "Swift Recovery"
    )
    log_lines.append(
        f"  {f.emoji} {f.name} **{label}** heals "
        f"**{actual}** HP ({f.hp}/{f.max_hp})."
    )


def _tick_overclock(f: Fighter, round_num: int, log_lines: list[str]) -> None:
    """Per-round ATK ramp. Robo (Overclock) goes every 3rd round; verdant
    (Photo Synth) goes every round. Cadence lives on Fighter.atk_up_every_n_rounds.
    """
    if f.hp <= 0 or f.atk_up_per_stack <= 0:
        return
    cadence = max(1, int(f.atk_up_every_n_rounds or 3))
    if round_num % cadence != 0 or f.atk_up_stacks >= f.atk_up_max_stacks:
        return
    f.atk_up_stacks += 1
    f.atk_mult *= 1.0 + f.atk_up_per_stack
    label = "Overclock" if f.ability_key == "atk_up_3rounds" else "Photo Synth"
    log_lines.append(
        f"  {f.emoji} **{label}** x{f.atk_up_stacks} -- "
        f"{f.name} ATK +{int(round(f.atk_up_per_stack * 100))}%!"
    )


def _xp_reward(winner: Fighter, loser: Fighter) -> int:
    """Punch-up-friendly XP formula. See BATTLE_XP_SCALE in config."""
    if winner.level <= 0:
        return BATTLE_XP_MIN
    ratio = loser.level / max(1, winner.level)
    return max(BATTLE_XP_MIN, int(round(BATTLE_XP_SCALE * ratio)))


def _usd_reward(winner: Fighter, loser: Fighter) -> float:
    """Punch-up-friendly USD prize, same ratio shape as XP.

    Mirrors _xp_reward so a low-level winner beating a high-level buddy
    takes a sizeable prize and a high-level bully grinding low-level
    buddies gets pennies. Capped at BATTLE_USD_MAX so edge-case ratios
    (e.g. L1 beats L100) can't print arbitrary dollars.
    """
    if winner.level <= 0:
        return BATTLE_USD_MIN
    ratio = loser.level / max(1, winner.level)
    prize = round(BATTLE_USD_SCALE * ratio, 2)
    return float(max(BATTLE_USD_MIN, min(BATTLE_USD_MAX, prize)))


# =============================================================================
# Stepwise battle (Buddy Battles expansion)
# =============================================================================
# ``run_battle`` simulates an entire battle in one synchronous call,
# which is fine for the legacy arena where the engine returns a log
# and the cog renders it. The new zone / tournament battle view needs
# to interject *between* rounds so the player can select consumables
# from a dropdown -- the round needs to pause, wait for a UI event,
# then resume. ``StepBattle`` is a thin generator-style state machine
# over the same helpers (_attack / _tick_poison / _tick_regen /
# _tick_overclock) so consumables and FPS animation hooks can sit in
# the cog without duplicating the engine math.

@dataclass
class StepBattle:
    """Per-round battle state. The cog owns one of these per active fight."""
    f1: Fighter
    f2: Fighter
    round_num: int = 0
    over: bool = False
    log_lines: list[str] = field(default_factory=list)

    @classmethod
    def from_rows(cls, p1_row: dict, p2_row: dict) -> "StepBattle":
        f1 = Fighter.from_row(p1_row)
        f2 = Fighter.from_row(p2_row)
        return cls(f1=f1, f2=f2)

    def winner(self) -> Fighter | None:
        if self.f1.hp <= 0 and self.f2.hp <= 0:
            return None
        if self.f1.hp <= 0:
            return self.f2
        if self.f2.hp <= 0:
            return self.f1
        if self.round_num >= BATTLE_MAX_ROUNDS:
            p1_pct = self.f1.hp / max(1, self.f1.max_hp)
            p2_pct = self.f2.hp / max(1, self.f2.max_hp)
            if p1_pct > p2_pct:
                return self.f1
            if p2_pct > p1_pct:
                return self.f2
            return None
        return None

    def loser(self) -> Fighter | None:
        w = self.winner()
        if w is None:
            return None
        return self.f2 if w is self.f1 else self.f1


# =============================================================================
# PvE per-action constants (Strike / Special / Brace / Risky)
# =============================================================================
# Map / tournament / wild battles use the same 4-action vocabulary as PvP
# (cogs.buddy._pvp_apply) so the player picks identical moves regardless
# of opponent type. Damage windows here MUST match
# _PVP_STRIKE_RANGE / _PVP_SPECIAL_RANGE / _PVP_RISKY_RANGE in
# cogs/buddy.py -- the two paths share a single source of truth via these
# names; PvP imports the same constants.

PVE_STRIKE_RANGE:  tuple[float, float] = (0.85, 1.15)
PVE_SPECIAL_RANGE: tuple[float, float] = (1.55, 1.95)
PVE_RISKY_RANGE:   tuple[float, float] = (2.20, 2.80)
PVE_BRACE_HEAL: float = 0.08
PVE_RISKY_HIT:  float = 0.60
PVE_RISKY_MISS: float = 0.25
PVE_SPECIAL_COST: int = 2
PVE_STAMINA_MAX:  int = 5


@dataclass
class PveActionState:
    """Player-side action state for a stepwise PvE battle.

    Carried by the cog's battle view across rounds. Mirrors the
    stamina/brace tracking that the PvP _PvpBattle uses so picking moves
    feels identical in zone, boss, tournament, and wild battles.
    """
    stamina: int = 0
    brace_next: bool = False
    actions_used: set[str] = field(default_factory=set)


def step_round(b: StepBattle) -> list[str]:
    """Resolve ONE round on ``b`` in place. Returns the new log lines.

    Auto-resolve flow used by arena / wild encounters that have no
    interactive UI. Both fighters attack via _attack and the engine
    runs all abilities normally. For interactive battles (map, boss,
    tournament) call ``step_round_with_player_action`` instead so the
    player picks Strike / Special / Brace / Risky each round.
    """
    if b.over or b.f1.hp <= 0 or b.f2.hp <= 0:
        b.over = True
        return []
    if b.round_num >= BATTLE_MAX_ROUNDS:
        b.over = True
        return []

    b.round_num += 1
    new_lines: list[str] = [f"__**Round {b.round_num}**__"]

    if b.f1.spd == b.f2.spd:
        first, second = (b.f1, b.f2) if random.random() < 0.5 else (b.f2, b.f1)
    else:
        first, second = (b.f1, b.f2) if b.f1.spd > b.f2.spd else (b.f2, b.f1)

    for attacker, defender in ((first, second), (second, first)):
        if attacker.hp <= 0 or defender.hp <= 0:
            continue
        if attacker.stunned_turns > 0:
            attacker.stunned_turns -= 1
            new_lines.append(
                f"  {attacker.emoji} {attacker.name} is held in place "
                f"and skips their turn."
            )
            continue
        _maybe_fire_pre_turn_ability(attacker, defender, new_lines)
        _attack(attacker, defender, new_lines)
        # Zenny bonus
        if (
            attacker.ability_key == "extra_turn_every_3"
            and b.round_num % 3 == 0
            and defender.hp > 0
        ):
            new_lines.append(
                f"  {attacker.emoji} **Chatterbox** -- {attacker.name} "
                f"squawks out a bonus attack!"
            )
            _attack(attacker, defender, new_lines)

    _tick_poison(b.f1, new_lines)
    _tick_poison(b.f2, new_lines)
    _tick_regen(b.f1, new_lines)
    _tick_regen(b.f2, new_lines)
    _tick_overclock(b.f1, b.round_num, new_lines)
    _tick_overclock(b.f2, b.round_num, new_lines)
    new_lines.append("")

    b.log_lines.extend(new_lines)
    if b.f1.hp <= 0 or b.f2.hp <= 0 or b.round_num >= BATTLE_MAX_ROUNDS:
        b.over = True
    return new_lines


def step_round_with_player_action(
    b: StepBattle, action: str, state: PveActionState,
) -> list[str]:
    """Resolve ONE round where f1 is the human player picking an action.

    ``action`` is one of ``"strike" / "special" / "brace" / "risky"`` --
    the same vocabulary used by the PvP view in cogs/buddy.py. ``state``
    carries the player's stamina + brace flag across rounds.

    The enemy uses the engine's standard ``_attack`` so its primary,
    secondary, and tertiary abilities (Pack Howl, Preen, Berserker,
    Ink Cloud, Rain Dance, Second Wind, Killing Blow, etc.) all fire
    exactly as they do in any other battle type. Brace halves the
    incoming hit by temporarily dropping the player's dmg_taken_mult,
    which keeps the effect compatible with abilities that also touch
    dmg_taken_mult (like Hard Shell).
    """
    if b.over or b.f1.hp <= 0 or b.f2.hp <= 0:
        b.over = True
        return []
    if b.round_num >= BATTLE_MAX_ROUNDS:
        b.over = True
        return []

    b.round_num += 1
    new_lines: list[str] = [f"__**Round {b.round_num}**__"]
    state.actions_used.add(action)

    if b.f1.spd == b.f2.spd:
        player_first = random.random() < 0.5
    else:
        player_first = b.f1.spd > b.f2.spd

    def _player_turn() -> None:
        if b.f1.hp <= 0 or b.f2.hp <= 0:
            return
        if b.f1.stunned_turns > 0:
            b.f1.stunned_turns -= 1
            new_lines.append(
                f"  {b.f1.emoji} {b.f1.name} is held in place and skips this turn."
            )
            return
        _maybe_fire_pre_turn_ability(b.f1, b.f2, new_lines)
        _apply_player_action(b, action, state, new_lines)

    def _enemy_turn() -> None:
        if b.f1.hp <= 0 or b.f2.hp <= 0:
            return
        if b.f2.stunned_turns > 0:
            b.f2.stunned_turns -= 1
            new_lines.append(
                f"  {b.f2.emoji} {b.f2.name} is held in place and skips their turn."
            )
            return
        _maybe_fire_pre_turn_ability(b.f2, b.f1, new_lines)
        if state.brace_next:
            orig = b.f1.dmg_taken_mult
            b.f1.dmg_taken_mult = orig * 0.5
            try:
                _attack(b.f2, b.f1, new_lines)
            finally:
                b.f1.dmg_taken_mult = orig
            state.brace_next = False
            new_lines.append(
                f"  \U0001F6E1️ {b.f1.emoji} braced -- damage halved."
            )
        else:
            _attack(b.f2, b.f1, new_lines)
        if (
            b.f2.ability_key == "extra_turn_every_3"
            and b.round_num % 3 == 0
            and b.f1.hp > 0
        ):
            new_lines.append(
                f"  {b.f2.emoji} **Chatterbox** -- {b.f2.name} squawks a bonus attack!"
            )
            _attack(b.f2, b.f1, new_lines)

    if player_first:
        _player_turn()
        _enemy_turn()
    else:
        _enemy_turn()
        _player_turn()

    _tick_poison(b.f1, new_lines)
    _tick_poison(b.f2, new_lines)
    _tick_regen(b.f1, new_lines)
    _tick_regen(b.f2, new_lines)
    _tick_overclock(b.f1, b.round_num, new_lines)
    _tick_overclock(b.f2, b.round_num, new_lines)
    new_lines.append("")

    b.log_lines.extend(new_lines)
    if b.f1.hp <= 0 or b.f2.hp <= 0 or b.round_num >= BATTLE_MAX_ROUNDS:
        b.over = True
    return new_lines


def _apply_player_action(
    b: StepBattle, action: str, state: PveActionState, log_lines: list[str],
) -> None:
    """Apply the player's chosen action against ``b.f2``.

    Wires Strike / Special / Brace / Risky into the engine helpers so
    species abilities (lifesteal, static_shock, reflect, counter,
    low_hp_rage, preen, berserker, second_wind, killing_blow) trigger
    the same way they do for an auto-resolved engine attack.
    """
    atk, defn = b.f1, b.f2

    if action == "brace":
        state.brace_next = True
        heal = max(1, int(round(atk.max_hp * PVE_BRACE_HEAL)))
        actual = _heal_fighter(atk, heal)
        state.stamina = min(PVE_STAMINA_MAX, state.stamina + 1)
        log_lines.append(
            f"  \U0001F6E1 {atk.emoji} **{atk.name}** braces, healing "
            f"**{actual}** HP ({atk.hp}/{atk.max_hp}); next hit halved."
        )
        return

    if action == "special" and state.stamina < PVE_SPECIAL_COST:
        action = "strike"

    if action == "strike":
        _player_damage_swing(
            atk, defn, PVE_STRIKE_RANGE, log_lines,
            hit_label="strikes",
        )
        state.stamina = min(PVE_STAMINA_MAX, state.stamina + 1)
        return

    if action == "special":
        state.stamina -= PVE_SPECIAL_COST
        ability = (atk.ability_name or "Special").strip() or "Special"
        _player_damage_swing(
            atk, defn, PVE_SPECIAL_RANGE, log_lines,
            hit_label=f"unleashes **{ability}** for",
            prefix_emoji="\U0001F4A5 ",
        )
        return

    if action == "risky":
        roll = random.random()
        if roll < PVE_RISKY_HIT:
            _player_damage_swing(
                atk, defn, PVE_RISKY_RANGE, log_lines,
                hit_label="lands a **RISKY** hit for",
                prefix_emoji="\U0001F3AF ",
            )
        elif roll < PVE_RISKY_HIT + PVE_RISKY_MISS:
            log_lines.append(
                f"  \U0001F4A8 {atk.emoji} {atk.name} tries a Risky -- whiff!"
            )
        else:
            recoil = max(1, int(round(atk.atk * 0.45)))
            atk.hp = max(0, atk.hp - recoil)
            log_lines.append(
                f"  \U0001F4A2 {atk.emoji} {atk.name}'s Risky backfires for "
                f"**{recoil}** self-dmg ({atk.hp}/{atk.max_hp})."
            )


def _player_damage_swing(
    atk: Fighter, defn: Fighter, dmg_range: tuple[float, float],
    log_lines: list[str], *, hit_label: str, prefix_emoji: str = "",
) -> None:
    """Roll + apply one hit from the player using the engine's full kit.

    Honors defender dodge, attacker first_strike / ambush, glitch damage
    reroll, crit chance + mult (including lucky_crit / battle_focus
    bonuses), killing_blow, draclet execute, and then routes through
    ``_apply_hit`` + ``_maybe_proc_on_hit`` for poison / stun / static /
    reflect / counter / lifesteal / preen / berserker / second_wind
    triggers. Same code paths the engine's ``_attack`` uses; the only
    difference is the damage-window range and the hit_label flavor.
    """
    if defn.dodge_chance > 0 and random.random() < defn.dodge_chance:
        if defn.phase_dodge_chance > 0 and defn.reflect_pct > 0 and atk.hp > 0:
            est = max(1, int(round(atk.atk * atk.atk_mult)))
            reflected = max(1, int(round(est * defn.reflect_pct)))
            atk.hp = max(0, atk.hp - reflected)
            log_lines.append(
                f"  {atk.emoji} {atk.name} swings -- "
                f"{defn.emoji} **{defn.name}** phases through and "
                f"reflects **{reflected}** dmg back ({atk.name}: "
                f"{atk.hp}/{atk.max_hp})."
            )
        else:
            dodge_label = "Phase Shift" if defn.ability_key == "phase_shift" else "Lucky Paw"
            log_lines.append(
                f"  {atk.emoji} {atk.name} swings -- "
                f"{defn.emoji} **{defn.name}** sidesteps via **{dodge_label}**!"
            )
        return

    force_crit = atk.first_strike_pending
    if force_crit:
        atk.first_strike_pending = False
        if atk.ambush_pending:
            atk.ambush_pending = False
            log_lines.append(
                f"  \U0001F4E6 {atk.emoji} **Ambush!** {atk.name} springs from "
                f"cover for a guaranteed crit."
            )
        else:
            log_lines.append(
                f"  {atk.emoji} {atk.name} opens with **First Strike** -- guaranteed crit."
            )

    lo, hi = dmg_range
    base = atk.atk * atk.atk_mult
    roll = base * random.uniform(lo, hi)
    if atk.damage_reroll_chance > 0 and random.random() < atk.damage_reroll_chance:
        alt = base * random.uniform(lo, hi)
        roll = max(roll, alt)
    is_crit = force_crit or random.random() < _crit_chance(atk)
    if is_crit:
        roll *= _crit_mult(atk)
    if atk.killing_blow_bonus_pct > 0 and defn.hp > 0:
        if defn.hp / max(1, defn.max_hp) < atk.killing_blow_thresh:
            roll *= 1.0 + atk.killing_blow_bonus_pct
    dmg = max(1, int(round(roll)))
    if atk.execute_thresh > 0 and defn.hp > 0:
        if defn.hp / max(1, defn.max_hp) < atk.execute_thresh:
            exec_bonus = max(1, int(round(dmg * atk.execute_bonus_pct)))
            dmg += exec_bonus
            log_lines.append(
                f"  {atk.emoji} **Death Grip** execute -- +{exec_bonus} bonus!"
            )
    _apply_hit(
        atk, defn, dmg, is_crit, log_lines,
        hit_label=f"{prefix_emoji}{hit_label}",
    )
    _maybe_proc_on_hit(atk, defn, log_lines)


def finalize_step_battle(b: StepBattle) -> BattleResult:
    """Resolve final winner + XP/USD on a finished StepBattle."""
    winner = b.winner()
    loser = b.loser()
    xp  = _xp_reward(winner, loser)  if winner and loser else 0
    usd = _usd_reward(winner, loser) if winner and loser else 0.0
    return BattleResult(
        winner=winner, loser=loser, rounds=b.round_num,
        xp_award=int(xp), usd_award=float(usd), log=b.log_lines,
    )


def run_battle(p1_row: dict, p2_row: dict, *, rng: Any = None) -> BattleResult:
    """Simulate a full battle between two buddy rows.

    ``rng`` lets tests pin the random source. In production we use the
    module-level ``random`` which the engine imports once.
    """
    if rng is not None:
        # Swap in the test RNG for the duration of this call.
        global random  # noqa: PLW0603  -- scoped swap, restored in finally
        _old = random
        random = rng
        try:
            return _run(p1_row, p2_row)
        finally:
            random = _old
    return _run(p1_row, p2_row)


def _run(p1_row: dict, p2_row: dict) -> BattleResult:
    f1 = Fighter.from_row(p1_row)
    f2 = Fighter.from_row(p2_row)

    # Include the species under each fighter so a renamed buddy ("Shrek"
    # the wolf) doesn't make the species-bound abilities look untethered.
    def _intro(f: Fighter) -> str:
        sp = f.species.title() if f.species else ""
        sp_tag = f" the {sp}" if sp and sp.lower() != f.name.lower() else ""
        return (
            f"{f.emoji} **{f.name}**{sp_tag}  *({f.tier_name} Lv. {f.level}, "
            f"HP {f.max_hp}, ATK {int(f.atk)})*"
        )

    log_lines: list[str] = [
        _intro(f1),
        f"vs",
        _intro(f2),
        "",
    ]
    if f1.ability_name:
        log_lines.append(f"> {f1.emoji} Ability: **{f1.ability_name}**")
    if f2.ability_name:
        log_lines.append(f"> {f2.emoji} Ability: **{f2.ability_name}**")
    log_lines.append("")

    round_num = 0
    while round_num < BATTLE_MAX_ROUNDS and f1.hp > 0 and f2.hp > 0:
        round_num += 1
        log_lines.append(f"__**Round {round_num}**__")

        # Turn order: higher SPD goes first, ties break on d100.
        if f1.spd == f2.spd:
            first, second = (f1, f2) if random.random() < 0.5 else (f2, f1)
        else:
            first, second = (f1, f2) if f1.spd > f2.spd else (f2, f1)

        for attacker, defender in ((first, second), (second, first)):
            if attacker.hp <= 0 or defender.hp <= 0:
                continue
            if attacker.stunned_turns > 0:
                attacker.stunned_turns -= 1
                log_lines.append(
                    f"  {attacker.emoji} {attacker.name} is held in place "
                    f"and skips their turn."
                )
                continue
            _maybe_fire_pre_turn_ability(attacker, defender, log_lines)
            _attack(attacker, defender, log_lines)
            # Zenny: bonus attack every 3rd round (after the normal one).
            if (
                attacker.ability_key == "extra_turn_every_3"
                and round_num % 3 == 0
                and defender.hp > 0
            ):
                log_lines.append(
                    f"  {attacker.emoji} **Chatterbox** -- {attacker.name} "
                    f"squawks out a bonus attack!"
                )
                _attack(attacker, defender, log_lines)

        # End-of-round ticks: poison, regen, overclock.
        _tick_poison(f1, log_lines)
        _tick_poison(f2, log_lines)
        _tick_regen(f1, log_lines)
        _tick_regen(f2, log_lines)
        _tick_overclock(f1, round_num, log_lines)
        _tick_overclock(f2, round_num, log_lines)
        log_lines.append("")

    # Winner resolution.
    if f1.hp <= 0 and f2.hp <= 0:
        winner = loser = None
    elif f1.hp <= 0:
        winner, loser = f2, f1
    elif f2.hp <= 0:
        winner, loser = f1, f2
    else:
        # Timed out on rounds -- whoever has more remaining HP % wins.
        p1_pct = f1.hp / f1.max_hp
        p2_pct = f2.hp / f2.max_hp
        if p1_pct > p2_pct:
            winner, loser = f1, f2
        elif p2_pct > p1_pct:
            winner, loser = f2, f1
        else:
            winner = loser = None

    xp  = _xp_reward(winner, loser)  if winner and loser else 0
    usd = _usd_reward(winner, loser) if winner and loser else 0.0

    if winner is None:
        log_lines.append("**Draw.**  Both buddies walk away sore but dignified.")
    else:
        log_lines.append(
            f"**Winner:** {winner.emoji} **{winner.name}** "
            f"({winner.hp}/{winner.max_hp} HP left)  -  "
            f"earns **{xp}** XP and **${usd:,.2f}**."
        )

    return BattleResult(
        winner=winner, loser=loser, rounds=round_num,
        xp_award=xp, usd_award=usd, log=log_lines,
    )


# =============================================================================
# Persistence helper
# =============================================================================

async def award_battle_xp(
    db: Any, guild_id: int, winner_owner_id: int, winner_buddy_id: int, xp: int,
) -> None:
    """Add ``xp`` to the winner's buddy row.

    Separate from ``run_battle`` so the engine stays pure and tests can
    skip the DB entirely. Does not recompute level -- the chat-XP path in
    cogs/buddy.py already persists level drift on its next tick and we
    don't want the battle to announce a level-up in a different embed.

    Wins / losses / battle_count / last_battle_at are persisted by
    :func:`record_battle_result`; this function is kept narrow so the XP
    award can still be called on its own in tests.
    """
    if xp <= 0:
        return
    try:
        await db.execute(
            "UPDATE cc_buddies SET "
            "  xp = xp + $3, "
            "  level = GREATEST("
            "      level, "
            "      LEAST(50, GREATEST(1, "
            "          FLOOR((1.0 + SQRT("
            "              1.0 + 8.0 * (xp + $3)::double precision / 120.0"
            "          )) / 2.0)::int"
            "      ))"
            "  ), "
            "  updated_at = NOW() "
            "WHERE id = $1 AND owner_user_id = $2 AND status = 'owned'",
            winner_buddy_id, winner_owner_id, xp,
        )
    except Exception:
        log.exception(
            "award_battle_xp failed gid=%s uid=%s buddy_id=%s xp=%s",
            guild_id, winner_owner_id, winner_buddy_id, xp,
        )


async def record_pve_battle_result(
    db: Any,
    *,
    player_buddy_id: int | None,
    won: bool,
    rounds: int = 0,
) -> None:
    """Persist a PvE result for a single buddy (no opponent row exists).

    Used by the four PvE buddy-battle surfaces (fish wild battle, delve
    wild battle, arena, escaped-buddy event win) so cc_buddies.wins /
    losses / battle_count stay unified across every fight a buddy
    participates in -- not just PvP. Bloodstone XP grants follow the
    same path as ``record_battle_result``.

    Never raises; DB errors are logged and swallowed.
    """
    if not player_buddy_id:
        return
    try:
        row = await db.fetch_one(
            "SELECT id, owner_user_id, guild_id FROM cc_buddies WHERE id = $1",
            int(player_buddy_id),
        )
        if row and int(row.get("owner_user_id") or 0) and int(row.get("guild_id") or 0):
            from services import themed_stones as _ts
            await _ts.grant_bloodstone_xp(
                db, int(row["owner_user_id"]), int(row["guild_id"]),
                rounds=int(rounds), won=bool(won), lost=not bool(won),
            )
    except Exception:
        log.debug(
            "record_pve_battle_result: bloodstone XP failed",
            exc_info=True,
        )
    try:
        if won:
            await db.execute(
                "UPDATE cc_buddies SET "
                "  wins          = wins + 1, "
                "  battle_count  = battle_count + 1, "
                "  last_battle_at = NOW(), "
                "  updated_at     = NOW() "
                "WHERE id = $1",
                int(player_buddy_id),
            )
        else:
            await db.execute(
                "UPDATE cc_buddies SET "
                "  losses        = losses + 1, "
                "  battle_count  = battle_count + 1, "
                "  last_battle_at = NOW(), "
                "  updated_at     = NOW() "
                "WHERE id = $1",
                int(player_buddy_id),
            )
    except Exception:
        log.exception(
            "record_pve_battle_result failed buddy_id=%s won=%s",
            player_buddy_id, won,
        )


async def record_battle_result(
    db: Any,
    *,
    winner_buddy_id: int | None,
    loser_buddy_id: int | None,
    rounds: int = 0,
) -> None:
    """Persist the battle record (W / L / battle_count / last_battle_at).

    Draws are represented by both ids being None (rare -- only on mutual
    KO or hp-tie timeout). In that case we still bump both fighters'
    battle_count + last_battle_at so the "last battle" timestamp on the
    panel doesn't skip the event, but no one scores a win or loss.

    ``rounds`` is the number of rounds the battle ran for; used to scale
    Bloodstone XP for both owners. Optional so older callers that don't
    know the round count keep working (they just grant the win/loss XP
    without the per-round chunk).

    Called by ``cogs/buddy.py`` after ``run_battle`` resolves. Never
    raises; DB errors are logged and swallowed so a record-keeping
    failure can't undo the battle that already happened.
    """
    ids = [i for i in (winner_buddy_id, loser_buddy_id) if i]
    if not ids:
        return
    # Themed Bloodstone XP for each combatant's owner. We resolve the
    # owner from the buddy row up front so the XP grant uses the SAME
    # identity that scored the W/L below. Best-effort.
    try:
        owners: dict[int, dict] = {}
        for bid in ids:
            try:
                row = await db.fetch_one(
                    "SELECT id, owner_user_id, guild_id FROM cc_buddies WHERE id = $1",
                    int(bid),
                )
            except Exception:
                row = None
            if row:
                owners[int(row["id"])] = {
                    "owner_user_id": int(row.get("owner_user_id") or 0),
                    "guild_id":      int(row.get("guild_id") or 0),
                }
        from services import themed_stones as _ts
        for bid, info in owners.items():
            won = (winner_buddy_id is not None and bid == int(winner_buddy_id))
            lost = (loser_buddy_id  is not None and bid == int(loser_buddy_id))
            if not (info["owner_user_id"] and info["guild_id"]):
                continue
            await _ts.grant_bloodstone_xp(
                db, info["owner_user_id"], info["guild_id"],
                rounds=int(rounds), won=won, lost=lost,
            )
    except Exception:
        log.debug(
            "buddy_battle: themed_stones.grant_bloodstone_xp failed",
            exc_info=True,
        )
    try:
        if winner_buddy_id and loser_buddy_id:
            await db.execute(
                "UPDATE cc_buddies SET "
                "  wins          = wins + CASE WHEN id = $1 THEN 1 ELSE 0 END, "
                "  losses        = losses + CASE WHEN id = $2 THEN 1 ELSE 0 END, "
                "  battle_count  = battle_count + 1, "
                "  last_battle_at = NOW(), "
                "  updated_at     = NOW() "
                "WHERE id = ANY($3::bigint[])",
                winner_buddy_id, loser_buddy_id, ids,
            )
        else:
            # Draw (or half-known): bump battle_count + last_battle_at only.
            await db.execute(
                "UPDATE cc_buddies SET "
                "  battle_count  = battle_count + 1, "
                "  last_battle_at = NOW(), "
                "  updated_at     = NOW() "
                "WHERE id = ANY($1::bigint[])",
                ids,
            )
    except Exception:
        log.exception(
            "record_battle_result failed winner_id=%s loser_id=%s",
            winner_buddy_id, loser_buddy_id,
        )


# =============================================================================
# Interactive (turn-based) battle helpers
# =============================================================================
# Shared by cogs/fishing.py (wild buddy battles), cogs/dungeon.py (delve wild
# buddy battles), and cogs/buddy.py (buddy arena). Each cog wraps these in its
# own discord.ui.View so the per-domain reward / capture / resolution logic
# stays where it belongs while the combat math lives in one place.

INTERACTIVE_BATTLE_MAX_ROUNDS: int = 25
INTERACTIVE_PLAYER_STAMINA_MAX: int = 5
INTERACTIVE_SPECIAL_STAMINA_COST: int = 2
INTERACTIVE_BRACE_HEAL_PCT: float = 0.08

INTERACTIVE_STRIKE_RANGE: tuple[float, float]  = (0.85, 1.15)
INTERACTIVE_SPECIAL_RANGE: tuple[float, float] = (1.55, 1.95)
INTERACTIVE_RISKY_RANGE: tuple[float, float]   = (2.20, 2.80)
INTERACTIVE_RISKY_HIT_CHANCE: float            = 0.60
INTERACTIVE_RISKY_MISS_CHANCE: float           = 0.25  # remainder = backfire

# Performance-bonus thresholds used by compute_battle_bonus.
INTERACTIVE_BONUS_FAST_ROUNDS: tuple[int, int]    = (4, 7)
INTERACTIVE_BONUS_FAST_PCT:    tuple[float, float] = (0.30, 0.15)
INTERACTIVE_BONUS_HP_PCT:      tuple[float, float] = (0.75, 0.50)
INTERACTIVE_BONUS_HP_VALUE:    tuple[float, float] = (0.25, 0.10)
INTERACTIVE_BONUS_VARIETY_THRESHOLD: int           = 3
INTERACTIVE_BONUS_VARIETY_PCT: float               = 0.15


@dataclass
class LiveBattle:
    """In-memory per-view state for an interactive turn-based battle."""
    player: Fighter
    enemy: Fighter
    player_stamina: int = 0
    player_brace_next: bool = False
    enemy_brace_next: bool = False
    round_num: int = 1
    actions_used: set = field(default_factory=set)
    log_lines: list = field(default_factory=list)

    def __post_init__(self) -> None:
        # Endurance Charm: gear-driven start_stamina bonus is applied
        # once at battle start. Capped at the interactive max.
        bonus = int(getattr(self.player, "start_stamina_bonus", 0) or 0)
        if bonus > 0:
            self.player_stamina = min(
                INTERACTIVE_PLAYER_STAMINA_MAX,
                self.player_stamina + bonus,
            )

    # Per-battle brace heal % (mutable so arena modifiers like Bloodbath
    # can disable healing without touching the engine constant).
    brace_heal_pct: float = INTERACTIVE_BRACE_HEAL_PCT

    def is_over(self) -> bool:
        return (
            self.player.hp <= 0
            or self.enemy.hp <= 0
            or self.round_num > INTERACTIVE_BATTLE_MAX_ROUNDS
        )

    def player_won(self) -> bool:
        return self.player.hp > 0 and self.enemy.hp <= 0


def hp_bar(cur: int, mx: int, width: int = 12) -> str:
    """ASCII HP bar with percentage. Renders like ``[████████░░░░] 67%``."""
    if mx <= 0:
        return f"[{'░' * width}]   0%"
    cur = max(0, min(cur, mx))
    pct = cur / mx
    filled = int(round(width * pct))
    return f"[{'█' * filled}{'░' * (width - filled)}] {int(pct * 100):3d}%"


def apply_player_action(b: LiveBattle, action: str) -> list[str]:
    """Resolve a player action against ``b.enemy``; return new log lines.

    Each action mutates ``b`` (player stamina, brace flag, enemy.hp).
    Damage windows live at module scope for easy balance tuning.
    """
    b.actions_used.add(action)
    p = b.player
    e = b.enemy
    p_emoji = p.emoji or "🦆"
    e_emoji = e.emoji or "🐙"
    lines: list[str] = []

    def _apply_execute(base_dmg: int) -> int:
        """Add execute bonus when enemy HP is below threshold."""
        if p.execute_thresh > 0 and e.hp > 0:
            if e.hp / max(1, e.max_hp) < p.execute_thresh:
                bonus = max(1, int(round(base_dmg * p.execute_bonus_pct)))
                lines.append(
                    f"  {p_emoji} **Death Grip** execute -- +{bonus} bonus!"
                )
                return base_dmg + bonus
        return base_dmg

    def _apply_lifesteal(dmg_dealt: int) -> None:
        """Heal player for a fraction of damage dealt (heal-capped)."""
        if p.lifesteal_pct > 0 and dmg_dealt > 0 and p.hp > 0:
            steal = max(1, int(round(dmg_dealt * p.lifesteal_pct)))
            actual = _heal_fighter(p, steal)
            if actual > 0:
                lines.append(
                    f"  {p_emoji} **Flame Drain** siphons **{actual}** HP "
                    f"({p.hp}/{p.max_hp})."
                )

    def _apply_killing_blow(raw: int) -> int:
        """Add level-gated killing_blow bonus when enemy is at low HP."""
        bonus_pct = float(getattr(p, "killing_blow_bonus_pct", 0.0))
        thresh = float(getattr(p, "killing_blow_thresh", 0.0))
        if bonus_pct > 0 and thresh > 0 and e.hp > 0:
            if e.hp / max(1, e.max_hp) < thresh:
                bonus = max(1, int(round(raw * bonus_pct)))
                lines.append(
                    f"  {p_emoji} **Killing Blow** -- +{bonus} bonus dmg!"
                )
                return raw + bonus
        return raw

    def _apply_static_shock(dmg_dealt: int) -> None:
        """Jolt-style discharge proc on player hits."""
        proc = float(getattr(p, "static_proc_chance", 0.0))
        if proc > 0 and dmg_dealt > 0 and e.hp > 0 and random.random() < proc:
            mult = float(getattr(p, "static_bonus_mult", 0.50) or 0.50)
            bonus = max(1, int(round(dmg_dealt * mult)))
            e.hp = max(0, e.hp - bonus)
            lines.append(
                f"⚡ {p_emoji} **Static Shock** arcs for **{bonus}** bonus dmg "
                f"({e.name}: {e.hp}/{e.max_hp})."
            )

    def _enemy_reflect(dmg_dealt: int) -> None:
        """Enemy-side reflect (tortuga / phantom) bouncing dmg back."""
        rp = float(getattr(e, "reflect_pct", 0.0))
        if rp > 0 and dmg_dealt > 0 and e.hp > 0 and p.hp > 0:
            reflected = max(1, int(round(dmg_dealt * rp)))
            p.hp = max(0, p.hp - reflected)
            lines.append(
                f"  \U0001F6E1️ {e_emoji} **{e.name}** reflects "
                f"**{reflected}** dmg back ({p.name}: {p.hp}/{p.max_hp})."
            )

    if action == "strike":
        lo, hi = INTERACTIVE_STRIKE_RANGE
        dmg = max(1, int(round(p.atk * random.uniform(lo, hi))))
        dmg = _apply_execute(dmg)
        dmg = _apply_killing_blow(dmg)
        if b.enemy_brace_next:
            dmg = max(1, dmg // 2)
            b.enemy_brace_next = False
            lines.append(f"{e_emoji} braced -- damage halved.")
        e.hp = max(0, e.hp - dmg)
        b.player_stamina = min(INTERACTIVE_PLAYER_STAMINA_MAX, b.player_stamina + 1)
        lines.append(
            f"{p_emoji} **{p.name}** strikes for **{dmg}** dmg  "
            f"({e.name}: {e.hp}/{e.max_hp})"
        )
        _apply_lifesteal(dmg)
        _apply_static_shock(dmg)
        _enemy_reflect(dmg)
    elif action == "special":
        if b.player_stamina < INTERACTIVE_SPECIAL_STAMINA_COST:
            lines.append(
                f"{p_emoji} {p.name} tries a Special but lacks stamina!"
            )
            return lines
        b.player_stamina -= INTERACTIVE_SPECIAL_STAMINA_COST
        lo, hi = INTERACTIVE_SPECIAL_RANGE
        dmg = max(1, int(round(p.atk * random.uniform(lo, hi))))
        dmg = _apply_execute(dmg)
        dmg = _apply_killing_blow(dmg)
        if b.enemy_brace_next:
            dmg = max(1, dmg // 2)
            b.enemy_brace_next = False
            lines.append(f"{e_emoji} braced -- damage halved.")
        e.hp = max(0, e.hp - dmg)
        ability = (p.ability_name or "Special").strip() or "Special"
        lines.append(
            f"\U0001F4A5 {p_emoji} **{p.name}** unleashes **{ability}** for "
            f"**{dmg}** dmg  ({e.name}: {e.hp}/{e.max_hp})"
        )
        _apply_lifesteal(dmg)
        _apply_static_shock(dmg)
        _enemy_reflect(dmg)
    elif action == "brace":
        b.player_brace_next = True
        heal_pct = max(0.0, float(b.brace_heal_pct))
        actual = 0
        if heal_pct > 0:
            heal = max(1, int(round(p.max_hp * heal_pct)))
            actual = _heal_fighter(p, heal)
        b.player_stamina = min(INTERACTIVE_PLAYER_STAMINA_MAX, b.player_stamina + 1)
        if heal_pct <= 0:
            lines.append(
                f"\U0001F6E1️ {p_emoji} **{p.name}** braces -- next hit halved. "
                f"_(Bloodbath: no heal)_"
            )
        else:
            lines.append(
                f"\U0001F6E1️ {p_emoji} **{p.name}** braces, healing "
                f"**{actual}** HP  ({p.hp}/{p.max_hp}); next hit halved."
            )
    elif action == "risky":
        roll = random.random()
        if roll < INTERACTIVE_RISKY_HIT_CHANCE:
            lo, hi = INTERACTIVE_RISKY_RANGE
            dmg = max(1, int(round(p.atk * random.uniform(lo, hi))))
            dmg = _apply_execute(dmg)
            dmg = _apply_killing_blow(dmg)
            if b.enemy_brace_next:
                dmg = max(1, dmg // 2)
                b.enemy_brace_next = False
                lines.append(f"{e_emoji} braced -- damage halved.")
            e.hp = max(0, e.hp - dmg)
            lines.append(
                f"\U0001F3AF {p_emoji} **{p.name}** lands a **RISKY** hit for "
                f"**{dmg}** dmg  ({e.name}: {e.hp}/{e.max_hp})"
            )
            _apply_lifesteal(dmg)
            _apply_static_shock(dmg)
            _enemy_reflect(dmg)
        elif roll < INTERACTIVE_RISKY_HIT_CHANCE + INTERACTIVE_RISKY_MISS_CHANCE:
            lines.append(
                f"\U0001F4A8 {p_emoji} {p.name} tried a Risky -- whiff!"
            )
        else:
            recoil = max(1, int(round(p.atk * 0.45)))
            p.hp = max(0, p.hp - recoil)
            lines.append(
                f"\U0001F4A2 {p_emoji} {p.name}'s Risky backfires for **{recoil}** "
                f"self-damage  ({p.hp}/{p.max_hp})"
            )

    return lines


def enemy_ai_turn(b: LiveBattle) -> list[str]:
    """Pick + resolve the opponent's action for the round."""
    p = b.player
    e = b.enemy
    p_emoji = p.emoji or "\U0001F986"
    e_emoji = e.emoji or "\U0001F419"
    lines: list[str] = []

    def _enemy_execute(base_dmg: int) -> int:
        if e.execute_thresh > 0 and p.hp > 0:
            if p.hp / max(1, p.max_hp) < e.execute_thresh:
                bonus = max(1, int(round(base_dmg * e.execute_bonus_pct)))
                lines.append(f"  {e_emoji} **Death Grip** execute -- +{bonus} bonus!")
                return base_dmg + bonus
        return base_dmg

    def _enemy_lifesteal(dmg_dealt: int) -> None:
        if e.lifesteal_pct > 0 and dmg_dealt > 0 and e.hp > 0:
            steal = max(1, int(round(dmg_dealt * e.lifesteal_pct)))
            actual = _heal_fighter(e, steal)
            if actual > 0:
                lines.append(
                    f"  {e_emoji} **Flame Drain** siphons **{actual}** HP "
                    f"({e.hp}/{e.max_hp})."
                )

    def _enemy_killing_blow(raw: int) -> int:
        bonus_pct = float(getattr(e, "killing_blow_bonus_pct", 0.0))
        thresh = float(getattr(e, "killing_blow_thresh", 0.0))
        if bonus_pct > 0 and thresh > 0 and p.hp > 0:
            if p.hp / max(1, p.max_hp) < thresh:
                bonus = max(1, int(round(raw * bonus_pct)))
                lines.append(
                    f"  {e_emoji} **Killing Blow** -- +{bonus} bonus dmg!"
                )
                return raw + bonus
        return raw

    def _enemy_static_shock(dmg_dealt: int) -> None:
        """Jolt-style bonus dmg discharge on hit (enemy side)."""
        proc = float(getattr(e, "static_proc_chance", 0.0))
        if proc > 0 and dmg_dealt > 0 and p.hp > 0 and random.random() < proc:
            mult = float(getattr(e, "static_bonus_mult", 0.50) or 0.50)
            bonus = max(1, int(round(dmg_dealt * mult)))
            p.hp = max(0, p.hp - bonus)
            lines.append(
                f"⚡ {e_emoji} **Static Shock** arcs for **{bonus}** bonus dmg "
                f"({p.name}: {p.hp}/{p.max_hp})."
            )

    def _player_reflect(dmg_dealt: int) -> None:
        """Player-side reflect (tortuga / phantom) bouncing dmg back."""
        rp = float(getattr(p, "reflect_pct", 0.0))
        if rp > 0 and dmg_dealt > 0 and p.hp > 0 and e.hp > 0:
            reflected = max(1, int(round(dmg_dealt * rp)))
            e.hp = max(0, e.hp - reflected)
            lines.append(
                f"  \U0001F6E1️ {p_emoji} **{p.name}** reflects "
                f"**{reflected}** dmg back ({e.name}: {e.hp}/{e.max_hp})."
            )

    def _player_counter(dmg_received: int) -> None:  # noqa: ARG001
        if p.counter_chance > 0 and p.hp > 0 and e.hp > 0:
            if random.random() < p.counter_chance:
                c_dmg = max(1, int(round(p.atk * p.counter_pct)))
                e.hp = max(0, e.hp - c_dmg)
                lines.append(
                    f"  {p_emoji} **Prickle Back** -- {p.name} retaliates for "
                    f"**{c_dmg}** dmg ({e.name}: {e.hp}/{e.max_hp})."
                )

    hp_pct = e.hp / max(1, e.max_hp)
    brace_p = 0.35 if hp_pct < 0.30 else (0.15 if hp_pct < 0.60 else 0.05)
    special_p = 0.15
    roll = random.random()
    if roll < brace_p:
        b.enemy_brace_next = True
        heal = max(1, int(round(e.max_hp * INTERACTIVE_BRACE_HEAL_PCT)))
        actual = _heal_fighter(e, heal)
        lines.append(
            f"\U0001F6E1️ {e_emoji} {e.name} braces, healing **{actual}** HP  "
            f"({e.hp}/{e.max_hp})"
        )
    elif roll < brace_p + special_p:
        lo, hi = INTERACTIVE_SPECIAL_RANGE
        dmg = max(1, int(round(e.atk * random.uniform(lo, hi))))
        dmg = _enemy_execute(dmg)
        dmg = _enemy_killing_blow(dmg)
        if b.player_brace_next:
            dmg = max(1, dmg // 2)
            b.player_brace_next = False
            lines.append(f"{p_emoji} braced -- damage halved.")
        p.hp = max(0, p.hp - dmg)
        ability = (e.ability_name or "Special").strip() or "Special"
        lines.append(
            f"\U0001F4A5 {e_emoji} {e.name} uses **{ability}** for **{dmg}** "
            f"dmg  ({p.name}: {p.hp}/{p.max_hp})"
        )
        _enemy_lifesteal(dmg)
        _enemy_static_shock(dmg)
        _player_reflect(dmg)
        _player_counter(dmg)
    else:
        lo, hi = INTERACTIVE_STRIKE_RANGE
        dmg = max(1, int(round(e.atk * random.uniform(lo, hi))))
        dmg = _enemy_execute(dmg)
        dmg = _enemy_killing_blow(dmg)
        if b.player_brace_next:
            dmg = max(1, dmg // 2)
            b.player_brace_next = False
            lines.append(f"{p_emoji} braced -- damage halved.")
        p.hp = max(0, p.hp - dmg)
        lines.append(
            f"{e_emoji} {e.name} strikes for **{dmg}** dmg  "
            f"({p.name}: {p.hp}/{p.max_hp})"
        )
        _enemy_lifesteal(dmg)
        _enemy_static_shock(dmg)
        _player_reflect(dmg)
        _player_counter(dmg)

    return lines


def apply_round_effects(b: LiveBattle) -> list[str]:
    """Per-round passive effects for interactive battles: regen + overclock.

    Call at the end of each round (after both actions) before advancing
    ``b.round_num``. Mirrors what ``_run`` does via ``_tick_regen`` /
    ``_tick_overclock`` for the auto-resolve engine.
    """
    lines: list[str] = []
    for fighter in (b.player, b.enemy):
        if fighter.hp <= 0:
            continue
        _tick_regen(fighter, lines)
        _tick_overclock(fighter, b.round_num, lines)
    return lines


def compute_battle_bonus(b: LiveBattle) -> float:
    """Return the decimal performance bonus for a winning fight.

    Scales by speed (rounds taken), HP remaining, and action variety.
    Sums independently so a fast + clean + varied fight stacks.
    """
    pct = 0.0
    rounds = b.round_num
    fast_a, fast_b = INTERACTIVE_BONUS_FAST_ROUNDS
    fast_pct_a, fast_pct_b = INTERACTIVE_BONUS_FAST_PCT
    if rounds <= fast_a:
        pct += fast_pct_a
    elif rounds <= fast_b:
        pct += fast_pct_b
    hp_pct = b.player.hp / max(1, b.player.max_hp)
    hp_a, hp_b = INTERACTIVE_BONUS_HP_PCT
    hp_val_a, hp_val_b = INTERACTIVE_BONUS_HP_VALUE
    if hp_pct >= hp_a:
        pct += hp_val_a
    elif hp_pct >= hp_b:
        pct += hp_val_b
    if len(b.actions_used) >= INTERACTIVE_BONUS_VARIETY_THRESHOLD:
        pct += INTERACTIVE_BONUS_VARIETY_PCT
    return round(pct, 4)


# =============================================================================
# Arena modifiers
# =============================================================================
# Each arena fight rolls a random modifier (or "none") that tweaks the live
# battle setup. Modifiers are pure functions on the LiveBattle / Fighter
# pair: applied once at battle start in cogs/buddy.py before the first
# round. They never write to the DB, never touch cc_buddies. The reward
# bonus they grant on win (``reward_bonus``) stacks additively with the
# clean-fight bonus and the streak / tier multipliers in
# services.buddy_economy.resolve_arena_battle.
#
# Tuning rules:
#   * Modifiers must hit BOTH fighters when they're stat changes -- the AI
#     opponent is rolled fresh per fight, so a one-sided buff would just
#     trivialise / brick fights.
#   * reward_bonus is the carrot for taking a harder modifier. "none" pays
#     0; chaotic / risky modifiers pay more.
#   * Brutal modifiers (Glass Cannon, Bloodbath) cap reward_bonus at 0.40
#     so a Diamond + clean + streak + modifier stack still lands inside
#     the network's mint-impact cap on a single fight.

ARENA_MOD_NONE_KEY: str = "none"


@dataclass
class ArenaModifier:
    """Static metadata for one arena modifier.

    ``apply`` mutates the LiveBattle in place. ``reward_bonus`` is the
    decimal added to BUD/BBT payouts on win (0.20 == "+20%"). ``flavor``
    is the player-facing one-liner shown in the intro and result embeds.
    """
    key: str
    label: str
    emoji: str
    flavor: str
    reward_bonus: float = 0.0


# Apply functions take the live battle (so they can read both fighters'
# stats) and return nothing. Defined as module-level free functions so the
# ARENA_MODIFIERS table can carry plain references without lambda capture.

def _mod_apply_none(b: LiveBattle) -> None:
    return


def _mod_apply_high_tide(b: LiveBattle) -> None:
    """Both fighters get +20% ATK -- shorter, punchier fights."""
    b.player.atk *= 1.20
    b.enemy.atk *= 1.20


def _mod_apply_low_gravity(b: LiveBattle) -> None:
    """Both fighters get a SPD floor of 0.90 -- crit-heavy, fast turns."""
    b.player.spd = max(b.player.spd, 0.90)
    b.enemy.spd = max(b.enemy.spd, 0.90)


def _mod_apply_glass_cannon(b: LiveBattle) -> None:
    """+30% ATK, -30% HP for both. Win or get one-shot."""
    for f in (b.player, b.enemy):
        f.atk *= 1.30
        f.max_hp = max(1, int(round(f.max_hp * 0.70)))
        f.hp = min(f.hp, f.max_hp)


def _mod_apply_bloodbath(b: LiveBattle) -> None:
    """No quarter -- Brace stops working as a heal (still halves the next hit)."""
    # Implementation: setting the brace heal to zero is handled at use-site
    # via ``b.brace_heal_pct``. Only the player sees this knob (the AI
    # never braces), so it's a pure player-difficulty knob.
    b.brace_heal_pct = 0.0


def _mod_apply_overclock(b: LiveBattle) -> None:
    """Player starts with full stamina -- spam Specials early."""
    b.player_stamina = INTERACTIVE_PLAYER_STAMINA_MAX


def _mod_apply_chaos(b: LiveBattle) -> None:
    """Both fighters get +15% ATK and +15% HP -- a longer, swingier fight."""
    for f in (b.player, b.enemy):
        f.atk *= 1.15
        f.max_hp = int(round(f.max_hp * 1.15))
        f.hp = f.max_hp


def _mod_apply_marksman(b: LiveBattle) -> None:
    """Crits land more often. SPD scaling is doubled for both."""
    b.player.spd = min(1.0, b.player.spd * 2.0)
    b.enemy.spd = min(1.0, b.enemy.spd * 2.0)


# Modifier table. Weights are relative -- "none" is the modal outcome so
# the arena still feels like the baseline fight most of the time.
# Modifier _apply_ callable is paired by key in ``_MODIFIER_APPLIERS``.
ARENA_MODIFIERS: tuple[tuple[ArenaModifier, int], ...] = (
    (ArenaModifier(
        key=ARENA_MOD_NONE_KEY, label="Standard", emoji="⚔️",
        flavor="No modifier -- a clean baseline fight.",
        reward_bonus=0.0,
    ), 50),
    (ArenaModifier(
        key="high_tide", label="High Tide", emoji="\U0001F30A",
        flavor="Both fighters get +20% ATK. Shorter, punchier fights.",
        reward_bonus=0.10,
    ), 14),
    (ArenaModifier(
        key="low_gravity", label="Low Gravity", emoji="\U0001FA90",
        flavor="SPD floor raised to 0.90 -- crits land more often on both sides.",
        reward_bonus=0.10,
    ), 12),
    (ArenaModifier(
        key="overclock", label="Overclock", emoji="⚡",
        flavor="You start with full stamina -- open with a Special.",
        reward_bonus=0.15,
    ), 10),
    (ArenaModifier(
        key="chaos", label="Chaos", emoji="\U0001F300",
        flavor="Both fighters get +15% ATK and +15% HP. Long swingy fights.",
        reward_bonus=0.20,
    ), 8),
    (ArenaModifier(
        key="marksman", label="Marksman", emoji="\U0001F3AF",
        flavor="SPD doubled on both sides -- crit-heavy combat.",
        reward_bonus=0.20,
    ), 6),
    (ArenaModifier(
        key="glass_cannon", label="Glass Cannon", emoji="\U0001F4A2",
        flavor="+30% ATK, -30% HP for both. One-shots are real.",
        reward_bonus=0.35,
    ), 5),
    (ArenaModifier(
        key="bloodbath", label="Bloodbath", emoji="\U0001FA78",
        flavor="Brace no longer heals (still halves the next hit). No quarter.",
        reward_bonus=0.40,
    ), 4),
)

_MODIFIER_APPLIERS: dict[str, Any] = {
    ARENA_MOD_NONE_KEY: _mod_apply_none,
    "high_tide": _mod_apply_high_tide,
    "low_gravity": _mod_apply_low_gravity,
    "glass_cannon": _mod_apply_glass_cannon,
    "bloodbath": _mod_apply_bloodbath,
    "overclock": _mod_apply_overclock,
    "chaos": _mod_apply_chaos,
    "marksman": _mod_apply_marksman,
}


def roll_arena_modifier() -> ArenaModifier:
    """Pick a random modifier from ARENA_MODIFIERS using its weight."""
    pool = list(ARENA_MODIFIERS)
    total = sum(w for _, w in pool)
    pick = random.uniform(0, total)
    acc = 0.0
    for mod, weight in pool:
        acc += weight
        if pick <= acc:
            return mod
    return pool[0][0]


def get_arena_modifier(key: str) -> ArenaModifier | None:
    """Look up an ArenaModifier by key, or None if unknown."""
    for mod, _w in ARENA_MODIFIERS:
        if mod.key == key:
            return mod
    return None


def apply_arena_modifier(b: LiveBattle, key: str) -> None:
    """Apply the named modifier to ``b`` in place. No-op for unknown keys."""
    fn = _MODIFIER_APPLIERS.get(key)
    if fn is None:
        return
    fn(b)
